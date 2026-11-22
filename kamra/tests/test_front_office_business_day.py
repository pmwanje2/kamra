"""The staffed boards and the flash agree before End of Day rolls the property."""

from unittest.mock import patch

import frappe

from kamra.api import front_desk_snapshot, hk_update_task, room_board
from kamra.dashboards import property_dashboard
from kamra.reports import manager_flash, movement_counts
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)


class TestFrontOfficeBusinessDay(AuditTestCase):
	def setUp(self):
		super().setUp()
		make_property(PROP_A, business_date=TODAY)
		self.rt = make_room_type(PROP_A)
		self.room = make_room(PROP_A, self.rt, "A-12")

	def test_snapshot_explicit_and_single_implicit_property(self):
		with patch("kamra.api.my_properties", return_value=[frappe._dict(name=PROP_A)]):
			for prop in (PROP_A, None):
				snap = front_desk_snapshot(prop)
				self.assertEqual((snap["date"], snap["business_date"]), (TODAY, TODAY))

	def test_implicit_property_keeps_strict_scope(self):
		# The role gate runs before a missing property is resolved. Recheck the
		# resolved property even if the list query returns a candidate.
		with (
			patch("kamra.authz.restricted_properties", return_value=set()),
			patch("kamra.api.my_properties", return_value=[frappe._dict(name=PROP_A)]),
		):
			with self.assertRaises(frappe.PermissionError):
				front_desk_snapshot()

	def test_movement_buckets_and_legacy_fallback(self):
		names = []
		for i, (scheduled, actual, status) in enumerate(
			[
				(-1, None, "Checked In"),
				(0, None, "Checked In"),
				(1, None, "Checked In"),
				(0, None, "Checked Out"),
				(1, TODAY, "Checked Out"),
				(0, d(-1), "Checked Out"),
			]
		):
			room = self.room if i == 0 else make_room(PROP_A, self.rt, f"A-{20 + i}")
			res = make_reservation(PROP_A, self.rt, d(-3), d(scheduled), room=room)
			frappe.db.set_value(
				"Reservation", res.name, {"status": status, "check_out_business_date": actual}
			)
			names.append(res.name)
		snap = front_desk_snapshot(PROP_A)
		for key, expected in [
			("departures_overdue", names[:1]),
			("departures_due", names[1:2]),
			("checked_out", names[3:5]),
		]:
			self.assertEqual({r.name for r in snap.get(key, [])}, set(expected), key)
		self.assertEqual(snap["departures_overdue"][0].room_number, "A-12")
		for counts in [movement_counts(PROP_A, TODAY, TODAY), property_dashboard(PROP_A)["front_desk"]]:
			self.assertEqual(
				[counts[k] for k in ("departures", "departures_overdue", "checked_out")], [1, 1, 2]
			)

	def test_flash_and_dashboard_expose_explicit_basis(self):
		for read in (manager_flash, property_dashboard):
			for day, basis in [(None, "projected"), (d(-1), "actual"), (d(1), "projected")]:
				r = read(PROP_A, day)
				self.assertEqual(
					(r.get("report_date"), r.get("business_date"), r.get("basis")),
					(day or TODAY, TODAY, basis),
				)

	def test_only_current_open_cleans_are_actionable(self):
		def task(kind, day):
			return (
				frappe.get_doc(
					dict(
						doctype="Housekeeping Task",
						room=self.room,
						property=PROP_A,
						task_type=kind,
						status="Pending",
						due_by=f"{day} 12:00:00",
					)
				)
				.insert()
				.name
			)

		old = task("Checkout Clean", d(-1))
		one = task("Checkout Clean", TODAY)
		two = task("Stayover Clean", TODAY)
		task("Inspection", TODAY)

		def actionable():
			return {t["name"] for t in room_board(PROP_A)["rooms"][0].get("clean_tasks", [])}

		self.assertEqual(actionable(), {one, two})
		hk_update_task(one, "Done")
		self.assertEqual(actionable(), {two})
		self.assertEqual(frappe.db.get_value("Room", self.room, "housekeeping_status"), "Clean")
		self.assertEqual(frappe.db.get_value("Housekeeping Task", old, "status"), "Pending")
