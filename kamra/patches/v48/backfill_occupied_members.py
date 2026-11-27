import frappe

from kamra.arrival_rooms import backfill_property


def execute():
	for prop in sorted(set(frappe.get_all("Reservation", filters={"status": "Checked In"}, pluck="property"))):
		backfill_property(prop)
