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
    batch_size = int(batch_size)
    offset = int(offset)

    filters = {
        "disabled": 0,
        "item_group": ["not in", EXCLUDED_GROUPS],
    }

    # 1) Get ONE "page" of items
    items = frappe.get_all(
        "Item",
        filters=filters,
        pluck="name",
        order_by="item_group asc, name asc",
        limit_start=offset,
        limit_page_length=batch_size,
    )

    # 2) Stop condition: no more items
    if not items:
        frappe.logger().info(f"[SYNC] Finished all items. last_offset={offset}")
        return

    # 3) Process this batch
    for item_code in items:
        try:
            run_item_sync(item_code)  # <-- your real sync here
            frappe.db.commit()
        except Exception:
            frappe.logger().exception(f"[SYNC] Failed item={item_code}")
            frappe.db.rollback()

    # 4) Enqueue the NEXT batch
    next_offset = offset + len(items)
    frappe.enqueue(
        method="your_app.your_module.sync_jobs.sync_active_items_batch",
        queue="long",
        timeout=60 * 60,
        kwargs={"batch_size": batch_size, "offset": next_offset},
        job_name=f"Sync batch offset={next_offset}",
    )