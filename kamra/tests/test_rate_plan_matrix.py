"""Rate Plan currency, base -> derived rates, rounding and the FX lock policy.

OPERA Cloud Rate Code financial details (Currency Code, Exchange Type, Base
Rate / Derived Rate, Base Rate Extra Person Calculation) and SAP rounding
(DIFF), checked on the real pricing, folio, API and controller code."""

from __future__ import annotations

import json
from decimal import Decimal
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from kamra.tests.rate_matrix_harness import Doc, RateMatrixHarness, ValidationError

STAY = ("Test Hotel", "Test Hotel-DLX", "2026-12-01", "2026-12-03", 2, 0)


class RoundingTest(RateMatrixHarness):
	def test_a_step_rounds_to_its_multiple_in_each_mode(self):
		r = self.pricing.round_rate_amount
		self.assertEqual(r(Decimal("445304"), Decimal("1000")), Decimal("445000"))
		self.assertEqual(r(Decimal("445500"), Decimal("1000")), Decimal("446000"))
		self.assertEqual(r(Decimal("445304"), Decimal("1000"), "Up"), Decimal("446000"))
		self.assertEqual(r(Decimal("445999"), Decimal("1000"), "Down"), Decimal("445000"))
		self.assertEqual(r(Decimal("447400"), 5000), Decimal("445000"))
		self.assertEqual(r(Decimal("447500"), 5000), Decimal("450000"))
		self.assertEqual(r(Decimal("12.33"), Decimal("0.05")), Decimal("12.35"))
		self.assertIsInstance(r(1, 0, currency="USD"), Decimal)

	def test_a_zero_step_rounds_to_the_currency_minor_unit(self):
		r = self.pricing.round_rate_amount
		self.assertEqual(r(Decimal("445304.5"), 0, currency="UGX"), Decimal("445305"))
		self.assertEqual(r(Decimal("445304.5"), 0, "Down", currency="UGX"), Decimal("445304"))
		self.assertEqual(r(Decimal("445304.1"), 0, "Up", currency="UGX"), Decimal("445305"))
		self.assertEqual(r(Decimal("130.125"), 0, currency="USD"), Decimal("130.13"))
		self.assertEqual(r(Decimal("1.2345"), 0, currency="KWD"), Decimal("1.235"))
		self.assertEqual(r(Decimal("1.005"), 0), Decimal("1.01"))

	def test_a_negative_step_or_unknown_mode_is_refused(self):
		with self.assertRaisesRegex(ValidationError, "negative"):
			self.pricing.round_rate_amount(100, -1)
		with self.assertRaisesRegex(ValidationError, "Nearest, Up or Down"):
			self.pricing.round_rate_amount(100, 10, "Sideways")


class RatePlanChainTest(RateMatrixHarness):
	def chain(self, name):
		return [plan.name for plan in self.pricing.resolve_rate_plan_chain(name)]

	def test_the_chain_lists_base_plans_root_first(self):
		self.plan("BAR")
		self.plan("CORP", base_rate_plan="BAR")
		self.plan("CORP-NR", base_rate_plan="CORP")
		self.assertEqual(self.chain("CORP-NR"), ["BAR", "CORP", "CORP-NR"])
		self.assertEqual(self.chain("BAR"), ["BAR"])
		self.assertEqual([p.name for p in self.pricing.resolve_rate_plan_chain(self.docs["Rate Plan", "CORP"])],
		                 ["BAR", "CORP"])

	def test_a_circular_derivation_is_refused(self):
		self.plan("A", base_rate_plan="B")
		self.plan("B", base_rate_plan="A")
		self.plan("SELF", base_rate_plan="SELF")
		for name in ("A", "SELF"):
			with self.subTest(name=name), self.assertRaisesRegex(ValidationError, "loop back"):
				self.chain(name)

	def test_a_chain_holds_at_most_five_plans(self):
		self.plan("P1")
		for level in range(2, 7):
			self.plan(f"P{level}", base_rate_plan=f"P{level - 1}")
		self.assertEqual(self.pricing.RATE_PLAN_CHAIN_LIMIT, 5)
		self.assertEqual(self.chain("P5"), ["P1", "P2", "P3", "P4", "P5"])
		with self.assertRaisesRegex(ValidationError, "at most 5"):
			self.chain("P6")


class DerivedRatePlanTest(RateMatrixHarness):
	def setUp(self):
		super().setUp()
		self.plan("BAR", modifier_type="Percent", modifier_value=-10)

	def apply(self, plan, total, extras=0):
		return self.pricing.apply_rate_plan(plan, Decimal(total), extra_person_total=Decimal(extras))

	def test_a_plan_without_a_base_keeps_its_own_modifier(self):
		self.plan("ADD", modifier_type="Amount", modifier_value=50)
		self.plan("FIX", modifier_type="Absolute", modifier_value=700)
		self.assertEqual(self.apply("BAR", 1000), Decimal(900))
		self.assertEqual(self.apply("BAR", 1000, extras=200), Decimal(900))
		self.assertEqual(self.apply("ADD", 1000), Decimal(1050))
		self.assertEqual(self.apply("FIX", 1000), Decimal(700))
		self.assertEqual(self.pricing.apply_rate_plan(None, Decimal(1000)), Decimal(1000))

	def test_a_derived_plan_applies_its_base_then_its_own_modifier(self):
		self.plan("CORP", base_rate_plan="BAR", modifier_type="Percent", modifier_value=5)
		self.plan("CORP-AMT", base_rate_plan="BAR", modifier_type="Amount", modifier_value=-100)
		self.assertEqual(self.apply("CORP", 1000), Decimal(945))
		self.assertEqual(self.apply("CORP-AMT", 1000), Decimal(800))

	def test_extra_person_charges_follow_the_base_unless_the_plan_adjusts_them(self):
		self.plan("CORP", base_rate_plan="BAR", modifier_type="Percent", modifier_value=10)
		# base: 1,000 (200 of it extra guests) -> 900, of which 180 extra guests
		self.assertEqual(self.apply("CORP", 1000, extras=200), Decimal(972))
		self.docs["Rate Plan", "CORP"].adjust_extra_person = 1
		self.assertEqual(self.apply("CORP", 1000, extras=200), Decimal(990))


class RatePlanValidationTest(RateMatrixHarness):
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

	def test_a_derived_plan_on_the_same_property_and_currency_is_accepted(self):
		self.validate(base_rate_plan="BAR")
		self.validate(base_rate_plan="BAR", currency="UGX")
		self.docs["Rate Plan", "BAR"].currency = "USD"
		self.validate(base_rate_plan="BAR")
		self.validate(base_rate_plan="BAR", currency="USD", rounding_step=1000)

	def test_a_plan_cannot_derive_from_itself_or_its_own_derivatives(self):
		with self.assertRaisesRegex(ValidationError, "loop back"):
			self.validate(base_rate_plan="Test Hotel-CORP")
		self.plan("CHILD", base_rate_plan="Test Hotel-CORP")
		with self.assertRaisesRegex(ValidationError, "loop back"):
			self.validate(base_rate_plan="CHILD")

	def test_the_base_must_be_at_the_same_property_and_in_the_same_currency(self):
		self.plan("OTHER", property="Sister Hotel")
		with self.assertRaisesRegex(ValidationError, "same property"):
			self.validate(base_rate_plan="OTHER")
		with self.assertRaisesRegex(ValidationError, "base plan's currency"):
			self.validate(base_rate_plan="BAR", currency="USD")
		self.docs["Rate Plan", "BAR"].currency = "USD"
		with self.assertRaisesRegex(ValidationError, "base plan's currency"):
			self.validate(base_rate_plan="BAR", currency="EUR")

	def test_a_negative_rounding_step_is_refused(self):
		# Select options (lock policy, rounding mode) are Frappe's own select validation.
		with self.assertRaisesRegex(ValidationError, "negative"):
			self.validate(rounding_step=-1)
		for policy in ("Fixed at Reservation", "Fixed at Check In", "Current Date", "Fixed at Block"):
			self.validate(fx_lock_policy=policy, rounding_step=5000)


class QuoteDerivationTest(RateMatrixHarness):
	"""The derived modifier, extra-person rule and rounding inside the real quote()."""

	def extra_modules(self):
		folio = ModuleType("kamra.folio")
		folio._fnb_gst = lambda property: 5
		return {"kamra.folio": folio}

	def setUp(self):
		super().setUp()
		self.add("Room Type", "Test Hotel-DLX", property="Test Hotel", base_price=100000, base_occupancy=2,
		         adults_capacity=4, extra_adult_price=20000, child_price=10000)
		self.plan("BAR", modifier_type="Percent", modifier_value=-10)
		self.plan("CORP", base_rate_plan="BAR", modifier_type="Percent", modifier_value=10)
		p = self.pricing
		rules = {"no_small_group": False, "minimum_nights": 0, "season": None, "non_refundable": False,
		         "full_payment": False, "is_holiday": False, "holiday": None, "advance_percent": 0,
		         "advance_season": None}
		for name, value in (
			("small_group_allows_stay", lambda *args: True), ("stay_season_rules", lambda *args, **kw: rules),
			("long_stay_rule", lambda *args: None), ("active_holiday_package", lambda *args: None),
			("season_adjust", lambda property, date, base, room_type=None: base),
			("forecast_occupancy", lambda *args, **kw: 0), ("demand_adjust", lambda *args, **kw: (args[3], None)),
			("advance_percent", lambda *args: (0.0, None)),
		):
			patcher = patch.object(p, name, value)
			patcher.start()
			self.addCleanup(patcher.stop)

	def quote(self, plan):
		# three adults and a child: 100,000 room + 20,000 extra adult + 10,000 child, two nights
		return self.pricing.quote("Test Hotel", "Test Hotel-DLX", "2026-12-01", "2026-12-03", 3, 1,
		                          rate_plan=plan)

	def test_the_card_base_plan_still_works_on_the_whole_stay(self):
		self.assertEqual(self.quote("BAR")["room_total"], 234000)

	def test_a_derived_plan_leaves_the_base_extra_person_charges_alone(self):
		# BAR: 260,000 -> 234,000 (54,000 extra guests); CORP +10% on the 180,000 room part
		self.assertEqual(self.quote("CORP")["room_total"], 252000)
		self.docs["Rate Plan", "CORP"].adjust_extra_person = 1
		self.assertEqual(self.quote("CORP")["room_total"], 257400)

	def test_the_plan_rounding_step_rounds_the_guest_facing_stay_price(self):
		self.docs["Rate Plan", "CORP"].rounding_step = 5000
		self.assertEqual(self.quote("CORP")["room_total"], 250000)
		self.docs["Rate Plan", "CORP"].rounding_mode = "Up"
		self.assertEqual(self.quote("CORP")["room_total"], 255000)
		# tax-inclusive house: the gross price is rounded, the tax backed out of it
		self.docs["Property", "Test Hotel"].rates_include_tax = 1
		self.room_tax = 18
		self.docs["Rate Plan", "CORP"].rounding_mode = "Nearest"
		quoted = self.quote("CORP")
		self.assertAlmostEqual(quoted["amount_after_tax"], 250000, places=6)
		self.assertAlmostEqual(quoted["room_total"], 250000 / 1.18, places=6)


class ContractRateTest(RateMatrixHarness):
	def resolve(self, plan="CORP", **kwargs):
		return self.pricing.contract_rate(*STAY[:2], plan, *STAY[2:], **kwargs)

	def test_the_plan_policy_rounding_and_pricing_event_reach_the_resolver(self):
		self.plan("CORP", fx_lock_policy="Fixed at Check In", rounding_step=1000, rounding_mode="Up")
		self.tariffs["CORP"] = self.tariff()
		self.resolve(company="Client Co", pricing_event="check_in", fx_date="2026-12-01")
		self.assertEqual(self.tariff_forms(), [{
			"property": "Test Hotel", "room_type": "Test Hotel-DLX", "rate_plan": "CORP",
			"check_in": "2026-12-01", "check_out": "2026-12-03", "adults": 2, "children": 0, "extra_beds": 0,
			"company": "Client Co", "fx_lock_policy": "Fixed at Check In", "rounding_step": 1000.0,
			"rounding_mode": "Up", "pricing_event": "check_in", "fx_date": "2026-12-01"}])

	def test_a_booking_is_the_default_event_and_the_resolver_figure_is_kept_without_a_step(self):
		self.plan("CORP")
		self.tariffs["CORP"] = self.tariff(agreed_rate=474500.4)
		agreed, tariff = self.resolve()
		form, = self.tariff_forms()
		self.assertEqual((form["pricing_event"], form["fx_lock_policy"], form["rounding_step"],
		                  form["rounding_mode"]), ("reservation", "Fixed at Reservation", 0.0, "Nearest"))
		self.assertNotIn("company", form)
		self.assertNotIn("fx_date", form)
		self.assertEqual(agreed, 474500.4)
		self.assertNotIn("derived_from", tariff)

	def test_an_explicit_step_rounds_the_converted_local_rate(self):
		self.plan("CORP", rounding_step=1000)
		self.tariffs["CORP"] = self.tariff(agreed_rate=445304)
		agreed, tariff = self.resolve()
		self.assertEqual(agreed, 445000)
		self.assertEqual((tariff["nightly"], tariff["fx_rate"]), (130, 3650))
		self.docs["Rate Plan", "CORP"].rounding_mode = "Up"
		self.assertEqual(self.resolve()[0], 446000)

	def test_a_derived_plan_inherits_its_base_contract_and_applies_its_modifier(self):
		self.plan("CON")
		self.plan("CON-NR", base_rate_plan="CON", modifier_type="Percent", modifier_value=-10,
		          rounding_step=1000)
		self.tariffs["CON"] = self.tariff()
		agreed, tariff = self.resolve("CON-NR")
		self.assertEqual([form["rate_plan"] for form in self.tariff_forms()], ["CON-NR", "CON"])
		self.assertEqual(agreed, 427000)  # USD 117.00 at 3,650 = 427,050, nearest 1,000
		self.assertEqual(tariff, {"currency": "USD", "nightly": 117.0, "fx_rate": 3650.0,
		                          "fx_date": "2026-11-20", "property_currency": "UGX",
		                          "derived_from": "CON"})

	def test_a_derived_contract_adjusts_the_extra_person_amount_only_when_asked(self):
		self.plan("CON")
		self.plan("CON-PLUS", base_rate_plan="CON", modifier_type="Percent", modifier_value=10)
		self.tariffs["CON"] = self.tariff(nightly=150, extra_person=20, agreed_rate=547500)
		self.assertEqual(self.resolve("CON-PLUS")[0], 594950)  # (130 x 1.1 + 20) x 3,650
		self.docs["Rate Plan", "CON-PLUS"].adjust_extra_person = 1
		self.assertEqual(self.resolve("CON-PLUS")[0], 602250)  # 150 x 1.1 x 3,650

	def test_a_plan_with_its_own_contract_is_not_derived_again(self):
		self.plan("CON")
		self.plan("CON-NR", base_rate_plan="CON", modifier_type="Percent", modifier_value=-10)
		self.tariffs["CON-NR"] = self.tariff(nightly=120, agreed_rate=438000)
		self.assertEqual(self.resolve("CON-NR")[0], 438000)
		self.assertEqual([form["rate_plan"] for form in self.tariff_forms()], ["CON-NR"])

	def test_a_base_contract_refusal_refuses_the_derived_plan(self):
		self.plan("CON")
		self.plan("CON-NR", base_rate_plan="CON", modifier_type="Percent", modifier_value=-10)
		self.tariffs["CON"] = {"is_tariff": 1, "refusal": "HSP-TARIFF-002: No price."}
		with self.assertRaisesRegex(self.pricing.ContractRateRefused, "HSP-TARIFF-002"):
			self.resolve("CON-NR")


class FxLockPolicyTest(RateMatrixHarness):
	modules = ("kamra.pricing", "kamra.folio")

	def setUp(self):
		super().setUp()
		self.add("Room Type", "Test Hotel-DLX", property="Test Hotel", base_price=400000)
		self.plan("CORP", fx_lock_policy="Fixed at Check In", rounding_step=1000)
		self.res = self.add("Reservation", "RES-1", property="Test Hotel", room="Test Hotel-101",
		                    room_type="Test Hotel-DLX", rate_plan="CORP", check_in_date="2026-12-01",
		                    check_out_date="2026-12-03", adults=2, children=0, extra_beds=0,
		                    company="Client Co", agreed_rate=474500, status="Confirmed",
		                    contract_tariff=json.dumps({"agreed_rate": 474500, "tariff": {
		                        "currency": "USD", "nightly": 130, "fx_rate": 3650,
		                        "fx_date": "2026-11-20", "property_currency": "UGX"}}),
		                    night_rates=[Doc(stay_date="2026-12-01", rate=474500, gst_rate=0),
		                                 Doc(stay_date="2026-12-02", rate=474500, gst_rate=0)])
		self.tariffs["CORP"] = self.tariff(fx_rate=3712.5, fx_date="2026-12-01", agreed_rate=482625)

	def test_check_in_relocks_a_fixed_at_check_in_contract_at_the_business_date(self):
		self.assertTrue(self.pricing.relock_contract_fx_at_check_in(self.res))
		form, = self.tariff_forms()
		self.assertEqual((form["pricing_event"], form["fx_date"], form["company"]),
		                 ("check_in", "2026-12-01", "Client Co"))
		self.assertEqual(self.res.agreed_rate, 483000)
		self.assertEqual(self.res.contract_tariff, {"agreed_rate": 483000, "tariff": {
			"currency": "USD", "nightly": 130.0, "fx_rate": 3712.5, "fx_date": "2026-12-01",
			"property_currency": "UGX"}})
		self.assertTrue(self.res.flags.kamra_fx_checkin_relock)

	def test_other_policies_and_home_currency_contracts_keep_the_booked_rate(self):
		for policy in ("Fixed at Reservation", "Current Date", "Fixed at Block"):
			with self.subTest(policy=policy):
				self.docs["Rate Plan", "CORP"].fx_lock_policy = policy
				self.assertFalse(self.pricing.relock_contract_fx_at_check_in(self.res))
		self.docs["Rate Plan", "CORP"].fx_lock_policy = "Fixed at Check In"
		self.res.contract_tariff = json.dumps({"agreed_rate": 400000, "tariff": {
			"currency": "UGX", "nightly": 400000, "fx_rate": 1, "fx_date": "2026-11-20",
			"property_currency": "UGX"}})
		self.assertFalse(self.pricing.relock_contract_fx_at_check_in(self.res))
		self.res.contract_tariff = None
		self.assertFalse(self.pricing.relock_contract_fx_at_check_in(self.res))
		self.assertEqual(self.bridge_calls, [])
		self.assertEqual(self.res.agreed_rate, 474500)
		self.assertFalse(self.res.flags.get("kamra_fx_checkin_relock"))

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

	def test_current_date_posts_the_contract_night_at_the_posting_business_date_rate(self):
		self.docs["Rate Plan", "CORP"].fx_lock_policy = "Current Date"
		self.docs["Property", "Test Hotel"].business_date = "2026-12-02"
		self.tariffs["CORP"] = self.tariff(fx_rate=3712.5, fx_date="2026-12-02", agreed_rate=482625)
		room = self.post_night()
		form, = self.tariff_forms()
		self.assertEqual((form["pricing_event"], form["fx_date"]), ("posting", "2026-12-02"))
		self.assertEqual((room["rate"], room["amount"]), (483000, 483000))  # USD 130 x 3,712.5, nearest 1,000
		self.assertIn("USD 130.00 at 3,712.5", room["description"])

	def test_current_date_backs_the_tax_out_of_an_inclusive_rate(self):
		self.docs["Rate Plan", "CORP"].fx_lock_policy = "Current Date"
		self.docs["Property", "Test Hotel"].rates_include_tax = 1
		self.room_tax = 18
		room = self.post_night()
		self.assertAlmostEqual(room["rate"], 483000 / 1.18, places=6)
		self.assertEqual(room["gst_rate"], 18)

	def test_other_policies_or_a_missing_posting_rate_bill_the_locked_night(self):
		room = self.post_night()
		self.assertEqual(room["rate"], 474500)
		self.assertEqual(self.bridge_calls, [])
		self.docs["Rate Plan", "CORP"].fx_lock_policy = "Current Date"
		self.tariffs["CORP"] = {"is_tariff": 1, "refusal": "HSP-TARIFF-003: No rate."}
		self.assertEqual(self.post_night()["rate"], 474500)

	def test_reference_currency_follows_the_property_calculation(self):
		self.assertEqual(self.folio.reference_currency(self.res), {
			"currency": "USD", "home_currency": "UGX", "calculation": "ACTUAL",
			"fx_rate": 3650.0, "fx_date": "2026-11-20"})
		self.assertEqual(self.bridge_calls, [])
		self.docs["Property", "Test Hotel"].reference_currency_calculation = "CURRENT"
		self.assertEqual(self.folio.reference_currency(self.res), {
			"currency": "USD", "home_currency": "UGX", "calculation": "CURRENT",
			"fx_rate": 3712.5, "fx_date": "2026-12-01"})
		form, = self.tariff_forms()
		self.assertEqual((form["pricing_event"], form["fx_date"]), ("posting", "2026-12-01"))
		self.tariffs["CORP"] = {"is_tariff": 1, "refusal": "HSP-TARIFF-003: No rate."}
		self.assertIsNone(self.folio.reference_currency(self.res))
		self.res.contract_tariff = None
		self.assertIsNone(self.folio.reference_currency(self.res))


class CheckInRelockTest(RateMatrixHarness):
	modules = ("kamra.pricing", "kamra.authz", "kamra.api")

	def extra_modules(self):
		checkin = ModuleType("kamra.checkin")
		checkin.assert_ready = lambda doc, room=None: None
		checkin.evaluate = lambda doc: ([], None)
		checkin.soft_pending = lambda steps: []
		checkin.after_check_in = lambda doc: None
		savings = ModuleType("kamra.savings")
		savings.log_action = lambda *args, **kwargs: None
		return {"kamra.checkin": checkin, "kamra.savings": savings}

	def setUp(self):
		super().setUp()
		self.roles = ["Front Desk"]
		self.authz.restricted_properties = lambda user=None: None
		self.add("Room", "Test Hotel-101", room_number="101")
		self.plan("CORP", fx_lock_policy="Fixed at Check In", rounding_step=1000)
		self.saved = []
		self.res = self.add("Reservation", "RES-1", property="Test Hotel", room="Test Hotel-101",
		                    room_type="Test Hotel-DLX", rate_plan="CORP", check_in_date="2026-12-01",
		                    check_out_date="2026-12-03", adults=2, children=0, agreed_rate=474500,
		                    status="Confirmed", guest_name="Contract Guest",
		                    contract_tariff={"agreed_rate": 474500, "tariff": {
		                        "currency": "USD", "nightly": 130, "fx_rate": 3650,
		                        "fx_date": "2026-11-20", "property_currency": "UGX"}})
		self.res.save = lambda: self.saved.append(SimpleNamespace(
			status=self.res.status, agreed_rate=self.res.agreed_rate,
			relocked=bool(self.res.flags.get("kamra_fx_checkin_relock")),
			fx_rate=self.res.contract_tariff["tariff"]["fx_rate"]))
		self.tariffs["CORP"] = self.tariff(fx_rate=3712.5, fx_date="2026-12-01", agreed_rate=482625)

	def test_check_in_relocks_the_contract_fx_before_the_stay_is_saved(self):
		self.api.check_in("RES-1")
		saved, = self.saved
		self.assertEqual((saved.status, saved.agreed_rate, saved.relocked, saved.fx_rate),
		                 ("Checked In", 483000, True, 3712.5))
		self.assertEqual(self.tariff_forms()[0]["pricing_event"], "check_in")

	def test_check_in_keeps_a_booking_locked_rate_under_other_policies(self):
		self.docs["Rate Plan", "CORP"].fx_lock_policy = "Fixed at Reservation"
		self.api.check_in("RES-1")
		saved, = self.saved
		self.assertEqual((saved.status, saved.agreed_rate, saved.relocked, saved.fx_rate),
		                 ("Checked In", 474500, False, 3650))
		self.assertEqual(self.bridge_calls, [])


class ReservationSnapshotTest(RateMatrixHarness):
	modules = ("kamra.pricing", "kamra.kamra.doctype.reservation.reservation")

	def snapshot(self, **flags):
		before = Doc(contract_tariff={"agreed_rate": 474500, "tariff": {"fx_rate": 3650}})
		doc = self.reservation.Reservation(
			contract_tariff={"agreed_rate": 483000, "tariff": {"fx_rate": 3712.5}}, flags=Doc(flags))
		doc.is_new = lambda: False
		doc.get_doc_before_save = lambda: before
		doc.capture_contract_tariff()
		return doc.contract_tariff

	def test_a_check_in_relock_survives_the_save(self):
		self.assertEqual(self.snapshot(kamra_fx_checkin_relock=True)["tariff"]["fx_rate"], 3712.5)

	def test_an_ordinary_save_keeps_the_booking_snapshot(self):
		self.assertEqual(self.snapshot()["tariff"]["fx_rate"], 3650)
