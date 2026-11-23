"""Exercise contract pricing through the real quote, rate list and save paths."""

from unittest.mock import patch

import frappe
from frappe.utils import add_days, nowdate

from kamra import api, rates
from kamra.folio import open_folio
from kamra.tests.audit_fixtures import AuditTestCase


def freeze_fixture_rate(doc, method=None):
	"""The external Before Validate writer that owns the booking's agreed rate."""
	if doc.rate_plan and doc.is_new():
		doc.agreed_rate = 474500


class ContractRateQuoteTest(AuditTestCase):
	properties = ("TEST K3",)

	def setUp(self):
		super().setUp()
		frappe.local.lang = "en"
		self.property = frappe.get_doc({"doctype": "Property", "property_name": "TEST K3",
			"country": "Uganda", "currency": "UGX", "rates_include_tax": 1,
			"gst_rate_low": 0}).insert().name
		self.room_type = frappe.get_doc({"doctype": "Room Type", "property": self.property,
			"room_type_code": "STD", "room_type_name": "Standard", "base_price": 410619,
			"adults_capacity": 2, "tax_percent": 0}).insert().name
		frappe.get_doc({"doctype": "Room", "property": self.property,
			"room_type": self.room_type, "room_number": "101"}).insert()
		self.plan = frappe.get_doc({"doctype": "Rate Plan", "property": self.property,
			"rate_plan_name": "Contract", "code": "CON", "is_default": 1,
			"modifier_type": "Percent", "modifier_value": 0}).insert().name
		self.args = dict(property=self.property, room_type=self.room_type,
			check_in_date=str(add_days(nowdate(), 1)), check_out_date=str(add_days(nowdate(), 3)))
		self.tariff = dict(currency="USD", nightly=130, fx_rate=3650,
			fx_date="2026-11-21", property_currency="UGX")
		self.tax = dict(label="VAT 18%", rate=18, inclusive=1)
		self.script = self.add_script("hospitality_tariffs.tariff_rate", {"tariff": {
			**self.tariff, "is_tariff": 1, "agreed_rate": 474500,
		}}, checks={"property": self.property, "room_type": self.room_type,
			"rate_plan": self.plan, "check_in": self.args["check_in_date"],
			"check_out": self.args["check_out_date"], "adults": 2, "children": 0, "extra_beds": 0})
		self.add_script("hospitality_kamra.tax_basis", self.tax, checks={"property": self.property})

	def add_script(self, endpoint, result, checks=None):
		form_checks = "".join(
			f"if frappe.form_dict.get({key!r}) != {value!r}:\n"
			f"    frappe.throw('Wrong resolver input: {key}')\n" for key, value in (checks or {}).items())
		return frappe.get_doc({"doctype": "Server Script", "name": endpoint,
			"script_type": "API", "api_method": endpoint,
			"script": f"{form_checks}frappe.response['message'] = {result!r}"}).insert()

	def test_quote_converts_contract_and_keeps_invoice_tax_out_of_posting_amounts(self):
		quote = api.get_quote(**self.args, rate_plan=self.plan)
		self.assertEqual(quote["amount_after_tax"], 949000)
		self.assertEqual(quote["tariff"], self.tariff)
		self.assertEqual(quote["tax_basis"], self.tax)
		self.assertEqual(quote["tax_amount"], 0)
		self.assertEqual(api.property_locale(self.property).get("tax_basis"), self.tax)

	def test_options_explain_the_converted_nights_and_default_plan(self):
		options = rates.rate_options(**self.args)
		plan, = options["plans"]
		self.assertEqual(plan["room_total"], 949000)
		options = rates.rate_options(**self.args, rate_plan=self.plan)
		self.assertEqual([night["rate"] for night in options["nights"]], [474500, 474500])
		self.assertEqual(plan["tariff"], self.tariff)
		self.assertTrue(plan["is_default"])
		self.assertEqual(options["default_plan"], self.plan)

	def test_resolver_refusal_disables_option_and_refuses_quote(self):
		message = "HSP-TARIFF-004: No approved tariff for this date."
		self.script.script = f"frappe.response['message'] = {{'tariff': {{'refusal': {message!r}}}}}"
		self.script.save()
		plan, = rates.rate_options(**self.args)["plans"]
		self.assertEqual((plan.get("available"), plan.get("refusal")), (False, message))
		with self.assertRaisesRegex(frappe.ValidationError, "HSP-TARIFF-004"):
			api.get_quote(**self.args, rate_plan=self.plan)

	def test_disabled_resolver_preserves_card_pricing(self):
		self.script.db_set("disabled", 1)
		quote = api.get_quote(**self.args, rate_plan=self.plan)
		self.assertEqual(quote["room_total"], 821238)
		self.assertIsNone(quote.get("tariff"))
		plan, = rates.rate_options(**self.args)["plans"]
		self.assertEqual(plan["room_total"], 821238)

	def test_saved_agreed_rate_matches_quote_and_folio_keeps_frozen_tariff(self):
		quote = api.get_quote(**self.args, rate_plan=self.plan)
		hooks = frappe.get_doc_hooks().get("Reservation", {})
		events = {**hooks, "before_validate": [*hooks.get("before_validate", []),
			f"{__name__}.freeze_fixture_rate"]}
		with patch.dict(frappe.get_doc_hooks(), Reservation=events):
			result = api.create_booking(**self.args, rate_plan=self.plan, guest_name="Contract Guest")
		res = frappe.get_doc("Reservation", result["reservation"])
		self.assertEqual(float(res.agreed_rate), quote["nightly"][0]["rate"])
		self.assertEqual(float(res.amount_after_tax), quote["amount_after_tax"])
		self.script.db_set("disabled", 1)
		self.assertEqual(api.reservation_detail(res.name)["tariff"], self.tariff)
		stay = api.folio_invoice(open_folio(res))["stay"]
		self.assertEqual(stay["tariff"], self.tariff)
		# a bill shows the contract currency at the rate the stay was priced at (Property ACTUAL)
		self.assertEqual(stay["reference_currency"], {"currency": "USD", "home_currency": "UGX",
			"calculation": "ACTUAL", "fx_rate": 3650.0, "fx_date": "2026-11-21"})

	def test_room_tariffs_are_a_property_scoped_read(self):
		payload = {"tariffs": [{"rate_plan": self.plan, "currency": "USD", "room": 130,
			"single": 100, "extra": 20, "approval_state": "Approved", "valid_from": "2026-11-01",
			"fx_rate": 3650, "fx_date": "2026-11-21"}], "all_active_plans_tariff": True}
		self.add_script("hospitality_tariffs.room_type_tariffs", payload, checks={"room_type": self.room_type})
		self.assertTrue(hasattr(rates, "room_type_tariffs"), "no room tariff read is exposed")
		self.assertEqual(rates.room_type_tariffs(self.room_type), payload)
