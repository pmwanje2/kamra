"""Ugandan desk defaults; tax follows the property's configured flat rate."""

import frappe

from kamra.localization import generic
from kamra.localization.generic import (
	calculate_room_tax,
	fnb_tax_rate,
	invoice_context,
	tax_rate_options,
)

DEFAULT_CURRENCY = "UGX"
DEFAULT_TIMEZONE = "Africa/Kampala"
DEFAULT_NATIONALITY = "Ugandan"
HOME_COUNTRY_ISO3 = "UGA"
PAYMENT_MODES = ["Cash", "Card", "Bank Transfer", "Mobile Money"]
INSTANT_PAYMENT_MODES = ["Mobile Money"]
FX_CURRENCIES = ["USD", "EUR", "GBP", "KES"]


def locale(prop_doc):
	loc = generic.locale(
		frappe._dict(name=prop_doc.get("name"), currency=prop_doc.get("currency") or DEFAULT_CURRENCY)
	)
	return {**loc, "locale": "en-UG"}
