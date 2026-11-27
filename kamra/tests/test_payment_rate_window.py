from unittest.mock import patch

import frappe

from kamra import api, ledger
from kamra.tests.audit_fixtures import (
	stocked_room_type,
	PROP_A,
	TODAY,
	YESTERDAY,
	AuditTestCase,
	d,
	make_property,
	make_reservation,
	make_room_type,
)


class SettlementRateTests(AuditTestCase):
	def setUp(self):
		super().setUp()
		make_property(PROP_A, business_date=TODAY, currency="UGX", require_cashier_pin=0)
		frappe.db.delete("Exchange Rate", {"property": PROP_A})

	def rate(self, date, buy, disabled=0, currency="EUR", **fields):
		frappe.get_doc(
			dict(
				doctype="Exchange Rate",
				property=PROP_A,
				currency=currency,
				rate_date=date,
				buy_rate=buy,
				sell_rate=fields.pop("sell_rate", buy),
				**fields,
				disabled=disabled,
			)
		).insert()

	def test_latest_unique_enabled_within_window(self):
		self.rate(YESTERDAY, 4100)
		self.rate(TODAY, 4200, disabled=1)
		self.rate(d(1), 4300)
		with patch.object(ledger, "_settlement_window", return_value=2, create=True):
			r = ledger.payment_rate(PROP_A, "eur")
			self.assertEqual((r["rate_date"], r["rate"], r["window_days"]), (YESTERDAY, 4100, 2))
			self.rate(TODAY, 4201)
			self.assertEqual(ledger.payment_rate(PROP_A, "EUR")["rate"], 4201)
			self.rate(TODAY, 4202)
			with self.assertRaisesRegex(frappe.ValidationError, "KMR-FX-002.*HSP-CASHIER-FX-RATE"):
				ledger.payment_rate(PROP_A, "EUR")

	def test_no_settings_defaults_one_day_and_refuses_missing_or_stale(self):
		for date in (None, YESTERDAY):
			if date:
				self.rate(date, 4100)
			with self.assertRaisesRegex(frappe.ValidationError, "KMR-FX-001.*HSP-CASHIER-FX-RATE"):
				ledger.payment_rate(PROP_A, "EUR")

	def test_advance_and_receipt_remember_effective_date(self):
		from kamra import cashier

		self.rate(YESTERDAY, 4100)
		res = make_reservation(PROP_A, stocked_room_type(PROP_A), TODAY, d(2))
		cashier.open_session(PROP_A, 0)
		with patch.object(ledger, "_settlement_window", return_value=2, create=True):
			out = api.record_advance(res.name, 2, currency="EUR")
		self.assertEqual(frappe.db.get_value("Reservation", res.name, "advance_paid"), 8200)
		self.rate(TODAY, 4200)
		p = api.folio_invoice(out["folio"])["payments"][0]
		self.assertEqual(
			(p["exchange_rate"], str(p["exchange_rate_date"]), p["amount"]), (4100, YESTERDAY, 8200)
		)

	def test_receipt_identifies_property_currency_independent_of_country_default(self):
		from kamra.folio import open_folio

		res = make_reservation(PROP_A, stocked_room_type(PROP_A), TODAY, d(2))
		invoice = api.folio_invoice(open_folio(res))
		self.assertEqual(invoice["property"].get("currency"), "UGX")

	def test_setting_value_and_quote_reconfirmation(self):
		from kamra import cashier
		from kamra.folio import open_folio

		with (
			patch.object(frappe.db, "exists", return_value=True),
			patch("frappe.get_meta") as meta,
			patch.object(frappe.db, "get_single_value", return_value="3") as value,
		):
			meta.return_value.has_field.return_value = True
			self.assertEqual(ledger._settlement_window(), 3)
			value.assert_called_once_with("Accounts Settings", "stale_days")
		self.rate(TODAY, 4100)
		q = ledger.payment_rate(PROP_A, "EUR", 2)
		res = make_reservation(PROP_A, stocked_room_type(PROP_A), TODAY, d(2))
		folio = open_folio(res)
		cashier.open_session(PROP_A, 0)
		ledger.upsert_exchange_rate(PROP_A, "EUR", 4200, 4201)
		with self.assertRaisesRegex(frappe.ValidationError, "published rate changed"):
			api.add_folio_payment(folio, "Cash", 2, currency="EUR", quote_id=q["quote_id"])
		self.assertFalse(frappe.get_doc("Folio", folio).payments)

	def test_walk_in_accepts_explicit_foreign_cash(self):
		from frappe.utils import add_days, nowdate

		from kamra import cashier
		from kamra.tests.audit_fixtures import make_room

		rt = make_room_type(PROP_A)
		room = make_room(PROP_A, rt, "103")
		self.rate(TODAY, 4100)
		cashier.open_session(PROP_A, 0)
		r = api.walk_in(
			PROP_A,
			rt,
			room,
			add_days(nowdate(), 1),
			"FX Walk In",
			defer_check_in=1,
			payment_mode="Foreign Cash",
			payment_amount=2,
			currency="EUR",
		)
		self.assertEqual(r["paid"], 8200)

	def test_online_settlement_and_bureau_transactions(self):
		from kamra import cashier

		self.rate(TODAY, 3920, currency="USD", sell_rate=4080, source="Online", market_rate=4000)
		self.assertEqual(ledger.payment_rate(PROP_A, "USD")["rate"], 4000)
		for rows in ("rates", "tender_rates"):
			row = ledger.list_exchange_rates(PROP_A)[rows][0]
			self.assertEqual((row.settlement_rate, row.buy_rate, row.sell_rate), (4000, 3920, 4080))
		cashier.open_session(PROP_A, 20000)
		for direction, rate in (("Buy", 3920), ("Sell", 4080)):
			self.assertEqual(ledger.post_exchange(PROP_A, "USD", 1, direction)["exchange_rate"], rate)

	def test_manual_midpoint_or_flat_rate(self):
		for sell, expected in ((4100, 4100), (4200, 4150)):
			ledger.upsert_exchange_rate(PROP_A, "EUR", 4100, sell)
			self.assertEqual(ledger.payment_rate(PROP_A, "EUR")["rate"], expected)

	def test_walk_in_contract_amount_settles_in_whole_shillings(self):
		from frappe.utils import add_days, nowdate
		from kamra import cashier
		from kamra.tests.audit_fixtures import make_room

		self.rate(TODAY, 3951.612903226, currency="USD", sell_rate=4112.903225806,
		          market_rate=4032.258064516, source="Online")
		make_property(PROP_A, business_date=TODAY, country="Uganda", currency="UGX", rates_include_tax=1)
		rt = make_room_type(PROP_A, "FX-CONTRACT", base_price=410619, tax_percent=0)
		plan = frappe.get_doc({"doctype": "Rate Plan", "property": PROP_A, "rate_plan_name": "USD contract",
		                       "code": "UT-FX", "modifier_type": "Percent", "modifier_value": 0}).insert()
		tariff = dict(currency="USD", nightly=130, fx_rate=4032.258064516, property_currency="UGX", fx_date=TODAY)
		room = make_room(PROP_A, rt, "104")
		cashier.open_session(PROP_A, 0)
		with patch("kamra.pricing.contract_rate", return_value=(524194, tariff)):
			result = api.walk_in(PROP_A, rt, room, add_days(nowdate(), 1), "USD Guest", rate_plan=plan.name,
			                     defer_check_in=1, payment_mode="Foreign Cash", currency="USD", payment_amount=130)

		self.assertEqual(result["paid"], 524194)
		self.assertEqual(result["amount_after_tax"] - result["paid"], 0)
