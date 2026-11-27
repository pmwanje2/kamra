"""A bridge rebill moves the folio link; the original retains the unique ownership backlink."""
from unittest.mock import patch

import frappe

from kamra import api, folio, receivables
from kamra.tests.audit_fixtures import (
	PROP_A, TODAY, AuditTestCase, d, ensure_user, make_property, make_reservation, stocked_room_type,
)
from kamra.tests.test_fiscal_folio import FISCALISED, OwnedSalesInvoiceFixture, fiscal_reader


class RebillCreditTest(OwnedSalesInvoiceFixture, AuditTestCase):
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
		self.company = "R7 Test Company"
		make_property(PROP_A, company=self.company, currency="USD", business_date=TODAY)
		self.group = frappe.get_doc({
			"doctype": "Group Booking", "property": PROP_A, "group_name": "Rebill guests",
			"check_in_date": d(-2), "check_out_date": d(-1), "status": "Confirmed",
		}).insert(ignore_permissions=True).name
		self.stay = make_reservation(PROP_A, stocked_room_type(PROP_A), d(-2), d(-1),
		                             group_booking=self.group, status="Checked Out")
		self.folio = folio.open_folio(self.stay)
		frappe.db.set_value("Folio", self.folio, {
			"grand_total": 80, "balance": 80, "status": "Closed", "sales_invoice": "R7-LATEST",
		})
		self.invoice("R7-ROOT", kamra_ref=self.folio, docstatus=2)
		self.invoice("R7-FIRST", kamra_rebill_of="R7-ROOT", docstatus=2)
		self.invoice("R7-LATEST", kamra_rebill_of="R7-FIRST")
		self.invoice("R7-CREDIT", is_return=1, return_against="R7-LATEST", grand_total=-25)

	def invoice(self, name, **fields):
		frappe.get_doc({
			"doctype": "Sales Invoice", "name": name, "company": self.company, "currency": "USD",
			"docstatus": 1, "is_return": 0, "grand_total": 80, **fields,
		}).db_insert()

	def printed(self):
		with fiscal_reader({**FISCALISED, "sales_invoice": "R7-LATEST", "credit_notes": ["R7-CREDIT"]}):
			return api.folio_invoice(self.folio)

	def check_positions(self, credited, net):
		printed = self.printed()
		members = api.group_folios(self.group)["members"]
		rows = [printed]
		for collection in (api.billing_folios(PROP_A), api.reservation_folios(self.stay.name), members[0]["folios"]):
			rows.append(next(row for row in collection if row["name"] == self.folio))
		for row in rows:
			self.assertEqual((row["credited_amount"], row["net_collectible_balance"]), (credited, net))
			self.assertEqual(row["sales_invoice"], "R7-LATEST")
			self.assertEqual(row["credit_notes"], ["R7-CREDIT"] if credited else [])
		owing = next(row for row in receivables.guests_owing(PROP_A) if row["folio"] == self.folio)
		self.assertEqual((owing["credited_amount"], owing["net_collectible_balance"], owing["balance"]),
		                 (credited, net, net))
		self.assertEqual(printed["document"]["number"], "R7-LATEST")
		self.assertEqual(printed["tax_source"], "accounting")
		self.assertEqual(printed["fiscal"]["fdn"], FISCALISED["fdn"])

	def test_applied_and_cancelled_credit_on_replacement_across_currencies(self):
		for currency, fraction, remainder in (("UGX", 25, 55), ("USD", 25.12, 54.88), ("KWD", 25.123, 54.877)):
			make_property(PROP_A, company=self.company, currency=currency, business_date=TODAY)
			for name in ("R7-ROOT", "R7-FIRST", "R7-LATEST", "R7-CREDIT"):
				frappe.db.set_value("Sales Invoice", name, "currency", currency)
			for parent in ("R7-ROOT", "R7-FIRST"):
				frappe.db.set_value("Sales Invoice", "R7-LATEST", "kamra_rebill_of", parent)
				for posted, amount, net in ((1, 25, 55), (1, fraction, remainder), (2, 25, 80)):
					with self.subTest(currency=currency, parent=parent, posted=posted, amount=amount):
						frappe.db.set_value("Sales Invoice", "R7-CREDIT", {"docstatus": posted, "grand_total": -amount})
						self.check_positions(amount if posted == 1 else 0, net)

	def test_matching_replacement_backlink_still_requires_the_owned_root(self):
		frappe.db.set_value("Sales Invoice", "R7-LATEST", "kamra_ref", self.folio)
		self.check_positions(25, 55)

	def test_original_folio_anchor_still_resolves_submitted_replacement(self):
		frappe.db.set_value("Folio", self.folio, "sales_invoice", "R7-ROOT")
		self.check_positions(25, 55)

	def check_restricted_print(self, credited, net):
		printed = self.printed()
		self.assertEqual((printed["credited_amount"], printed["net_collectible_balance"]), (credited, net))
		self.assertEqual(printed["credit_status"], "Partially credited" if credited else None)
		self.assertIsNone(printed["sales_invoice"])
		self.assertEqual(printed["credit_notes"], [])
		self.assertEqual(printed["document"]["number"], self.folio)
		self.assertEqual(printed["fiscal"]["credit_notes"], [])
		for key in ("sales_invoice", "fdn", "verification_code", "qr", "issued", "efris_status"):
			self.assertIsNone(printed["fiscal"].get(key), key)

	def test_front_desk_sees_credit_position_without_accounting_identifiers(self):
		user = ensure_user("r7-desk@example.test", ["Front Desk"])
		frappe.set_user(user)  # nosemgrep: frappe-setuser -- verify the desk's public print projection
		self.check_restricted_print(25, 55)

	def test_document_denial_on_any_ancestor_preserves_only_the_credit_position(self):
		native = frappe.has_permission
		for denied in ("R7-ROOT", "R7-FIRST", "R7-LATEST"):
			def permitted(doctype=None, *args, doc=None, **kwargs):
				if doctype == "Sales Invoice" and getattr(doc, "name", doc) == denied:
					return False
				return native(doctype, *args, doc=doc, **kwargs)

			with self.subTest(denied=denied), patch.object(frappe, "has_permission", side_effect=permitted):
				self.check_restricted_print(25, 55)

	def test_invalid_ancestry_cannot_establish_ownership_even_with_a_backlink_on_the_tip(self):
		for name, field, invalid in (
			("R7-ROOT", "kamra_ref", None), ("R7-FIRST", "kamra_ref", "R7-FOREIGN"),
			("R7-ROOT", "company", "Other Company"), ("R7-FIRST", "company", "Other Company"),
			("R7-ROOT", "currency", "KWD"), ("R7-FIRST", "currency", "KWD"),
			("R7-FIRST", "kamra_rebill_of", "R7-LATEST"), ("R7-FIRST", "kamra_rebill_of", "R7-MISSING"),
		):
			original = frappe.db.get_value("Sales Invoice", name, field)
			frappe.db.set_value("Sales Invoice", name, field, invalid)
			for owner in (None, self.folio):
				with self.subTest(name=name, field=field, invalid=invalid, owner=owner):
					frappe.db.set_value("Sales Invoice", "R7-LATEST", "kamra_ref", owner)
					self.check_restricted_print(0, 80)
			frappe.db.set_value("Sales Invoice", name, field, original)

	def test_sixteen_rebill_edges_are_accepted_but_a_seventeenth_fails_closed(self):
		parent = "R7-ROOT"
		for index in range(15):
			name = f"R7-HOP-{index}"
			self.invoice(name, docstatus=2, kamra_rebill_of=parent)
			parent = name
		frappe.db.set_value("Sales Invoice", "R7-LATEST", "kamra_rebill_of", parent)
		self.check_positions(25, 55)
		self.invoice("R7-EXTRA", docstatus=2, kamra_rebill_of=parent)
		frappe.db.set_value("Sales Invoice", "R7-LATEST", "kamra_rebill_of", "R7-EXTRA")
		self.check_restricted_print(0, 80)
