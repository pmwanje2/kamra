"""Live regression cases for the reservation writer, channel outbox and key journal."""
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from kamra import allocation, api, checkin, public_api
from kamra.door_lock import SimulatorAdapter
from kamra.tests.audit_fixtures import TODAY, d, make_property, make_reservation, make_room, make_room_type


class InventoryLifecycleTests(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		self.prop = make_property("UT R6 " + frappe.generate_hash(length=10), business_date=TODAY)
		self.bed = make_room_type(self.prop)
		self.family = make_room_type(self.prop, "FAM")

	def bundle(self, number):
		rooms = [make_room(self.prop, self.bed, str(i)) for i in (number, number + 1)]
		bundle = frappe.get_doc({"doctype": "Sellable Unit", "property": self.prop, "room_type": self.family,
			"unit_name": f"Family {number}", "unit_kind": "composite", "is_active": 1,
			"physical_rooms": [{"room": room} for room in rooms]}).insert()
		return bundle.name, rooms

	def book(self, unit, **fields):
		return make_reservation(self.prop, self.family, TODAY, d(1), sellable_unit=unit, **fields)

	def test_explicit_sold_bundle_with_room_is_refused(self):
		unit, _ = self.bundle(101)
		other, _ = self.bundle(201)
		nominal = make_room(self.prop, self.family, "301")
		self.book(unit)
		with self.assertRaises(frappe.ValidationError):
			self.book(unit, room=nominal)
		self.book(other)

	def test_checkin_occupies_and_checkout_releases_pinned_members(self):
		unit, members = self.bundle(101)
		_, others = self.bundle(201)
		stay = self.book(unit)
		api.check_in(stay.name)
		for room in members:
			self.assertEqual(frappe.db.get_value("Room", room, "occupancy_status"), "Occupied")
		for room in others:
			self.assertNotEqual(frappe.db.get_value("Room", room, "occupancy_status"), "Occupied")
		with patch.dict(frappe.flags, allow_unsettled_checkout=True):
			api.check_out(stay.name)
		for room in members:
			self.assertEqual(frappe.db.get_value("Room", room, ["occupancy_status", "housekeeping_status"]),
			                 ("Vacant", "Dirty"))

	def test_room_block_respects_composite_claim_and_exclusive_departure(self):
		unit, members = self.bundle(101)
		self.book(unit)
		with self.assertRaises(frappe.ValidationError):
			api.create_room_block(self.prop, members[1], TODAY, d(1))
		api.create_room_block(self.prop, members[1], d(1), d(2))

	def test_fallback_allocation_respects_inventory_holding_statuses(self):
		for n, status in enumerate(("Held", "Pending Payment")):
			with self.subTest(status=status):
				rt = make_room_type(self.prop, "F" + str(n))
				room = make_room(self.prop, rt, str(n + 1))
				make_room(self.prop, rt, str(n + 11))
				make_reservation(self.prop, rt, TODAY, d(1), room=room, status=status)
				stay = make_reservation(self.prop, rt, TODAY, d(1))
				frappe.db.set_value("Sellable Unit", {"room_type": rt}, "is_active", 0)
				result = allocation.apply_allocation(self.prop, [{"reservation": stay.name, "room": room}])
				self.assertEqual(result["executed"], [])
				self.assertFalse(frappe.db.get_value("Reservation", stay.name, "room"))

	def test_reverse_checkin_encoder_dispatch_requires_committed_intent(self):
		room = make_room(self.prop, self.bed, "101")
		stay = make_reservation(self.prop, self.bed, TODAY, d(1), room=room, status="Checked In")
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- establish the original in-house state
		adapter = SimulatorAdapter()
		with patch("kamra.door_lock.get_adapter", return_value=adapter), patch.object(
			adapter, "revoke_request", wraps=adapter.revoke_request) as send:
			api.reverse_check_in(stay.name, "Mistaken arrival")
			frappe.db.rollback()
			self.assertEqual(frappe.db.get_value("Reservation", stay.name, "status"), "Checked In")
			send.assert_not_called()
			self.assertFalse(any(row.operation == "reverse_checkin_revoke" for row in checkin.door_key_audit(stay.name)))
			result = api.reverse_check_in(stay.name, "Mistaken arrival")
			warning, = result["key_result"]["pending_revocations"]
			send.assert_not_called()
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- deliver the real transaction callback
			send.assert_called_once()
			self.assertEqual(send.call_args.args[0].options["request_id"], warning["operation_id"])
			self.assertEqual(frappe.db.get_value("Door Key Audit", warning["operation_id"], "status"), "Accepted")

	def test_desk_limit_cannot_break_zero_house_protection(self):
		make_room(self.prop, self.bed, "101")
		limit = frappe.get_doc({"doctype": "Room Sell Limit", "property": self.prop,
			"room_type": self.bed, "stay_date": TODAY, "adjustment": -1}).insert()
		limit.adjustment = 1
		with self.assertRaises(frappe.ValidationError):
			limit.save()
		self.assertEqual(frappe.db.get_value("Room Sell Limit", limit.name, "adjustment"), -1)

	def test_house_and_room_serviceability_publish_inventory(self):
		room = make_room(self.prop, self.bed, "101")
		conn = frappe.get_doc({"doctype": "Channel Manager Connection", "property": self.prop,
			"provider": "Channex", "active": 1, "external_property_id": "ut-r6"}).insert()
		prop = frappe.get_doc("Property", self.prop)
		prop.house_overbooking_limit = 3
		prop.save()
		room_doc = frappe.get_doc("Room", room)

		def edit_house():
			prop.house_overbooking_limit = 2
			prop.save()

		def desk_oos():
			room_doc.housekeeping_status = "Out of Service"
			room_doc.save()

		for name, change in (("house", edit_house), ("desk", desk_oos),
		                     ("housekeeping", lambda: api.set_housekeeping_status(room, "Clean"))):
			with self.subTest(change=name):
				frappe.db.delete("Channel ARI Outbox", {"connection": conn.name})
				change()
				self.assertTrue(frappe.db.exists("Channel ARI Outbox", {"connection": conn.name,
					"status": "Pending", "kinds": ("in", ["availability", "both"])}))

	def test_positive_limit_cannot_reopen_a_booked_villa_member(self):
		make_room(self.prop, self.bed, "101")
		villa = make_room_type(self.prop, "VIL", room_category="Villa")
		frappe.db.set_value("Property", self.prop, "house_overbooking_limit", 1)
		allocation.set_sell_limit(self.prop, self.bed, TODAY, TODAY, "1")
		make_reservation(self.prop, villa, TODAY, d(1))
		offers = public_api.search_stay(self.prop, TODAY, d(1))
		member = next(row for row in offers if row["room_type"] == self.bed)
		self.assertEqual((member["available"], member["rooms_left"]), (False, 0))
