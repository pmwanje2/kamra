"""RED→GREEN R2 regression tests for pmwanje2/kamra#54 (all 5 backend findings)."""

from __future__ import annotations

from decimal import Decimal

import frappe
from frappe.utils import add_days, getdate

from kamra import api, fixed_charges, folio, install, ledger, night_audit, packages
from kamra.tests.audit_fixtures import (
	TODAY,
	AuditTestCase,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

PROP_KWD = "UT R2 Pkg KWD"
PROP_USD_TAX = "UT R2 Pkg USD Tax"
PROP_USD_SEP = "UT R2 Pkg USD Sep"
PROP_FC = "UT R2 Fixed Charges"


def _ensure_currency(code: str, symbol: str, fraction_units: int = 100):
	if not frappe.db.exists("Currency", code):
		frappe.get_doc({
			"doctype": "Currency",
			"currency_name": code,
			"symbol": symbol,
			"fraction": "Unit",
			"fraction_units": fraction_units,
			"enabled": 1,
		}).insert(ignore_permissions=True)
	else:
		frappe.db.set_value("Currency", code, "enabled", 1, update_modified=False)


class TestPackagesFixedR2(AuditTestCase):
	properties = (PROP_KWD, PROP_USD_TAX, PROP_USD_SEP, PROP_FC)

	def setUp(self):
		super().setUp()
		self.at(TODAY)
		_ensure_currency("KWD", "KD", 1000)
		_ensure_currency("USD", "$", 100)
		ledger.ensure_default_transaction_codes()

		self.day0 = str(getdate(TODAY))
		self.day1 = str(add_days(self.day0, 1))
		self.day2 = str(add_days(self.day0, 2))
		self.day3 = str(add_days(self.day0, 3))

	def _setup_property(
		self,
		prop_name: str,
		currency: str,
		country: str,
		*,
		gst_rate: float = 0,
		rates_include_tax: int = 0,
	):
		make_property(
			prop_name,
			country=country,
			currency=currency,
			business_date=self.day0,
			gst_mode="Fixed",
			gst_rate_low=gst_rate,
			gst_rate_high=gst_rate,
			rates_include_tax=rates_include_tax,
		)
		rt = make_room_type(prop_name, "DLX", base_price=200, adults_capacity=4)
		room = make_room(prop_name, rt, "101")
		return rt, room

	def _make_kwd_inclusive_stay(self):
		rt, room = self._setup_property(PROP_KWD, "KWD", "Kuwait", gst_rate=0)
		frappe.get_doc({
			"doctype": "Holiday Package",
			"property": PROP_KWD,
			"package_name": "Royal Wellness 2N KWD",
			"room_type": rt,
			"start_date": self.day0,
			"end_date": self.day1,
			"package_nights": 2,
			"extra_night_percent": 100,
			"price_2_pax": 600.000,
			"folio_display_mode": "Inclusive",
			"elements": [
				{
					"element_name": "Breakfast",
					"element_type": "Meal",
					"transaction_code": "1100",
					"charge_type": "Meal Plan",
					"allowance_basis": "Per Adult Night",
					"unit_allowance": 20.000,
					"posting_rhythm": "Every Night",
					"included_in_rate": 1,
					"folio_display_mode": "Inclusive",
				},
				{
					"element_name": "Spa Credit",
					"element_type": "Spa",
					"transaction_code": "3100",
					"charge_type": "Spa",
					"allowance_basis": "Per Room Night",
					"unit_allowance": 20.000,
					"posting_rhythm": "Every Night",
					"included_in_rate": 1,
					"folio_display_mode": "Inclusive",
				},
			],
		}).insert(ignore_permissions=True)
		res = make_reservation(
			PROP_KWD, rt, self.day0, self.day2,
			status="Checked In", room=room, adults=2, children=0,
		)
		return rt, room, res

	def test_r2_f1_on_arrival_window1_before_checkin_no_recursion(self):
		"""Finding 1 [P0]: Checking in a stay with an active Window-1 On Arrival fixed charge
		must not trigger infinite recursion and must post the charge exactly once."""
		rt, room = self._setup_property(PROP_FC, "USD", "United States", gst_rate=0)
		res = make_reservation(
			PROP_FC, rt, self.day0, self.day2,
			status="Confirmed", room=room, adults=2,
		)
		arr = fixed_charges.save_fixed_charge(res.name, {
			"transaction_code": "3200",
			"charge_type": "Taxi",
			"description": "Window 1 Arrival Limo",
			"qty": 1,
			"unit_price": 45.00,
			"frequency": "On Arrival",
			"start_date": self.day0,
			"end_date": self.day2,
			"window_no": 1,
		})["row"]

		res.reload()
		res.status = "Checked In"
		res.save(ignore_permissions=True)

		guest_folio = frappe.db.get_value(
			"Folio", {"reservation": res.name, "folio_type": "Guest"}, "name",
		)
		self.assertIsNotNone(guest_folio)
		fdoc = frappe.get_doc("Folio", guest_folio)
		idem_key = f"fc:{res.name}:{arr['name']}:arrival"
		matched = [c for c in fdoc.charges if c.idempotency_key == idem_key]
		self.assertEqual(len(matched), 1)
		self.assertEqual(Decimal(str(matched[0].amount)), Decimal("45.00"))
		self.assertEqual(frappe.db.get_value("Reservation Fixed Charge", arr["name"], "post_count"), 1)

		# Re-entering open_folio and post_arrival_fixed_charges remains strictly idempotent
		folio.open_folio(res)
		again = fixed_charges.post_arrival_fixed_charges(res)
		self.assertEqual(again["count"], 0)
		fdoc.reload()
		self.assertEqual(len([c for c in fdoc.charges if c.idempotency_key == idem_key]), 1)

	def test_r2_f2_inclusive_package_split_zero_rate_variance_and_amend_stay_preserves_locked_rate(self):
		"""Finding 2 [P0]: Inclusive package split (240 Room + 40 Breakfast + 20 Spa = 300 KWD)
		must report 0.0 rate variance and preserve night_rates[0].rate == 300.0 on amend_stay,
		while separate add-ons (included_in_rate=0) must not inflate room rate."""
		_rt, _room, res = self._make_kwd_inclusive_stay()
		self.assertEqual(Decimal(str(res.night_rates[0].rate)), Decimal("300.000"))

		folio.run_night_audit(PROP_KWD, self.day0)

		rv = night_audit.check_rate_variance(PROP_KWD, self.day0)
		rv_matches = [item for item in rv["items"] if item["reservation"] == res.name]
		self.assertEqual(rv_matches, [])

		charged = folio.charged_nights(res.name)
		self.assertIn(self.day0, charged)
		self.assertEqual(Decimal(str(charged[self.day0]["base"])), Decimal("300.000"))

		api.amend_stay(res.name, self.day0, self.day3, confirm_price=1)
		res.reload()
		self.assertEqual(Decimal(str(res.night_rates[0].rate)), Decimal("300.000"))
		self.assertEqual(Decimal(str(res.amount_before_tax)), Decimal("900.000"))

		# Separate add-on (included_in_rate=0) must NOT be counted into room rate or rate variance
		rt_sep, room_sep = self._setup_property(PROP_USD_SEP, "USD", "United States", gst_rate=0)
		frappe.get_doc({
			"doctype": "Holiday Package",
			"property": PROP_USD_SEP,
			"package_name": "City Break Separate 2N",
			"room_type": rt_sep,
			"start_date": self.day0,
			"end_date": self.day1,
			"package_nights": 2,
			"price_2_pax": 250.00,
			"folio_display_mode": "Separate Add-on",
			"elements": [
				{
					"element_name": "Airport Transfer",
					"element_type": "Transport",
					"transaction_code": "3200",
					"charge_type": "Taxi",
					"allowance_basis": "Fixed Per Stay",
					"unit_allowance": 45.50,
					"posting_rhythm": "First Night",
					"included_in_rate": 0,
					"folio_display_mode": "Separate Add-on",
				},
			],
		}).insert(ignore_permissions=True)
		res_sep = make_reservation(
			PROP_USD_SEP, rt_sep, self.day0, self.day2,
			status="Checked In", room=room_sep, adults=2,
		)
		folio.run_night_audit(PROP_USD_SEP, self.day0)
		rv_sep = night_audit.check_rate_variance(PROP_USD_SEP, self.day0)
		self.assertEqual([item for item in rv_sep["items"] if item["reservation"] == res_sep.name], [])
		charged_sep = folio.charged_nights(res_sep.name)
		self.assertEqual(Decimal(str(charged_sep[self.day0]["base"])), Decimal("125.00"))

	def test_r2_f3_consume_package_allowance_source_ref_idempotent(self):
		"""Finding 3 [P1]: Replaying consume_package_allowance with the same source_ref must be
		100% idempotent both when overage > 0 and when overage == 0."""
		_rt, _room, res = self._make_kwd_inclusive_stay()
		folio.run_night_audit(PROP_KWD, self.day0)

		guest_folio = frappe.db.get_value(
			"Folio", {"reservation": res.name, "folio_type": "Guest"}, "name",
		)

		# Case A: overage > 0 (55.000 against 20.000 Spa allowance -> absorbs 20.000, bills 35.000 ONCE)
		out1 = packages.consume_package_allowance(
			res.name,
			transaction_code="3100",
			amount=55.000,
			business_date=self.day0,
			source_ref="SPA-ORD-101",
			description="Spa Treatment",
		)
		self.assertEqual(Decimal(str(out1["absorbed"])), Decimal("20.000"))
		self.assertEqual(Decimal(str(out1["guest_overage"])), Decimal("35.000"))
		self.assertEqual(Decimal(str(out1["remaining_allowance"])), Decimal("0.000"))

		out2 = packages.consume_package_allowance(
			res.name,
			transaction_code="3100",
			amount=55.000,
			business_date=self.day0,
			source_ref="SPA-ORD-101",
			description="Spa Treatment",
		)
		self.assertEqual(Decimal(str(out2["absorbed"])), Decimal("0.000"))
		self.assertEqual(Decimal(str(out2["guest_overage"])), Decimal("0.000"))
		self.assertEqual(Decimal(str(out2["remaining_allowance"])), Decimal("0.000"))

		fdoc = frappe.get_doc("Folio", guest_folio)
		over_rows = [
			c for c in fdoc.charges
			if c.idempotency_key == f"pkgover:{res.name}:SPA-ORD-101"
		]
		self.assertEqual(len(over_rows), 1)
		self.assertEqual(Decimal(str(over_rows[0].amount)), Decimal("35.000"))
		self.assertEqual(Decimal(str(fdoc.charges_total)), Decimal("335.000"))

		# Case B: overage == 0 (15.000 against 40.000 Breakfast allowance -> absorbs 15.000 ONCE)
		bf1 = packages.consume_package_allowance(
			res.name,
			transaction_code="1100",
			amount=15.000,
			business_date=self.day0,
			source_ref="BF-ORD-202",
			description="Breakfast Buffet",
		)
		self.assertEqual(Decimal(str(bf1["absorbed"])), Decimal("15.000"))
		self.assertEqual(Decimal(str(bf1["guest_overage"])), Decimal("0.000"))
		self.assertEqual(Decimal(str(bf1["remaining_allowance"])), Decimal("25.000"))

		bf2 = packages.consume_package_allowance(
			res.name,
			transaction_code="1100",
			amount=15.000,
			business_date=self.day0,
			source_ref="BF-ORD-202",
			description="Breakfast Buffet",
		)
		self.assertEqual(Decimal(str(bf2["absorbed"])), Decimal("0.000"))
		self.assertEqual(Decimal(str(bf2["guest_overage"])), Decimal("0.000"))
		self.assertEqual(Decimal(str(bf2["remaining_allowance"])), Decimal("25.000"))

		pnl = packages.package_allowance_summary(res.name)
		self.assertEqual(Decimal(str(pnl["consumed_total"])), Decimal("35.000"))
		self.assertEqual(Decimal(str(pnl["overage_total"])), Decimal("35.000"))

	def test_r2_f4_taxed_inclusive_package_reconciles_quote_after_tax(self):
		"""Finding 4 [P1]: On a 10% Fixed-GST hotel, a $400/2N inclusive package with a $50
		Meal Plan element (gst_rate=0) must post $220.00/night ($440.00 total), matching quote."""
		rt, room = self._setup_property(
			PROP_USD_TAX, "USD", "United States", gst_rate=10, rates_include_tax=0,
		)
		frappe.get_doc({
			"doctype": "Holiday Package",
			"property": PROP_USD_TAX,
			"package_name": "Taxed Escape 2N",
			"room_type": rt,
			"start_date": self.day0,
			"end_date": self.day1,
			"package_nights": 2,
			"price_2_pax": 400.00,
			"folio_display_mode": "Inclusive",
			"elements": [
				{
					"element_name": "Meal Plan",
					"element_type": "Meal",
					"transaction_code": "1100",
					"charge_type": "Meal Plan",
					"allowance_basis": "Per Room Night",
					"unit_allowance": 50.00,
					"posting_rhythm": "Every Night",
					"included_in_rate": 1,
					"folio_display_mode": "Inclusive",
					"gst_rate": 0,
				},
			],
		}).insert(ignore_permissions=True)

		res = make_reservation(
			PROP_USD_TAX, rt, self.day0, self.day2,
			status="Checked In", room=room, adults=2,
		)
		self.assertEqual(Decimal(str(res.amount_before_tax)), Decimal("400.00"))
		self.assertEqual(Decimal(str(res.amount_after_tax)), Decimal("440.00"))

		folio.run_night_audit(PROP_USD_TAX, self.day0)
		guest_folio = frappe.db.get_value(
			"Folio", {"reservation": res.name, "folio_type": "Guest"}, "name",
		)
		fdoc = frappe.get_doc("Folio", guest_folio)
		self.assertEqual(Decimal(str(fdoc.charges_total)), Decimal("200.00"))
		self.assertEqual(Decimal(str(fdoc.tax_total)), Decimal("20.00"))
		self.assertEqual(Decimal(str(fdoc.grand_total)), Decimal("220.00"))

		self.at(self.day1)
		folio.run_night_audit(PROP_USD_TAX, self.day1)
		fdoc.reload()
		self.assertEqual(Decimal(str(fdoc.charges_total)), Decimal("400.00"))
		self.assertEqual(Decimal(str(fdoc.tax_total)), Decimal("40.00"))
		self.assertEqual(Decimal(str(fdoc.grand_total)), Decimal("440.00"))
		self.assertEqual(Decimal(str(fdoc.grand_total)), Decimal(str(res.amount_after_tax)))

		recon = ledger.folio_reconciliation(PROP_USD_TAX)
		self.assertTrue(recon["in_balance"])
		self.assertEqual(recon["difference"], 0.0)

	def test_r2_f5a_fresh_install_seeds_pr3_transaction_codes(self):
		"""Finding 5a [P1]: ensure_default_transaction_codes and install.ensure_code_masters
		must seed 1400, 1410, 3300, and 3400 without needing a manual migration patch."""
		target_codes = ("1400", "1410", "3300", "3400")
		for code in target_codes:
			if frappe.db.exists("Transaction Code", code):
				frappe.db.delete("Transaction Code", {"name": code})

		ledger.ensure_default_transaction_codes()
		for code in target_codes:
			self.assertTrue(
				frappe.db.exists("Transaction Code", code),
				f"Transaction Code {code} missing after ensure_default_transaction_codes()",
			)

		for code in target_codes:
			frappe.db.delete("Transaction Code", {"name": code})
		install.ensure_code_masters()
		for code in target_codes:
			self.assertTrue(
				frappe.db.exists("Transaction Code", code),
				f"Transaction Code {code} missing after install.ensure_code_masters()",
			)

	def test_r2_f5b_direct_reservation_save_blocks_fixed_charges_tamper(self):
		"""Finding 5b [P1]: Reservation.validate must block direct REST/doc mutations to
		fixed_charges child rows while allowing benign Reservation saves."""
		rt, room = self._setup_property(PROP_FC, "USD", "United States", gst_rate=0)
		res = make_reservation(
			PROP_FC, rt, self.day0, self.day2,
			status="Confirmed", room=room, adults=2,
		)
		fixed_charges.save_fixed_charge(res.name, {
			"transaction_code": "3200",
			"charge_type": "Taxi",
			"description": "Protected Transfer",
			"qty": 1,
			"unit_price": 25.00,
			"frequency": "Daily",
			"start_date": self.day0,
			"end_date": self.day1,
			"window_no": 1,
		})

		# Tampering unit_price & post_count directly on Reservation.save() is refused
		res.reload()
		res.fixed_charges[0].unit_price = -99.0
		res.fixed_charges[0].post_count = 5
		with self.assertRaises(frappe.PermissionError):
			res.save(ignore_permissions=True)

		# Appending a rogue fixed_charges row directly on Reservation.save() is refused
		res.reload()
		res.append("fixed_charges", {
			"transaction_code": "3200",
			"charge_type": "Taxi",
			"description": "Injected",
			"qty": 1,
			"unit_price": 1.0,
			"frequency": "Daily",
			"start_date": self.day0,
			"end_date": self.day1,
			"window_no": 1,
			"active": 1,
		})
		with self.assertRaises(frappe.PermissionError):
			res.save(ignore_permissions=True)

		# Removing fixed_charges rows directly on Reservation.save() is refused
		res.reload()
		res.set("fixed_charges", [])
		with self.assertRaises(frappe.PermissionError):
			res.save(ignore_permissions=True)

		# Benign save on Reservation without altering fixed_charges still succeeds
		res.reload()
		res.special_requests = "High floor please"
		res.save(ignore_permissions=True)
		res.reload()
		self.assertEqual(res.special_requests, "High floor please")
		self.assertEqual(len(res.fixed_charges), 1)

	def test_r2_f5c_delete_fixed_charge_blocked_on_closed_statuses(self):
		"""Finding 5c [P1]: delete_fixed_charge must refuse Cancelled, Checked Out, and No Show stays."""
		rt, room = self._setup_property(PROP_FC, "USD", "United States", gst_rate=0)
		for closed_status in ("Cancelled", "Checked Out", "No Show"):
			res = make_reservation(
				PROP_FC, rt, self.day0, self.day2,
				status="Confirmed", room=room, adults=2,
			)
			row = fixed_charges.save_fixed_charge(res.name, {
				"transaction_code": "3200",
				"charge_type": "Taxi",
				"description": f"Charge before {closed_status}",
				"qty": 1,
				"unit_price": 20.00,
				"frequency": "Daily",
				"start_date": self.day0,
				"end_date": self.day1,
				"window_no": 1,
			})["row"]

			frappe.db.set_value("Reservation", res.name, "status", closed_status, update_modified=False)
			with self.assertRaises(frappe.ValidationError):
				fixed_charges.delete_fixed_charge(res.name, row["name"])
			self.assertTrue(frappe.db.exists("Reservation Fixed Charge", row["name"]))
