"""Share reservations through the desk, inventory and night-audit entry points."""

from decimal import Decimal

import frappe

from kamra import api, folio
from kamra.ledger import ensure_default_transaction_codes
from kamra.pricing import forecast_occupancy
from kamra.reports import _day_stats
from kamra.tests.audit_fixtures import (
	TODAY,
	AuditTestCase,
	charges,
	d,
	guest_folio,
	make_guest,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

PROPERTY = "UT P2 Share Hotel"


class TestP2ShareReservations(AuditTestCase):
	properties = (PROPERTY,)

	def setUp(self):
		super().setUp()
		self.at(TODAY)
		ensure_default_transaction_codes()
		make_property(PROPERTY, currency="USD", gst_rate_low=0, gst_rate_high=0, business_date=TODAY)
		self.room_type = make_room_type(PROPERTY, base_price=101)
		self.room = make_room(PROPERTY, self.room_type, "101")
		self.primary = make_reservation(PROPERTY, self.room_type, d(0), d(4), room=self.room)

	def add_sharer(self, start=None, end=None):
		from kamra.shares import create_share_reservation

		res = create_share_reservation(self.primary.name, make_guest("Sharer"), start or d(0), end or d(4))
		return frappe.get_doc("Reservation", res["reservation"])

	def check_in(self, *reservations):
		for res in reservations:
			res.reload()
			res.status = "Checked In"
			res.save()

	def test_shared_room_conflicts_and_inventory(self):
		secondary = self.add_sharer(d(1), d(3))
		self.assertEqual(secondary.room, self.room)
		self.assertNotEqual(secondary.guest, self.primary.guest)
		self.assertEqual(secondary.share_group_id, self.primary.reload().share_group_id)
		self.assertNotEqual(guest_folio(secondary.name), guest_folio(self.primary.name))
		with self.assertRaises(frappe.ValidationError):
			make_reservation(PROPERTY, self.room_type, d(1), d(2), room=self.room)
		make_room(PROPERTY, self.room_type, "102")
		self.assertEqual(forecast_occupancy(PROPERTY, d(1)), 50)
		cal = api.availability_calendar(PROPERTY, d(1), 1)
		self.assertEqual(cal["room_types"][0]["cells"][0]["available"], 1)

	def test_split_conserves_minor_units_and_all_modes(self):
		from kamra.shares import allocate_rates

		rows = [("A", True, "10.001"), ("B", False, "20.002")]
		for currency, amount, a, b in [
			("UGX", "101", "51", "50"),
			("USD", "100.01", "50.01", "50.00"),
			("KWD", "100.001", "50.001", "50.000"),
		]:
			with self.subTest(currency=currency):
				split = allocate_rates(amount, currency, "Split Equal", rows)
				self.assertEqual(split, {"A": Decimal(a), "B": Decimal(b)})
				self.assertEqual(sum(split.values()), Decimal(amount))
		self.assertEqual(allocate_rates("100.001", "KWD", "Primary Pays", rows),
		                 {"A": Decimal("100.001"), "B": Decimal(0)})
		self.assertEqual(allocate_rates("100.001", "KWD", "Full Rate Each", rows),
		                 {"A": Decimal("100.001"), "B": Decimal("100.001")})
		self.assertEqual(allocate_rates("100.001", "KWD", "Custom", rows),
		                 {"A": Decimal("10.001"), "B": Decimal("20.002")})
		with self.assertRaises(frappe.ValidationError):
			allocate_rates("1", "USD", "Custom", [("A", True, "-1")])

	def test_partial_overlap_and_night_audit(self):
		from kamra.shares import nightly_share_rate

		secondary = self.add_sharer(d(1), d(3))
		self.check_in(self.primary, secondary)
		self.assertEqual(nightly_share_rate(self.primary, d(0)), Decimal(101))
		self.assertEqual(nightly_share_rate(self.primary, d(1)), Decimal("50.50"))
		self.assertEqual(nightly_share_rate(secondary, d(2)), Decimal("50.50"))
		self.assertEqual(nightly_share_rate(self.primary, d(3)), Decimal(101))
		self.assertEqual(folio.run_night_audit(PROPERTY, d(0))["room_charges_posted"], 1)
		self.at(d(1))
		self.assertEqual(folio.run_night_audit(PROPERTY, d(1))["room_charges_posted"], 2)
		for res, expected in [(self.primary, [50.5, 101]), (secondary, [50.5])]:
			self.assertFalse(folio.post_room_night(res, d(1)))
			rows = charges(res.name, "Room")
			self.assertEqual(sorted(r.amount for r in rows), expected)
			self.assertEqual({r.parent for r in rows}, {guest_folio(res.name)})
		day = _day_stats(PROPERTY, d(1), 1)
		self.assertEqual((day["rooms_sold"], day["rooms_occupied"], day["occupancy_pct"]), (1, 1, 100))
		self.assertEqual((day["adr"], day["revpar"]), (101, 101))

	def test_break_keeps_folios_and_requires_nonconflicting_room(self):
		from kamra.shares import break_share, combine_into_share, update_share_rate_mode

		other_room = make_room(PROPERTY, self.room_type, "102")
		secondary = make_reservation(PROPERTY, self.room_type, d(1), d(3), room=other_room)
		combine_into_share(self.primary.name, secondary.name)
		update_share_rate_mode(self.primary.name, "Custom", {self.primary.name: 61, secondary.name: 40})
		self.assertEqual(secondary.reload().share_custom_rate, 40)
		bill = guest_folio(secondary.name)
		with self.assertRaises(frappe.ValidationError):
			break_share(secondary.name, self.room)
		break_share(secondary.name, other_room)
		self.assertFalse(secondary.reload().share_group_id)
		self.assertFalse(self.primary.reload().share_group_id)
		self.assertEqual(secondary.room, other_room)
		self.assertEqual(guest_folio(secondary.name), bill)
