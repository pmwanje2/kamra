# Copyright (c) 2026, Kamra and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class FolioReprint(Document):
	def as_dict(self, *args, **kwargs):
		# Some native write endpoints serialize the document without applying
		# field permissions. Only the verified revision reader exposes this snapshot.
		row = super().as_dict(*args, **kwargs)
		row.pop("invoice_number", None)
		return row
