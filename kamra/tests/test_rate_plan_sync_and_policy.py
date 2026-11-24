"""Site-free contracts for rate-plan-required resolution, Room Types pricing/tax
flags, and 4-way tariff agreement (Contract Tariffs <-> Room Types <-> Rate
Calendar <-> booking quote) across approved, scheduled, discarded and expired
tariff transitions."""

from __future__ import annotations

from types import ModuleType
from unittest.mock import patch

from kamra.tests.rate_matrix_harness import RateMatrixHarness, ValidationError

PROP = "Test Hotel"
STD = "Test Hotel-STD"
DLX = "Test Hotel-DLX"
PL_USD = "Tank Hill Contract USD"


class _MutableTariffBridge:
	"""In-memory stand-in for hospitality_tariffs supporting propose, approve,
	discard, list_tariffs, room_type_tariffs and tariff_rate across dates."""

	def __init__(self, fx_rate: float = 3650.0, fx_date: str = "2026-12-01"):
		self.fx_rate = fx_rate
		self.fx_date = fx_date
		self.versions: list[dict] = []
		self._seq = 0
		self.erp_tax_active = True

	def _next_name(self) -> str:
		self._seq += 1
		return f"IP-{self._seq:04d}"

	def add_version(self, *, room_type: str, item_code: str, rate: float,
	                valid_from: str, valid_upto: str | None = None,
	                workflow_state: str = "Approved", price_list: str = PL_USD,
	                currency: str = "USD", customer: str | None = None) -> dict:
		row = {
			"name": self._next_name(),
			"room_type": room_type,
			"item_code": item_code,
			"item_name": f"{room_type} (2 Adults)",
			"price_list": price_list,
			"currency": currency,
			"customer": customer,
			"rate": float(rate),
			"valid_from": valid_from,
			"valid_upto": valid_upto,
			"workflow_state": workflow_state,
			"category": "Room",
			"occupancy": "2 Adults",
		}
		self.versions.append(row)
		return row

	def approve(self, item_price: str) -> dict:
		for row in self.versions:
			if row["name"] == item_price:
				row["workflow_state"] = "Approved"
				return {"name": item_price, "workflow_state": "Approved"}
		raise ValidationError(f"Unknown draft {item_price}")

	def discard(self, item_price: str) -> dict:
		before = len(self.versions)
		self.versions = [r for r in self.versions if r["name"] != item_price]
		if len(self.versions) == before:
			raise ValidationError(f"Unknown draft {item_price}")
		return {"name": item_price, "discarded": True}

	def _active_for_date(self, room_type: str, day: str, price_list: str = PL_USD,
	                     customer: str | None = None) -> dict | None:
		hits = [
			r for r in self.versions
			if r["room_type"] == room_type
			and r["price_list"] == price_list
			and r["workflow_state"] == "Approved"
			and (r.get("customer") or None) == (customer or None)
			and r["valid_from"] <= day
			and (not r.get("valid_upto") or day <= r["valid_upto"])
		]
		if not hits:
			return None
		# Latest valid_from (and latest inserted for ties) wins, matching OPERA/SAP
		hits.sort(key=lambda r: (r["valid_from"], r["name"]))
		return hits[-1]

	def call(self, api_method: str, *, business_date: str, room_type_names: dict[str, str],
	         rate_plan_pl: dict[str, str], **form):
		if api_method == "hospitality_tariffs.list_tariffs":
			as_of = form.get("as_of_date") or business_date
			items = []
			for rt, rt_label in room_type_names.items():
				rt_versions = [v for v in self.versions if v["room_type"] == rt]
				if not rt_versions:
					continue
				active = self._active_for_date(rt, as_of)
				pending = [dict(v) for v in rt_versions if v["workflow_state"] == "Draft"]
				history = [dict(v) for v in rt_versions if v["workflow_state"] != "Draft"]
				items.append({
					"item_code": rt_versions[0]["item_code"],
					"item_name": rt_versions[0]["item_name"],
					"category": "Room",
					"room_type": rt_label,
					"occupancy": "2 Adults",
					"price_list": PL_USD,
					"currency": "USD",
					"customer": None,
					"fx_rate": self.fx_rate,
					"active": dict(active) if active else None,
					"pending": pending,
					"history": history,
				})
			return {
				"supported": True,
				"property": form.get("property", PROP),
				"property_currency": "UGX",
				"business_date": business_date,
				"can_propose": True,
				"can_approve": True,
				"price_lists": [{
					"name": PL_USD, "currency": "USD", "fx_rate": self.fx_rate,
					"fx_date": self.fx_date, "rate_plans": ["STD-USD"],
				}],
				"items": items,
			}

		if api_method == "hospitality_tariffs.room_type_tariffs":
			rt = form["room_type"]
			active = self._active_for_date(rt, business_date)
			tariffs = []
			if active:
				tariffs.append({
					"rate_plan": "STD-USD",
					"label": "Standard USD",
					"currency": "USD",
					"room": active["rate"],
					"single": None,
					"extra": None,
					"approval_state": "Approved",
					"valid_from": active["valid_from"],
					"valid_upto": active["valid_upto"],
					"fx_rate": self.fx_rate,
					"fx_date": self.fx_date,
				})
			return {"tariffs": tariffs, "all_active_plans_tariff": True}

		if api_method == "hospitality_tariffs.tariff_rate":
			rp = form["rate_plan"]
			pl = rate_plan_pl.get(rp)
			if not pl:
				return {"is_tariff": 0}
			rt = form["room_type"]
			ci, co = form["check_in"], form["check_out"]
			from datetime import date, timedelta
			start = date.fromisoformat(ci[:10])
			end = date.fromisoformat(co[:10])
			span = max((end - start).days, 1)
			nights = []
			for i in range(span):
				day = str(start + timedelta(days=i))
				hit = self._active_for_date(rt, day, price_list=pl)
				if not hit:
					return {
						"is_tariff": 1,
						"refusal": f"No approved tariff for {rt} on {day}.",
					}
				nights.append({
					"date": day,
					"nightly": hit["rate"],
					"agreed_rate": round(hit["rate"] * self.fx_rate, 2),
					"item_price": hit["name"],
				})
			return {
				"is_tariff": 1,
				"currency": "USD",
				"nightly": nights[0]["nightly"],
				"agreed_rate": nights[0]["agreed_rate"],
				"fx_rate": self.fx_rate,
				"fx_date": self.fx_date,
				"nights": nights,
			}

		if api_method == "hospitality_tariffs.approve_tariff":
			return self.approve(form["item_price"])
		if api_method == "hospitality_tariffs.discard_tariff":
			return self.discard(form["item_price"])
		if api_method == "hospitality_tariffs.propose_tariff":
			return self.add_version(
				room_type=STD,
				item_code=form["item_code"],
				rate=form["price_list_rate"],
				valid_from=form["valid_from"],
				valid_upto=form.get("valid_upto"),
				workflow_state="Draft",
				price_list=form["price_list"],
				customer=form.get("customer"),
			)
		return None


class RatePlanSyncAndPolicyTest(RateMatrixHarness):
	modules = ("kamra.pricing", "kamra.authz", "kamra.rates")

	def extra_modules(self):
		folio = ModuleType("kamra.folio")
		folio._fnb_gst = lambda property: 5
		settings_mod = ModuleType("kamra.settings")
		settings_mod.get_setting = lambda prop, key: self.settings_store.get((prop, key), 0)
		return {"kamra.folio": folio, "kamra.settings": settings_mod}

	def setUp(self):
		self.settings_store: dict[tuple[str, str], int] = {(PROP, "rate_plan_required"): 1}
		self.mutable_bridge = _MutableTariffBridge(fx_rate=3650.0, fx_date="2026-12-01")
		super().setUp()
		import sys
		sys.modules["kamra.erp_bridge"].erp_determines_tax = lambda: self.mutable_bridge.erp_tax_active
		self.authz.restricted_properties = lambda user=None: None
		self.frappe.has_permission = lambda *a, **kw: True
		self.records["Room Type"] = [
			self.add("Room Type", STD, property=PROP, disabled=0, room_type_code="STD",
			         room_type_name="Standard Room", base_price=400000, base_occupancy=2,
			         adults_capacity=2, extra_adult_price=50000, child_price=20000,
			         check_permission=lambda *a: None),
			self.add("Room Type", DLX, property=PROP, disabled=0, room_type_code="DLX",
			         room_type_name="Deluxe Suite", base_price=600000, base_occupancy=2,
			         adults_capacity=2, extra_adult_price=75000, child_price=20000,
			         check_permission=lambda *a: None),
		]
		self.records["Room"] = [
			self.add("Room", "STD-101", property=PROP, room_type=STD),
			self.add("Room", "DLX-201", property=PROP, room_type=DLX),
		]
		self.records["Rate Plan"] = []
		self.rate_plan(
			"STD-USD", rate_plan_name="Standard USD", code="STD-USD", currency="USD",
			is_default=1, custom_tariff_price_list=PL_USD, rounding_step=1000,
			rounding_mode="Nearest", fx_lock_policy="Fixed at Reservation",
		)
		self.rate_plan(
			"CORP-USD", rate_plan_name="Corporate USD", code="CORP-USD", currency="USD",
			base_rate_plan="STD-USD", modifier_type="Percent", modifier_value=-10,
			rounding_step=1000, rounding_mode="Nearest",
		)
		self.rate_plan(
			"COMP", rate_plan_name="Complimentary", code="COMP",
			modifier_type="Absolute", modifier_value=0, rate_category="Complimentary",
		)
		self.rate_plan(
			"HOUSE", rate_plan_name="House Use", code="HOUSE",
			modifier_type="Absolute", modifier_value=0, rate_category="House Use",
		)
		self.rate_plan(
			"OLD", rate_plan_name="Retired Plan", code="OLD", disabled=1,
		)
		self.add("Corporate Account", "ACME", company_name="Acme Corp", negotiated_rate_plan="CORP-USD")

		rules = {
			"no_small_group": False, "minimum_nights": 0, "season": None, "non_refundable": False,
			"full_payment": False, "is_holiday": False, "holiday": None, "advance_percent": 0,
			"advance_season": None,
		}
		for name, value in (
			("small_group_allows_stay", lambda *args: True),
			("stay_season_rules", lambda *args, **kw: rules),
			("long_stay_rule", lambda *args: None),
			("active_holiday_package", lambda *args: None),
			("season_adjust", lambda property, date, base, room_type=None: base),
			("forecast_occupancy", lambda *args, **kw: 0),
			("demand_adjust", lambda *args, **kw: (args[3], None)),
			("advance_percent", lambda *args: (0.0, None)),
			("active_season", lambda *args, **kw: None),
			("package_covering_date", lambda *args, **kw: None),
		):
			patcher = patch.object(self.pricing, name, value)
			patcher.start()
			self.addCleanup(patcher.stop)

	def rate_plan(self, name, **fields):
		doc = self.plan(name, **{"disabled": 0, "is_default": 0, **fields})
		self.records["Rate Plan"].append(doc)
		return doc

	def bridge_call(self, api_method, *, response_types=None, **form):
		self.bridge_calls.append((api_method, dict(form)))
		bdate = str(self.docs["Property", PROP].business_date)
		rt_names = {r.name: r.room_type_name for r in self.records["Room Type"]}
		rp_pl = {
			r.name: r.get("custom_tariff_price_list")
			for r in self.records["Rate Plan"]
			if r.get("custom_tariff_price_list")
		}
		return self.mutable_bridge.call(
			api_method, business_date=bdate, room_type_names=rt_names, rate_plan_pl=rp_pl, **form,
		)

	# ── Requirement 1: precedence + plain actionable refusal + comp/house use ──

	def test_resolve_booking_rate_plan_precedence_and_refusals(self):
		self.mutable_bridge.add_version(
			room_type=STD, item_code="ROOM-STD", rate=100,
			valid_from="2026-11-01", valid_upto="2026-12-31",
		)
		# 1. Explicit chosen plan beats company negotiated and default
		self.assertEqual(
			self.pricing.resolve_booking_rate_plan(
				PROP, STD, "2026-12-01", "2026-12-02", rate_plan="STD-USD", company="ACME",
			),
			"STD-USD",
		)
		# 2. Company negotiated plan beats property default
		self.assertEqual(
			self.pricing.resolve_booking_rate_plan(
				PROP, STD, "2026-12-01", "2026-12-02", company="ACME",
			),
			"CORP-USD",
		)
		# 3. Property default plan applies when neither chosen nor negotiated
		self.assertEqual(
			self.pricing.resolve_booking_rate_plan(
				PROP, STD, "2026-12-01", "2026-12-02",
			),
			"STD-USD",
		)
		# 4. Complimentary & House Use 0-rate plans resolve cleanly (compatible with #38)
		for zero_plan in ("COMP", "HOUSE"):
			resolved = self.pricing.resolve_booking_rate_plan(
				PROP, STD, "2026-12-01", "2026-12-02", rate_plan=zero_plan,
			)
			self.assertEqual(resolved, zero_plan)
			q = self.pricing.quote_with_contract(PROP, STD, "2026-12-01", "2026-12-02", rate_plan=resolved)
			self.assertEqual(q["room_total"], 0)
			self.assertEqual(q["amount_after_tax"], 0)

		# 5. Missing tariff for room type (DLX has no tariff) refuses with plain setup guidance
		with self.assertRaises(ValidationError) as ctx:
			self.pricing.resolve_booking_rate_plan(PROP, DLX, "2026-12-01", "2026-12-02")
		msg = str(ctx.exception)
		self.assertIn("Contract Tariffs", msg)
		self.assertIn("Deluxe Suite", msg)
		self.assertIn("Standard USD", msg)

		# 6. Disabled plan is refused with plain setup guidance
		with self.assertRaises(ValidationError) as ctx:
			self.pricing.resolve_booking_rate_plan(PROP, STD, "2026-12-01", "2026-12-02", rate_plan="OLD")
		self.assertIn("Rate Plans", str(ctx.exception))

		# 7. No default plan + none chosen refuses with KMR-RATE-REQ-001 naming Rate Plans
		self.docs["Rate Plan", "STD-USD"].is_default = 0
		for row in self.records["Rate Plan"]:
			if row.name == "STD-USD":
				row.is_default = 0
		with self.assertRaises(ValidationError) as ctx:
			self.pricing.resolve_booking_rate_plan(PROP, STD, "2026-12-01", "2026-12-02")
		self.assertIn("KMR-RATE-REQ-001", str(ctx.exception))
		self.assertIn("Rate Plans", str(ctx.exception))

		# 8. Legacy property (rate_plan_required = 0) allows booking without a rate plan
		self.settings_store[(PROP, "rate_plan_required")] = 0
		self.assertIsNone(self.pricing.resolve_booking_rate_plan(PROP, STD, "2026-12-01", "2026-12-02"))

	# ── Requirement 3: Room Types summary/panel/calendar policy & ERP tax flags ──

	def test_room_types_and_calendar_expose_policy_and_erp_tax_flags(self):
		self.mutable_bridge.add_version(
			room_type=STD, item_code="ROOM-STD", rate=100,
			valid_from="2026-11-01", valid_upto="2026-12-31",
		)
		summary = self.rates.room_types_summary(PROP)
		self.assertIs(summary["rate_plan_required"], True)
		self.assertIs(summary["erp_tax_managed"], True)

		panel = self.rates.room_type_tariffs(STD)
		self.assertIs(panel["rate_plan_required"], True)

		cal = self.rates.rate_calendar(PROP, from_date="2026-12-01", days=2)
		self.assertIs(cal["rate_plan_required"], True)
		self.assertEqual(cal["rate_plan"]["name"], "STD-USD")

		# Standalone site (no ERP tax bridge) -> erp_tax_managed False
		self.mutable_bridge.erp_tax_active = False
		self.assertIs(self.rates.room_types_summary(PROP)["erp_tax_managed"], False)

		# When rate_plan_required is ON and there is no default plan, Room Types never falls back to card rate
		self.docs["Rate Plan", "STD-USD"].is_default = 0
		for row in self.records["Rate Plan"]:
			if row.name == "STD-USD":
				row.is_default = 0
		no_default_summary = self.rates.room_types_summary(PROP)
		by_rt = {r["room_type"]: r for r in no_default_summary["room_types"]}
		self.assertIs(by_rt[STD]["priced"], False)
		self.assertIsNone(by_rt[STD]["amount"])

	# ── Requirement 4: ONE tariff walked through all 4 surfaces & all 4 states ──

	def test_single_tariff_lifecycle_agrees_across_all_four_surfaces(self):
		# Baseline: approved USD 100 from 2026-11-01 to 2026-12-10 (100 * 3650 = 365,000 UGX)
		self.mutable_bridge.add_version(
			room_type=STD, item_code="ROOM-STD", rate=100,
			valid_from="2026-11-01", valid_upto="2026-12-10",
		)

		def read_all_four(day: str = "2026-12-01"):
			self.docs["Property", PROP].business_date = day
			tariffs_dir = self.rates.list_contract_tariffs(PROP, as_of_date=day)
			summary = self.rates.room_types_summary(PROP)
			panel = self.rates.room_type_tariffs(STD)
			cal = self.rates.rate_calendar(PROP, from_date="2026-12-01", days=6)
			return tariffs_dir, summary, panel, cal

		def std_summary(summary):
			return next(r for r in summary["room_types"] if r["room_type"] == STD)

		def cal_by_date(cal):
			row = next(r for r in cal["room_types"] if r["room_type"] == STD)
			return {n["date"]: n for n in row["nights"]}

		# 1. DRAFT PROPOSED ($140) -> DISCARDED: never leaks into any of the 4 surfaces
		draft_discard = self.rates.propose_contract_tariff(
			PROP, PL_USD, "ROOM-STD", 140, "2026-12-01", "2026-12-05",
		)
		t_dir, summary, panel, cal = read_all_four("2026-12-01")
		self.assertEqual(t_dir["items"][0]["active"]["rate"], 100)
		self.assertEqual(len(t_dir["items"][0]["pending"]), 1)
		self.assertEqual(std_summary(summary)["amount"], 100)
		self.assertEqual(std_summary(summary)["local_amount"], 365000)
		self.assertEqual(panel["tariffs"][0]["room"], 100)
		self.assertEqual(cal_by_date(cal)["2026-12-01"]["rate"], 365000)
		q = self.pricing.quote_with_contract(PROP, STD, "2026-12-01", "2026-12-02", rate_plan="STD-USD")
		self.assertEqual((q["tariff"]["nightly"], q["agreed_rate"]), (100.0, 365000.0))

		self.rates.discard_contract_tariff(draft_discard["name"])
		t_dir, summary, panel, cal = read_all_four("2026-12-01")
		self.assertEqual(t_dir["items"][0]["pending"], [])
		self.assertEqual(t_dir["items"][0]["active"]["rate"], 100)
		self.assertEqual(std_summary(summary)["amount"], 100)
		self.assertEqual(panel["tariffs"][0]["room"], 100)
		self.assertEqual(cal_by_date(cal)["2026-12-01"]["rate"], 365000)
		q = self.pricing.quote_with_contract(PROP, STD, "2026-12-01", "2026-12-02", rate_plan="STD-USD")
		self.assertEqual((q["tariff"]["nightly"], q["agreed_rate"]), (100.0, 365000.0))

		# 2. APPROVED effective today ($120 from 2026-12-01..2026-12-05; 120 * 3650 = 438,000 UGX)
		# Expire baseline on 2026-11-30 so the new approved version is the only version for Dec
		self.mutable_bridge.versions[0]["valid_upto"] = "2026-11-30"
		draft_now = self.rates.propose_contract_tariff(
			PROP, PL_USD, "ROOM-STD", 120, "2026-12-01", "2026-12-05",
		)
		self.rates.approve_contract_tariff(draft_now["name"])
		t_dir, summary, panel, cal = read_all_four("2026-12-01")
		self.assertEqual(t_dir["items"][0]["active"]["rate"], 120)
		self.assertEqual((std_summary(summary)["amount"], std_summary(summary)["local_amount"]), (120, 438000))
		self.assertEqual((panel["tariffs"][0]["room"], panel["tariffs"][0]["local_room"]), (120, 438000))
		self.assertEqual(cal_by_date(cal)["2026-12-01"]["rate"], 438000)
		q_now = self.pricing.quote_with_contract(PROP, STD, "2026-12-01", "2026-12-02", rate_plan="STD-USD")
		self.assertEqual((q_now["tariff"]["nightly"], q_now["agreed_rate"]), (120.0, 438000.0))

		# 3. SCHEDULED future tariff ($150 approved from 2026-12-03..2026-12-05; 150 * 3650 = 547,500 -> 548,000 UGX)
		draft_future = self.rates.propose_contract_tariff(
			PROP, PL_USD, "ROOM-STD", 150, "2026-12-03", "2026-12-05",
		)
		self.rates.approve_contract_tariff(draft_future["name"])
		t_dir, summary, panel, cal = read_all_four("2026-12-01")
		# Today (2026-12-01) stays $120 in Contract Tariffs, Room Types, Rate Calendar & today's quote
		self.assertEqual(t_dir["items"][0]["active"]["rate"], 120)
		scheduled = [v for v in t_dir["items"][0]["history"] if v["valid_from"] > "2026-12-01"]
		self.assertEqual([v["rate"] for v in scheduled], [150])
		self.assertEqual((std_summary(summary)["amount"], std_summary(summary)["local_amount"]), (120, 438000))
		self.assertEqual(panel["tariffs"][0]["room"], 120)
		nights = cal_by_date(cal)
		self.assertEqual(nights["2026-12-01"]["rate"], 438000)
		self.assertEqual(nights["2026-12-02"]["rate"], 438000)
		self.assertEqual(nights["2026-12-03"]["rate"], 548000)
		self.assertEqual(nights["2026-12-05"]["rate"], 548000)
		# Booking quotes match calendar per stay date (including split stay over boundary)
		q_future = self.pricing.quote_with_contract(PROP, STD, "2026-12-03", "2026-12-04", rate_plan="STD-USD")
		self.assertEqual((q_future["tariff"]["nightly"], q_future["agreed_rate"]), (150.0, 548000.0))
		q_split = self.pricing.quote_with_contract(PROP, STD, "2026-12-02", "2026-12-04", rate_plan="STD-USD")
		self.assertEqual(
			[(n["date"], n["agreed_rate"]) for n in q_split["tariff"]["nights"]],
			[("2026-12-02", 438000.0), ("2026-12-03", 548000.0)],
		)

		# 4. EXPIRED (valid_upto = 2026-12-05 -> checked on 2026-12-06): all 4 agree no rate is available
		t_dir_exp, summary_exp, panel_exp, cal_exp = read_all_four("2026-12-06")
		self.assertIsNone(t_dir_exp["items"][0]["active"])
		self.assertIs(std_summary(summary_exp)["priced"], False)
		self.assertEqual(panel_exp["tariffs"], [])
		self.assertIs(cal_by_date(cal_exp)["2026-12-06"]["available"], False)
		self.assertIsNone(cal_by_date(cal_exp)["2026-12-06"]["rate"])
		with self.assertRaises(ValidationError) as ctx:
			self.pricing.resolve_booking_rate_plan(PROP, STD, "2026-12-06", "2026-12-07")
		self.assertIn("Contract Tariffs", str(ctx.exception))
