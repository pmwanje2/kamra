# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class RatePlan(Document):
	def validate(self):
		if flt(self.rounding_step) < 0:
			frappe.throw(_("The rounding step cannot be negative."))
		self.validate_base_rate_plan()

	def validate_base_rate_plan(self):
		"""A derived plan (OPERA Cloud Derived Rates) prices from a base plan
		at the same property, in the base plan's currency, without a loop."""
		if not self.base_rate_plan:
			return
		from kamra.pricing import rate_plan_currency, resolve_rate_plan_chain

		chain = resolve_rate_plan_chain(self)
		if chain[-2].property != self.property:
			frappe.throw(_("The base rate plan must belong to the same property."))
		base_currency = rate_plan_currency(chain[:-1])
		if self.currency and self.currency != base_currency:
			frappe.throw(_("A derived rate plan must use its base plan's currency ({0}).").format(base_currency))
