"""Country-neutral defaults (PMS parity PR-5, OPERA property parameters).

A new property takes its currency, clock, tax slab and money thresholds
from its own country pack, the way OPERA seeds a property from its chain
template. India keeps every value it had. Uganda gets UGX and Kampala
time and no INR-scaled amount: an amount nobody set is not acted on (the
high-balance check stays off, a new cashier gets no refund limit).
Values the caller gives, and values already saved, always win."""

from __future__ import annotations

from unittest.mock import Mock, patch

import frappe
from frappe.utils import flt

from kamra import api, cashier, erp_bridge, settings
from kamra.tests.audit_fixtures import AuditTestCase, d, make_reservation, make_room_type

UG = "UT Country Uganda"
IN = "UT Country India"
BLANK = "UT Country Blank"

MONEY_FIELDS = ("gst_slab_threshold", "gst_rate_low", "gst_rate_high", "security_deposit_amount")


def bare_property(name: str, country: str, **fields) -> str:
	"""A property inserted with only what an operator types - no fixture
	defaults - so the controller's own defaults are what is tested."""
	frappe.get_doc({"doctype": "Property", "property_name": name, "city": "Testville",
	                "country": country, **fields}).insert(ignore_permissions=True)
	frappe.clear_document_cache("Property", name)
	settings.clear_cache(name)
	return name


def stored(name: str, *fields):
	return frappe.db.get_value("Property", name, list(fields), as_dict=True)


class CountryDefaultsTestCase(AuditTestCase):
	properties = (UG, IN, BLANK)

	def setUp(self):
		super().setUp()
		for p in self.properties:
			settings.clear_cache(p)
			self.addCleanup(settings.clear_cache, p)

	# ── the Property record ─────────────────────────────────────────────

	def test_a_uganda_property_takes_currency_and_clock_from_its_pack(self):
		bare_property(UG, "Uganda")
		row = stored(UG, "currency", "timezone", "locale")
		self.assertEqual(row.currency, "UGX")
		self.assertEqual(row.timezone, "Africa/Kampala")
		# blank: the pack's own number locale applies, never en-IN
		self.assertFalse(row.locale)
		from kamra.localization import pack_for
		loc = pack_for(UG).locale(frappe.get_cached_doc("Property", UG))
		self.assertEqual(loc["locale"], "en-UG")

	def test_a_uganda_property_has_no_india_tax_slab_or_inr_deposit(self):
		with patch.object(erp_bridge, "get_bridge_script", return_value=None):
			bare_property(UG, "Uganda")
		row = stored(UG, "gst_mode", "room_levy_label", *MONEY_FIELDS)
		self.assertNotEqual(row.gst_mode, "Slab")
		self.assertEqual(row.room_levy_label, "Local Hotel Tax")
		for field in ("gst_slab_threshold", "gst_rate_high", "security_deposit_amount"):
			with self.subTest(field=field):
				self.assertEqual(flt(row[field]), 0)
		# standalone Uganda defaults to 18% VAT
		self.assertEqual(flt(row.gst_rate_low), 18)

	def test_uganda_property_on_erp_bridge_keeps_zero_folio_tax(self):
		from decimal import Decimal
		from kamra.localization import pack_for

		with patch.object(erp_bridge, "get_bridge_script", return_value=Mock()):
			bare_property(UG, "Uganda")
			row = stored(UG, "gst_rate_low", "gst_slab_threshold")
			self.assertEqual(flt(row.gst_rate_low), 0)
			self.assertEqual(flt(row.gst_slab_threshold), 0)
			pack = pack_for(UG)
			self.assertEqual(pack.calculate_room_tax(UG, {}, Decimal("250000")), Decimal("0"))
			self.assertEqual(pack.fnb_tax_rate(UG), 0.0)

	def test_uganda_settings_schema_hides_slab_fields_and_exposes_locale(self):
		bare_property(UG, "Uganda")
		bare_property(IN, "India")

		ug_schema = settings.settings_schema(UG)
		ug_mods = {m["id"]: m for m in ug_schema["modules"]}
		ug_tax_keys = [
			s["key"]
			for sec in ug_mods["finance"]["sections"] if sec["id"] == "tax"
			for s in sec["settings"]
		]
		for hidden in ("gst_mode", "gst_slab_threshold", "gst_rate_high"):
			self.assertNotIn(hidden, ug_tax_keys)
		self.assertIn("gst_rate_low", ug_tax_keys)
		self.assertIn("rates_include_tax", ug_tax_keys)

		ug_loc_keys = [
			s["key"]
			for sec in ug_mods["general"]["sections"] if sec["id"] == "location"
			for s in sec["settings"]
		]
		for key in ("country", "currency", "timezone", "locale", "date_format", "booking_language"):
			self.assertIn(key, ug_loc_keys)

		in_schema = settings.settings_schema(IN)
		in_mods = {m["id"]: m for m in in_schema["modules"]}
		in_tax_keys = [
			s["key"]
			for sec in in_mods["finance"]["sections"] if sec["id"] == "tax"
			for s in sec["settings"]
		]
		for kept in ("gst_mode", "gst_slab_threshold", "gst_rate_low", "gst_rate_high"):
			self.assertIn(kept, in_tax_keys)

	def test_property_locale_and_date_format_roundtrip_via_settings(self):
		bare_property(UG, "Uganda")
		settings.save_settings(UG, "general", {"locale": "en-GB", "date_format": "dd/mm/yyyy"})
		frappe.clear_document_cache("Property", UG)
		loc = api.property_locale(UG)
		self.assertEqual(loc["locale"], "en-GB")
		self.assertEqual(loc["date_format"], "dd/mm/yyyy")

	def test_an_india_property_keeps_every_default_it_had(self):
		bare_property(IN, "India")
		row = stored(IN, "currency", "timezone", "gst_mode", *MONEY_FIELDS)
		self.assertEqual(row.currency, "INR")
		self.assertEqual(row.timezone, "Asia/Kolkata")
		self.assertEqual(row.gst_mode, "Slab")
		self.assertEqual(flt(row.gst_slab_threshold), 7500)
		self.assertEqual(flt(row.gst_rate_low), 5)
		self.assertEqual(flt(row.gst_rate_high), 18)
		self.assertEqual(flt(row.security_deposit_amount), 5000)

	def test_values_the_operator_gives_win_over_the_pack(self):
		bare_property(UG, "Uganda", currency="USD", timezone="Europe/London",
		              gst_rate_low=18, security_deposit_amount=200000)
		row = stored(UG, "currency", "timezone", "gst_rate_low", "security_deposit_amount")
		self.assertEqual(row.currency, "USD")
		self.assertEqual(row.timezone, "Europe/London")
		self.assertEqual(flt(row.gst_rate_low), 18)
		self.assertEqual(flt(row.security_deposit_amount), 200000)

	# ── money thresholds in the settings registry ───────────────────────

	def test_uganda_has_no_inr_money_thresholds(self):
		bare_property(UG, "Uganda")
		self.assertFalse(settings.get_setting(UG, "na_high_balance_threshold"))
		self.assertFalse(settings.get_setting(UG, "max_cash_refund"))

	def test_india_keeps_its_money_thresholds(self):
		bare_property(IN, "India")
		self.assertEqual(settings.get_setting(IN, "na_high_balance_threshold"), 50000)
		self.assertEqual(settings.get_setting(IN, "max_cash_refund"), 5000)

	def test_a_saved_threshold_is_kept(self):
		bare_property(UG, "Uganda")
		settings.write_kv(UG, "na_high_balance_threshold", 2000000)
		settings.clear_cache(UG)
		self.assertEqual(settings.get_setting(UG, "na_high_balance_threshold"), 2000000)

	def test_the_high_balance_check_stays_off_until_a_threshold_is_set(self):
		from kamra import night_audit
		bare_property(UG, "Uganda")
		check = night_audit.check_high_balance(UG)
		self.assertFalse(check["enabled"])
		self.assertEqual(check["count"], 0)
		settings.write_kv(UG, "na_high_balance_threshold", 2000000)
		settings.clear_cache(UG)
		self.assertTrue(night_audit.check_high_balance(UG)["enabled"])

	def test_a_new_cashier_in_uganda_gets_no_inr_refund_limit(self):
		bare_property(UG, "Uganda")
		name = cashier.ensure_cashier(UG, "Administrator")
		self.assertEqual(flt(frappe.db.get_value("Cashier", name, "max_cash_refund")), 0)

	# ── guests and tax constants ────────────────────────────────────────

	def test_an_occupant_added_in_uganda_is_ugandan_by_default(self):
		bare_property(UG, "Uganda", rate_plan_required=0)
		res = make_reservation(UG, make_room_type(UG), d(1), d(2))
		res.append("occupants", {"full_name": "UT Second Guest"})
		res.save(ignore_permissions=True)
		self.assertEqual(res.occupants[-1].nationality, "Ugandan")

	def test_an_occupant_added_in_india_is_still_indian_by_default(self):
		bare_property(IN, "India", rate_plan_required=0)
		res = make_reservation(IN, make_room_type(IN), d(1), d(2))
		res.append("occupants", {"full_name": "UT Second Guest"})
		res.save(ignore_permissions=True)
		self.assertEqual(res.occupants[-1].nationality, "Indian")

	def test_an_occupant_nationality_typed_by_the_desk_is_kept(self):
		bare_property(UG, "Uganda", rate_plan_required=0)
		res = make_reservation(UG, make_room_type(UG), d(1), d(2))
		res.append("occupants", {"full_name": "UT Visitor", "nationality": "Kenyan"})
		res.save(ignore_permissions=True)
		self.assertEqual(res.occupants[-1].nationality, "Kenyan")

	def test_core_folio_carries_no_country_tax_rate(self):
		# the F&B rate is the pack's (kamra.folio._fnb_gst); a module-level
		# India 5% in core is the hardcoding this replaces
		import kamra.folio
		self.assertFalse(hasattr(kamra.folio, "FNB_GST"))

	# ── blank Property.country fallback (Company -> System -> neutral) ──

	def test_property_country_field_has_no_india_default(self):
		df = frappe.get_meta("Property").get_field("country")
		self.assertIsNotNone(df)
		self.assertFalse(df.default)

	def test_blank_country_falls_back_to_company_then_system_then_neutral(self):
		from decimal import Decimal
		from kamra import localization, public_api

		orig_sys_country = frappe.db.get_single_value("System Settings", "country")
		has_company_tbl = frappe.db.table_exists("Company")
		orig_def_company = (
			frappe.db.get_single_value("Global Defaults", "default_company")
			if frappe.db.exists("DocType", "Global Defaults") else None
		)

		def _restore():
			frappe.db.set_single_value("System Settings", "country", orig_sys_country or "")
			if frappe.db.exists("DocType", "Global Defaults"):
				frappe.db.set_single_value("Global Defaults", "default_company", orig_def_company or "")
			frappe.clear_cache()

		self.addCleanup(_restore)
		frappe.db.set_single_value("System Settings", "country", "")
		if frappe.db.exists("DocType", "Global Defaults"):
			frappe.db.set_single_value("Global Defaults", "default_company", "")
		frappe.clear_cache()

		# 1. No Property.country, no Company, no System Settings.country -> neutral (generic, 0% tax)
		bare_property(BLANK, "")
		row = stored(BLANK, "country", "currency", "gst_mode", *MONEY_FIELDS)
		self.assertFalse(row.country)
		self.assertNotEqual(row.currency, "INR")
		self.assertNotEqual(row.gst_mode, "Slab")
		self.assertEqual(flt(row.gst_rate_low), 0)
		self.assertEqual(flt(row.gst_rate_high), 0)
		self.assertEqual(flt(row.security_deposit_amount), 0)
		pack = localization.pack_for(BLANK)
		self.assertEqual(pack.__name__, "kamra.localization.generic")
		self.assertEqual(pack.calculate_room_tax(BLANK, {}, Decimal("10000")), Decimal("0"))
		self.assertEqual(pack.fnb_tax_rate(BLANK), 0.0)
		self.assertEqual(api._booking_property_policy(BLANK)["country"], "")
		self.assertEqual(public_api._property_payload(frappe.get_doc("Property", BLANK))["country"], "")

		# 2. System Settings.country = "Uganda" -> Uganda pack applies when Property.country is blank
		frappe.db.set_single_value("System Settings", "country", "Uganda")
		frappe.clear_document_cache("Property", BLANK)
		self.assertEqual(localization.pack_for(BLANK).__name__, "kamra.localization.uganda")
		self.assertEqual(api._booking_property_policy(BLANK)["country"], "Uganda")
		self.assertEqual(public_api._property_payload(frappe.get_doc("Property", BLANK))["country"], "Uganda")
		self.assertEqual(localization.property_country_defaults("")["currency"], "UGX")

		# 3. Company country wins over System Settings.country
		frappe.db.set_single_value("System Settings", "country", "India")
		real_table_exists = frappe.db.table_exists
		real_get_single = frappe.db.get_single_value
		real_get_value = frappe.db.get_value

		def _fake_table_exists(dt, *a, **kw):
			if dt == "Company":
				return True
			return real_table_exists(dt, *a, **kw)

		def _fake_get_single(dt, field, *a, **kw):
			if dt == "Global Defaults" and field == "default_company":
				return "UT Germany Co"
			return real_get_single(dt, field, *a, **kw)

		def _fake_get_value(dt, name, field=None, *a, **kw):
			if dt == "Company" and name == "UT Germany Co" and field == "country":
				return "Germany"
			return real_get_value(dt, name, field, *a, **kw)

		with (
			patch.object(frappe.db, "table_exists", side_effect=_fake_table_exists),
			patch.object(frappe.db, "get_single_value", side_effect=_fake_get_single),
			patch.object(frappe.db, "get_value", side_effect=_fake_get_value),
		):
			self.assertEqual(localization.resolve_country(BLANK), "Germany")
			self.assertEqual(localization.pack_for(BLANK).__name__, "kamra.localization.germany")
			self.assertEqual(api._booking_property_policy(BLANK)["country"], "Germany")
			self.assertEqual(public_api._property_payload(frappe.get_doc("Property", BLANK))["country"], "Germany")

		_ = has_company_tbl

	def test_backfill_blank_property_country_patch_sets_india_only_when_company_is_india(self):
		from kamra.patches.v48 import backfill_blank_property_country

		bare_property(BLANK, "")
		bare_property(UG, "Uganda")
		frappe.db.set_value("Property", BLANK, "country", "", update_modified=False)
		frappe.clear_document_cache("Property", BLANK)

		real_table_exists = frappe.db.table_exists
		real_get_single = frappe.db.get_single_value
		real_get_value = frappe.db.get_value

		def _with_company_country(country_val):
			def _te(dt, *a, **kw):
				return True if dt == "Company" else real_table_exists(dt, *a, **kw)

			def _gs(dt, field, *a, **kw):
				if dt == "Global Defaults" and field == "default_company":
					return "UT Patch Co" if country_val else None
				return real_get_single(dt, field, *a, **kw)

			def _gv(dt, name, field=None, *a, **kw):
				if dt == "Company" and name == "UT Patch Co" and field == "country":
					return country_val
				return real_get_value(dt, name, field, *a, **kw)

			return (
				patch.object(frappe.db, "table_exists", side_effect=_te),
				patch.object(frappe.db, "get_single_value", side_effect=_gs),
				patch.object(frappe.db, "get_value", side_effect=_gv),
			)

		# Uganda company -> blank stays blank
		p1, p2, p3 = _with_company_country("Uganda")
		with p1, p2, p3:
			backfill_blank_property_country.execute()
		self.assertFalse(stored(BLANK, "country").country)

		# No company -> blank stays blank
		p1, p2, p3 = _with_company_country(None)
		with p1, p2, p3:
			backfill_blank_property_country.execute()
		self.assertFalse(stored(BLANK, "country").country)

		# India company -> blank becomes "India" explicitly, idempotent, explicit Uganda untouched
		p1, p2, p3 = _with_company_country("India")
		with p1, p2, p3:
			backfill_blank_property_country.execute()
			backfill_blank_property_country.execute()
		self.assertEqual(stored(BLANK, "country").country, "India")
		self.assertEqual(stored(UG, "country").country, "Uganda")
