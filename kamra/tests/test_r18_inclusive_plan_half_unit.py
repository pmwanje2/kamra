"""Inclusive rate plans must round the actual selling tariff at half a minor unit."""
from decimal import Decimal, ROUND_HALF_UP

import frappe

from kamra import api, folio, pricing
from kamra.ledger import ensure_default_reason_codes
from kamra.tests.audit_fixtures import (
	PROP_A, TODAY, AuditTestCase, d, guest_folio, make_property, make_room, make_room_type,
)
from kamra.tests.test_reservation_discounts import set_setting


class HalfUnitCases:
	def check_price(self, card_units, modifiers, expected_units, extra_person_units=0):
		"""Inputs in minor units keep the business case identical in all three currencies."""
		self.at(TODAY, "kamra.api", "kamra.folio", "kamra.business_date", "kamra.ledger")
		unit = self.unit
		make_property(PROP_A, business_date=TODAY, currency=self.currency, rates_include_tax=1,
		              gst_mode="Slab", gst_slab_threshold=float(10500 * unit), gst_rate_low=5, gst_rate_high=18)
		ensure_default_reason_codes()
		set_setting(PROP_A, "rate_override_limit_pct", 50)
		rt = make_room_type(PROP_A, "R18", base_price=float((card_units - extra_person_units) * unit),
		                    base_occupancy=1, extra_adult_price=float(extra_person_units * unit), max_extra_beds=1)
		room = make_room(PROP_A, rt, "1801")
		parent = None
		for idx, (kind, value) in enumerate(modifiers):
			parent = frappe.get_doc({
				"doctype": "Rate Plan", "property": PROP_A, "rate_plan_name": f"R18 plan {idx}",
				"code": f"R18-{idx}", "base_rate_plan": parent, "currency": self.currency,
				"modifier_type": kind, "modifier_value": float(value * unit if kind == "Amount" else value),
				"adjust_extra_person": 0, "discount_eligible": 1,
			}).insert().name
		terms = dict(adults=2 if extra_person_units else 1, over_capacity_charge=float(500 * unit), rate_plan=parent)
		planned = pricing.quote(PROP_A, rt, TODAY, d(1), **terms)["nightly"][0]["sold_room_gross"]
		# The discounted room sells for 10,001 units. Adding the 500-unit bed
		# crosses the 10,500-unit slab by one unit, so the bed adds 90 units of tax.
		terms.update(discount_type="Amount", discount_value=float((expected_units - 10001) * unit))
		quote = pricing.quote(PROP_A, rt, TODAY, d(1), **terms)
		booking = api.create_booking(PROP_A, rt, TODAY, d(1), "R18 Guest", room=room, **terms,
		                             over_capacity_guests=1, over_capacity_reason="Extra bed",
		                             discount_reason="RATE-PROMO")
		stay = frappe.get_doc("Reservation", booking["reservation"])
		folio.open_folio(stay)
		folio.post_room_night(stay, TODAY)
		bill = frappe.get_doc("Folio", guest_folio(stay.name))
		actual = tuple(Decimal(str(v)) for v in (planned, quote["amount_after_tax"], stay.amount_after_tax,
		                                         bill.grand_total))
		self.assertEqual(actual, (expected_units * unit,) + (10591 * unit,) * 3)
		sold = 10001 * unit
		self.assertEqual(Decimal(str(quote["nightly"][0]["sold_room_gross"])), sold)
		net = (sold / Decimal("1.18")).quantize(min(unit, Decimal("0.01")), ROUND_HALF_UP)
		self.assertEqual([(Decimal(str(n.rate)), n.gst_rate) for n in stay.night_rates], [(net, 18)])
		self.assertEqual([(Decimal(str(c.amount)), c.gst_rate) for c in bill.charges if c.charge_type == "Room"],
		                 [(net, 18)])
		self.assertEqual([(Decimal(str(c.amount)), c.gst_rate) for c in bill.charges if c.charge_type == "Extra Guests"],
		                 [(500 * unit, 18)])

	def test_markup(self):
		# 10,835 x 1.10 = 11,918.5 payable minor units.
		self.check_price(10835, [("Percent", 10)], 11919)

	def test_markdown(self):
		self.check_price(13555, [("Percent", -10)], 12200)

	def test_percentage_chain(self):
		self.check_price(11535, [("Percent", 25), ("Percent", -12)], 12689)

	def test_derived_markdown_protects_the_base_extra_guest_price(self):
		# The child leaves the parent's 125-unit extra guest charge alone.
		self.check_price(11535, [("Percent", 25), ("Percent", -12)], 12704, extra_person_units=100)

	def test_flat_net_modifier_at_half_a_selling_unit(self):
		# A 25-unit taxable increment adds 29.5 units to the inclusive tariff.
		self.check_price(10835, [("Amount", 25)], 10865)


class TestUSD(HalfUnitCases, AuditTestCase):
	currency, unit = "USD", Decimal("0.01")


class TestUGX(HalfUnitCases, AuditTestCase):
	currency, unit = "UGX", Decimal(1)


class TestKWD(HalfUnitCases, AuditTestCase):
	currency, unit = "KWD", Decimal("0.001")
