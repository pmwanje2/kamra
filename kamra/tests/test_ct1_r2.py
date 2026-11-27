"""Regression tests at the tariff, ARI, audit and FX desk boundaries."""
from datetime import date, timedelta
from decimal import Decimal
from inspect import unwrap
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from kamra import channel_ari, ledger, night_audit, pricing


class ContractFinanceR2(UnitTestCase):
	def test_governed_rate_mismatch_holds_channel_price(self):
		day = "2026-11-27"
		answer = dict(is_tariff=1, agreed_rate=100, nightly=1, currency="USD", fx_rate=100, fx_date=day)
		def quote(property, room, start, end, adults, children, rate_plan):
			return pricing.contract_rate(property, room, rate_plan, start, end, adults, children)
		with patch.object(pricing, "property_currency", return_value="UGX"), \
				patch.object(pricing, "resolve_rate_plan_chain", return_value=[frappe._dict(name="P")]), \
				patch.object(pricing, "workflow_state_field", return_value="workflow_state"), \
				patch.object(pricing, "posting_fx_rate", return_value=(Decimal(200), day)), \
				patch("kamra.erp_bridge.bridge_call", return_value=answer), \
				patch.object(frappe.db, "exists", return_value=True), patch.object(frappe.db, "get_value", return_value=2), \
				patch.object(pricing, "quote_with_contract", side_effect=quote):
			with self.assertRaises(channel_ari.AriPriceHeld):
				channel_ari._plan_night_rate("Hotel", "Room", "P", day, "UGX")

	def test_late_open_ended_approval_has_a_nonempty_outbox_window(self):
		today = date(2026, 11, 27)
		start = today + timedelta(days=550)
		doc = frappe._dict(name="IP", price_list="USD", valid_from=start, valid_upto=None)
		with patch.object(channel_ari, "nowdate", return_value=today), patch.object(frappe, "get_meta"), \
				patch.object(pricing, "workflow_state_field", return_value=None), \
				patch.object(frappe, "get_all", return_value=["Hotel"]), patch.object(channel_ari, "enqueue_ari_change") as queue:
			channel_ari.enqueue_tariff_change(doc)
			_, lower, upper = queue.call_args.args
			self.assertEqual(channel_ari._clip(lower, upper),
			                 (str(start), str(today + timedelta(days=channel_ari.ARI_MAX_DAYS - 1))))

	def test_precheck_skips_free_night_and_manager_override(self):
		day = "2026-11-27"
		def stay(name, policy, agreed, nights=None):
			return frappe._dict(name=name, property="Hotel", fixed_rate=0, agreed_rate=agreed, contract_tariff={
				"fx_lock_policy": policy, "agreed_rate": 50, "tariff": {"property_currency": "UGX", "currency": "USD",
				"nightly": 2, "nights": nights or []}})
		free = stay("FREE", "Current Date", 50, [{"date": day, "nightly": 0}])
		override = stay("OVERRIDE", "Fixed at Check In", 75)
		paid = stay("PAID", "Current Date", 50)
		with patch.object(frappe, "get_all", side_effect=[[free, paid], [override]]), \
				patch.object(pricing, "posting_fx_status", return_value={"rate": None, "currency": "USD"}):
			check = night_audit.check_missing_posting_fx("Hotel", day)
		self.assertEqual([row["reservation"] for row in check["items"]], ["PAID"])
		self.assertIn("currency=USD", check["resolve"])

	def test_readers_can_inspect_but_cannot_set_rates(self):
		for role in ("Night Auditor", "Revenue Manager"):
			with self.subTest(role=role):
				self.assertIn(role, ledger.list_exchange_rates._kamra_roles)
				self.assertNotIn(role, ledger.upsert_exchange_rate._kamra_roles)
				self.assertNotIn(role, ledger.fetch_exchange_rates._kamra_roles)

	def test_desk_lists_pairs_without_an_exchange_row(self):
		def rows(dt, **kwargs):
			if dt == "Reservation":
				return [frappe._dict(contract_tariff={"tariff": {"currency": "EUR"}})]
			if dt == "Rate Plan":
				return [frappe._dict(name="Plan", currency="CAD")]
			return []
		with patch.object(ledger, "get_business_date", return_value="2026-11-27"), \
				patch.object(ledger, "home_currency", return_value="UGX"), patch.object(frappe.db, "exists", return_value=True), \
				patch.object(frappe, "get_all", side_effect=rows), patch("kamra.settings.list_setting", return_value=["GBP"]), \
				patch.object(pricing, "posting_fx_status", side_effect=lambda p, c, d: {"currency": c, "status": "Missing"}):
			out = unwrap(ledger.list_exchange_rates)("Hotel")
			filtered = unwrap(ledger.list_exchange_rates)("Hotel", currency="KWD")
		self.assertEqual({r["currency"] for r in out["posting_rates"]}, {"EUR", "CAD", "GBP"})
		self.assertEqual(filtered["posting_rates"], [{"currency": "KWD", "status": "Missing"}])
		self.assertTrue(all(r["status"] == "Missing" for r in out["posting_rates"]))
