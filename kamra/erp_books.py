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
``{"screens": {screen: {"label": ..., "url": ...}}}``. Keys are from
SCREENS, and a url is a path on this site. A kept screen's endpoints refuse
(kept_in_erp) and the desk hides it (window.kamra_erp_screens). A screen the
answer leaves out stays. A site with no such script keeps every screen. A
malformed answer raises ValueError.
"""

from __future__ import annotations

import re
from functools import wraps

import frappe

ERP_BOOKS = "hospitality_kamra.erp_books"
SCREENS = ("payables", "cash_forecast", "profit_and_loss", "balance_sheet",
           "accounting_export", "receivables")
# a path on this site: no scheme, no host, no control characters
_SITE_PATH = re.compile(r"/(?!/)[^\s\\]*\Z")


def erp_screens() -> dict[str, dict]:
	"""{screen: {"label", "url"}} for each screen the ERP keeps; {} when none."""
	from kamra import erp_bridge

	result = erp_bridge.bridge_call(ERP_BOOKS)
	if result is None:
		return {}
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
	return out


def desk_screens() -> dict[str, dict]:
	"""What the desk boot carries: nothing is asked for a signed-out visitor."""
	if frappe.session.user == "Guest":
		return {}
	return erp_screens()


def kept_in_erp(screen: str):
	"""Refuse an endpoint of `screen` while the ERP keeps it. Goes below
	require_roles, so the role check still answers first."""
	assert screen in SCREENS, screen

	def deco(fn):
		@wraps(fn)
		def fenced(*args, **kwargs):
			target = erp_screens().get(screen)
			if target:
				frappe.throw(
					frappe._("This is kept in the accounting system. Open {0}: {1}").format(
						target["label"], target["url"]),
					frappe.PermissionError, title=frappe._("Kept in the accounting system"))
			return fn(*args, **kwargs)
		return fenced
	return deco
