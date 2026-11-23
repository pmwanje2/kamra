"""Reservation discounts, round 5 (Gap Record #8555; Gemini 4 Neon's R4
re-review).

- R5-1 (P1-1) a governed import (Frappe's Data Import sets
  frappe.flags.in_import, and Document.insert then skips the DocType's
  defaults) books as the import writer it is; a REST insert or update still
  reaches no writer, whatever its payload says.
- R5-2 (P1-2) an agreed rate on a stay in house replaces the discount
  (OPERA's Fixed Rate): a night already on the folio keeps what it was
  charged, with no credit and no refusal under either policy, and only the
  nights still to come move to the agreed rate.
- R5-3 (P1-3) a room move that keeps the current rate re-prices nothing, so
  its preview asks for no sign-off, as the move asks for none.
- R5-4 (P1-4) a plain save never turns an existing stay's pricing off; the
  channel manager, its writer, still does.
- R5-5 (P2-1) a custom-rate room move at the stay's own agreed rate is the
  agreed rate's governed writer.
- R5-6 (P2-2) a limit by role that is not a finite number is no limit: the
  property's own applies.
- R5-7 (P2-4) a discount given at booking shows each night in the Changes Log."""

from __future__ import annotations

import frappe
from frappe.utils import flt

from kamra import api, settings
from kamra import channel_manager as cm
from kamra import folio as folio_mod
from kamra.tests.audit_fixtures import PROP_A, charges, d, make_reservation, make_room, make_room_type
from kamra.tests.test_reservation_discounts import DESK, GM, set_setting
from kamra.tests import test_reservation_discounts_r4 as r4
from kamra.tests.test_reservation_discounts_r2 import PROMO


class RoundFiveTestCase(r4.RoundFourTestCase):
	# the REST helpers of the round-4 flag tests (imported by module, so this
	# module does not run those tests again)
	rest = r4.RestFlagTests.rest
	payload = r4.RestFlagTests.payload

	def policy(self, value):
		settings.write_kv(PROP_A, "charged_night_rate_change", value)
		settings.clear_cache(PROP_A)

	def in_house(self, night_discount: dict):
		"""In house d(-1)..d(1) at 1000; d(-1) has a Daily Details discount
		of its own and is on the folio, d(0) is still to come."""
		res = make_reservation(PROP_A, self.rt, d(-1), d(1), status="Checked In", room=self.rooms[0])
		self.discount(res.name, **PROMO, supervisor_user=GM, supervisor_pin="4321",
		              night_discounts=[{"date": d(-1), **night_discount}])
		folio_mod.post_room_night(self.doc(res.name), d(-1))
		return res

	def billed(self, name):
		folio_mod.post_remaining_nights(self.doc(name))
		return sorted(round(flt(c.amount), 2) for c in charges(name, "Room"))


class GovernedImportTests(RoundFiveTestCase):
	"""R5-1 (P1-1): Frappe's import is a governed writer; a request never is."""

	def test_a_governed_import_books_although_it_skips_the_doctypes_defaults(self):
		was = frappe.flags.in_import
		frappe.flags.in_import = True
		try:
			res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.rooms[3])
		finally:
			frappe.flags.in_import = was
		self.assertTrue(frappe.db.exists("Reservation", res.name))

	def test_a_rest_write_naming_the_import_still_reaches_no_writer(self):
		from frappe.api import v1, v2
		with self.assertRaises(frappe.PermissionError):
			self.rest(v2.create_doc, "Reservation", **self.payload(
				auto_price=0, amount_after_tax=1, in_import=1, flags={"in_import": 1}))
		self.assertFalse(frappe.db.exists("Reservation", {
			"property": PROP_A, "room": self.rooms[3], "check_in_date": d(5)}))
		res = self.stay()
		for fn in (v1.update_doc, v2.update_doc):
			with self.subTest(api=fn.__module__):
				with self.assertRaises(frappe.PermissionError):
					self.rest(fn, "Reservation", res.name, auto_price=0, amount_after_tax=1,
					          in_import=1, flags={"in_import": 1})
		doc = self.doc(res.name)
		self.assertEqual((int(doc.auto_price), self.rows(res.name)), (1, [(1000, 0, 1000)] * 2))


class ChargedNightAgreedRateTests(RoundFiveTestCase):
	"""R5-2 (P1-2): a charged Daily Details night is never re-discounted by
	an agreed rate; it keeps what it was charged."""

	def test_credit_the_difference_credits_nothing_on_a_charged_discounted_night(self):
		self.policy("Credit the difference")
		res = self.in_house({"discount_type": "Percent", "discount_value": 50})
		self.assertEqual(self.rows(res.name), [(1000, 500, 500), (1000, 0, 1000)])
		self.agree(res.name, 900, GM)
		self.assertEqual(self.rows(res.name), [(1000, 500, 500), (900, 0, 900)])
		self.assertFalse(charges(res.name, "Rate Adjustment"))
		self.assertEqual(self.billed(res.name), [500, 900])
		self.assertAlmostEqual(flt(self.doc(res.name).amount_before_tax), 1400, places=2)

	def test_keep_charged_nights_keeps_a_charged_discount_above_the_new_rate(self):
		self.policy("Keep charged nights")
		res = self.in_house({"discount_type": "Amount", "discount_value": 950})
		self.assertEqual(self.rows(res.name), [(1000, 950, 50), (1000, 0, 1000)])
		self.agree(res.name, 900, GM)
		self.assertEqual(self.rows(res.name), [(1000, 950, 50), (900, 0, 900)])
		self.assertEqual(self.billed(res.name), [50, 900])

	def test_keep_charged_nights_never_rewrites_the_charged_nights_history(self):
		self.policy("Keep charged nights")
		res = self.in_house({"discount_type": "Percent", "discount_value": 50})
		self.agree(res.name, 900, GM)
		self.assertEqual(self.rows(res.name), [(1000, 500, 500), (900, 0, 900)])
		self.assertAlmostEqual(flt(self.doc(res.name).amount_before_tax), 1400, places=2)
		self.assertEqual(self.billed(res.name), [500, 900])


class KeepRateMovePreviewTests(RoundFiveTestCase):
	"""R5-3 (P1-3): "Keep current rate" re-prices nothing, so it judges nothing."""

	def test_a_keep_rate_move_preview_asks_for_no_sign_off(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		res = self.stay()
		self.discount(res.name, discount_type="Percent", discount_value=30,
		              supervisor_user=GM, supervisor_pin="4321", **PROMO)
		dlx = make_room_type(PROP_A, "DLX", base_price=1200)
		frappe.db.set_value("Room Type", dlx, "base_price", 1200)
		suite = make_room(PROP_A, dlx, "201")
		pv = self.as_user(DESK, api.room_move_preview, res.name, suite, rate_mode="source")
		self.assertFalse(pv["needs_supervisor"])
		self.assertIsNone(pv["discount_governance"])
		self.assertEqual(pv["supervisors"], [])
		self.assertEqual((pv["new_amount"], pv["difference"], pv["needs_confirmation"]),
		                 (pv["current_amount"], 0, False))
		# the move the preview describes: no PIN, the rates kept
		self.as_user(DESK, api.move_reservation, res.name, suite, rate_mode="source")
		doc = self.doc(res.name)
		self.assertEqual((doc.room, doc.room_type), (suite, dlx))
		self.assertEqual(self.rows(res.name), [(1000, 300, 700)] * 2)


class AutoPriceFlipTests(RoundFiveTestCase):
	"""R5-4 (P1-4): an existing stay's pricing is turned off only by its writers."""

	def test_a_plain_save_cannot_turn_an_existing_stays_pricing_off(self):
		from frappe.api import v1, v2
		res = self.stay()
		doc = self.doc(res.name)
		doc.auto_price, doc.amount_after_tax = 0, 1
		with self.assertRaises(frappe.PermissionError):
			self.as_user(DESK, doc.save, ignore_permissions=True)
		for fn in (v1.update_doc, v2.update_doc):
			with self.subTest(api=fn.__module__):
				with self.assertRaises(frappe.PermissionError):
					self.rest(fn, "Reservation", res.name, auto_price=0, amount_after_tax=1)
		doc = self.doc(res.name)
		self.assertEqual(int(doc.auto_price), 1)
		self.assertEqual(self.rows(res.name), [(1000, 0, 1000)] * 2)

	def test_the_channel_manager_still_turns_a_modified_stays_pricing_off(self):
		ref = "UT-R5-" + frappe.generate_hash(length=8)
		conn = frappe.get_doc({"doctype": "Channel Manager Connection", "property": PROP_A,
		                       "provider": "AioSell", "active": 1,
		                       "external_property_id": "ut-r5-" + frappe.generate_hash(length=6)}
		                      ).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Channel Room Mapping", "connection": conn.name,
		                "room_type": self.rt, "external_room_id": "ut-r5-std"}).insert(ignore_permissions=True)
		res = self.stay(ota_ref=ref, source="OTA", channel="Booking.com")
		self.assertEqual(int(self.doc(res.name).auto_price), 1)
		# a modify the hotel has to resolve: the channel's stay, held as sent
		out = cm._apply_event(conn, {
			"event": "modify", "ota_ref": ref, "channel": "Booking.com",
			"room_type_external_id": "ut-r5-std", "check_in": d(5), "check_out": d(7),
			"adults": 2, "children": 0, "total": 0}, requested_reason="UT resolve")
		self.assertEqual(out["result"], "modified")
		self.assertEqual(int(self.doc(res.name).auto_price), 0)


class CustomRateMoveTests(RoundFiveTestCase):
	"""R5-5 (P2-1): a custom-rate move is the agreed rate's writer, whatever the rate."""

	def test_a_custom_rate_move_at_the_stays_own_agreed_rate(self):
		out = self.as_user(GM, api.create_group_with_rooming_list, PROP_A, "UT R5 Group", d(5), d(7),
		                   [{"room_type": self.rt, "rooms_blocked": 1, "block_rate": 900}],
		                   [{"room_type": self.rt, "guest_name": "UT Group Lead"}])
		name = out["created"][0]
		doc = self.doc(name)
		self.assertEqual((flt(doc.agreed_rate), int(doc.fixed_rate)), (900, 0))
		self.as_user(GM, api.move_reservation, name, self.rooms[4], rate_mode="custom",
		             agreed_rate=900, confirm_price=1)
		doc = self.doc(name)
		self.assertEqual((doc.room, flt(doc.agreed_rate), int(doc.fixed_rate)), (self.rooms[4], 900, 1))
		self.assertEqual(self.rows(name), [(900, 0, 900)] * 2)


class FiniteRoleLimitTests(RoundFiveTestCase):
	"""R5-6 (P2-2): a role's limit is a finite number or no limit at all."""

	def test_a_role_limit_that_is_not_a_finite_number_is_ignored(self):
		set_setting(PROP_A, "rate_override_limit_pct", 10)
		for bad in ("nan", "inf", "-inf"):
			with self.subTest(limit=bad):
				set_setting(PROP_A, "rate_override_limits_by_role", f"Front Desk: {bad}")
				self.assertEqual(self.as_user(DESK, api.desk_limit_pct, PROP_A), 10)
		set_setting(PROP_A, "rate_override_limits_by_role", "Front Desk: nan")
		res = self.stay()
		pv = self.as_user(DESK, api.discount_preview, res.name, discount_type="Percent",
		                  discount_value=95, **PROMO)
		self.assertEqual((pv["limit_pct"], pv["needs_supervisor"]), (10, True))
		with self.assertRaises(frappe.PermissionError):
			self.discount(res.name, discount_type="Percent", discount_value=95, **PROMO)


class BookingDiscountChangesLogTests(RoundFiveTestCase):
	"""R5-7 (P2-4): the booking's own discount names each night it took off."""

	def test_a_discount_given_at_booking_shows_each_night(self):
		name = self.book(DESK, discount_type="Percent", discount_value=10,
		                 discount_reason="RATE-PROMO")["reservation"]
		entries = self.as_user(DESK, api.reservation_changes, name)["entries"]
		disc = next(e for e in entries if e.get("action_type") == "reservation_discount")
		self.assertEqual([(n["date"], n["from"], n["to"]) for n in disc["nights"]],
		                 [(d(5), 1000, 900), (d(6), 1000, 900)])
