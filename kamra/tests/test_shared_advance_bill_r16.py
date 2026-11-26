"""A retried advance bill describes the posted window and its current credit position."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from kamra import folio, ledger, shares
from kamra.tests.audit_fixtures import TODAY, d, make_guest, make_property, make_reservation, make_room, make_room_type
from kamra.tests.test_advance_3dp_precision_r12 import receipt_fixture
from kamra.tests.test_fiscal_folio import OwnedSalesInvoiceFixture
from kamra.tests.test_invoice_corrections import _second_connection


class SharedAdvanceBillRetryTest(OwnedSalesInvoiceFixture, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.get_meta("Sales Invoice").has_field("kamra_rebill_of"):
			from frappe.custom.doctype.custom_field.custom_field import create_custom_field

			field = create_custom_field("Sales Invoice", {
				"fieldname": "kamra_rebill_of", "label": "Rebill of", "fieldtype": "Data",
			})
			cls.addClassCleanup(frappe.delete_doc, "Custom Field", field.name, force=True)

	def setUp(self):
		super().setUp()
		old = frappe.db.sql("SELECT @@innodb_snapshot_isolation")[0][0]
		self.addCleanup(frappe.db.sql, "SET SESSION innodb_snapshot_isolation=%s", (old,))
		frappe.db.sql("SET SESSION innodb_snapshot_isolation=1")
		self.assertEqual(frappe.db.sql("SELECT @@tx_isolation")[0][0], "REPEATABLE-READ")
		room, _, _ = self.enterContext(receipt_fixture("USD"))
		self.prop = frappe.db.get_value("Room", room, "property")
		make_property(self.prop, company="R16 Company", currency="USD", business_date=TODAY)
		rt = make_room_type(self.prop, "SHARE", base_price=100, tax_percent=0)
		lead = make_reservation(self.prop, rt, TODAY, d(3), room=make_room(self.prop, rt, "201"))
		self.peer = shares.create_share_reservation(lead.name, make_guest("R16 peer"))["reservation"]
		ledger.ensure_default_transaction_codes()

	def check_concurrent_credit(self, rebill):
		first = ledger.force_advance_bill(self.peer, "tonight")
		self.assertEqual(first["balance"], 50)
		root = "R16-" + frappe.generate_hash(length=12)
		self.link_sales_invoice(first["folio"], root)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- publish the invoiced stay to the independent accountant
		self.assertEqual(frappe.db.get_value("Sales Invoice", root, "grand_total"), 50)
		frappe.db.savepoint("r16_before_retry")

		writer = _second_connection()
		try:
			with patch.object(frappe.local, "db", writer):
				invoice = root
				fields = {"doctype": "Sales Invoice", "company": "R16 Company", "currency": "USD", "docstatus": 1}
				if rebill:
					invoice = root + "-replacement"
					frappe.get_doc({**fields, "name": invoice, "kamra_rebill_of": root, "grand_total": 60}).db_insert()
				frappe.get_doc({
					**fields, "name": root + "-credit", "is_return": 1, "return_against": invoice, "grand_total": -20,
				}).db_insert()
				writer.commit()
		finally:
			writer.close()

		self.assertFalse(frappe.db.exists("Sales Invoice", root + "-credit"))
		retry = ledger.force_advance_bill(self.peer, "tonight")
		self.assertEqual((retry["folio"], retry["amount"], retry["nights_posted"]), (first["folio"], 0, []))
		self.assertEqual(retry["balance"], 40 if rebill else 30)
		self.assertEqual(frappe.db.sql("SELECT @@innodb_snapshot_isolation")[0][0], 1)
		frappe.db.rollback(save_point="r16_before_retry")

	def test_retry_sees_concurrently_committed_credit(self):
		self.check_concurrent_credit(rebill=False)

	def test_retry_sees_concurrently_committed_rebill_and_its_credit(self):
		self.check_concurrent_credit(rebill=True)

	def test_retry_returns_the_routed_room_window(self):
		extra = folio.open_window(reservation=self.peer, window_no=2)
		frappe.get_doc({
			"doctype": "Routing Instruction", "property": self.prop, "reservation": self.peer,
			"target_type": "Window", "target_window": 2, "codes": [{"charge_type": "Room"}],
		}).insert()
		first = ledger.force_advance_bill(self.peer, "tonight")
		self.assertEqual((first["folio"], first["balance"], first["amount"]), (extra, 50, 50))
		retry = ledger.force_advance_bill(self.peer, "tonight")
		self.assertEqual((retry["folio"], retry["balance"], retry["amount"], retry["nights_posted"]),
		                 (extra, 50, 0, []))
