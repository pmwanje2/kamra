"""Erase the primary guest's identifiers without changing other contacts on a shared group."""

import json

import frappe
from frappe.tests import IntegrationTestCase

from kamra import privacy, public_api
from kamra.tests import test_p7_direct_booking_r17 as r17
from kamra.tests.test_p7_direct_booking_cart_r2 import signature


class TestSharedGroupErasure(IntegrationTestCase):
	setUp = r17.TestGroupPrivacy.setUp
	make_room = r17.TestGroupPrivacy.make_room
	line = r17.TestGroupPrivacy.line
	book = r17.TestGroupPrivacy.book
	version = r17.TestGroupPrivacy.version

	def check_erasure(self, context, note, expected):
		group, guest, token = self.book()
		frappe.db.set_value("Guest", guest, "address_line", "14 Profile Street")
		public_api.precheckin_submit(
			token, consent=1, signature=signature(), id_type="Passport", id_number="R170009",
			email="arrival@example.test", address_line="9 Arrival Road",
		)
		title = "Riley Vale"
		frappe.db.set_value("Group Booking", group, {"group_name": title, "notes": note, **context})
		history = self.version(group, "notes", "Earlier: " + note, "Later: " + note)
		title_history = self.version(group, "group_name", title, title)
		alias = privacy.erase_guest(guest)["alias"]
		expected = expected.format(alias=alias)
		row = frappe.db.get_value("Group Booking", group, ["notes", "group_name"], as_dict=True)
		self.assertEqual(row.group_name, title)
		self.assertEqual(json.loads(title_history.reload().data)["changed"], [["group_name", title, title]])
		self.assertEqual(
			[row.notes, *json.loads(history.reload().data)["changed"][0][1:]],
			[expected, "Earlier: " + expected, "Later: " + expected],
		)

	def test_submitted_id_and_profile_address(self):
		account = frappe.get_doc({"doctype": "Corporate Account", "company_name": self.property}).insert()
		keep = "Other IDs AR170009, R1700098, REF-R170009-02. Ref INV-256700123456-09."
		self.check_erasure(
			{"company": account.name},
			"R170009 at 14 Profile Street. Phone (256) 700-123-456. " + keep,
			"{alias} at {alias}. Phone {alias}. " + keep,
		)

	def test_only_complete_email_addresses_are_erased(self):
		keep = "Copy team.arrival@example.test and arrival@example.test.org."
		self.check_erasure(
			{"group_type": "Tour"},
			"Email ARRIVAL@example.test. " + keep,
			"Email {alias}. " + keep,
		)

	def test_similar_profile_contacts_are_preserved(self):
		keep = "Copy team.profile@example.test, profile@example.test.org and +2567001234569."
		self.check_erasure(
			{"group_type": "Corporate"},
			"Profile <profile@example.test>, stay 9 Arrival Road. " + keep,
			"Profile <{alias}>, stay {alias}. " + keep,
		)
