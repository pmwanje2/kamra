"""Rename the two company billing presets that promised a liquor rule.

"Room and F&B (liquor to guest)" is now "Room and F&B" and "Everything except
liquor" is "Everything". Liquor is Food & Beverage like the food (OPERA routes
only what is included, by transaction code): a company that does not pay for
liquor names the codes it does pay in its billing rules. Only the stored
labels change; a second run changes nothing."""

import frappe

RENAMED = {
	"Room and F&B (liquor to guest)": "Room and F&B",
	"Everything except liquor": "Everything",
}


def execute():
	for old, new in RENAMED.items():
		frappe.db.sql("UPDATE `tabProperty` SET default_company_billing = %s "
		              "WHERE default_company_billing = %s", (new, old))
		frappe.db.sql("UPDATE `tabGroup Booking` SET master_pays = %s "
		              "WHERE master_pays = %s", (new, old))
