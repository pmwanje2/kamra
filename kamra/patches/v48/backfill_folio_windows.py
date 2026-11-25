"""Give every folio from before billing windows its window and payee.

OPERA keeps a stay's open bills on windows 1 to 8; a settled bill is folio
history and holds no window. So only open folios take a window: a stay's
oldest open Guest folio is window 1, its other open folios (a second Guest
folio included) the next free windows from 2 in the order they were opened,
and a group's open master bills from 1. A closed folio keeps no window (0).
No folio passes the property's `folio_windows` cap: an open folio past it
keeps no window and still takes charges moved to it by hand. Windows already
held, by any folio, are left alone.

The payee comes from the old kind, open or closed: a Company folio bills the
stay's company and a group master the group's company (direct bill when the
company has credit); every other folio, a named second guest's included,
bills the guest. Account invoices have no window. Only folios without a
window are touched, so a second run changes nothing."""

import frappe

DIRECT_BILL = "Company Credit"


def execute():
	from kamra.folio import window_cap
	rows = frappe.get_all(
		"Folio", filters={"folio_type": ("!=", "Account")},
		fields=["name", "folio_type", "reservation", "group_booking", "window_no",
		        "status", "property", "payee_type"],
		order_by="creation asc, name asc")
	taken: dict[tuple, set] = {}
	for r in rows:
		if r.window_no:
			taken.setdefault(_key(r), set()).add(int(r.window_no))
	legacy = [r for r in rows if not r.window_no and (r.reservation or r.group_booking)]
	caps: dict[str, int] = {}
	# the oldest open Guest folio of a stay is window 1: number those first
	legacy.sort(key=lambda r: 0 if r.folio_type == "Guest" else 1)
	for r in legacy:
		window = 0
		if r.status != "Closed":
			cap = caps.setdefault(r.property, window_cap(r.property))
			used = taken.setdefault(_key(r), set())
			first = 1 if r.folio_type in ("Guest", "Group") else 2
			window = next((n for n in range(first, cap + 1) if n not in used and
			               (n > 1 or r.folio_type in ("Guest", "Group"))), 0)
			if window:
				used.add(window)
		values = {"window_no": window}
		if not r.payee_type:
			values.update(_payee(r))
		frappe.db.set_value("Folio", r.name, values, update_modified=False)


def _payee(row) -> dict:
	company = None
	if row.folio_type == "Company" and row.reservation:
		company = frappe.db.get_value("Reservation", row.reservation, "company")
	elif row.folio_type == "Group" and row.group_booking:
		company = frappe.db.get_value("Group Booking", row.group_booking, "company")
	credit = company and frappe.db.get_value("Corporate Account", company, "credit_allowed")
	return {"payee_type": "Company" if company else "Guest",
	        "payee_company": company or None,
	        "payment_method": DIRECT_BILL if credit else None}


def _key(row) -> tuple:
	if row.folio_type == "Group":
		return ("group", row.group_booking)
	return ("stay", row.reservation)
