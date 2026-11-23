"""Complimentary and House Use rate plans, round 2 (Neon's R1 review of
#38): the comp sign-off can't be bypassed through a plan's category or the
settings, a discounted comp takes both sign-offs, and the smaller findings.

OPERA keeps rate code configuration and comp authorization as separate,
restricted privileges, and a change to a rate code reaches no reservation
already booked: the reservation keeps what it was booked with until it is
refreshed, and End of Day statistics stay as the night closed. So:

- a stay keeps the category it was booked on (Reservation.rate_category,
  written by its own plan change only); a plan's category changed later
  re-classifies no stay, booked, in house or closed;
- changing a plan's category is the configuration privilege's (the
  property's administrators), logged, and the user who set it is kept on the
  plan (Rate Plan.rate_category_set_by);
- the settings that define the categories and their approvers take the same
  privilege, not the module's editors, and every change is logged;
- nobody signs off a stay on a plan whose category they set, nor their own;
- a discounted comp needs both sign-offs: one manager whose roles cover both
  signs both, otherwise each is signed off by its own approver.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt

from kamra import api
from kamra.tests.audit_fixtures import (
	TODAY,
	YESTERDAY,
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
from kamra.tests.test_reservation_discounts import (
	DESK,
	GM,
	PROP_A,
	REVENUE,
	SELF,
	DiscountTestCase,
	set_setting,
)

# a manager who is both a discount approver (Hotel Admin) and, here, the
# property's comp approver (Revenue Manager)
BOTH = "ut.comp.both@test.local"


def logs(action, name):
	return frappe.get_all("Agent Action Log",
	                      filters={"action_type": action, "reference_name": name},
	                      fields=["actor", "approver", "rationale"], order_by="creation asc")


class CompCase(DiscountTestCase):
	def setUp(self):
		super().setUp()
		self.comp = self.plan("COMP", "Percent", -100, rate_category="Complimentary")
		self.bar = self.plan("BAR")

	def pin(self, user, pin):
		frappe.db.delete("Cashier PIN", {"name": user})
		frappe.get_doc({"doctype": "Cashier PIN", "user": user, "pin": pin}
		               ).insert(ignore_permissions=True)

	def set_category(self, user, plan, category):
		doc = frappe.get_doc("Rate Plan", plan)
		doc.rate_category = category
		self.as_user(user, doc.save)

	def category_of(self, plan):
		return frappe.db.get_value("Rate Plan", plan, "rate_category")


class PlanCategoryPrivilegeTests(CompCase):
	"""A plan's category is set by the configuration privilege only, and
	every change is logged with who made it."""

	def test_a_revenue_manager_cant_change_a_plans_category(self):
		# Neon (a): a -100% plan made Standard by the Revenue Manager
		sneak = self.as_user(REVENUE, lambda: frappe.get_doc({
			"doctype": "Rate Plan", "property": PROP_A, "rate_plan_name": "SNEAK",
			"code": "SNEAK", "modifier_type": "Percent", "modifier_value": -100}).insert().name)
		with self.assertRaises(frappe.PermissionError):
			self.set_category(REVENUE, sneak, "Complimentary")
		self.assertEqual(self.category_of(sneak), "Standard")
		with self.assertRaises(frappe.PermissionError):
			self.set_category(REVENUE, self.comp, "Standard")
		self.assertEqual(self.category_of(self.comp), "Complimentary")

	def test_a_revenue_manager_cant_make_a_plan_in_another_category(self):
		with self.assertRaises(frappe.PermissionError):
			self.as_user(REVENUE, lambda: frappe.get_doc({
				"doctype": "Rate Plan", "property": PROP_A, "rate_plan_name": "COMP2",
				"code": "COMP2", "modifier_type": "Percent", "modifier_value": -100,
				"rate_category": "Complimentary"}).insert())
		self.assertFalse(frappe.db.exists("Rate Plan", f"{PROP_A}-COMP2"))

	def test_a_category_change_is_logged_and_its_setter_kept(self):
		self.set_category(GM, self.bar, "House Use")
		self.assertEqual(self.category_of(self.bar), "House Use")
		self.assertEqual(frappe.db.get_value("Rate Plan", self.bar, "rate_category_set_by"), GM)
		row = logs("rate_category_changed", self.bar)
		self.assertEqual([r.actor for r in row], [GM])
		self.assertIn("Standard → House Use", row[0].rationale)

	def test_a_save_that_keeps_the_category_changes_nothing(self):
		self.set_category(GM, self.bar, "House Use")
		doc = frappe.get_doc("Rate Plan", self.bar)
		doc.cancellation_policy = "UT free until 18:00"
		self.as_user(REVENUE, doc.save)
		self.assertEqual(frappe.db.get_value("Rate Plan", self.bar, "rate_category_set_by"), GM)
		self.assertEqual(len(logs("rate_category_changed", self.bar)), 1)

	def test_a_rest_put_cant_write_who_set_the_category(self):
		self.set_category(GM, self.bar, "House Use")
		self.as_user(REVENUE, frappe.client.set_value, "Rate Plan", self.bar,
		             "rate_category_set_by", DESK)
		self.assertEqual(frappe.db.get_value("Rate Plan", self.bar, "rate_category_set_by"), GM)


class StayKeepsItsCategoryTests(CompCase):
	"""A stay keeps the category it was booked on: a plan's category changed
	later re-classifies no stay (OPERA: a rate code change reaches no
	reservation already booked)."""

	def stay_category(self, name):
		from kamra.room_statistics import category_rules, stay_categories
		return stay_categories(category_rules(PROP_A), [name])[name]

	def test_a_booked_stay_keeps_its_category_when_the_plan_changes(self):
		# Neon (a): a stay booked at no charge on a Standard plan, then the
		# plan made Complimentary - the stay is not a signed-off comp
		sneak = self.plan("SNEAK", "Percent", -100)
		out = self.book(DESK, rate_plan=sneak)
		name = out["reservation"]
		self.assertEqual(frappe.db.get_value("Reservation", name, "rate_category"), "Standard")
		self.set_category(GM, sneak, "Complimentary")
		self.assertEqual(frappe.db.get_value("Reservation", name, "rate_category"), "Standard")
		self.assertEqual(self.stay_category(name), "Standard")
		self.assertEqual(logs("comp_rate_sign_off", name), [])

	def test_flip_book_flip_back_is_not_a_signed_off_comp(self):
		# Neon (b): the GM takes COMP out of its category, books it for
		# themselves without a sign-off, and puts the category back
		self.set_category(GM, self.comp, "Standard")
		out = self.book(GM, rate_plan=self.comp)
		self.set_category(GM, self.comp, "Complimentary")
		name = out["reservation"]
		self.assertEqual(self.stay_category(name), "Standard")
		self.assertEqual(logs("comp_rate_sign_off", name), [])
		# and the GM, who set COMP's category, signs off no booking on it
		with self.assertRaises(frappe.PermissionError) as raised:
			self.book(DESK, rate_plan=self.comp, supervisor_user=GM, supervisor_pin="4321")
		self.assertTrue(str(raised.exception).startswith("SUPERVISOR_SELF_APPROVAL:"))
		self.desk_with_finance()
		signed = self.book(DESK, rate_plan=self.comp, supervisor_user=SELF, supervisor_pin="5678")
		self.assertEqual(frappe.db.get_value("Reservation", signed["reservation"], "rate_category"),
		                 "Complimentary")

	def test_a_plain_save_cant_write_a_stays_category(self):
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0], rate_plan=self.bar)
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, frappe.client.set_value, "Reservation", res.name,
			             "rate_category", "Complimentary")
		self.assertEqual(frappe.db.get_value("Reservation", res.name, "rate_category"), "Standard")

	def test_the_quote_leaves_out_who_set_the_plans_category(self):
		self.set_category(GM, self.bar, "Complimentary")
		gov = self.as_user(DESK, api.get_quote, PROP_A, self.rt, d(5), d(7),
		                   rate_plan=self.bar)["comp_governance"]
		self.assertTrue(gov["needs_supervisor"])
		self.assertNotIn(GM, [s["user"] for s in gov["supervisors"]])


class CategorySettingsPrivilegeTests(CompCase):
	"""Neon (c): the settings that define the categories and their approvers
	take the configuration privilege, not the Revenue page's editors."""

	def save(self, user, changes):
		from kamra.settings import save_settings
		return self.as_user(user, save_settings, PROP_A, "revenue", changes)

	def test_a_revenue_manager_cant_change_the_categories_or_their_approvers(self):
		from kamra.room_statistics import category_rules
		for changes in ({"rate_categories": "Standard\nComplimentary: no ADR\nHouse Use: no ADR"},
		                {"rate_category_approvers": "Front Desk"}):
			with self.subTest(changes=list(changes)):
				with self.assertRaises(frappe.PermissionError):
					self.save(REVENUE, changes)
		rules = category_rules(PROP_A)
		self.assertEqual(rules.sign_off, frozenset({"Complimentary", "House Use"}))
		self.assertEqual(rules.approvers, ("Hotel Admin", "Finance"))

	def test_a_revenue_manager_still_changes_the_rest_of_the_page(self):
		out = self.save(REVENUE, {"overbooking_pct": 5})
		self.assertEqual(out["values"]["overbooking_pct"], 5)

	def test_an_administrator_changes_them_and_it_is_logged(self):
		self.save(GM, {"rate_category_approvers": "Finance"})
		from kamra.room_statistics import category_rules
		self.assertEqual(category_rules(PROP_A).approvers, ("Finance",))
		row = frappe.get_all("Agent Action Log", filters={
			"action_type": "settings_changed", "reference_name": PROP_A},
			fields=["actor", "before_snapshot", "after_snapshot"])
		self.assertEqual([r.actor for r in row], [GM])
		self.assertIn("rate_category_approvers", row[0].after_snapshot)

	def test_the_page_shows_them_read_only_to_a_revenue_manager(self):
		from kamra.settings import settings_schema

		def editable(user):
			page = next(m for m in self.as_user(user, settings_schema, PROP_A)["modules"]
			            if m["id"] == "revenue")
			return {s["key"]: s.get("can_edit", True)
			        for sec in page["sections"] for s in sec["settings"]}

		rm = editable(REVENUE)
		self.assertEqual((rm["rate_categories"], rm["rate_category_approvers"]), (False, False))
		self.assertTrue(rm["overbooking_pct"])
		gm = editable(GM)
		self.assertEqual((gm["rate_categories"], gm["rate_category_approvers"]), (True, True))


class DiscountedCompSignOffTests(CompCase):
	"""Neon P1 #4: a comp plan that also takes a discount over the desk's
	limit needs both sign-offs. One manager whose roles cover both signs
	both (OPERA's one authorizer for both prompts); otherwise each is signed
	off by its own approver."""

	def setUp(self):
		super().setUp()
		set_setting(PROP_A, "rate_category_approvers", "Revenue Manager")
		self.half = self.plan("HALF", "Percent", -50, rate_category="Complimentary",
		                      discount_eligible=1)
		self.pin(REVENUE, "2468")
		ensure_user(BOTH, ["Hotel Admin", "Revenue Manager"])
		self.pin(BOTH, "1357")

	def discounted(self, **sign):
		return self.book(DESK, rate_plan=self.half, discount_type="Percent", discount_value=50,
		                 discount_reason="RATE-PROMO", **sign)

	def test_the_quote_asks_for_both_and_names_each_approver(self):
		q = self.as_user(DESK, api.get_quote, PROP_A, self.rt, d(5), d(7), rate_plan=self.half,
		                 discount_type="Percent", discount_value=50, discount_reason="RATE-PROMO")
		self.assertTrue(q["discount_governance"]["needs_supervisor"])
		self.assertTrue(q["comp_governance"]["needs_supervisor"])
		self.assertIn(GM, [s["user"] for s in q["discount_governance"]["supervisors"]])
		comp = [s["user"] for s in q["comp_governance"]["supervisors"]]
		self.assertIn(REVENUE, comp)
		self.assertNotIn(GM, comp)

	def test_each_sign_off_by_its_own_approver(self):
		out = self.discounted(supervisor_user=GM, supervisor_pin="4321",
		                      comp_supervisor_user=REVENUE, comp_supervisor_pin="2468")
		name = out["reservation"]
		self.assertEqual([r.approver for r in self.discount_logs(name)], [GM])
		self.assertEqual([r.approver for r in logs("comp_rate_sign_off", name)], [REVENUE])

	def test_one_manager_whose_roles_cover_both_signs_both(self):
		out = self.discounted(supervisor_user=BOTH, supervisor_pin="1357")
		name = out["reservation"]
		self.assertEqual([r.approver for r in self.discount_logs(name)], [BOTH])
		self.assertEqual([r.approver for r in logs("comp_rate_sign_off", name)], [BOTH])

	def test_the_discount_approver_alone_doesnt_sign_off_the_comp(self):
		with self.assertRaises(frappe.PermissionError) as raised:
			self.discounted(supervisor_user=GM, supervisor_pin="4321")
		# the approver's roles come from the property's settings
		self.assertIn("Revenue Manager", str(raised.exception))
		self.assertNotIn("Hotel Admin or Finance", str(raised.exception))
		self.assertEqual(self.booked(), 0)

	def booked(self):
		return frappe.db.count("Reservation", {"property": PROP_A,
		                                       "guest_name": "UT Discount Guest"})


class GroupCompTests(CompCase):
	"""Neon P2: several rooms on a comp plan carry the sign-off to every room."""

	def test_a_group_on_a_comp_plan_takes_the_sign_off(self):
		out = self.as_user(DESK, api.create_group_booking, PROP_A, "UT Comp Group", d(5), d(7),
		                   [{"room_type": self.rt, "count": 2}], "UT Comp Lead",
		                   rate_plan=self.comp, comp_supervisor_user=GM,
		                   comp_supervisor_pin="4321")
		self.assertEqual((len(out["created"]), out["skipped"]), (2, []))
		for name in out["created"]:
			self.assertEqual([r.approver for r in logs("comp_rate_sign_off", name)], [GM])


class ApproverMessageTests(CompCase):
	"""Neon P2: the refusal names the roles that can approve."""

	def test_the_refusal_names_the_configured_roles(self):
		set_setting(PROP_A, "rate_category_approvers", "Revenue Manager")
		with self.assertRaises(frappe.PermissionError) as raised:
			self.book(DESK, rate_plan=self.comp, supervisor_user=GM, supervisor_pin="4321")
		self.assertIn("It needs a Revenue Manager user.", str(raised.exception))

	def test_the_default_roles_read_as_before(self):
		from kamra.authz import verify_supervisor
		ensure_user("ut.comp.desk2@test.local", ["Front Desk"])
		self.pin("ut.comp.desk2@test.local", "1111")
		with self.assertRaises(frappe.PermissionError) as raised:
			verify_supervisor(PROP_A, "ut.comp.desk2@test.local", "1111")
		self.assertIn("It needs a Hotel Admin or Finance user.", str(raised.exception))


class RateLockKeyTests(CompCase):
	"""Neon P2: a REST PUT can't rewrite the rate-lock key to force a
	re-quote of a stay's locked nights."""

	def totals(self, name):
		return frappe.db.get_value("Reservation", name,
		                           ["amount_after_tax", "rate_lock_key"], as_dict=True)

	def test_a_rest_put_cant_rewrite_the_lock_key(self):
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[3])
		before = self.totals(res.name)
		self.assertGreater(flt(before.amount_after_tax), 0)
		# today's rate card would price the stay at half
		frappe.db.set_value("Room Type", self.rt, "base_price", 500)
		frappe.clear_document_cache("Room Type", self.rt)
		for change in ({"rate_lock_key": "fake"}, {"rate_lock_key": "fake", "amount_after_tax": 1}):
			with self.subTest(change=list(change)):
				with self.assertRaises(frappe.PermissionError):
					self.as_user(DESK, frappe.client.set_value, "Reservation", res.name, change)
				self.assertEqual(self.totals(res.name), before)


# ---------------------------------------------------------------------------
# Round 3 (Hydrogen's R2 review & live check of #38)
# ---------------------------------------------------------------------------

PROP_R3 = "UT Comp R3"


class OccupancyNowAndForecastSummaryTests(CompCase):
	"""P1 #1 (Today screen): occupancy_now returns the rule-counted occupied
	count matching occupancy_pct, and property_dashboard sends server-computed
	forecast_summary (30/60/90-day and weekly occupancy by the same rule, no
	money) so the browser never recomputes occupancy."""

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger", "kamra.api")
		make_property(PROP_A, business_date=TODAY)
		set_setting(
			PROP_A,
			"rate_categories",
			"Standard\nComplimentary: sign-off, no ADR\nHouse Use: sign-off, no ADR, no occupancy",
		)
		# four sellable rooms (mark the 5th Out of Order to prove sellable = 4)
		frappe.db.set_value("Room", self.rooms[4], "housekeeping_status", "Out of Order")
		self.house = self.plan("HOUSE", "Percent", -100, rate_category="House Use")
		self.stays = []
		for room, plan in zip(
			self.rooms[:4],
			(self.bar, self.comp, self.comp, self.house),
			strict=True,
		):
			doc = frappe.get_doc({
				"doctype": "Reservation", "property": PROP_A, "room_type": self.rt,
				"guest": make_guest(), "status": "Confirmed", "room": room, "rate_plan": plan,
				"check_in_date": YESTERDAY, "check_out_date": d(3), "adults": 2, "children": 0,
			})
			doc.flags.allow_past_check_in = True
			doc.flags.kamra_discount_supervisor = {"user": GM, "pin": "4321"}
			doc.insert(ignore_permissions=True)
			doc.status = "Checked In"
			doc.save(ignore_permissions=True)
			self.stays.append(doc)

	def test_occupancy_now_occupied_count_follows_the_shared_rule(self):
		from kamra import dashboards
		# 4 physically occupied rooms, 1 House Use (no occupancy): 3 of 4 = 75.0%
		out = dashboards.occupancy_now(PROP_A, {"occupied": 4})
		self.assertEqual(out, {"occupied": 3, "sellable": 4, "occupancy_pct": 75.0})

	def test_property_dashboard_sends_server_forecast_summary_for_desk_and_managers(self):
		from kamra import dashboards
		money = {"revenue", "adr", "revpar", "amount", "collections", "outstanding"}
		for user in (DESK, GM):
			with self.subTest(user=user):
				dash = self.as_user(user, dashboards.property_dashboard, PROP_A, TODAY)
				self.assertEqual(dash["occupancy_now"], {"occupied": 3, "sellable": 4, "occupancy_pct": 75.0})
				summary = dash["forecast_summary"]
				# 3 nights on the books (TODAY, d(1), d(2)) x 3 counted rooms = 9 room-nights
				# over 4 sellable rooms x 30 / 60 / 90 days -> 7.5%, 3.8%, 2.5%
				self.assertEqual(
					summary["spans"],
					[
						{"days": 30, "occupancy_pct": 7.5},
						{"days": 60, "occupancy_pct": 3.8},
						{"days": 90, "occupancy_pct": 2.5},
					],
				)
				week0 = summary["weeks"][0]
				# week 0: 3 nights x 4 booked = 12 booked, 3 x 3 = 9 counted over 28 = 32.1%
				self.assertEqual(week0["week_of"], TODAY)
				self.assertEqual((week0["booked"], week0["rooms_occupied"], week0["held"]), (12, 9, 0))
				self.assertEqual(week0["occupancy_pct"], 32.1)
				for row in summary["weeks"]:
					self.assertFalse(money & set(row))


class PostedNightCategoryImmutabilityTests(AuditTestCase):
	"""P1 #2 (OPERA End of Day statistics): each posted room night stamps its
	rate category on the Folio Charge at posting time. A later amend_stay plan
	change never restates closed nights, while unposted future nights follow
	the stay's new category. Existing posted nights are backfilled idempotently."""

	properties = (PROP_R3,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger", "kamra.api")
		make_property(PROP_R3, business_date=TODAY)
		frappe.db.delete("Rate Guardrail", {"property": PROP_R3})
		frappe.db.delete("Property Setting", {"property": PROP_R3})
		from kamra.settings import clear_cache
		clear_cache(PROP_R3)
		set_setting(
			PROP_R3,
			"rate_categories",
			"Standard\nComplimentary: sign-off, no ADR\nHouse Use: sign-off, no ADR, no occupancy",
		)
		self.rt = make_room_type(PROP_R3, "STD", base_price=1000)
		frappe.db.set_value("Room Type", self.rt, "base_price", 1000)
		frappe.clear_document_cache("Room Type", self.rt)
		self.rooms = [make_room(PROP_R3, self.rt, f"30{i}") for i in range(1, 6)]
		ensure_user(DESK, ["Front Desk"])
		ensure_user(GM, ["Hotel Admin"])
		frappe.db.delete("Cashier PIN", {"name": GM})
		frappe.get_doc({"doctype": "Cashier PIN", "user": GM, "pin": "4321"}
		               ).insert(ignore_permissions=True)

		self.bar = self._plan("BAR", 0, "Standard")
		self.half = self._plan("HALF", -50, "Complimentary")
		self.comp = self._plan("COMP", -100, "Complimentary")
		self.house = self._plan("HOUSE", -100, "House Use")

		# 5 stays in house from YESTERDAY through d(3)
		self.stays = [
			self._stay(self.rooms[0], self.bar),
			self._stay(self.rooms[1], self.bar),
			self._stay(self.rooms[2], self.half),
			self._stay(self.rooms[3], self.comp),
			self._stay(self.rooms[4], self.house),
		]
		from kamra import folio as folio_mod
		for res in self.stays:
			folio_mod.post_room_night(frappe.get_doc("Reservation", res.name), YESTERDAY)
		# +100 Rate Adjustment on stay0 on YESTERDAY -> Standard rev = 2100 over 2 = 1050 ADR;
		# total rev = 2600 over 5 = 520 RevPAR; 4 counted of 5 = 80.0% occupancy
		folio = frappe.get_doc("Folio", guest_folio(self.stays[0].name))
		folio.append("charges", {
			"posting_date": YESTERDAY, "charge_type": "Rate Adjustment",
			"reservation": self.stays[0].name, "description": "UT rate adj",
			"qty": 1, "rate": 100, "amount": 100, "gst_rate": 0,
		})
		folio.save(ignore_permissions=True)

	def _plan(self, code, modifier, category):
		name = f"{PROP_R3}-{code}"
		if frappe.db.exists("Rate Plan", name):
			frappe.delete_doc("Rate Plan", name, force=True, ignore_permissions=True)
		return frappe.get_doc({
			"doctype": "Rate Plan", "property": PROP_R3, "rate_plan_name": code, "code": code,
			"modifier_type": "Percent", "modifier_value": modifier, "rate_category": category,
		}).insert(ignore_permissions=True).name

	def _stay(self, room, plan):
		doc = frappe.get_doc({
			"doctype": "Reservation", "property": PROP_R3, "room_type": self.rt,
			"guest": make_guest(), "status": "Confirmed", "room": room, "rate_plan": plan,
			"check_in_date": YESTERDAY, "check_out_date": d(3), "adults": 2, "children": 0,
		})
		doc.flags.allow_past_check_in = True
		doc.flags.kamra_discount_supervisor = {"user": GM, "pin": "4321"}
		doc.insert(ignore_permissions=True)
		doc.status = "Checked In"
		doc.save(ignore_permissions=True)
		return doc

	def _as_user(self, user, fn, *args, **kwargs):
		frappe.set_user(user)  # nosemgrep: frappe-setuser -- acting as the persona
		try:
			return fn(*args, **kwargs)
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- restore

	def _assert_yesterday_unchanged(self):
		from kamra import audit_report, reports, rooms
		expected = {
			"rooms_sold": 5, "room_revenue": 2600, "adr": 1050, "revpar": 520,
			"occupancy_pct": 80.0, "rooms_by_category": {"Complimentary": 2, "House Use": 1},
		}
		for label, row in (
			("reports._day_stats", reports._day_stats(PROP_R3, YESTERDAY, 5)),
			("reports.range_stats", reports.range_stats(PROP_R3, YESTERDAY, YESTERDAY, 5)),
			("audit_report.build", audit_report.build(PROP_R3, YESTERDAY)["periods"][0]),
		):
			with self.subTest(report=label):
				self.assertEqual({k: row[k] for k in expected}, expected)
		owner_y = api.owner_briefing(PROP_R3, TODAY)["yesterday"]
		self.assertEqual(
			{k: owner_y[k] for k in ("adr", "revpar", "rooms_by_category")},
			{"adr": 1050, "revpar": 520, "rooms_by_category": {"Complimentary": 2, "House Use": 1}},
		)
		ins = rooms.room_insights(PROP_R3, from_date=YESTERDAY, to_date=YESTERDAY, refresh=1)
		self.assertEqual(ins["summary"]["occupancy_pct"], 80.0)

	def test_posted_nights_stamp_category_and_survive_mid_stay_plan_changes(self):
		from kamra import dashboards, folio as folio_mod, reports
		# 1. Every posted Room / Rate Adjustment line stamped its category at posting time
		for stay, cat in zip(
			self.stays,
			("Standard", "Standard", "Complimentary", "Complimentary", "House Use"),
			strict=True,
		):
			rows = frappe.get_all(
				"Folio Charge",
				filters={"reservation": stay.name, "posting_date": YESTERDAY,
				         "charge_type": ("in", ["Room", "Rate Adjustment"])},
				pluck="rate_category",
			)
			self.assertTrue(rows)
			self.assertEqual(set(rows), {cat})
		self._assert_yesterday_unchanged()

		# 2. COMP -> BAR mid-stay: closed YESTERDAY ADR must stay 1050 (not 700)
		self._as_user(DESK, api.amend_stay, self.stays[3].name, YESTERDAY, d(3),
		              confirm_price=1, rate_plan=self.bar)
		self._assert_yesterday_unchanged()

		# 3. BAR -> COMP mid-stay: closed YESTERDAY ADR must still stay 1050
		self._as_user(DESK, api.amend_stay, self.stays[1].name, YESTERDAY, d(3),
		              confirm_price=1, rate_plan=self.comp,
		              comp_supervisor_user=GM, comp_supervisor_pin="4321")
		self._assert_yesterday_unchanged()

		# 4. HOUSE -> BAR mid-stay: closed YESTERDAY stays 80.0%; unposted TODAY follows new categories
		self._as_user(DESK, api.amend_stay, self.stays[4].name, YESTERDAY, d(3),
		              confirm_price=1, rate_plan=self.bar)
		self._assert_yesterday_unchanged()

		# Unposted TODAY now has 3 Standard (stay0, stay3, stay4), 2 Comp (stay1, stay2), 0 House Use
		today_day = reports._day_stats(PROP_R3, TODAY, 5)
		self.assertEqual(today_day["occupancy_pct"], 100.0)
		self.assertEqual(today_day["rooms_by_category"], {"Complimentary": 2, "House Use": 0})
		self.assertEqual(
			(today_day["projected_room_revenue"], today_day["projected_adr"], today_day["projected_revpar"]),
			(3500, 1000, 700),
		)
		self.assertEqual(dashboards.occupancy_now(PROP_R3, {"occupied": 5}),
		                 {"occupied": 5, "sellable": 5, "occupancy_pct": 100.0})
		self.assertEqual(dashboards.occupancy_forecast(PROP_R3, TODAY, days=1)[0]["occupancy_pct"], 100.0)

		# 5. Post TODAY nights -> TODAY stamps the new categories while YESTERDAY stays intact
		for res in self.stays:
			folio_mod.post_room_night(frappe.get_doc("Reservation", res.name), TODAY)
		self._assert_yesterday_unchanged()
		posted_today = reports._day_stats(PROP_R3, TODAY, 5)
		self.assertEqual(
			{k: posted_today[k] for k in ("rooms_sold", "room_revenue", "adr", "revpar", "occupancy_pct", "rooms_by_category")},
			{"rooms_sold": 5, "room_revenue": 3500, "adr": 1000, "revpar": 700,
			 "occupancy_pct": 100.0, "rooms_by_category": {"Complimentary": 2, "House Use": 0}},
		)

	def test_credit_the_difference_keeps_the_original_posted_nights_category(self):
		set_setting(PROP_R3, "charged_night_rate_change", "Credit the difference")
		self._as_user(DESK, api.amend_stay, self.stays[0].name, YESTERDAY, d(3),
		              confirm_price=1, rate_plan=self.comp,
		              comp_supervisor_user=GM, comp_supervisor_pin="4321")
		adjs = frappe.get_all(
			"Folio Charge",
			filters={"reservation": self.stays[0].name, "charge_type": "Rate Adjustment",
			         "posting_date": TODAY},
			fields=["stay_date", "amount", "rate_category"],
		)
		self.assertEqual(len(adjs), 1)
		self.assertEqual(str(adjs[0].stay_date), YESTERDAY)
		self.assertEqual(flt(adjs[0].amount), -1000.0)
		self.assertEqual(adjs[0].rate_category, "Standard")

	def test_backfill_patch_populates_blank_room_charges_idempotently(self):
		from kamra.patches.v47 import backfill_folio_charge_rate_category
		# Add an F&B charge that must remain untouched
		folio = frappe.get_doc("Folio", guest_folio(self.stays[0].name))
		folio.append("charges", {
			"posting_date": YESTERDAY, "charge_type": "Food & Beverage",
			"reservation": self.stays[0].name, "description": "UT dinner",
			"qty": 1, "rate": 300, "amount": 300, "gst_rate": 0,
		})
		folio.save(ignore_permissions=True)

		# Clear rate_category on existing Room / Rate Adjustment rows to simulate pre-patch data
		frappe.db.sql(
			"""UPDATE `tabFolio Charge` fc
			   JOIN `tabFolio` f ON f.name = fc.parent
			   SET fc.rate_category = ''
			   WHERE f.property = %(p)s AND fc.reservation IN %(r)s""",
			{"p": PROP_R3, "r": (self.stays[0].name, self.stays[1].name, self.stays[3].name)},
		)
		# Simulate a legacy stay booked before categories on a property whose default category is Rack
		frappe.db.set_value("Reservation", self.stays[1].name, "rate_category", "")
		set_setting(
			PROP_R3,
			"rate_categories",
			"Rack\nComplimentary: sign-off, no ADR\nHouse Use: sign-off, no ADR, no occupancy",
		)

		backfill_folio_charge_rate_category.execute()

		stay0_room = frappe.get_all(
			"Folio Charge",
			filters={"reservation": self.stays[0].name, "charge_type": ("in", ["Room", "Rate Adjustment"])},
			pluck="rate_category",
		)
		self.assertEqual(set(stay0_room), {"Standard"})
		stay0_fnb = frappe.get_all(
			"Folio Charge",
			filters={"reservation": self.stays[0].name, "charge_type": "Food & Beverage"},
			pluck="rate_category",
		)
		self.assertEqual([c or "" for c in stay0_fnb], [""])
		# Legacy blank stay took the property's configured first category (Rack), not hardcoded Standard
		stay1_room = frappe.get_all(
			"Folio Charge",
			filters={"reservation": self.stays[1].name, "charge_type": "Room"},
			pluck="rate_category",
		)
		self.assertEqual(stay1_room, ["Rack"])
		stay3_room = frappe.get_all(
			"Folio Charge",
			filters={"reservation": self.stays[3].name, "charge_type": "Room"},
			pluck="rate_category",
		)
		self.assertEqual(stay3_room, ["Complimentary"])

		# Idempotency: mutating Reservation.rate_category later and re-running never overwrites stamped rows
		frappe.db.set_value("Reservation", self.stays[0].name, "rate_category", "House Use")
		backfill_folio_charge_rate_category.execute()
		stay0_after = frappe.get_all(
			"Folio Charge",
			filters={"reservation": self.stays[0].name, "charge_type": ("in", ["Room", "Rate Adjustment"])},
			pluck="rate_category",
		)
		self.assertEqual(set(stay0_after), {"Standard"})


class GroupBlockAndRoomingListCompTests(CompCase):
	"""P2 #3: create_group_with_rooming_list and pickup_group_room accept a
	comp sign-off once per operation for negotiated comp corporate blocks,
	and refuse cleanly without leaving an empty group behind."""

	def setUp(self):
		super().setUp()
		self.co_comp = "UT Comp Corp"
		if frappe.db.exists("Corporate Account", self.co_comp):
			frappe.delete_doc("Corporate Account", self.co_comp, force=True, ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Corporate Account", "company_name": self.co_comp,
			"negotiated_rate_plan": self.comp, "credit_allowed": 1,
		}).insert(ignore_permissions=True)

	def _blocks(self, n=3):
		return [{"room_type": self.rt, "rooms_blocked": n}]

	def _guests(self, *names):
		return [{"room_type": self.rt, "guest_name": n} for n in names]

	def test_rooming_list_on_comp_company_refuses_without_sign_off_leaving_no_group(self):
		before = frappe.db.count("Group Booking", {"property": PROP_A, "company": self.co_comp})
		with self.assertRaises(frappe.PermissionError):
			self.as_user(
				DESK, api.create_group_with_rooming_list, PROP_A, "UT Comp Unsigned",
				d(5), d(7), self._blocks(2), guests=self._guests("Guest A", "Guest B"),
				company=self.co_comp,
			)
		# No empty Group Booking left behind
		self.assertEqual(frappe.db.count("Group Booking", {"property": PROP_A, "company": self.co_comp}), before)
		# Self-approval refused
		with self.assertRaises(frappe.PermissionError) as raised:
			self.as_user(
				GM, api.create_group_with_rooming_list, PROP_A, "UT Comp Self",
				d(5), d(7), self._blocks(2), guests=self._guests("Guest A"),
				company=self.co_comp, comp_supervisor_user=GM, comp_supervisor_pin="4321",
			)
		self.assertTrue(str(raised.exception).startswith("SUPERVISOR_SELF_APPROVAL:"))
		# AI agent refused even with PIN
		with self.assertRaises(frappe.PermissionError):
			self.as_agent(
				DESK, api.create_group_with_rooming_list, PROP_A, "UT Comp Agent",
				d(5), d(7), self._blocks(2), guests=self._guests("Guest A"),
				company=self.co_comp, comp_supervisor_user=GM, comp_supervisor_pin="4321",
			)
		self.assertEqual(frappe.db.count("Group Booking", {"property": PROP_A, "company": self.co_comp}), before)

	def test_rooming_list_and_pickup_accept_comp_sign_off_once(self):
		# Price preview reports comp_governance for the company's negotiated comp plan
		preview = self.as_user(
			DESK, api.group_price_preview, PROP_A, d(5), d(7), self._blocks(3), company=self.co_comp,
		)
		self.assertTrue(preview["comp_governance"]["needs_supervisor"])
		self.assertEqual(preview["comp_governance"]["rate_category"], "Complimentary")

		# Quotation-only and empty rooming list need no PIN because no comp stay is booked yet
		quote = self.as_user(
			DESK, api.create_group_with_rooming_list, PROP_A, "UT Comp Quote",
			d(5), d(7), self._blocks(2), guests=self._guests("Guest Q"),
			company=self.co_comp, quotation_only=1,
		)
		self.assertEqual((quote["status"], quote["created"]), ("Open", []))

		# One sign-off authorizes all guests on the rooming list
		out = self.as_user(
			DESK, api.create_group_with_rooming_list, PROP_A, "UT Comp Retreat",
			d(5), d(7), self._blocks(3), guests=self._guests("Guest 1", "Guest 2"),
			company=self.co_comp, comp_supervisor_user=GM, comp_supervisor_pin="4321",
		)
		self.assertEqual((len(out["created"]), out["skipped"], out["held"]), (2, [], 1))
		for res_name in out["created"]:
			self.assertEqual(frappe.db.get_value("Reservation", res_name, "rate_category"), "Complimentary")
			self.assertEqual([r.approver for r in logs("comp_rate_sign_off", res_name)], [GM])
		self.assertEqual([r.approver for r in logs("group_created", out["group_booking"])], [GM])

		# group_detail surfaces comp_governance for subsequent pickups
		detail = self.as_user(DESK, api.group_detail, out["group_booking"])
		self.assertTrue(detail["comp_governance"]["needs_supervisor"])
		self.assertEqual(detail["comp_governance"]["rate_category"], "Complimentary")

		# Pickup without PIN or as AI agent is refused
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, api.pickup_group_room, out["group_booking"], self.rt, "Guest 3")
		with self.assertRaises(frappe.PermissionError):
			self.as_agent(
				DESK, api.pickup_group_room, out["group_booking"], self.rt, "Guest 3",
				comp_supervisor_user=GM, comp_supervisor_pin="4321",
			)

		# Pickup with supervisor PIN succeeds and logs approver
		picked = self.as_user(
			DESK, api.pickup_group_room, out["group_booking"], self.rt, "Guest 3",
			comp_supervisor_user=GM, comp_supervisor_pin="4321",
		)
		self.assertEqual(frappe.db.get_value("Reservation", picked["reservation"], "rate_category"),
		                 "Complimentary")
		self.assertEqual([r.approver for r in logs("comp_rate_sign_off", picked["reservation"])], [GM])
		self.assertEqual([r.approver for r in logs("group_pickup", out["group_booking"])], [GM])


class RatePlanChangeGuardTests(CompCase):
	"""P2 #5: a plain save / REST set_value on Reservation.rate_plan cannot
	bypass amend_stay and confirm_price."""

	def test_direct_rate_plan_change_is_refused_and_amend_stay_remains_governed(self):
		comp_stay = self.book(DESK, rate_plan=self.comp, supervisor_user=GM, supervisor_pin="4321")["reservation"]
		bar_stay = self.book(DESK, rate_plan=self.bar)["reservation"]
		comp_before = frappe.db.get_value(
			"Reservation", comp_stay, ["rate_plan", "rate_category", "amount_after_tax"], as_dict=True,
		)
		bar_before = frappe.db.get_value(
			"Reservation", bar_stay, ["rate_plan", "rate_category", "amount_after_tax"], as_dict=True,
		)

		# REST set_value COMP -> BAR bypassed amend_stay & confirm_price before R3
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, frappe.client.set_value, "Reservation", comp_stay, "rate_plan", self.bar)
		self.assertEqual(
			frappe.db.get_value("Reservation", comp_stay, ["rate_plan", "rate_category", "amount_after_tax"], as_dict=True),
			comp_before,
		)

		# Plain doc.save() COMP -> BAR refused
		doc = frappe.get_doc("Reservation", comp_stay)
		doc.rate_plan = self.bar
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, doc.save)

		# Clearing rate_plan via set_value refused
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, frappe.client.set_value, "Reservation", bar_stay, "rate_plan", "")
		self.assertEqual(
			frappe.db.get_value("Reservation", bar_stay, ["rate_plan", "rate_category", "amount_after_tax"], as_dict=True),
			bar_before,
		)

		# Governed path: amend_stay enforces confirm_price, then updates plan, category, and total
		with self.assertRaises(api.PriceConfirmationRequired):
			self.as_user(DESK, api.amend_stay, comp_stay, d(5), d(7), confirm_price=0, rate_plan=self.bar)
		self.as_user(DESK, api.amend_stay, comp_stay, d(5), d(7), confirm_price=1, rate_plan=self.bar)
		comp_after = frappe.db.get_value(
			"Reservation", comp_stay, ["rate_plan", "rate_category", "amount_after_tax"], as_dict=True,
		)
		self.assertEqual((comp_after.rate_plan, comp_after.rate_category), (self.bar, "Standard"))
		self.assertGreater(flt(comp_after.amount_after_tax), 0)

		# Ordinary non-plan save on an existing stay still succeeds
		stay_doc = frappe.get_doc("Reservation", comp_stay)
		stay_doc.special_requests = "UT quiet floor"
		self.as_user(DESK, stay_doc.save)


# ---------------------------------------------------------------------------
# Round 4 (Hydrogen's R3 review & live check of #38)
# ---------------------------------------------------------------------------

PROP_R4 = "UT Comp R4"


class ClosedNightVoidAndRepostTests(AuditTestCase):
	"""R4 P1 #1 & P2 #3:
	- Voiding a posted Room night on a later business date leaves the original
	  standing on its closed date (STANDS_ON_ITS_DATE); _charge_posted must use
	  that same standing rule so post_room_night and post_remaining_nights
	  (check-out) never re-post a second charge onto the closed date.
	- On the same open business date, a voided night drops out of standing
	  rows and re-posting uses the rate code in effect at posting time.
	- reports._projected_room_revenue must apply STANDS_ON_ITS_DATE so voided
	  same-date lines never skew the projected category or suppress unposted
	  nights."""

	properties = (PROP_R4,)

	def setUp(self):
		super().setUp()
		self.at(YESTERDAY, "kamra.folio", "kamra.business_date", "kamra.ledger", "kamra.api")
		make_property(PROP_R4, business_date=YESTERDAY)
		frappe.db.delete("Rate Guardrail", {"property": PROP_R4})
		frappe.db.delete("Property Setting", {"property": PROP_R4})
		from kamra.settings import clear_cache
		clear_cache(PROP_R4)
		set_setting(
			PROP_R4,
			"rate_categories",
			"Standard\nComplimentary: sign-off, no ADR\nHouse Use: sign-off, no ADR, no occupancy",
		)
		self.rt = make_room_type(PROP_R4, "STD", base_price=1000)
		frappe.db.set_value("Room Type", self.rt, "base_price", 1000)
		frappe.clear_document_cache("Room Type", self.rt)
		self.rooms = [make_room(PROP_R4, self.rt, f"40{i}") for i in range(1, 5)]
		ensure_user(DESK, ["Front Desk"])
		ensure_user(GM, ["Hotel Admin", "Finance"])
		frappe.db.delete("Cashier PIN", {"name": GM})
		frappe.get_doc({"doctype": "Cashier PIN", "user": GM, "pin": "4321"}
		               ).insert(ignore_permissions=True)
		self.bar = self._plan("BAR", 0, "Standard")
		self.comp = self._plan("COMP", -100, "Complimentary")
		self.house = self._plan("HOUSE", -100, "House Use")

	def _plan(self, code, modifier, category):
		name = f"{PROP_R4}-{code}"
		if frappe.db.exists("Rate Plan", name):
			frappe.delete_doc("Rate Plan", name, force=True, ignore_permissions=True)
		return frappe.get_doc({
			"doctype": "Rate Plan", "property": PROP_R4, "rate_plan_name": code, "code": code,
			"modifier_type": "Percent", "modifier_value": modifier, "rate_category": category,
		}).insert(ignore_permissions=True).name

	def _stay(self, room, plan, ci=YESTERDAY, co=None):
		doc = frappe.get_doc({
			"doctype": "Reservation", "property": PROP_R4, "room_type": self.rt,
			"guest": make_guest(), "status": "Confirmed", "room": room, "rate_plan": plan,
			"check_in_date": ci, "check_out_date": co or d(2), "adults": 2, "children": 0,
		})
		doc.flags.allow_past_check_in = True
		doc.flags.kamra_discount_supervisor = {"user": GM, "pin": "4321"}
		doc.insert(ignore_permissions=True)
		doc.status = "Checked In"
		doc.save(ignore_permissions=True)
		return doc

	def _as_user(self, user, fn, *args, **kwargs):
		frappe.set_user(user)  # nosemgrep: frappe-setuser -- acting as the persona
		try:
			return fn(*args, **kwargs)
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- restore

	def _set_business_date(self, day):
		frappe.db.set_value("Property", PROP_R4, "business_date", day, update_modified=False)
		frappe.clear_document_cache("Property", PROP_R4)
		self.at(day, "kamra.folio", "kamra.business_date", "kamra.ledger", "kamra.api")

	def _active_room_row(self, res_name):
		folio = frappe.get_doc("Folio", guest_folio(res_name))
		from kamra import folio as folio_mod
		voided = folio_mod.voided_rows(folio)
		row = next(
			c for c in folio.charges
			if c.charge_type == "Room" and not c.get("reversal_of") and c.name not in voided
		)
		return folio.name, row.name

	def test_voiding_a_closed_night_on_a_later_date_never_reposts_or_doubles_stats(self):
		from kamra import audit_report, folio as folio_mod, reports, room_statistics
		stay = self._stay(self.rooms[0], self.bar, ci=YESTERDAY, co=d(1))
		self.assertTrue(folio_mod.post_room_night(frappe.get_doc("Reservation", stay.name), YESTERDAY))

		# Roll to TODAY (YESTERDAY is now closed), switch to COMP, void YESTERDAY on TODAY
		self._set_business_date(TODAY)
		self._as_user(
			DESK, api.amend_stay, stay.name, YESTERDAY, d(1), confirm_price=1,
			rate_plan=self.comp, comp_supervisor_user=GM, comp_supervisor_pin="4321",
		)
		folio_name, orig_row = self._active_room_row(stay.name)
		self._as_user(GM, api.void_folio_charge, folio_name, orig_row, reason="UT later-date void", pin="4321")

		# Closed YESTERDAY must never be re-posted by post_room_night or check-out post_remaining_nights
		self.assertFalse(folio_mod.post_room_night(frappe.get_doc("Reservation", stay.name), YESTERDAY))
		remaining = folio_mod.post_remaining_nights(frappe.get_doc("Reservation", stay.name))
		self.assertEqual(remaining, 1)  # TODAY only, never YESTERDAY again

		yest_rooms = frappe.get_all(
			"Folio Charge",
			filters={"reservation": stay.name, "charge_type": "Room", "posting_date": YESTERDAY},
			fields=["amount", "rate_category", "reversal_of"],
		)
		self.assertEqual(len(yest_rooms), 1)
		self.assertEqual((flt(yest_rooms[0].amount), yest_rooms[0].rate_category), (1000.0, "Standard"))

		posted_yest = room_statistics.posted_rooms(PROP_R4, YESTERDAY, YESTERDAY)
		self.assertEqual(posted_yest["rooms_sold"], 1)
		self.assertEqual(flt(posted_yest["room_revenue"]), 1000.0)
		self.assertEqual(set(posted_yest["by_category"]), {"Standard"})

		flash_yest = reports._day_stats(PROP_R4, YESTERDAY, 4)
		self.assertEqual((flash_yest["rooms_sold"], flash_yest["occupancy_pct"], flash_yest["adr"]), (1, 25.0, 1000))
		self.assertEqual(flash_yest["rooms_by_category"], {"Complimentary": 0, "House Use": 0})

		audit_yest = audit_report.build(PROP_R4, YESTERDAY)["periods"][0]
		self.assertEqual((audit_yest["rooms_sold"], audit_yest["room_revenue"], audit_yest["adr"]), (1, 1000.0, 1000))

	def test_same_date_void_and_repost_uses_active_category_and_standing_projected_revenue(self):
		from kamra import folio as folio_mod, reports, room_statistics
		self._set_business_date(TODAY)
		# Stay 1 starts on BAR, posts TODAY (Standard 1000), voids same day, switches to HOUSE, re-posts (House Use 0)
		stay_house = self._stay(self.rooms[0], self.bar, ci=TODAY, co=d(2))
		# Stay 2 stays unposted on BAR (Standard 1000) so projected ADR isolates Stay 1's category
		self._stay(self.rooms[1], self.bar, ci=TODAY, co=d(2))

		self.assertTrue(folio_mod.post_room_night(frappe.get_doc("Reservation", stay_house.name), TODAY))
		folio_name, orig_row = self._active_room_row(stay_house.name)
		self._as_user(GM, api.void_folio_charge, folio_name, orig_row, reason="UT same-day switch", pin="4321")
		self._as_user(
			DESK, api.amend_stay, stay_house.name, TODAY, d(2), confirm_price=1,
			rate_plan=self.house, comp_supervisor_user=GM, comp_supervisor_pin="4321",
		)
		# Open business date: re-posting succeeds and uses the rate code in effect at posting time (House Use)
		self.assertTrue(folio_mod.post_room_night(frappe.get_doc("Reservation", stay_house.name), TODAY))

		posted = room_statistics.posted_rooms(PROP_R4, TODAY, TODAY)
		self.assertEqual(posted["rooms_sold"], 1)
		self.assertEqual(flt(posted["room_revenue"]), 0.0)
		self.assertEqual(posted["by_category"]["House Use"]["rooms"], 1)
		self.assertEqual(posted["by_category"].get("Standard", {}).get("rooms", 0), 0)

		# Projected revenue must read only the standing House Use line (not MAX('Standard','House Use')='Standard')
		proj = reports._projected_room_revenue(PROP_R4, TODAY, 4)
		self.assertEqual(proj["projected_room_revenue"], 1000.0)
		self.assertEqual(proj["projected_adr"], 1000)
		self.assertEqual(proj["projected_revpar"], 250)

	def test_projected_revenue_ignores_same_date_voided_room_before_repost(self):
		from kamra import folio as folio_mod, reports
		self._set_business_date(TODAY)
		stay = self._stay(self.rooms[0], self.bar, ci=TODAY, co=d(2))
		self.assertTrue(folio_mod.post_room_night(frappe.get_doc("Reservation", stay.name), TODAY))
		folio_name, orig_row = self._active_room_row(stay.name)
		self._as_user(GM, api.void_folio_charge, folio_name, orig_row, reason="UT void before audit", pin="4321")

		# No standing Room line yet: night audit will still post TODAY at its locked 1000 rate
		proj = reports._projected_room_revenue(PROP_R4, TODAY, 4)
		self.assertEqual(proj["projected_room_revenue"], 1000.0)
		self.assertEqual(proj["projected_adr"], 1000)
		self.assertEqual(proj["projected_revpar"], 250)


class GroupPreviewPricingParityTests(CompCase):
	"""R4 P2 #2: group_price_preview, group_quotation and group_detail use the
	same negotiated-rate pricing path as stay creation."""

	def setUp(self):
		super().setUp()
		self.corp20 = self.plan("CORP20", "Percent", -20, rate_category="Standard")
		self.co_comp = "UT Comp Corp R4"
		self.co_disc = "UT Disc Corp R4"
		for cname, plan in ((self.co_comp, self.comp), (self.co_disc, self.corp20)):
			if frappe.db.exists("Corporate Account", cname):
				frappe.delete_doc("Corporate Account", cname, force=True, ignore_permissions=True)
			frappe.get_doc({
				"doctype": "Corporate Account", "company_name": cname,
				"negotiated_rate_plan": plan, "credit_allowed": 1,
			}).insert(ignore_permissions=True)

	def test_group_price_preview_quotation_and_detail_apply_company_negotiated_rate_plan(self):
		blocks = [{"room_type": self.rt, "rooms_blocked": 2}]
		# 1. Negotiated Complimentary (-100%) company: preview, quotation, detail and stays all agree at 0
		pv_comp = self.as_user(DESK, api.group_price_preview, PROP_A, d(5), d(7), blocks, company=self.co_comp)
		self.assertEqual(pv_comp["rows"][0]["rate"], 0.0)
		self.assertEqual((pv_comp["before_tax"], pv_comp["tax"], pv_comp["total"]), (0.0, 0.0, 0.0))

		grp = self.as_user(
			DESK, api.create_group_with_rooming_list, PROP_A, "UT Comp Pricing Parity",
			d(5), d(7), blocks,
			guests=[{"room_type": self.rt, "guest_name": "Guest 1"}, {"room_type": self.rt, "guest_name": "Guest 2"}],
			company=self.co_comp, comp_supervisor_user=GM, comp_supervisor_pin="4321",
		)
		for res_name in grp["created"]:
			self.assertEqual(flt(frappe.db.get_value("Reservation", res_name, "amount_after_tax")), 0.0)
		detail = self.as_user(DESK, api.group_detail, grp["group_booking"])
		self.assertEqual(detail["money"]["block_value"], 0.0)
		quot = self.as_user(DESK, api.group_quotation, grp["group_booking"])
		self.assertEqual(quot["prices"]["total"], 0.0)

		# 2. Negotiated Standard (-20%) company: preview rate matches created stay nightly rate (800)
		pv_disc = self.as_user(DESK, api.group_price_preview, PROP_A, d(8), d(10), blocks, company=self.co_disc)
		self.assertEqual(pv_disc["rows"][0]["rate"], 800.0)
		self.assertEqual(pv_disc["before_tax"], 3200.0)

		# 3. Explicit block_rate overrides the company's negotiated plan, matching create_booking
		pv_override = self.as_user(
			DESK, api.group_price_preview, PROP_A, d(5), d(7),
			[{"room_type": self.rt, "rooms_blocked": 2, "block_rate": 650}],
			company=self.co_comp,
		)
		self.assertEqual(pv_override["rows"][0]["rate"], 650.0)
		self.assertEqual(pv_override["before_tax"], 2600.0)


class BackfillRateCategoryChainTests(AuditTestCase):
	"""R4 P2 #4: backfill follows same-date Room -> Version reconstruction ->
	Reservation.rate_category -> Rate Plan.rate_category -> property default,
	and logs ambiguous rows instead of guessing silently."""

	properties = (PROP_R4,)

	def setUp(self):
		super().setUp()
		self.at(YESTERDAY, "kamra.folio", "kamra.business_date", "kamra.ledger", "kamra.api")
		make_property(PROP_R4, business_date=YESTERDAY)
		frappe.db.delete("Rate Guardrail", {"property": PROP_R4})
		frappe.db.delete("Property Setting", {"property": PROP_R4})
		from kamra.settings import clear_cache
		clear_cache(PROP_R4)
		set_setting(
			PROP_R4,
			"rate_categories",
			"Standard\nComplimentary: sign-off, no ADR\nHouse Use: sign-off, no ADR, no occupancy",
		)
		self.rt = make_room_type(PROP_R4, "STD", base_price=1000)
		frappe.db.set_value("Room Type", self.rt, "base_price", 1000)
		frappe.clear_document_cache("Room Type", self.rt)
		self.rooms = [make_room(PROP_R4, self.rt, f"45{i}") for i in range(1, 6)]
		ensure_user(DESK, ["Front Desk"])
		ensure_user(GM, ["Hotel Admin", "Finance"])
		frappe.db.delete("Cashier PIN", {"name": GM})
		frappe.get_doc({"doctype": "Cashier PIN", "user": GM, "pin": "4321"}
		               ).insert(ignore_permissions=True)
		self.bar = self._plan("BAR", 0, "Standard")
		self.comp = self._plan("COMP", -100, "Complimentary")

	def _plan(self, code, modifier, category):
		name = f"{PROP_R4}-{code}"
		if frappe.db.exists("Rate Plan", name):
			frappe.delete_doc("Rate Plan", name, force=True, ignore_permissions=True)
		return frappe.get_doc({
			"doctype": "Rate Plan", "property": PROP_R4, "rate_plan_name": code, "code": code,
			"modifier_type": "Percent", "modifier_value": modifier, "rate_category": category,
		}).insert(ignore_permissions=True).name

	def _stay(self, room, plan=None, ci=YESTERDAY, co=None):
		doc = frappe.get_doc({
			"doctype": "Reservation", "property": PROP_R4, "room_type": self.rt,
			"guest": make_guest(), "status": "Confirmed", "room": room, "rate_plan": plan,
			"check_in_date": ci, "check_out_date": co or d(2), "adults": 2, "children": 0,
		})
		doc.flags.allow_past_check_in = True
		doc.flags.kamra_discount_supervisor = {"user": GM, "pin": "4321"}
		doc.insert(ignore_permissions=True)
		doc.status = "Checked In"
		doc.save(ignore_permissions=True)
		return doc

	def _as_user(self, user, fn, *args, **kwargs):
		frappe.set_user(user)  # nosemgrep: frappe-setuser -- acting as the persona
		try:
			return fn(*args, **kwargs)
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- restore

	def test_backfill_follows_same_date_room_then_reservation_then_rate_plan_then_default(self):
		from kamra import folio as folio_mod
		from kamra.patches.v47 import backfill_folio_charge_rate_category

		# 1. Pre-#38 comp stay: blank Reservation.rate_category, Rate Plan.rate_category = Complimentary
		legacy_comp = self._stay(self.rooms[0], self.comp)
		folio_mod.post_room_night(frappe.get_doc("Reservation", legacy_comp.name), YESTERDAY)
		fc_legacy = frappe.db.get_value(
			"Folio Charge", {"reservation": legacy_comp.name, "charge_type": "Room"}, "name",
		)
		frappe.db.set_value("Reservation", legacy_comp.name, "rate_category", "", update_modified=False)
		frappe.db.set_value("Folio Charge", fc_legacy, "rate_category", "", update_modified=False)

		# 2. Same-date Room row beats current Reservation.rate_category for a blank Rate Adjustment
		adj_stay = self._stay(self.rooms[1], self.bar)
		folio_mod.post_room_night(frappe.get_doc("Reservation", adj_stay.name), YESTERDAY)
		frappe.db.set_value("Property", PROP_R4, "business_date", TODAY, update_modified=False)
		frappe.clear_document_cache("Property", PROP_R4)
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger", "kamra.api")
		f_adj = frappe.get_doc("Folio", guest_folio(adj_stay.name))
		f_adj.append("charges", {
			"posting_date": TODAY, "stay_date": YESTERDAY, "charge_type": "Rate Adjustment",
			"reservation": adj_stay.name, "description": "UT adj credit",
			"qty": 1, "rate": -500, "amount": -500, "gst_rate": 0,
		})
		f_adj.save(ignore_permissions=True)
		fc_adj = f_adj.charges[-1].name
		# Reservation switched to Complimentary after YESTERDAY's Standard Room row was stamped
		frappe.db.set_value("Reservation", adj_stay.name, "rate_category", "Complimentary", update_modified=False)
		frappe.db.set_value("Folio Charge", fc_adj, "rate_category", "", update_modified=False)

		# 3. No Rate Plan and blank Reservation.rate_category -> falls back to property default
		no_plan = self._stay(self.rooms[2], None)
		folio_mod.post_room_night(frappe.get_doc("Reservation", no_plan.name), TODAY)
		fc_no_plan = frappe.db.get_value(
			"Folio Charge", {"reservation": no_plan.name, "charge_type": "Room"}, "name",
		)
		frappe.db.set_value("Reservation", no_plan.name, "rate_category", "", update_modified=False)
		frappe.db.set_value("Folio Charge", fc_no_plan, "rate_category", "", update_modified=False)

		backfill_folio_charge_rate_category.execute()

		self.assertEqual(frappe.db.get_value("Folio Charge", fc_legacy, "rate_category"), "Complimentary")
		self.assertEqual(frappe.db.get_value("Folio Charge", fc_adj, "rate_category"), "Standard")
		self.assertEqual(frappe.db.get_value("Folio Charge", fc_no_plan, "rate_category"), "Standard")

	def test_backfill_logs_the_rows_of_a_stay_whose_rate_plan_changed_instead_of_guessing(self):
		from unittest.mock import patch
		from kamra import folio as folio_mod
		from kamra.patches.v47 import backfill_folio_charge_rate_category

		# A stay moved BAR -> COMP after YESTERDAY was posted. Nothing says which plan a blank line was
		# posted on (a child row carries its folio's creation, not its own), so the stay's rows take its
		# present category and are logged for review: never guessed silently.
		switched = self._stay(self.rooms[3], self.bar, ci=YESTERDAY, co=d(2))
		folio_mod.post_room_night(frappe.get_doc("Reservation", switched.name), YESTERDAY)
		frappe.db.set_value("Property", PROP_R4, "business_date", TODAY, update_modified=False)
		frappe.clear_document_cache("Property", PROP_R4)
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger", "kamra.api")
		self._as_user(
			DESK, api.amend_stay, switched.name, YESTERDAY, d(2), confirm_price=1,
			rate_plan=self.comp, comp_supervisor_user=GM, comp_supervisor_pin="4321",
		)
		folio_mod.post_room_night(frappe.get_doc("Reservation", switched.name), TODAY)
		switched_rows = frappe.get_all(
			"Folio Charge", filters={"reservation": switched.name, "charge_type": "Room"}, pluck="name")
		self.assertEqual(len(switched_rows), 2)

		# a stay that never changed plan is not in the log
		steady = self._stay(self.rooms[4], self.bar, ci=YESTERDAY, co=d(2))
		folio_mod.post_room_night(frappe.get_doc("Reservation", steady.name), YESTERDAY)
		steady_row = frappe.db.get_value(
			"Folio Charge", {"reservation": steady.name, "charge_type": "Room"}, "name")

		for name in (*switched_rows, steady_row):
			frappe.db.set_value("Folio Charge", name, "rate_category", "", update_modified=False)

		with patch.object(frappe, "log_error") as mock_log:
			backfill_folio_charge_rate_category.execute()

		for name in switched_rows:
			self.assertEqual(frappe.db.get_value("Folio Charge", name, "rate_category"), "Complimentary")
		self.assertEqual(frappe.db.get_value("Folio Charge", steady_row, "rate_category"), "Standard")
		self.assertEqual(mock_log.call_count, 1, "ambiguous rows are logged once, with their count and names")
		logged = " ".join(str(a) for a in (*mock_log.call_args.args, *mock_log.call_args.kwargs.values()))
		self.assertIn("2 charge", logged)
		for name in switched_rows:
			self.assertIn(name, logged)
		self.assertNotIn(steady_row, logged)

	def test_backfill_gives_a_reversal_the_category_of_the_charge_it_reverses(self):
		from kamra import folio as folio_mod
		from kamra.patches.v47 import backfill_folio_charge_rate_category

		# A night posted on BAR (Standard), the stay moved to COMP, then the night voided: the
		# reversal is blank but reverses a Standard line, so it is Standard, not the stay's present category.
		stay = self._stay(self.rooms[0], self.bar, ci=YESTERDAY, co=d(2))
		folio_mod.post_room_night(frappe.get_doc("Reservation", stay.name), YESTERDAY)
		frappe.db.set_value("Property", PROP_R4, "business_date", TODAY, update_modified=False)
		frappe.clear_document_cache("Property", PROP_R4)
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger", "kamra.api")
		self._as_user(
			DESK, api.amend_stay, stay.name, YESTERDAY, d(2), confirm_price=1,
			rate_plan=self.comp, comp_supervisor_user=GM, comp_supervisor_pin="4321",
		)
		folio = frappe.get_doc("Folio", guest_folio(stay.name))
		original = next(c.name for c in folio.charges if c.charge_type == "Room" and not c.get("reversal_of"))
		self._as_user(GM, api.void_folio_charge, folio.name, original, reason="UT void after switch", pin="4321")
		reversal = frappe.db.get_value("Folio Charge", {"reversal_of": original}, "name")
		self.assertTrue(reversal)
		self.assertEqual(frappe.db.get_value("Folio Charge", original, "rate_category"), "Standard")
		frappe.db.set_value("Folio Charge", reversal, "rate_category", "", update_modified=False)

		backfill_folio_charge_rate_category.execute()

		self.assertEqual(frappe.db.get_value("Folio Charge", original, "rate_category"), "Standard")
		self.assertEqual(frappe.db.get_value("Folio Charge", reversal, "rate_category"), "Standard")


class ReservationDetailRatePlanAmendTests(CompCase):
	"""R4 UX #5: reservation_detail exposes the stay's rate plan metadata and
	the property's active rate plans so Amend stay can change the rate plan
	from the current business date for remaining nights with comp sign-off."""

	def test_reservation_detail_exposes_rate_plans_and_amend_stay_changes_remaining_nights_with_signoff(self):
		self.at(YESTERDAY, "kamra.folio", "kamra.business_date", "kamra.ledger", "kamra.api")
		make_property(PROP_A, business_date=YESTERDAY)
		doc = frappe.get_doc({
			"doctype": "Reservation", "property": PROP_A, "room_type": self.rt,
			"guest": make_guest(), "status": "Confirmed", "room": self.rooms[0], "rate_plan": self.bar,
			"check_in_date": YESTERDAY, "check_out_date": d(2), "adults": 2, "children": 0,
		})
		doc.flags.allow_past_check_in = True
		doc.insert(ignore_permissions=True)
		doc.status = "Checked In"
		doc.save(ignore_permissions=True)

		from kamra import folio as folio_mod
		folio_mod.post_room_night(frappe.get_doc("Reservation", doc.name), YESTERDAY)
		frappe.db.set_value("Property", PROP_A, "business_date", TODAY, update_modified=False)
		frappe.clear_document_cache("Property", PROP_A)
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger", "kamra.api")

		detail = self.as_user(DESK, api.reservation_detail, doc.name)
		self.assertEqual(detail["rate_plan"], self.bar)
		self.assertEqual(detail["rate_category"], "Standard")
		self.assertTrue(detail["rate_plan_name"])
		plan_names = {p["name"] for p in detail["rate_plans"]}
		self.assertIn(self.bar, plan_names)
		self.assertIn(self.comp, plan_names)

		# Previewing a move to COMP reports comp_governance and holds charged YESTERDAY
		pv = self.as_user(DESK, api.amend_stay_preview, doc.name, YESTERDAY, d(2), rate_plan=self.comp)
		self.assertTrue(pv["comp_governance"]["needs_supervisor"])
		self.assertEqual(pv["comp_governance"]["rate_category"], "Complimentary")
		self.assertTrue(any(n["date"] == YESTERDAY and n["action"] == "kept" for n in pv["charged_nights"]["nights"]))

		# Unsigned amend_stay to COMP is refused; signed amend_stay applies COMP from TODAY forward
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, api.amend_stay, doc.name, YESTERDAY, d(2), confirm_price=1, rate_plan=self.comp)
		self.as_user(
			DESK, api.amend_stay, doc.name, YESTERDAY, d(2), confirm_price=1,
			rate_plan=self.comp, comp_supervisor_user=GM, comp_supervisor_pin="4321",
		)
		updated = frappe.get_doc("Reservation", doc.name)
		rates_by_date = {str(r.stay_date): flt(r.rate) for r in updated.night_rates}
		self.assertGreater(rates_by_date[YESTERDAY], 0)  # closed night kept its billed rate
		self.assertEqual(rates_by_date[TODAY], 0.0)      # open business date onward takes COMP
		self.assertEqual(rates_by_date[d(1)], 0.0)

