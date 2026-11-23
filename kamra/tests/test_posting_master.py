"""The posting master (OPERA PM): the group's own bill, with no stay behind
it. It opens with the Group Booking, before any room is picked up, takes
deposits and banquet postings, and is never anchored to the lead room, so a
member's check-out never settles it.
"""

from __future__ import annotations

import frappe

from kamra import api, banquet, night_audit, settings
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

	# Neon R1 P1-6: a cancelled group has no posting master --------------

	def test_a_cancelled_group_opens_no_posting_master(self):
		gb = self.group(company=corp())
		r2 = self.members(gb)[1]
		frappe.get_doc({"doctype": "Routing Instruction", "property": PROP_A,
		                "reservation": r2.name, "target_type": "Group Master",
		                "codes": [{"charge_type": "Laundry"}]}).insert(ignore_permissions=True)
		gb.db_set("status", "Cancelled")
		# the instruction cannot reach the group's bill: window 1 takes it
		self.assertEqual(api.post_stay_charge(r2.name, "Laundry", "UT shirts", 300)
		                 ["folio_type"], "Guest")
		# nor do the presets bill a cancelled group's company
		self.assertEqual(api.post_stay_charge(r2.name, "Room", "UT night", 1000)
		                 ["folio_type"], "Guest")
		self.assertFalse(frappe.db.exists("Folio", {"group_booking": gb.name,
		                                            "folio_type": "Group"}))
		with self.assertRaises(frappe.ValidationError):
			folio_mod.open_group_folio(gb.name)

	# Neon R1 P1-8: the main room is window 1's payee, no other window's --

	def test_set_group_master_changes_window_1_only(self):
		gb = self.group()
		r2 = self.members(gb)[1]
		pm = folio_mod.open_group_folio(gb.name)
		frappe.db.set_value("Folio", pm, "status", "Closed")
		sponsor = corp()
		w2 = folio_mod.open_window(group_booking=gb.name, payee_type="Company",
		                           payee_company=sponsor)
		api.set_group_master(gb.name, r2.name)
		self.assertEqual(frappe.db.get_value("Folio", w2, ["payee_type", "payee_company"]),
		                 ("Company", sponsor))

	# Neon R1 P1-9: the night audit sees a posting master with no stay ----

	def test_night_audit_sees_an_open_posting_master(self):
		company = corp()
		gb = self.group(company=company)
		pm = folio_mod.open_group_folio(gb.name)
		api.add_folio_charge(pm, "Misc", "UT group sundry", 5000, gst_rate=0)
		settings.write_kv(PROP_A, "na_high_balance_threshold", 2000)
		settings.clear_cache(PROP_A)
		high = night_audit.check_high_balance(PROP_A)
		self.assertEqual([(i["folio"], i["balance"]) for i in high["items"]], [(pm, 5000)])
		frappe.get_doc({"doctype": "City Ledger Account", "property": PROP_A,
		                "company": company, "credit_limit": 3000}
		               ).insert(ignore_permissions=True)
		over = night_audit.check_credit_limit(PROP_A)
		self.assertEqual([(i["company"], i["in_house"], i["over_by"]) for i in over["items"]],
		                 [(company, 5000, 2000)])
		settings.clear_cache(PROP_A)

	# Hydrogen R2 P1: set_group_master uses doc.save() (validate_payee + hooks)

	def test_set_group_master_saves_folio_and_runs_validate_payee(self):
		from unittest.mock import patch
		gb = self.group()
		r1, r2 = self.members(gb)
		gb.db_set({"master_reservation": r1.name, "master_pays": "Room only"})
		bill = folio_mod.open_group_folio(gb.name)
		seen = []
		from kamra.kamra.doctype.folio.folio import Folio as FolioDoc
		orig_validate_payee = FolioDoc.validate_payee

		def spy_validate_payee(doc):
			seen.append((doc.name, doc.guest, doc.payee_type))
			return orig_validate_payee(doc)

		with patch.object(FolioDoc, "validate_payee", spy_validate_payee):
			api.set_group_master(gb.name, r2.name)
		self.assertIn((bill, r2.guest, "Guest"), seen)
