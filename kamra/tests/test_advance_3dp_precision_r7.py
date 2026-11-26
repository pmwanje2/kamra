"""A stay's recorded historical refund is allocated only once across Guest windows."""

from decimal import Decimal

import frappe

from kamra import api, cashier, deposit, ledger
from kamra.folio import open_folio, open_window, security_deposit_held_on_folio
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, make_property, make_reservation, make_room, make_room_type,
)


class TestAdvancePrecisionR7(AuditTestCase):
	properties = ("UT Advance R7 KWD", "UT Advance R7 USD", "UT Advance R7 UGX")
	cases = (
		("KWD", "25.375", "5.125", "2", "10"),
		("USD", "25.37", "5.12", "2", "10"),
		("UGX", "25375", "5125", "2000", "10000"),
	)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.business_date", "kamra.folio", "kamra.ledger")

	def mixed_windows(self, currency, collected, returned, security, advance):
		prop = make_property(f"UT Advance R7 {currency}", currency=currency, business_date=TODAY)
		rt = make_room_type(prop)
		res = make_reservation(prop, rt, TODAY, d(1), room=make_room(prop, rt, "101"))
		cashier.open_session(prop, 0)
		first = open_folio(res)
		second = open_window(reservation=res.name, window_no=2, folio_type="Guest")
		api.add_folio_payment(first, "Cash", float(collected), kind="Security Deposit")
		api._refund_folio_payment(first, float(returned), "Cash", "Historical return", ledger="Deposit")
		record = frappe.db.get_value("Security Deposit", {"reservation": res.name})
		frappe.db.set_value("Security Deposit", record, {
			"refunded_amount": returned, "balance": collected - returned,
		})
		api.add_folio_payment(second, "Cash", float(security), kind="Security Deposit")
		api.add_folio_payment(second, "Cash", float(advance), kind="Advance")
		api.refund_folio_payment(second, float(returned), "Cash", reason="Advance adjustment")
		return res, first, second

	def assert_held(self, res, windows, expected):
		held = [Decimal(str(security_deposit_held_on_folio(frappe.get_doc("Folio", w)))) for w in windows]
		self.assertEqual(held, expected)
		self.assertEqual(sum(held), Decimal(str(deposit.deposit_state(res)["held"])))

	def test_historical_return_belongs_to_first_window(self):
		for currency, *amounts in self.cases:
			with self.subTest(currency=currency):
				collected, returned, security, advance = map(Decimal, amounts)
				res, first, second = self.mixed_windows(currency, collected, returned, security, advance)
				self.assert_held(res, [first, second], [collected - returned, security])

	def check_payout(self, partial):
		for currency, *amounts in self.cases:
			with self.subTest(currency=currency):
				collected, returned, security, advance = map(Decimal, amounts)
				res, first, second = self.mixed_windows(currency, collected, returned, security, advance)
				left = security / 2 if partial else Decimal(0)
				first_part = collected - returned
				second_part = security - left
				api.refund_security_deposit(res.name, float(first_part + second_part), "Cash")
				for window, expected in ((first, first_part), (second, second_part)):
					debits = frappe.get_all("Folio Ledger Entry", filters={
						"folio": window, "ledger": "Deposit", "source_doctype": ledger.SECURITY_DEPOSIT_REFUND_SOURCE,
					}, pluck="debit")
					self.assertEqual([Decimal(str(v)) for v in debits], [expected])
				self.assert_held(res, [first, second], [Decimal(0), left])
				res.reload()
				res.status = "Checked In"
				res.save(ignore_permissions=True)
				self.assertEqual(ledger.transfer_advances_to_guest(res.name), 0)
				for account, field in (("Deposit", "debit"), ("Guest", "credit")):
					posted = frappe.get_all("Folio Ledger Entry", filters={
						"folio": second, "ledger": account, "description": ledger.ADVANCE_APPLIED,
					}, pluck=field)
					self.assertEqual([Decimal(str(v)) for v in posted], [advance - returned])
				self.assert_held(res, [first, second], [Decimal(0), left])

	def test_full_payout_and_check_in(self):
		self.check_payout(partial=False)

	def test_partial_payout_and_check_in(self):
		self.check_payout(partial=True)
