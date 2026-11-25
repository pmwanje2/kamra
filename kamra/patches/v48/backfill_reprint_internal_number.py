"""Populate the desk-visible folio reference without copying legacy statutory identifiers."""
import frappe
from frappe.query_builder.functions import Coalesce, NullIf


def execute():
	log = frappe.qb.DocType("Folio Reprint")
	folio = frappe.qb.DocType("Folio")
	internal = (
		frappe.qb.from_(folio)
		.select(NullIf(folio.invoice_number, ""))
		.where((folio.name == log.folio) & (folio.property == log.property))
	)
	(
		frappe.qb.update(log)
		.set(log.internal_number, Coalesce(internal, log.folio))
		.where(log.internal_number.isnull() | (log.internal_number == ""))
	).run()
