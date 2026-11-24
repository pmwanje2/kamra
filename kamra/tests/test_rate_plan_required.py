"""Rate-plan-required policy: seeding, readiness, non-repricing of legacy stays,
and resolution across desk, walk-in, day-use, group pick-up, OTA and public
bookings (OPERA Cloud Rate Code Mandatory parity)."""

from unittest.mock import patch

import frappe
from frappe.utils import add_days, nowdate

from kamra import api, channel_manager as cm, public_api, settings
from kamra.channels import channex
from kamra.settings import clear_cache, get_setting, write_kv
from kamra.tests.audit_fixtures import AuditTestCase


class RatePlanRequiredTest(AuditTestCase):
	properties = ("TEST K3 POLICY",)

	def setUp(self):
		super().setUp()
		self.addCleanup(clear_cache, self.properties[0])
		bridge_patch = patch("kamra.erp_bridge.get_bridge_script", return_value=None)
		bridge_patch.start()
		self.addCleanup(bridge_patch.stop)
		frappe.local.lang = "en"
		self.property = frappe.get_doc({
			"doctype": "Property",
			"property_name": "TEST K3 POLICY",
			"country": "Uganda",
			"currency": "UGX",
			"booking_engine_enabled": 1,
		}).insert().name
		self.room_type = frappe.get_doc({
			"doctype": "Room Type",
			"property": self.property,
			"room_type_code": "STD",
			"room_type_name": "Standard",
			"base_price": 1000,
			"adults_capacity": 2,
		}).insert().name
		self.room = frappe.get_doc({
			"doctype": "Room",
			"property": self.property,
			"room_type": self.room_type,
			"room_number": "101",
		}).insert().name
		self.room2 = frappe.get_doc({
			"doctype": "Room",
			"property": self.property,
			"room_type": self.room_type,
			"room_number": "102",
		}).insert().name
		self.today = nowdate()
		self.tomorrow = str(add_days(self.today, 1))
		self.day3 = str(add_days(self.today, 2))

	def require_plan(self, on: int = 1):
		write_kv(self.property, "rate_plan_required", on)
		clear_cache(self.property)

	def make_plan(self, code: str, name: str, **extra) -> str:
		return frappe.get_doc({
			"doctype": "Rate Plan",
			"property": self.property,
			"rate_plan_name": name,
			"code": code,
			"modifier_type": "Percent",
			"modifier_value": 0,
			**extra,
		}).insert().name

	def book(self, check_in_date=None, check_out_date=None, **kwargs):
		return api.create_booking(
			self.property,
			self.room_type,
			check_in_date or self.today,
			check_out_date or self.tomorrow,
			"K3 Booking Guest",
			**kwargs,
		)

	def walk_in(self, **kwargs):
		return api.walk_in(
			self.property, self.room_type, self.room, self.tomorrow,
			"K3 Walk-in Guest", defer_check_in=1, **kwargs,
		)

	# ── Requirement 2: seeded once on new properties, never overwritten, readiness ──

	def test_new_property_seeds_rate_plan_required_once_and_never_overwrites(self):
		self.assertEqual(get_setting(self.property, "rate_plan_required"), 1)
		self.assertTrue(frappe.db.exists(
			"Property Setting",
			{"property": self.property, "setting_key": "rate_plan_required"},
		))
		# Admin switches it off -> updating Property must NEVER overwrite it back to 1
		settings.save_settings(self.property, "front-desk", {"rate_plan_required": 0})
		self.assertEqual(get_setting(self.property, "rate_plan_required"), 0)
		prop = frappe.get_doc("Property", self.property)
		prop.city = "Kampala"
		prop.save(ignore_permissions=True)
		clear_cache(self.property)
		self.assertEqual(get_setting(self.property, "rate_plan_required"), 0)

	def test_existing_property_without_setting_allows_booking_without_plan_and_never_reprices(self):
		# Simulate an existing pre-upgrade property: no Property Setting row for rate_plan_required
		frappe.db.delete(
			"Property Setting",
			{"property": self.property, "setting_key": "rate_plan_required"},
		)
		clear_cache(self.property)
		self.assertEqual(get_setting(self.property, "rate_plan_required"), 0)

		legacy = self.book()
		res_name = legacy["reservation"]
		res = frappe.get_doc("Reservation", res_name)
		self.assertFalse(res.rate_plan)
		before_total = float(res.amount_after_tax)
		before_nights = [(str(n.stay_date), float(n.rate)) for n in res.night_rates]

		# Readiness lists this future booking without a plan
		readiness = settings.rate_plan_readiness(self.property)
		self.assertIs(readiness["rate_plan_required"], False)
		self.assertEqual(readiness["total_without_plan"], 1)
		self.assertEqual([r["name"] for r in readiness["future_bookings_without_plan"]], [res_name])

		# Switch rate_plan_required ON -> existing reservation is NEVER repriced, even when edited/saved
		self.require_plan(1)
		res.reload()
		res.special_requests = "Early arrival requested"
		res.save(ignore_permissions=True)
		res.reload()
		self.assertFalse(res.rate_plan)
		self.assertEqual(float(res.amount_after_tax), before_total)
		self.assertEqual([(str(n.stay_date), float(n.rate)) for n in res.night_rates], before_nights)

	# ── Requirement 1: desk, walk-in, day-use, group, OTA, public, comp/house/PM ──

	def test_booking_requires_plan_before_creating_guest(self):
		self.require_plan(1)
		with self.assertRaisesRegex(frappe.ValidationError, "KMR-RATE-REQ-001") as ctx:
			self.book()
		self.assertIn("Rate Plans", str(ctx.exception))
		self.assertFalse(frappe.db.exists("Guest", {"full_name": "K3 Booking Guest"}))

	def test_walk_in_requires_plan_before_creating_guest_and_uses_default_when_present(self):
		self.require_plan(1)
		with self.assertRaisesRegex(frappe.ValidationError, "KMR-RATE-REQ-001"):
			self.walk_in()
		self.assertFalse(frappe.db.exists("Guest", {"full_name": "K3 Walk-in Guest"}))

		default = self.make_plan("BAR", "Best Available", is_default=1)
		out = self.walk_in()
		self.assertEqual(frappe.db.get_value("Reservation", out["reservation"], "rate_plan"), default)

	def test_resolution_order_chosen_then_company_negotiated_then_default(self):
		self.require_plan(1)
		default = self.make_plan("BAR", "Best Available", is_default=1)
		corp_plan = self.make_plan("CORP", "Corporate", modifier_value=-10)
		chosen = self.make_plan("PROMO", "Promo", modifier_value=-15)
		company = frappe.get_doc({
			"doctype": "Corporate Account",
			"company_name": "K3 Corp " + frappe.generate_hash(length=6),
			"property": self.property,
			"negotiated_rate_plan": corp_plan,
		}).insert().name

		# No plan, no company -> property default
		r1 = self.book(check_in_date=self.tomorrow, check_out_date=self.day3)
		self.assertEqual(frappe.db.get_value("Reservation", r1["reservation"], "rate_plan"), default)

		# Company with negotiated plan -> company plan beats default
		r2 = self.book(company=company, check_in_date=self.day3, check_out_date=str(add_days(self.today, 3)))
		self.assertEqual(frappe.db.get_value("Reservation", r2["reservation"], "rate_plan"), corp_plan)

		# Explicit chosen plan -> beats company plan and default
		r3 = self.book(
			company=company, rate_plan=chosen,
			check_in_date=str(add_days(self.today, 3)),
			check_out_date=str(add_days(self.today, 4)),
		)
		self.assertEqual(frappe.db.get_value("Reservation", r3["reservation"], "rate_plan"), chosen)

	def test_group_pickup_and_rooming_list_resolve_rate_plan_or_refuse(self):
		self.require_plan(1)
		gb = frappe.get_doc({
			"doctype": "Group Booking",
			"property": self.property,
			"group_name": "K3 Summit",
			"check_in_date": self.tomorrow,
			"check_out_date": self.day3,
			"status": "Confirmed",
			"blocks": [{"room_type": self.room_type, "rooms_blocked": 2}],
		}).insert()

		# No rate plan configured -> pickup refuses cleanly
		with self.assertRaisesRegex(frappe.ValidationError, "KMR-RATE-REQ-001"):
			api.pickup_group_room(gb.name, self.room_type, "Group Guest One")

		# With property default plan -> pickup resolves and stamps it
		default = self.make_plan("BAR", "Best Available", is_default=1)
		picked = api.pickup_group_room(gb.name, self.room_type, "Group Guest One")
		self.assertEqual(frappe.db.get_value("Reservation", picked["reservation"], "rate_plan"), default)

		# Explicit rate_plan on pickup overrides default
		grp_plan = self.make_plan("GRP", "Group Rate", modifier_value=-12)
		picked2 = api.pickup_group_room(
			gb.name, self.room_type, "Group Guest Two", rate_plan=grp_plan,
		)
		self.assertEqual(frappe.db.get_value("Reservation", picked2["reservation"], "rate_plan"), grp_plan)

	def test_ota_channel_and_desk_ota_resolve_rate_plan_or_refuse(self):
		self.require_plan(1)
		conn = frappe.get_doc({
			"doctype": "Channel Manager Connection",
			"property": self.property,
			"provider": "Channex",
			"active": 1,
			"external_property_id": "chx-prop-1",
		}).insert(ignore_permissions=True)
		mapping = frappe.get_doc({
			"doctype": "Channel Room Mapping",
			"connection": conn.name,
			"room_type": self.room_type,
			"external_room_id": "rm-std",
			"external_rate_id": "rp-ota",
		}).insert(ignore_permissions=True)

		channex_payload = {
			"event": "booking",
			"payload": {
				"status": "new",
				"ota_reservation_code": "CHX-REF-1",
				"ota_name": "Booking.com",
				"currency": "UGX",
				"customer": {"name": "Channel", "surname": "Guest"},
				"rooms": [{
					"room_type_id": "rm-std",
					"rate_plan_id": "rp-ota",
					"checkin_date": self.tomorrow,
					"checkout_date": self.day3,
					"occupancy": {"adults": 2, "children": 0},
					"days": {self.tomorrow: "1200"},
				}],
			},
		}
		events = channex.parse_webhook(conn, channex_payload)
		self.assertEqual(events[0]["rate_plan_external_id"], "rp-ota")

		# Neither mapped rate plan nor default plan -> refused plainly, no Confirmed stay
		refused = cm._apply_event(conn, dict(events[0]))
		self.assertEqual(refused["result"], "rate_plan_refused")
		self.assertIn("Rate Plans", refused["reason"])
		self.assertFalse(frappe.db.exists("Reservation", {"property": self.property, "ota_ref": "CHX-REF-1"}))

		# Desk OTA booking also refuses without a plan
		with self.assertRaisesRegex(frappe.ValidationError, "KMR-RATE-REQ-001"):
			self.book(booked_via="Booking.com", ota_ref="DESK-OTA-1", ota_total=1200)

		# Map a rate plan on Channel Room Mapping -> OTA booking succeeds and stores it
		ota_plan = self.make_plan("OTA", "OTA Standard")
		mapping.rate_plan = ota_plan
		mapping.save(ignore_permissions=True)
		accepted = cm._apply_event(conn, dict(events[0]))
		self.assertEqual(accepted["result"], "booked")
		self.assertEqual(frappe.db.get_value("Reservation", accepted["reservation"], "rate_plan"), ota_plan)

	def test_public_booking_and_search_resolve_default_plan_or_refuse(self):
		self.require_plan(1)
		ci, co = str(add_days(self.today, 5)), str(add_days(self.today, 7))

		# No default rate plan -> search marks unavailable with plain reason; book refuses
		rows = public_api.search_stay(self.property, ci, co, 2, 0)
		self.assertEqual(len(rows), 1)
		self.assertIs(rows[0]["available"], False)
		self.assertIsNone(rows[0]["quote"])
		self.assertTrue(rows[0].get("reason"))
		with self.assertRaisesRegex(frappe.ValidationError, "KMR-RATE-REQ-001"):
			public_api.book(self.property, self.room_type, ci, co, "Web Guest", "+256700000101")

		# With default rate plan -> search quotes & book stamps default rate plan
		default = self.make_plan("WEB", "Standard Web", is_default=1)
		rows = public_api.search_stay(self.property, ci, co, 2, 0)
		self.assertIs(rows[0]["available"], True)
		self.assertGreater(rows[0]["quote"]["amount_after_tax"], 0)
		booked = public_api.book(self.property, self.room_type, ci, co, "Web Guest", "+256700000101")
		self.assertEqual(frappe.db.get_value("Reservation", booked["reservation"], "rate_plan"), default)

	def test_complimentary_house_use_and_pm_account_follow_opera(self):
		self.require_plan(1)
		comp = self.make_plan("COMP", "Complimentary", modifier_type="Absolute", modifier_value=0)
		house = self.make_plan("HOUSE", "House Use", modifier_type="Absolute", modifier_value=0)

		r_comp = self.book(rate_plan=comp)
		comp_doc = frappe.get_doc("Reservation", r_comp["reservation"])
		self.assertEqual(comp_doc.rate_plan, comp)
		self.assertEqual(float(comp_doc.amount_after_tax), 0)

		r_house = self.book(
			rate_plan=house,
			check_in_date=self.tomorrow,
			check_out_date=self.day3,
		)
		house_doc = frappe.get_doc("Reservation", r_house["reservation"])
		self.assertEqual(house_doc.rate_plan, house)
		self.assertEqual(float(house_doc.amount_after_tax), 0)

		# Posting Master (Account folio) has no Reservation and works cleanly
		pm = frappe.get_doc({
			"doctype": "Folio",
			"property": self.property,
			"folio_type": "Account",
			"status": "Open",
		}).insert(ignore_permissions=True)
		self.assertEqual(pm.folio_type, "Account")

	# ── Requirement 3: Room Type saves without base_price when setting is on; ERP tax flag ──

	def test_room_type_allows_omitting_base_price_only_when_rate_plan_required(self):
		self.require_plan(1)
		rt = frappe.get_doc({
			"doctype": "Room Type",
			"property": self.property,
			"room_type_code": "DLX",
			"room_type_name": "Deluxe",
			"adults_capacity": 2,
		}).insert()
		self.assertEqual(float(rt.base_price or 0), 0.0)

		self.require_plan(0)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({
				"doctype": "Room Type",
				"property": self.property,
				"room_type_code": "STE",
				"room_type_name": "Suite",
				"adults_capacity": 2,
			}).insert()

	def test_property_locale_reports_erp_tax_managed_only_when_erp_tax_bridge_is_active(self):
		with patch("kamra.erp_bridge.get_bridge_script", return_value=None):
			loc = api.property_locale(self.property)
			self.assertIs(loc["erp_tax_managed"], False)
		with patch("kamra.erp_bridge.get_bridge_script", return_value=object()), \
		     patch("kamra.pricing.tax_basis", return_value={"determined": 0, "label": "", "rate": 0, "inclusive": 0}):
			loc = api.property_locale(self.property)
			self.assertIs(loc["erp_tax_managed"], True)
