from __future__ import annotations

import frappe


def execute():
	"""Ensure composite lookup index exists on Rate Restriction."""
	if not frappe.db.exists("DocType", "Rate Restriction"):
		return
	frappe.db.add_index(
		"Rate Restriction",
		["property", "restriction_date", "room_type", "rate_plan"],
		index_name="idx_rate_restriction_scope",
	)
