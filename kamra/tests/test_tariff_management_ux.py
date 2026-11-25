"""Regression coverage for the tariff directory and governed rate query."""

import sys
from types import SimpleNamespace
from unittest.mock import patch

from kamra.tests.test_contract_tariffs_api import ContractTariffHarness


class TestTariffManagementUX(ContractTariffHarness):
	def test_directory_links_venue_to_local_room_type_and_exposes_unpriced_targets(self):
		self.room_types = [
			{"name": "Test Hotel-CONFERENCE", "room_type_name": "Conference Room", "room_type_code": "CONFERENCE"},
			{"name": "Test Hotel-STD", "room_type_name": "Standard", "room_type_code": "STD"},
		]
		self.script = {
			"supported": True, "can_propose": True,
			"price_lists": [{"name": "Contract USD"}],
			"items": [{"item_code": "COURTYARD", "item_name": "Conference Room Hire - Courtyard",
			           "price_list": "Contract USD", "room_type": "Conference Room", "category": "Venue",
			           "active": {"rate": 100}}],
		}
		result = self.endpoint("list_contract_tariffs")("Test Hotel")
		item = result["items"][0]
		with self.subTest("venue identity"):
			self.assertEqual(item.get("room_type_id"), "Test Hotel-CONFERENCE")
			self.assertEqual(item.get("room_type_code"), "CONFERENCE")
		with self.subTest("available targets"):
			self.assertIn("COURTYARD", {row["item_code"] for row in result.get("available_items", [])})
		with self.subTest("room without a tariff item"):
			self.assertIn("Test Hotel-STD", {row["name"] for row in result["unpriced_room_types"]})

	def test_rate_query_uses_plan_configuration_even_when_a_tariff_is_missing(self):
		rows = [
			{"name": "BAR", "rate_plan_name": "Best available", "code": "BAR", "modifier_type": "Fixed",
			 "modifier_value": 0, "is_default": 1, "discount_eligible": 1, "custom_tariff_price_list": "USD"},
			{"name": "DER", "rate_plan_name": "Derived", "code": "DER", "modifier_type": "Fixed",
			 "modifier_value": 0, "is_default": 0, "discount_eligible": 1, "base_rate_plan": "BAR"},
		]
		class AttrDict(dict):
			__getattr__ = dict.__getitem__
		get_plans = lambda dt, **kw: [AttrDict({field: row.get(field) for field in kw["fields"]}) for row in rows]
		class TariffRefused(Exception):
			pass
		def quote(*args, **kwargs):
			if args[7]:
				raise TariffRefused("No approved tariff for this stay.")
			return {"room_total": 410619, "amount_after_tax": 410619, "discount": 0}
		self.frappe.get_doc = lambda *args: SimpleNamespace(property="Test Hotel")
		self.frappe.get_all = get_plans
		self.frappe.get_meta = lambda dt: SimpleNamespace(has_field=lambda field: True)
		self.rates.date_diff = lambda *args: 0
		self.rates.add_days = lambda day, count: day
		self.rates.explain_night = lambda *args, **kwargs: {}
		modules = {
			"kamra.pricing": SimpleNamespace(
				DEFAULT_FX_LOCK="Fixed at Reservation", RATE_PLAN_CHAIN_LIMIT=12, ContractRateRefused=TariffRefused,
				negotiated_rate_plan=lambda *args: None, quote_with_contract=quote,
				rates_include_tax=lambda prop: False, small_group_allows_stay=lambda *args: True,
				validate_voucher=None,
			),
			"kamra.settings": SimpleNamespace(get_setting=lambda *args: 0),
			"kamra.restrictions": SimpleNamespace(evaluate_stay_restrictions=None),
			"kamra.api": SimpleNamespace(discount_options=lambda prop: {}),
		}
		with patch.dict(sys.modules, modules):
			query = lambda: self.endpoint("rate_options")("Test Hotel", "STD", "2026-11-25", "2026-11-25")
			result = query()
			with self.subTest("governed plans with unavailable rates"):
				self.assertIs(result.get("all_active_plans_tariff"), True)
				self.assertEqual(result.get("default_rate_plan"), "BAR")
				self.assertIs(result["rate_plan_required"], False)
			rows[1].pop("base_rate_plan")
			with self.subTest("mixed sources"):
				self.assertIs(query().get("all_active_plans_tariff"), False)
			rows.clear()
			with self.subTest("no active plans"):
				self.assertIs(query().get("all_active_plans_tariff"), False)
