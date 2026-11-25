"""P35: Room Status Out of Service (OS) & Pickup.

Live Frappe v16 / MariaDB 11.8.9 integration tests verifying the critical
OPERA Cloud invariant:
1. Out of Service (OS) blocks physical room assignment (available_rooms with
   for_assignment=1 / ready_now=1, allocation, checkin._free_rooms,
   Reservation.validate_room_serviceability, check_in) WITHOUT reducing
   Room Type sellable inventory (validate_type_capacity, search_stay,
   channel_manager._sellable, dashboards._sellable, reports.manager_flash
   sellable / available_rooms / RevPAR denominator).
2. Out of Order (OO) removes the room from BOTH physical room assignment AND
   sellable inventory / RevPAR denominator.
3. Pickup status persists cleanly via set_housekeeping_status (allowed for both
   attendants and supervisors), ranks between Clean and Dirty in room assignment
   (Inspected -> Clean -> Pickup -> Dirty), and preserves OS on checkout when an
   in-house room is marked OS mid-stay.
4. Night Audit check_room_status flags vacant OS rooms whose os_oo_until passed.
"""

from __future__ import annotations

import frappe

from kamra import allocation, api, channel_manager, checkin, dashboards, night_audit, public_api, reports
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


class P35OutOfServiceAndPickupTestCase(AuditTestCase):
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
		)
		make_property(PROP_A, business_date=TODAY, overbooking_pct=0)
		self.rt = make_room_type(PROP_A, "STD", base_price=1200)
		self.rooms = [
			make_room(PROP_A, self.rt, num)
			for num in ("101", "102", "103", "104", "105")
		]
		self.attendant = ensure_user("p35.hk@test.local", ["Housekeeping"])
		self.supervisor = ensure_user("p35.sup@test.local", ["Housekeeping Supervisor", "Front Desk"])
		for user in (self.attendant, self.supervisor):
			frappe.db.delete("User Permission", {"user": user, "allow": "Property"})
			frappe.get_doc({
				"doctype": "User Permission",
				"user": user,
				"allow": "Property",
				"for_value": PROP_A,
			}).insert(ignore_permissions=True)
			frappe.clear_cache(user=user)

	def test_oos_blocks_room_assignment_without_reducing_type_capacity_or_revpar_denominator(self):
		r101, r102, r103, r104, r105 = self.rooms

		# Supervisor marks 101 Out of Service (minor touch-up) and 102 Out of Order (major defect)
		frappe.set_user(self.supervisor)  # nosemgrep: frappe-setuser -- test persona
		res_os = api.set_housekeeping_status(
			r101,
			"Out of Service",
			reason="Touch-up paint on balcony railing",
			until_date=d(2),
		)
		self.assertTrue(res_os["ok"])
		self.assertEqual(res_os["status"], "Out of Service")
		r101_meta = frappe.db.get_value(
			"Room", r101, ["housekeeping_status", "os_oo_reason", "os_oo_until", "os_oo_set_by"], as_dict=True
		)
		self.assertEqual(r101_meta.housekeeping_status, "Out of Service")
		self.assertEqual(r101_meta.os_oo_reason, "Touch-up paint on balcony railing")
		self.assertEqual(str(r101_meta.os_oo_until), d(2))
		self.assertEqual(r101_meta.os_oo_set_by, self.supervisor)

		api.set_housekeeping_status(r102, "Out of Order", reason="Burst pipe", until_date=d(5))

		# 1. Sellable capacity across OTA, Dashboard, Public Search, and Manager Flash MUST be 4 (5 - 1 OO), NOT 3!
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- inspect reports/capacity
		self.assertEqual(channel_manager._sellable(PROP_A, self.rt, d(1), d(2)), 4)
		self.assertEqual(dashboards._sellable(PROP_A), 4)

		flash = reports.manager_flash(PROP_A, TODAY)
		self.assertEqual(flash["total_rooms"], 5)
		self.assertEqual(flash["sellable"], 4)
		self.assertEqual(flash["available_rooms"], 4)
		self.assertEqual(flash["os_rooms"], 1)

		hk_slice = dashboards._housekeeping_slice(PROP_A)
		self.assertEqual(hk_slice["room_status"]["Out of Service"], 1)
		self.assertEqual(hk_slice["room_status"]["Out of Order"], 1)
		self.assertIn("Pickup", hk_slice["room_status"])

		search_rows = public_api.search_stay(PROP_A, d(1), d(2), adults=2)
		std_row = next(r for r in search_rows if r["room_type"] == self.rt)
		self.assertEqual(std_row["rooms_left"], 4)

		# 2. Room-type capacity allows 4 bookings (3 assigned + 1 unassigned), blocks 5th
		b1 = make_reservation(PROP_A, self.rt, d(1), d(2), status="Confirmed", room=r103)
		b2 = make_reservation(PROP_A, self.rt, d(1), d(2), status="Confirmed", room=r104)
		b3 = make_reservation(PROP_A, self.rt, d(1), d(2), status="Confirmed", room=r105)
		b4 = make_reservation(PROP_A, self.rt, d(1), d(2), status="Confirmed", room=None)
		self.assertTrue(all(x.name for x in (b1, b2, b3, b4)))
		with self.assertRaises(frappe.ValidationError):
			make_reservation(PROP_A, self.rt, d(1), d(2), status="Confirmed", room=None)

		# 3. Physical assignment excludes BOTH 101 (OS) and 102 (OO) -> only 3 rooms assignable on free dates
		assignable = api.available_rooms(PROP_A, self.rt, d(5), d(6), for_assignment=1)
		assignable_names = {r["name"] for r in assignable}
		self.assertEqual(assignable_names, {r103, r104, r105})

		ready = api.available_rooms(PROP_A, self.rt, TODAY, d(1), ready_now=1)
		self.assertEqual({r["name"] for r in ready}, {r103, r104, r105})

		# Capacity query (default for_assignment=0, ready_now=0) still returns 4 (includes OS, excludes OO)
		cap_rooms = api.available_rooms(PROP_A, self.rt, d(5), d(6))
		self.assertEqual(len(cap_rooms), 4)
		self.assertIn(r101, {r["name"] for r in cap_rooms})
		self.assertNotIn(r102, {r["name"] for r in cap_rooms})

		# 4. Direct assignment or check-in into an OS room is refused
		with self.assertRaises(frappe.ValidationError):
			make_reservation(PROP_A, self.rt, d(5), d(6), status="Confirmed", room=r101)

		unassigned_today = make_reservation(PROP_A, self.rt, TODAY, d(1), status="Confirmed", room=None)
		with self.assertRaises(frappe.ValidationError):
			api.check_in(unassigned_today.name, room=r101)

		# Allocation never proposes or applies an OS or OO room
		suggested = allocation.suggest_allocation(PROP_A, TODAY)
		proposed_rooms = {p["suggested_room"] for p in suggested["proposals"]}
		self.assertNotIn(r101, proposed_rooms)
		self.assertNotIn(r102, proposed_rooms)

		alloc_out = allocation.apply_allocation(PROP_A, [{"reservation": unassigned_today.name, "room": r101}])
		self.assertEqual(alloc_out["executed"], [])
		self.assertEqual(len(alloc_out["skipped"]), 1)

	def test_pickup_status_permissions_ranking_and_checkout_preserves_os(self):
		r101, r102, r103, r104, r105 = self.rooms

		# Attendant MAY set Clean, Dirty, Pickup — but NOT Out of Service or Out of Order
		frappe.set_user(self.attendant)  # nosemgrep: frappe-setuser -- test persona
		res_pickup = api.set_housekeeping_status(r103, "Pickup", reason="Quick towel refresh")
		self.assertTrue(res_pickup["ok"])
		self.assertEqual(frappe.db.get_value("Room", r103, "housekeeping_status"), "Pickup")

		with self.assertRaises(frappe.PermissionError):
			api.set_housekeeping_status(r104, "Out of Service", reason="Attendant attempt")

		# Set distinct readiness levels: 101 Inspected, 102 Clean, 103 Pickup, 104 Dirty, 105 Out of Service
		frappe.set_user(self.supervisor)  # nosemgrep: frappe-setuser -- test persona
		api.set_housekeeping_status(r101, "Inspected")
		api.set_housekeeping_status(r102, "Clean")
		api.set_housekeeping_status(r104, "Dirty")
		api.set_housekeeping_status(r105, "Out of Service", reason="Curtain rod", until_date=d(-1))

		# Check-in free rooms rank: Inspected (101) -> Clean (102) -> Pickup (103) -> Dirty (104), excluding OS (105)
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- verify ranking
		stay = make_reservation(PROP_A, self.rt, TODAY, d(1), status="Confirmed", room=None)
		free = checkin._free_rooms(stay)
		self.assertEqual([r["name"] for r in free], [r101, r102, r103, r104])

		# Night audit check_room_status flags vacant OS room with expired os_oo_until (< business_date)
		rs = night_audit.check_room_status(PROP_A)
		expired_items = [i for i in rs["items"] if i["room_id"] == r105]
		self.assertTrue(expired_items, f"Expected expired OS room in check_room_status, got: {rs['items']}")

		# Marking an already Checked In room as OS preserves OS on check-out (instead of flipping to Dirty)
		in_house = make_reservation(PROP_A, self.rt, TODAY, d(1), status="Checked In", room=r102)
		frappe.set_user(self.supervisor)  # nosemgrep: frappe-setuser -- test persona
		api.set_housekeeping_status(r102, "Out of Service", reason="AC filter after departure")
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- check out
		frappe.flags.allow_unsettled_checkout = True
		self.addCleanup(setattr, frappe.flags, "allow_unsettled_checkout", False)
		api.check_out(in_house.name)
		r102_after = frappe.db.get_value(
			"Room", r102, ["housekeeping_status", "occupancy_status"], as_dict=True
		)
		self.assertEqual(r102_after.housekeeping_status, "Out of Service")
		self.assertEqual(r102_after.occupancy_status, "Vacant")
