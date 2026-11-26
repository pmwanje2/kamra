"""A paid room move keeps each night's tax treatment when the stay extends."""

from unittest.mock import patch

import frappe

from kamra import api, business_date, folio, pricing
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	guest_folio,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)
from kamra.tests.test_reservation_discounts import set_setting


class TestRoomMoveExtendGst(AuditTestCase):
	properties = (PROP_A,)

	def test_extension_keeps_pre_move_gst_when_the_unposted_night_bills(self):
		self.at(TODAY, "kamra.api", "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY, currency="INR", gst_mode="Fixed", rates_include_tax=0)
		set_setting(PROP_A, "rate_override_limit_pct", 20)
		standard = make_room_type(PROP_A, "STD", base_price=10000, tax_percent=5)
		suite = make_room_type(PROP_A, "STE", base_price=15000, tax_percent=18)
		source = make_room(PROP_A, standard, "101")
		destination = make_room(PROP_A, suite, "201")

		def resolve(_property, room_type, _rate_plan, start, *_args, **_kwargs):
			if room_type == suite:
				return 15000, {
					"currency": "INR", "property_currency": "INR", "nightly": 15000,
					"fx_rate": 1, "fx_date": str(start),
				}
			return None

		with patch.object(pricing, "contract_rate", side_effect=resolve):
			stay = make_reservation(
				PROP_A, standard, TODAY, d(2), status="Checked In", room=source, auto_price=1,
			)
			api.set_reservation_discount(
				stay.name, discount_type="Percent", discount_value=10, reason_code="RATE-PROMO",
				confirm_price=1,
			)
			business_date.advance_business_date(PROP_A, TODAY)
			api.move_reservation(stay.name, destination, upgrade_type="Paid", confirm_price=1)
			original = folio.locked_night(stay.reload(), TODAY)
			self.assertEqual((original.rate, original.gst_rate), (9000, 5))
			self.assertFalse(folio.charged_nights(stay.name))
			preview = api.amend_stay_preview(stay.name, TODAY, d(3))
			amended = api.amend_stay(stay.name, TODAY, d(3), confirm_price=1)

		stay.reload()
		for day in (TODAY, d(1), d(2)):
			self.assertTrue(folio.post_room_night(stay, day))
		charges = {str(row.posting_date): row
		           for row in frappe.get_doc("Folio", guest_folio(stay.name)).charges
		           if row.charge_type == "Room"}
		self.assertEqual(charges[TODAY].total, 9450)
		self.assertEqual((charges[TODAY].amount, charges[TODAY].gst_rate), (9000, 5))
		for day in (d(1), d(2)):
			self.assertEqual((charges[day].amount, charges[day].gst_rate, charges[day].total),
			                 (13500, 18, 15930))
		self.assertEqual(preview["new_amount"], 41310)
		self.assertEqual(amended["amount_after_tax"], 41310)
