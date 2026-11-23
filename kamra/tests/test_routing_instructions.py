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

	# Hydrogen R2 P0-1(i): configurable alcohol/beverage transaction codes -

	def test_beverage_code_2010_is_seeded_and_configurable(self):
		from kamra import ledger
		ledger.ensure_default_transaction_codes()
		tc = frappe.db.get_value(
			"Transaction Code", "2010",
			["code", "txn_type", "charge_type", "is_alcohol", "disabled"], as_dict=True)
		self.assertIsNotNone(tc, "default beverage transaction code 2010 must be seeded")
		self.assertEqual((tc.txn_type, tc.charge_type, tc.is_alcohol, tc.disabled),
		                 ("Revenue", "Food & Beverage", 1, 0))
		for dt in ("POS Outlet", "Menu Item", "Banquet Service Item"):
			df = frappe.get_meta(dt).get_field("transaction_code")
			self.assertIsNotNone(df, f"{dt} must have transaction_code field")
			self.assertEqual((df.fieldtype, df.options), ("Link", "Transaction Code"))
		self.assertEqual(settings.get_setting(PROP_A, "beverage_transaction_code"), "2010")
		bar = bar_code()
		settings.write_kv(PROP_A, "beverage_transaction_code", bar)
		settings.clear_cache(PROP_A)
		self.assertEqual(ledger.beverage_transaction_code(PROP_A), bar)
		settings.write_kv(PROP_A, "beverage_transaction_code", "2010")
		settings.clear_cache(PROP_A)

	def test_pos_beer_posts_under_beverage_code_and_stays_off_food_only_company(self):
		from kamra import pos
		food_only = corp([("#2000", "Company")])
		stay = self.stay(1, company=food_only, booking_type="Corporate")
		outlet = frappe.get_doc({
			"doctype": "POS Outlet", "property": PROP_A,
			"outlet_name": "UT Grill & Bar", "outlet_type": "Bar", "gst_rate": 15,
		}).insert(ignore_permissions=True).name
		beer = frappe.get_doc({
			"doctype": "Menu Item", "property": PROP_A, "outlet": outlet,
			"item_name": "Nile Special Beer", "category": "Alcohol",
			"price": 15000, "is_alcohol": 1, "available": 1, "prep_station": "Bar",
		}).insert(ignore_permissions=True).name
		tilapia = frappe.get_doc({
			"doctype": "Menu Item", "property": PROP_A, "outlet": outlet,
			"item_name": "Grilled Tilapia", "category": "Food",
			"price": 60000, "is_alcohol": 0, "available": 1, "prep_station": "Kitchen",
		}).insert(ignore_permissions=True).name
		# Hydrogen R2 probe: beer (is_alcohol=1) posts as 2010 -> Guest W1, NOT FoodOnly2000
		ord_beer = pos.create_order(outlet, [{"menu_item": beer, "qty": 1}],
		                            room=self.rooms[1], order_type="Room Service")["order"]
		pos.deliver_order(ord_beer)
		beer_rows = frappe.get_all(
			"Folio Charge",
			filters={"reservation": stay.name, "description": ("like", f"%({ord_beer})")},
			fields=["parent", "transaction_code", "is_alcohol", "amount"])
		self.assertEqual(len(beer_rows), 1)
		self.assertEqual(
			(frappe.db.get_value("Folio", beer_rows[0].parent, "folio_type"),
			 beer_rows[0].transaction_code, beer_rows[0].is_alcohol),
			("Guest", "2010", 1))
		# Food item posts as 2000 -> Company
		ord_food = pos.create_order(outlet, [{"menu_item": tilapia, "qty": 1}],
		                            room=self.rooms[1], order_type="Room Service")["order"]
		pos.deliver_order(ord_food)
		food_rows = frappe.get_all(
			"Folio Charge",
			filters={"reservation": stay.name, "description": ("like", f"%({ord_food})")},
			fields=["parent", "transaction_code", "is_alcohol", "amount"])
		self.assertEqual(len(food_rows), 1)
		self.assertEqual(
			(frappe.db.get_value("Folio", food_rows[0].parent, "folio_type"),
			 food_rows[0].transaction_code, food_rows[0].is_alcohol),
			("Company", "2000", 0))
		# Mixed order (food + beer on one check) splits by transaction code
		ord_mix = pos.create_order(
			outlet, [{"menu_item": tilapia, "qty": 1}, {"menu_item": beer, "qty": 1}],
			room=self.rooms[1], order_type="Room Service")["order"]
		pos.deliver_order(ord_mix)
		mix_rows = frappe.get_all(
			"Folio Charge",
			filters={"reservation": stay.name, "description": ("like", f"%({ord_mix})")},
			fields=["parent", "transaction_code", "is_alcohol", "amount"])
		by_code = {r.transaction_code: (frappe.db.get_value("Folio", r.parent, "folio_type"),
		                                float(r.amount), r.is_alcohol) for r in mix_rows}
		self.assertEqual(by_code, {
			"2000": ("Company", 60000.0, 0),
			"2010": ("Guest", 15000.0, 1),
		})
		# Explicit item transaction_code and outlet transaction_code override defaults
		bar = bar_code()
		frappe.db.set_value("Menu Item", beer, "transaction_code", bar)
		ord_custom = pos.create_order(outlet, [{"menu_item": beer, "qty": 1}],
		                              room=self.rooms[1], order_type="Room Service")["order"]
		pos.deliver_order(ord_custom)
		custom_row = frappe.get_all(
			"Folio Charge",
			filters={"reservation": stay.name, "description": ("like", f"%({ord_custom})")},
			fields=["transaction_code"])[0]
		self.assertEqual(custom_row.transaction_code, bar)
		# Outlet-level transaction_code applies to non-alcohol items with no code of their own
		frappe.db.set_value("POS Outlet", outlet, "transaction_code", bar)
		ord_out = pos.create_order(outlet, [{"menu_item": tilapia, "qty": 1}],
		                           room=self.rooms[1], order_type="Room Service")["order"]
		pos.deliver_order(ord_out)
		out_row = frappe.get_all(
			"Folio Charge",
			filters={"reservation": stay.name, "description": ("like", f"%({ord_out})")},
			fields=["transaction_code"])[0]
		self.assertEqual(out_row.transaction_code, bar)

	def test_add_folio_charge_and_post_stay_charge_keep_explicit_transaction_code(self):
		guest = guest_folio(self.res.name)
		# Hydrogen R2 live check: explicit transaction_code="2010" must not revert to "2000"
		api.add_folio_charge(guest, "Food & Beverage", "Bar Check Gin & Tonic", 40000,
		                     gst_rate=5, is_alcohol=1, transaction_code="2010")
		row = frappe.get_doc("Folio", guest).charges[-1]
		self.assertEqual(row.transaction_code, "2010")
		bar = bar_code()
		api.add_folio_charge(guest, "Food & Beverage", "Custom Bar Shot", 10000,
		                     gst_rate=0, transaction_code=bar)
		self.assertEqual(frappe.get_doc("Folio", guest).charges[-1].transaction_code, bar)
		# post_stay_charge with is_alcohol=1 defaults to 2010 and respects FoodOnly2000 rules
		food_only = corp([("#2000", "Company")])
		stay = self.stay(1, company=food_only, booking_type="Corporate")
		beer = api.post_stay_charge(stay.name, "Food & Beverage", "UT Bar Beer", 15000,
		                            is_alcohol=1)
		self.assertEqual(beer["folio"], guest_folio(stay.name))
		self.assertEqual(frappe.get_doc("Folio", beer["folio"]).charges[-1].transaction_code,
		                 "2010")
		# post_stay_charge with explicit transaction_code keeps it
		custom = api.post_stay_charge(stay.name, "Food & Beverage", "UT Bar Custom", 12000,
		                              transaction_code=bar)
		self.assertEqual(custom["folio"], guest_folio(stay.name))
		self.assertEqual(frappe.get_doc("Folio", custom["folio"]).charges[-1].transaction_code,
		                 bar)

	def test_banquet_alcohol_and_service_item_transaction_code(self):
		from kamra import banquet
		bar = bar_code()
		venue = frappe.get_doc({"doctype": "Venue", "property": PROP_A,
		                        "venue_name": "UT TC Hall"}).insert(ignore_permissions=True)
		svc_alc = frappe.get_doc({
			"doctype": "Banquet Service Item", "property": PROP_A,
			"item_name": "UT Open Bar", "category": "Alcohol",
			"rate": 50000, "gst_rate": 18, "chargeable": 1, "is_alcohol": 1,
		}).insert(ignore_permissions=True)
		svc_custom = frappe.get_doc({
			"doctype": "Banquet Service Item", "property": PROP_A,
			"item_name": "UT Craft Bar", "category": "Beverage",
			"rate": 30000, "gst_rate": 18, "chargeable": 1, "is_alcohol": 1,
			"transaction_code": bar,
		}).insert(ignore_permissions=True)
		fn = frappe.get_doc({
			"doctype": "Venue Booking", "property": PROP_A, "venue": venue.name,
			"event_date": d(1), "customer_name": "UT Banquet TC", "status": "Confirmed",
		}).insert(ignore_permissions=True)
		banquet.add_service(fn.name, svc_alc.name, qty=1)
		banquet.add_service(fn.name, svc_custom.name, qty=1)
		guest = guest_folio(self.res.name)
		banquet.post_to_folio(fn.name, folio=guest)
		posted = {c.description.split(" · ", 1)[-1].split(" (", 1)[0]: c.transaction_code
		          for c in frappe.get_doc("Folio", guest).charges if fn.name in (c.description or "")}
		self.assertEqual(posted, {"UT Open Bar": "2010", "UT Craft Bar": bar})

	def test_external_pos_alcohol_posts_under_beverage_code_and_respects_code_rules(self):
		from kamra import pos_interface as pi
		food_only = corp([("#2000", "Company")])
		stay = self.stay(1, company=food_only, booking_type="Corporate")
		with patch("kamra.webhooks._enqueue"):
			beer = pi.pos_post_room_charge(PROP_A, "UT-EPOS-BEER-1", 15000, "Bar Beer",
			                               reservation=stay.name, is_alcohol=1)
			food = pi.pos_post_room_charge(PROP_A, "UT-EPOS-FOOD-1", 25000, "Club Lunch",
			                               reservation=stay.name, is_alcohol=0)
			bar = bar_code()
			custom = pi.pos_post_room_charge(PROP_A, "UT-EPOS-BAR-1", 18000, "Bar Cocktail",
			                                 reservation=stay.name, transaction_code=bar)
		self.assertEqual(beer["folio"], guest_folio(stay.name))
		self.assertEqual(food["folio_type"], "Company")
		self.assertEqual(custom["folio"], guest_folio(stay.name))
		codes = {c.description: c.transaction_code
		         for f in (guest_folio(stay.name), food["folio"])
		         for c in frappe.get_doc("Folio", f).charges}
		self.assertEqual(codes["Bar Beer (POS UT-EPOS-BEER-1)"], "2010")
		self.assertEqual(codes["Club Lunch (POS UT-EPOS-FOOD-1)"], "2000")
		self.assertEqual(codes["Bar Cocktail (POS UT-EPOS-BAR-1)"], bar)

	# Hydrogen R2 P0-1(ii): pre-K1 liquor exclusion migration --------------

	def test_pre_k1_liquor_exclusion_migrates_to_non_alcohol_code_rules_before_rename(self):
		import importlib
		import os
		patches = [ln.strip() for ln in open(
			os.path.join(frappe.get_app_path("kamra"), "patches.txt"),
			encoding="utf-8") if ln.strip() and not ln.strip().startswith("#")]
		mig = "kamra.patches.v48.migrate_liquor_exclusion_to_code_rules"
		ren = "kamra.patches.v48.rename_company_billing_presets"
		self.assertIn(mig, patches)
		self.assertIn(ren, patches)
		self.assertLess(patches.index(mig), patches.index(ren))

		# Pre-K1 fixture: property on "Room and F&B (liquor to guest)"
		frappe.db.set_value("Property", PROP_A, "default_company_billing",
		                    "Room and F&B (liquor to guest)")
		frappe.clear_document_cache("Property", PROP_A)
		settings.clear_cache(PROP_A)

		# 1. Rule-less credit company relying on the pre-K1 property default
		ruleless = frappe.get_doc({
			"doctype": "Corporate Account", "company_name": "UT PreK1 Ruleless Corp",
			"credit_allowed": 1,
		}).insert(ignore_permissions=True).name
		# 2. Company with explicit pre-K1 charge-type rules (Room + Food & Beverage)
		explicit = frappe.get_doc({
			"doctype": "Corporate Account", "company_name": "UT PreK1 Explicit F&B Corp",
			"credit_allowed": 1,
			"billing_rules": [
				{"charge_type": "Room", "pay_by": "Company"},
				{"charge_type": "Food & Beverage", "pay_by": "Company"},
			],
		}).insert(ignore_permissions=True).name
		# 3. Non-credit company with no rules: must NOT gain billing rules
		cash_corp = frappe.get_doc({
			"doctype": "Corporate Account", "company_name": "UT PreK1 Cash Corp",
			"credit_allowed": 0,
		}).insert(ignore_permissions=True).name

		# 4. Non-company group with master_reservation and "Everything except liquor"
		gb = frappe.get_doc({
			"doctype": "Group Booking", "property": PROP_A,
			"group_name": "UT PreK1 Master Group", "check_in_date": d(-2),
			"check_out_date": d(2), "status": "Confirmed",
		}).insert(ignore_permissions=True)
		lead = self.stay(1, group_booking=gb.name)
		member = self.stay(2, group_booking=gb.name)
		frappe.db.set_value("Group Booking", gb.name, {
			"master_reservation": lead.name,
			"master_pays": "Everything except liquor",
		})

		# Run migration BEFORE preset rename, then run both a second time (idempotency)
		mig_mod = importlib.import_module(mig)
		ren_mod = importlib.import_module(ren)
		mig_mod.execute()
		ren_mod.execute()
		rules_after_1 = {
			c: sorted((r.charge_type or "", r.transaction_code or "", r.pay_by)
			          for r in frappe.get_doc("Corporate Account", c).billing_rules)
			for c in (ruleless, explicit, cash_corp)
		}
		ri_after_1 = frappe.get_all(
			"Routing Instruction", filters={"group_booking": gb.name}, pluck="name")
		mig_mod.execute()
		ren_mod.execute()
		self.assertEqual(
			{c: sorted((r.charge_type or "", r.transaction_code or "", r.pay_by)
			           for r in frappe.get_doc("Corporate Account", c).billing_rules)
			 for c in (ruleless, explicit, cash_corp)},
			rules_after_1)
		self.assertEqual(
			frappe.get_all("Routing Instruction", filters={"group_booking": gb.name}, pluck="name"),
			ri_after_1)

		# Verify explicit non-alcohol transaction_code rules (no 2010, no charge_type wildcard)
		for cname in (ruleless, explicit):
			rows = frappe.get_doc("Corporate Account", cname).billing_rules
			self.assertTrue(rows)
			codes = {r.transaction_code for r in rows}
			self.assertTrue(all(r.transaction_code and not r.charge_type for r in rows))
			self.assertIn("1000", codes)
			self.assertIn("2000", codes)
			self.assertNotIn("2010", codes)
		self.assertEqual(frappe.get_doc("Corporate Account", cash_corp).billing_rules, [])

		# Verify post-upgrade billing matches pre-K1 behavior exactly:
		# Ruleless corp: Room & Food -> Company; Beer (2010) & Laundry -> Guest
		r_ruleless = make_reservation(PROP_A, self.rt, d(-1), d(2), status="Checked In",
		                              room=make_room(PROP_A, self.rt, "315"),
		                              company=ruleless, booking_type="Corporate")
		self.assertEqual(api.post_stay_charge(r_ruleless.name, "Room", "UT night", 1000)["folio_type"],
		                 "Company")
		self.assertEqual(api.post_stay_charge(r_ruleless.name, "Food & Beverage", "UT lunch", 500)["folio_type"],
		                 "Company")
		self.assertEqual(api.post_stay_charge(r_ruleless.name, "Food & Beverage", "UT beer", 400, is_alcohol=1)["folio_type"],
		                 "Guest")
		self.assertEqual(api.post_stay_charge(r_ruleless.name, "Laundry", "UT shirt", 200)["folio_type"],
		                 "Guest")

		# Explicit F&B corp: Room & Food -> Company; Beer (2010) -> Guest
		r_explicit = make_reservation(PROP_A, self.rt, d(-1), d(2), status="Checked In",
		                              room=make_room(PROP_A, self.rt, "316"),
		                              company=explicit, booking_type="Corporate")
		self.assertEqual(api.post_stay_charge(r_explicit.name, "Food & Beverage", "UT dinner", 500)["folio_type"],
		                 "Company")
		self.assertEqual(api.post_stay_charge(r_explicit.name, "Food & Beverage", "UT gin", 400, is_alcohol=1)["folio_type"],
		                 "Guest")

		# Master group ("Everything except liquor"): Room, Food, Laundry -> Group; Beer (2010) -> Guest
		self.assertEqual(api.post_stay_charge(member.name, "Room", "UT grp night", 1000)["folio_type"],
		                 "Group")
		self.assertEqual(api.post_stay_charge(member.name, "Food & Beverage", "UT grp lunch", 500)["folio_type"],
		                 "Group")
		self.assertEqual(api.post_stay_charge(member.name, "Laundry", "UT grp laundry", 300)["folio_type"],
		                 "Group")
		self.assertEqual(api.post_stay_charge(member.name, "Food & Beverage", "UT grp beer", 400, is_alcohol=1)["folio_type"],
		                 "Guest")
		self.assertEqual(frappe.db.get_value("Property", PROP_A, "default_company_billing"),
		                 "Room and F&B")

	# Hydrogen R2 P0-2: upward correction re-applies routing limit ---------

	def test_upward_correction_reapplies_routing_limit_and_overflows_to_window_1(self):
		from kamra import ledger
		ri = route(["Laundry"], reservation=self.res.name, target_type="Window",
		           target_window=2, limit_type="Amount", limit_value=100000, per_day=1)
		self.charge("Laundry", 80000, "Upward orig")
		row0 = lines(self.w2, "Laundry")[-1].name
		api.correct_folio_charge(self.w2, row0, reason="upward past limit",
		                         amount=250000, gst_rate=0, charge_type="Laundry",
		                         description="Upward corr")
		guest = guest_folio(self.res.name)
		self.assertEqual(float(frappe.db.get_value("Folio", self.w2, "grand_total")), 100000.0)
		self.assertEqual(float(frappe.db.get_value("Folio", guest, "grand_total")), 150000.0)
		self.assertEqual(routing.used(ri.name, TODAY), Decimal("100000"))
		self.assertTrue(ledger.folio_reconciliation(PROP_A)["in_balance"])
		# Cross-day upward correction counts against the original line's date
		other = self.stay(1)
		w2_b = folio_mod.open_window(reservation=other.name, payee_type="Guest")
		ri_b = route(["Laundry"], reservation=other.name, target_type="Window",
		             target_window=2, limit_type="Amount", limit_value=100000, per_day=1)
		self.charge("Laundry", 50000, "Day0 A", res=other)
		self.charge("Laundry", 30000, "Day0 B", res=other)
		row_a = next(r.name for r in lines(w2_b, "Laundry") if r.amount == 50000)
		frappe.db.set_value("Property", PROP_A, "business_date", d(1))
		frappe.clear_document_cache("Property", PROP_A)
		api.correct_folio_charge(w2_b, row_a, reason="Day+1 upward",
		                         amount=120000, gst_rate=0, charge_type="Laundry",
		                         description="Day0 A corr")
		self.assertEqual((routing.used(ri_b.name, TODAY), routing.used(ri_b.name, d(1))),
		                 (Decimal("100000"), Decimal("0")))
		self.assertEqual(float(frappe.db.get_value("Folio", w2_b, "grand_total")), 100000.0)
		self.assertEqual(float(frappe.db.get_value("Folio", guest_folio(other.name), "grand_total")),
		                 50000.0)

	# Hydrogen R2 P0-3: UGX remainders in whole shillings ------------------

	def test_ugx_routing_splits_leave_no_fractional_shillings_on_remainders(self):
		# 1. Tax-exclusive 100,003 @ 15% at 33.333% (was W1 tax=10000.45)
		make_property(PROP_A, business_date=TODAY, country="Uganda", currency="UGX",
		              rates_include_tax=0, gst_rate_low=15, gst_rate_high=15)
		settings.clear_cache(PROP_A)
		stay_ex = self.stay(1)
		w1_ex = guest_folio(stay_ex.name)
		w2_ex = folio_mod.open_window(reservation=stay_ex.name, payee_type="Guest")
		route(["Misc"], reservation=stay_ex.name, target_type="Window", target_window=2,
		      limit_type="Percent", limit_value=33.333)
		api.post_stay_charge(stay_ex.name, "Misc", "UGX Excl Misc", 100003, gst_rate=15)
		c1_ex = frappe.get_doc("Folio", w1_ex).charges[-1]
		c2_ex = frappe.get_doc("Folio", w2_ex).charges[-1]
		for row in (c1_ex, c2_ex):
			for val in (row.amount, row.gst_amount, row.total):
				self.assertEqual(Decimal(str(val)) % 1, 0, f"fractional UGX on {row.description}: {val}")
			self.assertEqual(Decimal(str(row.amount)) + Decimal(str(row.gst_amount)),
			                 Decimal(str(row.total)))
		self.assertEqual(Decimal(str(c1_ex.amount)) + Decimal(str(c2_ex.amount)), Decimal("100003"))
		self.assertEqual(Decimal(str(c1_ex.gst_amount)) + Decimal(str(c2_ex.gst_amount)), Decimal("15000"))
		self.assertEqual(Decimal(str(c1_ex.total)) + Decimal(str(c2_ex.total)), Decimal("115003"))

		# 2. Tax-inclusive post_room_night 500,000 @ 15% capped at 200,000 (was 500000.0015)
		make_property(PROP_A, business_date=TODAY, country="Uganda", currency="UGX",
		              rates_include_tax=1, gst_rate_low=15, gst_rate_high=15)
		settings.clear_cache(PROP_A)
		rt15 = make_room_type(PROP_A, "UGX15A", base_price=500000, tax_percent=15)
		s_cap = make_reservation(PROP_A, rt15, TODAY, d(2), status="Checked In",
		                         room=make_room(PROP_A, rt15, "311"), agreed_rate=500000)
		w1_cap = folio_mod.open_folio(s_cap)
		w2_cap = folio_mod.open_window(reservation=s_cap.name, payee_type="Guest")
		route(["Room"], reservation=s_cap.name, target_type="Window", target_window=2,
		      limit_type="Amount", limit_value=200000, per_day=1)
		folio_mod.post_room_night(s_cap, TODAY)
		c1_cap = frappe.get_doc("Folio", w1_cap).charges[-1]
		c2_cap = frappe.get_doc("Folio", w2_cap).charges[-1]
		for row in (c1_cap, c2_cap):
			for val in (row.amount, row.gst_amount, row.total):
				self.assertEqual(Decimal(str(val)) % 1, 0, f"fractional UGX on cap split: {val}")
			self.assertEqual(Decimal(str(row.amount)) + Decimal(str(row.gst_amount)),
			                 Decimal(str(row.total)))
		self.assertEqual(Decimal(str(c2_cap.total)), Decimal("200000"))
		self.assertEqual(Decimal(str(c1_cap.total)), Decimal("300000"))
		self.assertEqual(Decimal(str(c1_cap.total)) + Decimal(str(c2_cap.total)), Decimal("500000"))

		# 3. Tax-inclusive post_room_night 500,003 @ 15% @ 33.333% (was 500003.003)
		rt15b = make_room_type(PROP_A, "UGX15B", base_price=500003, tax_percent=15)
		s_pct = make_reservation(PROP_A, rt15b, TODAY, d(2), status="Checked In",
		                         room=make_room(PROP_A, rt15b, "312"), agreed_rate=500003)
		w1_pct = folio_mod.open_folio(s_pct)
		w2_pct = folio_mod.open_window(reservation=s_pct.name, payee_type="Guest")
		route(["Room"], reservation=s_pct.name, target_type="Window", target_window=2,
		      limit_type="Percent", limit_value=33.333)
		folio_mod.post_room_night(s_pct, TODAY)
		c1_pct = frappe.get_doc("Folio", w1_pct).charges[-1]
		c2_pct = frappe.get_doc("Folio", w2_pct).charges[-1]
		for row in (c1_pct, c2_pct):
			for val in (row.amount, row.gst_amount, row.total):
				self.assertEqual(Decimal(str(val)) % 1, 0, f"fractional UGX on pct split: {val}")
			self.assertEqual(Decimal(str(row.amount)) + Decimal(str(row.gst_amount)),
			                 Decimal(str(row.total)))
		self.assertEqual(Decimal(str(c1_pct.total)) + Decimal(str(c2_pct.total)), Decimal("500003"))

	# Hydrogen R2 P1: unknown kwarg over frappe.call, invalid targets ------

	def test_route_outlet_charge_refuses_unknown_kwargs_over_frappe_call(self):
		with self.assertRaises(frappe.ValidationError):
			frappe.call("kamra.api.route_outlet_charge", reservation=self.res.name,
			            transaction_code="2000", amount=500, is_alcohol=1)
		# Frappe's HTTP dispatcher passes cmd in form_dict: still allowed
		out = frappe.call("kamra.api.route_outlet_charge", reservation=self.res.name,
		                  transaction_code="2000", amount=500,
		                  cmd="kamra.api.route_outlet_charge")
		self.assertEqual(set(out), {"folio", "window_no", "routing_instruction"})

	def test_routing_instruction_refuses_checked_out_room_and_cancelled_group_targets(self):
		res_out = make_reservation(PROP_A, self.rt, d(-3), d(-1), status="Checked Out",
		                           room=self.rooms[1])
		with self.assertRaises(frappe.ValidationError):
			route([], reservation=self.res.name, target_type="Room",
			      target_reservation=res_out.name, target_window=1, all_codes=1)
		gb_can = frappe.get_doc({
			"doctype": "Group Booking", "property": PROP_A,
			"group_name": "UT Cancelled RI Group", "check_in_date": TODAY,
			"check_out_date": d(2), "status": "Cancelled",
		}).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			route([], group_booking=gb_can.name, target_type="Group Master",
			      target_window=1, all_codes=1)
		member = self.stay(2, group_booking=gb_can.name)
		with self.assertRaises(frappe.ValidationError):
			route(["Laundry"], reservation=member.name, target_type="Group Master")

	# Hydrogen R2 P2: SOP code-only rule formatting & Corporate Account text

	def test_sop_confirm_bill_to_formats_code_only_rules_without_none(self):
		from kamra import sop
		company = corp([("#2000", "Company")])
		step = sop.evaluate_checkout_step(
			{"step_key": "confirm_bill_to", "label": "Confirm bill-to"},
			{"billing": {"company": company, "company_name": company,
			             "rules": folio_mod.billing_rule_rows(company)}},
		)
		self.assertNotIn("None:", step["tip"])
		self.assertIn("2000 Food & Beverage: Company", step["tip"])

	def test_corporate_account_billing_rules_description_has_no_liquor_promise(self):
		desc = frappe.get_meta("Corporate Account").get_field("billing_rules").description or ""
		self.assertNotRegex(desc, r"\b(alcohol|liquor)\b",
		                    f"Corporate Account.billing_rules still mentions alcohol: {desc!r}")
