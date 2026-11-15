"""Reversing a check-in made by mistake, the same business day.

api.reverse_check_in (OPERA Cloud "Reverse Check In"): allowed while the
stay's arrival date (check_in_date) is still the property's business date,
so a reversal made after midnight and before the night audit is admitted,
and only when nothing was posted or paid (any Folio Charge or Folio Payment
row refuses, a void pair included), no restaurant or laundry bill is open
and no deposit is held. Front Desk, Finance and Kamra Agent may call it.
The stay goes back to Confirmed (arrival), the room is released and marked
Dirty for housekeeping to inspect, and the reason goes on the action log.
From there the desk can check the guest in again or cancel the booking.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe

from kamra import api
from kamra import folio as folio_mod
from kamra.door_lock import DoorLockAdapter, KeyResult
from kamra.tests.audit_fixtures import (
	PROP_A,
	PROP_B,
	TODAY,
	AuditTestCase,
	d,
	ensure_user,
	guest_folio,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

DESK = "ut-reverse-desk@kamra.local"
FINANCE = "ut-reverse-finance@kamra.local"
AGENT = "ut-reverse-agent@kamra.local"


class _Lock(DoorLockAdapter):
	"""A connected lock system that records what it was asked to do."""

	key = "ut-lock"
	label = "UT lock"

	def __init__(self):
		self.revoked = []

	def issue_key(self, request):
		return KeyResult(ok=True, adapter=self.key)

	def revoke_keys(self, property, reservation, room):
		self.revoked.append((property, reservation, room))
		return KeyResult(ok=True, adapter=self.key, message="UT keys cancelled")


class ReverseCheckInTestCase(AuditTestCase):
	properties = (PROP_A, PROP_B)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.room = make_room(PROP_A, self.rt, "101")
		frappe.db.set_value("Room", self.room, "housekeeping_status", "Clean")
		self.res = make_reservation(PROP_A, self.rt, TODAY, d(2),
		                            status="Checked In", room=self.room,
		                            occupants=[{"full_name": "UT Partner"}])
		# checked in today, on the property's business date
		frappe.db.set_value("Reservation", self.res.name, "checked_in_at",
		                    f"{TODAY} 10:00:00")
		self.folio = guest_folio(self.res.name)

	def status(self):
		return frappe.db.get_value("Reservation", self.res.name, "status")

	def test_same_day_zero_balance_goes_back_to_arrival(self):
		out = api.reverse_check_in(self.res.name, "Wrong guest checked in")
		self.assertEqual(out["status"], "Confirmed")
		self.assertEqual(self.status(), "Confirmed")
		self.assertEqual(frappe.db.get_value("Reservation", self.res.name,
		                                     ["actual_check_in", "checked_in_at", "checked_in_by"]),
		                 (None, None, None))
		room = frappe.db.get_value("Room", self.room,
		                           ["occupancy_status", "housekeeping_status"])
		self.assertEqual(room, ("Vacant", "Dirty"))
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"action_type": "reverse_check_in", "reference_name": self.res.name}))
		# the desk can check the guest in again
		api.check_in(self.res.name)
		self.assertEqual(self.status(), "Checked In")

	def test_then_the_booking_can_be_cancelled(self):
		api.reverse_check_in(self.res.name, "Test booking")
		api.cancel_reservation(self.res.name, reason="Guest request", waive_fee=1)
		self.assertEqual(self.status(), "Cancelled")

	def test_refused_when_a_charge_is_on_the_bill(self):
		api.add_folio_charge(guest_folio(self.res.name), "Laundry", "UT laundry", 300)
		with self.assertRaisesRegex(frappe.ValidationError, "charge or payment is on the bill"):
			api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(self.status(), "Checked In")

	def test_refused_when_a_payment_is_on_the_bill(self):
		api.add_folio_payment(guest_folio(self.res.name), "Cash", 500, kind="Advance")
		with self.assertRaisesRegex(frappe.ValidationError, "charge or payment is on the bill"):
			api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(self.status(), "Checked In")

	def test_refused_after_the_night_audit(self):
		frappe.db.set_value("Property", PROP_A, "business_date", d(1))
		with self.assertRaisesRegex(frappe.ValidationError, "night audit has run"):
			api.reverse_check_in(self.res.name, "Too late")
		self.assertEqual(self.status(), "Checked In")

	def test_needs_a_reason_and_a_checked_in_stay(self):
		with self.assertRaisesRegex(frappe.ValidationError, "why"):
			api.reverse_check_in(self.res.name, " ")
		api.reverse_check_in(self.res.name, "Wrong room")
		with self.assertRaisesRegex(frappe.ValidationError, "checked-in"):
			api.reverse_check_in(self.res.name, "Again")

	def test_offered_only_when_allowed(self):
		actions = api.reservation_detail(self.res.name)["actions"]
		self.assertTrue(actions.get("can_reverse_check_in"))
		frappe.db.set_value("Property", PROP_A, "business_date", d(1))
		self.assertFalse(api.reservation_detail(self.res.name)["actions"]
		                 .get("can_reverse_check_in"))

	def test_a_connected_lock_cancels_the_keys(self):
		lock = _Lock()
		with patch("kamra.door_lock.get_adapter", return_value=lock):
			out = api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(lock.revoked, [(PROP_A, self.res.name, self.room)])
		self.assertTrue(out["key_result"]["ok"])

	def test_a_whole_villa_stay_cancels_the_keys_of_every_room(self):
		# a villa type with no rooms of its own takes the property's rooms
		make_property(PROP_B, business_date=TODAY)
		villa = make_room_type(PROP_B, "VIL", base_price=5000, room_category="Villa")
		bed = make_room_type(PROP_B, "BED", base_price=1000)
		rooms = [make_room(PROP_B, bed, n) for n in ("201", "202")]
		res = make_reservation(PROP_B, villa, TODAY, d(2), status="Checked In")
		frappe.db.set_value("Reservation", res.name, "checked_in_at", f"{TODAY} 10:00:00")
		lock = _Lock()
		with patch("kamra.door_lock.get_adapter", return_value=lock):
			out = api.reverse_check_in(res.name, "Wrong guest")
		self.assertEqual(sorted(lock.revoked), [(PROP_B, res.name, r) for r in sorted(rooms)])
		self.assertTrue(out["key_result"]["ok"])
		for r in rooms:
			self.assertEqual(frappe.db.get_value("Room", r, "occupancy_status"), "Vacant")

	def test_a_lock_lookup_failure_is_shown_not_raised(self):
		with patch("kamra.door_lock.get_adapter", side_effect=RuntimeError("UT no config")):
			out = api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(out["status"], "Confirmed")
		self.assertFalse(out["key_result"]["ok"])
		self.assertIn("Cancel the keys at the encoder", out["key_result"]["message"])

	def test_manual_keys_are_left_to_the_desk(self):
		out = api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertIsNone(out.get("key_result"))

	def test_the_next_check_in_stamps_arrival_afresh(self):
		occ = lambda: frappe.get_doc("Reservation", self.res.name).occupants[0]  # noqa: E731
		self.assertTrue(occ().arrived_at)
		api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertIsNone(occ().arrived_at)
		api.check_in(self.res.name)
		self.assertEqual(occ().arrived_at, frappe.db.get_value(
			"Reservation", self.res.name, "checked_in_at"))

	def test_refused_while_a_security_deposit_is_held(self):
		frappe.get_doc({"doctype": "Security Deposit", "reservation": self.res.name,
		                "property": PROP_A, "status": "Held", "method": "authorization",
		                "required_amount": 5000}).insert(ignore_permissions=True)
		with self.assertRaisesRegex(frappe.ValidationError, "security deposit"):
			api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(self.status(), "Checked In")

	# -- maximus carrier: arrival business date, Finance, open bills, MCP --

	def test_api_is_post_only_and_names_its_roles(self):
		fn = api.reverse_check_in
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func.get(fn), ["POST"])
		self.assertTrue({"Front Desk", "Finance", "Kamra Agent"} <= set(fn._kamra_roles))

	def test_after_midnight_before_the_audit_is_admitted(self):
		# the calendar ran past midnight; the audit has not rolled the hotel day
		frappe.db.set_value("Reservation", self.res.name, "checked_in_at",
		                    f"{d(1)} 00:30:00")
		self.assertTrue(api.reservation_detail(self.res.name)["actions"]
		                .get("can_reverse_check_in"))
		out = api.reverse_check_in(self.res.name, "Wrong guest after midnight")
		self.assertEqual(out["status"], "Confirmed")
		self.assertEqual(out["rooms"], [self.room])

	def test_refused_for_a_stay_that_arrived_on_an_earlier_day(self):
		old = make_reservation(PROP_A, self.rt, d(-1), d(1), status="Checked In",
		                       room=make_room(PROP_A, self.rt, "102"))
		frappe.db.set_value("Reservation", old.name, "checked_in_at", f"{TODAY} 01:00:00")
		with self.assertRaisesRegex(frappe.ValidationError, "night audit has run"):
			api.reverse_check_in(old.name, "Wrong guest")

	def test_finance_may_reverse_a_check_in(self):
		ensure_user(FINANCE, ["Finance"])
		frappe.set_user(FINANCE)  # nosemgrep: frappe-setuser -- test fixture
		try:
			out = api.reverse_check_in(self.res.name, "Finance: wrong guest")
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		self.assertEqual(out["status"], "Confirmed")
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"action_type": "reverse_check_in", "reference_name": self.res.name,
			"actor": FINANCE}))

	def test_refused_when_a_room_night_is_posted(self):
		folio_mod.post_room_night(frappe.get_doc("Reservation", self.res.name), TODAY)
		with self.assertRaisesRegex(frappe.ValidationError, "room night"):
			api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(self.status(), "Checked In")

	def test_a_voided_charge_still_refuses(self):
		# a void keeps the line and adds a signed reversal: both stay on the bill
		row = api.add_folio_charge(self.folio, "Laundry", "UT laundry", 300)["charges"][-1]["name"]
		api.void_folio_charge(self.folio, row, reason="UT posted to the wrong guest")
		with self.assertRaisesRegex(frappe.ValidationError, "charge or payment is on the bill"):
			api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(self.status(), "Checked In")

	def test_refused_while_a_restaurant_bill_is_open(self):
		outlet = frappe.db.get_value("POS Outlet", {"property": PROP_A,
		                                            "outlet_name": "UT Reverse Cafe"})
		if not outlet:
			outlet = frappe.get_doc({"doctype": "POS Outlet", "property": PROP_A,
			                         "outlet_name": "UT Reverse Cafe",
			                         "outlet_type": "Restaurant",
			                         "gst_rate": 5}).insert(ignore_permissions=True).name
		dish = frappe.get_doc({"doctype": "Menu Item", "property": PROP_A, "outlet": outlet,
		                       "item_name": "UT Reverse Tea", "category": "Food",
		                       "price": 100, "is_veg": 1, "available": 1,
		                       "prep_station": "Kitchen"}).insert(ignore_permissions=True).name
		from kamra import pos
		pos.create_order(outlet, [{"menu_item": dish, "qty": 1}], order_type="Room Service",
		                 room=self.room, reservation=self.res.name)
		with self.assertRaisesRegex(frappe.ValidationError, "restaurant or laundry bill"):
			api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(self.status(), "Checked In")

	def test_refused_while_a_laundry_bag_is_open(self):
		frappe.get_doc({"doctype": "Laundry Order", "property": PROP_A,
		                "order_type": "Guest", "room": self.room,
		                "reservation": self.res.name, "status": "Collected",
		                "items": [{"item_name": "UT Shirt", "qty": 2, "rate": 150}],
		                }).insert(ignore_permissions=True)
		with self.assertRaisesRegex(frappe.ValidationError, "restaurant or laundry bill"):
			api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(self.status(), "Checked In")

	def test_an_out_of_order_room_stays_out_of_order(self):
		frappe.db.set_value("Room", self.room, "housekeeping_status", "Out of Order")
		api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(frappe.db.get_value("Room", self.room,
		                                     ["occupancy_status", "housekeeping_status"]),
		                 ("Vacant", "Out of Order"))

	def test_property_scope_is_enforced(self):
		make_property(PROP_B, business_date=TODAY)
		ensure_user(DESK, ["Front Desk"])
		frappe.db.delete("User Permission", {"user": DESK})
		frappe.get_doc({"doctype": "User Permission", "user": DESK, "allow": "Property",
		                "for_value": PROP_B}).insert(ignore_permissions=True)
		frappe.clear_cache(user=DESK)
		frappe.set_user(DESK)  # nosemgrep: frappe-setuser -- test fixture
		try:
			with self.assertRaises(frappe.PermissionError):
				api.reverse_check_in(self.res.name, "Not my hotel")
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		self.assertEqual(self.status(), "Checked In")

	def test_exposed_to_agents_as_a_tool(self):
		from kamra.mcp_tools import BY_NAME, DESTRUCTIVE
		spec = BY_NAME["reverse_check_in"]
		self.assertEqual(spec.dotted, "api.reverse_check_in")
		self.assertEqual(set(spec.required), {"reservation", "reason"})
		self.assertTrue(spec.mutating)
		self.assertIn("reverse_check_in", DESTRUCTIVE)

	def test_an_agent_acts_for_a_desk_user_never_with_a_pin(self):
		ensure_user(AGENT, ["Kamra Agent"])
		ensure_user(DESK, ["Front Desk"])
		# a Kamra Agent acting for nobody with a desk role is refused
		frappe.set_user(AGENT)  # nosemgrep: frappe-setuser -- test fixture
		try:
			with self.assertRaises(frappe.PermissionError):
				api.reverse_check_in(self.res.name, "Agent alone")
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		# an agent call for a Front Desk user: a PIN property refuses, a PIN is never passed
		frappe.db.set_value("Property", PROP_A, "require_cashier_pin", 1)
		frappe.set_user(DESK)  # nosemgrep: frappe-setuser -- test fixture
		frappe.flags.kamra_agent_call = "MCP"
		try:
			with self.assertRaises(frappe.PermissionError):
				api.reverse_check_in(self.res.name, "Agent with property PIN on")
			frappe.db.set_value("Property", PROP_A, "require_cashier_pin", 0)
			with self.assertRaises(frappe.PermissionError):
				api.reverse_check_in(self.res.name, "Agent passing pin", pin="1234")
			out = api.reverse_check_in(self.res.name, "Agent: wrong guest")
		finally:
			frappe.flags.kamra_agent_call = None
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		self.assertEqual(out["status"], "Confirmed")
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"action_type": "reverse_check_in", "reference_name": self.res.name,
			"actor": DESK}))


class ReverseCheckInPosInvoiceUnitTest(unittest.TestCase):
	"""Siteless: a draft ERPNext POS Invoice (kamra_ref = the stay's folio)
	is an open restaurant bill. ERPNext is not on the Kamra test bench, so
	the database answers are stubbed."""

	def setUp(self):
		self._had_db = hasattr(frappe.local, "db")
		self._old_db = getattr(frappe.local, "db", None)
		frappe.local.db = frappe._dict(exists=lambda *a, **k: None,
		                               get_value=lambda *a, **k: None)
		self.enterContext(patch("frappe.translate.get_all_translations", return_value={}))

	def tearDown(self):
		if self._had_db:
			frappe.local.db = self._old_db
		elif hasattr(frappe.local, "db"):
			delattr(frappe.local, "db")

	def test_a_draft_pos_invoice_on_the_folio_refuses(self):
		doc = frappe._dict(name="RES-1", status="Checked In", check_in_date="2026-11-15",
		                   checked_in_at="2026-11-15 10:00:00", property="PROP-1")

		def fake_exists(dt, filters=None):
			if dt == "DocType" and filters == "POS Invoice":
				return "POS Invoice"
			if dt == "POS Invoice":
				return "POS-INV-DRAFT-1"
			return None

		frappe.local.db.exists = fake_exists
		with patch("kamra.business_date.get_business_date", return_value="2026-11-15"), \
		     patch("kamra.checkout._pending_bills", return_value=[]), \
		     patch.object(frappe, "get_all", return_value=["FOL-1"]), \
		     patch.object(frappe, "get_meta",
		                  return_value=frappe._dict(has_field=lambda f: f == "kamra_ref")):
			msg = api._reverse_check_in_blocker(doc)
		self.assertIsNotNone(msg)
		self.assertIn("restaurant or laundry bill", msg)
