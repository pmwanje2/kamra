# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class RatePlan(Document):
	def validate(self):
		self.validate_rate_category()

	def validate_rate_category(self):
		"""The plan's category is one of its property's rate categories
		(kamra.room_statistics.category_rules), stored as the list spells it;
		a plan that names none takes the list's first."""
		from kamra.room_statistics import category_rules
		rules = category_rules(self.property)
		category = rules.canonical(self.get("rate_category"))
		if category is None:
			frappe.throw(_("{0} is not one of this property's rate categories: {1}.").format(
				self.get("rate_category"), ", ".join(_(c) for c in rules.categories)),
				title=_("Rate category"))
		self.rate_category = category
