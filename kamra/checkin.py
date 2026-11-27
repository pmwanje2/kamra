"""Guided check-in - one screen per step, the property's SOP.

The steps and which of them are required come from the property's check-in
SOP (kamra.sop, "checkin"; Settings > Front desk > Check-in steps). This
module builds what each step needs, records who did each step and when, and
holds the one hard rule the server enforces: check_in refuses only while
there is no room (assign_room). Other steps the property marks required are
soft: the desk can check the guest in and finish them later (backfill).

Every step writes through the existing writer for its data: rooms through
move_reservation, deposits through the security deposit endpoints, payments
through the folio, requests through create_ticket. The step record itself
(Reservation.checkin_steps, JSON: step -> state, by, at, note) is the audit
trail, and each step also lands in the activity log under the operator who
did it.

Only the room step cannot be skipped. Soft-required steps can be skipped
with a reason, or left open and finished after check-in.
"""

from __future__ import annotations

import json
from contextlib import nullcontext

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from kamra.authz import require_roles
from kamra.guest_documents import id_status, used_stays_count
from kamra.no_show import property_now as property_local_now

DESK = ("Front Desk", "Night Auditor", "Kamra Agent")
# confirmations only a person at the desk can give
STAFF = ("Front Desk", "Night Auditor")


def payers() -> list[str]:
	"""Who may pay, as the desk confirms it: the Payer Type master. OTA
	prepaid, OTA guest pays here, Guest, Company and Group are system
	codes this module reasons about; a hotel adds its own beside them."""
	from kamra.masters import PAYER, enabled_codes
	return enabled_codes(PAYER)


def request_presets() -> dict[str, tuple]:
	"""Quick requests at check-in, from the Check-in Request master: each
	becomes a service ticket. {key: (label, team, priority, the charge type
	a price is billed as - None when the request is never charged)}."""
	from kamra.masters import CHECKIN_REQUEST, master_rows
	return {r.name: (r.description or r.name, r.team, r.priority, r.charge_type or None)
	        for r in master_rows(CHECKIN_REQUEST, fields=("team", "priority", "charge_type"))}

STAY_INFO_KEYS = ("guest_wifi_name", "guest_wifi_password", "breakfast_hours",
                  "emergency_number", "guest_info_note")


class CheckInStepsMissing(frappe.ValidationError):
	"""check_in refused: a required check-in step is not done yet."""


# ── the step list for a property ──────────────────────────────────────────

def checkin_steps(property: str) -> list[dict]:
	"""The property's check-in steps in order: its SOP template, with any
	built-in step it lacks added back at its default (a template saved
	before a step existed keeps working) and the room always required."""
	from kamra.sop import CHECKIN_ALWAYS_REQUIRED, checkin_default_steps
	# read-only: a property with no saved template uses the defaults (the
	# settings page saves one); never seed it from here, so two desks
	# opening check-ins at once don't race to insert the same template
	steps = []
	name = frappe.db.exists("DocType", "SOP Template") and frappe.db.get_value(
		"SOP Template", {"property": property, "key": "checkin", "active": 1}, "name")
	if name:
		steps = [{"step_key": s.step_key, "step_type": s.step_type, "title": s.title,
		          "instruction": s.instruction or "",
		          "prompt_to_guest": s.prompt_to_guest or "",
		          "is_blocking": 1 if s.is_blocking else 0,
		          "sequence": cint(s.sequence)}
		         for s in frappe.get_doc("SOP Template", name).steps or []]
	have = {s.get("step_key") for s in steps}
	steps = list(steps) + [s for s in checkin_default_steps(property)
	                       if s["step_key"] not in have]
	out = []
	for s in steps:
		s = dict(s)
		if s.get("step_key") in CHECKIN_ALWAYS_REQUIRED:
			s["is_blocking"] = 1
		out.append(s)
	out.sort(key=lambda s: int(s.get("sequence") or 0))
	return out


# ── per-stay record ───────────────────────────────────────────────────────

def _records(res) -> dict:
	raw = res.get("checkin_steps") if hasattr(res, "get") else None
	if not raw:
		return {}
	try:
		val = json.loads(raw)
		return val if isinstance(val, dict) else {}
	except (TypeError, ValueError):
		return {}


def record_step(res, key: str, state: str = "done", note: str | None = None,
                **extra) -> dict:
	"""Write one step's record (who, when) and the matching activity-log
	row. `extra` holds non-secret details for the record only."""
	recs = _records(res)
	rec = {"state": state, "by": frappe.session.user,
	       "at": str(now_datetime())}
	if note:
		rec["note"] = str(note)[:280]
	rec.update({k: v for k, v in extra.items() if v not in (None, "")})
	recs[key] = rec
	payload = json.dumps(recs, sort_keys=True, default=str)
	frappe.db.set_value("Reservation", res.name, "checkin_steps", payload,
	                    update_modified=False)
	res.checkin_steps = payload
	from kamra.savings import log_action
	what = "skipped" if state == "skipped" else "done"
	log_action(f"checkin_{key}", "Reservation", res.name, res.property,
	           rationale=f"Check-in step {key} {what} for {res.guest_name}"
	                     + (f": {str(note)[:140]}" if note else ""))
	return rec


# ── context ───────────────────────────────────────────────────────────────

def _symbol(property: str) -> str:
	from kamra.localization import pack_for
	prop = frappe.get_cached_doc("Property", property)
	return pack_for(property).locale(prop).get("currency_symbol", "")


def _ids(res, guest) -> dict:
	"""Whose ID is still missing, under the country's register rules: a
	minor is recorded by name and age only unless the pack keeps their ID."""
	from kamra.localization import is_minor, minor_id_kept, pack_for
	pack = pack_for(res.property)
	missing = []
	primary_ok = bool(guest and (guest.get("id_number") or guest.get("id_file"))
	                  or res.get("id_document"))
	if not primary_ok:
		missing.append(res.guest_name or _("the guest"))
	occupants, adults_named = [], 1
	for o in res.get("occupants") or []:
		# a blank age is stored as 0: it means not given, not a baby
		age = o.age or None
		minor = is_minor(pack, age)
		adults_named += 0 if minor else 1
		exempt = minor and not minor_id_kept(pack, o.nationality)
		has = bool(o.id_number or o.get("id_file"))
		if not exempt and not has:
			missing.append(o.full_name)
		occupants.append({
			"row": o.name, "full_name": o.full_name, "age": age,
			"guest_stated_age": o.get("guest_stated_age") or None,
			"gender": o.gender, "nationality": o.nationality, "id_type": o.id_type,
			"id_number": _mask(o.id_number), "has_id": has, "exempt": exempt,
			"has_photo": bool(o.get("photo")),
		})
	# every adult on the booking is in the register, by name and ID
	unnamed = max(0, cint(res.adults) - adults_named)
	if unnamed:
		missing.append(_("1 more adult") if unnamed == 1
		               else _("{0} more adults").format(unnamed))
	status = res.get("precheckin_status") or "Not Started"
	return {
		"ok": not missing,
		"missing": missing,
		"online": status in ("Submitted", "Verified"),
		"precheckin_status": status,
		"precheckin_on": str(res.precheckin_on) if res.get("precheckin_on") else None,
		"occupants": occupants,
	}


def _mask(value: str | None) -> str | None:
	if not value:
		return value
	v = str(value)
	return v if len(v) <= 4 else "•" * (len(v) - 4) + v[-4:]


def _billing(res) -> dict:
	"""Who pays, as the booking says it - the desk confirms or corrects."""
	company_name = rules = None
	if res.get("company"):
		company_name = frappe.db.get_value("Corporate Account", res.company,
		                                   "company_name") or res.company
		from kamra.folio import billing_rule_rows
		rules = billing_rule_rows(res.company)
	group_name = master = None
	if res.get("group_booking"):
		group_name = frappe.db.get_value("Group Booking", res.group_booking,
		                                 "group_name") or res.group_booking
		master = frappe.db.get_value(
			"Folio", {"group_booking": res.group_booking, "folio_type": "Group"},
			"name", order_by="window_no asc, creation desc")
	is_ota = res.source == "OTA" or bool(res.get("ota_ref"))
	if master:
		payer = "Group"
	elif res.get("company"):
		payer = "Company"
	elif is_ota:
		payer = "OTA, guest pays here" if res.get("is_pay_at_hotel") else "OTA prepaid"
	else:
		payer = "Guest"
	total = flt(res.amount_after_tax)
	paid = flt(res.advance_paid)
	folio = frappe.db.get_value(
		"Folio", {"reservation": res.name, "folio_type": "Guest"},
		["name", "balance"], as_dict=True)
	if folio:
		from kamra.folio import project_settlement_balances
		project_settlement_balances([folio], res.property)
		balance = flt(folio.balance)
	elif payer == "OTA prepaid":
		balance = 0.0
	else:
		balance = max(0.0, round(total - paid, 2))
	labels = {
		"OTA prepaid": _("{0} has collected the room. Extras are paid by the guest.").format(
			res.get("channel") or _("The OTA")),
		"OTA, guest pays here": _("{0} booking. The guest pays the hotel.").format(
			res.get("channel") or _("OTA")),
		"Guest": _("The guest pays."),
		"Company": _("{0} pays as agreed; the rest is paid by the guest.").format(
			company_name or _("The company")),
		"Group": _("The group account {0} pays as agreed.").format(group_name or ""),
	}
	return {
		"payer": payer,
		"payer_label": labels[payer],
		# every enabled payer type (system + hotel's own), as the description the hotel gave it
		"payer_labels": {r.name: r.description or r.name for r in _payer_rows()},
		"source": res.source, "channel": res.get("channel"),
		"ota_ref": res.get("ota_ref"),
		"pay_at_hotel": 1 if res.get("is_pay_at_hotel") else 0,
		"company": res.get("company"), "company_name": company_name,
		"billing_rules": rules or [],
		"group_booking": res.get("group_booking"), "group_name": group_name,
		"group_master_folio": master,
		"stay_total": total, "advance_paid": paid, "balance_due": balance,
		"options": payers(),
	}


def _payer_rows() -> list:
	from kamra.masters import PAYER, master_rows
	return master_rows(PAYER)


def _room(res) -> dict:
	from kamra.kamra.doctype.reservation.reservation import (
		entire_place_rooms, uses_member_rooms)
	out = {"room": res.room, "room_number": None, "housekeeping_status": None,
	       "entire_place": None,
	       "room_type": res.room_type,
	       "room_type_name": frappe.db.get_value("Room Type", res.room_type,
	                                             "room_type_name"),
	       "assigned_by": res.get("room_assigned_by"),
	       "assigned_at": str(res.room_assigned_at) if res.get("room_assigned_at") else None}
	if res.room:
		r = frappe.db.get_value("Room", res.room,
		                        ["room_number", "housekeeping_status"], as_dict=True) or {}
		out["room_number"] = r.get("room_number") or res.room
		out["housekeeping_status"] = r.get("housekeeping_status")
	else:
		members = res._composite_members()
		if not members and uses_member_rooms(res.property, res.room_type):
			members = entire_place_rooms(res.property, res.room_type)
		if members:
			out["entire_place"] = [r.room_number or r.name for r in members]
	return out


def _free_rooms(res) -> list[dict]:
	"""Rooms of the booked type the guest can go into now - clean first."""
	if res.status != "Confirmed":
		return []
	from kamra.api import available_rooms
	rank = {"Inspected": 0, "Ready": 0, "Clean": 1, "Pickup": 2, "Dirty": 3}
	rows = [{"name": r["name"], "room_number": r["room_number"],
	         "housekeeping_status": r["housekeeping_status"]}
	        for r in available_rooms(res.property, res.room_type,
	                                 str(res.check_in_date), str(res.check_out_date),
	                                 group_booking=res.group_booking, ready_now=1,
	                                 for_assignment=1)
	        if r["name"] != res.room
	        and r.get("housekeeping_status") not in ("Out of Order", "Out of Service")]
	rows.sort(key=lambda r: (rank.get(r["housekeeping_status"] or "", 4),
	                         len(str(r["room_number"])), str(r["room_number"])))
	return rows


def _deposit(res) -> dict:
	from kamra.api import _deposit_state
	from kamra.deposit import deposit_satisfied, deposit_state
	from kamra.ledger import _currency_places
	sec = deposit_state(res)
	adv = _deposit_state(res)
	eps = 0.5 * (10 ** -_currency_places(res.property))
	sec_applies = flt(sec.get("required")) > 0 and sec.get("status") != "None"
	adv_due = flt(adv.get("due")) if adv.get("can_take") else 0.0
	return {
		"applies": bool(sec_applies or adv_due > eps),
		"satisfied": deposit_satisfied(res.name),
		"security": sec,
		"advance": adv,
		"security_due": flt(sec.get("due")) if sec_applies else 0.0,
		"advance_due": adv_due,
	}


def stay_info(property: str) -> dict:
	from kamra.settings import get_setting
	vals = {k: get_setting(property, k) or "" for k in STAY_INFO_KEYS}
	vals["checkout_time"] = str(get_setting(property, "checkout_time") or "")[:5]
	vals["configured"] = any(vals[k] for k in STAY_INFO_KEYS)
	return vals


def _keys(res) -> dict:
	from kamra.door_lock import get_adapter
	adapter = get_adapter(res.property)
	return {
		"count": cint(res.get("key_count")),
		"cards": res.get("key_cards") or "",
		"issued_by": res.get("keys_issued_by"),
		"issued_at": str(res.keys_issued_at) if res.get("keys_issued_at") else None,
		"adapter": adapter.key, "adapter_label": adapter.label,
		"manual": adapter.key == "manual",
	}


def build_context(res, room_override: str | None = None) -> dict:
	from kamra.ledger import _currency_places
	from kamra.localization import pack_for

	guest = frappe.get_doc("Guest", res.guest) if res.guest else None
	prop = frappe.get_cached_doc("Property", res.property)
	loc = pack_for(res.property).locale(prop)
	symbol = loc.get("currency_symbol", "")
	currency = loc.get("currency") or prop.get("currency") or ""
	display_places = _currency_places(res.property)
	eps = 0.5 * (10 ** -display_places)
	billing = _billing(res)
	room = _room(res)
	if room_override and not room["room_number"]:
		room["room_number"] = frappe.db.get_value(
			"Room", room_override, "room_number") or room_override
	deposit = _deposit(res)
	records = _records(res)
	payer = (records.get("billing_check") or {}).get("payer") or billing["payer"]
	held = flt(deposit["security"].get("held"))
	return {
		"currency": currency,
		"currency_symbol": symbol,
		"currency_decimals": display_places,
		"records": records,
		"ids": _ids(res, guest),
		"billing": billing,
		"room": room,
		"deposit": deposit,
		"grc": {"signed": bool(res.get("precheckin_signature"))},
		"keys": _keys(res),
		"stay_info": stay_info(res.property),
		"special_requests": res.get("special_requests") or "",
		"room_charges": {
			"allowed": bool(cint(res.get("room_charges_allowed")
			                     if res.get("room_charges_allowed") is not None else 1)),
			"recommend_block": payer == "OTA prepaid" and held <= eps,
		},
		"_guest": guest,
	}


def evaluate(res, room_override: str | None = None) -> tuple[list[dict], dict]:
	from kamra.sop import evaluate_checkin_step
	ctx = build_context(res, room_override)
	steps = [evaluate_checkin_step(
		{"step_key": s.get("step_key") or "", "step_type": s.get("step_type") or "confirm",
		 "title": s.get("title") or "", "instruction": s.get("instruction") or "",
		 "prompt_to_guest": s.get("prompt_to_guest") or "",
		 "is_blocking": 1 if cint(s.get("is_blocking")) else 0,
		 "sequence": cint(s.get("sequence"))}, ctx)
		for s in checkin_steps(res.property)]
	return steps, ctx


def hard_missing(steps: list[dict]) -> list[dict]:
	"""Steps that refuse check_in: only the room (no room, no stay)."""
	from kamra.sop import CHECKIN_ALWAYS_REQUIRED
	return [s for s in steps
	        if s.get("step_key") in CHECKIN_ALWAYS_REQUIRED
	        and s.get("relevant") and s.get("state") != "done"]


def soft_pending(steps: list[dict]) -> list[dict]:
	"""Property-marked required steps still open — warn and backfill later;
	they do not refuse check_in."""
	from kamra.sop import CHECKIN_ALWAYS_REQUIRED
	return [s for s in steps
	        if s.get("is_blocking") and s.get("relevant") and s.get("state") != "done"
	        and s.get("step_key") not in CHECKIN_ALWAYS_REQUIRED]


def missing_required(steps: list[dict]) -> list[dict]:
	"""What still blocks check_in (room only). Kept name for callers."""
	return hard_missing(steps)


def assert_ready(res, room: str | None = None) -> None:
	"""check_in's hard gate: a room must be assigned. Other required steps
	are soft — check_in goes through and the desk finishes them later.
	Turned off only by server code (frappe.flags), never by a request."""
	if frappe.flags.kamra_checkin_steps_off:
		return
	steps, _ctx = evaluate(res, room_override=room)
	todo = hard_missing(steps)
	if todo:
		frappe.throw(
			_("Assign a room before check-in."),
			exc=CheckInStepsMissing, title=_("Check-in not finished"))


def after_check_in(res) -> dict | None:
	"""Issue the recorded number of keys once the guest has checked in."""
	count = cint(res.get("key_count"))
	if not count or not res.room:
		return None
	from kamra.door_lock import get_adapter
	adapter = get_adapter(res.property)
	if adapter.key == "fias_edge":
		request = _key_request(res, res.room, count, "issue")
		# Commit a retryable outbox entry with the stay. Redis failure or process
		# exit before queue acknowledgement requires no further write to recover.
		ticket = _audit_key(request, adapter.key, "Retrying", frappe.session.user)

		def enqueue_arrival():
			try:
				_enqueue_arrival(res.name, ticket.name, count)
			except Exception:
				import logging
				logging.getLogger(__name__).warning("Arrival key queue acknowledgement failed for audit %s", ticket.name)

		frappe.db.after_commit.add(enqueue_arrival)
		return {"ok": True, "adapter": adapter.key, "manual": False,
		        "message": _("Room keys queued for the encoder.")}
	return _call_adapter(adapter, res, count)


def _enqueue_arrival(reservation, ticket, count, job_id=None):
	from frappe.utils.background_jobs import create_job_id, get_queue
	job_id = job_id or f"arrival-key-{ticket}"
	kwargs = dict(reservation=reservation, ticket=ticket, count=count, queue="short")
	method = "kamra.checkin.dispatch_arrival_keys"
	job = frappe.enqueue(method, job_id=job_id, deduplicate=True, **kwargs)
	if job is None:
		# RQ can retain a QUEUED hash after the queue entry is gone.
		queue = get_queue("short")
		redis_id = create_job_id(job_id)
		if queue.get_job_position(redis_id) is None and redis_id not in queue.started_job_registry.get_job_ids():
			job_id = f"arrival-key-{ticket}-{frappe.generate_hash(length=16)}"
			frappe.enqueue(method, job_id=job_id, **kwargs)
	# Acknowledge on a separate connection after Redis accepts the job. If a
	# worker already holds the ticket, let it finish; check-in must not wait
	# for the encoder. A crashed worker leaves a recoverable outbox entry.
	conn = frappe.db.get_connection()
	try:
		with conn.cursor() as cursor:
			cursor.execute("""SELECT name FROM `tabDoor Key Audit` WHERE name=%s
				AND status IN ('Retrying','Queued') FOR UPDATE SKIP LOCKED""", (ticket,))
			if cursor.fetchone():
				cursor.execute("UPDATE `tabDoor Key Audit` SET status='Queued', delivery_job_id=%s, modified=%s WHERE name=%s",
				               (job_id, now_datetime(), ticket))
		conn.commit()
	finally:
		conn.close()


def reconcile_arrival_keys():
	"""Redeliver unqueued arrivals and queued jobs lost before a worker acknowledged them."""
	from frappe.utils import add_to_date
	tickets = frappe.db.sql("""
		SELECT a.name, a.reservation, a.delivery_job_id, COALESCE(NULLIF(a.key_count, 0), r.key_count) AS key_count
		FROM `tabDoor Key Audit` a JOIN `tabReservation` r
		  ON r.name=a.reservation AND r.property=a.property AND r.creation<=a.creation
		WHERE a.operation='issue' AND (a.status='Retrying' OR (a.status='Queued' AND a.modified<%s))
		ORDER BY a.modified, a.name LIMIT 100""", add_to_date(now_datetime(), minutes=-5), as_dict=True)
	for row in tickets:
		_enqueue_arrival(row.reservation, row.name, cint(row.key_count), row.delivery_job_id)


def dispatch_arrival_keys(reservation, ticket, count):
	from kamra.door_lock import get_adapter
	res = frappe.get_doc("Reservation", reservation, for_update=True)
	intent = frappe.get_doc("Door Key Audit", ticket, for_update=True)
	if (intent.reservation != res.name or intent.property != res.property or intent.operation != "issue"
			or intent.creation < res.creation or intent.status not in ("Queued", "Retrying")):
		return
	adapter = get_adapter(res.property)
	count = cint(intent.key_count or count)
	status = "Cancelled"
	if (1 <= count <= 10 and res.status == "Checked In" and res.room == intent.room
			and adapter.key == intent.adapter_key):
		superseded = frappe.db.get_value("Door Key Audit", {
			"property": res.property, "reservation": res.name, "room": res.room,
			"name": ("!=", _arrival_operation_id(res, ticket)), "creation": (">", intent.creation),
			"status": ("in", ["Accepted", "Manual"]),
		}, "name")
		if not superseded:
			result = _call_adapter(adapter, res, count, actor=intent.actor, request_id=ticket)
			status = "Completed" if result["ok"] else "Failed"
	frappe.db.set_value("Door Key Audit", ticket, "status", status)


def _arrival_operation_id(res, ticket):
	from uuid import NAMESPACE_URL, uuid5
	return str(uuid5(NAMESPACE_URL, f"{res.property}/{res.name}/{ticket}"))


def _key_request(res, room, count, operation, request_id=None):
	from kamra.door_lock import KeyRequest
	from kamra.settings import get_setting
	valid_from = str(property_local_now(res.property).replace(microsecond=0))
	departure = None
	if res.get("is_day_use"):
		departure = res.get("planned_check_out_time")
		arrival = res.get("planned_check_in_time")
		if arrival:
			from frappe.utils import get_time
			valid_from = max(valid_from, f"{res.check_in_date} {get_time(arrival)}")
	out_time = str(departure or get_setting(res.property, "checkout_time") or "11:00").split(".")[0]
	if out_time.count(":") == 1:
		out_time += ":00"
	return KeyRequest(
		property=res.property, reservation=res.name, room=room,
		room_number=frappe.db.get_value("Room", room, "room_number") or room,
		valid_from=valid_from,
		valid_to=f"{res.check_out_date} {out_time}", key_count=count,
		options={"operation": operation, "encoder_id": get_setting(res.property, "door_lock_encoder"),
		         "request_id": request_id})


def _audit_key(req, adapter_key, status, actor, issuer_target=None):
	return frappe.get_doc({
		"doctype": "Door Key Audit", "property": req.property, "reservation": req.reservation,
		"room": req.room, "operation": req.options["operation"], "encoder_id": req.options["encoder_id"],
		"valid_from": req.valid_from, "valid_to": req.valid_to, "adapter_key": adapter_key,
		"key_count": req.key_count, "issuer_target": issuer_target,
		"status": status, "actor": actor,
	}).insert(ignore_permissions=True)


def _dispatch_key(adapter, req, issuer, target):
	from dataclasses import replace
	from kamra.door_lock import KeyResult
	operation = req.options["operation"]
	try:
		if issuer != (adapter.key, target):
			message = _("The original lock adapter is unavailable.")
			if issuer[1]:
				message += f" ({issuer[0]} · {issuer[1][:12]})"
			result = KeyResult(False, issuer[0], message)
		elif operation.endswith("revoke") and not adapter.supports_revoke and adapter.key != "manual":
			result = KeyResult(False, adapter.key, _("The lock system did not answer. Cancel the keys at the encoder."))
		elif operation.endswith("revoke"):
			result = adapter.revoke_request(req)
		elif operation == "mobile" and not adapter.supports_mobile_keys:
			result = KeyResult(False, adapter.key, _("Mobile keys are unavailable."))
		elif operation == "rekey":
			result = adapter.revoke_request(req)
			if result.ok and not result.manual:
				issue = replace(req, options={**req.options, "operation": "issue"})
				result = adapter.issue_key(issue)
		else:
			result = adapter.issue_key(req)
	except Exception:
		message = _("The lock system did not answer. Cancel the keys at the encoder.") if operation.endswith("revoke") else _("The lock system did not answer. Make the keys at the encoder.")
		result = KeyResult(False, adapter.key, message, uncertain=True)
	return result


def _call_adapter(adapter, res, count, operation="issue", room=None, actor=None, request_id=None, issuer=None):
	"""Keep the reservation transaction and its locks independent of the durable I/O journal."""
	from uuid import uuid4
	from frappe.utils import now_datetime
	room = room or res.room
	actor = actor or frappe.session.user
	operation_id = _arrival_operation_id(res, request_id) if request_id else str(uuid4())
	req, target = adapter.bind_target(_key_request(res, room, count, operation, operation_id))
	current = (adapter.key, target)
	if issuer is None and operation in ("revoke", "reverse_checkin_revoke", "rekey"):
		# Desk revokes, reversed arrivals and re-keys also invalidate old cards.
		issuer = next((old for old in _outstanding_issuers(res, room) if old != current), None)
	adapter_key, issuer_target = issuer or current
	created = now_datetime()
	conn = frappe.db.get_connection()
	try:
		with conn.cursor() as cursor:
			if request_id:
				cursor.execute("SELECT status FROM `tabDoor Key Audit` WHERE name=%s", (operation_id,))
				previous = cursor.fetchone()
				if previous:
					# Recover the committed outcome without repeating hardware I/O.
					return {"ok": previous[0] in ("Accepted", "Manual"), "status": previous[0]}
			cursor.execute("""INSERT INTO `tabDoor Key Audit`
				(name, owner, creation, modified, modified_by, docstatus, idx, property, reservation,
				 room, operation, encoder_id, valid_from, valid_to, key_count, adapter_key, issuer_target, status, actor)
				VALUES (%s,%s,%s,%s,%s,0,0,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Pending',%s)""",
				(operation_id, actor, created, created, actor, req.property, req.reservation, room, operation,
				 req.options["encoder_id"], req.valid_from, req.valid_to, req.key_count, adapter_key, issuer_target, actor))
			conn.commit()  # independent journal: dispatch is forbidden until Pending is durable
			result = _dispatch_key(adapter, req, (adapter_key, issuer_target), target)
			status = ("Manual" if result.manual else "Accepted") if result.ok else "Failed"
			if result.uncertain and not operation.endswith("revoke"):
				status = "Pending"  # physical issuance cannot be ruled out
			cursor.execute("""UPDATE `tabDoor Key Audit` SET status=%s, modified=%s
				WHERE name=%s AND status='Pending'""", (status, now_datetime(), operation_id))
			conn.commit()
	finally:
		conn.close()
	return {"ok": result.ok, "adapter": adapter_key, "manual": result.manual, "operation_id": operation_id,
	        "message": result.message, "key_ids": list(result.key_ids or [])}


def revocation_warnings(res, rooms, operation, warnings=None):
	"""Commit one intent per issuing target with the reservation transaction."""
	from kamra.door_lock import get_adapter
	try:
		adapter = get_adapter(res.property)
	except Exception:
		adapter = None
		frappe.log_error(title="Door lock adapter lookup failed")
	warnings = [] if warnings is None else warnings
	for room in filter(None, rooms):
		req = _key_request(res, room, 0, operation)
		current = None
		if adapter:
			try:
				req, target = adapter.bind_target(req)
				current = (adapter.key, target)
			except Exception:
				frappe.log_error(title="Door lock target lookup failed")
		# A missing connector must not erase a known issuer or turn an
		# unknown target into a successful manual revocation.
		issuers = _outstanding_issuers(res, room) or [current or (None, None)]
		for issuer, fingerprint in issuers:
			manual = current == ("manual", None) and issuer == "manual"
			intent = _audit_key(req, issuer, "Manual" if manual else "Queued", frappe.session.user, fingerprint)
			if manual:
				continue
			warning = {"operation_id": intent.name, "reservation": res.name, "room": room,
			           "adapter": issuer, "issuer_target": fingerprint, "status": "Queued"}
			if current is None:
				warning["message"] = _("The lock system did not answer. Cancel the keys at the encoder.")
			warnings.append(warning)

			def after_commit(entry=warning):
				result = _deliver_committed_revocation(entry["operation_id"])
				if result:
					entry.update(message=result["message"], status=result["status"])
					if result["ok"]:
						warnings.remove(entry)

			frappe.db.after_commit.add(after_commit)
	return warnings


def _dispatch_revocation_intent(res, name):
	"""The reservation lock serializes delivery, retries, moves and replacement keys."""
	from kamra.door_lock import get_adapter
	record = frappe.get_doc("Door Key Audit", name, for_update=True)
	if (record.reservation != res.name or record.property != res.property or record.creation < res.creation
			or record.operation not in ("room_move_revoke", "checkout_revoke", "reverse_checkin_revoke")):
		return None
	if record.status != "Queued":
		return {"ok": record.status in ("Accepted", "Manual"), "status": record.status, "message": ""}
	if ((res.status == "Checked In" and record.room in res._occupied_rooms(checking_in=False))
			or _committed_key_history(res, room=record.room, issued_after=record)):
		status, message = "Cancelled", _("Review the room's current keys before starting a new revocation.")
	else:
		adapter = get_adapter(res.property)
		req, target = adapter.bind_target(_key_request(res, record.room, 0, record.operation, name))
		result = _dispatch_key(adapter, req, (record.adapter_key, record.issuer_target), target)
		status = ("Manual" if result.manual else "Accepted") if result.ok else "Failed"
		message = result.message
	frappe.db.set_value("Door Key Audit", name, "status", status)
	return {"ok": status in ("Accepted", "Manual"), "status": status, "message": message, "operation_id": name}


def _deliver_committed_revocation(name):
	# Isolate this callback from all other after-commit hooks. A crash before
	# acknowledgement leaves the same committed operation ID for redelivery.
	from frappe.database import get_db
	writer = frappe.local.db
	db = get_db(socket=writer.socket, host=writer.host, port=writer.port, user=writer.user,
	            password=writer.password, cur_db_name=writer.cur_db_name)
	try:
		frappe.local.db = db
		reservation = db.get_value("Door Key Audit", name, "reservation")
		if reservation and db.exists("Reservation", reservation):
			result = _dispatch_revocation_intent(frappe.get_doc("Reservation", reservation, for_update=True), name)
			db.commit()  # nosemgrep: frappe-manual-commit -- independent post-commit delivery acknowledgement
			return result
	except Exception:
		import logging
		logging.getLogger(__name__).exception("Could not acknowledge revocation %s", name)
	finally:
		frappe.local.db = writer
		db.close()


def reconcile_revocations():
	for name, in frappe.db.sql("""
		SELECT a.name FROM `tabDoor Key Audit` a JOIN `tabReservation` r
		ON r.name=a.reservation AND r.property=a.property AND r.creation<=a.creation
		WHERE a.status='Queued' AND a.operation IN ('room_move_revoke','checkout_revoke','reverse_checkin_revoke')
		ORDER BY a.creation LIMIT 100"""):
		_deliver_committed_revocation(name)


@frappe.whitelist(methods=["POST"])
@require_roles(*STAFF)
def retry_revocation(reservation: str, operation_id: str):
	from kamra.door_lock import get_adapter
	res = _res(reservation, for_update=True)
	# The reservation is locked. A failed journal entry is immutable. Read it
	# from the independent journal without taking a second set of row locks.
	records = _committed_key_history(res, operation_id=operation_id)
	record = records[0] if records else None
	if record and record.operation.endswith("revoke"):
		if record.status == "Queued":
			return _dispatch_revocation_intent(res, record.name)
		if record.status in ("Accepted", "Manual"):
			return {"ok": True, "manual": record.status == "Manual", "operation_id": record.name, "message": ""}
	if not record or record.status != "Failed" or not record.operation.endswith("revoke"):
		frappe.throw(_("Choose a failed key revocation."))
	if (record.adapter_key != "fias_edge" or record.issuer_target) and _committed_key_history(
		res, room=record.room, revoked_after=record
	):
		return {"ok": True, "manual": False, "operation_id": operation_id, "message": ""}
	if _committed_key_history(res, room=record.room, issued_after=record):
		frappe.throw(_("Review the room's current keys before starting a new revocation."))
	adapter = get_adapter(res.property)
	if adapter.key != record.adapter_key:
		frappe.throw(_("The original lock adapter is unavailable."))
	# Do not invalidate a subsequent assignment when a stay moves back.
	if res.status == "Checked In" and record.room in res._occupied_rooms(checking_in=False) and record.operation != "revoke":
		frappe.throw(_("Review the room's current keys before starting a new revocation."))
	return _call_adapter(adapter, res, 0, record.operation, record.room,
	                     issuer=(record.adapter_key, record.issuer_target))


def _outstanding_issuers(res, room):
	"""Read external issuances that a matching target has not revoked.

	Before target journaling, a failed physical dispatch (UUID operation ID)
	could be a lost acknowledgement. Outbox tickets have separate hash names.
	"""
	conn = frappe.db.get_connection()
	try:
		with conn.cursor() as cursor:
			cursor.execute("""
				SELECT DISTINCT i.adapter_key, i.issuer_target FROM `tabDoor Key Audit` i
				WHERE i.property=%s AND i.reservation=%s AND i.room=%s AND i.creation>=%s
				  AND i.adapter_key!='manual' AND i.operation IN ('issue','duplicate','mobile','rekey')
				  AND (i.status IN ('Accepted','Pending') OR
				       (i.status='Failed' AND i.issuer_target IS NULL AND CHAR_LENGTH(i.name)=36))
				  AND NOT EXISTS (
				    SELECT 1 FROM `tabDoor Key Audit` r
				    WHERE r.property=i.property AND r.reservation=i.reservation AND r.room=i.room
				      AND r.adapter_key=i.adapter_key AND r.issuer_target <=> i.issuer_target
				      AND (i.adapter_key!='fias_edge' OR i.issuer_target IS NOT NULL)
				      AND r.creation>i.creation AND r.status='Accepted'
				      AND r.operation IN ('revoke','room_move_revoke','checkout_revoke','reverse_checkin_revoke')
				  )""", (res.property, res.name, room, res.creation))
			return cursor.fetchall()
	finally:
		conn.close()


@frappe.whitelist(methods=["POST"])
@require_roles(*STAFF)
def key_operation(reservation: str, operation: str):
	from kamra.door_lock import get_adapter
	from kamra.crs import assert_property_access
	res = frappe.get_doc("Reservation", reservation, for_update=True)
	assert_property_access(res.property)
	if res.status != "Checked In" or not res.room:
		frappe.throw(_("A checked-in stay with a room is required."))
	if operation not in ("issue", "duplicate", "rekey", "revoke", "mobile"):
		frappe.throw(_("Unknown key operation."))
	return _call_adapter(get_adapter(res.property), res, 1, operation)


# ── endpoints ─────────────────────────────────────────────────────────────

def _res(reservation: str, *, for_update=False):
	res = frappe.get_doc("Reservation", reservation, for_update=for_update)
	from kamra.crs import assert_property_access
	assert_property_access(res.property)
	return res


def _open(res):
	if res.status not in ("Confirmed", "Checked In"):
		frappe.throw(_("Only a confirmed or in-house stay has check-in steps (this one is {0}).")
		             .format(_(res.status)))


def _step(res, key: str) -> dict:
	for s in checkin_steps(res.property):
		if s.get("step_key") == key:
			return s
	frappe.throw(_("{0} is not a check-in step at this property.").format(key))
	return {}  # unreachable: frappe.throw always raises


@frappe.whitelist()
@require_roles(*DESK)
def checkin_flow(reservation: str):
	"""Everything the guided check-in shows, in one round trip: the steps
	(required or optional, done, skipped or to do, and who did them), plus
	what each step screen needs."""
	res = _res(reservation)
	steps, ctx = evaluate(res)
	guest = ctx.pop("_guest")
	return _flow_payload(res, steps, ctx, guest)


def _till(property: str) -> dict:
	from kamra.cashier import till_state
	return till_state(property)


def _flow_payload(res, steps, ctx, guest) -> dict:
	from kamra.localization import front_desk_vocabulary, pack_for
	from kamra.traces import check_point_alerts
	alerts = check_point_alerts("Check-In", res.property, reservation=res.name, guest=res.guest)
	return {
		"alerts": alerts,
		"early_checkin_fee": early_checkin_fee(res),
		"reservation": {
			"name": res.name, "status": res.status,
			"guest": res.guest, "guest_name": res.guest_name,
			"room_type": res.room_type,
			"check_in_date": str(res.check_in_date),
			"check_out_date": str(res.check_out_date),
			"nights": res.nights, "adults": res.adults, "children": res.children,
			"vip": cint(guest.get("vip")) if guest else 0,
			"source": res.source, "special_requests": res.get("special_requests") or "",
			"checked_in_by": res.get("checked_in_by"),
			"checked_in_at": str(res.checked_in_at) if res.get("checked_in_at") else None,
		},
		"guest": {
			"name": guest.name if guest else None,
			"full_name": guest.full_name if guest else res.guest_name,
			"phone": guest.get("phone") if guest else None,
			"email": guest.get("email") if guest else None,
			"nationality": guest.get("nationality") if guest else None,
			"id_type": guest.get("id_type") if guest else None,
			"id_number": _mask(guest.get("id_number")) if guest else None,
			"id_on_file": bool(guest and (guest.get("id_number") or guest.get("id_file"))
			                   or res.get("id_document")),
			# what the profile already holds, so the desk can reuse it instead
			# of asking again; warnings only, never a reason to stop a check-in
			"id_expiry_date": str(guest.get("id_expiry_date") or "") or None if guest else None,
			"id_status": id_status(guest.get("id_expiry_date"), bool(
				guest.get("id_number") or guest.get("id_file") or res.get("id_document")))
			if guest else "none",
			"id_used_count": used_stays_count(guest.name) if guest else 0,
			"has_photo": bool(guest and guest.get("photo")),
			"gender": guest.get("gender") if guest else None,
			"date_of_birth": str(guest.get("date_of_birth") or "") or None if guest else None,
			"address": ", ".join(x for x in [guest.get("address_line"), guest.get("city")]
			                     if x) if guest else "",
		},
		"vocabulary": front_desk_vocabulary(pack_for(res.property), res.property),
		"steps": steps,
		"missing_required": [s["step_key"] for s in hard_missing(steps)],
		"pending_backfill": [
			{"step": s["step_key"], "title": s["title"], "tip": s.get("tip") or ""}
			for s in soft_pending(steps)],
		"rooms": _free_rooms(res) if not res.room else [],
		"request_presets": [{"key": k, "label": v[0], "priced": bool(v[3])}
		                    for k, v in request_presets().items()],
		# the deposit step takes money: is the operator's till open, or can
		# they open it here (kamra.cashier.open_session)
		"till": _till(res.property),
		**{k: v for k, v in ctx.items() if k != "records"},
		"records": ctx["records"],
	}


@frappe.whitelist()
@require_roles("Front Desk", "Night Auditor", "Finance", "Hotel Admin", "Kamra Agent")
def check_in_readiness(reservation: str):
	"""Read-only: which check-in steps are done, whether check_in would go
	through now (room assigned), and which soft-required steps are still
	open to finish later. Agents read this; they cannot do the desk's
	steps for it."""
	res = _res(reservation)
	steps, _ctx = evaluate(res)
	hard = hard_missing(steps)
	soft = soft_pending(steps)
	return {
		"reservation": res.name, "status": res.status,
		"ready": res.status == "Confirmed" and not hard,
		"missing_required": [{"step": s["step_key"], "title": s["title"], "tip": s["tip"]}
		                     for s in hard],
		"pending_backfill": [{"step": s["step_key"], "title": s["title"], "tip": s["tip"]}
		                     for s in soft],
		"steps": [{"step": s["step_key"], "title": s["title"],
		           "required": s["is_blocking"], "applies": s["relevant"],
		           "state": s["state"], "done_by": s.get("done_by"),
		           "done_at": s.get("done_at")} for s in steps],
	}


@frappe.whitelist(methods=["POST"])
@require_roles(*STAFF)
def complete_checkin_step(reservation: str, step: str, note: str | None = None):
	"""The desk marks a step done (a confirm-only step, or the review of
	what the guest sent online). Refused while what the step stands for is
	not true yet - an ID still missing, a deposit not taken."""
	res = _res(reservation)
	_open(res)
	_step(res, step)
	steps, _ctx = evaluate(res)
	ev = next(s for s in steps if s["step_key"] == step)
	if not ev.get("facts_ok"):
		frappe.throw(ev.get("tip") or _("This step is not finished yet."))
	if step in ("billing_check", "room_charges", "keys", "guest_requests"):
		frappe.throw(_("Use the {0} screen for this step.").format(_(ev["title"])))
	record_step(res, step, "done", note=note)
	return checkin_flow(reservation)


@frappe.whitelist(methods=["POST"])
@require_roles(*STAFF)
def skip_checkin_step(reservation: str, step: str, reason: str | None = None):
	"""Skip a step to finish later. Only the room cannot be skipped."""
	from kamra.sop import CHECKIN_ALWAYS_REQUIRED
	res = _res(reservation)
	_open(res)
	s = _step(res, step)
	if (s.get("step_key") or step) in CHECKIN_ALWAYS_REQUIRED:
		frappe.throw(_("{0} cannot be skipped — assign a room first.")
		             .format(_(s.get("title") or step)))
	note = (reason or "").strip() or _("Backfill after check-in")
	record_step(res, step, "skipped", note=note)
	return checkin_flow(reservation)


@frappe.whitelist(methods=["POST"])
@require_roles(*STAFF)
def save_guest_details(reservation: str, phone: str | None = None,
                       email: str | None = None, id_type: str | None = None,
                       id_number: str | None = None,
                       nationality: str | None = None,
                       date_of_birth: str | None = None,
                       gender: str | None = None,
                       id_expiry_date: str | None = None,
                       address_line: str | None = None,
                       city: str | None = None):
	"""Correct the primary guest's contact and ID details at the desk, or
	keep what an ID scan read. Only what is passed changes; an ID type must
	be one the country recognises."""
	res = _res(reservation)
	_open(res)
	if not res.guest:
		frappe.throw(_("This booking has no guest profile."))
	from kamra.localization import validate_id_type
	changes = {}
	if phone is not None:
		changes["phone"] = phone.strip() or None
	if email is not None:
		changes["email"] = email.strip() or None
	if nationality is not None:
		changes["nationality"] = nationality.strip() or None
	from frappe.utils import getdate
	for field, value in (("date_of_birth", date_of_birth), ("id_expiry_date", id_expiry_date)):
		if value:
			changes[field] = str(getdate(value))
	# Data fields hold 140 characters; an Aadhaar address can run longer
	for field, value in (("address_line", address_line), ("city", city)):
		if value and value.strip():
			changes[field] = value.strip()[:140]
	if gender in ("Male", "Female", "Other"):
		changes["gender"] = gender
	if id_type:
		if id_type != frappe.db.get_value("Guest", res.guest, "id_type"):
			validate_id_type(res.property, id_type)
		changes["id_type"] = id_type
	if id_number is not None and id_number.strip():
		from kamra import localization as loc
		stored = loc.store_id_number(loc.pack_for(res.property),
		                           id_type or frappe.db.get_value("Guest", res.guest, "id_type"),
		                           id_number)
		if stored is not None:
			changes["id_number"] = stored
		elif "•" not in id_number:
			changes["id_number"] = id_number.strip()
	current = frappe.db.get_value("Guest", res.guest, list(changes), as_dict=True) \
		if changes else {}
	changes = {k: v for k, v in changes.items() if (current or {}).get(k) != v}
	if changes:
		# field names only: the activity log never holds the ID number itself
		changed = ", ".join(sorted(changes))
		frappe.db.set_value("Guest", res.guest, changes)
		from kamra.savings import log_action
		log_action("guest_details", "Reservation", res.name, res.property,
		           rationale=f"Desk updated {changed}")
	return checkin_flow(reservation)


@frappe.whitelist(methods=["POST"])
@require_roles(*STAFF)
def confirm_billing(reservation: str, payer: str, note: str | None = None):
	"""The desk confirms who pays (OTA, guest, company, group)."""
	res = _res(reservation)
	_open(res)
	options = payers()
	if payer not in options:
		frappe.throw(_("Pick who pays: {0}.").format(", ".join(_(p) for p in options)))
	if payer == "Company" and not res.get("company"):
		frappe.throw(_("This booking has no company. Add the company to the booking first."))
	if payer == "Group" and not res.get("group_booking"):
		frappe.throw(_("This booking is not part of a group."))
	record_step(res, "billing_check", "done", note=note, payer=payer)
	return checkin_flow(reservation)


@frappe.whitelist(methods=["POST"])
@require_roles(*STAFF)
def assign_checkin_room(reservation: str, room: str | None = None):
	"""Keep the assigned room, or put the guest in another free room of
	the booked type. Upgrades and downgrades (a different type, maybe a
	new price) go through the room move screen (room_move_preview +
	move_reservation), which also records who assigned the room."""
	res = _res(reservation)
	_open(res)
	if res.status != "Confirmed":
		frappe.throw(_("The guest is already in house. Use Room move to change rooms."))
	if room and room != res.room:
		new_type = frappe.db.get_value("Room", room, "room_type")
		if new_type != res.room_type:
			frappe.throw(_("That room is another type. Use Upgrade or change type, so the price is shown first."))
		from kamra.api import move_reservation
		move_reservation(reservation, room, reason="Assigned at check-in")
		res.reload()
	if not res.room:
		ctx_room = _room(res)
		if not ctx_room.get("entire_place"):
			frappe.throw(_("Pick a room first."))
	number = frappe.db.get_value("Room", res.room, "room_number") if res.room else None
	record_step(res, "assign_room", "done",
	            note=_("Room {0}").format(number) if number else _("Whole place"),
	            room=res.room)
	return checkin_flow(reservation)


@frappe.whitelist(methods=["POST"])
@require_roles(*STAFF)
def sign_registration_card(reservation: str, signature: str | None = None,
                           on_paper: int = 0):
	"""The guest signs the registration card on screen (stored like the
	online signature) or on the printed card (on_paper=1)."""
	res = _res(reservation)
	_open(res)
	signed = bool(signature and str(signature).startswith("data:image"))
	if not signed and not cint(on_paper):
		frappe.throw(_("Have the guest sign on screen, or tick that they signed the printed card."))
	if signed:
		if len(signature) > 400_000:
			frappe.throw(_("That signature image is too large."))
		frappe.db.set_value("Reservation", res.name, {
			"precheckin_signature": signature, "precheckin_consent": 1,
		}, update_modified=False)
		res.precheckin_signature = signature
	record_step(res, "registration_card", "done",
	            note=_("Signed on screen") if signed else _("Signed on paper"),
	            method="screen" if signed else "paper")
	return checkin_flow(reservation)


@frappe.whitelist(methods=["POST"])
@require_roles(*STAFF)
def issue_keys(reservation: str, key_count: int = 1,
               key_cards: str | None = None):
	"""Record the keys handed to the guest, and who handed them. With a
	connected lock system the keys are made by it as soon as the guest is
	checked in (now, if already in house); with no lock system the desk
	encodes the cards at the encoder and notes the card numbers here."""
	res = _res(reservation, for_update=True)
	_open(res)
	count = cint(key_count)
	if count < 1 or count > 10:
		frappe.throw(_("Keys: between 1 and 10."))
	cards = (key_cards or "").strip()[:280] or None
	now = now_datetime()
	frappe.db.set_value("Reservation", res.name, {
		"key_count": count, "key_cards": cards,
		"keys_issued_by": frappe.session.user, "keys_issued_at": now,
	}, update_modified=False)
	res.key_count, res.key_cards = count, cards
	from kamra.door_lock import get_adapter
	adapter = get_adapter(res.property)
	result = None
	if adapter.key == "manual":
		number = frappe.db.get_value("Room", res.room, "room_number") if res.room else ""
		result = {"ok": True, "adapter": "manual", "manual": True,
		          "message": _("Encode {0} key card(s) for room {1} at the encoder.")
		          .format(count, number or "")}
	elif res.status == "Checked In" and res.room:
		result = _call_adapter(adapter, res, count)
	record_step(res, "keys", "done", note=_("{0} key(s)").format(count),
	            count=count, adapter=adapter.key)
	out = checkin_flow(reservation)
	out["key_result"] = result
	return out


def _request_tax_rate(res, charge_type: str) -> float:
	"""An extra bed is taxed as the room it is in (that night's rate);
	a taxi or other service at the country pack's services rate."""
	from kamra.erp_bridge import is_bridge_active
	if is_bridge_active():
		return 0.0
	if charge_type == "Extra Guests":
		from kamra.business_date import get_business_date
		from kamra.folio import _nightly_gst
		return _nightly_gst(res, get_business_date(res.property))
	from kamra.localization import pack_for, services_tax_rate
	return services_tax_rate(pack_for(res.property), res.property)


@frappe.whitelist(methods=["POST"])
@require_roles(*STAFF)
def log_checkin_requests(reservation: str, requests=None, note: str | None = None,
                         prices=None):
	"""Quick requests at check-in become service tickets (one each); a free
	note is kept on the step and, if given alone, raised as one ticket.
	`prices` ({request: amount}) bills a paid request (an extra bed, an
	airport drop) to the stay, by the hotel's tax rules and billing rules."""
	if isinstance(requests, str):
		requests = frappe.parse_json(requests) if requests.strip() else []
	presets = request_presets()
	requests = [r for r in (requests or []) if r in presets]
	note = (note or "").strip()[:500]
	if isinstance(prices, str):
		prices = frappe.parse_json(prices) if prices.strip() else {}
	from kamra.api import (
		_lock_folio_scope,
		_with_snapshot_retry,
		create_ticket,
		post_stay_charge,
	)

	def _log_once():
		_lock_folio_scope(reservations=[reservation])
		res = _res(reservation)
		_open(res)
		tickets = []
		# the step can be saved again (back, or a retry): a request already on
		# the bill is not charged twice. The row lock makes two saves at once
		# queue here, so the second reads the first one's `billed`.
		res.checkin_steps = frappe.db.get_value("Reservation", res.name, "checkin_steps",
		                                        for_update=True)
		billed = list((_records(res).get("guest_requests") or {}).get("billed") or [])
		# STAFF is checked above, but the night auditor has no folio or ticket
		# rights of its own: it posts as the governed agent, as housekeeping
		# charges do, and the step record below keeps who did it
		from kamra.authz import act_as
		from kamra.install import AGENT_EMAIL
		can_bill = frappe.has_permission("Folio", "write")
		can_ticket = frappe.has_permission("Service Ticket", "create")
		as_agent = not (can_bill and can_ticket)
		with act_as(AGENT_EMAIL) if as_agent else nullcontext():
			for key in requests:
				subject, category, priority, charge_type = presets[key]
				amount = flt((prices or {}).get(key))
				if charge_type and amount > 0 and key not in billed:
					post_stay_charge(res.name, charge_type, subject, amount,
					                 _request_tax_rate(res, charge_type))
					billed.append(key)
				tickets.append(create_ticket(
					res.property, f"{subject} ({res.guest_name})", category,
					priority=priority, room=res.room, reservation=res.name,
					guest=res.guest, description=note or None)["ticket"])
			if note and not requests:
				tickets.append(create_ticket(
					res.property, f"Request at check-in ({res.guest_name})", "Front Desk",
					room=res.room, reservation=res.name, guest=res.guest,
					description=note)["ticket"])
		record_step(res, "guest_requests", "done",
		            note=", ".join(presets[k][0] for k in requests) or note or _("Nothing needed"),
		            tickets=tickets, billed=billed)
		out = checkin_flow(reservation)
		out["tickets"] = tickets
		return out

	return _with_snapshot_retry(_log_once)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Night Auditor", "Finance", "Hotel Admin")
def set_room_charges(reservation: str, allowed: int = 1, reason: str | None = None):
	"""May the guest charge outlets, laundry and minibar to the room? POS,
	laundry and minibar postings check this flag. Changeable later in the
	stay by the desk or finance (logged)."""
	res = _res(reservation)
	if res.status not in ("Confirmed", "Checked In"):
		frappe.throw(_("Only a confirmed or in-house stay can change this."))
	allowed = 1 if cint(allowed) else 0
	frappe.db.set_value("Reservation", res.name, "room_charges_allowed", allowed,
	                    update_modified=False)
	res.room_charges_allowed = allowed
	record_step(res, "room_charges", "done",
	            note=(_("Room charges allowed") if allowed else _("Room charges blocked"))
	            + (f": {reason.strip()}" if (reason or "").strip() else ""),
	            allowed=allowed)
	return checkin_flow(reservation)


def early_checkin_fee(res, *, checking_in=False) -> dict:
	"""Quote an early arrival from the property's clock and first locked night."""
	from decimal import Decimal

	from frappe.utils import getdate

	from kamra.folio import _nightly_room_rate
	from kamra.ledger import _currency_places, _round_currency
	from kamra.no_show import _as_time
	from kamra.pricing import property_currency
	from kamra.settings import get_setting

	prop = frappe.db.get_value("Property", res.property, [
		"early_checkin_cutoff", "early_checkin_fee_type", "early_checkin_fee_value",
		"early_checkin_waiver_approval",
	], as_dict=True)
	places = _currency_places(res.property)
	out = {"amount": 0, "currency": property_currency(res.property), "places": places,
	       "waiver_approval_required": bool(prop.early_checkin_waiver_approval)}
	if ((res.status != "Confirmed" and not checking_in)
			or prop.early_checkin_fee_type not in ("Fixed", "Percentage")
			or not flt(prop.early_checkin_fee_value)):
		return out
	clock = property_local_now(res.property)
	start = _as_time(prop.early_checkin_cutoff)
	standard = _as_time(get_setting(res.property, "checkin_time"))
	if (clock.date() != getdate(res.check_in_date) or not standard
			or clock.time() >= standard or (start and clock.time() < start)):
		return out
	value = Decimal(str(prop.early_checkin_fee_value))
	if prop.early_checkin_fee_type == "Percentage":
		value *= Decimal(str(_nightly_room_rate(res, res.check_in_date))) / 100
	out["amount"] = _round_currency(value, places)
	if out["amount"] and out["waiver_approval_required"] and not checking_in:
		from kamra.authz import supervisors_for
		out["supervisors"] = supervisors_for(res.property, exclude=frappe.session.user)
	return out


@frappe.whitelist()
@require_roles(*DESK)
def early_checkin_fee_preview(reservation: str):
	return early_checkin_fee(_res(reservation))


def post_early_checkin_fee(res, amount):
	from kamra.business_date import get_business_date
	from kamra.folio import _append_charge, _room_gst, save_folios

	key = f"early-checkin:{res.name}"
	if frappe.db.exists("Folio Charge", {"reservation": res.name, "idempotency_key": key}):
		return
	folios = {}
	_append_charge(folios, res, "Misc", {
		"reservation": res.name, "posting_date": get_business_date(res.property),
		"idempotency_key": key,
		"description": "Early Check-In Fee", "charge_type": "Misc", "qty": 1, "rate": amount, "amount": amount,
		"gst_rate": _room_gst(res), "auto_posted": 1,
	})
	save_folios(folios)


def _committed_key_history(res, room=None, revoked_after=None, operation_id=None, issued_after=None):
	"""Read the encoder journal at its latest commit without locking future entries."""
	conn = frappe.db.get_connection()
	try:
		with conn.cursor() as cursor:
			conditions = "property=%s AND reservation=%s AND creation >= %s"
			params = [res.property, res.name, res.creation]
			if operation_id:
				conditions += " AND name=%s"
				params.append(operation_id)
			if room:
				conditions += " AND room=%s"
				params.append(room)
			if issued_after:
				conditions += """ AND adapter_key=%s AND issuer_target <=> %s AND creation>%s
					AND status IN ('Accepted','Pending') AND operation IN ('issue','duplicate','rekey','mobile')"""
				params.extend([issued_after.adapter_key, issued_after.issuer_target, issued_after.creation])
			if revoked_after:
				conditions += " AND adapter_key=%s AND issuer_target <=> %s AND creation>%s AND status='Accepted' AND operation IN (%s,%s,%s,%s)"
				params.extend([revoked_after.adapter_key, revoked_after.issuer_target, revoked_after.creation, "revoke", "room_move_revoke",
				               "checkout_revoke", "reverse_checkin_revoke"])
			cursor.execute("""SELECT name, creation, room, operation, status, adapter_key, issuer_target,
				encoder_id, valid_from, valid_to, actor FROM `tabDoor Key Audit` WHERE """
				+ conditions + " ORDER BY creation DESC LIMIT 50", params)
			fields = [column[0] for column in cursor.description]
			return [frappe._dict(zip(fields, values)) for values in cursor.fetchall()]
	finally:
		conn.close()


@frappe.whitelist()
@require_roles(*STAFF)
def door_key_audit(reservation: str):
	res = _res(reservation)
	rows = _committed_key_history(res)
	superseded = set()
	for row in rows:
		scope = (row.room, row.adapter_key, row.issuer_target)
		row.retryable = row.operation.endswith("revoke") and row.status in ("Failed", "Queued") and scope not in superseded
		if row.operation.endswith("revoke") and row.status == "Accepted" and (row.adapter_key != "fias_edge" or row.issuer_target):
			superseded.add(scope)
		if row.operation in ("issue", "duplicate", "rekey", "mobile") and row.status in ("Accepted", "Pending"):
			superseded.add(scope)
	return rows
