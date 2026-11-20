"""set_agreed_rate: a negotiated nightly rate for one stay, in place
(OPERA "Override Rate Amount").

The stay keeps its room; only its nightly rates change, locked like any
other quote, so the folio and check-out bill the agreed price. The rate
card (Room Type, Season) is never written. A Rate Change reason is always
given. Revenue Manager / Hotel Admin set any rate; Front Desk may only
lower it, within the property's rate_override_limit_pct, and a bigger cut
(or a supervisor reason code) needs a supervisor's PIN."""

from __future__ import annotations

import json

import frappe

from kamra import api, ledger
from kamra import folio as folio_mod
from kamra.settings import clear_cache
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	YESTERDAY,
	AuditTestCase,
	charges,
	d,
	ensure_user,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

DESK = "ut.rate.desk@test.local"
REVENUE = "ut.rate.revenue@test.local"
GM = "ut.rate.gm@test.local"
AGENT = "ut.rate.agent@test.local"


def set_setting(prop: str, key: str, value) -> None:
	name = frappe.db.get_value("Property Setting",
	                           {"property": prop, "setting_key": key})
	if name:
		frappe.db.set_value("Property Setting", name, "value", json.dumps(value))
	else:
		frappe.get_doc({"doctype": "Property Setting", "property": prop,
		                "setting_key": key, "value": json.dumps(value)}
		               ).insert(ignore_permissions=True)
	clear_cache(prop)


class AgreedRateTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY)
		make_property(PROP_A, business_date=YESTERDAY)
		frappe.db.delete("Property Setting", {"property": PROP_A})
		clear_cache(PROP_A)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.rooms = [make_room(PROP_A, self.rt, f"10{i}") for i in range(1, 4)]
		ensure_user(DESK, ["Front Desk"])
		ensure_user(REVENUE, ["Revenue Manager"])
		ensure_user(GM, ["Hotel Admin"])
		ensure_user(AGENT, ["Kamra Agent"])
		frappe.db.delete("Cashier PIN", {"name": GM})
		frappe.get_doc({"doctype": "Cashier PIN", "user": GM, "pin": "4321"}
		               ).insert(ignore_permissions=True)
		self.res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[0])

	def as_user(self, user, fn, *args, **kwargs):
		frappe.set_user(user)  # nosemgrep: frappe-setuser -- acting as the persona
		try:
			return fn(*args, **kwargs)
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- restore

	def as_agent(self, user, fn, *args, **kwargs):
		"""The call an AI agent (MCP / chat) makes on behalf of user."""
		frappe.flags.kamra_agent_call = "MCP"
		try:
			return self.as_user(user, fn, *args, **kwargs)
		finally:
			frappe.flags.kamra_agent_call = None

	def nights(self, name=None):
		doc = frappe.get_doc("Reservation", name or self.res.name)
		return [float(r.rate) for r in doc.night_rates]

	def rate_logs(self):
		return frappe.get_all("Agent Action Log",
		                      filters={"action_type": "rate_override",
		                               "reference_name": self.res.name},
		                      fields=["actor", "approver", "rationale"])

	def test_front_desk_lowers_the_rate_within_the_limit(self):
		set_setting(PROP_A, "rate_override_limit_pct", 30)
		seasons = frappe.db.count("Season", {"property": PROP_A})
		out = self.as_user(DESK, api.set_agreed_rate, self.res.name, 750,
		                   "RATE-PROMO", reason="Agreed at the desk", confirm_price=1)
		self.assertTrue(out["ok"])
		doc = frappe.get_doc("Reservation", self.res.name)
		self.assertEqual(doc.room, self.rooms[0])
		self.assertEqual(float(doc.agreed_rate), 750)
		self.assertEqual(self.nights(), [750, 750])
		# the rate card is untouched
		self.assertEqual(float(frappe.db.get_value("Room Type", self.rt, "base_price")), 1000)
		self.assertEqual(frappe.db.count("Season", {"property": PROP_A}), seasons)
		logs = self.rate_logs()
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].actor, DESK)
		self.assertIn("RATE-PROMO", logs[0].rationale)
		self.assertIn("1,000.00", logs[0].rationale)
		self.assertIn("750.00", logs[0].rationale)

	def test_front_desk_cut_over_the_limit_needs_a_supervisor(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, api.set_agreed_rate, self.res.name, 750,
			             "RATE-PROMO", confirm_price=1)
		self.assertEqual(self.nights(), [1000, 1000])
		# a Hotel Admin signs it off with their own PIN
		self.as_user(DESK, api.set_agreed_rate, self.res.name, 750, "RATE-PROMO",
		             confirm_price=1, pin="4321", supervisor=GM)
		self.assertEqual(self.nights(), [750, 750])
		self.assertEqual(self.rate_logs()[0].approver, GM)

	def test_default_limit_zero_sends_every_desk_cut_to_a_supervisor(self):
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, api.set_agreed_rate, self.res.name, 990,
			             "RATE-PROMO", confirm_price=1)
		self.assertEqual(self.nights(), [1000, 1000])

	def test_a_wrong_supervisor_pin_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.as_user(DESK, api.set_agreed_rate, self.res.name, 750, "RATE-PROMO",
			             confirm_price=1, pin="0000", supervisor=GM)
		self.assertEqual(self.nights(), [1000, 1000])

	def test_front_desk_never_raises_a_rate(self):
		set_setting(PROP_A, "rate_override_limit_pct", 100)
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, api.set_agreed_rate, self.res.name, 1200,
			             "RATE-PROMO", confirm_price=1, pin="4321", supervisor=GM)
		self.assertEqual(self.nights(), [1000, 1000])

	def test_supervisor_reason_code_needs_a_supervisor_within_the_limit(self):
		set_setting(PROP_A, "rate_override_limit_pct", 30)
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, api.set_agreed_rate, self.res.name, 900,
			             "RATE-MGR", confirm_price=1)
		self.as_user(DESK, api.set_agreed_rate, self.res.name, 900, "RATE-MGR",
		             confirm_price=1, pin="4321", supervisor=GM)
		self.assertEqual(self.nights(), [900, 900])

	def test_revenue_manager_sets_any_positive_rate(self):
		self.as_user(REVENUE, api.set_agreed_rate, self.res.name, 1200,
		             "RATE-MGR", confirm_price=1)
		self.assertEqual(self.nights(), [1200, 1200])
		with self.assertRaises(frappe.ValidationError):
			self.as_user(REVENUE, api.set_agreed_rate, self.res.name, 0,
			             "RATE-MGR", confirm_price=1)

	def test_the_reason_must_be_a_rate_change_code(self):
		with self.assertRaises(frappe.ValidationError):
			self.as_user(REVENUE, api.set_agreed_rate, self.res.name, 800,
			             "VOID-ERR", confirm_price=1)
		with self.assertRaises(frappe.ValidationError):
			self.as_user(REVENUE, api.set_agreed_rate, self.res.name, 800,
			             "", reason="cheaper", confirm_price=1)
		self.assertEqual(self.nights(), [1000, 1000])

	def test_the_new_price_is_confirmed_first(self):
		with self.assertRaises(api.PriceConfirmationRequired):
			self.as_user(REVENUE, api.set_agreed_rate, self.res.name, 800, "RATE-MGR")
		self.assertEqual(self.nights(), [1000, 1000])

	def test_preview_writes_nothing(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		pv = self.as_user(DESK, api.rate_override_preview, self.res.name, 750)
		self.assertAlmostEqual(pv["current_nightly"], 1000)
		self.assertAlmostEqual(pv["reduction_pct"], 25)
		self.assertEqual(pv["limit_pct"], 10)
		self.assertTrue(pv["needs_supervisor"])
		self.assertTrue(pv["needs_confirmation"])
		self.assertEqual([n["rate"] for n in pv["nights"]], [750, 750])
		self.assertEqual(self.nights(), [1000, 1000])
		self.assertFalse(frappe.db.get_value("Reservation", self.res.name, "agreed_rate"))

	def test_in_house_stay_bills_the_agreed_rate_with_no_phantom_balance(self):
		stay = make_reservation(PROP_A, self.rt, d(0), d(3), status="Checked In",
		                        room=self.rooms[1])
		self.as_user(REVENUE, api.set_agreed_rate, stay.name, 750, "RATE-MGR",
		             confirm_price=1)
		self.assertEqual(frappe.db.get_value("Reservation", stay.name, "room"), self.rooms[1])
		locked = self.nights(stay.name)
		self.assertEqual(locked, [750, 750, 750])
		folio_mod.post_remaining_nights(frappe.get_doc("Reservation", stay.name))
		room_lines = charges(stay.name, "Room")
		self.assertEqual(sorted(float(c.amount) for c in room_lines), locked)

	# --- AI agents: never past the front desk's limit, never a supervisor PIN

	def test_agent_call_under_a_revenue_manager_is_held_to_the_desk_limit(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		with self.assertRaises(frappe.PermissionError) as raised:
			self.as_agent(REVENUE, api.set_agreed_rate, self.res.name, 750,
			              "RATE-PROMO", confirm_price=1)
		self.assertIn("hotel screen", str(raised.exception))
		with self.assertRaises(frappe.PermissionError):
			self.as_agent(REVENUE, api.set_agreed_rate, self.res.name, 1200,
			              "RATE-PROMO", confirm_price=1)
		self.assertEqual(self.nights(), [1000, 1000])
		pv = self.as_agent(REVENUE, api.rate_override_preview, self.res.name, 750)
		self.assertFalse(pv["can_set_rate"])
		self.assertTrue(pv["needs_supervisor"])
		self.assertFalse(self.rate_logs())

	def test_agent_call_under_a_revenue_manager_applies_a_cut_within_the_limit(self):
		set_setting(PROP_A, "rate_override_limit_pct", 30)
		self.as_agent(REVENUE, api.set_agreed_rate, self.res.name, 900,
		              "RATE-PROMO", confirm_price=1)
		self.assertEqual(self.nights(), [900, 900])

	def test_the_agent_service_user_alone_cannot_set_an_agreed_rate(self):
		set_setting(PROP_A, "rate_override_limit_pct", 30)
		with self.assertRaises(frappe.PermissionError) as raised:
			self.as_user(AGENT, api.set_agreed_rate, self.res.name, 900,
			             "RATE-PROMO", confirm_price=1)
		self.assertIn("hotel screen", str(raised.exception))
		with self.assertRaises(frappe.PermissionError):
			self.as_agent(AGENT, api.set_agreed_rate, self.res.name, 900,
			              "RATE-PROMO", confirm_price=1)
		self.assertEqual(self.nights(), [1000, 1000])
		self.assertFalse(self.rate_logs())

	def test_an_agent_never_uses_a_supervisor_pin(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		for user in (DESK, REVENUE, GM):
			with self.assertRaises(frappe.PermissionError) as raised:
				self.as_agent(user, api.set_agreed_rate, self.res.name, 750,
				              "RATE-PROMO", confirm_price=1, pin="4321", supervisor=GM)
			self.assertIn("hotel screen", str(raised.exception))
		self.assertEqual(self.nights(), [1000, 1000])
		self.assertFalse(self.rate_logs())

	def test_an_agent_cannot_use_a_reason_code_that_needs_a_supervisor(self):
		set_setting(PROP_A, "rate_override_limit_pct", 30)
		with self.assertRaises(frappe.PermissionError) as raised:
			self.as_agent(DESK, api.set_agreed_rate, self.res.name, 900,
			              "RATE-MGR", confirm_price=1)
		self.assertIn("hotel screen", str(raised.exception))
		self.assertEqual(self.nights(), [1000, 1000])

	# --- tax-inclusive prices: the cut is measured gross against gross

	def vat_stay(self):
		"""A stay at a VAT-inclusive property: the card says 1,000 a night
		with 18% tax inside it, so the locked nightly rate is 847.46 net."""
		make_property(PROP_A, business_date=YESTERDAY, rates_include_tax=1)
		rt = make_room_type(PROP_A, "VAT", base_price=1000, tax_percent=18)
		frappe.db.set_value("Room Type", rt, {"base_price": 1000, "tax_percent": 18})
		room = make_room(PROP_A, rt, "201")
		res = make_reservation(PROP_A, rt, d(5), d(7), room=room)
		self.assertEqual([round(r, 2) for r in self.nights(res.name)], [847.46, 847.46])
		self.assertAlmostEqual(float(res.amount_after_tax), 2000, places=1)
		return res

	def test_inclusive_rates_a_desk_cut_within_the_limit_is_not_a_raise(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		res = self.vat_stay()
		pv = self.as_user(DESK, api.rate_override_preview, res.name, 900)
		self.assertAlmostEqual(pv["current_nightly"], 1000, places=1)
		self.assertAlmostEqual(pv["reduction_pct"], 10, places=1)
		self.assertFalse(pv["raises"])
		self.assertFalse(pv["needs_supervisor"])
		self.as_user(DESK, api.set_agreed_rate, res.name, 900, "RATE-PROMO",
		             confirm_price=1)
		self.assertEqual([round(r, 2) for r in self.nights(res.name)], [762.71, 762.71])
		self.assertAlmostEqual(
			float(frappe.db.get_value("Reservation", res.name, "amount_after_tax")),
			1800, places=1)

	def test_inclusive_rates_a_desk_cut_over_the_limit_needs_a_supervisor(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		res = self.vat_stay()
		pv = self.as_user(DESK, api.rate_override_preview, res.name, 800)
		self.assertAlmostEqual(pv["reduction_pct"], 20, places=1)
		self.assertTrue(pv["needs_supervisor"])
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, api.set_agreed_rate, res.name, 800, "RATE-PROMO",
			             confirm_price=1)
		self.assertEqual([round(r, 2) for r in self.nights(res.name)], [847.46, 847.46])
		self.as_user(DESK, api.set_agreed_rate, res.name, 800, "RATE-PROMO",
		             confirm_price=1, pin="4321", supervisor=GM)
		# 800 gross a night: 677.97 net each, the cent balanced so the stay
		# totals exactly 1,600
		for night in self.nights(res.name):
			self.assertAlmostEqual(night, 677.97, delta=0.011)
		self.assertAlmostEqual(
			float(frappe.db.get_value("Reservation", res.name, "amount_after_tax")),
			1600, places=1)

	def test_inclusive_rates_the_log_shows_gross_nightly_amounts(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		res = self.vat_stay()
		self.as_user(DESK, api.set_agreed_rate, res.name, 900, "RATE-PROMO",
		             confirm_price=1)
		log = frappe.get_all("Agent Action Log",
		                     filters={"action_type": "rate_override",
		                              "reference_name": res.name},
		                     pluck="rationale")
		self.assertEqual(len(log), 1)
		self.assertIn("1,000.00 → 900.00 a night", log[0])

	# --- what the hotel screen's Agreed rate action needs (M09)

	def test_preview_names_who_can_sign_off_a_desk_cut(self):
		"""The desk picks the approver from the people verify_supervisor
		accepts here (Hotel Admin / Finance with a cashier PIN)."""
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		pv = self.as_user(DESK, api.rate_override_preview, self.res.name, 750)
		self.assertTrue(pv["needs_supervisor"])
		users = [s["user"] for s in pv["supervisors"]]
		self.assertIn(GM, users)
		self.assertNotIn(REVENUE, users)
		self.assertNotIn(DESK, users)
		self.assertEqual(self.nights(), [1000, 1000])

	def test_preview_names_no_approver_for_a_manager(self):
		pv = self.as_user(REVENUE, api.rate_override_preview, self.res.name, 750)
		self.assertTrue(pv["can_set_rate"])
		self.assertEqual(pv["supervisors"], [])

	def test_a_revenue_manager_lists_the_rate_change_reasons(self):
		"""set_agreed_rate needs a Rate Change reason code, so everyone who
		may set a rate can read the list."""
		codes = self.as_user(REVENUE, ledger.list_reason_codes, "Rate Change")
		self.assertTrue({"RATE-PROMO", "RATE-MGR"} <= {c["name"] for c in codes})
		self.assertEqual({c["category"] for c in codes}, {"Rate Change"})
