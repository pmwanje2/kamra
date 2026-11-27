"""Ownership is enforced at RPC, ORM, and background entry points."""
import ast
import inspect
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase


class TestNativeOwnership(IntegrationTestCase):
	def test_all_agent_rpcs_refuse_when_disabled(self):
		root = Path(__file__).resolve().parents[1]
		files = [root / "assistant.py", root / "whatsapp.py", *root.glob("agents_*.py"), *root.glob("mcp_*.py")]
		with patch.dict(frappe.conf, kamra_mcp_disabled=1):
			for file in files:
				for fn in ast.parse(file.read_text()).body:
					if not isinstance(fn, ast.FunctionDef) or not any(
						ast.unparse(d).startswith("frappe.whitelist(") for d in fn.decorator_list
					):
						continue
					method = getattr(import_module("kamra." + file.stem), fn.name)
					args = {p.name: "fixture" for p in inspect.signature(method).parameters.values()
					        if p.default is p.empty and p.kind == p.POSITIONAL_OR_KEYWORD}
					with self.subTest(method=method.__qualname__), self.assertRaisesRegex(frappe.ValidationError, "^KMR-OFF-001$"):
						method(**args)

	def test_outbound_does_not_dispatch(self):
		from kamra import agents_channels, whatsapp
		with patch.dict(frappe.conf, kamra_mcp_disabled=1), patch.object(frappe, "get_all", side_effect=AssertionError):
			self.assertFalse(whatsapp.has_connection("missing"))
			self.assertFalse(agents_channels.send_outbound("missing", "WhatsApp", "test", "test")["sent"])

	def test_disabled_flag_off_preserves_provider_catalog(self):
		from kamra.assistant import provider_presets
		with patch.dict(frappe.conf, kamra_mcp_disabled=0):
			self.assertTrue(provider_presets())

	def test_owned_pos_does_not_load_legacy_order(self):
		from kamra import pos, inventory
		with patch("kamra.erp_books.erp_pos_url", return_value="/pos"):
			for method, kwargs in [(pos.order_detail, {"order": "missing"}), (inventory.ingredients, {"property": "missing"})]:
				with self.subTest(method=method.__name__), self.assertRaises(frappe.PermissionError):
					method(**kwargs)

	def test_owned_fiscal_and_voucher_paths_refuse_before_lookup(self):
		from kamra import api, pricing, zatca
		with patch("kamra.fiscal.fiscal_bridge", return_value=True), patch("kamra.erp_bridge.erp_determines_tax", return_value=True):
			for method, args in [(api.gstr1_rows, ("2026-01-01", "2026-01-02")), (zatca.settings_for, ("missing",)),
			                     (pricing.consume_voucher_use, ("missing",))]:
				with self.subTest(method=method.__name__), self.assertRaises(frappe.PermissionError):
					method(*args)

	def test_direct_document_writes_are_fenced(self):
		with patch("kamra.erp_books.erp_pos_url", return_value="/pos"), patch("kamra.fiscal.fiscal_bridge", return_value=True), patch("kamra.erp_bridge.erp_determines_tax", return_value=True), patch("kamra.erp_books.erp_screens", return_value={"payables": {"label": "Purchases", "url": "/purchase"}}):
			for doctype in ("POS Order", "Menu Item", "Ingredient Stock", "Vendor Bill", "ZATCA Invoice", "Discount Voucher"):
				with self.subTest(doctype=doctype), self.assertRaises(frappe.PermissionError):
					frappe.new_doc(doctype).run_method("before_validate")
