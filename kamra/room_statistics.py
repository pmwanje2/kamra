"""Room statistics: rooms sold, rooms by rate category, ADR, RevPAR and
occupancy. Every report computes them here, so the same posted nights give
the same figures in the flash, the night audit report, the owner's briefing,
the dashboards, the budget and the custom reports.

Each rate plan has a category from the property's own list (Settings →
Revenue → Complimentary and house use: rate_categories), and each category
says what it is left out of. The list is seeded with OPERA's rate code
Complimentary and House Use flags and its Manager Report:
- ADR is room revenue over the rooms sold minus comp and house use ("ADR
  minus Comp and House");
- RevPAR is room revenue over the rooms;
- occupancy counts comp and house-use rooms, because they are occupied.
A category left out of a figure adds neither rooms nor revenue to it.

Room revenue is what kamra.ledger books to the Rooms group's room-rate codes:
Room (1000) and Rate Adjustment (1010). A room sold is one reservation on one
posting date with a Room line that stands on that date
(folio.STANDS_ON_ITS_DATE).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import frappe

ROOM_REVENUE_TYPES = ("Room", "Rate Adjustment")
# the figures a category can be left out of
FIGURES = ("adr", "revpar", "occupancy")
# the words a rate_categories line may carry after its category
FLAGS = {"sign-off": "sign_off", "sign off": "sign_off", "no adr": "adr",
         "no revpar": "revpar", "no occupancy": "occupancy"}


@dataclass(frozen=True)
class CategoryRules:
	"""A property's rate categories (the first is a plan's when it names
	none), the ones a booking needs a sign-off on, what each is left out of
	and the roles that sign them off."""

	categories: tuple
	sign_off: frozenset
	out_of: dict = field(default_factory=dict)
	approvers: tuple = ()

	@property
	def default(self) -> str:
		return self.categories[0]

	def canonical(self, value) -> str | None:
		"""The category as the list spells it (blank: the default), or None
		when the property lists no such category."""
		text = " ".join(str(value or "").split())
		if not text:
			return self.default
		return next((c for c in self.categories if c.casefold() == text.casefold()), None)

	def of(self, value) -> str:
		"""A plan's category for the statistics: one the list no longer
		names counts as the default."""
		return self.canonical(value) or self.default


def _seeded(key: str) -> str:
	from kamra.settings import SETTINGS
	return SETTINGS[key][1].default


def parse_rules(raw, approvers=None) -> CategoryRules:
	"""rate_categories (one category a line: "House Use: sign-off, no ADR")
	and rate_category_approvers (one role a line), blank for OPERA's
	defaults. A category listed twice keeps its first line; a word that is
	not a rule is ignored."""
	from kamra.settings import parse_list
	if raw is None or not str(raw).strip():
		raw = _seeded("rate_categories")
	categories, seen, sign_off = [], set(), set()
	out_of: dict[str, set] = {f: set() for f in FIGURES}
	for line in str(raw).splitlines():
		head, _, rules = line.partition(":")
		name = " ".join(head.split())
		if not name or name.casefold() in seen:
			continue
		seen.add(name.casefold())
		categories.append(name)
		for word in rules.split(","):
			rule = FLAGS.get(" ".join(word.split()).casefold())
			if rule == "sign_off":
				sign_off.add(name)
			elif rule:
				out_of[rule].add(name)
	if not categories:
		return parse_rules(None, approvers)
	if approvers is None or not str(approvers).strip():
		approvers = _seeded("rate_category_approvers")
	return CategoryRules(tuple(categories), frozenset(sign_off),
	                     {f: frozenset(v) for f, v in out_of.items()}, tuple(parse_list(approvers)))


def category_rules(property: str | None) -> CategoryRules:
	"""The property's rate category rules (Settings → Revenue)."""
	from kamra.settings import get_setting
	return parse_rules(get_setting(property, "rate_categories"),
	                   get_setting(property, "rate_category_approvers"))


def rate_category(property: str | None, rate_plan: str | None) -> str:
	"""A rate plan's category at its property; no plan is the default."""
	rules = category_rules(property)
	value = frappe.db.get_value("Rate Plan", rate_plan, "rate_category") if rate_plan else None
	return rules.of(value)


def occupancy_pct(occupied, rooms) -> float:
	"""Rooms occupied over rooms, in % to one decimal."""
	return round(100 * occupied / rooms, 1) if rooms else 0


def room_figures(rules: CategoryRules, *, room_revenue, rooms_sold, by_category=None,
                 rooms_occupied=None, occupancy_rooms=0, revpar_rooms=None) -> dict:
	"""ADR, RevPAR, occupancy and rooms by category from a period's room
	revenue and rooms.

	by_category: {category: {"rooms", "revenue", "occupied"}} ("occupied"
	defaults to "rooms"). rooms_occupied defaults to the rooms sold (a closed
	date). The occupied rooms are divided by occupancy_rooms, and room
	revenue by revpar_rooms (default occupancy_rooms). Each report passes its
	own denominators: the rooms not out of order, every room, or the room
	nights elapsed."""
	by = by_category or {}

	def left_out(figure: str, what: str) -> float:
		rows = [v for c, v in by.items() if c in rules.out_of.get(figure, ())]
		if what == "occupied":
			return sum(v.get("occupied", v.get("rooms", 0)) for v in rows)
		return sum(v.get(what, 0) for v in rows)

	paid = rooms_sold - left_out("adr", "rooms")
	occupied = (rooms_sold if rooms_occupied is None else rooms_occupied) - left_out(
		"occupancy", "occupied")
	revpar_rooms = occupancy_rooms if revpar_rooms is None else revpar_rooms
	return {
		"rooms_by_category": {c: int((by.get(c) or {}).get("rooms", 0))
		                      for c in rules.categories if c != rules.default},
		"occupancy_pct": occupancy_pct(occupied, occupancy_rooms),
		"adr": round((room_revenue - left_out("adr", "revenue")) / paid, 0) if paid > 0 else 0,
		"revpar": round((room_revenue - left_out("revpar", "revenue")) / revpar_rooms, 0)
		if revpar_rooms else 0,
	}


def add_rooms(by: dict, category: str, rooms=0, revenue=0.0, occupied=0) -> None:
	row = by.setdefault(category, {"rooms": 0, "revenue": 0.0, "occupied": 0})
	row["rooms"] += int(rooms or 0)
	row["revenue"] += float(revenue or 0)
	row["occupied"] += int(occupied or 0)


def sum_categories(rows) -> dict:
	"""Several periods' by_category as one."""
	out: dict = {}
	for by in rows:
		for category, v in (by or {}).items():
			add_rooms(out, category, v.get("rooms"), v.get("revenue"), v.get("occupied"))
	return out


def posted_rooms(property: str, start, end, rules: CategoryRules | None = None) -> dict:
	"""Rooms sold and room revenue posted from start to end (inclusive), in
	total and by rate category, in one query. On a closed date a room sold is
	a room occupied, so each category's "occupied" is its rooms."""
	from kamra.folio import STANDS_ON_ITS_DATE
	rules = rules or category_rules(property)
	key = "CONCAT(COALESCE(fc.reservation, f.reservation), '|', fc.posting_date)"
	by: dict = {}
	for category, rooms, revenue in frappe.db.sql(
			"""
			SELECT IFNULL(rp.rate_category, ''),
			  COUNT(DISTINCT CASE WHEN fc.charge_type = 'Room' AND """ + STANDS_ON_ITS_DATE + """
			        THEN """ + key + """ END),
			  COALESCE(SUM(fc.amount), 0)
			FROM `tabFolio Charge` fc
			JOIN `tabFolio` f ON fc.parent = f.name
			LEFT JOIN `tabReservation` r ON r.name = COALESCE(fc.reservation, f.reservation)
			LEFT JOIN `tabRate Plan` rp ON rp.name = r.rate_plan
			WHERE f.property = %(p)s AND fc.posting_date BETWEEN %(s)s AND %(e)s
			  AND fc.charge_type IN %(types)s
			GROUP BY IFNULL(rp.rate_category, '')
			""",
			{"p": property, "s": str(start), "e": str(end), "types": ROOM_REVENUE_TYPES}):
		add_rooms(by, rules.of(category), rooms, revenue, rooms)
	return {"rooms_sold": sum(v["rooms"] for v in by.values()),
	        "room_revenue": sum(v["revenue"] for v in by.values()),
	        "by_category": by}


def stay_categories(rules: CategoryRules, reservations) -> dict:
	"""Each of these stays' rate category: {reservation: category}."""
	if not reservations:
		return {}
	plans = dict(frappe.db.sql(
		"""
		SELECT r.name, IFNULL(rp.rate_category, '') FROM `tabReservation` r
		LEFT JOIN `tabRate Plan` rp ON rp.name = r.rate_plan
		WHERE r.name IN %(r)s
		""", {"r": tuple(reservations)}))
	return {name: rules.of(plans.get(name)) for name in reservations}


def in_house_by_category(rules: CategoryRules, property: str, date=None) -> dict:
	"""Rooms in house (stays Checked In, arrived by date) by rate category:
	{category: rooms}."""
	out: dict = {}
	for category, rooms in frappe.db.sql(
			"""
			SELECT IFNULL(rp.rate_category, ''),
			       COUNT(DISTINCT COALESCE(NULLIF(r.room, ''), r.name))
			FROM `tabReservation` r
			LEFT JOIN `tabRate Plan` rp ON rp.name = r.rate_plan
			WHERE r.property = %(p)s AND r.status = 'Checked In'
			  AND (%(d)s IS NULL OR r.check_in_date <= %(d)s)
			GROUP BY IFNULL(rp.rate_category, '')
			""", {"p": property, "d": str(date) if date else None}):
		category = rules.of(category)
		out[category] = out.get(category, 0) + int(rooms or 0)
	return out


def with_occupied(by_category: dict, occupied: dict) -> dict:
	"""by_category with each category's rooms occupied replaced by
	`occupied` ({category: rooms}): the house in house, not the nights
	posted."""
	out = {c: {**v, "occupied": 0} for c, v in (by_category or {}).items()}
	for category, rooms in (occupied or {}).items():
		add_rooms(out, category, occupied=rooms)
	return out
