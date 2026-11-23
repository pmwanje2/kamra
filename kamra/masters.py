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
	("Folio Payment", "mode", PAYMENT),
	("Cashier Transaction", "mode", PAYMENT),
	("Banquet Receipt", "mode", PAYMENT),
	("Meal Plan", "code", MEAL_PLAN_CODE),
)


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
			if not frappe.db.exists(doctype, row["code"]):
				_insert(doctype, {**row, "sort_order": i * 10})


def adopt_values_in_use() -> int:
	"""A master row for every value stored in a linked field, so data an
	older release wrote (or a value the old Select never listed) still
	saves. Adopted rows are the hotel's codes, never system codes."""
	added = 0
	for doctype, fieldname, master in LINKED_FIELDS:
		if not (frappe.db.table_exists(doctype) and frappe.db.table_exists(master)
		        and frappe.db.has_column(doctype, fieldname)):
			continue
		values = frappe.get_all(doctype, filters={fieldname: ("is", "set")},
		                        fields=[fieldname], distinct=True, pluck=fieldname)
		for value in values:
			if frappe.db.exists(master, value):
				continue
			row = {"code": value, "description": value, "system": 0}
			if master == PAYMENT:
				row["tender_type"] = "Other"
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
		return frappe.get_all(doctype, filters=filters, fields=fields)
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
		"booker_relations": choices(RELATION),
		"cancellation_reasons": choices(CANCELLATION),
	}


def client_payment_methods(tender_type: str | None = None, *, enabled_only: bool = True) -> list[str]:
	"""Payment methods the hotel added itself (not system codes). System
	methods keep the country pack's rules (kamra.localization); a hotel's
	own method is offered everywhere and behaves as its tender type."""
	filters = {"system": 0}
	if enabled_only:
		filters["disabled"] = 0
	if tender_type:
		filters["tender_type"] = tender_type
	return [r.name for r in _sorted(_offered_rows(
		PAYMENT, filters, ["name", "sort_order", "creation"]))]


def client_tender_types() -> dict[str, str]:
	"""{method: tender type} for the hotel's own methods, switched-off ones
	included, so a till that took one still counts it in the right bucket."""
	return {r.name: r.tender_type for r in _offered_rows(
		PAYMENT, {"system": 0}, ["name", "tender_type"])}


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
