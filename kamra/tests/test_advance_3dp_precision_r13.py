"""Receipt transactions serialize on existing parent records, including on empty tills."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from kamra import api, cashier, folio
from kamra.tests.test_advance_3dp_precision_r11 import CASES
from kamra.tests.test_advance_3dp_precision_r12 import counts, receipt_fixture


@contextmanager
def statements():
	queries = []
	execute = frappe.db.sql

	def trace(query, *args, **kwargs):
		queries.append(" ".join(str(query).upper().replace("`", "").split()))
		return execute(query, *args, **kwargs)

	with patch.object(frappe.db, "sql", side_effect=trace):
		yield queries


def post_receipt(site, room, name, amount, key, ready=None):
	frappe.init(site=site)
	try:
		frappe.connect()
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- independent test cashier
		frappe.db.sql("SET SESSION innodb_snapshot_isolation=1")
		frappe.db.sql("SET SESSION innodb_lock_wait_timeout=2")
		frappe.db.set_value("Room", room, "notes", name)
		frappe.db.savepoint("caller")
		if ready is not None:
			ready.wait(timeout=20)
		result = api.add_folio_payment(name, "Cash", amount, idempotency_key=key)
		frappe.db.release_savepoint("caller")
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- independently committed test receipt
		return result.get("idempotent_replay", 0)
	finally:
		frappe.destroy()


class TestReceiptParentLocks(IntegrationTestCase):
	def test_first_receipts_on_independent_tills(self):
		for currency, _, _, amount, _ in CASES:
			for keyed in (False, True):
				with self.subTest(currency=currency, keyed=keyed), receipt_fixture(currency) as first, receipt_fixture(currency) as second:
					frappe.db.sql("SET SESSION innodb_snapshot_isolation=1")
					key = "first" if keyed else None
					# Check a single real posting before running the two writers together.
					frappe.db.savepoint("inspect_post")
					with statements() as queries:
						api.add_folio_payment(first[1], "Cash", amount, idempotency_key=key)
					frappe.db.rollback(save_point="inspect_post")
					self.assertFalse([q for q in queries if "FOR UPDATE" in q and any(
						t in q for t in ("TABFOLIO PAYMENT ", "TABFOLIO CHARGE "))])
					frappe.db.commit()  # nosemgrep: frappe-manual-commit -- release inspection locks
					ready = Barrier(2)
					with ThreadPoolExecutor(2) as workers:
						calls = [workers.submit(post_receipt, frappe.local.site, room, name, amount, key, ready)
						         for room, name, _ in (first, second)]
						self.assertEqual([task.result(timeout=30) for task in calls], [0, 0])
					for room, name, _ in (first, second):
						self.assertEqual(counts(name), (1, 1, 1))
						self.assertEqual(frappe.db.get_value("Room", room, "notes"), name)

	def test_an_open_till_lock_is_local_to_its_session(self):
		for currency, _, _, amount, _ in CASES:
			with self.subTest(currency=currency), receipt_fixture(currency) as first, receipt_fixture(currency) as second:
				frappe.db.sql("SET SESSION innodb_snapshot_isolation=1")
				for name in (first[1], second[1]):
					api.add_folio_payment(name, "Cash", amount, idempotency_key="opening")
				frappe.db.commit()  # nosemgrep: frappe-manual-commit -- publish both test journals
				session = cashier.require_open_session(frappe.db.get_value("Folio", first[1], "property"))
				with statements() as queries:
					folio.lock_receipt_till(session)
				self.assertTrue(any("TABCASHIER SESSION" in q and "FOR UPDATE" in q for q in queries))
				self.assertFalse([q for q in queries if "TABCASHIER TRANSACTION" in q and "FOR UPDATE" in q])
				with ThreadPoolExecutor(1) as worker:
					result = worker.submit(post_receipt, frappe.local.site, second[0], second[1], amount, "second").result(timeout=20)
				self.assertFalse(result)
				frappe.db.rollback()
				self.assertEqual(counts(first[1]), (1, 1, 1))
				self.assertEqual(counts(second[1]), (2, 2, 2))
