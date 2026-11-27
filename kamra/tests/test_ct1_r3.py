"""Tariff validity, currency precision, and guest-facing provisional bills."""
from datetime import date, timedelta
from inspect import unwrap
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase
from frappe.utils import getdate

from kamra import api, channel_ari, folio, ledger, pricing
from kamra.tests.audit_fixtures import AuditTestCase, d, guest_folio, make_property, make_reservation, make_room, make_room_type


class TariffControls(UnitTestCase):
	def test_changed_tariff_republishes_previously_approved_days(self):
		today = date(2026, 11, 27)
		old = frappe._dict(price_list="L", workflow_state="Approved", valid_from=today, valid_upto=None)
		for state in ("Approved", "Rejected"):
			with self.subTest(state=state):
				doc = frappe._dict(old, doctype="Item Price", name="T", flags=frappe._dict(), workflow_state=state,
				                   valid_upto=today + timedelta(days=560), get_doc_before_save=lambda: old)
				with patch.object(pricing, "workflow_state_field", return_value="workflow_state"), \
						patch.object(frappe, "get_meta"), patch.object(frappe, "get_all", return_value=["Hotel"]), \
						patch.object(channel_ari, "nowdate", return_value=today), patch.object(channel_ari, "enqueue_ari_change") as send:
					channel_ari.on_ari_source_change(doc, "on_update")
					windows = [channel_ari._clip(*c.args[1:3]) for c in send.call_args_list]
					self.assertTrue(any(w and getdate(w[0]) <= today + timedelta(days=561)
					                    and getdate(w[1]) >= today + timedelta(days=730) for w in windows), windows)

	def test_one_fils_override_exempts_arriving_guest(self):
		res = frappe._dict(property="H", agreed_rate=10.001, contract_tariff={"fx_lock_policy": "Fixed at Check In",
		                   "agreed_rate": 10, "tariff": {"nightly": 30, "currency": "USD", "property_currency": "KWD"}})
		self.assertIsNone(pricing.contract_fx_context(res, "Fixed at Check In"))

	def test_standalone_cashier_shows_required_pairs(self):
		with patch.object(frappe.db, "exists", return_value=False), patch.object(frappe, "get_meta"), \
				patch.object(frappe, "get_all", side_effect=lambda dt, **kw: {
					"Rate Plan": [frappe._dict(currency="CAD")],
					"Reservation": [frappe._dict(contract_tariff={"tariff": {"currency": "EUR"}})]
				}.get(dt, [])), patch("kamra.settings.list_setting", return_value=["GBP"]), \
				patch.object(pricing, "property_currency", return_value="UGX"), patch.object(ledger, "home_currency", return_value="UGX"), \
				patch.object(ledger, "get_business_date", return_value="2026-11-27"):
			rows = unwrap(ledger.list_exchange_rates)("H")["posting_rates"]
			self.assertEqual({(r["currency"], r["status"]) for r in rows}, {(c, "Missing") for c in ["GBP", "EUR", "CAD"]})

	def test_provisional_tax_uses_readable_default_template(self):
		dt = "Sales Taxes and Charges Template"
		for inclusive in (0, 1):
			with self.subTest(inclusive=inclusive):
				prop = frappe._dict(company="Test Co", rates_include_tax=inclusive, gst_rate_low=0, gst_rate_high=0)
				template = frappe._dict(taxes=[frappe._dict(charge_type="On Net Total", rate=18, included_in_print_rate=inclusive)])
				with patch.object(frappe.db, "exists", return_value=True), patch.object(frappe.db, "get_value", return_value="Default VAT"), \
						patch.object(frappe, "get_doc", return_value=template), patch.object(frappe, "has_permission", return_value=True):
					tax = api._apply_accounting_invoice_taxes(frappe._dict(status="Open"), prop, "UGX", {},
						[{"row": "CH", "gst_rate": 0, "gst_amount": 0}], set(), [], "VAT")
					self.assertEqual(tax["tax_source"], "accounting_deferred")
					self.assertIn("18%", tax["accounting_tax_note"])

	def test_new_manual_booking_skips_resolver_even_for_serialized_checkbox(self):
		from kamra.kamra.doctype.reservation.reservation import Reservation
		stay = frappe._dict(auto_price="0", flags=frappe._dict(), is_new=lambda: True, contract_tariff={"tariff": "stale"})
		with patch.object(pricing, "billable_children", return_value=0), patch.object(pricing, "contract_rate", return_value=None) as quote:
			Reservation.capture_contract_tariff(stay)
		quote.assert_not_called()
		self.assertIsNone(stay.contract_tariff)


class GuestBill(AuditTestCase):
	def setUp(self):
		super().setUp()
		self.prop = make_property("R3 Hotel", country="Uganda", currency="UGX", rates_include_tax=1, gst_rate_low=0, gst_rate_high=0)
		self.rt = make_room_type(self.prop, "JUNIOR-SUITE", room_type_name="Junior Suite")
		self.room = make_room(self.prop, self.rt, "110")
		self.res = make_reservation(self.prop, self.rt, d(-1), d(2), room=self.room, status="Checked In")
		self.folio = guest_folio(self.res.name)
		self.snapshot = {"agreed_rate": 526268, "tariff": {"currency": "USD", "nightly": 130, "fx_rate": 4048.22,
		                 "property_currency": "UGX", "fx_date": d(-1)}}
		self.persist()

	def persist(self):
		frappe.db.set_value("Reservation", self.res.name, {"agreed_rate": 290333.33, "auto_price": 0, "fixed_rate": 1,
			"contract_tariff": frappe.as_json(self.snapshot)})
		self.res.reload()

	def test_manual_rate_supersedes_stored_tariff_without_changing_snapshot(self):
		stay = api.folio_invoice(self.folio)["stay"]
		self.assertEqual(stay.get("pricing_basis"), "fixed_home")
		self.assertAlmostEqual(stay["fixed_rate_amount"], self.res.agreed_rate)
		self.assertFalse(stay.get("tariff"))
		self.assertIsNone(stay["reference_currency"])
		self.assertEqual(pricing.saved_contract_tariff(self.res)["tariff"]["nightly"], 130)

	def test_explicit_home_override_has_no_reference_totals(self):
		self.snapshot["agreed_override"] = {"currency": "UGX", "amount": 290333, "home_amount": 290333}
		self.persist()
		self.assertIsNone(folio.reference_currency(self.res, 871000, -7839000))

	def test_foreign_override_is_the_active_price_on_bill(self):
		self.snapshot["agreed_override"] = {"currency": "USD", "amount": 100, "home_amount": 404822, "fx_rate": 4048.22}
		self.persist()
		stay = api.folio_invoice(self.folio)["stay"]
		self.assertEqual(stay["tariff"]["nightly"], 100)
		self.assertEqual(stay["agreed_rate"], 404822)
		self.assertEqual(stay["reference_currency"]["currency"], "USD")

	def test_posted_description_and_invoice_use_room_display_name(self):
		self.assertEqual(folio._night_room_description(self.res, d(-1)), "Room 110 · Junior Suite")
		self.assertEqual(api.folio_invoice(self.folio)["stay"]["room_type_name"], "Junior Suite")

	def test_open_inclusive_bill_defers_zero_folio_tax(self):
		folio.post_room_night(self.res, d(-1))
		bill = api.folio_invoice(self.folio)
		self.assertEqual(bill["tax_source"], "accounting_deferred")
		self.assertEqual(bill["tax_summary"], [])
		self.assertEqual(bill["gst_summary"], [])
		self.assertTrue(bill["document"]["accounting_tax_note"])

	def test_reconcile_collected_advance_from_zero(self):
		fdoc = frappe.get_doc("Folio", self.folio)
		for kind, amount in [("Advance", 8710000), ("Settlement", 200), ("Security Deposit", 300)]:
			fdoc.append("payments", {"mode": "Cash", "posting_date": d(-1), "amount": amount, "payment_kind": kind})
		folio.reconcile_reservation_advance(self.res.name, self.prop, changed_folio=fdoc)
		self.assertEqual(frappe.db.get_value("Reservation", self.res.name, "advance_paid"), 8710000)

	def test_external_folio_save_reconciles_new_advance_once(self):
		doc = frappe.get_doc("Folio", self.folio)
		doc.append("payments", {"mode": "Cash", "posting_date": d(-1), "amount": 8710000, "payment_kind": "Advance"})
		doc.save()
		self.assertEqual(frappe.db.get_value("Reservation", self.res.name, "advance_paid"), 8710000)
		doc.save()
		self.assertEqual(frappe.db.get_value("Reservation", self.res.name, "advance_paid"), 8710000)
