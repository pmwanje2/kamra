"""Folio lifecycle: open at check-in, post nightly charges, settle, close.

All amounts flow from the deterministic pricing engine. GST is carried
per line so a folio can mix rates (room 5/18%, F&B 5%) and still produce
a correct multi-rate invoice - the PRD's FR-60.
"""

import json
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe.utils import add_days, getdate, now_datetime, nowdate

FNB_GST = 5.0  # F&B / meal-plan GST rate (India default; kept for imports)

# SQL on `tabFolio Charge` fc: a line that is neither voided nor a void's
# reversal. Both stay on the folio (void_charge) and net to zero, so sums
# need nothing; counts of rooms sold and "already posted" checks do.
NOT_VOIDED = """(IFNULL(fc.reversal_of, '') = '' AND NOT EXISTS (
	SELECT 1 FROM `tabFolio Charge` v
	WHERE v.parent = fc.parent AND v.reversal_of = fc.name))"""

# SQL on `tabFolio Charge` fc: a line that stands on its own posting date,
# for the day's statistics and audit checks. A line voided the same day
# drops out with its reversal; one voided on a later business date still
# stands on its own date, and its reversal is an adjustment on the void
# date (OPERA dates the adjustment of a past revenue date on the current
# business date: the closed day is not restated). A reversal line is never
# a room sold. Sums need nothing: each day's lines add up on their own.
STANDS_ON_ITS_DATE = """(IFNULL(fc.reversal_of, '') = '' AND NOT EXISTS (
	SELECT 1 FROM `tabFolio Charge` v
	WHERE v.parent = fc.parent AND v.reversal_of = fc.name
	  AND v.posting_date = fc.posting_date))"""


def _fnb_gst(property):
	from kamra.localization import pack_for
	return pack_for(property).fnb_tax_rate(property)


def _recalculate(folio):
	from kamra.ledger import minor_unit
	unit = minor_unit(folio.property) if folio.get("property") else None
	charges = tax = Decimal(0)
	for c in folio.charges:
		amount = Decimal(str(c.amount or 0))
		line_tax = _line_tax(c, amount, unit)
		c.gst_amount = float(line_tax)
		c.total = float(amount + line_tax)
		charges += amount
		tax += line_tax
	paid = sum(Decimal(str(p.amount or 0)) for p in folio.payments)
	folio.charges_total = float(charges)
	folio.tax_total = float(tax)
	folio.grand_total = float(charges + tax)
	folio.payments_total = float(paid)
	folio.balance = float(charges + tax - paid)


def _line_tax(c, amount: Decimal, unit: Decimal | None = None) -> Decimal:
	"""A line's tax: worked out from its rate (quantized to the property's
	minor unit whenever the line's base is in minor units), or, for a line
	split to the currency's minor unit (tax_as_posted), the tax it was
	posted with."""
	if c.get("tax_as_posted"):
		return Decimal(str(c.gst_amount or 0))
	tax = amount * Decimal(str(c.gst_rate or 0)) / 100
	if unit is not None and amount == amount.quantize(unit):
		return tax.quantize(unit, rounding=ROUND_HALF_UP)
	return tax


def open_folio(reservation) -> str:
	"""Open (or return) the folio for a reservation. Called at check-in."""
	existing = frappe.db.get_value(
		"Folio", {"reservation": reservation.name, "folio_type": "Guest"}
	)
	if existing:
		_post_addons(reservation, existing)
		_post_pet_charge(reservation, existing)
		return existing
	folio = frappe.get_doc({
		"doctype": "Folio",
		"property": reservation.property,
		"reservation": reservation.name,
		"guest": reservation.guest,
		"status": "Open",
		"opened_on": now_datetime(),
	})
	if reservation.discount_amount:
		folio.append("charges", {
			"posting_date": nowdate(),
			"charge_type": "Discount",
			"description": "Booking discount (voucher)",
			"qty": 1,
			"rate": -float(reservation.discount_amount),
			"amount": -float(reservation.discount_amount),
			"gst_rate": _room_gst(reservation),
			"auto_posted": 1,
		})
	_recalculate(folio)
	from kamra.ledger import save_folio
	save_folio(folio)
	if reservation.flags.get("carried_forward"):
		# an in-house guest moved over at go-live: the old system billed
		# the one-time fees, and they arrive inside the opening balance
		return folio.name
	_post_addons(reservation, folio.name)
	_post_cleaning_fee(reservation, folio.name)
	_post_pet_charge(reservation, folio.name)
	return folio.name


def _post_cleaning_fee(reservation, folio_name: str):
	"""Post one-time cleaning fee from quote snapshot / property (ADR-008)."""
	fallback = float(
		frappe.db.get_value("Property", reservation.property, "cleaning_fee") or 0
	)
	_post_stay_fee(reservation, folio_name, "cleaning_fee", "Cleaning Fee",
	               "Cleaning fee", fallback)


def _post_pet_charge(reservation, folio_name: str):
	"""Post the pet charge once per stay, at what the guest was quoted."""
	pets = int(reservation.get("pets") or 0)
	if pets <= 0:
		return
	from frappe.utils import date_diff

	from kamra.pricing import pet_charge_amount
	prop = frappe.get_cached_doc("Property", reservation.property)
	# a property that has since stopped taking pets must not block check-in;
	# the quoted line in the snapshot still posts
	fallback = float(pet_charge_amount(
		prop, pets,
		date_diff(reservation.check_out_date, reservation.check_in_date),
	)) if prop.get("pets_allowed") else 0.0
	_post_stay_fee(reservation, folio_name, "pet_charge", "Pet Charge",
	               f"Pets × {pets}", fallback)


def _post_stay_fee(reservation, folio_name: str, component: str,
                   charge_type: str, description: str, fallback: float):
	"""Post a one-time stay fee at the amount and tax in the booking's quote
	snapshot, so a later price change never re-bills the guest. Without a
	snapshot line it posts `fallback` untaxed. Idempotent per folio."""
	amount = 0.0
	tax_rate = 0.0
	snap = getattr(reservation, "quote_snapshot", None)
	if snap:
		try:
			data = json.loads(snap) if isinstance(snap, str) else snap
			for li in data.get("line_items") or []:
				if li.get("component") == component:
					amount = float(li.get("amount") or 0)
					tax = float(li.get("tax_amount") or 0)
					tax_rate = (tax / amount * 100) if amount else 0
					break
		except Exception:
			pass  # snapshot unreadable; fall back to the property's current fee
	if amount <= 0:
		amount = fallback
	if amount <= 0:
		return
	if frappe.db.exists(
		"Folio Charge",
		{"parent": folio_name, "charge_type": charge_type, "auto_posted": 1},
	):
		return
	doc = frappe.get_doc("Folio", folio_name)
	if doc.status == "Closed":
		return
	doc.append(
		"charges",
		{
			"posting_date": nowdate(),
			"charge_type": charge_type,
			"description": description,
			"qty": 1,
			"rate": amount,
			"amount": amount,
			"gst_rate": tax_rate,
			"auto_posted": 1,
		},
	)
	_recalculate(doc)
	from kamra.ledger import save_folio
	save_folio(doc)


def _post_addons(reservation, folio_name: str):
	"""Post booking-time add-ons (experiences) that haven't hit the folio
	yet. Idempotent via the row's posted flag."""
	pending = [a for a in reservation.get("addons") or [] if not a.posted]
	if not pending:
		return
	doc = frappe.get_doc("Folio", folio_name)
	if doc.status == "Closed":
		return
	for a in pending:
		doc.append("charges", {
			"posting_date": nowdate(),
			"charge_type": "Misc",
			"reservation": reservation.name,
			"description": a.description or a.experience,
			"qty": a.qty or 1,
			"rate": a.rate,
			"amount": a.amount,
			"gst_rate": a.gst_rate,
			"auto_posted": 1,
		})
		frappe.db.set_value("Stay Addon", a.name, "posted", 1,
		                    update_modified=False)
		a.posted = 1
	_recalculate(doc)
	from kamra.ledger import save_folio
	save_folio(doc)


# A billing window's payee (OPERA: each window has its own payee profile and
# payment method; SAP: one billing document per payer).
WINDOW_FIELDS = ("payee_type", "payee_company", "payee_agent", "payment_method")
DIRECT_BILL = "Company Credit"  # the Folio Payment mode of a direct-bill window


def window_cap(property: str | None) -> int:
	"""How many windows a stay or a posting master may have: the property's
	`folio_windows` setting (OPERA allows up to 8)."""
	from kamra.settings import get_setting
	return max(int(get_setting(property, "folio_windows") or 1), 1)


def _window_key(reservation: str | None = None, group_booking: str | None = None) -> dict:
	"""The folios that share one set of window numbers: a stay's own, or a
	group's posting master."""
	if group_booking and not reservation:
		return {"group_booking": group_booking, "folio_type": "Group"}
	return {"reservation": reservation, "folio_type": ("!=", "Group")}


def next_window(key: dict, property: str, start: int = 1) -> int:
	"""The lowest window number no folio of this stay or group holds yet. A
	window whose folio closed keeps its number: its next posting reopens it."""
	cap = window_cap(property)
	taken = {int(w) for w in frappe.get_all(
		"Folio", filters={**key, "window_no": (">", 0)}, pluck="window_no")}
	for n in range(start, cap + 1):
		if n not in taken:
			return n
	frappe.throw(frappe._("A bill can have at most {0} windows. Close a window or move its "
	                      "charges to another one.").format(cap))


def _company_credit(company: str | None) -> bool:
	return bool(company) and bool(frappe.db.get_value("Corporate Account", company,
	                                                  "credit_allowed"))


def open_window(reservation: str | None = None, group_booking: str | None = None,
                window_no: int | None = None, payee_type: str | None = None,
                payee_company: str | None = None, payee_agent: str | None = None,
                payment_method: str | None = None, bill_to_name: str | None = None,
                folio_type: str | None = None) -> str:
	"""Open a billing window on a stay or on a group's posting master and
	return its folio. Without `window_no` it takes the next free number (2
	on, for a stay: window 1 is the guest's). The payee defaults to the
	guest; a posting master's to the group's company. A credit company's
	window is direct bill (Company Credit) unless a method is given.
	`folio_type` stays the account kind (Guest, Extra, Company, Group)."""
	if bool(reservation) == bool(group_booking):
		frappe.throw(frappe._("Open a window on a stay or on a group's master bill."))
	if reservation:
		res = frappe.db.get_value("Reservation", reservation,
		                          ["name", "property", "guest"], as_dict=True)
		if not res:
			frappe.throw(frappe._("Reservation {0} not found.").format(reservation))
		prop, guest = res.property, res.guest
	else:
		gb = frappe.db.get_value("Group Booking", group_booking,
		                         ["name", "property", "company", "master_reservation", "status"],
		                         as_dict=True)
		if not gb:
			frappe.throw(frappe._("Group {0} not found.").format(group_booking))
		if gb.status == "Cancelled":
			frappe.throw(frappe._("Group {0} is cancelled, so it has no master bill.").format(
				group_booking))
		prop = gb.property
		guest = gb.master_reservation and frappe.db.get_value(
			"Reservation", gb.master_reservation, "guest")
		if not payee_type:
			payee_type = "Company" if gb.company else "Guest"
			payee_company = payee_company or gb.company
	payee_type = payee_type or "Guest"
	key = _window_key(reservation, group_booking)
	n = int(window_no or 0) or next_window(key, prop, start=1 if group_booking else 2)
	if not payment_method and payee_type == "Company" and _company_credit(payee_company):
		payment_method = DIRECT_BILL
	if not folio_type:
		folio_type = "Group" if group_booking else "Guest" if n == 1 \
			else "Company" if payee_type == "Company" else "Extra"
	folio = frappe.get_doc({
		"doctype": "Folio",
		"property": prop,
		"reservation": reservation,
		"guest": guest,
		"folio_type": folio_type,
		"group_booking": group_booking,
		"window_no": n,
		"payee_type": payee_type,
		"payee_company": payee_company,
		"payee_agent": payee_agent,
		"payment_method": payment_method,
		"bill_to_name": (bill_to_name or "")[:140] or None,
		"status": "Open",
		"opened_on": now_datetime(),
	})
	_recalculate(folio)
	folio.insert(ignore_permissions=True)
	return folio.name


def window_folio(reservation: str | None = None, group_booking: str | None = None,
                 window_no: int = 1) -> str:
	"""The open folio of one window. When the window's folio was closed
	(invoiced), a new one opens for it with the same payee; a window never
	used opens now (a stay's window 1 through open_folio, with its one-time
	fees)."""
	key = _window_key(reservation, group_booking)
	n = int(window_no or 1)
	current = frappe.db.get_value("Folio", {**key, "window_no": n, "status": "Open"},
	                              "name", order_by="creation desc")
	if current:
		return current
	last = frappe.db.get_value("Folio", {**key, "window_no": n},
	                           ["folio_type", "bill_to_name", *WINDOW_FIELDS],
	                           as_dict=True, order_by="creation desc")
	if last:
		return open_window(reservation, group_booking, window_no=n,
		                   folio_type=last.folio_type, bill_to_name=last.bill_to_name,
		                   **{f: last.get(f) for f in WINDOW_FIELDS})
	if reservation and n == 1:
		return open_folio(frappe.get_doc("Reservation", reservation))
	return open_window(reservation, group_booking, window_no=n)


def open_group_folio(group_booking: str) -> str:
	"""Open (or return) the group's posting master (OPERA PM): the group's
	own bill, window 1, with no stay behind it. It opens with the Group
	Booking, before any room is picked up, and takes deposits, banquet
	postings and what is routed to the group. A member's check-out never
	settles it; the group does (settle_group_bill)."""
	return window_folio(group_booking=group_booking, window_no=1)


def open_company_folio(reservation) -> str:
	"""Open (or return) the window of a corporate stay that bills the stay's
	company."""
	company = reservation.get("company")
	if company:
		key = _window_key(reservation.name)
		existing = frappe.db.get_value(
			"Folio", {**key, "payee_type": "Company", "payee_company": company,
			          "window_no": (">", 0)},
			["name", "window_no", "status"], as_dict=True, order_by="window_no asc")
		if existing:
			return existing.name if existing.status == "Open" else \
				window_folio(reservation=reservation.name, window_no=existing.window_no)
	existing = frappe.db.get_value(
		"Folio",
		{"reservation": reservation.name, "folio_type": "Company",
		 "status": "Open", "payee_type": ("is", "not set")},
	)
	if existing:
		return existing
	return split_folio(reservation.name, "Company")


# What a credit company pays when it has NO billing rules of its own - the
# property's `default_company_billing` setting. "Room" means the stay as sold:
# the room night, its meal plan, the levies on it, early/late fees, the
# cleaning fee, the extra beds agreed on it and any discount against it.
# Liquor is Food & Beverage like the food: a preset has no liquor rule (OPERA
# routes only what is included). A company that does not pay for liquor names
# the codes it does pay in its own billing rules.
_ROOM_CHARGES = frozenset({
	"Room", "Meal Plan", "Room Levy", "Early Check-in", "Late Checkout", "Early Departure",
	"Cleaning Fee", "Extra Guests", "Discount", "Rate Adjustment",
})
COMPANY_BILLING_DEFAULTS = {
	"Room and F&B": _ROOM_CHARGES | {"Food & Beverage"},
	"Room only": _ROOM_CHARGES,
	"Everything": None,  # every charge type
	"Nothing (guest pays unless rules say so)": frozenset(),
}
DEFAULT_COMPANY_BILLING = "Room and F&B"


def default_company_billing(property: str | None) -> str:
	from kamra.settings import get_setting
	value = get_setting(property, "default_company_billing")
	return value if value in COMPANY_BILLING_DEFAULTS else DEFAULT_COMPANY_BILLING


def _company_pays(company: str, charge_type: str,
                  property: str | None = None, code: str | None = None) -> bool:
	"""Does the company pay this charge? Its own billing rules decide when it
	has any (OPERA profile routing): a rule names a charge type, every code
	of it, or one transaction code; the company pays what a rule includes
	and nothing else. A credit company with none falls back to the
	property's default; a company without credit pays nothing by default."""
	rules = frappe.get_all(
		"Company Billing Rule",
		filters={"parent": company},
		fields=["charge_type", "transaction_code", "pay_by"])
	if rules:
		# a rule that names a transaction code covers that code alone
		return any(r.pay_by == "Company" and (
			r.transaction_code == code if r.transaction_code
			else r.charge_type == charge_type) for r in rules)
	if not frappe.db.get_value("Corporate Account", company, "credit_allowed"):
		return False
	covered = COMPANY_BILLING_DEFAULTS[default_company_billing(property)]
	return covered is None or charge_type in covered


def billing_rule_rows(company: str) -> list[dict]:
	"""A company's billing rules as the check-in and check-out screens show
	them: a charge-type rule by its type, a transaction-code rule by its code
	and the code's description (code_label)."""
	rows = frappe.get_all("Company Billing Rule", filters={"parent": company},
	                      fields=["charge_type", "transaction_code", "pay_by"],
	                      order_by="idx asc")
	out = []
	for r in rows:
		code = r.transaction_code or None
		label = None
		if code:
			label = " ".join(filter(None, (code, frappe.db.get_value(
				"Transaction Code", code, "description"))))
		out.append({"charge_type": None if code else r.charge_type,
		            "transaction_code": code, "code_label": label, "pay_by": r.pay_by})
	return out


def target_folio(reservation, charge_type: str, *, amount=None, posting_date=None,
                 transaction_code: str | None = None) -> str:
	"""Route a whole charge to the right folio of the stay: the stay's
	Routing Instruction for it first (kamra.routing), else the legacy
	presets (_legacy_target). A line routed whole is never split; with
	`amount` None an instruction with a limit is skipped."""
	from kamra.routing import route
	parts = route(reservation, charge_type, amount, posting_date,
	              transaction_code=transaction_code, whole=True)
	if parts:
		return parts[0]["folio"]
	return _legacy_target(reservation, charge_type, transaction_code)


def routed_target(reservation, charge_type: str, *, amount=None, posting_date=None,
                  transaction_code: str | None = None, pending=()) -> tuple[str, str | None]:
	"""(open folio, routing instruction) for a charge that moves whole, such
	as an outlet check or a bill moved to another room."""
	from kamra.routing import route
	parts = route(reservation, charge_type, amount, posting_date,
	              transaction_code=transaction_code, whole=True, pending=pending)
	if parts:
		return parts[0]["folio"], parts[0]["routing_instruction"]
	return open_line_target(reservation, charge_type, name=_legacy_target(
		reservation, charge_type, transaction_code)), None


def line_parts(reservation, charge_type: str, amount, posting_date=None, *,
               transaction_code: str | None = None, gst_rate=0,
               pending=()) -> list[dict]:
	"""Where a hotel line goes: [{folio, amount, routing_instruction}], never
	an invoiced folio. A routing limit splits it into the routed part and
	the remainder on window 1, each with its tax to the currency's minor unit
	(gst_amount, tax_as_posted); `pending` are routed lines not saved yet."""
	from kamra.routing import route
	parts = route(reservation, charge_type, amount, posting_date,
	              transaction_code=transaction_code, gst_rate=gst_rate, pending=pending)
	if parts:
		return parts
	# no instruction covers it: the legacy presets, on an open folio
	name = open_line_target(reservation, charge_type, name=_legacy_target(
		reservation, charge_type, transaction_code))
	return [{"folio": name, "amount": amount, "routing_instruction": None}]


def _legacy_target(reservation, charge_type: str, transaction_code: str | None = None) -> str:
	"""The presets, used only when no Routing Instruction covers a charge.

	Group stays route company-payable charges to the group's posting master
	(company pays the stay for every room, each guest pays their own
	extras). Individual corporate stays route to the stay's company window.
	The company's billing rules pick the charge types and transaction codes;
	a credit company with no rules uses the property's default
	(`default_company_billing`). Nothing else decides: no hidden default
	keeps a charge off a company that includes its code.

	A group with a main room (`master_reservation`) and no paying company
	routes what `master_pays` covers to the same posting master. A room that
	opted out (`pays_own_charges`) keeps its own charges, on a company group
	too: the group's company does not pick them up through the stay either.
	A cancelled group has no posting master.
	"""
	from kamra.routing import line_code
	code = line_code(charge_type, transaction_code)
	group = reservation.get("group_booking")
	g = frappe.db.get_value(
		"Group Booking", group,
		["company", "master_reservation", "master_pays", "status"], as_dict=True) if group else None
	if g and g.status == "Cancelled":
		g = None
	opted_out = bool(g) and bool(reservation.get("pays_own_charges"))
	if g and not opted_out:
		if g.company and _company_pays(
				g.company, charge_type, reservation.get("property"), code):
			return open_group_folio(group)
		if g.master_reservation:
			covered = COMPANY_BILLING_DEFAULTS.get(g.master_pays, frozenset())
			if covered is None or charge_type in covered:
				return open_group_folio(group)
	company = reservation.get("company")
	if company and not (opted_out and company == g.company) and _company_pays(
			company, charge_type, reservation.get("property"), code):
		return open_company_folio(reservation)
	return open_folio(reservation)


def _charge_posted(reservation_name: str, charge_type: str, date: str) -> bool:
	"""Has this charge type already been posted for this date on ANY folio
	of the stay? Routing can land a member's room night on the GROUP
	master (anchored to the lead reservation), so we match on the line's
	own provenance first and fall back to the folio's reservation for
	lines predating the provenance field. Only automated lines count: a
	charge the desk posted by hand (an extra bed asked for at check-in) is
	not the night's own posting and must not suppress it."""
	return bool(frappe.db.sql(
		"""
		SELECT 1 FROM `tabFolio Charge` fc
		JOIN `tabFolio` f ON fc.parent = f.name
		WHERE fc.charge_type = %(ct)s AND fc.posting_date = %(date)s
		  AND fc.auto_posted = 1 AND """ + NOT_VOIDED + """
		  AND (fc.reservation = %(res)s
		       OR (IFNULL(fc.reservation, '') = '' AND f.reservation = %(res)s))
		LIMIT 1
		""",
		{"res": reservation_name, "ct": charge_type, "date": str(date)},
	))


def _room_gst(reservation) -> float:
	"""Effective GST for this reservation's first night (used for the
	discount line). Slab-aware."""
	from kamra.pricing import room_gst_rate

	locked = locked_night(reservation, reservation.check_in_date)
	if locked is not None:
		return float(locked.gst_rate or 0)

	rt = frappe.get_doc("Room Type", reservation.room_type)
	rate = Decimal(str(_nightly_room_rate(reservation, reservation.check_in_date)))
	return float(room_gst_rate(reservation.property, rt, rate))


def locked_night(reservation, date):
	"""The rate confirmed for this night at booking (Reservation Night
	Rate), or None for a stay booked before rates were locked."""
	key = str(getdate(date))
	for row in reservation.get("night_rates") or []:
		if str(row.stay_date) == key:
			return row
	return None


def _nightly_room_rate(reservation, date) -> float:
	"""Taxable nightly rate as the guest was quoted it.

	A booking carries its confirmed nightly rates (`night_rates`, locked
	from the quote that priced it - demand premium included), and the folio
	bills exactly those: a later season, rate change or a drop in occupancy
	never re-prices a confirmed stay. Stays booked before rates were locked
	fall back to pricing the night now: seasons applied, tax backed out when
	the property configures tax-inclusive pricing, and the booking's rate
	plan applied the way pricing.quote applies it."""
	locked = locked_night(reservation, date)
	if locked is not None:
		return float(locked.rate or 0)
	rate = _plan_free_night_rate(reservation, date)
	plan = reservation.get("rate_plan")
	if plan:
		rate *= _rate_plan_factor(reservation, plan)
	return float(rate)


def _day_use_room_charge(reservation, date) -> tuple[float, float, str] | None:
	"""Taxable amount, GST rate and label for an hourly/block day-use stay.

	Uses elapsed hours from planned check-in to now (checkout), rounded by
	the property's day-use overstay rule. Returns None for overnight stays
	and for Nightly day-use plans (those keep the locked one-night rate)."""
	from kamra.day_use import (
		billable_units,
		overstay_rule,
		plan_billing,
		reservation_slot_hours,
	)
	from kamra.pricing import rates_include_tax, room_gst_rate

	if not getattr(reservation, "is_day_use", 0):
		return None
	plan = plan_billing(reservation.get("rate_plan"))
	if not plan:
		return None
	elapsed, planned = reservation_slot_hours(reservation)
	units = billable_units(
		elapsed, plan,
		overstay=overstay_rule(reservation.property),
		planned_hours=planned,
	)
	if units["full_day"]:
		# Overstay past planned ETD under "Full day": one card night.
		amount = float(_plan_free_night_rate(reservation, date))
		return amount, _nightly_gst(reservation, date), "day use · full day"
	gross = Decimal(str(units["amount"]))
	rt = frappe.get_doc("Room Type", reservation.room_type)
	gst = float(room_gst_rate(reservation.property, rt,
	                          gross + _over_cap_night(reservation)))
	if rates_include_tax(reservation.property):
		amount = float(gross / (Decimal(1) + Decimal(str(gst)) / Decimal(100)))
	else:
		amount = float(gross)
	if plan["billing_basis"] == "Hourly":
		label = f"day use · {units['hours']:g} h"
	else:
		label = (f"day use · {units['units']:g} × "
		         f"{plan['block_hours']:g}h")
	return amount, gst, label


def _rate_plan_factor(reservation, plan) -> Decimal:
	"""The stay's rate-plan factor, worked out once per loaded reservation:
	a plan that adds or fixes an amount works on the whole stay's total, so
	every night of the stay has to be priced to find one night's share."""
	from kamra.pricing import rate_plan_factor

	key = (plan, str(reservation.check_in_date), str(reservation.check_out_date),
	       reservation.adults, reservation.children, reservation.room_type,
	       reservation.get("over_capacity_charge"))
	flags = getattr(reservation, "flags", None)
	cache = flags.get("_kamra_plan_factor") if flags is not None else None
	if cache and cache[0] == key:
		return cache[1]
	start = getdate(reservation.check_in_date)
	nights = max((getdate(reservation.check_out_date) - start).days, 1)
	total = sum((_plan_free_night_rate(reservation, add_days(start, i))
	             for i in range(nights)), Decimal(0))
	factor = rate_plan_factor(plan, total)
	if flags is not None:
		flags["_kamra_plan_factor"] = (key, factor)
	return factor


def _plan_free_night_rate(reservation, date) -> Decimal:
	"""One night before the rate plan - the per-night step of pricing.quote."""
	from kamra.pricing import (
		occupancy_rate,
		rates_include_tax,
		room_gst_rate,
		season_adjust,
		small_group_allows_stay,
	)

	rt = frappe.get_doc("Room Type", reservation.room_type)
	base = occupancy_rate(
		rt, reservation.adults, reservation.children, date,
		allow_small_group=small_group_allows_stay(
			rt, reservation.check_in_date, reservation.check_out_date))
	rate = season_adjust(reservation.property, date, base,
	                     reservation.room_type)
	if rates_include_tax(reservation.property):
		gst = room_gst_rate(reservation.property, rt,
		                    rate + _over_cap_night(reservation))
		rate = rate / (Decimal(1) + gst / Decimal(100))
	return rate


def _over_cap_night(reservation) -> Decimal:
	"""The night's agreed extra-bed amount. It bills as its own line, but the
	slab reads the whole night's tariff, so it has to be in hand wherever the
	room's tax rate is worked out - matching pricing.quote."""
	return Decimal(str(reservation.get("over_capacity_charge") or 0))


def _nightly_gst(reservation, date) -> float:
	locked = locked_night(reservation, date)
	if locked is not None:
		return float(locked.gst_rate or 0)
	from kamra.pricing import (
		occupancy_rate,
		room_gst_rate,
		season_adjust,
		small_group_allows_stay,
	)

	rt = frappe.get_doc("Room Type", reservation.room_type)
	base = occupancy_rate(
		rt, reservation.adults, reservation.children, date,
		allow_small_group=small_group_allows_stay(
			rt, reservation.check_in_date, reservation.check_out_date))
	gross = season_adjust(reservation.property, date, base,
	                      reservation.room_type)
	return float(room_gst_rate(reservation.property, rt,
	                           gross + _over_cap_night(reservation)))


def open_line_target(reservation, charge_type: str, name: str | None = None) -> str:
	"""target_folio (or `name`), never an invoiced folio: when the folio the
	line would go to is closed, it goes on the open folio of the same window,
	and one opens when there is none, with the same payee (the part_settle
	shape, without repeating the one-time fees the first folio carried)."""
	name = name or target_folio(reservation, charge_type)
	row = frappe.db.get_value("Folio", name, ["status", "folio_type", "group_booking",
	                                         "window_no"], as_dict=True)
	if not row or row.status != "Closed":
		return name
	if row.folio_type == "Group" and row.group_booking:
		return window_folio(group_booking=row.group_booking, window_no=row.window_no or 1)
	return window_folio(reservation=reservation.name, window_no=row.window_no or 1)


def routed_figure(property: str, charge_type: str, amount, tax) -> Decimal:
	"""What a routing limit counts of a line: its price, tax included, for a
	line priced with tax (OPERA: an inclusive code routes its value with its
	taxes), else its base (an exclusive code's taxes are not routed)."""
	amount = Decimal(str(amount or 0))
	return amount + Decimal(str(tax or 0)) if priced_with_tax(property, charge_type) else amount


def _pending_routed(folios) -> list[tuple]:
	"""Routed lines appended to the loaded folios and not saved yet, so a
	multi-night posting run spends a routing limit only once:
	[(instruction, posting_date, figure)]."""
	from kamra.ledger import minor_unit
	return [(c.routing_instruction, c.posting_date,
	         routed_figure(doc.property, c.charge_type, c.amount,
	                       _line_tax(c, Decimal(str(c.amount or 0)),
	                                 minor_unit(doc.property) if doc.get("property") else None)))
	        for doc in folios.values() for c in doc.charges
	        if c.get("routing_instruction") and not c.get("creation")]


def _append_charge(folios, reservation, charge_type, line):
	"""Append a line to the folio(s) routing sends it to, reusing loaded docs
	so a multi-night posting run saves each folio once. A routing limit
	splits it: the routed part and the remainder are each a line, with their
	tax to the minor unit."""
	parts = line_parts(reservation, charge_type, line["amount"], line["posting_date"],
	                   transaction_code=line.get("transaction_code"),
	                   gst_rate=line.get("gst_rate"), pending=_pending_routed(folios))
	for part in parts:
		name = part["folio"]
		if name not in folios:
			folios[name] = frappe.get_doc("Folio", name)
		row = {**line, "routing_instruction": part["routing_instruction"]}
		if len(parts) > 1:
			row.update(qty=1, rate=part["amount"], amount=part["amount"],
			           gst_amount=part["gst_amount"], tax_as_posted=1)
		folios[name].append("charges", row)


def post_room_night(reservation, date, folios=None) -> bool:
	"""Post one night's room (and meal plan) charge, routed by the
	company's billing rules. Skips dates already posted - safe to call
	from both audit and checkout. Pass `folios` (a dict) to batch saves
	across nights; when omitted the touched folios save immediately."""
	date = str(date)
	own_batch = folios is None
	if own_batch:
		folios = {}
	posted = False

	if not _charge_posted(reservation.name, "Room", date):
		room_no = (reservation.room or "").split("-")[-1]
		day_use = _day_use_room_charge(reservation, date)
		if day_use:
			night_rate, gst_rate, du_label = day_use
			desc = (f"Room {room_no} · {reservation.room_type.split('-')[-1]}"
			        f" · {du_label}")
		else:
			night_rate = _nightly_room_rate(reservation, date)
			gst_rate = _nightly_gst(reservation, date)
			desc = (f"Room {room_no} · {reservation.room_type.split('-')[-1]}"
			        + (" · day use" if getattr(reservation, "is_day_use", 0) else ""))
		_append_charge(folios, reservation, "Room", {
			"posting_date": date,
			"charge_type": "Room",
			"reservation": reservation.name,
			"description": desc,
			"qty": 1,
			"rate": night_rate,
			"amount": night_rate,
			"gst_rate": gst_rate,
			"auto_posted": 1,
		})
		posted = True
		_post_room_levy(folios, reservation, date, night_rate)

	# Its own charge type, not the shared Misc bucket: dedupe is by type and
	# date, so a sundry the desk had already typed as Misc that day would
	# otherwise make the night's extra-bed charge look posted and skip it.
	over_cap = float(reservation.get("over_capacity_charge") or 0)
	if over_cap > 0 and not _charge_posted(
			reservation.name, "Extra Guests", date):
		guests = int(reservation.get("over_capacity_guests") or 0)
		_append_charge(folios, reservation, "Extra Guests", {
			"posting_date": date,
			"charge_type": "Extra Guests",
			"reservation": reservation.name,
			"description": (f"{guests} extra guests by arrangement"
			                if guests else "Extra guests by arrangement"),
			"qty": 1,
			"rate": over_cap,
			"amount": over_cap,
			"gst_rate": _nightly_gst(reservation, date),
			"auto_posted": 1,
		})
		posted = True

	if reservation.meal_plan and not _charge_posted(
			reservation.name, "Meal Plan", date):
		mp = frappe.get_doc("Meal Plan", reservation.meal_plan)
		locked = locked_night(reservation, date)
		if locked is not None:
			meal_amount = float(locked.meal_amount or 0)
		else:
			meal_amount = (
				(reservation.adults or 1) * float(mp.price_per_adult or 0)
				+ (reservation.children or 0) * float(mp.price_per_child or 0)
			)
		if meal_amount:
			_append_charge(folios, reservation, "Meal Plan", {
				"posting_date": date,
				"charge_type": "Meal Plan",
				"reservation": reservation.name,
				"description": f"{mp.label or mp.code} × {reservation.adults} adult(s)",
				"qty": 1,
				"rate": meal_amount,
				"amount": meal_amount,
				"gst_rate": _fnb_gst(reservation.property),
				"auto_posted": 1,
			})

	if own_batch:
		save_folios(folios)
	return posted


def _post_room_levy(folios, reservation, date, night_rate):
	"""The property's per-night levy on the room rate (municipality fee,
	city / tourism tax): a % of the night's rate and/or a fixed amount per
	occupied room-night (room_levy_per_night) - its own line so the invoice
	shows it and the ledger books it apart from room revenue."""
	prop = frappe.get_cached_doc("Property", reservation.property)
	pct = float(prop.get("room_levy_percent") or 0)
	fixed = float(prop.get("room_levy_per_night") or 0)
	levy = round(float(night_rate or 0) * pct / 100, 2) + round(fixed, 2)
	if levy <= 0 or _charge_posted(
			reservation.name, "Room Levy", date):
		return
	label = prop.get("room_levy_label") or "Room levy"
	parts = ([f"{pct:g}%"] if pct and night_rate else []) + (
		[f"{fixed:,g} per night"] if fixed else [])
	_append_charge(folios, reservation, "Room Levy", {
		"posting_date": date,
		"charge_type": "Room Levy",
		"reservation": reservation.name,
		"description": f"{label} {' + '.join(parts)}",
		"qty": 1,
		"rate": levy,
		"amount": levy,
		"gst_rate": _nightly_gst(reservation, date)
		            if int(prop.get("room_levy_taxable") if prop.get("room_levy_taxable") is not None else 1)
		            else 0,
		"auto_posted": 1,
	})


# A posted night is never re-priced in place: a later rate change reaches it
# only through these lines (kamra.settings "charged_night_rate_change"),
# dated the business date, with the night they belong to in stay_date.
RATE_ADJUSTMENT = "Rate Adjustment"


def charged_nights(reservation_name: str) -> dict:
	"""What the folios hold for each night of the stay already charged: the
	room line(s) posted for the night plus every rate adjustment made to it
	since, and the night's levy. {night: {base, tax, gst_rate, levy_base,
	levy_tax, folio}} - folio is where the night's room line went."""
	rows = frappe.db.sql(
		"""
		SELECT IF(fc.charge_type = %(adj)s, fc.stay_date, fc.posting_date) AS night,
		       fc.charge_type, fc.amount, fc.gst_rate, fc.parent
		FROM `tabFolio Charge` fc
		JOIN `tabFolio` f ON fc.parent = f.name
		WHERE (fc.reservation = %(res)s
		       OR (IFNULL(fc.reservation, '') = '' AND f.reservation = %(res)s))
		  AND """ + NOT_VOIDED + """
		  AND (fc.charge_type IN ('Room', 'Room Levy')
		       OR (fc.charge_type = %(adj)s AND fc.stay_date IS NOT NULL))
		ORDER BY fc.charge_type = 'Room' DESC, fc.amount DESC
		""", {"res": reservation_name, "adj": RATE_ADJUSTMENT}, as_dict=True)
	out: dict = {}
	for r in rows:
		key = str(r.night)
		amount = float(r.amount or 0)
		tax = amount * float(r.gst_rate or 0) / 100
		if r.charge_type == "Room Levy":
			if key in out:
				out[key]["levy_base"] += amount
				out[key]["levy_tax"] += tax
			continue  # a levy with no room line is not a charged night
		n = out.setdefault(key, {"base": 0.0, "tax": 0.0, "levy_base": 0.0,
		                         "levy_tax": 0.0, "folio": r.parent})
		n["base"] += amount
		n["tax"] += tax
	for n in out.values():
		n["base"] = round(n["base"], 2)
		n["gst_rate"] = round(n["tax"] / n["base"] * 100, 2) if n["base"] else 0.0
	return out


def post_rate_lines(reservation, lines: list) -> None:
	"""Post rate-change / rate-correction lines (from
	Reservation.hold_charged_nights) on the folio each night went to, dated
	the business date. Posted lines are never edited or deleted; a night
	whose folio has since been invoiced takes its line on the open folio."""
	from kamra.business_date import get_business_date
	day = get_business_date(reservation.property)
	folios: dict = {}
	for line in lines:
		name = line["folio"]
		if frappe.db.get_value("Folio", name, "status") == "Closed":
			name = open_line_target(reservation, "Room")
		if name not in folios:
			folios[name] = frappe.get_doc("Folio", name)
		folios[name].append("charges", {
			"posting_date": day,
			"stay_date": line["night"],
			"charge_type": RATE_ADJUSTMENT,
			"reservation": reservation.name,
			"description": line["description"],
			"qty": 1,
			"rate": line["amount"],
			"amount": line["amount"],
			"gst_rate": line["gst_rate"],
			"auto_posted": 1,
		})
	save_folios(folios)


def save_folios(folios: dict):
	"""Save each touched folio and post its new lines to the ledger."""
	from kamra.ledger import save_folio
	for doc in folios.values():
		_recalculate(doc)
		save_folio(doc)


def carried_forward_until(reservation_name: str):
	"""For a stay that was in house at go-live: the business date its
	opening balance was posted. Nights before it were billed by the old
	system and are inside that balance. None for every other stay."""
	cutoff = frappe.db.sql(
		"""
		SELECT MAX(fc.posting_date) FROM `tabFolio Charge` fc
		JOIN `tabFolio` f ON fc.parent = f.name
		WHERE fc.charge_type = 'Opening Balance' AND f.folio_type = 'Guest'
		  AND (fc.reservation = %(res)s
		       OR (IFNULL(fc.reservation, '') = '' AND f.reservation = %(res)s))
		""", {"res": reservation_name})[0][0]
	return getdate(cutoff) if cutoff else None


def post_remaining_nights(reservation) -> int:
	"""At checkout: make sure every night of the stay is on its folio(s) -
	from go-live on, for a stay carried forward from the old system."""
	open_folio(reservation)  # guest folio always exists after checkout
	folios: dict = {}
	posted = 0
	date = getdate(reservation.check_in_date)
	cutoff = carried_forward_until(reservation.name)
	if cutoff and cutoff > date:
		date = cutoff
	end = getdate(reservation.check_out_date)
	if getattr(reservation, "is_day_use", 0) and end == date:
		end = getdate(add_days(date, 1))  # day-use bills its one date
	while date < end:
		if post_room_night(reservation, date, folios):
			posted += 1
		date = getdate(add_days(date, 1))
	save_folios(folios)
	return posted


def split_folio(reservation: str, folio_type: str = "Extra") -> str:
	"""Open an additional folio for a stay (Extra or Company) so charges
	can be routed/split - e.g. 70/30 corporate vs personal.

	If an empty folio of this type already exists, reuse it rather than
	piling up blank duplicates - you only ever need one unused split open."""
	res = frappe.get_doc("Reservation", reservation)
	for f in frappe.get_all(
		"Folio",
		filters={"reservation": reservation, "folio_type": folio_type,
		         "status": "Open"},
		fields=["name", "grand_total", "payments_total"],
	):
		if not f.grand_total and not f.payments_total:
			return f.name
	# a company folio bills the stay's company as its window's payee
	company = res.get("company") if folio_type == "Company" else None
	return open_window(reservation=res.name, folio_type=folio_type,
	                   payee_type="Company" if company else "Guest",
	                   payee_company=company)


def transfer_charge(from_folio: str, charge_row: str, to_folio: str):
	"""Move one charge line between two open folios of the same stay."""
	transfer_charges(from_folio, [charge_row], to_folio)


def _folio_group(folio) -> str | None:
	if folio.get("group_booking"):
		return folio.group_booking
	return folio.get("reservation") and frappe.db.get_value(
		"Reservation", folio.reservation, "group_booking")


def _same_stay_or_group(src, dst) -> bool:
	# two folios with no stay (posting masters, account invoices) are not
	# one stay: only their group can make them one bill
	if src.reservation and src.reservation == dst.reservation:
		return True
	sg, dg = _folio_group(src), _folio_group(dst)
	return bool(sg and sg == dg)


def _assert_same_stay(src, dst):
	"""Charges may move within a stay, or within a group - company pays
	some rooms' charges on the master, guests settle the rest."""
	if not _same_stay_or_group(src, dst):
		frappe.throw("Folios belong to different stays.")


def transfer_charges(from_folio: str, charge_rows: list, to_folio: str):
	"""Move several charge lines at once - one save per folio, so a
	corporate re-bill of a whole stay is a single operation."""
	src = frappe.get_doc("Folio", from_folio)
	dst = frappe.get_doc("Folio", to_folio)
	_assert_same_stay(src, dst)
	move_charges(src, dst, charge_rows)


def move_charges(src, dst, charge_rows: list, reservation: str | None = None,
                 key: str | None = None, routing_instruction: str | None = None):
	"""Take charge lines off one open folio and put them on another, ledger
	and all. Callers decide whose bill a line may move to: transfer_charges
	keeps it within a stay or group, pos.move_room_bill to the right room.
	`reservation` re-points the lines at another stay."""
	if "Closed" in (src.status, dst.status):
		frappe.throw("Both folios must be open to transfer charges.")
	from kamra.ledger import ledger_charges, reverse_ledger_for_charge
	moved = []
	# a voided line and its reversal (a split's too) net to nothing and stay
	# on the folio they were posted to, as the bridge's void fences need: a
	# move of the whole bill leaves them, and moves what stands in their place
	pairs = void_pairs(src)
	if charge_rows and all(r in pairs for r in charge_rows):
		frappe.throw("A voided line and its reversal stay on the folio they were posted to.")
	for charge_row in charge_rows:
		if charge_row in pairs:
			continue
		row = next((c for c in src.charges if c.name == charge_row), None)
		if not row:
			frappe.throw(f"Charge {charge_row} not found on {src.name}.")
		# an outlet charge (the bridge's source_ref) was fiscalised at the
		# outlet: it moves whole and only within its stay or group, so the
		# outlet's own record still names the right bill
		if row.get("source_ref") and not _same_stay_or_group(src, dst):
			frappe.throw(frappe._("An outlet charge moves only between bills of its own "
			                      "stay or group. Correct the room at the outlet."))
		dst.append("charges", {
			"posting_date": row.posting_date,
			"stay_date": row.get("stay_date"),
			"charge_type": row.charge_type,
			"reservation": reservation or row.get("reservation"),
			"description": row.description,
			"qty": row.qty,
			"rate": row.rate,
			"amount": row.amount,
			"gst_rate": row.gst_rate,
			"auto_posted": row.auto_posted,
			"is_alcohol": row.get("is_alcohol"),
			# the line keeps the codes it was posted with
			"transaction_code": row.get("transaction_code"),
			"gl_code": row.get("gl_code"),
			# and its outlet reference, so the hotel invoice never bills an
			# outlet charge again (the source row is saved away first)
			"source_ref": row.get("source_ref"),
			# routed there now (pos.move_room_bill); a hand move is no routing
			"routing_instruction": routing_instruction,
			# a split part keeps its lineage and the tax it was split with
			"split_of": row.get("split_of"),
			"tax_as_posted": row.get("tax_as_posted"),
			"gst_amount": row.gst_amount,
			"idempotency_key": key,
		})
		moved.append(dst.charges[-1])
		src.charges.remove(row)
		# the line leaves this folio's ledger and joins the target's
		reverse_ledger_for_charge(src.property, src.name, row.name,
		                          f"to {dst.name}", prefix="TRANSFER")
	_recalculate(src)
	_recalculate(dst)
	src.save(ignore_permissions=True)
	dst.save(ignore_permissions=True)
	ledger_charges(dst, moved)
	return moved


def split_charge(from_folio: str, charge_row: str, to_folio: str,
                 percent: float | None = None,
                 amount: float | None = None, credit: bool = False,
                 qty: float | None = None) -> dict:
	"""Split one charge line in two - the 70/30 corporate deal, the shared
	room, the disputed minibar - as OPERA does: by a percent of the line, an
	amount (before tax) or a number of its units. Both parts may stay on
	this folio, or one may go to another open folio of the same stay or
	group.

	The posted line is never rewritten. A reversal voids it (reversal_of,
	the void pair the ERPNext bridge's KMR-VOID-001 fences admit) and the two
	parts are posted new as ordinary lines, each naming the line it was split
	from (split_of), so either part can be corrected, voided, moved or split
	again. All three keep the line's description, so a banquet (EVT-) or
	outlet (ORD-) line is still recognised by the reports.

	The part split off is worked out to the currency's minor unit, and so is
	its tax; the line keeps the rest of both, so gross and tax add up exactly
	across the bills (tax_as_posted). A room night priced with tax included
	is split on its price, the figure the guest was quoted.

	Refused, as OPERA refuses them: a payment, a package line (the meal plan
	posted with the room) and an outlet charge (the bridge's source_ref),
	which moves only whole (move_charges); and an amount or percent that is
	not above zero. credit=True splits an allowance line instead (both parts
	stay negative); only the server does that, following the charges it
	wrote down."""
	src = frappe.get_doc("Folio", from_folio)
	dst = _split_target(src, to_folio)
	row, sign, line = _split_line(src, charge_row, credit)
	units = Decimal(str(row.qty or 1))
	part_units = Decimal(1)
	whole = _split_whole(line)
	if qty:
		part_units = Decimal(str(qty))
		if units <= 1:
			frappe.throw(frappe._("This line is one unit: split it by amount or percent."))
		if part_units != part_units.to_integral_value() or not 1 <= part_units < units:
			frappe.throw(frappe._("Split off between 1 and {0} of its {1} units.").format(
				int(units) - 1, f"{units.normalize():f}"))
		part, kept = split_figure(line, line["figure"] * part_units / units)
	elif percent is not None:
		part, kept = split_figure(line, line["figure"] * Decimal(str(percent)) / 100)
	elif amount is not None:
		if Decimal(str(amount)) <= 0:
			frappe.throw(frappe._("Give an amount above zero to split off."))
		if line["priced_with_tax"]:
			base = Decimal(str(amount)).quantize(line["unit"], rounding=ROUND_HALF_UP)
			part = (base, (base * line["rate"]).quantize(line["unit"], rounding=ROUND_HALF_UP))
			kept = (whole[0] - part[0], whole[1] - part[1])
		else:
			part, kept = split_figure(line, amount)
	else:
		frappe.throw("Give a percent, an amount or a quantity to split off.")
	if whole != cut_figure(line, line["figure"]):
		kept = (whole[0] - part[0], whole[1] - part[1])
	if not 0 < _figure(line, part) < line["figure"]:
		from kamra.localization import money_text
		frappe.throw(f"Split must be between 0 and {money_text(src.property, line['figure'])} (exclusive).")
	return _post_split(src, row, sign, line, [(dst, part, part_units)],
	                   units - part_units if qty else Decimal(1), kept=kept)


def _split_target(src, to_folio: str):
	"""The folio a part goes to: this one, or an open one of its stay or group."""
	dst = src if to_folio == src.name else frappe.get_doc("Folio", to_folio)
	if dst is not src:
		_assert_same_stay(src, dst)
	if "Closed" in (src.status, dst.status):
		frappe.throw("Both folios must be open to split charges.")
	return dst


def _split_line(src, charge_row: str, credit: bool):
	"""The line to split, refused where OPERA refuses a split, and its
	figures (magnitudes; `sign` puts an allowance's back): the base and tax
	as they stand, the rate, the currency's minor unit and the figure a split
	cuts - the price for a night priced with tax included, else the base."""
	row = next((c for c in src.charges if c.name == charge_row), None)
	if not row:
		if any(p.name == charge_row for p in src.payments):
			frappe.throw(frappe._("A payment can't be split. Refund it or move the "
			                      "charges instead."))
		frappe.throw(f"Charge {charge_row} not found on {src.name}.")
	if row.name in void_pairs(src):
		frappe.throw("A voided line and its reversal can't be split.")
	if row.get("source_ref"):
		frappe.throw(frappe._("An outlet charge can't be split. Move it whole to the "
		                      "other bill of the stay or group."))
	if row.charge_type == "Meal Plan":
		frappe.throw(frappe._("A package line (the meal plan posted with the room) "
		                      "can't be split."))
	sign = -1 if credit else 1
	base = Decimal(str(row.amount or 0)) * sign
	if base <= 0 or (credit and row.charge_type != "Allowance"):
		frappe.throw("Only positive charge lines can be split.")
	from kamra.ledger import minor_unit
	tax = _line_tax(row, Decimal(str(row.amount or 0)),
	                minor_unit(src.property) if src.get("property") else None) * sign
	return row, sign, line_figures(src.property, row.charge_type, base, tax,
	                               row.gst_rate)


def priced_with_tax(property: str, charge_type: str) -> bool:
	"""A room night at a property whose rates include tax is priced with its
	tax (OPERA: an inclusive transaction code): it is split, and a routing
	limit caps it, on its price. Every other line on its base."""
	from kamra.pricing import rates_include_tax
	return charge_type == "Room" and bool(rates_include_tax(property))


def line_figures(property: str, charge_type: str, base, tax=None, gst_rate=0) -> dict:
	"""The figures one splitter cuts a line by - split_charge, share_charges
	and routing limits alike: its base and tax as they stand (tax worked out
	from the rate when not given), the rate, the currency's minor unit and the
	figure that is cut - the price for a line priced with tax, else the base."""
	from kamra.ledger import minor_unit
	base = Decimal(str(base or 0))
	rate = Decimal(str(gst_rate or 0)) / 100
	tax = base * rate if tax is None else Decimal(str(tax))
	unit = minor_unit(property)
	with_tax = priced_with_tax(property, charge_type)
	return {"base": base, "tax": tax, "rate": rate, "unit": unit,
	        "priced_with_tax": with_tax,
	        "figure": ((base + tax) if with_tax else base).quantize(unit, rounding=ROUND_HALF_UP)}


def _cut(line: dict, share: Decimal) -> tuple[Decimal, Decimal]:
	"""`share` of the line, to the minor unit, with its own tax to the minor
	unit: (base, tax). A line priced with tax included is cut on its price."""
	return cut_figure(line, line["figure"] * share)


def cut_figure(line: dict, figure) -> tuple[Decimal, Decimal]:
	"""The part of the line whose figure (its price, or its base) is
	`figure`, to the minor unit, with its own tax to the minor unit:
	(base, tax). The line keeps the rest of both."""
	unit, rate = line["unit"], line["rate"]
	part = Decimal(str(figure)).quantize(unit, rounding=ROUND_HALF_UP)
	if line["priced_with_tax"]:
		tax = (part * rate / (1 + rate)).quantize(unit, rounding=ROUND_HALF_UP)
		return part - tax, tax
	return part, (part * rate).quantize(unit, rounding=ROUND_HALF_UP)


def _figure(line: dict, part: tuple[Decimal, Decimal]) -> Decimal:
	return part[0] + part[1] if line["priced_with_tax"] else part[0]


def split_figure(line: dict, part_figure) -> tuple[tuple[Decimal, Decimal], tuple[Decimal, Decimal]]:
	"""Split `line` into (part, remainder), both quantized to the currency's
	minor unit so gross, tax and net of the two parts sum exactly to the
	quantized whole line (no sub-shilling / sub-minor-unit remainders)."""
	whole = cut_figure(line, line["figure"])
	part = cut_figure(line, part_figure)
	if _figure(line, part) >= line["figure"]:
		return whole, (Decimal(0), Decimal(0))
	if _figure(line, part) <= 0:
		return (Decimal(0), Decimal(0)), whole
	return part, (whole[0] - part[0], whole[1] - part[1])


def _split_whole(line: dict) -> tuple[Decimal, Decimal]:
	"""Whole (base, tax) to conserve across split parts: quantized to the
	currency's minor unit via cut_figure, except when a tax-inclusive room
	night carries a sub-unit net locked by pricing.night_rate_places."""
	if line["priced_with_tax"] and line["base"] != line["base"].quantize(line["unit"]):
		return line["base"], line["tax"]
	return cut_figure(line, line["figure"])


def _post_split(src, row, sign: int, line: dict, cuts: list, kept_units: Decimal,
                kept: tuple[Decimal, Decimal] | None = None) -> dict:
	"""Void the line and post its parts: each cut (folio, (base, tax), units)
	on its folio, and the rest of the line's base and tax on this one."""
	same_line = {
		"posting_date": row.posting_date,
		"stay_date": row.get("stay_date"),
		"charge_type": row.charge_type,
		"reservation": row.get("reservation"),
		"description": row.description,
		"allowance_serial": row.get("allowance_serial"),
		"gst_rate": row.gst_rate,
		"auto_posted": row.auto_posted,
		"is_alcohol": row.get("is_alcohol"),
		"transaction_code": row.get("transaction_code"),
		"gl_code": row.get("gl_code"),
		"split_of": row.name,
	}
	names = ", ".join(dict.fromkeys(f.name for f, _p, _u in cuts if f is not src))
	# the posted line stays; its reversal voids it on its own date, tax and all
	src.append("charges", {
		**same_line,
		"qty": row.qty,
		"rate": -float(row.rate or 0),
		"amount": -float(row.amount or 0),
		"tax_as_posted": row.get("tax_as_posted"),
		"gst_amount": -float(row.gst_amount or 0),
		"reversal_of": row.name,
		"void_reason": f"Split to {names}" if names else "Split",
		"voided_by": frappe.session.user,
	})
	reversal = src.charges[-1]
	if kept is not None:
		kept_base, kept_tax = kept
	else:
		whole = _split_whole(line)
		kept_base = whole[0] - sum(p[0] for _f, p, _u in cuts)
		kept_tax = whole[1] - sum(p[1] for _f, p, _u in cuts)

	def part_line(base, tax, units, folio):
		# a part left on the folio a routing instruction put the line on is
		# still routed by it, so its usage follows; a part moved off is not
		routed = row.get("routing_instruction") if folio is src else None
		return {**same_line, "qty": float(units), "rate": float(base * sign / units),
		        "amount": float(base * sign), "gst_amount": float(tax * sign),
		        "tax_as_posted": 1, "routing_instruction": routed}

	src.append("charges", part_line(kept_base, kept_tax, kept_units, src))
	kept = src.charges[-1]
	folios, moved = {src.name: src}, []
	for f, (base, tax), units in cuts:
		f = folios.setdefault(f.name, f)
		f.append("charges", part_line(base, tax, units, f))
		moved.append((f, f.charges[-1]))
	for f in folios.values():
		_recalculate(f)
		f.save(ignore_permissions=True)
	# the reversal takes the posted line off the ledger; each part is posted
	# on the ledger of the folio it is on
	from kamra.ledger import _original_charge_entry, ledger_charges, record_charge_ledger
	first = _original_charge_entry(src.name, row.name)
	if first:
		record_charge_ledger(src, reversal.as_dict(), reversal_of=first.name)
	ledger_charges(src, [kept])
	for f, part in moved:
		ledger_charges(f, [part])
	return {"kept": float(kept_base * sign),
	        "moved": float(sum(p[0] for _f, p, _u in cuts) * sign),
	        "reversal": reversal.name, "kept_row": kept.name,
	        "moved_row": moved[0][1].name, "moved_rows": [p.name for _f, p in moved]}


def open_guest_bills(reservation: str, names: list | None = None) -> list[dict]:
	"""A separate bill for each guest sharing the room. The main guest keeps
	the stay's Guest folio; everyone else gets an Extra folio made out to
	them (bill_to_name), so each bill is invoiced in that guest's name.
	`names` defaults to the stay's other occupants. Asking twice for the
	same name returns the bill that is already open."""
	res = frappe.get_doc("Reservation", reservation)
	if res.status != "Checked In":
		frappe.throw(frappe._("Separate bills open once the guests are checked in."))
	main = (res.guest_name or "").strip().lower()
	if names is None:
		names = [o.full_name for o in res.get("occupants") or []]
	wanted = []
	for n in names:
		n = (n or "").strip()
		if n and n.lower() != main and n.lower() not in {w.lower() for w in wanted}:
			wanted.append(n)
	if not wanted:
		frappe.throw(frappe._("Add the other guests' names to the stay first."))
	out = []
	for n in wanted:
		existing = frappe.db.get_value(
			"Folio", {"reservation": res.name, "folio_type": "Extra",
			          "status": "Open", "bill_to_name": n})
		if not existing:
			existing = open_window(reservation=res.name, folio_type="Extra",
			                       payee_type="Guest", bill_to_name=n)
		out.append({"folio": existing, "name": n})
	return out


def share_charges(from_folio: str, charge_rows: list, to_folios: list,
                  shares: list | None = None) -> dict:
	"""Share charge lines (the room nights, usually) between the guests'
	bills: each line is cut into one part per target bill, evenly by
	default or by `shares` (a percent per target), and the source bill
	keeps the rest. Each line is split once (split_charge's trail: one
	reversal, the parts and the rest), every part a share of the line's
	figure - its price, for a night priced with tax included - to the
	currency's minor unit with its tax, so the bills add up to the line
	exactly. A part that rounds to nothing, or would take all that is left
	(0.02 three ways), is skipped."""
	to_folios = [f for f in to_folios if f and f != from_folio]
	if not to_folios or len(set(to_folios)) != len(to_folios):
		frappe.throw(frappe._("Pick the other guests' bills to share with."))
	# a line listed twice would be cut twice: more than its share leaves
	charge_rows = list(dict.fromkeys(charge_rows or []))
	if not charge_rows:
		frappe.throw(frappe._("Pick the charges to share."))
	if shares:
		shares = [Decimal(str(x)) for x in shares]
		if len(shares) != len(to_folios) or any(x <= 0 for x in shares) \
				or sum(shares) >= 100:
			frappe.throw(frappe._("Give each other bill a share above 0%; "
			                      "together under 100%."))
		shares = [x / 100 for x in shares]
	else:
		shares = [Decimal(1) / (len(to_folios) + 1)] * len(to_folios)
	src = frappe.get_doc("Folio", from_folio)
	present = {c.name: c for c in src.charges}
	for row in charge_rows:
		if row not in present:
			frappe.throw(frappe._("Charge {0} is not on {1}.").format(row, from_folio))
		if Decimal(str(present[row].amount or 0)) <= 0:
			frappe.throw(frappe._("Only charges above zero can be shared."))
	moved = Decimal(0)
	for row in charge_rows:
		src = frappe.get_doc("Folio", from_folio)
		targets = [_split_target(src, f) for f in to_folios]
		line_row, sign, line = _split_line(src, row, False)
		whole = _split_whole(line)
		rem_base, rem_tax = whole
		cuts, left = [], line["figure"]
		for dst, share in zip(targets, shares):
			part = cut_figure(line, line["figure"] * share)
			if 0 < _figure(line, part) < left:
				if not line["priced_with_tax"] and part[1] > rem_tax:
					part = (part[0], rem_tax)
				cuts.append((dst, part, Decimal(1)))
				left -= _figure(line, part)
				rem_base -= part[0]
				rem_tax -= part[1]
		if cuts:
			moved += Decimal(str(_post_split(
				src, line_row, sign, line, cuts, Decimal(1),
				kept=(rem_base, rem_tax))["moved"]))
	return {"lines": len(charge_rows), "moved": float(moved)}


def percent_fee(reservation, pct: float) -> float:
	"""₹ for a cancellation band expressed as a percentage of the stay.

	A tiered policy says "half back between three and seven days", which is
	neither one night nor the whole stay, so it cannot be said with a fee
	basis. Pre-tax; GST rides on the folio line.
	"""
	return round(float(reservation.amount_before_tax or 0)
	             * float(pct) / 100.0, 2)


def policy_fee(reservation, basis: str, pct: float | None = None) -> float:
	"""₹ for a cancellation/no-show fee basis: 'First Night' or
	'Full Stay' (pre-tax; GST rides on the folio line).

	`pct` defers to percent_fee(), so a caller holding a tiered band can use
	the one entry point the posting path already calls.
	"""
	if pct is not None:
		return percent_fee(reservation, pct)
	if basis == "First Night":
		return float(_nightly_room_rate(reservation,
		                                reservation.check_in_date))
	if basis == "Full Stay":
		return float(reservation.amount_before_tax or 0)
	return 0.0


def post_policy_fee(reservation, basis: str, label: str,
                    pct: float | None = None,
                    posting_date: str | None = None) -> float:
	"""Open the guest folio and post a policy fee (no-show / late
	cancellation). Returns the pre-tax amount posted.

	`pct` posts that percentage of the stay, for a tiered policy.
	`posting_date` is the business day the fee belongs to: the night audit
	passes the date it is closing; left out (a cancellation at the desk) it
	is the property's current business date - never the calendar, which
	runs a day ahead between midnight and the audit."""
	amount = policy_fee(reservation, basis, pct=pct)
	if amount <= 0:
		return 0.0
	folio = frappe.get_doc("Folio", open_folio(reservation))
	if folio.status == "Closed":
		return 0.0
	already = any(c.description == label for c in folio.charges)
	if already:
		return 0.0
	if not posting_date:
		from kamra.business_date import get_business_date
		posting_date = get_business_date(reservation.property)
	folio.append("charges", {
		"posting_date": str(posting_date),
		"charge_type": "Misc",
		"reservation": reservation.name,
		"description": label,
		"qty": 1,
		"rate": amount,
		"amount": amount,
		"gst_rate": _room_gst(reservation),
		"auto_posted": 1,
	})
	_recalculate(folio)
	from kamra.ledger import save_folio, transfer_advances_to_guest
	save_folio(folio)
	# the advance now pays the fee: it leaves the Deposit ledger (money held
	# for an arrival) for the Guest ledger, as at check-in. Left behind, a
	# no-show's advance showed as still held while its folio read settled.
	transfer_advances_to_guest(reservation.name, business_date=str(posting_date))
	return amount


def close_folio(folio_name: str) -> str:
	"""Close the folio and assign the GST invoice number.

	A folio closes only when it is settled: what the guest owes is paid, what
	the company owes has moved to the city ledger (transfer_to_city_ledger
	zeroes it with a Company Credit payment), and nothing is owed back.
	Closing with money on it was how a guest left owing."""
	folio = frappe.get_doc("Folio", folio_name)
	if folio.status == "Closed":
		return folio.invoice_number
	_recalculate(folio)
	balance = float(folio.balance or 0)
	if abs(balance) > 0.01:
		from kamra.localization import pack_for
		symbol = pack_for(folio.property).locale(
			frappe.get_cached_doc("Property", folio.property)).get(
			"currency_symbol", "")
		if balance > 0:
			frappe.throw(frappe._(
				"Cannot close the folio: {0}{1:,.2f} is still owed. Collect "
				"payment, or move the company's share to the city ledger, "
				"first.").format(symbol, balance))
		frappe.throw(frappe._(
			"Cannot close the folio: {0}{1:,.2f} is owed back to the guest. "
			"Issue the refund first.").format(symbol, -balance))
	from frappe.model.naming import make_autoname

	folio.status = "Closed"
	folio.closed_on = now_datetime()
	# one series per property: each GSTIN must have its own unique
	# invoice series, and GST caps invoice numbers at 16 chars - a short
	# initials code keeps INV-KDP-26-00001 inside the limit
	code = "".join(w[0] for w in folio.property.split())[:3].upper()
	folio.invoice_number = make_autoname(f"INV-{code}-.YY.-.#####")
	_recalculate(folio)
	folio.save(ignore_permissions=True)
	# a company bill moved to the city ledger at checkout, before it had a
	# number: the account's bill takes the invoice number now
	frappe.db.set_value("City Ledger Entry", {
		"folio": folio.name, "kind": "Invoice", "invoice_number": ("is", "not set")},
		"invoice_number", folio.invoice_number)
	# statutory e-invoicing (ZATCA in Saudi Arabia) - the pack decides
	from kamra import localization as loc
	loc.on_invoice_issued(loc.pack_for(folio.property), folio.name)
	return folio.invoice_number


def _business_date(property: str) -> str:
	from kamra.business_date import get_business_date
	return str(get_business_date(property))


def post_allowance(folio_name: str, amount: float, reason: str,
                   gst_rate: float = 0, approver: str | None = None,
                   requested_by: str | None = None,
                   serial: str | None = None) -> str:
	"""A negative adjustment against a specific folio - the hotel writes off
	part of a charge (service recovery, dispute) WITHOUT touching the original
	line, so the trail stays honest. GST reverses at the same rate as the
	charge being adjusted."""
	folio = frappe.get_doc("Folio", folio_name)
	if folio.status == "Closed":
		frappe.throw("This folio is settled - pass the allowance on the "
		             "guest's open folio, or cancel the invoice first.")
	amount = abs(float(amount))
	if not amount:
		frappe.throw("Allowance amount is required.")
	if not (reason or "").strip():
		frappe.throw("An allowance needs a reason - it goes on the record.")
	folio.append("charges", {
		# the business date, like every other folio line: the calendar runs a
		# day ahead between midnight and the audit
		"posting_date": _business_date(folio.property),
		"charge_type": "Allowance",
		"description": reason.strip(),
		"allowance_serial": serial,
		"qty": 1,
		"rate": -amount,
		"amount": -amount,
		"gst_rate": float(gst_rate or 0),
	})
	_recalculate(folio)
	from kamra.ledger import save_folio
	save_folio(folio)
	from kamra.localization import money_text
	from kamra.savings import log_action
	log_action("post_allowance", "Folio", folio.name, folio.property,
	           rationale=f"Allowance {money_text(folio.property, amount)} - {reason.strip()}"
	                     + (f" (asked by {requested_by})"
	                        if requested_by and requested_by != frappe.session.user
	                        else ""),
	           approver=approver,
	           after_snapshot={"serial": serial} if serial else None)
	return folio.name


def void_charge(folio_name: str, charge_row: str, reason: str = "", *, correction: dict | None = None) -> dict:
	"""Void a wrong charge line on an OPEN folio - the correction path for
	a mis-posted bill (wrong amount, duplicate, wrong guest). The line stays
	on the folio and a line of the opposite sign follows it (same charge
	type and transaction code, the reason and who voided it), posted to the
	ledger like any other line, so the pair nets to zero and the bill keeps
	its trail (OPERA's adjustment). Once the folio is invoiced, use an
	allowance instead. Every void is logged with who and why."""
	folio = frappe.get_doc("Folio", folio_name)
	if folio.status == "Closed":
		frappe.throw("This folio is settled - post an allowance to reverse a "
		             "charge, or cancel the invoice first.")
	if not (reason or "").strip():
		frappe.throw("A void needs a reason - it goes on the record.")
	row = next((c for c in folio.charges if c.name == charge_row), None)
	if not row:
		frappe.throw("That charge line is not on this folio.")
	if row.get("reversal_of"):
		frappe.throw("This line reverses a void and can't be voided. Post the "
		             "charge again if it was right.")
	if voided_rows(folio).get(row.name):
		frappe.throw("This line is already voided.")
	removed = {"charge_type": row.charge_type, "description": row.description,
	           "amount": row.amount}
	line = {**removed, "row": row.name, "posting_date": row.posting_date,
	        "gst_rate": row.gst_rate, "reservation": row.get("reservation")}
	from kamra.business_date import get_business_date
	from kamra.ledger import _original_charge_entry, code_for_charge_type, record_charge_ledger
	folio.append("charges", {
		"posting_date": get_business_date(folio.property),
		"stay_date": row.get("stay_date"),
		"charge_type": row.charge_type,
		"transaction_code": row.get("transaction_code")
		or code_for_charge_type(row.charge_type),
		"reservation": row.get("reservation"),
		"description": f"VOID: {row.description or row.charge_type} - {reason.strip()}"[:140],
		"allowance_serial": row.get("allowance_serial"),
		"qty": row.qty,
		"rate": -float(row.rate or 0),
		"amount": -float(row.amount or 0),
		"gst_rate": row.gst_rate,
		# a line split to the minor unit is voided with the tax it was posted with
		"tax_as_posted": row.get("tax_as_posted"),
		"gst_amount": -float(row.gst_amount or 0),
		"auto_posted": row.auto_posted,
		"is_alcohol": row.get("is_alcohol"),
		"reversal_of": row.name,
		"void_reason": reason.strip(),
		"voided_by": frappe.session.user,
		**(correction or {}),
	})
	_recalculate(folio)
	folio.save(ignore_permissions=True)
	# the reversal line's own ledger entry, linked to the original's; a line
	# that never reached the ledger (posted before it existed) takes none,
	# so the folio and its ledger stay equal
	first = _original_charge_entry(folio.name, row.name)
	if first:
		record_charge_ledger(folio, folio.charges[-1].as_dict(), reversal_of=first.name)
	from kamra.webhooks import emit_charge_voided
	emit_charge_voided(folio, line, reason or "")
	from kamra.localization import money_text
	from kamra.savings import log_action
	log_action("void_charge", "Folio", folio.name, folio.property,
	           rationale=f"Voided {removed['charge_type']} "
	                     f"{removed['description']} "
	                     f"{money_text(folio.property, removed['amount'])}"
	                     + (f" - {reason}" if reason else ""))
	return {"folio": folio.name, "removed": removed, "reversal": folio.charges[-1].name,
	        "balance": folio.balance}


def voided_rows(folio) -> dict:
	"""{voided line: its reversal line} on a loaded folio."""
	return {c.reversal_of: c.name for c in folio.charges if c.get("reversal_of")}


def void_pairs(folio) -> set:
	"""Every line of a void pair on a loaded folio: each voided (or split)
	line and its reversal. Together they net to nothing."""
	return {n for c in folio.charges if c.get("reversal_of") for n in (c.name, c.reversal_of)}


def part_settle(folio_name: str) -> dict:
	"""Interim invoice mid-stay: freeze the fully-paid folio with a real
	invoice number and open a fresh one so the stay keeps running - the
	long-stay pattern (guests settling every N days without checking out).
	The room stays occupied; only the bill closes."""
	folio = frappe.get_doc("Folio", folio_name)
	if folio.status == "Closed":
		frappe.throw("This folio is already settled.")
	_recalculate(folio)
	if round(folio.balance or 0, 2) != 0:
		frappe.throw("Settle the balance first - an interim invoice needs "
		             "the folio fully paid.")
	if not folio.charges:
		frappe.throw("Nothing to invoice on this folio.")
	invoice = close_folio(folio.name)
	fresh = frappe.get_doc({
		"doctype": "Folio",
		"property": folio.property,
		"reservation": folio.reservation,
		"guest": folio.guest,
		"folio_type": folio.folio_type,
		"status": "Open",
		"opened_on": now_datetime(),
		"group_booking": folio.get("group_booking"),
		# the same window and payee go on billing
		"window_no": folio.get("window_no"),
		"bill_to_name": folio.get("bill_to_name"),
		**{f: folio.get(f) for f in WINDOW_FIELDS},
	})
	_recalculate(fresh)
	fresh.insert(ignore_permissions=True)
	return {"invoice_number": invoice, "closed_folio": folio.name,
	        "new_folio": fresh.name}


CREDIT_AND_REBILL = "hospitality_kamra.credit_and_rebill"


def bridge_invoice(folio) -> str | None:
	"""The Sales Invoice the ERPNext bridge issued for this folio (the
	bridge's sales_invoice field), or None. That invoice is corrected only
	by the bridge's credit and re-bill - OPERA's Credit Bill, SAP's VF11
	cancellation, a URA EFRIS credit note - never reopened or credited here
	on its own."""
	return folio.get("sales_invoice") or None


def invoiced(folio) -> bool:
	"""The bill was invoiced: here (invoice_number) or by the bridge, whose
	check-out close sets only its Sales Invoice (sales_invoice)."""
	return bool(folio.get("invoice_number") or bridge_invoice(folio))


def refuse_bridge_correction(folio):
	frappe.throw(frappe._(
		"Invoice {0} was issued by the accounting system as {1}. Correct it there "
		"with a credit note and re-bill; this bill is not reopened or credited here."
	).format(folio.invoice_number or folio.name, bridge_invoice(folio)))


def route_to_credit_and_rebill(folio, reason: str, amount: float | None = None):
	"""Hand a correction of a bridge-issued invoice to the bridge's credit
	and re-bill (`amount` None: the whole invoice). Refused when the bridge
	has no such flow: nothing is corrected here instead."""
	from kamra.erp_bridge import get_bridge_script, run_bridge_script
	script = get_bridge_script(CREDIT_AND_REBILL)
	if not script:
		refuse_bridge_correction(folio)
	return run_bridge_script(script, {"folio": folio.name, "reason": reason,
	                                  "amount": amount})


def cancel_invoice(folio_name: str, reason: str) -> dict:
	"""Void a generated invoice the auditable way: the number goes to the
	Cancelled Invoice register (finance never loses a bill in the sequence,
	and the old bill stays printable), then the folio reopens so it can be
	corrected and re-closed under a fresh number. An invoice the ERPNext
	bridge issued is never reopened here (bridge_invoice)."""
	folio = frappe.get_doc("Folio", folio_name)
	if bridge_invoice(folio):
		refuse_bridge_correction(folio)
	if folio.status != "Closed" or not folio.invoice_number:
		frappe.throw("Only a settled folio with an invoice can be cancelled.")
	if folio.folio_type == "Account":
		# reopened, an account invoice has no stay to correct it from and
		# nothing would reissue it: the AR way out is a credit note
		frappe.throw("An account invoice can't be cancelled - issue a credit "
		             "note against it instead.")
	if not (reason or "").strip():
		frappe.throw("A cancellation needs a reason - it goes on the record.")
	frappe.get_doc({
		"doctype": "Cancelled Invoice",
		"property": folio.property,
		"folio": folio.name,
		"invoice_number": folio.invoice_number,
		"grand_total": folio.grand_total,
		"cancelled_on": now_datetime(),
		"reason": reason.strip(),
	}).insert(ignore_permissions=True)
	old = folio.invoice_number
	# the company's account stops quoting the cancelled number; the bill
	# takes its new one when the folio closes again
	frappe.db.set_value("City Ledger Entry", {
		"folio": folio.name, "kind": "Invoice", "invoice_number": old},
		"invoice_number", None)
	folio.invoice_number = None
	folio.status = "Open"
	folio.closed_on = None
	frappe.flags.kamra_invoice_cancel = True
	try:
		folio.save(ignore_permissions=True)
	finally:
		frappe.flags.kamra_invoice_cancel = False
	# where the authority forbids cancelling (ZATCA), this issues the
	# credit note that reverses the invoice
	from kamra import localization as loc
	loc.on_invoice_cancelled(loc.pack_for(folio.property), folio.name, old,
	                         reason.strip())
	return {"cancelled": old, "folio": folio.name}


def apply_no_show(res, pol, decision, business_date) -> dict:
	"""Mark one stay No Show and apply the property's no-show fee policy
	(`decision` from kamra.no_show.audit_outcome): the fee is posted on
	the business date being closed, or waived for an OTA booking. Used by
	the night audit and by the End of Day "Mark no-show" action, so both
	charge the same way. Returns the audit pack's no-show row."""
	before = res.status
	frappe.db.set_value("Reservation", res.name, "status", "No Show")
	# a direct status write skips the Reservation doc events, so tell the
	# webhook subscribers here
	from kamra.webhooks import emit, reservation_payload
	emit("reservation.modified", res.property,
	     {**reservation_payload(res), "status": "No Show",
	      "changes": {"status": [before, "No Show"]}})
	# and the bell, the live desk screens and the channel push, for the same
	# reason: the room is free again, so OTAs should see it now, not at the
	# hourly push_all_ari
	from kamra.notifications import no_show
	no_show(res)
	from kamra.channel_manager import enqueue_property_push
	from kamra.realtime import notify
	notify(res, "on_update")
	enqueue_property_push(res.property)
	ns = {"reservation": res.name, "guest_name": res.guest_name,
	      "room_type": (res.room_type or "").split("-")[-1],
	      "check_in_date": str(res.check_in_date),
	      "source": res.get("channel") or res.get("source"),
	      "fee": decision.fee, "fee_amount": 0.0,
	      "outcome": "No show - no fee"}
	if decision.fee == "waived":
		ns["outcome"] = f"No show - fee waived ({decision.reason})"
	elif decision.fee == "charge":
		fee = post_policy_fee(res, pol.charge,
		                      f"No-show charge ({pol.charge})",
		                      posting_date=business_date)
		ns["fee_amount"] = float(fee or 0)
		ns["outcome"] = f"No show - {pol.charge.lower()} fee posted"
	ns["credit"] = no_show_credit(res.name)
	return ns


NO_SHOW_FORFEIT = "Forfeited advance (no-show)"
CREDIT_ACTIONS = ("Refund", "Keep as forfeited deposit")


def no_show_credit(reservation: str) -> float:
	"""What a No Show's open guest folio holds for the guest after the fee:
	the advance paid beyond it. 0 when the folio is settled, owes, or does
	not exist."""
	row = frappe.db.get_value(
		"Folio", {"reservation": reservation, "folio_type": "Guest",
		          "status": ("!=", "Closed")}, "balance")
	return round(max(0.0, -float(row or 0)), 2)


def settle_no_show_credit(res, action: str, reason: str, mode: str | None = None,
                          pin: str | None = None) -> dict:
	"""Refund a no-show's credit to the guest, or keep it as a forfeited
	deposit (a charge at the room's tax rate whose total is the credit, so
	the folio settles at zero). Either way the advance first leaves the
	Deposit ledger for the Guest ledger, as post_policy_fee does, so the
	refund or the forfeit charge clears the Guest ledger and nothing is left
	shown as held."""
	if res.status != "No Show":
		frappe.throw(frappe._("Only a No Show's credit is settled here (this one is {0}).")
		             .format(res.status))
	if action not in CREDIT_ACTIONS:
		frappe.throw(frappe._("Choose Refund or Keep as forfeited deposit."))
	if not (reason or "").strip():
		frappe.throw(frappe._("A reason is required."))
	credit = no_show_credit(res.name)
	if credit <= 0:
		frappe.throw(frappe._("This no-show has no credit on its folio."))
	folio = frappe.db.get_value(
		"Folio", {"reservation": res.name, "folio_type": "Guest",
		          "status": ("!=", "Closed")}, "name")
	from kamra.business_date import get_business_date
	from kamra.ledger import transfer_advances_to_guest
	bd = str(get_business_date(res.property))
	transfer_advances_to_guest(res.name, business_date=bd)
	if action == "Refund":
		from kamra.api import _refund_folio_payment
		_refund_folio_payment(folio, credit, mode or "Cash",
		                      f"No-show credit refund: {reason.strip()}", pin=pin)
	else:
		from kamra.api import _pin_guard, _post_folio_charge
		_pin_guard(folio, pin)
		gst = Decimal(str(_room_gst(res)))
		# pre-tax so that amount + tax is the credit; what rounding leaves
		# (under a cent) is inside close_folio's tolerance
		pre = (Decimal(str(credit)) * 100 / (100 + gst)).quantize(Decimal("0.01"))
		# the reason stays out of the description: words in it must not
		# re-tax the line (_resolve_charge_gst reads the description)
		_post_folio_charge(folio, "Misc", NO_SHOW_FORFEIT, float(pre),
		                   gst_rate=float(gst), posting_date=bd,
		                   reservation=res.name)
	from kamra.localization import money_text
	from kamra.savings import log_action
	log_action("no_show_credit", "Reservation", res.name, res.property,
	           rationale=f"{res.name} no-show credit {money_text(res.property, credit)}: "
	                     f"{action.lower()} - {reason.strip()[:100]}")
	return {"reservation": res.name, "action": action, "amount": credit,
	        "credit": no_show_credit(res.name)}


def audit_clock(property: str):
	"""When the audit is running, on the property's clock - what a late
	arrival's ETA is compared with. Its own function so tests can pin it."""
	from kamra.no_show import property_now
	return property_now(property)


def run_night_audit(property: str, business_date: str | None = None,
                    acknowledge=None, note: str | None = None,
                    mode: str | None = None) -> dict:
	"""Automated end-of-day: open missing folios, post the night's room
	charges for every in-house guest, flag no-shows. Closes only the
	property's current business date; a retry for a date that already
	closed returns ``already_ran`` instead of posting twice.

	The night audit checklist (kamra.night_audit) runs first:

	mode "manual" (Billing, REST, MCP): refused while a blocking check has
	  items, unless every one is listed in ``acknowledge`` with a ``note``
	  (the reason) - who acknowledged it and when is stored on the run.
	mode "scheduled" (the 15 minute tick): follows the property's
	  na_auto_audit_with_blockers - "Run and report exceptions" closes the
	  day and records the blockers as exceptions; "Hold and alert" leaves
	  the business date and alerts Hotel Admin and Finance.
	mode None (an internal call): never gated; open blockers are recorded
	  as exceptions.

	Every run saves its audit pack (night_audit.build_pack) on the record."""
	# In non-request contexts (the 3 AM scheduler, bench console) frappe.local.lang
	# is unset, which makes Frappe's number/currency formatting raise
	# UnboundLocalError. Default it so the audit runs everywhere.
	if not getattr(frappe.local, "lang", None):
		frappe.local.lang = "en"
	from kamra.business_date import ensure_business_date, get_business_date
	ensure_business_date(property)
	current = get_business_date(property)
	business_date = str(getdate(business_date or current))
	# look the run up per property AND date - a global per-date lookup would
	# make the second property's audit silently no-op every night. Checked
	# before the date guards so a retry after success answers already_ran.
	# an End of Day draft (status Draft) is the wizard's progress for the
	# date, not a closed day
	done = frappe.db.get_value(
		"Night Audit Run",
		{"property": property, "business_date": business_date,
		 "status": ("!=", "Draft")}, "name")
	if done:
		return {"already_ran": True, "audit": done}
	draft_name = frappe.db.get_value(
		"Night Audit Run",
		{"property": property, "business_date": business_date,
		 "status": "Draft"}, "name")
	# The audit only ever closes the property's current business date. A
	# different date would post into a closed day or skip days, and then
	# set the business date to that date + 1.
	if getdate(business_date) != getdate(current):
		frappe.throw(frappe._("Night audit can only close the current business date "
		               "({0}).").format(current))
	# A day can't be closed before it has happened. Without this, repeated
	# clicks rolled the business date into the future and charged in-house
	# guests a room night (and no-shows a fee) per click.
	if getdate(business_date) > getdate(nowdate()):
		frappe.throw(frappe._("Business date {0} is already ahead of today ({1}). "
		               "Night audit for it can run once that day has "
		               "started.").format(business_date, nowdate()))

	# the checklist, as it stands before anything is posted
	from kamra import night_audit as na
	run_at = audit_clock(property)
	# guests still in past their departure, with na_auto_extend on, stay one
	# more night before the checklist and the posting run (so it is posted)
	extended = na.auto_extend_departures(property, business_date)
	checks = na.run_checks(property, business_date, run_at)
	blockers = na.open_blockers(checks)
	acks: list[dict] = []
	if mode == "manual":
		# acknowledged in the End of Day wizard (each with its own reason)
		drafted = {a["key"]: a for a in na.draft_acknowledgements(draft_name)}
		keys = na.parse_keys(acknowledge)
		missing = [c for c in blockers
		           if c["key"] not in keys and c["key"] not in drafted]
		if missing:
			frappe.throw(
				frappe._("Night audit is blocked: {0}. Resolve them, or acknowledge "
				         "each with a reason to run anyway.").format(
					"; ".join(f"{c['title']} ({c['count']})" for c in missing)),
				title=frappe._("Night audit pre-checks"))
		fresh = [c for c in blockers if c["key"] not in drafted]
		if fresh and len((note or "").strip()) < 3:
			frappe.throw(frappe._("Give a reason for acknowledging the blocking "
			                      "checks."))
		acks = [drafted[c["key"]] for c in blockers if c["key"] in drafted] + \
			na.acknowledgement_rows(fresh, keys, (note or "").strip())
	elif mode == "scheduled" and blockers:
		from kamra.settings import get_setting
		if get_setting(property, "na_auto_audit_with_blockers") == na.HOLD_AND_ALERT:
			out = na.hold_and_alert(property, business_date, blockers)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- scheduler job persists the hold alert per property; reviewed as intentional
			return out
		acks = na.acknowledgement_rows(
			blockers, [c["key"] for c in blockers],
			"Scheduled audit: run and reported as exceptions", by="Administrator",
			auto=True)

	log_lines = []
	folios_opened = charges_posted = no_shows = 0
	amount_posted = Decimal(0)
	no_show_rows: list[dict] = []

	in_house = frappe.get_all(
		"Reservation",
		filters={
			"property": property,
			"status": "Checked In",
			"check_in_date": ("<=", business_date),
			"check_out_date": (">", business_date),
		},
		fields=["name"],
	)
	for row in in_house:
		res = frappe.get_doc("Reservation", row.name)
		if not frappe.db.get_value(
				"Folio", {"reservation": res.name, "folio_type": "Guest"}):
			folio_name = open_folio(res)
			folios_opened += 1
			log_lines.append(f"opened folio {folio_name} for {res.name}")
		if post_room_night(res, business_date):
			charges_posted += 1
			amount_posted += Decimal(str(_nightly_room_rate(res, business_date)))
			log_lines.append(f"posted room night {business_date} for {res.name}")

	# no-shows: closing day D ends D's arrivals too (Opera end of day), so
	# a confirmed stay due on or before the closed date that never checked
	# in is flagged AND charged per the property's policy - on the closed
	# date, like the room nights of the same run. The property's no-show
	# policy (kamra.no_show) can push that a day later, hold late arrivals
	# due on the closed date until the next audit, and waive the fee on OTA
	# bookings (the OTA collects under its own policy).
	from kamra import no_show
	pol = no_show.policy(property)
	cutoff = business_date if pol.timing != no_show.ONE_DAY_LATER \
		else str(add_days(business_date, -1))
	held_late: list[dict] = []
	fees_waived = 0
	stale = frappe.get_all(
		"Reservation",
		filters={
			"property": property,
			"status": "Confirmed",
			"check_in_date": ("<=", cutoff),
		},
		fields=["name"],
		order_by="check_in_date asc, name asc",
	)
	for row in stale:
		res = frappe.get_doc("Reservation", row.name)
		decision = no_show.audit_outcome(res, pol, business_date, run_at)
		if decision.outcome == "defer":
			continue
		if decision.outcome == "hold":
			held_late.append({"reservation": res.name,
			                  "guest_name": res.guest_name,
			                  "eta": res.get("eta") or None,
			                  "reason": decision.reason})
			log_lines.append(f"held late arrival: {res.name} ({decision.reason})")
			continue
		ns = apply_no_show(res, pol, decision, business_date)
		no_shows += 1
		no_show_rows.append(ns)
		log_lines.append(f"flagged no-show: {row.name}")
		if decision.fee == "waived":
			fees_waived += 1
			log_lines.append(
				f"no-show fee waived for {row.name}: {decision.reason}")
		if ns["fee_amount"]:
			amount_posted += Decimal(str(ns["fee_amount"]))
			log_lines.append(
				f"posted no-show charge {ns['fee_amount']:,.2f} for {row.name}")

	# purge stale waitlist entries - two days after their requested departure
	from frappe.utils import add_days as _add_days
	purged = 0
	for wl in frappe.get_all("Reservation", filters={
			"property": property, "status": "Waitlist",
			"check_out_date": ("<=", _add_days(business_date, -2))}, pluck="name"):
		frappe.delete_doc("Reservation", wl, ignore_permissions=True, force=True)
		purged += 1
	if purged:
		log_lines.append(f"purged {purged} stale waitlist entries")

	for a in acks:
		log_lines.append(f"acknowledged {a['key']} ({a['count']}) by {a['by']}: "
		                 f"{a['note']}")
	for x in extended:
		log_lines.append(f"extended {x['reservation']} one night" if x["extended"]
		                 else f"could not extend {x['reservation']}: {x.get('error')}")
	unacked = [c for c in blockers if c["key"] not in {a["key"] for a in acks}]
	for c in unacked:
		log_lines.append(f"exception: {c['title']} ({c['count']})")
	trigger = {"manual": "Manual", "scheduled": "Scheduled"}.get(mode or "", "System")
	summary = {
		"room_charges_posted": charges_posted,
		"amount_posted": float(amount_posted),
		"no_shows_flagged": no_shows,
		"late_arrivals_held": len(held_late),
		"no_show_fees_waived": fees_waived,
		"folios_opened": folios_opened,
		"stays_extended": sum(1 for x in extended if x["extended"]),
		"trigger": trigger,
		"run_by": frappe.session.user,
		"exceptions": len(blockers),
		"notes": (frappe.db.get_value("Night Audit Run", draft_name,
		                              "auditor_notes") if draft_name else None),
	}
	report_pack = None
	for _ in range(2):  # one retry
		try:
			pack = na.build_pack(property, business_date, checks=checks,
			                     acknowledgements=acks, summary=summary,
			                     no_shows=no_show_rows, held_late=held_late)
			report_pack = json.dumps(pack, default=str)
			break
		except Exception:
			frappe.log_error(title=f"Night audit pack failed: {property}")
			report_pack = None

	values = {
		"status": "Completed with Warnings" if blockers else "Completed",
		"room_charges_posted": charges_posted,
		"amount_posted": float(amount_posted),
		"no_shows_flagged": no_shows,
		"late_arrivals_held": len(held_late),
		"no_show_fees_waived": fees_waived,
		"folios_opened": folios_opened,
		"run_trigger": trigger,
		"run_by": frappe.session.user,
		"exceptions": len(blockers),
		"acknowledgements": json.dumps(acks) if acks else None,
		"report_pack": report_pack,
		"log": "\n".join(log_lines) or "Nothing to do.",
		"eod_step": 11,
	}
	if draft_name:
		# the wizard's draft becomes the run: its notes and progress stay
		audit = frappe.get_doc("Night Audit Run", draft_name)
		audit.update(values)
		audit.save(ignore_permissions=True)
	else:
		audit = frappe.get_doc({"doctype": "Night Audit Run",
		                        "property": property,
		                        "business_date": business_date, **values})
		audit.insert(ignore_permissions=True)

	# Advance the front-office business date (Opera End of Day roll).
	from kamra.business_date import advance_business_date, ensure_business_date
	ensure_business_date(property)
	new_bd = advance_business_date(property, business_date)
	# reports scheduled "After night audit" go out once this commits
	frappe.enqueue("kamra.custom_reports.send_after_night_audit",
	               property=property, business_date=business_date,
	               enqueue_after_commit=True)

	from kamra.savings import log_action
	log_action(
		action_type="night_audit",
		reference_doctype="Night Audit Run",
		reference_name=audit.name,
		property=property,
		minutes_saved=90,
		rationale=f"Posted {charges_posted} room nights, flagged {no_shows} "
		          f"no-shows, held {len(held_late)} late arrivals for "
		          f"{business_date}; business date → {new_bd}",
		agent_name="Night Audit",
		autonomy="Full",
		channel="API",
	)
	if acks and mode == "manual":
		log_action(
			action_type="night_audit_acknowledged",
			reference_doctype="Night Audit Run",
			reference_name=audit.name,
			property=property,
			rationale="Blocking checks acknowledged: "
			          + "; ".join(f"{a['title']} ({a['count']})" for a in acks)
			          + f" - {acks[0]['note']}",
			channel="MCP" if frappe.flags.get("kamra_agent_call") else "Desk",
			after_snapshot=acks,
		)
	from kamra.webhooks import emit_night_audit_closed
	emit_night_audit_closed(property, {
		"audit": audit.name, "status": audit.status,
		"business_date": business_date, "next_business_date": new_bd,
		"room_charges_posted": charges_posted,
		"amount_posted": float(amount_posted), "no_shows_flagged": no_shows,
		"exceptions": len(blockers)})
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persists the completed operation before returning to an external/public caller; reviewed as intentional
	return {
		"audit": audit.name,
		"status": audit.status,
		"exceptions": len(blockers),
		"acknowledged": [a["key"] for a in acks],
		"has_pack": bool(report_pack),
		"room_charges_posted": charges_posted,
		"amount_posted": float(amount_posted),
		"no_shows_flagged": no_shows,
		"late_arrivals_held": len(held_late),
		"held_late_arrivals": held_late,
		"no_show_fees_waived": fees_waived,
		"folios_opened": folios_opened,
		"business_date": business_date,
		"next_business_date": new_bd,
	}


# The scheduler closes at most this many overdue days on its own (the
# property's night_audit_catch_up_days, else this). A property further
# behind gets an alert instead: weeks of unattended room nights and no-show
# fees need a person to check the stays first.
NIGHTLY_CATCH_UP_DAYS = 3


def _days(n: int) -> str:
	return f"{n} day" if n == 1 else f"{n} days"


def _catch_up_limit(property: str) -> int | None:
	"""The property's stored night_audit_catch_up_days, NIGHTLY_CATCH_UP_DAYS
	when unset or empty, and None when the stored value is not a whole
	number from 1 to 7 (a row written around save_settings): the caller
	then stops instead of guessing a limit."""
	from kamra.settings import SETTINGS, read_kv
	raw = read_kv(property, "night_audit_catch_up_days")
	if raw is None or raw == "":
		return NIGHTLY_CATCH_UP_DAYS
	spec = SETTINGS["night_audit_catch_up_days"][1]
	if isinstance(raw, bool) or not isinstance(raw, int) or not spec.min <= raw <= spec.max:
		return None
	return raw


def audit_due_at(property: str, day):
	"""When the automatic audit is due on `day`, on the property's clock:
	night_audit_time plus the grace minutes."""
	from datetime import datetime, timedelta

	from kamra.settings import get_setting
	at = datetime.strptime(get_setting(property, "night_audit_time"), "%H:%M").time()
	grace = int(get_setting(property, "night_audit_grace_minutes") or 0)
	return datetime.combine(getdate(day), at) + timedelta(minutes=grace)


def scheduled_attempt_made(property: str, business_date, local_now, due) -> bool:
	"""The scheduler already tried this business date since today's due
	time and did not close it (held, skipped or failed). It then waits for
	tomorrow or for staff, instead of retrying and alerting every tick."""
	since = now_datetime() - (local_now - due)
	return bool(frappe.db.exists("Night Audit Log", {
		"property": property, "run_trigger": "Scheduled",
		"business_date": business_date, "status": ("!=", "Closed"),
		"started_at": (">=", since)}))


def nightly_audit_all_properties():
	"""Scheduler entry point, every 15 minutes. For each active property
	with night_audit_auto on, once its local clock is past night_audit_time
	(+ grace) and its business date is before the local calendar date,
	closes ONE business date. A property several days behind catches up
	one day per tick. Each property runs on its own; one failing never
	stops the rest."""
	from kamra.night_audit import record_run
	from kamra.settings import get_setting
	for p in frappe.get_all("Property", filters={"disabled": 0}, pluck="name"):
		try:
			if not get_setting(p, "night_audit_auto"):
				continue
			local_now = audit_clock(p)
			today = local_now.date()
			due = audit_due_at(p, today)
			if local_now < due:
				continue
			bd = frappe.db.get_value("Property", p, "business_date")
			if bd and getdate(bd) >= today:
				continue
			if bd and scheduled_attempt_made(p, bd, local_now, due):
				continue
			# a fresh snapshot, then a row lock on the property: a second
			# worker waits here and then sees the first one's date roll
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- releases the read snapshot before the per-property lock
			frappe.db.sql("SELECT name FROM `tabProperty` WHERE name = %s FOR UPDATE", p)
			record_run(p, "Scheduled", lambda p=p, today=today: _nightly_audit_property(p, today))
		except Exception:
			frappe.log_error(title=f"Night audit failed: {p}")


def _nightly_audit_property(property: str, today) -> dict | None:
	"""One scheduled attempt: None when there is nothing to do, else what
	happened (a run_night_audit result, or a skip with its reason)."""
	from frappe.utils import date_diff

	from kamra import night_audit as na
	from kamra.business_date import advance_business_date, ensure_business_date

	log = frappe.logger("kamra")
	bd = getdate(ensure_business_date(property))
	if bd >= today:
		log.info(f"Night audit skipped for {property}: business date {bd} "
		         f"is not before today ({today})")
		return None
	# A completed run for the open date whose roll did not stick. Without
	# this every night answered already_ran and the date never moved again.
	# Advance it like recover_stuck_business_date does; nothing is re-posted.
	limit = _catch_up_limit(property)
	if limit is None:
		name = frappe.db.get_value("Property", property, "property_name") or property
		msg = (f"Catch up at most (night_audit_catch_up_days) is not a whole number "
		       f"from 1 to 7 in Property Setting {property}::night_audit_catch_up_days. "
		       f"The scheduled night audit is stopped until it is fixed under Settings "
		       f"→ Front Desk → Automatic night audit.")
		log.warning(f"Night audit not run for {property}: {msg}")
		na.alert_staff(property, ("Hotel Admin",),
		               f"Night audit stopped - {name}", f"{name}: {msg}")
		return {"skipped": True, "business_date": str(bd), "message": msg}
	stuck = na.stuck_business_date(property)
	if stuck:
		bd = getdate(advance_business_date(property, str(bd)))
		msg = (f"Business date was stuck on {stuck['business_date']} "
		       f"(audit {stuck['audit']}), moved to {bd}")
		log.warning(f"Night audit for {property}: {msg}")
		if bd >= today:
			return {"skipped": True, "business_date": stuck["business_date"],
			        "message": msg}
	behind = date_diff(today, bd)
	if behind > limit:
		name = frappe.db.get_value("Property", property, "property_name") or property
		msg = (f"The business date is {bd}, {_days(behind)} behind today. The "
		       f"scheduled night audit only catches up {_days(limit)} on its own. "
		       f"Check in-house stays and arrivals, then close the open days from "
		       f"End of Day.")
		log.warning(f"Night audit not run for {property}: {msg}")
		na.alert_staff(property, ("Hotel Admin", "Finance"),
		               f"Night audit behind - {name}", f"{name}: {msg}")
		return {"skipped": True, "business_date": str(bd), "message": msg}
	return run_night_audit(property, mode="scheduled")
