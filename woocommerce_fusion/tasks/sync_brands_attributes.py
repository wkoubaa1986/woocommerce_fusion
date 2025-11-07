from dataclasses import dataclass
from typing import Dict, List, Optional, Any
import os
from erpnext.setup.doctype.brand import brand
import frappe
from frappe import _
from time import sleep
import requests
from requests.auth import HTTPBasicAuth
import pdb
import re, unicodedata
from woocommerce_fusion.tasks.sync import SynchroniseWooCommerce
from woocommerce_fusion.woocommerce.doctype.woocommerce_server.woocommerce_server import WooCommerceServer
from woocommerce_fusion.integrations.content_enrichment import generate_brand_seo_minimal
from woocommerce_fusion.tasks.utils import APIWithRequestLogging


def _slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii","ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "brand"

def _trim(s: str, n: int) -> str:
    s = (s or "").strip()
    return (s[:n-1] + "…") if len(s) > n else s

def _resolve_company_name(explicit: str | None = None) -> str | None:
    if explicit and explicit.strip():
        return explicit.strip()
    # user default → global default → any company
    return (
        frappe.db.get_default("company")
        or frappe.db.get_single_value("Global Defaults", "default_company")
        or (frappe.get_all("Company", pluck="name", limit=1) or [None])[0]
    )
@dataclass
class WPConfig:
    base_url: str
    username: str
    app_password: str
    verify_ssl: bool = True


def _get_wp_config() -> WPConfig:
    """Get WordPress configuration from settings, site config, or environment variables"""
    base_url = username = app_pw = None
    verify_ssl = True

    try:
        if frappe.db.exists("DocType", "WooCommerce Fusion Settings"):
            s = frappe.get_doc("WooCommerce Fusion Settings")
            base_url = (s.get("wordpress_url") or "").strip() or None
            username = (s.get("wordpress_user") or "").strip() or None
            app_pw = (s.get("wordpress_application_password") or "").strip() or None
            if s.get("verify_ssl_certificates") is not None:
                verify_ssl = bool(s.get("verify_ssl_certificates"))
    except Exception:
        pass

    if not (base_url and username and app_pw):
        frappe.throw(
            _("WordPress REST configuration missing. Fill it in WooCommerce Fusion Settings "
              "or set WORDPRESS_URL, WORDPRESS_USER and WORDPRESS_APP_PASSWORD.")
        )

    return WPConfig(base_url=base_url.rstrip("/"), username=username, app_password=app_pw, verify_ssl=verify_ssl)


class SynchroniseBrandsAttributes(SynchroniseWooCommerce):
    """Synchronize ERPNext Brands and Attributes to WooCommerce (one-way only)"""

    def __init__(self, servers: List[WooCommerceServer] = None) -> None:
        super().__init__(servers)
        self.wc_server = servers
        self.wp_config = None
        self.wc_api = None

    def run(self) -> None:
        """Run synchronization for all enabled servers"""
        # Get WordPress configuration once
        self.wp_config = _get_wp_config()
        
        for server in self.servers:
            self.wc_server = server
            if server.enable_sync:
                frappe.logger().info(f"Syncing to {server.name}")
                self.sync_brands()
                self.sync_attributes()

    # ==================== BRANDS ====================

    def sync_brands(self) -> None:
        """Sync all ERPNext brands to WooCommerce"""
        brands = frappe.get_all("Brand", filters={"disabled": 0}, fields=["name", "description"])
        
        for brand in brands:
            try:
                self._sync_brand(brand)
                sleep(0.5)
            except Exception as e:
                frappe.log_error(f"Brand Sync: {brand['name']}", frappe.get_traceback())

    def _sync_brand(self, brand_name: str, company: str | None = None) -> None:
        """Sync single brand to WooCommerce via WordPress REST API"""
        # Get existing WooCommerce ID if any
        wc_id = frappe.db.get_value("Brand", {"name": brand_name, "custom_woocomerce_server": self.wc_server[0].name}, "custom_woocomerce_id")
        brand = frappe.get_doc("Brand", brand_name)
        if frappe.utils.cint(brand.custom_generate_seo) == 1:
            generate_brand_seo_minimal(brand_name=brand.name)
            brand = frappe.get_doc("Brand", brand_name)
        
         # Prepare SEO fields
        name = brand.get("name", "").strip()
        desc = (brand.get("description") or "").strip()

            # 1) SEO fields from ERPNext
        seo_title = (brand.get("custom_seo_title")
                    or brand.get("seo_title")
                    or name).strip()

        seo_kw = (brand.get("custom_seo_keyword")
                or brand.get("custom_seo_keywords")
                or brand.get("seo_keyword")
                or "").strip()

        # 2) Ajustements Rank Math (seulement à partir de ces 3 champs)
        # - Title ≤ 60 + suffixe société si ça rentre
        company_name = _resolve_company_name(company)

        # Title ≤ 60; append " | {company}" only if it fits
        if company_name and len(seo_title) + 3 + len(company_name) <= 60:
            seo_title_final = f"{seo_title} | {company_name}"
        else:
            seo_title_final = _trim(seo_title, 60)

        # - Meta description = résumé de la description (≤ 160). Fallback = title.
        meta_desc_src = " ".join(desc.split()) or seo_title_final
        meta_desc = _trim(meta_desc_src, 160)

        # 3) Payload WP (⚠️ route: wp/v2/product_brand/{id}?context=edit)
        payload = {
            "name": name,
            "slug": _slugify(name),
            "description": desc,
            "parent": 0,
            "meta": {
                "rank_math_title": seo_title_final,
                "rank_math_description": meta_desc,
                "rank_math_focus_keyword": seo_kw,
                # on réutilise title/desc pour social
                "rank_math_facebook_title": seo_title_final,
                "rank_math_facebook_description": meta_desc,
                "rank_math_twitter_title": seo_title_final,
                "rank_math_twitter_description": meta_desc,
            }
        }
        
        
        try:
            if wc_id:
                if not(brand.custom_last_woocomerce_synchro) or brand.custom_last_woocomerce_synchro != brand.custom_last_seo_generated:
                    # Update existing brand
                    endpoint = f"{self.wp_config.base_url}/wp-json/wp/v2/product_brand/{wc_id}"
                    response = self._make_wp_request("PUT", endpoint, payload)
                    frappe.db.set_value("Brand", brand.name, "custom_last_woocomerce_synchro", brand.custom_last_seo_generated, update_modified=False)
                    frappe.db.commit()
                    frappe.logger().info(f"Updated brand: {brand.name}")
            else:
                # Create new brand
                endpoint = f"{self.wp_config.base_url}/wp-json/wp/v2/product_brand"
                response = self._make_wp_request("POST", endpoint, payload)
                wc_id = response.get("id")
                brand.custom_woocomerce_id = wc_id
                brand.custom_woocomerce_server = self.wc_server[0].name
                brand.flags.ignore_permissions = True
                brand.save()
                frappe.db.set_value("Brand", brand.name, "custom_last_woocomerce_synchro", brand.custom_last_seo_generated, update_modified=False)

                frappe.db.commit()


            frappe.logger().info(f"Synced brand: {brand.name}")

        except Exception as e:
            frappe.log_error(f"Brand Sync Error: {brand.name}", str(e))
            raise

    def _make_wp_request(self, method: str, url: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Make authenticated request to WordPress REST API"""
        auth = HTTPBasicAuth(self.wp_config.username, self.wp_config.app_password)
        
        headers = {
            "Content-Type": "application/json"
        }
        
        # Make request
        if method.upper() == "POST":
            response = requests.post(
                url, 
                json=data, 
                auth=auth, 
                headers=headers, 
                verify=self.wp_config.verify_ssl
            )
        elif method.upper() == "PUT":
            response = requests.put(
                url, 
                json=data, 
                auth=auth, 
                headers=headers, 
                verify=self.wp_config.verify_ssl
            )
        elif method.upper() == "GET":
            response = requests.get(
                url, 
                auth=auth, 
                headers=headers, 
                verify=self.wp_config.verify_ssl
            )
        else:
            frappe.throw(f"Unsupported HTTP method: {method}")
        
        # Check for errors
        if response.status_code not in [200, 201]:
            error_data = response.json() if response.content else {}
            error_msg = error_data.get("message", response.text)
            frappe.throw(f"WordPress API Error ({response.status_code}): {error_msg}")
        
        return response.json()

    # ==================== ATTRIBUTES ====================

    def _sync_udm_attribute(self, attr_doc) -> None:
        """Sync 'UdM' attribute - create if missing, check and add values"""
    
        # Step 1: Get or create the WooCommerce attribute

        wc_attr_id = self._get_or_create_wc_attribute()
        
        # Step 2: Get existing terms from WooCommerce
        existing_wc_terms = self._get_or_create_wc_attribute_terms(wc_attr_id,attr_doc)
        
        return [wc_attr_id, existing_wc_terms]
        # # Step 3: Sync each ERPNext UdM value
        # if attr_doc.item_attribute_values:
        #     self._sync_udm_values(attr_doc, wc_attr_id, existing_wc_terms)
        
        # frappe.logger().info(f"✅ Synced UdM attribute with {len(attr_doc.item_attribute_values)} values")
    def _get_or_create_wc_attribute_terms(self, wc_attr_id: int, attr_doc:str) -> List[int]:
        """Get existing WooCommerce attribute terms or create new ones"""
        # Implementation for fetching or creating WC attribute terms
        existing_terms = {}
        # Step 1: Fetch all existing terms from WooCommerce
        try:
            page = 1
            per_page = 100
            
            frappe.logger().info(f"Fetching existing terms for attribute ID: {wc_attr_id}")
            
            while True:
                response = self.wc_api.get(
                    f"products/attributes/{wc_attr_id}/terms",
                    params={"per_page": per_page, "page": page}
                )
                
                # Handle Response object
                if hasattr(response, 'json'):
                    terms_data = response.json()
                else:
                    terms_data = response
                
                if not terms_data:
                    break
                
                for term in terms_data:
                    term_name = term.get("name", "")
                    term_id = term.get("id")
                    if term_name and term_id:
                        existing_terms[term_name] = term_id
                        frappe.logger().debug(f"  Found term: '{term.get('name')}' (ID: {term_id})")
                
                if len(terms_data) < per_page:
                    break
                
                page += 1
            
            frappe.logger().info(f"Found {len(existing_terms)} existing terms in WooCommerce")
            
        except Exception as e:
            frappe.log_error("Get WC Attribute Terms Error", frappe.get_traceback())
            frappe.logger().warning(f"Could not fetch existing terms: {str(e)}")
        if attr_doc in existing_terms:
            frappe.logger().info(f"Term '{attr_doc}' already exists in WooCommerce with ID: {existing_terms[attr_doc]}")
            return existing_terms[attr_doc]
        else:
            # Create new term
            payload = {
                "name": attr_doc,
                "slug": _slugify(attr_doc)
            }
            try:
                response = self.wc_api.post(
                    f"products/attributes/{wc_attr_id}/terms",
                    payload
                )
                
                # Handle Response object
                if hasattr(response, 'json'):
                    result = response.json()
                else:
                    result = response
                
                term_id = result.get("id")
                
                if not term_id:
                    frappe.throw(f"Failed to create term. Response: {result}")
                
                frappe.logger().info(f"✅ Created new term '{attr_doc}' with ID: {term_id}")
                return term_id
                
            except Exception as e:
                frappe.log_error("Create WC Attribute Term Error", frappe.get_traceback())
                frappe.throw(f"Failed to create term: {str(e)}")

    def _get_or_create_wc_attribute(self) -> int:
        """Get existing WooCommerce attribute ID or create new one"""
        
        
        # Check if attribute exists in WooCommerce by slug or name
        
        try:
        
            response = self.wc_api.get("products/attributes")
                    # Handle Response object properly
            if hasattr(response, 'json'):
                all_attributes = response.json()
            else:
                all_attributes = response
            
            for wc_attr in all_attributes:
                name = wc_attr.get("name", "").lower()
                # Check for UdM, udm, unité de mesure, unite de mesure, etc.
                if name in ["unité de mesure", "unite de mesure"]:
                    wc_id = wc_attr.get("id")
                    frappe.logger().info(f"Found UdM in WooCommerce with ID: {wc_id}")
                    return int(wc_id)
            # If not found, create it
            frappe.logger().info("UdM attribute not found in WooCommerce, creating new one.")
               # Create new attribute
            payload = {
                "name":  "Unité de Mesure",
                "slug": _slugify("Unité de Mesure"),
                "type": "select",
                "order_by": "menu_order",
                "has_archives": False
            }
                
            try:
                response = self.wc_api.post("products/attributes", payload)
                
                # Handle Response object
                if hasattr(response, 'json'):
                    result = response.json()
                else:
                    result = response
                
                wc_id = result.get("id")
                if not wc_id:
                    frappe.throw(f"Failed to create attribute. Response: {result}")
                
                # Save mapping
                
                frappe.logger().info(f"✅ Created new attribute unité de mesure with ID: {wc_id}")
                return int(wc_id)
                
            except Exception as e:
                frappe.log_error("Create WC Attribute Error", frappe.get_traceback())
                frappe.throw(f"Failed to create attribute: {str(e)}")
    
            
        except Exception as e:
            frappe.logger().warning(f"Could not check existing attributes: {str(e)}")
        


# ==================== API FUNCTIONS ====================
# to do adjust sync_all to sync only brands and attributes
@frappe.whitelist()
def sync_all(woocommerce_server: str = None):
    """Sync all brands and attributes to WooCommerce"""
    if woocommerce_server:
        servers = [frappe.get_doc("WooCommerce Server", woocommerce_server)]
    else:
        servers = [
            frappe.get_doc("WooCommerce Server", s.name)
            for s in frappe.get_all("WooCommerce Server", filters={"enable_sync": 1})
        ]

    if not servers:
        frappe.throw("No enabled WooCommerce server found")

    sync = SynchroniseBrandsAttributes(servers=servers)
    sync.run()

    return {"status": "success", "message": f"Synced to {len(servers)} server(s)"}


@frappe.whitelist()
def sync_brand(brand_name: str, woocommerce_server: str = None):
    """Sync single brand"""
    if not woocommerce_server:
        woocommerce_server = frappe.db.get_value(
            "Brand",
            filters={
                "name": brand_name
            },
            fieldname="custom_woocomerce_server"
        )
        if not woocommerce_server:
            frappe.throw("No enabled WooCommerce server in brand found")
    server = frappe.get_doc("WooCommerce Server", woocommerce_server)
    sync = SynchroniseBrandsAttributes(servers=[server])
    sync.wp_config = _get_wp_config()
    
    
    sync._sync_brand(brand_name)

    return {"status": "success", "message": f"Synced brand {brand_name}"}


@frappe.whitelist()
def sync_attribute(attribute_name: str, woocommerce_server: str = None):
    """Sync single attribute"""
    if not woocommerce_server:
        woocommerce_server = frappe.get_value("WooCommerce Server", {"enable_sync": 1}, "name")
        if not woocommerce_server:
            frappe.throw("No enabled WooCommerce server found")

    
    server = frappe.get_doc("WooCommerce Server", woocommerce_server)
    sync = SynchroniseBrandsAttributes(servers=[server])
    sync.wp_config = _get_wp_config()
    sync.wc_api=APIWithRequestLogging(
        url=server.woocommerce_server_url,
        consumer_key=server.api_consumer_key,
        consumer_secret=server.api_consumer_secret,
        version="wc/v3",
        timeout=40,
        verify_ssl=False,
    )

    attr_id, attr_term_id = sync._sync_udm_attribute(attribute_name)

    return {"status": "success", "message": f"Synced attribute {attribute_name}","value_id": [attr_id, attr_term_id]}


@frappe.whitelist()
def sync_in_background(woocommerce_server: str = None):
    """Run sync in background"""
    frappe.enqueue(sync_all, queue="long", timeout=3600, woocommerce_server=woocommerce_server)
    return {"status": "success", "message": "Sync started in background"}