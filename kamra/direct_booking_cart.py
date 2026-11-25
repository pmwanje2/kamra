"""Public cart orchestration over the existing reservation and folio writers."""

import json
import re
from hashlib import sha256
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from typing import NamedTuple

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, today

from kamra.ledger import currency_precision, home_currency, round_money
from kamra.kamra.doctype.reservation.reservation import (
	InventoryRefused,
	is_entire_place,
	lock_property_for_inventory,
	uses_member_rooms,
	villa_lockout_conflict,
)

MAX_ROOMS = 8
MAX_NIGHTS = 30
MAX_UPSELLS = 12
BASES = {"Per stay": "On Arrival", "Per night": "Daily", "Per guest": "On Arrival"}



def _refuse_inventory() -> None:
	frappe.throw(_("One or more rooms in your cart are no longer available for the selected dates."))


def public_rate_plans(property: str, default_plan: dict | None) -> list[dict]:
	"""Only hotel-published standard rates, plus the existing public default."""
	from kamra.room_statistics import category_rules, rate_category

	rules = category_rules(property)
	plans = frappe.get_all(
		"Rate Plan", filters={"property": property, "disabled": 0, "show_on_booking_page": 1},
		fields=["name", "rate_plan_name", "billing_basis"], order_by="rate_plan_name asc",
	)
	if default_plan and all(p["name"] != default_plan["name"] for p in plans):
		plans.insert(0, default_plan)
	return [
		{"name": p["name"], "label": p["rate_plan_name"] or p["name"]}
		for p in plans if (p.get("billing_basis") or "Nightly") == "Nightly"
		and rate_category(property, p["name"]) not in rules.sign_off
	]


def public_upsells(property: str) -> dict:
	"""A published Experience can opt into fixed-charge posting with a code."""
	rows = frappe.get_all(
		"Experience",
		filters={"property": property, "disabled": 0, "show_on_booking_page": 1,
		         "direct_booking_transaction_code": ("is", "set"), "price": (">", 0)},
		fields=["name", "experience_name", "price", "direct_booking_basis",
		        "direct_booking_transaction_code"],
		order_by="experience_name asc",
	)
	codes = {r.direct_booking_transaction_code for r in rows}
	if not codes:
		return {}
	transactions = {
		row.name: row for row in frappe.get_all(
			"Transaction Code", filters={"name": ("in", sorted(codes)), "disabled": 0,
			                             "txn_type": ("in", ["Revenue", "Package"])},
			fields=["name", "property", "charge_type", "default_tax_rate"],
		) if not row.property or row.property == property
	}
	return {
		row.name: {"name": row.name, "label": row.experience_name,
		           "basis": row.direct_booking_basis or "Per stay",
		           "price": row.price, "code": transactions[row.direct_booking_transaction_code]}
		for row in rows if row.direct_booking_transaction_code in transactions
		and (row.direct_booking_basis or "Per stay") in BASES
	}


def text(value, limit: int, *, required: bool = False) -> str:
	if not isinstance(value, str) or len(value) > limit or "\x00" in value:
		frappe.throw(_("Some booking details are invalid."))
	value = value.strip()
	if required and not value:
		frappe.throw(_("Please fill in the required details."))
	return value


def number(value, low: int, high: int) -> int:
	if isinstance(value, bool) or not isinstance(value, (str, int)):
		frappe.throw(_("Invalid number of guests or pets."))
	if isinstance(value, str) and (not value.isascii() or not value.isdecimal() or len(value) > 3):
		frappe.throw(_("Invalid number of guests or pets."))
	value = int(value)
	if not low <= value <= high:
		frappe.throw(_("Invalid number of guests or pets."))
	return value


def _stay_date(value) -> str:
	value = text(value, 10, required=True)
	try:
		day = date.fromisoformat(value)
	except ValueError:
		frappe.throw(_("Enter valid stay dates."))
	if day.isoformat() != value:
		frappe.throw(_("Enter valid stay dates."))
	return value


def _parse_lines(lines) -> list[dict]:
	if isinstance(lines, str):
		try:
			lines = json.loads(text(lines, 32_000))
		except ValueError:
			frappe.throw(_("Invalid room cart."))
	if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_ROOMS:
		frappe.throw(_("Choose between one and eight rooms."))
	parsed = []
	for raw in lines:
		if not isinstance(raw, dict):
			frappe.throw(_("Invalid room cart."))
		ci = _stay_date(raw.get("check_in_date"))
		co = _stay_date(raw.get("check_out_date"))
		if not 1 <= date_diff(co, ci) <= MAX_NIGHTS:
			frappe.throw(_("Choose a future stay of one to 30 nights."))
		upsells = raw.get("upsells", [])
		if not isinstance(upsells, list) or len(upsells) > MAX_UPSELLS:
			frappe.throw(_("Invalid booking add-ons."))
		adults = number(raw.get("adults", 2), 1, 100)
		children = number(raw.get("children", 0), 0, 100)
		ages = raw.get("child_ages")
		if ages is not None:
			if not isinstance(ages, list) or len(ages) != children:
				frappe.throw(_("Enter an age for each child."))
			ages = [number(age, 0, 17) for age in ages]
		parsed.append({
			"room_type": text(raw.get("room_type"), 140, required=True),
			"check_in_date": ci, "check_out_date": co,
			"adults": adults, "children": children, "child_ages": ages,
			"meal_plan": text(raw.get("meal_plan") or "", 140),
			"rate_plan": text(raw.get("rate_plan") or "", 140),
			"voucher_code": text(raw.get("voucher_code") or "", 140),
			"pets": number(raw.get("pets", 0), 0, 20),
			"upsells": list(dict.fromkeys(text(addon, 140, required=True) for addon in upsells)),
		})
	return parsed


class _InventoryChoice(NamedTuple):
	unit: str | None
	rooms: tuple[str, ...]
	composite: bool = False


def _assign_continuous_rooms(lines: list[dict], choices: list[list[_InventoryChoice]], windows) -> None:
	"""Reserve each candidate's entire physical footprint for the stay."""
	order = sorted(range(len(lines)), key=lambda i: len(choices[i]))
	overlaps = [
		{j for j, other in enumerate(lines)
		 if line["check_in_date"] < other["check_out_date"] and other["check_in_date"] < line["check_out_date"]}
		for line in lines
	]
	assigned = {}

	def place(offset):
		if windows:
			footprints = {i: choice.rooms for i, choice in assigned.items()}
			if not all(window.fits(footprints) for window in windows):
				return False
		if offset == len(order):
			return True
		index = order[offset]
		busy = [choice for i, choice in assigned.items() if i in overlaps[index]]
		taken_rooms = {room for choice in busy for room in choice.rooms}
		for choice in choices[index]:
			if choice in busy or not taken_rooms.isdisjoint(choice.rooms):
				continue
			assigned[index] = choice
			if place(offset + 1):
				return True
			del assigned[index]
		return False

	if not place(0):
		_refuse_inventory()
	for index, choice in assigned.items():
		lines[index]["assigned_room"] = choice.rooms[0] if choice.rooms else None
		# A connecting unit holds all its members even when the desk defers
		# assigning the physical room. Individual stays retain that desk policy.
		lines[index]["composite_unit"] = choice.unit if choice.composite else None


def _check_inventory(property: str, lines: list[dict]) -> None:
	from kamra.api import _available_rooms_raw, _sellable_room_types, in_house_rooms
	from kamra.reservation_state import LIVE_STATUSES
	from kamra.siu.availability import availability, has_active_sius

	demand = defaultdict(Counter)
	choices = []
	free_units = defaultdict(dict)
	sides = defaultdict(set)
	sellable = _sellable_room_types(property, list(dict.fromkeys(line["room_type"] for line in lines)))
	for line in lines:
		room_type = line["room_type"]
		ci, co = line["check_in_date"], line["check_out_date"]
		if room_type not in sellable or villa_lockout_conflict(property, room_type, ci, co, for_update=True):
			_refuse_inventory()
		units = None
		virtual = uses_member_rooms(property, room_type)
		if has_active_sius(property, room_type):
			units = availability(property, room_type=room_type, check_in_date=ci, check_out_date=co)
			sellable_units = [unit for unit in units if unit["sellable"]]
			if not sellable_units:
				_refuse_inventory()
			rooms = [room for unit in sellable_units for room in unit["rooms"]
			         if room["housekeeping_status"] != "Out of Service"]
		elif virtual:
			rooms = []
		else:
			rooms = _available_rooms_raw(property, room_type, ci, co, exclude_out_of_service=True)
		available = {room["name"] for room in rooms}
		if available:
			# Legacy room queries omit some held statuses. Use the same live
			# status set as the Reservation writer, including day-use holds.
			bookings = frappe.get_all("Reservation", filters={
				"property": property, "room": ("in", sorted(available)),
				"status": ("in", sorted(LIVE_STATUSES)), "check_in_date": ("<", co),
				"check_out_date": (">=", ci),
			}, fields=["room", "check_in_date", "check_out_date"])
			available -= {row.room for row in bookings
			              if str(row.check_out_date) > ci or str(row.check_in_date) == ci}
			if ci <= today():
				available -= in_house_rooms(property)
		if virtual:
			candidates = [_InventoryChoice(None, ())]
		elif units is None:
			candidates = [_InventoryChoice(None, (room,)) for room in sorted(available)]
		else:
			candidates = []
			for unit in sellable_units:
				members = tuple(sorted(room["name"] for room in unit["rooms"]))
				if members and available.issuperset(members):
					candidates.append(_InventoryChoice(unit["siu"], members, unit["unit_kind"] == "composite"))
		choices.append(candidates)
		side = is_entire_place(room_type)
		composite_supply = units and any(unit["unit_kind"] == "composite" for unit in units)
		for offset in range(date_diff(co, ci)):
			night = str(add_days(ci, offset))
			sides[night].add(side)
			if len(sides[night]) > 1:
				_refuse_inventory()
			demand[room_type][night] += 1
			if units is not None:
				capacity = sum(unit["sellable_per_night"][offset] for unit in units)
				if composite_supply:
					free_units[room_type][night] = capacity
				if demand[room_type][night] > capacity:
					_refuse_inventory()

	# Include unassigned and pending reservations, out-of-order rooms, and
	# group block holds under the same inventory lock as desk writes.
	for room_type, nights in sorted(demand.items()):
		res = frappe.get_doc({
			"doctype": "Reservation", "property": property, "room_type": room_type,
			"status": "Confirmed",
		})
		try:
			res.validate_type_capacity(requested_by_night=nights, free_units_by_night=free_units.get(room_type))
		except InventoryRefused:
			frappe.clear_messages()
			_refuse_inventory()

	from kamra.siu.shared_capacity import shared_capacity_windows
	_assign_continuous_rooms(lines, choices, shared_capacity_windows(property, lines))


def _price_upsells(prop, line: dict, options: dict, guests: int) -> tuple[list[dict], Decimal, Decimal]:
	from kamra.fixed_charges import booked_charge_amount

	charges = []
	base = tax = Decimal(0)
	for name in line["upsells"]:
		option = options.get(name)
		if not option:
			frappe.throw(_("A selected add-on is no longer available."))
		basis, code = option["basis"], option["code"]
		price = round_money(option["price"], prop.name)
		qty = guests if basis == "Per guest" else 1
		row = {
			"transaction_code": code.name, "charge_type": code.charge_type or "Misc",
			"description": option["label"], "qty": qty, "unit_price": price,
			"booked_tax_rate": float(code.default_tax_rate or 0),
			"start_date": line["check_in_date"],
			"end_date": add_days(line["check_out_date"], -1) if basis == "Per night" else line["check_out_date"],
			"frequency": "On Departure" if basis == "Per stay" and code.charge_type == "Late Checkout"
			else BASES[basis],
		}
		net, row_tax = booked_charge_amount(row)
		base += net
		tax += row_tax
		charges.append(row)
	return charges, base, tax


def _price_line(prop, line: dict, rates: set, default_rate: str | None, meals: set, upsells: dict) -> dict:
	from kamra.pricing import ContractRateRefused, capacity_overflow, resolve_booking_rate_plan
	from kamra.public_api import NO_RATE_AVAILABLE, _party, _quote_public_stay
	from kamra.restrictions import evaluate_stay_restrictions

	rt = frappe.db.get_value("Room Type", line["room_type"], ["property", "disabled"], as_dict=True)
	if not rt or rt.property != prop.name or rt.disabled:
		frappe.throw(_("A selected room type is no longer available."))
	requested_rate = line["rate_plan"] or default_rate
	if requested_rate and requested_rate not in rates:
		frappe.throw(_(NO_RATE_AVAILABLE))
	meal = line["meal_plan"] or None
	if meal and meal not in meals:
		frappe.throw(_("A selected meal plan is no longer available."))
	if line["pets"] and not prop.get("pets_allowed"):
		frappe.throw(_("This property does not allow pets."))

	rtd = frappe.get_cached_doc("Room Type", line["room_type"])
	adults, children, infants = _party(rtd, line["adults"], line["children"], line["child_ages"])
	above, _beds = capacity_overflow(rtd, adults, children)
	if above:
		frappe.throw(_("Select a room that fits all guests."))
	ci, co = line["check_in_date"], line["check_out_date"]
	if ci < today():
		frappe.throw(_("Choose a future stay of one to 30 nights."))
	voucher = line["voucher_code"] or None
	try:
		rate = resolve_booking_rate_plan(
			prop.name, rtd.name, ci, co, rate_plan=requested_rate,
			adults=adults, children=children, check_availability=False,
		)
		if rate and rate not in rates:
			frappe.throw(_(NO_RATE_AVAILABLE))
		quote = _quote_public_stay(
			prop, rtd.name, ci, co, adults, children, meal,
			rate_plan=rate, voucher_code=voucher, pets=line["pets"],
		)
	except (ContractRateRefused, frappe.ValidationError):
		frappe.clear_messages()
		frappe.throw(_(NO_RATE_AVAILABLE))

	rules = quote.get("stay_rules") or {}
	minimum = max(int(prop.get("minimum_nights") or 1), int(rules.get("minimum_nights") or 1))
	if date_diff(co, ci) < minimum:
		frappe.throw(_("These dates need a stay of at least {0} nights.").format(minimum))
	stay = evaluate_stay_restrictions(prop.name, rtd.name, ci, co, rate_plan=rate)
	if not stay["allowed"]:
		frappe.throw(stay["summary"])
	charges, base, tax = _price_upsells(prop, line, upsells, adults + children)
	return {
		"room_type": rtd.name, "check_in_date": ci, "check_out_date": co,
		"adults": adults, "children": children + infants, "stay_details": {"infants": infants},
		"meal_plan": meal, "rate_plan": rate, "pets": line["pets"], "voucher_code": voucher,
		"room_total": quote["amount_after_tax"], "room_tax": quote["tax_amount"],
		"rules": rules, "charges": charges, "addon_base": base, "addon_tax": tax,
	}


def _line_totals(prop, line: dict, room_total, room_tax) -> dict:
	from kamra.public_api import _advance_terms

	room_total = Decimal(str(room_total or 0))
	room_tax = Decimal(str(room_tax or 0))
	net = round_money(room_total - room_tax + line["addon_base"], prop.name)
	gross = round_money(room_total + line["addon_base"] + line["addon_tax"], prop.name)
	due, policy = _advance_terms(prop, gross, line["rules"])
	return {
		"amount_after_tax": gross, "tax": round_money(Decimal(str(gross)) - Decimal(str(net)), prop.name),
		"advance_due": due, "payment_policy": policy,
		"balance_at_hotel": round_money(Decimal(str(gross)) - Decimal(str(due)), prop.name),
	}


def _summary(prop, rows: list[dict]) -> dict:
	return {
		"currency": home_currency(prop.name),
		"currency_decimals": currency_precision(prop.name),
		**{key: round_money(sum((Decimal(str(r[key])) for r in rows), Decimal(0)), prop.name)
		   for key in ("amount_after_tax", "tax", "advance_due", "balance_at_hotel")},
	}


def _validate_cart_vouchers(property: str, lines: list[dict]) -> None:
	from kamra.pricing import validate_voucher

	try:
		requested = Counter()
		for line in lines:
			if line["voucher_code"]:
				nights = date_diff(line["check_out_date"], line["check_in_date"])
				voucher = validate_voucher(property, line["voucher_code"], nights)
				requested[voucher.name] += 1
		for name, uses in sorted(requested.items()):
			row = frappe.db.get_value("Discount Voucher", name, ["max_uses", "times_used"],
			                          as_dict=True, for_update=True)
			if not row or (row.max_uses and (row.times_used or 0) + uses > row.max_uses):
				raise frappe.ValidationError
	except frappe.ValidationError:
		frappe.clear_messages()
		frappe.throw(_("This promo code is no longer available. Remove it and check the price again."))


def _prepare(property: str, lines: list[dict]) -> tuple:
	from kamra.public_api import _public_default_rate_plan

	if not frappe.db.exists("Property", {"name": property, "booking_engine_enabled": 1, "disabled": 0}):
		frappe.throw(_("Online booking is unavailable for this property."))
	prop = frappe.get_cached_doc("Property", property)
	default = _public_default_rate_plan(property)
	rates = {p["name"] for p in public_rate_plans(property, default)}
	meals = set(frappe.get_all("Meal Plan", filters={"property": property, "disabled": 0}, pluck="name"))
	upsells = public_upsells(property)
	_validate_cart_vouchers(property, lines)
	priced = [_price_line(prop, line, rates, default["name"] if default else None, meals, upsells)
	          for line in lines]
	_check_inventory(property, priced)
	return prop, priced


def quote(property: str, lines) -> dict:
	property = text(property, 140, required=True)
	lines = _parse_lines(lines)
	lock_property_for_inventory(property)
	prop, priced = _prepare(property, lines)
	totals = [_line_totals(prop, line, line["room_total"], line["room_tax"]) for line in priced]
	return {**_summary(prop, totals), "lines": totals}


def _insert_group(prop, priced: list[dict], guest_name: str, mode: str):
	from kamra.settings import get_setting

	if len(priced) == 1:
		return None
	return frappe.get_doc({
		"doctype": "Group Booking", "property": prop.name, "group_name": guest_name,
		"check_in_date": min(line["check_in_date"] for line in priced),
		"check_out_date": max(line["check_out_date"] for line in priced),
		"rate_plan": priced[0]["rate_plan"] if get_setting(prop.name, "rate_plan_required") else None,
		"status": "Open" if mode == "Request to Book" else "Confirmed",
	}).insert()


def book(property: str, lines, guest_name: str, phone: str, email: str = "",
         special_requests: str = "", marketing_consent: int = 0, idempotency_key: str = "") -> dict:
	from kamra.api import create_booking
	from kamra.authz import act_as
	from kamra.fixed_charges import _save_fixed_charge
	from kamra.public_api import GUEST_AGENT
	from kamra.reservation_state import resolve_instant_status
	from kamra.settings import get_setting

	guest_name = text(guest_name, 140, required=True)
	phone = text(phone, 50, required=True)
	email = text(email, 140)
	if email:
		frappe.utils.validate_email_address(email, throw=True)
	special_requests = text(special_requests, 2000)
	marketing_consent = number(marketing_consent, 0, 1)
	property = text(property, 140, required=True)
	lines = _parse_lines(lines)
	key = text(idempotency_key, 128)
	if key and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
		frappe.throw(_("Invalid checkout reference."))
	lock_property_for_inventory(property)
	booking_key = fingerprint = None
	if key:
		booking_key = "direct-cart:" + sha256(key.encode()).hexdigest()
		fingerprint = sha256(json.dumps(
			[lines, guest_name, phone, email, special_requests, marketing_consent],
			sort_keys=True, ensure_ascii=False,
		).encode()).hexdigest()
		saved = frappe.db.get_value("Reservation", {"property": property, "idempotency_key": booking_key},
		                            "cart_checkout_snapshot", for_update=True)
		if saved:
			snapshot = frappe.parse_json(saved)
			if snapshot["request"] != fingerprint:
				frappe.throw(_("This checkout reference belongs to a different booking."))
			return _replay_checkout(snapshot["result"])
	prop, priced = _prepare(property, lines)
	mode = prop.get("booking_mode") or "Instant"
	assign_rooms = mode != "Request to Book" and get_setting(prop.name, "assign_room_on_booking")
	results = []
	frappe.db.savepoint("direct_booking_cart")
	previous = frappe.flags.kamra_atomic_booking_cart
	frappe.flags.kamra_atomic_booking_cart = True
	try:
		with act_as(GUEST_AGENT):
			group = _insert_group(prop, priced, guest_name, mode)
			for index, line in enumerate(priced):
				estimated = _line_totals(prop, line, line["room_total"], line["room_tax"])
				requested = mode == "Request to Book"
				status, expires = ("Requested", None) if requested else resolve_instant_status(
					advance_due=estimated["advance_due"], hold_minutes=int(prop.get("hold_minutes") or 120),
				)
				booked = create_booking(
					property=prop.name, guest_name=guest_name, phone=phone, source="Website",
					group_booking=group.name if group else None,
					booking_type="Group" if group else "Individual",
					status=status, assign_room=0, hold_expires_on=str(expires) if expires else None,
					room=line["assigned_room"] if assign_rooms else None, sellable_unit=line["composite_unit"],
					idempotency_key=booking_key if index == 0 else None,
					**{key: line[key] for key in ("room_type", "check_in_date", "check_out_date",
					   "adults", "children", "stay_details", "meal_plan", "rate_plan", "pets", "voucher_code")},
				)
				res = frappe.get_doc("Reservation", booked["reservation"])
				for charge in line["charges"]:
					_save_fixed_charge(res.name, charge, booked_tax_rate=charge["booked_tax_rate"])
				totals = _line_totals(prop, line, res.amount_after_tax, res.get("tax_amount"))
				updated = {key: totals[key] for key in ("advance_due", "payment_policy")}
				updated["is_pay_at_hotel"] = int(totals["balance_at_hotel"] > 0)
				if special_requests:
					updated["special_requests"] = special_requests
				frappe.db.set_value("Reservation", res.name, updated)
				results.append({
					**totals, "reservation": res.name, "status": res.status,
					"precheckin_token": res.precheckin_token if res.status == "Confirmed" else None,
				})
			if email and not frappe.db.get_value("Guest", res.guest, "email", for_update=True):
				frappe.db.set_value("Guest", res.guest, "email", email)
			if marketing_consent:
				from kamra.privacy import set_marketing_consent
				set_marketing_consent(res.guest, 1, "Booking Page", prop.name)
			result = {
				**_summary(prop, results), "reservations": results,
				"group_booking": group.name if group else None,
				"confirmation": group.name if group else results[0]["reservation"],
			}
			if booking_key:
				frappe.db.set_value("Reservation", results[0]["reservation"], "cart_checkout_snapshot",
				                    frappe.as_json({"request": fingerprint, "result": result}))
	except frappe.QueryDeadlockError:
		raise
	except Exception:
		frappe.db.rollback(save_point="direct_booking_cart")
		raise
	finally:
		frappe.flags.kamra_atomic_booking_cart = previous
	return result


def _replay_checkout(result: dict) -> dict:
	"""Replay the price and confirmation without resurrecting a revoked link."""
	from kamra.public_api import _precheckin_link_active

	for booked in result["reservations"]:
		token = booked.get("precheckin_token")
		if token:
			res = frappe.get_doc("Reservation", booked["reservation"])
			if token != res.precheckin_token or not _precheckin_link_active(res):
				booked["precheckin_token"] = None
	return result
