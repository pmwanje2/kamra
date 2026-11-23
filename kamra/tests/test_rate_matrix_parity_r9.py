"""Round 9 of the rate matrix: Hydrogen's paired live verification (R5) on kamra#33 + siteops#8559.

P1-2 free children: capture_contract_tariff and contract_tariff_for_new_dates
must send the same billable children figure that Reservation.quote_inputs and
Kamra Tariff Resolve use (deducting infants and occupants at or under
Room Type.free_child_age), so quote, booking and amendment agree.
"""

from __future__ import annotations

import json

from kamra.tests import test_rate_matrix_parity_r7 as r7
from kamra.tests.rate_matrix_harness import Doc, RateMatrixHarness
from kamra.tests.test_rate_matrix_parity_r6 import QuotePatches, locked, stub_module


class FreeChildrenContractTest(QuotePatches, RateMatrixHarness):
	"""P1-2: quote, booking and amendment all deduct free children identically to the resolver."""

	modules = ("kamra.pricing", "kamra.kamra.doctype.reservation.reservation", "kamra.authz", "kamra.api")

	def extra_modules(self):
		return {"kamra.folio": stub_module("kamra.folio", _fnb_gst=lambda property: 5,
		                                   charged_nights=lambda name: {})}

	def setUp(self):
		super().setUp()
		self.roles = ["Front Desk"]
		self.authz.restricted_properties = lambda user=None: None
		self.add("Room Type", "Test Hotel-DLX", property="Test Hotel", base_price=100000, base_occupancy=2,
		         adults_capacity=4, children_capacity=4, free_child_age=6)
		self.plan("CON-RES")
		self.patch_quote()
		# Room $130 (2026-12-05..06) / $200 (2026-12-07+), plus TAR-CHILD = $25 per chargeable child, FX = 3,650.
		self.tariffs["CON-RES"] = self.resolver

	def resolver(self, form):
		nights = []
		children = int(form.get("children") or 0)
		for day in r7.stay_nights(form["check_in"], form["check_out"]):
			room = 200 if day >= "2026-12-07" else 130
			nightly = room + 25 * children
			agreed = nightly * 3650
			nights.append({"date": day, "nightly": float(nightly), "agreed_rate": float(agreed),
			               "item_price": "IP-NEW" if day >= "2026-12-07" else "IP-BASE"})
		return {
			"is_tariff": 1, "price_list": "Contract USD", "currency": "USD", "property_currency": "UGX",
			"nightly": nights[0]["nightly"], "agreed_rate": nights[0]["agreed_rate"],
			"fx_rate": 3650.0, "fx_date": form.get("fx_date") or "2026-11-20", "nights": nights,
		}

	def new_booking(self, **fields):
		values = dict(property="Test Hotel", room_type="Test Hotel-DLX", rate_plan="CON-RES",
		              check_in_date="2026-12-05", check_out_date="2026-12-07", adults=2, children=2,
		              infants=1, extra_beds=0, occupants=[], auto_price=1, agreed_rate=0, meal_plan=None,
		              company=None, group_booking=None, status="Confirmed")
		values.update(fields)
		doc = self.reservation.Reservation({**values, "flags": Doc()})
		doc.is_new = lambda: True
		return doc

	def test_booking_with_infant_matches_quote_and_resolver_without_rate_changed_error(self):
		# Reproduces Hydrogen's live probe: TAR-CHILD=$25, free_child_age=6, children=2, infants=1 -> 1 billable child ($155 = 565,750 UGX).
		quoted = self.pricing.quote_with_contract(
			"Test Hotel", "Test Hotel-DLX", "2026-12-05", "2026-12-07", adults=2, children=1, rate_plan="CON-RES")
		self.assertEqual((quoted["agreed_rate"], quoted["tariff"]["nightly"]), (565750.0, 155.0))

		# Before Validate (Kamra Tariff Resolve) already deducted the infant and set doc.agreed_rate = 565,750.
		doc = self.new_booking(agreed_rate=565750.0)
		doc.capture_contract_tariff()
		self.assertEqual(self.tariff_forms()[-1]["children"], 1)
		self.assertEqual(doc.agreed_rate, 565750.0)
		self.assertEqual(doc.contract_tariff["agreed_rate"], 565750.0)
		self.assertEqual(doc.contract_tariff["tariff"]["nightly"], 155.0)
		self.assertEqual(doc.quote_inputs()["children"], 1)
		self.assertEqual(doc.requote()["amount_after_tax"], quoted["amount_after_tax"])

	def test_booking_deducts_free_child_occupants_and_handles_zero_age_and_clamping(self):
		# Occupant age 4 <= free_child_age(6) is free; adult with unset age 0 and child age 9 are not.
		doc = self.new_booking(
			children=2, infants=0,
			occupants=[Doc(age=0), Doc(age=4), Doc(age=9)],
			agreed_rate=565750.0,
		)
		doc.capture_contract_tariff()
		self.assertEqual(self.tariff_forms()[-1]["children"], 1)
		self.assertEqual(doc.quote_inputs()["children"], 1)

		# free_child_age = 0: age 1 occupant is chargeable, while explicit infant count still deducts.
		self.docs["Room Type", "Test Hotel-DLX"].free_child_age = 0
		doc_zero = self.new_booking(children=2, infants=0, occupants=[Doc(age=1)], agreed_rate=657000.0)
		doc_zero.capture_contract_tariff()
		self.assertEqual(self.tariff_forms()[-1]["children"], 2)
		doc_inf = self.new_booking(children=1, infants=2, occupants=[], agreed_rate=474500.0)
		doc_inf.capture_contract_tariff()
		self.assertEqual(self.tariff_forms()[-1]["children"], 0)

	def test_amendment_and_preview_deduct_free_children_for_added_nights(self):
		booked_nights = [
			{"date": "2026-12-05", "nightly": 155.0, "agreed_rate": 565750.0, "item_price": "IP-BASE"},
			{"date": "2026-12-06", "nightly": 155.0, "agreed_rate": 565750.0, "item_price": "IP-BASE"},
		]
		snapshot = {"agreed_rate": 565750.0, "tariff": {
			"currency": "USD", "nightly": 155.0, "fx_rate": 3650.0, "fx_date": "2026-11-20",
			"property_currency": "UGX", "nights": booked_nights}}
		for label, party in (("infants", {"children": 2, "infants": 1, "occupants": []}),
		                     ("occupants", {"children": 2, "infants": 0, "occupants": [Doc(age=4), Doc(age=9)]})):
			with self.subTest(label):
				self.bridge_calls.clear()
				values = dict(name="RES-INF", property="Test Hotel", room_type="Test Hotel-DLX", rate_plan="CON-RES",
				              check_in_date="2026-12-05", check_out_date="2026-12-07", adults=2, extra_beds=0,
				              auto_price=1, agreed_rate=565750.0, meal_plan=None, status="Confirmed",
				              amount_after_tax=1131500.0, contract_tariff=json.dumps(snapshot),
				              night_rates=[Doc(stay_date="2026-12-05", rate=565750.0, gst_rate=0),
				                           Doc(stay_date="2026-12-06", rate=565750.0, gst_rate=0)],
				              **party)
				before = Doc(values)
				doc = self.reservation.Reservation({**values, "flags": Doc()})
				doc.is_new = lambda: False
				doc.get_doc_before_save = lambda: before
				self.docs["Reservation", "RES-INF"] = doc

				preview = self.api.amend_stay_preview("RES-INF", "2026-12-05", "2026-12-08")
				self.assertEqual(self.tariff_forms()[-1]["children"], 1)
				# 2026-12-07: $200 room + 1 chargeable child ($25) = $225 * 3,650 = 821,250 (not 912,500 for 2 children)
				self.assertEqual([(n["date"], n["rate"]) for n in preview["nights"]], [
					("2026-12-05", 565750.0), ("2026-12-06", 565750.0), ("2026-12-07", 821250.0)])

				# Save path agrees with preview
				self.bridge_calls.clear()
				doc.update(check_in_date="2026-12-05", check_out_date="2026-12-08")
				doc.capture_contract_tariff()
				self.assertEqual(self.tariff_forms()[-1]["children"], 1)
				self.assertEqual(doc.contract_tariff["tariff"]["nights"][-1]["agreed_rate"], 821250.0)
				self.assertEqual([(r["stay_date"], r["rate"]) for r in doc.quote_night_rows(doc.requote())], [
					("2026-12-05", 565750.0), ("2026-12-06", 565750.0), ("2026-12-07", 821250.0)])


class DisjointDateMoveHeadlineTest(QuotePatches, RateMatrixHarness):
	"""P1-3: moving to disjoint dates (or front-shortening past a renewal) updates headline agreed_rate and nightly."""

	modules = ("kamra.pricing", "kamra.kamra.doctype.reservation.reservation", "kamra.authz", "kamra.api")

	def extra_modules(self):
		return {
			"kamra.folio": stub_module("kamra.folio", _fnb_gst=lambda property: 5,
			                           charged_nights=lambda name: {}),
			"kamra.credit": stub_module("kamra.credit", _is_agent=lambda: False),
			"kamra.settings": stub_module("kamra.settings", get_setting=lambda prop, key: 20),
		}

	def setUp(self):
		super().setUp()
		self.roles = ["Front Desk"]
		self.authz.restricted_properties = lambda user=None: None
		self.authz.supervisors_for = lambda prop, exclude=None: []
		self.add("Room Type", "Test Hotel-DLX", property="Test Hotel", base_price=100000, base_occupancy=2,
		         adults_capacity=4, free_child_age=6)
		self.plan("CON-RES")
		self.plan("CON-CHK", fx_lock_policy="Fixed at Check In")
		self.patch_quote()
		self.tariffs["CON-RES"] = self.resolver
		self.tariffs["CON-CHK"] = self.resolver

	@staticmethod
	def tariff_for(day):
		if day >= "2026-12-01":
			return (100.0, 365000.0, "IP-DEC")
		if day >= "2026-11-27":
			return (200.0, 730000.0, "IP-PEAK")
		return (130.0, 474500.0, "IP-BASE")

	def resolver(self, form):
		nights = []
		for day in r7.stay_nights(form["check_in"], form["check_out"]):
			nightly, agreed, item_price = self.tariff_for(day)
			nights.append({"date": day, "nightly": nightly, "agreed_rate": agreed, "item_price": item_price})
		return {
			"is_tariff": 1, "price_list": "Contract USD", "currency": "USD", "property_currency": "UGX",
			"nightly": nights[0]["nightly"], "agreed_rate": nights[0]["agreed_rate"],
			"fx_rate": 3650.0, "fx_date": form.get("fx_date") or "2026-11-20", "nights": nights,
		}

	def stay(self, check_in="2026-11-24", check_out="2026-11-26", *, plan="CON-RES", one_rate=False, **fields):
		nights = []
		for day in r7.stay_nights(check_in, check_out):
			nightly, agreed, item_price = self.tariff_for(day)
			nights.append({"date": day, "nightly": nightly, "agreed_rate": agreed, "item_price": item_price})
		tariff = {"currency": "USD", "nightly": nights[0]["nightly"], "fx_rate": 3650.0,
		          "fx_date": "2026-11-20", "property_currency": "UGX"}
		if not one_rate:
			tariff["nights"] = nights
		snapshot = {"agreed_rate": nights[0]["agreed_rate"], "tariff": tariff}
		values = dict(
			name="RES-MOVE", property="Test Hotel", room_type="Test Hotel-DLX", rate_plan=plan,
			check_in_date=check_in, check_out_date=check_out, adults=2, children=0, infants=0, extra_beds=0,
			auto_price=1, agreed_rate=nights[0]["agreed_rate"], meal_plan=None, status="Confirmed",
			amount_after_tax=sum(n["agreed_rate"] for n in nights),
			contract_tariff=json.dumps(snapshot),
			night_rates=[Doc(stay_date=n["date"], rate=n["agreed_rate"], gst_rate=0) for n in nights],
		)
		values.update(fields)
		before = Doc(values)
		doc = self.reservation.Reservation({**values, "flags": Doc()})
		doc.is_new = lambda: False
		doc.get_doc_before_save = lambda: before
		self.docs["Reservation", "RES-MOVE"] = doc
		return doc

	def test_upward_disjoint_move_updates_headline_and_allows_downstream_discount(self):
		# Preview reprices accurately and restores doc.agreed_rate before returning so doc.save() enters
		# Kamra Tariff Resolve (before_validate) with old.agreed_rate intact (no false HSP-TARIFF-008).
		doc = self.stay()
		preview = self.api.amend_stay_preview("RES-MOVE", "2026-11-27", "2026-11-29")
		self.assertEqual([(n["date"], n["rate"]) for n in preview["nights"]], [
			("2026-11-27", 730000.0), ("2026-11-28", 730000.0)])
		self.assertEqual(preview["new_amount"], 1460000.0)
		self.assertEqual(doc.agreed_rate, 474500.0)

		# Save path: standalone Kamra AND paired path (where before_validate already updated doc.agreed_rate to 730,000)
		for mode, pre_agreed in (("standalone", 474500.0), ("after_before_validate_resolver", 730000.0)):
			with self.subTest(mode):
				doc = self.stay()
				doc.update(check_in_date="2026-11-27", check_out_date="2026-11-29", agreed_rate=pre_agreed)
				doc.capture_contract_tariff()
				self.assertEqual(doc.agreed_rate, 730000.0)
				snap = self.pricing.saved_contract_tariff(doc)
				self.assertEqual(snap["agreed_rate"], 730000.0)
				self.assertEqual(snap["tariff"]["nightly"], 200.0)
				self.assertEqual([(n["date"], n["nightly"], n["agreed_rate"]) for n in snap["tariff"]["nights"]], [
					("2026-11-27", 200.0, 730000.0), ("2026-11-28", 200.0, 730000.0)])
				rows = doc.quote_night_rows(doc.requote())
				self.assertEqual([(r["stay_date"], r["rate"]) for r in rows], [
					("2026-11-27", 730000.0), ("2026-11-28", 730000.0)])
				# Downstream 10% Front Desk discount (657,000) off the active 730,000 rate works cleanly
				doc.night_rates = [Doc(stay_date=r["stay_date"], rate=r["rate"], gst_rate=0) for r in rows]
				doc.amount_after_tax = 1460000.0
				plan = self.api._rate_override_plan(doc, 657000.0)
				self.assertFalse(plan["raises"])
				self.assertEqual(plan["reduction_pct"], 10.0)
				self.assertEqual([(n["date"], n["rate"]) for n in plan["nights"]], [
					("2026-11-27", 657000.0), ("2026-11-28", 657000.0)])

	def test_downward_disjoint_move_updates_headline_so_setting_old_rate_is_not_a_noop(self):
		# Booked at $200 (730,000) on 27-29 Nov, moved to $100 (365,000) on 01-03 Dec.
		doc = self.stay("2026-11-27", "2026-11-29")
		doc.update(check_in_date="2026-12-01", check_out_date="2026-12-03")
		doc.capture_contract_tariff()
		self.assertEqual(doc.agreed_rate, 365000.0)
		self.assertEqual(doc.contract_tariff["agreed_rate"], 365000.0)
		self.assertEqual(doc.contract_tariff["tariff"]["nightly"], 100.0)
		rows = doc.quote_night_rows(doc.requote())
		doc.night_rates = [Doc(stay_date=r["stay_date"], rate=r["rate"], gst_rate=0) for r in rows]
		doc.amount_after_tax = 730000.0
		# Setting 730,000 afterwards is a real override (raise), NOT a no-op keeping 365,000!
		self.roles = ["Revenue Manager"]
		plan = self.api._rate_override_plan(doc, 730000.0)
		self.assertTrue(plan["raises"])
		self.assertEqual([(n["date"], n["rate"]) for n in plan["nights"]], [
			("2026-12-01", 730000.0), ("2026-12-02", 730000.0)])

	def test_front_shortening_past_renewal_and_one_rate_disjoint_move_update_headline(self):
		# Front-shortening [26 Nov @ 130, 27 Nov @ 200] -> [27 Nov @ 200]
		doc = self.stay("2026-11-26", "2026-11-28")
		self.assertEqual(doc.agreed_rate, 474500.0)
		doc.update(check_in_date="2026-11-27", check_out_date="2026-11-28")
		doc.capture_contract_tariff()
		self.assertEqual(doc.agreed_rate, 730000.0)
		self.assertEqual(doc.contract_tariff["agreed_rate"], 730000.0)
		self.assertEqual(doc.contract_tariff["tariff"]["nightly"], 200.0)

		# One-rate contract moved to disjoint dates at $200
		one = self.stay("2026-11-24", "2026-11-26", one_rate=True)
		one.update(check_in_date="2026-11-27", check_out_date="2026-11-29")
		one.capture_contract_tariff()
		self.assertEqual(one.agreed_rate, 730000.0)
		self.assertEqual(one.contract_tariff["agreed_rate"], 730000.0)
		self.assertEqual(one.contract_tariff["tariff"]["nightly"], 200.0)

	def test_fixed_rate_and_managers_agreed_rate_keep_their_values_on_disjoint_move(self):
		for label, fields, expected_agreed, expected_nightly in (
			("fixed_rate", {"fixed_rate": 1}, 474500.0, 130.0),
			("managers_rate", {"agreed_rate": 450000.0}, 450000.0, 130.0),
		):
			with self.subTest(label):
				doc = self.stay(**fields)
				doc.update(check_in_date="2026-11-27", check_out_date="2026-11-29")
				doc.capture_contract_tariff()
				self.assertEqual(doc.agreed_rate, expected_agreed)
				snap = self.pricing.saved_contract_tariff(doc)
				self.assertEqual((snap["agreed_rate"], snap["tariff"]["nightly"]), (474500.0, expected_nightly))

	def test_check_in_fx_relock_after_disjoint_move_converts_new_headline_nightly(self):
		doc = self.stay(plan="CON-CHK")
		doc.update(check_in_date="2026-11-27", check_out_date="2026-11-29")
		doc.capture_contract_tariff()
		self.assertEqual((doc.agreed_rate, doc.contract_tariff["tariff"]["nightly"]), (730000.0, 200.0))
		self.docs["Property", "Test Hotel"].business_date = "2026-11-27"
		self.rate("USD", 3700, "2026-11-27")
		self.assertTrue(self.pricing.relock_contract_fx_at_check_in(doc))
		self.assertEqual(doc.agreed_rate, 740000.0)
		self.assertEqual(doc.contract_tariff["agreed_rate"], 740000.0)
		self.assertEqual([n["agreed_rate"] for n in doc.contract_tariff["tariff"]["nights"]], [740000.0, 740000.0])

