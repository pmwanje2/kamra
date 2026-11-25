"""Compare shared advance bills to native, independently billed room nights."""

from decimal import Decimal
from unittest.mock import patch

import frappe

from kamra import ledger, shares
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, charges, d, guest_folio, make_guest, make_property,
	make_reservation, make_room, make_room_type,
)

UNITS = {"UGX": Decimal("1"), "USD": Decimal(".01"), "KWD": Decimal(".001")}


class TestR7ShareBilling(AuditTestCase):
	properties = tuple(f"UT R7 {code}" for code in UNITS)

	def setUp(self):
		super().setUp()
		self.at(TODAY)
		ledger.ensure_default_transaction_codes()

	def bookings(self, code, mode, **levy):
		prop = make_property(f"UT R7 {code}", currency=code, business_date=TODAY,
		                     gst_rate_low=0, gst_rate_high=0, **levy)
		rt = make_room_type(prop)
		a = make_reservation(prop, rt, TODAY, d(3), room=make_room(prop, rt, "101"))
		b = shares.create_share_reservation(a.name, make_guest("Sharer"))["reservation"]
		shares.update_share_rate_mode(a.name, mode)
		control = make_reservation(prop, rt, TODAY, d(3), room=make_room(prop, rt, "102"))
		return b, a.name, control.name

	def bills(self, bookings, rate):
		with (patch("kamra.folio._nightly_room_rate", return_value=float(rate)),
		      patch("kamra.folio._nightly_gst", return_value=10)):
			for name in bookings:
				self.assertEqual(ledger.force_advance_bill(name, "tonight")["nights_posted"], [TODAY])
		return [Decimal(str(frappe.db.get_value("Folio", guest_folio(n), "grand_total"))) for n in bookings]

	def test_full_rate_each_pays_independent_exclusive_gst(self):
		for code, unit in UNITS.items():
			with self.subTest(currency=code):
				bills = self.bills(self.bookings(code, "Full Rate Each"), 1005 * unit)
				self.assertEqual(bills, [1106 * unit] * 3)

	def test_full_rate_each_pays_the_native_percentage_and_fixed_levy(self):
		for code, unit in UNITS.items():
			with self.subTest(currency=code):
				bookings = self.bookings(code, "Full Rate Each", room_levy_percent=1,
				                         room_levy_per_night=float(2 * unit), room_levy_taxable=1)
				b, a, control = self.bills(bookings, 1005 * unit)
				self.assertEqual((a, b), (control, control))
				control_line, = charges(bookings[-1], "Room Levy")
				for stay in bookings[:2]:
					line, = charges(stay, "Room Levy")
					self.assertEqual(line.amount, control_line.amount)

	def test_taxable_split_levy_matches_native_document_rounding(self):
		for code, unit in UNITS.items():
			with self.subTest(currency=code):
				# UGX retains fractional shillings on the taxable levy. At
				# 2/3 decimals the room and levy GST can both have half a minor unit.
				base, due = (450, 500) if code == "UGX" else (455, 506)
				bookings = self.bookings(code, "Split Equal", room_levy_percent=1, room_levy_taxable=1)
				b, a, control = self.bills(bookings, base * unit)
				self.assertEqual(control, due * unit)
				self.assertEqual(a + b, control)
