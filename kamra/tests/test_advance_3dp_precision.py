"""Regression coverage for check-in advance transfers at currency precision."""

import frappe

from kamra import api, cashier, ledger
from kamra.folio import open_folio
from kamra.tests.audit_fixtures import (
	TODAY,
	AuditTestCase,
	d,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)


class TestAdvancePrecision(AuditTestCase):
	properties = ("UT Advance KWD", "UT Advance UGX", "UT Advance USD")

	def advance(self, currency, paid):
		prop = make_property(f"UT Advance {currency}", currency=currency, business_date=TODAY)
		self.at(TODAY, "kamra.business_date", "kamra.folio", "kamra.ledger")
		room_type = make_room_type(prop)
		room = make_room(prop, room_type, "101")
		res = make_reservation(prop, room_type, TODAY, d(1), room=room)
		folio = open_folio(res)
		cashier.open_session(prop, 0)
		row = api.add_folio_payment(folio, "Cash", paid, kind="Advance")["payments"][-1]["name"]
		return res, folio, row

	def assert_check_in_transfer(self, res, folio, amount):
		res.reload()
		res.status = "Checked In"
		res.save(ignore_permissions=True)

		rows = frappe.get_all(
			"Folio Ledger Entry", filters={"folio": folio, "description": ledger.ADVANCE_APPLIED},
			fields=["ledger", "debit", "credit"],
		)
		self.assertEqual(
			sorted((row.ledger, row.debit, row.credit) for row in rows),
			[("Deposit", amount, 0), ("Guest", 0, amount)],
		)
		deposit_rows = frappe.get_all(
			"Folio Ledger Entry", filters={"folio": folio, "ledger": "Deposit"},
			fields=["debit", "credit"],
		)
		self.assertEqual(sum(ledger._money(row.credit) - ledger._money(row.debit)
		                     for row in deposit_rows), 0)
		self.assertEqual(ledger.transfer_advances_to_guest(res.name), 0)

	def test_kwd_transfers_exact_balance_after_linked_refund(self):
		res, folio, row = self.advance("KWD", 100.019)
		api.refund_folio_payment(folio, 40.012, reason="Partial advance refund", payment_row=row)
		self.assert_check_in_transfer(res, folio, 60.007)

	def test_kwd_transfers_four_fils_after_linked_refund(self):
		res, folio, row = self.advance("KWD", 100.019)
		api.refund_folio_payment(folio, 100.015, reason="Partial advance refund", payment_row=row)
		self.assert_check_in_transfer(res, folio, 0.004)

	def test_ugx_transfers_whole_units(self):
		res, folio, _row = self.advance("UGX", 60)
		self.assert_check_in_transfer(res, folio, 60)

	def test_usd_transfers_one_cent(self):
		res, folio, _row = self.advance("USD", 0.01)
		self.assert_check_in_transfer(res, folio, 0.01)

	def test_explicit_frappe_precision_takes_precedence(self):
		res, folio, row = self.advance("USD", 0.01)
		# The ledger can carry balances posted under an explicit precision.
		frappe.db.set_single_value("System Settings", "currency_precision", "3")
		ledger.write_ledger_entry(
			property=res.property, folio=folio, ledger="Deposit", debit=0.006,
			source_doctype="Folio Payment", source_name=row,
		)
		self.assert_check_in_transfer(res, folio, 0.004)
