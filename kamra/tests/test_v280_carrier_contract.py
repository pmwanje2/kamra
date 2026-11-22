"""Site-less compatibility checks for the v2.8.0 carrier.

Bench tests cover Frappe's runtime permissions and database integration.
"""

import ast
import hashlib
import json
import re
import runpy
import sys
import unittest
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

KAMRA = Path(__file__).resolve().parents[1]


def functions(filename):
	return {
		node.name: node
		for node in ast.parse((KAMRA / filename).read_text(encoding="utf-8")).body
		if isinstance(node, ast.FunctionDef)
	}


class TestV280CarrierContract(unittest.TestCase):
	def test_foreign_payment_amount_returns_bound_quote(self):
		ledger = functions("ledger.py")
		payment_rate = ledger["payment_rate"]
		self.assertIn("amount", [arg.arg for arg in payment_rate.args.args])

		# Execute the real calculation without importing the bench-bound ledger.
		row = SimpleNamespace(rate_date="2026-11-21", buy_rate=3650.0, max_exchange=500.0)
		utils = ModuleType("frappe.utils")
		utils.add_days = lambda value, days: str(date.fromisoformat(value) + timedelta(days=days))
		namespace = {
			"frappe": SimpleNamespace(get_all=lambda *args, **kwargs: [row]),
			"home_currency": lambda property: "UGX",
			"get_business_date": lambda property: "2026-11-21",
			"_settlement_window": lambda: 1,
			"flt": float,
			"Decimal": Decimal,
			"ROUND_HALF_UP": ROUND_HALF_UP,
			"hashlib": hashlib,
			"json": json,
		}
		payment_rate.decorator_list = []
		module = ast.Module(
			body=[ledger[name] for name in ("_money", "round_cents", "fx_to_home", "payment_rate")],
			type_ignores=[],
		)
		with patch.dict(sys.modules, {"frappe.utils": utils}):
			exec(compile(module, str(KAMRA / "ledger.py"), "exec"), namespace)
			quote = namespace["payment_rate"]("Test Hotel", "USD", amount=130)
			other = namespace["payment_rate"]("Test Hotel", "USD", amount=131)
		self.assertEqual(quote["home_amount"], 474500)
		self.assertRegex(quote["quote_id"], r"^[0-9a-f]{64}$")
		self.assertNotEqual(quote["quote_id"], other["quote_id"])

	def test_erp_bridge_entry_points_are_whitelisted(self):
		for filename, name in (
			("api.py", "erp_correction_context"),
			("authz.py", "list_supervisors"),
			("authz.py", "verify_supervisor_approval"),
		):
			with self.subTest(method=name):
				function = functions(filename).get(name)
				self.assertIsNotNone(function, f"{name} is missing from {filename}")
				self.assertIn(
					"frappe.whitelist",
					[ast.unparse(d.func) for d in function.decorator_list if isinstance(d, ast.Call)],
				)

	def test_closed_folio_line_target_is_preserved(self):
		self.assertIn("open_line_target", tuple(functions("folio.py")))

	def test_password_reset_stays_with_central_sign_in(self):
		for redirect in runpy.run_path(str(KAMRA / "hooks.py"))["website_redirects"]:
			self.assertIsNone(re.match(redirect["source"], "/update-password"), redirect)

	def test_upstream_night_prices_survive_walk_in(self):
		api = functions("api.py")
		for method in ("get_quote", "create_booking", "walk_in"):
			with self.subTest(method=method):
				args = api[method].args
				self.assertIn("night_prices", [arg.arg for arg in args.args + args.kwonlyargs])
		self.assertTrue(
			any(
				node.arg == "night_prices" and isinstance(node.value, ast.Name) and node.value.id == "night_prices"
				for call in ast.walk(api["walk_in"])
				if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "create_booking"
				for node in call.keywords
			),
			"walk_in must forward night_prices to create_booking",
		)
