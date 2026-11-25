"""PR 3 (P13/P29/P41) post-model-sync patch: seed missing default package & fixed-charge Transaction Codes."""

from __future__ import annotations

import frappe

DEFAULT_CODES = (
	("1400", "Package Allowance", "Package", "Package", "Allowance"),
	("1410", "Package Breakage", "Package", "Package", "Misc"),
	("3300", "Parking", "Revenue", "Other", "Misc"),
	("3400", "Resort Fee", "Revenue", "Other", "Misc"),
)


def execute():
	from kamra.ledger import ensure_default_transaction_codes
	ensure_default_transaction_codes()
	for code, desc, txn_type, group, charge_type in DEFAULT_CODES:
		if frappe.db.exists("Transaction Code", code):
			continue
		frappe.get_doc({
			"doctype": "Transaction Code",
			"code": code,
			"description": desc,
			"txn_type": txn_type,
			"revenue_group": group,
			"subgroup": desc,
			"charge_type": charge_type,
			"is_alcohol": 0,
			"disabled": 0,
		}).insert(ignore_permissions=True)

	if frappe.db.exists("DocType", "Holiday Package") and frappe.get_meta("Holiday Package").has_field("folio_display_mode"):
		frappe.db.sql(
			"""UPDATE `tabHoliday Package`
			   SET folio_display_mode = 'Inclusive'
			   WHERE IFNULL(folio_display_mode, '') = ''"""
		)
