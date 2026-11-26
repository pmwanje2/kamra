"""The desk discounts the inclusive selling tariff before resolving the nightly slab."""
from decimal import Decimal, ROUND_HALF_UP

import frappe

from kamra import api, folio, pricing
from kamra.ledger import ensure_default_reason_codes
from kamra.tests.audit_fixtures import (
	PROP_A, TODAY, AuditTestCase, d, guest_folio, make_property, make_room, make_room_type,
)
from kamra.tests.test_reservation_discounts import set_setting


class InclusiveBoundaryCases:
	def check_stay(self, modifier, kind, nights, card=None):
		self.at(TODAY, "kamra.api", "kamra.folio", "kamra.business_date", "kamra.ledger")
		unit = self.unit
		if card is None:
			card = self.markup_card if modifier > 0 else self.markdown_card
		planned = [((card + i * 20) * (1 + Decimal(modifier) / 100)).quantize(unit, ROUND_HALF_UP)
		           for i in range(nights)]
		discount = planned[0] - 185 if kind == "Amount" else Decimal(10)
		sold = [(gross - discount if kind == "Amount" else gross * Decimal("0.90")).quantize(unit, ROUND_HALF_UP)
		        for gross in planned]
		# The first night including the extra bed sits exactly on the lower slab.
		# A second, seasonal night is above it even after the same desk discount.
		make_property(PROP_A, business_date=TODAY, currency=self.currency, rates_include_tax=1,
		              gst_mode="Slab", gst_slab_threshold=float(sold[0] + 5), gst_rate_low=5, gst_rate_high=18)
		ensure_default_reason_codes()
		set_setting(PROP_A, "rate_override_limit_pct", 40)
		rt = make_room_type(PROP_A, "R17", base_price=float(card - 10), base_occupancy=1,
		                    extra_adult_price=6, child_price=4, children_capacity=1, max_extra_beds=1)
		room = make_room(PROP_A, rt, "1701")
		if nights > 1:
			frappe.get_doc({
				"doctype": "Season", "property": PROP_A, "season_name": "R17 second night",
				"room_type": rt, "start_date": d(1), "end_date": d(1),
				"adjustment_type": "Amount", "adjustment_value": 20,
			}).insert()
		plan = frappe.get_doc({
			"doctype": "Rate Plan", "property": PROP_A, "rate_plan_name": "R17 plan", "code": "R17",
			"currency": self.currency, "modifier_type": "Percent", "modifier_value": modifier,
			"discount_eligible": 1,
		}).insert()
		terms = dict(adults=2, children=1, over_capacity_charge=5, rate_plan=plan.name,
		             discount_type=kind, discount_value=float(discount))
		quote = pricing.quote(PROP_A, rt, TODAY, d(nights), **terms)
		booking = api.create_booking(PROP_A, rt, TODAY, d(nights), "R17 Guest", room=room, **terms,
		                             over_capacity_guests=1, over_capacity_reason="Extra bed",
		                             discount_reason="RATE-PROMO")
		stay = frappe.get_doc("Reservation", booking["reservation"])
		folio.open_folio(stay)
		rows, beds = [], []
		total = Decimal(0)
		for i, gross in enumerate(sold):
			day = d(i)
			gst = 5 if i == 0 else 18
			factor = 1 + Decimal(gst) / 100
			net = (gross / factor).quantize(min(unit, Decimal("0.01")), ROUND_HALF_UP)
			rows.append((day, net, gst))
			beds.append((day, 5, gst))
			total += gross + 5 * factor
			folio.post_room_night(stay, day)
		bill = frappe.get_doc("Folio", guest_folio(stay.name))
		expected = total.quantize(unit, ROUND_HALF_UP)
		self.assertEqual(tuple(Decimal(str(amount)) for amount in (
			quote["amount_after_tax"], stay.amount_after_tax, bill.grand_total)), (expected,) * 3)
		self.assertEqual([Decimal(str(n["sold_room_gross"])) for n in quote["nightly"]], sold)
		self.assertEqual([(n["date"], Decimal(str(n["rate"])), n["gst_rate"]) for n in quote["nightly"]], rows)
		self.assertEqual([(str(n.stay_date), Decimal(str(n.rate)), n.gst_rate) for n in stay.night_rates], rows)
		self.assertEqual(sorted((str(c.posting_date), Decimal(str(c.amount)), c.gst_rate)
		                        for c in bill.charges if c.charge_type == "Room"), rows)
		self.assertEqual(sorted((str(c.posting_date), c.amount, c.gst_rate)
		                        for c in bill.charges if c.charge_type == "Extra Guests"), beds)

	def test_markup_then_flat_discount_at_boundary(self):
		self.check_stay(10, "Amount", 1)

	def test_markup_half_minor_unit_rounds_the_selling_tariff_first(self):
		self.check_stay(10, "Amount", 1, card=200 + 5 * self.unit)

	def test_markup_then_percentage_across_two_slabs(self):
		self.check_stay(10, "Percent", 2)

	def test_markdown_then_percentage_at_boundary(self):
		self.check_stay(-10, "Percent", 1)

	def test_markdown_then_flat_discount_across_two_slabs(self):
		self.check_stay(-10, "Amount", 2)


class TestUSD(InclusiveBoundaryCases, AuditTestCase):
	currency, unit = "USD", Decimal("0.01")
	markup_card, markdown_card = Decimal("201.40"), Decimal("220.09")


class TestUGX(InclusiveBoundaryCases, AuditTestCase):
	currency, unit = "UGX", Decimal("1")
	markup_card, markdown_card = Decimal("224"), Decimal("207")


class TestKWD(InclusiveBoundaryCases, AuditTestCase):
	currency, unit = "KWD", Decimal("0.001")
	markup_card, markdown_card = Decimal("220.002"), Decimal("220.008")
