"""A submitted invoice freezes its charge rows; stored fractional debt still blocks departure."""
from unittest.mock import patch

import frappe

from kamra import api, checkout, folio
from kamra.tests.audit_fixtures import PROP_A, TODAY, AuditTestCase, d, make_property, make_reservation, make_room_type
from kamra.tests.test_fiscal_folio import OwnedSalesInvoiceFixture


class FrozenCharges(OwnedSalesInvoiceFixture):
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
		make_property(PROP_A, currency=self.currency, company="R11 Company", business_date=TODAY)
		self.stay = make_reservation(PROP_A, make_room_type(PROP_A), d(-2), d(-1), status="Checked Out")
		self.bill = folio.open_folio(self.stay)
		frappe.db.set_value("Reservation", self.stay.name, "status", "Checked In")
		api.add_folio_charge(self.bill, "Misc", "Original stay", 80)

	def test_rebill_refuses_extra_charge_and_new_window_blocks_checkout(self):
		self.link_sales_invoice(self.bill, "R11-ROOT")
		frappe.get_doc({"doctype": "Sales Invoice", "name": "R11-LEAF", "docstatus": 1, "is_return": 0,
		                "company": "R11 Company", "currency": self.currency, "grand_total": 110,
		                "kamra_rebill_of": "R11-ROOT"}).db_insert()
		api.add_folio_payment(self.bill, "Cash", 110)
		with self.assertRaises(frappe.ValidationError):
			api.add_folio_charge(self.bill, "Misc", "Late service", 12)
		self.assertFalse(frappe.db.exists("Folio Charge", {"parent": self.bill, "description": "Late service"}))
		self.assertEqual(checkout.settlement_position(self.stay.name)["owed"], 0)
		window = folio.open_window(reservation=self.stay.name)
		api.add_folio_charge(window, "Misc", "Late service", 12)
		with patch("kamra.folio.post_remaining_nights"), self.assertRaisesRegex(frappe.ValidationError, "12"):
			api.check_out(self.stay.name)
		self.assertEqual(frappe.db.get_value("Reservation", self.stay.name, "status"), "Checked In")

	def test_sibling_writer_cannot_append_to_an_issued_invoice(self):
		self.link_sales_invoice(self.bill, "R11-ROOT")
		doc = frappe.get_doc("Folio", self.bill)
		doc.append("charges", {"posting_date": TODAY, "charge_type": "Misc", "description": "Night fee",
		                       "qty": 1, "rate": 12, "amount": 12})
		with self.assertRaises(frappe.ValidationError):
			folio._recalculate(doc)
			doc.save(ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Folio Charge", {"parent": self.bill, "description": "Night fee"}))


class UGXTest(FrozenCharges, AuditTestCase):
	currency = "UGX"

	def test_legacy_fraction_blocks_checkout_and_close(self):
		api.add_folio_payment(self.bill, "Cash", 1000)
		row = frappe.get_doc("Folio", self.bill).charges[0].name
		frappe.db.set_value("Folio Charge", row, {"amount": 1000.32, "rate": 1000.32, "total": 1000.32})
		frappe.db.set_value("Folio", self.bill, {"charges_total": 1000.32, "grand_total": 1000.32, "balance": 0.32})
		self.assertEqual(checkout.settlement_position(self.stay.name)["owed"], 0.32)
		with self.assertRaises(frappe.ValidationError):
			folio.close_folio(self.bill)
		with patch("kamra.folio.post_remaining_nights"), self.assertRaisesRegex(frappe.ValidationError, r"0\.32"):
			api.check_out(self.stay.name)
		self.assertEqual(frappe.db.get_value("Reservation", self.stay.name, "status"), "Checked In")


class USDTest(FrozenCharges, AuditTestCase):
	currency = "USD"


class KWDTest(FrozenCharges, AuditTestCase):
	currency = "KWD"

