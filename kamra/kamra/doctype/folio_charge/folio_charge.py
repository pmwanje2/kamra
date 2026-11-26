# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from kamra.folio import sales_invoice_field
from kamra.kamra.doctype.folio.folio import charges_frozen


class FolioCharge(Document):
	def before_save(self):
		# Frappe runs before_save for native inserts and updates. For a move,
		# check both owners so changing parent cannot detach a billed charge.
		old = self.get_doc_before_save()
		parents = {self.parent}
		if old:
			parents.add(old.parent)
		for parent in sorted(name for name in parents if name):
			self.check_parent(parent)

	def on_trash(self):
		self.check_parent(self.parent)

	def check_parent(self, name):
		parent = frappe.db.get_value("Folio", name, ["status", "invoice_number", *sales_invoice_field()],
		                             as_dict=True, for_update=True)
		if charges_frozen(parent):
			frappe.throw(_("This folio has been invoiced. Post charges and adjustments on a new billing window."))
