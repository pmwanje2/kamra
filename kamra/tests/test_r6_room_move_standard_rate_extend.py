"""Rack-rate extensions bill each room at the price and GST for its occupied nights."""

from decimal import Decimal

import frappe

from kamra import api, business_date, folio
from kamra.tests.audit_fixtures import (
	PROP_A, TODAY, AuditTestCase, d, guest_folio, make_property, make_reservation, make_room,
	make_room_type,
)
from kamra.tests.test_reservation_discounts import set_setting


class TestRackRateMoveExtension(AuditTestCase):
	properties = (PROP_A,)

	def check_extension(self, currency, source_rate, destination_rate, first_gross, next_gross, total):
		self.at(TODAY, "kamra.api", "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY, currency=currency, gst_mode="Fixed", rates_include_tax=0)
		set_setting(PROP_A, "rate_override_limit_pct", 20)
		source_type = make_room_type(PROP_A, "R6A", base_price=source_rate, tax_percent=5)
		destination_type = make_room_type(PROP_A, "R6B", base_price=destination_rate, tax_percent=18)
		source = make_room(PROP_A, source_type, "101")
		destination = make_room(PROP_A, destination_type, "201")
		stay = make_reservation(PROP_A, source_type, TODAY, d(2), status="Checked In", room=source, auto_price=1)
		api.set_reservation_discount(stay.name, discount_type="Percent", discount_value=10,
		                             reason_code="RATE-PROMO", confirm_price=1)
		business_date.advance_business_date(PROP_A, TODAY)
		api.move_reservation(stay.name, destination, upgrade_type="Paid", confirm_price=1)
		old = folio.locked_night(stay.reload(), TODAY)
		self.assertEqual((Decimal(str(old.rate)), old.gst_rate), (Decimal(str(source_rate)) * Decimal("0.9"), 5))
		self.assertFalse(folio.charged_nights(stay.name))

		preview = api.amend_stay_preview(stay.name, TODAY, d(3))
		amended = api.amend_stay(stay.name, TODAY, d(3), confirm_price=1)
		stay.reload()
		for day in (TODAY, d(1), d(2)):
			self.assertTrue(folio.post_room_night(stay, day))
		bill = frappe.get_doc("Folio", guest_folio(stay.name))
		for stage, amount in (("preview", preview["new_amount"]), ("save", amended["amount_after_tax"]),
		                      ("folio", bill.grand_total)):
			with self.subTest(stage=stage):
				self.assertEqual(Decimal(str(amount)), Decimal(total))
		charges = {str(row.posting_date): (row.gst_rate, Decimal(str(row.total)))
		           for row in bill.charges if row.charge_type == "Room"}
		self.assertEqual(charges, {
			TODAY: (5, Decimal(first_gross)), d(1): (18, Decimal(next_gross)), d(2): (18, Decimal(next_gross)),
		})

	def test_inr_overcharge(self):
		self.check_extension("INR", 10000, 15000, "9450", "15930", "41310")

	def test_ugx_whole_shillings(self):
		self.check_extension("UGX", 10000, 15000, "9450", "15930", "41310")

	def test_usd_cents(self):
		self.check_extension("USD", 102, 150, "96.39", "159.30", "414.99")

	def test_kwd_fils(self):
		self.check_extension("KWD", 10.20, 15, "9.639", "15.930", "41.499")
