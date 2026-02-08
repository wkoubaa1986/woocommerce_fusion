# Copyright (c) 2025
# Synchronize ERPNext Item Groups with WooCommerce Categories
# Handles create/update/delete logic, image upload, and sync flags.

import frappe
import hashlib
import json
from woocommerce_fusion.tasks.utils import APIWithRequestLogging
from woocommerce_fusion.integrations.wp_media import upload_media_from_url, _make_absolute_public_file_url, _guess_filename_from_path, attach_media_to_wc_category
from woocommerce_fusion.integrations.content_enrichment import generate_item_group_seo_minimal

import pdb
import requests  # add this

_VERIFY_TLS = False

def get_verify_tls() -> bool:
    """Lit le setting uniquement quand Frappe est initialisé (runtime)."""
    global _VERIFY_TLS
    if _VERIFY_TLS is None:
        v = frappe.db.get_single_value("WooCommerce Fusion Settings", "verify_ssl_certificates")
        # Choisis ton défaut : True est généralement le meilleur
        _VERIFY_TLS = True if v is None else bool(v)
    return _VERIFY_TLS
def _sha1_of_remote(url: str, chunk: int = 65536) -> str | None:
    """Stream remote file and return SHA1 hex; None on failure."""
    try:
        h = hashlib.sha1()
        with requests.get(url, stream=True, timeout=60, verify=get_verify_tls()) as r:
            r.raise_for_status()
            for part in r.iter_content(chunk_size=chunk):
                if part:
                    h.update(part)
        return h.hexdigest()
    except Exception:
        frappe.log_error("Hashing remote image failed", frappe.get_traceback())
        return None
    
def compute_hash(payload: dict) -> str:
    """Compute a hash of payload to detect changes."""
    return hashlib.md5(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def ensure_category(item_group, wc_api):
    """
    Ensure one category exists/updated in WooCommerce.
    - Creates if missing
    - Updates if name or image changed
    - Deletes if sync disabled but wc_id exists
    """
    
    wc_id = item_group.custom_woocommerce_id
    wc_id = frappe.utils.cint(wc_id)
    enable_sync = bool(int(item_group.custom_enable_sync or 0))


    # treat as disabled
    if enable_sync and frappe.utils.cint(item_group.custom_generate_seo) == 1:
        generate_item_group_seo_minimal(item_group_name=item_group.name)
        item_group = frappe.get_doc("Item Group", item_group.name)

    # CASE 0: Sync disabled → delete category if exists
    if not enable_sync and wc_id != 0:

        print('deleting category', item_group.name, wc_id)
        wc_api.delete(f"products/categories/{wc_id}", params={"force": True})
        frappe.db.set_value("Item Group", item_group.name, "custom_woocommerce_id", None)
        frappe.db.set_value("Item Group", item_group.name, "custom_last_sync_hash", None)
        frappe.logger().info(f"🗑 Deleted WC category {wc_id} for {item_group.name}, sync disabled")
        return {"status": "deleted", "wc_category_id": wc_id}

    if not enable_sync:
        frappe.logger().info(f"⏭ Skipped {item_group.name}, sync disabled")
        return {"status": "skipped", "wc_category_id": wc_id}

    # Ensure parent exists first
    parent_id = None
    if item_group.parent_item_group and not(item_group.parent_item_group in ["Tous les Groupes d'Articles", "All Item Groups"]):
        parent_doc = frappe.get_doc("Item Group", item_group.parent_item_group)
        parent_result = ensure_category(parent_doc, wc_api)
        parent_id = parent_result.get("wc_category_id")
    
    current = None
    if wc_id !=0:
        current = wc_api.get(f"products/categories/{wc_id}").json()
        if current.get("data", {}).get("status") == 404:
            current = None
            wc_id = None  # force recreation
        # --- Image decision logic (upload only if needed) ---
    
    image_path = (item_group.image or "").strip()
    erp_image_id_to_set = None  # will be set if we decide to upload ERP image
    erp_img_sha1 = None

    if image_path:
        erp_url = _make_absolute_public_file_url(image_path)
        erp_img_sha1 = _sha1_of_remote(erp_url)

        # Cache ERP image hash on the Item Group (optional but useful)
        if erp_img_sha1 and getattr(item_group, "custom_last_image_hash", None) != erp_img_sha1:
            frappe.db.set_value("Item Group", item_group.name, "custom_last_image_hash", erp_img_sha1, update_modified=False)

        # Compare with current WC category image (if category exists)
        wc_current_src = (current or {}).get("image", {}) or {}
        wc_current_src = wc_current_src.get("src")

        same_image = False
        if wc_current_src and erp_img_sha1:
            wc_img_sha1 = _sha1_of_remote(wc_current_src)
            if wc_img_sha1 and wc_img_sha1 == erp_img_sha1:
                same_image = True
        # If creating OR WC has no image OR images differ → upload ERP image and plan to set it
        if (not wc_id) or (not wc_current_src) or (not same_image):
            try:

                media = upload_media_from_url(erp_url, filename=_guess_filename_from_path(image_path))

                if media and media.get("id"):
                    erp_image_id_to_set = int(media["id"])
            except Exception:
                frappe.log_error("Category image upload failed", frappe.get_traceback())

    # Build payload (include image only if we’ve decided to set it)
    payload = {
        "name": item_group.name,
        "slug": item_group.name.lower().replace(" ", "-"),
        "parent": parent_id or 0,
    }
    # Add Rank Math SEO metadata
    seo_meta = []

    if hasattr(item_group, "custom_seo_title") and item_group.custom_seo_title:
        seo_meta.append({
            "key": "rank_math_title",
            "value": item_group.custom_seo_title.strip()
        })

    if hasattr(item_group, "custom_seo_description") and item_group.custom_seo_description:
        payload['description'] = item_group.custom_seo_description.strip()
        seo_meta.append({
            "key": "rank_math_description",
            "value": item_group.custom_seo_description.strip()
        })

    if hasattr(item_group, "custom_seo_keyword") and item_group.custom_seo_keyword:
        seo_meta.append({
            "key": "rank_math_focus_keyword",
            "value": item_group.custom_seo_keyword.strip()
        })



    if seo_meta:
        payload["meta_data"] = seo_meta
    if erp_image_id_to_set:
        payload["image"] = {"id": erp_image_id_to_set}

    payload_hash = compute_hash(payload)

       # CASE 1: Already mapped -> check/update
    if wc_id:
        
        wc_name = (current or {}).get("name")
        wc_parent = (current or {}).get("parent") or 0
        wc_image_id = ((current or {}).get("image") or {}).get("id")

        name_changed = wc_name != item_group.name
        parent_changed = wc_parent != (parent_id or 0)
        img_changed = bool(erp_image_id_to_set) and (erp_image_id_to_set != wc_image_id)

        if not (name_changed or parent_changed or img_changed) and item_group.custom_last_sync_hash == payload_hash and not seo_meta:
            frappe.logger().info(f"✔ No change for {item_group.name}, skipped")
            return {"status": "skipped", "wc_category_id": wc_id}

        resp = wc_api.put(f"products/categories/{wc_id}", payload).json()
        frappe.db.set_value("Item Group", item_group.name, "custom_last_sync_hash", payload_hash)
        frappe.logger().info(f"✅ Updated WC category: {item_group.name} (ID {wc_id})")
        return {"status": "updated", "wc_category_id": wc_id, "resp": resp}

    # CASE 2: Not mapped yet or deleted -> create

    new_cat = wc_api.post("products/categories", payload).json()
    wc_id = new_cat.get("id")
    frappe.db.set_value("Item Group", item_group.name, "custom_woocommerce_id", wc_id)
    frappe.db.set_value("Item Group", item_group.name, "custom_last_sync_hash", payload_hash)
    frappe.logger().info(f"✨ Created WC category: {item_group.name} (ID {wc_id})")
    return {"status": "created", "wc_category_id": wc_id, "resp": new_cat}


@frappe.whitelist()
def sync_item_groups_to_wc(wc_server: str):
    """
    Synchronize ALL ERPNext Item Groups -> WooCommerce Categories
    """
    wc_server_doc = frappe.get_doc("WooCommerce Server", wc_server)
    wc_api = APIWithRequestLogging(
        url=wc_server_doc.woocommerce_server_url,
        consumer_key=wc_server_doc.api_consumer_key,
        consumer_secret=wc_server_doc.api_consumer_secret,
        version="wc/v3",
        timeout=40,
        verify_ssl=get_verify_tls(),
    )

    results = []
    item_groups = frappe.get_all("Item Group", fields=["name"])
    for ig in item_groups:
        doc = frappe.get_doc("Item Group", ig.name)
        result = ensure_category(doc, wc_api)
        results.append({"item_group": ig.name, **result})

    frappe.db.commit()
    return results


@frappe.whitelist()
def sync_single_item_group(wc_server: str, item_group_name: str):
    """
    Synchronize ONE specific Item Group -> WooCommerce Category
    """
    wc_server_doc = frappe.get_doc("WooCommerce Server", wc_server)
    wc_api = APIWithRequestLogging(
        url=wc_server_doc.woocommerce_server_url,
        consumer_key=wc_server_doc.api_consumer_key,
        consumer_secret=wc_server_doc.api_consumer_secret,
        version="wc/v3",
        timeout=40,
        verify_ssl=get_verify_tls(),
    )

    item_group = frappe.get_doc("Item Group", item_group_name)
    result = ensure_category(item_group, wc_api)

    frappe.db.commit()
    return {"item_group": item_group_name, **result}
