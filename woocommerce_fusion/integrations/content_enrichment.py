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
import pdb
from xml.parsers.expat import model
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
        return cls._read_setting("openai_api_key") or frappe.conf.get("openai_api_key")

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
            # Save alternatives to custom_woocommerce_categories
        if it.meta.has_field("custom_woocomerce_categories"):
            # Join alternatives with comma
            it.custom_woocomerce_categories = ", ".join(alts) if alts else ""
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
            "short_desc": "plain text, <= 400 chars",
            "long_html": "HTML with <h2>/<ul>/<p>, <= 1200 words. Facts only.",
            "product_weight": "string?, e.g. '2.5' always in kg"
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
    "- variants (dict: attribute -> list of values; may be empty)\n"
    "- variant_intro (short, preformatted one-liner built from `variants`; may be empty)\n"

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
        '  "product_weight": "estimation of the product string in kg, e.g., \\"0.3\\"",\n'
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
            '  "product_weight": "estimation of the product string in kg, e.g., \"0.3\"",\n'
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
        "   <h2>Étapes de Filtration</h2>"
        "     • this is a reverse osmosis system, describe each stage (e.g., PP, UDF, CTO, RO, post-carbon, remineralization) using a <ul>\n" 
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
    product_weight = (data.get("product_weight") or "")
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
        if it.meta.has_field("custom_seo_keywords"):
            max_len = _field_max_len(it, "custom_seo_keywords") or 140
            kw_text = _join_keywords_to_fit(keywords, max_len)
            put("custom_seo_keywords", kw_text)

        # optionnel: garder un JSON d'audit
        if it.meta.has_field("custom_enrichment_json"):
            put("custom_enrichment_json", json.dumps({
                "identity": identity, "sources": web.get("sources") or [], "keywords": keywords
            }, ensure_ascii=False))

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
    model: str = "gpt-4o-mini",
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
    temperature = 0.2
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
    model: str = "gpt-4o-mini",
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
    temperature = 0.2
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
# Settings helpers
# ---------------------------------------------------------------------------




def _get_gemini_client() -> genai.Client:
    doc = frappe.get_single("AI settings")
    api_key = doc.get("gemini_api_key")
    if not api_key:
        frappe.throw("google_api_key missing in 'AI settings' or site_config.")
    return genai.Client(api_key=api_key)


# ---------------------------------------------------------------------------
# File / image utils
# ---------------------------------------------------------------------------

def _file_url_to_path(file_url: str) -> Optional[str]:
    s = (file_url or "").strip("/")
    parts = s.split("/")
    if len(parts) >= 2 and parts[0] == "private" and parts[1] == "files":
        return frappe.get_site_path("private", "files", "/".join(parts[2:]))
    if len(parts) >= 1 and parts[0] == "files":
        return frappe.get_site_path("public", "files", "/".join(parts[1:]))
    return None


def _bytes_from_file_record(frow: Dict[str, Any]) -> bytes:
    url = (frow.get("file_url") or "").strip()
    if not url:
        raise ValueError("Fichier sans file_url")
    p = _file_url_to_path(url)
    if p and Path(p).exists():
        return Path(p).read_bytes()
    abs_url = url
    if not abs_url.lower().startswith(("http://", "https://")):
        abs_url = urljoin(get_url(), url)
    r = requests.get(abs_url, timeout=30)
    r.raise_for_status()
    return r.content


def _pil_to_webp_bytes(im: Image.Image, *, size=(2000, 2000), quality=88) -> bytes:
    """
    Fit image into size, pad to square on white, return WEBP bytes.
    """
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


# ---------------------------------------------------------------------------
# Gemini image edit + alt text
# ---------------------------------------------------------------------------

EDIT_PROMPT = (
    "E-commerce product image edit with STRICT centering and sizing requirements:\n\n"
    
    "BACKGROUND & CLEANUP:\n"
    "- Background: remove completely and replace with pure white (#FFFFFF); absolutely no gradients, textures, or shadows on background.\n"
    "- Remove overlays: erase any non-product text/graphics such as phone numbers, URLs, emails, QR codes, stickers, badges, watermarks, price tags, or icons. PRESERVE product labels/logos that are part of the product itself.\n\n"
    
    "CANVAS & OUTPUT:\n"
    "- Canvas: exactly 1024×1024 pixels (perfect square).\n"
    "- Output: high-quality WebP format on pure white background.\n\n"
    
    "CRITICAL SIZING & CENTERING (FOLLOW PRECISELY):\n"
    "1. MEASURE the product's bounding box (width and height) EXCLUDING any shadow you will add.\n"
    "2. CALCULATE the maximum scale factor: min(950/product_width, 950/product_height) - this ensures the product fits in ~93% of canvas.\n"
    "3. SCALE the product using this factor - it should occupy 900-950 pixels on its largest dimension.\n"
    "4. CENTER EXACTLY: place the scaled product at coordinates (512, 512) - the absolute center of the 1024×1024 canvas.\n"
    "5. VERIFY centering: equal white space on all sides (35-60 pixels margin).\n\n"
    
    "SHADOW (AFTER CENTERING):\n"
    "- Add a subtle, soft drop shadow UNDER the product only (10-15% opacity, natural blur).\n"
    "- Shadow must stay within canvas bounds and not affect product positioning.\n"
    "- Shadow should be directly beneath the product, slightly offset downward.\n\n"
    
    "QUALITY REQUIREMENTS:\n"
    "- Preserve product exactly as-is: same colors, textures, shape, proportions, and all genuine product labels.\n"
    "- Apply light sharpening and denoising.\n"
    "- Clean cutout edges with no halos or artifacts.\n"
    "- Neutral, true-to-life colors - avoid oversaturation.\n\n"
    
    "PROHIBITIONS:\n"
    "- NO stretching or aspect ratio distortion.\n"
    "- NO added text, graphics, borders, watermarks, or props.\n"
    "- NO reflections or mirror effects.\n"
    "- NO off-center positioning - must be perfectly centered.\n\n"
    
    "VALIDATION CHECKLIST:\n"
    "✓ Product is scaled large (900-950px on longest side)\n"
    "✓ Product is perfectly centered at (512, 512)\n"
    "✓ Equal margins on all sides (35-60px)\n"
    "✓ Pure white background everywhere\n"
    "✓ Subtle shadow beneath product only\n"
    "✓ Clean, professional e-commerce appearance"
)


def _gemini_edit_image_bytes(client: genai.Client, img_bytes: bytes) -> bytes:
    """
    Use Gemini to edit the image per EDIT_PROMPT.
    Returns PNG/WEBP bytes directly from the model if provided,
    else falls back to local square white canvas.
    """
    try:
        # Load the base image for the request
        base_im = Image.open(io.BytesIO(img_bytes)).convert("RGB")

        # google-genai SDK accepts PIL Image directly in contents
        resp = client.models.generate_content(
            model="gemini-2.5-flash-image-preview",
            contents=[base_im, EDIT_PROMPT],
        )

        # Extract the first inline image from the response
        for cand in getattr(resp, "candidates", []) or []:
            parts = getattr(cand, "content", None)
            if not parts:
                continue
            for part in parts.parts:
                if getattr(part, "inline_data", None) and getattr(part.inline_data, "mime_type", ""):
                    raw = part.inline_data.data
                    out = Image.open(io.BytesIO(raw))
                    # Enforce 2000×2000 WEBP final (even if model already did)
                    return _pil_to_webp_bytes(out, size=(2000, 2000), quality=88)

        # If nothing extracted, fallback
        return _pil_to_webp_bytes(base_im)

    except Exception:
        # Safety fallback: just square+pad locally
        im = Image.open(io.BytesIO(img_bytes))
        return _pil_to_webp_bytes(im)


def _gemini_generate_alt(client: genai.Client, image_url: str, locale: str = "fr") -> str:
    lang = "fr" if (locale or "").lower().startswith("fr") else "en"
    prompt = (
        f"Write concise, neutral e-commerce ALT text in {lang} (≤120 chars). "
        "Describe only what is clearly visible, no SKU/brand unless printed."
    )
    try:
        resp = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=[
                prompt,
                # URL reference: the SDK can accept string URL parts
                genai_types.Part.from_uri(image_url, mime_type="image/*"),
            ],
            config=genai_types.GenerateContentConfig(temperature=0),
        )
        text = getattr(resp, "text", "") or ""
        return text.strip()[:120]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Core: process Item attachments and save new Files
# ---------------------------------------------------------------------------

def _generate_content_based_filename(img_bytes: bytes, item_name: str) -> str:
    """
    Analyze image content to generate a descriptive, SEO-friendly filename using OpenAI.
    """
    try:
        # Get OpenAI client
        api_key = ItemGroupClassifier._read_openai_key()
        if not api_key:
            raise ValueError("OpenAI API key not found")
        
        client = OpenAI(api_key=api_key)
        
        # Convert image bytes to base64 data URL
        image = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img_buffer = io.BytesIO()
        image.save(img_buffer, format='JPEG', quality=85)
        img_base64 = base64.b64encode(img_buffer.getvalue()).decode('utf-8')
        data_url = f"data:image/jpeg;base64,{img_base64}"
        
        prompt = (
            "Analyze this product image and generate a short, descriptive filename (2-4 words max). "
            "Focus on the main product, its type, color, or key feature. "
            "Use only lowercase letters, numbers, and hyphens. "
            "Examples: 'blue-ceramic-mug', 'steel-kitchen-knife', 'red-leather-bag'. "
            "Return ONLY the filename, no explanation."
        )
        # params = {
        #     "model": "gpt-4o-mini",
        #     "response_format": {"type": "json_object"},
        #     "max_tokens": 50,
        #     "messages": [{"role": "user", "content": prompt}],
        # }
        # if not str(model).lower().startswith("gpt-5"):
        #     params["temperature"] = 0.01
        response = client.chat.completions.create(
            model="gpt-4o-mini",  # or "gpt-4-vision-preview"
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}}
                    ]
                }
            ],
            max_tokens=50,
            temperature=0
        )
        
        content_desc = response.choices[0].message.content.strip()
        
        # Clean and validate the generated description
        clean_desc = re.sub(r'[^a-z0-9\-\s]', '', content_desc.lower().strip())
        clean_desc = re.sub(r'\s+', '-', clean_desc)
        clean_desc = re.sub(r'-+', '-', clean_desc).strip('-')
        
        # Fallback if description is too short or invalid
        if len(clean_desc) < 3:
            raise ValueError("Generated description too short")
            
        return clean_desc[:50]  # Max 50 chars
        
    except Exception:
        # Fallback to item-based naming
        item = frappe.get_cached_doc("Item", item_name)
        base_name = item.item_name or item.item_code or "product"
        return _slugify(base_name)

def _retouch_one_attachment(
    client: genai.Client,
    item_name: str,
    frow: Dict[str, Any],
    *,
    make_public: bool = True,
    alt_locale: str = "fr",
) -> Dict[str, Any]:
    # Check if this file is already AI-generated (skip processing)
    original_filename = frow.get("file_name", "")
    if "ai-gen" in original_filename.lower():
        return {
            "original_file_url": frow.get("file_url"),
            "original_file_name": original_filename,
            "processed_file_url": frow.get("file_url"),
            "processed_file_name": original_filename,
            "content_description": "skipped-ai-gen",
            "alt": "",
            "is_private": frow.get("is_private", 0),
            "original_deleted": False,
            "skipped": True,
            "reason": "Already AI-generated"
        }
    
    raw = _bytes_from_file_record(frow)
    
    # Generate content-based filename BEFORE editing
    content_filename = _generate_content_based_filename(raw, item_name)
    
    # Edit the image
    edited_webp = _gemini_edit_image_bytes(client, raw)

    # Add timestamp and AI-gen marker to ensure uniqueness and identification
    # Option 1: Last 5 digits of timestamp
    timestamp = random.randint(0, 999)
    out_name = f"{content_filename}-ai-gen-{timestamp:03d}.webp"

    # Save as attached File (WEBP)
    fdoc = save_file(
        out_name,
        edited_webp,
        "Item",
        item_name,
        is_private=0 if make_public else 1,
    )

    # Delete the original file
    try:
        original_file_doc = frappe.get_doc("File", frow["name"])
        original_file_doc.delete()
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(f"Failed to delete original file {frow['name']}: {str(e)}")

    # Build absolute URL for ALT text generation (if public)
    abs_url = fdoc.file_url
    if abs_url and not abs_url.lower().startswith(("http://", "https://")):
        abs_url = urljoin(get_url(), abs_url)

    alt = ""
    try:
        if make_public and abs_url.lower().startswith(("http://", "https://")):
            alt = _gemini_generate_alt(client, abs_url, alt_locale)
    except Exception:
        alt = ""

    return {
        "original_file_url": frow.get("file_url"),
        "original_file_name": frow.get("file_name"),
        "processed_file_url": fdoc.file_url,
        "processed_file_name": fdoc.file_name,
        "content_description": content_filename,
        "alt": alt,
        "is_private": fdoc.is_private,
        "original_deleted": True,
        "skipped": False,
    }

@frappe.whitelist()
def retouch_item_images(
    item_name: str,
    max_images: int = 1,
    make_public: int = 1,
    set_website_image: int = 1,
    write_alt_to_field: str = "",
    alt_locale: str = "fr",
) -> Dict[str, Any]:
    """
    Retouch up to `max_images` attached images for an Item and save them as NEW files.
    Optionally set Item.image with the first processed image, and store ALT text.
    """
    it = frappe.get_doc("Item", item_name)
    client = _get_gemini_client()

    # Filter out AI-generated files from the query
    files = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": "Item", 
            "attached_to_name": it.name,
            "file_name": ["not like", "%ai-gen%"]  # Exclude AI-generated files
        },
        fields=["name", "file_url", "file_name", "is_private", "creation"],
        order_by="is_private asc, creation asc",
        limit_page_length=100,
    )
    
    if not files:
        return {"ok": False, "item": it.name, "message": "No non-AI-generated attached images found."}

    results: List[Dict[str, Any]] = []
    processed_urls: List[str] = []

    for frow in files:
        if len(results) >= int(max_images):
            break
        try:
            out = _retouch_one_attachment(
                client=client,
                item_name=it.name,
                frow=frow,
                make_public=bool(int(make_public)),
                alt_locale=alt_locale,
            )
            results.append(out)
            if out.get("processed_file_url") and not out.get("skipped"):
                processed_urls.append(out["processed_file_url"])
        except Exception as e:
            results.append({"original_file_url": frow.get("file_url"), "error": str(e)})

    if processed_urls and int(set_website_image):
        first_url = processed_urls[0]
        it.image = first_url
        if write_alt_to_field and it.meta.has_field(write_alt_to_field):
            alt = next((r.get("alt") for r in results if r.get("alt")), "")
            if alt:
                setattr(it, write_alt_to_field, alt)
        it.save(ignore_permissions=True)
        frappe.db.commit()

    return {"ok": True, "item": it.name, "processed": results}

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
    WRITE_ALT_TO_FIELD = ""         # e.g., "custom_image_alt" if you have one
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
        images_res = retouch_item_images(
            item_name=item_name,
            max_images=MAX_IMAGES_RETOUCH,
            make_public=MAKE_PUBLIC,
            set_website_image=SET_WEBSITE_IMAGE,
            write_alt_to_field=WRITE_ALT_TO_FIELD,
            alt_locale=ALT_LOCALE,
        )
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "generate_website_contenant: image retouch failed")
        images_res = {"ok": False, "error": str(e)}

    return {
        "ok": bool(content_res.get("ok", True) and images_res.get("ok", True)),
        "item": item_name,
        "content": content_res,
        "images": images_res,
    }