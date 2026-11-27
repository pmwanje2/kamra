"""Lifetime and rotation for links scoped to one reservation."""

import frappe
from frappe.utils import add_days, get_datetime, now_datetime

ACTIVE_STATUSES = ("Confirmed", "Checked In")
REVOKED_STATUSES = ("Cancelled", "Checked Out", "No Show")


def _stay_end(res):
	# Keep key and laundry links usable during the departure day until the
	# stay actually checks out. Earlier checked-out or cancelled stays revoke.
	return get_datetime(add_days(res.check_out_date, 1))


def active(res) -> bool:
	if res.status not in ACTIVE_STATUSES or not res.check_out_date:
		return False
	expiry = _stay_end(res)
	stored = res.get("precheckin_token_expires_at")
	if stored:
		expiry = min(expiry, get_datetime(stored))
	return now_datetime() < expiry


def new_link_fields(res) -> dict:
	return {
		"precheckin_token": frappe.generate_hash(length=32),
		"precheckin_token_expires_at": _stay_end(res),
	}
