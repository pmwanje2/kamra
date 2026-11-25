"""One physical room with separate reservations and guest folios."""

from __future__ import annotations

import json
from contextlib import contextmanager
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

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


def nightly_share(res, date, *, lock=False, advance_bill=False) -> dict | None:
	"""The active payer set and price for a room night, frozen by its first posting."""
	group = res.get("share_group_id")
	statuses = ("Checked In", "Checked Out", "Confirmed") if advance_bill else ("Checked In", "Checked Out")
	if not group or not res.room or res.status not in statuses:
		return None
	from kamra.folio import STANDS_ON_ITS_DATE, _nightly_gst, _nightly_room_rate, current_date_contract_night

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
	frozen = frappe.db.sql(query, {"property": res.property, "date": str(date), "group": group})
	if frozen:
		data = json.loads(frozen[0][0])
		return _share_line(data, res.name)
	members = _member_rows(res, lock=lock, date=date, statuses=statuses)
	if not members:
		return None
	leader = members[0]
	rate_source = res if leader.name == res.name else frappe.get_doc("Reservation", leader.name)
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
	rates, taxes = _night_amounts(
		res.property, rate, gst, mode,
		[(m.name, m.name == leader.name, m.share_custom_rate) for m in members],
	)
	fx = {}
	if current:
		fx = {"contract_currency": current["currency"], "contract_fx_rate": current["fx_rate"],
		      "contract_fx_date": str(current["fx_date"]) if current["fx_date"] else None,
		      "contract_fx_stale": int(current["stale"])}
	return _share_line({
		"room": res.room, "gst": gst, "category": leader.rate_category or "",
		"lead": leader.name, "fx": fx,
		"levy": _share_levy(res.property, rates, taxes, rate, gst, mode, leader.name),
		"rates": {name: str(value) for name, value in rates.items()},
		"taxes": {name: str(value) for name, value in taxes.items()},
	}, res.name)


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


def _share_levy(property, rates, room_taxes, room_rate, gst, mode, lead):
	from kamra.folio import room_levy_components
	from kamra.ledger import minor_unit

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
	if per_folio:
		base = rates[lead]
	percentage, flat = room_levy_components(base, pct, fixed, unit)
	levy = percentage + flat
	if not levy_gst:
		levy = levy.quantize(unit, rounding=ROUND_HALF_UP)

	if per_folio:
		# Native taxable lines retain sub-minor precision until folio rounding.
		amounts = dict.fromkeys(rates, levy)
		taxes = dict.fromkeys(rates, levy * tax_rate)
	else:
		levy_due = levy.quantize(unit, rounding=ROUND_HALF_UP)
		tax_due = Decimal(0)
		if levy_gst and levy > 0:
			room_due = sum(rates.values()) + sum(room_taxes.values())
			# Match folio._recalculate: sum posted amounts and line taxes,
			# then round the document once in the property's currency.
			charges = base + levy
			tax = base * Decimal(str(gst)) / 100 + levy * Decimal(str(levy_gst)) / 100
			grand = round_rate_amount(charges + tax, 0, "Nearest", property_currency(property))
			gross_levy = grand - room_due
			# A positive residual needs a levy line even when its base alone
			# rounds to zero. Never allocate tax to a zero-amount line.
			levy_due = min(gross_levy, max(unit, levy_due))
			tax_due = gross_levy - levy_due
		percentage_due = min(percentage.quantize(unit, rounding=ROUND_HALF_UP), levy_due) if flat else levy_due
		amounts = _apportion(percentage_due, rates, unit)
		amounts[lead] += levy_due - percentage_due
		taxes = _apportion(tax_due, amounts, unit)
	return {"percent": str(pct), "fixed": str(fixed), "gst": levy_gst, "per_folio": per_folio,
	        "amounts": {name: str(value) for name, value in amounts.items()},
	        "taxes": {name: str(value) for name, value in taxes.items()}}


def _night_amounts(property, rate, gst, mode, members):
	from kamra.folio import cut_figure, line_figures, priced_with_tax
	from kamra.ledger import minor_unit

	currency = property_currency(property)
	if mode == "Full Rate Each" and gst:
		# Keep the full native base on each folio. It computes GST and rounds
		# the guest's complete bill, including any taxable levy, independently.
		return dict.fromkeys((name for name, _primary, _custom in members), Decimal(str(rate))), {}
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
	from kamra.business_date import get_business_date

	with _operation(reservation) as doc:
		members = _member_rows(doc, lock=True)
		if not members or doc.status not in ACTIVE:
			frappe.throw(_("Choose an active share reservation."))
		if not new_room or doc.room == new_room:
			frappe.throw(_("Choose a different room to break the share."))
		if frappe.db.get_value("Room", new_room, ["property", "room_type"]) != (doc.property, doc.room_type):
			frappe.throw(_("Choose a room of the same type at this property."))
		business_date = getdate(get_business_date(doc.property))
		_assert_unposted(doc, [m.name for m in members], business_date)
		before = _snapshot(members)
		remaining = [m for m in members if m.name != doc.name]
		old_room = doc.room
		was_primary = bool(doc.is_share_primary)
		_assert_prior_shares_posted(doc, members, business_date)
		doc.room = new_room
		doc.sellable_unit = None
		_set_share(doc, None)
		if doc.status == "Checked In":
			frappe.db.set_value("Room", new_room, "occupancy_status", "Occupied")
			if not any(m.status == "Checked In" for m in remaining):
				hk = frappe.db.get_value("Room", old_room, "housekeeping_status")
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
