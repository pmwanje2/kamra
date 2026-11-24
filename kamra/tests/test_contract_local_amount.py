"""One conversion rule for a contract amount in the posting currency.

OPERA Cloud converts a foreign Rate Code amount at the exchange rate and
rounds it as the rate code rounds; SAP converts a PR00 condition at the
OB08 rate and applies the rounding rule (DIFF). The engine and every screen
that shows a local equivalent call the same helper."""

from __future__ import annotations

from decimal import Decimal

from kamra.tests.rate_matrix_harness import PRECISION, RateMatrixHarness


class ContractLocalAmountTest(RateMatrixHarness):
	def local(self, plan, nightly="130", fx="3648.2", home="UGX"):
		return self.pricing.contract_local_amount(plan, Decimal(nightly), Decimal(fx), home)

	def test_the_contract_amount_converts_and_rounds_to_the_plan_step(self):
		# 130 USD at 3,648.2 is 474,266 UGX before the plan rounds it
		plan = self.plan("CONTRACT", rounding_step=1000)
		self.assertEqual(self.local(plan), Decimal("474000"))
		plan.rounding_mode = "Up"
		self.assertEqual(self.local(plan), Decimal("475000"))
		plan.rounding_mode = "Down"
		self.assertEqual(self.local(plan), Decimal("474000"))
		self.assertIsInstance(self.local(plan), Decimal)

	def test_a_zero_step_rounds_to_the_posting_currency_minor_unit(self):
		# 130 x 3,648.27 = 474,275.1; UGX has no minor unit
		plan = self.plan("CONTRACT", rounding_step=0)
		self.assertEqual(PRECISION["UGX"], 0)
		self.assertEqual(self.local(plan, fx="3648.27"), Decimal("474275"))
		self.assertEqual(self.local(plan, nightly="130.125", fx="1", home="USD"), Decimal("130.13"))

	def test_an_unset_rounding_mode_is_nearest(self):
		plan = self.plan("CONTRACT", rounding_step=1000, rounding_mode=None)
		self.assertEqual(self.local(plan, fx="3650"), Decimal("475000"))
		self.assertEqual(self.local(plan), Decimal("474000"))

	def test_the_plan_may_be_a_plain_mapping(self):
		self.assertEqual(self.local({"rounding_step": 5000, "rounding_mode": "Nearest"}), Decimal("475000"))
		self.assertEqual(self.local({}), Decimal("474266"))
