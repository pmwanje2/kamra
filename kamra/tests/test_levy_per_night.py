"""A fixed local hotel tax per occupied room-night (#102).

Some municipalities charge a flat amount per occupied room per night (UGX
2,000 in Uganda) instead of a % of the rate. Property.room_levy_per_night
(default 0) sits beside room_levy_percent: when it is above 0, the quote,
the night audit / check-out posting and the pro forma carry it on the Room
Levy line. A property that leaves it at 0 is billed exactly as before.
"""

from __future__ import annotations

import frappe

from kamra import folio as folio_mod
from kamra import ledger
from kamra.pricing import quote
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	charges,
	d,
	guest_folio,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)


class LevyPerNightTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY, room_levy_per_night=2000,
		              room_levy_taxable=0, room_levy_label="Local hotel tax")
		self.rt = make_room_type(PROP_A, "STD", base_price=100000)
		self.room = make_room(PROP_A, self.rt, "101")

	def test_the_field_defaults_to_zero(self):
		field = frappe.get_meta("Property").get_field("room_levy_per_night")
		self.assertIsNotNone(field)
		self.assertEqual(str(field.default or "0"), "0")

	def test_the_quote_adds_it_per_night(self):
		q = quote(PROP_A, self.rt, d(0), d(3), 2, 0)
		self.assertAlmostEqual(q["room_levy"], 6000)
		line = next(r for r in q["line_items"] if r["component"] == "room_levy")
		self.assertAlmostEqual(line["amount"], 6000)
		self.assertAlmostEqual(line["tax_amount"], 0)
		self.assertEqual(line["nights_applied"], 3)
		self.assertEqual(line["description"], "Local hotel tax")
		self.assertAlmostEqual(q["amount_after_tax"] - q["amount_before_tax"], q["tax_amount"])

	def test_with_a_percent_levy_both_apply(self):
		frappe.db.set_value("Property", PROP_A, "room_levy_percent", 1)
		frappe.clear_document_cache("Property", PROP_A)
		q = quote(PROP_A, self.rt, d(0), d(2), 2, 0)
		# 1% of 2 x 100,000 plus 2 x 2,000
		self.assertAlmostEqual(q["room_levy"], 2000 + 4000)

	def test_each_posted_night_carries_the_fixed_levy(self):
		res = make_reservation(PROP_A, self.rt, d(0), d(2), status="Checked In",
		                       room=self.room)
		folio_mod.post_remaining_nights(frappe.get_doc("Reservation", res.name))
		levies = sorted(charges(res.name, "Room Levy"), key=lambda c: str(c.posting_date))
		self.assertEqual([str(c.posting_date) for c in levies], [d(0), d(1)])
		self.assertEqual([float(c.amount) for c in levies], [2000, 2000])
		self.assertTrue(all("Local hotel tax" in c.description for c in levies))
		# booked to the levy account on the ledger, apart from room revenue
		row = frappe.get_all("Folio Charge", filters={"reservation": res.name,
		                                               "charge_type": "Room Levy"},
		                     fields=["name", "gst_rate"], limit=1)[0]
		self.assertEqual(float(row.gst_rate), 0)
		self.assertEqual(frappe.db.get_value(
			"Folio Ledger Entry", {"source_doctype": "Folio Charge", "source_name": row.name},
			"transaction_code"), ledger.code_for_charge_type("Room Levy"))

	def test_a_night_is_not_levied_twice(self):
		res = make_reservation(PROP_A, self.rt, d(0), d(2), status="Checked In",
		                       room=self.room)
		doc = frappe.get_doc("Reservation", res.name)
		folio_mod.post_room_night(doc, d(0))
		folio_mod.post_room_night(doc, d(0))
		self.assertEqual(len(charges(res.name, "Room Levy")), 1)

	def test_the_proforma_projects_it(self):
		res = make_reservation(PROP_A, self.rt, d(0), d(2), status="Checked In",
		                       room=self.room)
		inv = ledger.generate_proforma(guest_folio(res.name))["invoice"]
		levy = [l for l in inv["projected_lines"] if l["charge_type"] == "Room Levy"]
		self.assertEqual([float(l["amount"]) for l in levy], [2000, 2000])

	def test_zero_keeps_an_existing_property_unchanged(self):
		frappe.db.set_value("Property", PROP_A, "room_levy_per_night", 0)
		frappe.clear_document_cache("Property", PROP_A)
		q = quote(PROP_A, self.rt, d(0), d(2), 2, 0)
		self.assertAlmostEqual(q["room_levy"], 0)
		self.assertFalse([r for r in q["line_items"] if r["component"] == "room_levy"])
		res = make_reservation(PROP_A, self.rt, d(0), d(2), status="Checked In",
		                       room=self.room)
		folio_mod.post_remaining_nights(frappe.get_doc("Reservation", res.name))
		self.assertEqual(charges(res.name, "Room Levy"), [])

	def test_the_settings_screen_offers_it(self):
		from kamra.settings import SETTINGS
		self.assertTrue("room_levy_per_night" in SETTINGS, "room_levy_per_night")
