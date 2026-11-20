"""A Property Setting change runs its document events, so a site's own
Before Save rule sees every write path (SAP: one owner of the change,
with a change log).

- write_kv saves the Property Setting document (validate and on_update
  run, Version history records before and after); the cache clears once.
- save_settings marks the row with the settings page it came from
  (flags.kamra_settings_module), so a site rule can tell the page apart
  from any other writer. Nothing outside save_settings sets that flag.
- A credit-note voucher from a cancellation is marked
  (flags.kamra_credit_note) so a site rule on Discount Voucher can tell it
  from a promo code a person creates.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe

from kamra import api, settings
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	ensure_user,
	make_guest,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)


HERE = "kamra.tests.test_settings_document_events"
DESK = "ut-desk-advance@kamra.local"
SEEN_VOUCHERS: list = []
SEEN_FLAGS: list = []
FENCE_FLAGS = ("kamra_settings_module", "kamra_credit_note", "kamra_ledger_advance")


def remember_flags(doc, method=None):
	SEEN_FLAGS.append({k: doc.flags.get(k) for k in FENCE_FLAGS if doc.flags.get(k)})


def refuse_governed_limit(doc, method=None):
	"""A site rule (as a governed-settings fence would): only the settings
	page is refused here, so the test can tell the flag reached it."""
	if doc.setting_key == "rate_override_limit_pct" and doc.flags.get("kamra_settings_module"):
		frappe.throw("UT governed", frappe.PermissionError)


def remember_voucher(doc, method=None):
	SEEN_VOUCHERS.append(doc.flags.get("kamra_credit_note"))


class SettingsDocumentEventsTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY)
		make_property(PROP_A, business_date=TODAY)
		frappe.db.delete("Property Setting", {"property": PROP_A})
		self.addCleanup(settings.clear_cache)
		settings.clear_cache(PROP_A)
		self.seen: list[dict] = []
		self.before: list[str] = []
		real = frappe.get_doc

		def watch(*args, **kwargs):
			doc = real(*args, **kwargs)
			if getattr(doc, "doctype", None) == "Property Setting":
				original = doc.run_method

				def run_method(method, *a, **kw):
					if method == "validate":
						self.seen.append({"key": doc.setting_key, "value": doc.value,
						                  "module": doc.flags.get("kamra_settings_module")})
						before = doc.get_doc_before_save()
						if before:
							self.before.append(before.value)
					return original(method, *a, **kw)

				doc.run_method = run_method
			return doc

		self._stack.enter_context(patch.object(frappe, "get_doc", watch))

	def test_the_module_declares_that_settings_writes_run_document_events(self):
		# A site that governs these rows (SiteOps) refuses to rely on a Kamra without this behaviour.
		self.assertIs(settings.WRITE_KV_RUNS_DOCUMENT_EVENTS, True)

	def test_a_new_row_runs_validate(self):
		settings.write_kv(PROP_A, "rate_override_limit_pct", 10)
		self.assertEqual(self.seen, [{"key": "rate_override_limit_pct", "value": "10", "module": None}])
		self.assertEqual(settings.get_setting(PROP_A, "rate_override_limit_pct"), 10)

	def test_an_existing_row_runs_validate_and_keeps_history(self):
		settings.write_kv(PROP_A, "rate_override_limit_pct", 10)
		self.seen.clear()
		settings.write_kv(PROP_A, "rate_override_limit_pct", 15)
		self.assertEqual(self.seen, [{"key": "rate_override_limit_pct", "value": "15", "module": None}])
		# Frappe skips Version rows under tests (Document.save: ignore_version =
		# frappe.in_test); a saved document carries its before-image, which is
		# what Version and a Before Save rule compare.
		self.assertEqual(self.before, ["10"])
		self.assertEqual(settings.get_setting(PROP_A, "rate_override_limit_pct"), 15)

	def test_an_unchanged_value_writes_nothing(self):
		settings.write_kv(PROP_A, "fx_margin_pct", 2.5)
		self.seen.clear()
		settings.write_kv(PROP_A, "fx_margin_pct", 2.5)
		self.assertEqual(self.seen, [])

	def test_the_settings_page_marks_its_rows(self):
		settings.save_settings(PROP_A, "finance", {"allowance_approval_above": 5000, "fx_auto_fetch": 1})
		self.assertEqual(sorted((s["key"], s["module"]) for s in self.seen),
		                 [("allowance_approval_above", "finance"), ("fx_auto_fetch", "finance")])
		self.assertEqual(settings.get_setting(PROP_A, "allowance_approval_above"), 5000)

	def test_a_site_rule_refusal_stops_the_settings_page(self):
		hooks = {"Property Setting": {"validate": [f"{HERE}.refuse_governed_limit"]}}
		with patch.object(frappe, "get_doc_hooks", lambda: hooks):
			with self.assertRaises(frappe.PermissionError):
				settings.save_settings(PROP_A, "finance", {"rate_override_limit_pct": 25})
		self.assertEqual(settings.get_setting(PROP_A, "rate_override_limit_pct"), 0)


class CreditNoteVoucherFlagTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.api")
		make_property(PROP_A, business_date=TODAY)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.room = make_room(PROP_A, self.rt, "101")

	def test_a_cancellation_credit_note_voucher_is_marked(self):
		res = make_reservation(PROP_A, self.rt, d(3), d(5), status="Confirmed", room=self.room)
		frappe.db.set_value("Reservation", res.name, "advance_paid", 500)
		SEEN_VOUCHERS.clear()
		hooks = {"Discount Voucher": {"validate": [f"{HERE}.remember_voucher"]}}
		with patch.object(frappe, "get_doc_hooks", lambda: hooks):
			out = api.cancel_reservation(res.name, waive_fee=1, issue_credit_note=1)
		self.assertTrue(out.get("voucher_code") or frappe.db.exists("Discount Voucher", {"property": PROP_A}))
		self.assertEqual(SEEN_VOUCHERS, [1])


class AdvancePaidIsLedgerOwnedTestCase(AuditTestCase):
	"""Reservation.advance_paid is the money the folio recorded (kamra.api payments
	and refunds keep it). A person editing the booking cannot change it, so a
	cancellation credit note (sized from it) can't be inflated by the desk."""

	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.api")
		make_property(PROP_A, business_date=TODAY)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.room = make_room(PROP_A, self.rt, "101")

	def test_a_booking_save_cannot_change_the_advance_paid(self):
		res = make_reservation(PROP_A, self.rt, d(3), d(5), status="Confirmed", room=self.room)
		res.reload()
		res.advance_paid = 99999
		with self.assertRaises(api.AdvancePaidIsLedgerOwned):
			res.save(ignore_permissions=True)
		self.assertTrue(issubclass(api.AdvancePaidIsLedgerOwned, frappe.ValidationError))
		self.assertEqual(float(frappe.db.get_value("Reservation", res.name, "advance_paid") or 0), 0)

	def test_a_booking_save_that_keeps_the_advance_paid_still_works(self):
		res = make_reservation(PROP_A, self.rt, d(3), d(5), status="Confirmed", room=self.room)
		res.reload()
		res.special_requests = "UT late arrival"
		res.save(ignore_permissions=True)

	def vouchers(self) -> list[float]:
		return [float(v) for v in frappe.get_all(
			"Discount Voucher", filters={"property": PROP_A}, pluck="value")]

	def test_a_new_booking_cannot_carry_an_advance_paid(self):
		with self.assertRaises(api.AdvancePaidIsLedgerOwned):
			make_reservation(PROP_A, self.rt, d(3), d(5), status="Confirmed",
			                 room=self.room, advance_paid=99999)
		self.assertFalse(frappe.db.exists("Reservation", {"property": PROP_A}))

	def test_an_inserted_advance_cannot_mint_a_credit_note(self):
		# review F1: the desk (create on Reservation) inserts a booking claiming
		# 99999 paid, then cancels it with the fee waived and a credit note
		ensure_user(DESK, ["Front Desk"])
		frappe.set_user(DESK)  # nosemgrep: frappe-setuser -- test fixture
		try:
			res = frappe.get_doc({
				"doctype": "Reservation", "property": PROP_A, "room_type": self.rt,
				"guest": make_guest(), "status": "Confirmed", "room": self.room,
				"check_in_date": d(3), "check_out_date": d(5), "adults": 2,
				"children": 0, "advance_paid": 99999}).insert()
			api.cancel_reservation(res.name, waive_fee=1, issue_credit_note=1)
		except api.AdvancePaidIsLedgerOwned:
			pass
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		self.assertEqual(self.vouchers(), [])

	def test_a_ledger_path_may_save_the_advance_with_the_server_flag(self):
		# the exemption a server-side ledger path sets on the document
		res = frappe.get_doc({
			"doctype": "Reservation", "property": PROP_A, "room_type": self.rt,
			"guest": make_guest(), "status": "Confirmed", "room": self.room,
			"check_in_date": d(3), "check_out_date": d(5), "adults": 2,
			"children": 0, "advance_paid": 400})
		res.flags.kamra_ledger_advance = True
		res.insert(ignore_permissions=True)
		res.reload()
		res.advance_paid = 650
		res.flags.kamra_ledger_advance = True
		res.save(ignore_permissions=True)
		self.assertEqual(float(frappe.db.get_value("Reservation", res.name, "advance_paid")), 650)

	def test_a_zero_advance_on_a_new_booking_is_fine(self):
		res = make_reservation(PROP_A, self.rt, d(3), d(5), status="Confirmed",
		                       room=self.room, advance_paid=0)
		self.assertTrue(frappe.db.exists("Reservation", res.name))


class AdvancePaidRestTestCase(AuditTestCase):
	"""The REST routes a desk user can reach: /api/resource (v1, v2) and
	frappe.client.insert. Frappe drops a "flags" key from every client
	payload, so the server-only exemption can't be claimed from outside."""

	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.api")
		make_property(PROP_A, business_date=TODAY)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.room = make_room(PROP_A, self.rt, "101")
		ensure_user(DESK, ["Front Desk"])

	def payload(self, **extra) -> dict:
		return {"doctype": "Reservation", "property": PROP_A, "room_type": self.rt,
		        "guest": make_guest(), "status": "Confirmed", "room": self.room,
		        "check_in_date": d(3), "check_out_date": d(5), "adults": 2,
		        "children": 0, "advance_paid": 99999, **extra}

	def as_desk(self, call, form: dict | None = None):
		frappe.set_user(DESK)  # nosemgrep: frappe-setuser -- test fixture
		frappe.local.form_dict = frappe._dict(form or {})  # set_user clears it
		try:
			with self.assertRaises(api.AdvancePaidIsLedgerOwned):
				call()
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		self.assertFalse(frappe.db.exists("Reservation", {"property": PROP_A}))

	def test_resource_v1_insert_is_refused(self):
		from frappe.api import v1
		body = self.payload(flags={"kamra_ledger_advance": 1})
		self.as_desk(lambda: v1.create_doc("Reservation"), {"data": json.dumps(body)})

	def test_resource_v2_insert_is_refused(self):
		from frappe.api import v2
		self.as_desk(lambda: v2.create_doc("Reservation"),
		             self.payload(flags={"kamra_ledger_advance": 1}))

	def test_client_insert_is_refused(self):
		from frappe import client
		body = self.payload(flags={"kamra_ledger_advance": 1})
		self.as_desk(lambda: client.insert(json.dumps(body)))


class FenceFlagsAreServerOnlyTestCase(AuditTestCase):
	"""Review F4: the flags a site rule keys on (kamra_settings_module,
	kamra_credit_note, kamra_ledger_advance) live on doc.flags, which Frappe
	never fills from a client payload. Each test has its server-side twin
	above, where the same hook does see the flag."""

	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		make_property(PROP_A, business_date=TODAY)
		frappe.db.delete("Property Setting", {"property": PROP_A})
		self.addCleanup(settings.clear_cache)
		SEEN_FLAGS.clear()

	def test_a_client_cannot_mark_a_settings_row(self):
		from frappe import client
		hooks = {"Property Setting": {"validate": [f"{HERE}.remember_flags"]}}
		with patch.object(frappe, "get_doc_hooks", lambda: hooks):
			client.insert(json.dumps({
				"doctype": "Property Setting", "property": PROP_A,
				"setting_key": "rate_override_limit_pct", "value": "25",
				"flags": {"kamra_settings_module": "finance"}}))
		self.assertEqual(SEEN_FLAGS, [{}])

	def test_a_client_cannot_mark_a_promo_code_as_a_credit_note(self):
		from frappe import client
		hooks = {"Discount Voucher": {"validate": [f"{HERE}.remember_flags"]}}
		with patch.object(frappe, "get_doc_hooks", lambda: hooks):
			client.insert(json.dumps({
				"doctype": "Discount Voucher", "property": PROP_A,
				"voucher_code": "UT-FLAG", "discount_type": "Amount", "value": 500,
				"valid_from": TODAY, "valid_to": d(30), "max_uses": 1,
				"flags": {"kamra_credit_note": 1}}))
		self.assertEqual(SEEN_FLAGS, [{}])


class LedgerWritersKeepAdvancePaidTestCase(AuditTestCase):
	"""Review F2/F3: with the guard on, the ledger paths still move
	advance_paid - a cash advance, a payment link (a Held booking is
	confirmed by it, OPERA: the link settles into the deposit ledger), a
	refund of an advance, and the credit note a cancellation issues."""

	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.api",
		        "kamra.ledger", "kamra.payments")
		make_property(PROP_A, business_date=TODAY)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.room = make_room(PROP_A, self.rt, "101")

	def paid(self, res) -> float:
		return float(frappe.db.get_value("Reservation", res.name, "advance_paid") or 0)

	def deposit_held(self, folio: str) -> float:
		return float(frappe.db.sql(
			"""SELECT COALESCE(SUM(credit - debit), 0) FROM `tabFolio Ledger Entry`
			   WHERE folio = %s AND ledger = 'Deposit'""", (folio,))[0][0] or 0)

	def test_a_payment_link_confirms_a_held_booking(self):
		from kamra.folio import open_folio
		from kamra.payments import settle_payment_link
		res = make_reservation(PROP_A, self.rt, d(3), d(5), status="Held", room=self.room)
		folio = open_folio(res)
		self.assertIs(settle_payment_link(folio, "plink_UT_HELD", 300), True)
		self.assertEqual(frappe.db.get_value("Reservation", res.name, "status"), "Confirmed")
		self.assertEqual(self.paid(res), 300)
		self.assertEqual(self.deposit_held(folio), 300)
		# a retried event posts nothing more
		self.assertIs(settle_payment_link(folio, "plink_UT_HELD", 300), False)
		self.assertEqual(self.paid(res), 300)

	def test_a_payment_link_on_a_confirmed_booking_adds_to_the_advance(self):
		from kamra.folio import open_folio
		from kamra.payments import settle_payment_link
		res = make_reservation(PROP_A, self.rt, d(3), d(5), status="Confirmed", room=self.room)
		folio = open_folio(res)
		settle_payment_link(folio, "plink_UT_A", 200)
		settle_payment_link(folio, "plink_UT_B", 50)
		self.assertEqual(self.paid(res), 250)
		self.assertEqual(frappe.db.get_value("Reservation", res.name, "status"), "Confirmed")

	def test_a_held_booking_save_still_cannot_change_the_advance(self):
		res = make_reservation(PROP_A, self.rt, d(3), d(5), status="Held", room=self.room)
		res.reload()
		res.advance_paid = 300
		with self.assertRaises(api.AdvancePaidIsLedgerOwned):
			res.save(ignore_permissions=True)

	def test_a_cash_advance_and_its_refund_move_the_advance(self):
		from kamra import cashier
		cashier.open_session(PROP_A, opening_float=500)
		res = make_reservation(PROP_A, self.rt, d(3), d(5), status="Held", room=self.room)
		out = api.record_advance(res.name, 1500, "Cash")
		self.assertEqual(self.paid(res), 1500)
		self.assertEqual(out["status"], "Confirmed")
		api.refund_folio_payment(out["folio"], 500, "Cash", reason="UT guest asked")
		self.assertEqual(self.paid(res), 1000)

	def test_a_cancellation_credit_note_is_sized_from_the_real_advance(self):
		from kamra import cashier
		cashier.open_session(PROP_A, opening_float=500)
		res = make_reservation(PROP_A, self.rt, d(3), d(5), status="Confirmed", room=self.room)
		api.record_advance(res.name, 700, "Cash")
		out = api.cancel_reservation(res.name, waive_fee=1, issue_credit_note=1)
		self.assertTrue(out["credit_note_voucher"])
		self.assertEqual(frappe.db.get_value(
			"Discount Voucher", {"voucher_code": out["credit_note_voucher"]}, "value"), 700)
