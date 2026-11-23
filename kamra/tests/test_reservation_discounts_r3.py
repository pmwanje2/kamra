"""Reservation discounts, round 3 (Gap Record #8555, round-2 adversarial review).

- P0-A a night's typed price (night_rates.hand_price) is part of its locked
  price: a plain save (REST PUT) cannot change it and a plain insert (REST
  POST) cannot type one. Only create_booking, after _check_night_prices,
  books typed nights.
- P0-B a discounted stay re-priced by an agreed rate, a custom-rate room
  move or a party change takes the discount rules again, on every night
  still to come; a night whose locked price changed for any other reason is
  judged too. An agreed rate stacked on a discount is judged as the whole
  cut from the base the stay was booked at, so neither the front desk's
  limit nor the full-comp sign-off can be stepped around.
- P2-C a room move's audit row names the supervisor who signed the
  discount off.
- P2-D a booking retried after a deadlock is inserted without the first
  attempt's night rows (HSP-TARIFF-007), and typed nights still book."""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

import frappe
from frappe.utils import flt

from kamra import api
from kamra.tests.audit_fixtures import (
	PROP_A,
	d,
	make_guest,
	make_reservation,
	make_room,
	make_room_type,
)
from kamra.tests.test_reservation_discounts import DESK, GM, SELF, set_setting
from kamra.tests.test_reservation_discounts_r2 import PROMO, RoundTwoTestCase

TARIFF_RESOLVER = "kamra.tests.test_reservation_discounts_r2.refuse_locked_nights_on_insert"


class HandPriceLockTests(RoundTwoTestCase):
	"""P0-A: a typed nightly price is locked like the rest of a night's price."""

	def test_a_plain_save_cannot_type_a_price_on_an_auto_priced_night(self):
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0])
		doc = self.doc(res.name)
		doc.night_rates[0].hand_price = 1
		with self.assertRaises(frappe.PermissionError) as raised:
			self.as_user(DESK, doc.save, ignore_permissions=True)
		self.assertIn(d(5), str(raised.exception))
		self.assertEqual(self.rows(res.name), [(1000, 0, 1000), (1000, 0, 1000)])
		self.assertEqual([flt(r.hand_price) for r in self.doc(res.name).night_rates], [0, 0])

	def test_a_plain_save_cannot_retype_a_hand_priced_night(self):
		out = self.as_user(GM, api.create_booking, PROP_A, self.rt, d(5), d(7), "UT Typed Guest",
		                   night_prices=[{"date": d(5), "price": 1200}])
		name = out["reservation"]
		self.assertEqual([flt(r.hand_price) for r in self.doc(name).night_rates], [1200, 0])
		doc = self.doc(name)
		doc.night_rates[0].hand_price = 1
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, doc.save, ignore_permissions=True)
		after = self.doc(name)
		self.assertEqual([flt(r.hand_price) for r in after.night_rates], [1200, 0])
		self.assertEqual([flt(r.rate) for r in after.night_rates], [1200, 1000])

	def test_a_plain_insert_cannot_type_a_nightly_price(self):
		# each case in a room of its own, so one never overlaps the other
		cases = {0: self.rooms[1], 1: self.rooms[2]}
		for auto_price, room in cases.items():
			with self.subTest(auto_price=auto_price):
				doc = frappe.get_doc({
					"doctype": "Reservation", "property": PROP_A, "room_type": self.rt,
					"guest": make_guest(), "status": "Confirmed", "room": room,
					"check_in_date": d(5), "check_out_date": d(7), "adults": 2,
					"auto_price": auto_price,
					"night_rates": [{"stay_date": d(5), "hand_price": 1}]})
				with self.assertRaises(frappe.PermissionError) as raised:
					doc.insert(ignore_permissions=True)
				self.assertIn(d(5), str(raised.exception))
		self.assertFalse(frappe.db.exists("Reservation", {
			"property": PROP_A, "room": ("in", list(cases.values())), "check_in_date": d(5)}))


class StackedRateTests(RoundTwoTestCase):
	"""P0-B: an agreed rate or a party change on a discounted stay."""

	def setUp(self):
		super().setUp()
		set_setting(PROP_A, "rate_override_limit_pct", 15)

	def agree(self, name, rate, user=DESK, **kw):
		return self.as_user(user, api.set_agreed_rate, name, rate, "RATE-PROMO",
		                    confirm_price=1, **kw)

	def test_an_agreed_rate_cannot_turn_a_managers_discount_into_an_unsigned_comp(self):
		name = self.book(GM, discount_type="Amount", discount_value=500,
		                 discount_reason="RATE-PROMO")["reservation"]
		self.assertEqual(self.rows(name), [(1000, 500, 500)] * 2)
		with self.assertRaises(frappe.PermissionError):
			self.agree(name, 500, GM)
		self.assertEqual(self.rows(name), [(1000, 500, 500)] * 2)
		with self.assertRaises(frappe.PermissionError) as raised:
			self.agree(name, 500, GM, supervisor=GM, pin="4321")
		self.assertTrue(str(raised.exception).startswith("SUPERVISOR_SELF_APPROVAL:"))
		self.desk_with_finance()
		# the preview asks for someone else's PIN before the desk confirms
		pv = self.as_user(GM, api.rate_override_preview, name, 500)
		self.assertTrue(pv["needs_supervisor"])
		self.assertTrue(pv["discount_governance"]["comp"])
		self.assertNotIn(GM, [s["user"] for s in pv["supervisors"]])
		self.assertIn(SELF, [s["user"] for s in pv["supervisors"]])
		out = self.agree(name, 500, GM, supervisor=SELF, pin="5678")
		self.assertEqual(self.rows(name), [(500, 500, 0)] * 2)
		self.assertEqual(out["approver"], SELF)
		logs = frappe.get_all("Agent Action Log", filters={
			"action_type": "rate_override", "reference_name": name}, pluck="approver")
		self.assertEqual(logs, [SELF])

	def test_a_custom_rate_room_move_cannot_turn_a_discount_into_an_unsigned_comp(self):
		name = self.book(GM, discount_type="Amount", discount_value=500,
		                 discount_reason="RATE-PROMO")["reservation"]
		room = self.doc(name).room
		other = next(r for r in self.rooms if r != room)
		with self.assertRaises(frappe.PermissionError):
			self.as_user(GM, api.move_reservation, name, other, rate_mode="custom",
			             agreed_rate=500, confirm_price=1)
		self.assertEqual(self.doc(name).room, room)
		self.assertEqual(self.rows(name), [(1000, 500, 500)] * 2)
		self.desk_with_finance()
		self.as_user(GM, api.move_reservation, name, other, rate_mode="custom",
		             agreed_rate=500, confirm_price=1, supervisor_user=SELF, supervisor_pin="5678")
		self.assertEqual(self.rows(name), [(500, 500, 0)] * 2)

	def test_the_front_desk_cannot_stack_an_agreed_rate_past_its_limit(self):
		name = self.book(DESK, discount_type="Amount", discount_value=100,
		                 discount_reason="RATE-PROMO")["reservation"]
		self.assertEqual(self.rows(name), [(1000, 100, 900)] * 2)
		# 800 less the 100 off is 700: 30% off the 1,000 it was booked at
		with self.assertRaises(frappe.PermissionError):
			self.agree(name, 800)
		self.assertEqual(self.rows(name), [(1000, 100, 900)] * 2)
		pv = self.as_user(DESK, api.rate_override_preview, name, 800)
		self.assertTrue(pv["needs_supervisor"])
		self.assertAlmostEqual(pv["discount_governance"]["max_pct"], 30, places=2)
		# the desk is told the whole cut, not the 11% the rate alone takes
		self.assertAlmostEqual(pv["reduction_pct"], 30, places=2)
		out = self.agree(name, 800, supervisor=GM, pin="4321")
		self.assertEqual(self.rows(name), [(800, 100, 700)] * 2)
		self.assertEqual(out["approver"], GM)

	def test_a_party_change_takes_the_discount_rules_again(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0])
		self.discount(res.name, discount_type="Percent", discount_value=30,
		              supervisor_user=GM, supervisor_pin="4321", **PROMO)
		doc = self.doc(res.name)
		doc.adults = 1
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, doc.save, ignore_permissions=True)
		self.assertEqual(self.doc(res.name).adults, 2)

	def test_a_night_re_priced_by_any_other_change_takes_the_rules_again(self):
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0])
		self.discount(res.name, discount_type="Amount", discount_value=100, **PROMO)
		self.assertEqual(self.rows(res.name), [(1000, 100, 900)] * 2)
		# the rate card falls after booking, then a meal plan re-prices the
		# stay: 100 off a 400 night is 25%, over the desk's 15%
		frappe.db.set_value("Room Type", self.rt, "base_price", 400)
		doc = self.doc(res.name)
		doc.meal_plan = self.meal_plan(300)
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, doc.save, ignore_permissions=True)
		self.assertEqual(self.rows(res.name), [(1000, 100, 900)] * 2)


class RoomMoveAuditTests(RoundTwoTestCase):
	"""P2-C: the room move's audit row carries the discount's approver."""

	def test_a_room_moves_audit_row_names_the_discount_approver(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		dlx = make_room_type(PROP_A, "DLX", base_price=1200)
		frappe.db.set_value("Room Type", dlx, "base_price", 1200)
		suite = make_room(PROP_A, dlx, "201")
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0])
		self.discount(res.name, discount_type="Percent", discount_value=30,
		              supervisor_user=GM, supervisor_pin="4321", **PROMO)
		self.as_user(DESK, api.move_reservation, res.name, suite, confirm_price=1,
		             supervisor_user=GM, supervisor_pin="4321")
		self.assertEqual(self.rows(res.name), [(1200, 360, 840)] * 2)
		logs = frappe.get_all("Agent Action Log", filters={
			"action_type": "room_move", "reference_name": res.name},
			fields=["approver", "rationale"])
		self.assertEqual([row.approver for row in logs], [GM])
		self.assertIn(GM, logs[0].rationale)


class DeadlockRetryTests(RoundTwoTestCase):
	"""P2-D: create_booking's deadlock retry rebuilds a clean insert."""

	def deadlock_once(self, stack: ExitStack) -> dict:
		from kamra.kamra.doctype.reservation.reservation import Reservation
		real = Reservation.db_insert
		state = {"raised": 0}

		def flaky(doc, *args, **kwargs):
			if not state["raised"]:
				state["raised"] = 1
				raise frappe.QueryDeadlockError("Deadlock found when trying to get lock")
			return real(doc, *args, **kwargs)

		stack.enter_context(patch.object(Reservation, "db_insert", flaky))
		# keep the test's own savepoint: the retry's rollback is what is under test
		stack.enter_context(patch.object(frappe.local.db, "rollback"))
		return state

	def tariff_resolver(self, stack: ExitStack) -> None:
		hooks = frappe.get_doc_hooks().get("Reservation", {})
		events = {**hooks, "before_validate": [*hooks.get("before_validate", []), TARIFF_RESOLVER]}
		stack.enter_context(patch.dict(frappe.get_doc_hooks(), Reservation=events))

	def test_a_retried_booking_is_inserted_without_the_first_attempts_night_rows(self):
		# typed nights still book on the retry, at the typed price
		with ExitStack() as stack:
			state = self.deadlock_once(stack)
			typed = self.as_user(GM, api.create_booking, PROP_A, self.rt, d(5), d(7),
			                     "UT Retry Typed", night_prices=[{"date": d(5), "price": 1200}])
		self.assertEqual(state["raised"], 1)
		res = self.doc(typed["reservation"])
		self.assertEqual([flt(r.hand_price) for r in res.night_rates], [1200, 0])
		self.assertEqual([flt(r.rate) for r in res.night_rates], [1200, 1000])
		# a booking with no typed nights reaches a Before Validate tariff
		# resolver with no night rows, on the retry as on the first try
		with ExitStack() as stack:
			state = self.deadlock_once(stack)
			self.tariff_resolver(stack)
			out = self.book(discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO")
		self.assertEqual(state["raised"], 1)
		self.assertEqual(self.rows(out["reservation"]), [(1000, 100, 900)] * 2)
