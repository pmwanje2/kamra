"""Round 12 of the rate matrix: Hydrogen R8 remediation on kamra#33.

P1-1: `_recalculate` must never mutate `charges_total`, `tax_total`, or
      `grand_total` of an already-Closed or invoiced folio when a payment or
      correction is added; rounding applies only to open, uninvoiced folios.
P1-2: `generate_proforma`, `folio_reconciliation`, `reverse_ledger_for_charge`,
      `sync_folio_rounding_ledger`, and `Folio Ledger Entry` `debit`/`credit`
      precision must honor each currency's ISO 4217 decimals (3 decimals for
      KWD: 11.944 x 2 = 23.888), never truncating to 2 decimals.
P1-3: with `tax == 0`, rounding must never mutate `charges_total` away from
      `sum(c.amount)`; unrounded lines on 0%-tax / bridged folios preserve
      `charges_total == sum(c.amount)` and `grand_total == charges + tax` so
      `folio_invoice` summaries and ERPNext bridge `KAM-SI-TOTAL-MISMATCH`
      pass for 0%, VAT-inclusive, and VAT-exclusive cases.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kamra.tests.rate_matrix_harness import Doc, RateMatrixHarness
from kamra.tests.test_rate_matrix_parity_r6 import QuotePatches, stub_module
from kamra.tests.test_rate_matrix_parity_r11 import _tax_invoice_localization

ROOT = Path(__file__).resolve().parents[2]
LEDGER_ENTRY_JSON = ROOT / "kamra/kamra/doctype/folio_ledger_entry/folio_ledger_entry.json"


class ClosedFolioImmutabilityTest(QuotePatches, RateMatrixHarness):
	"""P1-1: Closed / invoiced folios keep frozen charges, tax, and grand_total."""

	modules = (
		"kamra.pricing",
		"kamra.folio",
		"kamra.authz",
		"kamra.ledger",
	)

	def extra_modules(self):
		return {"kamra.localization": _tax_invoice_localization(Decimal("18"))}

	def setUp(self):
		super().setUp()
		self.docs["Property", "Test Hotel"].update(
			currency="UGX",
			property_name="Test Hotel",
			business_date="2026-11-24",
			rates_include_tax=0,
		)

	def _pre_r8_closed_folio(self, **overrides):
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
		base = dict(
			property="Test Hotel",
			folio_type="Guest",
			status="Closed",
			invoice_number="INV-THH-PRE-999",
			charges=[night1, night2],
			payments=[Doc(name="FP-1", amount=500000.0, mode="Cash", payment_kind="Payment")],
			charges_total=493426.0,
			tax_total=88816.68,
			grand_total=582242.68,
			payments_total=500000.0,
			balance=82242.68,
		)
		base.update(overrides)
		return self.add("Folio", "FOL-PRE-999", **base)

	def test_recalculate_preserves_closed_fractional_folio_totals_on_payment(self):
		folio = self._pre_r8_closed_folio()
		folio.payments.append(Doc(name="FP-2", amount=82242.68, mode="Cash", payment_kind="Payment"))

		self.folio._recalculate(folio)

		self.assertEqual(folio.charges_total, 493426.0)
		self.assertEqual(folio.tax_total, 88816.68)
		self.assertEqual(folio.grand_total, 582242.68)
		self.assertEqual(folio.payments_total, 582242.68)
		self.assertEqual(folio.balance, 0.0)

	def test_recalculate_preserves_invoiced_folio_even_if_status_not_closed(self):
		# Bridged folio linked to a native Sales Invoice (or issued invoice_number)
		for field, value in (("sales_invoice", "ACC-SINV-2026-00001"), ("invoice_number", "INV-THH-26-0001")):
			with self.subTest(field=field):
				kwargs = {"status": "Open", "invoice_number": None, field: value}
				folio = self._pre_r8_closed_folio(**kwargs)
				folio.payments.append(Doc(name="FP-2", amount=82242.68, mode="Cash", payment_kind="Payment"))

				self.folio._recalculate(folio)

				self.assertEqual(folio.grand_total, 582242.68)
				self.assertEqual(folio.tax_total, 88816.68)
				self.assertEqual(folio.balance, 0.0)

	def test_sync_folio_rounding_ledger_is_noop_for_closed_or_invoiced_folio(self):
		# Even if a closed/invoiced folio's grand_total differs from sum(c.total), never post rounding entries
		folio = self._pre_r8_closed_folio(grand_total=582243.0)
		writes = []
		self.frappe.db.sql = lambda *a, **kw: [(0.0,)]
		with patch.object(self.ledger, "write_ledger_entry", lambda **kw: writes.append(kw)):
			self.ledger.sync_folio_rounding_ledger(folio)
		self.assertEqual(writes, [])


class ThreeDecimalCurrencyPrecisionTest(QuotePatches, RateMatrixHarness):
	"""P1-2: 3-decimal ISO 4217 currencies (KWD) hold full 3-decimal precision across proforma and ledger."""

	modules = (
		"kamra.pricing",
		"kamra.folio",
		"kamra.authz",
		"kamra.api",
		"kamra.ledger",
	)

	def extra_modules(self):
		return {
			"kamra.localization": _tax_invoice_localization(Decimal("5")),
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
		self.add(
			"Property",
			"KWD Hotel",
			currency="KWD",
			property_name="KWD Hotel",
			business_date="2026-11-24",
			rates_include_tax=0,
		)

	def test_generate_proforma_preserves_three_decimal_kwd_estimate(self):
		# 11.375 KWD + 5% tax (0.56875) = 11.94375 -> Night 1 posted 11.944, 2 nights = 23.888
		night1 = Doc(
			name="FC-KWD-1",
			posting_date="2026-12-01",
			charge_type="Room",
			description="Room 201 · DLX",
			qty=1,
			rate=11.375,
			amount=11.375,
			gst_rate=5.0,
			auto_posted=1,
		)
		folio = self.add(
			"Folio",
			"FOL-KWD",
			property="KWD Hotel",
			folio_type="Guest",
			status="Open",
			guest_name="Amina Al-Sabah",
			charges=[night1],
			payments=[],
		)
		folio.as_dict = lambda: {k: v for k, v in folio.items() if not callable(v)}
		self.folio._recalculate(folio)
		self.assertEqual(folio.grand_total, 11.944)

		projected_night2 = [{
			"posting_date": "2026-12-02",
			"charge_type": "Room",
			"description": "Room 201 · DLX",
			"qty": 1,
			"rate": 11.375,
			"amount": 11.375,
			"gst_rate": 5.0,
			"gst_amount": 0.56875,
			"total": 11.94375,
			"projected": True,
		}]
		harness_get_doc = self.frappe.get_doc

		def get_doc_with_pf(doctype, name=None):
			if isinstance(doctype, dict):
				d = Doc(doctype, name="PF-KWD")
				d.insert = lambda **kw: d
				return d
			return harness_get_doc(doctype, name)

		with patch.object(self.frappe, "get_doc", get_doc_with_pf), \
				patch.object(self.ledger, "_projected_lines", lambda f: projected_night2):
			est = self.ledger.generate_proforma("FOL-KWD", include_projected=1)["invoice"]["estimate"]

		self.assertEqual(est["posted_total"], 11.944)
		self.assertEqual(est["projected_total"], 11.944)
		self.assertEqual(est["grand_total"], 23.888)
		self.assertEqual(est["balance"], 23.888)

	def test_folio_reconciliation_and_reverse_ledger_preserve_three_decimal_kwd(self):
		# 1. folio_reconciliation must preserve 3 decimals on KWD and detect a 0.004 KWD drift
		sql_calls = []

		def fake_sql(query, params=None, as_dict=False):
			sql_calls.append((query, params))
			if "LEFT JOIN" in query:
				# Simulate a 0.004 KWD imbalance (below 0.02, above 0.002)
				return [Doc(folio="FOL-KWD", folio_type="Guest", status="Open", balance=11.944, ledger=11.940)]
			if "tabFolio Ledger Entry" in query:
				return [(11.940,)]
			return [(11.944,)]

		self.frappe.db.sql = fake_sql
		rec = self.ledger.folio_reconciliation("KWD Hotel")
		self.assertFalse(rec["in_balance"])
		self.assertEqual(rec["folios_net"], 11.944)
		self.assertEqual(rec["ledger_net"], 11.94)
		self.assertEqual(rec["difference"], 0.004)
		self.assertEqual(rec["folios"][0]["balance"], 11.944)
		self.assertEqual(rec["folios"][0]["difference"], 0.004)
		# Verify the SQL threshold used for KWD is tighter than 0.02 (<= 0.002)
		join_query, join_params = sql_calls[0]
		self.assertIn("tol", join_params, f"Expected currency-scaled tolerance param in {join_query}")
		self.assertLessEqual(join_params["tol"], 0.002)

		# 2. reverse_ledger_for_charge must not truncate 11.944 -> 11.94 or skip 0.004 KWD
		written = []
		orig = Doc(name="FLE-1", debit=11.944, credit=0, transaction_code="1000", description="Room", ledger="Guest")
		with patch.object(self.ledger, "_original_charge_entry", lambda f, r: orig), \
				patch.object(self.ledger, "write_ledger_entry", lambda **kw: written.append(kw) or "FLE-REV"):
			with patch.object(self.ledger, "_charge_ledger_net", lambda f, r: 11.944):
				self.ledger.reverse_ledger_for_charge("KWD Hotel", "FOL-KWD", "FC-1")
			with patch.object(self.ledger, "_charge_ledger_net", lambda f, r: 0.004):
				self.ledger.reverse_ledger_for_charge("KWD Hotel", "FOL-KWD", "FC-1")
			with patch.object(self.ledger, "_charge_ledger_net", lambda f, r: 11.94375):
				self.ledger.reverse_ledger_for_charge("KWD Hotel", "FOL-KWD", "FC-1")

		self.assertEqual(len(written), 3)
		self.assertEqual(written[0]["credit"], 11.944)
		self.assertEqual(written[1]["credit"], 0.004)
		self.assertEqual(written[2]["credit"], 11.94375)

	def test_sync_folio_rounding_ledger_preserves_sub_fils_kwd_delta(self):
		# KWD Night 1: raw 11.94375 -> rounded 11.944 -> delta = +0.00025 (must NOT round to 0.0003)
		night1 = Doc(name="FC-1", amount=11.375, gst_rate=5.0, gst_amount=0.56875, total=11.94375)
		folio = Doc(name="FOL-KWD", property="KWD Hotel", status="Open", charges=[night1], grand_total=11.944)
		written = []
		self.frappe.db.sql = lambda *a, **kw: [(0.0,)]
		with patch.object(self.ledger, "write_ledger_entry", lambda **kw: written.append(kw)):
			self.ledger.sync_folio_rounding_ledger(folio)
		self.assertEqual(len(written), 1)
		self.assertEqual(written[0]["debit"], 0.00025)

	def test_folio_ledger_entry_debit_credit_precision_holds_three_decimals(self):
		schema = json.loads(LEDGER_ENTRY_JSON.read_text(encoding="utf-8"))
		fields = {f["fieldname"]: f for f in schema["fields"]}
		for fname in ("debit", "credit"):
			prec = fields[fname].get("precision")
			self.assertTrue(prec and int(prec) >= 3, f"Folio Ledger Entry.{fname} precision={prec!r} must be >= 3")


class ZeroTaxChargesTotalAndBridgeAgreementTest(QuotePatches, RateMatrixHarness):
	"""P1-3: tax == 0 never mutates charges_total away from sum(c.amount); bridge KAM-SI-TOTAL-MISMATCH invariant holds."""

	modules = (
		"kamra.pricing",
		"kamra.folio",
		"kamra.authz",
		"kamra.api",
		"kamra.ledger",
	)

	def extra_modules(self):
		return {
			"kamra.localization": _tax_invoice_localization(Decimal("0")),
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
		self.add("Room Type", "Test Hotel-DLX", property="Test Hotel", base_price=1000.40, base_occupancy=2,
		         adults_capacity=4)
		self.patch_quote()

	def test_zero_tax_folio_keeps_charges_total_equal_to_line_sum_and_bridge_invariant(self):
		charge = Doc(
			name="FC-0",
			posting_date="2026-11-24",
			charge_type="Misc",
			description="Laundry",
			qty=1,
			rate=1000.40,
			amount=1000.40,
			gst_rate=0.0,
		)
		folio = self.add(
			"Folio",
			"FOL-ZERO",
			property="Test Hotel",
			folio_type="Guest",
			status="Open",
			guest_name="Kato Mugisha",
			charges=[charge],
			payments=[],
		)
		folio.as_dict = lambda: {k: v for k, v in folio.items() if not callable(v)}

		self.folio._recalculate(folio)

		# Exact line-sum & bridge KAM-SI-TOTAL-MISMATCH invariants
		line_sum = sum(float(c.amount) for c in folio.charges)
		self.assertEqual(folio.charges_total, line_sum)
		self.assertEqual(folio.charges_total, 1000.4)
		self.assertEqual(folio.tax_total, 0.0)
		self.assertEqual(folio.grand_total, 1000.4)
		self.assertEqual(folio.balance, 1000.4)

		inv = self.api.folio_invoice("FOL-ZERO")
		self.assertEqual(inv["summary_by_head"][0]["amount"], folio.charges_total)
		self.assertEqual(inv["tax_summary"][0]["taxable"], folio.charges_total)

	def test_zero_tax_quote_and_proforma_do_not_mutate_unrounded_base(self):
		# 0%-tax stay quote rounds nightly room rate at the line to currency minor unit (UGX 1,000),
		# while folio/proforma still keep explicit charge lines unmutated (charges_total == sum(lines)).
		q = self.pricing.quote("Test Hotel", "Test Hotel-DLX", "2026-12-01", "2026-12-02", 2, 0)
		self.assertEqual(q["amount_before_tax"], 1000.0)
		self.assertEqual(q["tax_amount"], 0.0)
		self.assertEqual(q["amount_after_tax"], 1000.0)

		charge = Doc(
			name="FC-0",
			posting_date="2026-12-01",
			charge_type="Room",
			description="Room 101 · DLX",
			qty=1,
			rate=1000.40,
			amount=1000.40,
			gst_rate=0.0,
		)
		folio = self.add(
			"Folio",
			"FOL-ZERO-PF",
			property="Test Hotel",
			folio_type="Guest",
			status="Open",
			guest_name="Kato Mugisha",
			charges=[charge],
			payments=[],
		)
		folio.as_dict = lambda: {k: v for k, v in folio.items() if not callable(v)}
		self.folio._recalculate(folio)

		projected = [{
			"posting_date": "2026-12-02",
			"charge_type": "Room",
			"description": "Room 101 · DLX",
			"qty": 1,
			"rate": 1000.40,
			"amount": 1000.40,
			"gst_rate": 0.0,
			"gst_amount": 0.0,
			"total": 1000.40,
			"projected": True,
		}]
		harness_get_doc = self.frappe.get_doc

		def get_doc_with_pf(doctype, name=None):
			if isinstance(doctype, dict):
				d = Doc(doctype, name="PF-ZERO")
				d.insert = lambda **kw: d
				return d
			return harness_get_doc(doctype, name)

		with patch.object(self.frappe, "get_doc", get_doc_with_pf), \
				patch.object(self.ledger, "_projected_lines", lambda f: projected):
			est = self.ledger.generate_proforma("FOL-ZERO-PF", include_projected=1)["invoice"]["estimate"]

		self.assertEqual(est["posted_total"], 1000.4)
		self.assertEqual(est["projected_total"], 1000.4)
		self.assertEqual(est["grand_total"], 2000.8)
		self.assertEqual(est["balance"], 2000.8)

	def test_zero_tax_minor_unit_line_rounding_matrix_and_folio_currency_options(self):
		# 1. UGX (prec=0) 1-night & 2-night via base_price=1000.40 -> 1,000 / 2,000
		q1 = self.pricing.quote("Test Hotel", "Test Hotel-DLX", "2026-12-01", "2026-12-02", 2, 0)
		q2 = self.pricing.quote("Test Hotel", "Test Hotel-DLX", "2026-12-01", "2026-12-03", 2, 0)
		self.assertEqual([n["rate"] for n in q1["nightly"]], [1000.0])
		self.assertEqual(q1["amount_after_tax"], 1000.0)
		self.assertEqual([n["rate"] for n in q2["nightly"]], [1000.0, 1000.0])
		self.assertEqual(q2["room_total"], 2000.0)
		self.assertEqual(q2["amount_after_tax"], 2000.0)

		# 2. UGX (prec=0) 1-night & 2-night via agreed_rate=1000.40 -> 1,000 / 2,000
		qa1 = self.pricing.quote(
			"Test Hotel", "Test Hotel-DLX", "2026-12-01", "2026-12-02", 2, 0, agreed_rate=1000.40,
		)
		qa2 = self.pricing.quote(
			"Test Hotel", "Test Hotel-DLX", "2026-12-01", "2026-12-03", 2, 0, agreed_rate=1000.40,
		)
		self.assertEqual([n["rate"] for n in qa1["nightly"]], [1000.0])
		self.assertEqual(qa1["amount_after_tax"], 1000.0)
		self.assertEqual([n["rate"] for n in qa2["nightly"]], [1000.0, 1000.0])
		self.assertEqual(qa2["amount_after_tax"], 2000.0)

		# 3. Derived Rate Plan -15% on base_price=1,001.0 (rounding_step=0) -> 851 (not 850.85)
		self.add(
			"Room Type", "Test Hotel-1001", property="Test Hotel",
			base_price=1001.0, base_occupancy=2, adults_capacity=4,
		)
		self.add(
			"Rate Plan", "Test Hotel-BAR", property="Test Hotel",
			rate_plan_name="BAR", code="BAR", currency="UGX",
			modifier_type="Percent", modifier_value=0, rounding_step=0, rounding_mode="Nearest",
		)
		self.add(
			"Rate Plan", "Test Hotel-D15", property="Test Hotel",
			rate_plan_name="Derived 15", code="D15", currency="UGX",
			base_rate_plan="Test Hotel-BAR",
			modifier_type="Percent", modifier_value=-15, rounding_step=0, rounding_mode="Nearest",
		)
		qd = self.pricing.quote(
			"Test Hotel", "Test Hotel-1001", "2026-12-01", "2026-12-03", 2, 0,
			rate_plan="Test Hotel-D15",
		)
		self.assertEqual([n.get("planned_rate", n["rate"]) for n in qd["nightly"]], [851.0, 851.0])
		self.assertEqual(qd["room_total"], 1702.0)
		self.assertEqual(qd["amount_after_tax"], 1702.0)

		# 4. Rate Plan +150% on base_price=1,000.40 (= 2,501.00 exact integer, step=0) must NOT clobber room_total
		self.add(
			"Rate Plan", "Test Hotel-P150", property="Test Hotel",
			rate_plan_name="Plus 150", code="P150", currency="UGX",
			modifier_type="Percent", modifier_value=150, rounding_step=0, rounding_mode="Nearest",
		)
		qp = self.pricing.quote(
			"Test Hotel", "Test Hotel-DLX", "2026-12-01", "2026-12-02", 2, 0,
			rate_plan="Test Hotel-P150",
		)
		self.assertEqual(qp["room_total"], 2501.0)
		self.assertEqual(qp["amount_after_tax"], 2501.0)

		# 5. Unlocked post_room_night on UGX 0%-tax rounds 1,000.40 -> 1,000 at line
		res = self.add(
			"Reservation", "RES-UNLOCKED-FRAC",
			property="Test Hotel", room_type="Test Hotel-DLX", room="Test Hotel-101",
			check_in_date="2026-12-01", check_out_date="2026-12-02",
			adults=2, children=0, status="Checked In", night_rates=[],
		)
		folio = self.add(
			"Folio", "FOL-UNLOCKED-FRAC", property="Test Hotel", reservation="RES-UNLOCKED-FRAC",
			folio_type="Guest", status="Open", charges=[], payments=[],
		)
		folio.append = lambda f, row: folio[f].append(Doc(row, name=f"FC-{len(folio[f]) + 1}"))
		folio.save = lambda **kw: folio
		with patch.object(self.folio, "_charge_posted", lambda *a, **kw: False), \
				patch.object(self.folio, "current_date_contract_night", lambda *a, **kw: None), \
				patch.object(self.folio, "open_line_target", lambda r, ct, is_alcohol=0: "FOL-UNLOCKED-FRAC"), \
				patch.object(self.folio, "_post_room_levy", lambda *a, **kw: None), \
				patch.object(self.ledger, "save_folio", lambda f, **kw: f):
			self.folio.post_room_night(res, "2026-12-01")
		self.assertEqual(folio.charges[0].amount, 1000.0)
		self.assertEqual(folio.charges_total, 1000.0)
		self.assertEqual(folio.grand_total, 1000.0)

		# 6. USD (2dp) and KWD (3dp) preserve exact ISO 4217 minor units
		self.add("Property", "USD Zero", currency="USD", property_name="USD Zero", business_date="2026-11-24", rates_include_tax=0)
		self.add("Room Type", "USD Zero-STD", property="USD Zero", base_price=100.45, base_occupancy=2, adults_capacity=4)
		self.add("Property", "KWD Zero", currency="KWD", property_name="KWD Zero", business_date="2026-11-24", rates_include_tax=0)
		self.add("Room Type", "KWD Zero-STD", property="KWD Zero", base_price=10.125, base_occupancy=2, adults_capacity=4)
		qu = self.pricing.quote("USD Zero", "USD Zero-STD", "2026-12-01", "2026-12-03", 2, 0)
		self.assertEqual([n["rate"] for n in qu["nightly"]], [100.45, 100.45])
		self.assertEqual(qu["amount_after_tax"], 200.9)
		qk = self.pricing.quote("KWD Zero", "KWD Zero-STD", "2026-12-01", "2026-12-03", 2, 0)
		self.assertEqual([n["rate"] for n in qk["nightly"]], [10.125, 10.125])
		self.assertEqual(qk["amount_after_tax"], 20.25)

		# 7. Non-bridged VAT-inclusive 18% UGX (150,000 gross -> 127,118.64 net) does NOT break
		self.add("Property", "UGX VAT Inc", currency="UGX", property_name="UGX VAT Inc", business_date="2026-11-24", rates_include_tax=1)
		self.add("Room Type", "UGX VAT Inc-DLX", property="UGX VAT Inc", base_price=150000.0, base_occupancy=2, adults_capacity=4)
		with patch.object(self.pricing, "room_gst_rate", lambda *a, **kw: Decimal("18")):
			qinc = self.pricing.quote("UGX VAT Inc", "UGX VAT Inc-DLX", "2026-12-01", "2026-12-02", 2, 0)
		self.assertEqual(round(qinc["nightly"][0]["rate"], 2), 127118.64)
		self.assertEqual(qinc["amount_after_tax"], 150000.0)

		# 8. Folio DocType currency fields declare Property:property:currency so Folio.precision == SI.precision
		folio_json = Path(__file__).resolve().parents[1] / "kamra/doctype/folio/folio.json"
		fields = {f["fieldname"]: f for f in json.loads(folio_json.read_text(encoding="utf-8"))["fields"]}
		for fname in ("charges_total", "tax_total", "grand_total", "payments_total", "balance", "payment_link_amount"):
			self.assertEqual(fields[fname].get("options"), "Property:property:currency", fname)
