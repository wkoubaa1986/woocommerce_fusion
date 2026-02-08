import frappe
from woocommerce_fusion.tasks.sync_items import run_item_sync

EXCLUDED_GROUPS = [
    "Services & Interventions",
    "Tous les Groupes d'Articles",
    "Contrats de maintenance",
    "Echange",
    "Livraison",
    "Main d’œuvre",
]

@frappe.whitelist()
def sync_active_items_batch(batch_size: int = 25, offset: int = 0):
    batch_size = int(batch_size or 25)
    offset = int(offset or 0)

    filters = {
        "disabled": 0,
        "item_group": ["not in", EXCLUDED_GROUPS],
    }

    items = frappe.get_all(
        "Item",
        filters=filters,
        pluck="name",
        order_by="item_group asc, item_name asc, name asc",
        limit_start=offset,
        limit_page_length=batch_size,
    )

    if not items:
        frappe.logger().info(f"[SYNC] Finished all items. last_offset={offset}")
        return {
            "status": "done",
            "offset": offset,
            "processed": 0,
            "failed": 0,
        }

    failed = 0

    for item_code in items:
        try:
            run_item_sync(item_code)
            frappe.db.commit()
        except Exception:
            failed += 1
            frappe.logger().exception(f"[SYNC] Failed item={item_code}")
            frappe.db.rollback()

    # 🔥 ENQUEUE SEULEMENT ICI (après traitement)
    next_offset = offset + batch_size

    frappe.enqueue(
        method="woocommerce_fusion.tasks.sync_job.sync_active_items_batch",
        queue="long",
        timeout=2 * 60 * 60,
        job_name=f"Sync batch offset={next_offset}",
        batch_size=batch_size,
        offset=next_offset,
    )

    return {
        "status": "ok",
        "offset": offset,
        "processed": len(items),
        "failed": failed,
        "next_offset": next_offset,
    }
