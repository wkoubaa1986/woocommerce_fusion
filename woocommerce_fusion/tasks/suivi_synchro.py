"""Synchronisation manuelle d'un article avec le site : en tâche de fond, avec son avancement sur la fiche.

Demande du 23/11/2026 : « lorsque je synchronise manuellement, passer le job en background et avoir un indicateur sur
la fiche produit : qu'elle est en train de synchroniser, et à quelle étape ». Avant, le bouton de la fiche appelait la
synchronisation en direct : écran gelé de 1 à 4 minutes, et un échec en cours de route (retouche d'image refusée par
Google, ancienne image impossible à supprimer…) ne se voyait que dans le journal des erreurs.

Principe :
- `lancer(item_code)` (bouton de la fiche) note « en file d'attente » et met la synchronisation dans la file longue ;
- pendant le travail, chaque étape appelle `etape(code, detail)` : l'état est écrit dans le cache (une entrée par
  article, gardée trois jours) et poussé en direct à l'utilisateur qui a lancé ;
- `avertir(texte)` garde ce qui a été fait en mode dégradé (image envoyée sans retouche…), `noter(texte)` ce qui a
  été fait (« 3 images retouchées ») ;
- la fiche lit `etat(item_code)` à l'ouverture, puis toutes les quelques secondes tant que ça tourne.

Hors d'une synchronisation lancée par `lancer` (synchro de masse, tâches planifiées), `etape` / `avertir` / `noter` ne
font rien : le reste du code les appelle sans condition.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, now_datetime, time_diff_in_seconds

CLE = "wcf_synchro:"
GARDE = 3 * 24 * 3600            # l'état reste lisible trois jours
EVENEMENT = "wcf_synchro"
DELAI_MAX = 30 * 60              # une synchronisation muette depuis 30 minutes est considérée interrompue
DUREE_JOB = 30 * 60

# Les étapes, dans l'ordre où le code les fait (tasks/sync_items.py, integrations/content_enrichment.py).
ETAPES = {
    "attente": "En file d’attente",
    "site": "Lecture du produit sur le site",
    "classement": "Classement de l’article (groupe, collections)",
    "textes": "Textes du site et SEO",
    "images": "Retouche des images",
    "fiche": "Fiche produit envoyée au site",
    "images_site": "Images envoyées au site",
    "finitions": "Marque, catégories et promotions",
}
EN_COURS = ("attente", "en_cours")


# ── règles pures ─────────────────────────────────────────────────────────────
def etapes_prevues(item: dict, refaire_images: bool = False) -> list[str]:
    """Les étapes que CET article va traverser, d'après ses cases cochées."""
    prevues = ["images", "site"] if (refaire_images and not cint(item.get("custom_generate_seo"))) else ["site"]
    modele_ou_simple = not item.get("variant_of")
    if modele_ou_simple and (cint(item.get("custom_generate_classification")) or cint(item.get("custom_generate_tag"))):
        prevues.append("classement")
    if cint(item.get("custom_generate_seo")):
        prevues += ["textes", "images"]
    return prevues + ["fiche", "images_site", "finitions"]


def avancer(etat: dict, code: str, detail: str | None = None) -> dict:
    """L'état après le passage à l'étape `code`. Une étape imprévue s'ajoute à la suite : on ne recule jamais."""
    etat = dict(etat)
    prevues = list(etat.get("etapes") or [])
    if code not in prevues:
        prevues.append(code)
    etat.update(etat="en_cours", etape=code, libelle=ETAPES.get(code, code), detail=detail or "",
                etapes=prevues, rang=prevues.index(code) + 1, total=len(prevues))
    return etat


def interrompue(etat: dict | None, maintenant) -> bool:
    """En cours d'après le cache, mais muette depuis trop longtemps : le serveur a redémarré pendant le travail."""
    if not etat or etat.get("etat") not in EN_COURS or not etat.get("maj"):
        return False
    return time_diff_in_seconds(maintenant, etat["maj"]) > DELAI_MAX


def cause_lisible(erreur: Exception) -> str:
    """La première ligne utile d'une erreur, sans le jargon du code."""
    if erreur.__class__.__name__ == "SyncDisabledError":
        return "La synchronisation avec ce site est désactivée (case « Enable Sync » du serveur WooCommerce)."
    texte = str(erreur) or erreur.__class__.__name__
    premiere = next((l.strip() for l in texte.splitlines() if l.strip()), erreur.__class__.__name__)
    return frappe.utils.strip_html(premiere)[:300]


# ── état, dans le cache ──────────────────────────────────────────────────────
def _lire(item_code: str) -> dict | None:
    # ⚠️ `expires=True` : sans lui, Frappe garde la première lecture dans une copie locale au processus, que
    # `set_value(..., expires_in_sec=…)` ne met PAS à jour — la tâche de fond relisait indéfiniment « en file
    # d'attente », perdait ses avertissements et annonçait la mauvaise étape à l'échec (essai du 23/11/2026).
    return frappe.cache().get_value(CLE + item_code, expires=True)


def _ecrire(etat: dict) -> dict:
    etat["maj"] = etat["maintenant"] = str(now_datetime())
    frappe.cache().set_value(CLE + etat["item"], etat, expires_in_sec=GARDE)
    try:
        frappe.publish_realtime(EVENEMENT, etat, user=etat.get("par") or frappe.session.user)
    except Exception:
        pass            # la fiche relit de toute façon l'état toutes les quelques secondes
    return etat


def _suivi() -> str | None:
    return frappe.flags.get("wcf_suivi")


def etape(code: str, detail: str | None = None) -> None:
    """Appelé par le code de synchronisation à chaque étape (et dans l'étape : « image 2 sur 4 »)."""
    item_code = _suivi()
    if not item_code:
        return
    try:
        _ecrire(avancer(_lire(item_code) or {"item": item_code}, code, detail))
    except Exception:
        pass            # le suivi ne doit jamais faire échouer une synchronisation


def _ajouter(cle: str, texte: str) -> None:
    item_code = _suivi()
    if not item_code or not texte:
        return
    try:
        etat = _lire(item_code) or {"item": item_code}
        liste = list(etat.get(cle) or [])
        if texte not in liste:
            liste.append(texte)
        etat[cle] = liste
        _ecrire(etat)
    except Exception:
        pass


def avertir(texte: str) -> None:
    """Quelque chose n'a pas pu être fait, mais la synchronisation continue (affiché en orange sur la fiche)."""
    _ajouter("avertissements", texte)


def noter(texte: str) -> None:
    """Ce qui a été fait, pour le résumé final (« 3 images retouchées »)."""
    _ajouter("resume", texte)


# ── appels de la fiche article ───────────────────────────────────────────────
@frappe.whitelist()
def etat(item_code: str) -> dict | None:
    frappe.has_permission("Item", "read", item_code, throw=True)
    e = _lire(item_code)
    if interrompue(e, now_datetime()):
        e.update(etat="echec", fin=str(now_datetime()),
                 message=_("Synchronisation interrompue (le serveur a redémarré pendant le travail). Relancez-la."))
        frappe.cache().set_value(CLE + item_code, e, expires_in_sec=GARDE)
    if e:
        e["maintenant"] = str(now_datetime())       # pour la durée écoulée, comptée sur l'heure du serveur
    return e


@frappe.whitelist()
def lancer(item_code: str, refaire_images: int | str = 0) -> dict:
    """Met la synchronisation de l'article dans la file et rend la main tout de suite.

    `refaire_images` : repasse TOUTES les images de l'article à la retouche, même celles déjà traitées (utile après une
    retouche ratée, ou après un changement de consigne)."""
    frappe.has_permission("Item", "write", item_code, throw=True)
    refaire_images = cint(refaire_images)
    item = frappe.db.get_value("Item", item_code, ["name", "variant_of", "custom_generate_classification", "custom_generate_tag",
                                                   "custom_generate_seo"], as_dict=True)
    if not item:
        frappe.throw(_("Article {0} introuvable.").format(item_code))
    if not frappe.db.exists("Item WooCommerce Server", {"parent": item_code, "parenttype": "Item"}):
        frappe.throw(_("Aucun site WooCommerce n’est défini sur l’article {0}.").format(item_code))
    actuel = etat(item_code)
    if actuel and actuel.get("etat") in EN_COURS:
        return dict(actuel, deja=1)
    maintenant = str(now_datetime())
    prevues = etapes_prevues(item, bool(refaire_images))
    nouvel = _ecrire({"item": item_code, "etat": "attente", "etape": "attente", "libelle": ETAPES["attente"], "detail": "",
                      "etapes": prevues, "rang": 0, "total": len(prevues), "debut": maintenant, "fin": None,
                      "par": frappe.session.user, "refaire_images": refaire_images, "avertissements": [], "resume": [],
                      "message": ""})
    frappe.enqueue("woocommerce_fusion.tasks.suivi_synchro.executer", queue="long", timeout=DUREE_JOB,
                   job_id="wcf_synchro::" + item_code, deduplicate=True, enqueue_after_commit=True,
                   item_code=item_code, refaire_images=refaire_images)
    return nouvel


# ── le travail, dans la file ─────────────────────────────────────────────────
def executer(item_code: str, refaire_images: int = 0) -> None:
    from woocommerce_fusion.tasks.sync_items import run_item_sync

    frappe.flags.wcf_suivi = item_code
    try:
        try:        # une image principale qui pointe sur un fichier disparu fait échouer chaque enregistrement
            from woocommerce_fusion.integrations.content_enrichment import reparer_image_principale
            reparer_image_principale(item_code)
        except Exception:
            frappe.log_error(title="Synchronisation manuelle : image principale", message=frappe.get_traceback())
        if cint(refaire_images):
            _refaire_les_images(item_code)
        run_item_sync(item_code=item_code)
        frappe.db.commit()
        e = _lire(item_code) or {"item": item_code}
        e.update(etat="terminee", fin=str(now_datetime()), detail="", message="")
        _ecrire(e)
    except Exception as erreur:
        frappe.db.rollback()
        frappe.log_error(title="Synchronisation manuelle : échec (%s)" % item_code, message=frappe.get_traceback())
        e = _lire(item_code) or {"item": item_code}
        e.update(etat="echec", fin=str(now_datetime()), message=cause_lisible(erreur))
        _ecrire(e)
        frappe.db.commit()
    finally:
        frappe.flags.wcf_suivi = None


def _refaire_les_images(item_code: str) -> None:
    """Toutes les images de l'article repassent à la retouche. Si « Générer SEO » est coché, la synchronisation s'en
    charge elle-même juste après (elle retouche tout ce qui n'est pas marqué traité)."""
    from woocommerce_fusion.integrations import content_enrichment as C

    if not frappe.db.has_column("File", "custom_treated_ai"):
        return
    frappe.db.sql("""update `tabFile` set custom_treated_ai = 0
                     where attached_to_doctype = 'Item' and attached_to_name = %s and ifnull(is_folder, 0) = 0""", (item_code,))
    frappe.db.commit()
    if cint(frappe.db.get_value("Item", item_code, "custom_generate_seo")):
        return
    etape("images")
    C.dedupe_item_images(item_name=item_code, dry_run=False, detach_missing=True)
    C.treat_left_item_images(item_name=item_code)
    C.reparer_image_principale(item_code)
