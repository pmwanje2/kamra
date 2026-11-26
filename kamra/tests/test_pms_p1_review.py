"""Desk, folio, and housekeeping regressions from the first feature review."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.utils.logger import sanitized_dict

from kamra import api, authz, business_date, checkin, day_use, folio, housekeeping, pricing
from kamra.tests.audit_fixtures import (
	AuditTestCase, PROP_A, TODAY, d, ensure_user, make_property,
	make_reservation, make_room, make_room_type,
)


class TestPmsP1Review(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.api", "kamra.folio", "kamra.business_date")
		make_property(PROP_A, business_date=TODAY, currency="KWD")
		self.standard = make_room_type(PROP_A, "STD", base_price=100)
		self.suite = make_room_type(PROP_A, "STE", base_price=160)
		self.source = make_room(PROP_A, self.standard, "101")
		self.same_type = make_room(PROP_A, self.standard, "102")
		self.destination = make_room(PROP_A, self.suite, "201")

	def stay(self, status="Confirmed"):
		return make_reservation(PROP_A, self.standard, TODAY, d(2), status=status,
		                        room=self.source, auto_price=1)

	def configure_fee(self, approval=0):
		frappe.db.set_value("Property", PROP_A, {
			"early_checkin_cutoff": "08:00:00", "early_checkin_fee_type": "Fixed",
			"early_checkin_fee_value": 7.125, "checkin_time": "14:00:00",
			"early_checkin_waiver_approval": approval,
		})
		return patch.object(checkin, "property_local_now", return_value=datetime(2031, 3, 12, 10))

	def test_fee_eligible_desk_transition_requires_governed_check_in(self):
		stay = self.stay()
		with self.configure_fee():
			stay.status = "Checked In"
			with self.assertRaisesRegex(frappe.ValidationError, "guided check-in"):
				stay.save()
			api.check_in(stay.name)
		self.assertTrue(frappe.db.exists("Folio Charge", {
			"reservation": stay.name, "idempotency_key": f"early-checkin:{stay.name}",
		}))

	def test_desk_cannot_move_an_in_house_guest(self):
		stay = self.stay("Checked In")
		stay.room = self.same_type
		with self.assertRaisesRegex(frappe.ValidationError, "Move Room"):
			stay.save()
		api.move_room(stay.name, self.same_type)
		self.assertEqual(frappe.db.get_value("Room", self.source, "occupancy_status"), "Vacant")
		self.assertEqual(frappe.db.get_value("Room", self.same_type, "occupancy_status"), "Occupied")

	def test_supervisors_cannot_approve_their_own_waiver_or_upgrade(self):
		stay = self.stay()
		manager = ensure_user("pms.review.manager@test.local", ["Hotel Admin"])
		authz._save_cashier_pin_hash(manager, authz.hash_cashier_pin("4067"))
		frappe.db.set_value("Property", PROP_A, "complimentary_upgrade_approval", 1)
		frappe.set_user(manager)  # nosemgrep: frappe-setuser -- test persona
		with self.configure_fee(approval=1), self.subTest(action="waiver"):
			with self.assertRaisesRegex(frappe.PermissionError, "SUPERVISOR_SELF_APPROVAL"):
				api.check_in(stay.name, waive_early_checkin_reason="Recovery",
				             supervisor_user=manager, supervisor_pin="4067")
		with self.subTest(action="upgrade"):
			with self.assertRaisesRegex(frappe.PermissionError, "SUPERVISOR_SELF_APPROVAL"):
				api.move_reservation(stay.name, self.destination, reason="Recovery",
				                     upgrade_type="Complimentary", supervisor_user=manager, supervisor_pin="4067")

	def test_rpc_hooks_only_consume_masked_wire_pin(self):
		stay = self.stay()
		manager = ensure_user("pms.review.approver@test.local", ["Hotel Admin"])
		frappe.db.set_value("Property", PROP_A, "complimentary_upgrade_approval", 1)
		methods = {
			"check_in": {"waive_early_checkin_reason": "Recovery"},
			"move_room": {"new_room": self.destination, "upgrade_type": "Complimentary", "reason": "Recovery"},
			"move_reservation": {"new_room": self.destination, "upgrade_type": "Complimentary", "reason": "Recovery"},
		}
		for method, fields in methods.items():
			for key in ("pin_secret", "supervisor_pin"):
				with self.subTest(method=method, key=key):
					seen = []

					def approve(_property, _manager, pin, **_context):
						seen.append(str(pin.reveal()) if pin else None)
						frappe.throw("Approval probe", frappe.ValidationError)

					request = SimpleNamespace(path=f"/api/method/kamra.api.{method}", method="POST")
					form = frappe._dict(reservation=stay.name, supervisor_user=manager, **fields, **{key: "4067"})
					self.assertEqual(sanitized_dict(form)[key], "********" if key == "pin_secret" else "4067")
					with patch.object(frappe.local, "request", request, create=True), \
							patch.object(frappe.local, "form_dict", form), \
							patch.object(authz, "verify_supervisor", side_effect=approve), self.configure_fee(approval=1):
						authz.prepare_pin_request()
						self.assertNotIn(key, form)
						with self.assertRaisesRegex(frappe.ValidationError, "Approval probe"):
							getattr(api, method)(**form)
					self.assertEqual(seen, ["4067" if key == "pin_secret" else None])

	def test_complimentary_legacy_night_and_full_day_overstay_bill_the_booked_type(self):
		stay = self.stay("Checked In")
		api.move_room(stay.name, self.destination, upgrade_type="Complimentary", reason="Recovery")
		stay.reload()
		stay.set("night_rates", [])
		with self.subTest(rate="legacy"):
			self.assertEqual(folio._nightly_room_rate(stay, TODAY), 100)
		stay.is_day_use = 1
		with patch.object(day_use, "plan_billing", return_value={"billing_basis": "Hourly", "min_hours": 1, "block_hours": 3}), \
				patch.object(day_use, "reservation_slot_hours", return_value=(6, 4)), \
				patch.object(day_use, "overstay_rule", return_value=day_use.OVERSTAY_FULL_DAY):
			with self.subTest(rate="full day overstay"):
				self.assertEqual(folio._day_use_room_charge(stay, TODAY)[0], 100)

	def test_legacy_destination_move_resolves_contract_or_refuses_missing_tariff(self):
		stay = self.stay()
		frappe.db.set_value("Reservation", stay.name, "contract_tariff",
		                   frappe.as_json({"tariff": {"currency": "KWD"}, "agreed_rate": 100}))
		with patch.object(pricing, "contract_rate", return_value=None) as resolver:
			with self.subTest(path="preview"):
				with self.assertRaisesRegex(frappe.ValidationError, "no rate in this contract"):
					api.room_move_preview(stay.name, self.destination)
				self.assertEqual(resolver.call_args.args[1], self.suite)
			with self.subTest(path="save"):
				with self.assertRaisesRegex(frappe.ValidationError, "no rate in this contract"):
					api.move_reservation(stay.name, self.destination, confirm_price=1)
				self.assertEqual(frappe.db.get_value("Reservation", stay.name, "room"), self.source)

	def test_legacy_destination_move_keeps_resolved_tariff_in_preview_and_save(self):
		stay = self.stay()
		tariff = {"currency": "KWD", "fx_rate": 1, "fx_date": TODAY, "agreed_rate": 175, "nightly": 175}
		with patch.object(pricing, "contract_rate", return_value=(175, tariff)):
			preview = api.room_move_preview(stay.name, self.destination)
			self.assertEqual([row["rate"] for row in preview["nights"]], [175, 175])
			api.move_reservation(stay.name, self.destination, confirm_price=1)
		stay.reload()
		self.assertEqual(stay.agreed_rate, 175)
		self.assertEqual(pricing.saved_contract_tariff(stay)["tariff"], tariff)

	def test_ready_room_is_movable_and_occupied_peers_provide_swap_stay(self):
		stay = self.stay("Checked In")
		frappe.db.set_value("Room", self.same_type, "housekeeping_status", "Ready")
		with self.subTest(target="ready vacancy"):
			self.assertTrue(next(row for row in api.movable_rooms(stay.name) if row["name"] == self.same_type)["free"])
			api.room_move_preview(stay.name, self.same_type, upgrade_type="None")
		peer = make_reservation(PROP_A, self.standard, TODAY, d(2), status="Checked In", room=self.same_type)
		with self.subTest(target="ready swap"):
			row = next(row for row in api.movable_rooms(stay.name) if row["name"] == self.same_type)
			self.assertFalse(row["free"])
			self.assertEqual(row.get("swap_reservation"), peer.name)
			api.move_room(stay.name, self.same_type, swap_reservation=peer.name, reason="Same type")
			self.assertEqual(frappe.db.get_value("Reservation", peer.name, "room"), self.source)

	def task(self, room=None, kind="Checkout Clean"):
		return frappe.get_doc(doctype="Housekeeping Task", property=PROP_A,
		                      room=room or self.source, task_type=kind).insert(ignore_permissions=True)

	def attendant(self):
		user = ensure_user("pms.review.attendant@test.local", ["Housekeeping"])
		frappe.db.delete("User Permission", {"user": user, "allow": "Property"})
		frappe.get_doc(doctype="User Permission", user=user, allow="Property",
		               for_value=PROP_A).insert(ignore_permissions=True)
		frappe.clear_cache(user=user)
		return user

	def test_manual_assignment_and_claim_consume_credits_with_legacy_fallback(self):
		assigned, claimed, pending = self.task(), self.task(self.same_type), self.task(self.destination)
		attendant = self.attendant()
		api.hk_assign_task(assigned.name, attendant)
		with authz.act_as(attendant):
			api.hk_claim_task(claimed.name)
		for task in (assigned, claimed):
			with self.subTest(action=task.name):
				self.assertEqual(frappe.db.get_value("Housekeeping Task", task.name, "assignment_credits"), 2)
		result = housekeeping.auto_assign_housekeeping_tasks(PROP_A, TODAY, [attendant], 4)
		self.assertEqual(result["loads"][0]["credits"], 4)
		self.assertEqual([row["task"] for row in result["unassigned"]], [pending.name])

	def test_legacy_zero_assignment_field_uses_current_credit_weight(self):
		legacy, pending = self.task(), self.task(self.same_type)
		attendant = self.attendant()
		frappe.db.set_value("Housekeeping Task", legacy.name, {
			"assigned_to_user": attendant, "assignment_credits": 0,
		})
		result = housekeeping.auto_assign_housekeeping_tasks(PROP_A, TODAY, [attendant], 3)
		self.assertEqual(result["loads"][0]["credits"], 2)
		self.assertEqual([row["task"] for row in result["unassigned"]], [pending.name])

	def test_explicit_zero_credit_is_preserved_after_room_type_weight_changes(self):
		task = self.task()
		attendant = self.attendant()
		frappe.db.set_value("Room Type", self.standard, "hk_departure_credits", 0)
		api.hk_assign_task(task.name, attendant)
		frappe.db.set_value("Room Type", self.standard, "hk_departure_credits", 2)
		result = housekeeping.auto_assign_housekeeping_tasks(PROP_A, TODAY, [attendant], 1)
		self.assertEqual(result["loads"][0]["credits"], 0)

	def test_blocked_turndown_is_retired_and_cannot_be_requested_again(self):
		self.stay("Checked In")
		attendant = self.attendant()
		for status in ("Out of Order", "Out of Service"):
			with self.subTest(status=status):
				api.set_housekeeping_status(self.source, "Clean")
				housekeeping.set_turndown_status(self.source, "Pending")
				task = frappe.db.get_value("Housekeeping Task", {"room": self.source, "task_type": "Turndown"})
				api.set_housekeeping_status(self.source, status, reason="Service interruption")
				with self.assertRaises(frappe.ValidationError):
					housekeeping.set_turndown_status(self.source, "Pending")
				self.assertEqual(frappe.db.get_value("Housekeeping Task", task, "status"), "Cancelled")
				self.assertFalse(housekeeping.auto_assign_housekeeping_tasks(PROP_A, TODAY, [attendant], 3)["assigned"])
				with self.assertRaises(frappe.ValidationError):
					api.hk_assign_task(task, attendant)

	def test_expired_turndown_is_excluded_from_queue_and_assignment(self):
		self.stay("Checked In")
		attendant = self.attendant()
		housekeeping.set_turndown_status(self.source, "Pending")
		task = frappe.db.get_value("Housekeeping Task", {"room": self.source, "task_type": "Turndown"})
		business_date.advance_business_date(PROP_A, TODAY)
		with self.subTest(path="queue"):
			self.assertNotIn(task, [row.name for row in api.hk_queue(PROP_A)["tasks"]])
		with self.subTest(path="assignment"):
			self.assertFalse(housekeeping.auto_assign_housekeeping_tasks(PROP_A, TODAY, [attendant], 3)["assigned"])
			with self.assertRaises(frappe.ValidationError):
				api.hk_assign_task(task, attendant)

	def test_dnd_cancels_same_day_turndown(self):
		self.stay("Checked In")
		housekeeping.set_turndown_status(self.source, "Pending")
		housekeeping.set_turndown_status(self.source, "DND")
		self.assertEqual(housekeeping.get_turndown_queue(PROP_A)[0]["turndown_status"], "DND")
		self.assertEqual(frappe.db.get_value("Housekeeping Task", {"room": self.source, "task_type": "Turndown"},
		                                    "status"), "Cancelled")
