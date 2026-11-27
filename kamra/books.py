"""Management books: the next few days of cash, a management P&L and a
balance sheet of what Kamra can see.

None of this is statutory accounts. Kamra is the front office, not a
general ledger: it knows guest bills, company (city ledger) bills, advance
deposits, supplier bills (kamra.payables) and petty cash. It does not know
the bank balance, payroll run outside Kamra, fixed assets, loans or equity.
The accounting export (kamra.accounting) hands the invoices to the real
books.

Revenue is counted once, the way the night audit report does it:
- folio charges by department (kamra.ledger.CHARGE_TYPE_TO_CODE groups),
  leaving out restaurant bills charged to a room (their line ends
  "(ORD-...)") and banquet lines (they start "EVT-"), plus the room levy
  and go-live opening balances, which are not revenue
- restaurant bills (POS Order) from the bill itself, paid at the outlet
  or charged to a room
- banquets from the function (Venue Booking) on its event date
All figures are before tax.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, flt, get_first_day, getdate, nowdate

from kamra.authz import require_roles
from kamra.erp_books import erp_pos_url, erp_screens, kept_in_erp, receivables_source, refuse_kept_in_erp

FOLIO_SKIP = ("Opening Balance", "Room Levy")
NOT_FOLIO = """IFNULL(fc.description, '') NOT LIKE '%%(ORD-%%'
	  AND IFNULL(fc.description, '') NOT LIKE 'EVT-%%'"""


# a function is revenue (and its tax collected) once it has been held
BANQUET_HELD = "(status = 'Completed' OR (status = 'Confirmed' AND event_date <= %(t)s))"
# a supplier bill is a cost once approved; drafts only show in the cash view
BILL_COUNTS = "status IN ('Approved', 'Part Paid', 'Paid')"


def _places(property: str | None) -> int:
	from kamra.ledger import _currency_places
	return max(_currency_places(property), 2)


def _r(x, places: int = 2) -> float:
	from kamra.ledger import _round_currency
	return _round_currency(x, places)


def till_cash(property: str) -> float:
	"""Cash in the property's open tills right now (opening float + cash
	taken - paid outs, drops and petty cash)."""
	from kamra.cashier import _recompute_session_totals
	places = _places(property)
	names = frappe.get_all("Cashier Session",
	                       filters={"property": property, "status": "Open"}, pluck="name")
	return _r(sum(_recompute_session_totals(n, persist=False)["expected_cash"]
	              for n in names), places)


# ── next days of cash ───────────────────────────────────────────────────────

def _company_credit(company: str | None) -> bool:
	return bool(company and frappe.db.get_value(
		"Corporate Account", company, "credit_allowed"))


@frappe.whitelist()
@require_roles("Finance", "Hotel Admin", "Kamra Agent")
@kept_in_erp("cash_forecast")
def cash_forecast(property: str, days: int = 5, opening: float | None = None):
	"""Money expected in and out over the next `days` days (5, 15 or 30;
	default 5), day by day from today, with a running position and
	subtotals by week.

	In: company (city ledger) bills falling due, what departing guests
	still owe, advance deposits due from arrivals. Out: supplier bills
	falling due, drafts awaiting approval included and flagged `draft`.
	Anything already overdue is counted today. `opening` is
	the cash and bank to start from; without it, the cash in open tills.
	Read-only."""
	from kamra.api import _DEPOSIT_STATUSES, _deposit_state, _stay_balances
	from kamra.ledger import _account_position, _netted

	places = _places(property)
	eps = 0.5 * (10 ** -places)
	days = int(days or 5)
	if days not in (5, 15, 30):
		frappe.throw(_("Choose 5, 15 or 30 days."))
	today = getdate(nowdate())
	end = add_days(today, days - 1)
	items: list[dict] = []

	def add(kind, when, label, amount, ref=None, draft=False):
		if flt(amount) > eps:
			items.append({"kind": kind, "date": str(max(getdate(when), today)),
			              "label": label, "amount": _r(amount, places), "reference": ref,
			              "overdue": getdate(when) < today, "draft": draft})

	# A local cash position needs a supplier payment schedule. The ownership
	# declaration links to purchases but does not supply those due dates.
	if "payables" in erp_screens():
		return {"erp_owned": ["payables"], "from_date": str(today), "to_date": str(end),
		        "money_in": None, "money_out": None, "closing": None, "days": None, "weeks": None}

	source_ar = receivables_source(property)
	if source_ar.in_erp:
		positions = ((r["company"], r) for r in source_ar.rows)
	else:
		accounts = frappe.get_all("City Ledger Account", filters={"property": property, "disabled": 0},
		                          fields=["name", "company"])
		positions = ((a.company, _account_position(a.name)) for a in accounts)
	for company, position in positions:
		items_by_age = position["open_items"]
		if source_ar.in_erp:
			items_by_age = sorted(items_by_age, key=lambda item: item["invoice_date"])
		for it, amt in _netted(items_by_age, position["unapplied_credit"], places=places):
			if getdate(it["due_date"]) <= getdate(end):
				ref = it["folio"] or it.get("voucher_no")
				add("company_bills", it["due_date"], f"{company} {it.get('invoice_number') or ref}", amt, ref)

	# advance deposits due from guests arriving in the window
	deposits: dict[str, float] = {}
	for res in frappe.get_all(
			"Reservation",
			filters={"property": property, "status": ("in", _DEPOSIT_STATUSES),
			         "check_in_date": ("between", [today, end])},
			fields=["name", "property", "status", "guest_name", "check_in_date",
			        "amount_after_tax", "advance_paid"]):
		due = flt(_deposit_state(res)["due"])
		deposits[res.name] = due
		add("deposits", res.check_in_date, f"{res.guest_name} {res.name}", due, res.name)

	# what departing guests still owe. A stay a company pays on credit is
	# left out: its bill reaches the city ledger and falls due later.
	deps = frappe.get_all(
		"Reservation",
		filters={"property": property,
		         "status": ("in", ("Checked In",) + tuple(_DEPOSIT_STATUSES)),
		         "check_out_date": ("<=", end)},
		fields=["name", "status", "guest_name", "check_out_date", "company",
		        "group_booking", "pays_own_charges", "amount_after_tax"])
	group_co = dict(frappe.get_all(
		"Group Booking", filters={"name": ("in", [r.group_booking for r in deps
		                                           if r.group_booking] or [""])},
		fields=["name", "company"], as_list=True))
	deps = [r for r in deps
	        if (r.status == "Checked In" or getdate(r.check_out_date) >= today)
	        and (r.pays_own_charges or not _company_credit(
	            r.company or group_co.get(r.group_booking)))]
	money = _stay_balances([r.name for r in deps])
	for r in deps:
		m = money.get(r.name) or {}
		due = flt(m.get("due"))
		if r.status == "Checked In" and getdate(r.check_out_date) > today:
			# ponytail: nights not yet posted are taken from the booking
			# total; extras still to come are not forecast
			due = max(due, flt(r.amount_after_tax) - flt(m.get("paid")))
		# a deposit forecast above is part of the same bill
		add("departures", r.check_out_date, f"{r.guest_name} {r.name}",
		    due - deposits.get(r.name, 0), r.name)

	for b in frappe.get_all(
			"Vendor Bill",
			filters={"property": property,
			         "status": ("in", ("Awaiting Approval", "Approved", "Part Paid")),
			         "outstanding": (">", 0), "due_date": ("<=", end)},
			fields=["name", "vendor_name", "bill_number", "due_date", "outstanding",
			        "status"]):
		add("vendor_bills", b.due_date,
		    f"{b.vendor_name} {b.bill_number or b.name}", b.outstanding, b.name,
		    draft=b.status == "Awaiting Approval")

	source = "tills" if opening in (None, "") else "entered"
	position = till_cash(property) if source == "tills" else _r(opening, places)
	start = position
	IN = ("company_bills", "departures", "deposits")
	out_days = []
	for i in range(days):
		d = str(add_days(today, i))
		todays = sorted((x for x in items if x["date"] == d), key=lambda x: -x["amount"])
		sums = {k: _r(sum(x["amount"] for x in todays if x["kind"] == k), places)
		        for k in IN + ("vendor_bills",)}
		money_in = _r(sum(sums[k] for k in IN), places)
		money_out = sums["vendor_bills"]
		position = _r(position + money_in - money_out, places)
		out_days.append({"date": d, **sums, "money_in": money_in,
		                 "money_out": money_out, "net": _r(money_in - money_out, places),
		                 "closing": position, "items": todays})
	weeks = []
	for i in range(0, days, 7):
		wk = out_days[i:i + 7]
		money_in = _r(sum(d["money_in"] for d in wk), places)
		money_out = _r(sum(d["money_out"] for d in wk), places)
		weeks.append({"from_date": wk[0]["date"], "to_date": wk[-1]["date"],
		              "money_in": money_in, "money_out": money_out,
		              "net": _r(money_in - money_out, places), "closing": wk[-1]["closing"]})
	return {
		"from_date": str(today), "to_date": str(end),
		"opening": start, "opening_source": source,
		"days": out_days, "weeks": weeks,
		"money_in": _r(sum(d["money_in"] for d in out_days), places),
		"money_out": _r(sum(d["money_out"] for d in out_days), places),
		"closing": position,
		"overdue_in": _r(sum(x["amount"] for x in items
		                     if x["overdue"] and x["kind"] in IN), places),
		"overdue_out": _r(sum(x["amount"] for x in items
		                      if x["overdue"] and x["kind"] == "vendor_bills"), places),
		"draft_out": _r(sum(x["amount"] for x in items if x["draft"]), places),
	}


# ── management P&L ──────────────────────────────────────────────────────────

def _revenue(property: str, start, end) -> dict:
	from kamra.ledger import CHARGE_TYPE_TO_CODE
	rows = frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- values are parameterized; the interpolated text is a module constant
		f"""
		SELECT fc.charge_type, COALESCE(SUM(fc.amount), 0) AS amount
		FROM `tabFolio Charge` fc JOIN `tabFolio` f ON fc.parent = f.name
		WHERE f.property = %(p)s AND fc.posting_date BETWEEN %(s)s AND %(e)s
		  AND fc.charge_type NOT IN %(skip)s AND {NOT_FOLIO}
		  AND fc.charge_type != 'Outlet Tip' AND NOT EXISTS (SELECT 1 FROM `tabTransaction Code` tc WHERE tc.name = fc.transaction_code AND tc.txn_type = 'Non-Revenue')
		GROUP BY fc.charge_type
		""", {"p": property, "s": start, "e": end, "skip": FOLIO_SKIP}, as_dict=True)
	rev = {"rooms": 0.0, "fnb": 0.0, "banquets": 0.0, "other": 0.0, "allowances": 0.0}
	for r in rows:
		group = (CHARGE_TYPE_TO_CODE.get(r.charge_type) or ("", "", "Other"))[2]
		key = ("rooms" if group == "Rooms" or r.charge_type == "Discount" else
		       "fnb" if group == "F&B" else
		       "allowances" if r.charge_type == "Allowance" else "other")
		rev[key] += flt(r.amount)
	external_pos = erp_pos_url()
	if not external_pos:
		rev["fnb"] += flt(frappe.db.sql(
			"""SELECT COALESCE(SUM(order_total), 0) FROM `tabPOS Order`
			   WHERE property = %(p)s AND status = 'Delivered' AND IFNULL(nc, 0) = 0
			     AND DATE(creation) BETWEEN %(s)s AND %(e)s""",
			{"p": property, "s": start, "e": end})[0][0])
	# a confirmed function counts once it has been held
	rev["banquets"] += flt(frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- values are parameterized; the interpolated text is a module constant
		f"""SELECT COALESCE(SUM(taxable_amount), 0) FROM `tabVenue Booking`
		   WHERE property = %(p)s AND event_date BETWEEN %(s)s AND %(e)s
		     AND {BANQUET_HELD}""",
		{"p": property, "s": start, "e": end, "t": nowdate()})[0][0])
	# a credit note on a closed bill writes no charge line (on an open one
	# it is an Allowance line, already counted above); the P&L is before
	# tax, so it takes off what is credited less the tax given back
	rev["allowances"] -= flt(frappe.db.sql(
		"""SELECT COALESCE(SUM(cn.amount - IFNULL(cn.tax_amount, 0)), 0)
		   FROM `tabCredit Note` cn""" + CLOSED_BILL_CREDIT_NOTES,
		{"p": property, "s": start, "e": end})[0][0])
	places = _places(property)
	rounded = {k: _r(v, places) for k, v in rev.items()}
	if external_pos:
		rounded["fnb"] = None
	return rounded


# SQL from `tabCredit Note` cn: the period's issued credit notes on closed
# bills (on an open bill a credit note is an Allowance line, counted there)
CLOSED_BILL_CREDIT_NOTES = """
		   WHERE cn.property = %(p)s AND cn.status = 'Issued'
		     AND cn.issue_date BETWEEN %(s)s AND %(e)s
		     AND NOT EXISTS (SELECT 1 FROM `tabFolio Charge` fc
		                     WHERE fc.parent = cn.folio AND fc.charge_type = 'Allowance'
		                       AND fc.description LIKE CONCAT('Credit note ', cn.credit_note_number, ':%%'))"""


def tax_collected(property: str, start, end) -> float:
	"""Tax on guest bills (folio charges) plus banquets billed outside a
	folio, less the tax given back by credit notes on closed bills. Tax on
	restaurant bills paid at the outlet is not split out."""
	folio = frappe.db.sql(
		"""SELECT COALESCE(SUM(fc.gst_amount), 0)
		   FROM `tabFolio Charge` fc JOIN `tabFolio` f ON fc.parent = f.name
		   WHERE f.property = %(p)s AND fc.posting_date BETWEEN %(s)s AND %(e)s
		     AND fc.charge_type NOT IN ('Opening Balance')""",
		{"p": property, "s": start, "e": end})[0][0]
	banquet = frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- values are parameterized; the interpolated text is a module constant
		f"""SELECT COALESCE(SUM(tax_amount), 0) FROM `tabVenue Booking`
		   WHERE property = %(p)s AND event_date BETWEEN %(s)s AND %(e)s
		     AND {BANQUET_HELD} AND IFNULL(posted_to_folio, 0) = 0""",
		{"p": property, "s": start, "e": end, "t": nowdate()})[0][0]
	credited = frappe.db.sql(
		"""SELECT COALESCE(SUM(IFNULL(cn.tax_amount, 0)), 0)
		   FROM `tabCredit Note` cn""" + CLOSED_BILL_CREDIT_NOTES,
		{"p": property, "s": start, "e": end})[0][0]
	return _r(flt(folio) + flt(banquet) - flt(credited), _places(property))


def _bills_in(property: str, start, end):
	if "payables" in erp_screens():
		return None
	return frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- values are parameterized; the interpolated text is a module constant
		f"""SELECT expense_category AS category, COALESCE(SUM(amount), 0) AS amount,
		          COALESCE(SUM(tax_amount), 0) AS tax, COUNT(*) AS bills
		   FROM `tabVendor Bill`
		   WHERE property = %(p)s AND {BILL_COUNTS}
		     AND bill_date BETWEEN %(s)s AND %(e)s
		   GROUP BY expense_category ORDER BY amount DESC""",
		{"p": property, "s": start, "e": end}, as_dict=True)


def _dates(from_date, to_date):
	start, end = getdate(from_date), getdate(to_date)
	if end < start:
		frappe.throw(_("The end date is before the start date."))
	if date_diff(end, start) > 366:
		frappe.throw(_("Pick a period of a year or less."))
	return start, end


@frappe.whitelist()
@require_roles("Finance", "Hotel Admin", "Kamra Agent")
@kept_in_erp("profit_and_loss")
def profit_and_loss(property: str, from_date: str, to_date: str):
	"""A management P&L for the period, before tax: revenue by department
	(rooms, food and beverage, banquets, other, less allowances) and
	expenses (approved supplier bills by category on their bill date, petty
	cash by category, cash paid out from the tills). Credit notes on
	closed bills count under allowances. Not statutory accounts: payroll, depreciation and anything
	paid outside Kamra are not in it."""
	start, end = _dates(from_date, to_date)
	rev = _revenue(property, start, end)
	bills = _bills_in(property, start, end)
	petty = frappe.db.sql(
		"""SELECT category, COALESCE(SUM(amount), 0) AS amount, COUNT(*) AS vouchers
		   FROM `tabPetty Cash Voucher`
		   WHERE property = %(p)s AND status = 'Posted'
		     AND voucher_date BETWEEN %(s)s AND %(e)s
		   GROUP BY category ORDER BY amount DESC""",
		{"p": property, "s": start, "e": end}, as_dict=True)
	# cash paid to suppliers straight from a till (kamra.cashier.post_paid_out)
	paid_out = frappe.db.sql(
		"""SELECT COALESCE(SUM(-amount), 0), COUNT(*) FROM `tabCashier Transaction`
		   WHERE property = %(p)s AND kind = 'Paid Out' AND IFNULL(folio, '') = ''
		     AND business_date BETWEEN %(s)s AND %(e)s""",
		{"p": property, "s": start, "e": end})[0]
	places = _places(property)
	revenue = [{"key": k, "amount": rev[k]}
	           for k in ("rooms", "fnb", "banquets", "other", "allowances")]
	erp_owned = []
	if rev["fnb"] is None:
		erp_owned.append("pos")
	if bills is None:
		erp_owned.append("payables")
	revenue_total = _r(sum(rev.values()), places) if rev["fnb"] is not None else None
	expense_total = _r(sum(flt(b.amount) for b in bills) + sum(flt(p.amount) for p in petty)
	                   + flt(paid_out[0]), places) if bills is not None else None
	return {
		"erp_owned": erp_owned,
		"from_date": str(start), "to_date": str(end),
		"revenue": revenue, "revenue_total": revenue_total,
		"supplier_bills": [{"category": b.category, "amount": _r(b.amount, places),
		                    "count": int(b.bills)} for b in bills] if bills is not None else None,
		"petty_cash": [{"category": p.category, "amount": _r(p.amount, places),
		                "count": int(p.vouchers)} for p in petty],
		"paid_outs": {"amount": _r(paid_out[0], places), "count": int(paid_out[1])},
		"expense_total": expense_total,
		"net": None if erp_owned else _r(revenue_total - expense_total, places),
		"tax_collected": tax_collected(property, start, end),
		"tax_on_bills": _r(sum(flt(b.tax) for b in bills), places) if bills is not None else None,
	}


# ── balance sheet ───────────────────────────────────────────────────────────

def _ledger(property: str, ledger: str, as_of) -> float:
	return _r(frappe.db.sql(
		"""SELECT COALESCE(SUM(debit - credit), 0) FROM `tabFolio Ledger Entry`
		   WHERE property = %s AND ledger = %s AND business_date <= %s""",
		(property, ledger, as_of))[0][0], _places(property))


def payables_at(property: str, as_of) -> float | None:
	"""Supplier bills dated on or before `as_of` less what was paid on
	them by then."""
	if "payables" in erp_screens():
		return None
	billed = frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- values are parameterized; the interpolated text is a module constant
		f"""SELECT COALESCE(SUM(total), 0) FROM `tabVendor Bill`
		   WHERE property = %s AND {BILL_COUNTS} AND bill_date <= %s""",
		(property, as_of))[0][0]
	paid = frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- values are parameterized; the interpolated text is a module constant
		f"""SELECT COALESCE(SUM(p.amount), 0) FROM `tabVendor Bill Payment` p
		   JOIN `tabVendor Bill` b ON b.name = p.parent
		   WHERE b.property = %s AND b.{BILL_COUNTS}
		     AND b.bill_date <= %s AND p.payment_date <= %s""",
		(property, as_of, as_of))[0][0]
	return _r(flt(billed) - flt(paid), _places(property))


@frappe.whitelist()
@require_roles("Finance", "Hotel Admin", "Kamra Agent")
@kept_in_erp("balance_sheet")
def balance_sheet(property: str, as_of: str | None = None):
	"""What is owed to the hotel and what it owes, as of a date (default
	today): guest ledger and city ledger against advance deposits and
	supplier bills, plus cash in open tills when the date is today. Tax
	collected this month is shown for reference, not in the totals: Kamra
	does not record tax paid to the government. Not a full balance sheet:
	the bank, fixed assets, loans and equity are not in Kamra."""
	places = _places(property)
	today = getdate(nowdate())
	d = getdate(as_of) if as_of else today
	if d > today:
		frappe.throw(_("Pick today or a date in the past."))
	cash = till_cash(property) if d == today else None
	source_ar = receivables_source(property)
	if source_ar.in_erp:
		# The reader supplies a live balance and accepts no historical date.
		if d != today:
			refuse_kept_in_erp(source_ar.target)
		city = _r(sum(row["balance"] for row in source_ar.rows), places)
	else:
		city = _r(frappe.db.sql(
			"""SELECT COALESCE(SUM(debit - credit), 0) FROM `tabCity Ledger Entry`
			   WHERE property = %s AND posting_date <= %s""", (property, d))[0][0], places)
	assets = [
		{"key": "cash_in_tills", "amount": cash},
		{"key": "guest_ledger", "amount": _ledger(property, "Guest", d)},
		{"key": "city_ledger", "amount": city},
	]
	suppliers = payables_at(property, d)
	liabilities = [
		{"key": "advance_deposits", "amount": _r(-_ledger(property, "Deposit", d), places)},
		{"key": "payables", "amount": suppliers},
	]
	month = get_first_day(d)
	assets_total = _r(sum(flt(a["amount"]) for a in assets), places)
	liabilities_total = _r(sum(flt(x["amount"]) for x in liabilities), places) if suppliers is not None else None
	bills = _bills_in(property, month, d) if suppliers is not None else None
	return {
		"erp_owned": ["payables"] if suppliers is None else [],
		"as_of": str(d),
		"assets": assets, "assets_total": assets_total,
		"liabilities": liabilities, "liabilities_total": liabilities_total,
		"net": _r(assets_total - liabilities_total, places) if liabilities_total is not None else None,
		"tax_month_from": str(month),
		"tax_collected_month": tax_collected(property, month, d),
		"tax_on_bills_month": _r(sum(flt(b.tax) for b in bills), places) if bills is not None else None,
	}
