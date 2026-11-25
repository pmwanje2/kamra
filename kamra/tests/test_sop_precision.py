"""Dynamic 0/2/3dp precision and currency-epsilon regression tests for kamra/sop.py.

Siteless: loads the real kamra/sop.py and kamra/localization/__init__.py with
lightweight Frappe/Babel stubs so it runs under standard python3 -m unittest
as well as inside a Frappe bench.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

KAMRA = Path(__file__).resolve().parents[1]

ISO_PRECISION = {
	"UGX": 0,
	"JPY": 0,
	"USD": 2,
	"EUR": 2,
	"INR": 2,
	"KWD": 3,
	"BHD": 3,
	"OMR": 3,
}


def _module(name: str, **attrs) -> ModuleType:
	mod = ModuleType(name)
	vars(mod).update(attrs)
	return mod


def _load_sop_with_localization():
	"""Load real kamra/localization/__init__.py and kamra/sop.py siteless."""
	fake_frappe = _module(
		"frappe",
		_=lambda s: s,
		whitelist=lambda *a, **kw: (a[0] if a and callable(a[0]) else (lambda fn: fn)),
		local=SimpleNamespace(db=None),
		db=SimpleNamespace(
			table_exists=lambda _dt: False,
			exists=lambda *_a, **_kw: False,
			get_value=lambda *_a, **_kw: None,
		),
		get_cached_value=lambda *_a, **_kw: None,
		get_all=lambda *_a, **_kw: [],
		parse_json=lambda s: s,
		throw=lambda msg, *a, **kw: (_ for _ in ()).throw(RuntimeError(msg)),
		flags=SimpleNamespace(in_import=False),
	)
	fake_utils = _module("frappe.utils", flt=lambda v=0: float(v or 0))
	fake_authz = _module("kamra.authz", require_roles=lambda *_r, **_kw: (lambda fn: fn))
	fake_babel = _module("babel")
	fake_babel_numbers = _module(
		"babel.numbers",
		get_currency_precision=lambda code: ISO_PRECISION.get((code or "").upper(), 2),
	)
	fake_pricing = _module(
		"kamra.pricing",
		_minor_unit=lambda code: ISO_PRECISION.get((code or "").upper(), 2),
	)

	stubs = {
		"frappe": fake_frappe,
		"frappe.utils": fake_utils,
		"kamra.authz": fake_authz,
		"babel": fake_babel,
		"babel.numbers": fake_babel_numbers,
		"kamra.pricing": fake_pricing,
	}

	with patch.dict(sys.modules, stubs):
		loc_spec = importlib.util.spec_from_file_location(
			"kamra.localization", KAMRA / "localization" / "__init__.py"
		)
		loc_mod = importlib.util.module_from_spec(loc_spec)
		sys.modules["kamra.localization"] = loc_mod
		loc_spec.loader.exec_module(loc_mod)

		sop_spec = importlib.util.spec_from_file_location("sop_precision_under_test", KAMRA / "sop.py")
		sop_mod = importlib.util.module_from_spec(sop_spec)
		sop_spec.loader.exec_module(sop_mod)
		return sop_mod, loc_mod, stubs


class SopPrecisionTest(unittest.TestCase):
	def setUp(self):
		self.sop, self.loc, self._stubs = _load_sop_with_localization()
		self._patcher = patch.dict(
			sys.modules,
			{**self._stubs, "kamra.localization": self.loc},
		)
		self._patcher.start()

	def tearDown(self):
		self._patcher.stop()

	def _step(self, step_key: str, *, is_blocking: int = 1) -> dict:
		return {
			"step_key": step_key,
			"step_type": "action",
			"title": step_key,
			"instruction": "Default instruction.",
			"prompt_to_guest": "",
			"is_blocking": is_blocking,
			"sequence": 10,
		}

	def test_format_money_respects_currency_precision(self):
		self.assertEqual(self.sop.format_money("KWD ", 192.385, currency="KWD"), "KWD 192.385")
		self.assertEqual(self.sop.format_money("UGX ", 12500, currency="UGX"), "UGX 12,500")
		self.assertEqual(self.sop.format_money("USD ", 12.5, currency="USD"), "USD 12.50")
		self.assertEqual(self.sop.format_money("$", 12.5), "$12.50")
		self.assertEqual(self.sop.format_money("KWD ", -5.125, currency="KWD"), "-KWD 5.125")

	def test_collect_settle_formats_kwd_ugx_and_usd_precision(self):
		step = self._step("collect_settle")

		kwd = self.sop.evaluate_checkout_step(
			step,
			{
				"currency": "KWD",
				"currency_symbol": "KWD ",
				"totals": {"owed": 192.385},
			},
		)
		self.assertEqual(kwd["tip"], "Collect KWD 192.385 before departure.")
		self.assertEqual(kwd["state"], "blocked")

		ugx = self.sop.evaluate_checkout_step(
			step,
			{
				"currency": "UGX",
				"currency_symbol": "UGX ",
				"totals": {"owed": 12500},
			},
		)
		self.assertEqual(ugx["tip"], "Collect UGX 12,500 before departure.")
		self.assertEqual(ugx["state"], "blocked")

		usd = self.sop.evaluate_checkout_step(
			step,
			{
				"currency": "USD",
				"currency_symbol": "USD ",
				"totals": {"owed": 12.5},
			},
		)
		self.assertEqual(usd["tip"], "Collect USD 12.50 before departure.")
		self.assertEqual(usd["state"], "blocked")

		# Legacy context without explicit currency key still infers 3dp from ISO symbol "KWD "
		kwd_symbol_only = self.sop.evaluate_checkout_step(
			step,
			{
				"currency_symbol": "KWD ",
				"totals": {"owed": 192.385},
			},
		)
		self.assertEqual(kwd_symbol_only["tip"], "Collect KWD 192.385 before departure.")

	def test_collect_settle_treats_kwd_0_005_owed_refund_and_city_ledger_as_nonzero(self):
		step = self._step("collect_settle")

		owed = self.sop.evaluate_checkout_step(
			step,
			{
				"currency": "KWD",
				"currency_symbol": "KWD ",
				"totals": {"owed": 0.005},
				"actions": {"can_checkout": False},
			},
		)
		self.assertEqual(owed["tip"], "Collect KWD 0.005 before departure.")
		self.assertEqual(owed["state"], "blocked")

		refund = self.sop.evaluate_checkout_step(
			step,
			{
				"currency": "KWD",
				"currency_symbol": "KWD ",
				"totals": {"owed": 0, "refund_due": 0.005},
				"actions": {"can_checkout": False},
			},
		)
		self.assertEqual(refund["tip"], "Refund KWD 0.005 before departure.")
		self.assertEqual(refund["state"], "blocked")

		city = self.sop.evaluate_checkout_step(
			step,
			{
				"currency": "KWD",
				"currency_symbol": "KWD ",
				"totals": {"owed": 0, "refund_due": 0, "city_ledger_eligible": 0.005},
				"actions": {"can_city_ledger": True, "can_checkout": True},
			},
		)
		self.assertEqual(
			city["tip"],
			"KWD 0.005 can move to the city ledger (company has credit). Confirm, then check out.",
		)
		self.assertEqual(city["state"], "todo")

		# Sub-minor KWD dust (<= 0.0005) still resolves as settled
		dust = self.sop.evaluate_checkout_step(
			step,
			{
				"currency": "KWD",
				"currency_symbol": "KWD ",
				"totals": {"owed": 0.0004, "refund_due": 0, "city_ledger_eligible": 0},
				"actions": {"can_checkout": True},
			},
		)
		self.assertEqual(dust["tip"], "Balances are clear: ready to check out.")
		self.assertEqual(dust["state"], "done")

	def test_other_checkout_steps_use_dynamic_precision_and_epsilon(self):
		ctx = {
			"currency": "KWD",
			"currency_symbol": "KWD ",
			"folios": [{"folio": "FOL-1"}],
			"totals": {"charges": 192.385, "paid": 100.005},
			"groups": {
				"Restaurant": {"amount": 14.125},
				"Discounts": {"amount": -5.125},
				"Damages": {"amount": 0},
			},
			"deposit": {"collected_amount": 0.005, "balance": 0.005},
		}

		bills = self.sop.evaluate_checkout_step(self._step("review_bills"), ctx)
		self.assertEqual(bills["tip"], "Review 1 open folio(s). Total charges KWD 192.385, paid KWD 100.005.")

		rest = self.sop.evaluate_checkout_step(self._step("verify_restaurant"), ctx)
		self.assertEqual(rest["tip"], "Confirm KWD 14.125 in restaurant / minibar charges with the guest.")

		damages = self.sop.evaluate_checkout_step(self._step("check_damages"), ctx)
		self.assertEqual(
			damages["tip"],
			"Security deposit KWD 0.005 is held (KWD 0.005 refundable). Check the room; withhold damage or refund.",
		)
		self.assertEqual(damages["state"], "todo")

		discounts = self.sop.evaluate_checkout_step(self._step("confirm_discounts"), ctx)
		self.assertEqual(discounts["tip"], "Confirm discount / voucher / allowance lines totaling -KWD 5.125.")

		farewell = self.sop.evaluate_checkout_step(self._step("farewell"), ctx)
		self.assertEqual(farewell["tip"], "Wish farewell and return the remaining deposit of KWD 0.005.")

	def test_checkin_cancel_walkin_deposit_and_early_late_evaluators_respect_kwd_precision(self):
		# Check-in
		ci_ctx = {
			"currency": "KWD",
			"currency_symbol": "KWD ",
			"billing": {"payer_label": "The guest pays", "balance_due": 0.005},
			"deposit": {"applies": True, "satisfied": False, "security_due": 0.005, "advance_due": 0.005},
		}
		self.assertEqual(
			self.sop.evaluate_checkin_step(self._step("billing_check"), ci_ctx)["tip"],
			"The guest pays. Still to pay: KWD 0.005.",
		)
		self.assertEqual(
			self.sop.evaluate_checkin_step(self._step("security_deposit"), ci_ctx)["tip"],
			"Still to take: security deposit KWD 0.005, advance KWD 0.005.",
		)

		# Cancel
		cancel_ctx = {
			"currency": "KWD",
			"currency_symbol": "KWD ",
			"preview": {
				"estimated_fee": 0.005,
				"inside_window": True,
				"fee_basis": "First Night",
				"days_before_arrival": 1,
			},
		}
		self.assertEqual(
			self.sop.evaluate_cancel_step(self._step("check_policy"), cancel_ctx)["tip"],
			"1 day(s) before arrival: inside the fee window. Estimated fee KWD 0.005 (First Night).",
		)
		self.assertEqual(
			self.sop.evaluate_cancel_step(self._step("explain_fee"), {**cancel_ctx, "waive": True})["tip"],
			"Fee of KWD 0.005 will be WAIVED: manager call, logged on the stay. Tell the guest cancellation is free.",
		)

		# Walk-in
		walkin_ctx = {
			"currency": "KWD",
			"currency_symbol": "KWD ",
			"amount_after_tax": 0.005,
			"collect_amount": 0.005,
			"meal_plan_label": "BB",
		}
		self.assertEqual(
			self.sop.evaluate_walkin_step(self._step("confirm_rate"), walkin_ctx)["tip"],
			"Confirm with the guest: KWD 0.005 · BB.",
		)
		self.assertEqual(
			self.sop.evaluate_walkin_step(self._step("collect_payment"), walkin_ctx)["tip"],
			"Collecting KWD 0.005 now, then check in and hand keys.",
		)

		# Deposit
		dep_ctx = {
			"currency": "KWD",
			"currency_symbol": "KWD ",
			"deposit": {"pct": 50, "expected": 0.005, "paid": 0.005, "due": 0.005, "can_take": True},
		}
		self.assertEqual(
			self.sop.evaluate_deposit_step(self._step("quote_expected"), dep_ctx)["tip"],
			"Policy deposit KWD 0.005 (50% of stay). Still due KWD 0.005.",
		)
		self.assertEqual(
			self.sop.evaluate_deposit_step(self._step("collect_or_link"), dep_ctx)["tip"],
			"Collect KWD 0.005 at the desk or send a payment link.",
		)
		self.assertEqual(
			self.sop.evaluate_deposit_step(self._step("remind_balance"), dep_ctx)["tip"],
			"Still due KWD 0.005 before arrival (or at check-in). Tell the guest clearly.",
		)
		paid_ctx = {
			"currency": "KWD",
			"currency_symbol": "KWD ",
			"deposit": {"expected": 0.005, "paid": 0.005, "due": 0, "can_take": False},
		}
		self.assertEqual(
			self.sop.evaluate_deposit_step(self._step("collect_or_link"), paid_ctx)["tip"],
			"Deposit secured (KWD 0.005 received).",
		)
		self.assertEqual(
			self.sop.evaluate_deposit_step(self._step("confirm_receipt"), paid_ctx)["tip"],
			"Confirm the guest has a receipt / link confirmation for KWD 0.005.",
		)

		# Early check-in / late checkout
		el_ctx = {
			"currency": "KWD",
			"currency_symbol": "KWD ",
			"kind": "late_checkout",
			"amount": 0.005,
			"charge_type": "Late Checkout",
		}
		self.assertEqual(
			self.sop.evaluate_early_late_step(self._step("agree_fee"), el_ctx)["tip"],
			"Agreed fee KWD 0.005 for late checkout. Confirm with the guest before posting.",
		)
		self.assertEqual(
			self.sop.evaluate_early_late_step(self._step("post_fee"), el_ctx)["tip"],
			"Post KWD 0.005 as Late Checkout on the folio.",
		)
		el_quote_ctx = {
			"currency": "KWD",
			"currency_symbol": "KWD ",
			"kind": "late_checkout",
			"nightly_rate": 100.006,
		}
		self.assertEqual(
			self.sop.evaluate_early_late_step(self._step("agree_fee"), el_quote_ctx)["tip"],
			"Quote free, half night (KWD 50.003), or full night (KWD 100.006) for late checkout.",
		)

	def test_sop_has_no_hardcoded_money_epsilons_and_contexts_pass_currency(self):
		sop_src = (KAMRA / "sop.py").read_text(encoding="utf-8")
		tree = ast.parse(sop_src)
		bad_literals = [
			node.lineno
			for node in ast.walk(tree)
			if isinstance(node, ast.Constant)
			and isinstance(node.value, float)
			and node.value in (0.009, 0.005)
		]
		self.assertEqual(
			bad_literals,
			[],
			f"kamra/sop.py still contains hardcoded 0.009/0.005 money epsilon(s) on lines {bad_literals}",
		)

		checkout_src = (KAMRA / "checkout.py").read_text(encoding="utf-8")
		self.assertIn('"currency": currency', checkout_src)
		self.assertIn('"currency_decimals": display_places', checkout_src)

		checkin_src = (KAMRA / "checkin.py").read_text(encoding="utf-8")
		self.assertIn('"currency": currency', checkin_src)
		self.assertIn('"currency_decimals": display_places', checkin_src)


if __name__ == "__main__":
	unittest.main()
