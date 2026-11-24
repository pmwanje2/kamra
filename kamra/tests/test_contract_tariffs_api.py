"""Contract tariff endpoints, using the real role guard and bridge without a site."""

import importlib.util
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class Refused(Exception):
	pass


class PermissionDenied(Refused):
	pass


def throw(message, exc=Refused, title=None):
	raise exc(message)


class ContractTariffHarness(unittest.TestCase):
	"""The real role guard and bridge over recorded Server Scripts.

	`script` answers every bridge method; `scripts` answers per method and
	marks which ones a property has installed."""

	def setUp(self):
		self.roles = ["Hotel Admin"]
		self.can_delete_item_price = True
		self.allowed_properties = {"Test Hotel"}
		self.script = None
		self.scripts = {}
		self.rate_plans = []
		self.room_types = []
		self.rooms = []
		self.lookups = []
		self.calls = []
		self.whitelisted = {}
		self.frappe = SimpleNamespace(
			_=lambda text: text,
			whitelist=self.whitelist,
			get_roles=lambda: self.roles,
			has_permission=self.has_permission,
			throw=throw,
			PermissionError=PermissionDenied,
			ValidationError=Refused,
			local=SimpleNamespace(form_dict={}),
			response={},
			_dict=dict,
			db=SimpleNamespace(get_value=self.find_script),
			get_doc=self.get_script,
			get_all=self.get_rate_plans,
			parse_json=lambda value: json.loads(value) if isinstance(value, str) else value,
		)
		utils = SimpleNamespace(**dict.fromkeys(
			("add_days", "date_diff", "getdate", "add_to_date", "cint", "now_datetime")))
		modules = patch.dict(sys.modules, {"frappe": self.frappe, "frappe.utils": utils})
		modules.start()
		self.addCleanup(modules.stop)
		self.load_module("kamra.branding")
		authz = self.load_module("kamra.authz")
		authz.restricted_properties = lambda: self.allowed_properties
		self.load_module("kamra.erp_bridge")
		self.rates = self.load_module("kamra.rates")

	def load_module(self, name):
		spec = importlib.util.find_spec(name)
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		sys.modules[name] = module
		return module

	def whitelist(self, *, methods=None):
		def decorate(fn):
			self.whitelisted[fn.__name__] = methods
			return fn
		return decorate

	def find_script(self, doctype, filters):
		self.assertEqual(doctype, "Server Script")
		self.assertEqual(filters, {
			"script_type": "API", "api_method": filters["api_method"], "disabled": 0})
		self.lookups.append(filters["api_method"])
		installed = filters["api_method"] in self.scripts or self.script is not None
		return filters["api_method"] if installed else None

	def get_script(self, doctype, name):
		self.assertEqual(doctype, "Server Script")

		def execute():
			form = dict(self.frappe.local.form_dict)
			self.calls.append((name, form))
			answer = self.scripts.get(name, self.script)
			self.frappe.response["message"] = answer(form) if callable(answer) else answer

		return SimpleNamespace(execute_method=execute)

	def has_permission(self, doctype, ptype="read", *args, **kwargs):
		if doctype == "Item Price" and ptype == "delete":
			return self.can_delete_item_price
		return True

	def get_rate_plans(self, doctype, filters=None, fields=None, **kwargs):
		if doctype == "Room Type":
			rows = [rt for rt in self.room_types if not (filters or {}).get("disabled") == 0 or not rt.get("disabled")]
			return [{field: row.get(field) for field in fields} for row in rows] if fields else rows
		if doctype == "Room":
			return [{field: row.get(field) for field in fields} for row in self.rooms] if fields else self.rooms
		self.assertEqual(doctype, "Rate Plan")
		self.assertEqual(filters, {"property": "Test Hotel"})
		return [{field: plan.get(field) for field in fields} for plan in self.rate_plans]

	def endpoint(self, name):
		self.assertIn(name, self.whitelisted, f"Missing tariff endpoint: {name}")
		return getattr(self.rates, name)


class TestContractTariffsAPI(ContractTariffHarness):

	def test_absent_list_script_returns_read_only_unsupported_payload(self):
		self.roles = ["Front Desk"]
		result = self.endpoint("list_contract_tariffs")("Test Hotel")
		self.assertEqual(result, {
			"supported": False, "property": "Test Hotel", "price_lists": [], "items": [],
			"can_propose": False, "can_approve": False, "can_discard": False})
		self.assertEqual(self.lookups, ["hospitality_tariffs.list_tariffs"])
		self.assertEqual(self.calls, [])

	def test_list_forwards_the_property_and_preserves_the_payload(self):
		self.script = {"supported": True, "property": "Test Hotel", "property_currency": "UGX",
			"business_date": "2026-11-22", "price_lists": [], "items": [],
			"can_propose": True, "can_approve": False}
		self.assertIs(self.endpoint("list_contract_tariffs")(property="Test Hotel"), self.script)
		self.assertEqual(self.calls, [("hospitality_tariffs.list_tariffs", {"property": "Test Hotel"})])

	def test_proposal_forwards_all_fields_including_optional_end_date(self):
		self.roles = ["Revenue Manager"]
		self.script = {"item_price": "TARIFF-001"}
		propose = self.endpoint("propose_contract_tariff")
		args = {"property": "Test Hotel", "price_list": "USD Contract", "item_code": "DLX",
			"price_list_rate": 140.25, "valid_from": "2026-12-01"}
		for end in ("2026-12-31", None):
			with self.subTest(end=end):
				kwargs = {**args, "valid_upto": end} if end else args
				self.assertIs(propose(**kwargs), self.script)
				self.assertEqual(self.calls[-1],
					("hospitality_tariffs.propose_tariff", {**args, "valid_upto": end}))
		self.assertEqual(self.whitelisted["propose_contract_tariff"], ["POST"])

	def test_approval_and_discard_delegate_only_the_item_price(self):
		self.script = {"item_price": "TARIFF-001"}
		for action in ("approve", "discard"):
			with self.subTest(action=action):
				name = f"{action}_contract_tariff"
				self.assertIs(self.endpoint(name)(item_price="TARIFF-001"), self.script)
				self.assertEqual(self.calls[-1],
					(f"hospitality_tariffs.{action}_tariff", {"item_price": "TARIFF-001"}))
				self.assertEqual(self.whitelisted[name], ["POST"])

	def test_property_scope_refuses_list_and_proposal_before_bridge(self):
		with self.assertRaises(PermissionDenied):
			self.endpoint("list_contract_tariffs")("Other Hotel")
		with self.assertRaises(PermissionDenied):
			self.endpoint("propose_contract_tariff")("Other Hotel", "USD", "DLX", 140, "2026-12-01")
		self.assertEqual(self.lookups, [])

	def test_front_desk_cannot_mutate_tariffs(self):
		self.roles = ["Front Desk"]
		for name, args in (
			("propose_contract_tariff", ("Test Hotel", "USD", "DLX", 140, "2026-12-01")),
			("approve_contract_tariff", ("TARIFF-001",)),
			("discard_contract_tariff", ("TARIFF-001",)),
		):
			with self.subTest(name=name), self.assertRaises(PermissionDenied):
				self.endpoint(name)(*args)
		self.assertEqual(self.lookups, [])


ENTRY = {"price_list": "Corporate USD", "item_code": "DLX", "price_list_rate": 136.5, "valid_from": "2026-12-01"}
NORMALIZED = {**ENTRY, "valid_upto": None}
COLUMNS = ["Price List", "Currency", "Customer", "Item Code", "Room Type", "Occupancy Tier", "Rate",
           "Valid From", "Valid Upto", "Status"]


def version(name, rate, start, end=None, state="Approved", **extra):
	return {"name": name, "rate": rate, "valid_from": start, "valid_upto": end, "workflow_state": state, **extra}


def directory():
	room = {"price_list": "Corporate USD", "item_code": "DLX", "item_name": "Deluxe", "category": "Room",
	        "room_type": "Deluxe", "occupancy": "Double"}
	return {"supported": True, "property": "Test Hotel", "property_currency": "UGX",
	        "business_date": "2026-11-22", "can_propose": True, "can_approve": True,
	        "price_lists": [{"name": "Corporate USD", "currency": "USD", "rate_plans": ["Test Hotel-CORP"]},
	                        {"name": "Local rates", "currency": "UGX", "rate_plans": []}],
	        "items": [
	            {**room, "active": version("IP-1", 130, "2026-11-01"),
	             "pending": [version("IP-2", 140, "2026-12-01", "2026-12-31", "Draft", owner="rm@example.test")],
	             "history": [version("IP-0", 115, "2026-10-01", "2026-10-31"), version("IP-3", 150, "2027-01-01")]},
	            {**room, "customer": "Client Co", "active": version("IP-4", 120, "2026-11-01"),
	             "pending": [], "history": []},
	            {"price_list": "Local rates", "item_code": "HALL", "item_name": "Garden Hall", "category": "Venue",
	             "currency": "UGX", "active": None, "pending": [], "history": []},
	        ]}


class TestContractTariffBulkAPI(ContractTariffHarness):
	"""SAP F4111-style mass maintenance over the same governed bridge."""

	def test_list_forwards_only_the_filters_given(self):
		self.script = {"supported": True, "property": "Test Hotel", "price_lists": [], "items": []}
		listing = self.endpoint("list_contract_tariffs")
		listing("Test Hotel", price_list="Corporate USD", customer="Client Co", as_of_date="2026-12-15")
		listing("Test Hotel", customer="Client Co")
		self.assertEqual([form for _, form in self.calls], [
			{"property": "Test Hotel", "price_list": "Corporate USD", "customer": "Client Co",
			 "as_of_date": "2026-12-15"},
			{"property": "Test Hotel", "customer": "Client Co"}])

	def test_list_adds_the_rate_plan_policies_behind_its_price_lists(self):
		plan = {"rate_plan_name": "Corporate", "code": "CORP", "currency": "USD", "fx_lock_policy": "Fixed at Check In",
		        "base_rate_plan": "Test Hotel-BAR", "adjust_extra_person": 1, "rounding_step": 1000,
		        "rounding_mode": "Up"}
		self.rate_plans = [
			{"name": "Test Hotel-CORP", **plan},
			{"name": "Test Hotel-BAR", "rate_plan_name": "Best available", "code": "BAR", "currency": None,
			 "fx_lock_policy": "Fixed at Reservation", "base_rate_plan": None, "adjust_extra_person": 0,
			 "rounding_step": 0, "rounding_mode": "Nearest"},
			{"name": "Test Hotel-DAY", "rate_plan_name": "Day use", "code": "DAY", "currency": None,
			 "fx_lock_policy": "Fixed at Reservation", "base_rate_plan": None, "adjust_extra_person": 0,
			 "rounding_step": 0, "rounding_mode": "Nearest"},
		]
		self.script = directory()
		policies = self.endpoint("list_contract_tariffs")("Test Hotel")["rate_plan_policies"]
		self.assertEqual(policies, {
			"Test Hotel-CORP": {"label": "Corporate", "code": "CORP", "currency": "USD",
			                    "fx_lock_policy": "Fixed at Check In", "base_rate_plan": "Test Hotel-BAR",
			                    "adjust_extra_person": True, "rounding_step": 1000.0, "rounding_mode": "Up"},
			"Test Hotel-BAR": {"label": "Best available", "code": "BAR", "currency": None,
			                   "fx_lock_policy": "Fixed at Reservation", "base_rate_plan": None,
			                   "adjust_extra_person": False, "rounding_step": 0.0, "rounding_mode": "Nearest"},
		})

	def test_bulk_proposal_uses_the_bulk_integration_with_checked_entries(self):
		self.roles = ["Revenue Manager"]
		self.scripts = {"hospitality_tariffs.bulk_propose_tariffs": {"proposed": ["IP-7", "IP-8"]}}
		second = {**ENTRY, "item_code": "DLX-1A", "price_list_rate": "120", "valid_upto": "2026-12-31",
		          "customer": "Client Co"}
		result = self.endpoint("bulk_propose_contract_tariffs")(
			property="Test Hotel", entries=json.dumps([ENTRY, second]))
		self.assertEqual(result, {"proposed": ["IP-7", "IP-8"]})
		self.assertEqual(self.calls, [("hospitality_tariffs.bulk_propose_tariffs", {"property": "Test Hotel", "entries": [
			NORMALIZED, {**second, "price_list_rate": 120.0}]})])
		self.assertEqual(self.whitelisted["bulk_propose_contract_tariffs"], ["POST"])

	def test_bulk_proposal_falls_back_to_one_proposal_per_entry(self):
		self.scripts = {"hospitality_tariffs.propose_tariff": lambda form: {"item_price": "IP-" + form["item_code"]}}
		result = self.endpoint("bulk_propose_contract_tariffs")(
			"Test Hotel", [ENTRY, {**ENTRY, "item_code": "SGL"}])
		self.assertEqual(result, {"proposed": [{"item_price": "IP-DLX"}, {"item_price": "IP-SGL"}], "count": 2})
		self.assertEqual(self.calls, [
			("hospitality_tariffs.propose_tariff", {"property": "Test Hotel", **NORMALIZED}),
			("hospitality_tariffs.propose_tariff", {"property": "Test Hotel", **NORMALIZED, "item_code": "SGL"})])

	def test_the_fallback_never_turns_a_corporate_tariff_into_a_public_one(self):
		self.scripts = {"hospitality_tariffs.propose_tariff": {"item_price": "IP-9"}}
		with self.assertRaisesRegex(Refused, "Customer-specific"):
			self.endpoint("bulk_propose_contract_tariffs")("Test Hotel", [{**ENTRY, "customer": "Client Co"}])
		self.assertEqual(self.calls, [])

	def test_bulk_proposal_without_an_integration_returns_none(self):
		self.assertIsNone(self.endpoint("bulk_propose_contract_tariffs")("Test Hotel", [ENTRY]))
		self.assertEqual(self.calls, [])

	def test_bulk_entries_are_checked_before_the_integration_runs(self):
		self.scripts = {"hospitality_tariffs.bulk_propose_tariffs": {"proposed": []}}
		missing_from = {key: value for key, value in ENTRY.items() if key != "valid_from"}
		for entries, message in (
			("not json", "as a list"), ([1], "as a list"), ([], "at least one"),
			([ENTRY] * 201, "at most 200"),
			([{**ENTRY, "item_code": ""}], "Row 1: Choose the price list and the item"),
			([{**ENTRY, "price_list_rate": "abc"}], "Row 1: The rate must be a number"),
			([{**ENTRY, "price_list_rate": "inf"}], "Row 1: The rate must be a number"),
			([{**ENTRY, "price_list_rate": True}], "Row 1: The rate must be a number"),
			([ENTRY, {**ENTRY, "item_code": "SGL", "price_list_rate": -1}], "Row 2: The rate cannot be negative"),
			([missing_from], "Row 1: Effective from must be a date"),
			([{**ENTRY, "valid_from": "01/12/2026"}], "Row 1: Effective from must be a date"),
			([{**ENTRY, "valid_upto": "soon"}], "Row 1: Valid until must be a date"),
			([{**ENTRY, "valid_upto": "2026-11-30"}], "Row 1: Valid until cannot be before"),
			([ENTRY, {**ENTRY, "price_list_rate": 140}], "Row 2: The same item, customer and date appear twice"),
		):
			with self.subTest(message=message, entries=str(entries)[:60]), self.assertRaisesRegex(Refused, message):
				self.endpoint("bulk_propose_contract_tariffs")("Test Hotel", entries)
		self.assertEqual(self.calls, [])

	def test_bulk_decisions_use_the_bulk_integration(self):
		for action in ("approve", "discard"):
			with self.subTest(action=action):
				self.scripts = {f"hospitality_tariffs.bulk_{action}_tariffs": {"done": 2}}
				name = f"bulk_{action}_contract_tariffs"
				result = self.endpoint(name)(item_prices=json.dumps(["IP-1", "IP-2", "IP-1"]))
				self.assertEqual(result, {"done": 2})
				self.assertEqual(self.calls[-1], (f"hospitality_tariffs.bulk_{action}_tariffs",
				                                  {"item_prices": ["IP-1", "IP-2"]}))
				self.assertEqual(self.whitelisted[name], ["POST"])

	def test_bulk_decisions_fall_back_to_the_single_maker_checker_endpoint(self):
		for action, key in (("approve", "approved"), ("discard", "discarded")):
			with self.subTest(action=action):
				self.calls.clear()
				self.scripts = {f"hospitality_tariffs.{action}_tariff": lambda form: {"item_price": form["item_price"]}}
				result = self.endpoint(f"bulk_{action}_contract_tariffs")(["IP-1", "IP-2"])
				self.assertEqual(result, {key: [{"item_price": "IP-1"}, {"item_price": "IP-2"}], "count": 2})
				self.assertEqual(self.calls, [(f"hospitality_tariffs.{action}_tariff", {"item_price": "IP-1"}),
				                              (f"hospitality_tariffs.{action}_tariff", {"item_price": "IP-2"})])

	def test_a_separation_of_duties_refusal_stops_the_bulk_approval(self):
		def approve(form):
			if form["item_price"] == "IP-2":
				raise PermissionDenied("You cannot approve a tariff you proposed.")
			return {"item_price": form["item_price"]}

		self.scripts = {"hospitality_tariffs.approve_tariff": approve}
		with self.assertRaisesRegex(PermissionDenied, "you proposed"):
			self.endpoint("bulk_approve_contract_tariffs")(["IP-1", "IP-2", "IP-3"])
		self.assertEqual([form["item_price"] for _, form in self.calls], ["IP-1", "IP-2"])

	def test_bulk_decisions_need_named_drafts(self):
		self.scripts = {"hospitality_tariffs.bulk_approve_tariffs": {"done": 0}}
		for names, message in (([], "at least one"), ([""], "at least one"), ([f"IP-{n}" for n in range(201)], "at most 200"),
		                       ("IP-1", "as a list"), ([{"name": "IP-1"}], "as a list")):
			with self.subTest(message=message), self.assertRaisesRegex(Refused, message):
				self.endpoint("bulk_approve_contract_tariffs")(names)
		self.assertEqual(self.calls, [])
		self.scripts = {}
		self.assertIsNone(self.endpoint("bulk_discard_contract_tariffs")(["IP-1"]))

	def test_tariff_roles_are_enforced_for_every_bulk_endpoint(self):
		self.scripts = {"hospitality_tariffs.bulk_propose_tariffs": {}, "hospitality_tariffs.bulk_approve_tariffs": {}}
		self.roles = ["Front Desk"]
		for name, args in (("bulk_propose_contract_tariffs", ("Test Hotel", [ENTRY])),
		                   ("import_contract_tariffs_rows", ("Test Hotel", []))):
			with self.subTest(name=name), self.assertRaises(PermissionDenied):
				self.endpoint(name)(*args)
		self.roles = ["Revenue Manager"]
		for name in ("bulk_approve_contract_tariffs", "bulk_discard_contract_tariffs"):
			with self.subTest(name=name), self.assertRaises(PermissionDenied):
				self.endpoint(name)(["IP-1"])
		with self.assertRaises(PermissionDenied):
			self.endpoint("bulk_propose_contract_tariffs")("Other Hotel", [ENTRY])
		with self.assertRaises(PermissionDenied):
			self.endpoint("export_contract_tariffs_csv")("Other Hotel")
		self.assertEqual(self.lookups, [])

	def test_export_lists_current_scheduled_and_in_review_versions(self):
		self.roles = ["Front Desk"]
		self.script = directory()
		result = self.endpoint("export_contract_tariffs_csv")(property="Test Hotel")
		self.assertEqual(result, {"supported": True, "business_date": "2026-11-22", "columns": COLUMNS, "rows": [
			["Corporate USD", "USD", "", "DLX", "Deluxe", "Double", 130.0, "2026-11-01", "", "Approved"],
			["Corporate USD", "USD", "", "DLX", "Deluxe", "Double", 150.0, "2027-01-01", "", "Scheduled"],
			["Corporate USD", "USD", "", "DLX", "Deluxe", "Double", 140.0, "2026-12-01", "2026-12-31", "In Review"],
			["Corporate USD", "USD", "Client Co", "DLX", "Deluxe", "Double", 120.0, "2026-11-01", "", "Approved"],
			["Local rates", "UGX", "", "HALL", "", "", "", "", "", "No active tariff"],
		]})
		self.assertEqual(self.calls, [("hospitality_tariffs.list_tariffs", {"property": "Test Hotel"})])

	def test_export_can_be_limited_to_one_price_list(self):
		self.script = directory()
		result = self.endpoint("export_contract_tariffs_csv")("Test Hotel", price_list="Local rates")
		self.assertEqual([row[3] for row in result["rows"]], ["HALL"])
		self.assertEqual(self.calls[-1][1], {"property": "Test Hotel", "price_list": "Local rates"})

	def test_export_without_the_directory_is_unsupported(self):
		self.assertEqual(self.endpoint("export_contract_tariffs_csv")("Test Hotel"),
		                 {"supported": False, "business_date": None, "columns": COLUMNS, "rows": []})

	def csv(self, *rows):
		return "\ufeff" + "\r\n".join([",".join(COLUMNS), *rows])

	def test_import_creates_drafts_for_new_rows_only(self):
		self.roles = ["Revenue Manager"]
		self.scripts = {"hospitality_tariffs.list_tariffs": directory(),
		                "hospitality_tariffs.bulk_propose_tariffs": {"proposed": ["IP-10", "IP-11"]}}
		result = self.endpoint("import_contract_tariffs_rows")("Test Hotel", self.csv(
			"Corporate USD,USD,,DLX,Deluxe,Double,145,2026-12-15,,Approved",
			"Corporate USD,USD,,DLX,Deluxe,Double,130,2026-11-01,,Approved",
			"Corporate USD,USD,Client Co,DLX,Deluxe,Double,125,2026-12-15,,Approved",
			"Local rates,UGX,,HALL,,,900000,2026-12-01,2026-12-31,No active tariff",
			"Local rates,UGX,,HALL,,,,,,No active tariff",
		))
		self.assertEqual(result, {"created": 3, "unchanged": 1, "skipped": 1, "errors": [],
		                          "proposed": {"proposed": ["IP-10", "IP-11"]}})
		self.assertEqual(self.calls, [
			("hospitality_tariffs.list_tariffs", {"property": "Test Hotel"}),
			("hospitality_tariffs.bulk_propose_tariffs", {"property": "Test Hotel", "entries": [
				{"price_list": "Corporate USD", "item_code": "DLX", "price_list_rate": 145.0,
				 "valid_from": "2026-12-15", "valid_upto": None},
				{"price_list": "Corporate USD", "item_code": "DLX", "price_list_rate": 125.0,
				 "valid_from": "2026-12-15", "valid_upto": None, "customer": "Client Co"},
				{"price_list": "Local rates", "item_code": "HALL", "price_list_rate": 900000.0,
				 "valid_from": "2026-12-01", "valid_upto": "2026-12-31"}]})])
		self.assertEqual(self.whitelisted["import_contract_tariffs_rows"], ["POST"])

	def test_import_reads_back_a_cell_the_export_guarded_against_formulas(self):
		self.scripts = {"hospitality_tariffs.list_tariffs": directory(),
		                "hospitality_tariffs.bulk_propose_tariffs": {"proposed": ["IP-13"]}}
		self.endpoint("import_contract_tariffs_rows")("Test Hotel", self.csv(
			"Corporate USD,USD,'+Partner,DLX,Deluxe,Double,118,2026-12-20,,"))
		self.assertEqual(self.calls[-1][1]["entries"][0]["customer"], "+Partner")

	def test_import_accepts_row_objects(self):
		self.scripts = {"hospitality_tariffs.list_tariffs": directory(),
		                "hospitality_tariffs.bulk_propose_tariffs": {"proposed": ["IP-12"]}}
		result = self.endpoint("import_contract_tariffs_rows")("Test Hotel", json.dumps([
			{"price_list": "Corporate USD", "currency": "USD", "item_code": "DLX", "rate": 0, "valid_from": "2026-12-20"}]))
		self.assertEqual(result["created"], 1)
		self.assertEqual(self.calls[-1][1]["entries"][0]["price_list_rate"], 0.0)

	def test_import_reports_every_bad_row_and_creates_nothing(self):
		self.scripts = {"hospitality_tariffs.list_tariffs": directory(),
		                "hospitality_tariffs.bulk_propose_tariffs": {"proposed": []}}
		result = self.endpoint("import_contract_tariffs_rows")("Test Hotel", self.csv(
			"Corporate USD,UGX,,DLX,,,145,2026-12-15,,",
			"Unknown list,USD,,DLX,,,145,2026-12-15,,",
			"Corporate USD,USD,,NOPE,,,145,2026-12-15,,",
			"Corporate USD,USD,,DLX,,,-5,2026-12-15,,",
			"Corporate USD,USD,,DLX,,,145,,,",
			"Corporate USD,USD,,DLX,,,145,15/12/2026,,",
			",USD,,DLX,,,145,2026-12-15,,",
			"Corporate USD,,,DLX,,,145,2026-12-15,,",
			"Corporate USD,USD,,DLX,,,145,2026-12-16,,",
			"Corporate USD,USD,,DLX,,,146,2026-12-16,,",
		))
		self.assertEqual(result["created"], 0)
		self.assertEqual([(error["row"], error["message"]) for error in result["errors"]], [
			(2, "UGX does not match the Corporate USD price list currency (USD)."),
			(3, "Unknown list is not a price list of this property."),
			(4, "NOPE is not on the Corporate USD price list."),
			(5, "The rate cannot be negative."),
			(6, "Effective from must be a date (YYYY-MM-DD)."),
			(7, "Effective from must be a date (YYYY-MM-DD)."),
			(8, "Choose the price list and the item."),
			(9, "Give the price list currency."),
			(11, "The same item, customer and date appear twice."),
		])
		self.assertEqual([method for method, _ in self.calls], ["hospitality_tariffs.list_tariffs"])

	def test_import_without_the_directory_returns_none(self):
		self.assertIsNone(self.endpoint("import_contract_tariffs_rows")("Test Hotel", self.csv(
			"Corporate USD,USD,,DLX,,,145,2026-12-15,,")))


class TestContractTariffRemediationAPI(ContractTariffHarness):
	"""Round 2 (Gap Record #8556): customer scope, the integration's limits and the CSV round trip."""

	csv = TestContractTariffBulkAPI.csv

	def test_a_customer_version_forwards_the_customer(self):
		self.roles = ["Revenue Manager"]
		self.script = lambda form: {"name": "IP-21", "customer": form.get("customer")}
		result = self.endpoint("propose_contract_tariff")(
			"Test Hotel", "Corporate USD", "DLX", 120, "2026-12-01", customer="Client Co")
		self.assertEqual(result, {"name": "IP-21", "customer": "Client Co"})
		self.assertEqual(self.calls, [("hospitality_tariffs.propose_tariff", {
			"property": "Test Hotel", "price_list": "Corporate USD", "item_code": "DLX", "price_list_rate": 120,
			"valid_from": "2026-12-01", "valid_upto": None, "customer": "Client Co"})])

	def test_an_integration_that_drops_the_customer_is_refused_not_published(self):
		# a pre-v11 single proposal ignores customer: its draft would be the public rate
		self.script = {"name": "IP-22", "customer": None}
		with self.assertRaisesRegex(Refused, "customer"):
			self.endpoint("propose_contract_tariff")("Test Hotel", "Corporate USD", "DLX", 120, "2026-12-01",
			                                         customer="Client Co")
		self.script = {"name": "IP-23"}
		with self.assertRaisesRegex(Refused, "customer"):
			self.endpoint("propose_contract_tariff")("Test Hotel", "Corporate USD", "DLX", 120, "2026-12-01",
			                                         customer="Client Co")

	def test_batches_follow_the_integration_limit_of_200(self):
		self.assertEqual(self.rates.TARIFF_BATCH_LIMIT, 200)
		self.scripts = {"hospitality_tariffs.bulk_propose_tariffs": {"proposed": []},
		                "hospitality_tariffs.bulk_approve_tariffs": {"approved": 0}}
		rows = [{**ENTRY, "valid_from": f"2027-{1 + n // 28:02d}-{1 + n % 28:02d}"} for n in range(200)]
		self.endpoint("bulk_propose_contract_tariffs")("Test Hotel", rows)
		self.endpoint("bulk_approve_contract_tariffs")([f"IP-{n}" for n in range(200)])
		with self.assertRaisesRegex(Refused, "at most 200"):
			self.endpoint("bulk_propose_contract_tariffs")("Test Hotel", [*rows, {**ENTRY, "valid_from": "2028-01-01"}])

	def test_rate_plan_objects_in_a_price_list_never_break_the_policies(self):
		self.rate_plans = [{"name": "Test Hotel-CORP", "rate_plan_name": "Corporate", "code": "CORP", "currency": "USD",
		                    "fx_lock_policy": "Current Date", "base_rate_plan": None, "adjust_extra_person": 0,
		                    "rounding_step": 0, "rounding_mode": "Nearest"}]
		listing = directory()
		listing["price_lists"][0]["rate_plans"] = [{"name": "Test Hotel-CORP", "label": "Corporate"}]
		self.script = listing
		policies = self.endpoint("list_contract_tariffs")("Test Hotel")["rate_plan_policies"]
		self.assertEqual(list(policies), ["Test Hotel-CORP"])
		self.assertEqual(policies["Test Hotel-CORP"]["fx_lock_policy"], "Current Date")

	def test_the_directory_says_who_may_discard_a_draft(self):
		# a maker may not approve their own draft, but a Hotel Admin with Item Price delete permission may withdraw it
		self.script = {**directory(), "can_approve": False}
		self.assertTrue(self.endpoint("list_contract_tariffs")("Test Hotel")["can_discard"])
		# Hotel Admin without Item Price delete permission (gov20) must NOT be advertised can_discard=True (P2-1)
		self.can_delete_item_price = False
		self.script = directory()
		self.assertFalse(self.endpoint("list_contract_tariffs")("Test Hotel")["can_discard"])
		self.can_delete_item_price = True
		self.roles = ["Revenue Manager"]
		self.script = directory()
		self.assertFalse(self.endpoint("list_contract_tariffs")("Test Hotel")["can_discard"])
		self.roles = ["Hotel Admin"]
		self.script = {**directory(), "can_propose": False, "can_approve": False}
		self.assertFalse(self.endpoint("list_contract_tariffs")("Test Hotel")["can_discard"])

	def test_unbackfilled_tariff_plan_policy_infers_price_list_currency(self):
		self.rate_plans = [{
			"name": "Test Hotel-CORP", "rate_plan_name": "Corporate", "code": "CORP", "currency": None,
			"fx_lock_policy": "Fixed at Reservation", "base_rate_plan": None, "adjust_extra_person": 0,
			"rounding_step": 0, "rounding_mode": "Nearest",
		}]
		self.script = directory()
		policies = self.endpoint("list_contract_tariffs")("Test Hotel")["rate_plan_policies"]
		self.assertEqual(policies["Test Hotel-CORP"]["currency"], "USD")

	def test_an_oversized_import_is_refused_before_it_is_parsed(self):
		self.assertEqual(self.rates.TARIFF_IMPORT_BYTES, 1_048_576)
		self.scripts = {"hospitality_tariffs.list_tariffs": directory()}
		big = self.csv("Corporate USD,USD,,DLX,Deluxe,Double,145,2026-12-15,," + " " * 1_048_576)
		with patch.object(self.rates.csv, "DictReader", side_effect=AssertionError("parsed")), \
				patch.object(self.rates.json, "loads", side_effect=AssertionError("parsed")), \
				self.assertRaisesRegex(Refused, "1 MB"):
			self.endpoint("import_contract_tariffs_rows")("Test Hotel", big)
		self.assertEqual(self.calls, [])

	def test_an_import_reads_at_most_one_row_past_the_limit(self):
		self.scripts = {"hospitality_tariffs.list_tariffs": directory()}
		real, pulled = self.rates.csv.DictReader, []

		def counting(*args, **kwargs):
			for row in real(*args, **kwargs):
				pulled.append(row)
				yield row

		rows = [f"Corporate USD,USD,,DLX,Deluxe,Double,145,2027-01-{1 + n % 28:02d},," for n in range(5000)]
		with patch.object(self.rates.csv, "DictReader", counting), self.assertRaisesRegex(Refused, "at most 200"):
			self.endpoint("import_contract_tariffs_rows")("Test Hotel", self.csv(*rows))
		self.assertLessEqual(len(pulled), 201)

	def test_import_reads_back_every_cell_the_export_guards(self):
		unguard = self.rates._unguard
		# mirrors frontend/src/lib/csv.ts csvCell: a formula start after any leading whitespace, ASCII or fullwidth
		for typed in ("=SUM(A1)", "+Partner", "-Partner", "@Partner", " =x", "\t=x", "\r=x", "\n@x",
		              "\uff1dx", "\uff0bx", "\uff0dx", "\uff20x", "\u3000=x"):
			with self.subTest(typed=typed):
				self.assertEqual(unguard("'" + typed), typed)
		# what the export never guards keeps its quote: plain text and plain numbers
		for kept in ("'Client Co", "'-5", "'12.5", "'", "plain"):
			with self.subTest(kept=kept):
				self.assertEqual(unguard(kept), kept)

	def test_export_follows_the_customer_and_effective_date_on_screen(self):
		self.script = directory()
		export = self.endpoint("export_contract_tariffs_csv")
		result = export("Test Hotel", customer="Client Co", as_of_date="2026-12-15")
		self.assertEqual(self.calls[-1][1], {"property": "Test Hotel", "customer": "Client Co",
		                                     "as_of_date": "2026-12-15"})
		self.assertEqual({row[2] for row in result["rows"]}, {"Client Co"})
		public = export("Test Hotel", customer=self.rates.PUBLIC_TARIFFS)
		self.assertEqual(self.calls[-1][1], {"property": "Test Hotel"})  # the integration has no public-only filter
		self.assertEqual({row[2] for row in public["rows"]}, {""})
		self.assertEqual(self.rates.PUBLIC_TARIFFS, "__public__")

	def test_export_schedules_versions_from_the_effective_date_on_screen(self):
		self.script = {**directory(), "as_of_date": "2027-02-01"}
		rows = self.endpoint("export_contract_tariffs_csv")("Test Hotel", as_of_date="2027-02-01")["rows"]
		# IP-3 (from 2027-01-01) is no longer ahead of the date being viewed
		self.assertNotIn(["Corporate USD", "USD", "", "DLX", "Deluxe", "Double", 150.0, "2027-01-01", "", "Scheduled"],
		                 rows)

	def test_list_includes_unpriced_room_types_with_room_counts(self):
		self.room_types = [
			{"name": "Test Hotel-DLX", "room_type_name": "Deluxe", "room_type_code": "DLX", "disabled": 0},
			{"name": "Test Hotel-EXE", "room_type_name": "Executive Suite", "room_type_code": "EXE", "disabled": 0},
			{"name": "Test Hotel-STD", "room_type_name": "Standard", "room_type_code": "STD", "disabled": 0},
			{"name": "Test Hotel-OLD", "room_type_name": "Retired", "room_type_code": "OLD", "disabled": 1},
		]
		self.rooms = [
			{"room_type": "Test Hotel-DLX"},
			{"room_type": "Test Hotel-DLX"},
			{"room_type": "Test Hotel-EXE"},
			{"room_type": "Test Hotel-EXE"},
		]
		self.script = directory()
		result = self.endpoint("list_contract_tariffs")("Test Hotel")
		self.assertEqual(result["unpriced_room_types"], [
			{"name": "Test Hotel-DLX", "room_type_name": "Deluxe", "room_type_code": "DLX",
			 "room_count": 2, "price_lists": ["Local rates"]},
			{"name": "Test Hotel-EXE", "room_type_name": "Executive Suite", "room_type_code": "EXE",
			 "room_count": 2, "price_lists": ["Corporate USD", "Local rates"]},
			{"name": "Test Hotel-STD", "room_type_name": "Standard", "room_type_code": "STD",
			 "room_count": 0, "price_lists": ["Corporate USD", "Local rates"]},
		])
