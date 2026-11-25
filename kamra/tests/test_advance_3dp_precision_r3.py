"""Check-in preserves security deposits using recorded refund provenance."""

from decimal import Decimal

import frappe

from kamra import api, cashier, ledger
from kamra.folio import open_folio, security_deposit_held_on_folio
from kamra.tests.audit_fixtures import (
	TODAY,
	AuditTestCase,
	d,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)


class TestAdvancePrecisionR3(AuditTestCase):
	properties = ("UT Advance R3 KWD",)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.business_date", "kamra.folio", "kamra.ledger")
		prop = make_property(self.properties[0], currency="KWD", business_date=TODAY)
		room_type = make_room_type(prop)
		room = make_room(prop, room_type, "101")
		self.res = make_reservation(prop, room_type, TODAY, d(1), room=room)
		self.folio = open_folio(self.res)
		cashier.open_session(prop, 0)
		api.add_folio_payment(self.folio, "Cash", 100.019, kind="Advance")
		api.add_folio_payment(self.folio, "Cash", 25.375, kind="Security Deposit")
		self.deposit = frappe.db.get_value("Security Deposit", {"reservation": self.res.name}, "name")

	def assert_check_in_preserves_security(self, held):
		self.res.reload()
		self.res.status = "Checked In"
		self.res.save(ignore_permissions=True)
		rows = frappe.get_all(
			"Folio Ledger Entry", filters={"folio": self.folio},
			fields=["ledger", "description", "debit", "credit"],
		)
		applied = [r for r in rows if r.description == ledger.ADVANCE_APPLIED]
		self.assertEqual(
			sorted((r.ledger, Decimal(str(r.debit)), Decimal(str(r.credit))) for r in applied),
			[("Deposit", Decimal("60.007"), 0), ("Guest", 0, Decimal("60.007"))],
		)
		self.assertEqual(sum(Decimal(str(r.credit)) - Decimal(str(r.debit))
		                     for r in rows if r.ledger == "Deposit"), Decimal(held))
		doc = frappe.get_doc("Folio", self.folio)
		self.assertEqual(Decimal(str(security_deposit_held_on_folio(doc))), Decimal(held))
		self.assertEqual(Decimal(str(doc.payments_total)), Decimal("60.007"))
		self.assertEqual(ledger.transfer_advances_to_guest(self.res.name), 0)

	def test_ordinary_unlinked_refund_reason_does_not_spend_security_deposit(self):
		api.refund_folio_payment(
			self.folio, 40.012, "Cash", reason=f"Security deposit refund {self.deposit}: booking adjustment",
		)
		self.assert_check_in_preserves_security("25.375")

	def test_real_security_deposit_refund_keeps_its_provenance_after_note_edit(self):
		api.refund_security_deposit(self.res.name, 5.125, "Cash", "Partial return")
		refund = frappe.get_doc("Folio", self.folio).payments[-1]
		frappe.db.set_value("Folio Payment", refund.name, "reference", "Partial return")
		api.refund_folio_payment(self.folio, 40.012, "Cash", reason="Booking adjustment")
		self.assert_check_in_preserves_security("20.250")

	def test_legacy_refund_uses_recorded_security_deposit_total(self):
		# Older local refunds recorded the aggregate on Security Deposit.
		api._refund_folio_payment(
			self.folio, 5.125, "Cash", "Partial return", ledger="Deposit", allow_closed=True,
		)
		frappe.db.set_value("Security Deposit", self.deposit, {"refunded_amount": 5.125, "balance": 20.250})
		api.refund_folio_payment(
			self.folio, 40.012, "Cash", reason=f"Security deposit refund {self.deposit}: booking adjustment",
		)
		self.assert_check_in_preserves_security("20.250")
