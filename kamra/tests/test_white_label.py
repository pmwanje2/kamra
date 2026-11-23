"""White-label contracts; runs with standard Python and no bench/site."""

import ast
import html
import importlib.util
import json
import re
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import quote, unquote

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


def load_parts(name, names, frappe, imports=("kamra.branding",), **namespace):
	"""Run only the named top-level functions and constants of kamra/<name>,
	with its imports from `imports`, so no unrelated bench module loads."""
	path = KAMRA / name
	tree = ast.parse(path.read_text(encoding="utf-8"))

	def targets(node):
		return {t.id for t in getattr(node, "targets", []) if isinstance(t, ast.Name)}
	selected = [node for node in tree.body if (
		isinstance(node, ast.FunctionDef) and node.name in names
	) or (isinstance(node, ast.Assign) and targets(node) & set(names)
	) or (isinstance(node, ast.ImportFrom) and node.module in imports)]
	namespace = {"frappe": frappe, **namespace}
	exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
	return namespace


def server_sources():
	for file in sorted(KAMRA.rglob("*.py")):
		rel = file.relative_to(KAMRA)
		if not set(rel.parts) & {"tests", "scripts", "patches"}:
			yield rel, ast.parse(file.read_text(encoding="utf-8"))


def vendor_literals(node):
	return [part.value for part in ast.walk(node) if isinstance(part, ast.Constant)
	        and isinstance(part.value, str) and FORBIDDEN.search(part.value)]


def refuses_when_white_labelled(statement):
	"""`if white_label(): <...> raise / return / frappe.throw(...)`"""
	if not (isinstance(statement, ast.If) and ast.unparse(statement.test) == "white_label()" and statement.body):
		return False
	last = statement.body[-1]
	return isinstance(last, (ast.Raise, ast.Return)) or (
		isinstance(last, ast.Expr) and isinstance(last.value, ast.Call)
		and ast.unparse(last.value.func) == "frappe.throw")


def module(name, **attrs):
	result = ModuleType(name)
	vars(result).update(attrs)
	return result


class Row(dict):
	__getattr__ = dict.__getitem__


class Refused(Exception):
	pass


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
		frappe._ = lambda text, *args, **kwargs: text
		frappe.sessions = SimpleNamespace(get_csrf_token=lambda: "fixture-csrf")
		frappe.local = SimpleNamespace(site="fixture.test")
		frappe.request = SimpleNamespace(path="/kamra/settings")
		utils = frappe.utils = self.utils = ModuleType("frappe.utils")
		utils.get_url = lambda path=None, full_address=False: "https://fixture.test" + (path or "")
		utils.strip_html = lambda value: value
		utils.escape_html = html.escape
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

	# -- Links, mail, headers and metadata that leave the screen -------------

	def test_guest_check_in_links_use_the_public_route(self):
		outbound, templates = [], []
		self.utils.now_datetime = lambda: "2031-03-12 10:00:00"
		self.frappe.db = SimpleNamespace(
			get_value=lambda doctype, name, field=None, as_dict=False: Row(
				name="RES-1", property="North", guest="G-1", guest_name="Ada", precheckin_token="tok123",
			) if doctype == "Reservation" else {"Guest": "+256700000001", "Property": "North Inn"}[doctype],
			set_value=lambda *args, **kwargs: None, commit=lambda: None)
		self.frappe.get_doc = lambda doctype, name: Row(
			name="RES-1", property="North", guest="G-1", guest_name="Ada", precheckin_token="tok123",
			check_in_date="2031-03-12", check_out_date="2031-03-13")
		stubs = {
			"kamra.agents_channels": module("kamra.agents_channels",
				send_outbound=lambda *args: outbound.append(args[-1]) or {"sent": True}),
			"kamra.savings": module("kamra.savings", log_action=lambda *args, **kwargs: None),
		}
		with patch.dict(sys.modules, stubs):
			api = load_parts("api.py", {"send_precheckin_link"}, self.frappe,
			                 require_roles=lambda *roles: lambda fn: fn)
			whatsapp = load_parts("whatsapp.py", {"notify_booking_confirmed"}, self.frappe,
				_conn=lambda prop: {"tpl_booking_confirmation": "booking_confirmed", "tpl_precheckin": "precheckin"},
				send_template=lambda prop, phone, template, args, **kwargs: templates.append(args) or {"sent": True})
			for flag, link in (("1", "https://fixture.test/checkin/tok123"),
			                   ("0", "https://fixture.test/kamra/checkin/tok123")):
				self.frappe.conf["kamra_white_label"] = flag
				result = api["send_precheckin_link"]("RES-1")
				whatsapp["notify_booking_confirmed"]("RES-1")
				self.assertEqual((result["link"], templates[-1][-1]), (link, link))
				self.assertTrue(outbound[-1].endswith(link), outbound[-1])
				if flag == "1":
					self.assertNotRegex(outbound[-1], FORBIDDEN)

	def test_absolute_links_never_name_the_app_mount(self):
		mount = re.compile(r"^/kamra(?:/|$)")

		def get_url(node):
			return isinstance(node, ast.Call) and ast.unparse(node.func).split(".")[-1] == "get_url"
		findings = set()
		for rel, tree in server_sources():
			for node in ast.walk(tree):
				if isinstance(node, ast.JoinedStr):
					for left, right in zip(node.values, node.values[1:], strict=False):
						if isinstance(left, ast.FormattedValue) and get_url(left.value) and \
								isinstance(right, ast.Constant) and mount.match(right.value):
							findings.add(f"{rel}:{node.lineno}: {right.value}")
				if get_url(node) and node.args and isinstance(node.args[0], ast.Constant) and \
						isinstance(node.args[0].value, str) and mount.match(node.args[0].value):
					findings.add(f"{rel}:{node.lineno}: {node.args[0].value}")
		self.assertEqual(sorted(findings), [])

	def test_mcp_connect_pages_and_challenge_name_the_site_product(self):
		self.frappe.db = SimpleNamespace(get_value=lambda *args, **kwargs: "Fixture Inn",
		                                 sql=lambda *args, **kwargs: [], count=lambda *args, **kwargs: 0)
		self.frappe.session = SimpleNamespace(user="staff@example.test")
		oauth = load_parts("mcp_oauth.py", {"connect_info", "claude_install_url", "_error_page", "_html_shell"},
			self.frappe, quote=quote, require_roles=lambda *roles: lambda fn: fn, TOOL_COUNT=1,
			mcp_resource_url=lambda: "https://fixture.test/mcp", issuer_url=lambda: "https://fixture.test/mcp/oauth",
			is_public_https=lambda: True)
		http = load_parts("mcp_http.py", {"PROTOCOL_VERSIONS", "_auth_challenge", "_dispatch_method"}, self.frappe,
			imports=("kamra.branding", "kamra.mcp_tools"),
			protected_resource_url=lambda: "https://fixture.test/.well-known/oauth-protected-resource/mcp")
		info = oauth["connect_info"]("North")
		page = oauth["_error_page"]("That link has expired.")
		challenge = http["_auth_challenge"]()
		initialize = http["_dispatch_method"]("initialize", {}, None)
		for text in (info["claude_code"], unquote(info["claude_install_url"]), page, initialize["instructions"]):
			self.assertNotRegex(text, FORBIDDEN)
		self.assertIn("href='/hotel'>Back to Maximus Hotel</a>", page)
		self.assertIn("connectorName=Fixture Inn (Maximus Hotel)&", unquote(info["claude_install_url"]))
		self.assertEqual(re.search(r'realm="([^"]*)"', challenge).group(1), "Maximus Hotel")
		self.assertIn('scope="kamra.mcp"', challenge)  # an OAuth scope clients request, not a label
		self.assertEqual(initialize["serverInfo"]["title"], "Maximus Hotel")
		from kamra.mcp_tools import mcp_tool_list

		def shown(value):
			if isinstance(value, dict):
				for key, item in value.items():
					if key in {"title", "description"} and isinstance(item, str):
						yield item
					else:
						yield from shown(item)
			elif isinstance(value, list):
				for item in value:
					yield from shown(item)
		self.assertEqual(sorted({text for text in shown(mcp_tool_list()) if FORBIDDEN.search(text)}), [])
		self.frappe.conf.clear()  # upstream Kamra is unchanged
		self.assertIn("href='/kamra/assistant'>Back to Kamra</a>", oauth["_error_page"]("x"))
		self.assertIn("--transport http kamra https://fixture.test/mcp", oauth["connect_info"]("North")["claude_code"])
		self.assertIn('realm="Kamra"', http["_auth_challenge"]())

	def test_help_assistant_names_paths_staff_can_open(self):
		prompt = load_parts("assistant.py", {"HELP_SYSTEM"}, self.frappe, imports=())["HELP_SYSTEM"]
		self.assertNotRegex(prompt.format(product="Maximus Hotel", assistant="Eve"), FORBIDDEN)

	def test_hosting_enquiries_are_refused_when_white_labelled(self):
		self.frappe.throw = Mock(side_effect=Refused)
		self.frappe.get_doc = Mock(return_value=SimpleNamespace(
			insert=Mock(), name="HE-1", full_name="Ada", email="ada@example.test", phone=None, interest=None,
			property_name=None, rooms=0, city=None, message=None))
		self.frappe.sendmail = Mock()
		self.frappe.db = SimpleNamespace(commit=Mock())
		public = load_parts("public_api.py", {"hosting_enquiry"}, self.frappe, rate_limit=lambda **kw: lambda fn: fn)
		with self.assertRaises(Refused):
			public["hosting_enquiry"]("Ada", "ada@example.test")
		self.frappe.get_doc.assert_not_called()
		self.frappe.sendmail.assert_not_called()
		self.assertNotRegex(str(self.frappe.throw.call_args), FORBIDDEN)
		self.frappe.conf["kamra_white_label"] = "0"  # upstream Kamra still takes the lead
		self.assertTrue(public["hosting_enquiry"]("Ada", "ada@example.test")["ok"])
		self.frappe.sendmail.assert_called_once()

	def test_sendmail_subjects_and_recipients_name_no_vendor(self):
		findings = set()
		for rel, tree in server_sources():
			for function in ast.walk(tree):
				if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
					continue
				refused = [statement.lineno for statement in function.body if refuses_when_white_labelled(statement)]
				assigned = {}
				for node in ast.walk(function):
					if isinstance(node, ast.Assign):
						for target in node.targets:
							if isinstance(target, ast.Name):
								assigned.setdefault(target.id, []).append(node.value)
				for call in ast.walk(function):
					if not (isinstance(call, ast.Call) and ast.unparse(call.func) == "frappe.sendmail"):
						continue
					if refused and min(refused) < call.lineno:
						continue  # only reached when not white-labelled
					args = dict(zip(("recipients", "sender", "subject"), call.args, strict=False))
					args.update((kw.arg, kw.value) for kw in call.keywords if kw.arg)
					for key in ("recipients", "sender", "subject"):
						value = args.get(key)
						values = [value] + (assigned.get(value.id, []) if isinstance(value, ast.Name) else [])
						for text in (t for v in values if v is not None for t in vendor_literals(v)):
							findings.add(f"{rel}:{call.lineno}: {key}: {text}")
		self.assertEqual(sorted(findings), [])

	def test_webhook_deliveries_identify_the_site_product(self):
		posted = []

		def post(url, **kwargs):
			posted.append(kwargs["headers"])
			return SimpleNamespace(status_code=204, text="")
		self.frappe._ = lambda text: text
		self.frappe.ValidationError = type("ValidationError", (Exception,), {})
		vars(self.utils).update(add_to_date=lambda *args, **kwargs: None, cint=lambda value: int(value or 0),
		                        now_datetime=lambda: "2031-03-12 10:00:00")
		delivery = SimpleNamespace(webhook="WH-1", property="North", attempts=0, payload="{}", name="DEL-1",
		                           event="reservation.created", event_id="evt-1", save=lambda **kwargs: None)
		self.frappe.get_doc = lambda doctype, name: delivery
		self.frappe.db = SimpleNamespace(set_value=lambda *args, **kwargs: None, get_value=lambda *args, **kwargs: Row(
			name="WH-1", url="https://hooks.example.test/in", active=1, property="North"))
		stubs = {
			"requests": module("requests", post=post, RequestException=OSError),
			"frappe.utils.password": module("frappe.utils.password", get_decrypted_password=lambda *args, **kwargs: "s3cret"),
			"kamra.authz": module("kamra.authz", require_roles=lambda *roles, **kwargs: lambda fn: fn),
		}
		with patch.dict(sys.modules, stubs):
			spec = importlib.util.spec_from_file_location("webhooks_under_test", KAMRA / "webhooks.py")
			webhooks = importlib.util.module_from_spec(spec)
			spec.loader.exec_module(webhooks)
			webhooks.validate_target_url = lambda url: url  # no DNS lookups from a unit test
			for flag in ("1", "0"):
				self.frappe.conf["kamra_white_label"] = flag
				delivery.status = "Pending"
				self.assertEqual(webhooks.deliver("DEL-1")["status"], "Delivered")
		white, upstream = posted
		self.assertNotRegex(white["User-Agent"], FORBIDDEN)
		self.assertEqual(upstream["User-Agent"], "Kamra-Webhooks/1")
		# Receivers verify these names; they are a signature contract, not a label.
		contract = {"X-Kamra-Event", "X-Kamra-Event-Id", "X-Kamra-Delivery", "X-Kamra-Timestamp", "X-Kamra-Signature"}
		for headers in posted:
			self.assertEqual({name for name in headers if name.startswith("X-Kamra-")}, contract)

	def test_doctype_labels_and_descriptions_name_no_vendor(self):
		findings = []
		for path in sorted(KAMRA.glob("**/doctype/*/*.json")):
			meta = json.loads(path.read_text(encoding="utf-8"))
			if not isinstance(meta, dict) or meta.get("doctype") != "DocType":
				continue
			rows = [("description", meta.get("description"))] + [
				(f"{field.get('fieldname')}.{key}", field.get(key))
				for field in meta.get("fields", []) for key in ("label", "description")]
			findings += [f"{path.parent.name}.{where}: {text}" for where, text in rows
			             if isinstance(text, str) and FORBIDDEN.search(text)]
		self.assertEqual(findings, [])

	def test_bundled_kamra_artwork_is_never_the_site_logo_or_favicon(self):
		spec = importlib.util.spec_from_file_location("branding_under_test", KAMRA / "branding.py")
		brand = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(brand)
		for url in ("/assets/kamra/kamra-logo.png", "/assets/kamra/kamra-horizontal.png", "/files/kamra-logo.png",
		            "/assets/kamra/frontend/favicon-32.png", "/assets/kamra/frontend/apple-touch-180.png"):
			self.assertIsNone(brand._site_asset(url), url)
		self.assertEqual(brand._site_asset("/files/favicon-32.png"), "/files/favicon-32.png")  # the hotel's own
		self.frappe.conf["kamra_white_label"] = "0"
		self.assertEqual(brand._site_asset("/assets/kamra/kamra-logo.png"), "/assets/kamra/kamra-logo.png")
