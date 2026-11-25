"""Open checks at outlets another point of sale runs.

When the restaurant, bar and room service run on a separate point of sale,
the app's own POS Order table is empty. Its open checks are then
invisible to check-out and to the End of Day checklist. An integration can
answer for them with an enabled API Server Script at OPEN_CHECKS, called
through kamra.erp_bridge (no RPC of its own). The arguments are the property
and, for one stay, its reservation and room, or the business date being
closed.

The answer is ``{"checks": [{name, outlet, table, room, reservation,
amount, billed, opened_at}]}``. ``name`` and ``amount`` are required.
``billed`` is true once the bill was presented. Everything else is
optional text. A malformed answer raises ValueError: money nobody holds is
never waved through.

A site with no such script gets None, and every caller behaves as before.
"""

from __future__ import annotations

import math
from types import NoneType

OPEN_CHECKS = "hospitality_kamra.open_outlet_checks"
_TEXT = ("outlet", "table", "room", "reservation", "opened_at")


def _places(property: str | None) -> int:
	if not property:
		return 2
	import frappe

	from kamra.ledger import _currency_places
	try:
		return _currency_places(property)
	except frappe.DoesNotExistError:
		return 2


def open_outlet_checks(property: str, *, reservation: str | None = None,
                       room: str | None = None, business_date: str | None = None) -> list[dict] | None:
	"""The outlet checks still open for a stay (reservation, room) or a
	property (business_date), or None when no integration answers."""
	from kamra import erp_bridge

	form = {"property": property, "reservation": reservation, "room": room,
	        "business_date": business_date}
	result = erp_bridge.bridge_call(OPEN_CHECKS, response_types=(dict, list, NoneType),
	                                **{k: v for k, v in form.items() if v is not None})
	if result is None:
		return None
	rows = result.get("checks") if isinstance(result, dict) else None
	if not isinstance(rows, list):
		raise ValueError("Invalid outlet check response.")
	places = _places(property)
	return [_check(row, places=places) for row in rows]


def _check(row, *, places: int = 2) -> dict:
	if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"].strip():
		raise ValueError("Invalid outlet check response.")
	amount = row.get("amount")
	if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(float(amount)):
		raise ValueError("Invalid outlet check response.")
	from kamra.ledger import _round_currency
	out = {"name": row["name"]}
	for key in _TEXT:
		value = row.get(key)
		if value is not None and not isinstance(value, str):
			raise ValueError("Invalid outlet check response.")
		out[key] = value or None
	out["amount"] = _round_currency(amount, places)
	out["billed"] = bool(row.get("billed"))
	return out
