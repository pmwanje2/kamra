# Copyright (c) 2026, Kamra and contributors
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.utils import cint


class TransactionCode(Document):
	def validate(self):
		raw = self.default_tax_rate
		explicit_zero = (
			raw is not None
			and str(raw).strip() != ""
			and float(raw) == 0.0
		)
		if self.is_new():
			if explicit_zero and not cint(self.tax_exempt):
				self.tax_exempt = 1
			elif raw and float(raw) > 0:
				self.tax_exempt = 0
		elif cint(self.tax_exempt) and raw and float(raw) > 0:
			if self.has_value_changed("default_tax_rate") and not self.has_value_changed("tax_exempt"):
				self.tax_exempt = 0
			else:
				self.default_tax_rate = 0
		elif not cint(self.tax_exempt) and explicit_zero and self.has_value_changed("default_tax_rate"):
			self.tax_exempt = 1
