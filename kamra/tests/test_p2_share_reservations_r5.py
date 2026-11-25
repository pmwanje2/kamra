"""Advance bills and partly posted nights must conserve the shared room rate."""

from decimal import Decimal
from unittest.mock import patch

import frappe

from kamra import folio, ledger, shares
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, charges, d, make_guest, make_property, make_reservation, make_room,
	make_room_type,
)

# Odd minor-unit totals expose penny and fils drift as well as whole-unit rounding.
CASES = (
	("UGX", "101", "51", "50"),
	("USD", "100.01", "50.01", "50.00"),
	("KWD", "100.001", "50.001", "50.000"),
)


class ShareBillingR5Tests(AuditTestCase):
	properties = tuple(f"UT Share R5 {currency}" for currency, *_ in CASES)

	def setUp(self):
		super().setUp()
		self.at(TODAY)
		ledger.ensure_default_transaction_codes()

	def pair(self, currency):
		prop = make_property(f"UT Share R5 {currency}", currency=currency, business_date=TODAY,
		                     gst_rate_low=0, gst_rate_high=0)
		room_type = make_room_type(prop)
		room = make_room(prop, room_type, "101")
		primary = make_reservation(prop, room_type, TODAY, d(2), room=room)
		peer = shares.create_share_reservation(primary.name, make_guest("Sharer"))["reservation"]
		return primary.reload(), frappe.get_doc("Reservation", peer)

	def test_confirmed_advance_bills_split_each_night_once(self):
		for currency, rate, lead, other in CASES:
			with self.subTest(currency=currency), patch("kamra.folio._nightly_room_rate", return_value=float(rate)):
				a, b = self.pair(currency)
				self.assertFalse(folio.post_room_night(a, TODAY))
				for stay, expected in ((b, other), (a, lead)):
					bill = ledger.force_advance_bill(stay.name)
					self.assertEqual(bill["nights_posted"], [TODAY, d(1)])
					self.assertEqual(Decimal(str(bill["amount"])), 2 * Decimal(expected))
					self.assertEqual([Decimal(str(r.amount)) for r in charges(stay.name, "Room")],
					                 [Decimal(expected)] * 2)
					self.assertEqual(ledger.force_advance_bill(stay.name)["nights_posted"], [])

	def test_break_waits_for_prior_night_then_next_night_bills_unshared(self):
		for currency, rate, lead, other in CASES:
			with self.subTest(currency=currency), patch("kamra.folio._nightly_room_rate", return_value=float(rate)):
				a, b = self.pair(currency)
				for stay in (a, b):
					stay.status = "Checked In"
					stay.save()
				self.assertTrue(folio.post_room_night(a, TODAY))
				new_room = make_room(a.property, a.room_type, "102")
				frappe.db.set_value("Property", a.property, "business_date", d(1))
				with self.assertRaises(frappe.ValidationError):
					shares.break_share(a.name, new_room)
				self.assertEqual(a.reload().share_group_id, b.reload().share_group_id)
				self.assertTrue(folio.post_room_night(b, TODAY))
				for stay, expected in ((a, lead), (b, other)):
					line, = charges(stay.name, "Room")
					self.assertEqual(Decimal(str(line.amount)), Decimal(expected))
				shares.break_share(a.name, new_room)
				for stay in (a, b):
					self.assertFalse(stay.reload().share_group_id)
					self.assertTrue(folio.post_room_night(stay, d(1)))
					self.assertEqual({str(r.posting_date): Decimal(str(r.amount)) for r in charges(stay.name, "Room")}[d(1)],
					                 Decimal(rate))
