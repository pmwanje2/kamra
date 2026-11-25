"""Historical and new security returns are bounded by the funds on each window."""

from decimal import Decimal
from unittest.mock import patch

import frappe

from kamra import api, cashier, deposit, ledger
from kamra.folio import _recalculate, open_folio, open_window, security_deposit_held_on_folio
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, make_property, make_reservation, make_room, make_room_type,
)


class TestAdvancePrecisionR6(AuditTestCase):
	properties = ("UT Advance R6 KWD", "UT Advance R6 USD", "UT Advance R6 UGX")

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.business_date", "kamra.folio", "kamra.ledger")

	def windows(self, currency):
		prop = make_property(f"UT Advance R6 {currency}", currency=currency, business_date=TODAY)
		rt = make_room_type(prop)
		res = make_reservation(prop, rt, TODAY, d(1), room=make_room(prop, rt, "101"))
		cashier.open_session(prop, 0)
		first = open_folio(res)
		second = open_window(reservation=res.name, window_no=2, folio_type="Guest")
		return res, first, second

	def historical(self, currency, collected, returned, held, sibling_security=0):
		res, first, second = self.windows(currency)
		api.add_folio_payment(first, "Cash", collected, kind="Security Deposit")
		api._refund_folio_payment(first, returned, "Cash", "Historical return", ledger="Deposit")
		record = frappe.db.get_value("Security Deposit", {"reservation": res.name})
		frappe.db.set_value("Security Deposit", record, {"refunded_amount": returned, "balance": held})
		fd = frappe.get_doc("Folio", first)
		_recalculate(fd)
		self.assertEqual((security_deposit_held_on_folio(fd), fd.payments_total), (held, 0))
		if sibling_security:
			api.add_folio_payment(second, "Cash", sibling_security, kind="Security Deposit")
		api.add_folio_payment(second, "Cash", collected, kind="Advance")
		result = api.refund_folio_payment(second, returned, "Cash", reason="Advance adjustment")
		self.assertEqual(frappe.db.get_value("Folio Ledger Entry", {
			"folio": second, "source_name": result["refund"], "ledger": "Deposit",
		}, "debit"), returned)
		fd.reload()
		_recalculate(fd)
		fd.save()
		fd.reload()
		self.assertEqual(
			(security_deposit_held_on_folio(fd), fd.payments_total),
			(float(Decimal(str(held)) + Decimal(str(sibling_security))), -sibling_security),
		)

	def test_advance_only_sibling_cannot_explain_historical_security_return(self):
		for args in (("KWD", 25.375, 5.125, 20.250), ("USD", 25.37, 5.12, 20.25),
		             ("UGX", 25375, 5125, 20250)):
			with self.subTest(currency=args[0]):
				self.historical(*args)

	def test_sibling_can_explain_at_most_its_own_security_funds(self):
		self.historical("KWD", 25.375, 5.125, 20.250, sibling_security=2.001)

	def funded(self, currency, per_window):
		res, first, second = self.windows(currency)
		for window in (first, second):
			api.add_folio_payment(window, "Cash", per_window, kind="Security Deposit")
		return res, first, second

	def assert_window(self, folio, held, refund):
		fd = frappe.get_doc("Folio", folio)
		rows = frappe.get_all("Folio Ledger Entry", filters={"folio": folio, "ledger": "Deposit"},
		                      fields=["debit", "credit", "source_doctype"])
		balance = sum(Decimal(str(r.credit)) - Decimal(str(r.debit)) for r in rows)
		debits = sum(Decimal(str(r.debit)) for r in rows if r.source_doctype == ledger.SECURITY_DEPOSIT_REFUND_SOURCE)
		self.assertEqual((balance, debits, security_deposit_held_on_folio(fd), fd.payments_total),
		                 (Decimal(str(held)), Decimal(str(refund)), held, 0))

	def check_split(self, currency, per_window, amount, left):
		res, first, second = self.funded(currency, per_window)
		state = api.refund_security_deposit(res.name, amount, "Cash")
		self.assertEqual((state["held"], state["refunded"]), (left, amount))
		self.assertEqual(
			[security_deposit_held_on_folio(frappe.get_doc("Folio", f)) for f in (first, second)],
			[0, left],
		)
		self.assert_window(first, 0, per_window)
		self.assert_window(second, left, float(Decimal(str(amount)) - Decimal(str(per_window))))

	def test_full_return_draws_from_both_windows(self):
		for currency, each in (("KWD", 5), ("USD", 5), ("UGX", 5000)):
			with self.subTest(currency=currency):
				self.check_split(currency, each, 2 * each, 0)

	def test_partial_return_uses_window_order_and_currency_minor_units(self):
		for args in (("KWD", 5, 7.500, 2.500), ("USD", 5, 7.51, 2.49), ("UGX", 5000, 7501, 2499)):
			with self.subTest(currency=args[0]):
				self.check_split(*args)

	def test_insufficient_target_window_refuses_without_using_another_window(self):
		res, first, second = self.funded("KWD", 5)
		with self.assertRaises(frappe.ValidationError):
			deposit.refund_deposit(res.name, 7.5, mode="Cash", folio=second)
		self.assert_window(first, 5, 0)
		self.assert_window(second, 5, 0)
		deposit.refund_deposit(res.name, 3.001, mode="Cash", folio=second)
		self.assert_window(first, 5, 0)
		self.assert_window(second, 1.999, 3.001)

	def test_second_post_failure_rolls_back_both_refunds_and_the_stay(self):
		res, first, second = self.funded("KWD", 5)
		write = api._refund_folio_payment

		def fail_second(folio, *args, **kwargs):
			result = write(folio, *args, **kwargs)
			if folio == second:
				raise RuntimeError("second window posting failed")
			return result

		with patch.object(api, "_refund_folio_payment", side_effect=fail_second):
			with self.assertRaisesRegex(RuntimeError, "second window posting failed"):
				deposit.refund_deposit(res.name, 10, "Cash")
		for window in (first, second):
			self.assert_window(window, 5, 0)
			self.assertFalse(frappe.db.exists("Folio Payment", {"parent": window, "payment_kind": "Refund"}))
		state = deposit.deposit_state(res)
		self.assertEqual((state["refunded"], state["held"]), (0, 10))

	def test_withheld_security_is_charged_only_to_the_window_that_paid_it(self):
		res, first, second = self.funded("KWD", 5)
		deposit.withhold_deposit(res.name, 2.125, "Broken lamp")
		state = api.refund_security_deposit(res.name, mode="Cash")
		self.assertEqual((state["held"], state["refunded"]), (0, 7.875))
		for name in (first, second):
			doc = frappe.get_doc("Folio", name)
			rows = frappe.get_all("Folio Ledger Entry", filters={"folio": name, "ledger": "Deposit"},
			                      fields=["debit", "credit"])
			self.assertEqual(sum(Decimal(str(r.credit)) - Decimal(str(r.debit)) for r in rows), 0)
			self.assertEqual(security_deposit_held_on_folio(doc), 0)
