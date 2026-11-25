"""P36 live room discrepancy, turndown, and task assignment behavior."""

import frappe

from kamra import housekeeping
from kamra.tests.audit_fixtures import (
	PROP_A, PROP_B, TODAY, AuditTestCase, d, ensure_user, make_property,
	make_reservation, make_room, make_room_type,
)


class TestP36HousekeepingDiscrepancies(AuditTestCase):
	properties = (PROP_A, PROP_B)

	def setUp(self):
		super().setUp()
		make_property(PROP_A, business_date=TODAY)
		make_property(PROP_B, business_date=TODAY)
		self.rt = make_room_type(PROP_A)
		self.occupied = make_room(PROP_A, self.rt, "101")
		self.vacant = make_room(PROP_A, self.rt, "102")
		self.other = make_room(PROP_B, make_room_type(PROP_B), "201")
		make_reservation(PROP_A, self.rt, TODAY, d(1), room=self.occupied, status="Checked In")
		self.desk = self.restrict("p36.desk@test.local", ["Front Desk"], PROP_A)
		self.staff = self.restrict("p36.hk@test.local", ["Housekeeping"], PROP_A)
		self.foreign_staff = self.restrict("p36.other@test.local", ["Housekeeping"], PROP_B)

	def restrict(self, email, roles, prop):
		user = ensure_user(email, roles)
		frappe.db.delete("User Permission", {"user": user, "allow": "Property"})
		frappe.get_doc(doctype="User Permission", user=user, allow="Property",
		               for_value=prop).insert(ignore_permissions=True)
		frappe.clear_cache(user=user)
		return user

	def test_skip_and_sleep_are_scoped_and_resolution_is_audited(self):
		frappe.set_user(self.staff)
		housekeeping.report_hk_occupancy(self.occupied, "Vacant")
		housekeeping.report_hk_occupancy(self.vacant, "Occupied")
		with self.assertRaises(frappe.PermissionError):
			housekeeping.report_hk_occupancy(self.other, "Occupied")
		with self.assertRaises(frappe.PermissionError):
			housekeeping.get_hk_discrepancies(PROP_B)
		rows = housekeeping.get_hk_discrepancies(PROP_A)
		self.assertEqual({row["room"]: row["discrepancy"] for row in rows}, {
			self.occupied: "Skip", self.vacant: "Sleep",
		})
		frappe.set_user(self.desk)
		housekeeping.resolve_hk_discrepancy(self.occupied, "Guest stepped out temporarily")
		self.assertEqual([row["discrepancy"] for row in housekeeping.get_hk_discrepancies(PROP_A)],
		                 ["Sleep"])
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"reference_name": self.occupied, "action_type": "resolve_hk_discrepancy",
		}))

	def test_auto_assign_counts_turndown_credits_and_excludes_foreign_staff(self):
		housekeeping.set_turndown_status(self.occupied, "Pending")
		queue = housekeeping.get_turndown_queue(PROP_A)
		self.assertEqual(queue[0]["turndown_status"], "Pending")
		with self.assertRaises(frappe.PermissionError):
			housekeeping.auto_assign_housekeeping_tasks(PROP_A, TODAY, [self.foreign_staff], 1)
		result = housekeeping.auto_assign_housekeeping_tasks(PROP_A, TODAY, [self.staff], 1)
		self.assertEqual(result["loads"][0]["credits"], 0.5)
		self.assertEqual(len(result["assigned"]), 1)
		retry = housekeeping.auto_assign_housekeeping_tasks(PROP_A, TODAY, [self.staff], 1)
		self.assertEqual(retry["assigned"], [])
		self.assertEqual(retry["loads"][0]["credits"], 0.5)
