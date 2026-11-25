# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import add_to_date, get_datetime, now_datetime

# priority -> minutes allowed before the clean is overdue. Defaults - each
# property sets its own in Settings -> Housekeeping (hk_sla_<priority>).
HK_SLA_MINUTES = {"Urgent": 20, "High": 45, "Medium": 90, "Low": 180}


def sla_minutes(property: str | None, priority: str | None) -> int:
	from kamra.settings import get_setting
	p = priority if priority in HK_SLA_MINUTES else "Medium"
	return int(get_setting(property, f"hk_sla_{p.lower()}", HK_SLA_MINUTES[p]))


class HousekeepingTask(Document):
	def validate(self):
		if frappe.db.get_value("Room", self.room, "property") != self.property:
			frappe.throw(frappe._("The task room must belong to this property."), frappe.PermissionError)
		if self.reservation and frappe.db.get_value("Reservation", self.reservation, "property") != self.property:
			frappe.throw(frappe._("The task reservation must belong to this property."), frappe.PermissionError)
		from kamra.housekeeping import (
			OPEN_HOUSEKEEPING_STATUSES, assert_turndown_actionable, snapshot_assignment_credits,
		)
		previous = self.get_doc_before_save()
		if self.status in OPEN_HOUSEKEEPING_STATUSES or (
				self.status in ("Done", "Verified") and (not previous or previous.status != self.status)):
			assert_turndown_actionable(self)
		snapshot_assignment_credits(self, previous)

	def before_insert(self):
		if not self.business_date:
			from kamra.business_date import get_business_date
			self.business_date = get_business_date(self.property)
		if not self.due_by:
			self.due_by = add_to_date(
				now_datetime(),
				minutes=sla_minutes(self.property, self.priority),
			)

	def on_update(self):
		previous = self.get_doc_before_save()
		old_status = previous.status if previous else None
		if self.status == old_status:
			return
		if self.task_type == "Turndown":
			if self.status in ("Done", "Verified") and str(self.business_date) == str(
					frappe.db.get_value("Room", self.room, "turndown_business_date")):
				frappe.db.set_value("Room", self.room, "turndown_status", "Completed")
			return
		# Completed cleans flow back into the room's live status so the
		# front desk (and any agent checking availability) sees readiness.
		if self.status == "Done" and self.task_type == "Inspection":
			# an inspection never sets a room back to plain Clean: it is
			# passed by Verified (below); a Done here just closes the task
			if self.due_by and now_datetime() > get_datetime(self.due_by):
				self.db_set("breached", 1, update_modified=False)
		elif self.status == "Done":
			frappe.db.set_value("Room", self.room, "housekeeping_status", "Clean")
			if self.due_by and now_datetime() > get_datetime(self.due_by):
				self.db_set("breached", 1, update_modified=False)
			from kamra.housekeeping import notify_room_ready
			notify_room_ready(self)
			try:
				from kamra.turnover import on_checkout_clean_done
				on_checkout_clean_done(self)
			except Exception:
				frappe.log_error(title="Turnover inspection spawn failed")
		elif self.status == "Verified":
			# Inspection verified → Ready (STR) or Inspected (hotel default).
			profile = None
			try:
				from kamra.turnover import get_turnover_profile, mark_room_ready
				profile = get_turnover_profile(self.property)
			except Exception:
				profile = None
			if profile and profile.get("requires_inspection"):
				mark_room_ready(self.room)
			else:
				frappe.db.set_value(
					"Room", self.room, "housekeeping_status", "Inspected"
				)
