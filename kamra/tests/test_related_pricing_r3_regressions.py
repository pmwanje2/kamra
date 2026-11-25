"""Live contract pricing, booking lock and quotation regressions."""

from unittest.mock import patch

import frappe

from kamra import api, erp_bridge
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, make_property, make_reservation, make_room, make_room_type,
)

PROPERTY = "UT Related Pricing R3"


class TestRelatedPricingR3Regressions(AuditTestCase):
	properties = (PROPERTY,)

	def setUp(self):
		super().setUp()
		self.at(TODAY)
		frappe.local.lang = "en"
		make_property(PROPERTY, country="United States", currency="USD", business_date=TODAY,
		              gst_mode="Fixed", gst_rate_low=10, gst_rate_high=10, rates_include_tax=1)
		self.room_type = make_room_type(PROPERTY, base_price=4.41)
		self.room = make_room(PROPERTY, self.room_type, "101")
		self.plan = frappe.get_doc({
			"doctype": "Rate Plan", "property": PROPERTY, "rate_plan_name": "Fixture Contract", "code": "FX",
			"modifier_type": "Percent", "modifier_value": 0,
		}).insert().name

	def book(self, nights):
		bridge_call = erp_bridge.bridge_call

		def tariff_fixture(method, **params):
			if method == "hospitality_tariffs.tariff_rate":
				return {"tariff": {
					"is_tariff": 1, "currency": "KWD", "nightly": 1.358, "fx_rate": 3.25,
					"agreed_rate": 4.41, "fx_date": TODAY,
				}}
			return bridge_call(method, **params)

		with patch.object(erp_bridge, "bridge_call", side_effect=tariff_fixture):
			res = make_reservation(PROPERTY, self.room_type, d(0), d(nights), room=self.room, rate_plan=self.plan)
		return frappe.get_doc("Reservation", res.name)

	def test_inclusive_quotation_preserves_canonical_rate_after_booking_lock(self):
		res = self.book(1)
		snapshot = frappe.parse_json(res.quote_snapshot)
		self.assertTrue(snapshot["contract_summary"]["room_tax_included"])
		self.assertAlmostEqual(snapshot["nightly"][0]["rate"], 4.009090909, places=9)
		self.assertEqual(res.night_rates[0].rate, 4.01)

		quote = api.booking_quotation(res.name)
		ref = quote["currency_reference"]["nights"][0]
		html = api._booking_quotation_html(quote)
		self.assertIn("KWD 1.235", html, "Expected the canonical pre-tax KWD rate in the nightly table. " + html)
		self.assertEqual((ref["contract_rate"], ref["estimated"]), (1.235, False))

	def test_final_night_remainder_keeps_canonical_rate_until_a_locked_rate_changes(self):
		res = self.book(6)
		self.assertEqual([row.rate for row in res.night_rates], [4.01] * 5 + [4.00])
		quote = api.booking_quotation(res.name)
		rates = quote["currency_reference"]["nights"]
		self.assertEqual([(row["contract_rate"], row["estimated"]) for row in rates], [(1.235, False)] * 6)

		res.night_rates[-1].db_set("rate", 4.02)
		edited = api.booking_quotation(res.name)["currency_reference"]["nights"]
		self.assertEqual([(row["contract_rate"], row["estimated"]) for row in edited[:-1]], [(1.235, False)] * 5)
		self.assertTrue(edited[-1]["estimated"])
