"""Give every folio from before billing windows its window and payee.

A stay's Guest folios are window 1. Its Extra and Company folios are
numbered from 2 in the order they were opened, and a group's master bills
from 1. The payee comes from the old kind: a Company folio bills the stay's
company and a group master the group's company (direct bill when the company
has credit); every other folio, a named second guest's included, bills the
guest. Account invoices have no window. Only folios without a window are
touched, so a second run changes nothing."""

import frappe

DIRECT_BILL = "Company Credit"


def execute():
	rows = frappe.get_all(
		"Folio", filters={"folio_type": ("!=", "Account")},
		fields=["name", "folio_type", "reservation", "group_booking", "window_no"],
		order_by="creation asc, name asc")
	taken: dict[tuple, set] = {}
	for r in rows:
		if r.window_no:
			taken.setdefault(_key(r), set()).add(int(r.window_no))
	for r in rows:
		if r.window_no or not (r.reservation or r.group_booking):
			continue
		key = _key(r)
		used = taken.setdefault(key, set())
		if r.folio_type == "Guest":
			window = 1
		else:
			window = 1 if r.folio_type == "Group" else 2
			while window in used:
				window += 1
		used.add(window)
		company = None
		if r.folio_type == "Company" and r.reservation:
			company = frappe.db.get_value("Reservation", r.reservation, "company")
		elif r.folio_type == "Group" and r.group_booking:
			company = frappe.db.get_value("Group Booking", r.group_booking, "company")
		credit = company and frappe.db.get_value("Corporate Account", company,
		                                         "credit_allowed")
		frappe.db.set_value("Folio", r.name, {
			"window_no": window,
			"payee_type": "Company" if company else "Guest",
			"payee_company": company or None,
			"payment_method": DIRECT_BILL if credit else None,
		}, update_modified=False)


def _key(row) -> tuple:
	if row.folio_type == "Group":
		return ("group", row.group_booking)
	return ("stay", row.reservation)
