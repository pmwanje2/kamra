"""The published tariff and FX entries describe their optional inputs and readers."""
from pathlib import Path
import unittest


class TariffApiReference(unittest.TestCase):
	def test_published_tariff_and_exchange_rate_contracts(self):
		page = (Path(__file__).resolve().parents[2] / "docs-site" / "api-reference.md").read_text()
		for endpoint, parameter in (
			("rates.discard_contract_tariff", "reason"),
			("rates.bulk_discard_contract_tariffs", "reason"),
			("ledger.list_exchange_rates", "currency"),
		):
			with self.subTest(endpoint=endpoint):
				section = page.split(f"### `kamra.{endpoint}`\n", 1)[1].split("\n### ", 1)[0]
				self.assertIn(f"| `{parameter}` | no |", section)
				if endpoint == "ledger.list_exchange_rates":
					for role in ("Night Auditor", "Revenue Manager"):
						self.assertIn(f"`{role}`", section)
