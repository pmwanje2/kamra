"""Regression coverage for related prices in transaction and property currencies."""

import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


class RelatedPricingSchemaTests(unittest.TestCase):
	def test_voucher_and_fixed_charge_have_optional_currency_links(self):
		for name, follows in (("discount_voucher", "value"), ("reservation_fixed_charge", "qty")):
			with self.subTest(doctype=name):
				path = ROOT / "kamra" / "kamra" / "doctype" / name / f"{name}.json"
				schema = json.loads(path.read_text())
				currency = next((f for f in schema["fields"] if f["fieldname"] == "currency"), None)
				self.assertIsNotNone(currency)
				self.assertEqual((currency["fieldtype"], currency["options"]), ("Link", "Currency"))
				self.assertFalse(currency.get("reqd"))
				self.assertFalse(currency.get("default"))
				self.assertEqual(schema["field_order"][schema["field_order"].index(follows) + 1], "currency")
				if name == "discount_voucher":
					self.assertEqual(currency["depends_on"], "eval:doc.discount_type=='Amount'")


try:
	import frappe
except ModuleNotFoundError as exc:
	if exc.name != "frappe":
		raise
	frappe = None

if frappe is not None:
	from decimal import Decimal
	from unittest.mock import patch

	from kamra import api, fixed_charges, folio as folio_model, pricing
	from kamra.tests.audit_fixtures import (
		TODAY, AuditTestCase, d, guest_folio, make_property, make_reservation, make_room, make_room_type,
	)
	from kamra.tests.test_p29_fixed_charges import _ensure_txn_code

	PROPERTY = "UT Related Pricing"
	FX = Decimal("4085.83")
	TARIFF = {
		"tariff": {"currency": "USD", "property_currency": "UGX", "nightly": 100,
		           "fx_rate": float(FX), "fx_date": TODAY},
		"fx_lock_policy": "Fixed at Reservation",
	}


	class RelatedPricingTests(AuditTestCase):
		properties = (PROPERTY,)

		def setUp(self):
			super().setUp()
			self.at(TODAY)
			make_property(PROPERTY, currency="UGX", business_date=TODAY, gst_mode="Fixed",
			              gst_rate_low=0, gst_rate_high=0, rates_include_tax=0)
			self.room_type = make_room_type(PROPERTY, "STD", base_price=408583)
			self.room = make_room(PROPERTY, self.room_type, "101")

		def test_amount_vouchers_follow_the_stay_rate_and_preserve_legacy_home_amounts(self):
			for currency, expected in (("USD", 204292), (None, 50)):
				with self.subTest(currency=currency):
					voucher = frappe._dict(name="V1", discount_type="Amount", value=50, currency=currency)
					with patch.object(pricing, "validate_voucher", return_value=voucher):
						q = pricing.quote(PROPERTY, self.room_type, d(0), d(1),
						                  voucher_code="V1", contract_tariff=TARIFF)
					self.assertEqual(q["voucher_discount"], expected)
					if currency:
						self.assertEqual((q["voucher_currency"], q["voucher_tx_amount"],
						                  q["voucher_fx_rate"]), ("USD", 50, float(FX)))

		def test_foreign_voucher_rounds_before_conversion_with_a_reservation_discount(self):
			voucher = frappe._dict(name="V1", discount_type="Amount", value=35.004, currency="USD")
			with patch.object(pricing, "validate_voucher", return_value=voucher):
				quote = pricing.quote(PROPERTY, self.room_type, d(0), d(1),
				                      contract_tariff=TARIFF, voucher_code="V1",
				                      discount_type="Amount", discount_value=5, discount_currency="USD")
			self.assertEqual(quote["voucher_tx_amount"], 35)
			self.assertEqual(quote["voucher_discount"], 143004)
			self.assertGreater(quote["reservation_discount_total"], 0)
			self.assertEqual(quote["discount"], 143004 + quote["reservation_discount_total"])

		def test_missing_voucher_exchange_rate_is_a_validation_error(self):
			voucher = frappe._dict(name="V1", discount_type="Amount", value=50, currency="KWD")
			with patch.object(pricing, "validate_voucher", return_value=voucher), \
					patch.object(pricing, "posting_fx_rate", return_value=None):
				with self.assertRaisesRegex(frappe.ValidationError, "KWD"):
					pricing.quote(PROPERTY, self.room_type, d(0), d(1),
					              voucher_code="V1", contract_tariff=TARIFF)

		def test_cancellation_credit_note_uses_property_currency(self):
			res = make_reservation(PROPERTY, self.room_type, d(0), d(2), room=self.room)
			res.db_set("advance_paid", 50000)
			out = api.cancel_reservation(res.name, waive_fee=1, issue_credit_note=1)
			self.assertEqual(frappe.db.get_value("Discount Voucher", {"voucher_code": out["credit_note_voucher"]},
			                                     "currency"), "UGX")

		def test_fixed_charge_rounding_and_idempotency(self):
			res = make_reservation(PROPERTY, self.room_type, d(0), d(2), room=self.room, status="Checked In")
			res.contract_tariff = TARIFF
			code = _ensure_txn_code("3399", "Airport Shuttle", "Misc")
			with patch.object(pricing, "posting_fx_rate", return_value=(Decimal("12000"), TODAY)):
				for currency, price, rounded in (("USD", 35.004, 35), ("KWD", 1.2345, 1.235)):
					row = fixed_charges.save_fixed_charge(res.name, {
						"transaction_code": code, "currency": currency, "unit_price": price,
					})["row"]
					self.assertEqual(row["unit_price"], rounded)
				first = fixed_charges.post_fixed_charges_for_date(res, TODAY)
				second = fixed_charges.post_fixed_charges_for_date(res, TODAY)
			self.assertEqual((first["count"], second["count"]), (2, 0))
			rows = frappe.get_all("Folio Charge", {"idempotency_key": ("in", first["keys"])},
			                      ["contract_currency", "contract_fx_rate", "amount", "description"])
			rows = {row.contract_currency: row for row in rows}
			self.assertEqual((rows["USD"].amount, rows["USD"].contract_fx_rate), (143004, float(FX)))
			self.assertEqual((rows["KWD"].amount, rows["KWD"].contract_fx_rate), (14820, 12000))
			self.assertIn("(USD 35.00 @ 4,085.83)", rows["USD"].description)
			self.assertIn("(KWD 1.235 @ 12,000)", rows["KWD"].description)

		def test_quotation_shows_both_currencies(self):
			res = make_reservation(PROPERTY, self.room_type, d(0), d(1), room=self.room)
			res.contract_tariff = TARIFF
			q = api._booking_quotation(res)
			ref = q["currency_reference"]
			self.assertEqual((ref["contract_currency"], ref["fx_rate"]), ("USD", float(FX)))
			self.assertEqual(ref["nights"][0]["contract_rate"], 100)
			self.assertEqual(q["totals"]["total"], res.amount_after_tax)
			html = api._booking_quotation_html(q)
			self.assertIn("USD", html)
			self.assertIn("UGX", html)

		def test_folio_discount_converts_before_approval_and_keeps_provenance(self):
			res = make_reservation(PROPERTY, self.room_type, d(-1), d(1), room=self.room, status="Checked In")
			folio_model.post_room_night(res, d(-1))
			frappe.db.set_value("Reservation", res.name, "contract_tariff", json.dumps(TARIFF))
			folio = guest_folio(res.name)
			with patch.object(pricing, "posting_fx_rate", return_value=(FX, TODAY)):
				with patch.object(api, "_allowance_needs_approval", return_value=False) as approval:
					api.post_discount(folio, 20, "Amount", reason="Service", discount_currency="USD")
					approval.assert_called_once_with(PROPERTY, 81717, None)
			line = frappe.get_doc("Folio", folio).charges[-1]
			self.assertEqual((line.amount, line.contract_currency, line.contract_fx_rate),
			                 (-81717, "USD", float(FX)))
			self.assertIn("USD 20.00 @ 4,085.83", line.description)
