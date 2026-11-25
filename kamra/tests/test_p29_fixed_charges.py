"""RED→GREEN tests for P29: Recurring Fixed Charges (all 5 frequencies, window routing, RBAC & property scope)."""

from __future__ import annotations

from decimal import Decimal

import frappe
from frappe.utils import add_days, getdate

from kamra import folio
from kamra.ledger import ensure_default_transaction_codes
from kamra.tests.audit_fixtures import (
	TODAY,
	AuditTestCase,
	ensure_user,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

PROP_FC = "UT Fixed Charges Hotel"
PROP_FC_OTHER = "UT Fixed Charges Other"


def _ensure_txn_code(code: str, description: str, charge_type: str, prop: str | None = None):
	if frappe.db.exists("Transaction Code", code):
		return code
	frappe.get_doc({
		"doctype": "Transaction Code",
		"code": code,
		"description": description,
		"property": prop,
		"txn_type": "Revenue",
		"revenue_group": "Other",
		"subgroup": charge_type,
		"charge_type": charge_type,
		"disabled": 0,
	}).insert(ignore_permissions=True)
	return code


class TestP29FixedCharges(AuditTestCase):
	properties = (PROP_FC, PROP_FC_OTHER)

	def setUp(self):
		super().setUp()
		self.at(TODAY)
		ensure_default_transaction_codes()
		_ensure_txn_code("3300", "Parking", "Misc")
		_ensure_txn_code("3400", "Resort Fee", "Misc")
		make_property(
			PROP_FC,
			country="United States",
			currency="USD",
			business_date=TODAY,
			gst_mode="Fixed",
			gst_rate_low=0,
			gst_rate_high=0,
			rates_include_tax=0,
		)
		make_property(PROP_FC_OTHER, country="United States", currency="USD", business_date=TODAY)
		self.rt = make_room_type(PROP_FC, "STD", base_price=150, adults_capacity=2)
		self.room = make_room(PROP_FC, self.rt, "201")
		self.day0 = str(getdate(TODAY))  # 2031-03-12 (Wednesday)
		self.day1 = str(add_days(self.day0, 1))  # Thursday (Stay Day 2)
		self.day2 = str(add_days(self.day0, 2))  # Friday (Stay Day 3)
		self.day3 = str(add_days(self.day0, 3))  # Saturday (Departure)

	def test_fixed_charges_all_five_frequencies_window_routing_and_idempotency(self):
		"""Daily, Weekly, Stay Day N, On Arrival, and On Departure fixed charges post
		deterministically onto target folio windows with fc: idempotency keys and zero duplicates."""
		from kamra import fixed_charges

		# Start Confirmed so we can add On Arrival BEFORE check-in
		res = make_reservation(
			PROP_FC, self.rt, self.day0, self.day3,
			status="Confirmed", room=self.room, adults=2,
		)
		# 1) On Arrival -> routed to Window 2 (Extra folio)
		arrival_row = fixed_charges.save_fixed_charge(res.name, {
			"transaction_code": "3200",
			"charge_type": "Taxi",
			"description": "Airport Pickup",
			"qty": 1,
			"unit_price": 45.00,
			"frequency": "On Arrival",
			"start_date": self.day0,
			"end_date": self.day3,
			"window_no": 2,
		})["row"]

		# 2) Daily -> Window 1 (Guest folio), qty=2
		daily_row = fixed_charges.save_fixed_charge(res.name, {
			"transaction_code": "3300",
			"charge_type": "Misc",
			"description": "Valet Parking",
			"qty": 2,
			"unit_price": 15.00,
			"frequency": "Daily",
			"start_date": self.day0,
			"end_date": self.day2,
			"window_no": 1,
		})["row"]

		# 3) Weekly (Thursday = day1) -> Window 1
		weekly_row = fixed_charges.save_fixed_charge(res.name, {
			"transaction_code": "3000",
			"charge_type": "Laundry",
			"description": "Weekly Pressing",
			"qty": 1,
			"unit_price": 25.00,
			"frequency": "Weekly",
			"day_of_week": "Thursday",
			"start_date": self.day0,
			"end_date": self.day2,
			"window_no": 1,
		})["row"]

		# 4) Stay Day 2 (day1) -> Window 1
		day2_row = fixed_charges.save_fixed_charge(res.name, {
			"transaction_code": "2000",
			"charge_type": "Food & Beverage",
			"description": "Anniversary Cake",
			"qty": 1,
			"unit_price": 35.00,
			"frequency": "Stay Day N",
			"stay_day_n": 2,
			"start_date": self.day0,
			"end_date": self.day2,
			"window_no": 1,
		})["row"]

		# 5) On Departure -> Window 1
		dep_row = fixed_charges.save_fixed_charge(res.name, {
			"transaction_code": "3400",
			"charge_type": "Misc",
			"description": "Departure Cleaning & Resort",
			"qty": 1,
			"unit_price": 40.00,
			"frequency": "On Departure",
			"start_date": self.day0,
			"end_date": self.day3,
			"window_no": 1,
		})["row"]

		# Verify Reservation wasn't blocked by locked-nights guard & all 5 rows list cleanly
		listed = fixed_charges.list_fixed_charges(res.name)
		self.assertEqual(len(listed["rows"]), 5)

		# Check-in fires open_folio -> On Arrival posts onto Window 2 (Extra folio, NOT Guest W1)
		res.reload()
		res.status = "Checked In"
		res.save(ignore_permissions=True)

		extra_folio = frappe.db.get_value(
			"Folio", {"reservation": res.name, "folio_type": "Extra"}, "name",
		)
		guest_folio = frappe.db.get_value(
			"Folio", {"reservation": res.name, "folio_type": "Guest"}, "name",
		)
		self.assertIsNotNone(extra_folio)
		extra_charges = frappe.get_doc("Folio", extra_folio).charges
		self.assertEqual(len(extra_charges), 1)
		self.assertEqual(extra_charges[0].idempotency_key, f"fc:{res.name}:{arrival_row['name']}:arrival")
		self.assertEqual(Decimal(str(extra_charges[0].amount)), Decimal("45.00"))

		# Re-invoking arrival posting is 100% idempotent
		again_arr = fixed_charges.post_arrival_fixed_charges(res)
		self.assertEqual(again_arr["count"], 0)

		# Night Audit Day 0 (Wednesday): posts Daily Parking (2 * 15 = 30.00) only
		audit0 = folio.run_night_audit(PROP_FC, self.day0)
		self.assertEqual(audit0["fixed_charges_posted"], 1)
		self.assertEqual(Decimal(str(audit0["fixed_charges_amount"])), Decimal("30.00"))

		# Re-invoking post_fixed_charges_for_date on Day 0 posts 0 duplicates
		dup0 = fixed_charges.post_fixed_charges_for_date(res, self.day0, trigger="night_audit")
		self.assertEqual(dup0["count"], 0)

		# Night Audit Day 1 (Thursday = Stay Day 2): posts Daily (30) + Weekly (25) + Stay Day 2 (35) = 90.00
		self.at(self.day1)
		audit1 = folio.run_night_audit(PROP_FC, self.day1)
		self.assertEqual(audit1["fixed_charges_posted"], 3)
		self.assertEqual(Decimal(str(audit1["fixed_charges_amount"])), Decimal("90.00"))

		# Verify deterministic idempotency keys on Guest folio
		gdoc = frappe.get_doc("Folio", guest_folio)
		fc_keys = {c.idempotency_key for c in gdoc.charges if (c.idempotency_key or "").startswith("fc:")}
		self.assertIn(f"fc:{res.name}:{daily_row['name']}:{self.day0}", fc_keys)
		self.assertIn(f"fc:{res.name}:{daily_row['name']}:{self.day1}", fc_keys)
		self.assertIn(f"fc:{res.name}:{weekly_row['name']}:{self.day1}", fc_keys)
		self.assertIn(f"fc:{res.name}:{day2_row['name']}:{self.day1}", fc_keys)

		# Delete after posting soft-deactivates instead of deleting
		del_posted = fixed_charges.delete_fixed_charge(res.name, daily_row["name"])
		self.assertEqual(del_posted["action"], "deactivated")
		self.assertEqual(frappe.db.get_value("Reservation Fixed Charge", daily_row["name"], "active"), 0)

		# Unposted On Departure posts on post_remaining_nights (checkout path) idempotently
		folio.post_remaining_nights(res)
		gdoc.reload()
		dep_lines = [c for c in gdoc.charges if c.idempotency_key == f"fc:{res.name}:{dep_row['name']}:departure"]
		self.assertEqual(len(dep_lines), 1)
		self.assertEqual(Decimal(str(dep_lines[0].amount)), Decimal("40.00"))

		# Calling post_remaining_nights a second time never duplicates On Departure
		folio.post_remaining_nights(res)
		gdoc.reload()
		dep_lines_2 = [c for c in gdoc.charges if c.idempotency_key == f"fc:{res.name}:{dep_row['name']}:departure"]
		self.assertEqual(len(dep_lines_2), 1)

	def test_fixed_charge_delete_unposted_and_property_rbac(self):
		"""Unposted fixed charge deletes cleanly; unauthorized role or foreign property user gets PermissionError."""
		from kamra import fixed_charges

		res = make_reservation(PROP_FC, self.rt, self.day0, self.day2, status="Confirmed", room=self.room)
		created = fixed_charges.save_fixed_charge(res.name, {
			"transaction_code": "3300",
			"description": "Temp Parking",
			"qty": 1,
			"unit_price": 10.00,
			"frequency": "Daily",
			"start_date": self.day0,
			"end_date": self.day1,
			"window_no": 1,
		})["row"]
		del_res = fixed_charges.delete_fixed_charge(res.name, created["name"])
		self.assertEqual(del_res["action"], "deleted")
		self.assertFalse(frappe.db.exists("Reservation Fixed Charge", created["name"]))

		# Housekeeping role cannot save fixed charges
		hk_user = ensure_user("fc.hk@test.local", ["Housekeeping"])
		frappe.set_user(hk_user)  # nosemgrep: frappe-setuser -- testing RBAC refusal
		with self.assertRaises(frappe.PermissionError):
			fixed_charges.save_fixed_charge(res.name, {
				"transaction_code": "3300",
				"description": "Unauthorized",
				"qty": 1,
				"unit_price": 5.00,
				"frequency": "Daily",
				"start_date": self.day0,
				"end_date": self.day1,
			})

		# Front Desk user restricted to PROP_FC_OTHER gets PermissionError on PROP_FC
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- setup foreign user
		other_fd = ensure_user("fc.other@test.local", ["Front Desk"])
		if not frappe.db.exists("User Permission", {"user": other_fd, "allow": "Property", "for_value": PROP_FC_OTHER}):
			frappe.get_doc({
				"doctype": "User Permission",
				"user": other_fd,
				"allow": "Property",
				"for_value": PROP_FC_OTHER,
				"apply_to_all_doctypes": 1,
			}).insert(ignore_permissions=True)
		frappe.clear_cache(user=other_fd)

		frappe.set_user(other_fd)  # nosemgrep: frappe-setuser -- testing property scope isolation
		with self.assertRaises(frappe.PermissionError):
			fixed_charges.list_fixed_charges(res.name)
