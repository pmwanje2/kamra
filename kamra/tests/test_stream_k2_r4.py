"""Live encoder journal and Redis delivery regressions."""
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from kamra import api, checkin, settings
from kamra.door_lock import EdgeAdapter, ManualAdapter
from kamra.tests.audit_fixtures import (
	TODAY, d, make_property, make_reservation, make_room, make_room_type,
)


class EncoderRecoveryTests(IntegrationTestCase):
	properties = ("UT K2 Encoder Recovery",)

	def setUp(self):
		super().setUp()
		# Workers journal on independent connections. End this test transaction
		# after tearDown so locks retained by a savepoint rollback cannot
		# interfere with the next test's independently committed writes.
		self.addCleanup(frappe.db.rollback)
		self.prop = make_property(self.properties[0] + " " + frappe.generate_hash(length=8), business_date=TODAY)
		self.rt = make_room_type(self.prop)
		self.old = make_room(self.prop, self.rt, "101")
		self.new = make_room(self.prop, self.rt, "102")
		self.res = make_reservation(self.prop, self.rt, TODAY, d(1), status="Checked In", room=self.old)

	def configure_bridge(self, endpoint):
		from frappe.utils.password import set_encrypted_password
		settings.write_kv(self.prop, "door_lock_endpoint", endpoint)
		settings.clear_cache(self.prop)
		set_encrypted_password("Property", self.prop, frappe.generate_hash(), fieldname="door_lock_secret")
		self.enterContext(patch("kamra.door_lock.get_adapter", return_value=EdgeAdapter()))

	def test_lost_encoder_ack_retains_issuer_on_move(self):
		self.configure_bridge("https://locks.example/issuer-a")
		issued = []

		def lose_ack(url, payload, secret, **kwargs):
			issued.append((url, payload["room"]))
			raise TimeoutError("acknowledgement lost")

		with patch("kamra.bridge_transport.post_json", side_effect=lose_ack):
			self.assertFalse(checkin.key_operation(self.res.name, "issue")["ok"])
		self.assertEqual(issued, [("https://locks.example/issuer-a", self.old)])
		with patch("kamra.door_lock.get_adapter", return_value=ManualAdapter()):
			moved = api.move_room(self.res.name, self.new)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- exercise the real transaction boundary in the integration fixture
		self.assertEqual([w["room"] for w in moved["pending_revocations"]], [self.old])

	def test_endpoint_rotation_retains_original_target_for_revocation(self):
		first = "https://locks.example/issuer-a"
		second = "https://locks.example/issuer-b"
		self.configure_bridge(first)
		with patch("kamra.bridge_transport.post_json", return_value=200) as transport:
			self.assertTrue(checkin.key_operation(self.res.name, "issue")["ok"])
			settings.write_kv(self.prop, "door_lock_endpoint", second)
			settings.clear_cache(self.prop)
			moved = api.move_room(self.res.name, self.new)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- exercise the real transaction boundary in the integration fixture
			self.assertEqual([w["room"] for w in moved["pending_revocations"]], [self.old])
			warning, = moved["pending_revocations"]
			self.assertEqual(transport.call_count, 1)
			settings.write_kv(self.prop, "door_lock_endpoint", first)
			settings.clear_cache(self.prop)
			self.assertTrue(checkin.retry_revocation(self.res.name, warning["operation_id"])["ok"])
			self.assertEqual([(c.args[0], c.args[1]["operation"]) for c in transport.call_args_list],
			                 [(first, "issue"), (first, "revoke")])

	def test_reconciliation_replaces_orphaned_queued_job_in_redis(self):
		from frappe.utils import add_to_date, now_datetime
		from frappe.utils.background_jobs import get_queue
		queue = get_queue("short")
		ticket = checkin._audit_key(checkin._key_request(self.res, self.old, 1, "issue"),
		                            "fias_edge", "Queued", "Administrator")
		job = frappe.enqueue("kamra.checkin.dispatch_arrival_keys", queue="short",
			job_id=f"arrival-key-{ticket.name}", reservation=self.res.name, ticket=ticket.name, count=1)
		self.addCleanup(job.delete)
		self.assertEqual(queue.connection.lrem(queue.key, 0, job.id), 1)
		self.assertEqual(job.get_status(), "queued")
		frappe.db.set_value("Door Key Audit", ticket.name, "modified", add_to_date(now_datetime(), minutes=-10),
		                    update_modified=False)
		checkin.reconcile_arrival_keys()
		deliveries = [j for j in queue.jobs if j.kwargs.get("kwargs", {}).get("ticket") == ticket.name]
		for delivery in deliveries:
			self.addCleanup(delivery.delete)
		self.assertEqual(len(deliveries), 1)
		self.assertNotEqual(deliveries[0].id, job.id)
