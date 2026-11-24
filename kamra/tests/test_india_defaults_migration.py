"""Migration patch replacing untouched India-era defaults on non-India properties.

Replaces ONLY values that still equal the old India defaults AND were not
changed by a client:
  - slab 7500 (and untouched India Slab 5/18/7500 triplet)
  - refund limit 5000 (Property Setting + Cashier records)
  - high-balance 50000 (Property Setting)
  - deposit 5000 (Property)
  - locale en-IN (Property)
  - UGX format #,###.## -> #,### (Currency)

Money thresholds left to the customer are unset (empty/0/None), never derived
numbers. Client-changed values (different value OR explicit Version history)
and Indian properties are never overwritten.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import flt

from kamra import cashier, night_audit, settings
from kamra.tests.audit_fixtures import AuditTestCase

UG_UNTOUCHED = "UT Mig Uganda Untouched"
UG_CHANGED = "UT Mig Uganda Changed"
UG_VERSIONED = "UT Mig Uganda Versioned"
IN_PROP = "UT Mig India Kept"


def _add_version(ref_doctype: str, docname: str, field: str, old, new) -> None:
	frappe.get_doc({
		"doctype": "Version",
		"ref_doctype": ref_doctype,
		"docname": docname,
		"data": json.dumps({"changed": [[field, old, new]]}),
	}).insert(ignore_permissions=True)


class UntouchedIndiaDefaultsMigrationTestCase(AuditTestCase):
	properties = (UG_UNTOUCHED, UG_CHANGED, UG_VERSIONED, IN_PROP)

	def setUp(self):
		super().setUp()
		for p in self.properties:
			settings.clear_cache(p)
			self.addCleanup(settings.clear_cache, p)

	def _insert_raw_property(self, name: str, country: str, **fields) -> str:
		doc = frappe.get_doc({
			"doctype": "Property",
			"property_name": name,
			"city": "Testville",
			"country": country,
			**fields,
		})
		doc.insert(ignore_permissions=True)
		# stamp exact legacy values as they existed in pre-PR-5 databases
		if fields:
			frappe.db.set_value("Property", name, fields, update_modified=False)
		frappe.clear_document_cache("Property", name)
		return name

	def test_untouched_india_defaults_on_uganda_property_are_replaced_and_idempotent(self):
		from kamra.patches.v48 import replace_untouched_india_defaults

		self._insert_raw_property(
			UG_UNTOUCHED, "Uganda",
			currency="UGX", timezone="Africa/Kampala",
			locale="en-IN",
			gst_mode="Slab", gst_slab_threshold=7500, gst_rate_low=5, gst_rate_high=18,
			security_deposit_amount=5000,
		)
		settings.write_kv(UG_UNTOUCHED, "na_high_balance_threshold", 50000)
		settings.write_kv(UG_UNTOUCHED, "max_cash_refund", 5000)
		settings.clear_cache(UG_UNTOUCHED)
		csh = cashier.ensure_cashier(UG_UNTOUCHED, "Administrator")
		frappe.db.set_value("Cashier", csh, "max_cash_refund", 5000, update_modified=False)

		if not frappe.db.exists("Currency", "UGX"):
			frappe.get_doc({
				"doctype": "Currency", "currency_name": "UGX", "symbol": "UGX ",
				"number_format": "#,###.##", "enabled": 1,
			}).insert(ignore_permissions=True)
		else:
			frappe.db.set_value("Currency", "UGX", "number_format", "#,###.##", update_modified=False)
		frappe.db.delete("Version", {"ref_doctype": "Currency", "docname": "UGX"})

		replace_untouched_india_defaults.execute()
		replace_untouched_india_defaults.execute()  # safe to run twice

		frappe.clear_document_cache("Property", UG_UNTOUCHED)
		settings.clear_cache(UG_UNTOUCHED)
		prop = frappe.db.get_value(
			"Property", UG_UNTOUCHED,
			["gst_mode", "gst_slab_threshold", "gst_rate_low", "gst_rate_high",
			 "security_deposit_amount", "locale"],
			as_dict=True,
		)
		self.assertFalse(prop.gst_mode)
		self.assertEqual(flt(prop.gst_slab_threshold), 0)
		self.assertEqual(flt(prop.gst_rate_low), 0)
		self.assertEqual(flt(prop.gst_rate_high), 0)
		self.assertEqual(flt(prop.security_deposit_amount), 0)
		self.assertFalse(prop.locale)

		self.assertIsNone(settings.get_setting(UG_UNTOUCHED, "na_high_balance_threshold"))
		self.assertIsNone(settings.get_setting(UG_UNTOUCHED, "max_cash_refund"))
		self.assertFalse(night_audit.check_high_balance(UG_UNTOUCHED)["enabled"])
		self.assertEqual(flt(frappe.db.get_value("Cashier", csh, "max_cash_refund")), 0)
		self.assertEqual(frappe.db.get_value("Currency", "UGX", "number_format"), "#,###")

	def test_client_changed_values_are_never_overwritten(self):
		from kamra.patches.v48 import replace_untouched_india_defaults

		# 1. Client changed values to different figures
		self._insert_raw_property(
			UG_CHANGED, "Uganda",
			currency="UGX", timezone="Africa/Kampala",
			locale="en-GB",
			gst_mode="Fixed", gst_slab_threshold=10000, gst_rate_low=18, gst_rate_high=0,
			security_deposit_amount=200000,
		)
		settings.write_kv(UG_CHANGED, "na_high_balance_threshold", 3000000)
		settings.write_kv(UG_CHANGED, "max_cash_refund", 500000)
		settings.clear_cache(UG_CHANGED)
		csh_changed = cashier.ensure_cashier(UG_CHANGED, "Administrator")
		frappe.db.set_value("Cashier", csh_changed, "max_cash_refund", 500000, update_modified=False)

		# 2. Client explicitly saved the SAME numeric default (recorded in Version)
		self._insert_raw_property(
			UG_VERSIONED, "Uganda",
			currency="UGX", timezone="Africa/Kampala",
			locale="en-IN",
			gst_mode="Slab", gst_slab_threshold=7500, gst_rate_low=5, gst_rate_high=18,
			security_deposit_amount=5000,
		)
		_add_version("Property", UG_VERSIONED, "security_deposit_amount", 0, 5000)
		_add_version("Property", UG_VERSIONED, "locale", "", "en-IN")
		_add_version("Property", UG_VERSIONED, "gst_slab_threshold", 0, 7500)

		settings.write_kv(UG_VERSIONED, "na_high_balance_threshold", 50000)
		settings.write_kv(UG_VERSIONED, "max_cash_refund", 5000)
		settings.clear_cache(UG_VERSIONED)
		ps_hb = frappe.db.get_value("Property Setting", {"property": UG_VERSIONED, "setting_key": "na_high_balance_threshold"})
		ps_ref = frappe.db.get_value("Property Setting", {"property": UG_VERSIONED, "setting_key": "max_cash_refund"})
		_add_version("Property Setting", ps_hb, "value", "0", "50000")
		_add_version("Property Setting", ps_ref, "value", "0", "5000")

		csh_ver = cashier.ensure_cashier(UG_VERSIONED, "Administrator")
		frappe.db.set_value("Cashier", csh_ver, "max_cash_refund", 5000, update_modified=False)
		_add_version("Cashier", csh_ver, "max_cash_refund", 0, 5000)

		if not frappe.db.exists("Currency", "UGX"):
			frappe.get_doc({
				"doctype": "Currency", "currency_name": "UGX", "symbol": "UGX ",
				"number_format": "#.###,##", "enabled": 1,
			}).insert(ignore_permissions=True)
		else:
			frappe.db.set_value("Currency", "UGX", "number_format", "#.###,##", update_modified=False)

		replace_untouched_india_defaults.execute()

		settings.clear_cache(UG_CHANGED)
		settings.clear_cache(UG_VERSIONED)

		# Verify client-changed values stayed intact
		p_ch = frappe.db.get_value(
			"Property", UG_CHANGED,
			["gst_mode", "gst_slab_threshold", "gst_rate_low", "security_deposit_amount", "locale"],
			as_dict=True,
		)
		self.assertEqual(p_ch.gst_mode, "Fixed")
		self.assertEqual(flt(p_ch.gst_slab_threshold), 10000)
		self.assertEqual(flt(p_ch.gst_rate_low), 18)
		self.assertEqual(flt(p_ch.security_deposit_amount), 200000)
		self.assertEqual(p_ch.locale, "en-GB")
		self.assertEqual(settings.get_setting(UG_CHANGED, "na_high_balance_threshold"), 3000000)
		self.assertEqual(settings.get_setting(UG_CHANGED, "max_cash_refund"), 500000)
		self.assertEqual(flt(frappe.db.get_value("Cashier", csh_changed, "max_cash_refund")), 500000)
		self.assertEqual(frappe.db.get_value("Currency", "UGX", "number_format"), "#.###,##")

		# Verify Version-tracked same-number client edits stayed intact
		p_ver = frappe.db.get_value(
			"Property", UG_VERSIONED,
			["gst_slab_threshold", "security_deposit_amount", "locale"],
			as_dict=True,
		)
		self.assertEqual(flt(p_ver.gst_slab_threshold), 7500)
		self.assertEqual(flt(p_ver.security_deposit_amount), 5000)
		self.assertEqual(p_ver.locale, "en-IN")
		self.assertEqual(settings.get_setting(UG_VERSIONED, "na_high_balance_threshold"), 50000)
		self.assertEqual(settings.get_setting(UG_VERSIONED, "max_cash_refund"), 5000)
		self.assertEqual(flt(frappe.db.get_value("Cashier", csh_ver, "max_cash_refund")), 5000)

	def test_india_property_keeps_all_india_defaults(self):
		from kamra.patches.v48 import replace_untouched_india_defaults

		self._insert_raw_property(
			IN_PROP, "India",
			currency="INR", timezone="Asia/Kolkata",
			locale="en-IN",
			gst_mode="Slab", gst_slab_threshold=7500, gst_rate_low=5, gst_rate_high=18,
			security_deposit_amount=5000,
		)
		settings.write_kv(IN_PROP, "na_high_balance_threshold", 50000)
		settings.write_kv(IN_PROP, "max_cash_refund", 5000)
		settings.clear_cache(IN_PROP)
		csh_in = cashier.ensure_cashier(IN_PROP, "Administrator")
		frappe.db.set_value("Cashier", csh_in, "max_cash_refund", 5000, update_modified=False)

		replace_untouched_india_defaults.execute()

		settings.clear_cache(IN_PROP)
		p_in = frappe.db.get_value(
			"Property", IN_PROP,
			["gst_mode", "gst_slab_threshold", "gst_rate_low", "gst_rate_high",
			 "security_deposit_amount", "locale"],
			as_dict=True,
		)
		self.assertEqual(p_in.gst_mode, "Slab")
		self.assertEqual(flt(p_in.gst_slab_threshold), 7500)
		self.assertEqual(flt(p_in.gst_rate_low), 5)
		self.assertEqual(flt(p_in.gst_rate_high), 18)
		self.assertEqual(flt(p_in.security_deposit_amount), 5000)
		self.assertEqual(p_in.locale, "en-IN")
		self.assertEqual(settings.get_setting(IN_PROP, "na_high_balance_threshold"), 50000)
		self.assertEqual(settings.get_setting(IN_PROP, "max_cash_refund"), 5000)
		self.assertEqual(flt(frappe.db.get_value("Cashier", csh_in, "max_cash_refund")), 5000)
