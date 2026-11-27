"""Erasing a booker's contacts in shared group records preserves business identity."""

import json

import frappe
from frappe.tests import IntegrationTestCase

from kamra import privacy, public_api
from kamra.tests import test_p7_direct_booking_cart as original
from kamra.tests.test_p7_direct_booking_cart_r2 import signature


class TestGroupPrivacy(IntegrationTestCase):
	setUp = original.TestDirectBookingCart.setUp
	make_room = original.TestDirectBookingCart.make_room
	line = original.TestDirectBookingCart.line

	def book(self):
		cart = public_api.create_multi_room_booking(
			self.property, self.lines, guest_name="Riley Vale", phone="+256700123456",
			email="profile@example.test", idempotency_key=frappe.generate_hash(length=32),
		)
		stay = cart["reservations"][0]
		guest = frappe.db.get_value("Reservation", stay["reservation"], "guest")
		return cart["group_booking"], guest, stay["precheckin_token"]

	def version(self, group, field, old, new):
		return frappe.get_doc({
			"doctype": "Version", "ref_doctype": "Group Booking", "docname": group,
			"data": json.dumps({"changed": [[field, old, new]]}),
		}).insert(ignore_permissions=True)

	def erase_contacts(self):
		group, guest, token = self.book()
		public_api.precheckin_submit(
			token, consent=1, signature=signature(), id_type="Passport", id_number="R170009",
			email="arrival@example.test", address_line="17 Arrival Road", billing_address="42 Invoice Lane",
		)
		keep = "Ref INV-256700123456-09, 12567001234567. Other phone +256 700 123 457."
		current = f"Call +256 700 123 456 or arrival@example.test at 17 Arrival Road. {keep}"
		old = f"Call (256) 700-123-456 or arrival@example.test at 42 Invoice Lane. {keep}"
		frappe.db.set_value("Group Booking", group, "notes", current)
		version = self.version(group, "notes", old, current)
		alias = privacy.erase_guest(guest)["alias"]
		expected = f"Call {alias} or {alias} at {alias}. {keep}"
		return group, version.reload(), expected

	def test_notes_erase_stay_contacts_and_keep_other_numbers(self):
		group, _, expected = self.erase_contacts()
		self.assertEqual(frappe.db.get_value("Group Booking", group, "notes"), expected)

	def test_note_history_erases_contacts_in_both_values(self):
		_, version, expected = self.erase_contacts()
		self.assertEqual(json.loads(version.data)["changed"], [["notes", expected, expected]])

	def check_title(self, group_context, business):
		group, guest, _ = self.book()
		title = "Riley Vale"
		frappe.db.set_value("Group Booking", group, {"group_name": title, "notes": title, **group_context})
		version = self.version(group, "group_name", title, title)
		alias = privacy.erase_guest(guest)["alias"]
		expected = title if business else group
		self.assertEqual(frappe.db.get_value("Group Booking", group, "group_name"), expected)
		self.assertEqual(json.loads(version.reload().data)["changed"], [["group_name", expected, expected]])
		self.assertEqual(frappe.db.get_value("Group Booking", group, "notes"), alias)

	def test_company_link_preserves_proprietor_title(self):
		account = frappe.get_doc({"doctype": "Corporate Account", "company_name": self.property}).insert()
		self.check_title({"company": account.name}, business=True)

	def test_corporate_type_preserves_proprietor_title(self):
		self.check_title({"group_type": "Corporate"}, business=True)

	def test_tour_group_preserves_agency_title(self):
		self.check_title({"group_type": "Tour"}, business=True)

	def test_personal_cart_erases_title_and_history(self):
		self.check_title({}, business=False)
