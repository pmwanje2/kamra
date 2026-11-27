"""Group inventory is restored only for nights the old reservation picked up."""
import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from kamra.group_blocks import group_nights, restored_pickup_nights, unpicked_by_night
from kamra.kamra.doctype.reservation.reservation import InventoryRefused
from kamra.tests.test_p7_direct_booking_cart_r8 import PickupFixture


class TestPickupStatus(PickupFixture, IntegrationTestCase):
	def test_waitlist_can_unlink_and_confirm_on_a_free_later_night(self):
		group = self.group(self.standard, 1)
		guest = frappe.get_doc({"doctype": "Guest", "first_name": "Waitlisted guest"}).insert()
		stay = frappe.get_doc({
			"doctype": "Reservation", "property": self.property, "room_type": self.standard,
			"group_booking": group.name, "guest": guest.name, "status": "Waitlist",
			"check_in_date": self.ci, "check_out_date": self.co, "adults": 1,
		}).insert()
		original_hold = dict.fromkeys(group_nights(self.ci, self.co), 1)
		self.assertEqual(unpicked_by_night(group.name, self.standard), original_hold)
		room = frappe.db.get_value("Room", {"property": self.property, "room_type": self.standard})
		frappe.get_doc({
			"doctype": "Room Block", "property": self.property, "room": room,
			"from_date": self.ci, "to_date": self.co, "reason": "Maintenance",
		}).insert()

		stay.group_booking = None
		stay.check_in_date, stay.check_out_date = self.co, add_days(self.co, 1)
		stay.status = "Confirmed"
		stay.save()
		stay.reload()
		self.assertFalse(stay.group_booking)
		self.assertEqual(stay.status, "Confirmed")
		self.assertEqual(str(stay.check_in_date), self.co)
		self.assertEqual(str(stay.check_out_date), add_days(self.co, 1))
		self.assertEqual(unpicked_by_night(group.name, self.standard), original_hold)

	def test_restoration_agrees_with_both_pickup_readers_for_stored_statuses(self):
		group = self.group(self.standard, 1)
		stay = self.pickup(self.standard, group)
		after = frappe._dict(group_booking=None, room_type=self.standard, property=self.property)
		nights = group_nights(self.ci, self.co)
		# Exercise persisted statuses directly, including terminal states that
		# Desk does not permit moving back to Confirmed.
		for status in ("Waitlist", "Cancelled", "No Show", "Inquiry", "Quoted", "Requested",
		               "Confirmed", "Checked In", "Held", "Pending Payment"):
			with self.subTest(status=status):
				stay.db_set("status", status)
				picked = status in ("Confirmed", "Checked In", "Held", "Pending Payment")
				for locking in (False, True):
					self.assertEqual(unpicked_by_night(group.name, self.standard, for_update=locking),
					                 dict.fromkeys(nights, int(not picked)))
				self.assertEqual(restored_pickup_nights(stay, after), set(nights) if picked else set())

	def test_checked_out_counts_used_nights_only_after_departure(self):
		group = self.group(self.standard, 1)
		stay = self.pickup(self.standard, group)
		arrival, departure = add_days(nowdate(), -2), add_days(nowdate(), -1)
		# Historical checkout during a group whose negotiated block is still active.
		group.db_set({"check_in_date": arrival, "check_out_date": nowdate(), "cutoff_date": nowdate()})
		stay.db_set({"check_in_date": arrival, "check_out_date": departure, "status": "Checked Out"})
		for locking in (False, True):
			self.assertEqual(unpicked_by_night(group.name, self.standard, for_update=locking),
			                 {arrival: 0, departure: 1})
		after = frappe._dict(group_booking=None, room_type=self.standard, property=self.property)
		self.assertEqual(restored_pickup_nights(stay, after), {arrival})

	def assert_active_unlink_refused(self, status):
		group = self.group(self.standard, 1)
		stay = self.pickup(self.standard, group)
		stay.db_set("status", status)
		room = frappe.db.get_value("Room", {"property": self.property, "room_type": self.standard})
		frappe.db.set_value("Room", room, "housekeeping_status", "Out of Order")
		stay.group_booking = None
		with self.assertRaises(InventoryRefused):
			stay.save()
		self.assertEqual(stay.reload().group_booking, group.name)
		self.assertEqual(set(unpicked_by_night(group.name, self.standard).values()), {0})

	def test_confirmed_pickup_still_refuses_unlink_on_unavailable_room(self):
		self.assert_active_unlink_refused("Confirmed")

	def test_checked_in_pickup_still_refuses_unlink_on_unavailable_room(self):
		self.assert_active_unlink_refused("Checked In")
