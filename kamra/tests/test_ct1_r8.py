"""Exercise an upgrade from already-logged advance repairs on populated folios."""
import frappe
from frappe.modules import patch_handler

from kamra import api, folio, settings
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, make_property, make_reservation, make_room_type, post_legacy_advance,
)


class UpgradeAdvanceRepair(AuditTestCase):
	def setUp(self):
		super().setUp()
		self.prop = make_property("R8 Hotel", currency="USD", business_date=TODAY)
		settings.write_kv(self.prop, "use_cashier_tills", 0)
		settings.clear_cache(self.prop)
		self.room_type = make_room_type(self.prop)

	def booking(self):
		stay = make_reservation(self.prop, self.room_type, d(1), d(3))
		return stay, folio.open_folio(stay)

	def test_saved_guest_allocation_recovers_real_deposit_and_keeps_refunds(self):
		stay, name = self.booking()
		guest = api.add_folio_payment(name, "Cash", 100)["payments"][-1]["name"]
		deposit = post_legacy_advance(frappe.get_doc("Folio", name), 100)
		typed = api.add_folio_payment(name, "Cash", 25, kind="Advance")["payments"][-1]["name"]
		api.refund_folio_payment(name, payment_row=typed, reason="Return typed deposit")
		bad_basis = frappe.as_json({"receipts": {guest: "100"}, "deposit_returns": "0", "unallocated": {}})
		for previous_counter in (100, 0):
			with self.subTest(previous_counter=previous_counter):
				frappe.db.set_value("Reservation", stay.name, {
					"advance_paid": previous_counter, "legacy_advance_basis": bad_basis,
				})
				folio.reconcile_reservation_advance(stay.name, self.prop)
				self.assertEqual(stay.reload().advance_paid, 100)
		api.refund_folio_payment(name, amount=40, payment_row=deposit, reason="Partial legacy refund")
		# The upgrade may first encounter the invalid allocation after a linked refund.
		frappe.db.set_value("Reservation", stay.name, {"advance_paid": 100, "legacy_advance_basis": bad_basis})
		for _ in range(2):
			folio.reconcile_reservation_advance(stay.name, self.prop)
			self.assertEqual(stay.reload().advance_paid, 60)

	def test_post_model_sync_repairs_positive_and_zero_counters_after_73(self):
		overstated, first = self.booking()
		post_legacy_advance(frappe.get_doc("Folio", first), 100)
		missing, second = self.booking()
		api.add_folio_payment(second, "Cash", 25, kind="Advance")
		frappe.db.set_value("Reservation", overstated.name, "advance_paid", 200)
		frappe.db.set_value("Reservation", missing.name, "advance_paid", 0)

		old = "kamra.patches.v48.reconcile_refunded_advances"
		for patch in (old, old + " # Reconcile linked refunds including transfers between windows"):
			if not patch_handler.executed(patch):
				patch_handler.update_patch_log(patch)
		repair = "kamra.patches.v48.reconcile_advances_r8"
		frappe.db.delete("Patch Log", {"patch": repair})
		for _ in range(2):
			patch_handler.run_all(patch_type=patch_handler.PatchType.post_model_sync)
			self.assertEqual([overstated.reload().advance_paid, missing.reload().advance_paid], [100, 25])
			self.assertEqual(frappe.db.count("Patch Log", {"patch": repair, "skipped": 0}), 1)
