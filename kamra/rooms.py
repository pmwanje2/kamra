"""Rooms - one place to see and manage every room.

The Rooms screen (/kamra/room-board) reads from here: a board of every
room with what is happening in it today, one room's full story (status,
current stay, the next weeks, who stayed, who cleaned, tickets, moves)
and per-room numbers (most used, least sold, revenue, out-of-order days,
cleaning time).

Everything here only reads. Changing a room goes through the writers that
already exist: set_housekeeping_status, create_room_block /
release_room_block, check_in / check_out and move_reservation.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_days, cint, date_diff, get_datetime, getdate, nowdate

from kamra.api import HK_SUPERVISOR, _is_room_supervisor, _stay_balances
from kamra.authz import ADMIN, assert_property_access, require_roles
from kamra.floors import floors_for

# who may look at rooms: the desk, housekeeping supervisors, night audit,
# revenue, and the agent. Admins always pass.
BOARD_ROLES = ("Front Desk", HK_SUPERVISOR, "Night Auditor", "Revenue Manager",
               "Finance", "Kamra Agent")
# who may take a room out of service (the Room Blocks endpoints' gate)
_BLOCK_ROLES = {"Front Desk", "Kamra Agent", *ADMIN}
# who sees money on the room screens
_MONEY_ROLES = {"Front Desk", "Revenue Manager", "Finance", "Night Auditor",
                "Kamra Agent", *ADMIN}
_DESK_ROLES = {"Front Desk", "Kamra Agent", *ADMIN}

INSIGHT_WINDOWS = (7, 30, 90, 180, 365)
INSIGHTS_CACHE_SECONDS = 600
# a clean that took longer than this was left open, not worked on
_MAX_CLEAN_MINUTES = 8 * 60


def _roles() -> set:
	return set(frappe.get_roles())


def _can(roles: set) -> bool:
	mine = _roles()
	admin = frappe.session.user == "Administrator"
	return bool(admin or (mine & roles))


def _surname(name: str | None) -> str | None:
	parts = (name or "").strip().split()
	return parts[-1] if parts else None


def _user_names(users) -> dict:
	users = sorted({u for u in users if u})
	if not users:
		return {}
	return {u.name: u.full_name or u.name for u in frappe.get_all(
		"User", filters={"name": ("in", users)}, fields=["name", "full_name"])}


def _room_or_404(room: str) -> dict:
	r = frappe.db.get_value(
		"Room", room,
		["name", "property", "room_number", "room_type", "floor",
		 "housekeeping_status", "occupancy_status", "os_oo_reason",
		 "os_oo_until", "os_oo_set_by", "notes"], as_dict=True)
	if not r:
		frappe.throw(_("Room {0} not found.").format(room), frappe.DoesNotExistError)
	assert_property_access(r.property)
	return r


# --- the board -----------------------------------------------------------------

def add_room_numbers(rows: list, property: str) -> list:
	"""Add display labels while keeping room links usable for writes."""
	rooms = {row.get("room") for row in rows if row.get("room")}
	numbers = {r.name: r.room_number for r in frappe.get_all(
		"Room", filters={"property": property, "name": ("in", list(rooms))},
		fields=["name", "room_number"],
	)} if rooms else {}
	for row in rows:
		room = row.get("room")
		row["room_number"] = (numbers.get(room) or room.removeprefix(f"{property}-")) if room else None
	return rows


@frappe.whitelist()
@require_roles(*BOARD_ROLES)
def rooms_board(property: str):
	"""Every room of the property as a tile: housekeeping status, who is in
	it, who arrives or leaves today, whether it is out of service, and its
	open housekeeping task. One call, a fixed number of queries."""
	assert_property_access(property)
	today = getdate(nowdate())
	rooms = frappe.get_all(
		"Room", filters={"property": property},
		fields=["name", "room_number", "room_type", "floor",
		        "housekeeping_status", "occupancy_status",
		        "os_oo_reason", "os_oo_until"],
		order_by="room_number asc")
	rt_names = {rt.name: rt.room_type_name for rt in frappe.get_all(
		"Room Type", filters={"property": property},
		fields=["name", "room_type_name"])}
	in_house = {}
	for stay in frappe.get_all(
		"Reservation",
		filters={"property": property, "status": "Checked In", "room": ("is", "set")},
		fields=["name", "room", "guest_name", "check_in_date", "check_out_date",
		        "is_day_use", "is_share_primary"]):
		current = in_house.get(stay.room)
		rank = (getdate(stay.check_out_date), cint(stay.is_share_primary), stay.name)
		if not current or rank > (getdate(current.check_out_date), cint(current.is_share_primary), current.name):
			in_house[stay.room] = stay
	arrivals = {}
	for r in frappe.get_all(
			"Reservation",
			filters={"property": property, "status": "Confirmed",
			         "room": ("is", "set"), "check_in_date": today},
			fields=["name", "room", "guest_name", "eta"], order_by="creation asc"):
		arrivals.setdefault(r.room, r)
	blocks = {}
	for b in frappe.get_all(
			"Room Block",
			filters={"property": property, "block_status": "Active",
			         "from_date": ("<=", today), "to_date": (">", today)},
			fields=["name", "room", "reason", "from_date", "to_date", "note"],
			order_by="from_date asc"):
		blocks.setdefault(b.room, b)
	tasks = {}
	for t in frappe.get_all(
			"Housekeeping Task",
			filters={"property": property, "status": ("in", ["Pending", "In Progress"])},
			fields=["name", "room", "task_type", "status", "priority",
			        "assigned_to_user", "due_by"],
			order_by="creation asc"):
		tasks.setdefault(t.room, t)
	names = _user_names(t.assigned_to_user for t in tasks.values())
	for r in rooms:
		r["room_type_name"] = rt_names.get(r.room_type, r.room_type)
		stay = in_house.get(r.name)
		r["stay"] = {
			"reservation": stay.name, "guest_name": stay.guest_name,
			"surname": _surname(stay.guest_name),
			"check_in_date": str(stay.check_in_date),
			"check_out_date": str(stay.check_out_date),
			"departing_today": getdate(stay.check_out_date) <= today,
			"day_use": bool(stay.is_day_use),
		} if stay else None
		arr = arrivals.get(r.name)
		r["arrival"] = {
			"reservation": arr.name, "guest_name": arr.guest_name,
			"surname": _surname(arr.guest_name),
			"eta": str(arr.eta)[:5] if arr.eta else None,
		} if arr else None
		blk = blocks.get(r.name)
		r["block"] = {
			"name": blk.name, "reason": blk.reason,
			"from_date": str(blk.from_date), "to_date": str(blk.to_date),
			"note": blk.note,
		} if blk else None
		t = tasks.get(r.name)
		r["task"] = {
			"name": t.name, "type": t.task_type, "status": t.status,
			"priority": t.priority,
			"assignee": names.get(t.assigned_to_user) or t.assigned_to_user,
		} if t else None
	return {
		"date": str(today),
		"rooms": rooms,
		"room_types": [{"name": k, "room_type_name": v} for k, v in
		               sorted(rt_names.items(), key=lambda kv: kv[1] or "")],
		# the Floor master's order (name, sort_order); rooms[].floor is unchanged
		"floors": floors_for(property),
		"can": _permissions(),
	}


def _permissions() -> dict:
	return {
		"supervise": _is_room_supervisor(),
		"block": _can(_BLOCK_ROLES),
		"desk": _can(_DESK_ROLES),
		"money": _can(_MONEY_ROLES),
	}


# --- one room ------------------------------------------------------------------

@frappe.whitelist()
@require_roles(*BOARD_ROLES)
def room_detail(room: str):
	"""Everything about one room right now: what it is, its status, who is
	in it (with the balance), who arrives next, whether it is held out of
	service, and its open housekeeping work."""
	r = _room_or_404(room)
	today = getdate(nowdate())
	rt = frappe.db.get_value(
		"Room Type", r.room_type,
		["name", "room_type_name", "room_category", "description", "bed_type",
		 "room_view", "amenities", "air_conditioning", "bathroom",
		 "smoking_allowed", "kitchen", "adults_capacity", "children_capacity",
		 "max_extra_beds", "image"], as_dict=True) or {}
	photos = []
	if rt.get("image"):
		photos.append(rt.get("image"))
	if rt:
		photos += [m.url for m in frappe.get_all(
			"Room Type Media", filters={"parent": rt.name, "parenttype": "Room Type"},
			fields=["url", "media_type"], order_by="idx asc")
			if m.url and (m.media_type or "Image") == "Image"]
	stay = frappe.get_all(
		"Reservation",
		filters={"room": room, "status": "Checked In"},
		fields=["name", "guest", "guest_name", "check_in_date", "check_out_date",
		        "nights", "adults", "children", "is_day_use", "company"],
		order_by="check_in_date desc", limit=1)
	stay = stay[0] if stay else None
	nxt = frappe.get_all(
		"Reservation",
		filters={"room": room, "status": "Confirmed", "check_in_date": (">=", today)},
		fields=["name", "guest", "guest_name", "check_in_date", "check_out_date",
		        "nights", "adults", "children", "eta"],
		order_by="check_in_date asc", limit=1)
	nxt = nxt[0] if nxt else None
	can = _permissions()
	if stay:
		stay["check_in_date"] = str(stay.check_in_date)
		stay["check_out_date"] = str(stay.check_out_date)
		stay["departing_today"] = getdate(stay.check_out_date) <= today
		if can["money"]:
			m = _stay_balances([stay.name]).get(stay.name) or {}
			stay["total"] = m.get("total", 0.0)
			stay["paid"] = m.get("paid", 0.0)
			stay["balance_due"] = m.get("due", 0.0)
	if nxt:
		nxt["arriving_today"] = getdate(nxt.check_in_date) == today
		nxt["check_in_date"] = str(nxt.check_in_date)
		nxt["check_out_date"] = str(nxt.check_out_date)
		nxt["eta"] = str(nxt.eta)[:5] if nxt.eta else None
	blocks = frappe.get_all(
		"Room Block",
		filters={"room": room, "block_status": "Active", "to_date": (">", today)},
		fields=["name", "reason", "from_date", "to_date", "note"],
		order_by="from_date asc")
	for b in blocks:
		b["current"] = getdate(b.from_date) <= today
		b["from_date"], b["to_date"] = str(b.from_date), str(b.to_date)
	tasks = frappe.get_all(
		"Housekeeping Task",
		filters={"room": room, "status": ("in", ["Pending", "In Progress"])},
		fields=["name", "task_type", "status", "priority", "assigned_to_user",
		        "due_by", "notes"],
		order_by="creation asc")
	names = _user_names(t.assigned_to_user for t in tasks)
	for t in tasks:
		t["assignee"] = names.get(t.assigned_to_user) or t.assigned_to_user
		t["due_by"] = str(t.due_by) if t.due_by else None
	return {
		"date": str(today),
		"room": {**r, "room_type_name": rt.get("room_type_name") or r.room_type},
		"room_type": {k: v for k, v in rt.items() if k != "image"},
		"photos": photos,
		"stay": stay,
		"next_arrival": nxt,
		"blocks": blocks,
		"tasks": tasks,
		"can": can,
	}


@frappe.whitelist()
@require_roles(*BOARD_ROLES)
def room_availability(room: str, start_date: str | None = None, days: int = 14):
	"""This room, night by night: booked (by whom), held out of service, or
	free. Same overlap rules as the tape chart and the block guard (the
	check-out night is free; a day-use stay holds its own date)."""
	r = _room_or_404(room)
	start = getdate(start_date or nowdate())
	days = max(1, min(cint(days) or 14, 31))
	end = add_days(start, days)
	res = frappe.db.sql(
		"""
		SELECT name, guest_name, status, check_in_date, check_out_date
		FROM `tabReservation`
		WHERE room = %(room)s AND status IN ('Confirmed', 'Checked In')
		  AND check_in_date < %(end)s
		  AND GREATEST(check_out_date, DATE_ADD(check_in_date, INTERVAL 1 DAY)) > %(start)s
		ORDER BY check_in_date
		""", {"room": room, "start": start, "end": end}, as_dict=True)
	blocks = frappe.get_all(
		"Room Block",
		filters={"room": room, "block_status": "Active",
		         "from_date": ("<", end), "to_date": (">", start)},
		fields=["name", "reason", "from_date", "to_date", "note"])
	cells = []
	for i in range(days):
		d = getdate(add_days(start, i))
		cell = {"date": str(d), "state": "free"}
		for b in res:
			last = max(getdate(b.check_out_date), getdate(add_days(b.check_in_date, 1)))
			if getdate(b.check_in_date) <= d < last:
				cell = {"date": str(d), "state": "booked", "reservation": b.name,
				        "guest_name": b.guest_name, "status": b.status,
				        "arrives": getdate(b.check_in_date) == d}
				break
		else:
			for k in blocks:
				if getdate(k.from_date) <= d < getdate(k.to_date):
					cell = {"date": str(d), "state": "blocked", "block": k.name,
					        "reason": k.reason, "note": k.note}
					break
		cells.append(cell)
	summary = {s: sum(1 for c in cells if c["state"] == s)
	           for s in ("free", "booked", "blocked")}
	return {"room": r.name, "room_number": r.room_number, "start": str(start),
	        "days": days, "cells": cells, "summary": summary}


# --- history -------------------------------------------------------------------

def _task_timeline(task_names: list) -> dict:
	"""Who started, finished and checked each housekeeping task, and when -
	read from the task's change log (Housekeeping Task tracks changes)."""
	if not task_names:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT docname, owner, creation, data FROM `tabVersion`
		WHERE ref_doctype = 'Housekeeping Task' AND docname IN %(names)s
		ORDER BY creation ASC
		""", {"names": tuple(task_names)}, as_dict=True)
	out: dict = {}
	for v in rows:
		try:
			changed = (json.loads(v.data or "{}") or {}).get("changed") or []
		except (TypeError, ValueError):
			continue
		for row in changed:
			if not row or row[0] != "status" or len(row) < 3:
				continue
			new = row[2]
			t = out.setdefault(v.docname, {})
			key = {"In Progress": "started", "Done": "done", "Verified": "verified"}.get(new)
			if key and key not in t:
				t[key] = {"by": v.owner, "at": v.creation}
	return out


def _minutes(a, b) -> float | None:
	if not a or not b:
		return None
	mins = (get_datetime(b) - get_datetime(a)).total_seconds() / 60
	# under a minute is a tap-through, over the cap a task left open
	return round(mins, 1) if 1 <= mins <= _MAX_CLEAN_MINUTES else None


def _posted_room_revenue(reservations: list) -> dict:
	"""Room charges posted to folios, per reservation (pre-tax)."""
	if not reservations:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT COALESCE(fc.reservation, f.reservation) AS res,
		       COALESCE(SUM(fc.amount), 0) AS amount
		FROM `tabFolio Charge` fc
		JOIN `tabFolio` f ON fc.parent = f.name
		WHERE fc.charge_type IN ('Room', 'Rate Adjustment')
		  AND COALESCE(fc.reservation, f.reservation) IN %(names)s
		GROUP BY res
		""", {"names": tuple(reservations)}, as_dict=True)
	return {r.res: float(r.amount or 0) for r in rows}


def _parse_move(rationale: str) -> tuple[str | None, str | None]:
	"""'<old> → <new> · ...' (move_reservation's log line) -> (old, new)."""
	head = (rationale or "").split(" · ")[0]
	if "→" not in head:
		return None, None
	old, new = (s.strip() for s in head.split("→", 1))
	return (old if old and old != "None" else None), (new or None)


@frappe.whitelist()
@require_roles(*BOARD_ROLES)
def room_history(room: str, limit: int = 15):
	"""Who stayed in this room lately (nights, room revenue), who cleaned
	and checked it and when, its service tickets, and the guests moved in
	or out of it."""
	r = _room_or_404(room)
	limit = max(1, min(cint(limit) or 15, 50))
	money = _can(_MONEY_ROLES)

	stays = frappe.get_all(
		"Reservation",
		filters={"room": room, "status": ("in", ["Checked In", "Checked Out"])},
		fields=["name", "guest", "guest_name", "status", "check_in_date",
		        "check_out_date", "nights", "adults", "children",
		        "amount_before_tax", "room_assigned_by", "room_assigned_at",
		        "checked_in_by"],
		order_by="check_in_date desc", limit=limit)
	posted = _posted_room_revenue([s.name for s in stays]) if money else {}
	for s in stays:
		s["check_in_date"] = str(s.check_in_date)
		s["check_out_date"] = str(s.check_out_date)
		if money:
			got = posted.get(s.name)
			# what the folio shows once nights are posted; the booked
			# room amount until then
			s["revenue"] = got if got else float(s.amount_before_tax or 0)
			s["revenue_source"] = "posted" if got else "booked"
		s.pop("amount_before_tax", None)

	tasks = frappe.get_all(
		"Housekeeping Task",
		filters={"room": room},
		fields=["name", "task_type", "status", "priority", "assigned_to_user",
		        "creation", "modified", "modified_by", "notes"],
		order_by="creation desc", limit=limit)
	timeline = _task_timeline([t.name for t in tasks])
	status_log = frappe.get_all(
		"Agent Action Log",
		filters={"action_type": "room_status", "reference_doctype": "Room",
		         "reference_name": room},
		fields=["actor", "creation", "rationale"],
		order_by="creation desc", limit=limit)
	tickets = frappe.get_all(
		"Service Ticket",
		filters={"room": room},
		fields=["name", "subject", "category", "priority", "status",
		        "assigned_to_user", "creation", "resolved_on", "resolution_note"],
		order_by="creation desc", limit=limit)
	move_rows = frappe.get_all(
		"Agent Action Log",
		filters={"action_type": "room_move", "property": r.property,
		         "rationale": ("like", f"%{room}%")},
		fields=["actor", "creation", "rationale", "reference_name"],
		order_by="creation desc", limit=limit * 3)
	moves = []
	for m in move_rows:
		old, new = _parse_move(m.rationale)
		if room not in (old, new) or old == new:
			continue
		moves.append({"reservation": m.reference_name, "by": m.actor,
		              "at": str(m.creation), "direction": "in" if new == room else "out",
		              "other_room": old if new == room else new,
		              "note": " · ".join(m.rationale.split(" · ")[1:]) or None})
		if len(moves) >= limit:
			break
	res_names = {m["reservation"] for m in moves}
	guests = {x.name: x.guest_name for x in frappe.get_all(
		"Reservation", filters={"name": ("in", list(res_names))},
		fields=["name", "guest_name"])} if res_names else {}
	other_rooms = {m["other_room"] for m in moves if m["other_room"]}
	numbers = {x.name: x.room_number for x in frappe.get_all(
		"Room", filters={"name": ("in", list(other_rooms))},
		fields=["name", "room_number"])} if other_rooms else {}
	for m in moves:
		m["guest_name"] = guests.get(m["reservation"])
		m["other_room_number"] = numbers.get(m["other_room"], m["other_room"])

	users = set()
	for s in stays:
		users |= {s.room_assigned_by, s.checked_in_by}
	for t in tasks:
		users.add(t.assigned_to_user)
		for ev in timeline.get(t.name, {}).values():
			users.add(ev["by"])
	users |= {x.actor for x in status_log} | {x.assigned_to_user for x in tickets}
	users |= {m["by"] for m in moves}
	names = _user_names(users)

	def who(u):
		return names.get(u) or u

	for s in stays:
		s["room_assigned_by_name"] = who(s.room_assigned_by)
		s["checked_in_by_name"] = who(s.checked_in_by)
		s["room_assigned_at"] = str(s.room_assigned_at) if s.room_assigned_at else None
	hk = []
	for t in tasks:
		tl = timeline.get(t.name, {})
		done = tl.get("done") or tl.get("verified")
		# no change log (an old task): the last person to touch a closed
		# task is the best record there is
		if not done and t.status in ("Done", "Verified"):
			done = {"by": t.modified_by, "at": t.modified}
		hk.append({
			"task": t.name, "type": t.task_type, "status": t.status,
			"priority": t.priority,
			"assigned_to": who(t.assigned_to_user),
			"created": str(t.creation),
			"started_by": who(tl["started"]["by"]) if tl.get("started") else None,
			"started_at": str(tl["started"]["at"]) if tl.get("started") else None,
			"cleaned_by": who(done["by"]) if done else None,
			"cleaned_at": str(done["at"]) if done else None,
			"inspected_by": who(tl["verified"]["by"]) if tl.get("verified") else None,
			"inspected_at": str(tl["verified"]["at"]) if tl.get("verified") else None,
			"minutes": _minutes(tl.get("started", {}).get("at"),
			                    (tl.get("done") or tl.get("verified") or {}).get("at")),
			"notes": t.notes,
		})
	return {
		"room": r.name,
		"room_number": r.room_number,
		"stays": stays,
		"housekeeping": hk,
		"status_changes": [{"by": who(x.actor), "at": str(x.creation),
		                    "change": x.rationale} for x in status_log],
		"tickets": [{**x, "assigned_to": who(x.assigned_to_user),
		             "creation": str(x.creation),
		             "resolved_on": str(x.resolved_on) if x.resolved_on else None}
		            for x in tickets],
		"moves": moves,
		"money": money,
	}


# --- insights ------------------------------------------------------------------

def _overlap(a_start, a_end, b_start, b_end) -> int:
	"""Nights two half-open date ranges share."""
	s = max(getdate(a_start), getdate(b_start))
	e = min(getdate(a_end), getdate(b_end))
	return max(0, date_diff(e, s))


@frappe.whitelist()
@require_roles(*BOARD_ROLES)
def room_insights(property: str, days: int = 30, refresh: int = 0,
                  from_date: str | None = None, to_date: str | None = None):
	"""Per-room numbers for the last `days` nights (7, 30, 90, 180 or 365,
	up to yesterday): room nights sold and occupancy, room revenue posted to
	folios, out-of-service nights, and how long cleans take. Cached for a
	few minutes (refresh=1 recomputes); one query per measure, never one
	per room."""
	assert_property_access(property)
	days = cint(days) or 30
	if days not in INSIGHT_WINDOWS:
		days = 30
	money = _can(_MONEY_ROLES)
	today = nowdate()
	if from_date:  # a picked range: nights from_date..to_date
		to_date = to_date or str(add_days(today, -1))
		days = date_diff(to_date, from_date) + 1
		if not 1 <= days <= max(INSIGHT_WINDOWS):
			frappe.throw(f"Pick a range of 1 to {max(INSIGHT_WINDOWS)} days.")
		today = str(add_days(to_date, 1))
	key = f"kamra_room_insights:{property}:{days}:{today}:{int(money)}"
	cached = None if cint(refresh) else frappe.cache().get_value(key)
	if cached:
		return cached
	# a picked range counts cleans up to its last day; presets include today's
	clean_to = to_date if from_date else nowdate()
	out = _compute_insights(property, days, getdate(today), money, clean_to)
	frappe.cache().set_value(key, out, expires_in_sec=INSIGHTS_CACHE_SECONDS)
	return out


def _compute_insights(property: str, days: int, today, money: bool,
                      clean_to: str | None = None) -> dict:
	start = getdate(add_days(today, -days))
	end = today  # nights up to last night
	rooms = frappe.get_all(
		"Room", filters={"property": property},
		fields=["name", "room_number", "room_type", "floor"],
		order_by="room_number asc")
	rt_names = {rt.name: rt.room_type_name for rt in frappe.get_all(
		"Room Type", filters={"property": property},
		fields=["name", "room_type_name"])}
	stats = {r.name: {
		"room": r.name, "room_number": r.room_number,
		"room_type_name": rt_names.get(r.room_type, r.room_type),
		"floor": r.floor, "nights": 0, "stays": 0, "revenue": 0.0,
		"out_of_service_nights": 0, "cleans": 0, "clean_minutes": [],
		"occupied_nights": 0,
	} for r in rooms}

	# nights actually stayed (in-house or departed): closed nights keep the
	# rate category stamped when they posted (OPERA End of Day), unposted
	# nights follow the stay's current category (kamra.room_statistics)
	from kamra.folio import STANDS_ON_ITS_DATE
	from kamra.room_statistics import POSTED_CATEGORY, STAY_CATEGORY, category_rules
	rules = category_rules(property)
	posted_rooms = {}
	night_categories = {}
	room_stays = {}
	for name, date, stamped_room, category in frappe.db.sql(
			"""
			SELECT COALESCE(fc.reservation, f.reservation), fc.posting_date,
			       fc.share_room, """ + POSTED_CATEGORY + """
			FROM `tabFolio Charge` fc
			JOIN `tabFolio` f ON fc.parent = f.name
			LEFT JOIN `tabReservation` r ON r.name = COALESCE(fc.reservation, f.reservation)
			WHERE f.property = %(p)s AND fc.charge_type = 'Room'
			  AND """ + STANDS_ON_ITS_DATE + """
			  AND fc.posting_date >= %(start)s AND fc.posting_date < %(end)s
			ORDER BY fc.creation, fc.name
			""", {"p": property, "start": start, "end": end}):
		key = (name, date)
		if stamped_room or key not in posted_rooms:
			posted_rooms[key] = (stamped_room, category)
		if stamped_room in stats:
			night_categories.setdefault((stamped_room, date), category)
			room_stays.setdefault(stamped_room, set()).add(name)

	for s in frappe.db.sql(
			"SELECT r.name, r.room, r.check_in_date, r.check_out_date, " + STAY_CATEGORY + """ AS category
			FROM `tabReservation` r
			WHERE r.property = %(p)s AND r.room IS NOT NULL AND r.room != ''
			  AND r.status IN ('Checked In', 'Checked Out')
			  AND r.check_in_date < %(end)s AND r.check_out_date > %(start)s
			ORDER BY r.is_share_primary DESC, r.check_in_date, r.name
			""", {"p": property, "start": start, "end": end}, as_dict=True):
		day = max(getdate(s.check_in_date), start)
		hi = min(getdate(s.check_out_date), end)
		while day < hi:
			stamped, category = posted_rooms.get((s.name, day), (None, s.category))
			room = stamped or s.room
			if room in stats:
				night_categories.setdefault((room, day), category)
				room_stays.setdefault(room, set()).add(s.name)
			day = getdate(add_days(day, 1))

	for (room, _date), category in night_categories.items():
		stats[room]["nights"] += 1
		stats[room]["occupied_nights"] += int(rules.counts("occupancy", category))
	for room, stays in room_stays.items():
		stats[room]["stays"] = len(stays)

	revenue_places = 0
	if money:
		from kamra.ledger import _currency_places
		revenue_places = _currency_places(property)
		for x in frappe.db.sql(
				"""
				SELECT COALESCE(NULLIF(fc.share_room, ''), r.room) AS room,
				       COALESCE(SUM(fc.amount), 0) AS amount
				FROM `tabFolio Charge` fc
				JOIN `tabFolio` f ON fc.parent = f.name
				LEFT JOIN `tabReservation` r ON r.name = COALESCE(fc.reservation, f.reservation)
				WHERE f.property = %(p)s
				  AND fc.charge_type IN ('Room', 'Rate Adjustment')
				  AND fc.posting_date >= %(start)s AND fc.posting_date < %(end)s
				GROUP BY COALESCE(NULLIF(fc.share_room, ''), r.room)
				""", {"p": property, "start": start, "end": end}, as_dict=True):
			if x.room in stats:
				stats[x.room]["revenue"] = float(x.amount or 0)

	# held out of sale: active blocks, and released ones up to their release
	for b in frappe.get_all(
			"Room Block",
			filters={"property": property, "from_date": ("<", end),
			         "to_date": (">", start)},
			fields=["room", "from_date", "to_date", "block_status", "released_on"]):
		row = stats.get(b.room)
		if not row:
			continue
		stop = getdate(b.to_date)
		if b.block_status == "Released" and b.released_on:
			stop = min(stop, getdate(b.released_on))
		row["out_of_service_nights"] += _overlap(b.from_date, stop, start, end)

	# cleaning time: from In Progress to Done, out of the task change log
	tasks = frappe.get_all(
		"Housekeeping Task",
		filters={"property": property,
		         "creation": ("between", [str(start), f"{clean_to or today} 23:59:59"]),
		         "task_type": ("!=", "Inspection"),
		         "status": ("in", ["Done", "Verified"])},
		fields=["name", "room"])
	timeline = _task_timeline([t.name for t in tasks])
	for t in tasks:
		row = stats.get(t.room)
		if not row:
			continue
		row["cleans"] += 1
		tl = timeline.get(t.name, {})
		mins = _minutes((tl.get("started") or {}).get("at"),
		                (tl.get("done") or tl.get("verified") or {}).get("at"))
		if mins is not None:
			row["clean_minutes"].append(mins)

	all_minutes = []
	out_rooms = []
	occupied_nights = 0
	for row in stats.values():
		sellable = max(0, days - row["out_of_service_nights"])
		row["sellable_nights"] = sellable
		occupied = row.pop("occupied_nights")
		occupied_nights += occupied
		row["occupancy_pct"] = round(100 * occupied / sellable, 1) if sellable else 0.0
		mins = row.pop("clean_minutes")
		all_minutes += mins
		row["avg_clean_minutes"] = round(sum(mins) / len(mins), 1) if mins else None
		row["revenue"] = round(row["revenue"], revenue_places) if money else None
		out_rooms.append(row)

	total_nights = sum(r["nights"] for r in out_rooms)
	sellable = sum(r["sellable_nights"] for r in out_rooms)
	by_use = sorted(out_rooms, key=lambda r: (-r["nights"], r["room_number"] or ""))
	least = sorted(out_rooms, key=lambda r: (r["occupancy_pct"], r["nights"],
	                                          r["room_number"] or ""))
	return {
		"property": property,
		"days": days,
		"from_date": str(start),
		"to_date": str(add_days(end, -1)),
		"money": money,
		"summary": {
			"rooms": len(out_rooms),
			"room_nights": total_nights,
			"occupancy_pct": round(100 * occupied_nights / sellable, 1) if sellable else 0.0,
			"revenue": round(sum(r["revenue"] or 0 for r in out_rooms), revenue_places) if money else None,
			"out_of_service_nights": sum(r["out_of_service_nights"] for r in out_rooms),
			"avg_clean_minutes": round(sum(all_minutes) / len(all_minutes), 1)
			if all_minutes else None,
			"timed_cleans": len(all_minutes),
		},
		"most_used": by_use[:5],
		"least_used": least[:5],
		"rooms": out_rooms,
	}


def property_inventory(property, *, for_update=True):
	"""Physical inventory for computing category and house sell limits under the property lock."""
	from collections import Counter
	rows = frappe.db.get_values("Room", {"property": property}, ["room_type"], for_update=for_update)
	return dict(Counter(row[0] for row in rows))


def nightly_sell_adjustments(property, start_date, end_date, *, for_update=True, exclude=None):
	from collections import defaultdict
	rows = frappe.db.sql("""
		SELECT stay_date, room_type, adjustment FROM `tabRoom Sell Limit`
		WHERE property=%s AND stay_date BETWEEN %s AND %s AND name != %s
	""" + (" FOR UPDATE" if for_update else ""), (property, start_date, end_date, exclude or ""), as_dict=True)
	adjustments = defaultdict(dict)
	for row in rows:
		adjustments[str(row.stay_date)][row.room_type] = row.adjustment
	return adjustments


def _distribution_exclusions(property):
	"""Physical rooms withheld from channels, including their composite listings."""
	return set(frappe.db.sql("""
		SELECT r.room_type, r.name FROM `tabRoom` r
		WHERE r.property=%s AND (r.housekeeping_status='Out of Service' OR EXISTS (
			SELECT 1 FROM `tabSellable Unit Room` member
			JOIN `tabSellable Unit` u ON u.name=member.parent
			WHERE member.room=r.name AND u.property=r.property AND u.room_type=r.room_type
			  AND u.unit_kind='individual'
			GROUP BY member.room HAVING MAX(u.is_active)=0
		))""", property))


def _unavailable_rooms_by_night(property, nights, *, for_update=True, for_distribution=False, excluded=None):
	"""Return rooms off sale for the whole window and dated blocks by night."""
	if excluded is None:
		excluded = _distribution_exclusions(property) if for_distribution else set()
	rows = frappe.db.sql("""
		SELECT r.name, r.room_type, r.housekeeping_status, b.from_date, b.to_date
		FROM `tabRoom` r LEFT JOIN `tabRoom Block` b
		  ON b.room = r.name AND b.block_status = 'Active'
		  AND b.from_date <= %s AND b.to_date > %s
		WHERE r.property = %s AND (r.housekeeping_status = 'Out of Order' OR b.name IS NOT NULL)
	""" + (" FOR UPDATE" if for_update else ""), (nights[-1], nights[0], property), as_dict=True)
	standing = excluded.union((r.room_type, r.name) for r in rows if r.housekeeping_status == "Out of Order")
	blocked = {night: {(r.room_type, r.name) for r in rows
	                   if r.from_date and str(r.from_date) <= night < str(r.to_date)} for night in nights}
	return standing, blocked


def _available_bundles(units, night_index, member_types, remaining, house_free):
	"""Reserve the members of each offer against the same nightly physical budget."""
	from collections import Counter
	used = set()
	offers = 0
	for unit in units:
		if not unit["sellable_per_night"][night_index]:
			continue
		members = {r["name"] for r in unit["rooms"]}
		if not members or not used.isdisjoint(members) or len(members) > house_free:
			continue
		required = Counter(member_types[r] for r in members)
		if any(remaining[rt] < count for rt, count in required.items()):
			continue
		used.update(members)
		house_free -= len(members)
		remaining.subtract(required)
		offers += 1
	return offers


def nightly_inventory_position(property, room_type, dates, *, exclude=None, group=None, for_update=False,
                              for_distribution=False, occupied_unit=None):
	"""Category and house availability shared by booking enforcement and distribution."""
	from collections import Counter
	from kamra.kamra.doctype.reservation.reservation import InventoryRefused, uses_member_rooms
	from kamra.sell_limits import effective_capacities
	from kamra.siu.availability import availability, composite_claims
	physical = property_inventory(property, for_update=for_update)
	excluded = _distribution_exclusions(property) if for_distribution else set()
	# Composite listings add sellable units to their category, but their
	# constituent rooms already contribute to the physical house inventory.
	composite_units = frappe.db.get_values("Sellable Unit", {"property": property,
		"room_type": room_type, "is_active": 1, "unit_kind": "composite"},
		["name"], for_update=for_update)
	member_types = {}
	bundled_villa = False
	if composite_units:
		physical[room_type] = physical.get(room_type, 0) + len(composite_units)
		units = availability(property, room_type=room_type, check_in_date=dates[0],
		                     check_out_date=add_days(dates[-1], 1), for_update=for_update,
		                     excluded_rooms={name for _, name in excluded}, exclude_reservation=exclude,
		                     occupied_unit=occupied_unit)
		members = {r["name"] for unit in units for r in unit["rooms"]}
		if members:
			member_types = dict(frappe.db.get_values("Room", {"property": property, "name": ("in", members)},
			                                         ["name", "room_type"], for_update=for_update))
	elif room_type not in physical and physical and uses_member_rooms(property, room_type, for_update=for_update):
		# A hybrid villa listing sells its member rooms together as one unit.
		bundled_villa = True
		villa_types = set(frappe.db.get_values("Room Type", {"property": property, "room_category": "Villa"},
		                                      "name", pluck=True, for_update=for_update))
		physical[room_type] = 1
	house, protected, default_pct = frappe.db.get_value("Property", property,
		["house_overbooking_limit", "overbooking_protection", "overbooking_pct"], for_update=for_update)
	house = int(house or 0)
	from kamra.group_blocks import block_hold_by_night
	held = {rt: block_hold_by_night(property, rt, dates,
	            for_group=group if rt == room_type else None, for_update=for_update)
	        for rt in physical}
	standing, blocked = _unavailable_rooms_by_night(property, dates, for_update=for_update, excluded=excluded)
	arrival_rooms = {r.name for r in occupied_unit[1]} if occupied_unit else set()
	if occupied_unit and any(r.housekeeping_status == "Out of Service" for r in occupied_unit[1]):
		frappe.throw(_("Room unavailable"), exc=InventoryRefused)
	from kamra.shares import inventory_key
	adjustments = nightly_sell_adjustments(property, dates[0], dates[-1], for_update=for_update)
	defaults = {}
	if house > 0:
		for rt, pct in frappe.db.get_values("Room Type", {"property": property},
		                                   ["name", "overbooking_pct"], for_update=for_update):
			defaults[rt] = int(physical.get(rt, 0) * float(pct or default_pct or 0) / 100)
	rows = frappe.db.sql(
		"""SELECT name, room, room_type, sellable_unit, share_group_id, check_in_date, check_out_date,
		          GREATEST(check_out_date, DATE_ADD(check_in_date, INTERVAL 1 DAY)) AS departure
		   FROM `tabReservation`
		   WHERE property = %(property)s AND name != %(name)s
		     AND status IN ('Confirmed', 'Checked In', 'Held', 'Pending Payment')
		     AND check_in_date <= %(last)s
		     AND GREATEST(check_out_date, DATE_ADD(check_in_date, INTERVAL 1 DAY)) > %(first)s
		   """ + (" FOR UPDATE" if for_update else ""),
		{"property": property, "name": exclude or "new",
		 "first": dates[0], "last": dates[-1]}, as_dict=True)
	claims = composite_claims(property, rows, for_update=for_update)
	positions = {}
	for d in dates:
		unavailable = standing | blocked[d]
		bookings = [row for row in rows if str(row.check_in_date) <= d < str(row.departure)]
		if arrival_rooms and (
			any(room in arrival_rooms for _, room in unavailable)
			or any(row.room in arrival_rooms or not arrival_rooms.isdisjoint(claims.get(row.name, ()))
			       for row in bookings)
		):
			frappe.throw(_("Room unavailable"), exc=InventoryRefused)
		out = Counter(rt for rt, _ in unavailable)
		held_keys = {inventory_key(row) for row in bookings if row.room_type == room_type}
		member_sales = Counter(rt for row in bookings for rt in claims.get(row.name, {}).values())
		cnt = len(held_keys) + member_sales[room_type]
		hold = held.get(room_type, {}).get(d, 0)
		sellable = {rt: max(0, count - out[rt]) for rt, count in physical.items()}
		rooms = sellable.get(room_type, 0)
		try:
			limits = effective_capacities(sellable, adjustments[d], house, protected, defaults)
		except ValueError as exc:
			frappe.throw(_(str(exc)), exc=InventoryRefused)
		limit = limits.get(room_type, 0)
		house_sold = len({inventory_key(row) for row in bookings})
		house_sold += sum(len(claims[row.name]) - 1 for row in bookings if row.name in claims)
		house_held = sum(nightly.get(d, 0) for nightly in held.values())
		house_capacity = sum(sellable.values())
		member_free = limit - cnt - hold
		if composite_units:
			house_capacity -= len(composite_units)
			assigned = {inventory_key(row) for row in bookings
			            if row.room_type == room_type and (row.room or row.name in claims)}
			remaining = Counter(limits)
			remaining[room_type] -= len(composite_units)
			remaining.subtract(member_sales)
			remaining.subtract(rt for rt, key in {
				(row.room_type, inventory_key(row)) for row in bookings if row.name not in claims
			})
			for rt in set(member_types.values()):
				remaining[rt] -= held.get(rt, {}).get(d, 0)
			member_free = remaining[room_type]
			offers = _available_bundles(units, date_diff(d, dates[0]), member_types, remaining,
			                            house_capacity + house - house_held - house_sold)
			# Same-category members already contribute to sold. Their
			# physical claims were also deducted when computing offers.
			limit = min(limit, member_sales[room_type] + len(assigned) + offers)
		elif bundled_villa:
			# The villa is one offer over all non-villa rooms. Its synthetic unit
			# adds no physical house capacity and cannot substitute another room
			# for a blocked member, even when the house has overbooking headroom.
			house_capacity -= 1
			if not occupied_unit and any(rt not in villa_types for rt, _ in blocked[d]):
				rooms = limit = 0
				out[room_type] = 1
		positions[d] = {"sold": cnt, "hold": hold, "rooms": rooms, "out": out[room_type],
		                "limit": limit, "member_free": member_free, "held_keys": held_keys, "claimed_members": member_sales[room_type],
		                "house_free": house_capacity + house - house_held - house_sold}
	return positions
