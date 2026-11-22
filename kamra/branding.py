"""Site-local presentation identity. Never changes roles or API identifiers."""

import re

import frappe


def white_label() -> bool:
	return str(frappe.conf.get("kamra_white_label", 0)).strip().lower() in {"1", "true"}


def product_name() -> str:
	return str(frappe.conf.get("kamra_product_name") or "").strip() or (
		"Hotel PMS" if white_label() else "Kamra"
	)


def assistant_name() -> str:
	return str(frappe.conf.get("kamra_assistant_name") or "").strip() or (
		"Assistant" if white_label() else "Kamra Agent"
	)


def role_label(role: str) -> str:
	if role == "Kamra Agent" and (white_label() or assistant_name() != role):
		return f"{assistant_name()} (service)"
	return role


def _site_asset(url: str | None) -> str | None:
	if white_label() and url and re.search(r"kamra-mark|kamrapms\.com|/assets/frappe/|frappe-(?:logo|favicon)", url, re.I):
		return None
	return url or None


def brand_context(logo=None) -> dict:
	return {
		"product": product_name(),
		"assistant": assistant_name(),
		"white_label": white_label(),
		"logo": logo,
		"favicon": _site_asset(frappe.get_website_settings("favicon")),
	}


def brand_logo() -> str | None:
	if not white_label():
		website = frappe.get_website_settings("app_logo")
		if website:
			return website
	return _site_asset(frappe.db.get_single_value("Navbar Settings", "app_logo"))
