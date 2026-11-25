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
Room (1000) and Rate Adjustment (1010). A room sold is one booking or shared physical room on one posting
date with a Room line that stands on that date
(folio.STANDS_ON_ITS_DATE).

A stay keeps the category it was booked on (Reservation.rate_category, set
when the stay is put on its plan): OPERA's rate code changes reach no
reservation already booked, and its End of Day statistics stay as the night
closed. Only the configuration privilege (CONFIGURES: the property's
administrators, never the approvers as such) changes a plan's category or
the category and approver settings, and each change is logged.
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
# a stay's category as it was booked (blank: booked before categories, the
# default)
STAY_CATEGORY = "IFNULL(r.rate_category, '')"
# a posted room night's category as it was posted (blank: pre-R3 line before
# backfill, falls back to the stay's category)
POSTED_CATEGORY = "COALESCE(NULLIF(fc.rate_category, ''), IFNULL(r.rate_category, ''))"


def can_configure() -> bool:
	"""The configuration privilege: who may change a plan's category, the
	categories and their approvers (OPERA keeps rate code configuration and
	comp authorization apart). The property's administrators
	(kamra.authz.ADMIN), whoever the approvers are."""
	from kamra.authz import ADMIN
	return frappe.session.user == "Administrator" or bool(set(ADMIN) & set(frappe.get_roles()))


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

	def counts(self, figure: str, value) -> bool:
		"""A stay on this category counts in `figure` (adr, revpar,
		occupancy)."""
		return self.of(value) not in self.out_of.get(figure, ())


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


def counted_occupied(rules: CategoryRules, occupied, by_category: dict | None) -> int:
	"""The rooms occupied that occupancy counts: `occupied` less the rooms of
	the categories left out of occupancy (by_category: {category: rooms
	occupied}). Every reader of occupancy counts its rooms with this."""
	out = sum(int(n or 0) for c, n in (by_category or {}).items()
	          if not rules.counts("occupancy", c))
	return max(0, int(occupied or 0) - out)


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
		return sum(v.get(what, 0) for v in rows)

	paid = rooms_sold - left_out("adr", "rooms")
	occupied = counted_occupied(rules, rooms_sold if rooms_occupied is None else rooms_occupied,
	                            occupied_by_category(by))
	revpar_rooms = occupancy_rooms if revpar_rooms is None else revpar_rooms
	return {
		# each category's rooms occupied: the nights posted, or the house in
		# house on an open date (with_occupied)
		"rooms_by_category": {c: int(occupied_by_category(by).get(c, 0))
		                      for c in rules.categories if c != rules.default},
		"occupancy_pct": occupancy_pct(occupied, occupancy_rooms),
		"adr": round((room_revenue - left_out("adr", "revenue")) / paid, 0) if paid > 0 else 0,
		"revpar": round((room_revenue - left_out("revpar", "revenue")) / revpar_rooms, 0)
		if revpar_rooms else 0,
	}


def occupied_by_category(by_category: dict | None) -> dict:
	"""{category: rooms occupied} out of a by_category ("occupied", else
	"rooms")."""
	return {c: v.get("occupied", v.get("rooms", 0)) for c, v in (by_category or {}).items()}


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
	# The physical room is stamped when a share posts, so breaking the share
	# or moving the guest later never changes a closed night's room count.
	key = """CONCAT(CASE WHEN IFNULL(fc.share_room, '') != ''
	                     THEN CONCAT('room:', fc.share_room)
	                     ELSE CONCAT('stay:', COALESCE(fc.reservation, f.reservation))
	                END, '|', fc.posting_date)"""
	by: dict = {}
	for category, rooms, revenue in frappe.db.sql(
			"""
			SELECT """ + POSTED_CATEGORY + """,
			  COUNT(DISTINCT CASE WHEN fc.charge_type = 'Room' AND """ + STANDS_ON_ITS_DATE + """
			        THEN """ + key + """ END),
			  COALESCE(SUM(fc.amount), 0)
			FROM `tabFolio Charge` fc
			JOIN `tabFolio` f ON fc.parent = f.name
			LEFT JOIN `tabReservation` r ON r.name = COALESCE(fc.reservation, f.reservation)
			WHERE f.property = %(p)s AND fc.posting_date BETWEEN %(s)s AND %(e)s
			  AND fc.charge_type IN %(types)s
			GROUP BY """ + POSTED_CATEGORY + """
			""",
			{"p": property, "s": str(start), "e": str(end), "types": ROOM_REVENUE_TYPES}):
		add_rooms(by, rules.of(category), rooms, revenue, rooms)
	return {"rooms_sold": sum(v["rooms"] for v in by.values()),
	        "room_revenue": sum(v["revenue"] for v in by.values()),
	        "by_category": by}


def posted_night_category(property: str | None, reservation: str | None, stay_date) -> str:
	"""The rate category stamped on the standing Room charge for
	(reservation, stay_date), falling back to the stay's current category."""
	rules = category_rules(property)
	if reservation and stay_date:
		from kamra.folio import STANDS_ON_ITS_DATE
		row = frappe.db.sql(
			"""
			SELECT NULLIF(fc.rate_category, '')
			FROM `tabFolio Charge` fc
			WHERE fc.reservation = %(r)s AND fc.charge_type = 'Room'
			  AND fc.posting_date = %(d)s AND """ + STANDS_ON_ITS_DATE + """
			ORDER BY fc.creation DESC LIMIT 1
			""", {"r": reservation, "d": str(stay_date)})
		if row and row[0][0]:
			return rules.of(row[0][0])
	cat = frappe.db.get_value("Reservation", reservation, "rate_category") if reservation else None
	return rules.of(cat)


def stamp_room_rate_category(folio_doc) -> None:
	"""Keep each saved room charge's posted rate category immutable, and stamp
	any newly added Room / Rate Adjustment line from the original posted night
	(for Rate Adjustment with stay_date) or the stay's current rate category."""
	old = None if folio_doc.is_new() else folio_doc.get_doc_before_save()
	old_by_name = {c.name: c for c in (old.charges if old else []) if c.name}
	rules = None
	stay_cache: dict[str, str] = {}
	for c in folio_doc.charges or []:
		prev = old_by_name.get(c.name) if c.name else None
		if prev is not None:
			if prev.get("rate_category"):
				c.rate_category = prev.rate_category
			continue
		if c.charge_type not in ROOM_REVENUE_TYPES or c.get("rate_category"):
			continue
		res_name = c.get("reservation") or folio_doc.get("reservation")
		if not res_name:
			continue
		if c.charge_type == "Rate Adjustment" and c.get("stay_date"):
			c.rate_category = posted_night_category(folio_doc.property, res_name, c.stay_date)
			continue
		if rules is None:
			rules = category_rules(folio_doc.property)
		if res_name not in stay_cache:
			stay_cache[res_name] = rules.of(
				frappe.db.get_value("Reservation", res_name, "rate_category"))
		c.rate_category = stay_cache[res_name]


def stay_categories(rules: CategoryRules, reservations) -> dict:
	"""Each of these stays' rate category, as booked: {reservation:
	category}."""
	if not reservations:
		return {}
	booked = dict(frappe.db.sql(
		"SELECT r.name, " + STAY_CATEGORY + " FROM `tabReservation` r WHERE r.name IN %(r)s",
		{"r": tuple(reservations)}))
	return {name: rules.of(booked.get(name)) for name in reservations}


def in_house_by_category(rules: CategoryRules, property: str, date=None) -> dict:
	"""Rooms in house by rate category, using the lead active sharer's category."""
	out: dict = {}
	seen = set()
	for room, group, category in frappe.db.sql(
			"""
			SELECT COALESCE(NULLIF(r.room, ''), r.name), r.share_group_id, """ + STAY_CATEGORY + """
			FROM `tabReservation` r
			WHERE r.property = %(p)s AND r.status = 'Checked In'
			  AND (%(d)s IS NULL OR r.check_in_date <= %(d)s)
			ORDER BY r.is_share_primary DESC, r.check_in_date, r.name
			""", {"p": property, "d": str(date) if date else None}):
		key = (room,) if group else (room, category)
		if key in seen:
			continue
		seen.add(key)
		category = rules.of(category)
		out[category] = out.get(category, 0) + 1
	return out


def with_occupied(by_category: dict, occupied: dict) -> dict:
	"""by_category with each category's rooms occupied replaced by
	`occupied` ({category: rooms}): the house in house, not the nights
	posted."""
	out = {c: {**v, "occupied": 0} for c, v in (by_category or {}).items()}
	for category, rooms in (occupied or {}).items():
		add_rooms(out, category, occupied=rooms)
	return out


def booked_by_night(rules: CategoryRules, property: str, start, end, statuses, *,
                    day_use: bool = False) -> dict:
	"""Rooms on the books each night from start to end (end excluded), by
	the stays' rate categories: {date: {category: rooms}}. A stay in one of
	`statuses` covers the nights from its arrival to the night before it
	leaves; with day_use a same-day stay covers its own date (as the
	occupancy forecast counts it)."""
	from frappe.utils import add_days, getdate
	from kamra.shares import inventory_key
	leaves = ("GREATEST(r.check_out_date, DATE_ADD(r.check_in_date, INTERVAL 1 DAY))"
	          if day_use else "r.check_out_date")
	start, end = getdate(start), getdate(end)
	out: dict = {}
	seen = set()
	for row in frappe.db.sql(
			"SELECT r.name, r.room, r.share_group_id, r.check_in_date, " + leaves +
			" AS leaves, " + STAY_CATEGORY + """ AS category
			FROM `tabReservation` r
			WHERE r.property = %(p)s AND r.status IN %(st)s
			  AND r.check_in_date < %(e)s AND """ + leaves + """ > %(s)s
			ORDER BY r.is_share_primary DESC, r.check_in_date, r.name""",
			{"p": property, "st": tuple(statuses), "s": str(start), "e": str(end)}, as_dict=True):
		category = rules.of(row.category)
		key = inventory_key(row)
		night = max(getdate(row.check_in_date), start)
		while night < min(getdate(row.leaves), end):
			if (night, key) not in seen:
				seen.add((night, key))
				by = out.setdefault(str(night), {})
				by[category] = by.get(category, 0) + 1
			night = getdate(add_days(night, 1))
	return out
