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

