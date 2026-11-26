"""Issued charge rows are immutable and a legacy refund can finish a stay."""
import frappe

from kamra import api, checkout, folio
from kamra.tests.audit_fixtures import PROP_A, TODAY, AuditTestCase, d, make_property, make_reservation, make_room_type
from kamra.tests.test_fiscal_folio import OwnedSalesInvoiceFixture


class IssuedChargeTest(OwnedSalesInvoiceFixture, AuditTestCase):
	def test_direct_save_freezes_every_charge_and_its_position(self):
		for currency in ("UGX", "USD", "KWD"):
			make_property(PROP_A, currency=currency, business_date=TODAY)
			stay = make_reservation(PROP_A, make_room_type(PROP_A), d(-1), TODAY, status="Checked Out")
			bill = folio.open_folio(stay)
			for _ in range(2):
				api.add_folio_charge(bill, "Misc", "Same service", 80)
			self.link_sales_invoice(bill, "R12-" + currency)
			for field, value in (("description", "Rewritten"), ("gst_amount", 12), ("total", 92),
			                     ("qty", 2), ("rate", 40), ("gl_code", "DIFFERENT"),
			                     ("idx", 9), ("delete", None), ("reorder", None)):
				with self.subTest(currency=currency, field=field):
					frappe.db.savepoint("before_save")
					try:
						doc = frappe.get_doc("Folio", bill)
						if field == "delete":
							doc.remove(doc.charges[0])
						elif field == "reorder":
							doc.charges.reverse()
						else:
							doc.charges[0].set(field, value)
						with self.assertRaises(frappe.ValidationError):
							doc.save(ignore_permissions=True)
					finally:
						frappe.db.rollback(save_point="before_save")
			api.add_folio_payment(bill, "Cash", 160)
			self.assertEqual(frappe.db.get_value("Folio", bill, "balance"), 0)


class FractionalRefundTest(AuditTestCase):
	def setUp(self):
		super().setUp()
		self.at(TODAY)
		make_property(PROP_A, currency="UGX", business_date=TODAY)
		self.stay = make_reservation(PROP_A, make_room_type(PROP_A), d(-1), TODAY, status="Checked Out")
		self.bill = folio.open_folio(self.stay)
		frappe.db.set_value("Reservation", self.stay.name, "status", "Checked In")
		api.add_folio_payment(self.bill, "Cash", 1000)

	def legacy_charge(self, gross):
		doc = frappe.get_doc("Folio", self.bill)
		doc.append("charges", {"posting_date": TODAY, "charge_type": "Opening Balance", "description": "Imported stay",
		                       "qty": 1, "rate": gross, "amount": gross})
		folio._recalculate(doc)
		doc.save()

	def settle_and_check_out(self, payment_row=None):
		self.legacy_charge(999.68)
		self.assertEqual(checkout.settlement_position(self.stay.name)["refund_due"], 0.32)
		for amount in (0.31, 0.33):
			with self.assertRaises(frappe.ValidationError):
				api.refund_folio_payment(self.bill, amount, "Cash", "Departure", payment_row=payment_row)
		api.refund_folio_payment(self.bill, 0.32, "Cash", "Departure", payment_row=payment_row)
		doc = frappe.get_doc("Folio", self.bill)
		self.assertEqual(doc.payments[-1].amount, -0.32)
		self.assertEqual(doc.balance, 0)
		self.assertTrue(api.check_out(self.stay.name)["ok"])
		self.assertEqual(frappe.db.get_value("Reservation", self.stay.name, "status"), "Checked Out")

	def test_exact_fractional_credit_is_refundable(self):
		self.settle_and_check_out()

	def test_exact_fractional_credit_against_original_payment(self):
		self.settle_and_check_out(frappe.get_doc("Folio", self.bill).payments[0].name)

	def test_whole_unit_credit_rejects_fractional_refund(self):
		with self.assertRaisesRegex(frappe.ValidationError, "whole-unit"):
			api.refund_folio_payment(self.bill, 0.32, "Cash", "Departure")
		self.assertEqual(checkout.settlement_position(self.stay.name)["refund_due"], 1000)

	def test_fractional_debt_still_blocks_departure(self):
		self.legacy_charge(1000.32)
		with self.assertRaisesRegex(frappe.ValidationError, r"0\.32"):
			api.check_out(self.stay.name)

