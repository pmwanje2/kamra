"""Exercise the Server Script transport without running a Frappe site."""

import importlib.util
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class TestERPBridge(unittest.TestCase):
	def setUp(self):
		spec = importlib.util.find_spec("kamra.erp_bridge")
		self.assertIsNotNone(spec, "the shared Server Script bridge is missing")
		self.bridge = importlib.util.module_from_spec(spec)
		self.form = {"outer": "request"}
		self.message = {"outer": "response"}
		self.frappe = SimpleNamespace(
			local=SimpleNamespace(form_dict=self.form),
			response={"message": self.message},
			_dict=dict,
			db=SimpleNamespace(get_value=self.find_script),
			get_doc=self.get_script,
		)
		self.enabled = True
		self.execute = self.respond
		with patch.dict(sys.modules, {"frappe": self.frappe}):
			spec.loader.exec_module(self.bridge)

	def find_script(self, doctype, filters):
		self.assertEqual(doctype, "Server Script")
		self.assertEqual(filters, {"script_type": "API", "api_method": "fixture.read", "disabled": 0})
		return "Enabled Fixture Script" if self.enabled else None

	def get_script(self, doctype, name):
		self.assertTrue(self.enabled)
		self.assertEqual((doctype, name), ("Server Script", "Enabled Fixture Script"))
		return SimpleNamespace(execute_method=self.execute)

	def respond(self):
		self.assertEqual(self.frappe.local.form_dict, {"property": "Test Hotel"})
		self.assertNotIn("message", self.frappe.response)
		self.frappe.response["message"] = {"rate": 18}

	def assert_request_restored(self):
		self.assertIs(self.frappe.local.form_dict, self.form)
		self.assertIs(self.frappe.response["message"], self.message)

	def test_enabled_script_receives_form_and_returns_only_its_message(self):
		self.assertEqual(self.bridge.bridge_call("fixture.read", property="Test Hotel"), {"rate": 18})
		self.assert_request_restored()

	def test_absent_or_disabled_script_returns_none(self):
		self.enabled = False
		self.assertIsNone(self.bridge.bridge_call("fixture.read", property="Test Hotel"))
		self.assert_request_restored()

	def test_script_failure_restores_form_and_message(self):
		def fail():
			self.respond()
			raise RuntimeError("fixture refuses")

		self.execute = fail
		with self.assertRaisesRegex(RuntimeError, "fixture refuses"):
			self.bridge.bridge_call("fixture.read", property="Test Hotel")
		self.assert_request_restored()
