# Copyright (c) 2026, Kamra and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class FolioLedgerEntry(Document):
	def _is_payment_reversal(self):
		return self.ledger == "Deposit" and self.reversal_of and self.source_doctype == "Folio Payment"

	def before_insert(self):
		if self._is_payment_reversal():
			from kamra.folio import lock_guest_deposit_windows
			lock_guest_deposit_windows(frappe.db.get_value("Folio", self.folio, "reservation"))

	def after_insert(self):
		# A native cancellation can save its Refund row before the ledger
		# reversal supplies the link to the original security deposit.
		if not self._is_payment_reversal():
			return
		from kamra.folio import _payments_total, sync_deposit_payment_totals
		from kamra.ledger import _money

		folio = frappe.get_doc("Folio", self.folio, for_update=True)
		paid = _payments_total(folio)
		balance = _money(folio.grand_total) - paid
		if (paid, balance) != (_money(folio.payments_total), _money(folio.balance)):
			frappe.db.set_value("Folio", folio.name, {"payments_total": paid, "balance": balance})
		sync_deposit_payment_totals(folio)
