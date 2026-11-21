"""#57: the daytime manager flash projects ADR and RevPAR from the rooms in
house (OPERA's flash shows in-house rates before the night audit).

Room nights post at the night audit, so for the open business date the
posted ADR and RevPAR are 0 by construction while the rooms are occupied.
With occupancy on the in-house basis, the flash adds projected room
revenue, ADR and RevPAR from the in-house stays' nightly rates for that
date - the rate the audit will post - and leaves the posted figures as
they are."""

from __future__ import annotations

from kamra import folio as folio_mod
from kamra.reports import _day_stats, manager_flash
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	YESTERDAY,
	AuditTestCase,
	d,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)


class FlashProjectedTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.suite = make_room_type(PROP_A, "STE", base_price=3000)
		self.rooms = [make_room(PROP_A, self.rt, f"10{i}") for i in range(1, 4)] + [
			make_room(PROP_A, self.suite, "201")]
		# two in house tonight: 1,000 and 3,000; one arriving later
		self.a = make_reservation(PROP_A, self.rt, YESTERDAY, d(2),
		                          status="Checked In", room=self.rooms[0])
		self.b = make_reservation(PROP_A, self.suite, TODAY, d(1),
		                          status="Checked In", room=self.rooms[3])
		make_reservation(PROP_A, self.rt, d(3), d(4), room=self.rooms[1])

	def test_in_house_day_projects_room_revenue_adr_and_revpar(self):
		day = _day_stats(PROP_A, TODAY, 4)
		self.assertEqual(day["occupancy_basis"], "in_house")
		self.assertEqual(day["rooms_occupied"], 2)
		# posted figures are untouched: nothing has posted for tonight yet
		self.assertEqual(day["rooms_sold"], 0)
		self.assertEqual(day["room_revenue"], 0)
		self.assertEqual(day["adr"], 0)
		self.assertEqual(day["revpar"], 0)
		# projected from the in-house stays' rates for tonight
		self.assertEqual(day["projected_room_revenue"], 4000)
		self.assertEqual(day["projected_adr"], 2000)
		self.assertEqual(day["projected_revpar"], 1000)

	def test_a_night_already_posted_counts_once_at_its_posted_rate(self):
		folio_mod.post_room_night(self.a, TODAY)
		day = _day_stats(PROP_A, TODAY, 4)
		self.assertEqual(day["room_revenue"], 1000)
		self.assertEqual(day["projected_room_revenue"], 4000)
		self.assertEqual(day["projected_adr"], 2000)

	def test_projection_bills_the_locked_rate_not_todays_card(self):
		"""A rate-card change after booking never re-prices a confirmed stay;
		the projection uses the rate the audit will post."""
		import frappe
		frappe.db.set_value("Room Type", self.rt, "base_price", 5000)
		day = _day_stats(PROP_A, TODAY, 4)
		self.assertEqual(day["projected_room_revenue"], 4000)

	def test_a_closed_date_has_no_projection(self):
		day = _day_stats(PROP_A, YESTERDAY, 4)
		self.assertEqual(day["occupancy_basis"], "posted")
		self.assertNotIn("projected_adr", day)

	def test_a_stay_departing_on_the_business_date_is_not_projected(self):
		"""The night audit posts a room night only for a stay with arrival <=
		business date < departure (folio.run_night_audit). A guest checking
		out today has no night tonight, so the projection leaves it out."""
		make_reservation(PROP_A, self.rt, YESTERDAY, TODAY, status="Checked In",
		                 room=self.rooms[2])
		day = _day_stats(PROP_A, TODAY, 4)
		self.assertEqual(day["projected_room_revenue"], 4000)
		# ADR over the rooms that will post tonight, not every room in house
		self.assertEqual(day["projected_adr"], 2000)
		self.assertEqual(day["projected_revpar"], 1000)

	def test_the_audit_pack_statistics_carry_no_projection(self):
		"""The pack's statistics are the posted figures of the closed date;
		a projection is a daytime figure and is not kept there."""
		from kamra import night_audit
		pack = night_audit.build_pack(PROP_A, TODAY, checks=[], acknowledgements=[],
		                              summary={}, no_shows=[], held_late=[])
		self.assertEqual(pack["statistics"]["occupancy_basis"], "in_house")
		self.assertFalse([k for k in pack["statistics"] if k.startswith("projected_")])

	def test_the_flash_today_carries_the_projection(self):
		flash = manager_flash(PROP_A, TODAY)
		self.assertEqual(flash["today"]["projected_adr"], 2000)
		self.assertEqual(flash["today"]["projected_revpar"], 1000)
		self.assertEqual(flash["today"]["projected_room_revenue"], 4000)
		self.assertEqual(flash["today"]["adr"], 0)
