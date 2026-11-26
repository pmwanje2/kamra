"""Payment savepoints, receipt serialization and native Deposit reversal provenance."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.model.base_document import BaseDocument
from frappe.tests import IntegrationTestCase

from kamra import api, cashier, folio, ledger, payments
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, make_property, make_reservation, make_room, make_room_type,
)


CASES = (("KWD", 25.375, 5.125, 1.125, 1), ("USD", 25.37, 5.12, 1.12, 1),
         ("UGX", 25375, 5125, 1125, 1000))


def standalone(prop):
	return frappe.get_doc({"doctype": "Folio", "property": prop, "folio_type": "Account"}).insert()


class TestPaymentAccountingR11(AuditTestCase):
	properties = tuple("UT R11 " + case[0] for case in CASES)

	def test_failed_link_is_replayable_in_every_context(self):
		for currency, _, _, amount, _ in CASES:
			prop = make_property("UT R11 " + currency, currency=currency)
			room_type = make_room_type(prop)
			for number, status in enumerate(("Standalone", "Confirmed", "Checked In"), 1):
				with self.subTest(currency=currency, status=status):
					res = None
					if status == "Standalone":
						name = standalone(prop).name
					else:
						res = make_reservation(prop, room_type, TODAY, d(1), status=status,
						                       room=make_room(prop, room_type, str(number)))
						name = folio.open_folio(res)
					key = "link-" + name

					def state():
						return (
							frappe.db.get_value("Folio", name, "balance"),
							frappe.db.count("Folio Payment", {"parent": name}),
							frappe.db.count("Folio Ledger Entry", {"folio": name}),
							frappe.db.count("Cashier Transaction", {"property": prop}),
							frappe.db.get_value("Reservation", res.name, "advance_paid") if res else None,
						)

					before = state()
					with patch.object(ledger, "record_payment_ledger", side_effect=RuntimeError("ledger unavailable")):
						with self.assertRaisesRegex(RuntimeError, "ledger unavailable"):
							payments.settle_payment_link(name, key, amount)
					self.assertEqual(state(), before)
					self.assertTrue(payments.settle_payment_link(name, key, amount))
					settled = state()
					self.assertEqual(settled[1:3], (before[1] + 1, before[2] + 1))
					self.assertFalse(payments.settle_payment_link(name, key, amount))
					self.assertEqual(state(), settled)

	def test_savepoint_preserves_only_caller_notifications(self):
		for currency, _, _, amount, _ in CASES:
			with self.subTest(currency=currency):
				prop = make_property("UT R11 " + currency, currency=currency)
				res = make_reservation(prop, make_room_type(prop), TODAY, d(1))
				name = folio.open_folio(res)
				delivered = []
				frappe.db.after_commit.add(lambda: delivered.append("caller"))
				before = list(frappe.local._realtime_log or ())

				def fail(*args, **kwargs):
					frappe.get_doc({"doctype": "Kamra Notification", "property": prop,
					                "module": "finance", "subject": name}).insert()
					frappe.db.after_commit.add(lambda: delivered.append("rolled back"))
					raise RuntimeError("cancel posting")

				with patch.object(ledger, "record_payment_ledger", side_effect=fail):
					with self.assertRaisesRegex(RuntimeError, "cancel posting"):
						payments.settle_payment_link(name, "link-" + name, amount)
				self.assertFalse(frappe.db.exists("Kamra Notification", {"subject": name}))
				after = list(frappe.local._realtime_log or ())
				# Dispatch the same queue that an outer commit runs, without committing test fixtures.
				with patch("frappe.realtime.emit_via_redis"):
					frappe.db.after_commit.run()
				self.assertEqual((after, delivered), (before, ["caller"]))

	def test_nested_deposit_reversals_never_credit_the_guest(self):
		for currency, collected, returned, _, unit in CASES:
			with self.subTest(currency=currency):
				prop = make_property("UT R11 " + currency, currency=currency)
				doc = standalone(prop)

				def post(kind, amount, parent=None):
					doc.reload()
					doc.append("payments", {"payment_kind": kind, "mode": "Cash",
					                        "posting_date": TODAY, "amount": amount})
					folio._recalculate(doc)
					doc.save()
					return ledger.write_ledger_entry(
						property=prop, folio=doc.name, ledger="Deposit",
						debit=max(0, -amount), credit=max(0, amount), reversal_of=parent,
						source_doctype="Folio Payment", source_name=doc.payments[-1].name,
					)

				receipt = post("Security Deposit", collected)
				post("Refund", -returned, receipt)
				parent = post("Refund", -unit, receipt)
				net = Decimal(str(collected)) - Decimal(str(returned)) - unit
				for amount in (unit, -unit, unit):
					parent = post("Payment" if amount > 0 else "Refund", amount, parent)
					net += amount
					doc.reload()
					self.assertEqual((Decimal(str(folio.security_deposit_held_on_folio(doc))),
					                  doc.payments_total, doc.balance), (net, 0, 0))


class TestStaleReceiptR11(IntegrationTestCase):
	def test_dirty_caller_retains_its_room_write_and_savepoint(self):
		site = frappe.local.site
		for currency, _, _, amount, _ in CASES:
			for reset_counter in (False, True):
				with self.subTest(currency=currency, reset_counter=reset_counter):
					prop = "UT R11 " + frappe.generate_hash(length=10)
					inserted = []
					insert = BaseDocument.db_insert

					def track(doc, *args, **kwargs):
						result = insert(doc, *args, **kwargs)
						inserted.append((doc.doctype, doc.name))
						return result

					def competing_receipt(name):
						frappe.init(site=site)
						try:
							frappe.connect()
							frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- independent test cashier
							api.add_folio_payment(name, "Cash", amount, idempotency_key="competing")
							frappe.db.commit()  # nosemgrep: frappe-manual-commit -- independent test transaction
						finally:
							frappe.destroy()

					try:
						with patch.object(BaseDocument, "db_insert", track):
							make_property(prop, currency=currency)
							room = make_room(prop, make_room_type(prop), "101")
							name = standalone(prop).name
							cashier.open_session(prop, 0)
							frappe.db.commit()  # nosemgrep: frappe-manual-commit -- publish worker fixture
							frappe.db.sql("SET SESSION innodb_snapshot_isolation = 1")
							frappe.db.set_value("Room", room, "notes", "caller edit")
							frappe.db.savepoint("r11_caller")
							self.assertEqual(frappe.db.get_value("Folio", name, "payments_total"), 0)
							if reset_counter:
								frappe.db.savepoint("r11_later")
								frappe.db.rollback(save_point="r11_later")
								self.assertEqual(frappe.db.transaction_writes, 0)
							with ThreadPoolExecutor(1) as worker:
								worker.submit(competing_receipt, name).result(timeout=20)
							with patch.object(frappe, "get_doc", wraps=frappe.get_doc) as get_doc:
								try:
									api.add_folio_payment(name, "Cash", amount, idempotency_key="caller")
								except (frappe.QueryDeadlockError, frappe.TimestampMismatchError):
									pass  # The caller owns this transaction and can retry after closing it.
							self.assertEqual(frappe.db.get_value("Room", room, "notes"), "caller edit")
							attempts = [c for c in get_doc.call_args_list if c.args == ("Folio", name)
							            and c.kwargs.get("for_update")]
							self.assertLessEqual(len(attempts), 1)
							frappe.db.rollback(save_point="r11_caller")
							self.assertEqual(frappe.db.get_value("Room", room, "notes"), "caller edit")
							frappe.db.commit()  # nosemgrep: frappe-manual-commit -- commit only caller's work
							self.assertFalse(frappe.db.exists("Folio Payment", {"parent": name, "idempotency_key": "caller"}))
							api.add_folio_payment(name, "Cash", amount, idempotency_key="caller")
							replay = api.add_folio_payment(name, "Cash", amount, idempotency_key="caller")
							self.assertTrue(replay["idempotent_replay"])
							self.assertEqual(frappe.db.count("Folio Ledger Entry", {"folio": name}), 2)
					finally:
						frappe.db.rollback()
						for doctype, name in reversed(inserted):
							frappe.db.delete(doctype, {"name": name})
						frappe.db.commit()  # nosemgrep: frappe-manual-commit -- remove committed test fixtures
						frappe.clear_document_cache("Property", prop)
