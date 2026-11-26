"""A shared arrival retains room service and a shared departure retains the nightly room history."""

import frappe

from kamra import api, folio, housekeeping, shares
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, make_guest, make_property, make_reservation, make_room, make_room_type,
)


class ShareRoomLifecycleTests(AuditTestCase):
	currencies = ("UGX", "USD", "KWD")
	properties = ("UT R19 UGX", "UT R19 USD", "UT R19 KWD")

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.api", "kamra.folio", "kamra.business_date")

	def pair(self, currency):
		prop = make_property(f"UT R19 {currency}", currency=currency, business_date=TODAY)
		room_type = make_room_type(prop)
		lead = make_reservation(prop, room_type, TODAY, d(3), room=make_room(prop, room_type, "101"))
		peer = shares.create_share_reservation(lead.name, make_guest("R19 Peer"))["reservation"]
		return lead, frappe.get_doc("Reservation", peer)

	def check_shared_arrival(self, currency, status):
		lead, peer = self.pair(currency)
		room = lead.room
		fields = ["turndown_requested", "turndown_status", "turndown_business_date", "hk_resolved_occupancy"]
		frappe.db.set_value("Room", room, dict(zip(fields, [1, "Pending", d(-1), "Vacant"], strict=True)))
		api.check_in(lead.name)
		self.assertEqual(frappe.db.get_value("Room", room, fields), (0, "Not Required", None, None))

		housekeeping.set_turndown_status(room, status)
		frappe.db.set_value("Room", room, "hk_resolved_occupancy", "Occupied")
		before = frappe.db.get_value("Room", room, fields)
		self.assertEqual((before[0], before[1], str(before[2])), (1, status, TODAY))
		api.check_in(peer.name)
		self.assertEqual(frappe.db.get_value("Room", room, fields), before)
		if status == "Pending":
			self.assertTrue(frappe.db.exists("Housekeeping Task", {
				"property": lead.property, "room": room, "business_date": TODAY,
				"task_type": "Turndown", "status": "Pending",
			}))

	def test_pending_turndown_survives_another_sharers_arrival(self):
		for currency in self.currencies:
			with self.subTest(currency=currency):
				self.check_shared_arrival(currency, "Pending")

	def test_do_not_disturb_survives_another_sharers_arrival(self):
		for currency in self.currencies:
			with self.subTest(currency=currency):
				self.check_shared_arrival(currency, "DND")

	def test_break_records_physical_rooms_at_the_business_date_boundary(self):
		for currency in self.currencies:
			with self.subTest(currency=currency):
				lead, peer = self.pair(currency)
				for stay in (lead, peer):
					api.check_in(stay.name)
				for stay in (lead, peer):
					self.assertTrue(folio.post_room_night(stay.reload(), TODAY))
				frappe.db.set_value("Property", lead.property, "business_date", d(1))
				destination = make_room(lead.property, lead.room_type, "102")
				shares.break_share(peer.name, destination)

				def room_numbers(stay):
					stay.reload()
					return [folio._night_room_description(stay, day).split(" · ")[0]
					        for day in (TODAY, d(1), d(2))]

				self.assertEqual(room_numbers(lead), ["Room 101"] * 3)
				self.assertEqual(room_numbers(peer), ["Room 101", "Room 102", "Room 102"])
				frappe.db.set_value("Property", lead.property, "business_date", d(2))
				api.move_room(peer.name, make_room(lead.property, lead.room_type, "103"))
				self.assertEqual(room_numbers(peer), ["Room 101", "Room 102", "Room 103"])
