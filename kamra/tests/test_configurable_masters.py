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
	("Meal Plan", "code"): "Meal Plan Code",
}

# Payment Method is keyed per property (property, code); payment rows store
# the CODE text as Data and resolve/validate against (property, code).
PAYMENT_CODE_FIELDS = (
	("Folio Payment", "mode"),
	("Cashier Transaction", "mode"),
	("Banquet Receipt", "mode"),
)

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
		for doctype, fieldname in PAYMENT_CODE_FIELDS:
			with self.subTest(field=f"{doctype}.{fieldname}"):
				df = frappe.get_meta(doctype).get_field(fieldname)
				self.assertEqual(df.fieldtype, "Data")

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
		rt_b = make_room_type(PROP_B)
		res = self.booking()
		folio_name = open_folio(res)
		folio_doc = frappe.get_doc("Folio", folio_name)
		folio_doc.append("payments", {
			"posting_date": YESTERDAY, "payment_kind": "Payment",
			"mode": "Cash", "amount": 10000, "currency": "UGX",
		})
		folio_doc.append("payments", {
			"posting_date": YESTERDAY, "payment_kind": "Payment",
			"mode": "Cash", "amount": 5000, "currency": "UGX",
		})
		folio_doc.save(ignore_permissions=True)
		frappe.db.set_value("Folio Payment", folio_doc.payments[-2].name, "mode", "UT Legacy Voucher A",
		                    update_modified=False)
		frappe.db.set_value("Folio Payment", folio_doc.payments[-1].name, "mode", "UT Legacy Voucher Both",
		                    update_modified=False)

		res_b = make_reservation(PROP_B, rt_b, d(1), d(2))
		folio_b = frappe.get_doc("Folio", open_folio(res_b))
		folio_b.append("payments", {
			"posting_date": YESTERDAY, "payment_kind": "Payment",
			"mode": "Cash", "amount": 5000, "currency": "UGX",
		})
		folio_b.save(ignore_permissions=True)
		frappe.db.set_value("Folio Payment", folio_b.payments[-1].name, "mode", "UT Legacy Voucher Both",
		                    update_modified=False)

		frappe.db.delete("Payment Method", {"code": ("in", ["UT Legacy Voucher A", "UT Legacy Voucher Both"])})

		configurable_masters.execute()
		configurable_masters.execute()  # idempotent
		row = frappe.db.get_value(
			"Payment Method", {"code": "UT Legacy Voucher A", "property": PROP_A},
			["name", "code", "property", "system", "disabled"], as_dict=True,
		)
		self.assertIsNotNone(row)
		self.assertEqual(row.property, PROP_A)
		self.assertEqual(int(row.system), 0)
		self.assertEqual(int(row.disabled), 0)

		row_both_a = frappe.db.get_value(
			"Payment Method", {"code": "UT Legacy Voucher Both", "property": PROP_A},
			["name", "code", "property"], as_dict=True,
		)
		row_both_b = frappe.db.get_value(
			"Payment Method", {"code": "UT Legacy Voucher Both", "property": PROP_B},
			["name", "code", "property"], as_dict=True,
		)
		self.assertIsNotNone(row_both_a)
		self.assertIsNotNone(row_both_b)
		self.assertNotEqual(row_both_a.name, row_both_b.name)

		self.assertIn("UT Legacy Voucher A", localization.payment_modes(localization.pack_for(PROP_A), PROP_A))
		self.assertNotIn("UT Legacy Voucher A", localization.payment_modes(localization.pack_for(PROP_B), PROP_B))
		self.assertEqual(localization.payment_modes(nicaragua), ["Cash", "Card", "Bank Transfer"])

	def test_two_properties_can_define_same_payment_method_code_independently(self):
		from kamra import cashier
		from kamra.folio import open_folio
		from kamra.masters import resolve_payment_method

		make_property(PROP_B, country="Uganda", currency="UGX", business_date=YESTERDAY)
		pm_a = add_code("Payment Method", "UT Airtel Money", property=PROP_A,
		                tender_type="Instant", mode_of_payment="Airtel Money A")
		pm_b = add_code("Payment Method", "UT Airtel Money", property=PROP_B,
		                tender_type="Instant", mode_of_payment="Airtel Money B")
		self.assertNotEqual(pm_a, pm_b)

		# same property + same code is still a duplicate
		with self.assertRaises(frappe.DuplicateEntryError):
			add_code("Payment Method", "UT Airtel Money", property=PROP_A, tender_type="Instant")

		self.assertEqual(resolve_payment_method("UT Airtel Money", PROP_A)["mode_of_payment"], "Airtel Money A")
		self.assertEqual(resolve_payment_method("UT Airtel Money", PROP_B)["mode_of_payment"], "Airtel Money B")

		# disable on PROP_B only -> PROP_A keeps working and stores the CODE text
		frappe.db.set_value("Payment Method", pm_b, "disabled", 1)
		pack_a, pack_b = localization.pack_for(PROP_A), localization.pack_for(PROP_B)
		self.assertIn("UT Airtel Money", localization.payment_modes(pack_a, PROP_A))
		self.assertNotIn("UT Airtel Money", localization.payment_modes(pack_b, PROP_B))
		localization.assert_payment_mode(PROP_A, "UT Airtel Money")
		with self.assertRaises(frappe.ValidationError):
			localization.assert_payment_mode(PROP_B, "UT Airtel Money")

		# folio payment and cashier transaction store the CODE text ("UT Airtel Money")
		res = self.booking()
		folio_name = open_folio(res)
		self.at(TODAY)
		sess = cashier.open_session(PROP_A, opening_float=0)["name"]
		api.add_folio_payment(folio_name, "UT Airtel Money", 25000)
		pay = frappe.get_doc("Folio", folio_name).payments[-1]
		self.assertEqual(pay.mode, "UT Airtel Money")
		txn_mode = frappe.db.get_value("Cashier Transaction", {"session": sess, "folio": folio_name}, "mode")
		self.assertEqual(txn_mode, "UT Airtel Money")

		# direct ORM write with an unknown code is rejected
		folio_doc = frappe.get_doc("Folio", folio_name)
		folio_doc.append("payments", {
			"posting_date": TODAY, "payment_kind": "Payment",
			"mode": "UT No Such Tender", "amount": 1000, "currency": "UGX",
		})
		with self.assertRaises(frappe.ValidationError):
			folio_doc.save(ignore_permissions=True)

	def test_property_payment_method_overrides_shared_row_of_same_code(self):
		from kamra import cashier
		from kamra.masters import payment_tender_type, resolve_payment_method

		make_property(PROP_B, country="Uganda", currency="UGX", business_date=YESTERDAY)
		# Shared system row remains untouched
		self.assertTrue(frappe.db.exists("Payment Method", "Mobile Money"))
		override = add_code("Payment Method", "Mobile Money", property=PROP_A,
		                    tender_type="Instant", mode_of_payment="MTN MoMo A")
		self.assertNotEqual(override, "Mobile Money")
		self.assertEqual(resolve_payment_method("Mobile Money", PROP_A)["mode_of_payment"], "MTN MoMo A")
		self.assertFalse(resolve_payment_method("Mobile Money", PROP_B).get("mode_of_payment"))

		# Property override can disable Mobile Money for PROP_A without affecting PROP_B
		frappe.db.set_value("Payment Method", override, "disabled", 1)
		pack_a, pack_b = localization.pack_for(PROP_A), localization.pack_for(PROP_B)
		self.assertNotIn("Mobile Money", localization.payment_modes(pack_a, PROP_A))
		self.assertNotIn("Mobile Money", localization.instant_payment_modes(pack_a, PROP_A))
		with self.assertRaises(frappe.ValidationError):
			localization.assert_payment_mode(PROP_A, "Mobile Money")
		self.assertIn("Mobile Money", localization.payment_modes(pack_b, PROP_B))
		localization.assert_payment_mode(PROP_B, "Mobile Money")

		# Property override of a shared custom row wins for tender_type and cashier bucketing
		add_code("Payment Method", "UT Shared Wallet", tender_type="Other", mode_of_payment="Shared MoP")
		add_code("Payment Method", "UT Shared Wallet", property=PROP_A,
		         tender_type="Instant", mode_of_payment="Prop A MoP")
		self.assertEqual(payment_tender_type("UT Shared Wallet", PROP_A), "Instant")
		self.assertEqual(payment_tender_type("UT Shared Wallet", PROP_B), "Other")
		self.at(TODAY)
		sess = cashier.open_session(PROP_A, opening_float=0)["name"]
		cashier.record_cashier_txn(PROP_A, "Payment", "UT Shared Wallet", 12000, session=sess)
		totals = cashier._recompute_session_totals(sess, persist=False)
		self.assertEqual(totals["upi"], 12000)

	def test_payment_method_per_property_key_patch_is_idempotent(self):
		from kamra.folio import open_folio
		from kamra.patches.v48 import payment_method_per_property_key

		make_property(PROP_B, country="Uganda", currency="UGX", business_date=YESTERDAY)
		res = self.booking()
		folio_doc = frappe.get_doc("Folio", open_folio(res))
		folio_doc.append("payments", {
			"posting_date": YESTERDAY, "payment_kind": "Payment",
			"mode": "Cash", "amount": 15000, "currency": "UGX",
		})
		folio_doc.save(ignore_permissions=True)
		pay_row = folio_doc.payments[-1].name
		frappe.db.set_value("Folio Payment", pay_row, "mode", "UT Patch Airtel", update_modified=False)

		# Insert a #44-shaped row (PK == code, with property set)
		frappe.db.delete("Payment Method", {"code": "UT Patch Airtel"})
		frappe.db.sql(
			"""INSERT INTO `tabPayment Method`
			   (name, code, description, property, tender_type, mode_of_payment, sort_order, system, disabled,
			    creation, modified, owner, modified_by, docstatus, idx)
			   VALUES (%s, %s, %s, %s, %s, %s, 0, 0, 0, NOW(), NOW(), 'Administrator', 'Administrator', 0, 0)""",
			("UT Patch Airtel", "UT Patch Airtel", "UT Patch Airtel", PROP_A, "Instant", "Airtel Legacy"),
		)

		payment_method_per_property_key.execute()
		payment_method_per_property_key.execute()  # idempotent

		migrated = frappe.db.get_value(
			"Payment Method", {"code": "UT Patch Airtel", "property": PROP_A},
			["name", "code", "property", "mode_of_payment"], as_dict=True,
		)
		self.assertIsNotNone(migrated)
		self.assertEqual(migrated.name, f"{PROP_A}::UT Patch Airtel")
		self.assertEqual(migrated.mode_of_payment, "Airtel Legacy")
		# Folio Payment keeps the CODE text, never rewritten to the composite PK
		self.assertEqual(frappe.db.get_value("Folio Payment", pay_row, "mode"), "UT Patch Airtel")
		# PROP_B can now define the same code independently
		pm_b = add_code("Payment Method", "UT Patch Airtel", property=PROP_B, tender_type="Instant")
		self.assertEqual(pm_b, f"{PROP_B}::UT Patch Airtel")

	def test_standalone_fx_payment_gates_on_tender_type_and_locale_exposes_tender_types(self):
		from kamra import cashier, ledger
		from kamra.folio import open_folio

		make_property(PROP_A, country="Uganda", currency="UGX", business_date=TODAY)
		make_property(PROP_B, country="Uganda", currency="UGX", business_date=TODAY)
		add_code("Payment Method", "UT USD Wire", property=PROP_A,
		         tender_type="Bank Transfer", mode_of_payment="USD Wire")
		add_code("Payment Method", "UT USD Box", property=PROP_A,
		         tender_type="Foreign Cash", mode_of_payment="USD Cash Box")
		add_code("Payment Method", "UT Airtel FX", property=PROP_A,
		         tender_type="Instant", mode_of_payment="Airtel Money")

		loc_a = api.property_locale(PROP_A)
		loc_b = api.property_locale(PROP_B)
		self.assertEqual(loc_a["payment_tender_types"]["UT USD Wire"], "Bank Transfer")
		self.assertEqual(loc_a["payment_tender_types"]["UT USD Box"], "Foreign Cash")
		self.assertEqual(loc_a["payment_tender_types"]["UT Airtel FX"], "Instant")
		for code in ("UT USD Wire", "UT USD Box", "UT Airtel FX"):
			self.assertNotIn(code, loc_b["payment_tender_types"])

		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		ledger.upsert_exchange_rate(PROP_A, "USD", 3700, 3750)
		res = self.booking()
		folio_name = open_folio(res)
		cashier.open_session(PROP_A, opening_float=0)

		with patch.object(localization, "tender_modes", return_value=None):
			localization.assert_payment_mode(PROP_A, "UT USD Wire", "USD")
			localization.assert_payment_mode(PROP_A, "UT USD Box", "USD")
			with self.assertRaises(frappe.ValidationError):
				localization.assert_payment_mode(PROP_A, "UT USD Box", "UGX")
			with self.assertRaises(frappe.ValidationError):
				localization.assert_payment_mode(PROP_A, "UT Airtel FX", "USD")

			api.add_folio_payment(folio_name, "UT USD Wire", 10, currency="USD")
			api.add_folio_payment(folio_name, "UT USD Box", 5, currency="USD")
			with self.assertRaises(frappe.ValidationError):
				api.add_folio_payment(folio_name, "UT Airtel FX", 10, currency="USD")

		payments = frappe.get_doc("Folio", folio_name).payments
		self.assertEqual([(p.mode, p.currency) for p in payments],
		                 [("UT USD Wire", "USD"), ("UT USD Box", "USD")])

	def test_desk_vocabulary_includes_booking_types(self):
		add_code("Booking Type", "UT Staycation", description="Staycation")
		loc = api.property_locale(PROP_A)
		self.assertIn({"value": "UT Staycation", "label": "Staycation"}, loc["booking_types"])
		values = [c["value"] for c in loc["booking_types"]]
		for seeded in ("Individual", "Group", "Corporate"):
			self.assertIn(seeded, values)

	def test_billing_payer_labels_includes_system_payers_and_rewording(self):
		res = self.booking()
		billing = checkin._billing(res)
		for system_payer in ("OTA prepaid", "OTA, guest pays here", "Guest", "Company", "Group"):
			with self.subTest(payer=system_payer):
				self.assertIn(system_payer, billing["payer_labels"])
		doc = frappe.get_doc("Payer Type", "Guest")
		doc.description = "Walk-in Guest"
		doc.save(ignore_permissions=True)
		self.assertEqual(checkin._billing(res)["payer_labels"]["Guest"], "Walk-in Guest")

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

	# ── PR #47 R2: OPERA/SAP cashier, FX drawers, permissions, POS, groups ─

	def test_custom_cash_fx_posts_to_foreign_drawer_not_expected_cash(self):
		from kamra import cashier, ledger
		from kamra.folio import open_folio

		make_property(PROP_A, country="Uganda", currency="UGX", business_date=TODAY)
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		add_code("Payment Method", "UT Front Desk Till", property=PROP_A,
		         tender_type="Cash", mode_of_payment="Front Desk UGX Till")
		add_code("Payment Method", "UT USD Box", property=PROP_A,
		         tender_type="Foreign Cash", mode_of_payment="Front Desk USD Box")
		ledger.upsert_exchange_rate(PROP_A, "USD", 3700, 3700)
		ledger.upsert_exchange_rate(PROP_A, "EUR", 4000, 4000)

		res = self.booking()
		folio_name = open_folio(res)
		sess = cashier.open_session(PROP_A, opening_float=100_000)["name"]

		with patch.object(localization, "tender_modes", return_value=None):
			api.add_folio_payment(folio_name, "UT Front Desk Till", 50_000)
			api.add_folio_payment(folio_name, "UT Front Desk Till", 10, currency="USD")
			api.add_folio_payment(folio_name, "UT USD Box", 5, currency="USD")
			api.add_folio_payment(folio_name, "UT Front Desk Till", 20, currency="EUR")

		totals = cashier._recompute_session_totals(sess, persist=False)
		# Only home UGX cash (50,000) + opening float (100,000) belongs in expected_cash
		self.assertEqual(totals["cash"], 50_000.0)
		self.assertEqual(totals["expected_cash"], 150_000.0)
		# All foreign cash (10 USD + 5 USD = 55,500; 20 EUR = 80,000) stays out of expected_cash
		self.assertEqual(totals["foreign_cash"], 135_500.0)

		# Cashier Transaction stores currency and fx_amount on each FX row
		txns = frappe.get_all(
			"Cashier Transaction",
			filters={"session": sess, "folio": folio_name},
			fields=["mode", "currency", "fx_amount", "amount"],
			order_by="creation asc",
		)
		self.assertEqual(
			[(t.mode, t.currency, float(t.fx_amount or 0), float(t.amount)) for t in txns],
			[
				("UT Front Desk Till", "UGX", 0.0, 50_000.0),
				("UT Front Desk Till", "USD", 10.0, 37_000.0),
				("UT USD Box", "USD", 5.0, 18_500.0),
				("UT Front Desk Till", "EUR", 20.0, 80_000.0),
			],
		)

		# Session report exposes per-currency foreign drawers (OPERA Cashier Closure parity)
		rep = cashier.session_report(sess)
		by_ccy = {d["currency"]: d for d in rep.get("foreign_drawers") or []}
		self.assertEqual(set(by_ccy.keys()), {"EUR", "USD"})
		self.assertEqual(by_ccy["USD"]["expected_fx"], 15.0)
		self.assertEqual(by_ccy["USD"]["home_equivalent"], 55_500.0)
		self.assertEqual(by_ccy["EUR"]["expected_fx"], 20.0)
		self.assertEqual(by_ccy["EUR"]["home_equivalent"], 80_000.0)

		# Closing with per-currency foreign drawer counts computes per-currency variance
		from kamra import settings as kamra_settings
		kamra_settings.write_kv(PROP_A, "till_variance_tolerance", 100)
		kamra_settings.clear_cache(PROP_A)
		closed = cashier.close_session(
			sess, counted_cash=150_000, foreign_counts={"USD": 14.0, "EUR": 20.0},
		)
		closed_by_ccy = {d["currency"]: d for d in closed.get("foreign_drawers") or []}
		self.assertEqual(closed_by_ccy["USD"]["counted_fx"], 14.0)
		self.assertEqual(closed_by_ccy["USD"]["variance_fx"], -1.0)
		self.assertEqual(closed_by_ccy["EUR"]["counted_fx"], 20.0)
		self.assertEqual(closed_by_ccy["EUR"]["variance_fx"], 0.0)
		# Foreign drawer shortage flags session for supervisor variance review
		self.assertEqual(closed["variance_status"], "Needs Review")
		board = cashier.tills_board(PROP_A)
		rev = next((r for r in board["variance_review"] if r["name"] == sess), None)
		self.assertIsNotNone(rev)
		rev_by_ccy = {d["currency"]: d for d in rev.get("foreign_drawers") or []}
		self.assertEqual(rev_by_ccy["USD"]["variance_fx"], -1.0)

	def test_refund_of_foreign_cash_type_keeps_right_tender_and_currency(self):
		from kamra import cashier, ledger
		from kamra.folio import open_folio

		make_property(PROP_A, country="Uganda", currency="UGX", business_date=TODAY)
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		add_code("Payment Method", "UT USD Box", property=PROP_A,
		         tender_type="Foreign Cash", mode_of_payment="Front Desk USD Box")
		ledger.upsert_exchange_rate(PROP_A, "USD", 3700, 3700)

		res = self.booking()
		folio_name = open_folio(res)
		sess = cashier.open_session(PROP_A, opening_float=100_000)["name"]

		with patch.object(localization, "tender_modes", return_value=None):
			api.add_folio_payment(folio_name, "UT USD Box", 10, currency="USD")
			pay = frappe.get_doc("Folio", folio_name).payments[-1]

			# 1. Explicitly refunding a Foreign Cash tender in home currency is refused
			with self.assertRaises(frappe.ValidationError):
				api.refund_folio_payment(
					folio_name, amount=18_500, mode="UT USD Box",
					reason="Invalid home refund on FX box", payment_row=pay.name,
				)

			# 2. Home-currency refund with mode=None defaults to home Cash (never "UT USD Box" in UGX)
			api.refund_folio_payment(
				folio_name, amount=18_500, mode=None,
				reason="Partial refund in home cash", payment_row=pay.name,
			)
			home_ref = frappe.get_doc("Folio", folio_name).payments[-1]
			self.assertEqual(home_ref.mode, "Cash")
			self.assertEqual(home_ref.currency, "UGX")
			self.assertEqual(float(home_ref.fx_amount or 0), 0.0)
			self.assertEqual(float(home_ref.amount), -18_500.0)
			totals_after_home = cashier._recompute_session_totals(sess, persist=False)
			self.assertEqual(totals_after_home["expected_cash"], 81_500.0)
			self.assertEqual(totals_after_home["foreign_cash"], 37_000.0)

			# 3. Foreign-currency refund with currency="USD" and mode=None keeps "UT USD Box" and USD
			api.refund_folio_payment(
				folio_name, amount=5, currency="USD", mode=None,
				reason="Return USD notes", payment_row=pay.name,
			)
			fx_ref = frappe.get_doc("Folio", folio_name).payments[-1]
			self.assertEqual(fx_ref.mode, "UT USD Box")
			self.assertEqual(fx_ref.currency, "USD")
			self.assertEqual(float(fx_ref.fx_amount), -5.0)
			self.assertEqual(float(fx_ref.amount), -18_500.0)

		totals_after_fx = cashier._recompute_session_totals(sess, persist=False)
		self.assertEqual(totals_after_fx["expected_cash"], 81_500.0)
		self.assertEqual(totals_after_fx["foreign_cash"], 18_500.0)
		rep = cashier.session_report(sess)
		usd = next(d for d in rep["foreign_drawers"] if d["currency"] == "USD")
		self.assertEqual(usd["expected_fx"], 5.0)

	def test_reclassify_custom_cash_fx_to_foreign_cash_keeps_drawers_balanced(self):
		from kamra import cashier, ledger
		from kamra.folio import open_folio
		from kamra.tests.audit_fixtures import ensure_user

		make_property(PROP_A, country="Uganda", currency="UGX", business_date=TODAY, require_cashier_pin=0)
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		add_code("Payment Method", "UT Front Desk Till", property=PROP_A,
		         tender_type="Cash", mode_of_payment="Front Desk UGX Till")
		add_code("Payment Method", "UT USD Box", property=PROP_A,
		         tender_type="Foreign Cash", mode_of_payment="Front Desk USD Box")
		ledger.upsert_exchange_rate(PROP_A, "USD", 3700, 3700)

		sup = ensure_user("ut.r2.sup@test.local", ["Finance"])
		if not frappe.db.exists("Cashier PIN", sup):
			frappe.get_doc({"doctype": "Cashier PIN", "user": sup, "pin": "8394"}).insert(ignore_permissions=True)

		res = self.booking()
		folio_name = open_folio(res)
		sess = cashier.open_session(PROP_A, opening_float=0)["name"]
		with patch.object(localization, "tender_modes", return_value=None):
			api.add_folio_payment(folio_name, "UT Front Desk Till", 10, currency="USD")
			row_name = frappe.get_doc("Folio", folio_name).payments[-1].name
			api.correct_folio_payment(
				folio_name, row_name,
				action_type="reclassify_tender",
				reason_code="REV-ACCOUNT",
				reason_note="Move to USD Box",
				idempotency_key="r2-reclass-fx-1",
				destination_mode="UT USD Box",
				supervisor_user=sup,
				supervisor_pin="8394",
			)
		repost = frappe.get_doc("Folio", folio_name).payments[-1]
		self.assertEqual((repost.mode, repost.currency, float(repost.fx_amount), float(repost.amount)),
		                 ("UT USD Box", "USD", 10.0, 37_000.0))
		totals = cashier._recompute_session_totals(sess, persist=False)
		self.assertEqual(totals["expected_cash"], 0.0)
		self.assertEqual(totals["foreign_cash"], 37_000.0)
		rep = cashier.session_report(sess)
		usd = next(d for d in rep["foreign_drawers"] if d["currency"] == "USD")
		self.assertEqual(usd["expected_fx"], 10.0)

	def test_outlet_payment_modes_is_property_scoped_and_pos_accepts_custom_instant(self):
		from kamra import cashier, checkout, pos

		make_property(PROP_A, country="Uganda", currency="UGX", business_date=TODAY, require_cashier_pin=0)
		make_property(PROP_B, country="Uganda", currency="UGX", business_date=TODAY, require_cashier_pin=0)
		self.at(TODAY)
		add_code("Payment Method", "UT Airtel POS", property=PROP_A,
		         tender_type="Instant", mode_of_payment="Airtel Money KLA")
		add_code("Payment Method", "Mobile Money", property=PROP_B,
		         tender_type="Instant", disabled=1)

		pack_a, pack_b = localization.pack_for(PROP_A), localization.pack_for(PROP_B)
		self.assertIn("UT Airtel POS", localization.outlet_payment_modes(pack_a, PROP_A))
		self.assertIn("Mobile Money", localization.outlet_payment_modes(pack_a, PROP_A))
		self.assertNotIn("UT Airtel POS", localization.outlet_payment_modes(pack_b, PROP_B))
		self.assertNotIn("Mobile Money", localization.outlet_payment_modes(pack_b, PROP_B))
		self.assertIn("UT Airtel POS", api.property_locale(PROP_A)["outlet_payment_modes"])
		self.assertNotIn("Mobile Money", api.property_locale(PROP_B)["outlet_payment_modes"])
		self.assertIn("UT Airtel POS", checkout._outlet_modes(PROP_A))
		self.assertNotIn("Mobile Money", checkout._outlet_modes(PROP_B))

		outlet_a = frappe.get_doc({
			"doctype": "POS Outlet", "property": PROP_A,
			"outlet_name": "UT R2 Bistro A", "outlet_type": "Restaurant", "gst_rate": 0,
		}).insert(ignore_permissions=True).name
		outlet_b = frappe.get_doc({
			"doctype": "POS Outlet", "property": PROP_B,
			"outlet_name": "UT R2 Bistro B", "outlet_type": "Restaurant", "gst_rate": 0,
		}).insert(ignore_permissions=True).name
		item_a = frappe.get_doc({
			"doctype": "Menu Item", "property": PROP_A, "outlet": outlet_a,
			"item_name": "UT R2 Coffee", "category": "Beverage", "price": 15_000,
			"is_veg": 1, "available": 1, "prep_station": "Bar",
		}).insert(ignore_permissions=True).name
		item_b = frappe.get_doc({
			"doctype": "Menu Item", "property": PROP_B, "outlet": outlet_b,
			"item_name": "UT R2 Tea", "category": "Beverage", "price": 10_000,
			"is_veg": 1, "available": 1, "prep_station": "Bar",
		}).insert(ignore_permissions=True).name
		order_a = pos.create_order(
			outlet_a, [{"menu_item": item_a, "qty": 1}], order_type="Takeaway",
		)["order"]
		order_b = pos.create_order(
			outlet_b, [{"menu_item": item_b, "qty": 1}], order_type="Takeaway",
		)["order"]

		sess_a = cashier.open_session(PROP_A, opening_float=0)["name"]
		pos.pay_order(order_a, "UT Airtel POS")
		self.assertEqual(frappe.db.get_value("POS Order", order_a, "payment_mode"), "UT Airtel POS")
		self.assertEqual(cashier._recompute_session_totals(sess_a, persist=False)["upi"], 15_000.0)

		with self.assertRaises(frappe.ValidationError):
			pos.pay_order(order_b, "Mobile Money")

	def test_night_audit_check_open_tills_uses_tender_type_and_separates_fx(self):
		from kamra import cashier, ledger, night_audit
		from kamra.folio import open_folio

		make_property(PROP_A, country="Uganda", currency="UGX", business_date=TODAY)
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		add_code("Payment Method", "UT Front Desk Till", property=PROP_A,
		         tender_type="Cash", mode_of_payment="Front Desk UGX Till")
		ledger.upsert_exchange_rate(PROP_A, "USD", 3700, 3700)

		res = self.booking()
		folio_name = open_folio(res)
		sess = cashier.open_session(PROP_A, opening_float=0)["name"]
		with patch.object(localization, "tender_modes", return_value=None):
			api.add_folio_payment(folio_name, "UT Front Desk Till", 40_000)
			api.add_folio_payment(folio_name, "UT Front Desk Till", 10, currency="USD")

		chk = night_audit.check_open_tills(PROP_A, TODAY)
		item = next(i for i in chk["items"] if i["session"] == sess)
		self.assertEqual(item["cash_taken"], 40_000.0)
		usd = next((d for d in item.get("foreign_drawers") or [] if d["currency"] == "USD"), None)
		self.assertIsNotNone(usd)
		self.assertEqual(usd["expected_fx"], 10.0)

	def test_cashier_allowed_modes_enforced_on_server_and_reflected_in_locale(self):
		from kamra import cashier, checkout, pos
		from kamra.folio import open_folio
		from kamra.tests.audit_fixtures import ensure_user

		make_property(PROP_A, country="Uganda", currency="UGX", business_date=TODAY, require_cashier_pin=0)
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		add_code("Payment Method", "UT Airtel Money", property=PROP_A,
		         tender_type="Instant", mode_of_payment="Airtel Money")
		desk_user = ensure_user("ut.r2.cashier@test.local", ["Front Desk"])
		res = self.booking()
		folio_name = open_folio(res)
		outlet = frappe.get_doc({
			"doctype": "POS Outlet", "property": PROP_A,
			"outlet_name": "UT R2 Cafe", "outlet_type": "Restaurant", "gst_rate": 0,
		}).insert(ignore_permissions=True).name
		item = frappe.get_doc({
			"doctype": "Menu Item", "property": PROP_A, "outlet": outlet,
			"item_name": "UT R2 Juice", "category": "Beverage", "price": 12_000,
			"is_veg": 1, "available": 1, "prep_station": "Bar",
		}).insert(ignore_permissions=True).name
		order = pos.create_order(
			outlet, [{"menu_item": item, "qty": 1}], order_type="Takeaway",
		)["order"]

		with self.set_user(desk_user):
			cashier.open_session(PROP_A, opening_float=0)
		csh_name = cashier._cashier_for_user(PROP_A, desk_user)
		csh_doc = frappe.get_doc("Cashier", csh_name)
		# Invalid mode in allowed_modes is refused on validate
		csh_doc.allowed_modes = "Card, UT No Such Mode"
		with self.assertRaises(frappe.ValidationError):
			csh_doc.save(ignore_permissions=True)
		csh_doc.reload()
		csh_doc.allowed_modes = "Card, UT Airtel Money"
		csh_doc.save(ignore_permissions=True)

		with self.set_user(desk_user), patch.object(localization, "tender_modes", return_value=None):
			loc = api.property_locale(PROP_A)
			self.assertEqual(loc["cashier_allowed_modes"], ["Card", "UT Airtel Money"])
			self.assertEqual(loc["payment_modes"], ["Card", "UT Airtel Money"])
			self.assertEqual(loc["outlet_payment_modes"], ["Card", "UT Airtel Money"])
			self.assertEqual(checkout._modes(PROP_A), ["Card", "UT Airtel Money"])
			self.assertEqual(checkout._outlet_modes(PROP_A), ["Card", "UT Airtel Money"])

			with self.assertRaises(frappe.PermissionError):
				api.add_folio_payment(folio_name, "Cash", 10_000)
			with self.assertRaises(frappe.PermissionError):
				pos.pay_order(order, "Cash")

			api.add_folio_payment(folio_name, "Card", 10_000)
			pos.pay_order(order, "UT Airtel Money")

		# Blank allowed_modes restores unrestricted modes
		csh_doc.reload()
		csh_doc.allowed_modes = ""
		csh_doc.save(ignore_permissions=True)
		with self.set_user(desk_user), patch.object(localization, "tender_modes", return_value=None):
			loc = api.property_locale(PROP_A)
			self.assertIsNone(loc["cashier_allowed_modes"])
			self.assertIn("Cash", loc["payment_modes"])
			api.add_folio_payment(folio_name, "Cash", 5_000)

	def test_create_group_booking_honors_booking_type(self):
		add_code("Booking Type", "UT Retreat", description="Wellness Retreat")
		out = api.create_group_booking(
			property=PROP_A,
			group_name="R2 Retreat Group",
			check_in_date=d(5),
			check_out_date=d(7),
			rooms=[{"room_type": self.rt, "count": 2}],
			guest_name="Retreat Lead",
			booking_type="UT Retreat",
		)
		self.assertEqual(len(out["created"]), 2)
		for res_name in out["created"]:
			self.assertEqual(
				frappe.db.get_value("Reservation", res_name, "booking_type"),
				"UT Retreat",
			)

	def test_property_scoped_user_cannot_mutate_shared_payment_method(self):
		from kamra.tests.audit_fixtures import ensure_user

		fin_user = ensure_user("ut.r2.fin@test.local", ["Finance"])
		up = frappe.get_doc({
			"doctype": "User Permission", "user": fin_user,
			"allow": "Property", "for_value": PROP_A, "apply_to_all_doctypes": 1,
		}).insert(ignore_permissions=True).name
		frappe.clear_cache(user=fin_user)
		try:
			with self.set_user(fin_user):
				shared = frappe.get_doc("Payment Method", "Mobile Money")
				shared.description = "Attempted global edit"
				with self.assertRaises(frappe.PermissionError):
					shared.save()

				# Creating a property override for PROP_A succeeds without touching the shared row
				override = frappe.get_doc({
					"doctype": "Payment Method",
					"code": "Mobile Money",
					"description": "Kampala Mobile Money",
					"property": PROP_A,
					"tender_type": "Instant",
					"mode_of_payment": "MTN MoMo KLA",
				}).insert()
				self.assertEqual(override.name, f"{PROP_A}::Mobile Money")
			self.assertEqual(
				frappe.db.get_value("Payment Method", "Mobile Money", "description"),
				"Mobile Money",
			)
		finally:
			frappe.db.set_value("Payment Method", "Mobile Money", "description", "Mobile Money", update_modified=False)
			frappe.delete_doc("User Permission", up, ignore_permissions=True, force=True)
			frappe.clear_cache(user=fin_user)

	def test_historical_cashier_txns_and_backfill_patch_leave_closed_shifts_untouched(self):
		from kamra import cashier, ledger
		from kamra.folio import open_folio
		from kamra.patches.v48 import backfill_open_cashier_txn_currency

		make_property(PROP_A, country="Uganda", currency="UGX", business_date=TODAY)
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		add_code("Payment Method", "UT Front Desk Till", property=PROP_A,
		         tender_type="Cash", mode_of_payment="Front Desk UGX Till")
		ledger.upsert_exchange_rate(PROP_A, "USD", 3700, 3700)

		res = self.booking()
		folio_name = open_folio(res)
		with patch.object(localization, "tender_modes", return_value=None):
			# Closed shift with a legacy pre-R2 transaction (no currency/fx_amount)
			closed_sess = cashier.open_session(PROP_A, opening_float=0)["name"]
			api.add_folio_payment(folio_name, "UT Front Desk Till", 10, currency="USD")
			closed_txn = frappe.db.get_value(
				"Cashier Transaction", {"session": closed_sess}, "name",
			)
			# Simulate pre-R2 legacy DB state on the closed shift
			frappe.db.sql(
				"UPDATE `tabCashier Transaction` SET currency = NULL, fx_amount = 0 WHERE name = %s",
				(closed_txn,),
			)
			frappe.db.sql(
				"""UPDATE `tabCashier Session`
				   SET status = 'Closed', system_cash = 37000, expected_cash = 37000,
				       counted_cash = 37000, variance = 0
				   WHERE name = %s""",
				(closed_sess,),
			)

			# Open shift with a pre-R2 legacy FX transaction missing currency/fx_amount
			open_sess = cashier.open_session(PROP_A, opening_float=0)["name"]
			api.add_folio_payment(folio_name, "UT Front Desk Till", 10, currency="USD")
			open_txn = frappe.db.get_value(
				"Cashier Transaction", {"session": open_sess}, "name",
			)
			frappe.db.sql(
				"UPDATE `tabCashier Transaction` SET currency = NULL, fx_amount = 0 WHERE name = %s",
				(open_txn,),
			)
			frappe.db.sql(
				"UPDATE `tabCashier Session` SET system_cash = 37000, expected_cash = 37000 WHERE name = %s",
				(open_sess,),
			)

		# Run migration patch twice (must be idempotent)
		backfill_open_cashier_txn_currency.execute()
		backfill_open_cashier_txn_currency.execute()

		# Closed shift and its transaction MUST remain completely untouched
		closed_row = frappe.db.get_value(
			"Cashier Session", closed_sess,
			["status", "system_cash", "expected_cash", "counted_cash", "variance"], as_dict=True,
		)
		self.assertEqual(
			(closed_row.status, float(closed_row.system_cash), float(closed_row.expected_cash),
			 float(closed_row.counted_cash), float(closed_row.variance)),
			("Closed", 37000.0, 37000.0, 37000.0, 0.0),
		)
		closed_txn_row = frappe.db.get_value(
			"Cashier Transaction", closed_txn, ["currency", "fx_amount"], as_dict=True,
		)
		self.assertFalse(closed_txn_row.currency)
		self.assertEqual(float(closed_txn_row.fx_amount or 0), 0.0)

		# Open shift transaction IS backfilled and recomputed into the USD foreign drawer
		open_txn_row = frappe.db.get_value(
			"Cashier Transaction", open_txn, ["currency", "fx_amount"], as_dict=True,
		)
		self.assertEqual(open_txn_row.currency, "USD")
		self.assertEqual(float(open_txn_row.fx_amount), 10.0)
		open_totals = cashier._recompute_session_totals(open_sess, persist=False)
		self.assertEqual(open_totals["expected_cash"], 0.0)
		self.assertEqual(open_totals["foreign_cash"], 37_000.0)
		# Legacy Foreign Cash row without currency column still resolves via notes
		legacy_fc_txn = frappe.get_doc({
			"doctype": "Cashier Transaction",
			"session": open_sess,
			"property": PROP_A,
			"kind": "Payment",
			"mode": "Foreign Cash",
			"amount": 92_500.0,
			"business_date": TODAY,
			"notes": "USD 25.00 @ 3700",
			"posted_by": "Administrator",
			"posted_at": frappe.utils.now_datetime(),
		}).insert(ignore_permissions=True)
		frappe.db.sql(
			"UPDATE `tabCashier Transaction` SET currency = NULL, fx_amount = 0 WHERE name = %s",
			(legacy_fc_txn.name,),
		)
		rep = cashier.session_report(open_sess)
		usd = next(d for d in rep["foreign_drawers"] if d["currency"] == "USD")
		self.assertEqual(usd["expected_fx"], 35.0)
		self.assertEqual(rep["totals"]["expected_cash"], 0.0)
