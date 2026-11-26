"""A desk discount on a planned stay locks the accepted price and slab on each date."""
from decimal import Decimal, ROUND_HALF_UP

import frappe

from kamra import api, business_date, folio, pricing
from kamra.ledger import ensure_default_reason_codes
from kamra.tests.audit_fixtures import (
	PROP_A, TODAY, AuditTestCase, d, guest_folio, make_property, make_room, make_room_type,
)
from kamra.tests.test_reservation_discounts import set_setting


class StackedDiscountCases:
	def setup_stay(self, inclusive):
		self.at(TODAY, "kamra.api", "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY, currency=self.currency, rates_include_tax=inclusive,
		              gst_mode="Slab", gst_slab_threshold=190, gst_rate_low=5, gst_rate_high=18)
		ensure_default_reason_codes()
		set_setting(PROP_A, "rate_override_limit_pct", 30)
		# One billable adult and child bring the room card to 200. The agreed
		# extra bed is a separate 5 that counts toward the nightly slab.
		self.rt = make_room_type(PROP_A, "R16", base_price=190, base_occupancy=1, extra_adult_price=6,
		                         child_price=4, children_capacity=1, max_extra_beds=1)
		self.room = make_room(PROP_A, self.rt, "1601")
		frappe.get_doc({
			"doctype": "Season", "property": PROP_A, "season_name": "R16 second night",
			"room_type": self.rt, "start_date": d(1), "end_date": d(1),
			"adjustment_type": "Amount", "adjustment_value": 40,
		}).insert()
		plan = frappe.get_doc({
			"doctype": "Rate Plan", "property": PROP_A, "rate_plan_name": "R16 plan", "code": "R16",
			"currency": self.currency, "modifier_type": "Percent", "modifier_value": -10,
			"discount_eligible": 1,
		}).insert()
		self.terms = dict(adults=2, children=1, over_capacity_charge=5, rate_plan=plan.name)

	def book(self, kind, value):
		result = api.create_booking(
			PROP_A, self.rt, TODAY, d(2), "R16 Guest", room=self.room, **self.terms,
			over_capacity_guests=1, over_capacity_reason="Extra bed", discount_type=kind,
			discount_value=value, discount_reason="RATE-PROMO",
		)
		return frappe.get_doc("Reservation", result["reservation"])

	def check_discount(self, inclusive, kind, value):
		self.setup_stay(inclusive)
		quote = pricing.quote(PROP_A, self.rt, TODAY, d(2), **self.terms,
		                      discount_type=kind, discount_value=value)
		stay = self.book(kind, value)
		folio.open_folio(stay)
		for day in (TODAY, d(1)):
			folio.post_room_night(stay, day)
		bill = frappe.get_doc("Folio", guest_folio(stay.name))
		expected_rows = []
		total = Decimal(0)
		for i, planned in enumerate((Decimal(180), Decimal(216))):
			entry = planned * Decimal("0.9") if kind == "Percent" else planned - value
			entry = entry.quantize(self.unit, ROUND_HALF_UP)
			slab = 5 if entry + 5 <= 190 else 18
			factor = 1 + Decimal(slab) / 100
			net = (entry / factor).quantize(min(self.unit, Decimal("0.01")), ROUND_HALF_UP) if inclusive else entry
			expected_rows.append((d(i), net, slab))
			total += (entry if inclusive else entry * factor) + 5 * factor
		expected = total.quantize(self.unit, ROUND_HALF_UP)
		self.assertEqual(tuple(Decimal(str(v)) for v in (
			quote["amount_after_tax"], stay.amount_after_tax, bill.grand_total,
		)), (expected,) * 3)
		locks = [(str(n.stay_date), Decimal(str(n.rate)), n.gst_rate) for n in stay.night_rates]
		lines = sorted((str(c.posting_date), Decimal(str(c.amount)), c.gst_rate)
		               for c in bill.charges if c.charge_type == "Room")
		self.assertEqual(locks, expected_rows)
		self.assertEqual(lines, expected_rows)
		self.assertEqual([(n["date"], Decimal(str(n["rate"])), n["gst_rate"])
		                  for n in quote["nightly"]], expected_rows)
		self.assertEqual(sorted((str(c.posting_date), c.amount, c.gst_rate)
		                        for c in bill.charges if c.charge_type == "Extra Guests"),
		                 [(day, 5, slab) for day, _, slab in expected_rows])

	def test_exclusive_percent(self):
		self.check_discount(False, "Percent", 10)

	def test_inclusive_percent(self):
		self.check_discount(True, "Percent", 10)

	def test_exclusive_amount_crosses_second_slab(self):
		self.check_discount(False, "Amount", 32)

	def test_inclusive_amount_crosses_second_slab(self):
		self.check_discount(True, "Amount", 32)


class TestUSD(StackedDiscountCases, AuditTestCase):
	currency, unit = "USD", Decimal("0.01")

	def test_older_quote_with_pre_discount_planned_rates(self):
		self.setup_stay(False)
		quote = pricing.quote(PROP_A, self.rt, TODAY, d(2), **self.terms, discount_type="Percent", discount_value=10)
		for n, old_plan in zip(quote["nightly"], (180, 216), strict=True):
			n["planned_rate"] = old_plan
		stay = self.book("Percent", 10)
		self.assertEqual([Decimal(str(r["rate"])) for r in stay.quote_night_rows(quote)],
		                 [Decimal("162.00"), Decimal("194.40")])

	def test_package_rejects_stacking_at_quote_and_booking(self):
		self.setup_stay(True)
		frappe.get_doc({
			"doctype": "Holiday Package", "property": PROP_A, "package_name": "R16 sold package",
			"room_type": self.rt, "start_date": TODAY, "end_date": d(2), "package_nights": 2,
			"price_2_pax": 400,
		}).insert()
		for kind, value in (("Percent", 10), ("Amount", 32)):
			with self.subTest(kind=kind):
				with self.assertRaisesRegex(frappe.ValidationError, "holiday package"):
					pricing.quote(PROP_A, self.rt, TODAY, d(2), **self.terms, discount_type=kind, discount_value=value)
				with self.assertRaisesRegex(frappe.ValidationError, "holiday package"):
					self.book(kind, value)
		self.assertFalse(frappe.db.exists("Reservation", {"room": self.room}))

	def test_daily_source_discount_survives_move_to_package(self):
		self.setup_stay(False)
		destination_type = make_room_type(PROP_A, "R16P", base_price=300, children_capacity=1, max_extra_beds=1)
		destination = make_room(PROP_A, destination_type, "1603")
		pkg = frappe.get_doc({
			"doctype": "Holiday Package", "property": PROP_A, "package_name": "R16 destination",
			"room_type": destination_type, "start_date": d(1), "end_date": d(2), "package_nights": 1,
			"price_2_pax": 300,
		}).insert()
		result = api.create_booking(
			PROP_A, self.rt, TODAY, d(2), "R16 Daily", room=self.room, **self.terms,
			over_capacity_guests=1, over_capacity_reason="Extra bed",
			night_discounts=[{"date": TODAY, "discount_type": "Percent",
			                  "discount_value": 10, "discount_reason": "RATE-PROMO"}],
		)
		stay = frappe.get_doc("Reservation", result["reservation"])
		stay.status = "Checked In"
		stay.save()
		business_date.advance_business_date(PROP_A, TODAY)
		preview = api.room_move_preview(stay.name, destination, upgrade_type="Paid")
		api.move_reservation(stay.name, destination, upgrade_type="Paid", confirm_price=1)
		stay.reload()
		sold = frappe.parse_json(stay.quote_snapshot)["nightly"]
		self.assertEqual([row.get("package_id") for row in sold], [None, pkg.name])
		self.assertEqual([(row.rate, row.discount_amount) for row in stay.night_rates],
		                 [(162, 18), (270, 0)])
		for day in (TODAY, d(1)):
			folio.post_room_night(stay, day)
		bill = frappe.get_doc("Folio", guest_folio(stay.name))
		self.assertEqual((preview["new_amount"], stay.amount_after_tax, bill.grand_total), (499.85,) * 3)

	def test_extension_keeps_unposted_source_discount_once(self):
		self.setup_stay(False)
		stay = self.book("Percent", 10)
		stay.status = "Checked In"
		stay.save()
		destination_type = make_room_type(PROP_A, "R16B", base_price=300, children_capacity=1, max_extra_beds=1)
		destination = make_room(PROP_A, destination_type, "1602")
		business_date.advance_business_date(PROP_A, TODAY)
		api.move_reservation(stay.name, destination, upgrade_type="Paid", confirm_price=1)
		self.assertFalse(folio.charged_nights(stay.name))
		preview = api.amend_stay_preview(stay.name, TODAY, d(3))
		api.amend_stay(stay.name, TODAY, d(3), confirm_price=1)
		stay.reload()
		source = folio.locked_night(stay, TODAY)
		self.assertEqual((source.rate, source.base_rate, source.discount_amount, source.gst_rate), (162, 180, 18, 5))
		for day in (TODAY, d(1), d(2)):
			folio.post_room_night(stay, day)
		bill = frappe.get_doc("Folio", guest_folio(stay.name))
		self.assertEqual(Decimal(str(preview["new_amount"])), Decimal(str(bill.grand_total)))
		self.assertEqual(stay.amount_after_tax, bill.grand_total)
		lines = sorted((str(c.posting_date), c.amount, c.gst_rate) for c in bill.charges if c.charge_type == "Room")
		self.assertEqual(lines, [(str(n.stay_date), n.rate, n.gst_rate) for n in stay.night_rates])


class TestUGX(StackedDiscountCases, AuditTestCase):
	currency, unit = "UGX", Decimal("1")


class TestKWD(StackedDiscountCases, AuditTestCase):
	currency, unit = "KWD", Decimal("0.001")
