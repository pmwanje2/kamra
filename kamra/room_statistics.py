"""Room statistics: rooms sold, complimentary and house-use rooms, ADR,
RevPAR and occupancy. Every report computes them here, so the same posted
nights give the same figures in the flash, the night audit report, the owner's
briefing, the dashboards, the budget and the custom reports.

The rules follow OPERA's Manager Report:
- A complimentary or house-use room is a room sold on a rate plan whose
  rate_category is Complimentary or House Use (OPERA's rate code
  Complimentary and House Use flags).
- ADR is room revenue over the rooms sold minus comp and house use ("ADR
  minus Comp and House").
- RevPAR is room revenue over the rooms.
- Occupancy counts comp and house-use rooms, because they are occupied.

Room revenue is what kamra.ledger books to the Rooms group's room-rate codes:
Room (1000) and Rate Adjustment (1010). A room sold is one reservation on one
posting date with a Room line that stands on that date
(folio.STANDS_ON_ITS_DATE).
"""

from __future__ import annotations

import frappe

COMPLIMENTARY = "Complimentary"
HOUSE_USE = "House Use"
# the rate categories whose rooms are left out of ADR
COMP_CATEGORIES = (COMPLIMENTARY, HOUSE_USE)
ROOM_REVENUE_TYPES = ("Room", "Rate Adjustment")


def rate_category(rate_plan: str | None) -> str:
	"""A rate plan's category; no plan, or a plan from before the field, is
	Standard."""
	value = frappe.db.get_value("Rate Plan", rate_plan, "rate_category") if rate_plan else None
	return value or "Standard"


def occupancy_pct(occupied, rooms) -> float:
	"""Rooms occupied over rooms, in % to one decimal."""
	return round(100 * occupied / rooms, 1) if rooms else 0


def room_figures(*, room_revenue, rooms_sold, comp_rooms=0, house_use_rooms=0,
                 rooms_occupied=None, occupancy_rooms=0, revpar_rooms=None) -> dict:
	"""ADR, RevPAR and occupancy from a period's room revenue and rooms.

	rooms_occupied defaults to the rooms sold (a closed date). The occupied
	rooms are divided by occupancy_rooms, and room revenue by revpar_rooms
	(default occupancy_rooms). Each report passes its own denominators: the
	rooms not out of order, every room, or the room nights elapsed."""
	paid = rooms_sold - comp_rooms - house_use_rooms
	occupied = rooms_sold if rooms_occupied is None else rooms_occupied
	revpar_rooms = occupancy_rooms if revpar_rooms is None else revpar_rooms
	return {
		"comp_rooms": comp_rooms,
		"house_use_rooms": house_use_rooms,
		"occupancy_pct": occupancy_pct(occupied, occupancy_rooms),
		"adr": round(room_revenue / paid, 0) if paid > 0 else 0,
		"revpar": round(room_revenue / revpar_rooms, 0) if revpar_rooms else 0,
	}


def posted_rooms(property: str, start, end) -> dict:
	"""Rooms sold, complimentary and house-use rooms and room revenue posted
	from start to end (inclusive), in one query."""
	from kamra.folio import STANDS_ON_ITS_DATE

	sold = ("fc.charge_type = 'Room' AND " + STANDS_ON_ITS_DATE)
	key = "CONCAT(COALESCE(fc.reservation, f.reservation), '|', fc.posting_date)"
	row = frappe.db.sql(
		"""
		SELECT
		  COUNT(DISTINCT CASE WHEN """ + sold + """ THEN """ + key + """ END) AS rooms_sold,
		  COUNT(DISTINCT CASE WHEN """ + sold + """ AND rp.rate_category = %(comp)s
		        THEN """ + key + """ END) AS comp_rooms,
		  COUNT(DISTINCT CASE WHEN """ + sold + """ AND rp.rate_category = %(house)s
		        THEN """ + key + """ END) AS house_use_rooms,
		  COALESCE(SUM(fc.amount), 0) AS room_revenue
		FROM `tabFolio Charge` fc
		JOIN `tabFolio` f ON fc.parent = f.name
		LEFT JOIN `tabReservation` r ON r.name = COALESCE(fc.reservation, f.reservation)
		LEFT JOIN `tabRate Plan` rp ON rp.name = r.rate_plan
		WHERE f.property = %(p)s AND fc.posting_date BETWEEN %(s)s AND %(e)s
		  AND fc.charge_type IN %(types)s
		""",
		{"p": property, "s": str(start), "e": str(end), "comp": COMPLIMENTARY,
		 "house": HOUSE_USE, "types": ROOM_REVENUE_TYPES}, as_dict=True)[0]
	return {"rooms_sold": int(row.rooms_sold or 0), "comp_rooms": int(row.comp_rooms or 0),
	        "house_use_rooms": int(row.house_use_rooms or 0),
	        "room_revenue": float(row.room_revenue or 0)}


def stay_categories(reservations) -> dict:
	"""How many of these stays are on a Complimentary or House Use plan:
	{"comp_rooms", "house_use_rooms"}."""
	counts = {COMPLIMENTARY: 0, HOUSE_USE: 0}
	if reservations:
		for category, n in frappe.db.sql(
				"""
				SELECT rp.rate_category, COUNT(*) FROM `tabReservation` r
				JOIN `tabRate Plan` rp ON rp.name = r.rate_plan
				WHERE r.name IN %(r)s AND rp.rate_category IN %(c)s
				GROUP BY rp.rate_category
				""", {"r": tuple(reservations), "c": COMP_CATEGORIES}):
			counts[category] = int(n or 0)
	return {"comp_rooms": counts[COMPLIMENTARY], "house_use_rooms": counts[HOUSE_USE]}
