"""Whitelisted API for the Kamra front-desk UI.

Every endpoint here is also, by design, an agent tool: the same governed
surface serves the React console today and the MCP layer next.
"""

import json
import math
from contextlib import nullcontext

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_datetime, getdate, now_datetime, nowdate

from kamra.branding import product_name, public_url
from kamra.authz import act_as, assert_property_access, require_it_admin, require_roles
from kamra.install import AGENT_EMAIL

# The apps a property actually runs. The launcher and the sidebar read
# this so a serviced-apartment operator never sees an empty restaurant.
ALL_MODULES = ("front-desk", "housekeeping", "operations", "fnb", "events",
               "revenue", "finance", "booking-engine", "admin")


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Revenue Manager", "Housekeeping",
               "Housekeeping Supervisor", "Kamra Agent", "Night Auditor", "F&B Manager")
def enabled_modules(property: str) -> list:
	"""Which parts of Kamra this property runs. Empty setting = all of
	them, so an existing property keeps working untouched."""
	raw = frappe.db.get_value("Property", property, "enabled_modules")
	picked = [m.strip() for m in (raw or "").split(",") if m.strip()]
	return picked or list(ALL_MODULES)


@frappe.whitelist(methods=["POST"])
@require_roles("Hotel Admin")
def set_enabled_modules(property: str, modules) -> dict:
	"""Turn parts of the product on or off for one property."""
	if isinstance(modules, str):
		modules = [m.strip() for m in modules.split(",")]
	unknown = [m for m in modules if m and m not in ALL_MODULES]
	if unknown:
		frappe.throw(_("Unknown module(s): {0}").format(", ".join(unknown)))
	# Front desk is the product. Admin holds Settings, Rooms and Room
	# Types - switch it off and you lose the only screen that could switch
	# it back on, and the ability to add a room at all. Neither is optional.
	picked = sorted({m for m in modules if m} | {"front-desk", "admin"})
	frappe.db.set_value("Property", property, "enabled_modules",
	                    ",".join(picked))
	return {"ok": True, "modules": picked}


@frappe.whitelist(allow_guest=True, methods=["GET"])
def csrf_token():
	"""The session's current CSRF token. An open tab asks for it when its
	page-load token went stale because someone signed in or out in another
	tab (Frappe rotates the token on login). None for a guest. A GET, like
	the boot page that already carries the token, and readable only by the
	same origin."""
	if frappe.session.user == "Guest":
		return None
	return frappe.sessions.get_csrf_token()


@frappe.whitelist(allow_guest=True)
def whoami():
	"""Current user + roles - drives which modules the UI shows.

	allow_guest so the SPA's initial "am I logged in?" probe returns
	{user: "Guest"} cleanly instead of a 403 in the console.
	"""
	user = frappe.session.user
	return {
		"user": user,
		"full_name": frappe.db.get_value("User", user, "full_name") or user,
		"roles": frappe.get_roles(user),
	}


@frappe.whitelist()
@require_it_admin
def developer_info():
	"""REST base URL + whether the current user already has an API key.

	Drives the on-site Developers page. The secret itself is never returned
	here - Frappe stores it hashed; it's only shown once, at generation time.
	"""
	user = frappe.session.user
	return {
		"user": user,
		"has_key": bool(frappe.db.get_value("User", user, "api_key")),
		"base_url": frappe.utils.get_url(),
	}


@frappe.whitelist(methods=["POST"])
@require_it_admin
def generate_api_key():
	"""Generate (or rotate) the current user's REST API key + secret.

	Self-service: acts only on the signed-in user, so any authenticated staff
	member can mint a key scoped to their own roles. The secret is returned
	once here and stored hashed thereafter.
	"""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Sign in to generate an API key.")
	doc = frappe.get_doc("User", user)
	api_secret = frappe.generate_hash(length=15)
	if not doc.api_key:
		doc.api_key = frappe.generate_hash(length=15)
	doc.api_secret = api_secret
	doc.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persists the completed operation before returning to an external/public caller; reviewed as intentional
	return {"api_key": doc.api_key, "api_secret": api_secret}


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent", "Hotel Admin", "System Manager")
def upload_room_image():
	"""Uploads an image file for room types / properties to public files."""
	req = getattr(frappe.local, "request", None)
	files = getattr(req, "files", {}) if req else {}
	if "file" not in files:
		frappe.throw("No file uploaded.")
	file = files["file"]
	filename = file.filename
	content = file.stream.read()

	from frappe.utils.file_manager import save_file
	saved = save_file(filename, content, None, None, is_private=0)
	return {"file_url": saved.file_url}


def _assert_within_guardrails(guardrails, room_type, rate: float,
                              start_date, end_date) -> list:
	"""Refuse a rate that breaks the owner's rails on any day in the range,
	and return the rails that were actually applied.

	Rails can be scoped to days of the week, so a property can hold a hard
	weekend floor while still allowing a midweek discount. The most specific
	rail wins for each day: room-type + days, then room-type, then
	property-wide + days, then property-wide.
	"""
	applied: list = []
	if not guardrails:
		return applied
	from frappe.utils import add_days, date_diff

	from kamra.pricing import rail_for_day

	span = min(int(date_diff(end_date, start_date)) + 1, 92)
	checked: set = set()
	for i in range(span):
		day = add_days(start_date, i)
		day_abbr = getdate(day).strftime("%a").lower()
		if day_abbr in checked:
			continue  # the rails repeat weekly; one check per weekday is enough
		checked.add(day_abbr)
		rail = rail_for_day(guardrails, room_type, day_abbr)
		if not rail:
			continue
		if rail.name not in applied:
			applied.append(rail.name)
		scope = f" on {day_abbr.title()}" if rail.days_of_week else ""
		if rate < float(rail.floor_price):
			frappe.throw(
				f"Blocked by guardrail {rail.name}: \u20b9{rate:,.0f} is below "
				f"the floor of \u20b9{float(rail.floor_price):,.0f}{scope}."
			)
		if rate > float(rail.ceiling_price):
			frappe.throw(
				f"Blocked by guardrail {rail.name}: \u20b9{rate:,.0f} is above "
				f"the ceiling of \u20b9{float(rail.ceiling_price):,.0f}{scope}."
			)
	return applied


@frappe.whitelist()
@require_roles("Revenue Manager", "Kamra Agent")
def set_room_rate(property: str, room_type: str, start_date: str,
                  end_date: str, rate: float, reason: str = "",
                  agent: str | None = None, days_of_week: list | str | None = None):
	"""Set the nightly rate for a room type over a date range - bounded by
	the owner's Rate Guardrails (PRD FR-30). This is the Revenue Agent's
	write tool: it can never price outside the rails.

	Guardrails still clamp the rate; the change is recorded in the action log.
	"""
	from kamra.booking_refs import resolve_room_type
	room_type = resolve_room_type(property, room_type)
	rate = float(rate)

	guardrails = frappe.get_all(
		"Rate Guardrail",
		filters={"property": property, "disabled": 0,
		         "room_type": ("in", [room_type, "", None])},
		fields=["name", "room_type", "floor_price", "ceiling_price",
		        "days_of_week"],
	)
	rails_applied = _assert_within_guardrails(guardrails, room_type, rate,
	                                          start_date, end_date)

	# the hurdle is the DYNAMIC floor: when demand tiers are active for any
	# date in range, a manual rate can't undercut the minimum sell rate
	from frappe.utils import add_days, date_diff

	# Validate hurdle rates for each day in range
	from kamra.localization import money_text
	from kamra.pricing import demand_tier
	for i in range(min(int(date_diff(end_date, start_date)) + 1, 92)):
		day = add_days(start_date, i)
		tier = demand_tier(property, room_type, day)
		if tier and tier["min_rate"] and rate < tier["min_rate"]:
			frappe.throw(
				f"Blocked by the hurdle rate: occupancy for {day} is "
				f"{tier['occupancy']:.0f}%, so the minimum sell rate is "
				f"{money_text(property, round(tier['min_rate']))} - "
				f"{money_text(property, round(rate))} undercuts it."
			)

	from frappe.utils import date_diff

	season_name_suffix = f" {days_of_week}" if days_of_week else ""
	days_str = ",".join(days_of_week) if isinstance(days_of_week, list) else days_of_week

	season = frappe.get_doc({
		"doctype": "Season",
		"property": property,
		"season_name": f"Rate set {room_type.split('-')[-1]} {start_date}→{end_date}{season_name_suffix}",
		# scope it to the room type the caller priced - a blank room_type
		# would set this rate for every room type in the house
		"room_type": room_type,
		"start_date": start_date,
		"end_date": end_date,
		"adjustment_type": "Absolute",
		"adjustment_value": rate,
		"priority": 100,
		"days_of_week": days_str
	})
	season.insert()
	from kamra.localization import money_text
	from kamra.savings import log_action
	log_action("set_room_rate", "Season", season.name, property,
	           minutes_saved=6, rationale=reason or (
	               f"Set {room_type.split('-')[-1]} rate "
	               f"{money_text(property, round(rate))} "
	               f"for {start_date}→{end_date}"))
	from kamra.webhooks import emit_rate_overridden
	emit_rate_overridden(property, {
		"scope": "room_type", "season": season.name, "room_type": room_type,
		"rate": rate, "start_date": str(start_date), "end_date": str(end_date),
		"days_of_week": days_str or None, "reason": (reason or "").strip() or None})
	return {
		"season": season.name, "rate": rate,
		"guardrail_checked": rails_applied[0] if rails_applied else None,
		"guardrails_checked": rails_applied,
	}


@frappe.whitelist()
@require_roles()
def owner_briefing(property: str, date: str | None = None):
	"""Deterministic numbers for the owner's morning briefing (PRD FR-70).
	An LLM turns this into prose; it never invents the figures."""
	from frappe.utils import getdate

	date = date or nowdate()
	yesterday = add_days(date, -1)

	rooms_total = frappe.db.count("Room", {"property": property})
	arrivals = frappe.db.count(
		"Reservation",
		{"property": property, "status": "Confirmed", "check_in_date": date},
	)
	departures = frappe.db.count(
		"Reservation",
		{"property": property, "status": "Checked In", "check_out_date": date},
	)

	# yesterday's room revenue + rooms sold from posted folio charges, as
	# every report counts them (kamra.room_statistics); occupancy is the
	# house now: the rooms in house, by the same rule
	from kamra.room_statistics import (
		category_rules,
		counted_occupied,
		in_house_by_category,
		posted_rooms,
		room_figures,
	)
	from kamra.room_statistics import occupancy_pct as pct_of
	rules = category_rules(property)
	house = in_house_by_category(rules, property)
	occupied = counted_occupied(rules, sum(house.values()), house)
	rooms = posted_rooms(property, yesterday, yesterday, rules)
	room_revenue = rooms["room_revenue"]
	figures = room_figures(rules, room_revenue=room_revenue, rooms_sold=rooms["rooms_sold"],
	                       by_category=rooms["by_category"], occupancy_rooms=rooms_total)
	occupancy_pct, adr, revpar = pct_of(occupied, rooms_total), figures["adr"], figures["revpar"]

	open_tickets = frappe.get_all(
		"Service Ticket",
		filters={"property": property,
		         "status": ("in", ["Open", "In Progress"])},
		fields=["name", "subject", "priority", "due_by"],
	)

	minutes = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(minutes_saved), 0) FROM `tabAgent Action Log`
		WHERE property = %(property)s AND DATE(creation) = %(date)s
		""",
		{"property": property, "date": yesterday},
	)[0][0]

	audit = frappe.db.get_value(
		"Night Audit Run", {"property": property, "business_date": yesterday,
		                    "status": ("!=", "Draft")},
		["name", "room_charges_posted", "no_shows_flagged"], as_dict=True,
	)

	next_7 = availability_calendar(property, date, 7)
	pickup = [
		{"date": c["date"],
		 "rooms_available": sum(
			 rt["cells"][i]["available"] for rt in next_7["room_types"]
		 )}
		for i, c in enumerate(next_7["room_types"][0]["cells"])
	] if next_7["room_types"] else []

	return {
		"date": str(getdate(date)),
		"occupancy_pct": occupancy_pct,
		"rooms_occupied": occupied,
		"rooms_total": rooms_total,
		"arrivals_today": arrivals,
		"departures_today": departures,
		"yesterday": {
			"room_revenue": room_revenue, "adr": adr, "revpar": revpar,
			"rooms_by_category": figures["rooms_by_category"],
			"night_audit": audit,
			"agent_minutes_saved": float(minutes or 0),
		},
		"open_tickets": open_tickets,
		"next_7_days_availability": pickup,
	}


def _apply_country_defaults(p: dict):
	"""The country picks the pack; the pack knows the currency and clock.
	Explicit values from the caller always win."""
	country = (p.get("country") or "").strip()
	if not country:
		return
	p["country"] = country
	from kamra.localization import pack_for_country
	pack = pack_for_country(country)
	cur = p.get("currency") or getattr(pack, "DEFAULT_CURRENCY", None)
	if cur and frappe.db.exists("Currency", cur):
		p["currency"] = cur
		frappe.db.set_value("Currency", cur, "enabled", 1)
	elif p.get("currency"):
		frappe.throw(f"Unknown currency {p['currency']}.")
	if not p.get("timezone") and getattr(pack, "DEFAULT_TIMEZONE", None):
		p["timezone"] = pack.DEFAULT_TIMEZONE
	# a flat-VAT pack's rate seeds the property rate (the Property controller
	# fills the same pack defaults for any other insert); on ERP bridge sites
	# folio lines stay at 0% (KMR-TAX-001) because accounting owns tax
	from kamra.erp_bridge import is_bridge_active
	if p.get("gst_rate_low") in (None, "") and getattr(pack, "DEFAULT_VAT", None) and not is_bridge_active():
		p["gst_rate_low"] = float(pack.DEFAULT_VAT)
	# the levy's name follows the country; its % is the operator's to set
	if not p.get("room_levy_label") and getattr(pack, "ROOM_LEVY_LABEL", None):
		p["room_levy_label"] = pack.ROOM_LEVY_LABEL


def default_floor(room_number: str) -> str:
	"""Best-guess floor for a room the wizard creates without one.

	Hotel numbering puts the floor before the last two digits (101 -> 1,
	1205 -> 12). Floor 0 and anything that isn't hotel numbering (1, G01,
	"Room 3", "Villa A") is a small place's ground floor.
	"""
	n = str(room_number or "").strip()
	if n.isdigit() and len(n) >= 3 and int(n[:-2]):
		return str(int(n[:-2]))
	return "Ground"


@frappe.whitelist()
def setup_property(payload):
	"""One-call property onboarding - the wizard's submit button and the
	migration agent's tool. payload = {property:{property_name, city,
	gstin?, phone?, ...}, room_types:[{code,name,base_price,adults?,
	extra_adult_price?,tax_percent?}], rooms:[{room_type_code,
	numbers:["101","102"]}], meal_plans:[{code,label?,price_per_adult}]}"""
	if isinstance(payload, str):
		payload = json.loads(payload)
	frappe.only_for(("System Manager", "Hotel Admin"))

	p = payload["property"]
	from kamra.property_presets import apply_kind_defaults, skip_meal_plans
	apply_kind_defaults(p)
	_apply_country_defaults(p)
	# Inventory topology: rooms (hotel / multi-unit) or whole_property (entire place).
	topology = (payload.get("inventory_topology") or "rooms").strip()
	if topology not in ("rooms", "whole_property"):
		frappe.throw(f"Unknown inventory_topology '{topology}'")

	if frappe.db.exists("Property", p["property_name"]):
		frappe.throw(f"Property '{p['property_name']}' already exists.")

	prop = frappe.get_doc({"doctype": "Property", **p})
	prop.insert()
	# property scope is enforced: a creator pinned to their first property
	# must not lose sight of the one they just added
	from kamra.authz import restricted_properties
	if restricted_properties() is not None:
		frappe.get_doc({
			"doctype": "User Permission", "user": frappe.session.user,
			"allow": "Property", "for_value": prop.name,
			"apply_to_all_doctypes": 1,
		}).insert(ignore_permissions=True)
	try:
		from kamra.property_presets import KIND_STR, normalize_kind
		from kamra.turnover import ensure_default_profile
		ensure_default_profile(
			prop.name,
			str_defaults=normalize_kind(prop.property_kind) == KIND_STR,
		)
	except Exception:
		frappe.log_error(title="Turnover Profile ensure after setup_property")

	# What the owner switched on (guided setup). Empty keeps the default:
	# every module, exactly as before.
	picked_modules = payload.get("modules")
	if picked_modules:
		unknown = [m for m in picked_modules if m not in ALL_MODULES]
		if unknown:
			frappe.throw(_("Unknown module(s): {0}").format(", ".join(unknown)))
		prop.db_set("enabled_modules", ",".join(
			sorted(set(picked_modules) | {"front-desk", "admin"})))
	wants = [w for w in (payload.get("wants") or []) if w in ("channel-manager", "whatsapp")]
	if wants:
		prop.db_set("setup_wants", ",".join(wants))
	pos_made = {}
	if payload.get("pos"):
		from kamra.onboarding import create_pos_setup
		pos_made = create_pos_setup(prop.name, payload["pos"])

	rt_by_code = {}
	weekends_created = 0
	for rt in payload.get("room_types", []):
		# Entire-place listings use Villa so the SIU backfill creates a
		# whole_property sellable unit (ADR-001 / ADR-002).
		category = rt.get("room_category", "Private")
		if topology == "whole_property":
			category = "Villa"
		doc = frappe.get_doc({
			"doctype": "Room Type",
			"property": prop.name,
			"room_type_code": rt["code"],
			"room_type_name": rt["name"],
			"base_price": rt["base_price"],
			"base_occupancy": rt.get("base_occupancy", 2),
			"extra_adult_price": rt.get("extra_adult_price", 0),
			"adults_capacity": rt.get("adults", 2),
			"children_capacity": rt.get("children", 1),
			"room_category": category,
			"air_conditioning": rt.get("air_conditioning", ""),
			"free_child_age": rt.get("free_child_age", 0),
			# unset = the country pack's standard rate, not an Indian slab
			"tax_percent": rt.get("tax_percent"),
		})
		doc.insert()
		rt_by_code[rt["code"]] = doc.name

		# Auto-create a Season for weekend (Fri+Sat) pricing if provided
		weekend_price = rt.get("weekend_price")
		if weekend_price and float(weekend_price) > 0:
			import datetime
			year_end = str(datetime.date.today().replace(year=datetime.date.today().year + 2))
			frappe.get_doc({
				"doctype": "Season",
				"property": prop.name,
				"season_name": f"{rt['name']} Weekend",
				"room_type": doc.name,
				"start_date": str(datetime.date.today()),
				"end_date": year_end,
				"adjustment_type": "Absolute",
				"adjustment_value": float(weekend_price),
				"days_of_week": "fri, sat",
				"priority": 100,
			}).insert()
			weekends_created += 1

	rooms_created = 0
	for spec in payload.get("rooms", []):
		rt_name = rt_by_code.get(spec["room_type_code"])
		if not rt_name:
			frappe.throw(f"Unknown room type code {spec['room_type_code']}")
		for num in spec["numbers"]:
			num = str(num).strip()
			frappe.get_doc({
				"doctype": "Room", "property": prop.name,
				"room_number": num, "room_type": rt_name,
				# the Today room board groups by floor - never leave it blank
				"floor": str(spec.get("floor") or "").strip() or default_floor(num),
			}).insert()
			rooms_created += 1

	# Entire-place with no physical rooms: still create a whole_property SIU
	# via Room Type Villa sync / backfill.
	meal_plans_created = 0
	if not skip_meal_plans(prop.property_kind):
		for mp in payload.get("meal_plans", []):
			frappe.get_doc({
				"doctype": "Meal Plan", "property": prop.name,
				"code": mp["code"], "label": mp.get("label"),
				"price_per_adult": mp.get("price_per_adult", 0),
				"price_per_child": mp.get("price_per_child", 0),
				"is_default": mp.get("is_default", 0),
			}).insert()
			meal_plans_created += 1

	from kamra.savings import log_action
	log_action("setup_property", "Property", prop.name, prop.name,
	           minutes_saved=45,
	           rationale=f"Onboarded {prop.name} ({prop.property_kind}): "
	                     f"{len(rt_by_code)} room types, {rooms_created} rooms",
	           channel="API")
	# Ensure Sellable Units exist for the new inventory (ADR-001).
	siu_stats = {}
	try:
		from kamra.siu.units import backfill_property
		siu_stats = backfill_property(prop.name)
	except Exception:
		frappe.log_error(title="Sellable Unit backfill after setup_property")
	return {"property": prop.name, "room_types": len(rt_by_code),
	        "rooms": rooms_created,
	        "meal_plans": meal_plans_created,
	        "property_kind": prop.property_kind,
	        "inventory_topology": topology,
	        "pos": pos_made,
	        "sellable_units": siu_stats}



@frappe.whitelist()
def import_bookings(property: str, bookings):
	"""Bulk booking import - the switch-over tool. Each row: {guest_name,
	phone?, room_type_code, check_in, check_out, adults?, children?,
	amount_after_tax?, channel?, status?}. Rows with a fixed amount keep
	it (auto_price off); others are priced by the engine."""
	if isinstance(bookings, str):
		bookings = json.loads(bookings)
	frappe.only_for(("System Manager", "Hotel Admin"))
	from kamra.authz import assert_property_access
	assert_property_access(property)

	created, errors = [], []
	for i, row in enumerate(bookings):
		try:
			rt = frappe.db.get_value(
				"Room Type",
				{"property": property, "room_type_code": row["room_type_code"]},
			)
			if not rt:
				raise frappe.ValidationError(
					f"unknown room type code {row['room_type_code']}")
			guest = _find_or_create_guest(row["guest_name"], row.get("phone"),
			                              property, where="an import")
			doc = frappe.get_doc({
				"doctype": "Reservation",
				"property": property,
				"guest": guest,
				"room_type": rt,
				"check_in_date": row["check_in"],
				"check_out_date": row["check_out"],
				"adults": row.get("adults", 2),
				"children": row.get("children", 0),
				"source": "PMS",
				"channel": row.get("channel"),
				"auto_price": 0 if row.get("amount_after_tax") else 1,
			})
			if row.get("amount_after_tax"):
				doc.amount_after_tax = row["amount_after_tax"]
				# the import is the governed writer of its fixed amount
				# (Reservation.guard_locked_nights)
				doc.flags.kamra_night_rates_writer = True
			doc.insert()
			if row.get("status") in ("Checked In", "Cancelled"):
				doc.status = row["status"]
				doc.save()
			created.append(doc.name)
		except Exception as e:
			errors.append({"row": i + 1,
			               "guest": row.get("guest_name"),
			               "error": str(e)[:160]})

	from kamra.savings import log_action
	log_action("import_bookings", "Property", property, property,
	           minutes_saved=2 * len(created),
	           rationale=f"Imported {len(created)} bookings "
	                     f"({len(errors)} skipped)",
	           channel="API")
	return {"created": len(created), "reservations": created[:50],
	        "errors": errors}


def _pack_tax_id_label(prop) -> str:
	from kamra.localization import pack_for
	return pack_for(prop.name).locale(prop).get("tax_id_label") or "Tax ID"


def _pack_tax_label(property: str) -> str:
	from kamra.localization import pack_for
	prop = frappe.get_cached_doc("Property", property)
	return pack_for(property).locale(prop).get("tax_label") or "Tax"


def _stay_money(res):
	"""The GRC's money line: what the stay owes and what's been taken -
	advances and held deposits called out separately."""
	folio = frappe.db.get_value(
		"Folio", {"reservation": res.name, "folio_type": "Guest"},
		["name", "grand_total", "payments_total", "balance"], as_dict=True)
	if not folio:
		return None
	fdoc = frappe.get_doc("Folio", folio.name)
	rows = fdoc.payments or []
	def ksum(kind):
		return sum(float(r.amount or 0) for r in rows
		           if (r.payment_kind or "Payment") == kind)
	from kamra.folio import security_deposit_held_on_folio
	from kamra.ledger import _currency_places, _round_currency
	return {
		"folio": folio.name,
		"grand_total": float(folio.grand_total or 0),
		"paid_total": float(folio.payments_total or 0),
		"balance": float(folio.balance or 0),
		"advance": ksum("Advance"),
		"deposit_held": _round_currency(
			max(0.0, security_deposit_held_on_folio(fdoc)),
			_currency_places(res.property),
		),
		"refunded": -ksum("Refund"),
	}


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def registration_card(reservation: str):
	"""Everything the printed GRC (guest registration card) needs."""
	from kamra.day_use import format_hhmm as _format_hhmm
	res = frappe.get_doc("Reservation", reservation)
	guest = frappe.get_doc("Guest", res.guest)
	prop = frappe.get_doc("Property", res.property)
	return {
		"property": {
			"property_name": prop.property_name,
			"logo_url": prop.get("logo_url"),
			"address": ", ".join(filter(None, [  # nosemgrep: frappe-no-functional-code -- filter(None, ...) drops empty address parts; equivalent to a comprehension
				prop.address_line, prop.city, prop.state, prop.pincode])),
			"gstin": prop.gstin, "phone": prop.phone, "email": prop.email,
			"tax_id_label": _pack_tax_id_label(prop),
			"checkin_time": str(prop.checkin_time or ""),
			"checkout_time": str(prop.checkout_time or ""),
		},
		"money": _stay_money(res),
		"readiness": {
			"id_on_file": bool(guest.get("id_file") or guest.get("id_number")),
			"address_on_file": bool(guest.get("address_proof_file")),
			"occupants": len(res.get("occupants") or []),
			"pax": int(res.adults or 0) + int(res.children or 0),
			"precheckin_status": res.get("precheckin_status") or "Not sent",
			"signed": bool(res.get("precheckin_signature")),
		},
		"signature": res.get("precheckin_signature") or None,
		"tax_label": _pack_tax_label(res.property),
		"reservation": {
			"room_id": res.room,
			"room_number": frappe.db.get_value("Room", res.room, "room_number")
			               if res.room else None,
			"room_type_name": frappe.db.get_value(
				"Room Type", res.room_type, "room_type_name")
			                  if res.room_type else None,
			"actual_check_in": str(res.actual_check_in) if res.get("actual_check_in") else None,
			"actual_check_out": str(res.actual_check_out) if res.get("actual_check_out") else None,
			"name": res.name, "status": res.status,
			"room": (res.room or "").split("-")[-1],
			"room_type": res.room_type.split("-")[-1],
			"check_in_date": str(res.check_in_date),
			"check_out_date": str(res.check_out_date),
			# the times agreed for this booking; the card falls back to the
			# property's standard times when none were set
			"planned_check_in_time": (
				_format_hhmm(res.planned_check_in_time) or None
				if res.get("planned_check_in_time") not in (None, "") else None
			),
			"planned_check_out_time": (
				_format_hhmm(res.planned_check_out_time) or None
				if res.get("planned_check_out_time") not in (None, "") else None
			),
			"nights": res.nights, "adults": res.adults,
			"children": res.children, "is_day_use": res.get("is_day_use"),
			"rate_total": float(res.amount_after_tax or 0),
			"advance_paid": float(res.advance_paid or 0),
			"company": res.get("company"),
			"booked_by_name": res.get("booked_by_name"),
			"source": res.source, "eta": res.get("eta"),
			"special_requests": res.special_requests,
			"precheckin_status": res.get("precheckin_status"),
		},
		"guest": {
			"full_name": guest.full_name, "phone": guest.phone,
			"email": guest.email, "nationality": guest.nationality,
			"id_type": guest.id_type, "id_number": guest.id_number,
			"id_file": guest.get("id_file"),
			"address_proof_file": guest.get("address_proof_file"),
			"guest_id": guest.name,
			"address": ", ".join(filter(None, [  # nosemgrep: frappe-no-functional-code -- filter(None, ...) drops empty address parts; equivalent to a comprehension
				guest.get("address_line"), guest.get("city")])),
		},
		"occupants": [
			{"row": o.name, "full_name": o.full_name, "age": o.age or None,
			 "guest_stated_age": o.get("guest_stated_age") or None,
			 "gender": o.gender, "nationality": o.nationality,
			 "id_type": o.id_type, "id_number": o.id_number,
			 "phone": o.phone, "id_file": o.get("id_file"),
			 "arrived_at": _dt(o.get("arrived_at")), "left_at": _dt(o.get("left_at"))}
			for o in (res.get("occupants") or [])
		],
	}


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Night Auditor", "Revenue Manager", "Kamra Agent")
def cash_summary(property: str, date: str | None = None,
                 to_date: str | None = None):
	"""Cashier reconciliation: what the system says was collected today,
	per payment mode - the number the drawer must match at shift close.

	Prefers Cashier Transaction totals (FO + POS); falls back to folio
	payments for properties that have not opened a till yet."""
	from kamra.business_date import get_business_date
	date = date or get_business_date(property)
	try:
		from kamra.cashier import cash_summary_v2
		return cash_summary_v2(property, date, to_date)
	except frappe.ValidationError:
		raise
	except Exception:
		pass
	rows = frappe.db.sql(
		"""
		SELECT fp.mode, COUNT(*) AS txns, COALESCE(SUM(fp.amount), 0) AS total
		FROM `tabFolio Payment` fp
		JOIN `tabFolio` f ON fp.parent = f.name
		WHERE f.property = %(property)s AND fp.posting_date BETWEEN %(date)s AND %(to)s
		GROUP BY fp.mode ORDER BY total DESC
		""",
		{"property": property, "date": date, "to": to_date or date}, as_dict=True,
	)
	return {"date": date, "modes": rows,
	        "grand_total": float(sum(r.total for r in rows)),
	        "source": "folio"}


_DEPOSIT_STATUSES = ("Confirmed", "Pending Payment", "Held")


def _confirm_status(res):
	"""Held / Pending Payment → Confirmed: money has secured the booking."""
	if res.status not in ("Pending Payment", "Held"):
		return
	frappe.flags.kamra_status_transition = True
	try:
		res.status = "Confirmed"
		res.hold_expires_on = None
		res.save(ignore_permissions=True)
	finally:
		frappe.flags.kamra_status_transition = False
	res.reload()


def booking_advance_percent(res) -> float:
	"""The advance % this booking owes: the season % its dates asked for
	when it was priced (kept in its quote snapshot, so a later change to
	the season never re-bills it), else the property's deposit %."""
	snap = res.get("quote_snapshot")
	if snap is None and res.get("name"):
		snap = frappe.db.get_value("Reservation", res.name, "quote_snapshot")
	if snap:
		try:
			rules = (json.loads(snap) if isinstance(snap, str) else snap
			         ).get("stay_rules") or {}
			if float(rules.get("advance_percent") or 0) > 0:
				return float(rules["advance_percent"])
		except (ValueError, TypeError, AttributeError):
			pass  # unreadable snapshot: the property's usual %
	return float(frappe.get_cached_value("Property", res.property,
	                                     "deposit_pct") or 0)


def _deposit_state(res) -> dict:
	"""What the booking should have secured it with (its advance %, the
	figure the quote promised), what has come in before arrival, and any
	link already out with the guest."""
	from kamra.ledger import _currency_places, _money, _round_currency
	places = max(_currency_places(res.property), 2)
	total = float(res.amount_after_tax or 0)
	pct = booking_advance_percent(res)
	expected = _round_currency(_money(total) * _money(pct) / 100, places)
	paid = float(res.advance_paid or 0)
	link = frappe.db.get_value(
		"Folio", {"reservation": res.name, "folio_type": "Guest"},
		["payment_link_url", "payment_link_amount"], as_dict=True) or {}
	return {
		"pct": pct, "expected": expected, "paid": paid,
		"due": max(0.0, _round_currency(_money(expected) - _money(paid), places)),
		"link_url": link.get("payment_link_url"),
		"link_amount": float(link.get("payment_link_amount") or 0) or None,
		"can_take": res.status in _DEPOSIT_STATUSES,
	}


def _security_deposit_state(res) -> dict | None:
	try:
		from kamra.deposit import deposit_state
		return deposit_state(res)
	except Exception:
		frappe.log_error(title="security deposit state failed")
		return None


def _paid_with_key(key: str, *, reservation: str | None = None,
                   folio: str | None = None) -> str | None:
	"""The folio a payment with this idempotency key already landed on -
	scoped to one booking's folios, or to one folio."""
	return (frappe.db.sql(
		"""SELECT f.name FROM `tabFolio Payment` fp
		   JOIN `tabFolio` f ON f.name = fp.parent
		   WHERE fp.idempotency_key = %(key)s AND fp.parenttype = 'Folio'
		     AND (f.name = %(folio)s OR f.reservation = %(res)s) LIMIT 1""",
		{"key": key, "folio": folio or "", "res": reservation or ""})
		or [[None]])[0][0]


def _deposit_reservation(reservation: str):
	res = frappe.get_doc("Reservation", reservation)
	from kamra.crs import assert_property_access
	assert_property_access(res.property)
	if res.status not in _DEPOSIT_STATUSES:
		frappe.throw(_("A deposit is taken before arrival - this booking is "
		               "{0}. Take in-house payments on the folio.").format(
			res.status))
	return res


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Kamra Agent")
def record_advance(reservation: str, amount: float, mode: str = "Cash",
                   reference: str | None = None, pin: str | None = None,
                   idempotency_key: str | None = None, currency: str | None = None,
                   quote_id: str | None = None):
	"""Take a deposit / advance on a booking before arrival (#114). Opens
	the guest folio early so the money sits on the stay from day one and
	the balance at check-in is right; recorded through add_folio_payment,
	so the till, cashier PIN and ledger see it like any other payment.
	A Held / Pending Payment booking is confirmed by it.

	Pass idempotency_key (one per "Take advance" press): a retry with the
	same key returns the first result instead of posting again."""
	# one advance at a time per booking: the lock serializes a double
	# submit, so advance_paid and the key check below see the first post
	frappe.db.get_value("Reservation", reservation, "name", for_update=True)
	key = (idempotency_key or "").strip() or None
	if key:
		res = frappe.get_doc("Reservation", reservation)
		from kamra.crs import assert_property_access
		assert_property_access(res.property)
		folio = _paid_with_key(key, reservation=res.name)
		if folio:
			return {"folio": folio, "status": res.status,
			        "deposit": _deposit_state(res), "idempotent_replay": 1}
	res = _deposit_reservation(reservation)
	amount = float(amount or 0)
	if amount <= 0:
		frappe.throw(_("Enter the deposit amount."))
	from kamra.localization import assert_payment_mode
	assert_payment_mode(res.property, mode, currency, pack_only=True)
	# money guards before any write: a locked till refuses cleanly
	from kamra.authz import require_cashier_pin
	from kamra.cashier import require_open_session
	require_cashier_pin(res.property, pin)
	require_open_session(res.property)

	_confirm_status(res)
	from kamra.folio import open_folio
	folio = open_folio(res)
	posted = add_folio_payment(folio, mode, amount,
	                  reference=reference or f"deposit:{res.name}", pin=pin,
	                  kind="Advance", idempotency_key=key, currency=currency, quote_id=quote_id)
	amount = posted["payments"][-1]["amount"]
	from kamra.ledger import _currency_places, _money, _round_currency
	places = max(_currency_places(res.property), 2)
	frappe.db.set_value("Reservation", res.name, "advance_paid",
	                    _round_currency(_money(res.advance_paid) + _money(amount), places))
	res.reload()
	from kamra.savings import log_action
	log_action("record_advance", "Folio", folio, res.property,
	           rationale=f"{mode} deposit {amount:,.{places}f} on {res.name}")
	return {"folio": folio, "status": res.status, "paid": amount,
	        "deposit": _deposit_state(res)}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Kamra Agent")
def deposit_payment_link(reservation: str, amount: float | None = None):
	"""A payment link for the booking deposit - the deposit still due by
	default, or any amount the desk agrees with the guest. Paid through the
	gateway, it posts to the folio as an Advance (settle_payment_link)."""
	res = _deposit_reservation(reservation)
	amount = float(amount or 0) or _deposit_state(res)["due"]
	if amount <= 0:
		frappe.throw(_("No deposit is due - enter the amount to request."))
	from kamra.folio import open_folio
	from kamra.ledger import _currency_places
	from kamra.payments import create_payment_link
	link = create_payment_link(open_folio(res), amount=amount,
	                           purpose="Booking deposit")
	prop = frappe.get_cached_value("Property", res.property, "property_name")
	digits = _currency_places(res.property)
	link["message"] = _(
		"Hello {guest}, please pay the deposit of {cur} {amount} to secure "
		"your booking {ref} at {property} ({checkin} → {checkout}): {url}"
	).format(guest=res.guest_name, cur=link["currency"],
	         amount=f"{amount:,.{digits}f}", ref=res.name, property=prop,
	         checkin=res.check_in_date, checkout=res.check_out_date,
	         url=link["url"])
	link["deposit"] = _deposit_state(res)
	return link


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Hotel Admin")
def simulate_payment_link(reservation: str):
	"""Test mode only: act as the gateway and mark the outstanding link
	paid - for demos and training, never on a live gateway."""
	res = frappe.get_doc("Reservation", reservation)
	from kamra.crs import assert_property_access
	assert_property_access(res.property)
	from kamra.payments import _settings, settle_payment_link
	if not _settings(res.property).test_mode:
		frappe.throw(_("Only available while the payment gateway is in test mode."))
	f = frappe.db.get_value(
		"Folio", {"reservation": res.name, "folio_type": "Guest"},
		["name", "payment_link_id", "payment_link_amount"], as_dict=True)
	if not f or not f.payment_link_id:
		frappe.throw(_("No payment link has been sent for this booking."))
	posted = settle_payment_link(f.name, f.payment_link_id,
	                             float(f.payment_link_amount or 0))
	res.reload()
	return {"posted": posted, "status": res.status,
	        "deposit": _deposit_state(res)}


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Hotel Admin", "Kamra Agent")
def security_deposit(reservation: str):
	"""The stay's refundable security (damage) deposit: policy amount, what
	is held, withheld and refunded, its history and which actions are open."""
	from kamra.deposit import _reservation, deposit_state
	return deposit_state(_reservation(reservation))


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Kamra Agent")
def collect_security_deposit(reservation: str, amount: float,
                             mode: str = "Cash", reference: str | None = None,
                             pin: str | None = None,
                             idempotency_key: str | None = None):
	"""Take a refundable security deposit (villas: the damage deposit). Lands
	on the guest folio as a Security Deposit payment and in the Deposit
	ledger - a liability held for the guest, never revenue. Cashier PIN and
	open till as for any payment.

	Pass idempotency_key (one per Collect press): a retry with the same key
	returns the first result instead of posting again."""
	from kamra.deposit import collect_deposit
	return collect_deposit(reservation, amount, mode=mode, reference=reference,
	                       pin=pin, idempotency_key=idempotency_key)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Hotel Admin", "Kamra Agent")
def waive_security_deposit(reservation: str, reason: str):
	"""Waive the property's security deposit for this stay (reason required).
	Refused once money has been taken - refund it instead."""
	from kamra.deposit import waive_deposit
	return waive_deposit(reservation, reason)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Hotel Admin")
def withhold_security_deposit(reservation: str, amount: float, reason: str,
                              evidence: str | None = None,
                              pin: str | None = None):
	"""Keep all or part of the held deposit against damage (reason required).
	The withheld part posts as a damage charge on the guest folio and moves
	from the Deposit ledger to the Guest ledger to pay it."""
	from kamra.deposit import withhold_deposit
	return withhold_deposit(reservation, amount, reason, evidence, pin=pin)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Hotel Admin")
def refund_security_deposit(reservation: str, amount: float | None = None,
                            mode: str | None = None, note: str | None = None,
                            pin: str | None = None):
	"""Refund what is still held (all of it by default) through the folio
	refund path - till, folio and a Deposit-ledger debit. Cashier PIN."""
	from kamra.deposit import refund_deposit
	return refund_deposit(reservation, amount, mode=mode, note=note, pin=pin)


@frappe.whitelist()
@require_roles("Front Desk", "Hotel Admin", "Kamra Agent")
def release_access_instructions(reservation: str, force: int = 0):
	from kamra.access import release_access
	return release_access(reservation, force=int(force or 0))


@frappe.whitelist()
@require_roles("Front Desk", "Hotel Admin", "Kamra Agent")
def confirm_pending_reservation(reservation: str):
	"""Mark Held / Pending Payment / Requested as Confirmed (host or payment)."""
	res = frappe.get_doc("Reservation", reservation)
	if res.status not in ("Held", "Pending Payment", "Requested", "Waitlist"):
		frappe.throw(f"Cannot confirm from status {res.status}.")
	frappe.flags.kamra_status_transition = True
	try:
		res.status = "Confirmed"
		res.hold_expires_on = None
		res.save(ignore_permissions=True)
	finally:
		frappe.flags.kamra_status_transition = False
	try:
		from kamra.deposit import ensure_deposit_for_reservation
		ensure_deposit_for_reservation(res)
	except Exception:
		frappe.log_error(title="Security deposit ensure on confirm failed")
	return {"reservation": res.name, "status": res.status}


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Kamra Agent")
def folio_payment_link(folio: str):
	from kamra.payments import create_payment_link
	return create_payment_link(folio)


@frappe.whitelist()
@require_roles("Housekeeping", "Front Desk", "Kamra Agent")
def hk_queue(property: str):
	"""The housekeeper's phone view: prioritized task queue + room board.
	Checkout cleans for rooms with an arrival today jump the queue."""
	today = nowdate()
	arriving_rooms = set(frappe.get_all(
		"Reservation",
		filters={"property": property, "status": "Confirmed",
		         "check_in_date": today, "room": ("is", "set")},
		pluck="room",
	))

	tasks = frappe.get_all(
		"Housekeeping Task",
		filters={"property": property,
		         "status": ("in", ["Pending", "In Progress"])},
		fields=["name", "room", "task_type", "priority", "status", "notes",
		        "assigned_to_user", "assignment_status", "due_by", "creation"],
		order_by="creation asc",
	)
	me = frappe.session.user
	now_dt = now_datetime()
	prio_rank = {"Urgent": 0, "High": 1, "Medium": 2, "Low": 3}
	for t in tasks:
		t["arrival_today"] = t.room in arriving_rooms
		t["mine"] = bool(t.assigned_to_user) and t.assigned_to_user == me
		t["claimable"] = not t.assigned_to_user
		t["overdue"] = bool(t.due_by and get_datetime(t.due_by) < now_dt)
	tasks.sort(key=lambda t: (
		not t["arrival_today"],
		0 if t.task_type == "Checkout Clean" else 1,
		prio_rank.get(t.priority, 9),
		str(t.creation),
	))

	rooms = frappe.get_all(
		"Room",
		filters={"property": property},
		fields=["name", "room_number", "floor", "housekeeping_status",
		        "occupancy_status"],
		order_by="room_number asc",
	)

	numbers = {r.name: r.room_number for r in rooms}
	for task in tasks:
		task["room_number"] = numbers.get(task.room)

	# guest context the housekeeper needs on the floor: who's in the room or
	# arriving, whether they're a VIP, their requests and arrival timing
	ctx = {}
	stays = frappe.get_all(
		"Reservation",
		filters={"property": property, "room": ("is", "set"),
		         "status": ("in", ["Confirmed", "Checked In"]),
		         "check_in_date": ("<=", today), "check_out_date": (">=", today)},
		fields=["room", "guest", "guest_name", "status", "special_requests",
		        "eta", "check_in_date", "check_out_date"],
	) + frappe.get_all(
		"Reservation",
		filters={"property": property, "room": ("is", "set"),
		         "status": "Confirmed", "check_in_date": today},
		fields=["room", "guest", "guest_name", "status", "special_requests",
		        "eta", "check_in_date", "check_out_date"],
	)
	vips = set(frappe.get_all(
		"Guest", filters={"name": ("in", [s.guest for s in stays if s.guest]), "vip": 1},
		pluck="name")) if stays else set()
	for s in stays:
		c = ctx.setdefault(s.room, {})
		if s.guest in vips:
			c["vip"] = 1
		if s.special_requests:
			c["special_requests"] = s.special_requests
		if str(s.check_in_date) == today:
			c["arrival_today"] = 1
			c["eta"] = str(s.eta or "")
			c["arriving_guest"] = s.guest_name
		if str(s.check_out_date) == today:
			c["departure_today"] = 1
		if s.status == "Checked In":
			c["in_house_guest"] = s.guest_name

	for r in rooms:
		r["arrival_today"] = r.name in arriving_rooms
		r.update(ctx.get(r.name, {}))
	# tasks carry the same context so a card can show the VIP star inline
	for t in tasks:
		c = ctx.get(t.room, {})
		t["vip"] = c.get("vip", 0)
		t["special_requests"] = t.get("special_requests") or c.get("special_requests")
		t["eta"] = c.get("eta")

	# completion media (proof-of-clean photos/videos) attached to each task
	task_names = [t.name for t in tasks]
	media = {}
	if task_names:
		for f in frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Housekeeping Task",
			         "attached_to_name": ("in", task_names)},
			fields=["attached_to_name", "file_url"], order_by="creation asc"):
			media.setdefault(f.attached_to_name, []).append(f.file_url)
	for t in tasks:
		t["media"] = media.get(t.name, [])

	return {"date": today, "tasks": tasks, "rooms": rooms}


@frappe.whitelist()
@require_roles("Housekeeping", "Front Desk", "Kamra Agent")
def hk_update_task(task: str, status: str):
	"""Start or complete a housekeeping task from the phone.

	An Inspection is completed by passing it: Done on an inspection is taken
	as Verified, which moves the room to Ready (inspection required by the
	turnover profile) or Inspected - never back to plain Clean. Verifying -
	an inspection or anyone's clean - is a supervisor's call, the same rule
	set_housekeeping_status applies to Inspected / Ready."""
	if status not in ("In Progress", "Done", "Verified"):
		frappe.throw("Status must be In Progress, Done or Verified.")
	doc = frappe.get_doc("Housekeeping Task", task)
	if doc.task_type == "Inspection" and status == "Done":
		status = "Verified"
	if status == "Verified" and not _is_room_supervisor():
		frappe.throw(_("Only a housekeeping supervisor can pass an inspection "
		               "or verify a clean."), frappe.PermissionError)
	doc.status = status
	doc.save()
	if status in ("Done", "Verified"):
		from kamra.savings import log_action
		log_action("housekeeping_done", "Housekeeping Task", doc.name,
		           doc.property, minutes_saved=3,
		           rationale=f"{doc.task_type} for {doc.room} closed from mobile",
		           channel="API")
	return {"ok": True, "task": doc.name, "status": doc.status}


@frappe.whitelist(methods=["POST"])
@require_roles("Housekeeping", "Front Desk", "Kamra Agent")
def hk_upload_media(task: str):
	"""Attach a completion photo/video (proof of clean) to a housekeeping task.

	Uploaded from the housekeeper's phone while the room is being serviced. Stored
	public and attached to the task, so it stays with the record after the task
	closes and drops out of the live queue."""
	if not frappe.db.exists("Housekeeping Task", task):
		frappe.throw("Task not found.")
	req = getattr(frappe.local, "request", None)
	files = getattr(req, "files", {}) if req else {}
	if "file" not in files:
		frappe.throw("No file uploaded.")
	f = files["file"]
	content = f.stream.read()
	if len(content) > 25 * 1024 * 1024:
		frappe.throw("File is too large - keep photos/videos under 25 MB.")
	# endpoint-gated (Kamra authorizes on the route, not the doctype), so the
	# File is created ignore_permissions and attached to the task.
	saved = frappe.get_doc({
		"doctype": "File",
		"file_name": f.filename,
		"content": content,
		"attached_to_doctype": "Housekeeping Task",
		"attached_to_name": task,
		"is_private": 0,
	}).insert(ignore_permissions=True)
	from kamra.savings import log_action
	log_action("hk_media_upload", "Housekeeping Task", task,
	           frappe.db.get_value("Housekeeping Task", task, "property"),
	           rationale=f"Completion media added to {task}", channel="API")
	return {"file_url": saved.file_url, "file_name": saved.file_name}


@frappe.whitelist(methods=["POST"])
@require_roles("Housekeeping", "Front Desk", "Kamra Agent")
def hk_delete_media(task: str, file_url: str):
	"""Remove a completion photo/video from a housekeeping task."""
	name = frappe.db.get_value("File", {
		"attached_to_doctype": "Housekeeping Task",
		"attached_to_name": task,
		"file_url": file_url,
	})
	if not name:
		frappe.throw("That file is not attached to this task.")
	frappe.delete_doc("File", name, ignore_permissions=True)
	return {"ok": True}


@frappe.whitelist()
@require_roles("Housekeeping", "Front Desk", "Hotel Admin", "Kamra Agent")
def hk_task_media(task: str):
	"""Completion photos/videos attached to one housekeeping task (newest first)."""
	return frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Housekeeping Task",
		         "attached_to_name": task},
		fields=["file_url"], order_by="creation desc", pluck="file_url")


@frappe.whitelist()
@require_roles("Housekeeping", "Front Desk", "Hotel Admin", "Kamra Agent")
def hk_room_media(room: str):
	"""Photos/videos from the room's LATEST cleaning only - so the front desk
	sees just the most recent clean, not older cycles. Lets reception confirm a
	room is genuinely guest-ready."""
	tasks = frappe.get_all("Housekeeping Task", filters={"room": room}, pluck="name")
	if not tasks:
		return []
	# the newest attached file marks the most recent cleaning that has media
	latest = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Housekeeping Task",
		         "attached_to_name": ("in", tasks)},
		fields=["attached_to_name"], order_by="creation desc", limit=1)
	if not latest:
		return []
	return frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Housekeeping Task",
		         "attached_to_name": latest[0].attached_to_name},
		fields=["file_url"], order_by="creation asc", pluck="file_url")


def _hk_staff(property: str) -> list[dict]:
	"""Enabled staff who can work housekeeping tasks at this property: a
	Housekeeping or Housekeeping Supervisor role, and no property
	restriction that leaves this one out."""
	holders = set(frappe.get_all(
		"Has Role",
		filters={"role": ("in", ["Housekeeping", "Housekeeping Supervisor"]),
		         "parenttype": "User",
		         "parent": ("not in", ["Administrator", "Guest"])},
		pluck="parent"))
	if not holders:
		return []
	restricted: dict[str, set] = {}
	for p in frappe.get_all("User Permission",
	                        filters={"user": ("in", list(holders)), "allow": "Property"},
	                        fields=["user", "for_value"]):
		restricted.setdefault(p.user, set()).add(p.for_value)
	users = frappe.get_all(
		"User", filters={"name": ("in", list(holders)), "enabled": 1},
		fields=["name", "full_name"], order_by="full_name asc")
	return [{"user": u.name, "full_name": u.full_name or u.name}
	        for u in users
	        if u.name not in restricted or property in restricted[u.name]]


@frappe.whitelist()
@require_roles("Housekeeping Supervisor", "Front Desk", "Kamra Agent")
def hk_staff(property: str):
	"""Who a supervisor can hand a housekeeping task to at this property."""
	from kamra.crs import assert_property_access
	assert_property_access(property)
	return _hk_staff(property)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Housekeeping", "Kamra Agent")
def hk_assign_task(task: str, user: str):
	"""A supervisor hands a task to a specific housekeeper (awaits accept).
	Attendants take work from the pool with hk_claim_task; handing a task to
	someone else is the supervisor's (or desk's) call, and only to a
	housekeeper who can work this property."""
	if not _is_room_supervisor():
		frappe.throw(_("Only a housekeeping supervisor can assign tasks."),
		             frappe.PermissionError)
	doc = frappe.get_doc("Housekeeping Task", task)
	if doc.status not in ("Pending", "In Progress"):
		frappe.throw(_("Task {0} is already {1}.").format(doc.name, doc.status))
	if user not in {s["user"] for s in _hk_staff(doc.property)}:
		frappe.throw(_("{0} is not housekeeping staff at this property.").format(user))
	doc.assigned_to_user = user
	doc.assignment_status = "Assigned"
	doc.save()
	from kamra.savings import log_action
	log_action("hk_assign", "Housekeeping Task", doc.name, doc.property,
	           rationale=f"{doc.task_type} {doc.room} → {user}")
	return {"ok": True, "assignment_status": "Assigned", "assigned_to_user": user}


@frappe.whitelist(methods=["POST"])
@require_roles("Housekeeping", "Front Desk", "Kamra Agent")
def hk_claim_task(task: str):
	"""A housekeeper takes an unassigned task from the pool for themselves."""
	doc = frappe.get_doc("Housekeeping Task", task)
	if doc.assigned_to_user and doc.assigned_to_user != frappe.session.user:
		frappe.throw(f"Already taken by {doc.assigned_to_user}.")
	doc.assigned_to_user = frappe.session.user
	doc.assignment_status = "Accepted"
	doc.save()
	return {"ok": True, "assignment_status": "Accepted"}


@frappe.whitelist(methods=["POST"])
@require_roles("Housekeeping", "Front Desk", "Kamra Agent")
def hk_accept_task(task: str):
	"""The assigned housekeeper accepts the task handed to them."""
	doc = frappe.get_doc("Housekeeping Task", task)
	doc.assignment_status = "Accepted"
	if not doc.assigned_to_user:
		doc.assigned_to_user = frappe.session.user
	doc.save()
	return {"ok": True, "assignment_status": "Accepted"}


@frappe.whitelist(methods=["POST"])
@require_roles("Housekeeping", "Front Desk", "Kamra Agent")
def hk_reject_task(task: str, reason: str = ""):
	"""Decline a task - it drops back into the pool for someone else,
	keeping the reason on record."""
	doc = frappe.get_doc("Housekeeping Task", task)
	who = doc.assigned_to_user or frappe.session.user
	doc.assigned_to_user = None
	doc.assignment_status = "Unassigned"
	doc.reject_reason = f"{who}: {reason}" if reason else who
	doc.save()
	from kamra.savings import log_action
	log_action("hk_reject", "Housekeeping Task", doc.name, doc.property,
	           rationale=f"{who} declined {doc.task_type} {doc.room}: {reason}")
	return {"ok": True, "assignment_status": "Unassigned"}


@frappe.whitelist(methods=["POST"])
@require_roles("Housekeeping", "Front Desk", "Kamra Agent")
def hk_log_item(property: str, item_description: str, condition: str = "Found",
                room: str | None = None):
	"""A floor housekeeper logs a lost/found/missing/damaged item from the
	phone. Lands in the Lost & Found register for the desk to reconcile."""
	doc = frappe.get_doc({
		"doctype": "Lost And Found Item",
		"property": property,
		"item_description": item_description,
		"condition": condition,
		"found_in_room": room or None,
		"found_on": nowdate(),
		"found_by": frappe.session.user,
		"status": "In Storage",
	}).insert()
	from kamra.savings import log_action
	log_action("lost_found_logged", "Lost And Found Item", doc.name, property,
	           rationale=f"{condition}: {item_description}"
	                     + (f" in {room}" if room else ""))
	return {"ok": True, "name": doc.name}


@frappe.whitelist(methods=["POST"])
@require_roles("Housekeeping", "Front Desk", "Kamra Agent")
def hk_post_consumable(room: str, charge_type: str, description: str,
                       amount: float):
	"""Housekeeping posts what they find in the room - minibar consumption or
	laundry - onto the in-house guest's folio. Scoped to those two types so
	the floor can't touch discounts, allowances or room charges."""
	if charge_type not in ("Minibar", "Laundry"):
		frappe.throw("Housekeeping can only post Minibar or Laundry.")
	if float(amount) <= 0:
		frappe.throw("Amount must be positive.")
	res = frappe.db.get_value(
		"Reservation",
		{"room": room, "status": "Checked In"},
		["name", "property"], as_dict=True)
	if not res:
		frappe.throw("No checked-in guest in that room to bill.")
	from kamra.pos import assert_room_charges_allowed
	assert_room_charges_allowed(res.name)
	# post through the governed agent user (same pattern as public bookings):
	# the housekeeper is authorized above and scoped to two charge types, and
	# GST is still resolved server-side. Attribution is stamped below.
	# laundry is a taxable service at the country pack's services rate (the
	# same rate a Laundry Order bills at); minibar resolves to the F&B rate
	# inside post_stay_charge
	gst_rate = 0
	if charge_type == "Laundry":
		from kamra.localization import pack_for, services_tax_rate
		gst_rate = services_tax_rate(pack_for(res.property), res.property)
	with act_as(AGENT_EMAIL):
		out = post_stay_charge(res.name, charge_type, description, float(amount),
		                       gst_rate=gst_rate)
	from kamra.savings import log_action
	from kamra.localization import money_text
	log_action("hk_charge", "Folio", out.get("folio"), res.property,
	           rationale=f"{charge_type} {money_text(res.property, amount)} "
	                     f"to {room} ({description})")
	return {"ok": True, "folio": out.get("folio"), "balance": out.get("balance")}


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def create_ticket(property: str, subject: str, category: str,
                  priority: str = "Medium", room: str | None = None,
                  reservation: str | None = None, guest: str | None = None,
                  description: str | None = None, source: str = "Manual"):
	"""Create a guest-request ticket. This is also the agent tool for
	'guest wants towels / AC is broken' - PRD FR-42."""
	doc = frappe.get_doc({
		"doctype": "Service Ticket",
		"property": property,
		"subject": subject,
		"category": category,
		"priority": priority,
		"room": room or None,
		"reservation": reservation or None,
		"guest": guest or None,
		"description": description,
		"source": source,
	})
	doc.insert()
	if source in ("AI Agent", "WhatsApp", "Voice", "QR"):
		from kamra.savings import log_action
		log_action("create_ticket", "Service Ticket", doc.name, property,
		           minutes_saved=4,
		           rationale=f"Captured request without staff: {subject}",
		           agent_name="Concierge",
		           # the ticket source names who raised it; the log channel
		           # names how it reached Kamra (an AI agent is API)
		           channel=({"AI Agent": agent_channel() or "API", "QR": "API"}
		                    .get(source, source)))
	return {"ticket": doc.name, "due_by": str(doc.due_by)}


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def tickets_list(property: str, show_closed: int = 0):
	filters = {"property": property}
	if not int(show_closed):
		filters["status"] = ("in", ["Open", "In Progress"])
	rows = frappe.get_all(
		"Service Ticket",
		filters=filters,
		fields=["name", "subject", "category", "priority", "status", "source",
		        "room", "guest_name", "due_by", "resolved_on", "breached",
		        "assigned_to_user", "creation"],
		order_by="creation desc",
		limit=200,
	)
	now = str(frappe.utils.now_datetime())
	for r in rows:
		r["overdue"] = bool(
			r.status in ("Open", "In Progress")
			and r.due_by and str(r.due_by) < now
		)
	return rows


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def advance_ticket(ticket: str, status: str, resolution_note: str | None = None):
	allowed = {"In Progress", "Resolved", "Closed", "Cancelled"}
	if status not in allowed:
		frappe.throw(f"Status must be one of {sorted(allowed)}")
	doc = frappe.get_doc("Service Ticket", ticket)
	doc.status = status
	if resolution_note:
		doc.resolution_note = resolution_note
	doc.save()
	return {"ticket": doc.name, "status": doc.status,
	        "breached": bool(doc.breached)}


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Night Auditor", "Kamra Agent")
def get_folio(reservation: str):
	"""Folio for a reservation - opens one if the guest is checked in."""
	name = frappe.db.get_value(
		"Folio", {"reservation": reservation, "folio_type": "Guest"}
	)
	if not name:
		res = frappe.get_doc("Reservation", reservation)
		if res.status not in ("Checked In", "Checked Out"):
			return None
		from kamra.folio import open_folio
		name = open_folio(res)
	folio = frappe.get_doc("Folio", name)
	return folio.as_dict()


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Kamra Agent")
def add_folio_charge(folio: str, charge_type: str, description: str,
                     amount: float, gst_rate: float = 0,
                     posting_date: str | None = None, is_alcohol: int = 0,
                     reservation: str | None = None):
	_refuse_opening_balance(charge_type)
	# check what the caller sent, before the hotel's own rules pick a rate
	_check_charge_amount_and_tax(
		frappe.db.get_value("Folio", folio, "property"), amount, gst_rate)
	return _post_folio_charge(folio, charge_type, description, amount,
	                          gst_rate, posting_date, is_alcohol, reservation)


def _refuse_opening_balance(charge_type: str):
	"""An opening balance is carried over by the go-live import only - a
	hand-posted one would look like an old-system balance to the audit."""
	if charge_type == "Opening Balance":
		frappe.throw("Opening balances are posted by the go-live import "
		             "only (Go-live import screen).")
	if charge_type == "Rate Adjustment":
		frappe.throw(f"Rate adjustments are posted by {product_name()} when a stay's rate "
		             "changes. Post an allowance to give money back.")


def _post_folio_charge(folio: str, charge_type: str, description: str,
                       amount: float, gst_rate: float = 0,
                       posting_date: str | None = None, is_alcohol: int = 0,
                       reservation: str | None = None,
                       transaction_code: str | None = None):
	"""Put one charge on a bill. Not whitelisted and does no amount or rate
	check: a caller that takes an amount or rate from outside calls
	_check_charge_amount_and_tax on it first. Code that posts rates the
	hotel itself decided (the agent's post_stay_charge, banquet billing)
	calls this directly."""
	_refuse_opening_balance(charge_type)
	doc = frappe.get_doc("Folio", folio)
	if doc.status == "Closed":
		frappe.throw("Folio is closed.")
	if int(is_alcohol or 0) and doc.folio_type in ("Company", "Group"):
		frappe.throw("Alcohol cannot be billed to a company folio - "
		             "post it to the guest folio.")
	# tax is the hotel's rule, not the caller's guess - without this an
	# F&B/minibar charge posted without a rate lands on the bill untaxed
	gst_rate = _resolve_charge_gst(doc.property, charge_type, description,
	                               int(is_alcohol or 0), float(gst_rate or 0))
	if not float(gst_rate or 0):
		from kamra.pricing import property_currency, round_rate_amount
		amount = float(round_rate_amount(amount, 0, "Nearest", property_currency(doc.property)))
		if amount <= 0:
			frappe.throw("Charge amount must be more than zero. To reduce a "
			             "bill, post an allowance or void the charge.")
	from kamra.business_date import assert_transaction_date
	from kamra.ledger import code_for_charge_type, record_charge_ledger
	bd = assert_transaction_date(doc.property, posting_date, label="Posting date")
	# a configured code (banquet Revenue Type Mapping) else the charge type's default
	code = transaction_code or code_for_charge_type(charge_type)
	doc.append("charges", {
		"posting_date": bd,
		"charge_type": charge_type,
		"transaction_code": code,
		"reservation": reservation or doc.reservation,
		"description": description,
		"qty": 1,
		"rate": float(amount),
		"amount": float(amount),
		"gst_rate": float(gst_rate),
		"is_alcohol": 1 if int(is_alcohol or 0) else 0,
	})
	from kamra.folio import _recalculate
	_recalculate(doc)
	doc.save()
	charge = doc.charges[-1]
	try:
		record_charge_ledger(doc, charge.as_dict())
		import kamra.ledger as _ledger_mod
		if hasattr(_ledger_mod, "sync_folio_rounding_ledger"):
			_ledger_mod.sync_folio_rounding_ledger(doc)
	except Exception:
		frappe.log_error(title="ledger charge write failed")
	from kamra.localization import money_text
	from kamra.savings import log_action
	log_action("post_charge", "Folio", doc.name, doc.property,
	           rationale=f"{charge_type}: {description} "
	                     f"{money_text(doc.property, amount)}")
	from kamra.webhooks import emit_folio_posted
	emit_folio_posted(doc, charge)
	from kamra.whatsapp import queue_room_charge_alert
	queue_room_charge_alert(doc, charge)
	return doc.as_dict()




def _pin_guard(folio: str, pin=None):
	from kamra.authz import require_cashier_pin
	require_cashier_pin(frappe.db.get_value("Folio", folio, "property"), pin)

@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Kamra Agent")
def add_folio_payment(folio: str, mode: str, amount: float,
                      reference: str | None = None, pin: str | None = None,
                      kind: str = "Payment", currency: str | None = None,
                      idempotency_key: str | None = None, quote_id: str | None = None):
	"""Record money received on the stay ledger. kind labels WHY it came
	in - Payment (against the bill), Advance (collected before/at
	check-in) or Security Deposit (held, refundable). Refunds go through
	refund_folio_payment so they can never be entered by accident.

	currency: leave out (or pass the home currency) for a normal payment.
	A foreign code means `amount` is the foreign money received (cash or a
	bank transfer); the server converts it at the published settlement rate, posts the
	home amount and keeps the foreign amount and rate on the row.
	A retry with the same idempotency_key returns the folio without
	posting again."""
	if kind not in ("Payment", "Advance", "Security Deposit"):
		frappe.throw("kind must be Payment, Advance or Security Deposit.")
	if float(amount) <= 0:
		frappe.throw("Amount must be positive.")
	_pin_guard(folio, pin)
	key = (idempotency_key or "").strip() or None
	if key:
		# serialize posts on this folio so a double submit sees the first
		frappe.db.get_value("Folio", folio, "name", for_update=True)
		if _paid_with_key(key, folio=folio):
			out = frappe.get_doc("Folio", folio).as_dict()
			out["idempotent_replay"] = 1
			return out
	doc = frappe.get_doc("Folio", folio)
	if doc.status == "Closed":
		frappe.throw(_("This folio is closed. Record corrections through a credit note or reopen per policy."))
	from kamra.localization import FOREIGN_CURRENCY_TENDER_TYPES, assert_payment_mode
	from kamra.masters import default_mode_for_tender, is_system_payment_method, payment_tender_type
	assert_payment_mode(doc.property, mode, currency)
	from kamra.business_date import get_business_date
	from kamra.cashier import assert_cashier_mode_allowed, record_cashier_txn, require_open_session
	from kamra.ledger import _round_currency, home_currency, payment_rate
	from kamra.pricing import _minor_unit
	assert_cashier_mode_allowed(doc.property, mode)
	bd = get_business_date(doc.property)
	# Property currency — never leave Folio Payment on the INR schema default (#341)
	currency_home = home_currency(doc.property)
	fx = None
	currency = (currency or "").strip().upper()
	foreign = bool(currency and currency != currency_home)
	tender = payment_tender_type(mode, doc.property)
	if tender == "Foreign Cash":
		if not foreign:
			frappe.throw(_("Foreign Cash needs the currency of the notes received."))
	if foreign:
		if tender not in FOREIGN_CURRENCY_TENDER_TYPES:
			frappe.throw("Foreign currency is taken as Cash or Bank Transfer "
			             "only. A card is charged in the hotel's currency.")
		if kind == "Security Deposit":
			frappe.throw("Take a security deposit in the hotel's currency: "
			             "it is refunded in that currency.")
		r = payment_rate(doc.property, currency, amount)
		if quote_id and quote_id != r["quote_id"]:
			frappe.throw("HSP-CASHIER-FX-RATE: The published rate changed. Refresh the preview and confirm again.")
		fx_places = _minor_unit(r["currency"])
		fx = {"currency": r["currency"], "fx_amount": _round_currency(amount, fx_places),
		      "exchange_rate": r["rate"], "exchange_rate_date": r["rate_date"]}
		amount = r["home_amount"]
		if amount <= 0:
			frappe.throw("Amount must be positive.")
	currency = currency_home
	if fx and tender == "Cash" and is_system_payment_method(mode, doc.property):
		# system Cash + foreign currency maps to the Foreign Cash tender code;
		# custom Cash-type tenders keep their own mode and stay out of home cash
		# via currency/fx_amount on Cashier Transaction.
		mode = default_mode_for_tender(doc.property, "Foreign Cash")
	sess = None
	try:
		sess = require_open_session(doc.property)
	except Exception:
		if "Kamra Agent" not in frappe.get_roles() and \
		   frappe.session.user != "Administrator":
			raise
	doc.append("payments", {
		"posting_date": bd,
		"payment_kind": kind,
		"mode": mode,
		"amount": float(amount),
		"reference": reference,
		"currency": currency,
		**(fx or {}),  # an FX row keeps the foreign currency, amount and rate
		"idempotency_key": key,
	})
	prev_grand = doc.get("grand_total")
	from kamra.folio import _recalculate
	_recalculate(doc, payments_only=True)
	doc.save()
	pay = doc.payments[-1]
	fx_note = (f"{fx['currency']} {fx['fx_amount']:,.{_minor_unit(fx['currency'])}f} @ {fx['exchange_rate']}"
	           if fx else None)
	record_cashier_txn(
		doc.property, "Payment", mode, float(amount),
		folio=doc.name, reference=reference, notes=fx_note, session=sess,
		tender_validated=True, currency=pay.currency, fx_amount=float(pay.fx_amount or 0))
	try:
		from kamra import ledger as _ledger_mod
		_ledger_mod.record_payment_ledger(doc, pay.as_dict(), session=sess)
		if (
			prev_grand is not None
			and abs(float(doc.grand_total or 0) - float(prev_grand)) > 1e-9
			and hasattr(_ledger_mod, "sync_folio_rounding_ledger")
		):
			_ledger_mod.sync_folio_rounding_ledger(doc, session=sess)
	except Exception:
		frappe.log_error(title="ledger payment write failed")
	if kind in ("Advance", "Security Deposit"):
		from kamra.webhooks import emit_payment
		emit_payment("deposit.received", doc, pay)
	if kind == "Advance" and doc.reservation and frappe.db.get_value(
			"Reservation", doc.reservation, "status") == "Checked In":
		# an advance taken from a guest already in house pays the stay
		from kamra.ledger import transfer_advances_to_guest
		transfer_advances_to_guest(doc.reservation)
	elif kind == "Advance" and doc.folio_type == "Group" and doc.get("group_booking"):
		# the group's bill: paid in advance while any room of the group is in
		# house, it pays the group at once, whichever room it is anchored to
		from kamra.ledger import transfer_group_advances
		transfer_group_advances(doc.group_booking)
	if kind == "Security Deposit" and doc.reservation and \
	   (doc.folio_type or "Guest") == "Guest":
		# keep the stay's Security Deposit record in step, whichever screen
		# took the money (the deposit panel or the folio's own payment form)
		from kamra.deposit import record_collected
		record_collected(doc.reservation, float(amount), mode)
	elif kind != "Security Deposit":
		# who took which money, for the activity log (a deposit logs its own)
		from kamra.localization import money_text
		from kamra.savings import log_action
		log_action("folio_payment", "Folio", doc.name, doc.property,
		           rationale=f"{kind} {money_text(doc.property, amount)} by {mode}"
		                     + (f" ({fx_note})" if fx_note else "")
		                     + (f" ({reference})" if reference else ""))
	return doc.as_dict()


MAX_SPLIT_LINES = 20


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Front Desk", "Kamra Agent")
def add_folio_payments(payments, pin: str | None = None):
	"""Record several payments in one request: one guest settling a bill
	part by cash, part by card, part by UPI.

	`payments` is a list of {folio, mode, amount, reference?, kind?,
	idempotency_key?}. Each row is posted exactly as add_folio_payment
	posts it (till, ledger, activity log), and all rows share one
	transaction, so if any row is refused none is kept and the folio is
	never left half-paid. Returns the folios touched and how many rows
	were posted."""
	if isinstance(payments, str):
		payments = json.loads(payments)
	if not isinstance(payments, list) or not payments:
		frappe.throw("Give at least one payment.")
	if len(payments) > MAX_SPLIT_LINES:
		frappe.throw(f"At most {MAX_SPLIT_LINES} payments at a time.")
	for i, row in enumerate(payments, 1):
		if not isinstance(row, dict) or not row.get("folio") or not row.get("mode"):
			frappe.throw(f"Payment {i}: a folio and a mode are needed.")
		if float(row.get("amount") or 0) <= 0:
			frappe.throw(f"Payment {i}: the amount must be positive.")
	folios: list[str] = []
	# a savepoint, so a refused row undoes the rows before it even when the
	# caller keeps the transaction open (a request would roll back anyway)
	frappe.db.savepoint("split_payment")
	try:
		for row in payments:
			add_folio_payment(
				row["folio"], row["mode"], float(row["amount"]),
				reference=(row.get("reference") or "").strip() or None,
				pin=pin, kind=row.get("kind") or "Payment",
				currency=row.get("currency"),
				idempotency_key=row.get("idempotency_key"),
			)
			if row["folio"] not in folios:
				folios.append(row["folio"])
	except Exception:
		frappe.db.rollback(save_point="split_payment")
		raise
	return {"posted": len(payments), "folios": folios}


def _booked_correction_script(doc, row_name, method):
	from kamra.corrections import _row
	from kamra.erp_bridge import get_bridge_script

	row, charge = _row(doc, row_name)
	if not charge and row.get("payment_entry"):
		return get_bridge_script("hospitality_kamra." + method)


@frappe.whitelist()
def erp_correction_context(api_method: str) -> dict:
	"""safe_exec creates separate flags. Expose only the active method's verified actors."""
	flags = frappe.flags
	if flags.kamra_erp_correction_authorized is not True or flags.kamra_erp_correction_method != api_method:
		frappe.throw("A governed folio correction is required.", frappe.PermissionError)
	return {"requester": flags.kamra_erp_correction_requester, "approver": flags.kamra_erp_correction_approver}


def _execute_correction_script(script, args, approver=None):
	from kamra.erp_bridge import run_bridge_script

	flags = dict(kamra_erp_correction_authorized=True, kamra_erp_correction_method=script.api_method,
	             kamra_erp_correction_requester=frappe.session.user, kamra_erp_correction_approver=approver)
	previous = {k: frappe.flags[k] for k in flags if k in frappe.flags}
	try:
		with act_as(approver) if approver else nullcontext():
			frappe.flags.update(flags)
			return run_bridge_script(script, args)
	finally:
		for k in flags:
			frappe.flags.pop(k, None)
		frappe.flags.update(previous)


@frappe.whitelist()
@require_roles("Front Desk", "Finance")
def payment_correction_options(folio: str, payment_row: str):
	from kamra.corrections import options
	doc = frappe.get_doc("Folio", folio)
	script = _booked_correction_script(doc, payment_row, "payment_correction_options")
	if script:
		return _execute_correction_script(script, dict(folio=folio, row_name=payment_row))
	return options(doc, payment_row)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance")
def correct_folio_payment(folio: str, payment_row: str, action: str | None = None,
                          reason_code: str | None = None, reason: str | None = None,
                          idempotency_key: str | None = None, pin: str | None = None,
                          supervisor_pin: str | None = None, supervisor_user: str | None = None,
                          refund_amount: float | None = None, destination_mode: str | None = None,
                          action_type: str | None = None, reason_note: str | None = None):
	from kamra.authz import SUPERVISOR_ROLES, verify_supervisor
	from kamra.corrections import correct
	doc = frappe.get_doc("Folio", folio, for_update=True)
	script = _booked_correction_script(doc, payment_row, "correct_folio_payment")
	if script:
		approver = None
		if supervisor_user is not None or supervisor_pin is not None:
			approver = verify_supervisor(doc.property, supervisor_user, supervisor_pin,
			                             roles=SUPERVISOR_ROLES, requester=frappe.session.user)
		return _execute_correction_script(script, dict(folio=folio, row_name=payment_row,
			action=action_type or action, reason_code=reason_code, reason_text=reason_note or reason,
			new_payment_mode=destination_mode, refund_amount=refund_amount, supervisor=approver,
			idempotency_key=(idempotency_key or "").strip()), approver)
	return correct(folio, payment_row, action_type or action, reason_code, reason_note or reason,
	               (idempotency_key or "").strip(), pin, supervisor_user, supervisor_pin, refund_amount, destination_mode)


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Front Desk", "Kamra Agent")
def refund_folio_payment(folio: str, amount: float | None = None,
                         mode: str | None = None, reason: str = "",
                         pin: str | None = None,
                         reason_code: str | None = None,
                         supervisor_pin: str | None = None,
                         payment_row: str | None = None,
                         idempotency_key: str | None = None,
                         currency: str | None = None):
	"""Give money back on an open folio - a held security deposit at
	checkout, or an over-collected advance. Stored as a negative ledger
	row so every balance still sums exactly; a reason is mandatory.

	payment_row refunds against one payment line: mode defaults to that
	payment's mode (or the home Cash tender for home-currency refunds of a
	Foreign Cash tender) and amount to what is left of it. Pass currency for
	a foreign-currency refund so the foreign drawer and tender stay aligned.
	A retry with the same idempotency_key returns the first refund instead of
	paying out twice."""
	key = (idempotency_key or "").strip() or None
	if key:
		# serialize refunds on this folio so a double submit sees the first
		frappe.db.get_value("Folio", folio, "name", for_update=True)
		if _paid_with_key(key, folio=folio):
			return {"ok": True, "idempotent_replay": 1,
			        "balance": frappe.db.get_value("Folio", folio, "balance")}
	doc = frappe.get_doc("Folio", folio)
	from decimal import ROUND_HALF_UP, Decimal

	from babel.numbers import get_currency_precision

	from kamra.ledger import (
		_currency_places,
		_money,
		_parse_currency_amount,
		_round_currency,
		fx_to_home,
		home_currency,
		payment_rate,
	)
	from kamra.localization import FOREIGN_CURRENCY_TENDER_TYPES, money_text
	from kamra.masters import default_mode_for_tender, payment_tender_type
	from kamra.pricing import _minor_unit

	home = home_currency(doc.property)
	raw_places = _currency_places(doc.property)
	places = max(raw_places, 2)
	req_ccy = (currency or "").strip().upper()
	foreign = bool(req_ccy and req_ccy != home)
	fx_meta: dict | None = None

	if payment_row:
		pay = next((p for p in doc.payments if p.name == payment_row), None)
		if not pay or float(pay.amount or 0) <= 0:
			frappe.throw(_("That payment is not on this folio."))
		if pay.payment_kind == "Security Deposit":
			frappe.throw(_("A security deposit is refunded from the stay's "
			               "deposit (refund_deposit), not from the bill."))
		left_dec = (
			_money(pay.amount) + sum(
				(_money(p.amount) for p in doc.payments
				 if p.get("refund_of") == payment_row),
				Decimal(0),
			)
		).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
		if left_dec <= 0:
			frappe.throw(_("This payment is already refunded in full."))
		pay_tender = payment_tender_type(pay.mode, doc.property)
		if foreign:
			fx_places = _minor_unit(req_ccy)
			mode = mode or (
				pay.mode if pay_tender in FOREIGN_CURRENCY_TENDER_TYPES
				else default_mode_for_tender(doc.property, "Foreign Cash")
			)
			if payment_tender_type(mode, doc.property) not in FOREIGN_CURRENCY_TENDER_TYPES:
				frappe.throw(_("{0} cannot refund in foreign currency {1}.").format(mode, req_ccy))
			if amount in (None, ""):
				if pay.get("exchange_rate") and (pay.get("currency") or "").upper() == req_ccy:
					fx_refund = _round_currency(left_dec / _money(pay.exchange_rate), fx_places)
					if fx_refund <= 0:
						frappe.throw(_("Refund amount must be positive."))
				else:
					frappe.throw(_("Give the foreign currency amount to refund."))
			else:
				fx_refund = float(_parse_currency_amount(amount, fx_places))
			if pay.get("exchange_rate") and (pay.get("currency") or "").upper() == req_ccy:
				rate = float(pay.exchange_rate)
				rate_date = pay.get("exchange_rate_date")
				home_amt = fx_to_home(fx_refund, rate, get_currency_precision(home))
			else:
				r = payment_rate(doc.property, req_ccy, fx_refund)
				rate = r["rate"]
				rate_date = r["rate_date"]
				home_amt = r["home_amount"]
			if _money(home_amt) > left_dec:
				frappe.throw(_("Only {0} of this payment is left to refund.").format(
					money_text(doc.property, float(left_dec))))
			fx_meta = {
				"currency": req_ccy,
				"fx_amount": -fx_refund,
				"exchange_rate": rate,
				"exchange_rate_date": rate_date,
			}
			amount = home_amt
		else:
			if amount in (None, ""):
				amount_dec = left_dec
			else:
				amount_dec = _parse_currency_amount(amount, raw_places)
			if amount_dec > left_dec:
				frappe.throw(_("Only {0} of this payment is left to refund.").format(
					money_text(doc.property, float(left_dec))))
			amount = float(amount_dec)
			if not mode:
				mode = (
					default_mode_for_tender(doc.property, "Cash")
					if pay_tender == "Foreign Cash"
					else pay.mode
				)
	elif foreign:
		if amount in (None, "") or not mode:
			frappe.throw(_("Give the amount and the mode to refund."))
		if payment_tender_type(mode, doc.property) not in FOREIGN_CURRENCY_TENDER_TYPES:
			frappe.throw(_("{0} cannot refund in foreign currency {1}.").format(mode, req_ccy))
		fx_places = _minor_unit(req_ccy)
		fx_refund = float(_parse_currency_amount(amount, fx_places))
		r = payment_rate(doc.property, req_ccy, fx_refund)
		fx_meta = {
			"currency": r["currency"],
			"fx_amount": -fx_refund,
			"exchange_rate": r["rate"],
			"exchange_rate_date": r["rate_date"],
		}
		amount = r["home_amount"]

	if amount in (None, "") or not mode:
		frappe.throw(_("Give the amount and the mode to refund."))
	if not foreign and payment_tender_type(mode, doc.property) == "Foreign Cash":
		frappe.throw(_("Foreign Cash refund needs the foreign currency of the notes returned."))
	return _refund_folio_payment(folio, amount, mode, reason, pin=pin,
	                             reason_code=reason_code,
	                             supervisor_pin=supervisor_pin,
	                             refund_of=payment_row, idempotency_key=key,
	                             fx=fx_meta)


def _refund_folio_payment(folio: str, amount: float, mode: str, reason: str,
                          pin: str | None = None,
                          reason_code: str | None = None,
                          supervisor_pin: str | None = None,
                          ledger: str | None = None,
                          refund_of: str | None = None,
                          idempotency_key: str | None = None, correction: dict | None = None,
                          bookkeeping_action: str | None = None, session: str | None = None,
                          allow_closed: bool = False,
                          fx: dict | None = None):
	"""The one refund writer (folio row, till, ledger). Not whitelisted:
	ledger lets a security-deposit refund release the Deposit ledger -
	callers that pass it have already checked the deposit's balance.

	A pre-arrival Advance still sitting in Deposit is refunded there too
	(#343), not onto Guest, and Reservation.advance_paid is reduced."""
	if float(amount) <= 0:
		frappe.throw("Refund amount must be positive.")
	if not (reason or "").strip() and not reason_code:
		frappe.throw("A refund reason is required.")
	_pin_guard(folio, pin)
	doc = frappe.get_doc("Folio", folio)
	if doc.status == "Closed" and not (allow_closed and ledger == "Deposit"):
		frappe.throw("This folio is closed - refunds need a credit note "
		             "via allowance on a new folio.")
	from kamra.cashier import assert_cashier_mode_allowed, record_cashier_txn, require_open_session
	if bookkeeping_action is None:
		assert_cashier_mode_allowed(doc.property, mode)
		from kamra.masters import payment_tender_type
		if payment_tender_type(mode, doc.property) == "Foreign Cash" and not fx and not (correction or {}).get("fx_amount"):
			frappe.throw(_("Foreign Cash refund needs the foreign currency of the notes returned."))
	from decimal import ROUND_HALF_UP, Decimal
	from kamra.ledger import (
		_currency_places,
		_money,
		_parse_currency_amount,
		_round_currency,
		require_reason,
	)
	from kamra.pricing import _minor_unit
	raw_places = _currency_places(doc.property)
	places = max(raw_places, 2)
	if bookkeeping_action is None:
		amount_dec = _parse_currency_amount(amount, raw_places)
		amount = float(amount_dec)
	else:
		amount_dec = _money(amount)
	reason_text = require_reason(
		"Refund", reason_code, reason or "",
		supervisor_pin=supervisor_pin, property=doc.property)
	unit_calc = Decimal(1).scaleb(-places)
	if not ledger:
		from kamra.folio import _security_deposit_row_ids
		sec_idx = _security_deposit_row_ids(doc)
		received = sum(
			(_money(p.amount) for idx, p in enumerate(doc.payments)
			 if idx not in sec_idx and _money(p.amount) > 0),
			Decimal(0),
		)
		refunded = -sum(
			(_money(p.amount) for idx, p in enumerate(doc.payments)
			 if idx not in sec_idx and _money(p.amount) < 0),
			Decimal(0),
		)
		collected_left = (received - refunded).quantize(unit_calc, rounding=ROUND_HALF_UP)
		if amount_dec > collected_left:
			from kamra.localization import money_text
			frappe.throw(f"Only {money_text(doc.property, float(collected_left))} was collected on "
			             "this folio - can't refund more than that.")
	# money goes back only when the guest is in credit; a guest who still
	# owes settles first. A security deposit release (ledger set) has its
	# own balance check in kamra.deposit.
	balance_dec = _money(doc.balance).quantize(unit_calc, rounding=ROUND_HALF_UP)
	if not ledger and (balance_dec >= 0 or amount_dec > -balance_dec):
		from kamra.localization import money_text
		if balance_dec >= 0:
			frappe.throw(_("The guest owes {0} - nothing is due back to refund.").format(
				money_text(doc.property, max(0.0, float(balance_dec)))))
		frappe.throw(_("Only {0} is due back to the guest - can't refund more than that.").format(
			money_text(doc.property, float(-balance_dec))))
	# Pre-arrival advance: release Deposit liability, not Guest (#343)
	release_advance = False
	if not ledger:
		ledger = _prearrival_advance_refund_ledger(doc)
		release_advance = ledger == "Deposit"
	from kamra.business_date import get_business_date
	bd = get_business_date(doc.property)
	from kamra.localization import default_currency, pack_for
	currency = (frappe.get_cached_value("Property", doc.property, "currency")
	            or default_currency(pack_for(doc.property)))
	sess = session
	if correction is None:
		try:
			sess = require_open_session(doc.property)
		except Exception:
			if "Kamra Agent" not in frappe.get_roles() and \
			   frappe.session.user != "Administrator":
				raise
	doc.append("payments", {
		"posting_date": bd,
		"payment_kind": "Refund",
		"mode": mode,
		"amount": -float(amount),
		"reference": str(reason_text)[:140],
		"currency": currency,
		"refund_of": refund_of,
		"idempotency_key": idempotency_key,
		**(fx or {}),
		**(correction or {}),
	})
	prev_grand = doc.get("grand_total")
	from kamra.folio import _recalculate
	_recalculate(doc, payments_only=True)
	doc.save()
	pay = doc.payments[-1]
	fx_amt_val = float(pay.get("fx_amount") or 0)
	fx_d = _minor_unit(pay.get("currency"))
	fx_note = (
		f"{pay.currency} {abs(fx_amt_val):,.{fx_d}f} @ {pay.exchange_rate}"
		if fx_amt_val and pay.get("exchange_rate") else None
	)
	if sess or correction is None:
		record_cashier_txn(
			doc.property, "Refund", mode, -float(amount),
			folio=doc.name, reference=str(reason_text)[:140], notes=fx_note,
			session=sess, tender_validated=bool(fx or correction),
			currency=pay.get("currency"), fx_amount=fx_amt_val)
	try:
		from kamra import ledger as _ledger_mod
		_ledger_mod.record_payment_ledger(doc, pay.as_dict(), session=sess, ledger=ledger)
		if (
			prev_grand is not None
			and abs(float(doc.grand_total or 0) - float(prev_grand)) > 1e-9
			and hasattr(_ledger_mod, "sync_folio_rounding_ledger")
		):
			_ledger_mod.sync_folio_rounding_ledger(doc, session=sess)
	except Exception:
		if correction:
			raise
		frappe.log_error(title="ledger refund write failed")
	if bookkeeping_action:
		from kamra.savings import log_action
		log_action(bookkeeping_action, "Folio", doc.name, doc.property,
		           rationale=f"{refund_of}: {amount} {mode} ({reason_text})")
		return {"ok": True, "balance": doc.balance, "reversal": pay.name}
	from kamra.webhooks import emit_payment
	emit_payment("folio.refunded", doc, pay)
	if ledger == "Deposit":  # a security deposit, or an advance still held
		emit_payment("deposit.refunded", doc, pay)
	if release_advance and doc.reservation:
		paid = frappe.db.get_value(
			"Reservation", doc.reservation, "advance_paid") or 0
		frappe.db.set_value(
			"Reservation", doc.reservation, "advance_paid",
			max(0.0, _round_currency(_money(paid) - amount_dec, places)))
	if ledger != "Deposit":  # a deposit refund logs itself (kamra.deposit)
		from kamra.localization import money_text
		from kamra.savings import log_action
		log_action("folio_refund", "Folio", doc.name, doc.property,
		           rationale=f"Refund {money_text(doc.property, amount)} by {mode}: "
		                     f"{str(reason_text)[:100]}")
	elif release_advance or correction:
		from kamra.localization import money_text
		from kamra.savings import log_action
		log_action("folio_refund", "Folio", doc.name, doc.property,
		           rationale=f"Advance refund {money_text(doc.property, amount)} "
		                     f"by {mode}: {str(reason_text)[:100]}")
	return {"ok": True, "balance": doc.balance, "refund": pay.name}


def _prearrival_advance_refund_ledger(doc) -> str | None:
	"""Deposit when a pre-arrival Advance is still held there; else None
	(record_payment_ledger defaults Refund → Guest)."""
	if not doc.reservation:
		return None
	status = frappe.db.get_value("Reservation", doc.reservation, "status")
	if status not in ("Confirmed", "Held", "Pending Payment", "Waitlist"):
		return None
	held = float(frappe.db.sql(
		"""SELECT COALESCE(SUM(credit - debit), 0)
		   FROM `tabFolio Ledger Entry`
		   WHERE folio = %s AND ledger = 'Deposit'""",
		(doc.name,))[0][0] or 0)
	return "Deposit" if held > 0 else None


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def set_actual_times(reservation: str, actual_check_in: str | None = None,
                     actual_check_out: str | None = None):
	"""Correct the recorded arrival/departure moments - early check-ins
	and late checkouts should show what actually happened, not just when
	the button was pressed. Early/late charges stay explicit folio lines."""
	from frappe.utils import get_datetime
	updates = {}
	if actual_check_in:
		updates["actual_check_in"] = get_datetime(actual_check_in)
	if actual_check_out:
		updates["actual_check_out"] = get_datetime(actual_check_out)
	if not updates:
		frappe.throw("Nothing to update.")
	frappe.db.set_value("Reservation", reservation, updates)
	from kamra.savings import log_action
	res = frappe.db.get_value("Reservation", reservation,
	                          ["property", "guest_name"], as_dict=True)
	log_action("set_actual_times", "Reservation", reservation, res.property,
	           rationale=f"Actual times corrected for {res.guest_name}: "
	                     + ", ".join(f"{k}={v}" for k, v in updates.items()))
	return {"ok": True, **{k: str(v) for k, v in updates.items()}}


_FNB_WORDS = ("food", "dinner", "lunch", "breakfast", "meal", "restaurant",
              "f&b", "fnb", "beverage", "snack", "room service", "cafe",
              "kitchen", "buffet", "minibar", "mini bar")


def _check_charge_amount_and_tax(property: str, amount, gst_rate):
	"""A posted charge is a positive amount taxed at one of the country
	pack's rates. Reductions go through allowances and voids, not through a
	minus charge, and an unknown rate would put made-up tax on the bill."""
	try:
		amount = float(amount)
	except (TypeError, ValueError):
		frappe.throw("Charge amount must be a number.")
	if not math.isfinite(amount):
		frappe.throw("Charge amount must be a number.")
	if not amount > 0:
		frappe.throw("Charge amount must be more than zero. To reduce a "
		             "bill, post an allowance or void the charge.")
	from kamra.localization import pack_for
	allowed = [float(r) for r in pack_for(property).tax_rate_options(property)]
	if float(gst_rate or 0) not in allowed:
		frappe.throw("Tax rate {0}% is not allowed. Use one of: {1}.".format(
			f"{float(gst_rate or 0):g}",
			", ".join(f"{r:g}" for r in allowed)))


def _resolve_charge_gst(property: str, charge_type: str, description: str,
                        is_alcohol: int, provided: float) -> float:
	"""The tax rate a charge actually carries, decided server-side so a caller
	(human or agent) cannot mis-tax it. Under the ERP accounting bridge, folio
	lines carry zero tax because ERPNext's tax templates calculate tax on the
	Sales Invoice (KMR-TAX-001). Off the bridge, food & beverage uses the F&B
	rate from the localization pack and services/alcohol use the configured
	services rate."""
	from kamra.fiscal import fiscal_bridge

	if fiscal_bridge():
		return 0.0
	text = f"{charge_type} {description}".lower()
	if is_alcohol:
		if provided:
			return float(provided)
		from kamra.localization import pack_for, services_tax_rate
		return services_tax_rate(pack_for(property), property)
	if any(w in text for w in _FNB_WORDS):
		from kamra.folio import _fnb_gst
		return float(_fnb_gst(property))
	# a taxi is a taxable guest service: the desk form sends no rate
	if charge_type == "Taxi" and not provided:
		from kamra.localization import pack_for, services_tax_rate
		return services_tax_rate(pack_for(property), property)
	return float(provided or 0)


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Kamra Agent")
def void_folio_charge(folio: str, charge_row: str, reason: str = "",
                      pin: str | None = None,
                      reason_code: str | None = None,
                      supervisor_pin: str | None = None):
	"""Remove a wrong charge line from an open folio (the bill-correction
	path): the line stays and a signed reversal line with the reason and
	user follows it (folio.void_charge). PIN-guarded like other money
	actions for humans; agents are accountable through the action log."""
	_pin_guard(folio, pin)
	prop = frappe.db.get_value("Folio", folio, "property")
	from kamra.ledger import require_reason
	reason_text = require_reason(
		"Void", reason_code, reason or "",
		supervisor_pin=supervisor_pin, property=prop)
	from kamra.folio import void_charge
	return void_charge(folio, charge_row, str(reason_text))


# lines a correction may not touch: a room night and its levy change through
# the stay's rate (Kamra posts the rate adjustment), the rest are adjustments
_UNCORRECTABLE = ("Room", "Room Levy", "Rate Adjustment", "Allowance",
                  "Discount", "Opening Balance")


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Front Desk", "Kamra Agent")
def correct_folio_charge(folio: str, charge_row: str, reason: str = "",
                         amount: float | None = None,
                         gst_rate: float | None = None,
                         charge_type: str | None = None,
                         description: str | None = None,
                         pin: str | None = None,
                         reason_code: str | None = None,
                         supervisor_pin: str | None = None,
                         idempotency_key: str | None = None):
	"""Correct a posted line on an open folio without editing it: a reversal
	of the original and a repost with the right amount, tax rate or charge
	type, both linked to the original (corrects_row). Same roles, PIN and
	Void reason codes as void_folio_charge. A retry with the same
	idempotency_key returns the first correction."""
	key = (idempotency_key or "").strip() or None
	frappe.db.get_value("Folio", folio, "name", for_update=True)
	doc = frappe.get_doc("Folio", folio)
	if key:
		done = [c for c in doc.charges if c.get("idempotency_key") == key]
		if done:
			if done[0].corrects_row != charge_row:
				frappe.throw(_("This idempotency_key was already used for "
				               "another correction."))
			return {"ok": True, "idempotent_replay": 1, "folio": doc.name,
			        "reversal": done[0].name, "repost": done[-1].name,
			        "balance": doc.balance}
	_pin_guard(folio, pin)
	if doc.status == "Closed":
		frappe.throw(_("This folio is settled - issue a credit note, or "
		               "cancel the invoice first."))
	orig = next((c for c in doc.charges if c.name == charge_row), None)
	if not orig:
		frappe.throw(_("That charge line is not on this folio."))
	from kamra.folio import void_pairs
	if orig.name in void_pairs(doc):
		frappe.throw(_("This line was voided or split. Correct the line that stands "
		               "in its place."))
	if orig.corrects_row:
		frappe.throw(_("This line is itself a correction. Correct the "
		               "reposted line instead."))
	if any(c.corrects_row == orig.name for c in doc.charges):
		frappe.throw(_("This line was already corrected. Correct the "
		               "reposted line instead."))
	if orig.charge_type in _UNCORRECTABLE or float(orig.amount or 0) <= 0:
		frappe.throw(_("A {0} line can't be corrected here. Room nights change "
		               "through the stay's rate; reverse an allowance or "
		               "discount with a charge.").format(_(orig.charge_type)))
	from kamra.ledger import _original_charge_entry, record_charge_ledger, require_reason
	reason_text = str(require_reason(
		"Void", reason_code, reason or "",
		supervisor_pin=supervisor_pin, property=doc.property))
	new_type = charge_type or orig.charge_type
	new_desc = (description or "").strip() or orig.description
	new_amount = float(orig.amount) if amount in (None, "") else float(amount)
	_refuse_opening_balance(new_type)
	if new_type in _UNCORRECTABLE:
		frappe.throw(_("A correction can't repost as {0}.").format(_(new_type)))
	is_alcohol = int(orig.is_alcohol or 0)
	if is_alcohol and doc.folio_type in ("Company", "Group"):
		frappe.throw(_("Alcohol cannot be billed to a company folio."))
	rate_in = float(orig.gst_rate or 0) if gst_rate in (None, "") else float(gst_rate)
	_check_charge_amount_and_tax(doc.property, new_amount, rate_in)
	new_rate = _resolve_charge_gst(doc.property, new_type, new_desc,
	                               is_alcohol, rate_in)
	if not float(new_rate or 0):
		from kamra.pricing import property_currency, round_rate_amount
		new_amount = float(round_rate_amount(new_amount, 0, "Nearest", property_currency(doc.property)))
		if new_amount <= 0:
			frappe.throw("Charge amount must be more than zero. To reduce a "
			             "bill, post an allowance or void the charge.")
	from kamra.ledger import _currency_places, _round_currency
	places = max(_currency_places(doc.property), 2)
	if (new_type, new_desc, _round_currency(new_amount, places), new_rate) == (
			orig.charge_type, orig.description, _round_currency(orig.amount, places),
			float(orig.gst_rate or 0)):
		frappe.throw(_("Nothing to correct: the line already has these values."))
	from kamra.business_date import get_business_date
	bd = get_business_date(doc.property)
	common = {"posting_date": bd, "stay_date": orig.stay_date,
	          "reservation": orig.reservation, "is_alcohol": is_alcohol,
	          "corrects_row": orig.name, "idempotency_key": key, "qty": 1}
	# the reversal books back at the codes the original was posted with
	doc.append("charges", {
		**common, "charge_type": orig.charge_type,
		"transaction_code": orig.transaction_code, "gl_code": orig.gl_code,
		"description": f"Reversal: {orig.description}"[:140],
		"rate": -float(orig.amount), "amount": -float(orig.amount),
		"gst_rate": float(orig.gst_rate or 0),
		# a line split to the minor unit is reversed with the tax it was posted with
		"tax_as_posted": orig.get("tax_as_posted"),
		"gst_amount": -float(orig.gst_amount or 0),
	})
	doc.append("charges", {
		**common, "charge_type": new_type, "description": new_desc,
		"rate": new_amount, "amount": new_amount, "gst_rate": new_rate,
	})
	from kamra.folio import _recalculate
	_recalculate(doc)
	doc.save(ignore_permissions=True)
	rev, rep = doc.charges[-2], doc.charges[-1]
	first = _original_charge_entry(doc.name, orig.name)
	record_charge_ledger(doc, rev.as_dict(), reversal_of=first.name if first else None)
	record_charge_ledger(doc, rep.as_dict())
	import kamra.ledger as _ledger_mod
	if hasattr(_ledger_mod, "sync_folio_rounding_ledger"):
		_ledger_mod.sync_folio_rounding_ledger(doc)
	from kamra.webhooks import emit_folio_posted
	emit_folio_posted(doc, rev)
	emit_folio_posted(doc, rep)
	from kamra.localization import money_text
	from kamra.savings import log_action
	log_action("correct_charge", "Folio", doc.name, doc.property,
	           rationale=f"Corrected {orig.charge_type} {orig.description} "
	                     f"{money_text(doc.property, orig.amount)} to {new_type} "
	                     f"{money_text(doc.property, new_amount)} at {new_rate:g}%: "
	                     f"{reason_text[:100]}",
	           before_snapshot={"row": orig.name, "charge_type": orig.charge_type,
	                            "amount": orig.amount, "gst_rate": orig.gst_rate},
	           after_snapshot={"reversal": rev.name, "repost": rep.name})
	return {"ok": True, "folio": doc.name, "reversal": rev.name,
	        "repost": rep.name, "balance": doc.balance}


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Kamra Agent")
def post_stay_charge(reservation: str, charge_type: str, description: str,
                     amount: float, gst_rate: float = 0, is_alcohol: int = 0):
	"""Post a charge to a stay letting the billing rules pick the folio -
	corporate room/meals land on the Company folio, alcohol and anything
	unruled lands on the guest. The agent-facing way to post charges."""
	res = frappe.get_doc("Reservation", reservation)
	if res.status not in ("Confirmed", "Checked In", "Checked Out"):
		frappe.throw("Reservation is not active.")
	from kamra.folio import target_folio
	is_alcohol = 1 if int(is_alcohol or 0) else 0
	# check what the caller sent, not the rate the hotel picks below
	_check_charge_amount_and_tax(res.property, amount, gst_rate)
	# GST is decided by the hotel's tax rules, not by the caller's guess:
	# food & beverage carries the F&B rate (5% in India), not 12/18.
	gst_rate = _resolve_charge_gst(res.property, charge_type, description,
	                               is_alcohol, gst_rate)
	folio_name = target_folio(res, charge_type, is_alcohol)
	out = _post_folio_charge(folio_name, charge_type, description, amount,
	                         gst_rate, is_alcohol=is_alcohol,
	                         reservation=res.name)
	return {"folio": folio_name, "folio_type": out.get("folio_type"),
	        "balance": out.get("balance")}


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Kamra Agent")
def set_billing_rules(company: str, rules):
	"""Replace a company's billing rules. rules = [{charge_type, pay_by}]."""
	if isinstance(rules, str):
		rules = frappe.parse_json(rules)
	doc = frappe.get_doc("Corporate Account", company)
	doc.set("billing_rules", [])
	for r in rules or []:
		doc.append("billing_rules", {
			"charge_type": r.get("charge_type"),
			"pay_by": r.get("pay_by") or "Company",
		})
	doc.save()
	return {"company": company, "rules": len(doc.billing_rules)}


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Kamra Agent")
def get_billing_rules(company: str):
	doc = frappe.get_doc("Corporate Account", company)
	return [{"charge_type": r.charge_type, "pay_by": r.pay_by}
	        for r in (doc.get("billing_rules") or [])]


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Kamra Agent")
def company_billing_default(company: str, property: str | None = None):
	"""What this company pays when it has no billing rules: the property's
	`default_company_billing` for a credit company, nothing otherwise.
	`charge_types` is None when every charge type (except liquor) is covered."""
	from kamra.folio import COMPANY_BILLING_DEFAULTS, default_company_billing
	if property:
		from kamra.crs import assert_property_access
		assert_property_access(property)
	doc = frappe.get_doc("Corporate Account", company)
	has_rules = bool(doc.get("billing_rules"))
	label = default_company_billing(property)
	covered = COMPANY_BILLING_DEFAULTS[label]
	return {
		"company": company,
		"credit_allowed": bool(doc.credit_allowed),
		"has_rules": has_rules,
		"default": label,
		"charge_types": None if covered is None else sorted(covered),
		"applies": bool(doc.credit_allowed) and not has_rules,
	}


def split_names(names) -> list[str]:
	"""Names from a list, or one string separated by ; , or new lines."""
	if isinstance(names, str):
		import re
		names = re.split(r"[;,\n]", names)
	out = []
	for n in names or []:
		n = (n or "").strip()[:140]
		if n and n.lower() not in {x.lower() for x in out}:
			out.append(n)
	return out


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def add_occupant_names(reservation: str, names):
	"""Add the other people staying to the stay's occupant register by name
	(at booking, before anyone's ID is seen). Names already on the register
	are left as they are; age and ID are filled at check-in."""
	doc = frappe.get_doc("Reservation", reservation)
	have = {(o.full_name or "").strip().lower() for o in doc.get("occupants") or []}
	have.add((doc.guest_name or "").strip().lower())
	added = 0
	for n in split_names(names)[:20]:
		if n.lower() not in have:
			doc.append("occupants", {"full_name": n})
			have.add(n.lower())
			added += 1
	if added:
		doc.save()
	return {"reservation": reservation, "added": added}


@frappe.whitelist()
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def update_occupants(reservation: str, occupants, confirm_price: int = 0):
	"""Replace the stay's occupant register.
	occupants = [{full_name, age, gender, nationality, id_type, id_number, phone,
	              arrived_at, left_at}]
	Guests sharing a room can arrive and leave at different times: see
	_occupant_times for how arrived_at / left_at are kept.
	A child keeps name and age only: no phone, and ID details only where the
	country's register needs them (see localization.minor_id_kept).
	If occupant ages change billable_children (affecting room, meal-plan or
	extension pricing), only Revenue Manager / Hotel Admin may save it, and
	a change in the stay total requires confirm_price=1."""
	if isinstance(occupants, str):
		occupants = frappe.parse_json(occupants)
	doc = frappe.get_doc("Reservation", reservation)
	from kamra.localization import is_minor, minor_id_kept, pack_for, store_id_number, validate_id_type
	from kamra.pricing import billable_children
	old_billable = billable_children(doc)
	previous_occupants = list(doc.get("occupants") or [])
	pack = pack_for(doc.property)
	default_nat = getattr(pack, "DEFAULT_NATIONALITY", "")
	# scans survive edits even when the client sends stale rows: carry the
	# stored id_file for any row that comes back without one
	existing_scans = {o.name: o.get("id_file")
	                  for o in (doc.get("occupants") or [])}
	existing_types = {o.name: o.get("id_type")
	                  for o in (doc.get("occupants") or [])}
	stated_ages = {o.name: o.get("guest_stated_age")
	               for o in (doc.get("occupants") or [])}
	existing_photos = {o.name: o.get("photo")
	                   for o in (doc.get("occupants") or [])}
	carried = ("full_name", "age", "gender", "nationality", "id_type", "id_number", "phone")
	existing_rows = {o.name: {k: o.get(k) for k in carried}
	                 for o in (doc.get("occupants") or [])}
	# who arrived (or left) when: kept for rows the client sends back without
	# it; a guest added to a stay already in house arrived now
	existing_times = {o.name: (o.get("arrived_at"), o.get("left_at"))
	                  for o in (doc.get("occupants") or [])}
	# a client that replaces the list without row ids (an agent) still
	# keeps each guest's times: match by name
	times_by_name = {(o.full_name or "").strip().casefold(): existing_times[o.name]
	                 for o in (doc.get("occupants") or [])}
	in_house_now = now_datetime().replace(microsecond=0) \
		if doc.status == "Checked In" else None
	doc.set("occupants", [])
	stale_scans, minors_trimmed = [], 0
	for o in occupants or []:
		# a saved row keeps what the client left out, and the stored ID
		# number when the client only has the masked one (check-in shows it so)
		prev = existing_rows.get(o.get("row")) or {}
		o = {**prev, **{k: v for k, v in o.items() if v is not None}}
		stored = store_id_number(pack, o.get("id_type"), o.get("id_number"))
		if stored is not None:
			o["id_number"] = stored
		elif "•" in str(o.get("id_number") or ""):
			if not prev:
				# a masked number for a row that is gone: saving it would wipe the ID
				frappe.throw(_("The guest list changed since you opened it. Reload and save again."))
			o["id_number"] = prev.get("id_number") or ""
		if not (o.get("full_name") or "").strip():
			continue
		nationality = o.get("nationality") or default_nat
		row = {
			"full_name": o["full_name"].strip(),
			"age": o.get("age") or None,
			"gender": o.get("gender") or "",
			"nationality": nationality,
			"id_type": o.get("id_type") or "",
			"id_number": (o.get("id_number") or "").strip(),
			"phone": o.get("phone") or "",
			# an uploaded ID survives a register edit
			"id_file": o.get("id_file")
			           or existing_scans.get(o.get("row")) or None,
			"guest_stated_age": stated_ages.get(o.get("row")) or None,
			"photo": existing_photos.get(o.get("row")) or None,
		}
		row.update(_occupant_times(
			o, existing_times.get(o.get("row"))
			or times_by_name.get(row["full_name"].casefold()), in_house_now))
		if is_minor(pack, row["age"]):
			# a child's phone is never needed; ID only where the register asks
			trim = {"phone": ""} if minor_id_kept(pack, nationality) else \
			       {"phone": "", "id_type": "", "id_number": "", "id_file": None}
			if any(row[k] for k in trim):
				minors_trimmed += 1
				if row["id_file"] and "id_file" in trim:
					stale_scans.append(row["id_file"])
			row.update(trim)
		if row["id_type"] and row["id_type"] != existing_types.get(o.get("row")):
			validate_id_type(doc.property, row["id_type"])
		doc.append("occupants", row)
	new_billable = billable_children(doc)
	if new_billable != old_billable:
		if not _can_set_rate():
			doc.set("occupants", previous_occupants)
			frappe.throw(
				_("Changing occupant ages that affect the stay rate requires Revenue Manager or Hotel Admin."),
				frappe.PermissionError,
			)
		preview = _reprice_preview(doc)
		if preview["needs_confirmation"] and not cint(confirm_price):
			doc.set("occupants", previous_occupants)
			_require_price_confirmation(preview, confirm_price)
	doc.save(ignore_permissions=_can_set_rate())
	# a row taken off the register takes its ID scan and photo with it
	kept = {o.get("id_file") for o in doc.occupants} | {o.get("photo") for o in doc.occupants}
	stale_scans += [url for url in list(existing_scans.values()) + list(existing_photos.values())
	                if url and url not in kept]
	if stale_scans:
		from kamra.privacy import _delete_files
		for url in set(stale_scans):
			_delete_files("Reservation", reservation, url=url)
	return {"reservation": reservation, "occupants": len(doc.occupants),
	        "minors_trimmed": minors_trimmed,
	        "rows": [
	            {"row": o.name, "full_name": o.full_name, "age": o.age or None,
	             "gender": o.gender, "nationality": o.nationality,
	             "id_type": o.id_type, "id_number": o.id_number,
	             "phone": o.phone, "id_file": o.get("id_file"),
	             "photo": o.get("photo"),
	             "arrived_at": _dt(o.get("arrived_at")),
	             "left_at": _dt(o.get("left_at"))}
	            for o in doc.occupants]}


def _dt(v) -> str | None:
	return str(v)[:19] if v else None


def _occupant_times(o: dict, existing, in_house_now) -> dict:
	"""arrived_at / left_at for one register row. A value the client sends
	wins (the desk's "arrived now", or a corrected time; an empty one clears
	it); a row sent back without the key keeps what it had; a new row on a
	stay already in house arrived now."""
	had_in, had_out = existing or (None, None)
	out = {}
	for key, had in (("arrived_at", had_in), ("left_at", had_out)):
		out[key] = (get_datetime(o[key]) if o.get(key) else None) if key in o else had
	if out["arrived_at"] is None and existing is None:
		out["arrived_at"] = in_house_now
	if out["arrived_at"] and out["left_at"] and out["left_at"] < out["arrived_at"]:
		frappe.throw(_("{0} cannot leave before they arrived.").format(
			o.get("full_name") or _("A guest")))
	return out


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Kamra Agent")
def split_folio(reservation: str, folio_type: str = "Extra"):
	from kamra.folio import split_folio as _split
	return {"folio": _split(reservation, folio_type)}


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Front Desk", "Kamra Agent")
def delete_folio(folio: str):
	"""Remove an empty split/extra folio created by mistake.

	Guards: never the primary Guest folio, and only when it carries no
	charges and no payments - money is never dropped this way.
	"""
	doc = frappe.get_doc("Folio", folio)
	if doc.folio_type == "Guest":
		frappe.throw("The primary guest folio can't be deleted.")
	if doc.get("charges") or doc.get("payments"):
		frappe.throw(
			"This folio has charges or payments - move them to another folio first."
		)
	if doc.docstatus == 1:
		doc.cancel()
	frappe.delete_doc("Folio", folio, ignore_permissions=True, force=True)
	return {"deleted": folio}


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Kamra Agent")
def transfer_folio_charge(from_folio: str, charge_row: str, to_folio: str):
	from kamra.folio import transfer_charge
	transfer_charge(from_folio, charge_row, to_folio)
	from kamra.savings import log_action
	log_action("transfer_charge", "Folio", to_folio,
	           rationale=f"Moved charge {charge_row} {from_folio} → {to_folio}")
	return {"ok": True}


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Kamra Agent")
def transfer_folio_charges(from_folio: str, charge_rows, to_folio: str | None = None,
                           to_company: int = 0, to_reservation: str | None = None,
                           reason: str | None = None,
                           idempotency_key: str | None = None):
	"""Move charge lines off one open folio, ledger and all. Give one target:
	to_folio (another folio of the same stay or group), to_company=1 (the
	stay's Company folio, opened if needed) or to_reservation (the open Guest
	folio of another in-house stay here; Finance or Hotel Admin, with a
	reason). A retry with the same idempotency_key returns the first move."""
	if isinstance(charge_rows, str):
		charge_rows = frappe.parse_json(charge_rows)
	charge_rows = list(dict.fromkeys(charge_rows or []))
	if not charge_rows:
		frappe.throw(_("Pick the charges to move."))
	key = (idempotency_key or "").strip() or None
	frappe.db.get_value("Folio", from_folio, "name", for_update=True)
	if key:
		done = frappe.get_all("Folio Charge", pluck="parent", filters={
			"parenttype": "Folio", "idempotency_key": key})
		if done:
			return {"ok": True, "idempotent_replay": 1, "moved": len(done),
			        "to_folio": done[0]}
	if sum((bool(to_folio), bool(cint(to_company)), bool(to_reservation))) != 1:
		frappe.throw(_("Give one target: to_folio, to_company or to_reservation."))
	from kamra.folio import _assert_same_stay, move_charges, open_company_folio
	src = frappe.get_doc("Folio", from_folio)
	new_res = None
	if to_folio:
		dst = frappe.get_doc("Folio", to_folio)
		_assert_same_stay(src, dst)
	elif cint(to_company):
		res = src.reservation and frappe.get_doc("Reservation", src.reservation)
		if not res or not res.company:
			frappe.throw(_("This stay bills to no company. Set one with "
			               "change_stay_company first."))
		dst = frappe.get_doc("Folio", open_company_folio(res))
	else:
		if not (set(frappe.get_roles()) & {"Finance", "Hotel Admin", "System Manager"}) \
				and frappe.session.user != "Administrator":
			frappe.throw(_("Moving charges to another stay needs Finance or "
			               "Hotel Admin."), frappe.PermissionError)
		if not (reason or "").strip():
			frappe.throw(_("Give a reason to move charges to another stay."))
		res = frappe.db.get_value("Reservation", to_reservation,
		                          ["name", "property", "status"], as_dict=True)
		if not res or res.property != src.property:
			frappe.throw(_("That stay is not at this property."))
		if res.status != "Checked In":
			frappe.throw(_("Charges move only to a stay that is checked in."))
		if res.name == src.reservation:
			frappe.throw(_("That is the same stay. Use to_folio."))
		target = frappe.db.get_value("Folio", {
			"reservation": res.name, "folio_type": "Guest", "status": "Open"})
		if not target:
			frappe.throw(_("That stay has no open guest folio."))
		dst = frappe.get_doc("Folio", target)
		new_res = res.name
	if dst.name == src.name:
		frappe.throw(_("Pick a different folio to move the charges to."))
	moved = move_charges(src, dst, charge_rows, reservation=new_res, key=key)
	from kamra.savings import log_action
	log_action("transfer_charges", "Folio", dst.name, src.property,
	           rationale=f"Moved {len(moved)} charges {src.name} → {dst.name}"
	                     + (f": {reason.strip()}" if (reason or "").strip() else ""))
	return {"ok": True, "moved": len(moved), "to_folio": dst.name}


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Kamra Agent")
def split_folio_charge(from_folio: str, charge_row: str, to_folio: str,
                       percent: float | None = None,
                       amount: float | None = None, qty: float | None = None):
	"""Split one charge line in two - by percent, amount or quantity - on
	its own folio or onto another of the stay or group."""
	from kamra.folio import split_charge
	out = split_charge(from_folio, charge_row, to_folio,
	                   percent=float(percent) if percent else None,
	                   amount=float(amount) if amount else None,
	                   qty=float(qty) if qty else None)
	from kamra.savings import log_action
	from kamra.localization import money_text
	log_action("split_charge", "Folio", to_folio,
	           rationale=f"Split {money_text(frappe.db.get_value('Folio', to_folio, 'property'), out['moved'])} of {charge_row} "
	                     f"{from_folio} → {to_folio}")
	return {"kept": out["kept"], "moved": out["moved"]}


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Front Desk", "Kamra Agent")
def open_guest_bills(reservation: str, names=None):
	"""A separate bill for each guest sharing the room: one Extra folio per
	other occupant (or per name given), invoiced in that guest's name. The
	main guest keeps the Guest folio."""
	if isinstance(names, str):
		names = frappe.parse_json(names) if names.strip().startswith("[") \
			else [n for n in names.split(",")]
	from kamra.folio import open_guest_bills as _open
	out = _open(reservation, names)
	from kamra.savings import log_action
	log_action("open_guest_bills", "Reservation", reservation,
	           frappe.db.get_value("Reservation", reservation, "property"),
	           rationale="Separate bills for " + ", ".join(b["name"] for b in out))
	return {"bills": out}


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Front Desk", "Kamra Agent")
def share_folio_charges(from_folio: str, charge_rows, to_folios,
                        shares=None):
	"""Share charge lines (room nights) between the guests' bills: evenly,
	or by a percent per other bill (`shares`); the source keeps the rest."""
	charge_rows, to_folios, shares = (
		frappe.parse_json(v) if isinstance(v, str) else v
		for v in (charge_rows, to_folios, shares))
	from kamra.folio import share_charges
	out = share_charges(from_folio, charge_rows or [], to_folios or [],
	                    shares or None)
	from kamra.savings import log_action
	log_action("split_charge", "Folio", from_folio,
	           rationale=f"Shared {out['lines']} charges from {from_folio} "
	                     f"with {', '.join(to_folios)}")
	return out


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Front Desk", "Kamra Agent")
def change_stay_company(reservation: str, reason: str,
                        company: str | None = None, move: str = "rules",
                        override_reason: str | None = None):
	"""Change (or remove) the company a stay bills to, mid-stay or at
	check-out. Charges posted from now on route by the new company's
	billing rules. Charges already posted move too: `move="rules"` puts
	each line where the new company's rules send it (company bill or the
	guest's), `move="guest"` sends everything on the company bill back to
	the guest. Refused once a bill was issued to the old company. Credit
	control runs for the new company. The rate stays as booked (matching
	OPERA Cloud Managing Reservation Linked Profiles,
	https://docs.oracle.com/en/industries/hospitality/opera-cloud/25.1/ocsuh/t_managing_profiles_linking_reservations.htm,
	across both booked and extended nights); who and why go on the record."""
	from kamra.credit import credit_applies, enforce_company_credit
	from kamra.folio import _company_pays, open_company_folio, transfer_charges
	res = frappe.get_doc("Reservation", reservation)
	if res.status in ("Checked Out", "Cancelled", "No Show"):
		frappe.throw(_("This stay is {0}; its company can't change now.").format(
			_(res.status).lower()))
	if not (reason or "").strip():
		frappe.throw(_("Say why the company changes - it goes on the record."))
	if move not in ("rules", "guest"):
		frappe.throw(_("move must be rules or guest."))
	company = (company or "").strip() or None
	old = res.company or None
	if company == old:
		frappe.throw(_("The stay already bills to that company."))
	if company and not frappe.db.exists("Corporate Account",
	                                    {"name": company, "disabled": 0}):
		frappe.throw(_("Pick an active company."))
	company_folios = frappe.get_all(
		"Folio", filters={"reservation": res.name, "folio_type": "Company"},
		fields=["name", "status", "invoice_number", "payments_total"])
	for f in company_folios:
		if f.status == "Closed":
			frappe.throw(_("Invoice {0} was already issued to the old company. "
			               "Cancel that invoice first, then change the company.")
			             .format(f.invoice_number or f.name))
		if flt(f.payments_total):
			frappe.throw(_("The company bill {0} already has payments or was "
			               "moved to the city ledger. Settle that with the old "
			               "company first.").format(f.name))
	booking_type = res.booking_type
	if company and booking_type == "Individual":
		booking_type = "Corporate"
	elif not company and booking_type == "Corporate":
		booking_type = "Individual"
	# db_set, not save: a company change must not re-price the stay.
	# Backfill the original pricing company into legacy contract_tariff snapshots
	# that predate freezing "company" at booking so extensions keep the booked rate.
	from kamra.pricing import saved_contract_tariff
	updates = {"company": company, "booking_type": booking_type}
	snapshot = saved_contract_tariff(res)
	if isinstance(snapshot, dict) and snapshot and "company" not in snapshot:
		updates["contract_tariff"] = json.dumps({**snapshot, "company": old})
	res.db_set(updates)

	def company_pays(c) -> bool:
		return bool(company) and not c.is_alcohol and _company_pays(
			company, c.charge_type, res.property)

	moved = 0
	guest = frappe.db.get_value(
		"Folio", {"reservation": res.name, "folio_type": "Guest",
		          "status": "Open"})
	from kamra.folio import void_pairs

	def live(folio_doc):
		# a voided (or split) line and its reversal net to nothing and stay on
		# the bill they were posted to; what stands in their place moves
		pairs = void_pairs(folio_doc)
		return [c for c in folio_doc.charges if c.name not in pairs]

	for f in company_folios:
		doc = frappe.get_doc("Folio", f.name)
		lines = live(doc)
		# rules: an allowance stays with the bill it wrote down
		back = [c.name for c in lines
		        if move == "guest" or not company
		        or (c.charge_type != "Allowance" and not company_pays(c))]
		# an allowance wrote down the charges on this bill and has no link to
		# one line: it follows the charges that leave, in proportion
		allowances = [c for c in lines
		              if c.charge_type == "Allowance" and c.name not in back]
		out_total = sum(flt(c.amount) for c in lines if c.name in back)
		kept_total = sum(flt(c.amount) for c in lines
		                 if c.charge_type != "Allowance" and c.name not in back)
		if allowances and out_total > 0 and kept_total <= 0:
			back += [c.name for c in allowances]
			allowances = []
		if back:
			if not guest:
				frappe.throw(_("The guest has no open folio to take these charges."))
			transfer_charges(doc.name, back, guest)
			moved += len(back)
		if out_total > 0:
			from kamra.folio import split_charge
			from kamra.ledger import _currency_places, _round_currency
			places = max(_currency_places(doc.property), 2)
			share = out_total / (out_total + kept_total)
			for a in allowances:
				part = _round_currency(abs(flt(a.amount)) * share, places)
				if 0 < part < abs(flt(a.amount)):
					split_charge(doc.name, a.name, guest, amount=part, credit=True)
					moved += 1
	# a group stay's routing is the group's: only its own company bill moves
	if move == "rules" and company and guest and not res.get("group_booking"):
		gdoc = frappe.get_doc("Folio", guest)
		inbound = [c.name for c in live(gdoc)
		           if c.charge_type != "Allowance" and company_pays(c)]
		if inbound:
			transfer_charges(guest, inbound, open_company_folio(res))
			moved += len(inbound)
	credit = None
	if company and credit_applies(company):
		cf = frappe.db.get_value("Folio", {"reservation": res.name,
		                                   "folio_type": "Company",
		                                   "status": "Open"},
		                         ["name", "balance"], as_dict=True)
		credit = enforce_company_credit(
			company, res.property, flt(cf.balance) if cf else 0,
			override_reason=override_reason, context=_("company change"),
			reference_doctype="Reservation", reference_name=res.name,
			exclude_folios=[cf.name] if cf else None)
	from kamra.savings import log_action

	def label(c):
		return (frappe.db.get_value("Corporate Account", c, "company_name") or c) \
			if c else "no company"
	log_action("change_company", "Reservation", res.name, res.property,
	           rationale=f"Company {label(old)} -> {label(company)}: "
	                     f"{reason.strip()[:300]}. {moved} charge lines moved.",
	           channel=agent_channel() or "Desk",
	           before_snapshot={"company": old},
	           after_snapshot={"company": company, "move": move, "moved": moved})
	return {"ok": True, "reservation": res.name, "company": company,
	        "previous_company": old, "moved": moved,
	        "credit_warning": (credit or {}).get("warning")}


_FOLIO_LIST_FIELDS = ["name", "folio_type", "status", "invoice_number",
                      "grand_total", "payments_total", "balance",
                      "bill_to_name"]


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Kamra Agent")
def reservation_folios(reservation: str):
	"""All folios of a stay (guest + splits) with balances - plus the
	group master folio when the stay belongs to a group, so charges can
	be moved between a guest's bill and the company's consolidated one."""
	rows = frappe.get_all(
		"Folio",
		filters={"reservation": reservation},
		fields=_FOLIO_LIST_FIELDS,
		order_by="creation asc",
	)
	stay_company, group = frappe.db.get_value(
		"Reservation", reservation, ["company", "group_booking"]) or (None, None)
	if group:
		seen = {r.name for r in rows}
		rows += [m for m in frappe.get_all(
			"Folio",
			filters={"group_booking": group, "folio_type": "Group"},
			fields=_FOLIO_LIST_FIELDS,
		) if m.name not in seen]
	# name the company each company bill goes to, as get_folio bills it: a
	# group member billed to its own company also sees the group's bill
	group_company = group and frappe.db.get_value("Group Booking", group, "company")
	for r in rows:
		if r.folio_type == "Company":
			r["company"] = stay_company
		elif r.folio_type == "Group":
			r["company"] = group_company or stay_company
	return rows


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Kamra Agent")
def group_master_folio(group_booking: str):
	"""Get-or-create the group's consolidated company folio."""
	from kamra.folio import open_group_folio
	return {"folio": open_group_folio(group_booking)}


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Kamra Agent")
def group_folios(group_booking: str):
	"""The whole group's billing picture: the master folio plus every
	member reservation's folios, with balances."""
	master = frappe.get_all(
		"Folio", filters={"group_booking": group_booking,
		                  "folio_type": "Group"},
		fields=_FOLIO_LIST_FIELDS)
	members = frappe.get_all(
		"Reservation",
		filters={"group_booking": group_booking},
		fields=["name", "guest_name", "room", "status"],
		order_by="creation asc")
	for m in members:
		m["folios"] = [f for f in frappe.get_all(
			"Folio", filters={"reservation": m.name},
			fields=_FOLIO_LIST_FIELDS) if f.folio_type != "Group"]
	return {"master": master[0] if master else None, "members": members}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def set_group_master(group_booking: str, reservation: str):
	"""Make this room the group's main room: its guest pays for the group.
	An open group bill moves with it (re-anchored to the new main room)."""
	gb = frappe.get_doc("Group Booking", group_booking)
	from kamra.authz import assert_property_access
	assert_property_access(gb.property)
	if frappe.db.get_value("Reservation", reservation, "group_booking") != gb.name:
		frappe.throw(_("That reservation is not part of this group."))
	updates = {"master_reservation": reservation}
	if not gb.get("master_pays") or gb.master_pays.startswith("Nothing"):
		updates["master_pays"] = "Room only"
	gb.db_set(updates)
	bill = frappe.db.get_value(
		"Folio", {"group_booking": gb.name, "folio_type": "Group",
		          "status": "Open"})
	if bill:
		frappe.db.set_value("Folio", bill, {
			"reservation": reservation,
			"guest": frappe.db.get_value("Reservation", reservation, "guest")})
	from kamra.savings import log_action
	log_action("group_master", "Group Booking", gb.name, gb.property,
	           rationale=f"Main room set to {reservation}")
	return {"ok": True, "master_reservation": reservation, "master_folio": bill}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def set_pays_own_charges(reservation: str, value: int = 1):
	"""Opt a group room out of the main room's bill (or back in). Affects new
	charges only; move lines already posted with transfer_folio_charges."""
	res = frappe.get_doc("Reservation", reservation)
	from kamra.authz import assert_property_access
	assert_property_access(res.property)
	if not res.get("group_booking"):
		frappe.throw(_("Only a room of a group booking can pay its own charges."))
	value = 1 if int(value or 0) else 0
	res.db_set("pays_own_charges", value)
	return {"ok": True, "reservation": res.name, "pays_own_charges": value}


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Front Desk", "Kamra Agent")
def settle_group_bill(group_booking: str, mode: str | None = None,
                      amount: float | None = None, to_city_ledger: int = 0,
                      reference: str | None = None, pin: str | None = None,
                      credit_override_reason: str | None = None):
	"""Settle the group's master bill: take a payment on it (`mode`, and
	`amount`, default the whole balance) or, for a group whose company has
	credit, move the balance to the city ledger. The bill closes (and is
	invoiced) once nothing is left on it. Allowed while rooms are still in
	house: the reply warns, and their later charges open a new group bill."""
	gb = frappe.get_doc("Group Booking", group_booking)
	from kamra.authz import assert_property_access
	assert_property_access(gb.property)
	folio = frappe.db.get_value(
		"Folio", {"group_booking": gb.name, "folio_type": "Group",
		          "status": "Open"})
	if not folio:
		frappe.throw(_("This group has no open group bill."))
	from kamra.ledger import _currency_places, _round_currency
	places = max(_currency_places(gb.property), 2)
	eps = 0.5 * (10 ** -places)
	balance = _round_currency(frappe.db.get_value("Folio", folio, "balance"), places)
	if int(to_city_ledger or 0):
		if not gb.company:
			frappe.throw(_("Only a company group can go to the city ledger."))
		from kamra.ledger import city_ledger_transfer
		city_ledger_transfer(folio, gb.company, pin=pin,
		                     credit_override_reason=credit_override_reason)
	elif balance > eps:
		if not mode:
			frappe.throw(_("Choose how the group bill is paid."))
		add_folio_payment(folio, mode, flt(amount) or balance,
		                  reference=reference, pin=pin)
	balance = _round_currency(frappe.db.get_value("Folio", folio, "balance"), places)
	out = {"folio": folio, "balance": balance, "closed": False}
	if abs(balance) <= eps:
		out["invoice_number"] = close_folio(folio, pin=pin)["invoice_number"]
		out["closed"] = True
	in_house = frappe.db.count("Reservation", {"group_booking": gb.name,
	                                           "status": "Checked In"})
	if in_house and out["closed"]:
		out["warning"] = _("{0} room(s) of the group are still in house. "
		                   "Their new charges open a new group bill.").format(in_house)
	return out


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Kamra Agent")
def close_folio(folio: str, pin: str | None = None):
	"""Close a settled folio and issue its invoice number.

	The Guest folio of a stay that is still checked in stays open, so
	tonight's charges and the night audit have a bill to post to. Invoice
	what is already paid with part_settle_folio (Settle & continue stay).
	Any other folio, including a company folio, can still close while the
	guest is in house.
	"""
	_pin_guard(folio, pin)
	row = frappe.db.get_value(
		"Folio", folio, ["property", "folio_type", "reservation", "status"], as_dict=True)
	prop = row.property if row else None
	# closing an in-house guest's own bill leaves nothing open for tonight's
	# charges, and the night audit then fails on the frozen bill
	if row and row.status != "Closed" and (row.folio_type or "Guest") == "Guest" \
			and row.reservation and frappe.db.get_value(
				"Reservation", row.reservation, "status") == "Checked In":
		frappe.throw(_("The guest is still in house, so their bill stays open for "
		               "tonight's charges. To invoice what is paid so far, use "
		               "Settle & continue stay."))
	try:
		from kamra.cashier import require_open_session
		require_open_session(prop)
	except Exception:
		if "Kamra Agent" not in frappe.get_roles() and \
		   frappe.session.user != "Administrator":
			raise
	from kamra.folio import close_folio as _close
	was_open = frappe.db.get_value("Folio", folio, "status") != "Closed"
	invoice_number = _close(folio)
	if was_open:
		from kamra.savings import log_action
		log_action("close_folio", "Folio", folio, prop,
		           rationale=f"Folio closed, invoice {invoice_number}")
	return {"invoice_number": invoice_number}


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Front Desk", "Kamra Agent")
def post_allowance(folio: str, amount: float, reason: str = "",
                   gst_rate: float = 0, pin: str | None = None,
                   reason_code: str | None = None):
	"""Write off part of a bill against a specific folio, with a reason.

	When the property wants a manager's approval (Settings, Finance:
	allowance_approval / allowance_approval_above) or the reason code needs
	a supervisor, the allowance does not touch the folio yet: it becomes a
	Pending Allowance Request ({"pending": True, "request": ...}) that a
	manager approves on the spot with their PIN, or later from the list
	(decide_allowance). A manager's own allowance posts at once, signed
	with their own PIN (`pin`) whatever the property's cashier PIN setting.
	AI agents can ask, never approve. Every allowance that posts gets a
	voucher serial (allowance_voucher)."""
	from kamra.localization import money_text
	_pin_guard(folio, pin)
	doc = frappe.get_doc("Folio", folio)
	if doc.status == "Closed":
		frappe.throw(_("This folio is settled - pass the allowance on the "
		               "guest's open folio, or cancel the invoice first."))
	if not flt(gst_rate):
		from kamra.pricing import property_currency, round_rate_amount
		amount = float(round_rate_amount(abs(flt(amount)), 0, "Nearest", property_currency(doc.property)))
	else:
		from kamra.pricing import night_rate_places
		amount = round(abs(flt(amount)), night_rate_places(doc.property))
	if not amount:
		frappe.throw(_("Allowance amount is required."))
	text = _allowance_reason(reason_code, reason)
	if _allowance_needs_approval(doc.property, amount, reason_code):
		from kamra.credit import _is_agent
		if _is_agent() or not _may_approve_allowance(doc.property):
			req = frappe.get_doc({
				"doctype": "Allowance Request", "property": doc.property,
				"folio": doc.name, "reservation": doc.reservation,
				"amount": amount, "gst_rate": flt(gst_rate),
				"reason_code": reason_code or None, "reason": text,
				"status": "Pending", "requested_by": frappe.session.user,
				"requested_on": now_datetime(),
				"channel": agent_channel() or "Desk",
			}).insert(ignore_permissions=True)
			from kamra.webhooks import emit_approval
			emit_approval("approval.requested", req)
			from kamra.savings import log_action
			log_action("allowance_requested", "Folio", doc.name, doc.property,
			           rationale=f"Asked for an allowance of "
			                     f"{money_text(doc.property, amount)}: {text}",
			           channel=agent_channel() or "Desk",
			           approval_status="Pending")
			return {"ok": True, "pending": True, "request": req.name,
			        "folio": folio}
		# a manager's own allowance: they are the approval, and sign it
		# with their PIN like the paper voucher
		from kamra.authz import verify_supervisor
		approver = verify_supervisor(doc.property, frappe.session.user, pin)
		serial = _post_allowance(doc.name, amount, text, gst_rate, approver,
		                         reason_code=reason_code)
		return {"ok": True, "folio": folio, "approved_by": approver,
		        "serial": serial}
	serial = _post_allowance(doc.name, amount, text, gst_rate,
	                         reason_code=reason_code)
	return {"ok": True, "folio": folio, "serial": serial}


class NoRoomNightsToDiscount(frappe.ValidationError):
	"""A discount comes off posted room nights and the bill has none yet.
	The desk screen keys on this class to offer Advance bill."""


class AdvancePaidIsLedgerOwned(frappe.ValidationError):
	"""Reservation.advance_paid is what the folio recorded; payments and
	refunds keep it (frappe.db.set_value). A booking save never changes it."""


def _discount_lines(doc, kind: str, value: float, basis: str) -> list[dict]:
	"""What a discount takes off the room nights on the bill, one line per
	tax rate (the allowance reverses tax at the rate of the nights it
	reduces). Percent is the same per night or for the stay; an Amount is
	either for the stay (spread over the rates by value) or per room night."""
	groups: dict[float, dict] = {}
	from kamra.folio import voided_rows
	voided = voided_rows(doc)
	for c in doc.charges:
		if c.charge_type != "Room" or flt(c.amount) <= 0 or c.name in voided:
			continue
		g = groups.setdefault(flt(c.gst_rate), {"base": 0.0, "nights": 0})
		g["base"] += flt(c.amount)
		g["nights"] += 1
	if not groups:
		frappe.throw(_("No nights are posted yet. Use Advance bill to post the stay "
		               "now, then give the discount, or use a rate plan or promo "
		               "code at booking."), NoRoomNightsToDiscount)
	base = sum(g["base"] for g in groups.values())
	value = flt(value)
	if value <= 0:
		frappe.throw(_("Enter a discount above zero."))
	if kind == "Percent":
		if value > 100:
			frappe.throw(_("A discount can't be more than 100%."))
		want = {r: g["base"] * value / 100 for r, g in groups.items()}
	elif basis == "Night":
		want = {r: value * g["nights"] for r, g in groups.items()}
	else:
		want = {r: value * g["base"] / base for r, g in groups.items()}
	from kamra.ledger import _currency_places, _round_currency
	places = max(_currency_places(doc.property), 2)
	eps = 0.5 * (10 ** -places)
	lines = [{"gst_rate": r, "amount": _round_currency(a, places), "nights": groups[r]["nights"],
	          "base": _round_currency(groups[r]["base"], places)}
	         for r, a in want.items() if _round_currency(a, places) > 0]
	if sum(ln["amount"] for ln in lines) > base + eps:
		from kamra.localization import money_text
		frappe.throw(_("The discount is more than the room nights on the bill ({0}).")
		             .format(money_text(doc.property, base)))
	return lines


def _discount_tax_inclusive(lines: list[dict], places: int = 2) -> float:
	"""How much an allowance set of lines takes off the folio balance
	(base + the tax those lines reverse)."""
	from kamra.ledger import _round_currency
	return _round_currency(sum(flt(ln["amount"]) * (1 + flt(ln["gst_rate"]) / 100)
	                           for ln in lines), places)


def _cap_discount_to_open_balance(doc, lines: list[dict]) -> list[dict]:
	"""Refuse a discount on a settled open folio, and never let one push
	the balance below zero. The room-night base cap in `_discount_lines`
	still applies first; this is a second guard against the open balance
	(tax-inclusive), so a fully paid folio cannot take a discount."""
	from decimal import Decimal
	from kamra.ledger import _currency_places, _money, _round_currency
	places = max(_currency_places(doc.property), 2)
	eps = 0.5 * (10 ** -places)
	step = Decimal(1).scaleb(-places)
	balance = flt(doc.balance)
	if balance <= eps:
		frappe.throw(_("This folio has no open balance. Use a refund or "
		               "credit note instead."))
	impact = _discount_tax_inclusive(lines, places)
	if impact <= balance + eps:
		return lines
	# Scale each line's pre-tax amount so the tax-inclusive total fits.
	# Lines share one scale so the mix of GST rates stays proportional.
	scale = balance / impact if impact else 0
	capped = []
	for ln in lines:
		amount = _round_currency(flt(ln["amount"]) * scale, places)
		if amount > 0:
			capped.append({**ln, "amount": amount})
	if not capped or _discount_tax_inclusive(capped, places) <= 0:
		frappe.throw(_("This folio has no open balance. Use a refund or "
		               "credit note instead."))
	# Fix residual rounding so we never exceed the open balance.
	while capped and _discount_tax_inclusive(capped, places) > balance + eps:
		capped[-1]["amount"] = _round_currency(_money(capped[-1]["amount"]) - step, places)
		if capped[-1]["amount"] <= 0:
			capped.pop()
	if not capped:
		frappe.throw(_("This folio has no open balance. Use a refund or "
		               "credit note instead."))
	return capped


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Front Desk", "Kamra Agent")
def post_discount(folio: str, value: float = 0, kind: str = "Percent",
                  basis: str = "Stay", reason: str = "",
                  reason_code: str | None = None, pin: str | None = None,
                  voucher_code: str | None = None, preview: int = 0):
	"""Give a discount on the room nights already on the bill: a percent, or
	an amount for the whole stay or per room night. A promo code
	(voucher_code, a Discount Voucher) sets the kind and value instead.

	It posts as an allowance, so the property's allowance approval, the
	voucher serial and the allowance report all apply: above the approval
	line the desk only asks and a manager approves. preview=1 returns the
	lines without posting."""
	from kamra.localization import money_text
	doc = frappe.get_doc("Folio", folio)
	if doc.status == "Closed":
		frappe.throw(_("This folio is settled - give the discount on the guest's "
		               "open folio, or cancel the invoice first."))
	if kind not in ("Percent", "Amount") or basis not in ("Stay", "Night"):
		frappe.throw(_("Pick a percent or an amount, per night or for the stay."))
	label = ""
	if (voucher_code or "").strip():
		from kamra.folio import voided_rows
		from kamra.pricing import validate_voucher
		voided = voided_rows(doc)
		nights = sum(1 for c in doc.charges if c.charge_type == "Room"
		             and not c.get("reversal_of") and c.name not in voided)
		v = validate_voucher(doc.property, voucher_code, max(nights, 1))
		if any(v.voucher_code in (c.description or "") for c in doc.charges
		       if c.charge_type == "Allowance") or (
			doc.reservation and frappe.db.get_value(
				"Reservation", doc.reservation, "voucher") == v.name):
			frappe.throw(_("Promo code {0} is already on this stay.").format(v.voucher_code))
		kind = "Percent" if v.discount_type == "Percent" else "Amount"
		value, basis = flt(v.value), "Stay"
		label = _("Promo code {0}").format(v.voucher_code)
	from kamra.ledger import _currency_places, _round_currency
	places = max(_currency_places(doc.property), 2)
	lines = _cap_discount_to_open_balance(doc, _discount_lines(doc, kind, value, basis))
	total = _round_currency(sum(ln["amount"] for ln in lines), places)
	if not label:
		label = (_("Discount {0}%").format(f"{flt(value):g}") if kind == "Percent"
		         else _("Discount {0} per night").format(money_text(doc.property, value))
		         if basis == "Night" else _("Discount"))
	if cint(preview):
		return {"lines": lines, "total": total, "label": label}
	note = (reason or "").strip()
	if not (note or reason_code or (voucher_code or "").strip()):
		frappe.throw(_("A discount needs a reason - it goes on the record."))
	# post_allowance puts the reason code's text in front of this
	text = f"{label}: {note}" if note else label
	results = [post_allowance(folio, ln["amount"], reason=text,
	                          gst_rate=ln["gst_rate"], pin=pin,
	                          reason_code=reason_code)
	           for ln in lines]
	if (voucher_code or "").strip():
		from kamra.pricing import consume_voucher_use
		consume_voucher_use(v.name)
	return {"ok": True, "folio": folio, "total": total,
	        "pending": any(r.get("pending") for r in results), "results": results}


_ALLOWANCE_APPROVERS = {
	"Hotel Admin": ("Hotel Admin",),
	"Hotel Admin or Finance": ("Hotel Admin", "Finance"),
}


def _allowance_approver_roles(prop: str) -> tuple:
	"""Roles that approve an allowance here. With the setting Off, only a
	supervisor reason code asks, and the usual supervisor roles decide."""
	from kamra.settings import get_setting
	return _ALLOWANCE_APPROVERS.get(get_setting(prop, "allowance_approval"),
	                                ("Hotel Admin", "Finance"))


def _may_approve_allowance(prop: str, user: str | None = None) -> bool:
	user = user or frappe.session.user
	return user == "Administrator" or bool(
		set(_allowance_approver_roles(prop) + ("System Manager",))
		& set(frappe.get_roles(user)))


def _allowance_needs_approval(prop: str, amount: float,
                              reason_code: str | None) -> bool:
	from kamra.settings import get_setting
	if reason_code and frappe.db.get_value("Reason Code", reason_code,
	                                       "requires_supervisor"):
		return True
	if get_setting(prop, "allowance_approval") not in _ALLOWANCE_APPROVERS:
		return False
	return amount > flt(get_setting(prop, "allowance_approval_above"))


def _allowance_reason(reason_code: str | None, reason: str) -> str:
	"""The text that goes on the bill and the record: the reason code's
	description (an Adjust code), then the desk's note."""
	note = (reason or "").strip()
	if reason_code:
		from kamra.ledger import ensure_default_reason_codes
		ensure_default_reason_codes()
		row = frappe.db.get_value("Reason Code", reason_code,
		                          ["category", "description", "disabled"],
		                          as_dict=True)
		if not row or row.disabled:
			frappe.throw(_("That reason code is not in use."))
		if row.category != "Adjust":
			frappe.throw(_("Pick an Adjust reason code for an allowance."))
		return f"{row.description}: {note}" if note else row.description
	if not note:
		frappe.throw(_("An allowance needs a reason - it goes on the record."))
	return note


def _allowance_serial(prop: str) -> str:
	"""The next voucher number: ALW-<property short code>-<YY>-#####, one
	series per short code, taken only when an allowance posts. A changed
	short code starts a new series; the old one is never reused."""
	from frappe.model.naming import make_autoname
	from kamra.kamra.doctype.property.property import default_short_code
	code = (frappe.db.get_value("Property", prop, "short_code")
	        or default_short_code(prop, exclude=prop))
	return make_autoname(f"ALW-{code}-.YY.-.#####")


def _post_allowance(folio: str, amount: float, text: str, gst_rate,
                    approver: str | None = None,
                    requested_by: str | None = None, req=None,
                    reason_code: str | None = None) -> str:
	"""The one allowance writer behind the desk, approvals and agents.
	Every allowance that posts gets a voucher serial, kept on the bill line
	and on its Allowance Request: the approved request (`req`, the caller
	saves it), or an Approved one written here for an allowance that
	posted straight away. Returns the serial."""
	prop = frappe.db.get_value("Folio", folio, "property")
	serial = _allowance_serial(prop)
	from kamra.folio import post_allowance as _allow
	_allow(folio, amount, text, flt(gst_rate), approver=approver,
	       requested_by=requested_by, serial=serial)
	if req is None:
		now = now_datetime()
		req = frappe.get_doc({
			"doctype": "Allowance Request", "property": prop, "folio": folio,
			"reservation": frappe.db.get_value("Folio", folio, "reservation"),
			"amount": amount, "gst_rate": flt(gst_rate),
			"reason_code": reason_code or None, "reason": text,
			"status": "Approved", "requested_by": frappe.session.user,
			"requested_on": now, "channel": agent_channel() or "Desk",
			"decided_by": approver, "decided_on": now if approver else None,
			"serial": serial})
		req.flags.kamra_decision = True
		req.insert(ignore_permissions=True)
	else:
		req.serial = serial
	from kamra.webhooks import emit_discount_applied
	emit_discount_applied(folio, amount, gst_rate, text, serial, approver, req.name)
	from kamra.savings import log_action
	from kamra.localization import money_text
	log_action("allowance", "Folio", folio,
	           rationale=f"Allowance {serial} {money_text(prop, amount)}: {text}",
	           approver=approver)
	return serial


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Front Desk", "Hotel Admin")
def decide_allowance(allowance_request: str, approve: int = 1,
                     note: str = "", pin: str | None = None,
                     supervisor: str | None = None,
                     supervisor_pin: str | None = None):
	"""Approve or reject a pending allowance. The approver is the signed-in
	manager, or, on the spot at the desk, a manager who picks their name
	and types their own PIN (supervisor + supervisor_pin). Approving posts
	it on the folio; a rejection needs a note and is kept with who and why.
	AI agents never decide."""
	from kamra.credit import _is_agent
	from kamra.localization import money_text
	if _is_agent():
		frappe.throw(_("An AI agent can't approve or reject an allowance. "
		               "Ask a manager."), frappe.PermissionError)
	req = frappe.get_doc("Allowance Request", allowance_request, for_update=True)
	if req.status != "Pending":
		frappe.throw(_("This allowance was already {0}.").format(
			_(req.status).lower()))
	approver = (supervisor or "").strip() or frappe.session.user
	if cint(approve) and approver == req.requested_by:
		frappe.throw(_("You asked for this allowance; another manager approves it."),
		             frappe.PermissionError)
	if not _may_approve_allowance(req.property, approver):
		frappe.throw(_("{0} can't approve allowances here. It needs: {1}.").format(
			approver, ", ".join(_(r) for r in _allowance_approver_roles(req.property))),
			frappe.PermissionError)
	if supervisor or cint(approve):
		# an approval is signed with the manager's own PIN every time, even
		# when they are the one signed in and the property's cashier PIN is off
		from kamra.authz import verify_supervisor
		verify_supervisor(req.property, approver,
		                  supervisor_pin if supervisor else (pin or supervisor_pin))
	else:
		_pin_guard(req.folio, pin)
	note = (note or "").strip()
	if cint(approve):
		if frappe.db.get_value("Folio", req.folio, "status") == "Closed":
			frappe.throw(_("This folio is settled now. Reject this request and "
			               "pass the allowance on the guest's open folio."))
		_post_allowance(req.folio, flt(req.amount), req.reason, req.gst_rate,
		                approver, requested_by=req.requested_by, req=req)
		req.status = "Approved"
	else:
		if not note:
			frappe.throw(_("Say why the allowance is rejected - it goes on the record."))
		req.status = "Rejected"
		from kamra.savings import log_action
		log_action("allowance_rejected", "Folio", req.folio, req.property,
		           rationale=f"Rejected {money_text(req.property, req.amount)} "
		                     f"({req.reason}) asked by {req.requested_by}: {note}",
		           approver=approver, approval_status="Rejected")
	req.decided_by = approver
	req.decided_on = now_datetime()
	req.decision_note = note
	req.flags.kamra_decision = True
	req.save(ignore_permissions=True)
	from kamra.webhooks import emit_approval
	emit_approval("approval.decided", req)
	return {"ok": True, "request": req.name, "status": req.status,
	        "decided_by": approver, "serial": req.serial}


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Hotel Admin", "Kamra Agent")
def allowance_requests(property: str, status: str = "Pending",
                       folio: str | None = None):
	"""Allowances waiting for a manager (or decided ones, by status), with
	the stay they are for. can_decide says if the caller may approve."""
	assert_property_access(property)
	filters = {"property": property}
	if status:
		filters["status"] = status
	if folio:
		filters["folio"] = folio
	rows = frappe.get_all(
		"Allowance Request", filters=filters,
		fields=["name", "serial", "folio", "reservation", "amount", "gst_rate",
		        "reason_code", "reason", "status", "requested_by",
		        "requested_on", "channel", "decided_by", "decided_on",
		        "decision_note"],
		order_by="requested_on desc", limit_page_length=200)
	stays = {r.name: r for r in frappe.get_all(
		"Reservation",
		filters={"name": ["in", list({r.reservation for r in rows if r.reservation}) or [""]]},
		fields=["name", "guest_name", "room"])}
	folios = {f.name: f for f in frappe.get_all(
		"Folio", filters={"name": ["in", list({r.folio for r in rows}) or [""]]},
		fields=["name", "status", "bill_to_name"])}
	people = {u.name: u for u in frappe.get_all(
		"User", filters={"name": ["in", list({r.decided_by for r in rows if r.decided_by}) or [""]]},
		fields=["name", "full_name", "kamra_job_title"])}
	for r in rows:
		s = stays.get(r.reservation) or {}
		f = folios.get(r.folio) or {}
		u = people.get(r.decided_by) or {}
		r.update(guest_name=s.get("guest_name"), room=s.get("room"),
		         folio_status=f.get("status"),
		         bill_to_name=f.get("bill_to_name"),
		         decided_by_name=u.get("full_name"),
		         decided_by_title=u.get("kamra_job_title"))
	from kamra.credit import _is_agent
	return {"requests": rows,
	        "can_decide": not _is_agent() and _may_approve_allowance(property)}


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Hotel Admin")
def allowance_voucher(allowance_request: str):
	"""The printable allowance voucher for a posted allowance, by request
	name or serial: who asked, the manager who approved it with their PIN,
	and what it took off the bill. Rejected or waiting requests have none."""
	name = frappe.db.get_value("Allowance Request",
	                           {"serial": allowance_request}) or allowance_request
	if not frappe.db.exists("Allowance Request", name):
		frappe.throw(_("No such allowance."), frappe.DoesNotExistError)
	req = frappe.get_doc("Allowance Request", name)
	assert_property_access(req.property)
	if req.status != "Approved" or not req.serial:
		frappe.throw(_("Only a posted allowance has a voucher."))
	prop = frappe.get_doc("Property", req.property)
	stay = frappe.db.get_value("Reservation", req.reservation,
	                           ["guest_name", "room"], as_dict=True) or {}
	line = frappe.db.get_value(
		"Folio Charge", {"parent": req.folio, "allowance_serial": req.serial},
		["amount", "gst_amount", "total"], as_dict=True) or {}
	from frappe.utils import get_fullname
	approver_roles = frappe.get_roles(req.decided_by) if req.decided_by else []
	role = next((r for r in _allowance_approver_roles(req.property)
	             + ("System Manager",) if r in approver_roles), None)
	title = (frappe.db.get_value("User", req.decided_by, "kamra_job_title")
	         if req.decided_by else None)
	return {
		"property": {
			"property_name": prop.property_name,
			"address": ", ".join(p for p in (prop.address_line, prop.city,
			                                 prop.state, prop.pincode) if p),
			"phone": prop.phone,
		},
		"name": req.name, "serial": req.serial,
		"posted_on": str(req.decided_on or req.modified),
		"folio": req.folio,
		"bill_to_name": frappe.db.get_value("Folio", req.folio, "bill_to_name"),
		"reservation": req.reservation,
		"guest_name": stay.get("guest_name"),
		"room": frappe.db.get_value("Room", stay.get("room"), "room_number")
		if stay.get("room") else None,
		"amount": flt(req.amount), "gst_rate": flt(req.gst_rate),
		"tax": abs(flt(line.get("gst_amount"))),
		"total": abs(flt(line.get("total"))) or flt(req.amount),
		"reason_code": req.reason_code, "reason": req.reason,
		"requested_by": req.requested_by,
		"requested_by_name": get_fullname(req.requested_by),
		"requested_on": str(req.requested_on or ""),
		"channel": req.channel,
		"approved_by": req.decided_by,
		"approved_by_name": get_fullname(req.decided_by) if req.decided_by else None,
		"approved_by_role": role,
		# the staff job title from Staff & access, or the role when unset
		"approved_by_title": title or role,
		# every approval since vouchers began is signed with the PIN
		"approved_with_pin": bool(req.decided_by),
		"approved_on": str(req.decided_on or ""),
	}


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Hotel Admin")
def allowance_approvers(property: str):
	"""Managers who can approve an allowance on the spot here (role from
	the property setting, cashier PIN set). Names only."""
	assert_property_access(property)
	from kamra.authz import supervisors_for
	return supervisors_for(property, _allowance_approver_roles(property))


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Front Desk", "Kamra Agent")
def part_settle_folio(folio: str, pin: str | None = None):
	"""Interim invoice mid-stay: freeze the paid folio, open a fresh one."""
	_pin_guard(folio, pin)
	from kamra.folio import part_settle as _settle
	return _settle(folio)


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Front Desk", "Kamra Agent")
def cancel_invoice(folio: str, reason: str, pin: str | None = None):
	"""Void an invoice into the register and reopen the folio for correction.
	An invoice the ERPNext bridge issued goes to the bridge's credit and
	re-bill instead; the folio is not reopened here."""
	_pin_guard(folio, pin)
	from kamra.folio import bridge_invoice, cancel_invoice as _cancel, route_to_credit_and_rebill
	doc = frappe.get_doc("Folio", folio)
	if bridge_invoice(doc):
		# the whole invoice credited and re-billed: the same roles as a credit
		# note, the same checks and the same record as a cancellation here
		from kamra.authz import require_invoice_correction_role
		require_invoice_correction_role()
		if doc.status != "Closed":
			frappe.throw(_("Only a settled folio with an invoice can be cancelled."))
		if doc.folio_type == "Account":
			frappe.throw(_("An account invoice can't be cancelled - issue a credit "
			               "note against it instead."))
		if not (reason or "").strip():
			frappe.throw(_("A cancellation needs a reason - it goes on the record."))
		out = route_to_credit_and_rebill(doc, reason.strip())
		from kamra.savings import log_action
		log_action("cancel_invoice", "Folio", folio,
		           rationale=f"Invoice {bridge_invoice(doc)} sent for credit and re-bill: {reason}")
		return out
	out = _cancel(folio, reason)
	from kamra.savings import log_action
	log_action("cancel_invoice", "Folio", folio,
	           rationale=f"Invoice {out['cancelled']} cancelled: {reason}")
	return out


def _si_matches_folio_scope(si, prop, prop_currency: str | None) -> bool:
	"""Verify an accounting Sales Invoice belongs to the folio's own Company,
	currency, and is a regular (non-return) invoice."""
	if cint(si.get("is_return")):
		return False
	prop_company = prop.get("company") if prop else None
	si_company = si.get("company")
	si_meta = getattr(si, "meta", None)
	if prop_company or si_company or (si_meta and si_meta.has_field("company")):
		if not prop_company or si_company != prop_company:
			return False
	si_currency = si.get("currency")
	if prop_currency:
		if si_currency:
			if si_currency != prop_currency:
				return False
		elif si_meta and si_meta.has_field("currency"):
			return False
	return True


def _resolve_accounting_invoice(doc, prop=None, prop_currency: str | None = None):
	"""Resolve the active submitted accounting Sales Invoice for a bridged folio.

	Enforces native Sales Invoice read permission (role, doc-level, and User
	Permissions), Company/currency isolation, explicit bridge folio linkage
	(`kamra_ref == doc.name`), and follows `kamra_rebill_of` when a linked
	invoice was cancelled or credited and replaced. Never imports accounting
	internals. Returns `(current_si, source_si, state)` where `state` is one of
	`"none"`, `"submitted"`, `"not_final"`, or `"unavailable"`.
	"""
	si_name = doc.get("sales_invoice")
	if not si_name:
		return None, None, "none"
	try:
		if not frappe.db.exists("DocType", "Sales Invoice"):
			return None, None, "unavailable"
		if not frappe.has_permission("Sales Invoice", ptype="read"):
			return None, None, "unavailable"
		si = frappe.get_doc("Sales Invoice", si_name)
	except Exception:
		return None, None, "unavailable"
	if not frappe.has_permission("Sales Invoice", ptype="read", doc=si):
		return None, None, "unavailable"
	if (si.get("kamra_ref") or "") != doc.name:
		return None, None, "unavailable"
	if not _si_matches_folio_scope(si, prop, prop_currency):
		return None, None, "unavailable"

	docstatus = int(si.get("docstatus") or 0)
	if docstatus == 0:
		return None, si, "not_final"

	# Follow kamra_rebill_of chain when a linked invoice was cancelled or
	# superseded by a submitted replacement invoice.
	current = si
	visited = {si.name}
	try:
		has_rebill_col = bool(frappe.db.has_column("Sales Invoice", "kamra_rebill_of"))
	except Exception:
		has_rebill_col = False

	if has_rebill_col:
		for _hop in range(10):
			try:
				candidates = frappe.get_all(
					"Sales Invoice",
					filters={
						"kamra_rebill_of": current.name,
						"is_return": 0,
						"docstatus": ("in", [1, 2]),
					},
					pluck="name",
					order_by="docstatus asc, creation desc",
				)
			except Exception:
				candidates = []
			advanced = False
			for cand_name in candidates or []:
				if not cand_name or cand_name in visited:
					continue
				try:
					cand = frappe.get_doc("Sales Invoice", cand_name)
				except Exception:
					continue
				if not frappe.has_permission("Sales Invoice", ptype="read", doc=cand):
					# Fail closed without leaking replacement figures.
					return None, si, ("not_final" if int(current.get("docstatus") or 0) != 1 else "unavailable")
				if not _si_matches_folio_scope(cand, prop, prop_currency):
					continue
				if (cand.get("kamra_rebill_of") or "") != current.name:
					continue
				cand_ref = cand.get("kamra_ref")
				if cand_ref and cand_ref != doc.name:
					continue
				cand_ds = int(cand.get("docstatus") or 0)
				# Never step down from a submitted invoice to a cancelled one.
				if int(current.get("docstatus") or 0) == 1 and cand_ds != 1:
					continue
				visited.add(cand_name)
				current = cand
				advanced = True
				break
			if not advanced:
				break

	if int(current.get("docstatus") or 0) != 1:
		return None, si, "not_final"
	return current, si, "submitted"


def _load_accounting_invoice(doc, prop=None, prop_currency: str | None = None):
	"""Read the active submitted accounting Sales Invoice for a bridged folio.
	Returns None if missing, unpermitted, draft, cancelled (without a submitted
	rebill), or outside the folio's Company/currency/link scope."""
	si, _source_si, _state = _resolve_accounting_invoice(doc, prop=prop, prop_currency=prop_currency)
	return si


def _erp_credited_total(si, prop=None, prop_currency: str | None = None) -> float:
	"""Sum submitted, readable ERP Credit Notes (Sales Invoice with is_return=1)
	against the active accounting Sales Invoice."""
	if si is None or not si.get("name"):
		return 0.0
	try:
		if not frappe.has_permission("Sales Invoice", ptype="read"):
			return 0.0
		names = frappe.get_all(
			"Sales Invoice",
			filters={"return_against": si.name, "is_return": 1, "docstatus": 1},
			pluck="name",
		)
	except Exception:
		return 0.0
	from decimal import Decimal

	from kamra.ledger import _round_currency
	from kamra.pricing import _minor_unit

	total = Decimal(0)
	si_company = si.get("company")
	si_currency = si.get("currency")
	places = max(_minor_unit(si_currency or prop_currency), 2)
	for ret_name in names or []:
		try:
			ret = frappe.get_doc("Sales Invoice", ret_name)
		except Exception:
			continue
		if not frappe.has_permission("Sales Invoice", ptype="read", doc=ret):
			continue
		if int(ret.get("docstatus") or 0) != 1 or not cint(ret.get("is_return")):
			continue
		if (ret.get("return_against") or "") != si.name:
			continue
		if si_company and ret.get("company") != si_company:
			continue
		if si_currency and ret.get("currency") != si_currency:
			continue
		total += abs(Decimal(str(ret.get("grand_total") or 0)))
	return _round_currency(total, places)


def _clean_accounting_tax_label(tx, rate: float, fallback: str) -> str:
	import re

	raw = (tx.get("description") or "").strip()
	if not raw:
		raw = (tx.get("account_head") or "").strip()
		if " - " in raw:
			raw = raw.rsplit(" - ", 1)[0].strip()
	label = raw
	if rate:
		for fmt_rate in (f"{rate:g}", f"{rate:.2f}"):
			label = re.sub(
				rf"\s*(?:@\s*)?\(?{re.escape(fmt_rate)}\s*%\)?$",
				"", label,
			).strip()
	return label or fallback


def _parse_item_wise_tax(raw) -> dict:
	if isinstance(raw, dict):
		return raw
	if not raw or not isinstance(raw, str):
		return {}
	try:
		parsed = json.loads(raw)
		return parsed if isinstance(parsed, dict) else {}
	except ValueError:
		return {}


def _apply_accounting_invoice_taxes(doc, prop, prop_currency: str | None, folio_dict: dict,
                                    lines: list[dict], pairs: set[str], split: list,
                                    default_tax_label: str) -> dict:
	"""When a closed bridged folio carries a linked accounting Sales Invoice,
	replace the 0% folio tax lines and summary with the accounting invoice's
	own tax breakdown (single source of truth). If the accounting invoice
	cannot be read, report tax_source='accounting_unavailable' and emit no
	fake 0% summary rows."""
	si, source_si, state = _resolve_accounting_invoice(doc, prop=prop, prop_currency=prop_currency)
	if state == "none":
		return {
			"tax_source": "folio", "tax_summary": None, "gst_summary": None,
			"accounting_tax_note": None, "by_head": None,
			"si": None, "state": "none", "erp_credited": 0.0,
		}

	if si is None:
		return {
			"tax_source": "accounting_unavailable",
			"tax_summary": [],
			"gst_summary": [],
			"accounting_tax_note": _("Tax as per the accounting invoice"),
			"by_head": None,
			"si": None,
			"state": state,
			"erp_credited": 0.0,
		}

	from kamra.ledger import _round_currency
	from kamra.pricing import _minor_unit

	places = max(_minor_unit(si.get("currency") or prop_currency), 2)
	items = list(si.get("items") or [])
	taxes = list(si.get("taxes") or [])
	si_net_total = float(si.get("net_total") or 0)
	si_tax_total = float(si.get("total_taxes_and_charges") or 0)
	si_grand_total = float(si.get("grand_total") or 0)

	# ERPNext v16 stores per-line tax breakdown in the `item_wise_tax_details`
	# child table (item_row -> item.name, tax_row -> tax.name); ERPNext v15
	# stores a JSON map on each tax row's `item_wise_tax_detail`. Support both,
	# with a proportional net_amount fallback if neither is populated.
	item_idx_by_name = {it.get("name"): idx for idx, it in enumerate(items) if it.get("name")}
	v16_by_tax: dict[str, list] = {}
	for det in si.get("item_wise_tax_details") or []:
		trow = det.get("tax_row")
		if trow:
			v16_by_tax.setdefault(trow, []).append(det)

	item_rates = [0.0] * len(items)
	item_taxes = [0.0] * len(items)
	item_taxed = [False] * len(items)
	item_inclusive = [False] * len(items)
	tax_summary = []
	gst_summary = []

	for tx in taxes:
		raw_tax = tx.get("tax_amount_after_discount_amount")
		tax_amt = float(raw_tax if raw_tax is not None else (tx.get("tax_amount") or 0))
		tx_rate = float(tx.get("rate") or 0)
		tx_incl = bool(cint(tx.get("included_in_print_rate")))
		v16_rows = v16_by_tax.get(tx.get("name") or "", [])
		detail = {} if v16_rows else _parse_item_wise_tax(tx.get("item_wise_tax_detail"))

		taxable_for_row = 0.0
		detail_rates = set()
		has_item_detail = False

		if v16_rows:
			has_item_detail = True
			for det in v16_rows:
				e_rate = float(det.get("rate") or 0)
				e_tax = float(det.get("amount") or 0)
				e_taxable = float(det.get("taxable_amount") or 0)
				if e_rate:
					detail_rates.add(e_rate)
				idx = item_idx_by_name.get(det.get("item_row"))
				if e_rate or abs(e_tax) > 1e-6:
					taxable_for_row += e_taxable
					if idx is not None:
						item_taxed[idx] = True
						if tx_incl:
							item_inclusive[idx] = True
				if idx is not None:
					if e_rate:
						item_rates[idx] += e_rate
					item_taxes[idx] = _round_currency(item_taxes[idx] + e_tax, places)
		elif detail:
			# Match items referenced by v15 item_wise_tax_detail JSON
			by_key: dict[str, list[int]] = {}
			for idx, it in enumerate(items):
				key = it.get("item_code") if it.get("item_code") in detail else (
					it.get("item_name") if it.get("item_name") in detail else None
				)
				if key is not None:
					by_key.setdefault(key, []).append(idx)
			has_item_detail = bool(by_key)
			for key, idxs in by_key.items():
				entry = detail.get(key)
				if not isinstance(entry, (list, tuple)) or len(entry) < 2:
					continue
				e_rate = float(entry[0] or 0)
				e_tax = float(entry[1] or 0)
				if e_rate:
					detail_rates.add(e_rate)
				if e_rate or abs(e_tax) > 1e-6:
					taxable_for_row += sum(float(items[i].get("net_amount") or 0) for i in idxs)
					for idx in idxs:
						item_taxed[idx] = True
						if tx_incl:
							item_inclusive[idx] = True
				net_sum = sum(float(items[i].get("net_amount") or 0) for i in idxs)
				allocated = 0.0
				for pos, idx in enumerate(idxs):
					if e_rate:
						item_rates[idx] += e_rate
					if pos == len(idxs) - 1:
						share = _round_currency(e_tax - allocated, places)
					elif abs(net_sum) > 1e-9:
						share = _round_currency(e_tax * float(items[idx].get("net_amount") or 0) / net_sum, places)
					else:
						share = _round_currency(e_tax / len(idxs), places)
					allocated += share
					item_taxes[idx] = _round_currency(item_taxes[idx] + share, places)
		elif items and abs(tax_amt) > 1e-6:
			net_sum = sum(float(it.get("net_amount") or 0) for it in items)
			allocated = 0.0
			for pos, it in enumerate(items):
				if abs(float(it.get("net_amount") or 0)) > 1e-6:
					item_taxed[pos] = True
					if tx_incl:
						item_inclusive[pos] = True
				if tx_rate:
					item_rates[pos] += tx_rate
				if pos == len(items) - 1:
					share = _round_currency(tax_amt - allocated, places)
				elif abs(net_sum) > 1e-9:
					share = _round_currency(tax_amt * float(it.get("net_amount") or 0) / net_sum, places)
				else:
					share = _round_currency(tax_amt / len(items), places)
				allocated += share
				item_taxes[pos] = _round_currency(item_taxes[pos] + share, places)

		# Do not emit a phantom header-template row (e.g. 18% with 0 taxable / 0 tax
		# when all items are 0% exempt).
		if abs(tax_amt) < 1e-6 and (not has_item_detail or abs(taxable_for_row) < 1e-6):
			continue
		if tx_rate == 0 and tx.get("charge_type") != "Actual" and len(detail_rates) == 1:
			tx_rate = next(iter(detail_rates))
		taxable = _round_currency(taxable_for_row if has_item_detail else si_net_total, places)
		tax_rounded = _round_currency(tax_amt, places)
		label = _clean_accounting_tax_label(tx, tx_rate, default_tax_label)
		tax_summary.append({
			"rate": tx_rate,
			"taxable": taxable,
			"total_tax": tax_rounded,
			"parts": [{"label": label, "rate": tx_rate, "amount": tax_rounded}],
		})
		cgst_amt = _round_currency(tax_amt * float(split[0][1]), places) if split else tax_rounded
		sgst_amt = _round_currency(tax_amt * float(split[-1][1]), places) if len(split) >= 2 else 0.0
		gst_summary.append({
			"rate": tx_rate,
			"taxable": taxable,
			"cgst": cgst_amt,
			"sgst": sgst_amt,
			"total_tax": tax_rounded,
		})

	# Include 0% / exempt taxable base so mixed (e.g. 18% + 0% exempt) and pure-exempt
	# invoices account for the full net total without phantom taxed buckets.
	exempt_net = _round_currency(
		sum(float(it.get("net_amount") or 0) for idx, it in enumerate(items) if not item_taxed[idx]),
		places,
	)
	if abs(exempt_net) > 1e-6:
		tax_summary.append({
			"rate": 0.0,
			"taxable": exempt_net,
			"total_tax": 0.0,
			"parts": [{"label": default_tax_label, "rate": 0.0, "amount": 0.0}],
		})
		gst_summary.append({
			"rate": 0.0,
			"taxable": exempt_net,
			"cgst": 0.0,
			"sgst": 0.0,
			"total_tax": 0.0,
		})

	# Map accounting items back to folio lines (source_ref == charge.name) and
	# restore netted allowance/discount lines from kamra_allowance_refs so line
	# totals reconcile exactly to si.net_total, si.total_taxes_and_charges, si.grand_total.
	# On a rebill (kamra_rebill_of), unique source_ref stays on the original SI, so
	# fall back to the replaced source SI's positional item references.
	line_by_row = {ln["row"]: ln for ln in lines}
	source_items = list(source_si.get("items") or []) if source_si is not None and source_si is not si else []
	has_explicit_incl = any(tx.get("included_in_print_rate") is not None for tx in taxes)
	allowance_deltas: dict[str, list[float]] = {}  # row -> [net, tax, total, rate]
	mapped_rows: set[str] = set()

	for idx, it in enumerate(items):
		ref = it.get("source_ref")
		al_refs = it.get("kamra_allowance_refs")
		if not ref and len(source_items) == len(items):
			ref = source_items[idx].get("source_ref")
			al_refs = al_refs or source_items[idx].get("kamra_allowance_refs")
		ln = line_by_row.get(ref) if ref else None
		if not ln:
			continue
		mapped_rows.add(ref)
		it_net = _round_currency(float(it.get("net_amount") or 0), places)
		it_tax = _round_currency(item_taxes[idx], places)
		it_rate = round(item_rates[idx], 4)
		it_gross = _round_currency(it_net + it_tax, places)
		it_amount = float(it.get("amount") or 0)
		is_inclusive = item_inclusive[idx] if has_explicit_incl else (abs(it_gross - it_amount) < 0.05 and it_gross > 0)

		add_net = 0.0
		add_tax = 0.0
		add_gross = 0.0
		for raw_line in (al_refs or "").splitlines():
			parts = raw_line.strip().split()
			if len(parts) < 2:
				continue
			al_row = parts[0]
			try:
				take = float(parts[1])
			except ValueError:
				continue
			if take <= 0 or al_row not in line_by_row:
				continue
			if is_inclusive and it_gross > 0:
				t_net = _round_currency(take * it_net / it_gross, places)
				t_tax = _round_currency(take - t_net, places)
				t_gross = _round_currency(take, places)
			else:
				t_net = _round_currency(take, places)
				t_tax = _round_currency(take * it_rate / 100.0, places)
				t_gross = _round_currency(t_net + t_tax, places)
			add_net = _round_currency(add_net + t_net, places)
			add_tax = _round_currency(add_tax + t_tax, places)
			add_gross = _round_currency(add_gross + t_gross, places)
			slot = allowance_deltas.setdefault(al_row, [0.0, 0.0, 0.0, it_rate])
			slot[0] = _round_currency(slot[0] - t_net, places)
			slot[1] = _round_currency(slot[1] - t_tax, places)
			slot[2] = _round_currency(slot[2] - t_gross, places)
			if it_rate:
				slot[3] = it_rate

		final_net = _round_currency(it_net + add_net, places)
		final_tax = _round_currency(it_tax + add_tax, places)
		final_gross = _round_currency(it_gross + add_gross, places)
		qty = float(ln.get("qty") or 1) or 1.0
		ln["amount"] = final_net
		ln["rate"] = _round_currency(final_net / qty, places)
		ln["gst_rate"] = it_rate
		ln["gst_amount"] = final_tax
		ln["total"] = final_gross

	for al_row, (d_net, d_tax, d_gross, d_rate) in allowance_deltas.items():
		ln = line_by_row[al_row]
		mapped_rows.add(al_row)
		ln["amount"] = d_net
		ln["rate"] = d_net
		ln["gst_rate"] = d_rate
		ln["gst_amount"] = d_tax
		ln["total"] = d_gross

	# Non-voided outlet transfer charges (posted via Journal Entry, not Sales Invoice)
	transfer_net = 0.0
	transfer_tax = 0.0
	transfer_gross = 0.0
	for c in doc.charges:
		if c.name not in mapped_rows and c.name not in pairs and c.get("source_ref"):
			transfer_net += float(c.amount or 0)
			transfer_tax += float(c.gst_amount or 0)
			transfer_gross += float(c.total or 0)

	folio_dict["charges_total"] = _round_currency(si_net_total + transfer_net, places)
	folio_dict["tax_total"] = _round_currency(si_tax_total + transfer_tax, places)
	folio_dict["grand_total"] = _round_currency(si_grand_total + transfer_gross, places)

	by_head = {}
	for ln in lines:
		head_name = "Discount" if ln["charge_type"] == "Allowance" else (ln["charge_type"] or "Other")
		head = by_head.setdefault(
			head_name,
			{"head": head_name, "amount": 0.0, "tax": 0.0,
			 "total": 0.0, "lines": 0, "service_code": ln.get("service_code")},
		)
		head["amount"] = _round_currency(head["amount"] + float(ln["amount"] or 0), places)
		head["tax"] = _round_currency(head["tax"] + float(ln["gst_amount"] or 0), places)
		head["total"] = _round_currency(head["total"] + float(ln["total"] or 0), places)
		head["lines"] += ln["row"] not in pairs

	return {
		"tax_source": "accounting",
		"tax_summary": tax_summary,
		"gst_summary": gst_summary,
		"accounting_tax_note": None,
		"by_head": by_head,
		"si": si,
		"state": "submitted",
		"erp_credited": _erp_credited_total(si, prop=prop, prop_currency=prop_currency),
	}


def _tax_summary_has_vat(tax_summary: list[dict] | None, tax_label: str) -> bool:
	"""Return True only when a non-zero VAT/GST tax line is present (never
	for 0%/exempt invoices or non-VAT levies/service charges)."""
	non_vat_markers = ("levy", "service charge", "local hotel tax", "tourism", "municipal")
	vat_markers = ("vat", "gst", "cgst", "sgst", "igst", "mwst", "tva", "iva")
	norm_label = (tax_label or "").strip().lower()
	for row in tax_summary or ():
		if abs(float(row.get("total_tax") or 0)) <= 1e-6:
			continue
		parts = row.get("parts") or ()
		if not parts:
			return True
		for part in parts:
			if abs(float(part.get("amount") or 0)) <= 1e-6:
				continue
			lbl = (part.get("label") or "").strip().lower()
			if any(m in lbl for m in non_vat_markers):
				continue
			if (norm_label and norm_label in lbl) or any(m in lbl for m in vat_markers):
				return True
	return False


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Kamra Agent")
def folio_invoice(folio: str):
	"""Everything the bill needs to print itself.

	A tax invoice is a legal document, not a screenshot of a table: the
	service code belongs on each LINE (a room night, a restaurant cover
	and a laundry bag are three different supplies), the tax has to be
	named the way the country names it, the total has to appear in words,
	and the guest wants a summary by what they bought before they read
	forty lines. All of that is assembled here so every surface that
	prints a bill - the folio screen, a PDF, an email - agrees.

	Statement fields (stable for integrations):

	- `lines`: each charge line: row, posting_date, stay_date,
	  charge_type, description, qty, rate, amount (pre-tax), gst_rate,
	  gst_amount, total, service_code, transaction_code, gl_code (as
	  posted), corrects_row (a correction's original line), reversal_of
	  and void_reason (a void's or a split's reversal), split_of (the line
	  a split part or reversal came from), reservation, is_alcohol,
	  auto_posted
	- `payments`: row, posting_date, payment_kind, mode, amount (refunds
	  negative), reference, currency, fx_amount, exchange_rate,
	  transaction_code, gl_code, refund_of (the payment a refund is against)
	- `statement`: lines and payments in date order: date, row, kind
	  (Charge or the payment kind), charge_type, description, reference,
	  method, transaction_code, gl_code, charges, credits, tax, balance
	  (running)
	- `tax_summary`: per rate, taxable, total_tax and the country's parts
	- `allocations`: company payments that cleared this bill: payment,
	  account, posting_date, reference, amount
	- `credited`: what the issued credit notes on this invoice add up to
	- `folio`, `bill_to`, `guest`, `stay`, `property`, `document`
	"""
	from decimal import ROUND_HALF_UP, Decimal

	from kamra import localization as loc
	from kamra.folio import reference_currency
	from kamra.ledger import _currency_places, _round_currency, credited_total
	from kamra.pricing import saved_contract_tariff

	doc = frappe.get_doc("Folio", folio)
	prop = frappe.get_doc("Property", doc.property)
	# an Account folio (raised on a company's AR account) has no stay
	res = frappe.get_doc("Reservation", doc.reservation) if doc.reservation else None
	pack = loc.pack_for(doc.property)
	_loc_ctx = pack.invoice_context(prop)

	# B2B: corporate bookings carry the buyer's GSTIN on the invoice.
	# A group master folio bills to the GROUP's company.
	bill_to = None
	# a guest's own bill in a shared room is made out to that guest, never
	# the company
	bill_company = None if doc.get("bill_to_name") or not res else res.company
	if doc.folio_type == "Group" and doc.get("group_booking"):
		bill_company = frappe.db.get_value(
			"Group Booking", doc.group_booking, "company") or bill_company
	if doc.folio_type == "Account":
		# billed to the account it was raised on (as zatca._folio_buyer), or the event's company
		account = frappe.db.get_value(
			"City Ledger Entry", {"folio": doc.name, "kind": "Invoice"},
			"account", order_by="creation asc")
		bill_company = account and frappe.db.get_value(
			"City Ledger Account", account, "company")
		if not bill_company and doc.get("venue_booking"):
			bill_company = frappe.db.get_value("Venue Booking", doc.venue_booking, "company")
	if bill_company:
		company = frappe.db.get_value(
			"Corporate Account", bill_company, ["company_name", "gstin"], as_dict=True)
		if company:
			bill_to = {"name": company.company_name, "gstin": company.gstin}

	if doc.get("bill_to_name"):
		bill_to = {"name": doc.bill_to_name, "gstin": None}
	buyer_tax_id = (bill_to or {}).get("gstin")
	split = loc.tax_split(pack, prop, buyer_tax_id)

	# per-line service codes + a summary of what was actually bought
	from kamra.folio import void_pairs
	pairs = void_pairs(doc)
	lines, by_rate, by_head = [], {}, {}
	for c in doc.charges:
		code = loc.service_code_for(pack, prop, c.charge_type)
		lines.append({
			"row": c.name, "posting_date": str(c.posting_date),
			"charge_type": c.charge_type, "description": c.description,
			"qty": c.qty, "rate": c.rate, "amount": c.amount,
			"gst_rate": c.gst_rate, "gst_amount": c.gst_amount,
			"total": c.total, "is_alcohol": c.is_alcohol,
			"auto_posted": c.auto_posted,
			"service_code": (code or {}).get("value"),
			"transaction_code": c.get("transaction_code"),
			"gl_code": c.get("gl_code"),
			"corrects_row": c.get("corrects_row"),
			# a void's (or a split's) reversal, and the line it reverses
			"reversal_of": c.get("reversal_of"),
			"void_reason": c.get("void_reason"),
			"split_of": c.get("split_of"),
			"reservation": c.get("reservation"),
			"stay_date": str(c.stay_date) if c.get("stay_date") else None,
		})
		c_amount = Decimal(str(c.amount or 0))
		c_tax = Decimal(str(c.gst_amount or 0))
		c_total = Decimal(str(c.total or 0))
		key = float(c.gst_rate or 0)
		slot = by_rate.setdefault(key, {"taxable": Decimal(0), "tax": Decimal(0)})
		slot["taxable"] += c_amount
		slot["tax"] += c_tax
		# the guest's bill calls an allowance what it is to them: a discount
		head_name = "Discount" if c.charge_type == "Allowance" else (c.charge_type or "Other")
		head = by_head.setdefault(
			head_name,
			{"head": head_name, "amount": Decimal(0), "tax": Decimal(0),
			 "total": Decimal(0), "lines": 0, "service_code": (code or {}).get("value")})
		head["amount"] += c_amount
		head["tax"] += c_tax
		head["total"] += c_total
		# a voided line and its reversal net to nothing: not a line bought
		head["lines"] += c.name not in pairs

	prop_currency = prop.currency or loc.default_currency(pack)
	folio_dict = doc.as_dict()
	acct_tax = _apply_accounting_invoice_taxes(
		doc, prop, prop_currency, folio_dict, lines, pairs, split, _loc_ctx["tax_label"],
	)
	raw_places = _currency_places(doc.property)
	unit = Decimal(1).scaleb(-raw_places)
	currency_rounded = False
	if acct_tax["by_head"] is not None:
		by_head = acct_tax["by_head"]
	else:
		doc_charges = Decimal(str(doc.charges_total or 0))
		doc_tax = Decimal(str(doc.tax_total or 0))
		doc_grand = Decimal(str(doc.grand_total or 0))
		taxed_heads = [h for h in by_head.values() if abs(h["tax"]) > Decimal("1e-9")]
		taxable_rates = [(rate, v) for rate, v in by_rate.items() if rate > 0]
		grand_on_unit = abs(doc_grand - doc_grand.quantize(unit, rounding=ROUND_HALF_UP)) < Decimal("1e-9")
		if taxed_heads and grand_on_unit:
			# Round every head and rate bucket cleanly to the currency's ISO 4217 minor unit
			# (0 for UGX, 2 for USD, 3 for KWD), then fold any exact minor-unit residual onto
			# the dominant taxed head/rate bucket so every row sits on minor units while sums
			# reconcile exactly to document subtotal, tax_total, and grand_total.
			currency_rounded = True
			target_grand = doc_grand.quantize(unit, rounding=ROUND_HALF_UP)
			target_charges = doc_charges.quantize(unit, rounding=ROUND_HALF_UP)
			target_tax = target_grand - target_charges

			for h in by_head.values():
				h["amount"] = h["amount"].quantize(unit, rounding=ROUND_HALF_UP)
				h["tax"] = (
					Decimal(0) if abs(h["tax"]) <= Decimal("1e-9")
					else h["tax"].quantize(unit, rounding=ROUND_HALF_UP)
				)
			amt_residual = target_charges - sum((h["amount"] for h in by_head.values()), Decimal(0))
			if amt_residual:
				amt_pool = taxed_heads or list(by_head.values())
				dominant_amt = max(amt_pool, key=lambda h: (abs(h["amount"]), abs(h["tax"]), h["head"]))
				dominant_amt["amount"] += amt_residual
			tax_residual = target_tax - sum((h["tax"] for h in by_head.values()), Decimal(0))
			if tax_residual:
				dominant_head = max(taxed_heads, key=lambda h: (abs(h["tax"]), abs(h["amount"]), h["head"]))
				dominant_head["tax"] += tax_residual
			for h in by_head.values():
				h["total"] = h["amount"] + h["tax"]

			for rate, v in by_rate.items():
				v["taxable"] = v["taxable"].quantize(unit, rounding=ROUND_HALF_UP)
				v["tax"] = Decimal(0) if rate == 0 else v["tax"].quantize(unit, rounding=ROUND_HALF_UP)
			rate_taxable_residual = target_charges - sum((v["taxable"] for v in by_rate.values()), Decimal(0))
			if rate_taxable_residual and by_rate:
				rate_pool = taxable_rates or list(by_rate.items())
				_dom_r, dom_slot = max(rate_pool, key=lambda item: (abs(item[1]["taxable"]), item[0]))
				dom_slot["taxable"] += rate_taxable_residual
			rate_tax_residual = target_tax - sum((v["tax"] for v in by_rate.values()), Decimal(0))
			if rate_tax_residual and taxable_rates:
				_dom_r, dom_slot = max(taxable_rates, key=lambda item: (abs(item[1]["taxable"]), item[0]))
				dom_slot["tax"] += rate_tax_residual

			folio_dict["charges_total"] = float(target_charges)
			folio_dict["tax_total"] = float(target_tax)
		else:
			tax_delta = doc_tax - sum((v["tax"] for v in by_rate.values()), Decimal(0))
			if abs(tax_delta) > Decimal("1e-9"):
				if taxable_rates:
					_dominant_rate, dominant = max(
						taxable_rates, key=lambda item: (abs(item[1]["taxable"]), item[0]),
					)
					dominant["tax"] += tax_delta
				if taxed_heads:
					dominant_head = max(taxed_heads, key=lambda h: (abs(h["tax"]), abs(h["amount"]), h["head"]))
					dominant_head["tax"] += tax_delta
					dominant_head["total"] = dominant_head["amount"] + dominant_head["tax"]

		for h in by_head.values():
			h["amount"] = float(h["amount"])
			h["tax"] = float(h["tax"])
			h["total"] = float(h["total"])

	# A bridged folio is final once its linked accounting Sales Invoice is
	# submitted (real bridged check-outs set only sales_invoice, not
	# invoice_number). A draft or cancelled Sales Invoice is never final.
	acct_si = acct_tax["si"]
	acct_state = acct_tax["state"]
	if acct_state == "submitted" and acct_si is not None:
		settled = True
		doc_number = acct_si.name
	elif acct_state == "not_final":
		settled = False
		doc_number = doc.invoice_number or doc.name
	else:
		settled = bool(doc.invoice_number)
		doc_number = doc.invoice_number or doc.name
	# what the tax authority requires ON the bill (ZATCA: QR + Arabic title)
	statutory = (
		loc.invoice_print_block(pack, "Folio", doc.name, doc.invoice_number or doc_number)
		if settled and acct_tax["tax_source"] != "accounting_unavailable"
		else None
	)
	def _split_parts(tax_dec: Decimal) -> list[Decimal]:
		if not split:
			return []
		if len(split) == 1:
			return [tax_dec]
		if currency_rounded:
			amts = [
				(tax_dec * Decimal(str(share))).quantize(unit, rounding=ROUND_HALF_UP)
				for _lbl, share in split[:-1]
			]
			amts.append(tax_dec - sum(amts, Decimal(0)))
			return amts
		return [tax_dec * Decimal(str(share)) for _lbl, share in split]

	tax_summary = (
		acct_tax["tax_summary"] if acct_tax["tax_summary"] is not None else [
			{"rate": rate, "taxable": float(v["taxable"]), "total_tax": float(v["tax"]),
			 "parts": [{"label": label.upper(),
			            "rate": round(rate * float(share), 4),
			            "amount": float(part_amt)}
			           for (label, share), part_amt in zip(split, _split_parts(v["tax"]), strict=True)]}
			for rate, v in sorted(by_rate.items())
		]
	)
	if not settled:
		doc_title = _("Provisional Bill")
	elif acct_tax["tax_source"] == "accounting_unavailable":
		doc_title = _(loc.neutral_invoice_title(pack))
	else:
		tax_charged = _tax_summary_has_vat(tax_summary, _loc_ctx.get("tax_label") or "")
		doc_title = (statutory or {}).get("title") or _(loc.invoice_title(pack, tax_charged=tax_charged))

	doc_footer = _loc_ctx.get("footer")
	if acct_tax["tax_source"] == "accounting_unavailable" and doc_footer and "tax invoice" in doc_footer.lower():
		doc_footer = "This is a computer-generated invoice."

	# under the ERPNext accounting bridge the tax invoice is the ERP Sales
	# Invoice and its fiscal identifiers (EFRIS FDN, verification code, QR);
	# this folio's own number is then an internal reference only
	from kamra.fiscal import fiscal_status, fiscal_title
	from kamra.fiscal import is_final as fiscal_final
	fiscal = fiscal_status(doc)
	number, final, internal_number = doc_number, settled, None
	if fiscal:
		number, final, internal_number = fiscal["sales_invoice"] or doc.name, fiscal_final(fiscal), doc.invoice_number
		if acct_tax["tax_source"] == "accounting_unavailable":
			# Restricted viewer (lacks Sales Invoice read) or unreadable accounting invoice:
			# never leak accounting tax metadata or elevate the neutral title (#48).
			fiscal = {**fiscal, "charges_tax": None, "tax_template": None}
			doc_title = _(fiscal_title(fiscal)) if not final else _(loc.neutral_invoice_title(pack))
		elif not final:
			doc_title = _(fiscal_title(fiscal))
		elif acct_tax["tax_source"] == "accounting":
			# Authoritative accounting taxes were read (#48): preserve its
			# _tax_summary_has_vat + pack invoice_title for final non-EFRIS
			# invoices (so a 2% Tourism Levy with 0% VAT stays neutral "Invoice");
			# fiscalised invoices keep their statutory "Tax Invoice" title.
			if fiscal.get("state") == "fiscalised":
				doc_title = _(fiscal_title(fiscal))
		else:
			doc_title = _(fiscal_title(fiscal))
	payments = [{
		"row": p.name, "posting_date": str(p.posting_date),
		"payment_kind": p.payment_kind, "mode": p.mode,
		"amount": p.amount, "reference": p.reference,
		"currency": p.currency, "fx_amount": p.get("fx_amount"),
		"exchange_rate": p.get("exchange_rate"), "exchange_rate_date": p.get("exchange_rate_date"),
		"fx_rate_date": p.get("exchange_rate_date"),
		"transaction_code": p.get("transaction_code"),
		"gl_code": p.get("gl_code"), "refund_of": p.get("refund_of"),
	} for p in doc.payments]
	from kamra.erp_bridge import get_bridge_script
	from kamra.folio import CREDIT_AND_REBILL
	gst_summary = (
		acct_tax["gst_summary"] if acct_tax["gst_summary"] is not None else [
			{"rate": rate, "taxable": float(v["taxable"]),
			 "cgst": float(_split_parts(v["tax"])[0]) if split else float(v["tax"]),
			 "sgst": float(_split_parts(v["tax"])[-1]) if split else 0.0,
			 "total_tax": float(v["tax"])}
			for rate, v in sorted(by_rate.items())
		]
	)
	places = max(raw_places, 2)
	_prop_locale = pack.locale(prop) if callable(getattr(pack, "locale", None)) else {}
	return {
		"folio": folio_dict,
		"lines": lines,
		"payments": payments,
		"statement": _folio_statement(lines, payments, places=places),
		"allocations": _folio_allocations(doc.name, property=doc.property),
		# what the issued credit notes on this invoice add up to: the most a
		# new one may credit is the invoice total less this
		"credited": _round_currency(
			Decimal(str(credited_total(doc.name))) + Decimal(str(acct_tax.get("erp_credited") or 0.0)),
			places,
		),
		"bill_to": bill_to,
		"statutory": statutory,
		"fiscal": fiscal,
		"bridge_credit_and_rebill": bool(get_bridge_script(CREDIT_AND_REBILL)),
		"tax_source": acct_tax["tax_source"],
		"document": {
			# a bill before settlement is provisional and must say so - the
			# invoice number only exists once the folio closes, and under the
			# bridge only once the fiscal document is issued
			"title": doc_title,
			"title_local": (statutory or {}).get("title_ar"),
			"is_final": final,
			"number": number,
			"internal_number": internal_number,
			"reprint": None,
			"date": str(doc.closed_on or "")[:10] or nowdate(),
			"amount_in_words": loc.amount_in_words(pack, prop, folio_dict["grand_total"]),
			"footer": doc_footer,
			# the hotel's own lines under the statutory one - CIN, bank
			# details for a transfer
			"invoice_footer": prop.get("invoice_footer"),
			"service_code_label": (_loc_ctx.get("service_code") or {}).get(
				"label", "SAC"),
			"accounting_tax_note": acct_tax["accounting_tax_note"],
		},
		"property": {
			"currency": _prop_locale.get("currency") or prop.currency or loc.default_currency(pack),
			"currency_symbol": _prop_locale.get("currency_symbol", ""),
			"locale": _prop_locale.get("locale") or "en-US",
			"name": prop.property_name, "legal_name": prop.legal_name,
			"logo_url": prop.get("logo_url"),
			"address_line": prop.address_line, "city": prop.city,
			"state": prop.state, "pincode": prop.pincode,
			"address": ", ".join(filter(None, [prop.address_line, prop.city,  # nosemgrep: frappe-no-functional-code -- filter(None, ...) drops empty address parts; equivalent to a comprehension
			                                   prop.state, prop.pincode])),
			"gstin": prop.gstin, "phone": prop.phone, "email": prop.email,
			"website": prop.get("website"),
			# country pack owns the service code + place-of-supply rule
			"sac": _loc_ctx["sac"],
			"place_of_supply": _loc_ctx["place_of_supply"],
			"tax_label": _loc_ctx["tax_label"],
			"tax_id_label": _loc_ctx["tax_id_label"],
			"tax_note": loc.invoice_tax_note(pack),
		},
		"guest": _invoice_guest(res) if res else {"name": doc.guest_name},
		"stay": {"reservation": None, "company": bill_company} if not res else {
			"reservation": res.name,
			**saved_contract_tariff(res),
			# the contract currency beside home amounts (Property reference currency calculation)
			"reference_currency": reference_currency(res, grand_total=doc.grand_total, balance=doc.balance,
			                                         charges=doc.charges, payments=doc.payments),
			"check_in": str(res.check_in_date),
			"check_out": str(res.check_out_date),
			"arrival": str(res.get("actual_check_in") or "") or None,
			"departure": str(res.get("actual_check_out") or "") or None,
			"nights": res.nights, "room": res.room,
			"room_number": frappe.db.get_value("Room", res.room, "room_number") if res.room else None,
			"room_type": res.room_type,
			"adults": res.adults, "children": res.children,
			"pax": int(res.adults or 0) + int(res.children or 0),
			"meal_plan": res.get("meal_plan"),
			"company": res.company,
			"group_booking": res.get("group_booking"),
			"booked_by_name": res.get("booked_by_name"),
			"booked_by_phone": res.get("booked_by_phone"),
			"contact_preference": res.get("contact_preference"),
		},
		"summary_by_head": sorted(by_head.values(), key=lambda h: -h["total"]),
		"tax_summary": tax_summary,
		# kept for callers written against the pre-v2 shape
		"gst_summary": gst_summary,
	}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Kamra Agent")
def reprint_fiscal_folio(folio: str, reason: str | None = None):
	"""Reprint a settled folio's tax invoice (OPERA Folio History, Reprint).

	Under the ERPNext accounting bridge that is the fiscal document: the ERP
	Sales Invoice number with its fiscal identifiers. A fiscal document that
	is still pending, from a test fiscal service, or whose status can't be
	read is not a tax invoice, so it is not reprinted as one. Each reprint is
	logged with its revision."""
	data = folio_invoice(folio)
	if data["folio"]["status"] != "Closed":
		frappe.throw(_("Only a settled folio has an invoice to reprint."))
	fiscal, document = data["fiscal"], data["document"]
	if fiscal and not document["is_final"]:
		from kamra.fiscal import assert_reprintable
		assert_reprintable(fiscal)
	revision = frappe.db.count("Folio Reprint", {"folio": folio}) + 1
	reprint = frappe.get_doc({
		"doctype": "Folio Reprint",
		"property": data["folio"]["property"],
		"folio": folio,
		"invoice_number": document["number"],
		"reprint_of": "Guest Folio",
		"printed_on": now_datetime(),
		"printed_by": frappe.session.user,
		"reason": reason,
		"revision": revision,
	}).insert(ignore_permissions=True)
	document["reprint"] = revision
	return {**data, "reprint": reprint.name, "revision": revision}


def _folio_statement(lines: list, payments: list, places: int = 2) -> list[dict]:
	"""Charges and payments in date order with a running balance: charges
	(with tax) and refunds in `charges`, payments, allowances and reversals
	in `credits`."""
	from decimal import Decimal

	from kamra.ledger import _round_currency

	rows = []
	for ln in lines:
		total = _round_currency(ln["total"] or 0, places)
		rows.append((ln["posting_date"], 0, {
			"date": ln["posting_date"], "row": ln["row"], "kind": "Charge",
			"charge_type": ln["charge_type"], "description": ln["description"],
			"reference": ln.get("corrects_row"), "method": None,
			"transaction_code": ln["transaction_code"], "gl_code": ln["gl_code"],
			"charges": max(total, 0), "credits": max(-total, 0),
			"tax": _round_currency(ln["gst_amount"] or 0, places)}))
	for p in payments:
		amt = _round_currency(p["amount"] or 0, places)
		rows.append((p["posting_date"], 1, {
			"date": p["posting_date"], "row": p["row"], "kind": p["payment_kind"],
			"charge_type": None, "description": f"{p['payment_kind']} {p['mode'] or ''}".strip(),
			"reference": p["reference"], "method": p["mode"],
			"transaction_code": p["transaction_code"], "gl_code": p["gl_code"],
			"charges": max(-amt, 0), "credits": max(amt, 0), "tax": 0.0}))
	balance, out = Decimal(0), []
	for _d, _o, r in sorted(rows, key=lambda x: (x[0], x[1])):
		balance += Decimal(str(r["charges"])) - Decimal(str(r["credits"]))
		out.append({**r, "balance": _round_currency(balance, places)})
	return out


def _folio_allocations(folio: str, property: str | None = None) -> list[dict]:
	"""Company payments (city ledger) that cleared this bill, and how much
	of each."""
	from decimal import Decimal

	from kamra.ledger import _currency_places, _round_currency

	prop = property or frappe.db.get_value("Folio", folio, "property")
	places = max(_currency_places(prop), 2)
	eps = Decimal("0.5").scaleb(-places)
	by_payment: dict[str, Decimal] = {}
	for e in frappe.get_all("Folio Ledger Entry", filters={
			"folio": folio, "source_doctype": "City Ledger Entry"},
			fields=["source_name", "credit", "debit"]):
		by_payment[e.source_name] = by_payment.get(e.source_name, Decimal(0)) + \
			Decimal(str(e.credit or 0)) - Decimal(str(e.debit or 0))
	if not by_payment:
		return []
	cle = {c.name: c for c in frappe.get_all(
		"City Ledger Entry", filters={"name": ("in", list(by_payment)), "kind": "Payment"},
		fields=["name", "account", "posting_date", "reference"])}
	return [{"payment": n, "account": cle[n].account,
	         "posting_date": str(cle[n].posting_date),
	         "reference": cle[n].reference, "amount": _round_currency(a, places)}
	        for n, a in by_payment.items() if n in cle and abs(a) > eps]


def _invoice_guest(res) -> dict:
	"""Who the bill is addressed to, as the guest register knows them -
	nationality included, because a foreign guest's invoice needs it and
	so does the police report."""
	g = frappe.db.get_value(
		"Guest", res.guest,
		["full_name", "phone", "email", "nationality", "address_line",
		 "city", "id_type", "id_number"], as_dict=True) or {}
	return {
		"guest_id": res.guest,
		"name": g.get("full_name") or res.guest_name,
		"phone": g.get("phone"), "email": g.get("email"),
		"nationality": g.get("nationality"),
		"address": ", ".join(filter(None, [g.get("address_line"), g.get("city")])),  # nosemgrep: frappe-no-functional-code -- drops empty address parts
		"id_type": g.get("id_type"),
	}


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Night Auditor", "Kamra Agent")
def run_night_audit(property: str, business_date: str | None = None,
                    acknowledge=None, note: str | None = None):
	"""Close the current business date. Refused while a blocking pre-check
	(kamra.night_audit.night_audit_precheck) has items, unless each is
	listed in `acknowledge` (check keys) with a `note` giving the reason."""
	# Night audit is a front-desk / night-auditor ritual, not admin-only.
	from kamra.folio import run_night_audit as _run
	from kamra.night_audit import record_run
	return record_run(property, "Manual", lambda: _run(
		property, business_date, acknowledge=acknowledge, note=note, mode="manual"))


@frappe.whitelist()
@require_roles()
def gstr1_rows(from_date: str, to_date: str, property: str | None = None):
	"""Invoice-level rows for a GSTR-1 style export (v0: B2C summary).
	Filter by property - each GSTIN files its own return. The return is the
	India pack's: a property elsewhere is refused, and without a property
	only the user's India properties are included."""
	from kamra import localization as loc
	if property:
		return loc.tax_return_rows(loc.pack_for(property), property,
		                           from_date, to_date)
	scope = _property_scope()
	names = scope[1] if scope else frappe.get_all("Property", pluck="name")
	india = loc.pack_for_country("India")
	return loc.tax_return_rows(india, [p for p in names if loc.pack_for(p) is india],
	                           from_date, to_date)


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def guests_with_stats(search: str | None = None):
	"""Guest list with stay stats - the CRM index."""
	from kamra.authz import guest_scope_sql
	scope_sql, params = guest_scope_sql("g")
	where = f"WHERE {scope_sql}"
	join_scope = (" AND r.property IN %(scope_properties)s"
	              if "scope_properties" in params else "")
	if search:
		where += " AND (g.full_name LIKE %(q)s OR g.phone LIKE %(q)s)"
		params["q"] = f"%{search}%"
	return frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- values are parameterized; interpolated text is a constant or whitelisted identifier, not user input
		f"""
		SELECT
			g.name, g.full_name, g.phone, g.email, g.vip,
			COUNT(r.name) AS bookings,
			SUM(CASE WHEN r.status IN ('Checked In','Checked Out') THEN 1 ELSE 0 END) AS stays,
			COALESCE(SUM(CASE WHEN r.status != 'Cancelled' THEN r.nights ELSE 0 END), 0) AS nights,
			COALESCE(SUM(CASE WHEN r.status != 'Cancelled' THEN r.amount_after_tax ELSE 0 END), 0) AS lifetime_value,
			MAX(r.check_in_date) AS last_stay
		FROM `tabGuest` g
		LEFT JOIN `tabReservation` r ON r.guest = g.name{join_scope}
		{where}
		GROUP BY g.name
		ORDER BY lifetime_value DESC, g.modified DESC
		LIMIT 200
		""",
		params,
		as_dict=True,
	)


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def guest_search(q: str):
	"""Typeahead for attaching a booking to an existing profile."""
	q = (q or "").strip()
	if len(q) < 2:
		return []
	from kamra.authz import guest_scope_sql
	scope_sql, params = guest_scope_sql("g")
	params["q"] = f"%{q}%"
	return frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- values are parameterized; the interpolated scope clause is a constant from authz
		f"""
		SELECT g.name, g.full_name, g.phone, g.email, g.vip, g.blacklisted,
		       g.blacklist_reason,
		       COUNT(r.name) AS stays, MAX(r.check_in_date) AS last_stay
		FROM `tabGuest` g
		LEFT JOIN `tabReservation` r
		       ON r.guest = g.name AND r.status IN ('Checked In', 'Checked Out')
		WHERE (g.full_name LIKE %(q)s OR g.phone LIKE %(q)s) AND {scope_sql}
		GROUP BY g.name
		ORDER BY stays DESC, g.modified DESC
		LIMIT 8
		""",
		params, as_dict=True,
	)


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def guest_alert(guest: str | None = None, phone: str | None = None):
	"""Is the guest a booking would attach to blacklisted? The desk asks
	before booking so the warning shows up front, not as a refusal at the
	end. The chosen profile, else the profile with this phone number (which
	create_booking reuses only when the typed name matches too)."""
	from kamra import phone as phone_mod
	name = guest or (frappe.db.get_value("Guest", {"phone": phone_mod.matching(phone)})
	                 if phone else None)
	if not name:
		return {"guest": None, "blacklisted": 0, "blacklist_reason": None}
	g = frappe.db.get_value(
		"Guest", name, ["name", "full_name", "blacklisted", "blacklist_reason"],
		as_dict=True)
	if not g:
		return {"guest": None, "blacklisted": 0, "blacklist_reason": None}
	from kamra.authz import assert_guest_access
	try:
		assert_guest_access(g.name)
		visible = True
	except frappe.PermissionError:
		visible = False
	return {
		"guest": g.name if visible else None,
		"full_name": g.full_name if visible else None,
		"blacklisted": 1 if g.blacklisted else 0,
		"blacklist_reason": g.blacklist_reason if visible else None,
		"can_override": 1 if _is_admin() else 0,
	}


def _is_admin() -> bool:
	from kamra.authz import ADMIN
	return bool(set(ADMIN) & set(frappe.get_roles()))


@frappe.whitelist(methods=["POST"])
@require_roles(scope={"guest": "Guest"})
def set_guest_blacklist(guest: str, blacklisted: int, reason: str):
	"""Blacklist a guest, or lift it. Hotel Admin only; a reason is required
	either way and lands in the activity log with the before/after."""
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("A reason is required."))
	flag = 1 if cint(blacklisted) else 0
	before = frappe.db.get_value(
		"Guest", guest, ["blacklisted", "blacklist_reason"], as_dict=True)
	if before is None:
		frappe.throw(_("Guest profile {0} not found.").format(guest))
	frappe.db.set_value("Guest", guest, {
		"blacklisted": flag,
		# the standing reason is the one that put them on the list
		"blacklist_reason": reason[:500] if flag else None,
	})
	last_property = frappe.db.get_value(
		"Reservation", {"guest": guest}, "property", order_by="creation desc")
	from kamra.savings import log_action
	log_action(
		action_type="guest_blacklisted" if flag else "guest_blacklist_removed",
		reference_doctype="Guest",
		reference_name=guest,
		property=last_property,
		rationale=reason,
		before_snapshot={"blacklisted": cint(before.blacklisted),
		                 "blacklist_reason": before.blacklist_reason},
		after_snapshot={"blacklisted": flag,
		                "blacklist_reason": reason if flag else None},
	)
	return {"guest": guest, "blacklisted": flag,
	        "blacklist_reason": reason if flag else None}


_GUEST_LINKS = [  # every doctype that points at a Guest
	("Reservation", "guest"), ("Folio", "guest"),
	("Service Ticket", "guest"), ("Lost And Found Item", "guest"),
	("Security Deposit", "guest"),
	("Venue Booking", "customer"),  # banquet uses customer, not guest
	("WhatsApp Message", "guest"),
]


@frappe.whitelist()
@require_roles(scope={"source": "Guest", "target": "Guest"})
def merge_guests(source: str, target: str):
	"""Merge a duplicate profile into the surviving one: every linked
	document is repointed, missing contact fields are copied over, and
	the duplicate is deleted. Money is untouched - folios keep their
	lines and totals."""
	if source == target:
		frappe.throw("Pick two different profiles to merge.")
	src = frappe.get_doc("Guest", source)
	dst = frappe.get_doc("Guest", target)

	moved = {}
	for doctype, field in _GUEST_LINKS:
		if not frappe.db.exists("DocType", doctype):
			continue
		if not frappe.get_meta(doctype).has_field(field):
			continue
		rows = frappe.get_all(doctype, filters={field: source}, pluck="name")
		for name in rows:
			frappe.db.set_value(doctype, name, field, target,
			                    update_modified=False)
		if rows:
			moved[doctype] = len(rows)
	# denormalized guest_name on stays and bills follows the survivor
	for doctype in ("Reservation", "Folio"):
		frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- values are parameterized; interpolated text is a constant or whitelisted identifier, not user input
			f"UPDATE `tab{doctype}` SET guest_name = %s WHERE guest = %s",
			(dst.full_name, target))

	# fill the survivor's blanks from the duplicate; strictest flags win
	for field in ("phone", "email", "id_type", "id_number", "nationality",
	              "address_line", "city", "guest_notes"):
		if not dst.get(field) and src.get(field):
			dst.set(field, src.get(field))
	if src.vip:
		dst.vip = 1
	if src.blacklisted:
		dst.blacklisted = 1
		dst.blacklist_reason = dst.blacklist_reason or src.blacklist_reason
	dst.save(ignore_permissions=True)
	frappe.delete_doc("Guest", source, ignore_permissions=True)

	from kamra.savings import log_action
	log_action("merge_guests", "Guest", target,
	           rationale=f"Merged {source} into {target}: "
	                     + ", ".join(f"{k}×{v}" for k, v in moved.items()))
	return {"target": target, "moved": moved}


@frappe.whitelist()
@require_roles(scope={"guest": "Guest"})
def anonymize_guest(guest: str):
	"""Right to erasure (DPDP s.12): see kamra.privacy.erase_guest. Stays
	and bills stay for the books. Irreversible."""
	from kamra.privacy import erase_guest

	return erase_guest(guest, reason="request")


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent", scope={"guest": "Guest"})
def guest_journey(guest: str):
	"""One guest's full story: profile, stats, chronological timeline.
	This is the CRM detail view - and the context an AI concierge loads
	before speaking to a returning guest."""
	from kamra.guest_documents import guest_documents

	doc = frappe.get_doc("Guest", guest)

	filters = {"guest": guest}
	scope = _property_scope()
	if scope:  # a restricted user sees only the stays at their properties
		filters["property"] = scope
	reservations = frappe.get_all(
		"Reservation",
		filters=filters,
		fields=[
			"name", "status", "source", "channel", "room", "room_type",
			"check_in_date", "check_out_date", "nights", "adults", "children",
			"amount_after_tax", "discount_amount", "special_requests",
			"booking_type", "company", "creation",
			"actual_check_in", "actual_check_out", "property",
		],
		order_by="check_in_date desc",
	)

	live = [r for r in reservations if r.status != "Cancelled"]
	stats = {
		"bookings": len(reservations),
		"stays": sum(1 for r in reservations if r.status in ("Checked In", "Checked Out")),
		"nights": sum(r.nights or 0 for r in live),
		"lifetime_value": float(sum(r.amount_after_tax or 0 for r in live)),
		"first_seen": str(min((r.creation for r in reservations), default="")),
	}

	timeline = []
	for r in reservations:
		timeline.append({
			"ts": str(r.creation),
			"type": "booking",
			"title": f"Booked {r.room_type.split('-')[-1] if r.room_type else ''}"
			         f" · {r.nights} night{'s' if (r.nights or 0) != 1 else ''}",
			"detail": f"{r.check_in_date} → {r.check_out_date}"
			          f" · via {r.channel or r.source}"
			          + (f" · {r.company}" if r.company else ""),
			"amount": float(r.amount_after_tax or 0),
			"reference": r.name,
		})
		if r.actual_check_in:
			timeline.append({
				"ts": str(r.actual_check_in), "type": "check_in",
				"title": f"Checked in · Room {r.room.split('-')[-1] if r.room else '-'}",
				"detail": r.special_requests or "", "reference": r.name,
			})
		if r.actual_check_out:
			timeline.append({
				"ts": str(r.actual_check_out), "type": "check_out",
				"title": "Checked out",
				"detail": f"Folio {_currency_symbol(r.property)}"
				          f"{float(r.amount_after_tax or 0):,.0f}",
				"reference": r.name,
			})
		if r.status == "Cancelled":
			timeline.append({
				"ts": str(r.creation), "type": "cancelled",
				"title": "Booking cancelled", "detail": r.name,
				"reference": r.name,
			})

	# agent touches on this guest's reservations
	res_names = [r.name for r in reservations]
	if res_names:
		for log in frappe.get_all(
			"Agent Action Log",
			filters={
				"reference_doctype": "Reservation",
				"reference_name": ("in", res_names),
			},
			fields=["creation", "agent_name", "actor", "action_type",
			        "rationale", "minutes_saved", "action_channel"],
			order_by="creation desc",
			limit=50,
		):
			# a person at the desk is the actor and leaves agent_name empty
			who = log.agent_name or (
				frappe.utils.get_fullname(log.actor) if log.actor else "") \
				or "Staff"
			what = (log.action_type or "").replace("_", " ")
			timeline.append({
				"ts": str(log.creation), "type": "agent",
				"title": f"{who} · {what[:1].upper()}{what[1:]}",
				"detail": log.rationale or "",
				"channel": log.action_channel,
			})

	timeline.sort(key=lambda e: e["ts"], reverse=True)

	return {
		"guest": {
			"name": doc.name, "full_name": doc.full_name,
			"first_name": doc.first_name, "phone": doc.phone,
			"email": doc.email, "vip": doc.vip,
			"nationality": doc.nationality, "id_type": doc.id_type,
			"id_number": doc.id_number,
			"id_place_of_issue": doc.get("id_place_of_issue"),
			"id_issue_date": str(doc.get("id_issue_date") or "") or None,
			"id_expiry_date": str(doc.get("id_expiry_date") or "") or None,
			"blacklisted": doc.get("blacklisted"),
			"blacklist_reason": doc.get("blacklist_reason"),
			"marketing_consent": doc.get("marketing_consent") or 0,
			"marketing_consent_date": doc.get("marketing_consent_date"),
			"marketing_consent_source": doc.get("marketing_consent_source"),
			"address": ", ".join(filter(None, [  # nosemgrep: frappe-no-functional-code -- filter(None, ...) drops empty address parts; equivalent to a comprehension
				doc.get("address_line"), doc.get("city")])),
			"notes": doc.guest_notes,
		},
		"stats": stats,
		"documents": guest_documents(guest, scope),
		"reservations": reservations,
		"timeline": timeline,
	}


def _currency_symbol(property: str | None) -> str:
	"""The property's currency symbol from its country pack."""
	if not property:
		return ""
	from kamra.localization import pack_for
	return pack_for(property).locale(
		frappe.get_cached_doc("Property", property)).get("currency_symbol", "")


def _property_scope():
	"""Filter value for a call that left `property` out: the user's own
	properties when they're restricted, else None (no filter)."""
	from kamra.authz import restricted_properties
	allowed = restricted_properties()
	return ("in", sorted(allowed)) if allowed is not None else None


@frappe.whitelist()
def my_properties():
	"""Properties the current user may work with. frappe.get_list applies
	User Permissions, so a property-restricted user sees only theirs."""
	filters = {"disabled": 0}
	scope = _property_scope()
	if scope is not None:
		filters["name"] = scope
	props = frappe.get_list(
		"Property",
		filters=filters,
		fields=["name", "property_name", "city", "business_date"],
		order_by="property_name asc",
	)
	from kamra.business_date import get_business_date
	from kamra.no_show import property_now
	for row in props:
		row["business_date"] = str(row.business_date or get_business_date(row.name))
		row["calendar_date"] = str(property_now(row.name).date())
	return props


def _stay_balances(names: list) -> dict:
	"""What each stay costs, has paid and still owes - one rule set shared
	by the Today board and the reservation drawer so they never disagree.

	Only the Guest folio counts (summed, so a part-settled stay with a fresh
	folio still adds up). The group master is the company's bill and Extra /
	Company folios are routed elsewhere, so neither is this guest's balance.

	- Checked In / Checked Out / No Show / Cancelled with a Guest folio: the
	  folio is the bill.
	- No Show / Cancelled with no folio: nothing is owed; any advance taken
	  is a refund due.
	- Confirmed with an early deposit folio: the booking total is the bill,
	  the folio holds what was paid.
	- Confirmed with no folio: the booking-time advance is all we know."""
	names = list({n for n in names if n})
	if not names:
		return {}
	resv = {r.name: r for r in frappe.get_all(
		"Reservation", filters={"name": ("in", names)},
		fields=["name", "property", "status", "amount_after_tax", "advance_paid",
		        "check_out_date"])}
	folios = _guest_folio_sums(names)
	# A guest due out has nights the night audit has not posted yet;
	# check-out posts them before it counts what is owed, so the board does
	# the same (and throws the postings away) rather than show a balance
	# that check-out then refuses.
	today = getdate(nowdate())
	due_out = [n for n, r in resv.items() if r.status == "Checked In"
	           and r.check_out_date and getdate(r.check_out_date) <= today]
	unavailable: set = set()
	if due_out:
		projected, unavailable = _checkout_projection(due_out)
		folios.update(projected)
	from kamra.ledger import _currency_places, _money, _round_currency
	out = {}
	for name, res in resv.items():
		folio = folios.get(name)
		places = max(_currency_places(res.get("property")), 2)
		total_booked = float(res.amount_after_tax or 0)
		adv = float(res.advance_paid or 0)
		ended = res.status in ("No Show", "Cancelled")
		if folio and (ended or res.status in ("Checked In", "Checked Out")):
			m = {"total": float(folio.total), "paid": float(folio.paid),
			     "due": float(folio.due), "has_folio": True, "source": "folio"}
		elif ended:
			m = {"total": 0.0, "paid": adv, "due": 0.0, "has_folio": False,
			     "source": "none"}
			if adv > 0:
				m["refund_due"] = adv
		elif folio:
			paid = float(folio.paid)
			m = {"total": total_booked, "paid": paid,
			     "due": max(0.0, _round_currency(_money(total_booked) - _money(paid), places)),
			     "has_folio": True, "source": "booking"}
		else:
			m = {"total": total_booked, "paid": adv,
			     "due": max(0.0, _round_currency(_money(total_booked) - _money(adv), places)),
			     "has_folio": False, "source": "booking"}
		# the posted figures stand in: check-out's own sheet says why
		m["projection_unavailable"] = name in unavailable
		out[name] = m
	return out


def _guest_folio_sums(names: list) -> dict:
	"""Guest-folio total / paid / balance per reservation."""
	if not names:
		return {}
	return {row.reservation: row for row in frappe.db.sql(
		"""
		SELECT reservation,
		       COALESCE(SUM(grand_total), 0) AS total,
		       COALESCE(SUM(payments_total), 0) AS paid,
		       COALESCE(SUM(balance), 0) AS due
		FROM `tabFolio`
		WHERE reservation IN %(names)s AND folio_type = 'Guest'
		GROUP BY reservation
		""",
		{"names": list(names)}, as_dict=True,
	)}


PROJECTION_ERROR = "Kamra: check-out balance projection"


def _checkout_projection(names: list) -> tuple[dict, set]:
	"""Guest-folio sums as they will stand at check-out: every remaining
	night posted by the same writer check-out uses (post_remaining_nights),
	inside a savepoint that is rolled back. Nothing is kept - not the
	charges, the ledger lines, nor the realtime pings their saves queue -
	so this is a read.

	Returns (sums, unavailable): a stay whose nights could not be posted
	keeps its posted figures and is named in `unavailable`. The boards poll
	this every 30 seconds, so a failure is logged once per stay, business
	date and error class, not on every poll."""
	from kamra.business_date import get_business_date
	from kamra.folio import post_remaining_nights

	db = frappe.db
	queues = (db.before_commit, db.after_commit, db.after_rollback)
	saved = [list(q._functions) for q in queues]
	had_log = hasattr(frappe.local, "_realtime_log")
	saved_log = list(getattr(frappe.local, "_realtime_log", None) or [])
	sp = "kamra_checkout_projection"
	failed: dict = {}
	sums: dict = {}
	db.savepoint(sp)
	try:
		for name in names:
			res = frappe.get_doc("Reservation", name)
			db.savepoint(f"{sp}_stay")
			try:
				post_remaining_nights(res)
			except Exception as e:
				db.rollback(save_point=f"{sp}_stay")
				failed[name] = (res.property, type(e).__name__, frappe.get_traceback())
		ok = [n for n in names if n not in failed]
		sums = _guest_folio_sums(ok)
	finally:
		db.rollback(save_point=sp)
		for q, fns in zip(queues, saved):
			q._functions.clear()
			q._functions.extend(fns)
		if had_log:
			frappe.local._realtime_log = saved_log
		elif hasattr(frappe.local, "_realtime_log"):
			del frappe.local._realtime_log
	for name, (prop, error, trace) in failed.items():
		title = f"{PROJECTION_ERROR} {get_business_date(prop)} {error}"
		if frappe.db.exists("Error Log", {"reference_doctype": "Reservation",
		                                  "reference_name": name, "method": title}):
			continue
		frappe.log_error(title=title, message=trace,
		                 reference_doctype="Reservation", reference_name=name)
	return sums, set(failed)


def _stay_balance(reservation: str) -> dict:
	"""_stay_balances for one booking."""
	return _stay_balances([reservation]).get(reservation) or {
		"total": 0.0, "paid": 0.0, "due": 0.0, "has_folio": False,
		"source": "none"}


@frappe.whitelist()
@require_roles("Front Desk", "Night Auditor", "Kamra Agent", "Finance", "Revenue Manager")
def front_desk_snapshot(property: str | None = None, date: str | None = None):
	"""Everything the front desk needs for one day, in one call."""
	from kamra.authz import assert_property_access
	from kamra.business_date import get_business_date
	from kamra.movements import completed_on, departure_state
	if not property:
		props = my_properties()
		if len(props) != 1:
			frappe.throw(_("Select a property to view its business day."))
		property = props[0]["name"]
		assert_property_access(property)
	business_date = get_business_date(property)
	date = str(date or business_date)
	res_filters = {"property": property, "check_in_date": date, "status": "Confirmed"}
	inh_filters = {"property": property, "status": "Checked In"}
	room_filters = {"property": property}

	res_fields = [
		"name", "guest_name", "room_type", "room", "status", "source",
		"check_in_date", "check_out_date", "nights", "adults", "children",
		"special_requests", "channel", "precheckin_status", "eta",
		"precheckin_token", "booked_by_name", "booked_by_phone",
		"booker_relation", "contact_preference", "company",
	]

	arrivals = frappe.get_all(
		"Reservation", filters=res_filters, fields=res_fields,
		order_by="creation asc",
	)
	in_house = frappe.get_all(
		"Reservation", filters=inh_filters, fields=res_fields,
		order_by="room asc",
	)

	departures = [r for r in in_house if departure_state(r, date) == "due_today"]
	overdue = [r for r in in_house if departure_state(r, date) == "overdue"]
	stay = frappe.qb.DocType("Reservation")
	completed = (frappe.qb.from_(stay).select(*(stay[field] for field in res_fields))
	             .where((stay.property == property) & completed_on(stay, date, date))
	             .orderby(stay.room).run(as_dict=True))

	# payment state per stay - the same rules as the reservation drawer
	all_rows = arrivals + in_house + completed
	money = _stay_balances([r.name for r in all_rows])
	for r in all_rows:
		m = money.get(r.name) or {}
		r["paid_total"] = m.get("paid", 0.0)
		r["balance_due"] = m.get("due", 0.0)
	rooms = frappe.get_all(
		"Room",
		filters=room_filters,
		fields=[
			"name", "room_number", "room_type", "floor",
			"housekeeping_status", "occupancy_status",
		],
		order_by="room_number asc",
	)

	numbers = {r.name: r.room_number for r in rooms}
	for row in all_rows:
		row["room_number"] = numbers.get(row.room)

	minutes = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(minutes_saved), 0)
		FROM `tabAgent Action Log`
		WHERE DATE(creation) >= %(since)s
		""",
		{"since": add_days(date, -30)},
	)[0][0]

	return {
		"date": date,
		# an arrival due on or before it can be marked a no-show at the desk
		"business_date": business_date,
		"departures_due": departures,
		"departures_overdue": overdue,
		"checked_out": completed,
		"arrivals": arrivals,
		"departures": departures,
		"in_house": in_house,
		"rooms": rooms,
		"minutes_saved_30d": float(minutes or 0),
	}


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent", "Finance", "Revenue Manager")
def find_reservations(property: str, query: str | None = None,
                      status: str | None = None, limit: int = 20):
	"""Search reservations by guest name, room number, or reference - optionally
	filtered by status. The way to resolve a room number or a name to an actual
	reservation before acting on it."""
	filters = {"property": property}
	if status:
		filters["status"] = status
	or_filters = None
	if query and query.strip():
		q = f"%{query.strip()}%"
		or_filters = [
			["name", "like", q],
			["guest_name", "like", q],
			["room", "like", q],
		]
	return frappe.get_all(
		"Reservation",
		filters=filters,
		or_filters=or_filters,
		fields=[
			"name", "guest", "guest_name", "room", "room_type", "status",
			"check_in_date", "check_out_date", "nights", "adults", "children",
			"amount_after_tax", "advance_paid",
		],
		order_by="check_in_date desc",
		limit=int(limit or 20),
	)


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Revenue Manager", "Kamra Agent")
def find_invoices(property: str, query: str | None = None, limit: int = 8):
	"""Resolve an invoice number (or partial) to its folio and stay, so the
	command palette can jump straight from 'INV-KDP-26-00042' to the bill."""
	q = (query or "").strip()
	if not q:
		return []
	rows = frappe.get_all(
		"Folio",
		filters={"property": property, "invoice_number": ["like", f"%{q}%"]},
		fields=["name", "invoice_number", "reservation", "guest",
		        "guest_name", "grand_total", "status", "folio_type"],
		order_by="modified desc",
		limit=int(limit or 8),
	)
	return rows


STAY_INSTRUCTION_FOR = ("Front Desk", "Housekeeping", "F&B", "Concierge",
                         "Transport", "Maintenance", "Security")


def _stay_instruction_rows(reservation: str) -> list:
	return frappe.get_all(
		"Stay Instruction",
		filters={"parent": reservation, "parenttype": "Reservation",
		         "parentfield": "instructions"},
		fields=["name", "department", "instruction", "status", "done_by",
		        "done_on", "owner", "creation"],
		order_by="idx asc")


@frappe.whitelist()
@require_roles("Front Desk", "Housekeeping", "Kamra Agent")
def stay_instructions(reservation: str):
	"""The special instructions on a stay (a VIP amenity, a 6am wake-up, a
	no-feather pillow) - each for one department, Open until someone marks
	it Done."""
	if not frappe.db.exists("Reservation", reservation):
		frappe.throw(_("Reservation {0} not found.").format(reservation),
		             frappe.DoesNotExistError)
	return _stay_instruction_rows(reservation)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def add_stay_instruction(reservation: str, department: str, instruction: str):
	"""Add an instruction to a stay for one department. Written as its own
	child row so it never re-saves (and re-validates) the whole booking."""
	instruction = (instruction or "").strip()
	if not instruction:
		frappe.throw(_("Write the instruction."))
	if department not in STAY_INSTRUCTION_FOR:
		frappe.throw(_("Instruction must be for one of: {0}.").format(
			", ".join(STAY_INSTRUCTION_FOR)))
	res = frappe.db.get_value("Reservation", reservation,
	                          ["name", "property", "status"], as_dict=True)
	if not res:
		frappe.throw(_("Reservation {0} not found.").format(reservation),
		             frappe.DoesNotExistError)
	if res.status in ("Cancelled", "Checked Out", "No Show"):
		frappe.throw(_("This stay is {0} - no new instructions.").format(res.status))
	idx = (frappe.db.sql(
		"""SELECT MAX(idx) FROM `tabStay Instruction`
		WHERE parent=%s AND parenttype='Reservation' AND parentfield='instructions'""",
		reservation)[0][0] or 0) + 1
	row = frappe.get_doc({
		"doctype": "Stay Instruction", "parent": reservation,
		"parenttype": "Reservation", "parentfield": "instructions",
		"idx": idx, "department": department,
		"instruction": instruction[:1000], "status": "Open",
	})
	row.db_insert()
	from kamra.savings import log_action
	log_action("stay_instruction", "Reservation", reservation, res.property,
	           rationale=f"{department}: {instruction[:120]}")
	return _stay_instruction_rows(reservation)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Housekeeping", "Kamra Agent")
def set_stay_instruction_status(reservation: str, row: str, status: str):
	"""Mark one instruction Done (stamping who and when), or reopen it."""
	if status not in ("Open", "Done"):
		frappe.throw(_("Status must be Open or Done."))
	if not frappe.db.exists("Stay Instruction", {
			"name": row, "parent": reservation, "parenttype": "Reservation"}):
		frappe.throw(_("That instruction is not on this stay."))
	done = status == "Done"
	text = frappe.db.get_value("Stay Instruction", row, "instruction") or ""
	frappe.db.set_value("Stay Instruction", row, {
		"status": status,
		"done_by": frappe.session.user if done else None,
		"done_on": now_datetime() if done else None,
	})
	from kamra.savings import log_action
	log_action("stay_instruction", "Reservation", reservation,
	           frappe.db.get_value("Reservation", reservation, "property"),
	           rationale=f"{status}: {text[:120]}")
	return _stay_instruction_rows(reservation)


def _checkin_backfill(res) -> list[dict]:
	"""Soft-required check-in steps still open (Checked In or Confirmed).
	Empty when nothing applies or everything is done / skipped."""
	if res.status not in ("Confirmed", "Checked In"):
		return []
	from kamra.checkin import evaluate, soft_pending
	steps, _ctx = evaluate(res)
	return [{"step": s["step_key"], "title": s["title"], "tip": s.get("tip") or ""}
	        for s in soft_pending(steps)]


def _detail_rate_plans(res) -> list[dict]:
	"""The plans Amend stay can move a stay to (OPERA Stay Details' Rate
	Code list): the property's enabled non-tariff plans, and the stay's own
	when it has since been disabled, so the picker can still show where it is.
	Contract-tariff stays keep their locked tariff and cannot switch rate plans
	in Amend stay, so they return no selectable list."""
	from kamra.channel_manager import _is_tariff_owned_stay, _rate_plan_has_tariff
	if _is_tariff_owned_stay(res):
		return []
	current = res.rate_plan
	fields = ["name", "rate_plan_name", "code", "rate_category"]
	plans = [
		p for p in frappe.get_all(
			"Rate Plan", filters={"property": res.property, "disabled": 0},
			fields=fields, order_by="rate_plan_name asc, name asc",
		)
		if not _rate_plan_has_tariff(p["name"])
	]
	if current and all(p["name"] != current for p in plans):
		own = frappe.db.get_value("Rate Plan", current, fields, as_dict=True)
		if own:
			plans.insert(0, own)
	return plans


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent", "Finance", "Revenue Manager")
def reservation_detail(reservation: str):
	"""Everything about one booking in a single call - stay, money, guest,
	booker and the actions currently available. Powers the reservation drawer."""
	from kamra.business_date import get_business_date
	from kamra.folio import charged_nights
	from kamra.kamra.doctype.reservation.reservation import uses_member_rooms
	from kamra.pricing import saved_contract_tariff
	res = frappe.get_doc("Reservation", reservation)
	night_charged = set(charged_nights(res.name)) if res.get("night_rates") else set()

	# guest identity + stay history
	guest = None
	if res.guest:
		g = frappe.db.get_value(
			"Guest", res.guest,
			["name", "full_name", "phone", "email", "vip", "blacklisted"],
			as_dict=True,
		)
		if g:
			g["stays"] = frappe.db.count("Reservation", {
				"guest": res.guest, "status": ("in", ["Checked In", "Checked Out"])})
			g["last_stay"] = frappe.db.get_value(
				"Reservation", {"guest": res.guest, "status": "Checked Out",
				                "name": ("!=", res.name)},
				"check_out_date", order_by="check_out_date desc")
			guest = g

	money = _stay_balance(res.name)
	# the drawer's "Open folio" link - the newest guest folio (a
	# part-settled stay has several; the fresh one is the open one)
	folio = frappe.db.get_value(
		"Folio", {"reservation": reservation, "folio_type": "Guest"},
		["name"], as_dict=True, order_by="creation desc")

	booker = None
	if res.booked_by_name:
		booker = {
			"name": res.booked_by_name, "phone": res.booked_by_phone,
			"relation": res.booker_relation,
			"contact_preference": res.contact_preference,
		}

	cancellation = None
	if res.get("cancellation_number") or res.status == "Cancelled":
		cancellation = {
			"reason": res.get("cancellation_reason"),
			"note": res.get("cancellation_note"),
			"number": res.get("cancellation_number"),
			"fee": float(res.get("cancellation_fee") or 0),
			"cancelled_on": res.get("cancelled_on"),
		}

	# a stay in a group: the desk can check the whole group in from here
	group = None
	if res.get("group_booking"):
		group = {
			"name": res.group_booking,
			"group_name": frappe.db.get_value("Group Booking", res.group_booking, "group_name"),
			"rooms": frappe.db.count("Reservation", {
				"group_booking": res.group_booking,
				"status": ("in", ["Confirmed", "Checked In", "Checked Out"])}),
			"to_check_in": frappe.db.count("Reservation", {
				"group_booking": res.group_booking, "status": "Confirmed"}),
		}

	CHANNEL_REVIEW_REASONS = {
		"channel.modify_needs_review": "update_not_applied",
		"channel.cancel_needs_review": "update_not_applied",
		"channel.modify_discount_removed": "discount_removed",
		"channel.modify_discount_review": "discount_review",
	}
	channel_review_needed = int(res.get("channel_review_needed") or 0)
	channel_reviews = []
	if channel_review_needed:
		last_cleared = frappe.db.get_value(
			"Agent Action Log",
			{"reference_doctype": "Reservation", "reference_name": res.name,
			 "action_type": "channel.review_cleared"},
			"creation", order_by="creation desc")
		review_filters = {
			"reference_doctype": "Reservation",
			"reference_name": res.name,
			"action_type": ("in", list(CHANNEL_REVIEW_REASONS)),
		}
		if last_cleared:
			review_filters["creation"] = (">", last_cleared)
		channel_reviews = [
			{"name": row.name, "action_type": row.action_type,
			 "reason": CHANNEL_REVIEW_REASONS.get(row.action_type, "update_not_applied"),
			 "rationale": row.rationale, "executed_at": _dt(row.executed_at or row.creation)}
			for row in frappe.get_all(
				"Agent Action Log", filters=review_filters,
				fields=["name", "action_type", "rationale", "executed_at", "creation"],
				order_by="creation desc", limit_page_length=10)
		]
	channel_review_reason = (
		(res.get("channel_review_reason")
		 or (channel_reviews[0]["reason"] if channel_reviews else "update_not_applied"))
		if channel_review_needed else None
	)

	return {
		"name": res.name,
		**saved_contract_tariff(res),
		"group": group,
		"status": res.status,
		"source": res.source,
		"channel": res.channel,
		"channel_review_needed": channel_review_needed,
		"channel_review_reason": channel_review_reason,
		"channel_reviews": channel_reviews,
		"booking_type": res.booking_type,
		"property": res.property,
		"check_in_date": res.check_in_date,
		"check_out_date": res.check_out_date,
		"nights": res.nights,
		"adults": res.adults,
		"children": res.children,
		"room": res.room,
		"room_number": frappe.db.get_value("Room", res.room, "room_number") if res.room else None,
		"room_type": res.room_type,
		"room_type_name": frappe.db.get_value(
			"Room Type", res.room_type, "room_type_name") if res.room_type else None,
		# the plan's label ("Breakfast"), not its internal ID
		"meal_plan_name": (frappe.db.get_value(
			"Meal Plan", res.meal_plan, "label") or res.meal_plan)
			if res.meal_plan else None,
		"meal_plan": res.meal_plan,
		"rate_plan": res.rate_plan,
		"rate_plan_name": (frappe.db.get_value("Rate Plan", res.rate_plan, "rate_plan_name")
		                   or res.rate_plan) if res.rate_plan else None,
		"rate_category": res.get("rate_category") or None,
		"rate_plans": _detail_rate_plans(res),
		"special_requests": res.special_requests,
		"eta": res.eta,
		"late_arrival": int(res.get("late_arrival") or 0),
		# who is in the room and when each arrived / left (names only here;
		# IDs stay on the registration card)
		"occupants": [{"full_name": o.full_name, "arrived_at": _dt(o.get("arrived_at")),
		               "left_at": _dt(o.get("left_at"))}
		              for o in (res.get("occupants") or [])],
		"booked_check_out_date": str(res.get("booked_check_out_date") or "") or None,
		"early_departure_reason": res.get("early_departure_reason"),
		# the nightly rates confirmed at booking - what the folio bills
		"night_rates": [
			{"date": str(n.stay_date), "rate": float(n.rate or 0),
			 "demand_premium_pct": float(n.demand_premium_pct or 0),
			 "base_rate": float(n.get("base_rate") or n.rate or 0),
			 "discount_amount": float(n.get("discount_amount") or 0),
			 "discount_type": n.get("discount_type") or None,
			 "discount_value": float(n.get("discount_value") or 0),
			 # the night's own reason (Daily Details); blank follows the stay
			 "discount_reason": n.get("discount_reason") or None,
			 **_entry_figures(res.property, n),
			 # already on the folio: its discount is settled there (a folio
			 # allowance), so the Daily Details editor leaves it alone
			 "charged": str(n.stay_date) in night_charged}
			for n in (res.get("night_rates") or [])],
		# the desk's discount on the stay (each night's own is on its row)
		"discount": {"discount_type": res.get("discount_type") or None,
		             "discount_value": flt(res.get("discount_value")),
		             "discount_reason": res.get("discount_reason") or None,
		             "fixed_rate": cint(res.get("fixed_rate"))},
		"precheckin_status": res.precheckin_status,
		"precheckin_token": res.get("precheckin_token"),
		# the scan itself is never in this payload - id_document is only the
		# private path, fetched separately through id_document_image so one
		# role gate governs who ever sees the picture
		"id_document": res.get("id_document"),
		"id_document_source": res.get("id_document_source"),
		"id_document_on": str(res.get("id_document_on") or "") or None,
		"id_document_discarded": res.get("id_document_discarded") or 0,
		"precheckin_verified_by": res.get("precheckin_verified_by"),
		"precheckin_verified_on": str(res.get("precheckin_verified_on") or "") or None,
		"amount_after_tax": float(res.amount_after_tax or 0),
		"advance_paid": float(res.advance_paid or 0),
		"company": res.company,
		"travel_agent": res.travel_agent,
		"folio_name": folio.name if folio else None,
		"money": money,
		"deposit": _deposit_state(res),
		"security_deposit": _security_deposit_state(res),
		"guest": guest,
		"booker": booker,
		"cancellation": cancellation,
		"actions": {
			"can_check_in": res.status == "Confirmed" and (
				bool(res.room) or uses_member_rooms(res.property, res.room_type)),
			"can_check_out": res.status == "Checked In",
			"can_cancel": res.status in ("Confirmed", "Checked In"),
			# an arrival due on or before the business date (eod_mark_no_show)
			"can_no_show": res.status == "Confirmed"
			and getdate(res.check_in_date) <= getdate(get_business_date(res.property)),
			"can_amend": res.status in ("Confirmed", "Checked In"),
			# may fix a rate over a plan that takes no discount (_can_set_rate)
			"can_set_rate": _can_set_rate(),
			# undo checkout: same business day only (reinstate_checkout)
			"can_reinstate": res.status == "Checked Out" and bool(res.get("check_out_business_date"))
			and str(res.check_out_business_date) == str(get_business_date(res.property)),
			# undo check-in: same business day, nothing on the bill (reverse_check_in)
			"can_reverse_check_in": res.status == "Checked In"
			and not _reverse_check_in_blocker(res),
		},
		# Warnings are NOT capabilities, and they live apart on purpose. A
		# missing ID must never appear in `actions` - the guest is standing
		# at the desk with a physical card, and no arrival gets blocked over
		# an upload that didn't happen. The desk sees it and decides.
		"warnings": {
			"id_document_missing": not res.get("id_document")
			and not res.get("id_document_discarded"),
			"id_unverified": res.precheckin_status == "Submitted",
			"channel_review_needed": bool(channel_review_needed),
		},
		"checkin_backfill": _checkin_backfill(res),
		# the reason-code categories the screen offers (the property's settings)
		"reason_categories": {"discount": discount_reason_categories(res.property),
		                      "agreed_rate": agreed_rate_reason_categories(res.property)},
	}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def upload_id_document(reservation: str, data: str):
	"""Capture the guest's ID at the counter, when they never uploaded one.

	Same storage as the guest path, different gate: there the token proves
	ownership, here @require_roles does. This exists so that "never block
	check-in" has somewhere to go - the desk flags a missing document, then
	fixes it in the same breath instead of turning the guest away."""
	from kamra.id_documents import store_id_document

	res = frappe.get_doc("Reservation", reservation)
	store_id_document(res, data, source="Desk")
	from kamra.savings import log_action
	log_action("id_document_capture", "Reservation", res.name, res.property,
	           minutes_saved=1,
	           rationale=f"ID captured at the desk for {res.guest_name}")
	return {"ok": True}


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def id_document_image(reservation: str):
	"""The ID scan as a data URL, for the desk to eyeball.

	Why not just point an <img> at the /private/files/ URL: Frappe would
	authorise that through File.has_permission -> the Reservation's own
	doctype permissions. On a site whose Custom DocPerm rows omit Front Desk
	(as ours do - any custom perm on a doctype REPLACES all its standard
	perms), that check says no, and the desk gets a broken image while a
	Hotel Admin sees it. Kamra's authorization has always lived on the
	endpoint rather than the doctype (see authz.py), so the image is served
	the same way as everything else here: one gate, one rule, works for
	every role the app actually grants.
	"""
	import base64

	url = frappe.db.get_value("Reservation", reservation, "id_document")
	if not url:
		frappe.throw("No ID document on this booking.")
	name = frappe.db.get_value("File", {
		"attached_to_doctype": "Reservation", "attached_to_name": reservation,
		"attached_to_field": "id_document",
	})
	if not name:
		frappe.throw("That ID document is no longer stored.")
	content = frappe.get_doc("File", name).get_content()
	return {
		"data": "data:image/jpeg;base64," + base64.b64encode(content).decode(),
		"captured_on": str(frappe.db.get_value("Reservation", reservation,
		                                       "id_document_on") or ""),
	}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def verify_precheckin(reservation: str):
	"""The desk has held the document against the human and agrees.

	This is the write that finally makes precheckin_status="Verified" real -
	the enum has existed since pre-arrival check-in shipped and no code path
	ever set it. precheckin_submit already refuses to touch a Verified
	booking, so the guest is locked out of rewriting a checked card the
	moment this lands; that guard was clearly written for this."""
	res = frappe.get_doc("Reservation", reservation)
	if res.precheckin_status != "Submitted":
		frappe.throw("Only a submitted pre-check-in can be verified.")
	frappe.db.set_value("Reservation", res.name, {
		"precheckin_status": "Verified",
		"precheckin_verified_by": frappe.session.user,
		"precheckin_verified_on": frappe.utils.now_datetime(),
	})
	from kamra.savings import log_action
	log_action("verify_precheckin", "Reservation", res.name, res.property,
	           minutes_saved=2,
	           rationale=f"Desk verified {res.guest_name}'s ID against the document")
	return {"ok": True, "status": "Verified"}


@frappe.whitelist()
@require_roles("Front Desk", "Night Auditor", "Kamra Agent")
def checkin_context(reservation: str):
	"""Everything the check-in flow needs in one round trip: how complete
	the guest's registration is, the assigned room - or the allocator's
	suggestion plus every room the desk may hand over instead."""
	res = frappe.get_doc("Reservation", reservation)
	guest = frappe.get_doc("Guest", res.guest) if res.guest else None

	room_assigned = None
	if res.room:
		room_assigned = frappe.db.get_value(
			"Room", res.room,
			["name", "room_number", "housekeeping_status"], as_dict=True)

	# a whole villa with no Room of its own takes the property's member
	# rooms at check-in, so the desk checks it in as one unit. A villa type
	# with its own rooms gets the normal picker (one unit per stay).
	from kamra.kamra.doctype.reservation.reservation import (
		entire_place_rooms, uses_member_rooms)
	entire_place = None
	if not res.room and uses_member_rooms(res.property, res.room_type):
		entire_place = {"rooms": [
			r.room_number or r.name
			for r in entire_place_rooms(res.property, res.room_type)]}

	suggestion = None
	rooms = []
	if not res.room and not entire_place and res.status == "Confirmed":
		rooms = [
			{"name": r["name"], "room_number": r["room_number"],
			 "housekeeping_status": r["housekeeping_status"]}
			for r in available_rooms(res.property, res.room_type,
			                         res.check_in_date, res.check_out_date,
			                         group_booking=res.group_booking,
			                         ready_now=1)]
		from kamra.allocation import suggest_allocation
		for p_ in suggest_allocation(
				res.property, str(res.check_in_date))["proposals"]:
			if p_["reservation"] == res.name:
				suggestion = {
					"room": p_["suggested_room"],
					"room_number": p_["room_number"],
					"why": p_["why"],
					"needs_review": p_["needs_review"],
				}
				break

	return {
		"reservation": {
			"name": res.name, "status": res.status,
			"guest": res.guest, "guest_name": res.guest_name,
			"room_type": res.room_type,
			"room_type_name": frappe.db.get_value(
				"Room Type", res.room_type, "room_type_name"),
			"check_in_date": str(res.check_in_date),
			"check_out_date": str(res.check_out_date),
			"adults": res.adults, "children": res.children,
			"planned_check_in_time": str(res.get("planned_check_in_time") or ""),
			"vip": frappe.db.get_value("Guest", res.guest, "vip")
			       if res.guest else 0,
		},
		"readiness": {
			"phone": bool(guest and guest.get("phone")),
			"email": bool(guest and guest.get("email")),
			"id_on_file": bool(guest and guest.get("id_file")),
			"address_on_file": bool(guest and guest.get("address_proof_file")),
			"precheckin_status": res.get("precheckin_status") or "Not sent",
			"link_sent": bool(res.get("precheckin_link_sent")),
		},
		"room_assigned": room_assigned,
		"entire_place": entire_place,
		"suggestion": suggestion,
		"rooms": rooms,
	}


@frappe.whitelist()
@require_roles("Front Desk", "Night Auditor", "Kamra Agent")
def check_in(reservation: str, room: str | None = None):
	"""Check the guest in: the last step of the guided check-in.

	Hard gate: a room must be assigned (or passed here). Soft-required
	steps (ID, deposit, registration card, …) may still be open — the desk
	is warned and finishes them later. kamra.checkin.check_in_readiness
	lists hard blockers and pending backfill. Records who checked the guest
	in and when; a connected door-lock system then makes the keys recorded
	in the Keys step."""
	doc = frappe.get_doc("Reservation", reservation)
	from kamra.checkin import after_check_in, assert_ready, evaluate, soft_pending
	if doc.status == "Confirmed":
		assert_ready(doc, room=room)
	if room:
		doc.room = room
	if doc.status != "Checked In":
		# a contract whose plan fixes its exchange rate at check-in converts
		# at today's posting rate; the save re-locks the uncharged nights
		from kamra.pricing import relock_contract_fx_at_check_in
		relock_contract_fx_at_check_in(doc)
	doc.status = "Checked In"
	doc.save()
	from kamra.savings import log_action
	if doc.room:
		where = frappe.db.get_value("Room", doc.room, "room_number") or doc.room
	else:
		where = "the whole villa"
	log_action("check_in", "Reservation", doc.name, doc.property,
	           rationale=f"{doc.guest_name} into {where}")
	out = {"ok": True, "reservation": doc.name, "room": doc.room,
	       "checked_in_by": frappe.session.user}
	steps, _ctx = evaluate(doc)
	pending = soft_pending(steps)
	if pending:
		out["pending_backfill"] = [
			{"step": s["step_key"], "title": s["title"]} for s in pending]
	keys = after_check_in(doc)
	if keys:
		out["keys"] = keys
	return out


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def walk_in(property: str, room_type: str, room: str, check_out_date: str,
            guest_name: str, phone: str | None = None,
            guest: str | None = None, adults: int = 2, children: int = 0,
            meal_plan: str | None = None, voucher_code: str | None = None,
            company: str | None = None, id_type: str | None = None,
            rate_plan: str | None = None,
            id_number: str | None = None, nationality: str | None = None,
            payment_mode: str | None = None, payment_amount: float = 0,
            payment_reference: str | None = None, pin: str | None = None,
            idempotency_key: str | None = None,
            check_in_time: str | None = None,
            check_out_time: str | None = None,
            blacklist_override_reason: str | None = None,
            credit_override_reason: str | None = None,
            defer_check_in: int = 0,
            booked_via: str | None = None,
            ota_ref: str | None = None,
            ota_total: float = 0,
            ota_commission_pct: float | None = None, currency: str | None = None,
            quote_id: str | None = None, night_prices=None, extra_beds: int = 0,
            discount_type: str | None = None, discount_value: float = 0,
            discount_reason: str | None = None, night_discounts=None,
            fixed_rate: int = 0, supervisor_pin: str | None = None,
            supervisor_user: str | None = None, discount_note: str | None = None,
            comp_supervisor_user: str | None = None, comp_supervisor_pin: str | None = None):
	"""Walk-in in one step: book, register the ID, check into the chosen
	room and take the money - the counter flow that used to be four screens.

	defer_check_in=1 books, assigns the room and takes the money (as an
	advance) but leaves the stay Confirmed, so the desk finishes it in the
	guided check-in. Without it, check_in runs now (room is enough; other
	soft-required steps stay as backfill). The walk-in records the steps it
	does itself (room, who pays, the ID when given).

	It is the same writers chained (create_booking → check_in →
	add_folio_payment), inside one request, so it is all-or-nothing: a
	refused PIN or a room that just sold rolls the booking back too.
	Arrival is always today (calendar, like every new booking - a lagging
	night audit must not turn walk-ins away); a future stay is a normal
	booking, not a walk-in.

	An OTA guest who books and arrives the same day is a walk-in booked
	via that site: booked_via / ota_ref / ota_total / ota_commission_pct
	work as in create_booking (the stay's source is then OTA). night_prices
	books nights at the desk's own prices, and discount_type /
	discount_value / discount_reason / night_discounts give the desk's
	discount (supervisor_user + supervisor_pin above the desk's limit), as in
	create_booking - priced before the money is taken."""
	from kamra.crs import assert_property_access
	from kamra.localization import assert_payment_mode, validate_id_type
	assert_property_access(property)

	amount = float(payment_amount or 0)
	if amount < 0:
		frappe.throw(_("Payment amount cannot be negative."))
	if amount:
		assert_payment_mode(property, payment_mode, currency, pack_only=True)
	validate_id_type(property, id_type)

	check_in_date = nowdate()
	# a same-day check-out is a day-use walk-in (hourly), never the past
	if str(check_out_date) < check_in_date:
		frappe.throw(_("Check-out must be today (day use) or later ({0}).")
		             .format(check_in_date))

	r = frappe.db.get_value("Room", room, ["property", "room_type"],
	                        as_dict=True)
	if not r or r.property != property or r.room_type != room_type:
		frappe.throw(_("Room {0} is not a {1} room here.").format(
			room, room_type))

	# Money guards first, so a locked till refuses before anything is booked
	# rather than after the guest already holds a key.
	if amount:
		from kamra.authz import require_cashier_pin
		from kamra.cashier import require_open_session
		require_cashier_pin(property, pin)
		require_open_session(property)

	booked = create_booking(
		property=property, room_type=room_type,
		check_in_date=check_in_date, check_out_date=check_out_date,
		guest_name=guest_name, phone=phone, guest=guest,
		adults=adults, children=children, meal_plan=meal_plan,
		rate_plan=rate_plan,
		voucher_code=voucher_code, company=company,
		booking_type="Corporate" if company else "Individual",
		source="Walk-in", room=room, assign_room=0,
		idempotency_key=idempotency_key,
		check_in_time=check_in_time, check_out_time=check_out_time,
		blacklist_override_reason=blacklist_override_reason,
		credit_override_reason=credit_override_reason,
		booked_via=booked_via, ota_ref=ota_ref, ota_total=ota_total,
		ota_commission_pct=ota_commission_pct, night_prices=night_prices,
		stay_details={"extra_beds": cint(extra_beds)} if extra_beds else None,
		discount_type=discount_type, discount_value=discount_value,
		discount_reason=discount_reason, night_discounts=night_discounts,
		fixed_rate=fixed_rate, supervisor_pin=supervisor_pin,
		supervisor_user=supervisor_user, discount_note=discount_note,
		comp_supervisor_user=comp_supervisor_user, comp_supervisor_pin=comp_supervisor_pin)
	reservation = booked["reservation"]
	folio_filter = {"reservation": reservation, "folio_type": "Guest"}

	if booked.get("idempotent_replay"):
		# a double-tap on the button: report what the first tap did
		return {
			"reservation": reservation, "room": booked["room"],
			"room_number": frappe.db.get_value(
				"Room", booked["room"], "room_number"),
			"folio": frappe.db.get_value("Folio", folio_filter),
			"idempotent_replay": 1,
		}

	id_fields = {k: v for k, v in (("id_type", id_type),
	                               ("id_number", (id_number or "").strip()),
	                               ("nationality", nationality)) if v}
	if id_fields:
		frappe.db.set_value("Guest", booked["guest"], id_fields)

	# what the walk-in counter did itself, recorded under this operator
	from kamra.checkin import record_step
	res_doc = frappe.get_doc("Reservation", reservation)
	record_step(res_doc, "billing_check", "done", note="Walk-in",
	            payer="Company" if company else "Guest")
	if id_fields.get("id_number"):
		record_step(res_doc, "guest_ids", "done", note="Walk-in ID")

	if cint(defer_check_in):
		folio = None
		if amount:
			advance = record_advance(reservation, amount, mode=payment_mode,
			                       reference=payment_reference, pin=pin, currency=currency, quote_id=quote_id)
			folio, amount = advance["folio"], advance["paid"]
		return {
			"reservation": reservation, "room": room,
			"room_number": frappe.db.get_value("Room", room, "room_number"),
			"folio": folio or frappe.db.get_value("Folio", folio_filter),
			"checked_in": 0,
			"amount_after_tax": booked.get("amount_after_tax"),
			"paid": amount,
			**({"credit_warning": booked["credit_warning"]}
			   if booked.get("credit_warning") else {}),
		}

	check_in(reservation, room)
	folio = frappe.db.get_value("Folio", folio_filter)

	if amount:
		posted = add_folio_payment(folio, payment_mode, amount,
		                  reference=payment_reference, pin=pin,
		                  kind="Advance", currency=currency, quote_id=quote_id)
		amount = posted["payments"][-1]["amount"]

	return {
		"reservation": reservation,
		"room": room,
		"room_number": frappe.db.get_value("Room", room, "room_number"),
		"folio": folio,
		"amount_after_tax": booked.get("amount_after_tax"),
		"paid": amount,
		**({"credit_warning": booked["credit_warning"]}
		   if booked.get("credit_warning") else {}),
	}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def upload_occupant_id(reservation: str, row: str, image: str, kind: str = "id"):
	"""ID document (kind "id") or face photo (kind "photo") for one
	occupant on the stay register. Same security pipeline as every ID
	image: decoded, re-encoded through PIL (the sanitisation boundary),
	stored private, attached to the reservation so checkout retention
	rules find it. Checkout discards the ID, never the photo."""
	from kamra.id_documents import _decode, _sanitise

	if kind not in ("id", "photo"):
		frappe.throw("kind must be 'id' or 'photo'.")
	res = frappe.get_doc("Reservation", reservation)
	match = [o for o in (res.get("occupants") or []) if o.name == row]
	if not match:
		frappe.throw("No such occupant row on this stay.")
	occ = match[0]
	from kamra.localization import adult_age, is_minor, minor_id_kept, pack_for
	pack = pack_for(res.property)
	if is_minor(pack, occ.age or None) and not minor_id_kept(pack, occ.nationality):
		frappe.throw(f"Registration law needs only the name and age of a guest under "
		             f"{adult_age(pack)}; their ID and photo are not kept.")
	if kind == "photo":
		content = _sanitise(_decode(image))
		old_photo = occ.get("photo")
		fdoc = frappe.get_doc({
			"doctype": "File",
			"file_name": f"occupant-photo-{res.name}-{occ.idx}.jpg",
			"attached_to_doctype": "Reservation",
			"attached_to_name": res.name,
			"is_private": 1,
			"content": content,
		}).insert(ignore_permissions=True)
		frappe.db.set_value("Stay Occupant", occ.name, "photo", fdoc.file_url,
		                    update_modified=False)
		if old_photo:  # only once the new one is safely stored
			from kamra.privacy import _delete_files
			_delete_files("Reservation", res.name, url=old_photo)
		from kamra.savings import log_action
		log_action("occupant_photo_capture", "Reservation", res.name, res.property,
		           rationale=f"Photo taken for occupant {occ.full_name}")
		return {"ok": True, "file": fdoc.file_url, "row": occ.name}
	content = _sanitise(_decode(image))
	fdoc = frappe.get_doc({
		"doctype": "File",
		"file_name": f"occupant-id-{res.name}-{occ.idx}.jpg",
		"attached_to_doctype": "Reservation",
		"attached_to_name": res.name,
		"is_private": 1,
		"content": content,
	}).insert(ignore_permissions=True)
	frappe.db.set_value("Stay Occupant", occ.name, "id_file", fdoc.file_url,
	                    update_modified=False)
	from kamra.savings import log_action
	log_action("occupant_id_capture", "Reservation", res.name, res.property,
	           rationale=f"ID captured for occupant {occ.full_name}")
	return {"ok": True, "file": fdoc.file_url, "row": occ.name}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent", scope={"guest": "Guest"})
def upload_guest_document(guest: str, kind: str, image: str):
	"""The desk captures or replaces a guest's document while preparing
	the GRC - walk-ins, or a newer copy over last visit's. kind is 'id'
	or 'address'; stored privately, one current copy per slot."""
	if kind not in ("id", "address"):
		frappe.throw("kind must be 'id' or 'address'.")
	if not frappe.db.exists("Guest", guest):
		frappe.throw("Guest not found.")
	field = "id_file" if kind == "id" else "address_proof_file"
	from kamra.public_api import _save_id_image
	url = _save_id_image(guest, image, field)
	frappe.db.set_value("Guest", guest, field, url, update_modified=False)
	return {"ok": True, "file": url}


def _mask_id(value: str | None) -> str | None:
	"""'987654321012' → '••••••••1012' - enough for the register audit
	trail without holding the full number."""
	if not value or len(value) <= 4 or value.startswith("•"):
		return value
	return "•" * (len(value) - 4) + value[-4:]


def _scrub_stay_ids(res):
	"""Verify & Discard retention: after checkout, keep only the last 4
	digits of every ID collected for the stay - and the ID scan itself leaves
	with the guest.

	The scan has to go with the number. Masking the digits while a full
	photograph of the same Aadhaar sits on disk would make the whole setting
	a lie."""
	if frappe.db.get_value("Property", res.property, "id_retention") \
			!= "Verify & Discard":
		return
	for o in res.get("occupants") or []:
		if o.id_number:
			frappe.db.set_value("Stay Occupant", o.name, "id_number",
			                    _mask_id(o.id_number), update_modified=False)
		if o.get("id_file"):
			# occupant scans leave with the party, like every other ID
			for f in frappe.get_all("File", filters={
					"attached_to_doctype": "Reservation",
					"attached_to_name": res.name,
					"file_url": o.id_file}, pluck="name"):
				frappe.delete_doc("File", f, force=True,
				                  ignore_permissions=True)
			frappe.db.set_value("Stay Occupant", o.name, "id_file", None,
			                    update_modified=False)
	guest_id = frappe.db.get_value("Guest", res.guest, "id_number")
	if guest_id:
		frappe.db.set_value("Guest", res.guest, "id_number",
		                    _mask_id(guest_id), update_modified=False)
	from kamra.id_documents import discard_id_document
	discard_id_document(res.name)  # logs and moves on; never breaks checkout
	# the document photos are part of the same promise: verified, discarded
	for field in ("id_file", "address_proof_file"):
		if frappe.db.get_value("Guest", res.guest, field):
			for f in frappe.get_all("File", filters={
					"attached_to_doctype": "Guest",
					"attached_to_name": res.guest,
					"attached_to_field": field}, pluck="name"):
				frappe.delete_doc("File", f, force=True,
				                  ignore_permissions=True)
			frappe.db.set_value("Guest", res.guest, field, None,
			                    update_modified=False)


def cancellation_tier_for(property: str, days_before: int) -> dict | None:
	"""The tier covering a cancellation this many days before arrival.

	Tiers are bands keyed by the days remaining: 7/0%, 3/50%, 0/100% reads
	as free beyond a week, half between three and seven days, nothing back
	inside three. The band with the highest `days_before` at or below the
	days remaining wins. A property with no tiers keeps the single-window
	policy, so nothing changes for anyone who has not set them up.
	"""
	rows = frappe.get_all(
		"Cancellation Tier",
		# a child table is addressed by all three: parent alone would pick up
		# rows from any other doctype that grows a Cancellation Tier table,
		# and quietly change what a guest is charged
		filters={"parenttype": "Property", "parentfield": "cancellation_tiers",
		         "parent": property},
		fields=["days_before", "charge_pct", "label"],
		order_by="days_before desc")
	if not rows:
		return None
	for t in rows:
		if int(days_before) >= int(t.days_before or 0):
			return {"days_before": int(t.days_before or 0),
			        "charge_pct": float(t.charge_pct or 0),
			        "label": t.label}
	# earlier than every band (a late cancellation on a policy whose lowest
	# band is above zero): the strictest one applies
	last = rows[-1]
	return {"days_before": int(last.days_before or 0),
	        "charge_pct": float(last.charge_pct or 0), "label": last.label}


def _cancellation_terms(res):
	"""Policy + fee estimate for a reservation, before anyone commits."""
	from frappe.utils import date_diff

	from kamra.folio import percent_fee, policy_fee

	policy = frappe.db.get_value(
		"Property", res.property,
		["free_cancel_days", "cancellation_fee"], as_dict=True)
	days_before = date_diff(res.check_in_date, nowdate())

	from kamra.pricing import stay_season_rules

	rules = stay_season_rules(res.property, res.check_in_date,
	                          res.check_out_date, res.room_type)
	if rules["non_refundable"]:
		# Peak dates are sold on the understanding that they are not given
		# back; no band of the policy applies.
		return {
			"days_before_arrival": days_before,
			"free_cancel_days": int(policy.free_cancel_days or 0),
			"inside_window": True,
			"fee_basis": "100% of stay",
			"tier": {"days_before": days_before, "charge_pct": 100.0,
			         "label": rules["season"] or "Non-refundable dates"},
			"non_refundable_season": rules["season"],
			"estimated_fee": percent_fee(res, 100),
		}

	tier = cancellation_tier_for(res.property, days_before)
	if tier:
		pct = tier["charge_pct"]
		return {
			"days_before_arrival": days_before,
			"free_cancel_days": int(policy.free_cancel_days or 0),
			"inside_window": pct > 0,
			"fee_basis": f"{pct:g}% of stay",
			"tier": tier,
			"estimated_fee": percent_fee(res, pct) if pct else 0.0,
		}

	inside_window = days_before < int(policy.free_cancel_days or 0)
	basis = policy.cancellation_fee or "None"
	fee = policy_fee(res, basis) if inside_window and basis != "None" else 0.0
	return {
		"days_before_arrival": days_before,
		"free_cancel_days": int(policy.free_cancel_days or 0),
		"inside_window": inside_window,
		"fee_basis": basis,
		"tier": None,
		"estimated_fee": fee,
	}


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def cancellation_preview(reservation: str):
	"""What cancelling right now would cost - shown before confirming."""
	res = frappe.get_doc("Reservation", reservation)
	return _cancellation_terms(res)


def _do_cancel(res, reason: str = "Guest request", note: str | None = None,
               waive_fee: int = 0, issue_credit_note: int = 0) -> dict:
	"""Shared cancel path for desk API and OTA channel manager. The reason
	is one of the hotel's Cancellation Reason codes (kamra.masters); an
	unknown or switched-off one is recorded as Other."""
	from frappe.model.naming import make_autoname

	from kamra.masters import CANCELLATION, OTHER_REASON, enabled_codes
	if reason not in enabled_codes(CANCELLATION):
		reason = OTHER_REASON
	terms = _cancellation_terms(res)

	summary = (f"Cancel {res.name} · {reason}"
	           + (" · fee waived" if int(waive_fee or 0) else ""))

	fee = 0.0
	if terms["inside_window"] and not int(waive_fee or 0) \
			and terms["fee_basis"] != "None":
		from kamra.folio import post_policy_fee
		tier = terms.get("tier")
		fee = post_policy_fee(
			res, terms["fee_basis"],
			f"Cancellation fee ({terms['fee_basis']})",
			pct=tier["charge_pct"] if tier else None)

	res.status = "Cancelled"
	res.cancellation_reason = reason
	res.cancellation_note = note or ""
	res.cancellation_number = make_autoname("CXL-.YYYY.-.#####")
	res.cancellation_fee = fee
	res.cancelled_on = frappe.utils.now_datetime()
	frappe.flags.kamra_cancelling = True
	try:
		# ignore_permissions: both callers are pre-authorized — the desk wrapper
		# via @require_roles, the OTA webhook worker as a trusted internal caller
		# (it runs as Guest). Validation (villa lockout etc.) still runs.
		res.save(ignore_permissions=True)
	finally:
		frappe.flags.kamra_cancelling = False

	voucher_code = None
	if int(issue_credit_note or 0) == 1:
		credit_amount = max(0.0, float(res.advance_paid or 0) - fee)
		if credit_amount > 0:
			import random
			import string
			code_suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
			voucher_code = f"CN-{res.name.split('-')[-1]}-{code_suffix}"

			from frappe.utils import add_days, today
			voucher = frappe.get_doc({
				"doctype": "Discount Voucher",
				"property": res.property,
				"voucher_code": voucher_code,
				"discount_type": "Amount",
				"value": credit_amount,
				"valid_from": today(),
				"valid_to": add_days(today(), 180),
				"max_uses": 1,
				"disabled": 0
			})
			# a site rule on promo codes can tell this system credit note apart
			voucher.flags.kamra_credit_note = 1
			voucher.insert(ignore_permissions=True)

	from kamra.savings import log_action
	log_action("cancel_reservation", "Reservation", res.name, res.property,
	           minutes_saved=8, rationale=summary)

	return {"reservation": res.name,
	        "cancellation_number": res.cancellation_number,
	        "fee": fee, "waived": bool(int(waive_fee or 0)),
	        "credit_note_voucher": voucher_code}


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def cancel_reservation(reservation: str, reason: str = "Guest request",
                       note: str | None = None, waive_fee: int = 0,
                       agent: str | None = None, issue_credit_note: int = 0):
	"""Cancel a booking, applying the property's cancellation policy:
	free outside the window, else the configured fee lands on the folio.
	Issues a cancellation number the guest can hold on to. Pass
	waive_fee=1 to cancel graciously (logged).

	The cancellation is recorded in the action log.
	"""
	res = frappe.get_doc("Reservation", reservation)
	if res.status != "Confirmed":
		frappe.throw("Only confirmed bookings can be cancelled - "
		             "checked-in stays check out.")
	return _do_cancel(res, reason=reason, note=note, waive_fee=waive_fee,
	                  issue_credit_note=issue_credit_note)


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def cancellation_letter(reservation: str):
	"""Everything the printable cancellation confirmation needs."""
	res = frappe.get_doc("Reservation", reservation)
	if res.status != "Cancelled":
		frappe.throw("This reservation is not cancelled.")
	prop = frappe.get_doc("Property", res.property)
	guest = frappe.get_doc("Guest", res.guest)
	return {
		"property": {
			"property_name": prop.property_name,
			"logo_url": prop.get("logo_url"),
			"address": ", ".join(filter(None, [  # nosemgrep: frappe-no-functional-code -- filter(None, ...) drops empty address parts; equivalent to a comprehension
				prop.address_line, prop.city, prop.state, prop.pincode])),
			"phone": prop.phone, "email": prop.email,
		},
		"guest": {"full_name": guest.full_name, "phone": guest.phone,
		          "email": guest.email},
		"reservation": {
			"name": res.name,
			"room_type": (res.room_type or "").split("-")[-1],
			"check_in_date": str(res.check_in_date),
			"check_out_date": str(res.check_out_date),
			"nights": res.nights,
			"amount_after_tax": float(res.amount_after_tax or 0),
			"cancellation_number": res.cancellation_number,
			"cancellation_reason": res.cancellation_reason,
			"cancellation_fee": float(res.cancellation_fee or 0),
			"cancelled_on": str(res.cancelled_on or ""),
			"advance_paid": float(res.advance_paid or 0),
		},
	}


def _assert_settled(doc, credit_override_reason: str | None = None) -> list[dict]:
	"""A guest never departs owing money, or owed it (issue #132).

	Every night is posted first, because night audit may not have run for the
	last one. Only what the company pays moves to the city ledger: the stay's
	Company folio. A group's master bill is settled with the group
	(settle_group_bill), never by one member's check-out. The guest's own
	folios (Guest, Extra) - liquor, extras, anything the
	billing rules left on the guest - are the guest's to settle, so a balance
	there refuses the check-out. Closed folios count too: a bill closed with
	money still on it is still owed. frappe.flags.allow_unsettled_checkout is
	for seed and test scripts only - no endpoint sets it.

	Before anything moves, the property's credit control (kamra.credit)
	checks each company once for everything the stay bills to it. Returns
	the checks that warned or were overridden, for the desk to show.
	"""
	if frappe.flags.allow_unsettled_checkout:
		return []
	from kamra.checkout import settlement_position
	from kamra.folio import post_remaining_nights
	from kamra.ledger import _format_currency_amount

	post_remaining_nights(doc)
	# the one judgement of what is owed either way, shared with the desk's
	# check-out sheet (checkout_context)
	position = settlement_position(doc.name)
	owed, refund = position["owed"], position["refund_due"]
	to_city_ledger = [(f, c) for f, c, _b in position["to_city_ledger"]]
	if owed:
		frappe.throw(_("Cannot check out: {0} is still owed. Collect payment "
		               "or transfer it to the city ledger before departure.").format(
			_format_currency_amount(doc.property, owed)),
		             title=_("Settle before check-out"))
	if refund:
		frappe.throw(_("Cannot check out: the guest is owed {0}. Issue the "
		               "refund before departure.").format(
			_format_currency_amount(doc.property, refund)),
		             title=_("Settle before check-out"))
	# nothing moves until the guest's side is settled - and the company's
	# credit is checked once for everything the stay bills to it
	from kamra.credit import enforce_company_credit
	by_company: dict[str, list[str]] = {}
	for folio, company in to_city_ledger:
		by_company.setdefault(company, []).append(folio)
	credit = []
	for company, folios in by_company.items():
		amount = sum(float(frappe.db.get_value("Folio", f, "balance") or 0)
		             for f in folios)
		res = enforce_company_credit(
			company, doc.property, amount,
			override_reason=credit_override_reason,
			context="check-out", reference_doctype="Reservation",
			reference_name=doc.name, exclude_folios=folios)
		if not res["ok"]:
			credit.append(res)
	from kamra.ledger import transfer_to_city_ledger
	for folio, company in to_city_ledger:
		transfer_to_city_ledger(folio, company, credit_check=False)
	return credit



@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Night Auditor", "Kamra Agent")
def checkout_context(reservation: str):
	"""Everything the guided check-out needs in one round trip, without
	changing anything: each folio with its lines and invoice, as it will
	stand once check-out posts the remaining nights (worked out inside a
	savepoint that is always rolled back), bills not on the folio yet (open
	restaurant orders, laundry), a late check-out, what the guest owes
	(`collect`) or is owed (`refunds`: a held security deposit first, through
	its own refund path), what moves to the city ledger and its credit
	check, whether a cashier PIN and an open till are needed, every refusal
	check-out would give (`blockers`, each tied to its step) and the
	property's check-out SOP. check_out judges the balances with the same
	helper (kamra.checkout.settlement_position), so the two never disagree.
	Safe to call again and again: opening the sheet writes nothing."""
	from kamra.checkout import checkout_context as _context
	from kamra.sop import _template_steps, evaluate_sop_steps, next_tip

	ctx = _context(reservation)
	prop = frappe.db.get_value("Reservation", reservation, "property")
	meta, steps = _template_steps(prop, "checkout")
	evaluated = evaluate_sop_steps("checkout", steps, ctx) if meta else []
	ctx["sop"] = {
		"name": meta.get("name") if meta else None,
		"key": "checkout",
		"title": (meta or {}).get("title") or "Checkout",
		"steps": evaluated,
		"next_tip": next_tip(evaluated),
	}
	return ctx


def _reverse_check_in_blocker(doc) -> str | None:
	"""Why this stay's check-in can't be reversed now, or None (OPERA's
	Reverse Check In: arrived on the current business date, nothing posted
	or paid, nothing open, no deposit held). The day is the stay's arrival
	business date, not the calendar date of checked_in_at: between midnight
	and the night audit the calendar is a day ahead of the hotel day."""
	from kamra.business_date import get_business_date
	from kamra.checkout import _pending_bills
	if doc.status != "Checked In":
		return _("Only a checked-in stay can have its check-in reversed.")
	if not doc.get("check_in_date") or \
	   str(getdate(doc.check_in_date)) != str(get_business_date(doc.property)):
		return _("The night audit has run since this check-in, so it can't be "
		         "reversed. Check the guest out instead.")
	# the group's bill anchored to this stay is the group's: what other rooms
	# posted to it is no reason to keep this check-in
	folios = frappe.get_all("Folio", filters={"reservation": doc.name,
	                                          "folio_type": ("!=", "Group")}, pluck="name")
	if folios and frappe.db.exists("Folio Charge", {"parent": ("in", folios),
	                                                "charge_type": "Room"}):
		return _("A room night is posted for this stay: check the guest out instead.")
	if folios and (frappe.db.exists("Folio Charge", {"parent": ("in", folios)})
	               or frappe.db.exists("Folio Payment", {"parent": ("in", folios)})):
		return _("A charge or payment is on the bill: void it, or check the guest out.")
	# restaurant / laundry not on the bill yet, and a draft ERPNext POS
	# Invoice raised against one of the stay's folios
	if any(b["billable"] for b in _pending_bills(doc, [])) or (
			folios and frappe.db.exists("DocType", "POS Invoice")
			and frappe.get_meta("POS Invoice").has_field("kamra_ref")
			and frappe.db.exists("POS Invoice", {"kamra_ref": ("in", folios),
			                                     "docstatus": 0})):
		return _("A restaurant or laundry bill is still open for this stay: settle or "
		         "cancel it first.")
	# an authorization holds the deposit with no payment on the bill
	if frappe.db.exists("Security Deposit", {"reservation": doc.name,
	                                         "status": ("in", ("Collected", "Held"))}):
		return _("A security deposit is held for this stay: release or refund it "
		         "first, or check the guest out.")
	return None


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Kamra Agent")
def reverse_check_in(reservation: str, reason: str, pin: str | None = None):
	"""Undo a check-in made by mistake (wrong guest, wrong room), on the
	arrival's business date and before anything is posted, open or paid.
	The stay goes back to Confirmed (arrival) with its room; the room is
	released and marked Dirty so housekeeping inspects it before it is sold
	again. From there the desk checks the guest in again or cancels the
	booking. An agent acts for a Front Desk, Finance or Hotel Admin user,
	never passes a cashier PIN and is refused where the property requires
	one; the user it acts for is the logged actor."""
	from kamra.authz import require_cashier_pin
	from kamra.credit import _is_agent
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Say why the check-in is being reversed."))
	doc = frappe.get_doc("Reservation", reservation)
	assert_property_access(doc.property)
	if _is_agent():
		if frappe.session.user != "Administrator" and not (
				set(frappe.get_roles()) & {"Front Desk", "Finance", "Hotel Admin",
				                           "System Manager"}):
			frappe.throw(_("Only Front Desk, Finance or Hotel Admin may reverse a check-in."),
			             frappe.PermissionError)
		if pin is not None:
			frappe.throw(_("An AI agent cannot pass a cashier PIN: undo the check-in "
			               "on the hotel screen."), frappe.PermissionError)
		if cint(frappe.db.get_value("Property", doc.property, "require_cashier_pin")):
			frappe.throw(_("This property requires a cashier PIN: undo the check-in "
			               "on the hotel screen."), frappe.PermissionError)
	blocker = _reverse_check_in_blocker(doc)
	if blocker:
		frappe.throw(blocker)
	require_cashier_pin(doc.property, pin)
	rooms = doc._occupied_rooms(checking_in=False)
	doc.status = "Confirmed"
	frappe.flags.kamra_status_transition = True
	try:
		# authorized above (role guard, property scope, agent boundary):
		# Finance reads Reservation but may reverse a check-in
		doc.save(ignore_permissions=True)
	finally:
		frappe.flags.kamra_status_transition = False
	doc.db_set({"actual_check_in": None, "checked_in_at": None, "checked_in_by": None},
	           update_modified=False)
	# nobody is in house: the next check-in stamps everyone's arrival again
	for occ in doc.get("occupants") or []:
		if occ.get("arrived_at"):
			occ.db_set("arrived_at", None, update_modified=False)
	for room in rooms:
		if frappe.db.get_value("Room", room, "housekeeping_status") == "Out of Order":
			frappe.db.set_value("Room", room, "occupancy_status", "Vacant")
		else:
			frappe.db.set_value("Room", room, {"occupancy_status": "Vacant",
			                                   "housekeeping_status": "Dirty"})
	key_result = _revoke_stay_keys(doc, rooms)
	from kamra.savings import log_action
	log_action("reverse_check_in", "Reservation", doc.name, doc.property,
	           rationale=f"{doc.guest_name}: {reason}")
	return {"ok": True, "reservation": doc.name, "status": doc.status,
	        "rooms": rooms, "key_result": key_result}


def _revoke_stay_keys(doc, rooms: list[str]) -> dict | None:
	"""Cancel the stay's keys for every room it held on a connected lock
	system (door_lock revoke_keys). The manual adapter has nothing to call:
	the desk collects the cards. A connector failure is shown, never raised."""
	from kamra.door_lock import get_adapter
	adapter = None
	try:
		adapter = get_adapter(doc.property)
		if adapter.key == "manual" or not adapter.supports_revoke or not rooms:
			return None
		results = [adapter.revoke_keys(doc.property, doc.name, room) for room in rooms]
	except Exception:
		key = adapter.key if adapter else None
		frappe.log_error(title=f"Door lock {key} revoke_keys failed")
		return {"ok": False, "adapter": key, "manual": False,
		        "message": _("The lock system did not answer. Cancel the keys at the encoder.")}
	ok = all(r.ok for r in results)
	message = next((r.message for r in results if not r.ok), results[-1].message)
	from kamra.savings import log_action
	log_action("revoke_keys", "Reservation", doc.name, doc.property,
	           rationale=f"check-in reversed, keys for {', '.join(rooms)} via "
	                     f"{adapter.key}: {'ok' if ok else 'failed'}")
	return {"ok": ok, "adapter": adapter.key,
	        "manual": any(r.manual for r in results), "message": message}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance")
def reinstate_checkout(reservation: str, reason: str, pin: str | None = None):
	"""Undo a checkout made by mistake, the same business day: before the
	night audit runs. Each bill closed at checkout has its invoice
	cancelled the auditable way (the number goes to the Cancelled Invoice
	register; where the country needs it, a credit note is issued), a
	company bill comes back off the company's account, and the guest is
	in house again in the same room. Refused once the night audit has run,
	when someone else is in the room, or when the company has paid."""
	from kamra.authz import require_cashier_pin
	from kamra.folio import bridge_invoice, cancel_invoice, invoiced, refuse_bridge_correction
	from kamra.ledger import reverse_city_ledger_transfer

	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Say why the checkout is being undone."))
	doc = frappe.get_doc("Reservation", reservation)
	if doc.status != "Checked Out":
		frappe.throw(_("Only a checked-out stay can be reinstated."))
	require_cashier_pin(doc.property, pin)
	from kamra.business_date import get_business_date
	from kamra.kamra.doctype.reservation.reservation import villa_lockout_bookings
	bd = get_business_date(doc.property)
	out_bd = doc.get("check_out_business_date")
	if not out_bd or str(out_bd) != str(bd):
		frappe.throw(_("The night audit has run since this checkout, so it can't "
		              "be undone. Make a new booking, or ask Finance to correct the bills."))
	# the room may already be re-let: a guest in house, or today's arrival
	# given it. The overlap guard on save misses a stay that ended today.
	rooms = doc._occupied_rooms(checking_in=False)
	taken = rooms and frappe.db.sql(
		"""SELECT name FROM `tabReservation` WHERE name != %s AND room IN %s
		   AND (status = 'Checked In'
		        OR (status IN ('Confirmed', 'Held') AND check_in_date <= %s))
		   LIMIT 1""", (doc.name, tuple(rooms), bd), as_dict=True)
	taken = taken or villa_lockout_bookings(doc.property, doc.room_type, bd,
	                                        add_days(bd, 1), exclude=doc.name, limit=1)
	if taken:
		frappe.throw(_("The room has been given to {0} since the checkout, so it "
		              "can't be undone. Move that stay first, or make a new booking.").format(
			taken[0].name))
	# only the bills this checkout closed (since the last night audit) or
	# left open; a bill settled earlier in the stay keeps its invoice and
	# its company account entry
	day_start = frappe.db.get_value(
		"Night Audit Run", {"property": doc.property, "status": ("!=", "Draft")},
		"creation", order_by="creation desc")
	# the group's bill anchored to this stay is the group's: settled with the
	# group, it keeps its invoice when one room's check-out is undone
	fields = ["name", "status", "invoice_number", "closed_on", "modified", "folio_type"]
	if frappe.db.has_column("Folio", "sales_invoice"):
		fields.append("sales_invoice")
	folios = [f for f in frappe.get_all(
		"Folio", filters={"reservation": doc.name}, fields=fields)
		if f.folio_type != "Group"
		and (f.status != "Closed" or not day_start
		     or ((f.closed_on or f.modified)
		         and get_datetime(f.closed_on or f.modified) >= get_datetime(day_start)))]
	for f in folios:
		if f.status == "Closed" and bridge_invoice(f):
			refuse_bridge_correction(f)
	cancelled, brought_back = [], 0.0
	for f in folios:
		if f.status == "Closed" and invoiced(f):
			cancelled.append(cancel_invoice(f.name, f"Checkout reinstated: {reason}")["cancelled"])
	for f in folios:
		brought_back += reverse_city_ledger_transfer(f.name, reason)
	doc.status = "Checked In"
	frappe.flags.kamra_status_transition = True
	try:
		doc.save()
	finally:
		frappe.flags.kamra_status_transition = False
	from kamra.savings import log_action
	log_action("reinstate_checkout", "Reservation", doc.name, doc.property,
	           rationale=f"{doc.guest_name}: {reason}"
	                     + (f"; invoices cancelled {', '.join(cancelled)}" if cancelled else ""))
	warnings = []
	if frappe.db.get_value("Security Deposit", {"reservation": doc.name, "status": "Withheld"}):
		warnings.append(_("The security deposit was applied to the bill at checkout and "
		                  "stays applied. Take a new deposit if the guest needs one."))
	if frappe.db.get_value("Property", doc.property, "id_retention") == "Verify & Discard":
		warnings.append(_("The guest's ID was discarded at checkout. Capture it again "
		                  "in the check-in steps."))
	from kamra.ledger import _currency_places, _round_currency
	places = max(_currency_places(doc.property), 2)
	return {"ok": True, "reservation": doc.name, "status": doc.status,
	        "cancelled_invoices": cancelled, "from_city_ledger": _round_currency(brought_back, places),
	        "warnings": warnings}


@frappe.whitelist()
@require_roles("Front Desk", "Night Auditor", "Kamra Agent")
def check_out(reservation: str, credit_override_reason: str | None = None,
              early_reason: str | None = None, early_fee: str | None = None):
	"""Check a guest out. What the company pays moves to the city ledger,
	after the property's credit control: a warning comes back in
	`credit_warnings`; a block refuses (CREDIT_BLOCKED) unless Finance or a
	Hotel Admin passes `credit_override_reason` (logged). Agents can't
	override.

	Leaving before the booked date: pass `early_reason` and the stay is
	first shortened to today with the property's early-departure charge
	(early_departure, below). Without it, a property whose charge is not
	"All Nights" refuses, so unused nights are never billed by accident;
	under "All Nights" every booked night is billed as before."""
	doc = frappe.get_doc("Reservation", reservation)
	from kamra.checkout import apply_early_departure, early_departure_plan
	if (early_reason or "").strip():
		apply_early_departure(doc.name, early_reason, early_fee)
		doc = frappe.get_doc("Reservation", reservation)
	else:
		plan = early_departure_plan(doc)
		if plan and plan["policy"] != "All Nights":
			frappe.throw(
				_("{0} is leaving {1} night(s) early (booked to {2}). Give the reason "
				  "(early_reason) so the stay is shortened and the property's early "
				  "departure charge ({3}) applies.").format(
					doc.guest_name, plan["unused_nights"], plan["booked_check_out"],
					_(plan["policy"])),
				title=_("Leaving early"))
	from kamra.checkout import assert_no_open_bills
	assert_no_open_bills(doc)
	credit = _assert_settled(doc, credit_override_reason)
	doc.status = "Checked Out"
	doc.flags.kamra_check_out = True  # passed the checks above (Reservation.validate)
	doc.save()
	_scrub_stay_ids(doc)
	from kamra.savings import log_action
	log_action("check_out", "Reservation", doc.name, doc.property,
	           rationale=f"{doc.guest_name} departed")
	out = {"ok": True, "reservation": doc.name}
	if credit:
		out["credit_warnings"] = [
			{"company": c["company"], "reasons": c["reasons"],
			 "overridden": bool(c.get("overridden"))} for c in credit]
	return out


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Kamra Agent")
def early_departure(reservation: str, reason: str, fee: str | None = None):
	"""A guest in house is leaving before the booked date: shorten the stay
	to today through the amend path (availability, channel manager and
	price follow), post the property's early-departure charge for the
	unused nights (None / One Night / All Nights) and log the reason. Then
	settle and check out as usual. `fee` other than the property's policy
	needs Finance or a Hotel Admin. checkout_context shows the plan
	(`early_departure`) before anything changes."""
	from kamra.crs import assert_property_access
	assert_property_access(frappe.db.get_value("Reservation", reservation, "property"))
	from kamra.checkout import apply_early_departure
	return apply_early_departure(reservation, reason, fee)


HK_SUPERVISOR = "Housekeeping Supervisor"
# who oversees rooms: the supervisor, the desk, and the property's admins
_ROOM_SUPERVISORS = {HK_SUPERVISOR, "Front Desk", "Hotel Admin",
                     "System Manager", "Kamra Agent"}
# what an attendant may set by hand; passing a room (Inspected / Ready) or
# taking it out of order is a supervisor's call
_ATTENDANT_STATUSES = {"Clean", "Dirty"}


def _is_room_supervisor() -> bool:
	return frappe.session.user == "Administrator" or \
		bool(_ROOM_SUPERVISORS & set(frappe.get_roles()))


@frappe.whitelist(methods=["POST"])
@require_roles("Housekeeping", HK_SUPERVISOR, "Front Desk", "Kamra Agent")
def set_housekeeping_status(room: str, status: str, reason: str | None = None):
	"""Change a room's housekeeping status - role, property and DocPerm
	checked (#99): an attendant can mark Clean / Dirty, only a supervisor
	can pass a room or take it out of order, nobody can touch a room of a
	property they aren't assigned to, and removing Write on Room in the
	role permissions actually removes the ability."""
	allowed = {"Clean", "Dirty", "Inspected", "Ready", "Out of Order"}
	if status not in allowed:
		frappe.throw(f"Invalid status. Use one of {sorted(allowed)}")
	r = frappe.db.get_value("Room", room, ["property", "housekeeping_status"],
	                        as_dict=True)
	if not r:
		frappe.throw(_("Room {0} not found.").format(room), frappe.DoesNotExistError)
	from kamra.crs import assert_property_access
	assert_property_access(r.property)
	frappe.has_permission("Room", "write", doc=room, throw=True)
	if status not in _ATTENDANT_STATUSES and not _is_room_supervisor():
		frappe.throw(
			_("Only a housekeeping supervisor can set a room to {0}.").format(status),
			frappe.PermissionError)
	if status == r.housekeeping_status:
		return {"ok": True, "room": room, "status": status}
	frappe.db.set_value("Room", room, "housekeeping_status", status)
	from kamra.savings import log_action
	log_action("room_status", "Room", room, r.property,
	           rationale=f"{r.housekeeping_status or '-'} → {status}" + (f": {reason.strip()}" if reason else ""))
	if status == "Out of Order":
		# set_value skips the Room hooks, so the bell is told here
		from kamra.notifications import room_out_of_order
		room_out_of_order(r.property, room)
	return {"ok": True, "room": room, "status": status}


@frappe.whitelist()
@require_roles(HK_SUPERVISOR, "Front Desk", "Kamra Agent")
def room_board(property: str):
	"""Every room at a glance for the housekeeping supervisor: status,
	occupancy, who is in it, and its open task - the board the desk has,
	inside the Housekeeping module (#99)."""
	from kamra.crs import assert_property_access
	assert_property_access(property)
	rooms = frappe.get_all(
		"Room", filters={"property": property},
		fields=["name", "room_number", "room_type", "floor",
		        "housekeeping_status", "occupancy_status"],
		order_by="room_number asc",
	)
	in_house = {
		r.room: r for r in frappe.get_all(
			"Reservation",
			filters={"property": property, "status": "Checked In",
			         "room": ("is", "set")},
			fields=["room", "guest_name", "check_out_date", "is_day_use",
			        "planned_check_out_time", "status"])}
	from kamra.business_date import get_business_date
	business = get_business_date(property)
	clean_tasks = {}
	tasks = {}
	for t in frappe.get_all(
			"Housekeeping Task",
			filters={"property": property,
			         "status": ("in", ["Pending", "In Progress"])},
			fields=["name", "room", "task_type", "status", "priority",
			        "assigned_to_user", "due_by", "reservation.check_out_business_date as departure_day"],
			order_by="creation asc"):
		tasks.setdefault(t.room, t)
		day = t.departure_day if t.task_type == "Checkout Clean" and t.departure_day else t.due_by
		if t.task_type in ("Checkout Clean", "Stayover Clean") and str(day)[:10] == business:
			clean_tasks.setdefault(t.room, []).append({"name": t.name, "type": t.task_type})
	names = {u.name: u.full_name for u in frappe.get_all(
		"User", filters={"name": ("in", list({t.assigned_to_user for t in
		                                      tasks.values() if t.assigned_to_user}))},
		fields=["name", "full_name"])} if tasks else {}
	for r in rooms:
		stay = in_house.get(r.name)
		r["guest_name"] = stay.guest_name if stay else None
		# Day-use is due only once its planned ETD has passed; overnight
		# stays stay date-based (checkout date ≤ today).
		from kamra.day_use import format_hhmm as _format_hhmm, is_due_out as _is_due_out
		r["due_out"] = bool(stay and _is_due_out(stay))
		# a day-use room turns the same day: housekeeping plans on its slot
		r["day_use"] = bool(stay and stay.is_day_use)
		r["etd"] = _format_hhmm(stay.planned_check_out_time) or None \
			if stay and stay.planned_check_out_time not in (None, "") else None
		r["clean_tasks"] = clean_tasks.get(r.name, [])
		t = tasks.get(r.name)
		r["task"] = {
			"name": t.name, "type": t.task_type, "status": t.status,
			"priority": t.priority,
			"assignee": names.get(t.assigned_to_user) or t.assigned_to_user,
		} if t else None
	from kamra.floors import floors_for
	room_types = [{"name": t.name, "room_type_name": t.room_type_name} for t in frappe.get_all(
		"Room Type", filters={"property": property}, fields=["name", "room_type_name"],
		order_by="room_type_name asc")]
	return {"rooms": rooms, "can_supervise": _is_room_supervisor(),
	        "floors": floors_for(property), "room_types": room_types}


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def availability_calendar(property: str, start_date: str | None = None, days: int = 14):
	"""Per room-type, per date: rooms available and the 2-adult rate.
	Powers the calendar view and, later, the agent's availability tool."""
	from kamra.group_blocks import block_hold_by_night
	from kamra.kamra.doctype.reservation.reservation import villa_locked_nights
	from kamra.pricing import occupancy_rate, season_adjust
	from kamra.siu.availability import capacity_by_night, has_active_sius

	start = start_date or nowdate()
	days = min(int(days), 31)
	dates = [add_days(start, i) for i in range(days)]

	room_types = frappe.get_all(
		"Room Type",
		filters={"property": property, "disabled": 0},
		fields=["name", "room_type_name", "base_price"],
		order_by="base_price asc",
	)
	from frappe.utils import getdate

	end = add_days(start, days)
	rows = []
	for rt in room_types:
		rt_doc = frappe.get_doc("Room Type", rt.name)

		def _display_rate(date, rt_doc=rt_doc, rt_name=rt.name):
			"""The 2-adult shelf rate for one night. Priced per night rather
			than once for the range, so a day-scoped rule (small-group
			pricing, a weekend season) shows on the right columns."""
			return float(season_adjust(
				property, date, occupancy_rate(rt_doc, 2, 0, date), rt_name))

		# nights the villa <-> room lockout or a group block takes off sale
		locked = villa_locked_nights(property, rt.name, start, end)
		held = block_hold_by_night(property, rt.name, dates)
		if has_active_sius(property, rt.name):
			caps = capacity_by_night(property, rt.name, start, end)
			total = max(caps) if caps else 0
			cells = []
			for i, date in enumerate(dates):
				cap = caps[i] if i < len(caps) else 0
				cells.append({
					"date": str(date),
					"available": 0 if str(date) in locked
					else max(0, cap - held.get(str(date), 0)),
					"rate": _display_rate(date),
				})
		else:
			total = frappe.db.count(
				"Room",
				{"property": property, "room_type": rt.name},
			)
			if not total:
				continue
			bookings = frappe.get_all(
				"Reservation",
				filters={
					"room_type": rt.name,
					"status": ("in", ["Confirmed", "Checked In"]),
					"check_in_date": ("<", end),
					"check_out_date": (">", start),
				},
				fields=["check_in_date", "check_out_date"],
			)
			cells = []
			for date in dates:
				d = getdate(date)
				taken = sum(
					1 for b in bookings
					if getdate(b.check_in_date) <= d < getdate(b.check_out_date)
				)
				cells.append({
					"date": str(date),
					"available": 0 if str(date) in locked
					else max(0, total - taken - held.get(str(date), 0)),
					"rate": _display_rate(date),
				})
		rows.append({
			"room_type": rt.name,
			"room_type_name": rt.room_type_name,
			"total_rooms": total,
			"cells": cells,
		})
	return {"start": str(start), "days": days, "dates": [str(d) for d in dates],
	        "room_types": rows}


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def tape_chart(property: str, start_date: str | None = None, days: int = 14):
	"""Rooms × dates grid with reservation bars - the front desk's home."""
	from frappe.utils import getdate

	start = getdate(start_date or nowdate())
	days = min(int(days), 31)
	end = add_days(start, days)

	rooms = frappe.get_all(
		"Room", filters={"property": property},
		fields=["name", "room_number", "room_type", "housekeeping_status",
		        "occupancy_status", "floor"],
		order_by="room_type asc, room_number asc",
	)
	# room-type labels (accordion headers show the name, not the Link id)
	rt_names = {
		rt.name: rt.room_type_name for rt in frappe.get_all(
			"Room Type", filters={"property": property},
			fields=["name", "room_type_name"])}
	for r in rooms:
		r["room_type_name"] = rt_names.get(r.room_type, r.room_type)
	bookings = frappe.get_all(
		"Reservation",
		filters={
			"property": property, "room": ("is", "set"),
			"status": ("in", ["Confirmed", "Checked In"]),
			"check_in_date": ("<", end), "check_out_date": (">", start),
		},
		fields=["name", "room", "guest", "guest_name", "status",
		        "check_in_date", "check_out_date", "is_day_use", "adults",
		        "planned_check_in_time", "planned_check_out_time",
		        "precheckin_status", "source", "booking_type", "company",
		        "travel_agent", "group_booking"],
	)
	# VIP flag rides in from the guest profile (one lookup per distinct guest)
	guests = {b.guest for b in bookings if b.guest}
	vip = set(frappe.get_all(
		"Guest", filters={"name": ("in", list(guests)), "vip": 1},
		pluck="name")) if guests else set()
	by_room = {}
	for b in bookings:
		b["vip"] = 1 if b.guest in vip else 0
		by_room.setdefault(b.room, []).append(b)
	# rooms held out of sale (house use, VIP, maintenance) draw as bands too
	blocks = frappe.get_all(
		"Room Block",
		filters={
			"property": property, "block_status": "Active",
			"from_date": ("<", end), "to_date": (">", start),
		},
		fields=["name", "room", "reason", "from_date", "to_date", "note"],
	)
	blocks_by_room = {}
	for k in blocks:
		blocks_by_room.setdefault(k.room, []).append(k)
	for r in rooms:
		r["bookings"] = by_room.get(r.name, [])
		r["blocks"] = blocks_by_room.get(r.name, [])

	# changeover conflicts: same room turns over today and the incoming
	# guest lands before (or within the hk buffer of) the outgoing one
	from kamra.day_use import format_hhmm as _format_hhmm
	conflicts = []
	for r in rooms:
		by_date_out = {str(b.check_out_date): b for b in r["bookings"]
		               if b.planned_check_out_time not in (None, "")}
		for b in r["bookings"]:
			out = by_date_out.get(str(b.check_in_date))
			if not out or out.name == b.name or b.planned_check_in_time in (None, ""):
				continue
			in_eta = _format_hhmm(b.planned_check_in_time)
			out_etd = _format_hhmm(out.planned_check_out_time)
			if in_eta and out_etd and in_eta <= out_etd:
				conflicts.append({
					"room": r.name, "room_number": r.room_number,
					"date": str(b.check_in_date),
					"out_res": out.name, "out_guest": out.guest_name,
					"etd": out_etd,
					"in_res": b.name, "in_guest": b.guest_name,
					"eta": in_eta,
				})

	# the day-by-day house position: sold vs capacity vs the overbooking
	# ceiling, plus the demand tier the pricing engine is applying
	from kamra.pricing import demand_tier, forecast_occupancy
	prop_ob = float(frappe.get_cached_doc("Property", property)
	                .get("overbooking_pct") or 0)
	capacity = len(rooms)
	position = []
	for i in range(days):
		d = str(add_days(start, i))
		occ = forecast_occupancy(property, d)
		sold = round(occ * capacity / 100) if capacity else 0
		tier = demand_tier(property, "", d, occupancy=occ)
		position.append({
			"date": d, "sold": sold, "capacity": capacity,
			"occupancy": occ,
			"limit": int(capacity * (1 + prop_ob / 100)),
			"overbooked": sold > capacity,
			"premium_pct": tier["premium_pct"] if tier else 0,
			"min_rate": tier["min_rate"] if tier else 0,
		})

	return {
		"start": str(start), "days": days,
		"dates": [str(add_days(start, i)) for i in range(days)],
		"rooms": rooms,
		"position": position,
		"conflicts": conflicts,
	}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def send_precheckin_link(reservation: str, channel: str = "WhatsApp"):
	"""Send the guest their self check-in link (mints a token if needed). Sends
	over a connected channel when there is one; otherwise returns the link for
	the desk to share. Marks when it went out so the arrivals board can show it;
	a link nobody was sent is not marked."""
	res = frappe.db.get_value(
		"Reservation", reservation,
		["name", "property", "guest", "guest_name", "precheckin_token"],
		as_dict=True)
	if not res:
		frappe.throw("Reservation not found.")
	if not res.precheckin_token:
		res.precheckin_token = frappe.generate_hash(length=32)
		frappe.db.set_value("Reservation", reservation, "precheckin_token",
		                    res.precheckin_token)

	link = public_url(f"checkin/{res.precheckin_token}")
	phone = frappe.db.get_value("Guest", res.guest, "phone") if res.guest else None
	sent = False
	if phone:
		try:
			from kamra.agents_channels import send_outbound
			# send_outbound reports a dropped message ({"sent": False}) rather
			# than raising, e.g. when no channel is connected
			out = send_outbound(res.property, channel, phone,
			                    f"Hi {res.guest_name}, check in early and skip the "
			                    f"front-desk queue for your stay: {link}")
			sent = bool(out and out.get("sent"))
		except Exception:
			sent = False  # no live channel; fall back to returning the link

	if sent:
		frappe.db.set_value("Reservation", reservation, "precheckin_link_sent",
		                    frappe.utils.now_datetime())
	from kamra.savings import log_action
	log_action("send_precheckin_link", "Reservation", reservation, res.property,
	           minutes_saved=2,
	           rationale=f"Self check-in link {'sent via ' + channel if sent else 'generated'}")
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persists the completed operation before returning to an external/public caller; reviewed as intentional
	return {"ok": True, "link": link, "sent": sent,
	        "channel": channel if sent else None}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def set_stay_times(reservation: str, eta: str | None = None,
                   etd: str | None = None):
	"""Set the planned arrival (ETA) and departure (ETD) times for any stay.
	These drive the hotel-position view on the tape chart: back-to-back
	rooms conflict when the incoming guest lands before the outgoing one
	leaves, and the day's arrival flow is planned around them.

	On a day-use stay the times also re-price an hourly or block rate plan
	(same write path as booking — Reservation.save → apply_pricing)."""
	doc = frappe.get_doc("Reservation", reservation)
	eta, etd = _stay_slot(str(doc.check_in_date), str(doc.check_out_date),
	                      eta, etd)
	if cint(doc.is_day_use):
		doc.planned_check_in_time = eta or None
		doc.planned_check_out_time = etd or None
		doc.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persists the completed operation before returning to an external/public caller; reviewed as intentional
		return {"ok": True, "amount_after_tax": float(doc.amount_after_tax or 0)}
	frappe.db.set_value("Reservation", reservation, {
		"planned_check_in_time": eta or None,
		"planned_check_out_time": etd or None,
	})
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persists the completed operation before returning to an external/public caller; reviewed as intentional
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def set_day_use_times(reservation: str, from_time: str, to_time: str):
	"""Set planned check-in/out times for a day-use booking (drives the hourly
	tape view)."""
	return set_stay_times(reservation, from_time, to_time)


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Kamra Agent")
def position_briefing(property: str, date: str | None = None):
	"""The GM / front-desk position briefing - what Kamra Agent reads out
	at the morning meeting: today's occupancy against the overbooking
	ceiling, arrivals with ETAs, departures with ETDs and balances,
	back-to-back conflicts, the demand tier pricing is applying, and a
	7-day outlook."""
	from frappe.utils import getdate

	from kamra.pricing import demand_tier, forecast_occupancy

	d = str(getdate(date or nowdate()))
	prop = frappe.get_cached_doc("Property", property)
	capacity = frappe.db.count("Room", {"property": property})
	ob_pct = float(prop.get("overbooking_pct") or 0)

	arrivals = frappe.get_all(
		"Reservation",
		filters={"property": property, "check_in_date": d,
		         "status": "Confirmed"},
		fields=["name", "guest_name", "room", "room_type",
		        "planned_check_in_time", "adults", "children",
		        "precheckin_status", "company", "group_booking"],
		order_by="planned_check_in_time asc")
	departures = frappe.get_all(
		"Reservation",
		filters={"property": property, "check_out_date": d,
		         "status": "Checked In"},
		fields=["name", "guest_name", "room",
		        "planned_check_out_time"],
		order_by="planned_check_out_time asc")
	from kamra.day_use import format_hhmm as _format_hhmm
	for a in arrivals:
		a["eta"] = _format_hhmm(a.planned_check_in_time) or None \
			if a.planned_check_in_time not in (None, "") else None
		a["room_no"] = (a.room or "").split("-")[-1] or None
	for dep in departures:
		dep["etd"] = _format_hhmm(dep.planned_check_out_time) or None \
			if dep.planned_check_out_time not in (None, "") else None
		dep["room_no"] = (dep.room or "").split("-")[-1]
	# what check-out will ask for, unposted nights included
	dep_money = _stay_balances([dep.name for dep in departures])
	for dep in departures:
		dep["balance_due"] = (dep_money.get(dep.name) or {}).get("due", 0.0)

	# back-to-back: departure and arrival share a room today and the times cross
	conflicts = []
	dep_by_room = {x.room: x for x in departures if x.room and x.etd}
	for a in arrivals:
		out = dep_by_room.get(a.room)
		if out and a["eta"] and a["eta"] <= out["etd"]:
			conflicts.append({
				"room_no": out["room_no"], "out_guest": out.guest_name,
				"etd": out["etd"], "in_guest": a.guest_name, "eta": a["eta"]})

	outlook = []
	for i in range(7):
		day = str(add_days(d, i))
		occ = forecast_occupancy(property, day)
		tier = demand_tier(property, "", day, occupancy=occ)
		outlook.append({"date": day, "occupancy": occ,
		                "premium_pct": tier["premium_pct"] if tier else 0,
		                "min_rate": tier["min_rate"] if tier else 0})

	occ_today = outlook[0]["occupancy"]
	out = {
		"date": d,
		"occupancy": occ_today,
		"sold": round(occ_today * capacity / 100) if capacity else 0,
		"capacity": capacity,
		"overbooking_pct": ob_pct,
		"overbooking_limit": int(capacity * (1 + ob_pct / 100)),
		"arrivals": arrivals,
		"departures": departures,
		"changeover_conflicts": conflicts,
		"demand_tier": ({"premium_pct": outlook[0]["premium_pct"],
		                 "min_rate": outlook[0]["min_rate"]}
		                if outlook[0]["premium_pct"] or outlook[0]["min_rate"]
		                else None),
		"outlook": outlook,
	}
	# the front desk sees no occupancy or rates, as on the dashboard
	# (kamra.dashboards.FIGURE_ROLES); balances due stay, the desk collects them
	from kamra.dashboards import sees_figures
	if not sees_figures():
		for k in ("occupancy", "sold", "overbooking_pct", "overbooking_limit",
		          "demand_tier", "outlook"):
			out.pop(k)
		out["figures_hidden"] = True
	return out


# Demand tiers are a revenue control: the revenue manager sets them (the
# Settings -> Revenue card), the desk and finance keep the access they had.
HURDLE_ROLES = ("Front Desk", "Finance", "Revenue Manager", "Kamra Agent")


@frappe.whitelist()
@require_roles(*HURDLE_ROLES)
def hurdle_rates(property: str):
	"""The demand tiers: at each occupancy threshold, the premium applied
	and the minimum sell rate enforced. A row with a room type applies to
	that type only and beats the property-wide row at the same threshold."""
	rows = frappe.get_all(
		"Hurdle Rate", filters={"property": property, "disabled": 0},
		fields=["name", "room_type", "occupancy_from", "premium_pct",
		        "min_rate"],
		order_by="occupancy_from asc")
	names = {r.room_type for r in rows if r.room_type}
	labels = dict(frappe.get_all(
		"Room Type", filters={"name": ("in", list(names))},
		fields=["name", "room_type_name"], as_list=True)) if names else {}
	for r in rows:
		r["room_type_name"] = labels.get(r.room_type) if r.room_type else None
	return rows


@frappe.whitelist(methods=["POST"])
@require_roles(*HURDLE_ROLES, scope={"name": "Hurdle Rate"})
def save_hurdle_rate(property: str, occupancy_from: float,
                     premium_pct: float = 0, min_rate: float = 0,
                     room_type: str | None = None, name: str | None = None):
	if not (0 <= float(occupancy_from) <= 100):
		frappe.throw("Occupancy threshold must be between 0 and 100.")
	if float(premium_pct or 0) < 0 or float(min_rate or 0) < 0:
		frappe.throw("Premium and hurdle must not be negative.")
	if room_type:
		from kamra.booking_refs import resolve_room_type
		room_type = resolve_room_type(property, room_type)
	doc = (frappe.get_doc("Hurdle Rate", name) if name
	       else frappe.new_doc("Hurdle Rate"))
	doc.update({
		"property": property, "room_type": room_type or None,
		"occupancy_from": float(occupancy_from),
		"premium_pct": float(premium_pct or 0),
		"min_rate": float(min_rate or 0),
	})
	# the role gate above is the permission; Revenue Manager has no DocPerm
	doc.save(ignore_permissions=True)
	return {"ok": True, "name": doc.name}


@frappe.whitelist(methods=["POST"])
@require_roles(*HURDLE_ROLES, scope={"name": "Hurdle Rate"})
def delete_hurdle_rate(name: str):
	frappe.delete_doc("Hurdle Rate", name, ignore_permissions=True)
	return {"ok": True}


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def tape_chart_hourly(property: str, date: str | None = None):
	"""Single-day, rooms x hours. Day-use bookings sit at their planned times;
	an overnight stay covering this day shows as a full-width occupied band."""
	from frappe.utils import getdate

	d = getdate(date or nowdate())
	prop = frappe.get_cached_doc("Property", property)

	def _hour(v, default):
		try:
			return int(str(v).split(":")[0])
		except (ValueError, AttributeError):
			return default

	start_h = _hour(prop.get("hourly_view_start"), 6)
	end_h = _hour(prop.get("hourly_view_end"), 23) or 23
	if end_h <= start_h:
		start_h, end_h = 6, 23

	rooms = frappe.get_all(
		"Room", filters={"property": property},
		fields=["name", "room_number", "room_type", "housekeeping_status"],
		order_by="room_type asc, room_number asc")
	rt_names = {rt.name: rt.room_type_name for rt in frappe.get_all(
		"Room Type", filters={"property": property},
		fields=["name", "room_type_name"])}
	for r in rooms:
		r["room_type_name"] = rt_names.get(r.room_type, r.room_type)

	rows = frappe.get_all(
		"Reservation",
		filters={"property": property, "room": ("is", "set"),
		         "status": ("in", ["Confirmed", "Checked In"]),
		         "check_in_date": ("<=", str(d)),
		         "check_out_date": (">=", str(d))},
		fields=["name", "room", "guest", "guest_name", "status", "is_day_use",
		        "check_in_date", "check_out_date", "planned_check_in_time",
		        "planned_check_out_time", "source", "booking_type", "company",
		        "group_booking", "travel_agent"])
	vipset = set(frappe.get_all(
		"Guest", filters={"name": ("in", [r.guest for r in rows if r.guest] or [""]),
		                  "vip": 1}, pluck="name"))
	by_room = {}
	for b in rows:
		b["vip"] = 1 if b.guest in vipset else 0
		# overnight (multi-day) stay that includes today -> full band, no times
		b["overnight"] = 0 if b.is_day_use else 1
		if b.is_day_use:
			from kamra.day_use import format_hhmm as _format_hhmm
			b["from_hour"] = (
				_format_hhmm(b.planned_check_in_time) or "10:00"
				if b.planned_check_in_time not in (None, "") else "10:00"
			)
			b["to_hour"] = (
				_format_hhmm(b.planned_check_out_time) or "18:00"
				if b.planned_check_out_time not in (None, "") else "18:00"
			)
		by_room.setdefault(b.room, []).append(b)
	for r in rooms:
		r["bookings"] = by_room.get(r.name, [])
	return {"date": str(d), "start_hour": start_h, "end_hour": end_h,
	        "rooms": rooms}


@frappe.whitelist()
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def venue_calendar(property: str, start_date: str | None = None, days: int = 14):
	"""Venues × dates with their bookings - the banquet/function diary. Shows
	each venue's schedule so you can see availability and spot conflicts."""
	from frappe.utils import getdate

	start = getdate(start_date or nowdate())
	days = min(int(days), 31)
	end = add_days(start, days)
	venues = frappe.get_all(
		"Venue", filters={"property": property, "disabled": 0},
		fields=["name", "venue_name", "capacity", "base_price"],
		order_by="venue_name asc")
	bookings = frappe.get_all(
		"Venue Booking",
		filters={"property": property,
		         "event_date": ("between", [str(start), str(add_days(end, -1))])},
		fields=["name", "venue", "event_type", "status", "event_date",
		        "start_time", "end_time", "customer_name", "attendees",
		        "quoted_amount", "advance_received"],
		order_by="event_date asc, start_time asc")
	by_venue = {}
	for b in bookings:
		b["start_time"] = str(b.start_time or "")[:5]
		b["end_time"] = str(b.end_time or "")[:5]
		b["event_date"] = str(b.event_date)
		by_venue.setdefault(b.venue, []).append(b)
	for v in venues:
		v["bookings"] = by_venue.get(v.name, [])
	return {
		"start": str(start), "days": days,
		"dates": [str(add_days(start, i)) for i in range(days)],
		"venues": venues,
	}


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def move_reservation(reservation: str, new_room: str,
                     reason: str | None = None, confirm_price: int = 0,
                     rate_mode: str = "destination",
                     agreed_rate: float | None = None,
                     supervisor_user: str | None = None,
                     supervisor_pin: str | None = None):
	"""Room move / upgrade - mid-stay or before arrival. Overlap guard re-runs.
	One writer for the tape chart, the reservation drawer and the GRC (#113).

	The new room may be a DIFFERENT room type (e.g. Standard -> Suite): the
	reservation's room type follows the room it moves into, so upgrades and
	downgrades are allowed. If the booking auto-prices, the new type's rate
	is quoted and locked - shown first by room_move_preview, and refused
	unless confirm_price=1 when it changes the bill. A same-type move keeps
	the locked nightly rates; a manually-priced booking keeps its amount.

	rate_mode: "destination" (the new type's rate, above), "source" (keep
	what the guest pays now) or "custom" (agreed_rate per night; Revenue
	Manager / admins only). A discounted stay re-priced for its new room type
	takes the discount rules again (supervisor_user + supervisor_pin sign it
	off when it needs one)."""
	doc = frappe.get_doc("Reservation", reservation)
	from kamra.crs import assert_property_access
	assert_property_access(doc.property)
	_discount_sign_off(doc, supervisor_user, supervisor_pin)
	if doc.status not in ("Confirmed", "Checked In"):
		frappe.throw("Only active reservations can be moved.")
	if frappe.db.get_value("Room", new_room, "property") != doc.property:
		frappe.throw(_("Room {0} is not in this property.").format(new_room))
	old_room = doc.room
	old_type = doc.room_type
	old_amount = float(doc.amount_after_tax or 0)
	new_type = frappe.db.get_value("Room", new_room, "room_type")
	before_rate = flt(doc.get("agreed_rate"))
	_require_price_confirmation(
		_move_price(doc, new_type, rate_mode, agreed_rate), confirm_price)
	doc.room = new_room
	from kamra.ledger import _currency_places, _round_currency
	places = max(_currency_places(doc.property), 2)
	eps = 0.5 * (10 ** -places)
	if rate_mode == "custom" or abs(flt(doc.get("agreed_rate")) - before_rate) >= eps:
		# the room move is a governed writer of the agreed rate
		# (Reservation.guard_agreed_rate): a custom rate is a manager's, even
		# at the rate the stay already holds; the new room's own rate clears it
		doc.flags.kamra_agreed_rate_override = True
	if rate_mode == "custom":
		doc.fixed_rate = 1  # a rate set by hand: OPERA's Fixed Rate
	if rate_mode == "source":
		# keep what the guest pays: re-key the lock so the save keeps the rates
		doc.rate_lock_key = doc.rate_lock_key_for(doc.quote_inputs())
	doc.save()
	if doc.status == "Checked In" and old_room and old_room != new_room:
		frappe.db.set_value("Room", old_room,
		                    {"occupancy_status": "Vacant",
		                     "housekeeping_status": "Dirty"})
		frappe.db.set_value("Room", new_room, "occupancy_status", "Occupied")
	from kamra.savings import log_action
	note = f"{old_room} → {new_room}"
	if new_type and new_type != old_type:
		note += f" · {old_type} → {new_type}"
	if abs(float(doc.amount_after_tax or 0) - old_amount) > eps:
		note += (f" · re-priced {old_amount:,.{places}f} → "
		         f"{float(doc.amount_after_tax or 0):,.{places}f} (confirmed)")
	if rate_mode != "destination":
		note += f" · rate: {rate_mode}"
	if rate_mode == "custom":
		from kamra.webhooks import emit_rate_overridden
		emit_rate_overridden(doc.property, {
			"scope": "stay", "reservation": doc.name, "room_type": doc.room_type,
			"room": doc.room, "rate": float(agreed_rate or 0),
			"start_date": str(doc.check_in_date), "end_date": str(doc.check_out_date),
			"previous_amount": _round_currency(old_amount, places),
			"amount_after_tax": _round_currency(doc.amount_after_tax, places),
			"reason": (reason or "").strip() or None})
	# who signed off the discount the move re-priced (as amend_stay logs it)
	approver = (doc.flags.get("kamra_discount_governance") or {}).get("approver")
	if approver:
		note += f" · discount signed off by {approver}"
	if (reason or "").strip():
		note += f" · {reason.strip()[:140]}"
	log_action("room_move", "Reservation", doc.name, doc.property, rationale=note,
	           **({"approver": approver} if approver else {}))
	return {"ok": True, "room": doc.room, "room_type": doc.room_type,
	        "amount_after_tax": float(doc.amount_after_tax or 0)}


def _can_set_rate() -> bool:
	# the same people who may set room rates (set_room_rate)
	return _is_admin() or "Revenue Manager" in frappe.get_roles()


def _check_night_prices(property: str, room_type: str, check_in_date: str,
                        check_out_date: str, night_prices, *,
                        voucher_code: str | None = None,
                        rate_plan: str | None = None,
                        company: str | None = None, ota_total: float = 0,
                        group_booking: str | None = None) -> dict:
	"""Gate for nights a desk priced by hand ({"YYYY-MM-DD": Decimal}, empty
	when none were typed). Refuses - with the reason, never silently - a payload
	that is malformed, a caller without the right to set rates, or a booking
	whose price something else already decides: a voucher, a rate plan (its
	own or the company's negotiated one), an OTA total or a group block rate."""
	from kamra.pricing import negotiated_rate_plan, normalize_night_prices, refuse_with_night_prices
	from kamra.settings import get_setting
	typed = normalize_night_prices(night_prices, check_in_date, check_out_date)
	if not typed:
		return {}
	if not _can_set_rate():
		frappe.throw(_("Only a Revenue Manager or Hotel Admin can set nightly prices."),
		             frappe.PermissionError)
	if get_setting(property, "rate_plan_required"):
		frappe.throw(
			_("Hand-set nightly prices cannot be used while a rate plan is required for this property. "
			  "Choose a rate plan or apply a discount instead.")
		)
	if voucher_code:
		refuse_with_night_prices(_("a voucher"))
	if rate_plan:
		refuse_with_night_prices(_("a rate plan"))
	if negotiated_rate_plan(property, company):
		refuse_with_night_prices(_("the company's negotiated rate plan"))
	if flt(ota_total) > 0:
		refuse_with_night_prices(_("an OTA total, which already sets the price"))
	from kamra.group_blocks import group_rate
	if group_rate(group_booking, room_type) > 0:
		refuse_with_night_prices(_("the group's block rate"))
	return typed


def _record_night_prices(doc, typed: dict) -> None:
	"""Leave the trail for a booking priced night by night: a comment on the
	reservation, the action log, and the rate.overridden webhook (scope night)."""
	from kamra.localization import money_text
	from kamra.savings import log_action
	from kamra.webhooks import emit_rate_overridden
	typed_text = ", ".join(f"{d} {float(p):,.2f}" for d, p in sorted(typed.items()))
	doc.add_comment("Comment", _("Hand-set nightly prices: {0}. The other nights are at the rate card.")
	                .format(typed_text))
	log_action(
		"set_night_prices", "Reservation", doc.name, doc.property,
		rationale=(f"Hand-set nightly prices on {doc.room_type} "
		           f"{doc.check_in_date}→{doc.check_out_date}: "
		           + ", ".join(f"{d} {money_text(doc.property, float(p))}"
		                       for d, p in sorted(typed.items()))
		           + f"; total {money_text(doc.property, doc.amount_after_tax)}"))
	from kamra.ledger import _currency_places, _round_currency
	emit_rate_overridden(doc.property, {
		"scope": "night", "reservation": doc.name, "room_type": doc.room_type,
		"room": doc.room, "start_date": str(doc.check_in_date),
		"end_date": str(doc.check_out_date),
		"nights": [{"date": d, "price": float(p)} for d, p in sorted(typed.items())],
		"amount_after_tax": _round_currency(doc.amount_after_tax, max(_currency_places(doc.property), 2)),
		"reason": None})


def _current_nightly(res) -> float:
	"""What one night of this stay costs now, on the basis an agreed rate is
	entered in: the locked rates' average (the rate the folio bills), else
	the agreed rate. The locked rates are always before tax; where the
	property's prices include tax (rates_include_tax) an agreed rate is a
	gross price, so each locked night is put back to gross with its own tax
	rate before the two are compared."""
	from kamra.ledger import _currency_places, _round_currency
	from kamra.pricing import rates_include_tax
	rows = res.get("night_rates") or []
	if rows:
		gross = rates_include_tax(res.property)
		rates = [flt(r.rate) * (1 + flt(r.gst_rate) / 100 if gross else 1)
		         for r in rows]
		return _round_currency(sum(rates) / len(rates), max(_currency_places(res.property), 2))
	return flt(res.get("agreed_rate"))


# who may ask an AI agent to agree a rate for them: the people who set one
# at the desk themselves
AGREED_RATE_CALLER_ROLES = ("Front Desk", "Revenue Manager", "Hotel Admin")


def _refuse_agent_rate(reason: str):
	frappe.throw(_("{0} A manager approves it on the hotel screen.").format(reason),
	             frappe.PermissionError)


def _rate_override_plan(res, agreed_rate) -> dict:
	"""Who may set this agreed rate, and what it would do to the bill.
	Leaves res holding the new agreed rate, its discount cleared, for the
	save that follows.

	An agreed rate is OPERA's Fixed Rate: it replaces the desk's discount on
	every night still to come, so the rate entered is what the guest pays
	(_clear_desk_discount). Its cut is each night's whole cut from the
	night's rate-code amount (Reservation.rate_code_amounts) - never from
	the rate an earlier agreed rate left - so a chain of agreed rates is
	judged as one cut. The front desk works within rate_override_limit_pct
	and above the Rate Guardrail floor; past either, a supervisor signs it
	off. An AI agent is held to the front desk's limit whoever it acts for:
	a Revenue Manager's or Hotel Admin's own right to set any rate is theirs
	at the hotel screen, not the agent's."""
	from kamra.credit import _is_agent
	from kamra.folio import charged_nights
	from kamra.pricing import entry_rate, night_rate_places, rail_for_day, rates_include_tax
	agreed_rate = flt(agreed_rate)
	if agreed_rate <= 0:
		frappe.throw(_("Enter the new nightly rate."))
	if not res.get("auto_price"):
		frappe.throw(_("This booking is priced by hand; change its amount instead."))
	current = _current_nightly(res)
	limit = desk_limit_pct(res.property)
	agent = _is_agent()
	manager = _can_set_rate() and not agent
	cleared = _clear_desk_discount(res)
	res.flags.kamra_repriced_rows = None
	pv = _reprice_preview(res, agreed_rate=agreed_rate)
	rows = res.flags.get("kamra_repriced_rows") or res.get("night_rates") or []
	charged = set(charged_nights(res.name))
	inclusive = rates_include_tax(res.property)
	places = night_rate_places(res.property)
	money_eps = 0.5 * (10 ** -max(places, 2))
	rails = frappe.get_all(
		"Rate Guardrail", filters={"property": res.property, "disabled": 0,
		                           "room_type": ("in", [res.room_type, "", None])},
		fields=["room_type", "floor_price", "days_of_week"])
	cut, floor = 0.0, []
	for r in rows:
		day = str(getdate(r.get("stay_date")))
		if day in charged:
			continue  # on the folio: it keeps what it holds
		net = flt(entry_rate(r.get("rate"), r.get("gst_rate"), inclusive, places))
		code = flt(r.get("rate_code_amount")) or net
		if code:
			cut = max(cut, 100 * (code - net) / code)
		rail = rail_for_day(rails, res.room_type, getdate(day).strftime("%a").lower())
		# the floor is on the price basis the rate card uses
		if rail and flt(rail.floor_price) and net < flt(rail.floor_price) - money_eps:
			floor.append(day)
	cut = round(max(cut, 0.0), 2)
	pv.update(current_nightly=current, agreed_rate=agreed_rate,
	          reduction_pct=cut, limit_pct=limit, can_set_rate=manager,
	          agent=agent, floor_breach=floor, replaces_discount=bool(cleared),
	          raises=agreed_rate > current + money_eps,
	          needs_supervisor=not manager and (cut > limit + 0.005 or bool(floor)))
	# who can sign a desk cut off here: the people verify_supervisor accepts
	# (names only); a manager or an agent never picks one
	from kamra.authz import supervisors_for
	pv["supervisors"] = [] if manager or agent else supervisors_for(
		res.property, exclude=frappe.session.user)
	return pv


def _discount_snapshot(doc) -> dict:
	"""The stay's discount for an audit row: the stay's own and each night's
	daily details (base, discount and net on the locked row)."""
	return {
		"discount_type": doc.get("discount_type") or None,
		"discount_value": flt(doc.get("discount_value")),
		"discount_reason": doc.get("discount_reason") or None,
		"fixed_rate": cint(doc.get("fixed_rate")),
		"nights": [{"date": str(getdate(r.stay_date)), "base_rate": flt(r.get("base_rate")),
		            "discount_type": r.get("discount_type") or None,
		            "discount_value": flt(r.get("discount_value")),
		            "discount_amount": flt(r.get("discount_amount")),
		            "discount_reason": r.get("discount_reason") or None,
		            "rate": flt(r.get("rate")), "rate_code_amount": flt(r.get("rate_code_amount"))}
		           for r in sorted(doc.get("night_rates") or [], key=lambda r: getdate(r.stay_date))],
	}


def _clear_desk_discount(doc) -> dict | None:
	"""OPERA's Fixed Rate: a rate set by hand (set_agreed_rate, a custom-rate
	room move) replaces the desk's discount, so the rate entered is what the
	guest pays. Clears it in memory - the stay's and every night's Daily
	Details, so the re-quote never takes a discount off the agreed rate - and
	marks the save a governed discount change; returns the discount as it
	was (_discount_snapshot), or None when the stay had none. A night already
	on the folio keeps what it was charged (Reservation.hold_charged_nights)."""
	if not doc.has_discount():
		return None
	was = _discount_snapshot(doc)
	doc.discount_type, doc.discount_value, doc.discount_reason = None, 0, None
	for r in doc.get("night_rates") or []:
		r.discount_type, r.discount_value, r.discount_reason, r.fixed_rate = None, 0, None, 0
	doc.flags.kamra_reservation_discount = True
	return was


@frappe.whitelist()
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def rate_override_preview(reservation: str, agreed_rate: float):
	"""What an agreed nightly rate would do to this stay before the desk
	confirms it: the new nightly rates and total, the cut against what the
	guest pays now, the property's limit for the front desk and whether a
	supervisor has to sign it off. Nothing is written."""
	res = frappe.get_doc("Reservation", reservation)
	assert_property_access(res.property)
	return _rate_override_plan(res, agreed_rate)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def set_agreed_rate(reservation: str, agreed_rate: float, reason_code: str,
                    reason: str = "", confirm_price: int = 0,
                    pin: str | None = None, supervisor: str | None = None):
	"""Agree a different nightly rate for one stay, in place: the guest keeps
	their room, only this stay's nightly rates change (locked as usual, so
	the folio and check-out bill it). The rate card - room type, season,
	tariff - is never touched. Nights already charged follow the property's
	rate-change policy.

	reason_code is a Rate Change reason (RATE-PROMO, RATE-MGR, ...).
	Revenue Manager / Hotel Admin set any positive rate. The front desk may
	only lower it, by up to rate_override_limit_pct (Finance settings);
	a bigger cut, or a reason code that requires a supervisor, is signed
	off by a Hotel Admin or Finance user with their PIN (supervisor + pin).
	An AI agent may only apply a cut within the front desk's limit, for a
	signed-in Front Desk / Revenue Manager / Hotel Admin; anything that
	needs a supervisor (and any supervisor + pin it passes) is refused, for
	a manager to approve on the hotel screen.
	Call rate_override_preview first and pass confirm_price=1."""
	from kamra.authz import verify_supervisor
	from kamra.credit import _is_agent
	from kamra.ledger import ensure_default_reason_codes
	from kamra.savings import log_action

	doc = frappe.get_doc("Reservation", reservation)
	assert_property_access(doc.property)
	agent = _is_agent()
	if agent:
		if not set(AGREED_RATE_CALLER_ROLES) & set(frappe.get_roles()):
			_refuse_agent_rate(_("An AI agent can't agree a rate on its own."))
		if (supervisor or "").strip() or (pin or "").strip():
			_refuse_agent_rate(_("An AI agent can't use a supervisor's PIN."))
	if doc.status not in ("Confirmed", "Checked In"):
		frappe.throw(_("Only an active reservation can take an agreed rate."))
	ensure_default_reason_codes()
	code = frappe.db.get_value("Reason Code", reason_code or "",
	                           ["category", "requires_supervisor", "disabled"],
	                           as_dict=True)
	categories = agreed_rate_reason_categories(doc.property)
	if code and not code.disabled and code.category in categories:
		why = reason_code
	elif not reason_code and (reason or "").strip() and not (categories and frappe.db.exists(
			"Reason Code", {"category": ["in", categories], "disabled": 0})):
		# the hotel switched every code of those categories off: a written reason
		why, code = reason.strip()[:140], frappe._dict(requires_supervisor=0)
	else:
		frappe.throw(_("Pick a {0} reason code for the agreed rate.").format(
			_categories_text(categories)))
	old_nightly = _current_nightly(doc)
	old_amount = flt(doc.amount_after_tax)
	before = _discount_snapshot(doc)
	plan = _rate_override_plan(doc, agreed_rate)
	approver = None
	if agent:
		if plan["raises"]:
			_refuse_agent_rate(_("An AI agent can lower a stay's rate, not raise it."))
		if plan["needs_supervisor"] or code.requires_supervisor:
			_refuse_agent_rate(_("A cut of {0}% is over the front desk's limit of {1}%, "
			                     "or the reason needs a manager.").format(
				f"{plan['reduction_pct']:g}", f"{plan['limit_pct']:g}"))
	elif not plan["can_set_rate"]:
		if plan["raises"]:
			frappe.throw(_("The front desk can lower a stay's rate, not raise it. "
			               "Ask a Revenue Manager."), frappe.PermissionError)
		if plan["needs_supervisor"] or code.requires_supervisor:
			if not (supervisor or "").strip():
				if plan["floor_breach"] and plan["reduction_pct"] <= plan["limit_pct"] + 0.005:
					frappe.throw(_("{0} is under the rate floor: a Hotel Admin or Finance user "
					               "signs it off with their PIN.").format(", ".join(plan["floor_breach"])),
					             frappe.PermissionError)
				frappe.throw(_("A cut of {0}% is over the front desk's limit of {1}%, "
				               "or the reason needs a manager: a Hotel Admin or Finance "
				               "user signs it off with their PIN.").format(
					f"{plan['reduction_pct']:g}", f"{plan['limit_pct']:g}"),
					frappe.PermissionError)
			approver = verify_supervisor(doc.property, supervisor, pin,
			                             requester=frappe.session.user)
	_require_price_confirmation(plan, confirm_price)
	# the governed writer of the agreed rate (Reservation.guard_agreed_rate);
	# the discount it replaces was cleared by _rate_override_plan
	doc.flags.kamra_agreed_rate_override = True
	# an agreed rate is a rate set by hand: OPERA's Fixed Rate
	doc.fixed_rate = 1
	# the role and limit checks above are the permission; Revenue Manager
	# has no write DocPerm on Reservation
	doc.save(ignore_permissions=True)
	approver = approver or (doc.flags.get("kamra_discount_governance") or {}).get("approver")
	note = (f"{why}: {old_nightly:,.2f} → {flt(agreed_rate):,.2f} a night"
	        f" · total {old_amount:,.2f} → {flt(doc.amount_after_tax):,.2f}")
	if (reason or "").strip() and why != reason.strip()[:140]:
		note += f" · {reason.strip()[:140]}"
	if plan.get("replaces_discount"):
		note += " · replaces the stay's discount"
	log_action("rate_override", "Reservation", doc.name, doc.property,
	           rationale=note, approver=approver,
	           before_snapshot={**before, "nightly": old_nightly, "amount_after_tax": old_amount},
	           after_snapshot={**_discount_snapshot(doc), "agreed_rate": flt(agreed_rate),
	                           "amount_after_tax": flt(doc.amount_after_tax)})
	return {"ok": True, "reservation": doc.name, "room": doc.room,
	        "agreed_rate": flt(doc.agreed_rate),
	        "nights": [{"date": str(r.stay_date), "rate": flt(r.rate)}
	                   for r in doc.night_rates],
	        "amount_after_tax": flt(doc.amount_after_tax), "approver": approver}


# --- reservation and walk-in discounts --------------------------------------
# OPERA Cloud Reservation Discounts (Rate Code "Discount" flag, Look to Book,
# Walk-In, Stay Details, Daily Details) and SAP S/4HANA's manual discount
# condition: a percentage OR an amount off each night's room rate, gross and
# net kept apart, a mandatory reason, the desk's limit and a supervisor's
# sign-off above it. One set of rules, discount_governance, for every place a
# discount is given; Reservation.validate runs it on the nights it locks.

def discount_reason_categories(property: str) -> list[str]:
	"""The Reason Code categories a discount takes (RATE-PROMO, ADJ-RATE, ...):
	the property's discount_reason_categories setting (OPERA's discount
	reasons, SAP's order reasons for the purpose). The codes themselves are
	the Reason Code records."""
	from kamra.settings import list_setting
	return list_setting(property, "discount_reason_categories")


def agreed_rate_reason_categories(property: str) -> list[str]:
	"""The Reason Code categories an agreed rate takes: the property's
	agreed_rate_reason_categories setting."""
	from kamra.settings import list_setting
	return list_setting(property, "agreed_rate_reason_categories")


def _categories_text(categories: list[str]) -> str:
	return " or ".join(_(c) for c in categories)


def desk_limit_pct(property: str) -> float:
	"""The cut the person asking may give at the desk, in % (OPERA's
	authorizer levels, SAP's condition limits by responsibility): the
	highest of their roles' limits in rate_override_limits_by_role, else the
	property's rate_override_limit_pct. An AI agent takes its own role's
	line (AGENT_ROLE) whoever it acts for, and with none the lowest limit
	the property sets, so it never gives more than anyone at the desk.
	Revenue Managers and Hotel Admins are not held to it (_can_set_rate)."""
	from kamra.credit import _is_agent
	from kamra.settings import get_setting
	base = flt(get_setting(property, "rate_override_limit_pct"))
	tiers = {}
	for line in str(get_setting(property, "rate_override_limits_by_role") or "").splitlines():
		role, sep, pct = line.rpartition(":")
		try:
			value = float(pct) if sep and role.strip() else None
		except ValueError:
			value = None
		# nan or inf is no limit at all (nan compares False with every cut)
		if value is not None and math.isfinite(value):
			tiers[role.strip()] = min(max(value, 0.0), 100.0)
	if not tiers:
		return base
	if _is_agent():
		from kamra.install import AGENT_ROLE
		return tiers.get(AGENT_ROLE, min(base, *tiers.values()))
	mine = [pct for role, pct in tiers.items() if role in set(frappe.get_roles())]
	return max(mine) if mine else base


def discount_reasons(property: str) -> list[dict]:
	"""The active reason codes a reservation discount can be given with."""
	from kamra.ledger import ensure_default_reason_codes
	ensure_default_reason_codes()
	categories = discount_reason_categories(property)
	if not categories:
		return []
	return frappe.get_all(
		"Reason Code",
		filters={"disabled": 0, "category": ["in", categories]},
		fields=["name", "code", "description", "category", "requires_supervisor"],
		order_by="category asc, code asc")


def discount_options(property: str) -> dict:
	"""What a booking screen needs to offer a discount: whether this user
	may give one, the front desk's limit and the reasons to pick from."""
	# who may call the discount action (its declared roles, require_roles)
	allowed = getattr(set_reservation_discount, "_kamra_roles", None) or ()
	return {
		"can_discount": bool(set(allowed) & set(frappe.get_roles())),
		# this person's own limit (desk_limit_pct)
		"rate_override_limit_pct": desk_limit_pct(property),
		"discount_reasons": discount_reasons(property),
	}


def _discount_reason(reason_code, property: str) -> frappe._dict:
	"""The discount's reason code, or a refusal: it must be an active code of
	a category the property's discounts take (SAP's mandatory order reason)."""
	from kamra.ledger import ensure_default_reason_codes
	ensure_default_reason_codes()
	code = None
	if isinstance(reason_code, str) and reason_code.strip():
		code = frappe.db.get_value(
			"Reason Code", reason_code.strip(),
			["name", "category", "requires_supervisor", "disabled"], as_dict=True)
	categories = discount_reason_categories(property)
	if not code or code.disabled or code.category not in categories:
		frappe.throw(_("Pick a {0} reason code for the discount.").format(_categories_text(categories)),
		             title=_("Discount reason"))
	return code


def discount_governance(property: str, room_type: str | None, rate_plan: str | None,
                        nightly: list[dict], *, reason_code: str | None = None,
                        fixed_rate=0, check: bool = False,
                        supervisor_user: str | None = None,
                        supervisor_pin: str | None = None,
                        fixed_held: bool = False) -> dict:
	"""The rules for a reservation or walk-in discount, on the nights it
	prices ({date, base_rate, discount_amount, rate, gst_rate} - a quote's
	nights or a booking's locked rows).

	- Each discounted night carries an active Rate Change or Adjust reason
	  (the night's own, else the stay's).
	- A rate plan marked not discountable (discount_eligible 0) takes none,
	  unless a Revenue Manager / Hotel Admin fixes the rate (fixed_rate).
	- The front desk may cut each night by up to rate_override_limit_pct; a
	  deeper cut, a night under its rate guardrail floor or a reason code
	  that requires a supervisor is signed off by a Hotel Admin or Finance
	  user with their PIN, never the person asking. A Revenue Manager /
	  Hotel Admin gives any cut short of a full comp.
	- A full comp (a night at zero) is always signed off by another
	  supervisor with their PIN - whoever gives it, a Hotel Admin or
	  System Manager included (separation of duties: nobody approves their
	  own comp).
	- An AI agent is held to the front desk's limit whoever it acts for, and
	  never uses a supervisor's PIN.

	A night is discounted when its discount was entered (a type and a value
	above zero), and every % is read on the basis the desk entered it -
	gross where the property's rates include tax - so a cut that drops the
	night into a lower tax slab is judged as the cut it is.

	fixed_held: the stay already holds a manager's fixed-rate override
	(a re-price of a booked stay is judged with it, not asked for it again).

	check=False is the preview (get_quote, discount_preview): nothing is
	refused and no PIN is checked. check=True enforces it and returns the
	approver."""
	from kamra.authz import supervisors_for, verify_supervisor
	from kamra.credit import _is_agent
	from kamra.pricing import entry_rate, night_rate_places, rail_for_day, rates_include_tax

	inclusive = rates_include_tax(property)
	places = night_rate_places(property)

	def entry(n) -> float:
		# what the desk sees for the night after the discount
		return flt(entry_rate(n.get("rate"), n.get("gst_rate"), inclusive, places))

	discounted = [n for n in nightly if n.get("discount_type") and flt(n.get("discount_value")) > 0]
	base_total = sum(entry(n) + flt(n.get("discount_amount")) for n in nightly)
	discount_total = sum(flt(n.get("discount_amount")) for n in nightly)

	def cut(n) -> float:
		# the discount's share of the night's base, or - under an agreed rate
		# that overrides the rate code - the whole cut from the night's
		# rate-code amount, whichever is deeper
		base = entry(n) + flt(n.get("discount_amount"))
		share = 100 * flt(n.get("discount_amount")) / base if base else 0.0
		code = flt(n.get("rate_code_amount"))
		return max(share, 100 * (code - entry(n)) / code if code else 0.0)

	max_pct = max((cut(n) for n in discounted), default=0.0)
	comp = any(flt(n.get("rate")) <= 0 for n in discounted)
	floor_breach = []
	if discounted:
		rails = frappe.get_all(
			"Rate Guardrail", filters={"property": property, "disabled": 0,
			                           "room_type": ("in", [room_type, "", None])},
			fields=["room_type", "floor_price", "days_of_week"])
		for n in discounted:
			rail = rail_for_day(rails, room_type, getdate(n["date"]).strftime("%a").lower())
			# the floor is on the price basis the rate card uses
			if rail and flt(rail.floor_price) and entry(n) < flt(rail.floor_price) - 0.5 * (10 ** -max(places, 2)):
				floor_breach.append(str(n["date"]))
	limit = desk_limit_pct(property)
	agent = _is_agent()
	manager = _can_set_rate() and not agent
	flag = frappe.db.get_value("Rate Plan", rate_plan, "discount_eligible") if rate_plan else None
	eligible = flag is None or bool(cint(flag))
	fixed = bool(cint(fixed_rate)) or any(
		cint((n.get("night_discount") or {}).get("fixed_rate")) for n in discounted)
	codes, reason_ok = {}, True
	for n in discounted:
		why = n.get("discount_reason") or reason_code
		if why in codes:
			continue
		try:
			codes[why] = _discount_reason(why, property)
		except frappe.ValidationError:
			if check:
				raise
			reason_ok = False
	reason_sup = any(cint(c.requires_supervisor) for c in codes.values())
	desk_needs = max_pct > limit + 0.005 or bool(floor_breach) or reason_sup
	needs_supervisor = bool(discounted) and ((not manager and desk_needs) or comp)
	out = {
		"base_total": round(base_total, places), "discount_total": round(discount_total, places),
		"net_total": round(base_total - discount_total, places),
		"effective_pct": round(100 * discount_total / base_total, 2) if base_total else 0.0,
		"max_pct": round(max_pct, 2), "limit_pct": limit,
		"needs_supervisor": needs_supervisor, "can_set_rate": manager, "agent": agent,
		"eligible": eligible, "fixed_rate": int(fixed), "comp": comp,
		"floor_breach": floor_breach, "reason_requires_supervisor": reason_sup,
		"reason_ok": reason_ok,
		# who can sign it off here (names only); nobody signs their own
		"supervisors": supervisors_for(property, exclude=frappe.session.user)
		if needs_supervisor and not agent else [],
		"approver": None,
	}
	if not check or not discounted:
		return out
	if agent:
		if not set(AGREED_RATE_CALLER_ROLES) & set(frappe.get_roles()):
			_refuse_agent_rate(_("An AI agent can't give a discount on its own."))
		if (supervisor_user or "").strip() or (supervisor_pin or "").strip():
			_refuse_agent_rate(_("An AI agent can't use a supervisor's PIN."))
	if fixed and not manager and not fixed_held:
		if agent:
			_refuse_agent_rate(_("An AI agent can't fix a rate."))
		frappe.throw(_("Only a Revenue Manager or Hotel Admin can fix a rate to discount it."),
		             frappe.PermissionError)
	if not eligible and not (fixed or fixed_held):
		frappe.throw(_("The rate plan {0} can't be discounted at the desk. A Revenue Manager "
		               "or Hotel Admin can fix the rate instead.").format(
			frappe.db.get_value("Rate Plan", rate_plan, "rate_plan_name") or rate_plan),
			title=_("Discount"))
	if not needs_supervisor:
		return out
	why = (_("A night at no charge") if comp
	       else _("A cut of {0}% is over the front desk's limit of {1}%").format(
		f"{round(max_pct, 2):g}", f"{limit:g}") if max_pct > limit + 0.005 and not manager
	       else _("A night under the rate floor") if floor_breach
	       else _("The reason"))
	if agent:
		_refuse_agent_rate(_("{0} needs a manager's sign-off.").format(why))
	if not (supervisor_user or "").strip():
		frappe.throw(_("{0} needs a manager's sign-off: a Hotel Admin or Finance user signs "
		               "it off with their PIN.").format(why), frappe.PermissionError)
	out["approver"] = verify_supervisor(property, supervisor_user, supervisor_pin,
	                                    requester=frappe.session.user)
	return out


def comp_rate_governance(property: str, rate_plan: str | None, *, check: bool = False,
                         supervisor_user: str | None = None,
                         supervisor_pin: str | None = None) -> dict:
	"""A rate plan whose category needs a sign-off (Rate Plan.rate_category
	and the property's rate_categories: OPERA's Complimentary and House Use
	by default) is signed off by another supervisor with their PIN whoever
	books it, as a full comp is (discount_governance): OPERA's comp
	authorizer. The approvers are the roles in rate_category_approvers.
	Nobody signs off their own (verify_supervisor), nor a booking on a plan
	whose category they set (Rate Plan.rate_category_set_by: maker-checker
	against the configuration change too), and an AI agent can't book one or
	use a PIN.

	check=False is the preview (get_quote, amend_stay_preview): nothing is
	refused and no PIN is checked. check=True enforces it and returns the
	approver (Reservation.govern_rate_category).
	{rate_category, needs_supervisor, agent, supervisors, approver}"""
	from kamra.authz import supervisors_for, verify_supervisor
	from kamra.credit import _is_agent
	from kamra.room_statistics import category_rules, rate_category
	rules = category_rules(property)
	category = rate_category(property, rate_plan)
	needs = category in rules.sign_off
	agent = _is_agent()
	# the booker, and whoever set the plan's category
	makers = [frappe.session.user,
	          frappe.db.get_value("Rate Plan", rate_plan, "rate_category_set_by")
	          if rate_plan and needs else None]
	out = {"rate_category": category, "needs_supervisor": needs, "agent": agent,
	       # who can sign it off here (names only); nobody signs their own
	       "supervisors": supervisors_for(property, roles=rules.approvers, exclude=makers)
	       if needs and not agent else [],
	       "approver": None}
	if not check or not needs:
		return out
	why = _("A {0} rate plan").format(_(category))
	if agent:
		_refuse_agent_rate(_("{0} needs a manager's sign-off.").format(why))
	if not (supervisor_user or "").strip():
		frappe.throw(_("{0} needs a manager's sign-off: a {1} user signs it off with their "
		               "PIN.").format(why, _(" or ").join(_(r) for r in rules.approvers)),
		             frappe.PermissionError)
	out["approver"] = verify_supervisor(property, supervisor_user, supervisor_pin,
	                                    roles=rules.approvers, requester=makers)
	return out


def _comp_sign_off(doc, comp_supervisor_user=None, comp_supervisor_pin=None) -> None:
	"""A comp or house-use plan's own sign-off, when the discount's
	(_discount_sign_off) is by a manager who can't approve the comp
	(Reservation.govern_rate_category)."""
	if (comp_supervisor_user or "").strip() or (comp_supervisor_pin or "").strip():
		doc.flags.kamra_comp_supervisor = {"user": comp_supervisor_user, "pin": comp_supervisor_pin}


def _night_rows_as_nights(rows, doc) -> list[dict]:
	"""A booking's night rows in the shape discount_governance reads: each
	night's own Daily Details discount, else the stay's (doc's)."""
	stay = (doc.get("discount_type") or None, flt(doc.get("discount_value")))
	out = []
	for r in rows:
		# a dict from quote_night_rows or a saved row: both read with .get
		get = r.get
		kind, value = ((get("discount_type"), flt(get("discount_value")))
		               if get("discount_type") else stay)
		out.append({"date": str(getdate(get("stay_date"))), "rate": flt(get("rate")),
		            "base_rate": flt(get("base_rate")) or flt(get("rate")),
		            "discount_amount": flt(get("discount_amount")),
		            "discount_type": kind, "discount_value": value,
		            "gst_rate": flt(get("gst_rate")),
		            "rate_code_amount": flt(get("rate_code_amount")),
		            "discount_reason": get("discount_reason") or None,
		            "night_discount": {"fixed_rate": cint(get("fixed_rate"))}})
	return out


def govern_reservation_discount(doc) -> None:
	"""Reservation.validate's call, after the nights are priced: the rules
	on the nights this save locks, for the discount a governed action put on
	the booking (flags.kamra_discount_context). The outcome (approver,
	effective %) waits in flags.kamra_discount_governance for the log."""
	ctx = doc.flags.get("kamra_discount_context")
	if not isinstance(ctx, dict):
		return
	# a night already on the folio keeps what it holds; its rules were
	# settled when it was given, so only the nights still to come are judged
	charged = set(ctx.get("charged") or ())
	nights = [n for n in _night_rows_as_nights(doc.night_rates, doc) if n["date"] not in charged]
	doc.flags.kamra_discount_governance = discount_governance(
		doc.property, doc.room_type, doc.rate_plan, nights,
		reason_code=doc.get("discount_reason") or ctx.get("reason_code"),
		fixed_rate=ctx.get("fixed_rate"), check=True,
		supervisor_user=ctx.get("supervisor_user"), supervisor_pin=ctx.get("supervisor_pin"))


def _repriced_nights(doc, rows, dates) -> list[dict]:
	"""The re-priced nights the discount rules judge: those in `dates`,
	nights already on the folio left out."""
	from kamra.folio import charged_nights
	charged = set(charged_nights(doc.name))
	dates = set(dates)
	return [n for n in _night_rows_as_nights(rows, doc)
	        if n["date"] in dates and n["date"] not in charged]


def govern_repriced_discount(doc, dates) -> None:
	"""Reservation.validate's call when a save re-priced a discounted stay
	(a new rate plan, room type, party or dates): the discount rules on the
	nights that changed, with the stay's own reason and any manager's
	fixed-rate override it already holds. A supervisor signs it off with
	flags.kamra_discount_supervisor ({user, pin}: amend_stay,
	move_reservation, correct_charged_nights); nights already on the folio
	are not judged again."""
	nights = _repriced_nights(doc, doc.night_rates, dates)
	if not nights:
		return
	sign = doc.flags.get("kamra_discount_supervisor") or {}
	doc.flags.kamra_discount_governance = discount_governance(
		doc.property, doc.room_type, doc.rate_plan, nights,
		reason_code=doc.get("discount_reason"), check=True,
		fixed_held=doc.holds_manager_fixed_rate(),
		supervisor_user=sign.get("user"), supervisor_pin=sign.get("pin"))


def _repriced_governance(res) -> dict:
	"""What the discount rules say about a re-price a preview just priced
	(_reprice_preview, res holding the changes in memory): the same nights,
	reasons and fixed-rate override the save judges
	(Reservation.judged_nights), with who can sign it off. Never checks a
	PIN. {discount_governance, needs_supervisor, supervisors}."""
	out = {"discount_governance": None, "needs_supervisor": False, "supervisors": []}
	rows = res.flags.get("kamra_repriced_rows")
	if not rows or not res.has_discount() or res.flags.get("kamra_room_pinned"):
		return out
	saved = res.stored()
	nights = _repriced_nights(res, rows, res.judged_nights(saved, rows)) if saved else []
	if not nights:
		return out
	gov = discount_governance(res.property, res.room_type, res.rate_plan, nights,
	                          reason_code=res.get("discount_reason"),
	                          fixed_held=res.holds_manager_fixed_rate())
	return {"discount_governance": gov, "needs_supervisor": bool(gov["needs_supervisor"]),
	        "supervisors": gov["supervisors"]}


def _discount_sign_off(doc, supervisor_user=None, supervisor_pin=None) -> None:
	"""A supervisor's sign-off handed to the save that re-prices a
	discounted stay (Reservation.regovern_repriced_discount)."""
	if (supervisor_user or "").strip() or (supervisor_pin or "").strip():
		doc.flags.kamra_discount_supervisor = {"user": supervisor_user, "pin": supervisor_pin}


def _set_discount_inputs(doc, disc: dict, reason_code, fixed_rate, *,
                         charged=frozenset(), supervisor_user=None, supervisor_pin=None) -> None:
	"""Put a discount the desk asked for on the booking, for the save that
	prices and governs it: the stay's own fields, each Daily Details night on
	its row (a night not listed follows the stay's discount) and the flags
	that tell Reservation.validate this is a governed action. Nights already
	on the folio keep what they had."""
	stay = (disc or {}).get("stay")
	nights = (disc or {}).get("nights") or {}
	doc.discount_type = stay[0] if stay else None
	doc.discount_value = float(stay[1]) if stay else 0
	doc.discount_reason = (reason_code or "").strip() or None if disc else None
	if cint(fixed_rate):
		doc.fixed_rate = 1
	if (doc.is_new() or not doc.get("name")) and not doc.get("night_rates"):
		# a booking being made: its Daily Details ride on flags and the
		# insert carries no night rows until Reservation.validate locks them
		# (a Before Validate writer - hospitality-tariffs' tariff resolver -
		# refuses a new booking that already has night rows: HSP-TARIFF-007)
		doc.flags.kamra_initial_night_discounts = [
			{"date": day, "discount_type": own["type"] or "Percent",
			 "discount_value": float(own["value"]),
			 "discount_reason": own["reason"] or doc.discount_reason,
			 "fixed_rate": cint(own["fixed_rate"])}
			for day, own in sorted(nights.items())]
		nights = {}
	rows = {str(getdate(r.stay_date)): r for r in doc.get("night_rates") or []}
	for day in nights:
		if day not in rows:
			rows[day] = doc.append("night_rates", {"stay_date": day})
	for day, row in rows.items():
		if day in charged:
			continue
		own = nights.get(day)
		# a night listed with no discount is that night out of the stay's
		row.discount_type = (own["type"] or "Percent") if own else None
		row.discount_value = float(own["value"]) if own else 0
		row.discount_reason = (own["reason"] or doc.discount_reason) if own else None
		row.fixed_rate = cint(own["fixed_rate"]) if own else 0
	doc.flags.kamra_reservation_discount = True
	doc.flags.kamra_discount_context = {
		"reason_code": doc.discount_reason, "fixed_rate": cint(fixed_rate),
		"supervisor_user": supervisor_user, "supervisor_pin": supervisor_pin,
		"charged": sorted(charged)}


def _log_discount(doc, gov: dict | None, before: dict | None = None,
                  channel: str | None = None, note_text: str | None = None) -> None:
	"""The hotel's audit trail for a discount: who gave it, why, what it
	took off and who signed it off, each night's daily details before and
	after (OPERA's Changes Log, SAP's pricing change document) and the
	desk's optional note."""
	from kamra.localization import money_text
	from kamra.savings import log_action
	if not gov:
		return
	what = (f"{doc.discount_value:g}% off" if doc.get("discount_type") == "Percent"
	        else f"{money_text(doc.property, doc.discount_value)} off a night" if doc.get("discount_type")
	        else "Daily Details discount" if gov.get("discount_total") else "discount removed")
	note = (f"{doc.get('discount_reason') or 'no reason'}: {what} · base "
	        f"{money_text(doc.property, gov['base_total'])} less {money_text(doc.property, gov['discount_total'])} = "
	        f"{money_text(doc.property, gov['net_total'])} ({gov['effective_pct']:g}% effective)")
	if doc.get("fixed_rate"):
		note += " · fixed rate"
	if (note_text or "").strip():
		note += f" · {note_text.strip()[:140]}"
	log_action("reservation_discount", "Reservation", doc.name, doc.property,
	           rationale=note, approver=gov.get("approver"),
	           before_snapshot=before,
	           after_snapshot={**_discount_snapshot(doc),
	                           "discount_total": gov["discount_total"],
	                           "amount_after_tax": flt(doc.amount_after_tax)},
	           **({"channel": channel} if channel else {}))


def _discount_plan(doc, discount_type=None, discount_value=0, reason_code=None,
                   night_discounts=None, fixed_rate=0) -> dict:
	"""Put the discount on doc in memory and say what saving it would do:
	each night's base, discount and net (nights already on the folio keep
	what they hold), the new total against the current one, and the rules
	(effective %, whether a supervisor signs it off). Nothing is written.

	A stay whose nights are all locked takes the discount in place on each
	night's booked base (Reservation.rediscount_locked_nights - the save
	runs the same code, so this is exactly what it persists); today's rate
	card, season, demand, rate plan and meal prices never enter it. A night
	already billed may come back unchanged (the editor shows the whole
	stay); changing one is a folio allowance, not a discount."""
	from kamra.folio import charged_nights
	from kamra.pricing import normalize_discounts
	if doc.status not in ("Confirmed", "Checked In"):
		frappe.throw(_("Only an active reservation can take a discount."))
	if not doc.get("auto_price"):
		frappe.throw(_("This booking is priced by hand; change its amount instead."))
	disc = normalize_discounts(discount_type, discount_value, night_discounts,
	                           doc.check_in_date, doc.check_out_date, property=doc.property)
	charged = set(charged_nights(doc.name))
	rows_now = {str(getdate(r.stay_date)): r for r in doc.get("night_rates") or []}

	def unchanged(day, own) -> bool:
		row = rows_now.get(day)
		return bool(row) and (row.get("discount_type") or None) == (own["type"] or "Percent") \
			and abs(flt(row.get("discount_value")) - float(own["value"])) < 0.005
	on_folio = sorted(day for day, own in ((disc or {}).get("nights") or {}).items()
	                  if day in charged and not unchanged(day, own))
	if on_folio:
		frappe.throw(_("{0} is already on the folio: a discount there is a folio allowance "
		               "(Discount on the bill).").format(", ".join(on_folio)))
	current = flt(doc.amount_after_tax)
	# decided before the discount goes on, as the save decides it
	in_place = doc.nights_cover_stay() and doc.lock_is_current()
	_set_discount_inputs(doc, disc, reason_code, fixed_rate, charged=charged)
	if in_place:
		priced = doc.rediscount_locked_nights()
		rows, new = priced["rows"], priced["amount_after_tax"]
	else:
		# a stay booked before its nights were locked: priced as a new one
		q = doc.requote()
		rows = doc.quote_night_rows(q)
		hold = doc.hold_charged_nights(rows)
		from kamra.pricing import property_currency, round_rate_amount
		new = float(round_rate_amount(
			flt(q["amount_after_tax"]) + hold["base"] + hold["tax"], 0, "Nearest",
			property_currency(doc.property),
		))
	nights = _night_rows_as_nights(rows, doc)
	plan = discount_governance(doc.property, doc.room_type, doc.rate_plan,
	                           [n for n in nights if n["date"] not in charged],
	                           reason_code=doc.discount_reason, fixed_rate=fixed_rate)
	from kamra.ledger import _currency_places, _money, _round_currency
	places = max(_currency_places(doc.property), 2)
	eps = 0.5 * (10 ** -places)
	plan.update(
		current_amount=current, new_amount=new,
		difference=_round_currency(_money(new) - _money(current), places),
		needs_confirmation=abs(new - current) > eps,
		nights=[{"date": n["date"], "base_rate": flt(r.get("base_rate")) or flt(r.get("rate")),
		         "discount_type": n["discount_type"],
		         "discount_value": n["discount_value"] if n["discount_type"] else 0,
		         "discount_amount": flt(r.get("discount_amount")), "rate": flt(r.get("rate")),
		         **_entry_figures(doc.property, r),
		         "own": bool(r.get("discount_type")),
		         "charged": n["date"] in charged} for r, n in zip(rows, nights, strict=True)])
	return plan


def _entry_figures(property: str, row) -> dict:
	"""A night's base and net room rate on the basis the desk enters a
	discount (gross where the property's rates include tax), beside the
	before-tax base_rate / rate: base less discount_amount is the net."""
	from kamra.pricing import entry_rate, night_rate_places, rates_include_tax
	places = night_rate_places(property)
	net = flt(entry_rate(row.get("rate"), row.get("gst_rate"), rates_include_tax(property), places))
	return {"entry_base_rate": round(net + flt(row.get("discount_amount")), places), "entry_rate": net}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def discount_preview(reservation: str, discount_type: str | None = None,
                     discount_value: float = 0, reason_code: str | None = None,
                     night_discounts=None, fixed_rate: int = 0):
	"""What a discount would do to this stay before the desk confirms it
	(Stay Details / Daily Details): base, discount and net per night and in
	total, the effective %, the front desk's limit and whether a supervisor
	signs it off. Nothing is written."""
	doc = frappe.get_doc("Reservation", reservation)
	assert_property_access(doc.property)
	return _discount_plan(doc, discount_type, discount_value, reason_code,
	                      night_discounts, fixed_rate)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def set_reservation_discount(reservation: str, discount_type: str | None = None,
                             discount_value: float = 0, reason_code: str | None = None,
                             night_discounts=None, fixed_rate: int = 0,
                             supervisor_pin: str | None = None,
                             supervisor_user: str | None = None, confirm_price: int = 1,
                             discount_note: str | None = None):
	"""Give (or change, or clear) a confirmed or in-house stay's discount: a
	% OR an amount off each night still to come, or Daily Details nights of
	their own (night_discounts). The base rate, the agreed rate and the
	contract snapshot stay as booked; each night locks its base, discount
	and net and the folio bills the net. Nights already on the folio keep
	what they hold (a folio allowance covers those). The rules are
	discount_governance's; a supervisor signs off with supervisor_user +
	supervisor_pin. Call discount_preview first; confirm_price=0 refuses a
	change in the total until it is confirmed. discount_note is the desk's
	optional free text beside the reason code; it goes on the audit row."""
	doc = frappe.get_doc("Reservation", reservation)
	assert_property_access(doc.property)
	before = {**_discount_snapshot(doc), "amount_after_tax": flt(doc.amount_after_tax)}
	plan = _discount_plan(doc, discount_type, discount_value, reason_code,
	                      night_discounts, fixed_rate)
	_require_price_confirmation(plan, confirm_price)
	doc.flags.kamra_discount_context.update(supervisor_user=supervisor_user,
	                                        supervisor_pin=supervisor_pin)
	# the role and limit checks are the permission; Revenue Manager has no
	# write DocPerm on Reservation
	doc.save(ignore_permissions=True)
	gov = doc.flags.get("kamra_discount_governance")
	_log_discount(doc, gov, before, note_text=discount_note)
	return {"ok": True, "reservation": doc.name, "room": doc.room,
	        "discount_type": doc.get("discount_type"),
	        "discount_value": flt(doc.get("discount_value")),
	        "discount_reason": doc.get("discount_reason"),
	        "nights": [{"date": str(r.stay_date), "base_rate": flt(r.base_rate),
	                    "discount_amount": flt(r.discount_amount), "rate": flt(r.rate)}
	                   for r in doc.night_rates],
	        "amount_after_tax": flt(doc.amount_after_tax),
	        "approver": (gov or {}).get("approver")}


# --- the reservation's Changes Log -------------------------------------------
# OPERA Cloud Actions > Changes Log and SAP's pricing change documents
# (CDHDR / CDPOS), from what Frappe and Kamra already record: the Version rows
# Reservation keeps (track_changes) and the audit rows of the governed
# actions. Nothing new is stored.

# the fields the log shows: what is sold, its price and the discount; keys,
# snapshots and tokens never show
CHANGE_LOG_FIELDS = (
	"status", "check_in_date", "check_out_date", "room", "room_type", "rate_plan", "meal_plan",
	"adults", "children", "extra_beds", "voucher", "company", "market_segment", "auto_price",
	"agreed_rate", "fixed_rate", "discount_type", "discount_value", "discount_reason",
	"amount_after_tax")
CHANGE_LOG_TITLES = {
	"create_reservation": "Booked", "reservation_discount": "Discount",
	"rate_override": "Agreed rate", "room_move": "Room move", "amend_stay": "Dates changed",
	"correct_charged_nights": "Rate correction", "set_night_prices": "Nightly prices",
	"channel.modify_needs_review": "Channel update not applied",
	"channel.cancel_needs_review": "Channel cancel not applied",
	"channel.modify_discount_removed": "Channel update — discount removed",
	"channel.modify_discount_review": "Channel update — discount review"}


def _night_rate_changes(before: dict, after: dict) -> list[dict]:
	"""Each night whose rate moved ({date: rate} before and after); a night
	only on one side was added to or taken out of the stay."""
	out = []
	for day in sorted(set(before) | set(after)):
		was, now = before.get(day), after.get(day)
		if was is None or now is None or abs(was - now) >= 0.005:
			out.append({"date": day, "from": was, "to": now})
	return out


def _version_nights(data: dict) -> list[dict]:
	"""The nights a save re-locked, from its Version row: the night rows it
	took off and the ones it put on (Reservation replaces them whole)."""
	def rates(key):
		return {str(getdate(row["stay_date"])): flt(row.get("rate"))
		        for table, row in data.get(key) or []
		        if table == "night_rates" and isinstance(row, dict) and row.get("stay_date")}
	return _night_rate_changes(rates("removed"), rates("added"))


def _snapshot_nights(before, after) -> list[dict]:
	"""The nights an audit row's before / after snapshots hold
	(_discount_snapshot). A discount given at booking has nothing before
	it: each night goes from its base to its rate."""
	def rates(snap, column="rate"):
		try:
			snap = json.loads(snap) if isinstance(snap, str) else snap
		except ValueError:
			return {}
		return {str(n["date"]): flt(n.get(column)) or flt(n.get("rate"))
		        for n in (snap or {}).get("nights") or []
		        if isinstance(n, dict) and n.get("date")} if isinstance(snap, dict) else {}
	was, now = rates(before), rates(after)
	if now and not before:
		was = rates(after, "base_rate")
	return _night_rate_changes(was, now) if was and now else []


def _change_entry(kind: str, by: str | None, at, **values) -> dict:
	from frappe.utils import get_fullname
	approver = values.pop("approver", None)
	return {"kind": kind, "at": str(at), "by": by, "by_name": get_fullname(by) if by else None,
	        "approver": approver, "approver_name": get_fullname(approver) if approver else None,
	        "action_type": None, "title": None, "rationale": None, "fields": [], "nights": [],
	        **values}


def _format_change_value(meta, property: str, fieldname: str, value) -> str:
	"""Format a Version changed-field value in the property's currency when it
	is a Currency field (so historical or site-default Version rows never show
	another currency symbol or wrong minor-unit decimals)."""
	if value in (None, ""):
		return ""
	df = meta.get_field(fieldname)
	if df and df.fieldtype == "Currency":
		import re

		from kamra.localization import money_text
		if isinstance(value, (int, float)):
			return money_text(property, value)
		text = str(value).strip()
		match = re.search(r"\d[\d,]*(?:\.\d+)?", text)
		if match:
			num = float(match.group(0).replace(",", ""))
			if text.startswith("-") or re.search(r"-\s*[^\d\s]", text):
				num = -num
			return money_text(property, num)
	return str(value)


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent", "Finance", "Revenue Manager")
def reservation_changes(reservation: str):
	"""The reservation's Changes Log, newest first: every save Frappe
	recorded on it (its Version rows: the fields in CHANGE_LOG_FIELDS and
	each night's rate, before and after) and every governed action (the
	audit rows: who, why, each night before and after, and who signed it
	off). Read only."""
	res = frappe.get_doc("Reservation", reservation)
	assert_property_access(res.property)
	meta = frappe.get_meta("Reservation")
	entries = []
	for v in frappe.get_all("Version", filters={"ref_doctype": "Reservation", "docname": res.name},
	                        fields=["owner", "creation", "data"], order_by="creation desc", limit=200):
		try:
			data = json.loads(v.data or "{}")
		except ValueError:
			continue
		if not isinstance(data, dict):
			continue
		fields = [{"field": f, "label": meta.get_label(f),
		           "from": _format_change_value(meta, res.property, f, old),
		           "to": _format_change_value(meta, res.property, f, new)}
		          for f, old, new in data.get("changed") or [] if f in CHANGE_LOG_FIELDS]
		nights = _version_nights(data)
		if fields or nights:
			entries.append(_change_entry("change", v.owner, v.creation, fields=fields, nights=nights))
	for a in frappe.get_all(
			"Agent Action Log", filters={"reference_doctype": "Reservation", "reference_name": res.name},
			fields=["action_type", "actor", "approver", "rationale", "creation",
			        "before_snapshot", "after_snapshot"],
			order_by="creation desc", limit=200):
		entries.append(_change_entry(
			"action", a.actor, a.creation, action_type=a.action_type, approver=a.approver,
			title=CHANGE_LOG_TITLES.get(a.action_type) or (a.action_type or "").replace("_", " ").capitalize(),
			rationale=a.rationale, nights=_snapshot_nights(a.before_snapshot, a.after_snapshot)))
	entries.sort(key=lambda e: e["at"], reverse=True)
	return {"entries": entries}


def _move_price(res, new_type: str | None, rate_mode: str,
                agreed_rate: float | None) -> dict:
	"""Price preview for a room move. Leaves res holding the move's price
	inputs (room type, agreed rate) for the save that follows."""
	if rate_mode not in ("destination", "source", "custom"):
		frappe.throw(_("Unknown rate option: {0}").format(rate_mode))
	changes = {} if not new_type or new_type == res.room_type else {"room_type": new_type}
	if rate_mode == "source":
		# keep what the guest pays: nothing is re-priced (move_reservation
		# re-keys the lock), so nothing is judged again and no sign-off is due
		for k, v in changes.items():
			res.set(k, v)
		res.flags.kamra_repriced_rows = None
		current = float(res.amount_after_tax or 0)
		return {"current_amount": current, "new_amount": current, "difference": 0.0,
		        "auto_price": bool(res.get("auto_price")), "reprices": False,
		        "needs_confirmation": False, "nights": []}
	if rate_mode == "custom":
		if not _can_set_rate():
			frappe.throw(_("Only a Revenue Manager or Hotel Admin can enter a new rate."),
			             frappe.PermissionError)
		if float(agreed_rate or 0) <= 0:
			frappe.throw(_("Enter the new nightly rate."))
		if not res.get("auto_price"):
			frappe.throw(_("This booking is priced by hand; change its amount instead."))
		# a rate set by hand replaces the desk's discount (OPERA's Fixed Rate)
		_clear_desk_discount(res)
		changes["agreed_rate"] = float(agreed_rate)
	else:
		changes["agreed_rate"] = 0  # the destination's own rate card
	return _reprice_preview(res, **changes)


class PriceConfirmationRequired(frappe.ValidationError):
	"""An amendment would change what the guest pays; the caller has to see
	the new price (the *_preview endpoint) and pass confirm_price=1."""


def _reprice_preview(res, **changes) -> dict:
	"""What saving these changes would do to the bill - priced by the same
	code the save runs (Reservation.requote), the booking itself left out of
	the house's occupancy. Nothing is written.

	A booking with manual pricing (auto_price off) keeps its amount; one
	whose price inputs do not change keeps its locked nightly rates."""
	current = float(res.amount_after_tax or 0)
	old_key = res.get("rate_lock_key")
	booked = (res.get("check_in_date"), res.get("check_out_date"))
	original_agreed = res.get("agreed_rate")
	for k, v in changes.items():
		res.set(k, v)
	out = {"current_amount": current, "new_amount": current,
	       "difference": 0.0, "auto_price": bool(res.get("auto_price")),
	       "reprices": False, "needs_confirmation": False, "nights": []}
	# a stay with hand-set nights is re-priced around them (the save does it
	# in Reservation.keep_hand_set_nights); any other manual price is kept
	hand = any(flt(r.get("hand_price")) > 0 for r in res.get("night_rates") or [])
	if (not res.get("auto_price") and not hand) or not res.room_type:
		return out
	# new dates: the contract prices the nights it never priced, as the save will
	res.reprice_contract_nights(*booked)
	try:
		inputs = res.quote_inputs()
		if res.get("night_rates") and old_key == res.rate_lock_key_for(inputs):
			return out  # same stay, same locked rates
		q = res.requote()
		# the same nights the save locks: charged nights held by the policy
		rows = res.quote_night_rows(q)
		hold = res.hold_charged_nights(rows)
		# for a caller that judges them (_rate_override_plan); never saved
		res.flags.kamra_repriced_rows = rows
		from kamra.pricing import property_currency, round_rate_amount
		new = float(round_rate_amount(
			float(q["amount_after_tax"]) + hold["base"] + hold["tax"], 0, "Nearest",
			property_currency(res.property),
		))
		from kamra.ledger import _currency_places, _money, _round_currency
		places = max(_currency_places(res.property), 2)
		eps = 0.5 * (10 ** -places)
		out.update({
			"new_amount": new,
			"difference": _round_currency(_money(new) - _money(current), places),
			"reprices": True,
			"needs_confirmation": abs(new - current) > eps,
			"places": places,
			"nights": [{"date": str(r["stay_date"]), "rate": r["rate"],
			            "demand_premium_pct": r["demand_premium_pct"]}
			           for r in rows],
		})
		if hold["nights"]:
			out["charged_nights"] = _charged_nights_summary(hold, places=places)
		return out
	finally:
		if "agreed_rate" not in changes:
			res.agreed_rate = original_agreed


def _charged_nights_summary(hold: dict, places: int = 2) -> dict:
	"""What a rate change does to the nights already on the folio, for a
	preview: the policy, each night touched, what stays above the new price
	(kept nights - the desk may give it back as an allowance) and the
	credit lines Kamra posts (tax included)."""
	from kamra.ledger import _round_currency
	lines = hold["lines"]
	return {
		"policy": hold["policy"],
		"nights": hold["nights"],
		"kept_difference": _round_currency(hold["base"] + hold["tax"], places),
		"credit_total": _round_currency(sum(
			l["amount"] * (1 + l["gst_rate"] / 100) for l in lines), places),
	}


def _require_price_confirmation(preview: dict, confirm_price) -> None:
	if preview["needs_confirmation"] and not cint(confirm_price):
		places = int(preview.get("places") or 2)
		frappe.throw(
			_("This change re-prices the stay from {0} to {1}. Show the guest "
			  "the new price, then confirm it (confirm_price=1).").format(
				f"{preview['current_amount']:,.{places}f}",
				f"{preview['new_amount']:,.{places}f}"),
			exc=PriceConfirmationRequired, title=_("Confirm the new price"))


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def room_move_preview(reservation: str, new_room: str,
                      rate_mode: str = "destination",
                      agreed_rate: float | None = None):
	"""What a move would do to the bill before the desk confirms it: a
	same-type swap keeps the price (and the locked nightly rates); an
	upgrade / downgrade on an auto-priced booking is re-quoted at the new
	type's rate by the pricing engine, and move_reservation then needs
	confirm_price=1."""
	res = frappe.get_doc("Reservation", reservation)
	from kamra.crs import assert_property_access
	assert_property_access(res.property)
	new_type = frappe.db.get_value("Room", new_room, "room_type")
	same_type = new_type == res.room_type
	pv = _move_price(res, new_type, rate_mode, agreed_rate)
	return {
		# a discounted stay re-priced for its new room: the discount rules
		# the move will run, and who can sign them off
		**_repriced_governance(res),
		"same_type": same_type,
		"new_room_type": new_type,
		"new_room_type_name": frappe.db.get_value(
			"Room Type", new_type, "room_type_name") if new_type else None,
		"current_amount": pv["current_amount"],
		"new_amount": pv["new_amount"],
		"difference": pv["difference"],
		"needs_confirmation": pv["needs_confirmation"],
		"nights": pv["nights"],
		"auto_price": pv["auto_price"],
		"in_house": res.status == "Checked In",
		"can_set_rate": _can_set_rate(),
	}


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def movable_rooms(reservation: str, check_in_date: str | None = None,
                  check_out_date: str | None = None):
	"""Every room the booking could move into - across ALL room types, so the
	front desk can upgrade (Standard -> Suite) as well as swap same-type. Each
	room is flagged free/occupied for the dates and carries its type name; the
	booking's current type is listed first."""
	res = frappe.get_doc("Reservation", reservation)
	ci = check_in_date or res.check_in_date
	co = check_out_date or res.check_out_date
	from kamra.crs import assert_property_access
	assert_property_access(res.property)
	rooms = frappe.get_all(
		"Room", filters={"property": res.property},
		fields=["name", "room_number", "room_type", "floor",
		        "housekeeping_status"], order_by="room_number")
	# availability is computed per type; union the free rooms across every type
	types = {r.room_type for r in rooms}
	free = set()
	for rt in types:
		free |= {r.name for r in _available_rooms_raw(res.property, rt, ci, co)}
	if getdate(ci) <= getdate(nowdate()):
		# a guest still checked in holds the room until they check out,
		# whatever their departure date says (as available_rooms ready_now)
		free -= in_house_rooms(res.property)
	type_name = {
		t: (frappe.db.get_value("Room Type", t, "room_type_name") or t)
		for t in types
	}
	out = [
		{"name": r.name, "room_number": r.room_number,
		 "room_type": r.room_type, "floor": r.floor,
		 "housekeeping_status": r.housekeeping_status,
		 "current": r.name == res.room,
		 "room_type_name": type_name.get(r.room_type, r.room_type),
		 # the guest's own current room counts as available to them
		 "free": r.name in free or r.name == res.room,
		 "same_type": r.room_type == res.room_type}
		for r in rooms
	]
	# current type first, then the rest - so a same-type swap is the default and
	# upgrades/downgrades follow
	out.sort(key=lambda r: (not r["same_type"], r["room_type_name"], r["room_number"]))
	return out


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def amend_stay_preview(reservation: str, check_in_date: str, check_out_date: str,
                       adults: int | None = None, children: int | None = None,
                       rate_plan: str | None = None):
	"""What new dates (or a new party: adults, children) would do to the
	bill, before anything changes: the current and new totals and the new
	nightly rates, priced by the engine from today's rules. amend_stay needs
	confirm_price=1 when the total changes. For a stay in house,
	`charged_nights` says what the property's rate-change policy does to
	nights already on the folio: kept at their charged rate (kept_difference
	is what the desk may give back as an allowance) or credited
	(credit_total, posted by Kamra). A discounted stay also gets the
	discount rules the amendment will run (discount_governance,
	needs_supervisor and who can sign it off: supervisors). A new rate_plan
	gets comp_governance: a Complimentary or House Use plan's sign-off."""
	doc = frappe.get_doc("Reservation", reservation)
	from kamra.crs import assert_property_access
	assert_property_access(doc.property)
	plan = _plan_change(doc, rate_plan, check_in_date=check_in_date, check_out_date=check_out_date)
	pv = _reprice_preview(doc, check_in_date=check_in_date,
	                      check_out_date=check_out_date, **_party(adults, children), **plan)
	return {**pv, **_repriced_governance(doc),
	        "comp_governance": comp_rate_governance(doc.property, plan["rate_plan"])
	        if plan else None}


def _is_tariff_target_plan(doc, plan: str, check_in_date=None, check_out_date=None) -> bool:
	"""True when `plan` is priced by a contract tariff (via its own or base
	plan's custom_tariff_price_list, or via the hospitality_tariffs resolver)."""
	from kamra.channel_manager import _rate_plan_has_tariff
	if _rate_plan_has_tariff(plan):
		return True
	if not doc.room_type:
		return False
	from kamra.pricing import ContractRateRefused, billable_children, contract_rate
	try:
		return bool(contract_rate(
			doc.property, doc.room_type, plan,
			check_in_date or doc.check_in_date, check_out_date or doc.check_out_date,
			doc.adults, billable_children(doc), doc.get("extra_beds"), company=doc.get("company"),
		))
	except ContractRateRefused:
		return True


def _plan_change(doc, rate_plan, check_in_date=None, check_out_date=None) -> dict:
	"""amend_stay's rate plan change: the plan resolved at the stay's
	property, when it is another plan than the stay's.

	Contract-tariff stays keep their locked contract rate (hospitality-tariffs@11)
	and cannot be switched onto or off a tariff plan in Amend stay: keeping the
	tariff's agreed_rate on another plan would misclassify a paid stay as
	Complimentary, and switching onto a tariff plan without resolving its
	contract would fall back to the local card rate."""
	if rate_plan in (None, ""):
		return {}
	from kamra.booking_refs import resolve_rate_plan
	plan = resolve_rate_plan(doc.property, rate_plan)
	if not plan or plan == doc.get("rate_plan"):
		return {}
	from kamra.channel_manager import _is_tariff_owned_stay
	if _is_tariff_owned_stay(doc):
		frappe.throw(
			_("A contract-tariff stay's rate plan cannot be changed in Amend stay. "
			  "Apply a reservation discount, set an agreed rate, or rebook on the new plan."),
			title=_("Rate plan"))
	if _is_tariff_target_plan(doc, plan, check_in_date=check_in_date, check_out_date=check_out_date):
		frappe.throw(
			_("A stay cannot be moved onto a contract-tariff rate plan in Amend stay. "
			  "Rebook on the tariff plan so its contract rate and currency are resolved."),
			title=_("Rate plan"))
	return {"rate_plan": plan}


def _party(adults=None, children=None) -> dict:
	"""A party change for amend_stay: only what was given, refused when it
	is not a party (at least one adult, no fewer than no children)."""
	out = {}
	if adults not in (None, ""):
		if cint(adults) < 1:
			frappe.throw(_("A stay needs at least one adult."), title=_("Room capacity"))
		out["adults"] = cint(adults)
	if children not in (None, ""):
		if cint(children) < 0:
			frappe.throw(_("Children can't be fewer than none."), title=_("Room capacity"))
		out["children"] = cint(children)
	return out


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def amend_stay(reservation: str, check_in_date: str, check_out_date: str,
               confirm_price: int = 0, supervisor_user: str | None = None,
               supervisor_pin: str | None = None, adults: int | None = None,
               children: int | None = None, rate_plan: str | None = None,
               comp_supervisor_user: str | None = None, comp_supervisor_pin: str | None = None):
	"""Extend / shorten a stay; the overlap guard validates the new window.

	An auto-priced stay is re-quoted for its new dates and its nightly
	rates re-locked. That changes what the guest pays, so call
	amend_stay_preview first, show the new price, and pass confirm_price=1;
	without it a change in the total is refused. A manually-priced stay
	keeps its amount. Nights already charged are never re-priced in place:
	the property's rate-change policy keeps them or credits them
	(Reservation.hold_charged_nights).

	A discounted stay's new nights take its discount and the discount rules
	again (discount_governance): a cut over the desk's limit or under the
	rate floor is signed off with supervisor_user + supervisor_pin.

	adults / children change the party the same way (the governed party
	change): the room is re-priced for it - unless the stay holds a fixed
	rate, which keeps its room rate (OPERA) - and the discount rules run
	again, with the same sign-off.

	rate_plan moves the stay to another plan (OPERA Stay Details), re-priced
	the same way. A Complimentary or House Use plan is signed off with
	supervisor_user + supervisor_pin when that manager can approve it, else
	with comp_supervisor_user + comp_supervisor_pin (comp_rate_governance)."""
	doc = frappe.get_doc("Reservation", reservation)
	from kamra.crs import assert_property_access
	assert_property_access(doc.property)
	_discount_sign_off(doc, supervisor_user, supervisor_pin)
	_comp_sign_off(doc, comp_supervisor_user, comp_supervisor_pin)
	return _amend_dates(
		doc, check_in_date, check_out_date, confirm_price,
		party=_party(adults, children),
		plan=_plan_change(doc, rate_plan, check_in_date=check_in_date, check_out_date=check_out_date),
	)


def _amend_dates(doc, check_in_date: str, check_out_date: str,
                 confirm_price: int = 0, note: str = "", party: dict | None = None,
                 plan: dict | None = None) -> dict:
	"""amend_stay's writer, for callers that already checked access (the
	early check-out shortens a stay through it)."""
	if doc.status not in ("Confirmed", "Checked In"):
		frappe.throw("Only active reservations can be amended.")
	old = f"{doc.check_in_date}→{doc.check_out_date}"
	old_party = f"{cint(doc.adults)}+{cint(doc.children)}"
	old_plan = doc.get("rate_plan")
	old_amount = float(doc.amount_after_tax or 0)
	_require_price_confirmation(
		_reprice_preview(doc, check_in_date=check_in_date,
		                 check_out_date=check_out_date, **(party or {}), **(plan or {})),
		confirm_price)
	if plan:
		doc.flags.kamra_rate_plan_change = True
	doc.save()
	from kamra.savings import log_action
	discount_approver = (doc.flags.get("kamra_discount_governance") or {}).get("approver")
	# a move to a comp or house-use plan: who signed it off
	# (Reservation.log_comp_sign_off)
	comp_approver = doc.flags.get("kamra_comp_approver")
	approver = discount_approver or comp_approver
	log_action("amend_stay", "Reservation", doc.name, doc.property,
	           rationale=f"{old} → {check_in_date}→{check_out_date}; "
	                     + (f"party {old_party} → {cint(doc.adults)}+{cint(doc.children)}; "
	                        if party else "")
	                     + (f"rate plan {old_plan or 'none'} → {doc.rate_plan}; " if plan else "")
	                     + f"total {old_amount:,.2f} → "
	                     f"{float(doc.amount_after_tax or 0):,.2f}"
	                     + (" (new price confirmed)" if cint(confirm_price) else "")
	                     + (f"; discount signed off by {discount_approver}"
	                        if discount_approver else "")
	                     + (f"; rate category signed off by {comp_approver}" if comp_approver else "")
	                     + (f"; {note}" if note else ""),
	           **({"approver": approver} if approver else {}))
	return {"ok": True, "nights": doc.nights,
	        "previous_amount": old_amount,
	        "amount_after_tax": doc.amount_after_tax}


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Hotel Admin")
def correct_charged_nights(reservation: str, reason: str | None = None,
                           pin: str | None = None,
                           supervisor: str | None = None, dry_run: int = 0):
	"""Correct the rate of nights already charged, for a hotel that fixed a
	wrong tariff: each charged night whose rate differs from today's quote
	is reversed and posted again at that rate (both lines dated the business
	date, the night named in the description, each taxed at its own rate);
	nights still to come take the rate directly. Posted lines are never
	edited. Only when the property's rate-change policy is Correct
	back-dated rates; needs a reason and the PIN of the Finance / Hotel
	Admin user approving it (supervisor, default the caller). On a
	discounted stay the re-quote takes the discount rules again, and that
	approver signs them off too - a person other than the caller (an
	over-limit cut by a Finance user, any night at no charge). dry_run=1
	lists the nights without changing anything. Not an agent tool."""
	doc = frappe.get_doc("Reservation", reservation)
	from kamra.crs import assert_property_access
	assert_property_access(doc.property)
	from kamra.settings import get_setting
	if get_setting(doc.property, "charged_night_rate_change") != "Correct back-dated rates":
		frappe.throw(_("Correcting charged nights is off. Choose Correct back-dated "
		               "rates under Settings, Stay policies, Rate changes in house."))
	if not doc.get("auto_price") or not doc.room_type:
		frappe.throw(_("This stay is priced by hand; its rates are not re-quoted."))
	if doc.status not in ("Checked In", "Checked Out"):
		frappe.throw(_("Only a stay with charged nights can be corrected."))
	hold = doc.hold_charged_nights(doc.quote_night_rows(doc.requote()), correct=True)
	corrected = [n for n in hold["nights"] if n["action"] == "corrected"]
	summary = {"nights": corrected, "lines": len(hold["lines"]),
	           "current_amount": float(doc.amount_after_tax or 0)}
	if cint(dry_run):
		return summary
	if not corrected:
		frappe.throw(_("Every charged night already has the current rate."))
	if not (reason or "").strip():
		frappe.throw(_("A correction needs a reason. It goes on the record."))
	from kamra.authz import verify_supervisor
	approver = verify_supervisor(doc.property, supervisor or frappe.session.user, pin)
	# a folio correction: Finance or Hotel Admin only, whatever other roles
	# a supervisor list may grow to (the POS's F&B Manager, say)
	if not {"Finance", "Hotel Admin"} & set(frappe.get_roles(approver)):
		frappe.throw(_("{0} cannot approve this. A rate correction needs a "
		               "Finance or Hotel Admin user.").format(approver),
		             frappe.PermissionError)
	# the re-quote takes a discounted stay's discount rules again on the
	# nights still to come: the correction's approver signs them off, never
	# the person correcting (maker-checker, as amend_stay / move_reservation)
	_discount_sign_off(doc, approver, pin)
	doc.flags.correct_charged_nights = True
	doc.rate_lock_key = None  # re-quote, so the save runs the correction
	# Finance has no write on Reservation; the gate is this endpoint
	doc.save(ignore_permissions=True)
	from kamra.savings import log_action
	log_action("correct_charged_nights", "Reservation", doc.name, doc.property,
	           rationale=f"{reason.strip()}; approved by {approver}; "
	                     + ", ".join(f"{n['date']} {n['charged']:,.2f} to {n['new']:,.2f}"
	                                 for n in corrected)
	                     + f"; total {summary['current_amount']:,.2f} to "
	                       f"{float(doc.amount_after_tax or 0):,.2f}")
	return {**summary, "ok": True, "amount_after_tax": doc.amount_after_tax}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def set_late_arrival(reservation: str, late_arrival: int = 1,
                     eta: str | None = None):
	"""Flag (or unflag) a confirmed arrival as arriving late, optionally with
	the ETA the guest gave. With the property's late-arrival protection on,
	the night audit holds a flagged arrival as Due In instead of marking it
	a no-show; the next audit decides."""
	doc = frappe.get_doc("Reservation", reservation)
	from kamra.crs import assert_property_access
	assert_property_access(doc.property)
	if doc.status != "Confirmed":
		frappe.throw(_("Only an arrival that has not checked in can be flagged late."))
	values = {"late_arrival": 1 if cint(late_arrival) else 0}
	if eta is not None:
		values["eta"] = (eta or "").strip()[:60] or None
	frappe.db.set_value("Reservation", doc.name, values)
	from kamra.savings import log_action
	log_action("late_arrival", "Reservation", doc.name, doc.property,
	           rationale=(f"{doc.name} flagged late arrival"
	                      if values["late_arrival"] else
	                      f"{doc.name} late-arrival flag cleared")
	                     + (f" · ETA {values['eta']}" if values.get("eta") else ""))
	return {"ok": True, **values}


def _sellable_room_types(property: str, room_type_names: list[str]) -> set[str]:
	"""Room types that have at least one physical room or active sellable unit."""
	if not room_type_names:
		return set()
	from kamra.siu.availability import has_active_sius
	with_rooms = set(frappe.get_all(
		"Room",
		filters={"property": property, "room_type": ("in", list(room_type_names))},
		pluck="room_type",
	))
	return {rt for rt in room_type_names if rt in with_rooms or has_active_sius(property, rt)}


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def booking_options(property: str):
	"""Everything the booking form needs to render its dropdowns."""
	from kamra.channel_manager import channel_commission_pcts
	from kamra.rates import default_plan
	from kamra.settings import get_setting
	plans = frappe.get_all(
		"Rate Plan", filters={"property": property, "disabled": 0},
		fields=["name", "rate_plan_name", "code", "is_default", "discount_eligible"],
		order_by="rate_plan_name asc, name asc")
	all_room_types = frappe.get_all(
		"Room Type", filters={"property": property, "disabled": 0},
		fields=["name", "room_type_code", "room_type_name", "base_price",
		        "adults_capacity", "children_capacity", "room_category",
		        "max_extra_beds"],
		# cheapest first; name breaks ties so the dialog default is stable (#549)
		order_by="base_price asc, room_type_name asc",
	)
	sellable = _sellable_room_types(property, [rt["name"] for rt in all_room_types])
	return {
		"room_types": [rt for rt in all_room_types if rt["name"] in sellable],
		"meal_plans": frappe.get_all(
			"Meal Plan", filters={"property": property, "disabled": 0},
			fields=["name", "code", "label", "price_per_adult", "is_default"],
			order_by="price_per_adult asc",
		),
		"rate_plans": plans,
		"default_plan": default_plan(plans),
		"rate_plan_required": bool(get_setting(property, "rate_plan_required")),
		"companies": frappe.get_all(
			"Corporate Account", filters={"disabled": 0},
			fields=["name", "company_name", "negotiated_rate_plan"],
		),
		"travel_agents": frappe.get_all(
			"Travel Agent", filters={"disabled": 0},
			fields=["name", "agent_name", "commission_pct"],
			order_by="agent_name asc",
		),
		"experiences": frappe.get_all(
			"Experience", filters={"property": property, "disabled": 0},
			fields=["name", "experience_name", "category", "price",
			        "gst_rate"],
			order_by="price asc",
		),
		"property": _booking_property_policy(property),
		"ota_channels": _ota_channels(property),
		"ota_commission": channel_commission_pcts(),
		# who may type a night's price on the booking screen (night_prices)
		"can_set_rate": _can_set_rate(),
		# the inline discount: who may give one, the desk's limit, the reasons
		**discount_options(property),
	}


def _refuse_taken_ota_ref(property: str, ota_ref: str | None,
                          group_booking: str | None = None) -> None:
	"""One OTA reference books once. The rooms of one multi-room booking
	share it (same group booking); any other booking with it is refused."""
	if not ota_ref:
		return
	from kamra.kamra.doctype.reservation.reservation import lock_property_for_inventory
	lock_property_for_inventory(property)
	rows = frappe.db.sql(
		"""SELECT name, group_booking FROM `tabReservation`
		   WHERE property = %s AND ota_ref = %s AND status != 'Cancelled'
		   FOR UPDATE""",
		(property, ota_ref), as_dict=True)
	for r in rows:
		if not group_booking or r.group_booking != group_booking:
			frappe.throw(_("OTA booking {0} is already reservation {1}.").format(
				ota_ref, r.name))


def _ota_channels(property: str) -> list[str]:
	"""The common OTAs, then any other site this property has booked through."""
	from kamra.channel_manager import OTA_CHANNELS
	used = frappe.get_all("Reservation", filters={
		"property": property, "source": "OTA", "channel": ["is", "set"]},
		pluck="channel", distinct=True, limit=50)
	return OTA_CHANNELS + sorted({c for c in used if c not in OTA_CHANNELS})


def _booking_property_policy(property: str) -> dict:
	"""Property policy fields for the booking dialog — never null strings."""
	prop = frappe.db.get_value(
		"Property", property,
		["sell_message", "free_cancel_days", "cancellation_fee",
		 "no_show_charge", "deposit_pct", "country", "pets_allowed",
		 "pet_charge", "pet_charge_basis", "checkin_time", "checkout_time"],
		as_dict=True,
	) or {}
	# the property's standard times: the booking dialog offers them as the
	# arrival and departure time of an overnight stay
	prop["checkin_time"] = str(prop.get("checkin_time") or "14:00")[:5]
	prop["checkout_time"] = str(prop.get("checkout_time") or "11:00")[:5]
	prop["pets_allowed"] = int(prop.get("pets_allowed") or 0)
	prop["pet_charge"] = float(prop.get("pet_charge") or 0)
	prop["pet_charge_basis"] = prop.get("pet_charge_basis") or "Per stay"
	prop["cancellation_fee"] = prop.get("cancellation_fee") or "None"
	prop["no_show_charge"] = prop.get("no_show_charge") or "None"
	prop["free_cancel_days"] = int(prop.get("free_cancel_days") or 0)
	prop["deposit_pct"] = float(prop.get("deposit_pct") or 0)
	prop["sell_message"] = prop.get("sell_message") or ""
	# drives the dial-code prefix on guest phone inputs
	from kamra.localization import resolve_country
	prop["country"] = resolve_country(property, prop)
	return prop


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def get_quote(property: str, room_type: str, check_in_date: str,
              check_out_date: str, adults: int = 2, children: int = 0,
              meal_plan: str | None = None, rate_plan: str | None = None,
              voucher_code: str | None = None,
              over_capacity_charge: float = 0, pets: int = 0,
              company: str | None = None,
              check_in_time: str | None = None,
              check_out_time: str | None = None,
              night_prices=None, extra_beds: int = 0,
              discount_type: str | None = None, discount_value: float = 0,
              discount_reason: str | None = None, night_discounts=None,
              fixed_rate: int = 0, supervisor_pin: str | None = None,
              supervisor_user: str | None = None):
	"""Price a stay. With a company and no rate plan, the company's
	negotiated plan applies - the same plan create_booking stores.
	check_in_time / check_out_time price an hourly or block day-use plan.

	night_prices ([{date, price}], a Revenue Manager / Hotel Admin only)
	prices those nights at the typed figure - tax included when the
	property's rates include tax - and the rest at the rate card. A voucher,
	rate plan, long-stay discount or holiday package with typed nights is
	refused.

	discount_type / discount_value / discount_reason / night_discounts
	price the desk's discount the way create_booking will, and
	discount_governance says what booking it takes (the effective %, the
	front desk's limit, whether a supervisor signs it off). A quote never
	checks a PIN: supervisor_user / supervisor_pin are accepted so a booking
	screen can send one payload, and ignored."""
	from kamra.booking_refs import resolve_meal_plan, resolve_rate_plan, resolve_room_type
	from kamra.pricing import ContractRateRefused, quote_with_contract, resolve_booking_rate_plan, tax_basis

	# agents name things the way staff do ("Deluxe", "breakfast"); resolve
	# to IDs at this property or refuse with the valid options
	room_type = resolve_room_type(property, room_type)
	meal_plan = resolve_meal_plan(property, meal_plan)
	rate_plan = resolve_rate_plan(property, rate_plan)
	typed = _check_night_prices(
		property, room_type, check_in_date, check_out_date, night_prices,
		voucher_code=voucher_code, rate_plan=rate_plan, company=company)
	if not typed:
		rate_plan = resolve_booking_rate_plan(
			property, room_type, check_in_date, check_out_date,
			rate_plan=rate_plan, company=company,
			adults=int(adults), children=int(children), extra_beds=extra_beds,
		)
	try:
		q = quote_with_contract(property, room_type, check_in_date, check_out_date,
		                        int(adults), int(children), meal_plan or None,
		                        rate_plan or None, voucher_code or None,
		                        over_capacity_charge=float(over_capacity_charge or 0),
		                        pets=int(pets or 0), company=company or None,
		                        check_in_time=check_in_time or None, check_out_time=check_out_time or None,
		                        extra_beds=extra_beds,
		                        night_prices=[{"date": d, "price": str(p)} for d, p in typed.items()],
		                        discount_type=discount_type or None,
		                        discount_value=discount_value, night_discounts=night_discounts)
	except ContractRateRefused as exc:
		frappe.throw(str(exc))
	if discount_type or night_discounts not in (None, "", [], {}):
		q["discount_governance"] = discount_governance(
			property, room_type, q.get("rate_plan"), q["nightly"],
			reason_code=discount_reason, fixed_rate=fixed_rate)
	# a Complimentary or House Use plan: who signs the booking off
	q["comp_governance"] = comp_rate_governance(property, q.get("rate_plan") or rate_plan)
	q["tax_basis"] = tax_basis(property)
	q["cancellation"] = cancellation_outlook(property, room_type, check_in_date, check_out_date)
	return q


def _fee_label(basis: str) -> str:
	"""'First Night' -> 'the first night'; '50% of stay' -> '50% of the stay'."""
	if "%" in basis:
		return basis.replace("of stay", "of the stay")
	return f"the {basis.lower()}"


def cancellation_outlook(property: str, room_type: str, check_in_date: str,
                         check_out_date: str) -> dict:
	"""The cancellation terms a quote can promise, decided by the same rules
	cancellation_preview applies when someone actually cancels (season,
	tiers, then the single free window) - evaluated from today, so a window
	that has already closed is never offered as a future date.

	free_now: cancelling today costs nothing. free_until: the last date that
	is still free (None while free_now means free right up to arrival)."""
	from frappe.utils import add_days, date_diff

	from kamra.pricing import stay_season_rules

	days_now = date_diff(check_in_date, nowdate())
	rules = stay_season_rules(property, check_in_date, check_out_date,
	                          room_type)
	if rules["non_refundable"]:
		return {"free_now": False, "free_until": None,
		        "fee_basis": "100% of stay",
		        "fee_label": _fee_label("100% of stay"),
		        "non_refundable_season": rules["season"]
		        or "Non-refundable dates"}

	def charge(d: int) -> tuple[bool, str]:
		"""(charged?, basis) for a cancellation d days before arrival -
		the branch _cancellation_terms takes."""
		tier = cancellation_tier_for(property, d)
		if tier:
			return tier["charge_pct"] > 0, f"{tier['charge_pct']:g}% of stay"
		policy = frappe.db.get_value(
			"Property", property, ["free_cancel_days", "cancellation_fee"],
			as_dict=True)
		basis = policy.cancellation_fee or "None"
		inside = d < int(policy.free_cancel_days or 0)
		return inside and basis != "None", basis

	charged_now, basis_now = charge(days_now)
	if charged_now:
		return {"free_now": False, "free_until": None,
		        "fee_basis": basis_now, "fee_label": _fee_label(basis_now),
		        "non_refundable_season": None}

	# free today: walk towards arrival to the first day that costs. Charges
	# only change where a tier band starts or where the free window ends,
	# so those are the only days worth asking about.
	free_days = int(frappe.db.get_value(
		"Property", property, "free_cancel_days") or 0)
	tiers = frappe.get_all(
		"Cancellation Tier",
		filters={"parenttype": "Property", "parentfield": "cancellation_tiers",
		         "parent": property},
		pluck="days_before")
	edges = {int(t or 0) - 1 for t in tiers} | {free_days - 1, -1}
	for d in sorted((e for e in edges if e < days_now), reverse=True):
		charged, basis = charge(d)
		if charged:
			last_free = add_days(check_in_date, -(d + 1))
			return {"free_now": True, "free_until": str(last_free),
			        "fee_basis": basis, "fee_label": _fee_label(basis),
			        "non_refundable_season": None}
	return {"free_now": True, "free_until": None, "fee_basis": "None",
	        "fee_label": "", "non_refundable_season": None}


def _phone_for(raw: str | None, property: str | None) -> str:
	"""A phone read with the property's country (kamra.phone): E.164, or as
	typed when it can't be read. Blank stays blank."""
	from kamra import phone as phone_mod
	return phone_mod.for_property(raw, property)[0]


def _find_or_create_guest(guest_name: str, phone: str | None,
                          property: str | None = None, where: str = "a booking"):
	# read with the property's country (#468). A local number is stored as
	# E.164; one already written with + is kept as written. A number that
	# can't be read is kept as typed and flagged - never refused.
	from kamra import phone as phone_mod
	typed = (phone or "").strip()
	phone, problem = phone_mod.for_property(typed, property)
	if phone:
		# a profile saved before numbers were normalised, or stored as
		# E.164, still matches a spaced or local writing of the same number
		existing = frappe.db.get_value("Guest", {"phone": phone_mod.matching(typed, property)})
		if existing:
			return existing
	parts = guest_name.strip().split(" ", 1)
	# the likely nationality follows the property's country, not India's
	from kamra.localization import pack_for
	nationality = getattr(pack_for(property), "DEFAULT_NATIONALITY", None) \
		if property else None
	guest = frappe.get_doc({
		"doctype": "Guest",
		"first_name": parts[0],
		"last_name": parts[1] if len(parts) > 1 else "",
		"phone": phone or None,
		"nationality": nationality or None,
	}).insert(ignore_permissions=True)
	phone_mod.flag(problem, "Guest", guest.name, property, where)
	return guest.name


def _guest_for_row(guest_name: str, phone: str | None, property: str | None = None) -> str:
	"""The guest for one name typed on a rooming list. A shared phone (one
	number for a family or an office) never replaces the typed name: the
	existing guest is used only when the name matches too."""
	from kamra import phone as phone_mod
	typed = (phone or "").strip()
	phone = phone_mod.for_property(typed, property)[0]
	if phone:
		found = phone_mod.matching(typed, property)
		same = frappe.db.get_value(
			"Guest", {"phone": found, "full_name": guest_name.strip()}, "name")
		if same:
			return same
		if frappe.db.exists("Guest", {"phone": found}):
			parts = guest_name.strip().split(" ", 1)
			from kamra.localization import pack_for
			nationality = getattr(pack_for(property), "DEFAULT_NATIONALITY", None) \
				if property else None
			return frappe.get_doc({
				"doctype": "Guest", "first_name": parts[0],
				"last_name": parts[1] if len(parts) > 1 else "",
				"phone": phone, "nationality": nationality or None,
			}).insert(ignore_permissions=True).name
	return _find_or_create_guest(guest_name, phone, property)


def _blacklist_gate(guest: str, property: str, override_reason: str | None):
	"""A blacklisted guest is refused unless a Hotel Admin confirms with a
	reason. The override is scoped to this guest for this request only and
	is logged; Reservation.validate_blacklist still refuses everyone else."""
	flagged, reason = frappe.db.get_value(
		"Guest", guest, ["blacklisted", "blacklist_reason"]) or (0, None)
	if not flagged:
		return
	override_reason = (override_reason or "").strip()
	if not override_reason:
		return  # validate_blacklist refuses with the standing reason
	if not _is_admin():
		frappe.throw(_("Only a Hotel Admin can book a blacklisted guest."),
		             frappe.PermissionError)
	frappe.flags.kamra_blacklist_override = guest
	from kamra.savings import log_action
	log_action(
		action_type="blacklist_override",
		reference_doctype="Guest",
		reference_name=guest,
		property=property,
		rationale=f"Booked despite blacklist ({reason or 'no reason'}): {override_reason}",
	)


# How long an identical agent booking counts as a retry of the first one.
from kamra.reservation_state import AGENT_DUPLICATE_WINDOW_MINUTES  # noqa: E402


def agent_channel() -> str | None:
	"""The channel of the agent tool call running now ("MCP" from the MCP
	registry, "Chat" from the in-app copilot), or None for a human caller.

	frappe.flags.kamra_agent_call holds the channel name; a bare True (an
	older caller) still counts as an agent call, logged as "API"."""
	flag = getattr(frappe.flags, "kamra_agent_call", None)
	if not flag:
		return None
	return flag if isinstance(flag, str) else "API"


def _booking_result(doc) -> dict:
	"""create_booking's response for a reservation document."""
	return {
		"reservation": doc.name,
		"room": doc.room,
		"room_number": frappe.db.get_value("Room", doc.room, "room_number") if doc.room else None,
		"guest": doc.guest,
		"amount_after_tax": doc.amount_after_tax,
		"discount": doc.discount_amount,
		"status": doc.status,
	}


def _booking_replay(reservation: str) -> dict:
	"""create_booking's response for a booking that already exists."""
	out = _booking_result(frappe.get_doc("Reservation", reservation))
	out["idempotent_replay"] = 1
	return out


def _collapse(value) -> str:
	"""Trimmed, inner whitespace collapsed - how two spellings of one name
	are compared."""
	return " ".join(str(value or "").split())


def _assert_key_matches(reservation: str, key: str, room_type: str,
                        check_in_date: str, check_out_date: str, *,
                        guest: str | None, guest_name: str | None,
                        phone: str | None) -> None:
	"""An idempotency_key replays only the booking it was first used for.
	The same key on a different stay or guest is a caller bug: refuse it
	rather than hand back someone else's reservation as a success."""
	r = frappe.db.get_value(
		"Reservation", reservation,
		["room_type", "check_in_date", "check_out_date", "guest", "guest_name",
		 "property"],
		as_dict=True)
	same_stay = (r.room_type == room_type
	             and getdate(r.check_in_date) == getdate(check_in_date)
	             and getdate(r.check_out_date) == getdate(check_out_date))
	if guest:
		same_guest = r.guest == guest
	else:
		# the stored name comes from the guest profile; a phone match covers
		# a profile found by phone under a different spelling. Compare the
		# canonical number: storage keeps a leading + as written and stores
		# a local number as E.164.
		from kamra import phone as phone_mod
		stored = frappe.db.get_value("Guest", r.guest, "phone") if r.guest else None
		region = phone_mod.region_for(r.property)
		same_guest = (
			_collapse(r.guest_name).casefold() == _collapse(guest_name).casefold()
			or bool(phone and str(phone).strip() and stored and
			        phone_mod.canonical(stored, region)
			        == phone_mod.canonical(phone, region)))
	if not (same_stay and same_guest):
		frappe.throw(
			_("This idempotency_key ({0}) was already used for a different "
			  "booking ({1}: {2}, {3} to {4}, {5}). Use a new key for a new "
			  "booking.").format(key, reservation, r.room_type,
			                     r.check_in_date, r.check_out_date,
			                     r.guest_name or r.guest),
			title=_("Idempotency key already used"))


def _recent_agent_duplicate(property: str, room_type: str, check_in_date: str,
                            check_out_date: str, *, guest: str | None,
                            guest_name: str | None, phone: str | None,
                            meal_plan: str | None, rate_plan: str | None,
                            adults: int, children: int,
                            waitlist: int = 0) -> str | None:
	"""A reservation identical to this one (same guest, property, room type,
	dates, meal plan, rate plan and party size) created within the duplicate
	window - or None. Only live stays count (a waitlist call matches only
	Waitlist stays): an Inquiry or a cancelled booking is not a retry.

	The guest is matched by profile when given, else by phone, else by the
	name (trimmed, whitespace collapsed; the column collation ignores case):
	without a phone every call creates a new guest profile, so the profile
	alone can't catch a retry."""
	from frappe.utils import add_to_date

	from kamra.reservation_state import LIVE_STATUSES

	since = add_to_date(now_datetime(), minutes=-AGENT_DUPLICATE_WINDOW_MINUTES)
	conds = ["r.property = %(property)s", "r.room_type = %(room_type)s",
	         "r.check_in_date = %(ci)s", "r.check_out_date = %(co)s",
	         "r.creation >= %(since)s",
	         "IFNULL(r.meal_plan, '') = %(meal_plan)s",
	         "IFNULL(r.rate_plan, '') = %(rate_plan)s",
	         "r.adults = %(adults)s", "r.children = %(children)s"]
	values = {"property": property, "room_type": room_type,
	          "ci": getdate(check_in_date), "co": getdate(check_out_date),
	          "since": since, "meal_plan": meal_plan or "",
	          "rate_plan": rate_plan or "", "adults": int(adults or 0),
	          "children": int(children or 0)}
	if waitlist:
		conds.append("r.status = 'Waitlist'")
	else:
		conds.append("r.status IN %(live)s")
		values["live"] = LIVE_STATUSES
	if guest:
		conds.append("r.guest = %(guest)s")
		values["guest"] = guest
	elif phone and str(phone).strip():
		# storage keeps a leading + as written and stores a local number as
		# E.164, so a retry matches every form of that number
		from kamra import phone as phone_mod
		conds.append("g.phone IN %(phones)s")
		values["phones"] = tuple(phone_mod.matching(phone, property)[1])
	elif _collapse(guest_name):
		conds.append(
			"REGEXP_REPLACE(TRIM(r.guest_name), '[[:space:]]+', ' ') = %(gname)s")
		values["gname"] = _collapse(guest_name)
	else:
		return None
	rows = frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- conds holds only fixed SQL fragments written above; every value is bound through `values`
		f"""SELECT r.name FROM `tabReservation` r
		JOIN `tabGuest` g ON g.name = r.guest
		WHERE {' AND '.join(conds)}
		ORDER BY r.creation DESC LIMIT 1""",
		values,
	)
	return rows[0][0] if rows else None


def _stay_slot(check_in_date, check_out_date, t_in=None, t_out=None):
	"""Normalise a booking's planned times to HH:MM:SS (or None). A day-use
	slot must end after it starts - the room is only the guest's for those
	hours. End-of-day check-out '24:00' is valid."""
	def norm(v, *, allow_end_of_day: bool = False):
		if not v:
			return None
		parts = str(v).strip().split(":")
		try:
			h, m = int(parts[0]), int(parts[1] if len(parts) > 1 else 0)
		except ValueError:
			frappe.throw(_("Times are HH:MM, e.g. 10:00."))
		valid = (0 <= h <= 23 and 0 <= m <= 59) or (allow_end_of_day and h == 24 and m == 0)
		if not valid:
			frappe.throw(_("Times are HH:MM, e.g. 10:00."))
		return f"{h:02d}:{m:02d}:00"
	t_in, t_out = norm(t_in), norm(t_out, allow_end_of_day=True)
	if str(check_in_date) == str(check_out_date) and t_in and t_out \
			and t_out <= t_in:
		frappe.throw(_("A day-use slot must end after it starts."))
	return t_in, t_out


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def create_booking(property: str, room_type: str, check_in_date: str,
                   check_out_date: str, guest_name: str,
                   phone: str | None = None, adults: int = 2,
                   children: int = 0, meal_plan: str | None = None,
                   rate_plan: str | None = None,
                   voucher_code: str | None = None,
                   booking_type: str = "Individual",
                   company: str | None = None,
                   group_booking: str | None = None,
                   source: str = "Manual", assign_room: int = 1,
                   travel_agent: str | None = None,
                   booked_by_name: str | None = None,
                   booked_by_phone: str | None = None,
                   booker_relation: str | None = None,
                   contact_preference: str | None = None,
                   guest: str | None = None,
                   waitlist: int = 0,
                   addons=None,
                   guest_category: str | None = None,
                   stay_details=None,
                   instructions=None,
                   room: str | None = None,
                   status: str | None = None,
                   idempotency_key: str | None = None,
                   hold_expires_on: str | None = None,
                   over_capacity_reason: str | None = None,
                   over_capacity_guests: int = 0,
                   over_capacity_charge: float = 0,
                   check_in_time: str | None = None,
                   check_out_time: str | None = None,
                   blacklist_override_reason: str | None = None,
                   pets: int = 0,
                   credit_override_reason: str | None = None,
                   booked_via: str | None = None,
                   ota_ref: str | None = None,
                   ota_total: float = 0,
                   ota_commission_pct: float | None = None,
                   marketing_consent: int = 0,
                   market_segment: str | None = None,
                   night_prices=None,
                   discount_type: str | None = None,
                   discount_value: float = 0,
                   discount_reason: str | None = None,
                   night_discounts=None,
                   fixed_rate: int = 0,
                   supervisor_pin: str | None = None,
                   supervisor_user: str | None = None,
                   discount_note: str | None = None,
                   comp_supervisor_user: str | None = None,
                   comp_supervisor_pin: str | None = None):
	"""One-call booking: attach to an existing guest profile when given,
	else reuse a profile whose phone and full name both match (a
	blacklisted phone always resolves to its profile) / create one.
	Optional auto room assignment,
	voucher applied, price computed by the engine.

	waitlist=1 parks the stay with no room and status Waitlist - for dates
	that are sold out or restricted; promote it later when a room frees.

	status / idempotency_key / hold_expires_on support Instant public booking
	(ADR-006 / ADR-007). Desk callers leave them unset → Confirmed.

	Day use: check_out_date == check_in_date books a same-day stay, priced
	by the quote engine (one billable date). check_in_time / check_out_time
	(HH:MM) are its slot - they place it on the hourly tape and tell
	housekeeping when the room turns; on an overnight stay they are the
	ETA / ETD.

	A stay billed to a company on credit runs the property's credit control
	(kamra.credit) for its amount: a warning comes back as `credit_warning`;
	a block refuses (CREDIT_BLOCKED) unless Finance or a Hotel Admin passes
	`credit_override_reason`, which is logged. Agents can't override.

	night_prices ([{date, price}]) books those nights at the figure the desk
	agreed (tax included when the property's rates include tax) and the rest
	at the rate card; the booking is then priced by hand (auto_price off),
	each typed night is locked in night_rates and logged. Only a Revenue
	Manager or Hotel Admin may; a voucher, rate plan, company plan, OTA
	total or group block rate with typed nights is refused.

	discount_type (Percent / Amount) + discount_value + discount_reason give
	the stay a desk discount off each night's room rate, on any rate plan
	that allows one (discount_eligible) - night_discounts sets Daily Details
	nights of their own. The rules are discount_governance's (the desk's
	limit; supervisor_user + supervisor_pin above it; fixed_rate is a Revenue
	Manager's / Hotel Admin's override of a plan that is not discountable).
	discount_note is the desk's optional free text beside the reason; it goes
	on the discount's audit row. Leaving them out books exactly as before."""
	from kamra.crs import assert_property_access
	from kamra.kamra.doctype.reservation.reservation import lock_property_for_inventory
	from kamra.reservation_state import find_by_idempotency, lock_sius_for_booking
	assert_property_access(property)
	# Before the free-room read and the stay insert, so a second booking
	# waits and then sees this one.
	lock_property_for_inventory(property)
	check_in_time, check_out_time = _stay_slot(
		check_in_date, check_out_date, check_in_time, check_out_time)

	# names ("Deluxe", "Breakfast") or codes resolve to this property's IDs;
	# unknown / ambiguous values are refused with the valid options
	from kamra.booking_refs import (resolve_meal_plan, resolve_rate_plan,
	                                resolve_room_type)
	room_type = resolve_room_type(property, room_type)
	meal_plan = resolve_meal_plan(property, meal_plan)
	rate_plan = resolve_rate_plan(property, rate_plan)
	if not int(waitlist or 0) and not room and room_type not in _sellable_room_types(property, [room_type]):
		if frappe.db.exists("Room", {"property": property}):
			from kamra.kamra.doctype.reservation.reservation import InventoryRefused
			frappe.throw(_("{0} has no rooms configured.").format(room_type), exc=InventoryRefused)
	# refused here, before the guest or any inventory is touched
	typed = _check_night_prices(
		property, room_type, check_in_date, check_out_date, night_prices,
		voucher_code=voucher_code, rate_plan=rate_plan, company=company,
		ota_total=ota_total if booked_via else 0, group_booking=group_booking)
	# the desk's discount: its shape refused here, its rules on the priced nights
	from kamra.pricing import normalize_discounts
	disc = normalize_discounts(discount_type, discount_value, night_discounts,
	                           check_in_date, check_out_date, property=property)
	if disc and booked_via and flt(ota_total) > 0:
		frappe.throw(_("The OTA total already sets the price; it can't take a desk discount."))

	# "MCP" / "Chat" while an agent tool call runs; its bookings are AI Agent
	# bookings whichever agent surface made them
	channel = agent_channel()
	if channel and source == "Manual":
		source = "AI Agent"

	# Booked via an OTA and keyed in from its email: tag the site and its
	# reference, and keep the price the OTA charged (as a channel manager
	# booking does). One reference books once. The OTA's commission is its
	# default % (its Travel Agent record of type OTA) unless the desk says.
	from kamra.channel_manager import (canonical_channel,
	                                   channel_commission_pcts,
	                                   lock_channel_rates, set_ota_commission)
	via = canonical_channel(booked_via)
	ota_ref = (ota_ref or "").strip() or None
	if via:
		source = "OTA"
		if ota_commission_pct in (None, ""):
			ota_commission_pct = channel_commission_pcts().get(via)
	_refuse_taken_ota_ref(property, ota_ref, group_booking)

	key = (idempotency_key or "").strip() or None
	if key:
		existing = find_by_idempotency(property, key)
		if existing:
			_assert_key_matches(existing, key, room_type, check_in_date,
			                    check_out_date, guest=guest,
			                    guest_name=guest_name, phone=phone)
			return _booking_replay(existing)

	from kamra.group_blocks import group_rate
	from kamra.pricing import resolve_booking_rate_plan
	from kamra.settings import get_setting
	has_block_rate = bool(group_booking and float(group_rate(group_booking, room_type) or 0) > 0)
	parsed_stay = (
		frappe.parse_json(stay_details) if isinstance(stay_details, str) else stay_details
	)
	extra_beds_count = cint((parsed_stay or {}).get("extra_beds")) if isinstance(parsed_stay, dict) else 0
	rate_plan = resolve_booking_rate_plan(
		property, room_type, check_in_date, check_out_date,
		rate_plan=rate_plan, company=company, group_booking=group_booking,
		adults=int(adults), children=int(children), extra_beds=extra_beds_count,
		check_availability=not has_block_rate,
	)

	# An agent that retries a tool call (timeout, lost response, a model
	# that repeats itself) must not book the guest twice. Without a key,
	# an identical agent booking made moments ago is returned instead of a
	# second one. Desk bookings (several identical rooms for one party) are
	# never subject to this guard, nor are group pickups: several identical
	# rooms for one party is what a block is, and the block bounds it.
	if not key and not group_booking and (channel or source == "AI Agent"):
		recent = _recent_agent_duplicate(
			property, room_type, check_in_date, check_out_date,
			guest=guest, guest_name=guest_name, phone=phone,
			meal_plan=meal_plan, rate_plan=rate_plan,
			adults=int(adults or 0), children=int(children or 0),
			waitlist=int(waitlist or 0))
		if recent:
			from kamra.savings import log_action
			log_action(
				"agent_duplicate_booking_blocked", "Reservation", recent,
				property,
				rationale=(f"Identical booking for {guest_name} ({room_type} "
				           f"{check_in_date}→{check_out_date}) was already made "
				           f"in the last {AGENT_DUPLICATE_WINDOW_MINUTES} minutes; "
				           f"returned {recent} instead of booking again"),
				channel=channel or "API")
			out = _booking_replay(recent)
			out["duplicate_of_recent"] = 1
			out["note"] = (
				f"An identical booking ({recent}) was made in the last "
				f"{AGENT_DUPLICATE_WINDOW_MINUTES} minutes, so no new "
				"reservation was created. To book another identical room on "
				"purpose, call again with a new idempotency_key.")
			return out

	if guest:
		if not frappe.db.exists("Guest", guest):
			frappe.throw(f"Guest profile {guest} not found.")
	else:
		# a phone already on another profile never replaces the typed name,
		# but a blacklisted profile's number still meets its blacklist
		from kamra import phone as phone_mod
		banned = phone and frappe.db.get_value(
			"Guest", {"phone": phone_mod.matching(phone, property), "blacklisted": 1})
		guest = banned or _guest_for_row(guest_name, phone, property)
	_blacklist_gate(guest, property, blacklist_override_reason)
	if guest_category:
		frappe.db.set_value("Guest", guest, "guest_category", guest_category)
		if guest_category == "VIP":
			frappe.db.set_value("Guest", guest, "vip", 1)

	voucher = None
	if voucher_code:
		from frappe.utils import date_diff

		from kamra.pricing import validate_voucher
		voucher = validate_voucher(
			property, voucher_code,
			date_diff(check_out_date, check_in_date),
		).name

	# a property can turn room assignment at booking off (Settings > Front Desk);
	# a room the caller named (a walk-in) is still honoured
	if not get_setting(property, "assign_room_on_booking"):
		assign_room = 0

	if not room and int(assign_room) and not int(waitlist or 0):
		# Serialize hybrid / SIU capacity before reading free rooms (ADR-007).
		lock_sius_for_booking(property, room_type)
		# a group pickup books against its own block, not general inventory
		free = available_rooms(property, room_type, check_in_date,
		                       check_out_date, group_booking=group_booking)
		if free and getdate(check_in_date) <= getdate(nowdate()):
			# arriving today: hand out a room nobody is still checked in to
			# before one whose guest departs today but has not left yet
			occupied = in_house_rooms(property)
			free = sorted(free, key=lambda r: r["name"] in occupied)
		# SIU path returns plain dicts; legacy SQL returns frappe._dict.
		room = free[0]["name"] if free else None
	elif not int(waitlist or 0):
		lock_sius_for_booking(property, room_type)

	doc = frappe.get_doc({
		"doctype": "Reservation",
		"property": property,
		"guest": guest,
		"room_type": room_type,
		"room": room,
		"check_in_date": check_in_date,
		"check_out_date": check_out_date,
		"adults": int(adults),
		"children": int(children),
		"pets": max(0, int(pets or 0)),
		"meal_plan": meal_plan or None,
		"rate_plan": rate_plan or None,
		"voucher": voucher,
		"booking_type": booking_type,
		# corporate business defaults to the Corporate segment
		"market_segment": market_segment or ("Corporate" if company else None),
		"company": company or None,
		"group_booking": group_booking or None,
		"source": source,
		"is_day_use": 1 if check_in_date == check_out_date else 0,
		"planned_check_in_time": check_in_time,
		"planned_check_out_time": check_out_time,
		"travel_agent": travel_agent or None,
		"booked_by_name": booked_by_name or None,
		# read with the property's country, like the guest's (#468); a CRS
		# leg copying an older itinerary's booker phone is read here too
		"booked_by_phone": _phone_for(booked_by_phone, property) or None,
		"booker_relation": booker_relation or None,
		"contact_preference": contact_preference
			or ("Booker" if booked_by_name else "Guest"),
		"auto_price": 1,
		"idempotency_key": key,
		"hold_expires_on": hold_expires_on or None,
	})
	if int(waitlist or 0):
		doc.status = "Waitlist"
	elif status:
		doc.status = status
	# an OTA total keyed in from the channel's email: the stay is billed at the
	# channel's own nights (lock_channel_rates), on every attempt
	ota_locked = bool(via and float(ota_total or 0) > 0)
	if via:
		doc.channel, doc.ota_ref = via, ota_ref
		if ota_locked:
			doc.auto_price = 0
			doc.amount_after_tax = float(ota_total)
			lock_channel_rates(doc, float(ota_total))
	elif doc.get("group_booking"):
		# a room picked up from a block bills the group's negotiated rate,
		# priced by the engine like any agreed rate; the pickup is its
		# governed writer (Reservation.guard_agreed_rate)
		from kamra.group_blocks import group_rate
		doc.agreed_rate = group_rate(doc.group_booking, doc.room_type)
		doc.flags.kamra_agreed_rate_override = True

	# Booking a party above the room's capacity is a decision someone makes
	# by arrangement; the reason is what records who agreed it, and the
	# reservation refuses without one.
	if (over_capacity_reason or "").strip():
		doc.over_capacity_reason = over_capacity_reason.strip()
	if over_capacity_guests:
		doc.over_capacity_guests = int(over_capacity_guests)
	if over_capacity_charge:
		doc.over_capacity_charge = float(over_capacity_charge)

	# The desk's nightly prices: Reservation.keep_hand_set_nights prices the
	# booking with them in this first save (typed nights at the typed price,
	# the rest at the rate card) and locks the nights.
	if typed:
		doc.auto_price = 0
		doc.set("night_rates", [{"stay_date": d, "hand_price": float(p)}
		                        for d, p in sorted(typed.items())])
		# checked above (_check_night_prices): the only insert that may
		# carry a typed price (Reservation.guard_locked_nights)
		doc.flags.kamra_night_prices_writer = True
	# the discount the insert prices and governs (Reservation.validate)
	discount_request = dict(supervisor_user=supervisor_user, supervisor_pin=supervisor_pin)
	if disc:
		_set_discount_inputs(doc, disc, discount_reason, fixed_rate, **discount_request)
	# a Complimentary or House Use plan is signed off on the insert too
	# (Reservation.govern_rate_category): by the discount's approver when
	# their roles cover it, else by comp_supervisor_user with their PIN
	_discount_sign_off(doc, supervisor_user, supervisor_pin)
	_comp_sign_off(doc, comp_supervisor_user, comp_supervisor_pin)

	# extras chosen at booking - priced from the Experience, posted to the
	# folio the moment it opens
	if isinstance(addons, str):
		addons = frappe.parse_json(addons)
	for a in addons or []:
		exp = frappe.db.get_value(
			"Experience", a.get("experience"),
			["experience_name", "price", "gst_rate"], as_dict=True)
		if not exp:
			continue
		qty = float(a.get("qty") or 1)
		doc.append("addons", {
			"experience": a["experience"],
			"description": exp.experience_name,
			"qty": qty,
			"rate": float(exp.price or 0),
			"amount": qty * float(exp.price or 0),
			"gst_rate": float(exp.gst_rate or 0),
		})

	# Arrival, transport, purpose and service flags - the details a real
	# front desk collects. Whitelisted so a caller can't set arbitrary fields.
	_STAY_KEYS = {
		"arrival_mode", "arrival_ref", "arrival_datetime", "pickup_required",
		"pickup_note", "departure_mode", "departure_ref", "departure_datetime",
		"drop_required", "drop_note", "purpose", "extra_beds", "infants",
		"valet_parking", "early_checkin", "late_checkout",
	}
	if isinstance(stay_details, str):
		stay_details = frappe.parse_json(stay_details)
	for k, v in (stay_details or {}).items():
		if k in _STAY_KEYS and v not in (None, ""):
			doc.set(k, v)

	# Special instructions, each routed to the team that acts on it.
	if isinstance(instructions, str):
		instructions = frappe.parse_json(instructions)
	for ins in instructions or []:
		if ins.get("instruction"):
			doc.append("instructions", {
				"department": ins.get("department") or "Front Desk",
				"instruction": ins["instruction"],
			})
	# One-tap service flags become instructions for the right department, so
	# a pickup or a valet request reaches the people who fulfil it.
	for flag, dept, text in (
		("pickup_required", "Transport", "Airport / station pickup"),
		("drop_required", "Transport", "Airport / station drop"),
		("valet_parking", "Concierge", "Valet parking"),
		("early_checkin", "Front Desk", "Early check-in requested"),
		("late_checkout", "Front Desk", "Late check-out requested"),
	):
		if doc.get(flag):
			note = text
			if flag == "pickup_required" and doc.get("arrival_ref"):
				note = f"{text} · {doc.arrival_ref}"
			doc.append("instructions", {
				"department": dept, "instruction": note, "status": "Open",
			})

	# A company on credit: check its credit for this stay. The booking is
	# priced on insert, so it is checked straight after, inside a savepoint
	# that a refusal rolls back (a group loop that catches the error must
	# not keep the room).
	from kamra.credit import credit_applies, credit_policy
	gate_credit = (credit_applies(company) and doc.status != "Waitlist"
	               and credit_policy(property) != "off")
	# Concurrent bookings race on tabSeries (RES-YYYY-#####); retry instead
	# of returning HTTP 500 QueryDeadlockError (#344).
	credit = None
	for attempt in (1, 2, 3):
		try:
			if gate_credit:
				frappe.db.savepoint("kamra_credit_gate")
			doc.insert(ignore_permissions=False)
			if gate_credit:
				from kamra.credit import enforce_company_credit
				try:
					credit = enforce_company_credit(
						company, property, float(doc.amount_after_tax or 0),
						override_reason=credit_override_reason,
						context="booking", reference_doctype="Reservation",
						reference_name=doc.name)
				except Exception:
					frappe.db.rollback(save_point="kamra_credit_gate")
					raise
			break
		except frappe.DuplicateEntryError:
			existing = None
			if getattr(doc, "ota_live_key", None):
				existing = frappe.db.get_value(
					"Reservation", {"ota_live_key": doc.ota_live_key}, "name")
			if existing:
				frappe.throw(_("OTA booking {0} is already reservation {1}.").format(
					ota_ref or doc.ota_ref, existing))
			raise
		except frappe.QueryDeadlockError:
			frappe.db.rollback()
			if attempt == 3:
				raise
			# Guest may have been created in the rolled-back transaction
			if not frappe.db.exists("Guest", guest):
				guest = _guest_for_row(guest_name, phone, property)
			payload = doc.as_dict()
			payload.pop("name", None)
			payload["guest"] = guest
			if not typed:
				# the failed attempt's validate locked its nights: the retry
				# is inserted unpriced, as the first try was (a Before
				# Validate writer refuses a new booking with night rows:
				# HSP-TARIFF-007), and prices them again. The nights the
				# caller wrote before the insert are written again below.
				payload.pop("night_rates", None)
			doc = frappe.get_doc(payload)
			if typed:
				doc.flags.kamra_night_prices_writer = True
			if ota_locked:
				# the channel's own nights, as the first attempt locked them
				lock_channel_rates(doc, float(ota_total))
			if flt(doc.get("agreed_rate")) and doc.get("group_booking"):
				doc.flags.kamra_agreed_rate_override = True
			if disc:
				_set_discount_inputs(doc, disc, discount_reason, fixed_rate, **discount_request)
			_discount_sign_off(doc, supervisor_user, supervisor_pin)
			_comp_sign_off(doc, comp_supervisor_user, comp_supervisor_pin)
	if via:
		set_ota_commission(doc, ota_commission_pct)
	if typed:
		_record_night_prices(doc, typed)
	if disc:
		_log_discount(doc, doc.flags.get("kamra_discount_governance"),
		              channel=channel or ("API" if source == "AI Agent" else "Desk"),
		              note_text=discount_note)

	from kamra.savings import log_action
	log_action(
		action_type="create_reservation",
		reference_doctype="Reservation",
		reference_name=doc.name,
		property=property,
		minutes_saved=8 if source == "AI Agent" else 0,
		rationale=f"Booked {room_type} {check_in_date}→{check_out_date} for {guest_name}",
		channel=channel or ("API" if source == "AI Agent" else "Desk"),
	)
	if cint(marketing_consent):
		from kamra.privacy import set_marketing_consent
		set_marketing_consent(doc.guest, 1, "Desk", property)
	out = _booking_result(doc)
	if credit and not credit["ok"]:
		out["credit_warning"] = {
			"company": company, "reasons": credit["reasons"],
			"overridden": bool(credit.get("overridden"))}
	return out


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent", "Revenue Manager")
def waitlist(property: str):
	"""All waitlisted stays for the property, by arrival date."""
	return frappe.get_all(
		"Reservation",
		filters={"property": property, "status": "Waitlist"},
		fields=["name", "guest_name", "room_type", "check_in_date",
		        "check_out_date", "nights", "adults", "children",
		        "amount_after_tax"],
		order_by="check_in_date asc")


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def promote_waitlist(reservation: str):
	"""Promote a waitlisted stay to Confirmed when a room is free for its
	dates. Assigns the first free room; the overlap guard validates it."""
	doc = frappe.get_doc("Reservation", reservation)
	if doc.status != "Waitlist":
		frappe.throw("Only waitlisted reservations can be promoted.")
	free = available_rooms(doc.property, doc.room_type,
	                       doc.check_in_date, doc.check_out_date)
	if not free:
		frappe.throw("Still no room free for those dates.")
	doc.room = free[0]["name"]
	doc.status = "Confirmed"
	doc.save()
	from kamra.savings import log_action
	log_action("waitlist_promote", "Reservation", doc.name, doc.property,
	           rationale=f"Promoted from waitlist into {doc.room}")
	return {"ok": True, "reservation": doc.name, "room": doc.room}


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def waitlist_ready(property: str):
	"""Waitlisted stays that CAN now be accommodated - a room is free for
	their dates. This is the signal the voice/WhatsApp agent watches so it
	can proactively reach the guest the moment a room opens."""
	ready = []
	for r in frappe.get_all(
			"Reservation",
			filters={"property": property, "status": "Waitlist"},
			fields=["name", "guest", "guest_name", "room_type",
			        "check_in_date", "check_out_date", "nights",
			        "adults", "children", "amount_after_tax"]):
		free = available_rooms(property, r.room_type,
		                       r.check_in_date, r.check_out_date)
		if free:
			r["rooms_free"] = len(free)
			r["phone"] = frappe.db.get_value(
				"Guest", r.guest, "phone") if r.guest else None
			ready.append(r)
	return ready


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def create_group_booking(property: str, group_name: str, check_in_date: str,
                         check_out_date: str, rooms: str | list,
                         guest_name: str, phone: str | None = None,
                         company: str | None = None,
                         meal_plan: str | None = None,
                         rate_plan: str | None = None,
                         booked_via: str | None = None,
                         ota_ref: str | None = None,
                         ota_total: float = 0,
                         ota_commission_pct: float | None = None,
                         group_type: str | None = None,
                         booking_type: str | None = None,
                         master_index: int | None = None,
                         master_pays: str | None = None,
                         comp_supervisor_user: str | None = None,
                         comp_supervisor_pin: str | None = None):
	"""Create a Group Booking plus one reservation per requested room.
	`rooms` = [{"room_type": <name>, "count": 2, "room": <optional>,
	"guest_name": <optional>, "night_prices": <optional>}, ...] (JSON string
	ok). A row's `night_prices` ([{date, price}], as in create_booking) price
	that row's rooms night by night; every row keeps its own.

	`master_index` picks the main room (the index of the room as expanded
	by count, default the first): its guest pays what `master_pays` covers
	(a key of folio.COMPANY_BILLING_DEFAULTS, default "Room only") for every
	room on one group bill. If that room could not be booked, the next one
	made is the main room. Pass neither to keep a plain group.

	Booked via an OTA (see create_booking): every room carries the site and
	the one OTA reference, and the sold total (`ota_total`, tax included) is
	split across the rooms in proportion to their rate-card prices, each
	room locked to its share.

	A Complimentary or House Use plan is signed off once for every room:
	comp_supervisor_user + comp_supervisor_pin (create_booking)."""
	if isinstance(rooms, str):
		rooms = json.loads(rooms)
	lines = [spec for spec in rooms for _n in range(int(spec.get("count", 1)))]
	from kamra.folio import COMPANY_BILLING_DEFAULTS
	with_master = master_index is not None or bool(master_pays)
	if master_pays and master_pays not in COMPANY_BILLING_DEFAULTS:
		frappe.throw(_("master_pays must be one of: {0}").format(
			", ".join(COMPANY_BILLING_DEFAULTS)))

	ota_ref = (ota_ref or "").strip() or None
	_refuse_taken_ota_ref(property, ota_ref)  # before the group is made
	from kamra.booking_refs import resolve_rate_plan
	from kamra.pricing import resolve_group_rate_plan
	rate_plan = resolve_rate_plan(property, rate_plan)
	if not any(spec.get("night_prices") for spec in lines):
		rate_plan = resolve_group_rate_plan(property, rate_plan=rate_plan, company=company)
	shares = [0.0] * len(lines)
	total = float(ota_total or 0) if booked_via else 0
	if total > 0 and lines:
		weights = [float(get_quote(
			property, spec["room_type"], check_in_date, check_out_date,
			adults=int(spec.get("adults", 2)),
			children=int(spec.get("children", 0)),
			meal_plan=spec.get("meal_plan") or meal_plan, rate_plan=rate_plan,
			company=company).get("amount_after_tax") or 0) for spec in lines]
		if not sum(weights):
			weights = [1.0] * len(lines)
		from kamra.ledger import _currency_places, _round_currency
		places = max(_currency_places(property), 2)
		for i, w in enumerate(weights):
			shares[i] = _round_currency(total * w / sum(weights), places)
		shares[-1] = _round_currency(total - sum(shares[:-1]), places)  # rounding

	# Nights a room was priced by hand are checked for every room before the
	# group is made: a refusal must stop the booking, not show up later as a
	# room skipped from the group.
	for spec in lines:
		if spec.get("night_prices"):
			from kamra.booking_refs import resolve_room_type
			from kamra.pricing import quote as price_quote
			rt = resolve_room_type(property, spec["room_type"])
			typed = _check_night_prices(
				property, rt, check_in_date, check_out_date,
				spec["night_prices"], rate_plan=rate_plan, company=company,
				ota_total=total)
			price_quote(property, rt, check_in_date, check_out_date,
			            int(spec.get("adults", 2)), int(spec.get("children", 0)),
			            company=company or None, night_prices=[
			                {"date": d, "price": str(p)} for d, p in typed.items()])

	group = frappe.get_doc({
		"doctype": "Group Booking",
		"property": property,
		"group_name": group_name,
		"company": company or None,
		"rate_plan": rate_plan or None,
		"group_type": group_type or None,
		"check_in_date": check_in_date,
		"check_out_date": check_out_date,
		"status": "Confirmed",
	}).insert(ignore_permissions=False)

	created, skipped = [], []
	main = None
	for i, (spec, share) in enumerate(zip(lines, shares, strict=True)):
		try:
			res = create_booking(
				property=property,
				room_type=spec["room_type"],
				room=spec.get("room"),
				check_in_date=check_in_date,
				check_out_date=check_out_date,
				guest_name=spec.get("guest_name") or guest_name,
				phone=spec.get("phone") or phone,
				adults=int(spec.get("adults", 2)),
				children=int(spec.get("children", 0)),
				meal_plan=spec.get("meal_plan") or meal_plan,
				rate_plan=rate_plan,
				booking_type=booking_type or ("Corporate" if company else "Group"),
				company=company,
				group_booking=group.name,
				booked_via=booked_via,
				ota_ref=ota_ref,
				ota_total=share,
				ota_commission_pct=ota_commission_pct,
				night_prices=spec.get("night_prices"),
				comp_supervisor_user=comp_supervisor_user,
				comp_supervisor_pin=comp_supervisor_pin,
			)
			created.append(res["reservation"])
			if main is None and i >= int(master_index or 0):
				main = res["reservation"]
		except Exception as e:
			skipped.append({"room_type": spec["room_type"], "reason": str(e)})

	if not created:
		# no room could be booked: leave no empty group behind
		frappe.delete_doc("Group Booking", group.name, ignore_permissions=True, force=True)
		frappe.clear_messages()
		frappe.throw(_("No room could be booked, so the group was not created. {0}").format(
			"; ".join(dict.fromkeys(s["reason"] for s in skipped))))
	if with_master and created:
		group.db_set({"master_reservation": main or created[0],
		              "master_pays": master_pays or "Room only"})
	return {"group_booking": group.name, "created": created, "skipped": skipped,
	        "master_reservation": group.get("master_reservation")}


@frappe.whitelist(methods=["GET"])
@require_roles("Front Desk", "Revenue Manager", "Hotel Admin")
def rooming_list_template(property: str):
	"""An Excel rooming list for the group booking screen to paste or
	upload: Name, Phone, Room type (a dropdown of this property's room
	types) and Other guests, with a sheet explaining how to fill it."""
	from io import BytesIO

	from openpyxl import Workbook
	from openpyxl.styles import Font, PatternFill
	from openpyxl.worksheet.datavalidation import DataValidation

	from kamra.crs import assert_property_access
	assert_property_access(property)
	types = frappe.get_all("Room Type", filters={"property": property},
	                       pluck="room_type_name", order_by="room_type_name asc")
	wb = Workbook()
	ws = wb.active
	ws.title = "Rooming List"
	headers = ["Name", "Phone", "Room type", "Other guests"]
	for c, h in enumerate(headers, 1):
		cell = ws.cell(row=1, column=c, value=h)
		cell.font = Font(name="Arial", bold=True, color="FFFFFF")
		cell.fill = PatternFill("solid", fgColor="0F766E")
	for r in range(2, 202):
		ws.cell(row=r, column=2).number_format = "@"  # keep +91... as typed
	for col, width in zip("ABCD", (30, 18, 18, 40), strict=True):
		ws.column_dimensions[col].width = width
	ws.freeze_panes = "A2"
	if types:
		# a range, not an inline list: no length limit, and commas are safe
		lists = wb.create_sheet("Lists")
		for i, room_type in enumerate(types, 1):
			lists.cell(row=i, column=1, value=room_type)
		lists.sheet_state = "hidden"
		dv = DataValidation(type="list", formula1=f"Lists!$A$1:$A${len(types)}",
		                    allow_blank=True)
		dv.add("C2:C201")
		ws.add_data_validation(dv)
	help_ws = wb.create_sheet("How to use")
	for i, line in enumerate([
		"One guest per row: the guest the room is booked for.",
		"Name is required. Phone, Room type and Other guests are optional.",
		"Room type: pick from the list. Empty = any room of the group.",
		"Other guests: everyone else staying in that room, separated by ; (e.g. Ravi Rao; Meera Rao).",
		"Do not add the main guest - they are already room 1.",
		f"In {product_name()}: copy the rows (with the header) and use Paste from Excel,",
		"or File > Save As > CSV and use Upload CSV.",
	], 1):
		help_ws.cell(row=i, column=1, value=line).font = Font(name="Arial")
	help_ws.column_dimensions["A"].width = 95
	buf = BytesIO()
	wb.save(buf)
	frappe.response["filename"] = "rooming-list-template.xlsx"
	frappe.response["filecontent"] = buf.getvalue()
	frappe.response["type"] = "binary"


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def create_group_with_rooming_list(property: str, group_name: str,
                                   check_in_date: str, check_out_date: str,
                                   blocks, guests=None,
                                   phone: str | None = None,
                                   company: str | None = None,
                                   group_type: str | None = None,
                                   cutoff_date: str | None = None,
                                   master_index: int | None = None,
                                   master_pays: str | None = None,
                                   attrition_pct: float = 0,
                                   deposit_due: float = 0,
                                   quotation_only: int = 0,
                                   rate_plan: str | None = None,
                                   comp_supervisor_user: str | None = None,
                                   comp_supervisor_pin: str | None = None):
	"""The desk's group flow: group -> room blocks -> rooming list ->
	reservations. A Confirmed group holds `blocks` ([{room_type,
	rooms_blocked, block_rate, nightly_counts}]) off sale - nightly_counts
	({"YYYY-MM-DD": rooms}) when the number differs night to night, and
	block_rate the negotiated rate its rooms are billed. Each `guests` row
	({room_type, guest_name, phone, others, check_in_date, check_out_date})
	is booked now against that hold, on the group's dates or its own nights
	inside them. Blocked rooms with no guest stay held until the cutoff.

	`master_index` is the guests row whose room pays `master_pays` for the
	group (omit both, or pass `company`, for a company-paid group).
	`quotation_only` saves an Open group (a proposal): nothing is held or
	booked until the block is confirmed. Everything is checked before the
	group is made, so a refusal leaves nothing behind."""
	import json as _json

	from kamra.booking_refs import resolve_room_type
	from kamra.folio import COMPANY_BILLING_DEFAULTS
	from kamra.group_blocks import group_nights, parse_nightly

	blocks = frappe.parse_json(blocks) if isinstance(blocks, str) else (blocks or [])
	guests = frappe.parse_json(guests) if isinstance(guests, str) else (guests or [])
	quote_only = bool(cint(quotation_only))
	if quote_only:
		guests = []
	if getdate(check_in_date) < getdate(nowdate()):
		frappe.throw(_("Check-in {0} has already passed. Pick today or a later date.")
		             .format(check_in_date))
	if getdate(check_out_date) <= getdate(check_in_date):
		frappe.throw(_("Departure must be after arrival."))
	if master_pays and master_pays not in COMPANY_BILLING_DEFAULTS:
		frappe.throw(_("master_pays must be one of: {0}").format(
			", ".join(COMPANY_BILLING_DEFAULTS)))
	if not 0 <= flt(attrition_pct) <= 100:
		frappe.throw(_("Allowed shortfall must be between 0 and 100%."))

	nights = group_nights(check_in_date, check_out_date)
	counts: dict[str, dict[str, int]] = {}  # room type -> night -> rooms
	rates: dict[str, float] = {}
	for b in blocks:
		rt = resolve_room_type(property, b.get("room_type"))
		flat = cint(b.get("rooms_blocked"))
		nightly = parse_nightly(b.get("nightly_counts") or b.get("nightly"))
		row = counts.setdefault(rt, {d: 0 for d in nights})
		for d in nights:
			row[d] += nightly.get(d, flat) if nightly else flat
		if flt(b.get("block_rate")) > 0:
			rates[rt] = flt(b.get("block_rate"))
	counts = {rt: c for rt, c in counts.items() if any(c.values())}
	if not counts:
		frappe.throw(_("Block at least one room."))
	if rates and not _can_set_rate():
		frappe.throw(_("Only a Revenue Manager or Hotel Admin can set a group rate."),
		             frappe.PermissionError)

	short = []
	for rt, by_night in (counts.items() if not quote_only else []):
		if len(set(by_night.values())) == 1:
			n = by_night[nights[0]]
			free = len(available_rooms(property, rt, check_in_date, check_out_date))
			if free < n:
				short.append(_("{0}: {1} blocked, {2} free").format(rt, n, free))
			continue
		for d, n in by_night.items():
			free = len(available_rooms(property, rt, d, str(add_days(d, 1)))) if n else 0
			if free < n:
				short.append(_("{0} on {1}: {2} blocked, {3} free").format(rt, d, n, free))
	if short:
		frappe.throw(_("Not enough rooms for these dates. {0}").format("; ".join(short)))

	used: dict[str, dict[str, int]] = {rt: {d: 0 for d in nights} for rt in counts}
	for gst in guests:
		gst["room_type"] = resolve_room_type(property, gst.get("room_type"))
		if not (gst.get("guest_name") or "").strip():
			frappe.throw(_("Every rooming list row needs a guest name."))
		gci = str(getdate(gst.get("check_in_date") or check_in_date))
		gco = str(getdate(gst.get("check_out_date") or check_out_date))
		if not (getdate(check_in_date) <= getdate(gci) < getdate(gco) <= getdate(check_out_date)):
			frappe.throw(_("{0}: the stay must be inside the group's dates.").format(gst["guest_name"]))
		gst["check_in_date"], gst["check_out_date"] = gci, gco
		if gst["room_type"] not in counts:
			frappe.throw(_("No rooms blocked for {0}.").format(gst["room_type"]))
		for d in group_nights(gci, gco):
			used[gst["room_type"]][d] += 1
			if used[gst["room_type"]][d] > counts[gst["room_type"]][d]:
				frappe.throw(_("More guests than blocked rooms for {0} on {1}.")
				             .format(gst["room_type"], d))
	with_master = not company and (master_index is not None or bool(master_pays))
	if with_master and not guests and not quote_only:
		frappe.throw(_("Name the main room's guest in the rooming list."))

	from kamra.booking_refs import resolve_rate_plan
	from kamra.pricing import resolve_group_rate_plan
	rate_plan = resolve_rate_plan(property, rate_plan)
	rate_plan = resolve_group_rate_plan(property, rate_plan=rate_plan, company=company)

	comp_approver = None
	if not quote_only and guests:
		comp_plan = rate_plan
		if not comp_plan and company:
			from kamra.pricing import negotiated_rate_plan
			comp_plan = negotiated_rate_plan(property, company)
		if comp_plan:
			comp_approver = comp_rate_governance(
				property, comp_plan, check=True,
				supervisor_user=comp_supervisor_user,
				supervisor_pin=comp_supervisor_pin,
			).get("approver")

	blocked = {rt: max(c.values()) for rt, c in counts.items()}
	group = frappe.get_doc({
		"doctype": "Group Booking",
		"property": property,
		"group_name": group_name,
		"company": company or None,
		"rate_plan": rate_plan or None,
		"group_type": group_type or None,
		"check_in_date": check_in_date,
		"check_out_date": check_out_date,
		"cutoff_date": cutoff_date or None,
		"attrition_pct": flt(attrition_pct),
		"deposit_due": flt(deposit_due),
		"status": "Open" if quote_only else "Confirmed",
		"blocks": [{
			"room_type": rt, "rooms_blocked": blocked[rt],
			"block_rate": rates.get(rt, 0),
			"nightly_counts": _json.dumps(c) if len(set(c.values())) > 1 else None,
		} for rt, c in counts.items()],
	}).insert(ignore_permissions=False)

	created, skipped, warnings = [], [], []
	main = None
	for i, gst in enumerate(guests):
		others = split_names(gst.get("others"))
		capacity = cint(frappe.get_cached_value("Room Type", gst["room_type"], "adults_capacity")) or 2
		name = gst["guest_name"].strip()
		row_phone = gst.get("phone") or (phone if i == cint(master_index) else None)
		sp = f"kamra_group_guest_{i}"
		frappe.db.savepoint(sp)
		try:
			res = create_booking(
				property=property, room_type=gst["room_type"],
				check_in_date=gst["check_in_date"], check_out_date=gst["check_out_date"],
				guest_name=name, phone=row_phone,
				guest=_guest_for_row(name, row_phone, property),
				# the guest and the others named with them, as far as the room sleeps
				adults=cint(gst.get("adults")) or min(1 + len(others), capacity),
				children=cint(gst.get("children")),
				rate_plan=gst.get("rate_plan") or rate_plan or None,
				booking_type="Corporate" if company else "Group",
				company=company, group_booking=group.name, source="Manual",
				comp_supervisor_user=comp_supervisor_user,
				comp_supervisor_pin=comp_supervisor_pin)
		except Exception as e:
			frappe.db.rollback(save_point=sp)
			frappe.clear_messages()
			skipped.append({"room_type": gst["room_type"], "guest_name": name, "reason": str(e)})
			continue
		created.append(res["reservation"])
		if main is None and i >= cint(master_index):
			main = res["reservation"]
		if others:
			# the room is booked: a failure here is a note, not a skipped room
			sp_names = f"kamra_group_names_{i}"
			frappe.db.savepoint(sp_names)
			try:
				add_occupant_names(res["reservation"], others)
			except Exception as e:
				frappe.db.rollback(save_point=sp_names)
				frappe.clear_messages()
				warnings.append({"reservation": res["reservation"], "guest_name": name,
				                 "reason": str(e)})

	if with_master and created:
		group.db_set({"master_reservation": main or created[0],
		              "master_pays": master_pays or "Room only"})
	elif with_master and quote_only:
		group.db_set("master_pays", master_pays or "Room only")
	from kamra.savings import log_action
	log_action("group_created", "Group Booking", group.name, property,
	           rationale=(f"{group_name}: quotation for {sum(blocked.values())} rooms"
	                      if quote_only else
	                      f"{group_name}: {sum(blocked.values())} rooms blocked, "
	                      f"{len(created)} named on the rooming list"),
	           channel=agent_channel() or "Desk",
	           **({"approver": comp_approver} if comp_approver else {}))
	return {"group_booking": group.name, "status": group.status,
	        "created": created, "skipped": skipped, "warnings": warnings,
	        "blocked": sum(blocked.values()),
	        "held": 0 if quote_only else max(0, sum(blocked.values()) - len(created)),
	        "cutoff_date": str(group.cutoff_date or ""),
	        "master_reservation": group.get("master_reservation")}


def _block_hold(property: str, room_type: str, check_in_date: str,
                check_out_date: str, for_group: str | None = None) -> int:
	"""Rooms held by confirmed group blocks overlapping the window that
	haven't been picked up yet (kamra.group_blocks). A block stops holding
	once its cutoff passes; the scheduler then writes the release record.
	The group's own pickups see their held rooms, not a shortage."""
	from kamra.group_blocks import block_hold
	return block_hold(property, room_type, check_in_date, check_out_date,
	                  for_group=for_group)


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent", "Revenue Manager", "Hotel Admin")
def inventory_availability(property: str, check_in_date: str, check_out_date: str,
                           room_type: str | None = None, siu: str | None = None,
                           include_reasons: int = 0):
	"""Sellable Unit availability (ADR-003). Prefer this over ad-hoc room SQL."""
	from kamra.siu.availability import availability as avail
	return avail(
		property,
		siu=siu or None,
		room_type=room_type or None,
		check_in_date=check_in_date,
		check_out_date=check_out_date,
		include_reasons=bool(int(include_reasons or 0)),
	)


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def available_rooms(property: str, room_type: str, check_in_date: str,
                    check_out_date: str, group_booking: str | None = None,
                    ready_now: int = 0):
	"""Rooms of a type with no overlapping live reservation - the same
	logic the double-booking guard enforces, exposed as a query. Confirmed
	group blocks hold their unsold rooms out of general sale; pass the
	group to book against its own block.

	A stay may start on another's departure date, so a room whose guest
	departs today is "free" for a booking. Pass ready_now=1 when the guest
	is being put in the room now (check-in, walk-in, allocation for today):
	a room whose occupant is still Checked In is then left out until they
	check out - the same rule Reservation.validate_room_vacant enforces."""
	# Prefer Sellable Unit availability when SIUs exist for this type
	# (ADR-003). Fall back to legacy Room SQL during migration.
	rooms = None
	from kamra.siu.availability import has_active_sius
	if has_active_sius(property, room_type):
		from kamra.siu.availability import available_rooms_via_siu
		rooms = available_rooms_via_siu(
			property, room_type, check_in_date, check_out_date
		)
	if rooms is None:
		rooms = _available_rooms_raw(property, room_type, check_in_date,
		                             check_out_date)
	if int(ready_now or 0) and getdate(check_in_date) <= getdate(nowdate()):
		occupied = in_house_rooms(property)
		rooms = [r for r in (rooms or []) if r["name"] not in occupied]
	hold = _block_hold(property, room_type, check_in_date, check_out_date,
	                   for_group=group_booking)
	if hold:
		rooms = rooms[:max(0, len(rooms) - hold)]
	# Always frappe._dict so callers can use row["name"] or row.name.
	return [frappe._dict(r) for r in (rooms or [])]


def in_house_rooms(property: str) -> set[str]:
	"""Rooms a guest is checked in to right now, whatever their departure
	date says - they stay occupied until the guest checks out."""
	return set(frappe.get_all(
		"Reservation",
		filters={"property": property, "status": "Checked In",
		         "room": ("is", "set"),
		         "check_in_date": ("<=", nowdate())},
		pluck="room"))


def _available_rooms_raw(property: str, room_type: str, check_in_date: str,
                         check_out_date: str):
	# villa <-> room lockout: a booked whole villa takes its member rooms off
	# sale and any booked member room takes the villa off sale - the same
	# query validate_villa_lockout refuses a booking on
	from kamra.kamra.doctype.reservation.reservation import villa_lockout_conflict
	if villa_lockout_conflict(property, room_type, check_in_date, check_out_date):
		return []
	return frappe.db.sql(
		"""
		SELECT r.name, r.room_number, r.housekeeping_status
		FROM `tabRoom` r
		WHERE r.property = %(property)s
		  AND r.room_type = %(room_type)s
		  AND r.housekeeping_status != 'Out of Order'
		  AND NOT EXISTS (
			SELECT 1 FROM `tabReservation` b
			WHERE b.room = r.name
			  AND b.status IN ('Confirmed', 'Checked In')
			  AND b.check_in_date < GREATEST(%(check_out)s,
			                                 DATE_ADD(%(check_in)s, INTERVAL 1 DAY))
			  AND GREATEST(b.check_out_date,
			               DATE_ADD(b.check_in_date, INTERVAL 1 DAY)) > %(check_in)s
		  )
		  AND NOT EXISTS (
			SELECT 1 FROM `tabRoom Block` k
			WHERE k.room = r.name
			  AND k.block_status = 'Active'
			  AND k.from_date < %(check_out)s
			  AND k.to_date > %(check_in)s
		  )
		ORDER BY r.room_number
		""",
		{
			"property": property,
			"room_type": room_type,
			"check_in": check_in_date,
			"check_out": check_out_date,
		},
		as_dict=True,
	)


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def room_blocks(property: str, active_only: int = 1):
	"""Rooms held out of sale (house use, VIP, maintenance)."""
	filters = {"property": property}
	if int(active_only or 0):
		filters["block_status"] = "Active"
	rows = frappe.get_all(
		"Room Block", filters=filters,
		fields=["name", "room", "reason", "from_date", "to_date",
		        "block_status", "note", "released_by", "released_on"],
		order_by="from_date asc",
	)
	nums = {r.name: r.room_number for r in frappe.get_all(
		"Room", filters={"property": property},
		fields=["name", "room_number"])}
	for r in rows:
		r["room_number"] = nums.get(r.room, r.room)
	return rows


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def create_room_block(property: str, room: str, from_date: str,
                      to_date: str, reason: str = "House Use",
                      note: str | None = None):
	"""Hold a room out of sale for a date range. Refused if the room is
	already booked in that window (move the guest first)."""
	doc = frappe.get_doc({
		"doctype": "Room Block",
		"property": property,
		"room": room,
		"from_date": from_date,
		"to_date": to_date,
		"reason": reason,
		"note": note or None,
		"block_status": "Active",
	})
	doc.insert()
	from kamra.savings import log_action
	log_action("room_block", "Room Block", doc.name, property,
	           rationale=f"{reason}: {room} {from_date}→{to_date}")
	return {"name": doc.name}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent", scope={"name": "Room Block"})
def release_room_block(name: str):
	"""Free a held room before its end date (the room returns to sale)."""
	doc = frappe.get_doc("Room Block", name)
	doc.block_status = "Released"
	doc.released_by = frappe.session.user
	doc.released_on = frappe.utils.now_datetime()
	doc.save()
	from kamra.savings import log_action
	log_action("room_unblock", "Room Block", doc.name, doc.property,
	           rationale=f"released {doc.room}")
	return {"name": doc.name, "block_status": "Released"}


@frappe.whitelist()
@require_roles("Finance", "Front Desk", "Revenue Manager", "Housekeeping",
               "F&B Manager")
def cashier_pin_status(property: str):
	"""Does this property demand a PIN on money actions, and does the
	signed-in user have one set yet? Includes unlock / lockout state."""
	from kamra.authz import cashier_unlock_status
	return cashier_unlock_status(property)


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Front Desk", "Revenue Manager", "Housekeeping",
               "F&B Manager")
def set_cashier_pin(pin: str, current_pin: str | None = None):
	"""Set or change your own cashier PIN (4-8 digits). Changing an existing
	PIN needs the current one — unless must_reset was set by an admin."""
	pin = str(pin or "").strip()
	if not pin.isdigit() or not (4 <= len(pin) <= 8):
		frappe.throw("The PIN must be 4 to 8 digits.")
	user = frappe.session.user
	if frappe.db.exists("Cashier PIN", user):
		doc = frappe.get_doc("Cashier PIN", user)
		if not doc.get("must_reset"):
			from frappe.utils.password import get_decrypted_password
			stored = get_decrypted_password("Cashier PIN", user, "pin",
			                                raise_exception=False)
			if not current_pin or str(current_pin).strip() != str(stored):
				frappe.throw("Your current PIN is needed to change it.")
		doc.pin = pin
		doc.must_reset = 0
		doc.pin_attempts = 0
		doc.locked_until = None
		doc.save(ignore_permissions=True)
	else:
		frappe.get_doc({"doctype": "Cashier PIN", "user": user,
		                "pin": pin, "must_reset": 0}).insert(
			ignore_permissions=True)
	frappe.cache.delete_value(f"kamra_cashier_unlock:{user}")
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persists the completed operation before returning to an external/public caller; reviewed as intentional
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
@require_roles("Hotel Admin")
def reset_cashier_pin(user: str):
	"""Admin reset: wipe the user's PIN and force re-enrollment."""
	if not user:
		frappe.throw("user is required.")
	if frappe.db.exists("Cashier PIN", user):
		frappe.delete_doc("Cashier PIN", user, ignore_permissions=True,
		                  force=True)
	# Leave a must_reset stub so status can surface the flag, OR just
	# rely on PIN_NOT_SET. Create a stub with must_reset for clarity.
	frappe.get_doc({
		"doctype": "Cashier PIN",
		"user": user,
		"pin": "0000",  # replaced on enroll; blocked by must_reset
		"must_reset": 1,
		"pin_attempts": 0,
	}).insert(ignore_permissions=True)
	frappe.cache.delete_value(f"kamra_cashier_unlock:{user}")
	try:
		from kamra.savings import log_action
		log_action("reset_cashier_pin", "Cashier PIN", user, None,
		           rationale=f"PIN reset for {user} by {frappe.session.user}")
	except Exception:
		# the reset itself succeeded; losing its audit line must be visible
		frappe.log_error(title="Cashier PIN reset: action log write failed")
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persists the completed operation before returning to an external/public caller; reviewed as intentional
	return {"ok": True, "user": user, "must_reset": True}


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Front Desk", "Revenue Manager", "Housekeeping",
               "F&B Manager")
def verify_cashier_pin(property: str, pin: str):
	"""PinPad unlock: validate PIN and open a 15-minute sliding window."""
	from kamra.authz import unlock_cashier_session
	return unlock_cashier_session(property, pin)


# ---------------------------------------------------------------------------
# MICE: group room blocks + pickup (Group Rooms Control)
# ---------------------------------------------------------------------------

def _block_prices(property: str, check_in_date: str, check_out_date: str,
                  blocks, company: str | None = None) -> dict:
	"""What a room block costs: per room type, its rooms per night × the
	night's rate (the group rate when set, else the rate-card price for 2
	adults, room only, on the company's negotiated rate plan when it has
	one: the plan its stays are booked on) plus room tax. The same numbers
	drive the booking screen, the group's totals and the quotation."""
	from decimal import Decimal

	from kamra.booking_refs import resolve_room_type
	from kamra.group_blocks import group_nights, parse_nightly
	from kamra.ledger import _currency_places, _round_currency
	from kamra.pricing import apply_rate_plan, negotiated_rate_plan, quote, room_gst_rate
	places = max(_currency_places(property), 2)
	nights = group_nights(check_in_date, check_out_date)
	rows, before, tax, levy = [], 0.0, 0.0, 0.0
	for b in blocks or []:
		rt = resolve_room_type(property, b.get("room_type"))
		nightly = parse_nightly(b.get("nightly_counts") or b.get("nightly"))
		flat = cint(b.get("rooms_blocked"))
		counts = {d: nightly.get(d, flat) if nightly else flat for d in nights}
		if not any(counts.values()):
			continue
		rate = flt(b.get("block_rate"))
		rt_doc = frappe.get_cached_doc("Room Type", rt)
		levy_fixed = max(flt(frappe.db.get_value("Property", property, "room_levy_per_night")), 0.0)
		try:
			q = quote(property, rt, check_in_date, check_out_date,
			          min(2, cint(rt_doc.adults_capacity) or 2), 0, agreed_rate=rate,
			          company=company)
			# the quote's nights are the plan-free card; its room total is
			# the plan's. Each night carries the plan's share, as the folio
			# posts it (rate_plan_factor).
			card = sum(float(n["rate"]) for n in q["nightly"])
			factor = float(q["room_total"]) / card if card else 1.0
			per = {str(getdate(n["date"])): float(n["rate"]) * factor for n in q["nightly"]}
			room_total = sum(per.values())
			billable_q = max(len(q["nightly"]), 1)
			pct_levy = max(flt(q.get("room_levy")) - levy_fixed * billable_q, 0.0)
			# percent levy scales with room rate; fixed per-night levy scales with room_nights
			levy_share = pct_levy / room_total if room_total else 0
			tax_share = (flt(q["amount_after_tax"]) - room_total - flt(q.get("room_levy"))) \
				/ room_total if room_total else 0
		except frappe.ValidationError:
			# a closed or restricted date: show the card price (on the
			# company's plan, as quote prices it), tax at its slab
			base = rate or flt(rt_doc.base_price)
			plan = negotiated_rate_plan(property, company) if company and not rate else None
			if plan and nights:
				base = float(apply_rate_plan(plan, Decimal(str(base * len(nights))), nights=len(nights))) / len(nights)
			per = {d: base for d in nights}
			levy_share = 0
			tax_share = float(room_gst_rate(property, rt_doc, Decimal(str(base)))) / 100
		row_before = sum(n * per.get(d, 0) for d, n in counts.items())
		room_nights = sum(counts.values())
		row_tax = row_before * tax_share
		row_levy = row_before * levy_share + room_nights * levy_fixed
		rows.append({
			"room_type": rt, "room_type_name": rt_doc.room_type_name,
			"rooms": max(counts.values()), "nightly": counts if nightly else {},
			"room_nights": room_nights, "group_rate": rate > 0,
			"rate": _round_currency(row_before / room_nights, places) if room_nights else 0,
			"before_tax": _round_currency(row_before, places), "tax": _round_currency(row_tax, places),
			"levy": _round_currency(row_levy, places),
			"total": _round_currency(row_before + row_tax + row_levy, places),
		})
		before += row_before
		tax += row_tax
		levy += row_levy
	return {"rows": rows, "nights": len(nights), "before_tax": _round_currency(before, places),
	        "tax": _round_currency(tax, places), "levy": _round_currency(levy, places),
	        "total": _round_currency(before + tax + levy, places)}


@frappe.whitelist()
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def group_price_preview(property: str, check_in_date: str, check_out_date: str,
                        blocks, company: str | None = None):
	"""The price of a block before it is booked (New group booking)."""
	from kamra.crs import assert_property_access
	from kamra.pricing import negotiated_rate_plan
	assert_property_access(property)
	blocks = frappe.parse_json(blocks) if isinstance(blocks, str) else blocks
	comp_plan = negotiated_rate_plan(property, company) if company else None
	return {**_block_prices(property, check_in_date, check_out_date, blocks, company=company),
	        "can_set_rate": _can_set_rate(),
	        "comp_governance": comp_rate_governance(property, comp_plan)}


@frappe.whitelist()
@require_roles("Front Desk", "Revenue Manager", "Hotel Admin", "Kamra Agent")
def group_quotation(group_booking: str):
	"""The group's quotation / proforma: the hotel, the group, its rooms per
	type and night with the rate and tax, the total, the deposit asked and
	the terms (cutoff, allowed shortfall, who pays). Nothing is posted."""
	from kamra.crs import assert_property_access
	from kamra.group_blocks import effective_cutoff
	gb = frappe.get_doc("Group Booking", group_booking)
	assert_property_access(gb.property)
	prop = frappe.get_doc("Property", gb.property)
	from kamra.localization import pack_for
	ctx = pack_for(gb.property).invoice_context(prop) or {}
	prices = _block_prices(gb.property, str(gb.check_in_date), str(gb.check_out_date), [
		{"room_type": b.room_type, "rooms_blocked": b.rooms_blocked,
		 "block_rate": b.block_rate, "nightly_counts": b.get("nightly_counts")}
		for b in gb.blocks], company=gb.get("company"))
	contact = None
	if gb.get("master_reservation"):
		contact = frappe.db.get_value("Reservation", gb.master_reservation, "guest_name")
	return {
		"property": _quotation_header(prop, ctx),
		"group": {
			"name": gb.name, "group_name": gb.group_name, "group_type": gb.get("group_type"),
			"status": gb.status, "company": gb.company and frappe.db.get_value(
				"Corporate Account", gb.company, "company_name") or gb.company,
			"contact": contact, "master_pays": gb.get("master_pays"),
			"check_in_date": str(gb.check_in_date), "check_out_date": str(gb.check_out_date),
			"cutoff_date": str(effective_cutoff(gb.cutoff_date, gb.check_in_date)),
			"attrition_pct": flt(gb.get("attrition_pct")),
			"deposit_due": flt(gb.get("deposit_due")),
		},
		"prices": prices,
		"issued_on": nowdate(),
	}


def _quotation_header(prop, ctx: dict) -> dict:
	"""The hotel block at the top of a quotation (group or single booking)."""
	from kamra.localization import pack_for
	loc = pack_for(prop.name).locale(prop) or {}
	return {
		"property_name": prop.property_name, "logo_url": prop.get("logo_url"),
		"address": ", ".join(x for x in (prop.get("address_line"), prop.get("city"),
		                                 prop.get("state"), prop.get("pincode")) if x),
		"phone": prop.get("phone"), "email": prop.get("email"),
		"website": prop.get("website"), "tax_id": prop.get("gstin"),
		"tax_id_label": ctx.get("tax_id_label") or "Tax ID",
		"tax_label": ctx.get("tax_label") or "Tax",
		"currency": loc.get("currency") or prop.get("currency"),
		"currency_symbol": loc.get("currency_symbol"),
		"locale": loc.get("locale"),
	}


# Refusals for a single booking's quotation. Closed and append-only: a
# caller (the hotel screen, an agent) keys on the id before the colon.
QUOTE_REFUSALS = {
	"QUOTE_NO_EMAIL_ACCOUNT": "This site has no outgoing email account. An "
	                          "administrator sets one up (Email Account, default "
	                          "outgoing); until then print the quotation or save it as PDF.",
	"QUOTE_NO_RECIPIENT_EMAIL": "The booking has no email address on record for the "
	                            "booker. Add it to the guest profile first.",
	"QUOTE_BOOKING_CLOSED": "Only a booking that is still to come or in house gets a quotation.",
	"QUOTE_RECENTLY_SENT": "This quotation was emailed less than a minute ago. Wait a "
	                       "minute before sending the same quotation again.",
}
QUOTE_RESEND_SECONDS = 60
_QUOTE_STATUSES = ("Requested", "Held", "Pending Payment", "Waitlist", "Confirmed",
                   "Checked In")


def _refuse_quote(code: str):
	frappe.throw(f"{code}: {_(QUOTE_REFUSALS[code])}")


def _quotation_recipient(res) -> str | None:
	"""Where the booker reads email. The guest profile carries the only
	address a booking has; when someone else booked and asked to be the
	contact, that address is not theirs, so nobody is picked."""
	if res.get("booked_by_name") and res.get("contact_preference") == "Booker":
		return None
	email = frappe.db.get_value("Guest", res.guest, "email") if res.guest else None
	return (email or "").strip() or None


def _quote_line_items(res) -> list[dict]:
	"""The priced lines the booking's quote snapshot holds (pricing.quote
	line_items, saved with the totals by Reservation.apply_pricing)."""
	snap = res.get("quote_snapshot")
	if not snap:
		return []
	try:
		data = json.loads(snap) if isinstance(snap, str) else snap
		return [li for li in (data.get("line_items") or []) if isinstance(li, dict)]
	except (ValueError, TypeError, AttributeError):
		return []  # unreadable snapshot: the booked price shows as one line


def _quote_line_label(res, item: dict, meal_label: str | None) -> str:
	component = item.get("component")
	if component == "meal_plan" and meal_label:
		return _("Meal plan: {0}").format(meal_label)
	if component == "promotion" and res.get("voucher"):
		code = frappe.db.get_value("Discount Voucher", res.voucher, "voucher_code")
		return _("Discount: voucher {0}").format(code or res.voucher)
	return _(item.get("description") or component or "")


def _quotation_lines(res, nights: list[dict], meal_label: str | None) -> list[dict]:
	"""The price as an OPERA proforma shows it: room nights, then each
	inclusion (meal plan), fee and discount on its own line. The room line
	is the nights listed above it. The lines plus tax are the booking's
	total exactly: whatever the snapshot does not explain (a price set by
	hand, nights already charged) is its own adjustment line."""
	from kamra.pricing import night_rate_places
	places = night_rate_places(res.property)
	items = _quote_line_items(res)
	target = flt(flt(res.amount_after_tax) - flt(res.tax_amount), places)
	room = next((li for li in items if li.get("component") == "accommodation"), None)
	if nights:
		room_amount = flt(sum(n["rate"] for n in nights), places)
	elif room:
		room_amount = flt(room.get("amount"), places)
	else:
		room_amount = target
	lines = [{"component": "accommodation",
	          "description": _((room or {}).get("description")
	                           or ("Room nights" if nights or room else "Stay as booked")),
	          "amount": room_amount}]
	for li in items:
		if li.get("component") == "accommodation" or not flt(li.get("amount"), places):
			continue
		lines.append({"component": li.get("component"),
		              "description": _quote_line_label(res, li, meal_label),
		              "amount": flt(li.get("amount"), places)})
	rest = flt(target - sum(line["amount"] for line in lines), places)
	if rest:
		lines.append({"component": "adjustment",
		              "description": _("Adjustment to the booked price"), "amount": rest})
	return lines


def _quotation_terms(cancellation: dict, meal_label: str | None) -> list[str]:
	"""The terms printed under the price, matching the lines shown."""
	c = cancellation or {}
	if c.get("free_now") and c.get("free_until"):
		terms = [_("Free cancellation until {0}; after that {1} is charged.").format(
			c["free_until"], c.get("fee_label"))]
	elif c.get("free_now"):
		terms = [_("Free cancellation.")]
	else:
		terms = [_("Cancelling now is charged {0}.").format(c.get("fee_label"))]
	if meal_label:
		terms.append(_("Meal plan included: {0}, as priced above.").format(meal_label))
	else:
		terms.append(_("Room only: meals are not included."))
	terms.append(_("The lines above plus tax make up the total. Anything else taken "
	               "during the stay is billed separately."))
	return terms


def _booking_quotation(res) -> dict:
	import hashlib

	from kamra.localization import pack_for
	prop = frappe.get_doc("Property", res.property)
	ctx = pack_for(res.property).invoice_context(prop) or {}
	nights = [{"date": str(n.stay_date), "rate": flt(n.rate),
	           "meal": flt(n.get("meal_amount"))}
	          for n in (res.get("night_rates") or [])]
	meal_label = ((frappe.db.get_value("Meal Plan", res.meal_plan, "label")
	               or res.meal_plan) if res.meal_plan else None)
	cancellation = cancellation_outlook(res.property, res.room_type,
	                                    str(res.check_in_date), str(res.check_out_date))
	q = {
		"property": _quotation_header(prop, ctx),
		"booking": {
			"name": res.name, "status": res.status, "guest_name": res.guest_name,
			"booked_by": res.get("booked_by_name"),
			"company": (res.company and frappe.db.get_value(
				"Corporate Account", res.company, "company_name")) or res.company,
			"room_type_name": frappe.db.get_value(
				"Room Type", res.room_type, "room_type_name") if res.room_type else None,
			"meal_plan": meal_label,
			"adults": cint(res.adults), "children": cint(res.children),
			"check_in_date": str(res.check_in_date),
			"check_out_date": str(res.check_out_date), "nights": cint(res.nights),
		},
		"nights": nights,
		"lines": _quotation_lines(res, nights, meal_label),
		"totals": {
			"before_tax": flt(res.amount_before_tax), "tax": flt(res.tax_amount),
			"discount": flt(res.get("discount_amount")),
			"total": flt(res.amount_after_tax),
			"deposit_due": _deposit_state(res)["due"],
		},
		"cancellation": cancellation,
		"terms": _quotation_terms(cancellation, meal_label),
		"email_to": _quotation_recipient(res),
	}
	# what the guest is shown, so a resend of the same quotation is recognised
	q["version"] = hashlib.sha1(json.dumps(q, sort_keys=True, default=str).encode()
	                            ).hexdigest()[:12]  # nosec B324 -- change detector, not security
	q["issued_on"] = nowdate()
	return q


def _booking_quotation_html(q: dict) -> str:
	"""The quotation as a printable page, for the PDF the email carries."""
	from kamra.custom_reports import _page_html
	from kamra.localization import pack_for
	from kamra.pricing import _minor_unit
	esc = frappe.utils.escape_html
	p, b, t = q["property"], q["booking"], q["totals"]
	currency = p.get("currency") or ""
	raw_sym = p.get("currency_symbol")
	if raw_sym is None:
		prop_name = frappe.db.get_value("Reservation", b.get("name"), "property") if b.get("name") else None
		if prop_name:
			loc = pack_for(prop_name).locale(frappe.get_cached_doc("Property", prop_name)) or {}
			raw_sym = loc.get("currency_symbol")
			currency = currency or loc.get("currency") or ""
		if raw_sym is None:
			raw_sym = frappe.db.get_value("Currency", currency, "symbol") or ""
	sym = esc(raw_sym or "")
	digits = _minor_unit(currency)
	money = lambda n: f"{sym}{flt(n):,.{digits}f}"  # noqa: E731
	right = "style='text-align:right'"
	nights = "".join(
		f"<tr><td>{esc(n['date'])}</td><td {right}>{money(n['rate'])}</td></tr>"
		for n in q["nights"])
	lines = "".join(
		f"<tr><td>{esc(x['description'])}</td><td {right}>{money(x['amount'])}</td></tr>"
		for x in q["lines"])
	body = (
		f"<p>{esc(p['address'])}<br>{esc(' · '.join(x for x in (p.get('phone'), p.get('email')) if x))}</p>"
		f"<p><b>{esc(_('Guest'))}:</b> {esc(b.get('guest_name') or '')}<br>"
		f"<b>{esc(_('Stay'))}:</b> {esc(b['check_in_date'])} → {esc(b['check_out_date'])}"
		f" · {esc(_('{0} night(s)').format(b['nights']))} · {esc(b.get('room_type_name') or '')}</p>"
		+ (f"<table><thead><tr><th>{esc(_('Night'))}</th><th {right}>"
		   f"{esc(_('Rate / night'))}</th></tr></thead><tbody>{nights}</tbody></table>"
		   if nights else "")
		+ f"<h2>{esc(_('Price'))}</h2><table><tbody>{lines}"
		f"<tr><td>{esc(_(p['tax_label']))}</td><td {right}>{money(t['tax'])}</td></tr>"
		f"<tr><td><b>{esc(_('Total'))}</b></td><td {right}><b>{money(t['total'])}</b></td></tr>"
		+ (f"<tr><td>{esc(_('Deposit to confirm'))}</td><td {right}>{money(t['deposit_due'])}</td></tr>"
		   if t["deposit_due"] > 0 else "")
		+ "</tbody></table><h2>" + esc(_("Terms")) + "</h2><ul>"
		+ "".join(f"<li>{esc(x)}</li>" for x in q["terms"]) + "</ul>")
	meta = " · ".join([p["property_name"], b["name"], _("Issued {0}").format(q["issued_on"])])
	return _page_html(_("Quotation"), meta, body)


def _sent_recently(res, version: str) -> bool:
	"""This version of the booking's quotation already went out within the
	last QUOTE_RESEND_SECONDS (the quotation_emailed log row)."""
	from frappe.utils import add_to_date
	since = add_to_date(now_datetime(), seconds=-QUOTE_RESEND_SECONDS)
	for snap in frappe.get_all("Agent Action Log", filters={
			"action_type": "quotation_emailed", "reference_doctype": "Reservation",
			"reference_name": res.name, "executed_at": (">", since)},
			pluck="after_snapshot"):
		try:
			if (json.loads(snap or "{}") or {}).get("version") == version:
				return True
		except (ValueError, TypeError, AttributeError):
			continue
	return False


@frappe.whitelist()
@require_roles("Front Desk", "Revenue Manager", "Hotel Admin")
def booking_quotation(reservation: str):
	"""One booking's quotation / proforma: the hotel, the stay, each night's
	booked rate, the totals and deposit, and the cancellation terms. The
	same layout as group_quotation. Nothing is posted."""
	from kamra.crs import assert_property_access
	res = frappe.get_doc("Reservation", reservation)
	assert_property_access(res.property)
	return _booking_quotation(res)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Revenue Manager", "Hotel Admin")
def email_booking_quotation(reservation: str):
	"""Email the booking's quotation to the booker, as a PDF, through
	Frappe's own email: a Communication logged on the Reservation and sent
	by the site's outgoing account. It goes to the address on record only
	(the guest profile's email); no other recipient, cc or bcc can be given.
	The mail is queued (status "Queued"); Frappe's Email Queue delivers it.
	Refuses with QUOTE_NO_EMAIL_ACCOUNT, QUOTE_NO_RECIPIENT_EMAIL,
	QUOTE_BOOKING_CLOSED, or QUOTE_RECENTLY_SENT when the same version of
	the quotation went out less than a minute ago."""
	from frappe.core.doctype.communication.email import make
	from frappe.email.doctype.email_account.email_account import EmailAccount
	from frappe.utils.pdf import get_pdf

	from kamra.crs import assert_property_access
	res = frappe.get_doc("Reservation", reservation)
	assert_property_access(res.property)
	if res.status not in _QUOTE_STATUSES:
		_refuse_quote("QUOTE_BOOKING_CLOSED")
	if not EmailAccount.find_outgoing(match_by_doctype="Reservation"):
		_refuse_quote("QUOTE_NO_EMAIL_ACCOUNT")
	email = _quotation_recipient(res)
	if not email:
		_refuse_quote("QUOTE_NO_RECIPIENT_EMAIL")
	# one send per quotation version a minute; the row lock makes a double
	# click wait for the first send's log row
	frappe.db.get_value("Reservation", res.name, "name", for_update=True)
	q = _booking_quotation(res)
	if _sent_recently(res, q["version"]):
		_refuse_quote("QUOTE_RECENTLY_SENT")
	subject = _("Quotation {0} from {1}").format(res.name, q["property"]["property_name"])
	message = _("Dear {0}, please find your quotation for {1} to {2} attached. "
	            "Reference {3}.").format(res.guest_name or _("Guest"),
	                                     q["booking"]["check_in_date"],
	                                     q["booking"]["check_out_date"], res.name)
	pdf = get_pdf(_booking_quotation_html(q))
	comm = make(doctype="Reservation", name=res.name, subject=subject,
	            content=frappe.utils.escape_html(message), recipients=[email],
	            send_email=1,
	            attachments=[{"fname": f"Quotation-{res.name}.pdf", "fcontent": pdf}])
	from kamra.savings import log_action
	log_action("quotation_emailed", "Reservation", res.name, res.property,
	           rationale=f"Quotation PDF to {email} ({comm['name']})",
	           after_snapshot={"version": q["version"], "to": email,
	                           "communication": comm["name"]})
	# Frappe queues the mail; the Email Queue sends it
	return {"ok": True, "status": "Queued", "sent_to": email,
	        "communication": comm["name"]}


def _group_money(gb, prices: dict, rooming: list) -> dict:
	"""The group's room money: what the block is worth, what the named
	rooms are booked at, the advance on the group bill, everything paid so
	far on the group's bills, and what is still to pay - the open balances
	on those bills, plus the booked room nights not charged yet."""
	live = [r for r in rooming if r.status in ("Confirmed", "Checked In", "Checked Out")]
	booked = sum(flt(r.amount_after_tax) for r in live)
	group_folios = frappe.get_all(
		"Folio", filters={"group_booking": gb.name, "folio_type": "Group"},
		fields=["name", "payments_total", "balance"])
	member_folios = frappe.get_all(
		"Folio", filters={"reservation": ("in", [r.name for r in live] or [""]),
		                  "folio_type": ("!=", "Group")},
		fields=["name", "payments_total", "balance"])
	folios = [f.name for f in group_folios + member_folios]
	posted = flt(frappe.db.sql(
		"""SELECT COALESCE(SUM(total), 0) FROM `tabFolio Charge`
		   WHERE parent IN %(f)s AND charge_type IN ('Room', 'Meal Plan', 'Room Levy', 'Extra Guests')""",
		{"f": folios or [""]})[0][0])
	open_balances = sum(flt(f.balance) for f in group_folios + member_folios)
	# the advance alone: a settlement on the same bill is not a deposit
	deposits = flt(frappe.db.sql(
		"""SELECT COALESCE(SUM(amount), 0) FROM `tabFolio Payment`
		   WHERE parent IN %(f)s AND payment_kind = 'Advance'""",
		{"f": [f.name for f in group_folios] or [""]})[0][0])
	from kamra.ledger import _currency_places, _round_currency
	places = max(_currency_places(gb.property), 2)
	paid = sum(flt(f.payments_total) for f in group_folios + member_folios)
	return {"block_value": prices["total"], "booked": _round_currency(booked, places),
	        "deposits": _round_currency(deposits, places), "paid": _round_currency(paid, places),
	        "deposit_due": flt(gb.get("deposit_due")),
	        "due": _round_currency(max(0.0, open_balances + max(0.0, booked - posted)), places)}


def _group_attrition(gb, prices: dict) -> dict:
	"""How much of the block the group has picked up, in room-nights, and
	the fee its allowed shortfall (attrition %) permits for the rest."""
	from kamra.group_blocks import effective_cutoff, unpicked_by_night
	from kamra.ledger import _currency_places, _round_currency
	places = max(_currency_places(gb.property), 2)
	blocked = sum(r["room_nights"] for r in prices["rows"])
	held = sum(sum(unpicked_by_night(gb.name, r["room_type"]).values())
	           for r in prices["rows"])
	picked = max(0, blocked - held)
	allowed = flt(gb.get("attrition_pct"))
	short = max(0.0, blocked * (1 - allowed / 100) - picked)
	per_rn = prices["total"] / blocked if blocked else 0
	days = (effective_cutoff(gb.cutoff_date, gb.check_in_date) - getdate(nowdate())).days
	pct = round(picked / blocked * 100, 1) if blocked else 100.0
	return {"blocked_room_nights": blocked, "picked_room_nights": picked,
	        "pickup_pct": pct, "allowed_pct": allowed,
	        "shortfall_room_nights": round(short, 1),
	        "fee_estimate": _round_currency(short * per_rn, places),
	        "days_to_cutoff": days,
	        # a warning matters until the group arrives, not after it has stayed
	        "warn": bool(blocked) and gb.status == "Confirmed"
	        and getdate(nowdate()) <= getdate(gb.check_in_date)
	        and pct < 100 - allowed and days <= 7}


@frappe.whitelist()
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def group_detail(group_booking: str):
	"""Everything Group Rooms Control needs: the block, per-type pickup,
	the rooming list, the tied event and the master folio."""
	from kamra.group_blocks import _unpicked, effective_cutoff, parse_nightly
	gb = frappe.get_doc("Group Booking", group_booking)
	released_on = frappe.db.get_value("Group Booking", gb.name, "released_on") \
		if frappe.db.has_column("Group Booking", "released_on") else None
	pickup = []
	for b in gb.blocks:
		picked = frappe.db.count("Reservation", {
			"group_booking": gb.name, "room_type": b.room_type,
			"status": ("in", ["Confirmed", "Checked In", "Checked Out"])})
		pickup.append({
			"room_type": b.room_type,
			"rooms_blocked": b.rooms_blocked,
			"block_rate": b.block_rate,
			"nightly_counts": parse_nightly(b.get("nightly_counts")),
			"picked_up": picked,
			"remaining": _unpicked(gb.name, b.room_type),
			"rooms_released": int(b.get("rooms_released") or 0) if released_on else 0,
		})
	prices = _block_prices(gb.property, str(gb.check_in_date), str(gb.check_out_date), [
		{"room_type": b.room_type, "rooms_blocked": b.rooms_blocked,
		 "block_rate": b.block_rate, "nightly_counts": b.get("nightly_counts")}
		for b in gb.blocks], company=gb.get("company"))
	rooming = frappe.get_all(
		"Reservation",
		filters={"group_booking": gb.name},
		fields=["name", "guest", "guest_name", "room_type", "room", "status",
		        "check_in_date", "check_out_date", "pays_own_charges", "amount_after_tax"],
		order_by="creation asc")
	numbers = dict(frappe.get_all(
		"Room", filters={"name": ("in", [r.room for r in rooming if r.room])},
		fields=["name", "room_number"], as_list=True)) if rooming else {}
	phones = dict(frappe.get_all(
		"Guest", filters={"name": ("in", [r.guest for r in rooming] or [""])},
		fields=["name", "phone"], as_list=True))
	# each room's own bill (extras, and the room when it pays its own):
	# what that guest still owes at check-out
	own = {}
	for f in frappe.get_all("Folio", filters={
			"reservation": ("in", [r.name for r in rooming] or [""]),
			"folio_type": ("!=", "Group")}, fields=["reservation", "balance"]):
		own[f.reservation] = own.get(f.reservation, 0) + flt(f.balance)
	type_names = dict(frappe.get_all(
		"Room Type", filters={"property": gb.property},
		fields=["name", "room_type_name"], as_list=True))
	for p in pickup:
		p["room_type_name"] = type_names.get(p["room_type"], p["room_type"])
	from kamra.ledger import _currency_places, _round_currency
	places = max(_currency_places(gb.property), 2)
	for r in rooming:
		r["room_number"] = numbers.get(r.room)
		r["is_master"] = r.name == gb.get("master_reservation")
		r["phone"] = phones.get(r.guest)
		r["room_type_name"] = type_names.get(r.room_type, r.room_type)
		r["own_balance"] = _round_currency(own.get(r.name, 0), places)
		r["amount"] = flt(r.amount_after_tax)
	event = None
	if gb.get("event"):
		event = frappe.db.get_value(
			"Venue Booking", gb.event,
			["name", "venue", "event_type", "event_date", "status",
			 "attendees", "quoted_amount"], as_dict=True)
	bill = frappe.get_all(
		"Folio", filters={"group_booking": gb.name, "folio_type": "Group"},
		fields=["name", "status", "balance"], order_by="creation desc", limit=1)
	bill = bill[0] if bill else None
	master = bill.name if bill else None
	# the rooms the group has: per type, the block or the live bookings,
	# whichever is more (a group booked straight off create_group_booking
	# has no block at all, only its reservations)
	per_type = {b.room_type: int(b.rooms_blocked or 0) for b in gb.blocks}
	live = {}
	for r in rooming:
		if r.status in ("Confirmed", "Checked In", "Checked Out"):
			live[r.room_type] = live.get(r.room_type, 0) + 1
	rooms_total = sum(max(per_type.get(t, 0), live.get(t, 0))
	                  for t in set(per_type) | set(live))
	from kamra.business_date import get_business_date
	from kamra.pricing import negotiated_rate_plan
	comp_plan = negotiated_rate_plan(gb.property, gb.company) if gb.company else None
	return {
		"business_date": get_business_date(gb.property),
		"comp_governance": comp_rate_governance(gb.property, comp_plan),
		"group": {
			"rooms_total": rooms_total,
			"name": gb.name, "group_name": gb.group_name,
			"company": gb.company, "status": gb.status,
			"group_type": gb.get("group_type"),
			"master_reservation": gb.get("master_reservation"),
			"master_pays": gb.get("master_pays"),
			"in_house": sum(1 for r in rooming if r.status == "Checked In"),
			"check_in_date": str(gb.check_in_date),
			"check_out_date": str(gb.check_out_date),
			"cutoff_date": str(gb.cutoff_date) if gb.cutoff_date else None,
			"released_on": str(released_on) if released_on else None,
			"holds_inventory": gb.status == "Confirmed" and not released_on
			and effective_cutoff(gb.cutoff_date, gb.check_in_date)
			>= getdate(nowdate()),
			"notes": gb.notes,
			"attrition_pct": flt(gb.get("attrition_pct")),
			"deposit_due": flt(gb.get("deposit_due")),
		},
		"pickup": pickup,
		"money": _group_money(gb, prices, rooming),
		"attrition": _group_attrition(gb, prices),
		"room_types": [{"name": k, "room_type_name": v} for k, v in type_names.items()],
		"can_set_rate": _can_set_rate(),
		"rooming_list": rooming,
		"event": event,
		"master_folio": master,
		"master_folio_status": bill.status if bill else None,
		"master_folio_balance": _round_currency(bill.balance, places) if bill else 0,
	}


def _group_rooms(group_booking: str, reservations, statuses: tuple) -> list:
	"""The group's reservations in `statuses`: the ones picked on the
	rooming list, or every room when nothing is picked."""
	from kamra.crs import assert_property_access
	assert_property_access(
		frappe.db.get_value("Group Booking", group_booking, "property"))
	if isinstance(reservations, str):
		reservations = json.loads(reservations) if reservations.strip() else []
	filters = {"group_booking": group_booking, "status": ("in", statuses)}
	if reservations:
		filters["name"] = ("in", list(reservations))
	return frappe.get_all(
		"Reservation", filters=filters,
		fields=["name", "room", "status", "check_in_date", "check_out_date"],
		order_by="creation asc")


def _each_room(rows: list, fn) -> dict:
	"""Run `fn` on every room in its own savepoint, so one bad room is
	rolled back and reported without stopping the rest."""
	results = []
	for i, r in enumerate(rows):
		sp = f"kamra_group_room_{i}"
		frappe.db.savepoint(sp)
		try:
			results.append({"reservation": r.name, **(fn(r) or {})})
		except Exception as e:
			frappe.db.rollback(save_point=sp)
			frappe.clear_messages()
			results.append({"reservation": r.name, "error": str(e)})
	return {"results": results}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def set_rooming_names(group_booking: str, names):
	"""Name the rooms of a group from the rooming list.
	`names` = [{"reservation": ..., "guest_name": ..., "phone": ...}, ...].
	Each named room gets its own guest profile (an existing one when the
	phone matches) and its open guest folio follows. Rows left blank or
	unchanged are skipped."""
	if isinstance(names, str):
		names = json.loads(names)
	wanted = {n.get("reservation"): n for n in names if n.get("reservation")}
	rows = _group_rooms(group_booking, list(wanted) or ["-"],
	                    ("Confirmed", "Checked In"))

	def rename(r):
		row = wanted[r.name]
		guest_name = (row.get("guest_name") or "").strip()
		phone = (row.get("phone") or "").strip() or None
		res = frappe.get_doc("Reservation", r.name)
		current_phone = frappe.db.get_value("Guest", res.guest, "phone")
		if not guest_name or (guest_name == res.guest_name
		                      and (not phone or phone == current_phone)):
			return {"unchanged": 1}
		guest = _guest_for_row(guest_name, phone, res.property)
		res.guest = guest
		res.guest_name = frappe.db.get_value("Guest", guest, "full_name")
		res.save()
		for folio in frappe.get_all(
				"Folio", filters={"reservation": res.name, "folio_type": "Guest",
				                  "status": "Open"}, pluck="name"):
			frappe.db.set_value("Folio", folio,
			                    {"guest": guest, "guest_name": res.guest_name})
		return {"guest": guest, "guest_name": res.guest_name}

	return _each_room(rows, rename)


def _assign_free_room(reservation: str) -> str | None:
	"""Put a confirmed stay with no room number into a free room of its
	type (the group's own held rooms count as free for it). An arrival due
	today only gets a room nobody is still checked in to."""
	res = frappe.get_doc("Reservation", reservation)
	if res.room:
		return res.room
	free = available_rooms(
		res.property, res.room_type, str(res.check_in_date),
		str(res.check_out_date), group_booking=res.get("group_booking"),
		ready_now=1)
	if not free:
		return None
	res.room = free[0]["name"]
	res.save()
	return res.room


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def group_assign_rooms(group_booking: str, reservations=None):
	"""Give every confirmed room of the group that has no room number a
	free room of its type (the picked ones, or all)."""
	def one(r):
		if r.room:
			return {"unchanged": 1}
		room = _assign_free_room(r.name)
		if not room:
			return {"skipped": "No free room of this type"}
		return {"assigned": 1,
		        "room_number": frappe.db.get_value("Room", room, "room_number")}

	return _each_room(
		_group_rooms(group_booking, reservations, ("Confirmed",)), one)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def group_cancel(group_booking: str, reservations=None,
                 reason: str = "Guest request", waive_fee: int = 0):
	"""Cancel the group's confirmed rooms (the picked ones, or all), each
	under the cancellation policy. When no active room is left the group
	itself is marked Cancelled - unless nothing was cancelled here, or a
	room has already stayed."""
	out = _each_room(
		_group_rooms(group_booking, reservations, ("Confirmed",)),
		lambda r: {"cancelled": 1, **(cancel_reservation(
			r.name, reason=reason, waive_fee=cint(waive_fee)) or {})})
	if any(x.get("cancelled") for x in out["results"]) and not frappe.db.exists(
			"Reservation", {"group_booking": group_booking,
			                "status": ("in", ["Confirmed", "Checked In", "Checked Out"])}):
		frappe.db.set_value("Group Booking", group_booking, "status", "Cancelled")
	return out


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def group_amend_dates(group_booking: str, check_in_date: str,
                      check_out_date: str, reservations=None,
                      confirm_price: int = 0):
	"""Move the group's rooms (the picked ones, or all) to new dates. Each
	stay is re-quoted as amend_stay does, so pass confirm_price=1 once the
	desk has accepted that prices may change. A guest on their own nights
	keeps them relative to the group's arrival, inside the new dates.

	When every room moved, the group moves too: its rooms per night shift
	with it, and the rooms it still holds must be free on the new dates, or
	the whole change is refused."""
	import json as _json

	from kamra.group_blocks import _unpicked, effective_cutoff, group_nights, parse_nightly
	gb = frappe.get_doc("Group Booking", group_booking)
	old_ci, old_co = getdate(gb.check_in_date), getdate(gb.check_out_date)
	new_ci, new_co = getdate(check_in_date), getdate(check_out_date)
	if new_co <= new_ci:
		frappe.throw(_("Departure must be after arrival."))
	shift = (new_ci - old_ci).days
	statuses = ("Confirmed", "Checked In")

	def one(r):
		ci, co = getdate(r.check_in_date), getdate(r.check_out_date)
		if (ci, co) == (old_ci, old_co):
			ci, co = new_ci, new_co
		else:
			ci = max(getdate(add_days(ci, shift)), new_ci)
			co = min(getdate(add_days(co, shift)), new_co)
			if ci >= co:
				frappe.throw(_("This guest's own nights fall outside the new dates."))
		return {"amended": 1, **(amend_stay(
			r.name, str(ci), str(co), confirm_price=cint(confirm_price)) or {})}

	sp = "kamra_group_amend"
	frappe.db.savepoint(sp)
	every_room = {r.name for r in _group_rooms(group_booking, None, statuses)}
	out = _each_room(_group_rooms(group_booking, reservations, statuses), one)
	if not every_room <= {x["reservation"] for x in out["results"] if x.get("amended")}:
		return out

	old_nights = group_nights(old_ci, old_co)
	new_nights = group_nights(new_ci, new_co)
	for b in gb.blocks:
		nightly = parse_nightly(b.get("nightly_counts"))
		if nightly:
			by_position = [nightly.get(d, b.rooms_blocked) for d in old_nights]
			moved = {d: by_position[i] if i < len(by_position) else b.rooms_blocked
			         for i, d in enumerate(new_nights)}
			b.nightly_counts = _json.dumps(moved)
			b.rooms_blocked = max(moved.values())
	gb.check_in_date, gb.check_out_date = str(new_ci), str(new_co)
	gb.save()
	if gb.status == "Confirmed" and not gb.get("released_on") \
			and effective_cutoff(gb.cutoff_date, new_ci) >= getdate(nowdate()):
		short = []
		for b in gb.blocks:
			held = _unpicked(gb.name, b.room_type)
			free = len(available_rooms(gb.property, b.room_type, str(new_ci), str(new_co),
			                           group_booking=gb.name)) if held else 0
			if held > free:
				short.append(_("{0}: {1} kept, {2} free").format(b.room_type, held, free))
		if short:
			frappe.db.rollback(save_point=sp)
			frappe.clear_messages()
			frappe.throw(_("The rooms kept for this group are not free on those dates. {0}")
			             .format("; ".join(short)))
	return out


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def group_check_in(group_booking: str, reservations=None):
	"""Check in a group's arrivals in one go. Rooms still unassigned get
	the same suggestion the tape chart makes; then each stay goes through
	check_in, the same call as a single check-in. One stay that cannot go
	in (no free room) does not stop the rest. IDs, deposits and the
	registration card stay open per room to finish later, as with any
	check-in. `reservations` limits it to some of the group."""
	from kamra.allocation import apply_allocation, suggest_allocation
	from kamra.business_date import get_business_date

	gb = frappe.get_doc("Group Booking", group_booking)
	today = get_business_date(gb.property)
	filters = {"group_booking": gb.name, "status": "Confirmed",
	           "check_in_date": ("<=", today)}
	if reservations:
		if isinstance(reservations, str):
			reservations = frappe.parse_json(reservations)
		filters["name"] = ("in", list(reservations))
	arrivals = frappe.get_all("Reservation", filters=filters,
	                          fields=["name", "guest_name", "room", "check_in_date"],
	                          order_by="creation asc")
	mine = {a.name: str(a.check_in_date) for a in arrivals if not a.room}
	# suggestions are per arrival date: a late arrival from yesterday too
	for day in sorted(set(mine.values())):
		plan = suggest_allocation(gb.property, day)
		pairs = [{"reservation": p["reservation"], "room": p["suggested_room"]}
		         for p in plan["proposals"] if p["reservation"] in mine]
		if pairs:
			apply_allocation(gb.property, pairs)
	checked_in, skipped, to_finish = [], [], 0
	for a in arrivals:
		sp = f"gci_{len(checked_in) + len(skipped)}"
		frappe.db.savepoint(sp)
		try:
			out = check_in(a.name)
		except Exception as e:
			frappe.db.rollback(save_point=sp)
			frappe.clear_messages()
			skipped.append({"reservation": a.name, "guest_name": a.guest_name,
			                "reason": str(e) or "Could not check in"})
			continue
		pending = out.get("pending_backfill") or []
		to_finish += 1 if pending else 0
		checked_in.append({"reservation": a.name, "guest_name": a.guest_name,
		                   "room": out.get("room"), "keys": out.get("keys"),
		                   "pending": [p["title"] for p in pending]})
	# the same rows in the per-room shape the other group actions return
	results = [{"reservation": x["reservation"], "checked_in": 1} for x in checked_in] \
		+ [{"reservation": x["reservation"], "skipped": x["reason"]} for x in skipped]
	return {"checked_in": checked_in, "skipped": skipped, "to_finish": to_finish,
	        "business_date": today, "results": results}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def save_group_blocks(group_booking: str, blocks, cutoff_date: str | None = None,
                      status: str | None = None, attrition_pct: float | None = None,
                      deposit_due: float | None = None):
	"""Set the room block (list of {room_type, rooms_blocked, block_rate,
	nightly_counts}) and optionally the cutoff/status, allowed shortfall %
	and deposit due. Confirmed blocks hold inventory. A row sent without
	nightly_counts keeps its rooms per night while its Rooms Blocked is
	unchanged; a new Rooms Blocked makes every night that number."""
	import json as _json

	from kamra.group_blocks import parse_nightly
	if isinstance(blocks, str):
		blocks = frappe.parse_json(blocks)
	gb = frappe.get_doc("Group Booking", group_booking)
	before = {b.room_type: b for b in gb.blocks}
	gb.blocks = []
	for b in blocks or []:
		if not b.get("room_type") or not int(b.get("rooms_blocked") or 0):
			continue
		was = before.get(b["room_type"])
		if flt(b.get("block_rate")) != flt(was.block_rate if was else 0) and not _can_set_rate():
			# the block rate is billed, so changing it is a rate decision
			frappe.throw(_("Only a Revenue Manager or Hotel Admin can change a group rate."),
			             frappe.PermissionError)
		nightly = parse_nightly(b.get("nightly_counts"))
		old = before.get(b["room_type"])
		if not nightly and old and old.get("nightly_counts") \
				and int(old.rooms_blocked) == int(b["rooms_blocked"]):
			nightly = parse_nightly(old.nightly_counts)
		gb.append("blocks", {
			"room_type": b["room_type"],
			"rooms_blocked": max(nightly.values()) if nightly else int(b["rooms_blocked"]),
			"block_rate": float(b.get("block_rate") or 0),
			"nightly_counts": _json.dumps(nightly) if nightly else None,
		})
	if attrition_pct is not None:
		gb.attrition_pct = flt(attrition_pct)
	if deposit_due is not None:
		gb.deposit_due = flt(deposit_due)
	if cutoff_date is not None:
		gb.cutoff_date = cutoff_date or None
		# a cutoff pushed back into the future re-opens a released block:
		# its unpicked rooms are held again until the new date
		from kamra.group_blocks import effective_cutoff
		if gb.get("released_on") and effective_cutoff(
				gb.cutoff_date, gb.check_in_date) >= getdate(nowdate()):
			gb.released_on = None
			frappe.db.set_value("Group Booking", gb.name, "released_on", None,
			                    update_modified=False)
	if status:
		gb.status = status
	gb.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persists the completed operation before returning to an external/public caller; reviewed as intentional
	return {"ok": True, "blocks": len(gb.blocks)}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent")
def pickup_group_room(group_booking: str, room_type: str, guest_name: str,
                      phone: str | None = None, adults: int = 2,
                      children: int = 0, idempotency_key: str | None = None,
                      rate_plan: str | None = None,
                      comp_supervisor_user: str | None = None,
                      comp_supervisor_pin: str | None = None):
	"""Name a guest into the block: creates a reservation on the group's
	dates against its held inventory. Every call books a room (a block is
	several identical rooms for one party); pass idempotency_key to make a
	retry safe. A company block whose negotiated plan needs a comp sign-off
	takes comp_supervisor_user + comp_supervisor_pin."""
	gb = frappe.get_doc("Group Booking", group_booking)
	out = create_booking(
		property=gb.property, room_type=room_type,
		check_in_date=str(gb.check_in_date),
		check_out_date=str(gb.check_out_date),
		guest_name=guest_name, phone=phone,
		adults=int(adults), children=int(children),
		rate_plan=rate_plan or gb.get("rate_plan") or None,
		booking_type="Group", company=gb.company,
		group_booking=gb.name, source="Manual",
		idempotency_key=idempotency_key,
		comp_supervisor_user=comp_supervisor_user,
		comp_supervisor_pin=comp_supervisor_pin)
	if not out.get("idempotent_replay"):
		from kamra.savings import log_action
		approver = frappe.db.get_value(
			"Agent Action Log",
			{"action_type": "comp_rate_sign_off", "reference_name": out["reservation"]},
			"approver",
		)
		log_action("group_pickup", "Group Booking", gb.name, gb.property,
		           rationale=f"{guest_name} picked up a {room_type.split('-')[-1]}"
		                     f" from block {gb.group_name}",
		           channel=agent_channel() or "Desk",
		           **({"approver": approver} if approver else {}))
	return out


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Revenue Manager", "Kamra Agent")
def create_group_block(property: str, group_name: str, check_in_date: str,
                       check_out_date: str, blocks,
                       company: str | None = None,
                       cutoff_date: str | None = None,
                       venue: str | None = None,
                       event_type: str | None = None,
                       event_date: str | None = None,
                       attendees: int = 0,
                       customer_phone: str | None = None,
                       notes: str | None = None,
                       rate_plan: str | None = None):
	"""One call drafts the whole piece of MICE business: the group, its room
	block, and (optionally) the banquet event - the agent wedge: an inquiry
	agent turns "30 rooms + a 200-pax wedding on Dec 12" into a proposal."""
	if isinstance(blocks, str):
		blocks = frappe.parse_json(blocks)
	if any(flt(b.get("block_rate")) > 0 for b in blocks or []) and not _can_set_rate():
		frappe.throw(_("Only a Revenue Manager or Hotel Admin can set a group rate."),
		             frappe.PermissionError)
	from kamra.booking_refs import resolve_rate_plan, resolve_room_type
	from kamra.pricing import resolve_group_rate_plan
	rate_plan = resolve_rate_plan(property, rate_plan)
	rate_plan = resolve_group_rate_plan(property, rate_plan=rate_plan, company=company)
	gb = frappe.get_doc({
		"doctype": "Group Booking",
		"property": property,
		"group_name": group_name,
		"company": company,
		"rate_plan": rate_plan or None,
		"check_in_date": check_in_date,
		"check_out_date": check_out_date,
		"cutoff_date": cutoff_date,
		"status": "Open",
		"notes": notes,
	})
	for b in blocks or []:
		gb.append("blocks", {
			# agents name room types the way staff do ("Deluxe", "DLX")
			"room_type": resolve_room_type(property, b.get("room_type")),
			"rooms_blocked": int(b.get("rooms_blocked") or 0),
			"block_rate": float(b.get("block_rate") or 0),
		})
	gb.insert(ignore_permissions=True)
	event = None
	if venue:
		ev = frappe.get_doc({
			"doctype": "Venue Booking",
			"property": property,
			"venue": venue,
			"event_type": event_type or "Other",
			"status": "Enquiry",
			"event_date": event_date or check_in_date,
			"customer_name": group_name,
			"customer_phone": customer_phone,
			"company": company,
			"attendees": int(attendees or 0),
			"group_booking": gb.name,
		})
		ev.insert(ignore_permissions=True)
		gb.event = ev.name
		gb.save(ignore_permissions=True)
		event = ev.name
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persists the completed operation before returning to an external/public caller; reviewed as intentional
	from kamra.savings import log_action
	log_action("group_block_drafted", "Group Booking", gb.name, property,
	           minutes_saved=15,
	           rationale=f"Drafted block '{group_name}' "
	                     f"({sum(int(b.get('rooms_blocked') or 0) for b in blocks or [])} rooms"
	                     f"{' + event' if event else ''})")
	return {"group_booking": gb.name, "event": event, "status": gb.status}



@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Revenue Manager", "Housekeeping")
def my_connector_credentials(property: str):
	"""Personal MCP credentials for connecting Claude (or any MCP client)
	AS YOURSELF. The key acts with exactly your roles - Frappe enforces the
	same gates as the UI, so a front-desk connection can do front-desk
	things and nothing more. Regenerating invalidates the old secret.

	Platform-wide / service keys stay on the Developers page (IT admin)."""
	user = frappe.session.user
	api_key = frappe.db.get_value("User", user, "api_key")
	if not api_key:
		api_key = frappe.generate_hash(length=15)
	api_secret = frappe.generate_hash(length=15)
	u = frappe.get_doc("User", user)
	u.api_key = api_key
	u.api_secret = api_secret
	u.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persists the completed operation before returning to an external/public caller; reviewed as intentional
	from kamra.savings import log_action
	log_action("connector_key_issued", "User", user,
	           rationale="Personal MCP connector credentials (re)generated")
	return {"api_key": api_key, "api_secret": api_secret,
	        "base_url": frappe.utils.get_url(), "property": property,
	        "user": user}


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Revenue Manager", "Housekeeping")
def linked_records(doctype: str, name: str):
	"""The connective tissue: for any record, everything it's attached to -
	guest, reservation(s), folio(s), company, group, event - so every screen
	can offer one-tap paths to billing and editing. One endpoint, all types."""
	from kamra.authz import assert_record_access
	if frappe.db.exists("DocType", doctype):
		assert_record_access(doctype, name)
	out = {"guest": None, "guest_name": None, "reservations": [],
	       "folios": [], "company": None, "group_booking": None,
	       "group_name": None, "event": None, "event_type": None}

	def folios_for(filters):
		from kamra import localization as loc
		rows = frappe.get_all("Folio", filters=filters,
			fields=["name", "property", "folio_type", "status", "balance",
			        "invoice_number"], order_by="creation asc")
		cache: dict[str, dict] = {}
		for row in rows:
			p = row.get("property")
			if p and p not in cache:
				prop_doc = frappe.get_cached_doc("Property", p)
				pl = loc.pack_for(p).locale(prop_doc)
				cache[p] = {
					"currency": pl.get("currency") or prop_doc.get("currency") or "",
					"currency_symbol": pl.get("currency_symbol", ""),
					"locale": pl.get("locale") or "en-US",
				}
			if p in cache:
				row.update(cache[p])
		return rows

	if doctype == "Reservation":
		r = frappe.db.get_value("Reservation", name,
			["guest", "guest_name", "company", "group_booking"], as_dict=True)
		if not r:
			frappe.throw("Reservation not found.")
		out.update(guest=r.guest, guest_name=r.guest_name, company=r.company,
		           group_booking=r.group_booking)
		out["folios"] = folios_for({"reservation": name})
	elif doctype == "Venue Booking":
		v = frappe.db.get_value("Venue Booking", name,
			["company", "group_booking", "customer_name", "event_type"],
			as_dict=True)
		if not v:
			frappe.throw("Event not found.")
		out.update(company=v.company, group_booking=v.group_booking,
		           guest_name=v.customer_name, event=name,
		           event_type=v.event_type)
		if v.group_booking:
			out["folios"] = folios_for(
				{"group_booking": v.group_booking, "folio_type": "Group"})
	elif doctype == "Group Booking":
		g = frappe.db.get_value("Group Booking", name,
			["company", "event", "group_name"], as_dict=True)
		if not g:
			frappe.throw("Group not found.")
		out.update(company=g.company, event=g.event, group_booking=name,
		           group_name=g.group_name)
		if g.event:
			out["event_type"] = frappe.db.get_value(
				"Venue Booking", g.event, "event_type")
		out["folios"] = folios_for(
			{"group_booking": name, "folio_type": "Group"})
	elif doctype == "Guest":
		out["guest"] = name
		out["guest_name"] = frappe.db.get_value("Guest", name, "full_name")
		out["reservations"] = frappe.get_all("Reservation",
			filters={"guest": name},
			fields=["name", "status", "check_in_date"],
			order_by="check_in_date desc", limit=5)
		out["folios"] = folios_for({"guest": name})[-5:]
	if out["group_booking"] and not out.get("group_name"):
		out["group_name"] = frappe.db.get_value(
			"Group Booking", out["group_booking"], "group_name")
	return out


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Revenue Manager", "Housekeeping", "Kamra Agent",
               "F&B Manager")
def property_locale(property: str):
	"""Currency, number locale and tax vocabulary for this property, from its
	localization pack. Drives the frontend's money formatting and tax dropdowns
	so no screen hardcodes ₹ or GST %. `integrations` says which country
	integrations (gateways, tax return, ID checks) this property has."""
	from kamra.erp_bridge import erp_determines_tax
	from kamra.localization import front_desk_vocabulary, integrations, pack_for, services_tax_rate, tender_modes
	from kamra.masters import desk_vocabulary
	from kamra.pricing import tax_basis
	from kamra.settings import get_setting, list_setting
	prop = frappe.get_cached_doc("Property", property)
	pack = pack_for(property)
	loc = dict(pack.locale(prop))
	if prop.get("locale"):
		loc["locale"] = prop.get("locale")
	date_fmt = get_setting(property, "date_format") or ""
	if not isinstance(date_fmt, str) or not date_fmt:
		try:
			from frappe.utils.dateutils import get_user_date_format
			candidate = get_user_date_format()
			date_fmt = candidate if isinstance(candidate, str) else ""
		except Exception:
			date_fmt = ""
	from kamra.cashier import cashier_allowed_modes
	vocab = front_desk_vocabulary(pack, property)
	t_modes = tender_modes(property)
	allowed = cashier_allowed_modes(property)
	if allowed is not None:
		allowed_set = set(allowed)
		vocab["payment_modes"] = [m for m in vocab["payment_modes"] if m in allowed_set]
		vocab["outlet_payment_modes"] = [m for m in vocab["outlet_payment_modes"] if m in allowed_set]
		if t_modes is not None:
			t_modes = [row for row in t_modes if row["mode"] in allowed_set]
	return {**loc, **vocab,
	        "cashier_allowed_modes": allowed,
	        "date_format": date_fmt,
	        # the hotel's own Market codes, booker relations and Cancellation
	        # Reasons, [{value, label}], for the booking and cancel screens
	        **desk_vocabulary(),
	        "integrations": integrations(pack),
	        "tender_modes": t_modes,
	        "fx_currencies": list_setting(property, "fx_currencies"),
	        "tax_basis": tax_basis(property),
	        "erp_tax_managed": bool(erp_determines_tax()),
	        # the rate the server puts on any food & beverage charge, whatever
	        # rate the caller picked (_resolve_charge_gst) - so the folio's
	        # charge form shows the rate that will apply instead of a choice
	        # that is ignored
	        "fnb_tax_rate": float(pack.fnb_tax_rate(property)),
	        "fnb_charge_words": list(_FNB_WORDS),
	        # the property's rate on a guest service (laundry, spa, misc): the
	        # charge form starts there, so a hotel that charges no tax opens
	        # on 0% rather than a made-up rate
	        "services_tax_rate": float(services_tax_rate(pack, property)),
	        # a dry property hides every alcohol field and label
	        "sells_alcohol": bool(int(get_setting(property, "sells_alcohol") or 0))}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance")
def verify_tax_id(property: str, tax_id: str, business_name: str = ""):
	"""Check a buyer's tax ID before the invoice goes out, where the
	property's country pack offers it."""
	from kamra import localization as loc
	return loc.verify_tax_id(loc.pack_for(property), property, tax_id,
	                         business_name)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", scope={"guest": "Guest"})
def verify_guest_id(property: str, guest: str, id_type: str, consent: int = 0):
	"""Read the guest's ID document on file into the profile, where the
	property's country pack offers it. consent: the desk confirms the guest
	was told."""
	from kamra import localization as loc
	return loc.verify_guest_id(loc.pack_for(property), property, guest,
	                           id_type, consent)


@frappe.whitelist()
@require_roles("Finance")
def zatca_settings(property: str):
	"""The property's ZATCA (Saudi e-invoicing) settings, created from the
	property on first use, plus what is still missing for a valid invoice
	and where the invoice chain stands."""
	from kamra import zatca
	from kamra.crs import assert_property_access
	assert_property_access(property)
	s = zatca.settings_for(property)
	return {
		"name": s.name,
		"missing": zatca.readiness(s),
		"issued": frappe.db.count(zatca.RECORD, {"property": property}),
		"last_icv": s.last_icv,
		"onboarding_status": s.onboarding_status,
		"environment": s.environment,
	}


@frappe.whitelist()
@require_roles("Finance")
def zatca_invoice_xml(invoice_number: str, property: str,
                      document_type: str = "Invoice"):
	"""The UBL XML behind an issued invoice or credit note - for the
	accountant, an auditor or a ZATCA query."""
	from kamra.crs import assert_property_access
	assert_property_access(property)
	rec = frappe.db.get_value(
		"ZATCA Invoice", {"property": property, "invoice_number": invoice_number,
		                  "document_type": document_type},
		["xml", "uuid", "icv", "invoice_hash", "status"], as_dict=True)
	if not rec:
		frappe.throw(_("No ZATCA record for {0}.").format(invoice_number))
	return rec


@frappe.whitelist()
def localization_countries():
	"""Countries with a dedicated tax & invoicing pack, with the currency,
	time zone and tax vocabulary picking one sets up. Read-only reference
	data for the setup wizard and Settings."""
	from kamra.localization import supported_countries
	return supported_countries()


@frappe.whitelist()
@require_roles("Front Desk", "Kamra Agent")
def pending_deposit_refunds(property: str):
	"""Returns a list of checked-out reservations with pending security deposit refunds
	for NEFT processing (e.g. Tuesday/Saturday payout batches).
	"""
	reservations = frappe.get_all(
		"Reservation",
		filters={"property": property, "status": "Checked Out"},
		fields=["name", "guest_name", "check_in_date", "check_out_date"]
	)

	pending = []
	for res in reservations:
		folio_name = frappe.db.get_value("Folio", {"reservation": res.name}, "name")
		if not folio_name:
			continue

		payments = frappe.get_all(
			"Folio Payment",
			filters={"parent": folio_name, "parenttype": "Folio"},
			fields=["payment_kind", "amount"]
		)

		deposits_sum = sum(p.amount for p in payments if p.payment_kind == "Security Deposit")
		refunds_sum = sum(p.amount for p in payments if p.payment_kind == "Refund")

		if deposits_sum > refunds_sum:
			pending.append({
				"reservation": res.name,
				"guest_name": res.guest_name,
				"check_out_date": res.check_out_date,
				"deposit_amount": float(deposits_sum),
				"refunded_amount": float(refunds_sum),
				"pending_refund": float(deposits_sum - refunds_sum)
			})

	return pending
