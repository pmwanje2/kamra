from decimal import Decimal
from unittest import TestCase


class CreditArithmetic(TestCase):
	def test_available_credit_uses_greater_security_and_subtracts_unposted_nights(self):
		from kamra.room_charge_money import available_credit
		for currency, unit in (("UGX", "1"), ("USD", ".01"), ("KWD", ".001")):
			q = Decimal(unit)
			for held, allowance in ((9, 6), (6, 9)):
				self.assertEqual(available_credit(held*q, allowance*q, 8*q, 3*q, currency), -2*q)


# Use the same two-property POS fixture as the existing room-charge endpoints.
from kamra.tests.test_pos_interface import POSInterfaceTestCase as _POSFixture
from kamra.tests.audit_fixtures import PROP_A, PROP_B
import frappe


class CreditPosting(_POSFixture):
	def test_credit_reader_enforces_role_and_property(self):
		from kamra.pos_interface import room_charge_credit
		self.as_pos()
		with self.assertRaises(frappe.PermissionError):
			room_charge_credit(PROP_B, self.res.name)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			room_charge_credit(PROP_A, self.res.name)

	def test_block_policy_refuses_over_credit(self):
		from kamra.tests.test_pos_override_pin_and_room_charge import _set_prop_setting
		_set_prop_setting(PROP_A, "pos_room_charge_credit_check", "Block")
		self.as_pos()
		with self.assertRaisesRegex(frappe.ValidationError, "HSP-"):
			self.post(amount=100)

	def test_tip_posts_once_as_untaxed_non_revenue(self):
		self.as_pos()
		out = self.post(amount=120, tip_amount=15)
		self.assertTrue(self.post(amount=120, tip_amount=15)["duplicate"])
		with self.assertRaises(frappe.ValidationError):
			self.post(amount=120, tip_amount=14)
		rows = frappe.get_all("Folio Charge", filters={"parent": out["folio"], "charge_type": "Outlet Tip"},
		                      fields=["amount", "gst_amount", "transaction_code"])
		self.assertEqual([(r.amount, r.gst_amount, r.transaction_code) for r in rows], [(15, 0, "9300")])
		self.assertEqual(frappe.db.get_value("Transaction Code", "9300", "txn_type"), "Non-Revenue")
