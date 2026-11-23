"""Management books that never reach a general ledger step aside for the ERP.

Supplier bills (kamra.payables), the cash outlook, the management P&L and
balance sheet (kamra.books), the accounting export (kamra.accounting) and the
company accounts screen (kamra.receivables, the city-ledger payment, dispute
and statement endpoints) keep their own figures. None of them posts to a
general ledger. A site whose accounts live in an ERP declares which of these
screens the ERP keeps, and where. It does this through an enabled API Server
Script at ``hospitality_kamra.erp_books`` (kamra.erp_books.ERP_BOOKS), which
answers ``{"screens": {screen: {"label": ..., "url": ...}}}``.

A screen the ERP keeps is refused here (PermissionError, naming where it is
kept) and hidden from the desk's navigation. Screens the answer does not list
stay as they are. A site with no such script keeps every screen.

Business check: SAP S/4HANA keeps payables, receivables and the financial
statements in FI, posted to the general ledger, and a PMS such as OPERA
Cloud hands its postings to that ledger through an interface. A second
unposted set of books beside the real ones is what ADR-0040 §1 refuses
(ERPNext is the only books, tax and AR).
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from kamra.tests.audit_fixtures import PROP_A, TODAY, AuditTestCase, ensure_user, make_property

ERP_BOOKS = "hospitality_kamra.erp_books"
ERP_RECEIVABLES = "hospitality_kamra.erp_receivables"
PAYABLES = {"label": "Purchase Invoice", "url": "/app/purchase-invoice"}
PNL = {"label": "Profit and Loss Statement", "url": "/app/query-report/Profit%20and%20Loss%20Statement"}
RECEIVABLES = {"label": "Accounts Receivable", "url": "/app/query-report/Accounts%20Receivable"}

# (screen, endpoint, arguments): every whitelisted method of a fenced screen.
ENDPOINTS = (
	("payables", "kamra.payables.vendors", {"property": PROP_A}),
	("payables", "kamra.payables.save_vendor", {"property": PROP_A, "vendor_name": "UT Grocer"}),
	("payables", "kamra.payables.create_vendor_bill",
	 {"property": PROP_A, "vendor": "UT Grocer", "bill_date": TODAY, "amount": 100}),
	("payables", "kamra.payables.approve_vendor_bill", {"bill": "VB-UT-0001"}),
	("payables", "kamra.payables.pay_vendor_bill", {"bill": "VB-UT-0001", "amount": 100}),
	("payables", "kamra.payables.cancel_vendor_bill", {"bill": "VB-UT-0001", "reason": "UT"}),
	("payables", "kamra.payables.vendor_bill", {"bill": "VB-UT-0001"}),
	("payables", "kamra.payables.vendor_bills", {"property": PROP_A}),
	("payables", "kamra.payables.payables_aging", {"property": PROP_A}),
	("cash_forecast", "kamra.books.cash_forecast", {"property": PROP_A}),
	("profit_and_loss", "kamra.books.profit_and_loss", {"property": PROP_A, "from_date": TODAY, "to_date": TODAY}),
	("balance_sheet", "kamra.books.balance_sheet", {"property": PROP_A}),
	("accounting_export", "kamra.accounting.export_invoices",
	 {"property": PROP_A, "from_date": TODAY, "to_date": TODAY}),
	("receivables", "kamra.receivables.ar_accounts", {"property": PROP_A}),
	("receivables", "kamra.receivables.save_ar_account", {"property": PROP_A, "company": "UT Co"}),
	("receivables", "kamra.receivables.create_ar_invoice", {"account": "CLA-UT", "lines": "[]"}),
	("receivables", "kamra.receivables.ar_invoice", {"folio": "FOL-UT"}),
	("receivables", "kamra.receivables.transfer_ar_invoice",
	 {"account": "CLA-UT", "folio": "FOL-UT", "to_account": "CLA-UT-2"}),
	("receivables", "kamra.ledger.city_ledger_history", {"account": "CLA-UT"}),
	("receivables", "kamra.ledger.city_ledger_account", {"account": "CLA-UT"}),
	("receivables", "kamra.ledger.city_ledger_settle", {"account": "CLA-UT", "amount": 100}),
	("receivables", "kamra.ledger.decide_city_ledger_payment", {"request": "CPR-UT"}),
	("receivables", "kamra.ledger.reverse_city_ledger_payment", {"payment": "CLE-UT"}),
	("receivables", "kamra.ledger.set_city_ledger_dispute", {"account": "CLA-UT", "folio": "FOL-UT"}),
	("receivables", "kamra.statements.company_statement", {"account": "CLA-UT"}),
	("receivables", "kamra.statements.email_company_statement", {"account": "CLA-UT"}),
	("receivables", "kamra.statements.email_statements_bulk", {"property": PROP_A, "accounts": "[]"}),
	("receivables", "kamra.statements.company_monthly_bill", {"account": "CLA-UT", "month": "2031-03"}),
	("receivables", "kamra.statements.email_monthly_bill", {"account": "CLA-UT", "month": "2031-03"}),
)

# Reads a hotel with no ERP still opens: they must keep answering.
OFF_BRIDGE_READS = (
	("kamra.payables.vendors", {"property": PROP_A}),
	("kamra.payables.vendor_bills", {"property": PROP_A}),
	("kamra.payables.payables_aging", {"property": PROP_A}),
	("kamra.books.cash_forecast", {"property": PROP_A}),
	("kamra.books.profit_and_loss", {"property": PROP_A, "from_date": TODAY, "to_date": TODAY}),
	("kamra.books.balance_sheet", {"property": PROP_A, "as_of": TODAY}),
	("kamra.accounting.export_invoices", {"property": PROP_A, "from_date": TODAY, "to_date": TODAY}),
	("kamra.receivables.ar_accounts", {"property": PROP_A}),
)


class ERPBooksFenceTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.business_date", "kamra.books")
		make_property(PROP_A)
		self.answer = None
		self.ar_answer = None
		self.calls: list[str] = []

	def provider(self, api_method, *, response_types=None, **form):
		self.calls.append(api_method)
		if api_method == ERP_BOOKS:
			return self.answer
		if api_method == ERP_RECEIVABLES:
			return self.ar_answer
		return None

	def installed(self):
		return patch("kamra.erp_bridge.bridge_call", side_effect=self.provider)

	def keeps(self, **screens):
		self.answer = {"screens": screens}

	def test_each_screen_the_erp_keeps_is_refused_with_where_it_is_kept(self):
		target = {"label": "UT ledger screen", "url": "/app/ut-ledger-screen"}
		for screen, method, args in ENDPOINTS:
			self.keeps(**{screen: target})
			with self.subTest(method=method):
				with self.installed(), self.assertRaises(frappe.PermissionError) as caught:
					frappe.get_attr(method)(**args)
				message = str(caught.exception)
				self.assertIn("UT ledger screen", message)
				self.assertIn("/app/ut-ledger-screen", message)
				self.assertNotRegex(message, re.compile(r"\b(?:Kamra|ERPNext|Frappe|URY)\b", re.I))

	def test_a_screen_the_answer_does_not_list_stays(self):
		self.keeps(payables=PAYABLES)
		with self.installed():
			pnl = frappe.get_attr("kamra.books.profit_and_loss")(property=PROP_A, from_date=TODAY, to_date=TODAY)
			with self.assertRaises(frappe.PermissionError):
				frappe.get_attr("kamra.payables.vendors")(property=PROP_A)
		self.assertIsInstance(pnl, dict)

	def test_a_site_without_the_answer_keeps_every_screen(self):
		self.answer = None
		with self.installed():
			for method, args in OFF_BRIDGE_READS:
				with self.subTest(method=method):
					frappe.get_attr(method)(**args)  # no PermissionError
		self.assertIn(ERP_BOOKS, self.calls)

	def test_roles_are_still_checked_before_the_fence(self):
		user = ensure_user("ut-books-housekeeping@kamra.local", ["Housekeeping"])
		self.keeps(payables=PAYABLES)
		frappe.set_user(user)  # nosemgrep: frappe-setuser -- test fixture
		with self.installed(), self.assertRaises(frappe.PermissionError) as caught:
			frappe.get_attr("kamra.payables.vendors")(property=PROP_A)
		self.assertNotIn("/app/purchase-invoice", str(caught.exception))

	def test_a_finance_user_is_refused_with_the_erp_screen(self):
		user = ensure_user("ut-books-finance@kamra.local", ["Finance"])
		self.keeps(profit_and_loss=PNL)
		frappe.set_user(user)  # nosemgrep: frappe-setuser -- test fixture
		with self.installed(), self.assertRaises(frappe.PermissionError) as caught:
			frappe.get_attr("kamra.books.profit_and_loss")(property=PROP_A, from_date=TODAY, to_date=TODAY)
		self.assertIn("Profit and Loss Statement", str(caught.exception))

	def test_the_answer_is_validated(self):
		from kamra.erp_books import erp_screens

		bad = (
			[{"payables": PAYABLES}],                                    # not {"screens": {...}}
			{"screens": [PAYABLES]},                                     # screens not a mapping
			{"screens": {"general_ledger": PAYABLES}},                   # a screen this app does not have
			{"screens": {"payables": {"label": "", "url": "/app/x"}}},   # no label
			{"screens": {"payables": {"label": "X", "url": 7}}},         # url not text
			{"screens": {"payables": {"label": "X", "url": "https://elsewhere.example/app"}}},
			{"screens": {"payables": {"label": "X", "url": "//elsewhere.example/app"}}},
			{"screens": {"payables": {"label": "X", "url": "app/purchase-invoice"}}},
			{"screens": {"payables": {"label": "X", "url": "/app/x\nSet-Cookie: y"}}},
		)
		for answer in bad:
			self.answer = answer
			with self.subTest(answer=answer), self.installed(), \
					self.assertRaisesRegex(ValueError, "accounting screen"):
				erp_screens()

	def test_the_desk_boot_tells_the_screens_what_the_erp_keeps(self):
		html = self.boot_html()
		self.assertEqual(json.loads(self.boot_value(html)), {})
		self.keeps(payables=PAYABLES, profit_and_loss=PNL)
		html = self.boot_html()
		self.assertEqual(json.loads(self.boot_value(html)), {"payables": PAYABLES, "profit_and_loss": PNL})

	def test_the_sign_in_page_does_not_ask(self):
		self.keeps(payables=PAYABLES)
		frappe.set_user("Guest")  # nosemgrep: frappe-setuser -- test fixture
		html = self.boot_html()
		self.assertEqual(json.loads(self.boot_value(html)), {})
		self.assertNotIn(ERP_BOOKS, self.calls)

	def test_list_response_through_real_bridge_call_raises_value_error(self):
		from kamra.erp_books import erp_screens

		sentinel = SimpleNamespace(api_method=ERP_BOOKS)
		with patch("kamra.erp_bridge.get_bridge_script", return_value=sentinel), \
				patch("kamra.erp_bridge.run_bridge_script", return_value=[{"payables": PAYABLES}]), \
				self.assertRaisesRegex(ValueError, "accounting screen"):
			erp_screens()

	def test_desk_boot_carries_validated_pos_url_and_hides_it_from_guests(self):
		from kamra.erp_books import erp_pos_url

		self.answer = {"screens": {"payables": PAYABLES}, "pos_url": "/pos"}
		with self.installed():
			self.assertEqual(erp_pos_url(), "/pos")
		html = self.boot_html()
		self.assertEqual(json.loads(self.boot_pos_url(html)), "/pos")

		# Guest gets null and never queries the bridge
		self.calls.clear()
		frappe.set_user("Guest")  # nosemgrep: frappe-setuser -- test fixture
		guest_html = self.boot_html()
		self.assertIsNone(json.loads(self.boot_pos_url(guest_html)))
		self.assertNotIn(ERP_BOOKS, self.calls)

		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- restore test fixture user
		for bad_url in ("https://elsewhere.example/pos", "//elsewhere.example/pos", "pos", 7, ""):
			self.answer = {"screens": {"payables": PAYABLES}, "pos_url": bad_url}
			with self.subTest(pos_url=bad_url), self.installed(), \
					self.assertRaisesRegex(ValueError, "accounting screen"):
				erp_pos_url()

	def test_direct_frappe_client_get_list_on_vendor_bill_is_refused_when_bridged(self):
		user = ensure_user("ut-books-vendor-list@kamra.local", ["Finance"])
		frappe.set_user(user)  # nosemgrep: frappe-setuser -- test fixture

		# Off-bridge: direct get_list works as before
		self.answer = None
		with self.installed():
			rows = frappe.client.get_list("Vendor Bill", fields=["name"])
		self.assertIsInstance(rows, list)

		# Bridged (payables kept in ERP): direct get_list is refused with PermissionError
		self.keeps(payables=PAYABLES)
		with self.installed(), self.assertRaises(frappe.PermissionError) as caught:
			frappe.client.get_list("Vendor Bill", fields=["name"])
		self.assertIn("Purchase Invoice", str(caught.exception))
		self.assertIn("/app/purchase-invoice", str(caught.exception))

	def _seed_stale_city_ledger(self, *, credit_limit=2000.0, amount=5000.0, days_ago=60):
		from frappe.utils import add_days

		company_name = "UT Bridged Corp"
		if not frappe.db.exists("Corporate Account", company_name):
			frappe.get_doc({
				"doctype": "Corporate Account",
				"company_name": company_name,
				"credit_allowed": 1,
				"contact_email": "billing@ut-bridged.test",
			}).insert(ignore_permissions=True)
		account = frappe.get_doc({
			"doctype": "City Ledger Account",
			"property": PROP_A,
			"company": company_name,
			"account_number": "CL-UT-1",
			"credit_limit": credit_limit,
			"credit_days": 15,
			"use_default_credit_days": 0,
		}).insert(ignore_permissions=True).name
		folio = frappe.get_doc({
			"doctype": "Folio",
			"property": PROP_A,
			"folio_type": "Company",
			"status": "Closed",
		}).insert(ignore_permissions=True).name
		inv_date = str(add_days(TODAY, -days_ago))
		due_date = str(add_days(TODAY, -days_ago + 15))
		frappe.get_doc({
			"doctype": "City Ledger Entry",
			"property": PROP_A,
			"account": account,
			"posting_date": inv_date,
			"invoice_date": inv_date,
			"due_date": due_date,
			"kind": "Invoice",
			"folio": folio,
			"invoice_number": "INV-STALE-1",
			"debit": amount,
			"credit": 0,
		}).insert(ignore_permissions=True)
		frappe.db.set_value("City Ledger Account", account, "balance", amount)
		return company_name, account, folio

	def test_city_ledger_aging_and_owner_snapshot_use_erp_receivables_when_bridged(self):
		from frappe.utils import add_days
		from kamra import dashboards, ledger

		company, account, folio = self._seed_stale_city_ledger(credit_limit=2000.0, amount=5000.0, days_ago=60)
		self.keeps(receivables=RECEIVABLES)
		# ERPNext Payment Entry reduced receivable from 5000 to 400 (10 days overdue).
		self.ar_answer = {"accounts": [{
			"account": account,
			"company": company,
			"customer": company,
			"account_number": "CL-UT-1",
			"credit_limit": 2000.0,
			"balance": 400.0,
			"open_balance": 400.0,
			"unapplied_credit": 0.0,
			"open_items": [{
				"folio": folio,
				"invoice_number": "ACC-SINV-1",
				"voucher_type": "Sales Invoice",
				"voucher_no": "ACC-SINV-1",
				"invoice_date": str(add_days(TODAY, -25)),
				"due_date": str(add_days(TODAY, -10)),
				"outstanding": 400.0,
				"disputed": 0,
			}],
		}]}
		with self.installed():
			aging = ledger.city_ledger_aging(PROP_A)
			snap = dashboards.owner_snapshot(PROP_A)
		self.assertEqual(aging["totals"]["overdue"], 400.0)
		self.assertEqual(aging["totals"]["d1_30"], 400.0)
		self.assertEqual(aging["totals"]["d31_60"], 0.0)
		self.assertEqual(snap["city_ledger"], 400.0)
		self.assertEqual(snap["city_ledger_overdue"], 400.0)

		# If receivables are fenced in ERP without an AR reader, city_ledger_aging refuses (never returns stale CLE).
		self.ar_answer = None
		with self.installed(), self.assertRaises(frappe.PermissionError) as caught:
			ledger.city_ledger_aging(PROP_A)
		self.assertIn("Accounts Receivable", str(caught.exception))

	def test_night_audit_credit_and_overdue_checks_use_erp_receivables_when_bridged(self):
		from frappe.utils import add_days
		from kamra import night_audit

		company, account, folio = self._seed_stale_city_ledger(credit_limit=2000.0, amount=5000.0, days_ago=60)
		self.keeps(receivables=RECEIVABLES)

		# Paid in full in ERPNext -> zero stale warnings in Night Audit.
		self.ar_answer = {"accounts": [{
			"account": account, "company": company, "customer": company, "account_number": "CL-UT-1",
			"credit_limit": 2000.0, "balance": 0.0, "open_balance": 0.0, "unapplied_credit": 0.0,
			"open_items": [],
		}]}
		with self.installed():
			cl = night_audit.check_credit_limit(PROP_A)
			ov = night_audit.check_overdue_ar(PROP_A)
		self.assertEqual(cl["items"], [])
		self.assertEqual(ov["items"], [])

		# Overdue & over limit in ERPNext -> Night Audit flags exact ERP figures.
		self.ar_answer = {"accounts": [{
			"account": account, "company": company, "customer": company, "account_number": "CL-UT-1",
			"credit_limit": 2000.0, "balance": 2500.0, "open_balance": 2500.0, "unapplied_credit": 0.0,
			"open_items": [{
				"folio": folio, "invoice_number": "ACC-SINV-2", "voucher_type": "Sales Invoice",
				"voucher_no": "ACC-SINV-2", "invoice_date": str(add_days(TODAY, -35)),
				"due_date": str(add_days(TODAY, -20)), "outstanding": 2500.0, "disputed": 0,
			}],
		}]}
		with self.installed():
			cl = night_audit.check_credit_limit(PROP_A)
			ov = night_audit.check_overdue_ar(PROP_A)
		self.assertEqual(len(cl["items"]), 1)
		self.assertEqual(cl["items"][0]["ar_balance"], 2500.0)
		self.assertEqual(cl["items"][0]["over_by"], 500.0)
		self.assertEqual(len(ov["items"]), 1)
		self.assertEqual(ov["items"][0]["overdue"], 2500.0)

	def test_credit_control_uses_erp_receivables_and_avoids_false_credit_blocked(self):
		from frappe.utils import add_days
		from kamra import credit
		from kamra.settings import write_kv

		company, account, folio = self._seed_stale_city_ledger(credit_limit=2000.0, amount=5000.0, days_ago=60)
		write_kv(PROP_A, "credit_control", "block")
		self.keeps(receivables=RECEIVABLES)

		# ERPNext receipt cleared the balance -> NOT blocked despite stale 5000 CLE!
		self.ar_answer = {"accounts": [{
			"account": account, "company": company, "customer": company, "account_number": "CL-UT-1",
			"credit_limit": 2000.0, "balance": 0.0, "open_balance": 0.0, "unapplied_credit": 0.0,
			"open_items": [],
		}]}
		with self.installed():
			res = credit.check_company_credit(company, PROP_A, 500.0)
			terms = credit.company_credit_terms(company, PROP_A)
		self.assertTrue(res["ok"])
		self.assertFalse(res["blocked"])
		self.assertEqual(res["ar_balance"], 0.0)
		self.assertEqual(res["overdue_amount"], 0.0)
		self.assertEqual(terms["balance"], 0.0)

		# Unpaid overdue balance in ERPNext -> blocked accurately.
		self.ar_answer = {"accounts": [{
			"account": account, "company": company, "customer": company, "account_number": "CL-UT-1",
			"credit_limit": 2000.0, "balance": 1800.0, "open_balance": 1800.0, "unapplied_credit": 0.0,
			"open_items": [{
				"folio": folio, "invoice_number": "ACC-SINV-3", "voucher_type": "Sales Invoice",
				"voucher_no": "ACC-SINV-3", "invoice_date": str(add_days(TODAY, -35)),
				"due_date": str(add_days(TODAY, -15)), "outstanding": 1800.0, "disputed": 0,
			}],
		}]}
		with self.installed():
			res = credit.check_company_credit(company, PROP_A, 500.0)
		self.assertFalse(res["ok"])
		self.assertTrue(res["blocked"])
		self.assertEqual(res["ar_balance"], 1800.0)
		self.assertEqual(res["over_limit_by"], 300.0)
		self.assertEqual(res["overdue_amount"], 1800.0)

	def test_payment_reminders_stand_down_when_receivables_kept_in_erp(self):
		from kamra import statements
		from kamra.settings import write_kv

		self._seed_stale_city_ledger(credit_limit=2000.0, amount=5000.0, days_ago=30)
		write_kv(PROP_A, "credit_reminders", 1)
		self.keeps(receivables=RECEIVABLES)
		with self.installed(), patch.object(frappe, "sendmail") as sendmail:
			out = statements.send_property_reminders(PROP_A)
			statements.run_payment_reminders()
		self.assertEqual(out, {"sent": [], "no_email": []})
		sendmail.assert_not_called()

	def boot_value(self, html):
		match = re.search(r"window\.kamra_erp_screens\s*=\s*(\{.*?\});", html)
		self.assertIsNotNone(match, "the boot script does not carry the ERP screens")
		self.assertNotIn("<", match.group(1))
		return match.group(1)

	def boot_pos_url(self, html):
		match = re.search(r"window\.kamra_erp_pos_url\s*=\s*([^;]+);", html)
		self.assertIsNotNone(match, "the boot script does not carry the ERP POS url")
		self.assertNotIn("<", match.group(1))
		return match.group(1)

	def boot_html(self):
		from kamra.www import kamra as shell

		source = Path(frappe.get_app_path("kamra")).parent / "frontend" / "index.html"
		with tempfile.TemporaryDirectory() as directory:
			index = Path(directory) / "index.html"
			index.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
			with self.installed(), patch.object(frappe, "get_app_path", return_value=str(index)), \
					patch.object(frappe, "request", SimpleNamespace(path="/hotel/payables"), create=True):
				return shell.get_context(SimpleNamespace()).spa_html
