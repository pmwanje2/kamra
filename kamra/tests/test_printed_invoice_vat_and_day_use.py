"""Printed Tax Invoice VAT on bridged sites + Day-Use morning times before 10:00.

- BridgedFolioInvoiceVatTests: when a closed Folio carries a linked accounting
  Sales Invoice, api.folio_invoice reads the accounting invoice's own tax lines
  (rate, taxable amount, tax) and line tax breakdown instead of printing 0%.
  When the accounting invoice cannot be read, tax_source is
  "accounting_unavailable" with no fake 0% rows and the plain fallback note
  "Tax as per the accounting invoice".
- DayUseBeforeTenTests: MariaDB Time columns before 10:00 hydrate as
  timedelta(hours=9) -> str "9:00:00" (7 chars), where str(t)[:5] produced
  "9:00:" and crashed reservation_slot_hours / is_due_out / alert_overdue_day_use.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.utils import flt

from kamra import accounting, api, day_use
from kamra import folio as folio_mod
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	guest_folio,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)
from kamra.tests.bridge_fields import ensure_bridge_field

PROP_UG = "UT Vat UGX"


def _fake_sales_invoice(
	name: str,
	*,
	folio_name: str | None,
	net_total: float,
	total_taxes: float,
	grand_total: float,
	items: list[dict],
	taxes: list[dict],
	docstatus: int = 1,
	company: str | None = None,
	currency: str | None = None,
	is_return: int = 0,
	return_against: str | None = None,
	kamra_rebill_of: str | None = None,
	item_wise_tax_details: list[dict] | None = None,
):
	return frappe._dict(
		doctype="Sales Invoice",
		name=name,
		kamra_ref=folio_name,
		docstatus=docstatus,
		company=company,
		currency=currency,
		is_return=is_return,
		return_against=return_against,
		kamra_rebill_of=kamra_rebill_of,
		net_total=net_total,
		total_taxes_and_charges=total_taxes,
		grand_total=grand_total,
		items=[frappe._dict(it) for it in items],
		taxes=[frappe._dict(tx) for tx in taxes],
		item_wise_tax_details=[frappe._dict(d) for d in (item_wise_tax_details or [])],
	)


class BridgedFolioInvoiceVatTests(AuditTestCase):
	"""Closed bridged Tax Invoice reads its tax breakdown from the linked ERP
	Sales Invoice, or falls back to 'Tax as per the accounting invoice'."""

	properties = (PROP_A, PROP_UG)

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_bridge_field("Folio", "sales_invoice")
		ensure_bridge_field("Property", "company")

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		if not frappe.db.exists("Currency", "UGX"):
			frappe.get_doc({
				"doctype": "Currency", "currency_name": "UGX",
				"symbol": "UGX ", "fraction": "", "fraction_units": 1,
				"number_format": "#,###.##", "enabled": 1,
			}).insert(ignore_permissions=True)
		else:
			frappe.db.set_value("Currency", "UGX", "enabled", 1)
		make_property(
			PROP_UG, business_date=TODAY, country="Uganda", currency="UGX",
			gst_mode="Fixed", gst_rate_low=0, gst_rate_high=0, rates_include_tax=1,
		)
		frappe.db.set_value("Property", PROP_UG, "company", None, update_modified=False)
		self.rt = make_room_type(PROP_UG, "UDLX", base_price=499500, tax_percent=0)
		frappe.db.set_value("Room Type", self.rt, {"base_price": 499500, "tax_percent": 0})
		self.room = make_room(PROP_UG, self.rt, "U101")

	def _closed_bridged_folio(
		self,
		charges: list[dict],
		*,
		sales_invoice: str,
		tax_total: float,
		grand_total: float,
		invoice_number: str | None = "INV-THL-26-00003",
		room: str | None = None,
	):
		res = make_reservation(PROP_UG, self.rt, d(-1), d(0), status="Checked In", room=room or self.room)
		fname = guest_folio(res.name)
		fdoc = frappe.get_doc("Folio", fname)
		for row in charges:
			fdoc.append("charges", {"posting_date": d(-1), "qty": 1, "gst_rate": 0, "gst_amount": 0, **row})
		fdoc.save(ignore_permissions=True)
		fdoc.reload()
		frappe.db.set_value(
			"Folio", fname,
			{
				"status": "Closed",
				"invoice_number": invoice_number,
				"closed_on": f"{TODAY} 10:00:00",
				"sales_invoice": sales_invoice,
				"tax_total": tax_total,
				"grand_total": grand_total,
				"balance": 0,
			},
			update_modified=False,
		)
		return frappe.get_doc("Folio", fname)

	def test_bridged_closed_invoice_reads_erp_sales_invoice_taxes_and_lines(self):
		fdoc = self._closed_bridged_folio(
			[{"charge_type": "Room", "description": "Room 101 · DLX", "rate": 499500, "amount": 499500, "total": 499500}],
			sales_invoice="ACC-SINV-TEST-001",
			tax_total=76194.92,
			grand_total=499500.0,
		)
		charge_row = fdoc.charges[0]
		fake_si = _fake_sales_invoice(
			"ACC-SINV-TEST-001",
			folio_name=fdoc.name,
			net_total=423305.08,
			total_taxes=76194.92,
			grand_total=499500.0,
			items=[{
				"item_code": "ITEM-ROOM",
				"source_ref": charge_row.name,
				"net_amount": 423305.08,
				"amount": 499500.0,
				"kamra_allowance_refs": "",
			}],
			taxes=[{
				"charge_type": "On Net Total",
				"account_head": "VAT - THL",
				"description": "VAT 18%",
				"rate": 18.0,
				"tax_amount": 76194.92,
				"tax_amount_after_discount_amount": 76194.92,
				"included_in_print_rate": 1,
				"item_wise_tax_detail": json.dumps({"ITEM-ROOM": [18.0, 76194.92]}),
			}],
		)

		real_get_doc = frappe.get_doc
		real_exists = frappe.db.exists

		def fake_get_doc(doctype, name=None, *args, **kwargs):
			if doctype == "Sales Invoice" and name == "ACC-SINV-TEST-001":
				return fake_si
			return real_get_doc(doctype, name, *args, **kwargs)

		def fake_exists(doctype, name=None, *args, **kwargs):
			if doctype == "DocType" and name == "Sales Invoice":
				return True
			return real_exists(doctype, name, *args, **kwargs)

		with patch("frappe.get_doc", side_effect=fake_get_doc), \
				patch("frappe.db.exists", side_effect=fake_exists):
			inv = api.folio_invoice(fdoc.name)

		self.assertEqual(inv["tax_source"], "accounting")
		self.assertEqual(len(inv["tax_summary"]), 1)
		row = inv["tax_summary"][0]
		self.assertEqual(flt(row["rate"]), 18.0)
		self.assertAlmostEqual(flt(row["taxable"]), 423305.08, places=2)
		self.assertAlmostEqual(flt(row["total_tax"]), 76194.92, places=2)
		self.assertEqual(len(row["parts"]), 1)
		self.assertEqual(row["parts"][0]["label"], "VAT")
		self.assertEqual(flt(row["parts"][0]["rate"]), 18.0)
		self.assertAlmostEqual(flt(row["parts"][0]["amount"]), 76194.92, places=2)

		# Line and header totals agree with the accounting invoice
		self.assertAlmostEqual(flt(inv["lines"][0]["amount"]), 423305.08, places=2)
		self.assertEqual(flt(inv["lines"][0]["gst_rate"]), 18.0)
		self.assertAlmostEqual(flt(inv["lines"][0]["gst_amount"]), 76194.92, places=2)
		self.assertAlmostEqual(flt(inv["lines"][0]["total"]), 499500.0, places=2)
		self.assertAlmostEqual(flt(inv["folio"]["charges_total"]), 423305.08, places=2)
		self.assertAlmostEqual(flt(inv["folio"]["tax_total"]), 76194.92, places=2)
		self.assertAlmostEqual(flt(inv["folio"]["grand_total"]), 499500.0, places=2)

	def test_bridged_closed_invoice_multi_night_and_netted_discount_matches_erp_invoice(self):
		fdoc = self._closed_bridged_folio(
			[
				{"charge_type": "Room", "description": "Night 1", "rate": 150000, "amount": 150000, "total": 150000},
				{"charge_type": "Room", "description": "Night 2", "rate": 150000, "amount": 150000, "total": 150000},
				{"charge_type": "Discount", "description": "Voucher", "rate": -30000, "amount": -30000, "total": -30000},
			],
			sales_invoice="ACC-SINV-TEST-002",
			tax_total=41186.44,
			grand_total=270000.0,
		)
		c1, c2, c_disc = fdoc.charges
		# Bridge netted -30,000 voucher into Night 1 (120,000 net -> 101,694.92 ex-VAT; Night 2 150,000 -> 127,118.64 ex-VAT)
		fake_si = _fake_sales_invoice(
			"ACC-SINV-TEST-002",
			folio_name=fdoc.name,
			net_total=228813.56,
			total_taxes=41186.44,
			grand_total=270000.0,
			items=[
				{
					"item_code": "ITEM-ROOM", "source_ref": c1.name,
					"net_amount": 101694.92, "amount": 120000.0,
					"kamra_allowance_refs": f"{c_disc.name} 30000.0",
				},
				{
					"item_code": "ITEM-ROOM", "source_ref": c2.name,
					"net_amount": 127118.64, "amount": 150000.0,
					"kamra_allowance_refs": "",
				},
			],
			taxes=[{
				"charge_type": "On Net Total", "account_head": "VAT - THL",
				"description": "VAT 18%", "rate": 18.0,
				"tax_amount": 41186.44, "tax_amount_after_discount_amount": 41186.44,
				"included_in_print_rate": 1,
				"item_wise_tax_detail": json.dumps({"ITEM-ROOM": [18.0, 41186.44]}),
			}],
		)

		real_get_doc = frappe.get_doc
		real_exists = frappe.db.exists
		with patch("frappe.get_doc", side_effect=lambda dt, nm=None, *a, **k:
		           fake_si if dt == "Sales Invoice" and nm == "ACC-SINV-TEST-002" else real_get_doc(dt, nm, *a, **k)), \
				patch("frappe.db.exists", side_effect=lambda dt, nm=None, *a, **k:
				      True if dt == "DocType" and nm == "Sales Invoice" else real_exists(dt, nm, *a, **k)):
			inv = api.folio_invoice(fdoc.name)

		self.assertEqual(inv["tax_source"], "accounting")
		self.assertAlmostEqual(sum(flt(ln["amount"]) for ln in inv["lines"]), 228813.56, places=2)
		self.assertAlmostEqual(sum(flt(ln["gst_amount"]) for ln in inv["lines"]), 41186.44, places=2)
		self.assertAlmostEqual(sum(flt(ln["total"]) for ln in inv["lines"]), 270000.0, places=2)
		for ln in inv["lines"]:
			self.assertEqual(flt(ln["gst_rate"]), 18.0)

	def test_bridged_closed_invoice_exclusive_multi_tax_rows_preserved(self):
		fdoc = self._closed_bridged_folio(
			[{"charge_type": "Room", "description": "Room 101", "rate": 100000, "amount": 100000, "total": 100000}],
			sales_invoice="ACC-SINV-TEST-003",
			tax_total=20000.0,
			grand_total=120000.0,
		)
		fake_si = _fake_sales_invoice(
			"ACC-SINV-TEST-003",
			folio_name=fdoc.name,
			net_total=100000.0,
			total_taxes=20000.0,
			grand_total=120000.0,
			items=[{
				"item_code": "ITEM-ROOM", "source_ref": fdoc.charges[0].name,
				"net_amount": 100000.0, "amount": 100000.0, "kamra_allowance_refs": "",
			}],
			taxes=[
				{
					"charge_type": "On Net Total", "account_head": "VAT - THL",
					"description": "VAT 18%", "rate": 18.0,
					"tax_amount": 18000.0, "tax_amount_after_discount_amount": 18000.0,
					"included_in_print_rate": 0,
					"item_wise_tax_detail": json.dumps({"ITEM-ROOM": [18.0, 18000.0]}),
				},
				{
					"charge_type": "On Net Total", "account_head": "Levy - THL",
					"description": "Tourism Levy 2%", "rate": 2.0,
					"tax_amount": 2000.0, "tax_amount_after_discount_amount": 2000.0,
					"included_in_print_rate": 0,
					"item_wise_tax_detail": json.dumps({"ITEM-ROOM": [2.0, 2000.0]}),
				},
			],
		)

		real_get_doc = frappe.get_doc
		real_exists = frappe.db.exists
		with patch("frappe.get_doc", side_effect=lambda dt, nm=None, *a, **k:
		           fake_si if dt == "Sales Invoice" and nm == "ACC-SINV-TEST-003" else real_get_doc(dt, nm, *a, **k)), \
				patch("frappe.db.exists", side_effect=lambda dt, nm=None, *a, **k:
				      True if dt == "DocType" and nm == "Sales Invoice" else real_exists(dt, nm, *a, **k)):
			inv = api.folio_invoice(fdoc.name)

		self.assertEqual(inv["tax_source"], "accounting")
		self.assertEqual([(flt(r["rate"]), flt(r["taxable"]), flt(r["total_tax"])) for r in inv["tax_summary"]],
		                 [(18.0, 100000.0, 18000.0), (2.0, 100000.0, 2000.0)])
		self.assertEqual([r["parts"][0]["label"] for r in inv["tax_summary"]], ["VAT", "Tourism Levy"])
		self.assertEqual(flt(inv["lines"][0]["gst_rate"]), 20.0)
		self.assertAlmostEqual(flt(inv["lines"][0]["gst_amount"]), 20000.0, places=2)
		self.assertAlmostEqual(flt(inv["lines"][0]["total"]), 120000.0, places=2)

	def test_bridged_closed_invoice_unreadable_erp_invoice_never_emits_zero_percent(self):
		fdoc = self._closed_bridged_folio(
			[{"charge_type": "Room", "description": "Room 101", "rate": 499500, "amount": 499500, "total": 499500}],
			sales_invoice="ACC-SINV-MISSING-999",
			tax_total=76194.92,
			grand_total=499500.0,
		)
		# Missing document, PermissionError, or cancelled docstatus==2 all fall back cleanly
		inv = api.folio_invoice(fdoc.name)
		self.assertEqual(inv["tax_source"], "accounting_unavailable")
		self.assertEqual(inv["tax_summary"], [])
		self.assertEqual(inv["gst_summary"], [])
		self.assertEqual(inv["document"]["accounting_tax_note"], "Tax as per the accounting invoice")
		self.assertNotRegex(inv["document"]["accounting_tax_note"], r"(?i)kamra|frappe|erpnext|ury")

		real_get_doc = frappe.get_doc
		real_exists = frappe.db.exists
		cancelled_si = _fake_sales_invoice(
			"ACC-SINV-MISSING-999", folio_name=fdoc.name,
			net_total=423305.08, total_taxes=76194.92, grand_total=499500.0,
			items=[], taxes=[], docstatus=2,
		)
		with patch("frappe.get_doc", side_effect=lambda dt, nm=None, *a, **k:
		           cancelled_si if dt == "Sales Invoice" and nm == "ACC-SINV-MISSING-999" else real_get_doc(dt, nm, *a, **k)), \
				patch("frappe.db.exists", side_effect=lambda dt, nm=None, *a, **k:
				      True if dt == "DocType" and nm == "Sales Invoice" else real_exists(dt, nm, *a, **k)):
			inv_cancelled = api.folio_invoice(fdoc.name)
		self.assertEqual(inv_cancelled["tax_source"], "accounting_unavailable")
		self.assertEqual(inv_cancelled["tax_summary"], [])

	def test_unbridged_closed_invoice_keeps_folio_tax_summary(self):
		make_property(PROP_A, business_date=TODAY, gst_mode="Fixed", gst_rate_low=18, gst_rate_high=18)
		rt = make_room_type(PROP_A, "STD", base_price=1000, tax_percent=18)
		room = make_room(PROP_A, rt, "101")
		res = make_reservation(PROP_A, rt, d(-1), d(0), status="Checked In", room=room)
		fname = guest_folio(res.name)
		folio_mod.post_room_night(frappe.get_doc("Reservation", res.name), d(-1))
		inv = api.folio_invoice(fname)
		self.assertEqual(inv["tax_source"], "folio")
		self.assertTrue(inv["tax_summary"])

	def test_load_accounting_invoice_checks_sales_invoice_read_and_doc_permission(self):
		"""P0: _load_accounting_invoice enforces native Sales Invoice read permission
		(role-level and doc-level / User Permissions) and fails closed without leaking figures."""
		fdoc = self._closed_bridged_folio(
			[{"charge_type": "Room", "description": "Room 101 · DLX", "rate": 499500, "amount": 499500, "total": 499500}],
			sales_invoice="ACC-SINV-PERM-001",
			tax_total=76194.92,
			grand_total=499500.0,
			invoice_number=None,
		)
		fake_si = _fake_sales_invoice(
			"ACC-SINV-PERM-001",
			folio_name=fdoc.name,
			net_total=423305.08,
			total_taxes=76194.92,
			grand_total=499500.0,
			items=[{
				"item_code": "ITEM-ROOM", "source_ref": fdoc.charges[0].name,
				"net_amount": 423305.08, "amount": 499500.0,
			}],
			taxes=[{
				"charge_type": "On Net Total", "account_head": "VAT - THR",
				"description": "VAT 18%", "rate": 18.0,
				"tax_amount": 76194.92, "tax_amount_after_discount_amount": 76194.92,
				"included_in_print_rate": 1,
				"item_wise_tax_detail": json.dumps({"ITEM-ROOM": [18.0, 76194.92]}),
			}],
		)
		real_get_doc = frappe.get_doc
		real_exists = frappe.db.exists
		real_has_perm = frappe.has_permission

		# 1. Role lacks Sales Invoice read permission
		def deny_si_role(doctype, ptype="read", doc=None, *args, **kwargs):
			if doctype == "Sales Invoice":
				return False
			return real_has_perm(doctype, ptype=ptype, doc=doc, *args, **kwargs)

		with patch("frappe.get_doc", side_effect=lambda dt, nm=None, *a, **k:
		           fake_si if dt == "Sales Invoice" and nm == "ACC-SINV-PERM-001" else real_get_doc(dt, nm, *a, **k)), \
				patch("frappe.db.exists", side_effect=lambda dt, nm=None, *a, **k:
				      True if dt == "DocType" and nm == "Sales Invoice" else real_exists(dt, nm, *a, **k)), \
				patch("frappe.has_permission", side_effect=deny_si_role):
			inv = api.folio_invoice(fdoc.name)

		self.assertEqual(inv["tax_source"], "accounting_unavailable")
		self.assertEqual(inv["tax_summary"], [])
		self.assertEqual(inv["gst_summary"], [])
		self.assertEqual(inv["document"]["accounting_tax_note"], "Tax as per the accounting invoice")
		self.assertEqual(flt(inv["lines"][0]["gst_rate"]), 0.0)
		self.assertEqual(flt(inv["lines"][0]["gst_amount"]), 0.0)

		# 2. Role has Sales Invoice read, but doc-level / User Permission denies this document
		doc_checked = []

		def deny_si_doc(doctype, ptype="read", doc=None, *args, **kwargs):
			if doctype == "Sales Invoice":
				if doc is None:
					return True
				doc_checked.append(doc)
				return False
			return real_has_perm(doctype, ptype=ptype, doc=doc, *args, **kwargs)

		with patch("frappe.get_doc", side_effect=lambda dt, nm=None, *a, **k:
		           fake_si if dt == "Sales Invoice" and nm == "ACC-SINV-PERM-001" else real_get_doc(dt, nm, *a, **k)), \
				patch("frappe.db.exists", side_effect=lambda dt, nm=None, *a, **k:
				      True if dt == "DocType" and nm == "Sales Invoice" else real_exists(dt, nm, *a, **k)), \
				patch("frappe.has_permission", side_effect=deny_si_doc):
			inv_up = api.folio_invoice(fdoc.name)

		self.assertTrue(doc_checked, "doc-level / User Permission check must receive the Sales Invoice doc")
		self.assertEqual(doc_checked[0].name, "ACC-SINV-PERM-001")
		self.assertEqual(inv_up["tax_source"], "accounting_unavailable")
		self.assertEqual(inv_up["tax_summary"], [])
		self.assertEqual(flt(inv_up["lines"][0]["gst_rate"]), 0.0)

	def test_bridged_checkout_submitted_si_is_final_with_si_number_and_draft_or_cancelled_is_not_final(self):
		"""P1: real bridged check-out (invoice_number=None, submitted SI) prints as final with SI number;
		draft or cancelled SI is not final."""
		fdoc = self._closed_bridged_folio(
			[{"charge_type": "Room", "description": "Room 101 · DLX", "rate": 499500, "amount": 499500, "total": 499500}],
			sales_invoice="ACC-SINV-CHK-001",
			tax_total=76194.92,
			grand_total=499500.0,
			invoice_number=None,
		)
		real_get_doc = frappe.get_doc
		real_exists = frappe.db.exists

		def _run(docstatus: int):
			si = _fake_sales_invoice(
				"ACC-SINV-CHK-001",
				folio_name=fdoc.name,
				net_total=423305.08,
				total_taxes=76194.92,
				grand_total=499500.0,
				docstatus=docstatus,
				items=[{
					"item_code": "ITEM-ROOM", "source_ref": fdoc.charges[0].name,
					"net_amount": 423305.08, "amount": 499500.0,
				}],
				taxes=[{
					"charge_type": "On Net Total", "account_head": "VAT - THR",
					"description": "VAT 18%", "rate": 18.0,
					"tax_amount": 76194.92, "tax_amount_after_discount_amount": 76194.92,
					"included_in_print_rate": 1,
					"item_wise_tax_detail": json.dumps({"ITEM-ROOM": [18.0, 76194.92]}),
				}],
			)
			with patch("frappe.get_doc", side_effect=lambda dt, nm=None, *a, **k:
			           si if dt == "Sales Invoice" and nm == "ACC-SINV-CHK-001" else real_get_doc(dt, nm, *a, **k)), \
					patch("frappe.db.exists", side_effect=lambda dt, nm=None, *a, **k:
					      True if dt == "DocType" and nm == "Sales Invoice" else real_exists(dt, nm, *a, **k)):
				return api.folio_invoice(fdoc.name)

		inv_submitted = _run(1)
		self.assertEqual(inv_submitted["tax_source"], "accounting")
		self.assertTrue(inv_submitted["document"]["is_final"])
		self.assertEqual(inv_submitted["document"]["number"], "ACC-SINV-CHK-001")
		self.assertEqual(inv_submitted["document"]["title"], "Tax Invoice")

		inv_draft = _run(0)
		self.assertEqual(inv_draft["tax_source"], "accounting_unavailable")
		self.assertFalse(inv_draft["document"]["is_final"])
		self.assertEqual(inv_draft["document"]["title"], "Provisional Bill")

		# Even when hotel invoice_number is populated, a cancelled or draft SI is not final
		frappe.db.set_value("Folio", fdoc.name, "invoice_number", "INV-THL-26-00099", update_modified=False)
		inv_cancelled = _run(2)
		self.assertEqual(inv_cancelled["tax_source"], "accounting_unavailable")
		self.assertFalse(inv_cancelled["document"]["is_final"])
		self.assertEqual(inv_cancelled["document"]["title"], "Provisional Bill")

		inv_draft_with_num = _run(0)
		self.assertFalse(inv_draft_with_num["document"]["is_final"])
		self.assertEqual(inv_draft_with_num["document"]["title"], "Provisional Bill")

	def test_uganda_invoice_title_follows_vat_determination(self):
		"""Uganda authoritative VAT-charged invoices print 'Tax Invoice';
		zero-VAT, exempt, and non-VAT levy-only invoices stay 'Invoice'."""
		fdoc = self._closed_bridged_folio(
			[{"charge_type": "Room", "description": "Room 101", "rate": 499500, "amount": 499500, "total": 499500}],
			sales_invoice="ACC-SINV-UG-TITLE",
			tax_total=0,
			grand_total=499500.0,
			invoice_number=None,
		)
		real_get_doc = frappe.get_doc
		real_exists = frappe.db.exists

		def _run_case(taxes: list[dict], net: float, tax_tot: float, grand: float):
			si = _fake_sales_invoice(
				"ACC-SINV-UG-TITLE",
				folio_name=fdoc.name,
				net_total=net,
				total_taxes=tax_tot,
				grand_total=grand,
				docstatus=1,
				items=[{
					"item_code": "ITEM-ROOM", "source_ref": fdoc.charges[0].name,
					"net_amount": net, "amount": grand,
				}],
				taxes=taxes,
			)
			with patch("frappe.get_doc", side_effect=lambda dt, nm=None, *a, **k:
			           si if dt == "Sales Invoice" and nm == "ACC-SINV-UG-TITLE" else real_get_doc(dt, nm, *a, **k)), \
					patch("frappe.db.exists", side_effect=lambda dt, nm=None, *a, **k:
					      True if dt == "DocType" and nm == "Sales Invoice" else real_exists(dt, nm, *a, **k)):
				return api.folio_invoice(fdoc.name)

		# 1. 18% VAT charged -> Tax Invoice
		inv_vat = _run_case(
			[{
				"charge_type": "On Net Total", "account_head": "VAT - THR",
				"description": "VAT 18%", "rate": 18.0,
				"tax_amount": 76194.92, "tax_amount_after_discount_amount": 76194.92,
				"included_in_print_rate": 1,
				"item_wise_tax_detail": json.dumps({"ITEM-ROOM": [18.0, 76194.92]}),
			}],
			423305.08, 76194.92, 499500.0,
		)
		self.assertEqual(inv_vat["document"]["title"], "Tax Invoice")

		# 2. 0% / exempt (no VAT) -> Invoice
		inv_exempt = _run_case([], 499500.0, 0.0, 499500.0)
		self.assertEqual(inv_exempt["document"]["title"], "Invoice")

		# 3. Non-VAT levy only (Local Hotel Tax / Tourism Levy, zero VAT) -> Invoice
		inv_levy = _run_case(
			[{
				"charge_type": "On Net Total", "account_head": "Local Hotel Tax - THR",
				"description": "Local Hotel Tax 2%", "rate": 2.0,
				"tax_amount": 9990.0, "tax_amount_after_discount_amount": 9990.0,
				"included_in_print_rate": 0,
				"item_wise_tax_detail": json.dumps({"ITEM-ROOM": [2.0, 9990.0]}),
			}],
			499500.0, 9990.0, 509490.0,
		)
		self.assertEqual(inv_levy["document"]["title"], "Invoice")

	def test_unreadable_tax_never_titles_document_tax_invoice_across_country_packs(self):
		"""P1: UAE, India, Thailand, Uganda never title a document 'Tax Invoice' when
		accounting tax could not be read; use the pack's neutral title."""
		fdoc = self._closed_bridged_folio(
			[{"charge_type": "Room", "description": "Room 101", "rate": 499500, "amount": 499500, "total": 499500}],
			sales_invoice="ACC-SINV-UNREADABLE-PACK",
			tax_total=0,
			grand_total=499500.0,
			invoice_number="INV-PACK-0001",
		)
		for country in ("United Arab Emirates", "India", "Thailand", "Uganda"):
			with self.subTest(country=country):
				frappe.db.set_value("Property", PROP_UG, "country", country, update_modified=False)
				frappe.clear_document_cache("Property", PROP_UG)
				inv = api.folio_invoice(fdoc.name)
				self.assertEqual(inv["tax_source"], "accounting_unavailable")
				self.assertNotIn("tax invoice", inv["document"]["title"].lower())
				self.assertEqual(inv["document"]["title"], "Invoice")
				self.assertNotIn("tax invoice", (inv["document"]["footer"] or "").lower())

	def test_rejects_cross_company_cross_currency_unlinked_and_return_sales_invoice(self):
		"""P1: only accept a Sales Invoice matching the folio's company, currency,
		explicit kamra_ref == folio.name, and is_return == 0."""
		frappe.db.set_value("Property", PROP_UG, "company", "Tank Hill Review Ltd", update_modified=False)
		frappe.clear_document_cache("Property", PROP_UG)
		fdoc = self._closed_bridged_folio(
			[{"charge_type": "Room", "description": "Room 101", "rate": 499500, "amount": 499500, "total": 499500}],
			sales_invoice="ACC-SINV-ISOLATION",
			tax_total=0,
			grand_total=499500.0,
		)
		real_get_doc = frappe.get_doc
		real_exists = frappe.db.exists

		bad_cases = [
			("cross_company", dict(folio_name=fdoc.name, company="Other Lodge Ltd", currency="UGX", is_return=0)),
			("empty_kamra_ref", dict(folio_name="", company="Tank Hill Review Ltd", currency="UGX", is_return=0)),
			("wrong_kamra_ref", dict(folio_name="FOLIO-OTHER-999", company="Tank Hill Review Ltd", currency="UGX", is_return=0)),
			("cross_currency", dict(folio_name=fdoc.name, company="Tank Hill Review Ltd", currency="USD", is_return=0)),
			("return_invoice", dict(folio_name=fdoc.name, company="Tank Hill Review Ltd", currency="UGX", is_return=1)),
		]
		for label, kw in bad_cases:
			with self.subTest(case=label):
				bad_si = _fake_sales_invoice(
					"ACC-SINV-ISOLATION",
					net_total=777000.0,
					total_taxes=139860.0,
					grand_total=916860.0,
					items=[{"item_code": "ITEM-ROOM", "source_ref": fdoc.charges[0].name, "net_amount": 777000.0, "amount": 916860.0}],
					taxes=[{
						"charge_type": "On Net Total", "account_head": "VAT - OLL",
						"description": "VAT 18%", "rate": 18.0,
						"tax_amount": 139860.0, "tax_amount_after_discount_amount": 139860.0,
						"included_in_print_rate": 1,
						"item_wise_tax_detail": json.dumps({"ITEM-ROOM": [18.0, 139860.0]}),
					}],
					**kw,
				)
				with patch("frappe.get_doc", side_effect=lambda dt, nm=None, *a, **k:
				           bad_si if dt == "Sales Invoice" and nm == "ACC-SINV-ISOLATION" else real_get_doc(dt, nm, *a, **k)), \
						patch("frappe.db.exists", side_effect=lambda dt, nm=None, *a, **k:
						      True if dt == "DocType" and nm == "Sales Invoice" else real_exists(dt, nm, *a, **k)):
					inv = api.folio_invoice(fdoc.name)
				self.assertEqual(inv["tax_source"], "accounting_unavailable")
				self.assertEqual(inv["tax_summary"], [])
				self.assertAlmostEqual(flt(inv["folio"]["grand_total"]), 499500.0, places=2)

	def test_follows_kamra_rebill_of_from_cancelled_si_and_sums_erp_returns_as_credited(self):
		"""P1: cancelled linked SI follows kamra_rebill_of to the current submitted invoice,
		and submitted ERP return Sales Invoices (is_return=1) show in credited totals."""
		frappe.db.set_value("Property", PROP_UG, "company", "Tank Hill Review Ltd", update_modified=False)
		frappe.clear_document_cache("Property", PROP_UG)
		fdoc = self._closed_bridged_folio(
			[{"charge_type": "Room", "description": "Room 101 · DLX", "rate": 499500, "amount": 499500, "total": 499500}],
			sales_invoice="ACC-SINV-OLD-1",
			tax_total=76194.92,
			grand_total=499500.0,
			invoice_number=None,
		)
		charge_row = fdoc.charges[0]
		old_si = _fake_sales_invoice(
			"ACC-SINV-OLD-1",
			folio_name=fdoc.name,
			company="Tank Hill Review Ltd",
			currency="UGX",
			docstatus=2,
			net_total=423305.08,
			total_taxes=76194.92,
			grand_total=499500.0,
			items=[{"item_code": "ITEM-ROOM", "source_ref": charge_row.name, "net_amount": 423305.08, "amount": 499500.0}],
			taxes=[],
		)
		rebill_si = _fake_sales_invoice(
			"ACC-SINV-REBILL-2",
			folio_name=None,
			kamra_rebill_of="ACC-SINV-OLD-1",
			company="Tank Hill Review Ltd",
			currency="UGX",
			docstatus=1,
			net_total=423305.08,
			total_taxes=76194.92,
			grand_total=499500.0,
			# Bridge rebills omit source_ref because source_ref is unique on the original SI
			items=[{"item_code": "ITEM-ROOM", "net_amount": 423305.08, "amount": 499500.0}],
			taxes=[{
				"charge_type": "On Net Total", "account_head": "VAT - THR",
				"description": "VAT 18%", "rate": 18.0,
				"tax_amount": 76194.92, "tax_amount_after_discount_amount": 76194.92,
				"included_in_print_rate": 1,
				"item_wise_tax_detail": json.dumps({"ITEM-ROOM": [18.0, 76194.92]}),
			}],
		)
		ret_si = _fake_sales_invoice(
			"ACC-SINV-RET-1",
			folio_name=None,
			company="Tank Hill Review Ltd",
			currency="UGX",
			docstatus=1,
			is_return=1,
			return_against="ACC-SINV-REBILL-2",
			net_total=-84745.76,
			total_taxes=-15254.24,
			grand_total=-100000.0,
			items=[],
			taxes=[],
		)
		si_map = {
			"ACC-SINV-OLD-1": old_si,
			"ACC-SINV-REBILL-2": rebill_si,
			"ACC-SINV-RET-1": ret_si,
		}
		real_get_doc = frappe.get_doc
		real_exists = frappe.db.exists
		real_has_column = frappe.db.has_column
		real_get_all = frappe.get_all

		def fake_get_all(doctype, filters=None, *args, **kwargs):
			if doctype == "Sales Invoice" and isinstance(filters, dict):
				if filters.get("kamra_rebill_of") == "ACC-SINV-OLD-1":
					return ["ACC-SINV-REBILL-2"]
				if filters.get("kamra_rebill_of") == "ACC-SINV-REBILL-2":
					return []
				if filters.get("return_against") == "ACC-SINV-REBILL-2" and filters.get("is_return") == 1:
					return ["ACC-SINV-RET-1"]
				return []
			return real_get_all(doctype, filters=filters, *args, **kwargs)

		with patch("frappe.get_doc", side_effect=lambda dt, nm=None, *a, **k:
		           si_map[nm] if dt == "Sales Invoice" and nm in si_map else real_get_doc(dt, nm, *a, **k)), \
				patch("frappe.db.exists", side_effect=lambda dt, nm=None, *a, **k:
				      True if dt == "DocType" and nm == "Sales Invoice" else real_exists(dt, nm, *a, **k)), \
				patch("frappe.db.has_column", side_effect=lambda dt, col, *a, **k:
				      True if dt == "Sales Invoice" and col == "kamra_rebill_of" else real_has_column(dt, col, *a, **k)), \
				patch("frappe.get_all", side_effect=fake_get_all):
			inv = api.folio_invoice(fdoc.name)

		self.assertEqual(inv["tax_source"], "accounting")
		self.assertTrue(inv["document"]["is_final"])
		self.assertEqual(inv["document"]["number"], "ACC-SINV-REBILL-2")
		self.assertEqual(flt(inv["lines"][0]["gst_rate"]), 18.0)
		self.assertAlmostEqual(flt(inv["lines"][0]["gst_amount"]), 76194.92, places=2)
		self.assertAlmostEqual(flt(inv["credited"]), 100000.0, places=2)

	def test_mixed_18_and_0_exempt_includes_exempt_base_and_pure_exempt_omits_phantom_18(self):
		"""P2: 0% / exempt base is shown in tax_summary on mixed invoices, and pure-exempt
		under an 18% header template never emits a phantom 18% bucket."""
		fdoc = self._closed_bridged_folio(
			[
				{"charge_type": "Food & Beverage", "description": "Gala Dinner 18%", "rate": 499500, "amount": 499500, "total": 499500},
				{"charge_type": "Misc", "description": "Airport Shuttle Exempt", "rate": 50000, "amount": 50000, "total": 50000},
			],
			sales_invoice="ACC-SINV-MIXED-001",
			tax_total=76194.92,
			grand_total=549500.0,
		)
		c_taxed, c_exempt = fdoc.charges
		mixed_si = _fake_sales_invoice(
			"ACC-SINV-MIXED-001",
			folio_name=fdoc.name,
			net_total=473305.08,
			total_taxes=76194.92,
			grand_total=549500.0,
			items=[
				{"name": "sii-1", "item_code": "ITEM-FNB", "source_ref": c_taxed.name, "net_amount": 423305.08, "amount": 499500.0},
				{"name": "sii-2", "item_code": "ITEM-EXEMPT", "source_ref": c_exempt.name, "net_amount": 50000.0, "amount": 50000.0},
			],
			taxes=[{
				"name": "sit-1", "charge_type": "On Net Total", "account_head": "VAT - THR",
				"description": "VAT 18%", "rate": 18.0,
				"tax_amount": 76194.92, "tax_amount_after_discount_amount": 76194.92,
				"included_in_print_rate": 1,
			}],
			item_wise_tax_details=[
				{"tax_row": "sit-1", "item_row": "sii-1", "rate": 18.0, "taxable_amount": 423305.08, "amount": 76194.92},
				{"tax_row": "sit-1", "item_row": "sii-2", "rate": 0.0, "taxable_amount": 50000.0, "amount": 0.0},
			],
		)
		real_get_doc = frappe.get_doc
		real_exists = frappe.db.exists
		with patch("frappe.get_doc", side_effect=lambda dt, nm=None, *a, **k:
		           mixed_si if dt == "Sales Invoice" and nm == "ACC-SINV-MIXED-001" else real_get_doc(dt, nm, *a, **k)), \
				patch("frappe.db.exists", side_effect=lambda dt, nm=None, *a, **k:
				      True if dt == "DocType" and nm == "Sales Invoice" else real_exists(dt, nm, *a, **k)):
			inv_mixed = api.folio_invoice(fdoc.name)

		self.assertEqual(
			[(flt(r["rate"]), round(flt(r["taxable"]), 2), round(flt(r["total_tax"]), 2)) for r in inv_mixed["tax_summary"]],
			[(18.0, 423305.08, 76194.92), (0.0, 50000.0, 0.0)],
		)

		# Pure-exempt invoice carrying an 18% header template row with 0 tax
		room_ex = make_room(PROP_UG, self.rt, "U102")
		fdoc_ex = self._closed_bridged_folio(
			[{"charge_type": "Misc", "description": "Airport Shuttle Exempt", "rate": 50000, "amount": 50000, "total": 50000}],
			sales_invoice="ACC-SINV-EXEMPT-001",
			tax_total=0.0,
			grand_total=50000.0,
			room=room_ex,
		)
		pure_exempt_si = _fake_sales_invoice(
			"ACC-SINV-EXEMPT-001",
			folio_name=fdoc_ex.name,
			net_total=50000.0,
			total_taxes=0.0,
			grand_total=50000.0,
			items=[{"name": "sii-ex", "item_code": "ITEM-EXEMPT", "source_ref": fdoc_ex.charges[0].name, "net_amount": 50000.0, "amount": 50000.0}],
			taxes=[{
				"name": "sit-ex", "charge_type": "On Net Total", "account_head": "VAT - THR",
				"description": "VAT 18%", "rate": 18.0,
				"tax_amount": 0.0, "tax_amount_after_discount_amount": 0.0,
				"included_in_print_rate": 1,
			}],
			item_wise_tax_details=[
				{"tax_row": "sit-ex", "item_row": "sii-ex", "rate": 0.0, "taxable_amount": 50000.0, "amount": 0.0},
			],
		)
		with patch("frappe.get_doc", side_effect=lambda dt, nm=None, *a, **k:
		           pure_exempt_si if dt == "Sales Invoice" and nm == "ACC-SINV-EXEMPT-001" else real_get_doc(dt, nm, *a, **k)), \
				patch("frappe.db.exists", side_effect=lambda dt, nm=None, *a, **k:
				      True if dt == "DocType" and nm == "Sales Invoice" else real_exists(dt, nm, *a, **k)):
			inv_ex = api.folio_invoice(fdoc_ex.name)

		self.assertEqual(
			[(flt(r["rate"]), round(flt(r["taxable"]), 2), round(flt(r["total_tax"]), 2)) for r in inv_ex["tax_summary"]],
			[(0.0, 50000.0, 0.0)],
		)

	def test_accounting_export_does_not_double_count_multi_tax_base(self):
		"""P2: accounting.export_invoices must not double-count taxable base when two
		ERP tax rows (e.g. VAT 18% + Tourism Levy 2%) share the same net amount."""
		fdoc = self._closed_bridged_folio(
			[{"charge_type": "Room", "description": "Room 101", "rate": 100000, "amount": 100000, "total": 100000}],
			sales_invoice="ACC-SINV-EXPORT-MULTI",
			tax_total=20000.0,
			grand_total=120000.0,
			invoice_number="INV-THL-26-00777",
		)
		multi_si = _fake_sales_invoice(
			"ACC-SINV-EXPORT-MULTI",
			folio_name=fdoc.name,
			net_total=100000.0,
			total_taxes=20000.0,
			grand_total=120000.0,
			items=[{"item_code": "ITEM-ROOM", "source_ref": fdoc.charges[0].name, "net_amount": 100000.0, "amount": 100000.0}],
			taxes=[
				{
					"charge_type": "On Net Total", "account_head": "VAT - THR",
					"description": "VAT 18%", "rate": 18.0,
					"tax_amount": 18000.0, "tax_amount_after_discount_amount": 18000.0,
					"included_in_print_rate": 0,
					"item_wise_tax_detail": json.dumps({"ITEM-ROOM": [18.0, 18000.0]}),
				},
				{
					"charge_type": "On Net Total", "account_head": "Levy - THR",
					"description": "Tourism Levy 2%", "rate": 2.0,
					"tax_amount": 2000.0, "tax_amount_after_discount_amount": 2000.0,
					"included_in_print_rate": 0,
					"item_wise_tax_detail": json.dumps({"ITEM-ROOM": [2.0, 2000.0]}),
				},
			],
		)
		real_get_doc = frappe.get_doc
		real_exists = frappe.db.exists
		with patch("frappe.get_doc", side_effect=lambda dt, nm=None, *a, **k:
		           multi_si if dt == "Sales Invoice" and nm == "ACC-SINV-EXPORT-MULTI" else real_get_doc(dt, nm, *a, **k)), \
				patch("frappe.db.exists", side_effect=lambda dt, nm=None, *a, **k:
				      True if dt == "DocType" and nm == "Sales Invoice" else real_exists(dt, nm, *a, **k)):
			exported = accounting.export_invoices(PROP_UG, TODAY, TODAY)

		row = next(r for r in exported["rows"] if r["invoice_number"] == "INV-THL-26-00777")
		self.assertAlmostEqual(flt(row["taxable"]), 100000.0, places=2)
		self.assertAlmostEqual(flt(row["total_tax"]), 20000.0, places=2)
		self.assertAlmostEqual(flt(row["grand_total"]), 120000.0, places=2)

	def test_backend_translation_csvs_cover_all_languages_without_duplicates(self):
		"""P2: all 7 backend translation CSVs exist, contain the invoice strings once,
		and carry zero vendor leaks."""
		tdir = Path(frappe.get_app_path("kamra", "translations"))
		required = (
			"Tax as per the accounting invoice",
			"Tax Invoice",
			"Invoice",
			"Provisional Bill",
			"Credited",
		)
		vendor_re = re.compile(r"(?<![a-z0-9])(?:kamra|frappe|erpnext|ury)(?![a-z0-9])", re.I)
		for lang in ("ar", "de", "es", "fil", "id", "pt", "zh"):
			with self.subTest(lang=lang):
				path = tdir / f"{lang}.csv"
				self.assertTrue(path.exists(), f"missing backend translation CSV: {path.name}")
				seen: dict[str, str] = {}
				dupes: list[str] = []
				with path.open(encoding="utf-8", newline="") as fh:
					for row in csv.reader(fh):
						if not row or len(row) < 2:
							continue
						k, v = row[0], row[1]
						if k in seen:
							dupes.append(k)
						seen[k] = v
				self.assertEqual(dupes, [], f"{lang}.csv has duplicate keys: {dupes[:5]}")
				for key in required:
					self.assertTrue((seen.get(key) or "").strip(), f"{lang}.csv missing '{key}'")
					self.assertIsNone(vendor_re.search(seen[key]), f"{lang}.csv leaks vendor in '{key}'")


class DayUseBeforeTenTests(AuditTestCase):
	"""Day Use starting or ending before 10:00 (MariaDB timedelta '9:00:00')."""

	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY)
		self.rt = make_room_type(PROP_A, "STD", base_price=2400)
		self.room = make_room(PROP_A, self.rt, "201")
		frappe.db.delete("Rate Plan", {"property": PROP_A, "code": "HRLY9"})
		self.hourly = frappe.get_doc({
			"doctype": "Rate Plan", "property": PROP_A,
			"rate_plan_name": "Hourly Morning", "code": "HRLY9",
			"modifier_type": "Percent", "modifier_value": 0,
			"billing_basis": "Hourly", "hourly_rate": 200, "min_hours": 2,
		}).insert(ignore_permissions=True)

	def test_slot_hours_and_due_out_handle_single_digit_hour_timedelta(self):
		# MariaDB Time returns timedelta(hours=9) -> str "9:00:00"
		row = frappe._dict(
			check_in_date=TODAY,
			check_out_date=TODAY,
			is_day_use=1,
			planned_check_in_time=timedelta(hours=9),
			planned_check_out_time=timedelta(hours=12),
		)
		now_mid = datetime.strptime(f"{TODAY} 10:30:00", "%Y-%m-%d %H:%M:%S")
		with patch("kamra.day_use.now_datetime", return_value=now_mid):
			elapsed, planned = day_use.reservation_slot_hours(row)
		self.assertEqual(planned, 3.0)
		self.assertEqual(elapsed, 1.5)

		# Both start and end before 10:00 (06:00 -> 09:00)
		early = frappe._dict(
			check_in_date=TODAY,
			check_out_date=TODAY,
			is_day_use=1,
			planned_check_in_time=timedelta(hours=6),
			planned_check_out_time=timedelta(hours=9),
		)
		before_9 = datetime.strptime(f"{TODAY} 08:30:00", "%Y-%m-%d %H:%M:%S")
		at_9 = datetime.strptime(f"{TODAY} 09:00:00", "%Y-%m-%d %H:%M:%S")
		with patch("kamra.day_use.now_datetime", return_value=before_9):
			_, early_planned = day_use.reservation_slot_hours(early)
		self.assertEqual(early_planned, 3.0)
		self.assertFalse(day_use.is_due_out(early, now=before_9))
		self.assertTrue(day_use.is_due_out(early, now=at_9))

	def test_day_use_starting_at_0900_posts_to_folio_after_db_reload(self):
		res = make_reservation(
			PROP_A, self.rt, TODAY, TODAY, status="Checked In", room=self.room,
			rate_plan=self.hourly.name, is_day_use=1, auto_price=1,
			planned_check_in_time="09:00:00", planned_check_out_time="12:00:00",
		)
		reloaded = frappe.get_doc("Reservation", res.name)
		during = datetime.strptime(f"{TODAY} 11:30:00", "%Y-%m-%d %H:%M:%S")
		with patch("kamra.day_use.now_datetime", return_value=during):
			folio_mod.post_remaining_nights(reloaded)
		folio = frappe.get_doc("Folio", guest_folio(res.name))
		rooms = [c for c in folio.charges if c.charge_type == "Room"]
		self.assertEqual(len(rooms), 1)
		self.assertIn("3 h", rooms[0].description)

	def test_alert_overdue_day_use_formats_single_digit_hour_cleanly(self):
		res = make_reservation(
			PROP_A, self.rt, TODAY, TODAY, status="Checked In", room=self.room,
			rate_plan=self.hourly.name, is_day_use=1, auto_price=1,
			planned_check_in_time="06:00:00", planned_check_out_time="09:00:00",
		)
		past = datetime.strptime(f"{TODAY} 10:00:00", "%Y-%m-%d %H:%M:%S")
		with patch("kamra.day_use.now_datetime", return_value=past):
			alerted = day_use.alert_overdue_day_use(PROP_A)
		self.assertIn(res.name, alerted)
		subject = frappe.db.get_value(
			"Kamra Notification",
			{"reference_doctype": "Reservation", "reference_name": res.name},
			"subject",
		)
		self.assertIn("(09:00)", subject or "")
		self.assertNotIn("(9:00:)", subject or "")

	def test_format_hhmm_preserves_24_00_and_day_use_end_of_day_stays_valid(self):
		"""P1: 24:00 (end of day) stays '24:00' instead of wrapping to '00:00'."""
		self.assertEqual(day_use.format_hhmm(timedelta(hours=24)), "24:00")
		self.assertEqual(day_use.format_hhmm("24:00:00"), "24:00")
		self.assertEqual(day_use.format_hhmm(timedelta(hours=0)), "00:00")
		self.assertEqual(day_use.format_hhmm(timedelta(hours=9)), "09:00")

		row = frappe._dict(
			check_in_date=TODAY,
			check_out_date=TODAY,
			is_day_use=1,
			planned_check_in_time=timedelta(hours=10),
			planned_check_out_time=timedelta(hours=24),
		)
		evening = datetime.strptime(f"{TODAY} 22:00:00", "%Y-%m-%d %H:%M:%S")
		next_midnight = datetime.strptime(f"{d(1)} 00:00:00", "%Y-%m-%d %H:%M:%S")
		with patch("kamra.day_use.now_datetime", return_value=evening):
			elapsed, planned = day_use.reservation_slot_hours(row)
		self.assertEqual(planned, 14.0)
		self.assertEqual(elapsed, 12.0)
		self.assertFalse(day_use.is_due_out(row, now=evening))
		self.assertTrue(day_use.is_due_out(row, now=next_midnight))
		self.assertEqual(api._stay_slot(TODAY, TODAY, "10:00", "24:00"), ("10:00:00", "24:00:00"))

	def test_all_day_use_api_slices_pad_morning_times_without_slicing_colon(self):
		"""P1: registration_card, room_board, tape_chart, and position_briefing
		all use the shared formatter so morning hours return '09:00', never '9:00:'."""
		room2 = make_room(PROP_A, self.rt, "202")
		# Outgoing overnight stay departing 09:30 on TODAY (room 201)
		res_out = make_reservation(
			PROP_A, self.rt, d(-1), TODAY, status="Checked In", room=self.room,
			planned_check_in_time="14:00:00", planned_check_out_time="09:30:00",
		)
		# Incoming day-use stay arriving 09:00 on TODAY on the same room (conflict!)
		res_in = make_reservation(
			PROP_A, self.rt, TODAY, TODAY, status="Confirmed", room=self.room,
			rate_plan=self.hourly.name, is_day_use=1, auto_price=1,
			planned_check_in_time="09:00:00", planned_check_out_time="13:00:00",
		)
		# Checked-in morning day-use departing 09:00 on room 202
		res_du = make_reservation(
			PROP_A, self.rt, TODAY, TODAY, status="Checked In", room=room2,
			rate_plan=self.hourly.name, is_day_use=1, auto_price=1,
			planned_check_in_time="06:00:00", planned_check_out_time="09:00:00",
		)

		card = api.registration_card(res_in.name)
		self.assertEqual(card["reservation"]["planned_check_in_time"], "09:00")
		self.assertEqual(card["reservation"]["planned_check_out_time"], "13:00")

		with patch("kamra.api.nowdate", return_value=TODAY):
			board = api.room_board(PROP_A)
		tile202 = next(r for r in board["rooms"] if r.name == room2)
		self.assertEqual(tile202["etd"], "09:00")

		tape = api.tape_chart(PROP_A, start_date=d(-1), days=3)
		conflict = next(
			(c for c in tape["conflicts"] if c["out_res"] == res_out.name and c["in_res"] == res_in.name),
			None,
		)
		self.assertIsNotNone(conflict, f"expected morning changeover conflict in {tape['conflicts']}")
		self.assertEqual(conflict["eta"], "09:00")
		self.assertEqual(conflict["etd"], "09:30")

		briefing = api.position_briefing(PROP_A, date=TODAY)
		arr = next(a for a in briefing["arrivals"] if a.name == res_in.name)
		dep_out = next(x for x in briefing["departures"] if x.name == res_out.name)
		dep_du = next(x for x in briefing["departures"] if x.name == res_du.name)
		self.assertEqual(arr["eta"], "09:00")
		self.assertEqual(dep_out["etd"], "09:30")
		self.assertEqual(dep_du["etd"], "09:00")
