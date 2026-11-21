from unittest.mock import patch

import frappe

from kamra import api, ledger
from kamra.tests.audit_fixtures import PROP_A, TODAY, YESTERDAY, AuditTestCase, d, make_property, make_reservation, make_room_type


class SettlementRateTests(AuditTestCase):
	def setUp(self):
		super().setUp()
		make_property(PROP_A, business_date=TODAY, currency='UGX', require_cashier_pin=0)
		frappe.db.delete('Exchange Rate', {'property': PROP_A})

	def rate(self, date, buy, disabled=0):
		frappe.get_doc(dict(doctype='Exchange Rate', property=PROP_A, currency='EUR', rate_date=date,
		                    buy_rate=buy, sell_rate=buy, disabled=disabled)).insert()

	def test_latest_unique_enabled_within_window(self):
		self.rate(YESTERDAY, 4100)
		self.rate(TODAY, 4200, disabled=1)
		self.rate(d(1), 4300)
		with patch.object(ledger, '_settlement_window', return_value=2, create=True):
			r = ledger.payment_rate(PROP_A, 'eur')
			self.assertEqual((r['rate_date'], r['rate'], r['window_days']), (YESTERDAY, 4100, 2))
			self.rate(TODAY, 4201)
			self.assertEqual(ledger.payment_rate(PROP_A, 'EUR')['rate'], 4201)
			self.rate(TODAY, 4202)
			with self.assertRaisesRegex(frappe.ValidationError, 'KMR-FX-002.*HSP-CASHIER-FX-RATE'):
				ledger.payment_rate(PROP_A, 'EUR')

	def test_no_settings_defaults_one_day_and_refuses_missing_or_stale(self):
		for date in (None, YESTERDAY):
			if date:
				self.rate(date, 4100)
			with self.assertRaisesRegex(frappe.ValidationError, 'KMR-FX-001.*HSP-CASHIER-FX-RATE'):
				ledger.payment_rate(PROP_A, 'EUR')

	def test_advance_and_receipt_remember_effective_date(self):
		from kamra import cashier
		self.rate(YESTERDAY, 4100)
		res = make_reservation(PROP_A, make_room_type(PROP_A), TODAY, d(2))
		cashier.open_session(PROP_A, 0)
		with patch.object(ledger, '_settlement_window', return_value=2, create=True):
			out = api.record_advance(res.name, 2, currency='EUR')
		self.assertEqual(frappe.db.get_value('Reservation', res.name, 'advance_paid'), 8200)
		self.rate(TODAY, 4200)
		p = api.folio_invoice(out['folio'])['payments'][0]
		self.assertEqual((p['exchange_rate'], str(p['exchange_rate_date']), p['amount']), (4100, YESTERDAY, 8200))

	def test_receipt_identifies_property_currency_independent_of_country_default(self):
		from kamra.folio import open_folio
		res = make_reservation(PROP_A, make_room_type(PROP_A), TODAY, d(2))
		invoice = api.folio_invoice(open_folio(res))
		self.assertEqual(invoice['property'].get('currency'), 'UGX')
