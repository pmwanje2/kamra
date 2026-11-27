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

	def test_email_at_start_of_quoted_sentence(self):
		group, guest, _ = self.book()
		frappe.db.set_value("Guest", guest, "email", "profile@example.test")
		note = """Desk wrote 'profile@example.test is preferred'. Or "profile@example.test is fine"."""
		expected = """Desk wrote '{alias} is preferred'. Or "{alias} is fine"."""
		self.check_notes(group, guest, note, expected)

	def test_city_with_same_letters_as_name_preserves_reference(self):
		group, guest, _ = self.book()
		frappe.db.set_value("Guest", guest, "city", "RILEY")
		self.check_notes(group, guest, "Riley. Keep INV-Riley-02.", "{alias}. Keep INV-Riley-02.")

	def test_each_historical_booking_mailbox_is_erased(self):
		group, guest, _ = self.book()
		first, second = frappe.get_all("Reservation", {"group_booking": group}, pluck="name", order_by="name")
		frappe.db.set_value("Reservation", first, "booking_contact_email", "stay-only@example.test, second@example.test")
		frappe.db.set_value("Reservation", second, "booking_contact_email", "third@example.test; fourth@example.test fifth@example.test")
		keep = " Keep events@example.test and not-stay-only@example.test."
		contacts = "Stay-only@example.test, SECOND@example.test; third@example.test fourth@example.test fifth@example.test."
		self.check_notes(group, guest, contacts + keep, "{alias}, {alias}; {alias} {alias} {alias}." + keep)

	def test_wrapped_booking_mailbox_is_erased_without_matching_other_addresses(self):
		group, guest, _ = self.book()
		stay = frappe.db.get_value("Reservation", {"group_booking": group}, "name")
		email = "stay-only@example.test"
		frappe.db.set_value("Reservation", stay, "booking_contact_email", email)
		wrapped = """**{}** *{}* _{}_ `{}` <{}> ({}) [{}] "{}" '{}'"""
		keep = " Keep events@example.test, **x-stay-only@example.test** and stay-only@example.test.org."
		self.check_notes(group, guest, wrapped.format(*[email] * 9) + keep,
		                 wrapped.format(*["{alias}"] * 9) + keep)

	def test_punctuation_inside_emphasis_preserves_only_the_surrounding_prose(self):
		group, guest, _ = self.book()
		stay = frappe.db.get_value("Reservation", {"group_booking": group}, "name")
		email = "stay-only@example.test"
		frappe.db.set_value("Reservation", stay, "booking_contact_email", email)
		variants = " ".join(
			f"{marker}{{mailbox}}{punctuation}{marker}"
			for marker in ("*", "**", "_", "__", "`")
			for punctuation in (".", ",", ";", ":", "!", "?", "?!", "…")
		)
		note = (
			"Write to **{mailbox}.** Keep events@example.test. " + variants
			+ " Keep *xstay-only@example.test,* and **stay-only@example.test.org.**"
		)
		self.check_notes(group, guest, note.format(mailbox=email), note.format(mailbox="{alias}"))
