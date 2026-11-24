"""Opera-style cashier till: open/close sessions, float, paid-outs, drops,
and a unified cash trail for front-office + POS.

Money actions elsewhere call record_cashier_txn(...) after resolving the
caller's open session. Agents bypass the session gate (same rule as PIN)."""

from __future__ import annotations

import re
from typing import Any

import frappe
from frappe.utils import now_datetime

from kamra.authz import require_roles
from kamra.business_date import ensure_business_date, get_business_date
from kamra.business_date import report_range_end as _range_end
from kamra.settings import get_setting

_FX_NOTE_RE = re.compile(r"\b([A-Z]{3})\s+([0-9][0-9,]*(?:\.[0-9]+)?)\s*@")


def _txn_has_currency_cols() -> bool:
	try:
		return bool(
			frappe.db.table_exists("Cashier Transaction")
			and frappe.db.has_column("Cashier Transaction", "currency")
			and frappe.db.has_column("Cashier Transaction", "fx_amount")
		)
	except Exception:
		return False


def _session_has_foreign_count_col() -> bool:
	try:
		return bool(
			frappe.db.table_exists("Cashier Session")
			and frappe.db.has_column("Cashier Session", "foreign_cash_count")
		)
	except Exception:
		return False


def parse_allowed_modes(raw) -> list[str] | None:
	"""Parse Cashier.allowed_modes (comma, newline, or semicolon separated).
	Empty/blank returns None (unrestricted)."""
	if raw is None:
		return None
	if isinstance(raw, (list, tuple)):
		parts = [str(x).strip() for x in raw]
	else:
		parts = [p.strip() for p in re.split(r"[,;\n]+", str(raw))]
	cleaned = list(dict.fromkeys(p for p in parts if p))
	return cleaned or None


def cashier_allowed_modes(property: str, user: str | None = None) -> list[str] | None:
	"""Explicit mode allowlist on the user's active Cashier record for `property`,
	or None when unrestricted (blank allowed_modes, no Cashier row, or admin/agent)."""
	if not getattr(frappe.local, "db", None):
		return None
	if user is None:
		session = getattr(frappe.local, "session", None)
		if not session or not getattr(session, "user", None):
			return None
		if _agent_bypass():
			return None
		user = session.user
	if not property or not user or user in ("Administrator", "Guest"):
		return None
	raw = frappe.db.get_value(
		"Cashier",
		{"property": property, "user": user, "disabled": 0},
		"allowed_modes",
	)
	return parse_allowed_modes(raw)


def filter_allowed_modes(property: str, modes: list[str], user: str | None = None) -> list[str]:
	allowed = cashier_allowed_modes(property, user)
	if allowed is None:
		return list(modes)
	allowed_set = set(allowed)
	return [m for m in modes if m in allowed_set]


def assert_cashier_mode_allowed(property: str, mode: str | None, user: str | None = None) -> None:
	if not mode:
		return
	if user is None and _agent_bypass():
		return
	allowed = cashier_allowed_modes(property, user)
	if allowed is not None and mode not in allowed:
		frappe.throw(
			frappe._("Your cashier profile is not permitted to take {0}. Allowed modes: {1}.").format(
				mode, ", ".join(allowed),
			),
			frappe.PermissionError,
		)


def _parse_foreign_counts(raw) -> dict[str, float]:
	"""Normalize foreign_counts ({USD: 14} or [{currency: 'USD', counted_fx: 14}])."""
	if raw in (None, "", [], {}):
		return {}
	if isinstance(raw, str):
		raw = frappe.parse_json(raw)
	if not raw:
		return {}
	pairs = raw.items() if isinstance(raw, dict) else (
		(r.get("currency"), r.get("counted_fx", r.get("counted", r.get("amount")))) for r in raw
	)
	out: dict[str, float] = {}
	for ccy, val in pairs:
		code = str(ccy or "").strip().upper()
		if len(code) != 3 or not code.isascii() or not code.isalpha():
			frappe.throw(frappe._("Each foreign currency count needs a 3-letter ISO currency code."))
		if val in (None, ""):
			continue
		try:
			num = float(val)
		except (TypeError, ValueError):
			frappe.throw(frappe._("Foreign cash count for {0} must be a number.").format(code))
		if num < 0:
			frappe.throw(frappe._("Foreign cash count for {0} cannot be negative.").format(code))
		out[code] = round(num, 2)
	return out


def _session_foreign_drawers(
	session_names: list[str],
	property: str,
	*,
	count_overrides: dict[str, dict[str, float]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
	"""OPERA Cashier Closure / SAP FBCJ per-currency foreign cash drawers for one
	or more sessions. Combines FX cash Cashier Transactions (any Cash or Foreign Cash
	tender) and Exchange Transactions (Buy adds FX notes, Sell removes FX notes)."""
	names = [n for n in session_names if n]
	if not names:
		return {}
	from kamra.ledger import home_currency
	from kamra.masters import client_tender_types, payment_tender_type

	home = home_currency(property)
	tender_map = client_tender_types(property)
	has_ccy = _txn_has_currency_cols()
	has_fc_col = _session_has_foreign_count_col()

	fields = ["session", "kind", "mode", "amount", "notes"]
	if has_ccy:
		fields.extend(["currency", "fx_amount"])
	txns = frappe.get_all(
		"Cashier Transaction",
		filters={"session": ("in", names), "kind": ("in", ["Payment", "Refund"])},
		fields=fields,
		limit_page_length=0,
	)

	buckets: dict[str, dict[str, dict[str, Any]]] = {n: {} for n in names}

	def _slot(sess_name: str, code: str) -> dict[str, Any]:
		return buckets.setdefault(sess_name, {}).setdefault(
			code,
			{
				"currency": code,
				"receipts_fx": 0.0,
				"refunds_fx": 0.0,
				"expected_fx": 0.0,
				"home_equivalent": 0.0,
				"txns": 0,
			},
		)

	for r in txns:
		mode = r.mode or "Other"
		tt = tender_map.get(mode) or payment_tender_type(mode, property)
		if tt not in ("Cash", "Foreign Cash"):
			continue
		ccy = (r.get("currency") or "").strip().upper() if has_ccy else ""
		fx_amt = float(r.get("fx_amount") or 0) if has_ccy else 0.0
		if (not ccy or ccy == home or not fx_amt) and tt == "Foreign Cash":
			m = _FX_NOTE_RE.search(r.notes or "")
			if m:
				ccy = m.group(1).upper()
				raw_fx = float(m.group(2).replace(",", ""))
				fx_amt = -raw_fx if float(r.amount or 0) < 0 else raw_fx
		if not ccy or ccy == home or not fx_amt:
			continue
		slot = _slot(r.session, ccy)
		if fx_amt >= 0:
			slot["receipts_fx"] += fx_amt
		else:
			slot["refunds_fx"] += abs(fx_amt)
		slot["expected_fx"] += fx_amt
		slot["home_equivalent"] += float(r.amount or 0)
		slot["txns"] += 1

	if frappe.db.table_exists("Exchange Transaction"):
		ex_rows = frappe.get_all(
			"Exchange Transaction",
			filters={"session": ("in", names)},
			fields=["session", "direction", "currency", "fx_amount", "local_amount"],
			limit_page_length=0,
		)
		for ex in ex_rows:
			ccy = (ex.currency or "").strip().upper()
			fx_val = float(ex.fx_amount or 0)
			if not ccy or ccy == home or not fx_val:
				continue
			slot = _slot(ex.session, ccy)
			if ex.direction == "Buy":
				slot["receipts_fx"] += fx_val
				slot["expected_fx"] += fx_val
				slot["home_equivalent"] += float(ex.local_amount or 0)
			else:
				slot["refunds_fx"] += fx_val
				slot["expected_fx"] -= fx_val
				slot["home_equivalent"] -= float(ex.local_amount or 0)
			slot["txns"] += 1

	stored_counts: dict[str, dict[str, float]] = {}
	if has_fc_col:
		for s in frappe.get_all(
			"Cashier Session",
			filters={"name": ("in", names)},
			fields=["name", "foreign_cash_count"],
			limit_page_length=0,
		):
			if s.foreign_cash_count:
				try:
					stored_counts[s.name] = _parse_foreign_counts(s.foreign_cash_count)
				except Exception:
					stored_counts[s.name] = {}

	out: dict[str, list[dict[str, Any]]] = {}
	for n in names:
		counts = (count_overrides or {}).get(n)
		if counts is None:
			counts = stored_counts.get(n) or {}
		sess_map = buckets.get(n) or {}
		for ccy in counts:
			if ccy != home and ccy not in sess_map:
				_slot(n, ccy)
		drawers: list[dict[str, Any]] = []
		for ccy in sorted(sess_map):
			s = sess_map[ccy]
			exp_fx = round(s["expected_fx"], 2)
			counted_fx = counts.get(ccy)
			drawers.append({
				"currency": ccy,
				"receipts_fx": round(s["receipts_fx"], 2),
				"refunds_fx": round(s["refunds_fx"], 2),
				"expected_fx": exp_fx,
				"home_equivalent": round(s["home_equivalent"], 2),
				"counted_fx": counted_fx,
				"variance_fx": round(counted_fx - exp_fx, 2) if counted_fx is not None else None,
				"txns": s["txns"],
			})
		out[n] = drawers
	return out


def _fx_variance_home(drawer: dict[str, Any], property: str) -> float:
	var_fx = drawer.get("variance_fx")
	if var_fx is None or abs(float(var_fx)) <= 0.004:
		return 0.0
	exp_fx = float(drawer.get("expected_fx") or 0)
	home_eq = float(drawer.get("home_equivalent") or 0)
	if abs(exp_fx) > 0.004 and abs(home_eq) > 0.004:
		rate = abs(home_eq / exp_fx)
		return float(var_fx) * rate
	try:
		from kamra.ledger import payment_rate
		return float(var_fx) * float(payment_rate(property, drawer["currency"])["rate"])
	except Exception:
		return float(var_fx)


def _agent_bypass() -> bool:
	if getattr(frappe.flags, "kamra_agent_call", False) or \
	   getattr(frappe.flags, "kamra_gate_bypass", False):
		return True
	if "Kamra Agent" in frappe.get_roles():
		return True
	if frappe.session.user == "Administrator":
		return True
	return False


def _cashier_for_user(property: str, user: str | None = None) -> str | None:
	user = user or frappe.session.user
	name = frappe.db.get_value(
		"Cashier",
		{"property": property, "user": user, "disabled": 0},
		"name",
	)
	return name


def ensure_cashier(property: str, user: str | None = None) -> str:
	"""Auto-provision a Cashier ID for the user on first till open."""
	user = user or frappe.session.user
	existing = _cashier_for_user(property, user)
	if existing:
		return existing
	# Stable short id from username
	base = (user.split("@")[0] or "csh")[:12].upper().replace(".", "")
	cid = f"{base}-{property[:8]}".upper()
	n = 1
	while frappe.db.exists("Cashier", {"cashier_id": cid}):
		n += 1
		cid = f"{base}{n}-{property[:8]}".upper()
	doc = frappe.get_doc({
		"doctype": "Cashier",
		"cashier_id": cid,
		"user": user,
		"property": property,
		"is_floating": 0,
		"max_cash_refund": get_setting(property, "max_cash_refund"),
	})
	doc.insert(ignore_permissions=True)
	return doc.name


def _open_session_names(property: str, user: str) -> list[str]:
	"""The user's open tills here, newest first. Usually one; a person
	covering two stations holds one per terminal."""
	return frappe.get_all(
		"Cashier Session",
		filters={"property": property, "user": user, "status": "Open"},
		pluck="name", order_by="opened_at desc, creation desc")


def _pick_session(names: list[str], outlet: str | None = None) -> str | None:
	"""Of several open tills, the one serving `outlet` (a POS bill goes to the
	till of its outlet), else the one opened last."""
	if outlet and len(names) > 1:
		for n in names:
			if frappe.db.get_value("Cashier Session", n, "outlet") == outlet:
				return n
	return names[0] if names else None


# who may open their own till (open_session); admins always may
TILL_OPENERS = ("Front Desk", "Finance", "Kamra Agent")


def tills_required(property: str | None) -> bool:
	"""Does this property take money through cashier tills? On by default.
	A small place with no cash drawer turns it off (Settings > Finance):
	payments are then recorded without a till, no till can be opened
	(open_session), and the till screens (paid-outs, drops, petty cash,
	exchange) are unavailable."""
	return bool(get_setting(property, "use_cashier_tills"))


def can_open_till() -> bool:
	from kamra.authz import ADMIN
	return bool((set(TILL_OPENERS) | set(ADMIN)) & set(frappe.get_roles()))


def till_state(property: str) -> dict[str, Any]:
	"""What a money screen needs to know about the caller's till: is one
	needed, is it open, and can this user open it here (open_session)."""
	required = tills_required(property)
	session = None if _agent_bypass() else _pick_session(
		_open_session_names(property, frappe.session.user))
	return {
		"property": property,
		"required": required,
		"open": bool(session) or _agent_bypass() or not required,
		"session": session,
		"can_open": can_open_till(),
		"who_can_open": ["Front Desk", "Finance", "Hotel Admin"],
		"business_date": str(get_business_date(property) or ""),
	}


def require_open_session(property: str, outlet: str | None = None,
                         till_only: bool = False) -> str | None:
	"""Return open session name, or None when agents bypass. Throws otherwise.

	`outlet`: which till to take when the user holds several.
	A property that does not use tills (use_cashier_tills off) records
	payments without one: None comes back instead of a refusal, unless
	`till_only` (a paid-out, drop, petty cash or exchange, which only make
	sense against a till)."""
	if _agent_bypass():
		# Prefer an open session if one exists; else None (txn still recorded
		# without session when agents act).
		sess = frappe.db.get_value(
			"Cashier Session",
			{"property": property, "status": "Open"},
			"name",
			order_by="opened_at desc",
		)
		return sess
	sess = _pick_session(_open_session_names(property, frappe.session.user), outlet)
	if not sess:
		if not till_only and not tills_required(property):
			return None
		frappe.throw(
			"NO_OPEN_SESSION: open your till under Finance → Cashier first.",
		)
	return sess


_TOTAL_FIELDS = ("system_cash", "system_card", "system_upi", "system_other",
                 "paid_outs", "drops", "petty_cash", "expected_cash")


def _recompute_session_totals(session: str, persist: bool = True) -> dict[str, float]:
	"""Re-add the session's transactions into its stored totals. The row is
	written only when a total actually moved (and never on Closed sessions):
	a screen that merely reads the till (current_session, on every load) must
	not write, or two loads at once race on the same row (MariaDB 1020)."""
	has_ccy = _txn_has_currency_cols()
	if has_ccy:
		rows = frappe.db.sql(
			"""
			SELECT kind, mode, amount, currency, fx_amount
			FROM `tabCashier Transaction`
			WHERE session = %s
			""",
			session, as_dict=True,
		)
	else:
		rows = frappe.db.sql(
			"""
			SELECT kind, mode, amount
			FROM `tabCashier Transaction`
			WHERE session = %s
			""",
			session, as_dict=True,
		)
	sess = frappe.get_doc("Cashier Session", session)
	# the pack's instant modes keep their own bucket, stored
	# in system_upi / "upi" - field kept by name so existing tills read on
	from kamra.ledger import home_currency
	from kamra.localization import instant_payment_modes, pack_for
	from kamra.masters import client_tender_types, payment_tender_type

	home = home_currency(sess.property)
	instant_modes = instant_payment_modes(pack_for(sess.property), sess.property)
	# a hotel's own payment method counts as its tender type (switched-off
	# ones too, so a till that took one still adds up)
	tender = client_tender_types(sess.property)
	cash = card = upi = other = foreign = paid_outs = drops = petty = 0.0
	for r in rows:
		amt = float(r.amount or 0)
		kind = r.kind
		mode = r.mode or "Other"
		if kind == "Paid Out":
			paid_outs += abs(amt)
		elif kind == "Drop":
			drops += abs(amt)
		elif kind == "Petty Cash":
			petty += abs(amt)
		elif kind in ("Payment", "Exchange", "Refund"):
			tt = tender.get(mode) or payment_tender_type(mode, sess.property)
			row_ccy = (r.get("currency") or "").strip().upper() if has_ccy else ""
			is_fx = bool(has_ccy and ((row_ccy and row_ccy != home) or float(r.get("fx_amount") or 0)))
			if tt == "Foreign Cash" or (tt == "Cash" and is_fx and kind != "Exchange"):
				foreign += amt  # home value of foreign notes; never in home expected cash
			elif tt == "Cash":
				cash += amt
			elif tt == "Card":
				card += amt
			elif tt == "Instant" or mode in instant_modes:
				upi += amt
			else:
				other += amt

	opening = float(sess.opening_float or 0)
	expected = opening + cash - paid_outs - drops - petty
	fresh = dict(zip(_TOTAL_FIELDS, (cash, card, upi, other, paid_outs, drops,
	                                 petty, expected)))
	if persist and sess.status != "Closed" and any(
		abs(float(sess.get(k) or 0) - v) > 0.004 for k, v in fresh.items()
	):
		sess.update(fresh)
		sess.save(ignore_permissions=True)
	return {
		"cash": cash, "card": card, "upi": upi, "other": other,
		"foreign_cash": foreign, "paid_outs": paid_outs, "drops": drops, "petty_cash": petty,
		"opening_float": opening, "expected_cash": expected,
	}


def record_cashier_txn(
	property: str,
	kind: str,
	mode: str,
	amount: float,
	*,
	folio: str | None = None,
	pos_order: str | None = None,
	reference: str | None = None,
	notes: str | None = None,
	session: str | None = None,
	outlet: str | None = None,
	tender_validated: bool = False,
	currency: str | None = None,
	fx_amount: float | None = None,
) -> str | None:
	"""Append a Cashier Transaction. Returns session name (or None)."""
	if not tender_validated:
		from kamra.localization import assert_pack_payment_mode
		assert_pack_payment_mode(property, mode)
	sess = session
	if not outlet and pos_order:
		outlet = frappe.db.get_value("POS Order", pos_order, "outlet")
	if not sess:
		try:
			sess = require_open_session(property, outlet)
		except Exception:
			if _agent_bypass():
				sess = None
			else:
				raise
	if not sess:
		return None
	bd = get_business_date(property)
	payload: dict[str, Any] = {
		"doctype": "Cashier Transaction",
		"session": sess,
		"property": property,
		"kind": kind,
		"mode": mode or "Cash",
		"amount": float(amount),
		"business_date": bd,
		"folio": folio,
		"pos_order": pos_order,
		"outlet": outlet or frappe.db.get_value("Cashier Session", sess, "outlet"),
		"reference": (reference or "")[:140] or None,
		"notes": notes,
		"posted_by": frappe.session.user,
		"posted_at": now_datetime(),
	}
	if _txn_has_currency_cols():
		from kamra.ledger import home_currency
		home = home_currency(property)
		txn_ccy = (currency or "").strip().upper() or home
		payload["currency"] = txn_ccy
		payload["fx_amount"] = float(fx_amount or 0) if txn_ccy != home else 0.0
	frappe.get_doc(payload).insert(ignore_permissions=True)
	_recompute_session_totals(sess)
	return sess


def session_as_dict(name: str, persist: bool = True) -> dict[str, Any]:
	totals = _recompute_session_totals(name, persist=persist)
	doc = frappe.get_doc("Cashier Session", name)
	txn_fields = ["name", "kind", "mode", "amount", "folio", "pos_order",
	              "reference", "posted_by", "posted_at", "notes"]
	if _txn_has_currency_cols():
		txn_fields.extend(["currency", "fx_amount"])
	txns = frappe.get_all(
		"Cashier Transaction",
		filters={"session": name},
		fields=txn_fields,
		order_by="posted_at desc",
		limit_page_length=200,
	)
	d = doc.as_dict()
	d["totals"] = totals
	d["transactions"] = txns
	d["foreign_drawers"] = _session_foreign_drawers([name], doc.property).get(name, [])
	d["cashier_id"] = frappe.db.get_value("Cashier", doc.cashier, "cashier_id")
	from kamra.localization import cash_denominations, pack_for
	d["denominations"] = cash_denominations(pack_for(doc.property))
	return d


def _session_view(name: str) -> dict[str, Any]:
	"""session_as_dict for a read-only endpoint. Should a concurrent request
	refresh the same session's totals first (MariaDB snapshot isolation
	raises 1020 on the second write), nothing of ours was written yet: start
	a fresh transaction and answer from it without writing."""
	try:
		return session_as_dict(name)
	except frappe.QueryDeadlockError:
		frappe.db.rollback()
		return session_as_dict(name, persist=False)


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Kamra Agent")
def open_session(property: str, opening_float: float | None = None,
                 terminal: str | None = None, cashier_terminal: str | None = None):
	"""Open the caller's till for the current business date.

	`cashier_terminal` picks a configured till. Without it the session is the
	property's Default till, one per user, exactly as before terminals existed.
	A configured terminal has one open session at a time; one user may hold
	several, one per terminal. Refused where the property does not use
	tills (use_cashier_tills off): payments there are recorded without one."""
	if not tills_required(property):
		frappe.throw("Cashier tills are switched off at this property, so there is "
		             "no till to open. Payments are recorded without one. To use a "
		             "till (paid-outs, drops, petty cash, exchange), turn tills on "
		             "under Settings → Finance.")
	ensure_business_date(property)
	user = frappe.session.user
	term = None
	if cashier_terminal:
		term = frappe.db.get_value(
			"Cashier Terminal", cashier_terminal,
			["name", "property", "disabled", "outlet", "default_float", "terminal_name"],
			as_dict=True)
		if not term or term.property != property:
			frappe.throw("That terminal is not at this property.")
		if term.disabled:
			frappe.throw("That terminal is switched off.")
		# The second opener waits here, then the locking read sees the session
		# the first one inserted.
		frappe.db.get_value("Cashier Terminal", term.name, "name", for_update=True)
		taken_rows = frappe.db.sql(
			"""SELECT name, `user` FROM `tabCashier Session`
			   WHERE cashier_terminal = %s AND status = 'Open'
			   FOR UPDATE""",
			term.name, as_dict=True)
		taken = taken_rows[0] if taken_rows else None
		if taken:
			if taken.user == user:
				return session_as_dict(taken.name)
			frappe.throw(f"{term.terminal_name} is already open under {taken.user}. "
			             "Ask them (or a manager) to close it first.")
	else:
		cashier_name = ensure_cashier(property, user)
		frappe.db.get_value("Cashier", cashier_name, "name", for_update=True)
		existing_rows = frappe.db.sql(
			"""SELECT name FROM `tabCashier Session`
			   WHERE property = %s AND `user` = %s AND status = 'Open'
			     AND IFNULL(cashier_terminal, '') = ''
			   FOR UPDATE""",
			(property, user), as_dict=True)
		if existing_rows:
			return session_as_dict(existing_rows[0].name)
	cashier = ensure_cashier(property, user)
	bd = get_business_date(property)
	opening = float(opening_float) if opening_float not in (None, "") else \
		float(term.default_float or 0) if term else 0.0
	doc = frappe.get_doc({
		"doctype": "Cashier Session",
		"property": property,
		"cashier": cashier,
		"user": user,
		"business_date": bd,
		"terminal": (term.terminal_name if term else terminal) or "",
		"cashier_terminal": term.name if term else None,
		"outlet": term.outlet if term else None,
		"status": "Open",
		"opening_float": opening,
		"opened_at": now_datetime(),
		"expected_cash": opening,
	})
	doc.insert(ignore_permissions=True)
	return session_as_dict(doc.name)


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Night Auditor", "Kamra Agent")
def current_session(property: str, cashier_terminal: str | None = None):
	"""Caller's open session, or null. With several open, the newest unless
	`cashier_terminal` names one."""
	user = frappe.session.user
	names = _open_session_names(property, user)
	if cashier_terminal:
		names = [n for n in names
		         if frappe.db.get_value("Cashier Session", n, "cashier_terminal") == cashier_terminal]
	name = names[0] if names else None
	if not name and _agent_bypass():
		name = frappe.db.get_value(
			"Cashier Session",
			{"property": property, "status": "Open"},
			"name",
			order_by="opened_at desc",
		)
	if not name:
		return {
			"open": False,
			"business_date": get_business_date(property),
			"cashier": _cashier_for_user(property),
		}
	mine = _open_session_names(property, user)
	return {"open": True, "open_sessions": mine,
	        "open_tills": [{"name": n, "cashier_terminal": t, "terminal": lbl or "Default"}
	                       for n, t, lbl in (
	                           (n, *frappe.db.get_value("Cashier Session", n,
	                                                    ["cashier_terminal", "terminal"]))
	                           for n in mine)],
	        **_session_view(name)}


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Night Auditor", "Kamra Agent")
def my_till(property: str):
	"""Read-only: does taking money here need a till, is the caller's open,
	and may the caller open one (open_session). The check-in, walk-in and
	check-out sheets read it to offer "Open my till" in place."""
	return till_state(property)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Night Auditor", "Kamra Agent")
def close_session(session: str, counted_cash: float | None = None,
                  notes: str | None = None, pin: str | None = None,
                  cash_count=None, foreign_counts=None):
	"""Count the drawer and close the till. cash_count is the optional
	count by note and coin, {value: count} or [{value, count}]; it must add
	up to counted_cash, and fills it when counted_cash is left out.
	foreign_counts is the optional per-currency foreign drawer shift drop,
	{currency: counted_fx} or [{currency, counted_fx}]."""
	doc = frappe.get_doc("Cashier Session", session)
	if doc.status != "Open":
		frappe.throw("This session is already closed.")
	if doc.user != frappe.session.user and not (
			set(frappe.get_roles()) & {"Hotel Admin", "System Manager",
			                           "Administrator", "Finance",
			                           "Night Auditor"}):
		frappe.throw("Only the owning cashier (or Finance / the night auditor) can close this till.")
	from kamra.authz import require_cashier_pin
	require_cashier_pin(doc.property, pin)
	count = cash_count_rows(doc.property, cash_count)
	fx_counts = _parse_foreign_counts(foreign_counts)
	if count:
		total = round(sum(r["amount"] for r in count), 2)
		if counted_cash in (None, ""):
			counted_cash = total
		elif abs(float(counted_cash) - total) > 0.005:
			from kamra.localization import money_text
			frappe.throw(frappe._("The count by note adds up to {0}, not {1}.").format(
				money_text(doc.property, total), money_text(doc.property, counted_cash)))
	if counted_cash in (None, ""):
		frappe.throw(frappe._("Give the counted cash or the count by note."))
	totals = _recompute_session_totals(session)
	doc.reload()
	doc.cash_count = frappe.as_json(count) if count else None
	if _session_has_foreign_count_col():
		doc.foreign_cash_count = frappe.as_json(fx_counts) if fx_counts else None
	doc.counted_cash = float(counted_cash)
	doc.variance = float(counted_cash) - float(totals["expected_cash"])
	doc.status = "Closed"
	doc.closed_at = now_datetime()
	doc.closed_by = frappe.session.user
	doc.close_notes = (notes or "").strip()[:500] or None
	tol = float(get_setting(doc.property, "till_variance_tolerance") or 0)
	fx_drawers = _session_foreign_drawers(
		[session], doc.property, count_overrides={session: fx_counts},
	).get(session, [])
	fx_over_tol = tol > 0 and any(abs(_fx_variance_home(fd, doc.property)) > tol for fd in fx_drawers)
	if (tol > 0 and abs(doc.variance) > tol) or fx_over_tol:
		# never block a cash count; flag it for a manager instead
		doc.variance_status = "Needs Review"
	doc.save(ignore_permissions=True)
	from kamra.webhooks import emit_session_closed
	emit_session_closed(doc)

	# Derive a Shift Handover row so ops history stays consistent.
	try:
		_sync_shift_handover(doc)
	except Exception:
		frappe.log_error(title="shift handover sync failed")

	return session_as_dict(doc.name)


def cash_count_rows(property: str, raw) -> list[dict]:
	"""A drawer count by note and coin as [{value, count, amount}], largest
	note first. Values must be the country pack's notes and coins (any
	positive value where the pack names none); counts whole and not negative."""
	if raw in (None, "", [], {}):
		return []
	if isinstance(raw, str):
		raw = frappe.parse_json(raw)
	pairs = raw.items() if isinstance(raw, dict) else (
		(r.get("value"), r.get("count")) for r in raw)
	from kamra.localization import cash_denominations, pack_for
	allowed = cash_denominations(pack_for(property))
	counts: dict[float, int] = {}
	for value, count in pairs:
		try:
			value, n = float(value), float(count or 0)
		except (TypeError, ValueError):
			frappe.throw(frappe._("Each note count needs a value and a number."))
		if value <= 0 or n < 0 or n != int(n):
			frappe.throw(frappe._("Count whole notes and coins of a positive value."))
		if allowed and value not in allowed:
			frappe.throw(frappe._("{0} is not a note or coin here. Count in: {1}.").format(
				f"{value:g}", ", ".join(f"{v:g}" for v in allowed)))
		counts[value] = counts.get(value, 0) + int(n)
	return [{"value": v, "count": c, "amount": round(v * c, 2)}
	        for v, c in sorted(counts.items(), reverse=True) if c]


def _sync_shift_handover(sess) -> None:
	from frappe.utils import getdate
	shift_date = getdate(sess.business_date)
	# Map time-of-day roughly: Morning / Evening / Night by hour
	hour = (sess.closed_at or now_datetime()).hour
	slot = "Morning" if hour < 14 else ("Evening" if hour < 22 else "Night")
	# Prefer property-scoped uniqueness via filters if autoname collides
	existing = frappe.db.get_value(
		"Shift Handover",
		{"property": sess.property, "shift_date": shift_date, "shift": slot},
		"name",
	)
	payload = {
		"opening_cash": float(sess.opening_float or 0),
		"cash_collected": float(sess.system_cash or 0),
		"payouts": float(sess.paid_outs or 0) + float(sess.petty_cash or 0),
		"closing_cash": float(sess.counted_cash or 0),
		"status": "Closed",
		"handed_over_to": sess.user,
		"handover_notes": (
			f"Auto from cashier session {sess.name}; variance "
			f"{float(sess.variance or 0):,.2f}"
			+ (f"; {sess.close_notes}" if sess.close_notes else "")
		)[:500],
	}
	if existing:
		doc = frappe.get_doc("Shift Handover", existing)
		doc.update(payload)
		doc.save(ignore_permissions=True)
	else:
		frappe.get_doc({
			"doctype": "Shift Handover",
			"property": sess.property,
			"shift": slot,
			"shift_date": shift_date,
			**payload,
		}).insert(ignore_permissions=True)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Kamra Agent")
def post_paid_out(property: str, amount: float, reason: str,
                  payee: str | None = None, pin: str | None = None):
	"""Cash leaving the drawer (vendor / guest paid-out)."""
	if float(amount) <= 0:
		frappe.throw("Amount must be positive.")
	if not (reason or "").strip():
		frappe.throw("A reason is required.")
	from kamra.authz import require_cashier_pin
	require_cashier_pin(property, pin)
	sess = require_open_session(property, till_only=True)
	record_cashier_txn(
		property, "Paid Out", "Cash", -abs(float(amount)),
		reference=(payee or "")[:80] or None,
		notes=reason.strip()[:200],
		session=sess,
	)
	return session_as_dict(sess) if sess else {"ok": True}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Kamra Agent")
def cash_drop(property: str, amount: float, location: str | None = None,
              pin: str | None = None, to_terminal: str | None = None):
	"""Move cash from the till to a drop safe / general cashier.

	`to_terminal` names a Cash Office terminal: the drop is then a pending
	transfer the cash office confirms when the money arrives. Without it the
	drop is just recorded against `location`, as before."""
	if float(amount) <= 0:
		frappe.throw("Amount must be positive.")
	from kamra.authz import require_cashier_pin
	require_cashier_pin(property, pin)
	office = None
	if to_terminal:
		office = frappe.db.get_value("Cashier Terminal", to_terminal,
		                             ["name", "property", "kind", "terminal_name", "disabled"],
		                             as_dict=True)
		if not office or office.property != property or office.disabled:
			frappe.throw("That cash office is not available at this property.")
		if office.kind != "Cash Office":
			frappe.throw(f"{office.terminal_name} is not a cash office.")
	sess = require_open_session(property, till_only=True)
	where = office.terminal_name if office else (location or "safe")
	record_cashier_txn(
		property, "Drop", "Cash", -abs(float(amount)),
		reference=where[:80],
		notes=f"Cash drop to {where}",
		session=sess,
	)
	if office:
		frappe.get_doc({
			"doctype": "Cash Transfer", "property": property, "kind": "Drop",
			"status": "Pending", "amount": abs(float(amount)),
			"business_date": get_business_date(property),
			"from_session": sess,
			"from_terminal": frappe.db.get_value("Cashier Session", sess, "cashier_terminal") if sess else None,
			"to_terminal": office.name, "requested_by": frappe.session.user,
			"requested_at": now_datetime(),
		}).insert(ignore_permissions=True)
	return session_as_dict(sess) if sess else {"ok": True}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Kamra Agent")
def post_petty_cash(property: str, amount: float, payee: str, category: str,
                    reason: str, pin: str | None = None):
	if float(amount) <= 0:
		frappe.throw("Amount must be positive.")
	if not (payee or "").strip() or not (reason or "").strip():
		frappe.throw("Payee and reason are required.")
	from kamra.authz import assert_property_access, require_cashier_pin
	assert_property_access(property)
	require_cashier_pin(property, pin)
	sess = require_open_session(property, till_only=True)
	bd = get_business_date(property)
	voucher = frappe.get_doc({
		"doctype": "Petty Cash Voucher",
		"property": property,
		"session": sess,
		"voucher_date": bd,
		"payee": payee.strip()[:120],
		"category": category or "Miscellaneous",
		"amount": float(amount),
		"reason": reason.strip()[:500],
		"approver": frappe.session.user,
		"status": "Posted",
	})
	voucher.insert(ignore_permissions=True)
	record_cashier_txn(
		property, "Petty Cash", "Cash", -abs(float(amount)),
		reference=voucher.name,
		notes=f"{payee}: {reason.strip()[:120]}",
		session=sess,
	)
	return {"voucher": voucher.name, "session": session_as_dict(sess) if sess else None}


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Hotel Admin", "Night Auditor", "Kamra Agent")
def petty_cash_report(property: str, limit: int = 50):
	"""Vouchers and totals for one property only — never mixed across hotels."""
	from kamra.authz import assert_property_access
	assert_property_access(property)
	from frappe.utils import getdate, get_first_day
	bd = get_business_date(property)
	today = getdate(bd)
	month_start = get_first_day(today)
	prop_name = frappe.db.get_value("Property", property, "property_name") or property
	imprest = float(frappe.db.get_value("Property", property, "petty_cash_float") or 0)
	vouchers = frappe.get_all(
		"Petty Cash Voucher",
		filters={"property": property, "status": "Posted"},
		fields=["name", "voucher_date", "payee", "category", "amount",
		        "reason", "status", "session"],
		order_by="voucher_date desc, creation desc",
		limit_page_length=int(limit or 50),
	)
	agg = frappe.db.sql(
		"""
		SELECT
			COALESCE(SUM(CASE WHEN voucher_date = %(today)s THEN amount ELSE 0 END), 0) AS today,
			COALESCE(SUM(CASE WHEN voucher_date >= %(month_start)s THEN amount ELSE 0 END), 0) AS month,
			COALESCE(SUM(amount), 0) AS all_time,
			COUNT(*) AS voucher_count
		FROM `tabPetty Cash Voucher`
		WHERE property = %(property)s AND status = 'Posted'
		""",
		{"property": property, "today": today, "month_start": month_start},
		as_dict=True,
	)[0]
	return {
		"property": property,
		"property_name": prop_name,
		"imprest": imprest,
		"business_date": str(bd),
		"spent_today": float(agg.today or 0),
		"spent_month": float(agg.month or 0),
		"spent_all_time": float(agg.all_time or 0),
		"voucher_count": int(agg.voucher_count or 0),
		"vouchers": vouchers,
	}


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Hotel Admin", "Kamra Agent")
def set_petty_cash_float(property: str, amount: float):
	"""Set this property's standing petty-cash imprest (per hotel, not shared)."""
	from kamra.authz import assert_property_access
	assert_property_access(property)
	if float(amount) < 0:
		frappe.throw("Imprest cannot be negative.")
	if not frappe.db.exists("Property", property):
		frappe.throw(f"Unknown property: {property}")
	frappe.db.set_value("Property", property, "petty_cash_float", float(amount))
	return {"property": property, "imprest": float(amount)}


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Hotel Admin", "Night Auditor", "Kamra Agent")
def list_sessions(property: str, business_date: str | None = None,
                  status: str | None = None, to_date: str | None = None,
                  limit: int = 100):
	"""Till sessions, newest first: one business date, or business_date..
	to_date, optionally only Open or Closed ones (at most `limit`, 500 top).
	Each row: name, cashier, cashier_id, user, business_date, status,
	terminal, cashier_terminal, outlet, opening_float, system_cash,
	system_card, system_upi, system_other, paid_outs, drops, petty_cash,
	expected_cash, counted_cash, variance, variance_status,
	variance_approved_by, opened_at, closed_at, closed_by. Read
	session_report for one session's transactions and count."""
	filters: dict[str, Any] = {"property": property}
	if business_date:
		filters["business_date"] = ("between", [business_date,
		                                        _range_end(business_date, to_date)])
	if status:
		filters["status"] = status
	rows = frappe.get_all(
		"Cashier Session",
		filters=filters,
		fields=["name", "cashier", "user", "business_date", "status",
		        "terminal", "cashier_terminal", "outlet", "opening_float",
		        "system_cash", "system_card", "system_upi", "system_other",
		        "paid_outs", "drops", "petty_cash", "expected_cash",
		        "counted_cash", "variance", "variance_status",
		        "variance_approved_by", "opened_at", "closed_at", "closed_by"],
		order_by="opened_at desc",
		limit_page_length=max(1, min(int(limit or 100), 500)),
	)
	drawers_by_sess = _session_foreign_drawers([r.name for r in rows], property)
	for r in rows:
		r["cashier_id"] = frappe.db.get_value("Cashier", r.cashier, "cashier_id")
		r["foreign_drawers"] = drawers_by_sess.get(r.name, [])
	return {"sessions": rows, "business_date": get_business_date(property)}


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Hotel Admin", "Night Auditor", "Kamra Agent")
def session_report(session: str):
	"""One till session for a reconciliation: the session and its totals,
	system vs counted per method, the note-by-note count, every receipt
	and paid-out in time order, the cashier and the supervisor.

	Returns every Cashier Session field (name, property, business_date,
	status, terminal, cashier_terminal, outlet, opening_float, system_cash,
	system_card, system_upi, system_other, paid_outs, drops, petty_cash,
	expected_cash, counted_cash, variance, variance_status, opened_at,
	closed_at, close_notes, user, closed_by, variance_approved_by) plus:

	- `totals`: cash, card, upi, other, foreign_cash, paid_outs, drops,
	  petty_cash, opening_float, expected_cash, recomputed from transactions
	- `by_method`: one row per payment mode, Cash first: mode, receipts,
	  refunds, net, system, counted, variance. Only Cash is counted: its
	  system is the expected drawer (float + cash - paid outs - drops -
	  petty cash); other modes have counted and variance null
	- `cash_count`: [{value, count, amount}], largest note first; empty when
	  the drawer was counted as one total
	- `denominations`: the country pack's notes and coins
	- `receipts`: Payment, Refund and Exchange transactions, oldest first
	- `paid_outs`: Paid Out, Drop and Petty Cash transactions, oldest first
	- each transaction: name, kind, mode, amount (refunds and paid outs
	  negative), business_date, folio, pos_order, outlet, reference, notes,
	  posted_by, posted_at, guest_name, reservation
	- `transactions`: the newest 200, as the till screen shows them
	- `cashier_id`, and `cashier_user`, `closed_by_user`, `supervisor` as
	  {user, full_name} or null (supervisor: who approved a variance)"""
	d = _session_view(session)
	txn_fields = ["name", "kind", "mode", "amount", "business_date", "folio",
	              "pos_order", "outlet", "reference", "notes", "posted_by",
	              "posted_at"]
	if _txn_has_currency_cols():
		txn_fields.extend(["currency", "fx_amount"])
	txns = frappe.get_all(
		"Cashier Transaction", filters={"session": session},
		fields=txn_fields,
		order_by="posted_at asc, creation asc")
	folios = {t.folio for t in txns if t.folio}
	guests = {f.name: f for f in frappe.get_all(
		"Folio", filters={"name": ("in", list(folios))},
		fields=["name", "guest_name", "reservation", "invoice_number"])} if folios else {}
	for t in txns:
		f = guests.get(t.folio)
		t["guest_name"] = f.guest_name if f else None
		t["reservation"] = f.reservation if f else None
	receipts = [t for t in txns if t.kind in ("Payment", "Refund", "Exchange")]
	methods: dict[str, dict] = {}
	for t in receipts:
		m = methods.setdefault(t.mode or "Other", {
			"mode": t.mode or "Other", "receipts": 0.0, "refunds": 0.0, "net": 0.0})
		amt = float(t.amount or 0)
		m["receipts" if amt >= 0 else "refunds"] += abs(amt)
		m["net"] += amt
	closed = d.get("status") == "Closed"
	from kamra.masters import default_mode_for_tender
	cash_mode = default_mode_for_tender(d.get("property"), "Cash")
	cash = methods.setdefault(cash_mode, {"mode": cash_mode, "receipts": 0.0,
	                                      "refunds": 0.0, "net": 0.0})
	for m in methods.values():
		for k in ("receipts", "refunds", "net"):
			m[k] = round(m[k], 2)
		# only the cash drawer is counted; other methods are as the system has them
		m["system"] = m["net"]
		m["counted"] = m["variance"] = None
	cash["system"] = round(float(d["totals"]["expected_cash"]), 2)
	if closed:
		cash["counted"] = float(d.get("counted_cash") or 0)
		cash["variance"] = round(cash["counted"] - cash["system"], 2)

	def person(user):
		return {"user": user, "full_name": frappe.db.get_value(
			"User", user, "full_name") or user} if user else None

	d.update({
		"by_method": sorted(methods.values(), key=lambda m: m["mode"] != cash_mode),
		"cash_count": frappe.parse_json(d.get("cash_count")) if d.get("cash_count") else [],
		"receipts": receipts,
		"paid_outs": [t for t in txns if t.kind in ("Paid Out", "Drop", "Petty Cash")],
		"cashier_user": person(d.get("user")),
		"closed_by_user": person(d.get("closed_by")),
		"supervisor": person(d.get("variance_approved_by")),
	})
	return d


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Hotel Admin", "Night Auditor", "Revenue Manager",
               "Kamra Agent")
def cash_summary_v2(property: str, date: str | None = None,
                    to_date: str | None = None):
	"""Unified FO + POS cash for a business date, or date..to_date
	(replaces folio-only summary)."""
	date = date or get_business_date(property)
	to = _range_end(date, to_date)
	# Prefer cashier transactions when sessions exist for the day
	rows = frappe.db.sql(
		"""
		SELECT mode, kind, COUNT(*) AS txns, COALESCE(SUM(amount), 0) AS total
		FROM `tabCashier Transaction`
		WHERE property = %(property)s AND business_date BETWEEN %(date)s AND %(to)s
		GROUP BY mode, kind
		ORDER BY total DESC
		""",
		{"property": property, "date": date, "to": to}, as_dict=True,
	)
	if rows:
		by_mode: dict[str, dict] = {}
		for r in rows:
			if r.kind in ("Paid Out", "Drop", "Petty Cash"):
				continue
			m = r.mode or "Other"
			slot = by_mode.setdefault(m, {"mode": m, "txns": 0, "total": 0.0})
			slot["txns"] += int(r.txns or 0)
			slot["total"] += float(r.total or 0)
		modes = sorted(by_mode.values(), key=lambda x: -x["total"])
		return {
			"date": date,
			"modes": modes,
			"grand_total": float(sum(m["total"] for m in modes)),
			"source": "cashier",
		}
	# Fallback: folio payments (legacy) — inline to avoid circular import
	folio_rows = frappe.db.sql(
		"""
		SELECT fp.mode, COUNT(*) AS txns, COALESCE(SUM(fp.amount), 0) AS total
		FROM `tabFolio Payment` fp
		JOIN `tabFolio` f ON fp.parent = f.name
		WHERE f.property = %(property)s AND fp.posting_date BETWEEN %(date)s AND %(to)s
		GROUP BY fp.mode ORDER BY total DESC
		""",
		{"property": property, "date": date, "to": to}, as_dict=True,
	)
	return {
		"date": date,
		"modes": folio_rows,
		"grand_total": float(sum(r.total for r in folio_rows)),
		"source": "folio",
	}


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Hotel Admin", "Night Auditor", "Kamra Agent")
def shift_report(property: str, business_date: str | None = None,
                 to_date: str | None = None):
	"""Per-cashier totals for a business date, or business_date..to_date."""
	date = business_date or get_business_date(property)
	to = _range_end(date, to_date)
	sessions = frappe.get_all(
		"Cashier Session",
		filters={"property": property, "business_date": ("between", [date, to])},
		fields=["name", "cashier", "user", "status", "opening_float",
		        "system_cash", "system_card", "system_upi", "system_other",
		        "paid_outs", "drops", "petty_cash", "expected_cash",
		        "counted_cash", "variance", "opened_at", "closed_at"],
		order_by="opened_at asc",
	)
	drawers_by_sess = _session_foreign_drawers([s.name for s in sessions], property)
	for s in sessions:
		s["cashier_id"] = frappe.db.get_value("Cashier", s.cashier, "cashier_id")
		s["foreign_drawers"] = drawers_by_sess.get(s.name, [])
	journal = frappe.db.sql(
		"""
		SELECT ct.kind, ct.mode, COUNT(*) AS txns,
		       COALESCE(SUM(ct.amount), 0) AS total
		FROM `tabCashier Transaction` ct
		WHERE ct.property = %(property)s AND ct.business_date BETWEEN %(date)s AND %(to)s
		GROUP BY ct.kind, ct.mode
		ORDER BY ct.kind, ct.mode
		""",
		{"property": property, "date": date, "to": to}, as_dict=True,
	)
	return {"business_date": date, "to_date": to, "sessions": sessions,
	        "journal": journal}


# --- terminals: many tills, one place to see them ------------------------------

TERMINAL_EDITORS = ("Finance", "Hotel Admin")
TERMINAL_KINDS = ("Front Desk", "Restaurant", "Bar", "Spa", "Cash Office", "Other")


def _terminal_row(t: dict) -> dict:
	sess = frappe.db.get_value(
		"Cashier Session", {"cashier_terminal": t["name"], "status": "Open"},
		["name", "user", "opened_at", "expected_cash", "opening_float"], as_dict=True)
	t["open_session"] = sess
	return t


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Hotel Admin", "Night Auditor", "Kamra Agent")
def terminals(property: str, include_disabled: int = 0):
	"""A property's configured tills. Sessions opened without a terminal are
	the Default till and are not listed here."""
	filters: dict[str, Any] = {"property": property}
	if not int(include_disabled or 0):
		filters["disabled"] = 0
	rows = frappe.get_all(
		"Cashier Terminal", filters=filters,
		fields=["name", "terminal_name", "kind", "outlet", "bill_printer",
		        "default_float", "disabled"],
		order_by="terminal_name")
	return [_terminal_row(r) for r in rows]


@frappe.whitelist(methods=["POST"])
@require_roles(*TERMINAL_EDITORS)
def save_terminal(property: str, terminal_name: str, kind: str = "Front Desk",
                  outlet: str | None = None, bill_printer: str | None = None,
                  default_float: float = 0, disabled: int = 0,
                  name: str | None = None):
	"""Add or change a till."""
	from kamra.authz import assert_property_access
	assert_property_access(property)
	terminal_name = (terminal_name or "").strip()
	if not terminal_name:
		frappe.throw("Give the till a name.")
	if kind not in TERMINAL_KINDS:
		frappe.throw("Unknown kind of till.")
	if outlet and frappe.db.get_value("POS Outlet", outlet, "property") != property:
		frappe.throw("That outlet is not at this property.")
	if bill_printer:
		bp = frappe.db.get_value("Kitchen Printer", bill_printer,
		                         ["property", "prints_bills"], as_dict=True)
		if not bp or bp.property != property:
			frappe.throw("That printer is not at this property.")
		if not bp.prints_bills:
			frappe.throw("That printer is set to print KOTs only. Turn on "
			             "'Prints Bills' on it first.")
	taken = frappe.db.get_value("Cashier Terminal",
	                            {"property": property, "terminal_name": terminal_name})
	if taken and taken != name:
		frappe.throw(f"There's already a till called {terminal_name}.")
	fields = {"terminal_name": terminal_name, "kind": kind, "outlet": outlet or None,
	          "bill_printer": bill_printer or None,
	          "default_float": float(default_float or 0), "disabled": 1 if int(disabled or 0) else 0}
	if name:
		doc = frappe.get_doc("Cashier Terminal", name)
		if doc.property != property:
			frappe.throw("That till is not at this property.")
		if fields["disabled"] and frappe.db.exists(
				"Cashier Session", {"cashier_terminal": name, "status": "Open"}):
			frappe.throw("Close the open session on this till before switching it off.")
		doc.update(fields)
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(dict(fields, doctype="Cashier Terminal",
		                          property=property)).insert(ignore_permissions=True)
	return {"ok": True, "name": doc.name}


# Half a cent. The same slack the till totals use when they decide a figure
# has actually changed.
_MONEY_TOLERANCE = 0.004


def _lock_cash_office(terminal: str) -> None:
	"""One deposit at a time for this safe.

	The balance is a sum of transfers, not a column, so two requests can
	both read it and both insert. Locking the cash-office row makes the
	second wait until the first commits. The balance has to be read again
	after this returns: under REPEATABLE READ a plain SELECT still shows
	the snapshot from before the wait.
	"""
	if terminal:
		frappe.db.get_value("Cashier Terminal", terminal, "name", for_update=True)


def _sum_amount(query: str, terminal: str) -> float:
	return float(frappe.db.sql(query, terminal)[0][0] or 0)


def _locked_sum(query: str, terminal: str) -> float:
	"""Locking read of the rows behind the balance.

	A consistent read taken after `_lock_cash_office` can still be the
	snapshot from earlier in the request, and would miss the deposit that
	committed while this one waited. FOR UPDATE reads the latest committed
	rows.
	"""
	return float(sum(float(row[0] or 0) for row in frappe.db.sql(query, terminal)))


def _safe_balance(terminal: str, *, for_update: bool = False) -> dict[str, float]:
	if for_update:
		got = _locked_sum(
			"""SELECT amount FROM `tabCash Transfer`
			   WHERE to_terminal = %s AND kind = 'Drop' AND status = 'Received'
			   FOR UPDATE""",
			terminal)
		banked = _locked_sum(
			"""SELECT amount FROM `tabCash Transfer`
			   WHERE from_terminal = %s AND kind = 'Bank Deposit' AND status = 'Received'
			   FOR UPDATE""",
			terminal)
	else:
		got = _sum_amount(
			"""SELECT COALESCE(SUM(amount), 0) FROM `tabCash Transfer`
			   WHERE to_terminal = %s AND kind = 'Drop' AND status = 'Received'""",
			terminal)
		banked = _sum_amount(
			"""SELECT COALESCE(SUM(amount), 0) FROM `tabCash Transfer`
			   WHERE from_terminal = %s AND kind = 'Bank Deposit' AND status = 'Received'""",
			terminal)
	pending = _sum_amount(
		"""SELECT COALESCE(SUM(amount), 0) FROM `tabCash Transfer`
		   WHERE to_terminal = %s AND kind = 'Drop' AND status = 'Pending'""",
		terminal)
	return {"received": got, "banked": banked,
	        "in_transit": pending, "balance": got - banked}


def _counted_safe_effect(row) -> tuple[str | None, float]:
	"""(cash office, signed effect on its balance). Positive puts cash in.

	Mirrors `_safe_balance`: only a received drop into the office and a
	received bank deposit out of it count. Anything else is not in the safe.
	"""
	if not row:
		return None, 0.0
	kind, status = row.get("kind"), row.get("status")
	amount = float(row.get("amount") or 0)
	if kind == "Drop" and status == "Received" and row.get("to_terminal"):
		return row.get("to_terminal"), amount
	if kind == "Bank Deposit" and status == "Received" and row.get("from_terminal"):
		return row.get("from_terminal"), -amount
	return None, 0.0


def assert_transfer_keeps_safe_solvent(doc) -> None:
	"""A save may not take a safe balance below zero.

	`bank_deposit` checks before it inserts. This is the same rule on the
	document, so a Desk save cannot go around it. A save that does not
	reduce the safe (cash arriving, a smaller deposit) is allowed, including
	when the safe is already short, so the office can still receive the
	drop that repairs it.
	"""
	new_terminal, new_effect = _counted_safe_effect(doc)
	if doc.flags.in_insert or doc.is_new():
		old_terminal, old_effect = None, 0.0
	else:
		# check_if_latest loads this before validate. Fall back to the row
		# when validate runs outside save, so a note edit is not treated as
		# a brand-new withdrawal of the whole amount.
		prev = doc.get_doc_before_save()
		if prev is None and doc.name and frappe.db.exists("Cash Transfer", doc.name):
			prev = frappe.db.get_value(
				"Cash Transfer", doc.name,
				["kind", "status", "amount", "from_terminal", "to_terminal"],
				as_dict=True)
		old_terminal, old_effect = _counted_safe_effect(prev)
	for terminal in sorted({t for t in (new_terminal, old_terminal) if t}):
		effect = new_effect if terminal == new_terminal else 0.0
		saved = old_effect if terminal == old_terminal else 0.0
		# Not a reduction: nothing to refuse.
		if effect >= saved - _MONEY_TOLERANCE:
			continue
		_lock_cash_office(terminal)
		current = _safe_balance(terminal, for_update=True)["balance"]
		resulting = current - saved + effect
		if resulting < -_MONEY_TOLERANCE:
			frappe.throw("That is more than the safe holds.")


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Hotel Admin", "Night Auditor", "Kamra Agent")
def tills_board(property: str):
	"""Every till right now: who holds it, what should be in it, what is on
	its way to the cash office. The Default till (sessions opened without a
	terminal, i.e. everything from before terminals) is its own row."""
	bd = get_business_date(property)
	open_rows = frappe.get_all(
		"Cashier Session",
		filters={"property": property, "status": "Open"},
		fields=["name", "user", "cashier", "cashier_terminal", "terminal", "outlet",
		        "opened_at", "opening_float", "expected_cash", "business_date"],
		order_by="opened_at asc")
	by_term: dict[str | None, list] = {}
	for r in open_rows:
		r["cashier_id"] = frappe.db.get_value("Cashier", r.cashier, "cashier_id")
		by_term.setdefault(r.cashier_terminal or None, []).append(r)
	board = []
	for t in frappe.get_all("Cashier Terminal", filters={"property": property, "disabled": 0},
	                        fields=["name", "terminal_name", "kind", "outlet", "bill_printer"],
	                        order_by="terminal_name"):
		row = dict(t, sessions=by_term.get(t.name, []), is_default=False)
		if t.kind == "Cash Office":
			row["safe"] = _safe_balance(t.name)
		board.append(row)
	board.insert(0, {"name": None, "terminal_name": "Default", "kind": "Front Desk",
	                 "outlet": None, "bill_printer": None, "is_default": True,
	                 "sessions": by_term.get(None, [])})
	pending = frappe.get_all(
		"Cash Transfer", filters={"property": property, "status": "Pending"},
		fields=["name", "kind", "amount", "from_session", "from_terminal", "to_terminal",
		        "requested_by", "requested_at"], order_by="requested_at asc")
	review = frappe.get_all(
		"Cashier Session",
		filters={"property": property, "variance_status": "Needs Review"},
		fields=["name", "user", "cashier_terminal", "business_date", "variance", "closed_at"],
		order_by="closed_at desc", limit_page_length=20)
	drawers_by_sess = _session_foreign_drawers(
		[r.name for r in open_rows] + [r.name for r in review], property,
	)
	for r in open_rows:
		r["foreign_drawers"] = drawers_by_sess.get(r.name, [])
	for r in review:
		r["foreign_drawers"] = drawers_by_sess.get(r.name, [])
	return {"business_date": str(bd), "tills": board, "pending_transfers": pending,
	        "variance_review": review}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Hotel Admin", "Kamra Agent")
def receive_transfer(transfer: str, pin: str | None = None):
	"""The cash office confirms a drop arrived."""
	t = frappe.get_doc("Cash Transfer", transfer)
	from kamra.authz import assert_property_access, require_cashier_pin
	assert_property_access(t.property)
	if t.kind != "Drop" or t.status != "Pending":
		frappe.throw("This transfer is not waiting to be received.")
	require_cashier_pin(t.property, pin)
	if t.requested_by == frappe.session.user and not (
			set(frappe.get_roles()) & {"Hotel Admin", "System Manager", "Finance"}):
		frappe.throw("Someone other than the person who dropped the cash must receive it.")
	t.status = "Received"
	t.received_by = frappe.session.user
	t.received_at = now_datetime()
	t.save(ignore_permissions=True)
	return {"ok": True, "safe": _safe_balance(t.to_terminal)}


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Hotel Admin")
def bank_deposit(cash_office: str, amount: float, notes: str | None = None,
                 pin: str | None = None):
	"""Cash leaves the safe for the bank."""
	office = frappe.db.get_value("Cashier Terminal", cash_office,
	                             ["name", "property", "kind"], as_dict=True)
	if not office or office.kind != "Cash Office":
		frappe.throw("That is not a cash office.")
	from kamra.authz import assert_property_access, require_cashier_pin
	assert_property_access(office.property)
	require_cashier_pin(office.property, pin)
	amt = float(amount)
	if amt <= 0:
		frappe.throw("Amount must be positive.")
	# Serialise on the safe, then read the balance the lock waited for.
	_lock_cash_office(office.name)
	if amt > _safe_balance(office.name, for_update=True)["balance"] + _MONEY_TOLERANCE:
		frappe.throw("That is more than the safe holds.")
	now = now_datetime()
	doc = frappe.get_doc({
		"doctype": "Cash Transfer", "property": office.property, "kind": "Bank Deposit",
		"status": "Received", "amount": amt, "business_date": get_business_date(office.property),
		"from_terminal": office.name, "requested_by": frappe.session.user, "requested_at": now,
		"received_by": frappe.session.user, "received_at": now,
		"notes": (notes or "")[:500] or None,
	}).insert(ignore_permissions=True)
	return {"ok": True, "transfer": doc.name, "safe": _safe_balance(office.name)}


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Hotel Admin")
def approve_variance(session: str, note: str | None = None):
	"""A manager signs off a till that closed over or short."""
	doc = frappe.get_doc("Cashier Session", session)
	from kamra.authz import assert_property_access
	assert_property_access(doc.property)
	if doc.variance_status != "Needs Review":
		frappe.throw("This till has nothing waiting for review.")
	if doc.user == frappe.session.user and "Hotel Admin" not in frappe.get_roles() \
	   and frappe.session.user != "Administrator":
		frappe.throw("Someone other than the cashier has to approve the variance.")
	doc.variance_status = "Approved"
	doc.variance_approved_by = frappe.session.user
	if note:
		doc.close_notes = ((doc.close_notes or "") + f" | Review: {note.strip()}")[:500]
	doc.save(ignore_permissions=True)
	return {"ok": True}
