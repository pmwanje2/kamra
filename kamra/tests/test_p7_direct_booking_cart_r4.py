"""Desk and online reservations share the same physical inventory and currency rules."""

from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from kamra import api, fixed_charges, public_api
from kamra.settings import clear_cache, write_kv
from kamra.tests import test_p7_direct_booking_cart as original
from kamra.tests import test_p7_direct_booking_cart_r2 as r2


class TestCartR4(IntegrationTestCase):
	setUp = original.TestDirectBookingCart.setUp
	make_room = original.TestDirectBookingCart.make_room
	line = original.TestDirectBookingCart.line
	book = original.TestDirectBookingCart.book
	add_on = r2.TestDirectBookingCartR2.add_on

	def pairs(self, own_type=False):
		write_kv(self.property, "assign_room_on_booking", 1)
		clear_cache(self.property)
		for number in ("102", "103", "104"):
			frappe.get_doc({"doctype": "Room", "property": self.property,
			                "room_type": self.standard, "room_number": number}).insert()
		rooms = frappe.get_all("Room", filters={"room_type": self.standard},
		                       pluck="name", order_by="room_number")
		for name in frappe.get_all("Sellable Unit", filters={"room_type": self.standard}, pluck="name"):
			frappe.delete_doc("Sellable Unit", name)
		sold_type = self.standard
		if own_type:
			sold_type = frappe.get_doc({"doctype": "Room Type", "property": self.property,
			                            "room_type_code": "PAIR", "room_type_name": "Pair",
			                            "adults_capacity": 4, "base_price": 200}).insert().name
		units = {}
		for pair in (rooms[:2], rooms[2:]):
			unit = frappe.get_doc({"doctype": "Sellable Unit", "property": self.property,
			                       "unit_name": pair[0], "room_type": sold_type, "unit_kind": "composite",
			                       "physical_rooms": [{"room": room} for room in pair]}).insert()
			units[unit.name] = pair
		return sold_type, units

	def desk(self, room, unit=None):
		guest = frappe.get_doc({"doctype": "Guest", "first_name": "Desk Guest"}).insert()
		return frappe.get_doc({"doctype": "Reservation", "property": self.property, "guest": guest.name,
		                       "room": room, "room_type": self.standard, "sellable_unit": unit,
		                       "status": "Confirmed", "adults": 1, "check_in_date": self.ci,
		                       "check_out_date": self.co}).insert()

	def test_desk_cannot_book_other_member_held_by_cart(self):
		_, units = self.pairs()
		res = frappe.get_doc("Reservation", self.book([self.lines[0]])["reservations"][0]["reservation"])
		other, = set(units[res.sellable_unit]) - {res.room}
		with self.assertRaises(frappe.ValidationError):
			self.desk(other)

	def test_unit_cannot_cover_existing_physical_booking(self):
		_, units = self.pairs()
		unit, rooms = next(iter(units.items()))
		self.desk(rooms[1])
		with self.assertRaises(frappe.ValidationError):
			self.desk(rooms[0], unit)

	def test_composites_with_different_primary_rooms_conflict(self):
		_, units = self.pairs()
		unit, rooms = next(iter(units.items()))
		self.desk(rooms[0], unit)
		other_room = next(pair[0] for name, pair in units.items() if name != unit)
		overlap = frappe.get_doc({"doctype": "Sellable Unit", "property": self.property,
		                          "unit_name": "Overlapping pair", "unit_kind": "composite",
		                          "room_type": self.standard,
		                          "physical_rooms": [{"room": r} for r in (rooms[1], other_room)]}).insert()
		with self.assertRaises(frappe.ValidationError):
			self.desk(other_room, overlap.name)

	def test_moving_between_pairs_releases_the_original_pair(self):
		_, units = self.pairs()
		res = frappe.get_doc("Reservation", self.book([self.lines[0]])["reservations"][0]["reservation"])
		old = res.sellable_unit
		dest, = set(units) - {old}
		api.move_reservation(res.name, units[dest][1])
		res.reload()
		self.assertEqual((res.sellable_unit, res.room), (dest, units[dest][1]))
		next_stay = self.book([self.lines[0]])["reservations"][0]["reservation"]
		self.assertEqual(frappe.db.get_value("Reservation", next_stay, "sellable_unit"), old)
		single = frappe.db.get_value("Room", {"room_type": self.family}, "name")
		try:
			api.move_reservation(res.name, single, confirm_price=1)
		except frappe.ValidationError:
			self.assertEqual(res.reload().room, units[dest][1])
		else:
			self.assertFalse(res.reload().sellable_unit)

	def test_pair_sold_under_its_own_type_quotes_and_books_only_real_pairs(self):
		sold_type, units = self.pairs(own_type=True)
		line = self.line(sold_type, self.ci, self.co, 2)
		self.assertEqual(len(public_api.cart_quote(self.property, [line, line])["lines"]), 2)
		with self.assertRaises(frappe.ValidationError):
			public_api.cart_quote(self.property, [line] * 3)
		stays = [frappe.get_doc("Reservation", r["reservation"]) for r in self.book([line, line])["reservations"]]
		self.assertEqual({s.sellable_unit for s in stays}, set(units))
		for stay in stays:
			self.assertEqual(stay.room_type, sold_type)
			self.assertIn(stay.room, units[stay.sellable_unit])

	def upsell_stays(self):
		_, addon = self.add_on()
		lines = [{**self.lines[0], "upsells": [addon]}, self.lines[1]]
		return [frappe.get_doc("Reservation", r["reservation"]) for r in self.book(lines)["reservations"]]

	def test_refused_foreign_charge_does_not_mutate_saved_row(self):
		bad, _ = self.upsell_stays()
		row = bad.fixed_charges[0]
		with patch("kamra.pricing.posting_fx_rate", return_value=None):
			try:
				result = fixed_charges.save_fixed_charge(bad.name, {**row.as_dict(), "currency": "EUR"})
			except frappe.ValidationError as exc:
				self.assertIn("exchange rate", str(exc))
			else:
				self.assertIn("exchange rate", result.get("error", ""))
		self.assertEqual(bad.reload().fixed_charges[0].currency, row.currency)
		with patch("kamra.pricing.posting_fx_rate", return_value=(Decimal("4.325"), self.ci)):
			result = fixed_charges.save_fixed_charge(bad.name, {**row.as_dict(), "currency": "EUR"})
			self.assertEqual(result["row"]["currency"], "EUR")

	def test_batch_reports_missing_fx_only_for_affected_stay(self):
		bad, good = self.upsell_stays()
		frappe.db.set_value("Reservation Fixed Charge", bad.fixed_charges[0].name, "currency", "EUR")
		for home in ("UGX", "USD", "KWD"):
			with self.subTest(home=home):
				frappe.db.set_value("Property", self.property, "currency", home)
				frappe.clear_document_cache("Property", self.property)
				clear_cache(self.property)
				with patch("kamra.pricing.posting_fx_rate", return_value=None):
					batch = api._stay_balances([bad.name, good.name])
				self.assertTrue(batch[bad.name]["projection_unavailable"])
				self.assertFalse(batch[good.name]["projection_unavailable"])
				self.assertEqual(Decimal(str(batch[good.name]["total"])), Decimal(str(good.amount_after_tax)))
