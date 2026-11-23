"""Reservation discounts, round 6 (Gap Record #8555; Paul's standing
direction for PMS/POS work: nothing a client configures in SAP or OPERA is
hard-coded). What this PR had put in code now comes from the property's own
configuration.

- R6-1 the reason-code categories a discount and an agreed rate take are
  property settings (discount_reason_categories,
  agreed_rate_reason_categories; OPERA's discount and rate-change reason
  lists, SAP's order reasons per purpose); the codes themselves stay the
  Reason Code records.
- R6-2 an AI agent takes its own role's line in Limits by role; with none,
  the lowest limit the property sets (never more than any person at the desk).
- R6-3 who may give a discount is who may call the discount action (its
  declared roles), not a second role list.
- R6-4 reservation_detail tells the screen the categories and whether this
  person may fix a rate, so the screen holds no role or category list."""

from __future__ import annotations

from unittest.mock import patch

import frappe

from kamra import api
from kamra.authz import ADMIN
from kamra.install import AGENT_ROLE
from kamra.tests.audit_fixtures import PROP_A, ensure_user
from kamra.tests.test_reservation_discounts import DESK, REVENUE, set_setting
from kamra.tests.test_reservation_discounts_r4 import RoundFourTestCase

CLERK = "ut.disc.clerk@test.local"
CLERK_ROLE = "UT Rate Clerk"
HOUSEKEEPER = "ut.disc.housekeeper@test.local"


class ReasonCategoryTests(RoundFourTestCase):
	"""R6-1: the categories a discount and an agreed rate take are settings."""

	def test_a_discount_takes_the_reason_categories_the_property_sets(self):
		set_setting(PROP_A, "discount_reason_categories", "Adjust\nNC")
		names = {r["name"] for r in self.as_user(DESK, api.booking_options, PROP_A)["discount_reasons"]}
		self.assertTrue({"ADJ-RATE", "NC-MGMT"} <= names, names)
		self.assertFalse({"RATE-PROMO", "RATE-MGR"} & names, names)
		res = self.stay()
		with self.assertRaises(frappe.ValidationError):
			self.discount(res.name, discount_type="Percent", discount_value=10, reason_code="RATE-PROMO")
		self.discount(res.name, discount_type="Percent", discount_value=10, reason_code="ADJ-RATE")
		self.assertEqual(self.rows(res.name), [(1000, 100, 900)] * 2)

	def test_an_agreed_rate_takes_the_reason_categories_the_property_sets(self):
		set_setting(PROP_A, "agreed_rate_reason_categories", "Adjust")
		res = self.stay()
		with self.assertRaises(frappe.ValidationError):
			self.as_user(REVENUE, api.set_agreed_rate, res.name, 900, "RATE-MGR", confirm_price=1)
		self.as_user(REVENUE, api.set_agreed_rate, res.name, 900, "ADJ-RATE", confirm_price=1)
		self.assertEqual(self.rows(res.name), [(900, 0, 900)] * 2)

	def test_reservation_detail_sends_the_categories_the_screen_offers(self):
		res = self.stay()
		detail = self.as_user(DESK, api.reservation_detail, res.name)
		self.assertEqual(detail.get("reason_categories"),
		                 {"discount": ["Rate Change", "Adjust"], "agreed_rate": ["Rate Change"]})
		set_setting(PROP_A, "discount_reason_categories", "Adjust")
		set_setting(PROP_A, "agreed_rate_reason_categories", "Rate Change\nAdjust")
		detail = self.as_user(DESK, api.reservation_detail, res.name)
		self.assertEqual(detail.get("reason_categories"),
		                 {"discount": ["Adjust"], "agreed_rate": ["Rate Change", "Adjust"]})


class AgentLimitTests(RoundFourTestCase):
	"""R6-2: an AI agent's limit is its own role's line, else the lowest."""

	def test_an_ai_agent_takes_its_own_roles_line(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		set_setting(PROP_A, "rate_override_limits_by_role", f"Front Desk: 5\n{AGENT_ROLE}: 3")
		self.assertEqual(self.as_agent(DESK, api.desk_limit_pct, PROP_A), 3)
		self.assertEqual(self.as_user(DESK, api.desk_limit_pct, PROP_A), 5)

	def test_an_ai_agent_without_a_line_takes_the_lowest_limit(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		for limits, lowest in (("Front Desk: 5\nUT Duty Manager: 15", 5),
		                       ("UT Duty Manager: 15", 10)):
			with self.subTest(limits=limits):
				set_setting(PROP_A, "rate_override_limits_by_role", limits)
				self.assertEqual(self.as_agent(DESK, api.desk_limit_pct, PROP_A), lowest)


class DiscountRightTests(RoundFourTestCase):
	"""R6-3: who may give a discount is who may call the discount action."""

	def test_the_discount_actions_own_roles_say_who_may_discount(self):
		ensure_user(CLERK, [CLERK_ROLE])
		ensure_user(HOUSEKEEPER, ["Housekeeping"])
		roles = frozenset({CLERK_ROLE, *ADMIN})
		with patch.object(api.set_reservation_discount, "_kamra_roles", roles):
			self.assertTrue(self.as_user(CLERK, api.discount_options, PROP_A)["can_discount"])
			self.assertFalse(self.as_user(DESK, api.discount_options, PROP_A)["can_discount"])
		self.assertTrue(self.as_user(DESK, api.discount_options, PROP_A)["can_discount"])
		self.assertFalse(self.as_user(HOUSEKEEPER, api.discount_options, PROP_A)["can_discount"])


class FixRateRightTests(RoundFourTestCase):
	"""R6-4: the server says who may fix a rate (kamra.api._can_set_rate)."""

	def test_reservation_detail_says_whether_this_person_may_fix_a_rate(self):
		res = self.stay()
		for user, may in ((DESK, False), (REVENUE, True)):
			with self.subTest(user=user):
				actions = self.as_user(user, api.reservation_detail, res.name)["actions"]
				self.assertIs(actions.get("can_set_rate"), may)
