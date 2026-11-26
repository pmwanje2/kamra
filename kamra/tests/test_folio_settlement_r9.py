"""A rebill keeps the guest's signed settlement position at every entry point."""
import frappe

from kamra import api, dashboards, folio, ledger, receivables
from kamra.tests.audit_fixtures import PROP_A, TODAY, AuditTestCase, d, make_property, make_reservation, make_room_type
from kamra.tests.test_fiscal_folio import FISCALISED, OwnedSalesInvoiceFixture, fiscal_reader
from kamra.tests.test_group_billing import _corp


class RebillCases(OwnedSalesInvoiceFixture):
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
		self.company = "R9 Test Company"
		make_property(PROP_A, company=self.company, currency=self.currency, business_date=TODAY)
		stay = make_reservation(PROP_A, make_room_type(PROP_A), d(-2), d(-1), status="Checked Out")
		self.bill = folio.open_folio(stay)
		api._post_folio_charge(self.bill, "Misc", "Service", 80)
		self.link_sales_invoice(self.bill, "R9-ROOT")

	def rebill(self, total, collected):
		api.add_folio_payment(self.bill, "Cash", collected)
		frappe.get_doc({
			"doctype": "Sales Invoice", "name": "R9-LEAF", "docstatus": 1, "is_return": 0,
			"company": self.company, "currency": self.currency, "kamra_rebill_of": "R9-ROOT",
			"grand_total": total,
		}).db_insert()

	def test_local_number_cannot_make_an_unverified_invoice_final_or_reprintable(self):
		frappe.db.set_value("Folio", self.bill, {"status": "Closed", "invoice_number": "LOCAL-1"})
		for field, value, original in (
			("kamra_ref", "ANOTHER-FOLIO", self.bill), ("company", "ANOTHER-COMPANY", self.company),
			("currency", "EUR", self.currency),
		):
			frappe.db.set_value("Sales Invoice", "R9-ROOT", field, value)
			with fiscal_reader(None):
				with self.subTest(field=field, surface="print"):
					printed = api.folio_invoice(self.bill)
					self.assertEqual(printed["tax_source"], "accounting_unavailable")
					self.assertFalse(printed["document"]["is_final"])
				with self.subTest(field=field, surface="reprint"), self.assertRaises(frappe.ValidationError):
					api.reprint_fiscal_folio(self.bill)
			frappe.db.set_value("Sales Invoice", "R9-ROOT", field, original)
		self.assertEqual(frappe.db.count("Folio Reprint", {"folio": self.bill}), 0)
		with fiscal_reader({**FISCALISED, "sales_invoice": "R9-ROOT"}):
			self.assertEqual(api.reprint_fiscal_folio(self.bill)["document"]["number"], "R9-ROOT")
		frappe.db.set_value("Folio", self.bill, "sales_invoice", None)
		self.assertEqual(api.reprint_fiscal_folio(self.bill)["document"]["number"], "LOCAL-1")

	def test_lower_rebill_retains_refund_due_and_prevents_close(self):
		self.rebill(55, 70)
		with self.subTest(surface="settlement"):
			self.assertEqual(folio.settlement_balance(frappe.get_doc("Folio", self.bill)), -15)
		with self.assertRaisesRegex(frappe.ValidationError, "owed back"):
			folio.close_folio(self.bill)
		self.assertEqual(frappe.db.get_value("Folio", self.bill, "status"), "Open")

	def test_higher_rebill_reaches_owing_dashboard_and_settlement(self):
		self.rebill(110, 80)
		for net in (30, 25):
			if net == 25:
				api.add_folio_payment(self.bill, "Cash", 5)
			with self.subTest(net=net, surface="billing"):
				row = next(r for r in api.billing_folios(PROP_A) if r["name"] == self.bill)
				self.assertEqual(row["net_collectible_balance"], net)
			with self.subTest(net=net, surface="settlement"):
				self.assertEqual(folio.settlement_balance(frappe.get_doc("Folio", self.bill)), net)
			with self.subTest(net=net, surface="guests owing"):
				self.assertEqual({r["folio"]: r["balance"] for r in receivables.guests_owing(PROP_A)}.get(self.bill), net)
			with self.subTest(net=net, surface="dashboard"):
				self.assertEqual(dashboards._open_folio_totals(PROP_A), (net, 1))

	def test_city_ledger_collects_the_repriced_debit_once(self):
		self.rebill(110, 80)
		company = _corp()
		ledger.transfer_to_city_ledger(self.bill, company)
		self.assertEqual(frappe.db.get_value("City Ledger Entry", {"folio": self.bill}, "debit"), 30)
		self.assertEqual(folio.settlement_balance(frappe.get_doc("Folio", self.bill)), 0)
		with self.assertRaisesRegex(frappe.ValidationError, "No balance"):
			ledger.transfer_to_city_ledger(self.bill, company)


class UGXRebillTest(RebillCases, AuditTestCase):
	currency = "UGX"


class USDRebillTest(RebillCases, AuditTestCase):
	currency = "USD"


class KWDRebillTest(RebillCases, AuditTestCase):
	currency = "KWD"
