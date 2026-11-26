"""External POS interface - room charges from a third-party point of sale.

A restaurant, spa or bar running its own POS (not Kamra's) needs two calls:

    pos_room_lookup        is this room in house, and who is in it?
    pos_post_room_charge   put this ticket on their bill

Both are for a service user holding the "POS Integrator" role (or Kamra
Agent), limited to its property with a Property User Permission, calling
with its API key. Nothing here is a second way to bill: the charge goes
through api.post_stay_charge - the same path as the desk, housekeeping and
the AI agent - so the routing and billing rules pick the folio and tax
comes from the country pack. Liquor routes like any other charge.

Every ticket is posted at most once: `ticket_id` (the POS's own check
number) is the idempotency key per property. Retrying a ticket returns the
first posting; reusing a ticket number for a different charge is refused.
"""

from __future__ import annotations

import hashlib
from functools import wraps

import frappe
from frappe import _
from frappe.utils import cint, flt

from kamra.authz import (
	ADMIN,
	POS_SUPERVISOR_ROLES,
	_enforce_property_scope,
	act_as,
	assert_property_access,
	resolve_property_for_pos_profile,
	verify_supervisor,
)
from kamra.branding import role_label
from kamra.install import AGENT_EMAIL
from kamra.pos import RoomChargesBlocked

POS_INTEGRATOR = "POS Integrator"
ROLES = (POS_INTEGRATOR, "Kamra Agent")
# Outlet POS staff who may look up, verify, and post room charges for their
# authorized property/outlet without holding the elevated POS Integrator role.
# External POS apps extend this dynamically via the `pos_outlet_roles` hook.
POS_OUTLET_ROLES = (
	POS_INTEGRATOR,
	"Kamra Agent",
	"F&B Manager",
	"POS Cashier",
	"POS Waiter",
	"POS Captain",
	"POS Manager",
	"POS Admin",
)
# Roles that bypass per-POS-Profile assignment checks; extended dynamically
# via the `pos_bypass_roles` hook.
_OUTLET_BYPASS_ROLES = frozenset({
	POS_INTEGRATOR,
	"Kamra Agent",
	"F&B Manager",
	"Hotel Admin",
	"System Manager",
	"Administrator",
	"POS Manager",
	"POS Admin",
})
CHARGE = "External POS Charge"
# what an outlet sells; room nights, fees and allowances stay with the hotel
CHARGE_TYPES = ("Food & Beverage", "Minibar", "Laundry", "Spa", "Misc")


def _hook_values(hook_name: str) -> tuple[str, ...]:
	raw = frappe.get_hooks(hook_name) or ()
	if isinstance(raw, str):
		raw = (raw,)
	out: list[str] = []
	for item in raw:
		if isinstance(item, str) and item.strip():
			out.append(item.strip())
		elif isinstance(item, (list, tuple, set)):
			out.extend(v.strip() for v in item if isinstance(v, str) and v.strip())
	return tuple(dict.fromkeys(out))


def _pos_outlet_roles() -> tuple[str, ...]:
	return tuple(dict.fromkeys((*POS_OUTLET_ROLES, *_hook_values("pos_outlet_roles"))))


def _outlet_bypass_roles() -> frozenset[str]:
	return _OUTLET_BYPASS_ROLES | frozenset(_hook_values("pos_bypass_roles"))


def _require_pos_outlet_roles(fn):
	"""Dynamic role guard for outlet POS endpoints: evaluates base neutral
	POS_OUTLET_ROLES plus any roles registered via `pos_outlet_roles` hook."""
	@wraps(fn)
	def guarded(*args, **kwargs):
		allowed = set(_pos_outlet_roles()) | set(ADMIN)
		if not allowed & set(frappe.get_roles()):
			frappe.throw(
				f"Not permitted - needs one of: {', '.join(role_label(role) for role in sorted(POS_OUTLET_ROLES))}.",
				frappe.PermissionError,
			)
		_enforce_property_scope(fn, args, kwargs, {})
		return fn(*args, **kwargs)

	guarded._kamra_roles = set(POS_OUTLET_ROLES) | set(ADMIN)
	return guarded


def _assert_pos_profile_access(pos_profile: str | None) -> None:
	"""For outlet POS staff (cashiers/waiters without integration/admin roles),
	enforce POS Profile active state, user assignment, and Branch User Permissions
	when `pos_profile` exists on this site."""
	if not pos_profile:
		return
	user = frappe.session.user
	if not user or user == "Administrator":
		return
	if _outlet_bypass_roles() & set(frappe.get_roles()):
		return
	if not frappe.db.table_exists("POS Profile") or not frappe.db.exists("POS Profile", pos_profile):
		return
	meta = frappe.get_meta("POS Profile")
	fields = ["disabled"]
	if meta.has_field("branch"):
		fields.append("branch")
	row = frappe.db.get_value("POS Profile", pos_profile, fields, as_dict=True) or {}
	if cint(row.get("disabled")):
		frappe.throw(_("This POS profile is disabled."), frappe.PermissionError)
	if frappe.db.table_exists("POS Profile User"):
		assigned = frappe.get_all(
			"POS Profile User",
			filters={"parent": pos_profile, "parenttype": "POS Profile"},
			pluck="user",
		)
		if assigned and user not in assigned:
			frappe.throw(_("You are not assigned to this POS profile."), frappe.PermissionError)
	branch = row.get("branch")
	if branch:
		from frappe.core.doctype.user_permission.user_permission import get_user_permissions

		branch_perms = get_user_permissions(user).get("Branch") or []
		if branch_perms:
			allowed_branches = {p.get("doc") for p in branch_perms}
			if branch not in allowed_branches:
				frappe.throw(_("You do not have access to this outlet branch."), frappe.PermissionError)


def _room_number(room: str | None) -> str | None:
	if not room:
		return None
	return frappe.db.get_value("Room", room, "room_number") or room.split("-")[-1]


def _resolve_room(property: str, room: str) -> str | None:
	"""Accept the Room id or the number printed on the door."""
	if frappe.db.exists("Room", room) and \
	   frappe.db.get_value("Room", room, "property") == property:
		return room
	return frappe.db.get_value("Room", {"property": property,
	                                    "room_number": str(room).strip()}, "name")


@frappe.whitelist()
@_require_pos_outlet_roles
def pos_room_lookup(property: str, room: str | None = None,
                    last_name: str | None = None) -> list:
	"""In-house stays matching a room number and/or the guest's last name.
	Only checked-in guests, only this property, only what a cashier needs to
	confirm the charge: room, guest name, reservation and departure date.
	Ask with a room or a last name (2+ letters) - never the whole list."""
	last_name = (last_name or "").strip()
	if not room and len(last_name) < 2:
		frappe.throw(_("Look up by room number or by the guest's last name (2+ letters)."))
	filters = {"property": property, "status": "Checked In"}
	if room:
		name = _resolve_room(property, room)
		if not name:
			return []
		filters["room"] = name
	if last_name:
		filters["guest_name"] = ("like", f"%{last_name}%")
	rows = frappe.get_all("Reservation", filters=filters,
	                      fields=["name", "room", "guest_name", "check_out_date",
	                              "company"],
	                      order_by="room asc", limit=20)
	return [{
		"reservation": r.name,
		"room": r.room,
		"room_number": _room_number(r.room),
		"guest_name": r.guest_name,
		"check_out_date": str(r.check_out_date) if r.check_out_date else None,
		# so the POS can say "company pays food, drinks go to the guest"
		"corporate": bool(r.company),
	} for r in rows]


def _charge_name(property: str, ticket_id: str) -> str:
	"""One record per (property, ticket): the primary key itself refuses a
	second posting, even from two requests racing each other."""
	digest = hashlib.sha256(f"{property}\n{ticket_id}".encode()).hexdigest()[:24]
	return f"EPC-{digest}"


def _result(rec, duplicate: bool) -> dict:
	out = {
		"ok": True,
		"duplicate": duplicate,
		"ticket_id": rec.ticket_id,
		"reservation": rec.reservation,
		"room_number": _room_number(rec.room),
		"folio": rec.folio,
		"folio_type": frappe.db.get_value("Folio", rec.folio, "folio_type"),
		"amount": flt(rec.amount),
		"balance": flt(rec.balance_after),
	}
	if rec.get("override_approved_by"):
		out["override_approved_by"] = rec.override_approved_by
		out["override_reason"] = rec.get("override_reason")
	return out


def _is_checkread_error(exc: Exception) -> bool:
	args = getattr(exc, "args", ())
	if args and args[0] == 1020:
		return True
	return "1020" in str(exc) and "changed since last read" in str(exc).lower()


def _fresh_read_row(sql: str, params: tuple) -> dict | None:
	"""Read one committed row on a short-lived MariaDB connection so concurrent
	REPEATABLE READ transactions observe committed updates without hitting 1020."""
	create_conn = getattr(frappe.db, "create_connection", None)
	if not callable(create_conn):
		return None
	conn = None
	try:
		conn = create_conn()
		with conn.cursor() as cur:
			cur.execute(sql, params)
			row = cur.fetchone()
			if not row:
				return None
			cols = [col[0] for col in cur.description]
			return dict(zip(cols, row))
	except Exception:
		return None
	finally:
		if conn is not None:
			try:
				conn.close()
			except Exception:
				pass


def _current_folio_balance(folio_name: str | None) -> float:
	if not folio_name:
		return 0.0
	from kamra.folio import sales_invoice_field, settlement_balance
	fields = ["name", "property", "grand_total", "balance", "modified", *sales_invoice_field()]
	in_txn = frappe.db.get_value("Folio", folio_name, fields, as_dict=True)
	committed = _fresh_read_row(
		("SELECT name, property, grand_total, balance, modified, sales_invoice FROM `tabFolio` WHERE name = %s"
		 if "sales_invoice" in fields else "SELECT balance, modified FROM `tabFolio` WHERE name = %s"),
		(folio_name,),
	)
	if committed is not None:
		if not in_txn or str(committed.get("modified") or "") > str(in_txn.get("modified") or ""):
			return flt(settlement_balance(frappe._dict(committed)))
	return flt(settlement_balance(in_txn)) if in_txn else 0.0


def _lock_reservation_row(reservation: str) -> dict | None:
	sql = """
		SELECT name, status, room, property, guest_name, check_out_date,
		       room_charges_allowed, group_booking, company, pays_own_charges
		FROM `tabReservation`
		WHERE name = %s
	"""
	try:
		rows = frappe.db.sql(sql + " FOR UPDATE", (reservation,), as_dict=True)
		return rows[0] if rows else None
	except Exception as exc:
		if not _is_checkread_error(exc):
			raise
		return _fresh_read_row(sql, (reservation,))


def _masked_guest_name(full_name: str | None) -> str:
	parts = [p for p in str(full_name or "").strip().split() if p]
	if not parts:
		return ""
	if len(parts) == 1:
		return parts[0]
	return f"{parts[0]} {parts[-1][0]}."


def _currency_precision(currency: str | None) -> int:
	code = (currency or "").strip().upper()
	if not code:
		return 2
	try:
		from babel.numbers import get_currency_precision
		return int(get_currency_precision(code))
	except Exception:
		return 2


def _convert_to_home(property: str, amount: float | None, currency: str | None) -> tuple[float | None, str, str | None]:
	from babel.numbers import get_currency_precision
	from frappe.utils import add_days
	from kamra.business_date import get_business_date
	from kamra.ledger import _settlement_rate, _settlement_window, fx_to_home, home_currency

	home = home_currency(property)
	if amount is None:
		return None, home, None
	cur = (currency or home).strip().upper()
	if cur == home.upper():
		return flt(amount), home, None

	bd = get_business_date(property)
	window = _settlement_window()
	rows = frappe.get_all(
		"Exchange Rate",
		filters={
			"property": property,
			"currency": cur,
			"rate_date": ["between", [add_days(bd, 1 - window), bd]],
			"disabled": 0,
		},
		fields=["rate_date", "buy_rate", "sell_rate", "market_rate", "source"],
		order_by="rate_date desc",
		limit=2,
	)
	if len(rows) == 2 and rows[0].rate_date == rows[1].rate_date:
		return None, home, f"POS-RC-CURRENCY: Multiple exchange rates found for {cur}. Ask Finance."
	rate = _settlement_rate(rows[0]) if rows else 0
	if rate <= 0:
		return None, home, f"POS-RC-CURRENCY: No exchange rate is configured from {cur} to {home}."
	return fx_to_home(flt(amount), rate, get_currency_precision(home)), home, None


def _resolve_target_folio_readonly(
	res: dict,
	charge_type: str = "Food & Beverage",
	is_alcohol: int = 0,
	*,
	transaction_code: str | None = None,
	amount: float | None = None,
) -> tuple[str | None, str, float, bool]:
	"""Read-only mirror of `kamra.folio.target_folio` without opening new folios.

	Checks active K1 Routing Instructions first (same precedence as
	`kamra.routing.route(..., whole=True)`), then falls back to legacy
	group/company billing rules using the resolved transaction code.
	Also surfaces any House Account (`folio_type == 'Account'`) folio linked to
	the reservation so both `verify_room_charge` and `pos_post_room_charge`
	hard-refuse it instead of silently opening a fresh Guest folio.
	"""
	from decimal import Decimal

	from frappe.utils import getdate

	from kamra import folio as folio_mod
	from kamra import routing as routing_mod
	from kamra.business_date import get_business_date
	from kamra.folio import COMPANY_BILLING_DEFAULTS, _company_pays
	from kamra.ledger import resolve_transaction_code

	open_acct = frappe.db.get_value(
		"Folio",
		{"reservation": res["name"], "folio_type": "Account", "status": "Open"},
		"name",
	)
	if open_acct:
		return open_acct, "Account", _current_folio_balance(open_acct), False

	prop = res.get("property")
	code = resolve_transaction_code(
		charge_type,
		transaction_code=transaction_code,
		is_alcohol=cint(is_alcohol),
		property=prop,
	)

	def _window_1_readonly() -> tuple[str | None, str, float, bool]:
		gf = frappe.db.get_value(
			"Folio",
			{"reservation": res["name"], "folio_type": "Guest", "status": "Open"},
			"name",
		) or frappe.db.get_value(
			"Folio",
			{"reservation": res["name"], "folio_type": "Guest"},
			"name",
		)
		if gf:
			return gf, "Guest", _current_folio_balance(gf), False
		any_acct = frappe.db.get_value(
			"Folio",
			{"reservation": res["name"], "folio_type": "Account"},
			"name",
		)
		if any_acct:
			return any_acct, "Account", _current_folio_balance(any_acct), False
		return None, "Guest", 0.0, False

	# 1. Active K1 Routing Instructions (read-only mirror of kamra.routing.route(whole=True))
	if frappe.db.exists("Routing Instruction", {"reservation": res["name"], "disabled": 0}):
		day = getdate(get_business_date(prop))
		ri = routing_mod.matching(res["name"], charge_type, code, day)
		if ri:
			kind = ri.limit_type or "None"
			skip_limit_without_amount = kind != "None" and amount is None
			if not skip_limit_without_amount:
				fits = True
				if kind == "Percent":
					fits = False  # outlet charges move whole only
				elif kind == "Amount":
					line = folio_mod.line_figures(prop, charge_type, amount)
					if line["base"] > 0:
						left = Decimal(str(ri.limit_value or 0)) - routing_mod.used(
							ri.name, day if cint(ri.per_day) else None
						)
						fits = left >= line["figure"] and left > 0
				if not fits:
					return _window_1_readonly()
				window = cint(ri.target_window) or 1
				if ri.target_type == "Window":
					f_row = frappe.db.get_value(
						"Folio",
						{
							"reservation": res["name"],
							"window_no": window,
							"folio_type": ("!=", "Group"),
							"status": "Open",
						},
						["name", "folio_type"],
						as_dict=True,
					)
					if f_row:
						return (
							f_row.name,
							f_row.folio_type or "Guest",
							_current_folio_balance(f_row.name),
							False,
						)
					return None, "Guest", 0.0, False
				if ri.target_type == "Room":
					other = frappe.db.get_value(
						"Reservation",
						ri.target_reservation,
						["name", "status", "property", "room_charges_allowed"],
						as_dict=True,
					)
					if not other or other.status != "Checked In" or other.property != ri.property:
						return _window_1_readonly()
					target_no_post = (
						other.room_charges_allowed is not None and not cint(other.room_charges_allowed)
					)
					f_row = frappe.db.get_value(
						"Folio",
						{
							"reservation": other.name,
							"window_no": window,
							"folio_type": ("!=", "Group"),
							"status": "Open",
						},
						["name", "folio_type"],
						as_dict=True,
					)
					if f_row:
						return (
							f_row.name,
							f_row.folio_type or "Guest",
							_current_folio_balance(f_row.name),
							target_no_post,
						)
					return None, "Guest", 0.0, target_no_post
				if ri.target_type == "Group Master":
					group = res.get("group_booking")
					gb = group and frappe.db.get_value(
						"Group Booking",
						group,
						["property", "status", "master_reservation"],
						as_dict=True,
					)
					if not gb or gb.property != ri.property or gb.status == "Cancelled":
						return _window_1_readonly()
					master_no_post = False
					if gb.master_reservation:
						m_allowed = frappe.db.get_value(
							"Reservation", gb.master_reservation, "room_charges_allowed"
						)
						if m_allowed is not None and not cint(m_allowed):
							master_no_post = True
					gf = frappe.db.get_value(
						"Folio",
						{
							"group_booking": group,
							"folio_type": "Group",
							"window_no": window,
							"status": "Open",
						},
						"name",
					)
					if not gf and window == 1:
						gf = frappe.db.get_value(
							"Folio",
							{
								"group_booking": group,
								"folio_type": "Group",
								"window_no": 0,
								"status": "Open",
							},
							"name",
						)
					return gf, "Group", _current_folio_balance(gf), master_no_post
				return _window_1_readonly()

	# 2. Legacy presets (read-only mirror of kamra.folio._legacy_target)
	master_no_post = False
	group = res.get("group_booking")
	gb = frappe.db.get_value(
		"Group Booking",
		group,
		["company", "master_reservation", "master_pays", "status"],
		as_dict=True,
	) if group else None
	if gb and gb.status == "Cancelled":
		gb = None
	opted_out = bool(gb) and bool(cint(res.get("pays_own_charges")))
	if gb and not opted_out:
		company_covers = bool(gb.company and _company_pays(gb.company, charge_type, prop, code))
		master_covers = False
		if gb.master_reservation:
			covered = COMPANY_BILLING_DEFAULTS.get(gb.master_pays, frozenset())
			master_covers = covered is None or charge_type in covered
		if company_covers or master_covers:
			if gb.master_reservation:
				m_allowed = frappe.db.get_value(
					"Reservation", gb.master_reservation, "room_charges_allowed"
				)
				if m_allowed is not None and not cint(m_allowed):
					master_no_post = True
			gf = frappe.db.get_value(
				"Folio",
				{
					"group_booking": group,
					"folio_type": "Group",
					"window_no": 1,
					"status": "Open",
				},
				"name",
			) or frappe.db.get_value(
				"Folio",
				{
					"group_booking": group,
					"folio_type": "Group",
					"window_no": 0,
					"status": "Open",
				},
				"name",
			)
			return gf, "Group", _current_folio_balance(gf), master_no_post

	company = res.get("company")
	if (
		company
		and not (opted_out and gb and company == gb.company)
		and _company_pays(company, charge_type, prop, code)
	):
		cf = frappe.db.get_value(
			"Folio",
			{"reservation": res["name"], "folio_type": "Company", "status": "Open"},
			"name",
		)
		return cf, "Company", _current_folio_balance(cf), False

	return _window_1_readonly()


def _evaluate_stay(
	property: str,
	res: dict,
	amount: float | None = None,
	currency: str | None = None,
	charge_type: str = "Food & Beverage",
	is_alcohol: int = 0,
	transaction_code: str | None = None,
) -> dict:
	from kamra.ledger import code_is_alcohol, home_currency, resolve_transaction_code
	from kamra.settings import get_setting

	room_no = _room_number(res.get("room"))
	departure = str(res["check_out_date"]) if res.get("check_out_date") else None
	masked_name = _masked_guest_name(res.get("guest_name"))

	resolved_code = resolve_transaction_code(
		charge_type,
		transaction_code=transaction_code,
		is_alcohol=cint(is_alcohol),
		property=property,
	)
	effective_alcohol = 1 if (cint(is_alcohol) or code_is_alcohol(resolved_code)) else 0
	home_amount, home, fx_err = _convert_to_home(property, amount, currency)

	folio_name, folio_type, balance, master_no_post = _resolve_target_folio_readonly(
		res,
		charge_type=charge_type,
		is_alcohol=effective_alcohol,
		transaction_code=resolved_code,
		amount=home_amount,
	)
	actual_type = frappe.db.get_value("Folio", folio_name, "folio_type") if folio_name else folio_type
	if folio_type == "Account" or actual_type == "Account":
		return {
			"ok": False,
			"reservation": res["name"],
			"room": res.get("room"),
			"room_number": room_no,
			"guest_name": masked_name,
			"departure": departure,
			"folio": folio_name,
			"folio_type": "Account",
			"posting_allowed": False,
			"no_post": 1,
			"limit": 0.0,
			"balance": balance,
			"available": 0.0,
			"currency": home_currency(property),
			"charge_amount": None,
			"override_required": False,
			"reason_code": "POS-RC-HOUSE-ACCOUNT",
			"reason": "House accounts cannot be billed as a room charge.",
		}

	prec = _currency_precision(home)
	no_post = 1 if (
		(res.get("room_charges_allowed") is not None and not cint(res.get("room_charges_allowed")))
		or master_no_post
	) else 0

	per_ticket_limit = flt(get_setting(property, "pos_room_charge_limit") or 0)
	credit_check = cint(get_setting(property, "pos_room_charge_credit_check") or 0)
	allowance = flt(get_setting(property, "pos_room_charge_allowance") or 0)

	if credit_check and allowance > 0:
		limit_out = allowance
		available_out = max(0.0, flt(allowance - balance, prec))
		if per_ticket_limit > 0:
			available_out = min(available_out, per_ticket_limit)
	elif per_ticket_limit > 0:
		limit_out = per_ticket_limit
		available_out = per_ticket_limit
	else:
		limit_out = 0.0
		available_out = None

	posting_allowed = True
	override_required = False
	reason_code = None
	reason = None

	if fx_err:
		posting_allowed = False
		override_required = False
		reason_code = "POS-RC-CURRENCY"
		reason = fx_err
	elif no_post:
		posting_allowed = False
		override_required = True
		reason_code = "POS-RC-NO-POST"
		reason = "Room charges are blocked on this stay. Manager approval is required."
	elif home_amount is not None:
		if per_ticket_limit > 0 and flt(home_amount, prec) > flt(per_ticket_limit, prec):
			posting_allowed = False
			override_required = True
			reason_code = "POS-RC-OVER-LIMIT"
			reason = (
				f"Charge ({home_amount:g} {home}) exceeds the room charge limit "
				f"({per_ticket_limit:g} {home}). Manager approval is required."
			)
		elif credit_check and allowance > 0 and flt(balance + home_amount, prec) > flt(allowance, prec):
			posting_allowed = False
			override_required = True
			reason_code = "POS-RC-OVER-LIMIT"
			reason = (
				f"Projected balance ({flt(balance + home_amount, prec):g} {home}) exceeds the "
				f"room charge credit limit ({allowance:g} {home}). Manager approval is required."
			)

	return {
		"ok": True,
		"reservation": res["name"],
		"room": res.get("room"),
		"room_number": room_no,
		"guest_name": masked_name,
		"departure": departure,
		"folio": folio_name,
		"folio_type": folio_type,
		"posting_allowed": posting_allowed,
		"no_post": no_post,
		"limit": limit_out,
		"balance": balance,
		"available": available_out,
		"currency": home,
		"charge_amount": round(flt(home_amount), prec) if home_amount is not None else None,
		"override_required": override_required,
		"reason_code": reason_code,
		"reason": reason,
	}


@frappe.whitelist()
@_require_pos_outlet_roles
def verify_room_charge(
	property: str | None = None,
	room: str | None = None,
	surname: str | None = None,
	last_name: str | None = None,
	reservation: str | None = None,
	amount: float | None = None,
	currency: str | None = None,
	pos_profile: str | None = None,
	charge_type: str = "Food & Beverage",
	is_alcohol: int = 0,
	transaction_code: str | None = None,
) -> dict:
	"""Read-only room-charge eligibility lookup for POS tills.

	Returns a neutral shape with guest match(es), posting_allowed, no_post,
	limit, balance, available, currency, charge_amount (in property currency),
	override_required, reason_code, and reason. Supports room number, surname
	prefix (no wildcards), or explicit reservation, and rejects house accounts
	and cross-property lookups."""
	_assert_pos_profile_access(pos_profile)
	prop = (property or "").strip() or resolve_property_for_pos_profile(pos_profile)
	if not prop:
		frappe.throw(_("Property is required to verify a room charge."))
	assert_property_access(prop)

	amt = flt(amount) if amount is not None and str(amount).strip() != "" else None
	room_in = (room or "").strip()
	surname_in = (surname if surname is not None else last_name) or ""
	surname_in = surname_in.strip()
	res_in = (reservation or "").strip()

	fields = [
		"name", "status", "room", "property", "guest_name", "check_out_date",
		"room_charges_allowed", "group_booking", "company", "pays_own_charges",
	]

	if res_in:
		res_row = frappe.db.get_value("Reservation", res_in, fields, as_dict=True)
		if not res_row:
			return {
				"ok": False, "shared_room": False, "posting_allowed": False,
				"override_required": False, "matches": [],
				"reason_code": "POS-RC-NOT-FOUND",
				"reason": "Reservation not found.",
			}
		assert_property_access(res_row.property)
		if res_row.property != prop:
			frappe.throw(_("You do not have access to this reservation at this property."), frappe.PermissionError)
		if res_row.status != "Checked In":
			return {
				"ok": False, "shared_room": False, "posting_allowed": False,
				"override_required": False, "matches": [],
				"reason_code": "POS-RC-NOT-IN-HOUSE",
				"reason": "Guest is not checked in.",
			}
		ev = _evaluate_stay(prop, res_row, amt, currency, charge_type, is_alcohol, transaction_code)
		return {**ev, "shared_room": False, "matches": [ev] if ev.get("ok") else []}

	if not room_in and not surname_in:
		return {
			"ok": False, "shared_room": False, "posting_allowed": False,
			"override_required": False, "matches": [],
			"reason_code": "POS-RC-INVALID-QUERY",
			"reason": "Enter a room number or guest surname.",
		}

	filters: dict = {"property": prop, "status": "Checked In"}
	if room_in:
		# Explicitly reject House Accounts passed as a room reference.
		if frappe.db.exists("Folio", {"name": room_in, "folio_type": "Account"}):
			return {
				"ok": False, "shared_room": False, "posting_allowed": False,
				"override_required": False, "matches": [],
				"reason_code": "POS-RC-HOUSE-ACCOUNT",
				"reason": "House accounts cannot be billed as a room charge.",
			}
		room_id = _resolve_room(prop, room_in)
		if not room_id:
			return {
				"ok": False, "shared_room": False, "posting_allowed": False,
				"override_required": False, "matches": [],
				"reason_code": "POS-RC-NOT-FOUND",
				"reason": "No checked-in guest was found for that room.",
			}
		filters["room"] = room_id

	if surname_in:
		cleaned = surname_in.replace("%", "").replace("_", "").strip()
		if len(cleaned) < 2 or cleaned != surname_in:
			return {
				"ok": False, "shared_room": False, "posting_allowed": False,
				"override_required": False, "matches": [],
				"reason_code": "POS-RC-INVALID-QUERY",
				"reason": "Enter at least 2 letters of the guest's surname without wildcards.",
			}
		filters["guest_name"] = ("like", f"%{cleaned}%")

	rows = frappe.get_all(
		"Reservation",
		filters=filters,
		fields=fields,
		order_by="room asc, creation asc",
		limit=20,
	)
	if not rows:
		return {
			"ok": False, "shared_room": False, "posting_allowed": False,
			"override_required": False, "matches": [],
			"reason_code": "POS-RC-NOT-FOUND",
			"reason": "No checked-in guest matched your search.",
		}

	evaluated = [_evaluate_stay(prop, r, amt, currency, charge_type, is_alcohol, transaction_code) for r in rows]
	if len(evaluated) == 1:
		ev = evaluated[0]
		return {**ev, "shared_room": False, "matches": [ev] if ev.get("ok") else []}

	same_room = len({r.get("room") for r in rows}) == 1
	return {
		"ok": True,
		"shared_room": same_room,
		"posting_allowed": False,
		"override_required": False,
		"reason_code": "POS-RC-SHARED-ROOM" if same_room else "POS-RC-MULTIPLE",
		"reason": (
			"Multiple guests are checked into this room. Select the guest's stay."
			if same_room
			else "Multiple checked-in guests matched. Select the guest's stay."
		),
		"matches": evaluated,
	}


def _verify_room_charge_override(
	*,
	property: str,
	reservation: str,
	home_amount: float,
	currency: str | None = None,
	room_number: str | None = None,
	override_approval=None,
	override_token: str | None = None,
	override_reason: str | None = None,
	pos_profile: str | None = None,
	reference_name: str | None = None,
) -> tuple[str | None, str | None]:
	"""Verify duty-manager approval for an over-limit or No Post room charge.

	Accepts either:
	1. A POS override token consumed via the callable registered on the
	   `pos_override_consumer` hook (fails closed if the hook is missing or
	   raises), using the canonical 5-key claim
	   `{reservation, room, amount, currency, reason}` in property currency,
	2. An in-process server flag `frappe.flags.kamra_room_charge_override` set after
	   the POS already consumed the token in this transaction (matched in property
	   currency), or
	3. A direct `override_approval` dict (`{supervisor, pin, reason}`) verified
	   through `kamra.authz.verify_supervisor`.
	"""
	prec = _currency_precision(currency)
	tol = 0.5 * (10 ** -prec)
	norm_currency = (currency or "").strip().upper()
	if override_token:
		hook_paths = _hook_values("pos_override_consumer")
		consumer = None
		if hook_paths:
			try:
				consumer = frappe.get_attr(hook_paths[-1])
			except Exception:
				consumer = None
		if not callable(consumer):
			frappe.throw(
				_("Room charge override verification is unavailable. Ask a manager to resolve."),
				frappe.ValidationError,
			)
		effective_reason = (override_reason or "").strip()
		if not effective_reason and isinstance(override_approval, dict):
			effective_reason = str(override_approval.get("reason") or "").strip()
		try:
			claim = {
				"reservation": reservation,
				"room": (room_number or "").strip(),
				"amount": round(flt(home_amount), prec),
				"currency": norm_currency,
				"reason": effective_reason,
			}
			consumed = consumer(
				override_token,
				"room_charge_over_limit",
				pos_profile or "",
				reference_name or reservation,
				claim,
			)
		except (frappe.ValidationError, frappe.PermissionError):
			raise
		except Exception:
			frappe.throw(
				_("Room charge override verification failed. Ask a manager to approve again."),
				frappe.ValidationError,
			)
		approver = consumed.get("approver") if isinstance(consumed, dict) else getattr(consumed, "approver", None)
		reason = (
			(consumed.get("claim") or {}).get("reason")
			if isinstance(consumed, dict)
			else (getattr(consumed, "claim", None) or {}).get("reason")
		)
		if not approver:
			frappe.throw(_("Invalid manager approval token."), frappe.PermissionError)
		return str(approver), str(reason or effective_reason or "Manager approved room charge")

	srv_flag = getattr(frappe.flags, "kamra_room_charge_override", None)
	if isinstance(srv_flag, dict):
		flag_currency = (srv_flag.get("currency") or "").strip().upper()
		currency_ok = not flag_currency or not norm_currency or flag_currency == norm_currency
		if (
			currency_ok
			and srv_flag.get("reservation") == reservation
			and abs(flt(srv_flag.get("amount")) - flt(home_amount)) < max(tol, 0.01 if prec <= 2 else tol)
			and srv_flag.get("approver")
			and (srv_flag.get("reason") or "").strip()
		):
			return str(srv_flag["approver"]), str(srv_flag["reason"]).strip()

	if override_approval is not None:
		if isinstance(override_approval, str):
			override_approval = frappe.parse_json(override_approval)
		if not isinstance(override_approval, dict):
			frappe.throw(_("Manager approval details are required."), frappe.ValidationError)
		form = getattr(frappe.local, "form_dict", None)
		if isinstance(form, dict):
			for key in ("pin", "supervisor_pin", "override_pin"):
				form.pop(key, None)
		reason = (override_approval.get("reason") or "").strip()
		if not reason:
			frappe.throw(_("Enter a reason for the room charge override."), frappe.ValidationError)
		supervisor = override_approval.get("supervisor") or override_approval.get("approver")
		pin = override_approval.get("pin")
		approver = verify_supervisor(
			property,
			supervisor,
			pin,
			roles=POS_SUPERVISOR_ROLES,
			requester=frappe.session.user,
		)
		return str(approver), reason

	return None, None


@frappe.whitelist(methods=["POST"])
@_require_pos_outlet_roles
def pos_post_room_charge(
	property: str,
	ticket_id: str,
	amount: float,
	description: str,
	room: str | None = None,
	reservation: str | None = None,
	charge_type: str = "Food & Beverage",
	is_alcohol: int = 0,
	tax_rate: float = 0,
	outlet_name: str | None = None,
	currency: str | None = None,
	override_approval=None,
	override_token: str | None = None,
	override_reason: str | None = None,
	pos_profile: str | None = None,
	reference_name: str | None = None,
	transaction_code: str | None = None,
) -> dict:
	"""Post one POS ticket to an in-house guest's bill.

	`ticket_id` is the idempotency key: the same ticket again returns the
	first posting with duplicate=true. Name the stay by room (number or id)
	and/or reservation. Pass `transaction_code` to post under a specific
	revenue code (Simphony-OPERA mapping); otherwise `is_alcohol=1` uses
	the property's beverage transaction code (default 2010). The routing
	and billing rules choose the folio by transaction code. Over-limit or
	No Post stays refuse unless accompanied by a verified duty-manager
	approval, which is audited on both sides."""
	_assert_pos_profile_access(pos_profile)
	if not (property or "").strip() and pos_profile:
		property = resolve_property_for_pos_profile(pos_profile) or ""
		if property:
			assert_property_access(property)
	ticket_id = (ticket_id or "").strip()
	if not ticket_id or len(ticket_id) > 140:
		frappe.throw(_("ticket_id is required (the POS check number, up to 140 characters)."))
	if charge_type not in CHARGE_TYPES:
		frappe.throw(_("charge_type must be one of: {0}.").format(", ".join(CHARGE_TYPES)))
	description = (description or "").strip()
	if not description:
		frappe.throw(_("Describe the charge (e.g. 'Rooftop bar - check 1042')."))
	is_alcohol = 1 if cint(is_alcohol) else 0
	# the posting path checks these again; checking first means a bad ticket
	# never claims its ticket number
	from kamra.api import (
		_check_charge_amount_and_tax,
		_lock_folio_scope,
		_post_stay_charge_once,
		_with_snapshot_retry,
	)
	from kamra.erp_bridge import is_bridge_active
	from kamra.ledger import code_is_alcohol, resolve_transaction_code
	effective_tax = 0.0 if is_bridge_active() else flt(tax_rate)
	_check_charge_amount_and_tax(property, amount, effective_tax)
	resolved_code = resolve_transaction_code(
		charge_type, transaction_code=transaction_code,
		is_alcohol=is_alcohol, property=property)
	is_alcohol = 1 if (is_alcohol or code_is_alcohol(resolved_code)) else 0
	home_amount, home, fx_err = _convert_to_home(property, flt(amount), currency)
	if fx_err or home_amount is None:
		frappe.throw(fx_err or _("POS-RC-CURRENCY: Currency conversion failed."), frappe.ValidationError)
	amount = flt(home_amount)
	prec = _currency_precision(home)
	amount_tol = 0.5 * (10 ** -prec)

	key = _charge_name(property, ticket_id)
	if frappe.db.exists(CHARGE, key):
		rec = frappe.get_doc(CHARGE, key)
		same = (
			abs(flt(rec.amount) - amount) < amount_tol
			and (not reservation or reservation == rec.reservation)
		)
		if not same:
			frappe.throw(_("POS ticket {0} was already posted with a different "
			               "amount or stay. Use a new ticket number.").format(ticket_id))
		return _result(rec, duplicate=True)

	if room and frappe.db.exists("Folio", {"name": str(room).strip(), "folio_type": "Account"}):
		frappe.throw(_("House accounts cannot be billed as a room charge."), frappe.ValidationError)
	room_id = _resolve_room(property, room) if room else None
	if room and not room_id:
		frappe.throw(_("Room {0} is not a room of this property.").format(room))
	if not room_id and not reservation:
		frappe.throw(_("Name the room or the reservation to charge."))

	# Resolve stay safely: refuse ambiguous room-only posts when guests share a room.
	if not reservation:
		in_house = frappe.db.sql(
			"""
			SELECT name FROM `tabReservation`
			WHERE property = %s AND room = %s AND status = 'Checked In'
			FOR UPDATE
			""",
			(property, room_id),
			as_dict=True,
		)
		if not in_house:
			number = _room_number(room_id) or room
			frappe.throw(_("Room {0} has no checked-in guest. Room charges need "
			               "an in-house stay - take this as a walk-in bill "
			               "instead.").format(number))
		if len(in_house) > 1:
			number = _room_number(room_id) or room
			frappe.throw(_("Room {0} has multiple checked-in guests. Pick the "
			               "specific reservation before posting.").format(number))
		reservation = in_house[0].name

	# Lock the Reservation row to serialize concurrent room-charge postings on this stay.
	res_row = _lock_reservation_row(reservation)
	if not res_row or res_row.get("status") != "Checked In" or res_row.get("property") != property:
		frappe.throw(_("That stay is not checked in here - a bill can "
		               "only be charged to an in-house guest."))
	if room_id and res_row.get("room") and room_id != res_row.get("room"):
		frappe.throw(_("That guest is not staying in this room."))
	room_id = room_id or res_row.get("room")

	folio_name, folio_type, current_balance, master_no_post = _resolve_target_folio_readonly(
		res_row,
		charge_type=charge_type,
		is_alcohol=is_alcohol,
		transaction_code=resolved_code,
		amount=amount,
	)
	if folio_type == "Account" or (folio_name and frappe.db.get_value("Folio", folio_name, "folio_type") == "Account"):
		frappe.throw(_("House accounts cannot be billed as a room charge."), frappe.ValidationError)

	no_post_blocked = (
		(res_row.get("room_charges_allowed") is not None and not cint(res_row.get("room_charges_allowed")))
		or master_no_post
	)

	from kamra.settings import get_setting
	per_ticket_limit = flt(get_setting(property, "pos_room_charge_limit") or 0)
	credit_check = cint(get_setting(property, "pos_room_charge_credit_check") or 0)
	allowance = flt(get_setting(property, "pos_room_charge_allowance") or 0)

	over_ticket_limit = bool(per_ticket_limit and flt(amount, prec) > flt(per_ticket_limit, prec))
	over_cumulative_limit = bool(
		credit_check and allowance > 0 and flt(current_balance + amount, prec) > flt(allowance, prec)
	)

	override_approver = None
	override_reason_recorded = None
	if no_post_blocked or over_ticket_limit or over_cumulative_limit:
		room_no = _room_number(room_id) or (str(room).strip() if room else "")
		override_approver, override_reason_recorded = _verify_room_charge_override(
			property=property,
			reservation=reservation,
			home_amount=amount,
			currency=home,
			room_number=room_no,
			override_approval=override_approval,
			override_token=override_token,
			override_reason=override_reason,
			pos_profile=pos_profile,
			reference_name=reference_name or ticket_id,
		)
		if not override_approver:
			if no_post_blocked:
				number = _room_number(room_id) or room_id or ""
				frappe.throw(
					_("Room {0} cannot take charges to the room (set at check-in). "
					  "Take payment at the outlet, or ask the front desk.").format(number),
					exc=RoomChargesBlocked,
					title=_("Room charges blocked"),
				)
			if over_ticket_limit:
				frappe.throw(
					_("POS-RC-OVER-LIMIT: {0} is over this property's limit for one POS room "
					  "charge ({1}). Settle it at the outlet or ask a duty manager.").format(
						amount, per_ticket_limit
					)
				)
			frappe.throw(
				_("POS-RC-OVER-LIMIT: Projected balance ({0}) exceeds this property's room "
				  "charge credit limit ({1}). Settle it at the outlet or ask a duty manager.").format(
					flt(current_balance + amount, prec), allowance
				)
			)

	# claim the ticket first: a concurrent retry now waits on this row and
	# then sees the duplicate, instead of posting a second time. Wrap with
	# snapshot-isolation retry so MariaDB 11.8 error 1020 never bubbles as 500.
	text = f"{description} (POS {ticket_id})"[:140]
	posted_by = frappe.session.user

	def _post_once():
		_lock_folio_scope(reservations=[reservation])
		if frappe.db.exists(CHARGE, key):
			return _result(frappe.get_doc(CHARGE, key), duplicate=True)
		rec = frappe.get_doc({
			"doctype": CHARGE,
			"name": key,
			"property": property,
			"ticket_id": ticket_id,
			"outlet_name": (outlet_name or "").strip()[:140] or None,
			"posted_by": posted_by,
			"reservation": reservation,
			"room": room_id,
			"charge_type": charge_type,
			"transaction_code": resolved_code,
			"description": description[:140],
			"amount": amount,
			"is_alcohol": is_alcohol,
			"override_approved_by": override_approver,
			"override_reason": (override_reason_recorded or "")[:200] or None,
		})
		try:
			rec.insert(ignore_permissions=True)  # role + property checked above
		except frappe.DuplicateEntryError:
			frappe.clear_messages()
			return _result(frappe.get_doc(CHARGE, key), duplicate=True)

		# the one write path; the integrator was authorised above and is limited
		# to room-chargeable types, the same pattern as housekeeping's minibar
		with act_as(AGENT_EMAIL):
			out = _post_stay_charge_once(
				reservation,
				charge_type,
				text,
				amount,
				gst_rate=effective_tax,
				is_alcohol=is_alcohol,
				transaction_code=resolved_code,
			)
		rec.db_set(
			{"folio": out.get("folio"), "balance_after": flt(out.get("settlement_balance", out.get("balance")))},
			update_modified=False,
		)

		from kamra.savings import log_action
		log_action(
			"pos_room_charge",
			"Folio",
			out.get("folio"),
			property,
			rationale=(
				f"{outlet_name or 'External POS'} ticket {ticket_id}: "
				f"{charge_type} {amount} -> {out.get('folio')} by {posted_by}"
			),
			channel="API",
		)
		if override_approver:
			triggers = [
				tag for tag, hit in (
					("no_post", no_post_blocked),
					("per_ticket_limit", over_ticket_limit),
					("cumulative_limit", over_cumulative_limit),
				)
				if hit
			]
			log_action(
				"pos_room_charge_override",
				"Folio",
				out.get("folio"),
				property,
				rationale=(
					f"Override ({', '.join(triggers)}) for {outlet_name or 'External POS'} "
					f"ticket {ticket_id} ({amount} {home}): {override_reason_recorded}"
				),
				approval_status="Approved",
				approver=override_approver,
				before_snapshot={
					"reservation": reservation,
					"folio": folio_name,
					"balance": current_balance,
					"no_post": 1 if no_post_blocked else 0,
					"per_ticket_limit": per_ticket_limit,
					"allowance": allowance,
				},
				after_snapshot={
					"ticket_id": ticket_id,
					"folio": out.get("folio"),
					"amount": amount,
					"balance": flt(out.get("settlement_balance", out.get("balance"))),
					"override_approved_by": override_approver,
					"override_reason": override_reason_recorded,
				},
				channel="API",
			)
		return _result(rec, duplicate=False)

	return _with_snapshot_retry(_post_once)
