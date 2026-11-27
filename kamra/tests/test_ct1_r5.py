"""Reconciliation on the first read or deposit after upgrading a mixed stay."""
import frappe

from kamra import folio
from kamra.tests.audit_fixtures import AuditTestCase, d, make_property, make_reservation, make_room_type, post_legacy_advance


class MigratedAdvances(AuditTestCase):
	def setUp(self):
		super().setUp()
		self.prop = make_property("R5 Hotel")
		self.res = make_reservation(self.prop, make_room_type(self.prop), d(1), d(3))
		self.doc = frappe.get_doc("Folio", folio.open_folio(self.res))
		self.legacy = post_legacy_advance(self.doc, 100)
		self.typed = self.receipt("Advance", 25)

	def receipt(self, kind, amount, **fields):
		row = self.doc.append("payments", {
			"posting_date": d(0), "mode": "Cash", "payment_kind": kind, "amount": amount, **fields,
		})
		self.doc.save()
		return row.name

	def migrated_balance(self, amount):
		frappe.db.set_value("Reservation", self.res.name, {"advance_paid": amount, "legacy_advance_basis": None})

	def assert_replay(self, amount):
		for _ in range(2):
			folio.reconcile_reservation_advance(self.res.name, self.prop)
			self.assertEqual(self.res.reload().advance_paid, amount)

	def test_first_new_deposit_preserves_migrated_advance(self):
		self.migrated_balance(125)
		new = self.receipt("Advance", 10)
		self.assertEqual(self.res.reload().advance_paid, 135)
		self.assert_replay(135)
		self.receipt("Refund", -10, refund_of=new)
		self.assert_replay(125)
		self.receipt("Refund", -40, refund_of=self.legacy)
		self.assert_replay(85)

	def test_replay_before_next_deposit_with_prior_partial_refund(self):
		self.receipt("Refund", -5, refund_of=self.typed)
		self.migrated_balance(120)
		self.assert_replay(120)
		self.receipt("Advance", 10)
		self.assert_replay(130)
