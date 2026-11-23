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
		with accounting_bridge():
			out = bq.post_to_folio(fn)
		doc = sheet(fn)
		folio = frappe.get_doc("Folio", out["folio"])
		self.assertEqual((folio.folio_type, folio.venue_booking, doc.folio, doc.posted_to_folio),
		                 ("Account", fn, folio.name, 1))
		self.assertTrue(folio.charges)

	def test_a_held_deposit_is_refunded_natively_not_by_close_out(self):
		fn = confirmed(self.f)
		with accounting_bridge():
			bq.record_receipt(fn, 10000, mode="Cash", kind="Security Deposit")
			with self.assertRaisesRegex(frappe.ValidationError, "native"):
				bq.close_out(fn, refund_deposit=1)
		self.assertNotEqual(sheet(fn).status, "Completed")

	def test_the_function_sheet_says_money_runs_through_the_event_folio(self):
		fn = confirmed(self.f)
		with accounting_bridge():
			bq.record_receipt(fn, 5000, mode="Cash", kind="Advance")
			out = bq.function_sheet(fn)
		self.assertEqual(out["fiscal_bridge"], 1)
		self.assertEqual(out["event_folio"], sheet(fn).folio)

	# --- without the bridge nothing changes ---------------------------------------------------------------

	def test_standalone_kamra_keeps_receipts_on_the_function_and_its_own_invoice(self):
		fn = confirmed(self.f)
		bq.record_receipt(fn, 5000, mode="Cash", kind="Advance")
		doc = sheet(fn)
		self.assertFalse(doc.folio)
		self.assertEqual([(r.kind, r.amount) for r in doc.receipts], [("Advance", 5000)])
		self.assertTrue(bq.generate_invoice(fn)["header"]["number"])
		self.assertEqual(bq.function_sheet(fn)["fiscal_bridge"], 0)


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
		lines = self.posted(fn)
		self.assertEqual(lines["Test LED wall"], ("Misc", "UT-BQ-AV"))
		self.assertEqual(lines["Test Hall rental"], ("Misc", "UT-BQ-HALL"))

	def test_the_most_specific_mapping_wins(self):
		fn = confirmed(self.f, event_type="Conference")
		mapping("Venue Rental", revenue_code("UT-BQ-HALL"))
		mapping("Venue Rental", revenue_code("UT-BQ-CONF"), event_type="Conference")
		mapping("Venue Rental", revenue_code("UT-BQ-THIS-HALL"), venue=self.f["hall"])
		mapping("Venue Rental", revenue_code("UT-BQ-THIS-CONF"), venue=self.f["hall"], event_type="Conference")
		self.assertEqual(self.posted(fn)["Test Hall rental"], ("Misc", "UT-BQ-THIS-CONF"))

	def test_a_mapped_code_keeps_its_own_charge_type(self):
		fn = confirmed(self.f)
		mapping("Menu", revenue_code("UT-BQ-FOOD", charge_type="Food & Beverage"))
		self.assertEqual(self.posted(fn)["Test Buffet"], ("Food & Beverage", "UT-BQ-FOOD"))

	def test_an_unmapped_revenue_type_keeps_the_default_code(self):
		fn = confirmed(self.f)
		bq.add_service(fn, self.f["led"])
		from kamra.ledger import code_for_charge_type
		self.assertEqual(self.posted(fn)["Test LED wall"], ("Misc", code_for_charge_type("Misc")))

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
