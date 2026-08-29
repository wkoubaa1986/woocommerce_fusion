import math

import frappe

from woocommerce_fusion.tasks.utils import APIWithRequestLogging


def rupture_forcee(item) -> bool:
	"""La rupture EFFECTIVE d'un article : sa case « Rupture de stock (site
	web) », ou celle de son article MODÈLE — un modèle coché met toutes ses
	variantes en rupture, une variante peut aussi l'être seule (28/08/2026)."""
	if item.get("custom_rupture_site_web"):
		return True
	if item.get("variant_of"):
		return bool(
			frappe.db.get_value("Item", item.variant_of, "custom_rupture_site_web")
		)
	return False


def update_stock_levels_for_woocommerce_item(doc, method):
	if not frappe.flags.in_test:
		if doc.doctype in ("Stock Entry", "Stock Reconciliation", "Sales Invoice", "Delivery Note"):
			# Check if there are any enabled WooCommerce Servers with stock sync enabled.
			# Use frappe.db.count (no permission check): this is a system-level config
			# check and must not depend on the submitting user's read access to
			# "WooCommerce Server" (otherwise low-permission users get a PermissionError
			# when submitting Delivery Notes / Sales Invoices).
			#
			# ⚠️ « rupture_naturelle_stock » (décision 29/08/2026) : en MODE MANUEL
			# (case décochée, le défaut), les mouvements de stock n'ont AUCUN effet
			# sur le site — seule la case « Rupture de stock (site web) » de
			# l'article décide. On n'enqueue donc rien du tout.
			if (
				frappe.db.count(
					"WooCommerce Server",
					{
						"enable_sync": 1,
						"enable_stock_level_synchronisation": 1,
						"rupture_naturelle_stock": 1,
					},
				)
				> 0
			):
				if doc.doctype == "Sales Invoice":
					if doc.update_stock == 0:
						return
				item_codes = [row.item_code for row in doc.items]
				for item_code in item_codes:
					frappe.enqueue(
						"woocommerce_fusion.tasks.stock_update.update_stock_levels_on_woocommerce_site",
						enqueue_after_commit=True,
						item_code=item_code,
					)


def pousser_rupture_depuis_fiche(doc, method=None):
	"""Hook Item.on_update : quand la case « Rupture de stock (site web) »
	bascule, pousse l'état au site SANS attendre un mouvement de stock.

	- article MODÈLE coché/décoché : toutes ses variantes sont (re)poussées —
	  cocher le modèle met tout en rupture, le décocher restaure chaque
	  variante selon son stock réel (sauf celles cochées individuellement) ;
	- article simple ou variante : lui seul.
	La routine de stock reste inchangée : elle repasse par rupture_forcee() à
	chaque mouvement, donc une case cochée continue d'imposer 0."""
	if frappe.flags.in_test or frappe.flags.in_migrate or frappe.flags.in_install:
		return
	avant = doc.get_doc_before_save()
	etat = 1 if doc.get("custom_rupture_site_web") else 0
	if avant is not None and (1 if avant.get("custom_rupture_site_web") else 0) == etat:
		return
	if avant is None and not etat:
		return
	if (
		frappe.db.count(
			"WooCommerce Server", {"enable_sync": 1, "enable_stock_level_synchronisation": 1}
		)
		== 0
	):
		return

	if doc.has_variants:
		cibles = frappe.get_all(
			"Item", filters={"variant_of": doc.name, "disabled": 0}, pluck="name"
		)
		# Le produit variable lui-même sort du catalogue (ou y revient).
		frappe.enqueue(
			"woocommerce_fusion.tasks.stock_update.pousser_visibilite_produit",
			enqueue_after_commit=True,
			item_code=doc.name,
			cacher=etat,
		)
	else:
		cibles = [doc.name]
	for item_code in cibles:
		frappe.enqueue(
			"woocommerce_fusion.tasks.stock_update.update_stock_levels_on_woocommerce_site",
			enqueue_after_commit=True,
			item_code=item_code,
			forcer_statut=True,
		)


def pousser_visibilite_produit(item_code, cacher):
	"""Cache (ou réaffiche) le PRODUIT ENTIER sur le site — pour un article
	MODÈLE coché : ses variations passent en rupture (fan-out) ET le produit
	sort du catalogue ; décoché, il revient. `cacher` : 1/0."""
	item = frappe.get_doc("Item", item_code)
	for wc_site in item.woocommerce_servers:
		if not wc_site.woocommerce_id:
			continue
		wc_server = frappe.get_cached_doc("WooCommerce Server", wc_site.woocommerce_server)
		if (
			not wc_server
			or not wc_server.enable_sync
			or not wc_site.enabled
			or not wc_server.enable_stock_level_synchronisation
		):
			continue
		wc_api = APIWithRequestLogging(
			url=wc_server.woocommerce_server_url,
			consumer_key=wc_server.api_consumer_key,
			consumer_secret=wc_server.api_consumer_secret,
			version="wc/v3",
			timeout=40,
		)
		data_to_post = {
			"catalog_visibility": "hidden" if frappe.utils.cint(cacher) else "visible",
			"stock_status": "outofstock" if frappe.utils.cint(cacher) else "instock",
		}
		response = wc_api.put(endpoint=f"products/{wc_site.woocommerce_id}", data=data_to_post)
		if response.status_code != 200:
			frappe.log_error(
				"WooCommerce Error",
				f"Visibilite produit: statut {response.status_code}\n{response.text}"[:2000],
			)


def update_stock_levels_for_all_enabled_items_in_background():
	"""
	Get all enabled ERPNext Items and post stock updates to WooCommerce
	"""
	erpnext_items = []
	current_page_length = 500
	start = 0

	# Get all items, 500 records at a time
	while current_page_length == 500:
		items = frappe.db.get_all(
			doctype="Item",
			filters={"disabled": 0},
			fields=["name"],
			start=start,
			page_length=500,
		)
		erpnext_items.extend(items)
		current_page_length = len(items)
		start += current_page_length

	for item in erpnext_items:
		frappe.enqueue(
			"woocommerce_fusion.tasks.stock_update.update_stock_levels_on_woocommerce_site",
			item_code=item.name,
		)


@frappe.whitelist()
def update_stock_levels_on_woocommerce_site(item_code, forcer_statut=False):
	"""
	Updates stock levels of an item on all its associated WooCommerce sites.

	This function fetches the item from the database, then for each associated
	WooCommerce site, it retrieves the current inventory, calculates the new stock quantity,
	and posts the updated stock levels back to the WooCommerce site.
	"""
	item = frappe.get_doc("Item", item_code)

	if len(item.woocommerce_servers) == 0 or not item.is_stock_item or item.disabled:
		return False
	else:
		bins = frappe.get_list(
			"Bin", {"item_code": item_code}, ["name", "warehouse", "reserved_qty", "actual_qty"]
		)

		for wc_site in item.woocommerce_servers:
			if wc_site.woocommerce_id:
				woocommerce_id = wc_site.woocommerce_id
				woocommerce_server = wc_site.woocommerce_server
				wc_server = frappe.get_cached_doc("WooCommerce Server", woocommerce_server)

				if (
					not wc_server
					or not wc_server.enable_sync
					or not wc_site.enabled
					or not wc_server.enable_stock_level_synchronisation
				):
					continue

				wc_api = APIWithRequestLogging(
					url=wc_server.woocommerce_server_url,
					consumer_key=wc_server.api_consumer_key,
					consumer_secret=wc_server.api_consumer_secret,
					version="wc/v3",
					timeout=40,
				)

				# Sum all quantities from select warehouses and round the total down (WooCommerce API doesn't accept float values)
				data_to_post = {
					"stock_quantity": math.floor(
						sum(
							bin.actual_qty
							if not wc_server.subtract_reserved_stock
							else bin.actual_qty - bin.reserved_qty
							for bin in bins
							if bin.warehouse in [row.warehouse for row in wc_server.warehouses]
						)
					)
				}
				# Rupture forcée depuis la fiche article : 0 + outofstock, quel que
				# soit le stock réel. Passer par ICI (et non par un envoi ponctuel)
				# garantit qu'un mouvement de stock ultérieur re-pousse 0 tant que
				# la case est cochée, au lieu de restaurer la vraie quantité.
				est_variation = bool(item.variant_of)
				if rupture_forcee(item):
					data_to_post = {"stock_quantity": 0, "stock_status": "outofstock"}
					# L'article en rupture DISPARAÎT du site (décision 28/08/2026) —
					# sauf une variation seule : le produit reste affiché avec ses
					# autres déclinaisons, celle-ci devient juste non sélectionnable
					# (l'endpoint variations n'a d'ailleurs pas catalog_visibility).
					if not est_variation:
						data_to_post["catalog_visibility"] = "hidden"
				elif not bool(wc_server.get("rupture_naturelle_stock")):
					# MODE MANUEL (défaut, décision 29/08/2026) : AUCUNE quantité
					# réelle ne part au site — le stock ERPNext (souvent négatif) ne
					# doit jamais décider de la disponibilité web. L'article non coché
					# est DISPONIBLE, point. manage_stock=False empêche AUSSI le site
					# de se remettre tout seul en rupture quand ses propres compteurs
					# tombent à zéro après des commandes web.
					data_to_post = {"manage_stock": False, "stock_status": "instock"}
					if forcer_statut and not est_variation:
						# Décochage de la case : le produit revient au catalogue.
						data_to_post["catalog_visibility"] = "visible"
				elif forcer_statut:
					# Décochage de la rupture : une variation Woo qui ne gère pas les
					# quantités ignorerait stock_quantity et resterait bloquée en
					# rupture — on renvoie donc AUSSI le statut, déduit du stock réel.
					# (Quand manage_stock est actif côté Woo, ce statut est recalculé
					# de toute façon : l'envoyer est sans effet.)
					data_to_post["stock_status"] = (
						"instock" if data_to_post["stock_quantity"] > 0 else "outofstock"
					)
					if not est_variation:
						data_to_post["catalog_visibility"] = "visible"

				try:
					parent_item_id = item.variant_of
					if parent_item_id:
						parent_item = frappe.get_doc("Item", parent_item_id)
						# Get the parent item's woocommerce_id
						for parent_wc_site in parent_item.woocommerce_servers:
							if parent_wc_site.woocommerce_server == woocommerce_server:
								parent_woocommerce_id = parent_wc_site.woocommerce_id
								break
						if not parent_woocommerce_id:
							continue
						endpoint = f"products/{parent_woocommerce_id}/variations/{woocommerce_id}"
					else:
						endpoint = f"products/{woocommerce_id}"
					response = wc_api.put(endpoint=endpoint, data=data_to_post)
				except Exception as err:
					error_message = f"{frappe.get_traceback()}\n\nData in PUT request: \n{str(data_to_post)}"
					frappe.log_error("WooCommerce Error", error_message)
					raise err
				if response.status_code != 200:
					error_message = f"Status Code not 200\n\nData in PUT request: \n{str(data_to_post)}"
					error_message += (
						f"\n\nResponse: \n{response.status_code}\nResponse Text: {response.text}\nRequest URL: {response.request.url}\nRequest Body: {response.request.body}"
						if response is not None
						else ""
					)
					frappe.log_error("WooCommerce Error", error_message)
					raise ValueError(error_message)

		return True
