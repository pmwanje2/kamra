"""Localization seam. The core PMS never knows about GST, VAT or fiscal
printers - it asks the country pack. Packs are resolved through the
`kamra_localization` hook (ERPNext regional_overrides style), so a future
`kamra_uae` app claims its country just by declaring the hook. Countries
without a pack fall back to a plain flat-tax `generic` pack.

Interface every pack implements (see india/):
  calculate_room_tax(property, room_type_doc, nightly_rate) -> Decimal
  fnb_tax_rate(property) -> float
  tax_rate_options(property) -> list[float]
  invoice_context(prop_doc) -> dict   (labels, service code, place of supply)
  locale(prop_doc) -> dict            (currency_symbol, locale, tax_label...)

Optional, for invoice printing - a pack that doesn't implement these gets
a sensible default from the accessors at the bottom of this file, so an
existing pack keeps working untouched:
  service_code_for(prop_doc, charge_type) -> dict | None   (per-LINE SAC/HSN)
  tax_split(prop_doc, buyer_tax_id) -> list[(label, share)]
  amount_in_words(prop_doc, amount) -> str

Optional country integrations - absent means "not offered in this country",
and the accessors refuse rather than fall back to another country's:
  PAYMENT_GATEWAYS = [...]; payment_provider(gateway) -> module
  tax_return_rows(property, from_date, to_date) -> list[dict]
  verify_tax_id(property, tax_id, business_name) -> dict
  verify_guest_id(property, guest, id_type, consent) -> dict

Optional official FX source. Absent means the open market feed
(kamra.ledger.market_rates):
  market_rates(home) -> (rates, as_of)
  rates: {ISO code: home-currency units per 1 foreign unit}
"""

import importlib
import re
from datetime import date

import frappe

_ZERO_DECIMAL_NUMBER_FORMATS = frozenset({"#,###", "#.###", "# ###", "#'###", "#,##,###"})
_HIGH_PRECISION_TAIL = re.compile(r"[.,](#{3,})$")


def _decimals_from_number_format(number_format: str | None) -> int | None:
	"""Explicit minor-unit count declared by a Frappe Currency.number_format
	(`currency-minor-units@1`). Returns 0 for integer formats (e.g. `#,###`),
	3+ for high-precision formats (e.g. `#,###.###`), or None so stock `#,###.##`
	falls back to ISO 4217 (`babel.numbers.get_currency_precision`)."""
	fmt = (number_format or "").strip()
	if not fmt:
		return None
	if fmt in _ZERO_DECIMAL_NUMBER_FORMATS:
		return 0
	match = _HIGH_PRECISION_TAIL.search(fmt)
	if match:
		return len(match.group(1))
	return None


def _minor_unit_for_currency(currency: str | None, number_format: str | None = None) -> int:
	"""Minor-unit digits for `currency`, honoring explicit `Currency.number_format`
	alignment before falling back to ISO 4217 via `kamra.pricing._minor_unit`."""
	code = (currency or "").strip().upper()
	explicit = _decimals_from_number_format(number_format)
	if explicit is not None:
		return explicit
	if number_format is None and code and (getattr(frappe.local, "db", None) or getattr(frappe, "db", None)):
		try:
			if frappe.db.table_exists("Currency"):
				db_fmt = frappe.db.get_value("Currency", code, "number_format")
				explicit = _decimals_from_number_format(db_fmt)
				if explicit is not None:
					return explicit
		except Exception:
			pass
	from kamra.pricing import _minor_unit
	return int(_minor_unit(code))


def currency_precision_map(property: str | None = None, *, home_currency: str | None = None, pack=None) -> dict[str, int]:
	"""`{ISO code: minor_unit_digits}` for enabled Currency docs plus this
	property's home currency and configured FX currencies."""
	out: dict[str, int] = {}
	if getattr(frappe.local, "db", None) or getattr(frappe, "db", None):
		try:
			if frappe.db.table_exists("Currency"):
				rows = frappe.get_all(
					"Currency",
					filters={"enabled": 1},
					fields=["name", "number_format"],
				)
				for row in rows or ():
					code = _clean_str(row.get("name") if isinstance(row, dict) else getattr(row, "name", "")).upper()
					if code:
						fmt = row.get("number_format") if isinstance(row, dict) else getattr(row, "number_format", None)
						out[code] = _minor_unit_for_currency(code, fmt)
		except Exception:
			pass

	codes: list[str] = []
	if home_currency:
		codes.append(home_currency)
	elif property:
		try:
			prop_curr = _clean_str(frappe.get_cached_value("Property", property, "currency"))
			if prop_curr:
				codes.append(prop_curr)
		except Exception:
			pass
	if pack is not None:
		pack_default = _clean_str(getattr(pack, "DEFAULT_CURRENCY", None))
		if pack_default:
			codes.append(pack_default)
		for fx in getattr(pack, "FX_CURRENCIES", None) or ():
			if isinstance(fx, str) and fx.strip():
				codes.append(fx)
	if property:
		try:
			from kamra.settings import list_setting
			for fx in list_setting(property, "fx_currencies") or ():
				if isinstance(fx, str) and fx.strip():
					codes.append(fx)
		except Exception:
			pass
	for raw in codes:
		code = raw.strip().upper()
		if code and code not in out:
			out[code] = _minor_unit_for_currency(code)
	return out


def _clean_str(val) -> str:
	return val.strip() if isinstance(val, str) else ""


def company_country_for(property: str | None = None, prop_doc=None) -> str:
	"""Country of the property's Company (or the site's default Company), or ''."""
	if not getattr(frappe.local, "db", None):
		return ""
	try:
		if not frappe.db.table_exists("Company"):
			return ""
		company = ""
		if prop_doc is not None:
			company = _clean_str(prop_doc.get("company"))
		elif property and frappe.db.has_column("Property", "company"):
			company = _clean_str(frappe.get_cached_value("Property", property, "company"))
		if not company:
			company = _clean_str(frappe.db.get_single_value("Global Defaults", "default_company"))
		if company:
			return _clean_str(frappe.db.get_value("Company", company, "country"))
	except Exception:
		pass
	return ""


def default_country(property: str | None = None, prop_doc=None) -> str:
	"""Fallback country when Property.country is blank: Company country,
	then System Settings.country, then '' (neutral / no country tax)."""
	country = company_country_for(property=property, prop_doc=prop_doc)
	if country:
		return country
	if getattr(frappe.local, "db", None):
		try:
			country = _clean_str(frappe.db.get_single_value("System Settings", "country"))
			if country:
				return country
		except Exception:
			pass
	return ""


def resolve_country(property: str | None = None, prop_doc=None) -> str:
	"""Effective country for a property: explicit Property.country, else
	Company/System country, else '' (neutral)."""
	country = ""
	if prop_doc is not None:
		country = _clean_str(prop_doc.get("country"))
	elif property:
		country = _clean_str(frappe.get_cached_value("Property", property, "country"))
	return country or default_country(property=property, prop_doc=prop_doc)


def pack_for(property: str | None = None):
	return pack_for_country(resolve_country(property))


def pack_for_country(country: str | None):
	if country:
		mapping = frappe.get_hooks("kamra_localization") or {}
		target = mapping.get(country)
		if target:
			path = target[-1] if isinstance(target, (list, tuple)) else target
			try:
				return importlib.import_module(path)
			except ModuleNotFoundError:
				pass
	from kamra.localization import generic
	return generic


# ── optional pack behaviour, with defaults ───────────────────────────────
#
#   services_tax_rate(property) -> float   (laundry & other guest services)
# A pack that predates these keeps working: each accessor falls back to
# something correct-but-plain, so adding a country never means editing the
# invoice printer.


def services_tax_rate(pack, property: str | None) -> float:
	"""The tax rate on a guest service such as laundry. A pack can say so
	(India: 18% GST on services); otherwise the pack's standard rate - the
	one its F&B rate already resolves to in every flat-VAT country."""
	fn = getattr(pack, "services_tax_rate", None)
	if fn:
		return float(fn(property))
	return float(pack.fnb_tax_rate(property))


def store_id_number(pack, id_type: str | None, value: str | None) -> str | None:
	"""How a guest's ID number is kept. Core treats every ID as a plain card
	and keeps what was typed; a pack can hold a national ID differently
	(India: Aadhaar keeps only its last 4 digits). None means no rule."""
	fn = getattr(pack, "store_id_number", None)
	return fn(id_type, value) if fn else None


def read_id_scan(pack, text: str) -> dict | None:
	"""Guest fields from a national ID's own code (India: the Aadhaar QR).
	Core reads passport MRZ itself; None means the pack has no reader or
	the scan is not one it knows."""
	fn = getattr(pack, "read_id_scan", None)
	return fn(text) if fn else None


def home_country_iso3(pack) -> str | None:
	"""The 3-letter code a passport MRZ uses for the pack's own country."""
	return getattr(pack, "HOME_COUNTRY_ISO3", None)


def default_nationality(pack) -> str:
	return getattr(pack, "DEFAULT_NATIONALITY", "") or ""


# ── defaults a property takes from its country ───────────────────────────
# A pack may declare SETTING_DEFAULTS = {registry key: value}: its tax slab,
# and amounts that only mean something in its currency (a high-balance
# threshold, a cash refund limit, a security deposit). Core ships none of
# these, so a country without them has no amount set - and nothing acts on
# an amount nobody chose (the OPERA property template, seeded per country).

def setting_default(property: str | None, key: str):
	"""The property's country pack default for one settings-registry key,
	or None where the pack has none."""
	if not property:
		return None
	return dict(getattr(pack_for(property), "SETTING_DEFAULTS", None) or {}).get(key)


def uses_tax_slab(pack) -> bool:
	"""True only for country packs that calculate room tax by nightly slab (India)."""
	return bool(getattr(pack, "USES_TAX_SLAB", False))


def property_country_defaults(country: str | None, *, prop_doc=None) -> dict:
	"""{Property field: value} a new property in this country starts with:
	the pack's currency (when the Currency exists), clock, levy label, flat VAT
	(when not on the ERP bridge) and its SETTING_DEFAULTS."""
	from kamra.erp_bridge import is_bridge_active

	resolved = _clean_str(country) or default_country(prop_doc=prop_doc)
	pack = pack_for_country(resolved)
	out = {}
	currency = getattr(pack, "DEFAULT_CURRENCY", None)
	if currency and frappe.db.exists("Currency", currency):
		out["currency"] = currency
	if getattr(pack, "DEFAULT_TIMEZONE", None):
		out["timezone"] = pack.DEFAULT_TIMEZONE
	if getattr(pack, "ROOM_LEVY_LABEL", None):
		out["room_levy_label"] = pack.ROOM_LEVY_LABEL
	if getattr(pack, "DEFAULT_VAT", None) and not is_bridge_active():
		out["gst_rate_low"] = float(pack.DEFAULT_VAT)
	out.update(getattr(pack, "SETTING_DEFAULTS", None) or {})
	meta = frappe.get_meta("Property")
	return {k: v for k, v in out.items() if meta.has_field(k)}


def fill_property_defaults(doc) -> None:
	"""A new Property takes what the operator left blank from its country
	pack (property_country_defaults). Values given always win; an existing
	property is never touched. An import carries its own values, as Frappe
	skips doctype defaults there too."""
	if frappe.flags.in_import:
		return
	for key, value in property_country_defaults(doc.get("country"), prop_doc=doc).items():
		if doc.get(key) in (None, ""):
			doc.set(key, value)


def amount_text(property: str | None, amount) -> str:
	"""An amount formatted to the property currency's minor-unit precision
	(0 decimals for UGX/JPY, 2 for INR/USD, 3 for KWD) for folio line
	descriptions and plain-number messages."""
	currency = ""
	try:
		if property:
			currency = frappe.get_cached_value("Property", property, "currency") or ""
	except Exception:
		currency = ""
	digits = _minor_unit_for_currency(currency)
	try:
		value = float(amount or 0)
	except (TypeError, ValueError):
		return str(amount)
	return f"{value:,.{digits}f}"


def money_text(property: str | None, amount) -> str:
	"""An amount in the property's currency for server-written text (the
	activity log, alerts) - never a hardcoded rupee sign."""
	symbol, currency = "", ""
	try:
		if property:
			prop = frappe.get_cached_doc("Property", property)
			loc = pack_for(property).locale(prop)
			symbol, currency = loc.get("currency_symbol") or "", loc.get("currency") or ""
	except Exception:
		symbol = ""
	try:
		value = float(amount or 0)
	except (TypeError, ValueError):
		return f"{symbol}{amount}"
	minor = _minor_unit_for_currency(currency)
	if minor == 0:
		digits = f"{abs(value):,.0f}"
	elif minor > 2:
		digits = f"{abs(value):,.{minor}f}"
	else:
		digits = group_digits(abs(value), currency == "INR")
	return f"{'-' if value < 0 else ''}{symbol}{digits}"


def group_digits(value: float, lakh: bool = False) -> str:
	"""11025 -> 11,025 and 28360.4 -> 28,360.40; with lakh, 120000 -> 1,20,000
	as the desk shows rupees."""
	text = f"{value:,.2f}".removesuffix(".00")
	if not lakh:
		return text
	whole, _, frac = text.replace(",", "").partition(".")
	head, tail = whole[:-3], whole[-3:]
	groups = [head[max(i - 2, 0):i] for i in range(len(head), 0, -2)][::-1]
	return ",".join([*groups, tail]) + (f".{frac}" if frac else "")


def service_code_for(pack, prop_doc, charge_type: str | None = None):
	"""The tax service code for ONE line. A bill that mixes a room night,
	a restaurant cover and a laundry bag carries three different codes -
	printing the accommodation code against all of them is wrong."""
	fn = getattr(pack, "service_code_for", None)
	if fn:
		return fn(prop_doc, charge_type)
	return pack.invoice_context(prop_doc).get("service_code")


def tax_split(pack, prop_doc, buyer_tax_id: str | None = None):
	"""How the tax on this bill is named and divided - [(label, share)].
	Passed the buyer's tax id because in some countries who they are (and
	where) changes the answer."""
	fn = getattr(pack, "tax_split", None)
	if fn:
		return fn(prop_doc, buyer_tax_id)
	return pack.invoice_context(prop_doc)["split"]


def invoice_tax_note(pack) -> str:
	"""A line under the invoice on how tax is shown (India: GST inclusive),
	or "" where the pack has none."""
	return getattr(pack, "INVOICE_TAX_NOTE", None) or ""


def amount_in_words(pack, prop_doc, amount) -> str:
	fn = getattr(pack, "amount_in_words", None)
	if fn:
		return fn(prop_doc, amount)
	from kamra.localization.words import amount_in_words as spell

	loc = pack.locale(prop_doc)
	return spell(amount, loc.get("currency") or "", indian=False)


# ── front-desk vocabulary: IDs and ways to pay ───────────────────────────
# A pack declares ID_TYPES / PAYMENT_MODES / DEFAULT_* as module constants.
# The desk offers Cash, Card and Bank Transfer everywhere. A pack may add
# its own instant / wallet modes (INSTANT_PAYMENT_MODES, e.g. India's UPI):
# each is a system Payment Method (kamra.masters), and the till keeps them
# in their own bucket (Cashier Session.system_upi). A hotel's own Payment
# Methods (Settings data, OPERA payment methods) are offered beside the
# pack's in every country; an Instant one shares the instant bucket.

GENERIC_ID_TYPES = ["Passport", "National ID", "Driving License", "Other"]
GENERIC_PAYMENT_MODES = ["Cash", "Card", "Bank Transfer"]
# modes that are not a country's: any property may record them
CORE_PAYMENT_MODES = ("Cash", "Foreign Cash", "Card", "Bank Transfer", "OTA Prepaid",
                      "Company Credit", "Payment Link", "Cheque", "Other")


def id_types(pack) -> list[str]:
	return list(getattr(pack, "ID_TYPES", None) or GENERIC_ID_TYPES)


def _pack_instant_modes(pack) -> list[str]:
	return list(getattr(pack, "INSTANT_PAYMENT_MODES", None) or [])


def _pack_payment_modes(pack) -> list[str]:
	modes = getattr(pack, "PAYMENT_MODES", None) or GENERIC_PAYMENT_MODES
	allowed = (*GENERIC_PAYMENT_MODES, *_pack_instant_modes(pack))
	return [m for m in modes if m in allowed]


FOREIGN_CURRENCY_TENDER_TYPES = ("Cash", "Foreign Cash", "Bank Transfer")


def instant_payment_modes(pack, property: str | None = None) -> list[str]:
	"""The pack's instant / wallet modes (excluding property-disabled
	overrides), then this property's own Instant payment methods."""
	from kamra.masters import client_payment_methods, disabled_payment_methods
	disabled = disabled_payment_methods(property)
	base = [m for m in _pack_instant_modes(pack) if m not in disabled]
	return list(dict.fromkeys([*base, *client_payment_methods("Instant", property=property)]))


def payment_modes(pack, property: str | None = None) -> list[str]:
	"""What the desk takes money in: the pack's modes (excluding
	property-disabled overrides), then this property's own enabled payment
	methods."""
	from kamra.masters import client_payment_methods, disabled_payment_methods
	disabled = disabled_payment_methods(property)
	base = [m for m in _pack_payment_modes(pack) if m not in disabled]
	return list(dict.fromkeys([*base, *client_payment_methods(property=property)]))


def cash_denominations(pack) -> list[float]:
	"""The notes and coins a till counts at close (CASH_DENOMINATIONS),
	largest first. Empty where the pack names none: any value is counted."""
	return [float(v) for v in getattr(pack, "CASH_DENOMINATIONS", None) or ()]


OUTLET_TENDER_TYPES = ("Cash", "Card", "Instant")


def outlet_payment_modes(pack, property: str | None = None) -> list[str]:
	"""What a bill is settled with at an outlet (POS): enabled Cash, Card,
	and Instant methods for this property (or the country pack when no
	property is passed)."""
	from kamra.masters import SYSTEM_TENDER_TYPES, payment_tender_type

	if property:
		return [
			m for m in payment_modes(pack, property)
			if payment_tender_type(m, property) in OUTLET_TENDER_TYPES
		]
	instant = _pack_instant_modes(pack)
	return [
		m for m in _pack_payment_modes(pack)
		if SYSTEM_TENDER_TYPES.get(m) in OUTLET_TENDER_TYPES or m in instant
	]


def assert_pack_payment_mode(property: str, mode: str | None) -> None:
	"""Country vocabulary for non-folio operations, including historical refunds."""
	if not mode or mode in CORE_PAYMENT_MODES:
		return
	modes = payment_modes(pack_for(property), property)
	if mode not in modes:
		frappe.throw(frappe._("{0} is not a payment mode here. Pick one of: {1}.")
		             .format(mode, ", ".join(modes)))


def tender_modes(property: str) -> list[dict] | None:
	"""Optional property tender/currency matrix. An empty matrix is authoritative."""
	from kamra.erp_bridge import bridge_call

	result = bridge_call("hospitality_kamra.tender_modes", response_types=(dict, list), property=property)
	if result is None:
		return None
	rows = result.get("tender_modes") if isinstance(result, dict) else result
	if not isinstance(rows, list) or any(
		not isinstance(row, dict) or not isinstance(row.get("mode"), str)
		or not isinstance(row.get("currencies"), list)
		or any(not isinstance(code, str) or len(code) != 3 or not code.isascii()
		       or not code.isalpha() or not code.isupper() for code in row["currencies"])
		for row in rows
	):
		raise ValueError("Invalid tender configuration.")
	return [{"mode": row["mode"], "currencies": row["currencies"]} for row in rows]


def assert_payment_mode(property: str, mode: str | None, currency: str | None = None,
                        *, pack_only: bool = False) -> None:
	"""Validate a new folio tender against the property mapping, when installed."""
	if not mode and not pack_only:
		return
	from kamra.masters import (
		default_mode_for_tender,
		disabled_payment_methods,
		is_system_payment_method,
		payment_tender_type,
	)

	tender = payment_tender_type(mode, property)
	mapped = tender_modes(property)
	if mapped is None:
		if mode and mode in disabled_payment_methods(property):
			modes = payment_modes(pack_for(property), property)
			frappe.throw(frappe._("{0} is not a payment mode here. Pick one of: {1}.")
			             .format(mode, ", ".join(modes)))
		if pack_only:
			modes = payment_modes(pack_for(property), property)
			if mode not in modes and not (tender == "Foreign Cash" and currency):
				frappe.throw(frappe._("Pick a payment mode: {0}.").format(", ".join(modes)))
		else:
			assert_pack_payment_mode(property, mode)
		if currency or tender == "Foreign Cash":
			from kamra.ledger import home_currency
			home = home_currency(property)
			code = (currency or "").strip().upper() or home
			if code == home and tender == "Foreign Cash":
				frappe.throw(frappe._("Foreign Cash needs the currency of the notes received."))
			if code != home and tender not in FOREIGN_CURRENCY_TENDER_TYPES:
				frappe.throw(frappe._("{0} is not available for payments in {1}.").format(mode, code))
		return
	from kamra.ledger import home_currency

	home = home_currency(property)
	code = (currency or "").strip().upper() or home
	mode_names = {row["mode"] for row in mapped}
	target = mode
	if code == home and tender == "Foreign Cash":
		frappe.throw(frappe._("Foreign Cash needs the currency of the notes received."))
	if code != home and tender in ("Cash", "Foreign Cash") and is_system_payment_method(mode, property):
		# Some bridges report the posted mode; others group all notes under Cash.
		fx_cash = default_mode_for_tender(property, "Foreign Cash")
		home_cash = default_mode_for_tender(property, "Cash")
		target = fx_cash if fx_cash in mode_names else home_cash
	foreign_not_supported = code != home and tender not in FOREIGN_CURRENCY_TENDER_TYPES
	if foreign_not_supported or not any(row["mode"] == target and code in row["currencies"] for row in mapped):
		frappe.throw(frappe._("{0} is not available for payments in {1}.").format(mode, code))


def default_currency(pack) -> str:
	"""The pack's home currency, for a property that has none set."""
	return getattr(pack, "DEFAULT_CURRENCY", None) or "USD"


def front_desk_vocabulary(pack, property: str | None = None) -> dict:
	from kamra.masters import payment_tender_types
	home = ""
	if property:
		try:
			home = _clean_str(frappe.get_cached_value("Property", property, "currency"))
		except Exception:
			home = ""
	home = (home or default_currency(pack)).upper()
	decimals_map = currency_precision_map(property, home_currency=home, pack=pack)
	return {
		"decimals": decimals_map.get(home, _minor_unit_for_currency(home)),
		"currency_decimals": decimals_map,
		"id_types": id_types(pack),
		# a help line per ID type, e.g. which copy of a national ID is fine
		"id_type_hints": dict(getattr(pack, "ID_TYPE_HINTS", None) or {}),
		"payment_modes": payment_modes(pack, property),
		"payment_tender_types": payment_tender_types(property),
		"instant_payment_modes": instant_payment_modes(pack, property),
		"outlet_payment_modes": outlet_payment_modes(pack, property),
		"default_nationality": getattr(pack, "DEFAULT_NATIONALITY", "") or "",
		"adult_age": adult_age(pack),
		"minor_ids": minor_ids(pack),
	}


def financial_year_start(pack, d) -> date:
	"""The first day of the financial year d falls in, from the pack's
	FINANCIAL_YEAR_START (month, day): 1 April 2026 for 10 Feb 2027 where
	the year runs from 1 April (India); 1 January where it has none."""
	from frappe.utils import getdate
	month, day = getattr(pack, "FINANCIAL_YEAR_START", (1, 1))
	d = getdate(d)
	start = date(d.year, month, day)
	return start if start <= d else date(d.year - 1, month, day)


def financial_year(pack, d) -> int:
	"""The year a date's financial year started in: 2026 for 10 Feb 2027
	where the year runs from 1 April (India), 2027 where it is the
	calendar year. Numbering that resets each year uses it."""
	return financial_year_start(pack, d).year


# ── children on the stay register (India DPDP s.9 and alike) ─────────────
# A minor is recorded with name and age only. ID details are kept only where
# registration law asks for them: by default for foreign nationals (Form C,
# TM30 and the like); a pack whose register covers every guest sets
# MINOR_IDS = "all".

def adult_age(pack) -> int:
	return int(getattr(pack, "ADULT_AGE", 18))


def minor_ids(pack) -> str:
	return getattr(pack, "MINOR_IDS", "foreign")


def is_minor(pack, age) -> bool:
	try:
		return age not in (None, "") and int(age) < adult_age(pack)
	except (TypeError, ValueError):
		return False


def minor_id_kept(pack, nationality: str | None) -> bool:
	"""May ID details be recorded for a minor of this nationality?"""
	if minor_ids(pack) == "all":
		return True
	home = (getattr(pack, "DEFAULT_NATIONALITY", "") or "").strip().casefold()
	nat = (nationality or "").strip().casefold()
	return bool(nat) and nat != home


def registration_card_required(pack) -> bool:
	"""Does registration law here expect a signed registration card (GRC)
	for every stay? The default for the guided check-in's GRC step."""
	return bool(getattr(pack, "REGISTRATION_CARD_REQUIRED", False))


def privacy_terms(pack) -> dict:
	"""Who a guest complains to, and the statutory guest report (if any)
	the hotel files - for the privacy notice where data is collected."""
	return {
		"privacy_authority": getattr(pack, "PRIVACY_AUTHORITY", None)
		                     or "your local data protection authority",
		"guest_report": getattr(pack, "GUEST_REPORT", None),
		"adult_age": adult_age(pack),
		# languages a guest may read the notice in besides the page's own
		"notice_languages": list(getattr(pack, "NOTICE_LANGUAGES", None) or []),
	}


def validate_id_type(property: str | None, id_type: str | None):
	"""An ID type must be one this property's country recognises. Values
	already on file are never re-checked - only new writes."""
	if not id_type:
		return
	allowed = id_types(pack_for(property))
	if id_type not in allowed:
		frappe.throw(
			frappe._("Unknown ID type {0}. Use one of: {1}.").format(
				id_type, ", ".join(allowed)))


# ── country integrations: payment gateways, tax returns, verification ────
# The core keeps the entry points (create a link, settle it, export a
# return, check an ID); the pack decides whether the country has the
# integration at all and which provider runs it.


def _not_offered(what: str):
	frappe.throw(frappe._("{0} is not available for this property's country.")
	             .format(what))


def payment_gateways(pack) -> list[str]:
	"""Online gateways the desk may configure in this country."""
	if not getattr(pack, "payment_provider", None):
		return []
	return list(getattr(pack, "PAYMENT_GATEWAYS", None) or [])


def _provider(pack, gateway: str | None):
	if not gateway or gateway not in payment_gateways(pack):
		_not_offered(frappe._("The {0} payment gateway").format(gateway or "online"))
	return pack.payment_provider(gateway)


def create_gateway_link(pack, settings, **link) -> tuple[str, str]:
	"""A live payment link on the property's own gateway account.
	Returns (link id, url)."""
	return _provider(pack, settings.gateway).create_link(settings, **link)


def test_gateway_link(pack, gateway: str | None, link_id: str) -> str:
	"""Where a test-mode link points - nothing is charged."""
	return _provider(pack, gateway).test_link(link_id)


def gateway_info(pack, gateway: str | None) -> dict:
	"""What Settings shows beside the keys: webhook URL, signup link."""
	return _provider(pack, gateway).info()


def _gateway_provider_any(gateway: str):
	"""The provider for a gateway name, from whichever pack offers it. A
	webhook names its gateway before it names a property."""
	mapping = frappe.get_hooks("kamra_localization") or {}
	for target in mapping.values():
		path = target[-1] if isinstance(target, (list, tuple)) else target
		try:
			pack = importlib.import_module(path)
		except ModuleNotFoundError:
			continue
		if gateway in payment_gateways(pack):
			return pack.payment_provider(gateway)
	return None


def parse_payment_webhook(gateway: str, payload: bytes, headers: dict,
                          settings_for) -> dict | None:
	"""A gateway's "paid" callback, verified against the property's own
	keys. Returns {folio, property, link_id, amount}, or None when the
	event is not a payment we act on. A bad signature raises."""
	provider = _gateway_provider_any(gateway)
	if not provider:
		return None
	folio = provider.webhook_folio(payload)
	if not folio or not frappe.db.exists("Folio", folio):
		return None
	property = frappe.db.get_value("Folio", folio, "property")
	if gateway not in payment_gateways(pack_for(property)):
		return None
	settings = settings_for(property)
	if settings.gateway != gateway:
		frappe.throw(frappe._("This property does not take payments through {0}.")
		             .format(gateway), frappe.PermissionError)
	paid = provider.verify_webhook(settings, payload, headers)
	if not paid:
		return None
	return {"folio": folio, "property": property, **paid}


def tax_return_rows(pack, property: str | None, from_date: str, to_date: str):
	fn = getattr(pack, "tax_return_rows", None)
	if not fn:
		_not_offered(frappe._("The tax return export"))
	return fn(property, from_date, to_date)


def verify_tax_id(pack, property: str, tax_id: str, business_name: str = ""):
	if not verify_released():
		frappe.throw(frappe._("The ID verification partner isn't available yet."))
	fn = getattr(pack, "verify_tax_id", None)
	if not fn:
		_not_offered(frappe._("Tax ID verification"))
	return fn(property, tax_id, business_name)


def verify_guest_id(pack, property: str, guest: str, id_type: str,
                    consent: int = 0):
	if not verify_released():
		frappe.throw(frappe._("The ID verification partner isn't available yet."))
	fn = getattr(pack, "verify_guest_id", None)
	if not fn:
		_not_offered(frappe._("Guest ID verification"))
	return fn(property, guest, id_type, consent)


def verify_released() -> bool:
	"""Kamra Verify isn't released yet: its hub service isn't live. Until it
	is, every Verify button, the Settings page and the endpoints stay off
	unless a site opts in with `kamra_verify_enabled: 1` in site config."""
	return bool(frappe.conf.get("kamra_verify_enabled"))


def integrations(pack) -> dict:
	"""Which country integrations this property has - so the desk shows a
	Verify button or a gateway picker only where they exist."""
	verify = verify_released()
	return {
		"payment_gateways": payment_gateways(pack),
		"tax_return": bool(getattr(pack, "tax_return_rows", None)),
		"verify_tax_id": verify and bool(getattr(pack, "verify_tax_id", None)),
		"verify_guest_id": verify and bool(getattr(pack, "verify_guest_id", None)),
	}


def supported_countries() -> list[dict]:
	"""Countries with a dedicated pack, and what picking one sets up - the
	setup wizard and Settings offer these. Anything else runs on the
	generic pack with the currency the operator chooses."""
	mapping = frappe.get_hooks("kamra_localization") or {}
	out = []
	for country, target in mapping.items():
		path = target[-1] if isinstance(target, (list, tuple)) else target
		try:
			pack = importlib.import_module(path)
		except ModuleNotFoundError:
			continue
		ctx = pack.invoice_context(frappe._dict(name=None))
		out.append({
			"country": country,
			"currency": getattr(pack, "DEFAULT_CURRENCY", None),
			"timezone": getattr(pack, "DEFAULT_TIMEZONE", None),
			"tax_label": ctx.get("tax_label"),
			"tax_id_label": ctx.get("tax_id_label"),
		})
	return sorted(out, key=lambda c: c["country"])


# ── statutory e-invoicing hooks ──────────────────────────────────────────
# A pack that must report invoices to its tax authority (Saudi ZATCA, and
# tomorrow others) implements on_invoice_issued / on_invoice_cancelled /
# on_pos_bill_paid. The core calls these after the bill is final; a
# failure is logged loudly but never stops the desk closing a bill - the
# record can be regenerated, a guest kept waiting at checkout cannot.


def _hook(pack, name, *args):
	fn = getattr(pack, name, None)
	if not fn:
		return None
	# all-or-nothing: a half-written record would fork the invoice chain
	sp = f"einv_{name}"
	frappe.db.savepoint(sp)
	try:
		return fn(*args)
	except Exception:
		frappe.db.rollback(save_point=sp)
		frappe.log_error(title=f"e-invoicing: {name} failed")
		return None


def on_invoice_issued(pack, folio_name: str):
	return _hook(pack, "on_invoice_issued", folio_name)


def on_invoice_cancelled(pack, folio_name: str, invoice_number: str,
                         reason: str):
	return _hook(pack, "on_invoice_cancelled", folio_name, invoice_number,
	             reason)


def on_pos_bill_paid(pack, order_name: str, tax_rate: float):
	return _hook(pack, "on_pos_bill_paid", order_name, tax_rate)


def invoice_title(pack, *, tax_charged: bool = False) -> str:
	"""What a settled bill is called. "Tax Invoice" only where the law asks
	for those words (a pack sets INVOICE_TITLE, or VAT_INVOICE_TITLE when
	VAT/GST is actually charged on the settled invoice); everywhere else
	"Invoice"."""
	explicit = getattr(pack, "INVOICE_TITLE", None)
	if explicit:
		return explicit
	if tax_charged:
		vat_title = getattr(pack, "VAT_INVOICE_TITLE", None)
		if vat_title:
			return vat_title
	return "Invoice"


def neutral_invoice_title(pack) -> str:
	"""Neutral title for a settled bill when tax could not be read: never
	claims "Tax Invoice" without verified tax figures."""
	explicit = getattr(pack, "NEUTRAL_INVOICE_TITLE", None)
	if explicit:
		return explicit
	title = invoice_title(pack)
	if "tax invoice" in title.lower():
		return "Invoice"
	return title


def invoice_print_block(pack, source_doctype: str, source_name: str,
                        number: str | None = None):
	"""Whatever the authority requires ON the printed bill (ZATCA's QR and
	bilingual title) - None where nothing is required."""
	return _hook(pack, "invoice_print_block", source_doctype, source_name,
	             number)
