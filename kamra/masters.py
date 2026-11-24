"""Client-configurable code masters (OPERA Controls / Configuration).

OPERA lets each hotel keep its own Market codes, Origin codes, Cancellation
Reasons, Payment Methods and Package (board) codes. These were fixed
Selects and Python constants here; each is now a small master DocType that
the matching field links to, seeded with the values the product shipped.

A seeded code the PMS itself writes or reasons about (Origin OTA, Payment
Method Cash, Cancellation Reason Other, Payer Type Company ...) is a
*system* code: the hotel may reword it, but not delete or switch it off, so
code that names it keeps working. Everything else - seeded or added by the
hotel - is the hotel's to change.

Masters are site-wide, like Reason Code and Transaction Code. ensure_masters
seeds missing rows on install and migrate and never touches a row that
exists, so a hotel's edits survive upgrades.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

MARKET = "Market Code"
ORIGIN = "Origin Code"
BOOKING_TYPE = "Booking Type"
RELATION = "Booker Relation"
CANCELLATION = "Cancellation Reason"
PAYMENT = "Payment Method"
MEAL_PLAN_CODE = "Meal Plan Code"
PAYER = "Payer Type"
CHECKIN_REQUEST = "Check-in Request"

# the reason a cancellation falls back to when the one given is unknown
OTHER_REASON = "Other"


def _rows(codes, system=(), **extra) -> list[dict]:
	return [{"code": c, "description": c, "system": int(c in system), **extra}
	        for c in codes]


# Seed rows, in the order the desk lists them. Values are exactly the old
# Select options and check-in constants, so stored data stays valid.
SEEDS: dict[str, list[dict]] = {
	MARKET: _rows(("Corporate", "Leisure", "Group", "MICE", "Government", "Wedding",
	               "Long Stay", "Airline Crew", "Complimentary", "House Use"),
	              # create_booking files a company stay under Corporate
	              system=("Corporate",)),
	ORIGIN: _rows(("Manual", "AI Agent", "Phone", "Walk-in", "OTA", "Website", "PMS"),
	              system=("Manual", "AI Agent", "Phone", "Walk-in", "OTA", "Website", "PMS")),
	BOOKING_TYPE: _rows(("Individual", "Group", "Corporate"),
	                    system=("Individual", "Group", "Corporate")),
	RELATION: _rows(("Assistant", "Family", "Company Travel Desk", "Travel Agent", "Other")),
	CANCELLATION: _rows(("Guest request", "Change of plans", "Duplicate booking",
	                     "Payment failed", "Weather / travel disruption",
	                     "Booked elsewhere", OTHER_REASON),
	                    # the desk default, the channel manager's and the fallback
	                    system=("Guest request", "Booked elsewhere", OTHER_REASON)),
	PAYMENT: [
		{"code": code, "description": code, "system": 1, "tender_type": tender}
		for code, tender in (
			("Cash", "Cash"), ("Foreign Cash", "Foreign Cash"), ("Card", "Card"),
			("UPI", "Instant"), ("Bank Transfer", "Bank Transfer"),
			("Mobile Money", "Instant"), ("OTA Prepaid", "OTA Prepaid"),
			("Company Credit", "Company Credit"), ("Payment Link", "Payment Link"),
			("Cheque", "Cheque"), ("Other", "Other"))
	],
	MEAL_PLAN_CODE: [
		{"code": code, "description": description, "system": 0}
		for code, description in (
			("EP", "Room only"), ("CP", "Breakfast"),
			("MAP", "Breakfast and one meal"), ("AP", "All meals"))
	],
	PAYER: _rows(("OTA prepaid", "OTA, guest pays here", "Guest", "Company", "Group"),
	             system=("OTA prepaid", "OTA, guest pays here", "Guest", "Company", "Group")),
	CHECKIN_REQUEST: [
		{"code": code, "description": label, "system": 0, "team": team,
		 "priority": priority, "charge_type": charge_type}
		for code, label, team, priority, charge_type in (
			("extra_bed", "Extra bed", "Housekeeping", "Medium", "Extra Guests"),
			("wake_up_call", "Wake-up call", "Front Desk", "Medium", None),
			("airport_drop", "Airport drop", "Concierge", "Medium", "Taxi"),
			("dietary", "Dietary needs", "Room Service", "Medium", None),
			("special_occasion", "Special occasion", "Concierge", "Medium", "Misc"))
	],
}

# fields that store a master's code; the v48 patch adopts every value in use
LINKED_FIELDS: tuple[tuple[str, str, str], ...] = (
	("Reservation", "market_segment", MARKET),
	("Reservation", "source", ORIGIN),
	("Reservation", "booking_type", BOOKING_TYPE),
	("Reservation", "booker_relation", RELATION),
	("Reservation", "cancellation_reason", CANCELLATION),
	("Folio", "payment_method", PAYMENT),
	("Folio Payment", "mode", PAYMENT),
	("Cashier Transaction", "mode", PAYMENT),
	("Banquet Receipt", "mode", PAYMENT),
	("POS Order", "payment_mode", PAYMENT),
	("Meal Plan", "code", MEAL_PLAN_CODE),
)

SYSTEM_TENDER_TYPES: dict[str, str] = {
	row["code"]: row["tender_type"] for row in SEEDS[PAYMENT]
}


def _insert(doctype: str, row: dict) -> None:
	doc = frappe.get_doc({"doctype": doctype, **{k: v for k, v in row.items() if v is not None}})
	doc.flags.kamra_seeding = True
	doc.insert(ignore_permissions=True)


def ensure_masters() -> None:
	"""Insert every seed row that is missing. Existing rows are left as the
	hotel keeps them. Seeds go in with a sort order of 10, 20, ... so a
	code a hotel adds later (sort order 0) lists after them."""
	for doctype, rows in SEEDS.items():
		if not frappe.db.table_exists(doctype):
			continue
		for i, row in enumerate(rows, start=1):
			if doctype == PAYMENT:
				exists = bool(frappe.db.sql(
					"SELECT 1 FROM `tabPayment Method` WHERE code = %s AND COALESCE(property, '') = '' LIMIT 1",
					(row["code"],),
				))
				if not exists:
					_insert(doctype, {**row, "sort_order": i * 10})
			elif not frappe.db.exists(doctype, row["code"]):
				_insert(doctype, {**row, "sort_order": i * 10})


def _payment_mode_properties() -> dict[str, set[str]]:
	"""Map each stored payment mode string to the set of properties that used it."""
	props_by_mode: dict[str, set[str]] = {}
	sources = (
		("Folio", "payment_method", None, "SELECT DISTINCT payment_method, property FROM `tabFolio` WHERE payment_method IS NOT NULL AND payment_method != ''"),
		("Folio Payment", "mode", "Folio", "SELECT DISTINCT c.mode, p.property FROM `tabFolio Payment` c LEFT JOIN `tabFolio` p ON p.name = c.parent WHERE c.mode IS NOT NULL AND c.mode != ''"),
		("Cashier Transaction", "mode", None, "SELECT DISTINCT mode, property FROM `tabCashier Transaction` WHERE mode IS NOT NULL AND mode != ''"),
		("Banquet Receipt", "mode", "Venue Booking", "SELECT DISTINCT c.mode, p.property FROM `tabBanquet Receipt` c LEFT JOIN `tabVenue Booking` p ON p.name = c.parent WHERE c.mode IS NOT NULL AND c.mode != ''"),
		("POS Order", "payment_mode", None, "SELECT DISTINCT payment_mode, property FROM `tabPOS Order` WHERE payment_mode IS NOT NULL AND payment_mode != ''"),
	)
	for doctype, col, parent_dt, sql in sources:
		if not (frappe.db.table_exists(doctype) and frappe.db.has_column(doctype, col)):
			continue
		if parent_dt and not frappe.db.table_exists(parent_dt):
			continue
		for mode, prop in frappe.db.sql(sql):
			bucket = props_by_mode.setdefault(mode, set())
			if prop:
				bucket.add(prop)
	return props_by_mode


def adopt_values_in_use() -> int:
	"""A master row for every value stored in a linked field, so data an
	older release wrote (or a value the old Select never listed) still
	saves. Adopted rows are the hotel's codes, never system codes.
	Adopted Payment Methods are scoped per property that used them."""
	added = 0
	payment_props = _payment_mode_properties() if frappe.db.table_exists(PAYMENT) else {}
	has_pm_property = frappe.db.table_exists(PAYMENT) and frappe.db.has_column(PAYMENT, "property")

	for doctype, fieldname, master in LINKED_FIELDS:
		if not (frappe.db.table_exists(doctype) and frappe.db.table_exists(master)
		        and frappe.db.has_column(doctype, fieldname)):
			continue
		values = frappe.get_all(doctype, filters={fieldname: ("is", "set")},
		                        fields=[fieldname], distinct=True, pluck=fieldname)
		for value in values:
			if master == PAYMENT:
				shared = bool(frappe.db.sql(
					"SELECT 1 FROM `tabPayment Method` WHERE code = %s AND COALESCE(property, '') = '' LIMIT 1",
					(value,),
				))
				if shared:
					continue
				props = sorted(payment_props.get(value) or set())
				targets = props if (has_pm_property and props) else [None]
				for prop in targets:
					exists = bool(frappe.db.sql(
						"SELECT 1 FROM `tabPayment Method` WHERE code = %s AND COALESCE(property, '') = %s LIMIT 1",
						(value, prop or ""),
					))
					if exists:
						continue
					row = {"code": value, "description": value, "system": 0, "tender_type": "Other"}
					if has_pm_property and prop:
						row["property"] = prop
					try:
						_insert(master, row)
					except frappe.ValidationError:
						frappe.log_error(title=f"{master}: stored value {value!r} could not be adopted",
						                 message=f"{doctype}.{fieldname} holds {value!r}; add the code by hand.")
						continue
					added += 1
				continue
			if frappe.db.exists(master, value):
				continue
			row = {"code": value, "description": value, "system": 0}
			try:
				_insert(master, row)
			except frappe.ValidationError:
				# one value that can't be a code (a name Frappe refuses) must
				# not stop the migrate; the record carrying it is listed
				frappe.log_error(title=f"{master}: stored value {value!r} could not be adopted",
				                 message=f"{doctype}.{fieldname} holds {value!r}; add the code by hand.")
				continue
			added += 1
	return added


def _sorted(rows: list) -> list:
	return sorted(rows, key=lambda r: (r.sort_order or 10**9, r.creation or ""))


def master_rows(doctype: str, *, enabled_only: bool = True, fields: tuple = ()) -> list:
	"""The master's rows in desk order (seeds first, then the hotel's own)."""
	filters = {"disabled": 0} if enabled_only else {}
	return _sorted(frappe.get_all(
		doctype, filters=filters,
		fields=["name", "description", "system", "sort_order", "creation", *fields]))


def enabled_codes(doctype: str) -> list[str]:
	return [r.name for r in master_rows(doctype)]


def _has_db() -> bool:
	return bool(getattr(frappe.local, "db", None))


def _offered_rows(doctype: str, filters: dict, fields: list) -> list:
	"""Rows for a list a screen offers on top of what it already has. A
	master that can't be read (a site mid-migrate, a unit test with no
	database) offers nothing extra rather than breaking the screen: the
	hotel's own codes are simply not offered, and every check that
	enforces a code (kamra.api._do_cancel, kamra.checkin) still reads the
	master strictly."""
	if not _has_db():
		return []
	try:
		if not frappe.db.table_exists(doctype):
			return []
		rows = frappe.get_all(doctype, filters=filters, fields=fields)
		return rows if isinstance(rows, list) else []
	except Exception:
		frappe.logger("kamra").warning("code master %s could not be read", doctype, exc_info=True)
		return []


def choices(doctype: str) -> list[dict]:
	"""[{value, label}] for a desk dropdown: the code is stored, the
	description is shown."""
	rows = _sorted(_offered_rows(doctype, {"disabled": 0},
	                             ["name", "description", "sort_order", "creation"]))
	return [{"value": r.name, "label": r.description or r.name} for r in rows]


def desk_vocabulary() -> dict:
	"""The lists the booking and cancel screens offer (property_locale)."""
	return {
		"market_codes": choices(MARKET),
		"booking_types": choices(BOOKING_TYPE),
		"booker_relations": choices(RELATION),
		"cancellation_reasons": choices(CANCELLATION),
	}


def _pm_has_property_col() -> bool:
	if not _has_db():
		return False
	try:
		return bool(frappe.db.table_exists(PAYMENT) and frappe.db.has_column(PAYMENT, "property"))
	except Exception:
		return False


def _pm_fields(has_prop_col: bool) -> list[str]:
	return [
		"name", "code", "description", "tender_type", "mode_of_payment",
		"sort_order", "system", "disabled", "creation",
		*(["property"] if has_prop_col else []),
	]


def resolve_payment_method(code: str | None, property: str | None = None) -> dict | None:
	"""Resolve one Payment Method by (property, code): a property-scoped row
	overrides the shared (blank-property) row of the same code."""
	if not code or not _has_db():
		return None
	has_prop_col = _pm_has_property_col()
	rows = _offered_rows(PAYMENT, {"code": code}, _pm_fields(has_prop_col))
	if not rows:
		return None
	if not has_prop_col:
		return rows[0]
	shared = None
	for row in rows:
		row_prop = (row.get("property") or "").strip() if isinstance(row.get("property"), str) else ""
		if property and row_prop == property:
			return row
		if not row_prop and shared is None:
			shared = row
	return shared


def effective_payment_methods(
	property: str | None = None,
	*,
	enabled_only: bool = True,
	include_system: bool = True,
	tender_type: str | None = None,
) -> list:
	"""Payment Method rows effective for `property`, merging shared rows with
	property-scoped overrides of the same code BEFORE filtering by disabled,
	system, or tender_type."""
	if not _has_db():
		return []
	has_prop_col = _pm_has_property_col()
	rows = _offered_rows(PAYMENT, {}, _pm_fields(has_prop_col))
	by_code: dict[str, dict] = {}
	if has_prop_col:
		for row in rows:
			row_prop = (row.get("property") or "").strip() if isinstance(row.get("property"), str) else ""
			code = row.get("code") or row.get("name")
			if not row_prop and code:
				by_code[code] = row
		if property:
			for row in rows:
				row_prop = (row.get("property") or "").strip() if isinstance(row.get("property"), str) else ""
				code = row.get("code") or row.get("name")
				if row_prop == property and code:
					by_code[code] = row
	else:
		for row in rows:
			code = row.get("code") or row.get("name")
			if code:
				by_code[code] = row

	result = list(by_code.values())
	if enabled_only:
		result = [r for r in result if not int(r.get("disabled") or 0)]
	if not include_system:
		result = [r for r in result if not int(r.get("system") or 0)]
	if tender_type:
		result = [r for r in result if r.get("tender_type") == tender_type]
	return _sorted(result)


def disabled_payment_methods(property: str | None = None) -> set[str]:
	"""Payment method codes whose effective row for `property` is disabled."""
	if not property:
		return set()
	return {
		(r.get("code") or r.get("name"))
		for r in effective_payment_methods(property, enabled_only=False, include_system=True)
		if int(r.get("disabled") or 0)
	}


def client_payment_methods(
	tender_type: str | None = None,
	*,
	property: str | None = None,
	enabled_only: bool = True,
) -> list[str]:
	"""Payment methods added or overridden by this property (not untouched
	shared system codes). Returns CODE text; pack-only calls (property=None)
	never leak any property's methods."""
	if not property:
		return []
	rows = effective_payment_methods(
		property, enabled_only=enabled_only, include_system=False, tender_type=tender_type,
	)
	return [r.get("code") or r.get("name") for r in rows]


def client_tender_types(property: str | None = None) -> dict[str, str]:
	"""{code: tender type} for effective payment methods at `property`,
	switched-off ones included, so a till that took one still counts it in
	the right bucket."""
	rows = effective_payment_methods(property, enabled_only=False, include_system=True)
	return {
		(r.get("code") or r.get("name")): r.get("tender_type")
		for r in rows
		if r.get("tender_type")
	}


def assert_folio_payment_method(property: str | None, code: str | None) -> None:
	"""Validate a Folio window's payment_method code against the per-property
	Payment Method master (compatible with both field:code and per-property
	<property>::<code> keying). Shared system codes and this property's own
	enabled codes are allowed; unknown, disabled, or foreign-property codes
	are refused."""
	code = (code or "").strip()
	if not code or not _has_db() or not frappe.db.table_exists(PAYMENT):
		return
	has_prop_col = frappe.db.has_column(PAYMENT, "property")
	fields = ["name", "code", "disabled", *(["property"] if has_prop_col else [])]
	rows = frappe.get_all(PAYMENT, filters={"code": code}, fields=fields)
	if not rows and code in SYSTEM_TENDER_TYPES:
		ensure_masters()
		rows = frappe.get_all(PAYMENT, filters={"code": code}, fields=fields)
	match = None
	if has_prop_col:
		prop_rows = [r for r in rows if r.get("property") and property and r.get("property") == property]
		shared_rows = [r for r in rows if not r.get("property")]
		match = prop_rows[0] if prop_rows else (shared_rows[0] if shared_rows else None)
	elif rows:
		match = rows[0]
	if not match or int(match.get("disabled") or 0):
		frappe.throw(_("Payment method {0} is not available for this property.").format(code))


def payment_tender_types(property: str | None = None) -> dict[str, str]:
	"""{code: tender_type} for every enabled Payment Method in scope for
	`property` (shared rows overridden by property rows), falling back to
	seeded system tender types when no database is available."""
	out = dict(SYSTEM_TENDER_TYPES)
	for row in effective_payment_methods(property, enabled_only=False, include_system=True):
		code = row.get("code") or row.get("name")
		if not code:
			continue
		if int(row.get("disabled") or 0):
			out.pop(code, None)
		elif isinstance(row.get("tender_type"), str) and row["tender_type"]:
			out[code] = row["tender_type"]
	return out


def payment_tender_type(mode: str | None, property: str | None = None) -> str | None:
	"""Return Payment Method.tender_type for (property, mode), falling back
	to seeded system tender types when no database is available."""
	if not mode:
		return None
	row = resolve_payment_method(mode, property)
	if row and isinstance(row.get("tender_type"), str) and row["tender_type"]:
		return row["tender_type"]
	return SYSTEM_TENDER_TYPES.get(mode)


def default_mode_for_tender(property: str | None, tender_type: str) -> str:
	"""Canonical payment method code for a tender_type at `property`.
	Prefers the enabled system code of that tender_type when active, then
	the first enabled property/shared code of that tender_type, else the
	seeded system code for that tender_type."""
	rows = effective_payment_methods(
		property, enabled_only=True, include_system=True, tender_type=tender_type,
	)
	for row in rows:
		if int(row.get("system") or 0):
			return row.get("code") or row.get("name")
	if rows:
		return rows[0].get("code") or rows[0].get("name")
	for code, tt in SYSTEM_TENDER_TYPES.items():
		if tt == tender_type:
			return code
	return tender_type


def is_system_payment_method(mode: str | None, property: str | None = None) -> bool:
	"""True when `mode` resolves to a seeded system Payment Method."""
	if not mode:
		return False
	row = resolve_payment_method(mode, property)
	if row is not None:
		return bool(int(row.get("system") or 0))
	return mode in SYSTEM_TENDER_TYPES


def assert_known_payment_method(property: str | None, mode: str | None) -> None:
	"""Ensure `mode` (stored CODE text) exists on Payment Method for `property`
	(or as a shared row). Disabled rows remain accepted so historical records
	keep saving; unknown or another property's codes are refused."""
	if not mode or not _has_db():
		return
	try:
		if not frappe.db.table_exists(PAYMENT):
			return
	except Exception:
		return
	if resolve_payment_method(mode, property) is None:
		frappe.throw(_("Unknown payment method {0}.").format(mode))


class MasterCode(Document):
	"""Shared rules for every code master."""

	def validate(self):
		self.code = (self.code or "").strip()
		if not self.code:
			frappe.throw(_("Give the code."))
		self.description = (self.description or "").strip() or self.code
		if self.is_new():
			# only the seeder marks a code as one the PMS relies on
			if not self.flags.get("kamra_seeding"):
				self.system = 0
			return
		before = self.get_doc_before_save()
		if before and int(before.system or 0) != int(self.system or 0):
			self.system = before.system
		if self.system and self.disabled:
			frappe.throw(_("{0} {1} is used by the system and can't be switched off. "
			               "You can change its description.").format(_(self.doctype), self.name))

	def on_trash(self):
		if self.system:
			frappe.throw(_("{0} {1} is used by the system and can't be deleted.")
			             .format(_(self.doctype), self.name))
