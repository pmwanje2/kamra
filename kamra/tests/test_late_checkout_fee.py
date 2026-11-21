"""A property's late check-out fee rule (TKH-12).

The Late check-out step opened on an empty Amount box: Kamra had no late
fee tariff, so the desk typed whatever it remembered - once the whole
bill (Chris's 423,619). Property.late_checkout_fee_mode ("None" default,
"Fixed", "Percent of Night") and late_checkout_fee_value (default 0) let
checkout_context hand the step the fee the hotel charges, with the night's
room rate so the screen can warn when a fee is more than one night.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from kamra import api
from kamra import folio as folio_mod
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)


class LateCheckoutFeeTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY, checkout_time="11:00:00",
		              late_checkout_fee_mode="None", late_checkout_fee_value=0)
		self.rt = make_room_type(PROP_A, "STD", base_price=80000)
		self.room = make_room(PROP_A, self.rt, "101")
		self.res = make_reservation(PROP_A, self.rt, d(-1), TODAY, status="Checked In",
		                            room=self.room)
		folio_mod.post_room_night(frappe.get_doc("Reservation", self.res.name), d(-1))

	def late(self, **prop):
		if prop:
			frappe.db.set_value("Property", PROP_A, prop)
			frappe.clear_document_cache("Property", PROP_A)
		from datetime import datetime
		with patch("kamra.no_show.property_now",
		           return_value=datetime.fromisoformat(f"{TODAY} 14:30:00")):
			return api.checkout_context(self.res.name)["late_checkout"]

	def test_the_fields_exist_with_safe_defaults(self):
		meta = frappe.get_meta("Property")
		mode = meta.get_field("late_checkout_fee_mode")
		self.assertEqual(mode.options.split("\n"), ["None", "Fixed", "Percent of Night"])
		self.assertEqual(mode.default, "None")
		self.assertEqual(str(meta.get_field("late_checkout_fee_value").default or "0"), "0")

	def test_no_rule_suggests_nothing_but_gives_the_night_rate(self):
		lc = self.late()
		self.assertTrue(lc["late"])
		self.assertEqual(lc["fee_mode"], "None")
		self.assertIsNone(lc["suggested_fee"])
		self.assertAlmostEqual(lc["night_rate"], 80000)

	def test_a_fixed_fee_is_suggested(self):
		lc = self.late(late_checkout_fee_mode="Fixed", late_checkout_fee_value=25000)
		self.assertEqual(lc["fee_mode"], "Fixed")
		self.assertAlmostEqual(lc["fee_value"], 25000)
		self.assertAlmostEqual(lc["suggested_fee"], 25000)

	def test_a_percent_of_night_fee_is_worked_out_from_the_room_rate(self):
		lc = self.late(late_checkout_fee_mode="Percent of Night", late_checkout_fee_value=50)
		self.assertAlmostEqual(lc["suggested_fee"], 40000)
		self.assertAlmostEqual(lc["night_rate"], 80000)

	def test_the_settings_screen_offers_it(self):
		from kamra.settings import SETTINGS
		self.assertTrue("late_checkout_fee_mode" in SETTINGS, "late_checkout_fee_mode")
		self.assertTrue("late_checkout_fee_value" in SETTINGS, "late_checkout_fee_value")
