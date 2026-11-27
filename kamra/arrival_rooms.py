"""Preserve legacy in-house inventory before its physical membership changes."""
import frappe

from kamra.kamra.doctype.reservation.reservation import (
	entire_place_rooms, lock_property_for_inventory, uses_member_rooms,
)


def backfill_property(property_name, *, previous_unit=None, villas_only=False):
	lock_property_for_inventory(property_name)
	filters = {"property": property_name, "status": "Checked In"}
	if previous_unit:
		filters["sellable_unit"] = previous_unit.name
	for stay in frappe.db.get_values("Reservation", filters, ["name", "room", "room_type", "sellable_unit"],
	                                 as_dict=True, for_update=True):
		if frappe.db.get_value("Reservation Occupied Room", {"parent": stay.name}, for_update=True):
			continue
		if previous_unit:
			members = [r.room for r in previous_unit.physical_rooms]
		elif not villas_only and stay.sellable_unit and frappe.db.get_value(
			"Sellable Unit", stay.sellable_unit, "unit_kind", for_update=True) == "composite":
			members = frappe.db.get_values("Sellable Unit Room", {"parent": stay.sellable_unit},
			                               "room", pluck=True, for_update=True)
		elif not stay.room and uses_member_rooms(property_name, stay.room_type, for_update=True):
			members = [r.name for r in entire_place_rooms(
				property_name, stay.room_type, include_out_of_order=True, for_update=True)]
		else:
			continue
		occupied = frappe.db.get_values("Room", {"property": property_name, "name": ("in", members),
			"occupancy_status": "Occupied"}, "name", order_by="name", pluck=True, for_update=True)
		for idx, room in enumerate(occupied, 1):
			frappe.get_doc({"doctype": "Reservation Occupied Room", "parent": stay.name,
				"parenttype": "Reservation", "parentfield": "occupied_members", "idx": idx, "room": room}).db_insert()
