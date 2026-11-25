from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, getdate

from kamra.authz import assert_property_access


def make_scope_key(
	property: str,
	restriction_date,
	room_type: str | None = None,
	rate_plan: str | None = None,
) -> str:
	return f"{property}|{getdate(restriction_date)}|{room_type or '*'}|{rate_plan or '*'}"


class RateRestriction(Document):
	def validate(self):
		if self.property:
			assert_property_access(self.property)
		if not self.restriction_date:
			frappe.throw(_("Restriction date is required."))
		self.restriction_date = str(getdate(self.restriction_date))
		if self.room_type:
			rt_prop = frappe.db.get_value("Room Type", self.room_type, "property")
			if rt_prop and rt_prop != self.property:
				frappe.throw(_("Room type {0} does not belong to property {1}.").format(
					self.room_type, self.property))
		if self.rate_plan:
			rp_prop = frappe.db.get_value("Rate Plan", self.rate_plan, "property")
			if rp_prop and rp_prop != self.property:
				frappe.throw(_("Rate plan {0} does not belong to property {1}.").format(
					self.rate_plan, self.property))

		min_los = cint(self.min_los)
		max_los = cint(self.max_los)
		if min_los < 0 or max_los < 0:
			frappe.throw(_("Minimum and maximum length of stay cannot be negative."))
		if min_los > 0 and max_los > 0 and max_los < min_los:
			frappe.throw(_("Maximum length of stay ({0}) cannot be less than minimum length of stay ({1}).").format(
				max_los, min_los))

		self.min_los = min_los
		self.max_los = max_los
		self.stop_sell = cint(self.stop_sell)
		self.is_stay_through = cint(self.is_stay_through)
		self.cta = cint(self.cta)
		self.ctd = cint(self.ctd)
		self.scope_key = make_scope_key(
			self.property, self.restriction_date, self.room_type or None, self.rate_plan or None,
		)
		self.updated_by_user = getattr(frappe.session, "user", None) or self.updated_by_user

	def _notify_ari(self, method: str) -> None:
		if self.flags.get("skip_ari_enqueue"):
			return
		from kamra.channel_ari import on_ari_source_change
		on_ari_source_change(self, method)

	def after_insert(self):
		self._notify_ari("after_insert")

	def on_update(self):
		if self.flags.in_insert:
			return
		self._notify_ari("on_update")

	def on_trash(self):
		if self.property:
			assert_property_access(self.property)
		self._notify_ari("on_trash")
