"""A shared advance bill and a cashier at another property can post concurrently."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from kamra import api, folio, ledger, shares
from kamra.tests.audit_fixtures import TODAY, d, make_guest, make_property, make_reservation, make_room, make_room_type
from kamra.tests.test_advance_3dp_precision_r12 import counts, receipt_fixture
from kamra.tests.test_invoice_corrections import _second_connection


def cashier_receipt(site, name):
	frappe.init(site=site)
	try:
		frappe.connect()
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- independent test cashier
		frappe.db.sql("SET SESSION innodb_snapshot_isolation=1, innodb_lock_wait_timeout=2")
		frappe.db.get_value("Folio", name, "name", for_update=True)
		api.add_folio_payment(name, "Cash", 1, idempotency_key="R18 cashier")
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- independent cashier finished before the bill commits
		return counts(name)
	finally:
		frappe.destroy()


class TestIndependentShareBill(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.other = self.enterContext(receipt_fixture("USD"))[1]
		prop = make_property("UT R18 " + frappe.generate_hash(length=10), business_date=TODAY, currency="USD")
		rt = make_room_type(prop, base_price=100, tax_percent=0)
		self.stay = make_reservation(prop, rt, TODAY, d(3), room=make_room(prop, rt, "101"))
		self.guest = make_guest("R18 peer")
		ledger.ensure_default_transaction_codes()
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- publish isolated cross-connection fixtures
		old = frappe.db.sql("SELECT @@innodb_snapshot_isolation, @@innodb_lock_wait_timeout")[0]
		self.addCleanup(frappe.db.sql, "SET SESSION innodb_snapshot_isolation=%s, innodb_lock_wait_timeout=%s", old)
		frappe.db.sql("SET SESSION innodb_snapshot_isolation=1, innodb_lock_wait_timeout=2")
		self.assertEqual(frappe.db.sql("SELECT @@tx_isolation")[0][0], "REPEATABLE-READ")

	def assert_other_receipt(self):
		with ThreadPoolExecutor(1) as worker:
			self.assertEqual(worker.submit(cashier_receipt, frappe.local.site, self.other).result(timeout=15), (1, 1, 1))
		self.assertEqual(frappe.db.sql("SELECT @@in_transaction")[0][0], 1)

	def test_guest_window_explain(self):
		shares.create_share_reservation(self.stay.name, self.guest)
		plan = frappe.db.sql("""
			EXPLAIN SELECT name, share_prearrival FROM `tabFolio`
			WHERE reservation=%s AND folio_type='Guest'
			ORDER BY window_no ASC, status DESC, creation DESC LIMIT 1 FOR UPDATE
		""", self.stay.name, as_dict=True)[0]
		self.assertIn(plan.type, ("ref", "const", "range"), plan)
		self.assertNotIn("filesort", (plan.Extra or "").lower(), plan)
		parent = frappe.db.sql("EXPLAIN SELECT * FROM `tabFolio` WHERE name=%s FOR UPDATE",
		                       self.other, as_dict=True)[0]
		self.assertEqual((parent.type, parent.key), ("const", "PRIMARY"))

	def test_advance_bill_allows_another_property_receipt(self):
		peer = shares.create_share_reservation(self.stay.name, self.guest)["reservation"]
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- isolate advance bill locks from first join
		self.assertEqual(ledger.force_advance_bill(self.stay.name, "tonight")["balance"], 50)
		self.assert_other_receipt()
		self.assertEqual(ledger.force_advance_bill(peer, "tonight")["balance"], 50)

	def test_current_folio_children_do_not_lock_another_cashiers_payment(self):
		self.assertFalse(frappe.db.get_value("Folio", {"reservation": self.stay.name}, "name"))
		joiner = _second_connection()
		try:
			with patch.object(frappe.local, "db", joiner):
				peer = shares.create_share_reservation(self.stay.name, self.guest)["reservation"]
				name = frappe.db.get_value("Folio", {"reservation": self.stay.name}, "name")
				bill = frappe.get_doc("Folio", name)
				charge = bill.append("charges", {"charge_type": "Room", "posting_date": TODAY, "qty": 1,
				                                 "rate": 7, "amount": 7, "description": "Prior charge"})
				payment = bill.append("payments", {"posting_date": TODAY, "payment_kind": "Payment", "mode": "Cash", "amount": 7})
				folio._recalculate(bill)
				bill.save()
				joiner.commit()
		finally:
			joiner.close()
		with shares.current_inventory_reads():
			current = frappe.get_doc("Reservation", self.stay.name, for_update=True)
			token = folio.advance_share_targets.set(set())
			try:
				loaded = folio._posting_folio(current, name)
			finally:
				folio.advance_share_targets.reset(token)
		self.assertEqual([row.name for row in loaded.charges], [charge.name])
		self.assertEqual([row.name for row in loaded.payments], [payment.name])
		self.assert_other_receipt()
		self.assertEqual([ledger.force_advance_bill(r, "tonight")["balance"] for r in (current.name, peer)], [50, 50])
