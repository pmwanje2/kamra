# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class BanquetRevenueMapping(Document):
	def validate(self):
		code = frappe.db.get_value(
			"Transaction Code", self.transaction_code, ["txn_type", "property", "disabled"], as_dict=True
		)
		if not code or code.txn_type != "Revenue" or code.disabled:
			frappe.throw(_("Map a function charge to an enabled revenue transaction code."))
		if code.property and code.property != self.property:
			frappe.throw(_("Transaction code {0} belongs to another property.").format(self.transaction_code))
		if self.venue and frappe.db.get_value("Venue", self.venue, "property") != self.property:
			frappe.throw(_("Hall {0} belongs to another property.").format(self.venue))
