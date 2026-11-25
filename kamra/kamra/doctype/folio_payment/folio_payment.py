# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class FolioPayment(Document):
	def precision(self, fieldname, parentfield=None):
		if fieldname == "amount":
			from kamra.ledger import _currency_places
			from kamra.localization import _minor_unit_for_currency

			if self.parent and self.parenttype == "Folio":
				property_name = frappe.db.get_value("Folio", self.parent, "property")
				if property_name:
					return _currency_places(property_name)
			if self.currency and not self.fx_amount:
				return _minor_unit_for_currency(self.currency)
		return super().precision(fieldname, parentfield)
