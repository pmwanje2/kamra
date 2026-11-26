"""A retained catalog price cannot establish the tax basis of an agreed room night."""

from decimal import Decimal, ROUND_HALF_UP
from unittest.mock import patch

import frappe

from kamra import api, folio, shares
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, make_guest, make_property, make_reservation,
	make_room, make_room_type,
)
from kamra.tests.test_p2_share_reservations_r13 import HISTORICAL, checked_in, due


def legacy_snapshot(main, peer, room, pct, net, gst, fee, fee_tax):
	def parts(amount):
		return {main: amount, peer: "0"}

	return {"room": room, "lead": main, "category": "", "fx": {}, "gst": 10,
	        "rates": parts(net), "taxes": parts(gst),
	        "levy": {"gst": 10, "percent": pct, "fixed": "0", "per_folio": False,
	                "amounts": parts(fee), "taxes": parts(fee_tax)}}


class TestHistoricalAgreedRate(AuditTestCase):
	properties = tuple(f"UT R14 {code} {int(inc)}" for code, inc, *_ in HISTORICAL)

	def setUp(self):
		super().setUp()
		self.at(d(1))

	def check_stay(self, case, override):
		code, inclusive, price, upgrade, expected, net, gst, fee, fee_tax, _, _ = case
		pct = "1.1" if code == "UGX" else "1"
		prop = make_property(f"UT R14 {code} {int(inclusive)}", currency=code, business_date=TODAY,
		                     rates_include_tax=int(inclusive), room_levy_percent=float(pct), room_levy_taxable=1)
		# On an exclusive night the unrelated card price looks like the agreed rate plus tax.
		# On an inclusive night it looks like the rounded net.
		display_unit = Decimal(".001" if code == "KWD" else ".01")
		catalog = Decimal(net) if inclusive else (Decimal(net) * Decimal("1.1")).quantize(display_unit, ROUND_HALF_UP)
		rt = make_room_type(prop, base_price=float(catalog if override else price), tax_percent=10)
		room = make_room(prop, rt, "101")
		main = make_reservation(prop, rt, TODAY, d(3), room=room).name
		control_type = make_room_type(prop, "CTL", base_price=float(price), tax_percent=10)
		control = make_reservation(prop, control_type, TODAY, d(3), room=make_room(prop, control_type, "102")).name
		peer = shares.create_share_reservation(main, make_guest("Peer"))["reservation"]
		if override:
			api.set_agreed_rate(main, float(price), "RATE-PROMO", confirm_price=1)
			stay = frappe.get_doc("Reservation", main)
			self.assertTrue(stay.agreed_rate_overrides())
			if code == "USD" and not inclusive:
				night = folio.locked_night(stay, TODAY)
				self.assertEqual((Decimal(str(night.rate_code_amount)), Decimal(str(night.base_rate))),
				                 (Decimal("114.46"), Decimal("104.05")))
		shares.update_share_rate_mode(main, "Primary Pays")
		checked_in([main, peer, control])
		snapshot = legacy_snapshot(main, peer, room, pct, net, gst, fee, fee_tax)
		# Replay the captured pre-checkpoint posting alongside an independent native folio.
		with patch("kamra.folio.locked_night", return_value=None):
			with patch.object(shares, "nightly_share", side_effect=lambda res, *a, **kw: shares._share_line(snapshot, res.name)):
				folio.run_night_audit(prop, TODAY)
			self.assertEqual(due(main), due(control))
			frappe.db.set_value("Property", prop, "rates_include_tax", int(not inclusive))
			frappe.clear_document_cache("Property", prop)
			shares.break_share(peer, make_room(prop, rt, "103"))
			suite = make_room_type(prop, "STE", base_price=float(upgrade), tax_percent=10)
			for name, number in ((main, "201"), (control, "202")):
				api.move_reservation(name, make_room(prop, suite, number), confirm_price=1)
			folio.run_night_audit(prop, d(1))
			self.assertTrue(folio.run_night_audit(prop, d(1))["already_ran"])
		self.assertEqual(due(control), Decimal(expected))
		self.assertEqual(due(main), due(control))

	def test_agreed_rate_and_unrelated_catalog_after_upgrade(self):
		for case in HISTORICAL:
			with self.subTest(currency=case[0], originally_inclusive=case[1]):
				self.check_stay(case, override=True)

	def test_unadjusted_historical_prices_after_upgrade(self):
		for case in HISTORICAL:
			with self.subTest(currency=case[0], originally_inclusive=case[1]):
				self.check_stay(case, override=False)

	def test_ambiguous_import_and_pre_discount_quote_preserve_posted_allocation(self):
		prop = make_property("UT R14 USD 0", currency="USD", business_date=TODAY)
		rt = make_room_type(prop, base_price=114.46, tax_percent=10)
		room = make_room(prop, rt, "101")
		main = make_reservation(prop, rt, TODAY, d(3), room=room).name
		api.set_agreed_rate(main, 104.05, "RATE-PROMO", confirm_price=1)
		night = folio.locked_night(frappe.get_doc("Reservation", main), TODAY)
		snapshot = legacy_snapshot(main, "peer", room, "1", "104.05", "10.41", "1.04", ".10")
		for original_net, discount in ((0, 0), (104.05, 2)):
			with self.subTest(quoted_net=original_net, discount=discount):
				frappe.db.set_value("Reservation Night Rate", night.name,
				                    {"quoted_base_rate": original_net, "discount_amount": discount})
				self.assertFalse(shares._frozen_fractions(prop, snapshot, TODAY))
