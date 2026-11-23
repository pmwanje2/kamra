"""Channel rates in the currency of the mapped channel rate plan; Channex never converts.

OPERA Cloud sends a channel the rate code's own currency, or the channel's
configured currency converted at the property's rate. SAP replicates a
price in its condition currency, or converts it into a fixed-currency
channel at the governed rate of the pricing date. Channex can convert a
rate itself, but at its own exchange rate, which nobody governs, so Kamra
never lets it. H is the property currency, C the rate plan's contract
currency and E the currency of the mapped Channex rate plan.

Outbound: E == C sends the contract nightly, E == H the property-currency
agreed rate the desk books, anything else nothing at all. A missing tariff
still stops sale; a missing exchange rate holds the night instead.

Inbound: an OTA booking in C converts the OTA's own nights into H at
booking. An OTA-collected booking keeps that rate; a property-collected one
follows the rate plan's exchange-rate lock, as a desk booking does."""

from __future__ import annotations

import json
from decimal import Decimal
from types import ModuleType
from unittest.mock import patch

from kamra.tests.rate_matrix_harness import Doc, RateMatrixHarness

STD = "Test Hotel-STD"
NIGHT, NEXT = "2026-12-05", "2026-12-06"
CARD = Decimal(100000)
FX_REFUSAL = "HSP-TARIFF-003: No single Currency Exchange rate for the business date."
NO_TARIFF = "HSP-TARIFF-002: No valid Item Price for this date."


class OutboundCurrencyHarness(RateMatrixHarness):
	"""A UGX hotel: CONTRACT (USD 130 at 3,648.2, rounded to 1,000), DERIVED
	(CONTRACT less 10%), CARD (a home plan priced from the card) and CARD-USD
	(a USD plan with no tariff, so priced from the UGX card)."""

	modules = ("kamra.pricing", "kamra.authz", "kamra.channel_ari")

	def extra_modules(self):
		manager = ModuleType("kamra.channel_manager")
		manager._mappings = lambda connection: [
			m for m in self.records.get("Channel Room Mapping", []) if m.connection == connection]
		return {"kamra.channel_manager": manager}

	def setUp(self):
		super().setUp()
		self.add("Room Type", STD, property="Test Hotel", base_occupancy=2)
		self.plan("CONTRACT", currency="USD", rounding_step=1000)
		self.plan("DERIVED", base_rate_plan="CONTRACT", modifier_type="Percent", modifier_value=-10,
		          rounding_step=1000)
		self.plan("CARD")
		self.plan("CARD-USD", currency="USD")
		self.tariffs["CONTRACT"] = self.tariff(fx_rate=3648.2, agreed_rate=474266)
		quote = patch.object(self.pricing, "quote", self.card_quote)
		quote.start()
		self.addCleanup(quote.stop)
		self.conn = Doc(name="CMC-1", property="Test Hotel")

	@staticmethod
	def card_quote(property, room_type, check_in_date, check_out_date, adults=2, children=0, meal_plan=None,
	               rate_plan=None, voucher_code=None, agreed_rate=0, **kwargs):
		"""The quote, as far as the channel reads it: the agreed rate when the
		contract priced the night, else the UGX card."""
		total = float(agreed_rate or CARD)
		return {"room_total": total, "nightly": [{"date": str(check_in_date), "rate": total}]}

	def night(self, plan, currency):
		return self.channel_ari._plan_night_rate("Test Hotel", STD, plan, self.utils.getdate(NIGHT), currency)


class OutboundCurrencyTest(OutboundCurrencyHarness):
	def test_a_plan_sold_in_its_contract_currency_sends_the_contract_nightly(self):
		self.assertEqual(self.night("CONTRACT", "USD"), Decimal("130.00"))
		# a derived plan's modifier is already in its contract amount: USD 130 less 10%
		self.assertEqual(self.night("DERIVED", "USD"), Decimal("117.00"))

	def test_a_plan_sold_in_the_property_currency_sends_the_agreed_rate_the_desk_books(self):
		# USD 130 x 3,648.2 = 474,266, to the plan's 1,000 step
		self.assertEqual(self.night("CONTRACT", "UGX"), Decimal("474000.00"))
		# USD 117 x 3,648.2 = 426,839.4, to DERIVED's 1,000 step
		self.assertEqual(self.night("DERIVED", "UGX"), Decimal("427000.00"))
		agreed, _tariff = self.pricing.contract_rate("Test Hotel", STD, "DERIVED", NIGHT, NEXT, 2, 0)
		self.assertEqual(agreed, 427000)

	def test_any_other_currency_or_none_on_a_foreign_plan_is_refused(self):
		for plan, currency in (("CONTRACT", "EUR"), ("CONTRACT", None), ("DERIVED", "EUR"),
		                       ("CARD", "USD"), ("CARD-USD", "USD"), ("CARD-USD", None)):
			with self.subTest(plan=plan, currency=currency):
				with self.assertRaises(self.channel_ari.AriCurrencyRefused):
					self.night(plan, currency)

	def test_a_card_priced_plan_is_sent_only_in_the_property_currency(self):
		self.assertEqual(self.night("CARD", "UGX"), CARD)
		self.assertEqual(self.night("CARD-USD", "UGX"), CARD)
		self.assertEqual(self.night(None, "UGX"), CARD)
		# a home plan whose channel currency is not known yet sells as it always has
		self.assertEqual(self.night("CARD", None), CARD)

	def test_a_missing_exchange_rate_holds_the_night_and_a_missing_tariff_stops_sale(self):
		self.tariffs["CONTRACT"] = {"is_tariff": 1, "refusal": FX_REFUSAL}
		for currency in ("USD", "UGX"):
			with self.subTest(currency=currency):
				with self.assertRaisesRegex(self.channel_ari.AriPriceHeld, "HSP-TARIFF-003"):
					self.night("CONTRACT", currency)
		self.tariffs["CONTRACT"] = {"is_tariff": 1, "refusal": NO_TARIFF}
		self.assertIsNone(self.night("CONTRACT", "USD"))


class RestrictionCellsCurrencyTest(OutboundCurrencyHarness):
	def map(self, rate_id, plan, currency):
		self.records.setdefault("Channel Room Mapping", []).append(Doc(
			connection="CMC-1", room_type=STD, external_room_id="std", external_rate_id=rate_id,
			rate_plan=plan, external_currency=currency))

	def cells(self):
		problems: dict = {}
		return self.channel_ari.restriction_cells(self.conn, NIGHT, NEXT, problems), problems

	def test_each_plan_goes_in_its_channel_currency_and_a_mismatch_is_left_out(self):
		self.map("rp-usd", "CONTRACT", "USD")
		self.map("rp-ugx", "CONTRACT", "UGX")
		self.map("rp-eur", "CONTRACT", "EUR")
		self.map("rp-bar", None, None)
		cells, problems = self.cells()
		self.assertEqual(sorted(cells), ["rp-bar", "rp-ugx", "rp-usd"])
		self.assertEqual(cells["rp-usd"][NIGHT], {"rate": Decimal("130.00"), "stop_sell": False})
		self.assertEqual(cells["rp-ugx"][NIGHT], {"rate": Decimal("474000.00"), "stop_sell": False})
		self.assertEqual(cells["rp-bar"][NEXT], {"rate": CARD, "stop_sell": False})
		self.assertEqual(sorted(problems["refused"]), ["rp-eur"])
		for code in ("EUR", "USD", "UGX"):
			self.assertIn(code, problems["refused"]["rp-eur"])
		self.assertFalse(problems.get("held"))

	def test_a_missing_exchange_rate_sends_nothing_for_the_night_instead_of_a_stop_sell(self):
		self.map("rp-usd", "CONTRACT", "USD")
		self.map("rp-ugx", "CONTRACT", "UGX")
		self.map("rp-bar", None, None)
		priced = self.tariff(fx_rate=3648.2, agreed_rate=474266)
		self.tariffs["CONTRACT"] = lambda form: (
			{"is_tariff": 1, "refusal": FX_REFUSAL} if form["check_in"] == NIGHT else priced)
		cells, problems = self.cells()
		self.assertEqual(cells["rp-usd"], {NEXT: {"rate": Decimal("130.00"), "stop_sell": False}})
		self.assertEqual(cells["rp-ugx"], {NEXT: {"rate": Decimal("474000.00"), "stop_sell": False}})
		self.assertEqual(cells["rp-bar"][NIGHT], {"rate": CARD, "stop_sell": False})
		self.assertEqual(sorted(problems["held"]), ["rp-ugx", "rp-usd"])
		self.assertEqual(problems["held"]["rp-usd"]["dates"], [NIGHT])
		self.assertIn("HSP-TARIFF-003", problems["held"]["rp-usd"]["reason"])
		self.assertFalse(problems.get("refused"))

	def test_each_date_goes_at_its_own_contract_night(self):
		"""Guard (R6, OPERA Rate Changes During Stay): every pushed date is its
		own one-night contract quote, so a tariff change inside the window
		shows on the day it starts - never one figure for the horizon."""
		self.map("rp-usd", "CONTRACT", "USD")
		self.map("rp-ugx", "CONTRACT", "UGX")
		first = self.tariff(fx_rate=3648.2, agreed_rate=474266)
		second = self.tariff(nightly=140, fx_rate=3648.2, agreed_rate=510748)
		self.tariffs["CONTRACT"] = lambda form: dict(
			first if form["check_in"] == NIGHT else second,
			nights=[{"date": form["check_in"], "nightly": (first if form["check_in"] == NIGHT else second)["nightly"],
			         "agreed_rate": (first if form["check_in"] == NIGHT else second)["agreed_rate"],
			         "item_price": "IP-" + form["check_in"]}])
		cells, _problems = self.cells()
		self.assertEqual({d: v["rate"] for d, v in cells["rp-usd"].items()},
		                 {NIGHT: Decimal("130.00"), NEXT: Decimal("140.00")})
		# 510,748 to the plan's 1,000 step
		self.assertEqual({d: v["rate"] for d, v in cells["rp-ugx"].items()},
		                 {NIGHT: Decimal("474000.00"), NEXT: Decimal("511000.00")})

	def test_a_missing_tariff_still_stops_sale(self):
		self.map("rp-usd", "CONTRACT", "USD")
		self.tariffs["CONTRACT"] = {"is_tariff": 1, "refusal": NO_TARIFF}
		cells, problems = self.cells()
		self.assertEqual(cells["rp-usd"], {NIGHT: {"stop_sell": True}, NEXT: {"stop_sell": True}})
		self.assertFalse(problems.get("held"))
		self.assertFalse(problems.get("refused"))


# An OTA booking in USD on CORP, booked at 3,650: the OTA sold the first night
# at USD 130 and the second at USD 120 (its own discount), UGX 475,000 and
# 438,000 locked. The snapshot is the contract's per-night shape (R6): each
# night's own amounts in `nights`, the scalar figures the check-in night's.
CHANNEL = {"agreed_rate": 475000, "tariff": {
	"currency": "USD", "nightly": 130, "fx_rate": 3650, "fx_date": "2026-11-20", "property_currency": "UGX",
	"source": "channel", "nights": [{"date": "2026-12-01", "nightly": 130, "agreed_rate": 475000},
	                                {"date": "2026-12-02", "nightly": 120, "agreed_rate": 438000}]}}


class ChannelBookingFxLockTest(RateMatrixHarness):
	modules = ("kamra.pricing", "kamra.folio")

	def extra_modules(self):
		manager = ModuleType("kamra.channel_manager")
		manager.lock_channel_rates = lambda doc, total, nightly=None: self.locked.append(
			(float(total), [(n["date"], float(n["amount"])) for n in nightly or []]))
		return {"kamra.channel_manager": manager}

	def setUp(self):
		self.locked = []
		super().setUp()
		self.add("Room Type", "Test Hotel-DLX", property="Test Hotel", base_price=400000)
		self.plan("CORP", currency="USD", fx_lock_policy="Fixed at Check In", rounding_step=1000)
		self.res = self.add(
			"Reservation", "RES-OTA", property="Test Hotel", room="Test Hotel-101", room_type="Test Hotel-DLX",
			rate_plan="CORP", check_in_date="2026-12-01", check_out_date="2026-12-03", adults=2, children=0,
			extra_beds=0, agreed_rate=475000, auto_price=0, status="Confirmed", source="OTA",
			amount_after_tax=913000, contract_tariff=json.dumps(CHANNEL),
			night_rates=[Doc(stay_date="2026-12-01", rate=475000, gst_rate=0),
			             Doc(stay_date="2026-12-02", rate=438000, gst_rate=0)])
		self.rate("USD", 3712.5, "2026-12-01")

	def ota_collected(self):
		self.res.contract_tariff = json.dumps({**CHANNEL, "fx_lock_policy": "Fixed at Reservation"})

	def test_an_ota_collected_booking_keeps_its_booking_rate(self):
		self.ota_collected()
		self.assertEqual(self.pricing.reservation_fx_policy(self.res), "Fixed at Reservation")
		self.assertFalse(self.pricing.relock_contract_fx_at_check_in(self.res))
		self.assertEqual(self.locked, [])
		self.assertEqual(self.res.agreed_rate, 475000)
		self.docs["Rate Plan", "CORP"].fx_lock_policy = "Current Date"
		self.assertIsNone(self.folio.current_date_contract_night(self.res, "2026-12-01"))

	def test_a_desk_booking_still_follows_its_rate_plan(self):
		self.res.contract_tariff = json.dumps({"agreed_rate": 474500, "tariff": {
			"currency": "USD", "nightly": 130, "fx_rate": 3650, "fx_date": "2026-11-20",
			"property_currency": "UGX"}})
		self.assertEqual(self.pricing.reservation_fx_policy(self.res), "Fixed at Check In")
		self.docs["Rate Plan", "CORP"].fx_lock_policy = "Current Date"
		self.assertEqual(self.pricing.reservation_fx_policy(self.res), "Current Date")

	def test_a_property_collected_booking_converts_each_ota_night_again_at_check_in(self):
		self.assertEqual(self.pricing.reservation_fx_policy(self.res), "Fixed at Check In")
		self.assertTrue(self.pricing.relock_contract_fx_at_check_in(self.res))
		self.assertEqual(self.tariff_forms(), [])  # the OTA's price, never the Item Price
		# USD 130 x 3,712.5 = 482,625 and USD 120 x 3,712.5 = 445,500, to the plan's 1,000 step
		self.assertEqual(self.locked, [(929000.0, [("2026-12-01", 483000.0), ("2026-12-02", 446000.0)])])
		self.assertEqual(self.res.amount_after_tax, 929000)
		tariff = self.res.contract_tariff["tariff"]
		self.assertEqual((tariff["fx_rate"], tariff["fx_date"]), (3712.5, "2026-12-01"))
		self.assertEqual([(n["date"], n["nightly"], n["agreed_rate"]) for n in tariff["nights"]],
		                 [("2026-12-01", 130, 483000), ("2026-12-02", 120, 446000)])
		self.assertEqual(self.res.agreed_rate, 483000)  # the check-in night's

	def test_a_property_collected_booking_on_a_current_date_plan_posts_each_ota_night_at_its_date(self):
		self.docs["Rate Plan", "CORP"].fx_lock_policy = "Current Date"
		self.docs["Property", "Test Hotel"].business_date = "2026-12-02"
		self.rate("USD", 3712.5, "2026-12-02")
		night = self.folio.current_date_contract_night(self.res, "2026-12-02")
		# the OTA's own USD 120 for that night, at that night's rate: 445,500 to 446,000
		self.assertEqual((night["rate"], night["currency"], night["fx_rate"]), (446000.0, "USD", 3712.5))
		self.assertFalse(night["stale"])
		self.assertIn("USD 120", night["label"])
