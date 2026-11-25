"""A taxable levy at a half unit uses the net rate the independent room posted."""

from decimal import Decimal
from unittest.mock import patch

import frappe

from kamra import folio, ledger, shares
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, guest_folio, make_guest, make_property, make_reservation,
	make_room, make_room_type,
)

# UGX levies retain hundredths of a shilling. At 1.1%, a 9,500 gross
# night carries a 95 levy and 9.5 levy GST, the equivalent half-unit boundary.
CASES = (
	("USD", ("33.06", "33.06", "37.87"), 1, "105.03"),
	("KWD", ("3.306", "3.306", "3.787"), 1, "10.503"),
	("UGX", ("3100", "3100", "3300"), 1.1, "9604"),
)


class TestPostedCustomNet(AuditTestCase):
	properties = tuple(f"UT R9 {code}" for code, *_ in CASES)

	def test_custom_total_matches_posted_control_on_audit_and_retry(self):
		self.at(TODAY)
		ledger.ensure_default_transaction_codes()
		for code, prices, percent, expected in CASES:
			with self.subTest(currency=code):
				self.check_night(code, tuple(map(Decimal, prices)), percent, Decimal(expected))

	def check_night(self, code, prices, percent, expected):
		prop = make_property(f"UT R9 {code}", currency=code, business_date=TODAY,
		                     rates_include_tax=1, room_levy_percent=percent, room_levy_taxable=1)
		rt = make_room_type(prop, base_price=float(sum(prices)), tax_percent=10)
		primary = make_reservation(prop, rt, TODAY, d(3), room=make_room(prop, rt, "101"))
		names = [primary.name]
		for guest in ("Second", "Third"):
			names.append(shares.create_share_reservation(primary.name, make_guest(guest))["reservation"])
		shares.update_share_rate_mode(primary.name, "Custom", dict(zip(names, map(float, prices), strict=True)))
		# Bill the same inclusive tariff through the standard nightly rate lookup.
		control = make_reservation(prop, rt, TODAY, d(3), room=make_room(prop, rt, "102"))
		for name in [*names, control.name]:
			stay = frappe.get_doc("Reservation", name)
			stay.status = "Checked In"
			stay.save()

		# A reservation without locked nightly rows uses the inclusive tariff on
		# its room type. Exercise the native lookup, conversion and folio posting.
		with patch("kamra.folio.locked_night", return_value=None):
			self.assertEqual(ledger.force_advance_bill(primary.name, "tonight")["nights_posted"], [TODAY])
			folio.run_night_audit(prop, TODAY)
		first = [self.posted(name) for name in [*names, control.name]]
		self.assertTrue(folio.run_night_audit(prop, TODAY)["already_ran"])
		self.assertEqual([self.posted(name) for name in [*names, control.name]], first)
		self.assertEqual(first[-1][0], expected)
		self.assertEqual(sum(total for total, _ in first[:-1]), first[-1][0])

	def posted(self, name):
		bill = frappe.get_doc("Folio", guest_folio(name))
		self.assertCountEqual([c.charge_type for c in bill.charges], ["Room", "Room Levy"])
		return Decimal(str(bill.grand_total)), [(c.name, c.amount, c.gst_amount) for c in bill.charges]
