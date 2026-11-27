"""Group pickup supply, consistent write locks, and continuous interval matching."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import process_time_ns
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days

from kamra.group_blocks import unpicked_by_night
from kamra.kamra.doctype.reservation.reservation import InventoryRefused
from kamra.siu import shared_capacity as capacity
from kamra.tests import test_p7_direct_booking_cart_r7 as r7
from kamra.tests.test_p7_direct_booking_cart import book_reviewed_cart


class PickupFixture:
	setUp = r7.TestPickupChanges.setUp
	make_room = r7.TestPickupChanges.make_room
	line = r7.TestPickupChanges.line
	group = r7.TestPickupChanges.group
	pickup = r7.TestPickupChanges.pickup

	def extra_room(self, number):
		return frappe.get_doc({"doctype": "Room", "property": self.property, "room_type": self.standard,
		                       "room_number": number}).insert()


class TestGroupSupply(PickupFixture, IntegrationTestCase):
	def unavailable_change(self, *, shorten=False, transfer=False):
		room = self.extra_room("102")
		group = self.group(self.standard, 1)
		stay = self.pickup(self.standard, group)
		room.db_set("housekeeping_status", "Out of Order")
		stay.group_booking = self.group(self.standard, 0).name if transfer else None
		if shorten:
			stay.check_out_date = add_days(self.co, -1)
		with self.assertRaises(InventoryRefused):
			stay.save()
		self.assertEqual(stay.reload().group_booking, group.name)
		self.assertEqual(set(unpicked_by_night(group.name, self.standard).values()), {0})

	def test_unlink_rechecks_sellable_rooms(self):
		self.unavailable_change()

	def test_transfer_rechecks_sellable_rooms(self):
		self.unavailable_change(transfer=True)

	def test_shortening_and_unlinking_rechecks_retained_night(self):
		self.unavailable_change(shorten=True)

	def test_released_night_must_still_support_its_restored_hold(self):
		arrival = self.ci
		self.ci = add_days(self.co, -1)
		group = self.group(self.standard, 1)
		self.ci = arrival
		stay = self.pickup(self.standard, group)
		room = frappe.db.get_value("Room", {"room_type": self.standard})
		frappe.get_doc({"doctype": "Room Block", "property": self.property, "room": room,
		                "from_date": add_days(self.co, -1), "to_date": self.co,
		                "reason": "Maintenance"}).insert()
		stay.group_booking = None
		stay.check_out_date = add_days(self.co, -1)
		with self.assertRaises(InventoryRefused):
			stay.save()

	def test_group_hold_on_night_367(self):
		arrival = self.ci
		self.ci, self.co = add_days(arrival, 366), add_days(arrival, 367)
		group = self.group(self.standard, 1)
		self.ci = arrival
		stay = self.pickup(self.standard, group)
		stay.group_booking = None
		with self.assertRaises(InventoryRefused):
			stay.save()
		self.assertEqual(unpicked_by_night(group.name, self.standard), {add_days(arrival, 366): 0})


class TestConcurrentPickup(PickupFixture, IntegrationTestCase):
	def test_normal_save_and_checkout_both_commit_property_first(self):
		self.extra_room("102")
		self.extra_room("103")
		group = self.group(self.standard, 1)
		stay = self.pickup(self.standard, group)
		prop, rt, start, end = self.property, self.standard, self.ci, self.co
		site, sites_path = frappe.local.site, frappe.local.sites_path
		phone = "+12025550128"
		barrier = Barrier(2, timeout=30)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- fixture must be visible to both test transactions

		def checkout():
			frappe.init(site=site, sites_path=sites_path)
			try:
				frappe.connect()
				frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- independent test connection
				frappe.local.lang = "en"
				barrier.wait()
				book_reviewed_cart(prop, [{"room_type": rt, "adults": 1, "children": 0,
					"check_in_date": start, "check_out_date": end}], guest_name="R8 Checkout", phone=phone,
					idempotency_key=frappe.generate_hash(length=32))
				frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test verifies both transactions commit
			finally:
				frappe.destroy()

		locks = []
		run_sql = frappe.db.sql
		def trace(query, *args, **kwargs):
			sql = str(query).upper()
			if "FOR UPDATE" in sql:
				for table in ("TABPROPERTY", "TABRESERVATION"):
					if "`" + table + "`" in sql:
						locks.append(table)
			return run_sql(query, *args, **kwargs)

		try:
			with ThreadPoolExecutor(max_workers=1) as pool:
				other = pool.submit(checkout)
				try:
					stay.group_booking = None
					barrier.wait()
					with patch.object(frappe.db, "sql", side_effect=trace):
						stay.save()
					frappe.db.commit()  # nosemgrep: frappe-manual-commit -- release Desk locks before joining
				finally:
					frappe.db.rollback()
				other.result(timeout=40)
			self.assertEqual(frappe.db.count("Reservation", {"property": prop}), 2)
			self.assertFalse(stay.reload().group_booking)
			self.assertEqual(set(unpicked_by_night(group.name, rt).values()), {1})
			self.assertLess(locks.index("TABPROPERTY"), locks.index("TABRESERVATION"), locks)
		finally:
			frappe.db.rollback()
			guests = {stay.guest, *frappe.get_all("Guest", {"phone": phone}, pluck="name")}
			for dt in ("Folio", "Reservation", "Group Booking", "Sellable Unit", "Room", "Room Type",
			           "Meal Plan", "Property Setting"):
				for name in frappe.get_all(dt, {"property": prop}, pluck="name"):
					frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
			frappe.delete_doc("Property", prop, force=True, ignore_permissions=True)
			for guest in guests:
				frappe.delete_doc("Guest", guest, force=True, ignore_permissions=True)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- remove the committed test fixtures


class TestIntervalMatching(IntegrationTestCase):
	def test_unrelated_boundary_preserves_exact_pair_matching(self):
		edges = []
		for i in range(0, 24, 6):
			edges.extend((i + a, i + b) for a, b in [(0, 1), (1, 2), (0, 2), (0, 3), (1, 4), (2, 5)])
			if i:
				edges.append((i - 1, i))
		units = {"pair": [frozenset(edge) for edge in edges]}
		def fits(blocks):
			return capacity._window(units, [(0, "pair", 0, 2)], [("pair", 0, 2)] * 11,
			                        blocks, set(), [], 0, 2).fits({})
		self.assertTrue(fits([]))
		self.assertTrue(fits([({99}, 1, 2)]))
		self.assertFalse(fits([({0}, 1, 2)]))

	def window_inputs(self):
		units = {"pair": [frozenset((i, i + 1)) for i in range(0, 128, 2)]}
		stays = [("pair", i, i + 30) for i in range(100)]
		return units, [(0, "pair", 0, 30)], stays, [], set(), [], 0, 129

	def test_staggered_window_construction_is_compact(self):
		args = self.window_inputs()
		start = process_time_ns()
		window = capacity._window(*args)
		self.assertLess(process_time_ns() - start, 10_000_000)
		self.assertIsNotNone(window)

	def test_construction_spends_the_callers_budget(self):
		args = self.window_inputs()
		with self.assertRaises(capacity.SearchLimit):
			capacity._window(*args, budget=capacity.SearchBudget(0))
		budget = capacity.SearchBudget()
		initial = budget.remaining
		capacity._window(*args, budget=budget)
		self.assertGreater(budget.remaining, 0)
		self.assertLess(budget.remaining, initial)
