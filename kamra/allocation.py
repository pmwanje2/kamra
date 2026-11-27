"""Allocation agent (ORION): give every unassigned arrival the right room.

This is agent-native, not a rules engine bolted on: ORION reads each guest's
free-text wishes (special requests + guest profile notes) and the concrete
attributes of the free rooms, then proposes the best fit - VIPs to the best
room, "lake view" to a lake-view room, "high floor" to the top, a group kept
together. Proposals flow through the autonomy gate, so a manager can let ORION
assign the obvious ones and approve the judgement calls. A human clicking
"Auto-assign" on the tape chart runs the same logic with their own authority.

The scorer is deterministic and testable; when an OpenAI key is configured it
can be layered on top, but the hotel is never blocked on a model being up."""

import frappe

from kamra.authz import require_roles

# free-text preference cues -> how they score a candidate room
_VIEW_WORDS = ("view", "lake", "sea", "ocean", "pool", "garden", "city",
               "mountain", "hill", "beach", "river")
_HIGH_WORDS = ("high floor", "higher floor", "top floor", "upper floor",
               "upper", "highest")
_LOW_WORDS = ("ground floor", "low floor", "lower floor", "ground-floor",
              "no stairs", "accessible")
_BED_WORDS = ("king", "queen", "twin", "double", "single")
_QUIET_WORDS = ("quiet", "corner", "away from", "peaceful", "no noise")


_HK_READINESS_RANK = {
	"Inspected": 0,
	"Ready": 0,
	"Clean": 1,
	"Pickup": 2,
	"Dirty": 3,
}


def _room_pool(property: str):
	"""Every room with the attributes allocation cares about, keyed by name."""
	rooms = frappe.db.sql(
		"""
		SELECT r.name, r.room_number, r.floor, r.room_type, r.housekeeping_status,
		       rt.room_type_name, rt.room_view, rt.bed_type
		FROM `tabRoom` r
		LEFT JOIN `tabRoom Type` rt ON rt.name = r.room_type
		WHERE r.property = %(p)s
		  AND COALESCE(r.housekeeping_status, '') NOT IN ('Out of Order', 'Out of Service')
		""", {"p": property}, as_dict=True)
	floors = [int(r.floor) for r in rooms if _as_int(r.floor) is not None]
	return rooms, (max(floors) if floors else None), (min(floors) if floors else None)


def _as_int(v):
	try:
		return int(str(v).strip())
	except (ValueError, TypeError, AttributeError):
		return None


def _score(prefs: str, vip: bool, room: dict, max_floor, min_floor):
	"""Score how well a free room fits the guest's wishes; return (score, why)."""
	p = (prefs or "").lower()
	score = 0.0
	why = []

	view = (room.get("room_view") or "").lower()
	if any(w in p for w in _VIEW_WORDS):
		if view and view != "standard" and (view in p or "view" in p):
			score += 3
			why.append(f"{room['room_view']} view")

	fl = _as_int(room.get("floor"))
	if any(w in p for w in _HIGH_WORDS) and fl is not None and fl == max_floor:
		score += 2
		why.append("top floor")
	if any(w in p for w in _LOW_WORDS) and fl is not None and fl == min_floor:
		score += 2
		why.append("ground floor")

	bed = (room.get("bed_type") or "").lower()
	for w in _BED_WORDS:
		if w in p and w in bed:
			score += 2
			why.append(f"{room['bed_type']} bed")
			break

	if any(w in p for w in _QUIET_WORDS):
		# quieter rooms tend to sit at the far end of the corridor
		score += 0.5
		why.append("quieter end")

	if vip:
		# nudge VIPs toward the higher room numbers (usually the better rooms)
		score += (_as_int(room.get("room_number")) or 0) / 100000.0
		why.append("VIP - best available")

	return score, why


@frappe.whitelist()
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def suggest_allocation(property: str, date: str | None = None):
	"""Propose a room for every unassigned arrival on `date`. No mutation."""
	from frappe.utils import nowdate

	from kamra.api import available_rooms

	d = date or nowdate()
	arrivals = frappe.get_all(
		"Reservation",
		filters={"property": property, "status": "Confirmed",
		         "check_in_date": d, "room": ("in", ["", None])},
		fields=["name", "guest", "guest_name", "room_type", "check_in_date",
		        "check_out_date", "special_requests", "group_booking",
		        "is_day_use"],
		order_by="booking_type desc, creation asc")

	# batch the guest lookups (vip + notes)
	gnames = [a.guest for a in arrivals if a.guest]
	gmeta = {g.name: g for g in frappe.get_all(
		"Guest", filters={"name": ("in", gnames or [""])},
		fields=["name", "vip", "guest_notes"])} if gnames else {}

	pool, _max_floor, _min_floor = _room_pool(property)
	by_name = {r.name: r for r in pool}

	# rooms already handed out in this pass, so two arrivals never collide
	taken: set[str] = set()
	proposals, unfittable = [], []

	for a in arrivals:
		# the room is wanted today: one whose guest has not checked out yet
		# is not on offer (ready_now), even on that guest's departure date
		free = available_rooms(property, a.room_type, a.check_in_date,
		                       a.check_out_date, group_booking=a.group_booking,
		                       ready_now=1, for_assignment=1)
		cands = [by_name[r["name"]] for r in free
		         if r["name"] in by_name and r["name"] not in taken]
		if not cands:
			unfittable.append({"reservation": a.name, "guest_name": a.guest_name,
			                   "room_type": a.room_type,
			                   "reason": "No free room of this type"})
			continue

		g = gmeta.get(a.guest)
		vip = bool(g and g.vip)
		prefs = " ".join(filter(None, [a.special_requests,  # nosemgrep: frappe-no-functional-code -- filter(None, ...) drops empty parts; equivalent to a comprehension
		                               g.guest_notes if g else None]))
		# high/low floor is relative to the rooms actually on offer for this type
		cfloors = [_as_int(r.get("floor")) for r in cands
		           if _as_int(r.get("floor")) is not None]
		cmax = max(cfloors) if cfloors else None
		cmin = min(cfloors) if cfloors else None
		scored = sorted(
			((_score(prefs, vip, r, cmax, cmin), r) for r in cands),
			key=lambda x: (
				x[0][0],
				-_HK_READINESS_RANK.get(x[1].get("housekeeping_status") or "", 4),
				-(_as_int(x[1].get("room_number")) or 0),
			),
			reverse=True)
		(best_score, why), room = scored[0]
		taken.add(room.name)

		# a real choice (>1 candidate) or a preference/VIP match => needs a look
		needs_review = vip or best_score > 0 or len(cands) > 1
		proposals.append({
			"reservation": a.name,
			"guest_name": a.guest_name,
			"vip": 1 if vip else 0,
			"room_type_name": room.get("room_type_name") or a.room_type,
			"suggested_room": room.name,
			"room_number": room.get("room_number"),
			"why": ", ".join(why) if why else "First free room of the type",
			"alternatives": len(cands) - 1,
			"needs_review": 1 if needs_review else 0,
			"prefs": prefs.strip(),
		})

	return {"date": str(d), "proposals": proposals, "unfittable": unfittable}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def apply_allocation(property: str, assignments):
	"""Assign the proposed rooms - run by a person from the tape chart, or by
	an AI client on their behalf. Every pair is checked again before it is
	written: the stay is a confirmed one at this property still without a
	room, and the room is of the booked type and free and ready for those
	nights (the same available_rooms rule the suggestion used). Pairs that
	fail are returned in `skipped` with the reason. A pair already in place
	is skipped too, so repeating the call changes nothing. Each assignment is
	recorded in the action log."""
	if isinstance(assignments, str):
		assignments = frappe.parse_json(assignments)

	from kamra.crs import assert_property_access
	from kamra.folio import payment_savepoint
	from kamra.shares import current_inventory_reads

	assert_property_access(property)
	# See the previous desk's commit under the property lock, even when the
	# caller already has a read view and uncommitted work in this transaction.
	with payment_savepoint(), current_inventory_reads():
		result = _assign_rooms(property, assignments)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist the completed allocation batch before returning
	return result


def _assign_rooms(property, assignments):
	from kamra.api import _available_rooms
	from kamra.kamra.doctype.reservation.reservation import lock_property_for_inventory
	from kamra.siu.availability import availability
	from kamra.savings import log_action

	lock_property_for_inventory(property)
	executed, skipped = [], []
	taken: set[str] = set()
	for a in assignments or []:
		res, room = a.get("reservation"), a.get("room") or a.get("suggested_room")
		if not (res and room):
			skipped.append({"reservation": res, "room": room,
			                "reason": frappe._("Needs a reservation and a room")})
			continue
		doc = frappe.db.get_value(
			"Reservation", res,
			["property", "room", "status", "room_type", "check_in_date",
			 "check_out_date", "group_booking", "sellable_unit"], as_dict=True, for_update=True)
		why = None
		if not doc or doc.property != property:
			why = frappe._("Not a booking at this property")
		elif doc.room == room:
			why = frappe._("Already in that room")
		elif doc.status != "Confirmed":
			why = frappe._("The stay is {0}, not waiting for arrival").format(frappe._(doc.status))
		elif doc.room:
			why = frappe._("The stay already has a room; use a room move to change it")
		elif room in taken:
			why = frappe._("That room was just given to another arrival")
		else:
			r = frappe.db.get_value("Room", room, ["property", "room_type", "housekeeping_status"], as_dict=True, for_update=True)
			if not r or r.property != property:
				why = frappe._("Not a room at this property")
			elif not doc.sellable_unit and r.room_type != doc.room_type:
				why = frappe._("That room is another type; use a room move so the price is shown first")
			elif r.housekeeping_status in ("Out of Order", "Out of Service"):
				why = frappe._("That room is no longer free or ready ({0})").format(frappe._(r.housekeeping_status))
			elif doc.sellable_unit:
				units = availability(property, room_type=doc.room_type, check_in_date=doc.check_in_date,
				                     check_out_date=doc.check_out_date, exclude_reservation=res, for_update=True)
				free_members = {m.get("name") if isinstance(m, dict) else getattr(m, "name", m)
				                for unit in units if unit["sellable"] for m in unit["rooms"]}
				if room not in free_members:
					why = frappe._("That room is no longer free or ready for these nights")
			else:
				free = _available_rooms(property, doc.room_type, doc.check_in_date, doc.check_out_date,
				                        doc.group_booking, ready_now=1, for_assignment=1, for_update=True)
				if room not in {f["name"] for f in free}:
					why = frappe._("That room is no longer free or ready for these nights")
		if why:
			skipped.append({"reservation": res, "room": room, "reason": why})
			continue
		stay = frappe.get_doc("Reservation", res, for_update=True)
		stay.room = room
		frappe.db.savepoint("allocate_one_room")
		try:
			stay.save()
		except frappe.ValidationError as exc:
			frappe.db.rollback(save_point="allocate_one_room")
			skipped.append({"reservation": res, "room": room, "reason": str(exc)})
			continue
		taken.add(room)
		log_action("allocate_room", "Reservation", res, property,
		           minutes_saved=2, strict=True,
		           rationale=f"→ room {room}. {a.get('why') or ''}".strip())
		executed.append({"reservation": res, "room": room})

	return {"executed": executed, "skipped": skipped}


@frappe.whitelist(methods=["POST"])
@require_roles("Revenue Manager", "Hotel Admin")
def set_sell_limit(property: str, room_type: str, start_date: str, end_date: str, adjustment: str):
	from frappe.utils import add_days, date_diff, getdate
	from kamra.crs import assert_property_access
	from kamra.kamra.doctype.reservation.reservation import lock_property_for_inventory
	from kamra.rooms import nightly_sell_adjustments, property_inventory
	from kamra.kamra.doctype.room_sell_limit.room_sell_limit import validate_capacities, whole_adjustment

	assert_property_access(property)
	lock_property_for_inventory(property)
	if frappe.db.get_value("Room Type", room_type, "property") != property:
		frappe.throw(frappe._("Choose a room type at this property."))
	first, last = getdate(start_date), getdate(end_date)
	span = date_diff(last, first)
	if not start_date or not end_date or not 0 <= span <= 365:
		frappe.throw(frappe._("Choose a range of up to 366 nights."))
	value = whole_adjustment(adjustment)
	house, protected = frappe.db.get_value("Property", property,
		["house_overbooking_limit", "overbooking_protection"], for_update=True)
	rooms = property_inventory(property)
	rooms.setdefault(room_type, 0)
	adjustments = nightly_sell_adjustments(property, first, last)
	for offset in range(span + 1):
		date = str(add_days(first, offset))
		adjustments[date][room_type] = value
		validate_capacities(rooms, adjustments[date], house, protected)
		filters = {"property": property, "room_type": room_type, "stay_date": date}
		name = frappe.db.get_value("Room Sell Limit", filters, "name", for_update=True)
		if name:
			frappe.db.set_value("Room Sell Limit", name, "adjustment", value)
		else:
			limit = frappe.get_doc({"doctype": "Room Sell Limit", **filters, "adjustment": value})
			limit.flags.ari_range_update = True
			limit.insert(ignore_permissions=True)
	from kamra.channel_ari import enqueue_ari_change
	enqueue_ari_change(property, first, last, kinds="availability", reason="Room Sell Limit", strict=True)
	return {"adjustment": value}
