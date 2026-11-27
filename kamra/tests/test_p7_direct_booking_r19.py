"""Redact guest mentions in shared notes and history without changing other contacts or references."""

import json
import runpy
from pathlib import Path

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

	def test_erase_stay_identity_from_notes_and_both_history_values(self):
		group, guest, token = self.book()
		stay = frappe.db.get_value("Reservation", {"precheckin_token": token}, "name")
		registration = {"first_name": "Anita", "last_name": "Stone", "date_of_birth": "1993-07-14",
		                "id_number": "P02345", "full_name": "Anita R. Stone", "legal_name": "Anita Marlowe"}
		frappe.db.set_value("Reservation", stay, "precheckin_registration", frappe.as_json(registration))
		keep = " Keep Stonebridge, REF-Stone-42, P023456 and 1993-07-15."
		self.check_notes(
			group, guest,
			"Passport name Anita Stone, DOB 1993-07-14. Anita / Stone / Anita R. Stone / Anita Marlowe / P02345." + keep,
			"Passport name {alias}, DOB {alias}. Anita / Stone / {alias} / {alias} / {alias}." + keep,
		)
		self.assertFalse(frappe.db.get_value("Reservation", stay, "precheckin_registration"))

	def test_full_stay_name_erasure_preserves_other_guest_and_prose(self):
		group, guest, token = self.book()
		stay = frappe.db.get_value("Reservation", {"precheckin_token": token}, "name")
		registration = {"first_name": "May", "last_name": "Stone"}
		frappe.db.set_value("Reservation", stay, "precheckin_registration", frappe.as_json(registration))
		self.check_notes(group, guest, "May Stone arrives first. June Stone may arrive later.",
		                 "{alias} arrives first. June Stone may arrive later.")

	def check_booking_name_with_registration(self, booking_name):
		group, guest, token = self.book(booking_name)
		stay = frappe.get_doc("Reservation", {"precheckin_token": token})
		self.assertEqual(stay.guest_name, booking_name)
		self.assertEqual(frappe.db.get_value("Guest", guest, "full_name"), booking_name)
		stay.db_set("precheckin_registration", frappe.as_json({"first_name": "May", "last_name": "Stone"}))
		self.check_notes(group, guest, "May Stone arrives first. June Stone may arrive later.",
		                 "{alias} arrives first. June Stone may arrive later.")

	def test_given_name_booking_yields_to_registered_full_name(self):
		self.check_booking_name_with_registration("May")

	def test_surname_booking_yields_to_registered_full_name(self):
		self.check_booking_name_with_registration("Stone")

	def test_single_word_only_booking_preserves_lowercase_prose(self):
		group, guest, _ = self.book("May")
		self.check_notes(group, guest, "May arrives first. June Stone may arrive later.",
		                 "{alias} arrives first. June Stone may arrive later.")

	def test_mononym_does_not_hide_independent_guest_id(self):
		group, guest, _ = self.book("AB12345")
		frappe.db.set_value("Guest", guest, "id_number", "ab12345")
		self.check_notes(group, guest, "Passport: ab12345. Keep REF-ab12345-2.",
		                 "Passport: {alias}. Keep REF-ab12345-2.")

	def test_booking_name_component_does_not_hide_independent_city(self):
		group, guest, token = self.book("Paris")
		stay = frappe.get_doc("Reservation", {"precheckin_token": token})
		stay.db_set("precheckin_registration", frappe.as_json({"first_name": "Paris", "last_name": "Jones"}))
		frappe.db.set_value("Guest", guest, "city", "Paris")
		self.check_notes(group, guest, "Paris Jones arrives. Home city: Paris. Former home: PARIS.",
		                 "{alias} arrives. Home city: {alias}. Former home: {alias}.")

	def test_incomplete_stay_names_preserve_other_guest_and_prose(self):
		group, guest, _ = self.book()
		stays = frappe.get_all("Reservation", {"group_booking": group}, pluck="name")
		self.assertEqual(len(stays), 2)
		for stay, partial in zip(stays, ({"first_name": "May"}, {"last_name": "Stone"})):
			frappe.db.set_value("Reservation", stay, "precheckin_registration", frappe.as_json(partial))
		note = "June Stone may arrive later."
		self.check_notes(group, guest, note, note)

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
			" Keep INV-Riley-02, REF_Riley_02, RES/Riley/02, FOLIO.Riley.02, INV-Riley, Riley-02, REF-Vale. INV-Riley Vale-02."
		)
		prose = """{}, {}. {}'s note: ({}), '{}', "{}"; {} — late arrival."""
		self.check_notes(
			group,
			guest,
			prose.format(*["Riley Vale"] * 7) + " Riley and June Vale." + keep,
			"""{alias}, {alias}. {alias}'s note: ({alias}), '{alias}', "{alias}"; {alias} — late arrival."""
			+ " Riley and June Vale." + keep,
		)

	def test_full_name_with_hyphenated_given_name_is_erased_as_one_prose_token(self):
		group, guest, _ = self.book()
		frappe.db.set_value("Guest", guest, "first_name", "Mary-Jane")
		self.check_notes(
			group,
			guest,
			"Mary-Jane Vale. (Mary-Jane Vale), keep Mary-Jane and INV-Mary-Jane-02.",
			"{alias}. ({alias}), keep Mary-Jane and INV-Mary-Jane-02.",
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

	def test_mailbox_inside_emphasized_phrase(self):
		group, guest, _ = self.book()
		stay = frappe.db.get_value("Reservation", {"group_booking": group}, "name")
		email = "stay-only@example.test"
		frappe.db.set_value("Reservation", stay, "booking_contact_email", email)
		note = (
			"Desk wrote **{mailbox} is preferred.** Try *contact {mailbox} today* or **{mailbox}.** "
			"Use __{mailbox} for this stay__. Keep events@example.test, xstay-only@example.test "
			"and stay-only@example.test.org."
		)
		self.check_notes(group, guest, note.format(mailbox=email), note.format(mailbox="{alias}"))


	def test_struck_through_booking_mailbox(self):
		group, guest, _ = self.book()
		stay = frappe.db.get_value("Reservation", {"group_booking": group}, "name")
		email = "stay-only@example.test"
		frappe.db.set_value("Reservation", stay, "booking_contact_email", email)
		note = "Old address: ~~{mailbox}~~. Use events@example.test. Also ~~{mailbox} is old~~ and ~{mailbox}~."
		self.check_notes(group, guest, note.format(mailbox=email), note.format(mailbox="{alias}"))

	def test_literal_recipients_and_emphasis_boundaries(self):
		group, guest, _ = self.book()
		stay = frappe.db.get_value("Reservation", {"group_booking": group}, "name")
		email = "stay-only@example.test"
		frappe.db.set_value("Reservation", stay, "booking_contact_email", email)
		note = (
			"CC <_stay-only@example.test>. Use _email_ for updates. "
			"Keep <__stay-only@example.test_>, even before __text__.\n"
			"CC _stay-only@example.test before <flag_> or _updates_.\n"
			"CC _stay-only@example.test and <events@example.test> for _updates_.\n"
			"CC _stay-only@example.test and events@desk_team.test for _updates_.\n"
			"CC _stay-only@example.test on this line.\nUse _email_ on the next.\n"
			"Erase <{mailbox}> and _{mailbox}_."
		)
		self.check_notes(group, guest, note.format(mailbox=email), note.format(mailbox="{alias}"))

	def test_emphasis_spans_two_mailboxes(self):
		group, guest, _ = self.book()
		stay = frappe.db.get_value("Reservation", {"group_booking": group}, "name")
		email = "stay-only@example.test"
		frappe.db.set_value("Reservation", stay, "booking_contact_email", email)
		note = "Send to _{mailbox} or events@example.test_."
		self.check_notes(group, guest, note.format(mailbox=email), note.format(mailbox="{alias}"))

	def test_underscore_in_reference_cannot_close_emphasis(self):
		group, guest, _ = self.book()
		stay = frappe.db.get_value("Reservation", {"group_booking": group}, "name")
		frappe.db.set_value("Reservation", stay, "booking_contact_email", "stay-only@example.test")
		note = "CC _stay-only@example.test for order REF_42."
		self.check_notes(group, guest, note, note)


	def test_nested_emphasis_erases_only_the_marked_recipient(self):
		group, guest, _ = self.book()
		stay = frappe.db.get_value("Reservation", {"group_booking": group}, "name")
		email = "stay-only@example.test"
		frappe.db.set_value("Reservation", stay, "booking_contact_email", email)
		note = ("Send to _{mailbox} for __billing__ only_.\n"
		        "Send to __{mailbox} for _billing_ only__.\n"
		        "CC _stay-only@example.test for order REF_42.")
		self.check_notes(group, guest, note.format(mailbox=email), note.format(mailbox="{alias}"))


class TestChineseCartTranslations(IntegrationTestCase):
	def test_add_on_refusal_in_both_chinese_locales(self):
		for lang in ("zh", "zh-CN"):
			with self.subTest(lang=lang):
				translated = frappe._("A selected add-on is no longer available.", lang=lang)
				self.assertRegex(translated, r"[\u4e00-\u9fff]")

	def test_chinese_catalog_covers_cart_messages(self):
		extractor = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/i18n/extract_backend_strings.py"))
		strings, _, _ = extractor["extract_strings"]()
		cart_messages = {text for text, paths in strings.items() if "kamra/direct_booking_cart.py" in paths}
		self.assertTrue(cart_messages)
		self.assertEqual(cart_messages - extractor["existing_translations"]("zh").keys(), set())
