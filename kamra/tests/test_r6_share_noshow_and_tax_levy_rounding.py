"""Frozen shares follow non-arrival policy and conserve room-night taxes and levies."""

from decimal import Decimal
from unittest.mock import patch

import frappe

from kamra import api, folio, ledger, night_audit, shares
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, charges, d, guest_folio, make_guest, make_property,
	make_reservation, make_room, make_room_type,
)

UNITS = {"UGX": Decimal("1"), "USD": Decimal(".01"), "KWD": Decimal(".001")}


class TestR6ShareBilling(AuditTestCase):
	properties = tuple(f"UT Share R6 {code} {case}" for code in UNITS
	                   for case in ("Tax", "Levy", "No Show", "Cancelled"))

	def setUp(self):
		super().setUp()
		self.at(TODAY)
		ledger.ensure_default_transaction_codes()

	def pair(self, code, case, **settings):
		prop = make_property(f"UT Share R6 {code} {case}", currency=code, business_date=TODAY,
		                     gst_rate_low=0, gst_rate_high=0, **settings)
		rt = make_room_type(prop)
		a = make_reservation(prop, rt, TODAY, d(3), room=make_room(prop, rt, "101"))
		b = shares.create_share_reservation(a.name, make_guest("Sharer"))["reservation"]
		return a.reload(), frappe.get_doc("Reservation", b)

	def test_nonarrival_releases_only_its_unposted_room_allocation(self):
		for code, unit in UNITS.items():
			for status in ("No Show", "Cancelled"):
				with self.subTest(currency=code, status=status):
					a, b = self.pair(code, status)
					stays = [a]
					if status == "No Show":
						c = shares.create_share_reservation(a.name, make_guest("Third guest"))["reservation"]
						stays.append(frappe.get_doc("Reservation", c))
					for stay in stays:
						stay.reload()
						stay.status = "Checked In"
						stay.save()
					with patch("kamra.folio._nightly_room_rate", return_value=float(3000 * unit)):
						ledger.force_advance_bill(a.name, "tonight")
					original, = charges(a.name, "Room")
					if status == "No Show":
						night_audit.eod_mark_no_show(b.name)
					else:
						api.cancel_reservation(b.name, waive_fee=1)
					self.assertEqual(b.reload().status, status)
					folio.run_night_audit(a.property, TODAY)
					self.assertEqual(charges(a.name, "Room"), [original])
					self.assertEqual(charges(b.name, "Room"), [])
					for stay in stays:
						line, = charges(stay.name, "Room")
						self.assertEqual(Decimal(str(line.amount)), 3000 * unit / (len(stays) + 1))
					shares.break_share(a.name, make_room(a.property, a.room_type, "102"))
					self.assertFalse(a.reload().share_group_id)

	def test_exclusive_gst_is_conserved_across_two_bills(self):
		for code, unit in UNITS.items():
			with self.subTest(currency=code):
				a, b = self.pair(code, "Tax")
				with (patch("kamra.folio._nightly_room_rate", return_value=float(1010 * unit)),
				      patch("kamra.folio._nightly_gst", return_value=10)):
					for stay in (b, a):
						ledger.force_advance_bill(stay.name, "tonight")
				for stay, due in ((a, 556 * unit), (b, 555 * unit)):
					bill = frappe.get_doc("Folio", guest_folio(stay.name))
					self.assertEqual(Decimal(str(bill.grand_total)), due)
					self.assertEqual(Decimal(str(bill.charges_total)), 505 * unit)
					self.assertEqual(Decimal(str(bill.tax_total)), due - 505 * unit)

	def test_levy_rounds_once_and_fixed_part_posts_only_on_primary(self):
		for code, unit in UNITS.items():
			with self.subTest(currency=code):
				a, b = self.pair(code, "Levy", room_levy_percent=1,
				                 room_levy_per_night=float(2 * unit), room_levy_taxable=0)
				with patch("kamra.folio._nightly_room_rate", return_value=float(10100 * unit)):
					ledger.force_advance_bill(b.name, "tonight")
					ledger.force_advance_bill(a.name, "tonight")
					self.assertEqual(ledger.force_advance_bill(b.name, "tonight")["nights_posted"], [])
				for stay, expected in ((a, 53 * unit), (b, 50 * unit)):
					line, = charges(stay.name, "Room Levy")
					self.assertEqual(Decimal(str(line.amount)), expected)
