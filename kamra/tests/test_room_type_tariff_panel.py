"""The Room Types contract tariff panel, priced by the engine's own rules.

OPERA Cloud shows a room type's Rate Details per rate code with the rate
code's Exchange Type and rounding; SAP shows the PR00 condition with its
validity and rounding rule. The panel's local equivalent is the server's
`pricing.contract_local_amount`, never a client-side multiply."""

from __future__ import annotations

from kamra.tests.rate_matrix_harness import PermissionDenied, RateMatrixHarness

ROOM_TYPE = "Test Hotel-STD"


class RoomTypeTariffPanelTest(RateMatrixHarness):
	modules = ("kamra.pricing", "kamra.authz", "kamra.rates")

	def setUp(self):
		super().setUp()
		self.authz.restricted_properties = lambda user=None: None
		self.permission_checks = []
		self.add("Room Type", ROOM_TYPE, property="Test Hotel", room_type_name="Standard", room_type_code="STD",
		         base_price=100000, check_permission=self.permission_checks.append)
		self.installed = True
		self.rows = [self.row("CONTRACT")]
		self.records["Rate Plan"] = []
		self.rate_plan("CONTRACT", rate_plan_name="Corporate tariff", currency="USD", is_default=1,
		               rounding_step=1000, fx_lock_policy="Fixed at Check In")

	def rate_plan(self, name, **fields):
		doc = self.plan(name, **{"disabled": 0, "is_default": 0, **fields})
		self.records["Rate Plan"].append(doc)
		return doc

	@staticmethod
	def row(rate_plan, **fields):
		"""A row as hospitality_tariffs.room_type_tariffs returns it (tariff-test-fixture.mjs)."""
		values = {"rate_plan": rate_plan, "label": rate_plan, "currency": "USD", "room": 130, "single": 100,
		          "extra": 20, "approval_state": "Approved", "valid_from": "2026-11-01", "fx_rate": 3648.2,
		          "fx_date": "2026-11-21"}
		return {**values, **fields}

	def bridge_call(self, api_method, *, response_types=None, **form):
		if api_method != "hospitality_tariffs.room_type_tariffs":
			return super().bridge_call(api_method, response_types=response_types, **form)
		self.bridge_calls.append((api_method, dict(form)))
		if not self.installed:
			return None
		return {"tariffs": [dict(row) for row in self.rows], "all_active_plans_tariff": False}

	def panel(self):
		return self.rates.room_type_tariffs(ROOM_TYPE)

	def test_the_local_equivalent_is_converted_and_rounded_as_the_plan_rounds(self):
		row, = self.panel()["tariffs"]
		# 130 x 3,648.2 = 474,266 and 100 x 3,648.2 = 364,820, both to the plan's 1,000 step
		self.assertEqual((row["local_room"], row["local_single"]), (474000, 365000))
		self.assertNotEqual(row["local_room"], 474266)
		self.assertEqual(self.bridge_calls, [("hospitality_tariffs.room_type_tariffs", {"room_type": ROOM_TYPE})])
		self.assertEqual(self.permission_checks, ["read"])

	def test_each_row_carries_its_rate_plan_lock_and_rounding(self):
		row, = self.panel()["tariffs"]
		self.assertEqual((row["fx_lock_policy"], row["rounding_step"], row["rounding_mode"]),
		                 ("Fixed at Check In", 1000, "Nearest"))
		self.docs["Rate Plan", "CONTRACT"].update(fx_lock_policy=None, rounding_step=0, rounding_mode="Up")
		row, = self.panel()["tariffs"]
		self.assertEqual((row["fx_lock_policy"], row["rounding_step"], row["rounding_mode"]),
		                 ("Fixed at Reservation", 0, "Up"))
		# no step: the posting currency's minor unit, the mode still applies
		self.assertEqual((row["local_room"], row["local_single"]), (474266, 364820))

	def test_a_home_currency_row_or_one_without_a_rate_has_no_local_equivalent(self):
		self.rows = [self.row("CONTRACT", currency="UGX", room=474000, single=365000, fx_rate=1),
		             self.row("CONTRACT", fx_rate=None), self.row("CONTRACT", fx_rate=0)]
		for row in self.panel()["tariffs"]:
			with self.subTest(row=row):
				self.assertIsNone(row["local_room"])
				self.assertIsNone(row["local_single"])

	def test_the_primary_tariff_is_the_default_plan_when_it_has_a_row(self):
		self.rate_plan("RACK", rate_plan_name="Rack", currency="USD")
		self.rows = [self.row("RACK"), self.row("CONTRACT")]
		self.assertEqual(self.panel()["primary_rate_plan"], "CONTRACT")
		# a default plan with no row of its own (a derived default): no primary, the table stays
		self.rows = [self.row("RACK")]
		panel = self.panel()
		self.assertIsNone(panel["primary_rate_plan"])
		self.assertEqual(len(panel["tariffs"]), 1)
		# no default: the sole row's plan, else none
		self.docs["Rate Plan", "CONTRACT"].is_default = 0
		self.assertEqual(self.panel()["primary_rate_plan"], "RACK")
		self.rows = [self.row("RACK"), self.row("CONTRACT")]
		self.assertIsNone(self.panel()["primary_rate_plan"])

	def test_card_rate_plans_are_the_active_plans_with_no_tariff_on_themselves_or_a_base(self):
		self.rate_plan("BAR", rate_plan_name="Best available")
		self.rate_plan("CORP-NR", rate_plan_name="Corporate non-refundable", base_rate_plan="CONTRACT",
		               modifier_value=-5)
		self.rate_plan("WEEKEND", rate_plan_name="Weekend", base_rate_plan="BAR")
		self.rate_plan("OLD", rate_plan_name="Retired", disabled=1)
		self.rate_plan("LOOP-A", rate_plan_name="Loop A", base_rate_plan="LOOP-B")
		self.rate_plan("LOOP-B", rate_plan_name="Loop B", base_rate_plan="LOOP-A")
		self.assertEqual(self.panel()["card_rate_plans"], [
			{"name": "BAR", "label": "Best available"},
			{"name": "LOOP-A", "label": "Loop A"},
			{"name": "LOOP-B", "label": "Loop B"},
			{"name": "WEEKEND", "label": "Weekend"},
		])

	def test_the_bridge_payload_passes_through_with_the_property_currency(self):
		panel = self.panel()
		self.assertIs(panel["all_active_plans_tariff"], False)
		self.assertEqual(panel["property_currency"], "UGX")
		row, = panel["tariffs"]
		self.assertEqual({key: row[key] for key in self.row("CONTRACT")}, self.row("CONTRACT"))

	def test_no_installed_bridge_reads_as_none(self):
		self.installed = False
		self.assertIsNone(self.panel())

	def test_a_property_out_of_scope_is_refused_before_the_bridge(self):
		self.authz.restricted_properties = lambda user=None: {"Sister Hotel"}
		with self.assertRaises(PermissionDenied):
			self.panel()
		self.assertEqual(self.bridge_calls, [])

	def test_bridge_explicit_plan_lists_are_preserved_and_unpriced_tariff_plans_never_enter_card_rate_plans(self):
		self.rate_plan("HALF", rate_plan_name="Half Price USD (-50%)", base_rate_plan="CONTRACT",
		               modifier_type="Percent", modifier_value=-50)
		self.rows = []
		orig_bridge = self.bridge_call

		def explicit_bridge(api_method, *, response_types=None, **form):
			if api_method == "hospitality_tariffs.room_type_tariffs":
				self.bridge_calls.append((api_method, dict(form)))
				return {
					"tariffs": [],
					"all_active_plans_tariff": 1,
					"tariff_plans": [],
					"card_rate_plans": [],
					"unpriced_plans": [
						{"name": "CONTRACT", "label": "Corporate tariff"},
						{"name": "HALF", "label": "Half Price USD (-50%)"},
					],
				}
			return orig_bridge(api_method, response_types=response_types, **form)

		self.frappe.get_meta = lambda doctype: type("Meta", (), {"has_field": lambda _s, f: True})()
		import sys
		sys.modules["kamra.erp_bridge"].bridge_call = explicit_bridge
		panel = self.panel()
		self.assertIs(panel["all_active_plans_tariff"], True)
		self.assertEqual(panel["tariff_plans"], [])
		self.assertEqual(panel["card_rate_plans"], [])
		self.assertEqual(panel["unpriced_plans"], [
			{"name": "CONTRACT", "label": "Corporate tariff"},
			{"name": "HALF", "label": "Half Price USD (-50%)"},
		])

	def test_legacy_bridge_without_explicit_lists_separates_unpriced_tariff_plans_from_card_plans(self):
		self.docs["Rate Plan", "CONTRACT"].custom_tariff_price_list = "USD Contract"
		self.rate_plan("HALF", rate_plan_name="Half Price USD (-50%)", base_rate_plan="CONTRACT",
		               modifier_type="Percent", modifier_value=-50)
		self.rate_plan("BAR", rate_plan_name="Best available")
		self.rows = []
		panel = self.panel()
		self.assertEqual(panel["card_rate_plans"], [{"name": "BAR", "label": "Best available"}])
		self.assertEqual(panel["unpriced_plans"], [
			{"name": "CONTRACT", "label": "Corporate tariff"},
			{"name": "HALF", "label": "Half Price USD (-50%)"},
		])
		self.assertIs(panel["all_active_plans_tariff"], False)
		# Disable the only card plan: card_rate_plans empties and all_active_plans_tariff becomes True
		self.docs["Rate Plan", "BAR"].disabled = 1
		panel2 = self.panel()
		self.assertEqual(panel2["card_rate_plans"], [])
		self.assertIs(panel2["all_active_plans_tariff"], True)

	def test_derived_plans_appear_with_calculated_prices_and_can_be_primary(self):
		self.docs["Rate Plan", "CONTRACT"].update(rounding_step=0, rounding_mode="Nearest",
		                                          fx_lock_policy="Fixed at Check In")
		self.rows = [self.row("CONTRACT", label="Standard USD", room=150, single=120, extra=30,
		                      fx_rate=4084.953333, fx_date="2026-11-24")]
		self.rate_plan("HALF", rate_plan_name="Half Price USD (-50%)", base_rate_plan="CONTRACT",
		               modifier_type="Percent", modifier_value=-50, adjust_extra_person=0,
		               fx_lock_policy="Fixed at Check In", rounding_step=0, rounding_mode="Nearest")
		self.rate_plan("HALF-EXTRA", rate_plan_name="Half Incl. Extra", base_rate_plan="CONTRACT",
		               modifier_type="Percent", modifier_value=-50, adjust_extra_person=1,
		               fx_lock_policy="Fixed at Check In", rounding_step=1000, rounding_mode="Nearest")
		self.rate_plan("QTR", rate_plan_name="Quarter Price USD", base_rate_plan="HALF",
		               modifier_type="Percent", modifier_value=-50, adjust_extra_person=0,
		               fx_lock_policy="Fixed at Check In", rounding_step=0, rounding_mode="Nearest")
		self.rate_plan("OFF-DER", rate_plan_name="Disabled Derived", base_rate_plan="CONTRACT",
		               modifier_type="Percent", modifier_value=-20, disabled=1)

		panel = self.panel()
		by_plan = {r["rate_plan"]: r for r in panel["tariffs"]}
		self.assertEqual(set(by_plan), {"CONTRACT", "HALF", "HALF-EXTRA", "QTR"})
		# Base row untouched
		self.assertEqual((by_plan["CONTRACT"]["room"], by_plan["CONTRACT"]["local_room"]), (150, 612743.0))
		# Derived -50% (adjust_extra_person=0 -> extra keeps base 30.0)
		half = by_plan["HALF"]
		self.assertEqual((half["room"], half["single"], half["extra"]), (75.0, 60.0, 30.0))
		self.assertEqual((half["local_room"], half["local_single"]), (306371.0, 245097.0))
		self.assertEqual((half["derived_from"], half["derived_from_label"]), ("CONTRACT", "Standard USD"))
		self.assertEqual(half["fx_lock_policy"], "Fixed at Check In")
		# Derived -50% with adjust_extra_person=1 and 1,000 rounding step
		half_x = by_plan["HALF-EXTRA"]
		self.assertEqual((half_x["room"], half_x["single"], half_x["extra"]), (75.0, 60.0, 15.0))
		self.assertEqual(half_x["local_room"], 306000.0)
		# Multi-hop derived (QTR -> HALF -> CONTRACT): 75 * 0.5 = 37.5, single 30.0, extra 30.0
		qtr = by_plan["QTR"]
		self.assertEqual((qtr["room"], qtr["single"], qtr["extra"]), (37.5, 30.0, 30.0))
		self.assertEqual((qtr["derived_from"], qtr["derived_from_label"]), ("HALF", "Half Price USD (-50%)"))

		# When a derived plan is default, primary_rate_plan resolves to it.
		self.docs["Rate Plan", "CONTRACT"].is_default = 0
		self.docs["Rate Plan", "HALF"].is_default = 1
		self.assertEqual(self.panel()["primary_rate_plan"], "HALF")

		# When the base plan has no approved tariff row, derived plans are not fabricated.
		self.rows = []
		self.assertEqual(self.panel()["tariffs"], [])


class RoomTypesSummaryTest(RateMatrixHarness):
	modules = ("kamra.pricing", "kamra.authz", "kamra.rates")

	def extra_modules(self):
		from types import ModuleType
		folio = ModuleType("kamra.folio")
		folio._fnb_gst = lambda property: 5
		return {"kamra.folio": folio}

	def setUp(self):
		super().setUp()
		from unittest.mock import patch
		self.authz.restricted_properties = lambda user=None: None
		self.records["Room Type"] = [
			self.add("Room Type", "Test Hotel-JNR", property="Test Hotel", disabled=0, room_type_code="JNR",
			         room_type_name="Junior Suite", base_price=410619, base_occupancy=2, adults_capacity=3,
			         extra_adult_price=50000, child_price=20000),
			self.add("Room Type", "Test Hotel-EXE", property="Test Hotel", disabled=0, room_type_code="EXE",
			         room_type_name="Executive Suite", base_price=550000, base_occupancy=2, adults_capacity=3,
			         extra_adult_price=75000, child_price=20000),
			self.add("Room Type", "Test Hotel-PRS", property="Test Hotel", disabled=0, room_type_code="PRS",
			         room_type_name="Presidential Suite", base_price=900000, base_occupancy=2, adults_capacity=4,
			         extra_adult_price=100000, child_price=20000),
			self.add("Room Type", "Test Hotel-STD", property="Test Hotel", disabled=0, room_type_code="STD",
			         room_type_name="Standard", base_price=350000, base_occupancy=2, adults_capacity=2,
			         extra_adult_price=40000, child_price=20000),
		]
		self.records["Room"] = [
			*[self.add("Room", f"JNR-{i}", property="Test Hotel", room_type="Test Hotel-JNR") for i in range(1, 6)],
			*[self.add("Room", f"EXE-{i}", property="Test Hotel", room_type="Test Hotel-EXE") for i in range(1, 3)],
		]
		self.records["Rate Plan"] = []
		self.rate_plan("STD-USD", rate_plan_name="Standard USD", code="STD-USD", currency="USD", is_default=1,
		               custom_tariff_price_list="Tank Hill Contract USD", rounding_step=0, rounding_mode="Nearest")
		self.rate_plan("HALF-USD", rate_plan_name="Half Price USD (-50%)", code="HALF-USD", currency="USD",
		               base_rate_plan="STD-USD", modifier_type="Percent", modifier_value=-50,
		               rounding_step=0, rounding_mode="Nearest")
		# 150 USD x 4084.953333 = 612,743 UGX; 280 USD x 4084.953333 = 1,143,787 UGX
		def tariff_reply(form):
			if form["room_type"] == "Test Hotel-JNR":
				return self.tariff(nightly=150, fx_rate=4084.953333, agreed_rate=612743)
			if form["room_type"] == "Test Hotel-PRS":
				return self.tariff(nightly=280, fx_rate=4084.953333, agreed_rate=1143787)
			return {"is_tariff": 1, "refusal": "HSP-TARIFF-001: Set Room Type.custom_tariff_item."}

		self.tariffs["STD-USD"] = tariff_reply
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
			patcher = patch.object(self.pricing, name, value)
			patcher.start()
			self.addCleanup(patcher.stop)

	def rate_plan(self, name, **fields):
		doc = self.plan(name, **{"disabled": 0, "is_default": 0, **fields})
		self.records["Rate Plan"].append(doc)
		return doc

	def test_room_types_summary_returns_default_plan_selling_rates_room_counts_and_card_flag(self):
		summary = self.rates.room_types_summary("Test Hotel")
		self.assertEqual(summary["property_currency"], "UGX")
		self.assertIs(summary["has_card_rate_plans"], False)
		self.assertEqual(summary["default_plan"]["name"], "STD-USD")
		self.assertEqual(summary["default_plan"]["label"], "Standard USD")
		by_rt = {row["room_type"]: row for row in summary["room_types"]}
		self.assertEqual(by_rt["Test Hotel-JNR"]["room_count"], 5)
		self.assertIs(by_rt["Test Hotel-JNR"]["priced"], True)
		self.assertEqual(by_rt["Test Hotel-JNR"]["currency"], "USD")
		self.assertEqual(by_rt["Test Hotel-JNR"]["amount"], 150.0)
		self.assertEqual(by_rt["Test Hotel-JNR"]["local_amount"], 612743.0)
		self.assertEqual(by_rt["Test Hotel-JNR"]["source"], "contract")
		self.assertEqual(by_rt["Test Hotel-EXE"]["room_count"], 2)
		self.assertIs(by_rt["Test Hotel-EXE"]["priced"], False)
		self.assertIsNone(by_rt["Test Hotel-EXE"]["amount"])
		self.assertIn("HSP-TARIFF-001", by_rt["Test Hotel-EXE"]["refusal"])
		self.assertEqual(by_rt["Test Hotel-PRS"]["room_count"], 0)
		self.assertIs(by_rt["Test Hotel-PRS"]["priced"], True)
		self.assertEqual(by_rt["Test Hotel-STD"]["room_count"], 0)
		self.assertIs(by_rt["Test Hotel-STD"]["priced"], False)
		# Priced for 2 adults
		self.assertEqual({f["adults"] for f in self.tariff_forms()}, {2})

		# Adding a genuine card-priced active plan flips has_card_rate_plans to True
		self.rate_plan("BAR", rate_plan_name="Best Available")
		self.assertIs(self.rates.room_types_summary("Test Hotel")["has_card_rate_plans"], True)

	def test_room_types_summary_supports_derived_default_plan_and_property_scope(self):
		self.docs["Rate Plan", "STD-USD"].is_default = 0
		self.docs["Rate Plan", "HALF-USD"].is_default = 1
		summary = self.rates.room_types_summary("Test Hotel")
		self.assertIs(summary["has_card_rate_plans"], False)
		self.assertEqual(summary["default_plan"]["name"], "HALF-USD")
		jnr = next(row for row in summary["room_types"] if row["room_type"] == "Test Hotel-JNR")
		self.assertEqual(jnr["currency"], "USD")
		self.assertEqual(jnr["amount"], 75.0)
		self.assertEqual(jnr["local_amount"], 306371.0)

		self.authz.restricted_properties = lambda user=None: {"Sister Hotel"}
		with self.assertRaises(PermissionDenied):
			self.rates.room_types_summary("Test Hotel")

	def test_room_types_summary_counts_active_sius_without_double_counting_physical_rooms(self):
		# Hydrogen R8 P1: a whole-property Villa with 0 Room records and 1 active Sellable Unit is live inventory
		# (room_count == 1, never 0), physical rooms with synced individual SIUs are not double-counted, and
		# inactive SIUs are ignored.
		self.records["Room Type"].append(
			self.add("Room Type", "Test Hotel-VLA", property="Test Hotel", disabled=0, room_type_code="VLA",
			         room_type_name="Garden Villa", room_category="Villa", base_price=1200000,
			         base_occupancy=2, adults_capacity=6, extra_adult_price=100000, child_price=20000),
		)
		self.records["Sellable Unit"] = [
			self.add("Sellable Unit", "SIU-VLA-1", property="Test Hotel", room_type="Test Hotel-VLA",
			         unit_kind="whole_property", is_active=1),
			*[self.add("Sellable Unit", f"SIU-JNR-{i}", property="Test Hotel", room_type="Test Hotel-JNR",
			           unit_kind="individual", is_active=1) for i in range(1, 6)],
			self.add("Sellable Unit", "SIU-STD-OFF", property="Test Hotel", room_type="Test Hotel-STD",
			         unit_kind="individual", is_active=0),
		]
		summary = self.rates.room_types_summary("Test Hotel")
		by_rt = {row["room_type"]: row for row in summary["room_types"]}
		self.assertEqual(by_rt["Test Hotel-VLA"]["room_count"], 1)
		self.assertEqual(by_rt["Test Hotel-JNR"]["room_count"], 5)
		self.assertEqual(by_rt["Test Hotel-EXE"]["room_count"], 2)
		self.assertEqual(by_rt["Test Hotel-STD"]["room_count"], 0)

