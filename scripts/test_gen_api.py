"""Exercise the API docs generator used by the documentation workflow."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
PIN_ENDPOINTS = {
    "kamra.api.set_cashier_pin": {"pin_secret", "current_password"},
    "kamra.api.verify_cashier_pin": {"property", "pin_secret"},
}


class ApiDocsPinTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        root = Path(temporary.name)
        docs = root / "docs-site"
        (docs / "public").mkdir(parents=True)
        (root / "kamra").symlink_to(ROOT / "kamra", target_is_directory=True)
        shutil.copyfile(ROOT / "docs-site" / "gen_api.py", docs / "gen_api.py")
        subprocess.run([sys.executable, "gen_api.py"], cwd=docs, check=True, capture_output=True)
        cls.reference = (docs / "api-reference.md").read_text(encoding="utf-8")
        cls.postman = json.loads((docs / "public" / "kamra.postman_collection.json").read_text(encoding="utf-8"))

    def test_api_reference_lists_required_http_fields(self):
        for endpoint, expected in PIN_ENDPOINTS.items():
            with self.subTest(endpoint=endpoint):
                section = self.reference.split(f"### `{endpoint}`\n", 1)[1].split("\n### ", 1)[0]
                params = dict(re.findall(r"^\| `([^`]+)` \| (yes|no) \|", section, re.MULTILINE))
                self.assertEqual(params, dict.fromkeys(expected, "yes"))

    def test_postman_examples_use_http_fields(self):
        requests = {
            item["request"]["url"]["path"][-1]: item["request"]
            for folder in self.postman["item"]
            for item in folder["item"]
        }
        for endpoint, expected in PIN_ENDPOINTS.items():
            with self.subTest(endpoint=endpoint):
                body = json.loads(requests[endpoint]["body"]["raw"])
                self.assertEqual(set(body), expected)
