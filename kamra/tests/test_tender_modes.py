"""Property-scoped tender vocabulary and validation without a database."""

import unittest
from contextlib import ExitStack, contextmanager
from inspect import unwrap
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from kamra import api, erp_bridge, hooks, localization

MAPPED = [
	{"mode": "Cash", "currencies": ["UGX", "USD"]},
	{"mode": "Card", "currencies": ["UGX"]},
	{"mode": "Mobile Money", "currencies": ["UGX"]},
]


class TenderModesTest(unittest.TestCase):
	def setUp(self):
		prop = frappe._dict(name="P", currency="UGX", gst_rate_low=0)
		mocks = (
			patch("frappe.get_hooks", return_value=hooks.kamra_localization),
			patch("frappe.get_cached_doc", return_value=prop),
			patch(
				"frappe.get_cached_value",
				side_effect=lambda dt, name, field: {"country": "Uganda", "currency": "UGX"}[field],
			),
			patch("frappe.db", new=Mock()),
			patch("frappe.conf", new={}),
			patch("frappe.throw", side_effect=frappe.ValidationError),
			patch("frappe._", side_effect=lambda s: s),
			patch("kamra.settings.get_setting", return_value=0),
			patch("kamra.settings.list_setting", return_value=["USD"]),
		)
		stack = ExitStack()
		self.addCleanup(stack.close)
		for mock in mocks:
			stack.enter_context(mock)
		frappe.db.get_value.return_value = None

	@contextmanager
	def bridge(self, response):
		script = Mock()

		def execute():
			self.assertEqual(frappe.local.form_dict, {"property": "P"})
			frappe.response["message"] = response

		script.execute_method.side_effect = execute
		previous_form = frappe._dict(original=True)
		with (
			patch("frappe.local", SimpleNamespace(form_dict=previous_form)),
			patch("frappe.response", frappe._dict(message="outer")),
			patch.object(erp_bridge, "get_bridge_script", return_value=script) as lookup,
		):
			yield
			lookup.assert_called_with("hospitality_kamra.tender_modes")
			self.assertIs(frappe.local.form_dict, previous_form)
			self.assertEqual(frappe.response.message, "outer")

	def test_uganda_pack_works_without_bridge(self):
		with patch.object(erp_bridge, "get_bridge_script", return_value=None):
			pack = localization.pack_for("P")
			self.assertEqual(pack.DEFAULT_CURRENCY, "UGX")
			self.assertEqual(pack.FX_CURRENCIES, ["USD", "EUR", "GBP", "KES"])
			self.assertEqual(
				localization.payment_modes(pack), ["Cash", "Card", "Bank Transfer", "Mobile Money"]
			)
			self.assertEqual(localization.instant_payment_modes(pack), ["Mobile Money"])
			self.assertEqual(pack.locale(frappe._dict(name="P"))["currency"], "UGX")
			self.assertEqual(localization.money_text("P", 4148.25), "UGX 4,148")
			self.assertEqual(pack.fnb_tax_rate("P"), 0)
			localization.assert_payment_mode("P", "Mobile Money", "UGX")

	def test_locale_exposes_only_the_mapped_pairs(self):
		with self.bridge(MAPPED):
			locale = unwrap(api.property_locale)("P")
		self.assertEqual(locale["tender_modes"], MAPPED)
		self.assertEqual(locale["fx_currencies"], ["USD"])

	def test_mapping_is_authoritative_even_for_pack_and_core_modes(self):
		with self.bridge({"tender_modes": MAPPED}):
			for mode, currency in (("Mobile Money", "UGX"), ("Cash", "USD"), ("Foreign Cash", "USD")):
				localization.assert_payment_mode("P", mode, currency)
			for mode, currency in (
				("Card", "USD"),
				("Mobile Money", "USD"),
				("Bank Transfer", "UGX"),
				("Cash", "EUR"),
				("Foreign Cash", "UGX"),
			):
				with self.subTest(mode=mode, currency=currency), self.assertRaises(frappe.ValidationError):
					localization.assert_payment_mode("P", mode, currency)
		with self.bridge([MAPPED[0]]):
			with self.assertRaises(frappe.ValidationError):
				localization.assert_payment_mode("P", "Mobile Money")

	def test_explicit_foreign_cash_mapping_is_used_for_posted_mode(self):
		with self.bridge([MAPPED[0], {"mode": "Foreign Cash", "currencies": ["EUR"]}]):
			localization.assert_payment_mode("P", "Cash", "EUR")
			localization.assert_payment_mode("P", "Foreign Cash", "EUR")
			with self.assertRaises(frappe.ValidationError):
				localization.assert_payment_mode("P", "Cash", "USD")

	def test_empty_mapping_does_not_fall_back_to_pack_modes(self):
		with self.bridge([]):
			self.assertEqual(unwrap(api.property_locale)("P")["tender_modes"], [])
			with self.assertRaises(frappe.ValidationError):
				localization.assert_payment_mode("P", "Cash")

	def test_bad_mapping_does_not_fall_back_to_pack_modes(self):
		with self.bridge({"tender_modes": [{"mode": "Cash", "currencies": "UGX"}]}):
			with self.assertRaisesRegex(ValueError, "Invalid tender configuration"):
				localization.assert_payment_mode("P", "Cash")

	def test_folio_writer_rejects_unmapped_pair_before_posting(self):
		folio = Mock(property="P")
		with (
			self.bridge(MAPPED),
			patch.object(api, "_pin_guard"),
			patch("frappe.get_doc", return_value=folio),
			patch(
				"kamra.business_date.get_business_date",
				side_effect=AssertionError("unmapped tender reached posting"),
			),
		):
			with self.assertRaises(frappe.ValidationError):
				unwrap(api.add_folio_payment)("F", "Bank Transfer", 10, currency="UGX")
		folio.append.assert_not_called()
