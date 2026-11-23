"""Folio windows: a stay or a posting master holds up to the property's
`folio_windows` bills (OPERA billing windows 1 to 8). Each window has its own
payee and payment method and closes to its own invoice (SAP: one billing
document per payer). Window 1 is the guest's and takes unrouted charges.
"""

from __future__ import annotations

import importlib

import frappe

from kamra import api, ledger, settings
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

CORP_A = "UT Windows Corp A"
CORP_B = "UT Windows Corp B"


def corp(name: str, credit: int = 1, rules=()) -> str:
	if frappe.db.exists("Corporate Account", name):
		doc = frappe.get_doc("Corporate Account", name)
	else:
		doc = frappe.get_doc({"doctype": "Corporate Account", "company_name": name})
	doc.credit_allowed = credit
	doc.set("billing_rules", [{"charge_type": c, "pay_by": p} for c, p in rules])
	if doc.is_new():
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)
	return doc.name


def route(reservation: str, codes=(), **fields):
	return frappe.get_doc({
		"doctype": "Routing Instruction", "property": PROP_A,
		"reservation": reservation,
		"codes": [{"charge_type": c} for c in codes],
		**fields,
	}).insert(ignore_permissions=True)


def window(folio: str):
	return frappe.db.get_value(
		"Folio", folio,
		["window_no", "payee_type", "payee_company", "payee_agent",
		 "payment_method", "folio_type", "bill_to_name"], as_dict=True)


class FolioWindowsTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY)
		settings.clear_cache(PROP_A)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.res = make_reservation(PROP_A, self.rt, d(-1), d(2), status="Checked In",
		                            room=make_room(PROP_A, self.rt, "201"))

	def tearDown(self):
		settings.clear_cache(PROP_A)
		super().tearDown()

	def test_window_numbers_and_cap(self):
		guest = folio_mod.open_folio(self.res)
		self.assertEqual((window(guest).window_no, window(guest).payee_type), (1, "Guest"))
		w2 = folio_mod.open_window(reservation=self.res.name, payee_type="Company",
		                           payee_company=corp(CORP_A), payment_method="Company Credit")
		self.assertEqual(
			{k: window(w2)[k] for k in ("window_no", "payee_type", "payee_company",
			                            "payment_method", "folio_type")},
			{"window_no": 2, "payee_type": "Company", "payee_company": CORP_A,
			 "payment_method": "Company Credit", "folio_type": "Company"})
		w3 = api.open_folio_window(reservation=self.res.name, payee_type="Guest",
		                           bill_to_name="UT Second Guest")
		self.assertEqual((window(w3["folio"]).window_no, w3["window_no"],
		                  window(w3["folio"]).bill_to_name), (3, 3, "UT Second Guest"))
		# one open folio per window
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Folio", "property": PROP_A, "status": "Open",
			                "reservation": self.res.name, "folio_type": "Extra",
			                "window_no": 2}).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Folio", "property": PROP_A, "status": "Open",
			                "reservation": self.res.name, "folio_type": "Extra",
			                "window_no": 9}).insert(ignore_permissions=True)
		numbers = [window(folio_mod.open_window(reservation=self.res.name)).window_no
		           for _ in range(5)]
		self.assertEqual(numbers, [4, 5, 6, 7, 8])
		with self.assertRaisesRegex(frappe.ValidationError, "8"):
			folio_mod.open_window(reservation=self.res.name)

	def test_window_cap_is_the_propertys_setting(self):
		settings.write_kv(PROP_A, "folio_windows", 3)
		settings.clear_cache(PROP_A)
		folio_mod.open_folio(self.res)
		folio_mod.open_window(reservation=self.res.name)
		folio_mod.open_window(reservation=self.res.name)
		with self.assertRaisesRegex(frappe.ValidationError, "3"):
			folio_mod.open_window(reservation=self.res.name)

	def test_payee_fields_match_the_payee_type(self):
		with self.assertRaises(frappe.ValidationError):
			folio_mod.open_window(reservation=self.res.name, payee_type="Company")
		with self.assertRaises(frappe.ValidationError):
			folio_mod.open_window(reservation=self.res.name, payee_type="Guest",
			                      payee_company=corp(CORP_A))
		with self.assertRaises(frappe.ValidationError):
			folio_mod.open_window(reservation=self.res.name, payee_type="Travel Agent")

	def test_two_corporate_payers_one_stay(self):
		w2 = folio_mod.open_window(reservation=self.res.name, payee_type="Company",
		                           payee_company=corp(CORP_A))
		w3 = folio_mod.open_window(reservation=self.res.name, payee_type="Company",
		                           payee_company=corp(CORP_B))
		route(self.res.name, ["Room"], target_type="Window", target_window=2)
		route(self.res.name, ["Food & Beverage"], target_type="Window", target_window=3)
		folio_mod.post_room_night(self.res, d(-1))
		dinner = api.post_stay_charge(self.res.name, "Food & Beverage", "UT dinner", 500)
		self.assertEqual(dinner["folio"], w3)
		rooms = frappe.get_all("Folio Charge", filters={"reservation": self.res.name,
		                                                "charge_type": "Room"}, pluck="parent")
		self.assertEqual(rooms, [w2])
		self.assertEqual(ledger.company_payer_of(frappe.get_doc("Folio", w2)), CORP_A)
		self.assertEqual(ledger.company_payer_of(frappe.get_doc("Folio", w3)), CORP_B)

	def test_company_payer_follows_window_payee(self):
		a = corp(CORP_A)
		billed = folio_mod.open_window(reservation=self.res.name, payee_type="Company",
		                               payee_company=a)
		# the kind of account no longer decides who pays: the payee does
		frappe.db.set_value("Folio", billed, "folio_type", "Extra")
		doc = frappe.get_doc("Folio", billed)
		self.assertEqual(ledger.company_payer_of(doc), a)
		self.assertEqual(ledger._transfer_company(doc), a)
		self.res.db_set({"company": a, "booking_type": "Corporate"})
		own = folio_mod.open_window(reservation=self.res.name, payee_type="Guest")
		frappe.db.set_value("Folio", own, "folio_type", "Company")
		self.assertIsNone(ledger.company_payer_of(frappe.get_doc("Folio", own)))

	def test_backfill_windows_patch(self):
		a = corp(CORP_A)
		self.res.db_set({"company": a, "booking_type": "Corporate"})
		guest = folio_mod.open_folio(self.res)
		company = folio_mod.split_folio(self.res.name, "Company")
		extra = folio_mod.open_window(reservation=self.res.name, bill_to_name="UT Friend")
		gb = frappe.get_doc({"doctype": "Group Booking", "property": PROP_A,
		                     "group_name": "UT Backfill Group", "company": a,
		                     "check_in_date": d(0), "check_out_date": d(2),
		                     "status": "Confirmed"}).insert(ignore_permissions=True)
		first_pm = folio_mod.open_group_folio(gb.name)
		frappe.db.set_value("Folio", first_pm, "status", "Closed")
		second_pm = folio_mod.open_window(group_booking=gb.name)
		legacy = [guest, company, extra, first_pm, second_pm]
		# as the rows stood after the migration added the columns: no window
		# (an Int column is 0, never NULL), no payee
		for name in legacy:
			frappe.db.set_value("Folio", name, {
				"window_no": 0, "payee_type": None, "payee_company": None,
				"payee_agent": None, "payment_method": None})
		importlib.import_module("kamra.patches.v48.backfill_folio_windows").execute()
		got = {name: window(name) for name in legacy}
		self.assertEqual([got[n].window_no for n in legacy], [1, 2, 3, 1, 2])
		self.assertEqual([got[n].payee_type for n in legacy],
		                 ["Guest", "Company", "Guest", "Company", "Company"])
		self.assertEqual((got[company].payee_company, got[company].payment_method),
		                 (a, "Company Credit"))
		self.assertEqual(got[first_pm].payee_company, a)
		self.assertEqual(got[extra].bill_to_name, "UT Friend")
		# run twice, nothing moves
		importlib.import_module("kamra.patches.v48.backfill_folio_windows").execute()
		self.assertEqual({n: window(n) for n in legacy}, got)
