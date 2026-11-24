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

import ast
import json
import re
import tempfile
from collections import Counter
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

EXPECTED_RAW_CLE_COUNTS: dict[tuple[str, str], int] = {
	("kamra/audit_report.py", "_city_ledger"): 1,
	("kamra/books.py", "balance_sheet"): 1,
	("kamra/credit.py", "check_company_credit"): 1,
	("kamra/custom_reports.py", "_fetch_dso"): 1,
	("kamra/golive.py", "_kamra_ar"): 2,
	("kamra/golive.py", "reconcile"): 1,
	("kamra/ledger.py", "_account_position"): 1,
	("kamra/ledger.py", "_settled_with_key"): 1,
	("kamra/ledger.py", "_sync_account_balance"): 1,
	("kamra/ledger.py", "_unapplied_payments"): 3,
	("kamra/ledger.py", "city_ledger_history"): 1,
	("kamra/ledger.py", "city_ledger_open_folios"): 2,
	("kamra/ledger.py", "locked_account_figures"): 2,
	("kamra/ledger.py", "payment_bills"): 1,
	("kamra/ledger.py", "reverse_city_ledger_payment"): 1,
	("kamra/ledger.py", "reverse_city_ledger_transfer"): 1,
	("kamra/night_audit.py", "check_credit_limit"): 1,
	("kamra/receivables.py", "_summary"): 1,
	("kamra/receivables.py", "ar_accounts"): 3,
	("kamra/statements.py", "monthly_bill"): 2,
}


def _scan_receivables_module(rel: str, text: str) -> dict:
	"""AST-scan one production Python module for receivables_source / erp_receivables
	calls (including import aliases and local rebindings) and function-scoped raw
	`tabCity Ledger Entry` references."""
	tree = ast.parse(text, filename=rel)
	found_strict: set[tuple[str, str]] = set()
	found_nonstrict: set[tuple[str, str]] = set()
	direct_erp_receivables: list[str] = []
	dynamic_strict: list[str] = []
	star_imports: list[str] = []
	raw_cle: Counter[tuple[str, str]] = Counter()

	def _is_erp_books_module(node: ast.ImportFrom) -> bool:
		return node.module in ("kamra.erp_books", "erp_books")

	def _walk_body(
		stmts: list[ast.stmt],
		fn_name: str,
		rs_names: set[str],
		er_names: set[str],
		eb_modules: set[str],
	) -> None:
		# Track aliases per lexical scope so module-level and function-local
		# `from kamra.erp_books import receivables_source as rs` or `f = rs` are caught.
		rs_local = set(rs_names)
		er_local = set(er_names)
		eb_local = set(eb_modules)

		def _expr_ref(expr: ast.AST | None) -> str | None:
			if isinstance(expr, ast.Name):
				if expr.id in rs_local:
					return "rs"
				if expr.id in er_local:
					return "er"
				if expr.id in eb_local:
					return "eb"
			elif isinstance(expr, ast.Attribute):
				if expr.attr == "receivables_source":
					return "rs"
				if expr.attr == "erp_receivables":
					return "er"
				if expr.attr == "erp_books":
					return "eb"
			return None

		def _record_assign(target: ast.AST, value: ast.AST | None) -> None:
			if not isinstance(target, ast.Name):
				return
			kind = _expr_ref(value)
			if kind == "rs":
				rs_local.add(target.id)
			elif kind == "er":
				er_local.add(target.id)
			elif kind == "eb":
				eb_local.add(target.id)

		def _scan_expr(node: ast.AST) -> None:
			if isinstance(node, ast.Constant) and isinstance(node.value, str) and "tabCity Ledger Entry" in node.value:
				raw_cle[(rel, fn_name)] += node.value.count("tabCity Ledger Entry")
			if isinstance(node, ast.Call):
				ref = _expr_ref(node.func)
				if ref == "er" and rel != "kamra/erp_books.py":
					direct_erp_receivables.append(f"{rel}:{fn_name}")
				elif ref == "rs" and rel != "kamra/erp_books.py":
					strict_kws = [kw for kw in node.keywords if kw.arg == "strict"]
					if any(not (isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, bool))
					       for kw in strict_kws):
						dynamic_strict.append(f"{rel}:{fn_name}")
					strict_false = any(
						isinstance(kw.value, ast.Constant) and kw.value.value is False
						for kw in strict_kws
					)
					(found_nonstrict if strict_false else found_strict).add((rel, fn_name))
			for child in ast.iter_child_nodes(node):
				if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
					continue
				_scan_expr(child)

		for stmt in stmts:
			if isinstance(stmt, ast.ImportFrom):
				if _is_erp_books_module(stmt):
					for alias in stmt.names:
						if alias.name == "*":
							star_imports.append(f"{rel}:{fn_name}")
						elif alias.name == "receivables_source":
							rs_local.add(alias.asname or alias.name)
						elif alias.name == "erp_receivables":
							er_local.add(alias.asname or alias.name)
				elif stmt.module == "kamra" or (stmt.level and stmt.module is None):
					for alias in stmt.names:
						if alias.name == "erp_books":
							eb_local.add(alias.asname or alias.name)
			elif isinstance(stmt, ast.Import):
				for alias in stmt.names:
					if alias.name == "kamra.erp_books":
						eb_local.add(alias.asname or alias.name)
			elif isinstance(stmt, ast.Assign):
				_scan_expr(stmt.value)
				for target in stmt.targets:
					_record_assign(target, stmt.value)
			elif isinstance(stmt, ast.AnnAssign):
				if stmt.value is not None:
					_scan_expr(stmt.value)
					_record_assign(stmt.target, stmt.value)
			elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
				for dec in stmt.decorator_list:
					_scan_expr(dec)
				for default in (*stmt.args.defaults, *stmt.args.kw_defaults):
					if default is not None:
						_scan_expr(default)
				_walk_body(stmt.body, stmt.name, rs_local, er_local, eb_local)
			elif isinstance(stmt, ast.ClassDef):
				for dec in stmt.decorator_list:
					_scan_expr(dec)
				_walk_body(stmt.body, fn_name, rs_local, er_local, eb_local)
			else:
				for field, value in ast.iter_fields(stmt):
					if field in ("body", "orelse", "finalbody", "handlers", "cases"):
						continue
					if isinstance(value, ast.AST):
						_scan_expr(value)
					elif isinstance(value, list):
						for item in value:
							if isinstance(item, ast.AST):
								_scan_expr(item)
				for attr in ("body", "orelse", "finalbody"):
					sub = getattr(stmt, attr, None)
					if isinstance(sub, list):
						_walk_body(sub, fn_name, rs_local, er_local, eb_local)
				for handler in getattr(stmt, "handlers", None) or ():
					_walk_body(handler.body, fn_name, rs_local, er_local, eb_local)
				for case in getattr(stmt, "cases", None) or ():
					if case.guard is not None:
						_scan_expr(case.guard)
					_walk_body(case.body, fn_name, rs_local, er_local, eb_local)

	_walk_body(tree.body, "<module>", {"receivables_source"}, {"erp_receivables"}, {"erp_books"})
	return {
		"strict": found_strict,
		"nonstrict": found_nonstrict,
		"direct_erp_receivables": direct_erp_receivables,
		"dynamic_strict": dynamic_strict,
		"star_imports": star_imports,
		"raw_cle": raw_cle,
		"text_cle_count": text.count("tabCity Ledger Entry"),
	}

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
		self.assertFalse(cl.get("unverified"))
		self.assertFalse(ov.get("unverified"))
		self.assertEqual(cl["resolve"], "/receivables")
		self.assertEqual(ov["resolve"], "/receivables")

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

	def test_owner_snapshot_shows_no_receivable_figure_when_erp_keeps_them_and_answers_no_lookup(self):
		from kamra import dashboards

		self._seed_stale_city_ledger(credit_limit=2000.0, amount=5000.0, days_ago=60)
		self.keeps(receivables=RECEIVABLES)
		self.ar_answer = None
		before = list(frappe.local.message_log)
		with self.installed():
			snap = dashboards.owner_snapshot(PROP_A)
		# unknown is not zero, and never the stale 5000 of the hotel app's own ledger
		self.assertIsNone(snap["city_ledger"])
		self.assertIsNone(snap["city_ledger_overdue"])
		# the refusal was handled: the screen must not also pop a "kept in the accounting system" message
		self.assertEqual(list(frappe.local.message_log), before)

	def test_receivables_source_never_conflates_unavailable_erp_with_empty_list(self):
		from kamra.erp_books import ReceivablesSource, receivables_source

		# Off-bridge: hotel app keeps receivables.
		self.answer = None
		with self.installed():
			local = receivables_source(PROP_A, strict=False)
		self.assertIsInstance(local, ReceivablesSource)
		self.assertFalse(local.in_erp)
		self.assertIsNone(local.rows)
		self.assertFalse(local.unavailable)
		with self.assertRaises(TypeError):
			bool(local)

		# Bridged & ERP answered genuinely empty -> rows=[], unavailable=False.
		self.keeps(receivables=RECEIVABLES)
		self.ar_answer = {"accounts": []}
		with self.installed():
			empty = receivables_source(PROP_A, strict=False)
		self.assertTrue(empty.in_erp)
		self.assertEqual(empty.rows, [])
		self.assertFalse(empty.unavailable)

		# Bridged & ERP unavailable -> strict=True refuses; strict=False returns rows=None (NEVER []).
		self.ar_answer = None
		with self.installed(), self.assertRaises(frappe.PermissionError) as caught:
			receivables_source(PROP_A, strict=True)
		self.assertIn("Accounts Receivable", str(caught.exception))
		with self.installed():
			unavail = receivables_source(PROP_A, strict=False)
		self.assertTrue(unavail.in_erp)
		self.assertIsNone(unavail.rows)
		self.assertTrue(unavail.unavailable)
		self.assertEqual(unavail.target, RECEIVABLES)

	def test_kept_receivables_without_a_lookup_fail_closed_everywhere(self):
		from kamra import credit, night_audit
		from kamra.settings import write_kv

		vendor_re = re.compile(r"\b(?:Kamra|ERPNext|Frappe|URY)\b", re.IGNORECASE)
		company, _account, _folio = self._seed_stale_city_ledger(credit_limit=2000.0, amount=5000.0, days_ago=60)
		write_kv(PROP_A, "credit_control", "block")
		self.keeps(receivables=RECEIVABLES)
		self.ar_answer = None
		with self.installed():
			cl = night_audit.check_credit_limit(PROP_A)
			ov = night_audit.check_overdue_ar(PROP_A)
			res = credit.check_company_credit(company, PROP_A, 500.0)
			with self.assertRaises(credit.CreditBlocked) as blocked_exc:
				credit.enforce_company_credit(company, PROP_A, 500.0, context="check-out")
			terms = credit.company_credit_terms(company, PROP_A)

		# Night audit marks both checks unverified (never green / never zero total).
		self.assertTrue(cl.get("unverified"))
		self.assertEqual(cl["items"], [])
		self.assertEqual(cl["resolve"], "/receivables")
		self.assertNotRegex(cl["action"], vendor_re)
		self.assertTrue(ov.get("unverified"))
		self.assertEqual(ov["items"], [])
		self.assertIsNone(ov["total"])
		self.assertEqual(ov["resolve"], "/receivables")
		self.assertNotRegex(ov["action"], vendor_re)

		# Credit control under Block refuses in plain words (never zero debt, never stale 5000).
		self.assertFalse(res["ok"])
		self.assertTrue(res["blocked"])
		self.assertTrue(res.get("unverified"))
		self.assertIsNone(res["ar_balance"])
		self.assertIsNone(res["overdue_amount"])
		self.assertIsNone(res["exposure"])
		self.assertIsNone(res["over_limit_by"])
		self.assertTrue(res["reasons"])
		self.assertNotRegex(" ".join(res["reasons"]), vendor_re)
		self.assertNotRegex(str(blocked_exc.exception), vendor_re)

		# Credit terms screen receives balance=None (shows "—", never 0.0 or 5000.0).
		self.assertIsNone(terms["balance"])

		# Under Warn: unverified warning returned, never ok=True, never zero ar_balance.
		write_kv(PROP_A, "credit_control", "warn")
		with self.installed():
			warn_res = credit.enforce_company_credit(company, PROP_A, 500.0, context="check-out")
		self.assertFalse(warn_res["ok"])
		self.assertFalse(warn_res["blocked"])
		self.assertTrue(warn_res.get("unverified"))
		self.assertIsNone(warn_res["ar_balance"])
		self.assertTrue(warn_res.get("warning"))
		self.assertNotRegex(warn_res["warning"], vendor_re)

	def test_dso_and_audit_report_never_read_stale_city_ledger_when_bridged(self):
		from frappe.utils import add_days

		from kamra import audit_report, custom_reports

		make_property(PROP_A, business_date=TODAY)
		company, account, folio = self._seed_stale_city_ledger(credit_limit=2000.0, amount=5000.0, days_ago=60)
		from_d = str(add_days(TODAY, -90))

		# Off-bridge: both read City Ledger Entry as before.
		self.answer = None
		with self.installed():
			dso_local = custom_reports._fetch_dso(PROP_A, from_date=from_d, to_date=TODAY)
			ar_local = next(b for b in audit_report.build(PROP_A, TODAY)["balances"] if b["ledger"] == "AR")
		co_row = next(r for r in dso_local if r["company"] == company)
		self.assertEqual(co_row["closing_balance"], 5000.0)
		self.assertEqual(ar_local["carried_forward"], 5000.0)

		# Bridged with live ERP receivables: DSO refuses naming Accounts Receivable;
		# audit_report never reads stale 5000 (uses live ERP balance for current BD, None for past dates).
		self.keeps(receivables=RECEIVABLES)
		self.ar_answer = {"accounts": [{
			"account": account, "company": company, "customer": company, "account_number": "CL-UT-1",
			"credit_limit": 2000.0, "balance": 400.0, "open_balance": 400.0, "unapplied_credit": 0.0,
			"open_items": [{
				"folio": folio, "invoice_number": "ACC-SINV-4", "voucher_type": "Sales Invoice",
				"voucher_no": "ACC-SINV-4", "invoice_date": str(add_days(TODAY, -25)),
				"due_date": str(add_days(TODAY, -10)), "outstanding": 400.0, "disputed": 0,
			}],
		}]}
		with self.installed():
			with self.assertRaises(frappe.PermissionError) as caught:
				custom_reports._fetch_dso(PROP_A, from_date=from_d, to_date=TODAY)
			self.assertIn("kept in the accounting system", str(caught.exception).lower())
			self.assertIn("Accounts Receivable", str(caught.exception))
			self.assertNotIn("not verified", str(caught.exception).lower())
			ar_today = next(b for b in audit_report.build(PROP_A, TODAY)["balances"] if b["ledger"] == "AR")
			ar_past = next(
				b for b in audit_report.build(PROP_A, str(add_days(TODAY, -1)))["balances"] if b["ledger"] == "AR"
			)
		self.assertTrue(ar_today.get("kept_in_erp"))
		self.assertFalse(ar_today.get("unverified"))
		self.assertIsNone(ar_today["brought_forward"])
		self.assertIsNone(ar_today["debit"])
		self.assertIsNone(ar_today["credit"])
		self.assertEqual(ar_today["carried_forward"], 400.0)
		self.assertTrue(ar_past.get("kept_in_erp"))
		self.assertIsNone(ar_past["carried_forward"])

		# Bridged with unavailable ERP lookup: DSO refuses with "Not verified" (never "kept in the accounting
		# system", which is reserved for the by-design reachable case), and audit_report marks AR unverified.
		vendor_re = re.compile(r"\b(?:Kamra|ERPNext|Frappe|URY)\b", re.IGNORECASE)
		self.ar_answer = None
		with self.installed():
			with self.assertRaises(frappe.PermissionError) as caught_down:
				custom_reports._fetch_dso(PROP_A, from_date=from_d, to_date=TODAY)
			ar_unavail = next(b for b in audit_report.build(PROP_A, TODAY)["balances"] if b["ledger"] == "AR")
		self.assertIn("Not verified", str(caught_down.exception))
		self.assertNotIn("kept in the accounting system", str(caught_down.exception).lower())
		self.assertNotRegex(str(caught_down.exception), vendor_re)
		self.assertTrue(ar_unavail.get("kept_in_erp"))
		self.assertTrue(ar_unavail.get("unverified"))
		for col in ("brought_forward", "debit", "credit", "carried_forward"):
			self.assertIsNone(ar_unavail[col])

	def test_permission_error_from_erp_receivables_fails_closed_everywhere(self):
		"""PermissionError from Hotel ERP Receivables must produce the same fail-closed
		unavailable result for every caller (credit check, terms/status, night audit checks,
		audit report, ledger balances, owner snapshot, DSO) — never crash or pop raw errors."""
		from kamra import audit_report, credit, custom_reports, dashboards, ledger, night_audit
		from kamra.erp_books import receivables_source
		from kamra.settings import write_kv

		vendor_re = re.compile(r"\b(?:Kamra|ERPNext|Frappe|URY)\b", re.IGNORECASE)
		make_property(PROP_A, business_date=TODAY)
		company, _account, _folio = self._seed_stale_city_ledger(credit_limit=2000.0, amount=5000.0, days_ago=60)
		write_kv(PROP_A, "credit_control", "block")
		self.keeps(receivables=RECEIVABLES)

		def deny_ar(api_method, *, response_types=None, **form):
			self.calls.append(api_method)
			if api_method == ERP_BOOKS:
				return self.answer
			if api_method == ERP_RECEIVABLES:
				frappe.throw("Hotel settings are not available for that property.", frappe.PermissionError)
			return None

		before_log = list(frappe.local.message_log)
		with patch("kamra.erp_bridge.bridge_call", side_effect=deny_ar):
			src = receivables_source(PROP_A, strict=False)
			chk = credit.check_company_credit(company, PROP_A, 500.0)
			status = credit.company_credit_status(company, PROP_A, 500.0)
			terms = credit.company_credit_terms(company, PROP_A)
			cl = night_audit.check_credit_limit(PROP_A)
			ov = night_audit.check_overdue_ar(PROP_A)
			checks = night_audit.run_checks(PROP_A)
			ar_bal = next(b for b in audit_report.build(PROP_A, TODAY)["balances"] if b["ledger"] == "AR")
			lb = ledger.ledger_balances(PROP_A)
			snap = dashboards.owner_snapshot(PROP_A)

		self.assertTrue(src.in_erp)
		self.assertIsNone(src.rows)
		self.assertTrue(src.unavailable)
		self.assertEqual(src.target, RECEIVABLES)

		self.assertFalse(chk["ok"])
		self.assertTrue(chk["blocked"])
		self.assertTrue(chk.get("unverified"))
		self.assertIsNone(chk["ar_balance"])
		self.assertIsNone(chk["overdue_amount"])
		self.assertIsNone(chk["exposure"])
		self.assertIsNone(chk["over_limit_by"])
		self.assertTrue(chk["reasons"])
		self.assertNotRegex(" ".join(chk["reasons"]), vendor_re)

		self.assertTrue(status["applies"])
		self.assertFalse(status["ok"])
		self.assertTrue(status["blocked"])
		self.assertTrue(status.get("unverified"))

		self.assertIsNone(terms["balance"])

		self.assertTrue(cl.get("unverified"))
		self.assertEqual(cl["items"], [])
		self.assertTrue(ov.get("unverified"))
		self.assertEqual(ov["items"], [])
		self.assertIsNone(ov["total"])
		by_key = {c["key"]: c for c in checks}
		self.assertTrue(by_key["credit_limit"].get("unverified"))
		self.assertTrue(by_key["overdue_ar"].get("unverified"))

		self.assertTrue(ar_bal.get("unverified"))
		self.assertIsNone(ar_bal["carried_forward"])

		ar_ledger = next(r for r in lb["ledgers"] if r["ledger"] == "AR")
		self.assertTrue(lb.get("ar_kept_in_erp"))
		self.assertTrue(lb.get("ar_unverified"))
		self.assertTrue(ar_ledger.get("kept_in_erp"))
		self.assertTrue(ar_ledger.get("unverified"))
		self.assertIsNone(ar_ledger["balance"])

		self.assertIsNone(snap["city_ledger"])
		self.assertIsNone(snap["city_ledger_overdue"])
		# Caught PermissionError must never leak raw error pop-ups into message_log.
		self.assertEqual(list(frappe.local.message_log), before_log)

		# strict=True and DSO still refuse cleanly.
		with patch("kamra.erp_bridge.bridge_call", side_effect=deny_ar):
			with self.assertRaises(frappe.PermissionError) as strict_exc:
				receivables_source(PROP_A, strict=True)
			self.assertIn("Accounts Receivable", str(strict_exc.exception))
			with self.assertRaises(frappe.PermissionError) as dso_exc:
				custom_reports._fetch_dso(PROP_A, from_date=TODAY, to_date=TODAY)
			self.assertIn("Not verified", str(dso_exc.exception))
			self.assertNotIn("kept in the accounting system", str(dso_exc.exception).lower())

	def test_ledger_balances_marks_ar_kept_in_erp_or_unverified_and_refuses_ar_detail(self):
		from kamra import ledger

		make_property(PROP_A, business_date=TODAY)
		company, account, _folio = self._seed_stale_city_ledger(credit_limit=2000.0, amount=5000.0, days_ago=60)

		# Off-bridge: unchanged numeric AR row, ledger_detail("AR") allowed.
		self.answer = None
		with self.installed():
			local = ledger.ledger_balances(PROP_A)
			local_detail = ledger.ledger_detail(PROP_A, "AR")
		local_ar = next(r for r in local["ledgers"] if r["ledger"] == "AR")
		self.assertIsInstance(local_ar["balance"], float)
		self.assertFalse(local.get("ar_kept_in_erp"))
		self.assertFalse(local.get("ar_unverified"))
		self.assertEqual(local_detail["ledger"], "AR")

		# Bridged & ERP reachable: AR row is kept_in_erp (never 0.00 / stale balance),
		# and drilling into AR detail refuses naming Accounts Receivable while Guest still works.
		self.keeps(receivables=RECEIVABLES)
		self.ar_answer = {"accounts": [{
			"account": account, "company": company, "customer": company, "account_number": "CL-UT-1",
			"credit_limit": 2000.0, "balance": 400.0, "open_balance": 400.0, "unapplied_credit": 0.0,
			"open_items": [],
		}]}
		with self.installed():
			bridged = ledger.ledger_balances(PROP_A)
			guest_detail = ledger.ledger_detail(PROP_A, "Guest")
			with self.assertRaises(frappe.PermissionError) as detail_exc:
				ledger.ledger_detail(PROP_A, "AR")
		bridged_ar = next(r for r in bridged["ledgers"] if r["ledger"] == "AR")
		self.assertTrue(bridged.get("ar_kept_in_erp"))
		self.assertFalse(bridged.get("ar_unverified"))
		self.assertTrue(bridged_ar.get("kept_in_erp"))
		self.assertFalse(bridged_ar.get("unverified"))
		self.assertIsNone(bridged_ar["balance"])
		self.assertIsNone(bridged_ar["debit"])
		self.assertIsNone(bridged_ar["credit"])
		self.assertEqual(guest_detail["ledger"], "Guest")
		self.assertIn("Accounts Receivable", str(detail_exc.exception))

		# Bridged & ERP unreachable: AR row marked unverified with all None.
		self.ar_answer = None
		with self.installed():
			down = ledger.ledger_balances(PROP_A)
		down_ar = next(r for r in down["ledgers"] if r["ledger"] == "AR")
		self.assertTrue(down.get("ar_kept_in_erp"))
		self.assertTrue(down.get("ar_unverified"))
		self.assertTrue(down_ar.get("kept_in_erp"))
		self.assertTrue(down_ar.get("unverified"))
		self.assertIsNone(down_ar["balance"])
		self.assertIsNone(down_ar["debit"])
		self.assertIsNone(down_ar["credit"])

	def test_every_receivables_caller_is_verified_to_fail_closed_on_none(self):
		"""Contract guard: every production caller must use the one helper receivables_source (never
		erp_receivables directly), alias imports and function-scoped raw tabCity Ledger Entry queries
		are enforced, and every strict=False caller is proven to fail closed when rows is None."""
		from kamra import audit_report, credit, custom_reports, ledger, night_audit
		from kamra.erp_books import ReceivablesSource
		from kamra.settings import write_kv

		app_root = Path(frappe.get_app_path("kamra"))
		found_strict: set[tuple[str, str]] = set()
		found_nonstrict: set[tuple[str, str]] = set()
		direct_erp_receivables: list[str] = []
		dynamic_strict: list[str] = []
		star_imports: list[str] = []
		raw_cle_counts: Counter[tuple[str, str]] = Counter()

		for path in sorted(app_root.rglob("*.py")):
			rel = path.relative_to(app_root.parent).as_posix()
			if any(part in ("tests", "scripts", "patches") for part in path.parts):
				continue
			text = path.read_text(encoding="utf-8")
			scan = _scan_receivables_module(rel, text)
			found_strict |= scan["strict"]
			found_nonstrict |= scan["nonstrict"]
			direct_erp_receivables.extend(scan["direct_erp_receivables"])
			dynamic_strict.extend(scan["dynamic_strict"])
			star_imports.extend(scan["star_imports"])
			raw_cle_counts.update(scan["raw_cle"])
			self.assertEqual(
				sum(scan["raw_cle"].values()),
				scan["text_cle_count"],
				f"{rel}: raw tabCity Ledger Entry count must match AST string literals",
			)

		self.assertEqual(direct_erp_receivables, [], "Call receivables_source(), never erp_receivables() directly")
		self.assertEqual(star_imports, [], "Do not star-import kamra.erp_books")
		self.assertEqual(dynamic_strict, [], "Pass a literal bool for strict= on receivables_source()")
		self.assertEqual(
			dict(raw_cle_counts),
			EXPECTED_RAW_CLE_COUNTS,
			"Unexpected raw tabCity Ledger Entry reader (function-level) — route through receivables_source()",
		)
		expected_strict = {
			("kamra/ledger.py", "city_ledger_aging"),
		}
		expected_nonstrict = {
			("kamra/credit.py", "check_company_credit"),
			("kamra/credit.py", "company_credit_terms"),
			("kamra/night_audit.py", "check_credit_limit"),
			("kamra/night_audit.py", "check_overdue_ar"),
			("kamra/audit_report.py", "_balances"),
			("kamra/custom_reports.py", "_fetch_dso"),
			("kamra/ledger.py", "ledger_balances"),
		}
		self.assertEqual(
			found_strict, expected_strict,
			"Update STRICT_CALLERS inventory and add a fail-closed assertion for any new receivables_source() caller",
		)
		self.assertEqual(
			found_nonstrict, expected_nonstrict,
			"New strict=False receivables_source() caller detected — register it here and assert fail-closed handling of rows=None",
		)

		# Execute every registered caller against an unavailable ReceivablesSource(in_erp=True, rows=None).
		make_property(PROP_A, business_date=TODAY)
		company, _account, _folio = self._seed_stale_city_ledger(credit_limit=2000.0, amount=5000.0, days_ago=60)
		write_kv(PROP_A, "credit_control", "block")
		unavail = ReceivablesSource(in_erp=True, rows=None, target=RECEIVABLES)

		self.keeps(receivables=RECEIVABLES)
		self.ar_answer = None
		with self.installed():
			for _mod, fn in sorted(expected_strict):
				with self.subTest(strict_caller=fn), self.assertRaises(frappe.PermissionError):
					ledger.city_ledger_aging(PROP_A)

		with patch("kamra.erp_books.receivables_source", return_value=unavail), \
				patch("kamra.ledger.receivables_source", return_value=unavail):
			chk = credit.check_company_credit(company, PROP_A, 100.0)
			self.assertFalse(chk["ok"])
			self.assertTrue(chk["blocked"])
			self.assertIsNone(chk["ar_balance"])
			self.assertIsNone(credit.company_credit_terms(company, PROP_A)["balance"])
			self.assertTrue(night_audit.check_credit_limit(PROP_A).get("unverified"))
			ov = night_audit.check_overdue_ar(PROP_A)
			self.assertTrue(ov.get("unverified"))
			self.assertIsNone(ov["total"])
			ar = next(b for b in audit_report._balances(PROP_A, TODAY) if b["ledger"] == "AR")
			self.assertTrue(ar.get("unverified"))
			self.assertIsNone(ar["carried_forward"])
			lb = ledger.ledger_balances(PROP_A)
			lb_ar = next(r for r in lb["ledgers"] if r["ledger"] == "AR")
			self.assertTrue(lb_ar.get("unverified"))
			self.assertIsNone(lb_ar["balance"])
			with self.assertRaises(frappe.PermissionError) as dso_exc:
				custom_reports._fetch_dso(PROP_A, from_date=TODAY, to_date=TODAY)
			self.assertIn("Not verified", str(dso_exc.exception))
			self.assertNotIn("kept in the accounting system", str(dso_exc.exception).lower())

	def test_ast_caller_guard_kills_alias_import_and_allowlisted_module_raw_cle_mutants(self):
		"""Regression guard for the two R3 surviving mutants: alias imports
		(`from kamra.erp_books import receivables_source as rs`) and raw
		`tabCity Ledger Entry` SQL inside already-allowlisted `kamra/ledger.py`."""
		alias_mod = _scan_receivables_module(
			"kamra/day_use.py",
			"from kamra.erp_books import receivables_source as rs\n"
			"def sneak(prop):\n"
			"    return rs(prop, strict=False)\n",
		)
		self.assertIn(("kamra/day_use.py", "sneak"), alias_mod["nonstrict"])

		local_alias = _scan_receivables_module(
			"kamra/day_use.py",
			"def sneak(prop):\n"
			"    from kamra.erp_books import erp_receivables as er\n"
			"    return er(prop)\n",
		)
		self.assertEqual(local_alias["direct_erp_receivables"], ["kamra/day_use.py:sneak"])

		rebound_alias = _scan_receivables_module(
			"kamra/day_use.py",
			"from kamra import erp_books as eb\n"
			"def sneak(prop):\n"
			"    f = eb.receivables_source\n"
			"    return f(prop, strict=False)\n",
		)
		self.assertIn(("kamra/day_use.py", "sneak"), rebound_alias["nonstrict"])

		dynamic_mod = _scan_receivables_module(
			"kamra/day_use.py",
			"from kamra.erp_books import receivables_source as rs\n"
			"def sneak(prop, flag):\n"
			"    return rs(prop, strict=flag)\n",
		)
		self.assertEqual(dynamic_mod["dynamic_strict"], ["kamra/day_use.py:sneak"])

		rogue_ledger_fn = _scan_receivables_module(
			"kamra/ledger.py",
			"def rogue_ar_query():\n"
			"    return frappe.db.sql('SELECT * FROM `tabCity Ledger Entry`')\n",
		)
		self.assertIn(("kamra/ledger.py", "rogue_ar_query"), rogue_ledger_fn["raw_cle"])
		self.assertNotIn(("kamra/ledger.py", "rogue_ar_query"), EXPECTED_RAW_CLE_COUNTS)

		extra_in_allowlisted = _scan_receivables_module(
			"kamra/ledger.py",
			"def _account_position(account):\n"
			"    a = 'SELECT * FROM `tabCity Ledger Entry`'\n"
			"    b = 'SELECT * FROM `tabCity Ledger Entry`'\n"
			"    return a, b\n",
		)
		self.assertEqual(extra_in_allowlisted["raw_cle"][("kamra/ledger.py", "_account_position")], 2)
		self.assertGreater(
			extra_in_allowlisted["raw_cle"][("kamra/ledger.py", "_account_position")],
			EXPECTED_RAW_CLE_COUNTS[("kamra/ledger.py", "_account_position")],
		)

	def test_make_property_succeeds_when_country_and_currency_are_mandatory_without_defaults(self):
		"""Under enterprise-hospitality@1, Property.country and Property.currency carry reqd=1 with blank
		defaults; audit_fixtures.make_property must still create properties cleanly."""
		setters = (
			("Property-country-reqd", "country", "reqd", "Check", "1"),
			("Property-country-default", "country", "default", "Text", ""),
			("Property-currency-reqd", "currency", "reqd", "Check", "1"),
			("Property-currency-default", "currency", "default", "Text", ""),
		)
		for name, field_name, prop, prop_type, value in setters:
			if not frappe.db.exists("Property Setter", name):
				frappe.get_doc({
					"doctype": "Property Setter",
					"doctype_or_field": "DocField",
					"doc_type": "Property",
					"field_name": field_name,
					"property": prop,
					"property_type": prop_type,
					"value": value,
				}).insert(ignore_permissions=True)
		self.addCleanup(frappe.clear_cache, doctype="Property")
		frappe.clear_cache(doctype="Property")
		created = make_property("UT Mandatory Country Prop")
		doc = frappe.get_doc("Property", created)
		self.assertTrue(doc.country)
		self.assertTrue(doc.currency)

	def test_off_bridge_credit_control_still_reads_the_city_ledger(self):
		from kamra import credit
		from kamra.settings import write_kv

		company, _account, _folio = self._seed_stale_city_ledger(credit_limit=2000.0, amount=5000.0, days_ago=60)
		write_kv(PROP_A, "credit_control", "block")
		self.answer = None
		with self.installed():
			res = credit.check_company_credit(company, PROP_A, 500.0)
		self.assertEqual(res["ar_balance"], 5000.0)
		self.assertTrue(res["blocked"])
		self.assertNotIn(ERP_RECEIVABLES, self.calls)

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

	def test_erp_receivables_preserves_property_currency_precision(self):
		"""erp_receivables and its downstream readers must honour ISO 4217 minor units:
		KWD 3dp (123.456, never 123.46), UGX 0dp (42000), USD 2dp (125.75)."""
		from frappe.utils import add_days

		from kamra import audit_report, credit, dashboards, ledger, night_audit
		from kamra.erp_books import receivables_source

		prop_kwd = "UT ERP KWD"
		prop_ugx = "UT ERP UGX"
		prop_usd = "UT ERP USD"
		for prop, country, currency in (
			(prop_kwd, "Kuwait", "KWD"),
			(prop_ugx, "Uganda", "UGX"),
			(prop_usd, "United States", "USD"),
		):
			make_property(prop, country=country, currency=currency, business_date=TODAY)
			self.addCleanup(frappe.clear_document_cache, "Property", prop)

		self.keeps(receivables=RECEIVABLES)

		# KWD: 3 decimal places preserved through erp_receivables and every reader
		self.ar_answer = {"accounts": [{
			"account": "CLA-KWD-1",
			"company": "Gulf Corp",
			"customer": "Gulf Corp",
			"account_number": "CL-KWD-1",
			"credit_limit": 100.125,
			"balance": 123.456,
			"open_balance": 123.456,
			"unapplied_credit": 1.005,
			"open_items": [{
				"folio": None,
				"invoice_number": "ACC-SINV-KWD-1",
				"voucher_type": "Sales Invoice",
				"voucher_no": "ACC-SINV-KWD-1",
				"invoice_date": str(add_days(TODAY, -25)),
				"due_date": str(add_days(TODAY, -10)),
				"outstanding": 124.461,
				"disputed": 0,
			}],
		}]}
		with self.installed():
			kwd_src = receivables_source(prop_kwd, strict=False)
			kwd_aging = ledger.city_ledger_aging(prop_kwd)
			kwd_snap = dashboards.owner_snapshot(prop_kwd)
			kwd_cl = night_audit.check_credit_limit(prop_kwd)
			kwd_ov = night_audit.check_overdue_ar(prop_kwd)
			kwd_terms = credit.company_credit_terms("Gulf Corp", prop_kwd)
			kwd_chk = credit.check_company_credit("Gulf Corp", prop_kwd, 0.0)
			kwd_ar = next(b for b in audit_report.build(prop_kwd, TODAY)["balances"] if b["ledger"] == "AR")

		kwd_row = kwd_src.rows[0]
		self.assertEqual(kwd_row["balance"], 123.456)
		self.assertEqual(kwd_row["open_balance"], 123.456)
		self.assertEqual(kwd_row["credit_limit"], 100.125)
		self.assertEqual(kwd_row["unapplied_credit"], 1.005)
		self.assertEqual(kwd_row["open_items"][0]["outstanding"], 124.461)
		self.assertEqual(kwd_aging["totals"]["d1_30"], 123.456)
		self.assertEqual(kwd_aging["totals"]["overdue"], 123.456)
		self.assertEqual(kwd_snap["city_ledger"], 123.456)
		self.assertEqual(kwd_snap["city_ledger_overdue"], 123.456)
		self.assertEqual(kwd_cl["items"][0]["ar_balance"], 123.456)
		self.assertEqual(kwd_cl["items"][0]["over_by"], 23.331)
		self.assertEqual(kwd_ov["items"][0]["overdue"], 123.456)
		self.assertEqual(kwd_ov["items"][0]["balance"], 123.456)
		self.assertEqual(kwd_ov["total"], 123.456)
		self.assertEqual(kwd_terms["balance"], 123.456)
		self.assertEqual(kwd_chk["ar_balance"], 123.456)
		self.assertEqual(kwd_chk["overdue_amount"], 123.456)
		self.assertEqual(kwd_ar["carried_forward"], 123.456)

		# UGX: 0 decimal places (whole shillings)
		self.ar_answer = {"accounts": [{
			"account": "CLA-UGX-1",
			"company": "Kampala Corp",
			"customer": "Kampala Corp",
			"account_number": "CL-UGX-1",
			"credit_limit": 500000,
			"balance": 42000,
			"open_balance": 42000.4,
			"unapplied_credit": 0,
			"open_items": [{
				"folio": None,
				"invoice_number": "ACC-SINV-UGX-1",
				"voucher_type": "Sales Invoice",
				"voucher_no": "ACC-SINV-UGX-1",
				"invoice_date": str(add_days(TODAY, -25)),
				"due_date": str(add_days(TODAY, -10)),
				"outstanding": 42000.4,
				"disputed": 0,
			}],
		}]}
		with self.installed():
			ugx_src = receivables_source(prop_ugx, strict=False)
		ugx_row = ugx_src.rows[0]
		self.assertEqual(ugx_row["balance"], 42000.0)
		self.assertEqual(ugx_row["open_balance"], 42000.0)
		self.assertEqual(ugx_row["open_items"][0]["outstanding"], 42000.0)

		# USD: 2 decimal places
		self.ar_answer = {"accounts": [{
			"account": "CLA-USD-1",
			"company": "Harbor Corp",
			"customer": "Harbor Corp",
			"account_number": "CL-USD-1",
			"credit_limit": 1000.0,
			"balance": 125.75,
			"open_balance": 125.75,
			"unapplied_credit": 0.0,
			"open_items": [{
				"folio": None,
				"invoice_number": "ACC-SINV-USD-1",
				"voucher_type": "Sales Invoice",
				"voucher_no": "ACC-SINV-USD-1",
				"invoice_date": str(add_days(TODAY, -25)),
				"due_date": str(add_days(TODAY, -10)),
				"outstanding": 125.75,
				"disputed": 0,
			}],
		}]}
		with self.installed():
			usd_src = receivables_source(prop_usd, strict=False)
		self.assertEqual(usd_src.rows[0]["balance"], 125.75)

	def test_ledger_round_cents_and_folio_totals_preserve_kwd_3dp_precision(self):
		"""round_cents and folio totals must preserve 3dp KWD (12.345, NOT 12.35)
		and 0dp UGX while keeping default 2dp behaviour unchanged."""
		from kamra import checkout, ledger
		from kamra import folio as folio_mod
		from kamra.tests.audit_fixtures import (
			d,
			guest_folio,
			make_reservation,
			make_room,
			make_room_type,
		)

		prop_kwd = "UT Folio KWD"
		prop_ugx = "UT Folio UGX"
		for prop, country, currency in (
			(prop_kwd, "Kuwait", "KWD"),
			(prop_ugx, "Uganda", "UGX"),
		):
			make_property(
				prop, country=country, currency=currency, business_date=TODAY,
				gst_rate_low=0, gst_rate_high=0,
			)
			self.addCleanup(frappe.clear_document_cache, "Property", prop)

		# Real KWD folio totals preserve 12.345 through posting, settlement_position,
		# force_advance_bill, and folio_reconciliation (never rounded to 12.35).
		rt_kwd = make_room_type(prop_kwd, "KSTD-FOL", base_price=12.345)
		room_kwd = make_room(prop_kwd, rt_kwd, "K-31")
		res_kwd = make_reservation(
			prop_kwd, rt_kwd, d(-1), d(0), status="Checked In", room=room_kwd,
		)
		folio_mod.post_remaining_nights(res_kwd)
		folio_doc = frappe.get_doc("Folio", guest_folio(res_kwd.name))
		self.assertEqual(float(folio_doc.grand_total), 12.345)
		self.assertEqual(float(folio_doc.balance), 12.345)

		with self.subTest("settlement_position"):
			pos = checkout.settlement_position(res_kwd.name)
			self.assertEqual(pos["owed"], 12.345)
			self.assertEqual(pos["folios"][0]["balance"], 12.345)

		with self.subTest("folio_reconciliation"):
			recon = ledger.folio_reconciliation(prop_kwd)
			self.assertTrue(recon["in_balance"])
			self.assertEqual(recon["folios_net"], 12.345)
			self.assertEqual(recon["ledger_net"], 12.345)

		with self.subTest("force_advance_bill"):
			room_kwd_2 = make_room(prop_kwd, rt_kwd, "K-32")
			res_kwd_2 = make_reservation(
				prop_kwd, rt_kwd, d(0), d(1), status="Checked In", room=room_kwd_2,
			)
			adv = ledger.force_advance_bill(res_kwd_2.name, nights="entire")
			self.assertEqual(adv["amount"], 12.345)
			self.assertEqual(float(adv["balance"]), 12.345)

		with self.subTest("round_cents"):
			# round_cents supports explicit places / property / currency precision
			self.assertEqual(ledger.round_cents(12.345, places=3), 12.345)
			self.assertEqual(ledger.round_cents(12.345, property=prop_kwd), 12.345)
			self.assertEqual(ledger.round_cents(12.345, currency="KWD"), 12.345)
			self.assertEqual(ledger.round_cents(42000.4, property=prop_ugx), 42000.0)
			self.assertEqual(ledger.round_cents(2.345), 2.35)

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
