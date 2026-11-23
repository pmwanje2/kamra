"""Reservation discounts, round 2 (Gap Record #8555 review remediation).

OPERA Cloud keeps a booked reservation's rate where it was booked: giving,
changing or clearing a discount on a confirmed or in-house stay takes it off
each locked night's booked base, never off today's rate card, season, demand
premium, rate plan or meal price. These tests pin that and the rest of the
round-2 findings:

- P0-1 a discount on a booked stay is applied in place on the locked nights;
  meals, cleaning and the rate plan stay as booked, and the preview is what
  the save persists.
- P0-2 Daily Details given at booking reach the insert with no night rows,
  so a Before Validate writer (hospitality-tariffs' Kamra Tariff Resolve,
  HSP-TARIFF-007) still sees an unpriced booking.
- P0-3 a tax-inclusive slab crossing never reports a negative discount and
  is governed on the gross the desk entered.
- P0-4 the booking's discount follows its nights; the voucher is read as
  the voucher, not derived from what is left over.
- P0-5 a plain save (REST PUT) can neither clear an in-stay Daily Details
  night nor change a locked night's price.
- P0-6 a discounted hourly day-use stay bills the discounted room charge.
- P1-7 a charged Daily Details night sent back unchanged is left alone, and
  reservation_detail says which nights are on the folio.
- P1-9 re-pricing a discounted stay (rate plan, room type, dates) runs the
  discount rules again.
- P2-12 a full comp is always signed off by another supervisor, whatever
  the role of the person giving it."""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, nowdate

from kamra import api, pricing
from kamra import folio as folio_mod
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	YESTERDAY,
	charges,
	d,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)
from kamra.tests.test_reservation_discounts import (
	DESK,
	GM,
	SELF,
	DiscountTestCase,
	set_setting,
)

PROMO = dict(reason_code="RATE-PROMO")


def refuse_locked_nights_on_insert(doc, method):
	"""What hospitality-tariffs' Kamra Tariff Resolve (Before Validate) does
	to a new booking that already carries night_rates: HSP-TARIFF-007."""
	if doc.is_new() and doc.get("night_rates"):
		frappe.throw("HSP-TARIFF-007: A booking whose night_rates are already locked "
		             "cannot take a tariff rate_plan.")


class RoundTwoTestCase(DiscountTestCase):
	def setUp(self):
		super().setUp()
		set_setting(PROP_A, "rate_override_limit_pct", 20)

	def doc(self, name):
		return frappe.get_doc("Reservation", name)

	def meal_plan(self, price_per_adult):
		name = f"{PROP_A}-CP"
		if frappe.db.exists("Meal Plan", name):
			frappe.db.set_value("Meal Plan", name, {"price_per_adult": price_per_adult,
			                                        "price_per_child": 0})
			return name
		return frappe.get_doc({"doctype": "Meal Plan", "property": PROP_A, "code": "CP",
		                       "label": "UT Breakfast", "price_per_adult": price_per_adult}
		                      ).insert(ignore_permissions=True).name

	def discount(self, name, user=DESK, **kw):
		return self.as_user(user, api.set_reservation_discount, name, **kw)

	def snapshot(self, name):
		return json.loads(self.doc(name).quote_snapshot or "{}")

	@staticmethod
	def line(snap, component):
		return next((li for li in snap.get("line_items") or []
		             if li.get("component") == component), None)


class BookedRateImmutabilityTests(RoundTwoTestCase):
	"""P0-1: a booked stay's discount comes off its locked nights in place."""

	def test_a_discount_keeps_the_booked_base_rate_when_the_rate_card_moved(self):
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0])
		booked = flt(self.doc(res.name).amount_after_tax)
		gst = flt(self.doc(res.name).night_rates[0].gst_rate)
		# the rate card goes up after the booking was confirmed
		frappe.db.set_value("Room Type", self.rt, "base_price", 1500)
		pv = self.as_user(DESK, api.discount_preview, res.name, discount_type="Percent",
		                  discount_value=10, **PROMO)
		self.assertEqual([(n["base_rate"], n["discount_amount"], n["rate"]) for n in pv["nights"]],
		                 [(1000, 100, 900), (1000, 100, 900)])
		self.discount(res.name, discount_type="Percent", discount_value=10, **PROMO)
		self.assertEqual(self.rows(res.name), [(1000, 100, 900), (1000, 100, 900)])
		saved = flt(self.doc(res.name).amount_after_tax)
		# the preview is exactly what the save persists
		self.assertAlmostEqual(saved, pv["new_amount"], places=2)
		self.assertAlmostEqual(saved, booked - 200 * (1 + gst / 100), places=2)
		# clearing it puts the stay back on its booked rate, not today's card
		self.discount(res.name)
		self.assertEqual(self.rows(res.name), [(1000, 0, 1000), (1000, 0, 1000)])
		self.assertAlmostEqual(flt(self.doc(res.name).amount_after_tax), booked, places=2)

	def test_meals_cleaning_and_the_rate_plan_stay_as_booked(self):
		meal = self.meal_plan(300)
		plan = self.plan("CORP", "Percent", -20)
		frappe.db.set_value("Property", PROP_A, "cleaning_fee", 500)
		frappe.clear_document_cache("Property", PROP_A)
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0],
		                       meal_plan=meal, rate_plan=plan)
		before = self.doc(res.name)
		booked = flt(before.amount_after_tax)
		gst = flt(before.night_rates[0].gst_rate)
		meals = [flt(r.meal_amount) for r in before.night_rates]
		snap = self.snapshot(res.name)
		self.assertEqual(self.rows(res.name), [(800, 0, 800), (800, 0, 800)])
		# every price behind the booking moves afterwards
		frappe.db.set_value("Meal Plan", meal, "price_per_adult", 800)
		frappe.db.set_value("Rate Plan", plan, "modifier_value", -50)
		frappe.db.set_value("Property", PROP_A, "cleaning_fee", 2000)
		frappe.clear_document_cache("Property", PROP_A)
		pv = self.as_user(DESK, api.discount_preview, res.name, discount_type="Percent",
		                  discount_value=10, **PROMO)
		self.discount(res.name, discount_type="Percent", discount_value=10, **PROMO)
		after = self.doc(res.name)
		self.assertEqual(self.rows(res.name), [(800, 80, 720), (800, 80, 720)])
		self.assertEqual([flt(r.meal_amount) for r in after.night_rates], meals)
		new_snap = self.snapshot(res.name)
		self.assertAlmostEqual(new_snap["meal_total"], snap["meal_total"], places=2)
		self.assertEqual(self.line(new_snap, "cleaning_fee"), self.line(snap, "cleaning_fee"))
		self.assertAlmostEqual(flt(after.amount_after_tax), booked - 160 * (1 + gst / 100), places=2)
		self.assertAlmostEqual(flt(after.amount_after_tax), pv["new_amount"], places=2)

	def test_an_agreed_rate_stays_and_its_meals_keep_the_booked_price(self):
		meal = self.meal_plan(300)
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0],
		                       meal_plan=meal, agreed_rate=1200)
		meals = [flt(r.meal_amount) for r in self.doc(res.name).night_rates]
		frappe.db.set_value("Meal Plan", meal, "price_per_adult", 900)
		self.discount(res.name, discount_type="Amount", discount_value=200, **PROMO)
		self.assertEqual(self.rows(res.name), [(1200, 200, 1000), (1200, 200, 1000)])
		self.assertEqual([flt(r.meal_amount) for r in self.doc(res.name).night_rates], meals)


class InitialNightDiscountTests(RoundTwoTestCase):
	"""P0-2: Daily Details given at booking never put rows on the insert."""

	def tariff_resolver(self):
		hooks = frappe.get_doc_hooks().get("Reservation", {})
		events = {**hooks, "before_validate": [*hooks.get("before_validate", []),
		                                      f"{__name__}.refuse_locked_nights_on_insert"]}
		return patch.dict(frappe.get_doc_hooks(), Reservation=events)

	def test_create_booking_daily_details_reach_an_insert_without_night_rows(self):
		with self.tariff_resolver():
			out = self.book(discount_type="Percent", discount_value=10,
			                discount_reason="RATE-PROMO", night_discounts=[
				                {"date": d(6), "discount_type": "Amount", "discount_value": 50}])
		self.assertEqual(self.rows(out["reservation"]), [(1000, 100, 900), (1000, 50, 950)])
		night = self.doc(out["reservation"]).night_rates[1]
		self.assertEqual((night.discount_type, flt(night.discount_value), night.discount_reason),
		                 ("Amount", 50, "RATE-PROMO"))
		# the next plain save keeps the lock it was booked with
		doc = self.doc(out["reservation"])
		key = doc.rate_lock_key
		doc.special_requests = "Late arrival"
		doc.save(ignore_permissions=True)
		self.assertEqual(self.doc(out["reservation"]).rate_lock_key, key)

	def test_walk_in_daily_details_reach_an_insert_without_night_rows(self):
		today = nowdate()
		with self.tariff_resolver():
			out = self.as_user(DESK, api.walk_in, PROP_A, self.rt, self.rooms[3],
			                   str(add_days(today, 2)), "UT Walk Daily", defer_check_in=1,
			                   discount_reason="RATE-PROMO", night_discounts=[
				                   {"date": str(add_days(today, 1)), "discount_type": "Percent",
				                    "discount_value": 15}])
		self.assertEqual(self.rows(out["reservation"]), [(1000, 0, 1000), (1000, 150, 850)])


class TaxInclusiveSlabTests(RoundTwoTestCase):
	"""P0-3: a discount that drops the night into a lower tax slab."""

	def setUp(self):
		super().setUp()
		set_setting(PROP_A, "rate_override_limit_pct", 15)
		# 5% up to 7,500 a night gross, 18% above it, prices tax included
		make_property(PROP_A, business_date=YESTERDAY, rates_include_tax=1, gst_mode="Slab")
		self.lux_rt = make_room_type(PROP_A, "LUX", base_price=8000)
		frappe.db.set_value("Room Type", self.lux_rt, "base_price", 8000)
		self.lux_room = make_room(PROP_A, self.lux_rt, "301")

	def lux(self, user=DESK, **kw):
		return self.as_user(user, api.create_booking, PROP_A, self.lux_rt, d(5), d(7),
		                    "UT Slab Guest", **kw)

	def test_the_discount_is_reported_on_the_gross_and_never_negative(self):
		plain = pricing.quote(PROP_A, self.lux_rt, d(5), d(7))
		self.assertEqual([n["gst_rate"] for n in plain["nightly"]], [18, 18])
		q = pricing.quote(PROP_A, self.lux_rt, d(5), d(7), discount_type="Percent", discount_value=10)
		for night in q["nightly"]:
			self.assertEqual(night["gst_rate"], 5)
			self.assertAlmostEqual(night["base_rate"], 6779.66, places=2)
			self.assertAlmostEqual(night["rate"], 6857.14, places=2)
			# 8,000 gross less 10% is 7,200 gross: 800 off, never -77.48
			self.assertAlmostEqual(night["discount_amount"], 800, places=2)
		self.assertAlmostEqual(q["reservation_discount_total"], 1600, places=2)
		self.assertAlmostEqual(q["room_base_total"], 16000, places=2)
		self.assertAlmostEqual(q["discount"], 1600, places=2)
		self.assertAlmostEqual(q["amount_after_tax"], 14400, places=1)

	def test_the_rules_read_the_cut_the_desk_entered(self):
		gov = self.as_user(DESK, api.get_quote, PROP_A, self.lux_rt, d(5), d(7),
		                   discount_type="Percent", discount_value=10,
		                   discount_reason="RATE-PROMO")["discount_governance"]
		self.assertAlmostEqual(gov["effective_pct"], 10, places=2)
		self.assertAlmostEqual(gov["max_pct"], 10, places=2)
		self.assertAlmostEqual(gov["base_total"], 16000, places=2)
		self.assertAlmostEqual(gov["discount_total"], 1600, places=2)
		self.assertAlmostEqual(gov["net_total"], 14400, places=2)
		self.assertFalse(gov["needs_supervisor"])
		deep = self.as_user(DESK, api.get_quote, PROP_A, self.lux_rt, d(5), d(7),
		                    discount_type="Percent", discount_value=20,
		                    discount_reason="RATE-PROMO")["discount_governance"]
		self.assertAlmostEqual(deep["max_pct"], 20, places=2)
		self.assertTrue(deep["needs_supervisor"])
		with self.assertRaises(frappe.PermissionError):
			self.lux(discount_type="Percent", discount_value=20, discount_reason="RATE-PROMO")
		with self.assertRaises(frappe.ValidationError):
			self.lux(discount_type="Percent", discount_value=10)
		with self.assertRaises(frappe.ValidationError):
			self.lux(rate_plan=self.plan("NRF", discount_eligible=0), discount_type="Percent",
			         discount_value=10, discount_reason="RATE-PROMO")

	def test_the_nights_the_folio_and_the_total_reconcile_to_the_cent(self):
		out = self.lux(discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO")
		res = self.doc(out["reservation"])
		self.assertEqual(self.rows(res.name), [(6779.66, 800, 6857.14), (6779.66, 800, 6857.14)])
		self.assertEqual([flt(r.gst_rate) for r in res.night_rates], [5, 5])
		self.assertAlmostEqual(flt(res.amount_after_tax), 14400, places=1)
		self.assertAlmostEqual(flt(res.discount_amount), 1600, places=2)
		folio_mod.open_folio(res)
		folio_mod.post_room_night(res, d(5))
		room = frappe.get_all("Folio Charge", filters={"reservation": res.name, "charge_type": "Room"},
		                      fields=["amount", "gst_rate"])
		self.assertEqual([(round(flt(c.amount), 2), flt(c.gst_rate)) for c in room], [(6857.14, 5)])
		self.assertAlmostEqual(flt(room[0].amount) * 1.05, 7200, places=1)

	def test_a_booked_slab_night_is_re_discounted_and_cleared_on_its_own_base(self):
		out = self.lux(discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO")
		frappe.db.set_value("Room Type", self.lux_rt, "base_price", 9000)
		self.discount(out["reservation"], discount_type="Percent", discount_value=20,
		              supervisor_user=GM, supervisor_pin="4321", **PROMO)
		# 8,000 gross less 20% is 6,400 gross, 5% inside it
		self.assertEqual(self.rows(out["reservation"]),
		                 [(6779.66, 1600, 6095.24), (6779.66, 1600, 6095.24)])
		self.discount(out["reservation"])
		res = self.doc(out["reservation"])
		self.assertEqual(self.rows(res.name), [(6779.66, 0, 6779.66), (6779.66, 0, 6779.66)])
		self.assertEqual([flt(r.gst_rate) for r in res.night_rates], [18, 18])


class DiscountAmountTests(RoundTwoTestCase):
	"""P0-4: the booking's discount is its nights' plus the voucher."""

	def voucher(self, value=50):
		frappe.db.delete("Discount Voucher", {"property": PROP_A, "voucher_code": "UTR2FIFTY"})
		return frappe.get_doc({"doctype": "Discount Voucher", "property": PROP_A,
		                       "voucher_code": "UTR2FIFTY", "discount_type": "Amount",
		                       "value": value}).insert(ignore_permissions=True).name

	def test_the_discount_amount_follows_the_nights_after_a_charged_night(self):
		stay = make_reservation(PROP_A, self.rt, d(0), d(2), status="Checked In", room=self.rooms[1])
		folio_mod.post_room_night(self.doc(stay.name), d(0))
		self.discount(stay.name, discount_type="Percent", discount_value=10, **PROMO)
		res = self.doc(stay.name)
		self.assertEqual(self.rows(stay.name), [(1000, 0, 1000), (1000, 100, 900)])
		self.assertAlmostEqual(flt(res.discount_amount), 100, places=2)
		self.assertEqual(folio_mod.voucher_discount(res), 0)

	def test_clearing_a_billed_discount_keeps_the_voucher(self):
		stay = make_reservation(PROP_A, self.rt, d(0), d(2), status="Checked In",
		                        room=self.rooms[2], voucher=self.voucher())
		self.assertEqual(folio_mod.voucher_discount(self.doc(stay.name)), 50)
		self.discount(stay.name, discount_type="Percent", discount_value=10, **PROMO)
		self.assertEqual(folio_mod.voucher_discount(self.doc(stay.name)), 50)
		folio_mod.post_room_night(self.doc(stay.name), d(0))
		self.discount(stay.name)
		res = self.doc(stay.name)
		self.assertEqual(self.rows(stay.name), [(1000, 100, 900), (1000, 0, 1000)])
		self.assertAlmostEqual(flt(res.discount_amount), 150, places=2)
		self.assertEqual(folio_mod.voucher_discount(res), 50)


class PlainSaveGuardTests(RoundTwoTestCase):
	"""P0-5: what a REST PUT (doc.save) may not do to the discount or a locked night."""

	def test_clearing_an_in_stay_daily_details_night_is_refused(self):
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0])
		self.discount(res.name, discount_type="Percent", discount_value=10, **PROMO, night_discounts=[
			{"date": d(6), "discount_type": "Percent", "discount_value": 0}])
		self.assertEqual(self.rows(res.name), [(1000, 100, 900), (1000, 0, 1000)])
		doc = self.doc(res.name)
		doc.night_rates[1].discount_type = None
		doc.night_rates[1].discount_value = 0
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)
		self.assertEqual(self.rows(res.name), [(1000, 100, 900), (1000, 0, 1000)])

	def test_a_locked_nights_price_cannot_be_written_by_a_plain_save(self):
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0])
		self.discount(res.name, discount_type="Percent", discount_value=10, **PROMO)
		for field, value in (("rate", 1), ("base_rate", 1), ("discount_amount", 999),
		                     ("gst_rate", 0), ("meal_amount", 5)):
			with self.subTest(field=field):
				doc = self.doc(res.name)
				doc.night_rates[0].set(field, value)
				with self.assertRaises(frappe.PermissionError):
					doc.save(ignore_permissions=True)
		doc = self.doc(res.name)
		doc.set("night_rates", doc.night_rates[1:])
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)
		self.assertEqual(self.rows(res.name), [(1000, 100, 900), (1000, 100, 900)])

	def test_shortening_the_stay_takes_its_daily_details_night_with_it(self):
		res = make_reservation(PROP_A, self.rt, d(5), d(8), room=self.rooms[0])
		self.discount(res.name, **PROMO, night_discounts=[
			{"date": d(7), "discount_type": "Amount", "discount_value": 100}])
		doc = self.doc(res.name)
		doc.check_out_date = d(7)
		doc.save(ignore_permissions=True)
		self.assertEqual(self.rows(res.name), [(1000, 0, 1000), (1000, 0, 1000)])


class DayUseDiscountTests(RoundTwoTestCase):
	"""P0-6: an hourly day-use stay's discount reaches the folio."""

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY)
		set_setting(PROP_A, "day_use_overstay", "Next hour")
		frappe.db.delete("Rate Plan", {"property": PROP_A, "code": "HRLY"})
		self.hourly = frappe.get_doc({
			"doctype": "Rate Plan", "property": PROP_A, "rate_plan_name": "UT Hourly",
			"code": "HRLY", "modifier_type": "Percent", "modifier_value": 0,
			"billing_basis": "Hourly", "hourly_rate": 200, "min_hours": 2,
		}).insert(ignore_permissions=True).name

	def day_use_stay(self):
		res = make_reservation(PROP_A, self.rt, TODAY, TODAY, status="Checked In",
		                       room=self.rooms[4], rate_plan=self.hourly, is_day_use=1,
		                       auto_price=1, planned_check_in_time="10:00:00",
		                       planned_check_out_time="14:00:00")
		self.assertEqual(self.rows(res.name), [(800, 0, 800)])
		self.discount(res.name, discount_type="Percent", discount_value=10, **PROMO)
		self.assertEqual(self.rows(res.name), [(800, 80, 720)])
		return res

	def post_at(self, res, hhmm):
		at = datetime.strptime(f"{TODAY} {hhmm}:00", "%Y-%m-%d %H:%M:%S")
		with patch("kamra.day_use.now_datetime", return_value=at):
			folio_mod.post_remaining_nights(self.doc(res.name))
		return [round(flt(c.amount), 2) for c in charges(res.name, "Room")]

	def test_a_discounted_hourly_stay_bills_the_discounted_room_charge(self):
		res = self.day_use_stay()
		self.assertEqual(self.post_at(res, "14:00"), [720])
		self.assertIn("less discount 80.00", charges(res.name, "Room")[0].description)

	def test_an_overstay_bills_its_extra_hours_at_the_plan_rate(self):
		res = self.day_use_stay()
		# five hours at 200 is 1,000: the booked 800 carries the 80 off
		self.assertEqual(self.post_at(res, "15:00"), [920])


class ChargedNightPanelTests(RoundTwoTestCase):
	"""P1-7: the Daily Details editor on an in-house stay."""

	def setUp(self):
		super().setUp()
		self.stay = make_reservation(PROP_A, self.rt, d(0), d(2), status="Checked In",
		                             room=self.rooms[1])
		self.discount(self.stay.name, **PROMO, night_discounts=[
			{"date": d(0), "discount_type": "Percent", "discount_value": 10}])
		folio_mod.post_room_night(self.doc(self.stay.name), d(0))

	def test_a_billed_night_sent_back_unchanged_is_left_alone(self):
		self.discount(self.stay.name, discount_type="Percent", discount_value=5, **PROMO,
		              night_discounts=[{"date": d(0), "discount_type": "Percent", "discount_value": 10}])
		self.assertEqual(self.rows(self.stay.name), [(1000, 100, 900), (1000, 50, 950)])
		with self.assertRaises(frappe.ValidationError):
			self.discount(self.stay.name, **PROMO, night_discounts=[
				{"date": d(0), "discount_type": "Percent", "discount_value": 20}])

	def test_reservation_detail_says_which_nights_are_on_the_folio(self):
		detail = self.as_user(DESK, api.reservation_detail, self.stay.name)
		self.assertEqual([n.get("charged") for n in detail["night_rates"]], [True, False])


class RepriceRulesTests(RoundTwoTestCase):
	"""P1-9: a discounted stay re-priced by its plan, room type or dates."""

	def test_a_discounted_stay_cannot_move_to_a_plan_that_takes_no_discount(self):
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0])
		self.discount(res.name, discount_type="Percent", discount_value=10, **PROMO)
		nrf = self.plan("NRF", discount_eligible=0)
		doc = self.doc(res.name)
		doc.rate_plan = nrf
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)
		self.assertFalse(self.doc(res.name).rate_plan)

	def test_extending_a_supervisor_approved_cut_asks_for_the_sign_off_again(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0])
		self.discount(res.name, discount_type="Percent", discount_value=30,
		              supervisor_user=GM, supervisor_pin="4321", **PROMO)
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, api.amend_stay, res.name, d(5), d(8), confirm_price=1)
		self.assertEqual(len(self.doc(res.name).night_rates), 2)
		self.as_user(DESK, api.amend_stay, res.name, d(5), d(8), confirm_price=1,
		             supervisor_user=GM, supervisor_pin="4321")
		self.assertEqual(self.rows(res.name), [(1000, 300, 700)] * 3)

	def test_a_discount_within_the_limit_follows_an_extension(self):
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0])
		self.discount(res.name, discount_type="Percent", discount_value=10, **PROMO)
		self.as_user(DESK, api.amend_stay, res.name, d(5), d(8), confirm_price=1)
		self.assertEqual(self.rows(res.name), [(1000, 100, 900)] * 3)


class FullCompTests(RoundTwoTestCase):
	"""P2-12: nobody signs off their own 100% comp."""

	ARGS = dict(discount_type="Percent", discount_value=100, discount_reason="ADJ-RATE")

	def test_a_hotel_admin_needs_another_supervisors_pin_for_a_comp(self):
		with self.assertRaises(frappe.PermissionError):
			self.book(GM, **self.ARGS)
		with self.assertRaises(frappe.PermissionError) as raised:
			self.book(GM, **self.ARGS, supervisor_user=GM, supervisor_pin="4321")
		self.assertTrue(str(raised.exception).startswith("SUPERVISOR_SELF_APPROVAL:"))
		self.desk_with_finance()
		out = self.book(GM, **self.ARGS, supervisor_user=SELF, supervisor_pin="5678")
		self.assertEqual(self.rows(out["reservation"]), [(1000, 1000, 0), (1000, 1000, 0)])
		self.assertEqual(self.discount_logs(out["reservation"])[0].approver, SELF)

	def test_the_preview_asks_a_hotel_admin_for_someone_elses_pin(self):
		self.desk_with_finance()
		gov = self.as_user(GM, api.get_quote, PROP_A, self.rt, d(5), d(7),
		                   **self.ARGS)["discount_governance"]
		self.assertTrue(gov["comp"])
		self.assertTrue(gov["needs_supervisor"])
		self.assertNotIn(GM, [s["user"] for s in gov["supervisors"]])
		self.assertIn(SELF, [s["user"] for s in gov["supervisors"]])

	def test_a_comp_on_a_booked_stay_needs_another_supervisor_too(self):
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0])
		with self.assertRaises(frappe.PermissionError):
			self.discount(res.name, GM, discount_type="Percent", discount_value=100,
			              reason_code="ADJ-RATE")
		self.assertEqual(self.rows(res.name), [(1000, 0, 1000), (1000, 0, 1000)])
