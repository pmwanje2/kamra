# Copyright (c) 2026, Kamra and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class FolioLedgerEntry(Document):
	def after_insert(self):
		# A native cancellation can save its Refund row before the ledger
		# reversal supplies the link to the original security deposit.
		if self.ledger != "Deposit" or not self.reversal_of or self.source_doctype != "Folio Payment":
			return
		from kamra.folio import _payments_total
		from kamra.ledger import _money

		folio = frappe.get_doc("Folio", self.folio, for_update=True)
		paid = _payments_total(folio)
		balance = _money(folio.grand_total) - paid
		if (paid, balance) != (_money(folio.payments_total), _money(folio.balance)):
			frappe.db.set_value("Folio", folio.name, {"payments_total": paid, "balance": balance})
