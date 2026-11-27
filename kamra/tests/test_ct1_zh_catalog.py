"""Guest-facing deferred tax notes and the migration field have Chinese translations."""
import csv
from pathlib import Path
from string import Formatter
import unittest


def format_fields(text):
	return sorted(field for _, field, _, _ in Formatter().parse(text) if field is not None)


class ChineseContractTariffCatalog(unittest.TestCase):
	def test_guest_tax_notes_and_legacy_advance_label(self):
		with (Path(__file__).resolve().parents[1] / "translations" / "zh.csv").open(encoding="utf-8") as source:
			catalog = {row[0]: row[1] for row in csv.reader(source) if len(row) >= 2}
		for message in (
			"Rates include estimated {0} ({1}%). Final tax breakdown is issued on the accounting tax invoice at settlement.",
			"Estimated {0} ({1}%) is calculated on the accounting tax invoice at settlement.",
			"Tax-inclusive rates. Final {0} breakdown is issued on the tax invoice at settlement.",
			"Tax is calculated on the accounting tax invoice at settlement.",
			"Legacy Advance Basis",
		):
			with self.subTest(message=message):
				translated = catalog.get(message, "")
				self.assertRegex(translated, r"[\u4e00-\u9fff]")
				self.assertEqual(format_fields(translated), format_fields(message))
