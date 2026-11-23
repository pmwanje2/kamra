"""Round 2 of the rate matrix (Gap Record #8556): exchange rates and rounding.

The check-in re-lock converts only the booked contract at the check-in
rate; a Current Date night converts at its own posting date's published
rate (ERPNext Currency Exchange), keeps a discount or override, and says
so when it has to fall back; bills show the reference currency with their
totals; card-rate plans round each night; derived plans add amounts per
night; a base plan's children follow its currency and enablement.

Round 3: ACTUAL leaves out nights void_charge reversed and keeps the rate
a night posted at when it is moved or split (counting a split night once);
an Absolute or Amount plan on a zero-priced room still prices each night;
the Night Audit Run records the amount its room nights actually posted."""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from types import ModuleType
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



# -- Round 3 (R2 review): the folio trail, zero-priced plans, the audit total --

_ROW_NAMES = iter(range(1, 10**6))


class Folio(Doc):
	"""A loaded Folio: an appended line becomes a named child row of it, as in Frappe."""

	def append(self, field, row):
		row = Doc(row, name=row.get("name") or f"FC-{next(_ROW_NAMES)}", parent=self.name,
		          parenttype="Folio", parentfield=field)
		self[field].append(row)
		return row

	def save(self, **kwargs):
		return self


def stub_module(name, **attrs):
	module = ModuleType(name)
	for key, value in attrs.items():
		setattr(module, key, value)
	return module


class CurrentDateTrailHarness(ContractStayHarness):
	"""A Current Date stay whose nights post, void, move and split through the real folio code."""

	policy = "Current Date"

	def extra_modules(self):
		self.ledger_calls = []

		def record(name):
			return lambda *args, **kwargs: self.ledger_calls.append(name)

		return {
			"kamra.ledger": stub_module("kamra.ledger", ledger_charges=record("ledger_charges"),
			                            reverse_ledger_for_charge=record("reverse_ledger_for_charge"),
			                            _original_charge_entry=lambda *args: None,
			                            code_for_charge_type=lambda charge_type: charge_type.upper(),
			                            record_charge_ledger=record("record_charge_ledger")),
			"kamra.webhooks": stub_module("kamra.webhooks", emit_charge_voided=lambda *args: None,
			                              emit_night_audit_closed=lambda *args: None),
			"kamra.savings": stub_module("kamra.savings", log_action=lambda *args, **kwargs: None),
		}

	def setUp(self):
		super().setUp()
		sys.modules["kamra.localization"].money_text = lambda property, amount: f"{amount:,}"
		self.docs["Property", "Test Hotel"].business_date = "2026-12-05"
		self.guest = self.folio_doc("F-GUEST", "Guest")
		self.company = self.folio_doc("F-CO", "Company")
		for name, value in (("_charge_posted", lambda *args: False), ("_post_room_levy", lambda *args: None),
		                    ("_append_charge", self.append_to_guest_folio)):
			patcher = patch.object(self.folio, name, value)
			patcher.start()
			self.addCleanup(patcher.stop)

	def folio_doc(self, name, folio_type):
		doc = Folio(doctype="Folio", name=name, property="Test Hotel", reservation="RES-1", folio_type=folio_type,
		            status="Open", charges=[], payments=[], flags=Doc())
		self.docs["Folio", name] = doc
		return doc

	def append_to_guest_folio(self, folios, reservation, charge_type, line, *args):
		self.guest.append("charges", line)

	def post(self, night, fx):
		self.exchange_rates[:] = [row for row in self.exchange_rates if row.date != night]
		self.rate("USD", fx, night)
		self.assertTrue(self.folio.post_room_night(self.res, night, folios={}))
		return self.guest.charges[-1]

	def shown(self):
		self.records["Folio Charge"] = [row for doc in (self.guest, self.company) for row in doc.charges]
		shown = self.folio.reference_currency(self.res, grand_total=0, balance=0)
		return shown["fx_rate"], shown["fx_date"], shown.get("posted_nights")

	@staticmethod
	def fx_trail(row):
		return (row.get("contract_currency"), row.get("contract_fx_rate"), str(row.get("contract_fx_date")),
		        row.get("contract_fx_stale"))


class PostedContractFxTrailTest(CurrentDateTrailHarness):
	"""P1-R2-1: ACTUAL reads the rates the stay's nights still stand at."""

	def test_a_night_voided_by_void_charge_leaves_the_average(self):
		self.post("2026-12-01", 3700)
		wrong = self.post("2026-12-02", 9999)  # Finance mis-published the rate; the night posted at it
		self.folio.void_charge(self.guest.name, wrong.name, "wrong exchange rate")
		reversal = self.guest.charges[-1]
		self.assertEqual(reversal.reversal_of, wrong.name)
		self.assertIsNone(reversal.get("contract_currency"))  # void_charge never copies the FX trail
		self.post("2026-12-02", 3800)  # the night again, at the corrected rate
		self.assertEqual(self.shown(), (3750.0, "2026-12-02", 2))

	def test_a_moved_night_keeps_the_rate_it_posted_at(self):
		self.post("2026-12-01", 3700)
		night = self.post("2026-12-02", 3800)
		moved, = self.folio.move_charges(self.guest, self.company, [night.name])
		self.assertEqual(self.fx_trail(moved), ("USD", 3800.0, "2026-12-02", 0))
		self.assertEqual(self.shown(), (3750.0, "2026-12-02", 2))

	def test_a_split_night_keeps_its_rate_and_counts_once(self):
		night = self.post("2026-12-01", 3700)
		self.post("2026-12-02", 3800)
		self.assertEqual(self.folio.split_charge(self.guest.name, night.name, self.company.name, percent=40),
		                 {"kept": 288600.0, "moved": 192400.0})
		part, = self.company.charges
		self.assertEqual(self.fx_trail(part), ("USD", 3700.0, "2026-12-01", 0))
		self.assertEqual(self.fx_trail(night), ("USD", 3700.0, "2026-12-01", 0))
		# two rows, one night: 3,700 is not weighted twice against 3,800
		self.assertEqual(self.shown(), (3750.0, "2026-12-02", 2))


class NightAuditPostedAmountTest(CurrentDateTrailHarness):
	"""P2-R2-3: the Night Audit Run records what the night actually posted."""

	def extra_modules(self):
		stubs = super().extra_modules()
		stubs["kamra.night_audit"] = stub_module(
			"kamra.night_audit", HOLD_AND_ALERT="Hold and alert", auto_extend_departures=lambda *args: [],
			run_checks=lambda *args: [], open_blockers=lambda checks: [], build_pack=lambda *args, **kwargs: {})
		stubs["kamra.no_show"] = stub_module(
			"kamra.no_show", ONE_DAY_LATER="One day later", policy=lambda property: Doc(timing="At audit"),
			property_now=lambda property: None)
		return stubs

	def setUp(self):
		super().setUp()
		self.docs["Property", "Test Hotel"].business_date = "2026-12-02"
		self.res.status = "Checked In"
		self.audits = []
		harness_get_all, harness_get_doc = self.frappe.get_all, self.frappe.get_doc

		def get_all(doctype, filters=None, **kwargs):
			if doctype == "Reservation":
				return [Doc(name="RES-1")] if (filters or {}).get("status") == "Checked In" else []
			return harness_get_all(doctype, filters, **kwargs)

		def get_doc(doctype, name=None):
			if isinstance(doctype, dict) and doctype.get("doctype") == "Night Audit Run":
				audit = Doc(doctype, name="NAR-1")
				audit.insert = lambda **kwargs: self.audits.append(audit)
				return audit
			return harness_get_doc(doctype, name)

		patches = (
			patch.object(self.frappe, "get_all", get_all), patch.object(self.frappe, "get_doc", get_doc),
			patch.object(self.frappe.db, "commit", lambda: None, create=True),
			patch.object(self.frappe.db, "set_value", lambda *args, **kwargs: None, create=True),
			patch.object(self.folio, "nowdate", lambda: "2026-12-03"),
			patch.object(self.folio, "open_folio", lambda reservation: self.guest.name),
			patch.object(self.folio, "save_folios", lambda folios: None),
		)
		for patcher in patches:
			patcher.start()
			self.addCleanup(patcher.stop)

	def test_amount_posted_is_the_current_date_night_not_the_booked_one(self):
		self.rate("USD", 3712.5, "2026-12-02")
		out = self.folio.run_night_audit("Test Hotel")
		room, = self.guest.charges
		self.assertEqual(room.amount, 483000)  # USD 130 x 3,712.5, nearest 1,000 - not the booked 474,500
		self.assertEqual((out["room_charges_posted"], out["amount_posted"]), (1, 483000.0))
		audit, = self.audits
		self.assertEqual(audit.amount_posted, 483000.0)


class ZeroPricedRoomPlanTest(CardStayHarness):
	"""P2-R2-2: an Absolute or Amount plan on a room with no card price still prices the stay."""

	modules = ("kamra.pricing", "kamra.folio", "kamra.kamra.doctype.reservation.reservation")

	def setUp(self):
		super().setUp()
		self.docs["Room Type", "Test Hotel-DLX"].update(base_price=0, extra_adult_price=0, child_price=0)

	def book(self, quoted):
		return [row["rate"] for row in self.reservation.Reservation(meal_plan=None, flags=Doc()).quote_night_rows(quoted)]

	def test_an_absolute_plan_spreads_its_price_over_the_nights_and_rounds_each(self):
		self.plan("PKG", modifier_type="Absolute", modifier_value=300000, rounding_step=1000)
		quoted = self.quote("PKG", adults=2, children=0, check_out="2026-12-04")
		self.assertEqual([night["planned_rate"] for night in quoted["nightly"]], [100000, 100000, 100000])
		self.assertEqual(quoted["room_total"], 300000)
		self.assertEqual(self.book(quoted), [100000, 100000, 100000])

	def test_an_unrounded_plan_spreads_its_price_and_the_nights_add_up(self):
		self.plan("PKG", modifier_type="Absolute", modifier_value=1000000)
		quoted = self.quote("PKG", adults=2, children=0, check_out="2026-12-04")
		self.assertEqual(quoted["room_total"], 1000000)
		nights = self.book(quoted)
		self.assertEqual(len(nights), 3)
		self.assertAlmostEqual(sum(nights), 1000000, places=2)
		self.assertTrue(all(night > 0 for night in nights), nights)

	def test_a_root_amount_plan_on_a_zero_priced_room_bills_its_amount(self):
		self.plan("FEE", modifier_type="Amount", modifier_value=50000)
		quoted = self.quote("FEE", adults=2, children=0)
		self.assertEqual(quoted["room_total"], 50000)
		self.assertAlmostEqual(sum(self.book(quoted)), 50000, places=2)
		# a stay booked before nights were locked posts the same share
		unlocked = self.add("Reservation", "RES-3", property="Test Hotel", room_type="Test Hotel-DLX",
		                    rate_plan="FEE", check_in_date="2026-12-01", check_out_date="2026-12-03", adults=2,
		                    children=0, night_rates=[])
		self.assertAlmostEqual(self.folio._nightly_room_rate(unlocked, "2026-12-01"), 25000, places=2)

	def test_a_tax_inclusive_rounded_spread_books_nights_that_add_up_to_the_cent(self):
		# P2-R3: 18% inclusive, 250,000 over three nights rounded to 1,000 gross. Each night backs
		# out to 98,000 / 1.18 = 83,050.847...; the last night must absorb the sub-cent remainder
		self.docs["Property", "Test Hotel"].update(rates_include_tax=1)
		self.room_tax = 18
		self.plan("PKG", modifier_type="Absolute", modifier_value=250000, rounding_step=1000)
		quoted = self.quote("PKG", adults=2, children=0, check_out="2026-12-04")
		self.assertTrue(all("planned_rate" in night for night in quoted["nightly"]), quoted["nightly"])
		self.assertEqual(round(quoted["room_total"], 2), 249152.54)
		nights = self.book(quoted)
		self.assertEqual(nights, [83050.85, 83050.85, 83050.84])
		self.assertEqual(round(sum(nights), 2), round(quoted["room_total"], 2))
