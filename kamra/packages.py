"""P13: Itemised Package Elements, Revenue Splitting & Outlet Allowances.

Splits package nights deterministically onto distinct Transaction Codes
(Room Revenue vs Meal / Spa / Transport / F&B Allowances) using each
property's ISO 4217 currency minor unit (0/2/3dp), tracks outlet consumption
and over/under-allowance Profit & Loss (Package Breakage on 1410), and formats
guest-facing folios in Inclusive or Separate Add-on mode without altering GL truth.
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate

from kamra.authz import assert_property_access, require_roles
from kamra.business_date import get_business_date
from kamra.ledger import code_for_charge_type, home_currency, minor_unit, save_folio
from kamra.pricing import active_holiday_package, billing_room_type, billing_segments, nightly_billing_room_types, rate_quantum

ELEMENT_TYPES = ("Meal", "Spa", "Transport", "F&B Allowance", "Utility/Amenity")
ALLOWANCE_BASES = (
	"Per Adult Night",
	"Per Child Night",
	"Per Person Night",
	"Per Room Night",
	"Fixed Per Stay",
)
POSTING_RHYTHMS = ("Every Night", "First Night", "Last Night")
DISPLAY_MODES = ("Inclusive", "Separate Add-on")
BREAKAGE_TRANSACTION_CODE = "1410"

_ELEMENT_TYPE_CHARGE_TYPE = {
	"Meal": "Meal Plan",
	"Spa": "Spa",
	"Transport": "Taxi",
	"F&B Allowance": "Food & Beverage",
	"Utility/Amenity": "Misc",
}


def _dec(val) -> Decimal:
	if val is None or val == "":
		return Decimal(0)
	return Decimal(str(val))


def _quantize(value, unit: Decimal) -> Decimal:
	return _dec(value).quantize(unit, rounding=ROUND_HALF_UP)


def _standing_charge_folio(idempotency_key: str) -> str | None:
	"""Return parent Folio name of a standing Folio Charge matching idempotency_key, or None."""
	from kamra.folio import STANDS_ON_ITS_DATE

	rows = frappe.db.sql(
		"""
		SELECT fc.parent FROM `tabFolio Charge` fc
		WHERE fc.idempotency_key = %(key)s
		  AND """ + STANDS_ON_ITS_DATE + """
		LIMIT 1
		""",
		{"key": idempotency_key},
	)
	return rows[0][0] if rows else None


def _charge_key_standing(idempotency_key: str) -> bool:
	"""True when a standing (not same-day voided) Folio Charge already holds this idempotency_key."""
	return bool(_standing_charge_folio(idempotency_key))


def _parse_consumed_refs(raw) -> list[str]:
	if not raw:
		return []
	if isinstance(raw, list):
		return [str(x) for x in raw if x]
	try:
		parsed = json.loads(raw)
		if isinstance(parsed, list):
			return [str(x) for x in parsed if x]
	except (TypeError, ValueError):
		pass
	return []


def _package_window(res, date):
	room_type = billing_room_type(res)
	if date is not None and res.get("room_move_rate_cutoff"):
		for name, dates in billing_segments(res.check_in_date, res.check_out_date, room_type,
		                                  nightly_billing_room_types(res)):
			if dates[0] <= str(getdate(date)) <= dates[-1]:
				return name, dates[0], add_days(dates[-1], 1)
	return room_type, res.check_in_date, res.check_out_date


def reservation_package_id(res, date=None, *, snapshot=None):
	"""The quoted nightly package, or the eligible billing segment on an old stay."""
	if snapshot is None:
		try:
			snapshot = frappe.parse_json(res.get("quote_snapshot") or "{}")
		except ValueError:
			snapshot = {}
	snapshot = snapshot if isinstance(snapshot, dict) else {}
	if date is not None:
		night = next((n for n in snapshot.get("nightly") or [] if n.get("date") == str(getdate(date))), {})
		if "package_id" in night:
			return night["package_id"]
	room_type, start, end = _package_window(res, date)
	name = (snapshot.get("package") or {}).get("name")
	if name:
		scope = frappe.db.get_value("Holiday Package", name, ["property", "room_type", "start_date", "end_date"],
		                            as_dict=True)
		if (scope and scope.property == res.property and scope.room_type in (None, "", room_type)
		        and getdate(scope.start_date) <= max(getdate(start), getdate(add_days(end, -1)))
		        and getdate(scope.end_date) >= getdate(start)):
			return name
	if not flt(res.get("agreed_rate")):
		pkg = active_holiday_package(res.property, start, end, room_type)
		return pkg.name if pkg else None


def active_package_for_reservation(res, date=None):
	"""The sold package, if it has elements to split on the folio."""
	name = reservation_package_id(res, date)
	if name and frappe.db.exists("Holiday Package", name):
		doc = frappe.get_doc("Holiday Package", name)
		if doc.property == res.property and doc.get("elements"):
			return doc
	return None


def _element_due_on_date(el, start, end, business_date) -> bool:
	bd = getdate(business_date)
	first_night = getdate(start)
	last_night = getdate(add_days(end, -1))
	if last_night < first_night:
		last_night = first_night

	rhythm = el.get("posting_rhythm") or "Every Night"
	basis = el.get("allowance_basis") or "Per Adult Night"
	if basis == "Fixed Per Stay":
		return bd == last_night if rhythm == "Last Night" else bd == first_night
	if rhythm == "First Night":
		return bd == first_night
	if rhythm == "Last Night":
		return bd == last_night
	return True


def _element_multiplier(basis: str, adults: int, children: int) -> int:
	if basis == "Per Adult Night":
		return max(1, adults)
	if basis == "Per Child Night":
		return max(0, children)
	if basis == "Per Person Night":
		return max(1, adults) + max(0, children)
	return 1


def _resolve_element_charge_type(el) -> str:
	if el.get("charge_type"):
		return el.charge_type
	code = el.get("transaction_code")
	if code:
		tc_ct = frappe.db.get_value("Transaction Code", code, "charge_type")
		if tc_ct:
			return tc_ct
	return _ELEMENT_TYPE_CHARGE_TYPE.get(el.get("element_type") or "", "Misc")


def fixed_package_postings(reservation, *, lock=False):
	"""Standing entitlements, including dated keys issued by older versions."""
	from kamra.folio import STANDS_ON_ITS_DATE

	if not reservation:
		return {}
	sql = """
		SELECT pe.name AS element, fc.idempotency_key, fc.posting_date
		FROM `tabFolio Charge` fc
		JOIN `tabPackage Element` pe
		  ON pe.name = SUBSTRING_INDEX(SUBSTRING_INDEX(fc.idempotency_key, ':', 3), ':', -1)
		WHERE fc.reservation = %s AND fc.idempotency_key LIKE 'pkg:%%'
		  AND pe.allowance_basis = 'Fixed Per Stay' AND """ + STANDS_ON_ITS_DATE
	rows = frappe.db.sql(sql + (" FOR UPDATE" if lock else ""), reservation, as_dict=True)
	return {r.element: (str(r.posting_date), r.idempotency_key) for r in rows}


def split_package_night(res, package_doc, night_total, business_date, *, gst_rate: float = 0.0, fulfilled=None, sold_gross=None) -> dict:
	"""Split a package night deterministically into Room Net + due Package Elements.

	Included elements (`included_in_rate == 1`) are deducted from `night_total` to derive
	`room_net`, with rounding remainder absorbed into `room_net` so
	`room_net + included_total == night_total` down to the exact currency minor unit (0/2/3dp).
	Separate Add-on elements (`included_in_rate == 0`) post on top without reducing `room_net`.
	"""
	bd_str = str(getdate(business_date))
	unit = minor_unit(res.property)
	total_dec = _quantize(night_total, unit)
	adults = max(1, cint(res.get("adults") or 1))
	children = max(0, cint(res.get("children") or 0))
	pkg_mode = package_doc.get("folio_display_mode") or "Inclusive"

	included_items: list[dict] = []
	addon_items: list[dict] = []

	start, end = res.check_in_date, res.check_out_date
	if res.get("room_move_rate_cutoff"):
		_, start, end = _package_window(res, bd_str)
	fixed_window = None
	for el in package_doc.get("elements") or []:
		fixed = el.get("allowance_basis") == "Fixed Per Stay"
		if fixed and fulfilled is None:
			fulfilled = fixed_package_postings(res.name)
		prior = fulfilled.get(el.name) if fixed else None
		if prior and prior[0] != bd_str:
			continue
		if fixed and not prior and fixed_window is None:
			dates = res.get("package_dates")
			if dates is None and res.get("room_move_rate_cutoff"):
				from kamra.pricing import _stay_nights
				snapshot = frappe.parse_json(res.get("quote_snapshot") or "{}")
				dates = [day for day in _stay_nights(res.check_in_date, res.check_out_date)
				         if reservation_package_id(res, day, snapshot=snapshot) == package_doc.name]
			fixed_window = (dates[0], add_days(dates[-1], 1)) if dates else (start, end)
		window = fixed_window if fixed else (start, end)
		if not prior and not _element_due_on_date(el, *window, bd_str):
			continue
		mult = _element_multiplier(el.get("allowance_basis") or "Per Adult Night", adults, children)
		amt = _quantize(_dec(el.get("unit_allowance")) * Decimal(mult), unit)
		if amt <= 0:
			continue
		included = 1 if cint(el.get("included_in_rate")) else 0
		el_gst = flt(el.get("gst_rate"))
		if not el_gst and el.get("transaction_code"):
			el_gst = flt(frappe.db.get_value("Transaction Code", el.transaction_code, "default_tax_rate") or 0)
		if included and not el_gst and flt(gst_rate or 0) > 0:
			el_gst = flt(gst_rate)
		allowance_gross = amt if cint(el.get("tax_inclusive")) else None
		if allowance_gross is not None and el_gst > 0:
			amt = _quantize(amt / (Decimal(1) + _dec(el_gst) / Decimal(100)), unit)
		item = {
			"element_row": el.name,
			"element_name": el.element_name,
			"element_type": el.element_type or "Meal",
			"transaction_code": el.transaction_code,
			"charge_type": _resolve_element_charge_type(el),
			"amount": amt,
			"gst_rate": el_gst,
			"included_in_rate": included,
			"folio_display_mode": el.get("folio_display_mode") or pkg_mode,
			"idempotency_key": prior[1] if prior else f"pkg:{res.name}:{el.name}:" + ("stay" if fixed else bd_str),
		}
		if item["included_in_rate"]:
			if sold_gross is not None:
				item["gross"] = allowance_gross if allowance_gross is not None else amt + _quantize(amt * _dec(el_gst) / 100, unit)
			included_items.append(item)
		else:
			addon_items.append(item)

	if sold_gross is not None:
		# Allocate the advertised gross to inclusions first; only the residual
		# belongs to accommodation and attracts the room's tax rate.
		gross = _quantize(sold_gross, unit)
		allowances = sum((item["gross"] for item in included_items), Decimal(0))
		allocated = weight = Decimal(0)
		for item in included_items:
			weight += item["gross"]
			if allowances > gross:
				# Rounded cumulative shares conserve the cap, including at 0dp.
				share = _quantize(gross * weight / allowances, unit) - allocated
				item["amount"] = _quantize(share / (1 + _dec(item["gst_rate"]) / 100), unit)
			else:
				share = item["gross"]
			allocated += share
			item["gst_amount"] = share - item["amount"]
		room_gross = gross - allocated
		room_net = _quantize(room_gross / (1 + _dec(gst_rate) / 100), unit)
		total_dec = room_net + sum((item["amount"] for item in included_items), Decimal(0))

	included_total = sum((i["amount"] for i in included_items), Decimal(0))
	if included_total > total_dec and included_items:
		# Proportionally cap included allowances so room_net never goes negative
		# while conserving total_dec down to the exact minor unit.
		scaled_sum = Decimal(0)
		for idx, item in enumerate(included_items):
			if idx == len(included_items) - 1:
				item["amount"] = max(Decimal(0), total_dec - scaled_sum)
			else:
				share = _quantize(total_dec * item["amount"] / included_total, unit)
				item["amount"] = share
				scaled_sum += share
		included_total = sum((i["amount"] for i in included_items), Decimal(0))

	room_net = (total_dec - included_total).quantize(unit, rounding=ROUND_HALF_UP)
	addon_total = sum((i["amount"] for i in addon_items), Decimal(0))
	all_elements = included_items + addon_items

	return {
		"package": package_doc.name,
		"package_name": package_doc.package_name,
		"business_date": bd_str,
		"night_total": total_dec,
		"room_net": room_net,
		"room_gst_rate": flt(gst_rate),
		"room_idempotency_key": f"pkg:{res.name}:room:{bd_str}",
		"elements": all_elements,
		"included_total": included_total,
		"addon_total": addon_total,
		"total_posted": room_net + included_total + addon_total,
	}


def _ensure_allowance_record(res, package_doc, el_item: dict, bd_str: str) -> str:
	existing = frappe.db.get_value(
		"Reservation Package Allowance",
		{"idempotency_key": el_item["idempotency_key"]},
		"name",
	)
	if existing:
		return existing
	doc = frappe.get_doc({
		"doctype": "Reservation Package Allowance",
		"property": res.property,
		"reservation": res.name,
		"holiday_package": package_doc.name,
		"package_element": el_item["element_row"],
		"element_name": el_item["element_name"],
		"element_type": el_item["element_type"],
		"transaction_code": el_item["transaction_code"],
		"charge_type": el_item["charge_type"],
		"business_date": bd_str,
		"included_in_rate": 1 if cint(el_item.get("included_in_rate")) else 0,
		"idempotency_key": el_item["idempotency_key"],
		"allocated_amount": float(el_item["amount"]),
		"consumed_amount": 0,
		"overage_amount": 0,
		"breakage_amount": 0,
		"status": "Open",
	})
	doc.insert(ignore_permissions=True)
	return doc.name


def post_package_night_split(
	res,
	package_doc,
	night_total,
	gst_rate: float,
	business_date,
	*,
	folios: dict,
	room_desc: str,
	fx_posted: dict | None = None,
	rate_category: str | None = None,
	sold_gross=None,
) -> dict:
	"""Idempotently post Room Net + Package Element Folio Charges and Allowance buckets."""
	from kamra.folio import _append_charge, _charge_posted

	bd_str = str(getdate(business_date))
	# Serialize claims even when the same stay is routed to distinct folios.
	frappe.db.get_value("Reservation", res.name, "name", for_update=True)
	fixed = {el.name for el in package_doc.get("elements") or [] if el.allowance_basis == "Fixed Per Stay"}
	fulfilled = fixed_package_postings(res.name, lock=True) if fixed else {}
	for f in folios.values():
		for c in f.charges:
			key = (c.get("idempotency_key") or "").split(":")
			if not c.creation and len(key) == 4 and key[:2] == ["pkg", res.name] and key[2] in fixed:
				fulfilled[key[2]] = (str(c.posting_date), c.idempotency_key)
	split = split_package_night(res, package_doc, night_total, bd_str, gst_rate=gst_rate, fulfilled=fulfilled,
	                            sold_gross=sold_gross)

	tax_fields = {}
	if sold_gross is not None:
		# Net components are payable currency amounts. Allocate the remainder
		# of the locked gross to room tax after the included elements' tax.
		included_tax = sum((item["gst_amount"] for item in split["elements"] if item["included_in_rate"]), Decimal(0))
		tax_fields = {"tax_as_posted": 1, "gst_amount": float(_dec(sold_gross) - split["night_total"] - included_tax)}

	room_posted = False
	room_key = split["room_idempotency_key"]
	room_code = code_for_charge_type("Room") or "1000"
	if not _charge_key_standing(room_key) and not _charge_posted(res.name, "Room", bd_str):
		_append_charge(folios, res, "Room", {
			**(fx_posted or {}), **tax_fields,
			"posting_date": bd_str,
			"charge_type": "Room",
			"transaction_code": room_code,
			"idempotency_key": room_key,
			"reservation": res.name,
			"rate_category": rate_category,
			"description": f"{room_desc} · {package_doc.package_name}",
			"qty": 1,
			"rate": float(split["room_net"]),
			"amount": float(split["room_net"]),
			"gst_rate": float(gst_rate or 0),
			"auto_posted": 1,
		})
		room_posted = True

	elements_posted = 0
	elements_amount = Decimal(0)
	addon_posted_amount = Decimal(0)

	for el_item in split["elements"]:
		allowance = None
		if el_item["element_row"] in fixed:
			allowance = frappe.db.get_value("Reservation Package Allowance", {
				"reservation": res.name, "package_element": el_item["element_row"],
			}, ["name", "idempotency_key"], as_dict=True, for_update=True)
			if allowance and allowance.idempotency_key and el_item["element_row"] not in fulfilled:
				el_item["idempotency_key"] = allowance.idempotency_key
		el_key = el_item["idempotency_key"]
		pending = any(c.get("idempotency_key") == el_key for f in folios.values() for c in f.charges if not c.creation)
		if not pending and not _charge_key_standing(el_key):
			_append_charge(folios, res, el_item["charge_type"], {
				"posting_date": bd_str,
				"charge_type": el_item["charge_type"],
				"transaction_code": el_item["transaction_code"],
				"idempotency_key": el_key,
				"reservation": res.name,
				"description": f"{package_doc.package_name} · {el_item['element_name']}",
				"qty": 1,
				"rate": float(el_item["amount"]),
				"amount": float(el_item["amount"]),
				"gst_rate": float(el_item["gst_rate"] or 0),
				**({"tax_as_posted": 1, "gst_amount": float(el_item["gst_amount"])} if "gst_amount" in el_item else {}),
				"auto_posted": 1,
			})
			elements_posted += 1
			elements_amount += el_item["amount"]
			if not el_item["included_in_rate"]:
				addon_posted_amount += el_item["amount"]
		if not allowance:
			_ensure_allowance_record(res, package_doc, el_item, bd_str)

	return {
		"posted": room_posted,
		"room_net": float(split["room_net"]),
		"night_total": float(split["night_total"]),
		"elements_posted": elements_posted,
		"elements_amount": float(elements_amount),
		"addon_amount": float(addon_posted_amount),
		"total_posted": float(split["total_posted"]),
	}


def _get_reservation_checked(reservation: str):
	if not frappe.db.exists("Reservation", reservation):
		frappe.throw(_("Reservation {0} not found.").format(reservation), frappe.DoesNotExistError)
	res = frappe.get_doc("Reservation", reservation)
	assert_property_access(res.property)
	return res


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Night Auditor", "Hotel Admin", "F&B Manager", "POS Integrator", "Kamra Agent")
def consume_package_allowance(
	reservation: str,
	transaction_code: str | None = None,
	amount: float = 0,
	business_date: str | None = None,
	source_ref: str | None = None,
	description: str | None = None,
	element_type: str | None = None,
) -> dict:
	"""Apply an outlet charge against open package allowances; post ONLY excess to the folio."""
	from kamra.folio import _recalculate, open_line_target

	res = _get_reservation_checked(reservation)
	unit = minor_unit(res.property)
	req_amount = _quantize(amount, unit)
	if req_amount <= 0:
		frappe.throw(_("Consumed amount must be greater than zero."))
	if not transaction_code and not element_type:
		frappe.throw(_("Provide a Transaction Code or Element Type for allowance consumption."))

	bd = str(getdate(business_date or get_business_date(res.property)))
	src_ref = (source_ref or "").strip()
	overage_key = f"pkgover:{res.name}:{src_ref}" if src_ref else None

	# Lock reservation and all its allowance rows FOR UPDATE before idempotency check or mutation.
	frappe.db.sql("SELECT name FROM `tabReservation` WHERE name = %s FOR UPDATE", res.name)
	all_rows = frappe.db.sql(
		"""
		SELECT name, business_date, element_name, element_type,
		       transaction_code, charge_type, allocated_amount,
		       consumed_amount, overage_amount, status, consumed_refs
		FROM `tabReservation Package Allowance`
		WHERE reservation = %(res)s AND property = %(prop)s
		ORDER BY business_date ASC, creation ASC
		FOR UPDATE
		""",
		{"res": res.name, "prop": res.property},
		as_dict=True,
	)

	def _matches_scope(row) -> bool:
		if transaction_code and row.transaction_code != transaction_code:
			return False
		if element_type and row.element_type != element_type:
			return False
		return True

	def _remaining_open(rows_list) -> Decimal:
		return sum(
			(
				max(Decimal(0), _quantize(x.allocated_amount, unit) - _quantize(x.consumed_amount, unit))
				for x in rows_list
				if x.status == "Open" and _matches_scope(x)
			),
			Decimal(0),
		)

	if src_ref:
		existing_folio = _standing_charge_folio(overage_key) if overage_key else None
		ref_already_recorded = any(src_ref in _parse_consumed_refs(r.get("consumed_refs")) for r in all_rows)
		if existing_folio or ref_already_recorded:
			return {
				"reservation": res.name,
				"absorbed": 0.0,
				"guest_overage": 0.0,
				"remaining_allowance": float(_remaining_open(all_rows)),
				"overage_folio": existing_folio,
				"allowance_rows": [],
				"already_consumed": True,
			}

	rows = [r for r in all_rows if r.status == "Open" and _matches_scope(r)]
	# Prefer exact business_date allowance bucket before earlier/other open stay buckets.
	rows.sort(key=lambda r: (0 if str(getdate(r.business_date)) == bd else 1, str(r.business_date)))

	remaining_to_cover = req_amount
	absorbed_total = Decimal(0)
	touched_rows: list[dict] = []
	last_row_name: str | None = None
	resolved_code = transaction_code
	resolved_ct = None
	resolved_el_name = None

	for r in rows:
		alloc = _quantize(r.allocated_amount, unit)
		cons = _quantize(r.consumed_amount, unit)
		avail = max(Decimal(0), alloc - cons)
		if avail <= 0:
			continue
		take = min(remaining_to_cover, avail)
		new_cons = _quantize(cons + take, unit)
		new_status = "Consumed" if new_cons >= alloc else "Open"
		updates: dict = {"consumed_amount": float(new_cons), "status": new_status}
		if src_ref:
			refs = _parse_consumed_refs(r.get("consumed_refs"))
			if src_ref not in refs:
				refs.append(src_ref)
			updates["consumed_refs"] = json.dumps(refs)
			r.consumed_refs = updates["consumed_refs"]
		frappe.db.set_value(
			"Reservation Package Allowance",
			r.name,
			updates,
			update_modified=True,
		)
		r.consumed_amount = float(new_cons)
		r.status = new_status
		absorbed_total += take
		remaining_to_cover -= take
		last_row_name = r.name
		resolved_code = resolved_code or r.transaction_code
		resolved_ct = resolved_ct or r.charge_type
		resolved_el_name = resolved_el_name or r.element_name
		touched_rows.append({
			"name": r.name,
			"absorbed": float(take),
			"remaining": float(max(Decimal(0), alloc - new_cons)),
			"status": new_status,
		})
		if remaining_to_cover <= 0:
			break

	guest_overage = _quantize(remaining_to_cover, unit)
	overage_folio = None
	if guest_overage > 0:
		if last_row_name:
			prev_over = _quantize(
				frappe.db.get_value("Reservation Package Allowance", last_row_name, "overage_amount") or 0,
				unit,
			)
			frappe.db.set_value(
				"Reservation Package Allowance",
				last_row_name,
				"overage_amount",
				float(_quantize(prev_over + guest_overage, unit)),
				update_modified=True,
			)
		if not resolved_ct and resolved_code:
			resolved_ct = frappe.db.get_value("Transaction Code", resolved_code, "charge_type")
		resolved_ct = resolved_ct or _ELEMENT_TYPE_CHARGE_TYPE.get(element_type or "", "Misc")
		gst_rate = 0.0
		if resolved_code:
			gst_rate = flt(frappe.db.get_value("Transaction Code", resolved_code, "default_tax_rate") or 0)
		target_name = open_line_target(res, resolved_ct)
		fdoc = frappe.get_doc("Folio", target_name)
		label = (description or resolved_el_name or resolved_ct).strip()
		charge_row: dict = {
			"posting_date": bd,
			"charge_type": resolved_ct,
			"transaction_code": resolved_code,
			"reservation": res.name,
			"description": f"{label} (Over package allowance)",
			"qty": 1,
			"rate": float(guest_overage),
			"amount": float(guest_overage),
			"gst_rate": gst_rate,
			"auto_posted": 0,
		}
		if overage_key:
			charge_row["idempotency_key"] = overage_key
		fdoc.append("charges", charge_row)
		_recalculate(fdoc)
		save_folio(fdoc)
		overage_folio = fdoc.name

	remaining_allowance = _remaining_open(all_rows)

	return {
		"reservation": res.name,
		"absorbed": float(absorbed_total),
		"guest_overage": float(guest_overage),
		"remaining_allowance": float(remaining_allowance),
		"overage_folio": overage_folio,
		"allowance_rows": touched_rows,
	}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Night Auditor", "Hotel Admin", "Kamra Agent")
def settle_package_breakage(reservation: str, business_date: str | None = None) -> dict:
	"""Settle unconsumed package allowances as breakage profit without charging the guest."""
	res = _get_reservation_checked(reservation)
	unit = minor_unit(res.property)
	bd = str(getdate(business_date or get_business_date(res.property)))

	frappe.db.sql("SELECT name FROM `tabReservation` WHERE name = %s FOR UPDATE", res.name)
	sql = """
		SELECT name, allocated_amount, consumed_amount
		FROM `tabReservation Package Allowance`
		WHERE reservation = %(res)s AND property = %(prop)s AND status = 'Open'
	"""
	params = {"res": res.name, "prop": res.property}
	if business_date:
		sql += " AND business_date <= %(bd)s"
		params["bd"] = bd
	sql += " FOR UPDATE"
	rows = frappe.db.sql(sql, params, as_dict=True)
	settled_count = 0
	breakage_total = Decimal(0)
	for r in rows:
		alloc = _quantize(r.allocated_amount, unit)
		cons = _quantize(r.consumed_amount, unit)
		unused = max(Decimal(0), _quantize(alloc - cons, unit))
		frappe.db.set_value(
			"Reservation Package Allowance",
			r.name,
			{
				"breakage_amount": float(unused),
				"status": "Settled",
				"settled_on": bd,
			},
			update_modified=True,
		)
		settled_count += 1
		breakage_total += unused

	return {
		"reservation": res.name,
		"settled_count": settled_count,
		"breakage_total": float(_quantize(breakage_total, unit)),
		"breakage_code": BREAKAGE_TRANSACTION_CODE,
	}


def expire_daily_allowances(res, business_date) -> dict:
	"""Night Audit Step 9 helper: settle prior-day unconsumed allowances as package breakage."""
	bd = str(getdate(business_date))
	unit = minor_unit(res.property)
	frappe.db.sql("SELECT name FROM `tabReservation` WHERE name = %s FOR UPDATE", res.name)
	rows = frappe.db.sql(
		"""
		SELECT name, allocated_amount, consumed_amount
		FROM `tabReservation Package Allowance`
		WHERE reservation = %(res)s AND property = %(prop)s
		  AND status = 'Open' AND business_date < %(bd)s
		FOR UPDATE
		""",
		{"res": res.name, "prop": res.property, "bd": bd},
		as_dict=True,
	)
	count = 0
	breakage = Decimal(0)
	for r in rows:
		alloc = _quantize(r.allocated_amount, unit)
		cons = _quantize(r.consumed_amount, unit)
		unused = max(Decimal(0), _quantize(alloc - cons, unit))
		frappe.db.set_value(
			"Reservation Package Allowance",
			r.name,
			{
				"breakage_amount": float(unused),
				"status": "Settled",
				"settled_on": bd,
			},
			update_modified=True,
		)
		count += 1
		breakage += unused
	return {"count": count, "breakage_amount": float(_quantize(breakage, unit))}


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Night Auditor", "Hotel Admin", "Revenue Manager", "Kamra Agent")
def package_allowance_summary(reservation: str) -> dict:
	"""Package allowance P&L summary (allocated, consumed, overage billed, breakage profit)."""
	res = _get_reservation_checked(reservation)
	unit = minor_unit(res.property)
	rows = frappe.get_all(
		"Reservation Package Allowance",
		filters={"reservation": res.name, "property": res.property},
		fields=[
			"name", "holiday_package", "element_name", "element_type",
			"transaction_code", "charge_type", "business_date",
			"allocated_amount", "consumed_amount", "overage_amount",
			"breakage_amount", "status", "settled_on",
		],
		order_by="business_date asc, creation asc",
	)
	allocated = sum((_quantize(r.allocated_amount, unit) for r in rows), Decimal(0))
	consumed = sum((_quantize(r.consumed_amount, unit) for r in rows), Decimal(0))
	overage = sum((_quantize(r.overage_amount, unit) for r in rows), Decimal(0))
	breakage = sum((_quantize(r.breakage_amount, unit) for r in rows), Decimal(0))
	remaining = sum(
		(
			max(Decimal(0), _quantize(r.allocated_amount, unit) - _quantize(r.consumed_amount, unit))
			for r in rows if r.status == "Open"
		),
		Decimal(0),
	)
	return {
		"reservation": res.name,
		"property": res.property,
		"currency": home_currency(res.property),
		"allocated_total": float(allocated),
		"consumed_total": float(consumed),
		"overage_total": float(overage),
		"breakage_total": float(breakage),
		"remaining_total": float(remaining),
		"net_package_profit": float(_quantize(breakage + overage, unit)),
		"rows": rows,
	}


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Night Auditor", "Hotel Admin", "Kamra Agent")
def package_folio_view(folio: str, display_mode: str | None = None) -> dict:
	"""Guest-facing projection of a folio in Inclusive or Separate Add-on mode without mutating GL rows."""
	if not frappe.db.exists("Folio", folio):
		frappe.throw(_("Folio {0} not found.").format(folio), frappe.DoesNotExistError)
	fdoc = frappe.get_doc("Folio", folio)
	assert_property_access(fdoc.property)

	res = frappe.get_doc("Reservation", fdoc.reservation) if fdoc.get("reservation") else None
	pkg_doc = active_package_for_reservation(res) if res else None
	package_names = {}

	if display_mode and display_mode in DISPLAY_MODES:
		mode = display_mode
	elif res and res.get("package_display_mode") in DISPLAY_MODES:
		mode = res.package_display_mode
	elif pkg_doc and pkg_doc.get("folio_display_mode") in DISPLAY_MODES:
		mode = pkg_doc.folio_display_mode
	else:
		mode = "Inclusive"

	element_included: dict[str, int] = {}
	if pkg_doc:
		for el in pkg_doc.get("elements") or []:
			element_included[el.name] = 1 if cint(el.get("included_in_rate")) else 0
	allowance_included: dict[str, int] = {}
	if res:
		for rpa in frappe.get_all(
			"Reservation Package Allowance",
			filters={"reservation": res.name, "property": fdoc.property},
			fields=["idempotency_key", "included_in_rate"],
		):
			if rpa.get("idempotency_key"):
				allowance_included[rpa.idempotency_key] = 1 if cint(rpa.get("included_in_rate")) else 0

	voided = {c.reversal_of for c in fdoc.charges if c.get("reversal_of")}
	live_charges = [
		c for c in fdoc.charges
		if not c.get("reversal_of") and c.name not in voided
	]

	unit = minor_unit(fdoc.property)

	def _line_dict(c) -> dict:
		return {
			"name": c.name,
			"posting_date": str(c.posting_date or ""),
			"charge_type": c.charge_type,
			"transaction_code": c.transaction_code,
			"description": c.description,
			"qty": flt(c.qty or 1),
			"rate": float(_quantize(c.rate or 0, unit)),
			"amount": float(_quantize(c.amount or 0, unit)),
			"gst_amount": float(_quantize(c.gst_amount or 0, unit)),
			"total": float(_quantize(c.total or c.amount or 0, unit)),
			"idempotency_key": c.get("idempotency_key") or "",
			"bundled": False,
		}

	if mode == "Separate Add-on":
		return {
			"folio": fdoc.name,
			"currency": home_currency(fdoc.property),
			"display_mode": mode,
			"lines": [_line_dict(c) for c in live_charges],
		}

	# Inclusive mode: roll up room + included package elements for each (reservation, posting_date)
	bundles: dict[tuple[str, str], dict] = {}
	ordered_lines: list[dict] = []
	for c in live_charges:
		key = c.get("idempotency_key") or ""
		parts = key.split(":")
		is_pkg = len(parts) == 4 and parts[0] == "pkg"
		part_id = parts[2] if is_pkg else ""
		include_in_bundle = is_pkg and (
			part_id == "room"
			or allowance_included.get(key, element_included.get(part_id, 1)) == 1
		)
		if include_in_bundle:
			bkey = (parts[1], str(c.posting_date))
			if bkey not in bundles:
				origin = res if res and res.name == bkey[0] else None
				if not origin and frappe.db.get_value("Reservation", bkey[0], "property") == fdoc.property:
					origin = frappe.get_doc("Reservation", bkey[0])
				pkg_id = reservation_package_id(origin, bkey[1]) if origin else None
				if pkg_id not in package_names:
					package_names[pkg_id] = frappe.db.get_value("Holiday Package",
						{"name": pkg_id, "property": fdoc.property}, "package_name") if pkg_id else None
				pkg_name = package_names[pkg_id] or _("Package")
				bundle = {
					"name": f"bundle:{bkey[0]}:{bkey[1]}",
					"posting_date": bkey[1],
					"charge_type": "Room",
					"transaction_code": "1000",
					"description": f"Package Stay — {pkg_name} ({bkey[1]})",
					"qty": 1,
					"rate": Decimal(0),
					"amount": Decimal(0),
					"gst_amount": Decimal(0),
					"total": Decimal(0),
					"bundled": True,
					"breakdown": [],
				}
				bundles[bkey] = bundle
				ordered_lines.append(bundle)
			bundle = bundles[bkey]
			amt = _quantize(c.amount or 0, unit)
			gst = _quantize(c.gst_amount or 0, unit)
			tot = _quantize(c.total or (amt + gst), unit)
			bundle["amount"] += amt
			bundle["rate"] += amt
			bundle["gst_amount"] += gst
			bundle["total"] += tot
			bundle["breakdown"].append(_line_dict(c))
		else:
			ordered_lines.append(_line_dict(c))

	for line in ordered_lines:
		if line.get("bundled"):
			line["rate"] = float(_quantize(line["rate"], unit))
			line["amount"] = float(_quantize(line["amount"], unit))
			line["gst_amount"] = float(_quantize(line["gst_amount"], unit))
			line["total"] = float(_quantize(line["total"], unit))

	return {
		"folio": fdoc.name,
		"currency": home_currency(fdoc.property),
		"display_mode": mode,
		"lines": ordered_lines,
	}


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Revenue Manager", "Hotel Admin", "Kamra Agent")
def list_package_elements(holiday_package: str) -> dict:
	"""Return Package Elements, active Transaction Codes, and per-occupancy Net Room Revenue preview."""
	if not frappe.db.exists("Holiday Package", holiday_package):
		frappe.throw(_("Holiday Package {0} not found.").format(holiday_package), frappe.DoesNotExistError)
	pkg = frappe.get_doc("Holiday Package", holiday_package)
	assert_property_access(pkg.property)

	unit = rate_quantum(pkg.property)
	currency = home_currency(pkg.property)
	pkg_nights = max(1, cint(pkg.package_nights or pkg.minimum_nights or 1))

	elements = [
		{
			"name": el.name,
			"element_name": el.element_name,
			"element_type": el.element_type or "Meal",
			"transaction_code": el.transaction_code,
			"charge_type": el.charge_type or _resolve_element_charge_type(el),
			"allowance_basis": el.allowance_basis or "Per Adult Night",
			"unit_allowance": float(_quantize(el.unit_allowance or 0, unit)),
			"posting_rhythm": el.posting_rhythm or "Every Night",
			"included_in_rate": 1 if cint(el.included_in_rate) else 0,
			"folio_display_mode": el.folio_display_mode or pkg.folio_display_mode or "Inclusive",
			"tax_inclusive": 1 if cint(el.tax_inclusive) else 0,
			"gst_rate": flt(el.gst_rate or 0),
		}
		for el in (pkg.get("elements") or [])
	]

	txn_codes = frappe.get_all(
		"Transaction Code",
		filters={
			"disabled": 0,
			"txn_type": ("!=", "Payment"),
			"property": ("in", [pkg.property, "", None]),
		},
		fields=["code", "description", "charge_type", "txn_type", "revenue_group"],
		order_by="code asc",
		limit_page_length=200,
	)

	return {
		"holiday_package": pkg.name,
		"package_name": pkg.package_name,
		"property": pkg.property,
		"currency": currency,
		"package_nights": pkg_nights,
		"folio_display_mode": pkg.get("folio_display_mode") or "Inclusive",
		"price_1_pax": float(_quantize(pkg.price_1_pax or pkg.price_2_pax or 0, unit)),
		"price_2_pax": float(_quantize(pkg.price_2_pax or 0, unit)),
		"price_3_pax": float(_quantize(pkg.price_3_pax or 0, unit)),
		"price_4_pax": float(_quantize(pkg.price_4_pax or 0, unit)),
		"elements": elements,
		"transaction_codes": txn_codes,
	}


@frappe.whitelist(methods=["POST"])
@require_roles("Revenue Manager", "Hotel Admin")
def save_package_elements(
	holiday_package: str,
	elements,
	folio_display_mode: str | None = None,
) -> dict:
	"""Save itemised Package Elements and Folio Display Mode on a Holiday Package."""
	if not frappe.db.exists("Holiday Package", holiday_package):
		frappe.throw(_("Holiday Package {0} not found.").format(holiday_package), frappe.DoesNotExistError)
	pkg = frappe.get_doc("Holiday Package", holiday_package)
	assert_property_access(pkg.property)

	if isinstance(elements, str):
		elements = json.loads(elements)
	if not isinstance(elements, list):
		frappe.throw(_("Elements must be a list."))

	unit = rate_quantum(pkg.property)
	if folio_display_mode is not None:
		if folio_display_mode not in DISPLAY_MODES:
			frappe.throw(_("Folio display mode must be Inclusive or Separate Add-on."))
		pkg.folio_display_mode = folio_display_mode

	pkg.set("elements", [])
	for raw in elements:
		if not isinstance(raw, dict):
			continue
		name_val = (raw.get("element_name") or "").strip()
		if not name_val:
			frappe.throw(_("Every package element needs a name."))
		el_type = raw.get("element_type") or "Meal"
		if el_type not in ELEMENT_TYPES:
			frappe.throw(_("Invalid element type: {0}.").format(el_type))
		basis = raw.get("allowance_basis") or "Per Adult Night"
		if basis not in ALLOWANCE_BASES:
			frappe.throw(_("Invalid allowance basis: {0}.").format(basis))
		rhythm = raw.get("posting_rhythm") or "Every Night"
		if rhythm not in POSTING_RHYTHMS:
			frappe.throw(_("Invalid posting rhythm: {0}.").format(rhythm))
		code = (raw.get("transaction_code") or "").strip()
		if not code or not frappe.db.exists("Transaction Code", code):
			frappe.throw(_("Select a valid Transaction Code for {0}.").format(name_val))
		allowance = _quantize(raw.get("unit_allowance") or 0, unit)
		if allowance < 0:
			frappe.throw(_("Unit allowance cannot be negative for {0}.").format(name_val))
		mode = raw.get("folio_display_mode") or pkg.folio_display_mode or "Inclusive"
		if mode not in DISPLAY_MODES:
			mode = "Inclusive"
		pkg.append("elements", {
			"element_name": name_val[:140],
			"element_type": el_type,
			"transaction_code": code,
			"charge_type": raw.get("charge_type") or frappe.db.get_value("Transaction Code", code, "charge_type") or _ELEMENT_TYPE_CHARGE_TYPE.get(el_type, "Misc"),
			"allowance_basis": basis,
			"unit_allowance": float(allowance),
			"posting_rhythm": rhythm,
			"included_in_rate": 1 if cint(raw.get("included_in_rate", 1)) else 0,
			"folio_display_mode": mode,
			"tax_inclusive": 1 if cint(raw.get("tax_inclusive", 0)) else 0,
			"gst_rate": flt(raw.get("gst_rate") or 0),
		})

	pkg.save(ignore_permissions=True)
	from kamra.savings import log_action
	log_action(
		"package_elements_saved",
		"Holiday Package",
		pkg.name,
		pkg.property,
		rationale=f"Saved {len(pkg.elements)} package element(s) ({pkg.folio_display_mode})",
	)
	return {"ok": True, **list_package_elements(pkg.name)}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Hotel Admin")
def set_reservation_package_display_mode(reservation: str, display_mode: str = "") -> dict:
	"""Override Inclusive vs Separate Add-on folio display mode for one stay."""
	res = _get_reservation_checked(reservation)
	val = (display_mode or "").strip()
	if val and val not in DISPLAY_MODES:
		frappe.throw(_("Display mode must be Inclusive, Separate Add-on, or blank."))
	frappe.db.set_value("Reservation", res.name, "package_display_mode", val, update_modified=True)
	from kamra.savings import log_action
	log_action(
		"package_display_mode",
		"Reservation",
		res.name,
		res.property,
		rationale=f"Package folio display mode set to {val or 'Package Default'}",
	)
	return {"ok": True, "reservation": res.name, "package_display_mode": val}
