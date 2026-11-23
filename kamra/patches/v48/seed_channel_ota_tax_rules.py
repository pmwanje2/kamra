"""Give each Channex connection made before OTA tax rules existed the
standard rules, the ones a new connection starts from. A connection that
has rules of its own keeps them. Safe to rerun."""

import frappe


def execute():
	frappe.reload_doc("kamra", "doctype", "channel_ota_tax_rule")
	frappe.reload_doc("kamra", "doctype", "channel_ota_tax_basis")
	frappe.reload_doc("kamra", "doctype", "channel_manager_connection")
	from kamra.kamra.doctype.channel_ota_tax_rule.channel_ota_tax_rule import standard_rules

	for name in frappe.get_all("Channel Manager Connection", filters={"provider": "Channex"}, pluck="name"):
		conn = frappe.get_doc("Channel Manager Connection", name)
		if conn.get("ota_tax_rules"):
			continue
		for row in standard_rules():
			conn.append("ota_tax_rules", row).db_insert()
