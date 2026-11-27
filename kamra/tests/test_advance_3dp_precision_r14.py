"""Locked Folio lookups accept the same filters as ordinary Frappe document loads."""

import frappe

from kamra.folio import open_folio
from kamra.tests.audit_fixtures import TODAY, AuditTestCase, d, make_property, make_reservation, stocked_room_type


class TestLockedFolioLookup(AuditTestCase):
	properties = ("UT Locked Folio KWD", "UT Locked Folio USD", "UT Locked Folio UGX")

	def test_locked_folio_lookups(self):
		for currency, amount in (("KWD", 1.125), ("USD", 1.12), ("UGX", 1125)):
			prop = make_property(f"UT Locked Folio {currency}", currency=currency)
			stay = make_reservation(prop, stocked_room_type(prop), TODAY, d(1))
			name = open_folio(stay)
			folio = frappe.get_doc("Folio", name)
			payment = folio.append("payments", {
				"payment_kind": "Payment", "posting_date": TODAY, "mode": "Cash", "amount": amount,
			})
			folio.save()

			for lookup in (name, {"name": name}, {"reservation": stay.name}):
				with self.subTest(currency=currency, lookup=lookup):
					loaded = frappe.get_doc("Folio", lookup, for_update=True)
					self.assertEqual(loaded.name, name)
					self.assertEqual(loaded.reservation, stay.name)
					self.assertEqual([(row.name, row.amount) for row in loaded.payments], [(payment.name, amount)])

			with self.subTest(currency=currency, lookup="missing"):
				with self.assertRaises(frappe.DoesNotExistError):
					frappe.get_doc("Folio", {"name": name, "reservation": ["!=", stay.name]}, for_update=True)
