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

Retouche manuelle d'images (même jour) : « lorsque c'est traité par l'IA on ne la retraite plus, mais si elle est
traitée on peut la retraiter, une image, en ajoutant une consigne ». Le passage automatique ne touche jamais une image
déjà traitée ; `lancer_retouche(item_code, fichiers, consigne)` (fenêtre « Retoucher des images avec l'IA » de la
fiche) repasse à l'IA les images CHOISIES, traitées ou non, avec la consigne écrite par l'utilisateur.

Contrôle avant (même soir, après une retouche fausse partie directement sur le site) : la retouche ne remplace plus
rien. Elle produit des PROPOSITIONS, l'état passe à « à valider », la fiche montre l'avant / après et l'utilisateur
choisit image par image (`valider_retouches`) ; seules les retouches gardées prennent la place de l'ancienne image,
puis l'article part sur le site si c'était demandé. `restaurer` remet l'image d'avant à la place d'une retouche.
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
    "retouche": "Retouche des images choisies",
    "composition": "Composition de l’image",
    "application": "Mise en place des retouches gardées",
    "restauration": "Retour aux images d’avant",
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
def etapes_prevues(item: dict) -> list[str]:
    """Les étapes que CET article va traverser pendant une synchronisation, d'après ses cases cochées."""
    prevues = ["site"]
    modele_ou_simple = not item.get("variant_of")
    if modele_ou_simple and (cint(item.get("custom_generate_classification")) or cint(item.get("custom_generate_tag"))):
        prevues.append("classement")
    if cint(item.get("custom_generate_seo")):
        prevues += ["textes", "images"]
    return prevues + ["fiche", "images_site", "finitions"]


def etapes_apres(premiere: str, item: dict, synchroniser: bool) -> list[str]:
    """Une étape sur les images (« application » des retouches gardées, « restauration »), puis la synchronisation si
    elle est demandée."""
    return [premiere] + (etapes_prevues(item) if synchroniser else [])


# Ce que le bandeau dit, selon ce qui a été lancé.
LIBELLES = {
    "synchro": {"titre": "Synchronisation avec le site", "fait": "Synchronisé avec le site",
                "rate": "La synchronisation avec le site a échoué"},
    "retouche": {"titre": "Retouche des images", "fait": "Retouches prêtes", "rate": "La retouche des images a échoué"},
    "composition": {"titre": "Composition d’une image", "fait": "Image composée", "rate": "La composition de l’image a échoué"},
    "validation": {"titre": "Mise en place des images gardées", "fait": "Images mises en place",
                   "rate": "La mise en place des images a échoué"},
    "restauration": {"titre": "Retour aux images d’avant", "fait": "Images d’avant remises",
                     "rate": "Le retour aux images d’avant a échoué"},
}
A_VALIDER = "a_valider"            # des retouches attendent le choix de l'utilisateur : rien n'est encore remplacé


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


def _article(item_code: str) -> dict:
    frappe.has_permission("Item", "write", item_code, throw=True)
    item = frappe.db.get_value("Item", item_code, ["name", "variant_of", "custom_generate_classification", "custom_generate_tag",
                                                   "custom_generate_seo"], as_dict=True)
    if not item:
        frappe.throw(_("Article {0} introuvable.").format(item_code))
    return item


def _a_un_site(item_code: str) -> bool:
    return bool(frappe.db.exists("Item WooCommerce Server", {"parent": item_code, "parenttype": "Item"}))


def _mettre_en_file(item_code: str, genre: str, prevues: list, methode: str, en_plus: dict | None = None, **arguments) -> dict:
    maintenant = str(now_datetime())
    nouvel = _ecrire(dict({"item": item_code, "genre": genre, "etat": "attente", "etape": "attente", "libelle": ETAPES["attente"],
                           "detail": "", "etapes": prevues, "rang": 0, "total": len(prevues), "debut": maintenant, "fin": None,
                           "par": frappe.session.user, "avertissements": [], "resume": [], "message": ""},
                          **dict(LIBELLES[genre], **(en_plus or {}))))
    frappe.enqueue("woocommerce_fusion.tasks.suivi_synchro." + methode, queue="long", timeout=DUREE_JOB,
                   job_id="wcf_synchro::" + item_code, deduplicate=True, enqueue_after_commit=True,
                   item_code=item_code, **arguments)
    return nouvel


def _occupe(item_code: str) -> dict | None:
    """L'état en cours s'il y en a un (on ne lance pas deux traitements sur le même article)."""
    actuel = etat(item_code)
    return dict(actuel, deja=1) if actuel and actuel.get("etat") in EN_COURS else None


def _jeter_les_propositions(item_code: str) -> None:
    """Des retouches proposées et jamais validées : elles sont supprimées avant de lancer autre chose."""
    actuel = _lire(item_code)
    if actuel and actuel.get("etat") == A_VALIDER and actuel.get("propositions"):
        from woocommerce_fusion.integrations.content_enrichment import supprimer_propositions
        supprimer_propositions(actuel["propositions"])


@frappe.whitelist()
def lancer(item_code: str) -> dict:
    """Met la synchronisation de l'article dans la file et rend la main tout de suite."""
    item = _article(item_code)
    if not _a_un_site(item_code):
        frappe.throw(_("Aucun site WooCommerce n’est défini sur l’article {0}.").format(item_code))
    occupe = _occupe(item_code)
    if occupe:
        return occupe
    _jeter_les_propositions(item_code)
    return _mettre_en_file(item_code, "synchro", etapes_prevues(item), "executer")


@frappe.whitelist()
def images(item_code: str) -> list:
    """Les images de l'article, pour la fenêtre de retouche manuelle : adresse, traitée ou non, principale ou non,
    et l'image d'avant quand une retouche l'a remplacée."""
    frappe.has_permission("Item", "read", item_code, throw=True)
    from woocommerce_fusion.integrations.content_enrichment import images_de_l_article
    return images_de_l_article(item_code)


def _images_choisies(item_code: str, fichiers) -> list:
    fichiers = frappe.parse_json(fichiers) if isinstance(fichiers, str) else list(fichiers or [])
    connues = {i["name"] for i in images(item_code)}
    fichiers = [f for f in fichiers if f in connues]
    if not fichiers:
        frappe.throw(_("Choisissez au moins une image de cet article."))
    return fichiers


@frappe.whitelist()
def lancer_retouche(item_code: str, fichiers, consignes=None, synchroniser: int | str = 1, zones=None) -> dict:
    """Retouche manuelle, premier temps : les images choisies repassent à l'IA (traitées ou non), chacune avec SA
    consigne (`consignes` = {nom du fichier: texte}, facultatif). Rien n'est remplacé : le résultat attend sa
    validation (`valider_retouches`)."""
    _article(item_code)
    fichiers = _images_choisies(item_code, fichiers)
    consignes = frappe.parse_json(consignes) if isinstance(consignes, str) else dict(consignes or {})
    consignes = {f: str(consignes.get(f) or "").strip() for f in fichiers if str(consignes.get(f) or "").strip()}
    # une zone par image, facultative : (x, y, largeur, hauteur) en parts de l'image — seule cette partie est retouchée
    zones = frappe.parse_json(zones) if isinstance(zones, str) else dict(zones or {})
    zones = {f: list(zones[f]) for f in fichiers if zones.get(f)}
    occupe = _occupe(item_code)
    if occupe:
        return occupe
    _jeter_les_propositions(item_code)
    return _mettre_en_file(item_code, "retouche", ["retouche"], "executer_propositions",
                           en_plus={"synchroniser": cint(cint(synchroniser) and _a_un_site(item_code))},
                           fichiers=fichiers, consignes=consignes, zones=zones)


@frappe.whitelist()
def valider_retouches(item_code: str, gardees=None) -> dict:
    """Retouche manuelle, second temps : `gardees` = les images (nom de l'ANCIEN fichier) dont on garde la retouche.
    Les autres propositions sont supprimées. Puis l'article part sur le site si c'était demandé au lancement."""
    item = _article(item_code)
    actuel = _lire(item_code)
    if not actuel or actuel.get("etat") != A_VALIDER:
        frappe.throw(_("Aucune retouche n’attend de validation pour cet article."))
    from woocommerce_fusion.integrations.content_enrichment import cle_de_proposition
    gardees = frappe.parse_json(gardees) if isinstance(gardees, str) else list(gardees or [])
    propositions = actuel.get("propositions") or []
    gardees = [g for g in gardees if g in {cle_de_proposition(p) for p in propositions}]
    if not gardees:            # tout est refusé : rien à faire d'autre que jeter les propositions
        _jeter_les_propositions(item_code)
        actuel.update(etat="terminee", fin=str(now_datetime()), propositions=[], fait="Propositions refusées",
                      resume=["aucune image gardée : l’article reste tel qu’il était"])
        return _ecrire(actuel)
    synchroniser = cint(actuel.get("synchroniser"))
    return _mettre_en_file(item_code, "validation", etapes_apres("application", item, bool(synchroniser)), "executer_validation",
                           propositions=propositions, gardees=gardees, synchroniser=synchroniser)


def _sources(sources) -> list:
    """Les photos sources d'une composition : des fiches Fichier que l'utilisateur a le droit de lire."""
    sources = frappe.parse_json(sources) if isinstance(sources, str) else list(sources or [])
    lisibles = []
    for nom in sources:
        if frappe.db.exists("File", nom) and frappe.has_permission("File", "read", nom):
            lisibles.append(nom)
    if not lisibles:
        frappe.throw(_("Ajoutez au moins une photo source."))
    return lisibles


@frappe.whitelist()
def lancer_composition(item_code: str, sources, consigne: str | None = None, synchroniser: int | str = 1) -> dict:
    """« Composer une image » : une image de l'article est composée à partir de photos d'autres articles ou d'images
    déposées (`sources` = noms de fiches Fichier). Le résultat attend sa validation, comme une retouche."""
    _article(item_code)
    sources = _sources(sources)
    occupe = _occupe(item_code)
    if occupe:
        return occupe
    _jeter_les_propositions(item_code)
    return _mettre_en_file(item_code, "composition", ["composition"], "executer_composition",
                           en_plus={"synchroniser": cint(cint(synchroniser) and _a_un_site(item_code))},
                           sources=sources, consigne=(consigne or "").strip())


@frappe.whitelist()
def restaurer(item_code: str, fichiers, synchroniser: int | str = 1) -> dict:
    """« Revenir à l'image d'avant » pour les images choisies, puis synchronisation si demandée."""
    item = _article(item_code)
    fichiers = _images_choisies(item_code, fichiers)
    occupe = _occupe(item_code)
    if occupe:
        return occupe
    _jeter_les_propositions(item_code)
    synchroniser = cint(cint(synchroniser) and _a_un_site(item_code))
    return _mettre_en_file(item_code, "restauration", etapes_apres("restauration", item, bool(synchroniser)), "executer_restauration",
                           fichiers=fichiers, synchroniser=synchroniser)


@frappe.whitelist()
def enregistrer_images(item_code: str, ordre, hors_site=None, synchroniser: int | str = 1) -> dict:
    """Fenêtre « Images : ordre et site » : l'ordre des images (la première devient l'image principale), celles qui
    ne partent pas sur le site, puis la synchronisation de l'article si demandée."""
    item = _article(item_code)
    occupe = _occupe(item_code)
    if occupe:
        return occupe
    from woocommerce_fusion.integrations.content_enrichment import enregistrer_rangement
    ordre = frappe.parse_json(ordre) if isinstance(ordre, str) else list(ordre or [])
    hors_site = frappe.parse_json(hors_site) if isinstance(hors_site, str) else list(hors_site or [])
    resultat = enregistrer_rangement(item_code, ordre, hors_site)
    if cint(synchroniser) and _a_un_site(item_code):
        _jeter_les_propositions(item_code)
        return _mettre_en_file(item_code, "synchro", etapes_prevues(item), "executer")
    return dict(resultat, enregistre=1)


# ── le travail, dans la file ─────────────────────────────────────────────────
def _travailler(item_code: str, travail) -> None:
    """`travail()` peut renvoyer de quoi compléter l'état final (par défaut : « terminée »)."""
    frappe.flags.wcf_suivi = item_code
    try:
        try:        # une image principale qui pointe sur un fichier disparu fait échouer chaque enregistrement
            from woocommerce_fusion.integrations.content_enrichment import reparer_image_principale
            reparer_image_principale(item_code)
        except Exception:
            frappe.log_error(title="Synchronisation manuelle : image principale", message=frappe.get_traceback())
        final = travail() or {}
        frappe.db.commit()
        e = _lire(item_code) or {"item": item_code}
        e.update(dict({"etat": "terminee", "fin": str(now_datetime()), "detail": "", "message": ""}, **final))
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


def executer(item_code: str) -> None:
    from woocommerce_fusion.tasks.sync_items import run_item_sync
    _travailler(item_code, lambda: run_item_sync(item_code=item_code) and None)


def executer_propositions(item_code: str, fichiers: list, consignes: dict | None = None, zones: dict | None = None) -> None:
    from woocommerce_fusion.integrations.content_enrichment import proposer_retouches

    def travail():
        propositions, echecs = proposer_retouches(item_code, fichiers, consignes, zones)
        if echecs:
            avertir("%d image(s) non retouchée(s) : %s. Elles restent telles qu’elles étaient." % (len(echecs), echecs[0]))
        if not propositions:
            return {"fait": "Aucune retouche obtenue"}
        return {"etat": A_VALIDER, "propositions": propositions}

    _travailler(item_code, travail)


def executer_composition(item_code: str, sources: list, consigne: str = "") -> None:
    from woocommerce_fusion.integrations.content_enrichment import proposer_compositions

    def travail():
        etape("composition")
        resultat = proposer_compositions([item_code], sources, consigne).get(item_code) or {"echec": "article introuvable"}
        if resultat.get("echec"):
            avertir("Image non composée : %s." % resultat["echec"])
            return {"fait": "Aucune image obtenue"}
        return {"etat": A_VALIDER, "propositions": [resultat]}

    _travailler(item_code, travail)


def executer_validation(item_code: str, propositions: list, gardees: list, synchroniser: int = 1) -> None:
    from woocommerce_fusion.integrations.content_enrichment import appliquer_retouches
    from woocommerce_fusion.tasks.sync_items import run_item_sync

    def travail():
        appliquer_retouches(item_code, propositions, gardees)
        if cint(synchroniser):
            run_item_sync(item_code=item_code)
        return {"propositions": []}

    _travailler(item_code, travail)


def executer_restauration(item_code: str, fichiers: list, synchroniser: int = 1) -> None:
    from woocommerce_fusion.integrations.content_enrichment import restaurer_images
    from woocommerce_fusion.tasks.sync_items import run_item_sync

    def travail():
        restaurer_images(item_code, fichiers)
        if cint(synchroniser):
            run_item_sync(item_code=item_code)

    _travailler(item_code, travail)
