"""Ownership failures must not clear an outlet check or turn missing money into zero."""
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from kamra import books, checkout, erp_books, marketplace, night_audit, outlet_checks, settings
from kamra.tests.audit_fixtures import AuditTestCase, PROP_A, TODAY, YESTERDAY, make_property


class TestOwnershipReports(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.books", "kamra.business_date")
		make_property(PROP_A, business_date=TODAY)

	def bridge(self, *screens, pos=None, accounts=None):
		payload = {"screens": {s: {"label": s, "url": "/" + s} for s in screens}, "pos_url": pos}

		def answer(method, **kwargs):
			if method == erp_books.ERP_BOOKS:
				return payload
			if method == erp_books.ERP_RECEIVABLES and accounts is not None:
				return {"accounts": accounts}
			return None

		return patch("kamra.erp_bridge.bridge_call", side_effect=answer)

	def test_outlet_bridge_must_explicitly_clear_checkout_and_audit(self):
		stay = frappe._dict(property=PROP_A, name="UT stay", room="101")
		for payload in (None, [], {}, {"checks": None}, {"checks": [{"name": "check", "amount": 1, "billed": "false"}]}):
			script = SimpleNamespace(execute_method=lambda: frappe.response.update(message=payload))
			with patch("kamra.erp_bridge.get_bridge_script", side_effect=lambda key: script if key == outlet_checks.OPEN_CHECKS else None):
				for method, args in [(checkout.assert_no_open_bills, (stay,)), (night_audit.check_open_pos_bills, (PROP_A, TODAY))]:
					with self.subTest(payload=payload, method=method.__name__), self.assertRaises((TypeError, ValueError)):
						method(*args)

	def test_explicit_empty_and_absent_outlet_provider(self):
		script = SimpleNamespace(execute_method=lambda: frappe.response.update(message={"checks": []}))
		for provider, expected in ((script, []), (None, None)):
			with patch("kamra.erp_bridge.get_bridge_script", return_value=provider):
				self.assertEqual(outlet_checks.open_outlet_checks(PROP_A), expected)

	def test_external_payables_are_unknown_on_local_statements(self):
		with self.bridge("payables"):
			pnl = books.profit_and_loss(PROP_A, TODAY, TODAY)
			bs = books.balance_sheet(PROP_A, TODAY)
			forecast = books.cash_forecast(PROP_A)
			for report, keys in [(pnl, ("supplier_bills", "expense_total", "net", "tax_on_bills")),
			                     (bs, ("liabilities_total", "net", "tax_on_bills_month")),
			                     (forecast, ("money_out", "closing"))]:
				with self.subTest(keys=keys):
					self.assertIn("payables", report["erp_owned"])
					for key in keys:
						self.assertIsNone(report[key], key)
			self.assertIsNone(next(r["amount"] for r in bs["liabilities"] if r["key"] == "payables"))
			self.assertIsNotNone(bs["assets_total"])
			self.assertIsNotNone(pnl["revenue_total"])

	def test_external_outlet_totals_are_unknown_on_local_pnl(self):
		with self.bridge(pos="/external-pos"):
			report = books.profit_and_loss(PROP_A, TODAY, TODAY)
			self.assertIn("pos", report["erp_owned"])
			self.assertIsNone(next(r["amount"] for r in report["revenue"] if r["key"] == "fnb"))
			self.assertIsNone(report["revenue_total"])
			self.assertIsNone(report["net"])
			self.assertIsNotNone(report["expense_total"])

	def test_current_erp_receivables_fund_forecast_and_balance_sheet(self):
		account = {"customer": "Fixture company", "balance": 70, "open_balance": 80, "unapplied_credit": 10,
		           "open_items": [{"invoice_date": YESTERDAY, "due_date": TODAY, "outstanding": 80, "voucher_no": "INV-1"}]}
		with self.bridge("receivables", accounts=[account]), patch.object(frappe, "get_all", wraps=frappe.get_all) as reads, \
				patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql:
			bs = books.balance_sheet(PROP_A, TODAY)
			forecast = books.cash_forecast(PROP_A)
			self.assertFalse(any(c.args and c.args[0] == "City Ledger Account" for c in reads.call_args_list))
			self.assertFalse(any(c.args and "`tabCity Ledger Entry`" in str(c.args[0]) for c in sql.call_args_list))
			self.assertEqual(next(r["amount"] for r in bs["assets"] if r["key"] == "city_ledger"), 70)
			self.assertEqual(forecast["days"][0]["company_bills"], 70)

	def test_incomplete_erp_receivable_cannot_supply_a_report_total(self):
		with self.bridge("receivables", accounts=[{"open_items": []}]), self.assertRaises(ValueError):
			books.balance_sheet(PROP_A, TODAY)
		with self.bridge("receivables", accounts=[{"balance": 70}]), self.assertRaises(ValueError):
			books.cash_forecast(PROP_A)

	def test_live_erp_ar_cannot_answer_a_historical_balance(self):
		with self.bridge("receivables", accounts=[]), self.assertRaises(frappe.PermissionError):
			books.balance_sheet(PROP_A, YESTERDAY)

	def test_hidden_setting_refuses_the_entire_write(self):
		keys = ["gst_rate_low", "gst_slab_threshold"]
		before = frappe.db.get_value("Property", PROP_A, keys)
		with patch("kamra.localization.uses_tax_slab", return_value=False):
			with self.assertRaises(frappe.ValidationError):
				settings.save_settings(PROP_A, "finance", dict(zip(keys, [8, 3311])))
		self.assertEqual(frappe.db.get_value("Property", PROP_A, keys), before)

	def test_delegated_exports_are_not_offered_as_local(self):
		for owned in (True, False):
			with self.subTest(owned=owned), self.bridge(*(["accounting_export"] if owned else [])):
				routes = [card.get("route") for group in marketplace.registry(PROP_A) for card in group["cards"]]
				self.assertEqual("/accounting-export" in routes, not owned)

	def test_forecast_requires_a_reconciled_payment_schedule_for_each_account(self):
		account = {"customer": "Fixture company", "balance": 70, "open_balance": 80,
		           "open_items": [{"invoice_date": YESTERDAY, "due_date": TODAY, "outstanding": 80}]}
		with self.bridge("receivables", accounts=[account]):
			sheet = books.balance_sheet(PROP_A, TODAY)
			self.assertEqual(next(row["amount"] for row in sheet["assets"] if row["key"] == "city_ledger"), 70)
		for changes in ({}, {"unapplied_credit": None}, {"unapplied_credit": False},
		                {"unapplied_credit": "10"}, {"unapplied_credit": 0}, {"unapplied_credit": -10, "balance": 90},
		                {"unapplied_credit": 10, "open_balance": 75}):
			with self.subTest(changes=changes), self.bridge("receivables", accounts=[account | changes]):
				forecast = books.cash_forecast(PROP_A)
				self.assertIsNone(forecast["money_in"])
				self.assertIsNone(forecast["closing"])
				self.assertIsNone(forecast["days"])
				self.assertIn("receivables", forecast["erp_owned"])
		# Errors on separate accounts must not offset one another.
		with self.bridge("receivables", accounts=[account | {"unapplied_credit": 9}, account | {"unapplied_credit": 11}]):
			self.assertIsNone(books.cash_forecast(PROP_A)["money_in"])

	def test_forecast_accepts_explicit_zero_credit_and_a_net_credit_balance(self):
		for credit, balance, expected in ((0, 80, 80), (100, -20, 0)):
			account = {"company": "Fixture", "balance": balance, "unapplied_credit": credit,
			           "open_items": [{"invoice_date": TODAY, "due_date": TODAY, "outstanding": 80}]}
			with self.subTest(credit=credit), self.bridge("receivables", accounts=[account]):
				self.assertEqual(books.cash_forecast(PROP_A)["days"][0]["company_bills"], expected)
