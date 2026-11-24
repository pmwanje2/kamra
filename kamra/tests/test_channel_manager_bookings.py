"""Rejected OTA sales remain visible to staff without consuming inventory."""

from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, nowdate

from kamra import channel_manager as cm
from kamra import pricing
from kamra.channels import aiosell


class ChannelManagerBookingTestCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		frappe.local.lang = "en"
		self.property = frappe.get_doc({"doctype": "Property",
			"property_name": "TEST OTA " + frappe.generate_hash(length=8),
			"country": "Uganda", "currency": "UGX"}).insert(ignore_permissions=True).name
		self.room_type = frappe.get_doc({"doctype": "Room Type", "property": self.property,
			"room_type_code": "STD", "room_type_name": "Standard", "base_price": 1000,
			"adults_capacity": 2}).insert(ignore_permissions=True).name
		frappe.get_doc({"doctype": "Room", "property": self.property,
			"room_type": self.room_type, "room_number": "101"}).insert(ignore_permissions=True)
		self.guest = frappe.get_doc({"doctype": "Guest", "first_name": "OTA Guest"}).insert(ignore_permissions=True).name
		self.conn = frappe.get_doc({"doctype": "Channel Manager Connection", "property": self.property,
			"provider": "AioSell", "active": 1, "external_property_id": "demo"}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Channel Room Mapping", "connection": self.conn.name,
			"room_type": self.room_type, "external_room_id": "std"}).insert(ignore_permissions=True)
		self.ref = "TEST-" + frappe.generate_hash(length=8)

	def payload(self, currency="UGX", rooms=1):
		return {"action": "book", "hotelCode": "demo", "channel": "Booking.com",
			"bookingId": self.ref, "checkin": nowdate(), "checkout": str(add_days(nowdate(), 1)),
			"guest": {"firstName": "OTA", "lastName": "Arrival"},
			"amount": {"amountAfterTax": 1500, "currency": currency},
			"specialRequests": "Late arrival",
			"rooms": [{"roomCode": "std", "occupancy": {"adults": 2, "children": 0},
			           "prices": [{"date": nowdate(), "sellRate": 1500}]} for _ in range(rooms)]}

	def sold_out(self):
		return frappe.get_doc({"doctype": "Reservation", "property": self.property,
			"room_type": self.room_type, "guest": self.guest, "status": "Confirmed",
			"check_in_date": nowdate(), "check_out_date": add_days(nowdate(), 1),
			"adults": 2}).insert(ignore_permissions=True)

	def deliver(self, payload):
		# Real rollback, bounded to the worker's transaction; keep test fixtures.
		sp = "ota_test_" + frappe.generate_hash(length=8)
		frappe.db.savepoint(sp)
		rollback = frappe.db.rollback
		def worker_rollback(*args, **kwargs):
			return rollback(*args, **kwargs) if args or kwargs else rollback(save_point=sp)
		with patch.object(frappe.db, "commit", return_value=None), \
		     patch.object(frappe.db, "rollback", side_effect=worker_rollback):
			cm.process_webhook_events(self.conn.name, payload)

	def received(self, ref=None):
		name = frappe.db.get_value("Reservation", {"property": self.property, "ota_ref": ref or self.ref})
		self.assertTrue(name, "OTA booking was dropped instead of retained for resolution")
		return frappe.get_doc("Reservation", name)

	def test_sold_out_booking_is_requested_with_resolution_note(self):
		original = self.sold_out()
		self.deliver(self.payload())
		doc = self.received()
		self.assertEqual(doc.status, "Requested")
		self.assertIn("OTA overbooking — resolve", doc.special_requests)
		self.assertIn("Late arrival", doc.special_requests)
		self.assertEqual(float(doc.amount_after_tax), 1500)
		self.assertEqual(cm._sellable(self.property, self.room_type, nowdate(), add_days(nowdate(), 1)), 0)
		self.assertEqual(frappe.db.get_value("Reservation", original.name, "status"), "Confirmed")

	def test_multi_room_refusal_keeps_all_lines_requested(self):
		self.deliver(self.payload(rooms=2))
		for i in range(2):
			self.assertEqual(self.received(f"{self.ref}-{i}").status, "Requested")

	def test_requested_replay_does_not_duplicate_the_booking(self):
		self.sold_out()
		payload = self.payload()
		self.deliver(payload)
		self.deliver(payload)
		self.assertEqual(self.received().status, "Requested")
		self.assertEqual(frappe.db.count("Reservation", {"property": self.property, "ota_ref": self.ref}), 1)

	def test_a_second_delivery_returns_the_original_stay(self):
		payload = self.payload()
		self.deliver(payload)
		original = self.received()
		self.assertEqual(original.status, "Confirmed")
		self.deliver(payload)
		self.assertEqual(frappe.db.get_value(
			"Reservation", {"property": self.property, "ota_ref": self.ref}, "name"),
			original.name)
		self.assertEqual(frappe.db.count(
			"Reservation", {"property": self.property, "ota_ref": self.ref}), 1)

	def test_missing_currency_defaults_to_property_currency(self):
		payload = self.payload()
		payload["amount"].pop("currency")
		self.assertEqual(aiosell.parse_webhook(self.conn, payload)[0]["currency"], "UGX")

	def test_foreign_currency_is_requested_without_mislabelled_money(self):
		"""A booking in neither the property currency nor its mapped rate
		plan's (this AioSell room maps no rate plan) keeps the fail-closed
		path: Requested, the foreign money only in the note."""
		self.deliver(self.payload(currency="USD"))
		doc = self.received()
		self.assertEqual(doc.status, "Requested")
		self.assertIn("USD", doc.special_requests)
		self.assertIn("UGX", doc.special_requests)
		self.assertIn("1500", doc.special_requests)
		self.assertEqual(float(doc.amount_after_tax or 0), 0)
		self.assertEqual(cm._sellable(self.property, self.room_type, nowdate(), add_days(nowdate(), 1)), 1)

	def test_matching_currency_keeps_confirmed_booking_and_original_price(self):
		self.deliver(self.payload())
		doc = self.received()
		self.assertEqual(doc.status, "Confirmed")
		self.assertEqual(float(doc.amount_after_tax), 1500)

	def test_currency_mismatch_on_modify_is_requested(self):
		"""The same rule on a modify: no mapped rate plan in USD, so Requested."""
		self.deliver(self.payload())
		payload = self.payload(currency="USD")
		payload["action"] = "modify"
		self.deliver(payload)
		self.assertEqual(self.received().status, "Requested")
		self.assertIn("USD", self.received().special_requests)
		self.assertFalse(frappe.flags.kamra_status_transition)

	def test_capacity_refusal_on_modify_keeps_new_dates_for_resolution(self):
		payload = self.payload()
		payload["checkin"] = str(add_days(nowdate(), 2))
		payload["checkout"] = str(add_days(nowdate(), 3))
		self.deliver(payload)
		self.sold_out()
		payload = self.payload()
		payload["action"] = "modify"
		self.deliver(payload)
		doc = self.received()
		self.assertEqual(doc.status, "Requested")
		self.assertEqual(str(doc.check_in_date), nowdate())
		self.assertIn("OTA overbooking — resolve", doc.special_requests)
		self.assertFalse(frappe.flags.kamra_status_transition)

	def test_invalid_occupancy_is_not_treated_as_overbooking(self):
		payload = self.payload()
		payload["rooms"][0]["occupancy"]["adults"] = 3
		self.deliver(payload)
		self.assertFalse(frappe.db.exists("Reservation", {"property": self.property, "ota_ref": self.ref}))

	def test_ota_cancel_cancels_retained_requested_booking(self):
		self.sold_out()
		self.deliver(self.payload())
		self.assertEqual(self.received().status, "Requested")
		self.deliver({"action": "cancel", "hotelCode": "demo", "channel": "Booking.com",
			"bookingId": self.ref})
		doc = self.received()
		self.assertEqual(doc.status, "Cancelled")
		self.assertIn(self.ref, doc.cancellation_note or "")

	def test_modify_dropping_a_retained_room_cancels_it(self):
		self.deliver(self.payload(rooms=3))
		for i in range(3):
			self.assertEqual(self.received(f"{self.ref}-{i}").status, "Requested")
		payload = self.payload(rooms=2)
		payload["action"] = "modify"
		self.deliver(payload)
		self.assertEqual(self.received(f"{self.ref}-0").status, "Requested")
		self.assertEqual(self.received(f"{self.ref}-1").status, "Requested")
		self.assertEqual(self.received(f"{self.ref}-2").status, "Cancelled")

	def test_physical_room_double_booking_is_retained_as_requested(self):
		"""The room-overlap refusal ("Double booking blocked") is an inventory
		refusal too, whatever its message title says."""
		frappe.get_doc({"doctype": "Room", "property": self.property,
			"room_type": self.room_type, "room_number": "102"}).insert(ignore_permissions=True)
		taken = frappe.db.get_value("Room", {"property": self.property, "room_number": "101"})
		holder = self.sold_out()
		frappe.db.set_value("Reservation", holder.name, "room", taken)
		original = cm._apply_event

		def assign_taken_room(conn, e, requested_reason=None):
			out = original(conn, e, requested_reason=requested_reason)
			if not requested_reason and out.get("reservation"):
				doc = frappe.get_doc("Reservation", out["reservation"])
				doc.room = taken
				doc.save(ignore_permissions=True)
			return out

		with patch.object(cm, "_apply_event", side_effect=assign_taken_room):
			self.deliver(self.payload())
		doc = self.received()
		self.assertEqual(doc.status, "Requested")
		self.assertIn("OTA overbooking — resolve", doc.special_requests)
		self.assertFalse(doc.room)

	def test_readable_modify_on_overbooking_hold_keeps_hold_note_and_appends_ota_notes(self):
		"""A readable modify on a stay held for a non-tax reason (e.g.
		overbooking) must keep the hold note and append the OTA's notes."""
		self.sold_out()
		self.deliver(self.payload())
		held = self.received()
		self.assertEqual(held.status, "Requested")
		self.assertIn("OTA overbooking — resolve", held.special_requests)

		# Modify with empty OTA notes: hold note must NOT be wiped to None
		mod_empty = self.payload()
		mod_empty["action"] = "modify"
		mod_empty["specialRequests"] = ""
		self.deliver(mod_empty)
		doc = self.received()
		self.assertEqual(doc.status, "Requested")
		self.assertIn("OTA overbooking — resolve", doc.special_requests or "")

		# Modify with new OTA notes: keep the hold note and append the OTA notes
		mod_notes = self.payload()
		mod_notes["action"] = "modify"
		mod_notes["specialRequests"] = "High floor please"
		self.deliver(mod_notes)
		doc = self.received()
		self.assertEqual(doc.status, "Requested")
		self.assertIn("OTA overbooking — resolve", doc.special_requests or "")
		self.assertIn("High floor please", doc.special_requests or "")
		self.assertEqual((doc.special_requests or "").count("OTA overbooking — resolve"), 1)

		# Repeating the same modify stays idempotent without duplicating the hold line
		self.deliver(mod_notes)
		doc = self.received()
		self.assertEqual((doc.special_requests or "").count("OTA overbooking — resolve"), 1)
		self.assertIn("High floor please", doc.special_requests or "")

	def test_modify_dropping_a_checked_in_room_flags_review_instead_of_ignoring(self):
		"""When a multi-room modify omits a room that is already Checked In,
		that room is neither cancelled nor silently ignored: it stays Checked In
		and is flagged for front-desk review."""
		for n in ("102", "103"):
			frappe.get_doc({"doctype": "Room", "property": self.property,
				"room_type": self.room_type, "room_number": n}).insert(ignore_permissions=True)
		self.deliver(self.payload(rooms=3))
		dropped = self.received(f"{self.ref}-2")
		frappe.db.set_value("Reservation", dropped.name, "status", "Checked In")

		payload = self.payload(rooms=2)
		payload["action"] = "modify"
		self.deliver(payload)

		dropped.reload()
		self.assertEqual(dropped.status, "Checked In")
		self.assertEqual(dropped.channel_review_needed, 1)
		self.assertIn("not applied: the stay is Checked In", dropped.special_requests or "")
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": dropped.name,
			"action_type": "channel.modify_needs_review"}))

	def test_capacity_refusal_on_multi_room_modify_still_reconciles_dropped_rooms(self):
		"""When a multi-room modify hits InventoryRefused and falls back to
		retaining the remaining lines as Requested, omitted rooms are still
		reconciled: Confirmed dropped rooms are cancelled and Checked In
		dropped rooms stay Checked In with channel_review_needed flagged."""
		for n in ("102", "103", "104"):
			frappe.get_doc({"doctype": "Room", "property": self.property,
				"room_type": self.room_type, "room_number": n}).insert(ignore_permissions=True)

		# Book 4 rooms on future dates (all Confirmed: -0, -1, -2, -3)
		book = self.payload(rooms=4)
		book["checkin"] = str(add_days(nowdate(), 4))
		book["checkout"] = str(add_days(nowdate(), 5))
		self.deliver(book)
		for i in range(4):
			self.assertEqual(self.received(f"{self.ref}-{i}").status, "Confirmed")

		# Room -3 is already Checked In; room -2 is still Confirmed
		dropped_inhouse = self.received(f"{self.ref}-3")
		frappe.db.set_value("Reservation", dropped_inhouse.name, "status", "Checked In")

		# Sell out today completely (4 rooms) so moving rooms -0 and -1 to today hits InventoryRefused
		for _ in range(4):
			self.sold_out()

		# Modify moves to today and drops rooms -2 (Confirmed) and -3 (Checked In)
		mod = self.payload(rooms=2)
		mod["action"] = "modify"
		self.deliver(mod)

		self.assertEqual(self.received(f"{self.ref}-0").status, "Requested")
		self.assertEqual(self.received(f"{self.ref}-1").status, "Requested")
		self.assertEqual(self.received(f"{self.ref}-2").status, "Cancelled")
		dropped_inhouse.reload()
		self.assertEqual(dropped_inhouse.status, "Checked In")
		self.assertEqual(dropped_inhouse.channel_review_needed, 1)


class ChannexForeignContractBookingTestCase(IntegrationTestCase):
	"""A UGX hotel sells CORP, its USD contract plan, on Channex as rp-corp (a
	USD Channex rate plan). Finance's governed selling rate on the business
	date is 3,650 UGX a dollar; CORP rounds to 1,000 and fixes its rate at
	check-in. The OTA sold the first night at USD 130 and the second at USD
	120 (its own discount)."""

	def setUp(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		frappe.local.lang = "en"
		self.property = frappe.get_doc({"doctype": "Property",
			"property_name": "TEST CHX " + frappe.generate_hash(length=8),
			"country": "Uganda", "currency": "UGX"}).insert(ignore_permissions=True).name
		self.room_type = frappe.get_doc({"doctype": "Room Type", "property": self.property,
			"room_type_code": "STD", "room_type_name": "Standard", "base_price": 100000,
			"adults_capacity": 2}).insert(ignore_permissions=True).name
		for number in ("101", "102"):
			frappe.get_doc({"doctype": "Room", "property": self.property,
				"room_type": self.room_type, "room_number": number}).insert(ignore_permissions=True)
		self.corp = frappe.get_doc({"doctype": "Rate Plan", "property": self.property,
			"rate_plan_name": "Corporate USD", "code": "CORP" + frappe.generate_hash(length=4).upper(),
			"currency": "USD", "rounding_step": 1000, "fx_lock_policy": "Fixed at Check In",
			"modifier_type": "Percent", "modifier_value": 0}).insert(ignore_permissions=True).name
		self.conn = frappe.get_doc({"doctype": "Channel Manager Connection", "property": self.property,
			"provider": "Channex", "active": 1, "external_property_id": "cx-ota", "webhook_secret": "ut-hook",
			"ari_batch_seconds": 0}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Channel Room Mapping", "connection": self.conn.name,
			"room_type": self.room_type, "external_room_id": "rt-std", "external_rate_id": "rp-corp",
			"rate_plan": self.corp, "external_currency": "USD"}).insert(ignore_permissions=True)
		self.ref = "CHX-" + frappe.generate_hash(length=8)
		self.rates = {"USD": Decimal(3650)}
		fx = patch("kamra.pricing.posting_fx_rate", side_effect=self.fx)
		fx.start()
		self.addCleanup(fx.stop)
		self.nights = [nowdate(), str(add_days(nowdate(), 1))]

	def fx(self, property, currency, on_date=None):
		rate = self.rates.get(currency)
		return (rate, str(getdate(on_date or nowdate()))) if rate else None

	def payload(self, currency="USD", collect="property", status="new", amounts=("130.00", "120.00"), **extra):
		return {"event": "booking", "payload": dict({
			"status": status, "ota_reservation_code": self.ref, "ota_name": "BookingCom", "currency": currency,
			"arrival_date": self.nights[0], "departure_date": str(add_days(nowdate(), 2)),
			"payment_collect": collect, "customer": {"name": "Ana", "surname": "Viajera"},
			"rooms": [{"room_type_id": "rt-std", "rate_plan_id": "rp-corp",
			           "occupancy": {"adults": 2, "children": 0},
			           "days": dict(zip(self.nights, amounts, strict=True))}]}, **extra)}

	def deliver(self, payload):
		sp = "chx_test_" + frappe.generate_hash(length=8)
		frappe.db.savepoint(sp)
		rollback = frappe.db.rollback

		def worker_rollback(*args, **kwargs):
			return rollback(*args, **kwargs) if args or kwargs else rollback(save_point=sp)
		with patch.object(frappe.db, "commit", return_value=None), \
		     patch.object(frappe.db, "rollback", side_effect=worker_rollback):
			cm.process_webhook_events(self.conn.name, payload)

	def received(self):
		name = frappe.db.get_value("Reservation", {"property": self.property, "ota_ref": self.ref})
		self.assertTrue(name, "OTA booking was dropped instead of retained for resolution")
		return frappe.get_doc("Reservation", name)

	@staticmethod
	def gross_nights(doc) -> list:
		return [round(float(r.rate) * (1 + float(r.gst_rate or 0) / 100)) for r in doc.night_rates]

	def test_a_booking_in_its_plan_currency_is_confirmed_in_the_property_currency(self):
		self.deliver(self.payload())
		doc = self.received()
		self.assertEqual(doc.status, "Confirmed")
		self.assertEqual(doc.rate_plan, self.corp)
		# USD 130 x 3,650 = 474,500 and USD 120 x 3,650 = 438,000, to CORP's 1,000 step
		self.assertEqual(self.gross_nights(doc), [475000, 438000])
		self.assertEqual(float(doc.amount_after_tax), 913000)
		self.assertFalse(doc.auto_price)
		snapshot = frappe.parse_json(doc.contract_tariff)
		tariff = snapshot["tariff"]
		self.assertEqual((tariff["currency"], tariff["property_currency"], tariff["fx_rate"]), ("USD", "UGX", 3650))
		# the contract's per-night shape (R6): each night's own amounts, the
		# scalar figures the check-in night's
		self.assertEqual(tariff["nights"], [
			{"date": self.nights[0], "nightly": 130.0, "agreed_rate": 475000.0},
			{"date": self.nights[1], "nightly": 120.0, "agreed_rate": 438000.0}])
		self.assertEqual((tariff["nightly"], snapshot["agreed_rate"], float(doc.agreed_rate)), (130.0, 475000, 475000))
		self.assertEqual(pricing.contract_night_rates(doc), {self.nights[0]: 475000.0, self.nights[1]: 438000.0})
		# property-collected: the rate plan's own lock, as for a desk booking
		self.assertNotIn("fx_lock_policy", snapshot)
		self.assertEqual(pricing.reservation_fx_policy(doc), "Fixed at Check In")
		self.assertEqual(cm._sellable(self.property, self.room_type, nowdate(), add_days(nowdate(), 1)), 1)

	def test_an_ota_collected_or_virtual_card_booking_locks_its_rate_at_reservation(self):
		self.deliver(self.payload(collect="ota"))
		doc = self.received()
		self.assertEqual(doc.status, "Confirmed")
		self.assertEqual(frappe.parse_json(doc.contract_tariff)["fx_lock_policy"], "Fixed at Reservation")
		self.assertEqual(pricing.reservation_fx_policy(doc), "Fixed at Reservation")
		self.ref = "CHX-" + frappe.generate_hash(length=8)
		self.deliver(self.payload(collect="property", guarantee={"is_virtual": True}))
		self.assertEqual(pricing.reservation_fx_policy(self.received()), "Fixed at Reservation")

	def test_a_modify_converts_the_ota_s_new_nights(self):
		self.deliver(self.payload())
		self.deliver(self.payload(status="modified", amounts=("140.00", "140.00")))
		doc = self.received()
		self.assertEqual(doc.status, "Confirmed")
		self.assertEqual(self.gross_nights(doc), [511000, 511000])
		self.assertEqual(float(doc.amount_after_tax), 1022000)
		self.assertEqual([(n["date"], n["nightly"], n["agreed_rate"])
		                  for n in frappe.parse_json(doc.contract_tariff)["tariff"]["nights"]],
		                 [(self.nights[0], 140.0, 511000.0), (self.nights[1], 140.0, 511000.0)])

	def test_without_a_published_rate_the_booking_is_requested_for_finance(self):
		self.rates = {}
		self.deliver(self.payload())
		doc = self.received()
		self.assertEqual(doc.status, "Requested")
		self.assertEqual(float(doc.amount_after_tax or 0), 0)
		self.assertIn("exchange rate", doc.special_requests)
		self.assertIn("USD", doc.special_requests)
		self.assertIn("250", doc.special_requests)

	def test_a_booking_in_neither_currency_stays_requested(self):
		"""Retained behaviour (a guard): EUR is neither UGX nor CORP's USD."""
		self.deliver(self.payload(currency="EUR"))
		doc = self.received()
		self.assertEqual(doc.status, "Requested")
		self.assertEqual(float(doc.amount_after_tax or 0), 0)
		self.assertIn("EUR", doc.special_requests)
		self.assertIn("UGX", doc.special_requests)
		self.assertFalse(doc.contract_tariff)

	def test_a_booking_that_does_not_say_who_collects_follows_its_rate_plan(self):
		"""Neon R1 P1-1: Channex documents payment_collect null as "usually the
		property collects"; OPERA OXI keeps the rate code's exchange type
		unless the booking is prepaid, OTA-collected or a virtual card."""
		self.deliver(self.payload(collect=None))
		doc = self.received()
		self.assertEqual(doc.status, "Confirmed")
		self.assertNotIn("fx_lock_policy", frappe.parse_json(doc.contract_tariff))
		self.assertEqual(pricing.reservation_fx_policy(doc), "Fixed at Check In")

	def test_a_free_night_stays_the_ota_s_free_night(self):
		"""Neon R1 P1-4: Stay 2, Pay 1. The OTA's 0.00 night is its price, not
		a gap to fill with an even split."""
		self.deliver(self.payload(amounts=("130.00", "0.00")))
		doc = self.received()
		self.assertEqual(doc.status, "Confirmed")
		self.assertEqual(self.gross_nights(doc), [475000, 0])
		self.assertEqual(float(doc.amount_after_tax), 475000)
		self.assertEqual([(n["nightly"], n["agreed_rate"])
		                  for n in frappe.parse_json(doc.contract_tariff)["tariff"]["nights"]],
		                 [(130.0, 475000.0), (0.0, 0.0)])

	def test_a_booking_not_in_its_mapped_channel_currency_is_not_converted(self):
		"""Neon R1 P2-2: Kamra sells CORP on rp-corp in UGX, so a delivery
		labelled USD for those amounts is not read as dollars."""
		frappe.db.set_value("Channel Room Mapping", {"connection": self.conn.name, "external_rate_id": "rp-corp"},
		                    "external_currency", "UGX")
		self.deliver(self.payload(amounts=("474000.00", "438000.00")))
		doc = self.received()
		self.assertEqual(doc.status, "Requested")
		self.assertEqual(float(doc.amount_after_tax or 0), 0)
		self.assertFalse(doc.contract_tariff)
		self.assertIn("USD", doc.special_requests)

	def test_a_modify_into_the_property_currency_keeps_nothing_of_the_foreign_contract(self):
		"""Neon R1 P0-3: a modify is the booking's full new state. A stay the
		OTA now sells in UGX keeps nothing of the USD contract, so check-in
		and Night Audit cannot convert it again (OPERA OXI replaces a
		reservation's rate and currency on a modification)."""
		self.deliver(self.payload())
		self.deliver(self.payload(currency="UGX", status="modified", amounts=("480000.00", "440000.00")))
		doc = self.received()
		self.assertEqual(doc.status, "Confirmed")
		self.assertEqual(self.gross_nights(doc), [480000, 440000])
		self.assertFalse(doc.contract_tariff)
		self.assertFalse(doc.rate_plan)
		self.assertFalse(float(doc.agreed_rate or 0))
		self.assertEqual(pricing.contract_night_rates(doc), {})
		self.assertFalse(pricing.relock_contract_fx_at_check_in(doc))

	def test_a_modify_in_another_currency_is_requested_without_the_old_contract_or_its_nights(self):
		self.deliver(self.payload())
		self.deliver(self.payload(currency="EUR", status="modified"))
		doc = self.received()
		self.assertEqual(doc.status, "Requested")
		self.assertEqual(float(doc.amount_after_tax or 0), 0)
		self.assertFalse(doc.contract_tariff)
		self.assertFalse(doc.rate_plan)
		self.assertFalse(float(doc.agreed_rate or 0))
		self.assertEqual(list(doc.night_rates), [])
		self.assertIn("EUR", doc.special_requests)

	def test_a_modify_keeps_the_booking_s_exchange_rate_and_lock(self):
		"""Neon R1 P0-3: OPERA keeps a reservation's exchange basis on a
		modification; only the OTA's amounts are read again. With no rate
		published on the day of the modification, or a new one, the stay is
		converted at the booking's rate and stays fixed at reservation."""
		self.deliver(self.payload(collect="ota"))
		for rates in ({}, {"USD": Decimal(3750)}):
			with self.subTest(rates=rates):
				self.rates = rates
				self.deliver(self.payload(collect="ota", status="modified", amounts=("140.00", "130.00")))
				doc = self.received()
				self.assertEqual(doc.status, "Confirmed")
				# USD 140 and 130 at the booking's 3,650: 511,000 and 474,500 to 475,000
				self.assertEqual(self.gross_nights(doc), [511000, 475000])
				snapshot = frappe.parse_json(doc.contract_tariff)
				self.assertEqual((snapshot["tariff"]["fx_rate"], snapshot["fx_lock_policy"]),
				                 (3650, "Fixed at Reservation"))

	def test_foreign_currency_booking_still_enforces_ota_tax_hold(self):
		"""Merge RED -> GREEN (#39 + #36): a foreign-currency booking from an
		excluding-tax OTA with empty taxes[] (or an unread exclusive tax) must
		still be held as Requested by tax_hold, never Confirmed."""
		self.conn.reload()
		self.conn.append("ota_tax_basis", {"channel": "Expedia", "amounts": "Excluding tax"})
		self.conn.save(ignore_permissions=True)
		# Empty taxes[] on an Excluding tax OTA -> must hold as Requested
		self.deliver(self.payload(ota_name="Expedia"))
		doc = self.received()
		self.assertEqual(doc.status, "Requested")
		self.assertEqual(float(doc.amount_after_tax or 0), 0)
		self.assertFalse(doc.contract_tariff)
		self.assertIn(cm.ROOM_TAX_MISSING, doc.special_requests)

	def test_foreign_currency_booking_with_exclusive_room_tax_converts_gross_total(self):
		"""Merge RED -> GREEN (#39 + #36): a foreign-currency booking reporting
		net day prices plus an exclusive room tax (is_inclusive: false) must
		convert the gross total (days + room tax), never drop the tax."""
		p = self.payload(ota_name="Expedia", amounts=("100.00", "100.00"))
		p["payload"]["rooms"][0]["taxes"] = [
			{"name": "VAT", "type": "tax", "total_price": "36.00", "is_inclusive": False},
		]
		self.deliver(p)
		doc = self.received()
		self.assertEqual(doc.status, "Confirmed")
		# $100 + 18% VAT = $118/night; $118 * 3,650 = 430,700 -> rounded to 1,000 = 431,000/night (862,000 total)
		self.assertEqual(self.gross_nights(doc), [431000, 431000])
		self.assertEqual(float(doc.amount_after_tax), 862000)
		snapshot = frappe.parse_json(doc.contract_tariff)
		self.assertEqual([n["nightly"] for n in snapshot["tariff"]["nights"]], [118.0, 118.0])

	def test_inhouse_foreign_currency_modify_flags_review_with_foreign_currency_note(self):
		"""Merge RED -> GREEN (#46 + #36): an OTA modify on a Checked In foreign-
		currency stay must leave the stay untouched, flag channel_review_needed,
		and record the OTA's foreign amount/currency (never UGX labelled USD)."""
		self.deliver(self.payload(amounts=("130.00", "120.00")))
		doc = self.received()
		self.assertEqual(doc.status, "Confirmed")
		orig_nights = self.gross_nights(doc)
		frappe.db.set_value("Reservation", doc.name, "status", "Checked In")
		self.deliver(self.payload(status="modified", amounts=("140.00", "130.00")))
		doc.reload()
		self.assertEqual(doc.status, "Checked In")
		self.assertEqual(doc.channel_review_needed, 1)
		self.assertEqual(self.gross_nights(doc), orig_nights)
		self.assertIn("270.0 USD", doc.special_requests)
		self.assertNotIn("986000", doc.special_requests)
