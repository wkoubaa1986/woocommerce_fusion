import json
from dataclasses import dataclass
from datetime import datetime
from math import prod
from typing import List, Optional, Tuple
import re
import frappe
from erpnext.stock.doctype.item.item import Item
from frappe import ValidationError, _, _dict
from frappe.query_builder import Criterion
from frappe.utils import get_datetime, now
from jsonpath_ng.ext import parse

from woocommerce_fusion.exceptions import SyncDisabledError
from woocommerce_fusion.tasks.sync import SynchroniseWooCommerce
from woocommerce_fusion.tasks.utils import APIWithRequestLogging
from woocommerce_fusion.tasks.sync_item_discount import sync_single_item_discount
from woocommerce_fusion.tasks.sync_brands_attributes import sync_attribute, sync_brand
from woocommerce_fusion.tasks.sync_categories import sync_single_item_group, _sha1_of_remote
from woocommerce_fusion.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)
from woocommerce_fusion.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)
from woocommerce_fusion.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)
# add to your imports
from woocommerce_fusion.integrations import wp_media
from woocommerce_fusion.integrations.content_enrichment import generate_website_contenant, classify_item_group, retouch_item_images, classify_item_collections, treat_left_item_images
from woocommerce_fusion.integrations.item_images_treatment import dedupe_item_images

from typing import List
import mimetypes
import posixpath
import re
from urllib.parse import urljoin, urlparse

import frappe
from frappe.utils import get_url
from frappe.utils import flt, cint

def _get_shipping_class_slug(item) -> str:
    # ERPNext fields (adapt if your fieldnames differ)
    is_vol = cint(item.custom_is_volumineux) == 1

    # choose the weight field you trust (SEO weight or ERPNext weight)
    weight = flt(item.custom_seo_weight or 0)

    if is_vol:
        return "volumineux"
    if weight >= 20:
        return "lourd"
    return ""  # no class
_VERIFY_TLS = None

def get_verify_tls() -> bool:
    """Lit le setting uniquement quand Frappe est initialisé (runtime)."""
    global _VERIFY_TLS
    if _VERIFY_TLS is None:
        v = frappe.db.get_single_value("WooCommerce Fusion Settings", "verify_ssl_certificates")
        # Choisis ton défaut : True est généralement le meilleur
        _VERIFY_TLS = True if v is None else bool(v)
    return _VERIFY_TLS

def _extract_meta_key_from_jsonpath(expr: str) -> str | None:
	"""
	Supports patterns like:
	meta_data[?(@.key=="rank_math_title")].value
	meta_data[?(@.key=='rank_math_title')].value
	"""
	m = re.search(r'meta_data\[\?\(@\.key==[\'"]([^\'"]+)[\'"]\)\]\.value', expr)
	return m.group(1) if m else None

def _upsert_meta(wc_obj, key: str, value) -> bool:
	"""
	Ensure wc_obj.meta_data contains a dict {'key': key, 'value': value}.
	Returns True if changed.
	"""
	meta = getattr(wc_obj, "meta_data", None)
	if meta is None:
		setattr(wc_obj, "meta_data", [])
		meta = wc_obj.meta_data

	# update if exists
	for row in meta:
		if isinstance(row, dict) and row.get("key") == key:
			if row.get("value") != value:
				row["value"] = value
				return True
			return False

	# append if not found
	meta.append({"key": key, "value": value})
	return True

def _safe_json_list(value, *, default=None):
    """Return a list from JSON stored in DB (handles 'null', None, '', bad json)."""
    if default is None:
        default = []

    if value is None:
        return list(default)

    # sometimes the field is already a python object
    if isinstance(value, list):
        return value
    if isinstance(value, (dict, int, float, bool)):
        return list(default)

    s = str(value).strip()
    if not s or s.lower() in ("null", "none"):
        return list(default)

    try:
        parsed = json.loads(s)
    except Exception:
        frappe.log_error("Invalid JSON in woocommerce_product.attributes", f"value={s}")
        return list(default)

    return parsed if isinstance(parsed, list) else list(default)





def _upload_item_images_to_wp_and_attach(wc_api, item, wc_product: dict) -> list[dict]:
    """
    Rules:
    - Use ERPNext File attachments as the source of truth.
    - Upload ONLY File rows where custom_treated_ai == 1.
    - For each uploaded media, set WP title=custom_wp_title and alt=custom_wp_alternative.
    - After upload, write media id back to File.custom_wp_id.
    - For images currently attached to WC product:
        - if their media id is NOT present in any File.custom_wp_id for this item -> delete them from WP media.
      IMPORTANT (variation):
        - also consider WPC additional images stored in meta_data[wpcvi_images]
    - Then attach the selected/ordered images to the WC product/variation.
    """

    MAX_IMAGES_RETOUCH = 10
    MAKE_PUBLIC = 1
    SET_WEBSITE_IMAGE = 1
    WRITE_ALT_TO_FIELD = ""
    ALT_LOCALE = "fr"

    DRY_RUN = False

    dedupe_item_images(item_name=item.item_code, dry_run=DRY_RUN, detach_missing=True)
    item.reload()

    treat_left_item_images(item_name=item.item_code)
    item.reload()

    # -------- Helpers --------
    def _is_image_url(url: str, filename: str | None = None) -> bool:
        if not url and not filename:
            return False
        cand = (filename or url or "").lower()
        mt, _ = mimetypes.guess_type(cand)
        if mt and mt.startswith("image/"):
            return True
        return cand.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"))

    def _abs_erpnext_url(file_url: str) -> str:
        u = (file_url or "").strip()
        if not u:
            return u
        if u.lower().startswith(("http://", "https://")):
            return u
        base = get_url().rstrip("/") + "/"
        return urljoin(base, u.lstrip("/"))

    def _fallback_alt() -> str:
        txt = (
            (getattr(item, "item_name", "") or getattr(item, "item_code", "") or getattr(item, "name", "") or "")
            .strip()
        )
        txt = re.sub(r"\s+", " ", txt)
        return (txt[:120] if txt else "Image produit")

    def _wp_update_media(media_id: int, title: str | None, alt_text: str | None) -> None:
        payload = {}
        if title:
            payload["title"] = title
        if alt_text:
            payload["alt_text"] = alt_text
        if not payload:
            return
        try:
            if hasattr(wp_media, "update_media"):
                wp_media.update_media(media_id, payload)  # type: ignore
                return
            if hasattr(wp_media, "request"):
                wp_media.request("POST", f"/wp/v2/media/{media_id}", json=payload)  # type: ignore
                return
        except Exception:
            frappe.log_error("WP Media update (title/alt) failed", frappe.get_traceback())

    def _wp_media_exists(media_id: int) -> bool:
        try:
            if hasattr(wp_media, "get_media"):
                _ = wp_media.get_media(media_id)  # type: ignore
                return True
            if hasattr(wp_media, "request"):
                r = wp_media.request("GET", f"/wp/v2/media/{media_id}")  # type: ignore
                if isinstance(r, dict):
                    return True
                if hasattr(r, "status_code"):
                    return int(r.status_code) < 400
                return True
        except Exception:
            tb = frappe.get_traceback().lower()
            if "404" in tb or "not found" in tb:
                return False
            return True
        return True

    # ✅ ADJUST 1: flag variation early (used later for payload + deletion logic)
    is_variation = bool(getattr(item, "variant_of", None))

    # -------- Determine WC product id + link_site --------
    wc_product_id = wc_product.get("woocommerce_id") or wc_product.get("id")
    if not wc_product_id:
        frappe.log_error("WC product ID missing for image sync", f"wc_product={wc_product}")
        return []

    link_site = f"products/{wc_product_id}"

    # Variations support (kept)
    if is_variation:
        woo_parent_id = frappe.db.get_value(
            "Item WooCommerce Server",
            {
                "parent": item.variant_of,
                "parenttype": "Item",
                "parentfield": "woocommerce_servers",
                "woocommerce_server": wc_product.get("woocommerce_server"),
                "enabled": 1,
            },
            "woocommerce_id",
        )
        if woo_parent_id:
            link_site = f"products/{woo_parent_id}/variations/{wc_product_id}"

    # -------- Fetch WC product current images --------
    try:
        wc_product_json = wc_api.get(link_site).json()
    except Exception:
        frappe.log_error("Failed to fetch WC product", frappe.get_traceback())
        wc_product_json = {}

    wc_attached_media_ids: set[int] = set()
    for img in (wc_product_json.get("images") or []):
        if img.get("id"):
            try:
                wc_attached_media_ids.add(int(img["id"]))
            except Exception:
                pass

    # ✅ ADJUST 2: for variations, include WPC additional IDs (meta_data[wpcvi_images])
    if is_variation:
        for m in (wc_product_json.get("meta_data") or []):
            if m.get("key") == "wpcvi_images" and m.get("value"):
                try:
                    extra = [int(x.strip()) for x in str(m["value"]).split(",") if x.strip().isdigit()]
                    wc_attached_media_ids.update(extra)
                except Exception:
                    pass

    # -------- Load ERPNext Files attached to this Item (source of truth) --------
    def _norm_url(u: str) -> str:
        u = (u or "").strip()
        if not u:
            return ""
        p = urlparse(u)
        return (p.path or "").strip() if p.scheme else u

    def _get_files(attached_to_name: str):
        it = frappe.get_doc("Item", attached_to_name)

        rows = frappe.get_all(
            "File",
            filters={
                "attached_to_doctype": "Item",
                "attached_to_name": attached_to_name,
                "is_folder": 0,
            },
            fields=[
                "name",
                "file_url",
                "file_name",
                "custom_treated_ai",
                "custom_wp_id",
                "custom_wp_title",
                "custom_wp_alternative",
                "creation",
                "attached_to_field",
            ],
            order_by="creation asc",
        )

        if not rows:
            return rows

        item_image = _norm_url(getattr(it, "image", "") or "")
        if not item_image:
            return rows

        # 1) Prefer file explicitly attached to field "image"
        for i, r in enumerate(rows):
            if r.get("attached_to_field") == "image":
                if i == 0:
                    return rows
                return [rows[i]] + rows[:i] + rows[i + 1 :]

        # 2) Else match by URL equality
        idx = None
        for i, r in enumerate(rows):
            if _norm_url(r.get("file_url")) == item_image:
                idx = i
                break

        if idx is None or idx == 0:
            return rows

        # Move only that one to the front, preserve the rest order
        return [rows[idx]] + rows[:idx] + rows[idx + 1 :]

    file_rows = _get_files(item.name)
    if not file_rows and getattr(item, "item_code", None) and item.item_code != item.name:
        file_rows = _get_files(item.item_code)

    treated_files = []
    for f in file_rows:
        url = (f.get("file_url") or "").strip()
        if not url:
            continue
        if int(f.get("custom_treated_ai") or 0) != 1:
            continue
        if not _is_image_url(url, f.get("file_name")):
            continue
        treated_files.append(f)

    # ERPNext set of "valid WP media ids" for this item
    erp_wp_ids: set[int] = set()
    for f in treated_files:
        if f.get("custom_wp_id"):
            try:
                erp_wp_ids.add(int(f["custom_wp_id"]))
            except Exception:
                pass

    # Delete WC-attached images that are NOT referenced by ERPNext Files
    unused_on_wc = sorted(list(wc_attached_media_ids - erp_wp_ids))
    if unused_on_wc:
        try:
            wp_media.delete_media_by_ids(unused_on_wc)
            frappe.logger().info(
                f"Deleted {len(unused_on_wc)} WP media not referenced by ERPNext (custom_wp_id)"
            )
        except Exception:
            frappe.log_error("WP Media delete failed", frappe.get_traceback())

    # -------- Upload / reuse media in order --------
    media_list: list[dict] = []
    images_payload: list[dict] = []

    for pos, f in enumerate(treated_files):
        url_rel = (f.get("file_url") or "").strip()
        url_abs = _abs_erpnext_url(url_rel)

        filename = f.get("file_name") or (posixpath.basename(url_rel) or "erpnext-file")

        wp_title = (f.get("custom_wp_title") or "").strip() or None
        wp_alt = (f.get("custom_wp_alternative") or "").strip() or _fallback_alt()

        try:
            media_id = None

            if f.get("custom_wp_id"):
                try:
                    candidate = int(f["custom_wp_id"])
                    if _wp_media_exists(candidate):
                        media_id = candidate
                    else:
                        frappe.db.set_value("File", f["name"], "custom_wp_id", None)
                        media_id = None
                except Exception:
                    media_id = None

            if media_id is None:
                m = wp_media.upload_media_from_url(
                    url_abs,
                    filename=filename,
                    alt_text=wp_alt,
                )
                media_list.append(m)
                media_id = int(m["id"])
                frappe.db.set_value("File", f["name"], "custom_wp_id", media_id)
                frappe.logger().info(f"Uploaded new image {media_id} for {url_abs}")

            _wp_update_media(int(media_id), wp_title, wp_alt)
            images_payload.append({"id": int(media_id), "position": pos})

        except Exception as e:
            error_msg = f"Image sync failed for File={f.get('name')} url={url_abs}: {e}"
            frappe.log_error("WP Media upload/attach failed", f"{error_msg}\n\n{frappe.get_traceback()}")
            continue

    # ✅ ADJUST 3: compute featured + additional ids for variation WPC
    featured_id = None
    additional_ids: list[int] = []
    if images_payload:
        featured_id = int(images_payload[0]["id"])
        additional_ids = [int(x["id"]) for x in images_payload[1:]]

    # -------- Update WooCommerce product / variation images --------
    if images_payload:
        try:
            if is_variation:
                payload = {
                    "image": {"id": int(featured_id)} if featured_id else None,
                    "meta_data": [{"key": "wpcvi_images", "value": ",".join(str(i) for i in additional_ids)}],
                }
                payload = {k: v for k, v in payload.items() if v is not None}

                response = wc_api.put(link_site, payload)
                _ = response.json() if hasattr(response, "json") else response

                frappe.logger().info(
                    f"✅ Updated WC variation {wc_product_id}: featured={featured_id}, "
                    f"wpcvi_images={','.join(map(str, additional_ids))}"
                )
            else:
                response = wc_api.put(link_site, {"images": images_payload})
                _ = response.json() if hasattr(response, "json") else response
                frappe.logger().info(f"✅ Updated WC product {wc_product_id} with {len(images_payload)} images")

        except Exception:
            frappe.log_error("WC API product/variation image update failed", frappe.get_traceback())

    return media_list





def run_item_sync_from_hook(doc, method):
	"""
	Intended to be triggered by a Document Controller hook from Item
	"""
	if (
		doc.doctype == "Item"
		and not doc.flags.get("created_by_sync", None)
		and len(doc.woocommerce_servers) > 0
	):
		frappe.msgprint(
			_("Background sync to WooCommerce triggered for {0} {1}").format(frappe.bold(doc.name), method),
			indicator="blue",
			alert=True,
		)
		frappe.enqueue(clear_sync_hash_and_run_item_sync, item_code=doc.name)


@frappe.whitelist()
def run_item_sync(
	item_code: Optional[str] = None,
	item: Optional[Item] = None,
	woocommerce_product_name: Optional[str] = None,
	woocommerce_product: Optional[WooCommerceProduct] = None,
	enqueue=False,
) -> Tuple[Item, WooCommerceProduct]:
	"""
	Helper funtion that prepares arguments for item sync
	"""
	
	# Validate inputs, at least one of the parameters should be provided
	if not any([item_code, item, woocommerce_product_name, woocommerce_product]):
		raise ValueError(
			(
				"At least one of item_code, item, woocommerce_product_name, woocommerce_product parameters required"
			)
		)
	
	# Get ERPNext Item and WooCommerce product if they exist
	if woocommerce_product or woocommerce_product_name:
		
		if not woocommerce_product:
			woocommerce_product = frappe.get_doc(
				{"doctype": "WooCommerce Product", "name": woocommerce_product_name}
			)
			woocommerce_product.load_from_db()

		# Trigger sync
		sync = SynchroniseItem(woocommerce_product=woocommerce_product)
		if enqueue:
			frappe.enqueue(sync.run)
		else:
			sync.run()

	elif item or item_code:
		
		if not item:
			item = frappe.get_doc("Item", item_code)
		if not item.woocommerce_servers:
			frappe.throw(_("No WooCommerce Servers defined for Item {0}").format(item_code))
		for wc_server in item.woocommerce_servers:
			# Trigger sync for every linked server
			sync = SynchroniseItem(
				item=ERPNextItemToSync(item=item, item_woocommerce_server_idx=wc_server.idx)
			)
			if enqueue:
				frappe.enqueue(sync.run)
			else:
				sync.run()

	return (
		sync.item.item if sync and sync.item else None,
		sync.woocommerce_product if sync else None,
	)


def sync_woocommerce_products_modified_since(date_time_from=None):
	"""
	Get list of WooCommerce products modified since date_time_from
	"""
	wc_settings = frappe.get_doc("WooCommerce Integration Settings")

	if not date_time_from:
		date_time_from = wc_settings.wc_last_sync_date_items

	# Validate
	if not date_time_from:
		error_text = _(
			"'Last Items Syncronisation Date' field on 'WooCommerce Integration Settings' is missing"
		)
		frappe.log_error(
			"WooCommerce Items Sync Task Error",
			error_text,
		)
		raise ValueError(error_text)

	wc_products = get_list_of_wc_products(date_time_from=date_time_from)
	for wc_product in wc_products:
		try:
			run_item_sync(woocommerce_product=wc_product, enqueue=True)
		# Skip items with errors, as these exceptions will be logged
		except Exception:
			pass

	frappe.db.set_single_value("WooCommerce Settings", "wc_last_sync_date_items", now())


@dataclass
class ERPNextItemToSync:
	"""Class for keeping track of an ERPNext Item and the relevant WooCommerce Server to sync to"""

	item: Item
	item_woocommerce_server_idx: int

	@property
	def item_woocommerce_server(self):
		return self.item.woocommerce_servers[self.item_woocommerce_server_idx - 1]


class SynchroniseItem(SynchroniseWooCommerce):
	"""
	Class for managing synchronisation of WooCommerce Product with ERPNext Item
	"""

	def __init__(
		self,
		servers: List[WooCommerceServer | _dict] = None,
		item: Optional[ERPNextItemToSync] = None,
		woocommerce_product: Optional[WooCommerceProduct] = None,
	) -> None:
		super().__init__(servers)
		self.item = item
		self.woocommerce_product = woocommerce_product
		self.settings = frappe.get_cached_doc("WooCommerce Integration Settings")


	def run(self):
		"""
		Run synchronisation
		"""
		try:
			self.get_corresponding_item_or_product()
			self.sync_wc_product_with_erpnext_item()
		except Exception as err:
			try:
				woocommerce_product_dict = (
					self.woocommerce_product.as_dict()
					if isinstance(self.woocommerce_product, WooCommerceProduct)
					else self.woocommerce_product
				)
			except ValidationError as e:
				woocommerce_product_dict = self.woocommerce_product
			error_message = f"{frappe.get_traceback()}\n\nItem Data: \n{str(self.item) if self.item else ''}\n\nWC Product Data \n{str(woocommerce_product_dict) if self.woocommerce_product else ''})"
			frappe.log_error("WooCommerce Error", error_message)
			raise err
	def get_wc_api(self,wc_server) -> APIWithRequestLogging:
		"""Get WooCommerce API client with logging"""
		wc_server_doc = frappe.get_doc("WooCommerce Server", wc_server)
		return APIWithRequestLogging(
            url=wc_server_doc.woocommerce_server_url,
            consumer_key=wc_server_doc.api_consumer_key,
            consumer_secret=wc_server_doc.api_consumer_secret,
            version="wc/v3",
            timeout=300,
            verify_ssl=get_verify_tls(),
        )
	def get_corresponding_item_or_product(self):
		"""
		If we have an ERPNext Item, get the corresponding WooCommerce Product
		If we have a WooCommerce Product, get the corresponding ERPNext Item
		"""
	
		if (
			self.item and not self.woocommerce_product and self.item.item_woocommerce_server.woocommerce_id
		):
			# Validate that this Item's WooCommerce Server has sync enabled
			wc_server = frappe.get_cached_doc(
				"WooCommerce Server", self.item.item_woocommerce_server.woocommerce_server
			)
			if not wc_server.enable_sync:
				raise SyncDisabledError(wc_server)

			wc_products = get_list_of_wc_products(item=self.item)
			if len(wc_products) == 0:
				raise ValueError(
					f"No WooCommerce Product found with ID {self.item.item_woocommerce_server.woocommerce_id} on {self.item.item_woocommerce_server.woocommerce_server}"
				)
			self.woocommerce_product = wc_products[0]

		if self.woocommerce_product and not self.item:
			self.get_erpnext_item()

	def get_erpnext_item(self):
		"""
		Get erpnext item for a WooCommerce Product
		"""
		if not all(
			[self.woocommerce_product.woocommerce_server, self.woocommerce_product.woocommerce_id]
		):
			raise ValueError("Both woocommerce_server and woocommerce_id required")

		iws = frappe.qb.DocType("Item WooCommerce Server")
		itm = frappe.qb.DocType("Item")

		and_conditions = [
			iws.woocommerce_server == self.woocommerce_product.woocommerce_server,
			iws.woocommerce_id == self.woocommerce_product.woocommerce_id,
		]

		item_codes = (
			frappe.qb.from_(iws)
			.join(itm)
			.on(iws.parent == itm.name)
			.where(Criterion.all(and_conditions))
			.select(iws.parent, iws.name)
			.limit(1)
		).run(as_dict=True)

		found_item = frappe.get_doc("Item", item_codes[0].parent) if item_codes else None
		if found_item:
			self.item = ERPNextItemToSync(
				item=found_item,
				item_woocommerce_server_idx=next(
					server.idx for server in found_item.woocommerce_servers if server.name == item_codes[0].name
				),
			)

	def sync_wc_product_with_erpnext_item(self):
		"""
		Syncronise Item between ERPNext and WooCommerce
		"""
		if not(self.item.item.variant_of) and frappe.utils.cint(self.item.item.custom_generate_classification) == 1:
			update=frappe.utils.cint(self.item.item.custom_force_regenerate_item_group)
			classify_item_group(item_name=self.item.item.item_code,
								update=update,            # 0 or 1
								threshold=0.85,      # stricter than default
								language="fr",
								skip_root_if="All Item Groups")
			self.item.item.reload()
		if not(self.item.item.variant_of) and frappe.utils.cint(self.item.item.custom_generate_tag) == 1:

			
			if not self.woocommerce_product:
				rows = frappe.get_all(
					"Item WooCommerce Server",
					filters={"parenttype": "Item", "parent": self.item.item.name},
					fields=["woocommerce_server"],
					order_by="idx asc",
					limit=1,
				)
				woocommerce_server = rows[0].woocommerce_server
			else:
				woocommerce_server = self.woocommerce_product.woocommerce_server
			classify_item_collections(
				 item_name=self.item.item.item_code,
				 wc_server=woocommerce_server
			)
			self.item.item.reload()

		item_group_sync=frappe.db.get_value("Item Group", {"name": self.item.item.item_group}, "custom_enable_sync")
		if not frappe.utils.cint(item_group_sync):
			self.item.item.custom_sync_avec_woocommerce=0
			self.item.item.save()
		if self.item and self.item.item.custom_sync_avec_woocommerce:
			
			if frappe.utils.cint(self.item.item.custom_generate_seo) == 1:
				generate_website_contenant(item_name=self.item.item.item_code)
				self.item.item.reload()

			if self.item and not self.woocommerce_product:
				self.create_woocommerce_product(self.item)
			elif self.item and self.woocommerce_product:
				# # both exist, check sync hash
				# if (
				# 	self.woocommerce_product.woocommerce_date_modified
				# 	!= self.item.item_woocommerce_server.woocommerce_last_sync_hash
				# ):
					# if get_datetime(self.woocommerce_product.woocommerce_date_modified) > get_datetime(self.item.item.modified):
					# 	self.update_item(self.woocommerce_product, self.item)

				# if get_datetime(self.item.item.modified) > get_datetime(self.woocommerce_product.woocommerce_date_modified):

				self.update_woocommerce_product(self.woocommerce_product, self.item)

			# Sync item discount
			sync_price=sync_single_item_discount(item_code=self.item.item.item_code)
			# Sync item Brand and Unit of Measure
			
			payload = {
				"brands": [],
				"attributes": _safe_json_list(getattr(self.woocommerce_product, "attributes", None)),
				"categories": [],
				"tags": [],
				"meta_data": [],
			}

			if self.item.item.brand:
				sync_brand(brand_name=self.item.item.brand, woocommerce_server=self.woocommerce_product.woocommerce_server)
				
				brand_id= frappe.db.get_value("Brand", {"brand": self.item.item.brand, "custom_woocomerce_server": self.woocommerce_product.woocommerce_server}, "custom_woocomerce_id")
				payload["brands"].append({"id": brand_id})
			if self.item.item.stock_uom:
				sync_att_result=sync_attribute(attribute_name=self.item.item.stock_uom, woocommerce_server=self.woocommerce_product.woocommerce_server)
				attr_id=sync_att_result.get("value_id")
				attr_id=attr_id[0]
				payload["attributes"].append( {"id": attr_id,"visible": "true","variation": "false","options": [self.item.item.stock_uom]})
			
			main_group=sync_single_item_group(wc_server=self.woocommerce_product.woocommerce_server,item_group_name=self.item.item.item_group)
			
			payload["categories"].append({"id": main_group['wc_category_id']})
			primary_cat_id = main_group['wc_category_id']
			payload["meta_data"].append({
				"key": "rank_math_primary_product_cat",
				"value": primary_cat_id
			})
			list_categories=self.item.item.custom_woocomerce_categories
			if list_categories:
				categories = [c.strip() for c in list_categories.split(",")]
			else:
				categories = []
			for cat in categories:
				item_group_sync=frappe.db.get_value("Item Group", {"name": cat}, "custom_enable_sync")
				if frappe.utils.cint(item_group_sync)==1:
					cat_group=sync_single_item_group(wc_server=self.woocommerce_product.woocommerce_server,item_group_name=cat)
					payload["categories"].append({"id": cat_group['wc_category_id']})
			wc_server=self.get_wc_api(self.woocommerce_product.woocommerce_server)
			wc_id=self.woocommerce_product.woocommerce_id



			if self.item.item.custom_woocomerce_collection:
				tags=json.loads(self.item.item.custom_woocomerce_collection)
				for tag in tags:
					payload["tags"].append({"id": frappe.utils.cint(tag)})
			shipping_slug = _get_shipping_class_slug(self.item.item)

				
			payload["shipping_class"] = shipping_slug
			if sync_price['message']!="No Discounts":
				Promo_id=40
				if not(self.item.item.variant_of):
					payload["tags"].append({"id": Promo_id})
				else:

					woo_parent_id = frappe.db.get_value("Item WooCommerce Server", {"parent": frappe.db.get_value("Item", self.item.item.item_code, "variant_of") , "parenttype": "Item", "parentfield": "woocommerce_servers", "woocommerce_server": self.woocommerce_product.woocommerce_server, "enabled": 1}, "woocommerce_id")
					# Sans lien parent actif, products/None ferait une requête absurde.
					if woo_parent_id:
						payload_P={}
						payload_P["tags"] = (wc_server.get(f"products/{woo_parent_id}").json() or {}).get("tags", [])
						payload_P["tags"].append({"id": Promo_id})

						wc_server.put(f"products/{woo_parent_id}", payload_P).json()

			try:
				if self.item.item.variant_of:
					# Une variation WooCommerce n'a ni marque, ni catégories, ni tags
					# propres : ces taxonomies vivent sur le produit PARENT, que la
					# synchro du modèle (has_variants) pousse déjà — et un PUT
					# products/{id} sur une variation répond 404
					# woocommerce_rest_invalid_product_id (« utilisez le point de
					# terminaison produits/variations »). Le tag promo des variantes
					# est, lui, déjà propagé au parent par le bloc ci-dessus.
					pass
				else:
					print("Updating WC product with payload:", payload)
					if not payload["brands"]:
						payload["brands"] = [{}]
					response = wc_server.put(f"products/{wc_id}", payload).json()
					print(response.keys())
					print(response.get("brands"))
			except Exception as e:
				frappe.log_error("WooCommerce Product Update Brand attribute Error", frappe.get_traceback())
				return {
					"status": "error",
					"message": str(e)
				}
		elif self.item and not self.item.item.custom_sync_avec_woocommerce and self.woocommerce_product:																					
			
			wc_server=self.get_wc_api(self.woocommerce_product.woocommerce_server)
			wc_id=self.woocommerce_product.woocommerce_id
			try:
				if self.woocommerce_product.type=="variation":
					wc_parent_id = frappe.db.get_value(
							"Item WooCommerce Server",
							{"parent": self.item.item.variant_of, "parenttype": "Item", "woocommerce_server": self.item.item_woocommerce_server.woocommerce_server},
							"woocommerce_id",
						)
					wc_server.delete(f"products/{wc_parent_id}/variations/{wc_id}?force=true")
				else:
					wc_server.delete(f"products/{wc_id}?force=true")
				item=frappe.get_doc("Item", self.item.item.item_code)
				for iws in item.woocommerce_servers:
					if int(iws.woocommerce_id)==wc_id and iws.woocommerce_server==self.woocommerce_product.woocommerce_server:
						iws.woocommerce_id=None
						item.flags.ignore_mandatory = True
						item.save()
						break

			except Exception as e:
				frappe.log_error("WooCommerce Product Deletion Error", frappe.get_traceback())
				return {
					"status": "error",
					"message": str(e)
				}

	def update_item(self, woocommerce_product: WooCommerceProduct, item: ERPNextItemToSync):
		"""
		Update the ERPNext Item with fields from it's corresponding WooCommerce Product
		"""
		item_dirty = False
		if item.item.item_name != woocommerce_product.woocommerce_name:
			item.item.item_name = woocommerce_product.woocommerce_name
			item_dirty = True

		fields_updated, item.item = self.set_item_fields(item=item.item)

		wc_server = frappe.get_cached_doc("WooCommerce Server", woocommerce_product.woocommerce_server)
		if wc_server.enable_image_sync:
			wc_product_images = json.loads(woocommerce_product.images)
			if len(wc_product_images) > 0:
				if item.item.image != wc_product_images[0]["src"]:
					item.item.image = wc_product_images[0]["src"]
					item_dirty = True

		if item_dirty or fields_updated:
			item.item.flags.created_by_sync = True
			item.item.save()

		self.set_sync_hash()

	def update_woocommerce_product(
		self, wc_product: WooCommerceProduct, item: ERPNextItemToSync
	) -> None:
		"""
		Update the WooCommerce Product with fields from it's corresponding ERPNext Item
		"""
		wc_product_dirty = False
		
		# Update properties
		if wc_product.woocommerce_name != item.item.item_name:
			wc_product.woocommerce_name = item.item.item_name
			wc_product_dirty = True
		
		product_fields_changed, wc_product = self.set_product_fields(wc_product, item)
		if product_fields_changed:
			wc_product_dirty = True
		
		if wc_product_dirty:
			if wc_product.type=="variable" or wc_product.type=="variation":
				wc_product.flags.ignore_mandatory = True
			wc_product.save()

		self.woocommerce_product = wc_product
		
		wc_server=self.get_wc_api(self.woocommerce_product.woocommerce_server)
		_upload_item_images_to_wp_and_attach(wc_server,item.item, wc_product)
		self.set_sync_hash()

	def create_woocommerce_product(self, item: ERPNextItemToSync) -> None:
		"""
		Create the WooCommerce Product with fields from it's corresponding ERPNext Item
		"""
		if (
			item.item_woocommerce_server.woocommerce_server
			and item.item_woocommerce_server.enabled
			and not item.item_woocommerce_server.woocommerce_id
		):
			
			# Create a new WooCommerce Product doc
			wc_product = frappe.get_doc({"doctype": "WooCommerce Product"})

			wc_product.type = "simple"
			wc_product.status = "publish"
			# Handle variants
			
			if item.item.has_variants:
				wc_product.type = "variable"
				wc_product_attributes = []

				# Handle attributes
				for row in item.item.attributes:
					item_attribute = frappe.get_doc("Item Attribute", row.attribute)
					wc_product_attributes.append(
						{
							"name": row.attribute,
							"slug": row.attribute.lower().replace(" ", "_"),
							"visible": True,
							"variation": True,
							"options": [option.attribute_value for option in item_attribute.item_attribute_values],
						}
					)
				
				wc_product.attributes = json.dumps(wc_product_attributes)

			if item.item.variant_of:
				# Check if parent exists
				parent_item = frappe.get_doc("Item", item.item.variant_of)
				parent_item, parent_wc_product = run_item_sync(item_code=parent_item.item_code)
				wc_product.parent_id = parent_wc_product.woocommerce_id
				wc_product.type = "variation"

				# Handle attributes
				wc_product_attributes = [
					{
						"name": row.attribute,
						"slug": row.attribute.lower().replace(" ", "_"),
						"option": row.attribute_value,
					}
					for row in item.item.attributes
				]
				
				wc_product.attributes = json.dumps(wc_product_attributes)
			# Set properties
			wc_product.woocommerce_server = item.item_woocommerce_server.woocommerce_server
			wc_product.woocommerce_name = item.item.item_name
			wc_product.regular_price = get_item_price_rate(item) or "0"

			self.set_product_fields(wc_product, item)

			wc_product.insert()
			self.woocommerce_product = wc_product
			print("Uploading Item images to WP...")
			
			# NEW: upload Item images to WP & attach to this new Woo product
			
			wc_server=self.get_wc_api(self.woocommerce_product.woocommerce_server)
			_upload_item_images_to_wp_and_attach(wc_server,item.item, wc_product)


			# Reload ERPNext Item
			item.item.reload()
			item.item_woocommerce_server.woocommerce_id = wc_product.woocommerce_id
			item.item.flags.created_by_sync = True
			item.item.save()

			self.set_sync_hash()

	def create_item(self, wc_product: WooCommerceProduct) -> None:
		"""
		Create an ERPNext Item from the given WooCommerce Product
		"""
		wc_server = frappe.get_cached_doc("WooCommerce Server", wc_product.woocommerce_server)

		# Create Item
		item = frappe.new_doc("Item")

		# Handle variants' attributes
		if wc_product.type in ["variable", "variation"]:
			self.create_or_update_item_attributes(wc_product)
			wc_attributes = json.loads(wc_product.attributes)
			for wc_attribute in wc_attributes:
				row = item.append("attributes")
				row.attribute = wc_attribute["name"]
				if wc_product.type == "variation":
					row.attribute_value = wc_attribute["option"]

		# Handle variants
		if wc_product.type == "variable":
			item.has_variants = 1

		if wc_product.type == "variation":
			# Check if parent exists
			woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
				wc_product.woocommerce_server, wc_product.parent_id
			)
			parent_item, parent_wc_product = run_item_sync(
				woocommerce_product_name=woocommerce_product_name
			)
			item.variant_of = parent_item.item_code

		item.item_code = (
			wc_product.sku
			if wc_server.name_by == "Product SKU" and wc_product.sku
			else str(wc_product.woocommerce_id)
		)
		item.stock_uom = wc_server.uom or _("Nos")
		item.item_group = wc_server.item_group
		item.item_name = wc_product.woocommerce_name
		row = item.append("woocommerce_servers")
		row.woocommerce_id = wc_product.woocommerce_id
		row.woocommerce_server = wc_server.name
		item.flags.ignore_mandatory = True
		item.flags.created_by_sync = True

		if wc_server.enable_image_sync:
			wc_product_images = json.loads(wc_product.images)
			if len(wc_product_images) > 0:
				item.image = wc_product_images[0]["src"]

		modified, item = self.set_item_fields(item=item)
		item.flags.created_by_sync = True

		item.insert()
		
		self.item = ERPNextItemToSync(
			item=item,
			item_woocommerce_server_idx=next(
				iws.idx
				for iws in item.woocommerce_servers
				if iws.woocommerce_server == wc_product.woocommerce_server
			),
		)

		self.set_sync_hash()

	def create_or_update_item_attributes(self, wc_product: WooCommerceProduct):
		"""
		Create or update an Item Attribute
		"""
		if wc_product.attributes:
			wc_attributes = json.loads(wc_product.attributes)
			for wc_attribute in wc_attributes:
				if frappe.db.exists("Item Attribute", wc_attribute["name"]):
					# Get existing Item Attribute
					item_attribute = frappe.get_doc("Item Attribute", wc_attribute["name"])
				else:
					# Create a Item Attribute
					item_attribute = frappe.get_doc(
						{"doctype": "Item Attribute", "attribute_name": wc_attribute["name"]}
					)

				# Get list of attribute options.
				# In variable WooCommerce Products, it's a list with key "options"
				# In a WooCommerce Product variant, it's a single value with key "option"
				options = (
					wc_attribute["options"] if wc_product.type == "variable" else [wc_attribute["option"]]
				)

				# If no attributes values exist, or attribute values exist already but are different, remove and update them
				if len(item_attribute.item_attribute_values) == 0 or (
					len(item_attribute.item_attribute_values) > 0
					and set(options) != set([val.attribute_value for val in item_attribute.item_attribute_values])
				):
					item_attribute.item_attribute_values = []
					for option in options:
						row = item_attribute.append("item_attribute_values")
						row.attribute_value = option
						row.abbr = option.replace(" ", "")

				item_attribute.flags.ignore_mandatory = True
				if not item_attribute.name:
					item_attribute.insert()
				else:
					item_attribute.save()

	def set_item_fields(self, item: Item) -> Tuple[bool, Item]:
		"""
		If there exist any Field Mappings on `WooCommerce Server`, attempt to synchronise their values from
		WooCommerce to ERPNext
		"""
		item_dirty = False
		if item and self.woocommerce_product:
			wc_server = frappe.get_cached_doc(
				"WooCommerce Server", self.woocommerce_product.woocommerce_server
			)
			if wc_server.item_field_map:
				woocommerce_product_dict = (
					self.woocommerce_product.deserialize_attributes_of_type_dict_or_list(
						self.woocommerce_product.to_dict()
					)
				)
				for map in wc_server.item_field_map:
					erpnext_item_field_name = map.erpnext_field_name.split(" | ")

					# We expect woocommerce_field_name to be valid JSONPath
					jsonpath_expr = parse(map.woocommerce_field_name)
					woocommerce_product_field_matches = jsonpath_expr.find(woocommerce_product_dict)

					setattr(item, erpnext_item_field_name[0], woocommerce_product_field_matches[0].value)
					item_dirty = True
		return item_dirty, item



	# --- main ------------------------------------------------------------------

	def set_product_fields(
		self, woocommerce_product: WooCommerceProduct, item: ERPNextItemToSync
	) -> Tuple[bool, WooCommerceProduct]:
		"""
		Apply 'WooCommerce Server' field mappings to a WooCommerce product.
		Creates meta_data rows on demand for JSONPath filters like:
		meta_data[?(@.key=="rank_math_title")].value
		"""
		wc_product_dirty = False
		if item and woocommerce_product:
			wc_server = frappe.get_cached_doc("WooCommerce Server", woocommerce_product.woocommerce_server)
			if wc_server.item_field_map:
				# Work on a deserialised copy so jsonpath-ng can touch dict/list fields
				wc_product_with_deserialised_fields = (
					woocommerce_product.deserialize_attributes_of_type_dict_or_list(woocommerce_product)
				)

				for m in wc_server.item_field_map:
					# "Field | Label" → take the fieldname part
					erp_fieldname = m.erpnext_field_name.split(" | ")[0]
					erp_value = getattr(item.item, erp_fieldname)

					jsonpath_expr = parse(m.woocommerce_field_name)
					matches = jsonpath_expr.find(wc_product_with_deserialised_fields)

					# If JSONPath didn't match anything, try smart upsert
					if len(matches) == 0:
						meta_key = _extract_meta_key_from_jsonpath(m.woocommerce_field_name)

						if meta_key:
							# Create/update meta_data row (works for existing & new products)
							if _upsert_meta(wc_product_with_deserialised_fields, meta_key, erp_value):
								wc_product_dirty = True
							continue

						# Simple top-level field (e.g., "slug", "description", "short_description")
						# If it's a simple identifier without filters/indices, set it.
						if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", m.woocommerce_field_name.strip("$")):
							field = m.woocommerce_field_name.strip("$")
							if getattr(wc_product_with_deserialised_fields, field, None) != erp_value:
								setattr(wc_product_with_deserialised_fields, field, erp_value)
								wc_product_dirty = True
							continue

						# Otherwise, we don't know how to construct the nested path safely
						# (keep old strict behaviour)
						if woocommerce_product.name:
							raise ValueError(
								_("Field <code>{0}</code> not found in WooCommerce Product {1}").format(
									m.woocommerce_field_name, woocommerce_product.name
								)
							)
						else:
							# New product & unknown nested path → ignore silently
							continue

					# JSONPath matched → update if different
					current = matches[0].value
					if erp_value != current:
						jsonpath_expr.update(wc_product_with_deserialised_fields, erp_value)
						wc_product_dirty = True

				if wc_product_dirty:
					woocommerce_product = woocommerce_product.serialize_attributes_of_type_dict_or_list(
						wc_product_with_deserialised_fields
					)

		return wc_product_dirty, woocommerce_product

	def set_sync_hash(self):
		"""
		Set the last sync hash value using db.set_value, as it does not call the ORM triggers
		and it does not update the modified timestamp (by using the update_modified parameter)
		"""
		frappe.db.set_value(
			"Item WooCommerce Server",
			self.item.item_woocommerce_server.name,
			"woocommerce_last_sync_hash",
			self.woocommerce_product.woocommerce_date_modified,
			update_modified=False,
		)

		# If item was synchronised but the item is set not to sync, turn on the enabled flag
		# Items that are disabled for sync will still be synced if it is ordered on WooCommerce
		frappe.db.set_value(
			"Item WooCommerce Server",
			self.item.item_woocommerce_server.name,
			"enabled",
			1,
			update_modified=False,
		)


def get_list_of_wc_products(
	item: Optional[ERPNextItemToSync] = None, date_time_from: Optional[datetime] = None
) -> List[WooCommerceProduct]:
	"""
	Fetches a list of WooCommerce Products within a specified date range or linked with an Item, using pagination.

	At least one of date_time_from, item parameters are required
	"""
	if not any([date_time_from, item]):
		raise ValueError("At least one of date_time_from or item parameters are required")
	
	wc_records_per_page_limit = 100
	page_length = wc_records_per_page_limit
	new_results = True
	start = 0
	filters = []
	wc_products = []
	servers = None


	# Build filters
	if date_time_from:
		filters.append(["WooCommerce Product", "date_modified", ">", date_time_from])
	if item:
		if item.item.variant_of:

			wc_parent_id = frappe.db.get_value(
				"Item WooCommerce Server",
				{"parent": item.item.variant_of, "parenttype": "Item", "woocommerce_server": item.item_woocommerce_server.woocommerce_server},
				"woocommerce_id",
			)
			filters.append(["WooCommerce Product", "variant_id", "=", [wc_parent_id, item.item_woocommerce_server.woocommerce_id]])
		else:
			filters.append(["WooCommerce Product", "id", "=", item.item_woocommerce_server.woocommerce_id])
		servers = [item.item_woocommerce_server.woocommerce_server]
	

	while new_results:
		woocommerce_product = frappe.get_doc({"doctype": "WooCommerce Product"})
		new_results = woocommerce_product.get_list(
			args={
				"filters": filters,
				"page_lenth": page_length,
				"start": start,
				"servers": servers,
				"as_doc": True,
			}
		)
		for wc_product in new_results:
			wc_products.append(wc_product)
		start += page_length
		if len(new_results) < page_length:
			new_results = []

	return wc_products


def get_item_price_rate(item: ERPNextItemToSync):
	"""
	Get the Item Price if Item Price sync is enabled
	"""
	# Check if the Item Price sync is enabled
	wc_server = frappe.get_cached_doc(
		"WooCommerce Server", item.item_woocommerce_server.woocommerce_server
	)
	if wc_server.enable_price_list_sync:
		item_prices = frappe.get_all(
			"Item Price",
			filters={"item_code": item.item.item_code, "price_list": wc_server.price_list},
			fields=["price_list_rate", "valid_upto"],
		)
		return next(
			(
				price.price_list_rate
				for price in item_prices
				if not price.valid_upto or price.valid_upto > now()
			),
			None,
		)


def clear_sync_hash_and_run_item_sync(item_code: str):
	"""
	Clear the last sync hash value using db.set_value, as it does not call the ORM triggers
	and it does not update the modified timestamp (by using the update_modified parameter)
	"""

	iws = frappe.qb.DocType("Item WooCommerce Server")

	iwss = (
		frappe.qb.from_(iws).where(iws.enabled == 1).where(iws.parent == item_code).select(iws.name)
	).run(as_dict=True)

	for iws in iwss:
		frappe.db.set_value(
			"Item WooCommerce Server",
			iws.name,
			"woocommerce_last_sync_hash",
			None,
			update_modified=False,
		)

	if len(iwss) > 0:
		run_item_sync(item_code=item_code, enqueue=True)
