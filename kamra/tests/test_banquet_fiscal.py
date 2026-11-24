"""Events bill through folios, and divisible halls block their parts.

OPERA Cloud Sales and Event Management posts event charges and deposits to a
folio, and Revenue Type Mapping picks each charge's transaction code from the
revenue type, event type and function space. Under the ERPNext accounting
bridge a folio is what becomes a Payment Entry and a Sales Invoice (and the
EFRIS fiscal document), so a standalone function's money moves to a folio
with no stay: the event folio. The bridge is detected the way every bridge
reader is (kamra.erp_bridge), here through a double.

A combination (divisible) function space blocks the spaces it is made of
and is blocked by them.
"""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from kamra import banquet as bq
from kamra import erp_bridge
from kamra.tests.fixtures import PROPERTY, _upsert, build, enquiry

FISCAL = "hospitality_kamra.fiscal_document"


@contextmanager
def accounting_bridge():
	real = erp_bridge.get_bridge_script
	reader = object()
	with patch.object(erp_bridge, "get_bridge_script",
	                  side_effect=lambda method: reader if method == FISCAL else real(method)):
		yield


def confirmed(f, **kw):
	fn = enquiry(f, **kw)
	bq.add_menu(fn, f["menu"])
	bq.set_status(fn, "Confirmed")
	return fn


def sheet(fn):
	return frappe.get_doc("Venue Booking", fn)


class BanquetFiscalCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		frappe.local.lang = frappe.local.lang or "en"
		self.f = build()


class TestEventFolio(BanquetFiscalCase):
	def test_a_standalone_deposit_lands_on_the_event_folio(self):
		fn = confirmed(self.f)
		with accounting_bridge():
			out = bq.record_receipt(fn, 50000, mode="Cash", kind="Advance", reference="Wedding deposit")
		doc = sheet(fn)
		self.assertTrue(doc.folio)
		folio = frappe.get_doc("Folio", doc.folio)
		self.assertEqual((folio.folio_type, folio.venue_booking, folio.property, folio.reservation, folio.status),
		                 ("Account", fn, PROPERTY, None, "Open"))
		self.assertEqual([(p.payment_kind, p.mode, p.amount) for p in folio.payments],
		                 [("Advance", "Cash", 50000)])
		self.assertEqual([(r.kind, r.amount, r.folio_payment) for r in doc.receipts],
		                 [("Advance", 50000, folio.payments[0].name)])
		self.assertEqual((out["folio"], doc.advance_received), (folio.name, 50000))

	def test_later_receipts_reuse_the_open_event_folio(self):
		fn = confirmed(self.f)
		with accounting_bridge():
			bq.record_receipt(fn, 20000, mode="Cash", kind="Advance")
			bq.record_receipt(fn, 30000, mode="Cash", kind="Payment")
		self.assertEqual(frappe.db.count("Folio", {"venue_booking": fn}), 1)
		folio = frappe.get_doc("Folio", sheet(fn).folio)
		self.assertEqual([p.amount for p in folio.payments], [20000, 30000])

	def test_refunds_are_not_taken_on_the_function_sheet_under_the_bridge(self):
		fn = confirmed(self.f)
		with accounting_bridge(), self.assertRaisesRegex(frappe.ValidationError, "native"):
			bq.record_receipt(fn, 1000, mode="Cash", kind="Refund")
		self.assertFalse(sheet(fn).receipts)
		self.assertFalse(frappe.db.exists("Folio", {"venue_booking": fn}))

	def test_a_group_functions_money_goes_to_the_group_master_folio(self):
		fn = confirmed(self.f)
		frappe.db.set_value("Venue Booking", fn, "group_booking", "GRP-UT-NOT-REAL")
		with accounting_bridge(), self.assertRaisesRegex(frappe.ValidationError, "master folio"):
			bq.record_receipt(fn, 1000, mode="Cash", kind="Advance")
		self.assertFalse(frappe.db.exists("Folio", {"venue_booking": fn}))

	def test_the_non_fiscal_function_invoice_is_disabled_under_the_bridge(self):
		fn = confirmed(self.f)
		with accounting_bridge():
			with self.assertRaisesRegex(frappe.ValidationError, "event folio"):
				bq.generate_invoice(fn)
			with self.assertRaisesRegex(frappe.ValidationError, "event folio"):
				bq.banquet_document(fn, "invoice")
		self.assertFalse(sheet(fn).invoice_number)

	def test_final_billing_posts_a_standalone_function_to_its_event_folio(self):
		fn = confirmed(self.f)
		mapping("Venue Rental", revenue_code("UT-BQ-HALL"))
		mapping("Menu", revenue_code("UT-BQ-FOOD", charge_type="Food & Beverage"))
		with accounting_bridge():
			out = bq.post_to_folio(fn)
		doc = sheet(fn)
		folio = frappe.get_doc("Folio", out["folio"])
		self.assertEqual((folio.folio_type, folio.venue_booking, doc.folio, doc.posted_to_folio),
		                 ("Account", fn, folio.name, 1))
		self.assertTrue(folio.charges)
		for row in folio.charges:
			self.assertEqual((float(row.gst_rate or 0), float(row.gst_amount or 0)), (0.0, 0.0))
			self.assertAlmostEqual(float(row.total), float(row.amount), 2)

	def test_post_to_folio_under_bridge_never_calculates_line_tax_even_for_fnb_and_alcohol(self):
		"""Hydrogen P0-2 (probe1): bridge KMR-TAX-001 forbids line tax on Folio Charges."""
		fn = confirmed(self.f)
		bq.add_service(fn, self.f["led"])
		doc = sheet(fn)
		doc.append("items", {
			"item_type": "Alcohol", "item_name": "House Wine", "qty": 2, "uom": "Unit",
			"list_rate": 5000, "rate": 5000, "chargeable": 1, "is_alcohol": 1, "gst_rate": 18,
		})
		doc.save()
		mapping("Venue Rental", revenue_code("UT-BQ-HALL"))
		mapping("Menu", revenue_code("UT-BQ-FOOD", charge_type="Food & Beverage"))
		mapping("Audio Visual", revenue_code("UT-BQ-AV"))
		mapping("Alcohol", revenue_code("UT-BQ-BAR", charge_type="Food & Beverage"))
		with accounting_bridge():
			out = bq.post_to_folio(fn)
		folio = frappe.get_doc("Folio", out["folio"])
		self.assertEqual(len(folio.charges), 4)
		for row in folio.charges:
			self.assertEqual(float(row.gst_rate or 0), 0.0, row.description)
			self.assertEqual(float(row.gst_amount or 0), 0.0, row.description)
			self.assertAlmostEqual(float(row.total), float(row.amount), 2, row.description)

	def test_no_hardcoded_18_percent_tax_on_zero_tax_uganda_property(self):
		"""Hydrogen P0-2: tax comes from property/item config, never hardcoded 18."""
		ug_prop = _upsert("Property", {"property_name": "UT Uganda Banquet Hotel"}, {
			"property_name": "UT Uganda Banquet Hotel", "country": "Uganda", "currency": "UGX",
			"gst_rate_low": 0, "gst_rate_high": 0, "business_date": nowdate(),
		})
		ug_hall = _upsert("Venue", {"property": ug_prop, "venue_name": "UT Uganda Hall"}, {
			"property": ug_prop, "venue_name": "UT Uganda Hall", "venue_type": "Banquet Hall",
			"capacity": 100, "base_price": 500000, "gst_rate": 0,
		})
		ug_av = _upsert("Banquet Service Item", {"property": ug_prop, "item_name": "UT Uganda PA"}, {
			"property": ug_prop, "item_name": "UT Uganda PA", "category": "Audio Visual",
			"uom": "Per Event", "rate": 100000, "cost_rate": 50000, "gst_rate": 0, "cost_gst_rate": 0,
			"chargeable": 1,
		})
		res = bq.create_enquiry(
			ug_prop, ug_hall, add_days(nowdate(), 20), "Uganda Client",
			event_type="Conference", attendees=50, with_venue_line=1,
		)
		bq.add_service(res["function"], ug_av)
		doc = sheet(res["function"])
		self.assertEqual(len(doc.items), 2)
		for row in doc.items:
			self.assertEqual(float(row.gst_rate or 0), 0.0, row.item_name)
			self.assertEqual(float(row.gst_amount or 0), 0.0, row.item_name)
			self.assertEqual(float(row.input_tax or 0), 0.0, row.item_name)
		self.assertEqual(float(doc.tax_amount or 0), 0.0)

	def test_record_receipt_refuses_after_event_folio_is_closed(self):
		"""Hydrogen P1-2 (probe2a): closed event folio refuses receipts, never opens a second folio."""
		from kamra.folio import close_folio

		fn = confirmed(self.f)
		mapping("Venue Rental", revenue_code("UT-BQ-HALL"))
		mapping("Menu", revenue_code("UT-BQ-FOOD", charge_type="Food & Beverage"))
		with accounting_bridge():
			out = bq.post_to_folio(fn)
			folio_doc = frappe.get_doc("Folio", out["folio"])
			bq.record_receipt(fn, float(folio_doc.balance), mode="Cash", kind="Payment")
			close_folio(out["folio"])
			with self.assertRaisesRegex(frappe.ValidationError, "closed"):
				bq.record_receipt(fn, 1000, mode="Cash", kind="Payment")
		self.assertEqual(frappe.db.count("Folio", {"venue_booking": fn}), 1)
		self.assertEqual(sheet(fn).folio, out["folio"])

	def test_a_held_deposit_is_refunded_natively_and_unblocks_close_out_with_property_currency(self):
		"""Hydrogen P1-3 (probe3a-3d): UGX error text (no rupee sign) and native refund/cancel unblocks close_out."""
		from kamra.ledger import write_ledger_entry

		ug_prop = _upsert("Property", {"property_name": "UT Uganda Deposit Hotel"}, {
			"property_name": "UT Uganda Deposit Hotel", "country": "Uganda", "currency": "UGX",
			"gst_rate_low": 0, "gst_rate_high": 0, "business_date": nowdate(),
		})
		ug_hall = _upsert("Venue", {"property": ug_prop, "venue_name": "UT Deposit Hall"}, {
			"property": ug_prop, "venue_name": "UT Deposit Hall", "venue_type": "Banquet Hall",
			"capacity": 100, "base_price": 500000, "gst_rate": 0,
		})
		fn = bq.create_enquiry(
			ug_prop, ug_hall, add_days(nowdate(), 22), "Deposit Client",
			event_type="Conference", attendees=50, with_venue_line=1,
		)["function"]
		bq.set_status(fn, "Confirmed")
		with accounting_bridge():
			bq.record_receipt(fn, 200000, mode="Cash", kind="Advance")
			bq.record_receipt(fn, 100000, mode="Cash", kind="Security Deposit")
			# Advance is not counted in deposit_held; only the 100,000 security deposit is held.
			self.assertEqual(bq.function_sheet(fn)["deposit_held"], 100000.0)
			with self.assertRaises(frappe.ValidationError) as ctx:
				bq.close_out(fn, refund_deposit=1)
			msg = str(ctx.exception)
			self.assertIn("native", msg)
			self.assertNotIn("₹", msg)
			self.assertIn("100,000", msg)
			self.assertNotEqual(sheet(fn).status, "Completed")
			# Simulate Finance's native Pay refund PE copied to the event folio + Deposit ledger debit 9004.
			folio = frappe.get_doc("Folio", sheet(fn).folio)
			folio.append("payments", {
				"posting_date": nowdate(), "payment_kind": "Refund", "mode": "Cash",
				"amount": -100000.0, "currency": "UGX", "reference": "PE-REF-1",
			})
			folio.save(ignore_permissions=True)
			write_ledger_entry(
				property=ug_prop, folio=folio.name, ledger="Deposit", transaction_code="9004",
				debit=100000.0, credit=0.0, description="Refund Cash",
				source_doctype="Folio Payment", source_name=folio.payments[-1].name,
			)
			self.assertEqual(bq.function_sheet(fn)["deposit_held"], 0.0)
			closed = bq.close_out(fn, refund_deposit=1)
			self.assertEqual((closed["status"], closed["deposit_held"]), ("Completed", 0.0))

	def test_post_to_folio_only_allows_roles_with_native_folio_and_banquet_write(self):
		"""Hydrogen P1-4a (probe5): Front Desk & Hotel Admin may post; Kamra Agent, Revenue Manager, Finance refused."""
		from kamra.tests.audit_fixtures import ensure_user

		fn = confirmed(self.f)
		mapping("Venue Rental", revenue_code("UT-BQ-HALL"))
		mapping("Menu", revenue_code("UT-BQ-FOOD", charge_type="Food & Beverage"))
		for role in ("Kamra Agent", "Revenue Manager", "Finance"):
			with self.subTest(role=role):
				user = ensure_user(f"bq-{role.lower().replace(' ', '-')}@test.local", [role])
				frappe.set_user(user)  # nosemgrep: frappe-setuser -- test single-role permission gate
				try:
					with accounting_bridge(), self.assertRaises(frappe.PermissionError):
						bq.post_to_folio(fn)
				finally:
					frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- restore admin
		desk = ensure_user("bq-front-desk@test.local", ["Front Desk"])
		frappe.set_user(desk)  # nosemgrep: frappe-setuser -- test permitted Front Desk role
		try:
			with accounting_bridge():
				out = bq.post_to_folio(fn)
			self.assertTrue(out["ok"])
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- restore admin

	def test_corporate_event_settles_to_city_ledger_and_invoices_company(self):
		"""Hydrogen P1-4b (probe6/6b): corporate event settles on credit to city ledger like OPERA."""
		from kamra import api, ledger

		corp_credit = _upsert("Corporate Account", {"company_name": "UT Event Credit Corp"}, {
			"company_name": "UT Event Credit Corp", "credit_allowed": 1,
		})
		corp_cash = _upsert("Corporate Account", {"company_name": "UT Event Cash Corp"}, {
			"company_name": "UT Event Cash Corp", "credit_allowed": 0,
		})
		mapping("Venue Rental", revenue_code("UT-BQ-HALL"))
		mapping("Menu", revenue_code("UT-BQ-FOOD", charge_type="Food & Beverage"))

		fn_cash = confirmed(self.f, company=corp_cash)
		with accounting_bridge():
			bq.post_to_folio(fn_cash)
			with self.assertRaisesRegex(frappe.ValidationError, "credit"):
				bq.settle_event_folio(fn_cash, to_city_ledger=1)

		fn = confirmed(self.f, company=corp_credit)
		with accounting_bridge():
			out = bq.post_to_folio(fn)
			folio_doc = frappe.get_doc("Folio", out["folio"])
			self.assertEqual(ledger.company_payer_of(folio_doc), corp_credit)
			settled = bq.settle_event_folio(fn, to_city_ledger=1)
		self.assertTrue(settled["ok"])
		self.assertTrue(settled["city_ledger"])
		closed_folio = frappe.get_doc("Folio", out["folio"])
		self.assertEqual((closed_folio.status, float(closed_folio.balance)), ("Closed", 0.0))
		self.assertEqual(api.folio_invoice(out["folio"])["bill_to"]["name"], "UT Event Credit Corp")

	def test_banquet_receipt_accepts_mobile_money_from_config_and_rejects_unconfigured(self):
		"""Hydrogen P1-4c (probe4a): Banquet Receipt payment modes come from configuration, not a hardcoded Select."""
		ug_prop = _upsert("Property", {"property_name": "UT Uganda Tender Hotel"}, {
			"property_name": "UT Uganda Tender Hotel", "country": "Uganda", "currency": "UGX",
			"gst_rate_low": 0, "gst_rate_high": 0, "business_date": nowdate(),
		})
		ug_hall = _upsert("Venue", {"property": ug_prop, "venue_name": "UT Tender Hall"}, {
			"property": ug_prop, "venue_name": "UT Tender Hall", "venue_type": "Banquet Hall",
			"capacity": 100, "base_price": 500000, "gst_rate": 0,
		})
		fn = bq.create_enquiry(
			ug_prop, ug_hall, add_days(nowdate(), 25), "Mobile Client",
			event_type="Conference", attendees=40, with_venue_line=1,
		)["function"]
		bq.set_status(fn, "Confirmed")
		# Off-bridge Uganda pack allows Mobile Money and refuses India's UPI.
		bq.record_receipt(fn, 25000, mode="Mobile Money", kind="Advance", reference="MM-001")
		self.assertEqual(sheet(fn).receipts[-1].mode, "Mobile Money")
		with self.assertRaises(frappe.ValidationError):
			bq.record_receipt(fn, 10000, mode="UPI", kind="Advance", reference="BAD-1")
		# On-bridge with configured tender_modes allows Mobile Money and rejects unconfigured tenders.
		fn2 = bq.create_enquiry(
			ug_prop, ug_hall, add_days(nowdate(), 26), "Mobile Bridge Client",
			event_type="Conference", attendees=40, with_venue_line=1,
		)["function"]
		bq.set_status(fn2, "Confirmed")
		tenders = [{"mode": "Cash", "currencies": ["UGX"]}, {"mode": "Mobile Money", "currencies": ["UGX"]}]
		with accounting_bridge(), patch("kamra.localization.tender_modes", return_value=tenders):
			bq.record_receipt(fn2, 30000, mode="Mobile Money", kind="Advance", reference="MM-002")
			self.assertEqual(sheet(fn2).receipts[-1].mode, "Mobile Money")
			with self.assertRaises(frappe.ValidationError):
				bq.record_receipt(fn2, 5000, mode="Cheque", kind="Advance", reference="CHQ-1")

	def test_post_to_folio_includes_service_charge_on_fnb_lines(self):
		"""Hydrogen P1-4d: VenueBooking.service_charge on F&B lines posts to the folio."""
		fn = confirmed(self.f)
		bq.update_function(fn, {"service_charge_percent": 10})
		doc = sheet(fn)
		expected_sc = float(doc.service_charge)
		self.assertGreater(expected_sc, 0)
		mapping("Venue Rental", revenue_code("UT-BQ-HALL"))
		mapping("Menu", revenue_code("UT-BQ-FOOD", charge_type="Food & Beverage"))
		mapping("Service Charge", revenue_code("UT-BQ-SC"))
		with accounting_bridge():
			out = bq.post_to_folio(fn)
		folio = frappe.get_doc("Folio", out["folio"])
		sc_lines = [c for c in folio.charges if c.transaction_code == "UT-BQ-SC"]
		self.assertEqual(len(sc_lines), 1)
		self.assertAlmostEqual(float(sc_lines[0].amount), expected_sc, 2)
		self.assertAlmostEqual(float(folio.charges_total), float(doc.taxable_amount), 2)

	def test_folio_history_includes_event_account_folios(self):
		"""Hydrogen P1-4e: OPERA Folio History includes standalone event Account folios."""
		from kamra import ledger
		from kamra.folio import close_folio

		fn = confirmed(self.f)
		mapping("Venue Rental", revenue_code("UT-BQ-HALL"))
		mapping("Menu", revenue_code("UT-BQ-FOOD", charge_type="Food & Beverage"))
		with accounting_bridge():
			out = bq.post_to_folio(fn)
			folio_doc = frappe.get_doc("Folio", out["folio"])
			bq.record_receipt(fn, float(folio_doc.balance), mode="Cash", kind="Payment")
			close_folio(out["folio"])
		hist = ledger.folio_history(PROPERTY)["folios"]
		by_name = {r.name: r for r in hist}
		self.assertIn(out["folio"], by_name)
		self.assertEqual(by_name[out["folio"]].get("venue_booking"), fn)
		queried = ledger.folio_history(PROPERTY, query=fn)["folios"]
		self.assertIn(out["folio"], [r.name for r in queried])

	def test_the_function_sheet_says_money_runs_through_the_event_folio(self):
		fn = confirmed(self.f)
		with accounting_bridge():
			bq.record_receipt(fn, 5000, mode="Cash", kind="Advance")
			out = bq.function_sheet(fn)
		self.assertEqual(out["fiscal_bridge"], 1)
		self.assertEqual(out["event_folio"], sheet(fn).folio)

	def test_event_security_deposit_full_chains_and_native_refund_summary(self):
		"""OPERA/SAP: event Security Deposit never counts in payments_total/balance; native refunds mirror in function_sheet."""
		from kamra import ledger
		from kamra.folio import _recalculate, close_folio
		from kamra.ledger import write_ledger_entry

		ug_prop = _upsert("Property", {"property_name": "UT Uganda Event Deposit Chain"}, {
			"property_name": "UT Uganda Event Deposit Chain", "country": "Uganda", "currency": "UGX",
			"gst_rate_low": 0, "gst_rate_high": 0, "business_date": nowdate(),
		})
		ug_hall = _upsert("Venue", {"property": ug_prop, "venue_name": "UT Chain Hall"}, {
			"property": ug_prop, "venue_name": "UT Chain Hall", "venue_type": "Banquet Hall",
			"capacity": 100, "base_price": 500000, "gst_rate": 0,
		})
		hall_code = revenue_code("UT-BQ-CHAIN-HALL")
		if not frappe.db.exists("Banquet Revenue Mapping", {"property": ug_prop, "revenue_type": "Venue Rental"}):
			frappe.get_doc({"doctype": "Banquet Revenue Mapping", "property": ug_prop,
			                "revenue_type": "Venue Rental", "transaction_code": hall_code}).insert()

		# Chain A: collect deposit + advance -> post charges -> close while held -> refund later (partial + full)
		fn_held = bq.create_enquiry(
			ug_prop, ug_hall, add_days(nowdate(), 30), "Chain Held Client",
			event_type="Conference", attendees=50, with_venue_line=1,
		)["function"]
		bq.set_status(fn_held, "Confirmed")
		with accounting_bridge():
			bq.record_receipt(fn_held, 100000, mode="Cash", kind="Security Deposit")
			bq.record_receipt(fn_held, 200000, mode="Cash", kind="Advance")
			out = bq.post_to_folio(fn_held)
			folio = frappe.get_doc("Folio", out["folio"])
			# Security Deposit MUST NOT count in payments_total or reduce balance.
			self.assertAlmostEqual(float(folio.payments_total), 200000.0, 2)
			self.assertAlmostEqual(float(folio.balance), 300000.0, 2)
			self.assertTrue(ledger.folio_reconciliation(ug_prop)["in_balance"])
			# Settle remaining 300,000 and close while 100,000 Security Deposit is still held.
			bq.record_receipt(fn_held, 300000, mode="Cash", kind="Payment")
			folio = frappe.get_doc("Folio", out["folio"])
			self.assertAlmostEqual(float(folio.payments_total), 500000.0, 2)
			self.assertAlmostEqual(float(folio.balance), 0.0, 2)
			close_folio(folio.name)
			fs0 = bq.function_sheet(fn_held)
			self.assertEqual((fs0["deposit_held"], fs0["deposit_refunded"]), (100000.0, 0.0))

			# Partial native refund (40,000) then final native refund (60,000) on the closed event folio.
			folio = frappe.get_doc("Folio", out["folio"])
			sec_pay = next(p.name for p in folio.payments if p.payment_kind == "Security Deposit")
			folio.append("payments", {
				"posting_date": nowdate(), "payment_kind": "Refund", "mode": "Cash",
				"amount": -40000.0, "currency": "UGX", "reference": "PE-REF-PARTIAL",
				"refund_of": sec_pay,
			})
			_recalculate(folio)
			folio.save(ignore_permissions=True)
			write_ledger_entry(
				property=ug_prop, folio=folio.name, ledger="Deposit", transaction_code="9004",
				debit=40000.0, credit=0.0, description="Refund Cash",
				source_doctype="Folio Payment", source_name=folio.payments[-1].name,
			)
			self.assertAlmostEqual(float(folio.payments_total), 500000.0, 2)
			self.assertAlmostEqual(float(folio.balance), 0.0, 2)
			fs1 = bq.function_sheet(fn_held)
			self.assertEqual((fs1["deposit_held"], fs1["deposit_refunded"]), (60000.0, 40000.0))
			native_refunds = [r for r in fs1["receipts"] if r.get("kind") == "Refund" and r.get("native")]
			self.assertEqual([(r["amount"], r["reference"]) for r in native_refunds], [(40000.0, "PE-REF-PARTIAL")])

			folio = frappe.get_doc("Folio", out["folio"])
			folio.append("payments", {
				"posting_date": nowdate(), "payment_kind": "Refund", "mode": "Cash",
				"amount": -60000.0, "currency": "UGX", "reference": "PE-REF-FINAL",
				"refund_of": sec_pay,
			})
			_recalculate(folio)
			folio.save(ignore_permissions=True)
			write_ledger_entry(
				property=ug_prop, folio=folio.name, ledger="Deposit", transaction_code="9004",
				debit=60000.0, credit=0.0, description="Refund Cash",
				source_doctype="Folio Payment", source_name=folio.payments[-1].name,
			)
			self.assertAlmostEqual(float(folio.payments_total), 500000.0, 2)
			self.assertAlmostEqual(float(folio.balance), 0.0, 2)
			fs2 = bq.function_sheet(fn_held)
			self.assertEqual((fs2["deposit_held"], fs2["deposit_refunded"]), (0.0, 100000.0))
			native_refunds2 = [r for r in fs2["receipts"] if r.get("kind") == "Refund" and r.get("native")]
			self.assertEqual(len(native_refunds2), 2)
			self.assertTrue(ledger.folio_reconciliation(ug_prop)["in_balance"])

		# Chain B: collect -> cancel Receive PE (reversal of 9003) -> deposit_refunded MUST stay 0.
		fn_cancel = bq.create_enquiry(
			ug_prop, ug_hall, add_days(nowdate(), 31), "Chain Cancel Client",
			event_type="Conference", attendees=40, with_venue_line=1,
		)["function"]
		bq.set_status(fn_cancel, "Confirmed")
		with accounting_bridge():
			bq.record_receipt(fn_cancel, 100000, mode="Cash", kind="Security Deposit")
			folio_c = frappe.get_doc("Folio", sheet(fn_cancel).folio)
			orig_entry = frappe.db.get_value(
				"Folio Ledger Entry",
				{"folio": folio_c.name, "ledger": "Deposit", "transaction_code": "9003"},
				"name",
			)
			folio_c.append("payments", {
				"posting_date": nowdate(), "payment_kind": "Refund", "mode": "Cash",
				"amount": -100000.0, "currency": "UGX", "reference": "PE-DEP-CANCEL",
			})
			_recalculate(folio_c)
			folio_c.save(ignore_permissions=True)
			write_ledger_entry(
				property=ug_prop, folio=folio_c.name, ledger="Deposit", transaction_code="9004",
				debit=100000.0, credit=0.0, description="Refund Cash",
				reversal_of=orig_entry,
				source_doctype="Folio Payment", source_name=folio_c.payments[-1].name,
			)
			fsc = bq.function_sheet(fn_cancel)
			self.assertEqual((fsc["deposit_held"], fsc["deposit_refunded"]), (0.0, 0.0))
			self.assertAlmostEqual(float(folio_c.payments_total), 0.0, 2)

	def test_folio_history_includes_and_searches_sales_invoice_when_column_exists(self):
		"""Hydrogen P2: FolioHistory surfaces ERP sales_invoice instead of internal INV-... on bridge sites."""
		from kamra import ledger
		from kamra.folio import close_folio

		fn = confirmed(self.f)
		mapping("Venue Rental", revenue_code("UT-BQ-HALL"))
		mapping("Menu", revenue_code("UT-BQ-FOOD", charge_type="Food & Beverage"))
		with accounting_bridge():
			out = bq.post_to_folio(fn)
			folio_doc = frappe.get_doc("Folio", out["folio"])
			bq.record_receipt(fn, float(folio_doc.balance), mode="Cash", kind="Payment")
			close_folio(out["folio"])
		real_get_all = frappe.get_all

		def fake_get_all(doctype, *args, **kwargs):
			if doctype == "Folio":
				fields = list(kwargs.get("fields") or [])
				has_si = "sales_invoice" in fields
				kwargs["fields"] = [f for f in fields if f != "sales_invoice"]
				rows = real_get_all(doctype, *args, **kwargs)
				if has_si:
					for row in rows:
						row["sales_invoice"] = "ACC-SINV-2026-0099" if row.name == out["folio"] else None
				return rows
			return real_get_all(doctype, *args, **kwargs)

		with patch.object(frappe.db, "has_column", side_effect=lambda dt, col: True if (dt, col) == ("Folio", "sales_invoice") else False), \
				patch.object(frappe, "get_all", side_effect=fake_get_all):
			hist = ledger.folio_history(PROPERTY, query="ACC-SINV-2026-0099")["folios"]
		self.assertEqual([r.name for r in hist], [out["folio"]])
		self.assertEqual(hist[0].get("sales_invoice"), "ACC-SINV-2026-0099")

	# --- without the bridge nothing changes ---------------------------------------------------------------

	def test_standalone_kamra_keeps_receipts_on_the_function_and_its_own_invoice(self):
		fn = confirmed(self.f)
		bq.record_receipt(fn, 5000, mode="Cash", kind="Advance")
		doc = sheet(fn)
		self.assertFalse(doc.folio)
		self.assertEqual([(r.kind, r.amount) for r in doc.receipts], [("Advance", 5000)])
		self.assertTrue(bq.generate_invoice(fn)["header"]["number"])
		self.assertEqual(bq.function_sheet(fn)["fiscal_bridge"], 0)

	# --- Hydrogen R1 merge-gate regression tests (Findings 4 & 6) --------------------------------------

	def test_close_out_refuses_post_close_damage_on_settled_event_folio(self):
		"""Hydrogen R1 #4: once the Event Folio is Closed, close_out refuses adding damage or mutating totals."""
		from kamra.folio import close_folio

		fn = confirmed(self.f)
		mapping("Venue Rental", revenue_code("UT-BQ-HALL"))
		mapping("Menu", revenue_code("UT-BQ-FOOD", charge_type="Food & Beverage"))
		with accounting_bridge():
			bq.record_receipt(fn, 200000, mode="Cash", kind="Security Deposit")
			out = bq.post_to_folio(fn)
			folio_doc = frappe.get_doc("Folio", out["folio"])
			bq.record_receipt(fn, float(folio_doc.balance), mode="Cash", kind="Payment")
			close_folio(out["folio"])
			before = sheet(fn)
			before_total = float(before.grand_total)
			before_items = len(before.items)

			with self.assertRaisesRegex(frappe.ValidationError, "closed"):
				bq.close_out(fn, damage_amount=50000, damage_note="Broken projector", refund_deposit=0)

			after_refusal = sheet(fn)
			self.assertEqual(float(after_refusal.damage_amount or 0), 0.0)
			self.assertAlmostEqual(float(after_refusal.grand_total), before_total, 2)
			self.assertEqual(len(after_refusal.items), before_items)
			self.assertNotEqual(after_refusal.status, "Completed")

			# Completing without post-close damage or total changes remains allowed.
			done = bq.close_out(fn, damage_amount=0, refund_deposit=0)
			self.assertEqual(done["status"], "Completed")
			self.assertAlmostEqual(float(done["grand_total"]), before_total, 2)

	def test_kwd_three_decimal_event_deposit_summary_preserves_precision(self):
		"""Hydrogen R1 #6: event_folio_deposit_summary uses property currency precision (3dp KWD, never round(..., 2))."""
		from kamra.ledger import write_ledger_entry

		if not frappe.db.exists("Currency", "KWD"):
			frappe.get_doc({
				"doctype": "Currency", "currency_name": "KWD", "symbol": "د.ك",
				"fraction": "Fils", "fraction_units": 1000,
				"smallest_currency_fraction_value": 0.001,
				"number_format": "#,###.###", "enabled": 1,
			}).insert(ignore_permissions=True)
		else:
			frappe.db.set_value("Currency", "KWD", "enabled", 1)

		kw_prop = _upsert("Property", {"property_name": "UT Kuwait 3dp Hotel"}, {
			"property_name": "UT Kuwait 3dp Hotel", "country": "Kuwait", "currency": "KWD",
			"gst_rate_low": 0, "gst_rate_high": 0, "business_date": nowdate(),
		})
		kw_hall = _upsert("Venue", {"property": kw_prop, "venue_name": "UT Kuwait Hall"}, {
			"property": kw_prop, "venue_name": "UT Kuwait Hall", "venue_type": "Banquet Hall",
			"capacity": 80, "base_price": 150.0, "gst_rate": 0,
		})
		fn = bq.create_enquiry(
			kw_prop, kw_hall, add_days(nowdate(), 35), "Kuwait Client",
			event_type="Conference", attendees=30, with_venue_line=1,
		)["function"]
		bq.set_status(fn, "Confirmed")
		with accounting_bridge():
			bq.record_receipt(fn, 50.125, mode="Cash", kind="Security Deposit")
			summary = bq.event_folio_deposit_summary(sheet(fn))
			self.assertEqual(summary["deposit_held"], 50.125)

			# Unmirrored native refund of 10.125 KWD must keep 3dp in deposit_refunded, deposit_held, and native_receipts.
			folio = frappe.get_doc("Folio", sheet(fn).folio)
			sec_row = folio.payments[0].name
			folio.append("payments", {
				"posting_date": nowdate(), "payment_kind": "Refund", "mode": "Cash",
				"amount": -10.125, "currency": "KWD", "reference": "PE-KW-REF-1",
				"refund_of": sec_row,
			})
			folio.save(ignore_permissions=True)
			write_ledger_entry(
				property=kw_prop, folio=folio.name, ledger="Deposit", transaction_code="9004",
				debit=10.125, credit=0.0, description="Refund Cash",
				source_doctype="Folio Payment", source_name=folio.payments[-1].name,
			)
			summary2 = bq.event_folio_deposit_summary(sheet(fn))
			self.assertEqual(summary2["deposit_held"], 40.0)
			self.assertEqual(summary2["deposit_refunded"], 10.125)
			self.assertEqual(summary2["native_receipts"][0]["amount"], 10.125)


def revenue_code(code, charge_type="Misc", **extra):
	if not frappe.db.exists("Transaction Code", code):
		frappe.get_doc({"doctype": "Transaction Code", "code": code, "description": code,
		                "txn_type": "Revenue", "charge_type": charge_type, **extra}).insert(ignore_permissions=True)
	return code


def mapping(revenue_type, code, **extra):
	return frappe.get_doc({"doctype": "Banquet Revenue Mapping", "property": PROPERTY,
	                       "revenue_type": revenue_type, "transaction_code": code, **extra}).insert().name


class TestRevenueTypeMapping(BanquetFiscalCase):
	"""OPERA Revenue Type Mapping: the hotel decides which transaction code each revenue type posts to."""

	def setUp(self):
		super().setUp()
		frappe.db.delete("Banquet Revenue Mapping", {"property": PROPERTY})
		frappe.db.delete("Property Setting", {"property": PROPERTY, "setting_key": "banquet_default_transaction_code"})

	def posted(self, fn):
		folio = frappe.get_doc({"doctype": "Folio", "property": PROPERTY, "folio_type": "Account",
		                        "guest_name": "Mapping test", "status": "Open"}).insert(ignore_permissions=True)
		bq.post_to_folio(fn, folio.name)
		return {c.description.split(" · ", 1)[1].split(" (")[0]: (c.charge_type, c.transaction_code)
		        for c in frappe.get_doc("Folio", folio.name).charges}

	def test_mapped_revenue_types_post_to_their_transaction_codes(self):
		fn = confirmed(self.f)
		bq.add_service(fn, self.f["led"])
		mapping("Audio Visual", revenue_code("UT-BQ-AV"))
		mapping("Venue Rental", revenue_code("UT-BQ-HALL"))
		mapping("Menu", revenue_code("UT-BQ-FOOD", charge_type="Food & Beverage"))
		lines = self.posted(fn)
		self.assertEqual(lines["Test LED wall"], ("Misc", "UT-BQ-AV"))
		self.assertEqual(lines["Test Hall rental"], ("Misc", "UT-BQ-HALL"))

	def test_the_most_specific_mapping_wins(self):
		fn = confirmed(self.f, event_type="Conference")
		mapping("Menu", revenue_code("UT-BQ-FOOD", charge_type="Food & Beverage"))
		mapping("Venue Rental", revenue_code("UT-BQ-HALL"))
		mapping("Venue Rental", revenue_code("UT-BQ-CONF"), event_type="Conference")
		mapping("Venue Rental", revenue_code("UT-BQ-THIS-HALL"), venue=self.f["hall"])
		mapping("Venue Rental", revenue_code("UT-BQ-THIS-CONF"), venue=self.f["hall"], event_type="Conference")
		self.assertEqual(self.posted(fn)["Test Hall rental"], ("Misc", "UT-BQ-THIS-CONF"))

	def test_a_mapped_code_keeps_its_own_charge_type(self):
		fn = confirmed(self.f)
		mapping("Venue Rental", revenue_code("UT-BQ-HALL"))
		mapping("Menu", revenue_code("UT-BQ-FOOD", charge_type="Food & Beverage"))
		self.assertEqual(self.posted(fn)["Test Buffet"], ("Food & Beverage", "UT-BQ-FOOD"))

	def test_unmapped_revenue_types_are_refused_and_post_nothing(self):
		"""Hydrogen P1-4f (probe10): unmapped revenue lines never silently default to Misc/8000."""
		fn = confirmed(self.f)
		bq.add_service(fn, self.f["led"])
		mapping("Venue Rental", revenue_code("UT-BQ-HALL"))
		mapping("Menu", revenue_code("UT-BQ-FOOD", charge_type="Food & Beverage"))
		folio = frappe.get_doc({"doctype": "Folio", "property": PROPERTY, "folio_type": "Account",
		                        "guest_name": "Unmapped test", "status": "Open"}).insert(ignore_permissions=True)
		with self.assertRaisesRegex(frappe.ValidationError, "Audio Visual"):
			bq.post_to_folio(fn, folio.name)
		self.assertFalse(frappe.get_doc("Folio", folio.name).charges)
		self.assertFalse(sheet(fn).posted_to_folio)

	def test_explicit_banquet_default_transaction_code_setting_maps_unmapped_types(self):
		"""Hydrogen P1-4f: an explicit configured default code maps unmapped types without silent Misc/8000."""
		from kamra.settings import save_settings

		fn = confirmed(self.f)
		bq.add_service(fn, self.f["led"])
		fallback = revenue_code("UT-BQ-FALLBACK")
		save_settings(PROPERTY, "events", {"banquet_default_transaction_code": fallback})
		mapping("Venue Rental", revenue_code("UT-BQ-HALL"))
		mapping("Menu", revenue_code("UT-BQ-FOOD", charge_type="Food & Beverage"))
		lines = self.posted(fn)
		self.assertEqual(lines["Test LED wall"], ("Misc", fallback))

	def test_two_equally_specific_mappings_refuse_and_post_nothing(self):
		fn = confirmed(self.f)
		mapping("Venue Rental", revenue_code("UT-BQ-HALL"))
		mapping("Venue Rental", revenue_code("UT-BQ-HALL-2"))
		with self.assertRaisesRegex(frappe.ValidationError, "Venue Rental"):
			self.posted(fn)
		self.assertFalse(sheet(fn).posted_to_folio)

	def test_only_a_revenue_code_can_be_mapped(self):
		code = revenue_code("UT-BQ-PAY")
		frappe.db.set_value("Transaction Code", code, "txn_type", "Payment")
		with self.assertRaises(frappe.ValidationError):
			mapping("Decor", code)

	def test_the_revenue_types_are_the_function_line_types(self):
		def options(doctype, fieldname):
			return frappe.get_meta(doctype).get_field(fieldname).options.split("\n")
		self.assertEqual(options("Banquet Revenue Mapping", "revenue_type"),
		                 options("Banquet Function Item", "item_type"))
		self.assertEqual([o for o in options("Banquet Revenue Mapping", "event_type") if o],
		                 options("Venue Booking", "event_type"))


def venue(name, sections=()):
	return _upsert("Venue", {"property": PROPERTY, "venue_name": name}, {
		"property": PROPERTY, "venue_name": name, "venue_type": "Banquet Hall", "capacity": 100,
		"base_price": 1000, "sections": [{"section": s} for s in sections]})


class TestDivisibleHalls(BanquetFiscalCase):
	"""A combination space lists what it is made of. Selling it blocks those spaces and the reverse."""

	def setUp(self):
		super().setUp()
		self.east = venue("UT Ballroom East")
		self.west = venue("UT Ballroom West")
		self.whole = venue("UT Ballroom", sections=("UT Ballroom East", "UT Ballroom West"))

	def sell(self, hall, day, who="Customer"):
		return bq.set_status(enquiry(self.f, venue=hall, event_date=day, customer_name=who), "Confirmed")

	def test_the_parent_hall_is_blocked_by_a_child_booking(self):
		day = add_days(nowdate(), 400)
		self.sell(self.east, day)
		with self.assertRaises(frappe.ValidationError):
			self.sell(self.whole, day, "Big wedding")

	def test_a_child_hall_is_blocked_by_the_parent_booking(self):
		day = add_days(nowdate(), 401)
		self.sell(self.whole, day)
		with self.assertRaises(frappe.ValidationError):
			self.sell(self.west, day, "Small party")

	def test_the_children_still_sell_side_by_side(self):
		day = add_days(nowdate(), 402)
		self.sell(self.east, day)
		self.sell(self.west, day, "Other half")
		self.assertEqual(frappe.db.count("Venue Booking", {"venue": ("in", [self.east, self.west]),
		                                                   "event_date": day, "status": "Confirmed"}), 2)

	def test_sections_match_whatever_their_case_and_spacing(self):
		half = venue("UT Terrace Half", sections=("Terrace  north",))
		whole = venue("UT Terrace", sections=(" terrace North", "Terrace South"))
		day = add_days(nowdate(), 403)
		self.sell(half, day)
		with self.assertRaises(frappe.ValidationError):
			self.sell(whole, day, "Whole terrace")

	def test_availability_shows_the_parent_taken_by_a_child(self):
		day = add_days(nowdate(), 404)
		self.sell(self.east, day)
		halls = {v["name"]: v for v in bq.venue_availability(PROPERTY, day)["venues"]}
		self.assertFalse(halls[self.whole]["available"])
		self.assertTrue(halls[self.west]["available"])
