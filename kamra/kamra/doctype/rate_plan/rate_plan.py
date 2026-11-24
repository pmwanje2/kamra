# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


class RatePlan(Document):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		# Frappe's make_new_doc() populates any Link field named `currency` with
		# tabDefaultValue's global `currency` (e.g. INR) before REST/new_doc
		# kwargs are applied via `.update(kwargs)`. Clear that template default
		# on blank new-doc shells so omitted currency stays blank and takes the
		# base plan's, tariff Price List's, or property's currency in validate().
		if (
			self.get("__islocal")
			and not self.get("property")
			and not self.get("rate_plan_name")
			and not self.get("code")
			and not self.get("base_rate_plan")
		):
			self.currency = None

	def _set_defaults(self):
		# Rate Plan.currency has intentional blank semantics (linked tariff
		# Price List currency, base plan currency for derived plans, or property
		# currency). Never let Frappe's global tabDefaultValue `currency` fill it.
		if "currency" not in getattr(self, "dont_update_if_missing", ()):
			self.dont_update_if_missing = [*getattr(self, "dont_update_if_missing", ()), "currency"]
		super()._set_defaults()

	def validate(self):
		if flt(self.rounding_step) < 0:
			frappe.throw(_("The rounding step cannot be negative."))
		self.validate_base_rate_plan()
		self.validate_tariff_price_list()
		self.validate_derived_plans()

	def validate_base_rate_plan(self):
		"""A derived plan (OPERA Cloud Derived Rates) prices from a base plan
		at the same property, in the base plan's currency, without a loop."""
		if not self.base_rate_plan:
			return
		from kamra.pricing import rate_plan_currency, resolve_rate_plan_chain

		chain = resolve_rate_plan_chain(self)
		if chain[-2].property != self.property:
			frappe.throw(_("The base rate plan must belong to the same property."))
		if cint(chain[-2].get("disabled")):
			frappe.throw(_("The base rate plan {0} is disabled. Choose an enabled base plan.").format(
				chain[-2].get("rate_plan_name") or chain[-2].name))
		base_currency = rate_plan_currency(chain[:-1])
		is_new = self.is_new() if callable(getattr(self, "is_new", None)) else False
		reparented = (
			not is_new
			and callable(getattr(self, "has_value_changed", None))
			and self.has_value_changed("base_rate_plan")
			and not self.has_value_changed("currency")
		)
		if not self.currency or reparented:
			self.currency = base_currency
		elif self.currency != base_currency:
			frappe.throw(_("A derived rate plan must use its base plan's currency ({0}).").format(base_currency))

	def validate_tariff_price_list(self):
		"""A plan priced by a tariff price list (custom_tariff_price_list) is
		in that list's currency: one currency per rate code, as OPERA Cloud
		keeps it, so its badges and its derived plans never disagree with the
		tariff they convert."""
		price_list = self.get("custom_tariff_price_list")
		if not price_list or not frappe.db.exists("DocType", "Price List"):
			return
		list_currency = frappe.db.get_value("Price List", price_list, "currency")
		if not list_currency:
			return
		from kamra.pricing import rate_plan_currency, resolve_rate_plan_chain

		if not self.currency and not self.base_rate_plan:
			self.currency = list_currency
		currency = rate_plan_currency(resolve_rate_plan_chain(self))
		if currency != list_currency:
			frappe.throw(_("The tariff price list {0} is in {1}, so this rate plan must be in {1} too, not {2}. "
			               "Set the plan's currency to {1}, or choose a {2} price list.").format(
				price_list, list_currency, currency))

	def validate_derived_plans(self):
		"""The plans derived from this one (and from them) price in its
		currency and need it enabled: a currency their own currency, or the
		tariff price list of a plan that inherits it, no longer matches, or
		disabling it under an enabled derived plan, is refused."""
		if not self.name:
			return
		from kamra.pricing import RATE_PLAN_CHAIN_LIMIT, rate_plan_currency, resolve_rate_plan_chain

		currency = None
		lists = frappe.db.exists("DocType", "Price List") and frappe.get_meta("Rate Plan").has_field(
			"custom_tariff_price_list")
		fields = ["name", "rate_plan_name", "currency", "disabled", *(["custom_tariff_price_list"] if lists else [])]
		bases, seen = [self.name], {self.name}
		for _depth in range(RATE_PLAN_CHAIN_LIMIT):
			children = [child for base in bases for child in frappe.get_all(
				"Rate Plan", filters={"base_rate_plan": base}, fields=fields) if child.name not in seen]
			if not children:
				return
			if cint(self.disabled) and bases == [self.name] and any(not cint(child.disabled) for child in children):
				names = ", ".join(child.rate_plan_name or child.name for child in children if not cint(child.disabled))
				frappe.throw(_("Rate plans derive from this plan ({0}). Disable them or choose another base plan "
				               "for them first.").format(names))
			currency = currency or rate_plan_currency(resolve_rate_plan_chain(self))
			for child in children:
				if child.currency and child.currency != currency:
					frappe.throw(_("The derived rate plan {0} uses {1}; this plan's currency would be {2}. Change "
					               "the derived plan first.").format(child.rate_plan_name or child.name,
					                                                  child.currency, currency))
				price_list = None if child.currency else child.get("custom_tariff_price_list")
				list_currency = price_list and frappe.db.get_value("Price List", price_list, "currency")
				if list_currency and list_currency != currency:
					frappe.throw(_("The derived rate plan {0} uses the tariff price list {1} in {2}; this plan's "
					               "currency would be {3}. Change the derived plan first.").format(
						child.rate_plan_name or child.name, price_list, list_currency, currency))
			seen.update(child.name for child in children)
			bases = [child.name for child in children if not child.currency]
