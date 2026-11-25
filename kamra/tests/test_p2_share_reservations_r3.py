"""R3 regressions for shared-room turnover and physical capacity."""

from unittest.mock import patch

import frappe

from kamra import api, shares
from kamra.tests.audit_fixtures import (
	AuditTestCase,
	d,
	make_guest,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

PROPERTY = "UT Share R3 Hotel"


class TestP2ShareReservationsR3(AuditTestCase):
	properties = (PROPERTY,)

	def setUp(self):
		super().setUp()
		make_property(PROPERTY, currency="USD", gst_rate_low=0, gst_rate_high=0)
		self.room_type = make_room_type(PROPERTY, adults_capacity=3, max_total_occupants=3)
		self.room = make_room(PROPERTY, self.room_type, "101")
		self.primary = make_reservation(PROPERTY, self.room_type, d(0), d(2), room=self.room)

	def test_turnover_uses_the_last_sharer_to_leave_in_either_order(self):
		last = shares.create_share_reservation(self.primary.name, make_guest("Late departure"))["reservation"]
		incoming = make_reservation(PROPERTY, self.room_type, d(2), d(4), room=self.room)
		frappe.db.set_value("Reservation", self.primary.name, "planned_check_out_time", "09:00:00")
		frappe.db.set_value("Reservation", last, "planned_check_out_time", "12:00:00")
		frappe.db.set_value("Reservation", incoming.name, "planned_check_in_time", "11:00:00")
		original_get_all = frappe.get_all

		def bookings_in_order(doctype, *args, **kwargs):
			rows = original_get_all(doctype, *args, **kwargs)
			if doctype == "Reservation" and "planned_check_out_time" in (kwargs.get("fields") or ()):
				return sorted(rows, key=lambda row: row.name, reverse=reverse)
			return rows

		for start in (d(0), d(2)):
			for reverse in (False, True):
				with self.subTest(start=start, reverse=reverse):
					with patch.object(frappe, "get_all", side_effect=bookings_in_order):
						chart = api.tape_chart(PROPERTY, start, days=4)
					self.assertEqual(
						[(c["out_res"], c["in_res"], c["etd"], c["eta"]) for c in chart["conflicts"]],
						[(last, incoming.name, "12:00", "11:00")],
					)
					if start == d(2):
						self.assertEqual([b.name for b in chart["rooms"][0]["bookings"]], [incoming.name])

	def test_share_capacity_counts_bed_occupants_and_excludes_infants(self):
		other_room = make_room(PROPERTY, self.room_type, "102")
		sharer = make_reservation(
			PROPERTY, self.room_type, d(0), d(2), room=other_room,
			adults=1, children=1, infants=1,
		)
		shares.combine_into_share(self.primary.name, sharer.name)
		self.assertEqual(sharer.reload().room, self.room)
		self.assertEqual(sharer.infants, 1)
		sharer.infants = 0
		with self.assertRaises(frappe.ValidationError):
			sharer.save()
