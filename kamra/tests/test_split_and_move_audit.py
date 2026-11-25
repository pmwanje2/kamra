"""Splitting and moving folio lines keeps the audit trail (group billing P0).

OPERA Cloud splits a posting by amount, percentage or quantity and
recalculates tax on both parts; OPERA refuses payments and package
postings. A split here never rewrites the posted line: the original stays and
a reversal voids it (reversal_of, the void pair the ERPNext bridge's
KMR-VOID-001 fences admit), and the parts are posted new as ordinary lines,
each naming the line it was split from (split_of), so every part can still be
corrected, voided, moved or split again. Each part split off is worked out to
the currency's minor unit, its tax too, and the line split keeps the rest of
both, so gross and tax add up exactly across the bills. A line from an outlet
(the bridge's source_ref) moves only whole, within its stay or group, and
keeps its source_ref, so the hotel invoice never fiscalises it twice.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe.utils import flt

from kamra import api, audit_report, books
from kamra import folio as folio_mod
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


def rounded(value, unit: str) -> Decimal:
	"""What a bill in this currency states: half up to the minor unit."""
	return Decimal(str(value or 0)).quantize(Decimal(unit), rounding=ROUND_HALF_UP)


def bridge_void_refusals(folio: str) -> list[str]:
	"""The lines of this folio the ERPNext bridge's KMR-VOID-001 fence refuses
	(hospitality-kamra-bridge v27: Kamra ERP Folio Fences, Folio Close and
	Checkout Fence all carry the same block), and its KMR-CN-001 refusal of a
	negative line that is neither paired nor an allowance or discount.
	Restated here so a Kamra change can't drift from what the bridge admits:
	a line that names another (reversal_of or corrects_row) names a line of the
	same bill, and either voids it (one reversal_of line, no correction) or
	corrects it (one negative and one positive corrects_row line, no void), at
	the same charge type and transaction code and exactly the opposite amount."""
	rows = frappe.get_doc("Folio", folio).charges
	refused, dropped = [], set()
	for row in rows:
		target = row.get("reversal_of") or row.get("corrects_row")
		if not target:
			continue
		original = next((r for r in rows if r.name == target), None)
		voids = sum(1 for r in rows if r.get("reversal_of") == target)
		reversals = [r for r in rows if r.get("corrects_row") == target and flt(r.amount) < 0]
		reposts = sum(1 for r in rows if r.get("corrects_row") == target and flt(r.amount) >= 0)
		if row.get("reversal_of"):
			ok = voids == 1 and not reversals and not reposts and not row.get("corrects_row")
			reversal = row
		else:
			ok = voids == 0 and len(reversals) == 1 and reposts == 1
			reversal = reversals[0] if reversals else None
		ok = ok and original is not None and not row.get("source_ref")
		if ok:
			ok = (not original.get("source_ref") and not original.get("reversal_of")
			      and flt(original.amount) != 0
			      and not (original.get("corrects_row")
			               and (row.get("corrects_row") or flt(original.amount) < 0))
			      and reversal.charge_type == original.charge_type
			      and (reversal.transaction_code or "") == (original.transaction_code or "")
			      and flt(reversal.amount) == -flt(original.amount))
		if not ok:
			refused.append(row.name)
		else:
			dropped |= {target, reversal.name}
	for row in rows:
		if flt(row.amount) < 0 and row.name not in dropped and not row.get("source_ref") \
				and row.charge_type not in ("Discount", "Allowance"):
			refused.append(row.name)
	return refused


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

	def folios(self) -> list[str]:
		return frappe.get_all("Folio", filters={"reservation": self.res.name}, pluck="name")

	def standing(self, original: str) -> list:
		"""The lines that stand in place of `original` on any folio of the stay."""
		return [c for f in self.folios() for c in self.lines(f)
		        if c.get("split_of") == original and not c.get("reversal_of")]

	def assert_conserved(self, original: str, amount: float, tax: float, total: float):
		parts = self.standing(original)
		self.assertAlmostEqual(sum(c.amount for c in parts), amount, places=6)
		self.assertAlmostEqual(sum(c.gst_amount for c in parts), tax, places=6)
		self.assertAlmostEqual(sum(c.total for c in parts), total, places=6)
		for f in self.folios():
			self.assertEqual(ledger_net(f), bal(f), f)
			self.assertEqual(bridge_void_refusals(f), [], f)

	def taxed_night(self, price: float, tax: float, currency: str | None = None,
	                number: str = "103"):
		"""A night priced with tax included, posted by the audit."""
		make_property(PROP_A, business_date=TODAY, rates_include_tax=1,
		              **({"currency": currency} if currency else {}))
		taxed = make_room_type(PROP_A, f"INC{int(tax)}", base_price=price, tax_percent=tax)
		self.res = make_reservation(PROP_A, taxed, d(-1), d(1), status="Checked In",
		                            room=make_room(PROP_A, taxed, number))
		self.main = guest_folio(self.res.name)
		self.extra = folio_mod.split_folio(self.res.name, "Extra")
		folio_mod.post_room_night(frappe.get_doc("Reservation", self.res.name), d(-1))
		night = next(c for c in self.lines(self.main) if c.charge_type == "Room")
		# the night's net is posted to the cent, so its price holds to the cent
		self.assertEqual((night.gst_rate, round(night.total, 2)), (tax, price))
		return night

	def assert_bills_add_up(self, night, folios: list[str], unit: str):
		"""Each bill rounds its own totals; across them the night's price and
		its tax come out exactly, to the minor unit (nothing minted)."""
		docs = [frappe.get_doc("Folio", f) for f in folios]
		self.assertEqual(sum(rounded(f.grand_total, unit) for f in docs),
		                 rounded(night.total, unit))
		self.assertEqual(sum(rounded(f.tax_total, unit) for f in docs),
		                 rounded(night.gst_amount, unit))
		self.assertEqual(sum(rounded(f.charges_total, unit) for f in docs),
		                 rounded(night.amount, unit))

	# 1. a split is append-only, in the shape the bridge admits ----------

	def test_split_keeps_the_posted_line_and_voids_it(self):
		row = self.post(self.main, qty=3, rate=100, amount=300, gst_rate=18)
		out = folio_mod.split_charge(self.main, row, self.extra, percent=30)
		orig = next(c for c in self.lines(self.main) if c.name == row)
		self.assertEqual((orig.qty, orig.rate, orig.amount), (3, 100, 300))
		reversal = [c for c in self.lines(self.main) if c.get("reversal_of") == row]
		self.assertEqual([(c.qty, c.amount, c.get("split_of")) for c in reversal], [(3, -300, row)])
		kept = [c for c in self.lines(self.main) if c.get("split_of") == row
		        and not c.get("reversal_of")]
		moved = [c for c in self.lines(self.extra) if c.get("split_of") == row]
		self.assertEqual(([c.amount for c in kept], [c.amount for c in moved]), ([210], [90]))
		self.assertEqual((out["kept_row"], out["moved_row"]), (kept[0].name, moved[0].name))
		self.assertEqual((out["kept"], out["moved"]), (210, 90))
		self.assert_conserved(row, amount=300, tax=54, total=354)
		# the parts are ordinary lines: no correction lineage the bridge would
		# pair, and source_ref means an outlet charge and nothing else
		every = [c for f in (self.main, self.extra) for c in self.lines(f)]
		self.assertFalse([c.name for c in every if c.get("corrects_row")])
		self.assertFalse([c.name for c in every if c.get("source_ref")])

	def test_a_line_splits_on_its_own_folio(self):
		row = self.post(self.main, amount=1000, gst_rate=18)
		folio_mod.split_charge(self.main, row, self.main, percent=40)
		self.assertEqual(sorted(c.amount for c in self.standing(row)), [400, 600])
		self.assertEqual(bal(self.main), 1180)  # nothing left the bill
		self.assert_conserved(row, amount=1000, tax=180, total=1180)

	def test_the_api_keeps_its_split_contract(self):
		row = self.post(self.main, amount=1000)
		out = api.split_folio_charge(self.main, row, self.extra, percent=30)
		self.assertEqual(out, {"kept": 700.0, "moved": 300.0})

	def test_split_parts_stay_correctable(self):
		row = self.post(self.main, amount=1000, gst_rate=18)
		out = folio_mod.split_charge(self.main, row, self.extra, percent=30)
		api.correct_folio_charge(self.main, out["kept_row"], reason="UT wrong price",
		                         amount=650)
		api.correct_folio_charge(self.extra, out["moved_row"], reason="UT wrong price",
		                         amount=250)
		# the voided original is not a live line: it is never corrected again
		with self.assertRaises(frappe.ValidationError):
			api.correct_folio_charge(self.main, row, reason="UT again", amount=900)
		for f in (self.main, self.extra):
			self.assertEqual(bridge_void_refusals(f), [], f)
			self.assertEqual(ledger_net(f), bal(f), f)
		self.assertEqual(sorted(c.amount for f in (self.main, self.extra)
		                        for c in self.lines(f) if c.get("corrects_row")
		                        and c.amount > 0), [250, 650])

	def test_a_split_part_is_voided_and_split_again_in_the_bridge_shape(self):
		row = self.post(self.main, amount=900, gst_rate=5)
		out = folio_mod.split_charge(self.main, row, self.extra, percent=50)
		folio_mod.void_charge(self.extra, out["moved_row"], "UT not ours")
		again = folio_mod.split_charge(self.main, out["kept_row"], self.extra, amount=150)
		for f in (self.main, self.extra):
			self.assertEqual(bridge_void_refusals(f), [], f)
			self.assertEqual(ledger_net(f), bal(f), f)
		self.assertEqual((again["kept"], again["moved"]), (300, 150))

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

	def test_a_negative_amount_is_refused_not_turned_positive(self):
		row = self.post(self.main, amount=1000)
		with self.assertRaises(frappe.ValidationError):
			folio_mod.split_charge(self.main, row, self.extra, amount=-300)
		self.assertEqual(self.lines(self.extra), [])
		self.assertEqual([c.name for c in self.lines(self.main)], [row])

	# 2. the currency's minor unit, tax included ---------------------------

	def test_tax_inclusive_night_splits_its_price_exactly(self):
		night = self.taxed_night(5000, 12)
		gross, tax = night.total, night.gst_amount
		folio_mod.split_charge(self.main, night.name, self.extra, percent=30)
		(moved,) = [c for c in self.lines(self.extra) if c.charge_type == "Room"]
		# the share moved is 30% of the price to the minor unit, tax inside it;
		# the rest of the price, to the last fraction, stays
		self.assertAlmostEqual(moved.total, 1500, places=6)
		kept = next(c for c in self.standing(night.name) if c.parent == self.main)
		self.assertAlmostEqual(kept.total, gross - 1500, places=6)
		self.assert_conserved(night.name, amount=night.amount, tax=tax, total=gross)

	def test_r7_tax_inclusive_night_split_by_amount_and_kwd_conserves_exact_tax(self):
		# 1. INR 5000 @ 12% tax-inclusive night (base=4464.29, tax=535.7148) split by amount
		night = self.taxed_night(5000, 12)
		folio_mod.split_charge(self.main, night.name, self.extra, amount=1000)
		self.assert_conserved(night.name, amount=night.amount, tax=night.gst_amount, total=night.total)

		# 2. KWD (3dp) 50.125 @ 12% tax-inclusive night split by percent=30 conserves exact base/tax/total
		make_property(PROP_A, business_date=TODAY, country="Kuwait", currency="KWD", rates_include_tax=1)
		kwd_rt = make_room_type(PROP_A, "KINC12", base_price=50.125, tax_percent=12)
		self.res = make_reservation(PROP_A, kwd_rt, d(-1), d(1), status="Checked In",
		                            room=make_room(PROP_A, kwd_rt, "104"))
		self.main = guest_folio(self.res.name)
		self.extra = folio_mod.split_folio(self.res.name, "Extra")
		folio_mod.post_room_night(frappe.get_doc("Reservation", self.res.name), d(-1))
		kwd_night = next(c for c in self.lines(self.main) if c.charge_type == "Room")
		folio_mod.split_charge(self.main, kwd_night.name, self.extra, percent=30)
		self.assert_conserved(
			kwd_night.name,
			amount=kwd_night.amount,
			tax=kwd_night.gst_amount,
			total=kwd_night.total,
		)

	def test_a_shilling_night_splits_to_whole_shillings(self):
		night = self.taxed_night(500000, 18, currency="UGX")
		folio_mod.split_charge(self.main, night.name, self.extra, percent=33.3333)
		(moved,) = [c for c in self.lines(self.extra) if c.charge_type == "Room"]
		# UGX has no minor unit: the part split off, and its tax, are whole
		# shillings; the night keeps the rest of both
		self.assertEqual(Decimal(str(moved.total)), Decimal("166667"))
		self.assertEqual(Decimal(str(moved.gst_amount)) % 1, 0)
		self.assert_conserved(night.name, amount=night.amount, tax=night.gst_amount,
		                      total=night.total)
		self.assert_bills_add_up(night, [self.main, self.extra], "1")

	def test_a_three_way_share_of_a_shilling_night_mints_nothing(self):
		night = self.taxed_night(500000, 18, currency="UGX")
		bills = [b["folio"] for b in folio_mod.open_guest_bills(
			self.res.name, ["UT Second Guest", "UT Third Guest"])]
		folio_mod.share_charges(self.main, [night.name], bills)
		parts = [c for f in bills for c in self.lines(f)]
		# each guest's share is a third of the price, tax inside it, in whole
		# shillings; the main bill keeps the rest of the price and the tax
		self.assertEqual(sorted(Decimal(str(c.total)) for c in parts),
		                 [Decimal("166667"), Decimal("166667")])
		self.assertTrue(all(Decimal(str(c.gst_amount)) % 1 == 0 for c in parts))
		self.assert_bills_add_up(night, [self.main, *bills], "1")
		for f in (self.main, *bills):
			self.assertEqual(bridge_void_refusals(f), [], f)

	def test_a_three_way_share_keeps_tax_and_gross_to_the_cent(self):
		row = self.post(self.main, amount=100, gst_rate=5)
		bills = [b["folio"] for b in folio_mod.open_guest_bills(
			self.res.name, ["UT Second Guest", "UT Third Guest"])]
		folio_mod.share_charges(self.main, [row], bills)
		docs = [frappe.get_doc("Folio", f) for f in (self.main, *bills)]
		self.assertEqual([rounded(f.grand_total, "0.01") for f in docs],
		                 [Decimal("35.00")] * 3)
		self.assertEqual(sum(rounded(f.tax_total, "0.01") for f in docs), Decimal("5.00"))
		self.assertEqual(sum(rounded(f.charges_total, "0.01") for f in docs),
		                 Decimal("100.00"))

	def test_a_quantity_split_of_a_priced_with_tax_line_splits_its_price(self):
		make_property(PROP_A, business_date=TODAY, rates_include_tax=1, currency="UGX")
		gross = Decimal(300000)
		row = self.post(self.main, charge_type="Room", description="UT 3 nights", qty=3,
		                amount=float(gross / Decimal("1.18")),
		                rate=float(gross / 3 / Decimal("1.18")), gst_rate=18, auto_posted=1)
		out = folio_mod.split_charge(self.main, row, self.extra, qty=1)
		moved = next(c for c in self.lines(self.extra) if c.name == out["moved_row"])
		# one night of three is a third of the price, to the shilling
		self.assertEqual(Decimal(str(moved.total)), Decimal(100000))
		self.assertEqual(Decimal(str(moved.gst_amount)) % 1, 0)
		original = next(c for c in self.lines(self.main) if c.name == row)
		self.assert_bills_add_up(original, [self.main, self.extra], "1")

	# 3. what never splits -------------------------------------------------

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

	# 4. reports count a split once ----------------------------------------

	def test_splitting_a_banquet_line_adds_no_folio_revenue(self):
		row = self.post(self.main, charge_type="Misc", description="EVT-2026-0001 Banquet Hall",
		                amount=10000)
		before = (audit_report._folio(PROP_A, TODAY, TODAY), books._revenue(PROP_A, TODAY, TODAY))
		folio_mod.split_charge(self.main, row, self.extra, percent=50)
		self.assertEqual((audit_report._folio(PROP_A, TODAY, TODAY),
		                  books._revenue(PROP_A, TODAY, TODAY)), before)

	def test_splitting_a_long_outlet_bill_keeps_its_order_marker(self):
		desc = ("UT Restaurant " + "x" * 115 + " (ORD-0001)")[:140]
		self.assertTrue(desc.endswith("(ORD-0001)"))
		row = self.post(self.main, charge_type="Food & Beverage", description=desc, amount=800)
		before = audit_report._folio(PROP_A, TODAY, TODAY)
		folio_mod.split_charge(self.main, row, self.extra, percent=25)
		self.assertEqual(audit_report._folio(PROP_A, TODAY, TODAY), before)
		self.assertTrue(all("(ORD-0001)" in c.description for f in (self.main, self.extra)
		                    for c in self.lines(f)))

	def test_the_bill_shows_a_split_once(self):
		row = self.post(self.main, amount=1000)
		folio_mod.split_charge(self.main, row, self.extra, percent=30)
		inv = api.folio_invoice(self.main)
		by_row = {ln["row"]: ln for ln in inv["lines"]}
		reversal = next(c for c in self.lines(self.main) if c.get("reversal_of") == row)
		self.assertEqual(by_row[reversal.name]["reversal_of"], row)
		self.assertEqual(by_row[reversal.name]["split_of"], row)
		self.assertTrue(by_row[reversal.name]["void_reason"])
		(laundry,) = [h for h in inv["summary_by_head"] if h["head"] == "Laundry"]
		self.assertEqual((laundry["lines"], laundry["amount"]), (1, 700))

	# 5. moving after a split ----------------------------------------------

	def test_a_bulk_move_leaves_the_void_pair_where_it_was_posted(self):
		row = self.post(self.main, amount=1000)
		other = self.post(self.main, amount=200)
		folio_mod.split_charge(self.main, row, self.main, percent=40)
		every = [c.name for c in self.lines(self.main)]
		out = api.transfer_folio_charges(self.main, every, self.extra)
		self.assertEqual(out["moved"], 3)  # the two parts and the other line
		left = self.lines(self.main)
		self.assertEqual(sorted(c.name for c in left),
		                 sorted([row, next(c.name for c in left if c.get("reversal_of"))]))
		self.assertEqual((bal(self.main), bal(self.extra)), (0, 1200))
		self.assertNotIn(other, [c.name for c in left])
		for f in (self.main, self.extra):
			self.assertEqual(bridge_void_refusals(f), [], f)
			self.assertEqual(ledger_net(f), bal(f), f)
		# asked to move only the void pair, nothing would move: refused
		pair = [c.name for c in self.lines(self.main)]
		with self.assertRaisesRegex(frappe.ValidationError, "voided"):
			api.transfer_folio_charges(self.main, pair, self.extra)

	def test_the_company_changes_after_a_split(self):
		corp = _corp("UT Split Corp")
		api.change_stay_company(self.res.name, "UT company booking", company=corp)
		bill = folio_mod.open_company_folio(frappe.get_doc("Reservation", self.res.name))
		room = self.post(bill, charge_type="Room", description="UT night", amount=5000,
		                 auto_posted=1)
		folio_mod.split_charge(bill, room, self.main, percent=20)
		# the company drops out: everything live on its bill goes to the guest
		api.change_stay_company(self.res.name, "UT pays himself", company=None)
		self.assertEqual(bal(bill), 0)
		self.assertEqual(bal(self.main), 5000)
		# and comes back: the room lines route to it again, the void pair stays
		api.change_stay_company(self.res.name, "UT company again", company=corp)
		self.assertEqual(sum(c.amount for c in self.lines(self.main)
		                     if c.charge_type == "Room"), 0)
		for f in self.folios():
			self.assertEqual(bridge_void_refusals(f), [], f)
			self.assertEqual(ledger_net(f), bal(f), f)

	def test_two_accounts_are_never_the_same_stay(self):
		a, b = (_account_folio(), _account_folio())
		doc = frappe.get_doc("Folio", a)
		doc.append("charges", {"posting_date": TODAY, "charge_type": "Misc",
		                       "description": "UT account charge", "qty": 1,
		                       "rate": 100, "amount": 100})
		folio_mod._recalculate(doc)
		save_folio(doc)
		with self.assertRaisesRegex(frappe.ValidationError, "different stays"):
			folio_mod.transfer_charges(a, [doc.charges[-1].name], b)
		with self.assertRaisesRegex(frappe.ValidationError, "different stays"):
			folio_mod.split_charge(a, doc.charges[-1].name, b, percent=50)

	# 6. an outlet line moves whole and keeps its source -------------------

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


def _corp(name: str) -> str:
	if frappe.db.exists("Corporate Account", name):
		doc = frappe.get_doc("Corporate Account", name)
	else:
		doc = frappe.get_doc({"doctype": "Corporate Account", "company_name": name})
	doc.credit_allowed = 0
	doc.set("billing_rules", [{"charge_type": "Room", "pay_by": "Company"}])
	if doc.is_new():
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)
	return doc.name


def _account_folio() -> str:
	"""A bill raised on a company's account: no stay."""
	doc = frappe.get_doc({"doctype": "Folio", "property": PROP_A, "folio_type": "Account",
	                      "status": "Open"})
	folio_mod._recalculate(doc)
	doc.insert(ignore_permissions=True)
	return doc.name
