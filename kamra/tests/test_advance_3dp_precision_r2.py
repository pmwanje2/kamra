"""Check-in can apply only the ordinary Deposit balance at folio precision."""

from decimal import Decimal

import frappe

from kamra import api, cashier, ledger, payment_transfers
from kamra.folio import open_folio, open_window
from kamra.tests.audit_fixtures import (
	TODAY,
	AuditTestCase,
	d,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)


class TestAdvancePrecisionR2(AuditTestCase):
	properties = ("UT Advance R2 KWD", "UT Advance R2 USD")

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.business_date", "kamra.folio", "kamra.ledger")

	def advance(self, currency, paid):
		prop = make_property(f"UT Advance R2 {currency}", currency=currency, business_date=TODAY)
		room_type = make_room_type(prop)
		room = make_room(prop, room_type, "101")
		res = make_reservation(prop, room_type, TODAY, d(1), room=room)
		folio = open_folio(res)
		cashier.open_session(prop, 0)
		row = api.add_folio_payment(folio, "Cash", paid, kind="Advance")["payments"][-1]["name"]
		return res, folio, row

	def check_in(self, res):
		res.reload()
		res.status = "Checked In"
		res.save(ignore_permissions=True)
		self.assertEqual(ledger.transfer_advances_to_guest(res.name), 0)

	def assert_transfer(self, folio, amount, remaining="0"):
		entries = frappe.get_all(
			"Folio Ledger Entry", filters={"folio": folio},
			fields=["ledger", "description", "debit", "credit"],
		)
		applied = [row for row in entries if row.description == ledger.ADVANCE_APPLIED]
		debits = sum((Decimal(str(row.debit)) for row in applied if row.ledger == "Deposit"), Decimal(0))
		credits = sum((Decimal(str(row.credit)) for row in applied if row.ledger == "Guest"), Decimal(0))
		deposit = sum(
			Decimal(str(row.credit)) - Decimal(str(row.debit))
			for row in entries if row.ledger == "Deposit"
		)
		self.assertEqual(debits, Decimal(amount))
		self.assertEqual(credits, Decimal(amount))
		self.assertEqual(deposit, Decimal(remaining))

	def test_kwd_uses_folio_precision_even_when_global_precision_is_two(self):
		res, folio, row = self.advance("KWD", 100.019)
		api.refund_folio_payment(folio, 40.012, reason="Partial advance refund", payment_row=row)
		frappe.db.set_single_value("System Settings", "currency_precision", "2")
		self.assertEqual(frappe.get_doc("Folio", folio).precision("grand_total"), 3)
		self.check_in(res)
		self.assert_transfer(folio, "60.007")

	def test_unlinked_refund_reduces_advance_and_leaves_security_deposit_held(self):
		res, folio, _row = self.advance("KWD", 100.019)
		api.add_folio_payment(folio, "Cash", 25.375, kind="Security Deposit")
		api.refund_folio_payment(folio, 40.012, "Cash", reason="Unlinked advance refund")
		self.check_in(res)
		self.assert_transfer(folio, "60.007", remaining="25.375")

	def test_unlinked_refund_caps_the_combined_transfer_of_multiple_advances(self):
		res, folio, _row = self.advance("KWD", 50.019)
		api.add_folio_payment(folio, "Cash", 50, kind="Advance")
		api.refund_folio_payment(folio, 40.012, "Cash", reason="Unlinked advance refund")
		self.check_in(res)
		self.assert_transfer(folio, "60.007")

	def test_partial_window_transfer_reduces_source_advance_and_funds_target(self):
		res, folio, row = self.advance("KWD", 100.019)
		target = open_window(reservation=res.name, payee_type="Guest")
		payment_transfers.transfer_folio_payment(
			folio, row, target, reason="Split advance across windows", amount=40.012,
		)
		self.check_in(res)
		self.assert_transfer(folio, "60.007")
		self.assert_transfer(target, "40.012")

	def test_sub_minor_ledger_remainder_cannot_round_up(self):
		res, folio, row = self.advance("KWD", 60.008)
		ledger.write_ledger_entry(
			property=res.property, folio=folio, ledger="Deposit", debit=0.0004,
			source_doctype="Folio Payment", source_name=row,
		)
		self.check_in(res)
		self.assert_transfer(folio, "60.007", remaining="0.0006")

	def test_usd_uses_folio_precision_even_when_global_precision_is_three(self):
		res, folio, row = self.advance("USD", 0.01)
		frappe.db.set_single_value("System Settings", "currency_precision", "3")
		self.assertEqual(frappe.get_doc("Folio", folio).precision("grand_total"), 2)
		ledger.write_ledger_entry(
			property=res.property, folio=folio, ledger="Deposit", debit=0.006,
			source_doctype="Folio Payment", source_name=row,
		)
		self.check_in(res)
		self.assert_transfer(folio, "0", remaining="0.004")
