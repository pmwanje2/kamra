"""Cutover regressions for sites that delegate only part of the local system."""
import csv
from pathlib import Path
from unittest.mock import Mock, patch

import frappe

from kamra import api, audit_report, books, checkout, erp_books, marketplace, night_audit, onboarding, settings
from kamra.tests.audit_fixtures import AuditTestCase, PROP_A, TODAY, make_property


class TestOwnershipCutover(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.books", "kamra.business_date")
		make_property(PROP_A)

	def ownership(self, *, pos=False, payables=False):
		screens = {"payables": {"label": "Purchases", "url": "/purchases"}} if payables else {}
		payload = {"screens": screens, "pos_url": "/till" if pos else None}
		return patch("kamra.erp_bridge.bridge_call", side_effect=lambda method, **kw:
		             payload if method == erp_books.ERP_BOOKS else None)

	def test_missing_bridge_and_enabled_bridge_without_an_answer(self):
		with patch("kamra.erp_bridge.get_bridge_script", return_value=None):
			self.assertEqual(erp_books.desk_config(), {"screens": {}, "pos_url": None})
		for script in (Mock(execute_method=lambda: None), object()):
			with self.subTest(script=type(script)), patch("kamra.erp_bridge.get_bridge_script", return_value=script):
				with self.assertRaises((TypeError, ValueError)):
					erp_books.erp_screens()
				with self.assertRaises((TypeError, ValueError)):
					erp_books.erp_pos_url()

	def assert_no_local_reads(self, table, method, *args):
		with patch.object(frappe, "get_all", wraps=frappe.get_all) as reads, \
				patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql:
			result = method(*args)
		self.assertFalse(any(c.args and c.args[0] == table for c in reads.call_args_list), method.__name__)
		self.assertFalse(any(c.args and f"`tab{table}" in str(c.args[0]) for c in sql.call_args_list), method.__name__)
		return result

	def test_checkout_and_date_roll_keep_external_checks(self):
		res = frappe._dict(property=PROP_A, name="UT stay", room="101")
		check = dict(name="External ticket", outlet="Cafe", amount=25, billed=True,
		             room=res.room, reservation=res.name, table=None, opened_at=None)
		with self.ownership(pos=True), patch("kamra.outlet_checks.open_outlet_checks", return_value=[check]):
			with self.subTest(flow="checkout"):
				bills = self.assert_no_local_reads("POS Order", checkout._pending_bills, res, [])
				self.assertEqual([(b["name"], b["billable"]) for b in bills], [(check["name"], True)])
			with self.subTest(flow="night audit"):
				audit = self.assert_no_local_reads("POS Order", night_audit.check_open_pos_bills, PROP_A, TODAY)
				self.assertEqual([i["order"] for i in audit["items"]], [check["name"]])
			with self.subTest(flow="room postings"):
				self.assert_no_local_reads("POS Order", night_audit.check_pos_room_postings, PROP_A, TODAY)

	def test_pos_only_cutover_reports(self):
		with self.ownership(pos=True):
			for method in (audit_report._outlets, books.profit_and_loss):
				with self.subTest(report=method.__name__):
					self.assert_no_local_reads("POS Order", method, PROP_A, TODAY, TODAY)

	def test_payables_only_cutover_reports(self):
		with self.ownership(payables=True):
			for method, args in [(books.cash_forecast, (5, 0)), (books.balance_sheet, (TODAY,)),
			                     (books.profit_and_loss, (TODAY, TODAY))]:
				with self.subTest(report=method.__name__):
					self.assert_no_local_reads("Vendor Bill", method, PROP_A, *args)

	def test_tax_basis_alone_owns_gstr1(self):
		with patch("kamra.fiscal.fiscal_bridge", return_value=False), patch("kamra.erp_bridge.erp_determines_tax", return_value=True):
			with self.assertRaisesRegex(frappe.PermissionError, "accounting system"):
				api.gstr1_rows(TODAY, TODAY)

	def test_disabled_precedes_roles_and_preserves_whitelist_guest_policy(self):
		from kamra import agents_api, assistant, mcp_access, mcp_oauth, whatsapp
		with patch.dict(frappe.conf, kamra_mcp_disabled=1), patch.object(frappe, "get_roles", return_value=[]):
			for method, args in [(assistant.ask, (PROP_A, [])), (whatsapp.threads, (PROP_A,)),
			                     (agents_api.activity_feed, (PROP_A,)), (mcp_oauth.connect_info, (PROP_A,)),
			                     (mcp_access.access_overview, (PROP_A,)), (whatsapp.webhook, ())]:
				with self.subTest(method=method.__name__):
					self.assertIn(method, frappe.whitelisted)
					self.assertEqual(method in frappe.guest_methods, method == whatsapp.webhook)
					with self.assertRaisesRegex(frappe.ValidationError, "^KMR-OFF-001$"):
						method(*args)

	def test_http_and_workers_return_refusal_code(self):
		from kamra import agents_channels, mcp_http, whatsapp
		with patch.dict(frappe.conf, kamra_mcp_disabled=1):
			response = mcp_http.dispatch("/mcp", None)
			with self.subTest(entry="http"):
				self.assertEqual(response.status_code, 404)
				self.assertEqual(response.get_json().get("code"), "KMR-OFF-001")
			for method, args in [(whatsapp.send_text, (PROP_A, "0", "hello")),
			                     (whatsapp.send_template, (PROP_A, "0", "service", [])),
			                     (whatsapp.notify_booking_confirmed, ("stay",)),
			                     (whatsapp.send_room_charge_alert, ("folio", "stay", 1, "")),
			                     (agents_channels.send_outbound, (PROP_A, "WhatsApp", "0", "hello"))]:
				with self.subTest(entry=method.__name__):
					result = method(*args)
					self.assertFalse(result["sent"])
					self.assertEqual(result.get("code"), "KMR-OFF-001")

	def test_oauth_client_generic_writes(self):
		with patch.dict(frappe.conf, kamra_mcp_disabled=1):
			for hook in ("before_validate", "on_trash"):
				with self.subTest(hook=hook), self.assertRaisesRegex(frappe.ValidationError, "^KMR-OFF-001$"):
					frappe.new_doc("MCP OAuth Client").run_method(hook)

	def test_settings_only_return_visible_values_and_links(self):
		with self.ownership(pos=True), patch.dict(frappe.conf, kamra_mcp_disabled=1):
			modules = settings.settings_schema(PROP_A)["modules"]
			for module in modules:
				keys = {s["key"] for section in module["sections"] for s in section["settings"]}
				with self.subTest(module=module["id"]):
					self.assertEqual(set(module["values"]), keys)
					self.assertNotIn("/channels", [link["to"] for link in module["related"]])
			for changes in ({}, {"pos_room_charge_limit": 500}):
				with self.subTest(changes=changes):
					values = settings.save_settings(PROP_A, "fnb", changes)["values"]
					self.assertIn("pos_room_charge_limit", values)
					self.assertNotIn("menu_prep_stations", values)

	def test_menu_masters_keep_rate_categories(self):
		with self.ownership(pos=True):
			masters = settings.menu_masters(PROP_A)
			self.assertTrue(masters["rate_categories"])
			for key in ("allergens", "prep_stations", "courses", "categories"):
				self.assertFalse(masters.get(key), key)

	def test_setup_and_marketplace_follow_ownership_without_white_label(self):
		with self.ownership(pos=True), patch("kamra.onboarding._modules", return_value={"finance", "fnb"}), \
				patch("kamra.marketplace.white_label", return_value=False):
			with self.subTest(surface="checklist"):
				keys = {item["key"] for item in onboarding.setup_checklist(PROP_A)["items"]}
				self.assertIn("tills", keys)
				self.assertFalse(keys & {"menu", "outlets", "printers"})
			with self.subTest(surface="marketplace"):
				cards = marketplace.registry(PROP_A)
				self.assertNotIn("POS", [card["name"] for category in cards for card in category["cards"]])

	def test_every_shipped_locale_translates_refusals(self):
		for path in (Path(__file__).resolve().parents[1] / "translations").glob("*.csv"):
			with path.open(encoding="utf-8-sig", newline="") as source:
				translations = {row[0]: row[1] for row in csv.reader(source) if len(row) >= 2}
			for msg in ("Manage this in the point of sale.", "This is kept in the accounting system."):
				with self.subTest(locale=path.stem, message=msg):
					self.assertTrue(translations.get(msg))
					self.assertNotEqual(translations[msg], msg)
