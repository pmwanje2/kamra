"""Night-specific billing and assignment evidence after successive room moves."""
from decimal import Decimal
from unittest.mock import patch

import frappe

from kamra import api, folio, pricing
from kamra.tests import test_r10_room_move_posting as r10
from kamra.tests.audit_fixtures import AuditTestCase, TODAY, d


class TestR11Billing(AuditTestCase):
	start = r10.TestR10Posting.start
	package = r10.TestR10Posting.package
	book = r10.TestR10Posting.book
	move = r10.TestR10Posting.move
	bill = r10.TestR10Posting.bill

	def mixed_package(self, currency, price, total, room_net, room_tax, element_tax):
		self.start(currency, inclusive=1)
		pkg = self.package("R11 Transfer", self.types[1], price)
		element = pkg.elements[0]
		element.included_in_rate, element.gst_rate, element.unit_allowance = 1, 5, 50
		pkg.save()
		self.book()
		self.move(1)
		self.move(2)
		api.amend_stay(self.stay.name, TODAY, d(4), confirm_price=1)
		self.stay.reload()
		for day in (TODAY, d(1), d(2), d(3)):
			folio.post_room_night(self.stay, day)
		bill = self.bill()
		lines = {r.charge_type: r for r in bill.charges if str(r.posting_date) == d(1)}
		amounts = (self.stay.amount_after_tax, bill.grand_total, lines["Room"].amount,
		           lines["Room"].gst_amount, lines["Taxi"].amount, lines["Taxi"].gst_amount)
		self.assertEqual(tuple(Decimal(str(v)) for v in amounts),
		                 tuple(map(Decimal, (total, total, room_net, room_tax, "50", element_tax))))

	def test_mixed_tax_ugx(self):
		self.mixed_package("UGX", 200, "500", "125", "22", "3")

	def test_mixed_tax_usd(self):
		self.mixed_package("USD", 200.01, "500.01", "125.01", "22.50", "2.50")

	def test_mixed_tax_kwd(self):
		self.mixed_package("KWD", 200.001, "500.001", "125.001", "22.500", "2.500")

	def test_repost_reuses_legacy_consumed_allowance(self):
		self.start()
		pkg = self.package("R11 Transfer")
		self.book()
		folio.post_room_night(self.stay, TODAY)
		bill = self.bill()
		transfer = next(r for r in bill.charges if r.charge_type == "Taxi")
		scope = {"reservation": self.stay.name, "package_element": pkg.elements[0].name}
		doctype = "Reservation Package Allowance"
		allowance = frappe.db.get_value(doctype, scope, "name")
		legacy_key = f"pkg:{self.stay.name}:{pkg.elements[0].name}:{TODAY}"
		frappe.db.set_value(doctype, allowance, {"idempotency_key": legacy_key, "consumed_amount": 7})
		frappe.db.set_value("Folio Charge", transfer.name, "idempotency_key", legacy_key)
		folio.void_charge(bill.name, transfer.name, "Correct transfer")
		folio.post_room_night(self.stay, TODAY)
		self.assertEqual(frappe.db.count(doctype, scope), 1)
		self.assertEqual(frappe.db.get_value(doctype, allowance, "consumed_amount"), 7)
		self.assertEqual([r.idempotency_key for r in self.bill().charges
		                  if r.charge_type == "Taxi" and not r.reversal_of], [legacy_key, legacy_key])

	def test_unlocked_elapsed_night_needs_evidence(self):
		stay = frappe._dict(name="R11 sparse", room_type="C", check_in_date=TODAY,
		                    check_out_date=d(3), room_move_rate_cutoff=d(2),
		                    night_rates=[frappe._dict(stay_date=d(2))])
		with patch.object(frappe, "get_all", return_value=[]):
			with self.assertRaisesRegex(frappe.ValidationError, "ambiguous"):
				pricing.billing_room_type(stay, TODAY)
			stay.room_move_night_types = {TODAY: "A"}
			self.assertEqual(pricing.billing_room_type(stay, TODAY), "A")
			self.assertEqual(pricing.billing_room_type(stay, d(2)), "C")

	def test_partial_move_log_discloses_only_supported_assignments(self):
		self.start()
		self.book()
		self.move(1)
		self.move(2)
		logs = frappe.get_all("Agent Action Log", filters={"reference_name": self.stay.name,
		                      "action_type": "room_move"}, pluck="name", order_by="creation asc, name asc")
		self.assertEqual(len(logs), 2)
		frappe.db.delete("Agent Action Log", {"name": logs[0]})
		self.assertEqual(folio._night_room_description(self.stay, TODAY), "Room")
		self.assertIn("Room 202", folio._night_room_description(self.stay, d(1)))
		self.assertIn("Room 303", folio._night_room_description(self.stay, d(2)))
