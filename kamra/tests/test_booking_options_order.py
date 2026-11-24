"""booking_options room-type order is stable and cheapest-first (#549)."""

from __future__ import annotations

from kamra.api import availability_calendar, available_rooms, booking_options, create_booking
from kamra.crs import crs_search
from kamra.tests.audit_fixtures import (
	PROP_A, TODAY, AuditTestCase, d, make_property, make_room, make_room_type,
)


class TestBookingOptionsOrder(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		make_property(PROP_A, business_date=TODAY)
		# same price → name must break the tie; Alpha before Zulu
		self.rt_z = make_room_type(PROP_A, "ZUL", base_price=1000,
		                           room_type_name="Zulu")
		self.rt_a = make_room_type(PROP_A, "ALP", base_price=1000,
		                           room_type_name="Alpha")
		self.rt_dear = make_room_type(PROP_A, "DLX", base_price=2500,
		                              room_type_name="Deluxe")
		# cheaper than all of them, but has zero physical rooms: never offered
		self.rt_empty = make_room_type(PROP_A, "EMP", base_price=500,
		                               room_type_name="Empty")
		make_room(PROP_A, self.rt_a, "BO102")
		make_room(PROP_A, self.rt_z, "BO103")
		make_room(PROP_A, self.rt_dear, "BO101")

	def test_room_types_sorted_by_price_then_name(self):
		names = [rt["name"] for rt in booking_options(PROP_A)["room_types"]]
		# Alpha and Zulu share base_price; Alpha comes first by name; Empty (0 rooms) is never offered
		self.assertEqual(names, [self.rt_a, self.rt_z, self.rt_dear])
		self.assertNotIn(self.rt_empty, names)

	def test_empty_types_have_no_available_rooms(self):
		import frappe
		ci, co = d(1), d(2)
		self.assertEqual(available_rooms(PROP_A, self.rt_empty, ci, co), [])
		self.assertEqual(
			[r["name"] for r in available_rooms(PROP_A, self.rt_dear, ci, co)],
			[f"{PROP_A}-BO101"])
		cal_types = [r["room_type"] for r in availability_calendar(PROP_A, ci, 3)["room_types"]]
		self.assertNotIn(self.rt_empty, cal_types)
		self.assertIn(self.rt_dear, cal_types)
		crs_types = [rt["room_type"] for p in crs_search(ci, co)["properties"] for rt in p["room_types"]]
		self.assertNotIn(self.rt_empty, crs_types)
		with self.assertRaises(frappe.ValidationError):
			create_booking(PROP_A, self.rt_empty, ci, co, guest_name="Zero Room Guest", phone="9990001111")
