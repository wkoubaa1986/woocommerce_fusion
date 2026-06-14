from datetime import datetime
from time import sleep
from typing import Dict, List, Optional, Any
import json
import frappe
from frappe.utils import getdate, nowdate, flt

from woocommerce_fusion.tasks.sync import SynchroniseWooCommerce
from woocommerce_fusion.woocommerce.doctype.woocommerce_server.woocommerce_server import (
    WooCommerceServer,
)
from woocommerce_fusion.tasks.utils import APIWithRequestLogging
from woocommerce_fusion.woocommerce.woocommerce_api import (
    generate_woocommerce_record_name_from_domain_and_id,
)


_VERIFY_TLS = None

def get_verify_tls() -> bool:
    """Lit le setting uniquement quand Frappe est initialisé (runtime)."""
    global _VERIFY_TLS
    if _VERIFY_TLS is None:
        v = frappe.db.get_single_value("WooCommerce Fusion Settings", "verify_ssl_certificates")
        # Choisis ton défaut : True est généralement le meilleur
        _VERIFY_TLS = True if v is None else bool(v)
    return _VERIFY_TLS

def update_item_discount_for_woocommerce_from_hook(doc, method):
    """Hook to sync item discounts when pricing rules are created/updated"""
    if not frappe.flags.in_test:
        if doc.doctype == "Pricing Rule":
            frappe.enqueue(
                "woocommerce_fusion.tasks.sync_item_discount.run_item_discount_sync",
                enqueue_after_commit=True,
                pricing_rule_name=doc.name,
            )



def _gmt_start(d):
    return f"{frappe.utils.getdate(d).isoformat()}T00:00:00" if d else None

def _gmt_end(d):
    return f"{frappe.utils.getdate(d).isoformat()}T23:59:59" if d else None

def get_item_codes_from_rule(rule_data: Dict[str, Any]) -> List[str]:
    """
    Get list of item codes from pricing rule data.
    
    Args:
        rule_data: Dictionary containing pricing rule data with item_codes or item_groups
        
    Returns:
        List of item codes applicable to this pricing rule
        
    Example:
        rule_data = {
            'name': 'PRLE-0177',
            'item_codes': 'AP-5,F-ML-AC',
            'item_groups': None
        }
        result = get_item_codes_from_rule(rule_data)
        # Returns: ['AP-5', 'F-ML-AC']
    """
    item_codes = []
    
    # Check if item_codes is provided and not None
    if rule_data.get('item_codes'):
        # Split comma-separated item codes and strip whitespace
        item_codes = [code.strip() for code in rule_data['item_codes'].split(',') if code.strip()]
    
    # If no item codes, check for item groups
    elif rule_data.get('item_groups'):
        # Split comma-separated item groups
        item_groups = [group.strip() for group in rule_data['item_groups'].split(',') if group.strip()]
        
        # Get all items belonging to these item groups
        for item_group in item_groups:
            items_in_group = frappe.get_all(
                "Item",
                filters={
                    "item_group": item_group,
                    "disabled": 0
                },
                fields=["item_code"],
                order_by="item_code"
            )
            
            # Add item codes from this group
            item_codes.extend([item.item_code for item in items_in_group])
    
    # Return unique item codes (in case of duplicates)
    return list(set(item_codes))

def get_active_pricing_rule_names(item_code: str, price_list: str) -> list[str]:
    # Build item-group ancestry
    item_group = frappe.db.get_value("Item", item_code, "item_group")
    if not item_group:
        return []

    groups = []
    grp = item_group
    while grp and grp != "All Item Groups":
        groups.append(grp)
        grp = frappe.db.get_value("Item Group", grp, "parent_item_group")

    today = frappe.utils.nowdate()
    has_groups = 1 if groups else 0
    groups_tuple = tuple(groups) if groups else ("__never__",)  # placeholder

    rows = frappe.db.sql(
        """
        SELECT DISTINCT pr.name
        FROM `tabPricing Rule` pr
        WHERE
            pr.disable = 0
            AND IFNULL(pr.min_qty, 0) > 0
            AND (pr.valid_from IS NULL OR pr.valid_from <= %(today)s)
            AND (pr.valid_upto IS NULL OR pr.valid_upto >= %(today)s)
            AND (pr.for_price_list >= %(price_list)s)
            AND (
                EXISTS (
                    SELECT 1
                    FROM `tabPricing Rule Item Code` pri
                    WHERE pri.parent = pr.name
                      AND pri.item_code = %(item_code)s
                )
                OR (
                    %(has_groups)s = 1 AND EXISTS (
                        SELECT 1
                        FROM `tabPricing Rule Item Group` prg
                        WHERE prg.parent = pr.name
                          AND prg.item_group IN %(groups)s
                    )
                )
            )
        """,
        {"today": today, "item_code": item_code,"price_list": price_list, "has_groups": has_groups, "groups": groups_tuple},
        as_dict=True,
    )

    return [r["name"] for r in rows]
def fixed_price_from_rule(base_price: float | None, pr) -> float | None:
    
    # Only price discounts; skip product freebies
    if pr.price_or_product_discount and pr.price_or_product_discount != "Price":
        return None

    typ = pr.rate_or_discount or ""
    if typ == "Rate":
        return float(pr.rate)
    if typ == "Discount Percentage":
        if base_price is None: return None
        return float(base_price) * (1 - float(pr.discount_percentage or 0) / 100.0)
    if typ == "Discount Amount":
        if base_price is None: return None
        return max(float(base_price) - float(pr.discount_amount or 0), 0.01)
    return None

@frappe.whitelist()
def run_item_discount_sync_in_background():
    """Run all item discount sync in background"""
    frappe.enqueue(run_item_discount_sync, queue="long", timeout=3600)


@frappe.whitelist()
def run_item_discount_sync(pricing_rule_name: Optional[str] = None):
    """Run item discount synchronization"""
    sync = SynchroniseItemDiscount(pricing_rule_name=pricing_rule_name)
    sync.run()
    return True




class SynchroniseItemDiscount(SynchroniseWooCommerce):
    """
    Class for managing synchronisation of ERPNext Pricing Rules with WooCommerce Discounts
    """

    def __init__(
        self,
        servers: List[WooCommerceServer | frappe._dict] = None,
        pricing_rule_name: Optional[str] = None,
        item_code: Optional[str] = None,
    ) -> None:
        super().__init__(servers)
        self.pricing_rule_name = pricing_rule_name
        self.item_code = item_code
        self.wc_server = None
        self.pricing_rules = []
        self.discount_per_items_QTY = {}


    def run(self) -> None:
        """Run discount synchronisation"""
        for server in self.servers:
            self.wc_server = server
            if self._is_sync_enabled():
                self.get_applicable_pricing_rules()
                self.sync_discounts_with_woocommerce()

    def get_wc_api(self) -> APIWithRequestLogging:
        """Get WooCommerce API client with logging"""
        return APIWithRequestLogging(
            url=self.wc_server.woocommerce_server_url,
            consumer_key=self.wc_server.api_consumer_key,
            consumer_secret=self.wc_server.api_consumer_secret,
            version="wc/v3",
            timeout=300,
            verify_ssl=get_verify_tls(),
        )
    def _is_sync_enabled(self) -> bool:
        """Check if discount sync is enabled for this server"""
        return (
            self.wc_server.enable_sync and 
            getattr(self.wc_server, 'enable_discount_sync', True) and
            getattr(self.wc_server, 'price_list', None) is not None
        )

    def get_applicable_pricing_rules(self) -> None:
        """Get applicable pricing rules for discount sync using frappe functions"""
        self.pricing_rules = []
        
        # Check if server has price list configured
        if not getattr(self.wc_server, 'price_list', None):
            return
        
        # Build conditions for the query
        conditions = []
        values = []
        
        # Base conditions
        base_conditions = """
            IFNULL(pr.selling, 0) = 1
            AND pr.price_or_product_discount = 'Price'
            AND pr.apply_on IN ('Item Code', 'Item Group')
            AND (pr.applicable_for IS NULL OR pr.applicable_for = '')
            AND pr.rate_or_discount IN ('Discount Percentage', 'Discount Amount')
            AND pr.for_price_list = %s
        """
        conditions.append(base_conditions)
        values.extend([self.wc_server.price_list])
        
        # Filter for specific pricing rule if provided
        if self.pricing_rule_name:
            conditions.append("AND pr.name = %s")
            values.append(self.pricing_rule_name)
        if self.item_code:
            item_row = frappe.db.get_value(
                "Item", self.item_code, ["item_group"], as_dict=True
            )
            if item_row and item_row.item_group:
                # Efficient ancestor check via nested set (no need to enumerate all parents)
                ig_bounds = frappe.db.get_value(
                    "Item Group", item_row.item_group, ["lft", "rgt"], as_dict=True
                )

                if ig_bounds:
                    conditions.append("""
                        AND IFNULL(pr.disable, 0) = 0
                        AND (pr.valid_upto IS NULL OR pr.valid_upto >= CURDATE())
                        AND (
                            (pr.apply_on = 'Item Code' AND EXISTS (
                                SELECT 1
                                FROM `tabPricing Rule Item Code` prc2
                                WHERE prc2.parent = pr.name
                                AND prc2.item_code = %s
                            ))
                        OR
                            (pr.apply_on = 'Item Group' AND EXISTS (
                                SELECT 1
                                FROM `tabPricing Rule Item Group` prg2
                                JOIN `tabItem Group` ig_rule
                                ON ig_rule.name = prg2.item_group
                                WHERE prg2.parent = pr.name
                                -- ig_rule is an ancestor (or same) of the item's group
                                AND ig_rule.lft <= %s
                                AND ig_rule.rgt >= %s
                            ))
                        )
                    """)
                    values.extend([self.item_code, ig_bounds.lft, ig_bounds.rgt])

                else:
                    # Fallback if lft/rgt missing: only by exact item code
                    conditions.append("""
                        AND (pr.apply_on = 'Item Code' AND EXISTS (
                            SELECT 1 FROM `tabPricing Rule Item Code` prc2
                            WHERE prc2.parent = pr.name AND prc2.item_code = %s
                        ))
                    """)
                    values.append(self.item_code)

            else:
                # No item group found: only by exact item code
                conditions.append("""
                    AND (pr.apply_on = 'Item Code' AND EXISTS (
                        SELECT 1 FROM `tabPricing Rule Item Code` prc2
                        WHERE prc2.parent = pr.name AND prc2.item_code = %s
                    ))
                """)
                values.append(self.item_code)

        where_clause = " ".join(conditions)
        # Execute query using frappe.db.sql

        query = f"""
        SELECT DISTINCT
            pr.name,
            pr.title,
            pr.disable,
            pr.price_or_product_discount,
            pr.apply_on,
            pr.min_qty,
            pr.max_qty,
            pr.valid_from,
            pr.valid_upto,
            pr.rate_or_discount,
            pr.discount_amount,
            pr.discount_percentage,
            GROUP_CONCAT(DISTINCT prc.item_code) AS item_codes,
            GROUP_CONCAT(DISTINCT prg.item_group) AS item_groups
        FROM `tabPricing Rule` pr
        LEFT JOIN `tabPricing Rule Item Code` prc 
            ON prc.parent = pr.name
        LEFT JOIN `tabPricing Rule Item Group` prg 
            ON prg.parent = pr.name
        WHERE {where_clause}
        GROUP BY pr.name
        ORDER BY pr.priority ASC, pr.valid_from ASC
        """
        response = frappe.db.sql(query, values, as_dict=True)
        
        self.pricing_rules = response
        


    def sync_discounts_with_woocommerce(self) -> None:
        """Synchronise discounts with WooCommerce"""
        
        if not self.pricing_rules:
            wc_api = self.get_wc_api()

            CLEAR_TIERS = {
                "sale_price": "",
                "date_on_sale_from_gmt": None,
                "date_on_sale_to_gmt": None,
                "tiered_pricing_type": "fixed",
                "tiered_pricing_fixed_rules": {},
                "tiered_pricing_percentage_rules": [],
                "meta_data": [{"key": "_fixed_price_rules", "value": {}}]
            }

            results = []
            item_code = self.item_code
            woo_id = frappe.utils.cint(frappe.db.get_value("Item WooCommerce Server",{"parent": item_code, "parenttype": "Item", "parentfield": "woocommerce_servers", "woocommerce_server": self.wc_server.name, "enabled": 1},"woocommerce_id"))
            if not woo_id:
                results.append({"item_code": item_code, "status": "missing_woo_id"})
                return results

            # if payload is None (sync disabled), still PUT to clear tiers
            data = CLEAR_TIERS

            try:
                if bool(frappe.db.get_value("Item", item_code, "variant_of")):
                    woo_parent_id = frappe.db.get_value("Item WooCommerce Server", {"parent": (frappe.db.get_value("Item", item_code, "variant_of") or item_code), "parenttype": "Item", "parentfield": "woocommerce_servers", "woocommerce_server": self.wc_server.name, "enabled": 1}, "woocommerce_id")
                    resp = wc_api.put(f"products/{woo_parent_id}/variations/{woo_id}", data).json()

                else:
                    resp = wc_api.put(f"products/{woo_id}", data).json()
                results.append({
                    "item_code": item_code,
                    "woocommerce_id": woo_id,
                    "tiers": data.get("tiered_pricing_fixed_rules", {}),
                    "status": "ok" if data.get("tiered_pricing_fixed_rules") is not None else "cleared"
                })
            except Exception as e:
                frappe.log_error(
                    title="Woo qty tiers: PUT failed",
                    message=f"Item {item_code} -> product {woo_id}: {e}"
                )
                results.append({
                    "item_code": item_code,
                    "woocommerce_id": woo_id,
                    "status": f"error: {e}"
                })

            return results
        
        for rule_data in self.pricing_rules:
            try:
                pricing_rule = frappe.get_doc("Pricing Rule", rule_data['name'])
                if self._is_quantity_based_discount(pricing_rule):
                    self._sync_quantity_discount(rule_data)
                else:
                    self._sync_promotional_price(pricing_rule, rule_data)
                    
            except Exception as e:
                error_message = f"{frappe.get_traceback()}\n\nRule: {rule_data['pricing_rule_name']}\nItem: {rule_data['item_code']}"
                frappe.log_error("WooCommerce Item Discount Sync Error", error_message)
            
            # Rate limiting
            sleep(getattr(self.wc_server, 'discount_sync_delay', 0.5))

    def _is_quantity_based_discount(self, pricing_rule) -> bool:
        """Determine if this is a quantity-based discount"""
        return bool(
            (pricing_rule.min_qty and pricing_rule.min_qty > 1) or
            (pricing_rule.max_qty and pricing_rule.max_qty > 1)
        )

    def _sync_promotional_price(self, pricing_rule, rule_data) -> None:
        """Sync as promotional/sale price on WooCommerce product"""
        payloads = self._build_discount_data(rule_data, "promotional")
        wc_api = self.get_wc_api()
        
        CLEAR_TIERS = {
            "sale_price": "",
            "date_on_sale_from_gmt": None,
            "date_on_sale_to_gmt": None,
        }
        results = []
        for p in payloads:
            item_code = p.get("item_code")
            woo_id = p.get("woocommerce_id")
            if not woo_id:
                results.append({"item_code": item_code, "status": "missing_woo_id"})
                continue

            # if payload is None (sync disabled), still PUT to clear tiers
            data = p.get("payload") or CLEAR_TIERS
            try:
                if bool(frappe.db.get_value("Item", item_code, "variant_of")):
                    woo_parent_id = frappe.db.get_value("Item WooCommerce Server", {"parent": (frappe.db.get_value("Item", item_code, "variant_of") or item_code), "parenttype": "Item", "parentfield": "woocommerce_servers", "woocommerce_server": self.wc_server.name, "enabled": 1}, "woocommerce_id")
                    resp = wc_api.put(f"products/{woo_parent_id}/variations/{woo_id}", data).json()

                else:
                    resp = wc_api.put(f"products/{woo_id}", data).json()
                results.append({
                    "item_code": item_code,
                    "woocommerce_id": woo_id,
                    "status": "ok" if data.get("sale_price") is not None else "cleared"
                })
            except Exception as e:
                frappe.log_error(
                    title="Woo promo: PUT failed",
                    message=f"Item {item_code} -> product {woo_id}: {e}"
                )
                results.append({
                    "item_code": item_code,
                    "woocommerce_id": woo_id,
                    "status": f"error: {e}"
                })

        return results


    def _sync_quantity_discount(self, rule_data):
        payloads = self._build_discount_data(rule_data, "quantity")
        wc_api = self.get_wc_api()
        CLEAR_TIERS = {
            "tiered_pricing_type": "fixed",
            "tiered_pricing_fixed_rules": {},
            "tiered_pricing_percentage_rules": [],
            "meta_data": [{"key": "_fixed_price_rules", "value": {}}]
        }

        results = []
        for p in payloads:
            item_code = p.get("item_code")
            woo_id = p.get("woocommerce_id")
            if not woo_id:
                results.append({"item_code": item_code, "status": "missing_woo_id"})
                continue

            # if payload is None (sync disabled), still PUT to clear tiers
            data = p.get("payload") or CLEAR_TIERS
            try:
                if bool(frappe.db.get_value("Item", item_code, "variant_of")):
                    woo_parent_id = frappe.db.get_value("Item WooCommerce Server", {"parent": (frappe.db.get_value("Item", item_code, "variant_of") or item_code), "parenttype": "Item", "parentfield": "woocommerce_servers", "woocommerce_server": self.wc_server.name, "enabled": 1}, "woocommerce_id")
                    resp = wc_api.put(f"products/{woo_parent_id}/variations/{woo_id}", data).json()

                else:
                    resp = wc_api.put(f"products/{woo_id}", data).json()
                results.append({
                    "item_code": item_code,
                    "woocommerce_id": woo_id,
                    "tiers": data.get("tiered_pricing_fixed_rules", {}),
                    "status": "ok" if data.get("tiered_pricing_fixed_rules") is not None else "cleared"
                })
            except Exception as e:
                frappe.log_error(
                    title="Woo qty tiers: PUT failed",
                    message=f"Item {item_code} -> product {woo_id}: {e}"
                )
                results.append({
                    "item_code": item_code,
                    "woocommerce_id": woo_id,
                    "status": f"error: {e}"
                })

        return results


    def _calculate_discounted_price(self, regular_price: float, pricing_rule) -> Optional[float]:
        """Calculate discounted price based on pricing rule"""
        if not regular_price:
            return None
            
        if pricing_rule.rate_or_discount == "Discount Percentage" and pricing_rule.discount_percentage:
            discount_amount = regular_price * (pricing_rule.discount_percentage / 100)
            return max(0, regular_price - discount_amount)
            
        elif pricing_rule.rate_or_discount == "Discount Amount" and pricing_rule.discount_amount:
            return max(0, regular_price - pricing_rule.discount_amount)
            
        elif pricing_rule.rate_or_discount == "Rate" and pricing_rule.rate:
            return pricing_rule.rate
            
        return None
    def _get_base_price(self, item_code: str) -> float | None:
        return frappe.db.get_value(
                    "Item Price",
                    {"item_code": item_code, "price_list": self.wc_server.price_list},
                    "price_list_rate"
                )

    def _build_discount_data(self, rule_data, type) -> Dict[str, Any]:
        """Build WooCommerce coupon data for quantity discounts"""
        today = frappe.utils.nowdate()
        items_payloads: List[Dict[str, Any]] = []
        item_codes = [self.item_code]
        if not self.item_code:
            item_codes = get_item_codes_from_rule(rule_data)
        pr = rule_data
        for item in item_codes:
            item_data = frappe.get_doc("Item", item)
                    # Prefer Item's stored Woo ID, else fallback passed via rule_data
            query = """
                SELECT c.woocommerce_id
                FROM `tabItem WooCommerce Server` AS c
                WHERE c.parent = %s
                AND c.parenttype = 'Item'
                AND c.parentfield = 'woocommerce_servers'
                AND (
                        LOWER(REPLACE(REPLACE(TRIM(TRAILING '/' FROM c.woocommerce_server), 'https://', ''), 'http://', '')) = LOWER(%s)
                    OR LOWER(REPLACE(REPLACE(TRIM(TRAILING '/' FROM c.woocommerce_server), 'https://', ''), 'http://', '')) = LOWER(TRIM(TRAILING '/' FROM %s))
                )
                LIMIT 1
            """

            woo_id = frappe.db.sql(query, (item, self.wc_server.name, self.wc_server.name), as_dict=True)
            woo_id_int = int(woo_id[0]['woocommerce_id']) if woo_id else None
            # If sync disabled, record a skipped entry and continue
            if not getattr(item_data, "custom_sync_avec_woocommerce", False):
                items_payloads.append({
                    "item_code": item,
                    "woocommerce_id": woo_id_int,
                    "payload": None,
                    "status": "skipped_sync_disabled",
                })
                continue
            if type == "quantity":
                
                all_related_pricing_rules = get_active_pricing_rule_names(item,self.wc_server.price_list)
                
                fixed_map: dict[int, float] = {}
                for name in all_related_pricing_rules:
                    pr = frappe.get_doc("Pricing Rule", name)
                    if item not in self.discount_per_items_QTY:
                        self.discount_per_items_QTY[item] = {pr.name:fixed_map}
                    elif item in self.discount_per_items_QTY and pr.name not in self.discount_per_items_QTY[item]:
                        self.discount_per_items_QTY[item][pr.name]=fixed_map
                    else:
                        fixed_map = self.discount_per_items_QTY[item][pr.name]
                        continue

                    price = self._get_base_price(item)
                            # gatekeeping
                    if pr.disable: 
                        continue
                    if not pr.min_qty or int(pr.min_qty) <= 0:
                        continue

                    if pr.valid_from and frappe.utils.getdate(pr.valid_from) > frappe.utils.getdate(today):
                        continue
                    if pr.valid_upto and frappe.utils.getdate(pr.valid_upto) < frappe.utils.getdate(today):
                        continue

                    fp = fixed_price_from_rule(price, pr)
                    if fp is None:
                        continue

                    min_qty = int(pr.min_qty)
                    # if multiple rules share the same min_qty, pick the **lowest price**
                    fixed_map[min_qty] = min(fp, fixed_map.get(min_qty, fp))
                    self.discount_per_items_QTY[item][pr.name]=fixed_map
                # Sort & round to 2 decimals
                ordered = {str(k): round(fixed_map[k], 2) for k in sorted(fixed_map)}
                # Build Woo payload (top-level fields + optional meta mirror)
                payload = {
                    "tiered_pricing_type": "fixed",
                    "tiered_pricing_fixed_rules": ordered,
                    "tiered_pricing_percentage_rules": [],
                    "meta_data": [
                        {"key": "_fixed_price_rules",
                        "value": {k: f"{v:g}" for k, v in ordered.items()}}
                    ],
                }


                items_payloads.append({
                    "item_code": item,
                    "woocommerce_id": woo_id_int,
                    "payload": payload,  # if ordered == {}, you can still send it to clear tiers
                    "status": "ok" if ordered else "no_applicable_rules",
                })
            else:
                price = self._get_base_price(item)
                promotion_delete=False
                if pr.disable: 
                    promotion_delete=True
                if pr.min_qty or int(pr.min_qty) > 0:
                    promotion_delete=True

                if pr.valid_from and frappe.utils.getdate(pr.valid_from) > frappe.utils.getdate(today):
                    promotion_delete=True
                if pr.valid_upto and frappe.utils.getdate(pr.valid_upto) < frappe.utils.getdate(today):
                    promotion_delete=True
                
                if promotion_delete:
                    payload = {
                        "sale_price": "",
                        "date_on_sale_from_gmt": None,
                        "date_on_sale_to_gmt":   None,
                        }

                    items_payloads.append({
                        "item_code": item,
                        "woocommerce_id": woo_id_int,
                        "payload": payload,  # if ordered == {}, you can still send it to clear tiers
                        "status": "no_applicable_rules",
                    })
                else:
                    fp = fixed_price_from_rule(price, pr)
                    payload = {
                        "sale_price": f"{float(fp):.2f}",              # Woo expects string
                        "date_on_sale_from_gmt": _gmt_start(pr.valid_from),
                        "date_on_sale_to_gmt": _gmt_end(pr.valid_upto),
                    }
                    items_payloads.append({
                        "item_code": item,
                        "woocommerce_id": woo_id_int,
                        "payload": payload,
                        "status": "ok",
                    })

        
        return items_payloads

    

# Utility functions
@frappe.whitelist()
def sync_single_item_discount(pricing_rule_name: str =None, woocommerce_server: str = None, item_code: str = None):
    """Sync single pricing rule discount"""

    if not woocommerce_server:
        servers = frappe.get_all("WooCommerce Server", filters={"enable_sync": 1}, limit=1)
        if not servers:
            frappe.throw("No WooCommerce server configured")
        woocommerce_server = servers[0].name
    if not item_code:
        sync = SynchroniseItemDiscount(
            servers=[frappe.get_doc("WooCommerce Server", woocommerce_server)],
            pricing_rule_name=pricing_rule_name
        )
    else:
        sync = SynchroniseItemDiscount(
            servers=[frappe.get_doc("WooCommerce Server", woocommerce_server)],
            pricing_rule_name=pricing_rule_name,
            item_code=item_code
        )
    sync.run()
    return_message = f"Discount for rule {pricing_rule_name} synced successfully"
    if not sync.pricing_rules:
        return_message = f"No Discounts"
    return {"status": "success", "message": return_message, "items":item_code}

@frappe.whitelist()
def get_pricing_rules_for_price_list(woocommerce_server: str):
    """Get pricing rules available for the server's configured price list"""
    server_doc = frappe.get_doc("WooCommerce Server", woocommerce_server)
    
    if not getattr(server_doc, 'price_list', None):
        return {"error": "No price list configured for this WooCommerce server"}
    
    # Get pricing rules for this price list
    pricing_rules = frappe.get_all(
        "Pricing Rule",
        filters={
            "disable": 0,
            "for_price_list": server_doc.price_list
        },
        fields=[
            "name", 
            "title", 
            "rate_or_discount", 
            "discount_percentage", 
            "discount_amount",
            "min_qty",
            "max_qty",
            "valid_from",
            "valid_upto"
        ],
        order_by="name"
    )
    
    # Check which ones have items synced to WooCommerce
    applicable_rules = []
    for rule in pricing_rules:
        # Get items for this pricing rule that are synced to this server
        synced_items = frappe.db.sql("""
            SELECT pri.item_code, iwc.woocommerce_id
            FROM `tabPricing Rule Item Code` pri
            INNER JOIN `tabItem WooCommerce Server` iwc ON iwc.parent = pri.item_code
            WHERE pri.parent = %s AND iwc.woocommerce_server = %s AND iwc.enabled = 1
        """, (rule.name, woocommerce_server), as_dict=True)
        
        if synced_items:
            rule["synced_items"] = synced_items
            rule["items_count"] = len(synced_items)
            applicable_rules.append(rule)
    
    return {
        "price_list": server_doc.price_list,
        "total_rules": len(pricing_rules),
        "applicable_rules": len(applicable_rules),
        "rules": applicable_rules
    }


@frappe.whitelist()
def sync_discounts_for_price_list(woocommerce_server: str):
    """Sync all discounts for the server's configured price list"""
    server_doc = frappe.get_doc("WooCommerce Server", woocommerce_server)
    
    if not getattr(server_doc, 'enable_discount_sync', False):
        return {"status": "error", "message": "Discount sync is disabled for this server"}
    
    if not getattr(server_doc, 'price_list', None):
        return {"status": "error", "message": "No price list configured for this server"}
    
    sync = SynchroniseItemDiscount(servers=[server_doc])
    sync.run()
    
    return {
        "status": "success", 
        "message": f"Synced discounts for price list '{server_doc.price_list}' on server {woocommerce_server}",
        "price_list": server_doc.price_list
    }