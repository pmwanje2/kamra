"""Departed guest receivables follow the payee of each stay window."""
from decimal import Decimal

import frappe

from kamra import checkout, folio, receivables
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	make_property,
	make_reservation,
	make_room_type,
)
from kamra.tests.test_folio_windows import corp

RESIDUALS = {"UGX": Decimal("0.32"), "USD": Decimal("0.01"), "KWD": Decimal("0.001")}


class DepartedWindowReceivablesTest(AuditTestCase):
	def departed(self, currency):
		make_property(PROP_A, business_date=TODAY, currency=currency)
		stay = make_reservation(PROP_A, make_room_type(PROP_A), d(-1), TODAY, status="Checked Out")
		return stay.name, folio.open_folio(stay)

	def stored_balance(self, window, amount):
		# Model an imported bill, including stored fractions of a currency's minor unit.
		frappe.db.set_value("Folio", window, {
			"grand_total": 1000 + amount, "payments_total": 1000, "balance": amount,
		})

	def assert_guest_bills(self, stay, expected):
		report = receivables.guests_owing(PROP_A)
		summary = receivables.ar_accounts(PROP_A)["summary"]["guests_owing"]
		for rows in (report, summary):
			own = [row for row in rows if row["reservation"] == stay]
			self.assertCountEqual(
				[(row["folio"], Decimal(str(row["balance"]))) for row in own], list(expected.items()),
			)
			self.assertTrue(all(row["balance"] == row["net_collectible_balance"] for row in own))
		self.assertEqual(Decimal(str(checkout.settlement_position(stay)["owed"])), sum(expected.values()))

	def test_only_extra_window_owes_then_settles(self):
		for currency, residual in RESIDUALS.items():
			with self.subTest(currency=currency):
				stay, primary = self.departed(currency)
				extra = folio.open_window(reservation=stay)
				self.assertEqual(frappe.db.get_value("Folio", extra, ["folio_type", "payee_type"]), ("Extra", "Guest"))
				self.stored_balance(primary, 0)
				self.stored_balance(extra, residual)
				self.assert_guest_bills(stay, {extra: residual})
				self.stored_balance(extra, 0)
				self.assert_guest_bills(stay, {})

	def test_two_owing_windows_and_separate_primary_refund(self):
		for currency, residual in RESIDUALS.items():
			stay, primary = self.departed(currency)
			extra = folio.open_window(reservation=stay)
			self.stored_balance(extra, residual)
			for balance in (10, -10):
				with self.subTest(currency=currency, primary=balance):
					self.stored_balance(primary, balance)
					self.assertEqual(checkout.settlement_position(stay)["refund_due"], max(0, -balance))
					expected = {primary: 10, extra: residual} if balance > 0 else {extra: residual}
					self.assert_guest_bills(stay, expected)

	def test_payee_overrides_account_kind_and_legacy_extra_defaults_to_guest(self):
		stay, primary = self.departed("USD")
		explicit = folio.open_window(reservation=stay, folio_type="Company", payee_type="Guest")
		legacy = folio.open_window(reservation=stay)
		frappe.db.set_value("Folio", legacy, "payee_type", None)
		self.stored_balance(primary, 0)
		self.stored_balance(explicit, 11)
		self.stored_balance(legacy, 12)
		self.assert_guest_bills(stay, {explicit: 11, legacy: 12})

	def test_third_party_windows_and_group_master_are_excluded(self):
		stay, primary = self.departed("USD")
		sponsor = corp("R15 Window Sponsor")
		billed = folio.open_window(reservation=stay, folio_type="Extra", payee_type="Company", payee_company=sponsor)
		self.stored_balance(primary, 0)
		self.stored_balance(billed, 20)
		self.assertEqual(checkout.settlement_position(stay)["to_city_ledger"], [(billed, sponsor, 20)])
		self.assert_guest_bills(stay, {})

		agent = frappe.get_doc({"doctype": "Travel Agent", "agent_name": "R15 Agency"}).insert().name
		agency = folio.open_window(reservation=stay, payee_type="Travel Agent", payee_agent=agent)
		group = frappe.get_doc({
			"doctype": "Group Booking", "property": PROP_A, "group_name": "R15 group",
			"check_in_date": d(-1), "check_out_date": TODAY, "status": "Confirmed",
		}).insert()
		master = folio.open_group_folio(group.name)
		frappe.db.set_value("Folio", master, "reservation", stay)
		for window in (agency, master):
			self.stored_balance(window, 15)
		self.assertFalse([row for row in receivables.guests_owing(PROP_A) if row["reservation"] == stay])
