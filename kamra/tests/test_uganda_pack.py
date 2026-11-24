"""Uganda localization pack defaults, VAT/TIN vocabulary, bridge-aware 0% tax,
Bank of Uganda denominations, Local Hotel Tax levy and no static INVOICE_TITLE."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import frappe

from kamra import erp_bridge, hooks, localization
from kamra.localization import india, uganda


class TestUgandaPack(unittest.TestCase):
	def test_regional_defaults(self):
		self.assertEqual(uganda.DEFAULT_CURRENCY, "UGX")
		self.assertEqual(uganda.DEFAULT_TIMEZONE, "Africa/Kampala")
		self.assertEqual(ZoneInfo(uganda.DEFAULT_TIMEZONE).key, "Africa/Kampala")
		self.assertEqual(uganda.DEFAULT_NATIONALITY, "Ugandan")
		self.assertEqual(uganda.HOME_COUNTRY_ISO3, "UGA")
		self.assertEqual(uganda.DEFAULT_VAT, Decimal("18"))
		self.assertEqual(uganda.ROOM_LEVY_LABEL, "Local Hotel Tax")
		self.assertEqual(uganda.PAYMENT_MODES, ["Cash", "Card", "Bank Transfer", "Mobile Money"])
		self.assertEqual(uganda.INSTANT_PAYMENT_MODES, ["Mobile Money"])
		self.assertEqual(
			localization.cash_denominations(uganda),
			[50000.0, 20000.0, 10000.0, 5000.0, 2000.0, 1000.0, 500.0, 200.0, 100.0, 50.0],
		)
		self.assertEqual(uganda.tax_rate_options(None), [0, 18])
		# PR-11: no static Uganda INVOICE_TITLE — title follows VAT determination
		self.assertFalse(hasattr(uganda, "INVOICE_TITLE"))
		self.assertEqual(localization.invoice_title(uganda), "Invoice")
		self.assertFalse(localization.uses_tax_slab(uganda))
		self.assertTrue(localization.uses_tax_slab(india))

	def test_invoice_context_uses_vat_and_tin(self):
		ctx = uganda.invoice_context(frappe._dict(name=None))
		self.assertEqual(ctx["tax_label"], "VAT")
		self.assertEqual(ctx["tax_id_label"], "TIN")
		self.assertIsNone(ctx["place_of_supply"])
		self.assertIsNone(ctx["service_code"])
		self.assertEqual(ctx["split"], [("vat", Decimal("1"))])
		self.assertEqual(
			uganda.invoice_context({"city": "Kampala", "state": "Central"})["place_of_supply"],
			"Kampala",
		)

	def test_locale_uses_vat_tin_en_ug_and_honours_property_locale(self):
		with patch.object(uganda, "frappe") as framework:
			framework.db.get_value.return_value = "UGX "
			loc = uganda.locale(frappe._dict(name=None, currency="UGX"))
			self.assertEqual(loc["currency"], "UGX")
			self.assertEqual(loc["currency_symbol"], "UGX ")
			self.assertEqual(loc["locale"], "en-UG")
			self.assertEqual(loc["tax_label"], "VAT")
			self.assertEqual(loc["tax_id_label"], "TIN")
			self.assertEqual(loc["tax_rates"], [0, 18])

			custom = uganda.locale(frappe._dict(name=None, currency="UGX", locale="en-GB"))
			self.assertEqual(custom["locale"], "en-GB")

	def test_slab_mode_is_ignored_for_uganda(self):
		prop = frappe._dict(
			name="P", country="Uganda", currency="UGX",
			gst_mode="Slab", gst_slab_threshold=7500, gst_rate_low=18, gst_rate_high=28,
		)
		with (
			patch.object(erp_bridge, "get_bridge_script", return_value=None),
			patch("frappe.get_cached_doc", return_value=prop),
		):
			for tariff in (3000, 10000, 250000):
				with self.subTest(tariff=tariff):
					self.assertEqual(uganda.calculate_room_tax("P", {}, tariff), Decimal("18"))
			self.assertEqual(uganda.fnb_tax_rate("P"), 18.0)

	def test_bridge_keeps_uganda_tax_at_zero(self):
		prop = frappe._dict(
			name="P", country="Uganda", currency="UGX",
			gst_mode="Fixed", gst_rate_low=18, gst_rate_high=0,
		)
		bridge_script = Mock()
		with (
			patch.object(erp_bridge, "get_bridge_script", return_value=bridge_script),
			patch("frappe.get_cached_doc", return_value=prop),
			patch("frappe.get_hooks", return_value=hooks.kamra_localization),
			patch("frappe.db.exists", return_value=True),
			patch("frappe.get_meta") as get_meta,
		):
			get_meta.return_value.has_field.return_value = True
			self.assertEqual(uganda.calculate_room_tax("P", {"tax_percent": 18}, 200000), Decimal("0"))
			self.assertEqual(uganda.fnb_tax_rate("P"), 0.0)
			self.assertEqual(uganda.tax_rate_options("P"), [0])
			defaults = localization.property_country_defaults("Uganda")
			self.assertNotIn("gst_rate_low", defaults)

	def test_standalone_uganda_defaults_vat_18_and_levy_label(self):
		with (
			patch.object(erp_bridge, "get_bridge_script", return_value=None),
			patch("frappe.get_hooks", return_value=hooks.kamra_localization),
			patch("frappe.db.exists", return_value=True),
			patch("frappe.get_meta") as get_meta,
		):
			get_meta.return_value.has_field.return_value = True
			defaults = localization.property_country_defaults("Uganda")
			self.assertEqual(defaults["currency"], "UGX")
			self.assertEqual(defaults["timezone"], "Africa/Kampala")
			self.assertEqual(defaults["gst_rate_low"], 18.0)
			self.assertEqual(defaults["room_levy_label"], "Local Hotel Tax")
			self.assertNotIn("gst_mode", defaults)
			self.assertNotIn("gst_slab_threshold", defaults)
			self.assertNotIn("security_deposit_amount", defaults)

	def test_hook_resolves_uganda(self):
		self.assertEqual(hooks.kamra_localization["Uganda"], "kamra.localization.uganda")
		mapping = {key: [value] for key, value in hooks.kamra_localization.items()}
		with patch.object(localization.frappe, "get_hooks", return_value=mapping):
			self.assertIs(localization.pack_for_country("Uganda"), uganda)
			countries = {row["country"]: row for row in localization.supported_countries()}
			self.assertEqual(countries["Uganda"]["currency"], "UGX")
			self.assertEqual(countries["Uganda"]["timezone"], "Africa/Kampala")
			self.assertEqual(countries["Uganda"]["tax_label"], "VAT")
			self.assertEqual(countries["Uganda"]["tax_id_label"], "TIN")
