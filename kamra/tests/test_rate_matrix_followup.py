"""Follow-up rate matrix & multi-currency fixes (P2-1, P2-2, P2-3, P3-1).

P2-1: A legacy pre-R8 fractional folio reopened by `cancel_invoice` and then
      paid without changing charges keeps its stored charges/tax/grand_total
      and stays in balance (`balance == 0`, `folio_reconciliation` in balance);
      if a charge IS mutated after reopen, `_recalculate` + rounding ledger sync
      update totals and keep the ledger in balance.
P2-2: 3-decimal ISO 4217 currencies (KWD) keep full 3 decimals in the remaining
      readers: `night_audit.journal_by_cashier` / `_deposit_ledger` / checks,
      `ledger._sync_account_balance` and AR / city ledger helpers (plus City
      Ledger DocType field precision >= 3), and `books._ledger` / `_r` /
      `balance_sheet` / `_revenue`.
P2-3: 0% stays (including derived -50% plans on 0% / bridged properties) never
      carry sub-minor-unit fractions (SAP TCURX / OPERA): nightly room amounts
      round to the currency's ISO 4217 minor unit at the line so
      `charges_total == sum(c.amount)` holds with payable whole units.
P3-1: `folio_invoice.summary_by_head` folds the document-level tax rounding
      delta onto the dominant taxed head so `summary_by_head` totals reconcile
      with `tax_summary` and `folio.grand_total`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from kamra.tests.rate_matrix_harness import Doc, RateMatrixHarness
from kamra.tests.test_rate_matrix_parity_r6 import QuotePatches, stub_module
from kamra.tests.test_rate_matrix_parity_r11 import _tax_invoice_localization

ROOT = Path(__file__).resolve().parents[2]
CLA_JSON = ROOT / "kamra/kamra/doctype/city_ledger_account/city_ledger_account.json"
CLE_JSON = ROOT / "kamra/kamra/doctype/city_ledger_entry/city_ledger_entry.json"


class LegacyReopenThenPayBalanceTest(QuotePatches, RateMatrixHarness):
	"""P2-1: Legacy fractional folio reopened by cancel_invoice then paid stays in balance."""

	modules = (
		"kamra.pricing",
		"kamra.folio",
		"kamra.authz",
		"kamra.api",
		"kamra.ledger",
	)

	def extra_modules(self):
		return {
			"kamra.localization": _tax_invoice_localization(Decimal("18")),
			"kamra.checkout": stub_module("kamra.checkout", _NoTrace=self._no_trace),
			"kamra.erp_bridge": stub_module(
				"kamra.erp_bridge",
				bridge_invoice=lambda f: False,
				refuse_bridge_correction=lambda f: None,
			),
			"kamra.cashier": stub_module(
				"kamra.cashier",
				require_open_session=lambda prop: None,
				assert_cashier_mode_allowed=lambda prop, mode: None,
				record_cashier_txn=lambda *a, **kw: None,
			),
			"kamra.masters": stub_module(
				"kamra.masters",
				default_mode_for_tender=lambda tender, prop=None: tender,
				is_system_payment_method=lambda mode, prop=None: True,
				payment_tender_type=lambda mode, prop=None: mode,
			),
			"kamra.savings": stub_module("kamra.savings", log_action=lambda *a, **kw: None),
			"kamra.webhooks": stub_module("kamra.webhooks", emit_payment=lambda *a, **kw: None),
			"kamra.deposit": stub_module("kamra.deposit", record_collected=lambda *a, **kw: None),
		}

	@staticmethod
	@contextmanager
	def _no_trace(name):
		yield

	def setUp(self):
		super().setUp()
		self.roles = ["Finance", "Hotel Admin", "Front Desk"]
		self.authz.restricted_properties = lambda user=None: None
		self.docs["Property", "Test Hotel"].update(
			currency="UGX",
			property_name="Test Hotel",
			business_date="2026-11-24",
			rates_include_tax=0,
		)
		self.frappe.flags.kamra_invoice_cancel = False
		self.frappe.model = stub_module("frappe.model")
		self.frappe.model.naming = stub_module(
			"frappe.model.naming", make_autoname=lambda pat: "INV-TH-26-00002",
		)
		self.sys_modules_patch = patch.dict(
			"sys.modules",
			{"frappe.model": self.frappe.model, "frappe.model.naming": self.frappe.model.naming},
		)
		self.sys_modules_patch.start()
		self.addCleanup(self.sys_modules_patch.stop)

	def _pre_r8_closed_folio(self):
		night1 = Doc(
			name="FC-1",
			posting_date="2026-11-20",
			charge_type="Room",
			description="Room 101 · DLX",
			qty=1,
			rate=246713.0,
			amount=246713.0,
			gst_rate=18.0,
			gst_amount=44408.34,
			total=291121.34,
		)
		night2 = Doc(night1, name="FC-2", posting_date="2026-11-21")
		pay1 = Doc(name="FP-1", amount=500000.0, mode="Cash", payment_kind="Payment", currency="UGX")
		pay1.as_dict = lambda: dict(pay1)
		folio = self.add(
			"Folio",
			"FOL-PRE-999",
			property="Test Hotel",
			folio_type="Guest",
			status="Closed",
			invoice_number="INV-THH-PRE-999",
			charges=[night1, night2],
			payments=[pay1],
			charges_total=493426.0,
			tax_total=88816.68,
			grand_total=582242.68,
			payments_total=500000.0,
			balance=82242.68,
		)
		folio.save = lambda **kw: folio
		folio.as_dict = lambda: {k: v for k, v in folio.items() if not callable(v)}

		def _append(field, row):
			d = Doc(row, name=f"ROW-{len(folio[field]) + 1}")
			d.as_dict = lambda: {k: v for k, v in d.items() if not callable(v)}
			folio[field].append(d)
			return d

		folio.append = _append
		return folio

	def test_legacy_reopened_folio_paid_without_charge_change_keeps_totals_and_balance(self):
		folio = self._pre_r8_closed_folio()
		self.frappe.db.set_value = lambda *a, **kw: None
		loc = sys.modules["kamra.localization"]
		loc.FOREIGN_CURRENCY_TENDER_TYPES = ("Cash", "Foreign Cash", "Bank Transfer")
		loc.on_invoice_cancelled = lambda *a, **kw: None
		loc.on_invoice_issued = lambda *a, **kw: None
		loc.assert_payment_mode = lambda *a, **kw: None
		loc.money_text = lambda prop, amt: f"UGX {amt}"

		harness_get_doc = self.frappe.get_doc

		def get_doc_with_insert(doctype, name=None):
			if isinstance(doctype, dict):
				d = Doc(doctype, name="CI-1")
				d.insert = lambda **kw: d
				return d
			return harness_get_doc(doctype, name)

		ledger_writes = []
		with patch.object(self.frappe, "get_doc", get_doc_with_insert), \
				patch.object(self.api, "_pin_guard", lambda *a, **kw: None), \
				patch.object(self.ledger, "record_payment_ledger", lambda doc, p, **kw: ledger_writes.append(p["amount"])):
			self.folio.cancel_invoice("FOL-PRE-999", "Fix guest TIN")
			self.assertEqual(folio.status, "Open")
			self.assertIsNone(folio.invoice_number)

			# Pay exact legacy remaining balance (82,242.68) with NO charge mutation
			self.api.add_folio_payment("FOL-PRE-999", "Cash", 82242.68)

			# Totals stay frozen at legacy invoice amounts (no phantom 0.32 shortfall)
			self.assertEqual(folio.charges_total, 493426.0)
			self.assertEqual(folio.tax_total, 88816.68)
			self.assertEqual(folio.grand_total, 582242.68)
			self.assertEqual(folio.payments_total, 582242.68)
			self.assertEqual(folio.balance, 0.0)
			self.assertEqual(ledger_writes, [82242.68])

			# Re-closing succeeds cleanly at the exact settled legacy total
			new_inv = self.folio.close_folio("FOL-PRE-999")
			self.assertEqual(new_inv, "INV-TH-26-00002")
			self.assertEqual(folio.status, "Closed")
			self.assertEqual(folio.grand_total, 582242.68)
			self.assertEqual(folio.balance, 0.0)

	def test_legacy_reopened_folio_charge_mutation_recalculates_and_syncs_rounding_ledger(self):
		folio = self._pre_r8_closed_folio()
		self.frappe.db.set_value = lambda *a, **kw: None
		loc = sys.modules["kamra.localization"]
		loc.FOREIGN_CURRENCY_TENDER_TYPES = ("Cash", "Foreign Cash", "Bank Transfer")
		loc.on_invoice_cancelled = lambda *a, **kw: None

		harness_get_doc = self.frappe.get_doc

		def get_doc_with_insert(doctype, name=None):
			if isinstance(doctype, dict):
				d = Doc(doctype, name="CI-2")
				d.insert = lambda **kw: d
				return d
			return harness_get_doc(doctype, name)

		rounding_writes = []
		self.frappe.db.sql = lambda *a, **kw: [(0.0,)]
		with patch.object(self.frappe, "get_doc", get_doc_with_insert), \
				patch.object(self.ledger, "write_ledger_entry", lambda **kw: rounding_writes.append(kw)):
			self.folio.cancel_invoice("FOL-PRE-999", "Add late misc charge")
			folio.append("charges", {
				"posting_date": "2026-11-24",
				"charge_type": "Misc",
				"description": "Minibar",
				"qty": 1,
				"rate": 1000.40,
				"amount": 1000.40,
				"gst_rate": 18.0,
			})
			self.folio._recalculate(folio)
			self.ledger.sync_folio_rounding_ledger(folio)

		# 494,426.40 * 1.18 = 583,423.152 -> document-rounded to 583,423.0; rounding ledger credits 0.152
		self.assertEqual(folio.charges_total, 494426.4)
		self.assertEqual(folio.grand_total, 583423.0)
		self.assertEqual(folio.tax_total, 88996.6)
		self.assertEqual(len(rounding_writes), 1)
		self.assertEqual(rounding_writes[0]["credit"], 0.152)


class ThreeDecimalReadersTest(QuotePatches, RateMatrixHarness):
	"""P2-2: 3-decimal currencies (KWD) stay at 3 dp in night_audit, ledger AR/city ledger, and books."""

	modules = (
		"kamra.pricing",
		"kamra.folio",
		"kamra.authz",
		"kamra.ledger",
		"kamra.night_audit",
		"kamra.books",
	)

	def extra_modules(self):
		return {
			"kamra.localization": _tax_invoice_localization(Decimal("5")),
			"kamra.cashier": stub_module("kamra.cashier", _recompute_session_totals=lambda n, persist=False: {"expected_cash": 11.944}),
		}

	def setUp(self):
		super().setUp()
		self.roles = ["Finance", "Hotel Admin", "Night Auditor"]
		self.authz.restricted_properties = lambda user=None: None
		self.add(
			"Property",
			"KWD Hotel",
			currency="KWD",
			property_name="KWD Hotel",
			business_date="2026-11-24",
			rates_include_tax=0,
		)
		self.add(
			"City Ledger Account",
			"CLA-KWD-1",
			property="KWD Hotel",
			company="Kuwait Corp",
			account_number="AR-KWD-1",
			balance=11.944,
			credit_limit=100.0,
			credit_days=30,
			use_default_credit_days=0,
			disabled=0,
		)

	def test_night_audit_journal_by_cashier_and_deposit_ledger_preserve_three_decimal_kwd(self):
		rows = [
			Doc(
				name="FLE-1", business_date="2026-11-24", posted_at="2026-11-24 10:00:00",
				folio="FOL-KWD-1", ledger="Guest", transaction_code="1000",
				description="Room night 1", debit=11.944, credit=0.0,
				cashier="CASH-1", posted_by="desk@example.com", guest_name="Guest A",
				room="KWD-101", code_description="Room", cashier_id="C01",
			),
			Doc(
				name="FLE-2", business_date="2026-11-24", posted_at="2026-11-24 10:05:00",
				folio="FOL-KWD-1", ledger="Guest", transaction_code="1000",
				description="Room night 2", debit=11.944, credit=0.0,
				cashier="CASH-1", posted_by="desk@example.com", guest_name="Guest A",
				room="KWD-101", code_description="Room", cashier_id="C01",
			),
		]
		self.frappe.db.sql = lambda *a, **kw: rows
		journal = self.night_audit.journal_by_cashier("KWD Hotel", "2026-11-24")
		self.assertEqual(journal["lines"][0]["debit"], 11.944)
		self.assertEqual(journal["subtotals"][0]["debit"], 23.888)
		self.assertEqual(journal["total_debit"], 23.888)

		# _deposit_ledger keeps 3 decimals and scales HAVING threshold below 0.009
		dep_calls = []

		def fake_dep_sql(query, params=None, as_dict=False):
			dep_calls.append((query, params))
			return [Doc(
				folio="FOL-KWD-1", reservation="RES-1", guest_name="Guest A",
				room="KWD-101", status="Confirmed", check_in_date="2026-11-25",
				debit=0.0, credit=23.888,
			)]

		self.frappe.db.sql = fake_dep_sql
		dep = self.night_audit._deposit_ledger("KWD Hotel", "2026-11-24")
		self.assertEqual(dep["items"][0]["balance"], -23.888)
		self.assertEqual(dep["total"], -23.888)
		self.assertIn("min_bal", dep_calls[0][1])
		self.assertLessEqual(dep_calls[0][1]["min_bal"], 0.001)

	def test_ledger_city_ledger_and_ar_helpers_preserve_three_decimal_kwd(self):
		# 1. _sync_account_balance preserves 23.888 (not 23.89)
		saved = {}
		self.frappe.db.set_value = lambda dt, name, field, val=None, **kw: saved.update({(dt, name, field): val})
		self.frappe.db.sql = lambda *a, **kw: [Doc(debit=23.888, credit=0.0)]
		bal = self.ledger._sync_account_balance("CLA-KWD-1")
		self.assertEqual(bal, 23.888)
		self.assertEqual(saved[("City Ledger Account", "CLA-KWD-1", "balance")], 23.888)

		# 2. city_ledger_open_folios preserves 3 decimals
		def open_folios_sql(query, params=None, as_dict=False):
			if "tabCity Ledger Entry" in query and "tabFolio Ledger Entry" not in query:
				return [Doc(
					folio="FOL-KWD-1", invoice_date="2026-11-20", due_date="2026-12-20",
					first_date="2026-11-20", invoice_number="INV-KWD-1",
					disputed=0, dispute_note=None, billed=23.888,
				)]
			return [("FOL-KWD-1", 11.944)]

		self.frappe.db.sql = open_folios_sql
		open_rows = self.ledger.city_ledger_open_folios("CLA-KWD-1")
		self.assertEqual(open_rows[0]["billed"], 23.888)
		self.assertEqual(open_rows[0]["paid"], 11.944)
		self.assertEqual(open_rows[0]["outstanding"], 11.944)

		# 3. locked_account_figures preserves 3 decimals
		def locked_sql(query, params=None, as_dict=False):
			if "tabFolio Ledger Entry" in query:
				return [Doc(folio="FOL-KWD-1", credit=11.944, debit=0.0)]
			return [
				Doc(
					folio="FOL-KWD-1", kind="Invoice", debit=23.888, credit=0.0,
					posting_date="2026-11-20", invoice_date="2026-11-20",
					due_date="2026-12-20", invoice_number="INV-KWD-1",
					disputed=0, dispute_note=None,
				),
				Doc(
					folio=None, kind="Payment", debit=0.0, credit=11.944,
					posting_date="2026-11-21", invoice_date=None,
					due_date=None, invoice_number=None, disputed=0, dispute_note=None,
				),
			]

		self.frappe.db.sql = locked_sql
		figs = self.ledger.locked_account_figures("CLA-KWD-1")
		self.assertEqual(figs["ar_balance"], 11.944)
		self.assertEqual(figs["open_items"][0]["billed"], 23.888)
		self.assertEqual(figs["open_items"][0]["paid"], 11.944)
		self.assertEqual(figs["open_items"][0]["outstanding"], 11.944)

		# 4. _account_position and city_ledger_aging preserve 3 decimals
		def pos_sql(query, params=None, as_dict=False):
			if "AS total_debit" in query:
				return [Doc(total_debit=23.888, total_credit=11.944, balance=11.944)]
			return open_folios_sql(query, params, as_dict)

		self.frappe.db.sql = pos_sql
		self.frappe.get_all = lambda dt, **kw: [self.docs["City Ledger Account", "CLA-KWD-1"]] if dt == "City Ledger Account" else []
		aging = self.ledger.city_ledger_aging("KWD Hotel")
		self.assertEqual(aging["accounts"][0]["open_balance"], 11.944)
		self.assertEqual(aging["accounts"][0]["not_due"], 11.944)
		self.assertEqual(aging["totals"]["not_due"], 11.944)

		# 5. city_ledger_history and payment_bills preserve 3 decimals
		pb_calls = []

		def hist_sql(query, params=None, as_dict=False):
			if "source_doctype = 'City Ledger Entry'" in query:
				pb_calls.append((query, params))
				return [Doc(payment="CLE-PAY-1", folio="FOL-KWD-1", guest_name="Guest A", amount=11.944, invoice_number="INV-KWD-1")]
			return [
				Doc(name="CLE-INV-1", posting_date="2026-11-20", kind="Invoice", folio="FOL-KWD-1",
				    invoice_number="INV-KWD-1", due_date="2026-12-20", debit=23.888, credit=0.0,
				    reference="FOL-KWD-1", notes="", guest_name="Guest A", reservation="RES-1"),
				Doc(name="CLE-PAY-1", posting_date="2026-11-21", kind="Payment", folio=None,
				    invoice_number=None, due_date=None, debit=0.0, credit=11.944,
				    reference="WIRE", notes="", guest_name=None, reservation=None),
			]

		self.frappe.db.sql = hist_sql
		hist = self.ledger.city_ledger_history("CLA-KWD-1")
		self.assertEqual(hist["balance"], 11.944)
		self.assertEqual(hist["entries"][0]["balance"], 23.888)
		self.assertEqual(hist["entries"][1]["balance"], 11.944)
		self.assertEqual(hist["entries"][1]["applied"][0]["amount"], 11.944)
		self.assertIn("eps", pb_calls[0][1])
		self.assertLess(pb_calls[0][1]["eps"], 0.005)

		# 6. DocType JSON precision >= 3 on City Ledger Account.balance & City Ledger Entry.debit/credit
		cla_fields = {f["fieldname"]: f for f in json.loads(CLA_JSON.read_text(encoding="utf-8"))["fields"]}
		cle_fields = {f["fieldname"]: f for f in json.loads(CLE_JSON.read_text(encoding="utf-8"))["fields"]}
		self.assertTrue(cla_fields["balance"].get("precision") and int(cla_fields["balance"]["precision"]) >= 3)
		for fname in ("debit", "credit"):
			self.assertTrue(cle_fields[fname].get("precision") and int(cle_fields[fname]["precision"]) >= 3)

	def test_books_ledger_and_rounding_preserve_three_decimal_kwd(self):
		# _ledger must preserve 3 decimals on KWD
		self.frappe.db.sql = lambda *a, **kw: [(23.888,)]
		self.assertEqual(self.books._ledger("KWD Hotel", "Guest", "2026-11-24"), 23.888)

		# balance_sheet preserves 3 decimals across Guest, City, Deposit, Payables and net
		def bs_sql(query, params=None, as_dict=False):
			if "tabCity Ledger Entry" in query:
				return [(11.944,)]
			if "tabFolio Ledger Entry" in query:
				# params = (property, ledger, as_of)
				return [(23.888,)] if params[1] == "Guest" else [(-11.944,)]
			if "tabVendor Bill Payment" in query:
				return [(0.0,)]
			if "tabVendor Bill" in query and "GROUP BY" in query:
				return []
			if "tabVendor Bill" in query:
				return [(0.0,)]
			return [(0.0,)]

		self.frappe.db.sql = bs_sql
		self.frappe.get_all = lambda *a, **kw: []
		bs = self.books.balance_sheet("KWD Hotel", "2026-06-01")
		by_key = {a["key"]: a["amount"] for a in bs["assets"] + bs["liabilities"]}
		self.assertEqual(by_key["guest_ledger"], 23.888)
		self.assertEqual(by_key["city_ledger"], 11.944)
		self.assertEqual(by_key["advance_deposits"], 11.944)
		self.assertEqual(bs["assets_total"], 35.832)
		self.assertEqual(bs["net"], 23.888)

		# _revenue preserves 3 decimals on KWD
		def rev_sql(query, params=None, as_dict=False):
			if "GROUP BY fc.charge_type" in query:
				return [Doc(charge_type="Room", amount=22.755)]
			return [(0.0,)]

		self.frappe.db.sql = rev_sql
		rev = self.books._revenue("KWD Hotel", "2026-11-01", "2026-11-30")
		self.assertEqual(rev["rooms"], 22.755)


class ZeroTaxMinorUnitLineRoundingTest(QuotePatches, RateMatrixHarness):
	"""P2-3: 0% stays round nightly amounts at the line to the currency's ISO 4217 minor unit."""

	modules = (
		"kamra.pricing",
		"kamra.folio",
		"kamra.authz",
		"kamra.ledger",
	)

	def extra_modules(self):
		return {"kamra.localization": _tax_invoice_localization(Decimal("0"))}

	def setUp(self):
		super().setUp()
		self.docs["Property", "Test Hotel"].update(
			currency="UGX",
			property_name="Test Hotel",
			business_date="2026-11-24",
			rates_include_tax=0,
			gst_mode="Fixed",
			gst_rate_low=0,
			gst_rate_high=0,
		)
		self.add(
			"Room Type", "Test Hotel-FRAC", property="Test Hotel",
			base_price=1000.40, base_occupancy=2, adults_capacity=4,
		)
		self.add(
			"Room Type", "Test Hotel-ODD", property="Test Hotel",
			base_price=246713.0, base_occupancy=2, adults_capacity=4,
		)
		self.add(
			"Rate Plan", "Test Hotel-BAR", property="Test Hotel",
			rate_plan_name="BAR", code="BAR", currency="UGX",
			modifier_type="Percent", modifier_value=0,
			rounding_step=0, rounding_mode="Nearest",
		)
		self.add(
			"Rate Plan", "Test Hotel-HALF", property="Test Hotel",
			rate_plan_name="Half Price", code="HALF", currency="UGX",
			base_rate_plan="Test Hotel-BAR",
			modifier_type="Percent", modifier_value=-50,
			rounding_step=0, rounding_mode="Nearest",
		)
		self.add(
			"Property", "KWD Zero", currency="KWD", property_name="KWD Zero",
			business_date="2026-11-24", rates_include_tax=0,
			gst_mode="Fixed", gst_rate_low=0, gst_rate_high=0,
		)
		self.add(
			"Room Type", "KWD Zero-EXACT", property="KWD Zero",
			base_price=11.375, base_occupancy=2, adults_capacity=4,
		)
		self.add(
			"Room Type", "KWD Zero-SUB", property="KWD Zero",
			base_price=11.3755, base_occupancy=2, adults_capacity=4,
		)
		self.patch_quote()

	def test_zero_tax_ugx_stay_rounds_nightly_amount_at_line_and_matches_charges_total(self):
		q = self.pricing.quote("Test Hotel", "Test Hotel-FRAC", "2026-12-01", "2026-12-03", 2, 0)
		self.assertEqual([n["rate"] for n in q["nightly"]], [1000.0, 1000.0])
		self.assertEqual(q["room_total"], 2000.0)
		self.assertEqual(q["amount_before_tax"], 2000.0)
		self.assertEqual(q["tax_amount"], 0.0)
		self.assertEqual(q["amount_after_tax"], 2000.0)

	def test_zero_tax_derived_half_price_plan_rounds_ugx_nightly_at_line_not_half_shilling(self):
		# 246,713 * 0.5 = 123,356.5 -> must round at line to 123,357 UGX (not half-shilling .5)
		q = self.pricing.quote(
			"Test Hotel", "Test Hotel-ODD", "2026-12-01", "2026-12-03", 2, 0,
			rate_plan="Test Hotel-HALF",
		)
		self.assertEqual([n.get("planned_rate", n["rate"]) for n in q["nightly"]], [123357.0, 123357.0])
		self.assertEqual(q["room_total"], 246714.0)
		self.assertEqual(q["amount_before_tax"], 246714.0)
		self.assertEqual(q["tax_amount"], 0.0)
		self.assertEqual(q["amount_after_tax"], 246714.0)

		# Folio with those posted lines keeps charges_total == sum(lines) == grand_total
		folio = Doc(
			name="FOL-HALF", property="Test Hotel", status="Open",
			charges=[
				Doc(name="FC-1", amount=123357.0, gst_rate=0.0),
				Doc(name="FC-2", amount=123357.0, gst_rate=0.0),
			],
			payments=[],
		)
		self.folio._recalculate(folio)
		self.assertEqual(folio.charges_total, 246714.0)
		self.assertEqual(folio.grand_total, 246714.0)

	def test_zero_tax_post_room_night_rounds_unlocked_ugx_rate_at_line(self):
		res = self.add(
			"Reservation", "RES-UNLOCKED",
			property="Test Hotel", room_type="Test Hotel-FRAC", room="Test Hotel-101",
			check_in_date="2026-12-01", check_out_date="2026-12-02",
			adults=2, children=0, status="Checked In", night_rates=[],
		)
		folio = self.add(
			"Folio", "FOL-UNLOCKED", property="Test Hotel", reservation="RES-UNLOCKED",
			folio_type="Guest", status="Open", charges=[], payments=[],
		)
		folio.append = lambda f, row: folio[f].append(Doc(row, name=f"FC-{len(folio[f]) + 1}"))
		folio.save = lambda **kw: folio

		with patch.object(self.folio, "_charge_posted", lambda *a, **kw: False), \
				patch.object(self.folio, "current_date_contract_night", lambda *a, **kw: None), \
				patch.object(
					self.folio,
					"line_parts",
					lambda r, ct, amount, *a, **kw: [{"folio": "FOL-UNLOCKED", "amount": amount, "routing_instruction": None}],
				), \
				patch.object(self.folio, "_post_room_levy", lambda *a, **kw: None), \
				patch.object(self.ledger, "save_folio", lambda f, **kw: f):
			self.folio.post_room_night(res, "2026-12-01")

		self.assertEqual(len(folio.charges), 1)
		self.assertEqual(folio.charges[0].amount, 1000.0)
		self.assertEqual(folio.charges_total, 1000.0)
		self.assertEqual(folio.grand_total, 1000.0)

	def test_zero_tax_kwd_preserves_three_decimals_and_rounds_sub_fils(self):
		q_exact = self.pricing.quote("KWD Zero", "KWD Zero-EXACT", "2026-12-01", "2026-12-03", 2, 0)
		self.assertEqual([n["rate"] for n in q_exact["nightly"]], [11.375, 11.375])
		self.assertEqual(q_exact["amount_after_tax"], 22.75)

		q_sub = self.pricing.quote("KWD Zero", "KWD Zero-SUB", "2026-12-01", "2026-12-03", 2, 0)
		self.assertEqual([n["rate"] for n in q_sub["nightly"]], [11.376, 11.376])
		self.assertEqual(q_sub["amount_after_tax"], 22.752)


class SummaryByHeadReconciliationTest(QuotePatches, RateMatrixHarness):
	"""P3-1: folio_invoice.summary_by_head reconciles with tax_summary and document-rounded grand_total."""

	modules = (
		"kamra.pricing",
		"kamra.folio",
		"kamra.authz",
		"kamra.api",
		"kamra.ledger",
	)

	def extra_modules(self):
		return {
			"kamra.localization": _tax_invoice_localization(Decimal("18")),
			"kamra.checkout": stub_module("kamra.checkout", _NoTrace=self._no_trace),
		}

	@staticmethod
	@contextmanager
	def _no_trace(name):
		yield

	def setUp(self):
		super().setUp()
		self.roles = ["Finance", "Hotel Admin"]
		self.authz.restricted_properties = lambda user=None: None
		self.docs["Property", "Test Hotel"].update(
			currency="UGX",
			property_name="Test Hotel",
			business_date="2026-11-24",
			rates_include_tax=0,
		)
		self.add(
			"Property", "KWD Hotel", currency="KWD", property_name="KWD Hotel",
			business_date="2026-11-24", rates_include_tax=0,
		)

	def test_summary_by_head_reconciles_with_document_rounded_tax_and_grand_total(self):
		# UGX: 2 nights @ 246,713 @ 18% VAT -> raw tax 88,816.68, doc-rounded tax 88,817, grand 582,243
		n1 = Doc(name="FC-1", posting_date="2026-12-01", charge_type="Room", description="Night 1",
		         qty=1, rate=246713.0, amount=246713.0, gst_rate=18.0)
		n2 = Doc(n1, name="FC-2", posting_date="2026-12-02", description="Night 2")
		folio = self.add(
			"Folio", "FOL-UGX", property="Test Hotel", folio_type="Guest",
			status="Open", guest_name="Guest UGX", charges=[n1, n2], payments=[],
		)
		folio.as_dict = lambda: {k: v for k, v in folio.items() if not callable(v)}
		self.folio._recalculate(folio)
		inv = self.api.folio_invoice("FOL-UGX")

		room_head = inv["summary_by_head"][0]
		self.assertEqual(room_head["amount"], 493426.0)
		self.assertEqual(room_head["tax"], 88817.0)
		self.assertEqual(room_head["total"], 582243.0)
		self.assertEqual(sum(h["total"] for h in inv["summary_by_head"]), inv["folio"]["grand_total"])
		self.assertEqual(sum(h["tax"] for h in inv["summary_by_head"]), inv["tax_summary"][0]["total_tax"])

		# KWD: 2 nights @ 11.375 @ 5% VAT -> raw tax 1.1375, doc-rounded tax 1.138, grand 23.888
		k1 = Doc(name="FC-K1", posting_date="2026-12-01", charge_type="Room", description="Night 1",
		         qty=1, rate=11.375, amount=11.375, gst_rate=5.0)
		k2 = Doc(k1, name="FC-K2", posting_date="2026-12-02", description="Night 2")
		f_kwd = self.add(
			"Folio", "FOL-KWD", property="KWD Hotel", folio_type="Guest",
			status="Open", guest_name="Guest KWD", charges=[k1, k2], payments=[],
		)
		f_kwd.as_dict = lambda: {k: v for k, v in f_kwd.items() if not callable(v)}
		self.folio._recalculate(f_kwd)
		inv_kwd = self.api.folio_invoice("FOL-KWD")
		self.assertEqual(inv_kwd["summary_by_head"][0]["tax"], 1.138)
		self.assertEqual(inv_kwd["summary_by_head"][0]["total"], 23.888)

		# Bridged VAT-exclusive folio (lines gst_amount == 0, doc.tax_total copied from native SI):
		# summary_by_head keeps untaxed folio lines untouched.
		b1 = Doc(name="FC-B1", posting_date="2026-12-01", charge_type="Room", description="Night 1",
		         qty=1, rate=493426.0, amount=493426.0, gst_rate=0.0, gst_amount=0.0, total=493426.0)
		f_bridge = self.add(
			"Folio", "FOL-BRIDGE", property="Test Hotel", folio_type="Guest",
			status="Closed", sales_invoice="ACC-SINV-1", guest_name="Bridged Guest",
			charges=[b1], payments=[], charges_total=493426.0, tax_total=88816.68,
			grand_total=582242.68, payments_total=0.0, balance=582242.68,
		)
		f_bridge.as_dict = lambda: {k: v for k, v in f_bridge.items() if not callable(v)}
		inv_bridge = self.api.folio_invoice("FOL-BRIDGE")
		self.assertEqual(inv_bridge["summary_by_head"][0]["amount"], 493426.0)
		self.assertEqual(inv_bridge["summary_by_head"][0]["tax"], 0.0)
		self.assertEqual(inv_bridge["summary_by_head"][0]["total"], 493426.0)

	def test_summary_by_head_multi_head_ugx_exclusive_inclusive_and_kwd_zero_float_drift(self):
		# 1. Multi-head 18% VAT exclusive in 0-dp UGX (Room + Taxi + Laundry)
		r1 = Doc(name="FC-MH1", posting_date="2026-12-01", charge_type="Room", description="Night 1",
		         qty=1, rate=246713.0, amount=246713.0, gst_rate=18.0)
		r2 = Doc(r1, name="FC-MH2", posting_date="2026-12-02", description="Night 2")
		tx = Doc(name="FC-MH3", posting_date="2026-12-02", charge_type="Taxi", description="Airport transfer",
		         qty=1, rate=45000.0, amount=45000.0, gst_rate=18.0)
		ld = Doc(name="FC-MH4", posting_date="2026-12-02", charge_type="Laundry", description="Pressing",
		         qty=1, rate=15001.0, amount=15001.0, gst_rate=18.0)
		f_multi = self.add(
			"Folio", "FOL-UGX-MULTI", property="Test Hotel", folio_type="Guest",
			status="Open", guest_name="Multi UGX", charges=[r1, r2, tx, ld], payments=[],
		)
		f_multi.as_dict = lambda: {k: v for k, v in f_multi.items() if not callable(v)}
		self.folio._recalculate(f_multi)
		inv_multi = self.api.folio_invoice("FOL-UGX-MULTI")
		by_head = {h["head"]: h for h in inv_multi["summary_by_head"]}
		self.assertEqual(by_head["Room"]["amount"], 493426.0)
		self.assertEqual(by_head["Room"]["tax"], 88817.0)
		self.assertEqual(by_head["Room"]["total"], 582243.0)
		self.assertEqual(by_head["Taxi"]["amount"], 45000.0)
		self.assertEqual(by_head["Taxi"]["tax"], 8100.0)
		self.assertEqual(by_head["Taxi"]["total"], 53100.0)
		self.assertEqual(by_head["Laundry"]["amount"], 15001.0)
		self.assertEqual(by_head["Laundry"]["tax"], 2700.0)
		self.assertEqual(by_head["Laundry"]["total"], 17701.0)
		for h in inv_multi["summary_by_head"]:
			self.assertEqual(h["amount"], round(h["amount"], 0))
			self.assertEqual(h["tax"], round(h["tax"], 0))
			self.assertEqual(h["total"], round(h["total"], 0))
			self.assertEqual(h["amount"] + h["tax"], h["total"])
		self.assertEqual(sum(h["amount"] for h in inv_multi["summary_by_head"]), inv_multi["folio"]["charges_total"])
		self.assertEqual(sum(h["tax"] for h in inv_multi["summary_by_head"]), inv_multi["folio"]["tax_total"])
		self.assertEqual(sum(h["tax"] for h in inv_multi["summary_by_head"]), inv_multi["tax_summary"][0]["total_tax"])
		self.assertEqual(sum(h["total"] for h in inv_multi["summary_by_head"]), inv_multi["folio"]["grand_total"])

		# 2. Multi-head 18% VAT exclusive with minor-unit residual after per-head rounding
		# Raw taxes: 44408.34 + 2700.36 + 1800.36 = 48909.06 -> doc tax 48909; rounded sum 48908 -> +1 to Room
		res1 = Doc(name="FC-R1", posting_date="2026-12-01", charge_type="Room", description="Night 1",
		           qty=1, rate=246713.0, amount=246713.0, gst_rate=18.0)
		res2 = Doc(name="FC-R2", posting_date="2026-12-01", charge_type="Laundry", description="Laundry",
		           qty=1, rate=15002.0, amount=15002.0, gst_rate=18.0)
		res3 = Doc(name="FC-R3", posting_date="2026-12-01", charge_type="Spa", description="Massage",
		           qty=1, rate=10002.0, amount=10002.0, gst_rate=18.0)
		f_res = self.add(
			"Folio", "FOL-UGX-RES", property="Test Hotel", folio_type="Guest",
			status="Open", guest_name="Residual UGX", charges=[res1, res2, res3], payments=[],
		)
		f_res.as_dict = lambda: {k: v for k, v in f_res.items() if not callable(v)}
		self.folio._recalculate(f_res)
		inv_res = self.api.folio_invoice("FOL-UGX-RES")
		by_res = {h["head"]: h for h in inv_res["summary_by_head"]}
		self.assertEqual(by_res["Room"]["tax"], 44409.0)
		self.assertEqual(by_res["Room"]["total"], 291122.0)
		self.assertEqual(by_res["Laundry"]["tax"], 2700.0)
		self.assertEqual(by_res["Spa"]["tax"], 1800.0)
		self.assertEqual(sum(h["tax"] for h in inv_res["summary_by_head"]), inv_res["folio"]["tax_total"])
		self.assertEqual(sum(h["total"] for h in inv_res["summary_by_head"]), inv_res["folio"]["grand_total"])

		# 3. Multi-head 18% VAT-inclusive in 0-dp UGX (100,000 Room + 50,000 F&B)
		vi1 = Doc(name="FC-VI1", posting_date="2026-12-01", charge_type="Room", description="Room VAT incl",
		          qty=1, rate=84745.76, amount=84745.76, gst_rate=18.0)
		vi2 = Doc(name="FC-VI2", posting_date="2026-12-01", charge_type="Food & Beverage", description="Dinner VAT incl",
		          qty=1, rate=42372.88, amount=42372.88, gst_rate=18.0)
		f_vi = self.add(
			"Folio", "FOL-UGX-VI", property="Test Hotel", folio_type="Guest",
			status="Open", guest_name="VAT Incl UGX", charges=[vi1, vi2], payments=[],
		)
		f_vi.as_dict = lambda: {k: v for k, v in f_vi.items() if not callable(v)}
		self.folio._recalculate(f_vi)
		inv_vi = self.api.folio_invoice("FOL-UGX-VI")
		by_vi = {h["head"]: h for h in inv_vi["summary_by_head"]}
		self.assertEqual(by_vi["Room"]["amount"], 84746.0)
		self.assertEqual(by_vi["Room"]["tax"], 15254.0)
		self.assertEqual(by_vi["Room"]["total"], 100000.0)
		self.assertEqual(by_vi["Food & Beverage"]["amount"], 42373.0)
		self.assertEqual(by_vi["Food & Beverage"]["tax"], 7627.0)
		self.assertEqual(by_vi["Food & Beverage"]["total"], 50000.0)
		self.assertEqual(inv_vi["tax_summary"][0]["taxable"], 127119.0)
		self.assertEqual(inv_vi["tax_summary"][0]["total_tax"], 22881.0)
		self.assertEqual(sum(h["amount"] for h in inv_vi["summary_by_head"]), inv_vi["folio"]["charges_total"])
		self.assertEqual(sum(h["tax"] for h in inv_vi["summary_by_head"]), inv_vi["folio"]["tax_total"])
		self.assertEqual(sum(h["total"] for h in inv_vi["summary_by_head"]), inv_vi["folio"]["grand_total"])

		# 4. 0%-tax 10.100 + 20.200 KWD has zero IEEE-754 float drift (30.3, never 30.299999999999997)
		z1 = Doc(name="FC-Z1", posting_date="2026-12-01", charge_type="Room", description="N1",
		         qty=1, rate=10.1, amount=10.1, gst_rate=0.0)
		z2 = Doc(name="FC-Z2", posting_date="2026-12-02", charge_type="Room", description="N2",
		         qty=1, rate=20.2, amount=20.2, gst_rate=0.0)
		f_z = self.add(
			"Folio", "FOL-KWD-ZERO", property="KWD Hotel", folio_type="Guest",
			status="Open", guest_name="Zero Float KWD", charges=[z1, z2], payments=[],
		)
		f_z.as_dict = lambda: {k: v for k, v in f_z.items() if not callable(v)}
		self.folio._recalculate(f_z)
		inv_z = self.api.folio_invoice("FOL-KWD-ZERO")
		self.assertEqual(inv_z["summary_by_head"][0]["amount"], 30.3)
		self.assertEqual(inv_z["summary_by_head"][0]["total"], 30.3)
		self.assertEqual(inv_z["tax_summary"][0]["taxable"], 30.3)
		self.assertEqual(repr(inv_z["summary_by_head"][0]["amount"]), "30.3")
		self.assertEqual(repr(inv_z["tax_summary"][0]["taxable"]), "30.3")


class KwdThreeDecimalLiveGapsTest(QuotePatches, RateMatrixHarness):
	"""R2 live-review findings: KWD 3-dp advances, credit control, night audit no-show,
	folio statement/allocations/credited/accounting-tax, and amount-in-words."""

	modules = (
		"kamra.pricing",
		"kamra.folio",
		"kamra.authz",
		"kamra.ledger",
		"kamra.credit",
		"kamra.night_audit",
		"kamra.api",
	)

	def extra_modules(self):
		return {
			"kamra.localization": _tax_invoice_localization(Decimal("5")),
			"kamra.checkout": stub_module("kamra.checkout", _NoTrace=self._no_trace),
			"kamra.savings": stub_module("kamra.savings", log_action=lambda *a, **kw: self.audit_logs.append((a, kw))),
			"kamra.settings": stub_module(
				"kamra.settings",
				get_setting=lambda prop, key: self.settings_map.get(key),
			),
			"kamra.no_show": stub_module(
				"kamra.no_show",
				policy=lambda prop: self.ns_policy,
				fee_decision=lambda res, pol: self.ns_decision,
				audit_outcome=lambda res, pol, bd, run_at: self.ns_outcome,
			),
		}

	@staticmethod
	@contextmanager
	def _no_trace(name):
		yield

	def setUp(self):
		super().setUp()
		self.roles = ["Finance", "Hotel Admin", "Front Desk", "Night Auditor"]
		self.authz.restricted_properties = lambda user=None: None
		self.audit_logs = []
		self.settings_map = {"credit_control": "block", "credit_overdue_grace_days": 0}
		self.ns_policy = Doc(charge="First Night")
		self.ns_decision = Doc(fee="none", reason="")
		self.ns_outcome = Doc(outcome="no_show", fee="charge", reason="")
		self.add(
			"Property", "KWD Hotel", currency="KWD", property_name="KWD Hotel",
			business_date="2026-11-24", rates_include_tax=0,
			gst_mode="Fixed", gst_rate_low=5, gst_rate_high=5,
			no_show_credit_default="Refund",
		)
		self.add(
			"City Ledger Account", "CLA-KWD-1", property="KWD Hotel",
			company="Kuwait Oil Corp", account_number="AR-KWD-1",
			balance=0.0, credit_limit=20.0, credit_days=30,
			use_default_credit_days=0, disabled=0, restricted=0,
		)

	def test_advance_held_and_apply_advances_preserve_three_decimal_kwd_zero_stranded(self):
		# P1-1: 11.944 KWD advance must move full 11.944 from Deposit to Guest (0.000 stranded),
		# and a 0.004 KWD advance must not be skipped by hardcoded <= 0.005 threshold.
		deposit_balances = {"FP-ADV-1": 11.944, "FP-ADV-2": 0.004}
		folio = self.add(
			"Folio", "FOL-ADV", property="KWD Hotel", folio_type="Guest", status="Open",
			payments=[Doc(name=row, payment_kind="Advance", amount=amount)
			          for row, amount in deposit_balances.items()],
			precision=lambda field: self.ledger._currency_places("KWD Hotel"),
		)

		def fake_sql(query, params=None, as_dict=False):
			if "ledger = 'Deposit'" in query:
				return [("Folio Payment", row, str(amount)) for row, amount in deposit_balances.items()]
			return [(0.0,)]

		writes = []

		def fake_write(**kw):
			writes.append(kw)
			if kw["ledger"] == "Deposit":
				row = kw["source_name"]
				deposit_balances[row] = round(deposit_balances[row] + float(kw["credit"] or 0) - float(kw["debit"] or 0), 6)
			return f"FLE-{len(writes)}"

		self.frappe.db.sql = fake_sql
		with patch.object(self.ledger, "write_ledger_entry", side_effect=fake_write), \
				patch.object(self.ledger, "ensure_default_transaction_codes", lambda *a, **kw: None), \
				patch.object(self.frappe, "get_doc", return_value=folio):
			self.assertEqual(self.ledger._advance_held(folio, "FP-ADV-1"), Decimal("11.944"))
			self.assertEqual(self.ledger._advance_held(folio, "FP-ADV-2"), Decimal("0.004"))
			moved = self.ledger._apply_advances([folio])

		self.assertEqual(moved, 11.948)
		self.assertEqual(deposit_balances["FP-ADV-1"], 0.0)
		self.assertEqual(deposit_balances["FP-ADV-2"], 0.0)
		dep_debits = [w["debit"] for w in writes if w["ledger"] == "Deposit"]
		guest_credits = [w["credit"] for w in writes if w["ledger"] == "Guest"]
		self.assertEqual(dep_debits, [11.944, 0.004])
		self.assertEqual(guest_credits, [11.944, 0.004])

	def test_credit_city_ledger_moves_and_warnings_preserve_three_decimal_kwd(self):
		# P1-2 (credit.py): 23.888 KWD folio returns exact 23.888 (not 23.89);
		# 0.005 KWD folio is not skipped by hardcoded > 0.009; warnings & overrides keep 3-dp.
		self.add("Reservation", "RES-KWD-1", property="KWD Hotel", company="Kuwait Oil Corp", status="Checked In")
		folios = [
			Doc(name="FOL-KWD-23", property="KWD Hotel", reservation="RES-KWD-1",
			    folio_type="Company", group_booking=None, balance=23.888),
			Doc(name="FOL-KWD-SMALL", property="KWD Hotel", reservation="RES-KWD-1",
			    folio_type="Company", group_booking=None, balance=0.005),
		]
		self.frappe.get_all = lambda dt, **kw: folios if dt == "Folio" else []
		self.frappe.db.get_value = (
			lambda dt, name, fieldname="name", as_dict=False, **kw:
			"CLA-KWD-1" if (dt == "City Ledger Account" and isinstance(name, dict))
			else self.get_value(dt, name, fieldname, as_dict=as_dict, **kw)
		)
		with patch.object(self.ledger, "company_payer_of", lambda f: "Kuwait Oil Corp"):
			moves = self.credit._city_ledger_moves("RES-KWD-1")
		self.assertEqual(moves, [
			("FOL-KWD-23", "Kuwait Oil Corp", 23.888),
			("FOL-KWD-SMALL", "Kuwait Oil Corp", 0.005),
		])

		# Single 23.888 KWD folio checkout_credit_checks + over-limit / overdue message formatting
		self.frappe.get_all = lambda dt, **kw: [folios[0]] if dt == "Folio" else []
		with patch.object(self.ledger, "company_payer_of", lambda f: "Kuwait Oil Corp"), \
				patch.object(self.ledger, "_account_position", lambda acc: {"open_items": [], "unapplied_credit": 0.0}), \
				patch.object(self.ledger, "overdue_past_grace", lambda *a, **kw: (5.944, "2026-10-01")), \
				patch.object(self.ledger, "aging_date", lambda prop: "2026-11-24"), \
				patch.object(self.credit, "in_house_exposure", lambda *a, **kw: 0.0), \
				patch.object(self.credit, "_symbol", lambda prop: "KWD "):
			self.frappe.db.sql = lambda *a, **kw: [(0.0,)]
			checks = self.credit.checkout_credit_checks("RES-KWD-1")
			self.assertEqual(len(checks), 1)
			self.assertEqual(checks[0]["amount"], 23.888)
			self.assertEqual(checks[0]["exposure"], 23.888)
			self.assertEqual(checks[0]["over_limit_by"], 3.888)
			joined = " ".join(checks[0]["reasons"])
			self.assertIn("3.888", joined)
			self.assertIn("23.888", joined)
			self.assertIn("5.944", joined)
			self.assertNotIn("23.89", joined)
			self.assertNotIn("5.94 ", joined)

			# Override rationale & after_snapshot keep 23.888 (not 23.89)
			with patch.object(self.ledger, "locked_account_figures", lambda acc: {
				"open_items": [], "unapplied_credit": 0.0, "ar_balance": 0.0,
			}):
				self.credit.enforce_company_credit(
					"Kuwait Oil Corp", "KWD Hotel", 23.888,
					override_reason="Approved by GM", context="check-out",
				)
			_args, kw = self.audit_logs[-1]
			self.assertIn("23.888", kw["rationale"])
			self.assertEqual(kw["after_snapshot"]["amount"], 23.888)

	def test_night_audit_no_show_preview_and_credits_preserve_three_decimal_kwd(self):
		# P1-2 (night_audit.py): no_show_preview, no_show_credits, check_arrivals_pending,
		# check_zero_rate, and folio.no_show_credit preserve 3-dp KWD.
		self.add(
			"Reservation", "RES-NS-1", property="KWD Hotel", guest_name="KWD NS Guest",
			status="Confirmed", check_in_date="2026-11-24", check_out_date="2026-11-26",
			room="KWD-101", room_type="KWD Hotel-DLX",
		)
		self.add("Room", "KWD-101", room_number="101")
		self.add(
			"Folio", "FOL-NS-1", property="KWD Hotel", reservation="RES-NS-1",
			folio_type="Guest", status="Open", balance=-11.944,
		)

		def ns_get_value(dt, name, fieldname="name", as_dict=False, **kw):
			if dt == "Folio" and isinstance(name, dict):
				fdoc = self.docs["Folio", "FOL-NS-1"]
				if isinstance(fieldname, (list, tuple)):
					vals = Doc({k: fdoc.get(k) for k in fieldname})
					return vals if as_dict else tuple(vals.values())
				return fdoc.get(fieldname)
			return self.get_value(dt, name, fieldname, as_dict=as_dict, **kw)

		self.frappe.db.get_value = ns_get_value
		self.frappe.db.sql = lambda query, params=None, as_dict=False: [(11.944,)]

		# 1. no_show_preview with no fee -> paid=11.944, credit_after=11.944
		self.ns_decision = Doc(fee="none", reason="")
		prev = self.night_audit.no_show_preview("RES-NS-1")
		self.assertEqual(prev["paid"], 11.944)
		self.assertEqual(prev["credit_after"], 11.944)

		# 2. no_show_preview with 11.375 + 5% VAT = 11.944 fee -> fee_amount=11.375, fee_total=11.944
		self.ns_decision = Doc(fee="charge", reason="")
		with patch.object(self.folio, "policy_fee", lambda r, basis: 11.375), \
				patch.object(self.folio, "_room_gst", lambda r: 5.0):
			prev_fee = self.night_audit.no_show_preview("RES-NS-1")
		self.assertEqual(prev_fee["fee_amount"], 11.375)
		self.assertEqual(prev_fee["fee_total"], 11.944)
		self.assertEqual(prev_fee["credit_after"], 0.0)

		# 3. folio.no_show_credit keeps 11.944 (not 11.94)
		self.assertEqual(self.folio.no_show_credit("RES-NS-1"), 11.944)

		# 4. no_show_credits keeps 11.944 and uses currency-aware threshold (not -0.01)
		ns_sql_calls = []

		def ns_credits_sql(query, params=None, as_dict=False):
			ns_sql_calls.append((query, params))
			return [Doc(name="RES-NS-1", guest_name="KWD NS Guest", check_in_date="2026-11-24", credit=11.944)]

		self.frappe.db.sql = ns_credits_sql
		credits = self.night_audit.no_show_credits("KWD Hotel")
		self.assertEqual(credits[0]["credit"], 11.944)
		self.assertNotIn("-0.01", ns_sql_calls[0][0])

		# 5. check_arrivals_pending keeps 11.375 fee_amount (not 11.38)
		self.frappe.get_all = lambda dt, **kw: ["RES-NS-1"] if dt == "Reservation" else []
		with patch.object(self.folio, "policy_fee", lambda r, basis: 11.375), \
				patch.object(self.folio, "audit_clock", lambda p: "2026-11-24 23:00:00"), \
				patch.object(self.folio, "locked_night", lambda r, d: None):
			arr = self.night_audit.check_arrivals_pending("KWD Hotel", "2026-11-24")
		self.assertEqual(arr["items"][0]["fee_amount"], 11.375)

		# 6. check_zero_rate does not flag a 0.005 KWD rate as zero-rate (threshold < 0.005)
		with patch.object(self.night_audit, "_expected_night", lambda r, bd: (0.005, "locked nightly rate")):
			zr = self.night_audit.check_zero_rate("KWD Hotel", "2026-11-24")
		self.assertEqual(zr["items"], [])

	def test_folio_invoice_statement_allocations_credited_accounting_and_words_preserve_kwd_3dp(self):
		# P1-2 (api.py & localization/words.py): 23.888 / 11.944 / 5.944 / 3.456 KWD stay exact 3-dp.
		words_spec = importlib.util.spec_from_file_location(
			"kamra_words_under_test", ROOT / "kamra/localization/words.py",
		)
		words_mod = importlib.util.module_from_spec(words_spec)
		words_spec.loader.exec_module(words_mod)
		spell = words_mod.amount_in_words

		self.assertEqual(spell(23.888, "KWD", indian=False), "KWD Twenty Three point 888 Only")
		self.assertEqual(spell(23.005, "KWD", indian=False), "KWD Twenty Three point 005 Only")
		self.assertEqual(spell(23.000, "KWD", indian=False), "KWD Twenty Three Only")

		c1 = Doc(name="FC-K1", posting_date="2026-11-24", charge_type="Room", description="Night 1",
		         qty=1, rate=11.375, amount=11.375, gst_rate=5.0)
		c2 = Doc(c1, name="FC-K2", description="Night 2")
		p1 = Doc(name="FP-K1", posting_date="2026-11-24", payment_kind="Payment", mode="Cash",
		         amount=11.944, currency="KWD", reference="CASH-1")
		folio = self.add(
			"Folio", "FOL-KWD-STMT", property="KWD Hotel", folio_type="Guest",
			status="Open", guest_name="KWD Statement Guest", charges=[c1, c2], payments=[p1],
		)
		folio.as_dict = lambda: {k: v for k, v in folio.items() if not callable(v)}
		self.folio._recalculate(folio)

		self.records["Folio Ledger Entry"] = [
			Doc(folio="FOL-KWD-STMT", source_doctype="City Ledger Entry", source_name="CLE-P1", credit=5.944, debit=0.0),
			Doc(folio="FOL-KWD-STMT", source_doctype="City Ledger Entry", source_name="CLE-P2", credit=0.004, debit=0.0),
		]
		self.records["City Ledger Entry"] = [
			Doc(name="CLE-P1", account="CLA-KWD-1", kind="Payment", posting_date="2026-11-24", reference="KWD-PART-1"),
			Doc(name="CLE-P2", account="CLA-KWD-1", kind="Payment", posting_date="2026-11-24", reference="KWD-TINY-1"),
		]
		loc = sys.modules["kamra.localization"]
		loc.amount_in_words = lambda pack, prop_doc, amt: spell(amt, prop_doc.currency, indian=False)

		with patch.object(self.ledger, "credited_total", lambda f: Decimal("3.456")):
			inv = self.api.folio_invoice("FOL-KWD-STMT")

		stmt = inv["statement"]
		self.assertEqual(stmt[0]["charges"], 11.944)
		self.assertEqual(stmt[0]["tax"], 0.569)
		self.assertEqual(stmt[0]["balance"], 11.944)
		self.assertEqual(stmt[1]["charges"], 11.944)
		self.assertEqual(stmt[1]["tax"], 0.569)
		self.assertEqual(stmt[1]["balance"], 23.888)
		self.assertEqual(stmt[2]["credits"], 11.944)
		self.assertEqual(stmt[2]["balance"], 11.944)

		alloc_by_pay = {a["payment"]: a["amount"] for a in inv["allocations"]}
		self.assertEqual(alloc_by_pay, {"CLE-P1": 5.944, "CLE-P2": 0.004})
		self.assertEqual(inv["credited"], 3.456)
		self.assertEqual(inv["document"]["amount_in_words"], "KWD Twenty Three point 888 Only")

		# _apply_accounting_invoice_taxes & _erp_credited_total preserve 3-dp on a KWD Sales Invoice
		si = Doc(
			doctype="Sales Invoice", name="ACC-SINV-KWD-1", docstatus=1, currency="KWD",
			company="Kuwait Bay", net_total=22.750, total_taxes_and_charges=1.138, grand_total=23.888,
			items=[Doc(name="SII-1", item_code="ROOM", source_ref="FC-K1", net_amount=22.750, amount=22.750)],
			taxes=[Doc(name="SIT-1", charge_type="On Net Total", description="VAT 5%", rate=5.0,
			           tax_amount=1.138, tax_amount_after_discount_amount=1.138, included_in_print_rate=0)],
			item_wise_tax_details=[Doc(item_row="SII-1", tax_row="SIT-1", rate=5.0, amount=1.138, taxable_amount=22.750)],
		)
		ret_si = Doc(
			doctype="Sales Invoice", name="ACC-CN-KWD-1", docstatus=1, is_return=1,
			return_against="ACC-SINV-KWD-1", company="Kuwait Bay", currency="KWD", grand_total=-5.944,
		)
		self.docs["Sales Invoice", "ACC-CN-KWD-1"] = ret_si
		self.records["Sales Invoice"] = [ret_si]
		self.frappe.has_permission = lambda *a, **kw: True
		f_dict = folio.as_dict()
		lines = [{"row": "FC-K1", "charge_type": "Room", "qty": 1, "rate": 22.750, "amount": 22.750,
		          "gst_rate": 0.0, "gst_amount": 0.0, "total": 22.750, "service_code": None}]
		with patch.object(self.api, "_resolve_accounting_invoice", lambda *a, **kw: (si, None, "submitted")):
			acct = self.api._apply_accounting_invoice_taxes(
				folio, self.docs["Property", "KWD Hotel"], "KWD", f_dict, lines, set(), [("VAT", Decimal(1))], "VAT",
			)
		self.assertEqual(f_dict["tax_total"], 1.138)
		self.assertEqual(f_dict["grand_total"], 23.888)
		self.assertEqual(acct["tax_summary"][0]["total_tax"], 1.138)
		self.assertEqual(acct["by_head"]["Room"]["tax"], 1.138)
		self.assertEqual(acct["by_head"]["Room"]["total"], 23.888)
		self.assertEqual(acct["erp_credited"], 5.944)


class KwdRefundCorrectionCheckoutReceivablesTest(QuotePatches, RateMatrixHarness):
	"""R2 review findings: KWD 3-dp, USD 2-dp, and UGX 0-dp precision across
	refunds, payment corrections, folio close/part_settle/checkout, and receivables."""

	modules = (
		"kamra.pricing",
		"kamra.folio",
		"kamra.authz",
		"kamra.ledger",
		"kamra.checkout",
		"kamra.corrections",
		"kamra.receivables",
		"kamra.api",
	)

	def extra_modules(self):
		return {
			"kamra.localization": _tax_invoice_localization(Decimal("5")),
			"kamra.cashier": stub_module(
				"kamra.cashier",
				require_open_session=lambda prop: "SESS-1",
				assert_cashier_mode_allowed=lambda prop, mode: None,
				record_cashier_txn=lambda *a, **kw: None,
			),
			"kamra.masters": stub_module(
				"kamra.masters",
				default_mode_for_tender=lambda prop, tender="Cash": tender,
				is_system_payment_method=lambda mode, prop=None: True,
				payment_tender_type=lambda mode, prop=None: mode,
			),
			"kamra.savings": stub_module("kamra.savings", log_action=lambda *a, **kw: self.audit_logs.append((a, kw))),
			"kamra.webhooks": stub_module("kamra.webhooks", emit_payment=lambda *a, **kw: None),
			"kamra.deposit": stub_module("kamra.deposit", record_collected=lambda *a, **kw: None),
		}

	def setUp(self):
		super().setUp()
		self.roles = ["Finance", "Hotel Admin", "Front Desk"]
		self.authz.restricted_properties = lambda user=None: None
		self.audit_logs = []
		self.utils.get_first_day = lambda d: str(d)[:8] + "01"
		self.add(
			"Property", "KWD Hotel", currency="KWD", property_name="KWD Hotel",
			business_date="2026-11-24", rates_include_tax=0,
			gst_mode="Fixed", gst_rate_low=5, gst_rate_high=5,
		)
		self.add(
			"Property", "USD Hotel", currency="USD", property_name="USD Hotel",
			business_date="2026-11-24", rates_include_tax=0,
			gst_mode="Fixed", gst_rate_low=5, gst_rate_high=5,
		)
		self.docs["Property", "Test Hotel"].update(
			currency="UGX", property_name="Test Hotel",
			business_date="2026-11-24", rates_include_tax=0,
			gst_mode="Fixed", gst_rate_low=0, gst_rate_high=0,
		)
		loc = sys.modules["kamra.localization"]
		loc.FOREIGN_CURRENCY_TENDER_TYPES = ("Cash", "Foreign Cash", "Bank Transfer")
		loc.on_invoice_issued = lambda *a, **kw: None
		loc.assert_payment_mode = lambda *a, **kw: None

		def _money_text(prop, amt):
			cur = self.docs.get(("Property", prop), Doc()).get("currency") or "USD"
			digits = {"KWD": 3, "UGX": 0}.get(cur, 2)
			return f"{cur} {float(amt or 0):,.{digits}f}"

		loc.money_text = _money_text
		self.frappe.model = stub_module("frappe.model")
		self.frappe.model.naming = stub_module(
			"frappe.model.naming", make_autoname=lambda pat: "INV-KWD-26-00001",
		)
		self.sys_modules_patch = patch.dict(
			"sys.modules",
			{"frappe.model": self.frappe.model, "frappe.model.naming": self.frappe.model.naming},
		)
		self.sys_modules_patch.start()
		self.addCleanup(self.sys_modules_patch.stop)

		base_get_doc = self.get_doc
		self.inserted_docs = []

		def _get_doc(doctype, name=None, **kw):
			if isinstance(doctype, dict):
				d = Doc(doctype, name=f"NEW-{len(self.inserted_docs) + 1}")
				d.insert = lambda **ikw: (self.inserted_docs.append(d), d)[1]
				d.save = lambda **skw: d
				self._wire_folio(d)
				return d
			return base_get_doc(doctype, name)

		self.frappe.get_doc = _get_doc
		self.frappe.get_cached_doc = _get_doc
		self.frappe.get_cached_value = self.get_value

		def _set_value(dt, name, field, val=None, **kw):
			doc = self.docs.get((dt, name)) if isinstance(name, str) else None
			if doc is not None:
				if isinstance(field, dict):
					doc.update(field)
				else:
					doc[field] = val

		self.frappe.db.set_value = _set_value

	@staticmethod
	def _wire_folio(doc):
		doc.save = lambda **kw: doc
		doc.reload = lambda: doc
		doc.as_dict = lambda: {k: v for k, v in doc.items() if not callable(v)}
		doc.setdefault("charges", [])
		doc.setdefault("payments", [])

		def _append(field, row):
			d = Doc(row, name=f"{field[:2].upper()}-{len(doc[field]) + 1}")
			d.as_dict = lambda: {k: v for k, v in d.items() if not callable(v)}
			doc[field].append(d)
			return d

		doc.append = _append
		return doc

	def _make_folio(self, name, prop, **fields):
		doc = self.add("Folio", name, property=prop, folio_type="Guest", status="Open", **fields)
		return self._wire_folio(doc)

	def test_p1a_refund_payment_row_residual_full_none_over_refund_and_advance_paid(self):
		# 1. KWD: 11.944 Cash Advance -> refund 11.943 -> remaining 0.001 via payment_row succeeds cleanly
		res1 = self.add("Reservation", "RES-KWD-R1", property="KWD Hotel", status="Confirmed", advance_paid=11.944)
		p1 = Doc(name="FP-ADV-1", posting_date="2026-11-24", payment_kind="Advance", mode="Cash",
		         amount=11.944, currency="KWD")
		f1 = self._make_folio("FOL-KWD-R1", "KWD Hotel", reservation="RES-KWD-R1",
		                      charges=[], payments=[p1], grand_total=0.0, payments_total=11.944, balance=-11.944)
		deposit_held = {"FOL-KWD-R1": 11.944, "FOL-KWD-R2": 11.944, "FOL-USD-R1": 10.0, "FOL-UGX-R1": 50000.0}

		def _refund_sql(query, params=None, as_dict=False):
			if "ledger = 'Deposit'" in query:
				fname = params[0] if isinstance(params, (list, tuple)) else params
				return [(deposit_held.get(fname, 0.0),)]
			return [(0.0,)]

		self.frappe.db.sql = _refund_sql

		def _record_ledger(doc, pay, **kw):
			if kw.get("ledger") == "Deposit":
				deposit_held[doc.name] = round(deposit_held.get(doc.name, 0.0) + float(pay["amount"]), 6)

		with patch.object(self.api, "_pin_guard", lambda *a, **kw: None), \
				patch.object(self.ledger, "require_reason", lambda cat, rc, reason, **kw: reason or rc), \
				patch.object(self.ledger, "record_payment_ledger", side_effect=_record_ledger):
			r_part = self.api.refund_folio_payment(
				"FOL-KWD-R1", amount=11.943, mode="Cash", reason="Partial KWD refund", payment_row="FP-ADV-1",
			)
			self.assertEqual(r_part["balance"], -0.001)
			self.assertEqual(res1.advance_paid, 0.001)

			# Remaining 0.001 KWD via payment_row & amount=None MUST succeed (not "already refunded in full")
			r_rest = self.api.refund_folio_payment(
				"FOL-KWD-R1", amount=None, mode="Cash", reason="Final 1 fil", payment_row="FP-ADV-1",
			)
			self.assertEqual(r_rest["balance"], 0.0)
			self.assertEqual(f1.balance, 0.0)
			self.assertEqual(deposit_held["FOL-KWD-R1"], 0.0)
			self.assertEqual(res1.advance_paid, 0.0)

			# 2. KWD: amount=None on full 11.944 KWD Advance refunds full 11.944 (not 11.94, zero -0.004 stranded)
			res2 = self.add("Reservation", "RES-KWD-R2", property="KWD Hotel", status="Confirmed", advance_paid=11.944)
			p2 = Doc(name="FP-ADV-2", posting_date="2026-11-24", payment_kind="Advance", mode="Cash",
			         amount=11.944, currency="KWD")
			f2 = self._make_folio("FOL-KWD-R2", "KWD Hotel", reservation="RES-KWD-R2",
			                      charges=[], payments=[p2], grand_total=0.0, payments_total=11.944, balance=-11.944)
			r_full = self.api.refund_folio_payment(
				"FOL-KWD-R2", amount=None, mode="Cash", reason="Full KWD advance cancel", payment_row="FP-ADV-2",
			)
			self.assertEqual(r_full["balance"], 0.0)
			self.assertEqual(f2.payments[-1].amount, -11.944)
			self.assertEqual(deposit_held["FOL-KWD-R2"], 0.0)
			self.assertEqual(res2.advance_paid, 0.0)

			# 3. KWD: partial 0.001 advance refund preserves 3-dp Reservation.advance_paid (11.943, not 11.94)
			res3 = self.add("Reservation", "RES-KWD-R3", property="KWD Hotel", status="Confirmed", advance_paid=11.944)
			p3 = Doc(name="FP-ADV-3", posting_date="2026-11-24", payment_kind="Advance", mode="Cash",
			         amount=11.944, currency="KWD")
			deposit_held["FOL-KWD-R3"] = 11.944
			self._make_folio("FOL-KWD-R3", "KWD Hotel", reservation="RES-KWD-R3",
			                 charges=[], payments=[p3], grand_total=0.0, payments_total=11.944, balance=-11.944)
			self.api.refund_folio_payment(
				"FOL-KWD-R3", amount=0.001, mode="Cash", reason="1 fil adjustment", payment_row="FP-ADV-3",
			)
			self.assertEqual(res3.advance_paid, 11.943)

			# 4. KWD: over-refunding 0.004 KWD on a settled 0.000 balance folio MUST be refused
			c_settled = Doc(name="FC-S1", posting_date="2026-11-24", charge_type="Room",
			                qty=1, rate=11.375, amount=11.375, gst_rate=5.0, gst_amount=0.569, total=11.944)
			p_settled = Doc(name="FP-S1", posting_date="2026-11-24", payment_kind="Payment", mode="Cash",
			                amount=11.944, currency="KWD")
			self._make_folio("FOL-KWD-ZERO", "KWD Hotel", charges=[c_settled], payments=[p_settled],
			                 charges_total=11.375, tax_total=0.569, grand_total=11.944,
			                 payments_total=11.944, balance=0.0)
			with self.assertRaises(self.frappe.ValidationError):
				self.api.refund_folio_payment("FOL-KWD-ZERO", amount=0.004, mode="Cash", reason="Zero-bal over-refund")

			# 5. USD 2-dp & UGX 0-dp parity
			res_usd = self.add("Reservation", "RES-USD-R1", property="USD Hotel", status="Confirmed", advance_paid=10.0)
			p_usd = Doc(name="FP-USD-1", posting_date="2026-11-24", payment_kind="Advance", mode="Cash",
			            amount=10.0, currency="USD")
			self._make_folio("FOL-USD-R1", "USD Hotel", reservation="RES-USD-R1",
			                 charges=[], payments=[p_usd], grand_total=0.0, payments_total=10.0, balance=-10.0)
			self.api.refund_folio_payment("FOL-USD-R1", amount=9.99, mode="Cash", reason="Part USD", payment_row="FP-USD-1")
			r_usd = self.api.refund_folio_payment("FOL-USD-R1", amount=None, mode="Cash", reason="Rest USD", payment_row="FP-USD-1")
			self.assertEqual(r_usd["balance"], 0.0)
			self.assertEqual(res_usd.advance_paid, 0.0)

			res_ugx = self.add("Reservation", "RES-UGX-R1", property="Test Hotel", status="Confirmed", advance_paid=50000.0)
			p_ugx = Doc(name="FP-UGX-1", posting_date="2026-11-24", payment_kind="Advance", mode="Cash",
			            amount=50000.0, currency="UGX")
			self._make_folio("FOL-UGX-R1", "Test Hotel", reservation="RES-UGX-R1",
			                 charges=[], payments=[p_ugx], grand_total=0.0, payments_total=50000.0, balance=-50000.0)
			self.api.refund_folio_payment("FOL-UGX-R1", amount=49999.0, mode="Cash", reason="Part UGX", payment_row="FP-UGX-1")
			r_ugx = self.api.refund_folio_payment("FOL-UGX-R1", amount=None, mode="Cash", reason="Rest UGX", payment_row="FP-UGX-1")
			self.assertEqual(r_ugx["balance"], 0.0)
			self.assertEqual(res_ugx.advance_paid, 0.0)

	def test_p1b_payment_correction_options_and_partial_refund_respect_currency_minor_unit(self):
		res_kwd = self.add("Reservation", "RES-CORR-KWD", property="KWD Hotel", status="Confirmed", advance_paid=11.944)
		pay_kwd = Doc(name="FP-CK-1", posting_date="2026-11-24", payment_kind="Advance", mode="Cash",
		              amount=11.944, currency="KWD")
		f_kwd = self._make_folio("FOL-CORR-KWD", "KWD Hotel", reservation="RES-CORR-KWD",
		                         charges=[], payments=[pay_kwd], grand_total=0.0,
		                         payments_total=11.944, balance=-11.944)

		res_usd = self.add("Reservation", "RES-CORR-USD", property="USD Hotel", status="Confirmed", advance_paid=10.0)
		pay_usd = Doc(name="FP-CU-1", posting_date="2026-11-24", payment_kind="Advance", mode="Cash",
		              amount=10.0, currency="USD")
		self._make_folio("FOL-CORR-USD", "USD Hotel", reservation="RES-CORR-USD",
		                 charges=[], payments=[pay_usd], grand_total=0.0,
		                 payments_total=10.0, balance=-10.0)

		res_ugx = self.add("Reservation", "RES-CORR-UGX", property="Test Hotel", status="Confirmed", advance_paid=50000.0)
		pay_ugx = Doc(name="FP-CG-1", posting_date="2026-11-24", payment_kind="Advance", mode="Cash",
		              amount=50000.0, currency="UGX")
		self._make_folio("FOL-CORR-UGX", "Test Hotel", reservation="RES-CORR-UGX",
		                 charges=[], payments=[pay_ugx], grand_total=0.0,
		                 payments_total=50000.0, balance=-50000.0)

		base_get_value = self.get_value

		def _corr_get_value(dt, name, fieldname="name", as_dict=False, **kw):
			if dt == "Folio Ledger Entry":
				return Doc(name="FLE-ORIG-1", session="SESS-1")
			if dt == "Cashier Session":
				return "Open"
			return base_get_value(dt, name, fieldname, as_dict=as_dict, **kw)

		self.frappe.db.get_value = _corr_get_value
		self.frappe.db.exists = lambda dt, flt=None: False if dt == "Night Audit Run" else self.exists(dt, flt)
		reason_row = Doc(name="REF-OVER", requires_supervisor=1)

		with patch.object(self.corrections, "list_reason_codes", lambda cat: [reason_row]), \
				patch.object(self.authz, "supervisors_for", lambda *a, **kw: []), \
				patch.object(self.authz, "require_cashier_pin", lambda *a, **kw: None), \
				patch.object(self.authz, "verify_supervisor", lambda *a, **kw: "sup@example.test"), \
				patch.object(self.api, "_pin_guard", lambda *a, **kw: None), \
				patch.object(self.ledger, "require_reason", lambda cat, rc, reason, **kw: reason or rc), \
				patch.object(self.ledger, "record_payment_ledger", lambda *a, **kw: None):
			# 1. KWD options preserve full 11.944 residual and refund_cap (not 11.94)
			opts = self.corrections.options(f_kwd, "FP-CK-1")
			self.assertEqual(opts["residual"], 11.944)
			self.assertEqual(opts["refund_cap"], 11.944)

			# 2. KWD 0.001 partial_refund succeeds and preserves 3-dp balance & Reservation.advance_paid
			out = self.corrections.correct(
				"FOL-CORR-KWD", "FP-CK-1", "partial_refund", "REF-OVER", "1 fil back",
				"kwd-corr-1", None, "sup@example.test", "8394", 0.001, None,
			)
			self.assertEqual(out["balance"], -11.943)
			self.assertEqual(res_kwd.advance_paid, 11.943)
			opts_after = self.corrections.options(f_kwd, "FP-CK-1")
			self.assertEqual(opts_after["residual"], 11.943)
			self.assertEqual(opts_after["refund_cap"], 11.943)

			# 3. KWD sub-fil 0.0001 is rejected with currency-neutral wording (no "to the cent")
			with self.assertRaises(self.frappe.ValidationError) as cm_kwd:
				self.corrections.correct(
					"FOL-CORR-KWD", "FP-CK-1", "partial_refund", "REF-OVER", "sub-fil",
					"kwd-corr-bad", None, "sup@example.test", "8394", 0.0001, None,
				)
			self.assertNotIn("cent", str(cm_kwd.exception).lower())

			# 4. USD 0.01 succeeds; 0.001 is rejected
			out_usd = self.corrections.correct(
				"FOL-CORR-USD", "FP-CU-1", "partial_refund", "REF-OVER", "1 cent back",
				"usd-corr-1", None, "sup@example.test", "8394", 0.01, None,
			)
			self.assertEqual(out_usd["balance"], -9.99)
			self.assertEqual(res_usd.advance_paid, 9.99)
			with self.assertRaises(self.frappe.ValidationError):
				self.corrections.correct(
					"FOL-CORR-USD", "FP-CU-1", "partial_refund", "REF-OVER", "sub-cent",
					"usd-corr-bad", None, "sup@example.test", "8394", 0.001, None,
				)

			# 5. UGX 1 succeeds; fractional 0.5 is rejected with currency-neutral wording
			out_ugx = self.corrections.correct(
				"FOL-CORR-UGX", "FP-CG-1", "partial_refund", "REF-OVER", "1 UGX back",
				"ugx-corr-1", None, "sup@example.test", "8394", 1, None,
			)
			self.assertEqual(out_ugx["balance"], -49999.0)
			self.assertEqual(res_ugx.advance_paid, 49999.0)
			with self.assertRaises(self.frappe.ValidationError) as cm_ugx:
				self.corrections.correct(
					"FOL-CORR-UGX", "FP-CG-1", "partial_refund", "REF-OVER", "half shilling",
					"ugx-corr-bad", None, "sup@example.test", "8394", 0.5, None,
				)
			self.assertNotIn("cent", str(cm_ugx.exception).lower())

	def test_p1c_folio_close_part_settle_and_checkout_settlement_position_enforce_currency_eps(self):
		# 1. KWD folio with 0.004 KWD unpaid (11.944 charged, 11.940 paid)
		self.add("Reservation", "RES-CHK-KWD", property="KWD Hotel", status="Checked In")
		c_kwd = Doc(name="FC-CK1", posting_date="2026-11-24", charge_type="Room",
		            qty=1, rate=11.375, amount=11.375, gst_rate=5.0)
		p_kwd = Doc(name="FP-CK1", posting_date="2026-11-24", payment_kind="Payment", mode="Cash",
		            amount=11.940, currency="KWD")
		f_kwd = self._make_folio("FOL-CHK-KWD", "KWD Hotel", reservation="RES-CHK-KWD",
		                         charges=[c_kwd], payments=[p_kwd])
		self.folio._recalculate(f_kwd)
		self.assertEqual(f_kwd.balance, 0.004)
		self.records["Folio"] = [f_kwd]

		with patch.object(self.ledger, "company_payer_of", lambda f: None):
			pos = self.checkout.settlement_position("RES-CHK-KWD")
		self.assertEqual(pos["owed"], 0.004)
		self.assertEqual(pos["refund_due"], 0.0)
		self.assertEqual(pos["folios"][0]["balance"], 0.004)

		with self.assertRaisesRegex(self.frappe.ValidationError, "still owed") as cm_close:
			self.folio.close_folio("FOL-CHK-KWD")
		self.assertIn("0.004", str(cm_close.exception))
		self.assertEqual(f_kwd.status, "Open")

		with self.assertRaises(self.frappe.ValidationError):
			self.folio.part_settle("FOL-CHK-KWD")

		# 2. KWD folio with 0.004 KWD overpaid -> refund_due == 0.004 and close_folio refuses
		f_kwd.payments.append(Doc(name="FP-CK2", posting_date="2026-11-24", payment_kind="Payment",
		                          mode="Cash", amount=0.008, currency="KWD"))
		self.folio._recalculate(f_kwd, payments_only=True)
		self.assertEqual(f_kwd.balance, -0.004)
		with patch.object(self.ledger, "company_payer_of", lambda f: None):
			pos_over = self.checkout.settlement_position("RES-CHK-KWD")
		self.assertEqual(pos_over["owed"], 0.0)
		self.assertEqual(pos_over["refund_due"], 0.004)
		with self.assertRaisesRegex(self.frappe.ValidationError, "owed back") as cm_over:
			self.folio.close_folio("FOL-CHK-KWD")
		self.assertIn("0.004", str(cm_over.exception))

		# 3. Exact KWD settlement (balance == 0.000) closes cleanly
		f_kwd.payments.append(Doc(name="FP-CK3", posting_date="2026-11-24", payment_kind="Refund",
		                          mode="Cash", amount=-0.004, currency="KWD"))
		self.folio._recalculate(f_kwd, payments_only=True)
		self.assertEqual(f_kwd.balance, 0.0)
		with patch.object(self.ledger, "company_payer_of", lambda f: None):
			pos_zero = self.checkout.settlement_position("RES-CHK-KWD")
		self.assertEqual((pos_zero["owed"], pos_zero["refund_due"]), (0.0, 0.0))
		inv = self.folio.close_folio("FOL-CHK-KWD")
		self.assertEqual(inv, "INV-KWD-26-00001")
		self.assertEqual(f_kwd.status, "Closed")

		# 4. USD 0.01 unpaid is refused at close_folio and reported by settlement_position
		self.add("Reservation", "RES-CHK-USD", property="USD Hotel", status="Checked In")
		f_usd = self._make_folio("FOL-CHK-USD", "USD Hotel", reservation="RES-CHK-USD",
		                         charges=[Doc(name="FC-U1", amount=10.0, gst_rate=0.0)],
		                         payments=[Doc(name="FP-U1", amount=9.99, mode="Cash", payment_kind="Payment")])
		self.folio._recalculate(f_usd)
		self.assertEqual(f_usd.balance, 0.01)
		self.records["Folio"] = [f_usd]
		with patch.object(self.ledger, "company_payer_of", lambda f: None):
			self.assertEqual(self.checkout.settlement_position("RES-CHK-USD")["owed"], 0.01)
		with self.assertRaisesRegex(self.frappe.ValidationError, "still owed"):
			self.folio.close_folio("FOL-CHK-USD")

		# 5. UGX 1.0 unpaid is refused at close_folio and reported by settlement_position
		self.add("Reservation", "RES-CHK-UGX", property="Test Hotel", status="Checked In")
		f_ugx = self._make_folio("FOL-CHK-UGX", "Test Hotel", reservation="RES-CHK-UGX",
		                         charges=[Doc(name="FC-G1", amount=1000.0, gst_rate=0.0)],
		                         payments=[Doc(name="FP-G1", amount=999.0, mode="Cash", payment_kind="Payment")])
		self.folio._recalculate(f_ugx)
		self.assertEqual(f_ugx.balance, 1.0)
		self.records["Folio"] = [f_ugx]
		with patch.object(self.ledger, "company_payer_of", lambda f: None):
			self.assertEqual(self.checkout.settlement_position("RES-CHK-UGX")["owed"], 1.0)
		with self.assertRaisesRegex(self.frappe.ValidationError, "still owed"):
			self.folio.close_folio("FOL-CHK-UGX")

	def test_p2_receivables_summary_guests_owing_create_and_transfer_preserve_kwd_3dp(self):
		self.add("Corporate Account", "Kuwait Corp", company_name="Kuwait Corp")
		self.add("Corporate Account", "Kuwait Parent", company_name="Kuwait Parent")
		src_acc = self.add(
			"City Ledger Account", "CLA-SRC", property="KWD Hotel", company="Kuwait Corp",
			account_number="AR-KWD-SRC", balance=17.944, credit_limit=100.0, disabled=0, restricted=0,
		)
		dst_acc = self.add(
			"City Ledger Account", "CLA-DST", property="KWD Hotel", company="Kuwait Parent",
			account_number="AR-KWD-DST", balance=0.0, credit_limit=100.0, disabled=0, restricted=0,
		)
		src_acc.save = lambda **kw: src_acc
		dst_acc.save = lambda **kw: dst_acc

		# 1. ar_accounts + _summary + guests_owing preserve 3-dp KWD (5.944 / 11.949 / 0.004)
		guests_sql_calls = []

		def _ar_sql(query, params=None, as_dict=False):
			if "MAX(CONCAT(posting_date, creation))" in query:
				return [Doc(account="CLA-SRC", posting_date="2026-11-24", credit=5.944)]
			if "AS total_debit" in query:
				return [Doc(account="CLA-SRC", total_debit=23.888, total_credit=5.944)]
			if "kind = 'Payment' AND posting_date >=" in query:
				return [(5.944,)]
			if "ledger = 'Deposit'" in query:
				return [(11.949,)]
			if "FROM `tabFolio` f JOIN `tabReservation`" in query:
				guests_sql_calls.append((query, params))
				return [Doc(folio="FOL-OW-1", guest_name="Departed KWD", balance=0.004,
				            reservation="RES-OW-1", status="Checked Out",
				            check_in_date="2026-11-20", check_out_date="2026-11-24")]
			return [(0.0,)]

		self.frappe.db.sql = _ar_sql
		self.records["City Ledger Account"] = [src_acc]
		self.records["Corporate Account"] = [self.docs["Corporate Account", "Kuwait Corp"]]

		fake_credit = stub_module("kamra.credit", in_house_exposure=lambda comp, prop: 11.944)
		with patch.dict(sys.modules, {"kamra.credit": fake_credit}), \
				patch.object(self.ledger, "city_ledger_aging", lambda prop: {
					"accounts": [{
						"name": "CLA-SRC", "company": "Kuwait Corp", "balance": 17.944,
						"overdue": 5.944, "not_due": 12.0,
					}],
				}):
			out = self.receivables.ar_accounts("KWD Hotel")

		acc_row = out["accounts"][0]
		self.assertEqual(acc_row["total_debit"], 23.888)
		self.assertEqual(acc_row["total_credit"], 5.944)
		self.assertEqual(acc_row["last_payment"], 5.944)
		summary = out["summary"]
		self.assertEqual(summary["to_bill"], 11.944)
		self.assertEqual(summary["billed_pending"], 17.944)
		self.assertEqual(summary["overdue"], 5.944)
		self.assertEqual(summary["received_this_month"], 5.944)
		self.assertEqual(summary["deposits_held"], 11.949)
		self.assertEqual(summary["guests_owing"][0]["balance"], 0.004)
		self.assertNotIn("0.005", guests_sql_calls[0][0])
		self.assertIsInstance(guests_sql_calls[0][1], (list, tuple))
		self.assertLessEqual(guests_sql_calls[0][1][1], 0.0005)

		# 2. create_ar_invoice preserves 3-dp KWD line amount (11.375 -> grand_total 11.944, not 11.38)
		created_folios = []

		def _fake_save_folio(f):
			self.docs[("Folio", f.name)] = f
			created_folios.append(f)
			return f

		with patch.object(self.api, "_check_charge_amount_and_tax", lambda *a, **kw: None), \
				patch.object(self.api, "_refuse_opening_balance", lambda *a, **kw: None), \
				patch.object(self.api, "_resolve_charge_gst", lambda prop, ct, desc, alc, rate: rate), \
				patch.object(self.ledger, "code_for_charge_type", lambda ct: "8000"), \
				patch.object(self.ledger, "save_folio", side_effect=_fake_save_folio), \
				patch.object(self.ledger, "transfer_to_city_ledger", lambda fn, comp, **kw: {"balance": 11.944}), \
				patch.object(self.folio, "close_folio", lambda fn: "INV-AR-KWD-1"):
			inv = self.receivables.create_ar_invoice(
				"CLA-SRC",
				[{"charge_type": "Misc", "description": "KWD Hall", "amount": 11.375, "qty": 1, "gst_rate": 5}],
				reference="PO-KWD-1",
			)
		self.assertEqual(created_folios[0].charges[0].amount, 11.375)
		self.assertEqual(inv["total"], 11.944)
		self.assertEqual(inv["tax"], 0.569)

		# 3. transfer_ar_invoice preserves 0.004 KWD outstanding (not rejected by <= 0.005 or rounded to 0.0)
		with patch.object(self.ledger, "locked_account_figures", lambda acc: {
			"open_items": [{
				"folio": "FOL-AR-1", "invoice_number": "INV-AR-KWD-1",
				"invoice_date": "2026-11-20", "outstanding": 0.004,
			}],
		}), \
				patch.object(self.ledger, "due_date_for", lambda acc, dt: "2026-12-20"), \
				patch.object(self.ledger, "apply_unapplied_credit", lambda *a, **kw: None):
			tr = self.receivables.transfer_ar_invoice("CLA-SRC", "FOL-AR-1", "CLA-DST", reason="Parent pays 4 fils")
		self.assertEqual(tr["amount"], 0.004)
		self.assertEqual(tr["from_balance"], 17.94)
		self.assertEqual(tr["to_balance"], 0.004)


class R4HydrogenFollowupPrecisionTest(QuotePatches, RateMatrixHarness):
	"""R4 RED->GREEN tests for Hydrogen R3 blocking findings #1 and #2:

	1. _assert_settled, close_folio, and _checkout_context format currency amounts
	   with exact ISO 4217 minor-unit decimals (INR/USD keep .00 e.g. ₹4,200.00 /
	   $10.00 matching eval_harness t305, UGX omits decimals e.g. UGX 1 never
	   UGX 1.00, KWD uses 3 decimals e.g. د.ك0.004).
	2. api.refund_folio_payment / _refund_folio_payment reject sub-minor inputs
	   (0.0004 KWD, 0.4 UGX, 0.004 USD) BEFORE mutating folio/deposit/advance_paid,
	   reject over-refunds past -doc.balance without +eps leak (0.0044 / 0.005 on
	   -0.004 KWD), normalize IEEE-754 binary float drift (< 1e-9, e.g.
	   11.944 - 11.943), and ledger.payment_rate preserves currency-aware FX
	   precision (11.944 KWD @ 3.25 = 38.82 USD; 0.001 KWD accepted).
	"""

	modules = (
		"kamra.pricing",
		"kamra.folio",
		"kamra.authz",
		"kamra.ledger",
		"kamra.checkout",
		"kamra.corrections",
		"kamra.receivables",
		"kamra.api",
	)

	def extra_modules(self):
		return {
			"kamra.cashier": stub_module(
				"kamra.cashier",
				require_open_session=lambda prop: "SESS-1",
				assert_cashier_mode_allowed=lambda prop, mode: None,
				record_cashier_txn=lambda *a, **kw: None,
				filter_allowed_modes=lambda prop, modes: list(modes),
				till_state=lambda prop: {"open": True, "can_open": True, "who_can_open": []},
			),
			"kamra.masters": stub_module(
				"kamra.masters",
				default_mode_for_tender=lambda prop, tender="Cash": tender,
				is_system_payment_method=lambda mode, prop=None: True,
				payment_tender_type=lambda mode, prop=None: mode,
			),
			"kamra.savings": stub_module("kamra.savings", log_action=lambda *a, **kw: None),
			"kamra.webhooks": stub_module("kamra.webhooks", emit_payment=lambda *a, **kw: None),
			"kamra.deposit": stub_module(
				"kamra.deposit",
				record_collected=lambda *a, **kw: None,
				deposit_state=lambda res: {"held": 0.0, "folio": None},
			),
			"kamra.sop": stub_module("kamra.sop", group_for_charge=lambda ct, desc="": ct),
		}

	def setUp(self):
		super().setUp()
		self.roles = ["Finance", "Hotel Admin", "Front Desk"]
		self.authz.restricted_properties = lambda user=None: None
		self.utils.get_first_day = lambda d: str(d)[:8] + "01"
		self.add(
			"Property", "INR Hotel", currency="INR", country="India", property_name="INR Hotel",
			business_date="2026-11-24", rates_include_tax=0, gst_mode="Fixed", gst_rate_low=0, gst_rate_high=0,
		)
		self.add(
			"Property", "USD Hotel", currency="USD", country="United States", property_name="USD Hotel",
			business_date="2026-11-24", rates_include_tax=0, gst_mode="Fixed", gst_rate_low=5, gst_rate_high=5,
		)
		self.add(
			"Property", "KWD Hotel", currency="KWD", country="Kuwait", property_name="KWD Hotel",
			business_date="2026-11-24", rates_include_tax=0, gst_mode="Fixed", gst_rate_low=5, gst_rate_high=5,
		)
		self.docs["Property", "Test Hotel"].update(
			currency="UGX", country="Uganda", property_name="Test Hotel",
			business_date="2026-11-24", rates_include_tax=0, gst_mode="Fixed", gst_rate_low=0, gst_rate_high=0,
		)

		# Load the real kamra.localization module so group_digits / money_text run unstubbed
		sys.modules.pop("kamra.localization", None)
		self.localization = self.load("kamra.localization")
		symbols = {"INR": "₹", "USD": "$", "UGX": "UGX ", "KWD": "د.ك"}

		def _pack_for(prop):
			return Doc(
				calculate_room_tax=lambda *a, **kw: Decimal("5"),
				locale=lambda pdoc: {
					"currency": pdoc.get("currency") or "USD",
					"currency_symbol": symbols.get(pdoc.get("currency") or "USD", ""),
				},
			)

		self.localization.pack_for = _pack_for
		self.localization.on_invoice_issued = lambda *a, **kw: None
		self.localization.assert_payment_mode = lambda *a, **kw: None
		self.localization.payment_modes = lambda pack, prop=None: ["Cash", "Card", "Bank Transfer"]

		self.frappe.model = stub_module("frappe.model")
		self.frappe.model.naming = stub_module(
			"frappe.model.naming", make_autoname=lambda pat: "INV-R4-26-00001",
		)
		self.sys_modules_patch = patch.dict(
			"sys.modules",
			{"frappe.model": self.frappe.model, "frappe.model.naming": self.frappe.model.naming},
		)
		self.sys_modules_patch.start()
		self.addCleanup(self.sys_modules_patch.stop)

		base_get_doc = self.get_doc
		self.inserted_docs = []

		def _get_doc(doctype, name=None, **kw):
			if isinstance(doctype, dict):
				d = Doc(doctype, name=f"NEW-{len(self.inserted_docs) + 1}")
				d.insert = lambda **ikw: (self.inserted_docs.append(d), d)[1]
				d.save = lambda **skw: d
				self._wire_folio(d)
				return d
			return base_get_doc(doctype, name)

		self.frappe.get_doc = _get_doc
		self.frappe.get_cached_doc = _get_doc
		self.frappe.get_cached_value = self.get_value
		self.frappe.db.after_commit = Doc(_functions=[])
		self.frappe.db.before_commit = Doc(_functions=[])
		self.frappe.db.savepoint = lambda name: None
		self.frappe.db.rollback = lambda **kw: None

		def _set_value(dt, name, field, val=None, **kw):
			doc = self.docs.get((dt, name)) if isinstance(name, str) else None
			if doc is not None:
				if isinstance(field, dict):
					doc.update(field)
				else:
					doc[field] = val

		self.frappe.db.set_value = _set_value

	@staticmethod
	def _wire_folio(doc):
		doc.save = lambda **kw: doc
		doc.reload = lambda: doc
		doc.as_dict = lambda: {k: v for k, v in doc.items() if not callable(v)}
		doc.setdefault("charges", [])
		doc.setdefault("payments", [])

		def _append(field, row):
			d = Doc(row, name=f"{field[:2].upper()}-{len(doc[field]) + 1}")
			d.as_dict = lambda: {k: v for k, v in d.items() if not callable(v)}
			doc[field].append(d)
			return d

		doc.append = _append
		return doc

	def _make_folio(self, name, prop, **fields):
		doc = self.add("Folio", name, property=prop, folio_type="Guest", status="Open", **fields)
		return self._wire_folio(doc)

	def test_r4_p1_settlement_currency_formatting_across_assert_settled_close_and_checkout_context(self):
		cases = [
			("INR Hotel", "INR", 4200.0, "₹4,200.00", "₹4,200 "),
			("USD Hotel", "USD", 10.0, "$10.00", "$10 "),
			("Test Hotel", "UGX", 1.0, "UGX 1", "UGX 1.00"),
			("KWD Hotel", "KWD", 0.004, "د.ك0.004", "د.ك0.00 "),
		]
		for prop, code, owed, expected, forbidden in cases:
			with self.subTest(currency=code, direction="owed"):
				res = self.add(
					"Reservation", f"RES-FMT-{code}", property=prop, status="Checked In",
					guest=f"G-{code}", guest_name=f"Guest {code}",
					check_in_date="2026-11-23", check_out_date="2026-11-24",
					room_type=f"{prop}-STD", room=f"RM-{code}", adults=1, children=0,
				)
				f = self._make_folio(
					f"FOL-FMT-{code}", prop, reservation=res.name,
					charges=[Doc(name=f"FC-{code}", posting_date="2026-11-24", charge_type="Room",
					             description="Night", amount=owed, gst_rate=0.0, gst_amount=0.0, total=owed)],
					payments=[], charges_total=owed, tax_total=0.0, grand_total=owed,
					payments_total=0.0, balance=owed,
				)
				self.records["Folio"] = [f]
				self.records["Folio Charge"] = list(f.charges)

				# 1. _assert_settled preserves exact currency decimals (₹4,200.00 matching t305, UGX 1, د.ك0.004)
				with patch.object(self.folio, "post_remaining_nights", lambda d: None), \
						patch.object(self.ledger, "company_payer_of", lambda row: None):
					with self.assertRaises(self.frappe.ValidationError) as cm_settled:
						self.api._assert_settled(res)
				settled_msg = str(cm_settled.exception)
				self.assertIn(expected, settled_msg)
				self.assertNotIn(forbidden, settled_msg)

				# 2. close_folio matches the exact same currency decimal formatting
				with self.assertRaises(self.frappe.ValidationError) as cm_close:
					self.folio.close_folio(f.name)
				close_msg = str(cm_close.exception)
				self.assertIn(expected, close_msg)
				self.assertNotIn(forbidden, close_msg)

				# 3. _checkout_context blockers match the exact same currency decimal formatting
				with patch.object(self.folio, "post_remaining_nights", lambda d: None), \
						patch.object(self.ledger, "company_payer_of", lambda row: None), \
						patch.object(self.checkout, "_late_checkout", lambda *a, **kw: None):
					ctx = self.checkout._checkout_context(res.name)
				owed_msgs = [b["message"] for b in ctx["blockers"] if b["code"] == "owed"]
				self.assertEqual(owed_msgs, [f"Collect {expected} before departure."])
				self.assertNotIn(forbidden, owed_msgs[0])

			# Also verify guest-credit (refund due) formatting parity
			with self.subTest(currency=code, direction="refund"):
				res_r = self.add(
					"Reservation", f"RES-FMTR-{code}", property=prop, status="Checked In",
					guest=f"GR-{code}", guest_name=f"Guest R {code}",
					check_in_date="2026-11-23", check_out_date="2026-11-24",
					room_type=f"{prop}-STD", room=f"RMR-{code}", adults=1, children=0,
				)
				fr = self._make_folio(
					f"FOL-FMTR-{code}", prop, reservation=res_r.name,
					charges=[], payments=[Doc(name=f"FP-R-{code}", posting_date="2026-11-24",
					                          payment_kind="Payment", mode="Cash", amount=owed, currency=code)],
					charges_total=0.0, tax_total=0.0, grand_total=0.0,
					payments_total=owed, balance=-owed,
				)
				self.records["Folio"] = [fr]
				self.records["Folio Charge"] = []

				with patch.object(self.folio, "post_remaining_nights", lambda d: None), \
						patch.object(self.ledger, "company_payer_of", lambda row: None):
					with self.assertRaises(self.frappe.ValidationError) as cm_rs:
						self.api._assert_settled(res_r)
				self.assertIn(expected, str(cm_rs.exception))
				self.assertNotIn(forbidden, str(cm_rs.exception))

				with self.assertRaises(self.frappe.ValidationError) as cm_rc:
					self.folio.close_folio(fr.name)
				self.assertIn(expected, str(cm_rc.exception))
				self.assertNotIn(forbidden, str(cm_rc.exception))

				with patch.object(self.folio, "post_remaining_nights", lambda d: None), \
						patch.object(self.ledger, "company_payer_of", lambda row: None), \
						patch.object(self.checkout, "_late_checkout", lambda *a, **kw: None):
					ctx_r = self.checkout._checkout_context(res_r.name)
				ref_msgs = [b["message"] for b in ctx_r["blockers"] if b["code"] == "refund"]
				self.assertEqual(ref_msgs, [f"Refund {expected} before departure."])

	def test_r4_p1_refund_rejects_sub_minor_and_over_refund_and_normalizes_float_drift(self):
		deposit_held = {
			"FOL-SUB-KWD": 11.944,
			"FOL-SUB-UGX": 50000.0,
			"FOL-SUB-USD": 10.0,
			"FOL-OVR-KWD": 0.004,
			"FOL-DRIFT-KWD": 0.001,
		}

		def _refund_sql(query, params=None, as_dict=False):
			if "ledger = 'Deposit'" in query:
				fname = params[0] if isinstance(params, (list, tuple)) else params
				return [(deposit_held.get(fname, 0.0),)]
			return [(0.0,)]

		self.frappe.db.sql = _refund_sql

		def _record_ledger(doc, pay, **kw):
			if kw.get("ledger") == "Deposit":
				deposit_held[doc.name] = round(deposit_held.get(doc.name, 0.0) + float(pay["amount"]), 6)

		with patch.object(self.api, "_pin_guard", lambda *a, **kw: None), \
				patch.object(self.ledger, "require_reason", lambda cat, rc, reason, **kw: reason or rc), \
				patch.object(self.ledger, "record_payment_ledger", side_effect=_record_ledger):
			# 1. Sub-minor KWD (0.0004) MUST be rejected before mutating folio/deposit/advance_paid
			res_kwd = self.add("Reservation", "RES-SUB-KWD", property="KWD Hotel", status="Confirmed", advance_paid=11.944)
			f_kwd = self._make_folio(
				"FOL-SUB-KWD", "KWD Hotel", reservation="RES-SUB-KWD",
				charges=[], payments=[Doc(name="FP-SK1", posting_date="2026-11-24", payment_kind="Advance",
				                          mode="Cash", amount=11.944, currency="KWD")],
				grand_total=0.0, payments_total=11.944, balance=-11.944,
			)
			with self.assertRaises(self.frappe.ValidationError):
				self.api.refund_folio_payment("FOL-SUB-KWD", amount=0.0004, mode="Cash", reason="sub-fil")
			self.assertEqual(f_kwd.balance, -11.944)
			self.assertEqual(len(f_kwd.payments), 1)
			self.assertEqual(deposit_held["FOL-SUB-KWD"], 11.944)
			self.assertEqual(res_kwd.advance_paid, 11.944)

			# 2. Sub-minor UGX (0.4) MUST be rejected before mutating folio/advance_paid
			res_ugx = self.add("Reservation", "RES-SUB-UGX", property="Test Hotel", status="Confirmed", advance_paid=50000.0)
			f_ugx = self._make_folio(
				"FOL-SUB-UGX", "Test Hotel", reservation="RES-SUB-UGX",
				charges=[], payments=[Doc(name="FP-SG1", posting_date="2026-11-24", payment_kind="Advance",
				                          mode="Cash", amount=50000.0, currency="UGX")],
				grand_total=0.0, payments_total=50000.0, balance=-50000.0,
			)
			with self.assertRaises(self.frappe.ValidationError):
				self.api.refund_folio_payment("FOL-SUB-UGX", amount=0.4, mode="Cash", reason="sub-shilling")
			self.assertEqual(f_ugx.balance, -50000.0)
			self.assertEqual(len(f_ugx.payments), 1)
			self.assertEqual(res_ugx.advance_paid, 50000.0)

			# 3. Sub-minor USD (0.004) MUST be rejected before mutating folio/advance_paid
			res_usd = self.add("Reservation", "RES-SUB-USD", property="USD Hotel", status="Confirmed", advance_paid=10.0)
			f_usd = self._make_folio(
				"FOL-SUB-USD", "USD Hotel", reservation="RES-SUB-USD",
				charges=[], payments=[Doc(name="FP-SU1", posting_date="2026-11-24", payment_kind="Advance",
				                          mode="Cash", amount=10.0, currency="USD")],
				grand_total=0.0, payments_total=10.0, balance=-10.0,
			)
			with self.assertRaises(self.frappe.ValidationError):
				self.api.refund_folio_payment("FOL-SUB-USD", amount=0.004, mode="Cash", reason="sub-cent")
			self.assertEqual(f_usd.balance, -10.0)
			self.assertEqual(len(f_usd.payments), 1)
			self.assertEqual(res_usd.advance_paid, 10.0)

			# 4. Over-refund 0.0044 (and 0.005) on -0.004 KWD balance MUST refuse, NEVER flip positive
			f_ovr = self._make_folio(
				"FOL-OVR-KWD", "KWD Hotel",
				charges=[Doc(name="FC-OK1", posting_date="2026-11-24", charge_type="Room",
				             amount=11.940, gst_rate=0.0, gst_amount=0.0, total=11.940)],
				payments=[Doc(name="FP-OK1", posting_date="2026-11-24", payment_kind="Payment",
				              mode="Cash", amount=11.944, currency="KWD")],
				charges_total=11.940, tax_total=0.0, grand_total=11.940,
				payments_total=11.944, balance=-0.004,
			)
			for bad_amt in (0.0044, 0.005):
				with self.assertRaises(self.frappe.ValidationError, msg=f"over-refund {bad_amt} must fail"):
					self.api.refund_folio_payment("FOL-OVR-KWD", amount=bad_amt, mode="Cash", reason="over-refund")
				self.assertEqual(f_ovr.balance, -0.004)
				self.assertEqual(len(f_ovr.payments), 1)

			# 5. Binary float drift (11.944 - 11.943 == 0.0010000000000012221) MUST succeed cleanly
			res_drift = self.add("Reservation", "RES-DRIFT-KWD", property="KWD Hotel", status="Confirmed", advance_paid=0.001)
			f_drift = self._make_folio(
				"FOL-DRIFT-KWD", "KWD Hotel", reservation="RES-DRIFT-KWD",
				charges=[],
				payments=[
					Doc(name="FP-DR1", posting_date="2026-11-24", payment_kind="Advance",
					    mode="Cash", amount=11.944, currency="KWD"),
					Doc(name="FP-DR2", posting_date="2026-11-24", payment_kind="Refund",
					    mode="Cash", amount=-11.943, currency="KWD", refund_of="FP-DR1"),
				],
				grand_total=0.0, payments_total=0.001, balance=-0.001,
			)
			drift_amount = 11.944 - 11.943
			self.assertGreater(Decimal(str(drift_amount)), Decimal("0.001"))
			out = self.api.refund_folio_payment(
				"FOL-DRIFT-KWD", amount=drift_amount, mode="Cash", reason="Float-drift final fil",
			)
			self.assertEqual(out["balance"], 0.0)
			self.assertEqual(f_drift.balance, 0.0)
			self.assertEqual(f_drift.payments[-1].amount, -0.001)
			self.assertEqual(deposit_held["FOL-DRIFT-KWD"], 0.0)
			self.assertEqual(res_drift.advance_paid, 0.0)

	def test_r4_p1_payment_rate_and_foreign_kwd_on_usd_hotel_preserve_3dp(self):
		self.records["Exchange Rate"] = [
			Doc(
				property="USD Hotel", currency="KWD", rate_date="2026-11-24",
				buy_rate=3.25, sell_rate=3.25, market_rate=3.25,
				source="Manual", max_exchange=1000.0, disabled=0,
			),
		]
		# 1. 11.944 KWD @ 3.25 USD = 38.818 -> 38.82 USD (NOT 38.81 from round_cents(11.944) == 11.94)
		q = self.ledger.payment_rate("USD Hotel", "KWD", 11.944)
		self.assertEqual(q["home_amount"], 38.82)

		# 2. 0.001 KWD MUST NOT be rejected by HSP-CASHIER-FX-LIMIT
		q_one_fil = self.ledger.payment_rate("USD Hotel", "KWD", 0.001)
		self.assertEqual(q_one_fil["currency"], "KWD")
		self.assertRegex(q_one_fil["quote_id"], r"^[0-9a-f]{64}$")

		# 3. Sub-fil 0.0004 KWD rounds to 0.000 -> rejected by HSP-CASHIER-FX-LIMIT
		with self.assertRaisesRegex(self.frappe.ValidationError, "HSP-CASHIER-FX-LIMIT"):
			self.ledger.payment_rate("USD Hotel", "KWD", 0.0004)

		# 4. add_folio_payment with 11.944 KWD on USD hotel books fx_amount=11.944 & home amount=38.82
		f_fx = self._make_folio(
			"FOL-FX-USD", "USD Hotel",
			charges=[Doc(name="FC-FX1", posting_date="2026-11-24", charge_type="Room",
			             amount=50.0, gst_rate=0.0, gst_amount=0.0, total=50.0)],
			payments=[], charges_total=50.0, tax_total=0.0, grand_total=50.0,
			payments_total=0.0, balance=50.0,
		)
		with patch.object(self.api, "_pin_guard", lambda *a, **kw: None), \
				patch.object(self.ledger, "record_payment_ledger", lambda *a, **kw: None):
			self.api.add_folio_payment("FOL-FX-USD", "Cash", 11.944, currency="KWD")
		pay = f_fx.payments[-1]
		self.assertEqual(pay.currency, "KWD")
		self.assertEqual(pay.fx_amount, 11.944)
		self.assertEqual(pay.amount, 38.82)


class R5HydrogenFollowupTest(QuotePatches, RateMatrixHarness):
	"""R5 RED->GREEN tests for Hydrogen R4 blocking findings #1 (P1) and #4 (P2):

	1. RoomChargeConcurrencyTestCase (test_pos_override_pin_and_room_charge.py)
	   must pass a valid positive base_price to make_room_type because
	   audit_fixtures.make_property writes rate_plan_required=0 and
	   RoomType.validate() enforces Base Price (BAR) > 0 when rate_plan_required=0.
	4. Folio.guard_closed() must compare charge amounts using the property
	   currency's minor-unit precision (at least 3dp for KWD, 2dp for USD/UGX)
	   so +0.001 KWD and +0.003 KWD charge tampering on Closed KWD folios is
	   refused while untampered closed-folio payment saves remain allowed.
	"""

	modules = (
		"kamra.pricing",
		"kamra.folio",
		"kamra.authz",
		"kamra.ledger",
	)

	def extra_modules(self):
		return {
			"kamra.localization": _tax_invoice_localization(Decimal("5")),
			"kamra.settings": stub_module(
				"kamra.settings",
				get_setting=lambda prop, key: 0 if key == "rate_plan_required" else None,
			),
			"kamra.booking_slugs": stub_module(
				"kamra.booking_slugs",
				slugify=lambda s: str(s or "").lower().replace(" ", "-"),
				unique_listing_slug=lambda prop, name, exclude=None: f"{prop}-{name}".lower().replace(" ", "-"),
			),
		}

	def setUp(self):
		super().setUp()
		self.add(
			"Property", "KWD Hotel", currency="KWD", country="Kuwait",
			property_name="KWD Hotel", business_date="2026-11-24", rates_include_tax=0,
		)
		self.add(
			"Property", "USD Hotel", currency="USD", country="United States",
			property_name="USD Hotel", business_date="2026-11-24", rates_include_tax=0,
		)
		self.docs["Property", "Test Hotel"].update(
			currency="UGX", country="Uganda",
			property_name="Test Hotel", business_date="2026-11-24", rates_include_tax=0,
		)
		self.folio_doctype = self.load("kamra.kamra.doctype.folio.folio")
		self.room_type_doctype = self.load("kamra.kamra.doctype.room_type.room_type")

	def test_r5_p1_room_charge_concurrency_fixture_satisfies_room_type_bar_validation(self):
		import ast

		RoomType = self.room_type_doctype.RoomType
		self.frappe.db.exists = lambda *a, **kw: None

		# Confirm #49's RoomType.validate() BAR requirement is intact when rate_plan_required=0
		zero_bar = RoomType(
			doctype="Room Type", name="UT Race-R0", property="Test Hotel",
			room_type_code="R0", room_type_name="UT R0", base_price=0,
		)
		with self.assertRaisesRegex(self.frappe.ValidationError, r"Base Price \(BAR\) is required"):
			zero_bar.validate()

		# Parse RoomChargeConcurrencyTestCase.test_two_checks_racing_under_one_limit_mariadb_snapshot_isolation
		# and validate every make_room_type(..., base_price=...) call against real RoomType.validate()
		src = (ROOT / "kamra/tests/test_pos_override_pin_and_room_charge.py").read_text(encoding="utf-8")
		tree = ast.parse(src)
		race_cls = next(
			n for n in tree.body
			if isinstance(n, ast.ClassDef) and n.name == "RoomChargeConcurrencyTestCase"
		)
		make_rt_calls = [
			node for node in ast.walk(race_cls)
			if isinstance(node, ast.Call)
			and isinstance(node.func, ast.Name)
			and node.func.id == "make_room_type"
		]
		self.assertGreaterEqual(len(make_rt_calls), 1)

		for call_node in make_rt_calls:
			kw = {k.arg: k.value for k in call_node.keywords if k.arg}
			if "base_price" in kw:
				base_price = ast.literal_eval(kw["base_price"])
			elif len(call_node.args) >= 3:
				base_price = ast.literal_eval(call_node.args[2])
			else:
				# audit_fixtures.make_room_type default is 1000
				base_price = 1000
			self.assertGreater(
				base_price, 0,
				f"RoomChargeConcurrencyTestCase make_room_type must use positive base_price, got {base_price}",
			)
			rt_doc = RoomType(
				doctype="Room Type", name="UT Race-R1", property="Test Hotel",
				room_type_code="R1", room_type_name="UT R1", base_price=base_price,
			)
			rt_doc.validate()

	def test_r5_p2_guard_closed_blocks_one_fil_and_three_fil_kwd_charge_tampering(self):
		Folio = self.folio_doctype.Folio
		self.frappe.flags.kamra_invoice_cancel = False

		def _closed_pair(prop: str, inv: str, old_amt: float, new_amt: float, *, extra_payment: bool = False):
			old_charge = Doc(charge_type="Room", posting_date="2026-11-24", amount=old_amt, gst_rate=5.0)
			new_charge = Doc(charge_type="Room", posting_date="2026-11-24", amount=new_amt, gst_rate=5.0)
			old_payments = [Doc(name="FP-1", amount=old_amt, mode="Cash")]
			new_payments = list(old_payments)
			if extra_payment:
				new_payments.append(Doc(name="FP-2", amount=0.001, mode="Cash"))
			old_doc = Doc(
				name="FOL-CLOSED", property=prop, status="Closed", invoice_number=inv,
				charges=[old_charge], payments=old_payments,
			)
			doc = Folio(
				name="FOL-CLOSED", property=prop, status="Closed", invoice_number=inv,
				charges=[new_charge], payments=new_payments, _doc_before_save=old_doc,
			)
			return doc

		# 1. KWD (3dp): +0.001, -0.001, +0.003, +0.004 KWD tampers MUST all be refused
		for tampered in (11.376, 11.374, 11.378, 11.379):
			with self.subTest(currency="KWD", tampered=tampered):
				doc = _closed_pair("KWD Hotel", "INV-KBH-26-00002", 11.375, tampered)
				with self.assertRaisesRegex(self.frappe.ValidationError, "charges are frozen"):
					doc.guard_closed()

		# 2. KWD (3dp): untouched charges (even with IEEE-754 float dust) + settling payment MUST be allowed
		ok_kwd = _closed_pair("KWD Hotel", "INV-KBH-26-00002", 11.375, 11.375 + 1e-12, extra_payment=True)
		ok_kwd.guard_closed()

		# 3. USD (2dp): +0.01 USD tamper MUST be refused; untouched save allowed
		bad_usd = _closed_pair("USD Hotel", "INV-USD-26-00001", 10.00, 10.01)
		with self.assertRaisesRegex(self.frappe.ValidationError, "charges are frozen"):
			bad_usd.guard_closed()
		ok_usd = _closed_pair("USD Hotel", "INV-USD-26-00001", 10.00, 10.00, extra_payment=True)
		ok_usd.guard_closed()

		# 4. UGX (0dp): +1 UGX and +0.40 UGX tampers MUST both be refused; untouched save allowed
		for tampered_ugx in (246714.0, 246713.40):
			with self.subTest(currency="UGX", tampered=tampered_ugx):
				bad_ugx = _closed_pair("Test Hotel", "INV-UGX-26-00001", 246713.0, tampered_ugx)
				with self.assertRaisesRegex(self.frappe.ValidationError, "charges are frozen"):
					bad_ugx.guard_closed()
		ok_ugx = _closed_pair("Test Hotel", "INV-UGX-26-00001", 246713.0, 246713.0, extra_payment=True)
		ok_ugx.guard_closed()

	def test_r6_currency_places_handles_siteless_db_stub_when_cached_doc_lacks_currency(self):
		"""Regression for test_reverse_check_in: siteless frappe.db without callable sql must not crash _currency_places."""
		from kamra import ledger

		old_get_cached_doc = self.frappe.get_cached_doc
		old_get_cached_value = getattr(self.frappe, "get_cached_value", None)
		old_db = self.frappe.db

		class SitelessDb:
			sql = None

			def get_value(self, *a, **k):
				return None

		rt_doc = self.frappe._dict(adults_capacity=2, base_price=100, room_type_name="Deluxe")
		self.frappe.db = SitelessDb()
		self.frappe.get_cached_doc = lambda dt, name: rt_doc
		self.frappe.get_cached_value = lambda *a, **k: self.frappe.db.sql("SELECT 1")
		try:
			self.assertEqual(ledger._currency_places("PROP-1"), 2)
			# Still honours explicit currency on the cached Property doc even on a siteless DB stub
			kwd_doc = self.frappe._dict(name="KWD Hotel", currency="KWD")
			self.frappe.get_cached_doc = lambda dt, name: kwd_doc
			self.assertEqual(ledger._currency_places("KWD Hotel"), 3)
		finally:
			self.frappe.get_cached_doc = old_get_cached_doc
			self.frappe.db = old_db
			if old_get_cached_value is None:
				if hasattr(self.frappe, "get_cached_value"):
					delattr(self.frappe, "get_cached_value")
			else:
				self.frappe.get_cached_value = old_get_cached_value





