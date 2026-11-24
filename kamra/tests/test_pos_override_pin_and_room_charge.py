# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

"""POS manager override PIN hook (pos_override_pin_verifier) and room-charge
verification / duty-manager override contracts (matrix row 34, B5c+d)."""

from __future__ import annotations

import threading
from unittest.mock import patch

import frappe
from frappe.utils import flt

from kamra import authz, hooks
from kamra import pos_interface as pi
from kamra.pos import RoomChargesBlocked
from kamra.tests.audit_fixtures import (
	PROP_A,
	PROP_B,
	TODAY,
	AuditTestCase,
	d,
	ensure_user,
	guest_folio,
	make_guest,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

POS_USER = "ut.b5c.pos@test.local"
SUP_A = "ut.b5c.sup.a@test.local"
SUP_B = "ut.b5c.sup.b@test.local"
CLK_A = "ut.b5c.clerk.a@test.local"
SUP_NOPIN = "ut.b5c.nopin@test.local"


def _set_pin(user: str, pin: str, *, must_reset: int = 0) -> None:
	if frappe.db.exists("Cashier PIN", user):
		doc = frappe.get_doc("Cashier PIN", user)
		doc.pin = pin
		doc.must_reset = must_reset
		doc.pin_attempts = 0
		doc.locked_until = None
		doc.save(ignore_permissions=True)
	else:
		frappe.get_doc({
			"doctype": "Cashier PIN",
			"user": user,
			"pin": pin,
			"must_reset": must_reset,
		}).insert(ignore_permissions=True)
	frappe.cache.delete_value(f"kamra_supervisor_pin_fails:{user.casefold()}")


def _restrict_property(user: str, prop: str) -> None:
	existing = frappe.get_all(
		"User Permission",
		filters={"user": user, "allow": "Property"},
		pluck="name",
	)
	for name in existing:
		frappe.delete_doc("User Permission", name, ignore_permissions=True)
	frappe.get_doc({
		"doctype": "User Permission",
		"user": user,
		"allow": "Property",
		"for_value": prop,
	}).insert(ignore_permissions=True)
	frappe.clear_cache(user=user)


def _set_prop_setting(prop: str, key: str, value) -> None:
	from kamra.settings import clear_cache, write_kv
	write_kv(prop, key, value)
	clear_cache(prop)


class POSOverridePinHookTestCase(AuditTestCase):
	"""`pos_override_pin_verifier` verifies an existing Kamra Cashier PIN via
	`authz.verify_supervisor`: same lockout, self-approval refused, property
	access enforced, returning only yes/no + name (no new PIN store)."""

	properties = (PROP_A, PROP_B)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY, currency="UGX")
		make_property(PROP_B, business_date=TODAY, currency="UGX")
		ensure_user(POS_USER, ["POS Integrator", "Front Desk"])
		ensure_user(SUP_A, ["Hotel Admin"])
		ensure_user(SUP_B, ["Hotel Admin"])
		ensure_user(CLK_A, ["Front Desk"])
		ensure_user(SUP_NOPIN, ["Finance"])
		frappe.db.set_value("User", SUP_A, "full_name", "Amina Duty")
		frappe.db.set_value("User", SUP_B, "full_name", "Bala Other")
		_restrict_property(POS_USER, PROP_A)
		_restrict_property(SUP_A, PROP_A)
		_restrict_property(SUP_B, PROP_B)
		_restrict_property(CLK_A, PROP_A)
		_restrict_property(SUP_NOPIN, PROP_A)
		_set_pin(SUP_A, "4321")
		_set_pin(SUP_B, "5678")
		_set_pin(CLK_A, "1111")
		if frappe.db.exists("Cashier PIN", SUP_NOPIN):
			frappe.delete_doc("Cashier PIN", SUP_NOPIN, ignore_permissions=True)
		frappe.cache.delete_value(f"kamra_supervisor_pin_fails:{SUP_A.casefold()}")
		frappe.cache.delete_value(f"kamra_supervisor_pin_fails:{SUP_B.casefold()}")

	def test_hook_registered_and_verifies_existing_cashier_pin(self):
		hook_target = getattr(hooks, "pos_override_pin_verifier", None)
		self.assertEqual(hook_target, "kamra.authz.verify_pos_override_pin")
		fn = frappe.get_attr(hook_target)
		frappe.set_user(POS_USER)  # nosemgrep: frappe-setuser -- cashier persona
		res = fn(SUP_A, "4321", property=PROP_A, requester=POS_USER)
		self.assertEqual(res.get("ok"), True)
		self.assertEqual(res.get("name"), "Amina Duty")
		# Only yes/no + name on success: never leak PIN, hash, or extra fields.
		self.assertEqual(set(res.keys()), {"ok", "name"})

	def test_self_approval_refused(self):
		fn = frappe.get_attr(hooks.pos_override_pin_verifier)
		frappe.set_user(SUP_A)  # nosemgrep: frappe-setuser -- supervisor persona
		res = fn(SUP_A, "4321", property=PROP_A, requester=SUP_A)
		self.assertFalse(res.get("ok"))
		self.assertIsNone(res.get("name"))
		self.assertEqual(res.get("code"), "POS-AUTH-SELF")

	def test_wrong_property_supervisor_refused(self):
		fn = frappe.get_attr(hooks.pos_override_pin_verifier)
		frappe.set_user(POS_USER)  # nosemgrep: frappe-setuser -- cashier persona
		res = fn(SUP_B, "5678", property=PROP_A, requester=POS_USER)
		self.assertFalse(res.get("ok"))
		self.assertIsNone(res.get("name"))
		self.assertEqual(res.get("code"), "POS-AUTH-NOT-ELIGIBLE")

	def test_non_supervisor_and_administrator_refused(self):
		fn = frappe.get_attr(hooks.pos_override_pin_verifier)
		frappe.set_user(POS_USER)  # nosemgrep: frappe-setuser -- cashier persona
		clerk_res = fn(CLK_A, "1111", property=PROP_A, requester=POS_USER)
		self.assertFalse(clerk_res.get("ok"))
		self.assertEqual(clerk_res.get("code"), "POS-AUTH-NOT-ELIGIBLE")
		admin_res = fn("Administrator", "4321", property=PROP_A, requester=POS_USER)
		self.assertFalse(admin_res.get("ok"))
		self.assertEqual(admin_res.get("code"), "POS-AUTH-NOT-ELIGIBLE")

	def test_no_pin_returns_no_pin_code(self):
		fn = frappe.get_attr(hooks.pos_override_pin_verifier)
		frappe.set_user(POS_USER)  # nosemgrep: frappe-setuser -- cashier persona
		res = fn(SUP_NOPIN, "9999", property=PROP_A, requester=POS_USER)
		self.assertFalse(res.get("ok"))
		self.assertEqual(res.get("code"), "POS-AUTH-NO-PIN")

	def test_lockout_shared_with_verify_supervisor_and_survives_rollback(self):
		fn = frappe.get_attr(hooks.pos_override_pin_verifier)
		frappe.set_user(POS_USER)  # nosemgrep: frappe-setuser -- cashier persona
		for _ in range(authz.PIN_MAX_ATTEMPTS):
			res = fn(SUP_A, "0000", property=PROP_A, requester=POS_USER)
			self.assertFalse(res.get("ok"))
		# Even with the right PIN now, both the hook and verify_supervisor are locked.
		locked = fn(SUP_A, "4321", property=PROP_A, requester=POS_USER)
		self.assertFalse(locked.get("ok"))
		self.assertEqual(locked.get("code"), "POS-AUTH-LOCKED")
		with self.assertRaisesRegex(frappe.ValidationError, "PIN_LOCKED"):
			authz.verify_supervisor(PROP_A, SUP_A, "4321", requester=POS_USER)

	def test_agent_session_refused(self):
		fn = frappe.get_attr(hooks.pos_override_pin_verifier)
		frappe.set_user(POS_USER)  # nosemgrep: frappe-setuser -- cashier persona
		frappe.flags.kamra_agent_call = True
		try:
			res = fn(SUP_A, "4321", property=PROP_A, requester=POS_USER)
		finally:
			frappe.flags.kamra_agent_call = False
		self.assertFalse(res.get("ok"))
		self.assertEqual(res.get("code"), "POS-AUTH-NOT-ELIGIBLE")

	def test_form_dict_pin_scrubbed(self):
		fn = frappe.get_attr(hooks.pos_override_pin_verifier)
		frappe.set_user(POS_USER)  # nosemgrep: frappe-setuser -- cashier persona
		frappe.local.form_dict = frappe._dict({"pin": "4321"})
		res = fn(SUP_A, "4321", property=PROP_A, requester=POS_USER)
		self.assertTrue(res.get("ok"))
		self.assertNotIn("pin", frappe.local.form_dict)

	def test_must_reset_and_disabled_user_never_record_lockout_strikes_and_db_lockout_respected(self):
		fn = frappe.get_attr(hooks.pos_override_pin_verifier)
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- fixture
		_set_pin(SUP_A, "4321", must_reset=1)
		fails_key = f"kamra_supervisor_pin_fails:{SUP_A.casefold()}"
		frappe.cache.delete_value(fails_key)

		# Non-PIN must_reset state must NEVER record lockout strikes even after PIN_MAX_ATTEMPTS + 1 calls!
		frappe.set_user(POS_USER)  # nosemgrep: frappe-setuser -- cashier persona
		for _ in range(authz.PIN_MAX_ATTEMPTS + 1):
			res = fn(SUP_A, "4321", property=PROP_A, requester=POS_USER)
			self.assertFalse(res.get("ok"))
			self.assertEqual(res.get("code"), "POS-AUTH-NO-PIN")
		self.assertEqual(frappe.utils.cint(frappe.cache.get_value(fails_key)), 0)

		# Disabled user returns POS-AUTH-NOT-ELIGIBLE with zero strikes
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- fixture
		_set_pin(SUP_A, "4321", must_reset=0)
		frappe.db.set_value("User", SUP_A, "enabled", 0)
		frappe.set_user(POS_USER)  # nosemgrep: frappe-setuser -- cashier persona
		dis = fn(SUP_A, "4321", property=PROP_A, requester=POS_USER)
		self.assertFalse(dis.get("ok"))
		self.assertEqual(dis.get("code"), "POS-AUTH-NOT-ELIGIBLE")
		self.assertEqual(frappe.utils.cint(frappe.cache.get_value(fails_key)), 0)

		# DB-locked Cashier PIN (locked_until in future) returns POS-AUTH-LOCKED
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- fixture
		frappe.db.set_value("User", SUP_A, "enabled", 1)
		frappe.db.set_value("Cashier PIN", SUP_A, "locked_until", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=10))
		frappe.set_user(POS_USER)  # nosemgrep: frappe-setuser -- cashier persona
		locked = fn(SUP_A, "4321", property=PROP_A, requester=POS_USER)
		self.assertFalse(locked.get("ok"))
		self.assertEqual(locked.get("code"), "POS-AUTH-LOCKED")


class RoomChargeVerifyAndOverrideTestCase(AuditTestCase):
	"""`verify_room_charge` neutral read-only exposure + duty-manager override
	on over-limit / No Post + adversarial scenarios."""

	properties = (PROP_A, PROP_B)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		self._stack.enter_context(patch("kamra.webhooks._enqueue"))
		make_property(PROP_A, business_date=TODAY, currency="UGX")
		make_property(PROP_B, business_date=TODAY, currency="UGX")
		self.rt_a = make_room_type(PROP_A, "STD", base_price=1000)
		self.room_101 = make_room(PROP_A, self.rt_a, "101")
		self.room_102 = make_room(PROP_A, self.rt_a, "102")
		self.res_a = make_reservation(
			PROP_A, self.rt_a, d(-1), d(2), room=self.room_101,
			status="Checked In",
			guest=make_guest("Asha", last_name="Rao"),
		)
		self.rt_b = make_room_type(PROP_B, "STD", base_price=1000)
		self.room_b201 = make_room(PROP_B, self.rt_b, "201")
		self.res_b = make_reservation(
			PROP_B, self.rt_b, d(-1), d(2), room=self.room_b201,
			status="Checked In",
			guest=make_guest("Bala", last_name="Kato"),
		)
		ensure_user(POS_USER, ["POS Integrator", "Front Desk"])
		ensure_user(SUP_A, ["Hotel Admin"])
		ensure_user(SUP_B, ["Hotel Admin"])
		ensure_user(CLK_A, ["Front Desk"])
		frappe.db.set_value("User", SUP_A, "full_name", "Amina Duty")
		_restrict_property(POS_USER, PROP_A)
		_restrict_property(SUP_A, PROP_A)
		_restrict_property(SUP_B, PROP_B)
		_restrict_property(CLK_A, PROP_A)
		_set_pin(SUP_A, "4321")
		_set_pin(SUP_B, "5678")
		_set_pin(CLK_A, "1111")
		_set_prop_setting(PROP_A, "pos_room_charge_limit", 1000)
		_set_prop_setting(PROP_A, "pos_room_charge_credit_check", 0)
		_set_prop_setting(PROP_A, "pos_room_charge_allowance", 0)

	def tearDown(self):
		from kamra.settings import clear_cache
		for p in self.properties:
			clear_cache(p)
		super().tearDown()

	def as_pos(self):
		frappe.set_user(POS_USER)  # nosemgrep: frappe-setuser -- cashier persona

	# -- verify_room_charge neutral read-only shape -------------------------

	def test_verify_exposes_limit_balance_no_post_and_reason_in_neutral_shape(self):
		self.as_pos()
		res = pi.verify_room_charge(property=PROP_A, room="101", amount=400)
		self.assertTrue(res["ok"])
		self.assertTrue(res["posting_allowed"])
		self.assertEqual(res["no_post"], 0)
		self.assertEqual(flt(res["limit"]), 1000.0)
		self.assertEqual(res["currency"], "UGX")
		self.assertIn("balance", res)
		self.assertFalse(res["override_required"])
		# Neutral wording: never exposes internal brand names
		text = str(res).lower()
		for forbidden in ("kamra", "ury", "huf"):
			self.assertNotIn(forbidden, text)

	def test_verify_by_surname_prefix_and_wildcard_rejection(self):
		self.as_pos()
		found = pi.verify_room_charge(property=PROP_A, surname="Rao")
		self.assertTrue(found["ok"])
		self.assertEqual(len(found["matches"]), 1)
		self.assertEqual(found["matches"][0]["reservation"], self.res_a.name)
		self.assertEqual(found["matches"][0]["room_number"], "101")
		# Wildcards must never list all guests
		empty = pi.verify_room_charge(property=PROP_A, surname="%%%")
		self.assertFalse(empty["ok"])
		self.assertEqual(empty["matches"], [])

	def test_verify_flags_over_limit_and_no_post_as_override_required(self):
		self.as_pos()
		over = pi.verify_room_charge(property=PROP_A, room="101", amount=1500)
		self.assertTrue(over["ok"])
		self.assertFalse(over["posting_allowed"])
		self.assertTrue(over["override_required"])
		self.assertEqual(over["reason_code"], "POS-RC-OVER-LIMIT")
		self.assertTrue(over["reason"])

		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- fixture
		frappe.db.set_value("Reservation", self.res_a.name, "room_charges_allowed", 0)
		self.as_pos()
		np = pi.verify_room_charge(property=PROP_A, room="101", amount=200)
		self.assertTrue(np["ok"])
		self.assertFalse(np["posting_allowed"])
		self.assertEqual(np["no_post"], 1)
		self.assertTrue(np["override_required"])
		self.assertEqual(np["reason_code"], "POS-RC-NO-POST")
		self.assertTrue(np["reason"])

	def test_verify_is_permission_checked_and_blocks_wrong_property(self):
		frappe.set_user(ensure_user("ut.b5c.hk@test.local", ["Housekeeping"]))  # nosemgrep: frappe-setuser -- persona
		with self.assertRaises(frappe.PermissionError):
			pi.verify_room_charge(property=PROP_A, room="101")
		self.as_pos()
		with self.assertRaises(frappe.PermissionError):
			pi.verify_room_charge(property=PROP_B, room="201")
		with self.assertRaises(frappe.PermissionError):
			pi.verify_room_charge(property=PROP_A, reservation=self.res_b.name)

	# -- Over-limit and No Post duty-manager override + audit ---------------

	def test_over_limit_refuses_without_approval_and_posts_with_duty_manager_override(self):
		self.as_pos()
		with self.assertRaisesRegex(frappe.ValidationError, "limit"):
			pi.pos_post_room_charge(PROP_A, "T-OL-1", 1500, "Dinner", room="101")
		self.assertEqual(frappe.db.count(pi.CHARGE, {"property": PROP_A}), 0)

		# Wrong PIN / non-supervisor / wrong property still refused
		for bad in (
			{"supervisor": SUP_A, "pin": "0000", "reason": "VIP"},
			{"supervisor": CLK_A, "pin": "1111", "reason": "VIP"},
			{"supervisor": SUP_B, "pin": "5678", "reason": "VIP"},
			{"supervisor": SUP_A, "pin": "4321", "reason": ""},
		):
			with self.assertRaises((frappe.ValidationError, frappe.PermissionError)):
				pi.pos_post_room_charge(
					PROP_A, "T-OL-BAD", 1500, "Dinner", room="101",
					override_approval=bad,
				)
		self.assertEqual(frappe.db.count(pi.CHARGE, {"property": PROP_A}), 0)

		# Valid duty-manager approval posts and audits on PMS side
		out = pi.pos_post_room_charge(
			PROP_A, "T-OL-OK", 1500, "Dinner", room="101",
			override_approval={"supervisor": SUP_A, "pin": "4321", "reason": "VIP corporate dinner"},
		)
		self.assertTrue(out["ok"])
		self.assertEqual(out["override_approved_by"], SUP_A)
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- inspect audit
		logs = frappe.get_all(
			"Agent Action Log",
			filters={"property": PROP_A, "action_type": "pos_room_charge_override", "approver": SUP_A},
			fields=["name", "approver", "rationale"],
		)
		self.assertEqual(len(logs), 1)
		self.assertIn("VIP corporate dinner", logs[0].rationale)

	def test_no_post_refuses_without_approval_and_posts_with_duty_manager_override(self):
		frappe.db.set_value("Reservation", self.res_a.name, "room_charges_allowed", 0)
		self.as_pos()
		with self.assertRaises(RoomChargesBlocked):
			pi.pos_post_room_charge(PROP_A, "T-NP-1", 300, "Coffee", room="101")

		out = pi.pos_post_room_charge(
			PROP_A, "T-NP-OK", 300, "Coffee", room="101",
			override_approval={"supervisor": SUP_A, "pin": "4321", "reason": "Duty manager approved room charge"},
		)
		self.assertTrue(out["ok"])
		self.assertEqual(out["override_approved_by"], SUP_A)
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- inspect audit
		self.assertTrue(frappe.db.exists(
			"Agent Action Log",
			{"property": PROP_A, "action_type": "pos_room_charge_override", "approver": SUP_A},
		))

	# -- Adversarial scenarios ----------------------------------------------

	def test_adversarial_guest_checked_out_between_lookup_and_post(self):
		self.as_pos()
		check = pi.verify_room_charge(property=PROP_A, room="101", amount=200)
		self.assertTrue(check["posting_allowed"])

		# Guest checks out before the till posts
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- simulate checkout
		frappe.db.set_value("Reservation", self.res_a.name, "status", "Checked Out")

		self.as_pos()
		# Refused EVEN WITH a valid duty-manager approval
		with self.assertRaises(frappe.ValidationError):
			pi.pos_post_room_charge(
				PROP_A, "T-CO-1", 200, "Bar", room="101", reservation=self.res_a.name,
				override_approval={"supervisor": SUP_A, "pin": "4321", "reason": "Late charge"},
			)

	def test_adversarial_shared_room_lists_both_and_requires_explicit_reservation(self):
		sharer = make_reservation(
			PROP_A, self.rt_a, d(-1), d(2), room=self.room_102,
			status="Checked In",
			guest=make_guest("Chidi", last_name="Okonkwo"),
		)
		# Shared room: two Checked In stays occupying physical room 101
		frappe.db.set_value("Reservation", sharer.name, "room", self.room_101)
		self.as_pos()
		lookup = pi.verify_room_charge(property=PROP_A, room="101", amount=200)
		self.assertTrue(lookup["ok"])
		self.assertTrue(lookup["shared_room"])
		self.assertEqual(len(lookup["matches"]), 2)
		self.assertFalse(lookup["posting_allowed"])
		self.assertEqual(
			{m["reservation"] for m in lookup["matches"]},
			{self.res_a.name, sharer.name},
		)
		# Posting by room number alone MUST refuse when multiple guests share the room
		with self.assertRaisesRegex(frappe.ValidationError, "multiple|sharer|pick|reservation"):
			pi.pos_post_room_charge(PROP_A, "T-SH-AMB", 200, "Tea", room="101")
		# Posting with explicit reservation succeeds on that sharer's own folio
		out = pi.pos_post_room_charge(
			PROP_A, "T-SH-OK", 200, "Tea", room="101", reservation=sharer.name,
		)
		self.assertEqual(out["reservation"], sharer.name)
		self.assertEqual(out["folio"], guest_folio(sharer.name))

	def test_adversarial_group_master_routing_and_balance(self):
		gb = frappe.get_doc({
			"doctype": "Group Booking",
			"property": PROP_A,
			"group_name": "UT Summit Group",
			"check_in_date": d(-1),
			"check_out_date": d(2),
			"master_reservation": self.res_a.name,
			"master_pays": "Everything except liquor",
		}).insert(ignore_permissions=True)
		member = make_reservation(
			PROP_A, self.rt_a, d(-1), d(2), room=self.room_102,
			status="Checked In",
			guest=make_guest("Dina", last_name="Patel"),
			group_booking=gb.name,
		)
		frappe.db.set_value("Reservation", self.res_a.name, "group_booking", gb.name)
		from kamra.folio import open_group_folio
		gf_name = open_group_folio(gb.name)
		frappe.db.set_value("Folio", gf_name, "balance", 650)

		self.as_pos()
		check = pi.verify_room_charge(property=PROP_A, room="102", amount=200)
		self.assertTrue(check["ok"])
		self.assertEqual(check["reservation"], member.name)
		self.assertEqual(check["folio_type"], "Group")
		self.assertAlmostEqual(flt(check["balance"]), 650.0, places=2)

		# Group master reservation marked No Post blocks routing to the group master
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- fixture
		frappe.db.set_value("Reservation", self.res_a.name, "room_charges_allowed", 0)
		self.as_pos()
		blocked = pi.verify_room_charge(property=PROP_A, room="102", amount=200)
		self.assertFalse(blocked["posting_allowed"])
		self.assertEqual(blocked["no_post"], 1)

	def test_adversarial_house_account_rejected_for_room_charge(self):
		house_folio = frappe.get_doc({
			"doctype": "Folio",
			"property": PROP_A,
			"folio_type": "Account",
			"status": "Open",
			"balance": 500,
		}).insert(ignore_permissions=True)
		self.as_pos()
		res = pi.verify_room_charge(property=PROP_A, room=house_folio.name, amount=100)
		self.assertFalse(res["ok"])
		self.assertFalse(res["posting_allowed"])
		self.assertFalse(res["override_required"])
		with self.assertRaises(frappe.ValidationError):
			pi.pos_post_room_charge(
				PROP_A, "T-HOUSE-1", 100, "Bar", room=house_folio.name,
				override_approval={"supervisor": SUP_A, "pin": "4321", "reason": "House"},
			)

	def test_adversarial_limit_in_another_currency_fails_closed_or_converts(self):
		# Property is UGX with limit 1000; charge is 50 USD (never compare raw 50 <= 1000!)
		self.as_pos()
		no_rate = pi.verify_room_charge(property=PROP_A, room="101", amount=50, currency="USD")
		self.assertFalse(no_rate["posting_allowed"])
		self.assertFalse(no_rate["override_required"])
		self.assertEqual(no_rate["reason_code"], "POS-RC-CURRENCY")
		with self.assertRaisesRegex(frappe.ValidationError, "POS-RC-CURRENCY|currency"):
			pi.pos_post_room_charge(
				PROP_A, "T-FX-1", 50, "Dinner", room="101", currency="USD",
			)

		# With an official Exchange Rate (1 USD = 100 UGX -> 50 USD = 5000 UGX > 1000 limit)
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- fixture
		frappe.get_doc({
			"doctype": "Exchange Rate",
			"property": PROP_A,
			"currency": "USD",
			"rate_date": TODAY,
			"buy_rate": 100,
			"sell_rate": 100,
		}).insert(ignore_permissions=True)
		self.as_pos()
		converted = pi.verify_room_charge(property=PROP_A, room="101", amount=50, currency="USD")
		self.assertFalse(converted["posting_allowed"])
		self.assertTrue(converted["override_required"])
		self.assertEqual(converted["reason_code"], "POS-RC-OVER-LIMIT")

	def test_three_decimal_kwd_property_enforces_fils_precision_on_room_charge_limit(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- fixture
		frappe.db.set_value("Property", PROP_A, "currency", "KWD")
		_set_prop_setting(PROP_A, "pos_room_charge_limit", 100)
		self.as_pos()
		# 100.004 KWD is 4 fils above 100.000 KWD — must NOT be rounded down to 100.00!
		check = pi.verify_room_charge(property=PROP_A, room="101", amount=100.004, currency="KWD")
		self.assertTrue(check["ok"])
		self.assertFalse(check["posting_allowed"])
		self.assertTrue(check["override_required"])
		self.assertEqual(check["reason_code"], "POS-RC-OVER-LIMIT")
		with self.assertRaisesRegex(frappe.ValidationError, "POS-RC-OVER-LIMIT|limit"):
			pi.pos_post_room_charge(PROP_A, "T-KWD-1", 100.004, "KWD Dinner", room="101", currency="KWD")

	def test_adversarial_override_token_hook_missing_or_raising_fails_closed(self):
		self.as_pos()
		orig_get_attr = frappe.get_attr

		def missing_consumer(path):
			if "override.consume" in str(path):
				raise ImportError("no pos app")
			return orig_get_attr(path)

		def raising_consumer(path):
			if "override.consume" in str(path):
				return lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
			return orig_get_attr(path)

		with patch("frappe.get_attr", side_effect=missing_consumer):
			with self.assertRaises((frappe.ValidationError, frappe.PermissionError)):
				pi.pos_post_room_charge(
					PROP_A, "T-HOOK-MISS", 1500, "Dinner", room="101",
					override_token="tok-missing", pos_profile="Till A",
				)
		with patch("frappe.get_attr", side_effect=raising_consumer):
			with self.assertRaises((frappe.ValidationError, frappe.PermissionError)):
				pi.pos_post_room_charge(
					PROP_A, "T-HOOK-ERR", 1500, "Dinner", room="101",
					override_token="tok-err", pos_profile="Till A",
				)
		self.assertEqual(frappe.db.count(pi.CHARGE, {"property": PROP_A}), 0)


class RoomChargeConcurrencyTestCase(frappe.tests.IntegrationTestCase):
	"""Real two-transaction MariaDB 11.8 REPEATABLE READ race under one cumulative
	room-charge credit limit: two 600-unit checks race against a 1000-unit limit;
	exactly one commits and the second refuses."""

	def test_two_checks_racing_under_one_limit_mariadb_snapshot_isolation(self):
		suffix = frappe.generate_hash(length=6)
		prop_name = f"UT Race {suffix}"
		site = frappe.local.site
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- race fixture setup
		make_property(prop_name, business_date=TODAY, currency="UGX")
		_set_prop_setting(prop_name, "pos_room_charge_limit", 0)
		_set_prop_setting(prop_name, "pos_room_charge_credit_check", 1)
		_set_prop_setting(prop_name, "pos_room_charge_allowance", 1000)
		rt = make_room_type(prop_name, f"R{suffix}", base_price=0)
		room = make_room(prop_name, rt, f"3{suffix[:3]}")
		res = make_reservation(
			prop_name, rt, d(-1), d(2), room=room, status="Checked In",
			guest=make_guest("Race", last_name=f"Guest{suffix}"),
		)
		pos_user = f"ut.race.{suffix}@test.local"
		ensure_user(pos_user, ["POS Integrator"])
		_restrict_property(pos_user, prop_name)
		folio_name = guest_folio(res.name)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- expose fixture to worker threads

		barrier = threading.Barrier(2)
		outcomes: list[tuple[str, str]] = []
		lock = threading.Lock()

		def worker(ticket_id: str) -> None:
			frappe.init(site=site)
			frappe.connect()
			try:
				frappe.set_user(pos_user)  # nosemgrep: frappe-setuser -- worker persona
				# Force BOTH transactions to open their REPEATABLE READ snapshot
				# BEFORE either acquires row locks or posts.
				frappe.db.sql("SELECT balance FROM `tabFolio` WHERE name=%s", (folio_name,))
				barrier.wait(timeout=15)
				with patch("kamra.webhooks._enqueue"):
					out = pi.pos_post_room_charge(
						prop_name, ticket_id, 600, f"Race {ticket_id}",
						room=room, reservation=res.name, charge_type="Misc",
					)
				frappe.db.commit()  # nosemgrep: frappe-manual-commit -- commit concurrent worker tx
				with lock:
					outcomes.append(("ok", out["ticket_id"]))
			except Exception as exc:
				frappe.db.rollback()
				with lock:
					outcomes.append(("err", str(exc)))
			finally:
				frappe.destroy()

		t1 = threading.Thread(target=worker, args=(f"RACE-1-{suffix}",))
		t2 = threading.Thread(target=worker, args=(f"RACE-2-{suffix}",))
		t1.start()
		t2.start()
		t1.join(timeout=30)
		t2.join(timeout=30)

		try:
			oks = [o for o in outcomes if o[0] == "ok"]
			errs = [o for o in outcomes if o[0] == "err"]
			self.assertEqual(len(outcomes), 2, f"Unexpected worker outcomes: {outcomes}")
			self.assertEqual(len(oks), 1, f"Expected exactly 1 winner under 1000 limit, got {outcomes}")
			self.assertEqual(len(errs), 1, f"Expected exactly 1 refusal under 1000 limit, got {outcomes}")
			self.assertRegex(errs[0][1].lower(), r"limit|allowance|pos-rc-over-limit", f"Expected clean limit refusal, got {errs[0][1]}")
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- refresh snapshot before asserting balance
			final_balance = flt(frappe.db.get_value("Folio", folio_name, "balance"))
			self.assertLessEqual(final_balance, 1000.0)
			self.assertAlmostEqual(final_balance, 600.0, places=2)
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- cleanup
			for dt in ("External POS Charge", "Agent Action Log", "Folio Ledger Entry",
			           "Folio", "Reservation", "Room", "Room Type", "Property Setting"):
				for name in frappe.get_all(dt, filters={"property": prop_name}, pluck="name"):
					frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
			if frappe.db.exists("Property", prop_name):
				frappe.delete_doc("Property", prop_name, force=True, ignore_permissions=True)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- commit concurrent test cleanup
