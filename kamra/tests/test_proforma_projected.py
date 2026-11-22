"""generate_proforma(include_projected=1): a pro forma for a stay still in
progress shows what check-out will bill, not only what is posted so far
(OPERA advance / pro forma folio). The nights not posted yet are worked out
by the writer check-out uses, inside a savepoint that is always rolled
back: they come back as projected estimate lines and nothing is posted."""

from __future__ import annotations

import frappe

from kamra import folio as folio_mod
from kamra import ledger
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	YESTERDAY,
	AuditTestCase,
	charges,
	d,
	guest_folio,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)


class ProjectedProformaTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY)
		make_property(PROP_A, business_date=YESTERDAY)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.room = make_room(PROP_A, self.rt, "101")
		self.res = make_reservation(PROP_A, self.rt, d(-1), d(2),
		                            status="Checked In", room=self.room)
		self.folio = guest_folio(self.res.name)
		# last night is on the bill; tonight and tomorrow are not yet
		folio_mod.post_room_night(frappe.get_doc("Reservation", self.res.name), d(-1))

	def counts(self):
		return (frappe.db.count("Folio Charge", {"parent": self.folio}),
		        frappe.db.count("Folio Ledger Entry", {"folio": self.folio}))

	def test_in_house_proforma_adds_the_remaining_nights_as_projected(self):
		before = self.counts()
		out = ledger.generate_proforma(self.folio)
		inv = out["invoice"]
		self.assertEqual([l["posting_date"] for l in inv["lines"]], [d(-1)])
		projected = inv["projected_lines"]
		self.assertEqual([l["posting_date"] for l in projected], [d(0), d(1)])
		self.assertTrue(all(l["projected"] for l in projected))
		self.assertTrue(all(l["charge_type"] == "Room" for l in projected))
		self.assertEqual([float(l["amount"]) for l in projected], [1000, 1000])
		est = inv["estimate"]
		self.assertAlmostEqual(est["posted_total"], 1000)
		self.assertAlmostEqual(est["projected_total"], 2000)
		self.assertAlmostEqual(est["grand_total"], 3000)
		# an estimate only: nothing posted, the ledger untouched
		self.assertEqual(self.counts(), before)
		self.assertEqual(len(charges(self.res.name, "Room")), 1)
		self.assertAlmostEqual(float(frappe.db.get_value("Folio", self.folio, "grand_total")), 1000)
		# the stored proforma keeps the projected lines apart from the posted ones
		import json
		payload = json.loads(frappe.db.get_value("Proforma Folio", out["proforma"], "payload"))
		self.assertEqual(len(payload["lines"]), 1)
		self.assertEqual(len(payload["projected_lines"]), 2)

	def test_posted_only_when_asked(self):
		inv = ledger.generate_proforma(self.folio, include_projected=0)["invoice"]
		self.assertEqual(len(inv["lines"]), 1)
		self.assertEqual(inv.get("projected_lines") or [], [])

	def test_a_checked_out_stay_projects_nothing(self):
		folio_mod.post_remaining_nights(frappe.get_doc("Reservation", self.res.name))
		frappe.db.set_value("Reservation", self.res.name, "status", "Checked Out")
		inv = ledger.generate_proforma(self.folio)["invoice"]
		self.assertEqual(len(inv["lines"]), 3)
		self.assertEqual(inv["projected_lines"], [])
		self.assertAlmostEqual(inv["estimate"]["projected_total"], 0)

	def test_a_confirmed_stay_with_a_deposit_folio_projects_every_night(self):
		future = make_reservation(PROP_A, self.rt, d(3), d(5), room=self.room)
		folio = folio_mod.open_folio(frappe.get_doc("Reservation", future.name))
		inv = ledger.generate_proforma(folio)["invoice"]
		self.assertEqual(inv["lines"], [])
		self.assertEqual([l["posting_date"] for l in inv["projected_lines"]], [d(3), d(4)])
		self.assertAlmostEqual(inv["estimate"]["grand_total"], 2000)
		self.assertEqual(charges(future.name, "Room"), [])
