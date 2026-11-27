"""Repair stays with untyped Deposit receipts, including sites that already ran the R8 repair."""
import frappe


def execute():
	from kamra.folio import reconcile_reservation_advance

	for row in frappe.db.sql(
		"""SELECT DISTINCT r.name, r.property
		   FROM `tabFolio Ledger Entry` e
		   JOIN `tabFolio Payment` p ON p.name = e.source_name AND p.parent = e.folio
		   JOIN `tabFolio` f ON f.name = p.parent
		   JOIN `tabReservation` r ON r.name = f.reservation
		   WHERE e.source_doctype = 'Folio Payment' AND e.ledger = 'Deposit' AND e.credit > 0
		     AND p.parenttype = 'Folio' AND COALESCE(p.payment_kind, '') IN ('', 'Payment')
		     AND p.amount > 0 AND f.folio_type != 'Group'
		   ORDER BY r.name""",
		as_dict=True,
	):
		reconcile_reservation_advance(row.name, row.property)
