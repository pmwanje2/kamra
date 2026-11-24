"""Composed behaviour where PR #32 (reservation discounts, rate authority,
discount trail) meets the v2.8.0 PRs merged forward (#35, #37, #39, #40, #42, #46).

- KwdDiscountMinorUnitTests (#35 x #32): 3-decimal currency (KWD,
  pricing.night_rate_places == 3) across quote, Daily Details, tax-inclusive
  discounts, in-place rediscount, night posting, amend dates and rate override.
- ChannelModifyDiscountTests (#46/#39 x #32): pre-arrival OTA modify with a
  channel price or tax hold cleanly supersedes a desk discount instead of
  crashing in guard_discount_inputs or leaving stale header discount fields.
- SplitDiscountedNightTests (#37 x #32): append-only split of a discounted
  tax-inclusive room night conserves gross/tax/net, passes bridge void fences,
  prevents double posting, and composes with rediscount, amend_stay and
  set_agreed_rate.
- GroupPickupDiscountBillingTests (#37 x #32): discounted group pickup judged
  against the block rate, force_advance_bill routed to the Group master folio,
  OPERA posting master kept open across member checkout, group advances,
  settle_group_bill, rate override on a group pickup, and Kamra / bridge
  credit-and-rebill on a closed discounted invoice.
- InHouseChannelGuardDiscountTests (#46 x #32): in-house discounted OTA stay
  blocks OTA modify/cancel with channel_review_needed, preserves discount and
  locked nights, exposes both in reservation_detail, and composes with
  set_reservation_discount and clear_channel_review."""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal
from unittest.mock import patch

import frappe
from frappe.utils import flt

from kamra import api, ledger, pricing, settings
from kamra import channel_manager as cm
from kamra import folio as folio_mod
from kamra.checkout import settlement_position
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	d,
	guest_folio,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)
from kamra.tests.bridge_fields import ensure_bridge_field
from kamra.tests.test_reservation_discounts import DESK, REVENUE, set_setting
from kamra.tests.test_reservation_discounts_r2 import PROMO
from kamra.tests.test_reservation_discounts_r4 import RoundFourTestCase
from kamra.tests.test_split_and_move_audit import bal, bridge_void_refusals, ledger_net

PROP_KWD = "UT Disc KWD"
PROP_SPLIT = "UT Disc Split"
BRIDGE_FLOW = "hospitality_kamra.credit_and_rebill"


def _corp(name: str = "UT Disc Corp Ltd", credit: int = 1) -> str:
	if not frappe.db.exists("Corporate Account", name):
		frappe.get_doc({
			"doctype": "Corporate Account",
			"company_name": name,
			"credit_allowed": credit,
			"credit_limit": 500000,
			"credit_days": 30,
		}).insert(ignore_permissions=True)
	return name


class KwdDiscountMinorUnitTests(RoundFourTestCase):
	"""#35 night_rate_places (3 decimals for KWD) composed with #32 discounts,
	quote, in-place rediscount, night posting, amend dates and rate override."""

	properties = (PROP_A, PROP_KWD)

	def _kwd_setup(self, *, price: float = 12.345, tax: float = 0.0, inclusive: int = 0):
		make_property(
			PROP_KWD,
			business_date=TODAY,
			country="Kuwait",
			currency="KWD",
			gst_mode="Fixed",
			gst_rate_low=tax,
			gst_rate_high=tax,
			rates_include_tax=inclusive,
		)
		frappe.db.delete("Property Setting", {"property": PROP_KWD})
		set_setting(PROP_KWD, "rate_override_limit_pct", 20)
		code = f"KWD{int(tax)}{inclusive}"
		rt = make_room_type(PROP_KWD, code, base_price=price, tax_percent=tax)
		frappe.db.set_value("Room Type", rt, {"base_price": price, "tax_percent": tax})
		rooms = [make_room(PROP_KWD, rt, f"K{inclusive}{int(tax)}{i}") for i in (1, 2)]
		return rt, rooms

	def test_kwd_daily_details_quote_and_booking_keep_three_decimals(self):
		rt, rooms = self._kwd_setup(price=12.345, tax=0.0, inclusive=0)
		q = pricing.quote(
			PROP_KWD, rt, d(5), d(7), 2, 0,
			night_discounts=[{"date": d(6), "discount_type": "Percent", "discount_value": 10}],
		)
		n0, n1 = q["nightly"]
		# Night 1 has no discount: its 3rd decimal must never be truncated to 12.34
		self.assertEqual(Decimal(str(n0["rate"])), Decimal("12.345"))
		self.assertEqual(Decimal(str(n0["base_rate"])), Decimal("12.345"))
		# Night 2 takes 10% off 12.345 = 1.2345 -> net 11.111 (3 decimals)
		self.assertEqual(Decimal(str(n1["base_rate"])), Decimal("12.345"))
		self.assertEqual(Decimal(str(n1["rate"])), Decimal("11.111"))
		self.assertEqual(Decimal(str(n1["discount_amount"])), Decimal("1.234"))

		# A 3-decimal Amount discount is valid on a 3-decimal currency (KWD)
		q_amt = pricing.quote(
			PROP_KWD, rt, d(5), d(6), 2, 0,
			discount_type="Amount", discount_value="1.235",
		)
		self.assertEqual(Decimal(str(q_amt["nightly"][0]["rate"])), Decimal("11.110"))
		self.assertEqual(Decimal(str(q_amt["nightly"][0]["discount_amount"])), Decimal("1.235"))

		# Locked booking rows preserve 3 decimals on rate, base_rate, discount_amount and rate_code_amount
		out = self.as_user(
			DESK, api.create_booking, PROP_KWD, rt, d(5), d(7), "KWD Guest",
			room=rooms[0], discount_reason="RATE-PROMO",
			night_discounts=[{"date": d(6), "discount_type": "Percent", "discount_value": 10}],
		)
		doc = self.doc(out["reservation"])
		r0, r1 = doc.night_rates
		self.assertEqual(Decimal(str(r0.rate)), Decimal("12.345"))
		self.assertEqual(Decimal(str(r0.rate_code_amount)), Decimal("12.345"))
		self.assertEqual(Decimal(str(r1.base_rate)), Decimal("12.345"))
		self.assertEqual(Decimal(str(r1.rate)), Decimal("11.111"))
		self.assertEqual(Decimal(str(r1.discount_amount)), Decimal("1.234"))
		self.assertEqual(Decimal(str(r1.rate_code_amount)), Decimal("12.345"))

	def test_kwd_tax_inclusive_discount_keeps_three_decimal_net_and_exact_gross(self):
		rt, rooms = self._kwd_setup(price=10.000, tax=10.0, inclusive=1)
		# At booking: 10.000 gross less 10% = 9.000 gross -> net 9.000 / 1.1 = 8.182 KWD (not 8.18)
		out = self.as_user(
			DESK, api.create_booking, PROP_KWD, rt, d(5), d(6), "KWD Inc Book",
			room=rooms[0], discount_type="Percent", discount_value=10,
			discount_reason="RATE-PROMO",
		)
		doc = self.doc(out["reservation"])
		self.assertEqual(Decimal(str(doc.night_rates[0].rate)), Decimal("8.182"))
		self.assertEqual(Decimal(str(doc.night_rates[0].discount_amount)), Decimal("1.000"))

		# In-place rediscount on a booked stay produces the same 3-decimal net and exact 9.000 gross on folio
		res = make_reservation(PROP_KWD, rt, d(-1), d(0), status="Checked In", room=rooms[1])
		self.assertEqual(Decimal(str(self.doc(res.name).night_rates[0].rate)), Decimal("9.091"))
		self.as_user(
			DESK, api.set_reservation_discount, res.name,
			discount_type="Percent", discount_value=10, reason_code="RATE-PROMO",
		)
		doc2 = self.doc(res.name)
		self.assertEqual(Decimal(str(doc2.night_rates[0].rate)), Decimal("8.182"))
		folio_mod.post_room_night(doc2, d(-1))
		line = next(c for c in frappe.get_doc("Folio", guest_folio(res.name)).charges
		            if c.charge_type == "Room")
		self.assertEqual(Decimal(str(line.amount)), Decimal("8.182"))
		billed_gross = Decimal(str(line.total)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
		self.assertEqual(billed_gross, Decimal("9.000"))

	def test_kwd_charged_night_keeps_three_decimals_across_amend_and_rate_override(self):
		rt, rooms = self._kwd_setup(price=12.345, tax=0.0, inclusive=0)
		settings.write_kv(PROP_KWD, "charged_night_rate_change", "Keep charged nights")
		settings.clear_cache(PROP_KWD)
		res = make_reservation(PROP_KWD, rt, d(-1), d(1), status="Checked In", room=rooms[0])
		folio_mod.post_room_night(self.doc(res.name), d(-1))

		charged = folio_mod.charged_nights(res.name)
		self.assertEqual(Decimal(str(charged[d(-1)]["base"])), Decimal("12.345"))

		# Extend stay by 1 night: charged night 1 must stay 12.345
		self.as_user(DESK, api.amend_stay, res.name, d(-1), d(2), confirm_price=1)
		self.assertEqual(Decimal(str(self.doc(res.name).night_rates[0].rate)), Decimal("12.345"))

		# Rate override on future nights: charged night 1 must keep 12.345
		self.as_user(
			REVENUE, api.set_agreed_rate, res.name, 11.115, "RATE-PROMO", confirm_price=1,
		)
		doc = self.doc(res.name)
		self.assertEqual(Decimal(str(doc.night_rates[0].rate)), Decimal("12.345"))
		self.assertEqual(Decimal(str(doc.night_rates[1].rate)), Decimal("11.115"))

	def test_kwd_discount_amount_and_rediscount_snapshot_preserve_three_decimals(self):
		rt, rooms = self._kwd_setup(price=10.000, tax=0.0, inclusive=0)
		out = self.as_user(
			DESK, api.create_booking, PROP_KWD, rt, d(5), d(7), "KWD Disc Header",
			room=rooms[0], discount_type="Amount", discount_value="1.234",
			discount_reason="RATE-PROMO",
		)
		doc = self.doc(out["reservation"])
		self.assertEqual(Decimal(str(doc.discount_amount)), Decimal("2.468"))
		snap = json.loads(doc.quote_snapshot)
		self.assertEqual(Decimal(str(snap["discount"])), Decimal("2.468"))

		# In-place rediscount must also keep all 3 decimals on discount_amount and snapshot totals
		self.as_user(
			DESK, api.set_reservation_discount, out["reservation"],
			discount_type="Amount", discount_value="1.456", reason_code="RATE-PROMO",
		)
		doc2 = self.doc(out["reservation"])
		self.assertEqual(Decimal(str(doc2.discount_amount)), Decimal("2.912"))
		snap2 = json.loads(doc2.quote_snapshot)
		self.assertEqual(Decimal(str(snap2["discount"])), Decimal("2.912"))
		self.assertEqual(Decimal(str(snap2["totals"]["discount"])), Decimal("2.912"))
		self.assertEqual(Decimal(str(snap2["room_total"])), Decimal("17.088"))


class ChannelModifyDiscountTests(RoundFourTestCase):
	"""#46 / #39 pre-arrival OTA modify with a channel price or tax hold
	superseding a desk discount on a pre-arrival OTA booking."""

	def _ota_conn(self):
		conn = frappe.get_doc({
			"doctype": "Channel Manager Connection",
			"property": PROP_A,
			"provider": "AioSell",
			"active": 1,
			"external_property_id": "ut-disc-ota",
		}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Channel Room Mapping",
			"connection": conn.name,
			"room_type": self.rt,
			"external_room_id": "std",
		}).insert(ignore_permissions=True)
		return conn

	def _pre_arrival_ota(self, ref: str):
		res = make_reservation(
			PROP_A, self.rt, d(5), d(7), status="Confirmed", room=self.rooms[0],
			source="OTA", channel="Booking.com", ota_ref=ref, auto_price=1,
		)
		return res

	def test_pre_arrival_ota_modify_with_price_supersedes_desk_daily_details_discount(self):
		conn = self._ota_conn()
		ref = "OTA-DISC-" + frappe.generate_hash(length=6)
		res = self._pre_arrival_ota(ref)
		# Desk applies a Daily Details discount + stay-level discount on the pre-arrival OTA stay
		self.discount(
			res.name, discount_type="Percent", discount_value=5, **PROMO,
			night_discounts=[{"date": d(5), "discount_type": "Percent", "discount_value": 10}],
		)
		before = self.doc(res.name)
		self.assertEqual(before.discount_type, "Percent")
		self.assertEqual(before.night_rates[0].discount_type, "Percent")

		# OTA sends a modify with its own channel total (2400)
		event = {
			"event": "modify", "ota_ref": ref, "booking_id": ref,
			"channel": "Booking.com", "room_type_external_id": "std",
			"check_in": d(5), "check_out": d(7), "adults": 2, "children": 0,
			"total": 2400, "currency": "INR",
			"nightly": [{"date": d(5), "amount": 1200}, {"date": d(6), "amount": 1200}],
			"notes": "OTA revised rate",
		}
		out = cm._apply_event(conn, event)
		self.assertEqual(out["result"], "modified")
		doc = self.doc(res.name)
		self.assertEqual(int(doc.auto_price), 0)
		self.assertEqual(flt(doc.amount_after_tax), 2400.0)
		# Desk discount header and night discount inputs are superseded cleanly
		self.assertFalse(doc.get("discount_type"))
		self.assertEqual(flt(doc.get("discount_value")), 0.0)
		self.assertFalse(doc.get("discount_reason"))
		self.assertEqual(int(doc.get("fixed_rate") or 0), 0)
		self.assertFalse(any(r.get("discount_type") for r in doc.night_rates))

	def test_pre_arrival_ota_modify_tax_hold_clears_nights_and_desk_discount(self):
		conn = self._ota_conn()
		ref = "OTA-TAXH-" + frappe.generate_hash(length=6)
		res = self._pre_arrival_ota(ref)
		self.discount(
			res.name, **PROMO,
			night_discounts=[{"date": d(5), "discount_type": "Percent", "discount_value": 10}],
		)
		# OTA modify triggers #39 unread-tax hold (total becomes 0, status Requested)
		event = {
			"event": "modify", "ota_ref": ref, "booking_id": ref,
			"channel": "Booking.com", "room_type_external_id": "std",
			"check_in": d(5), "check_out": d(7), "adults": 2, "children": 0,
			"total": 2400, "currency": "INR",
			"unread_taxes": ["Mystery Levy"],
			"notes": "OTA tax hold test",
		}
		out = cm._apply_event(conn, event)
		self.assertEqual(out["result"], "modified")
		doc = self.doc(res.name)
		self.assertEqual(doc.status, "Requested")
		self.assertEqual(int(doc.auto_price), 0)
		self.assertEqual(flt(doc.amount_after_tax), 0.0)
		self.assertEqual(len(doc.night_rates), 0)
		self.assertFalse(doc.get("discount_type"))
		self.assertIn("OTA tax not recognised", doc.special_requests or "")

	def test_pre_arrival_ota_modify_superseding_discount_records_trail_logs_action_and_flags_review(self):
		conn = self._ota_conn()
		ref = "OTA-R7-" + frappe.generate_hash(length=6)
		res = self._pre_arrival_ota(ref)
		self.discount(
			res.name, discount_type="Percent", discount_value=5, **PROMO,
			night_discounts=[{
				"date": d(5), "discount_type": "Percent", "discount_value": 10,
				"discount_reason": "ADJ-RATE",
			}],
		)

		out = cm._apply_event(conn, {
			"event": "modify", "ota_ref": ref, "booking_id": ref,
			"channel": "Booking.com", "room_type_external_id": "std",
			"check_in": d(5), "check_out": d(7), "adults": 2, "children": 0,
			"total": 2400, "currency": "INR",
			"nightly": [{"date": d(5), "amount": 1200}, {"date": d(6), "amount": 1200}],
			"notes": "OTA revised rate",
		})
		self.assertEqual(out["result"], "modified")
		doc = self.doc(res.name)
		self.assertEqual(int(doc.channel_review_needed), 1)
		self.assertIn("5%", doc.special_requests or "")
		self.assertIn("RATE-PROMO", doc.special_requests or "")
		self.assertIn("10%", doc.special_requests or "")
		self.assertIn("ADJ-RATE", doc.special_requests or "")
		self.assertNotRegex(doc.special_requests or "", r"(?i)kamra|frappe|erpnext|ury")

		# Discount trail (reservation_discount) records old discount, reason and before/after nights
		disc_logs = frappe.get_all(
			"Agent Action Log",
			filters={
				"reference_doctype": "Reservation", "reference_name": res.name,
				"action_type": "reservation_discount", "action_channel": "API",
			},
			fields=["name", "rationale", "before_snapshot", "after_snapshot"],
			order_by="creation desc",
		)
		self.assertTrue(disc_logs, "Expected an API reservation_discount trail entry")
		trail = disc_logs[0]
		for token in ("5%", "RATE-PROMO", "10%", "ADJ-RATE"):
			self.assertIn(token, trail.rationale or "")
		self.assertNotRegex(trail.rationale or "", r"(?i)kamra|frappe|erpnext|ury")
		before_snap = json.loads(trail.before_snapshot)
		after_snap = json.loads(trail.after_snapshot)
		self.assertEqual(before_snap["discount_type"], "Percent")
		self.assertEqual(flt(before_snap["discount_value"]), 5.0)
		self.assertEqual(before_snap["discount_reason"], "RATE-PROMO")
		self.assertEqual(len(before_snap["nights"]), 2)
		self.assertIsNone(after_snap["discount_type"])
		self.assertEqual(flt(after_snap["discount_total"]), 0.0)
		self.assertEqual(flt(after_snap["amount_after_tax"]), 2400.0)

		# Channel review Agent Action Log (#46 mechanism) surfaces in reservation_detail
		review_logs = frappe.get_all(
			"Agent Action Log",
			filters={
				"reference_doctype": "Reservation", "reference_name": res.name,
				"action_type": "channel.modify_needs_review",
			},
			fields=["name", "rationale"],
			order_by="creation desc",
		)
		self.assertTrue(review_logs, "Expected a channel.modify_needs_review Agent Action Log entry")
		for token in ("5%", "RATE-PROMO", "10%", "ADJ-RATE"):
			self.assertIn(token, review_logs[0].rationale or "")
		self.assertNotRegex(review_logs[0].rationale or "", r"(?i)kamra|frappe|erpnext|ury")

		detail = self.as_user(DESK, api.reservation_detail, res.name)
		self.assertEqual(detail["channel_review_needed"], 1)
		self.assertTrue(detail["warnings"]["channel_review_needed"])
		self.assertTrue(any(
			"RATE-PROMO" in (r.get("rationale") or "") and "5%" in (r.get("rationale") or "")
			for r in detail["channel_reviews"]
		))

		changes = self.as_user(DESK, api.reservation_changes, res.name)["entries"]
		api_disc = next(
			(e for e in changes if e.get("action_type") == "reservation_discount"
			 and "Booking.com" in (e.get("rationale") or "")),
			None,
		)
		self.assertIsNotNone(api_disc)
		self.assertTrue(api_disc["nights"])

		# Existing #46 clear_channel_review clears the flag cleanly
		self.as_user(DESK, cm.clear_channel_review, res.name)
		self.assertEqual(int(self.doc(res.name).channel_review_needed), 0)

	def test_pre_arrival_ota_modify_tax_hold_superseding_daily_details_flags_review_and_logs_trail(self):
		conn = self._ota_conn()
		ref = "OTA-R7H-" + frappe.generate_hash(length=6)
		res = self._pre_arrival_ota(ref)
		self.discount(
			res.name, **PROMO,
			night_discounts=[{"date": d(5), "discount_type": "Percent", "discount_value": 10}],
		)

		out = cm._apply_event(conn, {
			"event": "modify", "ota_ref": ref, "booking_id": ref,
			"channel": "Booking.com", "room_type_external_id": "std",
			"check_in": d(5), "check_out": d(7), "adults": 2, "children": 0,
			"total": 2400, "currency": "INR",
			"unread_taxes": ["Mystery Levy"],
		})
		self.assertEqual(out["result"], "modified")
		doc = self.doc(res.name)
		self.assertEqual(doc.status, "Requested")
		self.assertEqual(int(doc.channel_review_needed), 1)
		self.assertIn("10%", doc.special_requests or "")
		self.assertIn("RATE-PROMO", doc.special_requests or "")
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": res.name,
			"action_type": "reservation_discount", "action_channel": "API",
		}))
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": res.name,
			"action_type": "channel.modify_needs_review",
		}))

	def test_pre_arrival_ota_modify_without_desk_discount_does_not_flag_review(self):
		conn = self._ota_conn()
		ref = "OTA-R7N-" + frappe.generate_hash(length=6)
		res = self._pre_arrival_ota(ref)

		out = cm._apply_event(conn, {
			"event": "modify", "ota_ref": ref, "booking_id": ref,
			"channel": "Booking.com", "room_type_external_id": "std",
			"check_in": d(5), "check_out": d(7), "adults": 2, "children": 0,
			"total": 2400, "currency": "INR",
			"nightly": [{"date": d(5), "amount": 1200}, {"date": d(6), "amount": 1200}],
		})
		self.assertEqual(out["result"], "modified")
		self.assertEqual(int(self.doc(res.name).channel_review_needed), 0)
		self.assertFalse(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": res.name,
			"action_type": "reservation_discount",
		}))
		self.assertFalse(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": res.name,
			"action_type": "channel.modify_needs_review",
		}))

	def test_lock_channel_rates_zero_total_sets_writer_flag_for_direct_callers(self):
		"""Direct caller pin (kills mutant M2 on channel_manager.lock_channel_rates):
		clearing locked nights with total <= 0 sets kamra_night_rates_writer before
		returning so guard_locked_nights admits the save."""
		ref = "OTA-M2-" + frappe.generate_hash(length=6)
		res = self._pre_arrival_ota(ref)
		doc = self.doc(res.name)
		self.assertTrue(doc.night_rates)
		self.assertFalse(doc.flags.get("kamra_night_rates_writer"))
		doc.auto_price = 0
		doc.amount_after_tax = 0
		cm.lock_channel_rates(doc, 0)
		self.assertTrue(doc.flags.get("kamra_night_rates_writer"))
		doc.save(ignore_permissions=True)
		self.assertEqual(len(self.doc(res.name).night_rates), 0)


class SplitDiscountedNightTests(RoundFourTestCase):
	"""Pin (#37 x #32): splitting a posted discounted tax-inclusive room night
	conserves gross/tax/net, passes bridge void fences, prevents double posting,
	and composes cleanly with set_reservation_discount, amend_stay and set_agreed_rate."""

	properties = (PROP_A, PROP_SPLIT)

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_bridge_field("Folio Charge", "source_ref")

	def test_split_discounted_tax_inclusive_night_conserves_tax_and_holds_across_rediscount_override_and_amend(self):
		# Zero-decimal currency (JPY) with 18% inclusive tax: 100,000 less 10% = 90,000 gross
		make_property(
			PROP_SPLIT,
			business_date=TODAY,
			country="Japan",
			currency="JPY",
			gst_mode="Fixed",
			gst_rate_low=18,
			gst_rate_high=18,
			rates_include_tax=1,
		)
		frappe.db.delete("Property Setting", {"property": PROP_SPLIT})
		set_setting(PROP_SPLIT, "rate_override_limit_pct", 20)
		set_setting(PROP_SPLIT, "charged_night_rate_change", "Keep charged nights")
		rt = make_room_type(PROP_SPLIT, "JDLX", base_price=100000, tax_percent=18)
		frappe.db.set_value("Room Type", rt, {"base_price": 100000, "tax_percent": 18})
		room = make_room(PROP_SPLIT, rt, "J101")

		res = make_reservation(PROP_SPLIT, rt, d(-1), d(1), status="Checked In", room=room)
		self.as_user(
			DESK, api.set_reservation_discount, res.name,
			discount_type="Percent", discount_value=10, reason_code="RATE-PROMO",
		)
		# 90,000 / 1.18 = 76,271.19 net, 13,728.81 tax
		self.assertAlmostEqual(flt(self.doc(res.name).night_rates[0].rate), 76271.19, places=2)
		self.assertAlmostEqual(flt(self.doc(res.name).night_rates[0].discount_amount), 10000.0, places=2)

		main = guest_folio(res.name)
		extra = folio_mod.split_folio(res.name, "Extra")
		folio_mod.post_room_night(self.doc(res.name), d(-1))
		orig_line = next(c for c in frappe.get_doc("Folio", main).charges if c.charge_type == "Room")
		self.assertIn("100,000 less discount 10,000", orig_line.description)

		# Append-only 50/50 split onto Extra folio
		split_out = folio_mod.split_charge(main, orig_line.name, extra, percent=50)
		self.assertTrue(split_out["reversal"])
		for f in (main, extra):
			self.assertEqual(bridge_void_refusals(f), [], f)
			self.assertEqual(ledger_net(f), bal(f), f)
		standing = [
			c for f in (main, extra)
			for c in frappe.get_doc("Folio", f).charges
			if c.get("split_of") == orig_line.name and not c.get("reversal_of")
		]
		self.assertEqual(len(standing), 2)
		self.assertAlmostEqual(sum(flt(c.amount) for c in standing), 76271.19, places=2)
		self.assertAlmostEqual(sum(flt(c.total) for c in standing), 90000.0, places=2)

		# Re-posting night 1 is a no-op (not double-posted)
		self.assertFalse(folio_mod.post_room_night(self.doc(res.name), d(-1)))

		# Updating discount to 15%: split night 1 stays untouched; uncharged night 2 becomes 85,000 gross
		self.as_user(
			DESK, api.set_reservation_discount, res.name,
			discount_type="Percent", discount_value=15, reason_code="RATE-PROMO",
		)
		doc = self.doc(res.name)
		self.assertAlmostEqual(flt(doc.night_rates[0].rate), 76271.19, places=2)
		self.assertAlmostEqual(flt(doc.night_rates[0].discount_amount), 10000.0, places=2)
		self.assertEqual(round(flt(doc.night_rates[1].rate) * 1.18), 85000)
		self.assertEqual(round(flt(doc.night_rates[1].discount_amount)), 15000)

		# Amend stay +1 night: split night 1 keeps billed history; night 3 takes 15% discount
		self.as_user(DESK, api.amend_stay, res.name, d(-1), d(2), confirm_price=1)
		doc = self.doc(res.name)
		self.assertEqual(len(doc.night_rates), 3)
		self.assertAlmostEqual(flt(doc.night_rates[0].rate), 76271.19, places=2)
		self.assertAlmostEqual(flt(doc.night_rates[0].discount_amount), 10000.0, places=2)
		self.assertEqual(round(flt(doc.night_rates[2].discount_amount)), 15000)

		# Set agreed rate (Keep charged nights): split night 1 keeps 76271.19; future nights move to 88,000 gross
		self.as_user(REVENUE, api.set_agreed_rate, res.name, 88000, "RATE-PROMO", confirm_price=1)
		doc = self.doc(res.name)
		self.assertAlmostEqual(flt(doc.night_rates[0].rate), 76271.19, places=2)
		self.assertAlmostEqual(flt(doc.night_rates[0].discount_amount), 10000.0, places=2)
		self.assertAlmostEqual(flt(doc.night_rates[1].rate), round(88000 / 1.18, 2), places=2)
		self.assertEqual(flt(doc.night_rates[1].discount_amount), 0.0)


class GroupPickupDiscountBillingTests(RoundFourTestCase):
	"""Pin (#37 x #32): discounted group pickup + group posting master,
	force_advance_bill, group advance transfer, settle_group_bill, rate override
	on a group pickup, and Kamra / bridge credit-and-rebill on a discounted invoice."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_bridge_field("Folio", "sales_invoice")

	def test_group_pickup_with_discount_routes_advance_bill_and_stays_open_as_posting_master(self):
		corp = _corp()
		gb = frappe.get_doc({
			"doctype": "Group Booking",
			"property": PROP_A,
			"group_name": "UT Discount Group",
			"company": corp,
			"check_in_date": d(-1),
			"check_out_date": d(1),
			"status": "Confirmed",
			"blocks": [{"room_type": self.rt, "rooms_blocked": 3, "block_rate": 800}],
		}).insert(ignore_permissions=True)

		# Pickup Room 1 with a 10% desk discount off the 800 block rate (= 720, within 15% limit)
		r1_out = self.as_user(
			DESK, api.create_booking, PROP_A, self.rt, d(-1), d(1), "Group Guest 1",
			booking_type="Group", company=corp, group_booking=gb.name,
			room=self.rooms[0],
			discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO",
		)
		r1 = self.doc(r1_out["reservation"])
		self.assertEqual(flt(r1.agreed_rate), 800.0)
		self.assertEqual(self.rows(r1.name), [(800, 80, 720)] * 2)

		# Pickup Room 2 at full block rate (800) via pickup_group_room
		r2_out = self.as_user(DESK, api.pickup_group_room, gb.name, self.rt, "Group Guest 2")
		r2 = self.doc(r2_out["reservation"])
		self.assertEqual(self.rows(r2.name), [(800, 0, 800)] * 2)

		# Check in Room 1 (anchor of Group master) and Room 2
		frappe.db.set_value("Reservation", r1.name, "status", "Checked In")
		frappe.db.set_value("Reservation", r2.name, "status", "Checked In")

		# force_advance_bill posts Room 1's discounted nights onto the Group master folio
		adv = ledger.force_advance_bill(r1.name, "entire")
		group_folio = adv["folio"]
		self.assertEqual(frappe.db.get_value("Folio", group_folio, "folio_type"), "Group")
		# 2 nights * 720 * 1.05 (5% default tax) = 1512 (or whatever tax applies on PROP_A)
		r1_expected_gross = round(flt(r1.amount_after_tax), 2)
		self.assertAlmostEqual(flt(adv["amount"]), r1_expected_gross, places=2)
		# Room lines carry the discount audit trail on the Group folio
		r1_lines = [c for c in frappe.get_doc("Folio", group_folio).charges if c.reservation == r1.name]
		self.assertTrue(all("800.00 less discount 80.00" in c.description for c in r1_lines))

		# Group advance payment on the Group folio transfers to Guest Ledger while in house
		api.add_folio_payment(group_folio, "Bank Transfer", 500, kind="Advance")
		self.assertTrue(frappe.db.exists("Folio Ledger Entry", {
			"folio": group_folio, "ledger": "Guest", "credit": 500,
		}))

		# Room 1's settlement position leaves the Group master bill out (OPERA posting master)
		pos = settlement_position(r1.name)
		self.assertNotIn(group_folio, [f for f, _c, _b in pos["to_city_ledger"]])
		self.assertEqual(pos["owed"], 0.0)

		# Room 1 checks out; Group master folio stays Open for Room 2
		self.as_user(DESK, api.check_out, r1.name)
		self.assertEqual(frappe.db.get_value("Folio", group_folio, "status"), "Open")

		# Room 2 posts its nights onto the same Group master folio, then settle_group_bill closes it
		folio_mod.post_remaining_nights(self.doc(r2.name))
		settle = api.settle_group_bill(gb.name, to_city_ledger=1)
		self.assertEqual(settle["folio"], group_folio)
		self.assertEqual(frappe.db.get_value("Folio", group_folio, "status"), "Closed")

		# Rate override on a discounted group pickup replaces the discount and judges cut vs 800 block rate
		r3_out = self.as_user(
			DESK, api.create_booking, PROP_A, self.rt, d(5), d(7), "Group Guest 3",
			booking_type="Group", company=corp, group_booking=gb.name,
			room=self.rooms[2],
			discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO",
		)
		# 700 is 12.5% off 800 (within 15% Front Desk limit, NOT 30% off 1000 card!)
		self.agree(r3_out["reservation"], 700, DESK)
		self.assertEqual(self.rows(r3_out["reservation"]), [(700, 0, 700)] * 2)

	def test_closed_discounted_folio_credit_note_tax_and_bridge_credit_and_rebill(self):
		# Tax-inclusive slab property: 8,000 @ 18% -> 10% desk discount -> 7,200 @ 5% slab!
		make_property(
			PROP_A, business_date=TODAY, rates_include_tax=1,
			gst_mode="Slab", gst_slab_threshold=7500, gst_rate_low=5, gst_rate_high=18,
		)
		rt = make_room_type(PROP_A, "SLB", base_price=8000)
		frappe.db.set_value("Room Type", rt, "base_price", 8000)
		room = make_room(PROP_A, rt, "S101")
		res = make_reservation(PROP_A, rt, d(-1), d(0), status="Checked In", room=room)
		self.discount(res.name, discount_type="Percent", discount_value=10, **PROMO)

		# Post discounted night (7,200 gross @ 5% = 6,857.14 net + 342.86 tax) and close folio
		folio_name = guest_folio(res.name)
		folio_mod.post_room_night(self.doc(res.name), d(-1))
		fdoc = frappe.get_doc("Folio", folio_name)
		self.assertAlmostEqual(flt(fdoc.grand_total), 7200.0, places=2)
		self.assertAlmostEqual(flt(fdoc.tax_total), 342.86, places=2)
		api.add_folio_payment(folio_name, "Cash", flt(fdoc.grand_total))
		folio_mod.close_folio(folio_name)

		# Kamra credit note carries the discounted 5% slab tax (half bill = 3,600 -> tax 171.43)
		cn_out = ledger.issue_credit_note(folio_name, 3600, "Discounted stay partial credit")
		cn = frappe.get_doc("Credit Note", cn_out["credit_note"])
		self.assertEqual((flt(cn.amount), flt(cn.tax_amount)), (3600.0, 171.43))

		# Crediting beyond the remaining discounted total (3,600) is refused
		with self.assertRaises(frappe.ValidationError):
			ledger.issue_credit_note(folio_name, 3601, "Over discounted invoice")

		# Bridge-invoiced discounted folio routes corrections to credit_and_rebill (never reopened locally)
		frappe.db.set_value("Folio", folio_name, "sales_invoice", "UT-SINV-DISC-1", update_modified=False)
		calls = []
		script = object()
		with patch("kamra.erp_bridge.get_bridge_script",
		           side_effect=lambda m: script if m == BRIDGE_FLOW else None), \
				patch("kamra.erp_bridge.run_bridge_script",
				      side_effect=lambda _s, form: (calls.append(form), {"routed": form["folio"]})[1]):
			res_cn = ledger.issue_credit_note(folio_name, 1000, "Bridge partial credit")
			res_cancel = api.cancel_invoice(folio_name, "Bridge full cancel")
		self.assertEqual((res_cn, res_cancel), ({"routed": folio_name}, {"routed": folio_name}))
		self.assertEqual([(c["amount"], c["reason"]) for c in calls],
		                 [(1000.0, "Bridge partial credit"), (None, "Bridge full cancel")])
		self.assertEqual(frappe.db.get_value("Folio", folio_name, "status"), "Closed")

		# Reinstating check-out on a bridge-invoiced discounted stay is refused
		frappe.db.set_value("Reservation", res.name, {
			"status": "Checked Out",
			"check_out_business_date": TODAY,
		})
		with self.assertRaisesRegex(frappe.ValidationError, "credit note and re-bill"):
			api.reinstate_checkout(res.name, "Should not reopen bridge bill")


class InHouseChannelGuardDiscountTests(RoundFourTestCase):
	"""Pin (#46 x #32): an in-house discounted OTA stay rejects OTA modify and
	cancel, preserves its discount and locked nights, exposes both in
	reservation_detail, and composes with discount updates and clear_channel_review."""

	def test_inhouse_discounted_stay_blocks_ota_modify_and_cancel_keeping_discount_intact(self):
		conn = frappe.get_doc({
			"doctype": "Channel Manager Connection",
			"property": PROP_A,
			"provider": "AioSell",
			"active": 1,
			"external_property_id": "ut-disc-ih",
		}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Channel Room Mapping",
			"connection": conn.name,
			"room_type": self.rt,
			"external_room_id": "std",
		}).insert(ignore_permissions=True)
		ref = "OTA-IH-" + frappe.generate_hash(length=6)
		res = make_reservation(
			PROP_A, self.rt, d(-1), d(2), status="Checked In", room=self.rooms[0],
			source="OTA", channel="Booking.com", ota_ref=ref, auto_price=1,
		)
		self.discount(res.name, discount_type="Percent", discount_value=10, **PROMO)
		folio_mod.post_room_night(self.doc(res.name), d(-1))
		before_rows = self.rows(res.name)
		before_total = flt(self.doc(res.name).amount_after_tax)

		# OTA modify on in-house discounted stay is held for review without touching rates or discount
		mod = cm._apply_event(conn, {
			"event": "modify", "ota_ref": ref, "booking_id": ref,
			"channel": "Booking.com", "room_type_external_id": "std",
			"check_in": d(-1), "check_out": d(3), "adults": 2, "children": 0,
			"total": 4000, "currency": "INR", "notes": "Guest asked OTA to extend",
		})
		self.assertEqual(mod["result"], "held_for_review")

		# OTA cancel on in-house discounted stay is also held for review
		can = cm._apply_event(conn, {
			"event": "cancel", "ota_ref": ref, "booking_id": ref, "channel": "Booking.com",
		})
		self.assertEqual(can["result"], "held_for_review")

		doc = self.doc(res.name)
		self.assertEqual(doc.status, "Checked In")
		self.assertEqual(int(doc.channel_review_needed), 1)
		self.assertEqual(doc.discount_type, "Percent")
		self.assertEqual(flt(doc.discount_value), 10.0)
		self.assertEqual(self.rows(res.name), before_rows)
		self.assertAlmostEqual(flt(doc.amount_after_tax), before_total, places=2)

		# reservation_detail exposes both the discount and the channel review alerts
		detail = self.as_user(DESK, api.reservation_detail, res.name)
		self.assertEqual(detail["channel_review_needed"], 1)
		self.assertGreaterEqual(len(detail["channel_reviews"]), 2)
		self.assertEqual(detail["discount"]["discount_type"], "Percent")
		self.assertEqual(detail["discount"]["discount_value"], 10.0)

		# Plain save cannot clear channel_review_needed
		tamper = self.doc(res.name)
		tamper.channel_review_needed = 0
		with self.assertRaises(frappe.PermissionError):
			tamper.save(ignore_permissions=True)

		# Updating future nights' discount while flagged succeeds and keeps channel_review_needed=1
		self.discount(res.name, discount_type="Percent", discount_value=15, **PROMO)
		doc = self.doc(res.name)
		self.assertEqual(int(doc.channel_review_needed), 1)
		self.assertEqual(self.rows(res.name), [(1000, 100, 900), (1000, 150, 850), (1000, 150, 850)])

		# Clearing channel review clears only the flag, leaving discount and locked nights intact
		self.as_user(DESK, cm.clear_channel_review, res.name)
		doc = self.doc(res.name)
		self.assertEqual(int(doc.channel_review_needed), 0)
		self.assertEqual(doc.discount_type, "Percent")
		self.assertEqual(flt(doc.discount_value), 15.0)
		self.assertEqual(self.rows(res.name), [(1000, 100, 900), (1000, 150, 850), (1000, 150, 850)])


PROP_UGX = "UT Disc UGX"


class PropertyCurrencyFormattingTests(RoundFourTestCase):
	"""P2-4: Changes Log (Version rows + discount audit) and folio discount
	descriptions use the property's currency and currency decimals (never INR
	or .00 on a whole-shilling UGX stay)."""

	properties = (PROP_A, PROP_UGX)

	def _ensure_ugx(self):
		if not frappe.db.exists("Currency", "UGX"):
			frappe.get_doc({
				"doctype": "Currency", "currency_name": "UGX",
				"symbol": "UGX ", "fraction": "", "fraction_units": 1,
				"number_format": "#,###.##", "enabled": 1,
			}).insert(ignore_permissions=True)
		else:
			frappe.db.set_value("Currency", "UGX", "enabled", 1)

	def test_ugx_changes_log_and_folio_descriptions_use_property_currency_and_whole_shillings(self):
		self._ensure_ugx()
		meta = frappe.get_meta("Reservation")
		for fieldname in ("amount_before_tax", "tax_amount", "amount_after_tax",
		                  "discount_amount", "agreed_rate"):
			self.assertEqual(
				meta.get_field(fieldname).options, "Property:property:currency", fieldname
			)

		make_property(
			PROP_UGX, business_date=TODAY, country="Uganda", currency="UGX",
			gst_mode="Fixed", gst_rate_low=0, gst_rate_high=0, rates_include_tax=1,
		)
		frappe.db.delete("Property Setting", {"property": PROP_UGX})
		set_setting(PROP_UGX, "rate_override_limit_pct", 25)
		rt = make_room_type(PROP_UGX, "UDLX", base_price=555000, tax_percent=0)
		frappe.db.set_value("Room Type", rt, {"base_price": 555000, "tax_percent": 0})
		room = make_room(PROP_UGX, rt, "U101")
		room_du = make_room(PROP_UGX, rt, "U102")

		out = self.as_user(
			DESK, api.create_booking, PROP_UGX, rt, d(5), d(6), "UGX Guest",
			room=room, discount_type="Percent", discount_value=10,
			discount_reason="RATE-PROMO",
		)
		res_name = out["reservation"]
		with patch.object(frappe, "in_test", False):
			self.as_user(
				DESK, api.set_reservation_discount, res_name,
				discount_type="Percent", discount_value=20, reason_code="RATE-PROMO",
			)

		entries = self.as_user(DESK, api.reservation_changes, res_name)["entries"]
		disc = next(e for e in entries if e.get("action_type") == "reservation_discount")
		self.assertIn("UGX", disc["rationale"])
		self.assertIn("555,000", disc["rationale"])
		self.assertIn("111,000", disc["rationale"])
		self.assertIn("444,000", disc["rationale"])
		self.assertNotIn(".00", disc["rationale"])
		self.assertNotIn("₹", disc["rationale"])

		amt_field = next(
			f for e in entries if e["kind"] == "change"
			for f in e["fields"] if f["field"] == "amount_after_tax"
		)
		for side in (amt_field["from"], amt_field["to"]):
			self.assertNotIn("₹", side)
			self.assertNotIn(".00", side)
			self.assertIn("UGX", side)
		self.assertIn("499,500", amt_field["from"])
		self.assertIn("444,000", amt_field["to"])

		# Folio Room line description uses whole shillings ("555,000 less discount 111,000")
		frappe.db.set_value("Reservation", res_name, "status", "Checked In")
		folio_mod.post_room_night(self.doc(res_name), d(5))
		room_line = next(
			c for c in frappe.get_doc("Folio", guest_folio(res_name)).charges
			if c.charge_type == "Room"
		)
		self.assertIn("555,000 less discount 111,000", room_line.description)
		self.assertNotIn("555,000.00", room_line.description)
		self.assertNotIn("111,000.00", room_line.description)

		# Day-use folio line description also uses whole shillings
		frappe.db.delete("Rate Plan", {"property": PROP_UGX, "code": "DUH"})
		du_plan = frappe.get_doc({
			"doctype": "Rate Plan", "property": PROP_UGX,
			"rate_plan_name": "UGX Hourly", "code": "DUH",
			"modifier_type": "Percent", "modifier_value": 0,
			"billing_basis": "Hourly", "hourly_rate": 50000, "min_hours": 2,
			"discount_eligible": 1,
		}).insert(ignore_permissions=True)
		du = make_reservation(
			PROP_UGX, rt, TODAY, TODAY, status="Checked In", room=room_du,
			rate_plan=du_plan.name, is_day_use=1, auto_price=1,
			planned_check_in_time="10:00:00", planned_check_out_time="12:00:00",
		)
		self.as_user(
			DESK, api.set_reservation_discount, du.name,
			discount_type="Percent", discount_value=10, reason_code="RATE-PROMO",
		)
		folio_mod.post_room_night(self.doc(du.name), TODAY)
		du_line = next(
			c for c in frappe.get_doc("Folio", guest_folio(du.name)).charges
			if c.charge_type == "Room"
		)
		self.assertIn("100,000 less discount 10,000", du_line.description)
		self.assertNotIn("100,000.00", du_line.description)
		self.assertNotIn("10,000.00", du_line.description)
