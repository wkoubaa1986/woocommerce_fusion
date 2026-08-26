"""
Recrée des produits WooCommerce dédiés pour Tuy-011, Tuy-021 et Tuy-024.

Ces trois tubes pointaient sur le MÊME produit Woo 2959 (SKU boutique =
Tuy-022) : deduplicate_item_woocommerce_links a désactivé leurs liens.
Décision utilisateur du 26/08/2026 : chacun doit exister sur la boutique
comme produit à part entière, avec enrichissement de contenu.

Pour chaque article dont le lien est encore désactivé et pointe sur 2959 :
woocommerce_id vidé puis lien réactivé (ID vide = « créer ce produit »,
comportement documenté du champ), custom_generate_seo coché, et modified
rafraîchi pour que le cron quotidien de 02h (sync_mode="modified") les
embarque à son prochain passage.

Idempotent : une fois l'ID vidé, la condition ne matche plus. Si le lien a
été retouché à la main entre-temps (réactivé ou re-mappé), on ne touche à
rien — le patch ne défait jamais une intervention humaine.
"""

import frappe

ARTICLES = ["Tuy-011", "Tuy-021", "Tuy-024"]
ANCIEN_WOO_ID = "2959"
SERVEUR = "aquaworldservicing.com"


def execute():
	for item_code in ARTICLES:
		lien = frappe.db.get_value(
			"Item WooCommerce Server",
			{"parent": item_code, "parenttype": "Item", "woocommerce_server": SERVEUR},
			["name", "enabled", "woocommerce_id"],
			as_dict=True,
		)
		if not lien or lien.enabled or (lien.woocommerce_id or "") != ANCIEN_WOO_ID:
			print(f"[recreer_produits_tuy] {item_code}: lien absent ou retouché à la main — ignoré.")
			continue

		frappe.db.set_value(
			"Item WooCommerce Server",
			lien.name,
			{"woocommerce_id": None, "enabled": 1},
			update_modified=False,
		)
		# set_value sans update_modified=False : rafraîchit Item.modified, ce qui
		# garantit la sélection par le cron quotidien en mode "modified".
		frappe.db.set_value("Item", item_code, "custom_generate_seo", 1)
		print(
			f"[recreer_produits_tuy] {item_code}: lien réactivé avec ID vide + "
			f"Generate SEO — produit créé à la prochaine synchro articles (02h)."
		)

	frappe.db.commit()
