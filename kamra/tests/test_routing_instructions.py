"""Routing instructions (OPERA reservation routing): a stay's charges of
chosen charge types or transaction codes go to another window, another room
or the group's posting master, on chosen dates and days of the week, up to
an amount or percent limit. Usage is derived from the posted lines, so it
cannot drift. Liquor is just another transaction code: it routes where an
instruction or a company's billing rules include its code, like any charge
(OPERA routes only what is included; there is no alcohol switch).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from unittest.mock import patch

import frappe

from kamra import api, routing, settings
from kamra import folio as folio_mod
from kamra.tests.audit_fixtures import (
	PROP_A,
	PROP_B,
	TODAY,
	AuditTestCase,
	d,
	guest_folio,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

CORP = "UT Routing Corp"


def corp(rules=()) -> str:
	if frappe.db.exists("Corporate Account", CORP):
		doc = frappe.get_doc("Corporate Account", CORP)
	else:
		doc = frappe.get_doc({"doctype": "Corporate Account", "company_name": CORP})
	doc.credit_allowed = 1
	# a rule names a charge type, or a transaction code ("#2000")
	doc.set("billing_rules", [
		{"transaction_code": c[1:], "pay_by": p} if c.startswith("#")
		else {"charge_type": c, "pay_by": p} for c, p in rules])
	if doc.is_new():
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)
	return doc.name


def route(codes=(), *, reservation=None, group_booking=None, property=PROP_A, **fields):
	rows = [{"transaction_code": c[1:]} if c.startswith("#") else {"charge_type": c}
	        for c in codes]
	return frappe.get_doc({
		"doctype": "Routing Instruction", "property": property,
		"reservation": reservation, "group_booking": group_booking,
		"codes": rows, **fields,
	}).insert(ignore_permissions=True)


def lines(folio: str, charge_type: str | None = None) -> list:
	filters = {"parent": folio, "parenttype": "Folio", "reversal_of": ("is", "not set")}
	if charge_type:
		filters["charge_type"] = charge_type
	return frappe.get_all("Folio Charge", filters=filters,
	                      fields=["name", "amount", "posting_date", "reservation",
	                              "routing_instruction", "charge_type"],
	                      order_by="idx asc")


def used(instruction: str) -> float:
	return float(frappe.get_doc("Routing Instruction", instruction).used_amount or 0)


BAR = "UT2010"


def bar_code() -> str:
	"""The bar's own transaction code, as a hotel configures it (OPERA keeps
	food and liquor on separate codes): Food & Beverage, flagged liquor."""
	if not frappe.db.exists("Transaction Code", BAR):
		frappe.get_doc({"doctype": "Transaction Code", "code": BAR, "description": "UT Bar",
		                "txn_type": "Revenue", "revenue_group": "F&B",
		                "subgroup": "Bar", "charge_type": "Food & Beverage",
		                "is_alcohol": 1}).insert(ignore_permissions=True)
	return BAR


def rounded(value, unit="1") -> Decimal:
	return Decimal(str(value)).quantize(Decimal(unit), rounding=ROUND_HALF_UP)


class RoutingTestCase(AuditTestCase):
	properties = (PROP_A, PROP_B)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY)
		settings.clear_cache(PROP_A)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.rooms = [make_room(PROP_A, self.rt, n) for n in ("301", "302", "303")]
		self.res = self.stay(0)
		self.w2 = folio_mod.open_window(reservation=self.res.name, payee_type="Company",
		                                payee_company=corp(), payment_method="Company Credit")

	def tearDown(self):
		settings.clear_cache(PROP_A)
		super().tearDown()

	def stay(self, i, check_in=-2, check_out=2, **fields):
		return make_reservation(PROP_A, self.rt, d(check_in), d(check_out),
		                        status="Checked In", room=self.rooms[i], **fields)

	def charge(self, charge_type, amount, desc="UT charge", alcohol=0, res=None):
		return api.post_stay_charge((res or self.res).name, charge_type, desc, amount,
		                            is_alcohol=alcohol)

	def test_route_by_code_to_window(self):
		ri = route(["Laundry"], reservation=self.res.name, target_type="Window",
		           target_window=2)
		out = self.charge("Laundry", 300)
		self.assertEqual(out["folio"], self.w2)
		self.assertEqual([(r.amount, r.routing_instruction, r.reservation)
		                  for r in lines(self.w2)], [(300, ri.name, self.res.name)])
		# another code stays on window 1
		self.assertEqual(self.charge("Spa", 200)["folio"], guest_folio(self.res.name))
		# a transaction code row targets that code alone
		route(["#3100"], reservation=self.res.name, target_type="Window", target_window=2)
		self.assertEqual(self.charge("Spa", 250)["folio"], self.w2)

	def test_route_by_code_to_room(self):
		other = self.stay(1)
		route(["Laundry"], reservation=self.res.name, target_type="Room",
		      target_reservation=other.name)
		out = self.charge("Laundry", 300)
		self.assertEqual(out["folio"], guest_folio(other.name))
		# the line keeps the stay it was posted for
		self.assertEqual([r.reservation for r in lines(out["folio"], "Laundry")],
		                 [self.res.name])

	def test_route_by_code_to_group_master(self):
		gb = frappe.get_doc({"doctype": "Group Booking", "property": PROP_A,
		                     "group_name": "UT Routing Group", "check_in_date": d(-2),
		                     "check_out_date": d(2),
		                     "status": "Confirmed"}).insert(ignore_permissions=True)
		member = self.stay(1, group_booking=gb.name)
		ri = route(["Food & Beverage"], reservation=member.name, target_type="Group Master")
		out = self.charge("Food & Beverage", 500, "UT group dinner", res=member)
		pm = folio_mod.open_group_folio(gb.name)
		self.assertEqual(out["folio"], pm)
		self.assertEqual([r.routing_instruction for r in lines(pm)], [ri.name])
		# an outlet check for the same code is routed by Kamra for the bridge
		outlet = api.route_outlet_charge(member.name, "2000", 400)
		self.assertEqual((outlet["folio"], outlet["routing_instruction"],
		                  outlet["window_no"]), (pm, ri.name, 1))

	def test_route_dates_and_days_of_week(self):
		# d(-2) is a Monday; Monday to Wednesday only
		route(["Room"], reservation=self.res.name, target_type="Window", target_window=2,
		      monday=1, tuesday=1, wednesday=1, thursday=0, friday=0, saturday=0,
		      sunday=0)
		for night in (-2, -1, 0, 1):
			folio_mod.post_room_night(self.res, d(night))
		routed = sorted(str(r.posting_date) for r in lines(self.w2, "Room"))
		self.assertEqual(routed, [d(-2), d(-1), d(0)])
		self.assertEqual([str(r.posting_date) for r in
		                  lines(guest_folio(self.res.name), "Room")], [d(1)])
		# a date range: from d(1) on, the rest of the stay
		other = self.stay(1)
		w2 = folio_mod.open_window(reservation=other.name, payee_type="Company",
		                           payee_company=corp())
		route(["Room"], reservation=other.name, target_type="Window", target_window=2,
		      from_date=d(1))
		for night in (-2, 1):
			folio_mod.post_room_night(other, d(night))
		self.assertEqual([str(r.posting_date) for r in lines(w2, "Room")], [d(1)])

	def test_route_amount_limit_and_used(self):
		ri = route(["Laundry"], reservation=self.res.name, target_type="Window",
		           target_window=2, limit_type="Amount", limit_value=150000)
		for _ in range(3):
			self.charge("Laundry", 60000)
		self.assertEqual([r.amount for r in lines(self.w2)], [60000, 60000, 30000])
		self.assertEqual([r.amount for r in lines(guest_folio(self.res.name), "Laundry")],
		                 [30000])
		self.assertEqual(used(ri.name), 150000)
		folio_mod.void_charge(self.w2, lines(self.w2)[0].name, "UT wrong bag")
		self.assertEqual(used(ri.name), 90000)
		self.assertEqual(self.charge("Laundry", 60000)["folio"], self.w2)
		self.assertEqual(used(ri.name), 150000)

	def test_route_percent_ugx_precision(self):
		make_property(PROP_A, business_date=TODAY, currency="UGX")
		route(["Laundry"], reservation=self.res.name, target_type="Window",
		      target_window=2, limit_type="Percent", limit_value=33.333)
		self.charge("Laundry", 100000)
		self.assertEqual([r.amount for r in lines(self.w2)], [33333])
		self.assertEqual([r.amount for r in lines(guest_folio(self.res.name), "Laundry")],
		                 [66667])

	def test_route_per_day_limit(self):
		daily = route(["Room"], reservation=self.res.name, target_type="Window",
		              target_window=2, limit_type="Amount", limit_value=600, per_day=1)
		nightly = float(folio_mod._nightly_room_rate(self.res, d(-2)))
		self.assertGreater(nightly, 600)
		for night in (-2, -1):
			folio_mod.post_room_night(self.res, d(night))
		self.assertEqual([(str(r.posting_date), r.amount) for r in lines(self.w2, "Room")],
		                 [(d(-2), 600), (d(-1), 600)])
		self.assertEqual([r.amount for r in lines(guest_folio(self.res.name), "Room")],
		                 [nightly - 600, nightly - 600])
		# without per-day the same limit is spent once for the whole stay
		frappe.db.set_value("Routing Instruction", daily.name, "disabled", 1)
		other = self.stay(1)
		w2 = folio_mod.open_window(reservation=other.name, payee_type="Company",
		                           payee_company=corp())
		route(["Room"], reservation=other.name, target_type="Window", target_window=2,
		      limit_type="Amount", limit_value=600)
		for night in (-2, -1):
			folio_mod.post_room_night(other, d(night))
		self.assertEqual([r.amount for r in lines(w2, "Room")], [600])
		self.assertEqual([r.amount for r in lines(guest_folio(other.name), "Room")],
		                 [nightly - 600, nightly])

	def test_outlet_line_routes_whole(self):
		ri = route(["Food & Beverage"], reservation=self.res.name, target_type="Window",
		           target_window=2, limit_type="Amount", limit_value=1000)
		first = api.route_outlet_charge(self.res.name, "2000", 600)
		self.assertEqual((first["folio"], first["routing_instruction"]), (self.w2, ri.name))
		# the bridge posts the outlet check on that window, carrying the instruction
		doc = frappe.get_doc("Folio", self.w2)
		doc.append("charges", {"posting_date": TODAY, "charge_type": "Food & Beverage",
		                       "transaction_code": "2000", "description": "Outlet transfer",
		                       "qty": 1, "rate": 600, "amount": 600,
		                       "reservation": self.res.name,
		                       "routing_instruction": first["routing_instruction"]})
		folio_mod._recalculate(doc)
		doc.save(ignore_permissions=True)
		self.assertEqual(used(ri.name), 600)
		# 400 left: the next 600 check is not split, it stays whole on window 1
		second = api.route_outlet_charge(self.res.name, "2000", 600)
		self.assertEqual((second["folio"], second["routing_instruction"], second["window_no"]),
		                 (guest_folio(self.res.name), None, 1))
		# a percent never splits an outlet check either
		frappe.db.set_value("Routing Instruction", ri.name,
		                    {"limit_type": "Percent", "limit_value": 50})
		self.assertEqual(api.route_outlet_charge(self.res.name, "2000", 600)["folio"],
		                 guest_folio(self.res.name))

	def test_one_instruction_per_code_period(self):
		route(["Food & Beverage"], reservation=self.res.name, target_type="Window",
		      target_window=2, from_date=d(-2), to_date=d(0))
		with self.assertRaises(frappe.ValidationError):
			route(["Food & Beverage"], reservation=self.res.name, target_type="Window",
			      target_window=2, from_date=d(0), to_date=d(1))
		# the code's own charge type overlaps too
		with self.assertRaises(frappe.ValidationError):
			route(["#2000"], reservation=self.res.name, target_type="Window",
			      target_window=2)
		# another code, another period, or other days of the week are fine
		route(["Laundry"], reservation=self.res.name, target_type="Window", target_window=2)
		route(["Food & Beverage"], reservation=self.res.name, target_type="Window",
		      target_window=2, from_date=d(1))
		with self.assertRaises(frappe.ValidationError):
			route([], reservation=self.res.name, target_type="Window", target_window=2,
			      all_codes=1)

	def test_all_codes_no_limit(self):
		with self.assertRaises(frappe.ValidationError):
			route([], reservation=self.res.name, target_type="Window", target_window=2,
			      all_codes=1, limit_type="Amount", limit_value=1000)
		with self.assertRaises(frappe.ValidationError):
			route([], reservation=self.res.name, target_type="Window", target_window=2)
		ri = route([], reservation=self.res.name, target_type="Window", target_window=2,
		           all_codes=1)
		self.assertEqual(self.charge("Spa", 200)["folio"], self.w2)
		self.assertEqual([r.routing_instruction for r in lines(self.w2)], [ri.name])

	def test_target_same_property(self):
		make_property(PROP_B, business_date=TODAY)
		rt_b = make_room_type(PROP_B, "STD", base_price=1000)
		away = make_reservation(PROP_B, rt_b, d(-2), d(2), status="Checked In",
		                        room=make_room(PROP_B, rt_b, "901"))
		with self.assertRaises(frappe.ValidationError):
			route(["Laundry"], reservation=self.res.name, target_type="Room",
			      target_reservation=away.name)
		with self.assertRaises(frappe.ValidationError):
			route(["Laundry"], reservation=self.res.name, property=PROP_B,
			      target_type="Window", target_window=2)
		# exactly one source: a stay or a group
		with self.assertRaises(frappe.ValidationError):
			route(["Laundry"], target_type="Window", target_window=2)

	def test_liquor_routes_by_its_code_like_any_charge(self):
		# no alcohol switch on an instruction or a group (OPERA has none)
		self.assertFalse(frappe.get_meta("Routing Instruction").has_field("include_alcohol"))
		self.assertFalse(frappe.get_meta("Group Booking").has_field("route_alcohol"))
		bar = bar_code()
		ri = route(["Food & Beverage"], reservation=self.res.name, target_type="Window",
		           target_window=2)
		# an instruction for Food & Beverage routes every code of it, liquor too
		self.assertEqual(self.charge("Food & Beverage", 800, "UT whisky", alcohol=1)["folio"],
		                 self.w2)
		out = api.route_outlet_charge(self.res.name, bar, 500)
		self.assertEqual((out["folio"], out["routing_instruction"]), (self.w2, ri.name))
		# one for the food code alone leaves the bar's code on window 1
		frappe.db.set_value("Routing Instruction", ri.name, "disabled", 1)
		food = route(["#2000"], reservation=self.res.name, target_type="Window",
		             target_window=2)
		self.assertEqual(api.route_outlet_charge(self.res.name, bar, 500)["folio"],
		                 guest_folio(self.res.name))
		self.assertEqual(api.route_outlet_charge(self.res.name, "2000", 500)
		                 ["routing_instruction"], food.name)

	def test_company_rules_name_codes_and_liquor_has_no_default(self):
		# Neon R1 P0-1: the bar's code (is_alcohol on the code) leaked onto the
		# company through the presets. The presets now follow the company's
		# rules and codes only: what they include is billed, nothing else.
		bar = bar_code()
		company = corp([("#2000", "Company")])
		stay = self.stay(1, company=company, booking_type="Corporate")
		billed = folio_mod.open_company_folio(stay)
		self.assertEqual(api.route_outlet_charge(stay.name, "2000", 500)["folio"], billed)
		self.assertEqual(api.route_outlet_charge(stay.name, bar, 500)["folio"],
		                 guest_folio(stay.name))
		# a company that pays Food & Beverage pays all of it, liquor included
		corp([("Food & Beverage", "Company")])
		self.assertEqual(api.route_outlet_charge(stay.name, bar, 500)["folio"], billed)
		self.assertEqual(self.charge("Food & Beverage", 800, "UT wine", alcohol=1,
		                             res=stay)["folio"], billed)
		# the rules editor round-trips a code rule
		api.set_billing_rules(company, [{"transaction_code": "2000", "pay_by": "Company"},
		                                {"charge_type": "Room", "pay_by": "Company"}])
		self.assertEqual(sorted((r.get("transaction_code") or "", r.get("charge_type") or "")
		                        for r in api.get_billing_rules(company)),
		                 [("", "Room"), ("2000", "")])

	def test_liquor_lines_post_move_split_and_correct_like_any_line(self):
		# the hard-coded liquor refusals are gone: a company or group that does
		# not pay for liquor simply routes no liquor code
		gb = frappe.get_doc({"doctype": "Group Booking", "property": PROP_A,
		                     "group_name": "UT Liquor Group", "check_in_date": d(-2),
		                     "check_out_date": d(2),
		                     "status": "Confirmed"}).insert(ignore_permissions=True)
		member = self.stay(1, group_booking=gb.name)
		beer = self.charge("Food & Beverage", 300, "UT beer", alcohol=1, res=member)
		pm = folio_mod.open_group_folio(gb.name)
		folio_mod.transfer_charges(beer["folio"], [lines(beer["folio"])[-1].name], pm)
		self.assertEqual([r.amount for r in lines(pm)], [300])
		api._post_folio_charge(self.w2, "Food & Beverage", "UT wine", 900, is_alcohol=1)
		wine = lines(self.w2)[-1].name
		api.correct_folio_charge(self.w2, wine, reason="UT wrong bottle", amount=700)
		self.assertEqual(sorted(r.amount for r in lines(self.w2)), [-900, 700, 900])
		whisky = self.charge("Food & Beverage", 1000, "UT whisky", alcohol=1)
		row = lines(whisky["folio"])[-1].name
		folio_mod.split_charge(whisky["folio"], row, self.w2, percent=40)
		self.assertIn(400, [r.amount for r in lines(self.w2)])

	# Neon R1 P0-2: usage follows corrections and splits exactly --------

	def test_correcting_a_routed_line_moves_its_usage(self):
		ri = route(["Laundry"], reservation=self.res.name, target_type="Window",
		           target_window=2, limit_type="Amount", limit_value=150000)
		self.charge("Laundry", 60000)
		api.correct_folio_charge(self.w2, lines(self.w2)[0].name, reason="UT one bag",
		                         amount=20000)
		# no stale usage: the corrected line counts, the original no longer does
		self.assertEqual(used(ri.name), 20000)
		self.charge("Laundry", 130000)
		self.assertEqual(used(ri.name), 150000)
		self.assertEqual(sum(r.amount for r in lines(self.w2)), 150000)
		# a correction up: usage follows it, and no limit is spent twice
		spa = route(["Spa"], reservation=self.res.name, target_type="Window",
		            target_window=2, limit_type="Amount", limit_value=100000)
		self.charge("Spa", 40000)
		row = lines(self.w2, "Spa")[0].name
		api.correct_folio_charge(self.w2, row, reason="UT full treatment", amount=100000)
		self.assertEqual(used(spa.name), 100000)
		self.assertEqual(self.charge("Spa", 60000)["folio"], guest_folio(self.res.name))
		self.assertEqual(sum(r.amount for r in lines(self.w2, "Spa")), 100000)
		# voiding the corrected line frees the limit again
		repost = next(r for r in lines(self.w2, "Spa") if r.amount == 100000)
		folio_mod.void_charge(self.w2, repost.name, "UT not taken")
		self.assertEqual(used(spa.name), 0)

	def test_a_correction_counts_on_the_day_of_the_line_it_corrects(self):
		ri = route(["Laundry"], reservation=self.res.name, target_type="Window",
		           target_window=2, limit_type="Amount", limit_value=50000, per_day=1)
		self.charge("Laundry", 50000)
		frappe.db.set_value("Property", PROP_A, "business_date", d(1))
		frappe.clear_document_cache("Property", PROP_A)
		api.correct_folio_charge(self.w2, lines(self.w2)[0].name, reason="UT one bag",
		                         amount=30000)
		self.assertEqual((routing.used(ri.name, TODAY), routing.used(ri.name, d(1))),
		                 (30000, 0))

	def test_splitting_a_routed_line_keeps_usage_on_what_stays(self):
		ri = route(["Laundry"], reservation=self.res.name, target_type="Window",
		           target_window=2, limit_type="Amount", limit_value=100000)
		self.charge("Laundry", 100000)
		row = lines(self.w2)[0].name
		guest = guest_folio(self.res.name)
		folio_mod.split_charge(self.w2, row, guest, percent=20)
		# the part left on the company's window is what it pays: 80,000
		self.assertEqual(used(ri.name), 80000)
		self.charge("Laundry", 100000)
		self.assertEqual(used(ri.name), 100000)
		# the split line and its reversal net to nothing and stay on the bill
		pairs = folio_mod.void_pairs(frappe.get_doc("Folio", self.w2))
		self.assertEqual(sum(r.amount for r in lines(self.w2) if r.name not in pairs), 100000)
		# a split that stays on the window keeps both parts routed
		other = lines(self.w2)[-1].name
		folio_mod.split_charge(self.w2, other, self.w2, percent=50)
		self.assertEqual(used(ri.name), 100000)

	# Neon R1 P0-3: routed parts in the currency's minor unit, tax kept --

	def ugx_night(self, price=500000, tax=18):
		make_property(PROP_A, business_date=TODAY, rates_include_tax=1, currency="UGX")
		settings.clear_cache(PROP_A)
		taxed = make_room_type(PROP_A, f"UGX{tax}", base_price=price, tax_percent=tax)
		res = make_reservation(PROP_A, taxed, d(-1), d(1), status="Checked In",
		                       room=make_room(PROP_A, taxed, "309"))
		w2 = folio_mod.open_window(reservation=res.name, payee_type="Company",
		                           payee_company=corp())
		return res, w2

	def assert_bills_add_up(self, folios, gross, tax):
		docs = [frappe.get_doc("Folio", f) for f in folios]
		self.assertEqual(sum(rounded(f.grand_total) for f in docs), rounded(gross))
		self.assertEqual(sum(rounded(f.tax_total) for f in docs), rounded(tax))

	def test_percent_routing_of_a_tax_inclusive_shilling_night(self):
		res, w2 = self.ugx_night()
		route(["Room"], reservation=res.name, target_type="Window", target_window=2,
		      limit_type="Percent", limit_value=33.333)
		folio_mod.post_room_night(res, d(-1))
		(routed,) = lines(w2, "Room")
		routed = frappe.get_doc("Folio Charge", routed.name)
		(rest,) = [frappe.get_doc("Folio Charge", r.name)
		           for r in lines(guest_folio(res.name), "Room")]
		# 33.333% of the 500,000 price, tax inside it, in whole shillings
		self.assertEqual(Decimal(str(routed.total)), Decimal("166665"))
		self.assertEqual(Decimal(str(routed.gst_amount)) % 1, 0)
		self.assertEqual(Decimal(str(routed.amount)) % 1, 0)
		self.assertEqual(rounded(rest.total), Decimal("333335"))
		self.assertAlmostEqual(routed.total + rest.total, 500000, places=2)
		self.assert_bills_add_up([w2, guest_folio(res.name)], 500000,
		                         routed.gst_amount + rest.gst_amount)
		self.assertEqual(rounded(routed.gst_amount + rest.gst_amount),
		                 rounded(Decimal(500000) * 18 / 118))

	def test_amount_limit_caps_the_gross_of_a_tax_inclusive_night(self):
		# OPERA: an inclusive code routes its value with its taxes, so the
		# limit caps what the company is billed, tax included
		res, w2 = self.ugx_night()
		ri = route(["Room"], reservation=res.name, target_type="Window", target_window=2,
		           limit_type="Amount", limit_value=200000, per_day=1)
		folio_mod.post_room_night(res, d(-1))
		(routed,) = [frappe.get_doc("Folio Charge", r.name) for r in lines(w2, "Room")]
		(rest,) = [frappe.get_doc("Folio Charge", r.name)
		           for r in lines(guest_folio(res.name), "Room")]
		self.assertEqual(Decimal(str(routed.total)), Decimal("200000"))
		self.assertEqual(rounded(rest.total), Decimal("300000"))
		self.assertEqual(routing.used(ri.name, d(-1)), 200000)

	def test_percent_routing_conserves_tax_in_shillings(self):
		make_property(PROP_A, business_date=TODAY, currency="UGX")
		settings.clear_cache(PROP_A)
		route(["Laundry"], reservation=self.res.name, target_type="Window",
		      target_window=2, limit_type="Percent", limit_value=33.333)
		api.post_stay_charge(self.res.name, "Laundry", "UT laundry", 100003, gst_rate=18)
		guest = guest_folio(self.res.name)
		routed = frappe.get_doc("Folio Charge", lines(self.w2)[0].name)
		self.assertEqual((routed.amount, routed.gst_amount), (33334, 6000))
		# the bills' own rounded tax adds up to the unsplit line's: 18,001
		self.assert_bills_add_up([self.w2, guest], 100003 * Decimal("1.18"),
		                         100003 * Decimal("0.18"))

	def test_routing_cuts_a_line_as_split_charge_does(self):
		# one splitter: a night routed 33.333% and the same night split
		# 33.333% by hand come out to the same parts, to the shilling
		res, w2 = self.ugx_night()
		route(["Room"], reservation=res.name, target_type="Window", target_window=2,
		      limit_type="Percent", limit_value=33.333)
		folio_mod.post_room_night(res, d(-1))
		routed = frappe.get_doc("Folio Charge", lines(w2, "Room")[0].name)
		taxed = frappe.db.get_value("Reservation", res.name, "room_type")
		other = make_reservation(PROP_A, taxed, d(-1), d(1), status="Checked In",
		                         room=make_room(PROP_A, taxed, "310"))
		folio_mod.post_room_night(other, d(-1))
		night = lines(guest_folio(other.name), "Room")[0].name
		extra = folio_mod.split_folio(other.name, "Extra")
		folio_mod.split_charge(guest_folio(other.name), night, extra, percent=33.333)
		cut = frappe.get_doc("Folio Charge", lines(extra, "Room")[0].name)
		self.assertEqual((routed.amount, routed.gst_amount, routed.total),
		                 (cut.amount, cut.gst_amount, cut.total))

	# Neon R1 P1-5: overlap is by code and by active day ------------------

	def test_disjoint_codes_of_one_charge_type_route_to_different_windows(self):
		bar = bar_code()
		w3 = folio_mod.open_window(reservation=self.res.name, payee_type="Guest")
		food = route(["#2000"], reservation=self.res.name, target_type="Window",
		             target_window=2)
		drinks = route([f"#{bar}"], reservation=self.res.name, target_type="Window",
		               target_window=3)
		self.assertEqual(api.route_outlet_charge(self.res.name, "2000", 400)
		                 ["routing_instruction"], food.name)
		out = api.route_outlet_charge(self.res.name, bar, 400)
		self.assertEqual((out["folio"], out["routing_instruction"]), (w3, drinks.name))
		# the charge type covers both codes, so it still overlaps either
		with self.assertRaises(frappe.ValidationError):
			route(["Food & Beverage"], reservation=self.res.name, target_type="Window",
			      target_window=2)

	def test_overlap_needs_a_day_active_in_both(self):
		week = dict(monday=1, tuesday=0, wednesday=0, thursday=1, friday=0, saturday=0,
		            sunday=0)
		# d(-2) is a Monday: the two ranges share only d(0), a Wednesday,
		# which neither instruction runs on
		route(["Laundry"], reservation=self.res.name, target_type="Window", target_window=2,
		      from_date=d(-2), to_date=d(0), **week)
		route(["Laundry"], reservation=self.res.name, target_type="Window", target_window=2,
		      from_date=d(0), to_date=d(1), **week)
		# a Thursday in both ranges (d(1)) is a real overlap
		with self.assertRaises(frappe.ValidationError):
			route(["Laundry"], reservation=self.res.name, target_type="Window",
			      target_window=2, from_date=d(1), to_date=d(5), **week)
		route(["Spa"], reservation=self.res.name, target_type="Window", target_window=2,
		      from_date=d(-2), to_date=d(-2), **week)
		with self.assertRaises(frappe.ValidationError):
			route(["Spa"], reservation=self.res.name, target_type="Window", target_window=2,
			      from_date=d(-9), to_date=d(-2), **{**week, "thursday": 0})

	# Neon R1 P1-6: a matched instruction never falls back to the presets -

	def test_a_checked_out_target_room_keeps_the_charge_on_window_1(self):
		company = corp([("Food & Beverage", "Company")])
		stay = self.stay(1, company=company, booking_type="Corporate")
		other = self.stay(2)
		ri = route(["Food & Beverage"], reservation=stay.name, target_type="Room",
		           target_reservation=other.name)
		self.assertEqual(self.charge("Food & Beverage", 500, "UT dinner", res=stay)["folio"],
		                 guest_folio(other.name))
		frappe.db.set_value("Reservation", other.name, "status", "Checked Out")
		out = self.charge("Food & Beverage", 500, "UT late dinner", res=stay)
		self.assertEqual(out["folio"], guest_folio(stay.name))
		self.assertEqual(out["parts"][0]["routing_instruction"], None)
		self.assertFalse(frappe.db.exists("Folio Charge", {"routing_instruction": ri.name,
		                                                   "description": "UT late dinner"}))

	# Neon R1 P1-10: a room-service order is routed with its amount -------

	def test_a_room_service_order_routes_by_its_limit_and_carries_it(self):
		from kamra import pos
		ri = route(["Food & Beverage"], reservation=self.res.name, target_type="Window",
		           target_window=2, limit_type="Amount", limit_value=1000)
		outlet = frappe.get_doc({"doctype": "POS Outlet", "property": PROP_A,
		                         "outlet_name": "UT Routing Kitchen",
		                         "outlet_type": "Room Service",
		                         "gst_rate": 0}).insert(ignore_permissions=True).name
		dish = frappe.get_doc({"doctype": "Menu Item", "property": PROP_A, "outlet": outlet,
		                       "item_name": "UT Club Sandwich", "category": "Food",
		                       "price": 400, "is_veg": 1, "available": 1,
		                       "prep_station": "Kitchen"}).insert(ignore_permissions=True).name
		order = pos.create_order(outlet, [{"menu_item": dish, "qty": 1}],
		                         room=self.rooms[0], order_type="Room Service")["order"]
		self.assertTrue(pos.deliver_order(order)["posted_to_folio"])
		line = frappe.get_all("Folio Charge", filters={"description": ["like", f"%{order}%"]},
		                      fields=["parent", "routing_instruction", "amount"])
		self.assertEqual([(r.parent, r.routing_instruction, r.amount) for r in line],
		                 [(self.w2, ri.name, 400)])
		self.assertEqual(used(ri.name), 400)

	# Neon R1 P2-12/13: the outlet lookup posts nothing; one route ------

	def test_the_outlet_lookup_opens_no_bill_and_checks_its_code(self):
		other = self.stay(1)
		route(["Food & Beverage"], reservation=other.name, target_type="Window",
		      target_window=2)
		before = frappe.db.count("Folio")
		out = api.route_outlet_charge(other.name, "2000", 400, dry_run=1)
		self.assertEqual((out["folio"], out["window_no"]), (None, 2))
		self.assertEqual(frappe.db.count("Folio"), before)
		self.assertTrue(api.route_outlet_charge(other.name, "2000", 400)["folio"])
		frappe.db.set_value("Transaction Code", bar_code(), "disabled", 1)
		with self.assertRaises(frappe.ValidationError):
			api.route_outlet_charge(other.name, BAR, 400)
		with self.assertRaises(frappe.ValidationError):
			api.route_outlet_charge(other.name, "9001", 400)

	def test_an_unrouted_line_is_routed_once(self):
		with patch("kamra.routing.route", wraps=routing.route) as spy:
			folio_mod.line_parts(self.res, "Laundry", 300)
		self.assertEqual(spy.call_count, 1)

	def test_pays_own_charges_on_company_group(self):
		gb = frappe.get_doc({"doctype": "Group Booking", "property": PROP_A,
		                     "group_name": "UT Opt Out Group",
		                     "company": corp([("Room", "Company")]),
		                     "check_in_date": d(-2), "check_out_date": d(2),
		                     "status": "Confirmed"}).insert(ignore_permissions=True)
		member = self.stay(1, group_booking=gb.name)
		api.set_pays_own_charges(member.name, 1)
		folio_mod.post_room_night(frappe.get_doc("Reservation", member.name), d(-2))
		self.assertEqual(len(lines(guest_folio(member.name), "Room")), 1)
		self.assertFalse(frappe.db.exists("Folio", {"group_booking": gb.name,
		                                            "folio_type": "Group"}))

	def test_group_template_copied_on_pickup(self):
		gb = frappe.get_doc({
			"doctype": "Group Booking", "property": PROP_A,
			"group_name": "UT Template Group", "check_in_date": d(1),
			"check_out_date": d(3), "status": "Confirmed",
			"blocks": [{"room_type": self.rt, "rooms_blocked": 2}],
		}).insert(ignore_permissions=True)
		template = route(["Laundry"], group_booking=gb.name, target_type="Group Master",
		                 limit_type="Amount", limit_value=1000)
		picked = [api.pickup_group_room(gb.name, self.rt, f"UT Pickup {i}")["reservation"]
		          for i in (1, 2)]
		copies = {r: frappe.get_all(
			"Routing Instruction", filters={"reservation": r},
			fields=["name", "source_instruction", "target_type", "limit_value",
			        "group_booking"]) for r in picked}
		for r in picked:
			self.assertEqual(
				[(c.source_instruction, c.target_type, c.limit_value, c.group_booking)
				 for c in copies[r]],
				[(template.name, "Group Master", 1000, None)], r)
		pm = folio_mod.open_group_folio(gb.name)
		first, second = (frappe.get_doc("Reservation", r) for r in picked)
		for res in (first, second, first):
			api.post_stay_charge(res.name, "Laundry", "UT laundry", 800)
		self.assertEqual(sorted(r.amount for r in lines(pm)), [200, 800, 800])
		self.assertEqual((used(copies[first.name][0].name), used(copies[second.name][0].name),
		                  used(template.name)), (1000, 800, 0))

	def test_split_night_is_one_room_sold(self):
		# a limit splits one room night over two windows: still one room sold
		route(["Room"], reservation=self.res.name, target_type="Window", target_window=2,
		      limit_type="Amount", limit_value=600, per_day=1)
		nightly = float(folio_mod._nightly_room_rate(self.res, d(-1)))
		folio_mod.post_room_night(self.res, d(-1))
		self.assertEqual(len(lines(self.w2, "Room")) + len(
			lines(guest_folio(self.res.name), "Room")), 2)
		yesterday = api.owner_briefing(PROP_A, TODAY)["yesterday"]
		self.assertEqual(yesterday["room_revenue"], nightly)
		self.assertEqual(yesterday["adr"], round(nightly, 0))

	def test_billing_rule_all_charge_types(self):
		meta = frappe.get_meta("Folio Charge").get_field("charge_type").options.split("\n")
		rule = frappe.get_meta("Company Billing Rule").get_field("charge_type").options.split("\n")
		# a blank first option: a rule that names a transaction code has no type
		self.assertEqual(set(rule) - {""}, set(meta) - {"Allowance", "Opening Balance"})
		make_property(PROP_A, business_date=TODAY, room_levy_percent=10)
		other = self.stay(1, company=corp([("Room Levy", "Company")]),
		                  booking_type="Corporate")
		folio_mod.post_room_night(other, d(-2))
		company = folio_mod.open_company_folio(other)
		self.assertEqual([r.charge_type for r in lines(company)], ["Room Levy"])
		self.assertEqual([r.charge_type for r in lines(guest_folio(other.name))], ["Room"])

	def test_no_desk_field_or_agent_tool_promises_a_liquor_rule(self):
		# liquor routes like any code: no Desk help text or agent tool may
		# still promise that it never reaches a company bill
		import re

		from kamra import mcp_tools
		promise = re.compile(r"\b(liquor|alcohol)\b[^.;:]*\b(never|always)\b"
		                     r"|\b(never|always)\b[^.;:]*\b(liquor|alcohol)\b", re.I)
		found = [f"{dt}.{df.fieldname}"
		         for dt in frappe.get_all("DocType", filters={"module": "Kamra"}, pluck="name")
		         for df in frappe.get_meta(dt).fields
		         if promise.search(df.description or "")]
		found += [t.name for t in mcp_tools.TOOLS if promise.search(t.description)]
		self.assertEqual(found, [])

	def test_a_code_rule_carries_no_charge_type_and_is_shown_by_its_code(self):
		# made as Desk makes it: a Select would take its first option, "Room"
		company = corp([("#2000", "Company")])
		self.assertEqual([(r.charge_type or None, r.transaction_code) for r in
		                  frappe.get_doc("Corporate Account", company).billing_rules],
		                 [(None, "2000")])
		stay = self.stay(1, company=company, booking_type="Corporate")
		from kamra import checkin, checkout
		shown = {"charge_type": None, "transaction_code": "2000",
		         "code_label": "2000 Food & Beverage", "pay_by": "Company"}
		self.assertEqual(checkin._billing(stay)["billing_rules"], [shown])
		self.assertEqual(checkout._checkout_context(stay.name)["billing"]["rules"], [shown])
