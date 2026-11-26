"""A legacy Custom allocation has no connection to the reservation's tariff."""

import csv
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import frappe

from kamra import api, folio, shares
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, guest_folio, make_guest, make_property, make_reservation,
	make_room, make_room_type,
)
from kamra.tests.test_p2_share_reservations_r13 import HISTORICAL, checked_in, due


class TestHistoricalCustomPricing(AuditTestCase):
	properties = tuple(f"UT R15 {code} {int(inc)} {rows}" for code, inc, *_ in HISTORICAL for rows in (0, 1))

	def setUp(self):
		super().setUp()
		self.at(d(1))

	def check_custom(self, case, tariff_rows):
		code, inclusive, price, _, _, net, tax, *_ = case
		prop = make_property(f"UT R15 {code} {int(inclusive)} {int(tariff_rows)}", currency=code,
		                     business_date=TODAY, rates_include_tax=int(inclusive),
		                     room_levy_percent=1.1 if code == "UGX" else 1, room_levy_taxable=1)
		# Give the unrelated room tariff precisely the opposite net-basis candidate.
		factor = Decimal("1.1")
		catalog = Decimal(net) * factor if inclusive else (Decimal(net) + Decimal(tax)) / factor
		rt = make_room_type(prop, base_price=float(catalog), tax_percent=10)
		main = make_reservation(prop, rt, TODAY, d(3), room=make_room(prop, rt, "101")).name
		peer = shares.create_share_reservation(main, make_guest("Peer"))["reservation"]
		ctl = make_room_type(prop, "CTL", base_price=float(price), tax_percent=10)
		control = make_reservation(prop, ctl, TODAY, d(3), room=make_room(prop, ctl, "102")).name
		shares.update_share_rate_mode(main, "Custom", {main: price, peer: 0})
		checked_in([main, peer, control])

		# Exercise the native independent room rate and persist the actual Custom posting.
		with patch("kamra.folio.locked_night", return_value=None):
			folio.run_night_audit(prop, TODAY)
			self.assertEqual(due(main), due(control))
			group = frappe.db.get_value("Reservation", main, "share_group_id")
			rows = frappe.get_all("Folio Charge", filters={"charge_type": "Room", "share_group": group},
			                      fields=["name", "share_snapshot"])
			self.assertEqual(len(rows), 2)
			for row in rows:
				frozen = json.loads(row.share_snapshot)
				for key in ("rates_include_tax", "rounding", "rate_split_mode"):
					frozen.pop(key, None)
				frozen["levy"].pop("primary_pays", None)
				frappe.db.set_value("Folio Charge", row.name, "share_snapshot", json.dumps(frozen))
			self.assertEqual(Decimal(frozen["rates"][main]), Decimal(net))
			self.assertEqual(Decimal(frozen["taxes"][main]), Decimal(tax))
			if code == "USD" and inclusive:
				self.assertEqual((catalog, price), (Decimal("103.994"), "103.99"))
			if not tariff_rows:
				frappe.db.delete("Reservation Night Rate", {"parent": main, "stay_date": TODAY})
			frappe.db.set_value("Property", prop, "rates_include_tax", int(not inclusive))
			frappe.clear_document_cache("Property", prop)
			shares.break_share(peer, make_room(prop, rt, "103"))
			upgrade = ({"USD": 104.02, "KWD": 10.402, "UGX": 9505} if inclusive else
			           {"USD": 105.60, "KWD": 10.560, "UGX": 9510})[code]
			suite = make_room_type(prop, "STE", base_price=upgrade, tax_percent=10)
			for name, room_number in ((main, "201"), (control, "202")):
				api.move_reservation(name, make_room(prop, suite, room_number), confirm_price=1)
			folio.run_night_audit(prop, d(1))
			self.assertTrue(folio.run_night_audit(prop, d(1))["already_ran"])
		if code == "USD" and inclusive:
			self.assertEqual(due(control), Decimal("220.60"))
		self.assertEqual(due(main), due(control))
		self.assertFalse(shares._frozen_fractions(prop, frozen, TODAY, group))
		self.assertFalse(shares._frozen_fractions(prop, dict(frozen, rate_split_mode="Custom"), TODAY, group))

	def test_legacy_custom_with_unrelated_tariff(self):
		for case in HISTORICAL:
			with self.subTest(currency=case[0], originally_inclusive=case[1]):
				self.check_custom(case, True)

	def test_legacy_custom_without_tariff(self):
		for case in HISTORICAL:
			with self.subTest(currency=case[0], originally_inclusive=case[1]):
				self.check_custom(case, False)

	def test_full_rate_each_replays_only_each_payers_own_posting(self):
		prop = make_property("UT R15 USD 0 0", currency="USD", business_date=TODAY, room_levy_percent=1)
		rt = make_room_type(prop, base_price=103.994, tax_percent=10)
		main = make_reservation(prop, rt, TODAY, d(3), room=make_room(prop, rt, "101")).name
		peer = shares.create_share_reservation(main, make_guest("Peer"))["reservation"]
		shares.update_share_rate_mode(main, "Full Rate Each")
		api.set_agreed_rate(peer, 80, "RATE-PROMO", confirm_price=1)
		checked_in([main, peer])
		folio.run_night_audit(prop, TODAY)
		row = frappe.db.get_value("Folio Charge", {"parent": guest_folio(peer), "charge_type": "Room"}, "share_snapshot")
		frozen = json.loads(row)
		expected = frozen.pop("rounding")["payers"][peer]
		frozen.pop("rates_include_tax", None)
		frozen.pop("rate_split_mode", None)
		self.assertTrue(frozen["levy"]["per_folio"])
		for lead_base in ("1.34", "1234.56"):
			with self.subTest(other_base=lead_base):
				frozen["rates"][main] = lead_base
				frappe.db.set_value("Reservation Night Rate", {"parent": main, "stay_date": TODAY},
				                    "quoted_base_rate", float(lead_base))
				recovered = shares._legacy_rounding(prop, frozen, {}, False, TODAY)
				self.assertEqual(recovered["payers"][peer], expected)

	def test_share_desk_labels_are_translated(self):
		fields = {"Reservation": shares.FIELDS, "Folio": ("share_prearrival",),
		          "Folio Charge": ("share_group", "share_room", "share_snapshot")}
		labels = {frappe.get_meta(dt).get_field(field).label for dt, names in fields.items() for field in names}
		self.assertEqual(len(labels), 8)
		for lang in ("fr", "es", "ar"):
			with self.subTest(locale=lang):
				path = Path(frappe.get_app_path("kamra", "translations", f"{lang}.csv"))
				with path.open(encoding="utf-8", newline="") as stream:
					translated = {row[0] for row in csv.reader(stream) if len(row) >= 2 and row[1].strip() != row[0] and row[1].strip()}
				self.assertEqual(labels - translated, set())
