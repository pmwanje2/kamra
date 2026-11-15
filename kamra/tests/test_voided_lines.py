"""Voided folio lines stay visible for audit (#37).

void_folio_charge takes a wrong line off an open bill. Patrick's test asked
for "every charge line, including voided ones" after a void and got "no
charge lines, active or voided": the row is deleted, and only the action
log remembered it in prose. get_folio / folio_invoice with include_voided=1
return a separate voided_charges list (what the line was, why it was
voided, by whom, when) that never adds to grand_total or balance.
"""

from __future__ import annotations

import frappe

from kamra import api
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	guest_folio,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)


class VoidedLinesTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.room = make_room(PROP_A, self.rt, "101")
		self.res = make_reservation(PROP_A, self.rt, TODAY, d(2),
		                            status="Checked In", room=self.room)
		self.folio = guest_folio(self.res.name)
		api.add_folio_charge(self.folio, "Laundry", "UT shirts", 200)
		doc = api.add_folio_charge(self.folio, "Minibar", "UT wrong minibar", 450)
		self.row = doc["charges"][-1]["name"]
		api.void_folio_charge(self.folio, self.row, reason="UT posted to the wrong room")

	def test_the_active_view_is_unchanged(self):
		out = api.get_folio(self.res.name)
		self.assertEqual([c["description"] for c in out["charges"]], ["UT shirts"])
		self.assertNotIn("voided_charges", out)
		self.assertAlmostEqual(out["grand_total"], 200)

	def test_include_voided_lists_the_line_with_who_why_and_when(self):
		out = api.get_folio(self.res.name, include_voided=1)
		self.assertEqual([c["description"] for c in out["charges"]], ["UT shirts"])
		voided = out["voided_charges"]
		self.assertEqual(len(voided), 1)
		v = voided[0]
		self.assertEqual(v["row"], self.row)
		self.assertEqual(v["charge_type"], "Minibar")
		self.assertEqual(v["description"], "UT wrong minibar")
		self.assertAlmostEqual(v["amount"], 450)
		self.assertEqual(v["reason"], "UT posted to the wrong room")
		self.assertEqual(v["voided_by"], "Administrator")
		self.assertTrue(v["voided_at"])
		self.assertEqual(str(v["posting_date"]), TODAY)
		self.assertTrue(v["voided"])

	def test_voided_lines_never_touch_the_totals(self):
		plain = api.get_folio(self.res.name)
		audit = api.get_folio(self.res.name, include_voided=1)
		for key in ("charges_total", "tax_total", "grand_total", "payments_total", "balance"):
			self.assertEqual(plain[key], audit[key], key)
		self.assertAlmostEqual(float(frappe.db.get_value("Folio", self.folio, "balance")), 200)

	def test_the_bill_view_can_show_them_too(self):
		inv = api.folio_invoice(self.folio, include_voided=1)
		self.assertEqual([line["description"] for line in inv["lines"]], ["UT shirts"])
		self.assertEqual([v["description"] for v in inv["voided_lines"]], ["UT wrong minibar"])
		self.assertAlmostEqual(inv["folio"]["grand_total"], 200)
		self.assertNotIn("voided_lines", api.folio_invoice(self.folio))

	def test_a_second_void_is_listed_in_order(self):
		row = api.add_folio_charge(self.folio, "Spa", "UT spa twice", 900)["charges"][-1]["name"]
		api.void_folio_charge(self.folio, row, reason="UT duplicate")
		voided = api.get_folio(self.res.name, include_voided=1)["voided_charges"]
		self.assertEqual([v["description"] for v in voided],
		                 ["UT wrong minibar", "UT spa twice"])

	def test_the_agent_tool_takes_include_voided(self):
		from kamra.mcp_tools import BY_NAME
		self.assertIn("include_voided", BY_NAME["get_folio"].parameters)
