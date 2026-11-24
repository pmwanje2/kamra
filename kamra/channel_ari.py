"""Outbound ARI by changes: the outbox a channel manager like Channex certifies.

Channex asks a PMS to send only what changed, batched into date ranges, under
its rate limit (10 calls a minute per property and per endpoint), and a full
sync only when someone asks for it - never on a timer. So instead of pushing
the whole snapshot every hour and after every booking:

1. Anything that moves availability or price (a booking, a season, a rate
   plan, a room going out of order, a mapping) queues a `Channel ARI Outbox`
   row, in the same transaction, with the window of dates it can move.
2. One worker per connection (`flush_outbox`) joins the due rows, computes
   the cells of those dates with the same engines as everything else
   (`channel_manager.ari_snapshot` for availability, `pricing.quote_with_contract`
   for each mapped rate plan), keeps only what differs from what the provider last
   accepted (`Channel ARI State`), joins consecutive equal days into ranges
   and sends ONE call per kind - under a local cap per endpoint, waiting a
   short batch window for restrictions so a few edits in a row go together,
   pausing on a 429 and backing off on a 5xx.

Adapters opt in by implementing `push_ari_changes(conn, kind, changes)` (see
kamra.channels); the others keep the hourly whole-snapshot `push_ari`.

Currency: each rate goes out in the currency of the mapped provider rate
plan (E, Channel Room Mapping.external_currency), and the provider never
converts it (Channex's own exchange rate is not the governed one). OPERA
Cloud sends a rate code's own currency or the channel's configured one;
SAP replicates a condition in its currency or converts it into a
fixed-currency channel at the governed rate of the pricing date. So with H
the property's currency and C the contract's: E == C sends the contract
nightly, E == H the property-currency agreed rate the desk books (a plan
priced from the card only this), and anything else - an unknown E included,
for every plan - nothing: the outbox row fails and says why. E == C
converts nothing, so it is read from the approved tariff master data
(hospitality_tariffs.list_tariffs, SAP's condition-record replicate), never
from a quote, and needs no exchange rate: a tariff quote that converts
refuses without one (SAP SG 105), so only E == H needs it. Only a night
with no approved price (a missing tariff) or no price above zero stops
sale. A missing exchange rate (HSP-TARIFF-003) on an E == H night is a
pricing error that passes, not a delisting, and so is any other error while
pricing (a permission refusal, the database, the bridge): the night is held
at what the provider last accepted, retried on its own, and Finance (an
exchange rate) or Revenue (anything else) is told. The worker prices as the system, never
as the user whose change queued it.
"""

from __future__ import annotations

import json
import math
import time
from contextvars import ContextVar
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe import _
from frappe.utils import add_days, cint, date_diff, getdate, nowdate

from kamra.authz import act_as, require_roles

ARI_HORIZON_DAYS = 500  # the full sync and the nightly sweep
ARI_MAX_DAYS = 730  # a user's change is sent whole up to here
KINDS = ("availability", "restrictions")
# the tariff directory of one restriction_cells pass, read once for all its nights
_pass_directory: ContextVar[dict | None] = ContextVar("kamra_ari_tariff_directory", default=None)


def _now() -> float:
	"""The module's clock (tests move it)."""
	return time.time()


# ---------------------------------------------------------------------------
# Cells: {external_id: {"YYYY-MM-DD": {field: value}}}
# ---------------------------------------------------------------------------


def _clip(date_from, date_to) -> tuple[str, str] | None:
	"""[date_from, date_to] inside [today, today + ARI_MAX_DAYS); None if
	nothing is left."""
	today = getdate(nowdate())
	start = max(getdate(date_from or today), today)
	end = min(getdate(date_to or add_days(today, ARI_HORIZON_DAYS - 1)),
	          getdate(add_days(today, ARI_MAX_DAYS - 1)))
	if end < start:
		return None
	return str(start), str(end)


def availability_cells(conn, date_from, date_to) -> dict[str, dict[str, dict]]:
	"""{external_room_id: {"YYYY-MM-DD": {"availability": int}}} for each mapped
	room type, once per external_room_id however many rate plans it has. It
	comes from channel_manager.ari_snapshot: the same engine as the
	double-booking guard, without copying it."""
	from kamra.channel_manager import ari_snapshot
	window = _clip(date_from, date_to)
	if not window:
		return {}
	start, end = window
	out: dict[str, dict[str, dict]] = {}
	for row in ari_snapshot(conn.property, conn.name, date_diff(end, start) + 1,
	                        start=start, with_rates=False):
		room = out.setdefault(str(row["external_room_id"]), {})
		for d in row["days"]:
			room.setdefault(d["date"], {"availability": int(d["available"])})
	return out


class AriPriceHeld(Exception):
	"""The night's contract has no published exchange rate (HSP-TARIFF-003):
	nothing is sent for it, so the provider keeps the last rate it accepted."""


class AriPricingFailed(Exception):
	"""The night could not be priced for a reason that is not a refusal (a
	permission refusal, the database, the bridge): nothing is sent for it,
	so the provider keeps the last rate it accepted."""


class AriCurrencyRefused(Exception):
	"""The mapped provider rate plan sells in a currency the night cannot be
	priced in: neither the contract's nor the property's, or not known."""


def rate_quantum(property: str | None) -> Decimal:
	"""The smallest step of a rate sent to a channel: the ISO 4217 minor unit
	of the currency the property sells in, as pricing.quote prices it (1 for
	UGX, 0.001 for KWD); 0.01 without a property."""
	from kamra.pricing import _minor_unit, selling_currency
	return Decimal(1).scaleb(-_minor_unit(selling_currency(property)))


def channel_rate(property: str, q: dict) -> Decimal:
	"""One night's room price as the channels sell it, from a one-night
	quote, on the property's selling basis: gross - the night's room tax put
	back on - where its rates include tax, net otherwise (the OTA adds the
	tax, its extranet set to match). An OTA booking's total is read as gross
	(channel_manager.lock_channel_rates), so one that comes back at this
	price bills what the desk would. To the currency's minor unit."""
	from kamra.pricing import rates_include_tax
	rate = Decimal(str(q["room_total"]))
	if rates_include_tax(property):
		rate *= 1 + Decimal(str((q.get("nightly") or [{}])[0].get("gst_rate") or 0)) / 100
	return rate.quantize(rate_quantum(property), rounding=ROUND_HALF_UP)


def _sold_currency(rate_plan: str | None, external_currency: str | None, home: str) -> str:
	"""The currency a mapping's rates go out in: the provider rate plan's
	when it is the plan's own (C) or the property's (H). AriCurrencyRefused
	otherwise, and when it is not known: never a guess."""
	from kamra.pricing import rate_plan_currency, resolve_rate_plan_chain
	priced_in = rate_plan_currency(resolve_rate_plan_chain(rate_plan)) if rate_plan else home
	if external_currency and external_currency in (priced_in, home):
		return external_currency
	raise AriCurrencyRefused(_("sells in {0}; it can go only in {1}").format(
		external_currency or _("an unknown currency"), " or ".join(sorted({priced_in, home}))))


def _plan_night_rate(property: str, room_type: str, rate_plan: str | None, d,
                     currency: str | None = None) -> Decimal | None:
	"""One night from `d` on `rate_plan` (None: the room type's base price),
	for the type's base occupancy (or 2 adults), at the currency's minor unit,
	in `currency` (E; None: see _sold_currency). In the property's currency it
	is priced as the desk prices it (pricing.quote_with_contract): the agreed
	rate of a contract night, else the card on the property's selling basis
	(channel_rate). In the contract's own currency it is the contract nightly
	(a derived plan's modifier in it) from the approved tariff master data
	(pricing.contract_list_nightly, read once in a restriction_cells pass),
	which converts nothing and so needs no exchange rate (SAP SG 105: only a
	quote that converts does). A card night goes only in the property's.

	None when the night has no approved price (a missing tariff among them)
	or it is not worth more than 0: stop sale. AriPriceHeld when the
	property-currency night's exchange rate is missing or stale,
	AriPricingFailed on any other error while pricing it,
	AriCurrencyRefused when it cannot go in `currency`."""
	from kamra.pricing import (
		ContractRateRefused,
		_minor_unit,
		contract_list_nightly,
		property_currency,
		quote_with_contract,
	)
	home = property_currency(property)
	if not currency:
		currency = _sold_currency(rate_plan, None, home)
	adults = frappe.db.get_value("Room Type", room_type, "base_occupancy") or 2
	try:
		if currency != home:
			listed = contract_list_nightly(property, room_type, rate_plan, d, int(adults), _pass_directory.get()) \
				if rate_plan else None
		else:
			q = quote_with_contract(property, room_type, str(d), str(add_days(d, 1)), adults=int(adults),
			                        children=0, rate_plan=rate_plan or None)
	except ContractRateRefused as exc:
		if "HSP-TARIFF-003" in str(exc):
			raise AriPriceHeld(str(exc)) from None
		return None
	except Exception as exc:
		raise AriPricingFailed(f"{type(exc).__name__}: {exc}") from exc
	if currency != home:
		if not listed or listed[0] != currency:
			raise AriCurrencyRefused(_("sells in {0}; this night is priced in {1}").format(
				currency, " or ".join(sorted({listed[0] or _("no currency"), home} if listed else {home}))))
		amount = listed[1]
	else:
		tariff = q.get("tariff")
		if not tariff:
			try:
				total = channel_rate(property, q)
			except Exception as exc:
				raise AriPricingFailed(f"{type(exc).__name__}: {q.get('room_total')!r} is not a price") from exc
			return total if total > 0 else None
		amount = tariff.get("nightly") if currency == tariff.get("currency") else q.get("agreed_rate")
	try:
		quantum = Decimal(1).scaleb(-_minor_unit(currency))
		total = Decimal(str(amount)).quantize(quantum, rounding=ROUND_HALF_UP)
	except Exception as exc:
		raise AriPricingFailed(f"{type(exc).__name__}: {amount!r} is not a price") from exc
	return total if total > 0 else None


def _restriction_day(rate: Decimal | None) -> dict:
	"""The one place a day's restriction values are put together (minimum
	stay and the like would add their fields here): the rate, or stop-sell
	when there is no price."""
	if rate is None:
		return {"stop_sell": True}
	return {"rate": rate, "stop_sell": False}


def restriction_cells(conn, date_from, date_to, problems: dict | None = None) -> dict[str, dict[str, dict]]:
	"""{external_rate_id: {"YYYY-MM-DD": {"rate": Decimal, "stop_sell": False}}}
	for each mapping with a provider rate plan id, priced by _plan_night_rate
	in the mapping's currency (the tariff directory read once for the pass);
	a day without a price is {"stop_sell": True} and carries no rate.

	A mapping that cannot go in its currency has no cells at all, and a night
	whose exchange rate is missing, or that errs while it is priced, has none
	either. `problems` (when given) collects them: "refused"
	{external_rate_id: why}, "held" and "errored" {external_rate_id:
	{"dates": [...], "reason": ..., "rate_plan": ...}}."""
	from kamra.pricing import property_currency
	problems = {} if problems is None else problems
	window = _clip(date_from, date_to)
	if not window:
		return {}
	start, end = window
	days = date_diff(end, start) + 1
	home = property_currency(conn.property)
	token = _pass_directory.set({})
	try:
		return _restriction_cells(conn, start, days, home, problems)
	finally:
		_pass_directory.reset(token)


def _restriction_cells(conn, start: str, days: int, home: str, problems: dict) -> dict[str, dict[str, dict]]:
	"""restriction_cells for `days` days from `start`, inside its pass."""
	from kamra.channel_manager import _mappings
	out: dict[str, dict[str, dict]] = {}
	for m in _mappings(conn.name):
		if not m.external_rate_id:
			continue
		ext_id = str(m.external_rate_id)
		plan: dict[str, dict] = {}
		try:
			currency = _sold_currency(m.rate_plan, m.get("external_currency"), home)
			for i in range(days):
				d = getdate(add_days(start, i))
				try:
					plan[str(d)] = _restriction_day(
						_plan_night_rate(conn.property, m.room_type, m.rate_plan, d, currency))
				except (AriPriceHeld, AriPricingFailed) as exc:
					key = "held" if isinstance(exc, AriPriceHeld) else "errored"
					held = problems.setdefault(key, {}).setdefault(
						ext_id, {"dates": [], "reason": str(exc), "rate_plan": m.rate_plan})
					held["dates"].append(str(d))
		except AriCurrencyRefused as exc:
			problems.setdefault("refused", {})[ext_id] = f"{ext_id} {exc}"
			problems.get("held", {}).pop(ext_id, None)
			problems.get("errored", {}).pop(ext_id, None)
			continue
		if plan:
			out[ext_id] = plan
	return out


def _canon(value):
	"""A cell value as stored in Channel ARI State (JSON): rates as decimal
	strings with every decimal they carry and at least 2, so 241 and
	"241.00" compare equal and a KWD 12.349 is not taken for 12.345."""
	if isinstance(value, Decimal | float) and not isinstance(value, bool):
		rate = Decimal(str(value))
		places = max(2, -rate.normalize().as_tuple().exponent)
		return str(rate.quantize(Decimal(1).scaleb(-places)))
	return value


def diff_cells(cells: dict, sent: dict) -> dict:
	"""Only the fields whose value differs from `sent` (same shape); a day
	with no change does not appear, nor an id with no changed day."""
	out: dict[str, dict[str, dict]] = {}
	for ext_id, days in cells.items():
		known = sent.get(ext_id) or {}
		for d, values in days.items():
			before = known.get(d) or {}
			changed = {f: v for f, v in values.items()
			           if f not in before or _canon(before[f]) != _canon(v)}
			if changed:
				out.setdefault(ext_id, {})[d] = changed
	return out


def compress(cells: dict) -> list[dict]:
	"""[{"external_id", "date" | "date_from" + "date_to", "values"}]:
	CONSECUTIVE days with the SAME values become one range; a lone day goes
	as "date". Stable order: by external_id, then date."""
	out = []
	for ext_id in sorted(cells):
		run = None
		for d in sorted(cells[ext_id]):
			values = cells[ext_id][d]
			key = {f: _canon(v) for f, v in values.items()}
			if run and run["key"] == key and str(add_days(run["end"], 1)) == d:
				run["end"] = d
				continue
			if run:
				out.append(_range(ext_id, run))
			run = {"start": d, "end": d, "key": key, "values": values}
		if run:
			out.append(_range(ext_id, run))
	return out


def _range(ext_id: str, run: dict) -> dict:
	if run["start"] == run["end"]:
		return {"external_id": ext_id, "date": run["start"], "values": run["values"]}
	return {"external_id": ext_id, "date_from": run["start"], "date_to": run["end"],
	        "values": run["values"]}


# ---------------------------------------------------------------------------
# What the provider accepted (Channel ARI State)
# ---------------------------------------------------------------------------

_ID_FIELD = {"availability": "external_room_id", "restrictions": "external_rate_id"}


def _state_name(connection: str, kind: str, ext_id: str) -> str:
	return f"{connection}|{kind}|{ext_id}"


def _load_sent(connection: str, kind: str) -> dict:
	out = {}
	for r in frappe.get_all("Channel ARI State", filters={"connection": connection, "kind": kind},
	                        fields=["external_id", "sent_values"]):
		try:
			out[r.external_id] = json.loads(r.sent_values or "{}")
		except ValueError:
			out[r.external_id] = {}
	return out


def _remember(connection: str, kind: str, cells: dict) -> None:
	"""Merge accepted cells into Channel ARI State, field by field; days
	before today are dropped."""
	today = nowdate()
	for ext_id, days in cells.items():
		if not days:
			continue
		name = _state_name(connection, kind, ext_id)
		exists = frappe.db.exists("Channel ARI State", name)
		known = {}
		if exists:
			try:
				known = json.loads(frappe.db.get_value("Channel ARI State", name, "sent_values") or "{}")
			except ValueError:
				known = {}
		for d, values in days.items():
			known.setdefault(d, {}).update({f: _canon(v) for f, v in values.items()})
		payload = json.dumps({d: known[d] for d in sorted(known) if d >= today}, sort_keys=True)
		if exists:
			frappe.db.set_value("Channel ARI State", name, "sent_values", payload)
		else:
			frappe.get_doc({"doctype": "Channel ARI State", "connection": connection, "kind": kind,
				"external_id": ext_id, "sent_values": payload}).insert(ignore_permissions=True)


def _forget(connection: str, kind: str, ext_id: str | None) -> None:
	if ext_id:
		frappe.db.delete("Channel ARI State", {"name": _state_name(connection, kind, ext_id)})


def _expand(changes: list[dict]) -> dict:
	"""compress() undone: the cells a list of changes stands for."""
	cells: dict[str, dict[str, dict]] = {}
	for c in changes:
		start = c.get("date") or c["date_from"]
		end = c.get("date") or c["date_to"]
		for i in range(date_diff(end, start) + 1):
			cells.setdefault(c["external_id"], {})[str(add_days(start, i))] = dict(c["values"])
	return cells


# ---------------------------------------------------------------------------
# The outbox
# ---------------------------------------------------------------------------

ARI_JOB = "channex-ari::{connection}"
_ARI_PAUSE = "channex_ari_pause::{connection}::{kind}"
_ARI_CALLS = "channex_ari_calls::{connection}::{kind}"  # timestamps of the last minute's calls
_ARI_ERROR = "channex_ari_error::{connection}"
_ARI_FX = "channex_ari_fx::{connection}"
_ARI_CURRENCY = "channex_ari_currency::{connection}"
_ARI_PRICING = "channex_ari_pricing::{connection}"


def _dt(ts: float) -> datetime:
	"""A timestamp of the module's clock as Frappe stores a Datetime: naive,
	on the system time zone, to the second."""
	from zoneinfo import ZoneInfo

	from frappe.utils import get_system_timezone
	return datetime.fromtimestamp(int(ts), ZoneInfo(get_system_timezone())).replace(tzinfo=None)


def _ts(value) -> float:
	from zoneinfo import ZoneInfo

	from frappe.utils import get_datetime, get_system_timezone
	return get_datetime(value).replace(tzinfo=ZoneInfo(get_system_timezone())).timestamp()


def _supports_outbox(provider: str | None) -> bool:
	from kamra.channels import provider_for
	try:
		return hasattr(provider_for(provider), "push_ari_changes")
	except Exception:
		return False


def outbox_connections(property: str | None = None) -> list[frappe._dict]:
	"""The active connections whose adapter takes changes (push_ari_changes)."""
	filters = {"active": 1}
	if property:
		filters["property"] = property
	return [c for c in frappe.get_all("Channel Manager Connection", filters=filters,
	                                  fields=["name", "property", "provider"])
	        if _supports_outbox(c.provider)]


def uses_outbox(connection: str) -> bool:
	return _supports_outbox(frappe.db.get_value("Channel Manager Connection", connection, "provider"))


def enqueue_flush(connection: str) -> None:
	"""One worker per connection at a time, after the change commits."""
	frappe.enqueue("kamra.channel_ari.flush_outbox", queue="short", connection=connection,
	               job_id=ARI_JOB.format(connection=connection), deduplicate=True,
	               enqueue_after_commit=True)


def _insert_row(values: dict) -> str:
	return frappe.get_doc(dict(values, doctype="Channel ARI Outbox")).insert(ignore_permissions=True).name


def _queued_here() -> set:
	"""The outbox rows this transaction inserted (emptied on commit or
	rollback). Only these can be joined: a Pending row another transaction
	committed may be the one a worker is claiming right now, and this
	transaction's snapshot would still show it Pending."""
	rows = getattr(frappe.local, "kamra_ari_queued", None)
	if rows is None:
		rows = frappe.local.kamra_ari_queued = set()

		def forget():
			frappe.local.kamra_ari_queued = None
		frappe.db.after_commit.add(forget)
		frappe.db.after_rollback.add(forget)
	return rows


def _queue(connection: str, property: str, date_from, date_to, kinds: str, reason: str,
           full: bool) -> str | None:
	window = _clip(date_from, date_to)
	if not window:
		return None
	values = {"connection": connection, "status": "Pending", "kinds": kinds, "full": int(bool(full)),
	          "date_from": window[0], "date_to": window[1], "attempts": 0}
	# the same change twice in one transaction is one row (an insert fires
	# both after_insert and on_update...). Never a row a worker claimed
	# (Sending): what it sends was computed before this change.
	queued = _queued_here()
	name = frappe.db.get_value("Channel ARI Outbox", dict(values, name=("in", sorted(queued))),
	                           "name") if queued else None
	if not name:
		name = _insert_row(dict(values, property=property, reason=(reason or "")[:140],
		                        queued_at=_dt(_now())))
		queued.add(name)
	enqueue_flush(connection)
	return name


def enqueue_ari_change(property: str, date_from=None, date_to=None, kinds: str = "both", reason: str = "",
                       full: bool = False, connections: set | None = None) -> list[str]:
	"""Queue a change for every active connection of `property` whose adapter
	takes changes (only those named in `connections`, when given): one
	Pending row each, in the caller's transaction (a rolled-back change
	leaves none), and a flush after the commit. Dates default to today ..
	today + ARI_HORIZON_DAYS. Never raises: a failure goes to the Error Log
	and the save that triggered it goes on."""
	try:
		names = []
		for c in outbox_connections(property):
			if connections is not None and c.name not in connections:
				continue
			name = _queue(c.name, c.property, date_from, date_to, kinds, reason, full)
			if name:
				names.append(name)
		return names
	except Exception as exc:
		frappe.log_error(title="Channex ARI enqueue",
		                 message=f"{property} · {reason} · {type(exc).__name__}: {exc}"[:1000])
		return []


def _hold(conn, kind: str, rows: list, now: float) -> int:
	"""Seconds this kind has to wait before a call: a pause (429 or a spent
	RateLimit header), the local cap of calls per minute, or - restrictions
	only - the batch window that lets a few edits in a row go together."""
	until = frappe.cache.get_value(_ARI_PAUSE.format(connection=conn.name, kind=kind))
	if until and float(until) > now:
		return max(1, math.ceil(float(until) - now))
	calls = sorted(t for t in (frappe.cache.get_value(
		_ARI_CALLS.format(connection=conn.name, kind=kind)) or []) if t > now - 60)
	if len(calls) >= int(conn.ari_calls_per_minute or 10):
		return max(1, math.ceil(calls[0] + 60 - now))
	batch = int(conn.ari_batch_seconds or 0)
	if kind == "restrictions" and batch and not any(r.full for r in rows):
		oldest = min((_ts(r.queued_at) for r in rows if r.queued_at), default=now)
		if now - oldest < batch:
			return max(1, math.ceil(oldest + batch - now))
	return 0


def _count_call(connection: str, kind: str, now: float) -> None:
	key = _ARI_CALLS.format(connection=connection, kind=kind)
	calls = [t for t in (frappe.cache.get_value(key) or []) if t > now - 60] + [now]
	frappe.cache.set_value(key, calls, expires_in_sec=120)


def _pause(connection: str, kind: str, seconds: int, now: float) -> int:
	seconds = max(1, int(seconds))
	frappe.cache.set_value(_ARI_PAUSE.format(connection=connection, kind=kind), now + seconds,
	                       expires_in_sec=seconds + 60)
	return seconds


def _ari_error(conn, exc: Exception) -> None:
	"""Channex unreachable (5xx, network, timeout): one Error Log an hour."""
	key = _ARI_ERROR.format(connection=conn.name)
	if frappe.cache.get_value(key):
		return
	frappe.cache.set_value(key, 1, expires_in_sec=3600)
	frappe.log_error(title=f"Channex ARI {conn.name}", message=f"{type(exc).__name__}: {exc}"[:1000])


def _once_an_hour(key: str) -> bool:
	if frappe.cache.get_value(key):
		return False
	frappe.cache.set_value(key, 1, expires_in_sec=3600)
	return True


def _currency_detail(refused: dict) -> str:
	return "Currency: " + " | ".join(refused[k] for k in sorted(refused)) + " - not sent" if refused else ""


def _held_detail(held: dict, why: str = "no exchange rate") -> str:
	if not held:
		return ""
	reasons = "; ".join(sorted({h["reason"] for h in held.values()}))
	return f"Held ({why}): {', '.join(sorted(held))}: {reasons}"


def _currency_refused(conn, detail: str) -> None:
	"""A mapping in a currency it cannot go in: one Error Log an hour."""
	if _once_an_hour(_ARI_CURRENCY.format(connection=conn.name)):
		frappe.log_error(title=f"Channex ARI currency {conn.name}", message=detail[:1000])


def _fx_alert(conn, held: dict) -> None:
	"""Finance publishes the exchange rate: tell it through the notification
	bell (and the Error Log), once an hour per connection."""
	if not _once_an_hour(_ARI_FX.format(connection=conn.name)):
		return
	from kamra.notifications import record
	from kamra.pricing import rate_plan_currency, resolve_rate_plan_chain
	plans = sorted({h["rate_plan"] for h in held.values() if h.get("rate_plan")})
	names = [frappe.db.get_value("Rate Plan", p, "rate_plan_name") or p for p in plans]
	currencies = sorted({rate_plan_currency(resolve_rate_plan_chain(p)) for p in plans})
	record(conn.property, "finance", _(
		"No published {0} exchange rate: channel rates for {1} stay at their last price until it is "
		"published.").format("/".join(currencies), ", ".join(names) or ", ".join(sorted(held))),
		doctype="Channel Manager Connection", name=conn.name)
	frappe.log_error(title=f"Channex ARI FX held {conn.name}", message=_held_detail(held)[:1000])


def _pricing_alert(conn, errored: dict) -> None:
	"""Nights that errored while they were priced: Revenue is told through
	the notification bell, and the Error Log says why, once an hour per
	connection."""
	if not _once_an_hour(_ARI_PRICING.format(connection=conn.name)):
		return
	from kamra.notifications import record
	plans = sorted({h["rate_plan"] for h in errored.values() if h.get("rate_plan")})
	names = [frappe.db.get_value("Rate Plan", p, "rate_plan_name") or p for p in plans]
	record(conn.property, "revenue", _(
		"Channel rates for {0} could not be priced: they stay at their last price and are retried.").format(
		", ".join(names) or ", ".join(sorted(errored))), doctype="Channel Manager Connection", name=conn.name)
	frappe.log_error(title=f"Channex ARI pricing error {conn.name}",
	                 message=_held_detail(errored, "pricing error")[:1000])


def _windows(rows: list) -> list[tuple[str, str]]:
	"""The rows' date windows, clipped and merged where they touch."""
	spans = sorted(w for w in (_clip(r.date_from, r.date_to) for r in rows) if w)
	merged: list[tuple[str, str]] = []
	for start, end in spans:
		if merged and getdate(start) <= getdate(add_days(merged[-1][1], 1)):
			merged[-1] = (merged[-1][0], max(merged[-1][1], end))
		else:
			merged.append((start, end))
	return merged


def _cells(conn, kind: str, windows: list[tuple[str, str]], problems: dict | None = None) -> dict:
	out: dict[str, dict[str, dict]] = {}
	for start, end in windows:
		cells = (availability_cells(conn, start, end) if kind == "availability"
		         else restriction_cells(conn, start, end, problems))
		for ext_id, days in cells.items():
			out.setdefault(ext_id, {}).update(days)
	return out


def _key(change: dict) -> tuple:
	return (str(change.get("external_id")), change.get("date"), change.get("date_from"), change.get("date_to"))


def _other(kind: str) -> str:
	return "restrictions" if kind == "availability" else "availability"


def _noted(r, task_ids: list, detail: str) -> dict:
	"""The task ids and detail a pass adds to a row."""
	values = {}
	if task_ids:
		values["task_ids"] = "\n".join(filter(None, [r.task_ids or "", *task_ids]))[:2000]
		r.task_ids = values["task_ids"]
	if detail:
		values["detail"] = detail[:280]
	return values


def _done(rows: list, kind: str, task_ids: list, now: float, detail: str = "", failed: bool = False) -> None:
	"""This kind of these rows went out (or had nothing to send); `failed`
	when part of it was refused (a currency mismatch): the row ends Failed."""
	for r in rows:
		values = _noted(r, task_ids, detail)
		if r.kinds == "both":
			r.kinds = values["kinds"] = _other(kind)
		elif failed:
			r.status = values["status"] = "Failed"
		else:
			r.status = values["status"] = "Sent"
			values["sent_at"] = _dt(now)
		frappe.db.set_value("Channel ARI Outbox", r.name, values, update_modified=True)


def _retry_held(conn, rows: list, kind: str, task_ids: list, detail: str, ext_ids: list, dates: list,
                refused: bool, now: float) -> int:
	"""Some nights of this kind could not be priced now (an exchange rate
	missing, an error): they get a retry of their own - one Pending row of
	this kind for those nights only, never a full sync, with the 5xx
	back-off. The rest of the kind went out, so the claimed rows are done
	with it: a full sync is not sent again, and the other kind of a row
	keeps its own wait. Seconds to the retry."""
	attempts = max(int(r.attempts or 0) for r in rows) + 1
	wait = min(30 * 2 ** (attempts - 1), 1800)
	_insert_row({"connection": conn.name, "property": conn.property, "status": "Pending", "kinds": kind,
	             "full": 0, "date_from": dates[0], "date_to": dates[-1], "attempts": attempts,
	             "next_attempt_at": _dt(now + wait), "queued_at": _dt(now),
	             "reason": f"Retry held nights: {', '.join(ext_ids)}"[:140], "detail": detail[:280]})
	_done(rows, kind, task_ids, now, detail, failed=refused)
	return wait


def _backoff(rows: list, bumped: set, now: float) -> None:
	for r in rows:
		if r.name in bumped:
			continue
		bumped.add(r.name)
		r.attempts = int(r.attempts or 0) + 1
		wait = min(30 * 2 ** (r.attempts - 1), 1800)
		r.next_attempt_at = _dt(now + wait)
		frappe.db.set_value("Channel ARI Outbox", r.name, {
			"attempts": r.attempts, "next_attempt_at": r.next_attempt_at}, update_modified=True)


def _fail(rows: list, detail: str) -> None:
	for r in rows:
		r.status = "Failed"
		frappe.db.set_value("Channel ARI Outbox", r.name, {"status": "Failed", "detail": detail[:280]},
		                    update_modified=True)


def _rejected_detail(rejected: list[dict]) -> str:
	parts = []
	for c in rejected:
		when = c.get("date") or f"{c.get('date_from')}..{c.get('date_to')}"
		why = "; ".join(f"{f}: {', '.join(map(str, m))}" for f, m in (c.get("reasons") or {}).items())
		parts.append(f"{c.get('external_id')} {when} ({why})")
	return "Rejected: " + " | ".join(parts) if parts else ""


_CLAIM_SECONDS = 600  # a worker that dies mid-call: its rows are taken again after this
_ROW_FIELDS = ["name", "status", "date_from", "date_to", "kinds", "full", "queued_at", "next_attempt_at",
               "attempts", "task_ids"]


def _claim(connection: str, now: float) -> list:
	"""Take the connection's due rows - Pending with their wait over, or
	Sending whose worker died (its claim lapsed) - and mark them Sending
	until the pass ends, committed BEFORE the cells are computed: a change
	saved from here on gets a row of its own (_queue never joins a Sending
	row) and goes out on the next pass. The locking read keeps a second
	worker off the same rows."""
	rows = frappe.db.get_values("Channel ARI Outbox",
	                            {"connection": connection, "status": ("in", ("Pending", "Sending"))},
	                            _ROW_FIELDS, as_dict=True, order_by="creation asc", for_update=True)
	due = [r for r in rows if not r.next_attempt_at or _ts(r.next_attempt_at) <= now]
	for r in due:
		# a lapsed claim's wait was the claim itself: none left
		r.next_attempt_at = r.next_attempt_at if r.status == "Pending" else None
		r.status = "Sending"
		frappe.db.set_value("Channel ARI Outbox", r.name, {
			"status": "Sending", "next_attempt_at": _dt(now + _CLAIM_SECONDS)}, update_modified=True)
	if due:
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- ari worker: the claim is seen before any cell is computed
	return due


def _release(rows: list, reread: bool = False) -> None:
	"""The claimed rows this pass did not finish go back to Pending with
	their own wait (the retry's, or the one they had). After a rollback the
	row's status is read again: what the pass had not committed is undone."""
	for r in rows:
		if reread:
			r.status = frappe.db.get_value("Channel ARI Outbox", r.name, "status")
		if r.status != "Sending":
			continue
		r.status = "Pending"
		frappe.db.set_value("Channel ARI Outbox", r.name, {
			"status": "Pending", "next_attempt_at": r.next_attempt_at}, update_modified=True)


def flush_outbox(connection: str) -> dict:
	"""Worker (short queue). Claims the connection's rows that are due,
	merges their windows and, per kind: computes the cells, keeps what
	differs from Channel ARI State (everything if a row asks for a full
	sync), joins it into ranges and makes ONE call. What it did not finish
	goes back to Pending. Returns {kind: {"sent": n} | {"paused": s} |
	{"waiting": s} | {"retry": s} | {"failed": detail} | {"nothing": True}}.

	frappe.enqueue runs a job as the user who queued it - Guest on a booking
	webhook, an accounts user saving a Currency Exchange - so the pass runs
	as the system (Administrator, as the scheduler's jobs do) and hands that
	user back after."""
	with act_as("Administrator"):
		return _flush_outbox(connection)


def _flush_outbox(connection: str) -> dict:
	conn = frappe.get_doc("Channel Manager Connection", connection)
	result = {kind: {"nothing": True} for kind in KINDS}
	if not conn.active or not _supports_outbox(conn.provider):
		return result
	now = _now()
	rows = _claim(connection, now)
	if not rows:
		return result
	try:
		_flush(conn, rows, now, result)
	except BaseException:
		frappe.db.rollback()
		_release(rows, reread=True)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- ari worker: an unexpected error gives the claimed rows back
		raise
	_release(rows)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- ari worker: the rows left for a later pass
	return result


def _flush(conn, rows: list, now: float, result: dict) -> None:
	from kamra.channels import provider_for
	provider = provider_for(conn.provider)
	connection = conn.name
	bumped: set = set()
	sent, failed = {}, []
	for kind in KINDS:
		krows = [r for r in rows if r.get("status") != "Failed" and r.kinds in ("both", kind)]
		if not krows:
			continue
		wait = _hold(conn, kind, krows, now)
		if wait:
			result[kind] = {"waiting": wait}
			continue
		full = any(r.full for r in krows)
		problems: dict = {}
		cells = _cells(conn, kind, _windows(krows), problems)
		changes = compress(cells if full else diff_cells(cells, _load_sent(connection, kind)))
		refused = _currency_detail(problems.get("refused") or {})
		if refused:
			failed.append(f"{kind}: {refused}")
			_currency_refused(conn, refused)
		if not changes:
			_settle(conn, krows, kind, [], now, refused, problems, result, 0)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- ari worker: nothing to send, the rows are done
			continue
		_count_call(connection, kind, now)
		try:
			out = provider.push_ari_changes(conn, kind, changes)
		except Exception as exc:
			status = getattr(exc, "status", None)
			if getattr(exc, "retry_after", None) is not None:
				result[kind] = {"paused": _pause(connection, kind, exc.retry_after, now)}
			elif status is not None and 400 <= int(status) < 500:
				detail = str(exc)[:280]
				_fail(krows, detail)
				failed.append(f"{kind}: {detail}")
				frappe.log_error(title=f"Channex ARI failed {conn.name}",
				                 message=f"{kind}: {type(exc).__name__}: {exc}"[:1000])
				result[kind] = {"failed": detail}
			else:
				_backoff(krows, bumped, now)
				_ari_error(conn, exc)
				result[kind] = {"retry": min(30 * 2 ** (max(r.attempts for r in krows) - 1), 1800)}
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- ari worker: keep the attempt and its error
			continue
		rejected = out.get("rejected") or []
		dropped = {_key(c) for c in rejected}
		_remember(connection, kind, _expand([c for c in changes if _key(c) not in dropped]))
		detail = " | ".join(filter(None, [_rejected_detail(rejected), refused]))
		_settle(conn, krows, kind, out.get("task_ids") or [], now, detail, problems, result, len(changes))
		if rejected:
			frappe.log_error(title=f"Channex ARI rejected {conn.name}", message=json.dumps(
				[dict(c, values={f: _canon(v) for f, v in (c.get("values") or {}).items()}) for c in rejected],
				default=str)[:4000])
		if out.get("wait"):
			_pause(connection, kind, out["wait"], now)
		sent[kind] = len(changes)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- ari worker: what the provider accepted is recorded before the next call
	if sent or failed:
		_report(conn, sent, failed)


def _settle(conn, rows: list, kind: str, task_ids: list, now: float, detail: str, problems: dict,
            result: dict, sent: int) -> None:
	"""After a kind's call (or none needed): nights held for an exchange rate
	or that errored get a retry of their own and Finance or Revenue is told;
	a refused currency fails the rows; otherwise they are done."""
	held, errored = problems.get("held") or {}, problems.get("errored") or {}
	refused = bool(problems.get("refused"))
	if held or errored:
		detail = " | ".join(filter(None, [detail, _held_detail(held), _held_detail(errored, "pricing error")]))
		dates = sorted({d for nights in (held, errored) for h in nights.values() for d in h["dates"]})
		wait = _retry_held(conn, rows, kind, task_ids, detail, sorted({*held, *errored}), dates, refused, now)
		result[kind] = {"sent": sent, "held": wait}
		if held:
			_fx_alert(conn, held)
		if errored:
			_pricing_alert(conn, errored)
		return
	_done(rows, kind, task_ids, now, detail, failed=refused)
	result[kind] = {"sent": sent, "failed": detail} if refused else {"sent": sent}


def _report(conn, sent: dict, failed: list) -> None:
	"""last_push / last_push_status and the audit entry, as push_ari does."""
	from kamra.savings import log_action
	if failed:
		status = "FAILED: " + "; ".join(failed)
	else:
		status = f"OK: {sent.get('availability', 0)} availability, {sent.get('restrictions', 0)} restrictions"
	frappe.db.set_value("Channel Manager Connection", conn.name, {
		"last_push": frappe.utils.now_datetime(), "last_push_status": status[:140]}, update_modified=False)
	log_action("channel.ari_push", "Channel Manager Connection", conn.name, conn.property,
	           rationale=f"{conn.provider}: {status[:160]}", channel="API")
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- ari worker: the push result


def enqueue_ari_flushes() -> None:
	"""Every minute: a flush for each connection with Pending rows (what was
	waiting for its batch window, its pause or its retry) or Sending ones (a
	worker that died mid-call: the flush takes them once the claim lapses)."""
	for name in sorted(set(frappe.get_all("Channel ARI Outbox",
	                                      filters={"status": ("in", ("Pending", "Sending"))},
	                                      pluck="connection"))):
		enqueue_flush(name)


# ---------------------------------------------------------------------------
# Triggers, the full sync on demand and the nightly sweep
# ---------------------------------------------------------------------------

_HORIZON_SOURCES = ("Rate Plan", "Room Type", "Hurdle Rate", "Rate Guardrail")
# the exchange rate a contract converts at: ERPNext's selling Currency
# Exchange where Kamra shares its site (pricing.posting_fx_rate and
# hospitality_tariffs.tariff_rate read nothing else; Kamra's own Exchange
# Rate is the FX desk's, and moves no channel price)
_FX_SOURCES = ("Currency Exchange",)
# What a property's prices are offered on (channel_rate, rate_quantum): its
# selling basis, currency, tax pack and room tax rates.
_PROPERTY_BASIS = ("rates_include_tax", "currency", "country", "gst_mode", "gst_slab_threshold",
                   "gst_rate_low", "gst_rate_high")


def _dates(*values) -> list:
	return [getdate(v) for v in values if v]


def _window(starts: list, ends: list) -> tuple[str, str] | None:
	if not starts or not ends:
		return None
	start, end = min(starts), max(ends)
	return str(start), str(max(start, end))


def reservation_window(doc, method=None) -> tuple[str, str] | None:
	"""The nights a booking holds, before and after the change:
	[min(check-in), max(check-out)) as an inclusive (from, to). None when an
	update moved nothing that counts (dates, room type, room, status)."""
	before = doc.get_doc_before_save() if method == "on_update" else None
	if method == "on_update":
		if doc.flags.in_insert:
			return None  # after_insert has it
		fields = ("check_in_date", "check_out_date", "room_type", "room", "status")
		if before and not any(str(before.get(f) or "") != str(doc.get(f) or "") for f in fields):
			return None
	ins = _dates(doc.get("check_in_date"), before and before.get("check_in_date"))
	outs = [add_days(d, -1) for d in _dates(doc.get("check_out_date"), before and before.get("check_out_date"))]
	return _window(ins, outs)


def _mapping_property(doc) -> str | None:
	return frappe.db.get_value("Channel Manager Connection", doc.get("connection"), "property")


def _source(doc, method) -> tuple[str, str | None, str | None, str] | None:
	"""(property, date_from, date_to, kinds) a document's change can move, or
	None when it moves nothing ARI."""
	before = doc.get_doc_before_save() if method == "on_update" else None
	dt = doc.doctype
	if dt == "Season":
		w = _window(_dates(doc.start_date, before and before.start_date),
		            _dates(doc.end_date, before and before.end_date))
		return (doc.property, *w, "restrictions") if w else None
	if dt in _HORIZON_SOURCES:
		return doc.property, None, None, "restrictions"
	if dt == "Property":
		if not before or not any(doc.has_value_changed(f) for f in _PROPERTY_BASIS):
			return None
		return doc.name, None, None, "restrictions"
	if dt == "Room":
		if method == "on_update":
			if not before:
				return None
			out_of_order = (before.housekeeping_status == "Out of Order") != (
				doc.housekeeping_status == "Out of Order")
			if not out_of_order and before.room_type == doc.room_type:
				return None
		return doc.property, None, None, "availability"
	if dt == "Room Block":
		w = _window(_dates(doc.from_date, before and before.from_date),
		            _dates(doc.to_date, before and before.to_date))
		return (doc.property, *w, "availability") if w else None
	if dt == "Channel Room Mapping":
		return _mapping_property(doc), None, None, "both"
	return None


def _forget_mapping(doc, before=None) -> None:
	"""A mapping removed (or re-pointed, or now selling in another currency)
	leaves its provider ids unknown: what was sent for them is forgotten, so
	a new mapping sends them whole. The room id's availability stays while
	another mapping still uses it."""
	old = before or doc
	# a provider rate plan that now sells in another currency: what it was
	# sent in the old one is not comparable
	recurrency = bool(before) and (before.get("external_currency") or None) != (doc.get("external_currency") or None)
	if before and not recurrency and before.external_rate_id == doc.external_rate_id \
			and before.external_room_id == doc.external_room_id:
		return
	if not before or recurrency or before.external_rate_id != doc.external_rate_id:
		_forget(old.connection, "restrictions", old.external_rate_id)
	if not before or before.external_room_id != doc.external_room_id:
		still = frappe.db.exists("Channel Room Mapping", {"connection": old.connection,
			"external_room_id": old.external_room_id, "name": ("!=", doc.name)})
		if not still:
			_forget(old.connection, "availability", old.external_room_id)


def _fx_source_connections(doc, before=None) -> dict[str, set]:
	"""{property: {connection}} whose prices a selling Currency Exchange row
	moves (one that is, or was, for selling): the connections that sell a
	plan priced in that currency in the property's own currency (E == H). A
	plan sold in its contract currency does not move with the rate."""
	from kamra.channel_manager import _mappings
	from kamra.pricing import property_currency, rate_plan_currency, resolve_rate_plan_chain
	if not (cint(doc.get("for_selling")) or cint(before.get("for_selling") if before else 0)):
		return {}
	currency, home_only = doc.get("from_currency"), doc.get("to_currency")
	connections = outbox_connections()
	out: dict[str, set] = {}
	for c in connections:
		home = property_currency(c.property)
		if not currency or (home_only and home_only != home):
			continue
		for m in _mappings(c.name):
			if m.rate_plan and m.get("external_currency") == home \
					and rate_plan_currency(resolve_rate_plan_chain(m.rate_plan)) == currency:
				out.setdefault(c.property, set()).add(c.name)
				break
	return out


def on_ari_source_change(doc, method=None) -> None:
	"""doc_events (on_update, after_insert, on_trash) of what moves
	availability or price: queue its window. Never blocks the save."""
	try:
		if method == "on_update" and doc.flags.in_insert:
			return  # after_insert has it
		if doc.doctype in _FX_SOURCES:
			before = doc.get_doc_before_save() if method == "on_update" else None
			for property, connections in _fx_source_connections(doc, before).items():
				enqueue_ari_change(property, kinds="restrictions", reason=f"{doc.doctype} {doc.name}",
				                   connections=connections)
			return
		if doc.doctype == "Channel Room Mapping":
			before = doc.get_doc_before_save() if method == "on_update" else None
			if method == "on_trash" or before:
				_forget_mapping(doc, before)
		spec = _source(doc, method)
		if spec and spec[0]:
			enqueue_ari_change(spec[0], spec[1], spec[2], kinds=spec[3], reason=f"{doc.doctype} {doc.name}")
	except Exception as exc:
		frappe.log_error(title="Channex ARI enqueue",
		                 message=f"{doc.doctype} {doc.name} · {type(exc).__name__}: {exc}"[:1000])


def queue_full_sync(conn) -> None:
	"""Everything, today .. today + ARI_HORIZON_DAYS, in one call per kind."""
	if not conn.active:
		frappe.throw(_("This connection is not active."))
	if not _supports_outbox(conn.provider):
		frappe.throw(_("{0} does not take ARI changes.").format(conn.provider))
	if not frappe.db.exists("Channel Room Mapping", {"connection": conn.name}):
		frappe.throw(_("No room mappings yet - map your room types to the provider's room and rate ids first."))
	today = nowdate()
	_queue(conn.name, conn.property, today, add_days(today, ARI_HORIZON_DAYS - 1), "both", "Full sync", True)
	frappe.db.set_value("Channel Manager Connection", conn.name, "last_full_sync",
	                    frappe.utils.now_datetime(), update_modified=False)



@frappe.whitelist()
@require_roles("Revenue Manager", "Hotel Admin")
def request_full_sync(connection: str, confirm: int = 0) -> dict:
	"""The full sync button: {"queued": True}, or {"needs_confirm": True,
	"last_full_sync"} when the last one is less than 24 h old and confirm is
	not set (Channex admits one a day)."""
	conn = frappe.get_doc("Channel Manager Connection", connection)
	last = conn.last_full_sync
	if last and not int(confirm or 0):
		age = (frappe.utils.now_datetime() - frappe.utils.get_datetime(last)).total_seconds()
		if age < 86400:
			return {"needs_confirm": True, "last_full_sync": str(last)}
	queue_full_sync(conn)
	return {"queued": True}


@frappe.whitelist()
@require_roles("Revenue Manager", "Hotel Admin", "Front Desk")
def outbox_status(connection: str) -> dict:
	"""The connection panel: whether the connection goes through the outbox,
	rows waiting and failed, pauses per kind (s) and the last push and full
	sync."""
	conn = frappe.get_doc("Channel Manager Connection", connection)
	now = _now()
	paused = {}
	for kind in KINDS:
		until = frappe.cache.get_value(_ARI_PAUSE.format(connection=connection, kind=kind))
		if until and float(until) > now:
			paused[kind] = math.ceil(float(until) - now)
	count = lambda status: frappe.db.count("Channel ARI Outbox", {"connection": connection, "status": status})  # noqa: E731
	return {"outbox": _supports_outbox(conn.provider),
	        "pending": count("Pending") + count("Sending"), "failed": count("Failed"), "paused": paused,
	        "last_push": str(conn.last_push or ""), "last_push_status": conn.last_push_status or "",
	        "last_full_sync": str(conn.last_full_sync or "")}


def nightly_sweep() -> None:
	"""Daily, before dawn: per connection, one row (not a full sync) for today
	.. today + ARI_HORIZON_DAYS. Only differences go out: the day that just
	entered the horizon, whatever a trigger missed, what failed. Rows done
	more than 30 days ago are deleted."""
	today = nowdate()
	for c in outbox_connections():
		_queue(c.name, c.property, today, add_days(today, ARI_HORIZON_DAYS - 1), "both", "Nightly sweep", False)
	cutoff = add_days(frappe.utils.now_datetime(), -30)
	frappe.db.delete("Channel ARI Outbox", {"status": "Sent", "sent_at": ("<", cutoff)})
	frappe.db.delete("Channel ARI Outbox", {"status": "Failed", "modified": ("<", cutoff)})
