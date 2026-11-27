"""Compatibility endpoints for the retired menu spreadsheet importer."""

import frappe

from kamra.authz import require_roles

MENU_ROLES = ("Finance", "F&B Manager")


def _retired():
	raise frappe.ValidationError(frappe._("Manage menu imports in the point of sale."))


@frappe.whitelist(methods=["POST"])
@require_roles(*MENU_ROLES)
def preview_menu_import(property: str, csv_text: str,
                        outlet: str | None = None):
	"""Retired. This endpoint raises ValidationError. Manage menus in the point of sale."""
	_retired()


@frappe.whitelist(methods=["POST"])
@require_roles(*MENU_ROLES)
def run_menu_import(property: str, csv_text: str, outlet: str | None = None,
                    update_existing: int = 1):
	"""Retired. This endpoint raises ValidationError. Manage menus in the point of sale."""
	_retired()


@frappe.whitelist()
@require_roles(*MENU_ROLES)
def menu_template():
	"""Retired. This endpoint raises ValidationError. Manage menus in the point of sale."""
	_retired()
