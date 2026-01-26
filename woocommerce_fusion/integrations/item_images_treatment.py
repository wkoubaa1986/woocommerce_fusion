# # -*- coding: utf-8 -*-
# """
# Item image normalization for ERPNext/Frappe.

# What this module guarantees (when dry_run=0):
#   - Exactly ONE main image for the Item (Item.image + File.attached_to_field="image")
#   - Duplicate File docs are removed safely:
#       * exact duplicates (file_url + content_hash)
#       * duplicates by same file_url
#       * perceptual duplicates (dHash + Hamming threshold)
#   - Broken links cleanup:
#       * If a /files/ or /private/files/ URL is CONFIRMED missing -> clear Item/child refs + delete all File docs using it
#   - BEFORE deleting any File doc, rewire Item-scoped references old_url -> keeper_url (when both exist and differ)
#   - Avoids double purges / double deletes in a single run
#   - Produces a detailed report (safe to log)

# Notes:
#   - Perceptual dedupe requires Pillow installed on the worker (PIL).
#   - If your deployment uses external storage (S3) without shared local disk,
#     you may want to adjust _file_status() to treat disk-miss as "unknown".
# """

# import io
# import os
# import requests
# import frappe
# from frappe.utils import get_url
# import pdb

# # -------------------------------------------------------------------
# # Safe traceback for restricted console
# # -------------------------------------------------------------------
# def _safe_tb(e=None):
#     try:
#         return frappe.get_traceback()
#     except Exception:
#         return str(e) if e else ""


# # -------------------------------------------------------------------
# # URL normalization helpers
# # -------------------------------------------------------------------
# def _norm_url(url: str) -> str:
#     """Normalize a file_url: strip, drop query/fragment, reduce absolute URL to its path."""
#     if not url:
#         return ""
#     url = (url or "").strip()
#     if not url:
#         return ""
#     # strip query/fragment
#     url = url.split("?", 1)[0].split("#", 1)[0]

#     # If absolute, reduce to path
#     if url.startswith("http://") or url.startswith("https://"):
#         try:
#             from urllib.parse import urlparse

#             p = urlparse(url)
#             url = p.path or url
#         except Exception:
#             pass
#     return url


# def _is_local_file_url(file_url: str) -> bool:
#     u = _norm_url(file_url)
#     return u.startswith("/files/") or u.startswith("/private/files/")


# # -------------------------------------------------------------------
# # File URL -> filesystem path
# # -------------------------------------------------------------------
# def _file_url_to_path(file_url: str):
#     """
#     Map Frappe File.file_url to disk path.
#       /files/X         -> sites/<site>/public/files/X
#       /private/files/X -> sites/<site>/private/files/X
#     """
#     u = _norm_url(file_url)
#     if not u:
#         return None

#     if u.startswith("/files/"):
#         rel = u.split("/files/", 1)[1]
#         return frappe.get_site_path("public", "files", rel)

#     if u.startswith("/private/files/"):
#         rel = u.split("/private/files/", 1)[1]
#         return frappe.get_site_path("private", "files", rel)

#     return None


# # -------------------------------------------------------------------
# # HTTP helpers (NO hardcoded localhost)
# # -------------------------------------------------------------------
# def _candidate_http_urls(file_url: str):
#     """
#     Candidates absolute URLs for a file_url:
#     - canonical: get_url(file_url)
#     - optional override: frappe.conf.files_base_url in site_config.json
#     """
#     u = _norm_url(file_url)
#     if not u:
#         return []

#     urls = []
#     try:
#         urls.append(get_url(u))
#     except Exception:
#         pass

#     base = frappe.conf.get("files_base_url")
#     if base and u.startswith("/"):
#         uu = base.rstrip("/") + u
#         if uu not in urls:
#             urls.append(uu)

#     # dedupe keep order
#     seen = set()
#     out = []
#     for x in urls:
#         if x and x not in seen:
#             seen.add(x)
#             out.append(x)
#     return out


# def _http_head_exists(file_url: str):
#     """
#     True  -> reachable (2xx/3xx)
#     False -> confirmed missing (404) OR static-serving error that usually means missing (500/502/503/504)
#     None  -> unknown (timeouts, dns, 401/403...)
#     """
#     for u in _candidate_http_urls(file_url):
#         try:
#             r = requests.head(u, timeout=3, allow_redirects=True)

#             if r.status_code == 404:
#                 return False

#             # Some setups throw missing static as 500 in logs
#             if r.status_code in (500, 502, 503, 504):
#                 return False

#             if 200 <= r.status_code < 400:
#                 return True

#             return None
#         except Exception:
#             continue
#     return None


# def _file_status(file_url: str):
#     """
#     True  -> exists
#     False -> confirmed missing
#     None  -> unknown (do NOT delete as missing)
#     """
#     u = _norm_url(file_url)
#     if not u:
#         return None

#     if u.startswith(("/files/", "/private/files/")):
#         # Disk check authoritative if mappable
#         p = _file_url_to_path(u)
#         if p:
#             return True if os.path.exists(p) else False

#         # Fallback when path can't be mapped (rare)
#         return _http_head_exists(u)

#     # Non-local URLs are treated as existing (do not purge)
#     return True


# # -------------------------------------------------------------------
# # Bytes + image detection
# # -------------------------------------------------------------------
# def _get_file_bytes(file_doc) -> bytes:
#     """
#     Read bytes safely:
#       1) local disk (public/private correct paths)
#       2) file_doc.get_content()
#       3) HTTP GET fallback for public /files/
#     """
#     file_url = _norm_url(getattr(file_doc, "file_url", "") or "")

#     # 1) disk
#     p = _file_url_to_path(file_url)
#     if p:
#         try:
#             with open(p, "rb") as fp:
#                 return fp.read()
#         except Exception:
#             pass

#     # 2) db/binary
#     try:
#         return file_doc.get_content()
#     except Exception:
#         pass

#     # 3) public http (only /files/)
#     if file_url.startswith("/files/"):
#         for u in _candidate_http_urls(file_url):
#             try:
#                 r = requests.get(u, timeout=10)
#                 if r.status_code == 200 and r.content:
#                     return r.content
#             except Exception:
#                 continue

#     raise FileNotFoundError(f"Cannot read bytes for {file_url}")


# def _is_image_doc(file_doc) -> bool:
#     """
#     Detect real image by parsing bytes (not relying on extension).
#     If Pillow is missing, raise so caller can record the error.
#     """
#     try:
#         from PIL import Image
#     except Exception as e:
#         raise RuntimeError("Pillow (PIL) is not installed; perceptual dedupe cannot run") from e

#     b = _get_file_bytes(file_doc)
#     Image.open(io.BytesIO(b)).verify()
#     return True


# # -------------------------------------------------------------------
# # dHash helpers
# # -------------------------------------------------------------------
# def _dhash_int(img_bytes: bytes, hash_size: int = 8) -> int:
#     from PIL import Image

#     img = Image.open(io.BytesIO(img_bytes)).convert("L")
#     img = img.resize((hash_size + 1, hash_size))
#     px = list(img.getdata())

#     bits = 0
#     for y in range(hash_size):
#         row = y * (hash_size + 1)
#         for x in range(hash_size):
#             bits = (bits << 1) | (1 if px[row + x] > px[row + x + 1] else 0)
#     return bits


# def _hamming(a: int, b: int) -> int:
#     return (a ^ b).bit_count()


# # -------------------------------------------------------------------
# # Reference rewiring + broken-link cleanup
# # -------------------------------------------------------------------
# def _count_items_using_url(file_url: str, exclude_item_code: str = None) -> int:
#     """
#     Count how many distinct items use this file_url.
#     Useful to detect shared images before purging.
#     """
#     file_url = _norm_url(file_url)
#     if not file_url:
#         return 0

#     # Count via File docs
#     filters = {"file_url": file_url, "attached_to_doctype": "Item", "is_folder": 0}
#     if exclude_item_code:
#         filters["attached_to_name"] = ["!=", exclude_item_code]

#     items_via_files = set(frappe.get_all("File", filters=filters, pluck="attached_to_name", limit=10000))

#     # Count via Item fields
#     for fn in _item_image_fields():
#         filters = {fn: file_url}
#         if exclude_item_code:
#             filters["name"] = ["!=", exclude_item_code]
#         items_via_fields = frappe.get_all("Item", filters=filters, pluck="name", limit=10000)
#         items_via_files.update(items_via_fields)

#     return len(items_via_files)


# def _doctype_exists(dt: str) -> bool:
#     try:
#         return bool(frappe.db.exists("DocType", dt))
#     except Exception:
#         return False


# def _item_image_fields():
#     meta = frappe.get_meta("Item")
#     candidates = ["image", "website_image", "thumbnail", "image_view"]
#     return [f for f in candidates if meta.has_field(f)]


# def _rewrite_references_item_scope(
#     old_url: str,
#     new_url: str,
#     *,
#     exclude_file_docname: str = None,
#     dry_run: bool = True,
#     report: dict = None,
#     context: dict = None,
# ):
#     """
#     Rewire Item-related references from old_url -> new_url:
#       1) Item fields (image-like)
#       2) common child doctypes (if exist)
#       3) File docs attached to Items that still point to old_url (excluding the one to delete)
#     """
#     old_url = _norm_url(old_url)
#     new_url = _norm_url(new_url)
#     if not old_url or not new_url or old_url == new_url:
#         return

#     ctx = context or {}
#     entry = {
#         "from_url": old_url,
#         "to_url": new_url,
#         "exclude_file_docname": exclude_file_docname,
#         "context": ctx,
#         "items_updated": {},
#         "child_rows_updated": {},
#         "item_file_docs_updated": [],
#         "applied": (not dry_run),
#     }

#     # 1) Item direct fields
#     for fn in _item_image_fields():
#         names = frappe.get_all("Item", filters={fn: old_url}, pluck="name", limit=5000)
#         if names:
#             entry["items_updated"][fn] = names[:50]
#             if not dry_run:
#                 frappe.db.sql(f"UPDATE `tabItem` SET `{fn}`=%s WHERE `{fn}`=%s", (new_url, old_url))

#     # 2) Child doctypes (depends on version/customizations)
#     child_candidates = ["Item Image", "Website Item Image", "Item Website Image"]
#     for dt in child_candidates:
#         if not _doctype_exists(dt):
#             continue
#         meta = frappe.get_meta(dt)
#         fields = [f for f in ("image", "file_url", "website_image", "url") if meta.has_field(f)]
#         for fn in fields:
#             rows = frappe.get_all(dt, filters={fn: old_url}, fields=["name", "parent"], limit=5000)
#             if rows:
#                 key = f"{dt}.{fn}"
#                 entry["child_rows_updated"][key] = rows[:50]
#                 if not dry_run:
#                     frappe.db.sql(f"UPDATE `tab{dt}` SET `{fn}`=%s WHERE `{fn}`=%s", (new_url, old_url))

#     # 3) File docs attached to Items still pointing to old_url
#     file_filters = {"attached_to_doctype": "Item", "file_url": old_url, "is_folder": 0}
#     if exclude_file_docname:
#         file_filters["name"] = ["!=", exclude_file_docname]

#     file_rows = frappe.get_all(
#         "File",
#         filters=file_filters,
#         fields=["name", "attached_to_name", "attached_to_field", "file_url"],
#         limit=5000,
#     )
#     if file_rows:
#         entry["item_file_docs_updated"] = file_rows[:50]
#         if not dry_run:
#             for r in file_rows:
#                 frappe.db.set_value("File", r["name"], "file_url", new_url, update_modified=False)

#     if report is not None:
#         report.setdefault("rewired_references", []).append(entry)


# def _clear_references_for_missing_url(file_url: str, *, dry_run: bool, report: dict):
#     """
#     Missing url cleanup:
#       - clears Item fields equal to file_url
#       - clears common child doctypes fields equal to file_url
#     """
#     file_url = _norm_url(file_url)
#     if not file_url:
#         return

#     entry = {
#         "missing_url": file_url,
#         "items_cleared": {},
#         "child_rows_cleared": {},
#         "applied": (not dry_run),
#     }

#     # Item fields
#     for fn in _item_image_fields():
#         names = frappe.get_all("Item", filters={fn: file_url}, pluck="name", limit=5000)
#         if names:
#             entry["items_cleared"][fn] = names[:50]
#             if not dry_run:
#                 frappe.db.sql(f"UPDATE `tabItem` SET `{fn}`=NULL WHERE `{fn}`=%s", (file_url,))

#     # Child doctypes
#     child_candidates = ["Item Image", "Website Item Image", "Item Website Image"]
#     for dt in child_candidates:
#         if not _doctype_exists(dt):
#             continue
#         meta = frappe.get_meta(dt)
#         fields = [f for f in ("image", "file_url", "website_image", "url") if meta.has_field(f)]
#         for fn in fields:
#             rows = frappe.get_all(dt, filters={fn: file_url}, fields=["name", "parent"], limit=5000)
#             if rows:
#                 key = f"{dt}.{fn}"
#                 entry["child_rows_cleared"][key] = rows[:50]
#                 if not dry_run:
#                     frappe.db.sql(f"UPDATE `tab{dt}` SET `{fn}`=NULL WHERE `{fn}`=%s", (file_url,))

#     report.setdefault("missing_links_cleared", []).append(entry)


# def _purge_missing_url_for_item(file_url: str, item_code: str, *, dry_run: bool, report: dict):
#     """
#     If a url is confirmed missing (ITEM-SCOPED):
#       1) clear references ONLY for this specific item
#       2) delete ONLY File docs attached to this item with that file_url
#     """
#     file_url = _norm_url(file_url)
#     if not file_url or not item_code:
#         return

#     entry = {
#         "missing_url": file_url,
#         "item_code": item_code,
#         "items_cleared": {},
#         "child_rows_cleared": {},
#         "applied": (not dry_run),
#     }

#     # Clear Item fields ONLY for this item
#     for fn in _item_image_fields():
#         item = frappe.get_doc("Item", item_code)
#         if _norm_url(getattr(item, fn, None)) == file_url:
#             entry["items_cleared"][fn] = [item_code]
#             if not dry_run:
#                 setattr(item, fn, None)
#                 item.save(ignore_permissions=True)

#     # Clear child doctypes ONLY for this item
#     child_candidates = ["Item Image", "Website Item Image", "Item Website Image"]
#     for dt in child_candidates:
#         if not _doctype_exists(dt):
#             continue
#         meta = frappe.get_meta(dt)
#         fields = [f for f in ("image", "file_url", "website_image", "url") if meta.has_field(f)]
#         for fn in fields:
#             rows = frappe.get_all(dt, filters={fn: file_url, "parent": item_code}, fields=["name", "parent"], limit=5000)
#             if rows:
#                 key = f"{dt}.{fn}"
#                 entry["child_rows_cleared"][key] = rows[:50]
#                 if not dry_run:
#                     frappe.db.sql(f"UPDATE `tab{dt}` SET `{fn}`=NULL WHERE `{fn}`=%s AND parent=%s", (file_url, item_code))

#     report.setdefault("missing_links_cleared", []).append(entry)

#     # Delete File docs ONLY attached to this item
#     file_docs = frappe.get_all(
#         "File",
#         filters={"file_url": file_url, "attached_to_doctype": "Item", "attached_to_name": item_code, "is_folder": 0},
#         pluck="name",
#         limit=5000,
#     )
#     if file_docs:
#         report.setdefault("missing_file_docs_deleted", []).append(
#             {"file_url": file_url, "item_code": item_code, "count": len(file_docs), "sample": file_docs[:50], "applied": (not dry_run)}
#         )
#         if not dry_run:
#             for nm in file_docs:
#                 frappe.delete_doc("File", nm, ignore_permissions=True, force=1)


# def _purge_missing_url_everywhere(file_url: str, *, dry_run: bool, report: dict, item_code: str = None):
#     """
#     If a url is confirmed missing (GLOBAL or SCOPED):
#       1) clear references (Item + child tables)
#       2) delete ALL File docs having that file_url
    
#     If item_code is provided, only purges for that specific item (SAFE).
#     If item_code is None, purges globally (DANGEROUS - affects all items).
#     """
#     file_url = _norm_url(file_url)
#     if not file_url:
#         return

#     # SCOPED MODE (safer)
#     if item_code:
#         _purge_missing_url_for_item(file_url, item_code, dry_run=dry_run, report=report)
#         return

#     # GLOBAL MODE (original behavior)
#     _clear_references_for_missing_url(file_url, dry_run=dry_run, report=report)

#     file_docs = frappe.get_all("File", filters={"file_url": file_url, "is_folder": 0}, pluck="name", limit=5000)
#     if file_docs:
#         report.setdefault("missing_file_docs_deleted", []).append(
#             {"file_url": file_url, "count": len(file_docs), "sample": file_docs[:50], "applied": (not dry_run)}
#         )
#         if not dry_run:
#             for nm in file_docs:
#                 frappe.delete_doc("File", nm, ignore_permissions=True, force=1)


# # -------------------------------------------------------------------
# # Shared file detection
# # -------------------------------------------------------------------
# def _is_file_shared(file_url: str, file_name: str, item_code: str) -> dict:
#     """
#     Check if a file is used by OTHER items (excluding current item).
    
#     Returns: {
#         "is_shared": bool,
#         "other_items_count": int,
#         "other_items": [list of item codes],
#         "usage_details": {dict with field usage}
#     }
#     """
#     file_url = _norm_url(file_url)
#     if not file_url:
#         return {"is_shared": False, "other_items_count": 0, "other_items": [], "usage_details": {}}

#     other_items = set()
#     usage_details = {}

#     # 1) Check File docs attached to OTHER items with same URL
#     try:
#         file_docs = frappe.get_all(
#             "File",
#             filters={
#                 "file_url": file_url,
#                 "attached_to_doctype": "Item",
#                 "attached_to_name": ["!=", item_code],
#                 "is_folder": 0,
#                 "name": ["!=", file_name],
#             },
#             fields=["attached_to_name", "attached_to_field"],
#             limit=1000,
#         )

#         for fd in file_docs:
#             other_item = fd.get("attached_to_name")
#             if other_item:
#                 other_items.add(other_item)
#                 field = fd.get("attached_to_field") or "attachment"
#                 usage_details.setdefault(f"File.{field}", []).append(other_item)
#     except Exception:
#         pass

#     # 2) Check Item fields (image, website_image, etc.) in OTHER items
#     try:
#         for fn in _item_image_fields():
#             items_using_url = frappe.get_all(
#                 "Item",
#                 filters={fn: file_url, "name": ["!=", item_code]},
#                 pluck="name",
#                 limit=1000,
#             )
#             for itm in items_using_url:
#                 other_items.add(itm)
#                 usage_details.setdefault(f"Item.{fn}", []).append(itm)
#     except Exception:
#         pass

#     # 3) Check child doctypes
#     child_candidates = ["Item Image", "Website Item Image", "Item Website Image"]
#     for dt in child_candidates:
#         if not _doctype_exists(dt):
#             continue
#         try:
#             meta = frappe.get_meta(dt)
#             fields = [f for f in ("image", "file_url", "website_image", "url") if meta.has_field(f)]
#             for fn in fields:
#                 rows = frappe.get_all(
#                     dt,
#                     filters={fn: file_url, "parent": ["!=", item_code]},
#                     fields=["parent"],
#                     limit=1000,
#                 )
#                 for r in rows:
#                     other_items.add(r["parent"])
#                     usage_details.setdefault(f"{dt}.{fn}", []).append(r["parent"])
#         except Exception:
#             continue

#     return {
#         "is_shared": len(other_items) > 0,
#         "other_items_count": len(other_items),
#         "other_items": sorted(list(other_items)),
#         "usage_details": usage_details,
#     }


# # -------------------------------------------------------------------
# # Keep / Delete helpers
# # -------------------------------------------------------------------
# def _pick_keeper(files):
#     """
#     Keeper priority:
#     1) attached_to_field == "image"
#     2) any attached_to_field
#     3) file status exists/unknown > missing
#     4) oldest creation
#     """
#     def score(f):
#         st = _file_status(f.get("file_url"))
#         exists_rank = 0 if (st is True or st is None) else 1
#         return (
#             0 if f.get("attached_to_field") == "image" else 1,
#             0 if f.get("attached_to_field") else 1,
#             exists_rank,
#             f.get("creation") or frappe.utils.now_datetime(),
#         )

#     return sorted(files, key=score)[0]


# def _safe_delete_or_unlink_file_doc(
#     file_name: str,
#     file_url: str,
#     item_code: str,
#     dry_run: bool,
#     report: dict
# ) -> dict:
#     """
#     Smart deletion strategy:
#     - If file is shared with other items → UNLINK only (delete this File doc, preserve physical file)
#     - If file is unique to this item → DELETE fully (File doc deletion triggers physical file cleanup)
    
#     Returns: {
#         "action": "deleted" | "unlinked" | "skipped",
#         "shared": bool,
#         "reason": str,
#         "other_items": [...] if shared
#     }
#     """
#     share_info = _is_file_shared(file_url, file_name, item_code)

#     if share_info["is_shared"]:
#         # UNLINK STRATEGY: File is shared, only remove THIS item's File doc
#         report.setdefault("unlinked_shared_files", []).append({
#             "file_name": file_name,
#             "file_url": file_url,
#             "item_code": item_code,
#             "reason": "shared_with_other_items",
#             "other_items_count": share_info["other_items_count"],
#             "other_items": share_info["other_items"][:10],
#         })

#         if not dry_run:
#             try:
#                 frappe.delete_doc("File", file_name, ignore_permissions=True, force=1)
#             except Exception as e:
#                 report["errors"].append({
#                     "file": file_name,
#                     "stage": "unlink_shared",
#                     "error": str(e),
#                     "traceback": _safe_tb(e),
#                 })
#                 return {
#                     "action": "skipped",
#                     "shared": True,
#                     "reason": "delete_failed",
#                     "error": str(e),
#                 }

#         return {
#             "action": "unlinked",
#             "shared": True,
#             "reason": "shared_with_other_items",
#             "other_items": share_info["other_items"],
#         }

#     # FULL DELETE: File is unique to this item
#     if not dry_run:
#         try:
#             frappe.delete_doc("File", file_name, ignore_permissions=True, force=1)
#         except Exception as e:
#             report["errors"].append({
#                 "file": file_name,
#                 "stage": "delete_unique",
#                 "error": str(e),
#                 "traceback": _safe_tb(e),
#             })
#             return {
#                 "action": "skipped",
#                 "shared": False,
#                 "reason": "delete_failed",
#                 "error": str(e),
#             }

#     return {
#         "action": "deleted",
#         "shared": False,
#         "reason": "unique_to_item",
#     }


# def _fetch_item_files(item_code: str):
#     return frappe.get_all(
#         "File",
#         filters={"attached_to_doctype": "Item", "attached_to_name": item_code, "is_folder": 0},
#         fields=["name", "file_name", "file_url", "content_hash", "attached_to_field", "creation"],
#         order_by="creation asc",
#     )


# def _rewire_then_delete(
#     file_to_delete: dict,
#     keeper: dict,
#     *,
#     item_code: str,
#     dry_run: bool,
#     report: dict,
#     stage: str,
#     reason: str,
# ):
#     """
#     SMART VERSION:
#     1. Rewire references within THIS item (old_url → keeper_url)
#     2. Check if file is shared with other items
#     3. If shared → UNLINK (delete this File doc only, preserve file)
#        If unique → DELETE (full removal)
    
#     Returns: True if action was taken (deleted or unlinked)
#     """
#     file_name = file_to_delete.get("name")
#     file_url = _norm_url(file_to_delete.get("file_url"))
#     keeper_url = _norm_url(keeper.get("file_url"))

#     # 1) Rewire references within THIS item only
#     if file_url and keeper_url and file_url != keeper_url:
#         try:
#             _rewrite_references_item_scope(
#                 file_url,
#                 keeper_url,
#                 exclude_file_docname=file_name,
#                 dry_run=dry_run,
#                 report=report,
#                 context={
#                     "stage": stage,
#                     "reason": reason,
#                     "item_code": item_code,
#                     "file_to_delete": file_name,
#                     "keeper": keeper.get("name"),
#                 },
#             )
#         except Exception as e:
#             report["errors"].append({
#                 "file": file_name,
#                 "stage": f"{stage}_rewire",
#                 "traceback": _safe_tb(e),
#             })

#     # 2) Smart delete/unlink
#     result = _safe_delete_or_unlink_file_doc(file_name, file_url, item_code, dry_run, report)
    
#     # Log action taken
#     action_taken = result["action"] in ("deleted", "unlinked")
#     if action_taken:
#         log_entry = {
#             "name": file_name,
#             "file_url": file_url,
#             "action": result["action"],
#             "shared": result["shared"],
#             "stage": stage,
#             "reason": reason,
#         }
#         if result["shared"]:
#             log_entry["other_items_count"] = len(result.get("other_items", []))
#             log_entry["other_items_sample"] = result.get("other_items", [])[:5]
        
#         # Add to appropriate detailed report section
#         if result["action"] == "unlinked":
#             report.setdefault("shared_files_unlinked", []).append(log_entry)
#         elif result["action"] == "deleted":
#             report.setdefault("unique_files_deleted", []).append(log_entry)
    
#     return action_taken


# # -------------------------------------------------------------------
# # Public: normalize (ONE item)
# # -------------------------------------------------------------------
# @frappe.whitelist()
# def normalize_item_images(
#     item_code: str,
#     dry_run: int = 1,
#     perceptual: int = 1,
#     dhash_threshold: int = 0,
#     safe_mode: int = 1,
# ):
#     """
#     SMART SHARED FILE HANDLING:
#     - If image is shared with other items → UNLINK from this item (delete File doc, keep physical file)
#     - If image is unique to this item → DELETE fully (File doc deletion triggers physical cleanup)
    
#     Guarantees:
#       - exactly ONE main image (File.attached_to_field="image" + Item.image)
#       - all remaining attached images are distinct (exact + file_url + perceptual)
#       - perceptual dedupe applies even if file has no extension
#       - orphan/broken link cleanup: if file_url is confirmed missing -> clear refs + delete/unlink File docs
#       - before deleting/unlinking duplicates, rewire references within this item to the kept file_url
#       - NEVER breaks other items: shared files are unlinked, not deleted
    
#     Args:
#         item_code: Item to normalize
#         dry_run: 1 = no changes, 0 = apply changes
#         perceptual: 1 = use perceptual hash (dHash) for duplicate detection
#         dhash_threshold: Hamming distance threshold for perceptual duplicates (0 = exact match)
#         safe_mode: 1 = only purge files not shared with other items, 0 = purge all missing files (for stage 0)
#     """
#     
#     dry_run = bool(int(dry_run))
#     perceptual = bool(int(perceptual))
#     dhash_threshold = int(dhash_threshold)
#     safe_mode = bool(int(safe_mode))
    
#     report = {
#         "item_code": item_code,
#         "dry_run": dry_run,
#         "safe_mode": safe_mode,
#         "missing_links_cleared": [],
#         "missing_file_docs_deleted": [],
#         "rewired_references": [],
#         "exact_dup_deleted": [],
#         "file_url_dup_deleted": [],
#         "orphans_deleted": [],
#         "perceptual_deleted": [],
#         "skipped_shared_files": [],
#         "unlinked_shared_files": [],
#         "shared_files_unlinked": [],
#         "unique_files_deleted": [],
#         "keeper": None,
#         "errors": [],
#     }

#     files = _fetch_item_files(item_code)
#     if not files:
#         return report

#     deleted = set()
#     # Caches to avoid repeated I/O and repeated purges
#     status_cache = {}
#     purged_urls = set()
#     
#     def cached_status(url: str):
#         u = _norm_url(url)
#         if not u:
#             return None
#         if u in status_cache:
#             return status_cache[u]
#         st = _file_status(u)
#         status_cache[u] = st
#         return st
#     # ---------------------------------------------------------------
#     # 0) Purge broken links among this item's files (confirmed missing)
#     # ---------------------------------------------------------------
#     try:
#         urls = sorted({_norm_url(f.get("file_url")) for f in files if _norm_url(f.get("file_url"))})
#         for url in urls:
#             
#             if not _is_local_file_url(url):
#                 continue
#             if url in purged_urls:
#                 continue
#             st = cached_status(url)
#             if st is False:
#                 # SAFE MODE: check if other items use this URL
#                 if safe_mode:
#                     other_items_count = _count_items_using_url(url, exclude_item_code=item_code)
#                     if other_items_count > 0:
#                         report["skipped_shared_files"].append({
#                             "file_url": url,
#                             "reason": "shared_with_other_items",
#                             "other_items_count": other_items_count,
#                         })
#                         purged_urls.add(url)  # Don't try again
#                         continue

#                 # SCOPED purge: only affects this item
#                 _purge_missing_url_everywhere(url, dry_run=dry_run, report=report, item_code=item_code)
#                 purged_urls.add(url)
#     except Exception as e:
#         report["errors"].append({"file": None, "stage": "missing_links_purge", "traceback": _safe_tb(e)})

#     if not dry_run and (report["missing_file_docs_deleted"] or report["missing_links_cleared"]):
#         files = _fetch_item_files(item_code)

#     # ---------------------------------------------------------------
#     # 1) Exact duplicates by (file_url, content_hash)  (SAFE delete with rewire)
#     # ---------------------------------------------------------------
#     groups = {}
#     for f in files:
#         groups.setdefault((_norm_url(f.get("file_url")), f.get("content_hash")), []).append(f)

#     for _, group in groups.items():
#         if len(group) <= 1:
#             continue
#         k = _pick_keeper(group)
#         for d in group:
#             if d["name"] == k["name"] or d["name"] in deleted:
#                 continue
#             try:
#                 did = _rewire_then_delete(
#                     d, k,
#                     item_code=item_code,
#                     dry_run=dry_run,
#                     report=report,
#                     stage="exact",
#                     reason="exact_duplicate",
#                 )
#                 deleted.add(d["name"])
#                 report["exact_dup_deleted"].append({"name": d["name"], "file_url": d.get("file_url"), "deleted": did})
#             except Exception as e:
#                 report["errors"].append({"file": d["name"], "stage": "exact_delete", "traceback": _safe_tb(e)})

#     if not dry_run and report["exact_dup_deleted"]:
#         files = _fetch_item_files(item_code)

#     # ---------------------------------------------------------------
#     # 2) Duplicate by file_url (two File docs pointing to same stored file) (SAFE delete with rewire)
#     # ---------------------------------------------------------------
#     by_url = {}
#     for f in files:
#         by_url.setdefault(_norm_url(f.get("file_url")), []).append(f)

#     for url, group in by_url.items():
#         if not url or len(group) <= 1:
#             continue
#         k = _pick_keeper(group)
#         for d in group:
#             if d["name"] == k["name"] or d["name"] in deleted:
#                 continue
#             try:
#                 did = _rewire_then_delete(
#                     d, k,
#                     item_code=item_code,
#                     dry_run=dry_run,
#                     report=report,
#                     stage="file_url_dup",
#                     reason="same_file_url",
#                 )
#                 deleted.add(d["name"])
#                 report["file_url_dup_deleted"].append({"name": d["name"], "file_url": url, "deleted": did})
#             except Exception as e:
#                 report["errors"].append({"file": d["name"], "stage": "file_url_dup_delete", "traceback": _safe_tb(e)})

#     if not dry_run and report["file_url_dup_deleted"]:
#         files = _fetch_item_files(item_code)

#     # ---------------------------------------------------------------
#     # 3) Orphans: purge ONLY if confirmed missing
#     # ---------------------------------------------------------------
#     remaining = []
#     for f in files:
#         try:
#             url = _norm_url(f.get("file_url"))
#             if not url:
#                 remaining.append(f)
#                 continue

#             st = cached_status(url)
#             if _is_local_file_url(url) and st is False and f["name"] not in deleted:
#                 if url not in purged_urls:
#                     # SAFE MODE: check if other items use this URL
#                     if safe_mode:
#                         other_items_count = _count_items_using_url(url, exclude_item_code=item_code)
#                         if other_items_count > 0:
#                             report["skipped_shared_files"].append({
#                                 "file_url": url,
#                                 "reason": "shared_with_other_items",
#                                 "other_items_count": other_items_count,
#                             })
#                             purged_urls.add(url)  # Don't try again
#                             remaining.append(f)
#                             continue

#                     # SCOPED purge: only affects this item
#                     _purge_missing_url_everywhere(url, dry_run=dry_run, report=report, item_code=item_code)
#                     purged_urls.add(url)
#                 deleted.add(f["name"])
#                 report["orphans_deleted"].append({"name": f["name"], "file_url": url, "deleted": (not dry_run)})
#             else:
#                 remaining.append(f)
#         except Exception as e:
#             report["errors"].append({"file": f["name"], "stage": "orphan_check", "traceback": _safe_tb(e)})

#     files = remaining
#     if not dry_run and (report["orphans_deleted"] or report["missing_file_docs_deleted"]):
#         files = _fetch_item_files(item_code)

#     # ---------------------------------------------------------------
#     # 4) Perceptual dedupe on ALL real images (FIXED keep-set logic)
#     # ---------------------------------------------------------------
#     if perceptual:
#         enriched = []
#         for f in files:
#             try:
#                 if f["name"] in deleted:
#                     continue
#                 doc = frappe.get_doc("File", f["name"])
#                 if not _is_image_doc(doc):
#                     continue
#                 b = _get_file_bytes(doc)
#                 f["_dhash"] = _dhash_int(b)
#                 enriched.append(f)
#             except Exception as e:
#                 report["errors"].append({"file": f["name"], "stage": "dhash_read", "traceback": _safe_tb(e)})

#         keep = []
#         for f in enriched:
#             if f["name"] in deleted:
#                 continue

#             match_idx = None
#             for idx, k in enumerate(keep):
#                 if k["name"] in deleted:
#                     continue
#                 if _hamming(f["_dhash"], k["_dhash"]) <= dhash_threshold:
#                     match_idx = idx
#                     break

#             if match_idx is None:
#                 keep.append(f)
#                 continue

#             k = keep[match_idx]

#             chosen = _pick_keeper([f, k])
#             if chosen["name"] == k["name"]:
#                 keeper_ref = k
#                 other = f
#             else:
#                 keeper_ref = f
#                 other = k
#                 keep[match_idx] = f  # replace kept element

#             if other["name"] in deleted:
#                 continue

#             did = _rewire_then_delete(
#                 other,
#                 keeper_ref,
#                 item_code=item_code,
#                 dry_run=dry_run,
#                 report=report,
#                 stage="perceptual",
#                 reason="perceptual_duplicate",
#             )
#             deleted.add(other["name"])
#             report["perceptual_deleted"].append({
#                 "name": other["name"],
#                 "file_url": other.get("file_url"),
#                 "deleted": did,
#                 "threshold": dhash_threshold,
#                 "reason": "perceptual_duplicate",
#                 "keeper": {"name": keeper_ref.get("name"), "file_url": keeper_ref.get("file_url")},
#             })

#         if not dry_run and report["perceptual_deleted"]:
#             files = _fetch_item_files(item_code)

#     # ---------------------------------------------------------------
#     # 5) Enforce ONE keeper as main image
#     # ---------------------------------------------------------------
#     files = [f for f in files if f["name"] not in deleted]
#     if files:
#         keeper = _pick_keeper(files)
#         report["keeper"] = {
#             "name": keeper["name"],
#             "file_url": keeper.get("file_url"),
#             "attached_to_field": keeper.get("attached_to_field"),
#         }

#         # Delete duplicates-of-keeper (perceptual compare) safely
#         if perceptual:
#             try:
#                 keeper_doc = frappe.get_doc("File", keeper["name"])
#                 if _is_image_doc(keeper_doc):
#                     keeper_hash = _dhash_int(_get_file_bytes(keeper_doc))

#                     for f in files:
#                         if f["name"] == keeper["name"] or f["name"] in deleted:
#                             continue
#                         try:
#                             doc = frappe.get_doc("File", f["name"])
#                             if not _is_image_doc(doc):
#                                 continue
#                             h = _dhash_int(_get_file_bytes(doc))
#                             if _hamming(keeper_hash, h) <= dhash_threshold:
#                                 did = _rewire_then_delete(
#                                     f,
#                                     keeper,
#                                     item_code=item_code,
#                                     dry_run=dry_run,
#                                     report=report,
#                                     stage="keeper_compare",
#                                     reason="duplicate_of_keeper",
#                                 )
#                                 deleted.add(f["name"])
#                                 report["perceptual_deleted"].append({
#                                     "name": f["name"],
#                                     "file_url": f.get("file_url"),
#                                     "deleted": did,
#                                     "threshold": dhash_threshold,
#                                     "reason": "duplicate_of_keeper",
#                                     "keeper": {"name": keeper.get("name"), "file_url": keeper.get("file_url")},
#                                 })
#                         except Exception as e:
#                             report["errors"].append({"file": f["name"], "stage": "keeper_compare", "traceback": _safe_tb(e)})
#             except Exception as e:
#                 report["errors"].append({"file": keeper["name"], "stage": "keeper_hash", "traceback": _safe_tb(e)})

#         if not dry_run:
#             # Set Item.image
#             try:
#                 item = frappe.get_doc("Item", item_code)
#                 kurl = _norm_url(keeper.get("file_url"))
#                 if kurl and _norm_url(item.image) != kurl:
#                     item.image = kurl
#                     item.save(ignore_permissions=True)
#             except Exception as e:
#                 report["errors"].append({"file": keeper["name"], "stage": "set_item_image", "traceback": _safe_tb(e)})

#             # Ensure keeper attached_to_field="image"
#             try:
#                 kd = frappe.get_doc("File", keeper["name"])
#                 if kd.attached_to_field != "image":
#                     kd.attached_to_field = "image"
#                     kd.save(ignore_permissions=True)
#             except Exception as e:
#                 report["errors"].append({"file": keeper["name"], "stage": "set_keeper_main", "traceback": _safe_tb(e)})

#             # Delete any other main images (rewire first)
#             try:
#                 mains = frappe.get_all(
#                     "File",
#                     filters={
#                         "attached_to_doctype": "Item",
#                         "attached_to_name": item_code,
#                         "attached_to_field": "image",
#                         "is_folder": 0,
#                         "name": ["!=", keeper["name"]],
#                     },
#                     fields=["name", "file_url", "attached_to_field", "creation", "file_name", "content_hash"],
#                 )
#                 for m in mains:
#                     if m["name"] in deleted:
#                         continue
#                     _rewire_then_delete(
#                         m,
#                         keeper,
#                         item_code=item_code,
#                         dry_run=dry_run,
#                         report=report,
#                         stage="delete_extra_mains",
#                         reason="extra_main_image",
#                     )
#                     deleted.add(m["name"])
#             except Exception as e:
#                 report["errors"].append({"file": keeper["name"], "stage": "delete_extra_mains", "traceback": _safe_tb(e)})

#     if not dry_run:
#         frappe.db.commit()

#     return report
import os
import hashlib
import frappe
from frappe.utils.file_manager import get_file
import io
from PIL import Image, ImageOps
import pdb

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def _is_image_url(url: str) -> bool:
    if not url:
        return False
    base = url.split("?")[0].lower()
    _, ext = os.path.splitext(base)
    return ext in IMAGE_EXTS


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_of_media(m: dict, cache: dict) -> str | None:
    """
    Hash media entry:
    - If it has file_docname => use File.get_content() (reliable)
    - Else fallback to get_file(file_url) (for item.image primary)
    Uses cache to avoid re-reading same file repeatedly.
    """
    # 1) Prefer File doc
    file_id = m.get("file_docname")
    if file_id:
        ck = ("file", file_id)
        if ck in cache:
            return cache[ck]
        try:
            content = frappe.get_doc("File", file_id).get_content()
            if not content:
                cache[ck] = None
                return None
            sig = _sha256_bytes(content)
            cache[ck] = sig
            return sig
        except Exception:
            cache[ck] = None
            return None

    # 2) Fallback to URL
    file_url = (m.get("file_url") or "").split("?")[0]
    if not file_url:
        return None

    ck = ("url", file_url)
    if ck in cache:
        return cache[ck]

    try:
        _path, content = get_file(file_url)
        if not content:
            cache[ck] = None
            return None
        sig = _sha256_bytes(content)
        cache[ck] = sig
        return sig
    except Exception:
        cache[ck] = None
        return None


def _pick_primary_file_doc(item_name: str, primary_url: str):
    """
    Returns a File row dict (name, file_url, creation, attached_to_field) or None.
    Preference:
      1) File attached to this item + attached_to_field="image" + same file_url
      2) File attached to this item + same file_url
      3) Any File with same file_url (fallback)
    """
    if not primary_url:
        return None

    # 1) Best match: same item + field=image + same url
    row = frappe.db.get_value(
        "File",
        {
            "attached_to_doctype": "Item",
            "attached_to_name": item_name,
            "attached_to_field": "image",
            "file_url": primary_url,
        },
        ["name", "file_url", "creation", "attached_to_field"],
        as_dict=True,
    )
    if row:
        return row

    # 2) Same item + same url
    row = frappe.db.get_value(
        "File",
        {
            "attached_to_doctype": "Item",
            "attached_to_name": item_name,
            "file_url": primary_url,
        },
        ["name", "file_url", "creation", "attached_to_field"],
        as_dict=True,
    )
    if row:
        return row

    # 3) Any file with same url (rare but possible after migrations)
    return frappe.db.get_value(
        "File",
        {"file_url": primary_url},
        ["name", "file_url", "creation", "attached_to_field"],
        as_dict=True,
    )

def _list_item_media(item_name: str):
    """
    Returns (item_doc, media_list) ordered:
      1) primary (prefer File doc if exists, else URL-only)
      2) attachments (File docs attached to item), images only, creation ASC
    """
    item = frappe.get_doc("Item", item_name)
    media = []

    primary_url = (item.image or "").split("?")[0]
    primary_file = None

    # Primary: promote the matching File doc (so you have file_docname)
    if primary_url and _is_image_url(primary_url):
        primary_file = _pick_primary_file_doc(item.name, primary_url)
        media.append({
            "role": "primary",
            "file_url": primary_url,
            "file_docname": primary_file["name"] if primary_file else None,
            "creation": primary_file.get("creation") if primary_file else None,
            "attached_to_field": primary_file.get("attached_to_field") if primary_file else None,
        })

    # Attachments (exclude the chosen primary file doc to avoid duplication)
    files = frappe.get_all(
        "File",
        filters={"attached_to_doctype": "Item", "attached_to_name": item.name},
        fields=["name", "file_url", "creation", "attached_to_field"],
        order_by="creation asc",
    )

    primary_file_id = primary_file["name"] if primary_file else None

    for f in files:
        if primary_file_id and f["name"] == primary_file_id:
            continue

        url = (f.get("file_url") or "").split("?")[0]
        if not _is_image_url(url):
            continue

        media.append({
            "role": "attachment",
            "file_url": url,
            "file_docname": f["name"],
            "creation": f.get("creation"),
            "attached_to_field": f.get("attached_to_field"),
        })

    return item, media


def _detach_file_doc(file_docname: str, note: str = ""):
    f = frappe.get_doc("File", file_docname)

    # detach only (safe)
    f.attached_to_doctype = None
    f.attached_to_name = None
    f.attached_to_field = None



    f.save(ignore_permissions=True)


def _dhash_int_from_bytes(content: bytes, hash_size: int = 8) -> int:
    img = Image.open(io.BytesIO(content))
    img = ImageOps.exif_transpose(img)          # fix rotation from phones
    img = img.convert("L").resize((hash_size + 1, hash_size))
    px = list(img.getdata())

    bits = 0
    for y in range(hash_size):
        row = y * (hash_size + 1)
        for x in range(hash_size):
            bits = (bits << 1) | (1 if px[row + x] > px[row + x + 1] else 0)
    return bits

def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()

def _dhash_of_media(m: dict, cache: dict):
    """
    Returns int dhash or None
    Cache key is based on file_docname or file_url.
    """
    file_id = m.get("file_docname")
    if file_id:
        ck = ("dhash_file", file_id)
        if ck in cache:
            return cache[ck]
        try:
            content = frappe.get_doc("File", file_id).get_content()
            if not content:
                cache[ck] = None
                return None
            h = _dhash_int_from_bytes(content)
            cache[ck] = h
            return h
        except Exception:
            cache[ck] = None
            return None

    file_url = (m.get("file_url") or "").split("?")[0]
    if not file_url:
        return None

    ck = ("dhash_url", file_url)
    if ck in cache:
        return cache[ck]

    try:
        _path, content = get_file(file_url)
        if not content:
            cache[ck] = None
            return None
        h = _dhash_int_from_bytes(content)
        cache[ck] = h
        return h
    except Exception:
        cache[ck] = None
        return None


@frappe.whitelist()
def dedupe_item_images(item_name: str, dry_run: bool = True, detach_missing: bool = False,
                      use_dhash: bool = True, dhash_threshold: int = 8):

    item, media = _list_item_media(item_name)

    cache = {}
    missing = []

    # compute sha + dhash
    for m in media:
        m["sig"] = _sha256_of_media(m, cache)
        if m["sig"] is None:
            missing.append(m)

        if use_dhash:
            m["dhash"] = _dhash_of_media(m, cache)
        else:
            m["dhash"] = None

    kept = []
    duplicates = []
    seen_sha = set()
    kept_hashes = []   # store dhash of kept images (primary + kept attachments)

    for m in media:
        # primary always kept
        if m["role"] == "primary":
            kept.append(m)
            if m["sig"]:
                seen_sha.add(m["sig"])
            if use_dhash and m["dhash"] is not None:
                kept_hashes.append(m["dhash"])
            continue

        # unreadable attachment
        if m["sig"] is None and (not use_dhash or m["dhash"] is None):
            if (not dry_run) and detach_missing and m.get("file_docname"):
                _detach_file_doc(m["file_docname"])
            else:
                kept.append(m)
            continue

        # 1) exact duplicate by sha256
        if m["sig"] and m["sig"] in seen_sha:
            duplicates.append({**m, "dup_reason": "sha256"})
            continue

        # 2) perceptual duplicate by dhash (similar image)
        if use_dhash and m["dhash"] is not None and kept_hashes:
            is_similar = False
            best_dist = None
            for h in kept_hashes:
                dist = _hamming(m["dhash"], h)
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                if dist <= int(dhash_threshold):
                    is_similar = True
                    break
            if is_similar:
                duplicates.append({**m, "dup_reason": "dhash", "distance": best_dist})
                continue

        # keep it
        kept.append(m)
        if m["sig"]:
            seen_sha.add(m["sig"])
        if use_dhash and m["dhash"] is not None:
            kept_hashes.append(m["dhash"])

    # detach duplicates
    if not dry_run:
        for d in duplicates:
            if d.get("file_docname"):
                _detach_file_doc(d["file_docname"])

        frappe.db.commit()

    return {
        "item": item.name,
        "dry_run": dry_run,
        "use_dhash": use_dhash,
        "dhash_threshold": dhash_threshold,
        "total_images_found": len(media),
        "kept_count": len(kept),
        "duplicate_count": len(duplicates),
        "missing_count": len(missing),
        "duplicates_detached": [
            {
                "file_url": d.get("file_url"),
                "file_docname": d.get("file_docname"),
                "sig": d.get("sig"),
                "dup_reason": d.get("dup_reason"),
                "distance": d.get("distance"),
            }
            for d in duplicates
        ],
    }


