"""R3 accounting projection regressions on a hotel-only Bench site."""
from unittest.mock import patch

import frappe

from kamra import api, folio, receivables
from kamra.tests import test_folio_credit_and_advance_r2 as r2
from kamra.tests.audit_fixtures import PROP_A, TODAY, AuditTestCase, make_property
from kamra.tests.bridge_fields import ensure_bridge_field


class FolioCreditR3Test(AuditTestCase):
	_erp = r2.FolioCreditAndAdvanceR2Test._erp
	_invoice = r2.FolioCreditAndAdvanceR2Test._invoice
	_departed_folio = r2.FolioCreditAndAdvanceR2Test._departed_folio

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_bridge_field("Folio", "sales_invoice")
		ensure_bridge_field("Property", "company")

	def setUp(self):
		super().setUp()
		make_property(PROP_A, company=r2.COMPANY, business_date=TODAY)
		self.name = self._departed_folio(PROP_A, "SI-ROOT")

	def bill(self):
		return next(row for row in api.billing_folios(PROP_A) if row["name"] == self.name)

	def test_credit_on_active_rebill_chain(self):
		for currency, amount in (("UGX", 410619), ("USD", 11.12), ("KWD", 11.123)):
			with self.subTest(currency=currency):
				make_property(PROP_A, currency=currency, company=r2.COMPANY)
				frappe.db.set_value("Folio", self.name, {"balance": amount, "grand_total": amount})
				def invoice(name, ref=None, **kw):
					return self._invoice(name, ref, currency=currency, **kw)
				root = invoice("SI-ROOT", self.name, docstatus=2, kamra_rebill_of=None)
				interim = invoice("SI-INTERIM", docstatus=2, kamra_rebill_of=root.name)
				active = invoice("SI-ACTIVE", kamra_rebill_of=interim.name, grand_total=amount)
				note = invoice("CN-ACTIVE", is_return=1, return_against=active.name, grand_total=-amount)
				foreign = invoice("SI-FOREIGN", "ANOTHER-FOLIO", kamra_rebill_of=root.name)
				with self._erp([root, foreign, interim, active, note]):
					row = self.bill()
					self.assertEqual(row["net_collectible_balance"], 0)
					self.assertEqual(row["sales_invoice"], active.name)
					self.assertEqual(row["credit_notes"], [note.name])
					self.assertNotIn(self.name, [r["folio"] for r in receivables.guests_owing(PROP_A)])

	def test_native_document_hook_can_deny_listed_note_or_invoice(self):
		invoice = self._invoice("SI-ROOT", self.name)
		notes = [self._invoice(name, self.name, is_return=1, return_against=invoice.name, grand_total=-10)
		         for name in ("CN-ONE", "CN-TWO")]
		with self._erp([invoice, *notes]):
			native = frappe.has_permission
			for denied in ("CN-TWO", "SI-ROOT"):
				with self.subTest(denied=denied):
					def permitted(doctype=None, *args, doc=None, **kwargs):
						if doctype == "Sales Invoice" and doc is not None:
							return getattr(doc, "name", doc) != denied
						return native(doctype, *args, doc=doc, **kwargs)
					with patch.object(frappe, "has_permission", side_effect=permitted):
						row = self.bill()
					self.assertEqual(row["net_collectible_balance"], 80)
					self.assertEqual(row["credit_notes"], ["CN-ONE"] if denied == "CN-TWO" else [])
					self.assertEqual(row["sales_invoice"], invoice.name if denied == "CN-TWO" else None)

	def test_foreign_invoice_reference_is_suppressed_even_without_returns(self):
		with self._erp([self._invoice("SI-ROOT", "ANOTHER-FOLIO")]):
			row = self.bill()
		self.assertIsNone(row["sales_invoice"])
		self.assertEqual(row["net_collectible_balance"], 100)

	def test_missing_invoice_metadata_preserves_printable_folio_balance(self):
		exists = frappe.db.exists
		query = frappe.get_all

		def missing_metadata(doctype, *args, **kwargs):
			if doctype == "Sales Invoice":
				raise frappe.DoesNotExistError("Sales Invoice metadata unavailable")
			return query(doctype, *args, **kwargs)

		with (
			patch.object(frappe.db, "exists", side_effect=lambda dt, name=None, *a, **kw:
			             True if (dt, name) == ("DocType", "Sales Invoice") else exists(dt, name, *a, **kw)),
			patch.object(frappe, "get_all", side_effect=missing_metadata),
		):
			position = folio.reconcile_credit_note_status(self.name)
		self.assertEqual(position["net_collectible_balance"], 100)
		self.assertEqual(position["credit_notes"], [])
