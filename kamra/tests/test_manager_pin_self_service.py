"""Site-less contracts for self-service and the cross-app Cashier PIN hook."""

import runpy
import sys
import unittest
from datetime import timedelta
from types import ModuleType
from unittest.mock import Mock, patch

from kamra.tests.test_p53_doa_floor_pin import KAMRA, _Doc, _FakeFrappe, _load_module
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
		audit = ModuleType("kamra.savings")
		self.audit = audit.log_action = Mock()
		self.authz, modules = _load_module("kamra.authz", "authz.py", self.site, {"kamra.savings": audit})
		self.check_password = modules["frappe.utils.password"].check_password = Mock(return_value=USER)
		modules["frappe.utils.password"].get_decrypted_password = (
			lambda doctype, user, field, **kwargs: self.site.db.get_value(doctype, user, field)
		)
		modules["kamra.authz"] = self.authz
		patcher = patch.dict(sys.modules, modules)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.api = load_parts(
			"api.py", {"PIN_SELF_SERVICE_ROLES", "my_pin_status", "set_cashier_pin"},
			modules["frappe"], imports=("kamra.authz",), _=self.site._,
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
		self.site.local.form_dict.update(pin="0246", current_pin="", password="account-secret", keep=True)
		self.assertEqual(set_pin("0246", password="account-secret"), {"ok": True})
		self.assertEqual(self.site.local.form_dict, {"keep": True})
		doc = self.site.get_doc("Cashier PIN", USER)
		self.assertEqual(doc.pin, "0246")
		self.assertEqual(set_pin("6420", password="account-secret"), {"ok": True})
		doc.must_reset = 1
		doc.pin_attempts = 3
		self.site.cache.set_value(f"kamra_cashier_unlock:{USER}", True)
		self.assertEqual(set_pin("4567", password="account-secret"), {"ok": True})
		self.assertEqual((doc.pin, doc.must_reset, doc.pin_attempts, doc.locked_until), ("4567", 0, 0, None))
		self.assertIsNone(self.site.cache.get_value(f"kamra_cashier_unlock:{USER}"))
		self.assertEqual(self.check_password.call_count, 3)
		self.assertEqual(self.site.db.commit.call_count, 3)
		self.audit.assert_called_with("set_cashier_pin", "Cashier PIN", USER, None)
		self.assertNotIn("account-secret", repr(self.audit.call_args_list))

	def test_bad_password_uses_validation_error_and_locks_for_15_minutes(self):
		set_pin = self.api["set_cashier_pin"]
		self.check_password.side_effect = AuthenticationError
		for _ in range(5):
			self.site.local.form_dict.update(pin="4567", password="incorrect")
			with self.assertRaises(self.site.ValidationError):
				set_pin("4567", password="incorrect")
			self.assertEqual(self.site.local.form_dict, {})
		self.assertFalse(self.site.db.exists("Cashier PIN", USER))
		status = self.api["my_pin_status"]()
		self.assertTrue(status["locked"])
		self.assertEqual(status["locked_until"], str(self.site.now + timedelta(minutes=15)))
		self.check_password.side_effect = None
		with self.assertRaises(self.site.ValidationError):
			set_pin("4567", password="correct")
		self.assertEqual(self.check_password.call_count, 5)
		self.site.now += timedelta(minutes=15)
		self.assertEqual(set_pin("4567", password="correct"), {"ok": True})

	def test_legacy_current_pin_and_ascii_validation(self):
		row = self.seed_pin()
		with self.assertRaises(self.site.ValidationError):
			self.api["set_cashier_pin"]("2345")
		with self.assertRaises(self.site.ValidationError):
			self.api["set_cashier_pin"]("１２３４", current_pin="1234")
		self.assertEqual(self.api["set_cashier_pin"]("2345", current_pin="1234"), {"ok": True})
		self.assertEqual(row.pin, "2345")

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
				self.site.local.form_dict.update(pin="4567", current_pin="1234", password="secret")
				with self.assertRaises(self.site.PermissionError):
					self.api["set_cashier_pin"]("4567", password="secret")
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
		self.assertEqual(manage("set", user=USER.upper(), pin="4567"), {"ok": True, "has_pin": True})
		self.assertEqual(manage("status"), {"has_pin": True, "must_reset": False, "locked": False})
		self.assertEqual(manage("clear", caller=USER), {"ok": True, "has_pin": False, "must_reset": True})
		self.assertEqual(manage("status"), {"has_pin": False, "must_reset": True, "locked": False})

	def test_hook_refuses_to_set_another_users_pin_or_accept_spoofed_caller(self):
		self.site.user_roles[USER] = ["Hotel Admin"]
		manage = self.authz.manage_pos_override_pin
		self.site.local.form_dict.update(pin="4567", override_pin="1234")
		with self.assertRaises(self.site.PermissionError):
			manage("set", user=OTHER, pin="4567")
		self.assertEqual(self.site.local.form_dict, {})
		with self.assertRaises(self.site.PermissionError):
			manage("clear", user=OTHER, caller=OTHER)
		self.assertFalse(self.site.db.exists("Cashier PIN", OTHER))
		self.assertEqual(manage("clear", user=OTHER, caller=USER), {
			"ok": True, "has_pin": False, "must_reset": True,
		})
		self.assertIn(USER, repr(self.audit.call_args))
