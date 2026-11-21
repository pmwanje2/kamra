from unittest.mock import patch

import frappe

from kamra import api, cashier
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	guest_folio,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)


class CorrectionTests(AuditTestCase):
	def setUp(self):
		super().setUp()
		make_property(PROP_A, business_date=TODAY, require_cashier_pin=0)
		rt = make_room_type(PROP_A)
		self.res = make_reservation(
			PROP_A, rt, TODAY, d(2), status="Checked In", room=make_room(PROP_A, rt, "101")
		)
		self.folio = guest_folio(self.res.name)
		self.session = cashier.open_session(PROP_A, 0)["name"]
		self.row = api.add_folio_payment(self.folio, "Cash", 100)["payments"][-1]["name"]

	def supervisor(self):
		user = "k2-supervisor@example.test"
		frappe.get_doc(
			dict(
				doctype="User",
				email=user,
				first_name="Test",
				send_welcome_email=0,
				roles=[dict(role="Finance")],
			)
		).insert()
		frappe.get_doc(dict(doctype="Cashier PIN", user=user, pin="8394")).insert()
		return user

	def options(self, row=None):
		return api.payment_correction_options(self.folio, row or self.row)

	def submit(self, action, code, key, **kw):
		return api.correct_folio_payment(
			self.folio,
			self.row,
			action_type=action,
			reason_code=code,
			reason_note="Correction at counter",
			idempotency_key=key,
			**kw,
		)

	def test_reclassify_replays_once_and_keeps_drawer_and_balance(self):
		self.assertTrue(self.options()["can_reclassify"])
		before = frappe.db.get_value("Folio", self.folio, "balance")
		user = self.supervisor()
		for _ in range(2):
			self.submit(
				"reclassify_tender",
				"REV-ACCOUNT",
				"one",
				destination_mode="Card",
				supervisor_user=user,
				supervisor_pin="8394",
			)
		doc = frappe.get_doc("Folio", self.folio)
		self.assertEqual(doc.balance, before)
		self.assertEqual(
			[(r.mode, r.amount) for r in doc.payments], [("Cash", 100), ("Cash", -100), ("Card", 100)]
		)
		self.assertEqual(doc.payments[1].refund_of, self.row)
		self.assertEqual(doc.payments[2].repost_of, self.row)
		self.assertNotEqual(doc.payments[1].idempotency_key, doc.payments[2].idempotency_key)
		self.assertFalse(self.options()["can_reverse"])
		self.assertFalse(self.options(doc.payments[1].name)["can_reclassify"])
		self.assertEqual(
			sum(
				frappe.get_all(
					"Cashier Transaction", {"session": self.session, "mode": "Cash"}, pluck="amount"
				)
			),
			0,
		)

	def test_refund_cap_and_named_approval(self):
		args = dict(refund_amount=25, supervisor_user="Administrator", supervisor_pin="1234")
		with self.assertRaisesRegex(frappe.PermissionError, "SUPERVISOR_SELF_APPROVAL"):
			self.submit("partial_refund", "REF-OVER", "two", **args)
		user = self.supervisor()
		args.update(supervisor_user=user)
		with self.assertRaises(frappe.ValidationError):
			self.submit("partial_refund", "REF-OVER", "two", **args)
		args.update(supervisor_pin="8394")
		self.submit("partial_refund", "REF-OVER", "two", **args)
		args.update(refund_amount=76)
		with self.assertRaises(frappe.ValidationError):
			self.submit("partial_refund", "REF-OVER", "three", **args)
		self.assertEqual(self.options()["residual"], 75)

	def test_closed_audited_and_role_denial(self):
		with patch("frappe.get_roles", return_value=["Housekeeping"]):
			with self.assertRaises(frappe.PermissionError):
				self.options()
			with self.assertRaises(frappe.PermissionError):
				self.submit("reverse_payment", "REV-ERR", "no")
		for dt, name, field, value in [
			("Property", PROP_A, "business_date", d(1)),
			("Folio", self.folio, "status", "Closed"),
		]:
			frappe.db.set_value(dt, name, field, value)
			self.assertIn("Credit Note", self.options()["blocked_reason"])
			with self.assertRaises(frappe.ValidationError):
				self.submit("reverse_payment", "REV-ERR", "no")

	def test_foreign_drawer_reclassification_moves_original_home_value(self):
		from kamra import ledger

		ledger.upsert_exchange_rate(PROP_A, "EUR", 42, 44)
		p = api.add_folio_payment(self.folio, "Cash", 3, currency="EUR")["payments"][-1]
		self.row = p["name"]
		self.submit(
			"reclassify_tender",
			"REV-ACCOUNT",
			"fx-fix",
			destination_mode="Bank Transfer",
			supervisor_user=self.supervisor(),
			supervisor_pin="8394",
		)
		totals = cashier._recompute_session_totals(self.session)
		self.assertEqual((totals["foreign_cash"], totals["other"]), (0, 126))

	def test_reclassified_advance_transfers_only_once_at_checkin(self):
		from kamra import ledger

		res = make_reservation(PROP_A, make_room_type(PROP_A, "ADV"), d(1), d(3))
		self.folio = api.record_advance(res.name, 500)["folio"]
		self.row = frappe.get_doc("Folio", self.folio).payments[0].name
		self.submit(
			"reclassify_tender",
			"REV-ACCOUNT",
			"adv-fix",
			destination_mode="Bank Transfer",
			supervisor_user=self.supervisor(),
			supervisor_pin="8394",
		)
		self.assertEqual(frappe.db.get_value("Reservation", res.name, "advance_paid"), 500)
		self.assertEqual(ledger.transfer_advances_to_guest(res.name), 500)
		self.assertEqual(ledger.transfer_advances_to_guest(res.name), 0)

	def test_void_reason_without_supervisor_and_closed_till_guidance(self):
		api._post_folio_charge(self.folio, "Misc", "Duplicate", 55)
		row = frappe.get_doc("Folio", self.folio).charges[-1].name
		params = dict(
			folio=self.folio,
			payment_row=row,
			action="void_charge",
			reason_code="VOID-DUP",
			reason="Double charge",
			idempotency_key="void",
		)
		self.assertTrue(self.options(row)["can_void"])
		api.correct_folio_payment(**params)
		self.assertEqual(api.correct_folio_payment(**params)["idempotent_replay"], 1)
		self.assertFalse(self.options(row)["can_void"])
		frappe.db.set_value("Cashier Session", self.session, "status", "Closed")
		self.assertIn("Finance", self.options()["blocked_reason"])

	def test_bookkeeping_reclass_is_not_announced_as_money_paid_to_guest(self):
		user = self.supervisor()
		with patch("kamra.webhooks.emit_payment") as emit:
			self.submit(
				"reclassify_tender",
				"REV-ACCOUNT",
				"mode",
				destination_mode="Card",
				supervisor_user=user,
				supervisor_pin="8394",
			)
			self.assertNotIn("folio.refunded", [call.args[0] for call in emit.call_args_list])

	def test_foreign_to_domestic_repost_carries_home_tender(self):
		from kamra import ledger

		ledger.upsert_exchange_rate(PROP_A, "EUR", 42, 44)
		self.row = api.add_folio_payment(self.folio, "Cash", 3, currency="EUR")["payments"][-1]["name"]
		self.submit(
			"reclassify_tender",
			"REV-ACCOUNT",
			"card-fix",
			destination_mode="Card",
			supervisor_user=self.supervisor(),
			supervisor_pin="8394",
		)
		p = frappe.get_doc("Folio", self.folio).payments[-1]
		self.assertEqual((p.amount, p.currency, p.fx_amount or 0), (126, ledger.home_currency(PROP_A), 0))
