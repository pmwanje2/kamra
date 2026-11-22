"""Seeing the rate: what a room costs on a date, and why.

Kamra prices a night in layers, all in code (kamra/pricing.py):

  BAR            the room type's base price for the guests (best available
                 rate, before anything else moves it)
  + season       a Season that covers the date (percent, amount or absolute)
  + demand       a Hurdle Rate tier when the house fills past a threshold
  = the night's rate
  + rate plan    a Rate Plan then scales the whole stay (corporate, package)
  - voucher      a Discount Voucher comes off the stay

This module only *explains* those layers for the screens (the rate calendar
and the booking dialog). It never decides a price: the numbers come from the
same pricing functions the quote uses, so what is shown is what is charged."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, getdate

from kamra.authz import assert_property_access, require_roles

RATE_ROLES = ("Front Desk", "Revenue Manager", "Hotel Admin", "Kamra Agent")


def default_plan(plans):
	return next((p.name for p in plans if p.is_default), None)


@frappe.whitelist()
@require_roles(*RATE_ROLES, scope={"room_type": "Room Type"})
def room_type_tariffs(room_type: str):
	"""Read the published tariff panel for a room type the caller can read."""
	from kamra.erp_bridge import bridge_call
	rt = frappe.get_doc("Room Type", room_type)
	rt.check_permission("read")
	assert_property_access(rt.property)
	return bridge_call("hospitality_tariffs.room_type_tariffs", room_type=room_type)


@frappe.whitelist()
@require_roles(*RATE_ROLES)
def list_contract_tariffs(property: str):
	"""Read the property's tariff directory if the integration is installed."""
	from kamra.erp_bridge import bridge_call

	assert_property_access(property)
	result = bridge_call("hospitality_tariffs.list_tariffs", property=property)
	if result is not None:
		return result
	return {"supported": False, "property": property, "price_lists": [], "items": [],
	        "can_propose": False, "can_approve": False}


@frappe.whitelist(methods=["POST"])
@require_roles("Revenue Manager", "Hotel Admin")
def propose_contract_tariff(property: str, price_list: str, item_code: str,
                            price_list_rate: float, valid_from: str, valid_upto: str | None = None):
	"""Propose a new version; the integration owns effective-date validation."""
	from kamra.erp_bridge import bridge_call

	assert_property_access(property)
	return bridge_call("hospitality_tariffs.propose_tariff", property=property, price_list=price_list,
	                   item_code=item_code, price_list_rate=price_list_rate,
	                   valid_from=valid_from, valid_upto=valid_upto)


@frappe.whitelist(methods=["POST"])
@require_roles("Hotel Admin")
def approve_contract_tariff(item_price: str):
	"""Release an eligible draft through the integration's approval workflow."""
	from kamra.erp_bridge import bridge_call

	# Item prices are external IDs. The bridge resolves their property from the
	# price list and enforces scope, draft state, and separation of duties.
	return bridge_call("hospitality_tariffs.approve_tariff", item_price=item_price)


@frappe.whitelist(methods=["POST"])
@require_roles("Hotel Admin")
def discard_contract_tariff(item_price: str):
	"""Discard a draft through the same property-scoped integration."""
	from kamra.erp_bridge import bridge_call

	return bridge_call("hospitality_tariffs.discard_tariff", item_price=item_price)


def _season_label(s) -> dict:
	unit = {"Percent": "%", "Amount": "", "Absolute": ""}.get(s.adjustment_type, "")
	sign = "+" if (s.adjustment_type != "Absolute" and float(s.adjustment_value or 0) >= 0) else ""
	text = (f"{sign}{float(s.adjustment_value):g}{unit}" if s.adjustment_type != "Absolute"
	        else f"set to {float(s.adjustment_value):g}")
	return {"name": s.name, "label": s.season_name, "change": text,
	        "type": s.adjustment_type, "value": float(s.adjustment_value or 0),
	        "applies_to": s.room_type or None}


def explain_night(property: str, rt, date, adults: int = 2, children: int = 0,
                  occupancy: float | None = None, small_ok: bool = True) -> dict:
	"""One night, layer by layer. `rt` is the Room Type document."""
	from kamra.pricing import (_dec, active_season, demand_adjust, forecast_occupancy,
	                           occupancy_rate, package_covering_date, season_adjust)
	bar = occupancy_rate(rt, adults, children, date, allow_small_group=small_ok)
	season = active_season(property, date, rt.name)
	after_season = season_adjust(property, date, bar, rt.name)
	occ = forecast_occupancy(property, date) if occupancy is None else occupancy
	final, tier = demand_adjust(property, rt.name, date, after_season, occupancy=occ)
	pkg = package_covering_date(property, date, rt.name)
	return {
		"date": str(date),
		"bar": float(_dec(bar)),
		"season": _season_label(season) if season else None,
		"package": {
			"name": pkg.name,
			"label": pkg.package_name,
			"flyer_image": pkg.flyer_image or None,
			"ota_strategy": pkg.ota_strategy,
			"ota_rate": float(pkg.ota_daily_rate or 0),
			"package_nights": int(getattr(pkg, "package_nights", None) or getattr(pkg, "minimum_nights", None) or 1),
			"min_nights": int(getattr(pkg, "package_nights", None) or getattr(pkg, "minimum_nights", None) or 1),
			"extra_night_percent": float(getattr(pkg, "extra_night_percent", 0) or 100),
			"extra_night_price": float(getattr(pkg, "extra_night_price", 0) or 0),
		} if pkg else None,
		"after_season": float(_dec(after_season)),
		"demand": ({"premium_pct": tier["premium_pct"], "min_rate": tier["min_rate"],
		            "from_occupancy": tier["occupancy_from"]} if tier else None),
		"occupancy": occ,
		"rate": float(_dec(final)),
	}


@frappe.whitelist()
@require_roles(*RATE_ROLES)
def rate_calendar(property: str, from_date: str | None = None, days: int = 14,
                  adults: int = 2, children: int = 0):
	"""Every room type's rate for each of the next `days` dates, with what
	moved it. A room type's own days are read with the same functions the
	quote uses; nothing here sets a price."""
	assert_property_access(property)
	from kamra.pricing import forecast_occupancy, rates_include_tax, small_group_allows_stay
	start = getdate(from_date) if from_date else getdate(frappe.utils.nowdate())
	days = max(1, min(int(days or 14), 31))
	dates = [add_days(start, i) for i in range(days)]
	occ = {str(d): forecast_occupancy(property, d) for d in dates}
	rows = []
	for rt in frappe.get_all("Room Type", filters={"property": property, "disabled": 0},
	                         pluck="name", order_by="base_price asc"):
		doc = frappe.get_doc("Room Type", rt)
		nights = []
		for d in dates:
			small_ok = small_group_allows_stay(doc, d, add_days(d, 1))
			nights.append(explain_night(property, doc, d, int(adults), int(children),
			                            occupancy=occ[str(d)], small_ok=small_ok))
		rows.append({"room_type": doc.name, "code": doc.room_type_code,
		             "name": doc.room_type_name, "base_price": float(doc.base_price or 0),
		             "nights": nights})
	return {"from": str(start), "days": days, "tax_inclusive": bool(rates_include_tax(property)),
	        "adults": int(adults), "room_types": rows}


@frappe.whitelist()
@require_roles(*RATE_ROLES)
def rate_options(property: str, room_type: str, check_in_date: str, check_out_date: str,
                 adults: int = 2, children: int = 0, meal_plan: str | None = None,
                 voucher_code: str | None = None, rate_plan: str | None = None,
                 extra_beds: int = 0):
	"""For the booking dialog: this stay night by night (BAR, season,
	demand), the whole stay under each active rate plan against the plain
	rate, and what the voucher would do. Each total is a real quote."""
	assert_property_access(property)
	from kamra.pricing import (
		ContractRateRefused,
		quote_with_contract,
		rates_include_tax,
		small_group_allows_stay,
		validate_voucher,
	)
	from kamra.settings import get_setting
	rt = frappe.get_doc("Room Type", room_type)
	if rt.property != property:
		frappe.throw(_("That room type is not at this property."))
	nights = date_diff(check_out_date, check_in_date)
	if nights < 0:
		frappe.throw(_("Check-out cannot be before check-in."))
	small_ok = small_group_allows_stay(rt, check_in_date, check_out_date)
	night_rows = [explain_night(property, rt, add_days(check_in_date, i), int(adults), int(children),
	                            small_ok=small_ok) for i in range(max(nights, 1))]

	def total(plan=None, voucher=None):
		return quote_with_contract(property, room_type, check_in_date, check_out_date, int(adults),
		                           int(children), meal_plan or None, plan, voucher,
		                           extra_beds=extra_beds, check_voucher_limits=False)

	plain = total()
	plans = []
	active = frappe.get_all("Rate Plan", filters={"property": property, "disabled": 0},
	                        fields=["name", "rate_plan_name", "code", "modifier_type",
	                                "modifier_value", "is_default"], order_by="rate_plan_name asc, name asc")
	for rp in active:
		row = {
			"name": rp.name, "label": rp.rate_plan_name, "code": rp.code,
			"modifier": f"{rp.modifier_type} {float(rp.modifier_value or 0):g}",
			"is_default": bool(rp.is_default),
		}
		try:
			q = total(rp.name)
		except ContractRateRefused as exc:
			plans.append({**row, "available": False, "refusal": str(exc), "tariff": None})
			continue
		plans.append({**row, "available": True, "tariff": q.get("tariff"),
		              "agreed_rate": q.get("agreed_rate"),
		              "room_total": q["room_total"], "amount_after_tax": q["amount_after_tax"],
		              "vs_plain": round(q["room_total"] - plain["room_total"], 2)})
		if rp.name == rate_plan and q.get("tariff"):
			night_rows = [{"date": r["date"], "rate": q["agreed_rate"], "bar": q["agreed_rate"],
			               "after_season": q["agreed_rate"], "season": None, "demand": None,
			               "occupancy": r.get("occupancy", 0)} for r in q["nightly"]]
	voucher = None
	if (voucher_code or "").strip():
		code = voucher_code.strip().upper()
		try:
			v = validate_voucher(property, code, max(nights, 1))
			t = total(rate_plan if any(p.name == rate_plan for p in active) else None, code)
			voucher = {"code": code, "valid": True, "discount": t["discount"],
			           "kind": v.discount_type, "value": float(v.value or 0)}
		except frappe.ValidationError as e:
			voucher = {"code": code, "valid": False, "reason": str(e)}
	return {"nights": night_rows, "plans": plans, "voucher": voucher,
	        "plain": {k: plain[k] for k in ("room_total", "amount_after_tax", "discount")},
	        "default_plan": default_plan(active), "rate_plan_required": bool(get_setting(property, "rate_plan_required")),
	        # the basis of every nightly figure above (and of a typed night price)
	        "tax_inclusive": bool(rates_include_tax(property))}
