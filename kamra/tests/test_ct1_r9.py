"""Direct upgrades and refunds of untyped, ledger-backed reservation deposits."""
import frappe
from frappe.modules import patch_handler

from kamra import api, folio, settings
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, make_property, make_reservation, make_room_type, post_legacy_advance,
)


def seed_stays(currency):
	"""Two populated stays as they would be found on a site upgrading from #73."""
	prop = make_property("R9 " + currency, currency=currency, business_date=TODAY)
	settings.write_kv(prop, "use_cashier_tills", 0)
	settings.clear_cache(prop)
	rt = make_room_type(prop)
	mixed = make_reservation(prop, rt, d(1), d(3))
	first = folio.open_folio(mixed)
	post_legacy_advance(frappe.get_doc("Folio", first), 100)
	typed = api.add_folio_payment(first, "Cash", 25, kind="Advance")["payments"][-1]["name"]
	api.refund_folio_payment(first, payment_row=typed, reason="Return typed advance")
	# #73 has no basis field. After model sync the new field is NULL.
	frappe.db.set_value("Reservation", mixed.name, {"advance_paid": 0, "legacy_advance_basis": None})

	legacy = make_reservation(prop, rt, d(4), d(6))
	second = folio.open_folio(legacy)
	api.add_folio_payment(second, "Cash", 100)
	receipt = post_legacy_advance(frappe.get_doc("Folio", second), 100)
	frappe.db.set_value("Reservation", legacy.name, {"advance_paid": 100, "legacy_advance_basis": None})
	api.refund_folio_payment(second, payment_row=receipt, amount=40, reason="Partial deposit refund")
	return mixed, legacy


class DirectUpgradeAndLegacyRefund(AuditTestCase):
	def test_linked_refund_and_two_post_model_sync_runs(self):
		old = "kamra.patches.v48.reconcile_refunded_advances"
		for patch in (old, old + " # Reconcile linked refunds including transfers between windows"):
			if not patch_handler.executed(patch):
				patch_handler.update_patch_log(patch)
		# Restore the #73 Patch Log on a newer test installation.
		frappe.db.delete("Patch Log", {"patch": ["like", "kamra.patches.v48.reconcile_advances_r8%"]})
		stays = {currency: seed_stays(currency) for currency in ("USD", "KWD", "UGX")}
		for currency, (_, legacy) in stays.items():
			with self.subTest(currency=currency, phase="payment API"):
				self.assertEqual(legacy.reload().advance_paid, 60)
		for run in (1, 2):
			patch_handler.run_all(patch_type=patch_handler.PatchType.post_model_sync)
			for currency, (mixed, legacy) in stays.items():
				with self.subTest(currency=currency, migration=run, stay="refunded typed advance"):
					self.assertEqual(mixed.reload().advance_paid, 100)
				with self.subTest(currency=currency, migration=run, stay="legacy only"):
					self.assertEqual(legacy.reload().advance_paid, 60)

		for currency, (_, legacy) in stays.items():
			with self.subTest(currency=currency, phase="refund Guest settlement"):
				name = folio.open_folio(legacy)
				doc = frappe.get_doc("Folio", name)
				api.refund_folio_payment(name, payment_row=doc.payments[0].name, amount=10, reason="Return settlement")
				self.assertEqual(legacy.reload().advance_paid, 60)
				refund = doc.reload().payments[-1].name
				self.assertTrue(frappe.db.exists("Folio Ledger Entry", {
					"source_doctype": "Folio Payment", "source_name": refund, "ledger": "Guest", "debit": 10,
				}))
