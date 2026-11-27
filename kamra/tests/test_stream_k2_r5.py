"""Reservation, physical inventory and encoder transactions on a live site."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from kamra import allocation, api, channel_manager, checkin, public_api, settings
from kamra.door_lock import EdgeAdapter, KeyResult, SimulatorAdapter
from kamra.tests.audit_fixtures import TODAY, d, make_property, make_reservation, make_room, make_room_type


class ReservationTransactionTests(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		self.prop = make_property("UT R5 " + frappe.generate_hash(length=10), business_date=TODAY)
		self.rt = make_room_type(self.prop)
		self.room = make_room(self.prop, self.rt, "101")

	def stay(self, checked_in=False):
		return make_reservation(self.prop, self.rt, TODAY, d(1),
			status="Checked In" if checked_in else "Confirmed", room=self.room if checked_in else None)

	def test_composite_claims_members_in_writer_and_ari(self):
		second = make_room(self.prop, self.rt, "102")
		family = make_room_type(self.prop, "FAM")
		unit = frappe.get_doc({"doctype": "Sellable Unit", "unit_name": "Family", "property": self.prop,
			"room_type": family, "unit_kind": "composite", "is_active": 1,
			"physical_rooms": [{"room": self.room}, {"room": second}]}).insert()
		make_reservation(self.prop, family, TODAY, d(1), sellable_unit=unit.name)
		left = channel_manager._sellable(self.prop, self.rt, TODAY, d(1))
		with self.subTest("channel"):
			self.assertEqual(left, 0)
		with self.subTest("reservation"), self.assertRaises(frappe.ValidationError):
			self.stay()
		self.assertEqual(channel_manager._sellable(self.prop, self.rt, d(1), d(2)), 2)

	def test_sell_limit_endpoint_and_desk_refresh_connected_channel(self):
		conn = frappe.get_doc({"doctype": "Channel Manager Connection", "property": self.prop,
			"provider": "Channex", "active": 1, "external_property_id": "ut-r5"}).insert()
		start = str(add_days(nowdate(), 3))
		end = str(add_days(start, 1))

		def pending():
			return {(str(r.date_from), str(r.date_to), r.kinds) for r in frappe.get_all(
				"Channel ARI Outbox", filters={"connection": conn.name, "status": "Pending"},
				fields=["date_from", "date_to", "kinds"])}

		for value in ("-1", "0"):
			frappe.db.delete("Channel ARI Outbox", {"connection": conn.name})
			allocation.set_sell_limit(self.prop, self.rt, start, end, value)
			with self.subTest(endpoint=value):
				self.assertIn((start, end, "availability"), pending())
		frappe.db.delete("Channel ARI Outbox", {"connection": conn.name})
		limit = frappe.get_doc("Room Sell Limit", {"property": self.prop, "room_type": self.rt, "stay_date": start})
		limit.adjustment = -1
		limit.save()
		self.assertIn((start, start, "availability"), pending())

	def test_reverse_checkin_processes_each_bridge_issuer(self):
		from frappe.utils.password import set_encrypted_password
		stay = self.stay(True)
		urls = ["https://locks.example/a", "https://locks.example/b"]
		set_encrypted_password("Property", self.prop, frappe.generate_hash(), fieldname="door_lock_secret")
		with patch("kamra.door_lock.get_adapter", return_value=EdgeAdapter()), patch(
			"kamra.bridge_transport.post_json", return_value=200) as send:
			for url in urls:
				settings.write_kv(self.prop, "door_lock_endpoint", url)
				settings.clear_cache(self.prop)
				self.assertTrue(checkin.key_operation(stay.name, "issue")["ok"])
			result = api.reverse_check_in(stay.name, "Mistaken arrival")
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- exercise the real transaction boundary in the integration fixture
			self.assertEqual([(c.args[0], c.args[1]["room"]) for c in send.call_args_list
			                  if c.args[1]["operation"] == "revoke"], [(urls[1], self.room)])
			warning, = result["key_result"]["pending_revocations"]
			settings.write_kv(self.prop, "door_lock_endpoint", urls[0])
			settings.clear_cache(self.prop)
			self.assertTrue(checkin.retry_revocation(stay.name, warning["operation_id"])["ok"])
			self.assertEqual(send.call_args.args[0], urls[0])

	def test_rollback_preserves_current_key_then_commit_dispatches_once(self):
		stay = self.stay(True)
		other = make_room(self.prop, self.rt, "102")
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- exercise the real transaction boundary in the integration fixture
		adapter = SimulatorAdapter()
		with patch("kamra.door_lock.get_adapter", return_value=adapter), patch.object(
			adapter, "revoke_request", wraps=adapter.revoke_request) as revoke:
			for action in ("move", "checkout"):
				with self.subTest(action=action):
					revoke.reset_mock()
					with patch.dict(frappe.flags, allow_unsettled_checkout=True):
						if action == "move":
							api.move_room(stay.name, other)
						else:
							api.check_out(stay.name)
					frappe.db.rollback()
					self.assertEqual(frappe.db.get_value("Reservation", stay.name, ["room", "status"]),
					                 (self.room, "Checked In"))
					self.assertEqual(revoke.call_count, 0)
			revoke.reset_mock()
			out = api.move_room(stay.name, other)
			before_commit = revoke.call_count
			intents = frappe.get_all("Door Key Audit", filters={"reservation": stay.name, "operation": "room_move_revoke"}, pluck="name")
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- exercise the real transaction boundary in the integration fixture
			self.assertEqual((before_commit, revoke.call_count), (0, 1))
			self.assertEqual(len(intents), 1)
			self.assertEqual(frappe.db.get_value("Door Key Audit", intents[0], "status"), "Accepted")
			self.assertEqual(out["pending_revocations"], [])

	def test_allocation_rechecks_after_a_concurrent_desk_commit(self):
		make_room(self.prop, self.rt, "102")
		a, b = self.stay(), self.stay()
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- exercise the real transaction boundary in the integration fixture
		# This request established its repeatable-read snapshot before the other desk assigned 101.
		self.assertFalse(frappe.db.get_value("Reservation", a.name, "room"))

		def assign_first(site):
			frappe.init(site=site)
			try:
				frappe.connect()
				frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- independent desk fixture
				return allocation.apply_allocation(self.prop, [{"reservation": a.name, "room": self.room}])
			finally:
				frappe.destroy()

		with ThreadPoolExecutor(1) as pool:
			self.assertEqual(len(pool.submit(assign_first, frappe.local.site).result(timeout=30)["executed"]), 1)
		result = allocation.apply_allocation(self.prop, [{"reservation": b.name, "room": self.room}])
		self.assertFalse(result["executed"])
		self.assertFalse(frappe.db.get_value("Reservation", b.name, "room"))

	def test_public_search_respects_dated_holdback_and_internal_oos_rule(self):
		frappe.db.set_value("Room", self.room, "housekeeping_status", "Out of Service")
		def offer():
			return next(r for r in public_api.search_stay(self.prop, TODAY, d(2)) if r["room_type"] == self.rt)
		self.assertEqual(offer()["rooms_left"], 1)
		allocation.set_sell_limit(self.prop, self.rt, d(1), d(1), "-1")
		row = offer()
		self.assertEqual((row["rooms_left"], row["available"]), (0, False))

	def test_later_issuance_invalidates_failed_desk_revocation(self):
		stay = self.stay(True)
		adapter = SimulatorAdapter()
		with patch("kamra.door_lock.get_adapter", return_value=adapter), patch.object(
			adapter, "revoke_request", return_value=KeyResult(False, adapter.key)) as revoke:
			op = checkin.key_operation(stay.name, "revoke")["operation_id"]
			self.assertTrue(checkin.key_operation(stay.name, "issue")["ok"])
			row = next(r for r in checkin.door_key_audit(stay.name) if r.name == op)
			with self.subTest("audit"):
				self.assertFalse(row.retryable)
			revoke.return_value = KeyResult(True, adapter.key)
			with self.assertRaises(frappe.ValidationError):
				checkin.retry_revocation(stay.name, op)
			self.assertEqual(revoke.call_count, 1)
