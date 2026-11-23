"""Channel rates in the currency of the mapped channel rate plan; Channex never converts.

OPERA Cloud sends a channel the rate code's own currency, or the channel's
configured currency converted at the property's rate. SAP replicates a
price in its condition currency, or converts it into a fixed-currency
channel at the governed rate of the pricing date. Channex can convert a
rate itself, but at its own exchange rate, which nobody governs, so Kamra
never lets it. H is the property currency, C the rate plan's contract
currency and E the currency of the mapped Channex rate plan.

Outbound: E == C sends the contract nightly, E == H the property-currency
agreed rate the desk books, anything else - an unknown E included, for every
plan - nothing at all. E == C converts nothing, so it is read from the
approved tariff master data (hospitality_tariffs.list_tariffs, SAP's
condition-record replicate), never from a quote: a tariff quote that needs
an exchange rate refuses without one (SAP SG 105, HSP-TARIFF-003), and only
E == H needs one. Only a night with no approved price (a missing tariff) or
no price above zero stops sale. A missing exchange rate, and any other error
while pricing (a permission refusal, the database, the bridge), holds the
night at what the channel last accepted instead.

Inbound: an OTA booking in C converts the OTA's own nights into H at
booking. A booking the OTA explicitly collects (payment_collect "ota") or
pays by virtual card keeps that rate; any other (Channex: null means the
property collects) follows the rate plan's exchange-rate lock, as a desk
booking does."""

from __future__ import annotations

import copy
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
STALE_FX = ("HSP-TARIFF-003: No single Currency Exchange.exchange_rate valid for USD on 2026-12-05; Finance "
            "enters the current published rate.")
BUSINESS = "2026-12-01"


def version(name, rate, valid_from="2026-11-01", valid_upto=None, state="Approved"):
	"""An Item Price version as hospitality_tariffs.list_tariffs lists it."""
	return {"name": name, "rate": rate, "valid_from": valid_from, "valid_upto": valid_upto, "workflow_state": state,
	        "local_equiv": None}


def tariff_row(item_code, *versions, room_type="Standard", tier=2, category="Room", occupancy=None, customer=None,
               price_list="Contract USD", currency="USD"):
	"""A directory row as list_tariffs builds it: the Approved version in force
	on the business date is `active`, drafts are `pending`, every other
	version (Approved or Rejected) is `history`. `versions` go newest
	valid_from first, as the directory orders them."""
	active = next((v for v in versions if v["workflow_state"] == "Approved" and (v["valid_from"] or "") <= BUSINESS
	               and (not v["valid_upto"] or v["valid_upto"] >= BUSINESS)), None)
	return {"price_list": price_list, "currency": currency, "fx_rate": None, "fx_date": None, "customer": customer,
	        "item_code": item_code, "item_name": item_code, "category": category, "room_type": room_type,
	        "occupancy": occupancy or ({1: "Single", 2: "Double", 3: "Triple"}.get(tier) if category == "Room" else None),
	        "occupancy_tier": tier if category == "Room" else None, "active": active,
	        "pending": [v for v in versions if v["workflow_state"] == "Draft"],
	        "history": [v for v in versions if v is not active and v["workflow_state"] != "Draft"]}


def directory(*rows, plans=("CONTRACT",), price_list="Contract USD", currency="USD"):
	"""hospitality_tariffs.list_tariffs for the property: one price list
	pricing `plans`, with no usable exchange rate (fx_rate None), and `rows`."""
	return {"supported": True, "property": "Test Hotel", "property_currency": "UGX", "business_date": BUSINESS,
	        "as_of_date": BUSINESS, "can_propose": False, "can_approve": False,
	        "price_lists": [{"name": price_list, "currency": currency, "fx_rate": None, "fx_date": None,
	                         "fx_source": "Currency Exchange", "fx_stale_days": 1, "rate_plans": list(plans),
	                         "rate_plan_details": [{"name": plan, "rate_plan_name": plan, "disabled": 0,
	                                                "fx_lock_policy": "Fixed at Reservation", "rounding_step": 1000,
	                                                "rounding_mode": "Nearest"} for plan in plans]}],
	        "items": list(rows)}


class OutboundCurrencyHarness(RateMatrixHarness):
	"""A UGX hotel: CONTRACT (USD 130 at 3,648.2, rounded to 1,000), DERIVED
	(CONTRACT less 10%), CARD (a home plan priced from the card) and CARD-USD
	(a USD plan with no tariff, so priced from the UGX card). The tariff
	directory lists CONTRACT's approved USD 130 for Standard at two adults,
	with no usable exchange rate."""

	modules = ("kamra.pricing", "kamra.authz", "kamra.channel_ari")

	def extra_modules(self):
		manager = ModuleType("kamra.channel_manager")
		manager._mappings = lambda connection: [
			m for m in self.records.get("Channel Room Mapping", []) if m.connection == connection]
		return {"kamra.channel_manager": manager}

	def setUp(self):
		super().setUp()
		self.add("Room Type", STD, property="Test Hotel", room_type_name="Standard", base_occupancy=2,
		         adults_capacity=2)
		self.plan("CONTRACT", currency="USD", rounding_step=1000)
		self.plan("DERIVED", base_rate_plan="CONTRACT", modifier_type="Percent", modifier_value=-10,
		          rounding_step=1000)
		self.plan("CARD")
		self.plan("CARD-USD", currency="USD")
		self.tariffs["CONTRACT"] = self.tariff(fx_rate=3648.2, agreed_rate=474266)
		self.listing = directory(tariff_row("STD-2A", version("IP-130", 130)))
		quote = patch.object(self.pricing, "quote", self.card_quote)
		quote.start()
		self.addCleanup(quote.stop)
		self.conn = Doc(name="CMC-1", property="Test Hotel")

	def bridge_call(self, api_method, *, response_types=None, **form):
		"""The harness's tariff_rate, and the tariff directory (`self.listing`,
		or what it answers for the form)."""
		if api_method != "hospitality_tariffs.list_tariffs":
			return super().bridge_call(api_method, response_types=response_types, **form)
		self.bridge_calls.append((api_method, dict(form)))
		return copy.deepcopy(self.listing(form) if callable(self.listing) else self.listing)

	def directory_forms(self):
		return [form for method, form in self.bridge_calls if method == "hospitality_tariffs.list_tariffs"]

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

	def test_any_other_or_unknown_currency_is_refused_for_every_plan(self):
		"""An unknown channel currency fails closed for a home or card plan too
		(Neon R1 P0-2): UGX must never go into a channel rate plan that may
		sell in USD."""
		for plan, currency in (("CONTRACT", "EUR"), ("CONTRACT", None), ("DERIVED", "EUR"),
		                       ("CARD", "USD"), ("CARD-USD", "USD"), ("CARD-USD", None),
		                       ("CARD", None), (None, None)):
			with self.subTest(plan=plan, currency=currency):
				with self.assertRaises(self.channel_ari.AriCurrencyRefused):
					self.night(plan, currency)

	def test_a_card_priced_plan_is_sent_only_in_the_property_currency(self):
		self.assertEqual(self.night("CARD", "UGX"), CARD)
		self.assertEqual(self.night("CARD-USD", "UGX"), CARD)
		self.assertEqual(self.night(None, "UGX"), CARD)

	def test_a_missing_exchange_rate_holds_only_the_property_currency_night(self):
		"""SAP SG 105 (Paul's decision on siteops#8559): only the night that
		converts needs the exchange rate. E == H holds without it; E == C,
		read from the tariff directory, goes at the contract nightly."""
		for refusal in (FX_REFUSAL, STALE_FX):
			with self.subTest(refusal=refusal[:40]):
				self.tariffs["CONTRACT"] = {"is_tariff": 1, "refusal": refusal}
				for plan in ("CONTRACT", "DERIVED"):
					with self.assertRaisesRegex(self.channel_ari.AriPriceHeld, "HSP-TARIFF-003"):
						self.night(plan, "UGX")
				self.assertEqual(self.night("CONTRACT", "USD"), Decimal("130.00"))
				self.assertEqual(self.night("DERIVED", "USD"), Decimal("117.00"))

	def test_a_missing_tariff_stops_sale_in_either_currency(self):
		self.tariffs["CONTRACT"] = {"is_tariff": 1, "refusal": NO_TARIFF}
		self.assertIsNone(self.night("CONTRACT", "UGX"))
		# the quote would price it; the directory has no approved version of the room line
		self.tariffs["CONTRACT"] = self.tariff(fx_rate=3648.2, agreed_rate=474266)
		self.listing = directory(tariff_row("STD-2A", version("IP-D", 130, state="Draft")))
		self.assertIsNone(self.night("CONTRACT", "USD"))

	def test_an_unexpected_error_while_pricing_holds_the_night_and_never_stops_sale(self):
		"""Neon R1 P0-1: Frappe's PermissionError (an Exception, not a
		ValidationError), a database or a bridge failure, even a
		ValidationError the engine did not mean as a refusal (InReadOnlyMode
		is one), is not a reason to take the plan off sale."""
		for error in (FramePermissionError("Tariff prices are for hotel staff."), RuntimeError("Lost connection"),
		              self.frappe.ValidationError("read only")):
			with self.subTest(error=type(error).__name__):
				def refuse(form, error=error):
					raise error
				self.tariffs["CONTRACT"] = refuse  # the quote, for E == H
				self.listing = refuse  # the tariff directory, for E == C
				for currency in ("USD", "UGX"):
					with self.assertRaisesRegex(self.channel_ari.AriPricingFailed, type(error).__name__):
						self.night("CONTRACT", currency)

	def test_no_price_above_zero_still_stops_sale(self):
		with patch.object(self.pricing, "quote", lambda *args, **kwargs: {"room_total": 0}):
			self.assertIsNone(self.night("CARD", "UGX"))


class ContractCurrencyDirectoryTest(OutboundCurrencyHarness):
	"""E == C from the tariff master data, never from a quote (Paul's SAP
	decision on siteops#8559): SAP replicates a condition record to a
	channel in its own currency and converts nothing, so it needs no
	exchange rate, while a tariff quote that needs one fails closed with
	HSP-TARIFF-003 (SG 105). The night is the general Approved Item Price in
	force on it, priced for the room type's base occupancy as tariff_rate
	prices it, and a derived plan's modifier applies to it as contract_rate
	applies it - so the figure is the one the quote gives when the rate
	exists."""

	def usd(self, plan, room_type=STD, night=NIGHT):
		return self.channel_ari._plan_night_rate("Test Hotel", room_type, plan, self.utils.getdate(night), "USD")

	def test_a_contract_currency_night_needs_no_exchange_rate_and_never_asks_the_quote(self):
		for case, answer in (("published", self.tariff(fx_rate=3648.2, agreed_rate=474266)),
		                     ("missing", {"is_tariff": 1, "refusal": FX_REFUSAL}),
		                     ("stale", {"is_tariff": 1, "refusal": STALE_FX})):
			with self.subTest(fx=case):
				self.tariffs["CONTRACT"] = answer
				self.bridge_calls.clear()
				self.assertEqual(self.usd("CONTRACT"), Decimal("130.00"))
				self.assertEqual(self.usd("DERIVED"), Decimal("117.00"))
				self.assertEqual(self.tariff_forms(), [])
				self.assertEqual(self.directory_forms(), [{"property": "Test Hotel"}] * 2)

	def test_a_property_currency_night_still_asks_the_quote_and_holds_without_the_rate(self):
		self.tariffs["CONTRACT"] = {"is_tariff": 1, "refusal": FX_REFUSAL}
		with self.assertRaisesRegex(self.channel_ari.AriPriceHeld, "HSP-TARIFF-003"):
			self.night("CONTRACT", "UGX")
		self.assertEqual(len(self.tariff_forms()), 1)
		self.assertEqual(self.directory_forms(), [])

	def test_the_figure_is_the_one_the_quote_gives_when_the_exchange_rate_exists(self):
		"""A Family room sold at three adults over its largest tier (two):
		USD 130 and one extra adult at USD 20.50, as tariff_rate prices it
		(nightly 150.50, extra_person 20.50), then each derivation as
		contract_rate applies it: Percent on the room part only, a per-night
		Amount, and a Percent that adjusts the extra person too."""
		self.add("Room Type", "Test Hotel-FAM", property="Test Hotel", room_type_name="Family", base_occupancy=3,
		         adults_capacity=3)
		self.plan("AMOUNT", base_rate_plan="CONTRACT", modifier_type="Amount", modifier_value=5)
		self.plan("WHOLE", base_rate_plan="CONTRACT", modifier_type="Percent", modifier_value=-10,
		          adjust_extra_person=1)
		self.listing = directory(
			tariff_row("FAM-1A", version("IP-F1", 100), room_type="Family", tier=1),
			tariff_row("FAM-2A", version("IP-F2", 130), room_type="Family", tier=2),
			tariff_row("TAR-EXTRA-ADULT", version("IP-XA", 20.5), room_type=None, category="Supplement"),
			tariff_row("STD-2A", version("IP-130", 130)))
		self.tariffs["CONTRACT"] = self.tariff(nightly=150.5, extra_person=20.5, fx_rate=3648.2, agreed_rate=549054)
		expected = {"CONTRACT": "150.50", "DERIVED": "137.50", "AMOUNT": "155.50", "WHOLE": "135.45"}
		for plan, figure in expected.items():
			with self.subTest(plan=plan):
				_agreed, quoted = self.pricing.contract_rate("Test Hotel", "Test Hotel-FAM", plan, NIGHT, NEXT, 3, 0)
				self.assertEqual(Decimal(str(quoted["nightly"])), Decimal(figure))
				self.assertEqual(self.usd(plan, "Test Hotel-FAM"), Decimal(figure))

	def test_the_base_occupancy_sells_at_its_tier_with_the_room_types_own_extra_adult(self):
		"""tariff_rate's lines: the largest tier up to the adults, else the
		smallest; each adult above it, up to adults_capacity, at the room
		type's own Extra Adult supplement, else TAR-EXTRA-ADULT."""
		self.add("Room Type", "Test Hotel-FAM", property="Test Hotel", room_type_name="Family", base_occupancy=3,
		         adults_capacity=3)
		rows = [tariff_row("FAM-1A", version("IP-F1", 100), room_type="Family", tier=1),
		        tariff_row("FAM-2A", version("IP-F2", 130), room_type="Family", tier=2),
		        tariff_row("FAM-XA", version("IP-FX", 25), room_type="Family", category="Supplement",
		                   occupancy="Extra Adult"),
		        tariff_row("TAR-EXTRA-ADULT", version("IP-XA", 20.5), room_type=None, category="Supplement")]
		self.listing = directory(*rows)
		self.assertEqual(self.usd("CONTRACT", "Test Hotel-FAM"), Decimal("155.00"))
		self.docs[("Room Type", "Test Hotel-FAM")].adults_capacity = 2  # no billable adult above the tier
		self.assertEqual(self.usd("CONTRACT", "Test Hotel-FAM"), Decimal("130.00"))
		self.docs[("Room Type", "Test Hotel-FAM")].base_occupancy = 1
		self.assertEqual(self.usd("CONTRACT", "Test Hotel-FAM"), Decimal("100.00"))
		self.listing = directory(rows[1], rows[2])  # one adult below the smallest tier sells at it
		self.assertEqual(self.usd("CONTRACT", "Test Hotel-FAM"), Decimal("130.00"))

	def test_each_night_goes_at_the_approved_general_version_in_force_on_it(self):
		"""SAP condition validity (DATAB-DATBI), OPERA Rate Changes During
		Stay: the latest valid_from first, then the name, as tariff_rate
		resolves it. A draft, a rejected version and a customer's own price
		never go to a channel."""
		self.listing = directory(
			tariff_row("STD-2A", version("IP-DRAFT", 999, NIGHT, state="Draft"), version("IP-140", 140, NEXT),
			           version("IP-131", 131, "2026-11-15", NIGHT), version("IP-130", 130, "2026-11-15", NIGHT),
			           version("IP-REJ", 1, "2026-11-20", state="Rejected"), version("IP-OLD", 120, "2026-10-01")),
			tariff_row("STD-2A", version("IP-CUST", 50), customer="CUST-1"))
		self.assertEqual(self.usd("CONTRACT"), Decimal("131.00"))
		self.assertEqual(self.usd("CONTRACT", night=NEXT), Decimal("140.00"))
		self.assertEqual(self.usd("CONTRACT", night="2026-11-10"), Decimal("120.00"))

	def test_a_night_no_approved_positive_room_price_covers_stops_sale(self):
		for case, rows in (
				("draft only", [tariff_row("STD-2A", version("IP-D", 130, state="Draft"))]),
				("expired", [tariff_row("STD-2A", version("IP-E", 130, "2026-11-01", "2026-12-04"))]),
				("zero", [tariff_row("STD-2A", version("IP-Z", 0))]),
				("another room type", [tariff_row("DBL-2A", version("IP-DBL", 130), room_type="Double")]),
				("extra adult unpriced", [tariff_row("STD-1A", version("IP-S", 100), tier=1)])):
			with self.subTest(case=case):
				self.listing = directory(*rows)
				self.assertIsNone(self.usd("CONTRACT"))

	def test_no_listed_contract_or_one_in_another_currency_cannot_go_in_the_contract_currency(self):
		for case, listing in (
				("no list prices the plan or a base", directory(tariff_row("STD-2A", version("IP-1", 130)),
				                                                plans=("CARD",))),
				("listed in EUR", directory(tariff_row("STD-2A", version("IP-1", 130), price_list="Contract EUR",
				                                       currency="EUR"), price_list="Contract EUR", currency="EUR")),
				("no directory", None)):
			with self.subTest(case=case):
				self.listing = listing
				for plan in ("CONTRACT", "DERIVED"):
					with self.assertRaises(self.channel_ari.AriCurrencyRefused):
						self.usd(plan)

	def test_a_directory_it_cannot_read_holds_the_night(self):
		"""Two Standard room types, or a rate that is not a number: Kamra
		cannot tell which price is the contract's, so the night is held and
		Revenue told - never a guess, never a stop-sell."""
		for case, rows in (
				("two rooms at one tier", [tariff_row("STD-2A", version("IP-1", 130)),
				                           tariff_row("STD2-2A", version("IP-2", 160))]),
				("not a number", [tariff_row("STD-2A", version("IP-1", "n/a"))])):
			with self.subTest(case=case):
				self.listing = directory(*rows)
				with self.assertRaises(self.channel_ari.AriPricingFailed):
					self.usd("CONTRACT")


class FramePermissionError(Exception):
	"""frappe.PermissionError as Frappe defines it (frappe/exceptions.py:
	class PermissionError(Exception)); the harness's own is a ValidationError."""


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
		self.map("rp-bar", None, "UGX")
		self.map("rp-unknown", "CARD", None)
		cells, problems = self.cells()
		self.assertEqual(sorted(cells), ["rp-bar", "rp-ugx", "rp-usd"])
		self.assertEqual(cells["rp-usd"][NIGHT], {"rate": Decimal("130.00"), "stop_sell": False})
		self.assertEqual(cells["rp-ugx"][NIGHT], {"rate": Decimal("474000.00"), "stop_sell": False})
		self.assertEqual(cells["rp-bar"][NEXT], {"rate": CARD, "stop_sell": False})
		self.assertEqual(sorted(problems["refused"]), ["rp-eur", "rp-unknown"])
		for code in ("EUR", "USD", "UGX"):
			self.assertIn(code, problems["refused"]["rp-eur"])
		self.assertFalse(problems.get("held"))

	def test_a_missing_exchange_rate_sends_nothing_for_the_property_currency_night_instead_of_a_stop_sell(self):
		"""SAP SG 105: only the night that converts is held. The plan sold in
		its contract currency goes on both nights, from the tariff directory."""
		self.map("rp-usd", "CONTRACT", "USD")
		self.map("rp-ugx", "CONTRACT", "UGX")
		self.map("rp-bar", None, "UGX")
		priced = self.tariff(fx_rate=3648.2, agreed_rate=474266)
		self.tariffs["CONTRACT"] = lambda form: (
			{"is_tariff": 1, "refusal": FX_REFUSAL} if form["check_in"] == NIGHT else priced)
		cells, problems = self.cells()
		self.assertEqual(cells["rp-usd"], {NIGHT: {"rate": Decimal("130.00"), "stop_sell": False},
		                                   NEXT: {"rate": Decimal("130.00"), "stop_sell": False}})
		self.assertEqual(cells["rp-ugx"], {NEXT: {"rate": Decimal("474000.00"), "stop_sell": False}})
		self.assertEqual(cells["rp-bar"][NIGHT], {"rate": CARD, "stop_sell": False})
		self.assertEqual(sorted(problems["held"]), ["rp-ugx"])
		self.assertEqual(problems["held"]["rp-ugx"]["dates"], [NIGHT])
		self.assertIn("HSP-TARIFF-003", problems["held"]["rp-ugx"]["reason"])
		self.assertFalse(problems.get("refused"))

	def test_the_tariff_directory_is_read_once_a_pass(self):
		"""A full sync prices 500 nights of every mapping: the directory (all
		of the property's tariff versions) is read once for them, not once a
		night."""
		self.map("rp-usd", "CONTRACT", "USD")
		self.map("rp-usd-derived", "DERIVED", "USD")
		cells, _problems = self.cells()
		self.assertEqual({d: v["rate"] for d, v in cells["rp-usd-derived"].items()},
		                 {NIGHT: Decimal("117.00"), NEXT: Decimal("117.00")})
		self.assertEqual(self.directory_forms(), [{"property": "Test Hotel"}])
		self.assertEqual(self.tariff_forms(), [])

	def test_each_date_goes_at_its_own_contract_night(self):
		"""Guard (R6, OPERA Rate Changes During Stay): every pushed date is its
		own contract night - a one-night quote in the property's currency, the
		version in force that night in the contract's - so a tariff change
		inside the window shows on the day it starts, never one figure for
		the horizon."""
		self.map("rp-usd", "CONTRACT", "USD")
		self.map("rp-ugx", "CONTRACT", "UGX")
		self.listing = directory(tariff_row("STD-2A", version("IP-140", 140, NEXT),
		                                    version("IP-130", 130, "2026-11-01", NIGHT)))
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
		self.map("rp-ugx", "CONTRACT", "UGX")
		self.tariffs["CONTRACT"] = {"is_tariff": 1, "refusal": NO_TARIFF}
		self.listing = directory(tariff_row("STD-2A", version("IP-130", 130, "2026-11-01", NIGHT)))
		cells, problems = self.cells()
		self.assertEqual(cells["rp-ugx"], {NIGHT: {"stop_sell": True}, NEXT: {"stop_sell": True}})
		self.assertEqual(cells["rp-usd"], {NIGHT: {"rate": Decimal("130.00"), "stop_sell": False},
		                                   NEXT: {"stop_sell": True}})
		self.assertFalse(problems.get("held"))
		self.assertFalse(problems.get("refused"))
		self.assertFalse(problems.get("errored"))

	def test_a_night_that_errors_while_pricing_sends_nothing_and_is_reported(self):
		self.map("rp-ugx", "CONTRACT", "UGX")
		self.map("rp-bar", None, "UGX")
		priced = self.tariff(fx_rate=3648.2, agreed_rate=474266)

		def resolve(form):
			if form["check_in"] == NIGHT:
				raise FramePermissionError("Tariff prices are for hotel staff.")
			return priced
		self.tariffs["CONTRACT"] = resolve
		cells, problems = self.cells()
		self.assertEqual(cells["rp-ugx"], {NEXT: {"rate": Decimal("474000.00"), "stop_sell": False}})
		self.assertEqual(cells["rp-bar"][NIGHT], {"rate": CARD, "stop_sell": False})
		self.assertEqual(sorted(problems["errored"]), ["rp-ugx"])
		self.assertEqual(problems["errored"]["rp-ugx"]["dates"], [NIGHT])
		self.assertEqual(problems["errored"]["rp-ugx"]["rate_plan"], "CONTRACT")
		self.assertIn("FramePermissionError", problems["errored"]["rp-ugx"]["reason"])
		self.assertFalse(problems.get("held"))

	def test_a_directory_that_errors_sends_nothing_for_the_contract_currency_plan(self):
		self.map("rp-usd", "CONTRACT", "USD")
		self.map("rp-bar", None, "UGX")

		def refuse(form):
			raise FramePermissionError("No contract tariffs can be shown for that Property.")
		self.listing = refuse
		cells, problems = self.cells()
		self.assertNotIn("rp-usd", cells)
		self.assertEqual(cells["rp-bar"][NIGHT], {"rate": CARD, "stop_sell": False})
		self.assertEqual(problems["errored"]["rp-usd"]["dates"], [NIGHT, NEXT])
		self.assertIn("FramePermissionError", problems["errored"]["rp-usd"]["reason"])
		self.assertFalse(problems.get("held"))


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
