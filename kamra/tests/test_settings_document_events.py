"""A Property Setting change runs its document events, so a site's own
Before Save rule sees every write path (SAP: one owner of the change,
with a change log).

- write_kv saves the Property Setting document (validate and on_update
  run, Version history records before and after); the cache clears once.
- save_settings marks the row with the settings page it came from
  (flags.kamra_settings_module), so a site rule can tell the page apart
  from any other writer. Nothing outside save_settings sets that flag.
- A credit-note voucher from a cancellation is marked
  (flags.kamra_credit_note) so a site rule on Discount Voucher can tell it
  from a promo code a person creates.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from kamra import api, settings
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)


HERE = "kamra.tests.test_settings_document_events"
SEEN_VOUCHERS: list = []


def refuse_governed_limit(doc, method=None):
	"""A site rule (as a governed-settings fence would): only the settings
	page is refused here, so the test can tell the flag reached it."""
	if doc.setting_key == "rate_override_limit_pct" and doc.flags.get("kamra_settings_module"):
		frappe.throw("UT governed", frappe.PermissionError)


def remember_voucher(doc, method=None):
	SEEN_VOUCHERS.append(doc.flags.get("kamra_credit_note"))


class SettingsDocumentEventsTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY)
		make_property(PROP_A, business_date=TODAY)
		frappe.db.delete("Property Setting", {"property": PROP_A})
		self.addCleanup(settings.clear_cache)
		settings.clear_cache(PROP_A)
		self.seen: list[dict] = []
		self.before: list[str] = []
		real = frappe.get_doc

		def watch(*args, **kwargs):
			doc = real(*args, **kwargs)
			if getattr(doc, "doctype", None) == "Property Setting":
				original = doc.run_method

				def run_method(method, *a, **kw):
					if method == "validate":
						self.seen.append({"key": doc.setting_key, "value": doc.value,
						                  "module": doc.flags.get("kamra_settings_module")})
						before = doc.get_doc_before_save()
						if before:
							self.before.append(before.value)
					return original(method, *a, **kw)

				doc.run_method = run_method
			return doc

		self._stack.enter_context(patch.object(frappe, "get_doc", watch))

	def test_a_new_row_runs_validate(self):
		settings.write_kv(PROP_A, "rate_override_limit_pct", 10)
		self.assertEqual(self.seen, [{"key": "rate_override_limit_pct", "value": "10", "module": None}])
		self.assertEqual(settings.get_setting(PROP_A, "rate_override_limit_pct"), 10)

	def test_an_existing_row_runs_validate_and_keeps_history(self):
		settings.write_kv(PROP_A, "rate_override_limit_pct", 10)
		self.seen.clear()
		settings.write_kv(PROP_A, "rate_override_limit_pct", 15)
		self.assertEqual(self.seen, [{"key": "rate_override_limit_pct", "value": "15", "module": None}])
		# Frappe skips Version rows under tests (Document.save: ignore_version =
		# frappe.in_test); a saved document carries its before-image, which is
		# what Version and a Before Save rule compare.
		self.assertEqual(self.before, ["10"])
		self.assertEqual(settings.get_setting(PROP_A, "rate_override_limit_pct"), 15)

	def test_an_unchanged_value_writes_nothing(self):
		settings.write_kv(PROP_A, "fx_margin_pct", 2.5)
		self.seen.clear()
		settings.write_kv(PROP_A, "fx_margin_pct", 2.5)
		self.assertEqual(self.seen, [])

	def test_the_settings_page_marks_its_rows(self):
		settings.save_settings(PROP_A, "finance", {"allowance_approval_above": 5000, "fx_auto_fetch": 1})
		self.assertEqual(sorted((s["key"], s["module"]) for s in self.seen),
		                 [("allowance_approval_above", "finance"), ("fx_auto_fetch", "finance")])
		self.assertEqual(settings.get_setting(PROP_A, "allowance_approval_above"), 5000)

	def test_a_site_rule_refusal_stops_the_settings_page(self):
		hooks = {"Property Setting": {"validate": [f"{HERE}.refuse_governed_limit"]}}
		with patch.object(frappe, "get_doc_hooks", lambda: hooks):
			with self.assertRaises(frappe.PermissionError):
				settings.save_settings(PROP_A, "finance", {"rate_override_limit_pct": 25})
		self.assertEqual(settings.get_setting(PROP_A, "rate_override_limit_pct"), 0)


class CreditNoteVoucherFlagTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.api")
		make_property(PROP_A, business_date=TODAY)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.room = make_room(PROP_A, self.rt, "101")

	def test_a_cancellation_credit_note_voucher_is_marked(self):
		res = make_reservation(PROP_A, self.rt, d(3), d(5), status="Confirmed", room=self.room)
		frappe.db.set_value("Reservation", res.name, "advance_paid", 500)
		SEEN_VOUCHERS.clear()
		hooks = {"Discount Voucher": {"validate": [f"{HERE}.remember_voucher"]}}
		with patch.object(frappe, "get_doc_hooks", lambda: hooks):
			out = api.cancel_reservation(res.name, waive_fee=1, issue_credit_note=1)
		self.assertTrue(out.get("voucher_code") or frappe.db.exists("Discount Voucher", {"property": PROP_A}))
		self.assertEqual(SEEN_VOUCHERS, [1])
