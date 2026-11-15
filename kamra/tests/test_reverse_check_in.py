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

	def test_refused_when_non_room_charges_net_to_zero(self):
		api.add_folio_charge(self.folio, "Laundry", "UT laundry", 300)
		api.post_allowance(self.folio, 300, reason="UT offset")
		self.assertAlmostEqual(float(frappe.db.get_value("Folio", self.folio, "grand_total") or 0), 0.0, places=2)
		with self.assertRaisesRegex(frappe.ValidationError, "void"):
			api.reverse_check_in(self.res.name, "Wrong guest")
		self.assertEqual(self.status(), "Checked In")

	def test_an_agent_follows_caller_role_and_cashier_pin_boundaries(self):
		ensure_user(AGENT, ["Kamra Agent"])
		ensure_user(DESK, ["Front Desk"])
		# 1. Standalone Kamra Agent without Front Desk/Finance/Hotel Admin is refused
		frappe.set_user(AGENT)  # nosemgrep: frappe-setuser -- test fixture
		try:
			with self.assertRaises(frappe.PermissionError):
				api.reverse_check_in(self.res.name, "Agent alone")
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		# 2. Under kamra_agent_call for Front Desk: refused if require_cashier_pin=1 or pin passed
		frappe.db.set_value("Property", PROP_A, "require_cashier_pin", 1)
		frappe.set_user(DESK)  # nosemgrep: frappe-setuser -- test fixture
		frappe.flags.kamra_agent_call = True
		try:
			with self.assertRaises(frappe.PermissionError):
				api.reverse_check_in(self.res.name, "Agent with property PIN on")
			frappe.db.set_value("Property", PROP_A, "require_cashier_pin", 0)
			with self.assertRaises(frappe.PermissionError):
				api.reverse_check_in(self.res.name, "Agent passing pin", pin="1234")
			out = api.reverse_check_in(self.res.name, "Agent: wrong guest")
		finally:
			frappe.flags.kamra_agent_call = False
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		self.assertEqual(out["status"], "Confirmed")
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"action_type": "reverse_check_in", "reference_name": self.res.name,
			"actor": DESK}))

	def test_exposed_to_agents_as_a_tool(self):
		from kamra.mcp_tools import BY_NAME, DESTRUCTIVE
		spec = BY_NAME["reverse_check_in"]
		self.assertEqual(spec.dotted, "api.reverse_check_in")
		self.assertEqual(set(spec.required), {"reservation", "reason"})
		self.assertTrue(spec.mutating)
		self.assertIn("reverse_check_in", DESTRUCTIVE)


import unittest


class ReverseCheckInBlockerAndAgentUnitTests(unittest.TestCase):
	"""Siteless unit tests for Hydrogen #9 findings 2a, 2b and group-quote fixed levy."""

	def setUp(self):
		self._had_db = hasattr(frappe.local, "db")
		self._old_db = getattr(frappe.local, "db", None)
		frappe.local.db = frappe._dict(exists=lambda *a, **k: None, get_value=lambda *a, **k: None)
		frappe.local.flags = frappe._dict()
		frappe.local.message_log = []
		self.enterContext(patch("frappe.translate.get_all_translations", return_value={}))

	def tearDown(self):
		if self._had_db:
			frappe.local.db = self._old_db
		elif hasattr(frappe.local, "db"):
			delattr(frappe.local, "db")

	def test_blocker_rejects_unvoided_charges_netting_to_zero(self):
		doc = frappe._dict(name="RES-1", status="Checked In", check_in_date="2026-11-15", property="PROP-1", guest_name="UT", _occupied_rooms=lambda **k: [], save=lambda *a, **k: None, db_set=lambda *a, **k: None)
		folios = [frappe._dict(name="FOL-1", grand_total=0.0, payments_total=0.0)]

		def fake_exists(dt, filters=None):
			if dt == "Folio Charge" and filters and filters.get("charge_type") == "Room":
				return None
			if dt == "Folio Charge":
				return "CHG-1"  # unvoided Laundry + Allowance rows netting to 0
			return None

		frappe.local.db.exists = fake_exists
		with patch("kamra.business_date.get_business_date", return_value="2026-11-15"), \
		     patch("kamra.checkout._pending_bills", return_value=[]), \
		     patch.object(frappe, "get_all", return_value=folios), \
		     patch.object(frappe, "get_meta", side_effect=lambda *a, **k: frappe._dict(has_field=lambda f: False)):
			msg = api._reverse_check_in_blocker(doc)
		self.assertIsNotNone(msg)
		self.assertIn("void", msg.lower())

	def test_blocker_rejects_open_ury_pos_invoice_on_folio(self):
		doc = frappe._dict(name="RES-1", status="Checked In", check_in_date="2026-11-15", property="PROP-1", guest_name="UT", _occupied_rooms=lambda **k: [], save=lambda *a, **k: None, db_set=lambda *a, **k: None)
		folios = [frappe._dict(name="FOL-1", grand_total=0.0, payments_total=0.0)]

		def fake_exists(dt, filters=None):
			if dt == "DocType" and filters == "POS Invoice":
				return "POS Invoice"
			if dt == "POS Invoice":
				return "POS-INV-DRAFT-1"
			return None

		frappe.local.db.exists = fake_exists
		with patch("kamra.business_date.get_business_date", return_value="2026-11-15"), \
		     patch("kamra.checkout._pending_bills", return_value=[]), \
		     patch.object(frappe, "get_all", return_value=folios), \
		     patch.object(frappe, "get_meta", return_value=frappe._dict(has_field=lambda f: f == "kamra_ref")):
			msg = api._reverse_check_in_blocker(doc)
		self.assertIsNotNone(msg)
		self.assertIn("bill", msg.lower())

	def test_agent_cannot_pass_pin_or_bypass_property_cashier_pin(self):
		doc = frappe._dict(name="RES-1", status="Checked In", check_in_date="2026-11-15", property="PROP-1", guest_name="UT", _occupied_rooms=lambda **k: [], save=lambda *a, **k: None, db_set=lambda *a, **k: None)
		frappe.local.db.get_value = lambda *a, **k: 1
		with patch("kamra.authz._enforce_property_scope"), \
		     patch("kamra.api.assert_property_access"), \
		     patch("kamra.credit._is_agent", return_value=True), \
		     patch.object(frappe, "get_roles", return_value=["Front Desk", "Kamra Agent"]), \
		     patch.object(frappe, "session", frappe._dict(user="fd@example.com")), \
		     patch.object(frappe, "get_doc", return_value=doc), \
		     patch("kamra.api._reverse_check_in_blocker", return_value=None), patch("kamra.api._revoke_stay_keys", return_value={"ok": True}), patch("kamra.savings.log_action"):
			with self.assertRaises(frappe.PermissionError):
				api.reverse_check_in("RES-1", "Wrong guest", pin="1234")
			with self.assertRaises(frappe.PermissionError):
				api.reverse_check_in("RES-1", "Wrong guest")

	def test_standalone_kamra_agent_without_desk_role_is_refused(self):
		doc = frappe._dict(name="RES-1", status="Checked In", check_in_date="2026-11-15", property="PROP-1", guest_name="UT", _occupied_rooms=lambda **k: [], save=lambda *a, **k: None, db_set=lambda *a, **k: None)
		with patch("kamra.authz._enforce_property_scope"), \
		     patch("kamra.api.assert_property_access"), \
		     patch("kamra.credit._is_agent", return_value=True), \
		     patch.object(frappe, "get_roles", return_value=["Kamra Agent"]), \
		     patch.object(frappe, "session", frappe._dict(user="agent@kamra.local")), \
		     patch.object(frappe, "get_doc", return_value=doc), \
		     patch("kamra.api._reverse_check_in_blocker", return_value=None), patch("kamra.api._revoke_stay_keys", return_value={"ok": True}), patch("kamra.savings.log_action"):
			with self.assertRaises(frappe.PermissionError):
				api.reverse_check_in("RES-1", "Agent alone")

	def test_group_block_prices_scales_fixed_levy_by_room_nights_not_rate_share(self):
		rt_doc = frappe._dict(adults_capacity=2, base_price=100, room_type_name="Deluxe")
		fake_q = {
			"nightly": [{"date": "2026-11-20", "rate": 100.0}, {"date": "2026-11-21", "rate": 200.0}],
			"room_levy": 4000.0,  # 2,000 fixed per night * 2 nights, 0% percent levy
			"amount_after_tax": 4300.0,
		}
		blocks = [frappe._dict(
			room_type="RT-DLX",
			rooms_blocked=1,
			block_rate=0,
			nightly_counts='{"2026-11-20": 3, "2026-11-21": 1}',  # 4 room-nights -> fixed levy must be 4 * 2000 = 8000!
		)]
		frappe.local.db.get_value = lambda dt, name, field=None, **k: 2000.0 if field == "room_levy_per_night" else 0
		with patch("kamra.pricing.quote", return_value=fake_q), patch("kamra.booking_refs.resolve_room_type", return_value="RT-DLX"), patch("kamra.api.resolve_room_type", return_value="RT-DLX", create=True), \
		     patch.object(frappe, "get_cached_doc", return_value=rt_doc):
			out = api._block_prices("PROP-1", "2026-11-20", "2026-11-22", blocks)
		self.assertEqual(out["rows"][0]["room_nights"], 4)
		self.assertEqual(out["levy"], 8000.0)
