"""A deferred stay keeps one room across its nights; a group holds its unpicked stock."""
import json
import re
from pathlib import Path
from string import Formatter

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime, today

from kamra import public_api
from kamra.group_blocks import block_hold_by_night
from kamra.tests import test_p7_direct_booking_cart_r5 as r5


class TestContinuousSharedInventory(IntegrationTestCase):
	setUp = r5.TestConnectingInventory.setUp
	make_room = r5.TestConnectingInventory.make_room
	line = r5.TestConnectingInventory.line
	book = r5.TestConnectingInventory.book
	pairs = r5.TestConnectingInventory.pairs
	stay = r5.TestConnectingInventory.stay

	def staggered(self):
		sold, pairs = self.pairs(own_type=True)
		first, second = pairs
		mid = add_days(self.ci, 1)
		self.stay(self.standard, room=pairs[second][0], end=mid)
		self.stay(self.standard, room=pairs[second][1], start=mid)
		self.stay(self.standard)
		return sold, first, pairs[first][0]

	def test_public_quote_and_checkout_preserve_two_night_deferred_stay(self):
		sold, _, _ = self.staggered()
		line = self.line(sold, self.ci, self.co, 2)
		for action in (lambda: public_api.cart_quote(self.property, [line]), lambda: self.book([line])):
			with self.subTest(action=action), self.assertRaises(frappe.ValidationError):
				action()
		self.assertEqual(frappe.db.count("Reservation", {"property": self.property}), 3)

	def test_desk_pair_refuses_to_displace_two_night_deferred_stay(self):
		sold, unit, room = self.staggered()
		with self.assertRaises(frappe.ValidationError):
			self.stay(sold, room=room, unit=unit)

	def test_shorter_request_checks_the_rest_of_the_existing_stay(self):
		sold, _, _ = self.staggered()
		with self.assertRaises(frappe.ValidationError):
			public_api.cart_quote(self.property, [self.line(sold, self.ci, add_days(self.ci, 1), 2)])

	def block(self, counts):
		group = frappe.get_doc({"doctype": "Group Booking", "property": self.property,
		                        "group_name": "Held rooms", "status": "Confirmed",
		                        "check_in_date": self.ci, "check_out_date": self.co, "cutoff_date": self.ci,
		                        "blocks": [{"room_type": self.standard, "rooms_blocked": max(counts.values()),
		                                    "nightly_counts": json.dumps(counts)}]}).insert()
		self.assertEqual(block_hold_by_night(self.property, self.standard, list(counts)), counts)
		return group

	def test_unpicked_block_protects_members_until_release(self):
		sold, _ = self.pairs(own_type=True)
		group = self.block({self.ci: 3, add_days(self.ci, 1): 3})
		line = self.line(sold, self.ci, self.co, 2)
		for action in (lambda: public_api.cart_quote(self.property, [line]), lambda: self.book([line])):
			with self.subTest(action=action), self.assertRaises(frappe.ValidationError):
				action()
		self.assertFalse(frappe.db.exists("Reservation", {"property": self.property}))
		group.db_set("released_on", now_datetime())
		self.assertTrue(self.book([line])["reservations"])

	def test_nightly_group_counts_and_cutoff(self):
		sold, _ = self.pairs(own_type=True)
		mid = add_days(self.ci, 1)
		group = self.block({self.ci: 2, mid: 3})
		self.assertTrue(public_api.cart_quote(self.property, [self.line(sold, self.ci, mid, 2)]))
		with self.assertRaises(frappe.ValidationError):
			self.stay(sold, start=mid)
		group.db_set("cutoff_date", add_days(today(), -1))
		self.assertTrue(self.stay(sold, start=mid).name)

	def test_pickup_uses_one_held_room_of_its_own_type(self):
		sold, _ = self.pairs(own_type=True)
		group = self.block({self.ci: 4, add_days(self.ci, 1): 4})
		guest = frappe.get_doc({"doctype": "Guest", "first_name": "Group guest"}).insert()
		def pickup(room_type):
			return frappe.get_doc({"doctype": "Reservation", "property": self.property, "group_booking": group.name,
			                       "guest": guest.name, "adults": 1, "status": "Confirmed", "room_type": room_type,
			                       "check_in_date": self.ci, "check_out_date": self.co}).insert()
		self.assertTrue(pickup(self.standard).name)
		with self.assertRaises(frappe.ValidationError):
			pickup(sold)

	def test_live_backend_translations_for_booking_and_turnover(self):
		message = "Room {0} is still occupied by {1} ({2}). Check them out first, or choose another room."
		for language in ("fr", "es", "ar"):
			for key in ("Show on Booking Page", "Room still occupied", message,
			            "Pre-Check-In Billing Address", "Online Registration Address File"):
				with self.subTest(language=language, key=key):
					value = frappe._(key, lang=language)
					self.assertNotEqual(value, key)
					self.assertEqual(sorted(field for _, field, _, _ in Formatter().parse(value) if field),
					                 sorted(field for _, field, _, _ in Formatter().parse(key) if field))

	def test_public_postman_display_metadata_is_white_label(self):
		path = Path(__file__).resolve().parents[2] / "docs-site/public/kamra.postman_collection.json"
		info = json.loads(path.read_text())["info"]
		for field in ("name", "description"):
			self.assertTrue(info[field].strip())
			self.assertNotRegex(info[field], re.compile(r"kamra|heykoala|frappe", re.I))
