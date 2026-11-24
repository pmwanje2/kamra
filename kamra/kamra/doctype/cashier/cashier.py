# Copyright (c) 2026, Kamra and contributors
# For license information, please see license.txt

from __future__ import annotations

from frappe.model.document import Document


class Cashier(Document):
	def validate(self):
		from kamra.cashier import parse_allowed_modes
		from kamra.masters import assert_known_payment_method

		modes = parse_allowed_modes(self.allowed_modes)
		if modes is None:
			self.allowed_modes = ""
			return
		for mode in modes:
			assert_known_payment_method(self.property, mode)
		self.allowed_modes = ", ".join(modes)
