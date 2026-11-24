"""Dated inter-departmental stay traces and multi-trigger guest alerts.

Implements OPERA Cloud-aligned dated Traces on Stay Instruction rows (with
Night Audit business-date activation and native ToDo overdue escalation) and
multi-trigger Guest Alerts (Info, Warning, Blocking) across Reservation,
Check-In, Check-Out, In-House, Cashier, and Night Audit touchpoints.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, now_datetime

from kamra.authz import (
	ADMIN,
	assert_property_access,
	require_roles,
	restricted_properties,
)
from kamra.business_date import get_business_date

TRACE_DEPARTMENTS = (
	"Front Desk",
	"Housekeeping",
	"F&B",
	"Concierge",
	"Transport",
	"Maintenance",
	"Security",
)

ALERT_TRIGGERS = (
	"Reservation",
	"Check-In",
	"Check-Out",
	"In-House",
	"Cashier",
	"Night Audit",
)

ALERT_SEVERITIES = ("Info", "Warning", "Blocking")
ALERT_DISPLAY_MODES = ("Every Access", "Once Per Business Day", "Until Resolved")

_SEVERITY_RANK = {"Blocking": 0, "Warning": 1, "Info": 2}
_BLOCKING_SUPERVISOR_ROLES = {"Hotel Admin", "Front Desk Supervisor", *ADMIN}

_TRACE_READ_ROLES = (
	"Front Desk",
	"Housekeeping",
	"Housekeeping Supervisor",
	"Night Auditor",
	"Finance",
	"F&B Manager",
	"Kamra Agent",
)

_ALERT_READ_ROLES = (
	"Front Desk",
	"Housekeeping Supervisor",
	"Night Auditor",
	"Finance",
	"Hotel Admin",
	"Kamra Agent",
)

_ALERT_WRITE_ROLES = (
	"Front Desk",
	"Night Auditor",
	"Finance",
	"Hotel Admin",
	"Kamra Agent",
)

_DEPT_ROLE_MAP = {
	"Housekeeping": "Housekeeping Supervisor",
	"F&B": "F&B Manager",
	"Front Desk": "Front Desk",
	"Concierge": "Front Desk",
	"Transport": "Front Desk",
	"Maintenance": "Hotel Admin",
	"Security": "Hotel Admin",
}


def can_override_blocking_alert(user: str | None = None) -> bool:
	who = user or frappe.session.user
	if who == "Administrator":
		return True
	return bool(_BLOCKING_SUPERVISOR_ROLES & set(frappe.get_roles(who)))


def _compute_trace_status(status: str | None, trace_date, business_date) -> str:
	if (status or "Open") == "Done":
		return "Done"
	if not trace_date:
		return "Open"
	td = getdate(trace_date)
	bd = getdate(business_date)
	if td > bd:
		return "Scheduled"
	if td < bd:
		return "Overdue"
	return "Open"


def _format_trace_row(row: dict, business_date: str) -> dict:
	tdate = str(row.get("trace_date")) if row.get("trace_date") else None
	status = row.get("status") or "Open"
	stored = row.get("trace_status") or _compute_trace_status(status, tdate, business_date)
	if status == "Done":
		tstatus = "Done"
	elif tdate and getdate(tdate) < getdate(business_date):
		tstatus = "Overdue"
	elif tdate and getdate(tdate) > getdate(business_date):
		tstatus = "Scheduled"
	else:
		tstatus = stored if stored in ("Scheduled", "Open", "Overdue", "Done") else "Open"
	return {
		"name": row.get("name"),
		"reservation": row.get("parent"),
		"property": row.get("property"),
		"guest_name": row.get("guest_name"),
		"room": row.get("room"),
		"room_number": row.get("room_number"),
		"check_in_date": str(row.get("check_in_date")) if row.get("check_in_date") else None,
		"check_out_date": str(row.get("check_out_date")) if row.get("check_out_date") else None,
		"department": row.get("department"),
		"instruction": row.get("instruction"),
		"status": status,
		"trace_date": tdate,
		"trace_time": str(row.get("trace_time")) if row.get("trace_time") not in (None, "") else None,
		"trace_status": tstatus,
		"activated_on": str(row.get("activated_on")) if row.get("activated_on") else None,
		"done_by": row.get("done_by"),
		"done_on": str(row.get("done_on")) if row.get("done_on") else None,
		"resolved_by": row.get("resolved_by") or row.get("done_by"),
		"resolved_at": str(row.get("resolved_at") or row.get("done_on"))
		if (row.get("resolved_at") or row.get("done_on"))
		else None,
		"todo_ref": row.get("todo_ref"),
		"owner": row.get("owner"),
		"creation": str(row.get("creation")) if row.get("creation") else None,
		"is_overdue": 1 if tstatus == "Overdue" else 0,
	}


def _department_lead(property: str, department: str) -> str | None:
	role = _DEPT_ROLE_MAP.get(department, "Front Desk")
	for candidate_role in (role, "Hotel Admin", "Front Desk"):
		users = frappe.get_all(
			"Has Role",
			filters={"role": candidate_role, "parenttype": "User"},
			pluck="parent",
		)
		if not users:
			continue
		enabled = frappe.get_all(
			"User",
			filters=[["name", "in", users], ["enabled", "=", 1], ["name", "!=", "Guest"]],
			pluck="name",
			order_by="creation asc",
		)
		for user in enabled:
			if user in ("Guest", "Administrator"):
				continue
			allowed = restricted_properties(user)
			if allowed is None or property in allowed:
				return user
	return None


@frappe.whitelist()
@require_roles(*_TRACE_READ_ROLES)
def list_traces(
	reservation: str | None = None,
	property: str | None = None,
	business_date: str | None = None,
	status: str | None = None,
	department: str | None = None,
) -> list[dict]:
	"""List dated traces for a reservation or property with strict property scope."""
	prop = property
	if reservation:
		res_prop = frappe.db.get_value("Reservation", reservation, "property")
		if not res_prop:
			frappe.throw(
				_("Reservation {0} not found.").format(reservation),
				frappe.DoesNotExistError,
			)
		assert_property_access(res_prop)
		prop = res_prop
	elif prop:
		assert_property_access(prop)
	else:
		allowed = restricted_properties()
		if allowed is not None and not allowed:
			return []
		if allowed is not None and len(allowed) == 1:
			prop = next(iter(allowed))

	allowed_props = None
	if not prop:
		allowed = restricted_properties()
		if allowed is not None:
			allowed_props = tuple(sorted(allowed)) or ("",)

	rows = frappe.db.sql(
		"""
		SELECT si.name, si.parent, si.department, si.instruction, si.status,
		       si.trace_date, si.trace_time, si.trace_status, si.activated_on,
		       si.done_by, si.done_on, si.resolved_by, si.resolved_at, si.todo_ref,
		       si.owner, si.creation,
		       r.property, r.guest_name, r.room, r.check_in_date, r.check_out_date,
		       rm.room_number
		FROM `tabStay Instruction` si
		JOIN `tabReservation` r ON r.name = si.parent
		LEFT JOIN `tabRoom` rm ON rm.name = r.room
		WHERE si.parenttype = 'Reservation'
		  AND si.parentfield = 'instructions'
		  AND (%(has_reservation)s = 0 OR si.parent = %(reservation)s)
		  AND (%(has_property)s = 0 OR r.property = %(property)s)
		  AND (%(has_allowed)s = 0 OR r.property IN %(allowed_props)s)
		  AND (%(has_department)s = 0 OR si.department = %(department)s)
		ORDER BY COALESCE(si.trace_date, DATE(si.creation)) ASC, si.idx ASC
		""",
		{
			"has_reservation": 1 if reservation else 0,
			"reservation": reservation or "",
			"has_property": 1 if prop else 0,
			"property": prop or "",
			"has_allowed": 1 if allowed_props is not None else 0,
			"allowed_props": allowed_props or ("",),
			"has_department": 1 if department else 0,
			"department": department or "",
		},
		as_dict=True,
	)

	bd_cache: dict[str, str] = {}
	out = []
	for row in rows:
		rprop = row.get("property")
		if business_date:
			bd = str(getdate(business_date))
		else:
			if rprop not in bd_cache:
				bd_cache[rprop] = get_business_date(rprop)
			bd = bd_cache[rprop]
		item = _format_trace_row(row, bd)
		if status:
			if status == "Due":
				if item["trace_status"] not in ("Open", "Overdue"):
					continue
			elif item["trace_status"] != status and item["status"] != status:
				continue
		out.append(item)
	return out


@frappe.whitelist(methods=["POST"])
@require_roles(*_TRACE_READ_ROLES)
def add_trace(
	reservation: str,
	department: str,
	instruction: str,
	trace_date: str | None = None,
	trace_time: str | None = None,
) -> dict:
	"""Create a dated trace on a stay without re-validating locked reservation nights."""
	text = (instruction or "").strip()
	if not text:
		frappe.throw(_("Write the trace instruction."))
	if department not in TRACE_DEPARTMENTS:
		frappe.throw(
			_("Department must be one of: {0}.").format(", ".join(TRACE_DEPARTMENTS))
		)
	res = frappe.db.get_value(
		"Reservation",
		reservation,
		["name", "property", "status", "guest_name", "room", "check_in_date", "check_out_date"],
		as_dict=True,
	)
	if not res:
		frappe.throw(
			_("Reservation {0} not found.").format(reservation),
			frappe.DoesNotExistError,
		)
	assert_property_access(res.property)
	if res.status in ("Cancelled", "Checked Out", "No Show"):
		frappe.throw(_("This stay is {0} - no new traces.").format(_(res.status)))

	bd = get_business_date(res.property)
	tdate = str(getdate(trace_date)) if trace_date else bd
	ttime = (str(trace_time).strip() or None) if trace_time not in (None, "") else None
	tstatus = _compute_trace_status("Open", tdate, bd)
	activated_on = bd if tstatus in ("Open", "Overdue") else None

	idx = (
		frappe.db.sql(
			"""SELECT MAX(idx) FROM `tabStay Instruction`
			WHERE parent=%(parent)s AND parenttype='Reservation' AND parentfield='instructions'""",
			{"parent": reservation},
		)[0][0]
		or 0
	) + 1

	row = frappe.get_doc({
		"doctype": "Stay Instruction",
		"parent": reservation,
		"parenttype": "Reservation",
		"parentfield": "instructions",
		"idx": idx,
		"department": department,
		"instruction": text[:1000],
		"status": "Open",
		"trace_date": tdate,
		"trace_time": ttime,
		"trace_status": tstatus,
		"activated_on": activated_on,
	})
	row.db_insert()

	from kamra.savings import log_action

	log_action(
		"stay_trace",
		"Reservation",
		reservation,
		res.property,
		rationale=f"[{department}] ({tdate}) {text[:120]}",
	)
	all_traces = list_traces(reservation=reservation)
	created = next((t for t in all_traces if t["name"] == row.name), None)
	return {"ok": True, "trace": created, "traces": all_traces}


@frappe.whitelist(methods=["POST"])
@require_roles(*_TRACE_READ_ROLES)
def resolve_trace(
	reservation: str,
	row: str,
	status: str = "Done",
	note: str | None = None,
) -> dict:
	"""Resolve or reopen a stay trace, syncing done/resolved timestamps and linked ToDo."""
	if status not in ("Open", "Done"):
		frappe.throw(_("Status must be Open or Done."))
	res_prop = frappe.db.get_value("Reservation", reservation, "property")
	if not res_prop:
		frappe.throw(
			_("Reservation {0} not found.").format(reservation),
			frappe.DoesNotExistError,
		)
	assert_property_access(res_prop)

	existing = frappe.db.get_value(
		"Stay Instruction",
		{"name": row, "parent": reservation, "parenttype": "Reservation"},
		["name", "instruction", "trace_date", "todo_ref"],
		as_dict=True,
	)
	if not existing:
		frappe.throw(_("That trace is not on this stay."))

	bd = get_business_date(res_prop)
	now = now_datetime()
	done = status == "Done"
	new_trace_status = "Done" if done else _compute_trace_status("Open", existing.trace_date, bd)
	updates = {
		"status": status,
		"trace_status": new_trace_status,
		"done_by": frappe.session.user if done else None,
		"done_on": now if done else None,
		"resolved_by": frappe.session.user if done else None,
		"resolved_at": now if done else None,
	}
	frappe.db.set_value("Stay Instruction", row, updates)

	if existing.todo_ref and frappe.db.exists("ToDo", existing.todo_ref):
		frappe.db.set_value(
			"ToDo",
			existing.todo_ref,
			"status",
			"Closed" if done else "Open",
		)

	from kamra.savings import log_action

	suffix = f" ({note.strip()[:120]})" if (note or "").strip() else ""
	log_action(
		"stay_trace",
		"Reservation",
		reservation,
		res_prop,
		rationale=f"{status}: {(existing.instruction or '')[:120]}{suffix}",
	)
	return {
		"ok": True,
		"row": row,
		"status": status,
		"trace_status": new_trace_status,
		"traces": list_traces(reservation=reservation),
	}


def activate_due_traces(property: str, business_date: str) -> dict:
	"""Activate scheduled traces due on/before business_date and escalate overdue ones."""
	bd = str(getdate(business_date))
	target = getdate(bd)
	rows = frappe.db.sql(
		"""
		SELECT si.name, si.parent, si.department, si.instruction, si.status,
		       si.trace_date, si.trace_status, si.activated_on, si.todo_ref
		FROM `tabStay Instruction` si
		JOIN `tabReservation` r ON r.name = si.parent
		WHERE r.property = %(prop)s
		  AND r.status IN ('Confirmed', 'Checked In', 'Held', 'Pending Payment')
		  AND si.parenttype = 'Reservation'
		  AND si.parentfield = 'instructions'
		  AND COALESCE(si.status, 'Open') != 'Done'
		""",
		{"prop": property},
		as_dict=True,
	)

	activated = 0
	overdue = 0
	todos_created = 0

	for row in rows:
		if not row.trace_date:
			continue
		td = getdate(row.trace_date)
		if td < target:
			updates: dict = {}
			if row.trace_status != "Overdue":
				updates["trace_status"] = "Overdue"
			if not row.activated_on:
				updates["activated_on"] = bd
			todo_name = row.todo_ref
			if not todo_name or not frappe.db.exists("ToDo", todo_name):
				lead = _department_lead(property, row.department)
				todo = frappe.get_doc({
					"doctype": "ToDo",
					"status": "Open",
					"priority": "High",
					"date": str(td),
					"allocated_to": lead,
					"reference_type": "Reservation",
					"reference_name": row.parent,
					"description": f"[Trace Overdue] [{row.department}] {row.instruction}",
				})
				todo.insert(ignore_permissions=True)
				updates["todo_ref"] = todo.name
				todos_created += 1
			if updates:
				frappe.db.set_value("Stay Instruction", row.name, updates, update_modified=False)
			overdue += 1
		elif td <= target and (row.trace_status or "Open") == "Scheduled":
			frappe.db.set_value(
				"Stay Instruction",
				row.name,
				{"trace_status": "Open", "activated_on": bd},
				update_modified=False,
			)
			activated += 1

	return {"activated": activated, "overdue": overdue, "todos_created": todos_created}


def on_night_audit_close(doc, method=None) -> None:
	"""Activate due traces and escalate overdue ones when Night Audit closes a business date."""
	if doc.get("status") not in ("Completed", "Completed with Warnings"):
		return
	prop = doc.get("property")
	closed_bd = doc.get("business_date")
	if not (prop and closed_bd):
		return
	next_bd = str(add_days(getdate(closed_bd), 1))
	activate_due_traces(prop, next_bd)


def escalate_overdue_traces() -> dict:
	"""Scheduled sweep ensuring every property's due/overdue traces stay current."""
	totals = {"activated": 0, "overdue": 0, "todos_created": 0}
	for prop in frappe.get_all("Property", filters={"disabled": 0}, pluck="name"):
		bd = get_business_date(prop)
		res = activate_due_traces(prop, bd)
		for key in totals:
			totals[key] += res[key]
	return totals


def _serialize_alert(row: dict) -> dict:
	return {
		"name": row.get("name"),
		"property": row.get("property"),
		"scope": row.get("scope") or ("Reservation" if row.get("reservation") else "Guest"),
		"reservation": row.get("reservation"),
		"guest": row.get("guest"),
		"trigger_area": row.get("trigger_area"),
		"severity": row.get("severity") or "Warning",
		"alert_code": row.get("alert_code") or None,
		"message": row.get("message") or "",
		"display_mode": row.get("display_mode") or "Every Access",
		"status": row.get("status") or "Active",
		"acknowledged_by": row.get("acknowledged_by"),
		"acknowledged_on": str(row.get("acknowledged_on")) if row.get("acknowledged_on") else None,
		"acknowledged_reason": row.get("acknowledged_reason"),
		"resolved_by": row.get("resolved_by"),
		"resolved_at": str(row.get("resolved_at")) if row.get("resolved_at") else None,
		"resolution_note": row.get("resolution_note"),
		"synthetic": bool(row.get("synthetic")),
		"can_override": 1 if can_override_blocking_alert() else 0,
	}


def check_point_alerts(
	trigger_area: str,
	property: str,
	reservation: str | None = None,
	guest: str | None = None,
	phone: str | None = None,
) -> list[dict]:
	"""Aggregate active Guest Alert records for a touchpoint plus Guest.blacklisted."""
	if not property:
		return []

	res_status = None
	if reservation:
		res_info = frappe.db.get_value(
			"Reservation",
			reservation,
			["guest", "status", "property"],
			as_dict=True,
		)
		if res_info:
			res_status = res_info.status
			if not guest:
				guest = res_info.guest

	if not guest and phone:
		from kamra import phone as phone_mod

		guest = frappe.db.get_value("Guest", {"phone": phone_mod.matching(phone)}, "name")

	triggers = [trigger_area]
	if res_status == "Checked In" and trigger_area in (
		"Reservation",
		"Check-Out",
		"Cashier",
		"In-House",
	):
		if "In-House" not in triggers:
			triggers.append("In-House")

	items: list[dict] = []
	if (reservation or guest) and frappe.db.exists("DocType", "Guest Alert"):
		rows = frappe.db.sql(
			"""
			SELECT name, property, scope, reservation, guest, trigger_area,
			       severity, alert_code, message, display_mode, status,
			       acknowledged_by, acknowledged_on, acknowledged_reason,
			       resolved_by, resolved_at, resolution_note, creation
			FROM `tabGuest Alert`
			WHERE property = %(prop)s
			  AND status != 'Resolved'
			  AND trigger_area IN %(triggers)s
			  AND (
			      (%(has_res)s = 1 AND reservation = %(reservation)s)
			      OR (%(has_guest)s = 1 AND scope = 'Guest' AND guest = %(guest)s)
			  )
			ORDER BY creation DESC
			""",
			{
				"prop": property,
				"triggers": tuple(triggers),
				"has_res": 1 if reservation else 0,
				"reservation": reservation or "",
				"has_guest": 1 if guest else 0,
				"guest": guest or "",
			},
			as_dict=True,
		)
		bd = get_business_date(property)
		for r in rows:
			if r.severity == "Blocking" and r.status == "Acknowledged":
				continue
			if (
				r.severity != "Blocking"
				and r.status == "Acknowledged"
				and r.display_mode == "Once Per Business Day"
				and r.acknowledged_on
				and str(r.acknowledged_on)[:10] == str(bd)
			):
				continue
			items.append(_serialize_alert(r))

	if guest and trigger_area in ("Reservation", "Check-In"):
		g = frappe.db.get_value(
			"Guest", guest, ["name", "full_name", "blacklisted", "blacklist_reason"], as_dict=True
		)
		if g and cint(g.blacklisted):
			items.append(
				_serialize_alert({
					"name": f"BLACKLIST-{g.name}",
					"property": property,
					"scope": "Guest",
					"reservation": reservation,
					"guest": g.name,
					"trigger_area": trigger_area,
					"severity": "Blocking",
					"alert_code": "BLACKLIST",
					"message": g.blacklist_reason or _("Guest is blacklisted."),
					"display_mode": "Until Resolved",
					"status": "Active",
					"synthetic": True,
				})
			)

	items.sort(key=lambda a: (_SEVERITY_RANK.get(a["severity"], 9), a["name"] or ""))
	return items


def alert_summary(alerts: list[dict]) -> dict:
	return {
		"items": alerts,
		"blocking_count": sum(1 for a in alerts if a.get("severity") == "Blocking"),
		"warning_count": sum(1 for a in alerts if a.get("severity") == "Warning"),
		"info_count": sum(1 for a in alerts if a.get("severity") == "Info"),
	}


def assert_no_blocking_alerts(
	trigger_area: str,
	property: str,
	reservation: str | None = None,
	guest: str | None = None,
	override_reason: str | None = None,
) -> None:
	"""Refuse Check-In, Check-Out, or Cashier completion while an active Blocking alert stands."""
	blocking = [
		a
		for a in check_point_alerts(
			trigger_area, property, reservation=reservation, guest=guest
		)
		if a.get("severity") == "Blocking" and not a.get("synthetic")
	]
	if not blocking:
		return
	if (
		trigger_area == "Check-Out"
		and (override_reason or "").strip()
		and can_override_blocking_alert()
	):
		from kamra.savings import log_action

		log_action(
			"guest_alert_override",
			"Reservation" if reservation else "Guest",
			reservation or guest,
			property,
			rationale=f"{trigger_area} blocking alert overridden: {override_reason.strip()[:200]}",
		)
		return
	messages = "; ".join(a["message"] for a in blocking)
	frappe.throw(
		_("Blocked by active {0} alert: {1}").format(_(trigger_area), messages),
		title=_("Blocking guest alert"),
	)


@frappe.whitelist()
@require_roles(*_ALERT_READ_ROLES)
def list_alerts(
	property: str | None = None,
	reservation: str | None = None,
	guest: str | None = None,
	trigger_area: str | None = None,
	include_resolved: int = 0,
) -> list[dict]:
	"""List Guest Alerts scoped to the caller's permitted property."""
	prop = property
	if reservation:
		res_prop = frappe.db.get_value("Reservation", reservation, "property")
		if not res_prop:
			frappe.throw(
				_("Reservation {0} not found.").format(reservation),
				frappe.DoesNotExistError,
			)
		assert_property_access(res_prop)
		prop = res_prop
	elif prop:
		assert_property_access(prop)

	if trigger_area and prop and not cint(include_resolved):
		return check_point_alerts(
			trigger_area, prop, reservation=reservation, guest=guest
		)

	filters: dict = {}
	if prop:
		filters["property"] = prop
	else:
		allowed = restricted_properties()
		if allowed is not None:
			if not allowed:
				return []
			filters["property"] = ("in", sorted(allowed))
	if reservation:
		filters["reservation"] = reservation
	elif guest:
		filters["guest"] = guest
	if trigger_area:
		filters["trigger_area"] = trigger_area
	if not cint(include_resolved):
		filters["status"] = ("!=", "Resolved")

	rows = frappe.get_all(
		"Guest Alert",
		filters=filters,
		fields=[
			"name", "property", "scope", "reservation", "guest", "trigger_area",
			"severity", "alert_code", "message", "display_mode", "status",
			"acknowledged_by", "acknowledged_on", "acknowledged_reason",
			"resolved_by", "resolved_at", "resolution_note",
		],
		order_by="creation desc",
	)
	items = [_serialize_alert(r) for r in rows]
	items.sort(key=lambda a: (_SEVERITY_RANK.get(a["severity"], 9), a["name"] or ""))
	return items


@frappe.whitelist(methods=["POST"])
@require_roles(*_ALERT_WRITE_ROLES)
def create_alert(
	property: str | None = None,
	trigger_area: str = "Reservation",
	message: str = "",
	severity: str = "Warning",
	reservation: str | None = None,
	guest: str | None = None,
	scope: str | None = None,
	alert_code: str | None = None,
	display_mode: str = "Every Access",
) -> dict:
	"""Create a multi-trigger Guest Alert scoped to a property."""
	if trigger_area not in ALERT_TRIGGERS:
		frappe.throw(
			_("Trigger area must be one of: {0}.").format(", ".join(ALERT_TRIGGERS))
		)
	if severity not in ALERT_SEVERITIES:
		frappe.throw(
			_("Severity must be one of: {0}.").format(", ".join(ALERT_SEVERITIES))
		)
	if display_mode not in ALERT_DISPLAY_MODES:
		frappe.throw(
			_("Display mode must be one of: {0}.").format(", ".join(ALERT_DISPLAY_MODES))
		)
	clean_message = (message or "").strip()[:500]
	if not clean_message:
		frappe.throw(_("Alert message is required."))

	prop = property
	if reservation:
		res = frappe.db.get_value(
			"Reservation", reservation, ["property", "guest"], as_dict=True
		)
		if not res:
			frappe.throw(
				_("Reservation {0} not found.").format(reservation),
				frappe.DoesNotExistError,
			)
		if prop and prop != res.property:
			frappe.throw(_("Reservation belongs to a different property."))
		prop = res.property
		if not guest and res.guest:
			guest = res.guest

	if not prop:
		frappe.throw(_("Property is required."))
	assert_property_access(prop)

	if not reservation and not guest:
		frappe.throw(_("Provide a reservation or guest for this alert."))

	resolved_scope = scope or ("Reservation" if reservation else "Guest")
	if resolved_scope not in ("Reservation", "Guest"):
		frappe.throw(_("Scope must be Reservation or Guest."))

	doc = frappe.get_doc({
		"doctype": "Guest Alert",
		"property": prop,
		"scope": resolved_scope,
		"reservation": reservation,
		"guest": guest,
		"trigger_area": trigger_area,
		"severity": severity,
		"alert_code": (alert_code or "").strip()[:60] or None,
		"message": clean_message,
		"display_mode": display_mode,
		"status": "Active",
	})
	doc.insert(ignore_permissions=True)

	from kamra.savings import log_action

	log_action(
		"guest_alert_created",
		"Guest Alert",
		doc.name,
		prop,
		rationale=f"[{trigger_area}/{severity}] {clean_message[:140]}",
	)
	return _serialize_alert(doc.as_dict())


@frappe.whitelist(methods=["POST"])
@require_roles(*_ALERT_WRITE_ROLES)
def acknowledge_alert(alert: str, reason: str | None = None) -> dict:
	"""Acknowledge an Info/Warning alert (or supervisor-clear a Blocking alert with reason)."""
	if not frappe.db.exists("Guest Alert", alert):
		frappe.throw(_("Guest alert {0} not found.").format(alert), frappe.DoesNotExistError)
	doc = frappe.get_doc("Guest Alert", alert)
	assert_property_access(doc.property)

	note = (reason or "").strip()[:500]
	if doc.severity == "Blocking":
		if not can_override_blocking_alert():
			frappe.throw(
				_("Only a Hotel Admin or Front Desk Supervisor can clear a Blocking alert."),
				frappe.PermissionError,
			)
		if not note:
			frappe.throw(_("A reason is required to clear a Blocking alert."))

	now = now_datetime()
	doc.status = "Acknowledged"
	doc.acknowledged_by = frappe.session.user
	doc.acknowledged_on = now
	if note:
		doc.acknowledged_reason = note
	doc.save(ignore_permissions=True)

	from kamra.savings import log_action

	log_action(
		"guest_alert_acknowledged",
		"Guest Alert",
		doc.name,
		doc.property,
		rationale=f"[{doc.trigger_area}/{doc.severity}] Acknowledged"
		+ (f": {note[:120]}" if note else ""),
	)
	return _serialize_alert(doc.as_dict())


@frappe.whitelist(methods=["POST"])
@require_roles(*_ALERT_WRITE_ROLES)
def resolve_alert(alert: str, resolution_note: str | None = None) -> dict:
	"""Resolve a Guest Alert; Blocking alerts require supervisor role and a resolution note."""
	if not frappe.db.exists("Guest Alert", alert):
		frappe.throw(_("Guest alert {0} not found.").format(alert), frappe.DoesNotExistError)
	doc = frappe.get_doc("Guest Alert", alert)
	assert_property_access(doc.property)

	note = (resolution_note or "").strip()[:500]
	if doc.severity == "Blocking":
		if not can_override_blocking_alert():
			frappe.throw(
				_("Only a Hotel Admin or Front Desk Supervisor can resolve a Blocking alert."),
				frappe.PermissionError,
			)
		if not note:
			frappe.throw(_("A resolution reason is required to resolve a Blocking alert."))

	now = now_datetime()
	doc.status = "Resolved"
	doc.resolved_by = frappe.session.user
	doc.resolved_at = now
	if note:
		doc.resolution_note = note
	doc.save(ignore_permissions=True)

	from kamra.savings import log_action

	log_action(
		"guest_alert_resolved",
		"Guest Alert",
		doc.name,
		doc.property,
		rationale=f"[{doc.trigger_area}/{doc.severity}] Resolved"
		+ (f": {note[:120]}" if note else ""),
	)
	return _serialize_alert(doc.as_dict())
