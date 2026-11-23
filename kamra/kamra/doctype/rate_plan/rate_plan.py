# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


class RatePlan(Document):
	def validate(self):
		if flt(self.rounding_step) < 0:
			frappe.throw(_("The rounding step cannot be negative."))
		self.validate_base_rate_plan()
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
		if self.currency and self.currency != base_currency:
			frappe.throw(_("A derived rate plan must use its base plan's currency ({0}).").format(base_currency))

	def validate_derived_plans(self):
		"""The plans derived from this one (and from them) price in its
		currency and need it enabled: a currency their own currency no
		longer matches, or disabling it under an enabled derived plan, is
		refused."""
		if not self.name:
			return
		from kamra.pricing import RATE_PLAN_CHAIN_LIMIT, rate_plan_currency, resolve_rate_plan_chain

		currency = None
		bases, seen = [self.name], {self.name}
		for _depth in range(RATE_PLAN_CHAIN_LIMIT):
			children = [child for base in bases for child in frappe.get_all(
				"Rate Plan", filters={"base_rate_plan": base},
				fields=["name", "rate_plan_name", "currency", "disabled"]) if child.name not in seen]
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
			seen.update(child.name for child in children)
			bases = [child.name for child in children if not child.currency]
