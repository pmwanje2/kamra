"""Routing instructions (OPERA reservation routing): a stay's charges of
chosen charge types or transaction codes go to another window, another room
or the group's posting master, on chosen dates and days of the week, up to
an amount or percent limit. Usage is derived from the posted lines, so it
cannot drift. Liquor routes only where an instruction says so.
"""

from __future__ import annotations

import frappe

from kamra import api, settings
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
	doc.set("billing_rules", [{"charge_type": c, "pay_by": p} for c, p in rules])
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

	def test_alcohol_off_by_default_configurable(self):
		ri = route(["Food & Beverage"], reservation=self.res.name, target_type="Window",
		           target_window=2)
		self.assertEqual(self.charge("Food & Beverage", 500, "UT lunch")["folio"], self.w2)
		self.assertEqual(self.charge("Food & Beverage", 800, "UT whisky", alcohol=1)["folio"],
		                 guest_folio(self.res.name))
		frappe.db.set_value("Routing Instruction", ri.name, "include_alcohol", 1)
		self.assertEqual(self.charge("Food & Beverage", 800, "UT whisky", alcohol=1)["folio"],
		                 self.w2)
		# moving liquor onto the posting master needs the group's route_alcohol
		gb = frappe.get_doc({"doctype": "Group Booking", "property": PROP_A,
		                     "group_name": "UT Liquor Group", "check_in_date": d(-2),
		                     "check_out_date": d(2),
		                     "status": "Confirmed"}).insert(ignore_permissions=True)
		member = self.stay(1, group_booking=gb.name)
		beer = self.charge("Food & Beverage", 300, "UT beer", alcohol=1, res=member)
		row = lines(beer["folio"])[-1].name
		pm = folio_mod.open_group_folio(gb.name)
		with self.assertRaisesRegex(frappe.ValidationError, "Alcohol"):
			folio_mod.transfer_charges(beer["folio"], [row], pm)
		gb.db_set("route_alcohol", 1)
		folio_mod.transfer_charges(beer["folio"], [row], pm)
		self.assertEqual([r.amount for r in lines(pm)], [300])
		# and a group's instruction takes its liquor default from the group
		template = route(["Minibar"], group_booking=gb.name, target_type="Group Master")
		self.assertEqual(template.include_alcohol, 1)

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

	def test_billing_rule_all_charge_types(self):
		meta = frappe.get_meta("Folio Charge").get_field("charge_type").options.split("\n")
		rule = frappe.get_meta("Company Billing Rule").get_field("charge_type").options.split("\n")
		self.assertEqual(set(rule), set(meta) - {"Allowance", "Opening Balance"})
		make_property(PROP_A, business_date=TODAY, room_levy_percent=10)
		other = self.stay(1, company=corp([("Room Levy", "Company")]),
		                  booking_type="Corporate")
		folio_mod.post_room_night(other, d(-2))
		company = folio_mod.open_company_folio(other)
		self.assertEqual([r.charge_type for r in lines(company)], ["Room Levy"])
		self.assertEqual([r.charge_type for r in lines(guest_folio(other.name))], ["Room"])
