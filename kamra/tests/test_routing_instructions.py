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

	# Hydrogen R3 P1-A: UGX share_charges & split_charge exact minor units -

	def test_ugx_share_charges_and_split_charge_leave_no_fractional_shillings(self):
		make_property(PROP_A, business_date=TODAY, country="Uganda", currency="UGX",
		              rates_include_tax=0, gst_rate_low=15, gst_rate_high=15)
		settings.clear_cache(PROP_A)
		stay = self.stay(1)
		f1 = guest_folio(stay.name)
		f2, f3 = [b["folio"] for b in folio_mod.open_guest_bills(
			stay.name, ["UT Share Guest 2", "UT Share Guest 3"])]
		api.add_folio_charge(f1, "Misc", "Odd 0pct", 100001, gst_rate=0)
		api.add_folio_charge(f1, "Misc", "Odd 15pct", 100001, gst_rate=15)
		f1_doc = frappe.get_doc("Folio", f1)
		row_0 = next(c.name for c in f1_doc.charges if c.description == "Odd 0pct")
		row_15 = next(c.name for c in f1_doc.charges if c.description == "Odd 15pct")

		folio_mod.share_charges(f1, [row_0, row_15], [f2, f3])

		for row_id, exp_amt, exp_tax, exp_tot in (
			(row_0, Decimal("100001"), Decimal("0"), Decimal("100001")),
			(row_15, Decimal("100001"), Decimal("15000"), Decimal("115001")),
		):
			parts = [
				c for f in (f1, f2, f3)
				for c in frappe.get_doc("Folio", f).charges
				if c.get("split_of") == row_id and not c.get("reversal_of")
			]
			self.assertEqual(len(parts), 3)
			for c in parts:
				for val in (c.amount, c.gst_amount, c.total):
					self.assertEqual(Decimal(str(val)) % 1, 0,
					                 f"fractional UGX on {c.description}: {val}")
				self.assertEqual(Decimal(str(c.amount)) + Decimal(str(c.gst_amount)),
				                 Decimal(str(c.total)))
			self.assertEqual(sum(Decimal(str(c.amount)) for c in parts), exp_amt)
			self.assertEqual(sum(Decimal(str(c.gst_amount)) for c in parts), exp_tax)
			self.assertEqual(sum(Decimal(str(c.total)) for c in parts), exp_tot)

		gts = [Decimal(str(frappe.db.get_value("Folio", f, "grand_total"))) for f in (f1, f2, f3)]
		for gt in gts:
			self.assertEqual(gt % 1, 0, f"fractional UGX grand_total: {gt}")
		self.assertEqual(gts, [Decimal("71666"), Decimal("71668"), Decimal("71668")])
		self.assertEqual(sum(gts), Decimal("215002"))

		# split_charge(..., percent=33.333) on 100,001 @ 15% UGX also lands in whole shillings
		api.add_folio_charge(f1, "Misc", "SplitCharge 15pct", 100001, gst_rate=15)
		row_sc = next(c.name for c in frappe.get_doc("Folio", f1).charges
		              if c.description == "SplitCharge 15pct")
		out = folio_mod.split_charge(f1, row_sc, f2, percent=33.333)
		kept = next(c for c in frappe.get_doc("Folio", f1).charges if c.name == out["kept_row"])
		moved = next(c for c in frappe.get_doc("Folio", f2).charges if c.name == out["moved_row"])
		for c in (kept, moved):
			for val in (c.amount, c.gst_amount, c.total):
				self.assertEqual(Decimal(str(val)) % 1, 0,
				                 f"fractional UGX on split_charge: {val}")
			self.assertEqual(Decimal(str(c.amount)) + Decimal(str(c.gst_amount)),
			                 Decimal(str(c.total)))
		self.assertEqual(Decimal(str(kept.amount)) + Decimal(str(moved.amount)), Decimal("100001"))
		self.assertEqual(Decimal(str(kept.gst_amount)) + Decimal(str(moved.gst_amount)), Decimal("15000"))
		self.assertEqual(Decimal(str(kept.total)) + Decimal(str(moved.total)), Decimal("115001"))

	# Hydrogen R3 P1-B: mixed POS room checks, move_room_bill, per-code tax, 140-char

	def test_mixed_code_pos_order_moves_taxes_per_code_rounds_ugx_and_fits_140_chars(self):
		from kamra import pos
		# (a) UGX mixed check with discount -> whole UGX tax & move_room_bill works
		make_property(PROP_A, business_date=TODAY, country="Uganda", currency="UGX",
		              rates_include_tax=0, gst_rate_low=15, gst_rate_high=15)
		settings.clear_cache(PROP_A)
		food_only = corp([("#1000", "Company"), ("#2000", "Company")])
		stay1 = self.stay(1, company=food_only, booking_type="Corporate")
		stay2 = self.stay(2)
		outlet_ug = frappe.get_doc({
			"doctype": "POS Outlet", "property": PROP_A, "outlet_name": "UT R4 Main Grill",
			"outlet_type": "Restaurant", "gst_rate": 15, "transaction_code": "2000",
		}).insert(ignore_permissions=True).name
		mi_steak = frappe.get_doc({
			"doctype": "Menu Item", "property": PROP_A, "outlet": outlet_ug,
			"item_name": "Steak", "category": "Mains", "price": 50000,
			"is_alcohol": 0, "transaction_code": "2000",
		}).insert(ignore_permissions=True).name
		mi_beer = frappe.get_doc({
			"doctype": "Menu Item", "property": PROP_A, "outlet": outlet_ug,
			"item_name": "Nile Special", "category": "Beverages", "price": 15000,
			"is_alcohol": 1, "transaction_code": "2010",
		}).insert(ignore_permissions=True).name
		order = frappe.get_doc({
			"doctype": "POS Order", "property": PROP_A, "outlet": outlet_ug,
			"order_type": "Room Service", "reservation": stay1.name, "room": stay1.room,
			"payment_status": "Room Charge", "discount_amount": 5000,
			"items": [
				{"menu_item": mi_steak, "item_name": "Steak", "qty": 1, "rate": 50000, "amount": 50000},
				{"menu_item": mi_beer, "item_name": "Nile Special", "qty": 1, "rate": 15000, "amount": 15000},
			],
		}).insert(ignore_permissions=True)
		order.status = "Delivered"
		order.save(ignore_permissions=True)

		rows = frappe.get_all(
			"Folio Charge",
			filters={"parenttype": "Folio", "description": ("like", f"%({order.name})%")},
			fields=["name", "parent", "transaction_code", "is_alcohol",
			        "amount", "gst_rate", "gst_amount", "total"],
			order_by="transaction_code asc",
		)
		self.assertEqual(len(rows), 2)
		for r in rows:
			for val in (r.amount, r.gst_amount, r.total):
				self.assertEqual(Decimal(str(val)) % 1, 0, f"fractional UGX on POS row: {r}")
			self.assertEqual(Decimal(str(r.amount)) + Decimal(str(r.gst_amount)),
			                 Decimal(str(r.total)))
		self.assertEqual(sum(Decimal(str(r.amount)) for r in rows), Decimal("60000"))
		self.assertEqual(sum(Decimal(str(r.gst_amount)) for r in rows), Decimal("9000"))
		self.assertEqual(sum(Decimal(str(r.total)) for r in rows), Decimal("69000"))
		by_code = {r.transaction_code: frappe.db.get_value("Folio", r.parent, "folio_type") for r in rows}
		self.assertEqual(by_code, {"2000": "Company", "2010": "Guest"})

		# Moving the mixed-code room bill to stay2 succeeds and moves all its code lines
		with patch("kamra.webhooks.emit_folio_posted"):
			moved = pos.move_room_bill(order.name, stay2.room, "wrong room test")
		self.assertEqual(moved["reservation"], stay2.name)
		self.assertEqual(
			frappe.db.get_value("POS Order", order.name, ["room", "reservation"]),
			(stay2.room, stay2.name),
		)
		after_rows = frappe.get_all(
			"Folio Charge",
			filters={"parenttype": "Folio", "description": ("like", f"%({order.name})%")},
			fields=["parent", "reservation", "transaction_code", "amount"],
			order_by="transaction_code asc",
		)
		self.assertEqual(len(after_rows), 2)
		stay2_guest = guest_folio(stay2.name)
		self.assertTrue(all(r.parent == stay2_guest and r.reservation == stay2.name for r in after_rows))
		self.assertEqual(sum(Decimal(str(r.amount)) for r in after_rows), Decimal("60000"))

		# (b) Per-code taxation on India property (food 5% vs alcohol 18%, plus TC.default_tax_rate)
		make_property(PROP_A, business_date=TODAY, country="India", currency="INR",
		              rates_include_tax=0)
		settings.clear_cache(PROP_A)
		outlet_in = frappe.get_doc({
			"doctype": "POS Outlet", "property": PROP_A, "outlet_name": "UT India Bistro",
			"outlet_type": "Restaurant", "gst_rate": 5, "transaction_code": "2000",
		}).insert(ignore_permissions=True).name
		mi_food_in = frappe.get_doc({
			"doctype": "Menu Item", "property": PROP_A, "outlet": outlet_in,
			"item_name": "Paneer Tikka", "category": "Food", "price": 1000,
			"is_alcohol": 0, "transaction_code": "2000",
		}).insert(ignore_permissions=True).name
		mi_beer_in = frappe.get_doc({
			"doctype": "Menu Item", "property": PROP_A, "outlet": outlet_in,
			"item_name": "Craft Beer", "category": "Beverages", "price": 1000,
			"is_alcohol": 1, "transaction_code": "2010",
		}).insert(ignore_permissions=True).name
		in_ord = pos.create_order(
			outlet_in,
			[{"menu_item": mi_food_in, "qty": 1}, {"menu_item": mi_beer_in, "qty": 1}],
			room=stay2.room, order_type="Room Service",
		)["order"]
		pos.deliver_order(in_ord)
		in_rows = {
			r.transaction_code: (float(r.gst_rate), float(r.gst_amount), float(r.total))
			for r in frappe.get_all(
				"Folio Charge",
				filters={"parenttype": "Folio", "description": ("like", f"%({in_ord})%")},
				fields=["transaction_code", "gst_rate", "gst_amount", "total"],
			)
		}
		self.assertEqual(in_rows["2000"], (5.0, 50.0, 1050.0))
		self.assertEqual(in_rows["2010"], (18.0, 180.0, 1180.0))

		# Configured Transaction Code.default_tax_rate wins (configuration, not hardcoding)
		bar = bar_code()
		frappe.db.set_value("Transaction Code", bar, "default_tax_rate", 12)
		mi_custom = frappe.get_doc({
			"doctype": "Menu Item", "property": PROP_A, "outlet": outlet_in,
			"item_name": "Custom Cocktail", "category": "Beverages", "price": 1000,
			"is_alcohol": 1, "transaction_code": bar,
		}).insert(ignore_permissions=True).name
		cust_ord = pos.create_order(
			outlet_in, [{"menu_item": mi_custom, "qty": 1}],
			room=stay2.room, order_type="Room Service",
		)["order"]
		pos.deliver_order(cust_ord)
		cust_row = frappe.get_all(
			"Folio Charge",
			filters={"parenttype": "Folio", "description": ("like", f"%({cust_ord})%")},
			fields=["transaction_code", "gst_rate", "gst_amount"],
		)[0]
		self.assertEqual((cust_row.transaction_code, float(cust_row.gst_rate), float(cust_row.gst_amount)),
		                 (bar, 12.0, 120.0))

		# (c) 6-item order whose raw description exceeds 140 chars delivers cleanly and keeps (ORD-...)
		long_outlet = frappe.get_doc({
			"doctype": "POS Outlet", "property": PROP_A,
			"outlet_name": "Grand Dining Hall & Bistro Lounge",
			"outlet_type": "Restaurant", "gst_rate": 5, "transaction_code": "2000",
		}).insert(ignore_permissions=True).name
		long_items = []
		for i in range(1, 7):
			mi = frappe.get_doc({
				"doctype": "Menu Item", "property": PROP_A, "outlet": long_outlet,
				"item_name": f"Signature Tasting Course Platter Number {i}",
				"category": "Food", "price": 100, "is_alcohol": 0, "transaction_code": "2000",
			}).insert(ignore_permissions=True).name
			long_items.append({"menu_item": mi, "qty": 1})
		long_ord = pos.create_order(
			long_outlet, long_items, room=stay2.room, order_type="Room Service",
		)["order"]
		pos.deliver_order(long_ord)
		long_rows = frappe.get_all(
			"Folio Charge",
			filters={"parenttype": "Folio", "description": ("like", f"%({long_ord})%")},
			fields=["description", "amount"],
		)
		self.assertEqual(len(long_rows), 1)
		self.assertLessEqual(len(long_rows[0].description), 140)
		self.assertTrue(long_rows[0].description.endswith(f"({long_ord})"))
		self.assertEqual(float(long_rows[0].amount), 600.0)

	# Hydrogen R3 P1-C: explicit transaction_code drives is_alcohol/tax/audit & banquet SC

	def test_explicit_alcohol_transaction_code_drives_flag_tax_and_audit_group(self):
		from kamra import audit_report
		from kamra import pos_interface as pi
		make_property(PROP_A, business_date=TODAY, country="India", currency="INR",
		              rates_include_tax=0)
		settings.clear_cache(PROP_A)
		stay = self.stay(1)
		guest = guest_folio(stay.name)

		api.post_stay_charge(stay.name, "Food & Beverage", "Whiskey 2010 stay",
		                     1000, is_alcohol=0, transaction_code="2010")
		row1 = frappe.get_doc("Folio", guest).charges[-1]
		self.assertEqual((row1.transaction_code, int(row1.is_alcohol), float(row1.gst_rate),
		                  float(row1.gst_amount), float(row1.total)),
		                 ("2010", 1, 18.0, 180.0, 1180.0))

		api.add_folio_charge(guest, "Food & Beverage", "Whiskey 2010 direct",
		                     1000, gst_rate=0, is_alcohol=0, transaction_code="2010")
		row2 = frappe.get_doc("Folio", guest).charges[-1]
		self.assertEqual((row2.transaction_code, int(row2.is_alcohol), float(row2.gst_rate),
		                  float(row2.gst_amount), float(row2.total)),
		                 ("2010", 1, 18.0, 180.0, 1180.0))

		with patch("kamra.webhooks._enqueue"):
			pi.pos_post_room_charge(PROP_A, "UT-TKT-IMP-2010", 1000, "Ext Bar 2010",
			                        reservation=stay.name, tax_rate=18,
			                        is_alcohol=0, transaction_code="2010")
		epc = frappe.get_all(pi.CHARGE, filters={"ticket_id": "UT-TKT-IMP-2010"},
		                     fields=["transaction_code", "is_alcohol"])[0]
		self.assertEqual((epc.transaction_code, int(epc.is_alcohol)), ("2010", 1))
		row3 = frappe.get_doc("Folio", guest).charges[-1]
		self.assertEqual((row3.transaction_code, int(row3.is_alcohol), float(row3.gst_rate),
		                  float(row3.gst_amount), float(row3.total)),
		                 ("2010", 1, 18.0, 180.0, 1180.0))

		rep = audit_report._folio(PROP_A, TODAY, TODAY)
		self.assertEqual((rep["food"], rep["beverage"]), (0.0, 3000.0))
		# Even a legacy DB row with transaction_code='2010' and is_alcohol=0 still counts as beverage
		frappe.db.set_value("Folio Charge", row1.name, "is_alcohol", 0, update_modified=False)
		rep2 = audit_report._folio(PROP_A, TODAY, TODAY)
		self.assertEqual((rep2["food"], rep2["beverage"]), (0.0, 3000.0))

	def test_banquet_post_to_folio_includes_service_charge_and_code_2010_alcohol_tax(self):
		from kamra import banquet
		make_property(PROP_A, business_date=TODAY, country="India", currency="INR",
		              rates_include_tax=0)
		settings.clear_cache(PROP_A)
		gb = frappe.get_doc({
			"doctype": "Group Booking", "property": PROP_A,
			"group_name": "UT Banquet SC Group", "check_in_date": TODAY,
			"check_out_date": d(2), "status": "Confirmed", "rooms_blocked": 1,
			"master_pays": "Nothing (guest pays unless rules say so)",
		}).insert(ignore_permissions=True)
		venue = frappe.get_doc({
			"doctype": "Venue", "property": PROP_A, "venue_name": "UT SC Probe Hall",
			"capacity_theatre": 100,
		}).insert(ignore_permissions=True).name
		vb = frappe.get_doc({
			"doctype": "Venue Booking", "property": PROP_A, "venue": venue,
			"group_booking": gb.name, "customer_name": "Banquet Probe Client",
			"customer_phone": "+256700000001", "event_name": "Probe Reception",
			"event_date": d(1), "session": "Evening", "attendees": 10,
			"status": "Confirmed", "service_charge_percent": 10,
			"items": [{
				"item_type": "Food & Beverage", "item_name": "Wine Service Coded 2010",
				"qty": 1, "uom": "Lot", "rate": 100000, "chargeable": 1,
				"is_alcohol": 0, "transaction_code": "2010", "gst_rate": 18,
			}],
		}).insert(ignore_permissions=True)
		self.assertEqual((float(vb.subtotal), float(vb.service_charge),
		                  float(vb.tax_amount), float(vb.grand_total)),
		                 (100000.0, 10000.0, 19800.0, 129800.0))

		out = banquet.post_to_folio(vb.name)
		gf = frappe.get_doc("Folio", out["folio"])
		self.assertEqual(float(gf.grand_total), 129800.0)
		wine = next(c for c in gf.charges if c.transaction_code == "2010")
		self.assertEqual((int(wine.is_alcohol), float(wine.gst_rate),
		                  float(wine.gst_amount), float(wine.total)),
		                 (1, 18.0, 18000.0, 118000.0))
		sc = next(c for c in gf.charges if "Service charge" in (c.description or ""))
		self.assertTrue(sc.description.startswith(vb.name))
		self.assertEqual((float(sc.amount), float(sc.gst_rate),
		                  float(sc.gst_amount), float(sc.total)),
		                 (10000.0, 18.0, 1800.0, 11800.0))

	# Hydrogen R3 P1-D: correct_folio_charge uncovered reclass, Percent, replay

	def test_correct_folio_charge_reroutes_uncovered_reclassification_percent_limit_and_replay(self):
		from kamra import ledger
		# (a1) Company Billing Rule (#2000 only): reclassifying F&B -> Spa moves off Company W2 to W1
		food_only = corp([("#2000", "Company")])
		stay_a = self.stay(1, company=food_only, booking_type="Corporate")
		api.post_stay_charge(stay_a.name, "Food & Beverage", "Dinner 100k", 100000,
		                     transaction_code="2000")
		w1_a = guest_folio(stay_a.name)
		w2_a = frappe.db.get_value("Folio", {"reservation": stay_a.name, "window_no": 2})
		row_a = frappe.db.get_value("Folio Charge", {"parent": w2_a, "description": "Dinner 100k"}, "name")
		corr_a = api.correct_folio_charge(
			w2_a, row_a, reason="Reclassify to uncovered Spa", amount=100000, gst_rate=0,
			charge_type="Spa", idempotency_key="ut-r4-idem-spa")
		self.assertEqual(float(frappe.db.get_value("Folio", w2_a, "charges_total")), 0.0)
		self.assertEqual(float(frappe.db.get_value("Folio", w1_a, "charges_total")), 100000.0)
		self.assertEqual(corr_a.get("overflow_folio"), w1_a)
		self.assertTrue(corr_a.get("overflow_row"))
		# Replay after 100% overflow returns overflow_folio, overflow_row and exact repost
		replay_a = api.correct_folio_charge(
			w2_a, row_a, reason="Reclassify to uncovered Spa", amount=100000, gst_rate=0,
			charge_type="Spa", idempotency_key="ut-r4-idem-spa")
		self.assertEqual(replay_a.get("idempotent_replay"), 1)
		self.assertEqual(replay_a.get("repost"), corr_a["repost"])
		self.assertEqual(replay_a.get("overflow_folio"), corr_a["overflow_folio"])
		self.assertEqual(replay_a.get("overflow_row"), corr_a["overflow_row"])

		# (a2) Routing Instruction covering #2000: reclassifying F&B -> Spa moves off W2 to W1
		stay_a2 = make_reservation(PROP_A, self.rt, d(-1), d(2), status="Checked In",
		                           room=make_room(PROP_A, self.rt, "341"))
		w1_a2 = guest_folio(stay_a2.name)
		w2_a2 = folio_mod.open_window(reservation=stay_a2.name, payee_type="Company",
		                              payee_company=corp([]), payment_method="Company Credit")
		ri_a2 = route(["#2000"], reservation=stay_a2.name, target_type="Window", target_window=2)
		api.post_stay_charge(stay_a2.name, "Food & Beverage", "RI Dinner", 100000,
		                     transaction_code="2000")
		row_a2 = frappe.db.get_value("Folio Charge", {"parent": w2_a2, "description": "RI Dinner"}, "name")
		corr_a2 = api.correct_folio_charge(w2_a2, row_a2, reason="RI reclass Spa",
		                                   amount=100000, gst_rate=0, charge_type="Spa")
		self.assertEqual(float(frappe.db.get_value("Folio", w2_a2, "charges_total")), 0.0)
		self.assertEqual(float(frappe.db.get_value("Folio", w1_a2, "charges_total")), 100000.0)
		self.assertEqual(corr_a2.get("overflow_folio"), w1_a2)
		self.assertEqual(routing.used(ri_a2.name), Decimal("0"))

		# (b) Percent routing limit (50%): correcting 50k -> 200k splits 100k W2 + 100k W1
		stay_b = self.stay(2)
		w1_b = guest_folio(stay_b.name)
		w2_b = folio_mod.open_window(reservation=stay_b.name, payee_type="Company",
		                             payee_company=corp([]), payment_method="Company Credit")
		route(["#2000"], reservation=stay_b.name, target_type="Window", target_window=2,
		      limit_type="Percent", limit_value=50)
		api.post_stay_charge(stay_b.name, "Food & Beverage", "Dining Pct", 100000,
		                     transaction_code="2000")
		row_b = frappe.db.get_value("Folio Charge", {"parent": w2_b, "description": "Dining Pct"}, "name")
		corr_b = api.correct_folio_charge(w2_b, row_b, reason="Pct upward", amount=200000)
		self.assertEqual(float(frappe.db.get_value("Folio", w2_b, "charges_total")), 100000.0)
		self.assertEqual(float(frappe.db.get_value("Folio", w1_b, "charges_total")), 150000.0)
		self.assertEqual(corr_b.get("overflow_folio"), w1_b)
		self.assertTrue(corr_b.get("overflow_row"))
		self.assertTrue(ledger.folio_reconciliation(PROP_A)["in_balance"])

		# (c) Amount-limit overflow idempotency replay keeps overflow_folio and overflow_row
		stay_c = make_reservation(PROP_A, self.rt, d(-1), d(2), status="Checked In",
		                          room=make_room(PROP_A, self.rt, "342"))
		w1_c = guest_folio(stay_c.name)
		w2_c = folio_mod.open_window(reservation=stay_c.name, payee_type="Company",
		                             payee_company=corp([]), payment_method="Company Credit")
		route(["#2000"], reservation=stay_c.name, target_type="Window", target_window=2,
		      limit_type="Amount", limit_value=100000)
		api.post_stay_charge(stay_c.name, "Food & Beverage", "Idem FnB", 80000,
		                     transaction_code="2000")
		row_c = frappe.db.get_value("Folio Charge", {"parent": w2_c, "description": "Idem FnB"}, "name")
		first_c = api.correct_folio_charge(w2_c, row_c, reason="Upward idem", amount=250000,
		                                   idempotency_key="ut-r4-idem-up")
		replay_c = api.correct_folio_charge(w2_c, row_c, reason="Upward idem", amount=250000,
		                                    idempotency_key="ut-r4-idem-up")
		self.assertEqual(first_c.get("overflow_folio"), w1_c)
		self.assertTrue(first_c.get("overflow_row"))
		self.assertEqual(replay_c.get("idempotent_replay"), 1)
		self.assertEqual(replay_c.get("reversal"), first_c["reversal"])
		self.assertEqual(replay_c.get("repost"), first_c["repost"])
		self.assertEqual(replay_c.get("overflow_folio"), first_c["overflow_folio"])
		self.assertEqual(replay_c.get("overflow_row"), first_c["overflow_row"])

	# Hydrogen R3 P1-E: real two-connection MariaDB 11.8 snapshot-isolation race

	def test_concurrent_post_and_correction_retry_snapshot_conflicts_without_500(self):
		import json
		import os
		import subprocess
		import sys
		import tempfile

		# Release any locks held by AuditTestCase.setUp (e.g. tabSeries / Transaction Code)
		# before spawning real MariaDB worker processes, and re-arm the savepoint in finally.
		from kamra.tests.audit_fixtures import SAVEPOINT
		frappe.db.rollback()
		tag = frappe.generate_hash(length=6)
		pname = f"UT Conc R4 {tag}"
		site = frappe.local.site
		bench_sites = os.path.abspath(frappe.get_site_path(".."))

		setup_code = (
			"import json, os, sys, frappe\n"
			f"os.chdir({bench_sites!r})\n"
			f"frappe.init(site={site!r})\n"
			"frappe.connect()\n"
			"frappe.set_user('Administrator')\n"
			f"pname, tag, today, checkout = {pname!r}, {tag!r}, {TODAY!r}, {d(2)!r}\n"
			"frappe.get_doc({\n"
			"    'doctype': 'Property', 'property_name': pname, 'country': 'Uganda',\n"
			"    'currency': 'UGX', 'city': 'Kampala', 'state': 'Central',\n"
			"    'gst_mode': 'Fixed', 'rates_include_tax': 0, 'business_date': today,\n"
			"    'default_company_billing': 'Nothing (guest pays unless rules say so)',\n"
			"    'disabled': 0,\n"
			"}).insert(ignore_permissions=True)\n"
			"from kamra import api, ledger\n"
			"ledger.ensure_default_transaction_codes()\n"
			"rt = frappe.get_doc({\n"
			"    'doctype': 'Room Type', 'property': pname, 'room_type_code': 'CSTD',\n"
			"    'room_type_name': 'Conc Std', 'base_price': 100000, 'adults_capacity': 2,\n"
			"}).insert(ignore_permissions=True).name\n"
			"corps, guests = [], []\n"
			"def mk_case(label):\n"
			"    rm = frappe.get_doc({'doctype': 'Room', 'property': pname, 'room_type': rt, 'room_number': f'CR{label}'}).insert(ignore_permissions=True).name\n"
			"    g = frappe.get_doc({'doctype': 'Guest', 'first_name': f'UT Conc {tag} {label}'}).insert(ignore_permissions=True).name\n"
			"    guests.append(g)\n"
			"    comp = frappe.get_doc({'doctype': 'Corporate Account', 'company_name': f'UT Conc Corp {tag} {label}', 'credit_allowed': 1}).insert(ignore_permissions=True).name\n"
			"    corps.append(comp)\n"
			"    res = frappe.get_doc({\n"
			"        'doctype': 'Reservation', 'property': pname, 'room_type': rt, 'guest': g,\n"
			"        'status': 'Confirmed', 'room': rm, 'company': comp,\n"
			"        'check_in_date': today, 'check_out_date': checkout, 'adults': 1, 'children': 0,\n"
			"    })\n"
			"    res.flags.allow_past_check_in = True\n"
			"    res.insert(ignore_permissions=True)\n"
			"    res.status = 'Checked In'\n"
			"    res.save(ignore_permissions=True)\n"
			"    w2 = api.open_folio_window(reservation=res.name, payee_type='Company', payee_company=comp)['folio']\n"
			"    w1 = frappe.db.get_value('Folio', {'reservation': res.name, 'window_no': 1})\n"
			"    ri = frappe.get_doc({\n"
			"        'doctype': 'Routing Instruction', 'property': pname, 'reservation': res.name,\n"
			"        'target_type': 'Window', 'target_window': 2,\n"
			"        'codes': [{'transaction_code': '2000'}],\n"
			"        'limit_type': 'Amount', 'limit_value': 100000,\n"
			"        'from_date': today, 'to_date': checkout,\n"
			"    }).insert(ignore_permissions=True).name\n"
			"    return {'res': res.name, 'win1': w1, 'win2': w2, 'ri': ri}\n"
			"c1, c2, c3 = mk_case('A'), mk_case('B'), mk_case('C')\n"
			"api.post_stay_charge(c2['res'], 'Food & Beverage', 'Base 60k', 60000, transaction_code='2000')\n"
			"c2['row'] = frappe.db.get_value('Folio Charge', {'parent': c2['win2'], 'description': 'Base 60k'}, 'name')\n"
			"api.post_stay_charge(c3['res'], 'Food & Beverage', 'C3 Row 1', 40000, transaction_code='2000')\n"
			"api.post_stay_charge(c3['res'], 'Food & Beverage', 'C3 Row 2', 40000, transaction_code='2000')\n"
			"c3['row1'] = frappe.db.get_value('Folio Charge', {'parent': c3['win2'], 'description': 'C3 Row 1'}, 'name')\n"
			"c3['row2'] = frappe.db.get_value('Folio Charge', {'parent': c3['win2'], 'description': 'C3 Row 2'}, 'name')\n"
			"frappe.db.commit()\n"
			"print(json.dumps({'c1': c1, 'c2': c2, 'c3': c3, 'corps': corps, 'guests': guests}))\n"
		)

		worker_code = (
			"import json, os, sys, time, frappe\n"
			f"os.chdir({bench_sites!r})\n"
			f"frappe.init(site={site!r})\n"
			"frappe.connect()\n"
			"frappe.set_user('Administrator')\n"
			"mode, payload, bf, wid = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3], sys.argv[4]\n"
			"frappe.db.sql('SELECT COUNT(*) FROM `tabRouting Instruction` WHERE name=%s', (payload['ri'],))\n"
			"with open(bf + '.' + wid, 'w') as fh: fh.write('1')\n"
			"other = '2' if wid == '1' else '1'\n"
			"t0 = time.time()\n"
			"while not os.path.exists(bf + '.' + other) and time.time() - t0 < 10: time.sleep(0.01)\n"
			"from kamra import api\n"
			"try:\n"
			"    if mode == 'post80':\n"
			"        res = api.post_stay_charge(payload['res'], 'Food & Beverage', 'Conc Post ' + wid, 80000, transaction_code='2000')\n"
			"    else:\n"
			"        res = api.correct_folio_charge(payload['win2'], payload['row'], reason='Conc corr', amount=int(sys.argv[5]))\n"
			"    frappe.db.commit()\n"
			"    print(json.dumps({'worker': wid, 'ok': True, 'res': res}, default=str))\n"
			"except Exception as e:\n"
			"    frappe.db.rollback()\n"
			"    print(json.dumps({'worker': wid, 'ok': False, 'error': f'{type(e).__name__}: {e}'}, default=str))\n"
		)

		verify_code = (
			"import json, os, sys, frappe\n"
			f"os.chdir({bench_sites!r})\n"
			f"frappe.init(site={site!r})\n"
			"frappe.connect()\n"
			"data = json.loads(sys.argv[1])\n"
			"out = {}\n"
			"for k in ('c1', 'c2', 'c3'):\n"
			"    d = data[k]\n"
			"    out[k] = [\n"
			"        float(frappe.db.get_value('Folio', d['win2'], 'charges_total') or 0),\n"
			"        float(frappe.db.get_value('Folio', d['win1'], 'charges_total') or 0),\n"
			"    ]\n"
			"print(json.dumps(out))\n"
		)

		cleanup_code = (
			"import json, os, sys, frappe\n"
			f"os.chdir({bench_sites!r})\n"
			f"frappe.init(site={site!r})\n"
			"frappe.connect()\n"
			"frappe.set_user('Administrator')\n"
			f"pname = {pname!r}\n"
			"data = json.loads(sys.argv[1])\n"
			"folio_names = frappe.get_all('Folio', filters={'property': pname}, pluck='name')\n"
			"if folio_names:\n"
			"    frappe.db.delete('Folio Charge', {'parent': ('in', folio_names)})\n"
			"    frappe.db.delete('Folio Payment', {'parent': ('in', folio_names)})\n"
			"ri_names = frappe.get_all('Routing Instruction', filters={'property': pname}, pluck='name')\n"
			"if ri_names:\n"
			"    frappe.db.delete('Routing Instruction Code', {'parent': ('in', ri_names)})\n"
			"res_names = frappe.get_all('Reservation', filters={'property': pname}, pluck='name')\n"
			"if res_names:\n"
			"    frappe.db.delete('Reservation Night Rate', {'parent': ('in', res_names)})\n"
			"for dt in ('Folio Ledger Entry', 'Agent Action Log', 'Routing Instruction',\n"
			"           'Folio', 'Reservation', 'Room', 'Room Type', 'Property Setting'):\n"
			"    frappe.db.delete(dt, {'property': pname})\n"
			"for c in data.get('corps', []):\n"
			"    frappe.db.delete('Company Billing Rule', {'parent': c})\n"
			"    frappe.db.delete('Corporate Account', {'name': c})\n"
			"for g in data.get('guests', []):\n"
			"    frappe.db.delete('Guest', {'name': g})\n"
			"frappe.db.delete('Property', {'name': pname})\n"
			"frappe.db.commit()\n"
		)

		def run_pair(m1, p1, a1, m2, p2, a2):
			bf = tempfile.mktemp(prefix="ut_kn43_conc_")
			try:
				pa = subprocess.Popen([sys.executable, "-c", worker_code, m1, json.dumps(p1), bf, "1", *a1],
				                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
				pb = subprocess.Popen([sys.executable, "-c", worker_code, m2, json.dumps(p2), bf, "2", *a2],
				                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
				oa, ea = pa.communicate(timeout=30)
				ob, eb = pb.communicate(timeout=30)
				self.assertTrue(oa.strip(), f"worker 1 empty stdout; stderr={ea}")
				self.assertTrue(ob.strip(), f"worker 2 empty stdout; stderr={eb}")
				return json.loads(oa.strip().splitlines()[-1]), json.loads(ob.strip().splitlines()[-1])
			finally:
				for sfx in (".1", ".2"):
					if os.path.exists(bf + sfx):
						os.remove(bf + sfx)

		data = None
		try:
			setup_out = subprocess.check_output([sys.executable, "-c", setup_code], text=True, timeout=30)
			data = json.loads(setup_out.strip().splitlines()[-1])
			c1, c2, c3 = data["c1"], data["c2"], data["c3"]

			r1a, r1b = run_pair("post80", c1, [], "post80", c1, [])
			r2a, r2b = run_pair("post80", c2, [], "corr_to", c2, ["90000"])
			r3a, r3b = run_pair("corr_to", {**c3, "row": c3["row1"]}, ["90000"],
			                    "corr_to", {**c3, "row": c3["row2"]}, ["90000"])

			self.assertTrue(r1a["ok"] and r1b["ok"], (r1a, r1b))
			self.assertTrue(r2a["ok"] and r2b["ok"], (r2a, r2b))
			self.assertTrue(r3a["ok"] and r3b["ok"], (r3a, r3b))

			ver_out = subprocess.check_output(
				[sys.executable, "-c", verify_code, json.dumps(data)], text=True, timeout=30)
			totals = json.loads(ver_out.strip().splitlines()[-1])
			self.assertEqual(totals["c1"], [100000.0, 60000.0])
			self.assertEqual(totals["c2"], [100000.0, 70000.0])
			self.assertEqual(totals["c3"], [100000.0, 80000.0])
		finally:
			if data is not None:
				subprocess.run([sys.executable, "-c", cleanup_code, json.dumps(data)],
				               check=False, timeout=30)
			frappe.db.rollback()
			frappe.db.savepoint(SAVEPOINT)

	# Hydrogen R3 P1-F: K1/R3 backend _() messages in all 7 locale CSVs ----

	def test_k1_backend_messages_translated_in_all_seven_locales(self):
		import csv
		import re
		from pathlib import Path

		required = (
			"Transaction code {0} is not an enabled charge code.",
			"Transaction code {0} has no charge type. Set one under Transaction codes.",
			"Unexpected parameter(s) for route_outlet_charge: {0}. Pass a transaction_code instead (is_alcohol was removed).",
			"Cannot route to reservation {0} because its status is {1}.",
			"Cannot route to cancelled group {0}.",
			"Cannot create or keep an active routing instruction for cancelled group {0}.",
			"A company window names the paying company, and only that.",
			"A guest window names no company or agent.",
			"A travel agent window names the paying agent, and only that.",
			"A group's master bill needs its Group Booking.",
			"A group's master bill must be at the group's property.",
			"Window must be from 1 to {0}.",
			"Window {0} already has an open bill.",
			"A bill can have at most {0} windows. Close a window or move its charges to another one.",
			"Open a window on a stay or on a group's master bill.",
			"Reservation {0} not found.",
			"Group {0} not found.",
			"Group {0} is cancelled, so it has no master bill.",
			"A routing instruction is for one stay or for one group.",
			"The stay or group must be at {0}.",
			"Target window must be from 1 to {0}.",
			"Choose the window to route to.",
			"A window instruction routes within the stay; leave the target room empty.",
			"Choose the room to route to.",
			"Charges can only be routed to a room at the same property.",
			"To route within the stay, route to one of its windows.",
			"This stay is not part of a group, so it has no group master.",
			"The group's bill must be at the same property.",
			"A group master instruction has no target room.",
			"Route to a window, a room or the group master.",
			"Each row names a charge type or a transaction code, not both.",
			"All Charges routes everything; remove the rows.",
			"Choose the charge types or codes to route, or tick All Charges.",
			"An instruction for all charges cannot carry a limit.",
			"An amount limit must be more than zero.",
			"A percent limit must be more than 0 and at most 100.",
			"To Date is before From Date.",
			"Tick at least one day of the week.",
			"Instruction {0} already routes some of these charges on some of these days. "
			"Only one instruction may cover a charge on a date: change its dates or charges, or disable it.",
			"Bill-to for {0}: {1}.",
		)
		placeholder_re = re.compile(r"\{[^{}]*\}")
		trans_dir = Path(frappe.get_app_path("kamra", "translations"))
		for lang in ("ar", "de", "es", "fil", "id", "pt", "zh"):
			csv_path = trans_dir / f"{lang}.csv"
			self.assertTrue(csv_path.is_file(), f"missing translation CSV: {csv_path.name}")
			seen: dict[str, str] = {}
			with csv_path.open(encoding="utf-8", newline="") as fh:
				for row in csv.reader(fh):
					if not row or len(row) < 2:
						continue
					src, dst = row[0], row[1]
					if src == "source":
						continue
					self.assertNotIn(src, seen, f"{lang}.csv: duplicate source {src!r}")
					seen[src] = dst
			for msg in required:
				self.assertIn(msg, seen, f"{lang}.csv missing {msg!r}")
				self.assertTrue(seen[msg].strip(), f"{lang}.csv empty translation for {msg!r}")
				self.assertEqual(
					sorted(placeholder_re.findall(msg)),
					sorted(placeholder_re.findall(seen[msg])),
					f"{lang}.csv placeholder mismatch for {msg!r}: {seen[msg]!r}",
				)

	# Hydrogen R3 P2-A: migration edges (master_reservation=None, multi-prop, orphan)

	def test_migration_handles_no_master_groups_and_multi_property_or_orphan_companies(self):
		import importlib
		make_property(PROP_A, business_date=TODAY)
		make_property(PROP_B, business_date=TODAY)
		frappe.db.set_value("Property", PROP_A, "default_company_billing",
		                    "Room and F&B (liquor to guest)")
		frappe.db.set_value("Property", PROP_B, "default_company_billing", "Room only")
		frappe.clear_document_cache("Property", PROP_A)
		frappe.clear_document_cache("Property", PROP_B)
		settings.clear_cache(PROP_A)
		settings.clear_cache(PROP_B)

		# 1. Confirmed group with master_reservation=None and liquor-excluding preset
		gb_no_master = frappe.get_doc({
			"doctype": "Group Booking", "property": PROP_A,
			"group_name": "UT NoMaster PreK1 Group", "check_in_date": TODAY,
			"check_out_date": d(2), "status": "Confirmed",
		}).insert(ignore_permissions=True)
		frappe.db.set_value("Group Booking", gb_no_master.name, {
			"master_reservation": None,
			"master_pays": "Room and F&B (liquor to guest)",
		})

		# 2. Ruleless credit company spanning PROP_A (liquor-excl) and PROP_B (Room only)
		c_multi = frappe.get_doc({
			"doctype": "Corporate Account", "company_name": "UT MultiProp Ruleless Corp",
			"credit_allowed": 1,
		}).insert(ignore_permissions=True).name
		stay_a = make_reservation(PROP_A, self.rt, d(-1), d(2), status="Checked In",
		                          room=make_room(PROP_A, self.rt, "351"),
		                          company=c_multi, booking_type="Corporate")
		rt_b = make_room_type(PROP_B, "STDB", base_price=1000)
		stay_b = make_reservation(PROP_B, rt_b, d(-1), d(2), status="Checked In",
		                          room=make_room(PROP_B, rt_b, "451"),
		                          company=c_multi, booking_type="Corporate")

		# 3. Orphan ruleless credit company on a mixed-preset multi-property site
		c_orphan = frappe.get_doc({
			"doctype": "Corporate Account", "company_name": "UT Orphan Ruleless Corp",
			"credit_allowed": 1,
		}).insert(ignore_permissions=True).name

		mig = importlib.import_module("kamra.patches.v48.migrate_liquor_exclusion_to_code_rules")
		ren = importlib.import_module("kamra.patches.v48.rename_company_billing_presets")
		mig.execute()
		ren.execute()
		ri_after_1 = frappe.get_all("Routing Instruction",
		                            filters={"group_booking": gb_no_master.name}, pluck="name")
		stay_a_ris_1 = frappe.get_all("Routing Instruction",
		                              filters={"reservation": stay_a.name}, pluck="name")
		# Idempotent second run
		mig.execute()
		ren.execute()
		self.assertEqual(
			frappe.get_all("Routing Instruction", filters={"group_booking": gb_no_master.name}, pluck="name"),
			ri_after_1,
		)
		self.assertEqual(
			frappe.get_all("Routing Instruction", filters={"reservation": stay_a.name}, pluck="name"),
			stay_a_ris_1,
		)

		# No-master group was NOT renamed to liquor-including "Room and F&B" and has non-alcohol group RI
		self.assertEqual(
			frappe.db.get_value("Group Booking", gb_no_master.name, "master_pays"),
			"Nothing (guest pays unless rules say so)",
		)
		self.assertEqual(len(ri_after_1), 1)
		gb_codes = {r.transaction_code for r in frappe.get_doc("Routing Instruction", ri_after_1[0]).codes}
		self.assertIn("2000", gb_codes)
		self.assertNotIn("2010", gb_codes)

		# Multi-property and orphan companies did NOT get global F&B rules across Room-only properties
		self.assertEqual(frappe.get_doc("Corporate Account", c_multi).billing_rules, [])
		self.assertEqual(frappe.get_doc("Corporate Account", c_orphan).billing_rules, [])
		# PROP_B (Room only) stays keep Food on Guest; PROP_A active stay routes Food to Company & Beer to Guest
		self.assertEqual(
			api.post_stay_charge(stay_b.name, "Food & Beverage", "PropB Food", 500, transaction_code="2000")["folio_type"],
			"Guest",
		)
		self.assertEqual(
			api.post_stay_charge(stay_a.name, "Food & Beverage", "PropA Food", 500, transaction_code="2000")["folio_type"],
			"Company",
		)
		self.assertEqual(
			api.post_stay_charge(stay_a.name, "Food & Beverage", "PropA Beer", 400, transaction_code="2010")["folio_type"],
			"Guest",
		)
