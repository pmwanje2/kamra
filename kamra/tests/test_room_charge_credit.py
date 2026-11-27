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

	def test_incoming_nights_consume_the_paying_stays_credit(self):
		source = make_reservation(PROP_A, self.rt, d(-1), d(2), room=make_room(PROP_A, self.rt, "102"),
		                          status="Checked In")
		route(["Room"], reservation=source.name, target_type="Room", target_reservation=self.res.name)
		self.block_with_allowance(3100)
		projected = pi.room_charge_credit(PROP_A, self.res.name)
		with self.subTest(operation="post"):
			with self.assertRaisesRegex(frappe.ValidationError, "HSP-RC-CREDIT"):
				self.post("INCOMING", 100)
		with self.subTest(operation="verify"):
			self.assertFalse(pi.verify_room_charge(property=PROP_A, room="101", amount=100)["posting_allowed"])
		self.assertEqual(pi.room_charge_credit(PROP_A, source.name)["remaining_nights_amount"], 0)
		for stay in (source, self.res):
			folio.post_remaining_nights(stay)
		rows = frappe.get_all("Folio Charge", filters={"parent": folio.open_folio(self.res), "charge_type": "Room"},
		                      pluck="amount")
		self.assertEqual(sum(rows), 6000)
		self.assertEqual(projected["remaining_nights_amount"], sum(rows))
		self.assertEqual(pi.room_charge_credit(PROP_A, self.res.name)["remaining_nights_amount"], 0)

	def test_incoming_room_limit_spent_once_over_remaining_nights(self):
		source = make_reservation(PROP_A, self.rt, d(-1), d(2), room=make_room(PROP_A, self.rt, "102"),
		                          status="Checked In")
		route(["Room"], reservation=source.name, target_type="Room", target_reservation=self.res.name,
		      limit_type="Amount", limit_value=1500)
		self.assertEqual(pi.room_charge_credit(PROP_A, self.res.name)["remaining_nights_amount"], 4500)
		folio.post_room_night(source, d(-1))
		self.assertEqual(pi.room_charge_credit(PROP_A, self.res.name)["remaining_nights_amount"], 3500)
		self.assertEqual(pi.room_charge_credit(PROP_A, source.name)["remaining_nights_amount"], 1500)

	def test_verify_includes_tip_on_the_same_payer_as_posting(self):
		self.block_with_allowance(3100)
		bill = dict(property=PROP_A, room="101", amount=100, description="Service", charge_type="Misc")
		self.assertTrue(pi.verify_room_charge(**bill)["posting_allowed"])
		self.assertEqual(pi.verify_room_charge(**bill, tip_amount=5)["reason_code"], "HSP-RC-CREDIT")
		with self.assertRaisesRegex(frappe.ValidationError, "HSP-RC-CREDIT"):
			self.post("TIP", 100, description="Service", charge_type="Misc", tip_amount=5)
		with self.assertRaisesRegex(frappe.ValidationError, "HSP-RC-TIP"):
			pi.verify_room_charge(**bill, tip_amount=-1)

	def test_non_revenue_room_code_is_excluded_from_daily_and_range_figures(self):
		from kamra import reports
		from kamra.room_statistics import posted_rooms

		frappe.get_doc({"doctype": "Transaction Code", "code": "UTNR", "description": "Room liability",
		                "txn_type": "Non-Revenue", "charge_type": "Room"}).insert(ignore_permissions=True)
		api.post_stay_charge(self.res.name, "Room", "Deposit", 5, transaction_code="UTNR")
		self.post("SALE", 100)
		self.assertEqual(reports.range_stats(PROP_A, TODAY, TODAY, 1)["total_revenue"], 100)
		self.assertEqual(reports._day_stats(PROP_A, TODAY, 1)["total_revenue"], 100)
		self.assertEqual(posted_rooms(PROP_A, TODAY, TODAY)["room_revenue"], 0)

	def test_shared_stay_prices_each_unposted_night_once_per_lookup(self):
		from kamra import shares
		from kamra.tests.audit_fixtures import make_guest

		primary = make_reservation(PROP_A, self.rt, TODAY, d(14), room=make_room(PROP_A, self.rt, "103"))
		other = shares.create_share_reservation(primary.name, make_guest("Sharer"))["reservation"]
		for res in (primary.reload(), frappe.get_doc("Reservation", other)):
			res.status = "Checked In"
			res.save()
		self.block_with_allowance(8000)
		with patch.object(shares, "_price_night", wraps=shares._price_night) as price:
			result = pi.verify_room_charge(property=PROP_A, reservation=primary.name, amount=1)
			self.assertEqual(result["room_charge_credit"]["remaining_nights_amount"], 7000)
			self.assertTrue(result["posting_allowed"])
			self.assertLessEqual(price.call_count, 14)

	def test_manual_room_charge_does_not_replace_automated_night(self):
		api.post_stay_charge(self.res.name, "Room", "Extra bed", 5)
		self.block_with_allowance(3010)
		credit = pi.room_charge_credit(PROP_A, self.res.name)
		with self.assertRaisesRegex(frappe.ValidationError, "HSP-RC-CREDIT"):
			self.post("MANUAL", 100)
		self.assertEqual(credit["remaining_nights_amount"], 3000)
		folio.post_room_night(self.res, TODAY)
		self.assertEqual(pi.room_charge_credit(PROP_A, self.res.name)["remaining_nights_amount"], 2000)

	def test_verify_reserves_company_nights_before_company_window_exists(self):
		from kamra.tests.test_pos_interface import _corp

		frappe.db.set_value("Reservation", self.res.name, {"company": _corp("UT Credit Corp"), "booking_type": "Corporate"})
		api.add_folio_payment(folio.open_folio(self.res), "Cash", 5000, kind="Security Deposit")
		self.block_with_allowance(3100)
		eligibility = pi.verify_room_charge(property=PROP_A, room="101", amount=100)
		self.assertIsNone(eligibility["folio"])
		self.assertEqual(eligibility["folio_type"], "Company")
		self.assertFalse(frappe.db.exists("Folio", {"reservation": self.res.name, "folio_type": "Company"}))
		with self.assertRaisesRegex(frappe.ValidationError, "HSP-RC-CREDIT"):
			self.post("COMPANY", 100)
		self.assertFalse(eligibility["posting_allowed"])
		self.assertEqual(eligibility["reason_code"], "HSP-RC-CREDIT")
		self.assertEqual(eligibility["room_charge_credit"]["remaining_nights_amount"], 3000)
		self.assertEqual(eligibility["room_charge_credit"]["held"], 0)
		self.assertTrue(pi.verify_room_charge(property=PROP_A, room="101", amount=90)["posting_allowed"])

	def test_verify_checks_no_post_on_split_destination(self):
		other = make_reservation(PROP_A, self.rt, d(-1), d(2), room=make_room(PROP_A, self.rt, "102"),
		                         status="Checked In")
		route(["Food & Beverage"], reservation=self.res.name, target_type="Room", target_reservation=other.name,
		      limit_type="Percent", limit_value=50)
		self.block_with_allowance(10000)
		frappe.db.set_value("Reservation", other.name, "room_charges_allowed", 0)
		eligibility = pi.verify_room_charge(property=PROP_A, room="101", amount=100)
		with self.assertRaises(pi.RoomChargesBlocked):
			self.post("SPLIT-BLOCKED", 100)
		self.assertFalse(eligibility["posting_allowed"])
		self.assertEqual(eligibility["no_post"], 1)
		self.assertTrue(eligibility["override_required"])
		self.assertEqual(eligibility["reason_code"], "POS-RC-NO-POST")
		frappe.db.set_value("Reservation", other.name, "room_charges_allowed", 1)
		self.assertTrue(pi.verify_room_charge(property=PROP_A, room="101", amount=100)["posting_allowed"])

	def test_unopened_group_window_reserves_other_members_routed_nights(self):
		group = frappe.get_doc({"doctype": "Group Booking", "property": PROP_A, "group_name": "UT Group Credit",
		                        "check_in_date": d(-1), "check_out_date": d(2), "status": "Confirmed"}).insert()
		frappe.db.set_value("Reservation", self.res.name, "group_booking", group.name)
		member = make_reservation(PROP_A, self.rt, d(-1), d(2), room=make_room(PROP_A, self.rt, "102"),
		                          status="Checked In", group_booking=group.name)
		route(["Room"], reservation=member.name, target_type="Group Master", target_window=2)
		route(["Food & Beverage"], reservation=self.res.name, target_type="Group Master", target_window=2)
		route(["Room"], reservation=self.res.name, target_type="Group Master", target_window=1)
		window = {"group_booking": group.name, "folio_type": "Group", "window_no": 2}
		self.block_with_allowance(3100)
		eligibility = pi.verify_room_charge(property=PROP_A, room="101", amount=100)
		self.assertFalse(frappe.db.exists("Folio", window))
		with self.assertRaisesRegex(frappe.ValidationError, "HSP-RC-CREDIT"):
			self.post("GROUP", 100)
		self.assertFalse(eligibility["posting_allowed"])
		self.assertEqual(eligibility["reason_code"], "HSP-RC-CREDIT")
		self.assertEqual(eligibility["room_charge_credit"]["remaining_nights_amount"], 3000)
		self.assertEqual(pi.verify_room_charge(property=PROP_A, room="101")["available"], 100)
		self.assertFalse(frappe.db.exists("Folio", window))
		self.assertTrue(pi.verify_room_charge(property=PROP_A, room="101", amount=90)["posting_allowed"])
		self.assertEqual(self.post("GROUP", 90)["folio"], frappe.db.get_value("Folio", window, "name"))

	def test_closed_company_window_projects_replacement_opening_balance(self):
		from kamra.tests.test_pos_interface import _corp

		frappe.db.set_value("Reservation", self.res.name, {"company": _corp("UT Credit Corp"), "booking_type": "Corporate"})
		closed = self.post("PREVIOUS", 100)["folio"]
		frappe.db.set_value("Folio", closed, "status", "Closed")
		self.block_with_allowance(3160)
		eligibility = pi.verify_room_charge(property=PROP_A, room="101", amount=100)
		self.assertFalse(frappe.db.exists("Folio", {"reservation": self.res.name, "folio_type": "Company", "status": "Open"}))
		replacement = self.post("REPLACEMENT", 100)["folio"]
		self.assertNotEqual(replacement, closed)
		self.assertEqual(frappe.db.get_value("Folio", replacement, "balance"), 105)
		self.assertEqual(frappe.db.get_value("Folio", closed, "balance"), 105)
		self.assertTrue(eligibility["posting_allowed"])
		self.assertEqual(eligibility["room_charge_credit"]["balance"], 0)
		self.assertEqual(eligibility["room_charge_credit"]["remaining_nights_amount"], 3000)
		self.assertEqual(eligibility["available"], 160)
		self.assertFalse(pi.verify_room_charge(property=PROP_A, room="101", amount=100)["posting_allowed"])

	def check_first_company_window_credit(self, expected_window):
		from kamra.tests.test_pos_interface import _corp

		frappe.db.set_value("Reservation", self.res.name,
		                    {"company": _corp("UT Credit Corp"), "booking_type": "Corporate"})
		route(["Room"], reservation=self.res.name, target_type="Window", target_window=expected_window)
		self.block_with_allowance(3100)
		window = {"reservation": self.res.name, "window_no": expected_window}
		over = pi.verify_room_charge(property=PROP_A, room="101", amount=100)
		within = pi.verify_room_charge(property=PROP_A, room="101", amount=95)
		self.assertFalse(frappe.db.exists("Folio", window))
		with self.assertRaisesRegex(frappe.ValidationError, "HSP-RC-CREDIT"):
			self.post("OVER", 100)
		self.assertFalse(frappe.db.exists("Folio", window))
		self.assertEqual(over["room_charge_credit"]["remaining_nights_amount"], 3000)
		self.assertEqual(over["available"], 100)
		self.assertFalse(over["posting_allowed"])
		self.assertEqual(over["reason_code"], "HSP-RC-CREDIT")
		self.assertTrue(within["posting_allowed"])
		bill = self.post("WITHIN", 95)["folio"]
		self.assertEqual(frappe.db.get_value("Folio", window, "name"), bill)
		self.assertEqual(frappe.db.get_value("Folio", bill, "balance"), 99.75)

	def test_first_company_bill_reserves_room_nights_routed_to_unopened_window_two(self):
		self.check_first_company_window_credit(2)

	def test_first_company_bill_uses_next_free_window_when_two_is_taken(self):
		folio.open_folio(self.res)
		folio.open_window(reservation=self.res.name, window_no=2, payee_type="Guest")
		self.check_first_company_window_credit(3)

	def check_full_guest_windows(self, policy):
		from kamra.tests.test_pos_interface import _corp

		_set_prop_setting(PROP_A, "folio_windows", 2)
		guest_bill = folio.open_folio(self.res)
		folio.open_window(reservation=self.res.name, window_no=2, payee_type="Guest")
		frappe.db.set_value("Reservation", self.res.name,
		                    {"company": _corp("UT Credit Corp"), "booking_type": "Corporate"})
		_set_prop_setting(PROP_A, "pos_room_charge_credit_check", policy)
		_set_prop_setting(PROP_A, "pos_room_charge_allowance", 3100)

		eligibility = pi.verify_room_charge(property=PROP_A, room="101", charge_type="Laundry", amount=1)
		self.assertEqual(eligibility["available"], 3100)
		self.assertEqual(eligibility["room_charge_credit"]["remaining_nights_amount"], 0)
		self.assertTrue(eligibility["posting_allowed"])
		out = self.post("LAUNDRY", 1, charge_type="Laundry")
		self.assertEqual(out["folio"], guest_bill)
		self.assertFalse(out.get("credit_warning"))

		# The same cap still applies to a ticket that actually needs a company window.
		for action in (lambda: pi.verify_room_charge(property=PROP_A, room="101", amount=1),
		               lambda: self.post("COMPANY", 1)):
			with self.assertRaisesRegex(frappe.ValidationError, "at most 2 windows"):
				action()
		self.assertEqual(frappe.db.count("Folio", {"reservation": self.res.name}), 2)

	def test_full_guest_windows_allow_guest_paid_laundry_under_block(self):
		self.check_full_guest_windows("Block")

	def test_full_guest_windows_allow_guest_paid_laundry_under_warn(self):
		self.check_full_guest_windows("Warn")

	def test_verify_and_post_apply_the_same_country_pack_tax_validation(self):
		self.block_with_allowance(5000)
		bill = dict(charge_type="Laundry", description="Laundry", amount=100)
		with self.assertRaisesRegex(frappe.ValidationError, "Tax rate 77% is not allowed") as posting:
			self.post("TAX", **bill, tax_rate=77)
		with self.assertRaises(frappe.ValidationError) as eligibility:
			pi.verify_room_charge(property=PROP_A, room="101", **bill, tax_rate=77)
		self.assertEqual(str(eligibility.exception), str(posting.exception))
		self.assertTrue(pi.verify_room_charge(property=PROP_A, room="101", **bill, tax_rate=18)["posting_allowed"])
		self.assertTrue(self.post("TAX", **bill, tax_rate=18)["ok"])

	def test_full_room_route_quotes_the_posted_folio_without_sql_writes(self):
		other = make_reservation(PROP_A, self.rt, d(-1), d(2), room=make_room(PROP_A, self.rt, "102"),
		                         status="Checked In")
		source = folio.open_folio(self.res)
		target = folio.open_window(reservation=other.name, window_no=2, payee_type="Guest")
		api.add_folio_charge(target, "Misc", "Prior charge", 37)
		route(["Laundry"], reservation=self.res.name, target_type="Room", target_reservation=other.name,
		      target_window=2, limit_type="Percent", limit_value=100)
		self.block_with_allowance(5000)
		ticket = dict(amount=1, charge_type="Laundry", description="Laundry")
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as queries:
			quote = pi.verify_room_charge(property=PROP_A, room="101", ticket_ref="ROUTED", **ticket)
		self.assertTrue(queries.call_count)
		for call in queries.call_args_list:
			query = call.args[0] if call.args else call.kwargs["query"]
			self.assertIn(str(query).lstrip().split(None, 1)[0].upper(), {"SELECT", "SHOW", "DESC", "DESCRIBE"})

		self.assertTrue(quote["posting_allowed"])
		posted = self.post("ROUTED", **ticket)
		self.assertNotEqual(source, target)
		self.assertEqual((posted["folio"], posted["folio_type"]), (target, "Extra"))
		self.assertEqual((quote["folio"], quote["folio_type"], quote["balance"]), (target, "Extra", 37))

	def test_quote_and_post_refuse_fx_amount_rounded_to_zero(self):
		self.block_with_allowance(5000)
		frappe.db.set_value("Property", PROP_A, "currency", "INR")
		frappe.get_doc({"doctype": "Exchange Rate", "property": PROP_A, "currency": "JPY",
		                "rate_date": TODAY, "buy_rate": 0.001, "sell_rate": 0.001}).insert()
		ticket = dict(amount=1, currency="JPY", charge_type="Laundry", description="Laundry")
		with self.assertRaisesRegex(frappe.ValidationError, "Charge amount must be more than zero") as posted:
			self.post("FX-ZERO", **ticket)
		with self.assertRaises(frappe.ValidationError) as quoted:
			pi.verify_room_charge(property=PROP_A, room="101", ticket_ref="FX-ZERO", **ticket)
		self.assertEqual(str(quoted.exception), str(posted.exception))
		ticket["amount"] = 10
		quote = pi.verify_room_charge(property=PROP_A, room="101", ticket_ref="FX-CENT", **ticket)
		self.assertTrue(quote["posting_allowed"])
		self.assertEqual(quote["charge_amount"], 0.01)
		self.assertEqual(self.post("FX-CENT", **ticket)["amount"], 0.01)

	def test_quote_missing_tip_code_is_read_only(self):
		frappe.db.delete("Transaction Code", {"name": "9300"})
		self.as_pos()
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as queries:
			try:
				quote = pi.verify_room_charge(property=PROP_A, room="101", amount=1, charge_type="Outlet Tip")
			except frappe.ValidationError as error:
				self.assertIn("9300", str(error))
				allowed = False
			else:
				allowed = quote["posting_allowed"]
		self.assertTrue(queries.call_count)
		for call in queries.call_args_list:
			query = call.args[0] if call.args else call.kwargs["query"]
			self.assertIn(str(query).lstrip().split(None, 1)[0].upper(), {"SELECT", "SHOW", "DESC", "DESCRIBE"})
		self.assertFalse(frappe.db.exists("Transaction Code", "9300"))
		self.assertFalse(allowed)

	def test_split_quote_shows_first_posted_part(self):
		target = folio.open_window(reservation=self.res.name, window_no=2, payee_type="Guest")
		api.add_folio_charge(target, "Misc", "Prior charge", 37)
		route(["Laundry"], reservation=self.res.name, target_type="Window", target_window=2,
		      limit_type="Percent", limit_value=50)
		self.block_with_allowance(5000)
		ticket = dict(amount=10, charge_type="Laundry", description="Laundry")
		quote = pi.verify_room_charge(property=PROP_A, room="101", ticket_ref="SPLIT-PAYER", **ticket)
		posted = self.post("SPLIT-PAYER", **ticket)
		self.assertEqual((posted["folio"], posted["folio_type"], posted["balance"]), (target, "Extra", 42))
		self.assertEqual((quote["folio"], quote["folio_type"], quote["balance"]), (target, "Extra", 37))

	def test_quote_rejects_unopened_route_above_lowered_cap(self):
		_set_prop_setting(PROP_A, "folio_windows", 3)
		route(["Laundry"], reservation=self.res.name, target_type="Window", target_window=3)
		_set_prop_setting(PROP_A, "folio_windows", 2)
		self.block_with_allowance(5000)
		ticket = dict(amount=1, charge_type="Laundry", description="Laundry")
		with self.assertRaisesRegex(frappe.ValidationError, "Window must be from 1 to 2") as posted:
			self.post("WINDOW-CAP", **ticket)
		with self.assertRaises(frappe.ValidationError) as quoted:
			pi.verify_room_charge(property=PROP_A, room="101", **ticket)
		self.assertEqual(str(quoted.exception), str(posted.exception))
		self.assertFalse(frappe.db.exists("Folio", {"reservation": self.res.name, "window_no": 3}))

	def test_quote_rejects_home_tip_below_minor_unit(self):
		from kamra.ledger import ensure_default_transaction_codes
		ensure_default_transaction_codes()
		frappe.db.set_value("Property", PROP_A, "currency", "INR")
		self.block_with_allowance(5000)
		ticket = dict(amount=0.001, currency="INR", charge_type="Outlet Tip", description="Tip")
		with self.assertRaisesRegex(frappe.ValidationError, "Charge amount must be more than zero") as posted:
			self.post("SUB-CENT", **ticket)
		with self.assertRaises(frappe.ValidationError) as quoted:
			pi.verify_room_charge(property=PROP_A, room="101", **ticket)
		self.assertEqual(str(quoted.exception), str(posted.exception))
		ticket["amount"] = 0.005
		quote = pi.verify_room_charge(property=PROP_A, room="101", **ticket)
		self.assertTrue(quote["posting_allowed"])
		self.assertEqual(quote["charge_amount"], 0.01)

	def test_quote_enforces_posting_charge_types(self):
		ticket = dict(amount=1, charge_type="Room", description="Room")
		with self.assertRaisesRegex(frappe.ValidationError, "charge_type must be one of") as posted:
			self.post("ROOM-TYPE", **ticket)
		with self.assertRaises(frappe.ValidationError) as quoted:
			pi.verify_room_charge(property=PROP_A, room="101", **ticket)
		self.assertEqual(str(quoted.exception), str(posted.exception))

	def test_closed_guest_bill_has_zero_opening_balance_on_replacement_quote(self):
		ticket = dict(charge_type="Laundry", description="Laundry")
		closed = self.post("PRIOR", 100, **ticket)["folio"]
		frappe.db.set_value("Folio", closed, "status", "Closed")
		self.block_with_allowance(3110)

		quote = pi.verify_room_charge(property=PROP_A, room="101", amount=10, **ticket)
		self.assertTrue(quote["posting_allowed"])
		self.assertEqual(quote["room_charge_credit"]["balance"], 100)
		self.assertEqual(quote["available"], 10)
		self.assertFalse(frappe.db.exists("Folio", {"reservation": self.res.name, "status": "Open"}))
		posted = self.post("REPLACEMENT", 10, **ticket)
		self.assertNotEqual(posted["folio"], closed)
		self.assertEqual(posted["balance"], 10)
		self.assertEqual(frappe.db.get_value("Folio", closed, "balance"), 100)
		self.assertEqual((quote["folio"], quote["folio_type"], quote["balance"]), (None, "Guest", 0))
		followup = pi.verify_room_charge(property=PROP_A, room="101", amount=1, **ticket)
		self.assertEqual((followup["folio"], followup["balance"]), (posted["folio"], 10))
		self.assertFalse(followup["posting_allowed"])

	def test_projected_room_revenue_ignores_non_revenue_room_lines(self):
		from kamra.reports import _day_stats

		def projected():
			return _day_stats(PROP_A, TODAY, 1)["projected_room_revenue"]

		self.assertEqual(projected(), 1000)
		frappe.get_doc({"doctype": "Transaction Code", "code": "UTNR", "description": "Room liability",
		                "txn_type": "Non-Revenue", "charge_type": "Room"}).insert(ignore_permissions=True)
		api.post_stay_charge(self.res.name, "Room", "Liability", 5, transaction_code="UTNR")
		self.assertEqual(projected(), 1000)
		api.post_stay_charge(self.res.name, "Room", "Agreed room charge", 800)
		self.assertEqual(projected(), 800)

	def test_legacy_allowance_counts_closed_guest_debt_on_replacement(self):
		ticket = dict(charge_type="Laundry", description="Laundry")
		closed = self.post("FIRST", 100, **ticket)["folio"]
		frappe.db.set_value("Folio", closed, "status", "Closed")
		_set_prop_setting(PROP_A, "pos_room_charge_credit_check", 1)
		_set_prop_setting(PROP_A, "pos_room_charge_allowance", 100)

		quote = pi.verify_room_charge(property=PROP_A, room="101", amount=10, **ticket)
		self.assertEqual((quote["folio"], quote["folio_type"], quote["balance"]), (None, "Guest", 0))
		with self.subTest("eligibility"):
			self.assertEqual((quote["available"], quote["posting_allowed"], quote["override_required"],
			                  quote["reason_code"]), (0, False, True, "POS-RC-OVER-LIMIT"))
		with self.assertRaisesRegex(frappe.ValidationError, "POS-RC-OVER-LIMIT"):
			self.post("REPLACEMENT", 10, **ticket)
		self.assertFalse(frappe.db.exists("Folio", {"reservation": self.res.name, "status": "Open"}))

	def test_company_paid_guest_replacement_reserves_its_scheduled_nights(self):
		from kamra.tests.test_pos_interface import _corp

		closed = folio.open_folio(self.res)
		frappe.db.set_value("Folio", closed, {"payee_type": "Company", "payee_company": _corp("UT Credit Corp"),
		                                     "status": "Closed"})
		self.block_with_allowance(100)
		ticket = dict(charge_type="Laundry", description="Laundry")
		quote = pi.verify_room_charge(property=PROP_A, room="101", amount=10, **ticket)
		self.assertEqual((quote["folio"], quote["folio_type"], quote["balance"]), (None, "Guest", 0))
		self.assertFalse(frappe.db.exists("Folio", {"reservation": self.res.name, "status": "Open"}))
		with self.assertRaisesRegex(frappe.ValidationError, "HSP-RC-CREDIT"):
			self.post("COMPANY-REPLACEMENT", 10, **ticket)
		self.assertEqual((quote["room_charge_credit"]["remaining_nights_amount"], quote["available"],
		                  quote["posting_allowed"], quote["reason_code"]), (3000, -2900, False, "HSP-RC-CREDIT"))
