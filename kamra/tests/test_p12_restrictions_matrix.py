"""P12: OPERA Restrictions Matrix & Editable BAR Calendar.

Live Frappe v16 / MariaDB 11.8.9 integration tests (zero DB mocks):
- 4-level specificity precedence:
  (property, rate_plan, room_type, date)
    > (property, rate_plan, NULL, date)
    > (property, NULL, room_type, date)
    > (property, NULL, NULL, date)
- Enforcement of Stop-Sell, CTA, CTD, MinLOS (arrival + stay-through), and MaxLOS
  across Desk (create_booking), Walk-in (walk_in), Direct Booking (public_api.search_stay
  & public_api.book), with Waitlist and unchanged-stay edit exemptions.
- Atomic bulk_grid_update for BAR + Restrictions across UGX (0dp), USD (2dp), KWD (3dp)
  with weekday filters, rate_calendar() & rate_options() integration, audit logging,
  and ARI outbox enqueue.
- Role and multi-property User Permission isolation.
"""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, nowdate

from kamra import api, public_api, rates
from kamra.restrictions import (
	StayRestrictionError,
	bulk_grid_update,
	bulk_set_restrictions,
	calendar_restrictions,
	evaluate_stay_restrictions,
	resolve_night_restrictions,
)
from kamra.settings import clear_cache, write_kv


def day(offset: int) -> str:
	return str(add_days(nowdate(), offset))


class RestrictionsMatrixTestCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		frappe.local.lang = "en"

	def tearDown(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- restore test user

	def _make_hotel(self, *, currency: str = "USD", country: str = "United States",
	                base_price: float = 120.0, tax: float = 0.0, with_channex: bool = False):
		if frappe.db.exists("Currency", currency):
			frappe.db.set_value("Currency", currency, "enabled", 1)
		tag = frappe.generate_hash(length=6).upper()
		prop = frappe.get_doc({
			"doctype": "Property",
			"property_name": f"TEST RST {currency} {tag}",
			"country": country,
			"currency": currency,
			"rates_include_tax": 0,
			"gst_rate_low": tax,
			"gst_rate_high": tax,
			"booking_engine_enabled": 1,
			"booking_mode": "Instant",
			"booking_payment_mode": "Pay at hotel",
			"business_date": nowdate(),
		}).insert(ignore_permissions=True).name
		write_kv(prop, "rate_plan_required", 0)
		clear_cache(prop)

		std = frappe.get_doc({
			"doctype": "Room Type",
			"property": prop,
			"room_type_code": f"STD{tag}",
			"room_type_name": "Standard",
			"base_price": base_price,
			"base_occupancy": 2,
			"adults_capacity": 2,
		}).insert(ignore_permissions=True).name

		dlx = frappe.get_doc({
			"doctype": "Room Type",
			"property": prop,
			"room_type_code": f"DLX{tag}",
			"room_type_name": "Deluxe",
			"base_price": base_price * 1.5,
			"base_occupancy": 2,
			"adults_capacity": 2,
		}).insert(ignore_permissions=True).name

		dlx_rooms = []
		for prefix, rt in (("S", std), ("D", dlx)):
			for i in range(3):
				room_doc = frappe.get_doc({
					"doctype": "Room",
					"property": prop,
					"room_type": rt,
					"room_number": f"{tag}-{prefix}{i + 1}",
					"housekeeping_status": "Clean",
				}).insert(ignore_permissions=True)
				if rt == dlx:
					dlx_rooms.append(room_doc.name)

		rp = frappe.get_doc({
			"doctype": "Rate Plan",
			"property": prop,
			"rate_plan_name": f"Corporate {tag}",
			"code": f"CRP{tag}",
			"modifier_type": "Percent",
			"modifier_value": -10,
		}).insert(ignore_permissions=True).name

		conn = None
		if with_channex:
			conn = frappe.get_doc({
				"doctype": "Channel Manager Connection",
				"property": prop,
				"provider": "Channex",
				"active": 1,
				"external_property_id": f"cx-{tag}",
				"webhook_secret": "rst-secret",
				"ari_batch_seconds": 0,
			}).insert(ignore_permissions=True)
			frappe.get_doc({
				"doctype": "Channel Room Mapping",
				"connection": conn.name,
				"room_type": std,
				"external_room_id": f"rt-std-{tag}",
				"external_rate_id": f"rp-std-{tag}",
				"external_currency": currency,
				"rate_plan": None,
			}).insert(ignore_permissions=True)
			frappe.db.delete("Channel ARI Outbox", {"connection": conn.name})

		return frappe._dict(
			property=prop, std=std, dlx=dlx, dlx_room=dlx_rooms[0],
			rate_plan=rp, conn=conn, tag=tag,
		)

	def _make_user(self, email: str, roles: list[str], allowed_property: str | None = None) -> str:
		if not frappe.db.exists("User", email):
			user = frappe.get_doc({
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"enabled": 1,
				"send_welcome_email": 0,
				"roles": [{"role": r} for r in roles],
			}).insert(ignore_permissions=True)
		else:
			user = frappe.get_doc("User", email)
			user.set("roles", [{"role": r} for r in roles])
			user.save(ignore_permissions=True)
		frappe.db.delete("User Permission", {"user": email, "allow": "Property"})
		if allowed_property:
			frappe.get_doc({
				"doctype": "User Permission",
				"user": email,
				"allow": "Property",
				"for_value": allowed_property,
				"apply_to_all_doctypes": 1,
			}).insert(ignore_permissions=True)
		return user.name

	def test_specificity_precedence_and_cta_ctd_minlos_maxlos_stop_sell(self):
		h = self._make_hotel(currency="USD", base_price=150.0)
		d5, d6, d7, d8 = day(5), day(6), day(7), day(8)

		# 1. Specificity hierarchy on d5:
		# House: min_los=2, max_los=10
		bulk_set_restrictions(h.property, d5, d5, min_los=2, max_los=10, reason="House floor")
		# Room Type STD: min_los=3, cta=1
		bulk_set_restrictions(h.property, d5, d5, room_type=h.std, min_los=3, cta=1, reason="STD peak")
		# Rate Plan + Room Type (CORP, STD): min_los=1, max_los=5, ctd=1
		bulk_set_restrictions(
			h.property, d5, d5, room_type=h.std, rate_plan=h.rate_plan,
			min_los=1, max_los=5, ctd=1, reason="Corp override",
		)

		r_house = resolve_night_restrictions(h.property, d5, room_type=h.dlx, rate_plan=None)
		self.assertEqual(r_house["min_los"], 2)
		self.assertEqual(r_house["max_los"], 10)
		self.assertFalse(r_house["cta"])
		self.assertEqual(r_house["source_scope"], "property")

		r_rt = resolve_night_restrictions(h.property, d5, room_type=h.std, rate_plan=None)
		self.assertEqual(r_rt["min_los"], 3)
		self.assertEqual(r_rt["max_los"], 10)
		self.assertTrue(r_rt["cta"])
		self.assertEqual(r_rt["source_scope"], "room_type")

		r_plan_rt = resolve_night_restrictions(h.property, d5, room_type=h.std, rate_plan=h.rate_plan)
		self.assertEqual(r_plan_rt["min_los"], 1)
		self.assertEqual(r_plan_rt["max_los"], 5)
		self.assertTrue(r_plan_rt["cta"])
		self.assertTrue(r_plan_rt["ctd"])
		self.assertEqual(r_plan_rt["source_scope"], "rate_plan_room_type")

		# 2. CTA on d6 for STD: arriving ON d6 blocked; arriving d5 and staying over d6 allowed!
		bulk_set_restrictions(h.property, d5, d5, room_type=h.std, min_los=0, cta=0, reason="clear d5")
		bulk_set_restrictions(h.property, d5, d5, min_los=0, max_los=0, reason="clear house d5")
		bulk_set_restrictions(h.property, d6, d6, room_type=h.std, cta=1, reason="CTA event")

		ev_cta = evaluate_stay_restrictions(h.property, h.std, d6, d7)
		self.assertFalse(ev_cta["allowed"])
		self.assertEqual(ev_cta["violations"][0]["code"], "cta")

		with self.assertRaises(StayRestrictionError):
			api.create_booking(
				property=h.property, room_type=h.std, check_in_date=d6, check_out_date=d7,
				guest_name="CTA Desk", phone="+12025550101", adults=2,
			)

		with self.assertRaises(frappe.ValidationError):
			public_api.book(
				property=h.property, room_type=h.std, check_in_date=d6, check_out_date=d7,
				guest_name="CTA Web", phone="+12025550102", adults=2,
			)

		# Stay-over through CTA date d6 (arriving d5, departing d7) IS allowed
		stay_over = api.create_booking(
			property=h.property, room_type=h.std, check_in_date=d5, check_out_date=d7,
			guest_name="Stay Over", phone="+12025550103", adults=2,
		)
		self.assertTrue(stay_over["reservation"])

		# 3. CTD on d8 for DLX: checkout ON d8 blocked; checkout on d9 allowed!
		bulk_set_restrictions(h.property, d8, d8, room_type=h.dlx, ctd=1, reason="CTD Sunday")
		with self.assertRaises(StayRestrictionError):
			api.create_booking(
				property=h.property, room_type=h.dlx, check_in_date=d6, check_out_date=d8,
				guest_name="CTD Blocked", phone="+12025550104", adults=2,
			)
		ctd_ok = api.create_booking(
			property=h.property, room_type=h.dlx, check_in_date=d6, check_out_date=day(9),
			guest_name="CTD Ok", phone="+12025550105", adults=2,
		)
		self.assertTrue(ctd_ok["reservation"])

		# 4. MinLOS=3 & MaxLOS=5 on d10 for STD across Desk, public_api.search_stay, and public_api.book
		d10, d12, d13, d16 = day(10), day(12), day(13), day(16)
		bulk_set_restrictions(h.property, d10, d10, room_type=h.std, min_los=3, max_los=5, reason="Holiday LOS")

		# 2-night search -> available=False with restriction reason
		search_2n = public_api.search_stay(h.property, d10, d12, adults=2)
		std_row = next(r for r in search_2n if r["room_type"] == h.std)
		self.assertFalse(std_row["available"])
		self.assertIn("3", std_row["reason"])

		# 2-night Desk & Web booking blocked
		with self.assertRaises(StayRestrictionError):
			api.create_booking(
				property=h.property, room_type=h.std, check_in_date=d10, check_out_date=d12,
				guest_name="MinLOS Fail", phone="+12025550106", adults=2,
			)
		with self.assertRaises(frappe.ValidationError):
			public_api.book(
				property=h.property, room_type=h.std, check_in_date=d10, check_out_date=d12,
				guest_name="MinLOS Web", phone="+12025550107", adults=2,
			)

		# 6-night booking exceeds MaxLOS=5 -> blocked
		with self.assertRaises(StayRestrictionError):
			api.create_booking(
				property=h.property, room_type=h.std, check_in_date=d10, check_out_date=d16,
				guest_name="MaxLOS Fail", phone="+12025550108", adults=2,
			)

		# 3-night booking succeeds
		ok_3n = api.create_booking(
			property=h.property, room_type=h.std, check_in_date=d10, check_out_date=d13,
			guest_name="MinLOS Ok", phone="+12025550109", adults=2,
		)
		self.assertTrue(ok_3n["reservation"])

		# Walk-in on today blocked when today has MinLOS=3
		d0, d1 = day(0), day(1)
		bulk_set_restrictions(h.property, d0, d0, room_type=h.dlx, min_los=3, reason="Walkin MinLOS")
		with self.assertRaises(StayRestrictionError):
			api.walk_in(
				property=h.property, room_type=h.dlx, room=h.dlx_room, check_out_date=d1,
				guest_name="Walkin Blocked", phone="+12025550110", adults=2,
			)

		# 5. MinLOS Stay-Through on d20: 2-night stay arriving d19 (covering d20) blocked!
		d19, d20, d21 = day(19), day(20), day(21)
		bulk_set_restrictions(
			h.property, d20, d20, room_type=h.dlx, min_los=3, is_stay_through=1, reason="Peak NYE",
		)
		ev_thru = evaluate_stay_restrictions(h.property, h.dlx, d19, d21)
		self.assertFalse(ev_thru["allowed"])
		self.assertEqual(ev_thru["violations"][0]["code"], "min_los_through")

		# 6. Stop-Sell on d25: Confirmed Desk/Web blocked, Waitlist permitted, existing stay edits pass!
		d25, d26 = day(25), day(26)
		pre_existing = api.create_booking(
			property=h.property, room_type=h.std, check_in_date=d25, check_out_date=d26,
			guest_name="Existing Guest", phone="+12025550111", adults=2,
		)
		bulk_set_restrictions(h.property, d25, d25, room_type=h.std, stop_sell=1, reason="Full closeout")

		with self.assertRaises(StayRestrictionError):
			api.create_booking(
				property=h.property, room_type=h.std, check_in_date=d25, check_out_date=d26,
				guest_name="StopSell Blocked", phone="+12025550112", adults=2,
			)

		# Waitlist stays remain exempt
		waitlist = api.create_booking(
			property=h.property, room_type=h.std, check_in_date=d25, check_out_date=d26,
			guest_name="Waitlist Guest", phone="+12025550113", adults=2,
			waitlist=1, status="Waitlist", assign_room=0,
		)
		self.assertTrue(waitlist["reservation"])

		# Existing stay can still update notes without dates/room_type/rate_plan changing
		doc = frappe.get_doc("Reservation", pre_existing["reservation"])
		doc.special_requests = "Late arrival note after stop-sell"
		doc.save(ignore_permissions=True)
		self.assertEqual(doc.special_requests, "Late arrival note after stop-sell")

	def test_bulk_grid_update_precisions_0_2_3dp_and_weekdays(self):
		d3, d4, d5 = day(3), day(4), day(5)
		d3_wd = getdate(d3).strftime("%a").lower()

		cases = [
			("UGX", "Uganda", 250000, 350000.4, 350000.0),
			("USD", "United States", 100.00, 129.555, 129.56),
			("KWD", "Kuwait", 30.000, 45.1254, 45.125),
		]
		for currency, country, base_price, raw_new_rate, expected_rate in cases:
			with self.subTest(currency=currency):
				h = self._make_hotel(
					currency=currency, country=country, base_price=base_price, with_channex=True,
				)
				out = bulk_grid_update(
					property=h.property,
					rate_updates=[{
						"room_type": h.std,
						"start_date": d3,
						"end_date": d5,
						"days_of_week": [d3_wd],
						"rate": raw_new_rate,
					}],
					restriction_updates=[{
						"room_type": h.std,
						"start_date": d3,
						"end_date": d5,
						"days_of_week": [d3_wd],
						"min_los": 2,
						"max_los": 6,
						"cta": 1,
						"stop_sell": 0,
					}],
					reason=f"Grid edit {currency}",
				)
				self.assertTrue(out["ok"])
				self.assertEqual(out["rates_updated"], 1)
				self.assertEqual(out["restrictions_updated"], 1)
				self.assertTrue(out["ari_queued"])

				# rate_calendar returns exact quantized BAR and merged restrictions on d3 only
				cal = rates.rate_calendar(h.property, from_date=d3, days=3)
				self.assertTrue(cal["can_edit"])
				std_row = next(r for r in cal["room_types"] if r["room_type"] == h.std)
				n0, n1 = std_row["nights"][0], std_row["nights"][1]
				self.assertAlmostEqual(n0["rate"], expected_rate, places=3)
				self.assertTrue(n0.get("is_restricted"))
				self.assertEqual(n0.get("min_los"), 2)
				self.assertEqual(n0.get("max_los"), 6)
				self.assertTrue(n0.get("cta"))
				self.assertFalse(n1.get("is_restricted", False))

				# calendar_restrictions endpoint returns structured matrix
				mat = calendar_restrictions(h.property, from_date=d3, days=3, room_type=h.std)
				self.assertEqual(mat["cells"][h.std][d3]["min_los"], 2)
				self.assertTrue(mat["cells"][h.std][d3]["cta"])

				# rate_options flags restricted plan on d3->d4 (CTA + MinLOS=2)
				opts = rates.rate_options(h.property, h.std, d3, d4)
				rp_row = next(p for p in opts["plans"] if p["name"] == h.rate_plan)
				self.assertTrue(rp_row.get("restricted"))
				self.assertTrue(rp_row.get("restriction_reason"))

				# ARI outbox queued for restrictions
				queued = frappe.get_all(
					"Channel ARI Outbox",
					filters={"connection": h.conn.name, "kinds": "restrictions"},
					fields=["name", "status"],
				)
				self.assertGreaterEqual(len(queued), 1)

	def test_bulk_grid_update_refuses_unauthorized_and_other_property(self):
		h_a = self._make_hotel(currency="USD", base_price=120.0)
		h_b = self._make_hotel(currency="USD", base_price=140.0)
		d4 = day(4)

		fd_user = self._make_user(f"fd-{h_a.tag.lower()}@example.test", ["Front Desk"], h_a.property)
		rm_b_user = self._make_user(f"rm-{h_b.tag.lower()}@example.test", ["Revenue Manager"], h_b.property)

		# Front Desk cannot edit restrictions or BAR grid
		frappe.set_user(fd_user)  # nosemgrep: frappe-setuser -- permission boundary test
		with self.assertRaises(frappe.PermissionError):
			bulk_set_restrictions(h_a.property, d4, d4, room_type=h_a.std, stop_sell=1)
		with self.assertRaises(frappe.PermissionError):
			bulk_grid_update(h_a.property, rate_updates=[{"room_type": h_a.std, "date": d4, "rate": 200}])

		# Revenue Manager scoped to Property B cannot read or mutate Property A
		frappe.set_user(rm_b_user)  # nosemgrep: frappe-setuser -- permission boundary test
		with self.assertRaises(frappe.PermissionError):
			calendar_restrictions(h_a.property, d4, days=3)
		with self.assertRaises(frappe.PermissionError):
			bulk_set_restrictions(h_a.property, d4, d4, room_type=h_a.std, stop_sell=1)
		with self.assertRaises(frappe.PermissionError):
			bulk_grid_update(h_a.property, rate_updates=[{"room_type": h_a.std, "date": d4, "rate": 200}])
