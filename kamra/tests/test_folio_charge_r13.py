"""Exercise native child lifecycle hooks as well as ordinary folio posting."""
import frappe

from kamra import api, folio
from kamra.tests.audit_fixtures import PROP_A, TODAY, AuditTestCase, d, make_property, make_reservation, make_room_type
from kamra.tests.test_fiscal_folio import OwnedSalesInvoiceFixture


class DirectChargeTest(OwnedSalesInvoiceFixture, AuditTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.get_meta("Sales Invoice").has_field("kamra_rebill_of"):
			from frappe.custom.doctype.custom_field.custom_field import create_custom_field
			field = create_custom_field("Sales Invoice", {
				"fieldname": "kamra_rebill_of", "label": "Rebill of", "fieldtype": "Data",
			})
			cls.addClassCleanup(frappe.delete_doc, "Custom Field", field.name, force=True)

	def open_bill(self):
		stay = make_reservation(PROP_A, make_room_type(PROP_A), d(-1), TODAY, status="Checked Out")
		return folio.open_folio(stay)

	def test_issued_parent_protects_direct_child_operations(self):
		for currency in ("UGX", "USD", "KWD"):
			make_property(PROP_A, currency=currency, business_date=TODAY)
			bill, other = self.open_bill(), self.open_bill()
			api.add_folio_charge(bill, "Misc", "Issued service", 80)
			charge = frappe.get_doc("Folio", bill).charges[0].name
			self.link_sales_invoice(bill, "SI-13-" + currency)
			for action in ("update", "delete", "insert", "move"):
				with self.subTest(currency=currency, action=action):
					frappe.db.savepoint("direct_charge")
					try:
						with self.assertRaisesRegex(frappe.ValidationError, "invoiced|frozen"):
							row = frappe.get_doc("Folio Charge", charge)
							if action == "update":
								row.description = "Changed"
								row.save()
							elif action == "delete":
								frappe.delete_doc("Folio Charge", charge)
							elif action == "insert":
								frappe.get_doc({
									"doctype": "Folio Charge", "parent": bill, "parenttype": "Folio",
									"parentfield": "charges", "posting_date": TODAY, "charge_type": "Misc",
									"description": "Extra charge", "amount": 12,
								}).insert()
							else:
								row.parent = other
								row.save()
						rows = frappe.get_doc("Folio", bill).charges
						self.assertEqual([(r.name, r.description, r.amount) for r in rows],
						                 [(charge, "Issued service", 80)])
					finally:
						frappe.db.rollback(save_point="direct_charge")

	def test_open_posting_and_rebill_credit_preserve_the_source_charge(self):
		for currency in ("UGX", "USD", "KWD"):
			with self.subTest(currency=currency):
				make_property(PROP_A, company="R13 Company", currency=currency, business_date=TODAY)
				bill = self.open_bill()
				api.add_folio_charge(bill, "Misc", "Draft", 80)
				doc = frappe.get_doc("Folio", bill)
				doc.charges[0].description = "Confirmed"
				doc.save()
				root, replacement = "ROOT-13-" + currency, "REBILL-13-" + currency
				self.link_sales_invoice(bill, root)
				frappe.db.set_value("Sales Invoice", root, "docstatus", 2)
				for name, fields in ((replacement, {"kamra_rebill_of": root, "grand_total": 80}),
				                     ("CREDIT-13-" + currency, {"is_return": 1, "return_against": replacement,
				                                              "grand_total": -25})):
					frappe.get_doc({"doctype": "Sales Invoice", "name": name, "company": "R13 Company",
					                "currency": currency, "docstatus": 1, "is_return": 0, **fields}).db_insert()
				frappe.db.set_value("Folio", bill, "sales_invoice", replacement)
				position = folio.reconcile_credit_note_status(bill)
				self.assertEqual((position["credited_amount"], position["net_collectible_balance"]), (25, 55))
				self.assertEqual(frappe.get_doc("Folio", bill).charges[0].description, "Confirmed")
