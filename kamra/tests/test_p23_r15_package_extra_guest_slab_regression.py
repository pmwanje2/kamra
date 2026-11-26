"""The separate extra-bed line follows each final room slab, including on package nights."""
from decimal import Decimal, ROUND_HALF_UP

import frappe

from kamra import folio, pricing
from kamra.tests.audit_fixtures import (
	AuditTestCase, PROP_A, TODAY, d, guest_folio, make_property, make_reservation, make_room, make_room_type,
)
from kamra.tests import test_r10_room_move_posting as fixtures


class ExtraGuestSlabs:
	def check_stay(self, package, inclusive, markup, nights=2):
		self.at(TODAY, "kamra.api", "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY, currency=self.currency, rates_include_tax=inclusive,
		              gst_mode="Slab", gst_slab_threshold=190, gst_rate_low=5, gst_rate_high=18)
		first, second = (180, 150) if markup else (200, 240)
		# On a standard rate, a child's 5 is inside the room tariff. The
		# separately agreed extra bed is another 5 and keeps its own line.
		children = 0 if package else 1
		rt = make_room_type(PROP_A, "R15", base_price=first - 5 * children, children_capacity=1,
		                    child_price=5, max_extra_beds=1)
		room = make_room(PROP_A, rt, "R15-01")
		if package:
			pkg = fixtures.TestR10Posting.package(self, "R15 Package", rt, first)
			pkg.extra_night_price = second
			element = pkg.elements[0]
			element.unit_allowance, element.gst_rate, element.included_in_rate = 50, 5, 1
			pkg.save()
		else:
			frappe.get_doc({
				"doctype": "Season", "property": PROP_A, "season_name": "Second night", "room_type": rt,
				"start_date": d(1), "end_date": d(1), "adjustment_type": "Amount",
				"adjustment_value": second - first,
			}).insert()
		plan = frappe.get_doc({
			"doctype": "Rate Plan", "property": PROP_A, "rate_plan_name": "R15 guest slab",
			"code": "R15", "currency": self.currency, "modifier_type": "Percent",
			"modifier_value": 10 if markup else -10,
		}).insert()
		args = dict(adults=1, children=children, over_capacity_charge=5, rate_plan=plan.name)
		quote = pricing.quote(PROP_A, rt, TODAY, d(nights), **args)
		stay = make_reservation(PROP_A, rt, TODAY, d(nights), room=room, status="Checked In",
		                        auto_price=1, over_capacity_guests=1, over_capacity_reason="Extra bed", **args)
		billed = frappe.get_doc("Folio", guest_folio(stay.name))
		guest_tax = total = Decimal(0)
		expected_lines = []
		tariffs = (198, 165) if markup else (180, 216)
		slabs = (18, 5) if markup else (5, 18)
		for i, (tariff, slab) in enumerate(zip(tariffs[:nights], slabs[:nights], strict=True)):
			folio.post_room_night(stay, d(i))
			ratio = Decimal(slab) / 100
			guest_tax += 5 * ratio
			expected_lines.append((d(i), 5, slab, 5 * ratio))
			total += tariff + 5 + 5 * ratio
			if not inclusive:
				inclusion = 50 if package and i == 0 else 0
				total += (tariff - inclusion) * ratio + Decimal(inclusion) * Decimal("0.05")
		expected = total.quantize(self.unit, rounding=ROUND_HALF_UP)
		billed.reload()
		actual = [Decimal(str(v)) for v in (quote["amount_after_tax"], stay.reload().amount_after_tax,
		                                   billed.grand_total)]
		self.assertEqual(actual, [expected] * 3)
		extra = next(row for row in quote["line_items"] if row["component"] == "over_capacity")
		self.assertEqual((extra["amount"], Decimal(str(extra["tax_amount"]))), (nights * 5, guest_tax))
		self.assertEqual(sorted((str(c.posting_date), c.amount, c.gst_rate, Decimal(str(c.gst_amount)))
		                        for c in billed.charges if c.charge_type == "Extra Guests"), expected_lines)

	def test_exclusive_package_discount(self):
		self.check_stay(True, False, False)

	def test_exclusive_package_markup(self):
		self.check_stay(True, False, True)

	def test_inclusive_package_discount(self):
		self.check_stay(True, True, False)

	def test_inclusive_package_markup(self):
		self.check_stay(True, True, True)

	def test_exclusive_standard_discount(self):
		self.check_stay(False, False, False)

	def test_exclusive_standard_markup(self):
		self.check_stay(False, False, True)

	def test_inclusive_standard_discount(self):
		self.check_stay(False, True, False)

	def test_inclusive_standard_markup(self):
		self.check_stay(False, True, True)


class TestUSD(ExtraGuestSlabs, AuditTestCase):
	currency, unit = "USD", Decimal("0.01")

	def test_194_25_single_night(self):
		self.check_stay(True, False, False, nights=1)


class TestUGX(ExtraGuestSlabs, AuditTestCase):
	currency, unit = "UGX", Decimal("1")


class TestKWD(ExtraGuestSlabs, AuditTestCase):
	currency, unit = "KWD", Decimal("0.001")
