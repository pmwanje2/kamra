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
from kamra.tests.audit_fixtures import d, ensure_user, make_reservation
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
