"""Reservation and walk-in discounts (OPERA Cloud Reservation Discounts,
SAP S/4HANA manual discount condition).

A discount is a percentage OR a flat amount off each night's room rate,
with a mandatory reason. It comes off the accommodation only - meal plans,
extra guests, levies and the other lines keep their price - and tax is worked
out again on the discounted room. Each night keeps its gross rate, the
discount and the net rate apart (the SAP gross / discount / net split), so the
rate card, the rate plan and a contract's agreed rate are never overwritten.

Who may grant what follows the property's front-desk limit
(rate_override_limit_pct): deeper cuts, a full comp, a cut under the rate
guardrail floor or a reason code that needs a supervisor are signed off with
another supervisor's PIN. An AI agent is held to the desk limit. A rate plan
can be marked not discountable (OPERA Rate Code "Discount" flag)."""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.utils import add_days, nowdate

from kamra import api, pricing, rates
from kamra import folio as folio_mod
from kamra.settings import clear_cache
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	YESTERDAY,
	AuditTestCase,
	charges,
	d,
	ensure_user,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

DESK = "ut.disc.desk@test.local"
REVENUE = "ut.disc.revenue@test.local"
# a revenue manager who also works the desk (create_booking is a desk action)
RATEDESK = "ut.disc.ratedesk@test.local"
GM = "ut.disc.gm@test.local"
AGENT = "ut.disc.agent@test.local"
SELF = "ut.disc.deskfinance@test.local"

DISCOUNT_FLAG_OBSERVATIONS = "reservation_discount_observations"


def observe_reservation_discount(doc, method):
	frappe.flags.get(DISCOUNT_FLAG_OBSERVATIONS).append(
		(doc.name, doc.flags.get("kamra_reservation_discount")))


def set_setting(prop: str, key: str, value) -> None:
	name = frappe.db.get_value("Property Setting",
	                           {"property": prop, "setting_key": key})
	if name:
		frappe.db.set_value("Property Setting", name, "value", json.dumps(value))
	else:
		frappe.get_doc({"doctype": "Property Setting", "property": prop,
		                "setting_key": key, "value": json.dumps(value)}
		               ).insert(ignore_permissions=True)
	clear_cache(prop)


class DiscountTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY)
		make_property(PROP_A, business_date=YESTERDAY)
		frappe.db.delete("Property Setting", {"property": PROP_A})
		frappe.db.delete("Rate Guardrail", {"property": PROP_A})
		clear_cache(PROP_A)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		frappe.db.set_value("Room Type", self.rt, "base_price", 1000)
		self.rooms = [make_room(PROP_A, self.rt, f"10{i}") for i in range(1, 6)]
		ensure_user(DESK, ["Front Desk"])
		ensure_user(REVENUE, ["Revenue Manager"])
		ensure_user(RATEDESK, ["Front Desk", "Revenue Manager"])
		ensure_user(GM, ["Hotel Admin"])
		ensure_user(AGENT, ["Kamra Agent"])
		frappe.db.delete("Cashier PIN", {"name": GM})
		frappe.get_doc({"doctype": "Cashier PIN", "user": GM, "pin": "4321"}
		               ).insert(ignore_permissions=True)
		from kamra.ledger import ensure_default_reason_codes
		ensure_default_reason_codes()

	# --- helpers ------------------------------------------------------------

	def as_user(self, user, fn, *args, **kwargs):
		frappe.set_user(user)  # nosemgrep: frappe-setuser -- acting as the persona
		try:
			return fn(*args, **kwargs)
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- restore

	def as_agent(self, user, fn, *args, **kwargs):
		frappe.flags.kamra_agent_call = "MCP"
		try:
			return self.as_user(user, fn, *args, **kwargs)
		finally:
			frappe.flags.kamra_agent_call = None

	def quote(self, **kw):
		return pricing.quote(PROP_A, self.rt, d(5), d(7), **kw)

	def book(self, user=DESK, **kw):
		return self.as_user(user, api.create_booking, PROP_A, self.rt, d(5), d(7),
		                    "UT Discount Guest", **kw)

	def rows(self, name):
		doc = frappe.get_doc("Reservation", name)
		return [(round(float(r.base_rate), 2), round(float(r.discount_amount), 2),
		         round(float(r.rate), 2)) for r in doc.night_rates]

	def discount_logs(self, name):
		return frappe.get_all("Agent Action Log",
		                      filters={"action_type": "reservation_discount",
		                               "reference_name": name},
		                      fields=["actor", "approver", "rationale"])

	def plan(self, code="BAR", modifier_type="Percent", modifier_value=0, **fields):
		name = f"{PROP_A}-{code}"
		if frappe.db.exists("Rate Plan", name):
			frappe.delete_doc("Rate Plan", name, force=True, ignore_permissions=True)
		return frappe.get_doc({"doctype": "Rate Plan", "property": PROP_A,
		                       "rate_plan_name": code, "code": code,
		                       "modifier_type": modifier_type,
		                       "modifier_value": modifier_value, **fields}
		                      ).insert(ignore_permissions=True).name

	def desk_with_finance(self):
		ensure_user(SELF, ["Front Desk", "Finance"])
		frappe.db.delete("Cashier PIN", {"name": SELF})
		frappe.get_doc({"doctype": "Cashier PIN", "user": SELF, "pin": "5678"}
		               ).insert(ignore_permissions=True)


class ReservationDiscountQuoteTests(DiscountTestCase):
	"""pricing.quote: the discount on the net accommodation only."""

	def test_percent_discount_comes_off_each_nights_room_rate(self):
		plain = self.quote()
		q = self.quote(discount_type="Percent", discount_value=10)
		gst = plain["nightly"][0]["gst_rate"]
		for night in q["nightly"]:
			self.assertEqual(night["base_rate"], 1000)
			self.assertEqual(night["discount_type"], "Percent")
			self.assertEqual(night["discount_value"], 10)
			self.assertAlmostEqual(night["discount_amount"], 100, places=2)
			self.assertAlmostEqual(night["rate"], 900, places=2)
		self.assertAlmostEqual(q["room_base_total"], 2000, places=2)
		self.assertAlmostEqual(q["reservation_discount_total"], 200, places=2)
		self.assertAlmostEqual(q["room_total"], 1800, places=2)
		# tax is worked out again on the discounted room
		self.assertAlmostEqual(q["tax_amount"], 1800 * gst / 100, places=2)
		self.assertAlmostEqual(q["amount_after_tax"], 1800 * (1 + gst / 100), places=2)
		# the stay's discount is reflected in `discount`; no voucher here
		self.assertAlmostEqual(q["discount"], 200, places=2)
		self.assertEqual(q["voucher_discount"], 0)
		self.assertAlmostEqual(plain["room_total"], 2000, places=2)

	def test_amount_discount_is_a_flat_amount_off_each_night(self):
		q = self.quote(discount_type="Amount", discount_value=150)
		self.assertEqual([round(n["rate"], 2) for n in q["nightly"]], [850, 850])
		self.assertEqual([n["discount_amount"] for n in q["nightly"]], [150, 150])
		self.assertAlmostEqual(q["reservation_discount_total"], 300, places=2)

	def test_the_discount_never_touches_meals_or_extra_guests(self):
		meal = f"{PROP_A}-CP"
		if frappe.db.exists("Meal Plan", meal):
			frappe.db.set_value("Meal Plan", meal, {"price_per_adult": 300, "price_per_child": 0})
		else:
			frappe.get_doc({"doctype": "Meal Plan", "property": PROP_A, "code": "CP",
			                "label": "UT Breakfast", "price_per_adult": 300}
			               ).insert(ignore_permissions=True)
		args = dict(meal_plan=meal, over_capacity_charge=200)
		plain = self.quote(**args)
		q = self.quote(**args, discount_type="Percent", discount_value=50)
		self.assertAlmostEqual(q["meal_total"], plain["meal_total"], places=2)
		self.assertAlmostEqual(q["meal_total"], 1200, places=2)
		self.assertAlmostEqual(q["over_capacity_total"], plain["over_capacity_total"], places=2)
		self.assertAlmostEqual(q["room_total"], plain["room_total"] / 2, places=2)
		self.assertAlmostEqual(plain["amount_before_tax"] - q["amount_before_tax"],
		                       plain["room_total"] / 2, places=2)

	def test_tax_inclusive_rates_discount_the_gross_and_back_out_tax_again(self):
		make_property(PROP_A, business_date=YESTERDAY, rates_include_tax=1)
		rt = make_room_type(PROP_A, "VAT", base_price=1000, tax_percent=18)
		frappe.db.set_value("Room Type", rt, {"base_price": 1000, "tax_percent": 18})
		q = pricing.quote(PROP_A, rt, d(5), d(7), discount_type="Percent", discount_value=10)
		# 1,000 gross a night, 10% off: 900 gross, 762.71 net of 18% tax
		self.assertAlmostEqual(q["amount_after_tax"], 1800, places=1)
		for night in q["nightly"]:
			self.assertAlmostEqual(night["base_rate"], 847.46, places=2)
			self.assertAlmostEqual(night["rate"], 762.71, places=2)
		amount = pricing.quote(PROP_A, rt, d(5), d(7), discount_type="Amount", discount_value=100)
		self.assertAlmostEqual(amount["amount_after_tax"], 1800, places=1)
		with self.assertRaises(frappe.ValidationError):
			pricing.quote(PROP_A, rt, d(5), d(7), discount_type="Amount", discount_value=1000.01)

	def test_out_of_range_discounts_are_refused_not_coerced(self):
		bad = [
			dict(discount_type="Percent", discount_value=100.5),
			dict(discount_type="Percent", discount_value=-5),
			dict(discount_type="Amount", discount_value=1000.01),
			dict(discount_type="Amount", discount_value=-1),
			dict(discount_type="Comp", discount_value=10),
			dict(discount_type=None, discount_value=10),
			dict(discount_type="Percent", discount_value="ten"),
			dict(discount_type="Percent", discount_value=True),
			dict(discount_type="Amount", discount_value=10.005),
			dict(night_discounts=[{"date": d(9), "discount_type": "Percent", "discount_value": 5}]),
			dict(night_discounts=[{"date": d(5), "discount_type": "Percent", "discount_value": 5},
			                      {"date": d(5), "discount_type": "Amount", "discount_value": 5}]),
			dict(night_discounts=[{"date": d(5), "discount_type": "Percent", "discount_value": 5,
			                       "price": 100}]),
			dict(night_discounts=[{"discount_type": "Percent", "discount_value": 5}]),
			dict(night_discounts=[{"date": d(6), "discount_type": "Amount", "discount_value": 1200}]),
			dict(night_discounts="not json"),
			dict(night_discounts={"date": d(5)}),
		]
		for kwargs in bad:
			with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
				with self.assertRaises(frappe.ValidationError):
					self.quote(**kwargs)
		# a full 100% comp is a valid price (its approval is the API's job)
		comp = self.quote(discount_type="Percent", discount_value=100)
		self.assertEqual(comp["room_total"], 0)
		self.assertEqual([n["rate"] for n in comp["nightly"]], [0, 0])

	def test_daily_details_override_the_stay_discount_night_by_night(self):
		q = self.quote(discount_type="Percent", discount_value=10, night_discounts=[
			{"date": d(6), "discount_type": "Amount", "discount_value": 300,
			 "discount_reason": "ADJ-RATE", "rate": 1}])
		first, second = q["nightly"]
		self.assertEqual((first["rate"], first["discount_source"]), (900, "stay"))
		self.assertEqual((second["rate"], second["discount_source"]), (700, "night"))
		self.assertEqual(second["discount_reason"], "ADJ-RATE")
		# `rate` from the client is an echo: the server prices the night
		self.assertAlmostEqual(q["room_total"], 1600, places=2)
		# an explicit zero takes one night out of the stay's discount
		q = self.quote(discount_type="Percent", discount_value=10, night_discounts=[
			{"date": d(6), "discount_type": "Percent", "discount_value": 0}])
		self.assertEqual([n["rate"] for n in q["nightly"]], [900, 1000])
		self.assertEqual(q["nightly"][1]["discount_amount"], 0)

	def test_a_rate_plan_is_discounted_on_its_planned_rate_not_disabled(self):
		plan = self.plan("CORP", "Percent", -20)
		q = self.quote(rate_plan=plan, discount_type="Percent", discount_value=10)
		self.assertEqual([n["base_rate"] for n in q["nightly"]], [800, 800])
		self.assertEqual([n["rate"] for n in q["nightly"]], [720, 720])
		self.assertAlmostEqual(q["room_base_total"], 1600, places=2)
		self.assertAlmostEqual(q["room_total"], 1440, places=2)
		self.assertEqual(q["rate_plan"], plan)

	def test_a_contract_or_fixed_rate_stays_gross_and_the_discount_is_separate(self):
		q = self.quote(agreed_rate=1200, discount_type="Percent", discount_value=10)
		for night in q["nightly"]:
			self.assertEqual(night["base_rate"], 1200)
			self.assertEqual(night["rate"], 1080)
			self.assertEqual(night["fixed_rate"], 1)
		fixed = self.quote(agreed_rate=1200)
		self.assertEqual([n["rate"] for n in fixed["nightly"]], [1200, 1200])

	def test_quote_with_contract_passes_the_discount_through(self):
		with patch.object(pricing, "contract_rate", return_value=(1200.0, {
				"currency": "USD", "nightly": 100.0, "fx_rate": 12.0,
				"fx_date": "2031-03-01", "property_currency": "INR"})):
			plan = self.plan("CON")
			q = pricing.quote_with_contract(PROP_A, self.rt, d(5), d(7), rate_plan=plan,
			                                discount_type="Amount", discount_value=200)
		self.assertEqual(q["agreed_rate"], 1200)
		self.assertEqual([n["rate"] for n in q["nightly"]], [1000, 1000])
		self.assertEqual([n["base_rate"] for n in q["nightly"]], [1200, 1200])

	def test_typed_night_prices_refuse_a_discount(self):
		with self.assertRaises(frappe.ValidationError):
			self.quote(night_prices=[{"date": d(5), "price": 900}],
			           discount_type="Percent", discount_value=10)

	def test_a_quote_without_a_discount_keeps_its_figures(self):
		q = self.quote()
		for night in q["nightly"]:
			self.assertEqual(night["base_rate"], night["rate"])
			self.assertEqual(night["discount_amount"], 0)
			self.assertIsNone(night["discount_type"])
			self.assertEqual(night["fixed_rate"], 0)
		self.assertEqual(q["reservation_discount_total"], 0)
		self.assertEqual(q["room_base_total"], q["room_total"])
		self.assertEqual(q["discount"], 0)


class ReservationDiscountBookingTests(DiscountTestCase):
	"""create_booking / walk_in: governed, locked night by night, billed once."""

	def setUp(self):
		super().setUp()
		set_setting(PROP_A, "rate_override_limit_pct", 15)

	def test_create_booking_locks_gross_discount_and_net_on_every_night(self):
		out = self.book(discount_type="Percent", discount_value=10,
		                discount_reason="RATE-PROMO")
		res = frappe.get_doc("Reservation", out["reservation"])
		self.assertEqual((res.discount_type, float(res.discount_value), res.discount_reason),
		                 ("Percent", 10, "RATE-PROMO"))
		self.assertEqual(self.rows(res.name), [(1000, 100, 900), (1000, 100, 900)])
		self.assertAlmostEqual(float(res.discount_amount), 200, places=2)
		q = self.quote(discount_type="Percent", discount_value=10)
		self.assertAlmostEqual(float(res.amount_after_tax), q["amount_after_tax"], places=2)
		self.assertAlmostEqual(out["discount"], 200, places=2)
		logs = self.discount_logs(res.name)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].actor, DESK)
		self.assertIn("RATE-PROMO", logs[0].rationale)
		# the rate card is untouched
		self.assertEqual(float(frappe.db.get_value("Room Type", self.rt, "base_price")), 1000)

	def test_the_folio_bills_the_net_night_and_never_discounts_twice(self):
		# 150 off a 1,000 night is 15%: the desk's own limit here
		out = self.book(discount_type="Amount", discount_value=150,
		                discount_reason="RATE-PROMO")
		res = frappe.get_doc("Reservation", out["reservation"])
		folio_mod.open_folio(res)
		folio_mod.post_room_night(res, d(5))
		self.assertEqual([round(float(c.amount), 2) for c in charges(res.name, "Room")], [850])
		room = charges(res.name, "Room")[0]
		self.assertIn("1,000.00 less discount 150.00", room.description)
		self.assertFalse(frappe.get_all("Folio Charge", filters={
			"parent": folio_mod.target_folio(res, "Room"), "charge_type": "Discount"}))

	def test_a_voucher_line_carries_only_the_voucher(self):
		frappe.db.delete("Discount Voucher", {"property": PROP_A, "voucher_code": "UTFIFTY"})
		frappe.get_doc({"doctype": "Discount Voucher", "property": PROP_A,
		                "voucher_code": "UTFIFTY", "discount_type": "Amount",
		                "value": 50}).insert(ignore_permissions=True)
		out = self.book(voucher_code="UTFIFTY", discount_type="Percent",
		                discount_value=10, discount_reason="RATE-PROMO")
		res = frappe.get_doc("Reservation", out["reservation"])
		self.assertAlmostEqual(float(res.discount_amount), 250, places=2)
		folio = folio_mod.open_folio(res)
		lines = frappe.get_all("Folio Charge", filters={"parent": folio, "charge_type": "Discount"},
		                       pluck="amount")
		self.assertEqual([round(float(a), 2) for a in lines], [-50])

	def test_an_unrelated_save_keeps_the_locked_discount(self):
		out = self.book(discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO")
		res = frappe.get_doc("Reservation", out["reservation"])
		key = res.rate_lock_key
		res.special_requests = "Late arrival"
		res.save(ignore_permissions=True)
		res.reload()
		self.assertEqual(res.rate_lock_key, key)
		self.assertEqual(self.rows(res.name), [(1000, 100, 900), (1000, 100, 900)])

	def test_a_discount_written_outside_the_governed_actions_is_refused(self):
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0])
		res.discount_type, res.discount_value, res.discount_reason = "Percent", 50, "RATE-PROMO"
		with self.assertRaises(frappe.PermissionError):
			res.save(ignore_permissions=True)
		res.reload()
		res.night_rates[0].discount_type = "Percent"
		res.night_rates[0].discount_value = 90
		with self.assertRaises(frappe.PermissionError):
			res.save(ignore_permissions=True)
		self.assertEqual(self.rows(res.name), [(1000, 0, 1000), (1000, 0, 1000)])
		with self.assertRaises(frappe.PermissionError):
			make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[1],
			                 discount_type="Amount", discount_value=500,
			                 discount_reason="RATE-PROMO")

	def test_walk_in_prices_the_discount_before_the_money_is_taken(self):
		today = nowdate()
		out = self.as_user(DESK, api.walk_in, PROP_A, self.rt, self.rooms[2],
		                   str(add_days(today, 2)), "UT Walk Discount", defer_check_in=1,
		                   discount_type="Percent", discount_value=10,
		                   discount_reason="RATE-PROMO")
		res = frappe.get_doc("Reservation", out["reservation"])
		self.assertEqual(self.rows(res.name), [(1000, 100, 900), (1000, 100, 900)])
		self.assertAlmostEqual(out["amount_after_tax"], float(res.amount_after_tax), places=2)
		plain = pricing.quote(PROP_A, self.rt, today, str(add_days(today, 2)))
		self.assertAlmostEqual(plain["amount_after_tax"] - out["amount_after_tax"],
		                       200 * (1 + plain["nightly"][0]["gst_rate"] / 100), places=2)

	def test_get_quote_previews_the_discount_and_its_approval(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		q = self.as_user(DESK, api.get_quote, PROP_A, self.rt, d(5), d(7),
		                 discount_type="Percent", discount_value=25,
		                 discount_reason="RATE-PROMO")
		gov = q["discount_governance"]
		self.assertAlmostEqual(gov["base_total"], 2000, places=2)
		self.assertAlmostEqual(gov["discount_total"], 500, places=2)
		self.assertAlmostEqual(gov["net_total"], 1500, places=2)
		self.assertAlmostEqual(gov["effective_pct"], 25, places=2)
		self.assertEqual(gov["limit_pct"], 10)
		self.assertTrue(gov["needs_supervisor"])
		self.assertIn(GM, [s["user"] for s in gov["supervisors"]])
		self.assertNotIn(DESK, [s["user"] for s in gov["supervisors"]])
		within = self.as_user(DESK, api.get_quote, PROP_A, self.rt, d(5), d(7),
		                      discount_type="Percent", discount_value=5,
		                      discount_reason="RATE-PROMO")
		self.assertFalse(within["discount_governance"]["needs_supervisor"])
		self.assertNotIn("discount_governance", self.as_user(
			DESK, api.get_quote, PROP_A, self.rt, d(5), d(7)))


class ReservationDiscountGovernanceTests(DiscountTestCase):
	"""One set of rules for booking, walk-in and an existing stay."""

	def test_a_discount_needs_an_active_rate_change_or_adjust_reason(self):
		set_setting(PROP_A, "rate_override_limit_pct", 15)
		for reason in (None, "", "VOID-ERR", "NO-SUCH-CODE"):
			with self.subTest(reason=reason):
				with self.assertRaises(frappe.ValidationError):
					self.book(discount_type="Percent", discount_value=10, discount_reason=reason)
		frappe.db.set_value("Reason Code", "RATE-PROMO", "disabled", 1)
		with self.assertRaises(frappe.ValidationError):
			self.book(discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO")
		out = self.book(discount_type="Percent", discount_value=10, discount_reason="ADJ-RATE")
		self.assertEqual(frappe.db.get_value("Reservation", out["reservation"], "discount_reason"),
		                 "ADJ-RATE")
		# no discount, no reason needed: existing callers are unchanged
		self.assertTrue(self.book()["reservation"])

	def test_a_desk_cut_over_the_limit_needs_a_supervisors_pin(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		args = dict(discount_type="Percent", discount_value=25, discount_reason="RATE-PROMO")
		with self.assertRaises(frappe.PermissionError):
			self.book(**args)
		with self.assertRaises(frappe.ValidationError):
			self.book(**args, supervisor_user=GM, supervisor_pin="0000")
		out = self.book(**args, supervisor_user=GM, supervisor_pin="4321")
		self.assertEqual(self.rows(out["reservation"]), [(1000, 250, 750), (1000, 250, 750)])
		self.assertEqual(self.discount_logs(out["reservation"])[0].approver, GM)

	def test_the_default_zero_limit_sends_every_desk_discount_to_a_supervisor(self):
		with self.assertRaises(frappe.PermissionError):
			self.book(discount_type="Amount", discount_value=10, discount_reason="RATE-PROMO")

	def test_nobody_approves_their_own_discount(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		self.desk_with_finance()
		with self.assertRaises(frappe.PermissionError) as raised:
			self.book(SELF, discount_type="Percent", discount_value=25,
			          discount_reason="RATE-PROMO", supervisor_user=SELF, supervisor_pin="5678")
		self.assertTrue(str(raised.exception).startswith("SUPERVISOR_SELF_APPROVAL:"))
		out = self.book(SELF, discount_type="Percent", discount_value=25,
		                discount_reason="RATE-PROMO", supervisor_user=GM, supervisor_pin="4321")
		self.assertEqual(self.discount_logs(out["reservation"])[0].approver, GM)

	def test_a_supervisor_reason_code_asks_for_a_pin_within_the_limit(self):
		set_setting(PROP_A, "rate_override_limit_pct", 30)
		with self.assertRaises(frappe.PermissionError):
			self.book(discount_type="Percent", discount_value=5, discount_reason="RATE-MGR")
		self.book(discount_type="Percent", discount_value=5, discount_reason="RATE-MGR",
		          supervisor_user=GM, supervisor_pin="4321")

	def test_a_full_comp_needs_a_supervisor_even_for_a_revenue_manager(self):
		args = dict(discount_type="Percent", discount_value=100, discount_reason="ADJ-RATE")
		with self.assertRaises(frappe.PermissionError):
			self.book(RATEDESK, **args)
		out = self.book(RATEDESK, **args, supervisor_user=GM, supervisor_pin="4321")
		self.assertEqual(self.rows(out["reservation"]), [(1000, 1000, 0), (1000, 1000, 0)])
		# a supervisor role is no sign-off of its own: another one signs a comp
		with self.assertRaises(frappe.PermissionError):
			self.book(GM, **args)
		# a revenue manager's deep (non-comp) cut is theirs to make
		self.book(RATEDESK, discount_type="Percent", discount_value=60, discount_reason="RATE-PROMO")

	def test_a_discount_under_the_rate_guardrail_floor_needs_a_supervisor(self):
		set_setting(PROP_A, "rate_override_limit_pct", 20)
		frappe.get_doc({"doctype": "Rate Guardrail", "property": PROP_A,
		                "floor_price": 900, "ceiling_price": 5000}
		               ).insert(ignore_permissions=True)
		args = dict(discount_type="Percent", discount_value=15, discount_reason="RATE-PROMO")
		with self.assertRaises(frappe.PermissionError):
			self.book(**args)
		pv = self.as_user(DESK, api.get_quote, PROP_A, self.rt, d(5), d(7), **args)
		self.assertEqual(pv["discount_governance"]["floor_breach"], [d(5), d(6)])
		self.book(**args, supervisor_user=GM, supervisor_pin="4321")
		self.book(discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO")

	def test_a_non_discountable_rate_plan_refuses_unless_a_manager_fixes_the_rate(self):
		set_setting(PROP_A, "rate_override_limit_pct", 50)
		plan = self.plan("NRF", discount_eligible=0)
		args = dict(rate_plan=plan, discount_type="Percent", discount_value=10,
		            discount_reason="RATE-PROMO")
		with self.assertRaises(frappe.ValidationError):
			self.book(**args)
		with self.assertRaises(frappe.PermissionError):
			self.book(**args, fixed_rate=1)
		out = self.book(RATEDESK, **args, fixed_rate=1)
		res = frappe.get_doc("Reservation", out["reservation"])
		self.assertEqual(int(res.fixed_rate), 1)
		self.assertEqual(self.rows(res.name), [(1000, 100, 900), (1000, 100, 900)])
		# an eligible plan (the default) takes a desk discount
		self.book(rate_plan=self.plan("BAR"), discount_type="Percent", discount_value=10,
		          discount_reason="RATE-PROMO")
		self.assertEqual(frappe.db.get_value("Rate Plan", f"{PROP_A}-BAR", "discount_eligible"), 1)

	def test_the_ai_agent_is_held_to_the_desk_limit(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		args = dict(discount_type="Percent", discount_value=5, discount_reason="RATE-PROMO")
		out = self.as_agent(DESK, api.create_booking, PROP_A, self.rt, d(5), d(7),
		                    "UT Agent Guest", **args)
		self.assertEqual(self.rows(out["reservation"]), [(1000, 50, 950), (1000, 50, 950)])
		over = dict(args, discount_value=25)
		# each call books a different guest: an identical agent booking
		# moments later is answered as a retry of the first one
		for user in (DESK, RATEDESK, GM):
			with self.subTest(user=user):
				with self.assertRaises(frappe.PermissionError) as raised:
					self.as_agent(user, api.create_booking, PROP_A, self.rt, d(5), d(7),
					              f"UT Agent Over {user}", **over)
				self.assertIn("hotel screen", str(raised.exception))
				with self.assertRaises(frappe.PermissionError):
					self.as_agent(user, api.create_booking, PROP_A, self.rt, d(5), d(7),
					              f"UT Agent Pin {user}", **args, supervisor_user=GM,
					              supervisor_pin="4321")
		with self.assertRaises(frappe.PermissionError):
			self.as_agent(AGENT, api.create_booking, PROP_A, self.rt, d(5), d(7),
			              "UT Agent Alone", **args)

	def test_booking_and_rate_options_describe_discounting(self):
		set_setting(PROP_A, "rate_override_limit_pct", 12)
		self.plan("BAR")
		self.plan("NRF", discount_eligible=0)
		opts = self.as_user(DESK, api.booking_options, PROP_A)
		eligible = {p["code"]: p["discount_eligible"] for p in opts["rate_plans"]}
		self.assertEqual((eligible["BAR"], eligible["NRF"]), (1, 0))
		self.assertTrue(opts["can_discount"])
		self.assertEqual(opts["rate_override_limit_pct"], 12)
		reasons = {r["name"]: r for r in opts["discount_reasons"]}
		self.assertTrue({"RATE-PROMO", "RATE-MGR", "ADJ-RATE", "ADJ-COMP"} <= set(reasons))
		self.assertNotIn("VOID-ERR", reasons)
		self.assertEqual(reasons["RATE-MGR"]["requires_supervisor"], 1)
		ro = self.as_user(DESK, rates.rate_options, PROP_A, self.rt, d(5), d(7))
		self.assertEqual({p["code"]: p["discount_eligible"] for p in ro["plans"]},
		                 {"BAR": 1, "NRF": 0})
		self.assertTrue(ro["can_discount"])
		self.assertEqual(ro["rate_override_limit_pct"], 12)
		self.assertIn("ADJ-RATE", {r["name"] for r in ro["discount_reasons"]})


class SetReservationDiscountTests(DiscountTestCase):
	"""An existing stay: Stay Details and Daily Details (OPERA)."""

	def setUp(self):
		super().setUp()
		set_setting(PROP_A, "rate_override_limit_pct", 20)
		self.res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0])

	def test_a_stay_discount_reprices_the_stay_under_the_governed_flag(self):
		hooks = frappe.get_doc_hooks().get("Reservation", {})
		events = {**hooks, "before_validate": [*hooks.get("before_validate", []),
		                                      f"{__name__}.observe_reservation_discount"]}
		observed = []
		with patch.dict(frappe.get_doc_hooks(), Reservation=events), patch.dict(
				frappe.flags, {DISCOUNT_FLAG_OBSERVATIONS: observed}):
			out = self.as_user(DESK, api.set_reservation_discount, self.res.name,
			                   discount_type="Percent", discount_value=15,
			                   reason_code="RATE-PROMO")
		self.assertEqual(observed, [(self.res.name, True)])
		self.assertTrue(out["ok"])
		self.assertEqual(self.rows(self.res.name), [(1000, 150, 850), (1000, 150, 850)])
		doc = frappe.get_doc("Reservation", self.res.name)
		self.assertEqual((doc.discount_type, float(doc.discount_value), doc.discount_reason),
		                 ("Percent", 15, "RATE-PROMO"))
		self.assertEqual(doc.room, self.rooms[0])
		self.assertEqual(len(self.discount_logs(self.res.name)), 1)
		# clearing it puts the stay back on its rate
		self.as_user(DESK, api.set_reservation_discount, self.res.name)
		self.assertEqual(self.rows(self.res.name), [(1000, 0, 1000), (1000, 0, 1000)])

	def test_the_contract_snapshot_and_agreed_rate_stay_as_booked(self):
		frappe.db.set_value("Reservation", self.res.name, {
			"agreed_rate": 1200, "contract_tariff": json.dumps({"agreed_rate": 1200})})
		doc = frappe.get_doc("Reservation", self.res.name)
		doc.flags.kamra_agreed_rate_override = True
		doc.save(ignore_permissions=True)
		self.as_user(REVENUE, api.set_reservation_discount, self.res.name,
		             discount_type="Amount", discount_value=200, reason_code="RATE-MGR")
		doc.reload()
		self.assertEqual(float(doc.agreed_rate), 1200)
		self.assertEqual(json.loads(doc.contract_tariff), {"agreed_rate": 1200})
		self.assertEqual(self.rows(self.res.name), [(1200, 200, 1000), (1200, 200, 1000)])

	def test_daily_details_discount_only_the_chosen_nights(self):
		self.as_user(DESK, api.set_reservation_discount, self.res.name,
		             reason_code="RATE-PROMO", night_discounts=[
			             {"date": d(6), "discount_type": "Amount", "discount_value": 100}])
		self.assertEqual(self.rows(self.res.name), [(1000, 0, 1000), (1000, 100, 900)])
		doc = frappe.get_doc("Reservation", self.res.name)
		second = doc.night_rates[1]
		self.assertEqual((second.discount_type, float(second.discount_value), second.discount_reason),
		                 ("Amount", 100, "RATE-PROMO"))
		self.assertFalse(doc.night_rates[0].discount_type)
		# a later unrelated save keeps the night's discount
		key = doc.rate_lock_key
		doc.special_requests = "Quiet room"
		doc.save(ignore_permissions=True)
		doc.reload()
		self.assertEqual(doc.rate_lock_key, key)

	def test_charged_nights_keep_what_the_folio_holds(self):
		stay = make_reservation(PROP_A, self.rt, d(0), d(3), status="Checked In",
		                        room=self.rooms[1])
		folio_mod.post_room_night(frappe.get_doc("Reservation", stay.name), d(0))
		with self.assertRaises(frappe.ValidationError):
			self.as_user(DESK, api.set_reservation_discount, stay.name,
			             reason_code="RATE-PROMO", night_discounts=[
				             {"date": d(0), "discount_type": "Percent", "discount_value": 10}])
		self.as_user(DESK, api.set_reservation_discount, stay.name,
		             discount_type="Percent", discount_value=10, reason_code="RATE-PROMO")
		self.assertEqual(self.rows(stay.name),
		                 [(1000, 0, 1000), (1000, 100, 900), (1000, 100, 900)])
		folio_mod.post_remaining_nights(frappe.get_doc("Reservation", stay.name))
		self.assertEqual(sorted(round(float(c.amount), 2) for c in charges(stay.name, "Room")),
		                 [900, 900, 1000])

	def test_clearing_an_in_house_discount_needs_no_sign_off_for_billed_nights(self):
		"""A night already on the folio keeps the discount it was billed with;
		taking the discount off the nights still to come is not a new cut."""
		set_setting(PROP_A, "rate_override_limit_pct", 0)
		stay = make_reservation(PROP_A, self.rt, d(0), d(3), status="Checked In",
		                        room=self.rooms[2])
		self.as_user(DESK, api.set_reservation_discount, stay.name,
		             discount_type="Percent", discount_value=10, reason_code="RATE-PROMO",
		             supervisor_user=GM, supervisor_pin="4321")
		folio_mod.post_room_night(frappe.get_doc("Reservation", stay.name), d(0))
		pv = self.as_user(DESK, api.discount_preview, stay.name)
		self.assertFalse(pv["needs_supervisor"])
		self.as_user(DESK, api.set_reservation_discount, stay.name)
		self.assertEqual(self.rows(stay.name),
		                 [(1000, 100, 900), (1000, 0, 1000), (1000, 0, 1000)])

	def test_the_preview_writes_nothing_and_says_when_a_pin_is_needed(self):
		pv = self.as_user(DESK, api.discount_preview, self.res.name,
		                  discount_type="Percent", discount_value=30, reason_code="RATE-PROMO")
		self.assertAlmostEqual(pv["base_total"], 2000, places=2)
		self.assertAlmostEqual(pv["discount_total"], 600, places=2)
		self.assertAlmostEqual(pv["net_total"], 1400, places=2)
		self.assertAlmostEqual(pv["effective_pct"], 30, places=2)
		self.assertTrue(pv["needs_supervisor"])
		self.assertTrue(pv["needs_confirmation"])
		self.assertEqual([n["rate"] for n in pv["nights"]], [700, 700])
		self.assertEqual(self.rows(self.res.name), [(1000, 0, 1000), (1000, 0, 1000)])
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, api.set_reservation_discount, self.res.name,
			             discount_type="Percent", discount_value=30, reason_code="RATE-PROMO")
		self.as_user(DESK, api.set_reservation_discount, self.res.name,
		             discount_type="Percent", discount_value=30, reason_code="RATE-PROMO",
		             supervisor_user=GM, supervisor_pin="4321")
		self.assertEqual(self.rows(self.res.name), [(1000, 300, 700), (1000, 300, 700)])
		self.assertEqual(self.discount_logs(self.res.name)[0].approver, GM)

	def test_the_new_price_is_confirmed_first(self):
		with self.assertRaises(api.PriceConfirmationRequired):
			self.as_user(DESK, api.set_reservation_discount, self.res.name,
			             discount_type="Percent", discount_value=10,
			             reason_code="RATE-PROMO", confirm_price=0)
		self.assertEqual(self.rows(self.res.name), [(1000, 0, 1000), (1000, 0, 1000)])

	def test_only_an_active_stay_takes_a_discount(self):
		frappe.db.set_value("Reservation", self.res.name, "status", "Checked Out")
		with self.assertRaises(frappe.ValidationError):
			self.as_user(DESK, api.set_reservation_discount, self.res.name,
			             discount_type="Percent", discount_value=10, reason_code="RATE-PROMO")

	def test_set_agreed_rate_is_unchanged_and_marks_the_fixed_rate(self):
		self.as_user(REVENUE, api.set_agreed_rate, self.res.name, 1200, "RATE-MGR",
		             confirm_price=1)
		doc = frappe.get_doc("Reservation", self.res.name)
		self.assertEqual(float(doc.agreed_rate), 1200)
		self.assertEqual(int(doc.fixed_rate), 1)
		self.assertEqual([float(r.rate) for r in doc.night_rates], [1200, 1200])
		# a discount on the fixed rate is measured against it
		self.as_user(DESK, api.set_reservation_discount, self.res.name,
		             discount_type="Percent", discount_value=10, reason_code="RATE-PROMO")
		self.assertEqual(self.rows(self.res.name), [(1200, 120, 1080), (1200, 120, 1080)])
