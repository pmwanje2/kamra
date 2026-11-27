"""R2 coverage for applied receivables, property ownership and advance replay."""
from contextlib import contextmanager
from unittest.mock import patch

import frappe

from kamra import api, folio, receivables, settings
from kamra.ledger import transfer_advances_to_guest
from kamra.patches.v48.reconcile_refunded_advances import execute
from kamra.tests.audit_fixtures import (
	PROP_A, PROP_B, TODAY, AuditTestCase, d, make_property, make_reservation, stocked_room_type,
)
from kamra.tests.bridge_fields import ensure_bridge_field

COMPANY = "Shared Accounting Company"


class FolioCreditAndAdvanceR2Test(AuditTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_bridge_field("Folio", "sales_invoice")
		ensure_bridge_field("Property", "company")

	def setUp(self):
		super().setUp()
		for prop in (PROP_A, PROP_B):
			make_property(prop, company=COMPANY, business_date=TODAY)
		self.room_type = stocked_room_type(PROP_A)

	def _departed_folio(self, property, invoice):
		res = make_reservation(property, stocked_room_type(property), d(-2), d(-1), status="Checked Out")
		name = folio.open_folio(res)
		frappe.db.set_value("Folio", name, {
			"sales_invoice": invoice, "status": "Closed", "grand_total": 100, "balance": 100,
		})
		return name

	def _invoice(self, name, folio_name, **details):
		return frappe._dict({
			"name": name, "kamra_ref": folio_name, "company": COMPANY, "currency": "INR",
			"docstatus": 1, "is_return": 0, "return_against": None, "update_outstanding_for_self": 0,
			"grand_total": 100, "rounded_total": 0, "outstanding_amount": 100, **details,
		})

	@contextmanager
	def _erp(self, invoices, *, can_read=True, hidden=()):
		get_all, get_list, has_permission = frappe.get_all, frappe.get_list, frappe.has_permission
		exists, has_column = frappe.db.exists, frappe.db.has_column

		def query(doctype, *args, **kwargs):
			if doctype != "Sales Invoice":
				return get_all(doctype, *args, **kwargs)
			out = []
			for row in invoices:
				for field, value in kwargs["filters"].items():
					if isinstance(value, (list, tuple)):
						if value[0] == "in" and row.get(field) in value[1]:
							continue
						if value == ["is", "not set"] and not row.get(field):
							continue
						break
					if row.get(field) != value:
						break
				else:
					out.append(row)
			if kwargs.get("pluck"):
				return [r.get(kwargs["pluck"]) for r in out]
			return out

		def scoped_query(doctype, *args, **kwargs):
			if doctype != "Sales Invoice":
				return get_list(doctype, *args, **kwargs)
			if not can_read:
				raise frappe.PermissionError
			matches = query(doctype, *args, **kwargs)
			return [item for item in matches if (item if isinstance(item, str) else item.name) not in hidden]

		def permitted(doctype=None, *args, **kwargs):
			if doctype == "Sales Invoice":
				return can_read
			return has_permission(doctype, *args, **kwargs)

		def doc_exists(doctype, name=None, *args, **kwargs):
			return True if (doctype, name) == ("DocType", "Sales Invoice") else exists(doctype, name, *args, **kwargs)

		def column_exists(doctype, field):
			return field in invoices[0] if doctype == "Sales Invoice" else has_column(doctype, field)

		with (
			patch.object(frappe, "get_all", side_effect=query),
			patch.object(frappe, "get_list", side_effect=scoped_query),
			patch.object(frappe, "has_permission", side_effect=permitted),
			patch.object(frappe.db, "exists", side_effect=doc_exists),
			patch.object(frappe.db, "has_column", side_effect=column_exists),
		):
			yield

	def _owing(self):
		return {row["folio"]: row["balance"] for row in receivables.guests_owing(PROP_A)}

	def test_unapplied_return_keeps_debt_and_applied_return_reduces_it(self):
		name = self._departed_folio(PROP_A, "SI-ORIGINAL")
		original = self._invoice("SI-ORIGINAL", name)
		unapplied = self._invoice("CN-SELF", name, is_return=1, return_against=original.name,
		                         grand_total=-100, update_outstanding_for_self=1)
		with self._erp([original, unapplied]):
			self.assertEqual(self._owing().get(name), 100)
			position = folio.reconcile_credit_note_status(name)
			self.assertEqual(position["credited_amount"], 0)
			self.assertEqual(position["credit_notes"], ["CN-SELF"])
			self.assertIsNone(position["credit_status"])
		applied = self._invoice("CN-APPLIED", name, is_return=1, return_against=original.name, grand_total=-25)
		original.outstanding_amount = 75
		with self._erp([original, unapplied, applied]):
			self.assertEqual(self._owing().get(name), 75)
			self.assertEqual(folio.reconcile_credit_note_status(name)["credit_status"], "Partially credited")

	def test_shared_company_cannot_credit_a_folio_with_a_foreign_invoice_link(self):
		foreign = self._departed_folio(PROP_B, "SI-FOREIGN-OWNED")
		local = self._departed_folio(PROP_A, "SI-WRONG-LINK")
		original = self._invoice("SI-WRONG-LINK", foreign, outstanding_amount=0)
		note = self._invoice("CN-FOREIGN", foreign, is_return=1, return_against=original.name, grand_total=-100)
		with self._erp([original, note]):
			bill = next(row for row in api.billing_folios(PROP_A) if row["name"] == local)
			self.assertEqual(bill["credit_notes"], [])
			self.assertEqual(bill["net_collectible_balance"], 100)
			self.assertEqual(self._owing().get(local), 100)

	def test_folio_summary_survives_native_permission_denial_but_credit_numbers_do_not(self):
		name = self._departed_folio(PROP_A, "SI-PERMITTED")
		original = self._invoice("SI-PERMITTED", name, outstanding_amount=75)
		visible = self._invoice("CN-VISIBLE", name, is_return=1, return_against=original.name, grand_total=-15)
		hidden = self._invoice("CN-RESTRICTED", name, is_return=1, return_against=original.name, grand_total=-10)
		with self._erp([original, visible, hidden], can_read=False):
			row = next(item for item in api.billing_folios(PROP_A) if item["name"] == name)
			self.assertEqual(row["net_collectible_balance"], 75)
			self.assertEqual(row["credit_notes"], [])
			self.assertNotIn("outstanding_amount", row)
		with self._erp([original, visible, hidden], hidden=[hidden.name]):
			row = next(item for item in api.billing_folios(PROP_A) if item["name"] == name)
			self.assertEqual(row["credit_notes"], [visible.name])

	def test_migration_attributes_refunded_advances_with_other_payments_and_replays(self):
		# Two simultaneous stays in each of the three currency cases.
		stocked_room_type(PROP_A, inventory=6)
		settings.write_kv(PROP_A, "use_cashier_tills", 0)
		settings.clear_cache(PROP_A)
		for currency, amount in (("UGX", 410619), ("USD", 11.12), ("KWD", 11.123)):
			with self.subTest(currency=currency):
				make_property(PROP_A, currency=currency, company=COMPANY, business_date=TODAY)
				stay = make_reservation(PROP_A, self.room_type, d(1), d(3))
				name = api.record_advance(stay.name, amount)["folio"]
				frappe.db.set_value("Reservation", stay.name, "status", "Checked In")
				transfer_advances_to_guest(stay.name)
				doc = frappe.get_doc("Folio", name)
				advance = doc.payments[0].name
				api.add_folio_payment(name, "Cash", amount + 1)
				api.refund_folio_payment(name, payment_row=advance, reason="Return the original advance")

				unrelated = make_reservation(PROP_A, self.room_type, d(1), d(3))
				other_name = api.record_advance(unrelated.name, amount)["folio"]
				frappe.db.set_value("Reservation", unrelated.name, "status", "Checked In")
				transfer_advances_to_guest(unrelated.name)
				ordinary = api.add_folio_payment(other_name, "Cash", amount + 1)["payments"][-1]["name"]
				api.refund_folio_payment(other_name, payment_row=ordinary, reason="Return the ordinary payment")

				# The old writer did not update the advance counter for in-house refunds.
				frappe.db.set_value("Reservation", stay.name, "advance_paid", amount)
				for _ in range(2):
					execute()
					self.assertEqual(frappe.db.get_value("Reservation", stay.name, "advance_paid"), 0)
					self.assertEqual(frappe.db.get_value("Reservation", unrelated.name, "advance_paid"), amount)
					self.assertEqual(frappe.db.get_value("Folio", name, "payments_total"), amount + 1)
					self.assertEqual(frappe.db.count("Folio Payment", {"parent": name}), 3)
