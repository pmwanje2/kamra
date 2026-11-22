"""A desk writer cannot use the card-only standard rate when policy is on."""

import frappe
from frappe.utils import add_days, nowdate

from kamra import api
from kamra.settings import clear_cache
from kamra.tests.audit_fixtures import AuditTestCase


class RatePlanRequiredTest(AuditTestCase):
	properties = ("TEST K3 POLICY",)

	def setUp(self):
		super().setUp()
		self.addCleanup(clear_cache, self.properties[0])
		frappe.local.lang = "en"
		self.property = frappe.get_doc({"doctype": "Property", "property_name": "TEST K3 POLICY",
			"country": "Uganda", "currency": "UGX"}).insert().name
		self.room_type = frappe.get_doc({"doctype": "Room Type", "property": self.property,
			"room_type_code": "STD", "room_type_name": "Standard", "base_price": 1000,
			"adults_capacity": 2}).insert().name
		self.room = frappe.get_doc({"doctype": "Room", "property": self.property,
			"room_type": self.room_type, "room_number": "101"}).insert().name
		self.tomorrow = str(add_days(nowdate(), 1))

	def require_plan(self):
		frappe.get_doc({"doctype": "Property Setting", "property": self.property,
			"setting_key": "rate_plan_required", "value": "1"}).insert()

	def book(self, **kwargs):
		return api.create_booking(self.property, self.room_type, nowdate(), self.tomorrow,
			"K3 Booking Guest", **kwargs)

	def walk_in(self):
		return api.walk_in(self.property, self.room_type, self.room, self.tomorrow,
			"K3 Walk-in Guest", defer_check_in=1)

	def test_booking_requires_plan_before_creating_guest(self):
		self.require_plan()
		with self.assertRaisesRegex(frappe.ValidationError, "KMR-RATE-REQ-001"):
			self.book()
		self.assertFalse(frappe.db.exists("Guest", {"full_name": "K3 Booking Guest"}))

	def test_walk_in_requires_plan_before_creating_guest(self):
		self.require_plan()
		with self.assertRaisesRegex(frappe.ValidationError, "KMR-RATE-REQ-001"):
			self.walk_in()
		self.assertFalse(frappe.db.exists("Guest", {"full_name": "K3 Walk-in Guest"}))

	def test_missing_setting_allows_booking_without_plan(self):
		self.assertFalse(frappe.db.get_value("Reservation", self.book()["reservation"], "rate_plan"))

	def test_required_policy_accepts_valid_rate_plan(self):
		self.require_plan()
		plan = frappe.get_doc({"doctype": "Rate Plan", "property": self.property,
			"rate_plan_name": "Contract", "code": "CON", "modifier_type": "Percent"}).insert().name
		out = self.book(rate_plan=plan)
		self.assertEqual(frappe.db.get_value("Reservation", out["reservation"], "rate_plan"), plan)
