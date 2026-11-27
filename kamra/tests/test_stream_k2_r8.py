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
