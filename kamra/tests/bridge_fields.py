"""The ERPNext bridge's own Custom Fields, for tests of what Kamra does with
them. The bridge (hospitality-kamra-bridge) adds them on a live site; Kamra
reads them and never defines them. Creating a field is DDL, which commits,
so call this from setUpClass, never inside a test.

Folio.sales_invoice is a Link to Sales Invoice on a bridge site. A test site
has no ERPNext, so it is Data here: Kamra only reads whether it is set."""

from __future__ import annotations

import frappe

BRIDGE_FIELDS = {
	("Folio Charge", "source_ref"): {"label": "Outlet Source Reference", "unique": 1},
	("Folio", "sales_invoice"): {"label": "Sales Invoice", "unique": 1},
	("Property", "company"): {"label": "Company"},
}


def ensure_bridge_field(doctype: str, fieldname: str) -> None:
	if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname}):
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_field
	create_custom_field(doctype, {
		"fieldname": fieldname, "fieldtype": "Data", "read_only": 1, "no_copy": 1,
		**BRIDGE_FIELDS[(doctype, fieldname)],
	})
	frappe.clear_cache(doctype=doctype)
