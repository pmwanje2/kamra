"""Discount amendments use each occupied room's tax rules, including after later moves."""

from decimal import Decimal
from unittest.mock import patch

import frappe

from kamra import api, business_date, folio, pricing
from kamra.tests.audit_fixtures import (
	PROP_A, TODAY, AuditTestCase, d, guest_folio, make_property, make_reservation, make_room,
	make_room_type,
)
from kamra.tests.test_reservation_discounts import set_setting


class TestRoomMoveRediscountTax(AuditTestCase):
	properties = (PROP_A,)

	def moved_stay(self, currency="INR", source=10000, destination=15000, *, contract=False):
		self.at(TODAY, "kamra.api", "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY, currency=currency, gst_mode="Fixed", rates_include_tax=0)
		set_setting(PROP_A, "rate_override_limit_pct", 25)
		a = make_room_type(PROP_A, "R7A", base_price=source, tax_percent=5)
		b = make_room_type(PROP_A, "R7B", base_price=destination, tax_percent=18)
		room_a, room_b = make_room(PROP_A, a, "101"), make_room(PROP_A, b, "201")
		if contract:
			def resolve(_property, room_type, _plan, start, *_args, **_kwargs):
				if room_type == b:
					return destination, {"currency": currency, "property_currency": currency,
					                     "nightly": destination, "fx_rate": 1, "fx_date": str(start)}
				return None
			self.enterContext(patch.object(pricing, "contract_rate", side_effect=resolve))
		stay = make_reservation(PROP_A, a, TODAY, d(2), status="Checked In", room=room_a, auto_price=1)
		api.set_reservation_discount(stay.name, discount_type="Percent", discount_value=10,
		                             reason_code="RATE-PROMO")
		business_date.advance_business_date(PROP_A, TODAY)
		api.move_reservation(stay.name, room_b, upgrade_type="Paid", confirm_price=1)
		api.amend_stay(stay.name, TODAY, d(3), confirm_price=1)
		self.assertFalse(folio.charged_nights(stay.name))
		return stay.reload()

	def verify_amendment(self, stay, terms, expected_lines, total):
		preview = api.discount_preview(stay.name, **terms)
		saved = api.set_reservation_discount(stay.name, confirm_price=1, **terms)
		stay.reload()
		for night in range(len(expected_lines)):
			self.assertTrue(folio.post_room_night(stay, d(night)))
		bill = frappe.get_doc("Folio", guest_folio(stay.name))
		for stage, amount in (("preview", preview["new_amount"]), ("save", saved["amount_after_tax"]),
		                      ("folio", bill.grand_total)):
			with self.subTest(stage=stage):
				self.assertEqual(Decimal(str(amount)), Decimal(total))
		charges = {str(r.posting_date): (r.gst_rate, Decimal(str(r.total)))
		           for r in bill.charges if r.charge_type == "Room"}
		for night, (gst, gross) in enumerate(expected_lines):
			with self.subTest(night=night):
				self.assertEqual(folio.locked_night(stay, d(night)).gst_rate, gst)
				self.assertEqual(charges[d(night)], (gst, Decimal(gross)))

	def test_inr_remove_discount(self):
		stay = self.moved_stay()
		self.assertEqual(stay.amount_after_tax, 41310)
		self.verify_amendment(stay, {}, [(5, "10500"), (18, "17700"), (18, "17700")], "45900")

	def test_change_percent_on_contract_destination(self):
		self.verify_amendment(self.moved_stay(contract=True),
		                      dict(discount_type="Percent", discount_value=20, reason_code="RATE-PROMO"),
		                      [(5, "8400"), (18, "14160"), (18, "14160")], "36720")

	def test_amount_discount_after_two_paid_moves_and_extensions(self):
		stay = self.moved_stay()
		c = make_room_type(PROP_A, "R7C", base_price=20000, tax_percent=12)
		room_c = make_room(PROP_A, c, "301")
		business_date.advance_business_date(PROP_A, d(1))
		api.move_reservation(stay.name, room_c, upgrade_type="Paid", confirm_price=1)
		api.amend_stay(stay.name, TODAY, d(4), confirm_price=1)
		self.verify_amendment(stay, dict(discount_type="Amount", discount_value=1000, reason_code="RATE-PROMO"),
		                      [(5, "9450"), (18, "16520"), (12, "21280"), (12, "21280")], "68530")

	def test_ugx_removal_rounds_total_to_whole_shillings(self):
		self.verify_amendment(self.moved_stay("UGX", 110, 150), {},
		                      [(5, "115.50"), (18, "177"), (18, "177")], "470")

	def test_usd_removal_retains_cents(self):
		self.verify_amendment(self.moved_stay("USD", 102, 150), {},
		                      [(5, "107.10"), (18, "177"), (18, "177")], "461.10")

	def test_kwd_removal_retains_fils(self):
		self.verify_amendment(self.moved_stay("KWD", 10.22, 15), {},
		                      [(5, "10.731"), (18, "17.7"), (18, "17.7")], "46.131")
