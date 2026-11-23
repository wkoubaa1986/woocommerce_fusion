"""Réglages de l'IA : un modèle par étape dans « AI Settings », et le champ « image d'avant » des fichiers.

Demande du 23/11/2026 : « remets tout en setting », avec des modèles récents de la gamme Sol / Luna d'OpenAI, à coût
équivalent. Avant, « AI Settings » ne réglait que le modèle de rédaction et celui de retouche ; la recherche sur
internet, les mots-clés, le poids et le texte alternatif utilisaient des modèles écrits en dur dans le code.

Tarifs relevés sur la page officielle d'OpenAI le 23/11/2026 (dollars par million de jetons, entrée / sortie) :
    gpt-5.2 1,75 / 14      gpt-6.2-sol 2 / 10        gpt-6.1-terra 1 / 5
    gpt-5-mini 0,25 / 2    gpt-4.1-mini 0,40 / 1,60  gpt-4o-mini 0,15 / 0,60     gpt-6-luna 0,10 / 0,50
Les modèles gpt-6 ont été essayés avec la clé du site : réponses JSON et recherche web acceptées, température refusée
(content_enrichment.accepte_temperature).

`installer()` est rejouable : il n'ajoute que ce qui manque et ne remplace une valeur que si elle est encore celle
d'avant (un choix fait à la main dans le réglage est toujours respecté).

⚠️ Les champs d'« AI Settings » doivent AUSSI être dans `fixtures/ai_settings_doctype.json` : à chaque migration,
Frappe réimporte tous les fichiers du dossier fixtures, APRÈS les patchs. Le 23/11/2026 le patch avait ajouté les
quatre champs en prod et la migration les a aussitôt retirés en réimportant l'ancienne définition : les valeurs
restaient en base mais le code, ne voyant plus les champs, reprenait les anciens modèles sans rien dire. Le test
`test_les_champs_sont_aussi_dans_la_fixture` garde les deux alignés.
"""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

REGLAGE = "AI Settings"

# Champ -> (libellé, modèle posé, modèles d'avant qu'on remplace, explication affichée sous le champ)
MODELES = {
    "open_ai_model": (
        "Rédaction des textes du site, SEO et classement", "gpt-6.2-sol", ("gpt-5.2",),
        "Le modèle principal. gpt-6.2-sol : 2 $ / 10 $ par million de jetons (avant : gpt-5.2, 1,75 $ / 14 $)."),
    "web_search_model": (
        "Recherche des caractéristiques sur internet", "gpt-6-luna", (),
        "gpt-6-luna : 0,10 $ / 0,50 $ (avant : gpt-4.1-mini, 0,40 $ / 1,60 $). Chaque recherche coûte en plus 0,01 $."),
    "keywords_model": (
        "Choix des mots-clés", "gpt-6-luna", (),
        "gpt-6-luna : 0,10 $ / 0,50 $ (avant : gpt-5-mini, 0,25 $ / 2 $). Si les mots-clés déçoivent : gpt-6.1-terra (1 $ / 5 $)."),
    "logistics_model": (
        "Poids et classe de livraison", "gpt-6-luna", (),
        "gpt-6-luna : 0,10 $ / 0,50 $ (avant : gpt-5-mini, 0,25 $ / 2 $)."),
    "alt_model": (
        "Texte alternatif des images", "gpt-6-luna", (),
        "gpt-6-luna : 0,10 $ / 0,50 $ (avant : gpt-4o-mini, 0,15 $ / 0,60 $)."),
}
NOUVEAUX = ("web_search_model", "keywords_model", "logistics_model", "alt_model")
SECTION = ("modeles_section", "Un modèle par étape (OpenAI)")
GEMINI = ("Retouche des images (Gemini)",
          "Modèle d’image de Google. gemini-3-pro-image-preview : environ 0,13 $ par image retouchée.")


def _champs_du_reglage() -> bool:
    """Ajoute les champs manquants à « AI Settings » (type de document créé sur le site) et reprend ses libellés."""
    if not frappe.db.exists("DocType", REGLAGE):
        return False
    doc = frappe.get_doc("DocType", REGLAGE)
    presents = {f.fieldname: f for f in doc.fields}
    change = False
    if SECTION[0] not in presents:
        doc.append("fields", {"fieldname": SECTION[0], "fieldtype": "Section Break", "label": SECTION[1]})
        change = True
    for champ in NOUVEAUX:
        if champ not in presents:
            doc.append("fields", {"fieldname": champ, "fieldtype": "Data", "label": MODELES[champ][0],
                                  "default": MODELES[champ][1], "description": MODELES[champ][3]})
            change = True
    for champ, (libelle, _modele, _avant, explication) in MODELES.items():
        f = presents.get(champ)
        if f and (f.label != libelle or (f.description or "") != explication):
            f.label, f.description = libelle, explication
            change = True
    f = presents.get("gemini_model")
    if f and (f.label != GEMINI[0] or (f.description or "") != GEMINI[1]):
        f.label, f.description = GEMINI
        change = True
    if change:
        doc.save(ignore_permissions=True)
    return change


def _valeurs() -> dict:
    """Pose les modèles : un champ vide prend le nouveau modèle ; un champ qui porte encore le modèle d'avant aussi."""
    posees = {}
    for champ, (_libelle, modele, avant, _explication) in MODELES.items():
        actuel = (frappe.db.get_single_value(REGLAGE, champ) or "").strip()
        if not actuel or actuel in avant:
            frappe.db.set_single_value(REGLAGE, champ, modele)
            posees[champ] = modele
    return posees


def _champ_image_avant() -> None:
    """Sur la fiche Fichier : le fichier que celui-ci a remplacé (« Revenir à l'image d'avant »), son rang sur
    l'article et « ne pas envoyer sur le site »."""
    apres = "custom_treated_ai" if frappe.db.has_column("File", "custom_treated_ai") else "file_url"
    create_custom_fields({"File": [
        {"fieldname": "custom_image_avant", "label": "Image d’avant la retouche", "fieldtype": "Data",
         "insert_after": apres, "read_only": 1, "no_copy": 1,
         "description": "Le fichier que cette image retouchée a remplacé. Rempli tout seul."},
        # Rangement des images d'un article (fenêtre « Images : ordre et site » de la fiche article).
        {"fieldname": "custom_ordre_image", "label": "Rang de l’image sur l’article", "fieldtype": "Int",
         "insert_after": "custom_image_avant", "no_copy": 1,
         "description": "1 = image principale. 0 = jamais rangée. Se règle depuis la fiche article."},
        {"fieldname": "custom_hors_site", "label": "Ne pas envoyer sur le site", "fieldtype": "Check",
         "insert_after": "custom_ordre_image", "no_copy": 1,
         "description": "L’image reste dans ERPNext et n’est pas envoyée sur le site. Se règle depuis la fiche article."},
    ]}, update=True)


def installer() -> dict:
    _champ_image_avant()
    posees = {}
    if _champs_du_reglage() or frappe.db.exists("DocType", REGLAGE):
        frappe.clear_cache(doctype=REGLAGE)
        posees = _valeurs()
    return posees
