"""Guest-facing names for property-scoped inventory and board plans."""


def room_type_label(name, property=None):
	return _display_name("Room Type", name, "room_type_name", property)


def meal_plan_label(name, property=None):
	import frappe

	if not name:
		return ""
	field = "meal_plan_name" if frappe.get_meta("Meal Plan").has_field("meal_plan_name") else "label"
	return _display_name("Meal Plan", name, field, property)


def _display_name(doctype, name, field, property):
	import frappe

	if not name:
		return ""
	return frappe.db.get_value(doctype, name, field, cache=True) or (
		name.removeprefix(f"{property}-") if property else name)
