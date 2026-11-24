"""Stamp Folio Charge.rate_category on existing Room and Rate Adjustment lines.

OPERA End of Day statistics never restate closed nights when a stay changes
rate plan mid-stay. New room charges stamp their rate category at posting time;
this idempotent patch backfills the already-posted blank ones. Each line takes
the first of these that answers, and already-stamped rows are never touched:

1. a reversal: the category of the line it reverses;
2. the stay's same-date Room line, when it is stamped (a Rate Adjustment of a
   night, or any line of a night already posted);
3. the stay's Reservation.rate_category;
4. the Rate Plan.rate_category of the stay's plan, for a stay booked before the
   category was kept on the reservation (one the property no longer lists
   counts as its default, as the statistics read it);
5. the property's default category.

Steps 3 to 5 read the stay as it is today. A stay whose rate plan was changed
(Amend stay logs it) may have posted a line before the change, and nothing
dates a line itself (a child row carries its folio's creation), so those
lines are stamped the same way and logged, their count and names, for the
hotel to review: never guessed silently.
"""

from __future__ import annotations

import frappe

from kamra.folio import STANDS_ON_ITS_DATE
from kamra.room_statistics import ROOM_REVENUE_TYPES, category_rules

_CHUNK = 2000


def _chunks(names):
	names = sorted(names)
	for i in range(0, len(names), _CHUNK):
		yield tuple(names[i:i + _CHUNK])


def _switched_stays(stays) -> set:
	"""The stays whose rate plan Amend stay changed."""
	out: set = set()
	for chunk in _chunks(stays):
		out.update(name for (name,) in frappe.db.sql(
			"""
			SELECT DISTINCT reference_name FROM `tabAgent Action Log`
			WHERE action_type = 'amend_stay' AND reference_name IN %(r)s
			  AND rationale LIKE '%%rate plan %%→%%'
			""", {"r": chunk}))
	return out


def execute():
	if not frappe.db.has_column("Folio Charge", "rate_category"):
		return
	rows = frappe.db.sql(
		"""
		SELECT fc.name, fc.posting_date, fc.stay_date, fc.reversal_of,
		       f.property, COALESCE(NULLIF(fc.reservation, ''), f.reservation) AS res
		FROM `tabFolio Charge` fc
		JOIN `tabFolio` f ON f.name = fc.parent
		WHERE fc.charge_type IN %(types)s AND IFNULL(fc.rate_category, '') = ''
		  AND IFNULL(f.property, '') != ''
		ORDER BY IFNULL(fc.reversal_of, '') != '', fc.creation
		""", {"types": ROOM_REVENUE_TYPES}, as_dict=True)
	if not rows:
		return
	stays = {r.res for r in rows if r.res}

	booked: dict = {}  # reservation -> (its rate_category, its plan's)
	night: dict = {}  # (reservation, date) -> the categories of its stamped Room lines
	for chunk in _chunks(stays):
		for name, own, plan in frappe.db.sql(
			"""
			SELECT r.name, IFNULL(r.rate_category, ''), IFNULL(rp.rate_category, '')
			FROM `tabReservation` r LEFT JOIN `tabRate Plan` rp ON rp.name = r.rate_plan
			WHERE r.name IN %(r)s
			""", {"r": chunk}):
			booked[name] = (own, plan)
		for name, day, category in frappe.db.sql(
			"""
			SELECT COALESCE(NULLIF(fc.reservation, ''), f.reservation), fc.posting_date, fc.rate_category
			FROM `tabFolio Charge` fc JOIN `tabFolio` f ON f.name = fc.parent
			WHERE COALESCE(NULLIF(fc.reservation, ''), f.reservation) IN %(r)s
			  AND fc.charge_type = 'Room' AND IFNULL(fc.rate_category, '') != ''
			  AND """ + STANDS_ON_ITS_DATE, {"r": chunk}):
			night.setdefault((name, str(day)), set()).add(category)
	switched = _switched_stays(stays)

	stamped: dict = {}  # the category a line was given by this run
	rules: dict = {}
	review = []
	for row in rows:
		category = None
		if row.reversal_of:
			category = stamped.get(row.reversal_of) or frappe.db.get_value(
				"Folio Charge", row.reversal_of, "rate_category")
		if not category:
			same_day = night.get((row.res, str(row.stay_date or row.posting_date)), set())
			if len(same_day) == 1:
				category = next(iter(same_day))
		if not category:
			own, plan = booked.get(row.res, ("", ""))
			# the plan's (or, with none, the default) as the statistics read it
			category = own or rules.setdefault(
				row.property, category_rules(row.property)).of(plan)
			if row.res in switched:
				review.append(f"{row.name} ({row.res})")
		frappe.db.set_value("Folio Charge", row.name, "rate_category", category, update_modified=False)
		stamped[row.name] = category
	if review:
		frappe.log_error(
			title="Folio Charge rate category backfill: lines of stays whose rate plan changed",
			message=(f"{len(review)} charge(s) were stamped from their stay's present rate category; "
			         "its rate plan was changed after the night may have been posted, and nothing "
			         "dates the line itself. Review: " + ", ".join(review)))
