"""Housekeeping SLA escalation and completion alerts.

Every open task carries a due_by (set from its priority). This module runs
on a schedule: once a task is past due it escalates - first to the front
desk (the duty desk that is staffed around the clock), then, once it is a
full SLA late, to the property's Hotel Admins - and notifies over the
property's connected WhatsApp channel (falling back to the activity log when
no channel is wired). Completing a checkout clean for a room with an arrival
today pings the front desk that the room is ready.
"""

from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, getdate, now_datetime, time_diff_in_seconds

from kamra.authz import assert_property_access, require_roles
from kamra.business_date import get_business_date
from kamra.kamra.doctype.reservation.reservation import lock_property_for_inventory
from kamra.savings import log_action

from kamra.kamra.doctype.housekeeping_task.housekeeping_task import sla_minutes

# escalation_level -> the role we notify at that level, and how the alert
# names them (the text must say who was actually told)
_ESCALATION_ROLE = {1: "Front Desk", 2: "Hotel Admin"}
_ESCALATION_WHO = {1: "the front desk", 2: "the hotel admin"}


def escalate_overdue_tasks():
	"""Scheduled: bump overdue tasks up the escalation ladder and alert."""
	now = now_datetime()
	tasks = frappe.get_all(
		"Housekeeping Task",
		filters={"status": ("in", ["Pending", "In Progress"]),
		         "due_by": ("<", now)},
		fields=["name", "property", "room", "task_type", "priority",
		        "due_by", "escalation_level"],
	)
	for t in tasks:
		overdue_min = time_diff_in_seconds(now, get_datetime(t.due_by)) / 60
		sla = sla_minutes(t.property, t.priority)
		# level 1 the moment it's overdue; level 2 once it's a full SLA late
		target = 2 if overdue_min >= sla else 1
		if target > (t.escalation_level or 0):
			_escalate(t, target, overdue_min)


def _escalate(task, level, overdue_min):
	frappe.db.set_value(
		"Housekeeping Task", task.name,
		{"escalation_level": level, "breached": 1}, update_modified=False)
	room = (task.room or "").split("-")[-1]
	who = _ESCALATION_WHO.get(level, "the hotel admin")
	body = (f"Housekeeping overdue: {task.task_type} in room {room} "
	        f"({task.priority}) is {int(overdue_min)} min past due - "
	        f"escalated to {who}.")
	_notify_role(task.property, _ESCALATION_ROLE.get(level, "Hotel Admin"), body)
	from kamra.savings import log_action
	log_action("hk_escalation", "Housekeeping Task", task.name, task.property,
	           rationale=body)


def notify_room_ready(task):
	"""Completion alert - only for the time-critical case: a checkout clean
	finished for a room with a guest arriving today."""
	if task.task_type != "Checkout Clean":
		return
	from frappe.utils import nowdate
	arriving = frappe.db.exists(
		"Reservation",
		{"room": task.room, "status": "Confirmed", "check_in_date": nowdate()})
	if not arriving:
		return
	room = (task.room or "").split("-")[-1]
	_notify_role(task.property, "Front Desk",
	             f"Room {room} is ready for today's arrival.")


def _notify_role(property, role, body):
	"""WhatsApp everyone holding `role` at this property (by their User
	mobile number). Staff limited to other properties are left out. Graceful:
	send_outbound logs and no-ops without a connected channel, so this never
	raises into the caller."""
	from kamra.agents_channels import send_outbound
	from kamra.authz import restricted_properties
	users = frappe.get_all(
		"Has Role", filters={"role": role, "parenttype": "User"}, pluck="parent")
	if not users:
		return
	phones = frappe.get_all(
		"User", filters={"name": ("in", users), "enabled": 1},
		fields=["name", "mobile_no"])
	for p in phones:
		allowed = restricted_properties(p.name)  # None: not limited to any
		if p.mobile_no and (allowed is None or property in allowed):
			try:
				send_outbound(property, "WhatsApp", p.mobile_no, body)
			except Exception:
				pass  # notify every recipient independently; one failure must not stop the rest


HOUSEKEEPING_ROLES = ("Housekeeping", "Housekeeping Supervisor", "Front Desk")
HOUSEKEEPING_SUPERVISORS = ("Housekeeping Supervisor", "Front Desk")
CREDIT_FIELDS = {
	"Stayover Clean": ("hk_stayover_credits", Decimal("1")),
	"Checkout Clean": ("hk_departure_credits", Decimal("2")),
	"Deep Clean": ("hk_deep_clean_credits", Decimal("3")),
	"Turndown": ("hk_turndown_credits", Decimal("0.5")),
}
UNSERVICEABLE_ROOMS = {"Out of Order", "Out of Service"}
OPEN_HOUSEKEEPING_STATUSES = ("Pending", "In Progress")
BOARD_FIELDS = [
	"hk_occupancy", "hk_reported_at", "hk_resolved_occupancy", "hk_section",
	"turndown_requested", "turndown_status", "turndown_business_date",
]


def _occupied_rooms(property):
	"""Use checked-in stays, including rooms held by whole-property bookings."""
	rooms = set()
	for stay in frappe.get_all("Reservation", filters={"property": property, "status": "Checked In"},
	                          fields=["name", "room", "room_type"]):
		if stay.room:
			rooms.add(stay.room)
		else:
			doc = frappe.get_doc("Reservation", stay.name)
			rooms.update(doc._occupied_rooms(checking_in=False))
	return rooms


def board_status(property, rooms, *, business_date=None):
	occupied = _occupied_rooms(property)
	day = str(business_date or get_business_date(property))
	for row in rooms:
		fd = "Occupied" if row.name in occupied else "Vacant"
		hk = row.hk_occupancy
		row["fd_occupancy"] = fd
		row["discrepancy"] = (
			("Skip" if fd == "Occupied" else "Sleep")
			if hk and hk != fd and row.hk_resolved_occupancy != fd else None
		)
		row.pop("hk_resolved_occupancy", None)
		if (str(row.turndown_business_date) != day or (
				row.turndown_status == "Pending" and (row.name not in occupied or row.housekeeping_status in UNSERVICEABLE_ROOMS))):
			row.update(turndown_requested=0, turndown_status="Not Required")
	return rooms


def _room_rows(property):
	return frappe.get_all("Room", filters={"property": property},
	                      fields=["name", "room_number", "housekeeping_status", *BOARD_FIELDS], order_by="room_number, name")


def _locked_room(room):
	property = frappe.db.get_value("Room", room, "property")
	if not property:
		frappe.throw(_("Room not found."), frappe.DoesNotExistError)
	assert_property_access(property)
	lock_property_for_inventory(property)
	return frappe.get_doc("Room", room, for_update=True)


@frappe.whitelist(methods=["POST"])
@require_roles(*HOUSEKEEPING_ROLES)
def report_hk_occupancy(room: str, hk_occupancy: str):
	doc = _locked_room(room)
	if hk_occupancy not in ("Vacant", "Occupied"):
		frappe.throw(_("Choose Vacant or Occupied."))
	doc.db_set({"hk_occupancy": hk_occupancy, "hk_reported_at": now_datetime(),
	            "hk_resolved_occupancy": None})
	return {"ok": True}


@frappe.whitelist()
@require_roles(*HOUSEKEEPING_ROLES)
def get_hk_discrepancies(property: str):
	assert_property_access(property)
	return [{**row, "room": row.name} for row in board_status(property, _room_rows(property))
	        if row.discrepancy]


@frappe.whitelist(methods=["POST"])
@require_roles(*HOUSEKEEPING_SUPERVISORS)
def resolve_hk_discrepancy(room: str, resolution_note: str):
	doc = _locked_room(room)
	note = (resolution_note or "").strip()
	if not note:
		frappe.throw(_("Enter a discrepancy resolution note."))
	row = board_status(doc.property, [frappe._dict(doc.as_dict())])[0]
	if not row.discrepancy:
		frappe.throw(_("This room has no open discrepancy."))
	doc.db_set("hk_resolved_occupancy", row.fd_occupancy)
	log_action("resolve_hk_discrepancy", "Room", room, doc.property, rationale=note,
	           before_snapshot={k: row[k] for k in ("fd_occupancy", "hk_occupancy", "discrepancy")},
	           after_snapshot={"resolved": True})
	from kamra.notifications import record
	record(doc.property, "front-desk", _("Housekeeping discrepancy resolved for room {0}").format(
		doc.room_number), doctype="Room", name=room, route="/housekeeping-board")
	return {"ok": True}


@frappe.whitelist()
@require_roles(*HOUSEKEEPING_ROLES)
def get_turndown_queue(property: str):
	assert_property_access(property)
	return [{**row, "room": row.name} for row in board_status(property, _room_rows(property))
	        if row.turndown_requested]


@frappe.whitelist(methods=["POST"])
@require_roles(*HOUSEKEEPING_ROLES)
def set_turndown_status(room: str, status: str):
	doc = _locked_room(room)
	if status not in ("Not Required", "Pending", "Completed", "DND"):
		frappe.throw(_("Choose a valid turndown status."))
	if status in ("Pending", "Completed") and doc.housekeeping_status in UNSERVICEABLE_ROOMS:
		frappe.throw(_("Turndown is unavailable for rooms out of order or out of service."))
	if status != "Not Required" and room not in _occupied_rooms(doc.property):
		frappe.throw(_("Turndown requires a checked-in stay."))
	day = get_business_date(doc.property)
	doc.db_set({"turndown_status": status, "turndown_requested": int(status != "Not Required"),
	            "turndown_business_date": day})
	filters = {"property": doc.property, "room": room, "task_type": "Turndown", "business_date": day}
	name = frappe.db.get_value("Housekeeping Task", filters, "name", for_update=True)
	if name:
		task = frappe.get_doc("Housekeeping Task", name)
		task.status = {"Not Required": "Cancelled", "Pending": "Pending",
		               "Completed": "Done", "DND": "Cancelled"}[status]
		task.save(ignore_permissions=True)
	elif status == "Pending":
		frappe.get_doc({"doctype": "Housekeeping Task", **filters, "priority": "Low"}).insert(
			ignore_permissions=True)
	log_action("turndown_status", "Room", room, doc.property, after_snapshot={"status": status})
	return {"ok": True}


def clear_stay_room_state(property, room):
	"""Retire observations resolved against the prior occupancy and service for its guest."""
	frappe.db.set_value("Room", room, {
		"hk_resolved_occupancy": None, "turndown_requested": 0,
		"turndown_status": "Not Required", "turndown_business_date": None,
	})
	retire_turndown_tasks(property, room=room)


def retire_turndown_tasks(property, *, room=None, before=None):
	filters = {"property": property, "task_type": "Turndown",
	           "status": ("in", OPEN_HOUSEKEEPING_STATUSES)}
	if room:
		filters["room"] = room
	if before:
		filters["business_date"] = ("<", before)
	frappe.db.set_value("Housekeeping Task", filters, "status", "Cancelled")
	if room:
		frappe.db.set_value("Room", {"name": room, "property": property, "turndown_status": "Pending"},
		                    {"turndown_requested": 0, "turndown_status": "Not Required"})


def turndown_is_current(service_date, business_date, room_status, occupied):
	return occupied and bool(room_status) and room_status not in UNSERVICEABLE_ROOMS and str(service_date) == str(business_date)


def assert_turndown_actionable(task):
	if task.task_type != "Turndown":
		return
	status = frappe.db.get_value("Room", {"name": task.room, "property": task.property}, "housekeeping_status")
	if task.status == "Cancelled" or not turndown_is_current(
			task.business_date, get_business_date(task.property), status, task.room in _occupied_rooms(task.property)):
		frappe.throw(_("Turndown is unavailable for this room on the current business date."))


def _has_credit_snapshot(task):
	# Older positive snapshots remain authoritative. An unmarked zero was
	# also the numeric default on manually assigned tasks.
	return bool(task.get("assignment_credits_recorded") or task.get("assignment_credits"))


def _configured_credit(task_type, value):
	if value is None:
		return CREDIT_FIELDS[task_type][1]
	return Decimal(str(value))


def snapshot_assignment_credits(task, previous):
	if task.task_type not in CREDIT_FIELDS or not task.assigned_to_user:
		task.assignment_credits = 0
		task.assignment_credits_recorded = 0
		return
	if (previous and _has_credit_snapshot(previous)
			and (task.assigned_to_user, task.room, task.task_type)
			== (previous.assigned_to_user, previous.room, previous.task_type)):
		task.assignment_credits = previous.assignment_credits
	else:
		room_type = frappe.db.get_value("Room", task.room, "room_type")
		field, _default = CREDIT_FIELDS[task.task_type]
		value = frappe.db.get_value("Room Type", {"name": room_type, "property": task.property}, field)
		task.assignment_credits = float(_configured_credit(task.task_type, value))
	task.assignment_credits_recorded = 1


def _credit_weight(task, room_types):
	if task.assigned_to_user and _has_credit_snapshot(task):
		return Decimal(str(task.assignment_credits))
	field, _default = CREDIT_FIELDS[task.task_type]
	return _configured_credit(task.task_type, room_types.get(task.room_type, {}).get(field))


@frappe.whitelist(methods=["POST"])
@require_roles(*HOUSEKEEPING_SUPERVISORS)
def auto_assign_housekeeping_tasks(property: str, business_date: str, attendants: list[str] | tuple[str, ...] | str,
                                   max_credits_per_attendant: float | int | str | Decimal, preview: int = 0):
	"""Allocate each open task once, preferring a section's current attendant."""
	from kamra.api import _hk_staff

	assert_property_access(property)
	staff = {row["user"]: row["full_name"] for row in _hk_staff(property)}
	if isinstance(attendants, str):
		attendants = frappe.parse_json(attendants)
	if not isinstance(attendants, (tuple, list)) or not attendants or not all(
			isinstance(user, str) and user in staff for user in attendants):
		frappe.throw(_("Choose attendants with access to this property."), frappe.PermissionError)
	users = sorted(set(attendants))
	try:
		cap = Decimal(str(max_credits_per_attendant))
		if not cap.is_finite() or cap <= 0:
			raise InvalidOperation
	except InvalidOperation:
		frappe.throw(_("Enter a positive task credit limit."))
	day = str(getdate(business_date))
	lock_property_for_inventory(property)
	current_day = get_business_date(property)
	room = frappe.qb.DocType("Room")
	task = frappe.qb.DocType("Housekeeping Task")
	rows = (frappe.qb.from_(task).join(room).on(task.room == room.name)
	        .select(task.name, task.room, task.task_type, task.assigned_to_user,
	                task.assignment_credits, task.assignment_credits_recorded, task.business_date,
	                task.status, task.priority, room.room_type, room.room_number,
	                room.hk_section, room.floor, room.housekeeping_status)
	        .where((task.property == property) & (room.property == property) &
	               ((task.business_date == day) |
	                (task.business_date.isnull() & task.creation.like(f"{day}%"))) &
	               task.task_type.isin(list(CREDIT_FIELDS)) &
	               task.status.isin(["Pending", "In Progress", "Done", "Verified"]))
	        .orderby(task.name).for_update().run(as_dict=True))
	room_types = {r.name: r for r in frappe.get_all(
		"Room Type", filters={"property": property},
		fields=["name", *(field for field, _default in CREDIT_FIELDS.values())])}
	loads = dict.fromkeys(users, Decimal(0))
	sections = {user: set() for user in users}
	pending = []
	occupied = _occupied_rooms(property) if any(row.task_type == "Turndown" for row in rows) else set()
	for row in rows:
		if (row.task_type == "Turndown" and row.status in OPEN_HOUSEKEEPING_STATUSES
				and not turndown_is_current(row.business_date, current_day, row.housekeeping_status, row.room in occupied)):
			continue
		row["credits"] = _credit_weight(row, room_types)
		row["section"] = row.hk_section or row.floor or ""
		user = row.assigned_to_user
		if user in loads:
			loads[user] += row.credits
			sections[user].add(row.section)
		elif not user and row.status == "Pending":
			pending.append(row)
	rank = {"Urgent": 0, "High": 1, "Medium": 2, "Low": 3}
	assigned, unassigned = [], []
	for row in sorted(pending, key=lambda r: (rank.get(r.priority, 4), r.section, r.room_number, r.name)):
		eligible = [user for user in users if loads[user] + row.credits <= cap]
		entry = {"task": row.name, "room_number": row.room_number, "section": row.section,
		         "task_type": row.task_type, "credits": float(row.credits)}
		if not eligible:
			unassigned.append(entry)
			continue
		user = min(eligible, key=lambda u: (row.section not in sections[u], loads[u], u))
		loads[user] += row.credits
		sections[user].add(row.section)
		assigned.append({**entry, "user": user})
		if not cint(preview):
			frappe.db.set_value("Housekeeping Task", row.name, {
				"assigned_to_user": user, "assignment_status": "Assigned",
				"assignment_credits": float(row.credits), "assignment_credits_recorded": 1,
			})
			log_action("hk_auto_assign", "Housekeeping Task", row.name, property,
			           after_snapshot={**assigned[-1], "business_date": day})
	return {"assigned": assigned, "unassigned": unassigned, "business_date": day,
	        "loads": [{"user": user, "full_name": staff[user], "credits": float(loads[user]),
	                   "max_credits": float(cap)} for user in users]}
