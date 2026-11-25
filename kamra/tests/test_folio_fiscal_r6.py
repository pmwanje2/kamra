"""The printable bill and its reprint log expose statutory data only to its reader."""
from unittest.mock import patch

import frappe
import frappe.client

from kamra import api, ledger
from kamra.folio import open_folio
from kamra.tests import test_folio_credit_and_advance_r2 as r2
from kamra.tests.audit_fixtures import PROP_A, TODAY, AuditTestCase, d, ensure_user, make_property, make_reservation, make_room_type
from kamra.tests.test_fiscal_folio import FISCALISED, INVOICE, OwnedSalesInvoiceFixture, fiscal_reader


class FiscalReprintR6Test(OwnedSalesInvoiceFixture, AuditTestCase):
	_erp = r2.FolioCreditAndAdvanceR2Test._erp
	_invoice = r2.FolioCreditAndAdvanceR2Test._invoice

	def setUp(self):
		super().setUp()
		make_property(PROP_A, company=r2.COMPANY, business_date=TODAY)
		res = make_reservation(PROP_A, make_room_type(PROP_A), TODAY, d(2))
		self.folio = open_folio(res)
		self.internal = "INV-LOCAL-R6"
		frappe.db.set_value("Folio", self.folio, {
			"sales_invoice": INVOICE, "invoice_number": self.internal, "status": "Closed",
		})
		self.finance = ensure_user("fiscal-r6-finance@example.test", ["Finance"])
		self.desk = ensure_user("fiscal-r6-desk@example.test", ["Front Desk"])

	def native_read(self, native):
		def permitted(doctype=None, *args, doc=None, **kwargs):
			if doctype == "Sales Invoice" and doc is not None:
				return frappe.session.user == self.finance
			return native(doctype, *args, doc=doc, **kwargs)
		return permitted

	def test_printable_fiscal_identifiers_require_a_readable_matching_invoice(self):
		for user, owner, bridge_invoice, visible in (
			(self.finance, self.folio, INVOICE, True),
			(self.desk, self.folio, INVOICE, False),
			(self.finance, self.folio, "SI-ANOTHER", False),
			(self.finance, "ANOTHER-FOLIO", INVOICE, False),
		):
			with self.subTest(user=user, owner=owner, bridge_invoice=bridge_invoice):
				frappe.set_user(user)  # nosemgrep: frappe-setuser -- exercise each role's print response
				with self._erp([self._invoice(INVOICE, owner)]), fiscal_reader({
					**FISCALISED, "sales_invoice": bridge_invoice,
				}), patch.object(frappe, "has_permission", side_effect=self.native_read(frappe.has_permission)):
					printed = api.folio_invoice(self.folio)
				fiscal = printed["fiscal"]
				self.assertEqual(fiscal["sales_invoice"], INVOICE if visible else None)
				for key in ("fdn", "verification_code", "qr", "issued", "efris_status", "efris_enrolled"):
					self.assertEqual(fiscal.get(key), FISCALISED[key] if visible else None, key)
				self.assertEqual(printed["document"]["internal_number"], self.internal)

	def test_unowned_bridge_reference_is_never_persisted_by_either_reprint_writer(self):
		with self._erp([self._invoice(INVOICE, "ANOTHER-FOLIO")]), fiscal_reader(FISCALISED):
			single = ledger.record_folio_reprint(self.folio)["reprint"]
			batch = ledger.batch_print_folios([self.folio])["reprints"][0]["reprint"]
			with self.assertRaises(frappe.ValidationError):
				api.reprint_fiscal_folio(self.folio)
		owned_invoice = "R6-OWNED-REPRINT"
		self.link_sales_invoice(self.folio, owned_invoice)
		with fiscal_reader({**FISCALISED, "sales_invoice": owned_invoice}):
			printed = api.reprint_fiscal_folio(self.folio)["reprint"]
		self.assertEqual(frappe.db.get_value("Folio Reprint", printed, "invoice_number"), owned_invoice)
		for name in (single, batch, printed):
			with self.subTest(name=name):
				self.assertNotEqual(frappe.db.get_value("Folio Reprint", name, "invoice_number"), INVOICE)

	def test_reader_of_finance_reprint_is_checked_on_each_read(self):
		frappe.set_user(self.finance)  # nosemgrep: frappe-setuser -- authorized issuer
		with self._erp([self._invoice(INVOICE, self.folio)]), fiscal_reader(FISCALISED), \
				patch.object(frappe, "has_permission", side_effect=self.native_read(frappe.has_permission)):
			record = ledger.record_folio_reprint(self.folio)["reprint"]
			self.assertEqual(api.folio_invoice(self.folio)["document"]["number"], INVOICE)
			self.assertEqual(ledger.folio_history(PROP_A, query=INVOICE)["folios"][0]["sales_invoice"], INVOICE)
			frappe.set_user(self.desk)  # nosemgrep: frappe-setuser -- read the same saved revision as Front Desk
			doc = frappe.client.get("Folio Reprint", record)
			listed = frappe.client.get_list("Folio Reprint", filters={"name": record}, fields=["*"])[0]
			history = ledger.folio_history(PROP_A, query=self.internal)["folios"][0]
			for surface, row in (("document", doc), ("list", listed), ("history", history)):
				with self.subTest(surface=surface):
					self.assertIn(self.internal, row.values())
					self.assertNotIn(INVOICE, row.values())
			self.assertEqual(ledger.folio_history(PROP_A, query=INVOICE)["folios"], [])

	def test_snapshot_api_checks_the_current_reader_of_the_saved_revision(self):
		frappe.set_user(self.finance)  # nosemgrep: frappe-setuser -- record under an authorized Finance user
		with self._erp([self._invoice(INVOICE, self.folio)]), fiscal_reader(FISCALISED), \
				patch.object(frappe, "has_permission", side_effect=self.native_read(frappe.has_permission)):
			name = api.reprint_fiscal_folio(self.folio)["reprint"]
			reader = getattr(ledger, "folio_reprint_details", None)
			self.assertTrue(callable(reader), "A saved revision needs a permission-checked read")
			self.assertEqual(reader(name)["sales_invoice"], INVOICE)
			frappe.set_user(self.desk)  # nosemgrep: frappe-setuser -- the creator's access must not carry over
			details = reader(name)
			self.assertEqual(details["internal_number"], self.internal)
			self.assertIsNone(details["sales_invoice"])
			self.assertNotIn(INVOICE, details.values())
