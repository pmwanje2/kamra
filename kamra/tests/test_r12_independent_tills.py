"""A receipt on an independent till preserves the caller's transaction."""
from concurrent.futures import ThreadPoolExecutor

import frappe
from frappe.tests import IntegrationTestCase

from kamra import api
from kamra.tests.test_advance_3dp_precision_r11 import CASES
from kamra.tests.test_advance_3dp_precision_r12 import counts, receipt_fixture, receive


class TestIndependentTills(IntegrationTestCase):
	def test_unrelated_journal_append_preserves_caller_savepoint(self):
		for currency, _, _, amount, _ in CASES:
			with self.subTest(currency=currency), receipt_fixture(currency) as (room, first, _), \
			     receipt_fixture(currency) as (_, other, _unused):
				for name in (first, other):
					api.add_folio_payment(name, "Cash", amount, idempotency_key="opening")
				frappe.db.commit()  # nosemgrep: frappe-manual-commit -- make both tills visible to the worker

				frappe.db.set_value("Room", room, "notes", "caller edit")
				frappe.db.savepoint("caller")
				frappe.db.get_value("Folio", first, "balance")
				with ThreadPoolExecutor(1) as worker:
					worker.submit(receive, frappe.local.site, other, amount, "other till").result(timeout=20)

				api.add_folio_payment(first, "Cash", amount, idempotency_key="mine")
				self.assertEqual(counts(first), (2, 2, 2))
				self.assertEqual(frappe.db.sql("SELECT @@in_transaction")[0][0], 1)
				frappe.db.rollback(save_point="caller")
				self.assertEqual(frappe.db.get_value("Room", room, "notes"), "caller edit")
				self.assertEqual(counts(first), (1, 1, 1))
