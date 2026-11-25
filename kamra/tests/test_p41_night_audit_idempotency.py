"""RED→GREEN tests for P41: Night Audit Step 9 Idempotent Auto-Post & Partial Crash Retry Safety."""

from __future__ import annotations

import json
from decimal import Decimal

import frappe
from frappe.utils import add_days, getdate

from kamra import folio
from kamra.ledger import ensure_default_transaction_codes
from kamra.tests.audit_fixtures import (
	TODAY,
	AuditTestCase,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

PROP_P41 = "UT P41 Night Audit Hotel"


def _ensure_txn_code(code: str, description: str, charge_type: str, txn_type: str = "Revenue"):
	if frappe.db.exists("Transaction Code", code):
		return code
	frappe.get_doc({
		"doctype": "Transaction Code",
		"code": code,
		"description": description,
		"txn_type": txn_type,
		"revenue_group": "Other",
		"subgroup": charge_type,
		"charge_type": charge_type,
		"disabled": 0,
	}).insert(ignore_permissions=True)
	return code


class TestP41NightAuditIdempotency(AuditTestCase):
	properties = (PROP_P41,)

	def setUp(self):
		super().setUp()
		self.at(TODAY)
		ensure_default_transaction_codes()
		_ensure_txn_code("3300", "Parking", "Misc")
		_ensure_txn_code("3400", "Resort Fee", "Misc")
		_ensure_txn_code("1410", "Package Breakage", "Misc", txn_type="Package")
		make_property(
			PROP_P41,
			country="United States",
			currency="USD",
			business_date=TODAY,
			gst_mode="Fixed",
			gst_rate_low=0,
			gst_rate_high=0,
			rates_include_tax=0,
		)
		self.rt = make_room_type(PROP_P41, "STD", base_price=200, adults_capacity=2)
		self.r1 = make_room(PROP_P41, self.rt, "301")
		self.r2 = make_room(PROP_P41, self.rt, "302")
		self.day0 = str(getdate(TODAY))
		self.day1 = str(add_days(self.day0, 1))
		self.day2 = str(add_days(self.day0, 2))

	def test_p41_partial_crash_retry_never_double_posts_package_splits_or_fixed_charges(self):
		"""Simulate a partial Night Audit failure after posting stay 1 (package + two Misc fixed
		charges on the same date), leave the run as Draft on the same business date, and re-run
		run_night_audit: ZERO duplicate Folio Charges or Package Allowances are created."""
		from kamra import fixed_charges

		frappe.get_doc({
			"doctype": "Holiday Package",
			"property": PROP_P41,
			"package_name": "Resort Escape 2N",
			"room_type": self.rt,
			"start_date": self.day0,
			"end_date": self.day1,
			"package_nights": 2,
			"price_2_pax": 500.00,
			"elements": [
				{
					"element_name": "Breakfast",
					"element_type": "Meal",
					"transaction_code": "1100",
					"charge_type": "Meal Plan",
					"allowance_basis": "Per Room Night",
					"unit_allowance": 50.00,
					"posting_rhythm": "Every Night",
					"included_in_rate": 1,
				},
			],
		}).insert(ignore_permissions=True)

		res1 = make_reservation(PROP_P41, self.rt, self.day0, self.day2, status="Checked In", room=self.r1)
		res2 = make_reservation(PROP_P41, self.rt, self.day0, self.day2, status="Checked In", room=self.r2)

		# Two DISTINCT Misc fixed charges on the SAME date for res1 (proves idempotency_key
		# never collides on shared charge_type="Misc"!)
		fixed_charges.save_fixed_charge(res1.name, {
			"transaction_code": "3300",
			"charge_type": "Misc",
			"description": "Garage Parking",
			"qty": 1,
			"unit_price": 20.00,
			"frequency": "Daily",
			"start_date": self.day0,
			"end_date": self.day1,
		})
		fixed_charges.save_fixed_charge(res1.name, {
			"transaction_code": "3400",
			"charge_type": "Misc",
			"description": "Daily Resort Fee",
			"qty": 1,
			"unit_price": 30.00,
			"frequency": "Daily",
			"start_date": self.day0,
			"end_date": self.day1,
		})
		fixed_charges.save_fixed_charge(res2.name, {
			"transaction_code": "3300",
			"charge_type": "Misc",
			"description": "Garage Parking",
			"qty": 1,
			"unit_price": 20.00,
			"frequency": "Daily",
			"start_date": self.day0,
			"end_date": self.day1,
		})

		# Simulate partial run: stay 1 already posted before crash; audit run stays Draft on day0
		folio.post_room_night(res1, self.day0)
		fixed_charges.post_fixed_charges_for_date(res1, self.day0, trigger="night_audit")
		frappe.get_doc({
			"doctype": "Night Audit Run",
			"property": PROP_P41,
			"business_date": self.day0,
			"status": "Draft",
		}).insert(ignore_permissions=True)

		# Full recovery run_night_audit on day0 completes stay 2 without duplicating stay 1
		out = folio.run_night_audit(PROP_P41, self.day0)
		self.assertEqual(out["status"], "Completed")
		self.assertEqual(out["room_charges_posted"], 1)  # only stay 2 remained unposted
		self.assertEqual(out["fixed_charges_posted"], 1)  # only stay 2's parking remained unposted
		self.assertEqual(out["package_elements_posted"], 1)  # only stay 2's breakfast remained unposted

		# Verify report_pack summary carries all P41 itemised counters
		audit_doc = frappe.get_doc("Night Audit Run", out["audit"])
		pack = json.loads(audit_doc.report_pack)
		self.assertIn("fixed_charges_posted", pack["run"])
		self.assertIn("fixed_charges_amount", pack["run"])
		self.assertIn("package_elements_posted", pack["run"])
		self.assertIn("package_breakage_total", pack["run"])

		# Verify stay 1 has EXACTLY 4 lines (Room Net 200 + Breakfast 50 + Parking 20 + Resort Fee 30 = 300)
		f1 = frappe.get_doc("Folio", frappe.db.get_value("Folio", {"reservation": res1.name, "folio_type": "Guest"}))
		self.assertEqual(len(f1.charges), 4)
		self.assertEqual(Decimal(str(f1.charges_total)), Decimal("300.00"))

		# Verify stay 2 has EXACTLY 3 lines (Room Net 200 + Breakfast 50 + Parking 20 = 270)
		f2 = frappe.get_doc("Folio", frappe.db.get_value("Folio", {"reservation": res2.name, "folio_type": "Guest"}))
		self.assertEqual(len(f2.charges), 3)
		self.assertEqual(Decimal(str(f2.charges_total)), Decimal("270.00"))

		# Verify exactly 2 Reservation Package Allowance rows total (1 per stay, zero duplicates)
		allowances = frappe.get_all("Reservation Package Allowance", filters={"property": PROP_P41, "business_date": self.day0})
		self.assertEqual(len(allowances), 2)

		# Simulate operator resetting audit to Draft + rolling business_date back to day0 and re-running
		frappe.db.set_value("Night Audit Run", out["audit"], "status", "Draft", update_modified=False)
		frappe.db.set_value("Property", PROP_P41, "business_date", self.day0, update_modified=False)
		frappe.clear_document_cache("Property", PROP_P41)

		rerun = folio.run_night_audit(PROP_P41, self.day0)
		self.assertEqual(rerun["room_charges_posted"], 0)
		self.assertEqual(rerun["fixed_charges_posted"], 0)
		self.assertEqual(rerun["package_elements_posted"], 0)

		f1.reload()
		f2.reload()
		self.assertEqual(len(f1.charges), 4)
		self.assertEqual(len(f2.charges), 3)
		allowances_after = frappe.get_all("Reservation Package Allowance", filters={"property": PROP_P41, "business_date": self.day0})
		self.assertEqual(len(allowances_after), 2)
