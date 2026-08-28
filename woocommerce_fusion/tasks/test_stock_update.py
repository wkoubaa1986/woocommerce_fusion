from unittest.mock import MagicMock, Mock, call, patch

import frappe
from frappe import _dict
from frappe.tests.utils import FrappeTestCase

from woocommerce_fusion.tasks.stock_update import (
	pousser_rupture_depuis_fiche,
	rupture_forcee,
	update_stock_levels_for_all_enabled_items_in_background,
	update_stock_levels_on_woocommerce_site,
)


class TestWooCommerceStockSync(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # important to call super() methods when extending TestCase.

	@patch("woocommerce_fusion.tasks.stock_update.frappe")
	@patch("woocommerce_fusion.tasks.stock_update.APIWithRequestLogging", autospec=True)
	def test_update_stock_levels_on_woocommerce_site(self, mock_wc_api, mock_frappe):
		# Set up a dummy item set to sync to two different WC sites
		some_item = frappe._dict(
			woocommerce_servers=[
				frappe._dict(woocommerce_id=1, woocommerce_server="woo1.example.com", enabled=1),
				frappe._dict(woocommerce_id=2, woocommerce_server="woo2.example.com", enabled=1),
			],
			is_stock_item=1,
			disabled=0,
		)
		mock_frappe.get_doc.return_value = some_item

		# Set up a dummy bin list with stock in two Warehouses
		bin_list = [
			frappe._dict(warehouse="Warehouse A", actual_qty=5),
			frappe._dict(warehouse="Warehouse B", actual_qty=10),
			frappe._dict(warehouse="Warehouse C", actual_qty=20),
		]
		mock_frappe.get_list.return_value = bin_list

		# Set up mock return values
		mock_frappe.get_cached_doc.side_effect = [
			frappe._dict(
				woocommerce_server="woo1.example.com",
				enable_sync=1,
				enable_stock_level_synchronisation=1,
				warehouses=[frappe._dict(warehouse="Warehouse A"), frappe._dict(warehouse="Warehouse B")],
			),
			frappe._dict(
				woocommerce_server="woo2.example.com",
				enable_sync=1,
				enable_stock_level_synchronisation=1,
				warehouses=[frappe._dict(warehouse="Warehouse A"), frappe._dict(warehouse="Warehouse B")],
			),
		]

		# Mock out calls to WooCommerce API's
		mock_put_response = Mock()
		mock_put_response.status_code = 200

		mock_api_instance = MagicMock()
		mock_api_instance.put.return_value = mock_put_response
		mock_wc_api.return_value = mock_api_instance

		# Call function under test
		update_stock_levels_on_woocommerce_site("some_item_code")

		# Assert that the inventories put calls were made with the correct arguments
		self.assertEqual(mock_api_instance.put.call_count, 2)
		actual_put_endpoints = [call.kwargs["endpoint"] for call in mock_api_instance.put.call_args_list]
		actual_put_data = [call.kwargs["data"] for call in mock_api_instance.put.call_args_list]

		expected_put_endpoints = ["products/1", "products/2"]
		expected_data = {"stock_quantity": 15}
		expected_put_data = [expected_data for x in range(2)]
		self.assertEqual(actual_put_endpoints, expected_put_endpoints)
		self.assertEqual(actual_put_data, expected_put_data)

	@patch("woocommerce_fusion.tasks.stock_update.frappe")
	@patch("woocommerce_fusion.tasks.stock_update.APIWithRequestLogging", autospec=True)
	def test_update_stock_levels_on_woocommerce_site_variant(self, mock_wc_api, mock_frappe):
		# Set up a dummy variant item set to sync to a WC site
		variant_item = frappe._dict(
			woocommerce_servers=[
				frappe._dict(woocommerce_id=101, woocommerce_server="woo1.example.com", enabled=1),
			],
			is_stock_item=1,
			disabled=0,
			variant_of="parent_item_code",
		)
		mock_frappe.get_doc.side_effect = [
			variant_item,
			frappe._dict(
				woocommerce_servers=[
					frappe._dict(woocommerce_id=100, woocommerce_server="woo1.example.com", enabled=1),
				]
			),
		]
		mock_frappe.db.get_value.return_value = None  # le modèle n'est pas en rupture

		# Set up a dummy bin list with stock in two Warehouses
		bin_list = [
			frappe._dict(warehouse="Warehouse A", actual_qty=5),
			frappe._dict(warehouse="Warehouse B", actual_qty=10),
		]
		mock_frappe.get_list.return_value = bin_list

		# Set up mock return values
		mock_frappe.get_cached_doc.return_value = frappe._dict(
			woocommerce_server="woo1.example.com",
			enable_sync=1,
			enable_stock_level_synchronisation=1,
			warehouses=[frappe._dict(warehouse="Warehouse A"), frappe._dict(warehouse="Warehouse B")],
		)

		# Mock out calls to WooCommerce API's
		mock_put_response = Mock()
		mock_put_response.status_code = 200

		mock_api_instance = MagicMock()
		mock_api_instance.put.return_value = mock_put_response
		mock_wc_api.return_value = mock_api_instance

		# Call function under test
		update_stock_levels_on_woocommerce_site("variant_item_code")

		# Assert that the inventories put calls were made with the correct arguments
		self.assertEqual(mock_api_instance.put.call_count, 1)
		actual_put_endpoint = mock_api_instance.put.call_args.kwargs["endpoint"]
		actual_put_data = mock_api_instance.put.call_args.kwargs["data"]

		expected_put_endpoint = "products/100/variations/101"
		expected_data = {"stock_quantity": 15}
		self.assertEqual(actual_put_endpoint, expected_put_endpoint)
		self.assertEqual(actual_put_data, expected_data)

	@patch("woocommerce_fusion.tasks.stock_update.frappe.db.get_all")
	@patch("woocommerce_fusion.tasks.stock_update.frappe.enqueue")
	def test_update_stock_levels_for_all_enabled_items_in_background(
		self, mock_enqueue, mock_get_all
	):
		# Set up mock return values
		mock_get_all.side_effect = [
			[_dict({"name": f"Item-1-{x}"}) for x in range(500)],  # First page of results
			[_dict({"name": f"Item-2-{x}"}) for x in range(500)],  # Second page of results
			[],  # No more results, loop should exit
		]

		# Call the function
		update_stock_levels_for_all_enabled_items_in_background()

		# Assertions to check if get_all was called correctly
		self.assertEqual(mock_get_all.call_count, 3)
		expected_calls = [
			call(doctype="Item", filters={"disabled": 0}, fields=["name"], start=0, page_length=500),
			call(doctype="Item", filters={"disabled": 0}, fields=["name"], start=500, page_length=500),
			call(doctype="Item", filters={"disabled": 0}, fields=["name"], start=1000, page_length=500),
		]
		mock_get_all.assert_has_calls(expected_calls, any_order=True)

		# Assertions to check if enqueue was called correctly
		# This assumes we have 1000 items, based on the pagination logic above.
		self.assertEqual(mock_enqueue.call_count, 1000)
		mock_enqueue.assert_called_with(
			"woocommerce_fusion.tasks.stock_update.update_stock_levels_on_woocommerce_site",
			item_code="Item-2-499",  # Here we'd check for the last `item_code` being passed.
		)


class TestRuptureSiteWeb(FrappeTestCase):
	"""La case « Rupture de stock (site web) » force outofstock, quelles que
	soient les quantités — décision utilisateur 28/08/2026."""

	def _montage(self, mock_wc_api, mock_frappe, item, get_value=None):
		mock_frappe.get_doc.return_value = item
		mock_frappe.db.get_value.return_value = get_value
		mock_frappe.get_list.return_value = [frappe._dict(warehouse="Warehouse A", actual_qty=7)]
		mock_frappe.get_cached_doc.return_value = frappe._dict(
			woocommerce_server="woo1.example.com",
			enable_sync=1,
			enable_stock_level_synchronisation=1,
			warehouses=[frappe._dict(warehouse="Warehouse A")],
		)
		reponse = Mock()
		reponse.status_code = 200
		api = MagicMock()
		api.put.return_value = reponse
		mock_wc_api.return_value = api
		return api

	@patch("woocommerce_fusion.tasks.stock_update.frappe")
	@patch("woocommerce_fusion.tasks.stock_update.APIWithRequestLogging", autospec=True)
	def test_rupture_directe_pousse_zero_et_outofstock(self, mock_wc_api, mock_frappe):
		item = frappe._dict(
			woocommerce_servers=[
				frappe._dict(woocommerce_id=1, woocommerce_server="woo1.example.com", enabled=1)
			],
			is_stock_item=1,
			disabled=0,
			custom_rupture_site_web=1,
		)
		api = self._montage(mock_wc_api, mock_frappe, item)
		update_stock_levels_on_woocommerce_site("x")
		# Rupture directe : plus vendable ET retiré du catalogue.
		self.assertEqual(
			api.put.call_args.kwargs["data"],
			{"stock_quantity": 0, "stock_status": "outofstock", "catalog_visibility": "hidden"},
		)

	@patch("woocommerce_fusion.tasks.stock_update.frappe")
	@patch("woocommerce_fusion.tasks.stock_update.APIWithRequestLogging", autospec=True)
	def test_rupture_par_le_modele(self, mock_wc_api, mock_frappe):
		variante = frappe._dict(
			woocommerce_servers=[
				frappe._dict(woocommerce_id=101, woocommerce_server="woo1.example.com", enabled=1)
			],
			is_stock_item=1,
			disabled=0,
			variant_of="MODELE",
			custom_rupture_site_web=0,
		)
		api = self._montage(mock_wc_api, mock_frappe, variante, get_value=1)
		mock_frappe.get_doc.side_effect = [
			variante,
			frappe._dict(
				woocommerce_servers=[
					frappe._dict(woocommerce_id=100, woocommerce_server="woo1.example.com", enabled=1)
				]
			),
		]
		update_stock_levels_on_woocommerce_site("x")
		self.assertEqual(api.put.call_args.kwargs["endpoint"], "products/100/variations/101")
		# Une VARIATION n'a pas de catalog_visibility : elle devient juste
		# non sélectionnable — le masquage du produit passe par le parent.
		self.assertEqual(
			api.put.call_args.kwargs["data"],
			{"stock_quantity": 0, "stock_status": "outofstock"},
		)

	@patch("woocommerce_fusion.tasks.stock_update.frappe")
	@patch("woocommerce_fusion.tasks.stock_update.APIWithRequestLogging", autospec=True)
	def test_decochage_renvoie_le_statut_reel(self, mock_wc_api, mock_frappe):
		# forcer_statut : après décochage, une variation Woo qui ne gère pas les
		# quantités doit quand même repasser instock.
		item = frappe._dict(
			woocommerce_servers=[
				frappe._dict(woocommerce_id=1, woocommerce_server="woo1.example.com", enabled=1)
			],
			is_stock_item=1,
			disabled=0,
			custom_rupture_site_web=0,
		)
		api = self._montage(mock_wc_api, mock_frappe, item)
		update_stock_levels_on_woocommerce_site("x", forcer_statut=True)
		# Décochage : redevient vendable ET réapparaît au catalogue.
		self.assertEqual(
			api.put.call_args.kwargs["data"],
			{"stock_quantity": 7, "stock_status": "instock", "catalog_visibility": "visible"},
		)

	@patch("woocommerce_fusion.tasks.stock_update.frappe")
	@patch("woocommerce_fusion.tasks.stock_update.APIWithRequestLogging", autospec=True)
	def test_synchro_normale_inchangee(self, mock_wc_api, mock_frappe):
		# Sans rupture ni forcer_statut : la donnée poussée reste EXACTEMENT
		# celle d'avant (pas de stock_status) — comportement historique intact.
		item = frappe._dict(
			woocommerce_servers=[
				frappe._dict(woocommerce_id=1, woocommerce_server="woo1.example.com", enabled=1)
			],
			is_stock_item=1,
			disabled=0,
		)
		api = self._montage(mock_wc_api, mock_frappe, item)
		update_stock_levels_on_woocommerce_site("x")
		self.assertEqual(api.put.call_args.kwargs["data"], {"stock_quantity": 7})

	def test_rupture_forcee_pure(self):
		self.assertTrue(rupture_forcee(frappe._dict(custom_rupture_site_web=1)))
		self.assertFalse(rupture_forcee(frappe._dict(custom_rupture_site_web=0)))

	@patch("woocommerce_fusion.tasks.stock_update.frappe")
	def test_bascule_sur_modele_deploie_les_variantes(self, mock_frappe):
		mock_frappe.flags.in_test = False
		mock_frappe.flags.in_migrate = False
		mock_frappe.flags.in_install = False
		mock_frappe.db.count.return_value = 1
		mock_frappe.get_all.return_value = ["VAR-1", "VAR-2"]
		doc = MagicMock()
		doc.get.side_effect = lambda k: {"custom_rupture_site_web": 1}.get(k)
		doc.get_doc_before_save.return_value = frappe._dict(custom_rupture_site_web=0)
		doc.has_variants = 1
		doc.name = "MODELE"
		pousser_rupture_depuis_fiche(doc)
		# 2 variantes (statut) + 1 masquage du produit parent
		self.assertEqual(mock_frappe.enqueue.call_count, 3)
		stocks = [c for c in mock_frappe.enqueue.call_args_list if "forcer_statut" in c.kwargs]
		visibilite = [c for c in mock_frappe.enqueue.call_args_list if "cacher" in c.kwargs]
		self.assertEqual([c.kwargs["item_code"] for c in stocks], ["VAR-1", "VAR-2"])
		self.assertTrue(all(c.kwargs["forcer_statut"] for c in stocks))
		self.assertEqual(len(visibilite), 1)
		self.assertEqual(visibilite[0].kwargs["item_code"], "MODELE")
		self.assertEqual(visibilite[0].kwargs["cacher"], 1)

	@patch("woocommerce_fusion.tasks.stock_update.frappe")
	def test_pas_denvoi_sans_changement(self, mock_frappe):
		mock_frappe.flags.in_test = False
		mock_frappe.flags.in_migrate = False
		mock_frappe.flags.in_install = False
		doc = MagicMock()
		doc.get.side_effect = lambda k: {"custom_rupture_site_web": 1}.get(k)
		doc.get_doc_before_save.return_value = frappe._dict(custom_rupture_site_web=1)
		pousser_rupture_depuis_fiche(doc)
		mock_frappe.enqueue.assert_not_called()
