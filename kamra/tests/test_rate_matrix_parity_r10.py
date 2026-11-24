"""Round 10 of the rate matrix: Hydrogen's R6 paired live verification on kamra#33 + siteops#8559.

Root cause: price-determination inputs must come from ONE authoritative list
(`pricing.PRICE_DETERMINATION_FIELDS` / `PRICE_DETERMINATION_GUARDED_FIELDS`),
checked by AST against Reservation.quote_inputs, hand_set_prices,
capture_contract_tariff, apply_pricing, keep_hand_set_nights,
apply_negotiated_rate_plan, pricing.billable_children, contract_night_rates,
contract_tariff_for_new_dates and relock_contract_fx_at_check_in so pricing
and HSP-TARIFF-007 cannot drift apart.

Also closes:
- P1-1: freeze the booked pricing company at the top level of contract_tariff
  (without changing contract_tariff["tariff"]) and backfill legacy snapshots on
  change_stay_company so "The rate stays as booked" holds across extensions
  (OPERA Cloud Managing Reservation Linked Profiles).
- P2-1: guard update_occupants when occupant ages change billable_children
  (requires Revenue Manager / Hotel Admin + confirm_price=1 when total changes;
  preserves Front Desk check-in guest registration when billable_children is
  unchanged).
- P2-2: pin safe degradation when Reservation.fixed_rate (sibling kamra#32) is
  absent from the document.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

from kamra.tests import test_rate_matrix_parity_r7 as r7
from kamra.tests.rate_matrix_harness import Doc, RateMatrixHarness
from kamra.tests.test_rate_matrix_parity_r6 import QuotePatches, stub_module

ROOT = Path(__file__).resolve().parents[2]
RESERVATION_PY = ROOT / "kamra/kamra/doctype/reservation/reservation.py"
RESERVATION_JSON = ROOT / "kamra/kamra/doctype/reservation/reservation.json"
PRICING_PY = ROOT / "kamra/pricing.py"

RESERVATION_PRICING_METHODS = (
	"quote_inputs",
	"hand_set_prices",
	"capture_contract_tariff",
	"apply_pricing",
	"keep_hand_set_nights",
	"apply_negotiated_rate_plan",
)
PRICING_RESERVATION_FUNCS = (
	"billable_children",
	"contract_night_rates",
	"contract_tariff_for_new_dates",
	"relock_contract_fx_at_check_in",
)
NON_INPUT_RESERVATION_ATTRS = frozenset({
	# Methods, document bookkeeping, derived outputs and non-pricing metadata.
	"is_new", "get_doc_before_save", "has_value_changed", "flags", "get", "name",
	"rate_lock_key", "rate_lock_key_for", "quote_inputs", "hand_set_prices",
	"reprice_contract_nights", "requote", "apply_quote", "_apply_commission",
	"night_discount_inputs", "discount_changes_only", "apply_rediscount",
	"rediscount_locked_nights", "custom_tariff_currency",
})
# kamra#32 is merged: every field in PRICE_DETERMINATION_FIELDS is now a real Reservation DocField.
FORWARD_COMPAT_SIBLING_FIELDS = frozenset()


def extract_pricing_reservation_fields(reservation_src: str, pricing_src: str) -> set[str]:
	"""AST-extract every Reservation field read by Kamra's price-determination functions."""
	found: set[str] = set()

	def scan(node: ast.AST, var_names: tuple[str, ...]) -> None:
		for sub in ast.walk(node):
			if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id in var_names:
				if sub.attr not in NON_INPUT_RESERVATION_ATTRS:
					found.add(sub.attr)
			elif (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "get"
			      and isinstance(sub.func.value, ast.Name) and sub.func.value.id in var_names
			      and sub.args and isinstance(sub.args[0], ast.Constant)
			      and isinstance(sub.args[0].value, str)):
				if sub.args[0].value not in NON_INPUT_RESERVATION_ATTRS:
					found.add(sub.args[0].value)

	res_tree = ast.parse(reservation_src)
	for node in ast.walk(res_tree):
		if isinstance(node, ast.FunctionDef) and node.name in RESERVATION_PRICING_METHODS:
			scan(node, ("self",))

	pri_tree = ast.parse(pricing_src)
	for node in ast.walk(pri_tree):
		if isinstance(node, ast.FunctionDef) and node.name in PRICING_RESERVATION_FUNCS:
			scan(node, ("reservation",))

	return found


class PriceDeterminationParityTest(RateMatrixHarness):
	"""Single authoritative list of Reservation price-determination inputs cannot drift from pricing code."""

	modules = ("kamra.pricing",)

	def test_pricing_code_inputs_match_authoritative_registry_and_catch_drift(self):
		expected_guarded = (
			("property", "str"),
			("room_type", "str"),
			("rate_plan", "str"),
			("company", "str"),
			("group_booking", "str"),
			("meal_plan", "str"),
			("voucher", "str"),
			("adults", "int"),
			("children", "int"),
			("infants", "int"),
			("extra_beds", "int"),
			("pets", "int"),
			("is_day_use", "int"),
			("fixed_rate", "int"),
			("auto_price", "int"),
			("agreed_rate", "flt"),
			("over_capacity_charge", "flt"),
			("discount_type", "str"),
			("discount_value", "flt"),
			("planned_check_in_time", "day_use_time"),
			("planned_check_out_time", "day_use_time"),
			("occupants", "occupant_ages"),
			("night_rates", "night_rates"),
		)
		self.assertEqual(getattr(self.pricing, "PRICE_DETERMINATION_GUARDED_FIELDS", None), expected_guarded)
		self.assertEqual(getattr(self.pricing, "PRICE_DETERMINATION_DATE_FIELDS", None),
		                 ("check_in_date", "check_out_date"))
		self.assertEqual(getattr(self.pricing, "PRICE_DETERMINATION_SNAPSHOT_FIELDS", None), ("contract_tariff",))

		res_src = RESERVATION_PY.read_text(encoding="utf-8")
		pri_src = PRICING_PY.read_text(encoding="utf-8")
		extracted = extract_pricing_reservation_fields(res_src, pri_src)
		self.assertEqual(extracted, self.pricing.PRICE_DETERMINATION_FIELDS)

		# Every field in the registry is either a real Reservation DocField or a documented sibling-PR field.
		schema = json.loads(RESERVATION_JSON.read_text(encoding="utf-8"))
		docfields = {f["fieldname"] for f in schema["fields"]
		             if f["fieldtype"] not in ("Section Break", "Column Break", "Tab Break")}
		self.assertEqual(self.pricing.PRICE_DETERMINATION_FIELDS - docfields, FORWARD_COMPAT_SIBLING_FIELDS)

		# Every Reservation-sourced parameter of pricing.quote() maps into PRICE_DETERMINATION_FIELDS.
		quote_params = set(inspect.signature(self.pricing.quote).parameters) - {
			"check_voucher_limits", "exclude_reservation",
		}
		param_to_field = {
			"voucher_code": "voucher",
			"check_in_time": "planned_check_in_time",
			"check_out_time": "planned_check_out_time",
			"night_prices": "night_rates",
			"night_discounts": "night_rates",
			"locked_nights": "night_rates",
			"agreed_nights": "contract_tariff",
		}
		mapped_quote_fields = {param_to_field.get(p, p) for p in quote_params}
		self.assertTrue(mapped_quote_fields <= self.pricing.PRICE_DETERMINATION_FIELDS,
		                mapped_quote_fields - self.pricing.PRICE_DETERMINATION_FIELDS)

		# Self-test: adding a new pricing input inside quote_inputs without registering it MUST fail parity.
		mutated_res = res_src.replace(
			'"over_capacity_charge": float(self.get("over_capacity_charge") or 0),',
			'"over_capacity_charge": float(self.get("over_capacity_charge") or 0),\n'
			'\t\t\t"resort_tier": self.get("resort_tier"),',
			1,
		)
		self.assertNotEqual(mutated_res, res_src)
		self.assertEqual(
			extract_pricing_reservation_fields(mutated_res, pri_src) - self.pricing.PRICE_DETERMINATION_FIELDS,
			{"resort_tier"},
		)


class ChangeStayCompanyContractTest(QuotePatches, RateMatrixHarness):
	"""P1-1: change_stay_company keeps 'The rate stays as booked' across extensions and check-in relock."""

	modules = ("kamra.pricing", "kamra.kamra.doctype.reservation.reservation", "kamra.authz", "kamra.api")

	def extra_modules(self):
		return {
			"kamra.folio": stub_module(
				"kamra.folio",
				DIRECT_BILL="Company Credit",
				_company_credit=lambda company: False,
				_fnb_gst=lambda property: 5,
				charged_nights=lambda name: {},
				_company_pays=lambda *args: False,
				open_company_folio=lambda res: "FOL-COMP",
				transfer_charges=lambda *args: None,
				void_pairs=lambda *args: set(),
			),
			"kamra.credit": stub_module(
				"kamra.credit",
				credit_applies=lambda company: False,
				enforce_company_credit=lambda *args, **kwargs: None,
				_is_agent=lambda: False,
			),
			"kamra.savings": stub_module("kamra.savings", log_action=lambda *args, **kwargs: None),
		}

	def setUp(self):
		super().setUp()
		self.roles = ["Front Desk"]
		self.authz.restricted_properties = lambda user=None: None
		orig_exists = self.frappe.db.exists

		def exists(doctype, name=None):
			if isinstance(name, dict):
				return any(
					dt == doctype and all(d.get(k) == v for k, v in name.items())
					for (dt, _n), d in self.docs.items()
				)
			return orig_exists(doctype, name)

		self.frappe.db.exists = exists
		self.add("Room Type", "Test Hotel-DLX", property="Test Hotel", base_price=100000, base_occupancy=2,
		         adults_capacity=4, children_capacity=4, free_child_age=6)
		self.add("Corporate Account", "Corp Negotiated", company_name="Corp Negotiated", disabled=0)
		self.plan("CON-RES")
		self.plan("CON-CHK", fx_lock_policy="Fixed at Check In")
		self.patch_quote()
		self.tariffs["CON-RES"] = self.resolver
		self.tariffs["CON-CHK"] = self.resolver

	def resolver(self, form):
		nights = []
		corp = form.get("company") == "Corp Negotiated"
		for day in r7.stay_nights(form["check_in"], form["check_out"]):
			nightly = 60.0 if corp else (200.0 if day >= "2026-11-27" else 130.0)
			agreed = nightly * 3650.0
			nights.append({"date": day, "nightly": nightly, "agreed_rate": agreed,
			               "item_price": "IP-CORP" if corp else ("IP-PEAK" if day >= "2026-11-27" else "IP-BASE")})
		return {
			"is_tariff": 1, "price_list": "Contract USD", "currency": "USD", "property_currency": "UGX",
			"nightly": nights[0]["nightly"], "agreed_rate": nights[0]["agreed_rate"],
			"fx_rate": 3650.0, "fx_date": form.get("fx_date") or "2026-11-20", "nights": nights,
		}

	def make_res(self, *, is_new=False, **fields):
		values = dict(
			name="RES-COMP", property="Test Hotel", room_type="Test Hotel-DLX", rate_plan="CON-RES",
			check_in_date="2026-11-24", check_out_date="2026-11-26", adults=2, children=0, infants=0,
			extra_beds=0, occupants=[], auto_price=1, agreed_rate=0, meal_plan=None, company=None,
			booking_type="Individual", group_booking=None, status="Confirmed", amount_after_tax=949000.0,
			night_rates=[Doc(stay_date="2026-11-24", rate=474500.0, gst_rate=0),
			             Doc(stay_date="2026-11-25", rate=474500.0, gst_rate=0)],
		)
		values.update(fields)
		before = Doc(values)
		doc = self.reservation.Reservation({**values, "flags": Doc()})
		doc.is_new = lambda: is_new
		doc.get_doc_before_save = lambda: None if is_new else before

		def db_set(updates, *args, **kwargs):
			if isinstance(updates, dict):
				doc.update(updates)
				before.update(updates)

		doc.db_set = db_set
		self.docs["Reservation", doc.name] = doc
		return doc

	def test_capture_contract_tariff_freezes_top_level_company_without_polluting_tariff_dict(self):
		# Public stay freezes company=None at top level of contract_tariff; tariff dict keeps its exact keys.
		pub = self.make_res(is_new=True, company=None)
		pub.capture_contract_tariff()
		self.assertIn("company", pub.contract_tariff)
		self.assertIsNone(pub.contract_tariff["company"])
		self.assertNotIn("company", pub.contract_tariff["tariff"])

		# Corporate stay freezes company="Corp Negotiated" at top level.
		corp = self.make_res(is_new=True, company="Corp Negotiated")
		corp.capture_contract_tariff()
		self.assertEqual(corp.contract_tariff["company"], "Corp Negotiated")
		self.assertNotIn("company", corp.contract_tariff["tariff"])
		self.assertEqual(corp.agreed_rate, 219000.0)

	def test_change_stay_company_preserves_booked_rate_on_extensions_for_new_and_legacy_stays(self):
		booked_nights = [
			{"date": "2026-11-24", "nightly": 130.0, "agreed_rate": 474500.0, "item_price": "IP-BASE"},
			{"date": "2026-11-25", "nightly": 130.0, "agreed_rate": 474500.0, "item_price": "IP-BASE"},
		]
		base_tariff = {"currency": "USD", "nightly": 130.0, "fx_rate": 3650.0,
		               "fx_date": "2026-11-20", "property_currency": "UGX", "nights": booked_nights}
		for label, snap in (
			("frozen_at_booking", {"agreed_rate": 474500.0, "company": None, "tariff": base_tariff}),
			("legacy_without_company_key", {"agreed_rate": 474500.0, "tariff": base_tariff}),
		):
			with self.subTest(label):
				self.bridge_calls.clear()
				doc = self.make_res(agreed_rate=474500.0, contract_tariff=json.dumps(snap))
				self.api.change_stay_company(doc.name, "Company paying bill", company="Corp Negotiated")
				self.assertEqual(doc.company, "Corp Negotiated")
				# Extending into 2026-11-26..28 must price at booked public tariff ($130 / $200), NOT corporate $60.
				pv = self.api.amend_stay_preview(doc.name, "2026-11-24", "2026-11-28")
				self.assertNotIn("company", self.tariff_forms()[-1])
				self.assertEqual([(n["date"], n["rate"]) for n in pv["nights"]], [
					("2026-11-24", 474500.0), ("2026-11-25", 474500.0),
					("2026-11-26", 474500.0), ("2026-11-27", 730000.0),
				])
				doc.update(check_out_date="2026-11-28")
				doc.capture_contract_tariff()
				self.assertEqual([n["agreed_rate"] for n in doc.contract_tariff["tariff"]["nights"]],
				                 [474500.0, 474500.0, 474500.0, 730000.0])

	def test_corporate_stay_cleared_to_guest_keeps_booked_corporate_rate_on_extension(self):
		corp_nights = [
			{"date": "2026-11-24", "nightly": 60.0, "agreed_rate": 219000.0, "item_price": "IP-CORP"},
			{"date": "2026-11-25", "nightly": 60.0, "agreed_rate": 219000.0, "item_price": "IP-CORP"},
		]
		snap = {"agreed_rate": 219000.0, "company": "Corp Negotiated",
		        "tariff": {"currency": "USD", "nightly": 60.0, "fx_rate": 3650.0, "fx_date": "2026-11-20",
		                   "property_currency": "UGX", "nights": corp_nights}}
		doc = self.make_res(company="Corp Negotiated", booking_type="Corporate",
		                    agreed_rate=219000.0, amount_after_tax=438000.0, contract_tariff=json.dumps(snap),
		                    night_rates=[Doc(stay_date="2026-11-24", rate=219000.0, gst_rate=0),
		                                 Doc(stay_date="2026-11-25", rate=219000.0, gst_rate=0)])
		self.api.change_stay_company(doc.name, "Guest paying personally", company=None)
		self.assertIsNone(doc.company)
		pv = self.api.amend_stay_preview(doc.name, "2026-11-24", "2026-11-28")
		self.assertEqual(self.tariff_forms()[-1].get("company"), "Corp Negotiated")
		self.assertEqual([n["rate"] for n in pv["nights"]], [219000.0, 219000.0, 219000.0, 219000.0])

	def test_check_in_fx_relock_preserves_frozen_pricing_company(self):
		snap = {"agreed_rate": 474500.0, "company": None,
		        "tariff": {"currency": "USD", "nightly": 130.0, "fx_rate": 3650.0,
		                   "fx_date": "2026-11-20", "property_currency": "UGX"}}
		doc = self.make_res(rate_plan="CON-CHK", company="Corp Negotiated", agreed_rate=474500.0,
		                    contract_tariff=json.dumps(snap))
		self.docs["Property", "Test Hotel"].business_date = "2026-11-24"
		self.rate("USD", 3700.0, "2026-11-24")
		self.assertTrue(self.pricing.relock_contract_fx_at_check_in(doc))
		self.assertIn("company", doc.contract_tariff)
		self.assertIsNone(doc.contract_tariff["company"])


class UpdateOccupantsGuardTest(QuotePatches, RateMatrixHarness):
	"""P2-1: update_occupants guards price-affecting child ages under authorization and price confirmation."""

	modules = ("kamra.pricing", "kamra.kamra.doctype.reservation.reservation", "kamra.authz", "kamra.api")

	def extra_modules(self):
		pack = SimpleNamespace(DEFAULT_NATIONALITY="Ugandan", calculate_room_tax=lambda *args: 0)
		return {
			"kamra.folio": stub_module("kamra.folio", _fnb_gst=lambda property: 5,
			                           charged_nights=lambda name: {}),
			"kamra.localization": stub_module(
				"kamra.localization",
				pack_for=lambda prop: pack,
				default_currency=lambda p: "UGX",
				is_minor=lambda p, age: bool(age) and int(age) < 18,
				minor_id_kept=lambda p, nat: False,
				store_id_number=lambda p, t, n: None,
				validate_id_type=lambda p, t: None,
			),
			"kamra.privacy": stub_module("kamra.privacy", _delete_files=lambda *args, **kwargs: None),
		}

	def setUp(self):
		super().setUp()
		self.roles = ["Front Desk"]
		self.authz.restricted_properties = lambda user=None: None
		orig_exists = self.frappe.db.exists

		def exists(doctype, name=None):
			if isinstance(name, dict):
				return any(
					dt == doctype and all(d.get(k) == v for k, v in name.items())
					for (dt, _n), d in self.docs.items()
				)
			return orig_exists(doctype, name)

		self.frappe.db.exists = exists
		self.add("Room Type", "Test Hotel-DLX", property="Test Hotel", base_price=400000, base_occupancy=2,
		         adults_capacity=4, children_capacity=4, free_child_age=6, extra_adult_price=50000, child_price=25000)
		self.add("Meal Plan", "Test Hotel-CP", property="Test Hotel", code="CP", label="BB",
		         price_per_adult=40000, price_per_child=20000)
		self.plan("CON-RES")
		self.plan("PLAN-CARD", modifier_type="Percent", modifier_value=5)
		self.patch_quote()

	def saved_stay(self, name, *, rate_plan, agreed_rate=0.0, meal_plan=None, children=1, infants=0, occupants=None):
		values = dict(
			name=name, property="Test Hotel", room_type="Test Hotel-DLX", rate_plan=rate_plan,
			check_in_date="2026-11-24", check_out_date="2026-11-26", adults=2, children=children,
			infants=infants, extra_beds=0, occupants=list(occupants or []), auto_price=1,
			agreed_rate=agreed_rate, meal_plan=meal_plan, company=None, group_booking=None,
			status="Confirmed", voucher=None,
		)
		doc = self.reservation.Reservation({**values, "flags": Doc()})
		doc.is_new = lambda: False
		doc.get_doc_before_save = lambda: doc
		doc.has_value_changed = lambda field: False
		# Seed initial quoted amounts, locked nights and rate_lock_key as a real booking save does.
		q0 = doc.requote()
		doc.amount_before_tax = q0["amount_before_tax"]
		doc.tax_amount = q0["tax_amount"]
		doc.amount_after_tax = q0["amount_after_tax"]
		doc.rate_lock_key = doc.rate_lock_key_for(doc.quote_inputs())
		doc.night_rates = [Doc(r) for r in doc.quote_night_rows(q0)]
		saved_calls = []

		def save(ignore_permissions=False):
			saved_calls.append(ignore_permissions)
			doc.apply_pricing()
			doc.night_rates = [Doc(r) if isinstance(r, dict) else r for r in doc.night_rates]

		def append(field, row):
			entry = Doc(row)
			entry.name = f"OCC-{len(doc.get(field) or []) + 1}"
			doc.setdefault(field, []).append(entry)
			return entry

		def set_field(field, value):
			doc[field] = value

		doc.save = save
		doc.append = append
		doc.set = set_field
		doc.saved_calls = saved_calls
		self.docs["Reservation", name] = doc
		return doc

	def test_front_desk_refused_and_revenue_manager_confirms_price_on_tariff_meal_stay(self):
		doc = self.saved_stay("RES-TAR-CP", rate_plan="CON-RES", agreed_rate=474500.0, meal_plan="Test Hotel-CP")
		before_total = doc.amount_after_tax

		# Front Desk adding child age=4 (<= free_child_age=6) drops billable_children 1->0 and is refused.
		self.roles = ["Front Desk"]
		with self.assertRaises(self.frappe.PermissionError):
			self.api.update_occupants(doc.name, [{"full_name": "Kid Four", "age": 4}])
		self.assertEqual(doc.saved_calls, [])
		self.assertEqual(doc.amount_after_tax, before_total)

		# Revenue Manager without confirm_price=1 hits PriceConfirmationRequired (meal drop of 42,000).
		self.roles = ["Revenue Manager"]
		with self.assertRaises(self.api.PriceConfirmationRequired):
			self.api.update_occupants(doc.name, [{"full_name": "Kid Four", "age": 4}])
		self.assertEqual(doc.saved_calls, [])

		# Revenue Manager with confirm_price=1 succeeds and re-prices cleanly.
		out = self.api.update_occupants(doc.name, [{"full_name": "Kid Four", "age": 4}], confirm_price=1)
		self.assertEqual(out["occupants"], 1)
		self.assertEqual(doc.saved_calls, [True])
		self.assertAlmostEqual(before_total - doc.amount_after_tax, 42000.0, places=2)

	def test_front_desk_refused_and_revenue_manager_confirms_price_on_card_plan_stay(self):
		doc = self.saved_stay("RES-CARD", rate_plan="PLAN-CARD", agreed_rate=0.0, meal_plan=None)
		before_nights = [r["rate"] for r in doc.night_rates]
		before_total = doc.amount_after_tax

		self.roles = ["Front Desk"]
		with self.assertRaises(self.frappe.PermissionError):
			self.api.update_occupants(doc.name, [{"full_name": "Baby Four", "age": 4}])
		self.assertEqual([r["rate"] for r in doc.night_rates], before_nights)
		self.assertEqual(doc.amount_after_tax, before_total)

		self.roles = ["Revenue Manager"]
		with self.assertRaises(self.api.PriceConfirmationRequired):
			self.api.update_occupants(doc.name, [{"full_name": "Baby Four", "age": 4}])
		self.api.update_occupants(doc.name, [{"full_name": "Baby Four", "age": 4}], confirm_price=1)
		self.assertLess(doc.amount_after_tax, before_total)
		self.assertEqual([r["rate"] for r in doc.night_rates], [420000.0, 420000.0])

	def test_front_desk_refused_on_no_meal_tariff_stay_when_billable_children_changes(self):
		# Even when meal_plan is unset (existing total unchanged), changing billable_children alters future extensions.
		doc = self.saved_stay("RES-TAR-RO", rate_plan="CON-RES", agreed_rate=456250.0, meal_plan=None)
		self.roles = ["Front Desk"]
		with self.assertRaises(self.frappe.PermissionError):
			self.api.update_occupants(doc.name, [{"full_name": "Kid Four", "age": 4}])
		# Revenue Manager is allowed (total does not change, so no confirm_price needed).
		self.roles = ["Revenue Manager"]
		out = self.api.update_occupants(doc.name, [{"full_name": "Kid Four", "age": 4}])
		self.assertEqual(out["occupants"], 1)

	def test_front_desk_non_pricing_occupant_registration_remains_allowed(self):
		# Registering adults, older children (> free_child_age), or already-booked infants never blocks check-in.
		self.roles = ["Front Desk"]
		for label, kwargs, rows in (
			("adult", {"children": 1, "infants": 0}, [{"full_name": "Companion", "age": 30}]),
			("older_child", {"children": 1, "infants": 0}, [{"full_name": "Kid Eight", "age": 8}]),
			("booked_infant", {"children": 1, "infants": 1}, [{"full_name": "Baby Two", "age": 2}]),
			("no_children_booked", {"children": 0, "infants": 0}, [{"full_name": "Visit Child", "age": 4}]),
		):
			with self.subTest(label):
				doc = self.saved_stay(f"RES-OK-{label}", rate_plan="CON-RES", agreed_rate=474500.0,
				                      meal_plan="Test Hotel-CP", **kwargs)
				before_total = doc.amount_after_tax
				out = self.api.update_occupants(doc.name, rows)
				self.assertEqual(out["occupants"], 1)
				self.assertEqual(doc.amount_after_tax, before_total)
				self.assertEqual(doc.saved_calls, [False])


class FixedRateDegradationTest(RateMatrixHarness):
	"""P2-2: Reservation without a fixed_rate field (sibling kamra#32 unmerged) degrades safely to 0."""

	modules = ("kamra.pricing", "kamra.folio")

	def test_absent_fixed_rate_degrades_safely_across_pricing_and_folio(self):
		self.plan("CON-CUR", fx_lock_policy="Current Date")
		self.plan("CON-CHK", fx_lock_policy="Fixed at Check In")
		self.docs["Property", "Test Hotel"].business_date = "2026-11-24"
		self.rate("USD", 3700.0, "2026-11-24")
		nights = [{"date": "2026-11-24", "nightly": 130.0, "agreed_rate": 474500.0, "item_price": "IP-1"}]
		snap = {"agreed_rate": 474500.0, "tariff": {
			"currency": "USD", "nightly": 130.0, "fx_rate": 3650.0, "fx_date": "2026-11-20",
			"property_currency": "UGX", "nights": nights}}
		res = Doc(name="RES-NOFR", property="Test Hotel", room_type="Test Hotel-DLX", rate_plan="CON-CHK",
		          check_in_date="2026-11-24", check_out_date="2026-11-25", auto_price=1, agreed_rate=474500.0,
		          contract_tariff=json.dumps(snap), flags=Doc())
		self.assertNotIn("fixed_rate", res)
		self.assertEqual(self.pricing.contract_night_rates(res), {"2026-11-24": 474500.0})
		self.assertTrue(self.pricing.relock_contract_fx_at_check_in(res))
		self.assertEqual(res.agreed_rate, 481000.0)
