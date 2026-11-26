"""Only the split mode in force when a legacy room night posted can price it."""

import json
from decimal import Decimal
from unittest.mock import patch

import frappe

from kamra import api, folio, ledger, shares
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, make_guest, make_property, make_reservation, make_room, make_room_type,
)
from kamra.tests.test_p2_share_reservations_r13 import HISTORICAL, checked_in, due
from kamra.tests.test_p2_share_reservations_r14 import legacy_snapshot


class TestSplitModeAtPosting(AuditTestCase):
	properties = tuple(f"UT R16 {currency} {int(inclusive)}" for currency, inclusive, *_ in HISTORICAL)

	def setUp(self):
		super().setUp()
		self.at(d(1))

	def check_stay(self, case, *, agreed=False, post_custom=False, keep_sharing=False, advance_first=False):
		code, inclusive, price, upgrade, expected, net, tax, levy, levy_tax, _, _ = case
		pct = "1.1" if code == "UGX" else "1"
		prop = make_property(f"UT R16 {code} {int(inclusive)}", currency=code, rates_include_tax=int(inclusive),
		                     business_date=TODAY, room_levy_percent=float(pct), room_levy_taxable=1)
		catalog = Decimal("103.994") if post_custom else Decimal(price) + int(agreed)
		rt = make_room_type(prop, base_price=float(catalog), tax_percent=10)
		room = make_room(prop, rt, "101")
		main = make_reservation(prop, rt, TODAY, d(3), room=room).name
		ctl = make_room_type(prop, "CTL", base_price=float(price), tax_percent=10)
		control = make_reservation(prop, ctl, TODAY, d(3), room=make_room(prop, ctl, "102")).name
		peer = shares.create_share_reservation(main, make_guest("Peer"))["reservation"]
		group = frappe.db.get_value("Reservation", main, "share_group_id")
		if agreed:
			api.set_agreed_rate(main, float(price), "RATE-PROMO", confirm_price=1)
		shares.update_share_rate_mode(main, "Custom", {main: price, peer: 0})
		if not post_custom:
			shares.update_share_rate_mode(main, "Primary Pays")
		checked_in([main, peer, control])
		with patch("kamra.folio.locked_night", return_value=None):
			# Post the captured pre-checkpoint format through the native audit and ledger.
			old = legacy_snapshot(main, peer, room, pct, net, tax, levy, levy_tax)
			with patch.object(shares, "nightly_share", side_effect=lambda res, *a, **kw: shares._share_line(old, res.name)):
				if advance_first:
					bill = ledger.force_advance_bill(main, "tonight")
					self.assertEqual(bill["nights_posted"], [TODAY])
					parts = shares._frozen_fractions(prop, old, TODAY, group)
					self.assertIn(main, parts)
					self.assertEqual(Decimal(parts[main]["stored"]), Decimal(case[-1]))
				folio.run_night_audit(prop, TODAY)
			self.assertEqual(due(main), due(control))
			if post_custom:
				shares.update_share_rate_mode(main, "Primary Pays")
				self.assertFalse(shares._frozen_fractions(prop, old, TODAY, group))
			frappe.db.set_value("Property", prop, "rates_include_tax", int(not inclusive))
			frappe.clear_document_cache("Property", prop)
			if not keep_sharing:
				shares.break_share(peer, make_room(prop, rt, "103"))
				suite = make_room_type(prop, "STE", base_price=float(upgrade), tax_percent=10)
				for name, number in ((main, "201"), (control, "202")):
					api.move_reservation(name, make_room(prop, suite, number), confirm_price=1)
			folio.run_night_audit(prop, d(1))
			balances = due(main), due(peer), due(control)
			self.assertTrue(folio.run_night_audit(prop, d(1))["already_ran"])
			self.assertEqual((due(main), due(peer), due(control)), balances)
		if keep_sharing:
			current = json.loads(frappe.db.get_value("Folio Charge", {
				"share_group": group, "posting_date": d(1), "charge_type": "Room", "reservation": main,
			}, "share_snapshot"))
			self.assertEqual(current["rate_split_mode"], "Primary Pays")
			self.assertEqual(Decimal(current["rates"][main]), catalog)
			self.assertEqual(Decimal(current["rates"][peer]), 0)
			self.assertEqual(due(peer), 0)
		else:
			self.assertEqual(due(control), Decimal(expected))
			self.assertEqual(due(main), due(control))

	def test_custom_abandoned_before_checkin_at_catalog_rate(self):
		for case in HISTORICAL:
			with self.subTest(currency=case[0], inclusive=case[1]):
				self.check_stay(case)

	def test_custom_abandoned_before_checkin_at_agreed_rate(self):
		for case in HISTORICAL:
			with self.subTest(currency=case[0], inclusive=case[1]):
				self.check_stay(case, agreed=True)

	def test_mode_change_after_custom_post_preserves_the_220_60_bill(self):
		self.check_stay(HISTORICAL[0], post_custom=True)

	def test_next_shared_night_uses_primary_pays(self):
		self.check_stay(HISTORICAL[0], post_custom=True, keep_sharing=True)

	def test_advance_post_before_business_date_close(self):
		self.check_stay(HISTORICAL[1], advance_first=True)
