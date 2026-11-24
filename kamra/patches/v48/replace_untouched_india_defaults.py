"""Replace untouched India-era defaults on non-India properties.

Older releases carried Indian defaults in the DocType schemas and settings
registry (slab threshold 7500, cash refund limit 5000, high-balance 50000,
security deposit 5000, locale en-IN, UGX format #,###.##).

This migration replaces ONLY values that still equal those exact old defaults
AND have no Version history showing a client explicitly set that field:
- Money thresholds left to the customer are unset (0 / removed), never derived.
- Indian properties keep every Indian default untouched.
- Any value a client changed (either a different value or recorded in Version)
  is never overwritten.
- Safe to run more than once (idempotent).
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import flt

from kamra.localization import pack_for_country, uses_tax_slab
from kamra.settings import clear_cache


def _version_touched_fields(ref_doctype: str, docname: str) -> set[str]:
	"""Return field names explicitly modified in Version history for one document."""
	touched: set[str] = set()
	if not frappe.db.table_exists("Version"):
		return touched
	rows = frappe.get_all(
		"Version",
		filters={"ref_doctype": ref_doctype, "docname": docname},
		pluck="data",
	)
	for raw in rows:
		try:
			payload = json.loads(raw or "{}")
		except (TypeError, ValueError):
			continue
		for change in payload.get("changed") or ():
			if isinstance(change, (list, tuple)) and change:
				touched.add(str(change[0]))
	return touched


def _kv_number(raw) -> float | None:
	try:
		val = json.loads(raw)
	except (TypeError, ValueError):
		val = raw
	if val in (None, ""):
		return None
	try:
		return float(val)
	except (TypeError, ValueError):
		return None


def _migrate_property(prop: dict) -> None:
	name = prop["name"]
	country = (prop.get("country") or "").strip()
	if not country or country == "India":
		return
	pack = pack_for_country(country)
	if uses_tax_slab(pack):
		return

	touched = _version_touched_fields("Property", name)
	updates: dict = {}

	# 1. Untouched India slab threshold 7500 (and untouched India Slab 5/18/7500 triplet)
	if flt(prop.get("gst_slab_threshold")) == 7500 and "gst_slab_threshold" not in touched:
		updates["gst_slab_threshold"] = 0
		if prop.get("gst_mode") == "Slab" and "gst_mode" not in touched:
			updates["gst_mode"] = ""
		if flt(prop.get("gst_rate_high")) == 18 and "gst_rate_high" not in touched:
			updates["gst_rate_high"] = 0
		if (
			prop.get("gst_mode") == "Slab"
			and flt(prop.get("gst_rate_low")) == 5
			and flt(prop.get("gst_rate_high")) == 18
			and "gst_rate_low" not in touched
		):
			updates["gst_rate_low"] = 0

	# 2. Untouched India security deposit 5000 -> unset (0)
	if flt(prop.get("security_deposit_amount")) == 5000 and "security_deposit_amount" not in touched:
		updates["security_deposit_amount"] = 0

	# 3. Untouched India locale en-IN -> blank (country pack default applies)
	if (prop.get("locale") or "").strip() == "en-IN" and "locale" not in touched:
		updates["locale"] = ""

	if updates:
		frappe.db.set_value("Property", name, updates, update_modified=False)
		frappe.clear_document_cache("Property", name)

	# 4. Untouched Property Setting rows: high-balance 50000 and max_cash_refund 5000 -> unset
	if frappe.db.table_exists("Property Setting"):
		kv_cleared = False
		for key, old_default in (("na_high_balance_threshold", 50000), ("max_cash_refund", 5000)):
			row = frappe.db.get_value(
				"Property Setting",
				{"property": name, "setting_key": key},
				["name", "value"],
				as_dict=True,
			)
			if not row:
				continue
			if _kv_number(row.value) != old_default:
				continue
			if "value" in _version_touched_fields("Property Setting", row.name):
				continue
			frappe.db.delete("Property Setting", {"name": row.name})
			kv_cleared = True
		if kv_cleared:
			clear_cache(name)

	# 5. Untouched Cashier.max_cash_refund 5000 copies on this property -> unset (0)
	if frappe.db.table_exists("Cashier"):
		cashiers = frappe.get_all(
			"Cashier",
			filters={"property": name},
			fields=["name", "max_cash_refund"],
		)
		for csh in cashiers:
			if flt(csh.max_cash_refund) != 5000:
				continue
			if "max_cash_refund" in _version_touched_fields("Cashier", csh.name):
				continue
			frappe.db.set_value("Cashier", csh.name, "max_cash_refund", 0, update_modified=False)


def _migrate_ugx_currency_format() -> None:
	# 6. Untouched UGX number format #,###.## -> #,### (ISO 4217 / SAP TCURX 0 decimals)
	if not frappe.db.exists("Currency", "UGX"):
		return
	fmt = (frappe.db.get_value("Currency", "UGX", "number_format") or "").strip()
	if fmt != "#,###.##":
		return
	if "number_format" in _version_touched_fields("Currency", "UGX"):
		return
	frappe.db.set_value("Currency", "UGX", "number_format", "#,###", update_modified=False)
	frappe.clear_document_cache("Currency", "UGX")


def execute() -> None:
	if frappe.db.table_exists("Property"):
		props = frappe.get_all(
			"Property",
			fields=[
				"name", "country", "gst_mode", "gst_slab_threshold",
				"gst_rate_low", "gst_rate_high", "security_deposit_amount", "locale",
			],
		)
		for prop in props:
			_migrate_property(prop)
	_migrate_ugx_currency_format()
