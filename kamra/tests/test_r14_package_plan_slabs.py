"""A plan's adjusted nightly tariff determines the package room component's slab."""
from decimal import Decimal, ROUND_HALF_UP

import frappe

from kamra import folio, pricing
from kamra.tests import test_r10_room_move_posting as fixtures
from kamra.tests.audit_fixtures import AuditTestCase, PROP_A, TODAY, d, make_property, make_reservation


class PackageSlabCases:
	start = fixtures.TestR10Posting.start
	package = fixtures.TestR10Posting.package
	bill = fixtures.TestR10Posting.bill

	def check_crossing(self, inclusive, advertised, modifier, adjusted, slab):
		self.start(self.currency, inclusive=inclusive)
		make_property(PROP_A, business_date=TODAY, currency=self.currency, rates_include_tax=inclusive,
		              gst_mode="Slab", gst_slab_threshold=190, gst_rate_low=5, gst_rate_high=18)
		pkg = self.package("R14 transfer package", self.types[1], advertised)
		element = pkg.elements[0]
		element.unit_allowance, element.gst_rate, element.included_in_rate = 50, 5, 1
		pkg.save()
		plan = frappe.get_doc({
			"doctype": "Rate Plan", "property": PROP_A, "rate_plan_name": "R14 slab crossing",
			"code": "R14", "currency": self.currency, "modifier_type": "Percent",
			"modifier_value": modifier,
		}).insert()
		quote = pricing.quote(PROP_A, self.types[1], TODAY, d(1), adults=1, rate_plan=plan.name)
		self.stay = make_reservation(PROP_A, self.types[1], TODAY, d(1), room=self.rooms[1],
		                             status="Checked In", adults=1, auto_price=1, rate_plan=plan.name)
		folio.post_room_night(self.stay, TODAY)
		bill = self.bill()
		room, = [c for c in bill.charges if c.charge_type == "Room"]
		transfer, = [c for c in bill.charges if c.charge_type == "Taxi"]
		tariff = Decimal(adjusted)
		ratio = Decimal(slab) / 100
		element_tax = Decimal("2.50")
		if inclusive:
			element_tax = element_tax.quantize(self.unit, ROUND_HALF_UP)
			room_gross = tariff - 50 - element_tax
			room_net = (room_gross / (1 + ratio)).quantize(self.unit, ROUND_HALF_UP)
			room_tax = room_gross - room_net
			gross = tariff
		else:
			room_net = tariff - 50
			room_tax = room_net * ratio
			gross = (tariff + room_tax + element_tax).quantize(self.unit, ROUND_HALF_UP)
		actual = (quote["amount_after_tax"], self.stay.reload().amount_after_tax, bill.grand_total,
		          quote["amount_before_tax"], self.stay.amount_before_tax, bill.charges_total,
		          room.gst_rate, room.amount, room.gst_amount, transfer.amount, transfer.gst_amount)
		self.assertEqual(tuple(Decimal(str(v)) for v in actual),
		                 (gross, gross, gross, room_net + 50, room_net + 50, room_net + 50,
		                  slab, room_net, room_tax, 50, element_tax))

	def test_exclusive_discount_crosses_down(self):
		self.check_crossing(0, 200, -10, 180, 5)

	def test_exclusive_markup_crosses_up(self):
		self.check_crossing(0, 180, 10, 198, 18)

	def test_inclusive_discount_crosses_down(self):
		self.check_crossing(1, 200, -10, 180, 5)

	def test_inclusive_markup_crosses_up(self):
		self.check_crossing(1, 180, 10, 198, 18)


class TestShillings(PackageSlabCases, AuditTestCase):
	currency, unit = "UGX", Decimal(1)


class TestDollars(PackageSlabCases, AuditTestCase):
	currency, unit = "USD", Decimal(".01")


class TestDinars(PackageSlabCases, AuditTestCase):
	currency, unit = "KWD", Decimal(".001")
