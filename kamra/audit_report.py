"""The night audit report: the day, month to date and year to date, side
by side.

Where each number comes from, so nothing is counted twice:
- rooms, meal plans, minibar and other charges: posted folio charges
- food and beverage: the restaurant bills themselves (POS Order), paid at
  the outlet or charged to a room, split per item by Menu Item.is_alcohol,
  for each outlet. A bill charged to a room is also on the guest's folio
  (its line ends "(ORD-...)"), so those folio lines are left out; other
  F&B on folios (an outside POS, a desk posting) is counted as is.
- banquets: the functions held (Venue Booking, Confirmed or Completed) by
  event date: food and beverage spend, hall rent, the rest. A function's
  lines posted to a folio start with its "EVT-..." name and are left out
  of the folio figures, so they count once.
- room revenue is Room and Rate Adjustment (kamra.room_statistics), the
  same revenue ADR and RevPAR divide; a discount posted on its own line (a
  booking voucher: Discount, booked to the Adjustment group) is with the
  allowances; the room levy is a tax collected for someone else, not
  revenue
- brought forward / carried forward: the Guest, Advance deposit and City
  ledger (AR) balances before and after the day, with the day's movement

The year starts where the country pack says (India: 1 April, the
financial year); everywhere else on 1 January.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, date_diff, flt, getdate

from kamra.authz import require_roles
from kamra.folio import NOT_VOIDED

FOLIO_SKIP = ("Opening Balance", "Room Levy")
FNB_TYPES = ("Food & Beverage",)
LEDGERS = (("Guest", "Guest ledger"), ("Deposit", "Advance deposits"), ("AR", "City ledger"))


# the folio's own lines in no other column: room revenue
# (room_statistics.ROOM_REVENUE_TYPES), meal plans and minibar, the
# allowances and discounts, food and beverage
NOT_OTHER = ("Room", "Rate Adjustment", "Discount", "Meal Plan", "Minibar", "Allowance")


def _folio(property: str, start, end) -> dict:
	row = frappe.db.sql(
		"""
		SELECT
		  COALESCE(SUM(CASE WHEN fc.charge_type IN ('Meal Plan', 'Minibar')
		        THEN fc.amount END), 0) AS meal_plan_minibar,
		  COALESCE(SUM(CASE WHEN fc.charge_type IN %(fnb)s AND fc.is_alcohol = 0
		        THEN fc.amount END), 0) AS food,
		  COALESCE(SUM(CASE WHEN fc.charge_type IN %(fnb)s AND fc.is_alcohol = 1
		        THEN fc.amount END), 0) AS beverage,
		  COALESCE(SUM(CASE WHEN fc.charge_type IN ('Allowance', 'Discount')
		        THEN fc.amount END), 0) AS allowances,
		  COALESCE(SUM(CASE WHEN fc.charge_type NOT IN %(not_other)s
		        AND fc.charge_type NOT IN %(fnb)s AND fc.charge_type NOT IN %(skip)s
		        THEN fc.amount END), 0) AS other
		""" + _FOLIO_FROM, {"p": property, "s": start, "e": end, "skip": FOLIO_SKIP,
		                     "fnb": FNB_TYPES, "not_other": NOT_OTHER}, as_dict=True)[0]
	return {k: flt(v) for k, v in row.items()}


# outlet bills and banquets are counted from their own records, so the folio
# lines they post are left out here
_FOLIO_FROM = """
		FROM `tabFolio Charge` fc JOIN `tabFolio` f ON fc.parent = f.name
		WHERE f.property = %(p)s AND fc.posting_date BETWEEN %(s)s AND %(e)s
		  AND IFNULL(fc.description, '') NOT LIKE '%%(ORD-%%'
		  AND IFNULL(fc.description, '') NOT LIKE 'EVT-%%'
"""


def departments(property: str, start, end) -> list[dict]:
	"""Revenue by operating department for a date range: rooms, each outlet,
	banquets, then each minor department (laundry, spa, taxi...) on its own
	line. Same figures as the night audit report, which sums the minor ones
	as "other"."""
	from kamra.ledger import _currency_places, _round_currency
	places = _currency_places(property)
	p = _period(property, "", start, end, 0)
	outlets = sum(o["total"] for o in p["outlets"])
	rows = [("Rooms", p["room_revenue"])]
	rows += [(o["name"], o["total"]) for o in p["outlets"]]
	rows += [("Food and beverage on folios", p["food"] + p["beverage"] - outlets),
	         ("Banquets", p["banquets"]),
	         ("Meal plans and minibar", p["meal_plan_minibar"])]
	rows += frappe.db.sql(
		"""SELECT fc.charge_type, SUM(fc.amount)""" + _FOLIO_FROM + """
		  AND fc.charge_type NOT IN %(not_other)s
		  AND fc.charge_type NOT IN %(fnb)s AND fc.charge_type NOT IN %(skip)s
		GROUP BY fc.charge_type ORDER BY fc.charge_type""",
		{"p": property, "s": start, "e": end, "skip": FOLIO_SKIP, "fnb": FNB_TYPES,
		 "not_other": NOT_OTHER})
	rows.append(("Discounts and allowances", p["allowances"]))
	return [{"department": frappe._(name), "amount": _round_currency(flt(amt), places)}
	        for name, amt in rows if name == "Rooms" or _round_currency(flt(amt), places)]


def _outlets(property: str, start, end) -> list[dict]:
	"""Each outlet's food and beverage sales (before tax, after discount)."""
	from kamra.ledger import _currency_places, _round_currency
	places = _currency_places(property)
	rows = frappe.db.sql(
		"""
		SELECT o.outlet, o.name AS bill, o.subtotal, o.discount_amount, o.paid,
		       COALESCE(SUM(CASE WHEN mi.is_alcohol = 1 THEN i.amount END), 0) AS bev,
		       COALESCE(SUM(CASE WHEN IFNULL(mi.is_alcohol, 0) = 0 THEN i.amount END), 0) AS food
		FROM `tabPOS Order` o
		JOIN `tabPOS Order Item` i ON i.parent = o.name AND i.voided = 0
		LEFT JOIN `tabMenu Item` mi ON mi.name = i.menu_item
		WHERE o.property = %(p)s AND o.status = 'Delivered' AND o.nc = 0
		  AND COALESCE(o.business_date, DATE(o.creation)) BETWEEN %(s)s AND %(e)s
		GROUP BY o.name
		""", {"p": property, "s": start, "e": end}, as_dict=True)
	# a bill charged to a room is revenue only while its charge stands on a
	# folio: a void at the desk reverses the line (a transfer keeps the order
	# number in it) but leaves the order Delivered, so it counted as sold
	to_room = [r.bill for r in rows if not r.paid]
	on_folio = {o for (o,) in frappe.db.sql(
		"""SELECT o.name FROM `tabPOS Order` o WHERE o.name IN %(b)s AND EXISTS (
		       SELECT 1 FROM `tabFolio Charge` fc WHERE fc.parenttype = 'Folio'
		         AND fc.description LIKE CONCAT('%%(', o.name, ')%%')
		         AND """ + NOT_VOIDED + """)""",
		{"b": tuple(to_room)})} if to_room else set()
	out: dict[str, dict] = {}
	for r in rows:
		if not r.paid and r.bill not in on_folio:
			continue
		gross = flt(r.food) + flt(r.bev)
		keep = 1 - (flt(r.discount_amount) / gross) if gross else 0
		o = out.setdefault(r.outlet, {"outlet": r.outlet, "bills": 0, "food": 0.0,
		                              "beverage": 0.0, "to_rooms": 0.0})
		o["bills"] += 1
		o["food"] += flt(r.food) * keep
		o["beverage"] += flt(r.bev) * keep
		if not r.paid:
			o["to_rooms"] += gross * keep
	names = dict(frappe.get_all("POS Outlet", filters={"name": ("in", list(out) or [""])},
	                            fields=["name", "outlet_name"], as_list=True))
	for o in out.values():
		o["name"] = names.get(o["outlet"]) or o["outlet"]
		o["total"] = o["food"] + o["beverage"]
		for k in ("food", "beverage", "to_rooms", "total"):
			o[k] = _round_currency(o[k], places)
	return sorted(out.values(), key=lambda o: o["name"])


def _banquets(property: str, start, end) -> dict:
	row = frappe.db.sql(
		"""
		SELECT COUNT(*) AS functions, COALESCE(SUM(fnb_spend), 0) AS fnb,
		       COALESCE(SUM(venue_rental), 0) AS hall,
		       COALESCE(SUM(taxable_amount), 0) AS total
		FROM `tabVenue Booking`
		WHERE property = %(p)s AND status IN ('Confirmed', 'Completed')
		  AND event_date BETWEEN %(s)s AND %(e)s
		""", {"p": property, "s": start, "e": end}, as_dict=True)[0]
	fnb, hall, total = flt(row.fnb), flt(row.hall), flt(row.total)
	return {"functions": int(row.functions or 0), "fnb": fnb, "hall": hall,
	        "other": max(0.0, total - fnb - hall), "total": total}


def _period(property: str, label: str, start, end, total_rooms: int) -> dict:
	from kamra.ledger import _currency_places, _round_currency
	from kamra.room_statistics import category_rules, posted_rooms, room_figures
	places = _currency_places(property)
	f = _folio(property, start, end)
	rules = category_rules(property)
	rooms = posted_rooms(property, start, end, rules)
	outlets = _outlets(property, start, end)
	banq = _banquets(property, start, end)
	# the restaurants' bills, plus F&B posted on folios some other way
	food = sum(o["food"] for o in outlets) + f["food"]
	bev = sum(o["beverage"] for o in outlets) + f["beverage"]
	nights = rooms["rooms_sold"]
	available = total_rooms * (date_diff(end, start) + 1)
	# ADR, RevPAR and occupancy as every report counts them, by the
	# property's rate categories (kamra.room_statistics)
	figures = room_figures(rules, room_revenue=rooms["room_revenue"], rooms_sold=nights,
	                       by_category=rooms["by_category"], occupancy_rooms=available)
	# discounts and allowances given after a charge (negative) get their own
	# column: inside "other" they made it negative and hid the other income
	total = (rooms["room_revenue"] + food + bev + banq["total"] + f["meal_plan_minibar"]
	         + f["other"] + f["allowances"])
	return {
		"label": label, "from_date": str(start), "to_date": str(end),
		"rooms_sold": nights,
		# the room revenue ADR and RevPAR divide (kamra.room_statistics)
		"room_revenue": rooms["room_revenue"],
		# rooms_by_category, occupancy_pct, adr, revpar
		**figures,
		"food": _round_currency(food, places), "beverage": _round_currency(bev, places),
		"banquets": banq["total"], "meal_plan_minibar": f["meal_plan_minibar"],
		"other": f["other"], "allowances": f["allowances"],
		"total_revenue": _round_currency(total, places),
		"outlets": outlets, "banquet": banq,
	}


def _balances(property: str, d) -> list[dict]:
	from kamra.business_date import get_business_date
	from kamra.erp_books import receivables_source
	from kamra.ledger import _currency_places, _round_currency
	from kamra.ledger import ledger_balances as guarded
	from kamra.night_audit import _raw
	# the report's own role check has run; the ledger endpoint's is narrower
	ledger_balances = _raw(guarded)
	places = _currency_places(property)
	before = {r["ledger"]: r for r in ledger_balances(property, str(add_days(d, -1)))["ledgers"]}
	after = {r["ledger"]: r for r in ledger_balances(property, str(d))["ledgers"]}
	src = receivables_source(property, strict=False)
	if not src.in_erp:
		# the city ledger is what companies owe: City Ledger Entry, as the
		# Receivables screen and the credit limit check read it, so a company
		# payment not yet applied to a folio counts too
		before["AR"], after["AR"] = _city_ledger(property, add_days(d, -1)), _city_ledger(property, d)
	out = []
	for key, label in LEDGERS:
		if key == "AR" and src.in_erp:
			# While the ERP keeps receivables, never read stale City Ledger Entry.
			# Only the live balance for the current business date is known here.
			if src.unavailable:
				carried = None
			elif str(getdate(d)) == str(getdate(get_business_date(property))):
				carried = _round_currency(sum(flt(r["balance"]) for r in src.rows), places)
			else:
				carried = None
			out.append({
				"ledger": key, "label": label,
				"brought_forward": None, "debit": None, "credit": None,
				"carried_forward": carried,
				"kept_in_erp": True, "unverified": src.unavailable,
			})
		else:
			out.append({
				"ledger": key, "label": label,
				"brought_forward": _round_currency(before[key]["balance"], places),
				"debit": _round_currency(after[key]["debit"] - before[key]["debit"], places),
				"credit": _round_currency(after[key]["credit"] - before[key]["credit"], places),
				"carried_forward": _round_currency(after[key]["balance"], places),
			})
	return out


def _city_ledger(property: str, d) -> dict:
	debit, credit = frappe.db.sql(
		"""SELECT COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0)
		   FROM `tabCity Ledger Entry` WHERE property = %s AND posting_date <= %s""",
		(property, str(d)))[0]
	return {"debit": flt(debit), "credit": flt(credit), "balance": flt(debit) - flt(credit)}


def build(property: str, d) -> dict:
	from kamra.localization import financial_year_start, pack_for
	d = getdate(d)
	total_rooms = frappe.db.count("Room", {"property": property}) or 0
	return {
		"business_date": str(d),
		"total_rooms": total_rooms,
		"periods": [
			_period(property, "Day", d, d, total_rooms),
			_period(property, "Month to date", d.replace(day=1), d, total_rooms),
			_period(property, "Year to date", financial_year_start(pack_for(property), d), d,
			        total_rooms),
		],
		"balances": _balances(property, d),
	}


@frappe.whitelist()
@require_roles("Finance", "Revenue Manager", "Night Auditor", "Kamra Agent")
def night_audit_report(property: str, date: str | None = None):
	"""The night audit report for a business date (default: the last one
	closed): rooms, food and beverage separately, each outlet, banquets,
	meal plans and minibar, other income, for the day, month to date and
	year to date, plus each ledger's balance brought and carried forward."""
	from kamra.authz import assert_property_access
	from kamra.business_date import get_business_date
	assert_property_access(property)
	return build(property, date or add_days(get_business_date(property), -1))
