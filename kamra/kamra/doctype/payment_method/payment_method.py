# Copyright (c) 2026, Kamra and contributors
# For license information, please see license.txt

from kamra.masters import MasterCode


class PaymentMethod(MasterCode):
	def validate(self):
		super().validate()
		if self.system:
			self.property = None
