"""Open outlet checks at check-out and End of Day, through a provider seam.

When another point of sale runs the outlets, the PMS cannot see its open
checks in its own POS Order table. An integration answers instead: an
enabled API Server Script at ``hospitality_kamra.open_outlet_checks``
(kamra.outlet_checks.OPEN_CHECKS), called through kamra.erp_bridge with the
property and, for one stay, the reservation and its room. It returns
``{"checks": [...]}``.

Business checks (Oracle):
- OPERA Cloud check-out settles the guest's bill before the room is
  released; an outlet check still open is money the bill does not show.
- OPERA Cloud End of Day halts on open items. Simphony's role privilege
  30059 "Prevent Running SOD from OPS with Open Checks" makes the open-check
  stop configurable. Here that is the existing ``na_block_open_pos`` setting
  (on by default: block; off: listed as a warning), with
  ``na_auto_audit_with_blockers`` deciding what the scheduled run does.

A site with no provider behaves exactly as before.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from kamra import checkout, night_audit
from kamra.settings import write_kv
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

OPEN_CHECKS = "hospitality_kamra.open_outlet_checks"


def check(name="PSINV-0001", amount=42000.0, billed=0, **extra):
	return {"name": name, "outlet": "Lakeside Grill", "table": "T4", "room": None,
	        "reservation": None, "amount": amount, "billed": billed,
	        "opened_at": f"{YESTERDAY} 20:15:00", **extra}


class OutletChecksTestCase(AuditTestCase):
	def setUp(self):
		super().setUp()
		self.at(TODAY)
		make_property(PROP_A, business_date=YESTERDAY)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.room = make_room(PROP_A, self.rt, "101")
		self.calls: list[tuple[str, dict]] = []
		self.answer = None

	def provider(self, api_method, *, response_types=None, **form):
		self.calls.append((api_method, form))
		if api_method != OPEN_CHECKS:
			return None
		return self.answer() if callable(self.answer) else self.answer

	def installed(self):
		return patch("kamra.erp_bridge.bridge_call", side_effect=self.provider)

	def in_house(self):
		return make_reservation(PROP_A, self.rt, d(-2), d(0), status="Checked In",
		                        room=self.room)

	def outlet_calls(self):
		return [form for method, form in self.calls if method == OPEN_CHECKS]

	# --- the seam --------------------------------------------------------------

	def test_no_provider_answers_none(self):
		from kamra.outlet_checks import open_outlet_checks

		self.answer = None
		with self.installed():
			self.assertIsNone(open_outlet_checks(PROP_A, business_date=YESTERDAY))
		self.assertEqual(self.outlet_calls(), [{"property": PROP_A, "business_date": YESTERDAY}])

	def test_provider_rows_are_normalised(self):
		from kamra.outlet_checks import open_outlet_checks

		self.answer = {"checks": [check(amount=42000, billed=1, room="101")]}
		with self.installed():
			rows = open_outlet_checks(PROP_A, reservation="RES-1", room=self.room)
		self.assertEqual(rows, [{
			"name": "PSINV-0001", "outlet": "Lakeside Grill", "table": "T4", "room": "101",
			"reservation": None, "amount": 42000.0, "billed": True,
			"opened_at": f"{YESTERDAY} 20:15:00"}])
		self.assertEqual(self.outlet_calls(), [{"property": PROP_A, "reservation": "RES-1", "room": self.room}])

	def test_a_malformed_answer_fails_closed(self):
		from kamra.outlet_checks import open_outlet_checks

		bad = [
			[check()],                                 # not {"checks": [...]}
			{"checks": check()},                       # not a list
			{"checks": [check(name="")]},              # no check id
			{"checks": [check(amount="42000")]},       # amount as text
			{"checks": [check(amount=True)]},          # a boolean is not money
			{"checks": [check(outlet=7)]},             # outlet not text
			{"checks": ["PSINV-0001"]},                # row not a mapping
		]
		for answer in bad:
			self.answer = answer
			with self.subTest(answer=answer), self.installed(), \
					self.assertRaisesRegex(ValueError, "outlet check"):
				open_outlet_checks(PROP_A)

	# --- check-out -------------------------------------------------------------

	def test_check_out_is_unchanged_without_a_provider(self):
		res = self.in_house()
		with self.installed():
			self.assertEqual(checkout._pending_bills(res, []), [])
			checkout.assert_no_open_bills(res)  # does not raise

	def test_an_open_outlet_check_for_the_stay_is_a_pending_bill(self):
		res = self.in_house()
		self.answer = {"checks": [check(reservation=res.name, room="101", billed=1)]}
		with self.installed():
			bills = checkout._pending_bills(res, [])
		self.assertEqual(self.outlet_calls(), [{"property": PROP_A, "reservation": res.name, "room": self.room}])
		self.assertEqual(len(bills), 1)
		bill = bills[0]
		self.assertEqual((bill["kind"], bill["name"], bill["label"]), ("outlet", "PSINV-0001", "Lakeside Grill"))
		self.assertEqual(bill["amount"], 42000.0)
		self.assertTrue(bill["billable"])
		# it is settled at the outlet, not from the desk's check-out sheet
		self.assertFalse(bill["can_post_to_room"])
		self.assertFalse(bill["can_mark_ready"])
		self.assertEqual(bill["pay_modes"], [])
		self.assertIn("outlet", bill["hint"])

	def test_an_open_outlet_check_refuses_check_out(self):
		res = self.in_house()
		self.answer = {"checks": [check(reservation=res.name)]}
		with self.installed(), self.assertRaises(frappe.ValidationError) as caught:
			checkout.assert_no_open_bills(res)
		self.assertIn("PSINV-0001", str(caught.exception))
		self.assertIn("Lakeside Grill", str(caught.exception))

	def test_check_out_sheet_lists_the_outlet_check_as_a_blocker(self):
		res = self.in_house()
		self.answer = {"checks": [check(reservation=res.name)]}
		with self.installed():
			ctx = checkout.checkout_context(res.name)
		self.assertIn("PSINV-0001", [b["name"] for b in ctx["pending_bills"]])
		self.assertIn("pending_bills", [b["code"] for b in ctx["blockers"]])

	def test_a_departed_stay_does_not_ask_the_provider(self):
		res = make_reservation(PROP_A, self.rt, d(-4), d(-1), status="Confirmed")
		self.answer = {"checks": [check()]}
		with self.installed():
			ctx = checkout.checkout_context(res.name)
		self.assertEqual(ctx["pending_bills"], [])
		self.assertEqual(self.outlet_calls(), [])

	# --- End of Day ------------------------------------------------------------

	def test_night_audit_is_unchanged_without_a_provider(self):
		with self.installed():
			result = night_audit.check_open_pos_bills(PROP_A, YESTERDAY)
		self.assertEqual((result["count"], result["amount"]), (0, 0))
		self.assertEqual(self.outlet_calls(), [{"property": PROP_A, "business_date": YESTERDAY}])

	def test_open_outlet_checks_block_the_audit_by_default(self):
		self.answer = {"checks": [check(), check(name="PSINV-0002", amount=8000.5, billed=1, room="101",
		                                         reservation="RES-9")]}
		with self.installed():
			result = night_audit.check_open_pos_bills(PROP_A, YESTERDAY)
		self.assertEqual(result["severity"], "block")
		self.assertEqual(result["count"], 2)
		self.assertEqual(result["amount"], 50000.5)
		first, second = result["items"]
		self.assertEqual((first["order"], first["outlet"], first["table_no"], first["state"]),
		                 ("PSINV-0001", "Lakeside Grill", "T4", "open"))
		self.assertEqual((second["order"], second["room"], second["reservation"], second["state"]),
		                 ("PSINV-0002", "101", "RES-9", "served, not settled"))
		with self.installed():
			blockers = night_audit.open_blockers(night_audit.run_checks(PROP_A, YESTERDAY))
		self.assertIn("open_pos_bills", [c["key"] for c in blockers])

	def test_the_property_can_make_open_checks_a_warning(self):
		write_kv(PROP_A, "na_block_open_pos", 0)
		self.answer = {"checks": [check()]}
		with self.installed():
			result = night_audit.check_open_pos_bills(PROP_A, YESTERDAY)
			blockers = night_audit.open_blockers(night_audit.run_checks(PROP_A, YESTERDAY))
		self.assertEqual((result["severity"], result["count"]), ("warn", 1))
		self.assertNotIn("open_pos_bills", [c["key"] for c in blockers])

	def test_manage_end_of_day_counts_the_outlet_checks(self):
		self.answer = {"checks": [check(), check(name="PSINV-0002")]}
		with self.installed():
			rows = night_audit._raw(night_audit.eod_overview)()
		row = next(r for r in rows if r["property"] == PROP_A)
		self.assertEqual(row["open_pos_bills"], 2)
		self.assertIn("open_pos_bills", row["blocker_keys"])
