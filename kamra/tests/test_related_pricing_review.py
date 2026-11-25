"""Live regressions for allowance FX allocation and the guest quotation."""

from decimal import Decimal
from unittest.mock import patch

import frappe

from kamra import api, folio as folio_model, pricing, settings
from kamra.tests.audit_fixtures import TODAY, AuditTestCase, d, guest_folio, make_property, make_reservation, make_room, make_room_type

PROPERTY = "UT Related Pricing Review"


class TestRelatedPricingReview(AuditTestCase):
	properties = (PROPERTY,)

	def setUp(self):
		super().setUp()
		self.at(TODAY)
		frappe.local.lang = "en"
		self.addCleanup(settings.clear_cache, PROPERTY)

	def stay(self, home, foreign, fx, home_rate, *, checked_in=False, inclusive=False):
		make_property(PROPERTY, country="United States", currency=home, business_date=TODAY,
		              gst_mode="Fixed", gst_rate_low=0, gst_rate_high=0, rates_include_tax=int(inclusive))
		room_type = make_room_type(PROPERTY, base_price=home_rate)
		res = make_reservation(PROPERTY, room_type, d(0), d(2),
		                       status="Checked In" if checked_in else "Confirmed",
		                       room=make_room(PROPERTY, room_type, "101"))
		res.db_set("contract_tariff", frappe.as_json({
			"tariff": {"currency": foreign, "property_currency": home, "fx_rate": fx, "fx_date": TODAY},
			"fx_lock_policy": "Fixed at Reservation",
		}))
		return res

	def approval(self, mode, threshold=0):
		settings.write_kv(PROPERTY, "allowance_approval", mode)
		settings.write_kv(PROPERTY, "allowance_approval_above", threshold)
		settings.clear_cache(PROPERTY)

	def test_foreign_discount_can_reach_approvals_inbox_with_immutable_fx(self):
		res = self.stay("UGX", "USD", 4085.83, 408583, checked_in=True)
		folio_model.post_room_night(res, d(0))
		folio = guest_folio(res.name)
		self.approval("Hotel Admin", 50000)
		with patch.object(pricing, "posting_fx_rate", return_value=(Decimal("4085.83"), TODAY)):
			out = api.post_discount(folio, 20, "Amount", reason="Service recovery", discount_currency="USD")

		self.assertTrue(out["pending"])
		request = frappe.get_doc("Allowance Request", out["results"][0]["request"])
		self.assertEqual((request.status, request.amount, request.contract_currency), ("Pending", 81717, "USD"))
		self.assertAlmostEqual(request.contract_fx_rate, 4085.83)
		self.assertEqual(str(request.contract_fx_date), TODAY)
		self.assertFalse(any(row.charge_type == "Allowance" for row in frappe.get_doc("Folio", folio).charges))
		forged = frappe.copy_doc(request)
		with self.assertRaisesRegex(frappe.ValidationError, "exchange rate is set"):
			forged.insert(ignore_permissions=True)
		request.flags.kamra_decision = True
		request.contract_fx_rate += 1
		with self.assertRaisesRegex(frappe.ValidationError, "can't change"):
			request.save(ignore_permissions=True)

	def test_workflow_backed_immediate_foreign_discount_persists_fx(self):
		for state in ("Pending", "Approved"):
			if not frappe.db.exists("Workflow State", state):
				frappe.get_doc({"doctype": "Workflow State", "workflow_state_name": state}).insert()
		if not frappe.db.exists("Workflow Action Master", "Approve"):
			frappe.get_doc({"doctype": "Workflow Action Master", "workflow_action_name": "Approve"}).insert()
		frappe.get_doc({
			"doctype": "Workflow", "workflow_name": "UT Related Pricing Allowance",
			"document_type": "Allowance Request", "is_active": 1, "workflow_state_field": "status",
			"states": [{"state": state, "doc_status": 0, "allow_edit": "System Manager"}
			           for state in ("Pending", "Approved")],
			"transitions": [{"state": "Pending", "action": "Approve", "next_state": "Approved",
			                 "allowed": "System Manager"}],
		}).insert(ignore_permissions=True)
		res = self.stay("UGX", "USD", 4085.83, 408583, checked_in=True)
		folio_model.post_room_night(res, d(0))
		folio = guest_folio(res.name)
		self.approval("Off")
		with patch.object(pricing, "posting_fx_rate", return_value=(Decimal("4085.83"), TODAY)):
			out = api.post_discount(folio, 20, "Amount", reason="Service recovery", discount_currency="USD")

		self.assertFalse(out["pending"])
		serial = out["results"][0]["serial"]
		request = frappe.get_doc("Allowance Request", {"serial": serial})
		line = frappe.get_doc("Folio", folio).charges[-1]
		self.assertEqual((request.status, request.amount, request.contract_currency), ("Approved", 81717, "USD"))
		self.assertEqual((line.amount, line.contract_currency, str(line.contract_fx_date)), (-81717, "USD", TODAY))

	def test_foreign_split_preserves_converted_total_and_approval_threshold(self):
		res = self.stay("USD", "KWD", 3.25, 100, checked_in=True)
		folio = frappe.get_doc("Folio", guest_folio(res.name))
		for gst_rate in (0, 10):
			folio.append("charges", {"charge_type": "Room", "description": "Room night", "qty": 1,
			                         "rate": 100, "amount": 100, "gst_rate": gst_rate, "posting_date": TODAY})
		folio_model._recalculate(folio)
		folio.save(ignore_permissions=True)
		self.approval("Hotel Admin", 100.01)
		with patch.object(pricing, "posting_fx_rate", return_value=(Decimal("3.25"), TODAY)):
			preview = api.post_discount(folio.name, 30.772, "Amount", preview=1, discount_currency="KWD")
			self.assertEqual(preview["conversion"]["home_amount"], 100.01)
			self.assertEqual(preview["total"], 100.01)
			self.assertEqual([row["amount"] for row in preview["lines"]], [50.01, 50.00])
			out = api.post_discount(folio.name, 30.772, "Amount", reason="Service recovery",
			                        discount_currency="KWD")

		self.assertFalse(out["pending"])
		self.assertEqual(out["total"], 100.01)
		posted = frappe.get_doc("Folio", folio.name).charges
		self.assertEqual([row.amount for row in posted if row.charge_type == "Allowance"], [-50.01, -50.00])

	def quoted_stay(self, *, inclusive=False, res=None):
		if res is None:
			res = self.stay("USD", "KWD", 3.25, 4.01, inclusive=inclusive)
		kwd, usd, gst = (1.375, 4.06, 10) if inclusive else (1.235, 4.01, 0)
		res.set("night_rates", [
			{"stay_date": d(0), "rate": usd, "gst_rate": gst},
			{"stay_date": d(1), "rate": 4.01, "gst_rate": 0},
		])
		res.quote_snapshot = frappe.as_json({
			"nightly": [{"date": d(0), "rate": usd, "gst_rate": gst,
			             "contract_currency": "KWD", "contract_rate": kwd}],
			"contract_summary": {"room_tax_included": inclusive},
		})
		return res

	def test_quotation_uses_canonical_rate_on_same_tax_basis(self):
		res = self.quoted_stay()
		ref = api._booking_quotation(res)["currency_reference"]
		self.assertEqual(ref["nights"][0]["contract_rate"], 1.235)
		self.assertFalse(ref["nights"][0]["estimated"])
		inclusive = api._booking_quotation(self.quoted_stay(inclusive=True, res=res))["currency_reference"]
		self.assertEqual(inclusive["nights"][0]["contract_rate"], 1.250)

	def test_quotation_labels_legacy_inverse_conversion_as_estimated(self):
		quote = api._booking_quotation(self.quoted_stay())
		legacy = quote["currency_reference"]["nights"][1]
		self.assertEqual(legacy["contract_rate"], 1.234)
		self.assertTrue(legacy["estimated"])
		html = api._booking_quotation_html(quote)
		self.assertIn("KWD 1.235", html)
		self.assertIn("≈ KWD 1.234", html)
		self.assertIn("Amounts marked ≈ are estimates.", html)

	def test_quotation_pdf_html_uses_arabic_direction_labels_and_numbers(self):
		res = self.quoted_stay()
		frappe.local.lang = "ar"
		html = api._booking_quotation_html(api._booking_quotation(res))
		self.assertIn('<html lang="ar" dir="rtl">', html)
		self.assertIn("عملة المحاسبة للمنشأة", html)
		self.assertIn("عملة السعر", html)
		self.assertIn("الإجمالي بعملة السعر", html)
		self.assertRegex(html, r"[1١]٫[2٢][3٣][5٥]")
