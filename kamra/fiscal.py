"""The fiscal document behind a settled folio, under the ERPNext accounting bridge.

OPERA Cloud prints a fiscal folio with the statutory identifiers its fiscal
service returned and reprints it from Folio History. Under the accounting
bridge the folio's tax invoice is the ERPNext Sales Invoice (ADR-0040: ERPNext
owns tax, GL, AR and the fiscal service), so this app's own folio number is an
internal reference only. In Uganda the URA EFRIS Fiscal Document Number (FDN),
verification code and QR code are part of that invoice.

The bridge answers through its `hospitality_kamra.fiscal_document` reader.
Without it this app issues its own invoice, as it always has.
"""

from __future__ import annotations

FISCAL_DOCUMENT = "hospitality_kamra.fiscal_document"
# what the reader may answer; anything else is treated as unknown
STATES = ("fiscalised", "pending", "not_efris")
FINAL_STATES = ("fiscalised", "not_efris")


def fiscal_bridge() -> bool:
	"""True when ERPNext issues this site's fiscal documents (the bridge's reader is installed)."""
	from kamra.erp_bridge import get_bridge_script

	return get_bridge_script(FISCAL_DOCUMENT) is not None


def _text(value) -> str | None:
	return value if isinstance(value, str) and value else None


def _opt_bool(value) -> bool | None:
	if value is None or isinstance(value, str):
		return None
	if isinstance(value, (bool, int, float)):
		return bool(int(value))
	return None


def _opt_float(value) -> float | None:
	if value is None:
		return None
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def _unverified() -> dict:
	return {
		"state": "unverified",
		"sales_invoice": None,
		"fdn": None,
		"verification_code": None,
		"qr": None,
		"issued": None,
		"sandbox": False,
		"efris_status": None,
		"tax_template": None,
		"charges_tax": None,
		"efris_enrolled": None,
		"outstanding_amount": None,
		"credit_notes": [],
		"replaced_by": None,
	}


def fiscal_status(folio) -> dict | None:
	"""The fiscal document of a closed folio, or None where there is none to read:
	an open folio (a provisional bill) or a site without the bridge."""
	if folio.status != "Closed":
		return None
	import frappe

	from kamra.erp_bridge import bridge_call

	try:
		answer = bridge_call(FISCAL_DOCUMENT, folio=folio.name)
	except Exception:
		frappe.log_error(title="fiscal status read failed")
		return _unverified()
	if answer is None:
		return None
	if not isinstance(answer, dict):
		return _unverified()
	state = answer.get("state")
	sales_invoice = _text(answer.get("sales_invoice"))
	charges_tax = _opt_bool(answer.get("charges_tax"))
	efris_enrolled = _opt_bool(answer.get("efris_enrolled"))
	notes = answer.get("credit_notes")
	out = {
		"state": state,
		"sales_invoice": sales_invoice,
		"fdn": None,
		"verification_code": None,
		"qr": None,
		"issued": None,
		"sandbox": bool(answer.get("sandbox")),
		"efris_status": _text(answer.get("efris_status")),
		"tax_template": _text(answer.get("tax_template")),
		"charges_tax": charges_tax,
		"efris_enrolled": efris_enrolled,
		"outstanding_amount": _opt_float(answer.get("outstanding_amount")),
		"credit_notes": list(notes) if isinstance(notes, list) else [],
		"replaced_by": _text(answer.get("replaced_by")),
	}
	if state == "fiscalised":
		identifiers = {key: _text(answer.get(key)) for key in ("fdn", "verification_code", "qr", "issued")}
		if not (sales_invoice and identifiers["fdn"] and identifiers["verification_code"]
		        and charges_tax is not None and efris_enrolled is not None):
			state = None
		out.update(identifiers)
	if state == "not_efris":
		if not (sales_invoice and charges_tax is not None and efris_enrolled is not None):
			state = None
		elif efris_enrolled:
			# PR-9: not_efris is never final when the company is enrolled in EFRIS
			state = "pending"
			out["state"] = "pending"
	if state not in STATES:
		# fail closed: a fiscal state this app does not know is never printed as final
		out.update(state="unverified", fdn=None, verification_code=None, qr=None, issued=None)
	return out


def visible_fiscal_references(fiscal: dict, credit: dict) -> dict:
	"""Expose bridge references only when the folio's accounting projection verifies them."""
	invoice = credit.get("sales_invoice")
	visible_invoice = invoice if invoice == fiscal["sales_invoice"] else None
	allowed = set(credit.get("credit_notes", [])) if visible_invoice else set()
	notes = []
	for note in fiscal["credit_notes"]:
		name = note.get("name") if isinstance(note, dict) else note
		if isinstance(name, str) and name in allowed:
			notes.append({key: note[key] for key in ("name", "grand_total", "efris_irn") if key in note}
			             if isinstance(note, dict) else name)
	return {**fiscal, "sales_invoice": visible_invoice, "credit_notes": notes,
	        "replaced_by": invoice if invoice == fiscal["replaced_by"] else None}


def is_final(fiscal: dict | None) -> bool:
	"""Whether the fiscal document can be handed over as the tax invoice."""
	if not fiscal or fiscal.get("sandbox"):
		return False
	state = fiscal.get("state")
	if state == "fiscalised":
		return bool(fiscal.get("sales_invoice") and fiscal.get("fdn")
		            and fiscal.get("verification_code")
		            and fiscal.get("charges_tax") is not None
		            and fiscal.get("efris_enrolled") is not None)
	if state == "not_efris":
		return bool(fiscal.get("sales_invoice")
		            and fiscal.get("efris_enrolled") is False
		            and fiscal.get("charges_tax") is not None)
	return False


def fiscal_title(fiscal: dict, *, tax_charged: bool | None = None) -> str:
	"""Document title under the accounting bridge (PR-9 §2.8)."""
	if fiscal.get("state") == "unverified":
		return "Fiscal status unavailable"
	if fiscal.get("sandbox"):
		return "Test fiscal number - not a valid tax invoice"
	if not is_final(fiscal):
		return "Fiscal document pending"
	if fiscal.get("state") == "fiscalised":
		return "Tax Invoice"
	charged = fiscal.get("charges_tax") if tax_charged is None else bool(tax_charged)
	return "Tax Invoice" if charged else "Invoice"


def assert_reprintable(fiscal: dict | None) -> None:
	"""Refuse reprinting a bridged fiscal document that is not final."""
	if not fiscal or is_final(fiscal):
		return
	import frappe
	from frappe import _

	if fiscal.get("state") == "unverified":
		frappe.throw(_("The fiscal status is unavailable, so this can't be reprinted as a tax invoice. Try again, or ask Finance."))
	if fiscal.get("sandbox"):
		frappe.throw(_("This fiscal number comes from a test fiscal service and is not a valid tax invoice."))
	frappe.throw(_("The fiscal document is still pending, so there is nothing final to reprint yet."))
