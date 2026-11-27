"""Upgrade repairs through the payment APIs, and the Currency Desk currency filter."""
import frappe

from kamra import api, folio, ledger, settings
from kamra.patches.v48.reconcile_refunded_advances import execute
from kamra.tests.audit_fixtures import AuditTestCase, TODAY, d, make_property, make_reservation, make_room_type, post_legacy_advance


class AdvanceRepairAndFx(AuditTestCase):
	def hotel(self, currency):
		prop = make_property("R6 " + currency, currency=currency, business_date=TODAY)
		settings.write_kv(prop, "use_cashier_tills", 0)
		settings.clear_cache(prop)
		return prop

	def test_repair_does_not_reclassify_settlement_after_refunded_typed_advance(self):
		for currency, amount in (("UGX", 410619), ("USD", 11.12), ("KWD", 11.123)):
			with self.subTest(currency=currency):
				prop = self.hotel(currency)
				stay = make_reservation(prop, make_room_type(prop), d(1), d(3))
				name = api.record_advance(stay.name, amount)["folio"]
				frappe.db.set_value("Reservation", stay.name, "status", "Checked In")
				ledger.transfer_advances_to_guest(stay.name)
				original = frappe.get_doc("Folio", name).payments[0].name
				api.add_folio_payment(name, "Cash", amount + 1)
				api.refund_folio_payment(name, payment_row=original, reason="Return advance")
				frappe.db.set_value("Reservation", stay.name, {"advance_paid": amount, "legacy_advance_basis": None})
				for _ in range(2):
					execute()
					self.assertEqual(stay.reload().advance_paid, 0)

	def test_linked_refunds_after_unlinked_deposit_return_reduce_legacy_advance(self):
		prop = self.hotel("USD")
		rt = make_room_type(prop)
		for upgraded in (False, True):
			with self.subTest(existing_basis=upgraded):
				stay = make_reservation(prop, rt, d(1), d(3))
				name = folio.open_folio(stay)
				legacy = post_legacy_advance(frappe.get_doc("Folio", name), 100)
				api.record_advance(stay.name, 25)
				api.add_folio_payment(name, "Cash", 60)
				api.refund_folio_payment(name, amount=40, mode="Cash", reason="Return deposit")
				# A pre-upgrade counter, or the basis already saved by R5 for this history.
				basis = {"receipts": {legacy: "85"}, "deposit_returns": "15"}
				frappe.db.set_value("Reservation", stay.name, {"advance_paid": 85,
					"legacy_advance_basis": frappe.as_json(basis) if upgraded else None})
				folio.reconcile_reservation_advance(stay.name, prop)
				self.assertEqual(stay.reload().advance_paid, 85)
				for amount, expected in ((40, 45), (45, 0)):
					api.refund_folio_payment(name, amount=amount, payment_row=legacy, reason="Return legacy deposit")
					self.assertEqual(stay.reload().advance_paid, expected)
					folio.reconcile_reservation_advance(stay.name, prop)
					self.assertEqual(stay.reload().advance_paid, expected)

	def test_currency_desk_filters_all_three_lists(self):
		prop = self.hotel("USD")
		for code in ("EUR", "GBP"):
			ledger.upsert_exchange_rate(prop, code, 1.1, 1.2)
		result = ledger.list_exchange_rates(prop, currency="GBP")
		for key in ("rates", "tender_rates", "posting_rates"):
			with self.subTest(key=key):
				self.assertEqual([r["currency"] for r in result[key]], ["GBP"])
		full = ledger.list_exchange_rates(prop, currency=" ")
		for key in ("rates", "tender_rates", "posting_rates"):
			self.assertLessEqual({"GBP", "EUR"}, {r["currency"] for r in full[key]})
