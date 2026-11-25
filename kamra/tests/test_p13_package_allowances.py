"""RED→GREEN tests for P13: Itemised Package Elements, Allowances, Breakage, 0/2/3dp precision & white-label."""

from __future__ import annotations

from decimal import Decimal

import frappe
from frappe.utils import add_days, getdate

from kamra import folio, pricing
from kamra.ledger import ensure_default_transaction_codes
from kamra.tests.audit_fixtures import (
	TODAY,
	AuditTestCase,
	ensure_user,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

PROP_KWD = "UT Pkg KWD"
PROP_UGX = "UT Pkg UGX"
PROP_USD = "UT Pkg USD"
PROP_OTHER = "UT Pkg Other"


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


def _ensure_txn_code(code: str, description: str, charge_type: str, txn_type: str = "Revenue",
                     revenue_group: str = "Other", prop: str | None = None):
	if frappe.db.exists("Transaction Code", code):
		return code
	frappe.get_doc({
		"doctype": "Transaction Code",
		"code": code,
		"description": description,
		"property": prop,
		"txn_type": txn_type,
		"revenue_group": revenue_group,
		"subgroup": charge_type,
		"charge_type": charge_type,
		"disabled": 0,
	}).insert(ignore_permissions=True)
	return code


class TestP13PackageAllowances(AuditTestCase):
	properties = (PROP_KWD, PROP_UGX, PROP_USD, PROP_OTHER)

	def setUp(self):
		super().setUp()
		self.at(TODAY)
		_ensure_currency("KWD", "KD", 1000)
		_ensure_currency("UGX", "UGX", 1)
		_ensure_currency("USD", "$", 100)
		ensure_default_transaction_codes()
		_ensure_txn_code("1400", "Package Allowance", "Allowance", txn_type="Package", revenue_group="Package")
		_ensure_txn_code("1410", "Package Breakage", "Misc", txn_type="Package", revenue_group="Package")

		self.day0 = str(getdate(TODAY))
		self.day1 = str(add_days(self.day0, 1))
		self.day2 = str(add_days(self.day0, 2))
		self.day3 = str(add_days(self.day0, 3))
		self.day4 = str(add_days(self.day0, 4))

	def _setup_property(self, prop_name: str, currency: str, country: str = "Kuwait"):
		make_property(
			prop_name,
			country=country,
			currency=currency,
			business_date=self.day0,
			gst_mode="Fixed",
			gst_rate_low=0,
			gst_rate_high=0,
			rates_include_tax=0,
		)
		rt = make_room_type(prop_name, "DLX", base_price=200, adults_capacity=4)
		room = make_room(prop_name, rt, "101")
		return rt, room

	def test_inclusive_package_splits_room_vs_allowance_and_tracks_over_under_consumption_kwd_3dp(self):
		"""300.000 KWD/night package = 240.000 Room Net + 40.000 Breakfast (1100) + 20.000 Spa (3100).
		Outlet posts 55.000 KWD Spa -> absorbs 20.000, bills only 35.000 extra;
		unused Breakfast records 40.000 KWD breakage without double-charging the guest."""
		from kamra import packages

		rt, room = self._setup_property(PROP_KWD, "KWD", "Kuwait")
		frappe.get_doc({
			"doctype": "Holiday Package",
			"property": PROP_KWD,
			"package_name": "Royal Wellness 2N",
			"room_type": rt,
			"start_date": self.day0,
			"end_date": self.day1,
			"package_nights": 2,
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
		# Night 1 audit posts exact 3dp split summing to 300.000 KWD
		audit = folio.run_night_audit(PROP_KWD, self.day0)
		self.assertEqual(audit["room_charges_posted"], 1)
		self.assertEqual(audit["package_elements_posted"], 2)

		guest_folio_name = frappe.db.get_value(
			"Folio", {"reservation": res.name, "folio_type": "Guest"}, "name",
		)
		fdoc = frappe.get_doc("Folio", guest_folio_name)
		by_code = {c.transaction_code: Decimal(str(c.amount)) for c in fdoc.charges}
		self.assertEqual(by_code["1000"], Decimal("240.000"))
		self.assertEqual(by_code["1100"], Decimal("40.000"))
		self.assertEqual(by_code["3100"], Decimal("20.000"))
		self.assertEqual(sum(by_code.values()), Decimal("300.000"))
		# All lines carry deterministic pkg: idempotency_key
		for c in fdoc.charges:
			self.assertTrue((c.idempotency_key or "").startswith(f"pkg:{res.name}:"))

		# Inclusive guest-facing view bundles into one line while preserving breakdown
		inc_view = packages.package_folio_view(guest_folio_name, display_mode="Inclusive")
		self.assertEqual(inc_view["display_mode"], "Inclusive")
		self.assertEqual(len(inc_view["lines"]), 1)
		self.assertIn("Royal Wellness 2N", inc_view["lines"][0]["description"])
		self.assertEqual(Decimal(str(inc_view["lines"][0]["amount"])), Decimal("300.000"))
		self.assertEqual(len(inc_view["lines"][0]["breakdown"]), 3)

		# Separate Add-on display mode shows all 3 itemised lines
		sep_view = packages.package_folio_view(guest_folio_name, display_mode="Separate Add-on")
		self.assertEqual(sep_view["display_mode"], "Separate Add-on")
		self.assertEqual(len(sep_view["lines"]), 3)

		# Outlet posts 55.000 KWD Spa -> absorbs 20.000 allowance, bills only 35.000 overage
		spa_out = packages.consume_package_allowance(
			res.name, transaction_code="3100", amount=55.000,
			business_date=self.day0, description="Deep Tissue Massage",
		)
		self.assertEqual(Decimal(str(spa_out["absorbed"])), Decimal("20.000"))
		self.assertEqual(Decimal(str(spa_out["guest_overage"])), Decimal("35.000"))
		self.assertEqual(Decimal(str(spa_out["remaining_allowance"])), Decimal("0.000"))

		fdoc.reload()
		self.assertEqual(Decimal(str(fdoc.charges_total)), Decimal("335.000"))

		# Settle breakage for day0: unused Breakfast (40.000) recorded as breakage, guest NOT double-charged
		brk = packages.settle_package_breakage(res.name, business_date=self.day0)
		self.assertEqual(Decimal(str(brk["breakage_total"])), Decimal("40.000"))
		self.assertEqual(brk["breakage_code"], "1410")

		fdoc.reload()
		self.assertEqual(Decimal(str(fdoc.charges_total)), Decimal("335.000"))

		# Package allowance P&L summary reflects exact numbers
		pnl = packages.package_allowance_summary(res.name)
		self.assertEqual(Decimal(str(pnl["allocated_total"])), Decimal("60.000"))
		self.assertEqual(Decimal(str(pnl["consumed_total"])), Decimal("20.000"))
		self.assertEqual(Decimal(str(pnl["overage_total"])), Decimal("35.000"))
		self.assertEqual(Decimal(str(pnl["breakage_total"])), Decimal("40.000"))

	def test_precision_0dp_ugx_2dp_usd_3dp_kwd_and_no_portuguese_extra_night_label(self):
		"""UGX (0dp), USD (2dp), KWD (3dp) quantize natively with zero hardcoded Decimal('0.01')
		and extra package nights use neutral English '(Additional Night)', never '(Diária Adicional)'."""
		from kamra import packages

		# UGX (0dp): 1000000 over 3 nights -> 333333, 333333, 333334 (exact 1000000, 0 decimal places)
		rt_ugx, room_ugx = self._setup_property(PROP_UGX, "UGX", "Uganda")
		pkg_ugx = frappe.get_doc({
			"doctype": "Holiday Package",
			"property": PROP_UGX,
			"package_name": "Kampala Weekend 3N",
			"room_type": rt_ugx,
			"start_date": self.day0,
			"end_date": self.day2,
			"package_nights": 3,
			"extra_night_percent": 50,
			"price_2_pax": 1000000,
			"elements": [
				{
					"element_name": "Breakfast",
					"element_type": "Meal",
					"transaction_code": "1100",
					"charge_type": "Meal Plan",
					"allowance_basis": "Per Room Night",
					"unit_allowance": 50000,
					"posting_rhythm": "Every Night",
					"included_in_rate": 1,
				},
			],
		}).insert(ignore_permissions=True)

		# 4 nights triggers extra night at 50% of daily rate, quantized to whole UGX (0dp)
		total_4n_ugx = pkg_ugx.package_price_for(2, nights=4)
		self.assertEqual(total_4n_ugx, total_4n_ugx.to_integral_value())

		q_ugx = pricing.quote(PROP_UGX, rt_ugx, self.day0, self.day4, adults=2)
		self.assertNotIn("Diária Adicional", q_ugx["nightly"][-1]["package"])
		self.assertIn("(Additional Night)", q_ugx["nightly"][-1]["package"])
		# Every UGX nightly rate is a whole shilling (0dp)
		for n in q_ugx["nightly"]:
			self.assertEqual(Decimal(str(n["rate"])), Decimal(str(n["rate"])).to_integral_value())

		# Split preserves exact minor-unit conservation on UGX
		res_ugx = make_reservation(PROP_UGX, rt_ugx, self.day0, self.day3, status="Checked In", room=room_ugx)
		split_ugx = packages.split_package_night(res_ugx, pkg_ugx, Decimal("333333"), self.day0)
		self.assertEqual(split_ugx["room_net"], Decimal("283333"))
		self.assertEqual(split_ugx["elements"][0]["amount"], Decimal("50000"))
		self.assertEqual(split_ugx["room_net"] + split_ugx["included_total"], Decimal("333333"))

		# KWD (3dp): 100.000 / 3 = 33.333, 33.333, 33.334 (3dp exact, never truncated to 2dp!)
		rt_kwd, _room_kwd = self._setup_property(PROP_KWD, "KWD", "Kuwait")
		frappe.get_doc({
			"doctype": "Holiday Package",
			"property": PROP_KWD,
			"package_name": "Desert Retreat 3N",
			"room_type": rt_kwd,
			"start_date": self.day0,
			"end_date": self.day2,
			"package_nights": 3,
			"extra_night_percent": 50,
			"price_2_pax": 100.000,
		}).insert(ignore_permissions=True)
		q_kwd = pricing.quote(PROP_KWD, rt_kwd, self.day0, self.day3, adults=2)
		self.assertEqual(Decimal(str(q_kwd["nightly"][0]["rate"])), Decimal("33.333"))
		self.assertEqual(Decimal(str(q_kwd["nightly"][2]["rate"])), Decimal("33.334"))
		self.assertEqual(sum(Decimal(str(n["rate"])) for n in q_kwd["nightly"]), Decimal("100.000"))

		# USD (2dp): 250.00 / 2 with separate add-on (included_in_rate=0) does NOT reduce room_net
		rt_usd, room_usd = self._setup_property(PROP_USD, "USD", "United States")
		pkg_usd = frappe.get_doc({
			"doctype": "Holiday Package",
			"property": PROP_USD,
			"package_name": "City Break 2N",
			"room_type": rt_usd,
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
		res_usd = make_reservation(PROP_USD, rt_usd, self.day0, self.day2, status="Checked In", room=room_usd)
		split_d0 = packages.split_package_night(res_usd, pkg_usd, Decimal("125.00"), self.day0)
		self.assertEqual(split_d0["room_net"], Decimal("125.00"))
		self.assertEqual(split_d0["addon_total"], Decimal("45.50"))
		self.assertEqual(split_d0["total_posted"], Decimal("170.50"))
		# First Night rhythm does NOT post on night 2
		split_d1 = packages.split_package_night(res_usd, pkg_usd, Decimal("125.00"), self.day1)
		self.assertEqual(split_d1["elements"], [])
		self.assertEqual(split_d1["room_net"], Decimal("125.00"))

	def test_package_endpoints_enforce_property_isolation(self):
		"""A staff user scoped only to PROP_OTHER receives PermissionError on PROP_USD package endpoints."""
		from kamra import packages

		rt_usd, room_usd = self._setup_property(PROP_USD, "USD", "United States")
		self._setup_property(PROP_OTHER, "USD", "United States")
		res_usd = make_reservation(PROP_USD, rt_usd, self.day0, self.day2, status="Checked In", room=room_usd)

		other_user = ensure_user("pkg.other@test.local", ["Front Desk"])
		if not frappe.db.exists("User Permission", {"user": other_user, "allow": "Property", "for_value": PROP_OTHER}):
			frappe.get_doc({
				"doctype": "User Permission",
				"user": other_user,
				"allow": "Property",
				"for_value": PROP_OTHER,
				"apply_to_all_doctypes": 1,
			}).insert(ignore_permissions=True)
		frappe.clear_cache(user=other_user)

		frappe.set_user(other_user)  # nosemgrep: frappe-setuser -- testing property isolation
		with self.assertRaises(frappe.PermissionError):
			packages.package_allowance_summary(res_usd.name)
		with self.assertRaises(frappe.PermissionError):
			packages.consume_package_allowance(res_usd.name, transaction_code="3100", amount=10.0)
