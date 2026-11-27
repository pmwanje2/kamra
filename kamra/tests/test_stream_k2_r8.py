"""Arrival-room claims during amendments, reversals and legacy inventory edits."""
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from kamra import api
from kamra.tests.audit_fixtures import TODAY, d, make_property, make_reservation, make_room, make_room_type


class ArrivalClaimTests(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		self.prop = make_property("UT R8 " + frappe.generate_hash(length=10), business_date=TODAY)
		self.bed = make_room_type(self.prop)
		self.rooms = [make_room(self.prop, self.bed, n) for n in ("101", "102")]

	def arrive(self, villa=False):
		if villa:
			category = make_room_type(self.prop, "VIL", room_category="Villa")
			unit = None
		else:
			category = make_room_type(self.prop, "FAM")
			self.unit = frappe.get_doc({"doctype": "Sellable Unit", "property": self.prop,
				"room_type": category, "unit_name": "Family", "unit_kind": "composite",
				"is_active": 1, "physical_rooms": [{"room": room} for room in self.rooms]}).insert()
			unit = self.unit.name
		self.stay = make_reservation(self.prop, category, TODAY, d(1), sellable_unit=unit)
		api.check_in(self.stay.name)
		self.assert_snapshot()

	def assert_snapshot(self):
		self.assertCountEqual([r.room for r in self.stay.reload().occupied_members], self.rooms)

	def forget_snapshot(self):
		frappe.db.delete("Reservation Occupied Room", {"parent": self.stay.name})

	def replace_member(self):
		new = make_room(self.prop, self.bed, "103")
		self.unit.physical_rooms[1].room = new
		self.unit.save()
		return new

	def test_extension_conflict_in_arrival_room(self):
		self.arrive()
		self.replace_member()
		make_reservation(self.prop, self.bed, d(1), d(2), room=self.rooms[1])
		with self.assertRaises(frappe.ValidationError):
			api.amend_stay(self.stay.name, TODAY, d(2), confirm_price=1)
		self.assertEqual(str(self.stay.reload().check_out_date), str(d(1)))

	def test_extension_ignores_replacement_room_booking(self):
		self.arrive()
		new = self.replace_member()
		make_reservation(self.prop, self.bed, d(1), d(2), room=new)
		api.amend_stay(self.stay.name, TODAY, d(2), confirm_price=1)
		self.assert_snapshot()
		self.assertEqual(str(self.stay.check_out_date), str(d(2)))

	def test_reverse_conflict_in_replacement_room(self):
		self.arrive()
		new = self.replace_member()
		make_reservation(self.prop, self.bed, TODAY, d(1), room=new)
		with patch("kamra.api._revoke_stay_keys") as revoke:
			with self.assertRaises(frappe.ValidationError):
				api.reverse_check_in(self.stay.name, "Wrong arrival")
			revoke.assert_not_called()
		self.assert_snapshot()
		self.assertEqual(self.stay.status, "Checked In")
		for room in self.rooms:
			self.assertEqual(frappe.db.get_value("Room", room, "occupancy_status"), "Occupied")

	def verify_legacy(self, villa, migrate):
		self.arrive(villa)
		self.forget_snapshot()
		if migrate:
			patch_name = "kamra.patches.v48.backfill_occupied_members"
			registered = Path(frappe.get_app_path("kamra", "patches.txt")).read_text()
			self.assertIn(patch_name, registered.split("[post_model_sync]")[1].splitlines())
			for _ in range(2):
				frappe.get_attr(patch_name + ".execute")()
				self.assert_snapshot()
		new = make_room(self.prop, self.bed, "103") if villa else self.replace_member()
		self.assert_snapshot()
		if villa:
			api.reverse_check_in(self.stay.name, "Wrong arrival")
		else:
			with patch.dict(frappe.flags, allow_unsettled_checkout=True):
				api.check_out(self.stay.name)
		for room in self.rooms:
			self.assertEqual(frappe.db.get_value("Room", room, ["occupancy_status", "housekeeping_status"]),
			                 ("Vacant", "Dirty"))
		self.assertNotEqual(frappe.db.get_value("Room", new, "housekeeping_status"), "Dirty")

	def test_legacy_suite_edit(self):
		self.verify_legacy(False, False)

	def test_legacy_villa_room_insert(self):
		self.verify_legacy(True, False)

	def test_migrate_legacy_suite(self):
		self.verify_legacy(False, True)

	def test_migrate_legacy_villa(self):
		self.verify_legacy(True, True)

	def test_whole_villa_extension_cannot_substitute_new_room_for_blocked_member(self):
		self.arrive(villa=True)
		make_room(self.prop, self.bed, "103")
		block = api.create_room_block(self.prop, self.rooms[1], d(1), d(2), reason="Maintenance")
		with self.assertRaises(frappe.ValidationError):
			api.amend_stay(self.stay.name, TODAY, d(2), confirm_price=1)
		self.assert_snapshot()
		self.assertEqual(str(self.stay.check_out_date), d(1))
		api.release_room_block(block["name"])
		api.amend_stay(self.stay.name, TODAY, d(2), confirm_price=1)
		self.assert_snapshot()
		self.assertEqual(str(self.stay.check_out_date), d(2))

	def test_reversal_waits_for_replacement_room_guest_to_check_out(self):
		new = make_room(self.prop, self.bed, "103")
		departing = make_reservation(self.prop, self.bed, d(-1), TODAY, room=new, status="Checked In")
		self.arrive()
		self.assertEqual(self.replace_member(), new)
		with patch("kamra.api._revoke_stay_keys") as revoke:
			with self.assertRaises(frappe.ValidationError):
				api.reverse_check_in(self.stay.name, "Wrong arrival")
			revoke.assert_not_called()
		self.assert_snapshot()
		self.assertEqual(self.stay.status, "Checked In")
		self.assertEqual(departing.reload().status, "Checked In")
		for room in self.rooms + [new]:
			self.assertEqual(frappe.db.get_value("Room", room, "occupancy_status"), "Occupied")
		with patch.dict(frappe.flags, allow_unsettled_checkout=True):
			api.check_out(departing.name)
		api.reverse_check_in(self.stay.name, "Wrong arrival")
		self.assertEqual(self.stay.reload().status, "Confirmed")

	def test_block_cannot_take_an_extended_villa_arrival_room(self):
		self.arrive(villa=True)
		self.assertFalse(frappe.db.exists("Sellable Unit", {"property": self.prop, "unit_kind": "composite"}))
		unoccupied = make_room(self.prop, self.bed, "103")
		api.amend_stay(self.stay.name, TODAY, d(2), confirm_price=1)
		with self.assertRaises(frappe.ValidationError):
			api.create_room_block(self.prop, self.rooms[1], d(1), d(2), reason="Maintenance")
		self.assertFalse(frappe.db.exists("Room Block", {"room": self.rooms[1]}))
		api.create_room_block(self.prop, unoccupied, d(1), d(2), reason="Maintenance")
		api.create_room_block(self.prop, self.rooms[1], d(2), d(3), reason="Maintenance")

	def test_villa_arrival_waits_for_member_guest_to_depart(self):
		departing = make_reservation(self.prop, self.bed, d(-1), TODAY, room=self.rooms[0], status="Checked In")
		villa = make_room_type(self.prop, "VIL", room_category="Villa")
		stay = make_reservation(self.prop, villa, TODAY, d(1))
		with self.assertRaises(frappe.ValidationError):
			api.check_in(stay.name)
		self.assertEqual(stay.reload().status, "Confirmed")
		self.assertFalse(stay.occupied_members)
		self.assertEqual(departing.reload().status, "Checked In")
		self.assertEqual(frappe.db.get_value("Room", self.rooms[0], "occupancy_status"), "Occupied")
		with patch.dict(frappe.flags, allow_unsettled_checkout=True):
			api.check_out(departing.name)
		api.check_in(stay.name)
		self.assertCountEqual([r.room for r in stay.reload().occupied_members], self.rooms)

	def test_villa_reversal_preserves_original_room_with_a_live_guest(self):
		departing = make_reservation(self.prop, self.bed, d(-1), TODAY, room=self.rooms[0])
		self.arrive(villa=True)
		# Restore the inconsistent in-house state that older check-in code admitted.
		frappe.db.set_value("Reservation", departing.name, "status", "Checked In")
		states = [frappe.db.get_value("Room", r, ["occupancy_status", "housekeeping_status"]) for r in self.rooms]
		with patch("kamra.api._revoke_stay_keys") as revoke:
			with self.assertRaises(frappe.ValidationError):
				api.reverse_check_in(self.stay.name, "Wrong arrival")
			revoke.assert_not_called()
		self.assert_snapshot()
		self.assertEqual(self.stay.status, "Checked In")
		self.assertEqual(departing.reload().status, "Checked In")
		self.assertEqual([frappe.db.get_value("Room", r, ["occupancy_status", "housekeeping_status"])
		                  for r in self.rooms], states)

	def test_scalar_move_refuses_an_in_house_whole_villa(self):
		self.arrive(villa=True)
		target = make_room(self.prop, self.bed, "103")
		rooms = self.rooms + [target]
		states = [frappe.db.get_value("Room", r, ["occupancy_status", "housekeeping_status"]) for r in rooms]
		with patch("kamra.checkin.revocation_warnings") as revoke:
			with self.assertRaises(frappe.ValidationError):
				api.move_room(self.stay.name, target, upgrade_type="Complimentary", reason="Guest request",
				              confirm_price=1)
			revoke.assert_not_called()
		self.assert_snapshot()
		self.assertFalse(self.stay.room)
		self.assertEqual([frappe.db.get_value("Room", r, ["occupancy_status", "housekeeping_status"])
		                  for r in rooms], states)
		self.assertCountEqual(self.stay._occupied_rooms(checking_in=False), self.rooms)

	def test_scalar_arrival_waits_for_due_out_villa_on_hotel_business_date(self):
		self.arrive(villa=True)
		frappe.db.set_value("Property", self.prop, "business_date", d(1))
		stay = make_reservation(self.prop, self.bed, d(1), d(2), room=self.rooms[0])
		with patch("kamra.kamra.doctype.reservation.reservation.today", return_value=d(-2)):
			with self.assertRaises(frappe.ValidationError):
				api.check_in(stay.name)
		self.assertEqual(stay.reload().status, "Confirmed")
		self.assertEqual(self.stay.reload().status, "Checked In")
		self.assert_snapshot()
		with patch.dict(frappe.flags, allow_unsettled_checkout=True):
			api.check_out(self.stay.name)
		api.check_in(stay.name)
		self.assertEqual(stay.reload().status, "Checked In")

	def test_villa_arrival_waits_for_active_member_block_to_be_released(self):
		villa = make_room_type(self.prop, "VIL", room_category="Villa")
		stay = make_reservation(self.prop, villa, TODAY, d(2))
		block = api.create_room_block(self.prop, self.rooms[1], d(1), d(2), reason="Maintenance")
		api.create_room_block(self.prop, self.rooms[0], d(2), d(3), reason="Maintenance")
		with self.assertRaises(frappe.ValidationError):
			api.check_in(stay.name)
		self.assertEqual(stay.reload().status, "Confirmed")
		self.assertFalse(stay.occupied_members)
		self.assertEqual(frappe.db.get_value("Room Block", block["name"], "block_status"), "Active")
		for room in self.rooms:
			self.assertEqual(frappe.db.get_value("Room", room, "occupancy_status"), "Vacant")
		api.release_room_block(block["name"])
		api.check_in(stay.name)
		self.assertCountEqual([r.room for r in stay.reload().occupied_members], self.rooms)

	def test_reverse_co_sharer_keeps_remaining_guest_in_house(self):
		from kamra.shares import create_share_reservation
		primary = make_reservation(self.prop, self.bed, TODAY, d(1), room=self.rooms[0])
		second = create_share_reservation(primary.name, guest_name="Second guest")["reservation"]
		api.check_in(primary.name)
		api.check_in(second)
		self.assertTrue(primary.reload().share_group_id)
		self.assertEqual(frappe.db.get_value("Reservation", second, "share_group_id"), primary.share_group_id)
		state = frappe.db.get_value("Room", self.rooms[0], ["occupancy_status", "housekeeping_status"])
		self.assertEqual(state[0], "Occupied")
		api.reverse_check_in(second, "Wrong arrival")
		self.assertEqual(frappe.db.get_value("Reservation", second, "status"), "Confirmed")
		self.assertEqual(primary.reload().status, "Checked In")
		self.assertEqual(frappe.db.get_value("Room", self.rooms[0], ["occupancy_status", "housekeeping_status"]),
		                 state)
