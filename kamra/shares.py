"""One physical room with separate reservations and guest folios."""

from __future__ import annotations

import json
from contextlib import contextmanager
from decimal import ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate

from kamra.authz import ADMIN, assert_guest_access, assert_property_access, require_roles
from kamra.kamra.doctype.reservation.reservation import is_entire_place, lock_property_for_inventory
from kamra.localization import _minor_unit_for_currency
from kamra.pricing import property_currency, rates_include_tax, round_rate_amount
from kamra.savings import log_action

MODES = ("Split Equal", "Primary Pays", "Full Rate Each", "Custom")
FIELDS = ("share_group_id", "is_share_primary", "share_rate_mode", "share_custom_rate")
ACTIVE = ("Confirmed", "Checked In")


@contextmanager
def current_inventory_reads():
	"""Use current locking reads while serializing against the property inventory writer.

	MariaDB 11.8 can otherwise reject even SELECT FOR UPDATE with error 1020
	after a concurrent commit. Plain reads keep their REPEATABLE READ snapshot.
	Restore the connection's original behavior before returning to the caller.
	"""
	strict = (getattr(frappe.db, "db_type", None) == "mariadb"
	          and frappe.db.sql("SHOW SESSION VARIABLES LIKE 'innodb_snapshot_isolation'"))
	if not strict or strict[0][1] != "ON":
		yield
		return
	frappe.db.sql("SET SESSION innodb_snapshot_isolation = OFF")
	try:
		yield
	finally:
		frappe.db.sql("SET SESSION innodb_snapshot_isolation = ON")


def _money(value, quantum: Decimal, *, custom: bool = False) -> Decimal:
	try:
		amount = Decimal(str(value))
		if amount.is_finite() and amount >= 0:
			rounded = amount.quantize(quantum, rounding=ROUND_HALF_UP)
			if not custom or rounded == amount:
				return rounded
	except (InvalidOperation, ValueError):
		pass
	frappe.throw(_("Enter a nonnegative share rate in the folio currency's minor units."))


def allocate_rates(room_rate, currency, mode, members) -> dict[str, Decimal]:
	"""Allocate minor units, breaking equal remainders by primary then name."""
	if mode not in MODES:
		frappe.throw(_("Select a valid share rate mode."))
	members = sorted(members, key=lambda row: (not row[1], row[0]))
	if not members:
		return {}
	quantum = Decimal(1).scaleb(-_minor_unit_for_currency(currency))
	if mode == "Custom":
		return {name: _money(rate, quantum, custom=True) for name, _primary, rate in members}
	total = _money(room_rate, quantum)
	if mode == "Full Rate Each":
		return {name: total for name, _primary, _rate in members}
	if mode == "Primary Pays":
		return {name: total if index == 0 else Decimal(0)
		        for index, (name, _primary, _rate) in enumerate(members)}
	units, remainder = divmod(int(total / quantum), len(members))
	return {name: (units + int(index < remainder)) * quantum
	        for index, (name, _primary, _rate) in enumerate(members)}


def inventory_key(res):
	"""Assigned shares consume a physical room; every other booking consumes its own slot."""
	return ("room", res.room) if res.share_group_id and res.room else ("reservation", res.name)


def _member_rows(res, *, lock=False, date=None, statuses=None):
	group = res.get("share_group_id")
	if not group:
		return []
	query = """
		SELECT name, room, share_group_id, is_share_primary, share_rate_mode, share_custom_rate,
		       guest, guest_name, status, check_in_date, check_out_date, rate_category,
		       adults, children, infants, over_capacity_guests
		FROM `tabReservation` WHERE property = %(property)s AND share_group_id = %(group)s
	"""
	args = {"property": res.property, "group": group}
	if date is not None:
		query += """ AND room = %(room)s AND status IN %(statuses)s
		             AND check_in_date <= %(date)s AND check_out_date > %(date)s"""
		args.update(room=res.room, statuses=statuses, date=str(date))
	query += " ORDER BY is_share_primary DESC, check_in_date, name"
	if lock:
		query += " FOR UPDATE"
	return frappe.db.sql(query, args, as_dict=True)


def _frozen_night(res, date, lock, folios):
	"""Return the frozen allocation and whether its lines still await a batch save."""
	from kamra.folio import STANDS_ON_ITS_DATE

	for bill in (folios or {}).values():
		for row in reversed(bill.charges):
			if (row.is_new() and row.share_group == res.share_group_id and row.charge_type == "Room"
			    and str(row.posting_date) == str(date) and row.share_snapshot):
				return json.loads(row.share_snapshot), True
	query = """
		SELECT fc.share_snapshot FROM `tabFolio Charge` fc
		JOIN `tabFolio` f ON f.name = fc.parent
		WHERE f.property = %(property)s AND fc.posting_date = %(date)s
		  AND fc.share_group = %(group)s AND fc.charge_type = 'Room'
		  AND fc.auto_posted = 1 AND """ + STANDS_ON_ITS_DATE + """
		  AND IFNULL(fc.share_snapshot, '') != ''
		ORDER BY fc.creation, fc.name LIMIT 1
	"""
	if lock:
		query += " FOR UPDATE"
	frozen = frappe.db.sql(query, {"property": res.property, "date": str(date), "group": res.share_group_id})
	return (json.loads(frozen[0][0]) if frozen else {}), False


def nightly_share(res, date, *, lock=False, advance_bill=False, folios=None) -> dict | None:
	"""The active payer set and price for a room night, frozen by its first posting."""
	group = res.get("share_group_id")
	statuses = ("Checked In", "Checked Out", "Confirmed") if advance_bill else ("Checked In", "Checked Out")
	if not group or not res.room or res.status not in statuses:
		return None
	frozen, _ = _frozen_night(res, date, lock, folios)
	if frozen:
		return _share_line(frozen, res.name)

	# Advance bills can start with a later arrival. Walk back to the nearest
	# checkpoint, then price missing nights chronologically without posting them.
	nights = []
	day = getdate(date)
	carry, pending = {}, False
	while True:
		if frozen:
			if frozen.get("room") != res.room:
				break
			if frozen.get("rounding"):
				carry = frozen["rounding"]
				break
			nights.append((day, None, frozen, pending))
		else:
			members = _member_rows(res, lock=lock, date=day, statuses=statuses)
			if not members:
				break
			nights.append((day, members, None, False))
		day = add_days(day, -1)
		frozen, pending = _frozen_night(res, day, lock, folios)
	if not nights:
		return None
	for day, members, legacy, unsaved in reversed(nights):
		if legacy:
			carry = _legacy_rounding(res.property, legacy, carry, pending, day, group)
		else:
			snapshot = _price_night(res, day, members, carry, pending, lock)
			carry = snapshot["rounding"]
		pending = unsaved
	return _share_line(snapshot, res.name)


def _price_night(res, date, members, carry, pending, lock):
	from kamra.folio import _nightly_gst, _nightly_room_rate, current_date_contract_night

	leader = members[0]
	rate_source = res if leader.name == res.name else frappe.get_doc("Reservation", leader.name, for_update=lock)
	mode = leader.share_rate_mode
	current = current_date_contract_night(rate_source, date) if mode != "Custom" else None
	gst = current["gst_rate"] if current else _nightly_gst(rate_source, date)
	if mode == "Custom":
		rate = sum(Decimal(str(m.share_custom_rate or 0)) for m in members)
		if gst and rates_include_tax(res.property):
			# _nightly_room_rate hands the folio a float after backing out GST.
			# Its posted precision matters when the document total is at half a unit.
			rate = float(rate / (1 + Decimal(str(gst)) / 100))
	else:
		rate = current["rate"] if current else _nightly_room_rate(rate_source, date)
	fx = {}
	if current:
		fx = {"contract_currency": current["currency"], "contract_fx_rate": current["fx_rate"],
		      "contract_fx_date": str(current["fx_date"]) if current["fx_date"] else None,
		      "contract_fx_stale": int(current["stale"])}
	if carry.get("currency") != property_currency(res.property):
		carry = {}
	rates, taxes, levy, rounding = _night_charges(
		res.property, rate, gst, mode, [(m.name, m.name == leader.name, m.share_custom_rate) for m in members],
		leader.name, carry, pending,
	)
	return {
		"room": res.room, "gst": gst, "category": leader.rate_category or "",
		"lead": leader.name, "fx": fx, "rate_split_mode": mode, "rates_include_tax": rates_include_tax(res.property),
		"levy": levy, "rounding": rounding,
		"rates": {name: str(value) for name, value in rates.items()},
		"taxes": {name: str(value) for name, value in taxes.items()},
	}


def _share_line(snapshot, reservation):
	taxes = snapshot.get("taxes") or {}
	# Each independently billed folio is the payer for its own fixed levy.
	lead = reservation if snapshot.get("levy", {}).get("per_folio") else snapshot["lead"]
	return {**snapshot, "lead": lead, "rate": Decimal(snapshot["rates"].get(reservation, "0")),
	        "tax": Decimal(taxes.get(reservation, "0")) if taxes else None}


def _rounded_parts(raw, unit, total=None):
	"""Round a whole amount once and distribute leftover minor units in payer order."""
	if total is None:
		total = sum(raw.values()).quantize(unit, rounding=ROUND_HALF_UP)
	parts = {name: value.quantize(unit, rounding=ROUND_DOWN) for name, value in raw.items()}
	remainder = int((total - sum(parts.values())) / unit)
	ranked = sorted(raw, key=lambda name: raw[name] - parts[name], reverse=True)
	for name in ranked[:remainder]:
		parts[name] += unit
	return parts


def _apportion(total, weights, unit):
	"""Allocate an already-rounded total in proportion to the payers' amounts."""
	weight = sum(weights.values())
	if not weight:
		return dict.fromkeys(weights, Decimal(0))
	return _rounded_parts({name: total * value / weight for name, value in weights.items()}, unit, total)


def _round_shared_night(currency, base, gst, levy, levy_rate, carry, pending):
	"""Allocate the increase in the rounded room bill since the last frozen night."""
	room_factor = 1 + Decimal(str(gst)) / 100
	gross_levy = levy * (1 + levy_rate)
	# Native recalculation reads the old room lines from Currency columns (9dp),
	# except for nights appended to the same, still unsaved checkout folio.
	stored_base = base.quantize(Decimal("0.000000001"), rounding=ROUND_HALF_UP)
	gross = base * room_factor + gross_levy
	open_total = gross + Decimal(carry.get("pending" if pending else "stored", "0"))
	grand = max(Decimal(0), round_rate_amount(open_total, 0, "Nearest", currency))
	stored_total = stored_base * room_factor + gross_levy + Decimal(carry.get("stored", "0"))
	return grand, {"currency": currency, "pending": str(open_total - grand), "stored": str(stored_total - grand)}


def _legacy_rounding(property, snapshot, carry, pending, day, group=None):
	"""Recover the last night's residual from its frozen prices and billed components."""
	from kamra.ledger import minor_unit

	currency = property_currency(property)
	gst = Decimal(str(snapshot["gst"]))
	rates = {name: Decimal(value) for name, value in snapshot["rates"].items()}
	levy = snapshot.get("levy") or {}
	if not gst or not levy:
		return carry
	tax_rate = Decimal(str(levy["gst"])) / 100
	if levy.get("per_folio") or levy.get("primary_pays"):
		return {"currency": currency, "payers": {
			name: _round_shared_night(currency, base, gst, Decimal(levy["amounts"][name]),
			                          tax_rate, carry.get("payers", {}).get(name, {}), pending)[1]
			for name, base in rates.items()}}
	fractions = _frozen_fractions(property, snapshot, day, group)
	opening = _continuing_carry(carry, snapshot["lead"], minor_unit(property))
	state = {"currency": currency}
	for key in ("stored", "pending"):
		prior = opening.get("stored" if key == "pending" and not pending else key, "0")
		state[key] = str(Decimal(prior) + sum(Decimal(row[key]) for row in fractions.values()))
	return _with_payer_rounding(state, carry, fractions, pending)


def _continuing_carry(carry, lead, unit):
	if "stored" in carry or "payers" not in carry:
		return carry
	# Full-rate lines retain their unrounded amounts on each guest's folio.
	# Only the fraction still unrepresented after reloading those lines is due.
	state = carry["payers"].get(lead, {})
	forward = {}
	for key in ("stored", "pending"):
		value = Decimal(state.get(key, "0"))
		units = (value / unit + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR)
		forward[key] = str(value - units * unit)
	return forward


def _payer_fractions(base, gst, levy, fixed, levy_tax, weights, posted, lead):
	"""The raw amount displaced by posting integral pooled lines, for each payer."""
	factor = 1 + Decimal(str(gst)) / 100
	fixed_gross = fixed * (1 + levy_tax)
	variable_levy = (levy - fixed) * (1 + levy_tax)
	total_weight = sum(weights.values())
	fractions = {name: {} for name in weights}
	for key, net in (("pending", base), ("stored", base.quantize(Decimal("1e-9"), rounding=ROUND_HALF_UP))):
		variable = net * factor + variable_levy
		for name, weight in weights.items():
			part = variable * weight / total_weight if total_weight else (variable if name == lead else 0)
			fractions[name][key] = str(part + (fixed_gross if name == lead else 0) - posted[name])
	return fractions


def _with_payer_rounding(state, carry, fractions, pending):
	payers = dict(carry.get("payers", {}))
	for name, delta in fractions.items():
		previous = payers.get(name, {})
		payers[name] = {
			"stored": str(Decimal(previous.get("stored", "0")) + Decimal(delta["stored"])),
			"pending": str(Decimal(previous.get("pending" if pending else "stored", "0")) + Decimal(delta["pending"])),
		}
	return {**state, "payers": payers, "fractions": fractions}


def _audit_modes(payload, group):
	"""An empty set means this group was absent from a complete member list."""
	try:
		members = json.loads(payload or "null")
	except ValueError:
		return None
	if not isinstance(members, list) or not members:
		return None
	modes = set()
	for member in members:
		if not isinstance(member, dict) or not member.get("reservation") or "share_group_id" not in member:
			return None
		if member["share_group_id"] == group:
			mode = member.get("share_rate_mode")
			if mode not in MODES:
				return None
			modes.add(mode)
	return modes if len(modes) <= 1 else None


def _priced_from_lead(property, snapshot, group, day):
	tariff_modes = ("Split Equal", "Primary Pays")
	if "rate_split_mode" in snapshot:
		return snapshot["rate_split_mode"] in tariff_modes
	if not group:
		return False
	from kamra.folio import STANDS_ON_ITS_DATE

	# A child charge can inherit its folio's opening time. Order by the first
	# original room posting to the guest ledger instead. A later transfer,
	# split or ledger backfill after the audit closed is not posting evidence.
	rows = frappe.db.sql("""
		SELECT a.creation, a.reference_doctype, a.reference_name, a.before_snapshot, a.after_snapshot, p.posted_at FROM `tabAgent Action Log` a
		JOIN (
		    SELECT MIN(e.posted_at) AS posted_at
		    FROM `tabFolio Charge` fc JOIN `tabFolio Ledger Entry` e
		      ON e.source_doctype = 'Folio Charge' AND e.source_name = fc.name AND e.folio = fc.parent
		    WHERE e.property = %(property)s AND e.ledger = 'Guest' AND e.business_date <= %(day)s
		      AND fc.share_group = %(group)s AND fc.share_room = %(room)s AND fc.posting_date = %(day)s
		      AND fc.auto_posted = 1 AND fc.charge_type = 'Room' AND IFNULL(e.reversal_of, '') = ''
		      AND IFNULL(fc.split_of, '') = '' AND IFNULL(fc.idempotency_key, '') = ''
		      AND """ + STANDS_ON_ITS_DATE + """
		      AND NOT EXISTS (SELECT 1 FROM `tabNight Audit Run` n JOIN `tabAgent Action Log` closed
		          ON closed.reference_doctype = 'Night Audit Run' AND closed.reference_name = n.name
		          AND closed.action_type = 'night_audit'
		          WHERE n.property = e.property AND n.business_date = fc.posting_date AND closed.creation <= e.posted_at)
		) p ON p.posted_at IS NOT NULL
		WHERE a.property = %(property)s
		  AND a.action_type IN ('create_share_reservation', 'combine_into_share', 'update_share_rate_mode', 'break_share')
		  AND (JSON_CONTAINS(a.after_snapshot, %(member)s) OR JSON_CONTAINS(a.before_snapshot, %(member)s)
		       OR a.reference_name IN %(payers)s
		       OR IFNULL(a.reference_doctype, '') != 'Reservation' OR IFNULL(a.reference_name, '') = ''
		       OR JSON_TYPE(a.before_snapshot) != 'ARRAY' OR JSON_TYPE(a.after_snapshot) != 'ARRAY'
		       OR IFNULL(JSON_LENGTH(a.before_snapshot), 0) = 0 OR IFNULL(JSON_LENGTH(a.after_snapshot), 0) = 0)
		ORDER BY a.creation
	""", {"property": property, "day": day, "room": snapshot["room"], "group": group,
	      "member": json.dumps({"share_group_id": group}), "payers": tuple(snapshot["rates"])})
	# Replay the group's transitions up to the posting. The first later before-image
	# must agree too: it can reveal an otherwise invisible missing transition.
	modes = set()
	previous = None
	for index, (changed, doctype, name, before, after, posted) in enumerate(rows):
		prior, current = _audit_modes(before, group), _audit_modes(after, group)
		if doctype != "Reservation" or not name or prior is None or current is None:
			# A fallback has no reliable group identity. Once this group exists,
			# its frozen allocation is safer than reconstructing an uncertain rate.
			if modes:
				return False
			previous = changed
			continue
		if prior != modes or changed == previous or changed == posted:
			return False
		if changed > posted:
			if index + 1 < len(rows) and rows[index + 1][0] == changed:
				return False
			break
		modes, previous = current, changed
	return bool(modes.intersection(tariff_modes))


def _frozen_fractions(property, snapshot, day, group=None):
	rounding = snapshot.get("rounding") or {}
	if "fractions" in rounding:
		return rounding["fractions"]
	# Older snapshots record the allocated components and the levy formula.
	# Recover the original price basis without consulting the guest's current tariff.
	levy = snapshot.get("levy") or {}
	if not snapshot.get("taxes") or not levy or levy.get("per_folio"):
		return {}
	from kamra.folio import room_levy_components
	from kamra.ledger import minor_unit

	rates = {n: Decimal(v) for n, v in snapshot["rates"].items()}
	gst = Decimal(str(snapshot["gst"]))
	gross = {n: rate + Decimal(snapshot["taxes"][n]) for n, rate in rates.items()}
	unit = minor_unit(property)
	levy_tax = Decimal(str(levy["gst"])) / 100
	posted = {n: gross[n] + Decimal(levy["amounts"][n]) + Decimal(levy["taxes"][n]) for n in rates}

	net_base = sum(rates.values())
	inclusive_base = Decimal(str(float(sum(gross.values()) / (1 + gst / 100))))

	def fractions(inclusive):
		weights = gross if inclusive else rates
		base = inclusive_base if inclusive else net_base
		pct, fixed = room_levy_components(base, levy["percent"], levy["fixed"], unit)
		raw_levy = pct + fixed
		if not levy_tax:
			raw_levy = raw_levy.quantize(unit, rounding=ROUND_HALF_UP)
		return _payer_fractions(base, gst, raw_levy, fixed, levy_tax, weights, posted, snapshot["lead"])

	if "rates_include_tax" in snapshot:
		return fractions(snapshot["rates_include_tax"])
	exclusive, inclusive = fractions(False), fractions(True)
	# The legacy checkpoint is cumulative. Compare its nightly change with
	# each posting basis, using only the frozen charges for this share.
	if "stored" in rounding and group:
		res = frappe._dict(property=property, share_group_id=group)
		prior, _ = _frozen_night(res, add_days(day, -1), False, None)
		opening = prior.get("rounding", {}) if prior.get("room") == snapshot["room"] else {}
		carry = _continuing_carry(opening, snapshot["lead"], unit)
		delta = Decimal(rounding["stored"]) - Decimal(carry.get("stored", "0"))
		matches = []
		for parts in (exclusive, inclusive):
			if sum(Decimal(part["stored"]) for part in parts.values()) == delta:
				matches.append(parts)
		if matches and all(parts == matches[0] for parts in matches):
			return matches[0]
	# Only pooled tariff modes priced the room from the lead's quote. A Custom
	# allocation stands as posted when it has no frozen tax basis or checkpoint.
	if not _priced_from_lead(property, snapshot, group, day):
		return {}
	if all(Decimal(value) == Decimal(inclusive[n][key])
	       for n, part in exclusive.items() for key, value in part.items()):
		return exclusive
	# The dated quote keeps the actual taxable price (including agreed/hand
	# rates) at nine decimals before discounts. The catalog rate can differ.
	night = frappe.db.get_value("Reservation Night Rate", {"parent": snapshot["lead"], "stay_date": day},
	                           ["quoted_base_rate", "discount_amount", "discount_value", "gst_rate"], as_dict=True)
	if (not night or not night.quoted_base_rate or night.discount_amount or night.discount_value
	    or Decimal(str(night.gst_rate or 0)) != gst):
		return {}
	quoted = Decimal(str(night.quoted_base_rate))
	precision = Decimal("0.000000001")
	net_matches = net_base.quantize(precision, rounding=ROUND_HALF_UP) == quoted
	inclusive_matches = inclusive_base.quantize(precision, rounding=ROUND_HALF_UP) == quoted
	if net_matches != inclusive_matches:
		return inclusive if inclusive_matches else exclusive
	return {}


def independent_rounding_adjustment(property, charges):
	"""Resume native document rounding after a pooled night on this same bill.

	Pooled charges already include their allocated minor units. Once this guest
	has a later independently priced night, their bill rounds the cumulative raw
	price again. Restore each earlier pooled night's fraction exactly once, even
	if the share has since broken or the guest has moved to a different room.
	"""
	independent = {}
	pooled = []
	reversed_lines = {row.get("reversal_of") for row in charges if row.get("reversal_of")}
	for row in charges:
		if (row.get("charge_type") != "Room" or not row.get("auto_posted") or row.get("reversal_of")
		    or row.get("name") in reversed_lines):
			continue
		name, day = row.get("reservation"), str(row.get("posting_date"))
		frozen = json.loads(row.share_snapshot) if row.get("share_snapshot") else None
		levy = (frozen or {}).get("levy") or {}
		if frozen and not (levy.get("per_folio") or levy.get("primary_pays")):
			pooled.append((name, day, row, frozen))
		elif row.get("amount") or row.get("gst_amount"):
			# A complimentary night does not restart this guest's rounding.
			independent[name] = max(day, independent.get(name, ""))
	adjustment = Decimal(0)
	for name, day, row, frozen in pooled:
		if day >= independent.get(name, ""):
			continue
		fraction = _frozen_fractions(property, frozen, day, row.get("share_group")).get(name, {})
		value = Decimal(fraction.get("pending" if row.is_new() else "stored", "0"))
		# A routing limit or a split bill may place only part of the room line here.
		rate = Decimal(frozen["rates"].get(name, "0"))
		amount = Decimal(str(row.amount))
		adjustment += value * amount / rate if rate and amount != rate else value
	return adjustment


def _night_charges(property, room_rate, gst, mode, members, lead, carry, pending):
	from kamra.folio import room_levy_components
	from kamra.ledger import minor_unit

	rates, room_taxes = _night_amounts(property, room_rate, gst, mode, members)
	prop = frappe.get_cached_doc("Property", property)
	unit = minor_unit(property)
	pct = Decimal(str(prop.get("room_levy_percent") or 0))
	fixed = prop.get("room_levy_per_night") or 0
	taxable = prop.get("room_levy_taxable")
	levy_gst = gst if taxable is None or cint(taxable) else 0
	tax_rate = Decimal(str(levy_gst or 0)) / 100
	per_folio = mode == "Full Rate Each"

	# Use the same base the independent room posting passes to the levy.
	base = Decimal(str(room_rate)) if gst else sum(rates.values())
	if per_folio or mode == "Primary Pays":
		base = rates[lead]
	percentage, flat = room_levy_components(base, pct, fixed, unit)
	levy = percentage + flat
	if not levy_gst:
		levy = levy.quantize(unit, rounding=ROUND_HALF_UP)

	currency = property_currency(property)
	rounding = {**carry, "fractions": {}} if carry else {}
	if per_folio or mode == "Primary Pays":
		# A full room charge has one payer. Let that folio round all its
		# taxable lines together, including any displaced pooled fractions.
		amounts = {name: levy if per_folio or name == lead else Decimal(0) for name in rates}
		taxes = {name: value * tax_rate for name, value in amounts.items()}
		payers = dict(carry.get("payers", {}))
		for name, rate in rates.items():
			if not rate and not amounts[name]:
				continue
			_, payers[name] = _round_shared_night(
				currency, rate, gst, amounts[name], tax_rate, payers.get(name, {}), pending,
			)
		rounding = {"currency": currency, "payers": payers}
	else:
		weights = {n: value + room_taxes.get(n, 0) for n, value in rates.items()} if rates_include_tax(property) else rates
		levy_due = levy.quantize(unit, rounding=ROUND_HALF_UP)
		tax_due = Decimal(0)
		if gst:
			grand, rounding = _round_shared_night(
				currency, base, gst, levy, tax_rate, _continuing_carry(carry, lead, unit), pending,
			)
			room_due = sum(rates.values()) + sum(room_taxes.values())
			if levy_gst and levy > 0:
				gross_levy = max(Decimal(0), grand - room_due)
				levy_due = min(gross_levy, max(unit, levy_due))
				tax_due = gross_levy - levy_due
			tax_target = grand - levy_due - tax_due - sum(rates.values())
			if tax_target != sum(room_taxes.values()):
				tax_weights = rates if any(rates.values()) else room_taxes
				room_taxes.update(_apportion(tax_target, tax_weights, unit))
		percentage_due = min(percentage.quantize(unit, rounding=ROUND_HALF_UP), levy_due) if flat else levy_due
		amounts = _apportion(percentage_due, rates, unit)
		amounts[lead] += levy_due - percentage_due
		taxes = _apportion(tax_due, amounts, unit)
		if gst:
			posted = {n: rate + room_taxes[n] + amounts[n] + taxes[n] for n, rate in rates.items()}
			fractions = _payer_fractions(base, gst, levy, flat, tax_rate, weights, posted, lead)
			rounding = _with_payer_rounding(rounding, carry, fractions, pending)
	return rates, room_taxes, {"percent": str(pct), "fixed": str(fixed), "gst": levy_gst, "per_folio": per_folio,
	        "primary_pays": mode == "Primary Pays",
	        "amounts": {name: str(value) for name, value in amounts.items()},
	        "taxes": {name: str(value) for name, value in taxes.items()}}, rounding


def _night_amounts(property, rate, gst, mode, members):
	from kamra.folio import cut_figure, line_figures, priced_with_tax
	from kamra.ledger import minor_unit

	currency = property_currency(property)
	if mode in ("Full Rate Each", "Primary Pays") and gst:
		# Keep the full native base on each folio. It computes GST and rounds
		# the guest's complete bill, including any taxable levy, independently.
		return {name: Decimal(str(rate)) if mode == "Full Rate Each" or primary else Decimal(0)
		        for name, primary, _custom in members}, {}
	if not gst or not priced_with_tax(property, "Room"):
		bases = allocate_rates(rate, currency, mode, members)
		if not gst:
			return bases, {}
		tax_rate = Decimal(str(gst)) / 100
		unit = minor_unit(property)
		if mode == "Custom":
			raw_tax = {name: base * tax_rate for name, base in bases.items()}
			return bases, _rounded_parts(raw_tax, unit)
		gross = (Decimal(str(rate)) * (1 + tax_rate)).quantize(unit, rounding=ROUND_HALF_UP)
		tax_due = gross - sum(bases.values())
		if not any(bases.values()):
			# A sub-minor rate can round to zero before GST but one unit with GST.
			return bases, allocate_rates(tax_due, currency, mode, members)
		return bases, _apportion(tax_due, bases, unit)

	whole = line_figures(property, "Room", rate, gst_rate=gst)
	gross = allocate_rates(whole["figure"], currency, mode, members)

	rate = whole["rate"]
	raw_tax = {name: amount * rate / (1 + rate) for name, amount in gross.items()}
	taxes = _rounded_parts(raw_tax, whole["unit"], cut_figure(whole, whole["figure"])[1])
	return {name: amount - taxes[name] for name, amount in gross.items()}, taxes


def nightly_share_rate(res, date) -> Decimal | None:
	night = nightly_share(res, date)
	return night["rate"] if night else None


def validate_reservation(res):
	"""Keep the inventory exception away from generic Resource and Desk writers."""
	before = res.get_doc_before_save()
	if not res.flags.share_operation:
		changed = any(
			(res.get(field) or None) != ((before.get(field) if before else None) or None)
			for field in FIELDS
		)
		if changed:
			frappe.throw(_("Use the Share panel to change a share reservation."), frappe.PermissionError)
		if before and before.get("share_group_id") and any(
			res.get(field) != before.get(field) for field in ("property", "room", "room_type", "guest", "sellable_unit")
		):
			frappe.throw(_("Break the share before changing its room or guest."))
	if res.get("share_group_id"):
		_check_eligible(res, require_active=False)
		fields = ("share_group_id", "status", "check_in_date", "check_out_date",
		          "adults", "children", "infants", "over_capacity_guests")
		if res.status in ACTIVE and (not before or any(res.get(key) != before.get(key) for key in fields)):
			_validate_capacity(res)


def _validate_capacity(res):
	"""Validate the aggregate party on each interval with constant occupancy."""
	from kamra.pricing import capacity_overflow

	cap = frappe.db.get_value("Room Type", res.room_type,
	                         ["adults_capacity", "children_capacity", "max_total_occupants"],
	                         as_dict=True)
	if not cap:
		return
	start, end = getdate(res.check_in_date), getdate(res.check_out_date)
	parties = [res, *(row for row in _member_rows(res, lock=True)
	                  if row.name != res.name and row.room == res.room and row.status in ACTIVE
	                  and getdate(row.check_in_date) < end and getdate(row.check_out_date) > start)]
	max_adults, max_children = cint(cap.adults_capacity), cint(cap.children_capacity)
	max_total = cint(cap.max_total_occupants) or (max_adults + max_children if max_adults and max_children else 0)
	changes = {}
	for party in parties:
		adults = cint(party.adults)
		children = max(0, cint(party.children) - cint(party.get("infants")))
		overflow = capacity_overflow(cap, adults, children)[0]
		extras = max(0, cint(party.get("over_capacity_guests")) - overflow)
		total = adults + children + extras
		for day, direction in ((max(start, getdate(party.check_in_date)), 1),
		                       (min(end, getdate(party.check_out_date)), -1)):
			change = changes.setdefault(day, [0, 0, 0])
			change[0] += direction * adults
			change[1] += direction * children
			change[2] += direction * total
	adults = children = total = 0
	for day, (add_adults, add_children, add_total) in sorted(changes.items()):
		adults += add_adults
		children += add_children
		total += add_total
		if ((max_adults and adults > max_adults) or (max_children and children > max_children)
		    or (max_total and total > max_total)):
			frappe.throw(_("The shared room exceeds its occupancy limit on {0}.").format(day))


def other_sharer_in_house(res, room: str) -> bool:
	if not res.get("share_group_id"):
		return False
	return bool(frappe.db.get_value("Reservation", {
		"property": res.property, "share_group_id": res.share_group_id,
		"room": room, "status": "Checked In", "name": ("!=", res.name),
	}, for_update=True))


def _check_eligible(res, *, require_active=True):
	if require_active and res.status not in ACTIVE:
		frappe.throw(_("Only confirmed or checked-in reservations can share a room."))
	if not res.room or res.get("is_day_use") or is_entire_place(res.room_type):
		frappe.throw(_("Assign a regular overnight room before sharing."))
	from kamra.packages import active_package_for_reservation
	if require_active and active_package_for_reservation(res):
		frappe.throw(_("Reservations with an active package cannot share a room."))


@contextmanager
def _operation(reservation):
	# Use the inventory lock also held by normal Reservation writes.
	property = frappe.db.get_value("Reservation", reservation, "property")
	assert_property_access(property)
	lock_property_for_inventory(property)
	doc = frappe.get_doc("Reservation", reservation, for_update=True)
	assert_property_access(doc.property)
	doc.check_permission("write")
	point = "share_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(point)
	try:
		yield doc
	except Exception:
		frappe.db.rollback(save_point=point)
		raise


def _set_share(doc, group, primary=False, mode=None, rate=0):
	doc.flags.share_operation = True
	doc.update(dict(zip(FIELDS, (group, int(primary), mode, rate), strict=True)))
	try:
		if doc.is_new():
			doc.insert()
		else:
			doc.save()
	finally:
		doc.flags.pop("share_operation", None)


def _ensure_folio(doc):
	from kamra.folio import open_folio
	return open_folio(doc, prearrival=True)


def _audit(action, doc, before, after):
	if not log_action(action, "Reservation", doc.name, doc.property,
	                  before_snapshot=before, after_snapshot=after):
		frappe.throw(_("The share change could not be recorded."))


def _snapshot(rows):
	return [{"reservation": row.name, "room": row.room, **{key: row.get(key) for key in FIELDS}}
	        for row in rows]


def _assert_unposted(doc, reservations, start):
	"""Do not change memberships or pricing for nights already posted in advance."""
	from kamra.folio import STANDS_ON_ITS_DATE
	if frappe.db.sql(
		"""SELECT fc.name FROM `tabFolio Charge` fc
		   JOIN `tabFolio` f ON f.name = fc.parent
		   WHERE f.property = %(property)s AND fc.posting_date >= %(start)s
		     AND COALESCE(NULLIF(fc.reservation, ''), f.reservation) IN %(names)s
		     AND fc.charge_type = 'Room' AND fc.auto_posted = 1 AND """ + STANDS_ON_ITS_DATE + """
		   LIMIT 1 FOR UPDATE""",
		{"property": doc.property, "start": str(start), "names": tuple(reservations)},
	):
		frappe.throw(_("A room night in this share has already been posted."))


def _assert_prior_shares_posted(doc, members, business_date):
	"""A break must not discard a prior night's outstanding allocation."""
	from kamra.folio import STANDS_ON_ITS_DATE, _charge_posted

	rows = frappe.db.sql(
		"""SELECT fc.reservation, fc.posting_date, fc.share_snapshot FROM `tabFolio Charge` fc
		   JOIN `tabFolio` f ON f.name = fc.parent
		   WHERE f.property = %(property)s AND fc.share_group = %(group)s
		     AND fc.posting_date < %(date)s AND fc.charge_type = 'Room'
		     AND fc.auto_posted = 1 AND """ + STANDS_ON_ITS_DATE + " FOR UPDATE",
		{"property": doc.property, "group": doc.share_group_id, "date": business_date},
	)
	# The native cancellation/no-show policy owns non-arrival fees. Their
	# unposted room allocations expire; posted lines and other payers stay frozen.
	billable = {m.name for m in members if m.status not in ("No Show", "Cancelled")}
	posted = set()
	required = set()
	for reservation, day, snapshot in rows:
		posted.add((reservation, day))
		if snapshot:
			required.update((name, day) for name in json.loads(snapshot)["rates"] if name in billable)

	for member in members:
		if member.status not in ("Checked In", "Checked Out"):
			continue
		night = getdate(member.check_in_date)
		end = min(business_date, getdate(member.check_out_date))
		while night < end:
			required.add((member.name, night))
			night = getdate(add_days(night, 1))

	for name, night in sorted(required - posted):
		# A night before joining the share may already stand on another folio.
		if not _charge_posted(name, "Room", str(night), lock=True):
			frappe.throw(_("Post previous room nights before breaking the share."))


def _join(anchor, new, custom_rate):
	_check_eligible(anchor)
	_check_eligible(new)
	members = _member_rows(anchor, lock=True)
	peers = members or [anchor]
	if new.guest in {peer.guest for peer in peers}:
		frappe.throw(_("Each sharer must have a different guest profile."))
	overlap = [max(getdate(peer.check_in_date), getdate(new.check_in_date))
	           for peer in peers if peer.status in ACTIVE
	           and getdate(peer.check_in_date) < getdate(new.check_out_date)
	           and getdate(new.check_in_date) < getdate(peer.check_out_date)]
	if not overlap or getdate(new.check_out_date) <= getdate(new.check_in_date):
		frappe.throw(_("The sharer's dates must overlap an active stay in the room."))
	_assert_unposted(anchor, [new.name or "", *(peer.name for peer in peers)], min(overlap))
	mode = anchor.get("share_rate_mode") or "Split Equal"
	rate = _custom(anchor, custom_rate) if mode == "Custom" else 0
	group = anchor.get("share_group_id") or frappe.generate_hash(length=24)
	if not members:
		_set_share(anchor, group, True, mode)
		_ensure_folio(anchor)
	_set_share(new, group, False, mode, rate)
	return {"reservation": new.name, "group_id": group, "folio": _ensure_folio(new)}


def _custom(doc, amount):
	quantum = Decimal(1).scaleb(-_minor_unit_for_currency(property_currency(doc.property)))
	return float(_money(amount, quantum, custom=True))


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", scope={"guest": "Guest"})
def create_share_reservation(reservation: str, guest: str | None = None,
                             check_in_date: str | None = None, check_out_date: str | None = None,
                             custom_rate: float | None = None, guest_name: str | None = None):
	"""Book an additional guest into the same room with their own dates and bill."""
	with _operation(reservation) as anchor:
		if bool(guest) == bool((guest_name or "").strip()):
			frappe.throw(_("Select an existing guest or enter a new guest name."))
		if guest:
			assert_guest_access(guest)
			frappe.get_doc("Guest", guest).check_permission("read")
		else:
			guest = frappe.get_doc({"doctype": "Guest", "first_name": guest_name.strip()}).insert().name
		new = frappe.get_doc({
			"doctype": "Reservation", "property": anchor.property, "room_type": anchor.room_type,
			"room": anchor.room, "sellable_unit": anchor.get("sellable_unit"), "status": "Confirmed",
			"guest": guest, "adults": 1, "check_in_date": check_in_date or anchor.check_in_date,
			"check_out_date": check_out_date or anchor.check_out_date, "rate_plan": anchor.rate_plan,
		})
		before = _snapshot([anchor])
		result = _join(anchor, new, custom_rate)
		_audit("create_share_reservation", anchor, before, _snapshot([anchor, new]))
		return result


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", scope={"other_reservation": "Reservation"})
def combine_into_share(reservation: str, other_reservation: str, custom_rate: float | None = None):
	"""Put an existing same-category arrival in the share, keeping its folio."""
	with _operation(reservation) as anchor:
		other = frappe.get_doc("Reservation", other_reservation, for_update=True)
		assert_property_access(other.property)
		other.check_permission("write")
		if (other.name == anchor.name or other.property != anchor.property or other.room_type != anchor.room_type
		    or other.status != "Confirmed" or other.get("share_group_id")):
			frappe.throw(_("Choose a confirmed, unshared reservation for the same room type and property."))
		before = _snapshot([anchor, other])
		other.room = anchor.room
		other.sellable_unit = anchor.get("sellable_unit")
		result = _join(anchor, other, custom_rate)
		_audit("combine_into_share", anchor, before, _snapshot([anchor, other]))
		return result


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", scope={"new_room": "Room"})
def break_share(reservation: str, new_room: str):
	"""Detach a sharer and assign a separate room of the same type."""
	from kamra.api import _available_rooms, _room_move_target
	from kamra.housekeeping import clear_stay_room_state
	from kamra.business_date import get_business_date

	with current_inventory_reads(), _operation(reservation) as doc:
		members = _member_rows(doc, lock=True)
		if not members or doc.status not in ACTIVE:
			frappe.throw(_("Choose an active share reservation."))
		if not new_room or doc.room == new_room:
			frappe.throw(_("Choose a different room to break the share."))
		target = frappe.db.get_value("Room", new_room, ["property", "room_type"],
		                             as_dict=True, for_update=True)
		if not target or (target.property, target.room_type) != (doc.property, doc.room_type):
			frappe.throw(_("Choose a room of the same type at this property."))
		business_date = getdate(get_business_date(doc.property))
		_assert_unposted(doc, [m.name for m in members], business_date)
		before = _snapshot(members)
		remaining = [m for m in members if m.name != doc.name]
		old_room = doc.room
		was_primary = bool(doc.is_share_primary)
		_assert_prior_shares_posted(doc, members, business_date)
		choices = _available_rooms(doc.property, doc.room_type, doc.check_in_date, doc.check_out_date,
		                           group_booking=doc.group_booking, ready_now=int(doc.status == "Checked In"),
		                           for_assignment=1, for_update=True)
		if not any(room.name == new_room for room in choices):
			frappe.throw(_("Room unavailable"))
		if doc.status == "Checked In":
			_room_move_target(doc, new_room, strict=True, lock=True)
			doc.flags.kamra_room_move = True
		doc.room = new_room
		doc.sellable_unit = None
		_set_share(doc, None)
		if doc.status == "Checked In":
			from kamra.folio import room_assignment_start

			move = log_action("room_move", "Reservation", doc.name, doc.property,
			                  rationale=f"{old_room} → {new_room}",
			                  before_snapshot={"room": old_room, "room_type": doc.room_type,
			                                   "assignment_since": room_assignment_start(doc, old_room)},
			                  after_snapshot={"room": new_room, "room_type": doc.room_type,
			                                  "effective_date": str(business_date)})
			# A fallback audit row lacks the dated snapshots needed to replay the move.
			if not move or frappe.db.get_value("Agent Action Log", move, "reference_name") != doc.name:
				frappe.throw(_("The share change could not be recorded."))
			clear_stay_room_state(doc.property, new_room)
			frappe.db.set_value("Room", new_room, "occupancy_status", "Occupied")
			if not any(m.status == "Checked In" for m in remaining):
				clear_stay_room_state(doc.property, old_room)
				hk = frappe.db.get_value("Room", old_room, "housekeeping_status", for_update=True)
				values = {"occupancy_status": "Vacant"}
				if hk not in ("Out of Order", "Out of Service"):
					values["housekeeping_status"] = "Dirty"
				frappe.db.set_value("Room", old_room, values)
		if len(remaining) == 1 or was_primary:
			peer = next((m for m in remaining if m.status in ACTIVE), remaining[0] if remaining else None)
			if peer:
				other = frappe.get_doc("Reservation", peer.name, for_update=True)
				other.check_permission("write")
				if len(remaining) == 1:
					_set_share(other, None)
				else:
					_set_share(other, peer.share_group_id, True, peer.share_rate_mode, peer.share_custom_rate)
		after = [doc, *(frappe.get_doc("Reservation", member.name) for member in remaining)]
		_audit("break_share", doc, before, _snapshot(after))
		return {"reservation": doc.name, "room": doc.room}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk")
def update_share_rate_mode(reservation: str, mode: str, custom_rates: dict | str | None = None):
	"""Set the group's room rate mode for unposted nights."""
	from kamra.business_date import get_business_date

	with _operation(reservation) as doc:
		members = _member_rows(doc, lock=True)
		if mode not in MODES or not members or doc.status not in ACTIVE:
			frappe.throw(_("Select a valid share rate mode for an active share."))
		rates = frappe.parse_json(custom_rates) if isinstance(custom_rates, str) else custom_rates
		if mode == "Custom" and (not isinstance(rates, dict) or set(rates) != {m.name for m in members}):
			frappe.throw(_("Enter a custom nightly rate for every sharer."))
		_assert_unposted(doc, [m.name for m in members], get_business_date(doc.property))
		before = _snapshot(members)
		for member in members:
			peer = frappe.get_doc("Reservation", member.name, for_update=True)
			peer.check_permission("write")
			rate = _custom(peer, rates[peer.name]) if mode == "Custom" else 0
			_set_share(peer, member.share_group_id, member.is_share_primary, mode, rate)
		_audit("update_share_rate_mode", doc, before, _snapshot(_member_rows(doc)))
		return {"reservation": doc.name, "mode": mode}


def share_summary(res) -> dict:
	"""The desk view only includes separately readable reservations and folios."""
	currency = property_currency(res.property)
	rows = _member_rows(res)
	members = []
	for row in rows:
		doc = res if row.name == res.name else frappe.get_doc("Reservation", row.name)
		if not doc.has_permission("read"):
			continue
		folio = frappe.db.get_value("Folio", {"reservation": row.name, "folio_type": "Guest"},
		                            "name", order_by="creation desc")
		if folio and not frappe.has_permission("Folio", "read", doc=folio):
			folio = None
		members.append({
			"reservation": row.name, "guest_name": row.guest_name, "folio": folio,
			"primary": bool(row.is_share_primary), "custom_rate": float(row.share_custom_rate or 0),
			"check_in": str(row.check_in_date), "check_out": str(row.check_out_date),
		})
	return {
		"group_id": res.get("share_group_id") or None, "mode": res.get("share_rate_mode") or "Split Equal",
		"currency": currency, "decimals": _minor_unit_for_currency(currency), "members": members,
		"can_manage": bool(res.room and res.status in ACTIVE and not res.get("is_day_use")
		                   and not is_entire_place(res.room_type) and len(members) == len(rows)
		                   and res.has_permission("write") and set(frappe.get_roles()) & (set(ADMIN) | {"Front Desk"})),
	}
