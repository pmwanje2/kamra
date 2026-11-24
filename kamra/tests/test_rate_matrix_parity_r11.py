"""Round 11 of the rate matrix: Hydrogen R7 remediation on kamra#33.

P1-2a: a tariff Rate Plan with blank currency takes its linked Price List's
       currency (USD), never the property's currency (UGX), at read time and in
       a post_model_sync migration patch (idempotent, fills only blanks, logs
       a Version row and count).
P1-2b: a derived Rate Plan with blank currency takes its base plan's currency,
       never Frappe's global tabDefaultValue `currency`, on create from REST or
       the PMS screen; explicit mismatched currencies are still refused.
P1-3:  tax-exclusive totals in a zero-decimal currency (e.g. UGX) round at the
       document level by the currency's ISO 4217 minor unit as SAP does (not
       per line, no tenant rounding setting). Booked total, quote, proforma,
       folio and invoice all agree.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from kamra.tests.rate_matrix_harness import Doc, RateMatrixHarness, ValidationError
from kamra.tests.test_rate_matrix_parity_r6 import QuotePatches, stub_module

ROOT = Path(__file__).resolve().parents[2]
PATCHES_TXT = ROOT / "kamra/patches.txt"
PATCH_MODULE = "kamra.patches.v48.backfill_rate_plan_tariff_currency"


class _FrappeDefaultsDoc(Doc):
	"""Simulates Frappe Document._set_defaults() / BaseDocument.update_if_missing()."""

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.dont_update_if_missing = []

	def is_new(self):
		return not bool(self.get("_saved"))

	def update_if_missing(self, defaults: dict):
		for key, value in defaults.items():
			if (
				value is not None
				and self.get(key) is None
				and key not in self.dont_update_if_missing
			):
				self[key] = value

	def _set_defaults(self):
		if self.is_new():
			# Exact replica of Frappe make_new_doc() injecting global tabDefaultValue `currency`
			self.update_if_missing({
				"currency": "INR",
				"fx_lock_policy": "Fixed at Reservation",
				"rounding_mode": "Nearest",
			})


class TariffPlanCurrencyInferenceAndDefaultsTest(RateMatrixHarness):
	"""P1-2: blank tariff plan currency reads Price List currency; derived plans never take Frappe global default."""

	def setUp(self):
		super().setUp()
		self.add("Price List", "Tank Hill CP USD", currency="USD")
		self.add("Price List", "Tank Hill BAR EUR", currency="EUR")
		harness_exists = self.frappe.db.exists
		patcher = patch.object(
			self.frappe.db,
			"exists",
			lambda doctype, name=None: name == "Price List" or harness_exists(doctype, name),
		)
		patcher.start()
		self.addCleanup(patcher.stop)
		sys.modules["frappe.model.document"].Document = _FrappeDefaultsDoc
		self.controller = self.load("kamra.kamra.doctype.rate_plan.rate_plan")

	def make_plan_doc(self, **fields):
		values = {
			"name": "Tank Hill Hotel-CPHALF",
			"property": "Test Hotel",
			"rate_plan_name": "Tank Hill CP Half",
			"code": "CPHALF",
			"modifier_type": "Percent",
			"modifier_value": -50,
			"fx_lock_policy": "Fixed at Check In",
			"rounding_step": 0,
			"rounding_mode": "Nearest",
		}
		return self.controller.RatePlan({**values, **fields})

	def test_tariff_rate_plan_with_blank_currency_takes_price_list_currency_at_read_time(self):
		# Unmigrated @10 USD tariff Rate Plan with blank currency on a UGX property
		cp = self.plan("Tank Hill Hotel-CPUSD", currency=None, custom_tariff_price_list="Tank Hill CP USD")
		self.assertEqual(self.pricing.rate_plan_currency([cp]), "USD")

		# Derived plan over that unmigrated USD tariff plan also resolves USD, never UGX
		half = self.plan("Tank Hill Hotel-CPHALF", currency="", base_rate_plan="Tank Hill Hotel-CPUSD")
		self.assertEqual(self.pricing.rate_plan_currency([cp, half]), "USD")

		# Validating a derived plan against the unbackfilled USD base accepts USD and fills blank to USD
		doc_usd = self.make_plan_doc(base_rate_plan="Tank Hill Hotel-CPUSD", currency="USD")
		doc_usd.validate()
		self.assertEqual(doc_usd.currency, "USD")

		doc_blank = self.make_plan_doc(base_rate_plan="Tank Hill Hotel-CPUSD", currency=None)
		doc_blank.validate()
		self.assertEqual(doc_blank.currency, "USD")

		# Validating the tariff plan itself with blank currency fills USD; explicit UGX is still refused
		cp_doc = self.make_plan_doc(
			name="Tank Hill Hotel-CPUSD",
			rate_plan_name="Tank Hill CP",
			code="CPUSD",
			modifier_value=0,
			currency=None,
			custom_tariff_price_list="Tank Hill CP USD",
		)
		cp_doc.validate()
		self.assertEqual(cp_doc.currency, "USD")

		cp_bad = self.make_plan_doc(
			name="Tank Hill Hotel-CPUSD",
			rate_plan_name="Tank Hill CP",
			code="CPUSD",
			modifier_value=0,
			currency="UGX",
			custom_tariff_price_list="Tank Hill CP USD",
		)
		with self.assertRaisesRegex(ValidationError, "Tank Hill CP USD"):
			cp_bad.validate()

	def test_derived_plan_with_blank_currency_skips_frappe_global_default_and_takes_base_currency(self):
		self.plan("Tank Hill Hotel-CPUSD", currency="USD", custom_tariff_price_list="Tank Hill CP USD")

		# Omitted currency (REST POST / PMS create): _set_defaults must NOT stamp global INR default
		for initial_currency in (None, ""):
			with self.subTest(initial_currency=initial_currency):
				fields = {"base_rate_plan": "Tank Hill Hotel-CPUSD"}
				if initial_currency is not None:
					fields["currency"] = initial_currency
				doc = self.make_plan_doc(**fields)
				doc.pop("fx_lock_policy", None)
				doc._set_defaults()
				self.assertEqual(doc.fx_lock_policy, "Fixed at Reservation")
				self.assertFalse(doc.get("currency"))
				doc.validate()
				self.assertEqual(doc.currency, "USD")

		# Explicit wrong currency is still refused
		bad = self.make_plan_doc(base_rate_plan="Tank Hill Hotel-CPUSD", currency="EUR")
		bad._set_defaults()
		with self.assertRaisesRegex(ValidationError, "base plan's currency"):
			bad.validate()

		# Switching base_rate_plan on an existing derived plan without editing currency updates to new base currency
		self.plan("Tank Hill Hotel-STD", currency="UGX")
		reparented = self.make_plan_doc(
			_saved=True,
			base_rate_plan="Tank Hill Hotel-STD",
			currency="USD",
		)
		reparented.has_value_changed = lambda field: field == "base_rate_plan"
		reparented.validate()
		self.assertEqual(reparented.currency, "UGX")


class BackfillRatePlanTariffCurrencyPatchTest(RateMatrixHarness):
	"""P1-2: post_model_sync migration patch backfills blank tariff plan currencies idempotently with Version/count."""

	def test_patch_is_registered_in_post_model_sync_patches_txt(self):
		text = PATCHES_TXT.read_text(encoding="utf-8")
		post_section = text.split("[post_model_sync]", 1)[1]
		lines = [line.strip() for line in post_section.splitlines() if line.strip() and not line.strip().startswith("#")]
		self.assertIn(PATCH_MODULE, lines)

	def test_patch_fills_only_blanks_logs_version_and_count_and_is_idempotent(self):
		patch_mod = self.load(PATCH_MODULE)

		plans = {
			"RP-BLANK-USD": {"name": "RP-BLANK-USD", "currency": "", "custom_tariff_price_list": "PL-USD"},
			"RP-NULL-EUR": {"name": "RP-NULL-EUR", "currency": None, "custom_tariff_price_list": "PL-EUR"},
			"RP-EXPLICIT": {"name": "RP-EXPLICIT", "currency": "GBP", "custom_tariff_price_list": "PL-USD"},
			"RP-CARD": {"name": "RP-CARD", "currency": None, "custom_tariff_price_list": None},
			"RP-NOLISTCUR": {"name": "RP-NOLISTCUR", "currency": None, "custom_tariff_price_list": "PL-BLANK"},
		}
		price_lists = {"PL-USD": "USD", "PL-EUR": "EUR", "PL-BLANK": ""}
		versions: list[dict] = []
		set_calls: list[tuple] = []
		cleared: list[tuple] = []
		info_logs: list[str] = []

		def sql(query, *args, as_dict=False, **kwargs):
			rows = []
			for rp in plans.values():
				pl = rp.get("custom_tariff_price_list") or ""
				cur = rp.get("currency") or ""
				pl_cur = price_lists.get(pl) or ""
				if not cur and pl and pl_cur:
					rows.append(Doc(name=rp["name"], price_list=pl, list_currency=pl_cur))
			return rows if as_dict else [tuple(r.values()) for r in rows]

		def set_value(doctype, name, field, value, **kwargs):
			set_calls.append((doctype, name, field, value, kwargs))
			plans[name][field] = value

		def get_doc(arg, *args, **kwargs):
			if isinstance(arg, dict) and arg.get("doctype") == "Version":
				doc = Doc(arg)
				doc.insert = lambda **kw: versions.append(dict(doc))
				return doc
			return self.get_doc(arg, *args, **kwargs)

		self.frappe.db = SimpleNamespace(
			table_exists=lambda dt: dt in ("Rate Plan", "Price List", "Version"),
			has_column=lambda dt, col: (dt, col) == ("Rate Plan", "custom_tariff_price_list"),
			sql=sql,
			set_value=set_value,
		)
		self.frappe.get_doc = get_doc
		self.frappe.clear_document_cache = lambda dt, name: cleared.append((dt, name))
		self.frappe.logger = lambda name=None: SimpleNamespace(
			info=lambda msg, *a: info_logs.append(msg % a if a else str(msg))
		)

		first_count = patch_mod.execute()
		self.assertEqual(first_count, 2)
		self.assertEqual(plans["RP-BLANK-USD"]["currency"], "USD")
		self.assertEqual(plans["RP-NULL-EUR"]["currency"], "EUR")
		self.assertEqual(plans["RP-EXPLICIT"]["currency"], "GBP")
		self.assertIsNone(plans["RP-CARD"]["currency"])
		self.assertIsNone(plans["RP-NOLISTCUR"]["currency"])
		self.assertEqual(len(set_calls), 2)
		self.assertEqual(len(cleared), 2)
		self.assertEqual(len(versions), 2)
		version_by_doc = {v["docname"]: json.loads(v["data"]) for v in versions}
		self.assertEqual(version_by_doc["RP-BLANK-USD"]["changed"], [["currency", "", "USD"]])
		self.assertEqual(version_by_doc["RP-NULL-EUR"]["changed"], [["currency", "", "EUR"]])
		self.assertTrue(any("2" in line for line in info_logs), info_logs)

		# Idempotent second run: 0 updates, 0 extra Versions
		second_count = patch_mod.execute()
		self.assertEqual(second_count, 0)
		self.assertEqual(len(set_calls), 2)
		self.assertEqual(len(versions), 2)

	def test_patch_is_a_clean_noop_on_standalone_site_without_price_list_or_column(self):
		patch_mod = self.load(PATCH_MODULE)
		self.frappe.logger = lambda name=None: SimpleNamespace(info=lambda *a: None)
		for case, table_ok, col_ok in (("no_price_list", False, True), ("no_custom_col", True, False)):
			with self.subTest(case):
				self.frappe.db = SimpleNamespace(
					table_exists=lambda dt, ok=table_ok: dt == "Rate Plan" or (ok and dt == "Price List"),
					has_column=lambda dt, col, ok=col_ok: ok,
					sql=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not query")),
				)
				self.assertEqual(patch_mod.execute(), 0)


def _tax_invoice_localization(room_tax_pct: Decimal):
	pack = SimpleNamespace(
		calculate_room_tax=lambda *args: room_tax_pct,
		fnb_tax_rate=lambda *args: float(room_tax_pct),
		invoice_context=lambda prop: {
			"sac": None,
			"place_of_supply": None,
			"tax_label": "VAT",
			"tax_id_label": "TIN",
		},
	)
	return stub_module(
		"kamra.localization",
		pack_for=lambda property: pack,
		default_currency=lambda pack: "UGX",
		tax_split=lambda pack, prop, tax_id: [("vat", Decimal("1"))],
		service_code_for=lambda *args: None,
		invoice_print_block=lambda *args: None,
		invoice_title=lambda pack: "Tax Invoice",
		amount_in_words=lambda *args: "",
		invoice_tax_note=lambda pack: "",
	)


class ZeroDecimalDocumentRoundingTest(QuotePatches, RateMatrixHarness):
	"""P1-3: document-level currency minor-unit rounding (SAP style); quote, booking, proforma, folio, invoice agree."""

	modules = (
		"kamra.pricing",
		"kamra.folio",
		"kamra.kamra.doctype.reservation.reservation",
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
		self.add("Room Type", "Test Hotel-DLX", property="Test Hotel", base_price=400000, base_occupancy=2,
		         adults_capacity=4, free_child_age=6)
		self.plan("Tank Hill Hotel-CPUSD", currency="USD", fx_lock_policy="Fixed at Reservation")
		self.plan("Tank Hill Hotel-CPHALF", base_rate_plan="Tank Hill Hotel-CPUSD", currency="USD",
		          modifier_type="Percent", modifier_value=-50, fx_lock_policy="Fixed at Reservation",
		          rounding_step=0, rounding_mode="Nearest")
		self.patch_quote()
		# Base plan $135 USD at odd FX 3,655.0 -> derived -50% is $67.50 -> 246,712.5 -> 246,713 UGX/night
		self.tariffs["Tank Hill Hotel-CPUSD"] = self.tariff(
			nightly=135.0,
			fx_rate=3655.0,
			fx_date="2026-11-24",
			agreed_rate=493425.0,
		)

	def test_tax_exclusive_ugx_rounds_document_total_not_per_line(self):
		# 2 nights @ 246,713 = 493,426 net; 18% VAT = 88,816.68; raw total = 582,242.68 -> 582,243 (NOT 291,121*2=582,242)
		q2 = self.pricing.quote_with_contract(
			"Test Hotel", "Test Hotel-DLX", "2026-12-01", "2026-12-03", 2, 0,
			rate_plan="Tank Hill Hotel-CPHALF",
		)
		self.assertEqual(q2["agreed_rate"], 246713.0)
		self.assertEqual(q2["room_total"], 493426.0)
		self.assertEqual(q2["amount_before_tax"], 493426.0)
		self.assertEqual(q2["tax_amount"], 88817.0)
		self.assertEqual(q2["amount_after_tax"], 582243.0)
		self.assertEqual(q2["totals"]["tax"], 88817.0)
		self.assertEqual(q2["totals"]["total"], 582243.0)
		self.assertEqual(q2["tax_percent"], 18.0)

		# 3 nights @ 246,713 = 740,139 net; 18% VAT = 133,225.02; raw total = 873,364.02 -> 873,364 (NOT 291,121*3=873,363)
		q3 = self.pricing.quote_with_contract(
			"Test Hotel", "Test Hotel-DLX", "2026-12-01", "2026-12-04", 2, 0,
			rate_plan="Tank Hill Hotel-CPHALF",
		)
		self.assertEqual(q3["amount_before_tax"], 740139.0)
		self.assertEqual(q3["tax_amount"], 133225.0)
		self.assertEqual(q3["amount_after_tax"], 873364.0)

	def test_rounding_uses_each_currency_s_own_iso_decimals(self):
		# USD (2 decimals): 100.33 + 18% (18.0594) = 118.3894 -> 118.39
		self.add("Property", "USD Hotel", currency="USD", business_date="2026-11-24", rates_include_tax=0)
		self.add("Room Type", "USD Hotel-DLX", property="USD Hotel", base_price=100.33, base_occupancy=2,
		         adults_capacity=4)
		q_usd = self.pricing.quote("USD Hotel", "USD Hotel-DLX", "2026-12-01", "2026-12-02", 2, 0)
		self.assertEqual(q_usd["amount_before_tax"], 100.33)
		self.assertEqual(q_usd["tax_amount"], 18.06)
		self.assertEqual(q_usd["amount_after_tax"], 118.39)

		# KWD (3 decimals): 10.125 + 18% (1.8225) = 11.9475 -> 11.948
		self.add("Property", "KWD Hotel", currency="KWD", business_date="2026-11-24", rates_include_tax=0)
		self.add("Room Type", "KWD Hotel-DLX", property="KWD Hotel", base_price=10.125, base_occupancy=2,
		         adults_capacity=4)
		q_kwd = self.pricing.quote("KWD Hotel", "KWD Hotel-DLX", "2026-12-01", "2026-12-02", 2, 0)
		self.assertEqual(q_kwd["amount_before_tax"], 10.125)
		self.assertEqual(q_kwd["tax_amount"], 1.823)
		self.assertEqual(q_kwd["amount_after_tax"], 11.948)

	def test_booked_total_quote_proforma_folio_and_invoice_all_agree(self):
		q = self.pricing.quote_with_contract(
			"Test Hotel", "Test Hotel-DLX", "2026-12-01", "2026-12-03", 2, 0,
			rate_plan="Tank Hill Hotel-CPHALF",
		)
		res = self.reservation.Reservation(
			name="RES-HALF",
			property="Test Hotel",
			room_type="Test Hotel-DLX",
			rate_plan="Tank Hill Hotel-CPHALF",
			check_in_date="2026-12-01",
			check_out_date="2026-12-03",
			adults=2,
			children=0,
			infants=0,
			extra_beds=0,
			auto_price=1,
			agreed_rate=q["agreed_rate"],
			meal_plan=None,
			guest_name="Kato Mugisha",
			status="Checked In",
			nights=2,
			contract_tariff=json.dumps({"agreed_rate": q["agreed_rate"], "tariff": q["tariff"]}),
			flags=Doc(),
		)
		res.is_new = lambda: True
		res.apply_quote(q, "key-1")
		self.docs["Reservation", "RES-HALF"] = res

		# 1. Booked total == Quote
		self.assertEqual(res.amount_after_tax, 582243.0)
		self.assertEqual(res.tax_amount, 88817.0)

		# 2. Mid-stay folio (Night 1 posted) + Proforma (Night 2 projected) == Quote
		night1 = Doc(
			name="FC-1",
			posting_date="2026-12-01",
			charge_type="Room",
			description="Room 109 · DLX",
			qty=1,
			rate=246713.0,
			amount=246713.0,
			gst_rate=18.0,
			auto_posted=1,
			reservation="RES-HALF",
		)
		folio = self.add(
			"Folio",
			"FOL-HALF",
			property="Test Hotel",
			reservation="RES-HALF",
			folio_type="Guest",
			status="Open",
			guest_name="Kato Mugisha",
			charges=[night1],
			payments=[],
		)
		folio.as_dict = lambda: {k: v for k, v in folio.items() if not callable(v)}
		self.folio._recalculate(folio)
		self.assertEqual(folio.grand_total, 291121.0)
		self.assertEqual(folio.tax_total, 44408.0)

		projected_night2 = [{
			"posting_date": "2026-12-02",
			"charge_type": "Room",
			"description": "Room 109 · DLX",
			"qty": 1,
			"rate": 246713.0,
			"amount": 246713.0,
			"gst_rate": 18.0,
			"gst_amount": 44408.34,
			"total": 291121.34,
			"projected": True,
		}]
		harness_get_doc = self.frappe.get_doc

		def get_doc_with_pf(doctype, name=None):
			if isinstance(doctype, dict):
				d = Doc(doctype, name="PF-HALF")
				d.insert = lambda **kw: d
				return d
			return harness_get_doc(doctype, name)

		with patch.object(self.frappe, "get_doc", get_doc_with_pf), \
				patch.object(self.ledger, "_projected_lines", lambda f: projected_night2):
			pf = self.ledger.generate_proforma("FOL-HALF")["invoice"]
		self.assertEqual(pf["estimate"]["grand_total"], 582243.0)
		self.assertEqual(pf["estimate"]["balance"], 582243.0)

		# 3. Full stay posted (Night 1 + Night 2) -> Folio & Invoice == Quote == Booked total
		night2 = Doc(night1, name="FC-2", posting_date="2026-12-02")
		folio.charges.append(night2)
		self.folio._recalculate(folio)
		self.assertEqual(folio.charges_total, 493426.0)
		self.assertEqual(folio.tax_total, 88817.0)
		self.assertEqual(folio.grand_total, 582243.0)
		self.assertEqual(folio.balance, 582243.0)

		# Settle with whole UGX 582,243 -> balance is exact 0.0
		folio.payments.append(Doc(
			name="FP-1", posting_date="2026-12-03", payment_kind="Payment",
			mode="Mobile Money", amount=582243.0, currency="UGX",
		))
		self.folio._recalculate(folio)
		self.assertEqual(folio.balance, 0.0)

		# Invoice agrees on grand_total, tax_total, balance and tax_summary
		inv = self.api.folio_invoice("FOL-HALF")
		self.assertEqual(inv["folio"]["grand_total"], 582243.0)
		self.assertEqual(inv["folio"]["tax_total"], 88817.0)
		self.assertEqual(inv["folio"]["balance"], 0.0)
		self.assertEqual(inv["tax_summary"][0]["total_tax"], 88817.0)
