"""Correcting an issued invoice (group billing P0).

A hotel without the ERPNext bridge corrects an invoice here: a credit note
that carries the tax it credits, or a cancellation that reopens the folio.
Where the bridge issued the invoice (the folio carries the bridge's
sales_invoice), the invoice is corrected only by the bridge's credit and
re-bill flow, as OPERA's Credit Bill, SAP's VF11 cancellation and a URA
EFRIS credit note do: nothing here reopens or credits that folio on its own.
The bridge may close a folio at check-out with its Sales Invoice and no hotel
invoice number; that folio is invoiced all the same. Credit notes never add
up to more than the invoice (OPERA's Credit Bill credits at most the bill),
and they count before tax in revenue and as tax given back.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from kamra import api, books, ledger
from kamra import folio as folio_mod
from kamra.ledger import save_folio
from kamra.tests.audit_fixtures import (
	PROP_A,
	SAVEPOINT,
	TODAY,
	AuditTestCase,
	d,
	ensure_user,
	guest_folio,
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
		self.invoice()

	def invoice(self):
		"""A 1,000 + 18% hall hire on its own bill, paid and invoiced."""
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

	def closed_by_bridge_at_checkout(self):
		"""The bridge's Kamra ERP Checkout Close: the folio is closed with the
		bridge's Sales Invoice and no hotel invoice number."""
		frappe.db.set_value("Folio", self.folio, {"sales_invoice": "UT-SINV-0002",
		                                          "invoice_number": None},
		                    update_modified=False)
		self.number = None

	def bridge_flow(self):
		"""The bridge's credit and re-bill, recording what it was asked."""
		script, calls = object(), []

		def bridge_script(method):
			return script if method == BRIDGE_FLOW else None

		def run(found, form):
			calls.append(form)
			return {"routed": form["folio"]}

		self._stack.enter_context(patch("kamra.erp_bridge.get_bridge_script",
		                                side_effect=bridge_script))
		self._stack.enter_context(patch("kamra.erp_bridge.run_bridge_script", side_effect=run))
		return calls

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

	# a folio the bridge closed at check-out is invoiced ------------------

	def test_a_bill_the_bridge_closed_at_checkout_goes_to_credit_and_rebill(self):
		self.closed_by_bridge_at_checkout()
		calls = self.bridge_flow()
		self.assertEqual(ledger.issue_credit_note(self.folio, 100, "UT short stay"),
		                 {"routed": self.folio})
		self.assertEqual(api.cancel_invoice(self.folio, "UT wrong guest name"),
		                 {"routed": self.folio})
		self.assertEqual([(c["amount"], c["reason"]) for c in calls],
		                 [(100.0, "UT short stay"), (None, "UT wrong guest name")])
		self.assert_untouched()

	def test_a_bill_the_bridge_closed_at_checkout_is_never_corrected_here(self):
		self.closed_by_bridge_at_checkout()
		with self.assertRaisesRegex(frappe.ValidationError, REFUSED):
			ledger.issue_credit_note(self.folio, 100, "UT goodwill")
		with self.assertRaisesRegex(frappe.ValidationError, REFUSED):
			api.cancel_invoice(self.folio, "UT wrong guest name")
		with self.assertRaisesRegex(frappe.ValidationError, REFUSED):
			folio_mod.cancel_invoice(self.folio, "UT checkout reinstated")
		self.assert_untouched()

	def _bridge_checked_out_stay(self, room_no: str, sales_invoice: str, *,
	                             use_save: bool = False, with_extra: bool = False):
		rt = make_room_type(PROP_A, "STD", base_price=1000)
		res = make_reservation(PROP_A, rt, d(-1), TODAY, status="Checked In",
		                       room=make_room(PROP_A, rt, room_no))
		folio_mod.post_remaining_nights(res)
		gf = guest_folio(res.name)
		bal = float(frappe.db.get_value("Folio", gf, "balance") or 0)
		if bal > 0:
			api.add_folio_payment(gf, "Cash", bal)
		extra, extra_inv = None, None
		if with_extra:
			extra = folio_mod.split_folio(res.name, "Extra")
			edoc = frappe.get_doc("Folio", extra)
			edoc.append("charges", {"posting_date": TODAY, "reservation": res.name,
			                        "charge_type": "Misc", "description": "UT minibar",
			                        "qty": 1, "rate": 100, "amount": 100, "gst_rate": 0})
			folio_mod._recalculate(edoc)
			save_folio(edoc)
			api.add_folio_payment(extra, "Cash", 100)
			extra_inv = folio_mod.close_folio(extra)
		api.check_out(res.name)
		if use_save:
			# mirrors Kamra ERP Checkout Close + Folio Close: closes folio with
			# sales_invoice and leaves invoice_number and closed_on unset
			fdoc = frappe.get_doc("Folio", gf)
			fdoc.status = "Closed"
			fdoc.invoice_number = None
			fdoc.closed_on = None
			fdoc.sales_invoice = sales_invoice
			fdoc.save(ignore_permissions=True)
		else:
			frappe.db.set_value("Folio", gf, {
				"status": "Closed",
				"sales_invoice": sales_invoice,
				"invoice_number": None,
			}, update_modified=False)
		return res, gf, extra, extra_inv

	def test_reinstating_a_bridge_checkout_is_refused(self):
		"""OPERA/ERP: a folio already invoiced by the accounting system at
		check-out (sales_invoice set, no hotel invoice_number) cannot be
		reopened by reinstating the check-out, and nothing else on the stay
		is cancelled or moved back to Checked In."""
		res, gf, extra, extra_inv = self._bridge_checked_out_stay(
			"102", "UT-SINV-CHK", with_extra=True)

		with self.assertRaisesRegex(frappe.ValidationError, REFUSED):
			api.reinstate_checkout(res.name, "UT checked out by mistake")

		self.assertEqual(frappe.db.get_value("Reservation", res.name, "status"), "Checked Out")
		self.assertEqual(
			frappe.db.get_value("Folio", gf, ["status", "invoice_number", "sales_invoice"]),
			("Closed", None, "UT-SINV-CHK"),
		)
		self.assertEqual(
			frappe.db.get_value("Folio", extra, ["status", "invoice_number"]),
			("Closed", extra_inv),
		)
		self.assertFalse(frappe.db.exists("Cancelled Invoice", {"folio": ("in", [gf, extra])}))

	def test_reinstating_a_bridge_checkout_after_prior_night_audit_is_refused(self):
		"""When a prior Night Audit Run exists and Kamra ERP Checkout Close
		closed the folio without stamping closed_on, reinstate_checkout still
		sees the bridge-invoiced folio and refuses."""
		run = frappe.get_doc({
			"doctype": "Night Audit Run",
			"property": PROP_A,
			"business_date": d(-1),
			"status": "Completed",
		}).insert(ignore_permissions=True)
		frappe.db.set_value("Night Audit Run", run.name, "creation", "2020-01-01 00:00:00",
		                    update_modified=False)

		res, gf, _extra, _extra_inv = self._bridge_checked_out_stay(
			"103", "UT-SINV-CHK-2", use_save=True)

		with self.assertRaisesRegex(frappe.ValidationError, REFUSED):
			api.reinstate_checkout(res.name, "UT checked out by mistake")

		self.assertEqual(frappe.db.get_value("Reservation", res.name, "status"), "Checked Out")
		self.assertEqual(
			frappe.db.get_value("Folio", gf, ["status", "invoice_number", "sales_invoice"]),
			("Closed", None, "UT-SINV-CHK-2"),
		)
		self.assertFalse(frappe.db.exists("Cancelled Invoice", {"folio": gf}))

	def test_a_bridge_cancellation_is_checked_and_logged_like_any_other(self):
		self.issued_by_bridge()
		calls = self.bridge_flow()
		api.cancel_invoice(self.folio, "UT wrong guest name")
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"action_type": "cancel_invoice", "reference_name": self.folio}))
		# the same roles as a credit note: Front Desk may not credit a bill
		frappe.set_user(ensure_user("ut-desk@example.com", ["Front Desk"]))  # nosemgrep: frappe-setuser -- test fixture
		with self.assertRaises(frappe.PermissionError):
			api.cancel_invoice(self.folio, "UT desk tries")
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		# and only an issued, closed bill is sent
		frappe.db.set_value("Folio", self.folio, "status", "Open", update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			api.cancel_invoice(self.folio, "UT still open")
		self.assertEqual(len(calls), 1)

	# credit notes are capped and counted once -----------------------------

	def test_credit_notes_never_add_up_to_more_than_the_invoice(self):
		ledger.issue_credit_note(self.folio, 1000, "UT hall unused")
		with self.assertRaisesRegex(frappe.ValidationError, "180"):
			ledger.issue_credit_note(self.folio, 200, "UT too much")
		ledger.issue_credit_note(self.folio, 180, "UT the rest")
		with self.assertRaises(frappe.ValidationError):
			ledger.issue_credit_note(self.folio, 1, "UT nothing left")
		self.assertEqual(frappe.db.count("Credit Note", {"folio": self.folio}), 2)
		self.assertEqual(api.folio_invoice(self.folio)["credited"], 1180)

	def test_a_credit_note_committed_meanwhile_is_counted(self):
		"""Another desk credits the invoice and commits while this request is
		under way (its snapshot is older). The cap still sees that credit:
		the cap reads the credit notes with a locking read, which either
		returns the committed row or (MariaDB 11.8, innodb_snapshot_isolation)
		refuses with error 1020. Either way nothing is over-credited."""
		# the tests of this class share one transaction, and a rollback to a
		# savepoint keeps the row locks taken after it (credit notes read
		# under lock): start afresh, so the other desk waits on nothing of theirs
		frappe.db.rollback()
		frappe.db.savepoint(SAVEPOINT)
		self.invoice()
		other = _second_connection()
		name = f"UT-CN-RACE-{frappe.generate_hash(length=6)}"
		mine = None
		try:
			other.sql("""INSERT INTO `tabCredit Note` (name, property, folio, amount,
			             tax_amount, status, credit_note_number, issue_date)
			             VALUES (%s, %s, %s, 1000, 152.54, 'Issued', %s, %s)""",
			          (name, PROP_A, self.folio, name, TODAY))
			other.commit()
			with self.assertRaises((frappe.ValidationError, frappe.QueryDeadlockError)):
				ledger.issue_credit_note(self.folio, 1000, "UT second desk")
			mine = frappe.db.sql(
				"""SELECT name FROM `tabCredit Note` WHERE folio = %s AND name != %s""",
				(self.folio, name))
		finally:
			# end this transaction (its row locks with it) before the other desk
			# tidies its committed row away, then once more so the next snapshot
			# is taken after that; tearDown rolls back to a fresh savepoint
			frappe.db.rollback()
			other.sql("DELETE FROM `tabCredit Note` WHERE name = %s", (name,))
			other.commit()
			other.close()
			frappe.db.rollback()
			frappe.db.savepoint(SAVEPOINT)
		self.assertFalse(mine)

	def test_a_credit_note_counts_once_before_tax_and_once_in_tax(self):
		self.assertEqual(books.tax_collected(PROP_A, TODAY, TODAY), 180)
		ledger.issue_credit_note(self.folio, 590, "UT hall half day")
		self.assertEqual(books._revenue(PROP_A, TODAY, TODAY)["allowances"], -500)
		self.assertEqual(books.tax_collected(PROP_A, TODAY, TODAY), 90)

	def test_the_tax_in_a_credit_note_is_in_the_currencys_minor_unit(self):
		make_property(PROP_A, business_date=TODAY, currency="UGX")
		out = ledger.issue_credit_note(self.folio, 100, "UT shilling credit")
		# 100 x 180 / 1180 = 15.25: a shilling bill states 15
		self.assertEqual(frappe.db.get_value("Credit Note", out["credit_note"], "tax_amount"), 15)


def _second_connection():
	"""A connection of its own to this site's database: another desk."""
	from frappe.database import get_db
	conf = frappe.local.conf
	return get_db(host=conf.db_host, port=conf.db_port, user=conf.db_user or conf.db_name,
	              password=conf.db_password, cur_db_name=conf.db_name)
