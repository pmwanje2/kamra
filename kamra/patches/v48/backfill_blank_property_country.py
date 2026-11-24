"""Preserve explicit India country on blank-country properties backed by an India Company.

Removing the hardcoded `"India"` fallback from `Property.country` means a blank
country now resolves from Company -> System Settings -> neutral. Existing
Indian properties where today's fallback was in effect and the Company country
is India receive `country = "India"` explicitly. Safe to run twice (idempotent).
"""

from __future__ import annotations

import frappe

from kamra.localization import company_country_for


def execute() -> None:
	if not (frappe.db.table_exists("Property") and frappe.db.has_column("Property", "country")):
		return
	has_company = frappe.db.has_column("Property", "company")
	fields = ["name", "country", *(["company"] if has_company else [])]
	props = frappe.get_all("Property", fields=fields)
	for prop in props:
		if (prop.get("country") or "").strip():
			continue
		if company_country_for(property=prop.name, prop_doc=prop) == "India":
			frappe.db.set_value("Property", prop.name, "country", "India", update_modified=False)
			frappe.clear_document_cache("Property", prop.name)
