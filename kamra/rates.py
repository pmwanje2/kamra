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
import io
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
# rows in one mass change, import or bulk decision
TARIFF_BATCH_LIMIT = 500
TARIFF_CSV_COLUMNS = ["Price List", "Currency", "Customer", "Item Code", "Room Type", "Occupancy Tier", "Rate",
                      "Valid From", "Valid Upto", "Status"]
RATE_PLAN_POLICY_FIELDS = ["name", "rate_plan_name", "code", "currency", "fx_lock_policy", "base_rate_plan",
                           "adjust_extra_person", "rounding_step", "rounding_mode"]


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
		return result
	return {"supported": False, "property": property, "price_lists": [], "items": [],
	        "can_propose": False, "can_approve": False}


def _rate_plan_policies(property: str, directory: dict) -> dict:
	"""The policies of the plans the directory's price lists name, and of their base plans."""
	named = [plan for price_list in directory.get("price_lists") or []
	         for plan in price_list.get("rate_plans") or []]
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
		"currency": plans[name].get("currency") or None,
		"fx_lock_policy": plans[name].get("fx_lock_policy") or "Fixed at Reservation",
		"base_rate_plan": plans[name].get("base_rate_plan") or None,
		"adjust_extra_person": bool(int(plans[name].get("adjust_extra_person") or 0)),
		"rounding_step": float(plans[name].get("rounding_step") or 0),
		"rounding_mode": plans[name].get("rounding_mode") or "Nearest",
	} for name in wanted}


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


def _decide(action: str, names: list[str]):
	"""Approve or discard drafts through the bulk integration, or one at a
	time through the single endpoint and its separation-of-duties check; a
	refusal stops the batch and the request rolls back."""
	from kamra.erp_bridge import call_bridge_script, get_bridge_script

	bulk = get_bridge_script(f"hospitality_tariffs.bulk_{action}_tariffs")
	if bulk:
		return call_bridge_script(bulk, {"item_prices": names})
	single = get_bridge_script(f"hospitality_tariffs.{action}_tariff")
	if not single:
		return None
	done = [call_bridge_script(single, {"item_price": name}) for name in names]
	return {{"approve": "approved", "discard": "discarded"}[action]: done, "count": len(done)}


@frappe.whitelist(methods=["POST"])
@require_roles("Hotel Admin")
def bulk_approve_contract_tariffs(item_prices):
	"""Release many drafts at once; the maker of a draft still cannot release it."""
	return _decide("approve", _item_price_names(item_prices))


@frappe.whitelist(methods=["POST"])
@require_roles("Hotel Admin")
def bulk_discard_contract_tariffs(item_prices):
	"""Discard many drafts at once through the same property-scoped integration."""
	return _decide("discard", _item_price_names(item_prices))


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
def export_contract_tariffs_csv(property: str, price_list: str | None = None):
	"""The tariff directory as spreadsheet rows for offline editing: one row
	per current, scheduled or in-review version (or an empty-rate row for an
	item with none). The screen writes the file; import reads it back."""
	assert_property_access(property)
	directory = _tariff_directory(property, price_list=price_list)
	if directory is None:
		return {"supported": False, "business_date": None, "columns": TARIFF_CSV_COLUMNS, "rows": []}
	currencies = {pl.get("name"): pl.get("currency") for pl in directory.get("price_lists") or []}
	today = directory.get("business_date") or ""
	rows = []
	for item in directory.get("items") or []:
		if price_list and item.get("price_list") != price_list:
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
	"""Import rows: the exported CSV text, or a list of row objects."""
	if isinstance(rows, str):
		text = rows.lstrip("\ufeff")
		rows = (_json_list(text, _("Send the tariff rows as a list.")) if text.lstrip().startswith("[")
		        else list(csv.DictReader(io.StringIO(text))))
	if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
		frappe.throw(_("Send the tariff rows as a list."))
	if len(rows) > TARIFF_BATCH_LIMIT:
		frappe.throw(_("Send at most {0} tariff rows at a time.").format(TARIFF_BATCH_LIMIT))
	return [{_column_key(key): value for key, value in row.items() if key} for row in rows]


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
