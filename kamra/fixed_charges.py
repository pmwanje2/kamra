"""P29 & P41: Recurring Fixed Charges & Idempotent Auto-Post Engine.

Manages OPERA-style recurring Fixed Charges on reservations (Daily, Weekly,
Stay Day N, On Arrival, On Departure) persisted via governed child-row writes
so Reservation.validate_locked_nights is never disturbed, routed cleanly to
target Folio Windows (W1..W8), and posted idempotently via `fc:` keys.
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe import _
from frappe.utils import cint, date_diff, flt, getdate, now_datetime

from kamra import pricing
from kamra.authz import assert_property_access, require_roles
from kamra.business_date import get_business_date
from kamra.ledger import home_currency, minor_unit, save_folio

FREQUENCIES = ("Daily", "Weekly", "Stay Day N", "On Arrival", "On Departure")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
CLOSED_STATUSES = ("Cancelled", "Checked Out", "No Show")


def _dec(val) -> Decimal:
	if val is None or val == "":
		return Decimal(0)
	return Decimal(str(val))


def _quantize(value, unit: Decimal) -> Decimal:
	return _dec(value).quantize(unit, rounding=ROUND_HALF_UP)


def _charge_key_standing(idempotency_key: str) -> bool:
	from kamra.folio import STANDS_ON_ITS_DATE

	return bool(
		frappe.db.sql(
			"""
			SELECT 1 FROM `tabFolio Charge` fc
			WHERE fc.idempotency_key = %(key)s
			  AND """ + STANDS_ON_ITS_DATE + """
			LIMIT 1
			""",
			{"key": idempotency_key},
		)
	)


def _reservation_meta(reservation: str):
	res = frappe.db.get_value(
		"Reservation",
		reservation,
		["name", "property", "status", "check_in_date", "check_out_date"],
		as_dict=True,
	)
	if not res:
		frappe.throw(_("Reservation {0} not found.").format(reservation), frappe.DoesNotExistError)
	assert_property_access(res.property)
	return res


def _fixed_charge_rows(reservation: str) -> list[dict]:
	return frappe.get_all(
		"Reservation Fixed Charge",
		filters={"parent": reservation, "parenttype": "Reservation", "parentfield": "fixed_charges"},
		fields=[
			"name", "idx", "transaction_code", "charge_type", "description",
			"qty", "currency", "unit_price", "frequency", "stay_day_n", "day_of_week",
			"start_date", "end_date", "window_no", "supplement",
			"active", "last_posted_business_date", "post_count",
		],
		order_by="idx asc, creation asc",
	)


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Night Auditor", "Hotel Admin", "Kamra Agent")
def list_fixed_charges(reservation: str) -> dict:
	"""List fixed charges on a reservation plus selectable property Transaction Codes."""
	res = _reservation_meta(reservation)
	codes = frappe.get_all(
		"Transaction Code",
		filters={
			"disabled": 0,
			"txn_type": ("!=", "Payment"),
			"property": ("in", [res.property, "", None]),
		},
		fields=["code", "description", "charge_type", "txn_type", "revenue_group"],
		order_by="code asc",
		limit_page_length=200,
	)
	snapshot = pricing.saved_contract_tariff(frappe.get_doc("Reservation", res.name))
	tariff = snapshot.get("tariff") or snapshot
	from kamra.settings import list_setting
	currencies = {code.upper() for code in list_setting(res.property, "fx_currencies")}
	if tariff.get("currency"):
		currencies.add(tariff["currency"])
	return {
		"reservation": res.name,
		"property": res.property,
		"currency": home_currency(res.property),
		"currencies": sorted(c for c in currencies if c),
		"check_in_date": str(res.check_in_date or ""),
		"check_out_date": str(res.check_out_date or ""),
		"rows": _fixed_charge_rows(res.name),
		"transaction_codes": codes,
	}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Hotel Admin")
def save_fixed_charge(reservation: str, row_data) -> dict:
	"""Create or update a Reservation Fixed Charge row without tripping Reservation rate locks."""
	return _save_fixed_charge(reservation, row_data)


def _save_fixed_charge(reservation: str, row_data) -> dict:
	"""Shared writer for authorized desk and server-priced direct bookings."""
	res = _reservation_meta(reservation)
	if res.status in CLOSED_STATUSES:
		frappe.throw(_("Cannot edit fixed charges on a {0} stay.").format(res.status))

	if isinstance(row_data, str):
		row_data = json.loads(row_data)
	if not isinstance(row_data, dict):
		frappe.throw(_("Fixed charge details are required."))

	code = (row_data.get("transaction_code") or "").strip()
	if not code or not frappe.db.exists("Transaction Code", code):
		frappe.throw(_("Select a valid Transaction Code."))
	tc = frappe.db.get_value(
		"Transaction Code",
		code,
		["name", "description", "charge_type", "txn_type", "property", "disabled"],
		as_dict=True,
	)
	if tc.disabled or tc.txn_type == "Payment":
		frappe.throw(_("Transaction Code {0} must be an active non-payment code.").format(code))
	if tc.property and tc.property != res.property:
		frappe.throw(_("Transaction Code {0} belongs to another property.").format(code))

	charge_type = (row_data.get("charge_type") or tc.charge_type or "Misc").strip()
	description = (row_data.get("description") or tc.description or "").strip()[:140]
	if not description:
		frappe.throw(_("Enter a description for the fixed charge."))

	qty = flt(row_data.get("qty", 1))
	if qty <= 0:
		frappe.throw(_("Quantity must be greater than zero."))

	row_name = (row_data.get("name") or "").strip()
	currency = row_data.get("currency")
	if currency is None and row_name:
		currency = frappe.db.get_value("Reservation Fixed Charge", {"name": row_name, "parent": res.name},
		                              "currency")
	currency = (currency or "").strip().upper() or home_currency(res.property)
	if not frappe.db.exists("Currency", currency):
		frappe.throw(_("Select a valid currency."))
	unit_price = pricing.round_rate_amount(row_data.get("unit_price", 0), 0, currency=currency)
	if unit_price < 0:
		frappe.throw(_("Unit price cannot be negative."))

	frequency = (row_data.get("frequency") or "Daily").strip()
	if frequency not in FREQUENCIES:
		frappe.throw(_("Frequency must be one of: {0}.").format(", ".join(FREQUENCIES)))

	stay_day_n = cint(row_data.get("stay_day_n") or 1)
	if frequency == "Stay Day N" and stay_day_n < 1:
		frappe.throw(_("Stay Day N must be at least 1."))

	day_of_week = (row_data.get("day_of_week") or "").strip()
	if day_of_week and day_of_week not in WEEKDAYS:
		frappe.throw(_("Day of week must be Monday through Sunday."))

	start_date = str(getdate(row_data.get("start_date") or res.check_in_date))
	end_date = str(getdate(row_data.get("end_date") or res.check_out_date))
	if getdate(start_date) > getdate(end_date):
		frappe.throw(_("Start date cannot be after end date."))

	window_no = cint(row_data.get("window_no") or 1)
	if not (1 <= window_no <= 8):
		frappe.throw(_("Folio window must be between 1 and 8."))

	supplement = (row_data.get("supplement") or "").strip()[:140]
	active = 1 if cint(row_data.get("active", 1)) else 0

	# Row-lock reservation before modifying child table directly
	frappe.db.sql("SELECT name FROM `tabReservation` WHERE name = %s FOR UPDATE", res.name)

	payload = {
		"transaction_code": code,
		"charge_type": charge_type,
		"description": description,
		"qty": qty,
		"currency": currency,
		"unit_price": float(unit_price),
		"frequency": frequency,
		"stay_day_n": stay_day_n,
		"day_of_week": day_of_week if frequency == "Weekly" else "",
		"start_date": start_date,
		"end_date": end_date,
		"window_no": window_no,
		"supplement": supplement,
		"active": active,
	}

	if row_name:
		if not frappe.db.exists(
			"Reservation Fixed Charge",
			{"name": row_name, "parent": res.name, "parenttype": "Reservation"},
		):
			frappe.throw(_("Fixed charge row not found on this reservation."))
		frappe.db.set_value("Reservation Fixed Charge", row_name, payload, update_modified=True)
		saved_name = row_name
	else:
		max_idx = frappe.db.sql(
			"""SELECT COALESCE(MAX(idx), 0)
			   FROM `tabReservation Fixed Charge`
			   WHERE parent = %s AND parenttype = 'Reservation' AND parentfield = 'fixed_charges'""",
			res.name,
		)[0][0]
		doc = frappe.get_doc({
			"doctype": "Reservation Fixed Charge",
			"parent": res.name,
			"parenttype": "Reservation",
			"parentfield": "fixed_charges",
			"idx": cint(max_idx) + 1,
			"post_count": 0,
			**payload,
		})
		doc.db_insert()
		saved_name = doc.name

	from kamra.savings import log_action
	log_action(
		"fixed_charge_saved",
		"Reservation",
		res.name,
		res.property,
		rationale=f"{frequency} {code} {description} ({qty:g} x {unit_price}) W{window_no}",
	)
	rows = _fixed_charge_rows(res.name)
	saved_row = next((r for r in rows if r["name"] == saved_name), None)
	return {"row": saved_row, "rows": rows}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Hotel Admin")
def delete_fixed_charge(reservation: str, row_name: str) -> dict:
	"""Delete an unposted fixed charge; soft-deactivate if it has already posted to a folio."""
	res = _reservation_meta(reservation)
	if res.status in CLOSED_STATUSES:
		frappe.throw(_("Cannot remove fixed charges on a {0} stay.").format(res.status))
	frappe.db.sql("SELECT name FROM `tabReservation` WHERE name = %s FOR UPDATE", res.name)
	row = frappe.db.get_value(
		"Reservation Fixed Charge",
		{"name": row_name, "parent": res.name, "parenttype": "Reservation"},
		["name", "description", "post_count"],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("Fixed charge row not found on this reservation."))

	if cint(row.post_count) > 0:
		frappe.db.set_value("Reservation Fixed Charge", row.name, "active", 0, update_modified=True)
		action = "deactivated"
	else:
		frappe.db.delete("Reservation Fixed Charge", {"name": row.name})
		action = "deleted"

	from kamra.savings import log_action
	log_action(
		"fixed_charge_removed",
		"Reservation",
		res.name,
		res.property,
		rationale=f"{action} fixed charge {row.description}",
	)
	return {"action": action, "name": row.name, "rows": _fixed_charge_rows(res.name)}


def _matches_trigger(row, res, bd_date, trigger: str) -> bool:
	freq = row.get("frequency") or "Daily"
	if trigger == "arrival":
		return freq == "On Arrival"
	if trigger == "departure":
		return freq == "On Departure"
	if trigger != "night_audit":
		return False
	if freq in ("On Arrival", "On Departure"):
		return False

	start = getdate(row.start_date)
	end = getdate(row.end_date)
	if not (start <= bd_date <= end):
		return False

	if freq == "Daily":
		return True
	if freq == "Weekly":
		dow = (row.get("day_of_week") or "").strip()
		if dow:
			return bd_date.strftime("%A") == dow
		return (bd_date - start).days % 7 == 0
	if freq == "Stay Day N":
		target_day = max(1, cint(row.get("stay_day_n") or 1))
		stay_day = int(date_diff(bd_date, getdate(res.check_in_date))) + 1
		return stay_day == target_day
	return False


def _resolve_window_folio(
	res,
	window_no: int,
	charge_type: str,
	is_alcohol: int = 0,
	*,
	primary_folio: str | None = None,
) -> str:
	"""Resolve target open Folio for window_no (1..8).

	Supports both v2.8.0 Guest/Extra/Company folios and PR #43 Folio.window_no / routing.py.
	"""
	from kamra.folio import _recalculate, open_company_folio, open_line_target

	win = max(1, min(8, cint(window_no or 1)))

	# Forward-compatible support when Folio.window_no is installed (PR #43)
	if frappe.get_meta("Folio").has_field("window_no"):
		existing = frappe.db.get_value(
			"Folio",
			{"reservation": res.name, "window_no": win, "status": "Open"},
			"name",
		)
		if existing:
			return existing
		if win > 1:
			folio_doc = frappe.get_doc({
				"doctype": "Folio",
				"property": res.property,
				"reservation": res.name,
				"guest": res.guest,
				"folio_type": "Extra",
				"window_no": win,
				"status": "Open",
				"opened_on": now_datetime(),
			})
			_recalculate(folio_doc)
			folio_doc.insert(ignore_permissions=True)
			return folio_doc.name

	if win == 1:
		if (
			primary_folio
			and not res.get("group_booking")
			and not res.get("company")
			and frappe.db.get_value("Folio", primary_folio, "status") != "Closed"
		):
			return primary_folio
		return open_line_target(res, charge_type, is_alcohol)
	if win == 3 and res.get("company") and not is_alcohol:
		return open_company_folio(res)

	# On v2.8.0 (without Folio.window_no): map W2+ deterministically to open Extra folios
	target_idx = win - 2  # W2 -> 0, W3 (no company) -> 1, ...
	extras = frappe.get_all(
		"Folio",
		filters={"reservation": res.name, "folio_type": "Extra", "status": "Open"},
		pluck="name",
		order_by="creation asc",
	)
	while len(extras) <= target_idx:
		new_folio = frappe.get_doc({
			"doctype": "Folio",
			"property": res.property,
			"reservation": res.name,
			"guest": res.guest,
			"folio_type": "Extra",
			"status": "Open",
			"opened_on": now_datetime(),
		})
		_recalculate(new_folio)
		new_folio.insert(ignore_permissions=True)
		extras.append(new_folio.name)
	return extras[target_idx]


def post_fixed_charges_for_date(
	res,
	business_date,
	folios: dict | None = None,
	*,
	trigger: str = "night_audit",
	primary_folio: str | None = None,
) -> dict:
	"""Idempotently post all matching active fixed charges for a reservation and trigger."""
	from kamra.folio import _recalculate

	bd_date = getdate(business_date)
	bd_str = str(bd_date)
	own_batch = folios is None
	if own_batch:
		folios = {}

	rows = frappe.get_all(
		"Reservation Fixed Charge",
		filters={
			"parent": res.name,
			"parenttype": "Reservation",
			"parentfield": "fixed_charges",
			"active": 1,
		},
		fields=[
			"name", "transaction_code", "charge_type", "description",
			"qty", "currency", "unit_price", "frequency", "stay_day_n", "day_of_week",
			"start_date", "end_date", "window_no", "supplement", "post_count",
		],
		order_by="idx asc, creation asc",
	)
	if not rows:
		return {"count": 0, "amount": 0.0, "keys": []}

	home = home_currency(res.property)
	unit = minor_unit(res.property)
	snapshot = pricing.saved_contract_tariff(res)
	tariff = snapshot.get("tariff") or snapshot
	posted_count = 0
	posted_amount = Decimal(0)
	posted_keys: list[str] = []

	for row in rows:
		if not _matches_trigger(row, res, bd_date, trigger):
			continue
		suffix = "arrival" if trigger == "arrival" else "departure" if trigger == "departure" else bd_str
		idem_key = f"fc:{res.name}:{row.name}:{suffix}"
		if _charge_key_standing(idem_key):
			continue

		tc = frappe.db.get_value(
			"Transaction Code",
			row.transaction_code,
			["charge_type", "default_tax_rate", "is_alcohol"],
			as_dict=True,
		) or {}
		charge_type = row.charge_type or tc.get("charge_type") or "Misc"
		is_alcohol = cint(tc.get("is_alcohol") or 0)
		gst_rate = flt(tc.get("default_tax_rate") or 0)

		qty = _dec(row.qty or 1)
		currency = (row.get("currency") or "").strip().upper() or home
		unit_price = pricing.round_rate_amount(row.unit_price or 0, 0, currency=currency)
		if unit_price <= 0:
			continue
		fx_fields, fx_note = {}, ""
		fx = Decimal(1)
		if currency != home:
			rate = None
			if currency == tariff.get("currency") and pricing.reservation_fx_policy(res) in (
				pricing.DEFAULT_FX_LOCK, "Fixed at Check In",
			):
				locked_fx = _dec(tariff.get("fx_rate"))
				if locked_fx.is_finite() and locked_fx > 0:
					rate = (locked_fx, tariff.get("fx_date"))
			rate = rate or pricing.posting_fx_rate(res.property, currency, bd_str)
			if not rate:
				frappe.throw(_("No exchange rate from {0} to {1} is available for the business date.")
				             .format(currency, home), frappe.ValidationError)
			fx, fx_date = rate
			fx_fields = {"contract_currency": currency, "contract_fx_rate": float(fx),
			             "contract_fx_date": fx_date}
			fx_note = f" ({currency} {unit_price:,f} @ {fx.normalize():,f})"
		amount = pricing.round_rate_amount(qty * unit_price * fx, 0, currency=home)
		if amount <= 0:
			continue

		desc = (row.description or charge_type).strip()
		if row.get("supplement"):
			desc = f"{desc} · {row.supplement.strip()}"

		folio_name = _resolve_window_folio(
			res,
			cint(row.window_no or 1),
			charge_type,
			is_alcohol,
			primary_folio=primary_folio,
		)
		if folio_name not in folios:
			folios[folio_name] = frappe.get_doc("Folio", folio_name)
		folios[folio_name].append("charges", {
			"posting_date": bd_str,
			"charge_type": charge_type,
			"transaction_code": row.transaction_code,
			"idempotency_key": idem_key,
			"reservation": res.name,
			"description": desc[:140 - len(fx_note)] + fx_note,
			"qty": float(qty),
			"rate": float(pricing.round_rate_amount(unit_price * fx, 0, currency=home)),
			"amount": float(amount),
			**fx_fields,
			"gst_rate": gst_rate,
			"is_alcohol": is_alcohol,
			"auto_posted": 1,
		})

		frappe.db.set_value(
			"Reservation Fixed Charge",
			row.name,
			{
				"post_count": cint(row.post_count or 0) + 1,
				"last_posted_business_date": bd_str,
			},
			update_modified=False,
		)
		posted_count += 1
		posted_amount += amount
		posted_keys.append(idem_key)

	if own_batch and folios:
		for doc in folios.values():
			_recalculate(doc)
			save_folio(doc)

	return {
		"count": posted_count,
		"amount": float(_quantize(posted_amount, unit)),
		"keys": posted_keys,
	}


def post_arrival_fixed_charges(
	res,
	folios: dict | None = None,
	*,
	primary_folio: str | None = None,
) -> dict:
	"""Post 'On Arrival' fixed charges idempotently when a stay checks in."""
	in_progress = getattr(frappe.flags, "_kamra_arrival_fc_res", None)
	if in_progress is None:
		in_progress = set()
		frappe.flags._kamra_arrival_fc_res = in_progress
	if res.name in in_progress:
		return {"count": 0, "amount": 0.0, "keys": []}
	in_progress.add(res.name)
	try:
		bd = get_business_date(res.property) or res.check_in_date
		return post_fixed_charges_for_date(
			res, bd, folios=folios, trigger="arrival", primary_folio=primary_folio,
		)
	finally:
		in_progress.discard(res.name)


def post_departure_fixed_charges(res, folios: dict | None = None) -> dict:
	"""Post 'On Departure' fixed charges idempotently prior to final checkout settlement."""
	bd = get_business_date(res.property) or res.check_out_date
	return post_fixed_charges_for_date(res, bd, folios=folios, trigger="departure")
