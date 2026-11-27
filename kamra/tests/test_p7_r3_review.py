"""Booking policy and reservation-scoped contact contracts."""
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from kamra import public_api
from kamra.tests import test_p7_direct_booking_cart as cart


class TestPublicBookingPolicy(IntegrationTestCase):
	setUp = cart.TestDirectBookingCart.setUp
	make_room = cart.TestDirectBookingCart.make_room
	line = cart.TestDirectBookingCart.line

	def test_single_room_booking_checks_publication_before_loading_inventory(self):
		for flags in ({"disabled": 1}, {"booking_engine_enabled": 0}):
			with self.subTest(flags=flags):
				frappe.db.set_value("Property", self.property,
				                    {"disabled": 0, "booking_engine_enabled": 1, **flags})
				with patch.object(frappe, "get_cached_doc", side_effect=AssertionError("premature document read")), \
				     patch("kamra.api.create_booking") as create:
					with self.assertRaisesRegex(frappe.ValidationError, "Online booking is unavailable"):
						public_api.book(self.property, self.standard, self.ci, self.co, "Fixture", "555")
					create.assert_not_called()

	def test_public_quote_percentage_and_amount_follow_the_same_policy(self):
		frappe.db.set_value("Property", self.property, {
			"currency": "USD", "deposit_pct": 50, "booking_payment_mode": "Advance percent",
			"advance_percent": 25,
		})
		frappe.db.set_value("Room Type", self.standard, {"base_price": 250.26, "tax_percent": 0})
		frappe.clear_cache()
		rows = public_api.search_stay(self.property, self.ci, frappe.utils.add_days(self.ci, 1))
		q = next(row["quote"] for row in rows if row["room_type"] == self.standard)
		self.assertEqual((q["amount_after_tax"], q["advance"]["percent"], q["advance"]["amount"],
		                  q["advance_due"]), (250.26, 25, 62.57, 62.57))

	def test_each_cart_reservation_keeps_the_returning_bookers_submission(self):
		guest = frappe.get_doc({
			"doctype": "Guest", "first_name": "Returning", "last_name": frappe.generate_hash(length=8),
			"phone": "+256700000919", "email": "original@example.test", "marketing_consent": 0,
		}).insert()
		response = public_api.create_multi_room_booking(self.property, self.lines, guest.full_name,
			guest.phone, email="stay@example.test", marketing_consent=1,
			idempotency_key=frappe.generate_hash(length=32))
		guest.reload()
		self.assertEqual((guest.email, guest.marketing_consent), ("original@example.test", 0))
		self.assertEqual(len(response["reservations"]), len(self.lines))
		for booked in response["reservations"]:
			res = frappe.get_doc("Reservation", booked["reservation"])
			self.assertEqual(res.guest, guest.name)
			self.assertEqual((res.get("booking_contact_email"), res.get("booking_marketing_opt_in")),
			                 ("stay@example.test", 1))
