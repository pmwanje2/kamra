"""A tender correction preserves the reservation's original Deposit allocation."""
import frappe

from kamra import api, folio, ledger, payment_transfers, settings
from kamra.patches.v48 import reconcile_legacy_deposits
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, make_property, make_reservation, make_room, make_room_type, post_legacy_advance,
)


class ReclassifiedDepositTests(AuditTestCase):
	currencies = ("USD", "KWD", "UGX")

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.business_date", "kamra.folio", "kamra.ledger")
		self.supervisor = "supervisor-r11@example.test"
		frappe.get_doc(dict(doctype="User", email=self.supervisor, first_name="Supervisor",
		                    send_welcome_email=0, roles=[dict(role="Finance")])).insert()
		frappe.get_doc(dict(doctype="Cashier PIN", user=self.supervisor, pin="8394")).insert()

	def deposit(self, currency, cap=100):
		prop = make_property("R11 " + currency, currency=currency, business_date=TODAY, require_cashier_pin=0)
		settings.write_kv(prop, "use_cashier_tills", 0)
		settings.clear_cache(prop)
		rt = make_room_type(prop)
		stay = make_reservation(prop, rt, TODAY, d(2), room=make_room(prop, rt, frappe.generate_hash(length=5)))
		name = folio.open_folio(stay)
		receipt = post_legacy_advance(frappe.get_doc("Folio", name), 100)
		frappe.db.set_value("Reservation", stay.name, {
			"advance_paid": cap, "legacy_advance_basis": frappe.as_json({"receipts": {receipt: str(cap)}}),
		})
		return stay, name, receipt

	def reclassify(self, window, receipt, destination):
		return api.correct_folio_payment(
			window, receipt, action_type="reclassify_tender", reason_code="REV-ACCOUNT",
			reason_note="Correct receipt tender", idempotency_key=receipt + "-reclass",
			destination_mode=destination, supervisor_user=self.supervisor, supervisor_pin="8394",
		)["repost"]

	def balance(self, name, account):
		rows = frappe.get_all("Folio Ledger Entry", filters={"folio": name, "ledger": account}, fields=["debit", "credit"])
		return sum(row.credit - row.debit for row in rows)

	def test_reposts_refund_and_check_in_preserve_deposit(self):
		for currency in self.currencies:
			with self.subTest(currency=currency):
				stay, name, original = self.deposit(currency)
				bank = self.reclassify(name, original, "Bank Transfer")
				after_bank = stay.reload().advance_paid
				self.assertEqual(self.balance(name, "Deposit"), 100)
				card = self.reclassify(name, bank, "Card")
				after_card = stay.reload().advance_paid
				api.refund_folio_payment(name, 40, payment_row=card, reason="Return part of deposit")
				after_return = stay.reload().advance_paid
				stay.status = "Checked In"
				stay.save()
				self.assertEqual((self.balance(name, "Guest"), self.balance(name, "Deposit")), (60, 0))
				self.assertEqual(ledger.transfer_advances_to_guest(stay.name), 0)
				reconcile_legacy_deposits.execute()
				self.assertEqual((after_bank, after_card, after_return, stay.reload().advance_paid), (100, 100, 60, 60))

	def test_window_refunds_spend_the_reposted_allocation_once(self):
		for currency in self.currencies:
			with self.subTest(currency=currency):
				stay, source, original = self.deposit(currency)
				replacement = self.reclassify(source, original, "Bank Transfer")
				target = folio.open_window(reservation=stay.name)
				move = payment_transfers.transfer_folio_payment(source, replacement, target, "Move deposit", 60)
				after_move = stay.reload().advance_paid
				api.refund_folio_payment(target, 40, payment_row=move["target_row"], reason="Return moved deposit")
				after_refund = stay.reload().advance_paid
				api.refund_folio_payment(target, 20, payment_row=move["target_row"], reason="Return window remainder")
				after_target = stay.reload().advance_paid
				api.refund_folio_payment(source, 40, payment_row=replacement, reason="Return original window remainder")
				reconcile_legacy_deposits.execute()
				self.assertEqual((after_move, after_refund, after_target, stay.reload().advance_paid), (100, 60, 40, 0))

	def test_reposting_residual_carries_partial_allocation_cap(self):
		for currency in self.currencies:
			with self.subTest(currency=currency):
				stay, name, original = self.deposit(currency, cap=70)
				api.refund_folio_payment(name, 10, payment_row=original, reason="Return original deposit")
				self.assertEqual(stay.reload().advance_paid, 70)
				bank = self.reclassify(name, original, "Bank Transfer")
				after_repost = stay.reload().advance_paid
				reconcile_legacy_deposits.execute()
				after_replay = stay.reload().advance_paid
				api.refund_folio_payment(name, 40, payment_row=bank, reason="Return replacement deposit")
				self.assertEqual((after_repost, after_replay, stay.reload().advance_paid), (70, 70, 50))
