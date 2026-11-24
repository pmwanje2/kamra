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

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import frappe
from frappe.utils import flt

from kamra import api, day_use
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
	folio_name: str,
	net_total: float,
	total_taxes: float,
	grand_total: float,
	items: list[dict],
	taxes: list[dict],
	docstatus: int = 1,
):
	return frappe._dict(
		doctype="Sales Invoice",
		name=name,
		kamra_ref=folio_name,
		docstatus=docstatus,
		net_total=net_total,
		total_taxes_and_charges=total_taxes,
		grand_total=grand_total,
		items=[frappe._dict(it) for it in items],
		taxes=[frappe._dict(tx) for tx in taxes],
	)


class BridgedFolioInvoiceVatTests(AuditTestCase):
	"""Closed bridged Tax Invoice reads its tax breakdown from the linked ERP
	Sales Invoice, or falls back to 'Tax as per the accounting invoice'."""

	properties = (PROP_A, PROP_UG)

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_bridge_field("Folio", "sales_invoice")

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
	):
		res = make_reservation(PROP_UG, self.rt, d(-1), d(0), status="Checked In", room=self.room)
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
				"invoice_number": "INV-THL-26-00003",
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
