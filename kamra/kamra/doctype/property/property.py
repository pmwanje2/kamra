# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document

SHORT_CODE = re.compile(r"^[A-Z0-9]{2,6}$")


def default_short_code(name: str, exclude: str | None = None) -> str:
	"""The property's initials (2-3 letters or digits), with a digit added
	when another property already holds them: "Sunrise Residency" -> SR,
	a second one -> SR2."""
	words = re.findall(r"[A-Z0-9]+", (name or "").upper())
	base = "".join(w[0] for w in words)[:3]
	if len(base) < 2:
		base = "".join(words)[:3]
	if len(base) < 2:
		base = "PR"
	taken = set(frappe.get_all("Property", filters={"name": ("!=", exclude or "")},
	                           pluck="short_code"))
	if base not in taken:
		return base
	for n in range(2, 100):
		code = f"{base[:6 - len(str(n))]}{n}"
		if code not in taken:
			return code
	frappe.throw(_("Pick a short code for this property."))
	raise  # frappe.throw always raises; CodeQL does not treat it as noreturn


class Property(Document):
	def _set_defaults(self):
		# Frappe's _set_defaults copies Global Defaults.currency (which ERPNext
		# seeds to INR on fresh sites) onto blank Link-to-Currency fields before
		# before_insert runs, masking the country pack's currency (e.g. UGX/KWD)
		# or a neutral blank fallback. Explicit operator values always win.
		had_currency = bool((self.currency or "").strip())
		super()._set_defaults()
		if not had_currency:
			self.currency = None

	def before_insert(self):
		# currency, clock, tax slab and money amounts come from the country
		# pack, never an Indian default; what the operator typed wins
		from kamra.localization import fill_property_defaults
		fill_property_defaults(self)

	def validate(self):
		if not self.is_new() and (self.has_value_changed("house_overbooking_limit")
		                         or self.has_value_changed("overbooking_protection")):
			from kamra.kamra.doctype.reservation.reservation import lock_property_for_inventory
			from kamra.rooms import nightly_sell_adjustments, property_inventory
			from kamra.sell_limits import effective_capacities
			from frappe.utils import nowdate
			lock_property_for_inventory(self.name)
			if self.overbooking_protection:
				rooms = property_inventory(self.name)
				start = self.business_date or nowdate()
				limits = nightly_sell_adjustments(self.name, start, "9999-12-31")
				for adjustments in limits.values():
					try:
						effective_capacities(rooms, adjustments, self.house_overbooking_limit or 0, True)
					except ValueError as exc:
						frappe.throw(_(str(exc)))
		if self.get("early_checkin_fee_type") == "Percentage" and not 0 <= float(
				self.get("early_checkin_fee_value") or 0) <= 100:
			frappe.throw(_("The early check-in percentage must be between 0 and 100."))
		if self.get("early_checkin_cutoff") and self.get("early_checkin_fee_type") in ("Fixed", "Percentage"):
			from kamra.no_show import _as_time
			start = _as_time(self.early_checkin_cutoff)
			standard = _as_time(self.get("checkin_time") or "14:00")
			if start and standard and start >= standard:
				frappe.throw(_("The early check-in window must start before standard check-in."))
		self._validate_short_code()
		self._validate_booking_language()
		# setup, Settings and the booking engine all save the hotel's phone
		# here: read it with the hotel's own country (#468), as typed if not
		if self.phone and (self.is_new() or self.has_value_changed("phone")
		                   or self.has_value_changed("country")):
			from kamra import phone as phone_mod
			code = frappe.get_cached_value("Country", self.country, "code") \
				if self.country else None
			self.phone = phone_mod.normalise(self.phone, code.upper() if code else None)[0]
		# A kind switch in Settings (or Desk) gets the new kind's preset,
		# the same way setup_property applies it at creation. Only empty
		# preset fields are filled, so explicit admin values survive.
		if not self.is_new() and self.has_value_changed("property_kind"):
			from kamra.property_presets import apply_kind_change
			apply_kind_change(self)

	def _validate_short_code(self):
		# Numbers allowance vouchers (ALW-<code>-YY-#####). The series key
		# holds the code, so a new code starts a new series and never
		# reuses a number. Invoice numbering does not use it.
		code = (self.short_code or "").strip().upper()
		if not code:
			code = default_short_code(self.property_name or self.name,
			                          exclude=self.name)
		if not SHORT_CODE.match(code):
			frappe.throw(_("Short code: 2 to 6 letters (A-Z) or digits."))
		if frappe.db.exists("Property", {"short_code": code,
		                                 "name": ("!=", self.name or "")}):
			frappe.throw(_("Short code {0} is already used by another property.").format(code))
		self.short_code = code

	def _validate_booking_language(self):
		# Empty follows the guest's browser. A value already stored stays
		# saveable after that language is switched off, so the rest of the
		# property can still be edited; choosing it again is refused.
		raw = self.booking_language or ""
		code = raw.strip().lower()
		changed = self.is_new() or self.has_value_changed("booking_language") or code != raw
		if code != raw:
			self.booking_language = code
		if code and changed:
			from kamra.languages import assert_enabled_language
			self.booking_language = assert_enabled_language(code)

	def after_insert(self):
		# A new property starts with allowances signed off by a Hotel Admin
		# (every amount) and rate-plan-required pricing on (OPERA Cloud Rate
		# Code Mandatory). Existing properties keep the registry defaults
		# (Off / 0), so an upgrade changes nothing, and seeding runs once on
		# insert so later admin toggles are never overwritten.
		from kamra.settings import clear_cache, write_kv
		write_kv(self.name, "allowance_approval", "Hotel Admin")
		write_kv(self.name, "allowance_approval_above", 0)
		if not frappe.db.exists(
			"Property Setting",
			{"property": self.name, "setting_key": "rate_plan_required"},
		):
			write_kv(self.name, "rate_plan_required", 1)
		clear_cache(self.name)

	def on_update(self):
		# Single-site tenants: keep Frappe's site clock aligned with the
		# property so night audit / now_datetime follow hotel local time.
		# Multi-property sites with different zones should leave System
		# Settings alone (only sync when this is the sole property).
		tz = (self.timezone or "").strip()
		if not tz or not self.has_value_changed("timezone"):
			return
		if frappe.db.count("Property") > 1:
			return
		current = frappe.db.get_single_value("System Settings", "time_zone")
		if current == tz:
			return
		frappe.db.set_single_value("System Settings", "time_zone", tz)
		frappe.clear_cache()
