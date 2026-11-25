"""P53 Horizontal DoA Matrix + Floor PIN & Four-Eyes SoD Convergence tests.

Site-less unit tests exercising real `kamra.authz` and `kamra.api`
(`post_allowance`, `decide_allowance`) against the exact
`delegation-of-authority@6` (`siteops_doa.verify_floor_authority`) and
`hospitality-doa-convergence@1` (`Allowance Request Approval` workflow)
contracts.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

KAMRA = Path(__file__).resolve().parents[1]


class _ValidationError(Exception):
	pass


class _PermissionError(Exception):
	pass


class _DoesNotExistError(Exception):
	pass


class _Dict(dict):
	def __getattr__(self, key):
		return self.get(key)

	def __setattr__(self, key, value):
		self[key] = value


class _Doc(_Dict):
	def __init__(self, data: dict, *, store: dict | None = None):
		super().__init__(data)
		object.__setattr__(self, "flags", _Dict())
		object.__setattr__(self, "_store", store)

	def __setattr__(self, key, value):
		if key in ("flags", "_store"):
			object.__setattr__(self, key, value)
		else:
			self[key] = value

	def insert(self, ignore_permissions: bool = False):
		store = object.__getattribute__(self, "_store")
		dt = self.get("doctype") or "Doc"
		if not self.get("name"):
			rows = store.setdefault(dt, [])
			self["name"] = f"ALW-{len(rows) + 1:05d}"
		if store is not None:
			store.setdefault(dt, []).append(self)
			# Frappe v16 runs process_workflow_actions on on_update after insert.
			if dt == "Allowance Request" and self.status == "Pending" and any(
				wf["document_type"] == dt and wf["is_active"] for wf in store["Workflow"]
			):
				store["Workflow Action"].append({
					"name": f"WA-{self.name}", "reference_doctype": dt,
					"reference_name": self.name, "status": "Open", "permitted_role": "Finance",
				})
		return self

	def save(self, ignore_permissions: bool = False):
		return self


class _FakeCache:
	def __init__(self):
		self._data: dict[str, tuple[object, int | None]] = {}

	def get_value(self, key: str):
		entry = self._data.get(key)
		return entry[0] if entry else None

	def set_value(self, key: str, value, expires_in_sec: int | None = None):
		self._data[key] = (value, expires_in_sec)

	def delete_value(self, key: str):
		self._data.pop(key, None)


def _match_filters(row: dict, filters) -> bool:
	if not filters:
		return True
	if isinstance(filters, str):
		return row.get("name") == filters
	for k, expected in filters.items():
		actual = row.get(k)
		if isinstance(expected, (list, tuple)) and len(expected) == 2 and expected[0] == "in":
			if actual not in expected[1]:
				return False
		elif actual != expected:
			return False
	return True


class _FakeFrappe:
	ValidationError = _ValidationError
	PermissionError = _PermissionError
	DoesNotExistError = _DoesNotExistError
	_dict = _Dict

	def __init__(self, *, user: str = "desk@alpha.ug", tables: dict | None = None,
	             meta_fields: dict[str, set[str]] | None = None,
	             existing_doctypes: set[str] | None = None):
		self.tables: dict[str, list[dict]] = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
		self.meta_fields = {k: set(v) for k, v in (meta_fields or {}).items()}
		self.existing_doctypes = set(existing_doctypes or set(self.tables.keys()))
		self.session = SimpleNamespace(user=user)
		self.local = SimpleNamespace(session={"user": user}, form_dict=_Dict())
		self.flags = _Dict()
		self.response: dict = {}
		self.cache = _FakeCache()
		self.conf: dict = {}
		self.messages: list[str] = []
		self.user_roles: dict[str, list[str]] = {}
		self.user_permissions: dict[str, dict[str, list[dict]]] = {}
		self.passwords: dict[tuple[str, str, str], str] = {}
		self.server_scripts: dict[str, object] = {}
		self.now = datetime(2026, 11, 25, 10, 0, 0)
		self.db = SimpleNamespace(
			exists=self._db_exists,
			get_value=self._db_get_value,
			set_value=self._db_set_value,
			has_column=self._db_has_column,
			get_default=lambda key: self.conf.get(f"default_{key}"),
			table_exists=lambda dt: dt in self.existing_doctypes or dt in self.tables,
			commit=lambda: None,
			close=lambda: None,
			bulk_insert=self._db_bulk_insert,
		)
		self.local.db = self.db

	def _(self, msg: str, *args, **kwargs) -> str:
		return msg

	def whitelist(self, **_kwargs):
		return lambda fn: fn

	def throw(self, msg: str, exc=None, title=None):
		self.messages.append(str(msg))
		raise (exc or self.ValidationError)(str(msg))

	def clear_messages(self):
		self.messages.clear()

	def set_user(self, user: str):
		self.session.user = user
		self.local.session["user"] = user

	def get_roles(self, user: str | None = None) -> list[str]:
		u = user or self.session.user
		if u in self.user_roles:
			return list(self.user_roles[u])
		roles = [r["role"] for r in self.tables.get("Has Role", [])
		         if r.get("parenttype") == "User" and r.get("parent") == u]
		return roles

	def get_hooks(self, name: str, default=None):
		return [] if default is None else default

	def get_meta(self, doctype: str):
		fields = self.meta_fields.get(doctype, set())
		return SimpleNamespace(
			has_field=lambda f: f in fields,
			get_field=lambda f: {"fieldname": f} if f in fields else None,
		)

	def _db_has_column(self, doctype: str, fieldname: str) -> bool:
		return fieldname in self.meta_fields.get(doctype, set())

	def _db_exists(self, doctype: str, filters=None):
		if doctype == "DocType":
			name = filters if isinstance(filters, str) else (filters or {}).get("name")
			return name if name in self.existing_doctypes else None
		if doctype == "Server Script":
			for name, script in self.server_scripts.items():
				if _match_filters(script, filters):
					return name
			return None
		for row in self.tables.get(doctype, []):
			if isinstance(filters, str):
				if str(row.get("name", "")).casefold() == filters.casefold():
					return row.get("name")
			elif _match_filters(row, filters):
				return row.get("name") or "1"
		return None

	def _db_bulk_insert(self, doctype, fields, values):
		self.tables.setdefault(doctype, []).extend(dict(zip(fields, row)) for row in values)

	def _db_get_value(self, doctype: str, filters=None, fieldname="name", as_dict: bool = False, **_kwargs):
		if doctype == "Server Script":
			for name, script in self.server_scripts.items():
				if _match_filters(script, filters):
					if fieldname == "name":
						return name
					return script.get(fieldname)
			return None
		for row in self.tables.get(doctype, []):
			matched = (
				str(row.get("name", "")).casefold() == filters.casefold()
				if isinstance(filters, str)
				else _match_filters(row, filters)
			)
			if not matched:
				continue
			if isinstance(fieldname, (list, tuple)):
				if as_dict:
					return _Dict({f: row.get(f) for f in fieldname})
				return tuple(row.get(f) for f in fieldname)
			if as_dict:
				return _Dict({fieldname: row.get(fieldname)})
			return row.get(fieldname)
		return None

	def _db_set_value(self, doctype: str, filters, fieldname, value=None, **_kwargs):
		if doctype == "Workflow Action" and not self.db.table_exists(doctype):
			raise self.DoesNotExistError(doctype)
		for row in self.tables.get(doctype, []):
			if _match_filters(row, filters):
				if isinstance(fieldname, dict):
					row.update(fieldname)
				else:
					row[fieldname] = value

	def get_all(self, doctype: str, filters=None, fields=None, pluck: str | None = None,
	            distinct: bool = False, order_by: str | None = None, **_kwargs):
		rows = [r for r in self.tables.get(doctype, []) if _match_filters(r, filters)]
		if order_by:
			col = order_by.split()[0]
			rows = sorted(rows, key=lambda r: str(r.get(col) or ""))
		if pluck:
			vals = [r.get(pluck) for r in rows]
			return list(dict.fromkeys(vals)) if distinct else vals
		if fields:
			return [_Dict({f: r.get(f) for f in fields}) for r in rows]
		return [_Dict(r) for r in rows]

	def get_doc(self, arg1, arg2=None, **_kwargs):
		if isinstance(arg1, dict):
			return _Doc(dict(arg1), store=self.tables)
		if arg1 == "Server Script" and arg2 in self.server_scripts:
			return self.server_scripts[arg2]["doc"]
		for row in self.tables.get(arg1, []):
			if row.get("name") == arg2:
				return row if isinstance(row, _Doc) else _Doc(row, store=self.tables)
		raise self.DoesNotExistError(f"{arg1} {arg2}")


def _load_module(name: str, rel_path: str, fake_frappe: _FakeFrappe, extra_modules: dict | None = None):
	frappe_mod = ModuleType("frappe")
	for attr in dir(fake_frappe):
		if not attr.startswith("__"):
			setattr(frappe_mod, attr, getattr(fake_frappe, attr))

	utils_mod = ModuleType("frappe.utils")
	utils_mod.cint = lambda v: int(float(v or 0))
	utils_mod.flt = lambda v, p=None: round(float(v or 0), p) if p is not None else float(v or 0)
	utils_mod.now_datetime = lambda: fake_frappe.now
	utils_mod.add_to_date = lambda dt, minutes=0, days=0: dt + timedelta(days=days, minutes=minutes)

	pw_mod = ModuleType("frappe.utils.password")
	pw_mod.get_decrypted_password = lambda dt, dn, field, raise_exception=False: fake_frappe.passwords.get((dt, dn, field))

	db_mod = ModuleType("frappe.database")
	db_mod.get_db = lambda **options: fake_frappe.db

	up_mod = ModuleType("frappe.core.doctype.user_permission.user_permission")
	up_mod.get_user_permissions = lambda user: fake_frappe.user_permissions.get(user, {})

	branding_mod = ModuleType("kamra.branding")
	branding_mod.role_label = lambda r: r

	settings_mod = ModuleType("kamra.settings")
	settings_mod.get_setting = lambda prop, key, default=None: default

	modules = {
		"frappe": frappe_mod,
		"frappe.database": db_mod,
		"frappe.utils": utils_mod,
		"frappe.utils.password": pw_mod,
		"frappe.core": ModuleType("frappe.core"),
		"frappe.core.doctype": ModuleType("frappe.core.doctype"),
		"frappe.core.doctype.user_permission": ModuleType("frappe.core.doctype.user_permission"),
		"frappe.core.doctype.user_permission.user_permission": up_mod,
		"kamra.branding": branding_mod,
		"kamra.settings": settings_mod,
	}
	if extra_modules:
		modules.update(extra_modules)

	# Load real kamra.erp_bridge inside the same fake frappe environment
	bridge_spec = importlib.util.spec_from_file_location("kamra.erp_bridge", KAMRA / "erp_bridge.py")
	bridge_mod = importlib.util.module_from_spec(bridge_spec)
	modules["kamra.erp_bridge"] = bridge_mod

	spec = importlib.util.spec_from_file_location(name, KAMRA / rel_path)
	mod = importlib.util.module_from_spec(spec)
	with patch.dict(sys.modules, modules):
		bridge_spec.loader.exec_module(bridge_mod)
		spec.loader.exec_module(mod)
	return mod, modules


def _load_api_functions(fake_frappe: _FakeFrappe, names: tuple[str, ...], extra_ns: dict, extra_modules: dict):
	"""Execute the real top-level functions from kamra/api.py cleanly without bench."""
	path = KAMRA / "api.py"
	tree = ast.parse(path.read_text(encoding="utf-8"))
	wanted = set(names)
	selected = []
	for node in tree.body:
		if isinstance(node, ast.FunctionDef) and node.name in wanted:
			node.decorator_list = []
			selected.append(node)
		elif isinstance(node, ast.Assign):
			targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
			if targets & wanted:
				selected.append(node)

	frappe_mod = extra_modules["frappe"]
	ns = {
		"frappe": frappe_mod,
		"_": fake_frappe._,
		"cint": extra_modules["frappe.utils"].cint,
		"flt": extra_modules["frappe.utils"].flt,
		"now_datetime": extra_modules["frappe.utils"].now_datetime,
		**extra_ns,
	}
	exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), ns)
	return ns


def _base_site(user: str = "desk@alpha.ug", currency: str = "UGX", floor_max: float = 50000.0,
               floor_roles: str = "Duty Manager, Hotel Admin",
               backoffice_roles: str = "Finance, Hotel Admin") -> _FakeFrappe:
	prop = "Grand Kampala" if currency == "UGX" else "Gulf Palace"
	company = "Alpha Hospitality Ltd"
	tables = {
		"Property": [
			{"name": prop, "property_name": prop, "company": company, "currency": currency,
			 "short_code": "GK", "disabled": 0},
		],
		"Company": [
			{"name": company, "default_currency": currency},
			{"name": "Beta Holdings Ltd", "default_currency": "USD"},
		],
		"Currency": [
			{"name": "UGX", "number_format": "#,###", "smallest_currency_fraction_value": 1},
			{"name": "USD", "number_format": "#,###.##", "smallest_currency_fraction_value": 0.01},
			{"name": "KWD", "number_format": "#,###.###", "smallest_currency_fraction_value": 0.001},
		],
		"Approval Limit": [
			{
				"name": company,
				"company": company,
				"currency": currency,
				"custom_floor_override_approver_roles": floor_roles,
				"custom_floor_override_max_amount": floor_max,
				"custom_backoffice_allowance_approver_roles": backoffice_roles,
			},
		],
		"Role": [
			{"name": "Duty Manager", "disabled": 0},
			{"name": "Hotel Admin", "disabled": 0},
			{"name": "Finance", "disabled": 0},
			{"name": "Front Desk", "disabled": 0},
			{"name": "System Manager", "disabled": 0},
			{"name": "Administrator", "disabled": 0},
		],
		"User": [
			{"name": "desk@alpha.ug", "full_name": "Desk Clerk", "enabled": 1, "user_type": "System User", "role_profile_name": ""},
			{"name": "gm@alpha.ug", "full_name": "Grace GM", "enabled": 1, "user_type": "System User", "role_profile_name": ""},
			{"name": "duty@alpha.ug", "full_name": "Darius Duty", "enabled": 1, "user_type": "System User", "role_profile_name": "Duty Profile"},
			{"name": "multi@alpha.ug", "full_name": "Mona Multi", "enabled": 1, "user_type": "System User", "role_profile_name": ""},
			{"name": "finance@alpha.ug", "full_name": "Farah Finance", "enabled": 1, "user_type": "System User", "role_profile_name": ""},
			{"name": "gm.beta@beta.ug", "full_name": "Beta GM", "enabled": 1, "user_type": "System User", "role_profile_name": ""},
			{"name": "Administrator", "full_name": "Administrator", "enabled": 1, "user_type": "System User", "role_profile_name": ""},
		],
		"Has Role": [
			{"parent": "desk@alpha.ug", "parenttype": "User", "role": "Front Desk"},
			{"parent": "gm@alpha.ug", "parenttype": "User", "role": "Hotel Admin"},
			{"parent": "Duty Profile", "parenttype": "Role Profile", "role": "Duty Manager"},
			{"parent": "Multi Duty Profile", "parenttype": "Role Profile", "role": "Duty Manager"},
			{"parent": "finance@alpha.ug", "parenttype": "User", "role": "Finance"},
			{"parent": "gm.beta@beta.ug", "parenttype": "User", "role": "Hotel Admin"},
			{"parent": "Administrator", "parenttype": "User", "role": "System Manager"},
			{"parent": "Administrator", "parenttype": "User", "role": "Hotel Admin"},
		],
		"User Role Profile": [
			{"parent": "multi@alpha.ug", "parenttype": "User", "parentfield": "role_profiles", "role_profile": "Multi Duty Profile"},
		],
		"User Permission": [
			{"user": "gm@alpha.ug", "allow": "Company", "for_value": company, "applicable_for": "", "apply_to_all_doctypes": 1},
			{"user": "duty@alpha.ug", "allow": "Company", "for_value": company, "applicable_for": "", "apply_to_all_doctypes": 1},
			{"user": "multi@alpha.ug", "allow": "Company", "for_value": company, "applicable_for": "", "apply_to_all_doctypes": 1},
			{"user": "finance@alpha.ug", "allow": "Company", "for_value": company, "applicable_for": "", "apply_to_all_doctypes": 1},
			{"user": "gm.beta@beta.ug", "allow": "Company", "for_value": "Beta Holdings Ltd", "applicable_for": "", "apply_to_all_doctypes": 1},
		],
		"Cashier PIN": [
			{"name": "gm@alpha.ug", "user": "gm@alpha.ug", "pin_attempts": 0, "locked_until": None, "must_reset": 0},
			{"name": "duty@alpha.ug", "user": "duty@alpha.ug", "pin_attempts": 0, "locked_until": None, "must_reset": 0},
			{"name": "multi@alpha.ug", "user": "multi@alpha.ug", "pin_attempts": 0, "locked_until": None, "must_reset": 0},
			{"name": "finance@alpha.ug", "user": "finance@alpha.ug", "pin_attempts": 0, "locked_until": None, "must_reset": 0},
			{"name": "gm.beta@beta.ug", "user": "gm.beta@beta.ug", "pin_attempts": 0, "locked_until": None, "must_reset": 0},
			{"name": "Administrator", "user": "Administrator", "pin_attempts": 0, "locked_until": None, "must_reset": 0},
		],
		"Workflow": [],
		"Folio": [
			{"name": "FOL-001", "property": prop, "reservation": "RES-001", "status": "Open"},
		],
		"Allowance Request": [],
	}
	meta_fields = {
		"Property": {"company", "currency", "short_code"},
		"Approval Limit": {
			"company",
			"currency",
			"custom_floor_override_approver_roles",
			"custom_floor_override_max_amount",
			"custom_backoffice_allowance_approver_roles",
		},
	}
	site = _FakeFrappe(
		user=user,
		tables=tables,
		meta_fields=meta_fields,
		existing_doctypes=set(tables.keys()) | {"Approval Limit", "Human Authority Event", "Workflow"},
	)
	for uname, pin in (
		("gm@alpha.ug", "2468"),
		("duty@alpha.ug", "4821"),
		("multi@alpha.ug", "7777"),
		("finance@alpha.ug", "9012"),
		("gm.beta@beta.ug", "5555"),
		("Administrator", "1357"),
	):
		site.passwords[("Cashier PIN", uname, "pin")] = pin
	return site


def _install_doa_floor_script(site: _FakeFrappe, captured_calls: list[dict], *,
                              floor_max: float = 50000.0, prec: int = 0,
                              force_error: str | None = None):
	"""Install a faithful DOA-002 API Server Script double verifying contract inputs."""
	seen_nonces: set[str] = set()

	def execute_method():
		req = site.local.form_dict
		captured_calls.append(dict(req))
		if force_error:
			site.throw(force_error)
		approver = str(req.get("approver") or "").strip()
		requester = str(req.get("requester") or "").strip()
		doc_owner = str(req.get("document_owner") or "").strip()
		if not approver or approver in ("Administrator", "Guest"):
			site.throw("[SITEOPS-DOA-ELIGIBLE] Floor approver must be a named active System User.")
		if approver.lower() in {requester.lower(), site.session.user.lower(), doc_owner.lower()} - {""}:
			site.throw("[SITEOPS-DOA-SELF] Maker and floor approver must be two different people.")
		nonce = str(req.get("nonce") or "").strip()
		if not nonce or not (8 <= len(nonce) <= 64):
			site.throw("[SITEOPS-DOA-NONCE] A unique single-use nonce (8 to 64 characters) is required.")
		if nonce in seen_nonces:
			site.throw("[SITEOPS-DOA-REPLAY] Nonce already used.")
		seen_nonces.add(nonce)
		amt = round(float(req.get("amount") or 0), prec)
		if amt > 0 and (floor_max <= 0 or amt > round(floor_max, prec)):
			site.throw("[SITEOPS-DOA-LIMIT] Amount exceeds Floor PIN limit; route through My Inbox approval workflow.")
		pin = str(req.get("pin") or "").strip()
		expected_pin = site.passwords.get(("Cashier PIN", approver, "pin"))
		if not pin or pin != expected_pin:
			site.throw("[SITEOPS-DOA-PIN] Invalid approver PIN.")
		site.response["message"] = {
			"ok": 1,
			"approver": approver,
			"matched_roles": ["Duty Manager"],
			"max_amount": floor_max,
			"authority_event": f"HAE-{len(captured_calls):05d}",
		}

	site.server_scripts["DOA-002 Floor Authority & SoD Verifier"] = {
		"name": "DOA-002 Floor Authority & SoD Verifier",
		"script_type": "API",
		"api_method": "siteops_doa.verify_floor_authority",
		"disabled": 0,
		"doc": SimpleNamespace(execute_method=execute_method),
	}


class TestP53DoAFloorPinAuthz(unittest.TestCase):
	"""`kamra/authz.py` DoA floor authority delegation, SoD, and supervisor scoping."""

	def test_verify_supervisor_delegates_to_siteops_doa_with_single_use_nonce_and_scrubs_pin(self):
		site = _base_site(user="desk@alpha.ug", currency="UGX", floor_max=50000.0)
		calls: list[dict] = []
		_install_doa_floor_script(site, calls, floor_max=50000.0, prec=0)
		authz, mods = _load_module("kamra.authz", "authz.py", site)

		with patch.dict(sys.modules, mods):
			site.local.form_dict = _Dict({
				"pin": "4821",
				"supervisor_pin": "4821",
				"override_pin": "4821",
				"keep": "yes",
			})
			approver1 = authz.verify_supervisor(
				"Grand Kampala", "duty@alpha.ug", "4821",
				requester="desk@alpha.ug",
				document_owner="desk@alpha.ug",
				amount=30000,
				reference_doctype="Folio",
				reference_name="FOL-001",
				reason="Service recovery",
			)
			approver2 = authz.verify_supervisor(
				"Grand Kampala", "duty@alpha.ug", "4821",
				requester="desk@alpha.ug",
				amount=10000,
			)

		self.assertEqual(approver1, "duty@alpha.ug")
		self.assertEqual(approver2, "duty@alpha.ug")
		self.assertEqual(len(calls), 2)
		first, second = calls
		self.assertEqual(first["company"], "Alpha Hospitality Ltd")
		self.assertEqual(first["property"], "Grand Kampala")
		self.assertEqual(first["requester"], "desk@alpha.ug")
		self.assertEqual(first["approver"], "duty@alpha.ug")
		self.assertEqual(first["document_owner"], "desk@alpha.ug")
		self.assertEqual(first["pin"], "4821")
		self.assertEqual(first["amount"], 30000)
		self.assertEqual(first["reference_doctype"], "Folio")
		self.assertEqual(first["reference_name"], "FOL-001")
		self.assertEqual(first["reason"], "Service recovery")
		self.assertTrue(8 <= len(first["nonce"]) <= 64)
		self.assertTrue(8 <= len(second["nonce"]) <= 64)
		self.assertNotEqual(first["nonce"], second["nonce"])
		# PIN scrubbed from outer form_dict while unrelated keys stay intact
		for secret_key in ("pin", "supervisor_pin", "override_pin"):
			self.assertNotIn(secret_key, site.local.form_dict)
		self.assertEqual(site.local.form_dict.get("keep"), "yes")

	def test_verify_supervisor_refuses_administrator_free_pass_and_self_approval_when_doa_active(self):
		site = _base_site(user="gm@alpha.ug", currency="UGX", floor_max=50000.0)
		calls: list[dict] = []
		_install_doa_floor_script(site, calls, floor_max=50000.0, prec=0)
		authz, mods = _load_module("kamra.authz", "authz.py", site)

		with patch.dict(sys.modules, mods):
			site.local.form_dict = _Dict({"pin": "1357", "supervisor_pin": "1357"})
			with self.assertRaisesRegex(_PermissionError, r"SITEOPS-DOA-ELIGIBLE"):
				authz.verify_supervisor("Grand Kampala", "Administrator", "1357", requester="desk@alpha.ug")
			self.assertNotIn("pin", site.local.form_dict)
			self.assertNotIn("supervisor_pin", site.local.form_dict)

			# Self-approval refused even when requester=None (session.user == gm@alpha.ug)
			with self.assertRaisesRegex(_PermissionError, r"SITEOPS-DOA-SELF"):
				authz.verify_supervisor("Grand Kampala", "gm@alpha.ug", "2468")

			# Document owner self-approval refused
			site.set_user("desk@alpha.ug")
			with self.assertRaisesRegex(_PermissionError, r"SITEOPS-DOA-SELF"):
				authz.verify_supervisor(
					"Grand Kampala", "gm@alpha.ug", "2468",
					requester="desk@alpha.ug", document_owner="GM@ALPHA.UG",
				)

	def test_verify_supervisor_standalone_preserves_5_attempt_15_min_lockout_and_scrubs_pin(self):
		site = _base_site(user="desk@alpha.ug", currency="UGX")
		# No siteops_doa.verify_floor_authority script installed -> standalone mode
		authz, mods = _load_module("kamra.authz", "authz.py", site)

		with patch.dict(sys.modules, mods):
			for _ in range(5):
				site.local.form_dict = _Dict({"pin": "0000", "supervisor_pin": "0000"})
				with self.assertRaises(_ValidationError):
					authz.verify_supervisor("Grand Kampala", "gm@alpha.ug", "0000", requester="desk@alpha.ug")
				self.assertNotIn("pin", site.local.form_dict)
				self.assertNotIn("supervisor_pin", site.local.form_dict)

			# 6th attempt with the CORRECT PIN is locked out for 15 minutes
			with self.assertRaisesRegex(_ValidationError, r"PIN_LOCKED"):
				authz.verify_supervisor("Grand Kampala", "gm@alpha.ug", "2468", requester="desk@alpha.ug")

	def test_verify_pos_override_pin_maps_doa_refusal_codes(self):
		site = _base_site(user="desk@alpha.ug", currency="UGX", floor_max=50000.0)
		calls: list[dict] = []
		_install_doa_floor_script(site, calls, force_error="[SITEOPS-DOA-LOCKED] Approver PIN is locked.")
		authz, mods = _load_module("kamra.authz", "authz.py", site)

		with patch.dict(sys.modules, mods):
			locked = authz.verify_pos_override_pin("duty@alpha.ug", "4821", property="Grand Kampala", requester="desk@alpha.ug")
			self.assertEqual(locked, {"ok": False, "name": None, "code": "POS-AUTH-LOCKED"})

			_install_doa_floor_script(site, calls, force_error="[SITEOPS-DOA-ROLE] Not authorized.")
			bad_role = authz.verify_pos_override_pin("duty@alpha.ug", "4821", property="Grand Kampala", requester="desk@alpha.ug")
			self.assertEqual(bad_role, {"ok": False, "name": None, "code": "POS-AUTH-NOT-ELIGIBLE"})

			_install_doa_floor_script(site, calls, floor_max=50000.0, prec=0)
			ok = authz.verify_pos_override_pin("duty@alpha.ug", "4821", property="Grand Kampala", requester="desk@alpha.ug")
			self.assertEqual(ok, {"ok": True, "name": "Darius Duty"})

	def test_list_supervisors_intersects_approval_limit_company_holders_and_excludes_requester(self):
		site = _base_site(
			user="desk@alpha.ug",
			currency="UGX",
			floor_roles="Duty Manager, Hotel Admin",
		)
		authz, mods = _load_module("kamra.authz", "authz.py", site)

		with patch.dict(sys.modules, mods):
			listed = authz.list_supervisors("Grand Kampala")
			users = {row["user"] for row in listed}
			# Active company-scoped holders via direct Has Role (gm), Role Profile (duty), User Role Profile (multi)
			self.assertEqual(users, {"gm@alpha.ug", "duty@alpha.ug", "multi@alpha.ug"})
			# Excluded: Finance (not in floor_roles), Beta GM (wrong company), Administrator (platform), desk (requester)
			self.assertNotIn("finance@alpha.ug", users)
			self.assertNotIn("gm.beta@beta.ug", users)
			self.assertNotIn("Administrator", users)
			self.assertNotIn("desk@alpha.ug", users)

			# When gm@alpha.ug is the requester, gm@alpha.ug is excluded too
			site.set_user("gm@alpha.ug")
			listed_as_gm = {row["user"] for row in authz.list_supervisors("Grand Kampala")}
			self.assertEqual(listed_as_gm, {"duty@alpha.ug", "multi@alpha.ug"})


class TestP53AllowanceConvergence(unittest.TestCase):
	"""`kamra/api.py` (`post_allowance`, `decide_allowance`) four-eyes & Workflow convergence."""

	def _build_api_env(self, site: _FakeFrappe, *, currency: str = "UGX",
	                   allowance_approval: str = "Hotel Admin or Finance", workflow_active: bool = True,
	                   workflow_side_effect=None):
		authz, mods = _load_module("kamra.authz", "authz.py", site)
		posted: list[dict] = []
		actions: list[dict] = []
		emitted: list[tuple] = []
		workflow_calls: list[tuple] = []

		if workflow_active:
			site.tables["Workflow"] = [
				{"name": "Allowance Request Approval", "document_type": "Allowance Request", "is_active": 1},
			]

			site.tables["Workflow Action"] = []
			site.existing_doctypes.add("Workflow Action")

		mods["kamra.settings"].get_setting = lambda prop, key, default=None: (
			{"allowance_approval": allowance_approval, "allowance_approval_above": 0}.get(key, default)
		)

		def fake_apply_workflow(doc, action):
			workflow_calls.append((doc.name, action, site.session.user, bool(doc.flags.get("kamra_decision"))))
			if workflow_side_effect:
				workflow_side_effect(doc, action)
			doc["status"] = "Approved" if action == "Approve" else "Rejected"
			doc["decided_by"] = site.session.user
			# The native on_update hook completes one action for a matching role.
			for row in site.tables["Workflow Action"]:
				if (
					row["reference_doctype"] == doc.doctype and row["reference_name"] == doc.name
					and row["status"] == "Open" and row["permitted_role"] in site.get_roles()
				):
					row.update(status="Completed", completed_by=site.session.user)
					break
			return doc

		wf_mod = ModuleType("frappe.model.workflow")
		wf_mod.apply_workflow = fake_apply_workflow

		places = {"UGX": 0, "USD": 2, "KWD": 3}[currency]
		pricing_mod = ModuleType("kamra.pricing")
		pricing_mod.property_currency = lambda prop: currency
		pricing_mod.round_rate_amount = lambda amt, step, mode, cur: Decimal(str(round(float(amt), places)))
		pricing_mod.night_rate_places = lambda prop: max(2, places)

		ledger_mod = ModuleType("kamra.ledger")
		ledger_mod._currency_places = lambda prop: places
		ledger_mod._round_currency = lambda amt, p: round(float(amt), p)

		loc_mod = ModuleType("kamra.localization")
		loc_mod.money_text = lambda prop, amt: f"{currency} {amt}"

		credit_mod = ModuleType("kamra.credit")
		credit_mod._is_agent = lambda: False

		webhooks_mod = ModuleType("kamra.webhooks")
		webhooks_mod.emit_approval = lambda event, req: emitted.append((event, req.name, req.status))

		savings_mod = ModuleType("kamra.savings")
		savings_mod.log_action = lambda action, *args, **kwargs: actions.append({"action": action, "args": args, "kwargs": kwargs})

		extra_mods = {
			**mods,
			"kamra.authz": authz,
			"frappe.model": ModuleType("frappe.model"),
			"frappe.model.workflow": wf_mod,
			"kamra.pricing": pricing_mod,
			"kamra.ledger": ledger_mod,
			"kamra.localization": loc_mod,
			"kamra.credit": credit_mod,
			"kamra.webhooks": webhooks_mod,
			"kamra.savings": savings_mod,
		}

		def fake_post_allowance(folio, amount, text, gst_rate, approver=None,
		                        requested_by=None, req=None, reason_code=None):
			serial = f"ALW-GK-26-{len(posted) + 1:05d}"
			posted.append({
				"folio": folio,
				"amount": amount,
				"text": text,
				"approver": approver,
				"requested_by": requested_by,
				"serial": serial,
			})
			if req is not None:
				req["serial"] = serial
			return serial

		api_ns = _load_api_functions(
			site,
			("post_allowance", "decide_allowance", "_allowance_needs_approval", "_ALLOWANCE_APPROVERS"),
			extra_ns={
				"_pin_guard": lambda folio, pin: None,
				"_allowance_reason": lambda rc, r: (r or rc or "Reason").strip(),
				"_allowance_approver_roles": lambda prop: ("Hotel Admin", "Finance"),
				"_may_approve_allowance": lambda prop, user=None: (user or site.session.user) in ("gm@alpha.ug", "finance@alpha.ug", "Administrator"),
				"_post_allowance": fake_post_allowance,
				"agent_channel": lambda: None,
			},
			extra_modules=extra_mods,
		)
		return api_ns, extra_mods, posted, actions, emitted, workflow_calls

	def test_post_allowance_removes_manager_self_approval(self):
		site = _base_site(user="gm@alpha.ug", currency="UGX", floor_max=50000.0)
		calls: list[dict] = []
		_install_doa_floor_script(site, calls, floor_max=50000.0, prec=0)
		api_ns, extra_mods, posted, _actions, _emitted, _wf = self._build_api_env(site, currency="UGX")

		with patch.dict(sys.modules, extra_mods):
			# 1. Manager calling post_allowance with own pin (old self-approval path) MUST create Pending request, NEVER post!
			res = api_ns["post_allowance"]("FOL-001", 20000, reason="Own allowance", pin="2468")
			self.assertTrue(res.get("pending"), "Manager must never self-approve own allowance")
			self.assertEqual(posted, [])
			self.assertEqual(len(site.tables["Allowance Request"]), 1)
			self.assertEqual(site.tables["Allowance Request"][0]["status"], "Pending")
			self.assertEqual(site.tables["Allowance Request"][0]["requested_by"], "gm@alpha.ug")

			# 2. Manager explicitly passing self as supervisor MUST be refused with SITEOPS-DOA-SELF!
			with self.assertRaisesRegex(_PermissionError, r"SITEOPS-DOA-SELF"):
				api_ns["post_allowance"](
					"FOL-001", 20000, reason="Own allowance",
					supervisor="gm@alpha.ug", supervisor_pin="2468",
				)
			self.assertEqual(posted, [])

	def test_post_allowance_floor_pin_enforces_cap_for_ugx_0dp_and_kwd_3dp(self):
		# 0dp UGX: cap 50000
		site_ugx = _base_site(user="desk@alpha.ug", currency="UGX", floor_max=50000.0)
		calls_ugx: list[dict] = []
		_install_doa_floor_script(site_ugx, calls_ugx, floor_max=50000.0, prec=0)
		api_ugx, mods_ugx, posted_ugx, *_ = self._build_api_env(site_ugx, currency="UGX")

		with patch.dict(sys.modules, mods_ugx):
			ok = api_ugx["post_allowance"](
				"FOL-001", 50000, reason="Within UGX cap",
				supervisor="duty@alpha.ug", supervisor_pin="4821",
			)
			self.assertTrue(ok.get("ok"))
			self.assertFalse(ok.get("pending"))
			self.assertEqual(ok.get("approved_by"), "duty@alpha.ug")
			self.assertEqual(len(posted_ugx), 1)
			self.assertEqual(posted_ugx[0]["amount"], 50000.0)

			# Above UGX cap: Floor PIN refused with [SITEOPS-DOA-LIMIT]
			with self.assertRaisesRegex(_ValidationError, r"SITEOPS-DOA-LIMIT"):
				api_ugx["post_allowance"](
					"FOL-001", 50001, reason="Above UGX cap",
					supervisor="duty@alpha.ug", supervisor_pin="4821",
				)
			self.assertEqual(len(posted_ugx), 1)

			# Without floor PIN -> routes to Pending Allowance Request
			pending = api_ugx["post_allowance"]("FOL-001", 50001, reason="Above UGX cap inbox")
			self.assertTrue(pending.get("pending"))
			self.assertEqual(len(posted_ugx), 1)

		# 3dp KWD: cap 10.000
		site_kwd = _base_site(user="desk@alpha.ug", currency="KWD", floor_max=10.000)
		calls_kwd: list[dict] = []
		_install_doa_floor_script(site_kwd, calls_kwd, floor_max=10.000, prec=3)
		api_kwd, mods_kwd, posted_kwd, *_ = self._build_api_env(site_kwd, currency="KWD")

		with patch.dict(sys.modules, mods_kwd):
			ok_kwd = api_kwd["post_allowance"](
				"FOL-001", 9.125, reason="Within KWD 3dp cap",
				supervisor="duty@alpha.ug", supervisor_pin="4821",
			)
			self.assertTrue(ok_kwd.get("ok"))
			self.assertEqual(posted_kwd[0]["amount"], 9.125)

			with self.assertRaisesRegex(_ValidationError, r"SITEOPS-DOA-LIMIT"):
				api_kwd["post_allowance"](
					"FOL-001", 10.001, reason="Above KWD 3dp cap",
					supervisor="duty@alpha.ug", supervisor_pin="4821",
				)
			self.assertEqual(len(posted_kwd), 1)

	def test_post_allowance_floor_pin_never_inserts_approved_request_directly_when_workflow_active(self):
		"""Live Frappe v16 validate_workflow rejects inserting a new doc directly into a
		non-initial state ('Approved') when an active Workflow exists on Allowance Request."""
		site = _base_site(user="desk@alpha.ug", currency="UGX", floor_max=50000.0)
		calls: list[dict] = []
		_install_doa_floor_script(site, calls, floor_max=50000.0, prec=0)
		api_ns, extra_mods, posted, _actions, _emitted, _wf = self._build_api_env(
			site, currency="UGX", workflow_active=True,
		)
		orig_post = api_ns["_post_allowance"]

		def strict_workflow_post_allowance(folio, amount, text, gst_rate, approver=None,
		                                   requested_by=None, req=None, reason_code=None):
			if req is None:
				raise _ValidationError("Workflow State transition not allowed from Pending to Approved")
			return orig_post(folio, amount, text, gst_rate, approver=approver,
			                 requested_by=requested_by, req=req, reason_code=reason_code)

		api_ns["_post_allowance"] = strict_workflow_post_allowance

		with patch.dict(sys.modules, extra_mods):
			res = api_ns["post_allowance"](
				"FOL-001", 50000, reason="Floor PIN with active workflow",
				supervisor="duty@alpha.ug", supervisor_pin="4821",
			)
		self.assertTrue(res["ok"])
		self.assertEqual(res["approved_by"], "duty@alpha.ug")
		self.assertTrue(res["serial"])
		self.assertEqual(len(site.tables["Allowance Request"]), 1)
		saved_req = site.tables["Allowance Request"][0]
		self.assertEqual(saved_req["status"], "Approved")
		self.assertEqual(saved_req["decided_by"], "duty@alpha.ug")
		self.assertEqual(saved_req["serial"], res["serial"])

	def _open_workflow_actions(self, site, request):
		return site.get_all("Workflow Action", filters={
			"reference_doctype": "Allowance Request", "reference_name": request, "status": "Open",
		})

	def test_post_allowance_floor_pin_leaves_no_open_workflow_action(self):
		site = _base_site()
		_install_doa_floor_script(site, [])
		api, modules, posted, *_ = self._build_api_env(site)

		with patch.dict(sys.modules, modules):
			result = api["post_allowance"](
				"FOL-001", 20000, reason="Service recovery",
				supervisor="duty@alpha.ug", supervisor_pin="4821",
			)

		self.assertEqual(len(posted), 1)
		self.assertEqual(result["approved_by"], "duty@alpha.ug")
		request, = site.tables["Allowance Request"]
		self.assertEqual(request.status, "Approved")
		self.assertEqual(len(self._open_workflow_actions(site, request.name)), 0)

	def test_post_allowance_approval_off_leaves_no_open_workflow_action(self):
		site = _base_site()
		api, modules, posted, *_ = self._build_api_env(site, allowance_approval="Off")

		with patch.dict(sys.modules, modules):
			result = api["post_allowance"]("FOL-001", 20000, reason="Service recovery")

		self.assertEqual(len(posted), 1)
		request, = site.tables["Allowance Request"]
		self.assertEqual(request.status, "Approved")
		self.assertEqual(request.serial, result["serial"])
		self.assertEqual(len(self._open_workflow_actions(site, request.name)), 0)

	def test_kamra_decisions_leave_no_open_workflow_actions(self):
		for approve, status in ((1, "Approved"), (0, "Rejected")):
			with self.subTest(status=status):
				site = _base_site()
				api, modules, posted, *_ = self._build_api_env(site)
				with patch.dict(sys.modules, modules):
					request = api["post_allowance"]("FOL-001", 20000, reason="Recovery")["request"]
					other_request = api["post_allowance"]("FOL-001", 10000, reason="Other")["request"]
					self.assertEqual(len(self._open_workflow_actions(site, request)), 1)
					# An extra open row for the same request is left by native completion.
					original = site.tables["Workflow Action"][0]
					site.tables["Workflow Action"].append({**original, "name": "WA-extra"})
					site.set_user("finance@alpha.ug")
					result = api["decide_allowance"](request, approve=approve, note="Reviewed")

				self.assertEqual(result["status"], status)
				self.assertEqual(len(posted), approve)
				self.assertEqual(len(self._open_workflow_actions(site, request)), 0)
				self.assertEqual(len(self._open_workflow_actions(site, other_request)), 1)

	def test_standalone_allowances_work_without_workflow_action_table(self):
		site = _base_site()
		api, modules, posted, *_ = self._build_api_env(
			site, allowance_approval="Off", workflow_active=False,
		)
		self.assertFalse(site.db.table_exists("Workflow Action"))

		with patch.dict(sys.modules, modules):
			result = api["post_allowance"]("FOL-001", 20000, reason="Service recovery")
			modules["kamra.settings"].get_setting = lambda prop, key, default=None: (
				"Hotel Admin or Finance" if key == "allowance_approval" else default
			)
			request = api["post_allowance"]("FOL-001", 10000, reason="Pending review")["request"]
			site.set_user("finance@alpha.ug")
			decision = api["decide_allowance"](request, approve=0, note="Reviewed")

		self.assertEqual(result["serial"], posted[0]["serial"])
		self.assertEqual(decision["status"], "Rejected")
		self.assertEqual(len(posted), 1)

	def test_decide_allowance_runs_native_apply_workflow_and_blocks_self_and_admin(self):
		site = _base_site(user="finance@alpha.ug", currency="KWD", floor_max=10.000)
		req_doc = _Doc({
			"doctype": "Allowance Request",
			"name": "ALW-00010",
			"property": "Gulf Palace",
			"folio": "FOL-001",
			"amount": 15.125,
			"gst_rate": 0,
			"reason": "VIP suite recovery",
			"status": "Pending",
			"requested_by": "desk@alpha.ug",
			"owner": "desk@alpha.ug",
			"serial": None,
		}, store=site.tables)
		site.tables["Allowance Request"] = [req_doc]

		api_ns, extra_mods, posted, _actions, emitted, wf_calls = self._build_api_env(
			site, currency="KWD", workflow_active=True,
		)

		with patch.dict(sys.modules, extra_mods):
			# 1. Self-approval (requester == session.user) fails closed on BOTH Approve and Reject!
			site.set_user("desk@alpha.ug")
			for approve_flag in (1, 0):
				with self.assertRaisesRegex(_PermissionError, r"HSP-DOA-SELF"):
					api_ns["decide_allowance"]("ALW-00010", approve=approve_flag, note="Self")
			self.assertEqual(posted, [])
			self.assertEqual(wf_calls, [])

			# 2. Administrator cannot decide via workflow
			site.set_user("Administrator")
			with self.assertRaisesRegex(_PermissionError, r"HSP-DOA-ROLE"):
				api_ns["decide_allowance"]("ALW-00010", approve=1)
			self.assertEqual(posted, [])
			self.assertEqual(wf_calls, [])

			# 3. Authorized second back-office approver (Finance) executes native apply_workflow and posts KWD 3dp cleanly
			site.set_user("finance@alpha.ug")
			res = api_ns["decide_allowance"]("ALW-00010", approve=1)
			self.assertEqual(res["ok"], True)
			self.assertEqual(res["status"], "Approved")
			self.assertEqual(res["decided_by"], "finance@alpha.ug")
			self.assertTrue(res["serial"])
			self.assertEqual(len(wf_calls), 1)
			self.assertEqual(wf_calls[0][:3], ("ALW-00010", "Approve", "finance@alpha.ug"))
			self.assertEqual(len(posted), 1)
			self.assertEqual(posted[0]["amount"], 15.125)
			self.assertEqual(emitted[-1], ("approval.decided", "ALW-00010", "Approved"))

	def test_decide_allowance_fails_closed_when_workflow_rejects_doa_mismatch(self):
		site = _base_site(user="finance@alpha.ug", currency="UGX", floor_max=50000.0)
		req_doc = _Doc({
			"doctype": "Allowance Request",
			"name": "ALW-00011",
			"property": "Grand Kampala",
			"folio": "FOL-001",
			"amount": 150000.0,
			"gst_rate": 0,
			"reason": "Conference credit",
			"status": "Pending",
			"requested_by": "desk@alpha.ug",
			"owner": "desk@alpha.ug",
			"serial": None,
		}, store=site.tables)
		site.tables["Allowance Request"] = [req_doc]

		def refuse_doa(_doc, _action):
			site.throw("[HSP-DOA-ROLE] Approver not in custom_backoffice_allowance_approver_roles.")

		api_ns, extra_mods, posted, _actions, _emitted, wf_calls = self._build_api_env(
			site, currency="UGX", workflow_active=True, workflow_side_effect=refuse_doa,
		)

		with patch.dict(sys.modules, extra_mods):
			with self.assertRaisesRegex(_ValidationError, r"HSP-DOA-ROLE"):
				api_ns["decide_allowance"]("ALW-00011", approve=1)
			# Folio MUST NOT be touched when workflow fails closed!
			self.assertEqual(posted, [])
			self.assertEqual(len(wf_calls), 1)


if __name__ == "__main__":
	unittest.main()
