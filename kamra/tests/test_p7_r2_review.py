"""Contracts for the R2 public booking review."""
import ast
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from kamra import public_api
from kamra.tests import test_p7_direct_booking_cart as cart

ROOT = Path(__file__).resolve().parents[1]


class TestBookingReview(IntegrationTestCase):
	setUp = cart.TestDirectBookingCart.setUp
	make_room = cart.TestDirectBookingCart.make_room
	line = cart.TestDirectBookingCart.line

	def test_public_blacklist_refusal_contains_only_guest_facing_copy(self):
		# Exercise the controller's error formatting without a public request.
		doc = frappe.get_doc({"doctype": "Reservation", "source": "Website",
		                      "guest": "Profile", "guest_name": "Synthetic person"})
		with patch.object(doc, "is_new", return_value=True), \
		     patch.object(frappe.db, "get_value", return_value=(1, "Staff-only note")):
			with self.assertRaises(frappe.ValidationError) as denied:
				doc.validate_blacklist()
		self.assertNotIn("Synthetic person", str(denied.exception))
		self.assertNotIn("Staff-only note", str(denied.exception))
		self.assertNotIn("blacklist", str(denied.exception).lower())

	def test_public_contact_writers_require_proof_of_guest_creation(self):
		# Keep profile updates in the private new-profile writer. Neither
		# public handler has authority to write directly to a shared Guest.
		for file, handler in (("public_api.py", "book"), ("direct_booking_cart.py", "book")):
			with self.subTest(handler=file):
				fn = next(n for n in ast.parse((ROOT / file).read_text()).body
				          if isinstance(n, ast.FunctionDef) and n.name == handler)
				writes = [ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)
				          and (ast.unparse(n.func) == "set_marketing_consent" or
				               (ast.unparse(n.func) == "frappe.db.set_value" and n.args
				                and ast.literal_eval(n.args[0]) == "Guest"))]
				self.assertEqual(writes, [])
				self.assertIn("save_new_guest_contact", ast.unparse(fn))
		with patch.object(frappe.db, "set_value") as write, patch("kamra.privacy.set_marketing_consent") as consent:
			for result in ({"guest": "Shared", "guest_created": False}, {"guest": "Replay"}):
				public_api.save_new_guest_contact(result, "booking@example.test", 1, self.property)
			write.assert_not_called()
			consent.assert_not_called()
			public_api.save_new_guest_contact({"guest": "New", "guest_created": True},
			                                  "booking@example.test", 1, self.property)
			write.assert_called_once_with("Guest", "New", "email", "booking@example.test")
			consent.assert_called_once_with("New", 1, "Booking Page", self.property)

	def test_public_reads_check_publication_before_loading_property(self):
		class Checked(Exception):
			pass
		def publication(doctype, filters, *args, **kwargs):
			self.assertEqual((doctype, filters), ("Property", {
				"name": self.property, "disabled": 0, "booking_engine_enabled": 1,
			}))
			raise Checked
		for read, args in ((public_api.showcase, ()), (public_api.search_stay, (self.ci, self.co))):
			with self.subTest(endpoint=read.__name__), patch.object(frappe.db, "exists", publication), \
			     patch.object(frappe, "get_doc", side_effect=AssertionError("read before guard")), \
			     patch.object(frappe, "get_cached_doc", side_effect=AssertionError("read before guard")):
				with self.assertRaises(Checked):
					read(self.property, *args)

	def test_empty_checkout_reference_rejected_before_inventory_lock(self):
		from kamra import direct_booking_cart
		with patch.object(direct_booking_cart, "lock_property_for_inventory", side_effect=AssertionError("missing key reached inventory")):
			for key in ("", "  "):
				with self.subTest(key=key), self.assertRaises(frappe.ValidationError):
					direct_booking_cart.book(self.property, self.lines, "Test", "555", idempotency_key=key)

	def test_search_has_same_limit_as_public_cart_quote(self):
		source = ast.parse((ROOT / "public_api.py").read_text())
		def limits(name):
			fn = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == name)
			return [ast.unparse(d) for d in fn.decorator_list if ast.unparse(d).startswith("rate_limit(")]
		self.assertTrue(limits("cart_quote"))
		self.assertEqual(limits("search_stay"), limits("cart_quote"))

	def test_search_exposes_one_currency_rounded_advance(self):
		for currency, price, expected in (("UGX", 125, 63), ("USD", 125.13, 62.57), ("KWD", 125.125, 62.563)):
			with self.subTest(currency=currency):
				frappe.db.set_value("Property", self.property, {"currency": currency, "deposit_pct": 50,
					"booking_payment_mode": "Advance percent", "advance_percent": 50})
				frappe.db.set_value("Room Type", self.standard, {"base_price": price, "tax_percent": 0})
				frappe.clear_cache()
				rows = public_api.search_stay(self.property, self.ci, frappe.utils.add_days(self.ci, 1))
				q = next(r["quote"] for r in rows if r["room_type"] == self.standard)
				self.assertEqual(q["advance_due"], expected)
				self.assertEqual(q["advance"]["amount"], q["advance_due"])

	def test_checkin_shell_uses_reservations_property_title(self):
		from kamra.www.kamra import _with_meta
		result = public_api.create_multi_room_booking(self.property, self.lines, "Fixture guest", "555",
			idempotency_key=frappe.generate_hash(length=32))
		token = result["reservations"][0]["precheckin_token"]
		frappe.db.set_value("Property", self.property, "meta_title", "Harbour & Hill")
		frappe.clear_document_cache("Property", self.property)
		with patch("kamra.www.kamra.product_name", return_value="Maximus Hotel"), patch("kamra.www.kamra.white_label", return_value=True):
			for mount in ("", "/hotel", "/kamra"):
				page = _with_meta("<head><title>Default</title></head>", f"{mount}/checkin/{token}")
				self.assertIn("<title>Harbour &amp; Hill</title>", page)
				self.assertIn("noindex", page)
				self.assertIn("no-referrer", page)
				self.assertNotIn(token, page)

	def test_failed_fixture_setup_registers_stack_cleanup(self):
		from kamra.tests.audit_fixtures import AuditTestCase
		case = AuditTestCase()
		with patch("kamra.tests.audit_fixtures.erp_bridge_scripts_installed", side_effect=RuntimeError("fixture")):
			original = frappe.local.db.commit
			try:
				with self.assertRaisesRegex(RuntimeError, "fixture"):
					case.setUp()
				case.doCleanups()
				self.assertEqual(frappe.local.db.commit, original)
			finally:
				case._stack.close()
