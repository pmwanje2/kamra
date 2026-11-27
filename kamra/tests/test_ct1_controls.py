"""Contract tariffs, finance readiness and operator route contracts."""
from pathlib import Path
import unittest
import sys
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from kamra.tests.test_contract_tariffs_api import ContractTariffHarness

ROOT = Path(__file__).resolve().parents[2]


class Decisions(ContractTariffHarness):
	def test_governed_fx_ignores_same_day_draft_and_rejection(self):
		utils = sys.modules["frappe.utils"]
		utils.nowdate = lambda: "2026-11-27"
		utils.flt = float
		utils.getdate = lambda day: date.fromisoformat(str(day))
		utils.add_days = lambda day, n: day + timedelta(days=n)
		day = "2026-11-27"
		with patch.dict(sys.modules, {"kamra.business_date": SimpleNamespace(get_business_date=lambda _: day),
				"kamra.localization": SimpleNamespace(default_currency=None, pack_for=None)}):
			pricing = self.load_module("kamra.pricing")
			self.frappe.db.exists = lambda dt, filters: True
			self.frappe.db.get_value = lambda *args: "workflow_state"
			rows = [SimpleNamespace(date=day, exchange_rate=n, workflow_state=state)
			        for n, state in [(100, "Approved"), (500, "Draft"), (600, "Rejected")]]
			self.frappe.get_all = lambda dt, fields, filters: [
				r for r in rows if r.workflow_state == filters.get("workflow_state", r.workflow_state)]
			with patch.object(pricing, "property_currency", return_value="UGX"), patch.object(pricing, "_fx_stale_window", return_value=1):
				self.assertEqual(pricing.posting_fx_rate("Test Hotel", "USD"), (Decimal(100), day))
				rows.pop(0)
				self.assertIsNone(pricing.posting_fx_rate("Test Hotel", "USD"))

	def test_discard_reason(self):
		self.script = {"discarded": 1}
		self.endpoint("discard_contract_tariff")("IP", reason="Duplicate")
		self.assertEqual(self.calls[-1][1]["reason"], "Duplicate")

	def test_bulk_reason(self):
		self.script = {"count": 2}
		self.endpoint("bulk_discard_contract_tariffs")(["A", "B"], reason="Duplicate")
		self.assertEqual(self.calls[-1][1]["reason"], "Duplicate")


class ControlContracts(unittest.TestCase):
	def requires(self, file, *terms):
		source = (ROOT / file).read_text()
		for term in terms:
			with self.subTest(file=file, term=term):
				self.assertTrue(term in source, term)

	def test_exchange_workflow(self):
		self.requires("kamra/pricing.py", "workflow_state", "Currency Exchange")

	def test_tariff_event(self):
		from kamra.hooks import doc_events
		self.assertIn("Item Price", doc_events)

	def test_desk_projection(self):
		self.requires("kamra/ledger.py", '"posting_rates"')
		self.requires("frontend/src/screens/CurrencyDesk.tsx", "posting_rates", "stale_days")
		self.assertNotIn('buy: "83"', (ROOT / "frontend/src/screens/CurrencyDesk.tsx").read_text())

	def test_audit_projection(self):
		self.requires("kamra/night_audit.py", '"missing_posting_fx"', '"/cashier/fx"')

	def test_config_links(self):
		files = ["ConfiguredScreen.tsx", "configs.ts", "../components/ResourceScreen.tsx"]
		source = "".join((ROOT / "frontend/src/screens" / f).read_text() for f in files)
		for term in ("custom_tariff_price_list", "external_currency", "custom_erp_item", "/tariffs?customer=", "/tariffs?price_list="):
			with self.subTest(term=term):
				self.assertTrue(term in source, term)
		self.requires("frontend/src/screens/RateCalendar.tsx", "/tariffs?q=")
		self.requires("frontend/src/screens/ChannelHealth.tsx", "/cashier/fx", "/tariffs")

	def test_cashier_float(self):
		self.assertNotIn('useState("2000")', (ROOT / "frontend/src/screens/CashierTill.tsx").read_text())

	def test_precedence_and_guardrails(self):
		self.requires("frontend/src/screens/ConfiguredScreen.tsx", "all_active_plans_tariff")
		for file in ("components/AddTariffDrawer.tsx", "screens/ContractTariffs.tsx"):
			self.requires("frontend/src/" + file, "Guardrail")

	def test_health_brand(self):
		for file in ("health.py", "translations/fr.csv", "translations/zh.csv"):
			with self.subTest(file=file):
				self.assertFalse("Contact Maximus support." in (ROOT / "kamra" / file).read_text())

	def test_dashboard_and_settings_entry_points(self):
		for name in ("Owner", "Dashboard"):
			file = ROOT / f"frontend/src/screens/{name}.tsx"
			self.assertTrue("/tariffs" in file.read_text() or "PendingTariff" in file.read_text(), name)
		settings = ROOT / "frontend/src/screens/settings"
		self.assertTrue(any("/tariffs" in file.read_text() for file in settings.glob("*.tsx")))

	def test_discard_controls(self):
		for name in ("screens/ContractTariffs", "components/TariffMassActions"):
			self.requires("frontend/src/" + name + ".tsx", "DiscardReason")

	def test_cash_amount_labels(self):
		for name, label in (("CashierBoard", "Default opening float"), ("PettyCash", "Amount"), ("CashierTill", "Opening float")):
			lines = (ROOT / f"frontend/src/screens/{name}.tsx").read_text().splitlines()
			self.assertTrue(any(label in line and "currency" in line for line in lines), name)
