"""Regression coverage for encoder side effects and dated distribution limits."""
from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase

from kamra import allocation, api, channel_manager, checkin, settings
from kamra.door_lock import EdgeAdapter, KeyResult, ManualAdapter, SimulatorAdapter
from kamra.tests.audit_fixtures import (
	TODAY, d, make_property, make_reservation, make_room, make_room_type,
)


class EncoderAndInventoryTests(IntegrationTestCase):
	properties = ("UT K2 Reconciliation",)

	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		self.prop = make_property(self.properties[0] + " " + frappe.generate_hash(length=8), business_date=TODAY)
		self.rt = make_room_type(self.prop)
		self.old = make_room(self.prop, self.rt, "101")
		self.new = make_room(self.prop, self.rt, "102")
		self.res = make_reservation(self.prop, self.rt, TODAY, d(1), status="Checked In", room=self.old)
		self.adapter = SimulatorAdapter()
		self.enterContext(patch("kamra.door_lock.get_adapter", return_value=self.adapter))

	def failed_revocation(self):
		self.adapter.revoke_keys = Mock(return_value=KeyResult(False, "simulator", "Encoder offline"))

	def test_move_warning_and_retry_identify_the_old_room(self):
		self.failed_revocation()
		result = api.move_room(self.res.name, self.new)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- exercise the real transaction boundary in the integration fixture
		warning, = result["pending_revocations"]
		self.assertEqual(warning["room"], self.old)
		self.adapter.revoke_keys.return_value = KeyResult(True, "simulator")
		self.assertTrue(checkin.retry_revocation(self.res.name, warning["operation_id"])["ok"])
		self.adapter.revoke_keys.assert_called_with(self.prop, self.res.name, self.old)

	def test_checkout_returns_actionable_revocation(self):
		self.failed_revocation()
		with patch.dict(frappe.flags, allow_unsettled_checkout=True):
			out = api.check_out(self.res.name)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- exercise the real transaction boundary in the integration fixture
		self.assertEqual(out["pending_revocations"][0]["room"], self.old)

	def test_swap_returns_both_revocations(self):
		second = make_reservation(self.prop, self.rt, TODAY, d(1), status="Checked In", room=self.new)
		for room in (self.old, self.new):
			frappe.db.set_value("Room", room, "housekeeping_status", "Clean")
		self.failed_revocation()
		out = api.move_room(self.res.name, self.new, swap_reservation=second.name)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- exercise the real transaction boundary in the integration fixture
		self.assertEqual({w["room"] for w in out["pending_revocations"]}, {self.old, self.new})

	def test_adapter_change_keeps_external_revocation_actionable(self):
		edge = EdgeAdapter()
		with patch.object(edge, "_send", return_value=KeyResult(True, edge.key)) as send:
			with patch("kamra.door_lock.get_adapter", return_value=edge):
				self.assertTrue(checkin.key_operation(self.res.name, "issue")["ok"])
			with patch("kamra.door_lock.get_adapter", return_value=ManualAdapter()):
				moved = api.move_room(self.res.name, self.new)
				frappe.db.commit()  # nosemgrep: frappe-manual-commit -- exercise the real transaction boundary in the integration fixture
			self.assertEqual(len(moved["pending_revocations"]), 1)
			warning, = moved["pending_revocations"]
			self.assertEqual(warning["room"], self.old)
			row = next(r for r in checkin.door_key_audit(self.res.name) if r.name == warning["operation_id"])
			self.assertEqual((row.adapter_key, row.status, row.retryable), (edge.key, "Failed", True))
			with patch("kamra.door_lock.get_adapter", return_value=edge):
				self.assertTrue(checkin.retry_revocation(self.res.name, row.name)["ok"])
				payload = send.call_args.args[1]
				self.assertEqual((payload["room"], payload["operation"]), (self.old, "revoke"))
				self.assertTrue(checkin.key_operation(self.res.name, "issue")["ok"])
			with patch("kamra.door_lock.get_adapter", return_value=ManualAdapter()), patch.dict(
				frappe.flags, allow_unsettled_checkout=True
			):
				departed = api.check_out(self.res.name)
				frappe.db.commit()  # nosemgrep: frappe-manual-commit -- exercise the real transaction boundary in the integration fixture
			self.assertEqual([w["room"] for w in departed["pending_revocations"]], [self.new])

	def test_queue_outage_leaves_recoverable_arrival_intent(self):
		self.res.key_count = 2
		self.adapter.key = "fias_edge"
		with patch("frappe.enqueue", side_effect=ConnectionError("Queue unavailable")):
			with patch.object(type(frappe.db.after_commit), "add", side_effect=lambda fn: fn()):
				checkin.after_check_in(self.res)
		ticket = frappe.db.get_value("Door Key Audit", {"reservation": self.res.name,
			"property": self.prop, "operation": "issue", "creation": (">=", self.res.creation)},
			["name", "status"], as_dict=True)
		self.assertEqual(ticket.status, "Retrying")
		with patch("frappe.enqueue") as enqueue:
			checkin.reconcile_arrival_keys()
		job = next(c.kwargs for c in enqueue.call_args_list if c.kwargs["ticket"] == ticket.name)
		self.assertEqual((job["reservation"], job["count"]), (self.res.name, 2))
		self.adapter.issue_key = Mock(return_value=KeyResult(True, self.adapter.key))
		for _ in range(2):
			checkin.dispatch_arrival_keys(self.res.name, ticket.name, job["count"])
		self.adapter.issue_key.assert_called_once()
		self.assertEqual(frappe.db.get_value("Door Key Audit", ticket.name, "status"), "Completed")

	def test_arrival_ignores_failed_mobile_attempt(self):
		ticket = checkin._audit_key(checkin._key_request(self.res, self.old, 1, "issue"), "simulator", "Queued", "Administrator")
		checkin._audit_key(checkin._key_request(self.res, self.old, 1, "mobile"), "simulator", "Failed", "Administrator")
		self.adapter.issue_key = Mock(return_value=KeyResult(True, "simulator"))
		checkin.dispatch_arrival_keys(self.res.name, ticket.name, 1)
		self.adapter.issue_key.assert_called_once()

	def test_day_use_keys_expire_at_booked_departure(self):
		self.res.is_day_use = 1
		self.res.planned_check_out_time = "16:00:00"
		req = checkin._key_request(self.res, self.old, 1, "issue")
		self.assertTrue(req.valid_to.endswith("16:00:00"), req.valid_to)

	def test_issue_keys_locks_before_reading_stay(self):
		seen = []
		real = frappe.get_doc
		def observe(*args, **kw):
			if args[:2] == ("Reservation", self.res.name):
				seen.append(kw.get("for_update"))
			return real(*args, **kw)
		with patch("frappe.get_doc", side_effect=observe), patch.object(checkin, "checkin_flow", return_value={}):
			checkin.issue_keys(self.res.name)
		self.assertTrue(seen[0])

	def test_rekey_aborts_when_old_credentials_cannot_be_revoked(self):
		self.failed_revocation()
		self.adapter.issue_key = Mock(return_value=KeyResult(True, "simulator"))
		self.assertFalse(checkin.key_operation(self.res.name, "rekey")["ok"])
		self.adapter.issue_key.assert_not_called()

	def test_vendor_observes_committed_pending_record(self):
		observed = []
		def encoder(req):
			conn = frappe.db.get_connection()
			try:
				with conn.cursor() as cursor:
					cursor.execute("SELECT status FROM `tabDoor Key Audit` WHERE name=%s",
					               (req.options["request_id"],))
					observed.append(cursor.fetchone())
			finally:
				conn.close()
			return KeyResult(True, "simulator")
		self.adapter.issue_key = encoder
		checkin.key_operation(self.res.name, "issue")
		self.assertEqual(observed, [("Pending",)])

	def test_reverse_checkin_records_revocation(self):
		api._revoke_stay_keys(self.res, [self.old])
		rows = checkin.door_key_audit(self.res.name)
		self.assertTrue(any(row.operation == "reverse_checkin_revoke" and row.room == self.old for row in rows))

	def test_hook_adapter_appears_and_can_be_saved(self):
		spec = settings.SETTINGS["door_lock_adapter"][1]
		with patch("kamra.door_lock.registered_adapters", return_value={"partner": SimulatorAdapter}):
			self.assertIn("partner", settings._spec_json(spec)["options"])
			self.assertEqual(settings._validate(spec, "partner"), "partner")

	def test_settings_refuse_endpoint_secrets_before_storage(self):
		for url in ("https://a:b@example.invalid", "https://example.invalid/?token=hidden", "https://example.invalid/#hidden"):
			with self.subTest(url=url), self.assertRaises(frappe.ValidationError) as raised:
				settings.save_settings(self.prop, "front-desk", {"door_lock_endpoint": url})
			self.assertNotIn(url, str(raised.exception))
			self.assertFalse(settings.get_setting(self.prop, "door_lock_endpoint"))

	def test_dated_holdback_reaches_distribution(self):
		allocation.set_sell_limit(self.prop, self.rt, TODAY, TODAY, "-1")
		self.assertEqual(channel_manager._sellable(self.prop, self.rt, TODAY, d(1)), 0)

	def test_unassigned_composite_can_be_sold_once(self):
		suite = make_room_type(self.prop, "FAM")
		for room in (self.old, self.new):
			for parent, in frappe.db.sql("SELECT parent FROM `tabSellable Unit Room` WHERE room=%s", room):
				frappe.delete_doc("Sellable Unit", parent, ignore_permissions=True)
		frappe.get_doc({"doctype":"Sellable Unit", "property":self.prop, "unit_name":"UT Family",
		                "unit_kind":"composite", "room_type":suite, "is_active":1,
		                "physical_rooms":[{"room":self.old},{"room":self.new}]}).insert()
		make_reservation(self.prop, suite, d(3), d(4))
		with self.assertRaises(frappe.ValidationError):
			make_reservation(self.prop, suite, d(3), d(4))

	def test_walk_letter_preserves_relocation_reason(self):
		res = make_reservation(self.prop, self.rt, d(3), d(4))
		api.walk_reservation(res.name, "Alternative Hotel", "Property closure")
		self.assertEqual(api.cancellation_letter(res.name)["reservation"]["cancellation_reason"], "Property closure")
