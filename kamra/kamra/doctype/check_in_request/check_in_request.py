# Copyright (c) 2026, Kamra and contributors
# For license information, please see license.txt

import frappe
from frappe import _

from kamra.masters import MasterCode


class CheckinRequest(MasterCode):
	def validate(self):
		super().validate()
		self.charge_type = (self.charge_type or "").strip() or None
		if self.charge_type:
			options = (frappe.get_meta("Folio Charge").get_field("charge_type").options or "").split("\n")
			if self.charge_type not in options:
				frappe.throw(_("Charge as: pick one of {0}.").format(", ".join(o for o in options if o)))
