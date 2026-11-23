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
