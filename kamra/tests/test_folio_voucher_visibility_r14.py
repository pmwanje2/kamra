"""Desk voucher references require document read access. Departed debt follows checkout."""
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.core.doctype.doctype.test_doctype import new_doctype
from frappe.custom.doctype.custom_field.custom_field import create_custom_field

from kamra import api, checkout, folio, receivables
from kamra.tests.audit_fixtures import (
	PROP_A, TODAY, AuditTestCase, d, ensure_user, make_property, make_reservation, stocked_room_type,
)
from kamra.tests.test_fiscal_folio import OwnedSalesInvoiceFixture


@contextmanager
def as_user(user):
	previous = frappe.session.user
	frappe.set_user(user)  # nosemgrep: frappe-setuser -- exercise the specified test reader
	try:
		yield
	finally:
		frappe.set_user(previous)  # nosemgrep: frappe-setuser -- restore fixture user


class FolioVoucherVisibilityTest(OwnedSalesInvoiceFixture, AuditTestCase):
	@classmethod
	def setUpClass(cls):
		# Like the invoice fixture, provision the bridge schema outside the test transaction.
		outer = frappe.local.db
		frappe.connect(set_admin_as_user=False)
		cls.addClassCleanup(setattr, frappe.local, "db", outer)
		cls.addClassCleanup(frappe.local.db.close)
		if not frappe.db.exists("DocType", "Payment Entry"):
			if frappe.db.table_exists("Payment Entry"):
				raise RuntimeError("Cannot provision Payment Entry over an existing table")
			new_doctype("Payment Entry", fields=[{
				"fieldname": "kamra_payment_kind", "label": "Payment Kind", "fieldtype": "Data",
			}]).insert()
			cls.addClassCleanup(frappe.db.sql_ddl, "DROP TABLE `tabPayment Entry`")
			cls.addClassCleanup(frappe.delete_doc, "DocType", "Payment Entry", force=True)
		if not frappe.get_meta("Folio Payment").has_field("payment_entry"):
			field = create_custom_field("Folio Payment", {
				"fieldname": "payment_entry", "label": "Payment Entry",
				"fieldtype": "Link", "options": "Payment Entry", "read_only": 1,
			})
			cls.addClassCleanup(frappe.delete_doc, "Custom Field", field.name)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- make the isolated fixture schema visible to the test connection
		super().setUpClass()

	def departed(self, currency):
		make_property(PROP_A, currency=currency, business_date=TODAY)
		stay = make_reservation(PROP_A, stocked_room_type(PROP_A), d(-1), TODAY, status="Checked Out")
		return stay.name, folio.open_folio(stay)

	def test_guests_owing_matches_checkout_for_stored_fractional_debt(self):
		for currency, residual in (("UGX", "0.32"), ("USD", "0.01"), ("KWD", "0.001")):
			with self.subTest(currency=currency):
				stay, bill = self.departed(currency)
				debt = Decimal(residual)
				frappe.db.set_value("Folio", bill, {
					"grand_total": 1000 + debt, "payments_total": 1000, "balance": debt,
				})
				self.assertEqual(checkout.settlement_position(stay)["owed"], float(debt))
				rows = {r["folio"]: r for r in receivables.guests_owing(PROP_A)}
				self.assertIn(bill, rows)
				self.assertEqual(rows[bill]["net_collectible_balance"], float(debt))
				frappe.db.set_value("Folio", bill, {"balance": 0, "payments_total": 1000 + debt})
				self.assertNotIn(bill, [r["folio"] for r in receivables.guests_owing(PROP_A)])

	def test_payment_references_follow_each_vouchers_read_permission(self):
		desk = ensure_user("r14-desk@example.test", ["Front Desk"])
		for currency in ("UGX", "USD", "KWD"):
			stay, bill = self.departed(currency)
			vouchers = []
			for amount in (20, 30):
				api.add_folio_payment(bill, "Cash", amount)
				voucher = f"R14-PAY-{currency}-{amount}"
				frappe.get_doc({"doctype": "Payment Entry", "name": voucher}).db_insert()
				payment = frappe.get_doc("Folio", bill).payments[-1]
				frappe.db.set_value("Folio Payment", payment.name, "payment_entry", voucher)
				vouchers.append(voucher)
			for linked in (False, True):
				if linked:
					self.link_sales_invoice(bill, "R14-SALES-" + currency)
				for reader in (desk, "Administrator"):
					with as_user(reader):
						for endpoint, response in (("desk", api.get_folio(stay)),
						                           ("print", api.folio_invoice(bill)["folio"])):
							with self.subTest(currency=currency, linked=linked, reader=reader, endpoint=endpoint):
								self.assertEqual([p.get("payment_entry") for p in response["payments"]],
								                 vouchers if reader == "Administrator" else [None, None])
				# A reader who can read one voucher may still be denied the other.
				native = frappe.has_permission

				def permission(doctype, ptype="read", doc=None, **kwargs):
					if doctype == "Payment Entry" and getattr(doc, "name", doc) == vouchers[1]:
						return False
					return native(doctype, ptype=ptype, doc=doc, **kwargs)

				with patch.object(frappe, "has_permission", side_effect=permission):
					with self.subTest(currency=currency, linked=linked, reader="one document"):
						self.assertEqual([p.get("payment_entry") for p in api.get_folio(stay)["payments"]],
						                 [vouchers[0], None])

	def test_correction_and_reinstatement_disclose_only_a_readable_owned_invoice(self):
		desk = ensure_user("r14-desk@example.test", ["Front Desk"])
		for currency in ("UGX", "USD", "KWD"):
			stay, bill = self.departed(currency)
			voucher = "R14-CORRECTION-" + currency
			self.link_sales_invoice(bill, voucher)
			frappe.db.set_value("Reservation", stay, "check_out_business_date", TODAY)
			frappe.db.set_value("Folio", bill, "status", "Closed")
			for owned in (True, False):
				frappe.db.set_value("Sales Invoice", voucher, "kamra_ref", bill if owned else "OTHER-FOLIO")
				for reader in (desk, "Administrator"):
					with as_user(reader):
						for endpoint, refuse in (
							("correction", lambda: folio.refuse_bridge_correction(frappe.get_doc("Folio", bill))),
							("reinstatement", lambda: api.reinstate_checkout(stay, "Duplicate checkout")),
						):
							with self.subTest(currency=currency, owned=owned, reader=reader, endpoint=endpoint):
								with self.assertRaisesRegex(frappe.ValidationError, "credit note and re-bill") as error:
									refuse()
								self.assertEqual(voucher in str(error.exception), owned and reader == "Administrator")
			self.assertEqual(frappe.db.get_value("Reservation", stay, "status"), "Checked Out")
