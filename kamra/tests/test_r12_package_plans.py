"""Inclusive package plans preserve the guest's gross and each component's tax."""
from decimal import Decimal, ROUND_HALF_UP

import frappe

from kamra import folio
from kamra.tests import test_r10_room_move_posting as fixtures
from kamra.tests.audit_fixtures import AuditTestCase, PROP_A, TODAY, d, make_reservation


class PackagePlanCases:
	start = fixtures.TestR10Posting.start
	package = fixtures.TestR10Posting.package
	bill = fixtures.TestR10Posting.bill

	def check_plan(self, kind, value, gross, step=0):
		self.start(self.currency, inclusive=1)
		pkg = self.package("Package and plan", self.types[1], float(self.sold))
		element = pkg.elements[0]
		element.included_in_rate, element.gst_rate, element.unit_allowance = 1, 5, 50
		pkg.save()
		plan = frappe.get_doc({
			"doctype": "Rate Plan", "property": PROP_A, "currency": self.currency,
			"code": "BUNDLE", "rate_plan_name": "Package plan", "modifier_type": kind,
			"modifier_value": value, "rounding_step": step,
		}).insert()
		self.stay = make_reservation(PROP_A, self.types[1], TODAY, d(1), status="Checked In",
		                             room=self.rooms[1], adults=1, auto_price=1, rate_plan=plan.name)
		self.stay.reload()
		folio.post_room_night(self.stay, TODAY)
		bill = self.bill()
		lines = {row.charge_type: row for row in bill.charges}
		def money(amount):
			return amount.quantize(self.unit, rounding=ROUND_HALF_UP)
		gross = money(gross)
		element_tax = money(Decimal("2.50"))
		room_gross = gross - 50 - element_tax
		room_net = money(room_gross / Decimal("1.18"))
		actual = (self.stay.amount_after_tax, bill.grand_total, lines["Room"].amount,
		          lines["Room"].gst_amount, lines["Taxi"].amount, lines["Taxi"].gst_amount)
		self.assertEqual(tuple(Decimal(str(a)) for a in actual),
		                 (gross, gross, room_net, room_gross - room_net, 50, element_tax))

	def test_neutral(self):
		self.check_plan("Percent", 0, self.sold)

	def test_discount(self):
		self.check_plan("Percent", -10, self.sold * Decimal("0.90"))

	def test_markup(self):
		self.check_plan("Percent", 10, self.sold * Decimal("1.10"))

	def test_fixed_amount(self):
		self.check_plan("Amount", 11, self.sold + 11)

	def test_commercial_rounding(self):
		gross = (self.sold / 7).quantize(Decimal(1), rounding=ROUND_HALF_UP) * 7
		self.check_plan("Percent", 0, gross, step=7)


class TestShillings(PackagePlanCases, AuditTestCase):
	currency, sold, unit = "UGX", Decimal("40003"), Decimal(1)


class TestDollars(PackagePlanCases, AuditTestCase):
	currency, sold, unit = "USD", Decimal("1200.07"), Decimal("0.01")


class TestDinars(PackagePlanCases, AuditTestCase):
	currency, sold, unit = "KWD", Decimal("200.001"), Decimal("0.001")
