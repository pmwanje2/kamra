"""/book quotes each meal plan and shows a real voucher discount."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from kamra.public_api import search_stay, showcase


class PublicBookingTestCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		frappe.local.lang = "en"
		self.property = frappe.get_doc({"doctype": "Property",
			"property_name": "TEST BOOK " + frappe.generate_hash(length=8),
			"booking_engine_enabled": 1}).insert(ignore_permissions=True).name
		self.room_type = frappe.get_doc({"doctype": "Room Type", "property": self.property,
			"room_type_code": "STD", "room_type_name": "Standard", "base_price": 1000,
			"adults_capacity": 2}).insert(ignore_permissions=True).name
		frappe.get_doc({"doctype": "Room", "property": self.property,
			"room_type": self.room_type, "room_number": "101"}).insert(ignore_permissions=True)
		self.meal = frappe.get_doc({"doctype": "Meal Plan", "property": self.property,
			"code": "CP", "label": "Breakfast", "price_per_adult": 300}).insert(
			ignore_permissions=True).name
		frappe.get_doc({"doctype": "Discount Voucher", "property": self.property,
			"voucher_code": "TENOFF", "discount_type": "Percent", "value": 10,
			"min_nights": 1}).insert(ignore_permissions=True)
		self.ci, self.co = add_days(nowdate(), 5), add_days(nowdate(), 7)

	def row(self, **kw):
		return search_stay(self.property, self.ci, self.co, 2, 0, **kw)[0]

	def test_each_meal_plan_is_quoted(self):
		row = self.row()
		plans = {p["meal_plan"]: p for p in row["plans"]}
		self.assertEqual(set(plans), {"", self.meal})
		self.assertEqual(plans[""]["amount_after_tax"], row["quote"]["amount_after_tax"])
		self.assertGreater(plans[self.meal]["amount_after_tax"], plans[""]["amount_after_tax"])
		self.assertNotIn("list_amount", plans[""])
		self.assertIn("advance_due", plans[""])

	def test_free_meal_plan_is_the_room_only_rate(self):
		ep = frappe.get_doc({"doctype": "Meal Plan", "property": self.property,
			"code": "EP", "label": "Room only"}).insert(ignore_permissions=True).name
		self.assertEqual([p["meal_plan"] for p in self.row()["plans"]], [ep, self.meal])

	def test_voucher_discounts_and_keeps_list_price(self):
		plain = self.row()["plans"][0]["amount_after_tax"]
		plan = self.row(voucher_code="tenoff")["plans"][0]
		self.assertAlmostEqual(plan["list_amount"], plain, places=2)
		self.assertLess(plan["amount_after_tax"], plain)

	def test_unknown_voucher_is_ignored(self):
		plan = self.row(voucher_code="NOPE")["plans"][0]
		self.assertNotIn("list_amount", plan)

	def test_showcase_says_whether_tiers_apply(self):
		self.assertIs(showcase(self.property)["property"]["has_cancellation_tiers"], False)

	def test_link_previews_get_the_hotels_meta_and_staff_pages_stay_private(self):
		from kamra.www.kamra import _with_meta

		frappe.db.set_value("Property", self.property, {"meta_title": "Sea View Inn",
			"meta_description": "Rooms <b>by</b> the sea.", "og_image": "/files/sea.jpg",
			"booking_language": "es"})
		shell = '<html lang="en"><head><meta\n  name="description"\n  content="Kamra" />\n<title>Kamra PMS</title></head></html>'
		from kamra.booking_slugs import slugify
		frappe.form_dict.property = slugify(frappe.db.get_value("Property", self.property, "property_name"))
		try:
			page = _with_meta(shell, "/kamra/book")
		finally:
			frappe.form_dict.pop("property", None)
		self.assertIn("<title>Sea View Inn</title>", page)
		self.assertIn('property="og:image" content="http', page)
		self.assertIn("&lt;b&gt;by&lt;/b&gt;", page)  # escaped, never raw HTML
		self.assertNotIn('content="Kamra"', page)
		self.assertNotIn("noindex", page)
		self.assertIn('<html lang="es">', page)
		from kamra.public_api import _public_locale
		self.assertEqual(_public_locale(self.property)["default_language"], "es")
		self.assertIn("noindex", _with_meta(shell, "/kamra/folio/F-1"))
		self.assertIn("noindex", _with_meta(shell, "/kamra/checkin/abc"))

	def test_co_branding_uses_the_hotels_own_name_and_never_frappes_default(self):
		from kamra.public_api import site_info

		def brand(name):
			frappe.db.set_single_value("Website Settings", "app_name", name)
			frappe.clear_document_cache("Website Settings", "Website Settings")
			if hasattr(frappe.local, "website_settings"):
				del frappe.local.website_settings
			return site_info()["brand_name"]

		self.assertIsNone(brand("Frappe"))
		self.assertIsNone(brand(""))
		self.assertEqual(brand("Example Hotels"), "Example Hotels")
		self.assertEqual(site_info()["app"], "kamra")

	def test_room_type_without_rooms_is_never_offered_in_public_showcase_or_search(self):
		empty = frappe.get_doc({"doctype": "Room Type", "property": self.property,
			"room_type_code": "EMP", "room_type_name": "Unbuilt Room", "base_price": 100,
			"adults_capacity": 2}).insert(ignore_permissions=True).name
		shown = [rt["name"] for rt in showcase(self.property)["room_types"]]
		self.assertIn(self.room_type, shown)
		self.assertNotIn(empty, shown)
		searched = [r["room_type"] for r in search_stay(self.property, self.ci, self.co, 2, 0)]
		self.assertIn(self.room_type, searched)
		self.assertNotIn(empty, searched)

	def test_search_and_book_use_property_default_rate_plan_and_expose_label(self):
		from unittest.mock import patch
		from kamra.pricing import ContractRateRefused, quote_with_contract
		from kamra.public_api import book

		rp = frappe.get_doc({
			"doctype": "Rate Plan", "property": self.property,
			"rate_plan_name": "Direct Standard", "code": "DIR-STD",
			"modifier_type": "Percent", "modifier_value": -20,
			"billing_basis": "Nightly", "is_default": 1, "disabled": 0,
		}).insert(ignore_permissions=True)

		expected = quote_with_contract(
			self.property, self.room_type, self.ci, self.co, 2, 0, rate_plan=rp.name,
		)["amount_after_tax"]
		row = self.row()
		self.assertEqual(row["rate_plan"], rp.name)
		self.assertEqual(row["rate_plan_label"], "Direct Standard")
		self.assertAlmostEqual(row["quote"]["amount_after_tax"], expected, places=2)
		self.assertAlmostEqual(row["plans"][0]["amount_after_tax"], expected, places=2)

		booked = book(
			property=self.property, room_type=self.room_type,
			check_in_date=self.ci, check_out_date=self.co,
			guest_name="Public Sync Guest", phone="+256700000999", adults=2,
		)
		res = frappe.get_doc("Reservation", booked["reservation"])
		self.assertEqual(res.rate_plan, rp.name)
		self.assertAlmostEqual(res.amount_after_tax, expected, places=2)

		# An untariffed room refused by the default plan must be marked unavailable, never fall back to Card BAR.
		with patch("kamra.pricing.quote_with_contract", side_effect=ContractRateRefused("HSP-TARIFF-001")):
			refused = self.row()
		self.assertIs(refused["available"], False)
		self.assertIsNone(refused["quote"])
