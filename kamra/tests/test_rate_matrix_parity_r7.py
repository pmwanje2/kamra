"""Round 7 of the rate matrix: the Gemini 4 Neon re-review (R6) of kamra#33.

P1-NEW-1 a fixed rate on a Current Date plan is fixed: its nights bill the
locked rate, never re-converted at the posting date's exchange rate.
P1-NEW-2 new dates on a contract stay re-price only the nights its frozen
contract does not cover, through the tariff resolver (OPERA re-prices added
nights from the rate code), and a night with no valid price refuses the new
dates. The nights still in the stay keep their locked rates; a manager's or
a fixed rate stays one price for every night.
P1-NEW-3 a base plan's currency cannot move away from the tariff price list
of a plan that inherits it.
P2 the resolver's scalar figures are its first night's, and "nights": None
is no per-night answer; a night may carry its own extra-person charge; extra
guests posted after their room line take that line's GST rate; a pro forma
heads its estimate with the rates its projected nights would post at.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import ClassVar
from unittest.mock import patch

from kamra.tests.rate_matrix_harness import Doc, RateMatrixHarness, ValidationError
from kamra.tests.test_rate_matrix_parity_r6 import (
	NIGHTS,
	STAY,
	PerNightContractStay,
	PostedLines,
	QuotePatches,
	locked,
	per_night_snapshot,
	stub_module,
)


def stay_nights(check_in, check_out):
	first, last = date.fromisoformat(str(check_in)), date.fromisoformat(str(check_out))
	return [str(first + timedelta(days=i)) for i in range(max((last - first).days, 1))]


class FixedRateCurrentDateTest(PostedLines, PerNightContractStay):
	"""P1-NEW-1: a fixed rate on a Current Date plan bills its locked night, not the posting date's FX."""

	policy = "Current Date"

	def test_a_fixed_rate_night_bills_its_locked_rate_whatever_the_posting_rate(self):
		self.res.fixed_rate = 1  # the manager's fixed rate in the home currency
		self.rate("USD", 4000, "2026-12-30")
		self.assertIsNone(self.folio.current_date_contract_night(self.res, "2026-12-30"))
		room = self.post_night("2026-12-30")["Room"]
		self.assertEqual(room["rate"], 365000)  # not USD 100 at 4,000
		self.assertNotIn("contract_fx_rate", room)
		self.assertNotIn("USD", room["description"])
		# after the renewal too: the locked night, not USD 200 at 4,000
		self.docs["Property", "Test Hotel"].business_date = "2027-01-01"
		self.rate("USD", 4000, "2027-01-01")
		self.assertEqual(self.post_night("2027-01-01")["Room"]["rate"], 730000)


class ContractStayAmendmentTest(QuotePatches, RateMatrixHarness):
	"""P1-NEW-2: new dates on a contract stay price the nights its contract never priced, as OPERA does."""

	modules = ("kamra.pricing", "kamra.kamra.doctype.reservation.reservation", "kamra.authz", "kamra.api")
	# the tariff as the resolver holds it today: USD 100 to 31 Dec, USD 200 from 1 Jan
	season: ClassVar[dict[str, tuple]] = {"2026": (100, 365000, "IP-2026"), "2027": (200, 730000, "IP-2027")}

	def extra_modules(self):
		return {"kamra.folio": stub_module("kamra.folio", _fnb_gst=lambda property: 5,
		                                   charged_nights=lambda name: {})}

	def setUp(self):
		super().setUp()
		self.roles = ["Front Desk"]
		self.authz.restricted_properties = lambda user=None: None
		self.add("Room Type", "Test Hotel-DLX", property="Test Hotel", base_price=100000, base_occupancy=2,
		         adults_capacity=4, free_child_age=6)
		self.plan("CORP")
		self.patch_quote()
		self.tariffs["CORP"] = self.resolver
		self.res = self.booked()

	def resolver(self, form):
		"""hospitality_tariffs.tariff_rate for the nights it is asked about: `nights` when they differ."""
		nights = []
		for day in stay_nights(form["check_in"], form["check_out"]):
			if day[:4] not in self.season:
				return {"is_tariff": 0}
			nightly, agreed, item_price = self.season[day[:4]]
			nights.append({"date": day, "nightly": nightly, "agreed_rate": agreed, "item_price": item_price})
		answer = self.tariff(nightly=nights[0]["nightly"], agreed_rate=nights[0]["agreed_rate"], fx_rate=3650,
		                     fx_date=form.get("fx_date") or "2026-12-20")
		if len({night["agreed_rate"] for night in nights}) > 1:
			answer["nights"] = nights
		return answer

	def booked(self, check_in=STAY[2], check_out=STAY[3], **fields):
		"""The saved booking: three nights over the renewal, each locked at its own contract rate."""
		values = dict(name="RES-1", property="Test Hotel", room_type="Test Hotel-DLX", rate_plan="CORP",
		              check_in_date=check_in, check_out_date=check_out, adults=2, children=0, extra_beds=0,
		              company="Client Co", auto_price=1, agreed_rate=365000, meal_plan=None, status="Confirmed",
		              amount_after_tax=1460000, contract_tariff=json.dumps(per_night_snapshot()),
		              night_rates=locked(365000, 365000, 730000))
		values.update(fields)
		before = Doc(values)
		doc = self.reservation.Reservation({**values, "flags": Doc()})
		doc.is_new = lambda: False
		doc.get_doc_before_save = lambda: before
		self.docs["Reservation", "RES-1"] = doc
		return doc

	def amend(self, check_in, check_out):
		"""What the save of new dates bills: capture_contract_tariff, then the re-quote apply_pricing locks."""
		self.res.update(check_in_date=check_in, check_out_date=check_out)
		self.res.capture_contract_tariff()
		return [(row["stay_date"], row["rate"]) for row in self.res.quote_night_rows(self.res.requote())]

	def snapshot(self):
		saved = self.res.contract_tariff
		return json.loads(saved) if isinstance(saved, str) else saved

	def contract_nights(self):
		return [(night["date"], night["nightly"], night["agreed_rate"])
		        for night in self.snapshot()["tariff"]["nights"]]

	def test_an_extension_past_the_renewal_prices_the_added_night_at_its_own_tariff(self):
		self.assertEqual(self.amend(STAY[2], "2027-01-03"), [
			("2026-12-30", 365000), ("2026-12-31", 365000), ("2027-01-01", 730000), ("2027-01-02", 730000)])
		# only the added night is asked for, at the booking's exchange-rate date
		form, = self.tariff_forms()
		self.assertEqual((form["check_in"], form["check_out"], form["fx_date"], form["company"]),
		                 ("2027-01-02", "2027-01-03", "2026-12-20", "Client Co"))
		self.assertEqual(self.contract_nights(), [
			("2026-12-30", 100, 365000), ("2026-12-31", 100, 365000), ("2027-01-01", 200, 730000),
			("2027-01-02", 200.0, 730000.0)])
		self.assertEqual(self.res.agreed_rate, 365000)

	def test_an_earlier_arrival_prices_its_new_night_at_today_s_tariff_and_keeps_the_rest(self):
		self.season = {**self.season, "2026": (90, 328500, "IP-2026-B")}  # the 2026 tariff revised since booking
		self.assertEqual(self.amend("2026-12-29", STAY[3]), [
			("2026-12-29", 328500), ("2026-12-30", 365000), ("2026-12-31", 365000), ("2027-01-01", 730000)])
		form, = self.tariff_forms()
		self.assertEqual((form["check_in"], form["check_out"]), ("2026-12-29", "2026-12-30"))

	def test_the_amendment_preview_prices_the_added_night_as_the_save_does(self):
		preview = self.api.amend_stay_preview("RES-1", STAY[2], "2027-01-03")
		self.assertEqual([(night["date"], night["rate"]) for night in preview["nights"]], [
			("2026-12-30", 365000), ("2026-12-31", 365000), ("2027-01-01", 730000), ("2027-01-02", 730000)])
		self.assertTrue(preview["needs_confirmation"])

	def test_an_added_night_with_no_valid_tariff_refuses_the_new_dates(self):
		# no tariff is valid after 2027-01-01: nothing silently bills last season's rate
		self.season = {"2026": (100, 365000, "IP-2026")}
		for case in ("save", "preview"):
			with self.subTest(case):
				self.res = self.booked()
				with self.assertRaisesRegex(ValidationError, "2027-01-02"):
					if case == "save":
						self.amend(STAY[2], "2027-01-04")
					else:
						self.api.amend_stay_preview("RES-1", STAY[2], "2027-01-04")
		# the resolver's own refusal reaches the desk as it gave it
		self.tariffs["CORP"] = lambda form: {"refusal": "The 2027 tariff is not approved yet."}
		self.res = self.booked()
		with self.assertRaisesRegex(self.pricing.ContractRateRefused, "not approved yet"):
			self.amend(STAY[2], "2027-01-03")

	def test_a_shorter_stay_keeps_each_remaining_night_s_locked_rate(self):
		self.assertEqual(self.amend("2026-12-31", STAY[3]), [("2026-12-31", 365000), ("2027-01-01", 730000)])
		self.assertEqual(self.tariff_forms(), [])
		# the nights that left the stay leave its contract: the subtotal adds up the stay's own nights
		self.assertEqual(self.contract_nights(), [("2026-12-31", 100, 365000), ("2027-01-01", 200, 730000)])

	def test_unchanged_dates_never_ask_the_resolver(self):
		self.assertEqual(self.amend(STAY[2], STAY[3]), [
			("2026-12-30", 365000), ("2026-12-31", 365000), ("2027-01-01", 730000)])
		self.assertEqual(self.tariff_forms(), [])
		self.assertEqual(self.snapshot(), per_night_snapshot())

	def test_a_one_rate_contract_extended_over_the_renewal_prices_the_new_nights_at_their_tariff(self):
		one_rate = json.dumps({"agreed_rate": 365000, "tariff": {
			"currency": "USD", "nightly": 100, "fx_rate": 3650, "fx_date": "2026-12-20", "property_currency": "UGX"}})
		self.res = self.booked("2026-12-29", "2026-12-31", contract_tariff=one_rate, night_rates=[
			Doc(stay_date="2026-12-29", rate=365000, gst_rate=0), Doc(stay_date="2026-12-30", rate=365000, gst_rate=0)])
		self.assertEqual(self.amend("2026-12-29", "2027-01-02"), [
			("2026-12-29", 365000), ("2026-12-30", 365000), ("2026-12-31", 365000), ("2027-01-01", 730000)])
		form, = self.tariff_forms()
		self.assertEqual((form["check_in"], form["check_out"]), ("2026-12-31", "2027-01-02"))
		self.assertEqual([night[2] for night in self.contract_nights()], [365000, 365000, 365000, 730000])
		# an extension the one rate still prices keeps the one-rate contract (and its rate-lock key)
		self.res = self.booked("2026-12-29", "2026-12-31", contract_tariff=one_rate)
		self.assertEqual([rate for _day, rate in self.amend("2026-12-29", "2027-01-01")], [365000] * 3)
		self.assertNotIn("nights", self.snapshot()["tariff"])

	def test_a_manager_s_rate_or_a_fixed_rate_stays_one_price_when_the_stay_grows(self):
		for fields, rate in (({"agreed_rate": 400000}, 400000), ({"fixed_rate": 1}, 365000)):
			with self.subTest(fields):
				self.bridge_calls.clear()
				self.res = self.booked(**fields)
				self.assertEqual([night_rate for _day, night_rate in self.amend(STAY[2], "2027-01-03")], [rate] * 4)
				self.assertEqual(self.tariff_forms(), [])


class DerivedPlanPriceListCurrencyTest(RateMatrixHarness):
	"""P1-NEW-3: a base plan's currency stays the currency of the tariff price list a derived plan inherits it for."""

	def setUp(self):
		super().setUp()
		self.controller = self.load("kamra.kamra.doctype.rate_plan.rate_plan")
		self.add("Price List", "Contract USD", currency="USD")
		harness_exists = self.frappe.db.exists
		patcher = patch.object(self.frappe.db, "exists",
		                       lambda doctype, name=None: name == "Price List" or harness_exists(doctype, name))
		patcher.start()
		self.addCleanup(patcher.stop)
		self.derived(currency="", custom_tariff_price_list="Contract USD")

	def derived(self, *more, **fields):
		self.records["Rate Plan"] = [Doc({"name": "Test Hotel-DER", "rate_plan_name": "Derived USD",
		                                  "base_rate_plan": "Test Hotel-BASE", "disabled": 0, **fields}), *more]

	def validate(self, **fields):
		values = {"name": "Test Hotel-BASE", "property": "Test Hotel", "rate_plan_name": "Base", "code": "BASE",
		          "modifier_type": "Percent", "modifier_value": 0, "fx_lock_policy": "Fixed at Reservation",
		          "rounding_step": 0, "rounding_mode": "Nearest", "currency": "USD", "disabled": 0}
		self.controller.RatePlan({**values, **fields}).validate()

	def test_a_base_plan_cannot_leave_the_currency_of_an_inheriting_plan_s_tariff_price_list(self):
		with self.assertRaisesRegex(ValidationError, "Contract USD"):
			self.validate(currency="EUR")
		with self.assertRaisesRegex(ValidationError, "Contract USD"):
			self.validate(currency=None)  # blank: the property's UGX
		self.validate()  # still USD

	def test_a_plan_further_down_the_chain_holds_the_base_too(self):
		self.derived(Doc(name="Test Hotel-DER2", rate_plan_name="Derived again", base_rate_plan="Test Hotel-DER",
		                 currency=None, disabled=0, custom_tariff_price_list="Contract USD"),
		             currency="", custom_tariff_price_list=None)
		with self.assertRaisesRegex(ValidationError, "Contract USD"):
			self.validate(currency="EUR")

	def test_a_derived_plan_with_no_tariff_price_list_follows_its_base(self):
		self.derived(currency="", custom_tariff_price_list=None)
		self.validate(currency="EUR")


class ResolverNightsContractTest(RateMatrixHarness):
	"""P2: the resolver's scalar figures are its first night's; "nights": None is no per-night answer."""

	def resolve(self, plan="CORP"):
		return self.pricing.contract_rate(*STAY[:2], plan, *STAY[2:])

	def test_scalar_figures_that_are_not_the_first_night_s_are_refused(self):
		self.plan("CORP")
		for case, first in (("another nightly", {"nightly": 200}), ("another agreed rate", {"agreed_rate": 730000}),
		                    ("another extra-person charge", {"extra_person": 40})):
			with self.subTest(case):
				self.tariffs["CORP"] = self.tariff(nightly=100, agreed_rate=365000, extra_person=20,
				                                   nights=[dict(NIGHTS[0], **first), *NIGHTS[1:]])
				with self.assertRaisesRegex(self.pricing.ContractRateRefused, "incomplete"):
					self.resolve()

	def test_nights_none_prices_the_stay_as_a_one_rate_contract(self):
		self.plan("CORP")
		self.tariffs["CORP"] = self.tariff(nightly=100, agreed_rate=365000, nights=None)
		agreed, tariff = self.resolve()
		self.assertEqual((agreed, tariff["nightly"]), (365000, 100.0))
		self.assertNotIn("nights", tariff)

	def test_a_derived_plan_keeps_each_night_s_own_extra_person_charge(self):
		self.plan("CON")
		self.plan("CON-NR", base_rate_plan="CON", modifier_type="Percent", modifier_value=-10, rounding_step=1000)
		nights = [dict(NIGHTS[0], extra_person=20), dict(NIGHTS[1], extra_person=20),
		          dict(NIGHTS[2], extra_person=40)]
		self.tariffs["CON"] = self.tariff(nightly=100, agreed_rate=365000, extra_person=20, nights=nights)
		_agreed, tariff = self.resolve("CON-NR")
		# USD 200 with USD 40 of extra persons: 160 less 10% = 144, plus 40 = 184 (not 182 off the stay's 20)
		self.assertEqual([(night["nightly"], night["agreed_rate"]) for night in tariff["nights"]],
		                 [(92.0, 336000.0), (92.0, 336000.0), (184.0, 672000.0)])
		# a night without its own keeps the stay's
		del nights[2]["extra_person"]
		_agreed, tariff = self.resolve("CON-NR")
		self.assertEqual(tariff["nights"][2]["nightly"], 182.0)
		for bad in (-5, "lots"):
			with self.subTest(bad):
				nights[2]["extra_person"] = bad
				with self.assertRaisesRegex(self.pricing.ContractRateRefused, "incomplete"):
					self.resolve("CON-NR")


class ExtraGuestsAfterTheRoomLineTest(PerNightContractStay):
	"""P2: extra guests posted after the night's room line take the GST rate that line posted at."""

	policy = "Current Date"

	def setUp(self):
		super().setUp()
		self.res.update(over_capacity_charge=50000, over_capacity_guests=1, night_rates=locked(
			365000, 365000, 730000, gst=12))
		room = dict(parenttype="Folio", parent="F-1", reservation="RES-1", charge_type="Room", auto_posted=1)
		self.records["Folio Charge"] = [Doc(room, name="FC-1", posting_date="2026-12-30", gst_rate=18, amount=380000)]

	def post_after_room(self, date):
		lines = []
		for name, value in (("_charge_posted", lambda reservation, charge_type, day: charge_type == "Room"),
		                    ("_append_charge", lambda folios, reservation, charge_type, line, *args: lines.append(line))):
			patcher = patch.object(self.folio, name, value)
			patcher.start()
			self.addCleanup(patcher.stop)
		self.folio.post_room_night(self.res, date, folios={})
		return {line["charge_type"]: line for line in lines}

	def test_extra_guests_posted_after_their_room_line_take_its_gst_rate(self):
		lines = self.post_after_room("2026-12-30")
		self.assertNotIn("Room", lines)
		self.assertEqual((lines["Extra Guests"]["amount"], lines["Extra Guests"]["gst_rate"]), (50000, 18))

	def test_a_voided_room_line_does_not_set_the_rate(self):
		room = dict(parenttype="Folio", parent="F-1", reservation="RES-1", charge_type="Room", auto_posted=1)
		self.records["Folio Charge"] += [
			Doc(room, name="FC-2", posting_date="2026-12-31", gst_rate=18, amount=-380000, reversal_of="FC-1"),
			Doc(room, name="FC-3", posting_date="2026-12-30", gst_rate=15, amount=365000)]
		self.assertEqual(self.post_after_room("2026-12-30")["Extra Guests"]["gst_rate"], 15)

	def test_with_no_room_line_found_the_locked_night_s_rate_still_applies(self):
		self.records["Folio Charge"] = []
		self.assertEqual(self.post_after_room("2026-12-30")["Extra Guests"]["gst_rate"], 12)


class ProformaHeaderRateTest(RateMatrixHarness):
	"""P2: a Current Date pro forma before the audit heads its estimate with its projected nights' rates."""

	modules = ("kamra.pricing", "kamra.folio")

	def setUp(self):
		super().setUp()
		self.plan("CORP", fx_lock_policy="Current Date")
		self.res = self.add("Reservation", "RES-1", property="Test Hotel", rate_plan="CORP",
		                    contract_tariff=json.dumps({"agreed_rate": 474500, "tariff": {
		                        "currency": "USD", "nightly": 130, "fx_rate": 3650, "fx_date": "2026-11-20",
		                        "property_currency": "UGX"}}))

	@staticmethod
	def projected(day, amount, rate, rate_date):
		return {"posting_date": day, "charge_type": "Room", "amount": amount, "gst_rate": 0, "gst_amount": 0,
		        "total": amount, "projected": True, "contract_currency": "USD", "contract_fx_rate": rate,
		        "contract_fx_date": rate_date}

	def test_the_header_rate_is_the_rates_the_projected_nights_would_post_at(self):
		lines = [self.projected("2026-12-01", 481000, 3700, "2026-12-01"),
		         self.projected("2026-12-02", 494000, 3800, "2026-12-01")]
		shown = self.folio.reference_currency(self.res, grand_total=975000, balance=975000, charges=lines,
		                                      payments=[])
		self.assertEqual((shown["fx_rate"], shown["fx_date"], shown["posted_nights"]), (3750.0, "2026-12-01", 2))
		self.assertEqual((shown["grand_total_reference"], shown["balance_reference"]), (260.0, 260.0))

	def test_posted_and_projected_nights_average_together(self):
		self.records["Folio Charge"] = [Doc(name="FC-1", parenttype="Folio", reservation="RES-1", charge_type="Room",
		                                    posting_date="2026-11-30", contract_currency="USD",
		                                    contract_fx_rate=3600, contract_fx_date="2026-11-30")]
		lines = [self.projected("2026-12-01", 481000, 3700, "2026-12-01"),
		         self.projected("2026-12-02", 494000, 3800, "2026-12-01")]
		shown = self.folio.reference_currency(self.res, charges=lines, payments=[])
		self.assertEqual((shown["fx_rate"], shown["fx_date"], shown["posted_nights"]), (3700.0, "2026-12-01", 3))

	def test_a_folio_with_no_projection_reads_its_posted_nights_alone(self):
		self.records["Folio Charge"] = [Doc(name="FC-1", parenttype="Folio", reservation="RES-1", charge_type="Room",
		                                    posting_date="2026-11-30", contract_currency="USD",
		                                    contract_fx_rate=3600, contract_fx_date="2026-11-30")]
		shown = self.folio.reference_currency(self.res, grand_total=360000, balance=0)
		self.assertEqual((shown["fx_rate"], shown["posted_nights"], shown["grand_total_reference"]),
		                 (3600.0, 1, 100.0))
