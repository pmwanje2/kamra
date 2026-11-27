"""Channel-manager core: what gets synced and how OTA bookings land.

Providers (kamra/channels/*) translate protocol; everything with
consequences lives here so an OTA booking obeys exactly the rules a
front-desk booking does - capacity, double-booking guard, pricing on
our side of the ledger, the audit log.

Sync model: availability + rates per (room type, day) pushed to the
provider on demand, on a schedule (hourly cron), and after any booking
lands. Bookings arrive on one webhook per connection; modifications
and cancellations match on the OTA's own reference (Reservation.ota_ref).
"""

from __future__ import annotations

import hmac
import json
import re
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, getdate, nowdate

from kamra.branding import product_name
from kamra.authz import require_roles
from kamra.channels import provider_for


# The sites a booking can arrive from. Channel managers spell them their own
# way ("BookingCom", "booking"); one name per site keeps reports and badges
# together whether the booking came in by API or was keyed from the email.
OTA_CHANNELS = ["Booking.com", "Agoda", "Airbnb", "Expedia", "MakeMyTrip",
                "Goibibo"]
_CHANNEL_ALIASES = {"booking": "Booking.com", "mmt": "MakeMyTrip"}


def canonical_channel(name: str | None) -> str | None:
	"""Booking.com for "BookingCom" / "booking.com" / "booking"; any other
	site is kept as typed."""
	name = (name or "").strip()
	if not name:
		return None
	key = re.sub(r"[^a-z0-9]", "", name.lower())
	known = {re.sub(r"[^a-z0-9]", "", c.lower()): c for c in OTA_CHANNELS}
	return known.get(key) or _CHANNEL_ALIASES.get(key) or name


def channel_commission_pcts() -> dict[str, float]:
	"""Each OTA's default commission %: its Travel Agent record of type OTA
	(Settings > Travel Agents), keyed by the site's one name."""
	return {canonical_channel(a.agent_name): float(a.commission_pct or 0)
	        for a in frappe.get_all(
	            "Travel Agent", filters={"agent_type": "OTA", "disabled": 0},
	            fields=["agent_name", "commission_pct"])}


def set_ota_commission(doc, pct) -> None:
	"""Record the OTA's commission on an inserted booking: `pct` of its
	pre-tax price (the base a travel agent's commission uses). A travel
	agent's own commission wins."""
	if doc.get("travel_agent") or not float(pct or 0):
		return
	base = (float(doc.amount_before_tax or 0)
	        or sum(float(n.rate or 0) for n in doc.night_rates))
	doc.db_set("commission_amount", round(base * float(pct) / 100, 2))


def _connections(property: str | None = None, only_active=True):
	filters = {}
	if property:
		filters["property"] = property
	if only_active:
		filters["active"] = 1
	return frappe.get_all("Channel Manager Connection", filters=filters,
	                      pluck="name")


def _mappings(connection: str) -> list[dict]:
	return frappe.get_all(
		"Channel Room Mapping", filters={"connection": connection},
		fields=["room_type", "external_room_id", "external_rate_id", "rate_plan", "external_currency"])


def _sellable(property: str, room_type: str, check_in, check_out) -> int:
	"""Rooms of a type actually sellable for [check_in, check_out): physical
	rooms (not out of order) minus every overlapping confirmed/checked-in
	reservation of that type - ASSIGNED OR NOT - minus unpicked group holds,
	and nothing at all while the villa <-> room lockout applies.

	Counting unassigned reservations is the point: an OTA booking arrives
	without a room number, and it must still lower what we offer the OTAs, or
	the same room sells twice. (The front-desk availability query is a separate
	path and is unchanged.)"""
	from frappe.utils import date_diff
	from kamra.kamra.doctype.reservation.reservation import villa_lockout_conflict
	if villa_lockout_conflict(property, room_type, check_in, check_out):
		return 0
	dates = [str(add_days(check_in, i)) for i in range(max(1, date_diff(check_out, check_in)))]
	return min(_distribution_counts(property, room_type, dates).values())


def _distribution_counts(property, room_type, dates):
	from kamra.rooms import nightly_inventory_position
	return {d: max(0, min(p["limit"] - p["sold"] - p["hold"], p["house_free"]))
	        for d, p in nightly_inventory_position(property, room_type, dates, for_distribution=True).items()}


def ari_snapshot(property: str, connection: str, days: int = 90, start=None,
                 with_rates: bool = True) -> list[dict]:
	"""Availability + rate per mapped room type per day, in the seam's
	normalized shape. Availability comes from the same engine the
	double-booking guard uses; the rate is the pricing engine's one-night
	sell rate (base occupancy, demand premiums and hurdle floors applied).

	`start` (default today) is the first day. with_rates=False leaves the
	rate out (None) and gives one row per room type and provider room id,
	however many rate plans are mapped to it: the outbox prices each mapped
	plan on its own (kamra.channel_ari)."""
	from kamra.kamra.doctype.reservation.reservation import villa_locked_nights

	mappings = _mappings(connection)
	if not with_rates:
		seen = set()
		mappings = [m for m in mappings
		            if (m.room_type, m.external_room_id) not in seen
		            and not seen.add((m.room_type, m.external_room_id))]
	# room_category + physical room count per mapped room type - needed by the
	# villa lockout to know which rows are the whole-property bundle and when a
	# room type is fully free.
	meta = {m.room_type: {
		"category": frappe.db.get_value("Room Type", m.room_type,
		                                "room_category"),
		"total": frappe.db.count("Room", {"room_type": m.room_type}),
	} for m in mappings}

	out = []
	start = str(start or nowdate())
	end = add_days(start, days)
	from kamra.restrictions import _fetch_rows_for_window, resolve_night_restrictions
	restr_rows = (
		_fetch_rows_for_window(property, start, add_days(start, max(days - 1, 0)))
		if with_rates else {}
	)

	def rate(room_type, d, d2):
		return _one_night_rate(property, room_type, d, d2) if with_rates else None

	def day_entry(m_row, d, avail, night_rate):
		item = {"date": str(d), "available": avail, "rate": night_rate}
		if with_rates:
			r = (
				resolve_night_restrictions(
					property, d, room_type=m_row.room_type,
					rate_plan=m_row.get("rate_plan") or None, _rows_by_date=restr_rows,
				)
				if (restr_rows and str(d) in restr_rows) else None
			)
			if r and r["has_rule"]:
				item.update(
					stop_sell=r["stop_sell"],
					min_los=r["min_los"],
					max_los=r["max_los"],
					cta=r["cta"],
					ctd=r["ctd"],
				)
				if r.get("is_stay_through"):
					item["is_stay_through"] = True
			else:
				item.update(
					stop_sell=False,
					min_los=1,
					max_los=0,
					cta=False,
					ctd=False,
				)
		return item

	dates = [str(add_days(start, i)) for i in range(days)]
	by_type = {}
	for m in mappings:
		row = {"room_type": m.room_type,
		       "external_room_id": m.external_room_id,
		       "external_rate_id": m.external_rate_id,
		       "external_currency": m.get("external_currency"),
		       "days": []}
		if m.room_type not in by_type and dates:
			by_type[m.room_type] = _distribution_counts(property, m.room_type, dates)
		counts = by_type.get(m.room_type, {})
		# nights the villa <-> room lockout takes this type off sale: a
		# booked villa zeroes its member rooms, a booked room the villa
		locked = villa_locked_nights(property, m.room_type, start, end)
		for i in range(days):
			d = add_days(start, i)
			d2 = add_days(d, 1)
			if str(d) in locked:
				row["days"].append(day_entry(m, d, 0, rate(m.room_type, d, d2)))
				continue
			avail = counts[str(d)]
			row["days"].append(day_entry(m, d, avail, rate(m.room_type, d, d2)))
		out.append(row)
	_apply_villa_lockout(out, meta)
	return out


def _one_night_rate(property: str, room_type: str, d, d2) -> float:
	"""The pricing engine's one-night sell rate (2 adults), as the channels
	sell it (channel_ari.channel_rate: gross where the property's rates
	include tax), 0 when the night cannot be quoted - availability still
	goes out for that day."""
	from kamra.channel_ari import channel_rate
	from kamra.pricing import quote
	try:
		q = quote(property, room_type, str(d), str(d2), 2, 0)
		return float(channel_rate(property, q))
	except Exception:
		return 0.0


def _apply_villa_lockout(rows: list[dict], meta: dict) -> None:
	"""Cross-check mapped offers after computing each type's physical inventory.

	Villa counts already include physical exclusions and group holds on
	unmapped members through _distribution_counts. This cross-check also respects mapped sell limits.

	An Entire-Property (Villa-category) unit and its member rooms cannot both
	sell the same night, so per day:
	  - a member room is booked  -> the villa pushes 0 available
	  - the villa is booked       -> every member room pushes 0 available

	This complements - it does NOT replace - the write-time guard in
	Reservation.validate_villa_lockout; that one stops Kamra accepting a
	conflicting booking, this one stops OTAs ever showing it. A property with
	no Villa-category room type mapped is a no-op (unchanged behavior)."""
	villa_rts = [rt for rt, mm in meta.items() if mm["category"] == "Villa"]
	member_rts = [rt for rt, mm in meta.items() if mm["category"] != "Villa"]
	if not villa_rts or not member_rts:
		return
	by_rt = {r["room_type"]: r for r in rows}
	ndays = len(rows[0]["days"]) if rows else 0
	for i in range(ndays):
		villa_sold = any(
			by_rt[rt]["days"][i]["available"] < (meta[rt]["total"] or 0)
			for rt in villa_rts if rt in by_rt)
		members_all_free = all(
			by_rt[rt]["days"][i]["available"] >= (meta[rt]["total"] or 0)
			for rt in member_rts if rt in by_rt)
		if not members_all_free:
			for rt in villa_rts:
				if rt in by_rt:
					by_rt[rt]["days"][i]["available"] = 0
		if villa_sold:
			for rt in member_rts:
				if rt in by_rt:
					by_rt[rt]["days"][i]["available"] = 0


@frappe.whitelist()
@require_roles("Revenue Manager", "Hotel Admin", "Kamra Agent")
def push_ari(connection: str, days: int | None = None):
	"""Push availability + rates for one connection, now. A connection whose
	adapter takes changes (Channex) queues a full sync on its outbox instead:
	one call per kind, through the same cap and retries (kamra.channel_ari)."""
	conn = frappe.get_doc("Channel Manager Connection", connection)
	if not conn.active:
		frappe.throw("This connection is not active.")
	from kamra import channel_ari
	if channel_ari.uses_outbox(connection):
		channel_ari.queue_full_sync(conn)
		return {"ok": True, "queued": True, "detail": "full sync queued"}
	snapshot = ari_snapshot(conn.property, connection,
	                        int(days or conn.sync_days or 90))
	if not snapshot:
		frappe.throw("No room mappings yet - map your room types to the "
		             "provider's room/rate ids first.")
	ok, detail = provider_for(conn.provider).push_ari(conn, snapshot)
	frappe.db.set_value("Channel Manager Connection", connection, {
		"last_push": frappe.utils.now_datetime(),
		"last_push_status": ("OK: " if ok else "FAILED: ") + detail[:130],
	}, update_modified=False)
	if frappe.get_meta("Channel Room Mapping").has_field("last_synced_at"):
		now_ts = frappe.utils.now_datetime()
		for m in _mappings(connection):
			if ok:
				frappe.db.set_value(
					"Channel Room Mapping", m.name,
					{"last_synced_at": now_ts, "last_error": ""}, update_modified=False,
				)
			else:
				frappe.db.set_value(
					"Channel Room Mapping", m.name,
					{"last_error": detail[:280]}, update_modified=False,
				)
	from kamra.savings import log_action
	log_action("channel.ari_push", "Channel Manager Connection", connection,
	           conn.property,
	           rationale=f"{conn.provider}: {'pushed' if ok else 'failed'} "
	                     f"- {detail[:160]}",
	           channel="API")
	if not ok:
		frappe.throw(f"{conn.provider} rejected the push: {detail}")
	return {"ok": True, "detail": detail}


def push_all_ari(property: str | None = None):
	"""Hourly cron (and the post-change trigger): keep active connections
	fresh. Best-effort per connection - one provider being down never blocks
	the others. Pass `property` to scope the push to one property.

	Connections whose adapter takes changes are skipped: their outbox sends
	what changed, and a full sync only on demand (kamra.channel_ari)."""
	from kamra.channel_ari import uses_outbox
	for name in _connections(property):
		if uses_outbox(name):
			continue
		try:
			push_ari(name)
		except Exception:
			frappe.log_error(title=f"ARI push failed: {name}")


def enqueue_property_push(property: str | None, outbox: bool = True) -> None:
	"""Pipeline 1 trigger: after any availability or rate change, fan the new
	numbers out to this property's OTAs. After-commit so it runs on the
	saved state, and fully swallowed so a channel hiccup can never block or
	fail the booking / rate write that triggered it.

	Connections with an outbox get an availability change over the whole
	horizon (only what moved goes out); `outbox=False` leaves them to a
	caller that queued its own window."""
	from kamra import channel_ari
	try:
		if not property:
			return
		if outbox:
			channel_ari.enqueue_ari_change(property, kinds="availability",
			                               reason="enqueue_property_push")
		if not [n for n in _connections(property) if not channel_ari.uses_outbox(n)]:
			return
		frappe.enqueue("kamra.channel_manager.push_all_ari", queue="long",
		               enqueue_after_commit=True, property=property)
	except Exception:
		frappe.log_error(title="enqueue_property_push failed")


def on_reservation_change(doc, method=None):
	"""doc_event on Reservation: any insert/update/cancel moves availability,
	so refresh the channel side. The lockout + push run through the same
	ari_snapshot as everything else, so a direct front-desk villa/room
	booking tightens OTA inventory exactly like an OTA one does.

	A connection with an outbox gets the booking's own nights, before and
	after the change, for availability and price (occupancy moves price)."""
	from kamra import channel_ari
	window = channel_ari.reservation_window(doc, method)
	if window and getattr(doc, "property", None):
		channel_ari.enqueue_ari_change(doc.property, window[0], window[1], kinds="both",
		                               reason=f"Reservation {doc.name}")
	enqueue_property_push(getattr(doc, "property", None), outbox=False)


# ---------------------------------------------------------------------------
# Inbound bookings
# ---------------------------------------------------------------------------


_SNAPSHOT_CONFLICT_CODES = frozenset({1020, 1205, 1213})
_WEBHOOK_CONFLICT_RETRIES = 3


def _is_snapshot_conflict(exc: Exception) -> bool:
	"""True when MariaDB rejects a locking read or write under snapshot
	isolation (error 1020 "Record has changed since last read") or a transient
	deadlock/lock-wait (1213/1205) that succeeds after rollback on a fresh
	transaction snapshot."""
	if isinstance(exc, frappe.QueryDeadlockError):
		return True
	args = getattr(exc, "args", ())
	if args and isinstance(args[0], int) and args[0] in _SNAPSHOT_CONFLICT_CODES:
		return True
	return "Record has changed since last read" in str(exc)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook(connection: str | None = None, **kwargs):
	"""One URL per connection:
	/api/method/kamra.channel_manager.webhook?connection=CMC-00001
	Authenticated by the connection's webhook secret (X-Webhook-Secret
	header or `secret` in the payload).

	The connection is read from the URL itself: on a JSON post (Channex
	sends one) Frappe builds the arguments from the body alone and drops
	the query string (frappe.app.make_form_dict). The URL's connection
	wins over an argument of the same name.

	Every refusal is the same: an unknown or inactive connection, no
	secret configured, and a wrong or missing secret all get one
	PermissionError, so an unauthenticated caller learns nothing about
	which connections exist."""
	connection = (frappe.request.args.get("connection") if frappe.request else None) \
		or connection
	sent = (frappe.get_request_header("X-Webhook-Secret")
	        if frappe.request else None) or kwargs.get("secret") or ""
	name = _authenticated_connection(connection, sent)
	if not name:
		frappe.throw("Webhook secret mismatch.", frappe.PermissionError)
	conn = frappe.get_doc("Channel Manager Connection", name)

	try:
		payload = json.loads(frappe.request.data or b"{}") \
			if frappe.request else dict(kwargs)
	except Exception:
		payload = dict(kwargs)

	for attempt in range(1, _WEBHOOK_CONFLICT_RETRIES + 1):
		try:
			if attempt > 1:
				conn = frappe.get_doc("Channel Manager Connection", connection)
				if not conn.active:
					return {"ok": False, "reason": "connection_inactive"}
			events = provider_for(conn.provider).parse_webhook(conn, payload)
			results = _apply_webhook_batch(conn, events)
			return {"ok": True, "results": results}
		except Exception as exc:
			if not _is_snapshot_conflict(exc) or attempt >= _WEBHOOK_CONFLICT_RETRIES:
				raise
			frappe.db.rollback()
	return {"ok": True, "results": []}


def _authenticated_connection(connection, sent) -> str | None:
	"""The active connection whose webhook secret `sent` matches, else None.

	`connection` must be a string: with `from __future__ import annotations`
	Frappe skips the type check, and a JSON object would reach the ORM as a
	filter. A name always takes the same two lookups and one constant-time
	compare (bytes, so a non-ASCII secret cannot raise), whether or not the
	connection exists, is active, or has a secret."""
	if not isinstance(connection, str) or not connection:
		return None
	from frappe.utils.password import get_decrypted_password

	row = frappe.db.get_value("Channel Manager Connection", {"name": connection},
	                          ["name", "active"], as_dict=True)
	secret = get_decrypted_password("Channel Manager Connection",
	                                row.name if row else connection, "webhook_secret",
	                                raise_exception=False) or ""
	matches = hmac.compare_digest(str(sent).encode(), secret.encode())
	# no secret configured = nothing to authenticate against: refuse
	return row.name if row and row.active and secret and matches else None


def _room_type_for(connection: str, external_id: str) -> str | None:
	return frappe.db.get_value(
		"Channel Room Mapping",
		{"connection": connection, "external_room_id": external_id},
		"room_type")


def _mapping_for(connection: str, external_room_id: str | None,
                 external_rate_id: str | None = None) -> tuple[str | None, str | None]:
	"""Resolve (room_type, mapped_rate_plan) for an inbound channel room+rate."""
	if not external_room_id:
		return None, None
	if external_rate_id:
		exact = frappe.db.get_value(
			"Channel Room Mapping",
			{"connection": connection, "external_room_id": external_room_id,
			 "external_rate_id": external_rate_id},
			["room_type", "rate_plan"],
			as_dict=True,
		)
		if exact and exact.get("room_type"):
			return exact["room_type"], exact.get("rate_plan") or None
	row = frappe.db.get_value(
		"Channel Room Mapping",
		{"connection": connection, "external_room_id": external_room_id},
		["room_type", "rate_plan"],
		as_dict=True,
	)
	if row and row.get("room_type"):
		return row["room_type"], row.get("rate_plan") or None
	return None, None


def _find_or_create_guest(name: str, phone: str, email: str,
                          property: str | None = None) -> str:
	"""The OTA guest's profile. The phone is read with the property's
	country (#468); one that can't be read is kept as typed and flagged,
	never a reason to drop an already-acknowledged booking."""
	from kamra import phone as phone_mod
	typed = (phone or "").strip()
	phone, problem = phone_mod.for_property(typed, property)
	for filt in ({"phone": phone_mod.matching(typed, property)} if phone else None,
	             {"email": email} if email else None):
		if filt:
			g = frappe.db.get_value("Guest", filt)
			if g:
				return g
	parts = (name or "OTA Guest").split(" ", 1)
	guest = frappe.get_doc({
		"doctype": "Guest", "first_name": parts[0],
		"last_name": parts[1] if len(parts) > 1 else "",
		"phone": phone or "", "email": email or "",
	}).insert(ignore_permissions=True).name
	phone_mod.flag(problem, "Guest", guest, property, "an OTA booking")
	return guest


def _reservations_for_booking(property: str, booking_id: str, *, for_update: bool = False) -> list[dict]:
	"""Every Kamra reservation for one OTA booking: the single-room case
	(ota_ref == bookingId) and each line of a multi-room booking
	(ota_ref == "bookingId-<n>"). A cancel payload carries only the base
	bookingId, so it must reach all of them."""
	if not booking_id:
		return []
	params = {"p": property, "b": booking_id, "bl": booking_id + "-%"}
	if for_update:
		from kamra.kamra.doctype.reservation.reservation import lock_property_for_inventory
		lock_property_for_inventory(property)
		return frappe.db.sql(
			"""SELECT name, status, ota_ref FROM `tabReservation`
			   WHERE property = %(p)s
			     AND (ota_ref = %(b)s OR ota_ref LIKE %(bl)s)
			   FOR UPDATE""",
			params,
			as_dict=True)
	return frappe.db.sql(
		"""SELECT name, status, ota_ref FROM `tabReservation`
		   WHERE property = %(p)s
		     AND (ota_ref = %(b)s OR ota_ref LIKE %(bl)s)""",
		params,
		as_dict=True)


def _locked_live_ota(property: str, ota_ref: str | None):
	"""The stay for this OTA reference, after the property inventory lock.

	A non-cancelled stay wins when a cancelled one shares the reference.
	The locking read sees a delivery that committed while this one waited.
	"""
	if not ota_ref:
		return None
	from kamra.kamra.doctype.reservation.reservation import lock_property_for_inventory
	lock_property_for_inventory(property)
	rows = frappe.db.sql(
		"""SELECT name, status FROM `tabReservation`
		   WHERE property = %s AND ota_ref = %s
		   ORDER BY CASE WHEN status = 'Cancelled' THEN 1 ELSE 0 END, creation
		   FOR UPDATE""",
		(property, ota_ref), as_dict=True)
	return rows[0] if rows else None


def ota_key(name: str | None) -> str:
	"""One OTA however it is written: its site name (canonical_channel),
	case folded, letters and digits only ("Hostelworld" is "hostelworld")."""
	return re.sub(r"[\W_]+", "", (canonical_channel(name) or "").casefold())


def ota_amounts(conn, channel: str | None) -> str | None:
	"""What an OTA's booking amounts are on this connection ("Including tax"
	or "Excluding tax": Channel Manager Connection > OTA tax basis, as set
	for the OTA on the channel manager); None when it is not listed."""
	key = ota_key(channel)
	for row in conn.get("ota_tax_basis") or []:
		if key and ota_key(row.get("channel")) == key:
			return row.get("amounts")
	return None


# How a hold for a tax that cannot be read starts its note: a later modify
# that reads releases it (an overbooking hold is staff's to resolve).
RULES_MISSING = "OTA tax rules missing — resolve"
TAX_UNREAD = "OTA tax not recognised — resolve"
ROOM_TAX_MISSING = "OTA room tax missing — resolve"
_TAX_HOLDS = (RULES_MISSING, TAX_UNREAD, ROOM_TAX_MISSING)


def _held_amount(conn, e: dict) -> str:
	"""The booking's amount and OTA as a hold note gives them."""
	currency = e.get("currency") or frappe.db.get_value("Property", conn.property, "currency") or ""
	return f"{e.get('total') or 0} {currency} from {e.get('channel') or 'OTA'}"


def room_tax_missing(conn, e: dict, room_type: str) -> str | None:
	"""Why a booking is held for review when its OTA reports amounts before
	tax (ota_amounts "Excluding tax") but it lists no room tax owed
	(room_tax_listed false): its price is then before tax, and the tax the
	guest paid on top is not there to bill. None otherwise - an OTA not
	listed, a booking that lists its room tax, or a room the property
	charges no tax on."""
	if e.get("room_tax_listed") is not False or ota_amounts(conn, e.get("channel")) != "Excluding tax":
		return None
	from kamra.pricing import room_gst_rate
	rt = frappe.get_cached_doc("Room Type", room_type)
	amounts = [float(n.get("amount") or 0) for n in e.get("nightly") or []] or [float(e.get("total") or 0)]
	if not any(room_gst_rate(conn.property, rt, Decimal(str(a))) > 0 for a in amounts):
		return None
	return (f"{ROOM_TAX_MISSING}: {_held_amount(conn, e)}, whose amounts are "
	        "before tax, with no room tax listed")


def tax_hold(conn, e: dict, room_type: str) -> str | None:
	"""Why a booking's taxes cannot be read into what the folio bills, for
	staff to resolve; None when they can. Fail closed: a connection with no
	OTA tax rules (tax_rules false) reads nothing; a tax listed on top of
	the price that no rule reads (unread_taxes) was paid by the guest and
	is not in the price; and an OTA whose amounts are before tax that lists
	no room tax (room_tax_missing). The booking is then held as Requested,
	billed nothing, never confirmed at a price the guest did not pay."""
	if e.get("tax_rules") is False:
		return (f"{RULES_MISSING}: {_held_amount(conn, e)}; the connection "
		        "has no OTA tax rules to read its taxes by")
	if e.get("unread_taxes"):
		return (f"{TAX_UNREAD}: {_held_amount(conn, e)} lists "
		        f"{', '.join(e['unread_taxes'])} on top of the price, which no OTA tax rule reads")
	return room_tax_missing(conn, e, room_type)


# Stays the guest has arrived to or that have closed: an OTA modify or cancel
# never rewrites financials, room, dates or status (OPERA Cloud OXI
# UPDATE INHOUSE RES = N, closed-stay immutability, and cancellation prior
# to check-in).
_ARRIVED = ("Checked In", "Checked Out")
_LOCKED_MODIFY = ("Checked In", "Checked Out", "No Show", "Cancelled")

# Separate reason codes for Reservation.channel_review_reason so the UI and
# callers distinguish #46 unapplied holds from applied pre-arrival OTA updates
# that removed or partially preserved a desk discount.
CHANNEL_REVIEW_NOT_APPLIED = "update_not_applied"
CHANNEL_REVIEW_DISCOUNT_REMOVED = "discount_removed"
CHANNEL_REVIEW_DISCOUNT_PARTIAL = "discount_review"
CHANNEL_REVIEW_RESTRICTION = "restriction_mismatch"
_REVIEW_NOTE_PREFIXES = ("Channel modify (",)


def _strict_ota_restrictions(property: str) -> bool:
	"""True when `property` explicitly opts into rejecting inbound OTA bookings
	that breach active stay restrictions (default False: honor guest + flag for review)."""
	if not property:
		return False
	raw = frappe.db.get_value(
		"Property Setting",
		{"property": property, "setting_key": "strict_ota_restrictions"},
		"value",
	)
	if raw is None or raw == "":
		return False
	text = str(raw).strip().lower()
	return text in ("1", "true", "yes", "on")


def _review_note_lines(notes: str | None) -> list[str]:
	"""Channel review lines in special_requests that subsequent OTA modifies
	must preserve (never overwritten by new or empty guest notes)."""
	lines = [line.strip() for line in (notes or "").splitlines() if line.strip()]
	return [
		line for line in lines
		if any(line.startswith(p) for p in _REVIEW_NOTE_PREFIXES)
		or ("— resolve" in line and not any(h in line for h in _TAX_HOLDS))
	]


def _non_tax_hold_lines(notes: str | None) -> list[str]:
	"""Hold-reason lines in special_requests that are not tax holds
	(e.g. "OTA overbooking — resolve" or a currency mismatch hold)."""
	lines = [line.strip() for line in (notes or "").splitlines() if line.strip()]
	return [line for line in lines
	        if "— resolve" in line and not any(h in line for h in _TAX_HOLDS)]


def _kept_hold_notes(existing_notes: str | None, new_notes: str | None) -> str | None:
	"""Preserve a non-tax Requested hold note (and any prior channel review
	notes) and append the OTA's notes without duplicating lines."""
	kept = _review_note_lines(existing_notes)
	if not kept and (existing_notes or "").strip():
		kept = [line.strip() for line in (existing_notes or "").splitlines()
		        if line.strip() and not any(h in line for h in _TAX_HOLDS)]
	out = list(kept)
	for line in (new_notes or "").splitlines():
		s = line.strip()
		if s and s not in out:
			out.append(s)
	return "\n".join(out) or None


def _merge_review_notes(existing_notes: str | None, new_notes: str | None) -> str | None:
	"""Preserve prior channel review notes across consecutive OTA modifies
	while updating the guest's own note."""
	kept = _review_note_lines(existing_notes)
	if not kept:
		return new_notes or None
	out = list(kept)
	for line in (new_notes or "").splitlines():
		s = line.strip()
		if s and s not in out:
			out.append(s)
	return "\n".join(out) or None


def _describe_requested_modify(conn, e: dict, room_type: str | None) -> str:
	"""Human summary of a readable OTA modify that could not be applied
	automatically because the stay is already in-house or closed."""
	parts = []
	if e.get("check_in") or e.get("check_out"):
		parts.append(f"{e.get('check_in') or '?'} to {e.get('check_out') or '?'}")
	rt_label = (frappe.db.get_value("Room Type", room_type, "room_type_name")
	            or room_type) if room_type else e.get("room_type_external_id")
	if rt_label:
		parts.append(str(rt_label))
	if e.get("total") is not None:
		parts.append(_held_amount(conn, e))
	else:
		parts.append(f"from {e.get('channel') or 'OTA'}")
	summary = ", ".join(parts)
	guest_note = (e.get("notes") or "").strip()
	if guest_note:
		summary = f"{summary}; note: {guest_note}"
	return f"OTA modify — resolve: {summary}"


def _describe_superseded_discount(doc) -> str:
	"""Plain, white-label description of the desk discount(s) on doc before a
	channel modify supersedes them (stay-level and any Daily Details nights)."""
	from frappe.utils import flt

	from kamra.localization import money_text
	from kamra.pricing import property_currency

	home = property_currency(doc.property)

	def fmt_val(kind: str, val, disc_cur: str | None = None) -> str:
		if kind == "Percent":
			return f"{flt(val):g}% off"
		cur = (disc_cur or "").strip().upper() or home
		return f"{money_text(doc.property, val, currency=cur)} off/night"

	parts = []
	if doc.get("discount_type") and flt(doc.get("discount_value")) > 0:
		reason = doc.get("discount_reason") or "no reason"
		parts.append(f"{fmt_val(doc.discount_type, doc.discount_value, doc.get('discount_currency'))} ({reason})")
	for r in doc.get("night_rates") or []:
		if r.get("discount_type") and flt(r.get("discount_value")) > 0:
			reason = r.get("discount_reason") or doc.get("discount_reason") or "no reason"
			row_cur = r.get("discount_currency") or doc.get("discount_currency")
			parts.append(f"{str(r.stay_date)}: {fmt_val(r.discount_type, r.discount_value, row_cur)} ({reason})")
	if not parts:
		reason = doc.get("discount_reason") or "no reason"
		parts.append(f"desk discount ({reason})")
	return "; ".join(parts)


def _channel_discount_needs_approval(doc, rows, dates) -> bool:
	"""True when applying the discount on `dates` would exceed standard desk
	authority (above limit, 100% comp, under floor, supervisor reason, or
	non-discountable plan without a held manager override). Evaluated without
	manager privilege so an inbound OTA webhook never inherits Administrator
	authority or escalates from Guest."""
	if not dates:
		return False
	from frappe.utils import flt
	from kamra.api import _repriced_nights, discount_governance

	nights = _repriced_nights(doc, rows, dates)
	if not nights:
		return False
	discounted = [n for n in nights if n.get("discount_type") and flt(n.get("discount_value")) > 0]
	if not discounted:
		return False
	gov = discount_governance(
		doc.property, doc.room_type, doc.rate_plan, nights,
		reason_code=doc.get("discount_reason"),
		fixed_held=doc.holds_manager_fixed_rate(),
		check=False,
	)
	desk_needs = (
		flt(gov["max_pct"]) > flt(gov["limit_pct"]) + 0.005
		or bool(gov["floor_breach"])
		or bool(gov["reason_requires_supervisor"])
	)
	fixed_held = doc.holds_manager_fixed_rate()
	return bool(
		desk_needs
		or gov["comp"]
		or (not gov["eligible"] and not fixed_held)
		or (gov["fixed_rate"] and not fixed_held)
		or not gov["reason_ok"]
	)


def _reconcile_ota_modify_discount(doc, old) -> dict | None:
	"""When an auto-priced pre-arrival OTA modify changes a discounted stay,
	keep the approved discount on new/changed nights only where the existing
	approval covers them. Otherwise preserve the approved discount on surviving
	originally-approved nights as Daily Details, price added or uncovered
	nights without the unapproved discount, and return review metadata so the
	desk is alerted without crashing or escalating Guest privilege."""
	from frappe.utils import cint, flt, getdate

	if not cint(doc.get("auto_price")) or not doc.has_discount():
		return None
	from kamra.api import _discount_snapshot
	from kamra.pricing import effective_rate_currency, property_currency, saved_contract_tariff

	home_cur = property_currency(doc.property)
	old_rate_cur = effective_rate_currency(
		old.property, contract_tariff=saved_contract_tariff(old), rate_plan=old.get("rate_plan"),
	)
	new_rate_cur = effective_rate_currency(
		doc.property, contract_tariff=saved_contract_tariff(doc), rate_plan=doc.get("rate_plan"),
	)
	allowed_new_curs = {c for c in (home_cur, new_rate_cur) if c}

	def _has_incompatible_flat_currency() -> bool:
		if doc.get("discount_type") == "Amount" and flt(doc.get("discount_value")) > 0:
			stay_c = (doc.get("discount_currency") or home_cur or "").strip().upper()
			if stay_c and allowed_new_curs and stay_c not in allowed_new_curs:
				return True
		for r in doc.get("night_rates") or []:
			if r.get("discount_type") == "Amount" and flt(r.get("discount_value")) > 0:
				row_c = (r.get("discount_currency") or doc.get("discount_currency") or home_cur or "").strip().upper()
				if row_c and allowed_new_curs and row_c not in allowed_new_curs:
					return True
		return False

	currency_mismatch = old_rate_cur != new_rate_cur and _has_incompatible_flat_currency()
	if (
		not currency_mismatch
		and old.get("night_rates")
		and cint(old.get("auto_price"))
		and doc.get("rate_lock_key")
		and doc.get("rate_lock_key") == old.get("rate_lock_key")
		and doc.get("rate_lock_key") == doc.rate_lock_key_for(doc.quote_inputs())
	):
		return None

	doc.flags.kamra_stored = old
	try:
		if currency_mismatch:
			before = {**_discount_snapshot(old), "amount_after_tax": flt(old.amount_after_tax)}
			before_desc = _describe_superseded_discount(old)
			doc.discount_type = None
			doc.discount_value = 0
			doc.discount_currency = None
			doc.discount_reason = None
			doc.fixed_rate = 0
			doc.flags.kamra_reservation_discount = True
			for r in doc.get("night_rates") or []:
				r.discount_type = None
				r.discount_value = 0
				r.discount_currency = None
				r.discount_reason = None
				r.fixed_rate = 0
			old_by_date = {str(getdate(r.stay_date)): r for r in old.get("night_rates") or []}
			added_dates = [day for day in doc.stay_nights() if day not in old_by_date]
			return {
				"kind": CHANNEL_REVIEW_DISCOUNT_REMOVED,
				"action_type": "channel.modify_discount_removed",
				"before": before,
				"desc": before_desc,
				"kept": [],
				"added": added_dates,
				"dropped": sorted(old_by_date),
			}

		q = doc.requote()
		rows = doc.quote_night_rows(q)
		judged = doc.judged_nights(old, rows)
		old_by_date = {str(getdate(r.stay_date)): r for r in old.get("night_rates") or []}
		new_stay_nights = doc.stay_nights()
		if not _channel_discount_needs_approval(doc, rows, judged):
			row_by_date = {str(getdate(r["stay_date"])): r for r in rows}

			def _has_row_disc(r) -> bool:
				return bool(
					r and (
						flt(r.get("discount_amount")) > 0
						or (r.get("discount_type") and flt(r.get("discount_value")) > 0)
					)
				)

			kept_dates = [day for day in new_stay_nights if _has_row_disc(row_by_date.get(day))]
			added_uncovered = [
				day for day in new_stay_nights
				if day not in old_by_date and not _has_row_disc(row_by_date.get(day))
			]
			if not added_uncovered:
				return None
			before = {**_discount_snapshot(old), "amount_after_tax": flt(old.amount_after_tax)}
			before_desc = _describe_superseded_discount(old)
			if kept_dates:
				return {
					"kind": CHANNEL_REVIEW_DISCOUNT_PARTIAL,
					"action_type": "channel.modify_discount_review",
					"before": before,
					"desc": before_desc,
					"kept": sorted(kept_dates),
					"added": added_uncovered,
				}
			return {
				"kind": CHANNEL_REVIEW_DISCOUNT_REMOVED,
				"action_type": "channel.modify_discount_removed",
				"before": before,
				"desc": before_desc,
				"kept": [],
				"added": added_uncovered,
			}

		before = {**_discount_snapshot(old), "amount_after_tax": flt(old.amount_after_tax)}
		before_desc = _describe_superseded_discount(old)

		same_product = (
			doc.room_type == old.room_type
			and (doc.get("rate_plan") or None) == (old.get("rate_plan") or None)
			and old_rate_cur == new_rate_cur
		)
		old_stay_type = old.get("discount_type") or None
		old_stay_val = flt(old.get("discount_value"))
		old_stay_cur = old.get("discount_currency") or None
		old_stay_reason = old.get("discount_reason") or None
		old_stay_fixed = cint(old.get("fixed_rate"))
		new_stay_set = set(new_stay_nights)

		def _approved_for(day: str):
			if not same_product or day not in old_by_date:
				return None
			r = old_by_date[day]
			if r.get("discount_type") and flt(r.get("discount_value")) > 0:
				return (
					r.discount_type,
					flt(r.discount_value),
					r.get("discount_currency") or old_stay_cur,
					r.get("discount_reason") or old_stay_reason,
					cint(r.get("fixed_rate")) or old_stay_fixed,
				)
			if old_stay_type and old_stay_val > 0:
				return (old_stay_type, old_stay_val, old_stay_cur, old_stay_reason, old_stay_fixed)
			return None

		# Clear stay-level discount so added/uncovered nights never inherit it;
		# pin each surviving originally-approved night's discount on its row.
		doc.discount_type = None
		doc.discount_value = 0
		doc.discount_currency = None
		doc.discount_reason = None
		doc.fixed_rate = 0
		doc.flags.kamra_reservation_discount = True

		kept_dates = []
		for r in doc.get("night_rates") or []:
			day = str(getdate(r.stay_date))
			approved = _approved_for(day) if day in new_stay_set else None
			if approved:
				r.discount_type, r.discount_value, r.discount_currency, r.discount_reason, r.fixed_rate = approved
				kept_dates.append(day)
			else:
				r.discount_type = None
				r.discount_value = 0
				r.discount_currency = None
				r.discount_reason = None
				r.fixed_rate = 0

		# Re-check: if any surviving night itself moved price (e.g., party change
		# or rate-card shift) and would require fresh supervisor sign-off, drop
		# its unapproved discount too so Guest save never crashes or escalates.
		dropped_dates = []
		if kept_dates:
			q2 = doc.requote()
			rows2 = doc.quote_night_rows(q2)
			judged2 = doc.judged_nights(old, rows2)
			if judged2 and _channel_discount_needs_approval(doc, rows2, judged2):
				still_kept = []
				for r in doc.get("night_rates") or []:
					day = str(getdate(r.stay_date))
					if day in kept_dates and day in judged2 and _channel_discount_needs_approval(doc, rows2, {day}):
						r.discount_type = None
						r.discount_value = 0
						r.discount_currency = None
						r.discount_reason = None
						r.fixed_rate = 0
						dropped_dates.append(day)
					elif day in kept_dates:
						still_kept.append(day)
				kept_dates = still_kept

		kept_set = set(kept_dates)
		dropped_sorted = sorted(set(dropped_dates))
		added_dates = [day for day in new_stay_nights if day not in old_by_date and day not in kept_set]
		if kept_dates:
			return {
				"kind": CHANNEL_REVIEW_DISCOUNT_PARTIAL,
				"action_type": "channel.modify_discount_review",
				"before": before,
				"desc": before_desc,
				"kept": sorted(kept_set),
				"added": added_dates,
				"dropped": dropped_sorted,
			}
		return {
			"kind": CHANNEL_REVIEW_DISCOUNT_REMOVED,
			"action_type": "channel.modify_discount_removed",
			"before": before,
			"desc": before_desc,
			"kept": [],
			"added": added_dates,
			"dropped": dropped_sorted,
		}
	finally:
		doc.flags.kamra_stored = None


def _rate_plan_has_tariff(plan_name: str | None) -> bool:
	"""True when `plan_name` or an ancestor in its `base_rate_plan` chain carries
	a hospitality-tariffs `custom_tariff_price_list` (mirrors hospitality-tariffs@11
	`tariff_source`, failing closed on cycles or >5-plan chains)."""
	if not plan_name:
		return False
	meta = frappe.get_meta("Rate Plan")
	if not meta.has_field("custom_tariff_price_list"):
		return False
	has_base = meta.has_field("base_rate_plan")
	fields = ["custom_tariff_price_list"] + (["base_rate_plan"] if has_base else [])
	current = plan_name
	seen: set[str] = set()
	while current:
		if current in seen or len(seen) >= 5:
			return True
		seen.add(current)
		row = frappe.db.get_value("Rate Plan", current, fields, as_dict=True)
		if not row:
			return False
		if row.get("custom_tariff_price_list"):
			return True
		current = (row.get("base_rate_plan") or "").strip() if has_base else ""
	return False


def _has_contract_tariff_snapshot(doc) -> bool:
	"""True when `doc.contract_tariff` holds a non-channel contract tariff snapshot
	(mirrors hospitality-tariffs@11 `snapshot_contract`; fails closed on corrupt JSON)."""
	raw = doc.get("contract_tariff")
	if isinstance(raw, str):
		if not raw.strip():
			return False
		try:
			raw = frappe.parse_json(raw)
		except Exception:
			return True
	if not isinstance(raw, dict):
		return False
	terms = raw.get("tariff")
	if not isinstance(terms, dict) or terms.get("source") == "channel":
		return False
	return bool(terms.get("derived_from") or (terms.get("currency") and frappe.utils.flt(terms.get("nightly")) > 0))


def _is_tariff_owned_stay(doc) -> bool:
	"""True when `doc` is a contract-tariff stay guarded by hospitality-tariffs@11:
	directly priced (`custom_tariff_currency`), backed by a non-channel
	`contract_tariff` snapshot (including derived or repointed stays), or on a
	Rate Plan whose `base_rate_plan` chain leads to a tariff price list."""
	return bool(
		doc.get("custom_tariff_currency")
		or _has_contract_tariff_snapshot(doc)
		or _rate_plan_has_tariff(doc.get("rate_plan"))
	)


def _flag_for_review(conn, doc, e: dict, reason: str, *,
                     action: str = "modify",
                     action_type: str = "channel.modify_needs_review",
                     not_applied_why: str | None = None) -> dict:
	"""An OTA modify or cancel on a stay that is in-house, closed, or contract-tariff
	owned, held for front-desk review: the stay is left untouched (status, room, dates,
	nights, amounts, folio), flagged via channel_review_needed, noted on the stay, and
	logged in Agent Action Log - matching OPERA Cloud OXI (UPDATE INHOUSE RES
	= N, WARNINGS AS TRACES = Y, and Message Status)."""
	why = not_applied_why or f"the stay is {doc.status}"
	note = f"{reason} (not applied: {why})"
	updates = {
		"channel_review_needed": 1,
		"channel_review_reason": CHANNEL_REVIEW_NOT_APPLIED,
	}
	if note not in (doc.special_requests or ""):
		updates["special_requests"] = "\n".join(s for s in (doc.special_requests, note) if s)
	doc.db_set(updates)
	from kamra.savings import log_action
	log_action(action_type, "Reservation", doc.name, conn.property,
	           rationale=f"{e.get('channel') or 'OTA'} {action} of {e.get('ota_ref')} via "
	                     f"{conn.provider} not applied: {note}",
	           channel="API")
	return {"ota_ref": e.get("ota_ref"), "result": "held_for_review", "reservation": doc.name}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Revenue Manager", "Hotel Admin", scope={"reservation": "Reservation"})
def clear_channel_review(reservation: str):
	"""Mark an OTA modify/cancel review flag as reviewed on a stay
	(OPERA Cloud OXI Message Status -> Mark As Reviewed), without touching any
	stay or financial fields."""
	res = frappe.get_doc("Reservation", reservation)
	frappe.db.set_value("Reservation", res.name, {
		"channel_review_needed": 0,
		"channel_review_reason": None,
	})
	from kamra.savings import log_action
	log_action("channel.review_cleared", "Reservation", res.name, res.property,
	           rationale=f"Channel review cleared by {frappe.session.user}",
	           channel="Desk")
	return {
		"ok": True,
		"reservation": res.name,
		"channel_review_needed": 0,
		"channel_review_reason": None,
	}


def _apply_event(conn, e: dict, requested_reason: str | None = None) -> dict:
	"""One normalized OTA event -> the same paths a human would take."""
	e["channel"] = canonical_channel(e.get("channel")) or "OTA"

	if e["event"] == "cancel":
		# A cancel frees pre-arrival rooms through the property's cancellation
		# policy. Once a guest has checked in or the stay has closed (Checked In,
		# Checked Out, No Show), OPERA Cloud rejects external cancellations: leave
		# the stay untouched and flag it for front-desk review. Duplicate cancels
		# on an already-Cancelled stay stay idempotent.
		from kamra.api import _do_cancel
		rows = _reservations_for_booking(
			conn.property, e.get("booking_id") or e.get("ota_ref"), for_update=True)
		if not rows:
			return {"ota_ref": e.get("ota_ref"), "result": "cancel_unmatched"}
		cancelled, vouchers, review = [], [], []
		for r in rows:
			if r.status == "Cancelled":
				continue  # idempotent: already cancelled
			res = frappe.get_doc("Reservation", r.name, for_update=True)
			if res.status == "Cancelled":
				continue
			if res.status not in ("Confirmed", "Requested"):
				reason = (f"OTA cancel — resolve: {e.get('channel') or 'OTA'} "
				          f"cancelled {e.get('booking_id') or e.get('ota_ref')}")
				_flag_for_review(conn, res, e, reason, action="cancel",
				                 action_type="channel.cancel_needs_review")
				review.append(r.name)
				continue
			note = (f"Cancelled by {e.get('channel') or 'OTA'} "
			        f"({e.get('booking_id') or e.get('ota_ref')})")
			if res.status == "Requested":
				# a retained OTA sale holds no room and took no money
				_release(res, "Booked elsewhere", note)
				cancelled.append(r.name)
				continue
			out = _do_cancel(res, reason="Booked elsewhere", note=note,
			                 issue_credit_note=1)
			cancelled.append(r.name)
			if out.get("credit_note_voucher"):
				vouchers.append(out["credit_note_voucher"])
		result = ("cancelled" if cancelled
		          else "held_for_review" if review
		          else "already_cancelled")
		payload = {"ota_ref": e.get("ota_ref"), "result": result,
		           "reservations": cancelled, "credit_notes": vouchers}
		if review:
			payload["review_reservations"] = review
		return payload

	existing = _locked_live_ota(conn.property, e.get("ota_ref"))

	room_type, mapped_rate_plan = _mapping_for(
		conn.name,
		e.get("room_type_external_id"),
		e.get("rate_plan_external_id") or e.get("external_rate_id"),
	)
	locked_modify = e["event"] == "modify" and existing and existing.status in _LOCKED_MODIFY
	if not room_type and not locked_modify:
		return {"ota_ref": e.get("ota_ref"), "result": "unmapped_room",
		        "external_room_id": e.get("room_type_external_id")}
	resolved_rate_plan = mapped_rate_plan
	if room_type and not locked_modify:
		from kamra.pricing import resolve_booking_rate_plan
		try:
			resolved_rate_plan = resolve_booking_rate_plan(
				conn.property, room_type, e["check_in"], e["check_out"],
				rate_plan=mapped_rate_plan,
				adults=e.get("adults") or 2, children=e.get("children") or 0,
				check_availability=not bool(e.get("total")),
			)
		except frappe.ValidationError as exc:
			from kamra.savings import log_action
			log_action(
				"channel.rate_plan_refused", "Channel Manager Connection", conn.name,
				conn.property,
				rationale=f"{e.get('channel') or 'OTA'} booking {e.get('ota_ref')} via "
				          f"{conn.provider} refused: {exc}",
				channel="API",
			)
			return {"ota_ref": e.get("ota_ref"), "result": "rate_plan_refused",
			        "reason": str(exc)}
	restriction_mismatch = None
	if room_type and not locked_modify and e.get("check_in") and e.get("check_out"):
		from kamra.restrictions import evaluate_stay_restrictions
		restr_ev = evaluate_stay_restrictions(
			conn.property, room_type, e["check_in"], e["check_out"], rate_plan=resolved_rate_plan,
		)
		if not restr_ev["allowed"]:
			restriction_mismatch = restr_ev["summary"]
			if _strict_ota_restrictions(conn.property):
				from kamra.savings import log_action
				log_action(
					"channel.restriction_refused", "Channel Manager Connection", conn.name,
					conn.property,
					rationale=f"{e.get('channel') or 'OTA'} {e['event']} {e.get('ota_ref')} via "
					          f"{conn.provider} rejected by strict stay restrictions: {restriction_mismatch}",
					channel="API",
				)
				return {
					"ota_ref": e.get("ota_ref"),
					"result": "restriction_refused",
					"reason": restriction_mismatch,
					"violations": restr_ev["violations"],
				}
	property_currency = frappe.db.get_value("Property", conn.property, "currency")
	# a modify of a stay the OTA sold on a foreign contract: what it was stamped with
	booked = _booked_channel_snapshot(existing.name) if e["event"] == "modify" and existing else None
	contract, currency_held = None, False
	if e.get("currency") and property_currency and e["currency"] != property_currency:
		contract, why = _channel_contract(conn, e, property_currency, booked)
		if not contract:
			currency_held = True
			requested_reason = why or (f"OTA currency mismatch — resolve: {e.get('total') or 0} "
			                           f"{e['currency']}; property currency {property_currency}")
			# Keep foreign money in the note, never in the property's ledger currency.
			e = dict(e, total=0)
	if not currency_held and room_type:
		tax_event = dict(e, nightly=contract["nightly"]) if contract else e
		if untaxed := tax_hold(conn, tax_event, room_type):
			requested_reason = "; ".join(r for r in (requested_reason, untaxed) if r)
			# a price whose taxes cannot be read is not what the guest paid: never bill it
			e = dict(e, total=0)
			contract = None
	if contract and not locked_modify:
		# the OTA's own nights (gross when exclusive tax was on top), converted into the property's currency
		e = dict(e, total=contract["total"], nightly=contract["nightly"], nightly_net=False)

	if e["event"] == "modify" and existing:
		# A modify payload is the full new state for pre-arrival stays. Once
		# checked in or closed, OPERA Cloud (OXI UPDATE INHOUSE RES = N on the
		# connection, and closed-stay immutability) never lets an external
		# modify alter financials, room, dates or status: record it for review.
		doc = frappe.get_doc("Reservation", existing.name, for_update=True)
		if doc.status in _LOCKED_MODIFY:
			reason = requested_reason or _describe_requested_modify(conn, e, room_type)
			return _flag_for_review(conn, doc, e, reason)
		will_auto_price = 0 if requested_reason or e.get("total") else 1
		if not will_auto_price and _is_tariff_owned_stay(doc) and frappe.utils.cint(doc.get("auto_price")):
			# Confirmed contract-tariff stays owned by hospitality-tariffs (direct,
			# derived-plan, or repointed contract_tariff snapshot) cannot switch
			# auto_price off (HSP-TARIFF-008): hold the OTA modify for desk review
			# rather than failing the channel event or dropping the tariff.
			reason = requested_reason or _describe_requested_modify(conn, e, room_type)
			if doc.has_discount():
				reason = f"{reason}; active desk discount: {_describe_superseded_discount(doc)}"
			return _flag_for_review(
				conn, doc, e, reason,
				not_applied_why="contract-tariff stay requires desk review",
			)
		old_doc = frappe.get_doc("Reservation", existing.name)
		if requested_reason:
			e = dict(e, notes="\n".join(s for s in (e.get("notes"), requested_reason) if s))
		other_holds = _non_tax_hold_lines(doc.special_requests) if doc.status == "Requested" else []
		tax_held = (doc.status == "Requested" and not other_holds
		            and any(h in (doc.special_requests or "") for h in _TAX_HOLDS))
		keep_non_tax_hold = doc.status == "Requested" and not tax_held
		doc.room_type = room_type
		if resolved_rate_plan:
			doc.rate_plan = resolved_rate_plan
		doc.check_in_date = e["check_in"]
		doc.check_out_date = e["check_out"]
		doc.adults = e.get("adults") or 2
		doc.children = e.get("children") or 0
		doc.special_requests = (
			_kept_hold_notes(doc.special_requests, e.get("notes"))
			if keep_non_tax_hold else _merge_review_notes(doc.special_requests, e.get("notes"))
		)
		from frappe.utils import flt
		old_amount = flt(doc.amount_after_tax)
		discount_review = None
		doc.auto_price = 0 if requested_reason or e.get("total") else 1
		if not doc.auto_price and doc.has_discount():
			from kamra.api import _discount_snapshot
			discount_review = {
				"kind": CHANNEL_REVIEW_DISCOUNT_REMOVED,
				"action_type": "channel.modify_discount_removed",
				"before": {**_discount_snapshot(doc), "amount_after_tax": old_amount},
				"desc": _describe_superseded_discount(doc),
				"kept": [],
				"added": [],
			}
		if e.get("total") or requested_reason:
			doc.amount_after_tax = e.get("total") or 0
		if not doc.auto_price:
			# the channel's own price: the channel manager is its writer
			# (Reservation.guard_locked_nights), with or without a total yet;
			# any pre-arrival desk discount is superseded and every dependent
			# total/tax/night figure is recomputed cleanly through lock_channel_rates
			doc.flags.kamra_night_rates_writer = True
			doc.discount_type = None
			doc.discount_value = 0
			doc.discount_reason = None
			doc.fixed_rate = 0
			doc.flags.kamra_reservation_discount = True
			lock_channel_rates(doc, e.get("total"), e.get("nightly"), e.get("nightly_net"))
		else:
			discount_review = _reconcile_ota_modify_discount(doc, old_doc)
		if contract:
			_stamp_channel_contract(doc, contract)
		elif booked:
			# the OTA no longer sells the stay on its foreign contract: none of it stays
			from kamra.rates import effective_rate_plan_required
			if not effective_rate_plan_required(conn.property):
				doc.rate_plan = None
			doc.agreed_rate, doc.contract_tariff = 0, None
			doc.flags.kamra_channel_tariff = True
		if requested_reason and not e.get("total"):
			doc.set("night_rates", [])  # no price until staff resolve it
		if restriction_mismatch:
			doc.flags.kamra_ota_restriction_override = True
		old_transition = frappe.flags.kamra_status_transition
		try:
			if requested_reason:
				doc.status = "Requested"
				frappe.flags.kamra_status_transition = True
			elif tax_held:
				# the OTA's new state reads its taxes: the hold is released and
				# the stay takes its room (or is refused one, and held as overbooked)
				doc.status = "Confirmed"
			doc.save(ignore_permissions=True)
		finally:
			frappe.flags.kamra_status_transition = old_transition
		if restriction_mismatch:
			mismatch_note = f"OTA restriction mismatch — resolve: {restriction_mismatch}"
			m_updates = {
				"channel_review_needed": 1,
				"channel_review_reason": CHANNEL_REVIEW_RESTRICTION,
			}
			if mismatch_note not in (doc.special_requests or ""):
				m_updates["special_requests"] = "\n".join(
					s for s in (doc.special_requests, mismatch_note) if s
				)
			doc.db_set(m_updates)
			from kamra.savings import log_action
			log_action(
				"channel.restriction_mismatch", "Reservation", doc.name, conn.property,
				rationale=f"{e.get('channel') or 'OTA'} {e['event']} {e.get('ota_ref')} via "
				          f"{conn.provider} accepted for review despite restriction: {restriction_mismatch}",
				channel="API",
			)
		if discount_review:
			from kamra.api import _discount_snapshot
			from kamra.localization import money_text
			from kamra.pricing import night_rate_places
			from kamra.savings import log_action

			channel_label = e.get("channel") or "OTA"
			ref_label = e.get("ota_ref") or doc.ota_ref or ""
			new_total_text = money_text(doc.property, doc.amount_after_tax)
			if discount_review["kind"] == CHANNEL_REVIEW_DISCOUNT_PARTIAL:
				kept_str = ", ".join(discount_review["kept"])
				dropped_list = discount_review.get("dropped") or []
				added_list = discount_review.get("added") or []
				uncovered_parts = []
				if dropped_list:
					uncovered_parts.append(f"repriced night(s) {', '.join(dropped_list)}")
				if added_list:
					uncovered_parts.append(f"added night(s) {', '.join(added_list)}")
				uncovered_str = " and ".join(uncovered_parts) or "other night(s)"
				review_note = (
					f"Channel modify ({channel_label} {ref_label}) applied — kept approved "
					f"desk discount on {kept_str}; {uncovered_str} priced without "
					f"unapproved discount."
				)
			elif requested_reason:
				review_note = (
					f"Channel modify ({channel_label} {ref_label}) applied (held as Requested) — "
					f"removed desk discount: {discount_review['desc']}."
				)
			else:
				review_note = (
					f"Channel modify ({channel_label} {ref_label}) applied — removed desk "
					f"discount: {discount_review['desc']}."
				)
			audit_rationale = f"{review_note[:-1]}; new total {new_total_text}."
			updates = {
				"channel_review_needed": 1,
				"channel_review_reason": discount_review["kind"],
			}
			if review_note not in (doc.special_requests or ""):
				updates["special_requests"] = "\n".join(
					s for s in (doc.special_requests, review_note) if s
				)
			doc.db_set(updates)
			places = night_rate_places(doc.property)
			after_discount_total = round(
				sum(flt(r.get("discount_amount")) for r in doc.get("night_rates") or []),
				places,
			)
			after_snap = {
				**_discount_snapshot(doc),
				"discount_total": after_discount_total,
				"amount_after_tax": flt(doc.amount_after_tax),
			}
			log_action(
				"reservation_discount", "Reservation", doc.name, conn.property,
				rationale=audit_rationale,
				before_snapshot=discount_review["before"],
				after_snapshot=after_snap,
				channel="API",
			)
			log_action(
				discount_review["action_type"], "Reservation", doc.name, conn.property,
				rationale=audit_rationale,
				channel="API",
			)
		out_mod = {"ota_ref": e["ota_ref"], "result": "modified",
		           "reservation": existing.name}
		if restriction_mismatch:
			out_mod["restriction_mismatch"] = True
		return out_mod

	if requested_reason:
		e = dict(e, notes="\n".join(s for s in (e.get("notes"), requested_reason) if s))

	if existing:
		return {"ota_ref": e["ota_ref"], "result": "duplicate_ignored",
		        "reservation": existing.name}

	guest = _find_or_create_guest(e.get("guest_name"), e.get("phone"),
	                              e.get("email"), conn.property)
	doc = frappe.get_doc({
		"doctype": "Reservation",
		"property": conn.property,
		"guest": guest,
		"room_type": room_type,
		"rate_plan": resolved_rate_plan or None,
		"check_in_date": e["check_in"],
		"check_out_date": e["check_out"],
		"adults": e.get("adults") or 2,
		"children": e.get("children") or 0,
		"source": "OTA",
		"status": "Requested" if requested_reason else "Confirmed",
		"channel": e.get("channel"),
		"ota_ref": e.get("ota_ref"),
		"special_requests": e.get("notes") or None,
		# the OTA already sold at its price - keep their number, don't reprice
		"auto_price": 0 if requested_reason or e.get("total") else 1,
	})
	if not doc.auto_price:
		# the channel's own price: the channel manager is its writer
		# (Reservation.guard_locked_nights), with or without a total yet
		doc.flags.kamra_night_rates_writer = True
	if e.get("total"):
		doc.amount_after_tax = e["total"]
		lock_channel_rates(doc, e["total"], e.get("nightly"), e.get("nightly_net"))
	if contract:
		_stamp_channel_contract(doc, contract)
	if restriction_mismatch:
		doc.flags.kamra_ota_restriction_override = True
	try:
		doc.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		# The other delivery committed while this one waited on the property
		# lock. Hand back that stay instead of booking the guest twice.
		winner = _locked_live_ota(conn.property, e.get("ota_ref"))
		if winner:
			return {"ota_ref": e.get("ota_ref"), "result": "duplicate_ignored",
			        "reservation": winner.name}
		raise
	if restriction_mismatch:
		mismatch_note = f"OTA restriction mismatch — resolve: {restriction_mismatch}"
		m_updates = {
			"channel_review_needed": 1,
			"channel_review_reason": CHANNEL_REVIEW_RESTRICTION,
		}
		if mismatch_note not in (doc.special_requests or ""):
			m_updates["special_requests"] = "\n".join(
				s for s in (doc.special_requests, mismatch_note) if s
			)
		doc.db_set(m_updates)
		from kamra.savings import log_action
		log_action(
			"channel.restriction_mismatch", "Reservation", doc.name, conn.property,
			rationale=f"{e.get('channel') or 'OTA'} {e['event']} {e.get('ota_ref')} via "
			          f"{conn.provider} accepted for review despite restriction: {restriction_mismatch}",
			channel="API",
		)
	# the channel's own commission when it sends one, else the site's default
	if e.get("commission"):
		doc.db_set("commission_amount", float(e["commission"]))
	else:
		set_ota_commission(doc, channel_commission_pcts().get(e.get("channel")))
	from kamra.savings import log_action
	log_action("channel.booking", "Reservation", doc.name, conn.property,
	           minutes_saved=4,
	           rationale=f"{e.get('channel') or 'OTA'} booking "
	                     f"{e.get('ota_ref')} via {conn.provider}",
	           channel="API")
	# the availability push back to the channel is triggered by the
	# on_reservation_change doc_event this insert fires - no ad-hoc enqueue.
	out_book = {"ota_ref": e.get("ota_ref"), "result": "booked",
	            "reservation": doc.name}
	if restriction_mismatch:
		out_book["restriction_mismatch"] = True
	return out_book


def _booked_channel_snapshot(reservation: str) -> dict | None:
	"""The contract snapshot an OTA booking was stamped with in its rate
	plan's currency (_channel_contract), or None."""
	raw = frappe.db.get_value("Reservation", reservation, "contract_tariff")
	snapshot = frappe.parse_json(raw) if raw else None
	if isinstance(snapshot, dict) and (snapshot.get("tariff") or {}).get("source") == "channel":
		return snapshot
	return None


def _channel_contract(conn, e: dict, home: str, booked: dict | None = None) -> tuple[dict | None, str | None]:
	"""An OTA booking in its mapped rate plan's currency (C), in the
	property's (H).

	OPERA Cloud posts a foreign rate code in the property currency at the
	property's rate; SAP converts at the governed rate of the pricing date.
	So the OTA's own nights (its discounts kept) convert at the governed
	selling rate of the business date (pricing.posting_fx_rate), rounded as
	the plan rounds (pricing.contract_local_amount), and the booking keeps a
	contract snapshot of them in the contract's per-night shape (each night's
	own amounts, the scalar figures the check-in night's, as
	pricing.contract_rate keeps a stay over a tariff change); a night the
	OTA sold at 0.00 (a free night) stays free. A booking the OTA explicitly
	collects (payment_collect "ota") or pays by virtual card is fixed at
	reservation; any other - Channex documents a missing payment_collect as
	the property collecting - follows its rate plan's lock, as a desk
	booking does and as OPERA OXI keeps the rate code's exchange type
	(pricing.reservation_fx_policy).

	A modification of a stay already on its contract in C (`booked`) keeps
	the booking's exchange basis - its rate, its date and its lock - and
	reads only the OTA's amounts again, as OPERA keeps a reservation's
	exchange basis on a modification.

	({rate_plan, total, nightly, agreed_rate, snapshot}, None); (None, why)
	when no rate is published for C; (None, None) when the booking is not in
	its mapped plan's currency, or not in the currency the mapping sells in."""
	from kamra.business_date import get_business_date
	from kamra.pricing import (
		DEFAULT_FX_LOCK,
		contract_local_amount,
		posting_fx_rate,
		rate_plan_currency,
		resolve_rate_plan_chain,
		round_rate_amount,
	)

	rate_id = e.get("rate_plan_external_id")
	mapping = rate_id and frappe.db.get_value(
		"Channel Room Mapping", {"connection": conn.name, "external_rate_id": rate_id},
		["rate_plan", "external_currency"], as_dict=True)
	plan = mapping and mapping.rate_plan
	if not plan:
		return None, None
	if mapping.external_currency and mapping.external_currency != e.get("currency"):
		return None, None  # Kamra sells this plan there in another currency: not money it can read
	chain = resolve_rate_plan_chain(plan)
	currency = rate_plan_currency(chain)
	if currency != e.get("currency") or currency == home:
		return None, None
	tariff = (booked or {}).get("tariff") or {}
	if tariff.get("currency") == currency and Decimal(str(tariff.get("fx_rate") or 0)) > 0:
		fx, fx_date, lock = Decimal(str(tariff["fx_rate"])), tariff.get("fx_date"), booked.get("fx_lock_policy")
	else:
		business = get_business_date(conn.property)
		posting = posting_fx_rate(conn.property, currency, business)
		if not posting:
			return None, (f"No published {currency} exchange rate for {business} — resolve: "
			              f"{e.get('total') or 0} {currency}; property currency {home}")
		fx, fx_date = posting
		lock = DEFAULT_FX_LOCK if e.get("payment_collect") == "ota" or e.get("virtual_card") else None
	dates = [str(add_days(e["check_in"], i))
	         for i in range(max(date_diff(e["check_out"], e["check_in"]), 1))]
	given = {str(getdate(n["date"])): Decimal(str(n.get("amount") or 0))
	         for n in e.get("nightly") or [] if n.get("date")}
	amounts = [given.get(d) for d in dates]
	if any(amount is None or amount < 0 for amount in amounts) or sum(amounts) <= 0:
		# no price for every night: the total, evenly
		total = Decimal(str(e.get("total") or 0))
		share = round_rate_amount(total / len(dates), 0, currency=currency)
		amounts = [share] * (len(dates) - 1) + [total - share * (len(dates) - 1)]
		if not all(amount > 0 for amount in amounts):
			return None, None
	elif e.get("nightly_net"):
		# day prices are before room tax; scale them to the gross total (days + room tax)
		total = Decimal(str(e.get("total") or 0))
		net_sum = sum(amounts)
		if total > net_sum > 0:
			scaled = [round_rate_amount(a * total / net_sum, 0, currency=currency) if a > 0 else Decimal(0)
			          for a in amounts]
			last = max(i for i, a in enumerate(amounts) if a > 0)
			scaled[last] += total - sum(scaled)
			amounts = scaled
	rp = chain[-1]
	local = [contract_local_amount(rp, amount, fx, home) for amount in amounts]
	nights = [{"date": d, "nightly": float(amount), "agreed_rate": float(value)}
	          for d, amount, value in zip(dates, amounts, local, strict=True)]
	agreed = nights[0]["agreed_rate"]
	snapshot = {"tariff": {"currency": currency, "nightly": nights[0]["nightly"], "fx_rate": float(fx),
	                       "fx_date": fx_date, "property_currency": home, "source": "channel", "nights": nights},
	            "agreed_rate": agreed}
	if lock:
		snapshot["fx_lock_policy"] = lock
	return {"rate_plan": plan, "total": float(sum(local)), "agreed_rate": agreed, "snapshot": snapshot,
	        "nightly": [{"date": d, "amount": float(v)} for d, v in zip(dates, local, strict=True)]}, None


def _stamp_channel_contract(doc, contract: dict) -> None:
	"""The booking's rate plan, its agreed rate and the snapshot of what the
	OTA sold (capture_contract_tariff keeps it under kamra_channel_tariff)."""
	doc.rate_plan = contract["rate_plan"]
	doc.agreed_rate = contract["agreed_rate"]
	doc.contract_tariff = contract["snapshot"]
	doc.flags.kamra_channel_tariff = True


def lock_channel_rates(doc, total_after_tax, nightly=None, nightly_net=False) -> None:
	"""Lock an OTA booking's nightly rates from the channel's own prices.

	The OTA sold the stay at its price (`total_after_tax`, tax included), so
	the folio must bill that - not today's rack rate. The channel's per-night
	prices say how the total splits across the nights (an even split when it
	sends none) - each with its own room tax put on first when they are
	before tax (`nightly_net`), so a night in a higher slab takes its own
	share; each night's share has the room tax backed out at the slab its
	own pre-tax rate falls in, and the last night takes the rounding, as the
	desk's nights do (Reservation.quote_night_rows). To the currency's
	decimals, never fewer than two (pricing.night_rate_places). A zero total
	(a currency mismatch or a tax that cannot be read, held for staff)
	locks nothing, and a night locked before it is cleared: a stay billed
	nothing keeps no price."""
	total = float(total_after_tax or 0)
	if total <= 0:
		doc.set("night_rates", [])
		doc.amount_before_tax = 0
		doc.tax_amount = 0
		doc.amount_after_tax = 0
		doc.discount_amount = 0
		if hasattr(doc, "quote_snapshot"):
			doc.quote_snapshot = None
		doc.rate_lock_key = None
		doc.flags.kamra_night_rates_writer = True
		return
	if not doc.room_type:
		return
	from frappe.utils import date_diff, getdate

	from kamra.pricing import entry_rate, night_rate_places, rates_include_tax, room_gst_rate

	nights = max(date_diff(doc.check_out_date, doc.check_in_date), 1)
	dates = [str(add_days(doc.check_in_date, i)) for i in range(nights)]
	given = {str(getdate(n["date"])): float(n.get("amount") or 0)
	         for n in (nightly or []) if n.get("date")}
	weights = [given.get(d, 0.0) for d in dates]
	rt = frappe.get_doc("Room Type", doc.room_type)
	if nightly_net:
		weights = [w * (1 + float(room_gst_rate(doc.property, rt, Decimal(str(w)))) / 100)
		           for w in weights]
	if not sum(weights):
		weights = [1.0] * nights
	places = night_rate_places(doc.property)
	inclusive = rates_include_tax(doc.property)
	shares, running = [], 0.0
	scale = total / sum(weights)
	for i, (d, w) in enumerate(zip(dates, weights, strict=True)):
		gross = round(total - running, places) if i == nights - 1 else round(w * scale, places)
		running += gross
		gst = Decimal(str(room_gst_rate(doc.property, rt, Decimal(str(gross)))))
		pre = Decimal(str(gross)) / (1 + gst / 100)
		gst2 = Decimal(str(room_gst_rate(doc.property, rt, pre)))
		if gst2 != gst:  # the slab follows the pre-tax rate
			gst = gst2
			pre = Decimal(str(gross)) / (1 + gst / 100)
		shares.append((d, pre, gst))
	room_total = float(sum(pre for _, pre, _ in shares))
	rows, running = [], 0.0
	for i, (d, pre, gst) in enumerate(shares):
		rate = round(room_total - running, places) if i == nights - 1 else round(float(pre), places)
		running += rate
		rows.append({
			"stay_date": d,
			"rate": rate,
			"base_rate": rate,
			"gst_rate": float(gst),
			"meal_amount": 0,
			"demand_premium_pct": 0,
			"discount_amount": 0,
			"discount_type": None,
			"discount_value": 0,
			"discount_reason": None,
			"fixed_rate": 0,
			"rate_code_amount": round(float(entry_rate(rate, gst, inclusive, places)), places),
		})
	before_tax = round(sum(r["rate"] for r in rows), places)
	after_tax = round(total, places)
	tax_amount = round(after_tax - before_tax, places)
	doc.set("night_rates", rows)
	doc.amount_before_tax = before_tax
	doc.tax_amount = tax_amount
	doc.amount_after_tax = after_tax
	doc.discount_amount = 0
	if hasattr(doc, "quote_snapshot"):
		doc.quote_snapshot = None
	doc.rate_lock_key = None
	# the channel's own prices: the one writer of a locked night besides the
	# booking's own pricing (Reservation.guard_locked_nights)
	doc.flags.kamra_night_rates_writer = True


def _reconcile_booking_rooms(conn, booking_id: str, keep: set) -> None:
	"""After a modify, a room line the payload no longer carries is a room the
	guest dropped. Release Confirmed/Requested rooms (no credit note - a modify
	is not a cancel); leave in-house or closed rooms untouched and flag them
	for front-desk review."""
	for r in _reservations_for_booking(conn.property, booking_id, for_update=True):
		if r.ota_ref in keep or r.status == "Cancelled":
			continue
		doc = frappe.get_doc("Reservation", r.name, for_update=True)
		if doc.status == "Cancelled":
			continue
		if doc.status not in ("Confirmed", "Requested"):
			reason = f"OTA modify removed room — resolve: {r.ota_ref} omitted by {conn.provider}"
			_flag_for_review(conn, doc, {"ota_ref": r.ota_ref, "channel": doc.channel or "OTA"}, reason)
			continue
		_release(doc, "Other", f"Room removed on modify by {conn.provider}")


def _release(doc, reason: str, note: str) -> None:
	"""Cancel without a fee or credit note."""
	frappe.flags.kamra_cancelling = True
	try:
		doc.status = "Cancelled"
		doc.cancellation_reason = reason
		doc.cancellation_note = note
		doc.save(ignore_permissions=True)
	finally:
		frappe.flags.kamra_cancelling = False


def _apply_webhook_batch(conn, events: list[dict], *, requested_reason: str | None = None) -> list[dict]:
	if not events:
		return []
	action = events[0].get("event")
	booking_id = events[0].get("booking_id") or events[0].get("ota_ref")
	results = [_apply_event(conn, e, requested_reason=requested_reason) for e in events]
	if action == "modify":
		_reconcile_booking_rooms(conn, booking_id, keep={e.get("ota_ref") for e in events})
	return results


def process_webhook_events(connection: str, payload: dict) -> None:
	"""Enqueued worker for an inbound OTA webhook (one delivery = one booking).

	Kept off the request path so the endpoint can answer Aiosell instantly.
	The whole booking is applied atomically. A capacity refusal rolls back
	all lines, then retains them as Requested for staff to resolve. A MariaDB
	snapshot-isolation conflict (1020) or deadlock rolls back to a fresh
	snapshot and retries; if retries are exhausted, the event is re-queued
	rather than silently dropped."""
	from kamra.kamra.doctype.reservation.reservation import InventoryRefused

	for attempt in range(1, _WEBHOOK_CONFLICT_RETRIES + 1):
		conn = frappe.get_doc("Channel Manager Connection", connection)
		if not conn.active:
			return
		events = provider_for(conn.provider).parse_webhook(conn, payload)
		if not events:
			return
		action = events[0].get("event")
		booking_id = events[0].get("booking_id") or events[0].get("ota_ref")
		try:
			_apply_webhook_batch(conn, events)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- background webhook worker persists the completed booking before returning; reviewed as intentional
			return
		except Exception as exc:
			capacity_refused = isinstance(exc, InventoryRefused)
			conflict = _is_snapshot_conflict(exc)
			frappe.db.rollback()
			if conflict:
				if attempt < _WEBHOOK_CONFLICT_RETRIES:
					continue
				frappe.enqueue(
					"kamra.channel_manager.process_webhook_events",
					queue="short",
					connection=connection,
					payload=payload,
				)
				frappe.log_error(
					title=f"OTA webhook re-queued after snapshot conflict: {conn.name} / {booking_id}")
				return
			if capacity_refused and action in ("book", "modify"):
				try:
					_apply_webhook_batch(conn, events, requested_reason="OTA overbooking — resolve")
					frappe.db.commit()  # nosemgrep: frappe-manual-commit -- background worker persists the non-inventory booking for resolution
					return
				except Exception as retry_exc:
					frappe.db.rollback()
					if _is_snapshot_conflict(retry_exc):
						if attempt < _WEBHOOK_CONFLICT_RETRIES:
							continue
						frappe.enqueue(
							"kamra.channel_manager.process_webhook_events",
							queue="short",
							connection=connection,
							payload=payload,
						)
						frappe.log_error(
							title=f"OTA webhook re-queued after snapshot conflict: {conn.name} / {booking_id}")
						return
					frappe.log_error(
						title=f"OTA webhook apply failed: {conn.name} / {booking_id}")
					return
			frappe.log_error(
				title=f"OTA webhook apply failed: {conn.name} / {booking_id}")
			return


def _norm(s: str) -> str:
	return (s or "").strip().lower().replace(" ", "").replace("_", "").replace("-", "")


@frappe.whitelist()
@require_roles("Revenue Manager", "Hotel Admin", "Kamra Agent")
def import_room_mappings(connection: str, dry_run: int = 0):
	"""Property mapping, done once: pull the provider's room + rate-plan codes
	(via the adapter's fetch_property_details) and map them onto this
	property's room types.

	Auto-matches a provider room to a Kamra Room Type by code or name, then
	creates/updates a Channel Room Mapping (external_room_id = provider room id,
	external_rate_id = the room's first rate plan). Anything it can't place is
	returned under `unmatched` so you can map it by hand. Pass dry_run=1 to
	preview without writing.

	A provider that prices each rate plan (details["per_rate_plan"], Channex)
	gets one mapping per provider rate plan instead, matched to a Kamra Rate
	Plan by code or name; the one plan of a room left unmatched sells the
	room type's base price. `warnings` lists what to check by hand: currency,
	time zone, room counts, per-person plans and fixed-amount plans."""
	conn = frappe.get_doc("Channel Manager Connection", connection)
	provider = provider_for(conn.provider)
	if not hasattr(provider, "fetch_property_details"):
		frappe.throw(f"{conn.provider} does not support mapping import.")
	details = provider.fetch_property_details(conn)

	rts = frappe.get_all("Room Type",
	                     filters={"property": conn.property, "disabled": 0},
	                     fields=["name", "room_type_code", "room_type_name"])
	index = {}
	for rt in rts:
		for key in (rt.room_type_code, rt.room_type_name,
		            rt.name.removeprefix(f"{conn.property}-")):
			index.setdefault(_norm(key), rt.name)

	if details.get("per_rate_plan"):
		return _import_rate_plan_mappings(conn, details, index, int(dry_run or 0))

	created, updated, unmatched = [], [], []
	for r in details.get("rooms") or []:
		room_id = str(r.get("room_id") or "")
		rateplans = r.get("rateplans") or []
		rate_id = str(rateplans[0].get("rateplan_id")) if rateplans else None
		match = index.get(_norm(r.get("room_id"))) \
			or index.get(_norm(r.get("room_name")))
		if not match:
			unmatched.append({
				"room_id": room_id, "room_name": r.get("room_name"),
				"rateplans": [rp.get("rateplan_id") for rp in rateplans]})
			continue
		existing = frappe.db.get_value(
			"Channel Room Mapping",
			{"connection": connection, "room_type": match}, "name")
		row = {"room_type": match, "external_room_id": room_id,
		       "external_rate_id": rate_id}
		if int(dry_run or 0):
			(updated if existing else created).append(row)
			continue
		if existing:
			frappe.db.set_value("Channel Room Mapping", existing, {
				"external_room_id": room_id, "external_rate_id": rate_id})
			updated.append({**row, "name": existing})
		else:
			doc = frappe.get_doc({
				"doctype": "Channel Room Mapping", "connection": connection,
				**row}).insert(ignore_permissions=True)
			created.append({**row, "name": doc.name})
	return {"hotel_id": details.get("hotel_id"),
	        "hotel_name": details.get("hotel_name"),
	        "created": created, "updated": updated, "unmatched": unmatched,
	        "warnings": []}


def _sends_in(property: str, room_type: str, rate_plan: str | None, currency: str) -> bool:
	"""Whether a night of `rate_plan` can go in `currency`, asked of the
	push's own channel_ari._plan_night_rate for the business date (a plan
	sold in its contract currency reads the tariff directory, so no exchange
	rate is needed): False only when it cannot (no listed contract prices a
	foreign plan, so its card price goes only in the property's currency, or
	the contract is listed in another currency). An error is the push's to
	report, not a currency mismatch."""
	from kamra.business_date import get_business_date
	from kamra.channel_ari import AriCurrencyRefused, AriPriceHeld, AriPricingFailed, _plan_night_rate
	try:
		_plan_night_rate(property, room_type, rate_plan, getdate(get_business_date(property)), currency)
	except AriCurrencyRefused:
		return False
	except (AriPriceHeld, AriPricingFailed):
		return True
	return True


def _import_rate_plan_mappings(conn, details: dict, index: dict, dry_run: int) -> dict:
	"""import_room_mappings for a provider that prices each rate plan: one
	Channel Room Mapping per provider rate plan, found again by
	(connection, external_rate_id)."""
	from frappe.utils import get_system_timezone
	plans = {}
	for p in frappe.get_all("Rate Plan", filters={"property": conn.property, "disabled": 0},
	                        fields=["name", "code", "rate_plan_name", "modifier_type"]):
		for key in (p.code, p.rate_plan_name):
			if key:
				plans.setdefault(_norm(key), p)
	from kamra.pricing import property_currency, rate_plan_currency, resolve_rate_plan_chain
	prop = frappe.db.get_value("Property", conn.property, ["currency", "timezone"], as_dict=True) or {}
	home = property_currency(conn.property)
	warnings, warned = [], set()

	def warn(code: str, key, message: str, **extra) -> None:
		if (code, key) not in warned:
			warned.add((code, key))
			warnings.append({"code": code, "message": message, **extra})

	def sold_in(rp, rate_plan, room_type) -> str | None:
		"""E: what the provider rate plan sells in, else the provider
		property's currency. A plan that cannot go in it - an unknown E
		included, and a foreign plan no tariff prices, whose card price goes
		only in the property's currency - is warned about, plan by plan."""
		rate_id = str(rp.get("rateplan_id") or "")
		external_currency = rp.get("currency") or details.get("currency") or None
		priced_in = rate_plan_currency(resolve_rate_plan_chain(rate_plan)) if rate_plan else home
		allowed = {priced_in, home}
		if external_currency == priced_in != home and not _sends_in(conn.property, room_type, rate_plan,
		                                                            external_currency):
			allowed = {home}
		if external_currency not in allowed:
			warn("rate_plan_currency", rate_id, _(
				"Rate plan {0} sells in {1} on the channel manager; {product} sends it only in {2}, so "
				"its rates will not go out. Change that rate plan's currency there, then import again.").format(
				rp.get("rateplan_name") or rate_id, external_currency or _("no currency"),
				" or ".join(sorted(allowed)), product=product_name()), rate_id=rate_id)
		return external_currency

	if details.get("currency") and prop.get("currency") and details["currency"] != prop["currency"]:
		warn("currency", None, _("The channel manager sells in {0}; this property is in {1}.").format(
			details["currency"], prop["currency"]))
	timezone = prop.get("timezone") or get_system_timezone()
	if details.get("timezone") and details["timezone"] != timezone:
		warn("timezone", None, _("The channel manager's property time zone is {0}; this property's is {1}.").format(
			details["timezone"], timezone))

	created, updated, unmatched = [], [], []
	for r in details.get("rooms") or []:
		room_id = str(r.get("room_id") or "")
		rateplans = r.get("rateplans") or []
		match = index.get(_norm(r.get("room_id"))) or index.get(_norm(r.get("room_name")))
		if not match:
			unmatched.append({"room_id": room_id, "room_name": r.get("room_name"),
			                  "rateplans": [rp.get("rateplan_id") for rp in rateplans],
			                  "reason": _("No room type matches it.")})
			continue
		rooms = frappe.db.count("Room", {"room_type": match})
		if r.get("count_of_rooms") is not None and int(r["count_of_rooms"]) != rooms:
			warn("count_of_rooms", room_id, _("The channel manager has {0} rooms of {1}; {product} has {2}.").format(
				r["count_of_rooms"], r.get("room_name") or room_id, rooms, product=product_name()))
		paired, loose = [], []
		for rp in rateplans:
			plan = plans.get(_norm(rp.get("rateplan_name"))) or plans.get(_norm(rp.get("rateplan_id")))
			(paired if plan else loose).append((rp, plan))
		if len(loose) == 1:  # the room's own price: no Kamra plan, the base price
			paired.append((loose[0][0], None))
			loose = []
		for rp, _plan in loose:
			rate_id = str(rp.get("rateplan_id") or "")
			hand = rate_id and frappe.db.get_value("Channel Room Mapping", {
				"connection": conn.name, "external_rate_id": rate_id},
				["name", "room_type", "external_room_id", "rate_plan"], as_dict=True)
			if hand:
				# mapped by hand: its room type and rate plan stay, its currency is read again
				row = {"room_type": hand.room_type, "external_room_id": hand.external_room_id,
				       "external_rate_id": rate_id, "rate_plan": hand.rate_plan,
				       "external_currency": sold_in(rp, hand.rate_plan, hand.room_type)}
				if not dry_run:
					doc = frappe.get_doc("Channel Room Mapping", hand.name)
					doc.external_currency = row["external_currency"]
					doc.save(ignore_permissions=True)
				updated.append({**row, "name": hand.name})
				continue
			unmatched.append({"room_id": room_id, "room_name": r.get("room_name"),
			                  "rateplan_id": str(rp.get("rateplan_id") or ""), "rateplan_name": rp.get("rateplan_name"),
			                  "reason": _("Several rate plans of this room match no {product} rate plan: map them by hand.").format(product=product_name())})
		for rp, plan in paired:
			rate_id = str(rp.get("rateplan_id") or "")
			if rp.get("sell_mode") == "per_person":
				warn("per_person", rate_id, _("Rate plan {0} is priced per person; {product} sends one price per room.").format(
					rp.get("rateplan_name") or rate_id, product=product_name()))
			if plan and plan.modifier_type in ("Amount", "Absolute"):
				warn("fixed_amount", plan.name, _("Rate plan {0} adds a fixed amount: on the channels it applies every "
				                                  "night, on direct bookings once per stay.").format(plan.rate_plan_name))
			external_currency = sold_in(rp, plan.name if plan else None, match)
			row = {"room_type": match, "external_room_id": room_id, "external_rate_id": rate_id,
			       "rate_plan": plan.name if plan else None, "external_currency": external_currency}
			existing = frappe.db.get_value("Channel Room Mapping",
			                               {"connection": conn.name, "external_rate_id": rate_id}, "name")
			if dry_run:
				(updated if existing else created).append(row)
				continue
			if existing:
				doc = frappe.get_doc("Channel Room Mapping", existing)
				doc.update(row)
				doc.save(ignore_permissions=True)
				updated.append({**row, "name": existing})
			else:
				doc = frappe.get_doc({"doctype": "Channel Room Mapping", "connection": conn.name,
				                      **row}).insert(ignore_permissions=True)
				created.append({**row, "name": doc.name})
	return {"hotel_id": details.get("hotel_id"), "hotel_name": details.get("hotel_name"),
	        "created": created, "updated": updated, "unmatched": unmatched, "warnings": warnings}


@frappe.whitelist()
@require_roles("Revenue Manager", "Hotel Admin", "Front Desk", "Kamra Agent")
def connection_status(property: str):
	"""The sync-health card: connections, mappings, last push."""
	out = []
	for name in _connections(property, only_active=False):
		c = frappe.get_doc("Channel Manager Connection", name)
		out.append({
			"name": c.name, "provider": c.provider, "active": c.active,
			"external_property_id": c.external_property_id,
			"mappings": len(_mappings(name)),
			"last_push": str(c.last_push or ""),
			"last_push_status": c.last_push_status,
			"webhook_url": f"/api/method/kamra.channel_manager.webhook"
			               f"?connection={c.name}",
		})
	return out
