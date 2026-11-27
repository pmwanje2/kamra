"""The ledger identifies legacy advances independently of receipt order or saved estimates."""
import frappe

from kamra import api, folio, settings
from kamra.patches.v48.reconcile_refunded_advances import execute
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, make_property, make_reservation, make_room, make_room_type, post_legacy_advance,
)


class LegacyAdvanceEvidence(AuditTestCase):
	def stay(self, currency, status="Confirmed"):
		prop = make_property("R7 " + currency, currency=currency, business_date=TODAY)
		settings.write_kv(prop, "use_cashier_tills", 0)
		settings.clear_cache(prop)
		rt = make_room_type(prop)
		stay = make_reservation(prop, rt, d(0), d(2), status=status, room=make_room(prop, rt, "101"))
		return stay, folio.open_folio(stay)

	def test_guest_settlement_before_refunded_advance_cannot_restore_counter(self):
		for currency, amount in (("UGX", 410619), ("USD", 11.12), ("KWD", 11.123)):
			with self.subTest(currency=currency):
				stay, name = self.stay(currency, "Checked In")
				ordinary = api.add_folio_payment(name, "Cash", amount + 1)["payments"][-1]["name"]
				typed = api.add_folio_payment(name, "Cash", amount, kind="Advance")["payments"][-1]["name"]
				api.refund_folio_payment(name, payment_row=typed, reason="Return advance")
				self.assertEqual(stay.reload().advance_paid, 0)
				# Include the empty JSON string and a basis already misclassified by R6.
				for basis in ("", {"receipts": {ordinary: str(amount)}, "deposit_returns": "0", "unallocated": {}}):
					with self.subTest(saved_basis=bool(basis)):
						frappe.db.set_value("Reservation", stay.name, {"advance_paid": amount,
							"legacy_advance_basis": frappe.as_json(basis)})
						for _ in range(2):
							execute()
							self.assertEqual(stay.reload().advance_paid, 0)

	def test_genuine_untyped_deposit_and_typed_refunds_replay_to_zero(self):
		stay, name = self.stay("USD")
		legacy = post_legacy_advance(frappe.get_doc("Folio", name), 100)
		typed = api.add_folio_payment(name, "Cash", 25, kind="Advance")["payments"][-1]["name"]
		frappe.db.set_value("Reservation", stay.name, {"advance_paid": 125, "legacy_advance_basis": None})
		new = api.add_folio_payment(name, "Cash", 10, kind="Advance")["payments"][-1]["name"]
		self.assertEqual(stay.reload().advance_paid, 135)
		folio.reconcile_reservation_advance(stay.name, stay.property)
		self.assertEqual(stay.reload().advance_paid, 135)
		for row, expected in ((new, 125), (typed, 100), (legacy, 0)):
			api.refund_folio_payment(name, payment_row=row, reason="Return deposit")
			self.assertEqual(stay.reload().advance_paid, expected)
			folio.reconcile_reservation_advance(stay.name, stay.property)
			self.assertEqual(stay.reload().advance_paid, expected)
