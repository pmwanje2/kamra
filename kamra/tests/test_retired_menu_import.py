"""Legacy menu RPCs must refuse even after authentication and role checks."""
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch


class RetiredMenuImportTest(TestCase):
	def test_legacy_requests_refuse_without_pos_or_database(self):
		def authorized(*args, **kwargs):
			return lambda fn: fn

		frappe = SimpleNamespace(whitelist=authorized, ValidationError=ValueError)
		imports = {
			"frappe": frappe,
			"kamra.authz": SimpleNamespace(require_roles=authorized),
			"kamra.migrate": SimpleNamespace(_norm=None, _parse_csv=None),
		}
		with patch.dict(sys.modules, imports):
			methods = runpy.run_path(str(Path(__file__).resolve().parents[1] / "menu_import.py"))
		for name, args in (
			("menu_template", ()),
			("preview_menu_import", ("Hotel", "item,price\nTea,2")),
			("run_menu_import", ("Hotel", "item,price\nTea,2", "Cafe", 1)),
		):
			with self.subTest(method=name), self.assertRaisesRegex(ValueError, "point of sale"):
				methods[name](*args)
