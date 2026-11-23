"""A group with a main room that pays.

The main room (Group Booking.master_reservation) carries what `master_pays`
covers for every room on one group bill, with no company needed. A room can
opt out (pays_own_charges), liquor never routes, the main guest can leave
while the bill stays open, and settle_group_bill closes it. A company group
without a main room bills the company on a group bill anchored to its first
room: OPERA's posting master. That bill is the group's, not the first room's:
the room's own check-out, reinstatement, no-show or check-out sheet leaves it
and its advances alone, and only the group's own settlement closes it.
"""

from __future__ import annotations

import frappe

from kamra import api, ledger
from kamra import folio as folio_mod
from kamra.checkout import settlement_position
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

COMPANY = "UT Group Billing Corp"


class GroupBillingTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.rooms = [make_room(PROP_A, self.rt, n) for n in ("101", "102", "103")]

	def group(self, company=None, master=True, pays="Room only", status="Checked In"):
		gb = frappe.get_doc({
			"doctype": "Group Booking", "property": PROP_A,
			"group_name": "UT Sharma Wedding", "company": company,
			"check_in_date": d(-1), "check_out_date": d(2),
			"status": "Confirmed"}).insert(ignore_permissions=True)
		stays = [make_reservation(PROP_A, self.rt, d(-1), TODAY if i == 0 else d(2),
		                          status=status, room=room,
		                          group_booking=gb.name)
		         for i, room in enumerate(self.rooms)]
		if master:
			gb.db_set({"master_reservation": stays[0].name, "master_pays": pays})
		return gb, stays

	def post(self, res, charge_type, desc, amount=1000, alcohol=0):
		return api.post_stay_charge(res.name, charge_type, desc, amount,
		                            is_alcohol=alcohol)

	def test_main_room_pays_room_guest_pays_extras(self):
		r1, r2 = self.group()[1][:2]
		room = self.post(r2, "Room", "UT R2 night")
		self.assertEqual(room["folio_type"], "Group")
		self.assertEqual(frappe.db.get_value("Folio", room["folio"], "reservation"),
		                 r1.name)
		self.assertEqual(self.post(r2, "Minibar", "UT R2 minibar", 200)["folio_type"],
		                 "Guest")

	def test_room_that_pays_its_own_keeps_its_room_charge(self):
		r3 = self.group()[1][2]
		api.set_pays_own_charges(r3.name, 1)
		self.assertEqual(self.post(r3, "Room", "UT R3 night")["folio_type"], "Guest")

	def test_alcohol_never_routes(self):
		r2 = self.group(pays="Everything except liquor")[1][1]
		self.assertEqual(self.post(r2, "Food & Beverage", "UT lunch", 500)["folio_type"],
		                 "Group")
		self.assertEqual(self.post(r2, "Food & Beverage", "UT whisky", 800, 1)["folio_type"],
		                 "Guest")

	def test_main_guest_leaves_and_the_bill_stays_open(self):
		r1, r2 = self.group()[1][:2]
		bill = self.post(r2, "Room", "UT R2 night")["folio"]
		self.assertNotIn(bill, [f["name"] for f in settlement_position(r1.name)["folios"]])
		api.check_out(r1.name)
		self.assertEqual(frappe.db.get_value("Reservation", r1.name, "status"),
		                 "Checked Out")
		self.assertEqual(frappe.db.get_value("Folio", bill, "status"), "Open")
		self.assertEqual(self.post(r2, "Room", "UT R2 late night")["folio"], bill)

	def test_settle_group_bill_closes_it(self):
		gb, (_r1, r2, _r3) = self.group()
		bill = self.post(r2, "Room", "UT R2 night")["folio"]
		out = api.settle_group_bill(gb.name, mode="Cash")
		self.assertTrue(out["closed"])
		self.assertEqual(out["balance"], 0)
		self.assertEqual(frappe.db.get_value("Folio", bill, "status"), "Closed")
		self.assertTrue(out["invoice_number"])
		self.assertIn("still in house", out["warning"])

	def test_company_group_settles_to_the_city_ledger(self):
		gb, (_r1, r2, _r3) = self.group(company=_corp())
		bill = self.post(r2, "Room", "UT R2 night")["folio"]
		out = api.settle_group_bill(gb.name, to_city_ledger=1)
		self.assertTrue(out["closed"])
		self.assertTrue(frappe.db.exists("City Ledger Entry",
		                                 {"folio": bill, "kind": "Invoice"}))

	def test_set_group_master_reanchors_the_open_bill(self):
		gb, (_r1, r2, _r3) = self.group()
		bill = self.post(r2, "Room", "UT R2 night")["folio"]
		api.set_group_master(gb.name, r2.name)
		self.assertEqual(frappe.db.get_value("Folio", bill, "reservation"), r2.name)
		self.assertEqual(frappe.db.get_value("Group Booking", gb.name,
		                                     "master_reservation"), r2.name)

	def test_company_group_bill_stays_open_when_the_first_room_leaves(self):
		gb, (r1, r2, _r3) = self.group(company=_corp(), master=False)
		bill = self.post(r2, "Room", "UT R2 night")["folio"]
		# anchored to the first room, but the group's bill, not that stay's
		self.assertEqual(frappe.db.get_value("Folio", bill, ["folio_type", "reservation"]),
		                 ("Group", r1.name))
		position = settlement_position(r1.name)
		self.assertNotIn(bill, [f for f, _c, _b in position["to_city_ledger"]])
		self.assertNotIn(bill, [f["name"] for f in position["folios"]])
		api.check_out(r1.name)
		self.assertEqual(frappe.db.get_value("Folio", bill, "status"), "Open")
		self.assertFalse(frappe.db.exists("City Ledger Entry", {"folio": bill}))
		self.assertEqual(self.post(r2, "Room", "UT R2 next night")["folio"], bill)
		# the group settles it, with the group
		self.assertTrue(api.settle_group_bill(gb.name, to_city_ledger=1)["closed"])

	def test_advance_bill_counts_the_nights_routed_to_the_group(self):
		gb, (_r1, r2, _r3) = self.group(company=_corp(), master=False)
		out = ledger.force_advance_bill(r2.name, "entire")
		self.assertEqual(out["nights_posted"], [TODAY, d(1)])
		bill = frappe.db.get_value("Folio", {"group_booking": gb.name, "folio_type": "Group"})
		routed = [c for c in frappe.get_doc("Folio", bill).charges
		          if c.reservation == r2.name and str(c.posting_date) in out["nights_posted"]]
		self.assertEqual(len([c for c in routed if c.charge_type == "Room"]), 2)
		self.assertGreater(out["amount"], 0)
		self.assertAlmostEqual(out["amount"], sum(c.total for c in routed), 2)
		# pressed again, nothing posts and nothing is counted twice
		again = ledger.force_advance_bill(r2.name, "entire")
		self.assertEqual((again["nights_posted"], again["amount"]), ([], 0))

	def test_group_advance_follows_the_group_not_the_first_room(self):
		gb, (r1, r2, _r3) = self.group(company=_corp(), master=False, status="Confirmed")
		bill = folio_mod.open_group_folio(gb.name)
		self.assertEqual(frappe.db.get_value("Folio", bill, "reservation"), r1.name)
		api.add_folio_payment(bill, "Cash", 500, kind="Advance")
		own = folio_mod.open_folio(r1)
		api.add_folio_payment(own, "Cash", 300, kind="Advance")
		self.assertEqual((_held(bill), _held(own)), (500, 300))
		# the first room never arrives; another room of the group does
		_check_in(r2)
		self.assertEqual(_held(bill), 0)
		self.assertEqual(_applied(bill), 500)
		# the first room's own advance waits for that room
		self.assertEqual(_held(own), 300)

	def test_group_advance_taken_while_the_group_is_in_house_applies_at_once(self):
		gb, (r1, r2, _r3) = self.group(company=_corp(), master=False, status="Confirmed")
		_check_in(r2)
		bill = folio_mod.open_group_folio(gb.name)
		self.assertEqual(frappe.db.get_value("Folio", bill, "reservation"), r1.name)
		api.add_folio_payment(bill, "Cash", 500, kind="Advance")
		self.assertEqual((_held(bill), _applied(bill)), (0, 500))

	def test_advance_bill_returns_the_bill_the_nights_went_to(self):
		gb, (_r1, r2, _r3) = self.group(company=_corp(), master=False)
		own = folio_mod.open_folio(r2)
		stamp = frappe.db.get_value("Folio", own, "modified")
		out = ledger.force_advance_bill(r2.name, "entire")
		bill = frappe.db.get_value("Folio", {"group_booking": gb.name, "folio_type": "Group"})
		self.assertEqual(out["folio"], bill)
		self.assertEqual(out["balance"], frappe.db.get_value("Folio", bill, "balance"))
		# nothing landed on the guest's own bill, and it is not saved again
		self.assertEqual(frappe.db.get_value("Folio", own, "modified"), stamp)

	def test_a_first_room_that_never_arrives_leaves_the_group_advance_held(self):
		gb, (r1, _r2, _r3) = self.group(company=_corp(), master=False, status="Confirmed")
		bill = folio_mod.open_group_folio(gb.name)
		self.assertEqual(frappe.db.get_value("Folio", bill, "reservation"), r1.name)
		api.add_folio_payment(bill, "Cash", 5000, kind="Advance")
		# the first room's no-show or cancellation fee applies that room's own
		# advances; the group's stay held while no room of it is in house
		ledger.transfer_advances_to_guest(r1.name)
		folio_mod.post_policy_fee(frappe.get_doc("Reservation", r1.name), "First Night",
		                          "No-show charge (UT)")
		self.assertEqual((_held(bill), _applied(bill)), (5000, 0))

	def test_reinstating_the_first_room_leaves_the_settled_group_bill(self):
		gb, (r1, r2, _r3) = self.group(company=_corp(), master=False)
		bill = self.post(r2, "Room", "UT R2 night")["folio"]
		api.check_out(r1.name)
		self.assertTrue(api.settle_group_bill(gb.name, to_city_ledger=1)["closed"])
		number = frappe.db.get_value("Folio", bill, "invoice_number")
		self.assertTrue(number)
		api.reinstate_checkout(r1.name, "UT checked out the wrong room")
		self.assertEqual(frappe.db.get_value("Folio", bill, ["status", "invoice_number"]),
		                 ("Closed", number))
		self.assertFalse(frappe.db.exists("Cancelled Invoice", {"folio": bill}))
		self.assertTrue(frappe.db.exists("City Ledger Entry", {"folio": bill, "kind": "Invoice"}))

	def test_the_first_rooms_check_out_sheet_leaves_the_group_bill_out(self):
		_gb, (r1, r2, _r3) = self.group(company=_corp(), master=False)
		bill = self.post(r2, "Room", "UT R2 night", amount=5000)["folio"]
		self.assertEqual(frappe.db.get_value("Folio", bill, "reservation"), r1.name)
		ctx = api.checkout_context(r1.name)
		self.assertEqual(ctx["bill_split"]["on_folio_now"], 0)

	def test_the_first_rooms_check_in_reverses_whatever_the_group_bill_holds(self):
		gb = frappe.get_doc({
			"doctype": "Group Booking", "property": PROP_A, "group_name": "UT Offsite",
			"company": _corp(), "check_in_date": TODAY, "check_out_date": d(2),
			"status": "Confirmed"}).insert(ignore_permissions=True)
		r1, r2 = (make_reservation(PROP_A, self.rt, TODAY, d(2), status="Checked In",
		                           room=room, group_booking=gb.name)
		          for room in self.rooms[:2])
		bill = self.post(r2, "Room", "UT R2 night")["folio"]
		self.assertEqual(frappe.db.get_value("Folio", bill, "reservation"), r1.name)
		self.assertIsNone(api._reverse_check_in_blocker(frappe.get_doc("Reservation", r1.name)))

	def test_plain_group_without_main_room_routes_nothing(self):
		r2 = self.group(master=False)[1][1]
		self.assertEqual(self.post(r2, "Room", "UT R2 night")["folio_type"], "Guest")

	def test_create_group_booking_sets_the_main_room(self):
		out = api.create_group_booking(
			PROP_A, "UT Corporate Offsite", d(10), d(12),
			[{"room_type": self.rt, "count": 2, "guest_name": "UT Guest of group"}],
			guest_name="UT Lead", phone="+91 90000 55501",
			group_type="Wedding", master_index=1)
		self.assertEqual(len(out["created"]), 2, out["skipped"])
		self.assertEqual(out["master_reservation"], out["created"][1])
		gb = frappe.db.get_value("Group Booking", out["group_booking"],
		                         ["master_pays", "group_type"], as_dict=True)
		self.assertEqual((gb.master_pays, gb.group_type), ("Room only", "Wedding"))


def _check_in(res):
	doc = frappe.get_doc("Reservation", res.name)
	doc.flags.allow_past_check_in = True
	doc.status = "Checked In"
	doc.save(ignore_permissions=True)


def _ledger_sum(folio, ledger_name, description=None):
	filters = "AND description = %(d)s" if description else ""
	return round(float(frappe.db.sql(
		f"""SELECT COALESCE(SUM(credit - debit), 0) FROM `tabFolio Ledger Entry`
		    WHERE folio = %(f)s AND ledger = %(l)s {filters}""",
		{"f": folio, "l": ledger_name, "d": description})[0][0]), 2)


def _held(folio):
	"""What of the folio's advances still sits in the Deposit ledger."""
	return _ledger_sum(folio, "Deposit")


def _applied(folio):
	"""Advances moved to the Guest ledger to pay the stay."""
	return _ledger_sum(folio, "Guest", ledger.ADVANCE_APPLIED)


def _corp():
	if frappe.db.exists("Corporate Account", COMPANY):
		doc = frappe.get_doc("Corporate Account", COMPANY)
	else:
		doc = frappe.get_doc({"doctype": "Corporate Account", "company_name": COMPANY})
	doc.credit_allowed = 1
	doc.set("billing_rules", [{"charge_type": "Room", "pay_by": "Company"}])
	if doc.is_new():
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)
	return doc.name
