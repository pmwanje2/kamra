"""Group pickups cover the guest's physical stay, even when all booked nights are billed."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, nowdate

from kamra import api
from kamra.checkout import early_departure_plan
from kamra.folio import post_remaining_nights
from kamra.group_blocks import group_nights, restored_pickup_nights, unpicked_by_night
from kamra.tests.audit_fixtures import guest_folio, make_reservation
from kamra.tests.test_p7_direct_booking_cart_r8 import PickupFixture


class TestGroupPickupDeparture(PickupFixture, IntegrationTestCase):
	def assert_pickup(self, group, stay, occupied):
		expected = {d: int(d not in occupied) for d in group_nights(self.ci, self.co)}
		for locking in (False, True):
			with self.subTest(for_update=locking):
				holds = unpicked_by_night(group.name, self.standard, for_update=locking)
				for day, count in expected.items():
					self.assertEqual(holds[day], count, day)
		after = frappe._dict(property=self.property, room_type=self.standard, group_booking=None)
		self.assertEqual(restored_pickup_nights(stay, after), occupied)

	def test_early_checkout_bills_three_nights_but_only_picks_up_one(self):
		today = nowdate()
		self.ci, self.co = add_days(today, -1), add_days(today, 2)
		group = self.group(self.standard, 1)
		group.db_set("cutoff_date", today)
		room = frappe.db.get_value("Room", {"property": self.property, "room_type": self.standard})
		stay = make_reservation(self.property, self.standard, self.ci, self.co, room=room,
		                        status="Checked In", group_booking=group.name)
		self.assertEqual(early_departure_plan(stay)["policy"], "All Nights")
		self.assert_pickup(group, stay, set(group_nights(self.ci, self.co)))
		post_remaining_nights(stay)
		folio = guest_folio(stay.name)
		balance = frappe.db.get_value("Folio", folio, "balance")
		if balance:
			api.add_folio_payment(folio, "Cash", balance)
		self.assertTrue(api.check_out(stay.name)["ok"])
		stay.reload()
		self.assertEqual(stay.status, "Checked Out")
		self.assertEqual(str(stay.check_out_date), self.co)
		self.assertEqual(str(getdate(stay.actual_check_out)), today)
		self.assert_pickup(group, stay, {self.ci})

	def test_departure_boundaries_and_historical_fallback(self):
		group = self.group(self.standard, 1)
		stay = self.pickup(self.standard, group)
		departure = add_days(self.ci, 1)
		for booked, actual, occupied in (
			(departure, departure, {self.ci}),             # on time
			(departure, self.co, {self.ci}),               # cap at booked end
			(departure, None, {self.ci}),                  # legacy checkout
			(self.ci, self.ci, {self.ci}),                 # day use
			(self.co, self.ci, {self.ci}),                 # same-day early checkout
			(self.co, add_days(self.ci, -1), {self.ci}),   # inconsistent imported timestamp
		):
			with self.subTest(booked=booked, actual=actual):
				stay.db_set({"status": "Checked Out", "check_out_date": booked, "actual_check_out": actual})
				self.assert_pickup(group, stay, occupied)

	def test_only_checked_out_stays_use_the_actual_departure(self):
		group = self.group(self.standard, 1)
		stay = self.pickup(self.standard, group)
		for status in ("Confirmed", "Checked In", "Waitlist", "Cancelled", "No Show"):
			with self.subTest(status=status):
				stay.db_set({"status": status, "actual_check_out": self.ci})
				picked = set(group_nights(self.ci, self.co)) if status in ("Confirmed", "Checked In") else set()
				self.assert_pickup(group, stay, picked)
