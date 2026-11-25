"""Folio collectibility, fiscal ownership and revision history across a rebill."""
from unittest.mock import patch

import frappe

from kamra import api, folio, ledger
from kamra.tests.audit_fixtures import PROP_A, TODAY, AuditTestCase, d, make_property, make_reservation, make_room_type
from kamra.tests.test_fiscal_folio import FISCALISED, OwnedSalesInvoiceFixture, fiscal_reader


class RebillSettlementTest(OwnedSalesInvoiceFixture, AuditTestCase):
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
		self.company = "R8 Test Company"
		make_property(PROP_A, company=self.company, currency="USD", business_date=TODAY)
		self.group = frappe.get_doc({
			"doctype": "Group Booking", "property": PROP_A, "group_name": "R8 group",
			"check_in_date": TODAY, "check_out_date": d(2), "status": "Confirmed",
		}).insert(ignore_permissions=True).name
		stay = make_reservation(PROP_A, make_room_type(PROP_A), TODAY, d(2), group_booking=self.group)
		frappe.db.set_value("Reservation", stay.name, "amount_after_tax", 0)
		self.name = folio.open_folio(stay)
		api._post_folio_charge(self.name, "Misc", "Service", 80)
		self.si("R8-ROOT", kamra_ref=self.name)
		frappe.db.set_value("Folio", self.name, {"sales_invoice": "R8-ROOT", "status": "Closed"})

	def si(self, name, **fields):
		frappe.get_doc({
			"doctype": "Sales Invoice", "name": name, "company": self.company,
			"currency": "USD", "is_return": 0, "docstatus": 1, "grand_total": 80, **fields,
		}).db_insert()

	def test_repriced_bill_applies_only_active_credits_and_counts_collections_once(self):
		self.si("R8-REVERSAL", grand_total=-80, is_return=1, return_against="R8-ROOT")
		self.si("R8-LEAF", grand_total=55, kamra_rebill_of="R8-ROOT")
		self.si("R8-CREDIT", grand_total=-10, is_return=1, return_against="R8-LEAF")
		for currency in ("UGX", "USD", "KWD"):
			make_property(PROP_A, company=self.company, currency=currency)
			for name in ("R8-ROOT", "R8-REVERSAL", "R8-LEAF", "R8-CREDIT"):
				frappe.db.set_value("Sales Invoice", name, "currency", currency)
			for posted, paid, net in ((0, 0, 55), (1, 0, 45), (1, 5, 40)):
				with self.subTest(currency=currency, posted=posted, paid=paid):
					frappe.db.set_value("Folio", self.name, {"balance": 80-paid, "payments_total": paid})
					frappe.db.set_value("Sales Invoice", "R8-CREDIT", "docstatus", posted)
					row = folio.reconcile_credit_note_status(self.name)
					self.assertEqual(row["net_collectible_balance"], net)
					self.assertEqual(row["credited_amount"], 10 if posted else 0)

	def test_submitted_descendant_of_cancelled_replacement_is_collectible(self):
		self.si("R8-CANCELLED", docstatus=2, kamra_rebill_of="R8-ROOT")
		self.si("R8-LEAF", kamra_rebill_of="R8-CANCELLED")
		self.si("R8-CREDIT", is_return=1, grand_total=-25, return_against="R8-LEAF")
		row = folio.reconcile_credit_note_status(self.name)
		self.assertEqual((row["sales_invoice"], row["net_collectible_balance"]), ("R8-LEAF", 55))

	def test_print_and_billing_resolve_the_complete_path_or_neither_does(self):
		parent = "R8-ROOT"
		for depth in range(1, 18):
			name = f"R8-HOP-{depth}"
			self.si(name, kamra_rebill_of=parent)
			parent = name
			if depth in (11, 16, 17):
				with self.subTest(depth=depth):
					current = name if depth <= 16 else None
					bill = folio.reconcile_credit_note_status(self.name)
					printed = api.folio_invoice(self.name)
					self.assertEqual(bill["sales_invoice"], current)
					self.assertEqual(printed["document"]["number"], current or self.name)
					self.assertEqual(printed["tax_source"], "accounting" if current else "accounting_unavailable")

	def test_unowned_fiscal_answer_cannot_supply_outstanding_or_finality(self):
		answer = {**FISCALISED, "sales_invoice": "R8-ROOT", "outstanding_amount": 55}
		for owner, verified in ((self.name, True), ("FOREIGN", False)):
			with self.subTest(owner=owner), fiscal_reader(answer):
				frappe.db.set_value("Sales Invoice", "R8-ROOT", "kamra_ref", owner)
				native = frappe.has_permission
				with patch.object(frappe, "has_permission", side_effect=lambda dt=None, *a, **kw:
				                  False if dt == "Sales Invoice" else native(dt, *a, **kw)):
					printed = api.folio_invoice(self.name)
				self.assertEqual(printed["net_collectible_balance"], 80)
				self.assertIsNone(printed["fiscal"]["sales_invoice"])
				self.assertEqual(printed["document"]["is_final"], verified)
				self.assertEqual(printed["fiscal"]["outstanding_amount"], 55 if verified else None)

	def group_bills(self):
		self.si("R8-GUEST-CREDIT", is_return=1, grand_total=-25, return_against="R8-ROOT")
		master = folio.open_group_folio(self.group)
		api._post_folio_charge(master, "Misc", "Group service", 80)
		self.si("R8-MASTER", kamra_ref=master)
		self.si("R8-MASTER-CREDIT", is_return=1, grand_total=-25, return_against="R8-MASTER")
		frappe.db.set_value("Folio", master, "sales_invoice", "R8-MASTER")
		return master

	def test_group_control_uses_member_and_master_collectibles(self):
		self.group_bills()
		group = api.group_detail(self.group)
		self.assertEqual(group["rooming_list"][0]["own_balance"], 55)
		self.assertEqual(group["master_folio_balance"], 55)
		self.assertEqual(group["money"]["due"], 110)

	def test_master_settlement_collects_only_the_remaining_credit_adjusted_amount(self):
		master = self.group_bills()
		first = api.settle_group_bill(self.group, mode="Cash", amount=5)
		self.assertFalse(first["closed"])
		self.assertEqual(first["balance"], 50)
		final = api.settle_group_bill(self.group, mode="Cash")
		self.assertTrue(final["closed"])
		self.assertEqual(final["balance"], 0)
		self.assertEqual(frappe.db.get_value("Folio", master, ["payments_total", "balance"]), (55, 25))

	def test_reprint_keeps_its_owned_historical_reference_and_checks_current_access(self):
		with fiscal_reader({**FISCALISED, "sales_invoice": "R8-ROOT"}):
			name = ledger.record_folio_reprint(self.name)["reprint"]
		self.si("R8-LEAF", kamra_rebill_of="R8-ROOT")
		frappe.db.set_value("Folio", self.name, "sales_invoice", "R8-LEAF")
		self.assertEqual(ledger.folio_reprint_details(name)["sales_invoice"], "R8-ROOT")
		native = frappe.has_permission
		with patch.object(frappe, "has_permission", side_effect=lambda dt=None, *a, doc=None, **kw:
		                  False if dt == "Sales Invoice" and getattr(doc, "name", doc) == "R8-ROOT"
		                  else native(dt, *a, doc=doc, **kw)):
			self.assertIsNone(ledger.folio_reprint_details(name)["sales_invoice"])

