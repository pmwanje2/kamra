"""P4: Dated Traces (Stay Instruction) & Multi-Trigger Pop-Up Guest Alerts.

Live Frappe v16 / MariaDB 11.8.9 integration tests verifying:
1. Scheduled traces remain Scheduled until Night Audit rolls the business date
   to trace_date (activating to Open with activated_on stamped).
2. Unresolved past-due traces escalate to Overdue and idempotently create a
   native Frappe ToDo; resolving the trace stamps resolved_by/resolved_at and
   closes the linked ToDo.
3. Multi-trigger Guest Alerts (Info/Warning/Blocking across Reservation,
   Check-In, Check-Out, In-House, Cashier, Night Audit) enforce non-dismissible
   Blocking check-in, check-out, and cashier gates until resolved with a
   supervisor note, and enforce multi-property User Permission isolation.
4. White-label purity on kamra/traces.py (zero vendor strings in user-facing
   messages/code).
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import frappe

from kamra import api
from kamra.folio import open_folio, run_night_audit
from kamra.tests.audit_fixtures import (
	PROP_A,
	PROP_B,
	TODAY,
	AuditTestCase,
	d,
	ensure_user,
	make_guest,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)
from kamra.tests.test_white_label import FORBIDDEN


def _grant_property_permission(user: str, prop: str) -> None:
	frappe.db.delete("User Permission", {"user": user, "allow": "Property"})
	frappe.get_doc({
		"doctype": "User Permission",
		"user": user,
		"allow": "Property",
		"for_value": prop,
	}).insert(ignore_permissions=True)
	frappe.clear_cache(user=user)


class P4TracesAndAlertsTestCase(AuditTestCase):
	properties = (PROP_A, PROP_B)

	def setUp(self):
		super().setUp()
		self.at(
			TODAY,
			"kamra.folio",
			"kamra.business_date",
			"kamra.ledger",
			"kamra.checkout",
			"kamra.api",
		)
		make_property(PROP_A, business_date=TODAY)
		make_property(PROP_B, business_date=TODAY)
		self.rt_a = make_room_type(PROP_A, "STD", base_price=1500)
		self.room_a = make_room(PROP_A, self.rt_a, "101")
		self.rt_b = make_room_type(PROP_B, "STD", base_price=1500)
		self.room_b = make_room(PROP_B, self.rt_b, "201")
		self.desk_a = ensure_user("p4.desk.a@test.local", ["Front Desk"])
		self.desk_b = ensure_user("p4.desk.b@test.local", ["Front Desk"])
		self.admin_a = ensure_user("p4.admin.a@test.local", ["Hotel Admin", "Front Desk"])
		_grant_property_permission(self.desk_a, PROP_A)
		_grant_property_permission(self.desk_b, PROP_B)
		_grant_property_permission(self.admin_a, PROP_A)

	def test_scheduled_trace_activates_on_business_date_roll_and_escalates_overdue_todo(self):
		from kamra import traces

		stay = make_reservation(
			PROP_A,
			self.rt_a,
			TODAY,
			d(4),
			status="Checked In",
			room=self.room_a,
		)
		tomorrow = d(1)
		day_after = d(2)

		frappe.set_user(self.desk_a)  # nosemgrep: frappe-setuser -- test persona
		created = traces.add_trace(
			reservation=stay.name,
			department="Housekeeping",
			instruction="Place hypoallergenic pillows and baby cot",
			trace_date=tomorrow,
			trace_time="09:30:00",
		)
		row_name = created["trace"]["name"]
		self.assertEqual(created["trace"]["trace_status"], "Scheduled")
		self.assertEqual(str(created["trace"]["trace_date"]), tomorrow)
		self.assertFalse(created["trace"].get("todo_ref"))
		self.assertEqual(
			frappe.db.count("ToDo", {"reference_type": "Reservation", "reference_name": stay.name}),
			0,
		)

		# Night Audit closes TODAY -> rolls business date to tomorrow: trace becomes Open
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- run audit
		run_night_audit(PROP_A, business_date=TODAY, mode="system")
		row = frappe.db.get_value(
			"Stay Instruction",
			row_name,
			["trace_status", "activated_on", "todo_ref", "status"],
			as_dict=True,
		)
		self.assertEqual(row.trace_status, "Open")
		self.assertEqual(str(row.activated_on), tomorrow)
		self.assertFalse(row.todo_ref)

		# Advance clock to tomorrow and run Night Audit closing tomorrow -> rolls to day_after:
		# unresolved trace from tomorrow becomes Overdue and creates 1 native ToDo.
		with (
			patch("kamra.folio.nowdate", return_value=tomorrow),
			patch("kamra.business_date.nowdate", return_value=tomorrow),
		):
			run_night_audit(PROP_A, business_date=tomorrow, mode="system")

		row_overdue = frappe.db.get_value(
			"Stay Instruction",
			row_name,
			["trace_status", "todo_ref", "status"],
			as_dict=True,
		)
		self.assertEqual(row_overdue.trace_status, "Overdue")
		self.assertTrue(row_overdue.todo_ref, "Overdue trace must link a native Frappe ToDo")
		todo = frappe.get_doc("ToDo", row_overdue.todo_ref)
		self.assertEqual(todo.status, "Open")
		self.assertEqual(todo.reference_type, "Reservation")
		self.assertEqual(todo.reference_name, stay.name)
		self.assertIn("[Trace Overdue]", todo.description)

		# Re-running escalation is strictly idempotent (still exactly 1 ToDo)
		traces.activate_due_traces(PROP_A, day_after)
		self.assertEqual(
			frappe.db.count("ToDo", {"reference_type": "Reservation", "reference_name": stay.name}),
			1,
		)

		# Resolving trace stamps resolved_by/resolved_at and closes the native ToDo
		frappe.set_user(self.desk_a)  # nosemgrep: frappe-setuser -- test persona
		resolved = traces.resolve_trace(stay.name, row_name, status="Done", note="Delivered to room")
		self.assertEqual(resolved["status"], "Done")
		self.assertEqual(resolved["trace_status"], "Done")
		final_row = frappe.db.get_value(
			"Stay Instruction",
			row_name,
			["status", "trace_status", "resolved_by", "resolved_at", "done_by", "done_on", "todo_ref"],
			as_dict=True,
		)
		self.assertEqual(final_row.status, "Done")
		self.assertEqual(final_row.trace_status, "Done")
		self.assertEqual(final_row.resolved_by, self.desk_a)
		self.assertTrue(final_row.resolved_at)
		self.assertEqual(frappe.db.get_value("ToDo", final_row.todo_ref, "status"), "Closed")

	def test_trace_multi_property_isolation(self):
		from kamra import traces

		stay_a = make_reservation(PROP_A, self.rt_a, TODAY, d(2), status="Confirmed", room=self.room_a)
		frappe.set_user(self.desk_a)  # nosemgrep: frappe-setuser -- test persona
		created = traces.add_trace(stay_a.name, "Concierge", "Airport pickup", trace_date=TODAY)
		row_name = created["trace"]["name"]

		# Property B staff cannot list, add, or resolve traces on Property A
		frappe.set_user(self.desk_b)  # nosemgrep: frappe-setuser -- test persona
		with self.assertRaises(frappe.PermissionError):
			traces.list_traces(reservation=stay_a.name)
		with self.assertRaises(frappe.PermissionError):
			traces.add_trace(stay_a.name, "Front Desk", "Cross-property probe", trace_date=TODAY)
		with self.assertRaises(frappe.PermissionError):
			traces.resolve_trace(stay_a.name, row_name, status="Done")

	def test_multi_trigger_blocking_alert_blocks_checkin_until_resolved_and_respects_property_scope(self):
		from kamra import traces
		from kamra.checkin import checkin_flow

		guest = make_guest("Alerted Guest")
		stay = make_reservation(
			PROP_A,
			self.rt_a,
			TODAY,
			d(2),
			status="Confirmed",
			room=self.room_a,
			guest=guest,
		)

		frappe.set_user(self.desk_a)  # nosemgrep: frappe-setuser -- test persona
		alert = traces.create_alert(
			property=PROP_A,
			reservation=stay.name,
			guest=guest,
			trigger_area="Check-In",
			severity="Blocking",
			alert_code="PASSPORT_CHECK",
			message="Verify diplomatic passport copy with duty manager before key handover",
			display_mode="Until Resolved",
		)
		self.assertEqual(alert["severity"], "Blocking")
		self.assertEqual(alert["trigger_area"], "Check-In")
		self.assertEqual(alert["status"], "Active")

		# Check-in flow exposes the Check-In alert
		flow = checkin_flow(stay.name)
		self.assertTrue(
			any(a["name"] == alert["name"] and a["severity"] == "Blocking" for a in flow.get("alerts") or []),
			f"Expected blocking alert in checkin_flow, got: {flow.get('alerts')}",
		)

		# Blocking alert refuses check_in()
		with self.assertRaises(frappe.ValidationError):
			api.check_in(stay.name)

		# Regular Front Desk clerk CANNOT clear/resolve a Blocking alert
		with self.assertRaises(frappe.PermissionError):
			traces.resolve_alert(alert["name"], resolution_note="Attempting desk override")
		with self.assertRaises(frappe.PermissionError):
			traces.acknowledge_alert(alert["name"], reason="Attempting desk dismiss")

		# Property B staff CANNOT read, create, or resolve Property A alerts
		frappe.set_user(self.desk_b)  # nosemgrep: frappe-setuser -- test persona
		with self.assertRaises(frappe.PermissionError):
			traces.list_alerts(property=PROP_A, reservation=stay.name)
		with self.assertRaises(frappe.PermissionError):
			traces.create_alert(
				property=PROP_A,
				reservation=stay.name,
				trigger_area="Reservation",
				severity="Info",
				message="Unauthorized cross-property alert",
			)

		# Hotel Admin at Property A must supply a non-empty reason to resolve a Blocking alert
		frappe.set_user(self.admin_a)  # nosemgrep: frappe-setuser -- test persona
		with self.assertRaises(frappe.ValidationError):
			traces.resolve_alert(alert["name"], resolution_note="   ")

		cleared = traces.resolve_alert(alert["name"], resolution_note="Passport verified by duty manager")
		self.assertEqual(cleared["status"], "Resolved")
		self.assertEqual(cleared["resolved_by"], self.admin_a)

		# Now check_in() succeeds cleanly
		frappe.set_user(self.desk_a)  # nosemgrep: frappe-setuser -- test persona
		res_out = api.check_in(stay.name)
		self.assertTrue(res_out["ok"])
		self.assertEqual(frappe.db.get_value("Reservation", stay.name, "status"), "Checked In")

	def test_all_six_triggers_and_checkout_cashier_gates(self):
		from kamra import traces
		from kamra.checkout import checkout_context

		guest = make_guest("Trigger Matrix Guest")
		stay = make_reservation(
			PROP_A,
			self.rt_a,
			TODAY,
			d(2),
			status="Checked In",
			room=self.room_a,
			guest=guest,
		)
		folio_name = frappe.db.get_value("Folio", {"reservation": stay.name, "folio_type": "Guest"}) or open_folio(stay)

		frappe.set_user(self.admin_a)  # nosemgrep: frappe-setuser -- test persona
		for trigger, sev in (
			("Reservation", "Info"),
			("In-House", "Warning"),
			("Check-Out", "Blocking"),
			("Cashier", "Blocking"),
			("Night Audit", "Warning"),
		):
			created = traces.create_alert(
				property=PROP_A,
				reservation=stay.name,
				guest=guest,
				trigger_area=trigger,
				severity=sev,
				message=f"{trigger} {sev} notice",
			)
			self.assertEqual(created["trigger_area"], trigger)
			self.assertEqual(created["severity"], sev)

		# Reservation detail includes alerts summary and traces
		detail = api.reservation_detail(stay.name)
		self.assertIn("alerts", detail)
		self.assertIn("traces", detail)
		# Reservation trigger sees Reservation (Info) + In-House (Warning) because stay is Checked In
		res_triggers = {a["trigger_area"] for a in detail["alerts"]["items"]}
		self.assertIn("Reservation", res_triggers)
		self.assertIn("In-House", res_triggers)

		# guest_alert remains backward-compatible and includes Reservation alerts
		ga = api.guest_alert(guest=guest, property=PROP_A, reservation=stay.name)
		self.assertEqual(ga["blacklisted"], 0)
		self.assertTrue(any(a["trigger_area"] == "Reservation" for a in ga.get("alerts") or []))

		# Check-Out context surfaces Blocking Check-Out alert + In-House alert
		ctx = checkout_context(stay.name)
		co_alerts = ctx.get("alerts") or []
		self.assertTrue(any(a["trigger_area"] == "Check-Out" and a["severity"] == "Blocking" for a in co_alerts))
		self.assertTrue(any(b["code"] == "blocking_alert" for b in ctx.get("blockers") or []))

		# Blocking Check-Out alert refuses check_out until resolved
		frappe.flags.allow_unsettled_checkout = True
		self.addCleanup(setattr, frappe.flags, "allow_unsettled_checkout", False)
		with self.assertRaises(frappe.ValidationError):
			api.check_out(stay.name)

		# Blocking Cashier alert refuses add_folio_charge on that stay's folio
		with self.assertRaises(frappe.ValidationError):
			api.add_folio_charge(folio_name, "Misc", "Laundry", 50)

		# Night Audit trigger returns the Night Audit alert
		na_alerts = traces.check_point_alerts("Night Audit", PROP_A, reservation=stay.name, guest=guest)
		self.assertTrue(any(a["trigger_area"] == "Night Audit" for a in na_alerts))

		# Warning alert can be acknowledged by Front Desk
		in_house_alert = next(a for a in detail["alerts"]["items"] if a["trigger_area"] == "In-House")
		frappe.set_user(self.desk_a)  # nosemgrep: frappe-setuser -- test persona
		ack = traces.acknowledge_alert(in_house_alert["name"])
		self.assertEqual(ack["status"], "Acknowledged")
		self.assertEqual(ack["acknowledged_by"], self.desk_a)

	def test_traces_module_has_zero_vendor_literals(self):
		traces_path = Path(__file__).resolve().parents[1] / "traces.py"
		self.assertTrue(traces_path.exists(), "kamra/traces.py must exist")
		tree = ast.parse(traces_path.read_text(encoding="utf-8"))
		findings = []
		for node in ast.walk(tree):
			if isinstance(node, ast.Call):
				call = ast.unparse(node.func)
				if call in {"_", "frappe._", "frappe.throw", "frappe.msgprint"}:
					for arg in node.args[:1]:
						for part in ast.walk(arg):
							if isinstance(part, ast.Constant) and isinstance(part.value, str) and FORBIDDEN.search(part.value):
								findings.append(f"traces.py:{part.lineno}: {part.value}")
		self.assertEqual(findings, [])
