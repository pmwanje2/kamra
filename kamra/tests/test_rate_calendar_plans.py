"""The Rate Calendar as a rate query: by rate plan and customer, priced by the quote.

OPERA Cloud's Rate Query and SAP's pricing analysis show the sellable price
per date for a rate code (or a customer's negotiated one), from the same
condition records a booking uses. Here every cell under a plan is a real
`pricing.quote_with_contract`, the path a booking prices through; with no
plan the calendar stays the card (BAR) view it was."""

from __future__ import annotations

from types import ModuleType
from unittest.mock import patch

from kamra.tests.rate_matrix_harness import PermissionDenied, RateMatrixHarness, ValidationError

STD, DLX = "Test Hotel-STD", "Test Hotel-DLX"
DATES = ["2026-12-01", "2026-12-02"]
REFUSAL = "HSP-TARIFF-004: No approved tariff for this date."


class RateCalendarPlansTest(RateMatrixHarness):
	modules = ("kamra.pricing", "kamra.authz", "kamra.rates")

	def extra_modules(self):
		folio = ModuleType("kamra.folio")
		folio._fnb_gst = lambda property: 5
		return {"kamra.folio": folio}

	def setUp(self):
		super().setUp()
		self.authz.restricted_properties = lambda user=None: None
		self.records["Room Type"] = [
			self.add("Room Type", STD, property="Test Hotel", disabled=0, room_type_code="STD",
			         room_type_name="Standard", base_price=100000, base_occupancy=2, adults_capacity=3,
			         extra_adult_price=20000, child_price=10000),
		]
		self.records["Rate Plan"] = []
		self.rate_plan("CONTRACT", rate_plan_name="Corporate tariff", currency="USD", is_default=1,
		               rounding_step=1000, fx_lock_policy="Fixed at Check In")
		self.rate_plan("CORP", rate_plan_name="Corporate card", modifier_type="Percent", modifier_value=-10)
		self.rate_plan("OLD", rate_plan_name="Retired", disabled=1)
		self.add("Rate Plan", "SISTER", property="Sister Hotel", rate_plan_name="Sister", code="SIS", disabled=0)
		self.add("Corporate Account", "ACME", company_name="Acme Ltd", negotiated_rate_plan="CONTRACT")
		# 130 USD at 3,648.2 = 474,266 UGX; the plan's 1,000 step makes it 474,000
		self.tariffs["CONTRACT"] = self.tariff(fx_rate=3648.2, agreed_rate=474266)
		self.permission_checks = []
		self.frappe.has_permission = self.has_permission
		self.denied = set()
		p = self.pricing
		rules = {"no_small_group": False, "minimum_nights": 0, "season": None, "non_refundable": False,
		         "full_payment": False, "is_holiday": False, "holiday": None, "advance_percent": 0,
		         "advance_season": None}
		for name, value in (
			("small_group_allows_stay", lambda *args: True), ("stay_season_rules", lambda *args, **kw: rules),
			("long_stay_rule", lambda *args: None), ("active_holiday_package", lambda *args: None),
			("season_adjust", lambda property, date, base, room_type=None: base),
			("forecast_occupancy", lambda *args, **kw: 0), ("demand_adjust", lambda *args, **kw: (args[3], None)),
			("advance_percent", lambda *args: (0.0, None)), ("active_season", lambda *args, **kw: None),
			("package_covering_date", lambda *args, **kw: None),
		):
			patcher = patch.object(p, name, value)
			patcher.start()
			self.addCleanup(patcher.stop)

	def rate_plan(self, name, **fields):
		doc = self.plan(name, **{"disabled": 0, "is_default": 0, **fields})
		self.records["Rate Plan"].append(doc)
		return doc

	def has_permission(self, doctype, ptype="read", doc=None, throw=False, **kwargs):
		self.permission_checks.append((doctype, ptype, doc, throw))
		if doctype in self.denied:
			raise PermissionDenied(f"No permission for {doctype}")
		return True

	def calendar(self, **kwargs):
		return self.rates.rate_calendar("Test Hotel", from_date=DATES[0], days=len(DATES), **kwargs)

	def cells(self, result, room_type=STD):
		row, = [row for row in result["room_types"] if row["room_type"] == room_type]
		return row["nights"]

	def test_without_a_plan_the_calendar_is_the_card_view_it_was(self):
		result = self.calendar()
		doc = self.docs["Room Type", STD]
		self.assertEqual(self.cells(result), [
			self.rates.explain_night("Test Hotel", doc, self.utils.getdate(d), 2, 0, occupancy=0, small_ok=True)
			for d in DATES])
		self.assertEqual(self.tariff_forms(), [])
		self.assertEqual([plan["name"] for plan in result["rate_plans"]], ["CONTRACT", "CORP"])
		self.assertEqual(result["default_plan"], "CONTRACT")
		self.assertEqual(result["property_currency"], "UGX")
		self.assertIsNone(result["rate_plan"])
		self.assertIsNone(result["company"])
		self.assertEqual(self.permission_checks, [])

	def test_a_contract_plan_prices_each_cell_through_the_contract_quote(self):
		agreed, snapshot = self.pricing.contract_rate("Test Hotel", STD, "CONTRACT", DATES[0], DATES[1], 2, 1)
		self.assertEqual(agreed, 474000)
		self.bridge_calls.clear()
		result = self.calendar(rate_plan="CONTRACT", adults=2, children=1)
		for night in self.cells(result):
			with self.subTest(date=night["date"]):
				self.assertEqual((night["rate"], night["bar"], night["after_season"]), (agreed, agreed, agreed))
				self.assertEqual(night["contract"], snapshot)
				self.assertEqual(night["source"], "contract")
				self.assertIs(night["available"], True)
				self.assertEqual((night["season"], night["demand"], night["package"]), (None, None, None))
				self.assertEqual(night["occupancy"], 0)
		forms = self.tariff_forms()
		self.assertEqual([(f["room_type"], f["check_in"], f["check_out"]) for f in forms],
		                 [(STD, "2026-12-01", "2026-12-02"), (STD, "2026-12-02", "2026-12-03")])
		for form in forms:
			self.assertEqual((form["rate_plan"], form["adults"], form["children"], form["pricing_event"]),
			                 ("CONTRACT", 2, 1, "reservation"))
			self.assertNotIn("company", form)

	def test_a_room_type_without_a_tariff_on_the_plan_is_priced_from_the_card(self):
		self.records["Room Type"].append(self.add(
			"Room Type", DLX, property="Test Hotel", disabled=0, room_type_code="DLX", room_type_name="Deluxe",
			base_price=150000, base_occupancy=2, adults_capacity=2))
		self.tariffs["CORP"] = lambda form: self.tariff() if form["room_type"] == DLX else {"is_tariff": 0}
		result = self.calendar(rate_plan="CORP")
		for night in self.cells(result):
			with self.subTest(date=night["date"]):
				self.assertEqual(night["rate"], 90000)
				self.assertEqual(night["source"], "card")
				self.assertIsNone(night["contract"])
				self.assertIs(night["available"], True)
				# the card layers stay on the cell
				self.assertEqual((night["card_rate"], night["bar"], night["after_season"]), (100000, 100000, 100000))
				self.assertIn("season", night)
		self.assertEqual({night["source"] for night in self.cells(result, DLX)}, {"contract"})

	def test_a_card_priced_cell_in_a_tax_inclusive_house_is_on_the_gross_basis(self):
		self.docs["Property", "Test Hotel"].rates_include_tax = 1
		self.room_tax = 18
		result = self.calendar(rate_plan="CORP")
		self.assertTrue(result["tax_inclusive"])
		for night in self.cells(result):
			with self.subTest(date=night["date"]):
				# 100,000 gross less 10%, as the BAR cells show the card: gross, whole shillings
				self.assertEqual(night["rate"], 90000)
				self.assertEqual(night["source"], "card")
		bar = self.cells(self.calendar())
		self.assertEqual([night["rate"] for night in bar], [100000, 100000])

	def test_a_derived_plan_on_its_base_contract_names_the_base(self):
		self.rate_plan("CON-NR", rate_plan_name="Contract non-refundable", base_rate_plan="CONTRACT",
		               modifier_type="Percent", modifier_value=-10, rounding_step=1000)
		night = self.cells(self.calendar(rate_plan="CON-NR"))[0]
		self.assertEqual(night["source"], "contract")
		self.assertEqual(night["contract"]["derived_from"], "CONTRACT")
		self.assertEqual(night["contract"]["nightly"], 117)
		# 117 USD x 3,648.2 = 426,839.4, to the derived plan's 1,000 step
		self.assertEqual(night["rate"], 427000)

	def test_a_resolver_refusal_closes_only_that_cell(self):
		self.tariffs["CONTRACT"] = lambda form: (
			{"refusal": REFUSAL} if form["check_in"] == DATES[1] else self.tariff(fx_rate=3648.2, agreed_rate=474266))
		first, second = self.cells(self.calendar(rate_plan="CONTRACT"))
		self.assertEqual((first["available"], first["rate"], first["source"]), (True, 474000, "contract"))
		self.assertEqual(second, {"date": DATES[1], "available": False, "refusal": REFUSAL, "rate": None,
		                          "contract": None, "occupancy": 0})

	def test_a_customer_alone_prices_on_its_negotiated_plan(self):
		result = self.calendar(company="ACME")
		self.assertEqual(self.permission_checks, [("Corporate Account", "read", "ACME", True)])
		self.assertEqual(result["rate_plan"]["name"], "CONTRACT")
		self.assertEqual(result["company"], "ACME")
		self.assertEqual({night["rate"] for night in self.cells(result)}, {474000})
		forms = self.tariff_forms()
		self.assertEqual(len(forms), len(DATES))
		self.assertEqual({form["company"] for form in forms}, {"ACME"})

	def test_a_customer_the_user_cannot_read_is_refused_before_any_price(self):
		self.denied.add("Corporate Account")
		for kwargs in ({"company": "ACME"}, {"company": "ACME", "rate_plan": "CONTRACT"}):
			with self.subTest(kwargs=kwargs), self.assertRaises(PermissionDenied):
				self.calendar(**kwargs)
		self.assertEqual(self.tariff_forms(), [])

	def test_a_disabled_or_foreign_plan_is_refused(self):
		for plan in ("OLD", "SISTER", "MISSING"):
			with self.subTest(plan=plan), self.assertRaises(ValidationError):
				self.calendar(rate_plan=plan)
		self.assertEqual(self.tariff_forms(), [])

	def test_the_response_names_the_plan_and_its_policy(self):
		self.rate_plan("CON-NR", rate_plan_name="Contract non-refundable", code="CNR", base_rate_plan="CONTRACT",
		               rounding_step=500, rounding_mode="Down", fx_lock_policy=None)
		expected = {
			"CONTRACT": {"name": "CONTRACT", "label": "Corporate tariff", "code": "CONTRACT", "currency": "USD",
			             "fx_lock_policy": "Fixed at Check In", "rounding_step": 1000, "rounding_mode": "Nearest",
			             "base_rate_plan": None},
			"CON-NR": {"name": "CON-NR", "label": "Contract non-refundable", "code": "CNR", "currency": None,
			           "fx_lock_policy": "Fixed at Reservation", "rounding_step": 500, "rounding_mode": "Down",
			           "base_rate_plan": "CONTRACT"},
		}
		for name, policy in expected.items():
			with self.subTest(plan=name):
				self.assertEqual(self.calendar(rate_plan=name)["rate_plan"], policy)
		listed = {plan["name"]: plan for plan in self.calendar()["rate_plans"]}
		self.assertEqual(listed["CON-NR"], expected["CON-NR"])

	def test_uganda_pack_room_levy_does_not_leak_into_card_or_contract_calendar_cells(self):
		"""Merge pin (#34 + #44): Uganda country pack room levy (Local Hotel Tax)
		appends after accommodation in quote(), so rate_calendar's card-fallback
		cell keeps the accommodation gross rate and contract cells stay on contract_local_amount."""
		import sys

		pack = sys.modules["kamra.localization"].pack_for("Test Hotel")
		pack.ROOM_LEVY_LABEL = "Local Hotel Tax"
		self.addCleanup(delattr, pack, "ROOM_LEVY_LABEL")
		self.docs["Property", "Test Hotel"].update(
			country="Uganda", rates_include_tax=1, room_levy_per_night=2000, room_levy_label="",
		)
		self.room_tax = 18
		self.records["Room Type"].append(self.add(
			"Room Type", DLX, property="Test Hotel", disabled=0, room_type_code="DLX", room_type_name="Deluxe",
			base_price=150000, base_occupancy=2, adults_capacity=2))
		self.tariffs["CORP"] = lambda form: (
			self.tariff(fx_rate=3648.2, agreed_rate=474266) if form["room_type"] == DLX else {"is_tariff": 0}
		)
		self.docs["Rate Plan", "CORP"].update(rounding_step=1000, rounding_mode="Nearest")
		result = self.calendar(rate_plan="CORP")
		# STD has no tariff -> card fallback: 100,000 gross - 10% = 90,000 (no +2,000 Uganda room levy)
		self.assertEqual({n["rate"] for n in self.cells(result, STD)}, {90000})
		self.assertEqual({n["source"] for n in self.cells(result, STD)}, {"card"})
		# DLX has USD contract tariff -> rounded via contract_local_amount to 474,000
		self.assertEqual({n["rate"] for n in self.cells(result, DLX)}, {474000})
		self.assertEqual({n["source"] for n in self.cells(result, DLX)}, {"contract"})

	def test_opens_on_tariff_default_plan_unless_default_is_card_or_card_explicitly_chosen(self):
		self.docs["Rate Plan", "CONTRACT"].custom_tariff_price_list = "Tank Hill Contract USD"
		# No rate_plan argument -> opens on the default tariff plan (CONTRACT), not Card rate
		opened = self.calendar()
		self.assertEqual(opened["rate_plan"]["name"], "CONTRACT")
		self.assertIs(opened["rate_plan"]["is_tariff"], True)
		self.assertEqual({n["rate"] for n in self.cells(opened)}, {474000})
		by_name = {p["name"]: p for p in opened["rate_plans"]}
		self.assertIs(by_name["CONTRACT"]["is_tariff"], True)
		self.assertIs(by_name["CORP"]["is_tariff"], False)

		# Explicit empty string rate_plan="" -> Card rate (BAR) view
		card = self.calendar(rate_plan="")
		self.assertIsNone(card["rate_plan"])
		self.assertEqual({n["rate"] for n in self.cells(card)}, {100000})

		# Derived default plan (base CONTRACT, -50%) -> also tariff-priced, so calendar opens on it
		self.rate_plan("HALF", rate_plan_name="Half Price USD (-50%)", base_rate_plan="CONTRACT",
		               modifier_type="Percent", modifier_value=-50, rounding_step=1000, is_default=1)
		self.docs["Rate Plan", "CONTRACT"].is_default = 0
		derived = self.calendar()
		self.assertEqual(derived["rate_plan"]["name"], "HALF")
		self.assertIs(derived["rate_plan"]["is_tariff"], True)
		# 65 USD x 3,648.2 = 237,133 -> rounded to 1,000 = 237,000
		self.assertEqual({n["rate"] for n in self.cells(derived)}, {237000})

		# Card-priced default plan (no tariff price list) -> opens on Card rate view
		self.docs["Rate Plan", "HALF"].is_default = 0
		self.docs["Rate Plan", "CORP"].is_default = 1
		card_default = self.calendar()
		self.assertIsNone(card_default["rate_plan"])

	def test_room_types_carry_room_count_and_sellable_flag(self):
		self.records["Room Type"].append(self.add(
			"Room Type", DLX, property="Test Hotel", disabled=0, room_type_code="DLX", room_type_name="Deluxe",
			base_price=150000, base_occupancy=2, adults_capacity=2))
		self.records["Room"] = [
			self.add("Room", "STD-101", property="Test Hotel", room_type=STD),
			self.add("Room", "STD-102", property="Test Hotel", room_type=STD),
		]
		result = self.calendar()
		by_rt = {row["room_type"]: row for row in result["room_types"]}
		self.assertEqual(by_rt[STD]["room_count"], 2)
		self.assertIs(by_rt[STD]["sellable"], True)
		self.assertEqual(by_rt[DLX]["room_count"], 0)
		self.assertIs(by_rt[DLX]["sellable"], False)
