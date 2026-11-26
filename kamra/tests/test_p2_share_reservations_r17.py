"""An incomplete share audit cannot authorize a correction to an old bill."""

import json
from decimal import Decimal
from unittest.mock import patch

import frappe

from kamra import api, folio, savings, shares
from kamra.tests import test_p2_share_reservations_r16 as r16
from kamra.tests.audit_fixtures import (
	TODAY, AuditTestCase, d, make_guest, make_property, make_reservation, make_room, make_room_type,
)
from kamra.tests.test_p2_share_reservations_r13 import HISTORICAL, checked_in, due


# Custom gross, coincidentally matching tariff, next night's suite, native bill.
CASES = (
	("USD", "103.99", "103.994", "104.02", "220.60"),
	("KWD", "10.399", "10.3994", "10.402", "22.060"),
	("UGX", "10399", "10399.4", "10407", "22065"),
)


class TestIncompleteShareAudit(AuditTestCase):
	properties = tuple(f"UT R17 {case[0]}" for case in CASES) + r16.TestSplitModeAtPosting.properties

	def setUp(self):
		super().setUp()
		self.at(d(1))

	def check_custom(self, case, *, remove_fallback=False):
		currency, gross, catalog, upgrade, expected = case
		prop = make_property(f"UT R17 {currency}", currency=currency, business_date=TODAY, rates_include_tax=1,
		                     room_levy_percent=1, room_levy_taxable=1)
		rt = make_room_type(prop, base_price=float(catalog), tax_percent=10)
		main = make_reservation(prop, rt, TODAY, d(3), room=make_room(prop, rt, "101")).name
		ctl = make_room_type(prop, "CTL", base_price=float(gross), tax_percent=10)
		control = make_reservation(prop, ctl, TODAY, d(3), room=make_room(prop, ctl, "102")).name
		join = shares.create_share_reservation(main, make_guest("Sharer"))
		peer, group = join["reservation"], join["group_id"]

		insert = savings._insert

		def fail_full_insert(row):
			if row["action_type"] == "update_share_rate_mode" and row.get("after_snapshot"):
				raise frappe.ValidationError("Simulated full audit insert failure")
			return insert(row)

		with patch.object(savings, "_insert", side_effect=fail_full_insert):
			shares.update_share_rate_mode(peer, "Custom", {main: gross, peer: 0})
		log = frappe.db.get_value("Agent Action Log", {"action_type": "update_share_rate_mode", "property": prop},
		                          ["name", "reference_name", "before_snapshot", "after_snapshot", "rationale"], as_dict=True)
		self.assertIn("log fallback", log.rationale)
		self.assertFalse(log.reference_name or log.before_snapshot or log.after_snapshot)
		if remove_fallback:
			# Only the next complete event's before-image can expose the missing change.
			frappe.db.delete("Agent Action Log", {"name": log.name})
		checked_in([main, peer, control])
		with patch("kamra.folio.locked_night", return_value=None):
			folio.run_night_audit(prop, TODAY)
			self.assertEqual(due(main), due(control))
			rows = frappe.get_all("Folio Charge", filters={"share_group": group, "charge_type": "Room"},
			                      fields=["name", "share_snapshot"])
			self.assertEqual(len(rows), 2)
			for row in rows:
				old = json.loads(row.share_snapshot)
				for key in ("rate_split_mode", "rates_include_tax", "rounding"):
					old.pop(key)
				old["levy"].pop("primary_pays")
				frappe.db.set_value("Folio Charge", row.name, "share_snapshot", json.dumps(old))
			before_next_change = shares._frozen_fractions(prop, old, TODAY, group)
			shares.update_share_rate_mode(main, "Primary Pays")
			after_next_change = shares._frozen_fractions(prop, old, TODAY, group)
			frappe.db.set_value("Property", prop, "rates_include_tax", 0)
			frappe.clear_document_cache("Property", prop)
			shares.break_share(peer, make_room(prop, rt, "103"))
			suite = make_room_type(prop, "STE", base_price=float(upgrade), tax_percent=10)
			for name, number in ((main, "201"), (control, "202")):
				api.move_reservation(name, make_room(prop, suite, number), confirm_price=1)
			folio.run_night_audit(prop, d(1))
			balances = due(main), due(control)
			self.assertTrue(folio.run_night_audit(prop, d(1))["already_ran"])
			self.assertEqual((due(main), due(control)), balances)
		self.assertEqual(due(control), Decimal(expected))
		self.assertEqual(due(main), due(control))
		self.assertFalse(after_next_change)
		if not remove_fallback:
			self.assertFalse(before_next_change)

	def test_fallback_mode_change_keeps_the_posted_custom_bill(self):
		for case in CASES:
			with self.subTest(currency=case[0]):
				self.check_custom(case)

	def test_subsequent_before_image_exposes_a_missing_transition(self):
		self.check_custom(CASES[0], remove_fallback=True)

	def test_complete_prearrival_mode_history_keeps_the_tariff_adjustment(self):
		for case in HISTORICAL:
			with self.subTest(currency=case[0], inclusive=case[1]):
				r16.TestSplitModeAtPosting.check_stay(self, case, agreed=True)
