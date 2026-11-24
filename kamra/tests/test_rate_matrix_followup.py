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
