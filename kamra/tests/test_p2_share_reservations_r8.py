"""Custom room pricing must reconcile to an independent room after night audit."""

from decimal import Decimal
from unittest.mock import patch

import frappe

from kamra import folio, ledger, shares
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, guest_folio, make_guest, make_property, make_reservation,
	make_room, make_room_type,
)

UNITS = {"UGX": Decimal("1"), "USD": Decimal(".01"), "KWD": Decimal(".001")}


class TestCustomRoomNight(AuditTestCase):
	properties = tuple(f"UT R8 {code} {inclusive}" for code in UNITS for inclusive in (0, 1))

	def setUp(self):
		super().setUp()
		self.at(TODAY)
		ledger.ensure_default_transaction_codes()

	def test_exclusive_gst_and_taxable_levy(self):
		for code in UNITS:
			with self.subTest(currency=code):
				prices, due = ((3349,) * 3, 11162) if code == "UGX" else ((3485,) * 3, 11616)
				self.check_bill(code, 0, prices, due)

	def test_inclusive_gst_and_taxable_levy(self):
		for code in UNITS:
			with self.subTest(currency=code):
				prices, due = ((3350, 3350, 3351), 10152) if code == "UGX" else ((3306, 3306, 4443), 11166)
				self.check_bill(code, 1, prices, due)

	def check_bill(self, code, inclusive, prices, due):
		unit = UNITS[code]
		prop = make_property(f"UT R8 {code} {inclusive}", currency=code, business_date=TODAY,
		                     rates_include_tax=inclusive, room_levy_percent=1, room_levy_taxable=1,
		                     gst_rate_low=0, gst_rate_high=0)
		rt = make_room_type(prop)
		primary = make_reservation(prop, rt, TODAY, d(3), room=make_room(prop, rt, "101"))
		names = [primary.name]
		for guest in ("Second", "Third"):
			names.append(shares.create_share_reservation(primary.name, make_guest(guest))["reservation"])
		custom = {name: float(price * unit) for name, price in zip(names, prices, strict=True)}
		shares.update_share_rate_mode(primary.name, "Custom", custom)
		control = make_reservation(prop, rt, TODAY, d(3), room=make_room(prop, rt, "102"))
		for name in [*names, control.name]:
			stay = frappe.get_doc("Reservation", name)
			stay.status = "Checked In"
			stay.save()

		base = sum(prices) * unit
		if inclusive:
			base /= Decimal("1.1")
		with (patch("kamra.folio._nightly_room_rate", return_value=float(base)),
		      patch("kamra.folio._nightly_gst", return_value=10)):
			self.assertEqual(ledger.force_advance_bill(primary.name, "tonight")["nights_posted"], [TODAY])
			folio.run_night_audit(prop, TODAY)
			first = [self.bill(name) for name in [*names, control.name]]
			self.assertTrue(folio.run_night_audit(prop, TODAY)["already_ran"])
			self.assertEqual([self.bill(name) for name in [*names, control.name]], first)

		for (_, lines), price in zip(first[:3], prices, strict=True):
			room = next(line for line in lines if line[1] == "Room")
			self.assertEqual(room[2] + (room[3] if inclusive else 0), price * unit)
		self.assertEqual(first[-1][0], due * unit)
		self.assertEqual(sum(total for total, _ in first[:3]), first[-1][0])

	def bill(self, name):
		bill = frappe.get_doc("Folio", guest_folio(name))
		lines = [(row.name, row.charge_type, Decimal(str(row.amount)), Decimal(str(row.gst_amount)))
		         for row in bill.charges]
		self.assertCountEqual([row[1] for row in lines], ["Room", "Room Levy"])
		return Decimal(str(bill.grand_total)), lines
