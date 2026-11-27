"""Native ownership checks shared by RPCs, document writes and workers."""
from functools import wraps

MCP_DISABLED_CODE = "KMR-OFF-001"


def mcp_disabled(config=None) -> bool:
	if config is None:
		import frappe
		config = frappe.conf
	return bool(config.get("kamra_mcp_disabled"))


def require_local_assistant():
	if mcp_disabled():
		import frappe
		frappe.throw(MCP_DISABLED_CODE)


def disabled_delivery():
	return {"sent": False, "reason": "disabled", "code": MCP_DISABLED_CODE}


def require_local_pos():
	from kamra.erp_books import erp_pos_url

	if erp_pos_url():
		import frappe
		frappe.throw(frappe._("Manage this in the point of sale."), frappe.PermissionError)


def require_local_fiscal():
	from kamra.fiscal import fiscal_bridge

	if fiscal_bridge():
		refuse_external_fiscal()


def refuse_external_fiscal():
	import frappe
	frappe.throw(frappe._("This is kept in the accounting system."), frappe.PermissionError)


def require_local_tax():
	from kamra.erp_bridge import erp_determines_tax

	if erp_determines_tax():
		refuse_external_fiscal()
	require_local_fiscal()


def local_only(check):
	"""Keep the public signature and role decorator on the registered callable."""
	def decorate(fn):
		@wraps(fn)
		def checked(*args, **kwargs):
			check()
			return fn(*args, **kwargs)
		return checked
	return decorate


ASSISTANT_DOCUMENTS = ("AI Assistant Settings", "Channel Provider Connection", "Copilot Conversation", "WhatsApp Message", "MCP OAuth Client", "MCP OAuth Grant")
POS_DOCUMENTS = (
	"POS Order", "POS Outlet", "POS Table Reservation", "Menu Item", "Ingredient",
	"Ingredient Stock", "Ingredient Ledger Entry", "Kitchen Printer", "Print Job",
)
FISCAL_DOCUMENTS = ("ZATCA Invoice", "ZATCA Settings", "Discount Voucher")
PAYABLE_DOCUMENTS = ("Vendor", "Vendor Bill", "Vendor Bill Payment")


def guard_document(doc, method=None):
	if doc.doctype in ASSISTANT_DOCUMENTS:
		require_local_assistant()
	elif doc.doctype in POS_DOCUMENTS:
		require_local_pos()
	elif doc.doctype in FISCAL_DOCUMENTS:
		require_local_fiscal()
	elif doc.doctype in PAYABLE_DOCUMENTS:
		from kamra.erp_books import erp_screens, refuse_kept_in_erp

		target = erp_screens().get("payables")
		if target:
			refuse_kept_in_erp(target)


def local_document_available(doctype):
	"""Mirror ownership on direct Desk lists and individual resource requests."""
	if doctype in ASSISTANT_DOCUMENTS:
		return not mcp_disabled()
	if doctype in POS_DOCUMENTS:
		from kamra.erp_books import erp_pos_url
		return not erp_pos_url()
	if doctype in FISCAL_DOCUMENTS:
		from kamra.fiscal import fiscal_bridge
		return not fiscal_bridge()
	return True
