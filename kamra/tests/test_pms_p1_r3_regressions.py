"""Room move and overstay billing regressions from the second review."""

from datetime import datetime
from unittest.mock import patch

import frappe

from kamra import api, business_date, day_use, folio, pricing
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	guest_folio,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)


class TestPmsP1R3Regressions(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.api", "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY, rates_include_tax=1, gst_mode="Slab",
		              gst_rate_low=5, gst_rate_high=18, gst_slab_threshold=7500)
		self.standard = make_room_type(PROP_A, "STD", base_price=10000)
		self.suite = make_room_type(PROP_A, "STE", base_price=18000)
		self.source = make_room(PROP_A, self.standard, "101")
		self.destination = make_room(PROP_A, self.suite, "201")

	def test_full_day_overstay_after_complimentary_move_posts_its_own_gst_slab(self):
		hourly = frappe.get_doc(
			doctype="Rate Plan", property=PROP_A, rate_plan_name="R3 Hourly", code="R3H",
			modifier_type="Percent", modifier_value=0, billing_basis="Hourly",
			hourly_rate=1000, min_hours=1,
		).insert(ignore_permissions=True)
		res = make_reservation(
			PROP_A, self.standard, TODAY, TODAY, status="Checked In", room=self.source,
			rate_plan=hourly.name, auto_price=1, is_day_use=1, planned_check_in_time="09:00:00",
			planned_check_out_time="12:00:00",
		)
		api.move_room(res.name, self.destination, upgrade_type="Complimentary", reason="Recovery")
		res.reload()
		self.assertEqual(res.room_type_to_charge, self.standard)
		self.assertEqual(folio.locked_night(res, TODAY).gst_rate, 5)
		at_checkout = datetime.fromisoformat(f"{TODAY} 13:30:00")
		with patch.object(day_use, "overstay_rule", return_value=day_use.OVERSTAY_FULL_DAY), \
				patch.object(day_use, "now_datetime", return_value=at_checkout):
			self.assertTrue(folio.post_room_night(res, TODAY))
		line, = [row for row in frappe.get_doc("Folio", guest_folio(res.name)).charges
		         if row.charge_type == "Room"]
		with self.subTest("slab"):
			self.assertEqual(line.gst_rate, 18)
		with self.subTest("gross"):
			self.assertEqual(line.total, 10000)

	def test_explicit_paid_move_resolves_only_remaining_tariff_nights(self):
		self.check_mid_stay_move(upgrade_type="Paid")

	def test_legacy_paid_move_resolves_only_remaining_tariff_nights(self):
		self.check_mid_stay_move()

	def check_mid_stay_move(self, **options):
		source_rates = [4000, 4200, 4500]
		destination_rates = [6000, 7000]
		requests = []

		def resolve(_property, room_type, _rate_plan, start, end, *_party, **_kwargs):
			start, end = str(start), str(end)
			if room_type == self.standard:
				self.assertEqual((start, end), (TODAY, d(3)))
				rates = source_rates
				offset = 0
			else:
				requests.append((room_type, start, end))
				if start != d(1) or end != d(3):
					raise pricing.ContractRateRefused("The destination tariff starts on the move date")
				rates = destination_rates
				offset = 1
			return rates[0], {
				"currency": "INR", "property_currency": "INR", "nightly": rates[0],
				"fx_rate": 1, "fx_date": start,
				"nights": [{"date": d(offset + i), "nightly": rate, "agreed_rate": rate,
				            "item_price": f"{room_type}-{i}"}
				           for i, rate in enumerate(rates)],
			}

		with patch.object(pricing, "contract_rate", side_effect=resolve):
			res = make_reservation(PROP_A, self.standard, TODAY, d(3), status="Checked In",
			                       room=self.source, auto_price=1)
			original_snapshot = pricing.saved_contract_tariff(res)["tariff"]["nights"][0]
			self.assertTrue(folio.post_room_night(res, TODAY))
			original_line, = frappe.get_doc("Folio", guest_folio(res.name)).charges
			business_date.advance_business_date(PROP_A, TODAY)
			preview = api.room_move_preview(res.name, self.destination, **options)
			self.assertEqual(preview["new_amount"], 17000)
			api.move_reservation(res.name, self.destination, confirm_price=1, **options)
		res.reload()
		self.assertEqual(requests, [(self.suite, d(1), d(3))] * 2)
		self.assertEqual(res.amount_after_tax, preview["new_amount"])
		self.assertEqual(res.upgrade_type, "Paid")
		nights = pricing.saved_contract_tariff(res)["tariff"]["nights"]
		self.assertEqual([n["agreed_rate"] for n in nights], [4000, 6000, 7000])
		self.assertEqual(nights[0]["item_price"], original_snapshot["item_price"])
		self.assertEqual(nights[0]["nightly"], original_snapshot["nightly"])
		after_line, = frappe.get_doc("Folio", guest_folio(res.name)).charges
		self.assertEqual((after_line.name, after_line.amount, after_line.total),
		                 (original_line.name, original_line.amount, original_line.total))
