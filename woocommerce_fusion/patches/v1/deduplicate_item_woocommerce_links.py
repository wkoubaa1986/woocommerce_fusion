"""
Désactive les liens Item ↔ WooCommerce en doublon.

« Dupliquer » un article recopiait la table woocommerce_servers (no_copy=0
jusqu'à la v3.2.1) : plusieurs articles ERPNext poussaient donc leur prix vers
le MÊME produit Woo, et le dernier passage du cron gagnait — le prix boutique
changeait d'une nuit à l'autre (constaté le 26/08/2026 sur 5 produits, ex. le
produit 2959 a reçu 1,5 puis 2,4 puis 2,2 DT dans la même nuit).

Pour chaque produit Woo référencé par plusieurs liens actifs, le SKU enregistré
sur la boutique désigne l'article légitime (convention vérifiée : SKU boutique
= item_code ERPNext) ; les autres liens sont désactivés (enabled=0), jamais
supprimés. Sans arbitre fiable — produit introuvable, SKU vide, SKU ne
correspondant à aucun ou à plusieurs articles du groupe, API injoignable ou
clés invalides — le groupe est loggé et laissé INTACT : on ne devine jamais.

Le patch n'échoue pas sur erreur réseau : il ne doit jamais bloquer un migrate.
Idempotent : une fois les doublons résolus, il ne reste plus de groupe.
"""

import frappe


def execute():
	groupes = frappe.db.sql(
		"""SELECT iws.woocommerce_server, iws.woocommerce_id, COUNT(*) AS n
		   FROM `tabItem WooCommerce Server` iws
		   JOIN `tabItem` i ON i.name = iws.parent
		   WHERE iws.enabled = 1 AND i.disabled = 0
		     AND IFNULL(iws.woocommerce_id, '') != ''
		   GROUP BY iws.woocommerce_server, iws.woocommerce_id
		   HAVING n > 1""",
		as_dict=True,
	)
	if not groupes:
		print("[deduplicate_item_woocommerce_links] aucun doublon de lien.")
		return

	resolus, ignores = 0, 0
	for groupe in groupes:
		liens = frappe.db.sql(
			"""SELECT iws.name, iws.parent
			   FROM `tabItem WooCommerce Server` iws
			   JOIN `tabItem` i ON i.name = iws.parent
			   WHERE iws.enabled = 1 AND i.disabled = 0
			     AND iws.woocommerce_server = %(srv)s
			     AND iws.woocommerce_id = %(wid)s""",
			{"srv": groupe.woocommerce_server, "wid": groupe.woocommerce_id},
			as_dict=True,
		)
		sku = _sku_boutique(groupe.woocommerce_server, groupe.woocommerce_id)
		a_garder = choisir_lien_a_garder(sku, liens)
		if not a_garder:
			ignores += 1
			frappe.log_error(
				f"Dédoublonnage lien Woo {groupe.woocommerce_id}: non résolu"[:100],
				f"Produit {groupe.woocommerce_id} ({groupe.woocommerce_server}) : "
				f"SKU boutique = {sku!r}, articles liés = "
				f"{[lien.parent for lien in liens]}. Aucun arbitrage sûr — liens "
				f"laissés intacts, à trancher à la main.",
			)
			continue

		for lien in liens:
			if lien.parent != a_garder:
				frappe.db.set_value(
					"Item WooCommerce Server", lien.name, "enabled", 0, update_modified=False
				)
		resolus += 1
		print(
			f"[deduplicate_item_woocommerce_links] {groupe.woocommerce_id}: "
			f"garde {a_garder}, désactive "
			f"{[lien.parent for lien in liens if lien.parent != a_garder]}"
		)

	frappe.db.commit()
	print(
		f"[deduplicate_item_woocommerce_links] {resolus} produit(s) résolu(s), "
		f"{ignores} laissé(s) intact(s) (voir Error Log)."
	)


def choisir_lien_a_garder(sku, liens):
	"""Nom de l'article à garder : celui dont l'item_code égale le SKU boutique.
	None si le SKU est vide ou ne désigne pas exactement un article du groupe."""
	if not sku:
		return None
	candidats = [lien.parent for lien in liens if lien.parent == sku]
	return candidats[0] if len(candidats) == 1 else None


def _sku_boutique(server_name, woo_id):
	"""SKU du produit côté boutique, ou None si irrécupérable (404, réseau, clés)."""
	try:
		from woocommerce_fusion.tasks.utils import APIWithRequestLogging

		srv = frappe.get_doc("WooCommerce Server", server_name)
		api = APIWithRequestLogging(
			url=srv.woocommerce_server_url,
			consumer_key=srv.api_consumer_key,
			consumer_secret=srv.api_consumer_secret,
			version="wc/v3",
			timeout=20,
		)
		response = api.get(f"products/{woo_id}")
		if response.status_code != 200:
			return None
		return (response.json().get("sku") or "").strip() or None
	except Exception:
		return None
