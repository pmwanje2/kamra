"""Composite physical inventory and reverse check-in at the HTTP commit boundary."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.test import Client
from werkzeug.wrappers import Response

from kamra import allocation, api, checkin, public_api
from kamra.door_lock import KeyResult, SimulatorAdapter
from kamra.tests.audit_fixtures import TODAY, d, make_property, make_reservation, make_room, make_room_type


class CompositeStayTests(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		self.prop = make_property("UT R7 " + frappe.generate_hash(length=10), business_date=TODAY)
		self.bed = make_room_type(self.prop)
		suite = make_room_type(self.prop, "FAM")
		self.rooms = [make_room(self.prop, self.bed, n) for n in ("101", "102")]
		self.unit = frappe.get_doc({"doctype": "Sellable Unit", "property": self.prop, "room_type": suite,
			"unit_name": "Family", "unit_kind": "composite", "is_active": 1,
			"physical_rooms": [{"room": room} for room in self.rooms]}).insert()
		self.stay = make_reservation(self.prop, suite, TODAY, d(1), sellable_unit=self.unit.name)

	def public_count(self, start, end):
		return next(row["rooms_left"] for row in public_api.search_stay(self.prop, start, end)
		            if row["room_type"] == self.bed)

	def test_dated_overbooking_cannot_reopen_members_of_a_nonvilla_suite(self):
		frappe.db.set_value("Property", self.prop, "house_overbooking_limit", 1)
		allocation.set_sell_limit(self.prop, self.bed, TODAY, d(1), "+1")
		self.assertEqual(self.public_count(TODAY, d(1)), 0)
		self.assertEqual(self.public_count(d(1), d(2)), 3)

	def change_members(self):
		new = make_room(self.prop, self.bed, "103")
		self.unit.physical_rooms[1].room = new
		self.unit.save()
		return new

	def assert_departure(self, reverse=False):
		api.check_in(self.stay.name)
		new = self.change_members()
		new_status = frappe.db.get_value("Room", new, ["occupancy_status", "housekeeping_status"])
		if reverse:
			api.reverse_check_in(self.stay.name, "Wrong arrival")
		else:
			with patch.dict(frappe.flags, allow_unsettled_checkout=True):
				api.check_out(self.stay.name)
		self.assertEqual([frappe.db.get_value("Room", r, ["occupancy_status", "housekeeping_status"])
		                  for r in self.rooms + [new]], [("Vacant", "Dirty"), ("Vacant", "Dirty"), new_status])

	def test_checkout_releases_arrival_members_after_unit_edit(self):
		self.assert_departure()

	def test_reversal_releases_arrival_members_after_unit_edit(self):
		self.assert_departure(reverse=True)

	def test_arrival_and_desk_keys_reach_each_occupied_room(self):
		frappe.db.set_value("Reservation", self.stay.name, "key_count", 1)
		adapter = SimulatorAdapter()
		with patch("kamra.door_lock.get_adapter", return_value=adapter), patch.object(
			adapter, "issue_key", wraps=adapter.issue_key) as send:
			out = api.check_in(self.stay.name)
			with self.subTest(stage="arrival"):
				self.assertTrue((out.get("keys") or {}).get("ok"))
				self.assertCountEqual([call.args[0].room for call in send.call_args_list], self.rooms)
			send.reset_mock()
			self.change_members()
			result = checkin.issue_keys(self.stay.name)["key_result"]
			with self.subTest(stage="desk"):
				self.assertTrue((result or {}).get("ok"))
				self.assertCountEqual([call.args[0].room for call in send.call_args_list], self.rooms)
			rows = checkin.door_key_audit(self.stay.name)
			with self.subTest(stage="audit"):
				self.assertCountEqual([r.room for r in rows if r.operation == "issue" and r.status == "Accepted"],
				                      self.rooms * 2)
			send.side_effect = lambda req: KeyResult(req.room == self.rooms[0], adapter.key, "Desk encoder")
			failed = checkin.issue_keys(self.stay.name)["key_result"]
			self.assertIsNotNone(failed)
			self.assertFalse(failed["ok"])
			self.assertNotEqual(checkin._records(self.stay.reload())["keys"]["state"], "done")

	def test_queued_arrival_uses_one_ticket_per_arrival_member_after_unit_edit(self):
		from kamra.door_lock import EdgeAdapter
		frappe.db.set_value("Reservation", self.stay.name, "key_count", 1)
		edge = EdgeAdapter()
		with patch("kamra.door_lock.get_adapter", return_value=edge), patch("kamra.checkin._enqueue_arrival") as queue, patch.object(
			edge, "issue_key", return_value=KeyResult(True, edge.key)) as issue:
			api.check_in(self.stay.name)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- deliver the durable queue callbacks
			tickets = [c.args[1] for c in queue.call_args_list]
			self.assertEqual(len(tickets), 2)
			self.change_members()
			for ticket in tickets:
				for attempt in range(2):
					checkin.dispatch_arrival_keys(self.stay.name, ticket, 1)
			self.assertCountEqual([c.args[0].room for c in issue.call_args_list], self.rooms)
			self.assertTrue(all(frappe.db.get_value("Door Key Audit", t, "status") == "Completed" for t in tickets))

	def test_reverse_http_reports_committed_result_and_dispatches_nothing_on_rollback(self):
		from frappe.app import application
		from kamra.savings import log_action
		third = make_room(self.prop, self.bed, "103")
		stay = make_reservation(self.prop, self.bed, TODAY, d(1), room=third, status="Checked In")
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- make the stay visible to separate HTTP requests
		site = frappe.local.site

		def post():
			with patch("frappe.app.validate_auth", side_effect=as_test_admin):
				return Client(application, Response).post("/api/method/kamra.api.reverse_check_in",
					base_url="http://" + site, data={"reservation": stay.name, "reason": "Wrong arrival"}, buffered=True)

		def as_test_admin():
			params = frappe.local.form_dict
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- authenticated test request
			frappe.local.form_dict = params

		def refuse_log(action, *args, **kwargs):
			if action == "reverse_check_in":
				raise frappe.ValidationError("Injected pre-commit failure")
			return log_action(action, *args, **kwargs)

		adapter = SimulatorAdapter()
		with ThreadPoolExecutor(1) as worker, patch("kamra.door_lock.get_adapter", return_value=adapter), patch.object(
			adapter, "revoke_request", wraps=adapter.revoke_request) as send:
			with patch("kamra.savings.log_action", side_effect=refuse_log) as log:
				failed = worker.submit(post).result(timeout=45)
			self.assertGreaterEqual(failed.status_code, 400)
			self.assertTrue(any(c.args[0] == "reverse_check_in" for c in log.call_args_list), failed.get_data(as_text=True))
			send.assert_not_called()
			self.assertEqual(frappe.db.get_value("Reservation", stay.name, "status"), "Checked In")
			resp = worker.submit(post).result(timeout=45)
			self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
			send.assert_called_once()
			result = resp.get_json()["message"]["key_result"]
			self.assertEqual((result["ok"], result["message"], result["pending_revocations"]),
			                 (True, "Simulated revocation completed.", []))
