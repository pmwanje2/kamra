"""R2 regressions for shared-room posting, inventory and turnover."""

from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.model.document import Document
from frappe.utils import getdate

from kamra import folio, rooms, shares
from kamra.ledger import ensure_default_transaction_codes
from kamra.tests.audit_fixtures import (
	TODAY,
	AuditTestCase,
	charges,
	d,
	guest_folio,
	make_guest,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

PROPERTY = "UT Share Review Hotel"


class TestP2ShareReservationsR2(AuditTestCase):
	properties = (PROPERTY,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.rooms")
		ensure_default_transaction_codes()
		make_property(PROPERTY, currency="USD", gst_rate_low=0, gst_rate_high=0, business_date=TODAY)
		self.room_type = make_room_type(PROPERTY, base_price=101, adults_capacity=4)
		self.room = make_room(PROPERTY, self.room_type, "301")
		self.primary = make_reservation(PROPERTY, self.room_type, d(0), d(4), room=self.room)

	def add_guest(self, start=None, end=None, anchor=None):
		result = shares.create_share_reservation(
			anchor or self.primary.name, make_guest("Sharer"), start or d(0), end or d(4))
		return frappe.get_doc("Reservation", result["reservation"])

	def check_in(self, *stays):
		for stay in stays:
			stay.reload()
			stay.status = "Checked In"
			stay.save()

	def test_closed_stay_window_does_not_abort_night_audit(self):
		sharer = self.add_guest()
		closed = guest_folio(sharer.name)
		folio.close_folio(closed)
		self.check_in(self.primary, sharer)
		self.assertEqual(folio.run_night_audit(PROPERTY, TODAY)["room_charges_posted"], 2)
		posted, = charges(sharer.name, "Room")
		self.assertNotEqual(posted.parent, closed)
		self.assertEqual(frappe.db.get_value("Folio", posted.parent, "status"), "Open")
		self.assertEqual(frappe.get_doc("Folio", closed).charges, [])

	def test_primary_pays_switches_to_an_active_occupant(self):
		first = self.add_guest()
		second = self.add_guest()
		shares.update_share_rate_mode(self.primary.name, "Primary Pays")
		self.check_in(first, second)
		self.assertEqual(folio.run_night_audit(PROPERTY, TODAY)["room_charges_posted"], 2)
		amounts = [charges(stay.name, "Room")[0].amount for stay in sorted((first, second), key=lambda s: s.name)]
		self.assertEqual(amounts, [101, 0])

	def test_inclusive_room_posts_exact_unsplit_gross_at_each_currency_precision(self):
		sharer = self.add_guest()
		self.check_in(self.primary, sharer)
		frappe.db.set_value("Property", PROPERTY, "rates_include_tax", 1)
		for offset, (currency, gross) in enumerate((("UGX", "101"), ("USD", "101.01"), ("KWD", "101.001"))):
			with self.subTest(currency=currency):
				frappe.db.set_value("Property", PROPERTY, "currency", currency)
				frappe.clear_document_cache("Property", PROPERTY)
				night = d(offset)
				with (patch("kamra.folio._nightly_room_rate", return_value=Decimal(gross) / Decimal("1.18")),
				      patch("kamra.folio._nightly_gst", return_value=18)):
					for stay in (sharer, self.primary):
						self.assertTrue(folio.post_room_night(stay, night))
				rows = frappe.get_all("Folio Charge", filters={"reservation": ("in", (self.primary.name, sharer.name)),
				                      "charge_type": "Room", "posting_date": night},
				                      fields=["total", "tax_as_posted"])
				self.assertEqual(sum(Decimal(str(row.total)) for row in rows), Decimal(gross))
				self.assertTrue(all(row.tax_as_posted for row in rows))

	def test_join_and_combine_enforce_shared_physical_capacity(self):
		frappe.db.set_value("Room Type", self.room_type, "adults_capacity", 2)
		with self.assertRaises(frappe.ValidationError):
			self.add_guest()
		other_room = make_room(PROPERTY, self.room_type, "302")
		separate = make_reservation(PROPERTY, self.room_type, d(1), d(3), room=other_room)
		with self.assertRaises(frappe.ValidationError):
			shares.combine_into_share(self.primary.name, separate.name)
		self.assertEqual(separate.reload().room, other_room)

	def test_date_amendment_respects_overlap_and_total_occupants(self):
		frappe.db.set_value("Room Type", self.room_type, {"adults_capacity": 4, "max_total_occupants": 3})
		early = self.add_guest(d(0), d(2))
		late = self.add_guest(d(2), d(4))
		early.check_out_date = d(3)
		with self.assertRaises(frappe.ValidationError):
			early.save()
		self.assertEqual(late.reload().check_in_date, getdate(d(2)))

	def test_share_room_and_levy_follow_the_explicit_payer(self):
		from kamra.routing import save_routing_instruction

		frappe.db.set_value("Property", PROPERTY, "room_levy_per_night", 2)
		frappe.clear_document_cache("Property", PROPERTY)
		other_room = make_room(PROPERTY, self.room_type, "302")
		sharer = make_reservation(PROPERTY, self.room_type, TODAY, d(4), room=other_room)
		shares.combine_into_share(self.primary.name, sharer.name)
		save_routing_instruction({
			"property": PROPERTY, "reservation": sharer.name, "target_type": "Window",
			"target_window": 2, "codes": [{"charge_type": "Room"}, {"charge_type": "Room Levy"}],
		})
		frappe.db.set_value("Property", PROPERTY, "room_levy_percent", 5)
		frappe.clear_document_cache("Property", PROPERTY)
		self.check_in(self.primary, sharer)
		folio.post_room_night(sharer, TODAY)
		room, = charges(sharer.name, "Room")
		levy, = charges(sharer.name, "Room Levy")
		self.assertEqual(room.parent, levy.parent)
		self.assertEqual(frappe.db.get_value("Folio", room.parent, "window_no"), 2)

	def test_insights_count_one_room_night_and_keep_revenue_at_the_posted_room(self):
		sharer = self.add_guest()
		self.check_in(self.primary, sharer)
		for stay in (self.primary, sharer):
			folio.post_room_night(stay, TODAY)
		result = rooms._compute_insights(PROPERTY, 1, getdate(d(1)), True)
		self.assertEqual(result["summary"]["room_nights"], 1)
		self.assertEqual(result["summary"]["occupancy_pct"], 100)
		other_room = make_room(PROPERTY, self.room_type, "302")
		frappe.db.set_value("Property", PROPERTY, "business_date", d(1))
		shares.break_share(sharer.name, other_room)
		result = rooms._compute_insights(PROPERTY, 1, getdate(d(1)), True)
		rows = {row["room"]: row for row in result["rooms"]}
		self.assertEqual((rows[self.room]["revenue"], rows[self.room]["nights"]), (101, 1))
		self.assertEqual((rows[other_room]["revenue"], rows[other_room]["nights"]), (0, 0))

	def test_room_board_does_not_turn_over_while_a_sharer_remains(self):
		sharer = self.add_guest(d(0), d(1))
		self.check_in(self.primary, sharer)
		original_get_all = frappe.get_all

		def get_all(doctype, *args, **kwargs):
			rows = original_get_all(doctype, *args, **kwargs)
			if doctype == "Reservation" and kwargs.get("filters", {}).get("status") == "Checked In":
				return sorted(rows, key=lambda row: row.name)
			return rows

		with (patch("kamra.rooms.nowdate", return_value=d(1)),
		      patch.object(frappe, "get_all", side_effect=get_all)):
			tile, = rooms.rooms_board(PROPERTY)["rooms"]
		self.assertFalse(tile["stay"]["departing_today"])
		self.assertEqual(tile["stay"]["reservation"], self.primary.name)

	def test_reservation_save_takes_property_lock_before_loading_for_update(self):
		events = []
		original_get = frappe.db.get_value
		original_load = Document.load_doc_before_save

		def get_value(doctype, *args, **kwargs):
			if doctype == "Property" and kwargs.get("for_update"):
				events.append("Property")
			return original_get(doctype, *args, **kwargs)

		def load_doc_before_save(doc, *args, **kwargs):
			if doc.doctype == "Reservation" and kwargs.get("raise_exception"):
				events.append("Reservation")
			return original_load(doc, *args, **kwargs)

		doc = frappe.get_doc("Reservation", self.primary.name)
		with (patch.object(frappe.db, "get_value", side_effect=get_value),
		      patch.object(Document, "load_doc_before_save", load_doc_before_save)):
			doc.save()
		self.assertEqual(events[:2], ["Property", "Reservation"])
