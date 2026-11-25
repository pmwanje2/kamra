"""A day-use arrival can overlap the last departure from a shared room."""

import frappe

from kamra import api, shares
from kamra.tests.audit_fixtures import (
	AuditTestCase, d, make_guest, make_property, make_reservation, make_room, make_room_type,
)


class TestShareDayUseTurnover(AuditTestCase):
	properties = ("UT Share R4 Hotel",)

	def test_day_use_arrival_conflicts_with_last_departing_sharer(self):
		prop, = self.properties
		make_property(prop)
		room_type = make_room_type(prop)
		room = make_room(prop, room_type, "201")
		early = make_reservation(prop, room_type, d(-1), d(0), room=room)
		late = shares.create_share_reservation(early.name, make_guest("Late departure"))["reservation"]
		frappe.db.set_value("Reservation", early.name, "planned_check_out_time", "08:30:00")
		frappe.db.set_value("Reservation", late, "planned_check_out_time", "09:30:00")
		hourly = frappe.get_doc({
			"doctype": "Rate Plan", "property": prop, "code": "R4HR",
			"rate_plan_name": "Hourly", "billing_basis": "Hourly", "hourly_rate": 200,
			"modifier_type": "Percent", "modifier_value": 0,
		}).insert()
		arrival = make_reservation(
			prop, room_type, d(0), d(0), room=room, is_day_use=1, rate_plan=hourly.name,
			planned_check_in_time="09:00:00", planned_check_out_time="13:00:00",
		)
		for start in (d(-1), d(0)):
			with self.subTest(start=start):
				chart = api.tape_chart(prop, start_date=start, days=2)
				self.assertIn(arrival.name, [b.name for b in chart["rooms"][0]["bookings"]])
				self.assertEqual(
					[(c["out_res"], c["in_res"], c["etd"], c["eta"]) for c in chart["conflicts"]],
					[(late, arrival.name, "09:30", "09:00")],
				)
