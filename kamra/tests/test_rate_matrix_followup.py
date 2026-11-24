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
				record_cashier_txn=lambda *a, **kw: None,
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
				patch.object(self.folio, "open_line_target", lambda r, ct, is_alcohol=0: "FOL-UNLOCKED"), \
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
		self.add("Folio", "FOL-ADV", property="KWD Hotel", folio_type="Guest", status="Open")
		deposit_balances = {"FP-ADV-1": 11.944, "FP-ADV-2": 0.004}

		def fake_sql(query, params=None, as_dict=False):
			if "ledger = 'Deposit'" in query:
				return [(deposit_balances.get(params[1], 0.0),)]
			return [(0.0,)]

		writes = []

		def fake_write(**kw):
			writes.append(kw)
			if kw["ledger"] == "Deposit":
				row = kw["source_name"]
				deposit_balances[row] = round(deposit_balances[row] + float(kw["credit"] or 0) - float(kw["debit"] or 0), 6)
			return f"FLE-{len(writes)}"

		self.frappe.db.sql = fake_sql
		self.frappe.get_all = lambda dt, **kw: ["FP-ADV-1", "FP-ADV-2"] if dt == "Folio Payment" else []

		with patch.object(self.ledger, "write_ledger_entry", side_effect=fake_write), \
				patch.object(self.ledger, "ensure_default_transaction_codes", lambda *a, **kw: None):
			self.assertEqual(self.ledger._advance_held("FOL-ADV", "FP-ADV-1"), 11.944)
			self.assertEqual(self.ledger._advance_held("FOL-ADV", "FP-ADV-2"), 0.004)
			moved = self.ledger._apply_advances([Doc(name="FOL-ADV", property="KWD Hotel")])

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

