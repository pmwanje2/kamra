"""Desk replacements on a registration card must update the document it displays."""

from io import BytesIO

import frappe
from frappe.tests import IntegrationTestCase

from PIL import Image

from kamra import api, public_api
from kamra.tests import test_p7_direct_booking_cart as cart
from kamra.tests.test_p7_direct_booking_cart_r2 import signature


class TestRegistrationCardDocuments(IntegrationTestCase):
	setUp = cart.TestDirectBookingCart.setUp
	make_room = cart.TestDirectBookingCart.make_room
	line = cart.TestDirectBookingCart.line
	book = cart.TestDirectBookingCart.book

	def test_desk_replaces_online_documents_on_the_selected_stay(self):
		first, second = self.book(self.lines)["reservations"]
		stay = frappe.get_doc("Reservation", first["reservation"])
		original, replacement = signature("blue"), signature("red")
		public_api.precheckin_submit(
			first["precheckin_token"], "Passport", "D123456", consent=1,
			signature=signature(), id_image=original, address_image=original,
		)
		for kind, stay_field, guest_field in (
			("id", "id_document", "id_file"),
			("address", "precheckin_address_file", "address_proof_file"),
		):
			with self.subTest(kind=kind):
				uploaded = api.upload_guest_document(stay.guest, kind, replacement, reservation=stay.name)
				self.assertEqual(api.registration_card(stay.name)["guest"][guest_field], uploaded["file"])
				files = frappe.get_all("File", filters={
					"attached_to_doctype": "Reservation", "attached_to_name": stay.name,
					"attached_to_field": stay_field,
				}, fields=["name", "file_url", "is_private"])
				self.assertEqual([(f.file_url, f.is_private) for f in files], [(uploaded["file"], 1)])
				saved = frappe.get_doc("File", files[0].name).get_content()
				with Image.open(BytesIO(saved)) as image:
					red, _, blue = image.convert("RGB").getpixel((6, 6))
					self.assertGreater(red, blue)
				self.assertFalse(frappe.db.get_value("Guest", stay.guest, guest_field))
				self.assertFalse(frappe.db.get_value("Reservation", second["reservation"], stay_field))

	def test_profile_upload_still_works_and_mismatched_stay_is_refused(self):
		first = self.book(self.lines)["reservations"][0]
		stay = frappe.get_doc("Reservation", first["reservation"])
		other = frappe.get_doc({"doctype": "Guest", "first_name": "Other"}).insert()
		uploaded = api.upload_guest_document(stay.guest, "address", signature())
		self.assertEqual(api.registration_card(stay.name)["guest"]["address_proof_file"], uploaded["file"])
		with self.assertRaises(frappe.PermissionError):
			api.upload_guest_document(other.name, "address", signature(), reservation=stay.name)
		self.assertFalse(frappe.db.get_value("Guest", other.name, "address_proof_file"))
