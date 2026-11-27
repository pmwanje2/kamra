"""Public cart price acceptance, live replay and erasure of submitted contact."""
import json

import frappe
from frappe.tests import IntegrationTestCase

from kamra import api, privacy, public_api
from kamra.tests import test_p7_direct_booking_cart as cart
from kamra.tests import test_p7_direct_booking_r17 as groups


class TestCheckoutReview(IntegrationTestCase):
	setUp = cart.TestDirectBookingCart.setUp
	make_room = cart.TestDirectBookingCart.make_room
	line = cart.TestDirectBookingCart.line
	version = groups.TestGroupPrivacy.version

	def checkout(self, key, **kwargs):
		return frappe.call(public_api.create_multi_room_booking, property=self.property, lines=self.lines,
			guest_name="Riley Vale", phone="+256700123456", idempotency_key=key, **kwargs)

	def test_changed_rate_needs_an_explicit_second_acceptance(self):
		key = frappe.generate_hash(length=32)
		frappe.db.set_value("Property", self.property, "currency", "USD")
		frappe.clear_cache()
		accepted = public_api.cart_quote(self.property, self.lines)
		frappe.db.set_value("Room Type", self.standard, "base_price", 500)
		frappe.clear_cache()
		current = public_api.cart_quote(self.property, self.lines)
		self.assertGreater(current["amount_after_tax"], accepted["amount_after_tax"])
		changed = self.checkout(key, expected_quote=accepted)
		self.assertIs(changed.get("price_changed"), True)
		self.assertEqual(changed["quote"], current)
		for dt in ("Group Booking", "Reservation"):
			self.assertFalse(frappe.db.exists(dt, {"property": self.property}))
		result = self.checkout(key, expected_quote=changed["quote"])
		self.assertEqual(result["amount_after_tax"], current["amount_after_tax"])
		self.assertEqual(len(result["reservations"]), 2)

	def test_unquoted_checkout_returns_terms_without_creating_a_booking(self):
		result = self.checkout(frappe.generate_hash(length=32))
		self.assertIs(result.get("price_changed"), True)
		self.assertFalse(frappe.db.exists("Reservation", {"property": self.property}))
		self.assertEqual(result["quote"], public_api.cart_quote(self.property, self.lines))

	def test_replay_refreshes_cancelled_status_and_keeps_original_price(self):
		key = frappe.generate_hash(length=32)
		accepted = public_api.cart_quote(self.property, self.lines)
		first = self.checkout(key, expected_quote=accepted)
		name = first["reservations"][0]["reservation"]
		api.cancel_reservation(name)
		self.assertEqual(frappe.db.get_value("Reservation", name, "status"), "Cancelled")
		replay = self.checkout(key, expected_quote=accepted)
		self.assertEqual(replay["confirmation"], first["confirmation"])
		self.assertEqual(replay["amount_after_tax"], first["amount_after_tax"])
		self.assertEqual([r["status"] for r in replay["reservations"]], ["Cancelled", "Confirmed"])
		self.assertFalse(replay["reservations"][0]["precheckin_token"])

	def test_erasure_includes_stay_only_email_in_group_notes_and_history(self):
		guest = frappe.get_doc({"doctype": "Guest", "first_name": "Riley", "last_name": "Vale",
			"phone": "+256700123456", "email": "profile@example.test"}).insert()
		email = "stay-only@example.test"
		first = self.checkout(frappe.generate_hash(length=32), email=email,
			expected_quote=public_api.cart_quote(self.property, self.lines))
		group = first["group_booking"]
		name = first["reservations"][0]["reservation"]
		self.assertEqual(frappe.db.get_value("Reservation", name, ["guest", "booking_contact_email"]),
		                 (guest.name, email))
		self.assertEqual(guest.reload().email, "profile@example.test")
		notes = "Contact " + email + " or events@example.test"
		frappe.db.set_value("Group Booking", group, "notes", notes)
		version = self.version(group, "notes", email.upper(), notes)
		alias = privacy.erase_guest(guest.name)["alias"]
		expected = "Contact " + alias + " or events@example.test"
		self.assertEqual((frappe.db.get_value("Group Booking", group, "notes"),
		                  json.loads(version.reload().data)["changed"]), (expected, [["notes", alias, expected]]))
