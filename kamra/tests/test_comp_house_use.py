"""Complimentary and House Use rate plans (OPERA Cloud rate code flags;
the parity check's item 11b on #32).

- A Rate Plan carries rate_category: Standard, Complimentary or House Use
  (OPERA's rate code Complimentary and House Use check boxes).
- kamra.room_statistics is the one rule for rooms sold, complimentary and
  house-use rooms, ADR, RevPAR and occupancy, and every report reads it:
  - ADR is room revenue over the rooms sold minus comp and house use (OPERA
    Manager Report, "ADR minus Comp and House");
  - RevPAR is room revenue over the rooms;
  - occupancy counts comp and house-use rooms, because they are occupied.
  So the same nights give the same figures in every report.
- Booking a stay on a Complimentary or House Use plan, or moving a stay to
  one, needs another supervisor's sign-off with their PIN. This is OPERA's
  comp authorizer, through the PIN route the discounts use. Nobody signs
  off their own, and an AI agent can't.
- A comp stay still taxes the lines taxed like the room (cleaning, the levy).
- A plain save (REST PUT) never writes an auto-priced stay's header totals.
"""

from __future__ import annotations

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
from kamra.tests.test_reservation_discounts import AGENT, DESK, GM, PROP_A, SELF, DiscountTestCase

PROP_S = "UT Comp Stats"
PROP_V = "UT Comp VAT"
FIGURES = ("rooms_sold", "comp_rooms", "house_use_rooms", "adr", "revpar", "occupancy_pct")


def sign_off_logs(name):
	return frappe.get_all("Agent Action Log",
	                      filters={"action_type": "comp_rate_sign_off", "reference_name": name},
	                      fields=["actor", "approver", "rationale"])


class RateCategoryFieldTests(AuditTestCase):
	properties = ()

	def test_the_rate_plan_has_operas_two_categories_and_standard(self):
		field = frappe.get_meta("Rate Plan").get_field("rate_category")
		self.assertIsNotNone(field)
		self.assertEqual(field.fieldtype, "Select")
		self.assertEqual(field.options.split("\n"), ["Standard", "Complimentary", "House Use"])
		self.assertEqual(field.default, "Standard")


class RoomFiguresTests(AuditTestCase):
	"""kamra.room_statistics.room_figures: the arithmetic every report uses."""

	properties = ()

	def test_adr_leaves_out_comp_and_house_use_rooms_occupancy_counts_them(self):
		from kamra.room_statistics import room_figures
		out = room_figures(room_revenue=2600, rooms_sold=4, comp_rooms=1, house_use_rooms=1,
		                   occupancy_rooms=4)
		self.assertEqual(out, {"comp_rooms": 1, "house_use_rooms": 1, "occupancy_pct": 100.0,
		                       "adr": 1300, "revpar": 650})

	def test_a_house_of_comps_has_no_adr(self):
		from kamra.room_statistics import room_figures
		out = room_figures(room_revenue=0, rooms_sold=2, comp_rooms=1, house_use_rooms=1,
		                   occupancy_rooms=4)
		self.assertEqual((out["adr"], out["revpar"], out["occupancy_pct"]), (0, 0, 50.0))

	def test_the_occupied_rooms_and_revpar_rooms_can_differ_from_the_rooms_sold(self):
		from kamra.room_statistics import room_figures
		# in house tonight (occupied) over rooms not out of order; RevPAR
		# over every room, as reports._day_stats reports them
		out = room_figures(room_revenue=900, rooms_sold=1, rooms_occupied=3,
		                   occupancy_rooms=3, revpar_rooms=4)
		self.assertEqual((out["adr"], out["revpar"], out["occupancy_pct"]), (900, 225, 100.0))


class CrossReportConsistencyTests(AuditTestCase):
	"""One closed night of four rooms: 1,000 and 1,500 at the rack plans
	(plus a 100 rate correction on the first), a comp and a house-use room
	at no charge. Every report gives the same rooms sold, comp and house-use
	rooms, ADR (2,600 over 2 = 1,300), RevPAR (2,600 over 4 = 650) and
	occupancy (100%)."""

	properties = (PROP_S,)
	EXPECTED = {"rooms_sold": 4, "comp_rooms": 1, "house_use_rooms": 1,
	            "adr": 1300, "revpar": 650, "occupancy_pct": 100.0}

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_S, business_date=TODAY)
		frappe.db.delete("Rate Guardrail", {"property": PROP_S})
		frappe.db.delete("Property Setting", {"property": PROP_S})
		self.rt = make_room_type(PROP_S, "STD", base_price=1000)
		frappe.db.set_value("Room Type", self.rt, "base_price", 1000)
		frappe.clear_document_cache("Room Type", self.rt)
		self.rooms = [make_room(PROP_S, self.rt, f"20{i}") for i in range(1, 5)]
		ensure_user(GM, ["Hotel Admin"])
		frappe.db.delete("Cashier PIN", {"name": GM})
		frappe.get_doc({"doctype": "Cashier PIN", "user": GM, "pin": "4321"}
		               ).insert(ignore_permissions=True)
		plans = [("BAR", 0, "Standard"), ("UP", 50, "Standard"),
		         ("COMP", -100, "Complimentary"), ("HOUSE", -100, "House Use")]
		self.stays = []
		for room, (code, modifier, category) in zip(self.rooms, plans):
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
			self.assertEqual(self.figures(owner, ("comp_rooms", "house_use_rooms", "adr", "revpar")),
			                 self.figures(self.EXPECTED, ("comp_rooms", "house_use_rooms", "adr",
			                                              "revpar")))
		# month to date: the same night is the only one posted, so the
		# average rate is the same; RevPAR and occupancy divide by the month
		with self.subTest(report="reports.manager_flash mtd"):
			self.assertEqual(self.figures(flash["mtd"], ("rooms_sold", "comp_rooms",
			                                             "house_use_rooms", "adr")),
			                 self.figures(self.EXPECTED, ("rooms_sold", "comp_rooms",
			                                              "house_use_rooms", "adr")))
		with self.subTest(report="reports.budget_vs_actual"):
			self.assertEqual(budget["adr"], self.EXPECTED["adr"])

	def test_the_projected_adr_leaves_out_comp_and_house_use_stays(self):
		# tonight (the open business date): the four stays in house at their
		# locked rates - 1,000, 1,500 and two at no charge
		tonight = reports._day_stats(PROP_S, TODAY, 4)
		self.assertEqual(tonight["projected_room_revenue"], 2500)
		self.assertEqual(tonight["projected_adr"], 1250)
		self.assertEqual(tonight["projected_revpar"], 625)


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

	def test_a_save_that_keeps_the_plan_asks_nothing_again(self):
		out = self.book(DESK, rate_plan=self.comp, supervisor_user=GM, supervisor_pin="4321")
		doc = frappe.get_doc("Reservation", out["reservation"])
		doc.special_requests = "UT late arrival"
		self.as_user(DESK, doc.save)
		self.assertEqual(len(sign_off_logs(doc.name)), 1)

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
