"""Multi-currency reservation discounts, FX re-lock, Current Date night audit,
Company-currency guardrail/DoA enforcement, and currency-aware agreed rates
(SAP S/4HANA WAERK -> HWAER + Oracle OPERA Cloud 1:1 — PR 1 KAMRA-MC-DISCOUNT-CORE).

Siteless: runs real Kamra modules (pricing, folio, reservation, api,
channel_manager, localization) under standard `python3 -m unittest` via
RateMatrixHarness.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kamra.tests.rate_matrix_harness import Doc, PermissionDenied, RateMatrixHarness, ValidationError
from kamra.tests.test_rate_matrix_parity_r6 import QuotePatches, stub_module

ROOT = Path(__file__).resolve().parents[2]
RESERVATION_JSON = ROOT / "kamra/kamra/doctype/reservation/reservation.json"
NIGHT_RATE_JSON = ROOT / "kamra/kamra/doctype/reservation_night_rate/reservation_night_rate.json"
LOCALIZATION_PY = ROOT / "kamra/localization/__init__.py"

TANKHILL_FX = 4085.833333
TANKHILL_GROSS_UGX = 612875.0
TANKHILL_DISC_UGX = 204292.0
TANKHILL_NET_UGX = 408583.0


def _fmt_money_stub(property_name, amount, currency=None, *, props=None):
	cur = (currency or (props or {}).get(property_name, "UGX") or "UGX").strip().upper()
	prec = {"UGX": 0, "JPY": 0, "KWD": 3, "BHD": 3}.get(cur, 2)
	val = float(amount or 0)
	sign = "-" if val < 0 else ""
	return f"{sign}{cur} {abs(val):,.{prec}f}"


class MultiCurrencyDiscountHarness(QuotePatches, RateMatrixHarness):
	"""Shared in-memory harness with USD/UGX Tankhill property, KWD/USD and INR setups."""

	modules = (
		"kamra.pricing",
		"kamra.folio",
		"kamra.kamra.doctype.reservation.reservation",
		"kamra.authz",
		"kamra.api",
		"kamra.channel_manager",
	)

	def extra_modules(self):
		self.action_logs: list[dict] = []
		self.verified_supervisors: list[dict] = []
		self.doa_bridge_calls: list[dict] = []
		self.doa_bridge_enabled = False
		self.doa_bridge_error: str | None = None
		self.settings: dict[tuple[str, str], object] = {}

		def _money_text(prop, amount, currency=None):
			home = self.docs[("Property", prop)].currency if ("Property", prop) in self.docs else "UGX"
			return _fmt_money_stub(prop, amount, currency=currency, props={prop: home})

		def _amount_text(prop, amount, currency=None):
			cur = (currency or (self.docs[("Property", prop)].currency if ("Property", prop) in self.docs else "UGX")).upper()
			prec = {"UGX": 0, "JPY": 0, "KWD": 3}.get(cur, 2)
			return f"{float(amount or 0):,.{prec}f}"

		def _pack_for(prop):
			pdoc = self.docs.get(("Property", prop))
			if pdoc and pdoc.get("country") == "India":
				def _india_tax(p, rt, nightly_rate):
					pr = self.docs[("Property", p)]
					thresh = Decimal(str(pr.get("gst_slab_threshold") or 7500))
					low = Decimal(str(pr.get("gst_rate_low") or 5))
					high = Decimal(str(pr.get("gst_rate_high") or 18))
					return low if Decimal(str(nightly_rate)) <= thresh else high
				return SimpleNamespace(calculate_room_tax=_india_tax, DEFAULT_CURRENCY="INR", ROOM_LEVY_LABEL="Room levy")
			return SimpleNamespace(calculate_room_tax=lambda *a: Decimal(str(self.room_tax)), DEFAULT_CURRENCY="UGX",
			                       ROOM_LEVY_LABEL="Local Hotel Tax")

		def _get_setting(prop, key, default=None):
			if (prop, key) in self.settings:
				return self.settings[(prop, key)]
			defaults = {
				"rate_override_limit_pct": 10.0,
				"rate_override_limits_by_role": "",
				"discount_reason_categories": "Rate Change\nAdjust",
				"agreed_rate_reason_categories": "Rate Change",
				"charged_night_rate_change": "Keep charged nights",
				"rate_plan_required": 0,
				"assign_room_on_booking": 1,
				"pin_max_attempts": 5,
				"pin_lock_minutes": 15,
			}
			return defaults.get(key, default)

		def _list_setting(prop, key):
			raw = _get_setting(prop, key, "")
			if isinstance(raw, list):
				return raw
			return [line.strip() for line in str(raw or "").splitlines() if line.strip()]

		def _log_action(action_type, reference_doctype, reference_name, property, **kwargs):
			self.action_logs.append({
				"action_type": action_type,
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"property": property,
				**kwargs,
			})

		return {
			"kamra.localization": stub_module(
				"kamra.localization",
				default_currency=lambda pack: getattr(pack, "DEFAULT_CURRENCY", "USD"),
				pack_for=_pack_for,
				setting_default=lambda prop, key: None,
				money_text=_money_text,
				amount_text=_amount_text,
				_minor_unit_for_currency=lambda cur: {"UGX": 0, "JPY": 0, "KWD": 3}.get((cur or "").upper(), 2),
			),
			"kamra.settings": stub_module(
				"kamra.settings",
				get_setting=_get_setting,
				list_setting=_list_setting,
			),
			"kamra.savings": stub_module("kamra.savings", log_action=_log_action),
			"kamra.credit": stub_module(
				"kamra.credit",
				credit_applies=lambda company: False,
				enforce_company_credit=lambda *a, **kw: None,
				_is_agent=lambda: False,
			),
			"kamra.ledger": stub_module(
				"kamra.ledger",
				ensure_default_reason_codes=lambda: None,
				_currency_places=lambda prop: {"UGX": 0, "JPY": 0, "KWD": 3}.get(
					(self.docs.get(("Property", prop)) or {}).get("currency", "UGX"), 2),
				_money=lambda v: Decimal(str(v or 0)),
				_round_currency=lambda v, places=2: round(float(v), places),
				home_currency=lambda prop: (self.docs.get(("Property", prop)) or {}).get("currency", "UGX"),
				minor_unit=lambda cur: {"UGX": 0, "JPY": 0, "KWD": 3}.get((cur or "").upper(), 2),
				save_folio=lambda *a, **kw: None,
			),
			"kamra.crs": stub_module("kamra.crs", assert_property_access=lambda p: None),
		}

	def setUp(self):
		super().setUp()
		self.patch_quote()
		self.roles = ["Front Desk"]
		self.authz.restricted_properties = lambda user=None: None
		self.authz.supervisors_for = lambda prop, roles=None, exclude=None: [
			{"user": "gm@test", "full_name": "General Manager"},
		]

		def _fake_verify_sup(prop, sup, pin, roles=None, requester=None):
			self.verified_supervisors.append({"property": prop, "supervisor": sup, "pin": pin, "requester": requester})
			if not sup or pin != "4321":
				raise ValidationError("Wrong cashier PIN.")
			return sup

		self.authz.verify_supervisor = _fake_verify_sup
		self.folio.charged_nights = lambda name: {}
		self.folio._fnb_gst = lambda prop: 5.0
		self.folio.voucher_discount = lambda res: 0.0

		# Wire optional siteops_doa.verify_floor_authority bridge hook cleanly
		bridge_mod = sys.modules["kamra.erp_bridge"]
		orig_bridge_call = bridge_mod.bridge_call

		def _get_bridge_script(api_method):
			if api_method == "siteops_doa.verify_floor_authority" and self.doa_bridge_enabled:
				return SimpleNamespace(name="DOA-002", execute_method=lambda: None)
			return None

		def _call_bridge_script(script, form, response_types=None):
			self.doa_bridge_calls.append(dict(form))
			if self.doa_bridge_error:
				raise PermissionDenied(self.doa_bridge_error)
			return {"ok": 1, "approver": form.get("approver"), "authority_event": "HAE-1"}

		bridge_mod.get_bridge_script = _get_bridge_script
		bridge_mod.call_bridge_script = _call_bridge_script
		bridge_mod.bridge_call = orig_bridge_call

		# Tankhill UGX property + Room Type + Reason Codes + USD Rate Plan
		self.add("Property", "Tankhill", currency="UGX", company="Tankhill Ltd",
		         business_date="2026-12-01", rates_include_tax=0)
		self.add("Room Type", "Tankhill-STD", property="Tankhill", base_price=300000,
		         base_occupancy=2, adults_capacity=4, children_capacity=2, free_child_age=6)
		self.plan("BB-USD", property="Tankhill", rate_plan_name="Bed & Breakfast (CP)",
		          currency="USD", discount_eligible=1, fx_lock_policy="Fixed at Reservation")
		self.plan("BB-USD-CHK", property="Tankhill", rate_plan_name="BB Check-In FX",
		          currency="USD", discount_eligible=1, fx_lock_policy="Fixed at Check In")
		self.plan("BB-USD-CUR", property="Tankhill", rate_plan_name="BB Current FX",
		          currency="USD", discount_eligible=1, fx_lock_policy="Current Date")
		self.plan("DOM-UGX", property="Tankhill", rate_plan_name="Domestic UGX",
		          currency="UGX", discount_eligible=1, fx_lock_policy="Fixed at Reservation")
		self.add("Reason Code", "RATE-PROMO", code="RATE-PROMO", category="Rate Change",
		         description="Promotional rate change", requires_supervisor=0, disabled=0)
		self.add("Reason Code", "ADJ-RATE", code="ADJ-RATE", category="Adjust",
		         description="Rate correction", requires_supervisor=0, disabled=0)
		self.tariffs["BB-USD"] = self.tariff(
			currency="USD", nightly=150.0, property_currency="UGX",
			fx_rate=TANKHILL_FX, fx_date="2026-11-24", agreed_rate=TANKHILL_GROSS_UGX,
		)
		self.tariffs["BB-USD-CHK"] = self.tariffs["BB-USD"]
		self.tariffs["BB-USD-CUR"] = self.tariffs["BB-USD"]

	def make_res(self, name="RES-MC-1", *, is_new=False, **fields):
		snap = {
			"agreed_rate": TANKHILL_GROSS_UGX,
			"company": None,
			"tariff": {
				"currency": "USD",
				"nightly": 150.0,
				"property_currency": "UGX",
				"fx_rate": TANKHILL_FX,
				"fx_date": "2026-11-24",
			},
		}
		values = dict(
			name=name, property="Tankhill", room_type="Tankhill-STD", rate_plan="BB-USD",
			check_in_date="2026-12-01", check_out_date="2026-12-02", adults=2, children=0,
			infants=0, extra_beds=0, occupants=[], auto_price=1, agreed_rate=TANKHILL_GROSS_UGX,
			meal_plan=None, company=None, group_booking=None, status="Confirmed",
			contract_tariff=json.dumps(snap),
			amount_before_tax=TANKHILL_GROSS_UGX, tax_amount=0.0, amount_after_tax=TANKHILL_GROSS_UGX,
			discount_type=None, discount_value=0.0, discount_currency=None, discount_reason=None,
			fixed_rate=0, voucher=None,
			night_rates=[Doc(
				stay_date="2026-12-01", rate=TANKHILL_GROSS_UGX, base_rate=TANKHILL_GROSS_UGX,
				quoted_base_rate=TANKHILL_GROSS_UGX, rate_code_amount=TANKHILL_GROSS_UGX,
				gst_rate=0.0, meal_amount=0.0, demand_premium_pct=0.0, hand_price=0.0,
				discount_type=None, discount_value=0.0, discount_currency=None,
				discount_amount=0.0, discount_reason=None, fixed_rate=0,
			)],
		)
		values.update(fields)
		doc = self.reservation.Reservation({**values, "flags": Doc()})
		if not is_new and not doc.get("rate_lock_key"):
			doc.rate_lock_key = doc.rate_lock_key_for(doc.quote_inputs())
		before = self.reservation.Reservation({**dict(doc), "flags": Doc()})
		before.is_new = lambda: False
		doc.is_new = lambda: is_new
		doc.get_doc_before_save = lambda: None if is_new else before
		doc.has_value_changed = lambda field: doc.get(field) != before.get(field)

		def _save(ignore_permissions=False):
			doc.capture_contract_tariff()
			doc.guard_agreed_rate()
			doc.guard_discount_inputs()
			doc.apply_pricing()
			doc.night_rates = [Doc(r) if isinstance(r, dict) else r for r in (doc.night_rates or [])]
			before.update(dict(doc))

		def _append(field, row):
			entry = Doc(row)
			doc.setdefault(field, []).append(entry)
			return entry

		doc.save = _save
		doc.append = _append
		self.docs[("Reservation", name)] = doc
		return doc


class MultiCurrencyDiscountsCoreTests(MultiCurrencyDiscountHarness):
	"""PR 1 Backend RED -> GREEN tests covering WAERK -> HWAER discount math & invariants."""

	def test_usd_amount_discount_on_ugx_property_reduces_usd_and_converts_via_fx(self):
		# Exact Tankhill live production scenario: USD 150/night - USD 50/night = USD 100/night @ 4085.833333
		q = self.pricing.quote_with_contract(
			"Tankhill", "Tankhill-STD", "2026-12-01", "2026-12-02",
			rate_plan="BB-USD", discount_type="Amount", discount_value=50, discount_currency="USD",
		)
		self.assertEqual(q["property_currency"], "UGX")
		self.assertEqual(q["rate_currency"], "USD")
		self.assertEqual(q["discount_currency"], "USD")
		cs = q["contract_summary"]
		self.assertEqual(cs["currency"], "USD")
		self.assertEqual(cs["room_base_total"], 150.0)
		self.assertEqual(cs["room_discount_total"], 50.0)
		self.assertEqual(cs["room_net_total"], 100.0)
		self.assertEqual(cs["room_after_tax_total"], 100.0)
		self.assertFalse(cs["has_home_currency_extras"])
		self.assertEqual(q["room_base_total"], TANKHILL_GROSS_UGX)
		self.assertEqual(q["reservation_discount_total"], TANKHILL_DISC_UGX)
		self.assertEqual(q["room_total"], TANKHILL_NET_UGX)
		self.assertEqual(q["amount_after_tax"], TANKHILL_NET_UGX)
		night = q["nightly"][0]
		self.assertEqual(night["discount_currency"], "USD")
		self.assertEqual(night["contract_currency"], "USD")
		self.assertEqual(night["contract_base_rate"], 150.0)
		self.assertEqual(night["contract_discount_amount"], 50.0)
		self.assertEqual(night["contract_rate"], 100.0)
		self.assertEqual(night["discount_amount"], TANKHILL_DISC_UGX)
		self.assertEqual(night["rate"], TANKHILL_NET_UGX)

		# Omitting discount_currency on a new foreign-rate API quote defaults cleanly to rate_currency (USD)
		q_default = self.pricing.quote_with_contract(
			"Tankhill", "Tankhill-STD", "2026-12-01", "2026-12-02",
			rate_plan="BB-USD", discount_type="Amount", discount_value=50,
		)
		self.assertEqual(q_default["discount_currency"], "USD")
		self.assertEqual(q_default["room_total"], TANKHILL_NET_UGX)
		self.assertEqual(q_default["contract_summary"]["room_after_tax_total"], 100.0)

		# Explicit home-currency (UGX) flat discount subtracts in UGX cleanly
		q_ugx = self.pricing.quote_with_contract(
			"Tankhill", "Tankhill-STD", "2026-12-01", "2026-12-02",
			rate_plan="BB-USD", discount_type="Amount", discount_value=50000, discount_currency="UGX",
		)
		self.assertEqual(q_ugx["discount_currency"], "UGX")
		self.assertEqual(q_ugx["reservation_discount_total"], 50000.0)
		self.assertEqual(q_ugx["room_total"], 562875.0)

		# Unrelated third currency (JPY) on a USD/UGX stay is refused cleanly
		with self.assertRaises(ValidationError):
			self.pricing.quote_with_contract(
				"Tankhill", "Tankhill-STD", "2026-12-01", "2026-12-02",
				rate_plan="BB-USD", discount_type="Amount", discount_value=50, discount_currency="JPY",
			)

		# Ceiling check in USD rejects discount > USD 150.00
		with self.assertRaisesRegex(ValidationError, "USD"):
			self.pricing.quote_with_contract(
				"Tankhill", "Tankhill-STD", "2026-12-01", "2026-12-02",
				rate_plan="BB-USD", discount_type="Amount", discount_value=151, discount_currency="USD",
			)

	def test_kwd_3dp_and_ugx_0dp_amount_discounts_respect_rate_currency_minor_units(self):
		self.add("Property", "UsdHotel", currency="USD", company="UsdCo",
		         business_date="2026-12-01", rates_include_tax=0)
		self.add("Room Type", "UsdHotel-DLX", property="UsdHotel", base_price=300,
		         base_occupancy=2, adults_capacity=4, children_capacity=2, free_child_age=6)
		self.plan("KWD-PLAN", property="UsdHotel", rate_plan_name="Kuwait Tariff",
		          currency="KWD", discount_eligible=1)
		self.tariffs["KWD-PLAN"] = self.tariff(
			currency="KWD", nightly=100.500, property_currency="USD",
			fx_rate=3.25, fx_date="2026-11-24", agreed_rate=326.63,
		)
		q = self.pricing.quote_with_contract(
			"UsdHotel", "UsdHotel-DLX", "2026-12-01", "2026-12-02",
			rate_plan="KWD-PLAN", discount_type="Amount", discount_value=10.255, discount_currency="KWD",
		)
		cs = q["contract_summary"]
		self.assertEqual(cs["currency"], "KWD")
		self.assertEqual(cs["room_base_total"], 100.5)
		self.assertEqual(cs["room_discount_total"], 10.255)
		self.assertEqual(cs["room_net_total"], 90.245)
		self.assertEqual(q["room_total"], 293.3)  # round(90.245 * 3.25, 2dp)

		# Domestic UGX 0dp plan
		q_dom = self.pricing.quote_with_contract(
			"Tankhill", "Tankhill-STD", "2026-12-01", "2026-12-02",
			rate_plan="DOM-UGX", discount_type="Amount", discount_value=25000, discount_currency="UGX",
		)
		self.assertEqual(q_dom["rate_currency"], "UGX")
		self.assertEqual(q_dom["room_total"], 275000.0)
		self.assertEqual(q_dom["reservation_discount_total"], 25000.0)
		self.assertIsNone(q_dom.get("contract_summary"))

	def test_legacy_blank_discount_currency_evaluates_as_property_currency(self):
		# Pre-migration Reservation row stored with blank discount_currency on a USD stay
		doc = self.make_res(
			"RES-LEGACY",
			discount_type="Amount", discount_value=50000.0, discount_currency="",
			discount_reason="RATE-PROMO",
			amount_before_tax=562875.0, amount_after_tax=562875.0,
			night_rates=[Doc(
				stay_date="2026-12-01", rate=562875.0, base_rate=TANKHILL_GROSS_UGX,
				quoted_base_rate=TANKHILL_GROSS_UGX, rate_code_amount=TANKHILL_GROSS_UGX,
				gst_rate=0.0, meal_amount=0.0, demand_premium_pct=0.0, hand_price=0.0,
				discount_type=None, discount_value=0.0, discount_currency="",
				discount_amount=50000.0, discount_reason=None, fixed_rate=0,
			)],
		)
		# Legacy rate_lock_key compatibility: blank discount_currency is not added to quote_inputs
		self.assertNotIn("discount_currency", doc.quote_inputs())
		# Re-quoting and re-discounting a legacy row evaluates 50,000 in UGX, never USD
		q = doc.requote()
		self.assertEqual(q["room_total"], 562875.0)
		self.assertEqual(q["reservation_discount_total"], 50000.0)
		plan = doc.rediscount_locked_nights()
		self.assertEqual(plan["discount_total"], 50000.0)
		self.assertEqual(plan["rows"][0]["rate"], 562875.0)

	def test_fixed_at_check_in_relock_preserves_usd_50_discount_anchor(self):
		doc = self.make_res(
			"RES-RELOCK", rate_plan="BB-USD-CHK",
			discount_type="Amount", discount_value=50.0, discount_currency="USD",
			discount_reason="RATE-PROMO",
			amount_before_tax=TANKHILL_NET_UGX, amount_after_tax=TANKHILL_NET_UGX,
			night_rates=[Doc(
				stay_date="2026-12-01", rate=TANKHILL_NET_UGX, base_rate=TANKHILL_GROSS_UGX,
				quoted_base_rate=TANKHILL_GROSS_UGX, rate_code_amount=TANKHILL_GROSS_UGX,
				gst_rate=0.0, meal_amount=0.0, demand_premium_pct=0.0, hand_price=0.0,
				discount_type=None, discount_value=0.0, discount_currency=None,
				discount_amount=TANKHILL_DISC_UGX, discount_reason=None, fixed_rate=0,
			)],
		)
		doc.rate_lock_key = doc.rate_lock_key_for(doc.quote_inputs())
		self.docs[("Property", "Tankhill")].business_date = "2026-12-01"
		self.rate("USD", 4200.0, "2026-12-01", to="UGX")

		self.assertTrue(self.pricing.relock_contract_fx_at_check_in(doc))
		self.assertEqual(doc.agreed_rate, 630000.0)  # USD 150 * 4200
		doc.apply_pricing()
		row = doc.night_rates[0]
		self.assertEqual(row["base_rate"], 630000.0)
		self.assertEqual(row["discount_amount"], 210000.0)  # USD 50 * 4200
		self.assertEqual(row["rate"], 420000.0)  # USD 100 * 4200 — zero FX drift!

	def test_current_date_night_audit_applies_usd_amount_discount_at_posting_date_fx(self):
		doc = self.make_res(
			"RES-CURDATE", rate_plan="BB-USD-CUR",
			check_in_date="2026-12-01", check_out_date="2026-12-03",
			discount_type="Amount", discount_value=50.0, discount_currency="USD",
			discount_reason="RATE-PROMO",
			night_rates=[
				Doc(stay_date="2026-12-01", rate=TANKHILL_NET_UGX, gst_rate=0.0, discount_amount=TANKHILL_DISC_UGX),
				Doc(stay_date="2026-12-02", rate=TANKHILL_NET_UGX, gst_rate=0.0, discount_amount=TANKHILL_DISC_UGX),
			],
		)
		self.docs[("Property", "Tankhill")].business_date = "2026-12-02"
		self.rate("USD", 4000.0, "2026-12-01", to="UGX")
		self.rate("USD", 4200.0, "2026-12-02", to="UGX")

		n1 = self.folio.current_date_contract_night(doc, "2026-12-01")
		n2 = self.folio.current_date_contract_night(doc, "2026-12-02")
		self.assertEqual(n1["rate"], 400000.0)  # (150 - 50) * 4000
		self.assertEqual(n1["discount_amount"], 200000.0)
		self.assertEqual(n1["currency"], "USD")
		self.assertEqual(n1["contract_net_rate"], 100.0)
		self.assertEqual(n2["rate"], 420000.0)  # (150 - 50) * 4200
		self.assertEqual(n2["discount_amount"], 210000.0)
		self.assertEqual(n2["contract_net_rate"], 100.0)

		# Legacy blank discount_currency on Current Date still subtracts in UGX
		legacy = self.make_res(
			"RES-CURDATE-LEG", rate_plan="BB-USD-CUR",
			discount_type="Amount", discount_value=50000.0, discount_currency="",
		)
		n_leg = self.folio.current_date_contract_night(legacy, "2026-12-01")
		self.assertEqual(n_leg["rate"], 550000.0)  # 600,000 - 50,000 UGX
		self.assertEqual(n_leg["discount_amount"], 50000.0)

	def test_india_gst_slab_drops_on_discounted_usd_rate_translated_to_inr(self):
		self.add("Property", "IndiaHotel", country="India", currency="INR",
		         business_date="2026-12-01", rates_include_tax=0,
		         gst_mode="Slab", gst_slab_threshold=7500, gst_rate_low=5, gst_rate_high=18)
		self.add("Room Type", "IndiaHotel-DLX", property="IndiaHotel", base_price=9000,
		         base_occupancy=2, adults_capacity=4, children_capacity=2, free_child_age=6)
		self.plan("IND-USD", property="IndiaHotel", rate_plan_name="Foreign USD",
		          currency="USD", discount_eligible=1)
		self.tariffs["IND-USD"] = self.tariff(
			currency="USD", nightly=100.0, property_currency="INR",
			fx_rate=85.0, fx_date="2026-11-24", agreed_rate=8500.0,
		)
		# Undiscounted USD 100 -> INR 8,500 (> 7,500 threshold -> 18% GST)
		q0 = self.pricing.quote_with_contract(
			"IndiaHotel", "IndiaHotel-DLX", "2026-12-01", "2026-12-02", rate_plan="IND-USD",
		)
		self.assertEqual(q0["nightly"][0]["gst_rate"], 18.0)

		# Discounted -USD 20 -> USD 80 = INR 6,800 (<= 7,500 -> drops to 5% GST)
		q1 = self.pricing.quote_with_contract(
			"IndiaHotel", "IndiaHotel-DLX", "2026-12-01", "2026-12-02",
			rate_plan="IND-USD", discount_type="Amount", discount_value=20, discount_currency="USD",
		)
		self.assertEqual(q1["nightly"][0]["gst_rate"], 5.0)
		self.assertEqual(q1["room_total"], 6800.0)
		self.assertEqual(q1["tax_amount"], 340.0)
		self.assertEqual(q1["contract_summary"]["room_net_total"], 80.0)
		self.assertEqual(q1["contract_summary"]["room_tax_amount"], 4.0)
		self.assertEqual(q1["contract_summary"]["room_after_tax_total"], 84.0)

	def test_rate_guardrail_and_doa_floor_cap_convert_usd_discount_to_company_currency(self):
		# Rate Guardrail floor in UGX: 450,000 UGX
		self.records["Rate Guardrail"] = [
			Doc(property="Tankhill", room_type="Tankhill-STD", floor_price=450000.0,
			    days_of_week="", disabled=0),
		]
		q = self.pricing.quote_with_contract(
			"Tankhill", "Tankhill-STD", "2026-12-01", "2026-12-02",
			rate_plan="BB-USD", discount_type="Amount", discount_value=50, discount_currency="USD",
		)
		gov = self.api.discount_governance(
			"Tankhill", "Tankhill-STD", "BB-USD", q["nightly"],
			reason_code="RATE-PROMO", check=False,
		)
		self.assertEqual(gov["floor_breach"], ["2026-12-01"])
		self.assertTrue(gov["needs_supervisor"])
		self.assertEqual(gov["discount_total"], TANKHILL_DISC_UGX)
		self.assertAlmostEqual(gov["max_pct"], 33.33, places=2)

		# DoA Floor PIN cap: Approval Limit.custom_floor_override_max_amount = 100,000 UGX
		self.records["Approval Limit"] = [
			Doc(company="Tankhill Ltd", role="Hotel Admin",
			    custom_floor_override_max_amount=100000.0, disabled=0),
		]
		self.frappe.get_roles = lambda user=None: ["Hotel Admin"] if user == "gm@test" else self.roles

		# USD 50 = UGX 204,292 > UGX 100,000 cap -> MUST be refused with SITEOPS-DOA-LIMIT (no FX bypass!)
		with self.assertRaisesRegex(PermissionDenied, "SITEOPS-DOA-LIMIT"):
			self.api.discount_governance(
				"Tankhill", "Tankhill-STD", "BB-USD", q["nightly"],
				reason_code="RATE-PROMO", check=True,
				supervisor_user="gm@test", supervisor_pin="4321",
			)

		# USD 20 = UGX 81,717 <= UGX 100,000 cap -> succeeds with supervisor PIN
		q_small = self.pricing.quote_with_contract(
			"Tankhill", "Tankhill-STD", "2026-12-01", "2026-12-02",
			rate_plan="BB-USD", discount_type="Amount", discount_value=20, discount_currency="USD",
		)
		# Lower floor rail so USD 20 passes floor, still > 10% desk limit (13.33% > 10%) so supervisor runs
		self.records["Rate Guardrail"] = []
		gov_ok = self.api.discount_governance(
			"Tankhill", "Tankhill-STD", "BB-USD", q_small["nightly"],
			reason_code="RATE-PROMO", check=True,
			supervisor_user="gm@test", supervisor_pin="4321",
		)
		self.assertEqual(gov_ok["approver"], "gm@test")

		# When siteops_doa.verify_floor_authority bridge is enabled, amount passed MUST be UGX 204,292 (never 50!)
		self.records["Approval Limit"] = []
		self.doa_bridge_enabled = True
		self.doa_bridge_calls.clear()
		self.api.discount_governance(
			"Tankhill", "Tankhill-STD", "BB-USD", q["nightly"],
			reason_code="RATE-PROMO", check=True,
			supervisor_user="gm@test", supervisor_pin="4321",
		)
		self.assertEqual(len(self.doa_bridge_calls), 1)
		self.assertEqual(self.doa_bridge_calls[0]["amount"], TANKHILL_DISC_UGX)
		self.assertEqual(self.doa_bridge_calls[0]["company"], "Tankhill Ltd")

	def test_set_agreed_rate_with_override_currency_usd_updates_home_and_logs_both(self):
		self.roles = ["Revenue Manager"]
		res = self.make_res("RES-OV-USD")
		res.rate_lock_key = res.rate_lock_key_for(res.quote_inputs())

		pv = self.api.rate_override_preview(res.name, 110.0, override_currency="USD")
		self.assertEqual(pv["override_currency"], "USD")
		self.assertEqual(pv["override_amount"], 110.0)
		self.assertEqual(pv["agreed_rate"], 449442.0)  # round(110 * 4085.833333)

		res2 = self.make_res("RES-OV-USD-SAVE")
		res2.rate_lock_key = res2.rate_lock_key_for(res2.quote_inputs())
		out = self.api.set_agreed_rate(
			res2.name, 110.0, "RATE-PROMO", confirm_price=1, override_currency="USD",
		)
		self.assertEqual(out["agreed_rate"], 449442.0)
		self.assertEqual(out["override_currency"], "USD")
		self.assertEqual(out["override_amount"], 110.0)
		self.assertEqual(res2.agreed_rate, 449442.0)
		snap = self.pricing.saved_contract_tariff(res2)
		self.assertEqual(snap["tariff"]["nightly"], 150.0)  # original tariff preserved
		self.assertEqual(snap["agreed_override"]["currency"], "USD")
		self.assertEqual(snap["agreed_override"]["amount"], 110.0)
		self.assertTrue(self.action_logs)
		rationale = self.action_logs[-1]["rationale"]
		self.assertIn("USD 110.00", rationale)
		self.assertIn("449,442", rationale)

	def test_ota_modify_superseded_discount_formats_discount_currency_and_clears_on_currency_change(self):
		old = self.make_res(
			"RES-OTA-1",
			discount_type="Amount", discount_value=50.0, discount_currency="USD",
			discount_reason="RATE-PROMO",
			amount_before_tax=TANKHILL_NET_UGX, amount_after_tax=TANKHILL_NET_UGX,
			night_rates=[Doc(
				stay_date="2026-12-01", rate=TANKHILL_NET_UGX, base_rate=TANKHILL_GROSS_UGX,
				quoted_base_rate=TANKHILL_GROSS_UGX, rate_code_amount=TANKHILL_GROSS_UGX,
				gst_rate=0.0, meal_amount=0.0, demand_premium_pct=0.0, hand_price=0.0,
				discount_type="Amount", discount_value=50.0, discount_currency="USD",
				discount_amount=TANKHILL_DISC_UGX, discount_reason="RATE-PROMO", fixed_rate=0,
			)],
		)
		desc = self.channel_manager._describe_superseded_discount(old)
		self.assertIn("USD 50.00 off/night", desc)
		self.assertNotIn("UGX 50", desc)

		# Plan switches from USD to EUR -> old USD flat discount must be cleared & flagged for review
		self.plan("BB-EUR", property="Tankhill", rate_plan_name="BB EUR",
		          currency="EUR", discount_eligible=1)
		eur_snap = {
			"agreed_rate": 650000.0, "company": None,
			"tariff": {"currency": "EUR", "nightly": 150.0, "property_currency": "UGX",
			           "fx_rate": 4333.333333, "fx_date": "2026-11-24"},
		}
		doc = self.make_res(
			"RES-OTA-1", rate_plan="BB-EUR", agreed_rate=650000.0,
			contract_tariff=json.dumps(eur_snap),
			discount_type="Amount", discount_value=50.0, discount_currency="USD",
			discount_reason="RATE-PROMO",
			night_rates=[Doc(
				stay_date="2026-12-01", rate=TANKHILL_NET_UGX, base_rate=TANKHILL_GROSS_UGX,
				quoted_base_rate=TANKHILL_GROSS_UGX, rate_code_amount=TANKHILL_GROSS_UGX,
				gst_rate=0.0, meal_amount=0.0, demand_premium_pct=0.0, hand_price=0.0,
				discount_type="Amount", discount_value=50.0, discount_currency="USD",
				discount_amount=TANKHILL_DISC_UGX, discount_reason="RATE-PROMO", fixed_rate=0,
			)],
		)
		review = self.channel_manager._reconcile_ota_modify_discount(doc, old)
		self.assertIsNotNone(review)
		self.assertEqual(review["kind"], self.channel_manager.CHANNEL_REVIEW_DISCOUNT_REMOVED)
		self.assertIsNone(doc.discount_type)
		self.assertIsNone(doc.discount_currency)
		self.assertEqual(doc.discount_value, 0)
		self.assertIn("USD 50.00 off/night", review["desc"])

	def test_schema_doctypes_and_price_determination_guarded_fields_include_discount_currency(self):
		res_schema = json.loads(RESERVATION_JSON.read_text(encoding="utf-8"))
		nr_schema = json.loads(NIGHT_RATE_JSON.read_text(encoding="utf-8"))
		res_fields = {f["fieldname"]: f for f in res_schema["fields"]}
		nr_fields = {f["fieldname"]: f for f in nr_schema["fields"]}
		self.assertIn("discount_currency", res_fields)
		self.assertEqual(res_fields["discount_currency"]["fieldtype"], "Link")
		self.assertEqual(res_fields["discount_currency"]["options"], "Currency")
		self.assertIn("discount_currency", nr_fields)
		self.assertEqual(nr_fields["discount_currency"]["fieldtype"], "Link")
		self.assertEqual(nr_fields["discount_currency"]["options"], "Currency")
		self.assertIn(("discount_currency", "str"), self.pricing.PRICE_DETERMINATION_GUARDED_FIELDS)

		# Real localization.money_text supports explicit foreign currency formatting (0/2/3dp)
		spec = importlib.util.spec_from_file_location("kamra_loc_real_check", LOCALIZATION_PY)
		loc_mod = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(loc_mod)
		self.assertEqual(loc_mod.money_text("Tankhill", 50, currency="USD"), "USD 50.00")
		self.assertEqual(loc_mod.money_text("Tankhill", 12.125, currency="KWD"), "KWD 12.125")
