import json
import frappe
from frappe.utils import now, time_diff_in_seconds, get_datetime
from woocommerce_fusion.tasks.sync_items import run_item_sync

EXCLUDED_GROUPS = [
    "Services & Interventions",
    "Tous les Groupes d'Articles",
    "Contrats de maintenance",
    "Echange",
    "Livraison",
    "Main d'œuvre",
]

# Clé pour le verrou de synchronisation (évite les syncs simultanées)
SYNC_LOCK_KEY = "woocommerce_item_sync_running"
SYNC_LOCK_TIMEOUT = 4 * 60 * 60  # 4 heures max


def get_sync_lock_info():
    """Récupère et désérialise les informations du verrou de synchronisation.
    
    Returns:
        dict ou None: Les informations du lock ou None si pas de lock
    """
    lock_data = frappe.cache().get(SYNC_LOCK_KEY)
    if lock_data:
        try:
            return json.loads(lock_data)
        except (json.JSONDecodeError, TypeError):
            # Si la désérialisation échoue, retourner None
            frappe.logger().warning(f"[SYNC] Failed to deserialize lock data: {lock_data}")
            return None
    return None


def log_sync_result(item_code: str, status: str, offset: int = 0, error_message: str = None, error_traceback: str = None):
    """Log the result of an item sync operation."""
    try:
        sync_log = frappe.get_doc({
            "doctype": "WooCommerce Item Sync Log",
            "item_code": item_code,
            "status": status,
            "sync_date": now(),
            "batch_offset": offset,
            "error_message": error_message,
            "error_traceback": error_traceback,
        })
        sync_log.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        # Don't let logging errors break the sync process
        frappe.logger().exception(f"[SYNC] Failed to log sync result for item={item_code}")


def update_sync_report(report_id: str, item_code: str, status: str, offset: int, error_message: str = None):
    """Update the sync report in real-time with item result."""
    try:
        report = frappe.get_doc("WooCommerce Sync Report", report_id)
        
        # Add item to child table
        report.append("items", {
            "item_code": item_code,
            "status": status,
            "sync_date": now(),
            "batch_offset": offset,
            "error_message": error_message[:140] if error_message else None
        })
        
        # Update summary statistics
        report.total_items = len(report.items)
        report.success_count = len([i for i in report.items if i.status == "Success"])
        report.failed_count = len([i for i in report.items if i.status == "Failed"])
        if report.total_items > 0:
            report.success_rate = (report.success_count / report.total_items) * 100
        
        report.save(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.logger().exception(f"[SYNC] Failed to update sync report {report_id}")


# 🔥 Fonction interne (sans @whitelist) pour les jobs en queue
def sync_active_items_batch(batch_size: int = 25, offset: int = 0, report_id: str = None, sync_mode: str = "all"):
    batch_size = int(batch_size or 25)
    offset = int(offset or 0)
    sync_mode = sync_mode or "all"
    
    try:
        # Poser le verrou au tout premier batch
        if offset == 0:
            lock_info = get_sync_lock_info()
            if lock_info:
                # Un lock existe déjà, mais vérifier s'il n'est pas expiré
                frappe.logger().warning(f"[SYNC] Lock already exists: {lock_info}. Continuing anyway (this is the initial batch).")
            
            # Poser le lock avec informations
            lock_data = {
                "report_id": report_id,
                "sync_mode": sync_mode,
                "started_at": now(),
            }
            frappe.cache().setex(SYNC_LOCK_KEY, SYNC_LOCK_TIMEOUT, json.dumps(lock_data))
            frappe.logger().info(f"[SYNC] Lock acquired for {sync_mode} sync (report={report_id})")

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
            
            # Libérer le verrou (sync terminée)
            try:
                frappe.cache().delete(SYNC_LOCK_KEY)
                frappe.logger().info(f"[SYNC] Lock released (sync completed)")
            except Exception:
                frappe.logger().exception(f"[SYNC] Failed to release lock")
            
            # Mark report as completed
            if report_id:
                try:
                    report = frappe.get_doc("WooCommerce Sync Report", report_id)
                    report.status = "Completed"
                    report.completed_at = now()
                    report.current_batch_offset = offset
                    
                    # Calculate duration
                    if report.started_at and report.completed_at:
                        duration = time_diff_in_seconds(get_datetime(report.completed_at), get_datetime(report.started_at))
                        report.duration_seconds = int(duration)
                    
                    report.save(ignore_permissions=True)
                    
                    # Update last synchronisation date in settings
                    settings = frappe.get_single("WooCommerce Integration Settings")
                    settings.wc_last_sync_date_items = report.completed_at
                    settings.save(ignore_permissions=True)
                    
                    frappe.db.commit()
                    
                    frappe.logger().info(f"[SYNC] Report {report_id} completed. Updated wc_last_sync_date_items to {report.completed_at}")
                except Exception:
                    frappe.logger().exception(f"[SYNC] Failed to finalize report {report_id}")
            
            return {
                "status": "done",
                "offset": offset,
                "processed": 0,
                "failed": 0,
                "report_id": report_id
            }

        failed = 0
        skipped = 0
        
        # Get last sync date for "modified" mode
        last_sync_date = None
        if sync_mode == "modified":
            settings = frappe.get_single("WooCommerce Integration Settings")
            last_sync_date = settings.wc_last_sync_date_items

        for item_code in items:
            try:
                # In "modified" mode, skip items not modified since last sync
                if sync_mode == "modified" and last_sync_date:
                    item = frappe.get_doc("Item", item_code)
                    if item.modified <= get_datetime(last_sync_date):
                        skipped += 1
                        frappe.logger().debug(f"[SYNC] Skipped item={item_code} (not modified since {last_sync_date})")
                        log_sync_result(item_code, "Skipped", offset)
                        continue
                
                run_item_sync(item_code)
                frappe.db.commit()
                # Log success
                log_sync_result(item_code, "Success", offset)
                # Update report in real-time
                if report_id:
                    update_sync_report(report_id, item_code, "Success", offset)
            except Exception as e:
                failed += 1
                error_msg = str(e)
                error_tb = frappe.get_traceback()
                frappe.logger().exception(f"[SYNC] Failed item={item_code}")
                frappe.db.rollback()
                # Log failure
                log_sync_result(item_code, "Failed", offset, error_msg, error_tb)
                # Update report in real-time
                if report_id:
                    update_sync_report(report_id, item_code, "Failed", offset, error_msg)

        # Update report progress
        if report_id:
            try:
                report = frappe.get_doc("WooCommerce Sync Report", report_id)
                report.current_batch_offset = offset
                report.total_batches = (offset // batch_size) + 1
                report.save(ignore_permissions=True)
                frappe.db.commit()
            except Exception:
                frappe.logger().exception(f"[SYNC] Failed to update report progress")

        # Enqueue le prochain batch
        next_offset = offset + batch_size

        frappe.enqueue(
            method="woocommerce_fusion.tasks.sync_job.sync_active_items_batch",
            queue="long",
            timeout=2 * 60 * 60,
            job_name=f"Sync batch offset={next_offset}",
            batch_size=batch_size,
            offset=next_offset,
            report_id=report_id,
            sync_mode=sync_mode,
        )

        return {
            "status": "ok",
            "offset": offset,
            "processed": len(items),
            "skipped": skipped,
            "failed": failed,
            "next_offset": next_offset,
            "report_id": report_id
        }
    
    except Exception as e:
        # En cas d'erreur critique, libérer le verrou et marquer le report comme Failed
        frappe.logger().exception(f"[SYNC] Critical error in batch processing at offset={offset}")
        
        # Libérer le verrou
        try:
            frappe.cache().delete(SYNC_LOCK_KEY)
            frappe.logger().info(f"[SYNC] Lock released due to critical error")
        except Exception:
            frappe.logger().exception(f"[SYNC] Failed to release lock after error")
        
        # Marquer le report comme Failed
        if report_id:
            try:
                report = frappe.get_doc("WooCommerce Sync Report", report_id)
                report.status = "Failed"
                report.completed_at = now()
                report.save(ignore_permissions=True)
                frappe.db.commit()
            except Exception:
                frappe.logger().exception(f"[SYNC] Failed to mark report as failed")
        
        # Re-raise l'exception
        raise


# 🔥 Fonction API séparée (avec @whitelist) pour démarrer le sync
@frappe.whitelist()
def start_sync(batch_size: int = 25, offset: int = 0, sync_mode: str = "all"):
    """Démarre la synchronisation des articles actifs.
    
    Args:
        batch_size: Nombre d'items par batch
        offset: Offset de départ
        sync_mode: "all" pour tous les items actifs, "modified" pour items modifiés seulement
    """
    batch_size = int(batch_size or 25)
    offset = int(offset or 0)
    sync_mode = sync_mode or "all"
    
    # Vérifier si une synchronisation est déjà en cours
    lock_info = get_sync_lock_info()
    if lock_info:
        existing_mode = lock_info.get("sync_mode", "unknown")
        existing_report = lock_info.get("report_id", "unknown")
        started_at = lock_info.get("started_at", "unknown")
        
        error_msg = (
            f"Une synchronisation est déjà en cours (mode={existing_mode}, "
            f"report={existing_report}, started_at={started_at}). "
            f"Veuillez patienter qu'elle se termine ou vérifier le rapport."
        )
        
        frappe.logger().warning(f"[SYNC] {error_msg}")
        frappe.throw(error_msg, title="Synchronisation déjà en cours")
    
    # Create sync report
    report = frappe.get_doc({
        "doctype": "WooCommerce Sync Report",
        "status": "Running",
        "started_at": now(),
        "batch_size": batch_size,
        "current_batch_offset": offset,
        "total_batches": 0,
        "total_items": 0,
        "success_count": 0,
        "failed_count": 0,
        "sync_mode": sync_mode,
    })
    report.insert(ignore_permissions=True)
    frappe.db.commit()
    
    frappe.enqueue(
        method="woocommerce_fusion.tasks.sync_job.sync_active_items_batch",
        queue="long",
        timeout=2 * 60 * 60,
        job_name=f"Sync {sync_mode} batch offset={offset}",
        batch_size=batch_size,
        offset=offset,
        report_id=report.name,
        sync_mode=sync_mode,
    )
    
    return {
        "status": "started",
        "message": f"Synchronisation démarrée (mode={sync_mode}) avec batch_size={batch_size}, offset={offset}",
        "report_id": report.name,
        "report_url": f"/app/woocommerce-sync-report/{report.name}",
        "sync_mode": sync_mode
    }


# 📅 Fonctions pour les tâches planifiées (cron)
def cron_daily_sync_modified_items():
    """Synchronisation quotidienne des items modifiés depuis la dernière sync.
    
    Cette fonction est appelée automatiquement par le scheduler (daily).
    Elle synchronise uniquement les items qui ont été modifiés depuis
    la dernière synchronisation complète.
    """
    try:
        frappe.logger().info("[SYNC CRON] Starting daily sync of modified items")
        result = start_sync(batch_size=50, offset=0, sync_mode="modified")
        frappe.logger().info(f"[SYNC CRON] Daily sync started: {result}")
        return result
    except Exception:
        frappe.logger().exception("[SYNC CRON] Failed to start daily sync")
        raise


def cron_weekly_sync_all_items():
    """Synchronisation hebdomadaire complète de tous les items actifs.
    
    Cette fonction est appelée automatiquement par le scheduler (weekly).
    Elle synchronise TOUS les items actifs, ignorant la date de dernière
    synchronisation, pour servir de filet de sécurité.
    """
    try:
        frappe.logger().info("[SYNC CRON] Starting weekly sync of all items")
        result = start_sync(batch_size=25, offset=0, sync_mode="all")
        frappe.logger().info(f"[SYNC CRON] Weekly sync started: {result}")
        return result
    except Exception:
        frappe.logger().exception("[SYNC CRON] Failed to start weekly sync")
        raise


# 🔧 Fonctions utilitaires pour gérer le verrou
@frappe.whitelist()
def get_sync_lock_status():
    """Vérifie si une synchronisation est en cours.
    
    Returns:
        dict: Informations sur le verrou actuel ou None
    """
    lock_info = get_sync_lock_info()
    if lock_info:
        return {
            "locked": True,
            "sync_mode": lock_info.get("sync_mode"),
            "report_id": lock_info.get("report_id"),
            "started_at": lock_info.get("started_at"),
            "report_url": f"/app/woocommerce-sync-report/{lock_info.get('report_id')}" if lock_info.get("report_id") else None
        }
    return {
        "locked": False,
        "message": "Aucune synchronisation en cours"
    }


@frappe.whitelist()
def force_release_sync_lock():
    """Force la libération du verrou de synchronisation.
    
    ⚠️ ATTENTION: À utiliser uniquement si une sync est bloquée et que
    vous êtes certain qu'aucun job n'est réellement en cours.
    
    Returns:
        dict: Statut de l'opération
    """
    lock_info = get_sync_lock_info()
    
    if not lock_info:
        return {
            "status": "no_lock",
            "message": "Aucun verrou à libérer"
        }
    
    # Libérer le verrou
    frappe.cache().delete(SYNC_LOCK_KEY)
    
    frappe.logger().warning(
        f"[SYNC] Lock forcefully released. Previous lock info: {lock_info}"
    )
    
    return {
        "status": "released",
        "message": "Verrou libéré avec succès",
        "previous_lock": lock_info
    }


# 🎯 Synchronisation d'une SÉLECTION d'articles (demande 29/08/2026) : mêmes
# verrou, rapport et unité de travail (run_item_sync) que la synchro de masse —
# seule la source change : les articles cochés dans la liste.

@frappe.whitelist()
def start_sync_selection(item_codes):
    import json as _json

    codes = _json.loads(item_codes) if isinstance(item_codes, str) else (item_codes or [])
    codes = [c for c in codes if c and frappe.db.exists("Item", c)]
    if not codes:
        frappe.throw("Sélectionnez au moins un article dans la liste.")

    lock_info = get_sync_lock_info()
    if lock_info:
        frappe.throw(
            f"Une synchronisation est déjà en cours (mode={lock_info.get('sync_mode')}, "
            f"report={lock_info.get('report_id')}, started_at={lock_info.get('started_at')}). "
            f"Veuillez patienter qu'elle se termine.",
            title="Synchronisation déjà en cours",
        )

    report = frappe.get_doc({
        "doctype": "WooCommerce Sync Report",
        "status": "Running",
        "started_at": now(),
        "batch_size": len(codes),
        "current_batch_offset": 0,
        "total_batches": 1,
        "total_items": len(codes),
        "success_count": 0,
        "failed_count": 0,
        "sync_mode": "selection",
    })
    report.insert(ignore_permissions=True)
    frappe.db.commit()

    frappe.enqueue(
        method="woocommerce_fusion.tasks.sync_job.sync_selected_items",
        queue="long",
        timeout=2 * 60 * 60,
        job_name=f"Sync selection ({len(codes)} items)",
        item_codes=codes,
        report_id=report.name,
    )

    return {
        "status": "started",
        "message": f"Synchronisation de {len(codes)} article(s) sélectionné(s) démarrée",
        "report_id": report.name,
        "report_url": f"/app/woocommerce-sync-report/{report.name}",
        "sync_mode": "selection",
    }


def sync_selected_items(item_codes, report_id=None):
    """La tournée d'une sélection — un seul lot, pas de chaînage. Le marqueur
    global wc_last_sync_date_items n'est PAS touché : une sélection partielle
    ne doit pas faire croire au mode « modified » que tout est à jour."""
    frappe.cache().setex(SYNC_LOCK_KEY, SYNC_LOCK_TIMEOUT, json.dumps({
        "report_id": report_id, "sync_mode": "selection", "started_at": now(),
    }))
    try:
        for item_code in item_codes:
            try:
                run_item_sync(item_code)
                frappe.db.commit()
                log_sync_result(item_code, "Success", 0)
                if report_id:
                    update_sync_report(report_id, item_code, "Success", 0)
            except Exception as e:
                frappe.db.rollback()
                frappe.logger().exception(f"[SYNC] Failed item={item_code} (selection)")
                log_sync_result(item_code, "Failed", 0, str(e), frappe.get_traceback())
                if report_id:
                    update_sync_report(report_id, item_code, "Failed", 0, str(e))

        if report_id:
            try:
                report = frappe.get_doc("WooCommerce Sync Report", report_id)
                report.status = "Completed"
                report.completed_at = now()
                if report.started_at and report.completed_at:
                    report.duration_seconds = int(time_diff_in_seconds(
                        get_datetime(report.completed_at), get_datetime(report.started_at)))
                report.save(ignore_permissions=True)
                frappe.db.commit()
            except Exception:
                frappe.logger().exception(f"[SYNC] Failed to finalize selection report {report_id}")
    finally:
        try:
            frappe.cache().delete(SYNC_LOCK_KEY)
        except Exception:
            frappe.logger().exception("[SYNC] Failed to release selection lock")
