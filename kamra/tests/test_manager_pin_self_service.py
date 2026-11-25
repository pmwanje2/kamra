"""Site-less contracts for self-service and the cross-app Cashier PIN hook."""

import hashlib
import json
import hmac
import runpy
import sys
import unittest
from traceback import walk_tb
from datetime import timedelta
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from kamra.tests.test_p53_doa_floor_pin import KAMRA, _Doc, _FakeFrappe, _load_module, _match_filters
from kamra.tests.test_white_label import load_parts

USER = "manager@example.test"
OTHER = "cashier@example.test"
ROLES = (
	"Finance", "Front Desk", "Revenue Manager", "Housekeeping", "F&B Manager",
	"Hotel Admin", "Night Auditor", "URY Manager", "URY Admin", "Accounts Manager",
)


class AuthenticationError(Exception):
	pass


class ManagerPinSelfServiceTest(unittest.TestCase):
	def setUp(self):
		self.site = _FakeFrappe(user=USER, tables={
			"User": [{"name": user, "enabled": 1} for user in (USER, OTHER, "Guest", "Administrator")],
			"Cashier PIN": [],
			"Cashier PIN Attempt": [],
			"DefaultValue": [],
		})
		self.site.user_roles.update({USER: ["Finance"], OTHER: ["Front Desk"]})
		self.site.AuthenticationError = AuthenticationError
		original_get_value = self.site.db.get_value
		self.site.db.get_value = lambda *a, for_update=False, **kw: original_get_value(*a, **kw)
		original_get_doc = self.site.get_doc

		def get_doc(value, name=None, **kwargs):
			if isinstance(value, dict) and value.get("doctype") == "Cashier PIN":
				value = {**value, "name": value["user"]}
			return original_get_doc(value, name, **kwargs)

		self.site.get_doc = get_doc
		self.site.db.commit = Mock()
		self.site.db.close = Mock()
		self.site.db.delete = lambda doctype, filters: self.site.tables[doctype].__setitem__(
			slice(None), [row for row in self.site.tables[doctype] if not _match_filters(row, filters)],
		)
		self.site.local.db = self.site.db

		def bulk_insert(doctype, fields, values, **kwargs):
			for value in values:
				row = dict(zip(fields, value))
				if not self.site.db.exists(doctype, row["name"]):
					self.site.tables[doctype].append(row)

		self.site.db.bulk_insert = bulk_insert
		audit = ModuleType("kamra.savings")
		self.audit = audit.log_action = Mock()
		self.authz, modules = _load_module("kamra.authz", "authz.py", self.site, {"kamra.savings": audit})
		self.check_password = modules["frappe.utils.password"].check_password = Mock(return_value=USER)
		modules["frappe.utils.password"].get_decrypted_password = (
			lambda doctype, user, field, **kwargs: self.site.db.get_value(doctype, user, field)
		)
		modules["kamra.authz"] = self.authz
		modules["frappe.utils.password"].remove_encrypted_password = (
			lambda doctype, user, field: self.site.db.set_value(doctype, user, field, None)
		)
		database = modules["frappe.database"] = ModuleType("frappe.database")
		database.get_db = Mock(return_value=self.site.db)
		self.bridge = modules["kamra.erp_bridge"]
		self.frappe = modules["frappe"]
		patcher = patch.dict(sys.modules, modules)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.api = load_parts(
			"api.py", {"PIN_SELF_SERVICE_ROLES", "my_pin_status", "set_cashier_pin", "reset_cashier_pin",
			           "verify_cashier_pin", "correct_folio_payment", "refund_folio_payment", "decide_allowance"},
			modules["frappe"], imports=("kamra.authz",), _=self.site._, __name__="kamra.api",
		)

	def seed_pin(self, user=USER, **values):
		row = _Doc({"name": user, "pin": "1234", "must_reset": 0, **values})
		self.site.tables["Cashier PIN"].append(row)
		return row

	def test_named_roles_can_read_property_free_status(self):
		for role in ROLES:
			with self.subTest(role=role):
				self.site.user_roles[USER] = [role]
				self.assertEqual(self.api["my_pin_status"](), {
					"has_pin": False, "must_reset": False, "locked": False, "locked_until": None,
				})

	def test_password_enroll_rotate_and_reenroll_with_no_current_pin(self):
		set_pin = self.api["set_cashier_pin"]
		self.site.local.form_dict.update(pin="0246", current_pin="", current_password="account-secret", keep=True)
		self.assertEqual(set_pin("0246", current_password="account-secret"), {"ok": True})
		self.assertEqual(self.site.local.form_dict, {"keep": True})
		doc = self.site.get_doc("Cashier PIN", USER)
		self.assert_hashed(doc, "0246")
		self.assertEqual(set_pin("6420", current_password="account-secret"), {"ok": True})
		doc.must_reset = 1
		doc.pin_attempts = 3
		self.site.cache.set_value(f"kamra_cashier_unlock:{USER}", True)
		self.assertEqual(set_pin("4057", current_password="account-secret"), {"ok": True})
		self.assert_hashed(doc, "4057")
		self.assertEqual((doc.must_reset, doc.pin_attempts, doc.locked_until), (0, 0, None))
		self.assertIsNone(self.site.cache.get_value(f"kamra_cashier_unlock:{USER}"))
		self.assertEqual(self.check_password.call_count, 3)
		self.audit.assert_called_with("set_cashier_pin", "Cashier PIN", USER, None)
		self.assertNotIn("account-secret", repr(self.audit.call_args_list))

	def test_bad_password_uses_validation_error_and_locks_for_15_minutes(self):
		set_pin = self.api["set_cashier_pin"]
		self.check_password.side_effect = AuthenticationError
		for _ in range(5):
			self.site.local.form_dict.update(pin="4057", password="incorrect")
			with self.assertRaises(self.site.ValidationError):
				set_pin("4057", current_password="incorrect")
			self.assertEqual(self.site.local.form_dict, {})
		self.assertFalse(self.site.db.exists("Cashier PIN", USER))
		self.site.cache._data.clear()
		status = self.api["my_pin_status"]()
		self.assertTrue(status["locked"])
		self.assertEqual(status["locked_until"], str(self.site.now + timedelta(minutes=15)))
		self.check_password.side_effect = None
		with self.assertRaises(self.site.ValidationError):
			set_pin("4057", current_password="correct")
		self.assertEqual(self.check_password.call_count, 5)
		self.site.now += timedelta(minutes=15)
		self.assertEqual(set_pin("4057", current_password="correct"), {"ok": True})

	def assert_hashed(self, doc, pin):
		self.assertIsNone(doc.pin)
		algorithm, rounds, salt, digest = (doc.pin_hash or "").split("$")
		self.assertEqual(algorithm, "pbkdf2_sha256")
		self.assertGreaterEqual(int(rounds), 600_000)
		self.assertGreaterEqual(len(bytes.fromhex(salt)), 16)
		expected = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(rounds))
		self.assertTrue(hmac.compare_digest(expected, bytes.fromhex(digest)))

	def test_every_enrollment_state_requires_account_password(self):
		for state in ("new", "reset", "rotation"):
			with self.subTest(state=state):
				self.site.tables["Cashier PIN"].clear()
				if state != "new":
					self.seed_pin(must_reset=int(state == "reset"))
				with self.assertRaises(self.site.ValidationError):
					self.api["set_cashier_pin"]("2605", current_pin="1234")
		self.check_password.assert_not_called()

	def test_shared_writer_rejects_weak_or_inexact_pin(self):
		for pin in (" 4067", "4067 ", "４０６７", "1111", "000000", "1234", "654321", "135", "123456789"):
			with self.subTest(pin=pin), self.assertRaises(self.site.ValidationError):
				self.authz.write_cashier_pin(USER, pin)
		self.assertFalse(self.site.db.exists("Cashier PIN", USER))

	def test_verified_legacy_pin_is_migrated_and_checked_in_constant_time(self):
		row = self.seed_pin()
		with patch("hmac.compare_digest", wraps=hmac.compare_digest) as compare:
			self.authz._check_user_pin(USER, "1234", None)
			self.assert_hashed(row, "1234")
			self.site.set_user(OTHER)
			self.assertEqual(self.authz.verify_supervisor("", USER, "1234", requester=OTHER), USER)
			self.assertGreaterEqual(compare.call_count, 2)

	def test_new_hashes_have_distinct_salts(self):
		self.api["set_cashier_pin"]("2605", current_password="account-secret")
		first = self.site.get_doc("Cashier PIN", USER).pin_hash
		self.api["set_cashier_pin"]("2605", current_password="account-secret")
		second = self.site.get_doc("Cashier PIN", USER).pin_hash
		self.assertIsNotNone(first)
		self.assertNotEqual(first, second)

	def test_till_and_supervisor_guesses_share_durable_pin_lockout(self):
		self.seed_pin()
		for _ in range(2):
			with self.assertRaisesRegex(self.site.ValidationError, "Wrong cashier PIN"):
				self.authz._check_user_pin(USER, "2948", None)
		self.site.set_user(OTHER)
		for _ in range(2):
			with self.assertRaisesRegex(self.site.ValidationError, "Wrong cashier PIN"):
				self.authz.verify_supervisor("", USER, "2948", requester=OTHER)
		with self.assertRaisesRegex(self.site.ValidationError, "PIN_LOCKED"):
			self.authz.verify_supervisor("", USER, "2948", requester=OTHER)
		self.site.cache._data.clear()
		self.assertTrue(self.site.tables["Cashier PIN Attempt"])
		with self.assertRaisesRegex(self.site.ValidationError, "PIN_LOCKED"):
			self.authz._check_user_pin(USER, "1234", None)
		self.site.now += timedelta(minutes=15)
		with self.assertRaisesRegex(self.site.ValidationError, "Wrong cashier PIN"):
			self.authz._check_user_pin(USER, "2948", None)
		self.authz._check_user_pin(USER, "1234", None)

	def test_forbidden_sessions_cannot_set_or_read_pin_and_form_is_scrubbed(self):
		cases = [
			("Guest", ["Finance"], True, {}),
			("Administrator", ["Administrator"], True, {}),
			(USER, ["Finance"], False, {}),
			(USER, ["Finance", "Kamra Agent"], True, {}),
			(USER, ["Finance"], True, {"kamra_agent_call": True}),
			(USER, ["Finance"], True, {"kamra_gate_bypass": True}),
			(USER, [], True, {}),
		]
		for user, roles, enabled, flags in cases:
			with self.subTest(user=user, roles=roles, enabled=enabled, flags=flags):
				self.site.set_user(user)
				self.site.user_roles[user] = roles
				self.site.flags.clear()
				self.site.flags.update(flags)
				next(row for row in self.site.tables["User"] if row["name"] == user)["enabled"] = enabled
				self.site.local.form_dict.update(pin="4057", current_pin="1234", password="secret")
				with self.assertRaises(self.site.PermissionError):
					self.api["set_cashier_pin"]("4057", password="secret")
				self.assertEqual(self.site.local.form_dict, {})
				with self.assertRaises(self.site.PermissionError):
					self.api["my_pin_status"]()

	def test_hook_registration_status_set_clear_and_canonical_user(self):
		hooks = runpy.run_path(str(KAMRA / "hooks.py"))
		self.assertEqual(hooks.get("pos_override_pin_manager"), "kamra.authz.manage_pos_override_pin")
		manage = self.authz.manage_pos_override_pin
		self.assertEqual(manage("status", user=USER.upper()), {
			"has_pin": False, "must_reset": False, "locked": False,
		})
		self.assertEqual(manage("set", user=USER.upper(), pin="4057", current_password="account-secret"), {"ok": True, "has_pin": True})
		self.assertEqual(manage("status"), {"has_pin": True, "must_reset": False, "locked": False})
		self.assertEqual(manage("clear", caller=USER), {"ok": True, "has_pin": False, "must_reset": True})
		self.assertEqual(manage("status"), {"has_pin": False, "must_reset": True, "locked": False})

	def test_hook_refuses_to_set_another_users_pin_or_accept_spoofed_caller(self):
		self.site.user_roles[USER] = ["Hotel Admin"]
		manage = self.authz.manage_pos_override_pin
		self.site.local.form_dict.update(pin="4057", override_pin="1234")
		with self.assertRaises(self.site.PermissionError):
			manage("set", user=OTHER, pin="4057")
		self.assertEqual(self.site.local.form_dict, {})
		with self.assertRaises(self.site.PermissionError):
			manage("clear", user=OTHER, caller=OTHER)
		self.assertFalse(self.site.db.exists("Cashier PIN", OTHER))
		self.assertEqual(manage("clear", user=OTHER, caller=USER), {
			"ok": True, "has_pin": False, "must_reset": True,
		})
		self.assertIn(USER, repr(self.audit.call_args))

	def test_web_enrollment_requires_current_password_even_with_old_fields(self):
		with self.assertRaises(self.site.ValidationError):
			self.api["set_cashier_pin"]("4067", password="account-secret", current_pin="1234")
		self.assertFalse(self.site.db.exists("Cashier PIN", USER))

	def test_companion_pos_hook_accepts_internal_reauthentication_for_its_actor(self):
		manage = self.authz.manage_pos_override_pin
		for verified in ({}, {"password_verified": True}):
			with self.subTest(verified=verified):
				self.assertTrue(manage("set", user=USER, pin="4067", **verified)["ok"])
		self.check_password.assert_not_called()
		with self.assertRaises(self.site.PermissionError):
			manage("set", user=OTHER, pin="4067", password_verified=True)
		self.check_password.side_effect = AuthenticationError
		with self.assertRaises(self.site.ValidationError):
			manage("set", user=USER, pin="8260", password_verified=True, current_password="invalid")
		self.check_password.assert_called_once_with(USER, "invalid", delete_tracker_cache=False)
		self.assert_hashed(self.site.get_doc("Cashier PIN", USER), "4067")

	def test_pos_status_allows_multi_property_staff_at_an_authorized_till(self):
		self.site.user_roles[USER] = ["URY Manager"]
		self.site.user_permissions = {
			USER: {"Property": [{"doc": "Alpha"}], "Company": [{"doc": "Alpha Ltd"}]},
			OTHER: {"Property": [{"doc": "Alpha"}, {"doc": "Beta"}],
			        "Company": [{"doc": "Alpha Ltd"}, {"doc": "Beta Ltd"}]},
		}
		with patch.object(self.authz, "resolve_property_for_pos_profile", return_value="Alpha"), \
		     patch.object(self.authz, "_property_company", return_value="Alpha Ltd"):
			status = self.authz.manage_pos_override_pin("status", user=OTHER, pos_profile="Till")
			self.assertFalse(status["has_pin"])

	def test_reset_checks_full_company_scope_and_empty_strict_property_scope(self):
		self.site.user_roles[USER] = ["Hotel Admin"]
		scenarios = [
			({"Property": [{"doc": "Alpha"}], "Company": [{"doc": "Alpha Ltd"}]},
			 {"Property": [{"doc": "Alpha"}], "Company": [{"doc": "Beta Ltd"}]}),
			({}, {}),
		]
		for actor, target in scenarios:
			with self.subTest(actor=actor, target=target):
				self.site.user_permissions = {USER: actor, OTHER: target}
				self.site.conf["kamra_strict_property_scope"] = 1
				with self.assertRaises(self.site.PermissionError):
					self.api["reset_cashier_pin"](OTHER)

	def test_passed_credentials_are_removed_before_permission_denials(self):
		self.site.user_roles[USER] = []
		credentials = dict.fromkeys(
			("pin", "current_pin", "new_pin", "confirm_pin", "supervisor_pin", "password", "current_password"),
			"synthetic-secret",
		)
		for method, args in (
			("set_cashier_pin", ("4067",)),
			("verify_cashier_pin", ("Local Property", "4067")),
			("correct_folio_payment", ("FOL-1", "PAY-1")),
			("refund_folio_payment", ("FOL-1",)),
		):
			with self.subTest(method=method):
				self.site.local.form_dict.update(credentials, keep=True)
				original_throw = self.site.throw
				with patch.object(self.frappe, "throw", wraps=original_throw) as throw:
					def check_scrubbing(*args, **kwargs):
						self.assertEqual(self.site.local.form_dict, {"keep": True})
						original_throw(*args, **kwargs)

					throw.side_effect = check_scrubbing
					with self.assertRaises(self.site.PermissionError):
						self.api[method](*args)

	def test_staff_wire_status_for_unenrolled_user_is_none(self):
		self.site.tables["Has Role"] = [{"parent": OTHER, "role": "Front Desk", "parenttype": "User"}]
		self.site.tables["User"][1].update(user_type="System User", full_name="Guest Services")
		staff = load_parts(
			"staff.py", {"list_staff"}, self.frappe, imports=("kamra.authz",),
			ROLE_NAMES=("Front Desk",), NEVER_LISTED=(), IT_ONLY=(),
			_caller_is_it=lambda: True, _visible_to=lambda *_: True,
		)
		self.site.user_roles[USER] = ["Hotel Admin"]
		self.assertEqual(staff["list_staff"]("Alpha")[0]["pin"], "none")

	def test_floor_only_pin_is_reported_for_self_service_and_row_corrections(self):
		self.site.tables["User"][0].update(user_type="System User", full_name="Shift Manager")
		self.site.tables["Has Role"] = [{"parent": USER, "role": "Finance", "parenttype": "User"}]
		self.site.tables["DefaultValue"].append({
			"name": "floor-hash", "parent": USER, "parenttype": "SiteOps Floor PIN",
			"defkey": "siteops_floor_pin_hash", "defvalue": "existing-floor-only-record",
		})
		with patch.object(self.authz, "_doa_floor_script", return_value=object()):
			self.assertTrue(self.api["my_pin_status"]()["has_pin"])
			self.site.set_user(OTHER)
			rows = self.authz.supervisors_for("Alpha", exclude=OTHER, include_pin_status=True)
			self.assertEqual([row["user"] for row in rows], [USER])
			self.assertTrue(rows[0]["has_pin"])
			self.assertFalse(rows[0]["must_reset"])

	def test_reset_revokes_both_store_entries_when_floor_bridge_is_active(self):
		self.site.user_roles[USER] = ["Hotel Admin"]
		self.site.tables["DefaultValue"].append({
			"name": "floor-hash", "parent": OTHER, "parenttype": "SiteOps Floor PIN",
			"defkey": "siteops_floor_pin_hash", "defvalue": "existing-floor-only-record",
		})
		with patch.object(self.authz, "_doa_floor_script", return_value=object()):
			self.assertTrue(self.api["reset_cashier_pin"](OTHER)["must_reset"])
			self.assertFalse(any(row.get("defvalue") for row in self.site.tables["DefaultValue"]
			                     if row.get("defkey") == "siteops_floor_pin_hash"))
			self.site.set_user(OTHER)
			self.assertFalse(self.api["my_pin_status"]()["has_pin"])

	def test_pos_staff_listing_status_without_profile_accepts_shared_scope(self):
		self.site.user_roles[USER] = ["URY Manager"]
		self.site.user_permissions = {
			USER: {"Property": [{"doc": "Alpha"}], "Company": [{"doc": "Alpha Ltd"}]},
			OTHER: {"Property": [{"doc": "Alpha"}, {"doc": "Beta"}],
			        "Company": [{"doc": "Alpha Ltd"}, {"doc": "Beta Ltd"}]},
		}
		self.seed_pin(OTHER)
		self.assertTrue(self.authz.manage_pos_override_pin("status", user=OTHER)["has_pin"])

	def test_explicit_unbound_or_missing_pos_profile_cannot_use_sole_property(self):
		self.site.user_roles[USER] = ["Hotel Admin"]
		self.site.tables["Property"] = [{"name": "Alpha", "disabled": 0}]
		self.site.tables["POS Profile"] = [{"name": "Unbound Till"}]
		for profile in ("Missing Till", "Unbound Till"):
			for action in ("status", "clear", "set"):
				with self.subTest(profile=profile, action=action), self.assertRaises(self.site.PermissionError):
					self.authz.manage_pos_override_pin(
						action, user=USER, pos_profile=profile, pin="4067", current_password="account-secret",
					)

	def horizontal_siteops(self):
		methods = (
			"siteops_doa.my_floor_pin_status", "siteops_doa.set_my_floor_pin",
			"siteops_doa.reset_user_floor_pin", "siteops_doa.verify_floor_authority",
		)
		calls = []

		def run(method, form):
			calls.append((method, dict(form)))
			if method == methods[0]:
				return {"store": "siteops_floor_pin", "has_pin": bool(self.floor_hash(self.site.session.user)),
				        "must_reset": False, "locked": False}
			if method == methods[1]:
				self.put_floor_hash(self.site.session.user, "native-enrollment")
				return {"ok": 1, "has_pin": 1, "store": "siteops_floor_pin"}
			if method == methods[2]:
				self.put_floor_hash(form["user"], "")
				return {"ok": 1, "user": form["user"], "must_reset": 1}
			return {"ok": 1, "approver": form["approver"]}

		class Script(str):
			script = 'return {"store": "siteops_floor_pin"}'
		self.enterContext(patch.object(self.bridge, "get_bridge_script",
		                               side_effect=lambda method: Script(method) if method in methods else None))
		self.enterContext(patch.object(self.bridge, "call_bridge_script", side_effect=run))
		return calls

	def put_floor_hash(self, user, digest):
		filters = {"parent": user, "parenttype": "SiteOps Floor PIN", "defkey": "siteops_floor_pin_hash"}
		self.site.db.delete("DefaultValue", filters)
		self.site.tables["DefaultValue"].append(dict(filters, name=f"floor-{user}", defvalue=digest))

	def floor_hash(self, user):
		return self.site.db.get_value(
			"DefaultValue", {"parent": user, "parenttype": "SiteOps Floor PIN", "defkey": "siteops_floor_pin_hash"},
			"defvalue",
		)

	def test_v7_web_enrollment_and_status_use_native_horizontal_methods(self):
		calls = self.horizontal_siteops()
		self.seed_pin()
		self.assertFalse(self.api["my_pin_status"]()["has_pin"])
		self.assertEqual(self.api["set_cashier_pin"]("4067", current_password="account-secret"), {"ok": True})
		self.assertTrue(self.api["my_pin_status"]()["has_pin"])
		self.assertIn(("siteops_doa.set_my_floor_pin", {"pin": "4067", "password": "account-secret"}), calls)
		self.assertIsNone(self.site.get_doc("Cashier PIN", USER).pin_hash)

	def test_v7_pos_preauth_enrolls_in_the_native_verifier_store(self):
		self.horizontal_siteops()
		self.assertTrue(self.authz.manage_pos_override_pin("set", user=USER, pin="4067")["ok"])
		algorithm, rounds, salt, digest = self.floor_hash(USER).split("$")
		self.assertEqual((algorithm, rounds), ("pbkdf2_sha256", "200000"))
		key = hashlib.sha256(b"siteops_floor_pin_v2|4067").digest()
		self.assertEqual(hashlib.pbkdf2_hmac("sha256", key, bytes.fromhex(salt), int(rounds)).hex(), digest)
		self.assertFalse(self.site.db.exists("Cashier PIN", USER))

	def test_v7_verification_never_moves_or_deletes_the_horizontal_credential(self):
		calls = self.horizontal_siteops()
		pin = "4067"
		digest = hashlib.sha256(f"siteops_floor_pin_v1|{USER}|{pin}".encode()).hexdigest()
		self.put_floor_hash(USER, digest)
		self.site.set_user(OTHER)
		self.assertEqual(self.authz.verify_supervisor("Alpha", USER, pin, requester=OTHER), USER)
		self.assertTrue(any(name == "siteops_doa.verify_floor_authority" for name, _ in calls))
		self.assertEqual(self.floor_hash(USER), digest)
		self.assertFalse(self.site.db.exists("Cashier PIN", USER))

	def test_v7_reset_delegates_without_creating_a_local_pin_record(self):
		calls = self.horizontal_siteops()
		self.site.user_roles[USER] = ["Hotel Admin"]
		self.put_floor_hash(OTHER, "native-enrollment")
		self.assertTrue(self.api["reset_cashier_pin"](OTHER)["must_reset"])
		self.assertTrue(any(name == "siteops_doa.reset_user_floor_pin" and args["user"] == OTHER
		                    for name, args in calls))
		self.assertFalse(self.site.db.exists("Cashier PIN", OTHER))
		self.assertFalse(self.floor_hash(OTHER))

	def test_allowance_entry_scrubs_metadata_before_permission_check(self):
		self.site.user_roles[USER] = []
		keys = ("pin", "new_pin", "confirm_pin", "current_password", "manager_pin", "override_pin")
		self.site.local.form_dict.update(dict.fromkeys(keys, "request-secret"), keep=True)
		original_throw = self.site.throw

		def check_form(*args, **kwargs):
			self.assertEqual(self.site.local.form_dict, {"keep": True})
			original_throw(*args, **kwargs)

		with patch.object(self.frappe, "throw", side_effect=check_form), self.assertRaises(self.site.PermissionError):
			self.api["decide_allowance"]("ALLOW-1", pin="7091")

	def test_pin_and_password_have_redacted_reprs_in_raised_endpoint_frames(self):
		self.site.user_roles[USER] = []
		try:
			self.api["set_cashier_pin"]("30971482", current_password="traceback-only-password")
		except self.site.PermissionError as e:
			frames = [repr(frame.f_locals) for frame, _ in walk_tb(e.__traceback__)
			          if frame.f_code.co_filename in (str(KAMRA / "authz.py"), str(KAMRA / "api.py"))]
			self.assertTrue(frames)
			self.assertNotIn("30971482", "\n".join(frames))
			self.assertNotIn("traceback-only-password", "\n".join(frames))
		else:
			self.fail("A role denial must raise")

	def test_http_entry_stages_opaque_credentials_outside_frappe_form_dict(self):
		self.site.local.request = SimpleNamespace(path="/api/method/kamra.api.set_cashier_pin")
		self.site.local.form_dict.update(pin="59872041", current_password="http-context-password", keep=True)
		self.authz.prepare_pin_request()
		self.assertEqual(self.site.local.form_dict, {"keep": True})
		self.site.local.request = None
		self.assertEqual(self.api["set_cashier_pin"](), {"ok": True})
		self.check_password.assert_called_once_with(USER, "http-context-password", delete_tracker_cache=False)

	def test_wrong_cashier_pin_is_localized_by_backend_and_frontend(self):
		self.seed_pin()
		message = "Code PIN de caisse incorrect."
		with patch.object(self.frappe, "_", side_effect=lambda key: message if key == "Wrong cashier PIN." else key):
			with self.assertRaisesRegex(self.site.ValidationError, message):
				self.authz._check_user_pin(USER, "4067", None)
		locales = KAMRA.parent / "frontend" / "src" / "i18n" / "locales"
		self.assertEqual(json.loads((locales / "fr.json").read_text())["Wrong cashier PIN."], message)
