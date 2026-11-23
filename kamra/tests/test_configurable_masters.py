"""Client-configurable masters (PMS parity PR-5).

OPERA lets a hotel keep its own Market codes, Origin codes, Cancellation
Reasons, Payment Methods and Package (board) codes. Each taxonomy that used
to be a fixed Select is a Link to a small master, seeded with today's
values so nothing breaks. Seeded codes the PMS itself writes (OTA, Walk-in,
Other, Cash...) are system codes: their description can change, but they
cannot be deleted or switched off. A hotel adds its own codes beside them,
and the desk, the till and check-in use them like any other.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from kamra import api, checkin, localization
from kamra.tests.audit_fixtures import (
	PROP_A,
	PROP_B,
	TODAY,
	YESTERDAY,
	AuditTestCase,
	d,
	make_property,
	make_reservation,
	make_room_type,
)

LINKS = {
	("Reservation", "market_segment"): "Market Code",
	("Reservation", "source"): "Origin Code",
	("Reservation", "booking_type"): "Booking Type",
	("Reservation", "booker_relation"): "Booker Relation",
	("Reservation", "cancellation_reason"): "Cancellation Reason",
	("Folio Payment", "mode"): "Payment Method",
	("Cashier Transaction", "mode"): "Payment Method",
	("Meal Plan", "code"): "Meal Plan Code",
}

# today's Select options and check-in constants: every one must still work
SEEDS = {
	"Market Code": ["Corporate", "Leisure", "Group", "MICE", "Government", "Wedding",
	                "Long Stay", "Airline Crew", "Complimentary", "House Use"],
	"Origin Code": ["Manual", "AI Agent", "Phone", "Walk-in", "OTA", "Website", "PMS"],
	"Booking Type": ["Individual", "Group", "Corporate"],
	"Booker Relation": ["Assistant", "Family", "Company Travel Desk", "Travel Agent", "Other"],
	"Cancellation Reason": ["Guest request", "Change of plans", "Duplicate booking",
	                        "Payment failed", "Weather / travel disruption",
	                        "Booked elsewhere", "Other"],
	"Payment Method": ["Cash", "Foreign Cash", "Card", "UPI", "Bank Transfer", "Mobile Money",
	                   "OTA Prepaid", "Company Credit", "Payment Link", "Cheque", "Other"],
	"Meal Plan Code": ["EP", "CP", "MAP", "AP"],
	"Payer Type": ["OTA prepaid", "OTA, guest pays here", "Guest", "Company", "Group"],
	"Check-in Request": ["extra_bed", "wake_up_call", "airport_drop", "dietary",
	                     "special_occasion"],
}


def add_code(doctype: str, code: str, **fields) -> str:
	return frappe.get_doc({"doctype": doctype, "code": code, "description": code,
	                       **fields}).insert(ignore_permissions=True).name


class ConfigurableMastersTestCase(AuditTestCase):
	properties = (PROP_A, PROP_B)

	def setUp(self):
		super().setUp()
		make_property(PROP_A, country="Uganda", currency="UGX", business_date=YESTERDAY)
		self.rt = make_room_type(PROP_A)

	def booking(self, **fields):
		return make_reservation(PROP_A, self.rt, d(1), d(2), **fields)

	# ── schema and seeds ────────────────────────────────────────────────

	def test_each_taxonomy_is_a_link_to_its_master(self):
		for (doctype, fieldname), master in LINKS.items():
			with self.subTest(field=f"{doctype}.{fieldname}"):
				df = frappe.get_meta(doctype).get_field(fieldname)
				self.assertEqual(df.fieldtype, "Link")
				self.assertEqual(df.options, master)

	def test_todays_values_are_seeded(self):
		for master, codes in SEEDS.items():
			for code in codes:
				with self.subTest(master=master, code=code):
					self.assertTrue(frappe.db.exists(master, code))

	def test_a_payment_method_maps_to_an_erp_mode_of_payment(self):
		meta = frappe.get_meta("Payment Method")
		self.assertTrue(meta.has_field("mode_of_payment"))
		self.assertEqual(frappe.db.get_value("Payment Method", "Mobile Money", "tender_type"),
		                 "Instant")

	# ── system codes stay, client codes come and go ─────────────────────

	def test_a_system_code_cannot_be_deleted_or_switched_off(self):
		self.assertTrue(frappe.db.get_value("Origin Code", "OTA", "system"))
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Origin Code", "OTA", ignore_permissions=True)
		doc = frappe.get_doc("Origin Code", "OTA")
		doc.disabled = 1
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)
		# its wording is the hotel's to change
		doc.reload()
		doc.description = "Online travel agency"
		doc.save(ignore_permissions=True)

	def test_a_client_code_can_be_deleted(self):
		add_code("Market Code", "UT Safari Operator")
		frappe.delete_doc("Market Code", "UT Safari Operator", ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Market Code", "UT Safari Operator"))

	# ── reservations ────────────────────────────────────────────────────

	def test_a_client_market_code_books(self):
		add_code("Market Code", "UT Safari Operator")
		res = self.booking(market_segment="UT Safari Operator")
		self.assertEqual(frappe.db.get_value("Reservation", res.name, "market_segment"),
		                 "UT Safari Operator")

	def test_an_unknown_market_code_is_refused(self):
		with self.assertRaises(frappe.LinkValidationError):
			self.booking(market_segment="UT No Such Segment")

	def test_a_client_cancellation_reason_is_recorded(self):
		add_code("Cancellation Reason", "UT Visa refused")
		res = self.booking()
		api._do_cancel(res, reason="UT Visa refused", waive_fee=1)
		self.assertEqual(frappe.db.get_value("Reservation", res.name, "cancellation_reason"),
		                 "UT Visa refused")

	def test_a_switched_off_cancellation_reason_falls_back_to_other(self):
		add_code("Cancellation Reason", "UT Retired reason", disabled=1)
		res = self.booking()
		api._do_cancel(res, reason="UT Retired reason", waive_fee=1)
		self.assertEqual(frappe.db.get_value("Reservation", res.name, "cancellation_reason"),
		                 "Other")

	def test_the_desk_vocabulary_lists_the_masters(self):
		add_code("Market Code", "UT Safari Operator", description="Safari operators")
		add_code("Cancellation Reason", "UT Visa refused")
		add_code("Booker Relation", "UT Driver")
		loc = api.property_locale(PROP_A)
		self.assertIn({"value": "UT Safari Operator", "label": "Safari operators"},
		              loc["market_codes"])
		self.assertIn("Corporate", [c["value"] for c in loc["market_codes"]])
		self.assertIn("UT Visa refused", [c["value"] for c in loc["cancellation_reasons"]])
		self.assertIn("UT Driver", [c["value"] for c in loc["booker_relations"]])
		add_code("Market Code", "UT Off segment", disabled=1)
		loc = api.property_locale(PROP_A)
		self.assertNotIn("UT Off segment", [c["value"] for c in loc["market_codes"]])

	# ── meal plans (board basis / package codes) ────────────────────────

	def test_a_client_meal_plan_code_can_be_sold(self):
		add_code("Meal Plan Code", "UT-BB", description="Bed and breakfast")
		mp = frappe.get_doc({"doctype": "Meal Plan", "property": PROP_A, "code": "UT-BB",
		                     "price_per_adult": 25000}).insert(ignore_permissions=True)
		self.assertEqual(mp.code, "UT-BB")

	# ── payment methods: desk, till and folio ───────────────────────────

	def test_a_client_payment_method_is_offered_validated_and_counted(self):
		from kamra import cashier
		add_code("Payment Method", "UT Airtel Money", property=PROP_A, tender_type="Instant",
		         mode_of_payment="Airtel Money")
		pack = localization.pack_for(PROP_A)
		self.assertIn("UT Airtel Money", localization.payment_modes(pack, PROP_A))
		self.assertIn("UT Airtel Money", localization.instant_payment_modes(pack, PROP_A))
		localization.assert_payment_mode(PROP_A, "UT Airtel Money")
		self.at(TODAY)
		session = cashier.open_session(PROP_A, opening_float=0)["name"]
		cashier.record_cashier_txn(PROP_A, "Payment", "UT Airtel Money", 30000, session=session)
		totals = cashier._recompute_session_totals(session, persist=False)
		self.assertEqual(totals["upi"], 30000)
		self.assertEqual(totals["other"], 0)

	def test_a_switched_off_payment_method_is_refused(self):
		add_code("Payment Method", "UT Old Wallet", property=PROP_A, tender_type="Instant", disabled=1)
		pack = localization.pack_for(PROP_A)
		self.assertNotIn("UT Old Wallet", localization.payment_modes(pack, PROP_A))
		with self.assertRaises(frappe.ValidationError):
			localization.assert_payment_mode(PROP_A, "UT Old Wallet")

	def test_payment_methods_are_scoped_to_their_property_and_never_leak(self):
		from kamra.localization import germany, nicaragua, philippines

		meta = frappe.get_meta("Payment Method")
		df = meta.get_field("property")
		self.assertIsNotNone(df)
		self.assertEqual(df.fieldtype, "Link")
		self.assertEqual(df.options, "Property")

		make_property(PROP_B, country="Uganda", currency="UGX", business_date=YESTERDAY)
		add_code("Payment Method", "UT Property A Wallet", property=PROP_A,
		         tender_type="Instant", mode_of_payment="Airtel Money")

		pack_a = localization.pack_for(PROP_A)
		pack_b = localization.pack_for(PROP_B)
		self.assertIn("UT Property A Wallet", localization.payment_modes(pack_a, PROP_A))
		self.assertIn("UT Property A Wallet", localization.instant_payment_modes(pack_a, PROP_A))
		self.assertIn("UT Property A Wallet", api.property_locale(PROP_A)["payment_modes"])

		self.assertNotIn("UT Property A Wallet", localization.payment_modes(pack_b, PROP_B))
		self.assertNotIn("UT Property A Wallet", localization.instant_payment_modes(pack_b, PROP_B))
		self.assertNotIn("UT Property A Wallet", api.property_locale(PROP_B)["payment_modes"])
		with self.assertRaises(frappe.ValidationError):
			localization.assert_payment_mode(PROP_B, "UT Property A Wallet")

		# pack-only calls without a property never pick up any property's custom payment methods
		for pack in (nicaragua, germany, philippines):
			with self.subTest(pack=pack.__name__):
				self.assertEqual(localization.payment_modes(pack), ["Cash", "Card", "Bank Transfer"])

	def test_adopted_payment_method_is_scoped_to_the_property_that_used_it(self):
		from kamra.folio import open_folio
		from kamra.localization import nicaragua
		from kamra.patches.v48 import configurable_masters

		make_property(PROP_B, country="Uganda", currency="UGX", business_date=YESTERDAY)
		res = self.booking()
		folio_name = open_folio(res)
		folio_doc = frappe.get_doc("Folio", folio_name)
		folio_doc.append("payments", {
			"posting_date": YESTERDAY, "payment_kind": "Payment",
			"mode": "Cash", "amount": 10000, "currency": "UGX",
		})
		folio_doc.save(ignore_permissions=True)
		row_name = folio_doc.payments[-1].name
		frappe.db.set_value("Folio Payment", row_name, "mode", "UT Legacy Voucher A",
		                    update_modified=False)
		frappe.db.delete("Payment Method", {"name": "UT Legacy Voucher A"})

		configurable_masters.execute()
		row = frappe.db.get_value(
			"Payment Method", "UT Legacy Voucher A",
			["property", "system", "disabled"], as_dict=True,
		)
		self.assertIsNotNone(row)
		self.assertEqual(row.property, PROP_A)
		self.assertEqual(int(row.system), 0)
		self.assertEqual(int(row.disabled), 0)

		self.assertIn("UT Legacy Voucher A", localization.payment_modes(localization.pack_for(PROP_A), PROP_A))
		self.assertNotIn("UT Legacy Voucher A", localization.payment_modes(localization.pack_for(PROP_B), PROP_B))
		self.assertEqual(localization.payment_modes(nicaragua), ["Cash", "Card", "Bank Transfer"])

	def test_assert_payment_mode_uses_tender_type_for_foreign_currency(self):
		add_code("Payment Method", "UT USD Wire", property=PROP_A,
		         tender_type="Bank Transfer", mode_of_payment="USD Wire")
		add_code("Payment Method", "UT USD Box", property=PROP_A,
		         tender_type="Foreign Cash", mode_of_payment="USD Cash Box")
		add_code("Payment Method", "UT Airtel FX", property=PROP_A,
		         tender_type="Instant", mode_of_payment="Airtel Money")
		mapped = [
			{"mode": "UT USD Wire", "currencies": ["UGX", "USD"]},
			{"mode": "UT USD Box", "currencies": ["UGX", "USD"]},
			{"mode": "UT Airtel FX", "currencies": ["UGX", "USD"]},
		]
		with patch.object(localization, "tender_modes", return_value=mapped):
			localization.assert_payment_mode(PROP_A, "UT USD Wire", "USD")
			localization.assert_payment_mode(PROP_A, "UT USD Box", "USD")
			localization.assert_payment_mode(PROP_A, "UT Airtel FX", "UGX")
			with self.assertRaises(frappe.ValidationError):
				localization.assert_payment_mode(PROP_A, "UT USD Box", "UGX")
			with self.assertRaises(frappe.ValidationError):
				localization.assert_payment_mode(PROP_A, "UT Airtel FX", "USD")

	def test_bridge_fields_on_payment_method_remain_stable(self):
		meta = frappe.get_meta("Payment Method")
		for fieldname in ("code", "mode_of_payment", "tender_type", "property", "disabled"):
			with self.subTest(fieldname=fieldname):
				self.assertTrue(meta.has_field(fieldname))

	# ── check-in: who pays, quick requests ──────────────────────────────

	def test_a_client_payer_is_offered_and_confirmed(self):
		add_code("Payer Type", "UT Embassy", description="The embassy pays.")
		res = self.booking()
		billing = checkin._billing(res)
		self.assertIn("UT Embassy", billing["options"])
		checkin.confirm_billing(res.name, "UT Embassy")
		res.reload()
		self.assertEqual(checkin._records(res)["billing_check"]["payer"], "UT Embassy")
		with self.assertRaises(frappe.ValidationError):
			checkin.confirm_billing(res.name, "UT No Such Payer")

	def test_a_client_check_in_request_raises_a_ticket(self):
		add_code("Check-in Request", "ut_boat_transfer", description="Boat transfer",
		         team="Concierge", priority="High")
		res = self.booking()
		presets = checkin.checkin_flow(res.name)["request_presets"]
		self.assertIn({"key": "ut_boat_transfer", "label": "Boat transfer", "priced": False},
		              presets)
		out = checkin.log_checkin_requests(res.name, ["ut_boat_transfer"])
		self.assertEqual(len(out["tickets"]), 1)
		ticket = frappe.db.get_value("Service Ticket", out["tickets"][0],
		                             ["subject", "category", "priority"], as_dict=True)
		self.assertTrue(ticket.subject.startswith("Boat transfer"))
		self.assertEqual((ticket.category, ticket.priority), ("Concierge", "High"))

	# ── the migration patch ─────────────────────────────────────────────

	def test_the_patch_keeps_legacy_values_working(self):
		from kamra.patches.v48 import configurable_masters
		res = self.booking()
		# a value the old Select never listed, as an older release left it
		frappe.db.set_value("Reservation", res.name, "market_segment", "UT Legacy Segment",
		                    update_modified=False)
		frappe.db.delete("Market Code", {"name": "UT Legacy Segment"})
		configurable_masters.execute()
		configurable_masters.execute()  # safe to run twice
		self.assertTrue(frappe.db.exists("Market Code", "UT Legacy Segment"))
		self.assertFalse(frappe.db.get_value("Market Code", "UT Legacy Segment", "system"))
		doc = frappe.get_doc("Reservation", res.name)
		doc.save(ignore_permissions=True)
		self.assertEqual(doc.market_segment, "UT Legacy Segment")
