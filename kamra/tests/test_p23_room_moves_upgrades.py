"""P23 room moves and the check-in fee through the live desk endpoints."""

from datetime import datetime
from unittest.mock import patch

import frappe

from kamra import api, checkin
from kamra.tests.audit_fixtures import (
	PROP_A, PROP_B, TODAY, AuditTestCase, d, ensure_user, make_property,
	make_reservation, make_room, make_room_type,
)


class TestP23RoomMovesUpgrades(AuditTestCase):
	properties = (PROP_A, PROP_B)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.api", "kamra.folio", "kamra.business_date")
		make_property(PROP_A, business_date=TODAY, currency="KWD")
		make_property(PROP_B, business_date=TODAY)
		self.standard = make_room_type(PROP_A, "STD", base_price=100)
		self.suite = make_room_type(PROP_A, "STE", base_price=160)
		self.source = make_room(PROP_A, self.standard, "101")
		self.destination = make_room(PROP_A, self.suite, "201")
		self.foreign = make_room(PROP_B, make_room_type(PROP_B), "301")

	def test_complimentary_move_preserves_boarded_rate_and_audits(self):
		stay = make_reservation(PROP_A, self.standard, TODAY, d(2), status="Checked In",
		                        room=self.source, auto_price=1)
		amount = stay.amount_after_tax
		with self.assertRaises(frappe.ValidationError):
			api.move_room(stay.name, self.destination, upgrade_type="Complimentary")
		result = api.move_room(stay.name, self.destination, upgrade_type="Complimentary",
		                       reason="Guest recovery")
		stay.reload()
		self.assertEqual(stay.original_room_type, self.standard)
		self.assertEqual(stay.upgrade_type, "Complimentary")
		self.assertEqual(stay.upgrade_reason, "Guest recovery")
		self.assertEqual(stay.amount_after_tax, amount)
		self.assertEqual(result["room_type_to_charge"], self.standard)
		self.assertEqual(frappe.db.get_value("Room", self.source, "housekeeping_status"), "Dirty")
		self.assertEqual(frappe.db.get_value("Room", self.destination, "occupancy_status"), "Occupied")

	def test_paid_move_uses_server_rates_and_checks_property_and_readiness(self):
		stay = make_reservation(PROP_A, self.standard, TODAY, d(2), status="Checked In",
		                        room=self.source, auto_price=1)
		user = ensure_user("p23.desk@test.local", ["Front Desk"])
		frappe.db.delete("User Permission", {"user": user, "allow": "Property"})
		frappe.get_doc(doctype="User Permission", user=user, allow="Property",
		               for_value=PROP_A).insert(ignore_permissions=True)
		frappe.clear_cache(user=user)
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			api.room_move_preview(stay.name, self.foreign, upgrade_type="Paid")
		with self.assertRaises(frappe.PermissionError):
			api.move_room(stay.name, self.foreign, upgrade_type="Paid")
		frappe.db.set_value("Room", self.destination, "housekeeping_status", "Dirty")
		with self.assertRaises(frappe.ValidationError):
			api.move_room(stay.name, self.destination, upgrade_type="Paid", confirm_price=1)
		frappe.db.set_value("Room", self.destination, "housekeeping_status", "Clean")
		preview = api.room_move_preview(stay.name, self.destination, upgrade_type="Paid")
		self.assertEqual(preview["places"], 3)
		self.assertGreater(preview["difference"], 0)
		api.move_room(stay.name, self.destination, upgrade_type="Paid", confirm_price=1)
		stay.reload()
		self.assertEqual(stay.upgrade_type, "Paid")
		self.assertEqual(stay.original_room_type, self.standard)
		self.assertAlmostEqual(stay.amount_after_tax, preview["new_amount"], places=3)

	def test_early_fee_is_previewed_and_posted_once(self):
		frappe.db.set_value("Property", PROP_A, {
			"early_checkin_cutoff": "08:00:00", "early_checkin_fee_type": "Fixed",
			"early_checkin_fee_value": 7.125, "checkin_time": "14:00:00",
		})
		stay = make_reservation(PROP_A, self.standard, TODAY, d(1), room=self.source)
		with patch.object(checkin, "property_local_now", return_value=datetime(2031, 3, 12, 10)):
			self.assertEqual(checkin.early_checkin_fee_preview(stay.name)["amount"], 7.125)
			api.check_in(stay.name)
			api.check_in(stay.name)
		folio = frappe.get_doc("Folio", {"reservation": stay.name, "folio_type": "Guest"})
		fees = [row for row in folio.charges if row.description == "Early Check-In Fee"]
		self.assertEqual(len(fees), 1)
		self.assertAlmostEqual(fees[0].amount, 7.125, places=3)
