"""Round 2 of the rate matrix (Gap Record #8556): exchange rates and rounding.

The check-in re-lock converts only the booked contract at the check-in
rate; a Current Date night converts at its own posting date's published
rate (ERPNext Currency Exchange), keeps a discount or override, and says
so when it has to fall back; bills show the reference currency with their
totals; card-rate plans round each night; derived plans add amounts per
night; a base plan's children follow its currency and enablement."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

from kamra.tests.rate_matrix_harness import Doc, RateMatrixHarness, ValidationError

BOOKED = {"agreed_rate": 474500, "tariff": {"currency": "USD", "nightly": 130, "fx_rate": 3650,
                                            "fx_date": "2026-11-20", "property_currency": "UGX"}}


class ContractStayHarness(RateMatrixHarness):
	"""A two-night USD contract stay booked at 3,650 (USD 130 = UGX 474,500 a night)."""

	modules = ("kamra.pricing", "kamra.folio")
	policy = "Fixed at Check In"

	def setUp(self):
		super().setUp()
		self.add("Room Type", "Test Hotel-DLX", property="Test Hotel", base_price=400000)
		self.plan("CORP", fx_lock_policy=self.policy, rounding_step=1000)
		self.res = self.add("Reservation", "RES-1", property="Test Hotel", room="Test Hotel-101",
		                    room_type="Test Hotel-DLX", rate_plan="CORP", check_in_date="2026-12-01",
		                    check_out_date="2026-12-03", adults=2, children=0, extra_beds=0,
		                    company="Client Co", agreed_rate=474500, status="Confirmed",
		                    contract_tariff=json.dumps(BOOKED),
		                    night_rates=[Doc(stay_date="2026-12-01", rate=474500, gst_rate=0),
		                                 Doc(stay_date="2026-12-02", rate=474500, gst_rate=0)])
		# a revised Item Price the stay was never booked at: nothing here may re-price to it
		self.tariffs["CORP"] = self.tariff(nightly=140, agreed_rate=511000, fx_rate=3650)


class CheckInRelockFxOnlyTest(ContractStayHarness):
	"""P0-2: OPERA Exchange Type Fixed at Check In keeps the booked contract amount."""

	def setUp(self):
		super().setUp()
		self.rate("USD", 3712.5, "2026-12-01")

	def test_only_the_booked_nightly_is_converted_at_the_check_in_rate(self):
		self.assertTrue(self.pricing.relock_contract_fx_at_check_in(self.res))
		self.assertEqual(self.tariff_forms(), [])  # the revised USD 140 Item Price is never read
		self.assertEqual(self.res.agreed_rate, 483000)  # USD 130 x 3,712.5 = 482,625, nearest 1,000
		self.assertEqual(self.res.contract_tariff["tariff"]["nightly"], 130.0)
		self.assertEqual(self.res.contract_tariff["tariff"]["fx_rate"], 3712.5)
		self.assertEqual(self.res.contract_tariff["agreed_rate"], 483000)

	def test_a_derived_plan_stay_relocks_without_the_resolver_flag(self):
		self.res.contract_tariff = json.dumps({**BOOKED, "tariff": {**BOOKED["tariff"], "derived_from": "CON"}})
		self.assertTrue(self.pricing.relock_contract_fx_at_check_in(self.res))
		# the tariff resolver never priced this stay (no custom_tariff_currency): its relock guard must not run
		self.assertFalse(self.res.flags.get("kamra_fx_checkin_relock"))
		self.assertTrue(self.res.flags.get("kamra_contract_relocked"))
		self.assertEqual(self.res.contract_tariff["tariff"]["derived_from"], "CON")
		# a fresh booking of a stay the tariff resolver priced
		self.res.flags.clear()
		self.res.update(custom_tariff_currency="USD", agreed_rate=474500, contract_tariff=json.dumps(BOOKED))
		self.assertTrue(self.pricing.relock_contract_fx_at_check_in(self.res))
		self.assertTrue(self.res.flags.get("kamra_fx_checkin_relock"))

	def test_a_manual_override_or_discount_is_never_wiped(self):
		self.res.agreed_rate = 450000  # a manager's override (or a reservation discount) before arrival
		self.assertFalse(self.pricing.relock_contract_fx_at_check_in(self.res))
		self.assertEqual(self.res.agreed_rate, 450000)
		self.assertEqual(self.res.contract_tariff, json.dumps(BOOKED))
		self.assertFalse(self.res.flags.get("kamra_contract_relocked"))
		self.assertFalse(self.res.flags.get("kamra_fx_checkin_relock"))

	def test_a_missing_check_in_rate_refuses_the_relock_loudly(self):
		self.exchange_rates.clear()
		with self.assertRaisesRegex(self.pricing.ContractRateRefused, "exchange rate"):
			self.pricing.relock_contract_fx_at_check_in(self.res)
		self.assertEqual(self.res.agreed_rate, 474500)


class CurrentDatePostingTest(ContractStayHarness):
	"""P1-1: OPERA Exchange Type Current Date posts each night at its own date's rate."""

	policy = "Current Date"

	def post_night(self, date="2026-12-02"):
		lines = []
		patches = (
			patch.object(self.folio, "_charge_posted", lambda *args: False),
			patch.object(self.folio, "_post_room_levy", lambda *args: None),
			patch.object(self.folio, "_append_charge",
			             lambda folios, reservation, charge_type, line, *args: lines.append(line)),
		)
		for patcher in patches:
			patcher.start()
			self.addCleanup(patcher.stop)
		self.folio.post_room_night(self.res, date, folios={})
		room, = [line for line in lines if line["charge_type"] == "Room"]
		return room

	def test_a_back_dated_night_converts_at_its_own_posting_date(self):
		self.docs["Property", "Test Hotel"].business_date = "2026-12-05"
		self.rate("USD", 3700, "2026-12-02")
		self.rate("USD", 3800, "2026-12-05")
		room = self.post_night("2026-12-02")
		self.assertEqual(self.fx_queries[-1]["date"], ["between", ["2026-12-02", "2026-12-02"]])
		self.assertEqual(room["rate"], 481000)  # USD 130 x 3,700
		self.assertIn("at 3,700 (2026-12-02)", room["description"])

	def test_the_posting_rate_comes_from_currency_exchange_not_a_re_priced_contract(self):
		self.docs["Property", "Test Hotel"].business_date = "2026-12-02"
		self.tariffs["CORP"] = {"is_tariff": 1, "refusal": "HSP-TARIFF-002: The Item Price expired."}
		self.rate("USD", 3712.5, "2026-12-02")
		room = self.post_night()
		self.assertEqual(self.tariff_forms(), [])
		self.assertEqual(room["rate"], 483000)
		self.assertNotIn("stale", room["description"])

	def test_the_charge_records_the_rate_it_posted_at(self):
		self.docs["Property", "Test Hotel"].business_date = "2026-12-02"
		self.rate("USD", 3712.5, "2026-12-02")
		room = self.post_night()
		self.assertEqual((room["contract_currency"], room["contract_fx_rate"], str(room["contract_fx_date"]),
		                  room["contract_fx_stale"]), ("USD", 3712.5, "2026-12-02", 0))

	def test_a_missing_posting_rate_is_marked_on_the_charge_and_logged(self):
		self.docs["Property", "Test Hotel"].business_date = "2026-12-02"
		room = self.post_night()
		self.assertEqual(room["rate"], 474500)  # the locked night, at the booking's rate
		self.assertIn("[stale booking FX]", room["description"])
		self.assertEqual((room["contract_fx_rate"], room["contract_fx_stale"]), (3650.0, 1))
		self.assertTrue(self.logged, "a stale posting rate must reach the error log for Night Audit and Finance")

	def test_a_reservation_discount_keeps_its_share_of_the_night(self):
		self.docs["Property", "Test Hotel"].business_date = "2026-12-02"
		self.rate("USD", 3712.5, "2026-12-02")
		for night in self.res.night_rates:
			night.rate = 427050  # 10% off the booked UGX 474,500
		self.res.agreed_rate = 427050
		self.assertEqual(self.post_night()["rate"], 434700)  # 483,000 at today's rate, less the same 10%


class ReferenceCurrencyTotalsTest(ContractStayHarness):
	"""P0-4: the bill in the contract currency, at the Property's ACTUAL or CURRENT rate."""

	def test_actual_converts_the_bill_totals_at_the_booked_rate(self):
		shown = self.folio.reference_currency(self.res, grand_total=949000, balance=474500)
		self.assertEqual((shown["calculation"], shown["fx_rate"], shown["fx_date"]), ("ACTUAL", 3650.0, "2026-11-20"))
		self.assertEqual((shown["grand_total_reference"], shown["balance_reference"]), (260.0, 130.0))

	def test_current_converts_at_today_s_published_rate(self):
		self.docs["Property", "Test Hotel"].reference_currency_calculation = "CURRENT"
		self.rate("USD", 3800, "2026-12-01")
		shown = self.folio.reference_currency(self.res, grand_total=949000, balance=0)
		self.assertEqual(self.tariff_forms(), [])
		self.assertEqual((shown["fx_rate"], shown["fx_date"]), (3800.0, "2026-12-01"))
		self.assertEqual((shown["grand_total_reference"], shown["balance_reference"]), (249.74, 0.0))

	def test_actual_on_a_current_date_stay_reflects_the_rates_its_nights_posted_at(self):
		self.docs["Rate Plan", "CORP"].fx_lock_policy = "Current Date"
		charge = dict(parenttype="Folio", reservation="RES-1", charge_type="Room", contract_currency="USD")
		self.records["Folio Charge"] = [
			Doc(charge, name="C1", contract_fx_rate=3700, contract_fx_date="2026-12-01", reversal_of=None),
			Doc(charge, name="C2", contract_fx_rate=3800, contract_fx_date="2026-12-02", reversal_of=None),
			# a voided night and its reversal never count
			Doc(charge, name="C3", contract_fx_rate=9999, contract_fx_date="2026-12-02", reversal_of=None),
			Doc(charge, name="C4", contract_fx_rate=9999, contract_fx_date="2026-12-02", reversal_of="C3"),
		]
		shown = self.folio.reference_currency(self.res, grand_total=950000, balance=0)
		self.assertEqual((shown["fx_rate"], shown["fx_date"], shown["posted_nights"]), (3750.0, "2026-12-02", 2))
		self.assertEqual(shown["grand_total_reference"], 253.33)

	def test_actual_on_a_current_date_stay_before_any_night_posts_uses_the_booked_rate(self):
		self.docs["Rate Plan", "CORP"].fx_lock_policy = "Current Date"
		shown = self.folio.reference_currency(self.res)
		self.assertEqual((shown["fx_rate"], shown["fx_date"]), (3650.0, "2026-11-20"))
		self.assertNotIn("posted_nights", shown)


class CardStayHarness(RateMatrixHarness):
	"""Three adults and a child in a 100,000 room (+20,000 extra adult, +10,000 child)."""

	modules = ("kamra.pricing", "kamra.folio")

	def setUp(self):
		super().setUp()
		self.add("Room Type", "Test Hotel-DLX", property="Test Hotel", base_price=100000, base_occupancy=2,
		         adults_capacity=4, extra_adult_price=20000, child_price=10000)
		self.plan("BAR", modifier_type="Percent", modifier_value=-10)
		self.plan("CORP", base_rate_plan="BAR", modifier_type="Percent", modifier_value=10)
		self.season = {}
		p = self.pricing
		rules = {"no_small_group": False, "minimum_nights": 0, "season": None, "non_refundable": False,
		         "full_payment": False, "is_holiday": False, "holiday": None, "advance_percent": 0,
		         "advance_season": None}
		for name, value in (
			("small_group_allows_stay", lambda *args: True), ("stay_season_rules", lambda *args, **kw: rules),
			("long_stay_rule", lambda *args: None), ("active_holiday_package", lambda *args: None),
			("season_adjust", lambda property, date, base, room_type=None: base * self.season.get(str(date), 1)),
			("forecast_occupancy", lambda *args, **kw: 0), ("demand_adjust", lambda *args, **kw: (args[3], None)),
			("advance_percent", lambda *args: (0.0, None)),
		):
			patcher = patch.object(p, name, value)
			patcher.start()
			self.addCleanup(patcher.stop)
		patcher = patch.object(self.folio, "_fnb_gst", lambda property: 5)
		patcher.start()
		self.addCleanup(patcher.stop)

	def quote(self, plan, adults=3, children=1, check_out="2026-12-03"):
		return self.pricing.quote("Test Hotel", "Test Hotel-DLX", "2026-12-01", check_out, adults, children,
		                          rate_plan=plan)


class PerNightRoundingTest(CardStayHarness):
	"""P1-5: OPERA rounds the nightly price a guest sees, not the stay total."""

	def test_nights_with_different_prices_round_on_their_own(self):
		self.plan("RND", modifier_type="Percent", modifier_value=-10, rounding_step=1000)
		self.season["2026-12-02"] = Decimal("1.5")
		self.docs["Room Type", "Test Hotel-DLX"].base_price = 100400
		quoted = self.quote("RND", adults=2, children=0)
		# 90,360 -> 90,000 and 135,540 -> 136,000; a stay-level 225,900 -> 226,000 would split 90,040 / 135,960
		self.assertEqual([night["planned_rate"] for night in quoted["nightly"]], [90000, 136000])
		self.assertEqual(quoted["room_total"], 226000)

	def test_a_three_night_stay_never_splits_a_rounded_total_into_fractions(self):
		# a root plan's Absolute is the whole stay's price: 1,000,000 over three nights
		self.plan("THIRD", modifier_type="Absolute", modifier_value=1000000, rounding_step=1000)
		quoted = self.quote("THIRD", adults=2, children=0, check_out="2026-12-04")
		self.assertEqual([night["planned_rate"] for night in quoted["nightly"]], [333000, 333000, 333000])
		self.assertEqual(quoted["room_total"], 999000)


class LockedRoundedNightsTest(RateMatrixHarness):
	modules = ("kamra.pricing", "kamra.kamra.doctype.reservation.reservation")

	def test_the_booking_locks_each_rounded_night(self):
		doc = self.reservation.Reservation(meal_plan=None, flags=Doc())
		quote = {"room_total": 226000, "nightly": [
			{"date": "2026-12-01", "rate": 100400, "gst_rate": 0, "planned_rate": 90000},
			{"date": "2026-12-02", "rate": 150600, "gst_rate": 0, "planned_rate": 136000}]}
		self.assertEqual([row["rate"] for row in doc.quote_night_rows(quote)], [90000, 136000])


class FolioFallbackAlignmentTest(CardStayHarness):
	"""P1-5: a stay booked before nights were locked posts what quote() prices."""

	def setUp(self):
		super().setUp()
		self.res = self.add("Reservation", "RES-2", property="Test Hotel", room_type="Test Hotel-DLX",
		                    rate_plan="CORP", check_in_date="2026-12-01", check_out_date="2026-12-03",
		                    adults=3, children=1, night_rates=[])

	def night(self, date="2026-12-01"):
		return self.folio._nightly_room_rate(self.res, date)

	def test_a_derived_plan_leaves_the_extra_person_part_as_the_quote_does(self):
		self.assertEqual(self.quote("CORP")["room_total"], 252000)
		self.assertAlmostEqual(self.night(), 126000, places=6)
		self.assertAlmostEqual(self.night("2026-12-02"), 126000, places=6)

	def test_a_rounded_plan_posts_the_quote_s_rounded_night(self):
		self.docs["Rate Plan", "CORP"].update(rounding_step=5000, rounding_mode="Up")
		self.assertEqual([n["planned_rate"] for n in self.quote("CORP")["nightly"]], [130000, 130000])
		self.assertAlmostEqual(self.night(), 130000, places=6)


class DerivedModifierPerNightTest(CardStayHarness):
	"""P1-6: a derived plan's Amount or Absolute is per night, on a card rate as on a contract."""

	def test_a_card_derived_amount_or_absolute_applies_to_each_night(self):
		self.plan("CORP-AMT", base_rate_plan="BAR", modifier_type="Amount", modifier_value=-100)
		self.plan("CORP-ABS", base_rate_plan="BAR", modifier_type="Absolute", modifier_value=300)
		self.plan("ADD", modifier_type="Amount", modifier_value=50)
		apply = self.pricing.apply_rate_plan
		self.assertEqual(apply("CORP-AMT", Decimal(1000), nights=2), Decimal(700))  # 900, less 100 a night
		self.assertEqual(apply("CORP-ABS", Decimal(1000), nights=2), Decimal(600))  # 300 a night
		self.assertEqual(apply("ADD", Decimal(1000), nights=2), Decimal(1050))  # a root plan's amount is per stay

	def test_the_quote_takes_a_derived_amount_once_per_night(self):
		self.plan("CORP-AMT", base_rate_plan="BAR", modifier_type="Amount", modifier_value=-10000)
		self.assertEqual(self.quote("CORP-AMT")["room_total"], 214000)  # 234,000 less 10,000 a night

	def test_a_contract_derived_amount_applies_to_each_night_too(self):
		self.plan("CON")
		self.plan("CON-AMT", base_rate_plan="CON", modifier_type="Amount", modifier_value=-10)
		self.tariffs["CON"] = self.tariff()
		agreed, tariff = self.pricing.contract_rate("Test Hotel", "Test Hotel-DLX", "CON-AMT", "2026-12-01",
		                                            "2026-12-03", 2, 0)
		self.assertEqual((agreed, tariff["nightly"]), (438000, 120.0))  # USD 120 a night at 3,650


class RatePlanParentValidationTest(RateMatrixHarness):
	"""P1-6: a base plan's derived plans follow its currency and its enablement."""

	def setUp(self):
		super().setUp()
		self.controller = self.load("kamra.kamra.doctype.rate_plan.rate_plan")
		self.plan("BAR")

	def validate(self, **fields):
		values = {"name": "Test Hotel-CORP", "property": "Test Hotel", "rate_plan_name": "Corporate",
		          "code": "CORP", "modifier_type": "Percent", "modifier_value": -5,
		          "fx_lock_policy": "Fixed at Reservation", "rounding_step": 0, "rounding_mode": "Nearest"}
		doc = self.controller.RatePlan({**values, **fields})
		doc.validate()
		return doc

	def children(self, *plans):
		self.records["Rate Plan"] = [self.plan(name, **fields) for name, fields in plans]

	def test_a_disabled_base_plan_is_refused(self):
		self.docs["Rate Plan", "BAR"].disabled = 1
		with self.assertRaisesRegex(ValidationError, "disabled"):
			self.validate(base_rate_plan="BAR")

	def test_a_base_plan_cannot_change_to_a_currency_its_derived_plans_do_not_use(self):
		self.children(("CHILD", {"base_rate_plan": "Test Hotel-CORP", "currency": "UGX"}))
		self.validate(currency="UGX")
		with self.assertRaisesRegex(ValidationError, "CHILD"):
			self.validate(currency="USD")

	def test_an_inheriting_child_passes_the_check_on_to_its_own_derived_plans(self):
		self.children(("CHILD", {"base_rate_plan": "Test Hotel-CORP"}),
		              ("GRANDCHILD", {"base_rate_plan": "CHILD", "currency": "UGX"}))
		self.validate(currency="UGX")
		with self.assertRaisesRegex(ValidationError, "GRANDCHILD"):
			self.validate(currency="USD")

	def test_a_base_plan_with_enabled_derived_plans_cannot_be_disabled(self):
		self.children(("CHILD", {"base_rate_plan": "Test Hotel-CORP", "disabled": 0}))
		with self.assertRaisesRegex(ValidationError, "CHILD"):
			self.validate(disabled=1)
		self.docs["Rate Plan", "CHILD"].disabled = 1
		self.records["Rate Plan"][0].disabled = 1
		self.validate(disabled=1)

