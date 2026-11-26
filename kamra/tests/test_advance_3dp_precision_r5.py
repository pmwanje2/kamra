"""A Guest-ledger refund on a sibling window cannot change a historical Deposit refund."""

import frappe

from kamra import api, cashier
from kamra.folio import _recalculate, open_folio, open_window, security_deposit_held_on_folio
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, make_property, make_reservation, make_room, make_room_type,
)


class TestAdvancePrecisionR5(AuditTestCase):
	properties = ("UT Advance R5 KWD", "UT Advance R5 USD", "UT Advance R5 UGX")

	def assert_refund_debit(self, folio, ledger, amount):
		refund = frappe.get_doc("Folio", folio).payments[-1]
		self.assertEqual(refund.payment_kind, "Refund")
		self.assertFalse(refund.refund_of)
		entries = frappe.get_all("Folio Ledger Entry", filters={
			"folio": folio, "source_doctype": "Folio Payment", "source_name": refund.name,
		}, fields=["ledger", "debit"])
		self.assertEqual([(e.ledger, e.debit) for e in entries], [(ledger, amount)])

	def assert_first_window(self, folio, held):
		doc = frappe.get_doc("Folio", folio)
		_recalculate(doc)
		doc.save(ignore_permissions=True)
		doc.reload()
		self.assertEqual((security_deposit_held_on_folio(doc), doc.payments_total), (held, 0))

	def check_guest_refund(self, currency, collected, returned, held):
		self.at(TODAY, "kamra.business_date", "kamra.folio", "kamra.ledger")
		prop = make_property(f"UT Advance R5 {currency}", currency=currency, business_date=TODAY)
		room_type = make_room_type(prop)
		res = make_reservation(prop, room_type, TODAY, d(1), room=make_room(prop, room_type, "101"))
		cashier.open_session(prop, 0)
		first = open_folio(res)
		api.add_folio_payment(first, "Cash", collected, kind="Security Deposit")
		# Historical returns posted to Deposit and recorded the total on the stay.
		api._refund_folio_payment(first, returned, "Cash", "Partial return", ledger="Deposit")
		record = frappe.db.get_value("Security Deposit", {"reservation": res.name})
		frappe.db.set_value("Security Deposit", record, {"refunded_amount": returned, "balance": held})
		self.assert_refund_debit(first, "Deposit", returned)
		self.assert_first_window(first, held)

		second = open_window(reservation=res.name, window_no=2, folio_type="Guest")
		api.add_folio_payment(second, "Cash", collected, kind="Payment")
		api.refund_folio_payment(second, returned, "Cash", reason="Booking adjustment")
		self.assert_refund_debit(second, "Guest", returned)
		self.assert_first_window(first, held)

	def test_kwd_guest_refund_preserves_first_window(self):
		self.check_guest_refund("KWD", 25.375, 5.125, 20.250)

	def test_usd_guest_refund_preserves_first_window(self):
		self.check_guest_refund("USD", 25.37, 5.12, 20.25)

	def test_ugx_guest_refund_preserves_first_window(self):
		self.check_guest_refund("UGX", 25375, 5125, 20250)
