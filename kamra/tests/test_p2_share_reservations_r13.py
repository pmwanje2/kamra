"""A free night leaves the folio alone. A finance edit cannot reprice a posted night."""

from decimal import Decimal
from unittest.mock import patch

import frappe

from kamra import api, folio, shares
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, guest_folio, make_guest, make_property, make_reservation,
	make_room, make_room_type,
)

# Pre-R12 Primary Pays snapshots captured from 6fea0cda. The last six values
# are the posted primary room, room tax, levy, levy tax and the two checkpoints.
# The peer received zero on all four lines.
HISTORICAL = (
	("USD", True, "103.99", "104.02", "220.60", "94.54", "9.45", ".95", ".09", ".004999999999993", ".0049999996"),
	("USD", False, "104.05", "104.06", "220.70", "104.05", "10.41", "1.04", ".10", "-.001", "-.0010000000"),
	("KWD", True, "10.399", "10.402", "22.060", "9.454", ".945", ".095", ".009", ".0004999999999993", ".0005000004"),
	("KWD", False, "10.405", "10.406", "22.070", "10.405", "1.041", ".104", ".010", "-.0001", "-.0001000000"),
	("UGX", True, "9500", "9501", "20171", "8636", "864", "95", "9", ".4999999999996", ".5000000004"),
	("UGX", False, "9501", "9502", "20173", "9501", "950", "105", "10", ".061", ".0610000000"),
)


def checked_in(names):
	for name in names:
		stay = frappe.get_doc("Reservation", name)
		stay.status = "Checked In"
		stay.save()


def due(name):
	return Decimal(str(frappe.db.get_value("Folio", guest_folio(name), "grand_total")))


class TestHistoricalShareBilling(AuditTestCase):
	properties = tuple(f"UT R13 {code} {int(inc)}" for code, inc, *_ in HISTORICAL)

	def setUp(self):
		super().setUp()
		self.at(d(1))

	def test_free_peer_night_keeps_each_prior_balance(self):
		for code, unit in (("USD", Decimal(".01")), ("UGX", Decimal(1)), ("KWD", Decimal(".001"))):
			with self.subTest(currency=code):
				prop = make_property(f"UT R13 {code} 0", currency=code, business_date=TODAY)
				rt = make_room_type(prop, base_price=float(1005 * unit), tax_percent=10)
				main = make_reservation(prop, rt, TODAY, d(3), room=make_room(prop, rt, "101")).name
				peers = [shares.create_share_reservation(main, make_guest(who))["reservation"] for who in ("B", "C")]
				checked_in([main, *peers])
				folio.run_night_audit(prop, TODAY)
				before = [due(peer) for peer in peers]
				self.assertIn(368 * unit, before)
				shares.update_share_rate_mode(main, "Primary Pays")
				folio.run_night_audit(prop, d(1))
				for peer in peers:
					line = frappe.db.get_value("Folio Charge", {"parent": guest_folio(peer), "charge_type": "Room",
					                          "posting_date": d(1)}, ["amount", "gst_amount"])
					self.assertEqual(line, (0, 0))
				self.assertTrue(folio.run_night_audit(prop, d(1))["already_ran"])
				self.assertEqual([due(peer) for peer in peers], before)

	def check_historical(self, code, inclusive, price, upgrade, expected, net, gst, fee, fee_tax, pending, stored):
		pct = "1.1" if code == "UGX" else "1"
		prop = make_property(f"UT R13 {code} {int(inclusive)}", currency=code, business_date=TODAY,
		                     rates_include_tax=int(inclusive), room_levy_percent=float(pct), room_levy_taxable=1)
		rt = make_room_type(prop, base_price=float(price), tax_percent=10)
		room = make_room(prop, rt, "101")
		main = make_reservation(prop, rt, TODAY, d(3), room=room).name
		control = make_reservation(prop, rt, TODAY, d(3), room=make_room(prop, rt, "102")).name
		peer = shares.create_share_reservation(main, make_guest("Peer"))["reservation"]
		shares.update_share_rate_mode(main, "Primary Pays")
		checked_in([main, peer, control])
		def parts(value):
			return {main: value, peer: "0"}

		snapshot = {"room": room, "gst": 10, "category": "", "lead": main, "fx": {},
		            "rates": parts(net), "taxes": parts(gst),
		            "rounding": {"currency": code, "pending": pending, "stored": stored},
		            "levy": {"percent": pct, "fixed": "0", "gst": 10, "per_folio": False,
		                    "amounts": parts(fee), "taxes": parts(fee_tax)}}
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

	def test_setting_change_after_legacy_inclusive_night(self):
		for case in HISTORICAL:
			if case[1]:
				with self.subTest(currency=case[0]):
					self.check_historical(*case)

	def test_setting_change_after_legacy_exclusive_night(self):
		for case in HISTORICAL:
			if not case[1]:
				with self.subTest(currency=case[0]):
					self.check_historical(*case)
