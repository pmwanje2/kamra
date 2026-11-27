"""R3 coverage for the public cart, Desk documents, and booked add-on balances."""

import csv
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from kamra import api, fixed_charges, public_api
from kamra.folio import open_folio
from kamra.settings import clear_cache, write_kv
from kamra.tests import test_p7_direct_booking_cart as original
from kamra.tests import test_p7_direct_booking_cart_r2 as r2


class TestCartR3(IntegrationTestCase):
	setUp = original.TestDirectBookingCart.setUp
	make_room = original.TestDirectBookingCart.make_room
	line = original.TestDirectBookingCart.line
	book = original.TestDirectBookingCart.book
	add_on = r2.TestDirectBookingCartR2.add_on

	def stay(self):
		booked = self.book([self.lines[0]])["reservations"][0]
		return frappe.get_doc("Reservation", booked["reservation"])

	def test_released_access_uses_the_registration_link_lifetime(self):
		res = self.stay()
		frappe.db.set_value("Property", self.property, "access_instructions", "Fixture directions")
		frappe.db.set_value("Reservation", res.name, "access_released", 1)
		self.assertEqual(public_api.access_info(res.precheckin_token)["access_instructions"], "Fixture directions")
		frappe.db.set_value("Reservation", res.name, "precheckin_token_expires_at", add_days(nowdate(), -1))
		for status in ("Confirmed", "Checked In"):
			with self.subTest(status=status):
				frappe.db.set_value("Reservation", res.name, "status", status)
				with self.assertRaises(frappe.ValidationError):
					public_api.access_info(res.precheckin_token)

	def test_verify_reads_the_scan_displayed_for_the_selected_stay(self):
		from kamra.localization import india

		res = self.stay()
		api.upload_guest_document(res.guest, "id", r2.signature("blue"))
		api.upload_id_document(res.name, r2.signature("red"))
		stay_url = api.registration_card(res.name)["guest"]["id_file"]
		stay_file = frappe.get_doc("File", {"file_url": stay_url, "attached_to_name": res.name})
		other = frappe.get_doc({"doctype": "Guest", "first_name": "Other"}).insert()
		with patch("kamra.localization.verify_released", return_value=True), \
		     patch("kamra.localization.pack_for", return_value=india), \
		     patch("kamra.connect.client.call", return_value={"charged": 1}) as partner:
			api.verify_guest_id(self.property, res.guest, "Passport", consent=1, reservation=res.name)
			self.assertEqual(partner.call_args.kwargs["data"], stay_file.get_content())
			with self.assertRaises(frappe.PermissionError):
				api.verify_guest_id(self.property, other.name, "Passport", consent=1, reservation=res.name)
			self.assertEqual(partner.call_count, 1)

	def test_closed_card_rejects_upload_when_stay_slot_is_empty(self):
		res = self.stay()
		for status in ("Cancelled", "Checked Out"):
			frappe.db.set_value("Reservation", res.name, "status", status)
			for kind in ("address", "id"):
				with self.subTest(status=status, kind=kind), self.assertRaises(frappe.ValidationError):
					api.upload_guest_document(res.guest, kind, r2.signature(), reservation=res.name)
		self.assertFalse(frappe.db.exists("File", {"attached_to_doctype": "Guest", "attached_to_name": res.guest}))

	def test_connecting_rooms_book_distinct_complete_units(self):
		write_kv(self.property, "assign_room_on_booking", 1)
		clear_cache(self.property)
		for number in ("102", "103", "104"):
			frappe.get_doc({"doctype": "Room", "property": self.property,
			                "room_type": self.standard, "room_number": number}).insert()
		rooms = frappe.get_all("Room", filters={"room_type": self.standard}, pluck="name", order_by="room_number")
		for name in frappe.get_all("Sellable Unit", filters={"room_type": self.standard}, pluck="name"):
			frappe.delete_doc("Sellable Unit", name)
		units = {}
		for pair in (rooms[:2], rooms[2:]):
			unit = frappe.get_doc({"doctype": "Sellable Unit", "property": self.property,
			                       "unit_name": pair[0], "room_type": self.standard, "unit_kind": "composite",
			                       "is_active": 1, "physical_rooms": [{"room": r} for r in pair]}).insert()
			units[unit.name] = pair
		lines = [self.lines[0], self.lines[0]]
		self.assertEqual(len(public_api.cart_quote(self.property, lines)["lines"]), 2)
		stays = [frappe.get_doc("Reservation", r["reservation"]) for r in self.book(lines)["reservations"]]
		self.assertEqual({r.sellable_unit for r in stays}, set(units))
		for res in stays:
			self.assertIn(res.room, units[res.sellable_unit])
		with self.assertRaises(frappe.ValidationError):
			public_api.cart_quote(self.property, [self.lines[0]])

	def test_expected_edited_upsells_match_folio_in_home_minor_units(self):
		code, addon = self.add_on()
		for i, home in enumerate(("UGX", "USD", "KWD")):
			with self.subTest(home=home):
				frappe.db.set_value("Property", self.property, "currency", home)
				frappe.clear_document_cache("Property", self.property)
				clear_cache(self.property)
				line = self.line(self.standard, add_days(self.ci, i * 3), add_days(self.co, i * 3), 1)
				res = frappe.get_doc("Reservation", self.book([{**line, "upsells": [addon]}])["reservations"][0]["reservation"])
				row = res.fixed_charges[0]
				fixed_charges.save_fixed_charge(res.name, {**row.as_dict(), "currency": "EUR", "unit_price": 2.17})
				with patch("kamra.pricing.posting_fx_rate", return_value=(Decimal("4.325"), self.ci)):
					expected = fixed_charges.booked_upsell_totals([res.name])[res.name]
					folio = frappe.get_doc("Folio", open_folio(res))
				charge, = [c for c in folio.charges if c.transaction_code == code]
				self.assertEqual(expected, Decimal(str(charge.amount)) + Decimal(str(charge.gst_amount)))

	def test_backend_labels_are_translated(self):
		base = Path(__file__).resolve().parents[1] / "translations"
		labels = ("Direct Booking Transaction Code", "Direct Booking Price Basis", "Show Online Pre-Check-In Completion")
		for lang, translated in {
			"es": ("Código de transacción de la reserva directa", "Base de cálculo del precio de la reserva directa",
			       "Mostrar la finalización del prerregistro en línea"),
			"ar": ("رمز معاملة الحجز المباشر", "أساس تسعير الحجز المباشر", "إظهار اكتمال التسجيل المسبق عبر الإنترنت"),
		}.items():
			with self.subTest(lang=lang), (base / f"{lang}.csv").open(encoding="utf-8") as catalog:
				entries = {row[0]: row[1] for row in csv.reader(catalog) if len(row) >= 2}
				self.assertEqual(tuple(entries.get(key) for key in labels), translated)
