# Copyright (c) 2025
# Upload ERPNext file images to WordPress Media Library (Application Password auth)
# and optionally attach them to WooCommerce products.
#
# Based on "restImgUL" logic with raw-bytes upload.

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit, urlunsplit, quote

import ipaddress

import frappe
import requests
import urllib3
from frappe import _
import mimetypes

TIMEOUT = 40  # seconds
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB hard cap


# -------------------------------
# Configuration helpers
# -------------------------------

@dataclass
class WPConfig:
    base_url: str
    username: str
    app_password: str
    verify_ssl: bool = True


def _get_wp_config() -> WPConfig:
    base_url = username = app_pw = None
    verify_ssl = True

    try:
        if frappe.db.exists("DocType", "WooCommerce Fusion Settings"):
            s = frappe.get_doc("WooCommerce Fusion Settings")
            base_url = (s.get("wordpress_url") or "").strip() or None
            username = (s.get("wordpress_user") or "").strip() or None
            app_pw   = (s.get("wordpress_application_password") or "").strip() or None
            if s.get("verify_ssl_certificates") is not None:
                verify_ssl = bool(s.get("verify_ssl_certificates"))
    except Exception:
        frappe.log_error("WooCommerce Fusion: failed to read WP config", frappe.get_traceback())

    if not (base_url and username and app_pw):
        frappe.throw(
            _("WordPress REST configuration missing. Fill it in WooCommerce Fusion Settings "
              "or set WORDPRESS_URL, WORDPRESS_USER and WORDPRESS_APP_PASSWORD.")
        )

    return WPConfig(base_url=base_url.rstrip("/"), username=username, app_password=app_pw, verify_ssl=verify_ssl)


def _new_wp_session(cfg: WPConfig) -> requests.Session:
    sess = requests.Session()
    sess.auth = (cfg.username, cfg.app_password)
    sess.verify = cfg.verify_ssl
    if not cfg.verify_ssl:
        frappe.logger().warning(
            "WordPress session: SSL certificate verification is DISABLED. Vulnerable to MITM."
        )
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return sess


# -------------------------------
# Helpers
# -------------------------------

def _guess_filename_from_path(path: str) -> str:
    return posixpath.basename(path or "").strip() or "erpnext-file"


def _make_absolute_public_file_url(image_path: str) -> str:
    if image_path.startswith(("http://", "https://")):
        iu = urlsplit(image_path)
        encoded_path = quote(iu.path, safe="/")
        return urlunsplit((iu.scheme, iu.netloc, encoded_path, iu.query, iu.fragment))

    base = frappe.utils.get_url().rstrip("/")
    path = image_path if image_path.startswith("/") else f"/{image_path}"
    encoded_path = quote(path, safe="/")
    return f"{base}{encoded_path}"


def _validate_fetch_url(url: str) -> None:
    """Reject URLs that could cause SSRF (private/loopback IPs, non-http schemes)."""
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        frappe.throw(_("Image URL scheme '{0}' is not allowed.").format(parsed.scheme))
    host = parsed.hostname or ""
    if host.lower() in ("localhost", "::1"):
        frappe.throw(_("Image URL points to a disallowed host: {0}").format(host))
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            frappe.throw(_("Image URL points to a disallowed IP address: {0}").format(host))
    except ValueError:
        pass  # hostname — DNS resolution not checked here


def _fetch_image_bytes(image_url: str) -> bytes:
    """
    Return the raw bytes for an image.

    ERPNext-hosted files (/files/… and /private/files/…) are read directly
    from disk — this avoids HTTP entirely, so it works even when the file is
    private (403 over HTTP) or the dev site is served on localhost (rejected
    by the SSRF guard).  Only genuinely external URLs are downloaded via HTTP.
    """
    from urllib.parse import unquote

    parsed = urlsplit(image_url)
    url_path = unquote(parsed.path)  # e.g. /files/foo.jpg or /private/files/foo.jpg

    disk_path = allowed_root = None
    if url_path.startswith("/private/files/"):
        allowed_root = os.path.realpath(frappe.get_site_path("private", "files"))
        disk_path = os.path.realpath(
            os.path.join(frappe.get_site_path("private", "files"), url_path[len("/private/files/"):])
        )
    elif url_path.startswith("/files/"):
        allowed_root = os.path.realpath(frappe.get_site_path("public", "files"))
        disk_path = os.path.realpath(
            os.path.join(frappe.get_site_path("public", "files"), url_path[len("/files/"):])
        )

    if disk_path is not None:
        # Guard against path traversal: stay inside the allowed root.
        if not (disk_path == allowed_root or disk_path.startswith(allowed_root + os.sep)):
            frappe.throw(_("Access to file path is not allowed."))
        if not os.path.isfile(disk_path):
            # Plain exception (no user popup): callers catch this and skip the image.
            raise FileNotFoundError(f"File not found on disk: {url_path}")
        with open(disk_path, "rb") as fh:
            data = fh.read()
        if len(data) > MAX_DOWNLOAD_BYTES:
            frappe.throw(
                _("Image exceeds maximum allowed size of {0} MB.").format(MAX_DOWNLOAD_BYTES // (1024 * 1024))
            )
        return data

    # External URL — download via HTTP (with SSRF guard).
    _validate_fetch_url(image_url)
    resp = requests.get(image_url, stream=True, timeout=TIMEOUT)
    resp.raise_for_status()
    chunks, total = [], 0
    for chunk in resp.iter_content(65536):
        total += len(chunk)
        if total > MAX_DOWNLOAD_BYTES:
            frappe.throw(
                _("Image exceeds maximum allowed size of {0} MB.").format(MAX_DOWNLOAD_BYTES // (1024 * 1024))
            )
        chunks.append(chunk)
    return b"".join(chunks)


# -------------------------------
# Upload to WordPress
# -------------------------------
def delete_media_by_ids(media_ids: dict) -> dict:
    """
    Delete WordPress media based on dictionary keys (media IDs).
    
    Args:
        media_ids: Dictionary where keys are media IDs to delete
                  Example: {123: "hash1", 456: "hash2", 789: "hash3"}
    
    Returns:
        Dictionary with deletion results
    """
    cfg = _get_wp_config()
    sess = _new_wp_session(cfg)
    
    results = {
        "deleted": [],
        "failed": [],
        "total": len(media_ids)
    }
    
    for media_id in media_ids.keys():
        try:
            # WordPress REST API endpoint to delete media
            delete_endpoint = f"{cfg.base_url}/wp-json/wp/v2/media/{media_id}"
            
            # Use force=true to permanently delete (skip trash)
            resp = sess.delete(
                delete_endpoint,
                params={"force": True},
                timeout=TIMEOUT
            )
            
            if resp.status_code in (200, 404):
                results["deleted"].append(media_id)
                action = "Deleted" if resp.status_code == 200 else "Already absent"
                frappe.logger().info(f"{action}: WordPress media ID {media_id}")
            else:
                results["failed"].append({
                    "id": media_id, 
                    "error": f"HTTP {resp.status_code}: {resp.text}"
                })
                frappe.logger().error(f"Failed to delete media ID {media_id}: {resp.status_code}")
                
        except Exception as e:
            results["failed"].append({
                "id": media_id,
                "error": str(e)
            })
            frappe.log_error("Media deletion failed", frappe.get_traceback())
    
    return results

def upload_media_from_url(image_url: str, filename: Optional[str] = None, alt_text: Optional[str] = None) -> dict:
    """
    Upload an ERPNext image to WordPress Media Library.
    Private files (/private/files/…) are read from disk; public files are
    fetched via HTTP.  Returns the media JSON with id + link.
    """
    cfg = _get_wp_config()
    sess = _new_wp_session(cfg)
    if not filename:
        filename = _guess_filename_from_path(image_url)

    try:
        file_bytes = _fetch_image_bytes(image_url)
    except frappe.exceptions.ValidationError:
        raise
    except Exception as e:
        _log_and_throw("Failed to download ERPNext image", exception=e)

    media_endpoint = f"{cfg.base_url}/wp-json/wp/v2/media"
    mime = mimetypes.guess_type(filename)[0] or "image/jpeg"
    safe_filename = filename.encode("ascii", "ignore").decode("ascii")
    headers = {
        "Content-Type": mime,
        "Content-Disposition": f'attachment; filename="{safe_filename}"',
        "Accept": "application/json",
    }

    try:
        
        res = sess.post(media_endpoint, data=file_bytes, headers=headers, timeout=TIMEOUT)
        
    except Exception as e:
        _log_and_throw("Media upload request to WordPress failed", exception=e)

    if res.status_code not in (200, 201):
        _log_and_throw("Media upload failed", response=res)
    new_dict = res.json()
    new_id = new_dict.get("id")
    link = new_dict.get("guid", {}).get("rendered")

    # Update alt_text after upload if provided
    if alt_text and new_id:
        try:
            update_endpoint = f"{cfg.base_url}/wp-json/wp/v2/media/{new_id}"
            update_data = {"alt_text": alt_text.strip()}
            
            update_resp = sess.post(
                update_endpoint,
                json=update_data,
                headers={"Content-Type": "application/json"},
                timeout=TIMEOUT
            )
            
            if update_resp.status_code in (200, 201):
                frappe.logger().info(f"Updated alt_text for media ID {new_id}")
                new_dict.update(update_resp.json())
            else:
                frappe.logger().warning(f"Failed to update alt_text for media ID {new_id}")
                
        except Exception as e:
            frappe.logger().warning(f"Failed to update alt_text: {str(e)}")

    frappe.logger().info(f"Uploaded to WP → ID {new_id}, Link {link}, Alt: {alt_text}")
    return {"id": new_id, "link": link, **new_dict}


def attach_media_to_wc_product(product_id: int, images: list[dict]) -> dict:
    """
    Attach uploaded media to a WooCommerce product.
    Expects a list of {"id": ..., "position": ...} dicts.
    """
    cfg = _get_wp_config()
    sess = _new_wp_session(cfg)

    wc_endpoint = f"{cfg.base_url}/wp-json/wc/v3/products/{product_id}"
    body = {"images": images}

    resp = sess.put(
        wc_endpoint,
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=TIMEOUT,
    )

    if resp.status_code not in (200, 201):
        _log_and_throw("WooCommerce product update failed", response=resp)

    return resp.json()

def attach_media_to_wc_category(category_id: int, media_id: int) -> dict:
    """
    Set the image of a WooCommerce category to an existing Media attachment.

    Args:
        category_id: WooCommerce category ID.
        media_id: WordPress Media (attachment) ID to set as the category image.

    Returns:
        The updated category JSON.

    Raises:
        Uses _log_and_throw(...) if the WooCommerce API call fails.
    """
    cfg = _get_wp_config()
    sess = _new_wp_session(cfg)

    wc_endpoint = f"{cfg.base_url}/wp-json/wc/v3/products/categories/{category_id}"
    body = {"image": {"id": int(media_id)}}

    resp = sess.put(
        wc_endpoint,
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=TIMEOUT,
    )

    if resp.status_code not in (200, 201):
        _log_and_throw("WooCommerce category update failed", response=resp)

    return resp.json()
# -------------------------------
# ERPNext-facing helpers
# -------------------------------

@frappe.whitelist()
def upload_item_image_to_wp(item_code: str) -> dict:
    """
    Upload the Item.image to WordPress Media.
    """
    item = frappe.get_doc("Item", item_code)
    image_path = (item.image or "").strip()
    if not image_path:
        frappe.throw(_("Item {0} does not have an image.").format(frappe.bold(item_code)))

    image_url = _make_absolute_public_file_url(image_path)
    media = upload_media_from_url(image_url, filename=_guess_filename_from_path(image_path))
    frappe.msgprint(_("Uploaded to WordPress Media ID {0}").format(media.get("id")), indicator="green")
    return media


@frappe.whitelist()
def upload_item_image_and_attach(item_code: str, wc_product_id: int) -> dict:
    media = upload_item_image_to_wp(item_code)
    product = attach_media_to_wc_product(int(wc_product_id), int(media["id"]))
    frappe.msgprint(_("Attached media {0} to product {1}").format(media["id"], wc_product_id), indicator="green")
    return {"media": media, "product": product}


# -------------------------------
# Error handling
# -------------------------------

def _log_and_throw(msg: str, exception: Exception | None = None, response: requests.Response | None = None):
    detail = ""
    if exception:
        detail += frappe.get_traceback()
    if response is not None:
        detail += f"\nResponse Code: {response.status_code}\nResponse Text: {response.text}"
        try:
            detail += f"\nRequest URL: {response.request.url}"
        except Exception:
            pass

    log = frappe.log_error(title="WordPress Media Error", message=f"{msg}\n\n{detail}")
    link = frappe.utils.get_link_to_form("Error Log", log.name)
    frappe.throw(_("{0}. See Error Log {1}.").format(msg, link))
