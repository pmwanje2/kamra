"""P18 R2 host-independent regression checks for controller and install wiring."""

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[2]


class TestP18R2Regressions(unittest.TestCase):
	def test_property_currency_controls_document_precision(self):
		for doctype, controller in (("folio", "Folio"), ("folio_payment", "FolioPayment")):
			path = ROOT / "kamra/kamra/doctype" / doctype / f"{doctype}.py"
			tree = ast.parse(path.read_text())
			doc = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == controller)
			self.assertIn("precision", [node.name for node in doc.body if isinstance(node, ast.FunctionDef)])

	def test_cashier_validates_explicit_pin_even_during_unlock_window(self):
		path = ROOT / "kamra/authz.py"
		function = next(
			node for node in ast.parse(path.read_text()).body
			if isinstance(node, ast.FunctionDef) and node.name == "require_cashier_pin"
		)
		check = Mock()
		cache = Mock()
		cache.get_value.return_value = 1
		frappe = SimpleNamespace(
			db=SimpleNamespace(get_value=lambda *args: 1),
			flags=SimpleNamespace(),
			get_roles=lambda: ["Front Desk"],
			session=SimpleNamespace(user="clerk@example.test"),
			cache=cache,
		)
		env = {"frappe": frappe, "_pin_policy": lambda property: (3, 15), "_check_user_pin": check}
		exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), env)
		require_pin = env["require_cashier_pin"]

		require_pin("Test Hotel")
		check.assert_not_called()
		require_pin("Test Hotel", "provided-pin")
		check.assert_called_once_with("clerk@example.test", "provided-pin", "Test Hotel")

	def test_guest_window_inherits_the_deposit_customer_from_window_one(self):
		path = ROOT / "kamra/folio.py"
		function = next(
			node for node in ast.parse(path.read_text()).body
			if isinstance(node, ast.FunctionDef) and node.name == "open_window"
		)
		for primary_customer, expected in (("Deposit Customer", "Deposit Customer"), (None, "Walk-in")):
			with self.subTest(primary_customer=primary_customer):
				doc = SimpleNamespace(
					name="FOLIO-W2",
					meta=SimpleNamespace(has_field=lambda field: field == "customer"),
					insert=Mock(),
				)
				get_value = Mock(side_effect=[
					SimpleNamespace(property="Test Hotel", guest="Guest"),
					primary_customer,
				])
				frappe = SimpleNamespace(
					db=SimpleNamespace(get_value=get_value), get_doc=Mock(return_value=doc),
					get_cached_doc=Mock(return_value={"walk_in_customer": "Walk-in"}),
				)
				env = {
					"frappe": frappe, "_window_key": lambda reservation, group: {"reservation": reservation},
					"_recalculate": Mock(), "now_datetime": lambda: None,
				}
				exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), env)
				self.assertEqual(env["open_window"](reservation="RES-1", window_no=2), "FOLIO-W2")
				self.assertEqual(getattr(doc, "customer", None), expected)
				get_value.assert_called_with("Folio", {"reservation": "RES-1", "window_no": 1},
				                             "customer", order_by="creation desc")

	def test_marketplace_allows_only_annotated_fixture_identity_switches(self):
		for filename in ("test_p18_k2_receipt_transfer.py", "test_p18_routing_crud.py"):
			lines = (ROOT / "kamra/tests" / filename).read_text().splitlines()
			for line in lines:
				if line.lstrip().startswith("frappe.set_user("):
					self.assertIn("nosemgrep: frappe-setuser", line)
