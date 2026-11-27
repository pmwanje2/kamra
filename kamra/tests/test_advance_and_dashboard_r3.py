"""R3 coverage for window transfers and property-scoped owner totals."""
import frappe

from kamra import api, dashboards, folio, settings
from kamra.ledger import transfer_advances_to_guest
from kamra.patches.v48.reconcile_refunded_advances import execute
from kamra.payment_transfers import transfer_folio_payment
from kamra.tests.audit_fixtures import (
	PROP_A, PROP_B, TODAY, AuditTestCase, d, ensure_user, make_property, make_reservation, stocked_room_type,
)
from kamra.tests.bridge_fields import ensure_bridge_field


class AdvanceAndDashboardR3Test(AuditTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_bridge_field("Folio", "sales_invoice")

	def setUp(self):
		super().setUp()
		make_property(PROP_A, business_date=TODAY)
		settings.write_kv(PROP_A, "use_cashier_tills", 0)
		settings.clear_cache(PROP_A)

	def test_migration_replays_refunded_transferred_advance(self):
		room_type = stocked_room_type(PROP_A, inventory=3)
		for currency, amount in (("UGX", 410619), ("USD", 11.12), ("KWD", 11.123)):
			with self.subTest(currency=currency):
				make_property(PROP_A, currency=currency)
				stay = make_reservation(PROP_A, room_type, d(1), d(3))
				source = api.record_advance(stay.name, amount)["folio"]
				frappe.db.set_value("Reservation", stay.name, "status", "Checked In")
				transfer_advances_to_guest(stay.name)
				target = folio.open_window(reservation=stay.name, payee_type="Guest")
				advance = frappe.get_doc("Folio", source).payments[0].name
				move = transfer_folio_payment(source, advance, target, "Move advance to second window")
				api.add_folio_payment(source, "Cash", amount + 1)
				api.refund_folio_payment(target, payment_row=move["target_row"], reason="Refund transferred advance")
				frappe.db.set_value("Reservation", stay.name, "advance_paid", amount)
				for _ in range(2):
					execute()
					self.assertEqual(frappe.db.get_value("Reservation", stay.name, "advance_paid"), 0)
					self.assertEqual(frappe.db.get_value("Folio", source, "payments_total"), amount + 1)

	def test_revenue_manager_reads_only_requested_property_totals(self):
		make_property(PROP_B, business_date=TODAY)
		for prop, amount in ((PROP_A, 41), (PROP_B, 99)):
			stay = make_reservation(prop, stocked_room_type(prop), d(1), d(3))
			name = folio.open_folio(stay)
			frappe.db.set_value("Folio", name, {"balance": amount, "sales_invoice": "SI-" + prop})
		user = ensure_user("r3-revenue@example.com", ["Revenue Manager"])
		frappe.set_user(user)  # nosemgrep: frappe-setuser -- exercise the Revenue Manager's native permissions
		self.assertFalse(frappe.has_permission("Folio", ptype="read"))
		snapshot = dashboards.owner_snapshot(PROP_A, TODAY)
		self.assertEqual(snapshot["guest_balances"], 41)
		self.assertEqual(snapshot["open_folios"], 1)
