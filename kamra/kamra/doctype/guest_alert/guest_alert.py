# Copyright (c) 2026, Contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class GuestAlert(Document):
	def validate(self):
		self.message = (self.message or "").strip()[:500]
		if not self.message:
			frappe.throw(_("Alert message is required."))
		if not self.reservation and not self.guest:
			frappe.throw(_("Select a reservation or a guest for this alert."))
		if self.reservation:
			res = frappe.db.get_value(
				"Reservation",
				self.reservation,
				["property", "guest"],
				as_dict=True,
			)
			if not res:
				frappe.throw(_("Reservation {0} not found.").format(self.reservation))
			if self.property and res.property != self.property:
				frappe.throw(_("Reservation belongs to a different property."))
			self.property = res.property
			if not self.guest and res.guest:
				self.guest = res.guest
		if not self.scope:
			self.scope = "Reservation" if self.reservation else "Guest"
		if not self.display_mode:
			self.display_mode = "Until Resolved" if self.severity == "Blocking" else "Every Access"
