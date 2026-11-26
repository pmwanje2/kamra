"""Deposit postings must publish the same balance that the next refund will read."""

from decimal import Decimal
from unittest.mock import patch

import frappe

from kamra import api, cashier, deposit, payment_transfers
from kamra.folio import _recalculate, open_folio, open_window
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, make_property, make_reservation, make_room, make_room_type,
)


class TestAdvancePrecisionR9(AuditTestCase):
	properties = ("UT Advance R9 KWD", "UT Advance R9 USD", "UT Advance R9 UGX")
	cases = (("KWD", 25.375, 5.125, 1), ("USD", 25.37, 5.12, 1), ("UGX", 25375, 5125, 1000))

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.business_date", "kamra.folio", "kamra.ledger")

	def fixture(self, currency, collected, returned, unit):
		prop = make_property(f"UT Advance R9 {currency}", currency=currency, business_date=TODAY)
		rt = make_room_type(prop)
		res = make_reservation(prop, rt, TODAY, d(1), room=make_room(prop, rt, "101"))
		cashier.open_session(prop, 0)
		first = open_folio(res)
		empty = open_window(reservation=res.name, window_no=2, folio_type="Guest")
		third = open_window(reservation=res.name, window_no=3, folio_type="Guest")
		api.add_folio_payment(third, "Cash", collected, kind="Security Deposit")
		api.add_folio_payment(third, "Cash", unit, kind="Advance")
		api._refund_folio_payment(third, returned, "Cash", "Historical return", ledger="Deposit")
		dep = frappe.db.get_value("Security Deposit", {"reservation": res.name})
		frappe.db.set_value("Security Deposit", dep, {
			"refunded_amount": returned, "balance": Decimal(str(collected)) - Decimal(str(returned)),
		})
		api.add_folio_payment(first, "Cash", 2 * unit, kind="Security Deposit")
		advance = api.add_folio_payment(first, "Cash", 10 * unit, kind="Advance")["payments"][-1]["name"]
		windows = (first, empty, third)
		self.synchronize_fixture(windows)
		return res, windows, advance

	def synchronize_fixture(self, windows):
		for name in windows:
			doc = frappe.get_doc("Folio", name)
			_recalculate(doc)
			doc.save()

	def saved(self, windows):
		return [tuple(Decimal(str(v)) for v in frappe.db.get_value("Folio", name, ["payments_total", "balance"]))
		        for name in windows]

	def test_refund_synchronizes_windows_before_events_and_response(self):
		for currency, collected, returned, unit in self.cases:
			with self.subTest(currency=currency):
				_, windows, _ = self.fixture(currency, collected, returned, unit)
				self.assertEqual(self.saved(windows), [(10 * unit, -10 * unit), (0, 0), (unit, -unit)])
				credit = 11 * unit - Decimal(str(returned))
				expected = [(credit, -credit), (0, 0), (0, 0)]
				events = []

				def capture(event, prop, data):
					if event in ("folio.refunded", "deposit.refunded"):
						events.append((event, data["balance"], data["payment"]["amount"], self.saved(windows)))
					return []

				with patch("kamra.webhooks.emit", side_effect=capture):
					result = api.refund_folio_payment(windows[0], returned, "Cash", reason="Advance adjustment")
				self.assertEqual(self.saved(windows), expected)
				self.assertEqual(result["balance"], float(-credit))
				self.assertEqual(events, [(event, float(-credit), returned, expected)
				                          for event in ("folio.refunded", "deposit.refunded")])

	def test_exact_follow_up_refund_clears_saved_credit(self):
		for currency, collected, returned, unit in self.cases:
			with self.subTest(currency=currency):
				_, windows, _ = self.fixture(currency, collected, returned, unit)
				api.refund_folio_payment(windows[0], returned, "Cash", reason="Advance adjustment")
				credit = 11 * unit - Decimal(str(returned))
				result = api.refund_folio_payment(windows[0], float(credit), "Cash", reason="Return remaining credit")
				self.assertEqual(result["balance"], 0)
				self.assertEqual(self.saved(windows), [(0, 0)] * 3)

	def test_advance_receipt_on_third_window_updates_its_sibling(self):
		_, windows, _ = self.fixture(*self.cases[0])
		api.refund_folio_payment(windows[0], 5.125, "Cash", reason="Advance adjustment")
		self.synchronize_fixture(windows)
		result = api.add_folio_payment(windows[2], "Cash", 1, kind="Advance")
		self.assertEqual(result["balance"], 0)
		self.assertEqual(self.saved(windows), [(6.875, -6.875), (0, 0), (0, 0)])

	def test_advance_window_transfer_publishes_both_final_balances(self):
		_, windows, row = self.fixture(*self.cases[0])
		api.refund_folio_payment(windows[0], 5.125, "Cash", reason="Advance adjustment")
		self.synchronize_fixture(windows)
		result = payment_transfers.transfer_folio_payment(
			windows[0], row, windows[2], "Move advance", amount=1,
		)
		self.assertEqual((result["from_balance"], result["to_balance"]), (-5.875, 0))
		self.assertEqual(self.saved(windows), [(5.875, -5.875), (0, 0), (0, 0)])

	def test_security_payout_uses_final_recorded_refund_total_for_events(self):
		res, windows, _ = self.fixture(*self.cases[0])
		api.refund_folio_payment(windows[0], 5.125, "Cash", reason="Advance adjustment")
		self.synchronize_fixture(windows)
		events = []

		def capture(event, prop, data):
			if event in ("folio.refunded", "deposit.refunded"):
				events.append((event, data["balance"], self.saved(windows)))
			return []

		with patch("kamra.webhooks.emit", side_effect=capture):
			deposit.refund_deposit(res.name, 1, "Cash", folio=windows[2])
		expected = [(5.875, -5.875), (0, 0), (0, 0)]
		self.assertEqual(self.saved(windows), expected)
		self.assertEqual(events, [(event, 0, expected) for event in ("folio.refunded", "deposit.refunded")])
