"""R1 regression cases for inventory, checkout, and online registration."""

from base64 import b64encode
from io import BytesIO
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate
from PIL import Image, ImageDraw

from kamra import api, public_api
from kamra.tests import test_p7_direct_booking_cart as original


def signature(color="black"):
	image = Image.new("RGBA", (12, 12), (255, 255, 255, 0))
	ImageDraw.Draw(image).line((2, 2, 9, 9), fill=color, width=2)
	buffer = BytesIO()
	image.save(buffer, "PNG")
	return "data:image/png;base64," + b64encode(buffer.getvalue()).decode()


class TestDirectBookingCartR2(IntegrationTestCase):
	setUp = original.TestDirectBookingCart.setUp
	make_room = original.TestDirectBookingCart.make_room
	line = original.TestDirectBookingCart.line

	def book(self, lines=None, **kwargs):
		return public_api.create_multi_room_booking(
			self.property, self.lines if lines is None else lines,
			guest_name="R2 Guest", phone="+256700000918", **kwargs,
		)

	def add_on(self):
		code = frappe.get_doc({
			"doctype": "Transaction Code", "code": "R2-" + frappe.generate_hash(length=6),
			"description": "Transfer", "txn_type": "Revenue", "property": self.property,
			"charge_type": "Taxi", "default_tax_rate": 18,
		}).insert().name
		addon = frappe.get_doc({
			"doctype": "Experience", "property": self.property, "experience_name": "Transfer",
			"price": 25, "show_on_booking_page": 1, "direct_booking_basis": "Per stay",
			"direct_booking_transaction_code": code,
		}).insert().name
		return code, addon

	def test_joint_room_assignment_requires_distinct_continuous_rooms(self):
		mid = add_days(self.ci, 1)
		for i, start, end in ((102, self.ci, mid), (103, mid, self.co)):
			room = frappe.get_doc({
				"doctype": "Room", "property": self.property, "room_type": self.standard,
				"room_number": str(i),
			}).insert()
			frappe.get_doc({
				"doctype": "Room Block", "property": self.property, "room": room.name,
				"from_date": start, "to_date": end, "reason": "Maintenance", "block_status": "Active",
			}).insert()
		lines = [self.lines[0], self.lines[0]]
		with self.subTest(endpoint="quote"), self.assertRaises(frappe.ValidationError):
			public_api.cart_quote(self.property, lines)
		with self.subTest(endpoint="book"), self.assertRaises(frappe.ValidationError):
			self.book(lines)
		self.assertFalse(frappe.db.exists("Reservation", {"property": self.property}))
		self.assertFalse(frappe.db.exists("Group Booking", {"property": self.property}))

	def test_upsell_tax_is_locked_and_desk_matches_checkout(self):
		from kamra.folio import open_folio

		code, addon = self.add_on()
		lines = [{**self.lines[0], "upsells": [addon]}]
		booked = self.book(lines)["reservations"][0]
		res = frappe.get_doc("Reservation", booked["reservation"])
		with self.subTest(surface="desk"):
			self.assertEqual(api._stay_balances([res.name])[res.name]["total"], booked["amount_after_tax"])
		frappe.db.set_value("Transaction Code", code, "default_tax_rate", 5)
		folio = frappe.get_doc("Folio", open_folio(res))
		with self.subTest(surface="folio"):
			[charge] = [row for row in folio.charges if row.transaction_code == code]
			self.assertEqual(charge.gst_rate, 18)
			self.assertAlmostEqual(charge.amount + charge.gst_amount, 29.50)

	def test_checkout_replay_keeps_one_group_and_one_fixed_charge(self):
		_, addon = self.add_on()
		key = "cart-" + frappe.generate_hash(length=32)
		lines = [{**self.lines[0], "upsells": [addon]}, self.lines[1]]
		first = self.book(lines, idempotency_key=key)
		self.assertEqual(self.book(lines, idempotency_key=key), first)
		self.assertEqual(frappe.db.count("Reservation", {"property": self.property}), 2)
		self.assertEqual(frappe.db.count("Group Booking", {"property": self.property}), 1)
		name = first["reservations"][0]["reservation"]
		self.assertEqual(frappe.db.count("Reservation Fixed Charge", {"parent": name}), 1)
		with self.assertRaises(frappe.ValidationError):
			self.book([self.lines[0]], idempotency_key=key)

	def test_exhausted_voucher_refuses_checkout(self):
		voucher = frappe.get_doc({
			"doctype": "Discount Voucher", "property": self.property, "voucher_code": "R2PROMO",
			"discount_type": "Percent", "value": 10, "max_uses": 1,
		}).insert()
		lines = [{**self.lines[0], "voucher_code": "R2PROMO"}]
		public_api.cart_quote(self.property, lines)
		frappe.db.set_value("Discount Voucher", voucher.name, "times_used", 1)
		with self.assertRaisesRegex(frappe.ValidationError, "promo code"):
			self.book(lines)
		self.assertFalse(frappe.db.exists("Reservation", {"property": self.property}))

	def test_link_expires_when_the_stay_ends(self):
		booked = self.book([self.lines[0]])["reservations"][0]
		frappe.db.set_value("Reservation", booked["reservation"], "check_out_date", add_days(nowdate(), -1))
		with self.assertRaises(frappe.ValidationError):
			public_api.precheckin_info(booked["precheckin_token"])

	def test_resending_rotates_the_link(self):
		booked = self.book([self.lines[0]])["reservations"][0]
		with patch("kamra.agents_channels.send_outbound", return_value={"sent": False}), patch.object(frappe.db, "commit"):
			next_link = api.send_precheckin_link(booked["reservation"])["link"]
		new_token = next_link.rsplit("/", 1)[-1]
		self.assertNotEqual(new_token, booked["precheckin_token"])
		with self.assertRaises(frappe.ValidationError):
			public_api.precheckin_info(booked["precheckin_token"])
		self.assertEqual(public_api.precheckin_info(new_token)["stay"]["reservation"], booked["reservation"])

	def test_blank_signature_is_refused(self):
		booked = self.book([self.lines[0]])["reservations"][0]
		with self.assertRaises(frappe.ValidationError):
			public_api.precheckin_submit(booked["precheckin_token"], "Passport", "P700100", signature="", consent=1)

	def test_original_consent_cannot_be_replaced(self):
		booked = self.book([self.lines[0]])["reservations"][0]
		token = booked["precheckin_token"]
		first = signature()
		public_api.precheckin_submit(token, "Passport", "P700100", signature=first, consent=1)
		with self.assertRaises(frappe.ValidationError):
			public_api.precheckin_submit(token, "Passport", "P700100", signature=signature("blue"), consent=1)
		self.assertEqual(frappe.db.get_value("Reservation", booked["reservation"], "precheckin_signature"), first)

	def test_legacy_experience_remains_selectable_and_is_persisted(self):
		_, addon = self.add_on()
		with self.subTest(surface="showcase"):
			self.assertIn(addon, [row["name"] for row in public_api.showcase(self.property)["experiences"]])
		with patch.object(frappe.db, "commit"):
			result = public_api.book(
				self.property, self.standard, self.ci, self.co, "Legacy Guest", "+256700000917",
				addons=[{"experience": addon, "qty": 2}],
			)
		res = frappe.get_doc("Reservation", result["reservation"])
		self.assertEqual([(row.experience, row.qty) for row in res.addons], [(addon, 2)])
