"""Internal transport for optional, enabled Frappe API Server Scripts.

Callers own endpoint authorization; this module does not expose an RPC.
"""

from types import NoneType

import frappe


def get_bridge_script(api_method: str):
	name = frappe.db.get_value(
		"Server Script", {"script_type": "API", "api_method": api_method, "disabled": 0}
	)
	return frappe.get_doc("Server Script", name) if name else None


def run_bridge_script(script, form: dict):
	previous_form = frappe.local.form_dict
	absent = object()
	previous_message = frappe.response.pop("message", absent)
	try:
		frappe.local.form_dict = form if getattr(type(form), "_kamra_pin_form", False) else frappe._dict(form)
		script.execute_method()
		return frappe.response.get("message")
	finally:
		frappe.local.form_dict = previous_form
		frappe.response.pop("message", None)
		if previous_message is not absent:
			frappe.response["message"] = previous_message


_REAL_RUN_BRIDGE_SCRIPT = run_bridge_script


def call_bridge_script(script, form: dict, response_types=(dict, NoneType)) -> dict | list | None:
	"""Run a bridge script already looked up, refusing an unexpected answer type."""
	result = run_bridge_script(script, form)
	if not isinstance(result, response_types):
		raise TypeError("Unexpected integration response.")
	return result


def erp_determines_tax() -> bool:
	"""True when the ERP tax-basis bridge script is installed and enabled."""
	return get_bridge_script("hospitality_kamra.tax_basis") is not None


def is_bridge_active() -> bool:
	"""True when an ERP hospitality bridge script is installed and enabled."""
	for method in ("hospitality_kamra.tax_basis", "hospitality_kamra.tender_modes"):
		if get_bridge_script(method) is not None:
			return True
	return False


def bridge_call(api_method: str, *, response_types=(dict, NoneType), **form) -> dict | list | None:
	"""Return an optional bridge's result; never substitute the outer response."""
	script = get_bridge_script(api_method)
	if script is None:
		return None
	if run_bridge_script is _REAL_RUN_BRIDGE_SCRIPT and not callable(getattr(script, "execute_method", None)):
		return None
	return call_bridge_script(script, form, response_types)
