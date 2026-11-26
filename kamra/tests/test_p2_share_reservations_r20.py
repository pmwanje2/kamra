"""Undoing a shared checkout preserves the continuing guest's room service."""

import frappe

from kamra import api, folio, housekeeping, shares
from kamra.tests.audit_fixtures import (
	AuditTestCase, TODAY, d, guest_folio, make_guest, make_property, make_reservation,
	make_room, make_room_type,
)


class ShareReinstatementTests(AuditTestCase):
	currencies = ("UGX", "USD", "KWD")
	properties = ("UT R20 UGX", "UT R20 USD", "UT R20 KWD")

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.api", "kamra.business_date", "kamra.folio", "kamra.ledger")

	def checked_out_peer(self, currency):
		prop = make_property(f"UT R20 {currency}", currency=currency, business_date=TODAY)
		rt = make_room_type(prop)
		room = make_room(prop, rt, "101")
		lead = make_reservation(prop, rt, TODAY, d(1), room=room)
		peer = shares.create_share_reservation(lead.name, make_guest("R20 Peer"))["reservation"]
		shares.update_share_rate_mode(lead.name, "Primary Pays")
		api.check_in(lead.name)
		api.check_in(peer)
		frappe.db.set_value("Property", prop, "business_date", d(1))
		api.check_out(peer)
		self.assertEqual(frappe.db.get_value("Reservation", peer, "status"), "Checked Out")
		return lead.reload(), peer

	def assert_service_survives(self, currency, service):
		lead, peer = self.checked_out_peer(currency)
		room = lead.room
		housekeeping.set_turndown_status(room, service)
		frappe.db.set_value("Room", room, "hk_resolved_occupancy", "Occupied")
		task = frappe.db.get_value("Housekeeping Task", {
			"room": room, "task_type": "Turndown", "status": "Pending", "business_date": d(1),
		}, "name")
		if service == "Pending":
			self.assertTrue(task)
		api.reinstate_checkout(peer, "Wrong sharer checked out")
		self.assertEqual(frappe.db.get_value("Reservation", peer, ["status", "share_group_id"]),
		                 ("Checked In", lead.share_group_id))
		self.assertEqual(lead.reload().status, "Checked In")
		fields = ["turndown_requested", "turndown_status", "hk_resolved_occupancy", "occupancy_status"]
		self.assertEqual(frappe.db.get_value("Room", room, fields), (1, service, "Occupied", "Occupied"))
		self.assertEqual(str(frappe.db.get_value("Room", room, "turndown_business_date")), d(1))
		if task:
			self.assertEqual(frappe.db.get_value("Housekeeping Task", task, "status"), "Pending")

	def test_pending_turndown_and_task_survive_reinstatement(self):
		for currency in self.currencies:
			with self.subTest(currency=currency):
				self.assert_service_survives(currency, "Pending")

	def test_dnd_survives_reinstatement(self):
		for currency in self.currencies:
			with self.subTest(currency=currency):
				self.assert_service_survives(currency, "DND")

	def settle_and_depart(self, lead):
		folio.post_remaining_nights(lead)
		bill = guest_folio(lead.name)
		api.add_folio_payment(bill, "Cash", frappe.db.get_value("Folio", bill, "balance"))
		api.check_out(lead.name)

	def test_same_day_unrelated_booking_blocks_even_with_a_co_sharer_present(self):
		for currency in self.currencies:
			with self.subTest(currency=currency):
				lead, peer = self.checked_out_peer(currency)
				arrival = make_reservation(lead.property, lead.room_type, d(1), d(3), room=lead.room)
				with self.assertRaisesRegex(frappe.ValidationError, f"given to {arrival.name}"):
					api.reinstate_checkout(peer, "Wrong checkout")

	def test_unrelated_in_house_guest_blocks_reinstatement(self):
		for currency in self.currencies:
			with self.subTest(currency=currency):
				lead, peer = self.checked_out_peer(currency)
				self.settle_and_depart(lead)
				arrival = make_reservation(lead.property, lead.room_type, d(1), d(3), room=lead.room)
				api.check_in(arrival.name)
				with self.assertRaisesRegex(frappe.ValidationError, f"given to {arrival.name}"):
					api.reinstate_checkout(peer, "Wrong checkout")
				self.assertEqual(frappe.db.get_value("Reservation", peer, "status"), "Checked Out")
				self.assertEqual(arrival.reload().status, "Checked In")

	def test_empty_room_still_resets_stale_service(self):
		for currency in self.currencies:
			with self.subTest(currency=currency):
				lead, peer = self.checked_out_peer(currency)
				self.settle_and_depart(lead)
				frappe.db.set_value("Room", lead.room, {"turndown_requested": 1, "turndown_status": "DND"})
				api.reinstate_checkout(peer, "Wrong checkout")
				self.assertEqual(frappe.db.get_value("Room", lead.room, ["turndown_requested", "turndown_status"]),
				                 (0, "Not Required"))
