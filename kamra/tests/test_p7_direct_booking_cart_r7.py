"""Pickup changes, bounded physical inventory matching, and public API display text."""
import json
import re
from itertools import combinations
from pathlib import Path
from time import process_time_ns

import frappe
from frappe.tests import IntegrationTestCase

from kamra.group_blocks import unpicked_by_night
from kamra.siu import shared_capacity
from kamra.tests import test_p7_direct_booking_cart_r6 as r6


class TestPickupChanges(IntegrationTestCase):
	setUp = r6.TestContinuousSharedInventory.setUp
	make_room = r6.TestContinuousSharedInventory.make_room
	line = r6.TestContinuousSharedInventory.line
	pairs = r6.TestContinuousSharedInventory.pairs

	def group(self, room_type, count):
		return frappe.get_doc({
			"doctype": "Group Booking", "group_name": frappe.generate_hash(length=8),
			"property": self.property, "status": "Confirmed", "check_in_date": self.ci,
			"check_out_date": self.co, "cutoff_date": self.ci,
			"blocks": [{"room_type": room_type, "rooms_blocked": count}] if count else [],
		}).insert()

	def pickup(self, rt, group):
		guest = frappe.get_doc({"doctype": "Guest", "first_name": "Group guest"}).insert()
		return frappe.get_doc({
			"doctype": "Reservation", "property": self.property, "group_booking": group.name,
			"guest": guest.name, "status": "Confirmed", "room_type": rt,
			"check_in_date": self.ci, "check_out_date": self.co, "adults": 1,
		}).insert()

	def assert_refused(self, room_type, target=None):
		old = self.group(room_type, 2)
		stay = self.pickup(room_type, old)
		stay.group_booking = target
		with self.assertRaises(frappe.ValidationError):
			stay.save()
		self.assertEqual(stay.reload().group_booking, old.name)
		self.assertEqual(set(unpicked_by_night(old.name, room_type).values()), {1})

	def test_unlink_restores_old_pair_hold(self):
		rt, _ = self.pairs(own_type=True)
		self.assert_refused(rt)

	def test_transfer_to_group_without_hold_is_refused(self):
		rt, _ = self.pairs(own_type=True)
		self.assert_refused(rt, self.group(rt, 0).name)

	def test_transfer_uses_new_groups_hold(self):
		rt, _ = self.pairs(own_type=True)
		old, new = self.group(rt, 1), self.group(rt, 1)
		stay = self.pickup(rt, old)
		stay.group_booking = new.name
		stay.save()
		self.assertEqual(set(unpicked_by_night(old.name, rt).values()), {1})
		self.assertEqual(set(unpicked_by_night(new.name, rt).values()), {0})

	def test_unlink_restores_standalone_room_type_hold(self):
		old = self.group(self.standard, 1)
		stay = self.pickup(self.standard, old)
		stay.group_booking = None
		with self.assertRaises(frappe.ValidationError):
			stay.save()


class TestMatchingAndDisplay(IntegrationTestCase):
	def test_independent_pair_clusters(self):
		# CPU time excludes scheduling pauses on shared CI runners. On the old
		# solver stop at the first failing size, before considering larger cases.
		for count in (12, 16, 20):
			pairs = [frozenset(edge) for cluster in range(count)
			         for edge in combinations(range(cluster * 3, cluster * 3 + 3), 2)]
			start = process_time_ns()
			self.assertFalse(shared_capacity._can_fit([(count + 1, pairs)], set()))
			self.assertTrue(shared_capacity._can_fit([(count, pairs)], set()))
			self.assertLess(process_time_ns() - start, 5_000_000)

	def test_odd_cycle_with_stems_needs_exact_pair_matching(self):
		edges = [(0, 1), (0, 2), (1, 2), (0, 3), (1, 4), (2, 5)]
		pairs = [frozenset(edge) for edge in edges]
		self.assertTrue(shared_capacity._can_fit([(3, pairs)], set()))
		self.assertFalse(shared_capacity._can_fit([(3, pairs)], {3}))

	def test_general_search_exhaustion_fails_closed(self):
		# Two stays with distinct spans and overlapping three-room units.
		def choices(nights, sets):
			return [frozenset((day, room) for day in nights for room in unit) for unit in sets]
		groups = [(1, choices([0, 1], ["abc", "bcd"])), (1, choices([1, 2], ["ade", "def"]))]
		self.assertTrue(shared_capacity._can_fit(groups, set()))
		with self.subTest("no search work remaining"):
			self.assertFalse(shared_capacity._can_fit(groups, set(), budget=shared_capacity.SearchBudget(0)))

	def test_postman_display_text_at_every_depth(self):
		path = Path(__file__).resolve().parents[2] / "docs-site/public/kamra.postman_collection.json"
		forbidden = re.compile(r"(?<![a-z0-9])(?:kamra|frappe|erpnext|ury|tridz|buildsuite)(?![a-z0-9])"
		                       r"|kamrapms\.com|heykoala|₹", re.I)
		def inspect(obj):
			if isinstance(obj, list):
				for value in obj:
					inspect(value)
			elif isinstance(obj, dict):
				for key, value in obj.items():
					if key in {"name", "description"}:
						self.assertNotRegex(str(value), forbidden)
					inspect(value)
		inspect(json.loads(path.read_text()))
