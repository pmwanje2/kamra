"""R10 posting, legacy history, request validation and turndown regressions."""
from decimal import Decimal
import json
from unittest.mock import patch

import frappe
from werkzeug.test import Client
from werkzeug.wrappers import Response

from kamra import api, business_date, folio, packages, pricing, wsgi
from kamra.tests.audit_fixtures import (
	AuditTestCase, PROP_A, TODAY, d, guest_folio, make_property, make_reservation, make_room, make_room_type,
)


class TestR10Posting(AuditTestCase):
	def start(self, currency="USD", inclusive=0):
		self.at(TODAY, "kamra.api", "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY, currency=currency, rates_include_tax=inclusive)
		self.types = [make_room_type(PROP_A, code, base_price=rate, tax_percent=gst)
		              for code, rate, gst in (("R10A", 100, 5), ("R10B", 200, 18), ("R10C", 100, 5))]
		self.rooms = [make_room(PROP_A, rt, str(num)) for rt, num in zip(self.types, (101, 202, 303))]

	def package(self, name, rt=None, price=100):
		return frappe.get_doc({
			"doctype": "Holiday Package", "property": PROP_A, "package_name": name,
			"room_type": rt, "start_date": TODAY, "end_date": d(8), "package_nights": 1,
			"price_2_pax": price, "extra_night_percent": 100,
			"elements": [{"element_name": "Airport transfer", "element_type": "Transport",
			              "transaction_code": "3200", "charge_type": "Taxi", "allowance_basis": "Fixed Per Stay",
			              "unit_allowance": 45.50, "posting_rhythm": "First Night", "included_in_rate": 0}],
		}).insert(ignore_permissions=True)

	def book(self):
		self.stay = make_reservation(PROP_A, self.types[0], TODAY, d(3), status="Checked In",
		                             room=self.rooms[0], adults=1, auto_price=1)

	def move(self, i):
		business_date.advance_business_date(PROP_A, d(i-1))
		api.move_reservation(self.stay.name, self.rooms[i], upgrade_type="Paid", confirm_price=1)
		self.stay.reload()

	def bill(self):
		return frappe.get_doc("Folio", guest_folio(self.stay.name))

	def test_house_package_transfer_once_after_two_moves_and_extension(self):
		self.start()
		pkg = self.package("R10 House")
		self.book()
		folio.post_room_night(self.stay, TODAY)
		self.move(1)
		folio.post_room_night(self.stay, d(1))
		self.move(2)
		api.amend_stay(self.stay.name, TODAY, d(4), confirm_price=1)
		for day in (d(2), d(3), d(1)):
			folio.post_room_night(self.stay.reload(), day)
		self.assertEqual([r.total for r in self.bill().charges if r.charge_type == "Taxi"], [45.5])
		self.assertEqual(frappe.db.count("Reservation Package Allowance",
		                 {"reservation": self.stay.name, "package_element": pkg.elements[0].name}), 1)

	def test_legacy_dated_element_is_already_fulfilled(self):
		self.start()
		pkg = self.package("R10 Legacy")
		self.book()
		folio.post_room_night(self.stay, TODAY)
		# A legacy posting dated on a previous segment, including its original allowance.
		key = f"pkg:{self.stay.name}:{pkg.elements[0].name}:{TODAY}"
		row = next(r for r in self.bill().charges if r.charge_type == "Taxi")
		frappe.db.set_value("Folio Charge", row.name, "idempotency_key", key)
		frappe.db.set_value("Reservation Package Allowance", {"reservation": self.stay.name},
		                    "idempotency_key", key)
		self.move(1)
		# A changed posting rhythm still refers to the already fulfilled entitlement.
		pkg.elements[0].posting_rhythm = "Last Night"
		pkg.save()
		folio.post_room_night(self.stay, d(2))
		self.assertEqual(sum(r.total for r in self.bill().charges if r.charge_type == "Taxi"), 45.5)
		self.assertEqual(frappe.db.count("Reservation Package Allowance", {"reservation": self.stay.name}), 1)

	def inclusive(self, currency, price, expected):
		self.start(currency, inclusive=1)
		self.package("R10 Included Tax", self.types[1], price)
		self.book()
		self.move(1)
		self.move(2)
		api.amend_stay(self.stay.name, TODAY, d(4), confirm_price=1)
		self.stay.reload()
		for day in (TODAY, d(1), d(2), d(3)):
			folio.post_room_night(self.stay, day)
		bill = self.bill()
		transfer = sum(Decimal(str(r.total)) for r in bill.charges if r.charge_type == "Taxi")
		self.assertEqual((Decimal(str(self.stay.amount_after_tax)), Decimal(str(bill.grand_total)) - transfer),
		                 (Decimal(expected), Decimal(expected)))

	def test_ugx_tax_inclusive(self):
		self.inclusive("UGX", 200, "500")

	def test_usd_tax_inclusive(self):
		self.inclusive("USD", 200.01, "500.01")

	def test_kwd_tax_inclusive(self):
		self.inclusive("KWD", 200.001, "500.001")

	def test_posting_and_guest_projection_identify_each_nights_room_and_package(self):
		self.start()
		self.package("R10 Source Escape", self.types[0])
		self.package("R10 Destination Escape", self.types[1], 200)
		self.book()
		self.move(1)
		for day in (TODAY, d(1)):
			folio.post_room_night(self.stay, day)
		bill = self.bill()
		actual = {str(r.posting_date): r.description for r in bill.charges if r.charge_type == "Room"}
		with self.subTest(view="historical room"):
			self.assertIn("101", actual[TODAY])
			self.assertIn("R10A", actual[TODAY])
			self.assertIn("202", actual[d(1)])
		bundles = {line["posting_date"]: line["description"] for line in
		           packages.package_folio_view(bill.name, "Inclusive")["lines"] if line["bundled"]}
		with self.subTest(view="inclusive labels"):
			self.assertIn("R10 Source Escape", bundles[TODAY])
			self.assertIn("R10 Destination Escape", bundles[d(1)])


class TestR10Guards(AuditTestCase):
	def test_partial_or_missing_legacy_history_requires_evidence(self):
		stay = frappe._dict(name="legacy", check_in_date=TODAY, room_move_rate_cutoff=d(3),
		                    room_type="C", night_rates=[frappe._dict(stay_date=d(1))])
		def version(old, new, before, after, created):
			return frappe._dict(creation=created, data=json.dumps({"changed": [
				["room_type", before, after], ["room_move_rate_cutoff", old, new]]}))
		for history, snapshot in (([], None), ([], {TODAY: "A"}), (
			[version(None, "unknown", "A", "B", d(2)), version("unknown", d(3), "B", "C", d(3))], None),
			([version(d(2), d(3), "B", "C", d(3))], None)):
			with self.subTest(history=history, snapshot=snapshot), patch.object(frappe, "get_all", return_value=history):
				stay.room_move_night_types = snapshot
				with self.assertRaisesRegex(frappe.ValidationError, "ambiguous"):
					pricing.nightly_billing_room_types(stay)

	def test_approval_schema_rejects_non_scalar_bodies_before_logging(self):
		seen = []
		def denied(environ, start_response):
			seen.append("session initialization")
			return Response(status=403)(environ, start_response)
		client = Client(wsgi.approval_middleware(denied))
		for body in ([{"supervisor_pin": "fixture"}], {"reason": {"pin_secret": "fixture"}},
		             {"reason": ["fixture"]}, {"supervisor_pin": {"value": "fixture"}}, "fixture"):
			with self.subTest(shape=type(body).__name__):
				response = client.post("/api/method/kamra.api.move_room", json=body)
				self.assertEqual((response.status_code, response.data, seen), (400, b"", []))

	def test_native_turndown_and_mobile_queue_require_in_house_occupancy(self):
		make_property(PROP_A, business_date=TODAY)
		room = make_room(PROP_A, make_room_type(PROP_A), "207")
		def task():
			return frappe.get_doc(doctype="Housekeeping Task", property=PROP_A, room=room,
			                      task_type="Turndown", priority="Low", status="Pending", business_date=TODAY)
		with self.subTest(path="native"):
			with self.assertRaises(frappe.ValidationError):
				task().insert()
		legacy = task()
		legacy.name = frappe.generate_hash(length=10)
		legacy.db_insert()
		with self.subTest(path="mobile"):
			self.assertNotIn(legacy.name, [r.name for r in api.hk_queue(PROP_A)["tasks"]])
		legacy.status = "Cancelled"
		legacy.save()
