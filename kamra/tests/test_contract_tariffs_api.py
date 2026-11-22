"""Contract tariff endpoints, using the real role guard and bridge without a site."""

import importlib.util
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class PermissionDenied(Exception):
	pass


def throw(message, exc):
	raise exc(message)


class TestContractTariffsAPI(unittest.TestCase):
	def setUp(self):
		self.roles = ["Hotel Admin"]
		self.allowed_properties = {"Test Hotel"}
		self.script = None
		self.lookups = []
		self.calls = []
		self.whitelisted = {}
		self.frappe = SimpleNamespace(
			_=lambda text: text,
			whitelist=self.whitelist,
			get_roles=lambda: self.roles,
			throw=throw,
			PermissionError=PermissionDenied,
			local=SimpleNamespace(form_dict={}),
			response={},
			_dict=dict,
			db=SimpleNamespace(get_value=self.find_script),
			get_doc=self.get_script,
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
		return filters["api_method"] if self.script is not None else None

	def get_script(self, doctype, name):
		self.assertEqual(doctype, "Server Script")

		def execute():
			self.calls.append((name, dict(self.frappe.local.form_dict)))
			self.frappe.response["message"] = self.script

		return SimpleNamespace(execute_method=execute)

	def endpoint(self, name):
		self.assertIn(name, self.whitelisted, f"Missing tariff endpoint: {name}")
		return getattr(self.rates, name)

	def test_absent_list_script_returns_read_only_unsupported_payload(self):
		self.roles = ["Front Desk"]
		result = self.endpoint("list_contract_tariffs")("Test Hotel")
		self.assertEqual(result, {
			"supported": False, "property": "Test Hotel", "price_lists": [], "items": [],
			"can_propose": False, "can_approve": False})
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
