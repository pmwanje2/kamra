"""External POS interface - room charges from a third-party point of sale.

A restaurant, spa or bar running its own POS (not Kamra's) needs two calls:

    pos_room_lookup        is this room in house, and who is in it?
    pos_post_room_charge   put this ticket on their bill

Both are for a service user holding the "POS Integrator" role (or Kamra
Agent), limited to its property with a Property User Permission, calling
with its API key. Nothing here is a second way to bill: the charge goes
through api.post_stay_charge - the same path as the desk, housekeeping and
the AI agent - so the billing rules pick the folio, tax comes from the
country pack, and alcohol never lands on a Company or Group folio.

Every ticket is posted at most once: `ticket_id` (the POS's own check
number) is the idempotency key per property. Retrying a ticket returns the
first posting; reusing a ticket number for a different charge is refused.
"""

from __future__ import annotations

import hashlib

import frappe
from frappe import _
from frappe.utils import cint, flt

from kamra.authz import (
	POS_SUPERVISOR_ROLES,
	act_as,
	assert_property_access,
	require_roles,
	resolve_property_for_pos_profile,
	verify_supervisor,
)
from kamra.install import AGENT_EMAIL
from kamra.pos import RoomChargesBlocked

POS_INTEGRATOR = "POS Integrator"
ROLES = (POS_INTEGRATOR, "Kamra Agent")
CHARGE = "External POS Charge"
# what an outlet sells; room nights, fees and allowances stay with the hotel
CHARGE_TYPES = ("Food & Beverage", "Minibar", "Laundry", "Spa", "Misc")


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
@require_roles(*ROLES)
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
	in_txn = frappe.db.get_value("Folio", folio_name, ["balance", "modified"], as_dict=True)
	committed = _fresh_read_row(
		"SELECT balance, modified FROM `tabFolio` WHERE name = %s",
		(folio_name,),
	)
	if committed is not None:
		if not in_txn or str(committed.get("modified") or "") > str(in_txn.get("modified") or ""):
			return flt(committed.get("balance"))
	return flt(in_txn.get("balance")) if in_txn else 0.0


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
	res: dict, charge_type: str = "Food & Beverage", is_alcohol: int = 0
) -> tuple[str | None, str, float, bool]:
	"""Read-only mirror of `kamra.folio.target_folio` without opening new folios."""
	from kamra.folio import COMPANY_BILLING_DEFAULTS, _company_pays

	master_no_post = False
	if not is_alcohol:
		group = res.get("group_booking")
		if group:
			gb = frappe.db.get_value(
				"Group Booking",
				group,
				["company", "master_reservation", "master_pays"],
				as_dict=True,
			)
			if gb:
				company_covers = bool(gb.company and _company_pays(gb.company, charge_type, res.get("property")))
				master_covers = False
				if gb.master_reservation and not cint(res.get("pays_own_charges")):
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
						{"group_booking": group, "folio_type": "Group", "status": "Open"},
						"name",
					)
					return gf, "Group", _current_folio_balance(gf), master_no_post

		if res.get("company") and _company_pays(res.get("company"), charge_type, res.get("property")):
			cf = frappe.db.get_value(
				"Folio",
				{"reservation": res["name"], "folio_type": "Company", "status": "Open"},
				"name",
			)
			return cf, "Company", _current_folio_balance(cf), False

	gf = frappe.db.get_value(
		"Folio",
		{"reservation": res["name"], "folio_type": "Guest", "status": "Open"},
		"name",
	) or frappe.db.get_value(
		"Folio",
		{"reservation": res["name"], "folio_type": "Guest"},
		"name",
	)
	return gf, "Guest", _current_folio_balance(gf), False


def _evaluate_stay(
	property: str,
	res: dict,
	amount: float | None = None,
	currency: str | None = None,
	charge_type: str = "Food & Beverage",
	is_alcohol: int = 0,
) -> dict:
	from kamra.settings import get_setting

	room_no = _room_number(res.get("room"))
	departure = str(res["check_out_date"]) if res.get("check_out_date") else None
	masked_name = _masked_guest_name(res.get("guest_name"))

	folio_name, folio_type, balance, master_no_post = _resolve_target_folio_readonly(
		res, charge_type=charge_type, is_alcohol=cint(is_alcohol)
	)
	if folio_name:
		actual_type = frappe.db.get_value("Folio", folio_name, "folio_type")
		if actual_type == "Account":
			return {
				"ok": False,
				"reservation": res["name"],
				"room": res.get("room"),
				"room_number": room_no,
				"guest_name": masked_name,
				"departure": departure,
				"folio": folio_name,
				"folio_type": actual_type,
				"posting_allowed": False,
				"no_post": 1,
				"limit": 0.0,
				"balance": balance,
				"available": 0.0,
				"currency": "",
				"override_required": False,
				"reason_code": "POS-RC-HOUSE-ACCOUNT",
				"reason": "House accounts cannot be billed as a room charge.",
			}

	home_amount, home, fx_err = _convert_to_home(property, amount, currency)
	no_post = 1 if (
		(res.get("room_charges_allowed") is not None and not cint(res.get("room_charges_allowed")))
		or master_no_post
	) else 0

	per_ticket_limit = flt(get_setting(property, "pos_room_charge_limit") or 0)
	credit_check = cint(get_setting(property, "pos_room_charge_credit_check") or 0)
	allowance = flt(get_setting(property, "pos_room_charge_allowance") or 0)

	if credit_check and allowance > 0:
		limit_out = allowance
		available_out = max(0.0, flt(allowance - balance, 2))
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
		if per_ticket_limit > 0 and flt(home_amount, 2) > flt(per_ticket_limit, 2):
			posting_allowed = False
			override_required = True
			reason_code = "POS-RC-OVER-LIMIT"
			reason = (
				f"Charge ({home_amount:g} {home}) exceeds the room charge limit "
				f"({per_ticket_limit:g} {home}). Manager approval is required."
			)
		elif credit_check and allowance > 0 and flt(balance + home_amount, 2) > flt(allowance, 2):
			posting_allowed = False
			override_required = True
			reason_code = "POS-RC-OVER-LIMIT"
			reason = (
				f"Projected balance ({flt(balance + home_amount, 2):g} {home}) exceeds the "
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
		"override_required": override_required,
		"reason_code": reason_code,
		"reason": reason,
	}


@frappe.whitelist()
@require_roles(*ROLES)
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
) -> dict:
	"""Read-only room-charge eligibility lookup for POS tills.

	Returns a neutral shape with guest match(es), posting_allowed, no_post,
	limit, balance, available, currency, override_required, reason_code, and
	reason. Supports room number, surname prefix (no wildcards), or explicit
	reservation, and rejects house accounts and cross-property lookups."""
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
		ev = _evaluate_stay(prop, res_row, amt, currency, charge_type, is_alcohol)
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

	evaluated = [_evaluate_stay(prop, r, amt, currency, charge_type, is_alcohol) for r in rows]
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
	override_approval=None,
	override_token: str | None = None,
	pos_profile: str | None = None,
	reference_name: str | None = None,
) -> tuple[str | None, str | None]:
	"""Verify duty-manager approval for an over-limit or No Post room charge.

	Accepts either:
	1. A POS override token consumed via `ury.ury_pos.override.consume` (fails closed
	   if the hook is missing or raises),
	2. An in-process server flag `frappe.flags.kamra_room_charge_override` set after
	   the POS already consumed the token in this transaction, or
	3. A direct `override_approval` dict (`{supervisor, pin, reason}`) verified
	   through `kamra.authz.verify_supervisor`.
	"""
	if override_token:
		try:
			consumer = frappe.get_attr("ury.ury_pos.override.consume")
		except Exception:
			frappe.throw(
				_("Room charge override verification is unavailable. Ask a manager to resolve."),
				frappe.ValidationError,
			)
		try:
			claim = {
				"reservation": reservation,
				"amount": round(flt(home_amount), 2),
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
		return str(approver), str(reason or "Manager approved room charge")

	srv_flag = getattr(frappe.flags, "kamra_room_charge_override", None)
	if isinstance(srv_flag, dict):
		if (
			srv_flag.get("reservation") == reservation
			and abs(flt(srv_flag.get("amount")) - flt(home_amount)) < 0.01
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
@require_roles(*ROLES)
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
	pos_profile: str | None = None,
	reference_name: str | None = None,
) -> dict:
	"""Post one POS ticket to an in-house guest's bill.

	`ticket_id` is the idempotency key: the same ticket again returns the
	first posting with duplicate=true. Name the stay by room (number or id)
	and/or reservation. Over-limit or No Post stays refuse unless accompanied
	by a verified duty-manager approval, which is audited on both sides."""
	ticket_id = (ticket_id or "").strip()
	if not ticket_id or len(ticket_id) > 140:
		frappe.throw(_("ticket_id is required (the POS check number, up to 140 characters)."))
	if charge_type not in CHARGE_TYPES:
		frappe.throw(_("charge_type must be one of: {0}.").format(", ".join(CHARGE_TYPES)))
	description = (description or "").strip()
	if not description:
		frappe.throw(_("Describe the charge (e.g. 'Rooftop bar - check 1042')."))
	is_alcohol = 1 if cint(is_alcohol) else 0

	from kamra.api import _check_charge_amount_and_tax
	_check_charge_amount_and_tax(property, amount, tax_rate)
	home_amount, home, fx_err = _convert_to_home(property, flt(amount), currency)
	if fx_err or home_amount is None:
		frappe.throw(fx_err or _("POS-RC-CURRENCY: Currency conversion failed."), frappe.ValidationError)
	amount = flt(home_amount)

	key = _charge_name(property, ticket_id)
	if frappe.db.exists(CHARGE, key):
		rec = frappe.get_doc(CHARGE, key)
		same = (
			abs(flt(rec.amount) - amount) < 0.005
			and (not reservation or reservation == rec.reservation)
		)
		if not same:
			frappe.throw(_("POS ticket {0} was already posted with a different "
			               "amount or stay. Use a new ticket number.").format(ticket_id))
		return _result(rec, duplicate=True)

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
		res_row, charge_type=charge_type, is_alcohol=is_alcohol
	)
	if folio_name and frappe.db.get_value("Folio", folio_name, "folio_type") == "Account":
		frappe.throw(_("House accounts cannot be billed as a room charge."))

	no_post_blocked = (
		(res_row.get("room_charges_allowed") is not None and not cint(res_row.get("room_charges_allowed")))
		or master_no_post
	)

	from kamra.settings import get_setting
	per_ticket_limit = flt(get_setting(property, "pos_room_charge_limit") or 0)
	credit_check = cint(get_setting(property, "pos_room_charge_credit_check") or 0)
	allowance = flt(get_setting(property, "pos_room_charge_allowance") or 0)

	over_ticket_limit = bool(per_ticket_limit and flt(amount, 2) > flt(per_ticket_limit, 2))
	over_cumulative_limit = bool(
		credit_check and allowance > 0 and flt(current_balance + amount, 2) > flt(allowance, 2)
	)

	override_approver = None
	override_reason = None
	if no_post_blocked or over_ticket_limit or over_cumulative_limit:
		override_approver, override_reason = _verify_room_charge_override(
			property=property,
			reservation=reservation,
			home_amount=amount,
			override_approval=override_approval,
			override_token=override_token,
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
					flt(current_balance + amount, 2), allowance
				)
			)

	# claim the ticket first: a concurrent retry now waits on this row and
	# then sees the duplicate, instead of posting a second time
	rec = frappe.get_doc({
		"doctype": CHARGE,
		"name": key,
		"property": property,
		"ticket_id": ticket_id,
		"outlet_name": (outlet_name or "").strip()[:140] or None,
		"posted_by": frappe.session.user,
		"reservation": reservation,
		"room": room_id,
		"charge_type": charge_type,
		"description": description[:140],
		"amount": amount,
		"is_alcohol": is_alcohol,
		"override_approved_by": override_approver,
		"override_reason": (override_reason or "")[:200] or None,
	})
	try:
		rec.insert(ignore_permissions=True)  # role + property checked above
	except frappe.DuplicateEntryError:
		frappe.clear_messages()
		return _result(frappe.get_doc(CHARGE, key), duplicate=True)

	from kamra.api import post_stay_charge
	text = f"{description} (POS {ticket_id})"[:140]
	with act_as(AGENT_EMAIL):
		out = post_stay_charge(
			reservation,
			charge_type,
			text,
			amount,
			gst_rate=flt(tax_rate),
			is_alcohol=is_alcohol,
		)
	rec.db_set(
		{"folio": out.get("folio"), "balance_after": flt(out.get("balance"))},
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
			f"{charge_type} {amount} -> {out.get('folio')} by {frappe.session.user}"
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
				f"ticket {ticket_id} ({amount} {home}): {override_reason}"
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
				"balance": flt(out.get("balance")),
				"override_approved_by": override_approver,
				"override_reason": override_reason,
			},
			channel="API",
		)
	return _result(rec, duplicate=False)
