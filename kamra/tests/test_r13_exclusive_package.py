"""Exclusive-tax package plans conserve the net price and each component's tax."""
from decimal import Decimal, ROUND_HALF_UP

import frappe

from kamra import folio, packages, pricing
from kamra.tests import test_r10_room_move_posting as fixtures
from kamra.tests.audit_fixtures import AuditTestCase, PROP_A, TODAY, d, make_reservation


class ExclusivePackageCases:
	start = fixtures.TestR10Posting.start
	package = fixtures.TestR10Posting.package
	bill = fixtures.TestR10Posting.bill

	def prepare(self, count):
		self.start(self.currency, inclusive=0)
		pkg = self.package("R13 exclusive package", self.types[1], 200)
		pkg.set("elements", [])
		for i in range(count):
			pkg.append("elements", {
				"element_name": f"Transfer {i + 1}", "element_type": "Transport",
				"transaction_code": "3200", "charge_type": "Taxi", "posting_rhythm": "Every Night",
				"allowance_basis": "Per Adult Night", "included_in_rate": 1, "unit_allowance": 50,
				"gst_rate": 5,
			})
		return pkg.save()

	def check_plan(self, kind, value, net, step=0, count=1, nights=1):
		self.prepare(count)
		plan = frappe.get_doc({
			"doctype": "Rate Plan", "property": PROP_A, "currency": self.currency,
			"code": "EXCL", "rate_plan_name": "R13 exclusive", "modifier_type": kind,
			"modifier_value": float(value), "rounding_step": step,
		}).insert()
		quote = pricing.quote(PROP_A, self.types[1], TODAY, d(nights), adults=1, rate_plan=plan.name)
		self.stay = make_reservation(PROP_A, self.types[1], TODAY, d(nights), status="Checked In",
		                             room=self.rooms[1], adults=1, rate_plan=plan.name, auto_price=1)
		for i in range(nights):
			folio.post_room_night(self.stay, d(i))
		bill = self.bill()
		rooms = [row for row in bill.charges if row.charge_type == "Room"]
		elements = [row for row in bill.charges if row.charge_type == "Taxi"]
		self.assertEqual((len(rooms), len(elements)), (nights, nights * count))
		allowed = min(net, 50 * count * nights)
		room_net = net - allowed
		room_tax = room_net * Decimal(".18")
		gross = (net + room_tax + allowed * Decimal(".05")).quantize(self.unit, ROUND_HALF_UP)
		self.assertTrue(all(0 <= Decimal(str(row.amount)) <= 50 for row in elements))
		self.assertEqual(sum(Decimal(str(row.amount)) for row in elements), allowed)
		self.assertTrue(all(row.amount >= 0 and row.gst_amount >= 0 for row in rooms))
		self.assertEqual(sum(Decimal(str(row.amount)) for row in rooms), room_net)
		self.assertEqual(sum(Decimal(str(row.gst_amount)) for row in rooms), room_tax)
		self.assertEqual(tuple(Decimal(str(n)) for n in
		                       (quote["amount_after_tax"], self.stay.reload().amount_after_tax, bill.grand_total)),
		                 (gross, gross, gross))

	def test_neutral(self):
		self.check_plan("Percent", 0, Decimal(200))

	def test_discount(self):
		self.check_plan("Percent", -10, Decimal(180))

	def test_markup(self):
		self.check_plan("Percent", 15, Decimal(230))

	def test_fixed(self):
		self.check_plan("Amount", 13, Decimal(213))

	def test_rounding(self):
		self.check_plan("Percent", 0, Decimal(203), step=7)

	def test_two_units_for_four_inclusions(self):
		self.check_plan("Absolute", 2 * self.unit, 2 * self.unit, count=4)

	def test_one_unit_for_four_inclusions(self):
		self.check_plan("Absolute", self.unit, self.unit, count=4)

	def test_stay_amount_with_an_odd_minor_unit(self):
		net = 400 + self.unit
		self.check_plan("Absolute", net, net, nights=2)

	def test_zero_net_for_four_inclusions(self):
		pkg = self.prepare(4)
		party = frappe._dict(property=PROP_A, adults=1, check_in_date=TODAY, check_out_date=d(1))
		split = packages.split_package_night(party, pkg, 0, TODAY, gst_rate=18)
		self.assertEqual(split["room_net"], 0)
		self.assertEqual([e["amount"] for e in split["elements"]], [0] * 4)


class TestShillings(ExclusivePackageCases, AuditTestCase):
	currency, unit = "UGX", Decimal(1)


class TestDollars(ExclusivePackageCases, AuditTestCase):
	currency, unit = "USD", Decimal(".01")


class TestDinars(ExclusivePackageCases, AuditTestCase):
	currency, unit = "KWD", Decimal(".001")
