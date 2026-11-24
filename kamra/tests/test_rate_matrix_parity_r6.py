"""Round 6 of the rate matrix: the OPERA Cloud / SAP S/4HANA parity check on kamra#33.

P0-1 a stay crossing a season or a tariff renewal bills each night at its
own tariff (OPERA Rate Changes During Stay, SAP condition validity). The
resolver may answer `nights`, one {date, nightly, agreed_rate, item_price}
per stay night in date order, its scalar fields staying the check-in
night's; without it nothing changes.
P0-3 a Current Date night's levy and extra guests are taxed at the rate
its room line posted at.
P0-4 ACTUAL reference currency sums each transaction at its own posting
rate, and a pro forma converts its estimate.
P1-1 Rate Options prices the company's own tariff; P1-5 Fixed at Block is
not offered; P1-6 derived contracts obey the Rate Guardrails, a plan's
currency matches its tariff price list and a fixed rate is never
re-locked; P1-8 the reference currency calculation is a property setting.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from kamra.tests.rate_matrix_harness import Doc, RateMatrixHarness, ValidationError

# three nights over a tariff renewal: USD 100 to 31 Dec, USD 200 from 1 Jan
STAY = ("Test Hotel", "Test Hotel-DLX", "2026-12-30", "2027-01-02", 2, 0)
DATES = ("2026-12-30", "2026-12-31", "2027-01-01")
NIGHTS = [
	{"date": "2026-12-30", "nightly": 100, "agreed_rate": 365000, "item_price": "IP-2026"},
	{"date": "2026-12-31", "nightly": 100, "agreed_rate": 365000, "item_price": "IP-2026"},
	{"date": "2027-01-01", "nightly": 200, "agreed_rate": 730000, "item_price": "IP-2027"},
]


def stub_module(name, **attrs):
	module = ModuleType(name)
	for key, value in attrs.items():
		setattr(module, key, value)
	return module


def locked(*rates, gst=0):
	return [Doc(stay_date=day, rate=rate, gst_rate=gst) for day, rate in zip(DATES, rates, strict=True)]


def per_night_snapshot(nights=NIGHTS, agreed=365000, fx=3650, fx_date="2026-12-20"):
	return {"agreed_rate": agreed, "tariff": {
		"currency": "USD", "nightly": nights[0]["nightly"], "fx_rate": fx, "fx_date": fx_date,
		"property_currency": "UGX", "nights": [dict(night) for night in nights]}}


class QuotePatches:
	"""The quote's collaborators that read seasons, demand and long-stay rules, as QuoteDerivationTest patches them."""

	def patch_quote(self):
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
			patcher = patch.object(self.pricing, name, value)
			patcher.start()
			self.addCleanup(patcher.stop)


class PerNightContractRateTest(RateMatrixHarness):
	"""P0-1: contract_rate keeps each night's own tariff when the resolver answers `nights`."""

	def resolve(self, plan="CORP", **kwargs):
		return self.pricing.contract_rate(*STAY[:2], plan, *STAY[2:], **kwargs)

	def test_each_night_keeps_its_own_tariff_and_the_scalars_stay_the_check_in_night(self):
		self.plan("CORP")
		self.tariffs["CORP"] = self.tariff(nightly=100, agreed_rate=365000, nights=NIGHTS)
		agreed, tariff = self.resolve()
		self.assertEqual((agreed, tariff["nightly"]), (365000, 100.0))
		self.assertEqual(tariff["nights"], [
			{"date": "2026-12-30", "nightly": 100.0, "agreed_rate": 365000.0, "item_price": "IP-2026"},
			{"date": "2026-12-31", "nightly": 100.0, "agreed_rate": 365000.0, "item_price": "IP-2026"},
			{"date": "2027-01-01", "nightly": 200.0, "agreed_rate": 730000.0, "item_price": "IP-2027"}])
		# a resolver that answers no nights prices the stay exactly as before
		self.tariffs["CORP"] = self.tariff(nightly=100, agreed_rate=365000)
		self.assertNotIn("nights", self.resolve()[1])

	def test_the_plan_rounding_step_rounds_each_night(self):
		self.plan("CORP", rounding_step=1000)
		nights = [dict(NIGHTS[0], agreed_rate=365250), dict(NIGHTS[1], agreed_rate=365250),
		          dict(NIGHTS[2], agreed_rate=730500)]
		self.tariffs["CORP"] = self.tariff(nightly=100, agreed_rate=365250, nights=nights)
		agreed, tariff = self.resolve()
		self.assertEqual(agreed, 365000)
		self.assertEqual([night["agreed_rate"] for night in tariff["nights"]], [365000, 365000, 731000])

	def test_a_derived_plan_prices_each_night_from_its_base_night(self):
		self.plan("CON")
		self.plan("CON-NR", base_rate_plan="CON", modifier_type="Percent", modifier_value=-10, rounding_step=1000)
		self.tariffs["CON"] = self.tariff(nightly=100, agreed_rate=365000, nights=NIGHTS)
		agreed, tariff = self.resolve("CON-NR")
		self.assertEqual(agreed, 329000)  # USD 90 at 3,650 = 328,500, nearest 1,000
		self.assertEqual(tariff["derived_from"], "CON")
		self.assertEqual([(night["nightly"], night["agreed_rate"]) for night in tariff["nights"]],
		                 [(90.0, 329000.0), (90.0, 329000.0), (180.0, 657000.0)])

	def test_nights_that_do_not_cover_the_stay_in_order_are_refused(self):
		self.plan("CORP")
		broken = {
			"a night missing": NIGHTS[:2],
			"out of order": [NIGHTS[1], NIGHTS[0], NIGHTS[2]],
			"a night outside the stay": [*NIGHTS[:2], dict(NIGHTS[2], date="2027-01-05")],
			"a night at zero": [*NIGHTS[:2], dict(NIGHTS[2], nightly=0)],
			"a night without an amount": [*NIGHTS[:2], {"date": "2027-01-01", "item_price": "IP-2027"}],
			"not a list": {"2026-12-30": 100},
		}
		for case, nights in broken.items():
			with self.subTest(case):
				self.tariffs["CORP"] = self.tariff(nightly=100, agreed_rate=365000, nights=nights)
				with self.assertRaisesRegex(self.pricing.ContractRateRefused, "incomplete"):
					self.resolve()


class PerNightQuoteTest(QuotePatches, RateMatrixHarness):
	"""P0-1: the quote bills each night at its own contract rate."""

	def extra_modules(self):
		return {"kamra.folio": stub_module("kamra.folio", _fnb_gst=lambda property: 5)}

	def setUp(self):
		super().setUp()
		self.add("Room Type", "Test Hotel-DLX", property="Test Hotel", base_price=100000, base_occupancy=2,
		         adults_capacity=4)
		self.plan("CORP")
		self.patch_quote()

	def test_a_quote_across_a_renewal_prices_each_night_at_its_own_tariff(self):
		self.tariffs["CORP"] = self.tariff(nightly=100, agreed_rate=365000, nights=NIGHTS)
		quoted = self.pricing.quote_with_contract(*STAY[:2], *STAY[2:4], 2, 0, rate_plan="CORP")
		self.assertEqual([night["rate"] for night in quoted["nightly"]], [365000, 365000, 730000])
		self.assertEqual(quoted["room_total"], 1460000)
		self.assertEqual(quoted["agreed_rate"], 365000)
		self.assertEqual(len(quoted["tariff"]["nights"]), 3)

	def test_each_night_takes_the_tax_slab_of_its_own_rate(self):
		self.tariffs["CORP"] = self.tariff(nightly=100, agreed_rate=365000, nights=NIGHTS)
		slab = lambda property, rt, rate: Decimal(18) if rate >= 500000 else Decimal(12)  # noqa: E731
		with patch.object(self.pricing, "room_gst_rate", slab):
			quoted = self.pricing.quote_with_contract(*STAY[:2], *STAY[2:4], 2, 0, rate_plan="CORP")
		self.assertEqual([night["gst_rate"] for night in quoted["nightly"]], [12, 12, 18])


class PerNightBookingTest(QuotePatches, RateMatrixHarness):
	"""P0-1: a booking locks each night's own contract rate in night_rates."""

	modules = ("kamra.pricing", "kamra.kamra.doctype.reservation.reservation")

	def extra_modules(self):
		return {"kamra.folio": stub_module("kamra.folio", _fnb_gst=lambda property: 5)}

	def setUp(self):
		super().setUp()
		self.add("Room Type", "Test Hotel-DLX", property="Test Hotel", base_price=100000, base_occupancy=2,
		         adults_capacity=4, free_child_age=6)
		self.plan("CORP")
		self.patch_quote()
		self.tariffs["CORP"] = self.tariff(nightly=100, agreed_rate=365000, nights=NIGHTS)

	def booking(self, **fields):
		values = dict(property="Test Hotel", room_type="Test Hotel-DLX", rate_plan="CORP", check_in_date=STAY[2],
		              check_out_date=STAY[3], adults=2, children=0, extra_beds=0, company="Client Co",
		              auto_price=1, agreed_rate=0, meal_plan=None, flags=Doc())
		doc = self.reservation.Reservation({**values, **fields})
		doc.is_new = lambda: True
		return doc

	def test_the_booking_locks_each_night_at_its_own_contract_rate(self):
		doc = self.booking()
		doc.capture_contract_tariff()
		self.assertEqual(doc.agreed_rate, 365000)
		inputs = doc.quote_inputs()
		self.assertEqual(inputs["agreed_nights"], {"2026-12-30": 365000.0, "2026-12-31": 365000.0,
		                                           "2027-01-01": 730000.0})
		rows = doc.quote_night_rows(doc.requote())
		self.assertEqual([(row["stay_date"], row["rate"]) for row in rows],
		                 [("2026-12-30", 365000), ("2026-12-31", 365000), ("2027-01-01", 730000)])

	def test_a_managers_agreed_rate_or_a_one_rate_contract_prices_every_night_alike(self):
		doc = self.booking()
		doc.capture_contract_tariff()
		self.assertIn("agreed_nights", doc.quote_inputs())
		doc.agreed_rate = 400000  # set_agreed_rate: the manager's price for the stay (OPERA fixed rate)
		self.assertNotIn("agreed_nights", doc.quote_inputs())
		self.assertEqual([row["rate"] for row in doc.quote_night_rows(doc.requote())], [400000] * 3)
		# a booking whose contract has one rate for the stay keeps its rate-lock key
		plain = self.booking(agreed_rate=365000, contract_tariff={"agreed_rate": 365000, "tariff": {
			"currency": "USD", "nightly": 100, "fx_rate": 3650, "fx_date": "2026-12-20",
			"property_currency": "UGX"}})
		self.assertNotIn("agreed_nights", plain.quote_inputs())


class PerNightContractStay(RateMatrixHarness):
	"""A three-night USD contract stay over the renewal, booked at 3,650."""

	modules = ("kamra.pricing", "kamra.folio")
	policy = "Fixed at Check In"

	def setUp(self):
		super().setUp()
		self.docs["Property", "Test Hotel"].business_date = "2026-12-30"
		self.add("Room Type", "Test Hotel-DLX", property="Test Hotel", base_price=400000)
		self.plan("CORP", fx_lock_policy=self.policy, rounding_step=1000)
		self.res = self.add("Reservation", "RES-1", property="Test Hotel", room="Test Hotel-101",
		                    room_type="Test Hotel-DLX", rate_plan="CORP", check_in_date=STAY[2],
		                    check_out_date=STAY[3], adults=2, children=0, extra_beds=0, company="Client Co",
		                    agreed_rate=365000, status="Confirmed",
		                    contract_tariff=json.dumps(per_night_snapshot()),
		                    night_rates=locked(365000, 365000, 730000))


class PerNightCheckInRelockTest(PerNightContractStay):
	"""P0-1 and P1-6: Fixed at Check In converts each booked night again; a fixed rate is never re-locked."""

	def setUp(self):
		super().setUp()
		self.rate("USD", 3712.5, "2026-12-30")

	def test_each_night_converts_its_own_booked_amount_at_the_check_in_rate(self):
		self.assertTrue(self.pricing.relock_contract_fx_at_check_in(self.res))
		self.assertEqual(self.tariff_forms(), [])
		self.assertEqual(self.res.agreed_rate, 371000)  # USD 100 x 3,712.5 = 371,250, nearest 1,000
		self.assertEqual(self.res.contract_tariff["agreed_rate"], 371000)
		self.assertEqual([(night["date"], night["nightly"], night["agreed_rate"], night["item_price"])
		                  for night in self.res.contract_tariff["tariff"]["nights"]], [
			("2026-12-30", 100, 371000.0, "IP-2026"), ("2026-12-31", 100, 371000.0, "IP-2026"),
			("2027-01-01", 200, 743000.0, "IP-2027")])  # USD 200 x 3,712.5 = 742,500

	def test_a_fixed_rate_booking_is_never_relocked(self):
		self.res.fixed_rate = 1  # a manual fixed rate (Track A): check-in never replaces it
		self.assertFalse(self.pricing.relock_contract_fx_at_check_in(self.res))
		self.assertEqual(self.res.agreed_rate, 365000)
		self.assertFalse(self.res.flags.get("kamra_contract_relocked"))


class PostedLines:
	def post_night(self, date):
		lines = []
		for name, value in (("_charge_posted", lambda *args: False),
		                    ("_append_charge", lambda folios, reservation, charge_type, line, *args: lines.append(line))):
			patcher = patch.object(self.folio, name, value)
			patcher.start()
			self.addCleanup(patcher.stop)
		self.folio.post_room_night(self.res, date, folios={})
		return {line["charge_type"]: line for line in lines}


class PerNightCurrentDateTest(PostedLines, PerNightContractStay):
	"""P0-1: a Current Date night converts its own contract amount at its posting date."""

	policy = "Current Date"

	def test_a_night_after_the_renewal_posts_its_own_tariff_at_its_own_rate(self):
		self.docs["Property", "Test Hotel"].business_date = "2027-01-01"
		self.rate("USD", 3712.5, "2027-01-01")
		room = self.post_night("2027-01-01")["Room"]
		self.assertEqual(room["rate"], 743000)  # USD 200 x 3,712.5 = 742,500, nearest 1,000
		self.assertIn("USD 200.00 at 3,712.5", room["description"])
		# a night before it posts its own USD 100
		self.rate("USD", 3700, "2026-12-31")
		self.assertEqual(self.post_night("2026-12-31")["Room"]["rate"], 370000)


class LevyTaxFollowsThePostedNightTest(PostedLines, PerNightContractStay):
	"""P0-3: the levy and the extra guests take the GST rate the night's room line posted at."""

	policy = "Current Date"

	def setUp(self):
		super().setUp()
		self.docs["Property", "Test Hotel"].update(room_levy_percent=10, room_levy_taxable=1)
		self.res.update(over_capacity_charge=50000, over_capacity_guests=1, night_rates=locked(
			365000, 365000, 730000, gst=12))
		self.rate("USD", 3800, "2026-12-30")
		# the slab reads the night's whole tariff: 380,000 + 50,000 extra guests is above 400,000
		slab = lambda property, rt, rate: Decimal(18) if rate >= 400000 else Decimal(12)  # noqa: E731
		patcher = patch.object(self.pricing, "room_gst_rate", slab)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_the_levy_and_the_extra_guests_are_taxed_at_the_night_s_posted_rate(self):
		lines = self.post_night("2026-12-30")
		self.assertEqual((lines["Room"]["rate"], lines["Room"]["gst_rate"]), (380000, 18))
		self.assertEqual((lines["Room Levy"]["amount"], lines["Room Levy"]["gst_rate"]), (38000, 18))
		self.assertEqual((lines["Extra Guests"]["amount"], lines["Extra Guests"]["gst_rate"]), (50000, 18))


class ActualReferenceCurrencyTest(RateMatrixHarness):
	"""P0-4: ACTUAL converts each transaction at the rate of its own posting date (OPERA)."""

	modules = ("kamra.pricing", "kamra.folio")

	def setUp(self):
		super().setUp()
		self.docs["Property", "Test Hotel"].business_date = "2026-12-02"
		self.plan("CORP", fx_lock_policy="Current Date")
		self.res = self.add("Reservation", "RES-1", property="Test Hotel", rate_plan="CORP",
		                    contract_tariff=json.dumps({"agreed_rate": 300000, "tariff": {
		                        "currency": "USD", "nightly": 100, "fx_rate": 3000, "fx_date": "2026-11-30",
		                        "property_currency": "UGX"}}))
		self.rate("USD", 3000, "2026-12-01")
		self.rate("USD", 4000, "2026-12-02")
		room = dict(charge_type="Room", gst_rate=0, contract_currency="USD", reservation="RES-1",
		            parenttype="Folio")
		# USD 100 a night: 300,000 at 3,000 on the first night, 400,000 at 4,000 on the second
		self.charges = [Doc(room, name="C1", amount=300000, posting_date="2026-12-01", contract_fx_rate=3000,
		                    contract_fx_date="2026-12-01"),
		                Doc(room, name="C2", amount=400000, posting_date="2026-12-02", contract_fx_rate=4000,
		                    contract_fx_date="2026-12-02")]
		self.payments = [Doc(name="P1", amount=300000, posting_date="2026-12-01", currency="UGX")]
		self.records["Folio Charge"] = self.charges

	def shown(self, grand_total, balance):
		return self.folio.reference_currency(self.res, grand_total=grand_total, balance=balance,
		                                     charges=self.charges, payments=self.payments)

	def test_a_paid_first_night_leaves_the_second_night_owed(self):
		shown = self.shown(700000, 400000)
		# not 400,000 / 3,500 = USD 114.29: night 2 is USD 100 at its own 4,000
		self.assertEqual((shown["grand_total_reference"], shown["balance_reference"]), (200.0, 100.0))
		self.assertEqual((shown["fx_rate"], shown["posted_nights"]), (3500.0, 2))

	def test_other_lines_and_payments_convert_at_their_own_posting_date(self):
		self.charges.append(Doc(name="F1", charge_type="F&B", amount=40000, gst_rate=0, posting_date="2026-12-02"))
		# a night posted at a mis-published rate, then voided: neither line counts
		self.charges += [Doc(name="C3", charge_type="Room", amount=500000, gst_rate=0, posting_date="2026-12-02",
		                     contract_currency="USD", contract_fx_rate=9999),
		                 Doc(name="C4", charge_type="Room", amount=-500000, gst_rate=0, posting_date="2026-12-02",
		                     reversal_of="C3")]
		# USD 100 tendered at the cashier counts as USD 100, whatever it credited in UGX
		self.payments.append(Doc(name="P2", amount=380000, posting_date="2026-12-02", currency="USD", fx_amount=100))
		shown = self.shown(740000, 60000)
		self.assertEqual((shown["grand_total_reference"], shown["balance_reference"]), (210.0, 10.0))

	def test_a_line_on_a_date_with_no_published_rate_converts_at_the_booked_rate(self):
		self.charges.append(Doc(name="F1", charge_type="F&B", amount=30000, gst_rate=10, posting_date="2026-11-28"))
		shown = self.shown(733000, 433000)
		self.assertEqual((shown["grand_total_reference"], shown["balance_reference"]), (211.0, 111.0))

	def test_a_fixed_rate_stay_still_converts_at_its_locked_rate(self):
		self.docs["Rate Plan", "CORP"].fx_lock_policy = "Fixed at Check In"
		shown = self.shown(600000, 300000)
		self.assertEqual((shown["fx_rate"], shown["grand_total_reference"], shown["balance_reference"]),
		                 (3000.0, 200.0, 100.0))


class ProformaReferenceCurrencyTest(RateMatrixHarness):
	"""P0-4: a pro forma converts its estimate (posted plus projected), not the posted folio alone."""

	modules = ("kamra.pricing", "kamra.folio", "kamra.authz", "kamra.ledger")

	def extra_modules(self):
		self.invoice = None
		return {"kamra.api": stub_module("kamra.api", folio_invoice=lambda folio: self.invoice),
		        "kamra.checkout": stub_module("kamra.checkout", _NoTrace=self.no_trace)}

	@staticmethod
	@contextmanager
	def no_trace(name):
		yield

	def setUp(self):
		super().setUp()
		self.roles = ["Finance"]
		self.authz.restricted_properties = lambda user=None: None
		self.plan("CORP", fx_lock_policy="Fixed at Check In")
		self.res = self.add("Reservation", "RES-1", property="Test Hotel", rate_plan="CORP", status="Checked In",
		                    contract_tariff=json.dumps({"agreed_rate": 474500, "tariff": {
		                        "currency": "USD", "nightly": 130, "fx_rate": 3650, "fx_date": "2026-11-20",
		                        "property_currency": "UGX"}}))
		folio = {"name": "F-1", "property": "Test Hotel", "reservation": "RES-1", "status": "Open",
		         "grand_total": 0, "balance": 0, "charges": [], "payments": []}
		self.invoice = {"folio": folio, "stay": {"reservation": "RES-1", "reference_currency": {
			"currency": "USD", "home_currency": "UGX", "calculation": "ACTUAL", "fx_rate": 3650.0,
			"fx_date": "2026-11-20", "grand_total_reference": 0.0, "balance_reference": 0.0}}}
		self.inserted = []
		harness_get_doc = self.frappe.get_doc

		def get_doc(doctype, name=None):
			if isinstance(doctype, dict):
				doc = Doc(doctype)
				doc.insert = lambda **kwargs: self.inserted.append(doc)
				doc.name = "PF-1"
				return doc
			return harness_get_doc(doctype, name)

		patcher = patch.object(self.frappe, "get_doc", get_doc)
		patcher.start()
		self.addCleanup(patcher.stop)

	def projected(self, *nights):
		return [{"posting_date": day, "charge_type": "Room", "amount": amount, "gst_rate": 0, "gst_amount": 0,
		         "total": amount, "projected": True, **fx} for day, amount, fx in nights]

	def test_a_pro_forma_before_the_audit_shows_the_estimate_in_the_contract_currency(self):
		lines = self.projected(("2026-12-01", 474500, {}), ("2026-12-02", 474500, {}))
		with patch.object(self.ledger, "_projected_lines", lambda f: lines):
			reference = self.ledger.generate_proforma("F-1")["invoice"]["stay"]["reference_currency"]
		self.assertEqual((reference["grand_total_reference"], reference["balance_reference"]), (260.0, 260.0))

	def test_a_current_date_pro_forma_sums_each_projected_night_at_its_rate(self):
		self.docs["Rate Plan", "CORP"].fx_lock_policy = "Current Date"
		fx = lambda rate, day: {"contract_currency": "USD", "contract_fx_rate": rate, "contract_fx_date": day}  # noqa: E731
		lines = self.projected(("2026-12-01", 481000, fx(3700, "2026-12-01")),
		                       ("2026-12-02", 494000, fx(3800, "2026-12-01")))
		with patch.object(self.ledger, "_projected_lines", lambda f: lines):
			reference = self.ledger.generate_proforma("F-1")["invoice"]["stay"]["reference_currency"]
		self.assertEqual((reference["grand_total_reference"], reference["balance_reference"]), (260.0, 260.0))

	def test_projected_nights_carry_the_rate_they_would_post_at(self):
		folio = Doc(doctype="Folio", name="F-1", charges=[])
		self.docs["Folio", "F-1"] = folio

		def post_remaining_nights(reservation):
			folio.charges.append(Doc(name="FC-9", posting_date="2026-12-02", charge_type="Room", description="Room",
			                         qty=1, rate=494000, amount=494000, gst_rate=0, gst_amount=0, total=494000,
			                         contract_currency="USD", contract_fx_rate=3800, contract_fx_date="2026-12-01"))

		with patch.object(self.folio, "post_remaining_nights", post_remaining_nights):
			line, = self.ledger._projected_lines(self.invoice["folio"])
		self.assertEqual((line["contract_currency"], line["contract_fx_rate"], line["contract_fx_date"]),
		                 ("USD", 3800, "2026-12-01"))


class RateOptionsCompanyTest(RateMatrixHarness):
	"""P1-1 and P2: Rate Options prices the company's own tariff and says how each plan locks its rate."""

	modules = ("kamra.pricing", "kamra.authz", "kamra.rates")

	def extra_modules(self):
		return {"kamra.settings": stub_module("kamra.settings", get_setting=lambda property, key: 0,
		                                      list_setting=lambda property, key: [])}

	def setUp(self):
		super().setUp()
		self.roles = ["Front Desk"]
		self.authz.restricted_properties = lambda user=None: None
		self.add("Room Type", "Test Hotel-DLX", property="Test Hotel", base_price=100000)
		self.plan("BAR", rate_plan_name="Best available")
		self.plan("CORP", rate_plan_name="Corporate", fx_lock_policy="Fixed at Check In", base_rate_plan="BAR")
		self.records["Rate Plan"] = [
			Doc(self.docs["Rate Plan", name], disabled=0, is_default=int(name == "BAR")) for name in ("BAR", "CORP")]
		self.add("Corporate Account", "Client Co", negotiated_rate_plan="CORP")
		self.quotes = []
		tariff = dict(self.tariff(nightly=100, agreed_rate=365000), nights=[dict(night) for night in NIGHTS])

		def quote_with_contract(property, room_type, check_in, check_out, adults, children, meal_plan=None,
		                        rate_plan=None, voucher_code=None, **kwargs):
			self.quotes.append((rate_plan, kwargs.get("company")))
			nightly = [{"date": day, "rate": 0} for day in DATES]
			if rate_plan == "CORP" or (not rate_plan and kwargs.get("company")):
				return {"room_total": 1460000, "amount_after_tax": 1460000, "discount": 0, "agreed_rate": 365000,
				        "tariff": tariff, "nightly": nightly}
			return {"room_total": 300000, "amount_after_tax": 300000, "discount": 0, "nightly": nightly}

		night = lambda property, rt, date, adults, children, small_ok=True: {  # noqa: E731
			"date": str(date), "rate": 100000, "bar": 100000, "after_season": 100000, "season": None,
			"demand": None, "occupancy": 0}
		for target, name, value in ((self.pricing, "quote_with_contract", quote_with_contract),
		                            (self.pricing, "small_group_allows_stay", lambda *args: True),
		                            (self.rates, "explain_night", night)):
			patcher = patch.object(target, name, value)
			patcher.start()
			self.addCleanup(patcher.stop)

	def options(self, **kwargs):
		return self.rates.rate_options("Test Hotel", "Test Hotel-DLX", STAY[2], STAY[3], 2, 0, **kwargs)

	def test_the_company_s_own_price_and_plan_reach_rate_options(self):
		out = self.options(company="Client Co")
		self.assertEqual(sorted(self.quotes, key=str), sorted([(None, None), ("BAR", "Client Co"),
		                                                        ("CORP", "Client Co")], key=str))
		self.assertEqual(out["company_plan"], "CORP")
		# no plan picked: the company's plan prices the stay, as the quote below it does - night by night
		self.assertEqual([night["rate"] for night in out["nights"]], [365000, 365000, 730000])

	def test_each_plan_says_how_it_locks_its_exchange_rate_and_what_it_derives_from(self):
		plans = {row["name"]: row for row in self.options()["plans"]}
		self.assertEqual((plans["CORP"]["fx_lock_policy"], plans["CORP"]["derived_from"]),
		                 ("Fixed at Check In", "Best available"))
		self.assertEqual((plans["BAR"]["fx_lock_policy"], plans["BAR"].get("derived_from")),
		                 ("Fixed at Reservation", None))

	def test_a_picked_contract_plan_lists_each_night_at_its_own_rate(self):
		out = self.options(rate_plan="CORP")
		self.assertEqual([night["rate"] for night in out["nights"]], [365000, 365000, 730000])
		self.assertIsNone(out["company_plan"])


class DerivedContractGuardrailTest(RateMatrixHarness):
	"""P1-6: a derived contract plan's local rate obeys the property's Rate Guardrails, as the resolver's do."""

	def setUp(self):
		super().setUp()
		self.plan("CON")
		self.plan("CON-NR", base_rate_plan="CON", modifier_type="Percent", modifier_value=-10, rounding_step=1000)
		self.tariffs["CON"] = self.tariff()  # USD 130; CON-NR prices USD 117 = 427,000 a night

	def rails(self, *rails):
		self.records["Rate Guardrail"] = [Doc({"property": "Test Hotel", "disabled": 0, "room_type": None,
		                                       "days_of_week": None, **rail}) for rail in rails]

	def resolve(self, check_in="2026-12-01", check_out="2026-12-03"):
		return self.pricing.contract_rate("Test Hotel", "Test Hotel-DLX", "CON-NR", check_in, check_out, 2, 0)

	def test_a_derived_rate_below_the_floor_or_above_the_ceiling_is_refused(self):
		self.rails({"floor_price": 430000, "ceiling_price": 900000})
		with self.assertRaisesRegex(self.pricing.ContractRateRefused, "floor"):
			self.resolve()
		self.rails({"floor_price": 100000, "ceiling_price": 420000})
		with self.assertRaisesRegex(self.pricing.ContractRateRefused, "ceiling"):
			self.resolve()
		self.rails({"floor_price": 400000, "ceiling_price": 500000})
		self.assertEqual(self.resolve()[0], 427000)

	def test_the_most_specific_rail_for_each_night_governs(self):
		# Tuesday and Wednesday: the weekend room-type rail does not bind; the house-wide one does
		self.rails({"floor_price": 100000, "ceiling_price": 900000},
		           {"room_type": "Test Hotel-DLX", "days_of_week": "sat,sun", "floor_price": 430000,
		            "ceiling_price": 900000})
		self.assertEqual(self.resolve()[0], 427000)
		with self.assertRaisesRegex(self.pricing.ContractRateRefused, "floor"):
			self.resolve("2026-12-05", "2026-12-07")  # Saturday and Sunday

	def test_each_night_of_a_renewal_stay_is_checked_at_its_own_rate(self):
		self.tariffs["CON"] = self.tariff(nightly=100, agreed_rate=365000, nights=NIGHTS)
		self.rails({"floor_price": 100000, "ceiling_price": 600000})  # USD 180 = 657,000 on 1 January
		with self.assertRaisesRegex(self.pricing.ContractRateRefused, "ceiling"):
			self.pricing.contract_rate(*STAY[:2], "CON-NR", *STAY[2:])


class RatePlanPriceListCurrencyTest(RateMatrixHarness):
	"""P1-6: a plan priced by a tariff price list is in that list's currency."""

	def setUp(self):
		super().setUp()
		self.controller = self.load("kamra.kamra.doctype.rate_plan.rate_plan")
		self.add("Price List", "Contract USD", currency="USD")
		self.plan("BAR", currency="USD")
		harness_exists = self.frappe.db.exists
		patcher = patch.object(self.frappe.db, "exists",
		                       lambda doctype, name=None: name == "Price List" or harness_exists(doctype, name))
		patcher.start()
		self.addCleanup(patcher.stop)

	def validate(self, **fields):
		values = {"name": "Test Hotel-CORP", "property": "Test Hotel", "rate_plan_name": "Corporate",
		          "code": "CORP", "modifier_type": "Percent", "modifier_value": 0,
		          "fx_lock_policy": "Fixed at Reservation", "rounding_step": 0, "rounding_mode": "Nearest",
		          "custom_tariff_price_list": "Contract USD"}
		doc = self.controller.RatePlan({**values, **fields})
		doc.validate()
		return doc

	def test_a_plan_in_another_currency_than_its_price_list_is_refused(self):
		# Blank currency on a tariff price list takes the price list's currency (P1-2), never the property's UGX.
		self.assertEqual(self.validate().currency, "USD")
		with self.assertRaisesRegex(ValidationError, "Contract USD"):
			self.validate(currency="UGX")
		with self.assertRaisesRegex(ValidationError, "USD"):
			self.validate(currency="EUR")
		self.validate(currency="USD")
		self.validate(base_rate_plan="BAR")  # USD from its base plan
		self.validate(custom_tariff_price_list=None)


class FixedAtBlockNotOfferedTest(RateMatrixHarness):
	"""P1-5: no group block locks a rate, so Fixed at Block is not a policy a plan can pick."""

	def test_the_rate_plan_offers_only_the_policies_kamra_applies(self):
		meta = json.loads((Path(__file__).parents[1] / "kamra/doctype/rate_plan/rate_plan.json").read_text())
		field, = [field for field in meta["fields"] if field["fieldname"] == "fx_lock_policy"]
		self.assertEqual(field["options"].split("\n"), ["Fixed at Reservation", "Fixed at Check In", "Current Date"])
		self.assertNotIn("block", field["description"].lower())


class ReferenceCurrencySettingTest(RateMatrixHarness):
	"""P1-8: a Finance user sets the Property's reference currency calculation in Settings."""

	modules = ("kamra.authz", "kamra.settings")

	def test_finance_settings_offer_the_reference_currency_calculation(self):
		module, spec = self.settings.SETTINGS["reference_currency_calculation"]
		self.assertEqual(module.id, "finance")
		self.assertEqual((spec.type, spec.store, spec.default, spec.options),
		                 ("select", "property", "ACTUAL", ("ACTUAL", "CURRENT")))
		self.assertTrue(spec.hint)
