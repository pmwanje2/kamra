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
		a plan that names none takes the list's first.

		Setting a plan's category (a new plan in another category than the
		first, or a change) is the configuration privilege's
		(room_statistics.can_configure: OPERA keeps rate code configuration
		apart from comp authorization). Whoever set it is kept on the plan
		(rate_category_set_by, written here only) and never signs off a
		booking on it; the change is logged by on_update. A change reaches no
		stay already booked (Reservation.rate_category)."""
		from kamra.room_statistics import can_configure, category_rules
		rules = category_rules(self.property)
		category = rules.canonical(self.get("rate_category"))
		if category is None:
			frappe.throw(_("{0} is not one of this property's rate categories: {1}.").format(
				self.get("rate_category"), ", ".join(_(c) for c in rules.categories)),
				title=_("Rate category"))
		self.rate_category = category
		old = None if self.is_new() else self.get_doc_before_save()
		was = rules.default if old is None else (
			rules.canonical(old.get("rate_category")) or old.get("rate_category"))
		self.rate_category_set_by = old.get("rate_category_set_by") if old is not None else None
		if category == was:
			return
		if not can_configure():
			frappe.throw(_("Only the property's administrators change a rate plan's category."),
			             frappe.PermissionError, title=_("Rate category"))
		self.rate_category_set_by = frappe.session.user
		self.flags.kamra_rate_category_change = (was, category)

	def on_update(self):
		change = self.flags.pop("kamra_rate_category_change", None)
		if not change:
			return
		from kamra.savings import log_action
		log_action("rate_category_changed", "Rate Plan", self.name, self.property,
		           rationale=f"{self.rate_plan_name or self.name}: {change[0]} → {change[1]}",
		           before_snapshot={"rate_category": change[0]},
		           after_snapshot={"rate_category": change[1]})
