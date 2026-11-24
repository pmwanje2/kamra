"""Management books that step aside when the site's ERP keeps the accounts.

Supplier bills (kamra.payables), the cash outlook, the management P&L and
balance sheet (kamra.books), the accounting export (kamra.accounting) and the
company accounts screen (kamra.receivables, the city-ledger payment, dispute
and statement endpoints) keep figures that never post to a general ledger.
When the site's accounts live in an ERP, a second set of books beside them
is a second answer to the same question.

An integration declares which of these screens the ERP keeps, and where.
It does this through an enabled API Server Script at ERP_BOOKS, called
through kamra.erp_bridge, which answers
``{"screens": {screen: {"label": ..., "url": ...}}, "pos_url": "..."}``.
Keys are from SCREENS, and every url is a path on this site. A kept screen's
endpoints refuse (kept_in_erp) and the desk hides it (window.kamra_erp_screens).
A screen the answer leaves out stays. A site with no such script keeps every
screen. A malformed answer raises ValueError.

While the ERP keeps the receivables screen, the company ledger the hotel app
holds never sees an ERP Payment Entry, so nothing may read it as the truth.
Its readers (aging, the owner view, the night audit credit and overdue
checks, credit control) ask receivables_source instead: None while the hotel
app still keeps them (read the City Ledger as before), or the live Customer
receivables from the read-only lookup at ERP_RECEIVABLES. When the ERP keeps
them and answers no lookup there is nothing true to read: a strict reader
refuses, naming the ERP report, and the others read nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import wraps
from types import NoneType

import frappe

ERP_BOOKS = "hospitality_kamra.erp_books"
ERP_RECEIVABLES = "hospitality_kamra.erp_receivables"
RECEIVABLES_UNAVAILABLE_REASON = "Company receivables could not be verified in the accounting system."
SCREENS = ("payables", "cash_forecast", "profit_and_loss", "balance_sheet",
           "accounting_export", "receivables")
# a path on this site: no scheme, no host, no control characters
_SITE_PATH = re.compile(r"/(?!/)[^\s\\]*\Z")


@dataclass(frozen=True)
class ReceivablesSource:
	"""Where a property's company receivables live and what was read.

	- ``in_erp=False``: the hotel app keeps receivables (``rows=None, target=None``).
	- ``in_erp=True, rows=[...]``: the ERP keeps receivables and answered with
	  live Customer receivables (``[]`` means genuinely zero owed).
	- ``in_erp=True, rows=None``: the ERP keeps receivables but answered no
	  lookup; callers must fail closed (never zero, never stale City Ledger).
	"""

	in_erp: bool
	rows: list[dict] | None
	target: dict | None = None

	@property
	def unavailable(self) -> bool:
		return self.in_erp and self.rows is None

	def __bool__(self) -> bool:
		raise TypeError("Check .in_erp and .rows (or .unavailable) explicitly.")


def _books_payload() -> tuple[dict[str, dict], str | None]:
	"""Return (screens, pos_url) declared by ERP_BOOKS; ({}, None) when absent."""
	from kamra import erp_bridge

	result = erp_bridge.bridge_call(ERP_BOOKS, response_types=(dict, list, NoneType))
	if result is None:
		return {}, None
	screens = result.get("screens") if isinstance(result, dict) else None
	if not isinstance(screens, dict):
		raise ValueError("Invalid accounting screen response.")
	out = {}
	for screen, target in screens.items():
		if screen not in SCREENS or not isinstance(target, dict):
			raise ValueError("Invalid accounting screen response.")
		label, url = target.get("label"), target.get("url")
		if not isinstance(label, str) or not label.strip() or not isinstance(url, str) \
				or not _SITE_PATH.match(url):
			raise ValueError("Invalid accounting screen response.")
		out[screen] = {"label": label, "url": url}
	pos_url = result.get("pos_url")
	if pos_url is not None:
		if not isinstance(pos_url, str) or not _SITE_PATH.match(pos_url):
			raise ValueError("Invalid accounting screen response.")
	return out, pos_url


def erp_screens() -> dict[str, dict]:
	"""{screen: {"label", "url"}} for each screen the ERP keeps; {} when none."""
	return _books_payload()[0]


def erp_pos_url() -> str | None:
	"""Site-local URL of the active external POS (e.g. '/pos'), or None."""
	return _books_payload()[1]


def desk_screens() -> dict[str, dict]:
	"""What the desk boot carries: nothing is asked for a signed-out visitor."""
	if frappe.session.user == "Guest":
		return {}
	return erp_screens()


def desk_config() -> dict:
	"""Single-call desk boot payload: {'screens': ..., 'pos_url': ...}."""
	if frappe.session.user == "Guest":
		return {"screens": {}, "pos_url": None}
	screens, pos_url = _books_payload()
	return {"screens": screens, "pos_url": pos_url}


def _money(value, label: str) -> float:
	if isinstance(value, bool) or not isinstance(value, (int, float)):
		raise ValueError(f"Invalid {label} response.")
	return round(float(value), 2)


def erp_receivables(property: str, *, company: str | None = None) -> list[dict] | None:
	"""Live ERP Customer receivable accounts and open vouchers for `property`
	(optionally narrowed to one `company`), or None when no integration answers."""
	from kamra import erp_bridge

	form = {"property": property}
	if company is not None:
		form["company"] = company
	before_log = len(getattr(frappe.local, "message_log", None) or ())
	try:
		result = erp_bridge.bridge_call(
			ERP_RECEIVABLES, response_types=(dict, list, NoneType), **form)
	except frappe.PermissionError:
		# A role or property refusal from Hotel ERP Receivables is the same
		# fail-closed "unavailable" state for every caller — never crash or
		# leave a raw bridge refusal in message_log.
		log = getattr(frappe.local, "message_log", None)
		if isinstance(log, list) and len(log) > before_log:
			del log[before_log:]
		return None
	if result is None:
		return None
	rows = result.get("accounts") if isinstance(result, dict) else None
	if not isinstance(rows, list):
		raise ValueError("Invalid receivables response.")
	out: list[dict] = []
	for row in rows:
		if not isinstance(row, dict):
			raise ValueError("Invalid receivables response.")
		raw_items = row.get("open_items", [])
		if not isinstance(raw_items, list):
			raise ValueError("Invalid receivables response.")
		open_items: list[dict] = []
		for item in raw_items:
			if not isinstance(item, dict):
				raise ValueError("Invalid receivables response.")
			due = item.get("due_date")
			inv_date = item.get("invoice_date")
			if not isinstance(due, str) or not due.strip() or not isinstance(inv_date, str) or not inv_date.strip():
				raise ValueError("Invalid receivables response.")
			open_items.append({
				"folio": item.get("folio") or None,
				"invoice_number": item.get("invoice_number") or item.get("voucher_no") or None,
				"voucher_type": item.get("voucher_type") or None,
				"voucher_no": item.get("voucher_no") or None,
				"invoice_date": inv_date,
				"due_date": due,
				"outstanding": _money(item.get("outstanding", 0.0), "receivables"),
				"disputed": int(bool(item.get("disputed"))),
			})
		out.append({
			"account": row.get("account") or None,
			"company": row.get("company") or row.get("customer") or None,
			"customer": row.get("customer") or row.get("company") or None,
			"account_number": row.get("account_number") or "",
			"credit_limit": _money(row.get("credit_limit", 0.0), "receivables"),
			"balance": _money(row.get("balance", 0.0), "receivables"),
			"open_balance": _money(row.get("open_balance", 0.0), "receivables"),
			"unapplied_credit": _money(row.get("unapplied_credit", 0.0), "receivables"),
			"open_items": open_items,
		})
	return out


def receivables_source(property: str, *, company: str | None = None,
                       strict: bool = True) -> ReceivablesSource:
	"""Where the company receivables of `property` are read from.

	Returns ``ReceivablesSource(in_erp=False, rows=None)`` while the hotel app
	keeps them (read City Ledger Entry as before). When the ERP keeps them:
	returns ``ReceivablesSource(in_erp=True, rows=[...], target=...)`` on a
	live answer; when the lookup answers nothing, ``strict=True`` refuses
	naming the ERP report and ``strict=False`` returns
	``ReceivablesSource(in_erp=True, rows=None, target=...)`` so callers fail
	closed instead of mistaking an outage for zero debt."""
	target = erp_screens().get("receivables")
	if not target:
		return ReceivablesSource(in_erp=False, rows=None)
	rows = erp_receivables(property, company=company)
	if rows is None:
		if strict:
			refuse_kept_in_erp(target)
		return ReceivablesSource(in_erp=True, rows=None, target=target)
	return ReceivablesSource(in_erp=True, rows=rows, target=target)


def refuse_kept_in_erp(target: dict) -> None:
	frappe.throw(
		frappe._("This is kept in the accounting system. Open {0}: {1}").format(
			target["label"], target["url"]),
		frappe.PermissionError, title=frappe._("Kept in the accounting system"))


def kept_in_erp(screen: str):
	"""Refuse an endpoint of `screen` while the ERP keeps it. Goes below
	require_roles, so the role check still answers first."""
	assert screen in SCREENS, screen

	def deco(fn):
		@wraps(fn)
		def fenced(*args, **kwargs):
			target = erp_screens().get(screen)
			if target:
				refuse_kept_in_erp(target)
			return fn(*args, **kwargs)
		return fenced
	return deco


def payables_query_conditions(user: str | None = None, doctype: str | None = None) -> str:
	"""Refuse direct frappe.client.get_list / frappe.get_list reads on payables
	DocTypes while the ERP keeps payables; otherwise apply property scope."""
	if (user or frappe.session.user) != "Guest":
		target = erp_screens().get("payables")
		if target:
			refuse_kept_in_erp(target)
	from kamra.authz import property_query_conditions
	return property_query_conditions(user, doctype)


def payables_has_permission(doc, ptype: str | None = None, user: str | None = None,
                            debug: bool = False) -> bool:
	"""Refuse direct ORM permission checks on payables DocTypes while the ERP
	keeps payables; otherwise apply property scope."""
	if (user or frappe.session.user) != "Guest" and erp_screens().get("payables"):
		return False
	from kamra.authz import property_has_permission
	return property_has_permission(doc, ptype=ptype, user=user, debug=debug)
