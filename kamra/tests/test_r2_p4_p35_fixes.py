"""R2 regression tests for P4 (Traces/Alerts) and P35 (Out of Service / Pickup).

Reproduces every backend P0 and P1 finding from Hydrogen R1 review against live
Frappe v16 / MariaDB 11.8.9:
1. P0-1 (`eval_harness` t317): plain `Housekeeping` attendant MUST NOT create
   traces or stay instructions (`traces.add_trace` / `api.add_stay_instruction`
   must raise `PermissionError`), while retaining read (`stay_instructions`,
   `list_traces`) and resolve (`set_stay_instruction_status`, `resolve_trace`)
   access; `Housekeeping Supervisor` retains creation access.
2. P0-2 (`eval_harness` t384): `allocation.apply_allocation` skip reason for
   `Out of Order` and `Out of Service` rooms must preserve `"no longer free"`
   contract asserted by `t384` while still refusing assignment.
3. P1-1: `booking_requests.accept_booking_request` must never assign an
   `Out of Service` room — picking a free serviceable room when one exists, or
   confirming unassigned (`room=None`) when only `OS` rooms remain physically
   unbooked inside sellable capacity, and refusing once sellable capacity is 0.
4. P1-2: `api.move_reservation` for an in-house guest must preserve `Out of
   Service` and `Out of Order` on `old_room` (setting only `occupancy_status`
   to `Vacant`), while continuing to mark normal vacated rooms `Dirty`.
"""

from __future__ import annotations

import frappe

from kamra import allocation, api, booking_requests, traces
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	ensure_user,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)


def _grant_property_permission(user: str, prop: str) -> None:
	frappe.db.delete("User Permission", {"user": user, "allow": "Property"})
	frappe.get_doc({
		"doctype": "User Permission",
		"user": user,
		"allow": "Property",
		"for_value": prop,
	}).insert(ignore_permissions=True)
	frappe.clear_cache(user=user)


class R2P4P35FixesTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(
			TODAY,
			"kamra.folio",
			"kamra.business_date",
			"kamra.ledger",
			"kamra.checkout",
			"kamra.api",
			"kamra.booking_requests",
		)
		make_property(PROP_A, business_date=TODAY, overbooking_pct=0)
		self.rt = make_room_type(PROP_A, "STD", base_price=1200)
		self.rooms = [
			make_room(PROP_A, self.rt, num)
			for num in ("101", "102", "103", "104", "105")
		]
		self.desk = ensure_user("r2.desk@test.local", ["Front Desk"])
		self.attendant = ensure_user("r2.hk@test.local", ["Housekeeping"])
		self.hk_sup = ensure_user("r2.hksup@test.local", ["Housekeeping Supervisor"])
		self.supervisor = ensure_user(
			"r2.sup@test.local", ["Housekeeping Supervisor", "Front Desk"]
		)
		for user in (self.desk, self.attendant, self.hk_sup, self.supervisor):
			_grant_property_permission(user, PROP_A)

	def test_p0_1_attendant_cannot_add_trace_or_stay_instruction_but_can_read_and_resolve(self):
		r101 = self.rooms[0]
		stay = make_reservation(
			PROP_A, self.rt, TODAY, d(2), status="Confirmed", room=r101
		)

		frappe.set_user(self.desk)  # nosemgrep: frappe-setuser -- test persona
		rows = api.add_stay_instruction(stay.name, "Housekeeping", "Extra pillows, no feathers")
		self.assertEqual(len(rows), 1)
		created = traces.add_trace(
			reservation=stay.name,
			department="Front Desk",
			instruction="Wake-up call 6am",
			trace_date=TODAY,
		)
		self.assertTrue(created["ok"])

		# Plain Housekeeping attendant MUST be refused on both add_stay_instruction and add_trace
		frappe.set_user(self.attendant)  # nosemgrep: frappe-setuser -- test persona
		with self.assertRaises(frappe.PermissionError):
			api.add_stay_instruction(stay.name, "Housekeeping", "Attendant must not add")
		with self.assertRaises(frappe.PermissionError):
			traces.add_trace(
				reservation=stay.name,
				department="Housekeeping",
				instruction="Attendant must not add trace",
				trace_date=TODAY,
			)

		# Attendant retains read and resolve access
		read_rows = api.stay_instructions(stay.name)
		self.assertEqual(len(read_rows), 2)
		listed = traces.list_traces(reservation=stay.name)
		self.assertEqual(len(listed), 2)

		after_done = api.set_stay_instruction_status(stay.name, read_rows[0]["name"], "Done")
		done_row = next(r for r in after_done if r["name"] == read_rows[0]["name"])
		self.assertEqual(done_row["status"], "Done")
		self.assertEqual(done_row["done_by"], self.attendant)

		resolved = traces.resolve_trace(stay.name, read_rows[1]["name"], status="Done")
		self.assertEqual(resolved["status"], "Done")

		# Housekeeping Supervisor retains add access
		frappe.set_user(self.hk_sup)  # nosemgrep: frappe-setuser -- test persona
		sup_created = traces.add_trace(
			reservation=stay.name,
			department="Housekeeping",
			instruction="Supervisor deep-clean check",
			trace_date=TODAY,
		)
		self.assertTrue(sup_created["ok"])

	def test_p0_2_apply_allocation_ooo_and_os_skip_reason_matches_t384_contract(self):
		r101, r102, _r103, _r104, _r105 = self.rooms
		stay = make_reservation(
			PROP_A, self.rt, TODAY, d(1), status="Confirmed", room=None
		)

		frappe.set_user(self.supervisor)  # nosemgrep: frappe-setuser -- test persona
		api.set_housekeeping_status(r101, "Out of Service", reason="Touch-up paint")
		api.set_housekeeping_status(r102, "Out of Order", reason="Pipe leak")

		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- verify allocation
		late_oo = allocation.apply_allocation(
			PROP_A, [{"reservation": stay.name, "room": r102}]
		)
		self.assertEqual(late_oo["executed"], [])
		self.assertEqual(len(late_oo["skipped"]), 1)
		self.assertIn("no longer free", late_oo["skipped"][0]["reason"])

		late_os = allocation.apply_allocation(
			PROP_A, [{"reservation": stay.name, "room": r101}]
		)
		self.assertEqual(late_os["executed"], [])
		self.assertEqual(len(late_os["skipped"]), 1)
		self.assertIn("no longer free", late_os["skipped"][0]["reason"])

	def test_p1_1_accept_booking_request_excludes_os_rooms_while_honoring_sellable_capacity(self):
		r101, r102, r103, r104, r105 = self.rooms

		frappe.set_user(self.supervisor)  # nosemgrep: frappe-setuser -- test persona
		api.set_housekeeping_status(r101, "Out of Service", reason="Balcony touch-up")
		api.set_housekeeping_status(r102, "Out of Order", reason="Burst pipe")

		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test setup
		# Sub-case A: non-OS rooms exist alongside OS room -> accept must assign a Clean room, never r101 (OS)
		req1 = make_reservation(
			PROP_A, self.rt, d(1), d(2), status="Requested", room=None, auto_price=1
		)
		out1 = booking_requests.accept_booking_request(req1.name)
		self.assertEqual(out1["status"], "Confirmed")
		self.assertIn(out1["room"], {r103, r104, r105})
		self.assertNotEqual(out1["room"], r101)

		# Book the remaining 2 Clean rooms -> 3/3 physically assignable taken, 1 sellable slot left (4 - 3 = 1)
		for rem_room in (r103, r104, r105):
			if rem_room != out1["room"]:
				make_reservation(
					PROP_A, self.rt, d(1), d(2), status="Confirmed", room=rem_room
				)

		# Sub-case B: only OS room (r101) remains physically unbooked inside sellable capacity 4.
		# Accepting the 4th request MUST succeed unassigned (room=None), NEVER assign r101 or crash.
		req2 = make_reservation(
			PROP_A, self.rt, d(1), d(2), status="Requested", room=None, auto_price=1
		)
		out2 = booking_requests.accept_booking_request(req2.name)
		self.assertEqual(out2["status"], "Confirmed")
		self.assertIsNone(out2["room"])
		self.assertFalse(frappe.db.get_value("Reservation", req2.name, "room"))

		# Sub-case C: 5th request exceeds sellable capacity (4/4 full) -> must be refused
		req3 = make_reservation(
			PROP_A, self.rt, d(1), d(2), status="Requested", room=None, auto_price=1
		)
		with self.assertRaises(frappe.ValidationError):
			booking_requests.accept_booking_request(req3.name)

	def test_p1_2_move_reservation_preserves_os_and_oo_on_old_room(self):
		r101, _r102, r103, r104, r105 = self.rooms
		stay = make_reservation(
			PROP_A, self.rt, TODAY, d(3), status="Checked In", room=r103
		)

		# Mid-stay room marked Out of Service -> moving guest out must preserve Out of Service
		frappe.set_user(self.supervisor)  # nosemgrep: frappe-setuser -- test persona
		api.set_housekeeping_status(r103, "Out of Service", reason="AC coil maintenance")

		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- room move
		move1 = api.move_reservation(
			stay.name, r104, confirm_price=1, reason="Relocate for AC maintenance"
		)
		self.assertTrue(move1["ok"])
		r103_after = frappe.db.get_value(
			"Room", r103, ["occupancy_status", "housekeeping_status"], as_dict=True
		)
		self.assertEqual(r103_after.occupancy_status, "Vacant")
		self.assertEqual(r103_after.housekeeping_status, "Out of Service")
		self.assertEqual(frappe.db.get_value("Room", r104, "occupancy_status"), "Occupied")

		# Mid-stay room marked Out of Order -> moving guest out must preserve Out of Order
		frappe.set_user(self.supervisor)  # nosemgrep: frappe-setuser -- test persona
		api.set_housekeeping_status(r104, "Out of Order", reason="Burst pipe")

		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- room move
		move2 = api.move_reservation(
			stay.name, r105, confirm_price=1, reason="Relocate for pipe repair"
		)
		self.assertTrue(move2["ok"])
		r104_after = frappe.db.get_value(
			"Room", r104, ["occupancy_status", "housekeeping_status"], as_dict=True
		)
		self.assertEqual(r104_after.occupancy_status, "Vacant")
		self.assertEqual(r104_after.housekeeping_status, "Out of Order")
		self.assertEqual(frappe.db.get_value("Room", r105, "occupancy_status"), "Occupied")

		# Normal Clean room vacated by move still becomes Dirty (no regression)
		move3 = api.move_reservation(
			stay.name, r101, confirm_price=1, reason="Guest preference"
		)
		self.assertTrue(move3["ok"])
		r105_after = frappe.db.get_value(
			"Room", r105, ["occupancy_status", "housekeeping_status"], as_dict=True
		)
		self.assertEqual(r105_after.occupancy_status, "Vacant")
		self.assertEqual(r105_after.housekeeping_status, "Dirty")
