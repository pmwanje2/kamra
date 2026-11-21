"""A property that does not use cashier tills (use_cashier_tills off).

open_session refuses there with a plain message, so no till opens by
mistake. An empty till that is already open (opened before tills were
switched off) only warns at the night audit; a till that took money still
blocks it, so money is never dropped from the count.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from kamra import cashier, settings
from kamra import folio as folio_mod
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	YESTERDAY,
	AuditTestCase,
	make_property,
)
from kamra.tests.test_billing_approvals_split import set_setting
from kamra.tests.test_night_audit import at_time


class CashierTillsOffTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY)
		make_property(PROP_A, business_date=YESTERDAY)
		frappe.db.delete("Property Setting", {"property": PROP_A})
		frappe.db.delete("Cashier Session", {"property": PROP_A})
		self._stack.enter_context(patch.object(
			folio_mod, "audit_clock", return_value=at_time(YESTERDAY, "23:00")))
		self._stack.enter_context(patch("kamra.night_audit.alert_staff"))
		settings.clear_cache(PROP_A)
		self.addCleanup(settings.clear_cache, PROP_A)

	def tills(self, on: bool):
		set_setting(PROP_A, "use_cashier_tills", 1 if on else 0)

	def open_till_then_switch_off(self) -> str:
		"""The live case: a till opened, then tills switched off."""
		self.tills(True)
		session = cashier.open_session(PROP_A, opening_float=2000)["name"]
		self.tills(False)
		return session

	def open_tills_check(self) -> dict:
		from kamra import night_audit
		checks = night_audit.night_audit_precheck(PROP_A)["checks"]
		return next(c for c in checks if c["key"] == "open_tills")

	def run_manual(self, **kw):
		from kamra import api
		return api.run_night_audit(PROP_A, **kw)

	def test_open_session_refuses_when_tills_are_off(self):
		self.tills(False)
		with self.assertRaisesRegex(frappe.ValidationError, "tills are switched off"):
			cashier.open_session(PROP_A, opening_float=2000)
		self.assertFalse(frappe.get_all("Cashier Session",
		                                filters={"property": PROP_A}))

	def test_open_session_still_opens_when_tills_are_on(self):
		self.tills(True)
		out = cashier.open_session(PROP_A, opening_float=500)
		self.assertEqual(out["status"], "Open")

	def test_empty_till_on_a_tills_off_property_only_warns(self):
		session = self.open_till_then_switch_off()
		c = self.open_tills_check()
		self.assertEqual((c["severity"], c["count"]), ("warn", 1))
		self.assertEqual(c["items"][0]["session"], session)
		self.assertEqual(c["items"][0]["transactions"], 0)
		out = self.run_manual()
		self.assertEqual(out["status"], "Completed")
		self.assertEqual(str(frappe.db.get_value("Property", PROP_A, "business_date")),
		                 TODAY)

	def test_till_with_money_on_a_tills_off_property_still_blocks(self):
		self.tills(True)
		session = cashier.open_session(PROP_A, opening_float=2000)["name"]
		cashier.record_cashier_txn(PROP_A, "Payment", "Cash", 300, session=session)
		self.tills(False)
		c = self.open_tills_check()
		self.assertEqual((c["severity"], c["count"]), ("block", 1))
		self.assertEqual(c["items"][0]["transactions"], 1)
		with self.assertRaises(frappe.ValidationError):
			self.run_manual()
		self.assertEqual(str(frappe.db.get_value("Property", PROP_A, "business_date")),
		                 YESTERDAY)

	def test_empty_till_still_blocks_when_tills_are_on(self):
		self.tills(True)
		cashier.open_session(PROP_A, opening_float=2000)
		c = self.open_tills_check()
		self.assertEqual((c["severity"], c["count"]), ("block", 1))
