from __future__ import annotations

# --- stdlib ---
import base64
import difflib
import io
import json
import mimetypes
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse
from xml.parsers.expat import model
import hashlib  # <-- ADD
# --- third-party ---
from erpnext.setup.doctype import brand
import requests
from PIL import Image
from openai import OpenAI
import random

# --- frappe ---
import frappe
from frappe.utils import get_url
from frappe.utils.file_manager import save_file

from google import genai
from google.genai import types as genai_types
from woocommerce_fusion.tasks.utils import APIWithRequestLogging
from woocommerce_fusion.integrations.item_images_treatment import _is_image_url, _detach_file_doc, dedupe_item_images

class ItemGroupClassifier:
    """
    Classify ERPNext Items into existing (leaf) Item Groups using the OpenAI SDK (multimodal).
    Uses full breadcrumb labels to disambiguate similar leaf names.

    Reads Single DocType "AI settings" (case-insensitive):
      - openai_api_key (Data)
      - open_ai_model (Data) or openai_model
      - open_ai_temperature (Data) or openai_temperature

    Fallback API key: frappe.conf.openai_api_key
    """

    # -------------------- init & settings --------------------
    def __init__(self, model: Optional[str] = None, temperature: Optional[float] = None):
        self.api_key = self._read_openai_key()
        if not self.api_key:
            frappe.throw(
                "OpenAI API key missing. Set it in Single DocType 'AI settings' (openai_api_key) "
                "or in site_config as openai_api_key."
            )

        self.model = (
            (model or "").strip()
            or self._read_setting("open_ai_model")
            or self._read_setting("openai_model")
            or "gpt-4o-mini"
        )

        try:
            self.temperature = float(
                temperature
                if temperature is not None
                else (
                    self._read_setting("open_ai_temperature")
                    or self._read_setting("openai_temperature")
                    or 0.2
                )
            )
        except Exception:
            self.temperature = 0.2

        self.client = OpenAI(api_key=self.api_key)

    @staticmethod
    def _ai_single_name() -> Optional[str]:
        for dt in ("AI settings", "AI Settings"):
            if frappe.db.exists("DocType", dt):
                return dt
        return None

    @classmethod
    def _read_setting(cls, fieldname: str) -> Optional[str]:
        dt = cls._ai_single_name()
        if not dt:
            return None
        try:
            doc = frappe.get_cached_doc(dt)
            val = getattr(doc, fieldname, None)
            if isinstance(val, str):
                return val.strip() or None
            return (str(val).strip() or None) if val is not None else None
        except Exception:
            return None

    @classmethod
    def _read_openai_key(cls) -> Optional[str]:
        return cls._read_setting("openai_api_key")

    # -------------------- public API --------------------
    def _get_parent_group(self, group_name: str) -> Optional[str]:
        """
        Get the parent Item Group of a given group.
        Returns None if no parent or if group doesn't exist.
        """
        try:
            parent = frappe.db.get_value("Item Group", group_name, "parent_item_group")
            return parent
        except Exception:
            return None

    def get_wc_api(self, wc_server: str) -> APIWithRequestLogging:
        """Return WooCommerce API client with logging for the given server."""
        if not wc_server:
            frappe.throw("wc_server is required")
        wc_server_doc = frappe.get_doc("WooCommerce Server", wc_server)
        return APIWithRequestLogging(
            url=wc_server_doc.woocommerce_server_url,
            consumer_key=wc_server_doc.api_consumer_key,
            consumer_secret=wc_server_doc.api_consumer_secret,
            version="wc/v3",
            timeout=300,
            verify_ssl=True,
        )

    def _fetch_wc_tags(self, server_name: str, *, per_page: int = 1000) -> List[Dict[str, Any]]:
        """
        Return [{'id':int,'name':str,'slug':str,'description':str}, ...] from WooCommerce.
        """
        if not server_name:
            return []
        api = self.get_wc_api(server_name)
        page, out = 1, []
        while True:
            resp = api.get("products/tags", params={"page": page, "per_page": min(per_page, 100)})
            data = resp.json() if hasattr(resp, "json") else (resp or [])
            if not data:
                break
            out.extend([
                {
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "slug": t.get("slug", ""),
                    "description": t.get("description", ""),
                }
                for t in data
            ])
            if len(data) < 100:
                break
            page += 1
        return out
    @staticmethod
    def _is_promotion_label(s: str) -> bool:
        """Detect promotion-like collection names."""
        txt = unicodedata.normalize("NFKD", (s or "")).encode("ascii", "ignore").decode("ascii").lower()
        return any(k in txt for k in ["promo", "promotion", "promotions", "soldes", "deal", "offers", "offres"])

    def classify_item_tags(
        self,
        item_name: str,
        wc_server: str,
        *,
        language: str = "fr",
        max_collections: int = 3,
        exclude_promotions: bool = True,
        update: bool = True,
        write_field: str = "custom_woocomerce_collection",
    ) -> Dict[str, Any]:
        """
        Classify an Item into WooCommerce tags using ONLY textual fields
        (name/brand/description). No images are used.
        """
        it = frappe.get_doc("Item", item_name)

        wc_tags = self._fetch_wc_tags(wc_server)
        if not wc_tags:
            return {"ok": False, "item": it.name, "error": "No WooCommerce tags found"}
                # Optionally remove promotion-like categories
        if exclude_promotions:
            wc_tags = [c for c in wc_tags if not self._is_promotion_label(c.get("name") or "")]

        allowed_names = [t.get("name") for t in wc_tags if t.get("name")]
        by_name_ci = {t["name"].strip().lower(): t for t in wc_tags if t.get("name")}

        # Build prompts (text only)
        sys = (
            "You are an e-commerce assistant. From the provided WooCommerce TAG LIST, "
            "select the most relevant tags for this product. Return STRICT JSON only:\n"
            '{ "tags": [string,...], "rationale": "..." }\n'
            f"- Choose up to {max_collections} tags.\n"
            "- Use EXACT tag names as provided (case and spelling).\n"
            "- Prefer specific tags. If none fit, return an empty list."
        )
        description = f"{it.description or ''}"
        if it.custom_web_short_description:
            description = f"{it.description or ''}\n\n{it.custom_web_short_description}"
        user_obj = {
            "language": language,
            "allowed_tags": allowed_names,
            "item": {
                "name": it.item_name,
                "description": description,
            },
        }

        params = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": sys},
                {"role": "user", "content": [{"type": "text", "text": json.dumps(user_obj, ensure_ascii=False)}]},
            ],
        }
        if not str(self.model).lower().startswith("gpt-5"):
            params["temperature"] = self.temperature

        res = self.client.chat.completions.create(**params)
        raw = res.choices[0].message.content
        try:
            data = json.loads(raw)
        except Exception:
            m = re.search(r"\{.*\}", raw, flags=re.S)
            data = json.loads(m.group(0)) if m else {}
        chosen: List[str] = []
        for name in (data.get("tags") or []):
            if not isinstance(name, str):
                continue
            key = name.strip().lower()
            c = by_name_ci.get(key)
            if not c:
                continue
            if exclude_promotions and self._is_promotion_label(c["name"]):
                continue
            if c["name"] not in chosen:
                chosen.append(c["name"])
            if len(chosen) >= int(max_collections):
                break

        mapping = {str(by_name_ci[n.lower()]["id"]): by_name_ci[n.lower()]["name"] for n in chosen if n.lower() in by_name_ci}
        out = {
            "ok": True,
            "item": it.name,
            "selected_collections": chosen,
            "selected_collection_ids": [by_name_ci[n.lower()]["id"] for n in chosen if n.lower() in by_name_ci],
            "mapping": mapping,  # { "123": "Name", ... }
            "rationale": (data.get("rationale") or "")[:300],
            "updated": False,
        }

        if update:
            if write_field and it.meta.has_field(write_field):
                # Save JSON mapping {woocommerce_id: "collection name"}
                text = json.dumps(mapping, ensure_ascii=False)
                # Respect field length if defined
                try:
                    df = it.meta.get_field(write_field)
                    if getattr(df, "length", None):
                        text = text[: df.length]
                except Exception:
                    pass
                setattr(it, write_field, text)
            it.custom_generate_tag=0
            it.save(ignore_permissions=True)
            frappe.db.commit()
            out["updated"] = True

        return out
    def classify_item(
        self,
        item_name: str,
        candidate_groups: Optional[List[Dict[str, Any]]] = None,
        *,
        update: bool = False,
        threshold: float = 0.80,
        language: str = "en",
        skip_root_if: Optional[str] = None,
        # images
        use_images: bool = True,
        max_images: int = 3,
        encode_policy: str = "auto",  # "auto" | "always" | "never"
        image_urls: Optional[List[str]] = None,  # manual override (URLs or data: URLs)
        image_detail: str = "low",  # "low" | "high"
    ) -> Dict[str, Any]:
        """
        Classify one Item and (optionally) write back to item_group when confidence >= threshold.

        encode_policy:
          - "auto": base64 if private/local/unreachable; else public URL (recommended)
          - "always": always base64 when file exists (good for dev/local)
          - "never": only public URLs (requires CDN/public media)
        """
        it = frappe.get_doc("Item", item_name)

        allowed = candidate_groups or self._leaf_groups_with_paths(skip_root_if=skip_root_if)
        if not allowed:
            frappe.throw("No leaf Item Groups found.")
        
        payload = self._build_payload(
            name=(it.item_name or "").strip(),
            description=(it.description or "").strip(),
            uom=it.stock_uom,
            allowed_groups=allowed,
            language=language,
        )

        # collect item images if not provided
        if image_urls is None and use_images:
            image_urls = self._collect_item_images(
                item_name=it.name,
                max_images=max_images,
                encode_policy=encode_policy,
            )

        content = self._chat_json(payload, image_urls=image_urls or [], image_detail=image_detail)
        data = self._safe_json(content)

        chosen_label = (data.get("chosen_label") or "").strip()
        conf = float(data.get("confidence") or 0.0)
        rationale = (data.get("rationale") or "").strip()[:300]
        alts = [a for a in (data.get("alternatives") or [])][:3]  # labels

        # Map chosen label back to canonical leaf name
        chosen_name = self._resolve_from_label(chosen_label, allowed)
        if not chosen_name:
            for a in alts:
                chosen_name = self._resolve_from_label(a, allowed)
                if chosen_name:
                    break

        if not chosen_name:
            allowed_names = {g["name"] for g in allowed}
            chosen_name = it.item_group if it.item_group in allowed_names else allowed[0]["name"]
            conf = min(conf, 0.6)
            # ==================== NEW LOGIC: Filter alternatives by unique parent ====================
    
        # Get the parent of the chosen group
        chosen_parent = self._get_parent_group(chosen_name)
        
        # Filter alternatives to keep only those with different parents
        filtered_alts = []
        seen_parents = {chosen_parent}  # Start with chosen group's parent
        
        for alt_label in alts:
            last_group = alt_label.split(" > ")[-1].strip() if " > " in alt_label else alt_label.strip()
            
            # Resolve to get the full group name
            alt_name = self._resolve_from_label(alt_label, allowed)
            if not alt_name:
                continue
            
            alt_parent = self._get_parent_group(alt_name)
            
            # Keep alternative only if its parent is unique
            if alt_parent not in seen_parents:
                filtered_alts.append(last_group)
                seen_parents.add(alt_parent)
        
        # Use filtered alternatives
        alts = filtered_alts
        out = {
            "item": it.name,
            "suggested_group": chosen_name,
            "confidence": round(conf, 3),
            "rationale": rationale,
            "alternatives": alts,
            "updated": False,
        }

        if update and conf >= float(threshold) and it.item_group != chosen_name:
            it.item_group = chosen_name
            it.custom_force_regenerate_item_group=0
            # Save alternatives to custom_woocommerce_categories
        if it.meta.has_field("custom_woocomerce_categories"):
            # Join alternatives with comma
            it.custom_woocomerce_categories = ", ".join(alts) if alts else ""
        it.custom_generate_classification=0
        it.save(ignore_permissions=True)
        frappe.db.commit()
        out["updated"] = True

        return out

    def batch_by_filters(
        self,
        *,
        item_filters: Optional[Dict[str, Any]] = None,
        limit: int = 500,
        update: bool = False,
        threshold: float = 0.80,
        language: str = "en",
        skip_root_if: Optional[str] = None,
        # images
        use_images: bool = True,
        max_images: int = 3,
        encode_policy: str = "auto",
        image_detail: str = "low",
    ) -> List[Dict[str, Any]]:
        """Classify many Items selected by filters (returns list of result dicts)."""
        rows = frappe.get_all(
            "Item",
            filters=item_filters or {},
            fields=["name"],
            order_by="modified desc",
            limit_page_length=limit,
        )
        allowed = self._leaf_groups_with_paths(skip_root_if=skip_root_if)
        results = []
        for r in rows:
            results.append(
                self.classify_item(
                    r["name"],
                    candidate_groups=allowed,
                    update=update,
                    threshold=threshold,
                    language=language,
                    skip_root_if=skip_root_if,
                    use_images=use_images,
                    max_images=max_images,
                    encode_policy=encode_policy,
                    image_detail=image_detail,
                )
            )
        return results

    # -------------------- internals --------------------
    def _leaf_groups_with_paths(self, skip_root_if: Optional[str]) -> List[Dict[str, Any]]:
        """
        Returns: [{ "name": <leaf>, "label": "Root > ... > Leaf", "path": ["Root","...","Leaf"] }]
        """
        rows = frappe.get_all(
            "Item Group",
            fields=["name", "parent_item_group", "is_group", "lft", "rgt"],
            order_by="lft asc",
            limit_page_length=10000,
        )
        by_name = {r["name"]: r for r in rows}

        def path_to_root(n: str) -> List[str]:
            p, out, seen = n, [], set()
            while p and p not in seen and p in by_name:
                seen.add(p)
                out.append(p)
                p = by_name[p].get("parent_item_group")
            out.reverse()
            if skip_root_if and out and out[0] == skip_root_if:
                out = out[1:]
            return out

        leaves = [r for r in rows if r["is_group"] == 0]
        out = []
        for leaf in leaves:
            path = path_to_root(leaf["name"])
            label = " > ".join(path)
            out.append({"name": leaf["name"], "label": label, "path": path})
        return out

    @staticmethod
    def _is_public_url(abs_url: str) -> bool:
        """True if URL is HTTP(S) and not localhost/host.docker.internal, etc."""
        try:
            p = urlparse(abs_url)
            host = (p.hostname or "").lower()
            if host in {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"}:
                return False
            return p.scheme in {"http", "https"}
        except Exception:
            return False

    @staticmethod
    def _file_url_to_path(file_url: str) -> Optional[str]:
        """Map /files/... or /private/files/... to disk path."""
        s = (file_url or "").strip("/")
        parts = s.split("/")
        if len(parts) >= 2 and parts[0] == "private" and parts[1] == "files":
            return frappe.get_site_path("private", "files", "/".join(parts[2:]))
        if len(parts) >= 1 and parts[0] == "files":
            return frappe.get_site_path("public", "files", "/".join(parts[1:]))
        return None

    @staticmethod
    def _to_data_url(path: str) -> str:
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        return f"data:{mime};base64,{b64}"

    def _collect_item_images(
        self,
        item_name: str,
        max_images: int = 3,
        encode_policy: str = "auto",  # "auto" | "always" | "never"
    ) -> List[str]:
        """
        Return up to max_images image sources (public URLs or data: URLs).

        auto   -> base64 if private/local/unreachable; else URL
        always -> always base64 (if file exists)
        never  -> only URLs (must be publicly reachable)
        """
        files = frappe.get_all(
            "File",
            filters={"attached_to_doctype": "Item", "attached_to_name": item_name},
            fields=["file_url", "is_private"],
            order_by="is_private asc, creation asc",
            limit_page_length=20,
        )

        out: List[str] = []
        base = get_url()

        for f in files:
            url = (f.get("file_url") or "").strip()
            if not url:
                continue

            fs_path = self._file_url_to_path(url)
            abs_url = url if url.lower().startswith(("http://", "https://", "data:")) else urljoin(base, url)

            if encode_policy == "always":
                if fs_path and Path(fs_path).exists():
                    out.append(self._to_data_url(fs_path))
                else:
                    # fallback to abs_url if file missing
                    out.append(abs_url)

            elif encode_policy == "never":
                if not f.get("is_private") and self._is_public_url(abs_url):
                    out.append(abs_url)

            else:  # auto
                if f.get("is_private") or not self._is_public_url(abs_url):
                    if fs_path and Path(fs_path).exists():
                        out.append(self._to_data_url(fs_path))
                    else:
                        out.append(abs_url)
                else:
                    out.append(abs_url)

            if len(out) >= max_images:
                break

        return out

    @staticmethod
    def _build_payload(
        name: str,
        description: str,
        uom: Optional[str],
        allowed_groups: List[Dict[str, Any]],
        language: str,
    ) -> Dict[str, Any]:
        """
        Ask the model to choose EXACTLY one label from the provided breadcrumb labels.
        """
        sys = (
        "You are a highly accurate product catalog classifier specialized in water treatment equipment.\n"
        "Your task is to assign the most suitable category to a given product using ONLY the provided list of category labels (each with full breadcrumb paths).\n\n"
        "Return your response in STRICT JSON format with the following keys:\n"
        "  - chosen_label: the single best-matching label from the list (EXACT match, case-sensitive).\n"
        "  - confidence: a float between 0 and 1 indicating your certainty.\n"
        "  - rationale: a concise justification for your choice (max 300 characters).\n"
        "  - alternatives: a list of up to 3 plausible alternative labels from the list (EXACT matches), which could also fit the item—for example, for secondary classification in WordPress.\n"
        "    If alternatives exist, the list MUST include at least one label that belongs to a DIFFERENT group after the shared root 'Tous les Groupes d'Articles >', if such a label is available.\n"
        "    The alternatives list may be empty only if no suitable alternatives exist at all.\n\n"
        "IMPORTANT DOMAIN RULES:\n"
        "- If a label refers to a complete system (e.g., 'Osmoseur', 'Système d’osmose inverse', 'Station de filtration'), it MUST be preferred over any of its sub-components "
        "(e.g., 'Pompes booster', 'Réservoirs', 'Robinet', 'Filtres', 'Membranes') when the product description suggests a whole device.\n"
        "- If the product name includes terms like 'kit', 'ensemble', 'système', 'machine', or 'appareil complet', always classify it as a complete system.\n"
        "- Component-level labels (pump, tank, faucet, membrane, filter, etc.) should only be selected if the product clearly describes a standalone spare part.\n"
        "- Do NOT classify reverse osmosis systems under their individual parts.\n"
        "- Do NOT invent, modify, or assume labels.\n"
        "- Use only labels from the provided list.\n"
        "- Ensure all labels match exactly and preserve case.\n"
        "- Return ONLY the JSON object, without any extra explanation or formatting."
    )

        user = {
            "language": language,
            "allowed_groups": allowed_groups,  # [{name,label,path:[...]}, ...]
            "item": {
                "name": name,
                "description": description[:4000],
                "uom": uom,
            },
        }
        return {"system": sys, "user": user}

    def _chat_json(self, payload: Dict[str, Any], image_urls: List[str], image_detail: str = "low") -> str:
        # multimodal user content: text + images
        user_content: List[Dict[str, Any]] = [
            {"type": "text", "text": json.dumps(payload["user"], ensure_ascii=False)}
        ]
        for u in image_urls[:3]:
            user_content.append({"type": "image_url", "image_url": {"url": u, "detail": image_detail}})

                # Build params
        params = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": payload["system"]},
                {"role": "user", "content": user_content},
            ],
        }
        if not str(self.model).lower().startswith("gpt-5"):
            params["temperature"] = self.temperature
                

        res = self.client.chat.completions.create(**params)
        return res.choices[0].message.content

    @staticmethod
    def _safe_json(text: str) -> Dict[str, Any]:
        try:
            return json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, flags=re.S)
            return json.loads(m.group(0)) if m else {}

    @staticmethod
    def _resolve_from_label(chosen_label: str, allowed: List[Dict[str, Any]]) -> Optional[str]:
        if not chosen_label:
            return None
        # exact (case-insensitive) on label
        for g in allowed:
            if g["label"].lower() == chosen_label.lower():
                return g["name"]
        # fuzzy on label
        labels = [g["label"] for g in allowed]
        match = difflib.get_close_matches(chosen_label, labels, n=1, cutoff=0.6)
        if match:
            for g in allowed:
                if g["label"] == match[0]:
                    return g["name"]
        return None


# -------------------- whitelisted wrappers --------------------

@frappe.whitelist()
def get_wc_tags(
    wc_server: str,
):
    clf = ItemGroupClassifier()
    return clf._fetch_wc_tags(server_name=wc_server)
@frappe.whitelist()
def classify_item_collections(
    item_name: str,
    wc_server: str,
    language: str = "fr",
    max_collections: int = 3,
    exclude_promotions: int = 1,
    update: int = 1,
    write_field: str = "custom_woocomerce_collection",
) -> Dict[str, Any]:
    
    """
    Whitelisted: classify an Item into WooCommerce tags using ONLY text (no images).
    """
    clf = ItemGroupClassifier()
    return clf.classify_item_tags(
        item_name=item_name,
        wc_server=wc_server,
        language=language,
        max_collections=int(max_collections),
        exclude_promotions=bool(int(exclude_promotions)),
        update=bool(int(update)),
        write_field=write_field,
    )    
@frappe.whitelist()
def classify_item_group(
    item_name: str,
    update: int = 0,
    threshold: float = 0.8,
    language: str = "en",
    skip_root_if: str = "",
    use_images: int = 1,
    max_images: int = 3,
    encode_policy: str = "auto",  # "auto" | "always" | "never"
    image_detail: str = "low",    # "low" | "high"
):
    clf = ItemGroupClassifier()
    return clf.classify_item(
        item_name=item_name,
        update=bool(int(update)),
        threshold=float(threshold),
        language=language,
        skip_root_if=(skip_root_if or None),
        use_images=bool(int(use_images)),
        max_images=int(max_images),
        encode_policy=encode_policy,
        image_detail=image_detail,
    )
@frappe.whitelist()
def classify_all_item_group(
    update: int = 0,
    threshold: float = 0.8,
    language: str = "en",
    skip_root_if: str = "",
    use_images: int = 1,
    max_images: int = 3,
    encode_policy: str = "auto",  # "auto" | "always" | "never"
    image_detail: str = "low",    # "low" | "high"
):
   all_items = frappe.get_all(
    "Item",
    filters={"disabled": 0},   # Only enabled (not disabled)
    fields=["name"]
    )
   results=[]

   for item in all_items:
       result = classify_item_group(
           item_name=item.name,
           update=update,
           threshold=threshold,
           language=language,
           skip_root_if=skip_root_if,
           use_images=use_images,
           max_images=max_images,
           encode_policy=encode_policy,
           image_detail=image_detail,
       )
       results.append(result)

   return results

@frappe.whitelist()
def classify_items_by_filters(
    item_filters: str = "{}",
    limit: int = 200,
    update: int = 0,
    threshold: float = 0.8,
    language: str = "en",
    skip_root_if: str = "",
    use_images: int = 1,
    max_images: int = 3,
    encode_policy: str = "auto",
    image_detail: str = "low",
):
    clf = ItemGroupClassifier()
    try:
        filters = json.loads(item_filters) if item_filters else {}
    except Exception:
        filters = {}
    return clf.batch_by_filters(
        item_filters=filters,
        limit=int(limit),
        update=bool(int(update)),
        threshold=float(threshold),
        language=language,
        skip_root_if=(skip_root_if or None),
        use_images=bool(int(use_images)),
        max_images=int(max_images),
        encode_policy=encode_policy,
        image_detail=image_detail,
    )
def _field_max_len(it, fieldname: str) -> Optional[int]:
    try:
        df = it.meta.get_field(fieldname)
        return getattr(df, "length", None)
    except Exception:
        return None

def _join_keywords_to_fit(keywords: list[str], max_len: Optional[int]) -> str:
    toks = [k.strip() for k in (keywords or []) if isinstance(k, str) and k.strip()]
    out, total = [], 0
    for k in toks:
        piece = (", " if out else "") + k
        if max_len and total + len(piece) > max_len:
            break
        out.append(k); total += len(piece)
    return ", ".join(out)

# --- slug & sanitisation ---
def _slugify(text: str) -> str:
    s = (text or "").strip().lower()
    # retirer accents
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    # remplacer tout sauf a-z0-9 par '-'
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return (s[:80] or "product")

def _sanitize_html(html: str) -> str:
    html = re.sub(r"<\s*(script|style)\b.*?>.*?</\1\s*>", "", html, flags=re.I | re.S)
    html = re.sub(r"\son\w+\s*=\s*(['\"]).*?\1", "", html, flags=re.I)
    return html

# --- vision: extraire identité ---
def _extract_description_from_images(openai_client: OpenAI, model: str, images: List[str],context: List[str]) -> Dict[str, Any]:
    """
    Generate a structured product description and technical specifications
    based on a list of related product images.
    """

    if not images:
        return {}

    # User instructions (JSON schema expected)
    user_content = [{
        "type": "text",
        "text": json.dumps({
            "task": (
                "You must analyze the provided product images to identify all visible technical specifications. "
                "Additionally, use the product name and a short user-provided description as context before analyzing the image content. "
                "Generate a concise, factual product description that highlights the key features of the item. "
                "If some details are missing or unclear, complete them using your general knowledge as realistically as possible. "
                "All measurements (weight, dimensions, volume, power, etc.) must be expressed in International System of Units (SI)."
            ),
            "context_fields": {
                "product_name": context[0] if context else "",              # user-provided name of the product
                "short_description": context[1] if context else ""          # short user-provided description
            },
            "schema": {
                "brand": "string?",
                "model": "string?",
                "part_number": "string?",
                "label_text": "string?",
                "keywords": "array?",
                "description": "string",               # human-readable long description
                "specifications": "object",            # structured technical specs
                "sources": "array"                     # keep image URLs for traceability
            }
        }, ensure_ascii=False)
    }]


    # Attach up to 3 images (expand if needed)
    for u in images[:3]:
        user_content.append({"type": "image_url", "image_url": {"url": u, "detail": "high"}})

    # Call LLM
    params = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": (
                "You are a product catalog extractor. "
                "Use OCR and visual cues from the images to identify technical specifications, "
                "and create a clear product description. "
                "Return STRICT JSON only."
            )},
            {"role": "user", "content": user_content},
        ],
    }
    if not str(model).lower().startswith("gpt-5"):
        params["temperature"] = 0.01
    res = openai_client.chat.completions.create(**params)

    try:
        return json.loads(res.choices[0].message.content)
    except Exception:
        return {}

# --- OpenAI web search (Assistants + web_retrieval), plus robuste ---
@frappe.whitelist()
def _openai_web_search_specs(
    query: str,
    *,
    web_model: str = "gpt-4.1-mini",   # Responses API model
    language: str = "fr",
    max_wait_s: float = 90.0,
    specifications: dict = {},  # kept for compatibility; Responses is sync
    client: Optional[OpenAI] = None,
) -> Dict[str, Any]:
    """
    Responses API + web_search with robust JSON handling.
    Returns: {"bullets":[...], "sources":[...]} (or {"error": "..."} on failure)
    """
    # ---- helpers (local) -----------------------------------------------------
    def _strip_code_fences(s: str) -> str:
        s = s.strip()
        if s.startswith("```"):
            s = re.sub(r"^```(?:json|JSON)?\s*", "", s)
            s = re.sub(r"\s*```$", "", s)
        return s.strip()

    def _coerce_json(s: str) -> Dict[str, Any]:
        # try direct
        try:
            return json.loads(s)
        except Exception:
            pass
        # strip fences
        s2 = _strip_code_fences(s)
        try:
            return json.loads(s2)
        except Exception:
            pass
        # replace smart quotes and tidy commas
        s3 = s2.replace("“", '"').replace("”", '"').replace("’", "'")
        s3 = re.sub(r",\s*([}\]])", r"\1", s3)  # trailing commas
        try:
            return json.loads(s3)
        except Exception:
            pass
        # extract largest {...} block
        m = re.search(r"\{[\s\S]*\}", s3)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        return {}

    def _bullets_from_text(s: str, limit: int = 10) -> List[str]:
        lines = []
        for line in s.splitlines():
            m = re.match(r"\s*(?:[-*•]|\d+[.)])\s+(.*)", line)
            if m:
                lines.append(m.group(1).strip())
        # fallback: split by periods if no list markers
        if not lines:
            chunks = re.split(r"[•\u2022;\n]", s)
            lines = [c.strip() for c in chunks if len(c.strip()) > 0]
        # dedupe & clamp
        out, seen = [], set()
        for b in lines:
            if b and b not in seen:
                seen.add(b); out.append(b)
            if len(out) >= limit:
                break
        return out

    def _urls_from_text(s: str, limit: int = 6) -> List[str]:
        urls = re.findall(r"https?://[^\s)>\]}]+", s)
        out, seen = [], set()
        for u in urls:
            if u not in seen:
                seen.add(u); out.append(u)
            if len(out) >= limit:
                break
        return out

    # ---- build client --------------------------------------------------------
    try:
        if client is None:
            api_key = ItemGroupClassifier._read_openai_key()
            if not api_key:
                frappe.throw("openai_api_key manquant (AI settings ou site_config).")
            client = OpenAI(api_key=api_key)

        max_bullets = 10
        max_sources = 6
        
        prompt = (
            f"Find concise technical specifications for: `{query}`.\n"
            f"Answer in {language}. Return STRICT JSON only:\n"
            '{"bullets":[string,...],"sources":[string,...]} '
            f"(max {max_bullets} bullets, max {max_sources} sources; sources must be URLs)."
        )
        if specifications:
            prompt += f" Use these existing specifications as context: {json.dumps(specifications, ensure_ascii=False)}"
        # ---- Responses API call with web_search + JSON-only output -----------
        resp = client.responses.create(
            model=web_model,
            tools=[{"type": "web_search"}],
            input=prompt
        )

        # Primary text (model should already output JSON because of response_format)
        text = getattr(resp, "output_text", "") or ""
        data = _coerce_json(text)

        # Collect any citations that SDK might expose inside the structured output
        urls_from_blocks: List[str] = []
        try:
            for block in getattr(resp, "output", []) or []:
                for part in getattr(block, "content", []) or []:
                    # Some SDKs expose citations/links on parts
                    cites = getattr(part, "citations", None) or getattr(part, "citation", None)
                    if isinstance(cites, list):
                        for c in cites:
                            u = getattr(c, "url", None)
                            if isinstance(u, str) and u:
                                urls_from_blocks.append(u)
        except Exception:
            pass

        # normalize
        bullets = [b for b in (data.get("bullets") or []) if isinstance(b, str)][:max_bullets]
        sources = [s for s in (data.get("sources") or []) if isinstance(s, str)][:max_sources]

        # if JSON was empty, salvage from plain text
        if not bullets:
            bullets = _bullets_from_text(text, limit=max_bullets)
        if not sources:
            extracted = _urls_from_text(text, limit=max_sources)
            sources = extracted[:]

        # merge block citations
        for u in urls_from_blocks:
            if len(sources) >= max_sources:
                break
            if u not in sources:
                sources.append(u)

        if not bullets and not sources and not text.strip():
            raise RuntimeError("Empty web_search response")

        return {"bullets": bullets, "sources": sources}

    except Exception as e:
        return {"error": str(e), "bullets": [], "sources": []}

# --- enrichissement avec recherche web ---
def is_under_other_group(item_group_name, group_to_check="Services & Interventions"):
    # Get target item group
    item_group = frappe.get_doc("Item Group", item_group_name)

    # Get the "Services & Interventions" node
    root_group = frappe.get_doc("Item Group", group_to_check)

    # Check if the item group is within the lft/rgt of the root group
    return (
        item_group.lft > root_group.lft and 
        item_group.rgt < root_group.rgt
    )
def get_unique_variant_attribute_values(template_item_name: str):
    """
    Returns a dictionary mapping each attribute -> list of unique values
    across all variants of a given Item Template, sorted by their order in Item Attribute.
    """
    if not frappe.db.exists("Item", template_item_name):
        frappe.throw(f"Template '{template_item_name}' introuvable.")

    # 1️⃣ Get all variants for the template
    variants = frappe.get_all("Item", filters={"variant_of": template_item_name}, pluck="name")
    if not variants:
        return {}

    # 2️⃣ Fetch all variant attributes
    rows = frappe.get_all(
        "Item Variant Attribute",
        filters={"parent": ["in", variants]},
        fields=["attribute", "attribute_value"]
    )

    # 3️⃣ Build dict of unique values manually
    unique_values = {}
    for row in rows:
        attr = row["attribute"]
        val = row["attribute_value"]
        if not val:
            continue
        if attr not in unique_values:
            unique_values[attr] = []
        if val not in unique_values[attr]:
            unique_values[attr].append(val)

    # 4️⃣ Sort values based on their order in Item Attribute (not alphabetically)
    for attr_name in unique_values:
        unique_values[attr_name] = _sort_by_item_attribute_order(attr_name, unique_values[attr_name])

    return unique_values


def _sort_by_item_attribute_order(attribute_name: str, values: list) -> list:
    """
    Sort attribute values based on their idx order in Item Attribute Value child table.
    Values not found in the attribute definition are placed at the end.
    """
    try:
        # Get the Item Attribute document
        attr_doc = frappe.get_doc("Item Attribute", attribute_name)
        
        # Create a mapping: value -> idx (position)
        value_order = {}
        for attr_val_row in (attr_doc.item_attribute_values or []):
            value_order[attr_val_row.attribute_value] = attr_val_row.idx
        
        # Sort using the idx from Item Attribute
        # Values not in the attribute get a high number (sorted to end)
        def sort_key(val):
            return value_order.get(val, 999999)
        
        return sorted(values, key=sort_key)
        
    except frappe.DoesNotExistError:
        # If attribute doesn't exist, return original order
        frappe.logger().warning(f"Item Attribute '{attribute_name}' not found")
        return values
    except Exception as e:
        # On any error, return values as-is
        frappe.logger().error(f"Error sorting attribute '{attribute_name}': {str(e)}")
        return values

def get_variant_attributes(item_name: str) -> dict:
    """Return {attribute: value} for an Item variant; {} if not a variant."""
    if not frappe.db.exists("Item", item_name):
        frappe.throw(f"Item '{item_name}' not found")

    doc = frappe.get_doc("Item", item_name)
    if not doc.variant_of:
        return {}  # not a variant

    out = {}
    for row in (doc.attributes or []):
        if row.attribute and row.attribute_value:
            out[row.attribute] = row.attribute_value
    return out
def build_variant_intro(variants=None, config_variant=None, max_chars=160):
    """
    Returns a short one-liner like:
    'Variantes disponibles — Configuration : A, B | Membrane : X, Y | Purge : ...'
    If only config_variant is provided (for a single variant), it returns:
    'Configuration : A | Membrane : X | Purge : ...'
    """
    def join_vals(v):
        if isinstance(v, list):
            s = ", ".join(v[:4])
            if len(v) > 4:
                s += "…"
            return s
        return str(v)

    parts = []
    if variants:
        for attr, vals in variants.items():
            if vals:
                parts.append(f"{attr} : {join_vals(vals)}")
        prefix = "Variantes disponibles — "
    elif config_variant:
        for attr, val in config_variant.items():
            if val:
                parts.append(f"{attr} : {join_vals(val)}")
        prefix = ""  # this is a single variant; no “Variantes disponibles —”
    else:
        return ""

    s = prefix + " | ".join(parts)
    # Trim to max_chars without cutting mid-word too badly
    if len(s) > max_chars:
        s = s[:max_chars-1].rstrip(" ,|") + "…"
    return s
@frappe.whitelist()
def enrich_item_content_with_openai_search(
    item_name: str,
    language: str = "fr",
    tone: str = "professionnel",
    target_audience: str = "grand public",
    update: int = 1,
    overwrite: int = 1,
    # images
    use_images: int = 1,
    max_images: int = 3,
    encode_policy: str = "auto",
    image_detail: str = "low",
    # modèles
    vision_model: str = "",
    web_model: str = "gpt-4.1-mini"
):
    it = frappe.get_doc("Item", item_name)
    api_key = ItemGroupClassifier._read_openai_key()
    if not api_key:
        frappe.throw("openai_api_key manquant (AI settings ou site_config).")
    client = OpenAI(api_key=api_key)

    base_model = (
        ItemGroupClassifier._read_setting("open_ai_model")
        or ItemGroupClassifier._read_setting("openai_model")
        or "gpt-4o-mini"
    )
    vis_model = (vision_model or base_model)
    try:
        temperature = float(
            ItemGroupClassifier._read_setting("open_ai_temperature")
            or ItemGroupClassifier._read_setting("openai_temperature")
            or 0.2
        )
    except Exception:
        temperature = 0.2

    # images
    images: List[str] = []
    if int(use_images):
        images = ItemGroupClassifier()._collect_item_images(
            item_name=it.name, max_images=int(max_images), encode_policy=encode_policy
        )

    # vision -> identité
    
    identity = _extract_description_from_images(client, vis_model, images,[it.name,it.description]) if images else {}
    brand = (identity.get("brand") or "").strip() or getattr(it, "brand", None)
    model = (identity.get("model") or "").strip()
    label_text = (identity.get("label_text") or "").strip()
    keywords = [str(k).strip() for k in (identity.get("keywords") or []) if str(k).strip()][:4]
    description_from_image = (identity.get("description") or "").strip()
    specifications = identity.get("specifications") or {}
    # quote multi-word keywords
    kw_tokens = [f'"{k}"' if " " in k else k for k in keywords]

    parts = [
        brand or "",
        model or "",
        getattr(it, "item_name", "") or "",
        getattr(it, "description", "") or "",
        description_from_image or "",
        *kw_tokens,
    ]

    # requête web + fallback si rien
    # dedupe tokens, case-insensitive
    seen = set(); tokens = []
    for t in parts:
        if not t: 
            continue
        k = t.lower()
        if k not in seen:
            tokens.append(t); seen.add(k)

    query = " ".join(tokens).strip()
    web = _openai_web_search_specs( query=query, web_model=web_model, language=language, max_wait_s=90.0,specifications=specifications,client=client)

    if not (web.get("sources") or web.get("bullets")):
        q2 = f"{it.item_name or it.item_code} specification site:pdf OR datasheet"
        web2 = _openai_web_search_specs( query=q2, web_model=web_model, language=language, max_wait_s=60.0,client= client)
        if (web2.get("sources") or web2.get("bullets")):
            web["bullets"] = list(dict.fromkeys((web.get("bullets") or []) + (web2.get("bullets") or [])))[:10]
            web["sources"] = list(dict.fromkeys((web.get("sources") or []) + (web2.get("sources") or [])))[:6]

    user_keywords = {
        "language": language,
        "tone": tone,
        "audience": target_audience,
        "item": {
            "name": it.item_name,
            "group": it.item_group,
            "brand": brand,
            "uom": getattr(it, "stock_uom", None),
            "description_raw": (it.description or "")[:4000],
            "hints_from_image": {
                "brand": brand, "model": model, "label_text": label_text,
                "keywords": identity.get("keywords") or [],
                "description": identity.get("description") or "",
                "specifications": identity.get("specifications") or {},

            },
            "web_context": {
                # "source_urls": web.get("sources") or [],
                "spec_bullets": web.get("bullets") or [],
            },
        },
        "output_schema": {
            "keywords": "array of 5-12",
            "classification": "string",
        }
    }
    prompt = (
    "You are an expert SEO keyword generator for an e-commerce store specializing in water treatment products.\n\n"
    f"Write in: {language}. Tone: {tone}. Audience: {target_audience} interested in water filtration and purification systems.\n\n"

    "🔍 TASK:\n"
    "1. Generate **5 to 12 highly relevant and specific SEO keyword phrases** for the given product.\n"
    "2. Classify the product into one of the following categories based on its function:\n"
    '   • "reverse osmosis system"\n'
    '   • "filter cartridge"\n'
    '   • "other filtration system"\n'
    '   • "not a filtration system"\n\n'

    "🧠 CONTEXT:\n"
    "Use ONLY the following product data:\n"
    "- Product name and brand\n"
    "- Product description\n"
    "- Technical specifications and known use cases\n"
    "- Visual cues (optional, e.g., label text or usage context)\n\n"

    "🎯 KEYWORD RULES:\n"
    "• The **first keyword** must be the best-performing, most specific **focus keyword**.\n"
    "• Use **2 to 5 word phrases** focused on product type + feature (e.g., '5 micron sediment filter').\n"
    "• Avoid brand names unless widely recognized.\n"
    "• Do NOT repeat keywords. Use singular/plural only when meaningfully different.\n"
    "• Avoid overly generic words like 'water' or 'filter' unless in a specific phrase.\n\n"

    "📦 FORMAT:\n"
    "Return a valid JSON object with exactly two fields:\n"
    "{\n"
    '  "keywords": [\n'
    '    "5 micron sediment filter",\n'
    '    "10 inch pre-filter cartridge",\n'
    '    "reverse osmosis replacement filter",\n'
    '    "high capacity pp filter",\n'
    '    "universal 254 mm water filter"\n'
    '  ],\n'
    '  "classification": "filter cartridge"\n'
    "}"
)
    
    
    user_content = [{"type": "text", "text": json.dumps(user_keywords, ensure_ascii=False)}]
    # for u in images[:3]:
    #     user_content.append({"type": "image_url", "image_url": {"url": u, "detail": image_detail}})
    #     # Call LLM
    model_key="gpt-5-mini"
    params = {
        "model": model_key,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": user_content}],
    }
    if not str(model_key).lower().startswith("gpt-5"):
        params["temperature"] = temperature
    resp = client.chat.completions.create(**params)
    content = resp.choices[0].message.content
    try:
        data = json.loads(content)
    except Exception:
        m = re.search(r"\{.*\}", content, flags=re.S)
        data = json.loads(m.group(0)) if m else {}
    
    keywords = [k for k in (data.get("keywords") or []) if isinstance(k, str)][:12]
    focus_keyword = keywords[0] if keywords else ""
    seo_class = data.get("classification") if isinstance(data.get("classification"), str) else ""
    if not it.brand:
        brand = (identity.get("brand") or "").strip() or getattr(it, "brand", None)
    else:
        brand = it.brand.strip()
    
    variants= None
    config_variant=None
    if it.has_variants:
        variants=get_unique_variant_attribute_values(it.name)
    if it.variant_of:
        config_variant=get_variant_attributes(it.name)
    variant_intro = build_variant_intro(variants=variants, config_variant=config_variant)
    user_obj = {
        "language": language,
        "tone": tone,
        "audience": target_audience,
        "item": {
            "name": it.item_name,
            "group": it.item_group,
            "brand": brand,
            "uom": getattr(it, "stock_uom", None),
            "description_raw": (it.description or "")[:4000],
            "hints_from_image": {
                "brand": brand, "model": model, "label_text": label_text,
                "keywords": identity.get("keywords") or [],
                "description": identity.get("description") or "",
                "specifications": identity.get("specifications") or {},

            },
            "web_context": {
                # "source_urls": web.get("sources") or [],
                "spec_bullets": web.get("bullets") or [],
            },
            "generated_keywords": keywords,
            "focus_keyword": focus_keyword,
            "variants": variants or {},            # dict
            "config_variant": config_variant or {},# dict
            "variant_intro": variant_intro or ""   # stringp
        },
        "output_schema": {
            "seo_title": "<= 60 chars",
            "slug": "kebab-case, <= 80 chars",
            "meta_description": "140-160 chars",
            "long_html": "HTML with <h2>/<ul>/<p>, <= 1200 words. Facts only."
        }
    }
    sys1= (
        "You are a professional e-commerce SEO writer specialized in water filtration products.\n"
    f"Write in: {language}. Tone: {tone}. Audience: {target_audience} looking for water purification systems.\n\n"

    "🧠 CONTEXT:\n"
    "Use ONLY the structured data from:\n"
    "- item.name, item.description_raw, item.brand, item.uom\n"
    "- hints_from_image\n"
    "- web_context.spec_bullets\n"
    "- focus_keyword : which is the focus keyword\n"
    "- generated_keywords (including the focus keyword)\n\n"

    "🔑 FOCUS KEYWORD:\n"
    f"Use the focus keyword: `{focus_keyword}`. Use it EXACTLY as-is:\n"
    "→ Use it in:\n"
    "• seo_title (start)\n"
    "• slug (include)\n"
    "• meta_description (once only)\n"
    "• short_desc (within first 100 characters)\n"
    "• long_html: in <h2>, first paragraph, and at least 6 times overall\n"

    "📋 OUTPUT (valid JSON only):\n")
    
    sys2 = (
        "{\n"
        f'  "seo_title": "≤ 60 chars. Must START with `{focus_keyword}`",\n'
        f'  "slug": "kebab-case, ≤ 80 chars. Must contain `{focus_keyword}`",\n'
        f'  "meta_description": "140–160 chars, `{focus_keyword}` once",\n'
        f'  "short_desc": "≤ 400 chars, keyword in first 100 chars",\n'
        '  "long_html": "well-structured semantic HTML content (see below)",\n'
        "}\n\n"
    )
    
    if variants :
            sys2 = (
            "{\n"
            f'  "seo_title": "≤ 60 chars. Must START with {focus_keyword}",\n'
            f'  "slug": "kebab-case, ≤ 80 chars. Must contain {focus_keyword}",\n'
            f'  "meta_description": "140–160 chars, {focus_keyword} once",\n'
            f'  "short_desc": "≤ 600 chars, keyword in first 100 chars, If `variants`: {variants} is not empty, APPEND it (~≤160 chars) at the an intro that lists ONLY the configuration attribute NAMES from `variants` (i.e., the DICTIONARY KEYS, not their values). Attributs disponibles — <key1> (<brief-role1>) | <key2> (<brief-role2>) | ,\n'
            '  "long_html": "well-structured semantic HTML content (see below)",\n'
            "}\n\n")
    if config_variant :
        parent_it=frappe.get_doc("Item",it.variant_of)
        user_obj = {
        "language": language,
        "tone": tone,
        "audience": target_audience,
        "item": {
            "name": it.item_name,
            "group": it.item_group,
            "brand": brand,
            "uom": getattr(it, "stock_uom", None),
            "description_raw": (it.description or "")[:4000],
            "hints_from_image": {
                "brand": brand, "model": model, "label_text": label_text,
                "keywords": identity.get("keywords") or [],
                "description": identity.get("description") or "",
                "specifications": identity.get("specifications") or {},

            },
            "web_context": {
                # "source_urls": web.get("sources") or [],
                "spec_bullets": web.get("bullets") or [],
            },
            "generated_keywords": keywords,
            "focus_keyword": focus_keyword,
            "variants": variants or {},            # dict
            "config_variant": config_variant or {},# dict
            "variant_intro": variant_intro or ""   # stringp
        },
        "output_schema": {
            "seo_title": "<= 60 chars",
            "slug": "kebab-case, <= 80 chars",
            "meta_description": "140-160 chars",
            "long_html": "HTML with <h2>/<ul>/<p>, <= 400 words. Facts only.",
        }
    }
        sys1 = (
            "You are a professional e-commerce SEO writer specialized in water filtration products.\n"
            f"Write in: {language}. Tone: {tone}. Audience: {target_audience} looking for water purification systems.\n\n"
            "Using the following configuration data, generate a clear, professional description of a product variant.\n"
            "Compare it to the base configuration where relevant, and emphasize the improvements or differences in specifications, features, and performance.\n\n"
            "Your output must contain only the field: \"long_html\".\n"
            "⚠️ The 'long_html' must:\n"
            f"• start with the name of variant as <h3>`{it.item_name}`</h3>.\n"
            "• Include a short paragraph (≤ 100 words) introducing the variant.\n"
            "• Present configuration advantages of the Variant Configuration only as HTML bullet points (<ul><li>...</li></ul>). no more than 5 points.\n"
            "• Highlight technical improvements, additional features, or performance benefits versus the base model.\n"
            "• Maintain a professional, catalog-style tone — concise, factual, and comparative.\n\n"
            f"Base Model Configuration:\n{parent_it.custom_web_short_description}\n\n"
            f"Variant Configuration:\n{config_variant}\n\n"
        )
        
        # sys2 = (
        # "{\n"
        # '  "seo_title": "≤ 60 chars. Must START with `{focus_keyword}`",\n'
        # '  "slug": "kebab-case, ≤ 80 chars. Must contain `{focus_keyword}`",\n'
        # '  "meta_description": "140–160 chars, `{focus_keyword}` once",\n'
        # '  "short_desc": "≤ 400 chars, keyword in first 100 chars",\n'
        # '  "long_html": "≤ 400 chars,introduce this configuration `variant_intro` and describe its advantages knowing that is a variant of this `{parent_it.custom_web_short_description}`",\n'
        # '  "product_weight": "estimation of the product string in kg, e.g., \"0.3\"",\n'
        # "}\n\n")
        sys2=("\n\n")

    sys3=("🧾 long_html must:\n"
    "• At least 800 words (to get green score)\n"
    "• Be semantic HTML using <h2>, <p>, <ul>, <li>\n"
    "• Contain the main keyword in at least one <h2> heading\n"
    "• Start with a descriptive <h2>Présentation</h2>\n"
    "• Structure:\n"
    f"   <h2>Présentation – {focus_keyword} </h2> — introduction with focus keyword\n"

    )
    if variants :
        sys4 = ("""
        • If `variants_list` is non-empty, add a dedicated section and KEEP THE GIVEN ORDER:
        <h2>Configurations disponibles</h2>
        <ul>
            For each element in `variants_list` IN ORDER (DO NOT reorder or sort):
            <li><b>{attribute}</b>:
                <ul>
                    For each value in element.values IN ORDER:
                    <li>{value}</li>
                </ul>
            </li>
        </ul>
        """)

        # html_variants = frappe.render_template(tpl, {"variants": variants})  # ← render now
        # # then inject html_variants into your prompt string
        # sys4 = f"...\n{html_variants}\n..."
    else:
        sys4= ""
    if seo_class == "reverse osmosis system":


       sys5 = (
            "   <h2>Étapes de Filtration</h2>\n"
            "   <p>Describe each stage below (1–2 sentences), factual and reverse osmosis (RO) oriented. "
            "Do NOT echo any rules and do NOT add a 'STRICT RULES' section.</p>\n"
            "   <ul>\n"
            "     <li><strong>PP</strong>: • describe its role in the filtration chain (sediment/particles prefilter)</li>\n"
            "     <li><strong>UDF</strong>: • describe its role in the filtration chain (granular carbon, taste/odor/chlorine if applicable)</li>\n"
            "     <li><strong>CTO</strong>: • describe its role in the filtration chain (carbon block, fine taste/odor/chlorine if applicable)</li>\n"
            "     <li><strong>RO</strong>: • describe its role in the filtration chain (reverse osmosis membrane, core separation stage)</li>\n"
            "     <li><strong>post carbon</strong>: • describe its role in the filtration chain (final polishing for taste/odor)</li>\n"
            "   </ul>\n"
            "   <p><strong>Optional</strong> (only if the product truly has extra stages such as UV, remineralization, alkalization): "
            "add a subheading <code>&lt;h3&gt;Optional&lt;/h3&gt;</code> and then a list of optional stages. "
            "Otherwise, add nothing.</p>\n"
            "   <p>Return ONLY the final HTML for this section, with no extra commentary.</p>\n"
        )      
    elif seo_class == "filter cartridge":
        sys5 = (
        "   <h2>Étapes de Filtration</h2>"
        "     • this is a filter cartridge, specify in which stage only it is used (e.g., first stage – sediment 5 microns) and compatible systems\n"
        )
    elif seo_class == "other filtration system":
        sys5 = (
        "   <h2>Étapes de Filtration</h2>"
        "     • describe its role in the overall filtration chain\n"
        )
    else:
        sys5 = (""


        )
    sys6= (
        "   <h2>Caractéristiques</h2> — specs, materials, microns, pressure, etc.\n"
        "   <h2>Compatibilité et Usage</h2> — use cases, flow rates, pressure, pipe sizes, etc.\n"
        "   <h2>Entretien</h2> — cleaning and replacement frequency\n"
        "   <h2>FAQ</h2> — 3 clear Q&A blocks using real customer concerns - at least 3 common questions and answers.\n"
        "• Keep keyword density around 1.5% for the focus keyword\n\n"
        "📌 STYLE:\n"
        "• Avoid fluff. Focus on pressure, flow, micron rating, materials, dimensions (e.g. 254 mm), compatibility\n"
        f"• this is the brand of the product `{brand}`do not use other names\n"
        "• Do not hallucinate facts or invent specifications\n"
        "• Do not mention the model reference\n"
        "• Avoid any external URLs or the words 'source', 'ressources', etc.\n"
        )
    if config_variant:
        sys6= (
        "📌 STYLE:\n"
        "• Return a JSON object . Reply ONLY with valid json (no extra text).\n"  # <-- THIS IS THE REQUIRED LINE

        "• Avoid fluff. Focus on pressure, flow, micron rating, materials, dimensions (e.g. 254 mm), compatibility\n"
        "• Do not hallucinate facts or invent specifications outside the provided information\n"
        "• Do not mention the model reference\n"
        "• Avoid any external URLs or the words 'source', 'ressources', etc.\n"
        )
        system = "\n\n".join([sys1, sys2,sys6])
    else:
        system = "\n\n".join([sys1, sys2, sys3, sys4, sys5, sys6])
    
    user_content = [{"type": "text", "text": json.dumps(user_obj, ensure_ascii=False)}]
    # for u in images[:3]:
    #     user_content.append({"type": "image_url", "image_url": {"url": u, "detail": image_detail}})
    #     # Call LLM
    model_seo=vis_model
    params = {
        "model": model_seo,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_content}],
    }
    if not str(model_seo).lower().startswith("gpt-5"):
        params["temperature"] = temperature

    resp = client.chat.completions.create(**params)
    content = resp.choices[0].message.content
    
    try:
        data = json.loads(content)
    except Exception:
        m = re.search(r"\{.*\}", content, flags=re.S)
        data = json.loads(m.group(0)) if m else {}
    # normalisation
    seo_title = (data.get("seo_title") or it.item_name or "")[:70]
    slug = _slugify(data.get("slug") or getattr(it, "custom_seo_slug", None) or it.item_code or it.item_name or "")
    meta_description = (data.get("meta_description") or "")[:200]
    long_html = _sanitize_html(data.get("long_html") or "")
    # long_html = _strip_sources_and_links(long_html)
    # keywords = [k for k in (data.get("keywords") or []) if isinstance(k, str)][:12]
    # tech_bullets_gen = [b for b in (data.get("tech_bullets") or []) if isinstance(b, str)][:10]
    # product_weight = (data.get("product_weight") or "")
    short_desc = (data.get("short_desc") or "")[:600]
    if config_variant:
        product_weight = parent_it.custom_seo_weight
        short_desc = parent_it.custom_web_short_description
        if not seo_title:
            seo_title = parent_it.custom_seo_title
        if not slug:
            slug = parent_it.custom_seo_slug
        if not meta_description:
            meta_description = parent_it.custom_seo_meta_description
    weight = estimate_weight_and_delivery_class(client, 'gpt-5-mini', images,[it.name,short_desc,it.description])
 
    product_weight = (str(weight.get("weight_kg")) or "")
    volumineux = (int(weight.get("is_volumineux")) or 0)

    # merge bullets + sources
    # bullets_all = list(dict.fromkeys((web.get("bullets") or []) + tech_bullets_gen))[:10]

    # écriture champs custom avec respect des longueurs
    if int(update):
        def put(fieldname: str, value: Any):
            if not int(overwrite) and getattr(it, fieldname, None):
                return
            df_len = _field_max_len(it, fieldname)
            if isinstance(value, str) and df_len:
                value = value[:df_len]
            setattr(it, fieldname, value)

        put("custom_seo_title", seo_title)
        put("custom_seo_slug", slug)
        put("custom_seo_meta_description", meta_description)
        put("custom_web_short_description", short_desc)
        put("custom_web_long_description", long_html)
        put("custom_seo_weight", product_weight)
        put("custom_is_volumineux", volumineux)
        if it.meta.has_field("custom_seo_keywords"):
            max_len = _field_max_len(it, "custom_seo_keywords") or 140
            kw_text = _join_keywords_to_fit(keywords, max_len)
            put("custom_seo_keywords", kw_text)
        it.custom_generate_seo = 0  # reset flag
        it.save(ignore_permissions=True)
        frappe.db.commit()

    return {
        "ok": True,
        "item": it.name,
        "images_used": len(images),
        "web_sources": web.get("sources") or [],
        "generated": {
            "seo_title": seo_title,
            "slug": slug,
            "meta_description": meta_description,
            "short_desc": short_desc,
            "long_html_preview": (long_html or "")[:800],
            "keywords": keywords,
        },
        "updated": bool(int(update)),
    }

@frappe.whitelist()
def generate_brand_seo_minimal(
    brand_name: str,
    language: str = "fr",
    model: str = "",
    ):
    brand = frappe.get_doc("Brand", brand_name)
    

    if frappe.utils.cint(brand.custom_generate_seo) == 0:
        return {
            "ok": True,
            "skipped": True,
            "reason": "custom_generate_seo == 0",
            "brand": brand.name,
            "existing": {
                "seo_title": brand.custom_seo_title,
                "seo_keywords": brand.custom_seo_keyword,
                "description_preview": (brand.description or "")[:300],
            },
        }
    brand_label = brand.name

    api_key = ItemGroupClassifier._read_openai_key()
    if not api_key:
        frappe.throw("openai_api_key manquant (AI settings ou site_config).")
    client = OpenAI(api_key=api_key)
    model = (model or _read_openai_model(default="gpt-4o-mini"))
    temperature = _read_ai_temperature(default=0.2)
    prompt = f"""
    Tu es un expert SEO pour l’e-commerce (traitement de l’eau).
    Contexte: marque = "{brand_label}".

    RENVOIE UNIQUEMENT un JSON valide (aucun texte en dehors du JSON) avec exactement ces clés:

    - seo_title: ≤ 60 caractères, clair et attractif, inclure "{brand_label}".
    - seo_keywords: chaîne en minuscules, mots-clés séparés par des virgules (pas de point final). 
    Le PREMIER mot-clé DOIT être exactement "{brand_label.lower()}". 
    3 à 8 mots-clés pertinents, uniques, sans doublons.
    - description: 150 à 250 mots, ton professionnel en {language}, bénéfices concrets, sans URL ni prix.

    Contraintes supplémentaires:
    - Pas de balises HTML, pas de Markdown, pas de sauts de ligne.
    - Respecte strictement la casse demandée (mots-clés en minuscules).
    - Ne renvoie QUE le JSON demandé.

    Exemple de format:
    {{"seo_title":"...", "seo_keywords":"{brand_label.lower()}, mot-clé 2, mot-clé 3", "description":"..."}}
    """.strip()
    params = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt}],
    }
    if not str(model).lower().startswith("gpt-5"):
        params["temperature"] = temperature
    resp = client.chat.completions.create(**params)

    content = resp.choices[0].message.content.strip()
    try:
        data = json.loads(content)
    except Exception:
        import re
        m = re.search(r"\{.*\}", content, flags=re.S)
        data = json.loads(m.group(0)) if m else {}

    brand.description=data.get("description")
    brand.custom_seo_title=data.get("seo_title")
    brand.custom_seo_keyword=data.get("seo_keywords")
   
    brand.save(ignore_permissions=True)
    last_modified = frappe.db.get_value("Brand", brand.name, "modified")
    frappe.db.set_value("Brand", brand.name, "custom_generate_seo", 0, update_modified=False)
    frappe.db.set_value("Brand", brand.name, "custom_last_seo_generated", last_modified, update_modified=False)
    frappe.db.commit()
    
    return {
        "ok": True,
        "brand": brand.name,
        "generated": {
            "seo_title": brand.custom_seo_title,
            "seo_keywords": brand.custom_seo_keyword,
            "description_preview": brand.description[:300]
        }
    }

@frappe.whitelist()
def generate_item_group_seo_minimal(
    item_group_name: str,
    language: str = "fr",
    model: str = "",
    ):
    item_group = frappe.get_doc("Item Group", item_group_name)

    if frappe.utils.cint(item_group.custom_generate_seo) == 0:
        return {
            "ok": True,
            "skipped": True,
            "reason": "custom_generate_seo == 0",
            "item_group": item_group.name,
            "existing": {
                "seo_title": item_group.custom_seo_title,
                "seo_keywords": item_group.custom_seo_keyword,
                "description_preview": (item_group.custom_seo_description or "")[:300],
            },
        }
    group_label = item_group.name

    api_key = ItemGroupClassifier._read_openai_key()
    if not api_key:
        frappe.throw("openai_api_key manquant (AI settings ou site_config).")
    client = OpenAI(api_key=api_key)
    model = (model or _read_openai_model(default="gpt-4o-mini"))
    temperature = _read_ai_temperature(default=0.2)
    prompt = f"""
        Tu es un expert SEO e-commerce (traitement de l’eau).
        Contexte : catégorie (Item Group) = "{group_label}".

        RENVOIE UNIQUEMENT un JSON valide (aucun autre texte) avec exactement ces clés :

        - seo_title : ≤ 60 caractères, clair et attractif, inclure "{group_label}".
        - description : 60 à 120 mots, ton professionnel en {language}, orientée catégorie :
        bénéfices concrets, cas d’usage, critères de choix, types de produits inclus.
        Pas d’URL, pas de prix, pas d’allégations exagérées.

        Contraintes supplémentaires :
        - Pas de balises HTML, pas de Markdown, pas de retours à la ligne (une seule ligne).
        - Respect strict des minuscules pour seo_keywords.
        - Ne renvoie QUE le JSON demandé.

        Exemple de format :
        {{"seo_title":"{group_label} – solutions de traitement de l’eau",
        "description":"..." }}
        """.strip()
    params = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt}],
    }
    if not str(model).lower().startswith("gpt-5"):
        params["temperature"] = temperature
    resp = client.chat.completions.create(**params)
    content = resp.choices[0].message.content.strip()
    try:
        data = json.loads(content)
    except Exception:
        import re
        m = re.search(r"\{.*\}", content, flags=re.S)
        data = json.loads(m.group(0)) if m else {}

        # Use db.set_value to avoid nested-set triggers
    frappe.db.set_value("Item Group", item_group.name, "custom_seo_description", data.get("description"), update_modified=False)
    frappe.db.set_value("Item Group", item_group.name, "custom_seo_title", data.get("seo_title"), update_modified=False)
    frappe.db.set_value("Item Group", item_group.name, "custom_seo_keyword", item_group.name.lower(), update_modified=False)
    frappe.db.set_value("Item Group", item_group.name, "custom_generate_seo", 0, update_modified=False)
    frappe.db.commit()
    
    return {
        "ok": True,
        "item_group": item_group.name,
        "generated": {
            "seo_title": data.get("seo_title"),
            "seo_keywords": item_group.name.lower(),
            "description_preview": data.get("description")[:300]
        }
    }




# ---------------------------------------------------------------------------
# Settings helpers (TU LES AS DEJA -> je les réutilise tels quels)
# ---------------------------------------------------------------------------

def _ai_settings_doctype() -> Optional[str]:
    # Reuse the same case-insensitive lookup already implemented for OpenAI.
    return ItemGroupClassifier._ai_single_name()


def _read_ai_setting(fieldname: str) -> Optional[str]:
    return ItemGroupClassifier._read_setting(fieldname)


def _read_ai_temperature(*, default: float = 0.2) -> float:
    try:
        return float(
            _read_ai_setting("open_ai_temperature")
            or _read_ai_setting("openai_temperature")
            or default
        )
    except Exception:
        return default


def _read_openai_model(*, default: str = "gpt-4o-mini") -> str:
    return (
        (_read_ai_setting("open_ai_model") or "").strip()
        or (_read_ai_setting("openai_model") or "").strip()
        or default
    )


def _read_gemini_model(*, purpose: str = "image") -> str:
    """Read Gemini model from AI settings.

    If the configured model looks image-only and we're generating ALT text,
    fall back to a broadly compatible text-capable model.
    """
    configured = (_read_ai_setting("gemini_model") or "").strip()
    if configured:
        if purpose == "alt" and "image" in configured.lower():
            return "gemini-1.5-flash"
        return configured
    return "gemini-2.5-flash-image-preview" if purpose == "image" else "gemini-1.5-flash"


def _get_gemini_client() -> genai.Client:
    dt = _ai_settings_doctype() or "AI settings"
    doc = frappe.get_cached_doc(dt) if _ai_settings_doctype() else frappe.get_single(dt)
    api_key = (getattr(doc, "gemini_api_key", None) or getattr(doc, "google_api_key", None) or "").strip()
    if not api_key:
        frappe.throw("Gemini API key missing in Single DocType 'AI settings' (gemini_api_key).")
    return genai.Client(api_key=api_key)


def _get_openai_client() -> OpenAI:
    api_key = (
        (ItemGroupClassifier._read_openai_key() or "").strip()
        or (frappe.conf.get("openai_api_key") or "").strip()
    )
    if not api_key:
        frappe.throw("OpenAI API key missing (AI settings openai_api_key or site_config openai_api_key).")
    return OpenAI(api_key=api_key)


def _is_gpt5(model_name: str) -> bool:
    return str(model_name or "").lower().startswith("gpt-5")


# ---------------------------------------------------------------------------
# File / image utils
# ---------------------------------------------------------------------------

def _file_url_to_path(file_url: str) -> Optional[str]:
    """Map ERPNext File.file_url -> disk path for local storage."""
    s = (file_url or "").strip("/")
    parts = s.split("/")
    if len(parts) >= 2 and parts[0] == "private" and parts[1] == "files":
        return frappe.get_site_path("private", "files", "/".join(parts[2:]))
    if len(parts) >= 1 and parts[0] == "files":
        return frappe.get_site_path("public", "files", "/".join(parts[1:]))
    return None


def _is_probably_image_file(file_name: str, file_url: str) -> bool:
    name = (file_name or "").lower()
    url = (file_url or "").lower()
    guess = mimetypes.guess_type(name or url)[0] or ""
    if guess.startswith("image/"):
        return True
    return any((name.endswith(ext) or url.endswith(ext)) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"))


def _safe_read_file_bytes(file_url: str) -> Optional[bytes]:
    """Read bytes from disk if possible, else from HTTP(S). Returns None on failure."""
    url = (file_url or "").strip()
    if not url:
        return None

    # disk
    try:
        p = _file_url_to_path(url)
        if p and Path(p).exists():
            return Path(p).read_bytes()
    except Exception:
        pass

    # http(s)
    try:
        abs_url = url if url.lower().startswith(("http://", "https://")) else urljoin(get_url(), url)
        r = requests.get(abs_url, timeout=30)
        r.raise_for_status()
        return r.content
    except Exception:
        return None


def _sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _set_if_field_exists(doc, fieldname: str, value) -> None:
    try:
        if fieldname and doc.meta.has_field(fieldname):
            setattr(doc, fieldname, value)
    except Exception:
        pass


def _has_db_column(doctype: str, column: str) -> bool:
    try:
        return bool(frappe.db.has_column(doctype, column))
    except Exception:
        return False


def _is_treated_ai_file(file_doc) -> bool:
    try:
        if file_doc.meta.has_field("custom_treated_ai"):
            return int(getattr(file_doc, "custom_treated_ai") or 0) == 1
    except Exception:
        pass
    return False


def _pil_to_webp_square_bytes(im: Image.Image, *, size=(2000, 2000), quality=88) -> bytes:
    """Force final to a square white canvas and export WEBP."""
    im = im.convert("RGB")
    target_w, target_h = size
    scale = min(target_w / im.width, target_h / im.height)
    new_w, new_h = max(1, int(im.width * scale)), max(1, int(im.height * scale))
    im_resized = im.resize((new_w, new_h), Image.LANCZOS)

    canvas = Image.new("RGB", (target_w, target_h), (255, 255, 255))
    off_x = (target_w - new_w) // 2
    off_y = (target_h - new_h) // 2
    canvas.paste(im_resized, (off_x, off_y))

    out = io.BytesIO()
    canvas.save(out, "WEBP", quality=quality, method=6)
    return out.getvalue()


def _reencode_bytes_to_match_original(*, edited_bytes: bytes, original_file_name: str = "", original_file_url: str = "") -> bytes:
    """If we overwrite in-place, keep extension semantics (.jpg stays jpg, etc.)."""
    ext = (Path(original_file_name or original_file_url).suffix or "").lower().strip()
    if ext in {".webp", ".jpg", ".jpeg", ".png"}:
        try:
            im = Image.open(io.BytesIO(edited_bytes)).convert("RGB")
            out = io.BytesIO()
            if ext in {".jpg", ".jpeg"}:
                im.save(out, format="JPEG", quality=88, optimize=True)
                return out.getvalue()
            if ext == ".png":
                im.save(out, format="PNG", optimize=True)
                return out.getvalue()
            im.save(out, format="WEBP", quality=88, method=6)
            return out.getvalue()
        except Exception:
            return edited_bytes
    return edited_bytes


# ---------------------------------------------------------------------------
# ALT + Title generation (ALT garanti non vide)
# ---------------------------------------------------------------------------

def _derive_wp_title(item_name: str, file_doc) -> str:
    try:
        it = frappe.get_cached_doc("Item", item_name)
        title = (it.item_name or it.item_code or "").strip()
        if title:
            return title[:140]
    except Exception:
        pass
    stem = re.sub(r"\.[a-z0-9]+$", "", (getattr(file_doc, "file_name", "") or ""), flags=re.I).strip()
    return (stem or "Image produit")[:140]


def _fallback_alt_from_item(item_name: str, max_len: int = 120) -> str:
    try:
        it = frappe.get_cached_doc("Item", item_name)
        txt = (it.item_name or it.item_code or item_name or "").strip()
    except Exception:
        txt = (item_name or "").strip()
    txt = re.sub(r"\s+", " ", txt).strip()
    return (txt[:max_len] if txt else "Image produit")[:max_len]


import io
import re
import base64
from PIL import Image

def _openai_generate_alt_from_bytes(
    img_bytes: bytes,
    *,
    locale: str = "fr",
    base: str = "",
    principal_keyword: str = "",
    description: str = "",
    max_chars: int = 250,   # ✅ adjustable (SEO-friendly default)
) -> str:
    """
    SEO ALT (Vision):
    - Uses image as source of truth.
    - MUST include principal_keyword EXACTLY (verbatim substring).
    - Can use base + description for wording, but must not add invisible claims.
    - Returns plain text only (no quotes, no trailing punctuation).
    """
    principal_keyword = (principal_keyword or "").strip()
    base = (base or "").strip()
    description = (description or "").strip()
    
    try:
        client = _get_openai_client()
        model_name = "gpt-4o-mini"
        lang = "fr" if (locale or "").lower().startswith("fr") else "en"

        desc_short = re.sub(r"\s+", " ", description).strip()[:400] if description else ""

        # IMPORTANT: force exact keyword inclusion
        if principal_keyword:
            kw_template = (
            "Output format (follow strictly):\n"
            f'- Start the ALT with the exact phrase "{principal_keyword}", then continue description.\n'
        )

        prompt = (
            f"Write e-commerce image ALT text in {lang}.\n"
            f"Length: 60–{int(max_chars)} characters.\n"
            "Use the image as the source of truth.\n"
            "Describe what is clearly visible ONLY.\n"
            "Do not guess technical features not visible.\n"
            "No SKU. No model codes. No brand unless printed.\n"
            "Return ONLY the ALT text. No quotes. No trailing punctuation.\n\n"
            f"{kw_template}\n"
            "Context (may help naming, but do not invent details):\n"
            f"- Product base name: {base}\n"
            f"- Product description (text only): {desc_short}\n"
        )

        # Build data URL
        im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        data_url = f"data:image/jpeg;base64,{b64}"

        params = {
            "model": model_name,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
                ],
            }],
        }

        # GPT-5* models don't support max_tokens -> use max_completion_tokens
        if _is_gpt5(model_name):
            params["max_completion_tokens"] =200
        else:
            params["max_tokens"] = 200
            params["temperature"] = 0.0
        
        resp = client.chat.completions.create(**params)
        txt = (resp.choices[0].message.content or "").strip()
        txt = re.sub(r"\s+", " ", txt).strip()
        txt = txt.strip('"\'')
        
        # remove trailing punctuation
        txt = re.sub(r"[.!?。！？]+$", "", txt).strip()

        # ---- HARD ENFORCEMENT: guarantee keyword exact ----
        if principal_keyword:
            # ensure keyword appears at least once
            if principal_keyword not in txt:
                txt = f"{principal_keyword} {txt}".strip()

            # ensure keyword appears EXACTLY once (avoid stuffing)
            count = txt.count(principal_keyword)
            if count > 1:
                # keep first occurrence, remove extra duplicates
                parts = txt.split(principal_keyword)
                txt = principal_keyword + (" ".join(parts[1:]).replace(principal_keyword, "")).strip()
                txt = re.sub(r"\s+", " ", txt).strip()

        # enforce length while preserving keyword
        if len(txt) > max_chars:
            if principal_keyword and principal_keyword in txt:
                # keep keyword + as much as possible after it
                start = txt.find(principal_keyword)
                if start > 0:
                    # prefer making keyword near the beginning
                    txt = (principal_keyword + " " + txt.replace(principal_keyword, "", 1).strip()).strip()
                txt = txt[:max_chars].rstrip()
            else:
                txt = txt[:max_chars].rstrip()

        return txt

    except Exception:
        # last-resort fallback that still includes keyword
        if principal_keyword:
            fallback = f"{principal_keyword} {base}".strip()
            fallback = re.sub(r"\s+", " ", fallback).strip()
            return fallback[:max_chars]
        fallback = re.sub(r"\s+", " ", base).strip()
        return fallback[:max_chars]



def _gemini_generate_alt(client: genai.Client, image_url: str, locale: str = "fr") -> str:
    lang = "fr" if (locale or "").lower().startswith("fr") else "en"
    prompt = (
        f"Write concise, neutral e-commerce ALT text in {lang} (≤120 chars). "
        "Describe only what is clearly visible, no SKU/brand unless printed. "
        "Return ONLY the ALT text."
    )
    try:
        resp = client.models.generate_content(
            model=_read_gemini_model(purpose="alt"),
            contents=[
                prompt,
                genai_types.Part.from_uri(image_url, mime_type="image/*"),
            ],
            config=genai_types.GenerateContentConfig(temperature=0),
        )
        text = getattr(resp, "text", "") or ""
        return text.strip()[:120]
    except Exception:
        return ""


def _ensure_wp_meta_for_file(
    *,
    file_name: str,
    item_name: str,
    gemini_client: Optional[genai.Client],
    alt_locale: str = "fr",
) -> Dict[str, Any]:
    """
    Ensures:
      - File.custom_wp_title
      - File.custom_wp_alternative (ALT) is NEVER empty after this call.
    """
    fdoc = frappe.get_doc("File", file_name)

    # Title
    if fdoc.meta.has_field("custom_wp_title") and not (getattr(fdoc, "custom_wp_title", None) or "").strip():
        _set_if_field_exists(fdoc, "custom_wp_title", _derive_wp_title(item_name, fdoc))

    # ALT
    need_alt = fdoc.meta.has_field("custom_wp_alternative") and not (getattr(fdoc, "custom_wp_alternative", None) or "").strip()
    if need_alt:
        alt = ""

        # 1) OpenAI from bytes (works for private/local)
        b = _safe_read_file_bytes(fdoc.file_url or "")
        
        if b:
            alt = _openai_generate_alt_from_bytes(b, locale=alt_locale)

        # 2) Gemini from URL (only if public)
        if not alt:
            try:
                abs_url = (fdoc.file_url or "").strip()
                if abs_url and not abs_url.lower().startswith(("http://", "https://")):
                    abs_url = urljoin(get_url(), abs_url)

                if gemini_client and abs_url.lower().startswith(("http://", "https://")) and int(getattr(fdoc, "is_private", 0) or 0) == 0:
                    alt = _gemini_generate_alt(gemini_client, abs_url, alt_locale)
            except Exception:
                alt = ""

        # 3) Hard fallback (never empty)
        if not alt:
            alt = _fallback_alt_from_item(item_name)

        _set_if_field_exists(fdoc, "custom_wp_alternative", alt)

    fdoc.save(ignore_permissions=True)
    return {
        "updated": True,
        "custom_wp_title": getattr(fdoc, "custom_wp_title", None),
        "custom_wp_alternative": getattr(fdoc, "custom_wp_alternative", None),
    }


# ---------------------------------------------------------------------------
# Gemini image edit
# ---------------------------------------------------------------------------

EDIT_PROMPT = (
    """You receive one or multiple photos of the SAME product.
The product can be:
- A water tank / cistern / reservoir
- A valve / faucet
- A pipe, PVC/PE fitting, connector, or plumbing accessory

Your task:
Generate ONE single, high-quality, professional e-commerce packshot
of this exact product.

Reference usage:
- Carefully analyze the input photo(s).
- Reproduce the EXACT same product.
- Preserve 100% of the original:
  shape, geometry, proportions, capacity, ports, flanges,
  thread types, valve handle design, surface texture,
  colors, logos, labels, engravings, graduations and markings.
- If multiple images are provided, combine them to improve accuracy.
- Do NOT invent, redesign, simplify, or modify the product.

View and framing:
- Show the product in a clean 3/4 angled front view.
- The entire product must be fully visible (no cropped edges).
- Keep correct proportions (no distortion or stretching).
- Center the product.
- Use a square 1:1 format.
- The product should fill about 85–90% of the frame
  with small, even white margins around it.

Background and lighting:
- Pure white seamless studio background (#FFFFFF).
- Soft, professional studio lighting.
- Even illumination with no harsh reflections.
- Soft, realistic shadow underneath and slightly behind the product.
- No background texture.

Restrictions:
- Do NOT add extra elements.
- No environment, no room, no installation context.
- No people.
- No additional text or graphics.
- No watermarks.
- No decorative props.

Final result:
A sharp, photorealistic, studio-quality 3/4 front view product packshot,
accurately matching the reference product,
ready for use on a professional e-commerce website."""
)


def _gemini_edit_image_bytes(client: genai.Client, img_bytes: bytes) -> bytes:
    """
    Ask Gemini to clean/center the product image.
    Always returns WEBP square bytes (2000×2000).
    """
    base_im = Image.open(io.BytesIO(img_bytes)).convert("RGB")

    try:
        resp = client.models.generate_content(
            model=_read_gemini_model(purpose="image"),
            contents=[base_im, EDIT_PROMPT],
        )

        # Try extract inline image
        for cand in getattr(resp, "candidates", []) or []:
            content = getattr(cand, "content", None)
            if not content:
                continue
            for part in content.parts:
                if getattr(part, "inline_data", None) and getattr(part.inline_data, "mime_type", ""):
                    raw = part.inline_data.data
                    out_im = Image.open(io.BytesIO(raw))
                    return _pil_to_webp_square_bytes(out_im, size=(2000, 2000), quality=88)

        # fallback local
        return _pil_to_webp_square_bytes(base_im, size=(2000, 2000), quality=88)
    except Exception:
        return _pil_to_webp_square_bytes(base_im, size=(2000, 2000), quality=88)


# ---------------------------------------------------------------------------
# Dedup + relink + safe delete helpers (éviter 2 images)
# ---------------------------------------------------------------------------

def _get_item_attach_fields() -> List[str]:
    """All Item fields of type Attach/Attach Image/Image (dynamic)."""
    meta = frappe.get_meta("Item")
    out = []
    for df in (meta.fields or []):
        if df.fieldtype in ("Attach", "Attach Image", "Image") and df.fieldname:
            out.append(df.fieldname)

    # stable dedupe
    seen, res = set(), []
    for f in out:
        if f not in seen:
            seen.add(f)
            res.append(f)

    if "image" not in res:
        res.insert(0, "image")
    return res


def _relink_old_url_for_other_items_only(
    *,
    current_item: str,
    old_url: str,
    new_url: str,
    new_file_name: str = "",
    new_hash: str = "",
    new_size: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Replace references old_url -> new_url for:
      - Item attach/image fields (all Items except current_item)
      - File rows attached to Items except current_item
    """
    old_url = (old_url or "").strip()
    new_url = (new_url or "").strip()
    if not old_url or not new_url or old_url == new_url:
        return {"ok": True, "changed_item_fields": {}, "changed_files": 0}

    changed_item_fields = {}

    for fieldname in _get_item_attach_fields():
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", fieldname):
            continue

        frappe.db.sql(
            f"""
            UPDATE `tabItem`
            SET `{fieldname}` = %s
            WHERE `{fieldname}` = %s AND name != %s
            """,
            (new_url, old_url, current_item),
        )
        cnt = int(frappe.db.sql("SELECT ROW_COUNT()")[0][0] or 0)
        if cnt:
            changed_item_fields[fieldname] = cnt

    frappe.db.sql(
        """
        UPDATE `tabFile`
        SET file_url = %s
        WHERE file_url = %s
          AND attached_to_doctype = 'Item'
          AND attached_to_name != %s
        """,
        (new_url, old_url, current_item),
    )
    changed_files = int(frappe.db.sql("SELECT ROW_COUNT()")[0][0] or 0)

    if new_file_name:
        frappe.db.sql(
            """
            UPDATE `tabFile`
            SET file_name = %s
            WHERE file_url = %s
              AND attached_to_doctype = 'Item'
              AND attached_to_name != %s
            """,
            (new_file_name, new_url, current_item),
        )

    if new_hash and _has_db_column("File", "content_hash"):
        frappe.db.sql(
            """
            UPDATE `tabFile`
            SET content_hash = %s
            WHERE file_url = %s
              AND attached_to_doctype = 'Item'
              AND attached_to_name != %s
            """,
            (new_hash, new_url, current_item),
        )

    if (new_size is not None) and _has_db_column("File", "file_size"):
        frappe.db.sql(
            """
            UPDATE `tabFile`
            SET file_size = %s
            WHERE file_url = %s
              AND attached_to_doctype = 'Item'
              AND attached_to_name != %s
            """,
            (int(new_size), new_url, current_item),
        )

    if _has_db_column("File", "custom_treated_ai"):
        frappe.db.sql(
            """
            UPDATE `tabFile`
            SET custom_treated_ai = 1
            WHERE file_url = %s
              AND attached_to_doctype = 'Item'
              AND attached_to_name != %s
            """,
            (new_url, current_item),
        )

    return {"ok": True, "changed_item_fields": changed_item_fields, "changed_files": changed_files}


def _count_item_url_refs(file_url: str) -> int:
    url = (file_url or "").strip()
    if not url:
        return 0
    total = 0
    for fn in _get_item_attach_fields():
        try:
            total += frappe.db.count("Item", filters={fn: url})
        except Exception:
            pass
    return int(total)


def _safe_delete_file_if_unreferenced(file_docname: str) -> Dict[str, Any]:
    """
    Delete File doc only if:
      - no Item fields point to its file_url
      - no other File rows share the same file_url
    """
    try:
        fdoc = frappe.get_doc("File", file_docname)
        url = (fdoc.file_url or "").strip()
        if not url:
            return {"deleted": False, "reason": "empty url"}

        others = frappe.get_all("File", filters={"file_url": url, "name": ["!=", fdoc.name]}, pluck="name")
        if others:
            return {"deleted": False, "reason": "same url used by other File rows", "others": others[:5]}

        if _count_item_url_refs(url) > 0:
            return {"deleted": False, "reason": "still referenced by Item fields"}

        frappe.delete_doc("File", fdoc.name, ignore_permissions=True)
        frappe.db.commit()
        return {"deleted": True, "file": fdoc.name, "url": url}
    except Exception as e:
        return {"deleted": False, "reason": str(e)}


# ---------------------------------------------------------------------------
# Core: retouch one attachment (0 duplication + ALT assuré)
# ---------------------------------------------------------------------------

def _retouch_one_attachment(
    *,
    gemini_client: genai.Client,
    item_name: str,
    file_docname: str,
    make_public: bool = True,
    alt_locale: str = "fr",
    delete_old_if_replaced: bool = True,
) -> Dict[str, Any]:
    """
    Strategy:
      - If already treated: DO NOT touch bytes; ensure WP title+ALT.
      - Else:
          A) If file exists on disk: overwrite in-place (keeps 1 file forever).
          B) Else: create new File + relink everywhere + safe-delete old if unreferenced.
    """
    fdoc = frappe.get_doc("File", file_docname)
    old_url = (fdoc.file_url or "").strip()
    old_name = (getattr(fdoc, "file_name", "") or "").strip()
    
    if not _is_probably_image_file(old_name, old_url):
        return {"skipped": True, "reason": "not an image", "file": fdoc.name, "original_file_url": old_url, "processed_file_url": old_url}

    # already treated -> ensure title/alt only
    if _is_treated_ai_file(fdoc):
        meta = _ensure_wp_meta_for_file(file_name=fdoc.name, item_name=item_name, gemini_client=gemini_client, alt_locale=alt_locale)
        frappe.db.commit()
        return {
            "skipped": True,
            "reason": "already treated -> ensured title/alt only",
            "file": fdoc.name,
            "original_file_url": old_url,
            "processed_file_url": old_url,
            "custom_wp_title": meta.get("custom_wp_title"),
            "alt": meta.get("custom_wp_alternative"),
        }

    # read bytes
    raw = _safe_read_file_bytes(old_url)
    if not raw:
        return {"skipped": True, "reason": "cannot read bytes (disk+http failed)", "file": fdoc.name, "original_file_url": old_url, "processed_file_url": old_url}

    # edit via gemini
    edited_webp = _gemini_edit_image_bytes(gemini_client, raw)
    new_hash = _sha256_hex(edited_webp)
    new_size = len(edited_webp)

    # A) in-place overwrite if possible
    fs_path = _file_url_to_path(old_url)
    if fs_path and Path(fs_path).exists():
        try:
            edited_inplace = _reencode_bytes_to_match_original(
                edited_bytes=edited_webp,
                original_file_name=old_name,
                original_file_url=old_url,
            )
            Path(fs_path).write_bytes(edited_inplace)

            if fdoc.meta.has_field("custom_treated_ai"):
                fdoc.custom_treated_ai = 1
            if _has_db_column("File", "content_hash"):
                fdoc.content_hash = _sha256_hex(edited_inplace)
            if _has_db_column("File", "file_size"):
                fdoc.file_size = len(edited_inplace)

            fdoc.save(ignore_permissions=True)
            frappe.db.commit()

            meta = _ensure_wp_meta_for_file(file_name=fdoc.name, item_name=item_name, gemini_client=gemini_client, alt_locale=alt_locale)
            frappe.db.commit()

            return {
                "skipped": False,
                "mode": "in_place_overwrite",
                "file": fdoc.name,
                "original_file_url": old_url,
                "processed_file_url": old_url,
                "content_hash": _sha256_hex(edited_inplace),
                "file_size": len(edited_inplace),
                "custom_wp_title": meta.get("custom_wp_title"),
                "alt": meta.get("custom_wp_alternative"),
            }
        except Exception:
            frappe.log_error(frappe.get_traceback(), "in-place overwrite failed -> fallback to new file")

    # B) fallback: new file + relink + safe delete
    try:
        it = frappe.get_cached_doc("Item", item_name)
        base = (it.item_name or it.item_code or "product").strip()
    except Exception:
        base = item_name or "product"

    slug = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")[:60] or "product"

    new_file_name = f"{slug}-{new_hash[:8]}.webp"
    is_private = 0 if make_public else 1

    new_file_doc = save_file(
        new_file_name,
        edited_webp,
        "Item",
        item_name,
        is_private=is_private,
        decode=False,
    )
    new_fdoc = frappe.get_doc("File", new_file_doc) if isinstance(new_file_doc, str) else new_file_doc
    new_url = (new_fdoc.file_url or "").strip()

    if new_fdoc.meta.has_field("custom_treated_ai"):
        new_fdoc.custom_treated_ai = 1
    if _has_db_column("File", "content_hash"):
        new_fdoc.content_hash = new_hash
    if _has_db_column("File", "file_size"):
        new_fdoc.file_size = new_size

    new_fdoc.save(ignore_permissions=True)
    frappe.db.commit()

    meta = _ensure_wp_meta_for_file(file_name=new_fdoc.name, item_name=item_name, gemini_client=gemini_client, alt_locale=alt_locale)
    frappe.db.commit()

    # Update current item fields if they were pointing to old_url
    it_doc = frappe.get_doc("Item", item_name)
    changed_current_fields: List[str] = []
    for fieldname in _get_item_attach_fields():
        if it_doc.meta.has_field(fieldname) and (getattr(it_doc, fieldname, None) or "").strip() == old_url:
            setattr(it_doc, fieldname, new_url)
            changed_current_fields.append(fieldname)
    if changed_current_fields:
        it_doc.save(ignore_permissions=True)
        frappe.db.commit()

    # Relink other items/files
    relink_summary = _relink_old_url_for_other_items_only(
        current_item=item_name,
        old_url=old_url,
        new_url=new_url,
        new_file_name=new_file_name,
        new_hash=new_hash,
        new_size=new_size,
    )
    frappe.db.commit()

    delete_summary = {"deleted": False}
    if delete_old_if_replaced:
        delete_summary = _safe_delete_file_if_unreferenced(fdoc.name)

    return {
        "skipped": False,
        "mode": "new_file_and_relink",
        "file": fdoc.name,
        "new_file": new_fdoc.name,
        "original_file_url": old_url,
        "processed_file_url": new_url,
        "relink": relink_summary,
        "delete_old": delete_summary,
        "content_hash": new_hash,
        "file_size": new_size,
        "custom_wp_title": meta.get("custom_wp_title"),
        "alt": meta.get("custom_wp_alternative"),
    }


# ---------------------------------------------------------------------------
# Public API: retouch all item images (dedupe par file_url)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def retouch_item_images(
    item_name: str,
    max_images: int = 10,
    make_public: int = 1,
    set_website_image: int = 1,
    write_alt_to_field: str = "",
    alt_locale: str = "fr",
) -> Dict[str, Any]:
    """
    Retouches Item attachments safely while PRESERVING the original file ordering.

    Rules:
      - Pull all File rows attached to the Item, ordered by (is_private asc, creation asc)
      - Keep only image-like files
      - Dedupe by file_url (keep the first occurrence, preserve order)
      - If Item.image exists and matches one of the attached file_url -> process it FIRST
        (then append remaining files in original order)
      - Process up to max_images in that final order
      - Ordering is preserved EVEN if some images are skipped (already treated) or replaced
      - Main image behavior:
          * If it.image is already set: DO NOT override it
              - but if that URL was replaced -> update it.image to the new URL
          * If it.image is empty: set it to the first URL in the final ordered list (if set_website_image=1)
    """
    it = frappe.get_doc("Item", item_name)
    gemini_client = _get_gemini_client()

    rows = frappe.get_all(
        "File",
        filters={"attached_to_doctype": "Item", "attached_to_name": it.name},
        fields=["name", "file_url", "file_name", "is_private", "creation"],
        order_by="is_private asc, creation asc",
        limit_page_length=500,
    )
    
    # ---- helpers ----
    from urllib.parse import urlparse

    def _norm_url(u: str) -> str:
        u = (u or "").strip()
        if not u:
            return ""
        p = urlparse(u)
        # If absolute URL, compare by path; otherwise keep relative
        return (p.path or "").strip() if p.scheme else u

    def _move_item_image_first(files_: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        img = _norm_url(getattr(it, "image", "") or "")
        if not img:
            return files_

        idx = None
        for i, f in enumerate(files_):
            if _norm_url(f.get("file_url")) == img:
                idx = i
                break

        if idx is None:
            return files_  # it.image not among attachments -> keep order

        return [files_[idx]] + [f for j, f in enumerate(files_) if j != idx]

    # ---- keep only images + dedupe by file_url (preserve order) ----
    seen = set()
    files: List[Dict[str, Any]] = []

    for r in rows:
        if not _is_probably_image_file(r.get("file_name"), r.get("file_url")):
            continue

        u = (r.get("file_url") or "").strip()
        if not u:
            continue

        # dedupe by normalized url so absolute/relative doesn't create duplicates
        key = _norm_url(u)
        if not key or key in seen:
            continue

        seen.add(key)
        files.append(r)

    if not files:
        return {"ok": False, "item": it.name, "message": "No attached images found."}

    # Process it.image first if it's one of the attachments (does NOT change ERPNext main image)
    files = _move_item_image_first(files)

    # Apply max_images after final ordering
    files = files[: int(max_images)]

    results: List[Dict[str, Any]] = []
    ordered_final_urls: List[str] = []
    url_map: Dict[str, str] = {}  # original_norm_url -> final_url (after replace/overwrite)

    for r in files:
        out = _retouch_one_attachment(
            gemini_client=gemini_client,
            item_name=it.name,
            file_docname=r["name"],
            make_public=bool(int(make_public)),
            alt_locale=alt_locale,
            delete_old_if_replaced=True,
        )
        results.append(out)

        original_url = (out.get("original_file_url") or "").strip()
        final_url = (out.get("processed_file_url") or out.get("original_file_url") or "").strip()

        # IMPORTANT: preserve ordering regardless of skipped/replaced
        if final_url:
            ordered_final_urls.append(final_url)

        # map original -> final (use normalized original for matching current it.image)
        orig_key = _norm_url(original_url)
        if orig_key and final_url:
            url_map[orig_key] = final_url

    # ---- main image logic (conservative; no override) ----
    if int(set_website_image):
        current = (it.image or "").strip()

        if current:
            # If current main image was replaced, update it to the new URL
            mapped = url_map.get(_norm_url(current))
            if mapped and mapped != current:
                it.image = mapped
        else:
            # If no main image set, pick the first in final order
            if ordered_final_urls:
                it.image = ordered_final_urls[0]

        # optional: write an alt field from the first non-empty alt we produced
        if write_alt_to_field and it.meta.has_field(write_alt_to_field):
            alt = ""
            for x in results:
                candidate = (x.get("alt") or "").strip()
                if candidate:
                    alt = candidate
                    break
            if alt:
                setattr(it, write_alt_to_field, alt)

        it.save(ignore_permissions=True)
        frappe.db.commit()

    return {
        "ok": True,
        "item": it.name,
        "processed": results,
        "ordered_final_urls": ordered_final_urls,  # debug + later use for Woo ordering
    }



@frappe.whitelist()
def generate_website_contenant(item_name: str) -> Dict[str, Any]:
    """
    Generate website/SEO content + retouch images for a given Item.
    Only `item_name` is allowed as input; all other parameters are defined here.
    """
    # --- fixed settings (tweak here only) ---
    LANGUAGE = "fr"
    TONE = "professionnel"
    AUDIENCE = "grand public"

    # content generation
    UPDATE = 1
    OVERWRITE = 1
    USE_IMAGES = 1
    MAX_IMAGES = 3
    ENCODE_POLICY = "auto"
    IMAGE_DETAIL = "low"
    VISION_MODEL = ""               # fallback handled in enrich_... if empty
    WEB_MODEL = "gpt-4.1-mini"

    # image retouch
    MAX_IMAGES_RETOUCH = 10
    MAKE_PUBLIC = 1
    SET_WEBSITE_IMAGE = 1
    WRITE_ALT_TO_FIELD = ""         # e.g., "" if you have one
    ALT_LOCALE = "fr"


    # --- step 1: SEO/content generation (writes custom fields on Item) ---
    content_res = {}
    try:
        content_res = enrich_item_content_with_openai_search(
            item_name=item_name,
            language=LANGUAGE,
            tone=TONE,
            target_audience=AUDIENCE,
            update=UPDATE,
            overwrite=OVERWRITE,
            use_images=USE_IMAGES,
            max_images=MAX_IMAGES,
            encode_policy=ENCODE_POLICY,
            image_detail=IMAGE_DETAIL,
            vision_model=VISION_MODEL,
            web_model=WEB_MODEL,
        )
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "generate_website_contenant: content generation failed")
        content_res = {"ok": False, "error": str(e)}

    # --- step 2: image retouch / ALT / website image ---
    images_res = {}
    try:
        dedupe_item_images(item_name=item_name, dry_run=False, detach_missing=True)
        images_res=treat_left_item_images(item_name=item_name)
        # images_res = retouch_item_images(
        #     item_name=item_name,
        #     max_images=MAX_IMAGES_RETOUCH,
        #     make_public=MAKE_PUBLIC,
        #     set_website_image=SET_WEBSITE_IMAGE,
        #     write_alt_to_field=WRITE_ALT_TO_FIELD,
        #     alt_locale=ALT_LOCALE,
        # )
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "generate_website_contenant: image retouch failed")
        images_res = {"ok": False, "error": str(e)}

    return {
        "ok": bool(content_res.get("ok", True) and images_res.get("ok", True)),
        "item": item_name,
        "content": content_res,
        "images": images_res,
    }
# Treat image file
def _make_main_first(it, rows):
    # 1) prefer attached_to_field="image"
    for i, r in enumerate(rows):
        if r.get("attached_to_field") == "image":
            return [rows[i]] + [x for j, x in enumerate(rows) if j != i]
    # 2) else match file_url to it.image
    img = (it.image or "").split("?")[0]
    if img:
        for i, r in enumerate(rows):
            if (r.get("file_url") or "").split("?")[0] == img:
                return [rows[i]] + [x for j, x in enumerate(rows) if j != i]
    return rows
def _principal_keyword(it) -> str:
    txt = it.custom_seo_keywords
    parts = txt.split(",") if txt else []
    parts = [p.strip() for p in parts if p.strip()]
    return parts[0] if parts else ""

def _generate_wp_alt(image_bytes: bytes, it, locale: str = "fr") -> str:
    """
    Generate SEO alt text using:
      - image content (vision)
      - base (item_name/item_code)
      - principal keyword (MUST be included exactly)
      - item description
    """
    base = (it.item_name or it.item_code or it.name or "").strip()
    kw = _principal_keyword(it)  # must be exact keyword you want
    desc = (getattr(it, "description", None) or getattr(it, "website_description", None) or "").strip()
    alt = _openai_generate_alt_from_bytes(
        image_bytes,
        locale=locale,
        base=base,
        principal_keyword=kw,
        description=desc,
        max_chars=180,  # change if you want
    )

    # safety fallback if OpenAI fails
    if not (alt or "").strip():
        if locale == "fr":
            return f"{kw} {base}".strip()
        return f"{kw} {base}".strip()

    return alt

@frappe.whitelist()
def treat_left_item_images(item_name: str, max_images: int = 10, preserve_order: int = 1):
    """
    Step 2 (after dedupe):
      - for each attached image File where custom_treated_ai == 0:
          * treat with Gemini
          * attach NEW treated file to the item
          * detach OLD file from the item
          * (optional) copy creation timestamp so it stays "in its place"
    """
    it = frappe.get_doc("Item", item_name)
    gemini_client = _get_gemini_client()
    rows = frappe.get_all(
        "File",
        filters={"attached_to_doctype": "Item", "attached_to_name": it.name},
        fields=[
            "name", "file_url", "file_name", "is_private", "creation", "attached_to_field",
            "custom_treated_ai", "custom_wp_title", "custom_wp_alternative"
        ],
        order_by="is_private asc, creation asc",
        limit_page_length=500,
    )

    # keep only images
    rows = [r for r in rows if _is_image_url( r.get("file_url"))]
    if not rows:
        return {"ok": False, "item": it.name, "message": "No image files attached."}

    # first must be main image
    rows = _make_main_first(it, rows)
    rows = rows[: int(max_images)]

    actions = []
    i=0
    for r in rows:
        i+=1
        old_id = r["name"]
        old_doc = frappe.get_doc("File", old_id)
            # ✅ Always ensure WP fields exist (independent from treated)
        if hasattr(old_doc, "custom_wp_title") and not (old_doc.custom_wp_title or "").strip():
            old_doc.custom_wp_title = f"{it.name}_{i}"  # or item_code-i

        if hasattr(old_doc, "custom_wp_alternative") and not (old_doc.custom_wp_alternative or "").strip():
            try:
                img_bytes_for_alt = old_doc.get_content()
                old_doc.custom_wp_alternative = _generate_wp_alt(img_bytes_for_alt, it)
            except Exception:
                # keep empty or put fallback
                old_doc.custom_wp_alternative = (it.item_name or it.name or "").strip()

        old_doc.save(ignore_permissions=True)

        treated = int(getattr(old_doc, "custom_treated_ai", 0) or 0)
        if treated == 1:
            continue

        # read bytes from old file
        old_bytes = old_doc.get_content()

        # treat with gemini
        new_bytes = _gemini_edit_image_bytes(gemini_client, old_bytes)
        # new_bytes, ext = gemini_treat_image_bytes(gemini_client, old_bytes)
        ext = ".jpg"

        # create NEW treated file attached to same item
        new_name = f"{it.name}_{str(i)}{ext}"
        new_doc = save_file(
            fname=new_name,
            content=new_bytes,
            dt="Item",
            dn=it.name,
            is_private=int(old_doc.is_private or 0),
        )

        # keep same attached_to_field (important for main image file)
        if getattr(old_doc, "attached_to_field", None):
            new_doc.attached_to_field = old_doc.attached_to_field
            new_doc.save(ignore_permissions=True)

        # if old one was the 'image' field, update item.image to new url
        if getattr(old_doc, "attached_to_field", None) == "image":
            it.image = new_doc.file_url

        # copy WP fields if you already set them on old file
        for fld in ("custom_wp_title", "custom_wp_alternative"):
            if hasattr(old_doc, fld) and hasattr(new_doc, fld):
                if not (getattr(new_doc, fld, "") or "").strip():
                    val = (getattr(old_doc, fld, "") or "").strip()
                    if val:
                        setattr(new_doc, fld, val)

        # mark new treated flag (on new file)
        if hasattr(new_doc, "custom_treated_ai"):
            new_doc.custom_treated_ai = 1
        new_doc.save(ignore_permissions=True)

        # OPTIONAL: keep ordering slot by copying creation timestamp
        if int(preserve_order):
            frappe.db.sql(
                "UPDATE `tabFile` SET creation=%s WHERE name=%s",
                (old_doc.creation, new_doc.name),
            )

        # detach the OLD file from item (only unlink)
        _detach_file_doc(old_id)

        actions.append({
            "old_file": old_id,
            "new_file": new_doc.name,
            "old_url": old_doc.file_url,
            "new_url": new_doc.file_url,
            "field": getattr(old_doc, "attached_to_field", None),
        })

    it.save(ignore_permissions=True)
    frappe.db.commit()

    return {"ok": True, "item": it.name, "treated_count": len(actions), "actions": actions}

def estimate_weight_and_delivery_class(
    openai_client,
    model: str,
    images: List[str],
    context: List[str],
) -> Dict[str, Any]:
    """
    Estimates packaged weight (kg) and whether the product is volumineux (bulky),
    using product description + up to 3 images.

    Parameters:
    -----------
    openai_client:
        OpenAI client instance.
    model: str
        Vision-capable model (e.g., gpt-4o, gpt-4.1, gpt-5...).
    images: List[str]
        Image URLs (prefer 1-3 clear images). If empty, model uses text only.
    context: List[str]
        [product_name, short_description] (description can include specs/dimensions/keywords).

    Returns:
    --------
    result: Dict[str, Any]
        STRICT JSON:
        {
          "weight_kg": number|null,
          "is_volumineux": true|false|null,
          "delivery_class": "normal"|"lourd"|"volumineux",
          "confidence": "high"|"medium"|"low",
          "reasons": [..],
          "missing_info": [..],
          "sources": [image_urls..]
        }
    """

    product_name = context[0] if context else ""
    short_description = context[1] if len(context) > 1 else ""
    description = context[2] if len(context) > 2 else ""

    # Build user message parts
    user_payload = {
        "task": (
            "Estimate packaged weight (kg) and whether the product is bulky/volumineux. "
            "Use both the text context and the images. "
            "Use images mainly to detect bulky/oversized packaging or any visible labels (weight/dimensions)."
            "If no information given use your knowledge"
        ),
        "rules": [
            "Output ONLY valid JSON (no markdown).",
            "Keys MUST be: weight_kg, is_volumineux",
        ],
        "context_fields": {
            "product_name": product_name,
            "short_description": short_description,
            "description": description
        },
        "schema_example": {
            "weight_kg": None,
            "is_volumineux": None
        }
    }

    user_content = [{"type": "text", "text": json.dumps(user_payload, ensure_ascii=False)}]

    # Attach up to 3 images
    for u in (images or [])[:3]:
        user_content.append({"type": "image_url", "image_url": {"url": u, "detail": "high"}})

    params = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
    "You are a logistics estimator for e-commerce products.\n"
    "Your task is to infer packaged weight (kg) and whether the item is bulky (volumineux) "
    "from the provided text and images.\n\n"
    "PRIORITIES:\n"
    "1) If a weight/dimensions label is visible, use it.\n"
    "2) If the text contains weight/dimensions/specs, use them.\n"
    "3) If neither exists, be conservative: only estimate weight if you can infer it reasonably from product type;\n\n"
    "VOLMINEUX GUIDANCE:\n"
    "- Set is_volumineux = true only when there are clear cues of oversized/bulky packaging or special handling: "
    "large tanks/FRP vessels, large housings, skid-mounted industrial units, big cabinets, pallet-sized packages.\n"
    "- Set is_volumineux = false for standard small/medium items: cartridges (PP/UDF/CTO), membranes, faucets, UV lamps, fittings, "
    "and typical domestic/commercial RO units unless there is clear evidence of bulky packaging.\n\n"

    "OUTPUT:\n"
    "- Return STRICT JSON only. No markdown, no extra text.\n"
),
            },
            {"role": "user", "content": user_content},
        ],
    }

    # keep deterministic for non gpt-5 models
    if not str(model).lower().startswith("gpt-5"):
        params["temperature"] = 0.01

    res = openai_client.chat.completions.create(**params)

    # Parse JSON safely
    try:
        raw = res.choices[0].message.content or "{}"
        result = json.loads(raw)
    except Exception:
        result = {}

    # Ensure sources
    result = result or {}
    
    return result
