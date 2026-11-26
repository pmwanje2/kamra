"""Deposit refunds retain their window, reversal provenance and recorded history."""

from decimal import Decimal

import frappe

from kamra import api, cashier, deposit, ledger
from kamra.folio import _recalculate, open_folio, open_window, security_deposit_held_on_folio
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, make_property, make_reservation, make_room, make_room_type,
)


class TestAdvancePrecisionR4(AuditTestCase):
	properties = ("UT Advance R4 KWD", "UT Advance R4 USD", "UT Advance R4 UGX")
	# Advance, ordinary refund, security collection, security refund.
	cases = (
		("KWD", 100.019, 40.012, 25.375, 5.125),
		("USD", 100.19, 40.12, 25.37, 5.12),
		("UGX", 100019, 40012, 25375, 5125),
	)

	def stay(self, currency):
		prop = make_property(f"UT Advance R4 {currency}", currency=currency, business_date=TODAY)
		room_type = make_room_type(prop)
		res = make_reservation(prop, room_type, TODAY, d(1), room=make_room(prop, room_type, "101"))
		cashier.open_session(prop, 0)
		return res, open_folio(res)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.business_date", "kamra.folio", "kamra.ledger")

	def test_security_refund_on_first_window_cannot_release_security_on_second(self):
		for currency, advance, ordinary_refund, security, returned in self.cases:
			with self.subTest(currency=currency):
				res, first = self.stay(currency)
				api.add_folio_payment(first, "Cash", returned, kind="Security Deposit")
				api.refund_security_deposit(res.name, returned, "Cash")
				second = open_window(reservation=res.name, window_no=2, folio_type="Guest")
				api.add_folio_payment(second, "Cash", advance, kind="Advance")
				api.add_folio_payment(second, "Cash", security, kind="Security Deposit")
				api.refund_folio_payment(second, ordinary_refund, "Cash", reason="Booking adjustment")
				res.reload()
				res.status = "Checked In"
				res.save(ignore_permissions=True)
				moved = frappe.db.get_value("Folio Ledger Entry", {
					"folio": second, "ledger": "Deposit", "description": ledger.ADVANCE_APPLIED,
				}, "debit")
				self.assertEqual(Decimal(str(moved)), Decimal(str(advance)) - Decimal(str(ordinary_refund)))
				self.assertEqual(security_deposit_held_on_folio(frappe.get_doc("Folio", second)), security)
				self.assertEqual(ledger.transfer_advances_to_guest(res.name), 0)

	def test_deposit_reversal_written_after_refund_save_updates_persisted_totals(self):
		res, folio = self.stay("UGX")
		api.add_folio_payment(folio, "Cash", 100000, kind="Security Deposit")
		doc = frappe.get_doc("Folio", folio)
		original = frappe.db.get_value("Folio Ledger Entry", {
			"folio": folio, "ledger": "Deposit", "source_name": doc.payments[-1].name,
		})
		self.assertTrue(original)
		refund = doc.append("payments", {
			"posting_date": TODAY, "payment_kind": "Refund", "mode": "Cash",
			"currency": "UGX", "amount": -100000,
		})
		_recalculate(doc)
		doc.save(ignore_permissions=True)
		ledger.write_ledger_entry(
			property=res.property, folio=folio, ledger="Deposit", debit=100000,
			source_doctype="Folio Payment", source_name=refund.name, reversal_of=original,
		)
		doc.reload()
		self.assertEqual(doc.payments_total, 0)
		self.assertEqual(doc.balance, doc.grand_total)

	def test_unattributed_recorded_refunds_are_visible_in_history(self):
		for currency, _, _, security, returned in self.cases:
			with self.subTest(currency=currency):
				res, folio = self.stay(currency)
				api.add_folio_payment(folio, "Cash", security, kind="Security Deposit")
				api._refund_folio_payment(folio, returned, "Cash", "Legacy return", ledger="Deposit")
				record = frappe.db.get_value("Security Deposit", {"reservation": res.name})
				frappe.db.set_value("Security Deposit", record, {
					"refunded_amount": returned, "balance": float(Decimal(str(security)) - Decimal(str(returned))),
				})
				state = deposit.deposit_state(res)
				self.assertEqual(state["refunded"], returned)
				refunds = [h for h in state["history"] if h["kind"] == "Refunded"]
				self.assertEqual([h["amount"] for h in refunds], [returned])
				self.assertIn("histor", refunds[0]["note"].lower())
				self.assertFalse(any(refunds[0].get(k) for k in ("date", "mode", "payment_row")))
				# A subsequent, individually recorded return must appear exactly once.
				api.refund_security_deposit(res.name, returned, "Cash")
				history = deposit.deposit_state(res)["history"]
				refunds = [h for h in history if h["kind"] == "Refunded"]
				self.assertEqual(sorted((bool(h["date"]), h["amount"]) for h in refunds),
				                 [(False, returned), (True, returned)])
