"""Migrate pre-K1 liquor-excluding company and group rules to explicit
non-alcohol transaction-code rules before presets are renamed.

Before K1, the legacy presets "Room and F&B (liquor to guest)" and
"Everything except liquor" (and explicit Company Billing Rules covering
Food & Beverage / Minibar) kept alcohol off the company/group folio. K1
removes the hidden alcohol-exclusion rule in favour of OPERA-style
inclusion-only transaction-code routing (where beverage/alcohol posts under
its own code, default 2010).

Running this patch strictly BEFORE `rename_company_billing_presets` ensures no
existing company or group sees its effective billing change on upgrade:
1. Companies with explicit charge-type rules covering alcohol-capable types
   (Food & Beverage, Minibar, or any charge type that has an is_alcohol code)
   are expanded into explicit non-alcohol transaction_code rules.
2. Credit-allowed companies with no billing rules of their own that are
   governed by a property still on a liquor-excluding preset receive explicit
   non-alcohol transaction_code rules matching that preset. Companies without
   credit_allowed are left untouched.
3. Non-cancelled Group Bookings with a master_reservation whose master_pays is
   a liquor-excluding preset receive a group-level Routing Instruction (copied
   to active non-opted-out member stays) covering the preset's non-alcohol
   transaction codes, and their master_pays is set to
   "Nothing (guest pays unless rules say so)" so the subsequent preset rename
   never silently pulls alcohol onto the group master.

Idempotent: a second run changes nothing.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint

from kamra.folio import _ROOM_CHARGES, open_group_folio
from kamra.ledger import ensure_default_transaction_codes
from kamra.routing import _overlap, copy_group_instructions

LIQUOR_EXCLUDING_PRESETS: dict[str, frozenset[str] | None] = {
	"Room and F&B (liquor to guest)": _ROOM_CHARGES | {"Food & Beverage"},
	"Everything except liquor": None,
}
NO_MASTER_PRESET = "Nothing (guest pays unless rules say so)"


def _alcohol_charge_types() -> set[str]:
	types = {"Food & Beverage", "Minibar"}
	rows = frappe.get_all(
		"Transaction Code",
		filters={"is_alcohol": 1, "disabled": 0},
		pluck="charge_type",
	)
	for ct in rows:
		if ct:
			types.add(ct)
	return types


def _non_alcohol_codes(charge_types: frozenset[str] | set[str] | None) -> list[str]:
	rows = frappe.get_all(
		"Transaction Code",
		filters={"disabled": 0, "txn_type": ("!=", "Payment")},
		fields=["name", "charge_type", "is_alcohol"],
		order_by="name asc",
	)
	out: list[str] = []
	for r in rows:
		if cint(r.is_alcohol):
			continue
		if not r.charge_type:
			continue
		if charge_types is None:
			if r.charge_type in ("Allowance", "Opening Balance"):
				continue
			out.append(r.name)
		elif r.charge_type in charge_types:
			out.append(r.name)
	return out


def _migrate_explicit_company_rules(alcohol_cts: set[str]) -> None:
	companies = frappe.get_all("Corporate Account", pluck="name")
	for name in companies:
		doc = frappe.get_doc("Corporate Account", name)
		rules = doc.get("billing_rules") or []
		if not rules:
			continue
		needs_expand = any(
			r.pay_by == "Company" and not r.transaction_code and r.charge_type in alcohol_cts
			for r in rules
		)
		if not needs_expand:
			continue

		new_rows: list[dict] = []
		seen_codes: set[tuple[str, str]] = set()
		for r in rules:
			if r.pay_by == "Company" and not r.transaction_code and r.charge_type:
				for code in _non_alcohol_codes({r.charge_type}):
					key = (code, "Company")
					if key not in seen_codes:
						seen_codes.add(key)
						new_rows.append({
							"charge_type": None,
							"transaction_code": code,
							"pay_by": "Company",
						})
			elif r.transaction_code:
				key = (r.transaction_code, r.pay_by)
				if key not in seen_codes:
					seen_codes.add(key)
					new_rows.append({
						"charge_type": None,
						"transaction_code": r.transaction_code,
						"pay_by": r.pay_by,
					})
			elif r.charge_type:
				new_rows.append({
					"charge_type": r.charge_type,
					"transaction_code": None,
					"pay_by": r.pay_by,
				})

		doc.set("billing_rules", new_rows)
		doc.save(ignore_permissions=True)


def _covered_for_presets(presets: list[str]) -> frozenset[str] | None:
	union: set[str] = set()
	for p in presets:
		cov = LIQUOR_EXCLUDING_PRESETS.get(p)
		if cov is None:
			return None
		union.update(cov)
	return frozenset(union)


def _migrate_ruleless_credit_companies() -> None:
	liquor_props = {
		p.name: p.default_company_billing
		for p in frappe.get_all(
			"Property",
			filters={"default_company_billing": ("in", list(LIQUOR_EXCLUDING_PRESETS))},
			fields=["name", "default_company_billing"],
		)
	}
	if not liquor_props:
		return

	credit_companies = frappe.get_all(
		"Corporate Account",
		filters={"credit_allowed": 1},
		pluck="name",
	)
	for name in credit_companies:
		if frappe.db.exists("Company Billing Rule", {"parent": name}):
			continue
		linked_props = set(
			frappe.get_all("Reservation", filters={"company": name}, pluck="property")
		) | set(
			frappe.get_all("Group Booking", filters={"company": name}, pluck="property")
		)
		matched_presets = [
			liquor_props[p] for p in linked_props if p in liquor_props
		]
		if not matched_presets:
			if linked_props:
				# Company only operates at properties whose preset did not exclude liquor
				continue
			matched_presets = list(liquor_props.values())
		covered = _covered_for_presets(matched_presets)
		codes = _non_alcohol_codes(covered)
		if not codes:
			continue
		doc = frappe.get_doc("Corporate Account", name)
		doc.set(
			"billing_rules",
			[{"charge_type": None, "transaction_code": c, "pay_by": "Company"} for c in codes],
		)
		doc.save(ignore_permissions=True)


def _migrate_master_groups() -> None:
	groups = frappe.get_all(
		"Group Booking",
		filters={
			"status": ("!=", "Cancelled"),
			"master_reservation": ("is", "set"),
			"master_pays": ("in", list(LIQUOR_EXCLUDING_PRESETS)),
		},
		fields=["name", "property", "master_pays"],
	)
	for gb in groups:
		covered = LIQUOR_EXCLUDING_PRESETS[gb.master_pays]
		codes = _non_alcohol_codes(covered)
		if not codes:
			continue
		open_group_folio(gb.name)
		candidate = frappe.get_doc({
			"doctype": "Routing Instruction",
			"property": gb.property,
			"group_booking": gb.name,
			"target_type": "Group Master",
			"target_window": 1,
			"all_codes": 0,
			"codes": [{"transaction_code": c} for c in codes],
			"limit_type": "None",
			"monday": 1, "tuesday": 1, "wednesday": 1, "thursday": 1,
			"friday": 1, "saturday": 1, "sunday": 1,
			"notes": "Migrated from pre-K1 liquor-excluding master_pays preset",
		})
		existing = [
			frappe.get_doc("Routing Instruction", n)
			for n in frappe.get_all(
				"Routing Instruction",
				filters={"group_booking": gb.name, "reservation": ("is", "not set"), "disabled": 0},
				pluck="name",
			)
		]
		if not any(_overlap(candidate, other) for other in existing):
			candidate.insert(ignore_permissions=True)

		members = frappe.get_all(
			"Reservation",
			filters={
				"group_booking": gb.name,
				"status": ("not in", ["Cancelled", "No Show", "Checked Out"]),
			},
			fields=["name", "group_booking", "pays_own_charges"],
		)
		for m in members:
			if cint(m.pays_own_charges):
				continue
			try:
				copy_group_instructions(m)
			except frappe.ValidationError:
				# Stay already has a custom overlapping instruction; leave it intact.
				frappe.clear_messages()

		frappe.db.set_value(
			"Group Booking", gb.name, "master_pays", NO_MASTER_PRESET,
			update_modified=False,
		)


def execute():
	ensure_default_transaction_codes()
	alcohol_cts = _alcohol_charge_types()
	_migrate_explicit_company_rules(alcohol_cts)
	_migrate_ruleless_credit_companies()
	_migrate_master_groups()
