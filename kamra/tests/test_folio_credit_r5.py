"""Public folio projections share ownership, native visibility and collectible balances."""
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

import frappe

from kamra import api, payment_transfers
from kamra.tests import test_folio_credit_and_advance_r2 as r2
from kamra.tests.audit_fixtures import PROP_A, TODAY, AuditTestCase, d, make_property, make_reservation, make_room_type
from kamra.tests.bridge_fields import ensure_bridge_field
from kamra.tests.test_fiscal_folio import FISCALISED, fiscal_reader
from kamra.folio import open_folio

CURRENCIES = (("UGX", 25), ("USD", 25.12), ("KWD", 25.123))


class FolioCreditR5Test(AuditTestCase):
	_erp = r2.FolioCreditAndAdvanceR2Test._erp
	_invoice = r2.FolioCreditAndAdvanceR2Test._invoice

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		with patch.object(frappe.local, "db", frappe.local.db):
			frappe.connect(set_admin_as_user=False)
			try:
				ensure_bridge_field("Folio", "sales_invoice")
				ensure_bridge_field("Property", "company")
			finally:
				frappe.db.close()

	def setUp(self):
		super().setUp()
		make_property(PROP_A, company=r2.COMPANY, business_date=TODAY)
		self.group = frappe.get_doc({
			"doctype": "Group Booking", "group_name": "Credit projection", "property": PROP_A,
			"check_in_date": TODAY, "check_out_date": d(2), "status": "Confirmed",
		}).insert(ignore_permissions=True).name
		self.stay = make_reservation(PROP_A, make_room_type(PROP_A), TODAY, d(2), group_booking=self.group)
		self.folio = open_folio(self.stay)
		frappe.db.set_value("Folio", self.folio, {
			"sales_invoice": "SI-OWNED", "status": "Closed", "balance": 100, "grand_total": 100,
		})

	@contextmanager
	def accounting(self, currency, amount, *, owner=None, note_owner=None, denied=()):
		make_property(PROP_A, currency=currency, company=r2.COMPANY, business_date=TODAY)
		invoice = self._invoice("SI-OWNED", owner or self.folio, currency=currency, efris_irn="issued")
		note = self._invoice("CN-APPLIED", note_owner, currency=currency,
		                     is_return=1, return_against=invoice.name, grand_total=-amount)
		with self._erp([invoice, note]):
			native = frappe.has_permission

			def permitted(doctype=None, *args, doc=None, **kwargs):
				if doctype == "Sales Invoice" and doc is not None:
					return (doc if isinstance(doc, str) else doc.name) not in denied
				return native(doctype, *args, doc=doc, **kwargs)

			with patch.object(frappe, "has_permission", side_effect=permitted):
				yield

	def test_printable_references_obey_ownership_and_each_document_read(self):
		note = {"name": "CN-APPLIED", "grand_total": -25}
		for currency, amount in CURRENCIES:
			for owner, denied, shown_invoice, notes in (
				(self.folio, (), "SI-OWNED", [note]),
				(self.folio, ("CN-APPLIED",), "SI-OWNED", []),
				(self.folio, ("SI-OWNED",), None, []),
				("FOREIGN-FOLIO", (), None, []),
			):
				with self.subTest(currency=currency, owner=owner, denied=denied), \
						self.accounting(currency, amount, owner=owner, denied=denied), fiscal_reader({
							**FISCALISED, "sales_invoice": "SI-OWNED", "replaced_by": "SI-FOREIGN",
							"credit_notes": [note, {"name": "CN-FOREIGN"}], "outstanding_amount": 75,
						}):
					data = api.folio_invoice(self.folio)
					self.assertEqual(data["document"]["number"], shown_invoice or self.folio)
					self.assertEqual(data["fiscal"]["sales_invoice"], shown_invoice)
					self.assertEqual(data["fiscal"]["credit_notes"], notes)
					self.assertIsNone(data["fiscal"]["replaced_by"])
					self.assertEqual(data["fiscal"]["outstanding_amount"], 75)

	def test_window_fiscal_lookup_only_queries_verified_visible_invoices(self):
		for currency, amount in CURRENCIES:
			for owner, denied, visible in ((self.folio, (), True), (self.folio, ("SI-OWNED",), False),
			                              ("FOREIGN-FOLIO", (), False)):
				with self.subTest(currency=currency, owner=owner, denied=denied), \
						self.accounting(currency, amount, owner=owner, denied=denied):
					get_meta, get_all = frappe.get_meta, frappe.get_all
					meta = frappe._dict(has_field=lambda key: key == "efris_irn")
					with patch.object(frappe, "get_meta", side_effect=lambda dt, *a, **kw:
					                  meta if dt == "Sales Invoice" else get_meta(dt, *a, **kw)), \
							patch.object(frappe, "get_all", wraps=get_all) as reads:
						data = payment_transfers.folio_windows_overview(self.folio)
					queries = [c.kwargs["filters"]["name"][1] for c in reads.call_args_list
					           if c.args[0] == "Sales Invoice" and "efris_irn" in c.kwargs.get("fields", [])]
					window = next(w for w in data["windows"] if w["folio"] == self.folio)
					self.assertEqual(queries, [["SI-OWNED"]] if visible else [])
					self.assertEqual(window["sales_invoice"], "SI-OWNED" if visible else None)
					self.assertEqual(window["efris_fiscalized"], visible)

	def test_conflicting_return_backlink_cannot_credit_another_folio(self):
		for currency, amount in CURRENCIES:
			with self.subTest(currency=currency), self.accounting(currency, amount, note_owner="FOREIGN-FOLIO"):
				row = next(r for r in api.billing_folios(PROP_A) if r["name"] == self.folio)
				self.assertEqual(row["credited_amount"], 0)
				self.assertEqual(row["net_collectible_balance"], 100)
				self.assertEqual(row["credit_notes"], [])
				self.assertIsNone(row["credit_status"])

	def test_reservation_and_group_keep_the_verified_credit_position(self):
		for currency, amount in CURRENCIES:
			for denied in ((), ("CN-APPLIED",)):
				with self.subTest(currency=currency, denied=denied), self.accounting(currency, amount, denied=denied):
					for rows in (api.reservation_folios(self.stay.name), api.group_folios(self.group)["members"][0]["folios"]):
						row = next(r for r in rows if r["name"] == self.folio)
						self.assertEqual(row["balance"], 100)
						self.assertEqual(row.get("credited_amount"), amount)
						self.assertEqual(row.get("net_collectible_balance"), float(100 - Decimal(str(amount))))
						self.assertEqual(row.get("credit_status"), "Partially credited")
						self.assertEqual(row.get("credit_notes"), [] if denied else ["CN-APPLIED"])
