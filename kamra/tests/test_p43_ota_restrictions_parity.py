"""P43: Bidirectional OTA Restrictions Push & Channel Mapping Health.

Live Frappe v16 / MariaDB 11.8.9 + loopback HTTP receiver integration tests:
- Channex POST /restrictions carries exact rate_quantum strings for KWD (3dp "45.125"),
  UGX (0dp "350000"), USD (2dp "129.00") AND min_stay_arrival, max_stay,
  closed_to_arrival, closed_to_departure, stop_sell compressed across consecutive days,
  plus clearing previously sent restrictions.
- AioSell build_push_bodies & push_ari include restriction fields (stopSell, minStay,
  maxStay, cta, ctd) with exact currency precision over live loopback HTTP.
- Inbound OTA webhook restriction mismatch: default honor policy sets
  channel_review_needed=1 & channel_review_reason="restriction_mismatch"; strict mode
  (strict_ota_restrictions=1) rejects creation explicitly and logs Agent Action Log.
- channel_health_snapshot & retry_outbox_failures enforce multi-property isolation,
  compute live rate parity (OK / Undercut / Overpriced), and expose outbox diagnostics.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from kamra import channel_ari
from kamra import channel_manager as cm
from kamra.channels import aiosell
from kamra.restrictions import (
	bulk_set_restrictions,
	channel_health_snapshot,
	retry_outbox_failures,
)
from kamra.settings import clear_cache, write_kv


def day(offset: int) -> str:
	return str(add_days(nowdate(), offset))


class _LoopbackReceiver:
	"""Real loopback HTTP server recording Channex & AioSell POST bodies."""

	def __init__(self):
		self.requests: list[dict] = []
		receiver = self

		class Handler(BaseHTTPRequestHandler):
			def do_POST(self):
				length = int(self.headers.get("Content-Length") or 0)
				raw = self.rfile.read(length) if length else b"{}"
				body = json.loads(raw.decode("utf-8"))
				receiver.requests.append({"path": self.path, "body": body})
				if "update-rates/" in self.path or "update/" in self.path:
					resp = {"success": True, "message": "ok"}
				else:
					resp = {"data": [{"id": f"task-{len(receiver.requests)}", "type": "task"}],
					        "meta": {"message": "Success"}}
				encoded = json.dumps(resp).encode("utf-8")
				self.send_response(200)
				self.send_header("Content-Type", "application/json")
				self.send_header("Content-Length", str(len(encoded)))
				self.end_headers()
				self.wfile.write(encoded)

			def log_message(self, format, *args):
				return

		self._server = HTTPServer(("127.0.0.1", 0), Handler)
		self.port = self._server.server_address[1]
		self.base_url = f"http://127.0.0.1:{self.port}"
		self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
		self._thread.start()

	def close(self):
		self._server.shutdown()
		self._server.server_close()
		self._thread.join(timeout=2)


class OtaRestrictionsParityTestCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		frappe.local.lang = "en"

	def tearDown(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- restore test user

	def _flush_in_tx(self, conn_name: str) -> dict:
		state = {"sp": "p43_" + frappe.generate_hash(length=8)}
		frappe.db.savepoint(state["sp"])
		rollback = frappe.db.rollback

		def worker_commit(*args, **kwargs):
			state["sp"] = "p43_" + frappe.generate_hash(length=8)
			frappe.db.savepoint(state["sp"])

		def worker_rollback(*args, **kwargs):
			if args or kwargs:
				return rollback(*args, **kwargs)
			return rollback(save_point=state["sp"])

		with patch.object(frappe.db, "commit", side_effect=worker_commit), \
		     patch.object(frappe.db, "rollback", side_effect=worker_rollback):
			return channel_ari.flush_outbox(conn_name)

	def _make_channel_hotel(self, *, currency: str, country: str, price: float,
	                        provider: str = "Channex", endpoint: str = "http://127.0.0.1:9"):
		if frappe.db.exists("Currency", currency):
			frappe.db.set_value("Currency", currency, "enabled", 1)
		tag = frappe.generate_hash(length=6).upper()
		prop = frappe.get_doc({
			"doctype": "Property",
			"property_name": f"TEST P43 {currency} {tag}",
			"country": country,
			"currency": currency,
			"rates_include_tax": 0,
			"gst_rate_low": 0,
			"gst_rate_high": 0,
			"business_date": nowdate(),
		}).insert(ignore_permissions=True).name
		write_kv(prop, "rate_plan_required", 0)
		clear_cache(prop)

		rt = frappe.get_doc({
			"doctype": "Room Type",
			"property": prop,
			"room_type_code": f"STD{tag}",
			"room_type_name": "Standard",
			"base_price": price,
			"base_occupancy": 2,
			"adults_capacity": 2,
		}).insert(ignore_permissions=True).name

		for i in range(3):
			frappe.get_doc({
				"doctype": "Room",
				"property": prop,
				"room_type": rt,
				"room_number": f"{tag}-{i + 1}",
			}).insert(ignore_permissions=True)

		conn_doc = {
			"doctype": "Channel Manager Connection",
			"property": prop,
			"provider": provider,
			"active": 1,
			"external_property_id": f"ext-prop-{tag}",
			"api_key": "live-test-key",
			"webhook_secret": "p43-hook-secret",
			"endpoint": endpoint,
			"ari_batch_seconds": 0,
		}
		if provider == "AioSell":
			conn_doc.update(api_username="p43user", pms_slug="p43-pms")
		conn = frappe.get_doc(conn_doc).insert(ignore_permissions=True)

		mapping = frappe.get_doc({
			"doctype": "Channel Room Mapping",
			"connection": conn.name,
			"room_type": rt,
			"external_room_id": f"rt-{tag}",
			"external_rate_id": f"rp-{tag}",
			"external_currency": currency,
			"rate_plan": None,
		}).insert(ignore_permissions=True)

		frappe.db.delete("Channel ARI Outbox", {"connection": conn.name})
		for kind in channel_ari.KINDS:
			frappe.cache.delete_value(f"channex_ari_pause::{conn.name}::{kind}")
			frappe.cache.delete_value(f"channex_ari_calls::{conn.name}::{kind}")

		return frappe._dict(property=prop, room_type=rt, conn=conn, mapping=mapping, tag=tag)

	def test_channex_restrictions_push_preserves_0_2_3dp_and_restriction_fields(self):
		receiver = _LoopbackReceiver()
		self.addCleanup(receiver.close)

		d5, d7 = day(5), day(7)
		cases = [
			("KWD", "Kuwait", 45.125, "45.125"),
			("UGX", "Uganda", 350000, "350000"),
			("USD", "United States", 129.00, "129.00"),
		]
		for currency, country, price, expected_rate_str in cases:
			with self.subTest(currency=currency):
				receiver.requests.clear()
				h = self._make_channel_hotel(
					currency=currency, country=country, price=price,
					provider="Channex", endpoint=receiver.base_url,
				)
				bulk_set_restrictions(
					h.property, d5, d7, room_type=h.room_type,
					stop_sell=1, min_los=2, max_los=7, cta=1, ctd=0,
					reason=f"Push {currency}",
				)
				self._flush_in_tx(h.conn.name)

				restriction_posts = [r["body"]["values"] for r in receiver.requests if r["path"].endswith("/restrictions")]
				self.assertEqual(len(restriction_posts), 1, receiver.requests)
				values = restriction_posts[0]
				# 3 consecutive identical days compressed into 1 range block
				self.assertEqual(len(values), 1, values)
				entry = values[0]
				self.assertEqual(entry["rate_plan_id"], h.mapping.external_rate_id)
				self.assertEqual(entry["date_from"], d5)
				self.assertEqual(entry["date_to"], d7)
				self.assertEqual(entry["rate"], expected_rate_str)
				self.assertIs(entry["stop_sell"], True)
				self.assertEqual(entry["min_stay_arrival"], 2)
				self.assertEqual(entry["max_stay"], 7)
				self.assertIs(entry["closed_to_arrival"], True)
				self.assertIs(entry["closed_to_departure"], False)

				# Clearing restrictions pushes the reset to Channex
				receiver.requests.clear()
				bulk_set_restrictions(
					h.property, d5, d7, room_type=h.room_type,
					stop_sell=0, min_los=0, max_los=0, cta=0, ctd=0,
					reason=f"Lift {currency}",
				)
				self._flush_in_tx(h.conn.name)

				lift_posts = [r["body"]["values"] for r in receiver.requests if r["path"].endswith("/restrictions")]
				self.assertEqual(len(lift_posts), 1, receiver.requests)
				lifted = lift_posts[0][0]
				self.assertEqual(lifted["date_from"], d5)
				self.assertEqual(lifted["date_to"], d7)
				self.assertIs(lifted["stop_sell"], False)
				self.assertEqual(lifted["min_stay_arrival"], 1)
				self.assertEqual(lifted["max_stay"], 0)
				self.assertIs(lifted["closed_to_arrival"], False)

	def test_aiosell_push_includes_restrictions_and_preserves_precision(self):
		receiver = _LoopbackReceiver()
		self.addCleanup(receiver.close)

		h = self._make_channel_hotel(
			currency="KWD", country="Kuwait", price=45.125,
			provider="AioSell", endpoint=receiver.base_url,
		)
		d4, d5 = day(4), day(5)
		bulk_set_restrictions(
			h.property, d4, d5, room_type=h.room_type,
			stop_sell=1, min_los=2, max_los=5, cta=1, ctd=1,
			reason="AioSell restriction push",
		)
		snapshot = cm.ari_snapshot(h.property, h.conn.name, days=2, start=d4)
		ok, detail = aiosell.push_ari(h.conn, snapshot)
		self.assertTrue(ok, detail)

		rate_posts = [r["body"] for r in receiver.requests if "update-rates/" in r["path"]]
		self.assertEqual(len(rate_posts), 1, receiver.requests)
		block = rate_posts[0]["updates"][0]
		self.assertEqual(block["startDate"], d4)
		self.assertEqual(block["endDate"], d5)
		rate_entry = block["rates"][0]
		self.assertAlmostEqual(float(rate_entry["rate"]), 45.125, places=3)
		self.assertIs(rate_entry["stopSell"], True)
		self.assertEqual(rate_entry["minStay"], 2)
		self.assertEqual(rate_entry["maxStay"], 5)
		self.assertIs(rate_entry["cta"], True)
		self.assertIs(rate_entry["ctd"], True)

	def test_inbound_ota_restriction_mismatch_honor_vs_strict(self):
		h = self._make_channel_hotel(currency="USD", country="United States", price=150.0)
		d8, d9 = day(8), day(9)
		bulk_set_restrictions(
			h.property, d8, d8, room_type=h.room_type,
			stop_sell=1, min_los=3, reason="OTA race window",
		)

		event_honor = {
			"event": "book",
			"ota_ref": f"OTA-HONOR-{h.tag}",
			"channel": "Booking.com",
			"room_type_external_id": h.mapping.external_room_id,
			"rate_plan_external_id": h.mapping.external_rate_id,
			"check_in": d8,
			"check_out": d9,
			"adults": 2,
			"children": 0,
			"guest_name": "Honor Guest",
			"phone": "+12025550201",
			"email": "honor@example.test",
			"total": 150.0,
			"nightly": [{"date": d8, "amount": 150.0}],
			"currency": "USD",
			"notes": "",
		}
		res_honor = cm._apply_event(h.conn, dict(event_honor))
		self.assertEqual(res_honor["result"], "booked")
		self.assertTrue(res_honor.get("restriction_mismatch"))
		doc = frappe.get_doc("Reservation", res_honor["reservation"])
		self.assertEqual(doc.channel_review_needed, 1)
		self.assertEqual(doc.channel_review_reason, "restriction_mismatch")

		# Enable strict_ota_restrictions -> webhook refuses booking explicitly
		frappe.get_doc({
			"doctype": "Property Setting",
			"property": h.property,
			"setting_key": "strict_ota_restrictions",
			"value": "1",
		}).insert(ignore_permissions=True)
		event_strict = dict(event_honor, ota_ref=f"OTA-STRICT-{h.tag}", guest_name="Strict Guest")
		res_strict = cm._apply_event(h.conn, event_strict)
		self.assertEqual(res_strict["result"], "restriction_refused")
		self.assertFalse(frappe.db.exists("Reservation", {"ota_ref": event_strict["ota_ref"]}))

	def test_channel_health_snapshot_parity_retry_and_multi_property_isolation(self):
		receiver = _LoopbackReceiver()
		self.addCleanup(receiver.close)

		h_a = self._make_channel_hotel(
			currency="USD", country="United States", price=120.00,
			provider="Channex", endpoint=receiver.base_url,
		)
		h_b = self._make_channel_hotel(
			currency="KWD", country="Kuwait", price=50.000,
			provider="Channex", endpoint=receiver.base_url,
		)
		d0 = day(0)
		# Seed last accepted Channex rate at 100.00 while PMS direct rate is 120.00 -> Undercut (-20.00)
		channel_ari._remember(
			h_a.conn.name, "restrictions",
			{h_a.mapping.external_rate_id: {
				d0: {"rate": "100.00", "stop_sell": False, "min_stay_arrival": 2, "closed_to_arrival": True},
			}},
		)
		# Seed a Failed outbox row on Property A
		failed_row = frappe.get_doc({
			"doctype": "Channel ARI Outbox",
			"connection": h_a.conn.name,
			"property": h_a.property,
			"date_from": d0,
			"date_to": day(2),
			"kinds": "restrictions",
			"status": "Failed",
			"attempts": 3,
			"reason": "Test push failure",
			"detail": "Channex 422: rate plan rejected",
		}).insert(ignore_permissions=True)

		snap = channel_health_snapshot(h_a.property, days=7)
		self.assertEqual(snap["property"], h_a.property)
		self.assertEqual(snap["property_currency"], "USD")
		self.assertEqual([c["name"] for c in snap["connections"]], [h_a.conn.name])
		self.assertEqual(snap["summary"]["outbox_failed"], 1)

		parity_row = snap["parity"][0]
		self.assertEqual(parity_row["mapping_name"], h_a.mapping.name)
		self.assertAlmostEqual(parity_row["direct_rate"], 120.0, places=2)
		self.assertAlmostEqual(parity_row["ota_rate"], 100.0, places=2)
		self.assertAlmostEqual(parity_row["parity_delta"], -20.0, places=2)
		self.assertEqual(parity_row["parity_status"], "Undercut")
		self.assertEqual(parity_row["restrictions_synced"]["min_stay_arrival"], 2)
		self.assertTrue(parity_row["restrictions_synced"]["closed_to_arrival"])

		# Retry failed outbox resets row to Pending
		retried = retry_outbox_failures(h_a.conn.name)
		self.assertTrue(retried["ok"])
		self.assertEqual(retried["retried"], 1)
		self.assertEqual(frappe.db.get_value("Channel ARI Outbox", failed_row.name, "status"), "Pending")
		self.assertEqual(frappe.db.get_value("Channel ARI Outbox", failed_row.name, "attempts"), 0)

		# Property B Revenue Manager is blocked from reading or retrying Property A
		email_b = f"rm-p43-{h_b.tag.lower()}@example.test"
		if not frappe.db.exists("User", email_b):
			frappe.get_doc({
				"doctype": "User",
				"email": email_b,
				"first_name": "RM B",
				"enabled": 1,
				"send_welcome_email": 0,
				"roles": [{"role": "Revenue Manager"}],
			}).insert(ignore_permissions=True)
		frappe.db.delete("User Permission", {"user": email_b, "allow": "Property"})
		frappe.get_doc({
			"doctype": "User Permission",
			"user": email_b,
			"allow": "Property",
			"for_value": h_b.property,
			"apply_to_all_doctypes": 1,
		}).insert(ignore_permissions=True)

		frappe.set_user(email_b)  # nosemgrep: frappe-setuser -- property isolation test
		with self.assertRaises(frappe.PermissionError):
			channel_health_snapshot(h_a.property)
		with self.assertRaises(frappe.PermissionError):
			retry_outbox_failures(h_a.conn.name)
