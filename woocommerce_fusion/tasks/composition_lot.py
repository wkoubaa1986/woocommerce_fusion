"""« Composer des images » — la page /app/composer-images : composer l'image de PLUSIEURS articles d'un coup.

Demande du 23/11/2026 : « les articles qui n'ont pas d'image, je peux recomposer l'image en prenant plusieurs images
des produits ou des images que j'ajoute moi-même (cas des FRP) ; est-ce que je peux le paramétrer sur plusieurs
articles et le lancer en // » — puis : « créer une page pour ça, c'est une bonne idée lorsque je crée plusieurs
images ».

- `articles()` liste les articles (sans image par défaut) avec, pour chacun, les photos sources PROPOSÉES d'après son
  nom et ses attributs (sa bouteille FRP, sa vanne, son bac à sel) ; l'utilisateur les corrige dans la page ;
- `lancer()` met le lot dans la file longue : les images sont composées quatre à la fois ; l'avancement est écrit
  dans le cache (une entrée par lot, gardée un jour) et relu par la page ;
- chaque image est une PROPOSITION : `valider()` ajoute aux articles celles que l'utilisateur garde, jette les
  autres, et envoie les articles sur le site si c'est demandé. Chaque article porte aussi son état « à valider »
  (suivi_synchro) : on peut finir la vérification depuis sa fiche.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from woocommerce_fusion.tasks import suivi_synchro as S

CLE = "wcf_lot:"
GARDE = 24 * 3600
LOT_MAX = 60                 # articles par lot
DUREE_JOB = 60 * 60


def _lire(lot: str) -> dict | None:
    return frappe.cache().get_value(CLE + lot, expires=True)


def _ecrire(etat: dict) -> dict:
    etat["maj"] = str(now_datetime())
    frappe.cache().set_value(CLE + etat["lot"], etat, expires_in_sec=GARDE)
    return etat


def _droit():
    if not frappe.has_permission("Item", "write"):
        frappe.throw(_("Vous n’avez pas le droit de modifier les articles."), frappe.PermissionError)


@frappe.whitelist()
def articles(groupe: str | None = None, sans_image: int | str = 1, texte: str | None = None, limite: int | str = 200) -> list:
    """Les articles à illustrer, avec les photos sources proposées pour chacun."""
    _droit()
    from woocommerce_fusion.integrations.content_enrichment import sources_proposees

    conditions, valeurs = ["i.disabled = 0"], []
    if cint(sans_image):
        conditions.append("ifnull(i.image, '') = ''")
    if groupe:
        groupes = frappe.db.get_descendants("Item Group", groupe) + [groupe]
        conditions.append("i.item_group in %s")
        valeurs.append(groupes)
    if (texte or "").strip():
        conditions.append("(i.name like %s or i.item_name like %s)")
        valeurs += ["%" + texte.strip() + "%"] * 2
    lignes = frappe.db.sql(
        """select i.name, i.item_name, i.item_group, i.image, i.has_variants, i.variant_of
           from `tabItem` i where {0} order by i.item_group, i.name limit {1}""".format(" and ".join(conditions), min(cint(limite) or 200, 500)),
        valeurs, as_dict=True)
    for l in lignes:
        l["sources"] = sources_proposees(l.name)
    return lignes


@frappe.whitelist()
def images_de(item_code: str) -> list:
    """Les images d'un article, pour les prendre comme photos sources."""
    return S.images(item_code)


@frappe.whitelist()
def lancer(lignes, consigne: str | None = None, synchroniser: int | str = 0) -> dict:
    """`lignes` = [{"article": code, "sources": [fiches Fichier]}]. Rend la main tout de suite : {"lot": identifiant}."""
    _droit()
    lignes = frappe.parse_json(lignes) if isinstance(lignes, str) else list(lignes or [])
    travaux = {}
    for l in lignes[:LOT_MAX]:
        code, sources = l.get("article"), [s for s in (l.get("sources") or []) if frappe.db.exists("File", s)]
        if code and sources and frappe.db.exists("Item", code):
            travaux[code] = sources[:6]
    if not travaux:
        frappe.throw(_("Cochez au moins un article qui a des photos sources."))
    lot = frappe.generate_hash(length=12)
    etat = _ecrire({"lot": lot, "etat": "en_cours", "total": len(travaux), "faits": 0, "par": frappe.session.user,
                    "debut": str(now_datetime()), "consigne": (consigne or "").strip(), "synchroniser": cint(synchroniser),
                    "articles": list(travaux), "resultats": {}})
    frappe.enqueue("woocommerce_fusion.tasks.composition_lot.executer", queue="long", timeout=DUREE_JOB,
                   enqueue_after_commit=True, lot=lot, travaux=travaux, consigne=(consigne or "").strip())
    return etat


@frappe.whitelist()
def etat(lot: str) -> dict | None:
    _droit()
    e = _lire(lot)
    if e:
        e["maintenant"] = str(now_datetime())
    return e


def executer(lot: str, travaux: dict, consigne: str = "") -> None:
    from woocommerce_fusion.integrations.content_enrichment import proposer_compositions_par_article

    def avancement(faits, total):
        e = _lire(lot) or {"lot": lot}
        e.update(faits=faits, total=total)
        _ecrire(e)

    e = _lire(lot) or {"lot": lot}
    try:
        resultats = proposer_compositions_par_article(travaux, consigne, avancement)
        noms = dict(frappe.db.sql("select name, item_name from `tabItem` where name in %s", (list(travaux),)))
        for code, r in resultats.items():
            r["article"], r["nom"] = code, noms.get(code, code)
            if not r.get("echec"):            # la fiche de l'article montre aussi « 1 image prête : à vérifier »
                S._ecrire(dict({"item": code, "genre": "composition", "etat": S.A_VALIDER, "etape": "composition",
                                "libelle": S.ETAPES["composition"], "detail": "", "etapes": ["composition"], "rang": 1, "total": 1,
                                "debut": e.get("debut"), "fin": None, "par": e.get("par"), "avertissements": [], "resume": [],
                                "message": "", "propositions": [r], "synchroniser": cint(e.get("synchroniser")), "lot": lot},
                               **S.LIBELLES["composition"]))
        e = _lire(lot) or e
        e.update(etat="a_valider", fin=str(now_datetime()), resultats=resultats, faits=len(resultats))
        _ecrire(e)
    except Exception as erreur:
        frappe.db.rollback()
        frappe.log_error(title="Composer des images : échec du lot", message=frappe.get_traceback())
        e = _lire(lot) or e
        e.update(etat="echec", fin=str(now_datetime()), message=S.cause_lisible(erreur))
        _ecrire(e)


@frappe.whitelist()
def valider(lot: str, gardees=None, synchroniser: int | str = 0) -> dict:
    """Ajoute aux articles les images gardées (`gardees` = codes d'article), jette les autres, puis envoie sur le
    site les articles gardés si `synchroniser`."""
    _droit()
    from woocommerce_fusion.integrations.content_enrichment import appliquer_retouches, cle_de_proposition, supprimer_propositions

    e = _lire(lot)
    if not e or e.get("etat") != "a_valider":
        frappe.throw(_("Ce lot n’attend pas de validation."))
    gardees = set(frappe.parse_json(gardees) if isinstance(gardees, str) else (gardees or []))
    ajoutees, jetees, envoyees = [], 0, 0
    for code, r in (e.get("resultats") or {}).items():
        if r.get("echec") or r.get("traite"):
            continue
        actuel = S._lire(code) or {}
        encore_la = actuel.get("etat") == S.A_VALIDER and any(p.get("proposition") == r["proposition"] for p in actuel.get("propositions") or [])
        if not encore_la:                      # déjà traitée depuis la fiche de l'article
            r["traite"] = "ailleurs"
            continue
        if code in gardees:
            appliquer_retouches(code, [r], [cle_de_proposition(r)])
            ajoutees.append(code)
            r["traite"] = "gardee"
        else:
            supprimer_propositions([r])
            jetees += 1
            r["traite"] = "jetee"
        actuel.update(etat="terminee", fin=str(now_datetime()), propositions=[],
                      fait="Image composée ajoutée" if code in gardees else "Image composée refusée",
                      resume=["image ajoutée à l’article"] if code in gardees else ["image refusée : l’article reste tel qu’il était"])
        S._ecrire(actuel)
    frappe.db.commit()
    if cint(synchroniser):
        for code in ajoutees:
            try:
                if S._a_un_site(code) and not S._occupe(code):
                    S._mettre_en_file(code, "synchro", S.etapes_prevues(S._article(code)), "executer")
                    envoyees += 1
            except Exception:
                frappe.log_error(title="Composer des images : envoi sur le site (%s)" % code, message=frappe.get_traceback())
    e.update(etat="termine", valide_le=str(now_datetime()))
    _ecrire(e)
    return {"ajoutees": len(ajoutees), "jetees": jetees, "envoyees": envoyees}
