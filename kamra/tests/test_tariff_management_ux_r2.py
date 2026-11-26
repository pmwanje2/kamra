"""Round two regression coverage for first rates above five adults."""

from types import SimpleNamespace
from unittest.mock import Mock

from kamra.tests.test_contract_tariffs_api import ContractTariffHarness, Refused


class TestTariffManagementUXR2(ContractTariffHarness):
	def setUp(self):
		super().setUp()
		values = {
			"property": "Test Hotel", "disabled": 0, "name": "Test Hotel-VILLA",
			"room_type_name": "Garden Villa", "base_occupancy": 6, "adults_capacity": 7,
		}
		self.room = Mock(**values)
		self.room.get.side_effect = values.get
		item = Mock(disabled=0, is_sales_item=1, is_stock_item=0)
		item.name = "VILLA"
		get_script = self.frappe.get_doc
		self.frappe.get_doc = lambda dt, name, **kw: (
			self.room if dt == "Room Type" else item if dt == "Item" else get_script(dt, name)
		)
		self.frappe.get_meta = lambda dt: SimpleNamespace(has_field=lambda field: True)
		self.scripts = {"hospitality_tariffs.propose_tariff": {"name": "IP-NEW", "workflow_state": "Draft"}}

	def propose(self, tier):
		return self.endpoint("propose_contract_tariff")(
			"Test Hotel", "Contract USD", "VILLA", 125, "2026-11-25",
			room_type="Test Hotel-VILLA", occupancy_tier=tier,
		)

	def test_first_proposal_supports_six_adult_base_occupancy(self):
		self.assertEqual(self.propose("6")["name"], "IP-NEW")
		self.room.set.assert_called_once_with("custom_tariff_item", "VILLA")
		self.assertEqual(self.calls[-1][1]["item_code"], "VILLA")

	def test_proposal_rejects_a_tier_beyond_room_capacity(self):
		with self.assertRaises(Refused):
			self.propose("8")
		self.room.set.assert_not_called()
		self.assertEqual(self.calls, [])

	def test_effective_policy_with_loaded_catalog(self):
		from unittest.mock import patch
		import sys
		pricing = SimpleNamespace(RATE_PLAN_CHAIN_LIMIT=8)
		settings = SimpleNamespace(get_setting=lambda *_: False)
		parent = {"name": "Published", "custom_tariff_price_list": "USD"}
		child = {"name": "Derived", "base_rate_plan": "Published"}
		with patch.dict(sys.modules, {"kamra.pricing": pricing, "kamra.settings": settings}):
			required = self.rates.effective_rate_plan_required
			self.assertFalse(required("Test Hotel", []))
			self.assertTrue(required("Test Hotel", [parent, child]))
			self.assertFalse(required("Test Hotel", [parent, {"name": "Card"}]))
