from time import sleep
from typing import List, Optional

import frappe
from erpnext.stock.doctype.item_price.item_price import ItemPrice
from frappe import qb
from frappe.query_builder import Criterion
from frappe.utils import flt, nowdate

from woocommerce_fusion.tasks.sync import SynchroniseWooCommerce
from woocommerce_fusion.tasks.utils import APIWithRequestLogging
from woocommerce_fusion.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)


def update_item_price_for_woocommerce_item_from_hook(doc, method):
	if not frappe.flags.in_test:
		if doc.doctype == "Item Price":
			# item_price_doc n'est PLUS transmis : la synchro re-requête la bonne
			# ligne de prix standard — la sauvegarde d'un prix client-spécifique ou
			# expiré ne doit jamais écraser le prix public de la boutique.
			frappe.enqueue(
				"woocommerce_fusion.tasks.sync_item_prices.run_item_price_sync",
				enqueue_after_commit=True,
				item_code=doc.item_code,
				job_id=f"wc_price_sync::{doc.item_code}",
				deduplicate=True,
			)


@frappe.whitelist()
def run_item_price_sync_in_background():
	# 7200 s : ~1000 liens × (délai par article + 2 requêtes) ne tient pas en 1 h —
	# le timeout de 3600 tuait le job avant la fin de la liste (queue de la liste
	# jamais synchronisée).
	frappe.enqueue(run_item_price_sync, queue="long", timeout=7200)


@frappe.whitelist()
def run_item_price_sync(
	item_code: Optional[str] = None, item_price_doc: Optional[ItemPrice] = None
):
	# item_price_doc est conservé pour compatibilité d'appel mais ignoré (voir hook).
	sync = SynchroniseItemPrice(item_code=item_code)
	sync.run()
	return True


class SynchroniseItemPrice(SynchroniseWooCommerce):
	"""
	Class for managing synchronisation of ERPNext Items with WooCommerce Products
	"""

	item_code: Optional[str]
	item_price_list: List

	def __init__(
		self,
		servers: List[WooCommerceServer | frappe._dict] = None,
		item_code: Optional[str] = None,
	) -> None:
		super().__init__(servers)
		self.item_code = item_code
		self.wc_server = None
		self.item_price_list = []

	def run(self) -> None:
		"""
		Run synchornisation
		"""
		for server in self.servers:
			self.wc_server = server
			self.get_erpnext_item_prices()
			self.sync_items_with_woocommerce_products()

	def get_erpnext_item_prices(self) -> None:
		"""
		Get list of ERPNext Item Prices to synchronise.

		Seule la ligne de prix PUBLIQUE et VALIDE compte : prix de vente, sans
		client attaché, dans la fenêtre valid_from/valid_upto. Sans ces filtres,
		un prix client-spécifique ou expiré de la même liste écrasait le prix de
		la boutique.
		"""
		self.item_price_list = []
		if (
			self.wc_server.enable_sync
			and self.wc_server.enable_price_list_sync
			and self.wc_server.price_list
		):
			ip = qb.DocType("Item Price")
			iwc = qb.DocType("Item WooCommerce Server")
			item = qb.DocType("Item")
			aujourd_hui = nowdate()
			and_conditions = []
			and_conditions.append(ip.price_list == self.wc_server.price_list)
			and_conditions.append(iwc.woocommerce_server == self.wc_server.name)
			and_conditions.append(item.disabled == 0)
			and_conditions.append(iwc.woocommerce_id.isnotnull())
			# un woocommerce_id VIDE passe isnotnull() puis fait planter la
			# construction du nom — 31 liens en base étaient dans ce cas
			and_conditions.append(iwc.woocommerce_id != "")
			and_conditions.append(iwc.enabled == 1)
			and_conditions.append(ip.selling == 1)
			and_conditions.append(ip.customer.isnull())
			and_conditions.append(ip.valid_from.isnull() | (ip.valid_from <= aujourd_hui))
			and_conditions.append(ip.valid_upto.isnull() | (ip.valid_upto >= aujourd_hui))
			if self.item_code:
				and_conditions.append(ip.item_code == self.item_code)

			self.item_price_list = (
				qb.from_(ip)
				.inner_join(iwc)
				.on(iwc.parent == ip.item_code)
				.inner_join(item)
				.on(item.name == ip.item_code)
				.select(
					ip.name,
					ip.item_code,
					ip.price_list_rate,
					iwc.woocommerce_server,
					iwc.woocommerce_id,
					item.variant_of,
				)
				.where(Criterion.all(and_conditions))
				.run(as_dict=True)
			)

	def sync_items_with_woocommerce_products(self) -> None:
		"""
		Synchronise Item Prices with WooCommerce Products.

		GET puis PUT directs sur le bon endpoint : une VARIANTE WooCommerce ne
		répond que sur products/{parent}/variations/{id} — l'ancien chemin
		(products/{id} pour tout le monde) faisait échouer chaque variante,
		les 335 variantes liées n'ont donc jamais eu leur prix synchronisé.
		Même mécanique que tasks/stock_update.py.
		"""
		if not self.item_price_list:
			return

		wc_api = APIWithRequestLogging(
			url=self.wc_server.woocommerce_server_url,
			consumer_key=self.wc_server.api_consumer_key,
			consumer_secret=self.wc_server.api_consumer_secret,
			version="wc/v3",
			timeout=40,
		)

		for item_price in self.item_price_list:
			try:
				endpoint = self._endpoint_produit(item_price)
				if not endpoint:
					continue  # variante sans parent lié : déjà loggé

				response = wc_api.get(endpoint)
				if response.status_code != 200:
					raise ValueError(
						f"GET {endpoint} -> {response.status_code}: {response.text[:300]}")

				prix_woo = flt(response.json().get("regular_price") or 0)
				prix_erp = flt(item_price.price_list_rate)
				if abs(prix_woo - prix_erp) > 0.0005:
					reponse_put = wc_api.put(endpoint, data={"regular_price": str(prix_erp)})
					if reponse_put.status_code != 200:
						raise ValueError(
							f"PUT {endpoint} -> {reponse_put.status_code}: {reponse_put.text[:300]}")
			except Exception:
				frappe.log_error(
					f"WooCommerce Price Sync Error: {item_price.item_code}"[:100],
					frappe.get_traceback(),
				)

			sleep(self.wc_server.price_list_delay_per_item)

	def _endpoint_produit(self, item_price) -> Optional[str]:
		"""products/{id}, ou products/{parent}/variations/{id} pour une variante."""
		if not item_price.variant_of:
			return f"products/{item_price.woocommerce_id}"

		parent_item = frappe.get_doc("Item", item_price.variant_of)
		parent_woocommerce_id = None
		for parent_wc_site in parent_item.woocommerce_servers:
			if parent_wc_site.woocommerce_server == item_price.woocommerce_server:
				parent_woocommerce_id = parent_wc_site.woocommerce_id
				break
		if not parent_woocommerce_id:
			frappe.log_error(
				f"WooCommerce Price Sync Error: {item_price.item_code}"[:100],
				f"Variante {item_price.item_code}: le modèle {item_price.variant_of} "
				f"n'a pas de woocommerce_id pour {item_price.woocommerce_server} — "
				f"prix non synchronisé.",
			)
			return None
		return f"products/{parent_woocommerce_id}/variations/{item_price.woocommerce_id}"
