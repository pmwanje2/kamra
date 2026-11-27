"""Guest access and erasure through the direct cart and online registration."""

import frappe
from frappe.tests import IntegrationTestCase

from kamra import privacy, public_api
from kamra.tests import test_p7_direct_booking_cart as original
from kamra.tests.test_p7_direct_booking_cart_r2 import signature


class TestDirectBookingPrivacy(IntegrationTestCase):
	setUp = original.TestDirectBookingCart.setUp
	make_room = original.TestDirectBookingCart.make_room
	line = original.TestDirectBookingCart.line

	def cart(self):
		return original.book_reviewed_cart(
			self.property, self.lines, guest_name="Robin Mire", phone="+256700000916",
			email="robin@example.test", idempotency_key=frappe.generate_hash(length=32),
		)

	def test_cart_group_title_contains_no_booker_contact(self):
		group = frappe.get_doc("Group Booking", self.cart()["group_booking"])
		self.assertTrue(group.group_name.strip())
		for value in ("Robin", "Mire", "robin@example.test", "700000916"):
			self.assertNotIn(value.casefold(), group.group_name.casefold())

	def test_erasure_cleans_legacy_group_but_preserves_organization_and_unrelated_groups(self):
		result = self.cart()
		first, second = [r["reservation"] for r in result["reservations"]]
		guest = frappe.db.get_value("Reservation", first, "guest")
		group = result["group_booking"]
		frappe.db.set_value("Group Booking", group, {
			"group_name": "Robin Mire", "notes": "Coach arrival. Call +256700000916 or robin@example.test.",
		})

		def group_named(title):
			return frappe.get_doc({
				"doctype": "Group Booking", "property": self.property, "group_name": title,
				"check_in_date": self.ci, "check_out_date": self.co,
			}).insert().name

		agency = group_named("Robin Mire Travel Agency")
		unrelated = group_named("Robin Mire")
		frappe.db.set_value("Reservation", second, "group_booking", agency)
		privacy.erase_guest(guest)
		self.assertFalse(frappe.db.exists("Group Booking", {
			"name": group, "group_name": ("like", "%Robin Mire%"),
		}))
		notes = frappe.db.get_value("Group Booking", group, "notes")
		self.assertIn("Coach arrival.", notes)
		self.assertNotIn("robin@example.test", notes)
		self.assertNotIn("700000916", notes)
		self.assertEqual(frappe.db.get_value("Group Booking", agency, "group_name"), "Robin Mire Travel Agency")
		self.assertEqual(frappe.db.get_value("Group Booking", unrelated, "group_name"), "Robin Mire")
		self.assertEqual(frappe.db.get_value("Reservation", first, "group_booking"), group)

	def submit(self, profile):
		booked, sibling = self.cart()["reservations"]
		stay = frappe.get_doc("Reservation", booked["reservation"])
		frappe.db.set_value("Guest", stay.guest, dict(profile))
		payload = {
			"id_type": "Passport", "id_number": "P701600", "email": "arrival@example.test",
			"nationality": "Kenyan", "address_line": "26 Arrival Road", "city": "Nairobi",
		}
		public_api.precheckin_submit(
			booked["precheckin_token"], **payload, consent=1, signature=signature(),
			eta="17:35", special_requests="Please call my carer", billing_address="32 Invoice Road",
			id_image=signature("blue"), address_image=signature("green"),
		)
		stay.reload()
		return stay, payload, sibling["reservation"]

	def assert_export(self, profile):
		stay, payload, sibling_id = self.submit(profile)
		report = privacy.export_guest_data(stay.guest)
		for key, value in profile.items():
			self.assertEqual(report["profile"].get(key) or "", value)
		row = next(r for r in report["stays"] if r["name"] == stay.name)
		self.assertEqual(row.get("precheckin_registration"), payload)
		for field in ("eta", "special_requests", "precheckin_billing_address", "precheckin_signature",
		              "precheckin_status", "precheckin_on", "precheckin_consent", "precheckin_terms_version",
		              "precheckin_consent_ip", "precheckin_address_file", "id_document", "id_document_source",
		              "id_document_on"):
			self.assertIn(field, row)
			self.assertEqual(str(row[field]), str(stay.get(field)))
		sibling = next(r for r in report["stays"] if r["name"] == sibling_id)
		self.assertFalse(sibling.get("precheckin_registration"))
		self.assertNotIn(stay.precheckin_token, frappe.as_json(report))

	def test_export_keeps_profile_and_submitted_registration_distinct(self):
		self.assert_export({"email": "profile@example.test", "nationality": "Ugandan",
		                    "address_line": "14 Profile Street", "city": "Kampala"})

	def test_export_includes_registration_with_an_empty_profile(self):
		self.assert_export(dict.fromkeys(("email", "nationality", "address_line", "city"), ""))

	def test_erasure_removes_registration_requests_and_private_documents(self):
		stay, _, _ = self.submit({"email": "profile@example.test"})
		files = frappe.get_all("File", filters={"attached_to_doctype": "Reservation",
		                       "attached_to_name": stay.name}, pluck="name")
		self.assertEqual(len(files), 2)
		privacy.erase_guest(stay.guest)
		stay.reload()
		for field in ("eta", "special_requests", "precheckin_registration", "precheckin_signature",
		              "precheckin_billing_address", "precheckin_consent_ip", "precheckin_address_file",
		              "id_document", "precheckin_token"):
			self.assertFalse(stay.get(field), field)
		for file in files:
			self.assertFalse(frappe.db.exists("File", file))
		self.assertFalse(stay.precheckin_consent)
		self.assertTrue(stay.amount_after_tax)
