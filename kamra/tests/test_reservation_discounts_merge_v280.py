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
from kamra.tests.test_channel_webhook_query_args import guest_request
from kamra.tests.test_reservation_discounts import DESK, GM, REVENUE, set_setting
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

		# Channel review Agent Action Log uses the applied discount-removed reason (never #46's unapplied code)
		self.assertEqual(doc.get("channel_review_reason"), "discount_removed")
		review_logs = frappe.get_all(
			"Agent Action Log",
			filters={
				"reference_doctype": "Reservation", "reference_name": res.name,
				"action_type": "channel.modify_discount_removed",
			},
			fields=["name", "rationale"],
			order_by="creation desc",
		)
		self.assertTrue(review_logs, "Expected a channel.modify_discount_removed Agent Action Log entry")
		self.assertFalse(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": res.name,
			"action_type": "channel.modify_needs_review",
		}))
		for token in ("5%", "RATE-PROMO", "10%", "ADJ-RATE", "2,400"):
			self.assertIn(token, review_logs[0].rationale or "")
		self.assertNotRegex(review_logs[0].rationale or "", r"(?i)kamra|frappe|erpnext|ury")

		detail = self.as_user(DESK, api.reservation_detail, res.name)
		self.assertEqual(detail["channel_review_needed"], 1)
		self.assertEqual(detail["channel_review_reason"], "discount_removed")
		self.assertTrue(detail["warnings"]["channel_review_needed"])
		self.assertTrue(any(
			r.get("action_type") == "channel.modify_discount_removed"
			and r.get("reason") == "discount_removed"
			and "RATE-PROMO" in (r.get("rationale") or "")
			and "5%" in (r.get("rationale") or "")
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

		# Existing #46 clear_channel_review clears both the flag and its reason cleanly
		self.as_user(DESK, cm.clear_channel_review, res.name)
		cleared_doc = self.doc(res.name)
		self.assertEqual(int(cleared_doc.channel_review_needed), 0)
		self.assertFalse(cleared_doc.get("channel_review_reason"))

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
		self.assertEqual(doc.get("channel_review_reason"), "discount_removed")
		self.assertEqual(flt(doc.discount_amount), 0.0)
		self.assertEqual(flt(doc.amount_before_tax), 0.0)
		self.assertEqual(flt(doc.tax_amount), 0.0)
		self.assertEqual(flt(doc.amount_after_tax), 0.0)
		self.assertIn("10%", doc.special_requests or "")
		self.assertIn("RATE-PROMO", doc.special_requests or "")
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": res.name,
			"action_type": "reservation_discount", "action_channel": "API",
		}))
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": res.name,
			"action_type": "channel.modify_discount_removed",
		}))
		self.assertFalse(frappe.db.exists("Agent Action Log", {
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
		doc = self.doc(res.name)
		self.assertEqual(int(doc.channel_review_needed), 0)
		self.assertFalse(doc.get("channel_review_reason"))
		self.assertFalse(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": res.name,
			"action_type": "reservation_discount",
		}))
		self.assertFalse(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": res.name,
			"action_type": ("in", ["channel.modify_needs_review", "channel.modify_discount_removed",
			                       "channel.modify_discount_review"]),
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

	def _ota_conn_with_secret(self, secret: str = "r8-webhook-secret"):
		conn = frappe.get_doc({
			"doctype": "Channel Manager Connection",
			"property": PROP_A,
			"provider": "AioSell",
			"active": 1,
			"external_property_id": "ut-disc-r8",
			"webhook_secret": secret,
		}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Channel Room Mapping",
			"connection": conn.name,
			"room_type": self.rt,
			"external_room_id": "std",
		}).insert(ignore_permissions=True)
		return conn, secret

	def _post_guest_webhook(self, conn_name: str, secret: str, payload: dict):
		import frappe.handler
		with guest_request({"connection": conn_name}, payload, secret=secret):
			self.assertEqual(frappe.session.user, "Guest")
			out = frappe.handler.execute_cmd("kamra.channel_manager.webhook")
			# Webhook must never escalate privilege away from Guest
			self.assertEqual(frappe.session.user, "Guest")
			return out

	def test_r8_pre_arrival_ota_modify_recomputes_header_totals_and_clears_stale_discount_amount(self):
		"""R8 P1 #2: clearing a desk discount on an OTA modify recomputes
		discount_amount, amount_before_tax, tax_amount, amount_after_tax and night rates."""
		conn = self._ota_conn()
		ref = "OTA-R8-HDR-" + frappe.generate_hash(length=6)
		res = self._pre_arrival_ota(ref)
		self.discount(res.name, discount_type="Percent", discount_value=10, **PROMO)
		before = self.doc(res.name)
		self.assertGreater(flt(before.discount_amount), 0.0)
		self.assertTrue(before.quote_snapshot)

		out = cm._apply_event(conn, {
			"event": "modify", "ota_ref": ref, "booking_id": ref,
			"channel": "Booking.com", "room_type_external_id": "std",
			"check_in": d(5), "check_out": d(7), "adults": 2, "children": 0,
			"total": 2400, "currency": "INR",
			"nightly": [{"date": d(5), "amount": 1200}, {"date": d(6), "amount": 1200}],
			"notes": "Channel repriced stay",
		})
		self.assertEqual(out["result"], "modified")
		doc = self.doc(res.name)
		self.assertEqual(flt(doc.discount_amount), 0.0)
		self.assertEqual(flt(doc.amount_after_tax), 2400.0)
		nights_before_tax = round(sum(flt(r.rate) for r in doc.night_rates), 2)
		self.assertAlmostEqual(flt(doc.amount_before_tax), nights_before_tax, places=2)
		self.assertAlmostEqual(
			flt(doc.amount_before_tax) + flt(doc.tax_amount),
			flt(doc.amount_after_tax),
			places=2,
		)
		self.assertFalse(doc.quote_snapshot)
		for row in doc.night_rates:
			self.assertEqual(flt(row.discount_amount), 0.0)
			self.assertFalse(row.get("discount_type"))
			self.assertAlmostEqual(flt(row.base_rate), flt(row.rate), places=2)
			self.assertGreater(flt(row.rate_code_amount), 0.0)

		# Also verify non-zero tax (18% GST) recomputes amount_before_tax and tax_amount cleanly
		rt_tax = make_room_type(PROP_A, "R8TX", base_price=1000, tax_percent=18)
		frappe.db.set_value("Room Type", rt_tax, {"base_price": 1000, "tax_percent": 18})
		room_tax = make_room(PROP_A, rt_tax, "T101")
		frappe.get_doc({
			"doctype": "Channel Room Mapping",
			"connection": conn.name,
			"room_type": rt_tax,
			"external_room_id": "r8tx",
		}).insert(ignore_permissions=True)
		ref_tax = "OTA-R8-TAX-" + frappe.generate_hash(length=6)
		res_tax = make_reservation(
			PROP_A, rt_tax, d(5), d(7), status="Confirmed", room=room_tax,
			source="OTA", channel="Booking.com", ota_ref=ref_tax, auto_price=1,
		)
		self.discount(res_tax.name, discount_type="Percent", discount_value=10, **PROMO)
		before_tax = self.doc(res_tax.name)
		self.assertGreater(flt(before_tax.discount_amount), 0.0)
		self.assertGreater(flt(before_tax.tax_amount), 0.0)
		cm._apply_event(conn, {
			"event": "modify", "ota_ref": ref_tax, "booking_id": ref_tax,
			"channel": "Booking.com", "room_type_external_id": "r8tx",
			"check_in": d(5), "check_out": d(7), "adults": 2, "children": 0,
			"total": 2360, "currency": "INR",
			"nightly": [{"date": d(5), "amount": 1180}, {"date": d(6), "amount": 1180}],
		})
		doc_tax = self.doc(res_tax.name)
		self.assertEqual(flt(doc_tax.discount_amount), 0.0)
		self.assertAlmostEqual(flt(doc_tax.amount_before_tax), 2000.0, places=2)
		self.assertAlmostEqual(flt(doc_tax.tax_amount), 360.0, places=2)
		self.assertAlmostEqual(flt(doc_tax.amount_after_tax), 2360.0, places=2)
		self.assertFalse(doc_tax.quote_snapshot)

	def test_r8_real_webhook_as_guest_extends_over_limit_stay_without_crashing_or_escalating(self):
		"""R8 P1 #3: real webhook (Guest) extending a supervisor-approved >limit stay
		without a channel total keeps the approved discount only on the originally
		approved nights, prices added nights without the unapproved discount, flags
		discount_review, and never escalates privilege from Guest."""
		conn, secret = self._ota_conn_with_secret()
		ref = "OTA-R8-25-" + frappe.generate_hash(length=6)
		res = self._pre_arrival_ota(ref)
		# Supervisor approves 25% (> 15% front desk limit) on nights d(5), d(6)
		self.discount(
			res.name, discount_type="Percent", discount_value=25, **PROMO,
			supervisor_user=GM, supervisor_pin="4321",
		)
		self.assertEqual(self.rows(res.name), [(1000, 250, 750), (1000, 250, 750)])

		payload = {
			"action": "modify", "hotelCode": "ut-disc-r8", "channel": "Booking.com",
			"bookingId": ref, "checkin": d(5), "checkout": d(8),
			"guest": {"firstName": "OTA", "lastName": "Extend"},
			"amount": {"currency": "INR"},
			"specialRequests": "Extending by one night",
			"rooms": [{"roomCode": "std", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
		}
		out = self._post_guest_webhook(conn.name, secret, payload)
		self.assertTrue(out["ok"])
		self.assertEqual(out["results"][0]["result"], "modified")

		doc = self.doc(res.name)
		self.assertEqual(str(doc.check_in_date), d(5))
		self.assertEqual(str(doc.check_out_date), d(8))
		# Original approved nights keep 25% off (750); added night d(7) gets full rate (1000)
		self.assertEqual(self.rows(res.name), [(1000, 250, 750), (1000, 250, 750), (1000, 0, 1000)])
		self.assertFalse(doc.get("discount_type"))
		self.assertEqual(flt(doc.get("discount_value")), 0.0)
		self.assertAlmostEqual(flt(doc.discount_amount), 500.0, places=2)
		self.assertAlmostEqual(flt(doc.amount_before_tax), 2500.0, places=2)
		self.assertAlmostEqual(
			flt(doc.amount_before_tax) + flt(doc.tax_amount),
			flt(doc.amount_after_tax),
			places=2,
		)
		self.assertEqual(int(doc.channel_review_needed), 1)
		self.assertEqual(doc.get("channel_review_reason"), "discount_review")
		self.assertIn(d(7), doc.special_requests or "")
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": res.name,
			"action_type": "channel.modify_discount_review", "action_channel": "API",
		}))
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": res.name,
			"action_type": "reservation_discount", "action_channel": "API",
		}))
		detail = self.as_user(DESK, api.reservation_detail, res.name)
		self.assertEqual(detail["channel_review_needed"], 1)
		self.assertEqual(detail["channel_review_reason"], "discount_review")
		self.assertEqual(detail["channel_reviews"][0]["action_type"], "channel.modify_discount_review")
		self.assertEqual(detail["channel_reviews"][0]["reason"], "discount_review")
		self.assertNotRegex(detail["channel_reviews"][0]["rationale"] or "", r"(?i)kamra|frappe|erpnext|ury")

	def test_r8_real_webhook_as_guest_extends_full_comp_stay_without_crashing_or_comping_new_night(self):
		"""R8 P1 #3: real webhook (Guest) extending a 100% COMP stay keeps comp on
		the originally approved nights only, prices the added night at full rate,
		and flags discount_review without crashing."""
		conn, secret = self._ota_conn_with_secret()
		ref = "OTA-R8-COMP-" + frappe.generate_hash(length=6)
		res = self._pre_arrival_ota(ref)
		self.discount(
			res.name, discount_type="Percent", discount_value=100, **PROMO,
			supervisor_user=GM, supervisor_pin="4321",
		)
		self.assertEqual(self.rows(res.name), [(1000, 1000, 0), (1000, 1000, 0)])

		payload = {
			"action": "modify", "hotelCode": "ut-disc-r8", "channel": "Booking.com",
			"bookingId": ref, "checkin": d(5), "checkout": d(8),
			"guest": {"firstName": "OTA", "lastName": "Comp"},
			"amount": {"currency": "INR"},
			"rooms": [{"roomCode": "std", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
		}
		out = self._post_guest_webhook(conn.name, secret, payload)
		self.assertTrue(out["ok"])
		self.assertEqual(out["results"][0]["result"], "modified")

		doc = self.doc(res.name)
		self.assertEqual(str(doc.check_out_date), d(8))
		self.assertEqual(self.rows(res.name), [(1000, 1000, 0), (1000, 1000, 0), (1000, 0, 1000)])
		self.assertAlmostEqual(flt(doc.discount_amount), 2000.0, places=2)
		self.assertAlmostEqual(flt(doc.amount_before_tax), 1000.0, places=2)
		self.assertEqual(int(doc.channel_review_needed), 1)
		self.assertEqual(doc.get("channel_review_reason"), "discount_review")

	def test_r8_real_webhook_as_guest_shifts_over_limit_stay_and_extends_within_limit_cleanly(self):
		"""R8 P1 #3: shifting an over-limit stay completely off its approved nights
		clears the unapproved discount and flags discount_removed; extending a
		within-limit stay or shortening an over-limit stay succeeds cleanly as Guest."""
		conn, secret = self._ota_conn_with_secret()

		# (a) Full shift off approved nights on 25% stay -> all nights rack, discount_removed
		ref_shift = "OTA-R8-SHF-" + frappe.generate_hash(length=6)
		res_shift = self._pre_arrival_ota(ref_shift)
		self.discount(
			res_shift.name, discount_type="Percent", discount_value=25, **PROMO,
			supervisor_user=GM, supervisor_pin="4321",
		)
		out = self._post_guest_webhook(conn.name, secret, {
			"action": "modify", "hotelCode": "ut-disc-r8", "channel": "Booking.com",
			"bookingId": ref_shift, "checkin": d(10), "checkout": d(12),
			"amount": {"currency": "INR"},
			"rooms": [{"roomCode": "std", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
		})
		self.assertTrue(out["ok"])
		doc_shift = self.doc(res_shift.name)
		self.assertEqual((str(doc_shift.check_in_date), str(doc_shift.check_out_date)), (d(10), d(12)))
		self.assertEqual(self.rows(res_shift.name), [(1000, 0, 1000), (1000, 0, 1000)])
		self.assertEqual(flt(doc_shift.discount_amount), 0.0)
		self.assertEqual(int(doc_shift.channel_review_needed), 1)
		self.assertEqual(doc_shift.get("channel_review_reason"), "discount_removed")

		# (b) Within-limit (10% <= 15%) stay extended as Guest -> keeps 10% on all 3 nights, no review
		ref_ok = "OTA-R8-OK-" + frappe.generate_hash(length=6)
		res_ok = make_reservation(
			PROP_A, self.rt, d(5), d(7), status="Confirmed", room=self.rooms[1],
			source="OTA", channel="Booking.com", ota_ref=ref_ok, auto_price=1,
		)
		self.discount(res_ok.name, discount_type="Percent", discount_value=10, **PROMO)
		out_ok = self._post_guest_webhook(conn.name, secret, {
			"action": "modify", "hotelCode": "ut-disc-r8", "channel": "Booking.com",
			"bookingId": ref_ok, "checkin": d(5), "checkout": d(8),
			"amount": {"currency": "INR"},
			"rooms": [{"roomCode": "std", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
		})
		self.assertTrue(out_ok["ok"])
		doc_ok = self.doc(res_ok.name)
		self.assertEqual(self.rows(res_ok.name), [(1000, 100, 900)] * 3)
		self.assertEqual(int(doc_ok.channel_review_needed), 0)

	def test_r8_two_ota_modifies_in_a_row_preserve_first_review_note(self):
		"""R8 P2 #4: a second (and third) pre-arrival OTA modify with new or empty
		notes must keep the first modify's superseded-discount review note in
		special_requests without duplicating it."""
		conn = self._ota_conn()
		ref = "OTA-R8-2M-" + frappe.generate_hash(length=6)
		res = self._pre_arrival_ota(ref)
		self.discount(res.name, discount_type="Percent", discount_value=10, **PROMO)

		# Modify 1 supersedes the 10% desk discount and records the review note
		cm._apply_event(conn, {
			"event": "modify", "ota_ref": ref, "booking_id": ref,
			"channel": "Booking.com", "room_type_external_id": "std",
			"check_in": d(5), "check_out": d(7), "adults": 2, "children": 0,
			"total": 2400, "currency": "INR",
			"nightly": [{"date": d(5), "amount": 1200}, {"date": d(6), "amount": 1200}],
			"notes": "First guest note",
		})
		after1 = self.doc(res.name)
		self.assertIn("10% off (RATE-PROMO)", after1.special_requests or "")

		# Modify 2 arrives with different guest notes -> MUST keep Modify 1's review note
		cm._apply_event(conn, {
			"event": "modify", "ota_ref": ref, "booking_id": ref,
			"channel": "Booking.com", "room_type_external_id": "std",
			"check_in": d(5), "check_out": d(7), "adults": 2, "children": 0,
			"total": 2600, "currency": "INR",
			"nightly": [{"date": d(5), "amount": 1300}, {"date": d(6), "amount": 1300}],
			"notes": "High floor quiet room",
		})
		after2 = self.doc(res.name)
		self.assertEqual(int(after2.channel_review_needed), 1)
		self.assertEqual(after2.get("channel_review_reason"), "discount_removed")
		self.assertIn("10% off (RATE-PROMO)", after2.special_requests or "")
		self.assertIn("High floor quiet room", after2.special_requests or "")

		# Modify 3 arrives with empty notes -> STILL keeps Modify 1's review note once
		cm._apply_event(conn, {
			"event": "modify", "ota_ref": ref, "booking_id": ref,
			"channel": "Booking.com", "room_type_external_id": "std",
			"check_in": d(5), "check_out": d(7), "adults": 2, "children": 0,
			"total": 2600, "currency": "INR",
			"nightly": [{"date": d(5), "amount": 1300}, {"date": d(6), "amount": 1300}],
			"notes": "",
		})
		after3 = self.doc(res.name)
		self.assertIn("10% off (RATE-PROMO)", after3.special_requests or "")
		self.assertEqual((after3.special_requests or "").count("10% off (RATE-PROMO)"), 1)

	def test_r8_followup_tax_hold_releases_to_confirmed_after_desk_discount_removed(self):
		"""Follow-up P2-1: a superseded-discount review note in special_requests
		must not be mistaken for a non-tax hold, so a corrected OTA modify with
		readable taxes releases a Requested stay back to Confirmed while still
		preserving the review note and flag. A genuine non-tax hold (currency
		mismatch) must still hold as Requested."""
		conn = self._ota_conn()
		ref = "OTA-FUP1-" + frappe.generate_hash(length=6)
		res = self._pre_arrival_ota(ref)
		self.discount(res.name, discount_type="Percent", discount_value=10, **PROMO)

		# Modify 1: unreadable taxes -> held as Requested, desk discount removed
		o1 = cm._apply_event(conn, {
			"event": "modify", "ota_ref": ref, "booking_id": ref,
			"channel": "Booking.com", "room_type_external_id": "std",
			"check_in": d(5), "check_out": d(7), "adults": 2, "children": 0,
			"total": 2400, "currency": "INR", "tax_rules": False,
			"notes": "Initial OTA modify",
		})
		self.assertEqual(o1["result"], "modified")
		d1 = self.doc(res.name)
		self.assertEqual(d1.status, "Requested")
		self.assertEqual(int(d1.channel_review_needed), 1)
		self.assertEqual(d1.get("channel_review_reason"), "discount_removed")
		self.assertEqual(flt(d1.amount_after_tax), 0.0)
		self.assertIn("10% off (RATE-PROMO)", d1.special_requests or "")

		# Modify 2: corrected OTA modify with readable taxes -> releases to Confirmed
		o2 = cm._apply_event(conn, {
			"event": "modify", "ota_ref": ref, "booking_id": ref,
			"channel": "Booking.com", "room_type_external_id": "std",
			"check_in": d(5), "check_out": d(7), "adults": 2, "children": 0,
			"total": 2400, "currency": "INR",
			"nightly": [{"date": d(5), "amount": 1200}, {"date": d(6), "amount": 1200}],
			"notes": "Corrected OTA modify",
		})
		self.assertEqual(o2["result"], "modified")
		d2 = self.doc(res.name)
		self.assertEqual(d2.status, "Confirmed", "tax hold must release once OTA taxes are readable")
		self.assertEqual(flt(d2.amount_after_tax), 2400.0)
		self.assertAlmostEqual(flt(d2.amount_before_tax) + flt(d2.tax_amount), 2400.0, places=2)
		self.assertEqual(flt(d2.discount_amount), 0.0)
		self.assertEqual(len(d2.night_rates), 2)
		self.assertEqual(int(d2.channel_review_needed), 1)
		self.assertEqual(d2.get("channel_review_reason"), "discount_removed")
		self.assertIn("10% off (RATE-PROMO)", d2.special_requests or "")
		self.assertEqual((d2.special_requests or "").count("10% off (RATE-PROMO)"), 1)
		self.assertIn("Corrected OTA modify", d2.special_requests or "")
		self.assertNotIn("OTA tax rules missing", d2.special_requests or "")
		self.assertNotIn("new total", (d2.special_requests or "").lower())

		# Control: genuine non-tax hold (currency mismatch) with a superseded desk
		# discount stays Requested and keeps both the hold note and review note
		ref_fx = "OTA-FUPFX-" + frappe.generate_hash(length=6)
		res_fx = make_reservation(
			PROP_A, self.rt, d(8), d(10), status="Confirmed", room=self.rooms[1],
			source="OTA", channel="Booking.com", ota_ref=ref_fx, auto_price=1,
		)
		self.discount(res_fx.name, discount_type="Percent", discount_value=10, **PROMO)
		cm._apply_event(conn, {
			"event": "modify", "ota_ref": ref_fx, "booking_id": ref_fx,
			"channel": "Booking.com", "room_type_external_id": "std",
			"check_in": d(8), "check_out": d(10), "adults": 2, "children": 0,
			"total": 200, "currency": "USD", "notes": "Foreign currency",
		})
		cm._apply_event(conn, {
			"event": "modify", "ota_ref": ref_fx, "booking_id": ref_fx,
			"channel": "Booking.com", "room_type_external_id": "std",
			"check_in": d(8), "check_out": d(10), "adults": 2, "children": 0,
			"total": 2400, "currency": "INR", "notes": "Follow-up note",
		})
		dfx = self.doc(res_fx.name)
		self.assertEqual(dfx.status, "Requested")
		self.assertIn("OTA currency mismatch — resolve", dfx.special_requests or "")
		self.assertIn("10% off (RATE-PROMO)", dfx.special_requests or "")

	def test_r8_followup_second_no_total_modify_adding_night_logs_audit_and_avoids_stale_total(self):
		"""Follow-up P2-2: a second no-total OTA modify as Guest that adds another
		night to a stay holding night-level discounts must write a fresh audit row,
		re-raise channel_review_needed (even if cleared after modify 1), and never
		leave a stale 'new total' in special_requests; replaying the same modify
		remains idempotent."""
		from kamra.localization import money_text

		conn, secret = self._ota_conn_with_secret()
		ref = "OTA-FUP2-" + frappe.generate_hash(length=6)
		res = self._pre_arrival_ota(ref)
		self.discount(
			res.name, discount_type="Percent", discount_value=25, **PROMO,
			supervisor_user=GM, supervisor_pin="4321",
		)
		self.assertEqual(self.rows(res.name), [(1000, 250, 750), (1000, 250, 750)])

		# Modify 1 (Guest, no total): extend d(5)..d(8), adding d(7) at rack
		out1 = self._post_guest_webhook(conn.name, secret, {
			"action": "modify", "hotelCode": "ut-disc-r8", "channel": "Booking.com",
			"bookingId": ref, "checkin": d(5), "checkout": d(8),
			"amount": {"currency": "INR"},
			"specialRequests": "First extension",
			"rooms": [{"roomCode": "std", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
		})
		self.assertTrue(out1["ok"])
		after1 = self.doc(res.name)
		self.assertEqual(self.rows(res.name), [(1000, 250, 750), (1000, 250, 750), (1000, 0, 1000)])
		self.assertEqual(int(after1.channel_review_needed), 1)
		self.assertEqual(after1.get("channel_review_reason"), "discount_review")
		total1_text = money_text(PROP_A, after1.amount_after_tax)

		# Staff marks modify 1 as reviewed before modify 2 arrives
		self.as_user(DESK, cm.clear_channel_review, res.name)
		self.assertEqual(int(self.doc(res.name).channel_review_needed), 0)

		# Modify 2 (Guest, no total): extend again d(5)..d(9), adding d(8) at rack
		out2 = self._post_guest_webhook(conn.name, secret, {
			"action": "modify", "hotelCode": "ut-disc-r8", "channel": "Booking.com",
			"bookingId": ref, "checkin": d(5), "checkout": d(9),
			"amount": {"currency": "INR"},
			"specialRequests": "Second extension",
			"rooms": [{"roomCode": "std", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
		})
		self.assertTrue(out2["ok"])
		after2 = self.doc(res.name)
		self.assertEqual(
			self.rows(res.name),
			[(1000, 250, 750), (1000, 250, 750), (1000, 0, 1000), (1000, 0, 1000)],
		)
		self.assertAlmostEqual(flt(after2.amount_before_tax), 3500.0, places=2)
		self.assertAlmostEqual(
			flt(after2.amount_before_tax) + flt(after2.tax_amount),
			flt(after2.amount_after_tax),
			places=2,
		)
		self.assertEqual(int(after2.channel_review_needed), 1)
		self.assertEqual(after2.get("channel_review_reason"), "discount_review")

		total2_text = money_text(PROP_A, after2.amount_after_tax)
		self.assertNotEqual(total1_text, total2_text)
		self.assertIn(d(7), after2.special_requests or "")
		self.assertIn(d(8), after2.special_requests or "")
		self.assertIn("Second extension", after2.special_requests or "")
		self.assertNotIn("new total", (after2.special_requests or "").lower())
		self.assertNotIn(total1_text, after2.special_requests or "")

		# Must write a 2nd channel.modify_discount_review AND 2nd reservation_discount API log
		review_logs = frappe.get_all(
			"Agent Action Log",
			filters={
				"reference_doctype": "Reservation", "reference_name": res.name,
				"action_type": "channel.modify_discount_review", "action_channel": "API",
			},
			fields=["name", "rationale"],
			order_by="creation desc",
		)
		self.assertEqual(len(review_logs), 2, f"Expected 2 review audit rows, got {review_logs}")
		self.assertIn(d(8), review_logs[0].rationale or "")
		self.assertIn(total2_text, review_logs[0].rationale or "")
		self.assertNotRegex(review_logs[0].rationale or "", r"(?i)kamra|frappe|erpnext|ury")

		disc_logs = frappe.get_all(
			"Agent Action Log",
			filters={
				"reference_doctype": "Reservation", "reference_name": res.name,
				"action_type": "reservation_discount", "action_channel": "API",
			},
			fields=["name", "rationale", "after_snapshot"],
			order_by="creation desc",
		)
		self.assertEqual(len(disc_logs), 2, f"Expected 2 reservation_discount API rows, got {disc_logs}")
		snap2 = json.loads(disc_logs[0].after_snapshot)
		self.assertAlmostEqual(flt(snap2["amount_after_tax"]), flt(after2.amount_after_tax), places=2)

		detail = self.as_user(DESK, api.reservation_detail, res.name)
		self.assertEqual(detail["channel_review_needed"], 1)
		self.assertEqual(detail["channel_review_reason"], "discount_review")
		# Modify 1 was cleared via clear_channel_review, so the active banner lists
		# only the unreviewed Modify 2 alert while Agent Action Log keeps both rows
		self.assertEqual(len(detail["channel_reviews"]), 1)
		self.assertIn(d(8), detail["channel_reviews"][0]["rationale"] or "")
		self.assertIn(total2_text, detail["channel_reviews"][0]["rationale"] or "")

		# Replaying the exact Modify 2 payload must stay idempotent (no 3rd audit row)
		out_replay = self._post_guest_webhook(conn.name, secret, {
			"action": "modify", "hotelCode": "ut-disc-r8", "channel": "Booking.com",
			"bookingId": ref, "checkin": d(5), "checkout": d(9),
			"amount": {"currency": "INR"},
			"specialRequests": "Second extension",
			"rooms": [{"roomCode": "std", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
		})
		self.assertTrue(out_replay["ok"])
		self.assertEqual(
			frappe.db.count("Agent Action Log", {
				"reference_doctype": "Reservation", "reference_name": res.name,
				"action_type": "channel.modify_discount_review", "action_channel": "API",
			}),
			2,
		)


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

		# reservation_detail exposes both the discount and the #46 unapplied review alerts
		self.assertEqual(doc.get("channel_review_reason"), "update_not_applied")
		detail = self.as_user(DESK, api.reservation_detail, res.name)
		self.assertEqual(detail["channel_review_needed"], 1)
		self.assertEqual(detail["channel_review_reason"], "update_not_applied")
		self.assertGreaterEqual(len(detail["channel_reviews"]), 2)
		self.assertEqual(detail["channel_reviews"][0]["reason"], "update_not_applied")
		self.assertEqual(detail["discount"]["discount_type"], "Percent")
		self.assertEqual(detail["discount"]["discount_value"], 10.0)

		# Plain save cannot clear channel_review_needed or tamper with channel_review_reason
		tamper = self.doc(res.name)
		tamper.channel_review_needed = 0
		with self.assertRaises(frappe.PermissionError):
			tamper.save(ignore_permissions=True)
		tamper_reason = self.doc(res.name)
		tamper_reason.channel_review_reason = "discount_removed"
		with self.assertRaises(frappe.PermissionError):
			tamper_reason.save(ignore_permissions=True)

		# Updating future nights' discount while flagged succeeds and keeps channel_review_needed=1
		self.discount(res.name, discount_type="Percent", discount_value=15, **PROMO)
		doc = self.doc(res.name)
		self.assertEqual(int(doc.channel_review_needed), 1)
		self.assertEqual(doc.get("channel_review_reason"), "update_not_applied")
		self.assertEqual(self.rows(res.name), [(1000, 100, 900), (1000, 150, 850), (1000, 150, 850)])

		# Clearing channel review clears the flag and reason, leaving discount and locked nights intact
		self.as_user(DESK, cm.clear_channel_review, res.name)
		doc = self.doc(res.name)
		self.assertEqual(int(doc.channel_review_needed), 0)
		self.assertFalse(doc.get("channel_review_reason"))
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


PROP_UGX_VAT = "UT Disc UGX VAT18"
PROP_USD_VAT = "UT Disc USD VAT18"
PROP_USD_VAT10 = "UT Disc USD VAT10"
PROP_KWD_VAT = "UT Disc KWD VAT18"


class TaxInclusiveOtaExtendDiscountTests(RoundFourTestCase):
	"""Regression tests for tax-inclusive OTA modify discount reconciliation:
	- 18% VAT-inclusive UGX (prec=0), USD (prec=2), KWD (prec=3) stays preserve
	  supervisor-approved >limit (25%) and 100% COMP discounts on surviving
	  nights when an auto-priced OTA modify extends the stay (ignoring
	  last-night inclusive-tax net-rounding remainder redistribution).
	- Genuine rate changes (season/card price hike, room category change,
	  party change, rate code change) still drop/review unapproved discounts
	  with zero security weakening and cleanly separate repriced surviving
	  nights from genuinely added nights in review text."""

	properties = (PROP_A, PROP_UGX_VAT, PROP_USD_VAT, PROP_USD_VAT10, PROP_KWD_VAT)

	def _ensure_currency(self, code: str, *, symbol: str, fraction_units: int, number_format: str):
		if not frappe.db.exists("Currency", code):
			frappe.get_doc({
				"doctype": "Currency",
				"currency_name": code,
				"symbol": symbol,
				"fraction": "",
				"fraction_units": fraction_units,
				"number_format": number_format,
				"enabled": 1,
			}).insert(ignore_permissions=True)
		else:
			frappe.db.set_value("Currency", code, {
				"enabled": 1,
				"symbol": symbol,
				"fraction_units": fraction_units,
				"number_format": number_format,
			})

	def _setup_vat_property(
		self,
		prop: str,
		*,
		country: str,
		currency: str,
		tax_pct: float,
		base_price: float,
		rt_code: str = "DLX",
		extra_adult_price: float = 0.0,
		secret: str = "vat-ota-secret",
	):
		make_property(
			prop,
			business_date=TODAY,
			country=country,
			currency=currency,
			gst_mode="Fixed",
			gst_rate_low=tax_pct,
			gst_rate_high=tax_pct,
			rates_include_tax=1,
		)
		frappe.db.delete("Property Setting", {"property": prop})
		set_setting(prop, "rate_override_limit_pct", 15)
		rt = make_room_type(prop, rt_code, base_price=base_price, tax_percent=tax_pct)
		frappe.db.set_value("Room Type", rt, {
			"base_price": base_price,
			"tax_percent": tax_pct,
			"adults_capacity": 3,
			"base_occupancy": 2,
			"extra_adult_price": extra_adult_price,
		})
		frappe.clear_document_cache("Room Type", rt)
		rooms = [make_room(prop, rt, f"{rt_code}{i}") for i in (1, 2, 3, 4)]
		conn = frappe.get_doc({
			"doctype": "Channel Manager Connection",
			"property": prop,
			"provider": "AioSell",
			"active": 1,
			"external_property_id": f"ut-vat-{rt_code.lower()}",
			"webhook_secret": secret,
		}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Channel Room Mapping",
			"connection": conn.name,
			"room_type": rt,
			"external_room_id": "dlx",
		}).insert(ignore_permissions=True)
		return rt, rooms, conn, secret

	def _post_guest_webhook(self, conn_name: str, secret: str, payload: dict):
		import frappe.handler
		with guest_request({"connection": conn_name}, payload, secret=secret):
			self.assertEqual(frappe.session.user, "Guest")
			out = frappe.handler.execute_cmd("kamra.channel_manager.webhook")
			self.assertEqual(frappe.session.user, "Guest")
			return out

	def _assert_no_vendor_leaks(self, text: str):
		self.assertNotRegex(text or "", r"(?i)\b(?:kamra|frappe|erpnext|ury|huf|tridz)\b")

	def test_ugx_18pct_vat_inclusive_25pct_extend_preserves_surviving_nights_and_only_flags_added_night(self):
		"""Hydrogen R1 P0: 18% VAT-inclusive UGX 500,000/night stay with 25% off
		extended 2n -> 3n (and 3n -> 4n) via Guest OTA webhook keeps 25% on Nights
		1 & 2 (no +UGX 125,000 overcharge) and only flags genuinely added nights."""
		self._ensure_currency("UGX", symbol="UGX ", fraction_units=1, number_format="#,###")
		rt, rooms, conn, secret = self._setup_vat_property(
			PROP_UGX_VAT, country="Uganda", currency="UGX", tax_pct=18, base_price=500000, rt_code="UG25",
		)
		ref = "OTA-UGX25-" + frappe.generate_hash(length=6)
		res = make_reservation(
			PROP_UGX_VAT, rt, d(5), d(7), status="Confirmed", room=rooms[0],
			source="OTA", channel="Booking.com", ota_ref=ref, auto_price=1,
		)
		# Verify precondition: initial 2-night inclusive booking puts +0.01 remainder on Night 2
		init_doc = self.doc(res.name)
		self.assertAlmostEqual(flt(init_doc.night_rates[0].base_rate), 423728.81, places=2)
		self.assertAlmostEqual(flt(init_doc.night_rates[1].base_rate), 423728.82, places=2)

		self.discount(
			res.name, discount_type="Percent", discount_value=25, **PROMO,
			supervisor_user=GM, supervisor_pin="4321",
		)

		# Modify 1 (Guest, no total): extend 2n -> 3n (d5..d8, adding d7)
		out1 = self._post_guest_webhook(conn.name, secret, {
			"action": "modify", "hotelCode": "ut-vat-ug25", "channel": "Booking.com",
			"bookingId": ref, "checkin": d(5), "checkout": d(8),
			"amount": {"currency": "UGX"},
			"specialRequests": "First VAT extension",
			"rooms": [{"roomCode": "dlx", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
		})
		self.assertTrue(out1["ok"])
		self.assertEqual(out1["results"][0]["result"], "modified")

		doc1 = self.doc(res.name)
		self.assertEqual(len(doc1.night_rates), 3)
		self.assertEqual(
			[round(flt(r.discount_amount), 2) for r in doc1.night_rates],
			[125000.0, 125000.0, 0.0],
		)
		self.assertEqual(
			[round(flt(r.rate), 2) for r in doc1.night_rates],
			[317796.61, 317796.61, 423728.81],
		)
		self.assertAlmostEqual(flt(doc1.amount_after_tax), 1250000.0, places=2)
		self.assertAlmostEqual(flt(doc1.discount_amount), 250000.0, places=2)
		self.assertEqual(int(doc1.channel_review_needed), 1)
		self.assertEqual(doc1.get("channel_review_reason"), "discount_review")
		sr1 = doc1.special_requests or ""
		self.assertIn(f"kept approved desk discount on {d(5)}, {d(6)}", sr1)
		self.assertIn(f"added night(s) {d(7)} priced without unapproved discount", sr1)
		self.assertNotIn(f"added night(s) {d(6)}", sr1)
		self.assertNotIn("new total", sr1.lower())
		self._assert_no_vendor_leaks(sr1)

		logs1 = frappe.get_all(
			"Agent Action Log",
			filters={
				"reference_doctype": "Reservation", "reference_name": res.name,
				"action_type": "channel.modify_discount_review", "action_channel": "API",
			},
			fields=["name", "rationale"],
			order_by="creation desc",
		)
		self.assertEqual(len(logs1), 1)
		self.assertIn(f"added night(s) {d(7)}", logs1[0].rationale or "")
		self.assertNotIn(f"added night(s) {d(6)}", logs1[0].rationale or "")
		self.assertIn("1,250,000", logs1[0].rationale or "")
		self._assert_no_vendor_leaks(logs1[0].rationale or "")

		# Clear review then Modify 2 (Guest, no total): extend 3n -> 4n (d5..d9, adding d8)
		self.as_user(DESK, cm.clear_channel_review, res.name)
		out2 = self._post_guest_webhook(conn.name, secret, {
			"action": "modify", "hotelCode": "ut-vat-ug25", "channel": "Booking.com",
			"bookingId": ref, "checkin": d(5), "checkout": d(9),
			"amount": {"currency": "UGX"},
			"specialRequests": "Second VAT extension",
			"rooms": [{"roomCode": "dlx", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
		})
		self.assertTrue(out2["ok"])
		doc2 = self.doc(res.name)
		self.assertEqual(
			[round(flt(r.discount_amount), 2) for r in doc2.night_rates],
			[125000.0, 125000.0, 0.0, 0.0],
		)
		self.assertAlmostEqual(flt(doc2.amount_after_tax), 1750000.0, places=2)
		self.assertEqual(int(doc2.channel_review_needed), 1)
		self.assertEqual(doc2.get("channel_review_reason"), "discount_review")
		sr2 = doc2.special_requests or ""
		self.assertIn(f"added night(s) {d(8)}", sr2)
		self.assertNotIn("new total", sr2.lower())
		self.assertNotIn("1,250,000", sr2)
		self._assert_no_vendor_leaks(sr2)
		self.assertEqual(
			frappe.db.count("Agent Action Log", {
				"reference_doctype": "Reservation", "reference_name": res.name,
				"action_type": "channel.modify_discount_review", "action_channel": "API",
			}),
			2,
		)

	def test_ugx_18pct_vat_inclusive_100pct_comp_extend_preserves_surviving_comp_nights(self):
		"""Hydrogen R1 P0: 18% VAT-inclusive UGX 500,000/night 100% COMP stay
		extended 2n -> 3n via Guest OTA webhook keeps 0 charge on Nights 1 & 2 and
		bills only added Night 3 (UGX 500,000, no +UGX 500,000 overcharge)."""
		self._ensure_currency("UGX", symbol="UGX ", fraction_units=1, number_format="#,###")
		rt, rooms, conn, secret = self._setup_vat_property(
			PROP_UGX_VAT, country="Uganda", currency="UGX", tax_pct=18, base_price=500000, rt_code="UGCP",
		)
		ref = "OTA-UGXCP-" + frappe.generate_hash(length=6)
		res = make_reservation(
			PROP_UGX_VAT, rt, d(5), d(7), status="Confirmed", room=rooms[0],
			source="OTA", channel="Booking.com", ota_ref=ref, auto_price=1,
		)
		self.discount(
			res.name, discount_type="Percent", discount_value=100, **PROMO,
			supervisor_user=GM, supervisor_pin="4321",
		)

		out = self._post_guest_webhook(conn.name, secret, {
			"action": "modify", "hotelCode": "ut-vat-ugcp", "channel": "Booking.com",
			"bookingId": ref, "checkin": d(5), "checkout": d(8),
			"amount": {"currency": "UGX"},
			"specialRequests": "COMP extension",
			"rooms": [{"roomCode": "dlx", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
		})
		self.assertTrue(out["ok"])
		self.assertEqual(out["results"][0]["result"], "modified")

		doc = self.doc(res.name)
		self.assertEqual(len(doc.night_rates), 3)
		self.assertEqual(
			[round(flt(r.rate), 2) for r in doc.night_rates],
			[0.0, 0.0, 423728.81],
		)
		self.assertEqual(
			[round(flt(r.discount_amount), 2) for r in doc.night_rates],
			[500000.0, 500000.0, 0.0],
		)
		self.assertAlmostEqual(flt(doc.amount_after_tax), 500000.0, places=2)
		self.assertAlmostEqual(flt(doc.discount_amount), 1000000.0, places=2)
		self.assertEqual(int(doc.channel_review_needed), 1)
		self.assertEqual(doc.get("channel_review_reason"), "discount_review")
		sr = doc.special_requests or ""
		self.assertIn(f"kept approved desk discount on {d(5)}, {d(6)}", sr)
		self.assertIn(f"added night(s) {d(7)} priced without unapproved discount", sr)
		self.assertNotIn(f"added night(s) {d(6)}", sr)
		self.assertNotIn("new total", sr.lower())
		self._assert_no_vendor_leaks(sr)

		logs = frappe.get_all(
			"Agent Action Log",
			filters={
				"reference_doctype": "Reservation", "reference_name": res.name,
				"action_type": "channel.modify_discount_review", "action_channel": "API",
			},
			fields=["rationale"],
			order_by="creation desc",
		)
		self.assertEqual(len(logs), 1)
		self.assertIn("500,000", logs[0].rationale or "")
		self.assertIn(f"added night(s) {d(7)}", logs[0].rationale or "")
		self.assertNotIn(f"added night(s) {d(6)}", logs[0].rationale or "")
		self._assert_no_vendor_leaks(logs[0].rationale or "")

	def test_genuine_rate_room_party_and_plan_changes_still_drop_discount_and_separate_repriced_from_added(self):
		"""Zero security weakening: a genuine price hike, room category change,
		party change, or rate plan change on a surviving night still drops the
		unapproved discount and clearly separates repriced surviving nights from
		genuinely added nights in review text."""
		self._ensure_currency("UGX", symbol="UGX ", fraction_units=1, number_format="#,###")
		rt, rooms, conn, secret = self._setup_vat_property(
			PROP_UGX_VAT, country="Uganda", currency="UGX", tax_pct=18, base_price=500000,
			rt_code="UGSEC", extra_adult_price=100000,
		)
		rt_suite = make_room_type(PROP_UGX_VAT, "UGSTE", base_price=650000, tax_percent=18)
		frappe.db.set_value("Room Type", rt_suite, {"base_price": 650000, "tax_percent": 18})
		make_room(PROP_UGX_VAT, rt_suite, "UGSTE1")
		frappe.get_doc({
			"doctype": "Channel Room Mapping",
			"connection": conn.name,
			"room_type": rt_suite,
			"external_room_id": "ste",
		}).insert(ignore_permissions=True)

		# (a) Genuine price hike on surviving Night 2 (Season +100,000 on d6) + extend to d8 (adding d7)
		ref_hike = "OTA-UGXHK-" + frappe.generate_hash(length=6)
		res_hike = make_reservation(
			PROP_UGX_VAT, rt, d(5), d(7), status="Confirmed", room=rooms[0],
			source="OTA", channel="Booking.com", ota_ref=ref_hike, auto_price=1,
		)
		self.discount(
			res_hike.name, discount_type="Percent", discount_value=25, **PROMO,
			supervisor_user=GM, supervisor_pin="4321",
		)
		season = frappe.get_doc({
			"doctype": "Season",
			"property": PROP_UGX_VAT,
			"season_name": "Peak Night 2",
			"room_type": rt,
			"start_date": d(6),
			"end_date": d(6),
			"adjustment_type": "Amount",
			"adjustment_value": 100000,
			"priority": 10,
		}).insert(ignore_permissions=True)
		try:
			out_hike = self._post_guest_webhook(conn.name, secret, {
				"action": "modify", "hotelCode": "ut-vat-ugsec", "channel": "Booking.com",
				"bookingId": ref_hike, "checkin": d(5), "checkout": d(8),
				"amount": {"currency": "UGX"},
				"rooms": [{"roomCode": "dlx", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
			})
			self.assertTrue(out_hike["ok"])
			doc_hike = self.doc(res_hike.name)
			# Night 1 keeps 25% (375,000); repriced Night 2 drops discount (600,000); added Night 3 at rack (500,000)
			self.assertEqual(
				[round(flt(r.discount_amount), 2) for r in doc_hike.night_rates],
				[125000.0, 0.0, 0.0],
			)
			self.assertAlmostEqual(flt(doc_hike.amount_after_tax), 1475000.0, places=2)
			self.assertEqual(int(doc_hike.channel_review_needed), 1)
			self.assertEqual(doc_hike.get("channel_review_reason"), "discount_review")
			sr_hike = doc_hike.special_requests or ""
			self.assertIn(f"kept approved desk discount on {d(5)}", sr_hike)
			self.assertIn(f"repriced night(s) {d(6)}", sr_hike)
			self.assertIn(f"added night(s) {d(7)}", sr_hike)
			self.assertNotIn(f"added night(s) {d(6)}", sr_hike)
			self.assertNotIn("new total", sr_hike.lower())
			self._assert_no_vendor_leaks(sr_hike)

			# (a2) Surviving repriced night ONLY (shorten d5..d8 -> d5..d7 with Season on d6): no "added night(s)"
			ref_short = "OTA-UGXSH-" + frappe.generate_hash(length=6)
			frappe.db.set_value("Season", season.name, "disabled", 1)
			res_short = make_reservation(
				PROP_UGX_VAT, rt, d(5), d(8), status="Confirmed", room=rooms[1],
				source="OTA", channel="Booking.com", ota_ref=ref_short, auto_price=1,
			)
			self.discount(
				res_short.name, discount_type="Percent", discount_value=25, **PROMO,
				supervisor_user=GM, supervisor_pin="4321",
			)
			frappe.db.set_value("Season", season.name, "disabled", 0)
			out_short = self._post_guest_webhook(conn.name, secret, {
				"action": "modify", "hotelCode": "ut-vat-ugsec", "channel": "Booking.com",
				"bookingId": ref_short, "checkin": d(5), "checkout": d(7),
				"amount": {"currency": "UGX"},
				"rooms": [{"roomCode": "dlx", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
			})
			self.assertTrue(out_short["ok"])
			doc_short = self.doc(res_short.name)
			self.assertEqual(
				[round(flt(r.discount_amount), 2) for r in doc_short.night_rates],
				[125000.0, 0.0],
			)
			self.assertAlmostEqual(flt(doc_short.amount_after_tax), 975000.0, places=2)
			sr_short = doc_short.special_requests or ""
			self.assertIn(f"kept approved desk discount on {d(5)}", sr_short)
			self.assertIn(f"repriced night(s) {d(6)}", sr_short)
			self.assertNotIn("added night(s)", sr_short)
			self._assert_no_vendor_leaks(sr_short)
		finally:
			frappe.delete_doc("Season", season.name, force=True, ignore_permissions=True)

		# (b) Room category change (DLX -> STE) drops discount across all nights
		ref_rm = "OTA-UGXRM-" + frappe.generate_hash(length=6)
		res_rm = make_reservation(
			PROP_UGX_VAT, rt, d(5), d(7), status="Confirmed", room=None,
			source="OTA", channel="Booking.com", ota_ref=ref_rm, auto_price=1,
		)
		self.discount(
			res_rm.name, discount_type="Percent", discount_value=25, **PROMO,
			supervisor_user=GM, supervisor_pin="4321",
		)
		out_rm = self._post_guest_webhook(conn.name, secret, {
			"action": "modify", "hotelCode": "ut-vat-ugsec", "channel": "Booking.com",
			"bookingId": ref_rm, "checkin": d(5), "checkout": d(7),
			"amount": {"currency": "UGX"},
			"rooms": [{"roomCode": "ste", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
		})
		self.assertTrue(out_rm["ok"])
		doc_rm = self.doc(res_rm.name)
		self.assertEqual([round(flt(r.discount_amount), 2) for r in doc_rm.night_rates], [0.0, 0.0])
		self.assertAlmostEqual(flt(doc_rm.amount_after_tax), 1300000.0, places=2)
		self.assertEqual(doc_rm.get("channel_review_reason"), "discount_removed")
		self._assert_no_vendor_leaks(doc_rm.special_requests or "")

		# (c) Party change (adults 2 -> 3, fixed_rate=0) drops discount across all nights
		ref_pty = "OTA-UGXPT-" + frappe.generate_hash(length=6)
		res_pty = make_reservation(
			PROP_UGX_VAT, rt, d(5), d(7), status="Confirmed", room=rooms[3],
			source="OTA", channel="Booking.com", ota_ref=ref_pty, auto_price=1,
		)
		self.discount(
			res_pty.name, discount_type="Percent", discount_value=25, **PROMO,
			supervisor_user=GM, supervisor_pin="4321",
		)
		out_pty = self._post_guest_webhook(conn.name, secret, {
			"action": "modify", "hotelCode": "ut-vat-ugsec", "channel": "Booking.com",
			"bookingId": ref_pty, "checkin": d(5), "checkout": d(7),
			"amount": {"currency": "UGX"},
			"rooms": [{"roomCode": "dlx", "occupancy": {"adults": 3, "children": 0}, "prices": []}],
		})
		self.assertTrue(out_pty["ok"])
		doc_pty = self.doc(res_pty.name)
		self.assertEqual([round(flt(r.discount_amount), 2) for r in doc_pty.night_rates], [0.0, 0.0])
		self.assertAlmostEqual(flt(doc_pty.amount_after_tax), 1200000.0, places=2)
		self.assertEqual(doc_pty.get("channel_review_reason"), "discount_removed")
		self._assert_no_vendor_leaks(doc_pty.special_requests or "")

		# (d) Rate code (rate_plan) change judges all nights and drops unapproved discount
		plan_bar = frappe.get_doc({
			"doctype": "Rate Plan",
			"property": PROP_UGX_VAT,
			"rate_plan_name": "Standard BAR",
			"code": "SBAR",
			"modifier_type": "Percent",
			"modifier_value": 0,
			"discount_eligible": 1,
		}).insert(ignore_permissions=True)
		plan_corp = frappe.get_doc({
			"doctype": "Rate Plan",
			"property": PROP_UGX_VAT,
			"rate_plan_name": "Alt Rate Code",
			"code": "SARC",
			"modifier_type": "Percent",
			"modifier_value": 0,
			"discount_eligible": 1,
		}).insert(ignore_permissions=True)
		res_rp = make_reservation(
			PROP_UGX_VAT, rt, d(10), d(12), status="Confirmed", room=rooms[0],
			rate_plan=plan_bar.name, source="OTA", channel="Booking.com",
			ota_ref="OTA-UGXRP-" + frappe.generate_hash(length=6), auto_price=1,
		)
		self.discount(
			res_rp.name, discount_type="Percent", discount_value=25, **PROMO,
			supervisor_user=GM, supervisor_pin="4321",
		)
		old_rp = self.doc(res_rp.name)
		cand_rp = self.doc(res_rp.name)
		cand_rp.rate_plan = plan_corp.name
		rev_rp = cm._reconcile_ota_modify_discount(cand_rp, old_rp)
		self.assertIsNotNone(rev_rp)
		self.assertEqual(rev_rp["kind"], cm.CHANNEL_REVIEW_DISCOUNT_REMOVED)
		self.assertEqual(rev_rp["kept"], [])
		self.assertEqual(cand_rp.judged_nights(old_rp, cand_rp.quote_night_rows(cand_rp.requote())), {d(10), d(11)})

	def test_multicurrency_precision_usd_prec2_and_kwd_prec3_tax_inclusive_ota_extend(self):
		"""Multi-currency precision: USD (prec=2, 18% VAT-inclusive $100/night where
		net shifts 84.74 -> 84.75 and discounted net shifts 63.55 -> 63.57 by 0.02)
		and KWD (prec=3, 18% VAT-inclusive 10.000/night) preserve 25% and 100% COMP
		on surviving nights across OTA extensions while still catching genuine hikes."""
		# --- USD prec=2 ---
		self._ensure_currency("USD", symbol="$", fraction_units=100, number_format="#,###.##")
		rt_usd, rooms_usd, conn_usd, sec_usd = self._setup_vat_property(
			PROP_USD_VAT, country="United States", currency="USD", tax_pct=18, base_price=100,
			rt_code="USDV", secret="usd-vat-sec",
		)
		for idx, disc_val in enumerate((25, 100)):
			ref = f"OTA-USD{disc_val}-" + frappe.generate_hash(length=6)
			res = make_reservation(
				PROP_USD_VAT, rt_usd, d(5), d(7), status="Confirmed", room=rooms_usd[idx],
				source="OTA", channel="Booking.com", ota_ref=ref, auto_price=1,
			)
			# Precondition: Night 2 absorbed -0.01 remainder (84.74 vs 84.75)
			init_u = self.doc(res.name)
			self.assertAlmostEqual(flt(init_u.night_rates[0].base_rate), 84.75, places=2)
			self.assertAlmostEqual(flt(init_u.night_rates[1].base_rate), 84.74, places=2)

			self.discount(
				res.name, discount_type="Percent", discount_value=disc_val, **PROMO,
				supervisor_user=GM, supervisor_pin="4321",
			)
			out = self._post_guest_webhook(conn_usd.name, sec_usd, {
				"action": "modify", "hotelCode": "ut-vat-usdv", "channel": "Booking.com",
				"bookingId": ref, "checkin": d(5), "checkout": d(8),
				"amount": {"currency": "USD"},
				"rooms": [{"roomCode": "dlx", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
			})
			self.assertTrue(out["ok"], f"USD {disc_val}% extend failed: {out}")
			doc = self.doc(res.name)
			self.assertEqual(len(doc.night_rates), 3)
			if disc_val == 25:
				self.assertEqual([round(flt(r.discount_amount), 2) for r in doc.night_rates], [25.0, 25.0, 0.0])
				self.assertEqual([round(flt(r.rate), 2) for r in doc.night_rates], [63.57, 63.57, 84.75])
			else:
				self.assertEqual([round(flt(r.rate), 2) for r in doc.night_rates], [0.0, 0.0, 84.75])
				self.assertGreater(flt(doc.night_rates[0].discount_amount), 99.0)
				self.assertGreater(flt(doc.night_rates[1].discount_amount), 99.0)
				self.assertEqual(flt(doc.night_rates[2].discount_amount), 0.0)
			self.assertEqual(int(doc.channel_review_needed), 1)
			self.assertEqual(doc.get("channel_review_reason"), "discount_review")
			sr = doc.special_requests or ""
			self.assertIn(f"kept approved desk discount on {d(5)}, {d(6)}", sr)
			self.assertIn(f"added night(s) {d(7)}", sr)
			self.assertNotIn(f"added night(s) {d(6)}", sr)
			self._assert_no_vendor_leaks(sr)

		# --- KWD prec=3 ---
		self._ensure_currency("KWD", symbol="KD ", fraction_units=1000, number_format="#,###.###")
		rt_kwd, rooms_kwd, conn_kwd, sec_kwd = self._setup_vat_property(
			PROP_KWD_VAT, country="Kuwait", currency="KWD", tax_pct=18, base_price=10,
			rt_code="KWDV", secret="kwd-vat-sec",
		)
		for idx, disc_val in enumerate((25, 100)):
			ref = f"OTA-KWD{disc_val}-" + frappe.generate_hash(length=6)
			res = make_reservation(
				PROP_KWD_VAT, rt_kwd, d(5), d(7), status="Confirmed", room=rooms_kwd[idx],
				source="OTA", channel="Booking.com", ota_ref=ref, auto_price=1,
			)
			init_k = self.doc(res.name)
			self.assertEqual(Decimal(str(init_k.night_rates[0].base_rate)), Decimal("8.475"))
			self.assertEqual(Decimal(str(init_k.night_rates[1].base_rate)), Decimal("8.474"))

			self.discount(
				res.name, discount_type="Percent", discount_value=disc_val, **PROMO,
				supervisor_user=GM, supervisor_pin="4321",
			)
			out = self._post_guest_webhook(conn_kwd.name, sec_kwd, {
				"action": "modify", "hotelCode": "ut-vat-kwdv", "channel": "Booking.com",
				"bookingId": ref, "checkin": d(5), "checkout": d(8),
				"amount": {"currency": "KWD"},
				"rooms": [{"roomCode": "dlx", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
			})
			self.assertTrue(out["ok"], f"KWD {disc_val}% extend failed: {out}")
			doc = self.doc(res.name)
			self.assertEqual(len(doc.night_rates), 3)
			if disc_val == 25:
				self.assertEqual(
					[Decimal(str(r.discount_amount)).quantize(Decimal("0.001")) for r in doc.night_rates],
					[Decimal("2.500"), Decimal("2.500"), Decimal("0.000")],
				)
				self.assertEqual(
					[Decimal(str(r.rate)).quantize(Decimal("0.001")) for r in doc.night_rates],
					[Decimal("6.357"), Decimal("6.357"), Decimal("8.475")],
				)
			else:
				self.assertEqual(
					[Decimal(str(r.rate)).quantize(Decimal("0.001")) for r in doc.night_rates],
					[Decimal("0.000"), Decimal("0.000"), Decimal("8.475")],
				)
				self.assertEqual(Decimal(str(doc.night_rates[2].discount_amount)), Decimal("0"))
				self.assertGreater(flt(doc.night_rates[1].discount_amount), 9.9)
			self.assertEqual(int(doc.channel_review_needed), 1)
			self.assertEqual(doc.get("channel_review_reason"), "discount_review")
			sr = doc.special_requests or ""
			self.assertIn(f"kept approved desk discount on {d(5)}, {d(6)}", sr)
			self.assertIn(f"added night(s) {d(7)}", sr)
			self.assertNotIn(f"added night(s) {d(6)}", sr)
			self._assert_no_vendor_leaks(sr)

		# KWD negative check: genuine +0.010 KWD price hike on surviving Night 2 still drops its discount
		ref_kh = "OTA-KWDHK-" + frappe.generate_hash(length=6)
		res_kh = make_reservation(
			PROP_KWD_VAT, rt_kwd, d(10), d(12), status="Confirmed", room=rooms_kwd[2],
			source="OTA", channel="Booking.com", ota_ref=ref_kh, auto_price=1,
		)
		self.discount(
			res_kh.name, discount_type="Percent", discount_value=25, **PROMO,
			supervisor_user=GM, supervisor_pin="4321",
		)
		kwd_season = frappe.get_doc({
			"doctype": "Season",
			"property": PROP_KWD_VAT,
			"season_name": "KWD Minor Hike",
			"room_type": rt_kwd,
			"start_date": d(11),
			"end_date": d(11),
			"adjustment_type": "Amount",
			"adjustment_value": 0.010,
			"priority": 10,
		}).insert(ignore_permissions=True)
		try:
			out_kh = self._post_guest_webhook(conn_kwd.name, sec_kwd, {
				"action": "modify", "hotelCode": "ut-vat-kwdv", "channel": "Booking.com",
				"bookingId": ref_kh, "checkin": d(10), "checkout": d(13),
				"amount": {"currency": "KWD"},
				"rooms": [{"roomCode": "dlx", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
			})
			self.assertTrue(out_kh["ok"])
			doc_kh = self.doc(res_kh.name)
			self.assertEqual(Decimal(str(doc_kh.night_rates[0].discount_amount)).quantize(Decimal("0.001")), Decimal("2.500"))
			self.assertEqual(Decimal(str(doc_kh.night_rates[1].discount_amount)).quantize(Decimal("0.001")), Decimal("0.000"))
			self.assertEqual(Decimal(str(doc_kh.night_rates[2].discount_amount)).quantize(Decimal("0.001")), Decimal("0.000"))
			sr_kh = doc_kh.special_requests or ""
			self.assertIn(f"repriced night(s) {d(11)}", sr_kh)
			self.assertIn(f"added night(s) {d(12)}", sr_kh)
			self.assertNotIn(f"added night(s) {d(11)}", sr_kh)
		finally:
			frappe.delete_doc("Season", kwd_season.name, force=True, ignore_permissions=True)

	def test_adversarial_2n_to_3n_minor_price_hikes_drop_boundary_discount(self):
		"""Hydrogen R2 blocking finding (2n -> 3n): exact remainder reconstruction
		must catch minor genuine price hikes on old_last instead of waiving them
		under a loose (span // 2) * unit heuristic:
		- USD $101.00 -> $101.02 (18% VAT-inclusive, 2n -> 3n)
		- USD $100.00 -> $100.01 (18% VAT-inclusive, 2n -> 3n)
		- KWD 10.100 -> 10.102 (18% VAT-inclusive, 2n -> 3n)
		- USD $100.05 -> $100.06 (18% VAT-inclusive, 2n -> 3n, rounding-boundary +$0.01)"""
		self._ensure_currency("USD", symbol="$", fraction_units=100, number_format="#,###.##")
		self._ensure_currency("KWD", symbol="KD ", fraction_units=1000, number_format="#,###.###")

		cases = [
			("USD101", PROP_USD_VAT, "United States", "USD", 18, 101.0, 0.02, 2, d(5), d(7), d(8), "U101H"),
			("USD100", PROP_USD_VAT, "United States", "USD", 18, 100.0, 0.01, 2, d(10), d(12), d(13), "U100H"),
			("KWD101", PROP_KWD_VAT, "Kuwait", "KWD", 18, 10.100, 0.002, 3, d(5), d(7), d(8), "K101H"),
			("USD105", PROP_USD_VAT, "United States", "USD", 18, 100.05, 0.01, 2, d(15), d(17), d(18), "U105H"),
		]
		for label, prop, country, currency, tax_pct, base_price, hike, places, ci, co, new_co, rt_code in cases:
			with self.subTest(case=label):
				rt, rooms, conn, secret = self._setup_vat_property(
					prop, country=country, currency=currency, tax_pct=tax_pct,
					base_price=base_price, rt_code=rt_code, secret=f"sec-{rt_code.lower()}",
				)
				ref = f"OTA-{label}-" + frappe.generate_hash(length=6)
				res = make_reservation(
					prop, rt, ci, co, status="Confirmed", room=rooms[0],
					source="OTA", channel="Booking.com", ota_ref=ref, auto_price=1,
				)
				self.discount(
					res.name, discount_type="Percent", discount_value=25, **PROMO,
					supervisor_user=GM, supervisor_pin="4321",
				)
				n1_date = ci
				n2_date = str(frappe.utils.add_days(ci, 1))
				n3_date = str(frappe.utils.add_days(ci, 2))
				season = frappe.get_doc({
					"doctype": "Season",
					"property": prop,
					"season_name": f"{label} Minor Hike",
					"room_type": rt,
					"start_date": n2_date,
					"end_date": n2_date,
					"adjustment_type": "Amount",
					"adjustment_value": hike,
					"priority": 10,
				}).insert(ignore_permissions=True)
				try:
					# Direct judged_nights (desk amend_stay parity): Night 2 MUST be flagged
					old_doc = self.doc(res.name)
					cand = self.doc(res.name)
					cand.check_out_date = new_co
					cand.flags.kamra_stored = old_doc
					try:
						cand_rows = cand.quote_night_rows(cand.requote())
						judged = cand.judged_nights(old_doc, cand_rows)
					finally:
						cand.flags.kamra_stored = None
					self.assertIn(n2_date, judged, f"{label}: Night 2 hike +{hike} missed by judged_nights ({judged})")
					self.assertIn(n3_date, judged)

					# Real Guest OTA webhook modify: Night 1 keeps 25%, Night 2 drops discount
					out = self._post_guest_webhook(conn.name, secret, {
						"action": "modify", "hotelCode": f"ut-vat-{rt_code.lower()}",
						"channel": "Booking.com", "bookingId": ref,
						"checkin": ci, "checkout": new_co,
						"amount": {"currency": currency},
						"rooms": [{"roomCode": "dlx", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
					})
					self.assertTrue(out["ok"], f"{label}: webhook failed {out}")
					doc = self.doc(res.name)
					self.assertEqual(len(doc.night_rates), 3)
					self.assertGreater(flt(doc.night_rates[0].discount_amount), 0.0)
					self.assertEqual(
						round(flt(doc.night_rates[1].discount_amount), places), 0.0,
						f"{label}: Night 2 must drop unapproved discount after +{hike} hike",
					)
					self.assertEqual(round(flt(doc.night_rates[2].discount_amount), places), 0.0)
					self.assertEqual(int(doc.channel_review_needed), 1)
					self.assertEqual(doc.get("channel_review_reason"), "discount_review")
					sr = doc.special_requests or ""
					self.assertIn(f"kept approved desk discount on {n1_date}", sr)
					self.assertIn(f"repriced night(s) {n2_date}", sr)
					self.assertIn(f"added night(s) {n3_date}", sr)
					self.assertNotIn(f"added night(s) {n2_date}", sr)
					self.assertNotIn("new total", sr.lower())
					self._assert_no_vendor_leaks(sr)
				finally:
					frappe.delete_doc("Season", season.name, force=True, ignore_permissions=True)

	def test_adversarial_10n_span_minor_hikes_drop_discount_while_unchanged_spans_preserve_it(self):
		"""Hydrogen R2 blocking finding (10n -> 11n extend & 11n -> 10n shorten):
		- Genuine unchanged 10n -> 11n and 11n -> 10n stays preserve surviving 25% discounts
		  across USD $100 (18% VAT) and KWD 10.010 (18% VAT).
		- Minor price hikes on Night 10 MUST flag Night 10 and drop its unapproved discount:
		  * USD $100.00 -> $100.02 (18% VAT-inclusive, 10n -> 11n extend AND 11n -> 10n shorten)
		  * USD $110.00 -> $110.05 (10% VAT-inclusive, 10n -> 11n extend)
		  * KWD 10.010 -> 10.015 (18% VAT-inclusive, 10n -> 11n extend)"""
		self._ensure_currency("USD", symbol="$", fraction_units=100, number_format="#,###.##")
		self._ensure_currency("KWD", symbol="KD ", fraction_units=1000, number_format="#,###.###")

		# --- USD $100 @ 18% VAT-inclusive ---
		rt_u18, rooms_u18, conn_u18, sec_u18 = self._setup_vat_property(
			PROP_USD_VAT, country="United States", currency="USD", tax_pct=18,
			base_price=100.0, rt_code="U18S", secret="sec-u18s",
		)
		n10_date = d(14)  # 10th night of d(5)..d(15)
		n11_date = d(15)  # 11th night of d(5)..d(16)

		# (a) Positive control: USD $100 10n -> 11n unchanged gross preserves all 10 nights' 25% discount
		ref_u_ext_ok = "OTA-U10OK-" + frappe.generate_hash(length=6)
		res_u_ext_ok = make_reservation(
			PROP_USD_VAT, rt_u18, d(5), d(15), status="Confirmed", room=rooms_u18[0],
			source="OTA", channel="Booking.com", ota_ref=ref_u_ext_ok, auto_price=1,
		)
		self.assertAlmostEqual(flt(self.doc(res_u_ext_ok.name).night_rates[9].base_rate), 84.71, places=2)
		self.discount(
			res_u_ext_ok.name, discount_type="Percent", discount_value=25, **PROMO,
			supervisor_user=GM, supervisor_pin="4321",
		)
		out_u_ext_ok = self._post_guest_webhook(conn_u18.name, sec_u18, {
			"action": "modify", "hotelCode": "ut-vat-u18s", "channel": "Booking.com",
			"bookingId": ref_u_ext_ok, "checkin": d(5), "checkout": d(16),
			"amount": {"currency": "USD"},
			"rooms": [{"roomCode": "dlx", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
		})
		self.assertTrue(out_u_ext_ok["ok"])
		doc_u_ext_ok = self.doc(res_u_ext_ok.name)
		self.assertEqual([round(flt(r.discount_amount), 2) for r in doc_u_ext_ok.night_rates], [25.0] * 10 + [0.0])
		self.assertNotIn("repriced night(s)", doc_u_ext_ok.special_requests or "")
		self.assertIn(f"added night(s) {n11_date}", doc_u_ext_ok.special_requests or "")

		# (b) Positive control: USD $100 11n -> 10n shorten unchanged gross preserves all 10 nights cleanly
		ref_u_sh_ok = "OTA-U11OK-" + frappe.generate_hash(length=6)
		res_u_sh_ok = make_reservation(
			PROP_USD_VAT, rt_u18, d(5), d(16), status="Confirmed", room=rooms_u18[1],
			source="OTA", channel="Booking.com", ota_ref=ref_u_sh_ok, auto_price=1,
		)
		self.discount(
			res_u_sh_ok.name, discount_type="Percent", discount_value=25, **PROMO,
			supervisor_user=GM, supervisor_pin="4321",
		)
		out_u_sh_ok = self._post_guest_webhook(conn_u18.name, sec_u18, {
			"action": "modify", "hotelCode": "ut-vat-u18s", "channel": "Booking.com",
			"bookingId": ref_u_sh_ok, "checkin": d(5), "checkout": d(15),
			"amount": {"currency": "USD"},
			"rooms": [{"roomCode": "dlx", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
		})
		self.assertTrue(out_u_sh_ok["ok"])
		doc_u_sh_ok = self.doc(res_u_sh_ok.name)
		self.assertEqual([round(flt(r.discount_amount), 2) for r in doc_u_sh_ok.night_rates], [25.0] * 10)
		self.assertEqual(int(doc_u_sh_ok.channel_review_needed), 0)

		# (c) Counterexample: USD $100 -> $100.02 (18% VAT, 10n -> 11n extend) drops Night 10 discount
		# (d) Counterexample: USD $100 -> $100.02 (18% VAT, 11n -> 10n shorten) drops Night 10 discount
		ref_u_ext_hk = "OTA-U10HK-" + frappe.generate_hash(length=6)
		res_u_ext_hk = make_reservation(
			PROP_USD_VAT, rt_u18, d(5), d(15), status="Confirmed", room=rooms_u18[2],
			source="OTA", channel="Booking.com", ota_ref=ref_u_ext_hk, auto_price=1,
		)
		self.discount(
			res_u_ext_hk.name, discount_type="Percent", discount_value=25, **PROMO,
			supervisor_user=GM, supervisor_pin="4321",
		)
		ref_u_sh_hk = "OTA-U11HK-" + frappe.generate_hash(length=6)
		res_u_sh_hk = make_reservation(
			PROP_USD_VAT, rt_u18, d(5), d(16), status="Confirmed", room=rooms_u18[3],
			source="OTA", channel="Booking.com", ota_ref=ref_u_sh_hk, auto_price=1,
		)
		self.discount(
			res_u_sh_hk.name, discount_type="Percent", discount_value=25, **PROMO,
			supervisor_user=GM, supervisor_pin="4321",
		)
		u18_season = frappe.get_doc({
			"doctype": "Season",
			"property": PROP_USD_VAT,
			"season_name": "USD18 Night10 +0.02",
			"room_type": rt_u18,
			"start_date": n10_date,
			"end_date": n10_date,
			"adjustment_type": "Amount",
			"adjustment_value": 0.02,
			"priority": 10,
		}).insert(ignore_permissions=True)
		try:
			with self.subTest(case="USD100_18pct_10n11n_plus002"):
				out_ext = self._post_guest_webhook(conn_u18.name, sec_u18, {
					"action": "modify", "hotelCode": "ut-vat-u18s", "channel": "Booking.com",
					"bookingId": ref_u_ext_hk, "checkin": d(5), "checkout": d(16),
					"amount": {"currency": "USD"},
					"rooms": [{"roomCode": "dlx", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
				})
				self.assertTrue(out_ext["ok"])
				doc_ext = self.doc(res_u_ext_hk.name)
				self.assertEqual(
					[round(flt(r.discount_amount), 2) for r in doc_ext.night_rates],
					[25.0] * 9 + [0.0, 0.0],
					"USD $100->$100.02 (18% VAT, 10n->11n) must drop Night 10 discount",
				)
				self.assertAlmostEqual(flt(doc_ext.night_rates[9].rate), 84.76, places=2)
				self.assertIn(f"repriced night(s) {n10_date}", doc_ext.special_requests or "")
				self.assertIn(f"added night(s) {n11_date}", doc_ext.special_requests or "")
				self._assert_no_vendor_leaks(doc_ext.special_requests or "")

			with self.subTest(case="USD100_18pct_11n10n_shorten_plus002"):
				out_sh = self._post_guest_webhook(conn_u18.name, sec_u18, {
					"action": "modify", "hotelCode": "ut-vat-u18s", "channel": "Booking.com",
					"bookingId": ref_u_sh_hk, "checkin": d(5), "checkout": d(15),
					"amount": {"currency": "USD"},
					"rooms": [{"roomCode": "dlx", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
				})
				self.assertTrue(out_sh["ok"])
				doc_sh = self.doc(res_u_sh_hk.name)
				self.assertEqual(
					[round(flt(r.discount_amount), 2) for r in doc_sh.night_rates],
					[25.0] * 9 + [0.0],
					"USD $100->$100.02 (18% VAT, 11n->10n shorten) must drop Night 10 discount",
				)
				self.assertAlmostEqual(flt(doc_sh.night_rates[9].rate), 84.76, places=2)
				self.assertIn(f"repriced night(s) {n10_date}", doc_sh.special_requests or "")
				self.assertNotIn("added night(s)", doc_sh.special_requests or "")
				self._assert_no_vendor_leaks(doc_sh.special_requests or "")
		finally:
			frappe.delete_doc("Season", u18_season.name, force=True, ignore_permissions=True)

		# --- (e) Counterexample: USD $110 -> $110.05 (10% VAT-inclusive, 10n -> 11n) ---
		with self.subTest(case="USD110_10pct_10n11n_plus005"):
			rt_u10, rooms_u10, conn_u10, sec_u10 = self._setup_vat_property(
				PROP_USD_VAT10, country="United States", currency="USD", tax_pct=10,
				base_price=110.0, rt_code="U10S", secret="sec-u10s",
			)
			ref_u10 = "OTA-U110HK-" + frappe.generate_hash(length=6)
			res_u10 = make_reservation(
				PROP_USD_VAT10, rt_u10, d(5), d(15), status="Confirmed", room=rooms_u10[0],
				source="OTA", channel="Booking.com", ota_ref=ref_u10, auto_price=1,
			)
			self.discount(
				res_u10.name, discount_type="Percent", discount_value=25, **PROMO,
				supervisor_user=GM, supervisor_pin="4321",
			)
			u10_season = frappe.get_doc({
				"doctype": "Season",
				"property": PROP_USD_VAT10,
				"season_name": "USD10 Night10 +0.05",
				"room_type": rt_u10,
				"start_date": n10_date,
				"end_date": n10_date,
				"adjustment_type": "Amount",
				"adjustment_value": 0.05,
				"priority": 10,
			}).insert(ignore_permissions=True)
			try:
				out_u10 = self._post_guest_webhook(conn_u10.name, sec_u10, {
					"action": "modify", "hotelCode": "ut-vat-u10s", "channel": "Booking.com",
					"bookingId": ref_u10, "checkin": d(5), "checkout": d(16),
					"amount": {"currency": "USD"},
					"rooms": [{"roomCode": "dlx", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
				})
				self.assertTrue(out_u10["ok"])
				doc_u10 = self.doc(res_u10.name)
				self.assertEqual(
					[round(flt(r.discount_amount), 2) for r in doc_u10.night_rates],
					[27.5] * 9 + [0.0, 0.0],
					"USD $110->$110.05 (10% VAT, 10n->11n) must drop Night 10 discount",
				)
				self.assertAlmostEqual(flt(doc_u10.night_rates[9].rate), 100.05, places=2)
				self.assertIn(f"repriced night(s) {n10_date}", doc_u10.special_requests or "")
				self.assertIn(f"added night(s) {n11_date}", doc_u10.special_requests or "")
				self._assert_no_vendor_leaks(doc_u10.special_requests or "")
			finally:
				frappe.delete_doc("Season", u10_season.name, force=True, ignore_permissions=True)

		# --- (f) KWD 10.010 @ 18% VAT-inclusive: unchanged 10n->11n control + 10.010->10.015 counterexample ---
		rt_k18, rooms_k18, conn_k18, sec_k18 = self._setup_vat_property(
			PROP_KWD_VAT, country="Kuwait", currency="KWD", tax_pct=18,
			base_price=10.010, rt_code="K18S", secret="sec-k18s",
		)
		with self.subTest(case="KWD10010_18pct_10n11n_unchanged"):
			ref_k_ok = "OTA-K10OK-" + frappe.generate_hash(length=6)
			res_k_ok = make_reservation(
				PROP_KWD_VAT, rt_k18, d(5), d(15), status="Confirmed", room=rooms_k18[0],
				source="OTA", channel="Booking.com", ota_ref=ref_k_ok, auto_price=1,
			)
			self.assertEqual(Decimal(str(self.doc(res_k_ok.name).night_rates[9].base_rate)), Decimal("8.484"))
			self.discount(
				res_k_ok.name, discount_type="Percent", discount_value=25, **PROMO,
				supervisor_user=GM, supervisor_pin="4321",
			)
			out_k_ok = self._post_guest_webhook(conn_k18.name, sec_k18, {
				"action": "modify", "hotelCode": "ut-vat-k18s", "channel": "Booking.com",
				"bookingId": ref_k_ok, "checkin": d(5), "checkout": d(16),
				"amount": {"currency": "KWD"},
				"rooms": [{"roomCode": "dlx", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
			})
			self.assertTrue(out_k_ok["ok"])
			doc_k_ok = self.doc(res_k_ok.name)
			self.assertEqual(
				[Decimal(str(r.discount_amount)).quantize(Decimal("0.001")) for r in doc_k_ok.night_rates],
				[Decimal("2.502")] * 10 + [Decimal("0.000")],
			)
			self.assertNotIn("repriced night(s)", doc_k_ok.special_requests or "")
			self.assertIn(f"added night(s) {n11_date}", doc_k_ok.special_requests or "")

		with self.subTest(case="KWD10010_18pct_10n11n_plus0005"):
			ref_k_hk = "OTA-K10HK-" + frappe.generate_hash(length=6)
			res_k_hk = make_reservation(
				PROP_KWD_VAT, rt_k18, d(5), d(15), status="Confirmed", room=rooms_k18[1],
				source="OTA", channel="Booking.com", ota_ref=ref_k_hk, auto_price=1,
			)
			self.discount(
				res_k_hk.name, discount_type="Percent", discount_value=25, **PROMO,
				supervisor_user=GM, supervisor_pin="4321",
			)
			k18_season = frappe.get_doc({
				"doctype": "Season",
				"property": PROP_KWD_VAT,
				"season_name": "KWD18 Night10 +0.005",
				"room_type": rt_k18,
				"start_date": n10_date,
				"end_date": n10_date,
				"adjustment_type": "Amount",
				"adjustment_value": 0.005,
				"priority": 10,
			}).insert(ignore_permissions=True)
			try:
				out_k_hk = self._post_guest_webhook(conn_k18.name, sec_k18, {
					"action": "modify", "hotelCode": "ut-vat-k18s", "channel": "Booking.com",
					"bookingId": ref_k_hk, "checkin": d(5), "checkout": d(16),
					"amount": {"currency": "KWD"},
					"rooms": [{"roomCode": "dlx", "occupancy": {"adults": 2, "children": 0}, "prices": []}],
				})
				self.assertTrue(out_k_hk["ok"])
				doc_k_hk = self.doc(res_k_hk.name)
				self.assertEqual(
					[Decimal(str(r.discount_amount)).quantize(Decimal("0.001")) for r in doc_k_hk.night_rates],
					[Decimal("2.502")] * 9 + [Decimal("0.000"), Decimal("0.000")],
					"KWD 10.010->10.015 (18% VAT, 10n->11n) must drop Night 10 discount",
				)
				self.assertEqual(Decimal(str(doc_k_hk.night_rates[9].rate)).quantize(Decimal("0.001")), Decimal("8.487"))
				self.assertIn(f"repriced night(s) {n10_date}", doc_k_hk.special_requests or "")
				self.assertIn(f"added night(s) {n11_date}", doc_k_hk.special_requests or "")
				self._assert_no_vendor_leaks(doc_k_hk.special_requests or "")
			finally:
				frappe.delete_doc("Season", k18_season.name, force=True, ignore_permissions=True)

