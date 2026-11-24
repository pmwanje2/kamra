"""Uganda localization pack: VAT (18%), TIN, UGX, Local Hotel Tax, Bank of Uganda
cash denominations and East Africa Time.

Room-tax slab mode does not exist in Uganda and is always ignored.
VAT 18% is the default rate ONLY on standalone properties (not on the ERP bridge).
On bridge sites, folio lines stay at 0% (KMR-TAX-001) because ERPNext is the
only tax engine. No static INVOICE_TITLE is declared: invoice titles follow
whether VAT was determined (PR-11).
"""

from decimal import Decimal

import frappe

from kamra.erp_bridge import is_bridge_active

DEFAULT_VAT = Decimal("18")
DEFAULT_CURRENCY = "UGX"
DEFAULT_TIMEZONE = "Africa/Kampala"
DEFAULT_NATIONALITY = "Ugandan"
HOME_COUNTRY_ISO3 = "UGA"
ROOM_LEVY_LABEL = "Local Hotel Tax"
VAT_INVOICE_TITLE = "Tax Invoice"
PAYMENT_MODES = ["Cash", "Card", "Bank Transfer", "Mobile Money"]
INSTANT_PAYMENT_MODES = ["Mobile Money"]
FX_CURRENCIES = ["USD", "EUR", "GBP", "KES"]
# Bank of Uganda banknotes (50,000..1,000) and coins (1,000..50), largest first
CASH_DENOMINATIONS = (50000, 20000, 10000, 5000, 2000, 1000, 500, 200, 100, 50)


def calculate_room_tax(property, room_type_doc, nightly_rate) -> Decimal:
	"""Flat VAT; slab fields are ignored. On ERP bridge sites folio lines stay 0%."""
	if is_bridge_active():
		return Decimal("0")
	rt_pct = room_type_doc.get("tax_percent") if room_type_doc else None
	if rt_pct:
		return Decimal(str(rt_pct))
	prop = frappe.get_cached_doc("Property", property)
	return Decimal(str(prop.get("gst_rate_low") or 0))


def fnb_tax_rate(property) -> float:
	"""Flat VAT rate on F&B; 0% on ERP bridge sites (ERPNext owns tax)."""
	if is_bridge_active():
		return 0.0
	prop = frappe.get_cached_doc("Property", property)
	return float(prop.get("gst_rate_low") or 0)


def tax_rate_options(property=None) -> list:
	if property:
		if is_bridge_active():
			return [0]
		prop = frappe.get_cached_doc("Property", property)
		low = float(prop.get("gst_rate_low") or 0)
		high = float(prop.get("gst_rate_high") or 0)
		if not low and not high:
			return [0]
		if low and low not in (0, 18):
			rate = int(low) if low.is_integer() else low
			return sorted({0, rate, 18})
	return [0, 18]


def invoice_context(prop_doc) -> dict:
	return {
		"tax_label": "VAT",
		"tax_id_label": "TIN",
		"service_code": None,
		"sac": None,
		"place_of_supply": prop_doc.get("city") or prop_doc.get("state"),
		"split": [("vat", Decimal("1"))],
		"footer": "This is a computer-generated invoice.",
	}


def locale(prop_doc) -> dict:
	currency = prop_doc.get("currency") or DEFAULT_CURRENCY
	symbol = frappe.db.get_value("Currency", currency, "symbol")
	if currency == "UGX" and symbol in (None, "", "Sh", "USh"):
		symbol = "UGX "
	return {
		"currency_symbol": symbol or f"{currency} ",
		"locale": prop_doc.get("locale") or "en-UG",
		"currency": currency,
		"tax_label": "VAT",
		"tax_id_label": "TIN",
		"tax_rates": tax_rate_options(prop_doc.get("name")),
	}
