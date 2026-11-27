"""Only the verified invoice reference may leave a folio through a desk API."""
from unittest.mock import patch

import frappe

from kamra import api, folio
from kamra.tests import test_folio_credit_and_advance_r2 as r2
from kamra.tests.audit_fixtures import (
	PROP_A, TODAY, AuditTestCase, d, ensure_user, make_property, make_reservation, stocked_room_type,
)
from kamra.tests.bridge_fields import ensure_bridge_field


class FolioCreditR4Test(AuditTestCase):
	_erp = r2.FolioCreditAndAdvanceR2Test._erp
	_invoice = r2.FolioCreditAndAdvanceR2Test._invoice

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Schema setup needs its own transaction when Bench groups test modules.
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
			"doctype": "Group Booking", "property": PROP_A, "group_name": "R4 group",
			"check_in_date": TODAY, "check_out_date": d(2), "status": "Confirmed",
		}).insert(ignore_permissions=True).name
		self.stay = make_reservation(PROP_A, stocked_room_type(PROP_A), TODAY, d(2), group_booking=self.group)
		self.guest = folio.open_folio(self.stay)
		self.master = folio.open_group_folio(self.group)
		self.refs = {self.guest: "SI-R4-GUEST", self.master: "SI-R4-GROUP"}
		for name, ref in self.refs.items():
			frappe.db.set_value("Folio", name, "sales_invoice", ref)

	def check_views(self, role, *, owned=True, doc_read=True):
		user = ensure_user("r4-" + role.split()[0].lower() + "@example.test", [role])
		frappe.set_user(user)  # nosemgrep: frappe-setuser -- verify the desk and finance roles' API responses
		invoices = [self._invoice(ref, name if owned else "OTHER-FOLIO") for name, ref in self.refs.items()]
		with self._erp(invoices):
			native = frappe.has_permission

			def permitted(doctype=None, *args, doc=None, **kwargs):
				if doctype == "Sales Invoice" and doc is not None:
					return doc_read
				return native(doctype, *args, doc=doc, **kwargs)

			with patch.object(frappe, "has_permission", side_effect=permitted):
				single = api.get_folio(self.stay.name)
				stay = api.reservation_folios(self.stay.name)
				group = api.group_folios(self.group)
				printed = api.folio_invoice(self.guest)
				billing = [r for r in api.billing_folios(PROP_A) if r["name"] in self.refs]
		for view, rows, names in (
			("single guest folio", [single], {self.guest}),
			("reservation windows", stay, set(self.refs)),
			("group master", [group["master"]], {self.master}),
			("group member", group["members"][0]["folios"], {self.guest}),
			("printable folio", [printed["folio"]], {self.guest}),
			("billing", billing, set(self.refs)),
		):
			with self.subTest(view=view):
				self.assertEqual({r["name"] for r in rows}, names)
				for row in rows:
					expected = self.refs[row["name"]] if owned and doc_read else None
					self.assertEqual(row.get("sales_invoice"), expected)

	def test_front_desk_denied_by_invoice_document_hook(self):
		self.check_views("Front Desk", doc_read=False)

	def test_front_desk_foreign_link_in_the_same_company(self):
		self.check_views("Front Desk", owned=False)

	def test_authorized_finance_sees_each_verified_reference(self):
		self.check_views("Finance")
