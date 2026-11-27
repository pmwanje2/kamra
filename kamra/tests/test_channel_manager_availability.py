"""OTA inventory must include bookings that do not yet have a room number."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from kamra import channel_manager as cm


class ChannelManagerAvailabilityTestCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		self.property = frappe.get_doc({"doctype": "Property",
			"property_name": "TEST ARI " + frappe.generate_hash(length=8)}).insert(ignore_permissions=True).name
		# This inventory fixture models a standalone property with planless stays.
		from kamra.settings import clear_cache, write_kv
		write_kv(self.property, "rate_plan_required", 0)
		clear_cache(self.property)
		self.room_type = frappe.get_doc({"doctype": "Room Type", "property": self.property,
			"room_type_code": "STD", "room_type_name": "Standard", "base_price": 1000,
			"adults_capacity": 2}).insert(ignore_permissions=True).name
		self.room = frappe.get_doc({"doctype": "Room", "property": self.property,
			"room_type": self.room_type, "room_number": "101"}).insert(ignore_permissions=True).name
		self.guest = frappe.get_doc({"doctype": "Guest", "first_name": "ARI Guest"}).insert(ignore_permissions=True).name
		self.conn = frappe.get_doc({"doctype": "Channel Manager Connection", "property": self.property,
			"provider": "AioSell", "active": 1, "external_property_id": "demo"}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Channel Room Mapping", "connection": self.conn.name,
			"room_type": self.room_type, "external_room_id": "std",
			"external_rate_id": "std-rate"}).insert(ignore_permissions=True)

	def reservation(self, status="Confirmed", room=None, check_in=None):
		ci = check_in or nowdate()
		return frappe.get_doc({"doctype": "Reservation", "property": self.property,
			"room_type": self.room_type, "guest": self.guest, "room": room,
			"status": status, "check_in_date": ci, "check_out_date": add_days(ci, 1),
			"adults": 2}).insert(ignore_permissions=True)

	def available(self):
		return cm.ari_snapshot(self.property, self.conn.name, days=1)[0]["days"][0]["available"]

	def test_inactive_individual_units_are_removed_from_outbound_ari(self):
		second = frappe.get_doc({"doctype": "Room", "property": self.property,
			"room_type": self.room_type, "room_number": "102"}).insert(ignore_permissions=True)
		self.assertEqual(self.available(), 2)
		unit = frappe.db.get_value("Sellable Unit Room", {"room": second.name}, "parent")
		self.assertTrue(unit)
		frappe.db.set_value("Sellable Unit", unit, "is_active", 0)
		self.assertEqual(self.available(), 1)
		# A physical room that is also out of service is excluded only once.
		frappe.db.set_value("Room", second.name, "housekeeping_status", "Out of Service")
		self.assertEqual(self.available(), 1)
		frappe.db.set_value("Room", self.room, "housekeeping_status", "Out of Service")
		self.assertEqual(self.available(), 0)
		frappe.db.set_value("Room", self.room, "housekeeping_status", "Clean")
		frappe.db.set_value("Sellable Unit", {"property": self.property}, "is_active", 0)
		self.assertEqual(self.available(), 0)

	def test_unassigned_confirmed_stay_reduces_ota_inventory(self):
		stay = self.reservation()
		self.assertFalse(stay.room)
		self.assertEqual(self.available(), 0)

	def test_legacy_unassigned_stay_reduces_ota_inventory(self):
		frappe.db.set_value("Sellable Unit", {"property": self.property}, "is_active", 0)
		self.reservation()
		self.assertEqual(self.available(), 0)

	def test_assigned_stay_is_not_counted_twice(self):
		self.reservation(room=self.room)
		self.assertEqual(self.available(), 0)

	def test_requested_stay_holds_no_ota_inventory(self):
		self.reservation(status="Requested")
		self.assertEqual(self.available(), 1)

	def test_adjacent_stay_leaves_tonight_available(self):
		self.reservation(check_in=add_days(nowdate(), 1))
		self.assertEqual(self.available(), 1)

	def test_room_block_still_holds_ota_inventory(self):
		frappe.get_doc({"doctype": "Room Block", "property": self.property,
			"room": self.room, "from_date": nowdate(), "to_date": add_days(nowdate(), 1),
			"block_status": "Active", "reason": "Maintenance"}).insert(ignore_permissions=True)
		self.assertEqual(self.available(), 0)

	def test_composite_distribution_excludes_an_unserviceable_member_each_night(self):
		from kamra.tests.audit_fixtures import make_room_type, make_room
		second = make_room(self.property, self.room_type, "102")
		suite = make_room_type(self.property, "FAM")
		frappe.get_doc({"doctype": "Sellable Unit", "property": self.property, "unit_name": "Family",
			"unit_kind": "composite", "room_type": suite, "is_active": 1,
			"physical_rooms": [{"room": self.room}, {"room": second}]}).insert()
		frappe.get_doc({"doctype": "Channel Room Mapping", "connection": self.conn.name,
			"room_type": suite, "external_room_id": "family", "external_rate_id": "family-rate"}).insert()
		start = nowdate()
		frappe.get_doc({"doctype": "Room Block", "property": self.property, "room": second,
			"from_date": add_days(start, 1), "to_date": add_days(start, 2),
			"block_status": "Active", "reason": "Maintenance"}).insert()

		def nights():
			rows = cm.ari_snapshot(self.property, self.conn.name, days=3, start=start)
			return next([d["available"] for d in row["days"]] for row in rows if row["room_type"] == suite)

		self.assertEqual(nights(), [1, 0, 1])
		frappe.db.set_value("Room", self.room, "housekeeping_status", "Out of Service")
		self.assertEqual(nights(), [0, 0, 0])
		frappe.db.set_value("Room", self.room, "housekeeping_status", "Clean")
		frappe.db.set_value("Sellable Unit", {"property": self.property, "room_type": self.room_type,
			"unit_kind": "individual"}, "is_active", 0)
		self.assertEqual(nights(), [0, 0, 0])

	def villa_only_channel(self):
		from kamra.tests.audit_fixtures import make_room, make_room_type

		second = make_room(self.property, self.room_type, "102")
		villa = make_room_type(self.property, "VIL", room_category="Villa")
		mapping = frappe.get_doc("Channel Room Mapping", {"connection": self.conn.name})
		mapping.room_type = villa
		mapping.save()
		start = add_days(nowdate(), 1)
		end = add_days(start, 1)
		return villa, second, start, end

	def blocked_villa(self):
		villa, second, start, end = self.villa_only_channel()
		block = frappe.get_doc({"doctype": "Room Block", "property": self.property, "room": second,
			"from_date": start, "to_date": end, "block_status": "Active", "reason": "Maintenance"}).insert()
		return villa, block, start, end

	def test_villa_only_channel_withholds_only_the_blocked_member_nights(self):
		from kamra.api import release_room_block

		villa, block, start, _ = self.blocked_villa()

		def nights():
			rows = cm.ari_snapshot(self.property, self.conn.name, days=3, start=add_days(start, -1))
			self.assertEqual([row["room_type"] for row in rows], [villa])
			return [day["available"] for day in rows[0]["days"]]

		self.assertEqual(nights(), [1, 0, 1])
		release_room_block(block.name)
		self.assertEqual(nights(), [1, 1, 1])

	def test_whole_villa_confirmation_waits_for_member_block_release(self):
		from kamra.api import release_room_block
		from kamra.kamra.doctype.reservation.reservation import InventoryRefused
		from kamra.tests.audit_fixtures import make_reservation

		villa, block, start, end = self.blocked_villa()
		with self.assertRaises(InventoryRefused):
			make_reservation(self.property, villa, start, end, guest=self.guest)
		self.assertFalse(frappe.db.exists("Reservation", {"property": self.property, "status": "Confirmed"}))
		release_room_block(block.name)
		self.assertEqual(make_reservation(self.property, villa, start, end, guest=self.guest).status, "Confirmed")

	def assert_villa_inventory(self, villa, start, end, expected):
		from kamra.kamra.doctype.reservation.reservation import InventoryRefused
		from kamra.tests.audit_fixtures import make_reservation

		rows = cm.ari_snapshot(self.property, self.conn.name, days=3, start=add_days(start, -1))
		self.assertEqual([r["room_type"] for r in rows], [villa])
		offered = [d["available"] for d in rows[0]["days"]]
		try:
			status = make_reservation(self.property, villa, start, end, guest=self.guest).status
		except InventoryRefused:
			status = "Refused"
		self.assertEqual((offered, status), (expected, "Confirmed" if expected[1] else "Refused"))

	def test_villa_only_channel_and_booking_respect_member_group_hold(self):
		villa, _, start, end = self.villa_only_channel()
		group = frappe.get_doc({
			"doctype": "Group Booking", "property": self.property, "status": "Confirmed",
			"group_name": "Villa member hold", "check_in_date": start, "check_out_date": end,
			"cutoff_date": start, "blocks": [{"room_type": self.room_type, "rooms_blocked": 1}],
		}).insert()
		self.assert_villa_inventory(villa, start, end, [1, 0, 1])
		group.status = "Cancelled"
		group.save()
		self.assert_villa_inventory(villa, start, end, [1, 1, 1])

	def check_housekeeping_exclusion(self, housekeeping_status):
		villa, second, start, end = self.villa_only_channel()
		room = frappe.get_doc("Room", second)
		room.housekeeping_status = housekeeping_status
		room.save()
		self.assert_villa_inventory(villa, start, end, [0, 0, 0])
		room.housekeeping_status = "Clean"
		room.save()
		self.assert_villa_inventory(villa, start, end, [1, 1, 1])

	def test_villa_only_channel_and_booking_respect_out_of_service_member(self):
		self.check_housekeeping_exclusion("Out of Service")

	def test_villa_only_channel_and_booking_respect_out_of_order_member(self):
		self.check_housekeeping_exclusion("Out of Order")

	def test_villa_only_channel_and_booking_respect_inactive_member_unit(self):
		villa, second, start, end = self.villa_only_channel()
		unit = frappe.get_doc("Sellable Unit", frappe.db.get_value("Sellable Unit Room", {"room": second}, "parent"))
		self.assertEqual(unit.unit_kind, "individual")
		unit.is_active = 0
		unit.save()
		self.assert_villa_inventory(villa, start, end, [0, 0, 0])
		unit.is_active = 1
		unit.save()
		self.assert_villa_inventory(villa, start, end, [1, 1, 1])
