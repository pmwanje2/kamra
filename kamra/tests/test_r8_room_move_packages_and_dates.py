"""Room moves preserve the business date, nightly package price and add-on eligibility."""

import json
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.utils import getdate

from kamra import api, business_date, folio, pricing
from kamra.tests.audit_fixtures import (
	PROP_A, TODAY, AuditTestCase, d, guest_folio, make_property, make_reservation, make_room,
	make_room_type,
)
from kamra.tests.test_reservation_discounts import set_setting


class MoveScenarios:
	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.api", "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY, currency=self.currency, rates_include_tax=0)
		set_setting(PROP_A, "rate_override_limit_pct", 25)
		self.a = make_room_type(PROP_A, "R8A", base_price=self.source, tax_percent=5)
		self.b = make_room_type(PROP_A, "R8B", base_price=self.destination, tax_percent=18)
		room = make_room(PROP_A, self.a, "101")
		self.target = make_room(PROP_A, self.b, "201")
		self.stay = make_reservation(PROP_A, self.a, TODAY, d(2), status="Checked In", room=room,
		                             adults=1, auto_price=1)

	def add_destination_package(self):
		return frappe.get_doc({
			"doctype": "Holiday Package", "property": PROP_A, "package_name": "R8 destination",
			"room_type": self.b, "start_date": TODAY, "end_date": d(1), "package_nights": 2,
			"price_2_pax": self.package_night * 2,
			"elements": [{"element_name": "Airport transfer", "element_type": "Transport",
			              "transaction_code": "3200", "charge_type": "Taxi", "allowance_basis": "Per Room Night",
			              "unit_allowance": 500, "posting_rhythm": "Every Night", "included_in_rate": 0,
			              "folio_display_mode": "Separate Add-on"}],
		}).insert(ignore_permissions=True)

	def test_legacy_move_date_after_date_format_change(self):
		stay = self.stay
		api.set_reservation_discount(stay.name, discount_type="Percent", discount_value=10,
		                             reason_code="RATE-PROMO")
		business_date.advance_business_date(PROP_A, TODAY)
		with patch.object(frappe, "in_test", False), patch.object(frappe.local, "user_date_format", "dd-mm-yyyy", create=True):
			api.move_reservation(stay.name, self.target, upgrade_type="Paid", confirm_price=1)
		api.amend_stay(stay.name, TODAY, d(3), confirm_price=1)
		versions = frappe.get_all("Version", filters={"ref_doctype": "Reservation", "docname": stay.name},
		                          fields=["name", "data"])
		version, cutoff_change = next((v, change) for v in versions for change in json.loads(v.data)["changed"]
		                              if change[0] == "room_move_rate_cutoff")
		with self.subTest(new_move="ISO"):
			self.assertEqual(cutoff_change[2], d(1))

		# Model a pre-snapshot move. Its only source-type evidence is the original Version.
		stay.db_set("room_move_night_types", None)
		self.assertFalse(stay.get("contract_tariff"))
		frappe.db.set_single_value("System Settings", "date_format", "mm/dd/yyyy")
		frappe.clear_document_cache("System Settings")
		self.addCleanup(frappe.clear_document_cache, "System Settings")
		self.enterContext(patch.object(frappe.local, "user_date_format", "mm/dd/yyyy", create=True))
		data = json.loads(version.data)
		cutoff = next(change for change in data["changed"] if change[0] == "room_move_rate_cutoff")
		for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m-%d-%Y", "%m/%d/%Y", "unrecorded"):
			cutoff[2] = getdate(d(1)).strftime(fmt)
			frappe.db.set_value("Version", version.name, {"data": json.dumps(data), "creation": d(1)})
			with self.subTest(legacy_format=fmt):
				self.assertEqual(api.discount_preview(stay.name)["new_amount"], self.undiscounted)
		removed = api.set_reservation_discount(stay.name, confirm_price=1)
		self.assertEqual(removed["amount_after_tax"], self.undiscounted)
		self.assertEqual(folio.locked_night(stay.reload(), TODAY).gst_rate, 5)

	def test_paid_destination_package_only_prices_destination_nights(self):
		self.add_destination_package()
		business_date.advance_business_date(PROP_A, TODAY)
		pv = api.room_move_preview(self.stay.name, self.target, upgrade_type="Paid")
		move = api.move_reservation(self.stay.name, self.target, upgrade_type="Paid", confirm_price=1)
		stay = self.stay.reload()
		quote = stay.requote()
		for label, amount in (("preview", pv["new_amount"]), ("save", move["amount_after_tax"]),
		                      ("requote", quote["amount_after_tax"])):
			with self.subTest(stage=label):
				self.assertEqual(Decimal(str(amount)), Decimal(self.package_total))
		with self.subTest(stage="source quote"):
			night = quote["nightly"][0]
			self.assertEqual((night["rate"], night["gst_rate"], night.get("package")), (self.source, 5, None))
		self.assertEqual(quote["nightly"][1]["rate"], self.package_night)
		self.assertEqual(pricing.billing_room_type(stay, TODAY), self.a)
		for day in (TODAY, d(1)):
			self.assertTrue(folio.post_room_night(stay, day))
		bill = frappe.get_doc("Folio", guest_folio(stay.name))
		addons = [str(row.posting_date) for row in bill.charges if row.charge_type == "Taxi"]
		with self.subTest(stage="add-on"):
			self.assertEqual(addons, [d(1)])
		self.assertEqual(Decimal(str(bill.grand_total)), Decimal(self.package_total) + 500)

	def test_complimentary_upgrade_keeps_source_package_eligibility(self):
		self.add_destination_package()
		pv = api.room_move_preview(self.stay.name, self.target, upgrade_type="Complimentary")
		self.assertEqual(pv["difference"], 0)
		api.move_reservation(self.stay.name, self.target, upgrade_type="Complimentary", reason="Guest recovery")
		stay = self.stay.reload()
		self.assertEqual(stay.room_type_to_charge, self.a)
		self.assertFalse(stay.requote()["package"])
		self.assertTrue(folio.post_room_night(stay, TODAY))
		bill = frappe.get_doc("Folio", guest_folio(stay.name))
		self.assertEqual([(line.charge_type, Decimal(str(line.total))) for line in bill.charges],
		                 [("Room", Decimal(self.first_gross))])


class TestINR(MoveScenarios, AuditTestCase):
	currency, source, destination, package_night = "INR", 10000, 15000, 15000
	first_gross, package_total, undiscounted = "10500", "28200", 45900


class TestUGX(MoveScenarios, AuditTestCase):
	currency, source, destination, package_night = "UGX", 10000, 15000, 15500
	first_gross, package_total, undiscounted = "10500", "28790", 45900


class TestUSD(MoveScenarios, AuditTestCase):
	currency, source, destination, package_night = "USD", 102, 150, 155
	first_gross, package_total, undiscounted = "107.10", "290.00", 461.10


class TestKWD(MoveScenarios, AuditTestCase):
	currency, source, destination, package_night = "KWD", 10.22, 15, 15.5
	first_gross, package_total, undiscounted = "10.731", "29.021", 46.131
