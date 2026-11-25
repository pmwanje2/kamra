"""PR 4 (P18 / K2) post-model-sync patch: ensure default revenue/beverage transaction codes exist."""

from __future__ import annotations

import frappe


def execute():
	from kamra.ledger import ensure_default_transaction_codes
	ensure_default_transaction_codes()
	frappe.clear_cache(doctype="Folio Payment")
