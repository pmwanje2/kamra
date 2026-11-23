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


def fiscal_status(folio) -> dict | None:
	"""The fiscal document of a closed folio, or None where there is none to read:
	an open folio (a provisional bill) or a site without the bridge."""
	if folio.status != "Closed":
		return None
	from kamra.erp_bridge import bridge_call

	answer = bridge_call(FISCAL_DOCUMENT, folio=folio.name)
	if answer is None:
		return None
	state = answer.get("state")
	sales_invoice = _text(answer.get("sales_invoice"))
	out = {
		"state": state,
		"sales_invoice": sales_invoice,
		"fdn": None,
		"verification_code": None,
		"qr": None,
		"issued": None,
		"sandbox": bool(answer.get("sandbox")),
		"efris_status": _text(answer.get("efris_status")),
	}
	if state == "fiscalised":
		identifiers = {key: _text(answer.get(key)) for key in ("fdn", "verification_code", "qr", "issued")}
		if not (sales_invoice and identifiers["fdn"] and identifiers["verification_code"]):
			state = None
		out.update(identifiers)
	if state == "not_efris" and not sales_invoice:
		state = None
	if state not in STATES:
		# fail closed: a fiscal state this app does not know is never printed as final
		out.update(state="unverified", fdn=None, verification_code=None, qr=None, issued=None)
	return out


def is_final(fiscal: dict | None) -> bool:
	"""Whether the fiscal document can be handed over as the tax invoice."""
	return bool(fiscal and fiscal["state"] in FINAL_STATES and not fiscal["sandbox"])
