"""Upgrade repair and cashier controls for tender corrections on legacy deposits."""
import frappe
from frappe.modules import patch_handler

from kamra import api, folio, ledger, payment_transfers
from kamra.localization import money_text
from kamra.tests.test_ct1_r11 import LegacyDepositCorrectionCase


class LegacyDepositRepairTests(LegacyDepositCorrectionCase):
	def test_logged_r9_upgrade_repairs_reposted_deposits_once(self):
		r9 = "kamra.patches.v48.reconcile_legacy_deposits"
		repair = "kamra.patches.v48.reconcile_reposted_legacy_deposits"
		r9_log = patch_handler.executed(r9)
		self.assertTrue(r9_log)
		frappe.db.delete("Patch Log", {"patch": repair})
		stays = {}
		for currency in self.currencies:
			stay, window, receipt = self.deposit(currency)
			self.reclassify(window, receipt, "Bank Transfer")
			self.assertEqual(self.balance(window, "Deposit"), 100)
			frappe.db.set_value("Reservation", stay.name, "advance_paid", 0)
			stays[currency] = stay

		for run in (1, 2):
			patch_handler.run_all(patch_type=patch_handler.PatchType.post_model_sync)
			self.assertEqual(patch_handler.executed(r9), r9_log)
			for currency, stay in stays.items():
				with self.subTest(currency=currency, run=run):
					self.assertEqual(stay.reload().advance_paid, 100)
		self.assertEqual(frappe.db.count("Patch Log", {"patch": repair, "skipped": 0}), 1)

	def test_only_source_window_remainder_is_reclassified(self):
		for currency in self.currencies:
			with self.subTest(currency=currency):
				stay, source, receipt = self.deposit(currency)
				bank = self.reclassify(source, receipt, "Bank Transfer")
				target = folio.open_window(reservation=stay.name)
				payment_transfers.transfer_folio_payment(source, bank, target, "Move deposit", 60)
				options = api.payment_correction_options(source, bank)
				self.assertEqual((options["residual"], options["refund_cap"]), (40, 40))
				card = self.reclassify(source, bank, "Card")
				self.assertEqual(frappe.db.get_value("Folio Payment", card, ["amount", "mode"]), (40, "Card"))
				self.assertEqual(api.payment_correction_options(source, bank)["residual"], 0)
				self.assertEqual((self.balance(source, "Deposit"), self.balance(target, "Deposit")), (40, 60))
				self.assertEqual(stay.reload().advance_paid, 100)

	def test_deposit_rejections_report_the_localized_refundable_amount(self):
		stay, window, receipt = self.deposit("USD")
		guest = api.add_folio_payment(window, "Cash", 100)["payments"][-1]["name"]
		api.refund_folio_payment(window, 60, "Cash", "Return part of the deposit")
		doc = frappe.get_doc("Folio", window)
		frappe.local.lang = "fr"
		linked = frappe._("Only {0} of this payment is left to refund.")
		unlinked = frappe._("Only {0} is due back to the guest - can't refund more than that.")
		for label, reference, message in (
			("Guest receipt", guest, linked.format(money_text(stay.property, 0))),
			("unlinked", None, unlinked.format(money_text(stay.property, 40))),
			("Deposit receipt", receipt, linked.format(money_text(stay.property, 40))),
		):
			with self.subTest(path=label):
				with self.assertRaises(frappe.ValidationError) as error:
					ledger.record_payment_ledger(doc, {"payment_kind": "Refund", "amount": -100, "refund_of": reference},
					                             ledger="Deposit")
				self.assertEqual(str(error.exception), message)
		with self.assertRaises(frappe.ValidationError) as error:
			self.reclassify(window, receipt, "Bank Transfer")
		self.assertEqual(str(error.exception), linked.format(money_text(stay.property, 40)))
		self.assertEqual((self.balance(window, "Deposit"), stay.reload().advance_paid), (40, 40))
