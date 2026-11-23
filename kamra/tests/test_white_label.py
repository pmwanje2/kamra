"""White-label contracts; runs with standard Python and no bench/site."""

import ast
import importlib.util
import json
import re
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

KAMRA = Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile(r"\bKamra\b|kamrapms\.com|\bFrappe\b|\bERPNext\b|\bURY\b|HeyKoala", re.I)
CONFIG = {"kamra_white_label": "1", "kamra_product_name": "Maximus Hotel", "kamra_assistant_name": "Eve",
          "kamra_channel": "nightly", "kamra_build": "aa9"}


def load_site_info(frappe):
	"""Evaluate the real endpoint without importing unrelated bench modules."""
	path = KAMRA / "public_api.py"
	tree = ast.parse(path.read_text(encoding="utf-8"))
	selected = [node for node in tree.body if (
		isinstance(node, ast.FunctionDef) and node.name in {"site_info", "_brand"}
	) or (isinstance(node, ast.ImportFrom) and node.module == "kamra.branding")]
	namespace = {"frappe": frappe}
	exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
	return namespace["site_info"]()


class WhiteLabelTest(unittest.TestCase):
	def setUp(self):
		frappe = self.frappe = ModuleType("frappe")
		frappe.conf = CONFIG.copy()
		frappe.db = SimpleNamespace(
			get_default=lambda _: None,
			get_single_value=lambda *_: "/files/hotel-logo.svg",
		)
		frappe.get_website_settings = {"app_name": "Fixture Hotel", "favicon": "/files/favicon.png"}.get
		frappe.whitelist = lambda **kw: lambda fn: fn
		frappe.sessions = SimpleNamespace(get_csrf_token=lambda: "fixture-csrf")
		frappe.local = SimpleNamespace(site="fixture.test")
		frappe.session = SimpleNamespace(user="Guest")  # the boot asks nothing for a signed-out visitor
		frappe.request = SimpleNamespace(path="/kamra/settings")
		utils = ModuleType("frappe.utils")
		utils.get_url = lambda path: "https://fixture.test" + path
		utils.strip_html = lambda value: value
		dateutils = ModuleType("frappe.utils.dateutils")
		dateutils.get_user_date_format = lambda: "yyyy-mm-dd"
		modules = patch.dict(sys.modules, {"frappe": frappe, "frappe.utils": utils, "frappe.utils.dateutils": dateutils})
		modules.start()
		self.addCleanup(modules.stop)

	def test_resolution_and_unset_defaults(self):
		spec = importlib.util.spec_from_file_location("branding_under_test", KAMRA / "branding.py")
		brand = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(brand)
		self.assertEqual((brand.product_name(), brand.assistant_name(), brand.white_label()), ("Maximus Hotel", "Eve", True))
		self.frappe.conf["kamra_white_label"] = "0"
		self.assertFalse(brand.white_label())
		self.frappe.conf.clear()
		self.assertEqual((brand.product_name(), brand.assistant_name(), brand.white_label()), ("Kamra", "Kamra Agent", False))
		self.frappe.conf["kamra_white_label"] = 1
		self.assertEqual((brand.product_name(), brand.assistant_name()), ("Hotel PMS", "Assistant"))

	def test_public_metadata_omits_upstream_identity_and_build(self):
		info = load_site_info(self.frappe)
		self.assertFalse(info.keys() & {"app", "version", "channel", "build"}, info)
		self.assertEqual(info["brand"], {"product": "Maximus Hotel", "assistant": "Eve", "white_label": True,
		                                "logo": "/files/hotel-logo.svg", "favicon": "/files/favicon.png"})
		self.assertEqual(info["brand_name"], "Maximus Hotel")
		self.frappe.db.get_single_value = lambda *_: "/assets/kamra/frontend/kamra-mark.svg"
		self.frappe.get_website_settings = {"app_name": "Frappe", "favicon": "/assets/frappe/images/favicon.svg"}.get
		self.assertIsNone(load_site_info(self.frappe)["brand"]["logo"])
		self.assertIsNone(load_site_info(self.frappe)["brand"]["favicon"])
		self.frappe.conf.clear()
		self.assertTrue({"app", "version", "channel", "build"} <= load_site_info(self.frappe).keys())

	def test_shell_metadata_and_boot_are_safe(self):
		self.frappe.conf["kamra_product_name"] = "Maximus Hotel </script><test>"
		with tempfile.TemporaryDirectory() as directory:
			index = Path(directory) / "index.html"
			index.write_text((KAMRA.parent / "frontend/index.html").read_text(encoding="utf-8"), encoding="utf-8")
			self.frappe.get_app_path = lambda *_: str(index)
			serve = runpy.run_path(str(KAMRA / "www/kamra.py"))["get_context"]
			html = serve(SimpleNamespace()).spa_html
		title = re.search(r"<title>(.*?)</title>", html).group(1)
		desc = re.search(r'name="description"\s+content="([^"]*)"', html).group(1)
		for value in (title, desc):
			self.assertNotRegex(value, FORBIDDEN)
			self.assertIn("Maximus Hotel &lt;/script&gt;&lt;test&gt;", value)
		boot = re.search(r"window\.kamra_brand\s*=\s*(\{.*?\});", html).group(1)
		self.assertNotIn("<", boot)
		self.assertEqual(json.loads(boot)["product"], self.frappe.conf["kamra_product_name"])
		self.assertEqual(json.loads(boot)["assistant"], "Eve")

	def test_reconciliation_messages_render_the_site_product(self):
		from kamra.branding import product_name

		path = KAMRA / "golive.py"
		function = next(node for node in ast.parse(path.read_text(encoding="utf-8")).body
		                if isinstance(node, ast.FunctionDef) and node.name == "_compare")
		scope = {"product_name": product_name, "_money": float, "_same": lambda left, right: left == right}
		exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), scope)
		result = scope["_compare"]("booking", "Bookings", {"test stay": 12}, {}, str)
		self.assertEqual(result["problems"], ["test stay: 12.00 in the file, not in Maximus Hotel"])

	def test_server_message_sources_have_no_vendor_literals(self):
		findings = set()
		for file in KAMRA.rglob("*.py"):
			rel = file.relative_to(KAMRA)
			if set(rel.parts) & {"tests", "scripts", "patches"}:
				continue
			for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
				messages = []
				if isinstance(node, ast.Call):
					call = ast.unparse(node.func)
					if call in {"_", "frappe._", "frappe.throw", "frappe.msgprint"}:
						messages.extend(node.args[:1])
						messages.extend(kw.value for kw in node.keywords if kw.arg in {"msg", "title"})
					if rel.name == "golive.py" and call == "problems.append":
						messages.extend(node.args)
					if rel.name == "settings.py" and call == "S":
						messages.extend(kw.value for kw in node.keywords if kw.arg == "hint")
				if isinstance(node, ast.Dict):
					messages.extend(value for key, value in zip(node.keys, node.values)
					                if isinstance(key, ast.Constant) and key.value in {"error", "message", "error_description"})
				if rel.name == "golive.py" and isinstance(node, ast.Assign):
					if any(isinstance(target, ast.Name) and target.id == "problems" for target in node.targets):
						messages.append(node.value)
				if rel.name == "mcp_access.py" and isinstance(node, ast.Assign):
					if any(isinstance(target, ast.Name) and target.id == "NO_ACCESS" for target in node.targets):
						messages.append(node.value)
				for message in messages:
					for part in ast.walk(message):
						if isinstance(part, ast.Constant) and isinstance(part.value, str) and FORBIDDEN.search(part.value) and not re.fullmatch(r"(?:kamra|frappe)\.[\w.]+", part.value):
							findings.add(f"{rel}:{part.lineno}: {part.value}")
		self.assertEqual(len(findings), 0, f"{len(findings)} vendor strings:\n" + "\n".join(sorted(findings)))
