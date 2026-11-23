"""Correcting an issued invoice (group billing P0).

A hotel without the ERPNext bridge corrects an invoice here: a credit note
that carries the tax it credits, or a cancellation that reopens the folio.
Where the bridge issued the invoice (the folio carries the bridge's
sales_invoice), the invoice is corrected only by the bridge's credit and
re-bill flow, as OPERA's Credit Bill, SAP's VF11 cancellation and a URA
EFRIS credit note do: nothing here reopens or credits that folio on its own.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from kamra import api, ledger
from kamra import folio as folio_mod
from kamra.ledger import save_folio
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)
from kamra.tests.bridge_fields import ensure_bridge_field

BRIDGE_FLOW = "hospitality_kamra.credit_and_rebill"
REFUSED = "credit note and re-bill"


class InvoiceCorrectionTestCase(AuditTestCase):
	properties = (PROP_A,)

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_bridge_field("Folio", "sales_invoice")

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY)
		frappe.db.delete("Property Setting", {"property": PROP_A})
		rt = make_room_type(PROP_A, "STD", base_price=1000)
		res = make_reservation(PROP_A, rt, d(-1), d(1), status="Checked In",
		                       room=make_room(PROP_A, rt, "101"))
		self.folio = folio_mod.split_folio(res.name, "Extra")
		doc = frappe.get_doc("Folio", self.folio)
		doc.append("charges", {"posting_date": TODAY, "reservation": res.name,
		                       "charge_type": "Misc", "description": "UT hall hire",
		                       "qty": 1, "rate": 1000, "amount": 1000, "gst_rate": 18})
		folio_mod._recalculate(doc)
		save_folio(doc)
		api.add_folio_payment(self.folio, "Cash", 1180)
		self.number = folio_mod.close_folio(self.folio)

	def issued_by_bridge(self):
		frappe.db.set_value("Folio", self.folio, "sales_invoice", "UT-SINV-0001",
		                    update_modified=False)

	def assert_untouched(self):
		f = frappe.db.get_value("Folio", self.folio, ["status", "invoice_number"], as_dict=True)
		self.assertEqual((f.status, f.invoice_number), ("Closed", self.number))
		self.assertFalse(frappe.db.exists("Credit Note", {"folio": self.folio}))
		self.assertFalse(frappe.db.exists("Cancelled Invoice", {"folio": self.folio}))

	def test_a_credit_note_here_carries_the_tax_it_credits(self):
		out = ledger.issue_credit_note(self.folio, 590, "UT hall half day")
		cn = frappe.get_doc("Credit Note", out["credit_note"])
		self.assertEqual((cn.amount, cn.tax_amount), (590, 90))

	def test_a_bridge_invoice_is_never_credited_here(self):
		self.issued_by_bridge()
		with self.assertRaisesRegex(frappe.ValidationError, REFUSED):
			ledger.issue_credit_note(self.folio, 100, "UT goodwill")
		self.assert_untouched()

	def test_a_bridge_invoice_is_never_reopened_here(self):
		self.issued_by_bridge()
		with self.assertRaisesRegex(frappe.ValidationError, REFUSED):
			api.cancel_invoice(self.folio, "UT wrong guest name")
		# the same refusal for every caller of the cancellation (reinstating
		# a check-out cancels the stay's invoices through it)
		with self.assertRaisesRegex(frappe.ValidationError, REFUSED):
			folio_mod.cancel_invoice(self.folio, "UT checkout reinstated")
		self.assert_untouched()

	def test_corrections_of_a_bridge_invoice_go_to_its_credit_and_rebill(self):
		self.issued_by_bridge()
		script, calls = object(), []

		def bridge_script(method):
			return script if method == BRIDGE_FLOW else None

		def run(found, form):
			calls.append((found, form))
			return {"routed": form["folio"]}

		with patch("kamra.erp_bridge.get_bridge_script", side_effect=bridge_script), \
				patch("kamra.erp_bridge.run_bridge_script", side_effect=run):
			credit = ledger.issue_credit_note(self.folio, 100, "UT short stay")
			cancel = api.cancel_invoice(self.folio, "UT wrong guest name")
		self.assertEqual(calls, [
			(script, {"folio": self.folio, "reason": "UT short stay", "amount": 100.0}),
			(script, {"folio": self.folio, "reason": "UT wrong guest name", "amount": None}),
		])
		self.assertEqual((credit, cancel), ({"routed": self.folio}, {"routed": self.folio}))
		self.assert_untouched()
