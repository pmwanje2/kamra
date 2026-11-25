"""Site-local presentation identity. Never changes roles or API identifiers."""

import re

import frappe


def white_label() -> bool:
	return str(frappe.conf.get("kamra_white_label", 0)).strip().lower() in {"1", "true"}


def product_name() -> str:
	return str(frappe.conf.get("kamra_product_name") or "").strip() or (
		"Hotel PMS" if white_label() else "Kamra"
	)


def product_token() -> str:
	"""product_name() as one token, for a User-Agent or a command-line name."""
	return re.sub(r"[^A-Za-z0-9]+", "-", product_name()).strip("-") or "Hotel-PMS"


# SiteOps serves the SPA under this mask on white-labelled sites; the front
# end's router recognises it too (frontend/src/lib/routing.ts).
WHITE_LABEL_MOUNT = "/hotel"


def app_path(route: str = "") -> str:
	"""Site path of an app screen, under the mount a person sees."""
	mount = WHITE_LABEL_MOUNT if white_label() else "/kamra"
	return mount + ("/" + route.lstrip("/") if route else "")


def public_url(route: str) -> str:
	"""Absolute link sent to a guest. White-labelled sites send the clean
	route (/checkin/<token>), which Kamra's hooks and the SiteOps mask both
	redirect into the app."""
	return frappe.utils.get_url() + ("" if white_label() else "/kamra") + "/" + route.lstrip("/")


def assistant_name() -> str:
	return str(frappe.conf.get("kamra_assistant_name") or "").strip() or (
		"Assistant" if white_label() else "Kamra Agent"
	)


def role_label(role: str) -> str:
	if role == "Kamra Agent" and (white_label() or assistant_name() != role):
		return f"{assistant_name()} (service)"
	if role.startswith("URY "):
		return f"POS {role[4:]}"
	return role


def provider_label(provider: str | None) -> str:
	"""Display name for an external channel provider; keeps stored provider
	keys intact for webhook routing."""
	value = (provider or "").strip()
	if white_label() and value.casefold() == "heykoala":
		return "Voice partner"
	return value


def _site_asset(url: str | None) -> str | None:
	# Kamra's own artwork (its logos, favicon-32, apple-touch-180) ships under
	# /assets/kamra/; a hotel's uploaded file of the same name is its own.
	if white_label() and url and re.search(
		r"kamra-(?:mark|logo|horizontal)|/assets/kamra/|kamrapms\.com|/assets/frappe/|frappe-(?:logo|favicon)", url, re.I):
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
