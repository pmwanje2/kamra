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

	def supervisor(self, user="k2-supervisor@example.test"):
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
		receipts = []
		for _ in range(2):
			receipts.append(
				self.submit(
					"reclassify_tender",
					"REV-ACCOUNT",
					"one",
					destination_mode="Card",
					supervisor_user=user,
					supervisor_pin="8394",
				)
			)
		self.assertEqual(receipts[0]["repost"], receipts[1].get("repost"))
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
		frappe.db.set_value("Folio", self.folio, "status", "Closed")
		self.assertIn("Credit Note", self.options()["blocked_reason"])
		with self.assertRaisesRegex(frappe.ValidationError, "KMR-CN-001"):
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
		self.assertIn(
			"Journal Entry",
			next(b["reason"] for b in self.options()["blocked"] if b["id"] == "reverse_payment"),
		)

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

	def test_requester_with_other_drawer_corrects_original_drawer(self):
		approver = self.supervisor()
		operator = self.supervisor("different-cashier@example.test")
		with self.set_user(operator):
			other = cashier.open_session(PROP_A, 0)["name"]
			r = self.submit(
				"reclassify_tender",
				"REV-ACCOUNT",
				"other",
				destination_mode="Card",
				supervisor_user=approver,
				supervisor_pin="8394",
			)
		self.assertFalse(frappe.db.exists("Cashier Transaction", {"session": other, "folio": self.folio}))
		for name in [r["reversal"], r["repost"]]:
			self.assertEqual(
				frappe.db.get_value("Folio Ledger Entry", {"source_name": name}, "session"), self.session
			)
		totals = cashier._recompute_session_totals(self.session)
		self.assertEqual((totals["cash"], totals["card"]), (0, 100))

	def test_prior_receipt_guides_journal_but_refunds_credit_on_current_day(self):
		frappe.db.set_value("Property", PROP_A, "business_date", d(1))
		frappe.db.set_value("Cashier Session", self.session, "status", "Closed")
		current = cashier.open_session(PROP_A, 0)["name"]
		opts = self.options()
		self.assertEqual([a["id"] for a in opts["actions"]], ["partial_refund"])
		for action in ("reverse_payment", "reclassify_tender"):
			with self.assertRaisesRegex(frappe.ValidationError, "Journal Entry"):
				self.submit(action, "REV-ACCOUNT", "stop")
		refund = self.submit(
			"partial_refund",
			"REF-OVER",
			"refund",
			refund_amount=25,
			supervisor_user=self.supervisor(),
			supervisor_pin="8394",
		)["refund"]
		self.assertEqual(str(frappe.db.get_value("Folio Payment", refund, "posting_date")), d(1))
		self.assertEqual(
			frappe.db.get_value("Folio Ledger Entry", {"source_name": refund}, "session"), current
		)

	def test_prior_charge_needs_supervisor_and_voids_today(self):
		api._post_folio_charge(self.folio, "Misc", "Duplicate", 55)
		self.row = frappe.get_doc("Folio", self.folio).charges[-1].name
		frappe.db.set_value("Property", PROP_A, "business_date", d(1))
		self.assertTrue(self.options()["can_void"])
		with self.assertRaises(frappe.PermissionError):
			self.submit("void_charge", "VOID-DUP", "prior-charge")
		self.submit(
			"void_charge",
			"VOID-DUP",
			"prior-charge",
			supervisor_user=self.supervisor(),
			supervisor_pin="8394",
		)
		self.assertEqual(str(frappe.get_doc("Folio", self.folio).charges[-1].posting_date), d(1))

	def test_foreign_wire_to_foreign_cash_preserves_notes(self):
		from kamra import ledger

		ledger.upsert_exchange_rate(PROP_A, "EUR", 42, 44)
		self.row = api.add_folio_payment(self.folio, "Bank Transfer", 3, currency="EUR")["payments"][-1][
			"name"
		]
		self.submit(
			"reclassify_tender",
			"REV-ACCOUNT",
			"notes",
			destination_mode="Foreign Cash",
			supervisor_user=self.supervisor(),
			supervisor_pin="8394",
		)
		p = frappe.get_doc("Folio", self.folio).payments[-1]
		self.assertEqual((p.mode, p.currency, p.fx_amount, p.amount), ("Foreign Cash", "EUR", 3, 126))
		totals = cashier._recompute_session_totals(self.session)
		self.assertEqual((totals["foreign_cash"], totals["other"]), (126, 0))

	def test_options_offer_named_eligible_supervisors(self):
		user = self.supervisor()
		self.assertIn(user, [s["user"] for s in self.options().get("supervisors", [])])

	def test_governed_prior_advance_refund_has_audit_record(self):
		res = make_reservation(PROP_A, make_room_type(PROP_A, "ADV"), d(1), d(3))
		self.folio = api.record_advance(res.name, 500)["folio"]
		self.row = frappe.get_doc("Folio", self.folio).payments[0].name
		frappe.db.set_value("Property", PROP_A, "business_date", d(1))
		with patch("kamra.savings.log_action") as log:
			self.submit(
				"partial_refund",
				"REF-OVER",
				"payback",
				refund_amount=50,
				supervisor_user=self.supervisor(),
				supervisor_pin="8394",
			)
		self.assertIn("folio_refund", [c.args[0] for c in log.call_args_list])
		self.assertEqual(frappe.db.get_value("Reservation", res.name, "advance_paid"), 450)

	def test_finance_without_till_corrects_sessionless_bank_payment(self):
		frappe.db.set_value("Cashier Session", self.session, "status", "Closed")
		self.row = api.add_folio_payment(self.folio, "Bank Transfer", 100)["payments"][-1]["name"]
		approver = self.supervisor()
		operator = self.supervisor("no-drawer@example.test")
		with self.set_user(operator):
			r = self.submit(
				"reverse_payment", "REV-ERR", "bank", supervisor_user=approver, supervisor_pin="8394"
			)
		self.assertFalse(frappe.db.get_value("Folio Ledger Entry", {"source_name": r["reversal"]}, "session"))


class BridgeSupervisorTests(AuditTestCase):
	def setUp(self):
		super().setUp()
		from kamra.tests.audit_fixtures import PROP_B, ensure_user

		self.home = make_property(PROP_A)
		self.other = make_property(PROP_B)
		self.desk = ensure_user("k2b-desk@example.test", ["Front Desk"])
		self.approver = ensure_user("k2b-approver@example.test", ["Finance"])
		self.housekeeping = ensure_user("k2b-housekeeping@example.test", ["Housekeeping"])
		for user in (self.desk, self.approver):
			frappe.get_doc(
				dict(doctype="User Permission", user=user, allow="Property", for_value=self.home)
			).insert()
			frappe.clear_cache(user=user)
		frappe.get_doc(dict(doctype="Cashier PIN", user=self.approver, pin="8394")).insert()
		self.fails_key = f"kamra_supervisor_pin_fails:{self.approver}"
		frappe.cache.delete_value(self.fails_key)
		self.addCleanup(frappe.cache.delete_value, self.fails_key)

	def bridge(self, endpoint, prop, **kwargs):
		from frappe.utils.safe_exec import safe_exec

		from kamra import authz

		method = getattr(authz, endpoint, None)
		self.assertIn(method, frappe.whitelisted, f"Server Scripts need whitelisted kamra.authz.{endpoint}")
		local = dict(method=f"kamra.authz.{endpoint}", prop=prop, **kwargs)
		args = "property=prop"
		if endpoint == "verify_supervisor_approval":
			args += ", supervisor=supervisor, pin=pin"
		if "requester" in kwargs:
			args += ", requester=requester"
		with patch("frappe.utils.safe_exec.is_safe_exec_enabled", return_value=True):
			safe_exec(f"result = frappe.call(method, {args})", _locals=local)
		return local["result"]

	def test_front_desk_lists_and_verifies_named_approver(self):
		with self.set_user(self.desk):
			self.assertIn(self.approver, [r["user"] for r in self.bridge("list_supervisors", self.home)])
			result = self.bridge(
				"verify_supervisor_approval", self.home, supervisor=self.approver, pin="8394"
			)
			self.assertEqual(result, {"supervisor": self.approver})

	def test_requester_is_session_owned_and_excluded(self):
		with self.set_user(self.approver):
			self.assertNotIn(self.approver, [r["user"] for r in self.bridge("list_supervisors", self.home)])
			with self.assertRaisesRegex(frappe.PermissionError, "SUPERVISOR_SELF_APPROVAL"):
				self.bridge(
					"verify_supervisor_approval",
					self.home,
					supervisor=self.approver,
					pin="8394",
					requester=self.desk,
				)

	def test_unauthorized_calls_do_not_attempt_a_pin(self):
		for user, prop in [
			(self.housekeeping, self.home),
			(self.desk, self.other),
			(self.desk, ""),
			("Guest", self.home),
		]:
			with self.subTest(user=user, prop=prop), self.set_user(user):
				with self.assertRaises(frappe.PermissionError):
					self.bridge("list_supervisors", prop)
				with self.assertRaises(frappe.PermissionError):
					self.bridge("verify_supervisor_approval", prop, supervisor=self.approver, pin="0000")
		self.assertFalse(frappe.cache.get_value(self.fails_key))

	def test_wrong_attempts_survive_request_rollback_and_lock_correct_pin(self):
		with self.set_user(self.desk), patch("kamra.authz._pin_policy", return_value=(2, 15)):
			for count in (1, 2):
				frappe.db.savepoint("bad_supervisor_pin")
				with self.assertRaisesRegex(frappe.ValidationError, "Wrong cashier PIN"):
					self.bridge("verify_supervisor_approval", self.home, supervisor=self.approver, pin="0000")
				frappe.db.rollback(save_point="bad_supervisor_pin")
				self.assertEqual(frappe.cache.get_value(self.fails_key), count)
			with self.assertRaisesRegex(frappe.ValidationError, "PIN_LOCKED"):
				self.bridge("verify_supervisor_approval", self.home, supervisor=self.approver, pin="8394")
