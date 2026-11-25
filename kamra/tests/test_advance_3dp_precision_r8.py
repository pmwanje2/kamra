"""Historical security returns must account for each Guest window's Deposit ledger."""

from decimal import Decimal

import frappe

from kamra import api, cashier, deposit, ledger
from kamra.folio import _recalculate, open_folio, open_window, security_deposit_held_on_folio
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, make_property, make_reservation, make_room, make_room_type,
)


class TestAdvancePrecisionR8(AuditTestCase):
	properties = ("UT Advance R8 KWD", "UT Advance R8 USD", "UT Advance R8 UGX")
	# Currency, collection on window 3, historic return, and one unit of the fixture.
	cases = (("KWD", "25.375", "5.125", 1), ("USD", "25.37", "5.12", 1), ("UGX", "25375", "5125", 1000))

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.business_date", "kamra.folio", "kamra.ledger")

	def stay(self, currency, collected, returned, unit, small_advance=False):
		prop = make_property(f"UT Advance R8 {currency}", currency=currency, business_date=TODAY)
		rt = make_room_type(prop)
		res = make_reservation(prop, rt, TODAY, d(1), room=make_room(prop, rt, "101"))
		cashier.open_session(prop, 0)
		first = open_folio(res)
		second = open_window(reservation=res.name, window_no=2, folio_type="Guest")
		third = open_window(reservation=res.name, window_no=3, folio_type="Guest")
		collected, returned = Decimal(collected), Decimal(returned)
		api.add_folio_payment(third, "Cash", float(collected), kind="Security Deposit")
		if small_advance:
			api.add_folio_payment(third, "Cash", unit, kind="Advance")
		api._refund_folio_payment(third, float(returned), "Cash", "Historical return", ledger="Deposit")
		record = frappe.db.get_value("Security Deposit", {"reservation": res.name})
		frappe.db.set_value("Security Deposit", record, {
			"refunded_amount": returned, "balance": collected - returned,
		})
		api.add_folio_payment(first, "Cash", 2 * unit, kind="Security Deposit")
		api.add_folio_payment(first, "Cash", 10 * unit, kind="Advance")
		api.refund_folio_payment(first, float(returned), "Cash", reason="Advance adjustment")
		return res, (first, second, third), collected - returned, 10 * unit - returned

	def assert_windows(self, res, windows, held, paid):
		security = []
		payments = []
		for name in windows:
			doc = frappe.get_doc("Folio", name)
			_recalculate(doc)
			security.append(Decimal(str(security_deposit_held_on_folio(doc))))
			payments.append(Decimal(str(doc.payments_total)))
			rows = frappe.get_all("Folio Ledger Entry", filters={"folio": name, "ledger": "Deposit"},
			                      fields=["credit", "debit"])
			balance = sum(Decimal(str(r.credit)) - Decimal(str(r.debit)) for r in rows)
			self.assertLessEqual(security[-1], balance)
		self.assertEqual(security, held)
		self.assertEqual(payments, paid)
		self.assertEqual(sum(security), Decimal(str(deposit.deposit_state(res)["held"])))

	def test_later_window_shortfall_precedes_window_order(self):
		for currency, collected, returned, unit in self.cases:
			with self.subTest(currency=currency):
				res, windows, third_held, ordinary = self.stay(
					currency, collected, returned, unit,
				)
				self.assert_windows(res, windows, [2 * unit, 0, third_held], [ordinary, 0, 0])

	def test_remaining_refund_uses_window_order_after_covering_shortfall(self):
		for currency, collected, returned, unit in self.cases:
			with self.subTest(currency=currency):
				res, windows, third_held, ordinary = self.stay(
					currency, collected, returned, unit, small_advance=True,
				)
				self.assert_windows(res, windows, [unit, 0, third_held + unit], [ordinary + unit, 0, 0])

	def test_full_security_payout_posts_each_leg_to_its_window(self):
		for currency, collected, returned, unit in self.cases:
			with self.subTest(currency=currency):
				res, windows, third_held, ordinary = self.stay(
					currency, collected, returned, unit,
				)
				api.refund_security_deposit(res.name, float(2 * unit + third_held), "Cash")
				for name, expected in zip(windows, ([2 * unit], [], [third_held])):
					legs = frappe.get_all("Folio Ledger Entry", filters={
						"folio": name, "ledger": "Deposit", "source_doctype": ledger.SECURITY_DEPOSIT_REFUND_SOURCE,
					}, pluck="debit")
					self.assertEqual([Decimal(str(amount)) for amount in legs], expected)
				self.assert_windows(res, windows, [0, 0, 0], [ordinary, 0, 0])

	def test_check_in_after_partial_payout_preserves_remaining_security(self):
		for currency, collected, returned, unit in self.cases:
			with self.subTest(currency=currency):
				res, windows, third_held, ordinary = self.stay(
					currency, collected, returned, unit,
				)
				api.refund_security_deposit(res.name, unit, "Cash")
				res.reload()
				res.status = "Checked In"
				res.save(ignore_permissions=True)
				self.assertEqual(ledger.transfer_advances_to_guest(res.name), 0)
				for account, field in (("Deposit", "debit"), ("Guest", "credit")):
					legs = frappe.get_all("Folio Ledger Entry", filters={
						"folio": windows[0], "ledger": account, "description": ledger.ADVANCE_APPLIED,
					}, pluck=field)
					self.assertEqual([Decimal(str(amount)) for amount in legs], [ordinary])
				self.assert_windows(res, windows, [unit, 0, third_held], [ordinary, 0, 0])
