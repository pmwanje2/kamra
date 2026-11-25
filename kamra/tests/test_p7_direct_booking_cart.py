"""Integration coverage for a public cart and its reservation-scoped registration."""

from decimal import Decimal

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from kamra import public_api
from kamra.settings import clear_cache, write_kv


class TestDirectBookingCart(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		self.property = frappe.get_doc({
			"doctype": "Property", "property_name": "P7 " + frappe.generate_hash(length=8),
			"booking_engine_enabled": 1, "booking_payment_mode": "Pay at hotel",
		}).insert().name
		write_kv(self.property, "rate_plan_required", 0)
		clear_cache(self.property)
		self.standard = self.make_room("STD", "101")
		self.family = self.make_room("FAM", "201")
		self.ci = add_days(nowdate(), 5)
		self.co = add_days(nowdate(), 7)
		self.meal = frappe.get_doc({
			"doctype": "Meal Plan", "property": self.property, "code": "CP",
			"label": "Breakfast", "price_per_adult": 12.345,
		}).insert().name
		self.lines = [
			self.line(self.standard, self.ci, self.co, 1),
			{**self.line(self.family, add_days(self.ci, 1), add_days(self.co, 1), 2),
			 "meal_plan": self.meal},
		]

	def make_room(self, code, number):
		room_type = frappe.get_doc({
			"doctype": "Room Type", "property": self.property, "room_type_code": code,
			"room_type_name": code, "base_price": 125.125, "adults_capacity": 2,
		}).insert().name
		frappe.get_doc({
			"doctype": "Room", "property": self.property, "room_type": room_type,
			"room_number": number,
		}).insert()
		return room_type

	def line(self, room_type, start, end, adults):
		return {"room_type": room_type, "check_in_date": start, "check_out_date": end,
		        "adults": adults, "children": 0}

	def book(self, lines):
		return public_api.create_multi_room_booking(
			self.property, lines, guest_name="Cart Guest", phone="+256700000919",
		)

	def test_overlapping_lines_refused_and_cart_left_empty(self):
		lines = [self.lines[0], self.line(self.standard, add_days(self.ci, 1), self.co, 2)]
		with self.assertRaises(frappe.ValidationError):
			public_api.cart_quote(self.property, lines)
		with self.assertRaises(frappe.ValidationError):
			self.book(lines)
		for doctype in ("Reservation", "Group Booking"):
			self.assertFalse(frappe.db.exists(doctype, {"property": self.property}))

	def test_server_prices_and_groups_mixed_dates(self):
		for currency, places in (("UGX", 0), ("USD", 2), ("KWD", 3)):
			with self.subTest(currency=currency):
				frappe.db.set_value("Property", self.property, "currency", currency)
				frappe.clear_document_cache("Property", self.property)
				clear_cache(self.property)
				clean = public_api.cart_quote(self.property, self.lines)
				tampered = [{**line, "total": 0.001, "unit_price": 0, "tax": -100}
				            for line in self.lines]
				priced = public_api.cart_quote(self.property, tampered)
				self.assertEqual(clean, priced)
				self.assertEqual(priced["currency"], currency)
				self.assertEqual(priced["currency_decimals"], places)
				total = Decimal(str(priced["amount_after_tax"]))
				self.assertGreater(total, 0)
				self.assertEqual(total, total.quantize(Decimal(1).scaleb(-places)))
		result = self.book(tampered)
		self.assertEqual(result["amount_after_tax"], priced["amount_after_tax"])
		for line, booked in zip(self.lines, result["reservations"], strict=True):
			res = frappe.get_doc("Reservation", booked["reservation"])
			self.assertEqual(res.group_booking, result["group_booking"])
			self.assertEqual(str(res.check_in_date), line["check_in_date"])
			self.assertEqual(str(res.check_out_date), line["check_out_date"])
			self.assertEqual(res.adults, line["adults"])
			self.assertEqual(res.meal_plan or "", line.get("meal_plan", ""))

	def test_upsell_posts_through_reservation_fixed_charges(self):
		code = frappe.get_doc({
			"doctype": "Transaction Code", "code": "P7-" + frappe.generate_hash(length=6),
			"description": "Transfer", "txn_type": "Revenue", "property": self.property,
			"charge_type": "Taxi", "default_tax_rate": 18,
		}).insert().name
		addon = frappe.get_doc({
			"doctype": "Experience", "property": self.property, "experience_name": "Transfer",
			"price": 21, "show_on_booking_page": 1, "direct_booking_basis": "Per guest",
			"direct_booking_transaction_code": code,
		}).insert().name
		lines = [self.lines[0], {**self.lines[1], "upsells": [addon]}]
		base = public_api.cart_quote(self.property, self.lines)
		quote = public_api.cart_quote(self.property, lines)
		self.assertGreater(quote["amount_after_tax"], base["amount_after_tax"] + 42)
		result = self.book(lines)
		self.assertEqual(result["amount_after_tax"], quote["amount_after_tax"])
		res = frappe.get_doc("Reservation", result["reservations"][1]["reservation"])
		charge, = res.fixed_charges
		self.assertEqual((charge.transaction_code, charge.qty, charge.unit_price, charge.frequency),
		                 (code, 2, 21, "On Arrival"))
		self.assertFalse(res.addons)

	def test_precheckin_requires_consent_and_only_changes_token_stay(self):
		frappe.db.set_value("Property", self.property, "show_online_precheckin_completion", 1)
		result = self.book(self.lines)
		first, second = [frappe.get_doc("Reservation", r["reservation"])
		                 for r in result["reservations"]]
		token = first.precheckin_token
		with self.assertRaises(frappe.ValidationError):
			public_api.precheckin_info(second.name)
		with self.assertRaises(frappe.ValidationError):
			public_api.precheckin_submit(token, "Passport", "P700100", consent=0)
		from kamra.tests.test_p7_direct_booking_cart_r2 import signature

		payload = public_api.precheckin_submit(
			token, "Passport", "P700100", consent=1, eta="18:30", signature=signature(),
			address_line="10 High Street", city="Kampala",
			special_requests="Quiet room", billing_address="10 High Street, Kampala",
		)
		self.assertEqual(payload["reservation"], first.name)
		first.reload()
		second.reload()
		self.assertEqual(first.precheckin_status, "Submitted")
		self.assertTrue(first.precheckin_on)
		self.assertEqual(first.precheckin_billing_address, "10 High Street, Kampala")
		self.assertFalse(second.precheckin_on)
		self.assertNotIn("id_number", public_api.precheckin_info(token)["guest"])

		from kamra.api import front_desk_snapshot
		arrivals = front_desk_snapshot(self.property, self.ci)["arrivals"]
		self.assertTrue(next(row for row in arrivals if row.name == first.name).pre_checked_in)

