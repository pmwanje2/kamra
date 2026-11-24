"""Tape Chart unassigned queue, room attribute metadata, HK-prioritized
allocation suggestions, and quick room assignment.

Thin, RBAC-gated endpoints reusing kamra.allocation and kamra.api availability
guards with zero schema changes and strict multi-property isolation.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, getdate, nowdate

from kamra.authz import assert_property_access, require_roles

HK_READINESS_WEIGHT: dict[str, int] = {
	"Inspected": 100,
	"Ready": 90,
	"Clean": 50,
	"Pickup": 25,
	"Dirty": 0,
}

EXCLUDED_HK_STATUSES: frozenset[str] = frozenset({
	"Out of Order",
	"Out of Service",
	"OO",
	"OS",
})

_FLOOR_ONLY_RE = re.compile(r"^(?:floor\s*)?-?\d+(?:st|nd|rd|th)?$", re.IGNORECASE)
_WING_TEXT_RE = re.compile(r"\b([A-Za-z]+\s+Wing|Wing\s+[A-Za-z0-9]+)\b", re.IGNORECASE)
_ROOM_PREFIX_RE = re.compile(r"^([A-Za-z]+)[-\s]*\d")
_SEP_RE = re.compile(r"\s*(?:-|/|·|\||,)\s*")


def hk_readiness_weight(status: str | None) -> int:
	"""Housekeeping readiness rank: Inspected > Ready > Clean > Pickup > Dirty."""
	return HK_READINESS_WEIGHT.get((status or "").strip(), 0)


def is_assignable_hk_status(status: str | None) -> bool:
	"""Out of Order and Out of Service rooms are never assignable."""
	return (status or "").strip() not in EXCLUDED_HK_STATUSES


def _parse_wing(floor: str | None, room_number: str | None = None) -> str:
	"""Derive an optional wing/zone label from Room.floor or a room prefix."""
	text = (floor or "").strip()
	if text:
		parts = [p.strip() for p in _SEP_RE.split(text) if p.strip()]
		if len(parts) > 1:
			for part in parts:
				if not _FLOOR_ONLY_RE.fullmatch(part):
					return part
		match = _WING_TEXT_RE.search(text)
		if match:
			return match.group(1).strip()
	rn = (room_number or "").strip()
	if rn:
		prefix = _ROOM_PREFIX_RE.match(rn)
		if prefix:
			return prefix.group(1)
	return ""


def _parse_amenities(raw: str | None) -> list[str]:
	return [item.strip() for item in str(raw or "").split(",") if item.strip()]


@frappe.whitelist()
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def unassigned_queue(property: str, start_date: str | None = None, days: int = 14):
	"""Confirmed unassigned reservations overlapping [start_date, start_date + days)."""
	if not property:
		frappe.throw(_("Property is required."))
	assert_property_access(property)
	frappe.has_permission("Reservation", "read", throw=True)

	start = getdate(start_date or nowdate())
	span = max(1, min(int(days or 14), 31))
	end = add_days(start, span)

	rows = frappe.db.sql(
		"""
		SELECT
			r.name,
			r.guest,
			r.guest_name,
			COALESCE(g.vip, 0) AS vip,
			r.room_type,
			COALESCE(rt.room_type_name, r.room_type) AS room_type_name,
			r.check_in_date,
			r.check_out_date,
			r.is_day_use,
			r.adults,
			r.children,
			r.booking_type,
			r.source,
			r.company,
			r.travel_agent,
			r.group_booking,
			r.special_requests,
			r.planned_check_in_time,
			r.planned_check_out_time,
			r.status,
			r.creation
		FROM `tabReservation` r
		LEFT JOIN `tabRoom Type` rt ON rt.name = r.room_type
		LEFT JOIN `tabGuest` g ON g.name = r.guest
		WHERE r.property = %(property)s
		  AND r.status = 'Confirmed'
		  AND (r.room IS NULL OR r.room = '')
		  AND r.check_in_date < %(end)s
		  AND GREATEST(r.check_out_date, DATE_ADD(r.check_in_date, INTERVAL 1 DAY)) > %(start)s
		ORDER BY r.check_in_date ASC, COALESCE(g.vip, 0) DESC, r.booking_type DESC, r.creation ASC
		""",
		{"property": property, "start": str(start), "end": str(end)},
		as_dict=True,
	)

	items = []
	for row in rows:
		ci = str(row.get("check_in_date") or "")
		co = str(row.get("check_out_date") or "")
		nights = max(1, int(date_diff(co, ci))) if ci and co else 1
		items.append({
			"name": row.get("name"),
			"guest": row.get("guest"),
			"guest_name": row.get("guest_name"),
			"vip": 1 if int(row.get("vip") or 0) else 0,
			"room_type": row.get("room_type"),
			"room_type_name": row.get("room_type_name") or row.get("room_type"),
			"check_in_date": ci,
			"check_out_date": co,
			"nights": nights,
			"is_day_use": 1 if int(row.get("is_day_use") or 0) else 0,
			"adults": int(row.get("adults") or 0),
			"children": int(row.get("children") or 0),
			"booking_type": row.get("booking_type") or "",
			"source": row.get("source") or "",
			"company": row.get("company"),
			"travel_agent": row.get("travel_agent"),
			"group_booking": row.get("group_booking"),
			"special_requests": row.get("special_requests") or "",
			"planned_check_in_time": str(row.get("planned_check_in_time")) if row.get("planned_check_in_time") else None,
			"planned_check_out_time": str(row.get("planned_check_out_time")) if row.get("planned_check_out_time") else None,
			"status": row.get("status") or "Confirmed",
			"creation": str(row.get("creation") or ""),
		})

	# Stable multi-key sort matching SQL: check_in_date asc, vip desc, booking_type desc, creation asc
	items.sort(key=lambda item: item["creation"])
	items.sort(key=lambda item: item["booking_type"], reverse=True)
	items.sort(key=lambda item: item["vip"], reverse=True)
	items.sort(key=lambda item: item["check_in_date"])

	return {
		"start": str(start),
		"days": span,
		"property": property,
		"count": len(items),
		"items": items,
	}


@frappe.whitelist()
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def room_metadata(property: str):
	"""Floors and room/room-type filter metadata without schema mutations."""
	if not property:
		frappe.throw(_("Property is required."))
	assert_property_access(property)
	frappe.has_permission("Room", "read", throw=True)

	from kamra.floors import floors_for

	rows = frappe.db.sql(
		"""
		SELECT
			r.name,
			r.room_number,
			r.floor,
			r.room_type,
			r.housekeeping_status,
			r.occupancy_status,
			COALESCE(rt.room_type_name, r.room_type) AS room_type_name,
			rt.room_view,
			rt.bed_type,
			rt.smoking_allowed,
			rt.air_conditioning,
			rt.kitchen,
			rt.amenities
		FROM `tabRoom` r
		LEFT JOIN `tabRoom Type` rt ON rt.name = r.room_type
		WHERE r.property = %(property)s
		ORDER BY r.room_number ASC
		""",
		{"property": property},
		as_dict=True,
	)

	rooms: dict[str, dict] = {}
	for row in rows:
		floor_val = (row.get("floor") or "").strip()
		room_number = str(row.get("room_number") or "")
		rooms[row["name"]] = {
			"name": row["name"],
			"room_number": room_number,
			"room_type": row.get("room_type") or "",
			"room_type_name": row.get("room_type_name") or row.get("room_type") or "",
			"housekeeping_status": row.get("housekeeping_status") or "",
			"occupancy_status": row.get("occupancy_status") or "",
			"floor": floor_val,
			"wing": _parse_wing(floor_val, room_number),
			"room_view": (row.get("room_view") or "").strip(),
			"bed_type": (row.get("bed_type") or "").strip(),
			"smoking_allowed": 1 if int(row.get("smoking_allowed") or 0) else 0,
			"air_conditioning": (row.get("air_conditioning") or "").strip(),
			"kitchen": 1 if int(row.get("kitchen") or 0) else 0,
			"amenities": _parse_amenities(row.get("amenities")),
		}

	return {
		"floors": floors_for(property),
		"rooms": rooms,
	}


@frappe.whitelist()
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def suggest_queue_allocation(property: str, date: str | None = None, days: int = 1):
	"""Propose rooms for unassigned arrivals ranked primarily by HK readiness
	(Inspected > Ready > Clean > Pickup > Dirty), excluding OO and OS rooms."""
	if not property:
		frappe.throw(_("Property is required."))
	assert_property_access(property)
	frappe.has_permission("Reservation", "read", throw=True)

	from kamra.allocation import _as_int, _room_pool, _score
	from kamra.api import available_rooms

	d = str(getdate(date or nowdate()))
	span = max(1, min(int(days or 1), 31))
	filters: dict = {
		"property": property,
		"status": "Confirmed",
		"room": ("in", ["", None]),
	}
	if span == 1:
		filters["check_in_date"] = d
	else:
		filters["check_in_date"] = ("between", [d, str(add_days(d, span - 1))])

	arrivals = frappe.get_all(
		"Reservation",
		filters=filters,
		fields=[
			"name",
			"guest",
			"guest_name",
			"room_type",
			"check_in_date",
			"check_out_date",
			"special_requests",
			"group_booking",
			"is_day_use",
			"booking_type",
			"creation",
		],
		order_by="check_in_date asc, booking_type desc, creation asc",
	)

	gnames = [a.guest for a in arrivals if a.get("guest")]
	gmeta = (
		{
			g.name: g
			for g in frappe.get_all(
				"Guest",
				filters={"name": ("in", gnames)},
				fields=["name", "vip", "guest_notes"],
			)
		}
		if gnames
		else {}
	)

	# Prioritize VIPs within each arrival day so they get Inspected rooms first
	arrivals = sorted(
		arrivals,
		key=lambda a: (
			str(a.get("check_in_date") or ""),
			0 if (gmeta.get(a.get("guest")) and int(gmeta[a["guest"]].get("vip") or 0)) else 1,
			str(a.get("creation") or ""),
		),
	)

	pool, _max_floor, _min_floor = _room_pool(property)
	by_name = {r.name: r for r in pool}
	hk_by_room = {
		r.name: r.get("housekeeping_status")
		for r in frappe.get_all(
			"Room",
			filters={"property": property},
			fields=["name", "housekeeping_status"],
		)
	}

	taken: set[str] = set()
	proposals = []
	unfittable = []

	for a in arrivals:
		free = available_rooms(
			property,
			a.room_type,
			a.check_in_date,
			a.check_out_date,
			group_booking=a.get("group_booking"),
			ready_now=1,
		)
		cands = []
		for f in free or []:
			rname = f["name"]
			if rname not in by_name or rname in taken:
				continue
			hk_status = f.get("housekeeping_status") or hk_by_room.get(rname) or ""
			if not is_assignable_hk_status(hk_status):
				continue
			room_copy = dict(by_name[rname])
			room_copy["housekeeping_status"] = hk_status
			cands.append(room_copy)

		if not cands:
			unfittable.append({
				"reservation": a.name,
				"guest_name": a.guest_name,
				"room_type": a.room_type,
				"reason": "No ready assignable room of this type",
			})
			continue

		g = gmeta.get(a.get("guest"))
		vip = bool(g and int(g.get("vip") or 0))
		pref_parts = [p for p in (a.get("special_requests"), g.get("guest_notes") if g else None) if p]
		prefs = " ".join(pref_parts)
		cfloors = [_as_int(r.get("floor")) for r in cands if _as_int(r.get("floor")) is not None]
		cmax = max(cfloors) if cfloors else None
		cmin = min(cfloors) if cfloors else None

		scored = sorted(
			(
				(
					hk_readiness_weight(r.get("housekeeping_status")),
					_score(prefs, vip, r, cmax, cmin),
					r,
				)
				for r in cands
			),
			key=lambda entry: (entry[0], entry[1][0], -(_as_int(entry[2].get("room_number")) or 0)),
			reverse=True,
		)
		hk_w, (pref_score, why), room = scored[0]
		taken.add(room["name"])

		hk_status = room.get("housekeeping_status") or ""
		why_parts = ([hk_status] if hk_status else []) + list(why)
		needs_review = vip or pref_score > 0 or len(cands) > 1 or hk_w < HK_READINESS_WEIGHT["Clean"]

		proposals.append({
			"reservation": a.name,
			"guest_name": a.guest_name,
			"vip": 1 if vip else 0,
			"room_type": a.room_type,
			"room_type_name": room.get("room_type_name") or a.room_type,
			"suggested_room": room["name"],
			"room_number": room.get("room_number"),
			"housekeeping_status": hk_status,
			"why": ", ".join(why_parts) if why_parts else "First ready room of the type",
			"alternatives": len(cands) - 1,
			"needs_review": 1 if needs_review else 0,
			"prefs": prefs.strip(),
		})

	return {"date": d, "proposals": proposals, "unfittable": unfittable}


@frappe.whitelist()
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def candidate_rooms(property: str, reservation: str):
	"""Available assignable rooms for one unassigned reservation, ranked by HK readiness."""
	if not property or not reservation:
		frappe.throw(_("Property and reservation are required."))
	assert_property_access(property)
	frappe.has_permission("Reservation", "read", throw=True)

	doc = frappe.db.get_value(
		"Reservation",
		reservation,
		["property", "room_type", "check_in_date", "check_out_date", "group_booking"],
		as_dict=True,
	)
	if not doc or doc.property != property:
		frappe.throw(_("Not a booking at this property."), frappe.ValidationError)

	from kamra.allocation import _as_int
	from kamra.api import available_rooms

	free = available_rooms(
		property,
		doc.room_type,
		doc.check_in_date,
		doc.check_out_date,
		group_booking=doc.group_booking,
		ready_now=1,
	)
	cands = [
		{
			"name": r["name"],
			"room_number": r.get("room_number") or r["name"],
			"housekeeping_status": r.get("housekeeping_status") or "",
			"room_type": doc.room_type,
		}
		for r in (free or [])
		if is_assignable_hk_status(r.get("housekeeping_status"))
	]
	cands.sort(
		key=lambda r: (
			-hk_readiness_weight(r["housekeeping_status"]),
			_as_int(r["room_number"]) if _as_int(r["room_number"]) is not None else 999999,
			str(r["room_number"]),
		)
	)
	return cands


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def quick_assign_room(property: str, reservation: str, room: str, note: str = ""):
	"""Assign one room from the Tape Chart queue dock via apply_allocation."""
	if not property or not reservation or not room:
		frappe.throw(_("Property, reservation, and room are required."), frappe.ValidationError)
	assert_property_access(property)

	res_doc = frappe.db.get_value(
		"Reservation",
		reservation,
		["property", "room_type", "status", "room"],
		as_dict=True,
	)
	if not res_doc or res_doc.property != property:
		frappe.throw(_("Not a booking at this property."), frappe.ValidationError)

	room_doc = frappe.db.get_value(
		"Room",
		room,
		["property", "room_type", "housekeeping_status"],
		as_dict=True,
	)
	if not room_doc or room_doc.property != property:
		frappe.throw(_("Not a room at this property."), frappe.ValidationError)

	if not is_assignable_hk_status(room_doc.housekeeping_status):
		frappe.throw(
			_("Room {0} is {1} and cannot be assigned.").format(room, room_doc.housekeeping_status),
			frappe.ValidationError,
		)

	from kamra.allocation import apply_allocation

	reason_note = (note or "").strip() or "Tape Chart queue assignment"
	result = apply_allocation(
		property,
		[{"reservation": reservation, "room": room, "why": reason_note}],
	)
	skipped = result.get("skipped") or []
	if skipped:
		frappe.throw(skipped[0].get("reason") or _("Could not assign room."), frappe.ValidationError)

	return {
		"ok": True,
		"reservation": reservation,
		"room": room,
		"executed": result.get("executed") or [],
	}
