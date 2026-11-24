# Copyright (c) 2026, Kamra and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CashierTransaction(Document):
	def validate(self):
		if self.mode:
			from kamra.masters import assert_known_payment_method
			assert_known_payment_method(self.property, self.mode)
