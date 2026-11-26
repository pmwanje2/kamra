"""A share break preserves maintenance, and a change of payer preserves the bill."""

from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from kamra import api, folio, ledger, shares
from kamra.tests import test_p2_share_reservations_r11 as r11
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, guest_folio, make_guest, make_property, make_reservation,
	make_room, make_room_type,
)
from kamra.tests.test_p2_share_reservations_r9 import CASES


class TestOldRoomRestriction(IntegrationTestCase):
	concurrent_change = r11.TestCurrentShareInventory.concurrent_change
	remove_fixture = r11.TestCurrentShareInventory.remove_fixture

	def check_restriction(self, status):
		self.prop = make_property("UT R12 " + frappe.generate_hash(length=8), business_date=TODAY)
		rt = make_room_type(self.prop)
		original = make_room(self.prop, rt, "101")
		destination = make_room(self.prop, rt, "102")
		self.primary = make_reservation(self.prop, rt, TODAY, d(3), room=original)
		self.guest = make_guest("R12 nonarrival")
		self.addCleanup(self.remove_fixture)
		peer = shares.create_share_reservation(self.primary.name, self.guest)["reservation"]
		for name, state in ((peer, "No Show"), (self.primary.name, "Checked In")):
			stay = frappe.get_doc("Reservation", name)
			stay.status = state
			stay.save()
		self.concurrent_change(lambda: frappe.db.set_value("Room", original, "housekeeping_status", status))
		shares.break_share(self.primary.name, destination)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- verify the move seen by the next desk
		self.assertEqual(frappe.db.get_value("Room", original, ["housekeeping_status", "occupancy_status"]),
		                 (status, "Vacant"))

	def test_concurrent_out_of_service(self):
		self.check_restriction("Out of Service")

	def test_concurrent_out_of_order(self):
		self.check_restriction("Out of Order")


class TestContinuingPayer(AuditTestCase):
	properties = tuple(f"UT R12 {code}" for code, *_ in CASES)

	def setUp(self):
		super().setUp()
		self.at(d(2))
		ledger.ensure_default_transaction_codes()

	def check_stay(self, code, prices, percent, scenario, credit=0):
		rate = sum(map(Decimal, prices))
		prop = make_property(f"UT R12 {code}", currency=code, business_date=TODAY, rates_include_tax=1,
		                     room_levy_percent=percent, room_levy_taxable=1)
		rt = make_room_type(prop, base_price=float(rate), tax_percent=10)
		def booking(number):
			return make_reservation(prop, rt, TODAY, d(3), room=make_room(prop, rt, number), adults=1).name
		primary, control = booking("101"), booking("102")
		peers = [shares.create_share_reservation(primary, make_guest("Peer"))["reservation"]]
		if scenario != "upgrade":
			peers.append(shares.create_share_reservation(primary, make_guest("Peer 2"))["reservation"])
		names = [primary, *peers]
		shares.update_share_rate_mode(primary, "Primary Pays" if scenario == "upgrade" else "Full Rate Each")
		for name in [*names, control]:
			stay = frappe.get_doc("Reservation", name)
			stay.status = "Checked In"
			stay.save()
		def due(name):
			return Decimal(str(frappe.db.get_value("Folio", guest_folio(name), "grand_total")))
		with patch("kamra.folio.locked_night", return_value=None):
			for i in range(3):
				if i == 1:
					if credit:
						for name in (primary, control):
							bill = frappe.get_doc("Folio", guest_folio(name))
							bill.append("charges", {"charge_type": "Allowance", "amount": -credit,
							                        "rate": -credit, "qty": 1, "gst_rate": 0, "posting_date": d(i)})
							folio._recalculate(bill)
							ledger.save_folio(bill)
							self.assertEqual(bill.reload().charges[-1].amount, -credit)
					if scenario == "upgrade":
						shares.break_share(peers[0], make_room(prop, rt, "103"))
						self.assertFalse(frappe.db.get_value("Reservation", primary, "share_group_id"))
						price = {"USD": 104.01, "KWD": 10.400, "UGX": 9517}[code]
						suite = make_room_type(prop, "STE", base_price=price, tax_percent=10)
						for name, number in ((primary, "201"), (control, "202")):
							api.move_reservation(name, make_room(prop, suite, number), confirm_price=1)
					elif scenario == "custom":
						shares.update_share_rate_mode(primary, "Custom", {n: float(rate) if n == primary else 0 for n in names})
					else:
						shares.update_share_rate_mode(primary, "Primary Pays")
				if i == 2 and scenario != "upgrade":
					shares.update_share_rate_mode(primary, "Full Rate Each")
				folio.run_night_audit(prop, d(i))
				self.assertTrue(folio.run_night_audit(prop, d(i))["already_ran"])
				self.assertEqual(due(primary), due(control), f"{code} {scenario}, night {i + 1}")
		if scenario == "upgrade" and code == "UGX":
			self.assertEqual(due(control), 28848)

	def run_matrix(self, scenario, credit=0):
		for code, prices, percent, _ in CASES:
			with self.subTest(currency=code):
				self.check_stay(code, prices, percent, scenario, credit)

	def test_full_primary_full(self):
		self.run_matrix("primary")

	def test_full_primary_full_with_credit(self):
		self.run_matrix("primary", credit=1)

	def test_full_custom_full(self):
		self.run_matrix("custom")

	def test_break_and_upgrade(self):
		self.run_matrix("upgrade")

	def test_equal_middle_night_carries_each_payers_fraction(self):
		for code, unit in (("USD", Decimal(".01")), ("UGX", Decimal(1)), ("KWD", Decimal(".001"))):
			with self.subTest(currency=code):
				prop = make_property(f"UT R12 {code}", currency=code, business_date=TODAY)
				rt = make_room_type(prop, base_price=float(1005 * unit), tax_percent=10)
				def book(number):
					return make_reservation(prop, rt, TODAY, d(3), room=make_room(prop, rt, number), adults=1).name
				primary, control = book("101"), book("102")
				peers = [shares.create_share_reservation(primary, make_guest(n))["reservation"] for n in ("B", "C")]
				shares.update_share_rate_mode(primary, "Full Rate Each")
				for name in (primary, *peers, control):
					stay = frappe.get_doc("Reservation", name)
					stay.status = "Checked In"
					stay.save()
				def net(res, date):
					return float(unit * (335 if res.name == control and str(date) == d(1) else 1005))
				with patch("kamra.folio._nightly_room_rate", side_effect=net):
					for day, mode in enumerate(("Full Rate Each", "Split Equal", "Full Rate Each")):
						if day:
							shares.update_share_rate_mode(primary, mode)
						folio.run_night_audit(prop, d(day))
				expected = frappe.db.get_value("Folio", guest_folio(control), "grand_total")
				for name in (primary, *peers):
					self.assertEqual(frappe.db.get_value("Folio", guest_folio(name), "grand_total"), expected)
