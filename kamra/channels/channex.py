"""Channex.io adapter - https://docs.channex.io

API-first channel-manager infrastructure: self-serve signup, sandbox at
staging.channex.io, per-property pricing, certified with Booking.com,
Agoda, Expedia, Airbnb and more. The hotel brings its own Channex
account; api_key is a Channex user API key, external_property_id the
Channex property id, and mappings carry Channex room_type/rate_plan ids.

Webhook: register a Channex webhook (event: booking) pointing at
/api/method/kamra.channel_manager.webhook?connection=<name>; put the
same secret on both sides.

Availability and rates go out through the ARI outbox (kamra.channel_ari):
only what changed, as date ranges, one call per endpoint, under Channex's
rate limit.
"""

from __future__ import annotations

import unicodedata
from decimal import ROUND_HALF_UP, Decimal

DEFAULT_ENDPOINT = "https://app.channex.io/api/v1"


class ChannexError(Exception):
	"""A non-2xx answer from Channex. detail is short (<= 300 characters) and
	never the whole body."""

	def __init__(self, status: int, detail: str):
		self.status = status
		self.detail = str(detail or "")[:300]
		super().__init__(f"Channex {status}: {self.detail}")


class ChannexRateLimited(ChannexError):
	"""429: wait retry_after seconds (Retry-After; 60 when missing)."""

	def __init__(self, status: int, detail: str, retry_after: int = 60):
		super().__init__(status, detail)
		self.retry_after = retry_after


def _retry_after(value) -> int:
	try:
		seconds = int(str(value).strip())
	except (TypeError, ValueError):
		return 60
	return seconds if seconds > 0 else 60


def _send(conn, method: str, path: str, *, params: dict | None = None,
          payload=None) -> tuple[dict, dict]:
	"""One call to the Channex API: (the JSON of a 2xx, its headers with
	lower-case names). ChannexRateLimited on a 429, ChannexError on anything
	else; a timeout or a dropped connection is ChannexError(0, ...)."""
	import requests

	key = conn.get_password("api_key", raise_exception=False)
	base = (conn.endpoint or DEFAULT_ENDPOINT).rstrip("/")
	try:
		res = requests.request(
			method, f"{base}/{path}", params=params, json=payload,
			headers={"user-api-key": key or ""}, timeout=20)
	except (requests.Timeout, requests.ConnectionError) as exc:
		raise ChannexError(0, f"{type(exc).__name__}: {exc}") from None
	headers = {str(k).lower(): v for k, v in (res.headers or {}).items()}
	if 200 <= res.status_code < 300:
		try:
			return res.json(), headers
		except ValueError:
			return {}, headers
	try:
		body = res.json()
		detail = str(body.get("errors") or body) if isinstance(body, dict) else str(body)
	except ValueError:
		detail = res.text or ""
	if res.status_code == 429:
		raise ChannexRateLimited(429, detail, _retry_after(headers.get("retry-after")))
	raise ChannexError(res.status_code, detail)


def _request(conn, method: str, path: str, *, params: dict | None = None,
             payload=None) -> dict:
	"""One call to the Channex API: the JSON of a 2xx, ChannexRateLimited on a
	429, ChannexError on anything else."""
	return _send(conn, method, path, params=params, payload=payload)[0]


def rate_limit_wait(headers: dict) -> int:
	"""Seconds to hold back according to the `RateLimit` header
	("<name>";r=<remaining>;t=<seconds>, ...): the longest t among the
	entries with nothing remaining, 0 when every one has some left. Names
	change, so it is read generically; a header that does not parse is 0."""
	wait = 0
	for entry in str((headers or {}).get("ratelimit") or "").split(","):
		fields = {}
		for part in entry.split(";")[1:]:
			key, _, value = part.strip().partition("=")
			fields[key] = value
		try:
			remaining, seconds = int(fields["r"]), int(fields["t"])
		except (KeyError, ValueError):
			continue
		if remaining <= 0:
			wait = max(wait, seconds)
	return wait


def _attributes(item: dict) -> dict:
	return dict(item.get("attributes") or {}, id=item.get("id"))


_ARI_ID_FIELD = {"availability": "room_type_id", "restrictions": "rate_plan_id"}


_INT_RESTRICTION_FIELDS = frozenset({"availability", "min_stay_arrival", "min_stay_through", "max_stay"})
_BOOL_RESTRICTION_FIELDS = frozenset({"stop_sell", "closed_to_arrival", "closed_to_departure"})


def _ari_value(field: str, value, quantum: Decimal):
	if field == "rate":
		# a decimal string with the currency's decimals (none for UGX, three
		# for KWD): Channex refuses a bare float with decimals
		return str(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))
	if field in _INT_RESTRICTION_FIELDS:
		return int(value)
	if field in _BOOL_RESTRICTION_FIELDS:
		return bool(value)
	return value


def _change_key(id_field: str, obj: dict) -> tuple:
	return (str(obj.get(id_field)), obj.get("date"), obj.get("date_from"), obj.get("date_to"))


def push_ari_changes(conn, kind: str, changes: list[dict]) -> dict:
	"""Send ARI changes, in the order given, in ONE call: kind is
	"availability" or "restrictions", changes the output of
	channel_ari.compress ({external_id, date | date_from + date_to, values}).
	Returns {"task_ids", "rejected": [change + "reasons": {field: [why]}],
	"wait": seconds the RateLimit header asks to hold back}. A rejected value
	does not fail the call: Channex applies the others and lists it under
	meta.warnings. ChannexRateLimited on a 429, ChannexError otherwise."""
	from kamra.channel_ari import rate_quantum
	id_field = _ARI_ID_FIELD[kind]
	prop = conn.get("property")
	default_quantum = rate_quantum(prop)
	currencies = {}
	if kind == "restrictions" and conn.get("name") and prop:
		import frappe
		if getattr(frappe, "db", None):
			from kamra.channel_manager import _mappings
			currencies = {str(m.external_rate_id): m.get("external_currency")
			              for m in _mappings(conn.name) if m.external_rate_id and m.get("external_currency")}
	values, by_key = [], {}
	for change in changes:
		obj = {"property_id": conn.external_property_id, id_field: change["external_id"]}
		if change.get("date"):
			obj["date"] = change["date"]
		else:
			obj["date_from"], obj["date_to"] = change["date_from"], change["date_to"]
		currency = currencies.get(str(change["external_id"]))
		quantum = rate_quantum(prop, currency) if currency else default_quantum
		for field, value in (change.get("values") or {}).items():
			if value is None:
				continue
			obj[field] = _ari_value(field, value, quantum)
		values.append(obj)
		by_key.setdefault(_change_key(id_field, obj), change)
	body, headers = _send(conn, "POST", kind, payload={"values": values})
	rejected = []
	for warning in (body.get("meta") or {}).get("warnings") or []:
		change = by_key.get(_change_key(id_field, warning)) or {
			"external_id": warning.get(id_field), "date": warning.get("date"),
			"date_from": warning.get("date_from"), "date_to": warning.get("date_to"), "values": {}}
		rejected.append(dict(change, reasons=warning.get("warning") or {}))
	return {"task_ids": [d.get("id") for d in body.get("data") or [] if d.get("id")],
	        "rejected": rejected, "wait": rate_limit_wait(headers)}


def _all_pages(conn, path: str, params: dict) -> list[dict]:
	items, page = [], 1
	while True:
		body = _request(conn, "GET", path, params=dict(
			params, **{"pagination[page]": page, "pagination[limit]": 100}))
		data = body.get("data") or []
		items += data
		total = (body.get("meta") or {}).get("total")
		if len(data) < 100 or (total is not None and len(items) >= int(total)):
			return items
		page += 1


def fetch_property_details(conn) -> dict:
	"""The property, its room types and their rate plans, for the mapping
	import: {hotel_id, hotel_name, currency, timezone, per_rate_plan: True,
	rooms: [{room_id, room_name, count_of_rooms, rateplans: [{rateplan_id,
	rateplan_name, sell_mode, currency}]}]}. Channex prices each rate plan, so
	the import maps one Kamra plan to each."""
	pid = conn.external_property_id
	prop = _attributes(_request(conn, "GET", f"properties/{pid}").get("data") or {})
	scope = {"filter[property_id]": pid}
	rooms, by_id = [], {}
	for item in _all_pages(conn, "room_types", scope):
		attrs = item.get("attributes") or {}
		room = {"room_id": str(item.get("id")), "room_name": attrs.get("title"),
		        "count_of_rooms": attrs.get("count_of_rooms"), "rateplans": []}
		rooms.append(room)
		by_id[room["room_id"]] = room
	for item in _all_pages(conn, "rate_plans", scope):
		attrs = item.get("attributes") or {}
		room_id = (((item.get("relationships") or {}).get("room_type") or {}).get("data") or {}).get("id")
		room = by_id.get(str(room_id))
		if room is None:
			continue
		room["rateplans"].append({"rateplan_id": str(item.get("id")), "rateplan_name": attrs.get("title"),
		                          "sell_mode": attrs.get("sell_mode"), "currency": attrs.get("currency")})
	return {"hotel_id": prop.get("id") or pid, "hotel_name": prop.get("title"),
	        "currency": prop.get("currency"), "timezone": prop.get("timezone"),
	        "per_rate_plan": True, "rooms": rooms}


def push_ari(conn, snapshot) -> tuple[bool, str]:
	"""The seam's whole-snapshot push, as ranges, in two calls at most (one
	availability, one restrictions). Channex connections normally go through
	the outbox (kamra.channel_ari), which sends only what changed."""
	from kamra.channel_ari import compress
	availability, rates = {}, {}
	for rt in snapshot:
		room = availability.setdefault(rt["external_room_id"], {})
		for d in rt["days"]:
			room[d["date"]] = {"availability": d["available"]}
			if rt.get("external_rate_id"):
				cell: dict = {}
				if float(d.get("rate") or 0) > 0:
					cell["rate"] = d["rate"]
				if "stop_sell" in d:
					cell["stop_sell"] = bool(d["stop_sell"])
				if d.get("min_los") or d.get("min_stay_arrival"):
					cell["min_stay_arrival"] = int(d.get("min_stay_arrival") or d.get("min_los"))
				if d.get("max_los") or d.get("max_stay"):
					cell["max_stay"] = int(d.get("max_stay") or d.get("max_los"))
				if "cta" in d or "closed_to_arrival" in d:
					cell["closed_to_arrival"] = bool(d.get("closed_to_arrival", d.get("cta")))
				if "ctd" in d or "closed_to_departure" in d:
					cell["closed_to_departure"] = bool(d.get("closed_to_departure", d.get("ctd")))
				if cell:
					rates.setdefault(rt["external_rate_id"], {})[d["date"]] = cell
	sent = {}
	try:
		for kind, cells in (("availability", availability), ("restrictions", rates)):
			changes = compress(cells)
			if changes:
				push_ari_changes(conn, kind, changes)
				sent[kind] = sum(len(c) for c in cells.values())
	except ChannexError as exc:
		return False, exc.detail[:280] or str(exc)[:280]
	return True, f"{sent.get('availability', 0)} availability, {sent.get('restrictions', 0)} rate values"


def _is_cjk_char(ch: str) -> bool:
	"""Han ideographs, Japanese Kana, Korean Hangul, and CJK iteration mark:
	scripts written without spaces between words."""
	cp = ord(ch)
	return (
		cp == 0x3005
		or 0x3040 <= cp <= 0x30FF
		or 0x31F0 <= cp <= 0x31FF
		or 0x3400 <= cp <= 0x4DBF
		or 0x4E00 <= cp <= 0x9FFF
		or 0xAC00 <= cp <= 0xD7AF
		or 0xF900 <= cp <= 0xFAFF
		or 0x20000 <= cp <= 0x323AF
	)


def _char_kind(ch: str) -> str | None:
	cat = unicodedata.category(ch)[0]
	if cat == "N":
		return "N"
	if cat in "LM":
		return "C" if _is_cjk_char(ch) else "L"
	return None


def is_cjk_word(word: str) -> bool:
	return bool(word) and all(_is_cjk_char(ch) for ch in word)


_is_cjk_word = is_cjk_word


def tax_words(value) -> list[str]:
	"""A tax label as its words: runs of letters/marks or digits in any
	script, case folded, split at letter/digit and CJK/non-CJK boundaries
	("VAT18%" -> ["vat", "18"], "增值税6%" -> ["增值税", "6"], "Hotel增值税" ->
	["hotel", "增值税"]). Punctuation and spaces only part them."""
	words, word, kind = [], [], None
	for ch in unicodedata.normalize("NFKC", str(value or "")).casefold():
		k = _char_kind(ch)
		if k is None:
			if word:
				words.append("".join(word))
				word, kind = [], None
		elif k == kind:
			word.append(ch)
		else:
			if word:
				words.append("".join(word))
			word, kind = [ch], k
	if word:
		words.append("".join(word))
	return words


def _rule_matches(rule, label: list[str]) -> bool:
	"""Is: the label is the rule's words. Contains: they appear in it, in
	order and whole ("vat" is in "VAT (18%)" and "VAT18%", not in "Private").
	CJK phrases carry no spaces, so a CJK Contains rule matches as a substring
	within CJK runs ("增值税" in "酒店增值税"), while non-CJK stays whole-word
	and unmatched exclusive taxes still fail closed."""
	want = tax_words(rule.get("match_text"))
	if not want or not label:
		return False
	if rule.get("match_type") == "Is":
		return label == want
	if any(label[i:i + len(want)] == want for i in range(len(label) - len(want) + 1)):
		return True
	if not any(_is_cjk_word(w) for w in want):
		return False
	if all(_is_cjk_word(w) for w in want):
		needle = "".join(want)
		run = []
		for tok in label:
			if _is_cjk_word(tok):
				run.append(tok)
				if needle in "".join(run):
					return True
			else:
				run = []
		return False
	for i in range(len(label) - len(want) + 1):
		window = label[i:i + len(want)]
		ok = True
		for got, exp in zip(window, want, strict=True):
			if got == exp:
				continue
			if is_cjk_word(exp) and is_cjk_word(got) and exp in got:
				continue
			ok = False
			break
		if ok:
			return True
	return False


def _counts_as(conn, tax: dict) -> str | None:
	"""What the connection's OTA tax rules (Channel Manager Connection >
	OTA tax rules) read a booking room's taxes[] entry as: "Room tax" (the
	tax the folio bills with the room), "Ignored" (the OTA's own: a fee, a
	city tax), or None when no rule reads it. The first rule its type
	and/or name matches decides. Channex relays an OTA's labels as it gets
	them (BookingRoomTax: type a string or null, name a string), so both
	are read."""
	labels = {"Type": tax_words(tax.get("type")), "Name": tax_words(tax.get("name"))}
	for rule in conn.get("ota_tax_rules") or []:
		on = rule.get("match_on") or "Type or Name"
		fields = ("Type", "Name") if on == "Type or Name" else (on,)
		if any(_rule_matches(rule, labels.get(f) or []) for f in fields):
			return rule.get("counts_as")
	return None


def _owed(tax: dict) -> bool:
	"""A tax the guest owes the property on this booking: an amount above
	nothing that the OTA did not withhold. Channex marks is_withheld on the
	taxes an OTA collected and remits itself (docs.channex.io changelog,
	Airbnb tax_withholding_details; bookings collection, collected taxes):
	those are not the property's to bill."""
	return float(tax.get("total_price") or 0) > 0 and not tax.get("is_withheld")


def _tax_on_top(conn, room: dict) -> float:
	"""The room tax a booking room lists on top of its day prices
	(is_inclusive false - docs.channex.io, bookings collection: Expedia
	"Total Amount Excluding Tax", Agoda "Exclusive"); 0 when the prices hold
	it. A tax the OTA withheld is not the property's to bill."""
	return sum(float(tax.get("total_price") or 0) for tax in room.get("taxes") or []
	           if tax.get("is_inclusive") is False and _owed(tax) and _counts_as(conn, tax) == "Room tax")


def _unread_taxes(conn, room: dict) -> list[str]:
	"""The taxes a booking room lists on top of its day prices (is_inclusive
	false), owed to the property, that no OTA tax rule reads, as "name
	(amount)". The guest paid them on top: they are not in the price, so
	the booking is held until a rule says what they are. One inside the
	price (is_inclusive true) is already in what the folio bills."""
	return [f"{tax.get('name') or tax.get('type') or 'tax'} ({tax.get('total_price')})"
	        for tax in room.get("taxes") or []
	        if tax.get("is_inclusive") is False and _owed(tax) and _counts_as(conn, tax) is None]


def parse_webhook(conn, payload) -> list[dict]:
	"""Channex booking webhook -> normalized events.

	Payload: {event: "booking", payload: {status, ota_reservation_code,
	ota_name, arrival_date, departure_date, currency, payment_collect,
	guarantee{is_virtual}, customer{...}, rooms[{room_type_id, rate_plan_id,
	days{date: amount}, ...}]}}. payment_collect is "ota" (the OTA takes the
	money: prepaid, or a virtual card) or "property".

	A room's total is what the guest paid for it: its day prices, plus the
	room tax when the channel lists it on top of them (then the day prices
	are before tax: nightly_net). room_tax_listed says whether its taxes[]
	name a room tax owed (above nothing, not withheld): a booking does not
	carry its OTA's tax setting, so one from an OTA set to amounts before
	tax that lists none is held for review (channel_manager, the
	connection's OTA tax basis). unread_taxes are the taxes on top no rule
	reads, and tax_rules whether the connection has any rules at all: either
	holds the booking for review too (channel_manager.tax_hold)."""
	body = payload.get("payload") or payload
	status = (body.get("status") or "new").lower()
	event = {"new": "book", "modified": "modify",
	         "cancelled": "cancel"}.get(status, "book")
	customer = body.get("customer") or {}
	collect = str(body.get("payment_collect") or "").strip().lower()
	virtual = bool((body.get("guarantee") or {}).get("is_virtual"))
	out = []
	for room in body.get("rooms") or [{}]:
		days = room.get("days") or {}
		on_top = _tax_on_top(conn, room)
		total = (sum(float(v or 0) for v in days.values())
		         or float(body.get("amount") or 0)) + on_top
		occ = room.get("occupancy") or {}
		out.append({
			"event": event,
			"ota_ref": body.get("ota_reservation_code")
			           or body.get("unique_id") or "",
			"channel": body.get("ota_name") or "OTA",
			"room_type_external_id": room.get("room_type_id") or "",
			"rate_plan_external_id": str(room.get("rate_plan_id") or ""),
			"payment_collect": collect,
			"virtual_card": virtual,
			"check_in": room.get("checkin_date") or body.get("arrival_date"),
			"check_out": room.get("checkout_date") or body.get("departure_date"),
			"adults": int(occ.get("adults") or 2),
			"children": int(occ.get("children") or 0),
			"guest_name": " ".join(filter(None, [customer.get("name"),  # nosemgrep: frappe-no-functional-code -- filter(None, ...) drops empty parts; equivalent to a comprehension
			                                     customer.get("surname")]))
			              or "OTA Guest",
			"phone": customer.get("phone") or "",
			"email": customer.get("mail") or customer.get("email") or "",
			"total": total,
			"nightly": [{"date": k, "amount": float(v or 0)} for k, v in sorted(days.items())],
			"nightly_net": bool(on_top),
			"room_tax_listed": any(_owed(t) and _counts_as(conn, t) == "Room tax"
			                       for t in room.get("taxes") or []),
			"unread_taxes": _unread_taxes(conn, room),
			"tax_rules": bool(conn.get("ota_tax_rules")),
			"currency": body.get("currency") or "",
			"notes": body.get("notes") or "",
		})
	return out
