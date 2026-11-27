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

import csv
import hashlib
import io
import itertools
import json
import math
import re
from datetime import date

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, getdate

from kamra.authz import assert_property_access, require_roles

RATE_ROLES = ("Front Desk", "Revenue Manager", "Hotel Admin", "Kamra Agent")
TARIFF_EDITORS = ("Revenue Manager", "Hotel Admin")
# rows in one mass change, import or bulk decision: hospitality-tariffs@11 refuses more (HSP-TARIFF-005)
TARIFF_BATCH_LIMIT = 200
# the largest spreadsheet an import reads (1 MiB), checked before it is parsed
TARIFF_IMPORT_BYTES = 1_048_576
# the export's customer filter for the public (no-customer) tariffs only
PUBLIC_TARIFFS = "__public__"
TARIFF_CSV_COLUMNS = ["Price List", "Currency", "Customer", "Item Code", "Room Type", "Occupancy Tier", "Rate",
                      "Valid From", "Valid Upto", "Status"]
RATE_PLAN_POLICY_FIELDS = ["name", "rate_plan_name", "code", "currency", "fx_lock_policy", "base_rate_plan",
                           "adjust_extra_person", "rounding_step", "rounding_mode"]


def default_plan(plans, *, allow_sole=False):
	"""Use a sole active plan as an implicit choice only for governed sales."""
	return next((p["name"] for p in plans if p.get("is_default")),
	            plans[0]["name"] if allow_sole and len(plans) == 1 else None)


def picker_currency(plan):
	"""Show the currency used by the tariff even when the plan inherits it."""
	if not plan.get("currency") and (plan.get("custom_tariff_price_list") or plan.get("base_rate_plan")):
		from kamra.pricing import rate_plan_currency, resolve_rate_plan_chain
		chain = resolve_rate_plan_chain(plan["name"])
		if any(link.get("custom_tariff_price_list") for link in chain):
			return rate_plan_currency(chain)
	return plan.get("currency") or None


def _rate_plan_fields(extra: tuple[str, ...] = ()) -> list[str]:
	fields = [*RATE_PLAN_POLICY_FIELDS, *extra]
	if frappe.get_meta("Rate Plan").has_field("custom_tariff_price_list"):
		fields.append("custom_tariff_price_list")
	return fields


def _room_counts(property: str) -> dict[str, int]:
	"""Count sellable inventory units per room type at `property`: active
	Sellable Units when configured for a room type (matching `has_active_sius`
	so whole-property Villas count as live inventory without double-counting
	physical rooms that also have synced individual SIUs), else physical Rooms."""
	counts: dict[str, int] = {}
	for row in frappe.get_all("Room", filters={"property": property}, fields=["room_type"]):
		rt = row.get("room_type")
		if rt:
			counts[rt] = counts.get(rt, 0) + 1
	exists = getattr(frappe.db, "exists", None)
	if callable(exists) and exists("DocType", "Sellable Unit"):
		siu_counts: dict[str, int] = {}
		for row in frappe.get_all(
			"Sellable Unit",
			filters={"property": property, "is_active": 1},
			fields=["room_type"],
		):
			rt = row.get("room_type")
			if rt:
				siu_counts[rt] = siu_counts.get(rt, 0) + 1
		for rt, count in siu_counts.items():
			counts[rt] = count
	return counts


def _plan_uses_tariff(plan_name: str | None, plans_by_name: dict[str, dict]) -> bool:
	"""True when `plan_name` (or an ancestor via base_rate_plan) carries a tariff Price List."""
	from kamra.pricing import RATE_PLAN_CHAIN_LIMIT

	seen: set[str] = set()
	current = plan_name
	while current and current in plans_by_name and current not in seen and len(seen) < RATE_PLAN_CHAIN_LIMIT:
		seen.add(current)
		row = plans_by_name[current]
		if row.get("custom_tariff_price_list"):
			return True
		current = row.get("base_rate_plan")
	return False


def all_active_plans_use_tariffs(property: str, plans: list[dict] | None = None) -> bool:
	"""Whether every active plan uses a governed price list, directly or through its base plan."""
	if plans is None:
		if not frappe.get_meta("Rate Plan").has_field("custom_tariff_price_list"):
			return False
		plans = frappe.get_all("Rate Plan", filters={"property": property, "disabled": 0},
		                       fields=["name", "base_rate_plan", "custom_tariff_price_list"])
	if not plans or "custom_tariff_price_list" not in plans[0]:
		return False
	by_name = {plan["name"]: plan for plan in plans}
	if any(plan.get("base_rate_plan") and plan["base_rate_plan"] not in by_name for plan in plans):
		# A disabled base plan can still supply the pricing of an active derived plan.
		for base in frappe.get_all("Rate Plan", filters={"property": property, "disabled": 1},
		                           fields=["name", "base_rate_plan", "custom_tariff_price_list"]):
			by_name[base["name"]] = base
	if not any(plan.get("custom_tariff_price_list") for plan in by_name.values()):
		return False
	return all(_plan_uses_tariff(plan["name"], by_name) for plan in plans)


def effective_rate_plan_required(property: str, active_plans: list[dict] | None = None) -> bool:
	from kamra.settings import get_setting

	return bool(get_setting(property, "rate_plan_required")) or all_active_plans_use_tariffs(property, active_plans)


@frappe.whitelist()
@require_roles(*RATE_ROLES, scope={"room_type": "Room Type"})
def room_type_tariffs(room_type: str):
	"""Read the published tariff panel for a room type the caller can read."""
	from kamra.erp_bridge import bridge_call
	rt = frappe.get_doc("Room Type", room_type)
	rt.check_permission("read")
	assert_property_access(rt.property)
	result = bridge_call("hospitality_tariffs.room_type_tariffs", room_type=room_type)
	if isinstance(result, dict) and isinstance(result.get("tariffs"), list):
		_price_tariff_panel(rt.property, result)
	if isinstance(result, dict):
		from kamra.settings import get_setting
		if "all_active_plans_tariff" not in result:
			result["all_active_plans_tariff"] = all_active_plans_use_tariffs(rt.property)
		result["rate_plan_required"] = bool(get_setting(rt.property, "rate_plan_required"))
	return result


def _price_tariff_panel(property: str, panel: dict) -> None:
	"""Add the engine's local equivalent and each row's plan lock and rounding,
	calculated rows for active derived plans (using the same modifier math as
	`contract_rate`), the primary (default) plan's tariff, and explicit tariff /
	card / unpriced active plan lists."""
	from decimal import Decimal

	from kamra.pricing import (
		DEFAULT_FX_LOCK,
		RATE_PLAN_CHAIN_LIMIT,
		_dec,
		_price_through,
		contract_local_amount,
		property_currency,
		round_rate_amount,
	)

	home = property_currency(property)
	plans = {plan["name"]: plan for plan in frappe.get_all(
		"Rate Plan", filters={"property": property},
		fields=_rate_plan_fields(("is_default", "disabled", "modifier_type", "modifier_value")))}
	for row in panel["tariffs"]:
		plan = plans.get(row.get("rate_plan")) or {}
		fx = _rate(row.get("fx_rate"))
		foreign = bool(row.get("currency")) and row.get("currency") != home and bool(fx and fx > 0)
		for amount, local in (("room", "local_room"), ("single", "local_single")):
			value = _rate(row.get(amount))
			row[local] = (float(contract_local_amount(plan, value, fx, home))
			              if foreign and value is not None else None)
		row.update(fx_lock_policy=plan.get("fx_lock_policy") or DEFAULT_FX_LOCK,
		           rounding_step=float(plan.get("rounding_step") or 0),
		           rounding_mode=plan.get("rounding_mode") or "Nearest")
	base_rows = {row.get("rate_plan"): row for row in panel["tariffs"] if row.get("rate_plan")}
	active = [plan for plan in plans.values() if not int(plan.get("disabled") or 0)]
	active.sort(key=lambda p: (p.get("rate_plan_name") or "", p.get("name") or ""))
	for plan in active:
		plan_name = plan["name"]
		if plan_name in base_rows or not plan.get("base_rate_plan"):
			continue
		# Walk up base_rate_plan (cycle- & depth-guarded) to find the nearest priced ancestor row.
		chain = [plan]
		seen = {plan_name}
		cur = plan.get("base_rate_plan")
		base_row = None
		while cur and cur in plans and cur not in seen and len(chain) < RATE_PLAN_CHAIN_LIMIT:
			if cur in base_rows:
				base_row = base_rows[cur]
				break
			seen.add(cur)
			parent = plans[cur]
			chain.insert(0, parent)
			cur = parent.get("base_rate_plan")
		if base_row is None:
			continue
		base_room = _rate(base_row.get("room"))
		if base_room is None or base_room <= 0:
			continue
		currency = base_row.get("currency")
		d_room_raw = _price_through(chain, _dec(base_room), Decimal(0), root=False)
		if d_room_raw <= 0:
			continue
		d_room = round_rate_amount(d_room_raw, 0, currency=currency)
		base_single = _rate(base_row.get("single"))
		d_single = None
		if base_single is not None and base_single > 0:
			d_single_raw = _price_through(chain, _dec(base_single), Decimal(0), root=False)
			if d_single_raw > 0:
				d_single = round_rate_amount(d_single_raw, 0, currency=currency)
		base_extra = _rate(base_row.get("extra"))
		d_extra = None
		if base_extra is not None:
			d_extra_val = _dec(base_extra)
			for step_plan in chain:
				if int(step_plan.get("adjust_extra_person") or 0) and step_plan.get("modifier_type") == "Percent":
					d_extra_val = _price_through([step_plan], d_extra_val, Decimal(0), root=True)
			d_extra = round_rate_amount(max(d_extra_val, Decimal(0)), 0, currency=currency)
		fx = _rate(base_row.get("fx_rate"))
		foreign = bool(currency) and currency != home and bool(fx and fx > 0)
		parent_name = plan.get("base_rate_plan")
		parent_row = base_rows.get(parent_name)
		parent_plan = plans.get(parent_name) or {}
		parent_label = (
			(parent_row.get("label") if parent_row else None)
			or parent_plan.get("rate_plan_name")
			or parent_name
		)
		panel["tariffs"].append({
			"rate_plan": plan_name,
			"label": plan.get("rate_plan_name") or plan_name,
			"currency": currency,
			"room": float(d_room),
			"single": float(d_single) if d_single is not None else None,
			"extra": float(d_extra) if d_extra is not None else None,
			"approval_state": base_row.get("approval_state"),
			"valid_from": base_row.get("valid_from") or "",
			"fx_rate": base_row.get("fx_rate"),
			"fx_date": base_row.get("fx_date"),
			"local_room": float(contract_local_amount(plan, d_room, fx, home)) if foreign else None,
			"local_single": (float(contract_local_amount(plan, d_single, fx, home))
			                 if foreign and d_single is not None else None),
			"fx_lock_policy": plan.get("fx_lock_policy") or DEFAULT_FX_LOCK,
			"rounding_step": float(plan.get("rounding_step") or 0),
			"rounding_mode": plan.get("rounding_mode") or "Nearest",
			"derived_from": parent_name,
			"derived_from_label": parent_label,
		})
	priced = {row.get("rate_plan") for row in panel["tariffs"]}
	default = default_plan(active)
	if default:
		primary = default if default in priced else None
	else:
		primary = next(iter(priced)) if len(priced) == 1 else None
	def sort_key(plan):
		return (plan.get("label") or "", plan.get("name") or "")
	if isinstance(panel.get("card_rate_plans"), list):
		tariff_plans = sorted(panel.get("tariff_plans") or [], key=sort_key)
		card = sorted(panel.get("card_rate_plans") or [], key=sort_key)
		unpriced = sorted(panel.get("unpriced_plans") or [], key=sort_key)
		all_tariff = bool(panel.get("all_active_plans_tariff"))
		panel.update(property_currency=home, primary_rate_plan=primary,
		             all_active_plans_tariff=all_tariff,
		             tariff_plans=tariff_plans, card_rate_plans=card, unpriced_plans=unpriced)
	else:
		card, unpriced = [], []
		for plan in active:
			chain, name = [], plan["name"]
			while name in plans and name not in chain and len(chain) < RATE_PLAN_CHAIN_LIMIT:
				chain.append(name)
				name = plans[name].get("base_rate_plan")
			entry = {"name": plan["name"], "label": plan.get("rate_plan_name")}
			if priced.intersection(chain):
				continue
			if _plan_uses_tariff(plan["name"], plans):
				unpriced.append(entry)
			else:
				card.append(entry)
		card.sort(key=sort_key)
		unpriced.sort(key=sort_key)
		has_tariff_meta = any(plan.get("custom_tariff_price_list") for plan in active)
		all_tariff = (bool(active) and not card) if has_tariff_meta else bool(panel.get("all_active_plans_tariff"))
		panel.update(property_currency=home, primary_rate_plan=primary,
		             all_active_plans_tariff=all_tariff, card_rate_plans=card)
		if unpriced:
			panel["unpriced_plans"] = unpriced


@frappe.whitelist()
@require_roles(*RATE_ROLES)
def room_types_summary(property: str):
	"""Effective selling rate (default plan, 2 adults), room inventory count, and
	whether any active plan actually prices from the card rate."""
	assert_property_access(property)
	from kamra import erp_bridge
	from kamra.business_date import get_business_date
	from kamra.pricing import (
		ContractRateRefused,
		property_currency,
		quote_with_contract,
		rates_include_tax,
		round_rate_amount,
	)
	from kamra.settings import get_setting

	rate_plan_required = bool(get_setting(property, "rate_plan_required"))
	erp_tax_fn = getattr(erp_bridge, "erp_determines_tax", None)
	erp_tax_managed = bool(erp_tax_fn()) if callable(erp_tax_fn) else False
	home = property_currency(property)
	inclusive = bool(rates_include_tax(property))
	business_date = getdate(get_business_date(property))
	next_day = add_days(business_date, 1)
	all_plans = frappe.get_all(
		"Rate Plan", filters={"property": property},
		fields=_rate_plan_fields(("is_default", "disabled")),
		order_by="rate_plan_name asc, name asc",
	)
	plans_by_name = {plan["name"]: plan for plan in all_plans}
	active = [plan for plan in all_plans if not int(plan.get("disabled") or 0)]
	card_plans = [plan for plan in active if not _plan_uses_tariff(plan["name"], plans_by_name)]
	has_card_rate_plans = bool(card_plans) if active else True
	default_name = default_plan(active, allow_sole=rate_plan_required or not has_card_rate_plans)
	default_doc = plans_by_name.get(default_name) if default_name else None
	default_info = (
		{**_plan_policy(default_doc), "is_tariff": _plan_uses_tariff(default_name, plans_by_name)}
		if default_doc else None
	)
	counts = _room_counts(property)
	rows = []
	for rt_name in frappe.get_all(
		"Room Type", filters={"property": property, "disabled": 0},
		pluck="name", order_by="base_price asc",
	):
		room_count = counts.get(rt_name, 0)
		if (rate_plan_required or not has_card_rate_plans) and not default_name:
			rows.append({
				"room_type": rt_name, "room_count": room_count, "priced": False,
				"source": None, "currency": None, "amount": None, "local_amount": None,
				"refusal": _("Set a default rate plan in Rate Plans."),
			})
			continue
		try:
			q = quote_with_contract(
				property, rt_name, business_date, next_day, 2, 0,
				rate_plan=default_name, check_voucher_limits=False,
			)
		except ContractRateRefused as exc:
			rows.append({
				"room_type": rt_name, "room_count": room_count, "priced": False,
				"source": None, "currency": None, "amount": None, "local_amount": None,
				"refusal": str(exc),
			})
			continue
		if q.get("tariff"):
			tariff = q["tariff"]
			t_currency = tariff.get("currency") or home
			t_amount = float(tariff.get("nightly") or 0)
			local_amount = float(q["agreed_rate"])
			rows.append({
				"room_type": rt_name, "room_count": room_count, "priced": True,
				"source": "contract", "currency": t_currency, "amount": t_amount,
				"local_amount": local_amount, "refusal": None,
			})
		else:
			acc = q["line_items"][0]
			gross = acc["amount"] + (acc["tax_amount"] if inclusive else 0)
			amt = float(round_rate_amount(gross, 0, currency=home))
			rows.append({
				"room_type": rt_name, "room_count": room_count, "priced": True,
				"source": "card", "currency": home, "amount": amt,
				"local_amount": amt, "refusal": None,
			})
	return {
		"property_currency": home,
		"has_card_rate_plans": has_card_rate_plans,
		"all_active_plans_tariff": bool(active) and not has_card_rate_plans,
		"rate_plan_required": rate_plan_required,
		"erp_tax_managed": erp_tax_managed,
		"default_plan": default_info,
		"room_types": rows,
	}


def _tariff_directory(property: str, **filters):
	"""The integration's tariff directory, with only the filters given."""
	from kamra.erp_bridge import bridge_call

	return bridge_call("hospitality_tariffs.list_tariffs", property=property,
	                   **{key: value for key, value in filters.items() if value})


@frappe.whitelist()
@require_roles(*RATE_ROLES)
def list_contract_tariffs(property: str, price_list: str | None = None, customer: str | None = None,
                          as_of_date: str | None = None):
	"""Read the property's tariff directory if the integration is installed.

	The optional filters pass through to the integration. The Rate Plans
	behind its price lists - currency, exchange-rate lock, base plan and
	rounding - are this app's own records and are added here."""
	assert_property_access(property)
	result = _tariff_directory(property, price_list=price_list, customer=customer, as_of_date=as_of_date)
	if result is not None:
		policies = _rate_plan_policies(property, result)
		if policies:
			result["rate_plan_policies"] = policies
		# Separation of duties keeps a maker from approving their own draft,
		# not from withdrawing it: a Hotel Admin with tariff access and
		# Item Price delete permission discards (matching discard_tariff).
		can_delete_price = frappe.has_permission("Item Price", "delete")
		result["can_discard"] = (
			"Hotel Admin" in frappe.get_roles()
			and bool(result.get("can_propose") or result.get("can_approve"))
			and bool(can_delete_price)
		)
		room_types = frappe.get_all(
			"Room Type", filters={"property": property, "disabled": 0},
			fields=["name", "room_type_name", "room_type_code", "base_occupancy", "adults_capacity"],
			order_by="room_type_name asc, name asc",
		)
		_enrich_tariff_items(result, room_types)
		result["unpriced_room_types"] = _unpriced_room_types(property, result, room_types)
		if result["can_discard"] and frappe.db.exists("DocType", "Reason Code"):
			result["reason_codes"] = frappe.get_all("Reason Code", filters={"category": "Adjust", "disabled": 0},
			                                      fields=["name", "description"], order_by="code asc")
		if frappe.db.exists("DocType", "Currency Exchange"):
			_directory_posting_rates(property, result, as_of_date)
		if result.get("can_propose"):
			result["room_types"] = room_types
			result["available_items"] = _tariff_item_choices(result)
			if frappe.db.exists("DocType", "Rate Guardrail"):
				result["guardrails"] = _proposal_guardrails(property, room_types)
		return result
	return {"supported": False, "property": property, "price_lists": [], "items": [],
	        "can_propose": False, "can_approve": False, "can_discard": False}


def _directory_posting_rates(property, directory, as_of):
	from kamra.business_date import get_business_date
	from kamra.pricing import posting_fx_status

	day = min(getdate(as_of or get_business_date(property)), getdate(get_business_date(property)))
	by_currency = {}
	for price in directory.get("price_lists") or []:
		currency = price.get("currency")
		if not currency or currency == directory.get("property_currency"):
			continue
		if currency not in by_currency:
			by_currency[currency] = posting_fx_status(property, currency, day)
		quote = by_currency[currency]
		price.update(fx_rate=quote["rate"], fx_date=quote["date"], workflow_state=quote["workflow_state"],
		             fx_status=quote["status"], stale_days=quote["stale_days"])
	lists = {row["name"]: row for row in directory.get("price_lists") or []}
	for item in directory.get("items") or []:
		price = lists.get(item.get("price_list")) or {}
		if "fx_status" in price:
			item.update(fx_rate=price["fx_rate"], fx_date=price["fx_date"])
			for version in [item.get("active"), *(item.get("pending") or []), *(item.get("history") or [])]:
				if version:
					version["local_equiv"] = None


def _proposal_guardrails(property, room_types):
	from kamra.pricing import rail_for_day
	rails = frappe.get_all("Rate Guardrail", filters={"property": property, "disabled": 0},
	                       fields=["room_type", "floor_price", "ceiling_price", "days_of_week"])
	result = {}
	if rails:
		for room in ["", *(rt["name"] for rt in room_types)]:
			limits = []
			for day in ("sun", "mon", "tue", "wed", "thu", "fri", "sat"):
				rail = rail_for_day(rails, room, day)
				limits.append([rail.floor_price, rail.ceiling_price] if rail else None)
			result[room] = limits
	return result


def _enrich_tariff_items(directory: dict, room_types: list[dict]):
	"""Join directory labels to unambiguous identifiers scoped to the property."""
	by_identity: dict[str, list[dict]] = {}
	for rt in room_types:
		for value in {rt.get("room_type_name"), rt.get("room_type_code"), rt["name"]} - {None, ""}:
			by_identity.setdefault(value.casefold(), []).append(rt)
	by_id = {rt["name"]: rt for rt in room_types}
	for item in directory.get("items") or []:
		rt = by_id.get(item.get("room_type_id"))
		for value in (item.get("room_type"), item.get("room_type_code")):
			if rt:
				break
			matches = by_identity.get(_text(value).casefold(), [])
			if len(matches) == 1:
				rt = matches[0]
		if rt:
			item.update(room_type_id=rt["name"], room_type_code=rt.get("room_type_code"))


def _tariff_item_choices(directory: dict) -> list[dict]:
	"""Offer directory items and readable, enabled sales items without prices."""
	choices = {
		row["item_code"]: {"item_code": row["item_code"], "item_name": row.get("item_name") or row["item_code"],
		                   "category": row.get("category"), "room_type_id": row.get("room_type_id")}
		for row in [*(directory.get("items") or []), *(directory.get("available_items") or [])]
		if row.get("item_code")
	}
	if directory.get("price_lists") and frappe.has_permission("Item", "read"):
		# An optional ERP integration owns the Item master. get_list respects its User Permissions.
		get_list = getattr(frappe, "get_list", None)
		if get_list:
			for row in get_list("Item", filters={"disabled": 0, "is_sales_item": 1},
			                    fields=["name", "item_name", "is_stock_item"], limit_page_length=0):
				code = row["name"]
				choice = choices.setdefault(code, {"item_code": code, "item_name": row.get("item_name") or code})
				choice["can_link_to_room"] = not row.get("is_stock_item")
	return sorted(choices.values(), key=lambda row: (row["item_name"].casefold(), row["item_code"]))


def _unpriced_room_types(property: str, directory: dict, room_types: list[dict]) -> list[dict]:
	"""Active room types without an effective room or venue tariff on one or more price lists."""
	list_names = [pl.get("name") for pl in directory.get("price_lists") or [] if pl.get("name")]
	if not list_names:
		return []
	priced_by_list: dict[str, set[str]] = {name: set() for name in list_names}
	for item in directory.get("items") or []:
		pl = item.get("price_list")
		room = item.get("room_type_id") or item.get("room_type")
		if pl in priced_by_list and room and item.get("category") in ("Room", "Venue") and item.get("active"):
			priced_by_list[pl].add(room)

	if not room_types:
		return []
	counts = _room_counts(property)
	missing = []
	for rt in room_types:
		name = rt.get("name")
		label = rt.get("room_type_name") or name
		missing_lists = [
			pl for pl in list_names
			if label not in priced_by_list[pl] and name not in priced_by_list[pl]
		]
		if missing_lists:
			missing.append({
				"name": name,
				"room_type_name": label,
				"room_type_code": rt.get("room_type_code") or "",
				"room_count": counts.get(name, 0),
				"price_lists": missing_lists,
			})
	return missing


def _rate_plan_policies(property: str, directory: dict) -> dict:
	"""The policies of the plans the directory's price lists name, and of their base plans."""
	named = []
	list_currencies: dict[str, str] = {}
	for price_list in directory.get("price_lists") or []:
		pl_currency = price_list.get("currency") or ""
		for plan in price_list.get("rate_plans") or []:
			name = plan.get("name") if isinstance(plan, dict) else plan
			if isinstance(name, str) and name:
				named.append(name)
				if pl_currency and name not in list_currencies:
					list_currencies[name] = pl_currency
	if not named:
		return {}
	plans = {plan["name"]: plan for plan in frappe.get_all(
		"Rate Plan", filters={"property": property}, fields=RATE_PLAN_POLICY_FIELDS)}
	wanted = []
	for name in named:
		while name in plans and name not in wanted:
			wanted.append(name)
			name = plans[name].get("base_rate_plan")
	return {name: {
		"label": plans[name].get("rate_plan_name"), "code": plans[name].get("code"),
		"currency": plans[name].get("currency") or list_currencies.get(name) or None,
		"fx_lock_policy": plans[name].get("fx_lock_policy") or "Fixed at Reservation",
		"base_rate_plan": plans[name].get("base_rate_plan") or None,
		"adjust_extra_person": bool(int(plans[name].get("adjust_extra_person") or 0)),
		"rounding_step": float(plans[name].get("rounding_step") or 0),
		"rounding_mode": plans[name].get("rounding_mode") or "Nearest",
	} for name in wanted}


@frappe.whitelist(methods=["POST"])
@require_roles("Revenue Manager", "Hotel Admin")
def propose_contract_tariff(property: str, price_list: str, item_code: str,
                            price_list_rate: float, valid_from: str, valid_upto: str | None = None,
                            customer: str | None = None, room_type: str | None = None,
                            occupancy_tier: str | None = None):
	"""Propose a new version; the integration owns effective-date validation.

	For a `room_type` and `occupancy_tier`, use its binding or link a selling
	item in the same request. A blank item code creates one if permitted.

	With `customer` the version is that customer's own price (SAP PR02
	A305). An integration that answers without that customer ignored it
	and drafted the public rate: the request is refused, so its draft rolls
	back with it, rather than publishing a corporate rate to everyone."""
	from kamra.erp_bridge import bridge_call

	assert_property_access(property)
	customer = _text(customer)
	if room_type:
		# Binding and the native proposal share the POST transaction. A refusal rolls both back.
		item_code = _bind_tariff_item(property, room_type, _text(occupancy_tier), price_list_rate, _text(item_code))
	form = {"property": property, "price_list": price_list, "item_code": item_code,
	        "price_list_rate": price_list_rate, "valid_from": valid_from, "valid_upto": valid_upto}
	if customer:
		form["customer"] = customer
	result = bridge_call("hospitality_tariffs.propose_tariff", **form)
	if room_type and (not result or result.get("workflow_state") != "Draft"):
		frappe.throw(_("The tariff could not be submitted for approval."))
	if customer and result is not None and _text(result.get("customer")) != customer:
		frappe.throw(_("This tariff integration cannot propose a customer's own rate, so nothing was saved. "
		               "Update the tariff integration, then try again."))
	return result


def _bind_tariff_item(property: str, room_type: str, tier: str, rate: float, item_code: str) -> str:
	"""Resolve an existing Room Type binding or create its native selling Item."""
	rt = frappe.get_doc("Room Type", room_type, for_update=True)
	if rt.property != property or rt.disabled:
		frappe.throw(_("Choose an active room type at this property."), frappe.PermissionError)
	rt.check_permission("write")
	base = max(1, int(rt.base_occupancy or 2))
	limit = max(base, int(rt.get("adults_capacity") or 5))
	if tier != "bed" and (len(tier) > len(str(limit)) or not re.fullmatch(r"[1-9][0-9]*", tier)
	                      or int(tier) > limit):
		frappe.throw(_("Choose an occupancy tier."))
	amount = _rate(rate)
	if amount is None or (amount < 0 if tier == "bed" else amount <= 0):
		frappe.throw(_("Enter a valid rate for this occupancy tier."))

	meta = frappe.get_meta("Room Type")
	base = str(base)
	raw_map = _text(rt.get("custom_tariff_occupancy_map"))
	tiers = {}
	if raw_map:
		try:
			tiers = json.loads(raw_map)
		except ValueError:
			tiers = None
		if not isinstance(tiers, dict) or not tiers or not all(
			key.isascii() and key.isdigit() and key == str(int(key)) and int(key) > 0 and _text(code)
			and isinstance(code, str) for key, code in tiers.items()
		):
			frappe.throw(_("Correct the room type's occupancy tariff configuration before adding a rate."))

	field = "custom_tariff_occupancy_map"
	if tier == "bed":
		field = "custom_tariff_extra_bed_item"
	elif not tiers and tier == "1" and rt.get("custom_tariff_item") and (
		tier != base or rt.get("custom_tariff_single_item")
	):
		field = "custom_tariff_single_item"
	elif not tiers and tier == base:
		field = "custom_tariff_item"
	if not meta.has_field(field):
		frappe.throw(_("This tariff integration does not support the selected occupancy tier."))
	existing = tiers.get(tier) if field == "custom_tariff_occupancy_map" else rt.get(field)
	if existing:
		if item_code and item_code != existing.strip():
			frappe.throw(_("This occupancy tier already has a linked item. Use that item to propose its rate."))
		return existing.strip()

	if field == "custom_tariff_occupancy_map" and not tiers:
		# Preserve the legacy tiers when the first additional adult count moves this type to a schedule.
		for count, code in ((base, rt.get("custom_tariff_item")), ("1", rt.get("custom_tariff_single_item"))):
			if code:
				tiers[count] = code
	if item_code:
		doc = frappe.get_doc("Item", item_code)
		doc.check_permission("read")
		if doc.disabled or not doc.is_sales_item or doc.is_stock_item:
			frappe.throw(_("Choose an enabled non-stock sales item for the room type."))
	else:
		hash_part = hashlib.sha256(rt.name.encode()).hexdigest()[:20]
		suffix = "BED" if tier == "bed" else f"{tier}A"
		code = f"TAR-{hash_part}-{suffix}"
		label = _("Extra bed") if tier == "bed" else _("{0} adult occupancy").format(tier)
		doc = frappe.get_doc({
			"doctype": "Item", "item_code": code, "item_name": f"{rt.room_type_name or rt.name} · {label}",
			"item_group": frappe.defaults.get_global_default("item_group") or "Services",
			"stock_uom": frappe.defaults.get_global_default("stock_uom") or "Nos",
			"is_stock_item": 0, "is_sales_item": 1, "is_purchase_item": 0,
		}).insert()
	if field == "custom_tariff_occupancy_map":
		tiers[tier] = doc.name
		rt.set(field, json.dumps(tiers, sort_keys=True))
	else:
		rt.set(field, doc.name)
	rt.save()
	return doc.name


def _enqueue_approved_prices(names):
	if frappe.db.exists("DocType", "Item Price"):
		from kamra.channel_ari import enqueue_tariff_change
		for name in names:
			if frappe.db.exists("Item Price", name):
				enqueue_tariff_change(frappe.get_doc("Item Price", name))


@frappe.whitelist(methods=["POST"])
@require_roles("Hotel Admin")
def approve_contract_tariff(item_price: str):
	"""Release an eligible draft through the integration's approval workflow."""
	from kamra.erp_bridge import bridge_call

	# Item prices are external IDs. The bridge resolves their property from the
	# price list and enforces scope, draft state, and separation of duties.
	result = bridge_call("hospitality_tariffs.approve_tariff", item_price=item_price)
	if result is not None:
		_enqueue_approved_prices([item_price])
	return result


@frappe.whitelist(methods=["POST"])
@require_roles("Hotel Admin")
def discard_contract_tariff(item_price: str, reason: str | None = None):
	"""Discard a draft through the same property-scoped integration."""
	from kamra.erp_bridge import bridge_call

	return bridge_call("hospitality_tariffs.discard_tariff", item_price=item_price, **({"reason": reason} if reason else {}))


# ── Mass maintenance (create with reference, bulk decisions, CSV) ──────────

def _text(value) -> str:
	return "" if value is None else str(value).strip()


def _json_list(value, message: str) -> list:
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			frappe.throw(message)
	if not isinstance(value, list):
		frappe.throw(message)
	return value


def _rate(value) -> float | None:
	if isinstance(value, bool) or not isinstance(value, (int, float, str)):
		return None
	try:
		rate = float(str(value).strip())
	except ValueError:
		return None
	return rate if math.isfinite(rate) else None


def _iso_date(value) -> str | None:
	if isinstance(value, date):
		return value.isoformat()
	text = _text(value)
	if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
		return None
	try:
		return date.fromisoformat(text).isoformat()
	except ValueError:
		return None


def _tariff_entry(row: dict) -> tuple[dict | None, str | None]:
	"""One proposed version from a row, or the reason it cannot be one."""
	price_list, item_code = _text(row.get("price_list")), _text(row.get("item_code"))
	if not price_list or not item_code:
		return None, _("Choose the price list and the item.")
	rate = _rate(row.get("price_list_rate"))
	if rate is None:
		return None, _("The rate must be a number.")
	if rate < 0:
		return None, _("The rate cannot be negative.")
	valid_from = _iso_date(row.get("valid_from"))
	if not valid_from:
		return None, _("Effective from must be a date (YYYY-MM-DD).")
	valid_upto = None
	if _text(row.get("valid_upto")):
		valid_upto = _iso_date(row.get("valid_upto"))
		if not valid_upto:
			return None, _("Valid until must be a date (YYYY-MM-DD).")
		if valid_upto < valid_from:
			return None, _("Valid until cannot be before Effective from.")
	entry = {"price_list": price_list, "item_code": item_code, "price_list_rate": rate,
	         "valid_from": valid_from, "valid_upto": valid_upto}
	if _text(row.get("customer")):
		entry["customer"] = _text(row.get("customer"))
	return entry, None


def _entry_key(entry: dict) -> tuple:
	return entry["price_list"], entry["item_code"], entry.get("customer", ""), entry["valid_from"]


def _checked_entries(entries) -> list[dict]:
	rows = _json_list(entries, _("Send the tariff rows as a list."))
	if not rows:
		frappe.throw(_("Send at least one tariff row."))
	if len(rows) > TARIFF_BATCH_LIMIT:
		frappe.throw(_("Send at most {0} tariff rows at a time.").format(TARIFF_BATCH_LIMIT))
	checked, seen = [], set()
	for number, row in enumerate(rows, start=1):
		if not isinstance(row, dict):
			frappe.throw(_("Send the tariff rows as a list."))
		entry, problem = _tariff_entry(row)
		if not problem and _entry_key(entry) in seen:
			problem = _("The same item, customer and date appear twice.")
		if problem:
			frappe.throw(_("Row {0}: {1}").format(number, problem))
		seen.add(_entry_key(entry))
		checked.append(entry)
	return checked


def _propose(property: str, entries: list[dict]):
	"""Draft versions through the bulk integration, or one proposal each."""
	from kamra.erp_bridge import call_bridge_script, get_bridge_script

	bulk = get_bridge_script("hospitality_tariffs.bulk_propose_tariffs")
	if bulk:
		return call_bridge_script(bulk, {"property": property, "entries": entries})
	single = get_bridge_script("hospitality_tariffs.propose_tariff")
	if not single:
		return None
	# the single proposal has no customer: it would publish a corporate rate
	if any(entry.get("customer") for entry in entries):
		frappe.throw(_("Customer-specific tariffs need the bulk tariff integration."))
	proposed = [call_bridge_script(single, {"property": property, **entry}) for entry in entries]
	return {"proposed": proposed, "count": len(proposed)}


@frappe.whitelist(methods=["POST"])
@require_roles(*TARIFF_EDITORS)
def bulk_propose_contract_tariffs(property: str, entries):
	"""Propose many new versions at once (create with reference): each entry
	is {price_list, item_code, price_list_rate, valid_from, valid_upto,
	customer}. Every entry is checked before anything is sent; the versions
	are Drafts for approval, and the integration owns effective-date rules."""
	assert_property_access(property)
	return _propose(property, _checked_entries(entries))


def _item_price_names(item_prices) -> list[str]:
	names = _json_list(item_prices, _("Send the tariff versions as a list."))
	if not all(isinstance(name, str) for name in names):
		frappe.throw(_("Send the tariff versions as a list."))
	names = list(dict.fromkeys(name.strip() for name in names if name.strip()))
	if not names:
		frappe.throw(_("Choose at least one tariff version."))
	if len(names) > TARIFF_BATCH_LIMIT:
		frappe.throw(_("Choose at most {0} tariff versions at a time.").format(TARIFF_BATCH_LIMIT))
	return names


def _decide(action: str, names: list[str], reason: str | None = None):
	"""Approve or discard drafts through the bulk integration, or one at a
	time through the single endpoint and its separation-of-duties check; a
	refusal stops the batch and the request rolls back."""
	from kamra.erp_bridge import call_bridge_script, get_bridge_script

	details = {"reason": reason} if reason else {}
	bulk = get_bridge_script(f"hospitality_tariffs.bulk_{action}_tariffs")
	if bulk:
		return call_bridge_script(bulk, {"item_prices": names, **details})
	single = get_bridge_script(f"hospitality_tariffs.{action}_tariff")
	if not single:
		return None
	done = [call_bridge_script(single, {"item_price": name, **details}) for name in names]
	return {{"approve": "approved", "discard": "discarded"}[action]: done, "count": len(done)}


@frappe.whitelist(methods=["POST"])
@require_roles("Hotel Admin")
def bulk_approve_contract_tariffs(item_prices):
	"""Release many drafts at once; the maker of a draft still cannot release it."""
	names = _item_price_names(item_prices)
	result = _decide("approve", names)
	if result is not None:
		skipped = {row.get("name") for row in result.get("results", []) if row.get("status") == "skipped"}
		_enqueue_approved_prices([name for name in names if name not in skipped])
	return result


@frappe.whitelist(methods=["POST"])
@require_roles("Hotel Admin")
def bulk_discard_contract_tariffs(item_prices, reason: str | None = None):
	"""Discard many drafts at once through the same property-scoped integration."""
	return _decide("discard", _item_price_names(item_prices), reason)


def _export_versions(item: dict, today: str) -> list[tuple[dict, str]]:
	"""The versions a spreadsheet carries: current, scheduled and in review."""
	versions = [(item["active"], "Approved")] if item.get("active") else []
	scheduled = [version for version in item.get("history") or []
	             if version.get("workflow_state") == "Approved" and (version.get("valid_from") or "") > today]
	versions += [(version, "Scheduled") for version in sorted(scheduled, key=lambda v: v.get("valid_from") or "")]
	pending = sorted(item.get("pending") or [], key=lambda v: v.get("valid_from") or "")
	return versions + [(version, "In Review") for version in pending]


@frappe.whitelist()
@require_roles(*RATE_ROLES)
def export_contract_tariffs_csv(property: str, price_list: str | None = None, customer: str | None = None,
                                as_of_date: str | None = None):
	"""The tariff directory as spreadsheet rows for offline editing: one row
	per current, scheduled or in-review version (or an empty-rate row for an
	item with none), for what the screen shows - one price list, one
	customer's own tariffs (or PUBLIC_TARIFFS for the public ones only) and
	the effective date being viewed.

	The cells are raw values (numbers, ISO dates, text as stored). The
	screen writes the file with frontend/src/lib/csv.ts, which quotes each
	cell and neutralises a cell that would run as a spreadsheet formula;
	the import's _unguard reads that guard back. Nothing here is a file."""
	assert_property_access(property)
	customer = _text(customer)
	directory = _tariff_directory(property, price_list=price_list,
	                              customer=customer if customer != PUBLIC_TARIFFS else None,
	                              as_of_date=as_of_date)
	if directory is None:
		return {"supported": False, "business_date": None, "columns": TARIFF_CSV_COLUMNS, "rows": []}
	currencies = {pl.get("name"): pl.get("currency") for pl in directory.get("price_lists") or []}
	today = directory.get("as_of_date") or as_of_date or directory.get("business_date") or ""
	rows = []
	for item in directory.get("items") or []:
		if price_list and item.get("price_list") != price_list:
			continue
		if customer and (item.get("customer") or "") != ("" if customer == PUBLIC_TARIFFS else customer):
			continue
		head = [item.get("price_list"), item.get("currency") or currencies.get(item.get("price_list")) or "",
		        item.get("customer") or "", item.get("item_code"), item.get("room_type") or "",
		        item.get("occupancy") or ""]
		versions = _export_versions(item, today)
		if not versions:
			rows.append([*head, "", "", "", "No active tariff"])
		for version, status in versions:
			rows.append([*head, float(version.get("rate") or 0), version.get("valid_from") or "",
			             version.get("valid_upto") or "", status])
	return {"supported": True, "business_date": directory.get("business_date"),
	        "columns": TARIFF_CSV_COLUMNS, "rows": rows}


def _column_key(header) -> str:
	return re.sub(r"\s+", "_", _text(header).lstrip("\ufeff").lower())


def _import_records(rows) -> list[dict]:
	"""Import rows: the exported CSV text, or a list of row objects. Text
	over TARIFF_IMPORT_BYTES is refused before it is parsed, and a CSV is
	read only to one row past TARIFF_BATCH_LIMIT."""
	if isinstance(rows, str):
		if len(rows.encode("utf-8")) > TARIFF_IMPORT_BYTES:
			frappe.throw(_("The file is larger than 1 MB. Split it and import each part."))
		text = rows.lstrip("\ufeff")
		rows = (_json_list(text, _("Send the tariff rows as a list.")) if text.lstrip().startswith("[")
		        else list(itertools.islice(csv.DictReader(io.StringIO(text)), TARIFF_BATCH_LIMIT + 1)))
	if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
		frappe.throw(_("Send the tariff rows as a list."))
	if len(rows) > TARIFF_BATCH_LIMIT:
		frappe.throw(_("Send at most {0} tariff rows at a time.").format(TARIFF_BATCH_LIMIT))
	return [{_column_key(key): _unguard(value) for key, value in row.items() if key} for row in rows]


# frontend/src/lib/csv.ts csvCell: a text cell whose first non-blank
# character would start a formula (ASCII or fullwidth), and that is not a
# plain number, is written with a leading quote.
_FORMULA_START = re.compile(r"[=+\-@\t\r\n\uff1d\uff0b\uff0d\uff20]")
_PLAIN_NUMBER = re.compile(r"-?\d+(\.\d+)?")


def _unguard(value):
	"""A cell the export guarded against running as a formula reads back as typed: the exact inverse of csvCell."""
	if not isinstance(value, str) or not value.startswith("'"):
		return value
	typed = value[1:]
	if _FORMULA_START.match(typed.lstrip()) and not _PLAIN_NUMBER.fullmatch(typed.strip()):
		return typed
	return value


def _unchanged(item: dict | None, entry: dict) -> bool:
	versions = [item.get("active")] + (item.get("pending") or []) + (item.get("history") or []) if item else []
	return any(version and abs(float(version.get("rate") or 0) - entry["price_list_rate"]) < 1e-9
	           and version.get("valid_from") == entry["valid_from"]
	           and (version.get("valid_upto") or None) == entry["valid_upto"] for version in versions)


@frappe.whitelist(methods=["POST"])
@require_roles(*TARIFF_EDITORS)
def import_contract_tariffs_rows(property: str, rows):
	"""Spreadsheet import: every new row becomes a Draft version for
	approval - nothing is released here. A row needs a price list of this
	property, its currency, an item on that list, a rate of zero or more and
	an Effective from date. Rows without a rate are skipped and rows that
	repeat an existing version are left alone; with any bad row nothing is
	created and every problem is listed by spreadsheet row."""
	assert_property_access(property)
	records = _import_records(rows)
	directory = _tariff_directory(property)
	if directory is None:
		return None
	currencies = {pl.get("name"): pl.get("currency") or "" for pl in directory.get("price_lists") or []}
	items = {(item.get("price_list"), item.get("item_code"), item.get("customer") or ""): item
	         for item in directory.get("items") or []}
	codes = {(price_list, code) for price_list, code, _customer in items}
	entries, errors, seen = [], [], set()
	unchanged = skipped = 0
	for number, record in enumerate(records, start=2):  # row 1 is the header
		if not _text(record.get("rate")):
			skipped += 1
			continue
		entry, problem = _tariff_entry({**record, "price_list_rate": record.get("rate")})
		if not problem:
			price_list, currency = entry["price_list"], _text(record.get("currency")).upper()
			if price_list not in currencies:
				problem = _("{0} is not a price list of this property.").format(price_list)
			elif not currency:
				problem = _("Give the price list currency.")
			elif currency != currencies[price_list].upper():
				problem = _("{0} does not match the {1} price list currency ({2}).").format(
					currency, price_list, currencies[price_list])
			elif (price_list, entry["item_code"]) not in codes:
				problem = _("{0} is not on the {1} price list.").format(entry["item_code"], price_list)
			elif _entry_key(entry) in seen:
				problem = _("The same item, customer and date appear twice.")
		if problem:
			errors.append({"row": number, "message": problem})
			continue
		seen.add(_entry_key(entry))
		if _unchanged(items.get((entry["price_list"], entry["item_code"], entry.get("customer", ""))), entry):
			unchanged += 1
		else:
			entries.append(entry)
	result = {"created": 0, "unchanged": unchanged, "skipped": skipped, "errors": errors}
	if errors or not entries:
		return result
	proposed = _propose(property, entries)
	if proposed is None:
		return None
	return {**result, "created": len(entries), "proposed": proposed}


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


def _plan_policy(plan) -> dict:
	"""A rate plan's name, currency, exchange-rate lock, rounding and base plan, for a screen."""
	from kamra.pricing import DEFAULT_FX_LOCK

	return {"name": plan.get("name"), "label": plan.get("rate_plan_name"), "code": plan.get("code"),
	        "currency": plan.get("currency") or None,
	        "fx_lock_policy": plan.get("fx_lock_policy") or DEFAULT_FX_LOCK,
	        "rounding_step": float(plan.get("rounding_step") or 0),
	        "rounding_mode": plan.get("rounding_mode") or "Nearest",
	        "base_rate_plan": plan.get("base_rate_plan") or None}


def _plan_night(property: str, rt, day, adults: int, children: int, rate_plan: str, company: str | None,
                occupancy: float, small_ok: bool, inclusive: bool, home: str) -> dict:
	"""One cell under a rate plan: the quote a one-night booking gets. A
	contract cell is the agreed rate the booking locks; a room type the plan
	has no tariff for is priced from the card, on the basis the house quotes."""
	from kamra.pricing import ContractRateRefused, quote_with_contract, round_rate_amount

	try:
		q = quote_with_contract(property, rt.name, day, add_days(day, 1), adults, children, rate_plan=rate_plan,
		                        company=company, check_voucher_limits=False)
	except ContractRateRefused as exc:
		return {"date": str(day), "available": False, "refusal": str(exc), "rate": None, "contract": None,
		        "occupancy": occupancy}
	if q.get("tariff"):
		agreed = q["agreed_rate"]
		return {"date": str(day), "available": True, "rate": agreed, "bar": agreed, "after_season": agreed,
		        "season": None, "demand": None, "package": None, "occupancy": occupancy,
		        "contract": q["tariff"], "source": "contract"}
	night = explain_night(property, rt, day, adults, children, occupancy=occupancy, small_ok=small_ok)
	acc = q["line_items"][0]
	gross = acc["amount"] + (acc["tax_amount"] if inclusive else 0)
	return {**night, "card_rate": night["rate"], "rate": float(round_rate_amount(gross, 0, currency=home)),
	        "contract": None, "source": "card", "available": True}


def _can_edit_rate() -> bool:
	return bool({"Revenue Manager", "Hotel Admin", "System Manager", "Administrator"} & set(frappe.get_roles()))


@frappe.whitelist()
@require_roles(*RATE_ROLES)
def rate_calendar(property: str, from_date: str | None = None, days: int = 14,
                  adults: int = 2, children: int = 0, rate_plan: str | None = None,
                  company: str | None = None):
	"""Every room type's rate for each of the next `days` dates, with what
	moved it. A room type's own days are read with the same functions the
	quote uses; nothing here sets a price.

	With a rate plan, or a customer's negotiated plan (OPERA Rate Query, SAP
	pricing analysis), each cell is that plan's real quote for the night."""
	assert_property_access(property)
	from kamra.pricing import (
		forecast_occupancy,
		negotiated_rate_plan,
		property_currency,
		rates_include_tax,
		small_group_allows_stay,
	)
	from kamra.settings import get_setting

	rate_plan_required = bool(get_setting(property, "rate_plan_required"))
	all_plans = frappe.get_all(
		"Rate Plan", filters={"property": property},
		fields=_rate_plan_fields(("is_default", "disabled", "rate_category")),
		order_by="rate_plan_name asc, name asc",
	)
	plans_by_name = {p["name"]: p for p in all_plans}
	active = [p for p in all_plans if not int(p.get("disabled") or 0)]
	all_tariff = all_active_plans_use_tariffs(property, active)
	effective_required = rate_plan_required or all_tariff
	has_tariff_plans = any(p.get("custom_tariff_price_list") for p in active)
	default_name = default_plan(active, allow_sole=effective_required)
	default_is_tariff = bool(default_name and _plan_uses_tariff(default_name, plans_by_name))
	if company:
		frappe.has_permission("Corporate Account", "read", doc=company, throw=True)
	company_plan = negotiated_rate_plan(property, company)
	if rate_plan == "":
		plan = default_name if effective_required else None
		if effective_required and not plan:
			frappe.throw(
				_("A rate plan is required for this property. Set up a default rate plan in Rate Plans, "
				  "or choose a rate plan.")
			)
	elif rate_plan is not None:
		plan = rate_plan
	else:
		plan = company_plan or (
			default_name if (default_is_tariff or effective_required) else None
		)
	policy = next((p for p in active if p.name == plan), None) if plan else None
	if plan and not policy:
		frappe.throw(_("{0} is not an active rate plan of this property.").format(plan))
	no_plan_refusal = (
		_("Set a default rate plan in Rate Plans.")
		if active
		else _("No active rate plans are configured. Set up a default rate plan in Rate Plans.")
	)
	inclusive = bool(rates_include_tax(property))
	home = property_currency(property)
	start = getdate(from_date) if from_date else getdate(frappe.utils.nowdate())
	days = max(1, min(int(days or 14), 31))
	dates = [add_days(start, i) for i in range(days)]
	occ = {str(d): forecast_occupancy(property, d) for d in dates}
	counts = _room_counts(property)
	rt_list = frappe.get_all("Room Type", filters={"property": property, "disabled": 0},
	                         pluck="name", order_by="base_price asc")
	from kamra.restrictions import calendar_restrictions_map
	restrictions_map = calendar_restrictions_map(property, start, days, rt_list, rate_plan=plan)
	rows = []
	for rt in rt_list:
		doc = frappe.get_doc("Room Type", rt)
		nights = []
		for d in dates:
			small_ok = small_group_allows_stay(doc, d, add_days(d, 1))
			if policy:
				cell = _plan_night(property, doc, d, int(adults), int(children), plan, company or None,
				                   occ[str(d)], small_ok, inclusive, home)
			elif effective_required:
				cell = {
					"date": str(d), "available": False, "refusal": no_plan_refusal,
					"rate": None, "contract": None, "occupancy": occ[str(d)],
				}
			else:
				cell = explain_night(property, doc, d, int(adults), int(children),
				                     occupancy=occ[str(d)], small_ok=small_ok)
			restr = restrictions_map.get((doc.name, str(d)))
			if restr:
				cell = {
					**cell,
					"is_restricted": True,
					"stop_sell": restr["stop_sell"],
					"min_los": restr["min_los"],
					"max_los": restr["max_los"],
					"is_stay_through": restr["is_stay_through"],
					"cta": restr["cta"],
					"ctd": restr["ctd"],
					"restriction_scope": restr["source_scope"],
					"restriction_reason": restr["reason"],
				}
			nights.append(cell)
		count = counts.get(doc.name, 0)
		rows.append({"room_type": doc.name, "code": doc.room_type_code,
		             "name": doc.room_type_name,
		             "base_price": 0.0 if effective_required else float(doc.base_price or 0),
		             "room_count": count, "sellable": count > 0, "nights": nights})

	def _plan_entry(p):
		entry = _plan_policy(p)
		entry.update(is_default=bool(p.get("is_default")), rate_category=p.get("rate_category"),
		             currency=picker_currency(p))
		if has_tariff_plans:
			entry["is_tariff"] = _plan_uses_tariff(p["name"], plans_by_name)
		return entry

	return {"from": str(start), "days": days, "tax_inclusive": inclusive,
	        "adults": int(adults), "can_edit": _can_edit_rate(), "room_types": rows,
	        "rate_plans": [_plan_entry(p) for p in active], "default_plan": default_name,
	        "rate_plan_required": rate_plan_required,
	        "all_active_plans_tariff": all_tariff,
	        "property_currency": home, "rate_plan": _plan_entry(policy) if policy else None,
	        "company": company or None, "company_plan": company_plan}


@frappe.whitelist()
@require_roles(*RATE_ROLES)
def rate_options(property: str, room_type: str, check_in_date: str, check_out_date: str,
                 adults: int = 2, children: int = 0, meal_plan: str | None = None,
                 voucher_code: str | None = None, rate_plan: str | None = None,
                 extra_beds: int = 0, company: str | None = None, travel_agent: str | None = None):
	"""For the booking dialog: this stay night by night (BAR, season,
	demand), the whole stay under each active rate plan against the plain
	rate, and what the voucher would do. Each total is a real quote.

	With the booking's `company` each plan is priced at that company's own
	tariff (OPERA Rate Query with a Company, SAP PR02 A305), and
	`company_plan` names its negotiated plan, which prices the stay when no
	plan is picked - as get_quote does. Each plan says how it fixes its
	exchange rate and which plan it derives from; a contract plan's nights
	show each night's own rate."""
	assert_property_access(property)
	from kamra.pricing import (
		DEFAULT_FX_LOCK,
		ContractRateRefused,
		negotiated_rate_plan,
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

	company = company or None
	company_plan = negotiated_rate_plan(property, company)
	negotiated = company_plan
	if travel_agent and not negotiated:
		from kamra.pricing import travel_agent_rate_plan
		negotiated = travel_agent_rate_plan(property, travel_agent)

	def total(plan=None, voucher=None):
		return quote_with_contract(property, room_type, check_in_date, check_out_date, int(adults),
		                           int(children), meal_plan or None, plan, voucher,
		                           extra_beds=extra_beds, check_voucher_limits=False,
		                           # the plain rate is the house's own; a plan is the company's price
		                           **({"company": company} if plan and company else {}))

	plans = []
	active = frappe.get_all("Rate Plan", filters={"property": property, "disabled": 0},
	                        fields=_rate_plan_fields(("modifier_type", "modifier_value", "is_default", "discount_eligible", "rate_category")),
	                        order_by="rate_plan_name asc, name asc")
	labels = {rp.name: rp.rate_plan_name for rp in active}
	all_tariff = all_active_plans_use_tariffs(property, active)
	raw_required = bool(get_setting(property, "rate_plan_required"))
	required = raw_required or all_tariff
	pricing_plan = rate_plan or negotiated or (default_plan(active, allow_sole=True) if required else None)
	plain = {"room_total": 0, "amount_after_tax": 0, "discount": 0} if required else total()
	night_rows = [] if all_tariff else [
		explain_night(property, rt, add_days(check_in_date, i), int(adults), int(children), small_ok=small_ok)
		for i in range(max(nights, 1))
	]
	active.sort(key=lambda rp: (rp.name != negotiated, not rp.is_default, rp.rate_plan_name.casefold()))
	from kamra.restrictions import evaluate_stay_restrictions
	for rp in active:
		base = rp.get("base_rate_plan")
		row = {
			"name": rp.name, "label": rp.rate_plan_name, "code": rp.code,
			"modifier": f"{rp.modifier_type} {float(rp.modifier_value or 0):g}",
			"is_default": bool(rp.is_default),
			"is_negotiated": bool(negotiated and rp.name == negotiated),
			"rate_category": rp.get("rate_category") or None,
			# OPERA Rate Code "Discount": may the desk discount this plan
			"discount_eligible": 1 if rp.discount_eligible is None else int(rp.discount_eligible),
			# OPERA Exchange Type and Base Rate, shown before booking
			"fx_lock_policy": rp.get("fx_lock_policy") or DEFAULT_FX_LOCK,
			"derived_from": (labels.get(base) or frappe.db.get_value("Rate Plan", base, "rate_plan_name") or base)
			if base else None,
		}
		if nights > 0:
			ev = evaluate_stay_restrictions(property, room_type, check_in_date, check_out_date, rate_plan=rp.name)
			if not ev["allowed"]:
				row["restricted"] = True
				row["restriction_reason"] = ev["summary"]
				row["restrictions"] = ev["violations"]
		try:
			q = total(rp.name)
		except ContractRateRefused as exc:
			plans.append({**row, "available": False, "refusal": str(exc), "tariff": None})
			continue
		plans.append({**row, "available": True, "tariff": q.get("tariff"),
		              "agreed_rate": q.get("agreed_rate"),
		              "room_total": q["room_total"], "amount_after_tax": q["amount_after_tax"],
		              "vs_plain": 0 if required else round(q["room_total"] - plain["room_total"], 2)})
		if required and rp.name == pricing_plan:
			plain = q
		if rp.name == pricing_plan and q.get("tariff"):
			# each night's own contract rate (a stay over a tariff change), else the stay's
			own = {night["date"]: night["agreed_rate"] for night in q["tariff"].get("nights") or []}
			night_rows = []
			for r in q["nightly"]:
				rate = own.get(str(r["date"]), q["agreed_rate"])
				night_rows.append({"date": r["date"], "rate": rate, "bar": rate, "after_season": rate, "season": None,
				                   "demand": None, "occupancy": r.get("occupancy", 0)})
	voucher = None
	if (voucher_code or "").strip():
		code = voucher_code.strip().upper()
		try:
			v = validate_voucher(property, code, max(nights, 1))
			t = total(pricing_plan if any(p.name == pricing_plan for p in active) else None, code)
			voucher = {"code": code, "valid": True, "discount": t["discount"],
			           "kind": v.discount_type, "value": float(v.value or 0),
			           **{k: t[k] for k in ("voucher_currency", "voucher_tx_amount", "voucher_fx_rate") if k in t}}
		except frappe.ValidationError as e:
			voucher = {"code": code, "valid": False, "reason": str(e)}
	from kamra.api import discount_options
	default = default_plan(active, allow_sole=required)
	return {"nights": night_rows, "plans": plans, "voucher": voucher,
	        "plain": {k: plain[k] for k in ("room_total", "amount_after_tax", "discount")},
	        "company_plan": company_plan, "negotiated_plan": negotiated,
	        "default_plan": default, "default_rate_plan": default,
	        "rate_plan_required": raw_required,
	        "all_active_plans_tariff": all_tariff,
	        # the basis of every nightly figure above (and of a typed night price)
	        "tax_inclusive": bool(rates_include_tax(property)),
	        # the inline discount: who may give one, the desk's limit, the reasons
	        **discount_options(property)}
