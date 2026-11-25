"""Operational dashboards - one property, and the whole portfolio.

Property dashboard: the day at a glance for a single hotel, arranged by
department (front desk / housekeeping / finance) on top of the numbers the
manager flash already computes. Portfolio dashboard: the same headline
metrics rolled up across every property the signed-in user can access, with
a per-property table - the chain's central view.
"""

from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe.utils import add_days, add_years, getdate

from kamra.authz import ADMIN, require_roles


def _housekeeping_slice(property: str):
	"""Room-status breakdown + task load for the housekeeping department."""
	rooms = frappe.get_all(
		"Room", filters={"property": property},
		fields=["housekeeping_status", "occupancy_status"])
	status = {
		"Clean": 0, "Dirty": 0, "Pickup": 0,
		"Inspected": 0, "Ready": 0,
		"Out of Service": 0, "Out of Order": 0,
	}
	occupied = 0
	for r in rooms:
		status[r.housekeeping_status] = status.get(r.housekeeping_status, 0) + 1
		if r.occupancy_status == "Occupied":
			occupied += 1
	from frappe.utils import now_datetime
	open_tasks = frappe.get_all(
		"Housekeeping Task",
		filters={"property": property, "status": ("in", ["Pending", "In Progress"])},
		fields=["due_by"])
	now = now_datetime()
	overdue = sum(
		1 for t in open_tasks
		if t.due_by and frappe.utils.get_datetime(t.due_by) < now)
	return {
		"room_status": status,
		"occupied": occupied,
		"vacant": len(rooms) - occupied,
		"open_tasks": len(open_tasks),
		"overdue_tasks": overdue,
	}


def _finance_slice(property: str, date: str):
	"""Collections today (by mode), and money still owed on open folios."""
	from kamra.api import cash_summary
	collections = cash_summary(property, date)
	outstanding, open_folios = _open_folio_totals(property)
	return {
		"collections_today": collections.get("grand_total", 0),
		"collections_by_mode": collections.get("modes", []),
		"outstanding": outstanding,
		"open_folios": open_folios,
	}


# who sees the money on the dashboard; the front desk sees arrivals,
# departures, in house, rooms and occupancy (now and on the books), never
# revenue, ADR, RevPAR, collections or outstanding
FIGURE_ROLES = ("Finance", "Revenue Manager", "Night Auditor", "Kamra Agent", *ADMIN)
FIGURE_KEYS = ("occupancy_pct", "revenue_today", "revpar", "adr", "room_revenue_today",
               "collections_today", "statistics", "finance", "outlook", "comparison")
FORECAST_DAYS = 90


def _sellable(property: str) -> int:
	# out of order rooms are not for sale, as channel_manager._sellable counts
	return frappe.db.count("Room", {
		"property": property, "housekeeping_status": ("!=", "Out of Order")})


def occupancy_now(property: str, hk: dict) -> dict:
	"""Rooms occupied right now over sellable rooms, the rooms in house on a
	category left out of occupancy not counted (kamra.room_statistics)."""
	from kamra.room_statistics import (
		category_rules,
		counted_occupied,
		in_house_by_category,
		occupancy_pct,
	)
	sellable = _sellable(property)
	occupied = hk["occupied"]
	rules = category_rules(property)
	counted = counted_occupied(rules, occupied, in_house_by_category(rules, property))
	return {"occupied": counted, "sellable": sellable,
	        "occupancy_pct": occupancy_pct(counted, sellable)}


def forecast_summary(forecast: list[dict], sellable: int) -> dict:
	"""30/60/90-day and weekly occupancy roll-ups of `occupancy_forecast`,
	using the shared room_statistics rule (rooms_occupied over sellable
	room-nights, rooms only, no money) so no frontend recomputes occupancy."""
	from kamra.room_statistics import occupancy_pct
	spans = []
	for n in (30, 60, 90):
		chunk = forecast[:n]
		spans.append({
			"days": n,
			"occupancy_pct": occupancy_pct(sum(x["rooms_occupied"] for x in chunk),
			                               sellable * len(chunk)),
		})
	weeks = []
	for i in range(0, len(forecast), 7):
		chunk = forecast[i:i + 7]
		weeks.append({
			"week_of": chunk[0]["date"],
			"booked": sum(x["booked"] for x in chunk),
			"rooms_occupied": sum(x["rooms_occupied"] for x in chunk),
			"held": sum(x["held"] for x in chunk),
			"occupancy_pct": occupancy_pct(sum(x["rooms_occupied"] for x in chunk),
			                               sellable * len(chunk)),
		})
	return {"spans": spans, "weeks": weeks}


def occupancy_forecast(property: str, date: str, days: int = FORECAST_DAYS) -> list[dict]:
	"""On the books for each night from date: live reservations covering the
	night (every live status, as group_blocks counts a pick up) over sellable rooms,
	plus group block rooms held but not yet picked up. Rooms only, no money.
	Every room booked is off sale; occupancy leaves out the rooms on a
	category left out of it (kamra.room_statistics), which rooms_occupied
	counts."""
	sellable = _sellable(property)
	start = getdate(date)
	end = getdate(add_days(start, days))
	from kamra.reservation_state import LIVE_STATUSES
	from kamra.room_statistics import (
		booked_by_night,
		category_rules,
		counted_occupied,
		occupancy_pct,
	)
	rules = category_rules(property)
	nights = booked_by_night(rules, property, start, end, LIVE_STATUSES, day_use=True)
	held_by_night = _held_by_night(property, start, end)
	pct = lambda n: occupancy_pct(n, sellable)  # noqa: E731
	out = []
	for i in range(days):
		d = getdate(add_days(start, i))
		by = nights.get(str(d)) or {}
		booked = sum(by.values())
		counted = counted_occupied(rules, booked, by)
		held = min(held_by_night.get(str(d), 0), max(0, sellable - booked))
		out.append({"date": str(d), "booked": booked, "rooms_occupied": counted, "held": held,
		            "available": max(0, sellable - booked - held),
		            "occupancy_pct": pct(counted), "occupancy_with_holds_pct": pct(counted + held)})
	return out


def _held_by_night(property: str, start, end) -> dict[str, int]:
	"""Group block rooms not yet picked up, per night, every room type: the
	same holds group_blocks keeps off sale (released and past-cutoff blocks
	no longer count)."""
	from kamra.group_blocks import _holding_groups, _unpicked
	out: dict[str, int] = {}
	for gb in _holding_groups(property, start, end):
		types = set(frappe.get_all("Group Room Block", filters={"parent": gb.name},
		                           pluck="room_type"))
		held = sum(_unpicked(gb.name, rt) for rt in types)
		if not held:
			continue
		d, co = max(getdate(gb.check_in_date), start), min(getdate(gb.check_out_date), end)
		while d < co:
			out[str(d)] = out.get(str(d), 0) + held
			d = getdate(add_days(d, 1))
	return out


COMPARE_METRICS = ("occupancy_pct", "rooms_sold", "room_revenue", "total_revenue", "adr", "revpar")


def _change(this, last):
	return round(100 * (this - last) / last, 1) if last else None


def comparison(property: str, date: str, total_rooms: int, forecast: list[dict]) -> dict:
	"""Today, month to date, year to date and the next 90 days on the books,
	each against the same dates one year back. Year to date starts at the
	country pack's financial year (April in India). The 90 days are the
	forecast's nights from tonight, over sellable rooms like the forecast."""
	from kamra.localization import financial_year_start, pack_for
	from kamra.reports import range_stats
	from kamra.room_statistics import occupancy_pct
	d = getdate(date)
	ly = lambda x: getdate(add_years(x, -1))  # noqa: E731 - 29 Feb -> 28 Feb
	fy = financial_year_start(pack_for(property), d)
	periods = []
	for pid, start, end in (("today", d, d), ("mtd", d.replace(day=1), d), ("ytd", fy, d)):
		this = range_stats(property, start, end, total_rooms)
		last = range_stats(property, ly(start), ly(end), total_rooms)
		periods.append({"id": pid, "from_date": str(start), "to_date": str(end),
		                "ly_from_date": str(ly(start)), "ly_to_date": str(ly(end)),
		                "this_year": this, "last_year": last,
		                "change_pct": {k: _change(this[k], last[k]) for k in COMPARE_METRICS}})
	# the future: rooms on the books against what last year actually sold
	if forecast:
		start, end = getdate(forecast[0]["date"]), getdate(forecast[-1]["date"])
		sellable = _sellable(property)
		sold = sum(x["booked"] for x in forecast)
		n = sellable * len(forecast)
		# occupancy by the same rule as last year's (range_stats)
		this = {"rooms_sold": sold,
		        "occupancy_pct": occupancy_pct(sum(x["rooms_occupied"] for x in forecast), n)}
		last = range_stats(property, ly(start), ly(end), sellable)
		periods.append({"id": "on_the_books", "rooms_basis": "sellable",
		                "from_date": str(start), "to_date": str(end),
		                "ly_from_date": str(ly(start)), "ly_to_date": str(ly(end)),
		                "this_year": this, "last_year": last,
		                "change_pct": {k: _change(this[k], last[k]) for k in this}})
	return {"financial_year_start": str(fy), "periods": periods}


def sees_figures() -> bool:
	return bool(set(FIGURE_ROLES) & set(frappe.get_roles()))


def _open_folio_totals(property: str) -> tuple[float, int]:
	"""What guests owe on open folios right now, and how many are open."""
	total, count = frappe.db.sql(
		"""SELECT COALESCE(SUM(balance), 0), COUNT(*) FROM `tabFolio`
		   WHERE property = %(p)s AND status = 'Open'""",
		{"p": property})[0]
	from kamra.folio import credit_note_positions, sales_invoice_field
	from kamra.ledger import _currency_places, _money, _round_currency

	if not count or not sales_invoice_field():
		return float(total or 0), int(count or 0)
	linked = frappe.get_all(
		"Folio", filters={"property": property, "status": "Open", "sales_invoice": ("is", "set"),
		                  "balance": (">", 0)},
		fields=["sales_invoice", "balance", "grand_total"],
	)
	adjusted = _money(total)
	for row, credit in zip(linked, credit_note_positions(linked, property), strict=True):
		if credit["credit_notes"]:
			adjusted += _money(credit["net_collectible_balance"]) - _money(row.balance)
	return _round_currency(adjusted, _currency_places(property)), int(count or 0)


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Revenue Manager", "Night Auditor", "Hotel Admin",
               "Kamra Agent")
def property_dashboard(property: str, date: str | None = None, with_comparison: int = 0):
	"""Everything one hotel's dashboard needs, by department. The year on
	year comparison is heavy, so only the Dashboard screen asks for it."""
	from kamra.crs import assert_property_access
	assert_property_access(property)
	from kamra.movements import report_dates
	dates = report_dates(property, date)
	date = dates["report_date"]
	hk = _housekeeping_slice(property)
	forecast = occupancy_forecast(property, date)
	summary = forecast_summary(forecast, _sellable(property))
	if not sees_figures():
		from kamra.reports import movement_counts
		move = movement_counts(property, date, date)
		return {
			"property": property,
			"property_name": frappe.db.get_value("Property", property, "property_name"),
			**dates,
			"figures_hidden": True,
			**move,
			"front_desk": move,
			"housekeeping": hk,
			"occupancy_now": occupancy_now(property, hk),
			"forecast": forecast,
			"forecast_summary": summary,
		}
	from kamra.reports import manager_flash
	flash = manager_flash(property, date)
	today = flash.get("today") or {}
	mtd = flash.get("mtd") or {}
	move = flash.get("movement") or {}
	finance = _finance_slice(property, date)

	return {
		"property": property,
		"property_name": frappe.db.get_value("Property", property, "property_name"),
		**dates,
		"total_rooms": flash.get("total_rooms", 0),
		# headline metrics
		"occupancy_pct": today.get("occupancy_pct", 0),
		"arrivals": move.get("arrivals", 0),
		"departures": move.get("departures", 0),
		"departures_overdue": move["departures_overdue"],
		"checked_out": move["checked_out"],
		"in_house": move.get("in_house", 0),
		"no_shows": move.get("no_shows", 0),
		# net of allowances (reports._day_stats)
		"revenue_today": round(today.get("total_revenue", 0), 0),
		# Room revenue ÷ rooms available today — same day as revenue_today.
		# (MTD RevPAR lives under statistics.revpar for the flash report.)
		"revpar": today.get("revpar", 0),
		"adr": today.get("adr", 0),
		"room_revenue_today": today.get("room_revenue", 0),
		# credits to guests, apart from the gross departments (reports._day_stats)
		"allowances_today": today.get("allowances", 0),
		"collections_today": finance["collections_today"],
		# statistics (month to date)
		"statistics": {
			"mtd_occupancy_pct": mtd.get("occupancy_pct", 0),
			"mtd_revenue": round(mtd.get("total_revenue", 0), 0),
			"adr": mtd.get("adr", 0),
			"revpar": mtd.get("revpar", 0),
			"rooms_sold_mtd": mtd.get("rooms_sold", 0),
		},
		# department dashboards
		"front_desk": move,
		"housekeeping": hk,
		"finance": finance,
		"outlook": flash.get("outlook", []),
		"occupancy_now": occupancy_now(property, hk),
		"forecast": forecast,
		"forecast_summary": summary,
		**({"comparison": comparison(property, date, flash.get("total_rooms", 0), forecast)}
		   if int(with_comparison or 0) else {}),
	}


def _whole(amount) -> float:
	"""Round money to whole units, half up - the way the portfolio shows it.
	Python's round() is half-to-even (11792.5 -> 11792) while the browser
	rounds half up (11,793), so a total rounded by one and a row by the other
	disagreed by a unit. Rows are rounded here once; totals add the rounded
	rows, so the tile is always the sum of the table."""
	return float(Decimal(str(amount or 0)).quantize(Decimal("1"), ROUND_HALF_UP))


def _property_summary(property: str, date: str):
	"""Compact headline numbers for one property in the portfolio table."""
	total_rooms = frappe.db.count("Room", {"property": property}) or 0
	from kamra.movements import departure_counts
	from kamra.reports import _day_stats
	s = _day_stats(property, date, total_rooms)
	finance = _finance_slice(property, date)
	return {
		"property": property,
		"property_name": frappe.db.get_value("Property", property, "property_name"),
		"total_rooms": total_rooms,
		"occupancy_pct": s.get("occupancy_pct", 0),
		"rooms_sold": s.get("rooms_sold", 0),
		# the rooms the occupancy counts, over the rooms not out of order
		# (reports._day_stats): what the portfolio totals add up
		"rooms_occupied": s.get("rooms_occupied", 0),
		"available_rooms": s.get("available_rooms", 0),
		"arrivals": frappe.db.count("Reservation", {
			"property": property, "check_in_date": date,
			"status": ("in", ["Confirmed", "Checked In"])}),
		**departure_counts(property, date, date),
		"in_house": frappe.db.count("Reservation", {
			"property": property, "status": "Checked In"}),
		"revenue_today": _whole(s.get("total_revenue", 0)),
		"collections_today": _whole(finance["collections_today"]),
		"outstanding": _whole(finance["outstanding"]),
	}


@frappe.whitelist()
@require_roles("Finance", "Revenue Manager", "Hotel Admin", "Kamra Agent")
def portfolio_dashboard(date: str | None = None):
	"""The chain's central view: headline metrics rolled up across every
	property the signed-in user may access, plus a per-property table."""
	from kamra.api import my_properties
	rows = []
	for prop in my_properties():
		day = date or prop["business_date"]
		rows.append({**_property_summary(prop["name"], day), "report_date": day})
	days = {row["report_date"] for row in rows}
	date = date or (days.pop() if len(days) == 1 else None)

	from kamra.room_statistics import occupancy_pct
	rooms = sum(r["total_rooms"] for r in rows)
	totals = {
		"properties": len(rows),
		"total_rooms": rooms,
		# the rows' own occupancy, added up: rooms occupied over rooms
		# available, each by its property's rate categories
		"occupancy_pct": occupancy_pct(sum(r["rooms_occupied"] for r in rows),
		                               sum(r["available_rooms"] for r in rows)),
		"arrivals": sum(r["arrivals"] for r in rows),
		"departures": sum(r["departures"] for r in rows),
		"in_house": sum(r["in_house"] for r in rows),
		# sums of the already-rounded rows: the tile equals the table
		"revenue_today": sum(r["revenue_today"] for r in rows),
		"collections_today": sum(r["collections_today"] for r in rows),
		"outstanding": sum(r["outstanding"] for r in rows),
	}
	rows.sort(key=lambda r: r["revenue_today"], reverse=True)
	return {"date": date, "totals": totals, "properties": rows}


@frappe.whitelist()
@require_roles("Finance", "Revenue Manager", "Night Auditor", "Kamra Agent")
def owner_snapshot(property: str, date: str | None = None, from_date: str | None = None,
                   period: str | None = None):
	"""The owner's phone view of one hotel: the flash for a day (or
	from_date..date), what guests and companies still owe (as of now), and
	the last few night audits. Read only; the same figures as the flash and
	the ledgers. `period` (today, yesterday, month) counts from the
	property's business date, never the phone's clock."""
	from inspect import unwrap

	from kamra.business_date import get_business_date
	from kamra.ledger import DUE_BUCKETS, city_ledger_aging
	from kamra.night_audit import AUDIT_ROLES, night_audit_history
	from kamra.reports import manager_flash

	if not date:
		date = get_business_date(property)
		if period == "yesterday":
			date = add_days(date, -1)
		elif period == "month":
			from_date = str(date)[:8] + "01"
	# this endpoint's gate stands in for the inner endpoints' own gates,
	# which predate the owner view and leave out some of its roles
	flash = unwrap(manager_flash)(property, str(date), from_date)
	outstanding, open_folios = _open_folio_totals(property)
	try:
		ar = unwrap(city_ledger_aging)(property)["totals"]
	except frappe.PermissionError:
		# the ERP keeps the receivables and answers no lookup: unknown, never
		# a zero or the hotel app's stale ledger. Handled here, so the screen
		# gets no "kept in the accounting system" pop-up for it.
		frappe.clear_last_message()
		ar = None
	sees_audits = (set(AUDIT_ROLES) | set(ADMIN)) & set(frappe.get_roles())
	from kamra.ledger import _currency_places, _round_currency
	places = _currency_places(property)
	return {
		**flash,
		"property": property,
		"property_name": frappe.db.get_value("Property", property, "property_name"),
		"guest_balances": outstanding,
		"open_folios": open_folios,
		"city_ledger": _round_currency(sum(ar[k] for k in DUE_BUCKETS), places) if ar else None,
		"city_ledger_overdue": ar["overdue"] if ar else None,
		"audits": unwrap(night_audit_history)(property, 5) if sees_audits else None,
	}
