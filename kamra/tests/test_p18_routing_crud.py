"""RED->GREEN tests for P18 Routing Instruction UI CRUD endpoints & cross-property isolation."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

KAMRA_ROOT = Path(__file__).resolve().parents[1]


class TestP18RoutingCrudSurface(unittest.TestCase):
	"""Siteless contract checks verifying routing CRUD whitelisted endpoints exist."""

	def test_routing_module_exports_crud_endpoints(self):
		source = (KAMRA_ROOT / "routing.py").read_text(encoding="utf-8")
		tree = ast.parse(source)
		funcs = {
			node.name: node
			for node in tree.body
			if isinstance(node, ast.FunctionDef)
		}
		for fn_name, expected_params in (
			("list_routing_instructions", {"reservation", "group_booking"}),
			("save_routing_instruction", {"data"}),
			("delete_routing_instruction", {"name"}),
		):
			self.assertIn(fn_name, funcs, f"kamra.routing.{fn_name} missing")
			node = funcs[fn_name]
			decos = [ast.unparse(d) for d in node.decorator_list]
			self.assertTrue(
				any("whitelist" in d for d in decos),
				f"kamra.routing.{fn_name} must be whitelisted",
			)
			params = {a.arg for a in node.args.args}
			self.assertTrue(
				expected_params.issubset(params),
				f"kamra.routing.{fn_name} missing params {expected_params - params}",
			)


try:
	import frappe
	from frappe.utils import add_days, getdate
	from kamra import api, ledger, routing, settings
	from kamra import folio as folio_mod
	from kamra.tests.audit_fixtures import (
		TODAY,
		AuditTestCase,
		ensure_user,
		make_property,
		make_reservation,
		make_room,
		make_room_type,
	)
except ImportError:
	frappe = None


if frappe is not None:
	PROP_A = "UT P18 Route A"
	PROP_B = "UT P18 Route B"

	def _grant_property(user: str, prop: str):
		for existing in frappe.get_all("User Permission", filters={"user": user, "allow": "Property"}, pluck="name"):
			frappe.delete_doc("User Permission", existing, ignore_permissions=True)
		frappe.get_doc({
			"doctype": "User Permission",
			"user": user,
			"allow": "Property",
			"for_value": prop,
		}).insert(ignore_permissions=True)
		frappe.clear_cache(user=user)

	class TestP18RoutingCrud(AuditTestCase):
		properties = (PROP_A, PROP_B)

		def setUp(self):
			super().setUp()
			self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
			ledger.ensure_default_transaction_codes()
			self.day0 = str(getdate(TODAY))
			self.day2 = str(add_days(self.day0, 2))
			make_property(PROP_A, country="Uganda", currency="UGX", business_date=self.day0, gst_mode="Fixed", gst_rate_low=0, gst_rate_high=0)
			make_property(PROP_B, country="Uganda", currency="UGX", business_date=self.day0, gst_mode="Fixed", gst_rate_low=0, gst_rate_high=0)
			settings.clear_cache(PROP_A)
			settings.clear_cache(PROP_B)
			self.rt_a = make_room_type(PROP_A, "STD", base_price=100000)
			self.rt_b = make_room_type(PROP_B, "STD", base_price=100000)
			self.room_a1 = make_room(PROP_A, self.rt_a, "101")
			self.room_b1 = make_room(PROP_B, self.rt_b, "201")
			self.res_a = make_reservation(PROP_A, self.rt_a, self.day0, self.day2, status="Checked In", room=self.room_a1)
			self.res_b = make_reservation(PROP_B, self.rt_b, self.day0, self.day2, status="Checked In", room=self.room_b1)
			self.w1_a = folio_mod.open_folio(self.res_a)
			self.w2_a = folio_mod.open_window(reservation=self.res_a.name, payee_type="Guest", bill_to_name="Route Target W2")

		def test_routing_crud_lifecycle_and_hard_delete_when_unused(self):
			created = routing.save_routing_instruction({
				"reservation": self.res_a.name,
				"target_type": "Window",
				"target_window": 2,
				"all_codes": 0,
				"codes": [{"transaction_code": "2010"}, {"charge_type": "Laundry"}],
				"from_date": self.day0,
				"to_date": self.day2,
				"monday": 1,
				"tuesday": 1,
				"wednesday": 1,
				"thursday": 1,
				"friday": 1,
				"saturday": 0,
				"sunday": 0,
				"limit_type": "Amount",
				"limit_value": 150000,
				"per_day": 1,
			})
			self.assertTrue(created["name"])
			self.assertEqual(created["target_window"], 2)
			self.assertEqual(created["saturday"], 0)
			self.assertEqual(created["sunday"], 0)
			self.assertEqual(created["limit_type"], "Amount")
			self.assertEqual(float(created["limit_value"]), 150000.0)
			self.assertEqual(float(created["used_amount"]), 0.0)

			listed = routing.list_routing_instructions(reservation=self.res_a.name)
			self.assertEqual(len(listed), 1)
			self.assertEqual(listed[0]["name"], created["name"])

			# Update existing instruction
			updated = routing.save_routing_instruction({
				"name": created["name"],
				"reservation": self.res_a.name,
				"target_type": "Window",
				"target_window": 2,
				"all_codes": 0,
				"codes": [{"transaction_code": "2010"}, {"charge_type": "Laundry"}],
				"from_date": self.day0,
				"to_date": self.day2,
				"monday": 1,
				"tuesday": 1,
				"wednesday": 1,
				"thursday": 1,
				"friday": 1,
				"saturday": 1,
				"sunday": 0,
				"limit_type": "Amount",
				"limit_value": 200000,
				"per_day": 1,
			})
			self.assertEqual(float(updated["limit_value"]), 200000.0)
			self.assertEqual(updated["saturday"], 1)

			# Unused instruction hard-deletes
			deleted = routing.delete_routing_instruction(created["name"])
			self.assertEqual(deleted["action"], "deleted")
			self.assertFalse(frappe.db.exists("Routing Instruction", created["name"]))

		def test_delete_routing_instruction_soft_disables_when_used_or_referenced(self):
			created = routing.save_routing_instruction({
				"reservation": self.res_a.name,
				"target_type": "Window",
				"target_window": 2,
				"all_codes": 0,
				"codes": [{"charge_type": "Laundry"}],
				"limit_type": "Amount",
				"limit_value": 100000,
			})
			# Post charge routed by this instruction
			posted = api.post_stay_charge(self.res_a.name, "Laundry", "Shirt press", 25000)
			self.assertEqual(posted["folio"], self.w2_a)

			# Deleting a used instruction soft-disables it instead of deleting
			out = routing.delete_routing_instruction(created["name"])
			self.assertEqual(out["action"], "disabled")
			self.assertEqual(out["disabled"], 1)
			self.assertGreater(float(out["used_amount"]), 0)
			self.assertTrue(frappe.db.exists("Routing Instruction", created["name"]))
			self.assertEqual(frappe.db.get_value("Routing Instruction", created["name"], "disabled"), 1)

		def test_routing_crud_enforces_property_user_permission_and_bounds(self):
			clerk_a = ensure_user("p18_route_a@example.test", ["Front Desk"])
			clerk_b = ensure_user("p18_route_b@example.test", ["Front Desk"])
			_grant_property(clerk_a, PROP_A)
			_grant_property(clerk_b, PROP_B)

			prev_user = frappe.session.user
			try:
				frappe.set_user(clerk_a)  # nosemgrep: frappe-setuser
				created = routing.save_routing_instruction({
					"reservation": self.res_a.name,
					"target_type": "Window",
					"target_window": 2,
					"codes": [{"charge_type": "Laundry"}],
				})
				self.assertTrue(created["name"])

				# Validation bounds: target window 9 (> 8) and inverted dates rejected
				with self.assertRaises(frappe.ValidationError):
					routing.save_routing_instruction({
						"reservation": self.res_a.name,
						"target_type": "Window",
						"target_window": 9,
						"codes": [{"charge_type": "Spa"}],
					})
				with self.assertRaises(frappe.ValidationError):
					routing.save_routing_instruction({
						"reservation": self.res_a.name,
						"target_type": "Window",
						"target_window": 2,
						"codes": [{"charge_type": "Spa"}],
						"from_date": self.day2,
						"to_date": self.day0,
					})

				# Clerk B restricted to PROP_B gets PermissionError on list/save/delete for PROP_A
				frappe.set_user(clerk_b)  # nosemgrep: frappe-setuser
				with self.assertRaises(frappe.PermissionError):
					routing.list_routing_instructions(reservation=self.res_a.name)
				with self.assertRaises(frappe.PermissionError):
					routing.save_routing_instruction({
						"reservation": self.res_a.name,
						"target_type": "Window",
						"target_window": 2,
						"codes": [{"charge_type": "Minibar"}],
					})
				with self.assertRaises(frappe.PermissionError):
					routing.delete_routing_instruction(created["name"])
			finally:
				frappe.set_user(prev_user)  # nosemgrep: frappe-setuser
