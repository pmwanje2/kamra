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
from decimal import InvalidOperation
from functools import partial, wraps
from typing import NamedTuple

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
from kamra.room_charge_money import available_credit, minor_amount, rounded_tip

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
CHARGE_TYPES = ("Food & Beverage", "Minibar", "Laundry", "Spa", "Misc", "Outlet Tip")


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
	if rec.get("tip_amount"):
		out["tip_amount"] = flt(rec.tip_amount)
	if rec.get("credit_warning"):
		out["credit_warning"] = rec.credit_warning
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


def _quote_routing(res, charge_type, amount, description, is_alcohol, tax_rate, code):
	from kamra.api import _resolve_charge_gst
	from kamra.routing import route

	res = frappe._dict(res)
	rate = _resolve_charge_gst(res.property, charge_type, description, is_alcohol, tax_rate,
	                           transaction_code=code)
	parts = route(res, charge_type, amount, transaction_code=code, gst_rate=rate,
	              resolve_window=partial(_lookup_credit_window, opening_property=res.property))
	return parts, rate


def _resolve_target_folio_readonly(res, charge_type, code, parts) -> tuple[str | _UnopenedFolio | None, str, float, bool, str | None]:
	"""Identify the first bill returned by posting, or apply the stay's billing defaults."""
	from kamra.folio import COMPANY_BILLING_DEFAULTS, _company_pays, _window_key

	open_acct = frappe.db.get_value(
		"Folio",
		{"reservation": res["name"], "folio_type": "Account", "status": "Open"},
		"name",
	)
	if open_acct:
		return open_acct, "Account", _current_folio_balance(open_acct), False, res["name"]

	prop = res.get("property")

	def _window_1_readonly() -> tuple[str | _UnopenedFolio | None, str, float, bool, str | None]:
		from kamra.folio import _is_closed_or_invoiced, sales_invoice_field

		# Select the same guest bill as open_folio, then follow its window if posting would replace it.
		guest = frappe.db.get_value(
			"Folio", {"reservation": res["name"], "folio_type": "Guest"},
			["name", "window_no", "status", "invoice_number", "reservation", "folio_type", "payee_type", *sales_invoice_field()],
			as_dict=True, order_by="window_no asc, status desc, creation desc",
		)
		if guest:
			if not _is_closed_or_invoiced(guest):
				return guest.name, "Guest", _current_folio_balance(guest.name), False, _guest_payer(guest)
			bill, payer = _lookup_credit_window(reservation=res["name"], window_no=guest.window_no or 1,
			                                   opening_property=prop)
			if isinstance(bill, str):
				kind = frappe.db.get_value("Folio", bill, "folio_type")
				return bill, kind, _current_folio_balance(bill), False, payer
			return bill, "Guest", 0.0, False, payer
		any_acct = frappe.db.get_value(
			"Folio",
			{"reservation": res["name"], "folio_type": "Account"},
			"name",
		)
		if any_acct:
			return any_acct, "Account", _current_folio_balance(any_acct), False, res["name"]
		return None, "Guest", 0.0, False, res["name"]

	if parts:
		no_post = _routed_no_post(res, parts)
		bill, payer = parts[0]["folio"]
		if isinstance(bill, str):
			kind = frappe.db.get_value("Folio", bill, "folio_type")
			return bill, kind, _current_folio_balance(bill), no_post, payer
		# A replacement inherits the previous window's type and opens with zero balance.
		key = _window_key(bill.reservation, bill.group_booking)
		kind = frappe.db.get_value("Folio", {**key, "window_no": bill.window_no}, "folio_type",
		                           order_by="creation desc")
		if not kind:
			kind = "Group" if bill.group_booking else "Guest" if bill.window_no == 1 else "Extra"
		return None, kind, 0.0, no_post, payer

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
			return gf, "Group", _current_folio_balance(gf), master_no_post, gb.master_reservation

	company = res.get("company")
	if (
		company
		and not (opted_out and gb and company == gb.company)
		and _company_pays(company, charge_type, prop, code)
	):
		bill, _payer = _company_credit_window(frappe._dict(res))
		cf = bill if isinstance(bill, str) else None
		return cf, "Company", _current_folio_balance(cf), False, res["name"]

	return _window_1_readonly()


def _scheduled_night_lines(res, day, currency, rounding_cache, for_update):
	"""Room and included package amounts, before applying the night's routing instructions."""
	from decimal import Decimal

	from kamra import folio as fm
	from kamra.packages import active_package_for_reservation, split_package_night
	from kamra.shares import nightly_share

	if res.share_group_id:
		share = nightly_share(res, day, lock=for_update, log_missing_fx=False, rounding_cache=rounding_cache)
		if share is not None:
			yield "Room", None, share["rate"], share["gst"], share["tax"]
		return

	day_use = fm._day_use_room_charge(res, day)
	current = None if day_use else fm.current_date_contract_night(res, day, log_missing=False)
	if day_use:
		amount, gst, _label = day_use
	elif current:
		amount, gst = current["rate"], current["gst_rate"]
	else:
		amount, gst = fm._nightly_room_rate(res, day), fm._nightly_gst(res, day)
	amount = Decimal(str(amount)) if gst else minor_amount(amount, currency)
	quote = fm._quoted_room_night(res, str(day))
	gross = None if current or day_use else quote.get("sold_room_gross")
	tax = Decimal(str(gross)) - amount if gross is not None else None
	package = active_package_for_reservation(res, day)
	if not package:
		yield "Room", None, amount, gst, tax
		return

	gst = quote.get("room_gst_rate", gst)
	split = split_package_night(res, package, amount, day, gst_rate=gst, sold_gross=gross)
	included = [line for line in split["elements"] if line["included_in_rate"]]
	if gross is not None:
		tax = Decimal(str(gross)) - split["night_total"] - sum(line["gst_amount"] for line in included)
	yield "Room", None, split["room_net"], gst, tax
	for line in included:
		yield line["charge_type"], line["transaction_code"], line["amount"], line["gst_rate"], line.get("gst_amount")


class _UnopenedFolio(NamedTuple):
	"""Read-only identity for the new bill of a stay or group window."""
	reservation: str | None = None
	group_booking: str | None = None
	window_no: int | None = None


def _company_credit_window(res, *, for_update=False, forecast_only=False):
	from kamra.folio import _window_key, existing_company_folio, next_window, window_cap

	window = existing_company_folio(res, for_update=for_update)
	if window:
		if window.window_no:
			return _lookup_credit_window(reservation=res.name, window_no=window.window_no, for_update=for_update,
			                             opening_property=None if forecast_only else res.property)
		return window.name, None

	# split_folio first reuses an empty company split, then allocates the next stay window.
	for bill in frappe.db.get_values("Folio", {"reservation": res.name, "folio_type": "Company", "status": "Open"},
	                                 ["name", "grand_total", "payments_total"], as_dict=True, for_update=for_update):
		if not bill.grand_total and not bill.payments_total:
			return bill.name, None
	key = _window_key(res.name)
	if forecast_only:
		# Unallocated company nights stay separate from every numbered guest window.
		cap = window_cap(res.property)
		occupied = frappe.db.get_values("Folio", {**key, "window_no": (">", 1)}, "window_no",
		                                pluck=True, for_update=for_update)
		if set(range(2, cap + 1)).issubset(occupied):
			return _UnopenedFolio(reservation=res.name), None
	number = next_window(key, res.property, start=2, for_update=for_update)
	return _UnopenedFolio(reservation=res.name, window_no=number), None


def _default_credit_destination(res, charge_type, code, for_update, *, forecast_only=False):
	from kamra.folio import legacy_billing_type

	kind = legacy_billing_type(res, charge_type, code, for_update=for_update)
	if kind == "Group":
		return _lookup_credit_window(group_booking=res.group_booking, for_update=for_update)
	if kind == "Company":
		return _company_credit_window(res, for_update=for_update, forecast_only=forecast_only)
	return _lookup_credit_window(reservation=res.name, for_update=for_update)


def _scheduled_exposure(res, currency, for_update):
	from collections import defaultdict
	from decimal import Decimal

	from kamra.folio import billable_stay_dates, posted_room_nights, routed_figure
	from kamra.ledger import resolve_transaction_code
	from kamra.routing import route

	exposure = defaultdict(Decimal)
	posted = posted_room_nights(res.name, lock=for_update)
	rounding_cache = {}
	defaults = {}
	default_codes = {}
	pending = []
	has_routes = frappe.db.get_value("Routing Instruction", {"reservation": res.name, "disabled": 0}, for_update=for_update)
	for day in billable_stay_dates(res):
		if day in posted:
			continue
		for kind, code, base, gst, tax in _scheduled_night_lines(res, day, currency, rounding_cache, for_update):
			if not code:
				if kind not in default_codes:
					default_codes[kind] = resolve_transaction_code(kind, property=res.property, seed_defaults=False)
				code = default_codes[kind]
			parts = has_routes and route(res, kind, base, day, transaction_code=code, gst_rate=gst, tax=tax,
			                             pending=pending, resolve_window=_lookup_credit_window, for_update=for_update)
			if not parts:
				if (kind, code) not in defaults:
					defaults[kind, code] = _default_credit_destination(res, kind, code, for_update, forecast_only=True)
				parts = [{"folio": defaults[kind, code], "amount": base}]
			for part in parts:
				bill, guest = part["folio"]
				amount = minor_amount(part["amount"], currency)
				# A first company bill can claim a numbered window that currently defaults
				# to the guest. Keep the window total as well as the guest pool total.
				exposure[None, bill] += amount
				if guest:
					exposure[guest, None] += amount
				if part.get("routing_instruction"):
					line_base = Decimal(str(part["amount"]))
					line_tax = part.get("gst_amount", tax)
					if line_tax is None:
						line_tax = line_base * Decimal(str(gst or 0)) / 100
					figure = routed_figure(res.property, kind, line_base, line_tax)
					pending.append((part["routing_instruction"], day, figure))
	return exposure


def _remaining_room_charges(prop, payer, folio, currency, for_update, forecasts):
	"""Only price active stays that can bill the target guest pool or independent folio."""
	from kamra.shares import ACTIVE

	owner = payer
	group = None
	if isinstance(folio, _UnopenedFolio):
		owner = payer or folio.reservation
		group = folio.group_booking
	elif folio and not payer:
		bill = frappe.db.get_value("Folio", folio, ["reservation", "group_booking", "folio_type"],
		                           as_dict=True, for_update=for_update)
		if bill:
			owner = bill.reservation
			group = bill.group_booking if bill.folio_type == "Group" else None
	candidates = set()
	if owner:
		candidates.add(owner)
		candidates.update(frappe.db.get_values("Routing Instruction", {
			"property": prop, "disabled": 0, "target_type": "Room", "target_reservation": owner,
			"reservation": ("is", "set")}, "reservation", pluck=True, for_update=for_update))
	if group:
		candidates.update(frappe.db.get_values("Reservation", {"property": prop, "group_booking": group,
		                                      "status": ("in", ACTIVE)}, "name", pluck=True, for_update=for_update))
	remaining = minor_amount(0, currency)
	if candidates:
		filters = {"name": ("in", sorted(candidates)), "property": prop, "status": ("in", ACTIVE)}
		for name in frappe.db.get_values("Reservation", filters, "name", pluck=True, order_by="name", for_update=for_update):
			if name not in forecasts:
				res = frappe.get_doc("Reservation", name, for_update=for_update)
				forecasts[name] = _scheduled_exposure(res, currency, for_update)
			remaining += forecasts[name].get((payer, None if payer else folio), 0)
	return remaining


def _credit_position(prop, payer, folio, allowance, currency, *, for_update=False, forecasts=None):
	from kamra.deposit import _effective_preauth
	from kamra.folio import security_deposit_held_on_folio

	held = balance = minor_amount(0, currency)
	if payer:
		res = frappe.get_doc("Reservation", payer)
		if res.property != prop:
			frappe.throw(_("The paying stay belongs to a different property."), frappe.PermissionError)
		deposit = frappe.db.get_value(
			"Security Deposit", {"property": prop, "reservation": payer},
			["status", "preauth_hold", "expires_on", "authorized_amount"], as_dict=True)
		places = _currency_precision(currency)
		hold = _effective_preauth(deposit, places, 0.5 * 10**-places)
		for name in _guest_window_names(prop, payer, for_update=for_update):
			balance += minor_amount(_current_folio_balance(name), currency)
			cash = security_deposit_held_on_folio(frappe.get_doc("Folio", name, for_update=for_update))
			held += minor_amount(cash, currency)
		held = max(0, held) + minor_amount(hold["remaining_hold_amount"], currency)
	elif isinstance(folio, str):
		balance = minor_amount(_current_folio_balance(folio), currency)
	remaining = _remaining_room_charges(prop, payer, folio, currency, for_update, {} if forecasts is None else forecasts)

	return {
		"reservation": payer, "currency": currency, "held": float(held),
		"allowance": float(minor_amount(allowance, currency)),
		"balance": float(minor_amount(balance, currency)), "remaining_nights_amount": float(remaining),
		"available_credit": float(available_credit(held, allowance, balance, remaining, currency)),
	}


@frappe.whitelist()
@_require_pos_outlet_roles
def room_charge_credit(property: str, reservation: str, pos_profile: str | None = None) -> dict:
	"""Credit shared by the checked-in guest's billing windows, including outstanding closed bills."""
	from kamra.ledger import home_currency
	from kamra.settings import get_setting

	_assert_pos_profile_access(pos_profile)
	assert_property_access(property)
	res = frappe.get_doc("Reservation", reservation)
	if res.property != property:
		frappe.throw(_("You do not have access to this reservation."), frappe.PermissionError)
	if res.status != "Checked In":
		frappe.throw(_("HSP-RC-STAY: The guest must be checked in."))
	return _credit_position(property, reservation, None,
	                        get_setting(property, "pos_room_charge_allowance"), home_currency(property))


def _guest_payer(window):
	"""Only guest-billed windows draw on the stay's pooled security."""
	if window.folio_type in ("Guest", "Extra") and window.payee_type in (None, "", "Guest"):
		return window.reservation
	return None


def _guest_window_names(prop, payer, *, for_update):
	windows = frappe.db.get_values(
		"Folio", {"property": prop, "reservation": payer}, ["name", "folio_type", "payee_type", "reservation"],
		as_dict=True, order_by="name", for_update=for_update)
	return [window.name for window in windows if _guest_payer(window) == payer]


def _legacy_credit_balance(prop, payer, kind, folio_name, opening_balance, currency, *, for_update=False):
	"""Guest windows share the reservation's allowance, including unpaid balances on closed bills."""
	if not payer or kind not in ("Guest", "Extra"):
		return opening_balance
	balance = minor_amount(opening_balance, currency)
	for name in _guest_window_names(prop, payer, for_update=for_update):
		if name != folio_name:
			balance += minor_amount(_current_folio_balance(name), currency)
	return float(balance)


def _credit_destination(name, default_payer, *, for_update=False):
	if not isinstance(name, str):
		return name, default_payer
	row = frappe.db.get_value("Folio", name, ["reservation", "folio_type", "payee_type"],
	                          as_dict=True, for_update=for_update)
	return name, _guest_payer(row)


def _routed_no_post(res, parts, *, for_update=False):
	"""Check only destinations that receive a share, independently of who pays their bill."""
	instructions = {part["routing_instruction"] for part in parts if part.get("routing_instruction")}
	if not instructions:
		return False
	targets = frappe.db.get_values("Routing Instruction", {"name": ("in", sorted(instructions))},
	                               ["target_type", "target_reservation"], as_dict=True, for_update=for_update)
	stays = {target.target_reservation for target in targets if target.target_type == "Room"}
	if res.get("group_booking") and any(target.target_type == "Group Master" for target in targets):
		master = frappe.db.get_value("Group Booking", res["group_booking"], "master_reservation", for_update=for_update)
		if master:
			stays.add(master)
	return bool(stays and frappe.db.get_value("Reservation", {
		"name": ("in", sorted(stays)), "room_charges_allowed": 0,
	}, "name", for_update=for_update))


def _lookup_credit_window(reservation=None, group_booking=None, window_no=1, *, for_update=False, opening_property=None):
	"""Read-only routing target. Keep the payer even when the writer must open a window."""
	from kamra.folio import _window_key, sales_invoice_field, window_cap
	key = {**_window_key(reservation, group_booking), "window_no": window_no}
	open_filters = {**key, "status": "Open", "invoice_number": ("is", "not set")}
	if sales_invoice_field():
		open_filters["sales_invoice"] = ("is", "not set")
	name = frappe.db.get_value("Folio", open_filters, "name", order_by="creation desc", for_update=for_update)
	if name:
		return _credit_destination(name, reservation, for_update=for_update)
	# window_folio opens a fresh bill, inheriting the previous window's payee.
	last = frappe.db.get_value("Folio", key, ["reservation", "folio_type", "payee_type"],
	                           as_dict=True, order_by="creation desc", for_update=for_update)
	if not last and reservation and window_no == 1:
		# window_folio reuses an open guest bill from before windows were numbered.
		legacy = frappe.db.get_value("Folio", {**open_filters, "folio_type": "Guest",
		                             "window_no": ("is", "not set")}, "name", for_update=for_update)
		if legacy:
			return _credit_destination(legacy, reservation, for_update=for_update)
	if opening_property:
		cap = window_cap(opening_property)
		if not 1 <= window_no <= cap:
			frappe.throw(_("Window must be from 1 to {0}.").format(cap))
	return _UnopenedFolio(reservation, group_booking, window_no), _guest_payer(last) if last else reservation


def _parts_over_credit(prop, currency, allowance, charge_type, rate, parts, destinations, tip=0, *, for_update=False, available=None, forecasts=None):
	"""Measure each document's change, then combine all shares drawing on the same security."""
	from collections import defaultdict
	from decimal import Decimal

	from kamra.folio import _is_closed_or_invoiced, _recalculate

	exposure = defaultdict(Decimal)
	forecasts = {} if forecasts is None else forecasts
	available = {} if available is None else available
	lines = defaultdict(list)
	for part, destination in zip(parts, destinations, strict=True):
		lines[destination].append(part)
	for (name, payer), shares in lines.items():
		doc = frappe.get_doc("Folio", name, for_update=for_update) if isinstance(name, str) else None
		if not doc or _is_closed_or_invoiced(doc):
			doc = frappe.get_doc({"doctype": "Folio", "property": prop})
		before = minor_amount(doc.grand_total, currency)
		for part in shares:
			amount = part["amount"] if rate else minor_amount(part["amount"], currency)
			doc.append("charges", {"charge_type": charge_type, "amount": amount, "gst_rate": rate,
			                       "tax_as_posted": int("gst_amount" in part), "gst_amount": part.get("gst_amount")})
		_recalculate(doc)
		key = (payer, None if payer else name)
		exposure[key] += minor_amount(doc.grand_total, currency) - before
		if (name, payer) == destinations[0]:
			exposure[key] += tip
		if key not in available:
			position = _credit_position(prop, payer, name, allowance, currency, for_update=for_update, forecasts=forecasts)
			available[key] = minor_amount(position["available_credit"], currency)
	return any(value > available[key] for key, value in exposure.items())


_CREDIT_POLICIES = ("Warn", "Block", "Manager Override")


def _credit_warning():
	return frappe._("HSP-RC-CREDIT: This charge exceeds the available room-charge credit.")


def _home_tip(property, tip_amount, currency, home):
	try:
		tip = rounded_tip(tip_amount, (currency or home).strip().upper())
		if tip:
			converted, _home_currency, error = _convert_to_home(property, float(tip), currency)
			if error or converted is None:
				frappe.throw(error or _("Currency conversion failed."))
			tip = rounded_tip(converted, home)
		return tip
	except (ValueError, InvalidOperation):
		frappe.throw(_("HSP-RC-TIP: Enter a non-negative tip within the supported amount range."))


def _ticket_description(description, ticket_ref):
	return f"{(description or '').strip()} (POS {(ticket_ref or '').strip()})"[:140]


def _evaluate_stay(
	property: str,
	res: dict,
	amount: float | None = None,
	currency: str | None = None,
	charge_type: str = "Food & Beverage",
	is_alcohol: int = 0,
	transaction_code: str | None = None,
	description: str = "",
	tax_rate: float = 0,
	tip_amount=0,
) -> dict:
	from kamra.api import _check_charge_amount_and_tax
	from kamra.ledger import code_is_alcohol, home_currency, resolve_transaction_code
	from kamra.pricing import round_rate_amount
	from kamra.settings import get_setting

	room_no = _room_number(res.get("room"))
	departure = str(res["check_out_date"]) if res.get("check_out_date") else None
	masked_name = _masked_guest_name(res.get("guest_name"))

	resolved_code = resolve_transaction_code(
		charge_type,
		transaction_code=transaction_code,
		is_alcohol=cint(is_alcohol),
		property=property, seed_defaults=False,
	)
	effective_alcohol = 1 if (cint(is_alcohol) or code_is_alcohol(resolved_code)) else 0
	home_amount, home, fx_err = _convert_to_home(property, amount, currency)

	parts, rate = _quote_routing(res, charge_type, home_amount, description, effective_alcohol, tax_rate, resolved_code)
	prec = _currency_precision(home)
	charge_amount = None
	if home_amount is not None:
		# Untaxed lines are rounded individually by _post_folio_charge; taxable lines retain their base.
		line_amount = home_amount if rate else float(round_rate_amount(home_amount, 0, "Nearest", home))
		charge_amount = round(line_amount, prec)
		if line_amount != amount:
			_check_charge_amount_and_tax(property, line_amount, tax_rate)
	target, folio_type, balance, master_no_post, payer = _resolve_target_folio_readonly(
		res, charge_type, resolved_code, parts)
	folio_name = target if isinstance(target, str) else None
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

	tip = _home_tip(property, tip_amount, currency, home) if not fx_err else 0
	if tip and charge_type != "Outlet Tip":
		resolve_transaction_code("Outlet Tip", property=property, seed_defaults=False)
	total = float(minor_amount(home_amount, home) + tip) if tip and home_amount is not None else home_amount
	no_post = 1 if (
		(res.get("room_charges_allowed") is not None and not cint(res.get("room_charges_allowed")))
		or master_no_post
	) else 0

	per_ticket_limit = flt(get_setting(property, "pos_room_charge_limit") or 0)
	policy = get_setting(property, "pos_room_charge_credit_check")
	credit_check = str(policy) in ("1", "True", "true")
	allowance = flt(get_setting(property, "pos_room_charge_allowance") or 0)

	credit = None
	forecasts = {}
	if policy in _CREDIT_POLICIES:
		if parts:
			bill, payer = parts[0]["folio"]
		elif folio_type in ("Group", "Company") and not folio_name:
			bill, payer = _default_credit_destination(res, charge_type, resolved_code, False)
		else:
			bill, payer = _credit_destination(target, payer)
		credit = _credit_position(property, payer, bill, allowance, home, forecasts=forecasts)
		limit_out = max(credit["held"], credit["allowance"])
		available_out = credit["available_credit"]
		if per_ticket_limit:
			available_out = min(available_out, per_ticket_limit)
	elif credit_check and allowance > 0:
		limit_out = allowance
		credit_balance = _legacy_credit_balance(property, payer, folio_type, folio_name, balance, home)
		available_out = max(0.0, flt(allowance - credit_balance, prec))
		if per_ticket_limit > 0:
			available_out = min(available_out, per_ticket_limit)
	elif per_ticket_limit > 0:
		limit_out = per_ticket_limit
		available_out = per_ticket_limit
	else:
		limit_out = 0.0
		available_out = None

	if home_amount is not None and credit and not parts:
		parts = [{"folio": (bill, payer), "amount": home_amount}]

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
		if per_ticket_limit > 0 and flt(total, prec) > flt(per_ticket_limit, prec):
			posting_allowed = False
			override_required = True
			reason_code = "POS-RC-OVER-LIMIT"
			reason = (
				f"Charge ({total:g} {home}) exceeds the room charge limit "
				f"({per_ticket_limit:g} {home}). Manager approval is required."
			)
		elif credit_check and allowance > 0 and flt(credit_balance + total, prec) > flt(allowance, prec):
			posting_allowed = False
			override_required = True
			reason_code = "POS-RC-OVER-LIMIT"
			reason = (
				f"Projected balance ({flt(credit_balance + total, prec):g} {home}) exceeds the "
				f"room charge credit limit ({allowance:g} {home}). Manager approval is required."
			)

	if credit and home_amount is not None:
		available = {(payer, None if payer else bill): minor_amount(credit["available_credit"], home)}
		destinations = [part["folio"] for part in parts]
		if _parts_over_credit(property, home, allowance, charge_type, rate, parts, destinations,
		                      tip, available=available, forecasts=forecasts):
			credit["credit_warning"] = _credit_warning()
			if policy == "Block" or (posting_allowed and policy == "Manager Override"):
				posting_allowed = False
				override_required = policy == "Manager Override"
				reason_code = "HSP-RC-CREDIT"
				reason = credit["credit_warning"]

	return {
		"ok": True,
		"reservation": res["name"],
		"room": res.get("room"),
		"room_number": room_no,
		"guest_name": masked_name,
		"departure": departure,
		**({"room_charge_credit": credit} if credit else {}),
		"folio": folio_name,
		"folio_type": folio_type,
		"posting_allowed": posting_allowed,
		"no_post": no_post,
		"limit": limit_out,
		"balance": balance,
		"available": available_out,
		"currency": home,
		"charge_amount": charge_amount,
		"override_required": override_required,
		"reason_code": reason_code,
		"reason": reason,
	}


def _check_pos_charge_type(charge_type):
	if charge_type not in CHARGE_TYPES:
		frappe.throw(_("charge_type must be one of: {0}.").format(", ".join(CHARGE_TYPES)))


def _validated_ticket_tax(prop, amount, tax_rate):
	from kamra.api import _check_charge_amount_and_tax
	from kamra.erp_bridge import is_bridge_active

	effective_tax = 0.0 if is_bridge_active() else flt(tax_rate)
	_check_charge_amount_and_tax(prop, amount, effective_tax)
	return effective_tax


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
	description: str | None = None,
	ticket_ref: str | None = None,
	tax_rate: float = 0,
	tip_amount=0,
) -> dict:
	"""Read-only room-charge eligibility lookup for POS tills.

	Pass the posting description, ticket reference and tax rate for the ticket being checked.
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

	_check_pos_charge_type(charge_type)
	text = _ticket_description(description, ticket_ref)
	amt = flt(amount) if amount is not None and str(amount).strip() != "" else None
	if amt is not None:
		tax_rate = _validated_ticket_tax(prop, amount, tax_rate)
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
		ev = _evaluate_stay(prop, res_row, amt, currency, charge_type, is_alcohol, transaction_code, text, tax_rate, tip_amount)
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

	evaluated = [
		_evaluate_stay(prop, r, amt, currency, charge_type, is_alcohol, transaction_code, text, tax_rate, tip_amount)
		for r in rows
	]
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
	tip_amount=0,
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
	_check_pos_charge_type(charge_type)
	description = (description or "").strip()
	if not description:
		frappe.throw(_("Describe the charge (e.g. 'Rooftop bar - check 1042')."))
	is_alcohol = 1 if cint(is_alcohol) else 0
	# the posting path checks these again; checking first means a bad ticket
	# never claims its ticket number
	from kamra.api import (
		_lock_folio_scope,
		_prepare_stay_charge,
		_with_snapshot_retry,
		_write_stay_charge,
	)
	from kamra.ledger import code_is_alcohol, resolve_transaction_code
	effective_tax = _validated_ticket_tax(property, amount, tax_rate)
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

	tip = _home_tip(property, tip_amount, currency, home)

	total = float(minor_amount(amount, home) + tip) if tip else amount

	key = _charge_name(property, ticket_id)
	def duplicate():
		rec = frappe.get_doc(CHARGE, key)
		same = (
			abs(flt(rec.amount) - amount) < amount_tol
			and minor_amount(rec.get("tip_amount"), home) == tip
			and (not reservation or reservation == rec.reservation)
		)
		if not same:
			frappe.throw(_("POS ticket {0} was already posted with a different "
			               "amount or stay. Use a new ticket number.").format(ticket_id))
		return _result(rec, duplicate=True)

	if frappe.db.exists(CHARGE, key):
		return duplicate()

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

	text = _ticket_description(description, ticket_id)
	posted_by = frappe.session.user

	from kamra.folio import payment_savepoint

	@payment_savepoint()
	def _post_once():
		nonlocal room_id
		_lock_folio_scope(reservations=[reservation])
		if frappe.db.exists(CHARGE, key):
			return duplicate()
		# Lock the Reservation row to serialize concurrent room-charge postings on this stay.
		res_row = _lock_reservation_row(reservation)
		if not res_row or res_row.get("status") != "Checked In" or res_row.get("property") != property:
			frappe.throw(_("That stay is not checked in here - a bill can "
			               "only be charged to an in-house guest."))
		if room_id and res_row.get("room") and room_id != res_row.get("room"):
			frappe.throw(_("That guest is not staying in this room."))
		room_id = room_id or res_row.get("room")

		quoted_parts, _rate = _quote_routing(res_row, charge_type, amount, text, is_alcohol, effective_tax, resolved_code)
		target, folio_type, current_balance, master_no_post, payer = _resolve_target_folio_readonly(
			res_row, charge_type, resolved_code, quoted_parts)
		folio_name = target if isinstance(target, str) else None
		if folio_type == "Account" or (folio_name and frappe.db.get_value("Folio", folio_name, "folio_type") == "Account"):
			frappe.throw(_("House accounts cannot be billed as a room charge."), frappe.ValidationError)

		no_post_blocked = (
			(res_row.get("room_charges_allowed") is not None and not cint(res_row.get("room_charges_allowed")))
			or master_no_post
		)

		from kamra.settings import get_setting
		per_ticket_limit = flt(get_setting(property, "pos_room_charge_limit") or 0)
		policy = get_setting(property, "pos_room_charge_credit_check")
		credit_check = str(policy) in ("1", "True", "true")
		allowance = flt(get_setting(property, "pos_room_charge_allowance") or 0)

		over_ticket_limit = bool(per_ticket_limit and flt(total, prec) > flt(per_ticket_limit, prec))
		over_cumulative_limit = False
		if credit_check and allowance > 0:
			current_balance = _legacy_credit_balance(property, payer, folio_type, folio_name, current_balance,
			                                        home, for_update=True)
			over_cumulative_limit = flt(current_balance + total, prec) > flt(allowance, prec)

		warning = None
		with act_as(AGENT_EMAIL):
			prepared = _prepare_stay_charge(frappe.get_doc("Reservation", reservation), charge_type, text,
			                                amount, effective_tax, is_alcohol, resolved_code)
		parts, rate, _code, _alcohol = prepared
		no_post_blocked = no_post_blocked or _routed_no_post(res_row, parts, for_update=True)
		if policy in _CREDIT_POLICIES:
			destinations = [_credit_destination(part["folio"], reservation, for_update=True) for part in parts]
			for target, _guest in destinations:
				if frappe.db.get_value("Folio", target, "folio_type") == "Account":
					frappe.throw(_("House accounts cannot be billed as a room charge."), frappe.ValidationError)
			if _parts_over_credit(property, home, allowance, charge_type, rate, parts, destinations, tip, for_update=True):
				if policy == "Block":
					frappe.throw(_credit_warning())
				warning = _credit_warning()
				over_cumulative_limit = policy == "Manager Override"

		override_approver = None
		override_reason_recorded = None
		if no_post_blocked or over_ticket_limit or over_cumulative_limit:
			room_no = _room_number(room_id) or (str(room).strip() if room else "")
			override_approver, override_reason_recorded = _verify_room_charge_override(
				property=property,
				reservation=reservation,
				home_amount=total,
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
							total, per_ticket_limit
						)
					)
				if warning:
					frappe.throw(warning)
				frappe.throw(
					_("POS-RC-OVER-LIMIT: Projected balance ({0}) exceeds this property's room "
					  "charge credit limit ({1}). Settle it at the outlet or ask a duty manager.").format(
						flt(current_balance + total, prec), allowance
					)
				)

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
			"tip_amount": float(tip),
			"credit_warning": warning if policy == "Warn" else None,
			"is_alcohol": is_alcohol,
			"override_approved_by": override_approver,
			"override_reason": (override_reason_recorded or "")[:200] or None,
		})
		try:
			rec.insert(ignore_permissions=True)  # role + property checked above
		except frappe.DuplicateEntryError:
			frappe.clear_messages()
			return duplicate()

		# the one write path; the integrator was authorised above and is limited
		# to room-chargeable types, the same pattern as housekeeping's minibar
		with act_as(AGENT_EMAIL):
			out = _write_stay_charge(reservation, charge_type, text, *prepared)
			if tip:
				from kamra.api import _post_folio_charge
				folio = out["folio"]
				tip_line = _post_folio_charge(folio, "Outlet Tip", text, float(tip), tax=0,
				                              reservation=reservation)
				out["balance"] = tip_line.get("settlement_balance", tip_line["balance"])
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
