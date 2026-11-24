"""OPERA-style Restrictions Matrix, Editable BAR Grid & Channel Parity Health.

Supports five first-class stay/date restrictions across four specificity levels:
  1. (property, rate_plan, room_type, date)  — highest specificity
  2. (property, rate_plan, NULL, date)
  3. (property, NULL, room_type, date)
  4. (property, NULL, NULL, date)            — house-level default

Restrictions enforced:
  - stop_sell: Close date to new sales across stay nights
  - cta: Closed to Arrival on check_in_date (stay-overs permitted)
  - ctd: Closed to Departure on check_out_date
  - min_los: Minimum Length of Stay (arrival-date semantics, plus optional
    is_stay_through enforcing the floor on any stay covering that night)
  - max_los: Maximum Length of Stay (arrival-date semantics; 0 = unlimited)

All currency values follow exact ISO 4217 precision via channel_ari.rate_quantum
(0dp UGX, 2dp USD, 3dp KWD). All endpoints enforce require_roles and
assert_property_access for multi-property isolation.
"""

from __future__ import annotations

import json
import math
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe import _
from frappe.utils import add_days, cint, date_diff, getdate, now_datetime, nowdate

from kamra.authz import assert_property_access, require_roles

RESTRICTION_VIEW_ROLES = ("Revenue Manager", "Hotel Admin", "Front Desk", "Night Auditor")
RESTRICTION_EDIT_ROLES = ("Revenue Manager", "Hotel Admin")
MAX_BULK_DAYS = 730
_WEEKDAY_MAP = {
	"0": "mon", "mon": "mon", "monday": "mon",
	"1": "tue", "tue": "tue", "tuesday": "tue",
	"2": "wed", "wed": "wed", "wednesday": "wed",
	"3": "thu", "thu": "thu", "thursday": "thu",
	"4": "fri", "fri": "fri", "friday": "fri",
	"5": "sat", "sat": "sat", "saturday": "sat",
	"6": "sun", "sun": "sun", "sunday": "sun",
}
_EXEMPT_STATUSES = frozenset({"Cancelled", "No Show", "Checked Out", "Inquiry", "Quoted", "Waitlist"})
_PRE_BOOK_STATUSES = frozenset({"Waitlist", "Inquiry", "Quoted"})
_RESTRICTION_FIELDS = (
	"name", "property", "restriction_date", "room_type", "rate_plan",
	"stop_sell", "min_los", "max_los", "is_stay_through", "cta", "ctd",
	"reason", "updated_by_user",
)


class StayRestrictionError(frappe.ValidationError):
	"""Raised when a stay violates an active Rate Restriction."""


def _table_available() -> bool:
	exists = getattr(frappe.db, "exists", None)
	return bool(callable(exists) and exists("DocType", "Rate Restriction"))


def _can_manage_restrictions() -> bool:
	return bool({"Revenue Manager", "Hotel Admin", "System Manager", "Administrator"} & set(frappe.get_roles()))


def _parse_json_arg(value, expected_type, label: str):
	if value is None:
		return None
	if isinstance(value, str):
		text = value.strip()
		if not text:
			return None
		try:
			value = json.loads(text)
		except ValueError:
			frappe.throw(_("Invalid {0} payload.").format(label))
	if not isinstance(value, expected_type):
		frappe.throw(_("Invalid {0} payload.").format(label))
	return value


def _parse_weekdays(days_of_week) -> set[str] | None:
	if days_of_week is None or days_of_week == "":
		return None
	raw = days_of_week
	if isinstance(raw, str):
		stripped = raw.strip()
		if not stripped:
			return None
		if stripped.startswith("["):
			try:
				raw = json.loads(stripped)
			except ValueError:
				frappe.throw(_("Invalid days_of_week value."))
		else:
			raw = [part.strip() for part in stripped.split(",") if part.strip()]
	if not isinstance(raw, (list, tuple, set)):
		frappe.throw(_("Invalid days_of_week value."))
	out = set()
	for item in raw:
		key = _WEEKDAY_MAP.get(str(item).strip().lower())
		if not key:
			frappe.throw(_("Unrecognized weekday: {0}.").format(item))
		out.add(key)
	return out or None


def _date_range(start_date, end_date, weekdays: set[str] | None = None) -> list[str]:
	start = getdate(start_date)
	end = getdate(end_date)
	if end < start:
		frappe.throw(_("End date cannot be before start date."))
	span = date_diff(end, start) + 1
	if span > MAX_BULK_DAYS:
		frappe.throw(_("Date range cannot exceed {0} days.").format(MAX_BULK_DAYS))
	dates = []
	for i in range(span):
		d = getdate(add_days(start, i))
		if weekdays and d.strftime("%a").lower() not in weekdays:
			continue
		dates.append(str(d))
	return dates


def _scope_rank(row: dict) -> tuple[int, str]:
	has_rp = bool(row.get("rate_plan"))
	has_rt = bool(row.get("room_type"))
	if has_rp and has_rt:
		return 4, "rate_plan_room_type"
	if has_rp:
		return 3, "rate_plan"
	if has_rt:
		return 2, "room_type"
	return 1, "property"


def _row_matches(row: dict, room_type: str | None, rate_plan: str | None) -> bool:
	r_rt = row.get("room_type") or None
	r_rp = row.get("rate_plan") or None
	if r_rt and r_rt != (room_type or None):
		return False
	if r_rp and r_rp != (rate_plan or None):
		return False
	return True


def _resolve_from_rows(
	rows: list[dict],
	room_type: str | None = None,
	rate_plan: str | None = None,
) -> dict:
	matched = [r for r in rows if _row_matches(r, room_type, rate_plan)]
	if not matched:
		return {
			"has_rule": False,
			"is_restricted": False,
			"stop_sell": False,
			"min_los": 0,
			"max_los": 0,
			"is_stay_through": False,
			"cta": False,
			"ctd": False,
			"source_scope": None,
			"reason": "",
		}

	# Order from highest specificity (4) down to house-level default (1)
	ranked = sorted(matched, key=lambda r: _scope_rank(r)[0], reverse=True)
	top_scope = _scope_rank(ranked[0])[1]

	# min_los / max_los: most-specific non-zero rule wins (0 = unset/inherit)
	min_los = 0
	is_stay_through = False
	for r in ranked:
		val = cint(r.get("min_los"))
		if val > 0:
			min_los = val
			is_stay_through = bool(cint(r.get("is_stay_through")))
			break
	if not min_los:
		is_stay_through = any(bool(cint(r.get("is_stay_through"))) for r in ranked)

	max_los = 0
	for r in ranked:
		val = cint(r.get("max_los"))
		if val > 0:
			max_los = val
			break

	# Boolean closeouts / arrival / departure locks inherit from broader scopes
	# so adding a MinLOS row at Room Type level never accidentally lifts a House Stop-Sell.
	stop_sell = any(bool(cint(r.get("stop_sell"))) for r in ranked)
	cta = any(bool(cint(r.get("cta"))) for r in ranked)
	ctd = any(bool(cint(r.get("ctd"))) for r in ranked)
	reason = next((str(r.get("reason") or "").strip() for r in ranked if (r.get("reason") or "").strip()), "")
	is_restricted = bool(stop_sell or cta or ctd or min_los > 0 or max_los > 0)

	return {
		"has_rule": True,
		"is_restricted": is_restricted,
		"stop_sell": stop_sell,
		"min_los": min_los,
		"max_los": max_los,
		"is_stay_through": is_stay_through,
		"cta": cta,
		"ctd": ctd,
		"source_scope": top_scope,
		"reason": reason,
	}


def _fetch_rows_for_window(property: str, start_date, end_date) -> dict[str, list[dict]]:
	"""Fetch all Rate Restriction rows for `property` across [start_date, end_date] in one query."""
	if not property or not _table_available():
		return {}
	rows = frappe.get_all(
		"Rate Restriction",
		filters={
			"property": property,
			"restriction_date": ("between", [str(getdate(start_date)), str(getdate(end_date))]),
		},
		fields=list(_RESTRICTION_FIELDS),
	)
	by_date: dict[str, list[dict]] = {}
	for r in rows:
		by_date.setdefault(str(getdate(r.get("restriction_date"))), []).append(r)
	return by_date


def resolve_night_restrictions(
	property: str,
	restriction_date,
	room_type: str | None = None,
	rate_plan: str | None = None,
	*,
	_rows_by_date: dict[str, list[dict]] | None = None,
) -> dict:
	"""Resolve effective restriction rules for one night at `(property, room_type, rate_plan)`."""
	d_str = str(getdate(restriction_date))
	if _rows_by_date is not None:
		rows = _rows_by_date.get(d_str) or []
	else:
		rows = _fetch_rows_for_window(property, d_str, d_str).get(d_str) or []
	return _resolve_from_rows(rows, room_type=room_type, rate_plan=rate_plan)


def evaluate_stay_restrictions(
	property: str,
	room_type: str | None,
	check_in_date,
	check_out_date,
	rate_plan: str | None = None,
	ignore_day_use: bool = True,
) -> dict:
	"""Evaluate all active Rate Restrictions for a stay `[check_in_date, check_out_date)`."""
	if not property or not check_in_date or not check_out_date or not _table_available():
		return {"allowed": True, "violations": [], "summary": ""}

	ci = getdate(check_in_date)
	co = getdate(check_out_date)
	nights = date_diff(co, ci)
	if nights <= 0:
		if ignore_day_use:
			return {"allowed": True, "violations": [], "summary": ""}
		nights = 1

	rows_by_date = _fetch_rows_for_window(property, ci, co)
	if not rows_by_date:
		return {"allowed": True, "violations": [], "summary": ""}

	violations: list[dict] = []
	stay_dates = [str(getdate(add_days(ci, i))) for i in range(nights)]
	ci_str = str(ci)
	co_str = str(co)

	resolved_by_day = {
		d: resolve_night_restrictions(
			property, d, room_type=room_type, rate_plan=rate_plan, _rows_by_date=rows_by_date,
		)
		for d in [*stay_dates, co_str]
	}

	# 1. Stop-Sell across every stay night [ci .. co)
	for d in stay_dates:
		r = resolved_by_day[d]
		if r["stop_sell"]:
			violations.append({
				"code": "stop_sell",
				"date": d,
				"scope": r["source_scope"],
				"message": _("Closed to sale (Stop-Sell) on {0}.").format(d),
			})

	# 2. CTA on arrival date (stay-overs passing through a CTA date are allowed)
	r_ci = resolved_by_day[ci_str]
	if r_ci["cta"]:
		violations.append({
			"code": "cta",
			"date": ci_str,
			"scope": r_ci["source_scope"],
			"message": _("Closed to arrival on {0}.").format(ci_str),
		})

	# 3. CTD on departure date
	r_co = resolved_by_day[co_str]
	if r_co["ctd"]:
		violations.append({
			"code": "ctd",
			"date": co_str,
			"scope": r_co["source_scope"],
			"message": _("Closed to departure on {0}.").format(co_str),
		})

	# 4. MinLOS on arrival date
	if r_ci["min_los"] > 0 and nights < r_ci["min_los"]:
		violations.append({
			"code": "min_los",
			"date": ci_str,
			"required": r_ci["min_los"],
			"actual": nights,
			"scope": r_ci["source_scope"],
			"message": _("Minimum {0} nights stay required on {1}.").format(r_ci["min_los"], ci_str),
		})

	# 5. Stay-Through MinLOS on subsequent stay nights
	for d in stay_dates[1:]:
		r_d = resolved_by_day[d]
		if r_d["is_stay_through"] and r_d["min_los"] > 0 and nights < r_d["min_los"]:
			violations.append({
				"code": "min_los_through",
				"date": d,
				"required": r_d["min_los"],
				"actual": nights,
				"scope": r_d["source_scope"],
				"message": _("Minimum {0} nights stay-through required covering {1}.").format(
					r_d["min_los"], d),
			})

	# 6. MaxLOS on arrival date
	if r_ci["max_los"] > 0 and nights > r_ci["max_los"]:
		violations.append({
			"code": "max_los",
			"date": ci_str,
			"maximum": r_ci["max_los"],
			"actual": nights,
			"scope": r_ci["source_scope"],
			"message": _("Maximum {0} nights stay allowed arriving on {1}.").format(
				r_ci["max_los"], ci_str),
		})

	return {
		"allowed": not violations,
		"violations": violations,
		"summary": "; ".join(v["message"] for v in violations),
	}


def assert_stay_allowed(res) -> None:
	"""Enforce active Rate Restrictions on a Reservation document."""
	if res.status in _EXEMPT_STATUSES:
		return
	if res.flags.get("kamra_ota_restriction_override"):
		return
	if not res.property or not res.check_in_date or not res.check_out_date:
		return
	if date_diff(res.check_out_date, res.check_in_date) <= 0:
		return

	# Allow non-stay edits (e.g. room swap, guest note, check-in/out) on existing stays
	# whose dates, room_type, and rate_plan have not changed.
	if not res.is_new():
		old = res.get_doc_before_save()
		if old and old.status not in _PRE_BOOK_STATUSES:
			unchanged = (
				str(getdate(old.check_in_date)),
				str(getdate(old.check_out_date)),
				old.room_type or "",
				old.get("rate_plan") or "",
			) == (
				str(getdate(res.check_in_date)),
				str(getdate(res.check_out_date)),
				res.room_type or "",
				res.get("rate_plan") or "",
			)
			if unchanged:
				return

	rp = res.get("rate_plan") or None
	if not rp and res.get("company"):
		from kamra.pricing import negotiated_rate_plan
		rp = negotiated_rate_plan(res.property, res.get("company"))

	ev = evaluate_stay_restrictions(
		res.property, res.room_type, res.check_in_date, res.check_out_date, rate_plan=rp,
	)
	if not ev["allowed"]:
		frappe.throw(ev["summary"], StayRestrictionError, title=_("Stay restriction"))


def calendar_restrictions_map(
	property: str,
	from_date,
	days: int,
	room_types: list[str],
	rate_plan: str | None = None,
) -> dict[tuple[str, str], dict]:
	"""Internal fast lookup {(room_type, 'YYYY-MM-DD'): resolved_restriction} for rate_calendar."""
	if not property or not room_types or not _table_available():
		return {}
	start = getdate(from_date)
	end = getdate(add_days(start, max(1, int(days)) - 1))
	rows_by_date = _fetch_rows_for_window(property, start, end)
	if not rows_by_date:
		return {}
	out: dict[tuple[str, str], dict] = {}
	for i in range(max(1, int(days))):
		d_str = str(getdate(add_days(start, i)))
		if d_str not in rows_by_date:
			continue
		for rt in room_types:
			resolved = resolve_night_restrictions(
				property, d_str, room_type=rt, rate_plan=rate_plan, _rows_by_date=rows_by_date,
			)
			if resolved["is_restricted"]:
				out[(rt, d_str)] = resolved
	return out


@frappe.whitelist()
@require_roles(*RESTRICTION_VIEW_ROLES)
def calendar_restrictions(
	property: str,
	from_date: str | None = None,
	days: int = 14,
	room_type: str | None = None,
	rate_plan: str | None = None,
) -> dict:
	"""Structured restriction matrix for `(property, from_date..+days)`."""
	assert_property_access(property)
	start = getdate(from_date) if from_date else getdate(nowdate())
	days = max(1, min(cint(days) or 14, 90))
	end = getdate(add_days(start, days - 1))

	if room_type:
		rt_prop = frappe.db.get_value("Room Type", room_type, "property")
		if rt_prop != property:
			frappe.throw(_("That room type does not belong to this property."))
		room_types = [room_type]
	else:
		room_types = frappe.get_all(
			"Room Type", filters={"property": property, "disabled": 0},
			pluck="name", order_by="base_price asc",
		)

	rows_by_date = _fetch_rows_for_window(property, start, end)
	dates = [str(getdate(add_days(start, i))) for i in range(days)]
	cells: dict[str, dict[str, dict]] = {}
	for rt in room_types:
		cells[rt] = {
			d: resolve_night_restrictions(
				property, d, room_type=rt, rate_plan=rate_plan or None, _rows_by_date=rows_by_date,
			)
			for d in dates
		}

	house = {
		d: resolve_night_restrictions(
			property, d, room_type=None, rate_plan=rate_plan or None, _rows_by_date=rows_by_date,
		)
		for d in dates
	}

	return {
		"property": property,
		"from": str(start),
		"days": days,
		"dates": dates,
		"rate_plan": rate_plan or None,
		"can_edit": _can_manage_restrictions(),
		"house": house,
		"cells": cells,
	}


def _upsert_restriction_day(
	property: str,
	d_str: str,
	room_type: str | None,
	rate_plan: str | None,
	*,
	stop_sell=None,
	min_los=None,
	max_los=None,
	is_stay_through=None,
	cta=None,
	ctd=None,
	reason: str = "",
) -> str:
	"""Insert, update, or delete (when all cleared) one Rate Restriction row.
	Returns 'updated' or 'deleted'."""
	from kamra.kamra.doctype.rate_restriction.rate_restriction import make_scope_key

	key = make_scope_key(property, d_str, room_type or None, rate_plan or None)
	existing_name = frappe.db.get_value("Rate Restriction", {"scope_key": key}, "name")
	if existing_name:
		doc = frappe.get_doc("Rate Restriction", existing_name)
	else:
		doc = frappe.get_doc({
			"doctype": "Rate Restriction",
			"property": property,
			"restriction_date": d_str,
			"room_type": room_type or None,
			"rate_plan": rate_plan or None,
			"stop_sell": 0,
			"min_los": 0,
			"max_los": 0,
			"is_stay_through": 0,
			"cta": 0,
			"ctd": 0,
		})

	if stop_sell is not None:
		doc.stop_sell = cint(stop_sell)
	if min_los is not None:
		doc.min_los = cint(min_los)
	if max_los is not None:
		doc.max_los = cint(max_los)
	if is_stay_through is not None:
		doc.is_stay_through = cint(is_stay_through)
	if cta is not None:
		doc.cta = cint(cta)
	if ctd is not None:
		doc.ctd = cint(ctd)
	if reason is not None:
		doc.reason = str(reason or "").strip()[:500]

	# If every restriction is now off/0, remove redundant row cleanly
	all_clear = not (
		cint(doc.stop_sell)
		or cint(doc.min_los)
		or cint(doc.max_los)
		or cint(doc.is_stay_through)
		or cint(doc.cta)
		or cint(doc.ctd)
	)
	if all_clear:
		if existing_name:
			doc.flags.skip_ari_enqueue = True
			frappe.delete_doc("Rate Restriction", existing_name, ignore_permissions=True)
			return "deleted"
		return "noop"

	doc.flags.skip_ari_enqueue = True
	if existing_name:
		doc.save(ignore_permissions=True)
	else:
		doc.insert(ignore_permissions=True)
	return "updated"


def _upsert_bar_override(property: str, room_type: str, d_str: str, raw_rate, reason: str = "") -> Decimal:
	"""Write a date-specific BAR override at exact currency precision (`rate_quantum`).

	Uses a dedicated high-priority Absolute `Season` row for `(property, room_type, d_str)`
	so every downstream consumer (`pricing.quote`, `rates.explain_night`, `public_api`,
	`channel_ari`) immediately prices at the exact updated BAR without altering pricing.py.
	"""
	from kamra.channel_ari import rate_quantum

	if isinstance(raw_rate, bool):
		frappe.throw(_("BAR rate must be a valid number."))
	try:
		numeric = float(str(raw_rate).strip())
	except (TypeError, ValueError):
		frappe.throw(_("BAR rate must be a valid number."))
	if not math.isfinite(numeric) or numeric <= 0:
		frappe.throw(_("BAR rate must be greater than zero."))

	rt = frappe.db.get_value(
		"Room Type", room_type, ["property", "room_type_code", "room_type_name"], as_dict=True,
	)
	if not rt or rt.property != property:
		frappe.throw(_("Room type {0} does not belong to property {1}.").format(room_type, property))

	quantum = rate_quantum(property)
	quantized = Decimal(str(raw_rate).strip()).quantize(quantum, rounding=ROUND_HALF_UP)
	if quantized <= 0:
		frappe.throw(_("BAR rate must be greater than zero."))

	label = f"BAR Override {rt.room_type_code or room_type} {d_str}"
	existing_name = frappe.db.get_value(
		"Season",
		{
			"property": property,
			"room_type": room_type,
			"start_date": d_str,
			"end_date": d_str,
			"adjustment_type": "Absolute",
			"season_name": label,
		},
		"name",
	)

	other_seasons = frappe.get_all(
		"Season",
		filters={
			"property": property,
			"disabled": 0,
			"start_date": ("<=", d_str),
			"end_date": (">=", d_str),
			"room_type": ("in", [room_type, "", None]),
		},
		fields=["name", "priority"],
	)
	max_other = max(
		(cint(s.priority) for s in other_seasons if s.name != existing_name),
		default=0,
	)
	priority = max(100, max_other + 10)

	if existing_name:
		doc = frappe.get_doc("Season", existing_name)
		doc.adjustment_value = float(quantized)
		doc.priority = priority
		doc.disabled = 0
		doc.days_of_week = ""
		doc.save(ignore_permissions=True)
	else:
		frappe.get_doc({
			"doctype": "Season",
			"property": property,
			"season_name": label,
			"room_type": room_type,
			"start_date": d_str,
			"end_date": d_str,
			"adjustment_type": "Absolute",
			"adjustment_value": float(quantized),
			"priority": priority,
			"disabled": 0,
		}).insert(ignore_permissions=True)

	return quantized


@frappe.whitelist(methods=["POST"])
@require_roles(*RESTRICTION_EDIT_ROLES)
def bulk_set_restrictions(
	property: str,
	start_date: str,
	end_date: str,
	room_type: str | None = None,
	rate_plan: str | None = None,
	days_of_week=None,
	stop_sell=None,
	min_los=None,
	max_los=None,
	is_stay_through=None,
	cta=None,
	ctd=None,
	reason: str = "",
) -> dict:
	"""Atomically set/clear Rate Restrictions over a date range and enqueue ARI push."""
	assert_property_access(property)
	weekdays = _parse_weekdays(days_of_week)
	dates = _date_range(start_date, end_date, weekdays)

	if room_type:
		rt_prop = frappe.db.get_value("Room Type", room_type, "property")
		if rt_prop != property:
			frappe.throw(_("Room type {0} does not belong to property {1}.").format(room_type, property))
	if rate_plan:
		rp_prop = frappe.db.get_value("Rate Plan", rate_plan, "property")
		if rp_prop != property:
			frappe.throw(_("Rate plan {0} does not belong to property {1}.").format(rate_plan, property))

	updated = 0
	deleted = 0
	for d_str in dates:
		outcome = _upsert_restriction_day(
			property, d_str, room_type or None, rate_plan or None,
			stop_sell=stop_sell, min_los=min_los, max_los=max_los,
			is_stay_through=is_stay_through, cta=cta, ctd=ctd, reason=reason,
		)
		if outcome == "updated":
			updated += 1
		elif outcome == "deleted":
			deleted += 1

	ari_queued: list[str] = []
	if dates:
		from kamra import channel_ari, channel_manager
		ari_queued = channel_ari.enqueue_ari_change(
			property, dates[0], dates[-1], kinds="restrictions", reason="restrictions.bulk_set",
		)
		channel_manager.enqueue_property_push(property, outbox=False)

	from kamra.savings import log_action
	log_action(
		"restrictions.bulk_set",
		"Property",
		property,
		property,
		rationale=(
			f"Restrictions {start_date}..{end_date}"
			f"{f' ({room_type})' if room_type else ''}"
			f"{f' [{rate_plan}]' if rate_plan else ''}: "
			f"{updated} updated, {deleted} cleared"
			f"{f' — {reason}' if reason else ''}"
		)[:300],
		after_snapshot={
			"start_date": str(getdate(start_date)),
			"end_date": str(getdate(end_date)),
			"room_type": room_type or None,
			"rate_plan": rate_plan or None,
			"stop_sell": stop_sell,
			"min_los": min_los,
			"max_los": max_los,
			"is_stay_through": is_stay_through,
			"cta": cta,
			"ctd": ctd,
			"updated": updated,
			"deleted": deleted,
		},
		channel="Desk",
	)

	return {
		"ok": True,
		"property": property,
		"updated": updated,
		"deleted": deleted,
		"dates": dates,
		"ari_queued": ari_queued,
	}


def _expand_entry_dates(entry: dict) -> list[str]:
	if entry.get("date"):
		return _date_range(entry["date"], entry["date"], _parse_weekdays(entry.get("days_of_week")))
	if entry.get("start_date") and entry.get("end_date"):
		return _date_range(entry["start_date"], entry["end_date"], _parse_weekdays(entry.get("days_of_week")))
	frappe.throw(_("Each update row requires a date or start_date and end_date."))


@frappe.whitelist(methods=["POST"])
@require_roles(*RESTRICTION_EDIT_ROLES)
def bulk_grid_update(
	property: str,
	rate_updates=None,
	restriction_updates=None,
	reason: str = "",
) -> dict:
	"""Single atomic transaction for Editable BAR + Restrictions Grid updates."""
	assert_property_access(property)
	rates_list = _parse_json_arg(rate_updates, list, "rate_updates") or []
	restr_list = _parse_json_arg(restriction_updates, list, "restriction_updates") or []
	if not rates_list and not restr_list:
		frappe.throw(_("Provide at least one rate or restriction update."))

	touched_dates: list[str] = []
	rates_updated = 0
	restrictions_updated = 0
	restrictions_deleted = 0

	for item in rates_list:
		if not isinstance(item, dict):
			frappe.throw(_("Each rate update must be an object."))
		rt = item.get("room_type")
		if not rt:
			frappe.throw(_("Room type is required for BAR rate updates."))
		if "rate" not in item or item.get("rate") is None or str(item.get("rate")).strip() == "":
			frappe.throw(_("Rate is required for BAR rate updates."))
		dates = _expand_entry_dates(item)
		for d_str in dates:
			_upsert_bar_override(property, rt, d_str, item["rate"], reason=reason)
			rates_updated += 1
			touched_dates.append(d_str)

	for item in restr_list:
		if not isinstance(item, dict):
			frappe.throw(_("Each restriction update must be an object."))
		rt = item.get("room_type") or None
		rp = item.get("rate_plan") or None
		if rt and frappe.db.get_value("Room Type", rt, "property") != property:
			frappe.throw(_("Room type {0} does not belong to property {1}.").format(rt, property))
		if rp and frappe.db.get_value("Rate Plan", rp, "property") != property:
			frappe.throw(_("Rate plan {0} does not belong to property {1}.").format(rp, property))
		dates = _expand_entry_dates(item)
		for d_str in dates:
			outcome = _upsert_restriction_day(
				property,
				d_str,
				rt,
				rp,
				stop_sell=item.get("stop_sell") if "stop_sell" in item else None,
				min_los=item.get("min_los") if "min_los" in item else None,
				max_los=item.get("max_los") if "max_los" in item else None,
				is_stay_through=item.get("is_stay_through") if "is_stay_through" in item else None,
				cta=item.get("cta") if "cta" in item else None,
				ctd=item.get("ctd") if "ctd" in item else None,
				reason=item.get("reason", reason),
			)
			if outcome == "updated":
				restrictions_updated += 1
			elif outcome == "deleted":
				restrictions_deleted += 1
			touched_dates.append(d_str)

	ari_queued: list[str] = []
	if touched_dates:
		from kamra import channel_ari, channel_manager
		min_d, max_d = min(touched_dates), max(touched_dates)
		ari_queued = channel_ari.enqueue_ari_change(
			property, min_d, max_d, kinds="restrictions", reason="restrictions.bulk_grid_update",
		)
		channel_manager.enqueue_property_push(property, outbox=False)

	from kamra.savings import log_action
	log_action(
		"restrictions.bulk_grid_update",
		"Property",
		property,
		property,
		rationale=(
			f"Grid update: {rates_updated} BAR rates, "
			f"{restrictions_updated} restrictions updated, {restrictions_deleted} cleared"
			f"{f' — {reason}' if reason else ''}"
		)[:300],
		after_snapshot={
			"rates_updated": rates_updated,
			"restrictions_updated": restrictions_updated,
			"restrictions_deleted": restrictions_deleted,
			"date_min": min(touched_dates) if touched_dates else None,
			"date_max": max(touched_dates) if touched_dates else None,
		},
		channel="Desk",
	)

	return {
		"ok": True,
		"property": property,
		"rates_updated": rates_updated,
		"restrictions_updated": restrictions_updated,
		"restrictions_deleted": restrictions_deleted,
		"ari_queued": ari_queued,
	}


# ---------------------------------------------------------------------------
# P43: Channel Mapping & Rate Parity Health Control Tower
# ---------------------------------------------------------------------------


def _connection_health_status(conn, outbox_counts: dict, paused: dict) -> str:
	if not cint(conn.active):
		return "Inactive"
	if outbox_counts["failed"] > 0 or str(conn.last_push_status or "").upper().startswith("FAILED"):
		return "Error"
	if paused:
		return "Paused"
	if not conn.last_push:
		return "Stale"
	age_hours = (now_datetime() - frappe.utils.get_datetime(conn.last_push)).total_seconds() / 3600.0
	if age_hours > 24:
		return "Stale"
	return "Online"


@frappe.whitelist()
@require_roles("Revenue Manager", "Hotel Admin", "Front Desk")
def channel_health_snapshot(property: str, days: int = 14) -> dict:
	"""Property-scoped Channel Mapping, Outbox & Rate Parity Health snapshot."""
	assert_property_access(property)
	from kamra import channel_ari
	from kamra.pricing import property_currency
	from kamra.rates import _room_counts

	days = max(1, min(cint(days) or 14, 30))
	home = property_currency(property)
	start = getdate(nowdate())
	end = getdate(add_days(start, days - 1))
	dates = [str(getdate(add_days(start, i))) for i in range(days)]

	conns = frappe.get_all(
		"Channel Manager Connection",
		filters={"property": property},
		fields=[
			"name", "provider", "active", "external_property_id", "sync_days",
			"ari_calls_per_minute", "ari_batch_seconds", "last_push",
			"last_push_status", "last_full_sync",
		],
		order_by="active desc, modified desc",
	)
	conn_names = [c.name for c in conns]
	conn_by_name = {c.name: c for c in conns}

	mappings = (
		frappe.get_all(
			"Channel Room Mapping",
			filters={"connection": ("in", conn_names)},
			fields=[
				"name", "connection", "room_type", "external_room_id", "external_rate_id",
				"external_currency", "rate_plan", "last_synced_at", "last_parity_status",
				"last_parity_delta", "last_error",
			],
			order_by="connection asc, room_type asc, rate_plan asc",
		)
		if conn_names else []
	)

	outbox_rows = (
		frappe.get_all(
			"Channel ARI Outbox",
			filters={"connection": ("in", conn_names)},
			fields=[
				"name", "connection", "date_from", "date_to", "kinds", "full",
				"status", "attempts", "queued_at", "next_attempt_at", "sent_at",
				"reason", "detail",
			],
			order_by="creation desc",
			limit=50,
		)
		if conn_names else []
	)

	rt_names_map = {
		r.name: r.room_type_name or r.name
		for r in frappe.get_all(
			"Room Type", filters={"property": property}, fields=["name", "room_type_name"],
		)
	}
	rp_names_map = {
		r.name: r.rate_plan_name or r.name
		for r in frappe.get_all(
			"Rate Plan", filters={"property": property}, fields=["name", "rate_plan_name"],
		)
	}

	# Count outbox rows per connection
	counts_by_conn: dict[str, dict[str, int]] = {
		c.name: {"pending": 0, "sending": 0, "sent": 0, "failed": 0} for c in conns
	}
	for c in conns:
		for st_key, st_label in (("pending", "Pending"), ("sending", "Sending"),
		                         ("sent", "Sent"), ("failed", "Failed")):
			counts_by_conn[c.name][st_key] = frappe.db.count(
				"Channel ARI Outbox", {"connection": c.name, "status": st_label},
			)

	now_ts = channel_ari._now()
	connection_cards = []
	for c in conns:
		paused = {}
		for kind in channel_ari.KINDS:
			until = frappe.cache.get_value(channel_ari._ARI_PAUSE.format(connection=c.name, kind=kind))
			if until and float(until) > now_ts:
				paused[kind] = math.ceil(float(until) - now_ts)
		m_count = sum(1 for m in mappings if m.connection == c.name)
		oc = counts_by_conn[c.name]
		connection_cards.append({
			"name": c.name,
			"provider": c.provider,
			"active": cint(c.active),
			"external_property_id": c.external_property_id or "",
			"uses_outbox": channel_ari._supports_outbox(c.provider),
			"sync_days": cint(c.sync_days or 90),
			"ari_calls_per_minute": cint(c.ari_calls_per_minute or 10),
			"ari_batch_seconds": cint(c.ari_batch_seconds or 0),
			"last_push": str(c.last_push or ""),
			"last_push_status": c.last_push_status or "",
			"last_full_sync": str(c.last_full_sync or ""),
			"health_status": _connection_health_status(c, oc, paused),
			"paused": paused,
			"outbox_counts": oc,
			"mappings_count": m_count,
		})

	# Load Channel ARI State for all connections once
	sent_by_conn: dict[str, dict] = {
		c.name: channel_ari._load_sent(c.name, "restrictions") for c in conns
	}
	rows_by_date = _fetch_rows_for_window(property, start, end)

	parity_rows = []
	parity_ok = 0
	parity_issues = 0
	token = channel_ari._pass_directory.set({})
	try:
		for m in mappings:
			conn = conn_by_name[m.connection]
			currency = m.external_currency or home
			quantum = channel_ari.rate_quantum(property, currency)
			ext_rate_id = str(m.external_rate_id or "")
			sent_days = (sent_by_conn.get(m.connection) or {}).get(ext_rate_id) or {}

			day_rows = []
			synced_stop_sell = False
			synced_min_los = None
			synced_max_los = None
			synced_cta = False
			synced_ctd = False
			synced_active_days = 0
			last_err = m.last_error or ""

			for d_str in dates:
				direct_dec: Decimal | None = None
				if ext_rate_id:
					try:
						direct_dec = channel_ari._plan_night_rate(
							property, m.room_type, m.rate_plan or None, getdate(d_str), currency,
						)
					except (channel_ari.AriCurrencyRefused, channel_ari.AriPriceHeld, channel_ari.AriPricingFailed) as exc:
						last_err = str(exc)[:280]
						direct_dec = None

				ota_cell = sent_days.get(d_str) or {}
				ota_dec: Decimal | None = None
				if ota_cell.get("rate") is not None:
					try:
						ota_dec = Decimal(str(ota_cell["rate"])).quantize(quantum, rounding=ROUND_HALF_UP)
					except Exception:
						ota_dec = None

				if not ext_rate_id:
					d_status = "Unmapped"
					delta_dec = None
				elif ota_dec is None:
					d_status = "OK" if (direct_dec is None and ota_cell.get("stop_sell")) else "Stale"
					delta_dec = None
				elif direct_dec is None:
					d_status = "Stale"
					delta_dec = None
				else:
					delta_dec = (ota_dec - direct_dec).quantize(quantum, rounding=ROUND_HALF_UP)
					if delta_dec == 0:
						d_status = "OK"
					elif delta_dec < 0:
						d_status = "Undercut"
					else:
						d_status = "Overpriced"

				live_r = resolve_night_restrictions(
					property, d_str, room_type=m.room_type, rate_plan=m.rate_plan or None,
					_rows_by_date=rows_by_date,
				)
				if (
					ota_cell.get("stop_sell")
					or cint(ota_cell.get("min_stay_arrival")) > 1
					or cint(ota_cell.get("max_stay")) > 0
					or ota_cell.get("closed_to_arrival")
					or ota_cell.get("closed_to_departure")
				):
					synced_active_days += 1
				if ota_cell.get("stop_sell"):
					synced_stop_sell = True
				if cint(ota_cell.get("min_stay_arrival")) > 1 and synced_min_los is None:
					synced_min_los = cint(ota_cell["min_stay_arrival"])
				if cint(ota_cell.get("max_stay")) > 0 and synced_max_los is None:
					synced_max_los = cint(ota_cell["max_stay"])
				if ota_cell.get("closed_to_arrival"):
					synced_cta = True
				if ota_cell.get("closed_to_departure"):
					synced_ctd = True

				day_rows.append({
					"date": d_str,
					"direct_rate": float(direct_dec) if direct_dec is not None else None,
					"ota_rate": float(ota_dec) if ota_dec is not None else None,
					"delta": float(delta_dec) if delta_dec is not None else None,
					"status": d_status,
					"stop_sell": live_r["stop_sell"],
					"min_los": live_r["min_los"],
					"max_los": live_r["max_los"],
					"cta": live_r["cta"],
					"ctd": live_r["ctd"],
				})

			lead = day_rows[0] if day_rows else None
			row_status = lead["status"] if lead else "Unmapped"
			if row_status == "OK":
				# Escalate if a later day in the horizon has Undercut/Overpriced
				issue_day = next((dr for dr in day_rows if dr["status"] in ("Undercut", "Overpriced")), None)
				if issue_day:
					lead = issue_day
					row_status = issue_day["status"]

			if row_status == "OK":
				parity_ok += 1
			else:
				parity_issues += 1

			# Keep Channel Room Mapping health fields synced when writable by an authorized manager
			if _can_manage_restrictions() and frappe.get_meta("Channel Room Mapping").has_field("last_parity_status"):
				frappe.db.set_value(
					"Channel Room Mapping",
					m.name,
					{
						"last_parity_status": row_status,
						"last_parity_delta": lead["delta"] if (lead and lead["delta"] is not None) else 0.0,
						"last_error": last_err[:280],
					},
					update_modified=False,
				)

			parity_rows.append({
				"mapping_name": m.name,
				"connection": m.connection,
				"provider": conn.provider,
				"room_type": m.room_type,
				"room_type_name": rt_names_map.get(m.room_type, m.room_type),
				"rate_plan": m.rate_plan or None,
				"rate_plan_label": rp_names_map.get(m.rate_plan, m.rate_plan) if m.rate_plan else _("BAR"),
				"external_room_id": m.external_room_id or "",
				"external_rate_id": ext_rate_id,
				"external_currency": m.external_currency or "",
				"currency": currency,
				"direct_rate": lead["direct_rate"] if lead else None,
				"ota_rate": lead["ota_rate"] if lead else None,
				"parity_delta": lead["delta"] if lead else None,
				"parity_status": row_status,
				"last_synced_at": str(m.last_synced_at or conn.last_push or ""),
				"last_error": last_err,
				"restrictions_synced": {
					"stop_sell": synced_stop_sell,
					"min_stay_arrival": synced_min_los,
					"max_stay": synced_max_los,
					"closed_to_arrival": synced_cta,
					"closed_to_departure": synced_ctd,
					"active_days": synced_active_days,
				},
				"days": day_rows,
			})
	finally:
		channel_ari._pass_directory.reset(token)

	# Identify sellable Room Types with zero active channel mappings
	mapped_rts = {m.room_type for m in mappings if cint(conn_by_name[m.connection].active)}
	room_counts = _room_counts(property)
	unmapped_room_types = []
	for rt in frappe.get_all(
		"Room Type", filters={"property": property, "disabled": 0},
		fields=["name", "room_type_name"], order_by="room_type_name asc",
	):
		rc = room_counts.get(rt.name, 0)
		if rc > 0 and rt.name not in mapped_rts:
			unmapped_room_types.append({
				"name": rt.name,
				"room_type_name": rt.room_type_name or rt.name,
				"room_count": rc,
			})

	formatted_outbox = [
		{
			"name": r.name,
			"connection": r.connection,
			"provider": conn_by_name[r.connection].provider if r.connection in conn_by_name else "",
			"date_from": str(r.date_from or ""),
			"date_to": str(r.date_to or ""),
			"kinds": r.kinds or "both",
			"full": cint(r.full),
			"status": r.status or "Pending",
			"attempts": cint(r.attempts),
			"queued_at": str(r.queued_at or ""),
			"next_attempt_at": str(r.next_attempt_at or "") or None,
			"sent_at": str(r.sent_at or "") or None,
			"reason": r.reason or "",
			"detail": r.detail or "",
		}
		for r in outbox_rows
	]

	return {
		"property": property,
		"property_currency": home,
		"checked_at": str(now_datetime()),
		"can_manage": _can_manage_restrictions(),
		"connections": connection_cards,
		"outbox": formatted_outbox,
		"parity": parity_rows,
		"summary": {
			"connections_total": len(connection_cards),
			"connections_active": sum(1 for c in connection_cards if c["active"]),
			"outbox_pending": sum(c["outbox_counts"]["pending"] + c["outbox_counts"]["sending"] for c in connection_cards),
			"outbox_failed": sum(c["outbox_counts"]["failed"] for c in connection_cards),
			"mappings_total": len(parity_rows),
			"parity_ok": parity_ok,
			"parity_issues": parity_issues,
			"unmapped_room_types": unmapped_room_types,
		},
	}


@frappe.whitelist(methods=["POST"])
@require_roles(*RESTRICTION_EDIT_ROLES)
def retry_outbox_failures(connection: str) -> dict:
	"""Reset Failed Channel ARI Outbox items for `connection` to Pending and trigger a flush."""
	conn = frappe.get_doc("Channel Manager Connection", connection)
	assert_property_access(conn.property)

	failed_names = frappe.get_all(
		"Channel ARI Outbox",
		filters={"connection": conn.name, "status": "Failed"},
		pluck="name",
	)
	for name in failed_names:
		frappe.db.set_value(
			"Channel ARI Outbox",
			name,
			{"status": "Pending", "attempts": 0, "next_attempt_at": None, "detail": ""},
			update_modified=True,
		)

	if failed_names:
		from kamra import channel_ari
		channel_ari.enqueue_flush(conn.name)

	from kamra.savings import log_action
	log_action(
		"channel.retry_outbox",
		"Channel Manager Connection",
		conn.name,
		conn.property,
		rationale=f"Retried {len(failed_names)} failed ARI outbox row(s) on {conn.provider}",
		channel="Desk",
	)

	return {
		"ok": True,
		"connection": conn.name,
		"retried": len(failed_names),
	}
