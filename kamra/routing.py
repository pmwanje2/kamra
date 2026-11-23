"""Routing instructions: which window a stay's charge goes to (OPERA
reservation routing; SAP gets the same result from the payer on each item).

One resolver sits behind folio.target_folio and the posting paths. They try,
in order:

1. the stay's active Routing Instructions, including the copies taken from
   its group at pickup;
2. the legacy presets (Company Billing Rule, master_pays), only when no
   instruction covers the charge (folio._legacy_target);
3. window 1, the guest's.

Only one active instruction may cover a code on a date (OPERA's rule), so at
most one matches. An amount or percent limit splits a hotel line into a
routed part and a remainder, both in the property currency's minor unit, and
the remainder stays on window 1. An outlet check (`whole`) routes whole or
not at all: a percent never splits it, and it routes only when it fits in
what the limit has left. Usage is never counted: it is the sum of the posted,
non-voided lines that carry the instruction, so a void lowers it.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def currency_unit(property: str) -> Decimal:
	"""The property currency's minor unit: 1 for UGX, 0.01 for INR."""
	from babel.numbers import get_currency_precision

	from kamra.ledger import home_currency
	try:
		digits = get_currency_precision(home_currency(property))
	except Exception:
		digits = 2
	return Decimal(1).scaleb(-int(digits))


def line_code(charge_type: str, transaction_code: str | None = None) -> str | None:
	if transaction_code:
		return transaction_code
	from kamra.ledger import code_for_charge_type
	return code_for_charge_type(charge_type)


def code_charge_type(code: str | None) -> str | None:
	return frappe.db.get_value("Transaction Code", code, "charge_type") if code else None


def covers(ri, charge_type: str, code: str | None) -> bool:
	"""Does the instruction route this charge type or transaction code?"""
	if cint(ri.all_codes):
		return True
	for row in ri.get("codes") or []:
		if row.transaction_code and row.transaction_code == code:
			return True
		if row.charge_type and row.charge_type == charge_type:
			return True
	return False


def applies_on(ri, day) -> bool:
	day = getdate(day)
	if ri.from_date and day < getdate(ri.from_date):
		return False
	if ri.to_date and day > getdate(ri.to_date):
		return False
	return bool(cint(ri.get(DAYS[day.weekday()])))


def used(instruction: str, day=None, pending=()) -> Decimal:
	"""What the lines routed by this instruction add up to - the day's when
	`day` is given - plus `pending` lines not saved yet
	([(instruction, posting_date, amount)])."""
	from kamra.folio import NOT_VOIDED
	total = Decimal(str(frappe.db.sql(
		"""SELECT COALESCE(SUM(fc.amount), 0) FROM `tabFolio Charge` fc
		   WHERE fc.parenttype = 'Folio' AND fc.routing_instruction = %(ri)s
		     AND (%(day)s IS NULL OR fc.posting_date = %(day)s)
		     AND """ + NOT_VOIDED,
		{"ri": instruction, "day": str(getdate(day)) if day else None})[0][0] or 0))
	for name, posting_date, amount in pending:
		if name == instruction and (not day or getdate(posting_date) == getdate(day)):
			total += Decimal(str(amount or 0))
	return total


def matching(reservation: str, charge_type: str, code: str | None, day, alcohol: bool):
	"""The stay's one active instruction that covers this charge on this
	day, or None. Liquor matches only an instruction that includes it."""
	for name in frappe.get_all("Routing Instruction",
	                           filters={"reservation": reservation, "disabled": 0},
	                           pluck="name", order_by="creation asc"):
		ri = frappe.get_doc("Routing Instruction", name)
		if not applies_on(ri, day) or not covers(ri, charge_type, code):
			continue
		if alcohol and not cint(ri.include_alcohol):
			return None
		return ri
	return None


def target(ri, res) -> str | None:
	"""The open folio the instruction sends charges to, or None when its
	target cannot take them now (the other room is not in house)."""
	from kamra import folio as fm
	window = cint(ri.target_window) or 1
	if ri.target_type == "Window":
		return fm.window_folio(reservation=res.name, window_no=window)
	if ri.target_type == "Room":
		other = frappe.db.get_value("Reservation", ri.target_reservation,
		                            ["name", "status", "property"], as_dict=True)
		if not other or other.status != "Checked In" or other.property != ri.property:
			return None
		return fm.window_folio(reservation=other.name, window_no=window)
	if ri.target_type == "Group Master":
		group = res.get("group_booking")
		if not group or frappe.db.get_value("Group Booking", group, "property") != ri.property:
			return None
		return fm.window_folio(group_booking=group, window_no=window)
	return None


def _part(folio: str, amount, instruction: str | None) -> dict:
	return {"folio": folio, "amount": amount, "routing_instruction": instruction}


def route(reservation, charge_type: str, amount=None, posting_date=None, *,
          transaction_code: str | None = None, is_alcohol=0, whole: bool = False,
          pending=()) -> list[dict] | None:
	"""Where an instruction sends this charge: [{folio, amount,
	routing_instruction}], the routed part first, or None when no
	instruction applies (the caller then uses the legacy presets).

	`whole` is an outlet check or a caller that moves a line whole: it is
	never split. `amount` None (a caller that does not know it yet) skips an
	instruction with a limit. `pending` are routed lines not saved yet."""
	from kamra import folio as fm
	res = frappe.get_doc("Reservation", reservation) if isinstance(reservation, str) \
		else reservation
	if not res or not frappe.db.exists("Routing Instruction",
	                                    {"reservation": res.name, "disabled": 0}):
		return None
	if not posting_date:
		from kamra.business_date import get_business_date
		posting_date = get_business_date(res.property)
	day = getdate(posting_date)
	code = line_code(charge_type, transaction_code)
	alcohol = bool(cint(is_alcohol)) or bool(
		code and cint(frappe.db.get_value("Transaction Code", code, "is_alcohol")))
	ri = matching(res.name, charge_type, code, day, alcohol)
	if not ri:
		return None
	dst = target(ri, res)
	if not dst:
		return None
	kind = ri.limit_type or "None"
	if kind == "None":
		return [_part(dst, amount, ri.name)]
	if amount is None:
		return None
	total = Decimal(str(amount))
	if total <= 0:
		return [_part(dst, amount, ri.name)]
	unit = currency_unit(res.property)
	if kind == "Percent":
		if whole:
			return [_part(fm.window_folio(reservation=res.name, window_no=1), amount, None)]
		routed = (total * Decimal(str(ri.limit_value or 0)) / 100).quantize(unit, ROUND_HALF_UP)
	else:
		left = Decimal(str(ri.limit_value or 0)) - used(
			ri.name, day if cint(ri.per_day) else None, pending)
		if left >= total:
			return [_part(dst, amount, ri.name)]
		if left <= 0 or whole:
			return [_part(fm.window_folio(reservation=res.name, window_no=1), amount, None)]
		routed = left.quantize(unit, ROUND_DOWN)
	routed = min(max(routed, Decimal(0)), total)
	rest = total - routed
	parts = []
	if routed > 0:
		parts.append(_part(dst, float(routed), ri.name))
	if rest > 0:
		parts.append(_part(fm.window_folio(reservation=res.name, window_no=1),
		                   float(rest), None))
	return parts


def copy_group_instructions(reservation) -> list[str]:
	"""Copy the group's instructions to a room picked up from it (OPERA: the
	rooming list inherits the posting master's routing). Each copy keeps its
	own limit and usage. Instructions changed later are not pushed to rooms
	already picked up. Idempotent per instruction."""
	group = reservation.get("group_booking")
	if not group:
		return []
	out = []
	for name in frappe.get_all(
			"Routing Instruction",
			filters={"group_booking": group, "reservation": ("is", "not set"), "disabled": 0},
			pluck="name", order_by="creation asc"):
		if frappe.db.exists("Routing Instruction", {"reservation": reservation.name,
		                                           "source_instruction": name}):
			continue
		copy = frappe.copy_doc(frappe.get_doc("Routing Instruction", name))
		copy.reservation = reservation.name
		copy.group_booking = None
		copy.source_instruction = name
		copy.insert(ignore_permissions=True)
		out.append(copy.name)
	return out


def _codes(ri) -> tuple[bool, set, set]:
	"""(all codes, charge types, transaction codes) an instruction covers;
	a transaction code also counts under its own charge type."""
	types, codes = set(), set()
	for row in ri.get("codes") or []:
		if row.charge_type:
			types.add(row.charge_type)
		if row.transaction_code:
			codes.add(row.transaction_code)
			parent = code_charge_type(row.transaction_code)
			if parent:
				types.add(parent)
	return bool(cint(ri.all_codes)), types, codes


def _overlap(a, b) -> bool:
	"""Do two instructions cover a common code on a common day?"""
	lo = max(getdate(a.from_date) if a.from_date else getdate("1900-01-01"),
	         getdate(b.from_date) if b.from_date else getdate("1900-01-01"))
	hi = min(getdate(a.to_date) if a.to_date else getdate("2999-12-31"),
	         getdate(b.to_date) if b.to_date else getdate("2999-12-31"))
	if lo > hi:
		return False
	if not any(cint(a.get(d)) and cint(b.get(d)) for d in DAYS):
		return False
	all_a, types_a, codes_a = _codes(a)
	all_b, types_b, codes_b = _codes(b)
	if all_a or all_b:
		return True
	return bool(types_a & types_b) or bool(codes_a & codes_b)


def validate_instruction(ri) -> None:
	"""The Routing Instruction controller's checks."""
	if bool(ri.reservation) == bool(ri.group_booking):
		frappe.throw(_("A routing instruction is for one stay or for one group."))
	source_property = (frappe.db.get_value("Reservation", ri.reservation, "property")
	                   if ri.reservation else
	                   frappe.db.get_value("Group Booking", ri.group_booking, "property"))
	if source_property != ri.property:
		frappe.throw(_("The stay or group must be at {0}.").format(ri.property))
	window = cint(ri.target_window)
	if window:
		from kamra.folio import window_cap
		cap = window_cap(ri.property)
		if not 1 <= window <= cap:
			frappe.throw(_("Target window must be from 1 to {0}.").format(cap))
	if ri.target_type == "Window":
		if not window:
			frappe.throw(_("Choose the window to route to."))
		if ri.target_reservation:
			frappe.throw(_("A window instruction routes within the stay; leave the target room empty."))
	elif ri.target_type == "Room":
		other = frappe.db.get_value("Reservation", ri.target_reservation,
		                            ["name", "property"], as_dict=True)
		if not other:
			frappe.throw(_("Choose the room to route to."))
		if other.property != ri.property:
			frappe.throw(_("Charges can only be routed to a room at the same property."))
		if other.name == ri.reservation:
			frappe.throw(_("To route within the stay, route to one of its windows."))
	elif ri.target_type == "Group Master":
		group = ri.group_booking or frappe.db.get_value(
			"Reservation", ri.reservation, "group_booking")
		if not group:
			frappe.throw(_("This stay is not part of a group, so it has no group master."))
		if frappe.db.get_value("Group Booking", group, "property") != ri.property:
			frappe.throw(_("The group's bill must be at the same property."))
		if ri.target_reservation:
			frappe.throw(_("A group master instruction has no target room."))
	else:
		frappe.throw(_("Route to a window, a room or the group master."))
	for row in ri.get("codes") or []:
		if bool(row.charge_type) == bool(row.transaction_code):
			frappe.throw(_("Each row names a charge type or a transaction code, not both."))
	if cint(ri.all_codes):
		if ri.get("codes"):
			frappe.throw(_("All Charges routes everything; remove the rows."))
	elif not ri.get("codes"):
		frappe.throw(_("Choose the charge types or codes to route, or tick All Charges."))
	kind = ri.limit_type or "None"
	value = flt(ri.limit_value)
	if kind == "None":
		ri.limit_value = 0
		ri.per_day = 0
	elif cint(ri.all_codes):
		frappe.throw(_("An instruction for all charges cannot carry a limit."))
	elif kind == "Amount" and value <= 0:
		frappe.throw(_("An amount limit must be more than zero."))
	elif kind == "Percent" and not 0 < value <= 100:
		frappe.throw(_("A percent limit must be more than 0 and at most 100."))
	if kind == "Percent":
		ri.per_day = 0
	if ri.from_date and ri.to_date and getdate(ri.to_date) < getdate(ri.from_date):
		frappe.throw(_("To Date is before From Date."))
	if not any(cint(ri.get(d)) for d in DAYS):
		frappe.throw(_("Tick at least one day of the week."))
	if cint(ri.disabled):
		return
	source = {"reservation": ri.reservation} if ri.reservation else {
		"group_booking": ri.group_booking, "reservation": ("is", "not set")}
	for name in frappe.get_all("Routing Instruction",
	                           filters={**source, "disabled": 0, "name": ("!=", ri.name)},
	                           pluck="name"):
		other = frappe.get_doc("Routing Instruction", name)
		if _overlap(ri, other):
			frappe.throw(_("Instruction {0} already routes some of these charges on some "
			               "of these days. Only one instruction may cover a charge on a "
			               "date: change its dates or charges, or disable it.").format(name))
