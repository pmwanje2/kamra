from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

import frappe

from kamra import api, audit_report, books, folio
from kamra import pos_interface as pi
from kamra.tests.audit_fixtures import PROP_A, PROP_B, TODAY, d, make_reservation, make_room
from kamra.tests.test_pos_interface import POSInterfaceTestCase as _POSFixture
from kamra.tests.test_pos_override_pin_and_room_charge import _set_prop_setting
from kamra.tests.test_routing_instructions import route


class CreditArithmetic(TestCase):
	def test_available_credit_uses_greater_security_and_subtracts_unposted_nights(self):
		from kamra.room_charge_money import available_credit
		for currency, unit in (("UGX", "1"), ("USD", ".01"), ("KWD", ".001")):
			q = Decimal(unit)
			for held, allowance in ((9, 6), (6, 9)):
				self.assertEqual(available_credit(held*q, allowance*q, 8*q, 3*q, currency), -2*q)


class CreditPosting(_POSFixture):
	def test_credit_reader_enforces_role_and_property(self):
		from kamra.pos_interface import room_charge_credit
		self.as_pos()
		with self.assertRaises(frappe.PermissionError):
			room_charge_credit(PROP_B, self.res.name)
		frappe.set_user("Guest")  # nosemgrep: frappe-setuser -- exercise the unauthenticated reader guard
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

	def test_reports_exclude_tip_and_custom_non_revenue_code(self):
		self.post(amount=100, tip_amount=15)
		frappe.get_doc({"doctype": "Transaction Code", "code": "UTNR", "description": "Liability",
		                "txn_type": "Non-Revenue", "charge_type": "Misc"}).insert(ignore_permissions=True)
		self.post("N", 5, charge_type="Misc", transaction_code="UTNR")
		self.assertEqual(audit_report._folio(PROP_A, TODAY, TODAY)["other"], 0)
		self.assertEqual(books._revenue(PROP_A, TODAY, TODAY)["other"], 0)
		self.assertEqual(books._revenue(PROP_A, TODAY, TODAY)["fnb"], 100)

	def test_same_day_and_carried_forward_credit(self):
		_set_prop_setting(PROP_A, "pos_room_charge_allowance", 5000)
		frappe.db.set_value("Reservation", self.res.name,
		                    {"check_out_date": self.res.check_in_date, "is_day_use": 1})
		with patch("kamra.folio._day_use_room_charge", return_value=(1000, 0, "Day use")):
			self.assertEqual(pi.room_charge_credit(PROP_A, self.res.name)["available_credit"], 4000)
		with patch("kamra.folio.carried_forward_until", return_value=frappe.utils.getdate(TODAY)):
			self.assertEqual(pi.room_charge_credit(PROP_A, self.res.name)["available_credit"], 5000)

	def test_carry_forward_skips_billed_dates(self):
		with patch("kamra.folio.carried_forward_until", return_value=frappe.utils.getdate(d(1))):
			self.assertEqual(pi.room_charge_credit(PROP_A, self.res.name)["remaining_nights_amount"], 1000)

	def test_cash_on_another_guest_window_is_available(self):
		folio.open_folio(self.res)
		second = folio.open_window(reservation=self.res.name, payee_type="Guest")
		api.add_folio_payment(second, "Cash", 5000, kind="Security Deposit")
		self.assertEqual(pi.room_charge_credit(PROP_A, self.res.name)["held"], 5000)

	def test_routed_room_cannot_use_source_preauthorization(self):
		other = make_reservation(PROP_A, self.rt, d(-1), d(2), room=make_room(PROP_A, self.rt, "102"),
		                         status="Checked In")
		route(["Food & Beverage"], reservation=self.res.name, target_type="Room", target_reservation=other.name)
		_set_prop_setting(PROP_A, "pos_room_charge_credit_check", "Block")
		frappe.get_doc({"doctype": "Security Deposit", "property": PROP_A, "reservation": self.res.name,
		                "guest": self.res.guest, "status": "Authorized", "required_amount": 10000,
		                "authorized_amount": 10000, "preauth_hold": "UT-HOLD"}).insert(ignore_permissions=True)
		with self.assertRaisesRegex(frappe.ValidationError, "HSP-RC-CREDIT"):
			self.post(amount=1)

	def test_block_includes_tax_rounding_carried_from_previous_line(self):
		self.post("FIRST", 0.09)
		before = pi.room_charge_credit(PROP_A, self.res.name)
		_set_prop_setting(PROP_A, "pos_room_charge_allowance", before["remaining_nights_amount"] + 0.18)
		_set_prop_setting(PROP_A, "pos_room_charge_credit_check", "Block")
		with self.assertRaisesRegex(frappe.ValidationError, "HSP-RC-CREDIT"):
			self.post("SECOND", 0.09)

	def test_converted_tip_above_storage_limit_is_rejected_before_ticket(self):
		with patch.object(pi, "_convert_to_home", side_effect=[(1, "USD", None), (10**12, "USD", None)]):
			with self.assertRaisesRegex(frappe.ValidationError, "HSP-RC-TIP"):
				self.post("FX", 1, tip_amount=1)
		self.assertFalse(frappe.db.exists(pi.CHARGE, pi._charge_name(PROP_A, "FX")))
