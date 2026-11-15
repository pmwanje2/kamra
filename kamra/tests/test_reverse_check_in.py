"""Reversing a check-in made by mistake, the same hotel day (#36).

api.reverse_check_in (Oracle OPERA Cloud "Reverse Check-In"): only a
Checked In stay whose check_in_date is the property's current business
date (no night audit has rolled over it), with a reason, no Room night
posted, no open restaurant bill and nothing owed or paid on any of its
folios. The stay goes back to Confirmed, the room is released and marked
Dirty (a guest or staff may have been inside), and the reason goes on the
action log. Property scope is enforced; an agent is held to the same
rules and is accountable through the log.
"""

from __future__ import annotations

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
		self.folio = guest_folio(self.res.name)

	def status(self):
		return frappe.db.get_value("Reservation", self.res.name, "status")

	def test_api_is_post_only_and_names_its_roles(self):
		fn = api.reverse_check_in
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func.get(fn), ["POST"])
		self.assertTrue({"Front Desk", "Finance", "Kamra Agent"} <= set(fn._kamra_roles))

	def test_same_day_zero_balance_goes_back_to_confirmed(self):
		out = api.reverse_check_in(self.res.name, "Wrong guest checked in")
		self.assertEqual(out["status"], "Confirmed")
		self.assertEqual(self.status(), "Confirmed")
		self.assertEqual(frappe.db.get_value("Reservation", self.res.name,
		                                     ["actual_check_in", "checked_in_at", "checked_in_by"]),
		                 (None, None, None))
		self.assertEqual(frappe.db.get_value("Room", self.room,
		                                     ["occupancy_status", "housekeeping_status"]),
		                 ("Vacant", "Dirty"))
		log = frappe.get_all("Agent Action Log", fields=["rationale", "property"], filters={
			"action_type": "reverse_check_in", "reference_name": self.res.name})
		self.assertEqual(len(log), 1)
		self.assertIn("Wrong guest checked in", log[0].rationale)
		self.assertEqual(log[0].property, PROP_A)
		# and the desk can check the guest in again
		api.check_in(self.res.name)
		self.assertEqual(self.status(), "Checked In")

	def test_then_the_booking_can_be_cancelled(self):
		api.reverse_check_in(self.res.name, "Test booking")
		api.cancel_reservation(self.res.name, reason="Guest request", waive_fee=1)
		self.assertEqual(self.status(), "Cancelled")

	def test_needs_a_reason_and_a_checked_in_stay(self):
		with self.assertRaisesRegex(frappe.ValidationError, "why"):
			api.reverse_check_in(self.res.name, "  ")
		self.assertEqual(self.status(), "Checked In")
		api.reverse_check_in(self.res.name, "Wrong room")
		with self.assertRaisesRegex(frappe.ValidationError, "checked-in"):
			api.reverse_check_in(self.res.name, "Again")

	def test_refused_when_the_check_in_date_is_not_the_business_date(self):
		# the night audit rolled the hotel day over this arrival
		frappe.db.set_value("Property", PROP_A, "business_date", d(1))
		with self.assertRaisesRegex(frappe.ValidationError, "night audit"):
			api.reverse_check_in(self.res.name, "Too late")
		self.assertEqual(self.status(), "Checked In")

	def test_refused_for_a_stay_that_arrived_on_an_earlier_day(self):
		old = make_reservation(PROP_A, self.rt, d(-1), d(1), status="Checked In",
		                       room=make_room(PROP_A, self.rt, "102"))
		with self.assertRaisesRegex(frappe.ValidationError, "night audit"):
			api.reverse_check_in(old.name, "Wrong guest")

	def test_refused_when_a_room_night_is_posted(self):
		folio_mod.post_room_night(frappe.get_doc("Reservation", self.res.name), TODAY)
		with self.assertRaisesRegex(frappe.ValidationError, "room night"):
			api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(self.status(), "Checked In")

	def test_refused_when_a_charge_is_owed(self):
		api.add_folio_charge(self.folio, "Laundry", "UT laundry", 300)
		with self.assertRaisesRegex(frappe.ValidationError, "balance"):
			api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(self.status(), "Checked In")

	def test_refused_when_a_payment_is_unrefunded(self):
		api.add_folio_payment(self.folio, "Cash", 500, kind="Advance")
		with self.assertRaisesRegex(frappe.ValidationError, "balance"):
			api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(self.status(), "Checked In")

	def test_a_voided_charge_and_a_refunded_payment_net_to_zero(self):
		row = api.add_folio_charge(self.folio, "Laundry", "UT laundry", 300)["charges"][-1]["name"]
		api.void_folio_charge(self.folio, row, reason="UT posted to the wrong guest")
		api.add_folio_payment(self.folio, "Cash", 500, kind="Advance")
		api.refund_folio_payment(self.folio, 500, "Cash", "UT wrong guest")
		out = api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(out["status"], "Confirmed")

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
		with self.assertRaisesRegex(frappe.ValidationError, "bill"):
			api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(self.status(), "Checked In")

	def test_refused_while_a_security_deposit_is_held(self):
		frappe.get_doc({"doctype": "Security Deposit", "reservation": self.res.name,
		                "property": PROP_A, "status": "Held", "method": "authorization",
		                "required_amount": 5000}).insert(ignore_permissions=True)
		with self.assertRaisesRegex(frappe.ValidationError, "security deposit"):
			api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(self.status(), "Checked In")

	def test_an_out_of_order_room_stays_out_of_order(self):
		frappe.db.set_value("Room", self.room, "housekeeping_status", "Out of Order")
		api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(frappe.db.get_value("Room", self.room,
		                                     ["occupancy_status", "housekeeping_status"]),
		                 ("Vacant", "Out of Order"))

	def test_the_next_check_in_stamps_arrival_afresh(self):
		occ = lambda: frappe.get_doc("Reservation", self.res.name).occupants[0]  # noqa: E731
		self.assertTrue(occ().arrived_at)
		api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertIsNone(occ().arrived_at)

	def test_a_connected_lock_cancels_the_keys(self):
		lock = _Lock()
		with patch("kamra.door_lock.get_adapter", return_value=lock):
			out = api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(lock.revoked, [(PROP_A, self.res.name, self.room)])
		self.assertTrue(out["key_result"]["ok"])

	def test_a_lock_failure_is_shown_not_raised(self):
		with patch("kamra.door_lock.get_adapter", side_effect=RuntimeError("UT no config")):
			out = api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(out["status"], "Confirmed")
		self.assertFalse(out["key_result"]["ok"])

	def test_offered_only_when_allowed(self):
		self.assertTrue(api.reservation_detail(self.res.name)["actions"]
		                .get("can_reverse_check_in"))
		frappe.db.set_value("Property", PROP_A, "business_date", d(1))
		self.assertFalse(api.reservation_detail(self.res.name)["actions"]
		                 .get("can_reverse_check_in"))

	def test_property_scope_is_enforced(self):
		make_property(PROP_B, business_date=TODAY)
		ensure_user(DESK, ["Front Desk"])
		frappe.db.delete("User Permission", {"user": DESK})
		frappe.get_doc({"doctype": "User Permission", "user": DESK, "allow": "Property",
		                "for_value": PROP_B}).insert(ignore_permissions=True)
		frappe.clear_cache(user=DESK)
		frappe.set_user(DESK)  # nosemgrep: frappe-setuser -- test fixture
		with self.assertRaises(frappe.PermissionError):
			api.reverse_check_in(self.res.name, "Not my hotel")
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		self.assertEqual(self.status(), "Checked In")

	def test_an_agent_follows_the_same_rules_and_is_logged(self):
		ensure_user(AGENT, ["Kamra Agent"])
		frappe.db.set_value("Property", PROP_A, "require_cashier_pin", 1)
		api.add_folio_charge(self.folio, "Laundry", "UT laundry", 300)
		frappe.set_user(AGENT)  # nosemgrep: frappe-setuser -- test fixture
		try:
			with self.assertRaisesRegex(frappe.ValidationError, "balance"):
				api.reverse_check_in(self.res.name, "Agent: wrong guest")
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		frappe.db.delete("Folio Charge", {"parent": self.folio})
		folio_mod._recalculate(doc := frappe.get_doc("Folio", self.folio))
		doc.save(ignore_permissions=True)
		frappe.set_user(AGENT)  # nosemgrep: frappe-setuser -- test fixture
		try:
			out = api.reverse_check_in(self.res.name, "Agent: wrong guest")
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		self.assertEqual(out["status"], "Confirmed")
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"action_type": "reverse_check_in", "reference_name": self.res.name,
			"actor": AGENT}))

	def test_exposed_to_agents_as_a_tool(self):
		from kamra.mcp_tools import BY_NAME, DESTRUCTIVE
		spec = BY_NAME["reverse_check_in"]
		self.assertEqual(spec.method, "api.reverse_check_in")
		self.assertEqual(set(spec.required), {"reservation", "reason"})
		self.assertTrue(spec.mutating)
		self.assertIn("reverse_check_in", DESTRUCTIVE)
