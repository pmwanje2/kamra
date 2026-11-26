"""Redact guest mentions in shared notes and history without changing other contacts or references."""

import json

import frappe
from frappe.tests import IntegrationTestCase

from kamra import privacy, public_api
from kamra.tests import test_p7_direct_booking_r17 as r17
from kamra.tests.test_p7_direct_booking_cart_r2 import signature


class TestGroupErasureBoundaries(IntegrationTestCase):
	setUp = r17.TestGroupPrivacy.setUp
	make_room = r17.TestGroupPrivacy.make_room
	line = r17.TestGroupPrivacy.line
	book = r17.TestGroupPrivacy.book
	version = r17.TestGroupPrivacy.version

	def check_notes(self, group, guest, note, expected):
		frappe.db.set_value("Group Booking", group, "notes", note)
		history = self.version(group, "notes", "Before: " + note, "After: " + note)
		alias = privacy.erase_guest(guest)["alias"]
		redacted = expected.format(alias=alias)
		self.assertEqual(
			[
				frappe.db.get_value("Group Booking", group, "notes"),
				*json.loads(history.reload().data)["changed"][0][1:],
			],
			[redacted, "Before: " + redacted, "After: " + redacted],
		)

	def test_quoted_stay_and_apostrophe_profile_emails(self):
		group, guest, token = self.book()
		frappe.db.set_value("Guest", guest, "email", "o'neill@example.test")
		public_api.precheckin_submit(
			token,
			consent=1,
			signature=signature(),
			id_type="Passport",
			id_number="R190009",
			email="arrival@example.test",
		)
		wrappers = """Contact '{}'. "{}", <{}>; ({}), [{}]! `{}`?"""
		keep = " Keep team.arrival@example.test, 'team.arrival@example.test' and arrival@example.test.org."
		self.check_notes(
			group,
			guest,
			wrappers.format(*["arrival@example.test"] * 6)
			+ " Or o'neill@example.test, 'o'neill@example.test'."
			+ keep,
			wrappers.format(*["{alias}"] * 6) + " Or {alias}, '{alias}'." + keep,
		)

	def test_names_in_prose_and_reference_codes(self):
		group, guest, _ = self.book()
		keep = (
			" Keep INV-Riley-02, REF_Riley_02, RES/Riley/02, FOLIO.Riley.02, INV-Riley, Riley-02, REF-Vale."
		)
		prose = """{} Vale, {}. {}'s note: ({}), '{}', "{}"; {} — late arrival."""
		self.check_notes(
			group,
			guest,
			prose.format(*["Riley"] * 7) + keep,
			"""{alias}, {alias}. {alias}'s note: ({alias}), '{alias}', "{alias}"; {alias} — late arrival."""
			+ keep,
		)

	def test_hyphenated_given_name_is_erased_as_one_prose_token(self):
		group, guest, _ = self.book()
		frappe.db.set_value("Guest", guest, "first_name", "Mary-Jane")
		self.check_notes(
			group,
			guest,
			"Mary-Jane. (Mary-Jane), keep INV-Mary-Jane-02.",
			"{alias}. ({alias}), keep INV-Mary-Jane-02.",
		)
