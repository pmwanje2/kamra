"""The posting master (OPERA PM): the group's own bill, with no stay behind
it. It opens with the Group Booking, before any room is picked up, takes
deposits and banquet postings, and is never anchored to the lead room, so a
member's check-out never settles it.
"""

from __future__ import annotations

import frappe

from kamra import api, banquet
from kamra import folio as folio_mod
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

COMPANY = "UT Posting Master Corp"


def corp(rules=(("Room", "Company"),)) -> str:
	if frappe.db.exists("Corporate Account", COMPANY):
		doc = frappe.get_doc("Corporate Account", COMPANY)
	else:
		doc = frappe.get_doc({"doctype": "Corporate Account", "company_name": COMPANY})
	doc.credit_allowed = 1
	doc.set("billing_rules", [{"charge_type": c, "pay_by": p} for c, p in rules])
	if doc.is_new():
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)
	return doc.name


class PostingMasterTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.rooms = [make_room(PROP_A, self.rt, n) for n in ("101", "102")]

	def group(self, company=None):
		return frappe.get_doc({
			"doctype": "Group Booking", "property": PROP_A,
			"group_name": "UT Posting Master Group", "company": company,
			"check_in_date": d(-1), "check_out_date": d(2),
			"status": "Confirmed"}).insert(ignore_permissions=True)

	def members(self, gb):
		return [make_reservation(PROP_A, self.rt, d(-1), TODAY if i == 0 else d(2),
		                         status="Checked In", room=room, group_booking=gb.name)
		        for i, room in enumerate(self.rooms)]

	def test_pm_opens_before_pickup(self):
		gb = self.group()
		pm = folio_mod.open_group_folio(gb.name)
		row = frappe.db.get_value(
			"Folio", pm, ["folio_type", "group_booking", "reservation", "window_no",
			              "property", "status"], as_dict=True)
		self.assertEqual(
			(row.folio_type, row.group_booking, row.reservation, row.window_no,
			 row.property, row.status),
			("Group", gb.name, None, 1, PROP_A, "Open"))
		self.assertEqual(api.group_master_folio(gb.name)["folio"], pm)

	def test_pm_takes_deposit_and_banquet_before_pickup(self):
		gb = self.group()
		venue = frappe.get_doc({"doctype": "Venue", "property": PROP_A,
		                        "venue_name": "UT PM Hall"}).insert(ignore_permissions=True)
		fn = frappe.get_doc({
			"doctype": "Venue Booking", "property": PROP_A, "venue": venue.name,
			"event_date": d(1), "customer_name": "UT PM Group", "status": "Confirmed",
			"group_booking": gb.name,
			"items": [{"item_type": "Food & Beverage", "item_name": "UT Dinner",
			           "qty": 10, "rate": 2000, "chargeable": 1, "gst_rate": 0}],
		}).insert(ignore_permissions=True)
		banquet.post_to_folio(fn.name)
		pm = frappe.db.get_value("Venue Booking", fn.name, "folio")
		self.assertEqual(api.group_master_folio(gb.name)["folio"], pm)
		api.add_folio_payment(pm, "Cash", 5000, kind="Advance")
		doc = frappe.get_doc("Folio", pm)
		self.assertIsNone(doc.reservation)
		self.assertEqual([(p.payment_kind, p.amount) for p in doc.payments],
		                 [("Advance", 5000)])
		self.assertEqual([c.amount for c in doc.charges], [20000])

	def test_pm_never_anchored_to_lead(self):
		gb = self.group(company=corp())
		r1, r2 = self.members(gb)
		bill = api.post_stay_charge(r2.name, "Room", "UT R2 night", 1000)["folio"]
		self.assertEqual(frappe.db.get_value("Folio", bill, ["folio_type", "reservation"]),
		                 ("Group", None))
		api.check_out(r1.name)
		self.assertEqual(frappe.db.get_value("Reservation", r1.name, "status"),
		                 "Checked Out")
		self.assertEqual(frappe.db.get_value("Folio", bill, "status"), "Open")
		self.assertFalse(frappe.db.exists("City Ledger Entry", {"folio": bill}))
		self.assertEqual(api.post_stay_charge(r2.name, "Room", "UT R2 next", 1000)["folio"],
		                 bill)

	def test_set_group_master_sets_payee_not_anchor(self):
		gb = self.group()
		r1, r2 = self.members(gb)
		gb.db_set({"master_reservation": r1.name, "master_pays": "Room only"})
		bill = api.post_stay_charge(r2.name, "Room", "UT R2 night", 1000)["folio"]
		api.set_group_master(gb.name, r2.name)
		row = frappe.db.get_value(
			"Folio", bill, ["reservation", "window_no", "payee_type", "guest"], as_dict=True)
		self.assertEqual((row.reservation, row.window_no, row.payee_type, row.guest),
		                 (None, 1, "Guest", r2.guest))
		self.assertEqual(frappe.db.get_value("Group Booking", gb.name, "master_reservation"),
		                 r2.name)

	def test_routed_line_keeps_origin_reservation(self):
		gb = self.group(company=corp())
		r2 = self.members(gb)[1]
		folio_mod.post_room_night(r2, d(-1))
		folio_mod.post_room_night(r2, d(-1))  # already posted: nothing twice
		pm = folio_mod.open_group_folio(gb.name)
		lines = frappe.get_all("Folio Charge", filters={"parent": pm, "charge_type": "Room"},
		                       fields=["reservation", "posting_date"])
		self.assertEqual([(r.reservation, str(r.posting_date)) for r in lines],
		                 [(r2.name, d(-1))])
		self.assertIsNone(frappe.db.get_value("Folio", pm, "reservation"))
		self.assertTrue(folio_mod._charge_posted(r2.name, "Room", d(-1)))

	def test_pm_moves_stay_within_its_group(self):
		# two posting masters have no stay: that never makes them one bill
		gb_a, gb_b = self.group(), self.group()
		pm_a, pm_b = (folio_mod.open_group_folio(g.name) for g in (gb_a, gb_b))
		api.add_folio_charge(pm_a, "Misc", "UT group sundry", 500, gst_rate=0)
		row = frappe.get_doc("Folio", pm_a).charges[-1].name
		with self.assertRaisesRegex(frappe.ValidationError, "different stays"):
			folio_mod.transfer_charges(pm_a, [row], pm_b)

	def test_open_pm_counts_in_company_exposure(self):
		from kamra.credit import in_house_exposure
		company = corp()
		gb = self.group(company=company)
		r1, r2 = self.members(gb)
		bill = api.post_stay_charge(r2.name, "Room", "UT R2 night", 1000)["folio"]
		# check-out posts the first room's own night to the group's bill too
		api.check_out(r1.name)
		owed = float(frappe.db.get_value("Folio", bill, "balance"))
		self.assertGreater(owed, 1000)
		# the group is still in house, so its open bill still counts
		self.assertEqual(in_house_exposure(company, PROP_A), round(owed, 2))
