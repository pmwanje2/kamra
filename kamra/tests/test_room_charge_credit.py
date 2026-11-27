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

	def block_with_allowance(self, allowance):
		_set_prop_setting(PROP_A, "pos_room_charge_credit_check", "Block")
		_set_prop_setting(PROP_A, "pos_room_charge_allowance", allowance)

	def test_split_requires_credit_from_each_room(self):
		other = make_reservation(PROP_A, self.rt, d(-1), d(2), room=make_room(PROP_A, self.rt, "102"),
		                         status="Checked In")
		route(["Food & Beverage"], reservation=self.res.name, target_type="Room", target_reservation=other.name,
		      limit_type="Percent", limit_value=50)
		api.add_folio_payment(folio.open_folio(self.res), "Cash", 5000, kind="Security Deposit")
		self.block_with_allowance(0)
		with self.assertRaisesRegex(frappe.ValidationError, "HSP-RC-CREDIT"):
			self.post("SPLIT", 100)
		self.assertFalse(frappe.db.exists(pi.CHARGE, pi._charge_name(PROP_A, "SPLIT")))
		self.assertFalse(pi.verify_room_charge(property=PROP_A, room="101", amount=100)["posting_allowed"])
		# With exactly enough at the destination, the same ticket succeeds.
		api.add_folio_payment(folio.open_folio(other), "Cash", 3052.50, kind="Security Deposit")
		self.post("SPLIT", 100)
		self.assertEqual(pi.room_charge_credit(PROP_A, other.name)["available_credit"], 0)

	def test_two_windows_cannot_spend_the_same_security(self):
		api.add_folio_payment(folio.open_folio(self.res), "Cash", 5000, kind="Security Deposit")
		self.block_with_allowance(0)
		self.post("FIRST", 2000, charge_type="Laundry", description="Laundry")
		folio.open_window(reservation=self.res.name, payee_type="Guest")
		route(["Laundry"], reservation=self.res.name, target_type="Window", target_window=2)
		with self.assertRaisesRegex(frappe.ValidationError, "HSP-RC-CREDIT"):
			self.post("SECOND", 2000, charge_type="Laundry", description="Laundry")

	def test_replacement_window_cannot_ignore_a_closed_window(self):
		self.post("OLD", 100)
		frappe.db.set_value("Folio", folio.open_folio(self.res), "status", "Closed")
		self.block_with_allowance(3105)
		with self.assertRaisesRegex(frappe.ValidationError, "HSP-RC-CREDIT"):
			self.post("NEW", 100)
		self.assertEqual(pi.room_charge_credit(PROP_A, self.res.name)["available_credit"], 0)

	def test_credit_uses_tax_on_the_final_ticket_text(self):
		self.block_with_allowance(3100)
		with self.assertRaisesRegex(frappe.ValidationError, "HSP-RC-CREDIT"):
			self.post("DINNER-1", 100, charge_type="Misc", description="Service")

	def test_current_date_fx_credit_matches_posted_room_amount(self):
		plan = frappe.get_doc({"doctype": "Rate Plan", "property": PROP_A, "code": "UT-FX",
		                       "rate_plan_name": "Foreign rate", "modifier_type": "Percent",
		                       "modifier_value": 0}).insert(ignore_permissions=True)
		frappe.db.set_value("Property", PROP_A, {"currency": "UGX", "rates_include_tax": 0})
		frappe.clear_document_cache("Property", PROP_A)
		frappe.db.set_value("Reservation", self.res.name, {
			"rate_plan": plan.name, "fixed_rate": 0, "check_out_date": TODAY,
			"contract_tariff": frappe.as_json({"fx_lock_policy": "Current Date", "tariff": {
				"currency": "USD", "property_currency": "UGX", "nightly": 100, "fx_rate": 4085.83}})})
		with patch("kamra.pricing.posting_fx_rate", return_value=(Decimal("4200"), TODAY)):
			projected = pi.room_charge_credit(PROP_A, self.res.name)["remaining_nights_amount"]
			folio.post_room_night(self.res.reload(), d(-1))
		posted = frappe.db.get_value("Folio Charge", {"reservation": self.res.name, "charge_type": "Room"}, "amount")
		self.assertEqual(posted, 420000)
		self.assertEqual(projected, posted)

	def check_shared_credit(self, mode, expected, allowed):
		from kamra.shares import create_share_reservation, update_share_rate_mode
		from kamra.tests.audit_fixtures import charges, make_guest, make_room_type

		rt = make_room_type(PROP_A, "SHARE", base_price=101)
		primary = make_reservation(PROP_A, rt, TODAY, d(4), room=make_room(PROP_A, rt, "103"))
		other = create_share_reservation(primary.name, make_guest("Sharer"))["reservation"]
		if mode == "Custom":
			update_share_rate_mode(primary.name, mode, {primary.name: 160, other: 70})
		for res in (primary.reload(), frappe.get_doc("Reservation", other)):
			res.status = "Checked In"
			res.save()
		self.block_with_allowance(410)
		credit = pi.room_charge_credit(PROP_A, primary.name)
		eligibility = pi.verify_room_charge(property=PROP_A, reservation=primary.name, amount=5)
		for offset in range(4):
			folio.post_room_night(primary, d(offset))
		posted = sum(row.amount for row in charges(primary.name, "Room"))
		self.assertEqual(posted, expected)
		self.assertEqual(credit["remaining_nights_amount"], posted)
		self.assertEqual(eligibility["posting_allowed"], allowed)

	def test_credit_reserves_custom_payer_nights(self):
		self.check_shared_credit("Custom", 640, False)

	def test_credit_reserves_equal_payer_nights(self):
		self.check_shared_credit("Split Equal", 202, True)

	def test_eligibility_and_post_classify_the_same_ticket(self):
		self.block_with_allowance(3100)
		for ticket, allowed in (("DINNER-1", False), ("DESK-1", True)):
			with self.subTest(ticket=ticket):
				result = pi.verify_room_charge(property=PROP_A, room="101", amount=100, charge_type="Misc",
				                               description=" Service ", ticket_ref=ticket)
				self.assertEqual(result["posting_allowed"], allowed)
				if allowed:
					self.post(ticket, 100, description=" Service ", charge_type="Misc")
				else:
					with self.assertRaisesRegex(frappe.ValidationError, "HSP-RC-CREDIT"):
						self.post(ticket, 100, description=" Service ", charge_type="Misc")

	def test_manager_reports_exclude_non_revenue_transaction_code(self):
		from kamra import reports

		frappe.get_doc({"doctype": "Transaction Code", "code": "UTNR", "description": "Liability",
		                "txn_type": "Non-Revenue", "charge_type": "Misc"}).insert(ignore_permissions=True)
		self.post("SALE", 100, tip_amount=15)
		self.post("LIABILITY", 5, charge_type="Misc", description="Deposit", transaction_code="UTNR")
		with self.subTest(report="range"):
			self.assertEqual(reports.range_stats(PROP_A, TODAY, TODAY, 1)["total_revenue"], 100)
		with self.subTest(report="daily"):
			day = reports._day_stats(PROP_A, TODAY, 1)
			self.assertEqual((day["other_revenue"], day["total_revenue"]), (0, 100))

	def test_french_credit_warning_and_refusal(self):
		self.block_with_allowance(0)
		source = "HSP-RC-CREDIT: This charge exceeds the available room-charge credit."
		with patch.object(frappe.local, "lang", "fr"):
			expected = frappe._(source, lang="fr")
			result = pi.verify_room_charge(property=PROP_A, room="101", amount=1)
			with self.assertRaises(frappe.ValidationError) as refused:
				self.post("FR", 1)
			self.assertIn("crédit", expected)
			self.assertEqual(result["reason"], expected)
			self.assertEqual(result["room_charge_credit"]["credit_warning"], expected)
			self.assertEqual(str(refused.exception), expected)
