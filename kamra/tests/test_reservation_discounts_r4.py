"""Reservation discounts, round 4 (Gap Record #8555; R3 code review and the
SAP S/4HANA / Oracle OPERA Cloud parity check).

- R4-1 a plain save or insert (REST PUT / POST) never sets, changes or clears
  an agreed rate: set_agreed_rate, a room move, a group pickup and a
  contract tariff are its writers.
- R4-2 a plain insert never carries its own night rows or a hand price:
  create_booking (typed nights, an OTA total), the channel manager and the
  imports are their writers.
- R4-3 an agreed rate's cut is the whole cut from each night's rate-code
  amount, however many agreed rates came before, and is held to the rate
  guardrail floor; a discount after an agreed rate is measured the same way.
- R4-4 a full comp leaves the cleaning fee, the pet charge and a taxable levy
  taxed at the room's own rate, as the folio posts them.
- R4-5 an OTA booking retried after a deadlock keeps the channel's nights.
- R4-6 correct_charged_nights hands its approver to the discount rules, and
  that approver is never the person correcting.
- R4-7 the date-amendment and room-move previews say when the discount
  needs a supervisor, and who can sign it off.
- R4-8 an agreed rate (set_agreed_rate, a custom-rate move) replaces the
  desk's discount (OPERA Fixed Rate), and a desk agreed rate never waives a
  plan that takes no discount.
- R4-9 a meal plan change keeps the booked room rate; a party change keeps
  a fixed rate; amend_stay changes the party with a supervisor's sign-off.
- R4-10 the discount's audit rows carry each night's daily details.
- R4-15 the discount takes an optional note; discount_amount says its basis.
- R4-16 Frappe v16 never fills doc.flags from a REST payload, so no payload
  reaches a writer flag."""

from __future__ import annotations

import json
from contextlib import ExitStack
from unittest.mock import patch

import frappe
from frappe.utils import flt

from kamra import api, pricing, settings
from kamra import folio as folio_mod
from kamra.tests.audit_fixtures import (
	PROP_A,
	d,
	ensure_user,
	make_guest,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)
from kamra.tests.test_reservation_discounts import DESK, GM, REVENUE, SELF, set_setting
from kamra.tests.test_reservation_discounts_r2 import PROMO, RoundTwoTestCase

FIN = "ut.disc.finance@test.local"
PROP_V = "UT Disc VAT"


def rate_codes(doc) -> list[float]:
	return [round(flt(r.get("rate_code_amount")), 2) for r in doc.night_rates]


class RoundFourTestCase(RoundTwoTestCase):
	def setUp(self):
		super().setUp()
		set_setting(PROP_A, "rate_override_limit_pct", 15)

	def agree(self, name, rate, user=DESK, **kw):
		return self.as_user(user, api.set_agreed_rate, name, rate, "RATE-PROMO",
		                    confirm_price=1, **kw)

	def stay(self, room=0, **fields):
		return make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[room], **fields)

	def new_doc(self, **fields):
		return frappe.get_doc({
			"doctype": "Reservation", "property": PROP_A, "room_type": self.rt,
			"guest": make_guest(), "status": "Confirmed", "room": self.rooms[3],
			"check_in_date": d(5), "check_out_date": d(7), "adults": 2, **fields})

	def finance(self):
		ensure_user(FIN, ["Finance"])
		frappe.db.delete("Cashier PIN", {"name": FIN})
		frappe.get_doc({"doctype": "Cashier PIN", "user": FIN, "pin": "2468"}
		               ).insert(ignore_permissions=True)


class AgreedRatePlainSaveTests(RoundFourTestCase):
	"""R4-1: an agreed rate is set only through its governed writers."""

	def test_a_plain_save_cannot_set_an_agreed_rate_on_an_undiscounted_stay(self):
		res = self.stay()
		doc = self.doc(res.name)
		doc.agreed_rate = 1
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, doc.save, ignore_permissions=True)
		self.assertEqual(flt(self.doc(res.name).agreed_rate), 0)
		self.assertEqual(self.rows(res.name), [(1000, 0, 1000)] * 2)

	def test_a_plain_save_cannot_change_or_clear_an_agreed_rate(self):
		res = self.stay()
		self.agree(res.name, 900)
		for value in (850, 0):
			with self.subTest(agreed_rate=value):
				doc = self.doc(res.name)
				doc.agreed_rate = value
				with self.assertRaises(frappe.PermissionError):
					self.as_user(DESK, doc.save, ignore_permissions=True)
		self.assertEqual(flt(self.doc(res.name).agreed_rate), 900)
		self.assertEqual(self.rows(res.name), [(900, 0, 900)] * 2)

	def test_a_plain_insert_cannot_carry_an_agreed_rate(self):
		doc = self.new_doc(auto_price=1, agreed_rate=1)
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, doc.insert, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Reservation", {
			"property": PROP_A, "room": self.rooms[3], "check_in_date": d(5)}))


class HandPricedInsertTests(RoundFourTestCase):
	"""R4-2: a new booking's price comes from its own pricing or a governed writer."""

	def test_a_plain_insert_cannot_lock_its_own_night_rates(self):
		for auto_price in (0, 1):
			with self.subTest(auto_price=auto_price):
				doc = self.new_doc(auto_price=auto_price, amount_after_tax=0, night_rates=[
					{"stay_date": d(5), "rate": 0, "gst_rate": 0},
					{"stay_date": d(6), "rate": 0, "gst_rate": 0}])
				with self.assertRaises(frappe.PermissionError) as raised:
					self.as_user(DESK, doc.insert, ignore_permissions=True)
				self.assertIn(d(5), str(raised.exception))
		self.assertFalse(frappe.db.exists("Reservation", {
			"property": PROP_A, "room": self.rooms[3], "check_in_date": d(5)}))

	def test_a_plain_insert_cannot_price_a_stay_by_hand(self):
		doc = self.new_doc(auto_price=0, amount_after_tax=1)
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, doc.insert, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Reservation", {
			"property": PROP_A, "room": self.rooms[3], "check_in_date": d(5)}))


class RateCodeCutTests(RoundFourTestCase):
	"""R4-3: every cut is measured from each night's rate-code amount."""

	def test_chained_agreed_rates_are_measured_from_the_rate_code_amount(self):
		res = self.stay()
		self.agree(res.name, 850)  # 15% off 1,000: within the desk's limit
		self.assertEqual(self.rows(res.name), [(850, 0, 850)] * 2)
		self.assertEqual(rate_codes(self.doc(res.name)), [1000, 1000])
		# 800 is 5.9% off 850 but 20% off the 1,000 rate-code amount
		pv = self.as_user(DESK, api.rate_override_preview, res.name, 800)
		self.assertAlmostEqual(pv["reduction_pct"], 20, places=2)
		self.assertTrue(pv["needs_supervisor"])
		with self.assertRaises(frappe.PermissionError):
			self.agree(res.name, 800)
		self.assertEqual(self.rows(res.name), [(850, 0, 850)] * 2)
		out = self.agree(res.name, 800, supervisor=GM, pin="4321")
		self.assertEqual(out["approver"], GM)
		self.assertEqual(self.rows(res.name), [(800, 0, 800)] * 2)
		self.assertEqual(rate_codes(self.doc(res.name)), [1000, 1000])

	def test_an_agreed_rate_under_the_rate_floor_needs_a_supervisor(self):
		frappe.get_doc({"doctype": "Rate Guardrail", "property": PROP_A,
		                "floor_price": 900, "ceiling_price": 5000}).insert(ignore_permissions=True)
		res = self.stay()
		pv = self.as_user(DESK, api.rate_override_preview, res.name, 880)
		self.assertAlmostEqual(pv["reduction_pct"], 12, places=2)
		self.assertEqual(pv["floor_breach"], [d(5), d(6)])
		self.assertTrue(pv["needs_supervisor"])
		with self.assertRaises(frappe.PermissionError):
			self.agree(res.name, 880)
		self.assertEqual(self.rows(res.name), [(1000, 0, 1000)] * 2)
		self.agree(res.name, 880, supervisor=GM, pin="4321")
		self.assertEqual(self.rows(res.name), [(880, 0, 880)] * 2)

	def test_a_discount_after_an_agreed_rate_is_measured_from_the_rate_code_amount(self):
		res = self.stay()
		self.agree(res.name, 900)
		# 10% off 900 is 810: 19% off the 1,000 rate-code amount
		pv = self.as_user(DESK, api.discount_preview, res.name, discount_type="Percent",
		                  discount_value=10, **PROMO)
		self.assertAlmostEqual(pv["max_pct"], 19, places=2)
		self.assertTrue(pv["needs_supervisor"])
		with self.assertRaises(frappe.PermissionError):
			self.discount(res.name, discount_type="Percent", discount_value=10, **PROMO)
		self.assertEqual(self.rows(res.name), [(900, 0, 900)] * 2)
		self.discount(res.name, discount_type="Percent", discount_value=10,
		              supervisor_user=GM, supervisor_pin="4321", **PROMO)
		self.assertEqual(self.rows(res.name), [(900, 90, 810)] * 2)


class CompTaxTests(RoundFourTestCase):
	"""R4-4: a full comp still taxes what is taxed like the room."""

	properties = (PROP_A, PROP_V)

	def setUp(self):
		super().setUp()
		make_property(PROP_V, country="United Arab Emirates", cleaning_fee=500,
		              room_levy_per_night=100, room_levy_taxable=1, cleaning_fee_taxable=1)
		self.vat_rt = make_room_type(PROP_V, "STD", base_price=1000)
		frappe.db.set_value("Room Type", self.vat_rt, {"base_price": 1000, "tax_percent": 0})
		frappe.clear_document_cache("Room Type", self.vat_rt)

	def comp_quote(self, **kw):
		return pricing.quote(PROP_V, self.vat_rt, d(5), d(7), discount_type="Percent",
		                     discount_value=100, **kw)

	def test_a_full_comp_keeps_the_cleaning_fee_and_levy_taxed_at_the_room_rate(self):
		full = pricing.quote(PROP_V, self.vat_rt, d(5), d(7))
		rate = flt(full["nightly"][0]["gst_rate"])
		self.assertGreater(rate, 0)
		comp = self.comp_quote()
		self.assertEqual([flt(n["rate"]) for n in comp["nightly"]], [0, 0])
		# the night's own tax rate is still the room's: the folio posts the
		# levy at it (folio._nightly_gst reads the locked night)
		self.assertEqual([flt(n["gst_rate"]) for n in comp["nightly"]], [rate, rate])
		for q in (full, comp):
			with self.subTest(room_total=q["room_total"]):
				cleaning = self.line(q, "cleaning_fee")
				self.assertAlmostEqual(cleaning["tax_amount"], 500 * rate / 100, places=2)
				levy = self.line(q, "room_levy")
				self.assertAlmostEqual(levy["amount"], 200, places=2)
				self.assertAlmostEqual(levy["tax_amount"], 200 * rate / 100, places=2)
		self.assertAlmostEqual(comp["amount_after_tax"], 700 * (1 + rate / 100), places=2)


class OtaDeadlockRetryTests(RoundFourTestCase):
	"""R4-5: a retried OTA booking keeps the channel's own nights."""

	def deadlock_once(self, stack: ExitStack) -> dict:
		# as test_reservation_discounts_r3.DeadlockRetryTests: the first
		# insert deadlocks, and the test keeps its own savepoint
		from kamra.kamra.doctype.reservation.reservation import Reservation
		real = Reservation.db_insert
		state = {"raised": 0}

		def flaky(doc, *args, **kwargs):
			if not state["raised"]:
				state["raised"] = 1
				raise frappe.QueryDeadlockError("Deadlock found when trying to get lock")
			return real(doc, *args, **kwargs)

		stack.enter_context(patch.object(Reservation, "db_insert", flaky))
		stack.enter_context(patch.object(frappe.local.db, "rollback"))
		return state

	def test_a_retried_ota_booking_keeps_the_channels_nights(self):
		with ExitStack() as stack:
			state = self.deadlock_once(stack)
			out = self.as_user(DESK, api.create_booking, PROP_A, self.rt, d(5), d(7),
			                   "UT Retry OTA", booked_via="Booking.com", ota_ref="UT-R4-OTA-1",
			                   ota_total=2600)
		self.assertEqual(state["raised"], 1)
		res = self.doc(out["reservation"])
		self.assertEqual(int(res.auto_price), 0)
		self.assertEqual([str(r.stay_date) for r in res.night_rates], [d(5), d(6)])
		gross = sum(flt(r.rate) * (1 + flt(r.gst_rate) / 100) for r in res.night_rates)
		self.assertAlmostEqual(gross, 2600, delta=0.05)


class CorrectionSignOffTests(RoundFourTestCase):
	"""R4-6: a back-dated rate correction signs the discount off too."""

	def setUp(self):
		super().setUp()
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		settings.write_kv(PROP_A, "charged_night_rate_change", "Correct back-dated rates")
		settings.clear_cache(PROP_A)
		self.res = make_reservation(PROP_A, self.rt, d(-2), d(2), status="Checked In",
		                            room=self.rooms[0])
		for day in (d(-2), d(-1)):
			folio_mod.post_room_night(self.doc(self.res.name), day)

	def correct(self, user, **kw):
		return self.as_user(user, api.correct_charged_nights, self.res.name,
		                    reason="Wrong tariff", **kw)

	def test_a_finance_correction_of_an_over_limit_stay_is_signed_off_by_another_supervisor(self):
		self.discount(self.res.name, discount_type="Percent", discount_value=30,
		              supervisor_user=GM, supervisor_pin="4321", **PROMO)
		self.finance()
		frappe.db.set_value("Room Type", self.rt, "base_price", 1200)
		frappe.clear_document_cache("Room Type", self.rt)
		# the person correcting never signs their own discount off
		with self.assertRaises(frappe.PermissionError) as raised:
			self.correct(FIN, pin="2468")
		self.assertIn("SUPERVISOR_SELF_APPROVAL", str(raised.exception))
		out = self.correct(FIN, supervisor=GM, pin="4321")
		self.assertTrue(out["ok"])
		self.assertEqual(self.rows(self.res.name)[2:], [(1200, 360, 840)] * 2)

	def test_a_correction_of_a_stay_with_a_comp_night_to_come_is_signed_off(self):
		self.desk_with_finance()
		self.discount(self.res.name, **PROMO, night_discounts=[
			{"date": d(1), "discount_type": "Percent", "discount_value": 100,
			 "discount_reason": "ADJ-RATE"}], supervisor_user=GM, supervisor_pin="4321")
		frappe.db.set_value("Room Type", self.rt, "base_price", 1200)
		frappe.clear_document_cache("Room Type", self.rt)
		with self.assertRaises(frappe.PermissionError) as raised:
			self.correct(GM, pin="4321")
		self.assertIn("SUPERVISOR_SELF_APPROVAL", str(raised.exception))
		out = self.correct(GM, supervisor=SELF, pin="5678")
		self.assertTrue(out["ok"])
		self.assertEqual(self.rows(self.res.name)[3], (1200, 1200, 0))


class RepricePreviewSignOffTests(RoundFourTestCase):
	"""R4-7: the previews name the discount's sign-off before the desk confirms."""

	def setUp(self):
		super().setUp()
		set_setting(PROP_A, "rate_override_limit_pct", 10)

	def over_limit(self):
		res = self.stay()
		self.discount(res.name, discount_type="Percent", discount_value=30,
		              supervisor_user=GM, supervisor_pin="4321", **PROMO)
		return res

	def assert_asks_for_a_supervisor(self, pv):
		self.assertTrue(pv.get("needs_supervisor"))
		gov = pv.get("discount_governance") or {}
		self.assertTrue(gov.get("needs_supervisor"))
		self.assertAlmostEqual(gov.get("max_pct"), 30, places=2)
		users = [s["user"] for s in pv.get("supervisors") or []]
		self.assertIn(GM, users)
		self.assertNotIn(DESK, users)

	def test_the_amend_preview_names_the_discounts_sign_off(self):
		res = self.over_limit()
		self.assert_asks_for_a_supervisor(
			self.as_user(DESK, api.amend_stay_preview, res.name, d(5), d(8)))

	def test_the_room_move_preview_names_the_discounts_sign_off(self):
		res = self.over_limit()
		dlx = make_room_type(PROP_A, "DLX", base_price=1200)
		frappe.db.set_value("Room Type", dlx, "base_price", 1200)
		suite = make_room(PROP_A, dlx, "201")
		self.assert_asks_for_a_supervisor(
			self.as_user(DESK, api.room_move_preview, res.name, suite))

	def test_a_discount_within_the_limit_asks_for_nobody(self):
		res = self.stay()
		self.discount(res.name, discount_type="Percent", discount_value=5, **PROMO)
		pv = self.as_user(DESK, api.amend_stay_preview, res.name, d(5), d(8))
		self.assertIn("discount_governance", pv)
		self.assertFalse(pv["needs_supervisor"])
		self.assertEqual(pv["supervisors"], [])


class AgreedRateReplacesDiscountTests(RoundFourTestCase):
	"""R4-8: OPERA's Fixed Rate replaces the desk's discount."""

	def test_an_agreed_rate_replaces_the_stays_discount(self):
		name = self.book(GM, discount_type="Amount", discount_value=500,
		                 discount_reason="RATE-PROMO")["reservation"]
		self.assertEqual(self.rows(name), [(1000, 500, 500)] * 2)
		out = self.agree(name, 600, GM)
		self.assertIsNone(out["approver"])
		doc = self.doc(name)
		self.assertEqual(self.rows(name), [(600, 0, 600)] * 2)
		self.assertEqual((doc.discount_type or None, flt(doc.discount_value),
		                  doc.discount_reason or None, int(doc.fixed_rate)),
		                 (None, 0, None, 1))
		log = frappe.get_all("Agent Action Log", filters={
			"action_type": "rate_override", "reference_name": name},
			fields=["before_snapshot", "after_snapshot"])[0]
		before = json.loads(log.before_snapshot)
		self.assertEqual((before["discount_type"], before["discount_value"]), ("Amount", 500))
		self.assertEqual([(n["date"], n["discount_amount"]) for n in before["nights"]],
		                 [(d(5), 500), (d(6), 500)])
		self.assertEqual(json.loads(log.after_snapshot)["discount_type"], None)

	def test_the_new_rate_a_night_is_what_the_guest_pays(self):
		name = self.book(DESK, discount_type="Percent", discount_value=10,
		                 discount_reason="RATE-PROMO", night_discounts=[
			                 {"date": d(6), "discount_type": "Amount", "discount_value": 50}]
		                 )["reservation"]
		self.assertEqual(self.rows(name), [(1000, 100, 900), (1000, 50, 950)])
		pv = self.as_user(DESK, api.rate_override_preview, name, 880)
		self.assertEqual([n["rate"] for n in pv["nights"]], [880, 880])
		self.agree(name, 880)
		self.assertEqual(self.rows(name), [(880, 0, 880)] * 2)
		self.assertFalse(self.doc(name).has_discount())

	def test_a_desk_agreed_rate_never_waives_a_plan_that_takes_no_discount(self):
		plan = self.plan("BAR")
		res = self.stay(rate_plan=plan)
		self.agree(res.name, 950)
		self.discount(res.name, discount_type="Percent", discount_value=5, **PROMO)
		self.assertEqual(self.rows(res.name), [(950, 47.5, 902.5)] * 2)
		frappe.db.set_value("Rate Plan", plan, "discount_eligible", 0)
		with self.assertRaises(frappe.ValidationError) as raised:
			self.as_user(DESK, api.amend_stay, res.name, d(5), d(8), confirm_price=1)
		self.assertIn("can't be discounted", str(raised.exception))
		self.assertEqual(len(self.doc(res.name).night_rates), 2)


class PartyAndMealPlanTests(RoundFourTestCase):
	"""R4-9: what a party or meal plan change does to a booked room rate."""

	def test_a_meal_plan_change_keeps_the_booked_room_rate(self):
		res = self.stay()
		self.discount(res.name, discount_type="Amount", discount_value=100, **PROMO)
		frappe.db.set_value("Room Type", self.rt, "base_price", 400)
		frappe.clear_document_cache("Room Type", self.rt)
		doc = self.doc(res.name)
		doc.meal_plan = self.meal_plan(300)
		self.as_user(DESK, doc.save, ignore_permissions=True)
		after = self.doc(res.name)
		self.assertEqual(self.rows(res.name), [(1000, 100, 900)] * 2)
		self.assertEqual([flt(r.meal_amount) for r in after.night_rates], [600, 600])
		snap = self.snapshot(res.name)
		self.assertAlmostEqual(snap["meal_total"], 1200, places=2)
		self.assertAlmostEqual(flt(after.amount_after_tax), snap["amount_after_tax"], places=2)

	def test_a_party_change_keeps_a_fixed_rate(self):
		frappe.db.set_value("Room Type", self.rt, "single_occupancy_price", 800)
		frappe.clear_document_cache("Room Type", self.rt)
		res = self.stay()
		self.discount(res.name, GM, discount_type="Percent", discount_value=30, fixed_rate=1,
		              **PROMO)
		self.assertEqual(int(self.doc(res.name).fixed_rate), 1)
		doc = self.doc(res.name)
		doc.adults = 1
		self.as_user(DESK, doc.save, ignore_permissions=True)
		self.assertEqual(self.doc(res.name).adults, 1)
		self.assertEqual(self.rows(res.name), [(1000, 300, 700)] * 2)

	def test_amend_stay_changes_the_party_with_a_supervisors_sign_off(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		frappe.db.set_value("Room Type", self.rt, "single_occupancy_price", 800)
		frappe.clear_document_cache("Room Type", self.rt)
		res = self.stay()
		self.discount(res.name, discount_type="Percent", discount_value=30,
		              supervisor_user=GM, supervisor_pin="4321", **PROMO)
		pv = self.as_user(DESK, api.amend_stay_preview, res.name, d(5), d(7), adults=1)
		self.assertTrue(pv["needs_supervisor"])
		self.assertEqual([n["rate"] for n in pv["nights"]], [560, 560])
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, api.amend_stay, res.name, d(5), d(7), confirm_price=1, adults=1)
		self.assertEqual(self.doc(res.name).adults, 2)
		self.as_user(DESK, api.amend_stay, res.name, d(5), d(7), confirm_price=1, adults=1,
		             supervisor_user=GM, supervisor_pin="4321")
		self.assertEqual(self.doc(res.name).adults, 1)
		self.assertEqual(self.rows(res.name), [(800, 240, 560)] * 2)


class DailyDetailsAuditTests(RoundFourTestCase):
	"""R4-10: the audit row of a discount carries each night, before and after."""

	def test_the_discount_log_carries_each_nights_daily_details(self):
		res = self.stay()
		self.discount(res.name, discount_type="Percent", discount_value=10, **PROMO,
		              night_discounts=[{"date": d(6), "discount_type": "Amount",
		                                "discount_value": 50}])
		self.discount(res.name, discount_type="Percent", discount_value=5, **PROMO)
		logs = frappe.get_all("Agent Action Log", filters={
			"action_type": "reservation_discount", "reference_name": res.name},
			fields=["before_snapshot", "after_snapshot"], order_by="creation asc")
		before, after = (json.loads(logs[-1][k]) for k in ("before_snapshot", "after_snapshot"))
		self.assertEqual(
			[(n["date"], n["base_rate"], n["discount_amount"], n["rate"]) for n in before["nights"]],
			[(d(5), 1000, 100, 900), (d(6), 1000, 50, 950)])
		self.assertEqual(
			[(n["date"], n["base_rate"], n["discount_amount"], n["rate"]) for n in after["nights"]],
			[(d(5), 1000, 50, 950), (d(6), 1000, 50, 950)])
		self.assertEqual(before["nights"][1]["discount_type"], "Amount")


class DiscountNoteTests(RoundFourTestCase):
	"""R4-15: a free-text note beside the reason code, and the field's basis."""

	def test_a_discount_takes_an_optional_note_onto_its_audit_row(self):
		res = self.stay()
		self.discount(res.name, discount_type="Percent", discount_value=10, **PROMO,
		              discount_note="Guest waited 40 minutes at check-in")
		self.assertIn("Guest waited 40 minutes at check-in", self.discount_logs(res.name)[0].rationale)
		out = self.book(discount_type="Percent", discount_value=5, discount_reason="RATE-PROMO",
		                discount_note="Returning corporate guest")
		self.assertIn("Returning corporate guest", self.discount_logs(out["reservation"])[0].rationale)

	def test_discount_amount_says_it_is_on_the_basis_the_desk_entered(self):
		field = frappe.get_meta("Reservation Night Rate").get_field("discount_amount")
		self.assertIn("gross where", field.description)


class RestFlagTests(RoundFourTestCase):
	"""R4-16: on Frappe v16 a REST payload's `flags` reach no writer flag."""

	def rest(self, fn, *args, **data):
		# api/v1 reads its payload from form_dict.data; api/v2 from form_dict
		payload = frappe._dict({"data": json.dumps(data)} if fn.__module__.endswith("v1") else data)

		def request():
			frappe.local.form_dict = payload  # frappe.set_user clears form_dict
			return fn(*args)
		try:
			return self.as_user(DESK, request)
		finally:
			frappe.local.form_dict = frappe._dict()

	def payload(self, **fields):
		return {"property": PROP_A, "room_type": self.rt, "guest": make_guest(),
		        "status": "Confirmed", "room": self.rooms[3], "check_in_date": d(5),
		        "check_out_date": d(7), "adults": 2, **fields}

	def test_a_rest_insert_cannot_reach_a_night_writer_flag(self):
		from frappe.api import v2
		for case, fields in (
				("typed price", dict(night_rates=[{"stay_date": d(5), "hand_price": 1}],
				                     flags={"kamra_night_prices_writer": 1})),
				("locked rows", dict(auto_price=0, night_rates=[{"stay_date": d(5), "rate": 0}],
				                     flags={"kamra_night_rates_writer": 1})),
				("hand price", dict(auto_price=0, amount_after_tax=1,
				                    flags={"kamra_night_rates_writer": 1})),
				("agreed rate", dict(agreed_rate=1, flags={"kamra_agreed_rate_override": 1}))):
			with self.subTest(case=case):
				with self.assertRaises(frappe.PermissionError):
					self.rest(v2.create_doc, "Reservation", **self.payload(**fields))
		self.assertFalse(frappe.db.exists("Reservation", {
			"property": PROP_A, "room": self.rooms[3], "check_in_date": d(5)}))

	def test_a_rest_update_cannot_reach_the_agreed_rate_or_discount_flags(self):
		from frappe.api import v1, v2
		res = self.stay()
		for fn in (v1.update_doc, v2.update_doc):
			for case, fields in (
					("agreed rate", dict(agreed_rate=1, flags={"kamra_agreed_rate_override": 1})),
					("discount", dict(discount_type="Percent", discount_value=50,
					                  flags={"kamra_reservation_discount": 1}))):
				with self.subTest(api=fn.__module__, case=case):
					with self.assertRaises(frappe.PermissionError):
						self.rest(fn, "Reservation", res.name, **fields)
		doc = self.doc(res.name)
		self.assertEqual((flt(doc.agreed_rate), doc.discount_type or None), (0, None))
		self.assertEqual(self.rows(res.name), [(1000, 0, 1000)] * 2)


class GovernedWritersStillWriteTests(RoundFourTestCase):
	"""The writers the guards name keep working (regression pins)."""

	def test_a_room_move_to_the_new_rooms_rate_clears_an_agreed_rate(self):
		res = self.stay()
		self.agree(res.name, 900)
		dlx = make_room_type(PROP_A, "DLX", base_price=1200)
		frappe.db.set_value("Room Type", dlx, "base_price", 1200)
		suite = make_room(PROP_A, dlx, "201")
		self.as_user(DESK, api.move_reservation, res.name, suite, confirm_price=1)
		doc = self.doc(res.name)
		self.assertEqual((doc.room, flt(doc.agreed_rate)), (suite, 0))
		self.assertEqual(self.rows(res.name), [(1200, 0, 1200)] * 2)

	def test_a_revenue_manager_still_raises_an_agreed_rate(self):
		res = self.stay()
		self.as_user(REVENUE, api.set_agreed_rate, res.name, 1200, "RATE-MGR", confirm_price=1)
		self.assertEqual(self.rows(res.name), [(1200, 0, 1200)] * 2)
		self.assertEqual(rate_codes(self.doc(res.name)), [1000, 1000])


class ChangesLogTests(RoundFourTestCase):
	"""R4-10: the reservation's Changes Log (OPERA Actions > Changes Log, SAP
	Environment > Changes), read from Frappe's own Version rows and the
	audit rows: who changed what, each night's rate, who signed it off."""

	def test_the_changes_log_names_each_change_its_nights_and_its_approver(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		res = self.stay()
		# Frappe skips Version rows under test (Document._save: ignore_version
		# defaults to frappe.in_test); production writes them, so these saves
		# do too
		with patch.object(frappe, "in_test", False):
			self.discount(res.name, discount_type="Percent", discount_value=30,
			              supervisor_user=GM, supervisor_pin="4321", **PROMO)
			self.agree(res.name, 650, supervisor=GM, pin="4321")
		entries = self.as_user(DESK, api.reservation_changes, res.name)["entries"]
		actions = [e["action_type"] for e in entries if e["kind"] == "action"]
		self.assertEqual(actions[:2], ["rate_override", "reservation_discount"])
		disc = next(e for e in entries if e.get("action_type") == "reservation_discount")
		self.assertEqual((disc["by"], disc["approver"]), (DESK, GM))
		self.assertTrue(disc["title"])
		self.assertIn("RATE-PROMO", disc["rationale"])
		self.assertEqual([(n["date"], n["from"], n["to"]) for n in disc["nights"]],
		                 [(d(5), 1000, 700), (d(6), 1000, 700)])
		agreed = next(e for e in entries if e.get("action_type") == "rate_override")
		self.assertEqual([(n["date"], n["from"], n["to"]) for n in agreed["nights"]],
		                 [(d(5), 700, 650), (d(6), 700, 650)])
		# Frappe's Version rows: the fields and nights the discount save changed
		# (the agreed rate's later save clears the discount it replaces)
		change = next(e for e in entries if e["kind"] == "change"
		              and any(f["field"] == "discount_type" and f["to"] == "Percent"
		                      for f in e["fields"]))
		field = next(f for f in change["fields"] if f["field"] == "discount_type")
		self.assertEqual((field["label"], field["from"], field["to"]), ("Discount Type", "", "Percent"))
		self.assertEqual(change["by"], DESK)
		self.assertEqual([(n["date"], n["from"], n["to"]) for n in change["nights"]],
		                 [(d(5), 1000, 700), (d(6), 1000, 700)])
		shown = {f["field"] for e in entries if e["kind"] == "change" for f in e["fields"]}
		self.assertFalse(shown & {"rate_lock_key", "quote_snapshot", "modified", "precheckin_token"})

	def test_reservation_detail_names_each_nights_own_reason(self):
		res = self.stay()
		self.discount(res.name, discount_type="Percent", discount_value=5, **PROMO, night_discounts=[
			{"date": d(6), "discount_type": "Percent", "discount_value": 10, "discount_reason": "ADJ-RATE"}])
		detail = self.as_user(DESK, api.reservation_detail, res.name)
		self.assertEqual([n.get("discount_reason") for n in detail["night_rates"]], [None, "ADJ-RATE"])
