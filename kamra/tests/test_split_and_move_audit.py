"""Splitting and moving folio lines keeps the audit trail (group billing P0).

OPERA Cloud splits a posting by amount, percentage or quantity and
recalculates tax on both parts; OPERA refuses payments and package
postings. A split here never rewrites the posted line: the original stays,
a reversal cancels it and the parts are posted new, each pointing at the
original (corrects_row). Gross and tax are conserved exactly. A line from an
outlet (the bridge's source_ref) moves only whole, within its stay or group,
and keeps its source_ref, so the hotel invoice never fiscalises it twice.
"""

from __future__ import annotations

import frappe

from kamra import api, folio as folio_mod
from kamra.ledger import save_folio
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
from kamra.tests.bridge_fields import ensure_bridge_field


def bal(folio: str) -> float:
	return round(float(frappe.db.get_value("Folio", folio, "balance") or 0), 2)


def ledger_net(folio: str) -> float:
	return round(float(frappe.db.sql(
		"""SELECT COALESCE(SUM(debit - credit), 0) FROM `tabFolio Ledger Entry`
		   WHERE folio = %s AND ledger = 'Guest'""", folio)[0][0]), 2)


class SplitAndMoveAuditTestCase(AuditTestCase):
	properties = (PROP_A,)

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_bridge_field("Folio Charge", "source_ref")

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY)
		frappe.db.delete("Property Setting", {"property": PROP_A})
		self.rt = make_room_type(PROP_A, "STD", base_price=5000)
		self.res = make_reservation(PROP_A, self.rt, d(-1), d(1), status="Checked In",
		                            room=make_room(PROP_A, self.rt, "101"))
		self.main = guest_folio(self.res.name)
		self.extra = folio_mod.split_folio(self.res.name, "Extra")

	def post(self, folio: str, **line) -> str:
		doc = frappe.get_doc("Folio", folio)
		doc.append("charges", {
			"posting_date": TODAY, "reservation": self.res.name,
			"charge_type": "Laundry", "description": "UT laundry", "qty": 1,
			"rate": line.get("amount"), "gst_rate": 0, **line})
		folio_mod._recalculate(doc)
		save_folio(doc)
		return doc.charges[-1].name

	def lines(self, folio: str) -> list:
		return frappe.get_doc("Folio", folio).charges

	def standing(self, original: str) -> list:
		"""The lines that stand in place of `original` on either folio."""
		return [c for f in (self.main, self.extra) for c in self.lines(f)
		        if c.get("corrects_row") == original]

	def assert_conserved(self, original: str, amount: float, tax: float, total: float):
		parts = self.standing(original)
		self.assertAlmostEqual(sum(c.amount for c in parts), amount, places=6)
		self.assertAlmostEqual(sum(c.gst_amount for c in parts), tax, places=6)
		self.assertAlmostEqual(sum(c.total for c in parts), total, places=6)
		for f in (self.main, self.extra):
			self.assertEqual(ledger_net(f), bal(f), f)

	# 1. a split is append-only ------------------------------------------

	def test_split_keeps_the_posted_line_and_reverses_it(self):
		row = self.post(self.main, qty=3, rate=100, amount=300, gst_rate=18)
		out = folio_mod.split_charge(self.main, row, self.extra, percent=30)
		orig = next(c for c in self.lines(self.main) if c.name == row)
		self.assertEqual((orig.qty, orig.rate, orig.amount), (3, 100, 300))
		reversal = [c for c in self.lines(self.main) if c.get("reversal_of") == row]
		self.assertEqual([(c.qty, c.amount) for c in reversal], [(3, -300)])
		kept = [c for c in self.lines(self.main) if c.get("corrects_row") == row]
		moved = [c for c in self.lines(self.extra) if c.get("corrects_row") == row]
		self.assertEqual(([c.amount for c in kept], [c.amount for c in moved]), ([210], [90]))
		self.assertEqual((out["kept_row"], out["moved_row"]), (kept[0].name, moved[0].name))
		self.assertEqual((out["kept"], out["moved"]), (210, 90))
		self.assert_conserved(row, amount=300, tax=54, total=354)
		# lineage rides on reversal_of / corrects_row: source_ref means an
		# outlet charge and nothing else, so no split or reversal line has one
		self.assertFalse([c.name for f in (self.main, self.extra) for c in self.lines(f)
		                  if c.get("source_ref")])

	def test_a_line_splits_on_its_own_folio(self):
		row = self.post(self.main, amount=1000, gst_rate=18)
		folio_mod.split_charge(self.main, row, self.main, percent=40)
		self.assertEqual(sorted(c.amount for c in self.standing(row)), [400, 600])
		self.assertEqual(bal(self.main), 1180)  # nothing left the bill
		self.assert_conserved(row, amount=1000, tax=180, total=1180)

	def test_quantity_split_moves_whole_units(self):
		row = self.post(self.main, qty=3, rate=100, amount=300, gst_rate=18)
		out = folio_mod.split_charge(self.main, row, self.extra, qty=1)
		kept = next(c for c in self.lines(self.main) if c.name == out["kept_row"])
		moved = next(c for c in self.lines(self.extra) if c.name == out["moved_row"])
		self.assertEqual((kept.qty, kept.rate, kept.amount), (2, 100, 200))
		self.assertEqual((moved.qty, moved.rate, moved.amount), (1, 100, 100))
		self.assert_conserved(row, amount=300, tax=54, total=354)
		# OPERA: between 1 and the line's quantity less one
		with self.assertRaisesRegex(frappe.ValidationError, "unit"):
			folio_mod.split_charge(self.main, kept.name, self.extra, qty=2)
		single = self.post(self.main, amount=50)
		with self.assertRaisesRegex(frappe.ValidationError, "unit"):
			folio_mod.split_charge(self.main, single, self.extra, qty=1)

	def test_tax_inclusive_night_splits_its_price_exactly(self):
		make_property(PROP_A, business_date=TODAY, rates_include_tax=1)
		# booked once prices include tax: the night locks its rate at booking
		taxed = make_room_type(PROP_A, "INC", base_price=5000, tax_percent=12)
		self.res = make_reservation(PROP_A, taxed, d(-1), d(1), status="Checked In",
		                            room=make_room(PROP_A, taxed, "103"))
		self.main = guest_folio(self.res.name)
		self.extra = folio_mod.split_folio(self.res.name, "Extra")
		folio_mod.post_room_night(frappe.get_doc("Reservation", self.res.name), d(-1))
		night = next(c for c in self.lines(self.main) if c.charge_type == "Room")
		# the price (5000) is the gross; the night stores the net left after tax
		self.assertGreater(night.gst_rate, 0)
		self.assertEqual(round(night.total, 2), 5000)
		gross, tax = night.total, night.gst_amount
		folio_mod.split_charge(self.main, night.name, self.extra, percent=30)
		(moved,) = [c for c in self.lines(self.extra) if c.charge_type == "Room"]
		# the share moved is 30% of the price to the minor unit, tax inside it;
		# the rest of the price, to the last fraction, stays
		self.assertAlmostEqual(moved.total, 1500, places=6)
		kept = next(c for c in self.lines(self.main) if c.get("corrects_row") == night.name)
		self.assertAlmostEqual(kept.total, gross - 1500, places=6)
		self.assert_conserved(night.name, amount=night.amount, tax=tax, total=gross)

	def test_an_outlet_line_never_splits(self):
		row = self.post(self.main, charge_type="Food & Beverage", description="UT dinner",
		                amount=500, source_ref="UT-OUTLET-0001")
		with self.assertRaisesRegex(frappe.ValidationError, "outlet"):
			folio_mod.split_charge(self.main, row, self.extra, percent=50)
		self.assertEqual(self.lines(self.extra), [])

	def test_a_package_line_never_splits(self):
		row = self.post(self.main, charge_type="Meal Plan", description="UT breakfast",
		                amount=400, auto_posted=1)
		with self.assertRaisesRegex(frappe.ValidationError, "package"):
			folio_mod.split_charge(self.main, row, self.extra, percent=50)
		self.assertEqual(self.lines(self.extra), [])

	def test_a_payment_never_splits(self):
		api.add_folio_payment(self.main, "Cash", 100)
		pay = frappe.get_doc("Folio", self.main).payments[-1].name
		with self.assertRaisesRegex(frappe.ValidationError, "payment"):
			folio_mod.split_charge(self.main, pay, self.extra, percent=50)

	# 2. an outlet line moves whole and keeps its source -----------------

	def test_moving_an_outlet_line_keeps_its_source_ref(self):
		row = self.post(self.main, charge_type="Food & Beverage", description="UT room service",
		                amount=500, source_ref="UT-OUTLET-0002")
		folio_mod.transfer_charges(self.main, [row], self.extra)
		self.assertFalse([c for c in self.lines(self.main) if c.get("source_ref")])
		self.assertEqual([(c.get("source_ref"), c.amount) for c in self.lines(self.extra)],
		                 [("UT-OUTLET-0002", 500)])
		# exactly one line anywhere carries the outlet's reference
		self.assertEqual(frappe.db.count("Folio Charge", {"source_ref": "UT-OUTLET-0002"}), 1)

	def test_an_outlet_line_moves_only_within_its_stay_or_group(self):
		other = make_reservation(PROP_A, self.rt, d(-1), d(1), status="Checked In",
		                         room=make_room(PROP_A, self.rt, "102"))
		row = self.post(self.main, charge_type="Food & Beverage", description="UT lunch",
		                amount=300, source_ref="UT-OUTLET-0003")
		with self.assertRaisesRegex(frappe.ValidationError, "outlet"):
			api.transfer_folio_charges(self.main, [row], to_reservation=other.name,
			                           reason="UT wrong room")
		self.assertEqual([c.get("source_ref") for c in self.lines(self.main)
		                  if c.name == row], ["UT-OUTLET-0003"])
