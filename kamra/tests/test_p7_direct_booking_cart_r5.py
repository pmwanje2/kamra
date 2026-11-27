"""Physical-room commitments shared by the Desk, connecting units and the public cart."""
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, today

from kamra import allocation, api, public_api
from kamra.settings import clear_cache, write_kv
from kamra.tests import test_p7_direct_booking_cart_r4 as r4


class TestConnectingInventory(IntegrationTestCase):
	setUp = r4.TestCartR4.setUp
	make_room = r4.TestCartR4.make_room
	line = r4.TestCartR4.line
	book = r4.TestCartR4.book
	pairs = r4.TestCartR4.pairs

	def stay(self, room_type, *, room=None, unit=None, start=None, end=None):
		guest = frappe.get_doc({"doctype": "Guest", "first_name": "Desk Guest"}).insert()
		res = frappe.get_doc({"doctype": "Reservation", "guest": guest.name, "property": self.property,
		                      "room_type": room_type, "room": room, "sellable_unit": unit,
		                      "status": "Confirmed", "adults": 1, "check_in_date": start or self.ci,
		                      "check_out_date": end or self.co})
		res.flags.allow_past_check_in = True
		return res.insert()

	def test_four_deferred_singles_block_pair_quote_and_checkout(self):
		pair_type, _ = self.pairs(own_type=True)
		for _ in range(4):
			self.assertFalse(self.stay(self.standard).room)
		line = self.line(pair_type, self.ci, self.co, 2)
		for label, action in [("quote", lambda: public_api.cart_quote(self.property, [line])),
		                      ("checkout", lambda: self.book([line]))]:
			with self.subTest(label=label), self.assertRaises(frappe.ValidationError):
				action()
		self.assertEqual(frappe.db.count("Reservation", {"property": self.property}), 4)
		self.assertTrue(public_api.cart_quote(self.property, [self.line(pair_type, self.co, add_days(self.co, 1), 2)]))

	def test_mixed_cart_counts_shared_demand_jointly(self):
		pair_type, _ = self.pairs(own_type=True)
		self.stay(self.standard)
		self.stay(self.standard)
		pair = self.line(pair_type, self.ci, self.co, 2)
		single = self.line(self.standard, self.ci, self.co, 1)
		self.assertTrue(public_api.cart_quote(self.property, [pair]))
		self.assertTrue(public_api.cart_quote(self.property, [single]))
		with self.assertRaises(frappe.ValidationError):
			public_api.cart_quote(self.property, [pair, single])

	def test_desk_rejects_unassigned_pair_after_singles(self):
		pair_type, _ = self.pairs(own_type=True)
		for _ in range(3):
			self.stay(self.standard)
		with self.assertRaises(frappe.ValidationError):
			self.stay(pair_type)

	def test_desk_counts_deferred_pair_against_new_single(self):
		pair_type, _ = self.pairs(own_type=True)
		self.stay(pair_type)
		self.stay(self.standard)
		self.stay(self.standard)
		with self.assertRaises(frappe.ValidationError):
			self.stay(self.standard)

	def test_cart_chooses_the_pair_that_leaves_deferred_singles_feasible(self):
		pair_type, units = self.pairs(own_type=True)
		_, family_pair = list(units)
		for room in units[family_pair]:
			frappe.db.set_value("Room", room, "room_type", self.family)
		self.stay(self.standard)
		self.stay(self.standard)
		stay = self.book([self.line(pair_type, self.ci, self.co, 2)])["reservations"][0]["reservation"]
		self.assertEqual(frappe.db.get_value("Reservation", stay, "sellable_unit"), family_pair)

	def turnover(self, departing_pair):
		pair_type, units = self.pairs(own_type=True)
		unit, (first, second) = next(iter(units.items()))
		single = dict(room_type=self.standard, room=second)
		pair = dict(room_type=pair_type, room=first, unit=unit)
		departing, arriving = (pair, single) if departing_pair else (single, pair)
		out = self.stay(**departing, start=add_days(today(), -1), end=today())
		with patch.dict(frappe.flags, kamra_checkin_steps_off=True, allow_unsettled_checkout=True):
			api.check_in(out.name)
			incoming = self.stay(**arriving, start=today(), end=add_days(today(), 1))
			with self.assertRaisesRegex(frappe.ValidationError, "still occupied"):
				api.check_in(incoming.name)
			self.assertEqual(incoming.reload().status, "Confirmed")
			api.check_out(out.name)
			self.assertTrue(api.check_in(incoming.name)["ok"])

	def test_member_arrival_waits_for_departing_pair(self):
		self.turnover(departing_pair=True)

	def test_pair_arrival_waits_for_departing_member(self):
		self.turnover(departing_pair=False)

	def test_move_checks_both_members_for_maintenance_and_dated_blocks(self):
		pair_type, units = self.pairs(own_type=True)
		original, dest = units
		res = self.stay(pair_type, room=units[original][0], unit=original)
		first, second = units[dest]
		for status in ("Out of Order", "Out of Service"):
			frappe.db.set_value("Room", second, "housekeeping_status", status)
			with self.subTest(status=status), self.assertRaises(frappe.ValidationError):
				api.move_reservation(res.name, first)
			self.assertEqual(res.reload().sellable_unit, original)
		frappe.db.set_value("Room", second, "housekeeping_status", "Clean")
		block = frappe.get_doc({"doctype": "Room Block", "property": self.property, "room": second,
		                        "from_date": self.ci, "to_date": self.co, "reason": "Maintenance"}).insert()
		with self.assertRaises(frappe.ValidationError):
			api.move_reservation(res.name, first)
		frappe.db.set_value("Room Block", block.name, "block_status", "Released")
		self.assertTrue(api.move_reservation(res.name, first)["ok"])
		self.assertEqual(res.reload().sellable_unit, dest)

	def test_batch_allocates_deferred_pair_without_losing_unit(self):
		pair_type, units = self.pairs(own_type=True)
		write_kv(self.property, "assign_room_on_booking", 0)
		clear_cache(self.property)
		booked = self.book([self.line(pair_type, self.ci, self.co, 2)])["reservations"][0]["reservation"]
		res = frappe.get_doc("Reservation", booked)
		unit = res.sellable_unit
		self.assertFalse(res.room)
		target = units[unit][1]
		result = allocation.apply_allocation(self.property, [{"reservation": res.name, "room": target}])
		self.assertEqual(result["skipped"], [])
		self.assertEqual(result["executed"], [{"reservation": res.name, "room": target}])
		self.assertEqual((res.reload().room, res.sellable_unit), (target, unit))
