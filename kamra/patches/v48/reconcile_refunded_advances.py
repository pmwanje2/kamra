"""Repair stale reservation advances where the folio already records refunds."""
import frappe


def execute():
	from kamra.folio import reconcile_reservation_advance

	for row in frappe.db.sql(
		"""SELECT DISTINCT r.name, r.property
		   FROM `tabReservation` r JOIN `tabFolio` f ON f.reservation = r.name
		   JOIN `tabFolio Payment` p ON p.parent = f.name AND p.parenttype = 'Folio'
		   WHERE r.advance_paid > 0 AND p.payment_kind = 'Refund' AND p.amount < 0
		   ORDER BY r.name""",
		as_dict=True,
	):
		reconcile_reservation_advance(row.name, row.property)
