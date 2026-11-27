"""Cashier actions use the signed position on the active accounting invoice."""
from unittest.mock import patch

import frappe

from kamra import api, checkout, folio
from kamra.tests.audit_fixtures import PROP_A, TODAY, AuditTestCase, d, make_property, make_reservation, stocked_room_type
from kamra.tests.test_fiscal_folio import OwnedSalesInvoiceFixture


class SettlementActions(OwnedSalesInvoiceFixture):
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
		self.company = "Settlement Company"
		make_property(PROP_A, company=self.company, currency=self.currency, business_date=TODAY)
		self.stay = make_reservation(PROP_A, stocked_room_type(PROP_A), d(-2), d(-1), status="Checked Out")
		self.bill = folio.open_folio(self.stay)
		frappe.db.set_value("Reservation", self.stay.name, "status", "Checked In")
		api._post_folio_charge(self.bill, "Misc", "Stay", 80)
		self.link_sales_invoice(self.bill, "ROOT")

	def invoice(self, name, total, **fields):
		frappe.get_doc({
			"doctype": "Sales Invoice", "name": name, "docstatus": 1, "company": self.company,
			"currency": self.currency, "grand_total": total, **fields,
		}).db_insert()

	def rebill(self, total, paid):
		self.invoice("REPLACEMENT", total, is_return=0, kamra_rebill_of="ROOT")
		api.add_folio_payment(self.bill, "Cash", paid)

	def check_departure(self, owed, refund):
		position = checkout.settlement_position(self.stay.name)
		with self.subTest(surface="preview"):
			self.assertEqual((position["owed"], position["refund_due"]), (owed, refund))
		# All accommodation charges in this fixture are already posted.
		with patch("kamra.folio.post_remaining_nights"), self.assertRaisesRegex(
			frappe.ValidationError, str(owed or refund)
		):
			api.check_out(self.stay.name)
		self.assertEqual(frappe.db.get_value("Reservation", self.stay.name, "status"), "Checked In")

	def test_checkout_with_zero_posted_balance(self):
		self.rebill(110, 80)
		self.check_departure(30, 0)

	def test_checkout_with_negative_posted_balance(self):
		self.rebill(110, 85)
		self.check_departure(25, 0)

	def test_checkout_with_refund_due(self):
		self.rebill(55, 70)
		self.check_departure(0, 15)

	def test_partial_refund_leaves_ten_to_return(self):
		self.rebill(55, 70)
		api.refund_folio_payment(self.bill, 5, "Cash", "Guest overpayment")
		self.assertEqual(folio.settlement_balance(frappe.get_doc("Folio", self.bill)), -10)

	def test_refund_refused_with_and_without_applied_credit(self):
		self.rebill(110, 85)
		for due, amount in ((25, 5), (21, 3)):
			if due == 21:
				self.invoice("ALLOWANCE", -4, is_return=1, return_against="REPLACEMENT")
			with self.subTest(due=due), self.assertRaisesRegex(frappe.ValidationError, str(due)):
				api.refund_folio_payment(self.bill, amount, "Cash", "Guest request")
		self.assertEqual(frappe.db.get_value("Folio", self.bill, "payments_total"), 85)

	def test_links_only_request_collectible_debt(self):
		self.rebill(110, 80)
		with (
			patch("kamra.localization.payment_gateways", return_value=["Test"]),
			patch("kamra.payments._settings", return_value=frappe._dict(test_mode=True, gateway="Test")),
			patch("kamra.localization.test_gateway_link", return_value="https://example.test/payment"),
		):
			for paid, due in ((80, 30), (85, 25)):
				if paid == 85:
					api.add_folio_payment(self.bill, "Cash", 5)
				with self.subTest(due=due):
					self.assertEqual(api.folio_payment_link(self.bill)["amount"], due)
			frappe.db.set_value("Sales Invoice", "REPLACEMENT", "grand_total", 70)
			with self.assertRaisesRegex(frappe.ValidationError, "Nothing due"):
				api.folio_payment_link(self.bill)

	def test_refund_due_cannot_generate_a_payment_link(self):
		self.rebill(55, 70)
		with patch("kamra.localization.payment_gateways", return_value=[]), self.assertRaisesRegex(
			frappe.ValidationError, "Nothing due"
		):
			api.folio_payment_link(self.bill)

	def test_interim_invoice_for_fully_paid_rebill(self):
		self.rebill(110, 110)
		result = api.part_settle_folio(self.bill)
		self.assertEqual(frappe.db.get_value("Folio", self.bill, "status"), "Closed")
		self.assertEqual(frappe.db.get_value("Folio", result["new_folio"], "status"), "Open")

	def test_foreign_link_prints_a_provisional_title(self):
		frappe.db.set_value("Folio", self.bill, {"status": "Closed", "invoice_number": "LOCAL-1"})
		for field, value, original in (
			("kamra_ref", "OTHER", self.bill), ("company", "OTHER", self.company), ("currency", "EUR", self.currency),
		):
			frappe.db.set_value("Sales Invoice", "ROOT", field, value)
			with self.subTest(field=field):
				document = api.folio_invoice(self.bill)["document"]
				self.assertFalse(document["is_final"])
				self.assertIn(document["title"], ("Provisional Bill", "Provisional Statement", "Folio Statement"))
			frappe.db.set_value("Sales Invoice", "ROOT", field, original)


class UGXSettlementTest(SettlementActions, AuditTestCase):
	currency = "UGX"


class USDSettlementTest(SettlementActions, AuditTestCase):
	currency = "USD"


class KWDSettlementTest(SettlementActions, AuditTestCase):
	currency = "KWD"
