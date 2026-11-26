"""Transaction boundaries for direct receipts on a shared till."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, BrokenBarrierError
from unittest.mock import patch

import frappe
from frappe.model.base_document import BaseDocument
from frappe.tests import IntegrationTestCase

from kamra import api, cashier, folio, ledger
from kamra.tests.audit_fixtures import make_property, make_room, make_room_type
from kamra.tests.test_advance_3dp_precision_r11 import CASES, standalone


@contextmanager
def receipt_fixture(currency):
	# Independent connections must see these rows, including the open till.
	inserted = []
	insert = BaseDocument.db_insert
	prop = "UT R12 " + frappe.generate_hash(length=10)

	def track(doc, *args, **kwargs):
		result = insert(doc, *args, **kwargs)
		inserted.append((doc.doctype, doc.name))
		return result

	try:
		with patch.object(BaseDocument, "db_insert", track):
			make_property(prop, currency=currency)
			room = make_room(prop, make_room_type(prop), "101")
			first, second = standalone(prop).name, standalone(prop).name
			cashier.open_session(prop, 0)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- publish the test fixture
			yield room, first, second
	finally:
		frappe.db.rollback()
		for doctype, name in reversed(inserted):
			frappe.db.delete(doctype, {"name": name})
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- remove this test's committed rows
		frappe.clear_document_cache("Property", prop)


def receive(site, name, amount, key, reuse=False):
	frappe.init(site=site)
	try:
		frappe.connect()
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- independent test cashier
		frappe.db.sql("SET SESSION innodb_snapshot_isolation=1")
		if reuse:
			frappe.db.savepoint("previous_transaction")
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- end the earlier transaction
		result = api.add_folio_payment(name, "Cash", amount, idempotency_key=key)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- independent test receipt
		return result
	finally:
		frappe.destroy()


def counts(name):
	return tuple(frappe.db.count(dt, {field: name}) for dt, field in (
		("Folio Payment", "parent"), ("Cashier Transaction", "folio"), ("Folio Ledger Entry", "folio"),
	))


class TestReceiptBoundariesR12(IntegrationTestCase):
	def test_stale_till_preserves_room_and_savepoint(self):
		for currency, _, _, amount, _ in CASES:
			with self.subTest(currency=currency), receipt_fixture(currency) as (room, first, second):
				frappe.db.sql("SET SESSION innodb_snapshot_isolation=1")
				frappe.db.set_value("Room", room, "notes", "caller edit")
				frappe.db.savepoint("caller")
				frappe.db.savepoint("later")
				frappe.db.rollback(save_point="later")
				frappe.db.get_value("Folio", first, "balance")
				with ThreadPoolExecutor(1) as worker:
					worker.submit(receive, frappe.local.site, second, amount, "other").result(timeout=20)
				try:
					api.add_folio_payment(first, "Cash", amount, idempotency_key="caller")
				except (frappe.TimestampMismatchError, frappe.QueryDeadlockError) as exc:
					conflict = exc
				else:
					self.fail("The stale till accepted a receipt")
				self.assertEqual(frappe.db.sql("SELECT @@in_transaction")[0][0], 1)
				self.assertIsInstance(conflict, frappe.TimestampMismatchError)
				self.assertEqual(frappe.db.get_value("Room", room, "notes"), "caller edit")
				frappe.db.rollback(save_point="caller")
				self.assertEqual(frappe.db.get_value("Room", room, "notes"), "caller edit")
				self.assertEqual(counts(first), (0, 0, 0))

	def test_failed_direct_receipt_can_be_retried_after_outer_commit(self):
		for currency, _, _, amount, _ in CASES:
			with self.subTest(currency=currency), receipt_fixture(currency) as (_, name, _other):
				callbacks = []
				frappe.db.after_commit.add(lambda: callbacks.append("caller"))

				def fail(*args, **kwargs):
					frappe.db.after_commit.add(lambda: callbacks.append("receipt"))
					raise RuntimeError("ledger unavailable")

				with patch.object(ledger, "record_payment_ledger", side_effect=fail) as posting:
					with self.assertRaises((RuntimeError, frappe.ValidationError)):
						api.add_folio_payment(name, "Cash", amount, idempotency_key="key")
					posting.assert_called_once()
				frappe.db.commit()  # nosemgrep: frappe-manual-commit -- caller handles the failure
				self.assertEqual((counts(name), frappe.db.get_value("Folio", name, "balance"), callbacks),
				                 ((0, 0, 0), 0, ["caller"]))
				first = api.add_folio_payment(name, "Cash", amount, idempotency_key="key")
				self.assertFalse(first.get("idempotent_replay"))
				self.assertTrue(api.add_folio_payment(name, "Cash", amount, idempotency_key="key")["idempotent_replay"])
				self.assertEqual(counts(name), (1, 1, 1))

	def test_snapshot_comparison_restores_session_on_success_and_sql_error(self):
		for currency, *_ in CASES:
			with receipt_fixture(currency) as (_, name, _other):
				for bad_column in (False, True):
					with self.subTest(currency=currency, sql_error=bad_column):
						frappe.db.sql("SET SESSION innodb_snapshot_isolation=1")
						execute = frappe.db.sql

						def compare(query, *args, **kwargs):
							if bad_column and str(query).rstrip().upper().endswith("FOR UPDATE"):
								query = query.replace("SELECT name FROM", "SELECT r12_missing_column FROM")
							return execute(query, *args, **kwargs)

						with patch.object(frappe.db, "sql", side_effect=compare):
							if bad_column:
								with self.assertRaises(Exception) as caught:
									folio._check_receipt_snapshot("SELECT name FROM `tabFolio` WHERE name=%s", name)
								self.assertEqual(caught.exception.args[0], 1054)
							else:
								folio._check_receipt_snapshot("SELECT name FROM `tabFolio` WHERE name=%s", name)
						self.assertEqual(execute("SELECT @@SESSION.innodb_snapshot_isolation")[0][0], 1)

	def test_concurrent_receipts_on_reused_connections(self):
		for currency, _, _, amount, _ in CASES:
			with self.subTest(currency=currency), receipt_fixture(currency) as (_, name, _other):
				at_pin = Barrier(2)
				guard = api._pin_guard

				def overlap(*args, **kwargs):
					guard(*args, **kwargs)
					try:
						at_pin.wait(timeout=3)
					except BrokenBarrierError:
						pass  # A writer may already hold the receipt lock.

				with patch.object(api, "_pin_guard", side_effect=overlap), ThreadPoolExecutor(2) as workers:
					tasks = [workers.submit(receive, frappe.local.site, name, amount, key, True)
					         for key in ("first", "second")]
					for task in tasks:
						self.assertFalse(task.result(timeout=20).get("idempotent_replay"))
				self.assertEqual(counts(name), (2, 2, 2))
