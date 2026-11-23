"""Complimentary and House Use rate plans (OPERA Cloud rate code flags;
the parity check's item 11b on #32).

- A Rate Plan carries a rate_category from the property's own list
  (Settings > Revenue > Complimentary and house use: rate_categories),
  seeded with OPERA's rate code Complimentary and House Use flags:
      Standard
      Complimentary: sign-off, no ADR
      House Use: sign-off, no ADR
  Each line can also say "no RevPAR" or "no occupancy". The approvers are
  configured too (rate_category_approvers, seeded Hotel Admin and Finance).
- kamra.room_statistics is the one rule for rooms sold, rooms by category,
  ADR, RevPAR and occupancy, and every report reads it. A category left out
  of a figure adds neither rooms nor revenue to it. With OPERA's defaults:
  - ADR is room revenue over the rooms sold minus comp and house use (OPERA
    Manager Report, "ADR minus Comp and House");
  - RevPAR is room revenue over the rooms;
  - occupancy counts comp and house-use rooms, because they are occupied.
  So the same nights give the same figures in every report.
- Booking a stay on a plan whose category needs a sign-off, or moving a
  stay to one, needs another supervisor's sign-off with their PIN. This is
  OPERA's comp authorizer, through the PIN route the discounts use. Nobody
  signs off their own, and an AI agent can't.
- A comp stay still taxes the lines taxed like the room (cleaning, the levy).
- A plain save (REST PUT) never writes an auto-priced stay's header totals.
"""

from __future__ import annotations

from typing import ClassVar

import frappe
from frappe.utils import flt

from kamra import api, audit_report, dashboards, pricing, reports
from kamra import folio as folio_mod
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
	AGENT,
	DESK,
	GM,
	PROP_A,
	REVENUE,
	SELF,
	DiscountTestCase,
	set_setting,
)

PROP_S = "UT Comp Stats"
PROP_V = "UT Comp VAT"
FIGURES = ("rooms_sold", "rooms_by_category", "adr", "revpar", "occupancy_pct")
OPERA = {"Complimentary": 1, "House Use": 1}
# a property that counts comps in ADR, leaves house use out of ADR and
# occupancy, and has a Staff category out of ADR and RevPAR
CONFIGURED = ("Standard\nComplimentary: sign-off\nHouse Use: sign-off, no ADR, no occupancy\n"
              "Staff: no ADR, no RevPAR")


def sign_off_logs(name):
	return frappe.get_all("Agent Action Log",
	                      filters={"action_type": "comp_rate_sign_off", "reference_name": name},
	                      fields=["actor", "approver", "rationale"])


class RateCategoryFieldTests(DiscountTestCase):
	"""Rate Plan.rate_category holds one of the property's categories."""

	def test_the_field_takes_its_values_from_the_propertys_list(self):
		field = frappe.get_meta("Rate Plan").get_field("rate_category")
		self.assertIsNotNone(field)
		# not a fixed Select: the property's own list (Settings) decides
		self.assertEqual(field.fieldtype, "Data")
		# and no category name of its own (R2: the list's first is the default)
		self.assertFalse(field.default)

	def test_a_new_plan_takes_the_first_category_the_property_lists(self):
		# a property that calls its first category Rack, not Standard
		set_setting(PROP_A, "rate_categories", "Rack\nComplimentary: sign-off, no ADR")
		rack = self.plan("RACK")
		self.assertEqual(frappe.db.get_value("Rate Plan", rack, "rate_category"), "Rack")

	def test_a_plan_takes_only_a_category_the_property_lists(self):
		with self.assertRaises(frappe.ValidationError):
			self.plan("STAFF", rate_category="Staff")
		set_setting(PROP_A, "rate_categories", CONFIGURED)
		# matched without regard to case, stored as the list spells it
		staff = self.plan("STAFF", rate_category="staff")
		self.assertEqual(frappe.db.get_value("Rate Plan", staff, "rate_category"), "Staff")
		# a plan with none takes the list's first category
		bar = self.plan("BAR", rate_category="")
		self.assertEqual(frappe.db.get_value("Rate Plan", bar, "rate_category"), "Standard")

	def test_the_rate_plans_form_lists_the_propertys_categories(self):
		from kamra.settings import menu_masters
		set_setting(PROP_A, "rate_categories", CONFIGURED)
		out = self.as_user(REVENUE, menu_masters, PROP_A)
		self.assertEqual(out["rate_categories"], ["Standard", "Complimentary", "House Use", "Staff"])


class CategoryRulesTests(AuditTestCase):
	"""kamra.room_statistics.category_rules: the property's categories and
	what each is left out of, seeded with OPERA's defaults."""

	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		make_property(PROP_A)
		frappe.db.delete("Property Setting", {"property": PROP_A, "setting_key": (
			"in", ["rate_categories", "rate_category_approvers"])})
		from kamra.settings import clear_cache
		clear_cache(PROP_A)

	def test_the_seeded_rules_are_operas(self):
		from kamra.room_statistics import category_rules
		rules = category_rules(PROP_A)
		self.assertEqual(rules.categories, ("Standard", "Complimentary", "House Use"))
		self.assertEqual(rules.default, "Standard")
		self.assertEqual(rules.sign_off, frozenset({"Complimentary", "House Use"}))
		self.assertEqual(rules.out_of["adr"], frozenset({"Complimentary", "House Use"}))
		self.assertEqual((rules.out_of["revpar"], rules.out_of["occupancy"]),
		                 (frozenset(), frozenset()))
		self.assertEqual(rules.approvers, ("Hotel Admin", "Finance"))

	def test_each_line_names_a_category_and_what_it_is_left_out_of(self):
		from kamra.room_statistics import parse_rules
		rules = parse_rules(CONFIGURED + "\n staff : NO occupancy\n\nVIP: no ADR, upgrade")
		# a category listed twice keeps its first line; an unknown word is ignored
		self.assertEqual(rules.categories, ("Standard", "Complimentary", "House Use", "Staff", "VIP"))
		self.assertEqual(rules.sign_off, frozenset({"Complimentary", "House Use"}))
		self.assertEqual(rules.out_of["adr"], frozenset({"House Use", "Staff", "VIP"}))
		self.assertEqual(rules.out_of["revpar"], frozenset({"Staff"}))
		self.assertEqual(rules.out_of["occupancy"], frozenset({"House Use"}))
		self.assertEqual(rules.canonical("house use"), "House Use")
		self.assertEqual(rules.canonical(""), "Standard")
		self.assertIsNone(rules.canonical("Unknown"))

	def test_the_approvers_are_configured(self):
		from kamra.room_statistics import category_rules
		set_setting(PROP_A, "rate_category_approvers", "Revenue Manager")
		self.assertEqual(category_rules(PROP_A).approvers, ("Revenue Manager",))


class RoomFiguresTests(AuditTestCase):
	"""kamra.room_statistics.room_figures: the arithmetic every report uses."""

	properties = ()

	@staticmethod
	def nights(**by_category):
		return {cat.replace("_", " "): {"rooms": rooms, "revenue": revenue}
		        for cat, (rooms, revenue) in by_category.items()}

	def test_adr_leaves_out_comp_and_house_use_rooms_occupancy_counts_them(self):
		from kamra.room_statistics import parse_rules, room_figures
		out = room_figures(parse_rules(""), room_revenue=2600, rooms_sold=4, occupancy_rooms=4,
		                   by_category=self.nights(Standard=(2, 2600), Complimentary=(1, 0),
		                                           House_Use=(1, 0)))
		self.assertEqual(out, {"rooms_by_category": OPERA, "occupancy_pct": 100.0,
		                       "adr": 1300, "revpar": 650})

	def test_a_house_of_comps_has_no_adr(self):
		from kamra.room_statistics import parse_rules, room_figures
		out = room_figures(parse_rules(""), room_revenue=0, rooms_sold=2, occupancy_rooms=4,
		                   by_category=self.nights(Complimentary=(1, 0), House_Use=(1, 0)))
		self.assertEqual((out["adr"], out["revpar"], out["occupancy_pct"]), (0, 0, 50.0))

	def test_the_occupied_rooms_and_revpar_rooms_can_differ_from_the_rooms_sold(self):
		from kamra.room_statistics import parse_rules, room_figures
		# in house tonight (occupied) over rooms not out of order; RevPAR
		# over every room, as reports._day_stats reports them
		out = room_figures(parse_rules(""), room_revenue=900, rooms_sold=1,
		                   by_category=self.nights(Standard=(1, 900)), rooms_occupied=3,
		                   occupancy_rooms=3, revpar_rooms=4)
		self.assertEqual((out["adr"], out["revpar"], out["occupancy_pct"]), (900, 225, 100.0))

	def test_a_category_left_out_of_a_figure_adds_neither_rooms_nor_revenue(self):
		from kamra.room_statistics import parse_rules, room_figures
		out = room_figures(parse_rules(CONFIGURED), room_revenue=2600, rooms_sold=4,
		                   occupancy_rooms=4,
		                   by_category=self.nights(Standard=(1, 1100), Staff=(1, 1500),
		                                           Complimentary=(1, 0), House_Use=(1, 0)))
		# ADR: the standard room and the comp (1,100 / 2); RevPAR: all but
		# Staff (1,100 / 4); occupancy: all but house use (3 / 4)
		self.assertEqual(out, {"rooms_by_category": {"Complimentary": 1, "House Use": 1, "Staff": 1},
		                       "adr": 550, "revpar": 275, "occupancy_pct": 75.0})


class CrossReportConsistencyTests(AuditTestCase):
	"""One closed night of four rooms: 1,000 and 1,500 at the rack plans
	(plus a 100 rate correction on the first), a comp and a house-use room
	at no charge. Every report gives the same rooms sold, comp and house-use
	rooms, ADR (2,600 over 2 = 1,300), RevPAR (2,600 over 4 = 650) and
	occupancy (100%)."""

	properties = (PROP_S,)
	EXPECTED: ClassVar[dict] = {"rooms_sold": 4, "rooms_by_category": OPERA,
	                            "adr": 1300, "revpar": 650, "occupancy_pct": 100.0}
	PROJECTED: ClassVar[tuple] = (2500, 1250, 625)
	TONIGHT_OCCUPANCY: ClassVar[float] = 100.0
	# tonight (the open business date): the rooms counted as occupied, and
	# the comp and house-use rooms in house (rooms_by_category)
	TONIGHT_ROOMS: ClassVar[int] = 4
	TONIGHT_BY_CATEGORY: ClassVar[dict] = OPERA
	# the property's rate_categories (None: OPERA's seeded list) and each
	# plan's (code, modifier %, category), one stay on each
	CATEGORIES: ClassVar[str | None] = None
	PLANS: ClassVar[tuple] = (("BAR", 0, "Standard"), ("UP", 50, "Standard"),
	                          ("COMP", -100, "Complimentary"), ("HOUSE", -100, "House Use"))

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_S, business_date=TODAY)
		frappe.db.delete("Rate Guardrail", {"property": PROP_S})
		frappe.db.delete("Property Setting", {"property": PROP_S})
		from kamra.settings import clear_cache
		clear_cache(PROP_S)
		if self.CATEGORIES:
			set_setting(PROP_S, "rate_categories", self.CATEGORIES)
		self.rt = make_room_type(PROP_S, "STD", base_price=1000)
		frappe.db.set_value("Room Type", self.rt, "base_price", 1000)
		frappe.clear_document_cache("Room Type", self.rt)
		self.rooms = [make_room(PROP_S, self.rt, f"20{i}") for i in range(1, 5)]
		ensure_user(GM, ["Hotel Admin"])
		frappe.db.delete("Cashier PIN", {"name": GM})
		frappe.get_doc({"doctype": "Cashier PIN", "user": GM, "pin": "4321"}
		               ).insert(ignore_permissions=True)
		self.stays = []
		for room, (code, modifier, category) in zip(self.rooms, self.PLANS, strict=True):
			self.stays.append(self.stay(room, self.plan(code, modifier, category)))
		for res in self.stays:
			folio_mod.post_room_night(frappe.get_doc("Reservation", res.name), YESTERDAY)
		# a later correction of the first night's rate is room revenue
		# (kamra.ledger books Rate Adjustment to the Rooms group, 1010)
		self.post(self.stays[0], "Rate Adjustment", 100, YESTERDAY)

	def plan(self, code, modifier, category):
		name = f"{PROP_S}-{code}"
		if frappe.db.exists("Rate Plan", name):
			frappe.delete_doc("Rate Plan", name, force=True, ignore_permissions=True)
		return frappe.get_doc({"doctype": "Rate Plan", "property": PROP_S, "rate_plan_name": code,
		                       "code": code, "modifier_type": "Percent",
		                       "modifier_value": modifier, "rate_category": category}
		                      ).insert(ignore_permissions=True).name

	def stay(self, room, plan):
		doc = frappe.get_doc({
			"doctype": "Reservation", "property": PROP_S, "room_type": self.rt,
			"guest": make_guest(), "status": "Confirmed", "room": room, "rate_plan": plan,
			"check_in_date": YESTERDAY, "check_out_date": d(1), "adults": 2, "children": 0})
		doc.flags.allow_past_check_in = True
		# the sign-off a comp or house-use plan takes at booking
		# (CompSignOffTests): another supervisor's PIN
		doc.flags.kamra_discount_supervisor = {"user": GM, "pin": "4321"}
		doc.insert(ignore_permissions=True)
		doc.status = "Checked In"
		doc.save(ignore_permissions=True)
		return doc

	def post(self, res, charge_type, amount, day):
		folio = frappe.get_doc("Folio", guest_folio(res.name))
		folio.append("charges", {"posting_date": day, "charge_type": charge_type,
		                         "reservation": res.name, "description": f"UT {charge_type}",
		                         "qty": 1, "rate": amount, "amount": amount, "gst_rate": 0})
		folio.save(ignore_permissions=True)

	def figures(self, row, keys=FIGURES):
		return {k: row.get(k) for k in keys}

	def test_the_nights_posted_are_the_plans_prices(self):
		posted = [round(flt(c.amount), 2) for res in self.stays
		          for c in frappe.get_all("Folio Charge", filters={
				"reservation": res.name, "charge_type": "Room", "posting_date": YESTERDAY},
			fields=["amount"])]
		self.assertEqual(posted, [1000, 1500, 0, 0])

	def test_the_flash_day_leaves_comp_and_house_use_out_of_adr(self):
		day = reports._day_stats(PROP_S, YESTERDAY, 4)
		self.assertEqual(self.figures(day), self.EXPECTED)
		self.assertEqual(day["room_revenue"], 2600)

	def test_every_report_gives_the_same_figures_for_the_same_nights(self):
		self.assert_every_report_gives_the_expected_figures()

	def test_a_category_changed_after_the_night_leaves_every_report_as_it_was(self):
		# OPERA keeps End of Day statistics as the night closed, and a rate
		# code change reaches no reservation already booked (R2): the stay
		# keeps the category it was booked on
		plan = frappe.get_doc("Rate Plan", f"{PROP_S}-BAR")
		plan.rate_category = "House Use"
		plan.save()
		self.assertEqual(frappe.db.get_value("Reservation", self.stays[0].name, "rate_category"),
		                 self.PLANS[0][2])
		self.assert_every_report_gives_the_expected_figures()

	def test_every_occupancy_reader_counts_the_house_as_the_flash_does(self):
		# tonight, the open business date: the four stays are in house. Every
		# reader of occupancy applies the property's categories
		# (kamra.room_statistics), so no-occupancy rooms are out of all of them
		from unittest.mock import patch
		day = reports._day_stats(PROP_S, TODAY, 4)
		owner = api.owner_briefing(PROP_S, TODAY)
		forecast = dashboards.occupancy_forecast(PROP_S, TODAY, days=1)
		books = next(p for p in dashboards.comparison(PROP_S, TODAY, 4, forecast)["periods"]
		             if p["id"] == "on_the_books")
		with patch("kamra.api.my_properties",
		           return_value=[{"name": PROP_S, "business_date": TODAY}]):
			portfolio = dashboards.portfolio_dashboard(TODAY)
		readers = {
			"reports._day_stats": day["occupancy_pct"],
			"api.owner_briefing": owner["occupancy_pct"],
			"dashboards.occupancy_now": dashboards.occupancy_now(
				PROP_S, {"occupied": 4})["occupancy_pct"],
			"dashboards.occupancy_forecast": forecast[0]["occupancy_pct"],
			"dashboards.occupancy_forecast with holds": forecast[0]["occupancy_with_holds_pct"],
			"reports.manager_flash outlook": reports.manager_flash(
				PROP_S, YESTERDAY)["outlook"][0]["occupancy_pct"],
			"dashboards.comparison on_the_books": books["this_year"]["occupancy_pct"],
			"dashboards.portfolio_dashboard row": portfolio["properties"][0]["occupancy_pct"],
			"dashboards.portfolio_dashboard totals": portfolio["totals"]["occupancy_pct"],
		}
		for name, pct in readers.items():
			with self.subTest(reader=name):
				self.assertEqual(pct, self.TONIGHT_OCCUPANCY)
		# the rooms occupied are the rooms the percentage counts, and the
		# comp and house-use rooms are the ones in house
		self.assertEqual((day["rooms_occupied"], owner["rooms_occupied"]),
		                 (self.TONIGHT_ROOMS, self.TONIGHT_ROOMS))
		self.assertEqual(day["rooms_by_category"], self.TONIGHT_BY_CATEGORY)
		# a room on the books is not for sale, whatever its category
		self.assertEqual((forecast[0]["booked"], forecast[0]["available"]), (4, 0))

	def test_room_insights_count_the_night_as_every_report_does(self):
		from kamra.rooms import room_insights
		out = room_insights(PROP_S, from_date=YESTERDAY, to_date=YESTERDAY, refresh=1)
		self.assertEqual(out["summary"]["occupancy_pct"], self.EXPECTED["occupancy_pct"])

	def test_the_night_audit_report_counts_room_revenue_as_the_flash_does(self):
		# a discount on its own line (a booking voucher) is an adjustment, as
		# kamra.ledger books it (Discount, 9000), not room revenue
		self.post(self.stays[0], "Discount", -200, YESTERDAY)
		day = audit_report.build(PROP_S, YESTERDAY)["periods"][0]
		flash = reports._day_stats(PROP_S, YESTERDAY, 4)
		# Room and Rate Adjustment, as ADR and RevPAR count it
		self.assertEqual((day["room_revenue"], flash["room_revenue"]), (2600, 2600))
		self.assertEqual((day["other"], day["allowances"]), (0, -200))
		self.assertEqual(day["total_revenue"],
		                 day["room_revenue"] + day["food"] + day["beverage"] + day["banquets"]
		                 + day["meal_plan_minibar"] + day["other"] + day["allowances"])
		depts = {r["department"]: r["amount"]
		         for r in audit_report.departments(PROP_S, YESTERDAY, YESTERDAY)}
		self.assertEqual((depts["Rooms"], depts["Discounts and allowances"]), (2600, -200))
		self.assertNotIn("Rate Adjustment", depts)

	def assert_every_report_gives_the_expected_figures(self):
		from kamra.custom_reports import _fetch_occupancy_by_day
		flash = reports.manager_flash(PROP_S, YESTERDAY)
		owner = api.owner_briefing(PROP_S, TODAY)["yesterday"]
		budget = {r["key"]: r["actual"] for r in
		          reports.budget_vs_actual(PROP_S, YESTERDAY[:7])["rows"]}
		single_day = {
			"reports._day_stats": reports._day_stats(PROP_S, YESTERDAY, 4),
			"reports.manager_flash today": flash["today"],
			"reports.range_stats": reports.range_stats(PROP_S, YESTERDAY, YESTERDAY, 4),
			"dashboards.comparison today": dashboards.comparison(
				PROP_S, YESTERDAY, 4, [])["periods"][0]["this_year"],
			"audit_report day": audit_report.build(PROP_S, YESTERDAY)["periods"][0],
			"custom_reports occupancy_by_day": _fetch_occupancy_by_day(
				PROP_S, YESTERDAY, YESTERDAY)[0],
		}
		for name, row in single_day.items():
			with self.subTest(report=name):
				self.assertEqual(self.figures(row), self.EXPECTED)
		# the owner's briefing: yesterday's revenue figures (its occupancy
		# is the house tonight, not yesterday's)
		with self.subTest(report="api.owner_briefing yesterday"):
			keys = ("rooms_by_category", "adr", "revpar")
			self.assertEqual(self.figures(owner, keys), self.figures(self.EXPECTED, keys))
		# month to date: the same night is the only one posted, so the
		# average rate is the same; RevPAR and occupancy divide by the month
		with self.subTest(report="reports.manager_flash mtd"):
			keys = ("rooms_sold", "rooms_by_category", "adr")
			self.assertEqual(self.figures(flash["mtd"], keys), self.figures(self.EXPECTED, keys))
		with self.subTest(report="reports.budget_vs_actual"):
			self.assertEqual(budget["adr"], self.EXPECTED["adr"])

	def test_the_projected_adr_leaves_out_comp_and_house_use_stays(self):
		# tonight (the open business date): the four stays in house at their
		# locked rates - 1,000, 1,500 and two at no charge
		tonight = reports._day_stats(PROP_S, TODAY, 4)
		self.assertEqual((tonight["projected_room_revenue"], tonight["projected_adr"],
		                  tonight["projected_revpar"]), self.PROJECTED)
		# the rooms in house tonight, by the same rule
		self.assertEqual((tonight["occupancy_basis"], tonight["occupancy_pct"]),
		                 ("in_house", self.TONIGHT_OCCUPANCY))


class ConfiguredStatisticsTests(CrossReportConsistencyTests):
	"""The same night at a property that configured its own categories
	(CONFIGURED): the comp counts in ADR, house use is out of ADR and
	occupancy, and the 1,500 room is on a Staff plan, out of ADR and RevPAR.
	Every report still agrees: ADR 1,100 / 2 = 550, RevPAR 1,100 / 4 = 275,
	occupancy 3 / 4 = 75%. Each stay is booked on its plan's category (R2: a
	stay keeps the category it was booked on)."""

	EXPECTED: ClassVar[dict] = {"rooms_sold": 4,
	                            "rooms_by_category": {"Complimentary": 1, "House Use": 1, "Staff": 1},
	                            "adr": 550, "revpar": 275, "occupancy_pct": 75.0}
	# tonight's locked rates: 1,000 standard, 1,500 Staff, two at no charge
	PROJECTED: ClassVar[tuple] = (2500, 500, 250)
	TONIGHT_OCCUPANCY: ClassVar[float] = 75.0
	# the house-use room is in house but not counted as occupied
	TONIGHT_ROOMS: ClassVar[int] = 3
	TONIGHT_BY_CATEGORY: ClassVar[dict] = {"Complimentary": 1, "House Use": 1, "Staff": 1}
	CATEGORIES: ClassVar[str | None] = CONFIGURED
	PLANS: ClassVar[tuple] = (("BAR", 0, "Standard"), ("UP", 50, "Staff"),
	                          ("COMP", -100, "Complimentary"), ("HOUSE", -100, "House Use"))


class CompSignOffTests(DiscountTestCase):
	"""A Complimentary or House Use plan is signed off by another supervisor
	with their PIN at booking and when a stay is moved to it."""

	def setUp(self):
		super().setUp()
		self.comp = self.plan("COMP", "Percent", -100, rate_category="Complimentary")
		self.house = self.plan("HOUSE", "Percent", -100, rate_category="House Use")
		self.bar = self.plan("BAR")

	def booked(self, guest):
		return frappe.db.count("Reservation", {"property": PROP_A, "guest_name": guest})

	def test_a_new_plan_is_standard(self):
		self.assertEqual(frappe.db.get_value("Rate Plan", self.bar, "rate_category"), "Standard")

	def test_booking_a_comp_or_house_use_plan_needs_a_supervisor(self):
		for plan in (self.comp, self.house):
			with self.subTest(plan=plan):
				with self.assertRaises(frappe.PermissionError):
					self.as_user(DESK, api.create_booking, PROP_A, self.rt, d(5), d(7),
					             f"UT Comp {plan}", rate_plan=plan)
				self.assertEqual(self.booked(f"UT Comp {plan}"), 0)

	def test_a_supervisors_pin_signs_it_off_and_is_logged(self):
		out = self.book(DESK, rate_plan=self.comp, supervisor_user=GM, supervisor_pin="4321")
		doc = frappe.get_doc("Reservation", out["reservation"])
		self.assertEqual(doc.rate_plan, self.comp)
		self.assertEqual([flt(r.rate) for r in doc.night_rates], [0, 0])
		logs = sign_off_logs(doc.name)
		self.assertEqual([(r.actor, r.approver) for r in logs], [(DESK, GM)])
		self.assertIn("Complimentary", logs[0].rationale)

	def test_nobody_signs_off_their_own_and_a_wrong_pin_is_refused(self):
		with self.assertRaises(frappe.PermissionError) as raised:
			self.book(GM, rate_plan=self.house, supervisor_user=GM, supervisor_pin="4321")
		self.assertTrue(str(raised.exception).startswith("SUPERVISOR_SELF_APPROVAL:"))
		with self.assertRaises((frappe.ValidationError, frappe.PermissionError)):
			self.book(DESK, rate_plan=self.house, supervisor_user=GM, supervisor_pin="0000")
		self.desk_with_finance()
		out = self.book(GM, rate_plan=self.house, supervisor_user=SELF, supervisor_pin="5678")
		self.assertEqual([r.approver for r in sign_off_logs(out["reservation"])], [SELF])

	def test_an_ai_agent_cant_book_a_comp_or_use_a_pin(self):
		for i, sign in enumerate(({}, {"supervisor_user": GM, "supervisor_pin": "4321"})):
			with self.subTest(sign=bool(sign)):
				with self.assertRaises(frappe.PermissionError):
					self.as_agent(DESK, api.create_booking, PROP_A, self.rt, d(5), d(7),
					              f"UT Agent Comp {i}", rate_plan=self.comp, **sign)
		with self.assertRaises(frappe.PermissionError):
			self.as_agent(AGENT, api.create_booking, PROP_A, self.rt, d(5), d(7),
			              "UT Agent Alone Comp", rate_plan=self.comp)

	def test_a_plain_save_cant_move_a_stay_to_house_use(self):
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0], rate_plan=self.bar)
		doc = frappe.get_doc("Reservation", res.name)
		doc.rate_plan = self.house
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, doc.save)
		self.assertEqual(frappe.db.get_value("Reservation", res.name, "rate_plan"), self.bar)

	def test_a_rest_put_cant_move_a_stay_to_complimentary(self):
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[1], rate_plan=self.bar)
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, frappe.client.set_value, "Reservation", res.name,
			             "rate_plan", self.comp)
		self.assertEqual(frappe.db.get_value("Reservation", res.name, "rate_plan"), self.bar)

	def test_amend_stay_moves_a_stay_to_comp_with_the_sign_off(self):
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[2], rate_plan=self.bar)
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, api.amend_stay, res.name, d(5), d(7), confirm_price=1,
			             rate_plan=self.comp)
		preview = self.as_user(DESK, api.amend_stay_preview, res.name, d(5), d(7),
		                       rate_plan=self.comp)
		self.assertTrue(preview["comp_governance"]["needs_supervisor"])
		self.assertEqual(preview["new_amount"], 0)
		self.as_user(DESK, api.amend_stay, res.name, d(5), d(7), confirm_price=1,
		             rate_plan=self.comp, supervisor_user=GM, supervisor_pin="4321")
		doc = frappe.get_doc("Reservation", res.name)
		self.assertEqual(doc.rate_plan, self.comp)
		self.assertEqual([flt(r.rate) for r in doc.night_rates], [0, 0])
		self.assertEqual([r.approver for r in sign_off_logs(res.name)], [GM])
		# the amendment's own row names who signed it off too (R2)
		amend = frappe.get_all("Agent Action Log", filters={
			"action_type": "amend_stay", "reference_name": res.name}, pluck="approver")
		self.assertEqual(amend, [GM])
		# and the stay now has the plan's category
		self.assertEqual(doc.rate_category, "Complimentary")

	def test_a_save_that_keeps_the_plan_asks_nothing_again(self):
		out = self.book(DESK, rate_plan=self.comp, supervisor_user=GM, supervisor_pin="4321")
		doc = frappe.get_doc("Reservation", out["reservation"])
		doc.special_requests = "UT late arrival"
		self.as_user(DESK, doc.save)
		self.assertEqual(len(sign_off_logs(doc.name)), 1)

	def test_a_category_without_a_sign_off_books_straight_away(self):
		# this property's comps take no sign-off; house use still does
		set_setting(PROP_A, "rate_categories",
		            "Standard\nComplimentary: no ADR\nHouse Use: sign-off, no ADR")
		out = self.book(DESK, rate_plan=self.comp)
		self.assertEqual(frappe.db.get_value("Reservation", out["reservation"], "rate_plan"),
		                 self.comp)
		self.assertEqual(sign_off_logs(out["reservation"]), [])
		gov = self.as_user(DESK, api.get_quote, PROP_A, self.rt, d(5), d(7),
		                   rate_plan=self.comp)["comp_governance"]
		self.assertEqual((gov["rate_category"], gov["needs_supervisor"]), ("Complimentary", False))
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, api.create_booking, PROP_A, self.rt, d(5), d(7),
			             "UT House Unsigned", rate_plan=self.house)

	def test_the_approvers_come_from_the_propertys_settings(self):
		set_setting(PROP_A, "rate_category_approvers", "Revenue Manager")
		frappe.db.delete("Cashier PIN", {"name": REVENUE})
		frappe.get_doc({"doctype": "Cashier PIN", "user": REVENUE, "pin": "2468"}
		               ).insert(ignore_permissions=True)
		gov = self.as_user(DESK, api.get_quote, PROP_A, self.rt, d(5), d(7),
		                   rate_plan=self.comp)["comp_governance"]
		approvers = [s["user"] for s in gov["supervisors"]]
		self.assertIn(REVENUE, approvers)
		self.assertNotIn(GM, approvers)
		# a Hotel Admin is not an approver here
		with self.assertRaises(frappe.PermissionError):
			self.book(DESK, rate_plan=self.comp, supervisor_user=GM, supervisor_pin="4321")
		out = self.book(DESK, rate_plan=self.comp, supervisor_user=REVENUE, supervisor_pin="2468")
		self.assertEqual([r.approver for r in sign_off_logs(out["reservation"])], [REVENUE])

	def test_the_quote_says_a_comp_needs_a_supervisor_and_who_can_sign(self):
		gov = self.as_user(DESK, api.get_quote, PROP_A, self.rt, d(5), d(7),
		                   rate_plan=self.comp)["comp_governance"]
		self.assertEqual(gov["rate_category"], "Complimentary")
		self.assertTrue(gov["needs_supervisor"])
		self.assertIn(GM, [s["user"] for s in gov["supervisors"]])
		self.assertNotIn(DESK, [s["user"] for s in gov["supervisors"]])
		plain = self.as_user(DESK, api.get_quote, PROP_A, self.rt, d(5), d(7),
		                     rate_plan=self.bar)["comp_governance"]
		self.assertEqual((plain["rate_category"], plain["needs_supervisor"]), ("Standard", False))


class CompTaxTests(DiscountTestCase):
	"""A stay on a Complimentary plan still taxes the cleaning fee and a
	taxable levy at the room's own rate (#32 R4-4, through a rate plan
	rather than a 100% discount)."""

	properties = (PROP_A, PROP_V)

	def setUp(self):
		super().setUp()
		make_property(PROP_V, country="United Arab Emirates", cleaning_fee=500,
		              room_levy_per_night=100, room_levy_taxable=1, cleaning_fee_taxable=1,
		              business_date=YESTERDAY)
		self.vat_rt = make_room_type(PROP_V, "STD", base_price=1000)
		frappe.db.set_value("Room Type", self.vat_rt, {"base_price": 1000, "tax_percent": 0})
		frappe.clear_document_cache("Room Type", self.vat_rt)
		self.vat_room = make_room(PROP_V, self.vat_rt, "301")
		name = f"{PROP_V}-COMP"
		if frappe.db.exists("Rate Plan", name):
			frappe.delete_doc("Rate Plan", name, force=True, ignore_permissions=True)
		self.vat_comp = frappe.get_doc({
			"doctype": "Rate Plan", "property": PROP_V, "rate_plan_name": "COMP", "code": "COMP",
			"modifier_type": "Percent", "modifier_value": -100,
			"rate_category": "Complimentary"}).insert(ignore_permissions=True).name

	def test_a_comp_plan_keeps_the_cleaning_fee_and_levy_taxed_at_the_room_rate(self):
		rate = flt(pricing.quote(PROP_V, self.vat_rt, d(5), d(7))["nightly"][0]["gst_rate"])
		self.assertGreater(rate, 0)
		q = pricing.quote(PROP_V, self.vat_rt, d(5), d(7), rate_plan=self.vat_comp)
		# the plan works on the stay's room total (pricing.apply_rate_plan)
		self.assertEqual(flt(q["room_total"]), 0)
		self.assertEqual([flt(n["gst_rate"]) for n in q["nightly"]], [rate, rate])
		cleaning = self.line(q, "cleaning_fee")
		self.assertAlmostEqual(cleaning["tax_amount"], 500 * rate / 100, places=2)
		levy = self.line(q, "room_levy")
		self.assertAlmostEqual(levy["tax_amount"], 200 * rate / 100, places=2)
		out = self.as_user(DESK, api.create_booking, PROP_V, self.vat_rt, d(5), d(7),
		                   "UT Comp VAT Guest", rate_plan=self.vat_comp,
		                   supervisor_user=GM, supervisor_pin="4321")
		doc = frappe.get_doc("Reservation", out["reservation"])
		self.assertAlmostEqual(flt(doc.amount_after_tax), 700 * (1 + rate / 100), places=2)
		# the folio posts the levy at the night's own (room) rate
		self.assertEqual(flt(folio_mod._nightly_gst(doc, d(5))), rate)

	@staticmethod
	def line(q, component):
		# as test_reservation_discounts_r2: a quote's line_items by component
		return next(li for li in q.get("line_items") or [] if li.get("component") == component)


class HeaderTotalsGuardTests(DiscountTestCase):
	"""An auto-priced stay's header totals follow its locked nights: a plain
	save (REST PUT) that writes them is refused, like a locked night."""

	HEADER = ("amount_before_tax", "tax_amount", "amount_after_tax", "discount_amount")

	def totals(self, name):
		return frappe.db.get_value("Reservation", name, list(self.HEADER), as_dict=True)

	def test_a_plain_save_cant_write_a_header_total(self):
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0])
		before = self.totals(res.name)
		self.assertGreater(flt(before.amount_after_tax), 0)
		for field in self.HEADER:
			with self.subTest(field=field):
				doc = frappe.get_doc("Reservation", res.name)
				doc.set(field, flt(doc.get(field)) + 1)
				with self.assertRaises(frappe.PermissionError):
					self.as_user(DESK, doc.save)
		self.assertEqual(self.totals(res.name), before)

	def test_a_rest_put_cant_write_the_total(self):
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[1])
		before = self.totals(res.name)
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, frappe.client.set_value, "Reservation", res.name,
			             "amount_after_tax", 1)
		self.assertEqual(self.totals(res.name), before)

	def test_the_stays_own_pricing_still_moves_the_totals(self):
		res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[2])
		before = flt(self.totals(res.name).amount_after_tax)
		self.as_user(DESK, api.amend_stay, res.name, d(5), d(8), confirm_price=1)
		self.assertAlmostEqual(flt(self.totals(res.name).amount_after_tax), before * 3 / 2,
		                       places=2)
		doc = frappe.get_doc("Reservation", res.name)
		doc.special_requests = "UT quiet room"
		self.as_user(DESK, doc.save)
