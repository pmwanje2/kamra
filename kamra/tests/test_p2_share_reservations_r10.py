"""Exercise share decisions at posting, multi-night rounding and room reassignment."""

from decimal import Decimal
from unittest.mock import patch

import frappe

from kamra import api, folio, ledger, shares
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, guest_folio, make_guest, make_property, make_reservation,
	make_room, make_room_type,
)
from kamra.tests.test_p2_share_reservations_r9 import CASES


class TestShareLifecycle(AuditTestCase):
	properties = ("UT R10 desk", *(f"UT R10 {code}" for code, *_ in CASES))

	def setUp(self):
		super().setUp()
		self.at(d(1))
		ledger.ensure_default_transaction_codes()

	def booking(self, code="USD", price=100, **settings):
		prop = make_property(self.properties[0] if not settings else f"UT R10 {code}",
		                     currency=code, business_date=TODAY, **settings)
		rt = make_room_type(prop, base_price=price, tax_percent=10 if settings else 0)
		return make_reservation(prop, rt, TODAY, d(3), room=make_room(prop, rt, "101"))

	def test_first_join_during_advance_bill(self):
		primary = self.booking()
		guest = make_guest("Peer")
		get_doc = frappe.get_doc
		get_value = frappe.db.get_value
		joined = []

		def finish_join():
			with patch.object(frappe, "get_doc", get_doc), patch.object(frappe.db, "get_value", get_value):
				joined.append(shares.create_share_reservation(primary.name, guest)["reservation"])

		def join_before_inventory_lock(*args, **kwargs):
			value = get_value(*args, **kwargs)
			if args == ("Reservation", primary.name, "property") and not joined:
				finish_join()
			return value

		def join_after_read(*args, **kwargs):
			doc = get_doc(*args, **kwargs)
			if args == ("Reservation", primary.name) and not joined and not kwargs.get("for_update"):
				# Finish the first join after the biller observes the old document.
				finish_join()
			return doc

		with (patch.object(frappe, "get_doc", join_after_read),
		      patch.object(frappe.db, "get_value", join_before_inventory_lock)):
			ledger.force_advance_bill(primary.name, "tonight")
		peer, = joined
		ledger.force_advance_bill(peer, "tonight")
		bills = [frappe.get_doc("Folio", guest_folio(name)) for name in (primary.name, peer)]
		self.assertEqual(sum(b.grand_total for b in bills), 100)
		self.assertEqual([b.grand_total for b in bills], [50, 50])
		self.assertTrue(all(b.charges[0].share_snapshot for b in bills))

	def test_break_rechecks_maintenance_block(self):
		primary = self.booking()
		peer = shares.create_share_reservation(primary.name, make_guest("Peer"))["reservation"]
		room = make_room(primary.property, primary.room_type, "102")
		block = api.create_room_block(primary.property, room, d(1), d(2), reason="Maintenance")
		with self.assertRaises(frappe.ValidationError):
			shares.break_share(peer, room)
		stay = frappe.get_doc("Reservation", peer)
		self.assertEqual((stay.room, stay.share_group_id), (primary.room, primary.reload().share_group_id))
		api.release_room_block(block["name"])
		self.assertEqual(shares.break_share(peer, room)["room"], room)

	def test_two_audits_match_independent_folio_and_retries(self):
		for code, prices, levy, first_due in CASES:
			with self.subTest(currency=code):
				prices = tuple(map(Decimal, prices))
				a = self.booking(code, float(sum(prices)), rates_include_tax=1,
				                 room_levy_percent=levy, room_levy_taxable=1)
				names = [a.name, *(shares.create_share_reservation(a.name, make_guest(g))["reservation"]
				                  for g in ("Second", "Third"))]
				shares.update_share_rate_mode(a.name, "Custom", dict(zip(names, map(float, prices), strict=True)))
				control = make_reservation(a.property, a.room_type, TODAY, d(3),
				                           room=make_room(a.property, a.room_type, "102"))
				for name in [*names, control.name]:
					stay = frappe.get_doc("Reservation", name)
					stay.status = "Checked In"
					stay.save()
				with patch("kamra.folio.locked_night", return_value=None):
					original = {}
					for night in (TODAY, d(1)):
						folio.run_night_audit(a.property, night)
						bills = [frappe.get_doc("Folio", guest_folio(n)) for n in [*names, control.name]]
						lines = {c.name: (c.amount, c.gst_amount, c.share_snapshot) for b in bills for c in b.charges}
						self.assertTrue(original.items() <= lines.items())
						self.assertTrue(folio.run_night_audit(a.property, night)["already_ran"])
						self.assertEqual(lines, {c.name: (c.amount, c.gst_amount, c.share_snapshot)
						                         for b in bills for c in b.reload().charges})
						original = lines
						due = Decimal(str(bills[-1].grand_total))
						expected = Decimal(first_due) if night == TODAY else {
							"USD": Decimal("210.07"), "KWD": Decimal("21.007"), "UGX": Decimal("19209")}[code]
						self.assertEqual(due, expected)
						self.assertEqual(sum(Decimal(str(b.grand_total)) for b in bills[:-1]), due)
