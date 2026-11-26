"""The public REST contract follows request sanitization, independent of Python calls."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import json
import unittest
from unittest.mock import patch

from scripts.contract_check import check

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location("kamra_gen_contract", ROOT / "docs-site" / "gen_contract.py")
generator = module_from_spec(spec)
spec.loader.exec_module(generator)
METHOD = "kamra.api.move_reservation"


class WireContractTests(unittest.TestCase):
	def test_room_move_contract_uses_the_masked_optional_field(self):
		params = generator.build(str(ROOT))["methods"][METHOD]["params"]
		self.assertEqual(params.get("pin_secret"), {"default": True})
		self.assertNotIn("supervisor_pin", params)
		self.assertEqual(params["reservation"], {"default": False})

	def test_wire_only_rename_is_detected_and_requires_a_migration(self):
		literal = generator._literal

		def pretend_legacy(root, module, name):
			if (module, name) == ("authz", "_PIN_HTTP_FIELDS"):
				return {}
			return literal(root, module, name)

		with patch.object(generator, "_literal", side_effect=pretend_legacy):
			old = generator.build(str(ROOT))
		current = generator.build(str(ROOT))
		ok, report = check(old, current, [])
		self.assertFalse(ok, report)
		self.assertIn(f"BREAKING  method:{METHOD}:param:supervisor_pin", "\n".join(report))
		metadata = json.loads((ROOT / "contract-deprecations.json").read_text())["deprecations"]
		ok, report = check(old, current, metadata)
		self.assertTrue(ok, report)
		self.assertIn("use pin_secret", "\n".join(report))
