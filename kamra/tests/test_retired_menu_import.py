"""Legacy menu RPCs must refuse even after authentication and role checks."""
import csv
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch


MESSAGE = "Manage menu imports in the point of sale."


class RetiredMenuImportTest(TestCase):
	def test_legacy_requests_refuse_without_pos_or_database(self):
		def authorized(*args, **kwargs):
			return lambda fn: fn

		translate = Mock(return_value="translated message")
		frappe = SimpleNamespace(whitelist=authorized, ValidationError=ValueError, _=translate)
		imports = {
			"frappe": frappe,
			"kamra.authz": SimpleNamespace(require_roles=authorized),
		}
		with patch.dict(sys.modules, imports):
			methods = runpy.run_path(str(Path(__file__).resolve().parents[1] / "menu_import.py"))
		translate.assert_not_called()
		for name, args in (
			("menu_template", ()),
			("preview_menu_import", ("Hotel", "item,price\nTea,2")),
			("run_menu_import", ("Hotel", "item,price\nTea,2", "Cafe", 1)),
		):
			with self.subTest(method=name):
				translate.reset_mock()
				with self.assertRaisesRegex(ValueError, "^translated message$"):
					methods[name](*args)
				translate.assert_called_once_with(MESSAGE)

	def test_french_and_arabic_translations(self):
		translations = Path(__file__).resolve().parents[1] / "translations"
		for language in ("fr", "ar"):
			with self.subTest(language=language), (translations / f"{language}.csv").open(
				encoding="utf-8-sig", newline=""
			) as catalog:
				matches = [row[1] for row in csv.reader(catalog) if row and row[0] == MESSAGE]
				self.assertEqual(len(matches), 1)
				self.assertTrue(matches[0].strip())
				self.assertNotEqual(matches[0], MESSAGE)
