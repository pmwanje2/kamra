"""Backfill blank Rate Plan.currency from linked tariff Price List.currency.

Older hospitality-tariffs@10 sites linked tariff Rate Plans to foreign-currency
Price Lists via `custom_tariff_price_list` before Rate Plan carried its own
`currency` column. Without this backfill, those plans appear blank in DB and
fall back to the property's currency unless inferred at read time.

- Only fills rows whose `currency` is blank/NULL and whose linked Price List
  has a non-blank `currency`.
- Never overwrites an explicitly set `Rate Plan.currency`.
- Records a native Frappe `Version` entry per backfilled plan and logs the
  updated row count.
- Safe to run more than once (idempotent) and on standalone sites without
  ERPNext / `Price List` / `custom_tariff_price_list`.
"""

from __future__ import annotations

import json

import frappe


def execute() -> int:
	if (
		not frappe.db.table_exists("Rate Plan")
		or not frappe.db.table_exists("Price List")
		or not frappe.db.has_column("Rate Plan", "custom_tariff_price_list")
	):
		return 0

	rows = frappe.db.sql(
		"""
		SELECT rp.name, rp.custom_tariff_price_list AS price_list, pl.currency AS list_currency
		FROM `tabRate Plan` rp
		INNER JOIN `tabPrice List` pl ON pl.name = rp.custom_tariff_price_list
		WHERE IFNULL(rp.currency, '') = ''
		  AND IFNULL(rp.custom_tariff_price_list, '') != ''
		  AND IFNULL(pl.currency, '') != ''
		""",
		as_dict=True,
	)

	has_version = frappe.db.table_exists("Version")
	updated = 0
	for row in rows:
		frappe.db.set_value("Rate Plan", row.name, "currency", row.list_currency, update_modified=False)
		frappe.clear_document_cache("Rate Plan", row.name)
		if has_version:
			frappe.get_doc({
				"doctype": "Version",
				"ref_doctype": "Rate Plan",
				"docname": row.name,
				"data": json.dumps({
					"changed": [["currency", "", row.list_currency]],
					"comment": f"Backfilled from linked Price List {row.price_list}",
				}),
			}).insert(ignore_permissions=True)
		updated += 1

	frappe.logger("kamra").info(
		"backfill_rate_plan_tariff_currency: backfilled %d Rate Plan row(s)",
		updated,
	)
	return updated
