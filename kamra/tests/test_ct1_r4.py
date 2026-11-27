"""R4 billing agreement and opening-deposit regressions."""
import frappe

from kamra import api, folio, golive
from kamra.tests.audit_fixtures import AuditTestCase, d, make_property, make_reservation, make_room_type


class ContractAndAdvance(AuditTestCase):
	def setUp(self):
		super().setUp()
		self.prop = make_property("R4 Hotel", currency="UGX", country="Uganda")
		self.room_type = make_room_type(self.prop)

	def booking(self, status="Confirmed"):
		return make_reservation(self.prop, self.room_type, d(1), d(3), status=status)

	def test_prior_usd_charge_does_not_override_current_home_agreement(self):
		res = self.booking()
		name = folio.open_folio(res)
		doc = frappe.get_doc("Folio", name)
		doc.append("charges", {"reservation": res.name, "charge_type": "Room", "posting_date": d(0),
			"description": "Prior contract night", "qty": 1, "amount": 526268, "rate": 526268,
			"contract_currency": "USD", "contract_fx_rate": 4048.22})
		doc.save()
		tariff = {"agreed_rate": 526268, "tariff": {"currency": "USD", "nightly": 130, "fx_rate": 4048.22,
			"property_currency": "UGX", "fx_date": d(0)}}
		frappe.db.set_value("Reservation", res.name, {"auto_price": 0, "fixed_rate": 1, "agreed_rate": 290333.33,
			"contract_tariff": frappe.as_json(tariff)})
		stay = api.folio_invoice(name)["stay"]
		self.assertIsNone(stay["tariff"])
		self.assertEqual(stay["pricing_basis"], "fixed_home")
		self.assertAlmostEqual(stay["fixed_rate_amount"], 290333.33)
		self.assertIsNone(stay["reference_currency"])
		self.assertEqual(frappe.db.get_value("Folio Charge", doc.charges[-1].name, "contract_currency"), "USD")

	def test_held_opening_deposit_confirms_once(self):
		res = self.booking("Held")
		frappe.db.set_value("Reservation", res.name, "idempotency_key", "import:R4-OLD")
		csv = "Type,Reservation No,Amount,Mode\nDeposit,R4-OLD,100,Cash\n"
		result = golive.run_opening(self.prop, csv)
		self.assertEqual(result["errors"], [])
		self.assertEqual(result["posted"]["deposit"], 1)
		self.assertEqual(golive.run_opening(self.prop, csv)["posted"]["deposit"], 0)
		res.reload()
		self.assertEqual((res.status, res.advance_paid), ("Confirmed", 100))

	def test_mixed_legacy_and_typed_advances_survive_reconciliation_and_refunds(self):
		res = self.booking()
		doc = frappe.get_doc("Folio", folio.open_folio(res))
		def receipt(kind, amount, **kw):
			row = doc.append("payments", {"posting_date": d(0), "mode": "Cash", "payment_kind": kind, "amount": amount, **kw})
			doc.save()
			return row.name
		receipt("Payment", 100)
		receipt("Payment", 60)  # ordinary settlement must never be promoted to a deposit
		frappe.db.set_value("Reservation", res.name, "advance_paid", 100)
		res.reload()
		stamp = res.modified
		typed = receipt("Advance", 25)
		self.assertEqual(res.reload().advance_paid, 125)
		self.assertEqual(res.modified, stamp)
		for _ in range(2):
			folio.reconcile_reservation_advance(res.name, self.prop)
			self.assertEqual(res.reload().advance_paid, 125)
		receipt("Advance", 10)
		self.assertEqual(res.reload().advance_paid, 135)
		receipt("Refund", -25, refund_of=typed)
		for _ in range(2):
			folio.reconcile_reservation_advance(res.name, self.prop)
			self.assertEqual(res.reload().advance_paid, 110)
