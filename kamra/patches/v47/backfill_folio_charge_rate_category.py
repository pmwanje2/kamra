"""Stamp Folio Charge.rate_category on existing Room and Rate Adjustment lines.

OPERA End of Day statistics never restate closed nights when a stay changes
rate plan mid-stay. New room charges stamp their rate category at posting time;
this idempotent patch backfills already-posted nights from each stay's current
Reservation.rate_category (or the property's configured default category when
blank). Already-stamped rows are never touched.
"""

from __future__ import annotations

import frappe

from kamra.room_statistics import ROOM_REVENUE_TYPES, category_rules


def execute():
	if not frappe.db.has_column("Folio Charge", "rate_category"):
		return
	props = [
		row[0] for row in frappe.db.sql(
			"""
			SELECT DISTINCT f.property
			FROM `tabFolio Charge` fc
			JOIN `tabFolio` f ON f.name = fc.parent
			WHERE fc.charge_type IN %(types)s
			  AND IFNULL(fc.rate_category, '') = ''
			  AND f.property IS NOT NULL AND f.property != ''
			""", {"types": ROOM_REVENUE_TYPES})
	]
	for prop in props:
		default = category_rules(prop).default
		frappe.db.sql(
			"""
			UPDATE `tabFolio Charge` fc
			JOIN `tabFolio` f ON f.name = fc.parent
			LEFT JOIN `tabReservation` r ON r.name = COALESCE(fc.reservation, f.reservation)
			SET fc.rate_category = COALESCE(NULLIF(r.rate_category, ''), %(default)s)
			WHERE f.property = %(prop)s
			  AND fc.charge_type IN %(types)s
			  AND IFNULL(fc.rate_category, '') = ''
			""", {"prop": prop, "types": ROOM_REVENUE_TYPES, "default": default})
