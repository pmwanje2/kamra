"""A ledger-backed historical receipt follows the same cash and window controls as an advance."""
import frappe
from frappe.modules import patch_handler

from kamra import api, folio, ledger, payment_transfers, settings
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, make_property, make_reservation, make_room, make_room_type, post_legacy_advance,
)


class DepositReceiptTests(AuditTestCase):
	currencies = ("USD", "KWD", "UGX")

	def stay(self, currency, *, typed=False):
		prop = make_property("R10 " + currency, currency=currency, business_date=TODAY)
		settings.write_kv(prop, "use_cashier_tills", 0)
		settings.clear_cache(prop)
		rt = make_room_type(prop)
		stay = make_reservation(prop, rt, TODAY, d(2), room=make_room(prop, rt, frappe.generate_hash(length=5)))
		name = folio.open_folio(stay)
		api.add_folio_payment(name, "Cash", 100)
		if typed:
			receipt = api.add_folio_payment(name, "Cash", 100, kind="Advance")["payments"][-1]["name"]
		else:
			receipt = post_legacy_advance(frappe.get_doc("Folio", name), 100)
			frappe.db.set_value("Reservation", stay.name, "advance_paid", 100)
		return stay, name, receipt

	def balances(self, name):
		rows = frappe.get_all("Folio Ledger Entry", filters={"folio": name},
		                      fields=["ledger", "credit", "debit"])
		return tuple(sum(row.credit - row.debit for row in rows if row.ledger == account)
		             for account in ("Deposit", "Guest"))

	def test_linked_refund_cannot_consume_more_than_remaining_deposit(self):
		for currency in self.currencies:
			for typed in (False, True):
				with self.subTest(currency=currency, typed=typed):
					stay, name, receipt = self.stay(currency, typed=typed)
					api.refund_folio_payment(name, 90, "Cash", "Return held deposit")
					before = len(frappe.get_doc("Folio", name).payments)
					with self.assertRaises(frappe.ValidationError):
						api.refund_folio_payment(name, 40, payment_row=receipt, reason="Exceeds held deposit")
					self.assertEqual(self.balances(name), (10, 100))
					self.assertEqual(len(frappe.get_doc("Folio", name).payments), before)
					api.refund_folio_payment(name, 10, payment_row=receipt, reason="Return remainder")
					self.assertEqual((self.balances(name), stay.reload().advance_paid), ((0, 100), 0))

	def test_check_in_applies_only_remaining_legacy_deposit_once(self):
		self.at(TODAY, "kamra.business_date", "kamra.folio", "kamra.ledger")
		for currency in self.currencies:
			with self.subTest(currency=currency):
				stay, name, receipt = self.stay(currency)
				api.refund_folio_payment(name, 40, payment_row=receipt, reason="Return deposit")
				stay.reload()
				stay.status = "Checked In"
				stay.save()
				self.assertEqual(self.balances(name), (0, 160))
				self.assertEqual(ledger.transfer_advances_to_guest(stay.name), 0)
				self.assertEqual(stay.reload().advance_paid, 60)

	def test_transferred_receipt_retains_its_deposit_and_refund(self):
		for currency in self.currencies:
			with self.subTest(currency=currency):
				stay, source, receipt = self.stay(currency)
				target = folio.open_window(reservation=stay.name)
				result = payment_transfers.transfer_folio_payment(source, receipt, target, "Move deposit", 60)
				api.refund_folio_payment(target, 40, payment_row=result["target_row"], reason="Return deposit")
				self.assertEqual((self.balances(source), self.balances(target), stay.reload().advance_paid),
				                 ((40, 100), (20, 0), 60))
				frappe.db.set_value("Reservation", stay.name, "legacy_advance_basis",
				                    frappe.as_json({"receipts": {receipt: "100"}, "deposit_returns": "0"}))
				folio.reconcile_reservation_advance(stay.name, stay.property)
				self.assertEqual(stay.reload().advance_paid, 60)

	def test_other_receipts_cannot_fund_a_refund_of_transferred_principal(self):
		for currency in self.currencies:
			with self.subTest(currency=currency):
				stay, source, receipt = self.stay(currency)
				api.add_folio_payment(source, "Cash", 100, kind="Advance")
				target = folio.open_window(reservation=stay.name)
				payment_transfers.transfer_folio_payment(source, receipt, target, "Move deposit", 60)
				with self.assertRaises(frappe.ValidationError):
					api.refund_folio_payment(source, 70, payment_row=receipt, reason="Exceeds source receipt")
				self.assertEqual((self.balances(source), self.balances(target)), ((140, 100), (60, 0)))

	def test_native_refund_save_updates_advance_before_and_after_ledger_posting(self):
		for currency in self.currencies:
			with self.subTest(currency=currency):
				stay, name, receipt = self.stay(currency)
				api.refund_folio_payment(name, 40, payment_row=receipt, reason="Return deposit")
				doc = frappe.get_doc("Folio", name)
				row = doc.append("payments", {"posting_date": TODAY, "payment_kind": "Refund", "mode": "Cash",
				                              "amount": -10, "refund_of": receipt})
				doc.save()
				after_save = stay.reload().advance_paid
				ledger.record_payment_ledger(doc, row.as_dict(), ledger="Deposit")
				self.assertEqual((after_save, stay.reload().advance_paid, self.balances(name)), (50, 50, (50, 100)))

	def test_r9_patch_alone_recovers_destination_refunds_on_historical_transfers(self):
		r8 = "kamra.patches.v48.reconcile_advances_r8"
		r9 = "kamra.patches.v48.reconcile_legacy_deposits"
		if not patch_handler.executed(r8):
			patch_handler.update_patch_log(r8)
		r8_log = patch_handler.executed(r8)
		frappe.db.delete("Patch Log", {"patch": r9})
		self.assertEqual(set(patch_handler.get_all_patches(patch_handler.PatchType.post_model_sync))
		                 - set(frappe.get_all("Patch Log", filters={"skipped": 0}, pluck="patch")), {r9})
		stays = {}
		for currency in self.currencies:
			stay, source, receipt = self.stay(currency)
			target = folio.open_window(reservation=stay.name)
			# Seed the historical rows and their actual Deposit legs, independently of today's transfer API.
			for window, amount, links in ((source, -60, {"transferred_to": target}),
			                              (target, 60, {"transferred_from": source})):
				doc = frappe.get_doc("Folio", window)
				row = doc.append("payments", {"posting_date": TODAY, "payment_kind": "Payment", "mode": "Cash",
				                              "amount": amount, "transfer_group_key": f"PT:{receipt}:import", **links})
				folio._recalculate(doc, payments_only=True)
				doc.save()
				ledger.record_payment_ledger(doc, row.as_dict(), ledger="Deposit")
			api.refund_folio_payment(target, 40, payment_row=row.name, reason="Return moved deposit")
			frappe.db.set_value("Reservation", stay.name, {"advance_paid": 0, "legacy_advance_basis": None})
			stays[currency] = stay
		for run in (1, 2):
			patch_handler.run_all(patch_type=patch_handler.PatchType.post_model_sync)
			self.assertEqual(patch_handler.executed(r8), r8_log)
			self.assertEqual(frappe.db.count("Patch Log", {"patch": r9, "skipped": 0}), 1)
			for currency, stay in stays.items():
				with self.subTest(currency=currency, run=run):
					self.assertEqual(stay.reload().advance_paid, 60)
