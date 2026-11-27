"""Restore advance allocations lost when a legacy Deposit receipt was reclassified."""
import frappe

from kamra.folio import reconcile_reservation_advance


def execute():
	stays = frappe.db.sql(
		"""SELECT DISTINCT f.reservation, f.property
		   FROM `tabFolio Payment` repost
		   JOIN `tabFolio Payment` original ON original.name = repost.repost_of AND original.parent = repost.parent
		   JOIN `tabFolio` f ON f.name = original.parent
		   JOIN `tabFolio Ledger Entry` e ON e.folio = f.name AND e.source_name = original.name
		   WHERE original.parenttype = 'Folio' AND repost.parenttype = 'Folio'
		     AND COALESCE(original.payment_kind, '') IN ('', 'Payment') AND original.amount > 0
		     AND e.source_doctype = 'Folio Payment' AND e.ledger = 'Deposit' AND e.credit > 0
		     AND f.folio_type != 'Group' AND COALESCE(f.reservation, '') != ''
		   ORDER BY f.reservation""",
		as_dict=True,
	)
	for stay in stays:
		reconcile_reservation_advance(stay.reservation, stay.property)
