"""Real concurrent inventory changes and continuity of room-night rounding."""

import json
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from kamra import api, ledger, shares
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, guest_folio, make_guest, make_property, make_reservation,
	make_room, make_room_type,
)
from kamra.tests.test_invoice_corrections import _second_connection
from kamra.tests.test_p2_share_reservations_r9 import CASES


class TestCurrentShareInventory(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.prop = make_property("UT R11 " + frappe.generate_hash(length=8), business_date=TODAY,
		                          currency="USD")
		rt = make_room_type(self.prop, base_price=100, tax_percent=0)
		self.primary = make_reservation(self.prop, rt, TODAY, d(3), room=make_room(self.prop, rt, "101"))
		self.room = make_room(self.prop, rt, "102")
		self.guest = make_guest("R11 peer")
		ledger.ensure_default_transaction_codes()
		self.addCleanup(self.remove_fixture)

	def remove_fixture(self):
		frappe.db.rollback()
		for dt in ("Room Block", "Agent Action Log", "Folio Ledger Entry", "Folio", "Reservation",
		           "Sellable Unit", "Room", "Room Type", "Property Setting"):
			for name in frappe.get_all(dt, filters={"property": self.prop}, pluck="name"):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		for dt, name in (("Property", self.prop), ("Guest", self.primary.guest), ("Guest", self.guest)):
			frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- remove committed concurrency fixtures

	def concurrent_change(self, action):
		# Publish the fixture, then let the reader observe the old inventory.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- independent desk needs the fixture
		reader = frappe.db.sql("SELECT CONNECTION_ID(), @@tx_isolation")[0]
		self.assertEqual(reader[1], "REPEATABLE-READ")
		self.assertEqual(frappe.db.get_value("Reservation", self.primary.name, "property"), self.prop)
		other = _second_connection()
		try:
			with patch.object(frappe.local, "db", other):
				self.assertNotEqual(other.sql("SELECT CONNECTION_ID()")[0][0], reader[0])
				result = action()
				other.commit()
		finally:
			other.close()
		return result

	def test_advance_bill_sees_folios_committed_by_first_join(self):
		peer = self.concurrent_change(
			lambda: shares.create_share_reservation(self.primary.name, self.guest))["reservation"]
		first = ledger.force_advance_bill(self.primary.name, "tonight")
		second = ledger.force_advance_bill(peer, "tonight")
		self.assertEqual([first["balance"], second["balance"]], [50, 50])
		self.assertNotEqual(first["folio"], second["folio"])

	def test_break_sees_committed_release(self):
		peer = shares.create_share_reservation(self.primary.name, self.guest)["reservation"]
		block = api.create_room_block(self.prop, self.room, TODAY, d(2), reason="Maintenance")["name"]
		self.concurrent_change(lambda: api.release_room_block(block))
		self.assertEqual(shares.break_share(peer, self.room)["room"], self.room)


class TestShareRoundingContinuity(AuditTestCase):
	properties = tuple(f"UT R11 {code}" for code, *_ in CASES)

	def setUp(self):
		super().setUp()
		self.at(d(1))
		ledger.ensure_default_transaction_codes()

	def check_two_nights(self, scenario):
		for code, amounts, pct, first_due in CASES:
			with self.subTest(currency=code), patch("kamra.folio.locked_night", return_value=None):
				gross = sum(map(Decimal, amounts))
				prop = make_property(f"UT R11 {code}", business_date=TODAY, currency=code,
				                     rates_include_tax=1, room_levy_percent=pct, room_levy_taxable=1)
				rt = make_room_type(prop, base_price=float(gross), tax_percent=10)
				def booking(number):
					return make_reservation(prop, rt, TODAY, d(3), room=make_room(prop, rt, number))
				primary, control = booking("101"), booking("102")
				peer = shares.create_share_reservation(primary.name, make_guest("R11 peer"),
				                                       check_in_date=d(1) if scenario == "later" else TODAY)["reservation"]
				if scenario == "switch":
					shares.update_share_rate_mode(primary.name, "Full Rate Each")
				else:
					shares.update_share_rate_mode(primary.name, "Custom", {primary.name: float(gross), peer: 0})
				if scenario == "later":
					self.assertEqual(ledger.force_advance_bill(peer, "tonight")["nights_posted"], [d(1)])
				else:
					for name in (primary.name, peer):
						ledger.force_advance_bill(name, "tonight")
				legacy = {}
				if scenario == "legacy":
					for row in frappe.get_all("Folio Charge", filters={"share_group": primary.reload().share_group_id,
					                          "charge_type": "Room"}, fields=["name", "share_snapshot"]):
						snapshot = json.loads(row.share_snapshot)
						snapshot.pop("rounding", None)
						legacy[row.name] = json.dumps(snapshot)
						frappe.db.set_value("Folio Charge", row.name, "share_snapshot", legacy[row.name])
					self.assertEqual(len(legacy), 2)
				if scenario == "switch":
					frappe.db.set_value("Property", prop, "business_date", d(1))
					frappe.clear_document_cache("Property", prop)
					shares.update_share_rate_mode(primary.name, "Primary Pays")
				for name in (control.name, primary.name, peer):
					ledger.force_advance_bill(name, "tonight" if scenario == "later" and name == peer else "2")
				before = [self.bill(name) for name in (primary.name, peer, control.name)]
				for name in (primary.name, peer):
					self.assertFalse(ledger.force_advance_bill(name, "tonight" if scenario == "later" and name == peer else "2")["nights_posted"])
				self.assertEqual(before, [self.bill(name) for name in (primary.name, peer, control.name)])
				for line, frozen in legacy.items():
					self.assertEqual(frappe.db.get_value("Folio Charge", line, "share_snapshot"), frozen)
				self.assertEqual(before[2][0], Decimal({"USD": "210.07", "KWD": "21.007", "UGX": "19209"}[code]))
				self.assertEqual(before[0][0], before[2][0])
				if scenario != "switch":
					self.assertEqual(before[1][0], 0)
				elif code == "USD":
					self.assertEqual(before[1][0], Decimal(first_due))

	def bill(self, name):
		bill = frappe.get_doc("Folio", guest_folio(name))
		return Decimal(str(bill.grand_total)), [(c.name, c.amount, c.share_snapshot) for c in bill.charges]

	def test_later_arriving_peer_bills_second_night_first(self):
		self.check_two_nights("later")

	def test_legacy_allocation_without_rounding_state(self):
		self.check_two_nights("legacy")

	def test_full_rate_each_to_primary_pays(self):
		self.check_two_nights("switch")
