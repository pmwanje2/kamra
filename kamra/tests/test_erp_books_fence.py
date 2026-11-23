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
PAYABLES = {"label": "Purchase Invoice", "url": "/app/purchase-invoice"}
PNL = {"label": "Profit and Loss Statement", "url": "/app/query-report/Profit%20and%20Loss%20Statement"}

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
		self.calls: list[str] = []

	def provider(self, api_method, *, response_types=None, **form):
		self.calls.append(api_method)
		if api_method != ERP_BOOKS:
			return None
		return self.answer

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

	def boot_value(self, html):
		match = re.search(r"window\.kamra_erp_screens\s*=\s*(\{.*?\});", html)
		self.assertIsNotNone(match, "the boot script does not carry the ERP screens")
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
