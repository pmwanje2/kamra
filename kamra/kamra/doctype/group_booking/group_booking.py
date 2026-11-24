# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class GroupBooking(Document):
	def before_insert(self):
		# a block always has a release date: unpicked rooms go back on sale
		# at the cutoff (kamra.group_blocks), 7 days before arrival unless
		# the group negotiated otherwise
		if not self.cutoff_date and self.check_in_date:
			from kamra.group_blocks import default_cutoff
			self.cutoff_date = default_cutoff(self.check_in_date)

	def validate(self):
		# the block rate is billed (Reservation.agreed_rate), so setting or
		# changing it is a rate decision on every write path, REST included
		before = self.get_doc_before_save()
		old = {b.room_type: float(b.block_rate or 0) for b in (before.blocks if before else [])}
		if any(float(b.block_rate or 0) != old.get(b.room_type, 0) for b in self.blocks):
			from kamra.api import _can_set_rate
			if not _can_set_rate():
				import frappe
				frappe.throw(frappe._("Only a Revenue Manager or Hotel Admin can change a group rate."),
				             frappe.PermissionError)
		if self.property:
			import frappe
			from kamra.pricing import resolve_group_rate_plan
			from kamra.settings import get_setting

			enforce = bool(get_setting(self.property, "rate_plan_required"))
			new_plan = (self.get("rate_plan") or "").strip() or None
			if self.is_new():
				if new_plan or enforce:
					resolved = resolve_group_rate_plan(
						self.property, rate_plan=new_plan, company=self.get("company"), required=enforce,
					)
					if resolved:
						self.rate_plan = resolved
			else:
				old_plan = ((before.get("rate_plan") or "").strip() or None) if before else None
				if new_plan != old_plan:
					if old_plan and not new_plan and enforce:
						frappe.throw(frappe._(
							"A rate plan is required for this property and cannot be cleared from an existing group booking."
						))
					elif new_plan:
						resolve_group_rate_plan(
							self.property, rate_plan=new_plan, company=self.get("company"), required=enforce,
						)
