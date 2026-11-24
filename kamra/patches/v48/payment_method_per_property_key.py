"""Migrate Payment Method rows from PR #44's global `field:code` key to per-property keys.

On bench/dev sites that already ran PR #44:
1. MariaDB keeps the old UNIQUE index on `tabPayment Method.code` after removing
   `"unique": 1` from the DocType JSON. Drop any non-PRIMARY unique index on
   `code` so multiple properties can define the same code independently.
2. Property-scoped Payment Method rows created under #44 have `name == code`
   while `property` is set. Re-key them to `<property>::<code>` in-place
   without touching stored `Folio Payment.mode`, `Cashier Transaction.mode`, or
   `Banquet Receipt.mode` (which intentionally store the CODE text).

Shared rows (`property` blank, including all seeded system rows) keep
`name == code`. Safe to run multiple times (idempotent).
"""

from __future__ import annotations

import frappe

from kamra.kamra.doctype.payment_method.payment_method import payment_method_name


def _drop_code_unique_indexes() -> None:
	rows = frappe.db.sql(
		"""SHOW INDEX FROM `tabPayment Method`
		   WHERE Column_name = 'code' AND Non_unique = 0 AND Key_name != 'PRIMARY'""",
		as_dict=True,
	)
	seen: set[str] = set()
	for row in rows:
		key_name = row.get("Key_name")
		if not key_name or key_name in seen:
			continue
		seen.add(key_name)
		safe_key = key_name.replace("`", "``")
		frappe.db.sql_ddl(  # nosemgrep: frappe-sql-format-injection -- index name comes from SHOW INDEX metadata on tabPayment Method, not user input
			f"ALTER TABLE `tabPayment Method` DROP INDEX `{safe_key}`"
		)


def _rekey_property_rows() -> None:
	if not frappe.db.has_column("Payment Method", "property"):
		return
	rows = frappe.db.sql(
		"""SELECT name, code, property FROM `tabPayment Method`
		   WHERE COALESCE(property, '') != ''""",
		as_dict=True,
	)
	for row in rows:
		code = (row.get("code") or row.get("name") or "").strip()
		prop = (row.get("property") or "").strip()
		if not code or not prop:
			continue
		target = payment_method_name(code, prop)
		if row.name == target:
			continue
		if frappe.db.exists("Payment Method", target):
			continue
		frappe.db.sql(
			"UPDATE `tabPayment Method` SET name = %s WHERE name = %s",
			(target, row.name),
		)
		if frappe.db.table_exists("Version"):
			frappe.db.sql(
				"UPDATE `tabVersion` SET docname = %s WHERE ref_doctype = 'Payment Method' AND docname = %s",
				(target, row.name),
			)
		if frappe.db.table_exists("Comment"):
			frappe.db.sql(
				"UPDATE `tabComment` SET reference_name = %s WHERE reference_doctype = 'Payment Method' AND reference_name = %s",
				(target, row.name),
			)


def execute() -> None:
	if not frappe.db.table_exists("Payment Method"):
		return
	_drop_code_unique_indexes()
	_rekey_property_rows()
	frappe.clear_cache(doctype="Payment Method")
