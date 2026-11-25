"""RED->GREEN tests for PMS P0 P40: Night Audit & Audit Pack GL Control Tie-Out.

Verifies the thin kamra/night_audit.py consumer against the exact
`hospitality_kamra.control_tieout` contract from `hospitality-gl-tieout@1`:
- ORDER, STEPS, and PACK_FIGURE_SECTIONS registration
- Inert info check when the tie-out bridge script is absent
- Balanced 5-bucket tie-out across UGX (0dp), USD (2dp), KWD (3dp)
- Blocking variance detection across UGX (1), USD (0.05), KWD (0.002)
- Fail-closed Unverified handling (explicit status + bridge PermissionError/exception
  with message_log cleanup) blocking open_blockers and refusing eod_acknowledge
- build_pack persistence and _strip_figures redaction for Front Desk users
  (sees_figures() == False)
"""

from __future__ import annotations

import copy
import json
import re
from types import SimpleNamespace
from unittest.mock import patch

from kamra.tests.rate_matrix_harness import Doc, RateMatrixHarness, ValidationError

FORBIDDEN_VENDOR_RE = re.compile(r"(?<![a-z0-9])(?:Kamra|Frappe|ERPNext|URY|Tridz|BuildSuite)(?![a-z0-9])", re.I)

FIVE_CONTROL_KEYS = (
	"guest_ledger",
	"guest_advance_received",
	"guest_ledger_clearing",
	"room_tax_revenue",
	"city_ledger_ar",
)


def _sample_tieout(
	*,
	property_name: str = "USD Hotel",
	company: str = "Demo Hotels LLC",
	currency: str = "USD",
	precision: int = 2,
	tolerance: float = 0.01,
	business_date: str = "2026-11-24",
	clearing_variance: float = 0.0,
) -> dict:
	clearing_status = "Balanced" if abs(clearing_variance) <= tolerance else "Variance"
	in_balance = clearing_status == "Balanced"
	controls = [
		{
			"key": "guest_ledger",
			"label": "Guest Ledger",
			"account": "Debtors - DH",
			"subledger_balance": 250.0,
			"gl_balance": 250.0,
			"ple_balance": 250.0,
			"variance": 0.0,
			"status": "Balanced",
		},
		{
			"key": "guest_advance_received",
			"label": "Guest Advance Received",
			"account": "Guest Advance Received - DH",
			"subledger_balance": 100.0,
			"gl_balance": 100.0,
			"ple_balance": 100.0,
			"variance": 0.0,
			"status": "Balanced",
		},
		{
			"key": "guest_ledger_clearing",
			"label": "Guest Ledger Clearing",
			"account": "Guest Ledger Clearing - DH",
			"subledger_balance": 50.0,
			"gl_balance": round(50.0 + clearing_variance, precision),
			"ple_balance": 0.0,
			"variance": clearing_variance,
			"status": clearing_status,
		},
		{
			"key": "room_tax_revenue",
			"label": "Room & Tax Revenue",
			"account": "",
			"subledger_balance": 200.0,
			"gl_balance": 200.0,
			"ple_balance": 0.0,
			"variance": 0.0,
			"status": "Balanced",
		},
		{
			"key": "city_ledger_ar",
			"label": "City Ledger AR",
			"account": "Debtors - DH",
			"subledger_balance": 500.0,
			"gl_balance": 500.0,
			"ple_balance": 500.0,
			"variance": 0.0,
			"status": "Balanced",
		},
	]
	return {
		"ok": True,
		"status": "Balanced" if in_balance else "Variance",
		"in_balance": in_balance,
		"property": property_name,
		"company": company,
		"currency": currency,
		"precision": precision,
		"tolerance": tolerance,
		"business_date": business_date,
		"total_variance": abs(clearing_variance) if not in_balance else 0.0,
		"controls": controls,
	}


class TestP40NightAuditGlTieout(RateMatrixHarness):
	modules = (
		"kamra.authz",
		"kamra.ledger",
		"kamra.settings",
		"kamra.night_audit",
		"kamra.folio",
	)

	def extra_modules(self):
		self.can_see_figures = True
		self.logged_actions: list[tuple] = []
		dashboards_mod = SimpleNamespace(sees_figures=lambda: self.can_see_figures)
		crs_mod = SimpleNamespace(assert_property_access=lambda prop: None)
		savings_mod = SimpleNamespace(
			log_action=lambda *args, **kwargs: self.logged_actions.append((args, kwargs))
		)
		reports_mod = SimpleNamespace(
			PROJECTED_KEYS=set(),
			_day_stats=lambda prop, bd, total_rooms: {"rooms_sold": 1, "occupancy_pct": 50.0},
			manager_flash=lambda prop, bd: {"today": {}, "mtd": {}, "movement": {}, "collections": {"modes": []}},
		)
		audit_report_mod = SimpleNamespace(build=lambda prop, bd: {"periods": [], "balances": []})
		return {
			"kamra.dashboards": dashboards_mod,
			"kamra.crs": crs_mod,
			"kamra.savings": savings_mod,
			"kamra.reports": reports_mod,
			"kamra.audit_report": audit_report_mod,
		}

	def setUp(self):
		self.tieout_handler = lambda form: None
		super().setUp()
		self.roles = ["Finance", "Hotel Admin", "Night Auditor"]
		self.authz.restricted_properties = lambda user=None: None
		self.frappe.local.message_log = []
		self.frappe.db.count = lambda doctype, filters=None: 10
		self.frappe.cache = lambda: SimpleNamespace(hget=lambda *_: None, hset=lambda *_: None)
		self.add("Property", "UGX Hotel", property_name="UGX Hotel", currency="UGX", business_date="2026-11-24")
		self.add("Property", "USD Hotel", property_name="USD Hotel", currency="USD", business_date="2026-11-24")
		self.add("Property", "KWD Hotel", property_name="KWD Hotel", currency="KWD", business_date="2026-11-24")

	def bridge_call(self, api_method, *, response_types=None, **form):
		self.bridge_calls.append((api_method, dict(form)))
		if api_method == "hospitality_kamra.control_tieout":
			return self.tieout_handler(form)
		if api_method in (
			"hospitality_kamra.erp_books",
			"hospitality_kamra.erp_receivables",
			"hospitality_kamra.open_outlet_checks",
		):
			return None
		return super().bridge_call(api_method, response_types=response_types, **form)

	def set_override_policy(self, enabled):
		self.records["Property Setting"] = [
			Doc(property="USD Hotel", setting_key="allow_na_gl_variance_override", value=str(enabled)),
		]

	def live_gl_checks(self, property, bd, *_):
		return [self.night_audit.check_gl_control_tieout(property, bd)]

	def test_eod_gl_variance_override_requires_policy_and_role(self):
		self.tieout_handler = lambda _: _sample_tieout(clearing_variance=0.05)
		reason = "Controller approved pending reconciliation"
		for role in ("Night Auditor", "Kamra Agent", "Front Desk", "Finance", "Hotel Admin"):
			for enabled in (0, 1):
				with self.subTest(role=role, enabled=enabled):
					self.roles = [role]
					self.set_override_policy(enabled)
					allowed = enabled == 1 and role in ("Finance", "Hotel Admin")
					draft = self.add("Night Audit Run", "DRAFT", acknowledgements=None, save=lambda **_: None)
					with (
						patch.object(self.night_audit, "_draft", return_value=draft.name),
						patch.object(self.night_audit, "run_checks", side_effect=self.live_gl_checks),
					):
						if allowed:
							result = self.night_audit.eod_acknowledge("USD Hotel", "gl_control_tieout", reason)
							self.assertEqual(result["acknowledgement"]["note"], reason)
							self.assertEqual(json.loads(draft.acknowledgements), result["acknowledgements"])
						else:
							with self.assertRaises(self.frappe.PermissionError):
								self.night_audit.eod_acknowledge("USD Hotel", "gl_control_tieout", reason)
							self.assertIsNone(draft.acknowledgements)
					check = self.live_gl_checks("USD Hotel", "2026-11-24")[0]
					self.assertIs(check.get("can_override_gl_variance"), allowed)

	def test_manual_close_revalidates_drafted_and_inline_gl_overrides(self):
		self.tieout_handler = lambda _: _sample_tieout(clearing_variance=0.05)
		key = "gl_control_tieout"
		reason = "Controller approved pending reconciliation"
		cases = (
			(0, "Finance", True),
			(1, "Night Auditor", True),
			(0, "Hotel Admin", False),
			(1, "Kamra Agent", False),
		)
		for enabled, role, drafted in cases:
			with self.subTest(enabled=enabled, role=role, drafted=drafted):
				self.set_override_policy(enabled)
				self.roles = [role]
				draft = self.add("Night Audit Run", "DRAFT", acknowledgements=json.dumps([
					{"key": key, "by": self.frappe.session.user, "note": reason},
				] if drafted else []))

				def get_value(doctype, name, *args, **kwargs):
					if doctype == "Night Audit Run" and isinstance(name, dict) and name["status"] == "Draft":
						return draft.name
					return self.get_value(doctype, name, *args, **kwargs)

				with (
					patch.object(self.frappe.db, "get_value", side_effect=get_value),
					patch.object(self.night_audit, "run_checks", side_effect=self.live_gl_checks),
					patch.object(self.night_audit, "auto_extend_departures"),
					patch.object(self.folio, "audit_clock"),
					patch.object(self.folio, "nowdate", return_value="2026-11-24"),
					patch.object(self.frappe, "get_all", wraps=self.frappe.get_all) as get_all,
				):
					# Stop at the first posting query if the GL guard fails to block.
					def stop_before_posting(doctype, *args, **kwargs):
						if doctype == "Reservation":
							self.fail("Night audit reached posting with an unauthorized GL override")
						return self.get_all(doctype, *args, **kwargs)

					get_all.side_effect = stop_before_posting
					with self.assertRaisesRegex((ValidationError, self.frappe.PermissionError), "GL control tie-out"):
						self.folio.run_night_audit("USD Hotel", mode="manual",
							acknowledge=[] if drafted else [key], note=reason)

	def test_order_steps_and_pack_figure_sections_include_gl_control_tieout(self):
		self.assertIn("gl_control_tieout", self.night_audit.ORDER)
		folios_step = next(checks for key, _title, checks in self.night_audit.STEPS if key == "folios")
		self.assertIn("gl_control_tieout", folios_step)
		self.assertIn("gl_control_tieout", self.night_audit.PACK_FIGURE_SECTIONS)
		self.assertTrue(callable(getattr(self.night_audit, "check_gl_control_tieout", None)))

	def test_check_gl_control_tieout_is_inert_when_bridge_absent(self):
		self.tieout_handler = lambda form: None
		check = self.night_audit.check_gl_control_tieout("USD Hotel", "2026-11-24")
		self.assertEqual(check["key"], "gl_control_tieout")
		self.assertEqual(check["severity"], "info")
		self.assertEqual(check["count"], 0)
		self.assertEqual(check["items"], [])
		self.assertFalse(check.get("unverified"))
		self.assertIsNone(check.get("summary"))
		self.assertEqual(self.night_audit.open_blockers([check]), [])
		self.assertIn(
			("hospitality_kamra.control_tieout", {"property": "USD Hotel", "business_date": "2026-11-24"}),
			self.bridge_calls,
		)
		self.assertNotRegex(check["title"], FORBIDDEN_VENDOR_RE)
		self.assertNotRegex(check["action"], FORBIDDEN_VENDOR_RE)

	def test_check_gl_control_tieout_balanced_across_ugx_usd_kwd(self):
		cases = (
			("UGX Hotel", "UGX", 0, 0.0),
			("USD Hotel", "USD", 2, 0.01),
			("KWD Hotel", "KWD", 3, 0.001),
		)
		for prop, currency, precision, tolerance in cases:
			with self.subTest(currency=currency):
				payload = _sample_tieout(
					property_name=prop,
					currency=currency,
					precision=precision,
					tolerance=tolerance,
					clearing_variance=0.0,
				)
				self.tieout_handler = lambda _form, p=payload: p
				check = self.night_audit.check_gl_control_tieout(prop, "2026-11-24")
				self.assertEqual(check["key"], "gl_control_tieout")
				self.assertEqual(check["severity"], "block")
				self.assertEqual(check["count"], 0)
				self.assertEqual(check["items"], [])
				self.assertFalse(check.get("unverified"))
				self.assertEqual(self.night_audit.open_blockers([check]), [])
				self.assertIsInstance(check.get("summary"), dict)
				self.assertTrue(check["summary"]["in_balance"])
				self.assertEqual(check["summary"]["status"], "Balanced")
				self.assertEqual(
					tuple(c["key"] for c in check["summary"]["controls"]),
					FIVE_CONTROL_KEYS,
				)

	def test_check_gl_control_tieout_variance_blocks_night_audit_ugx_usd_kwd(self):
		cases = (
			("UGX Hotel", "UGX", 0, 0.0, 1.0),
			("USD Hotel", "USD", 2, 0.01, 0.05),
			("KWD Hotel", "KWD", 3, 0.001, 0.002),
		)
		for prop, currency, precision, tolerance, var_amt in cases:
			with self.subTest(currency=currency):
				payload = _sample_tieout(
					property_name=prop,
					currency=currency,
					precision=precision,
					tolerance=tolerance,
					clearing_variance=var_amt,
				)
				self.tieout_handler = lambda _form, p=payload: p
				check = self.night_audit.check_gl_control_tieout(prop, "2026-11-24")
				self.assertEqual(check["key"], "gl_control_tieout")
				self.assertEqual(check["severity"], "block")
				self.assertEqual(check["count"], 1)
				self.assertFalse(check.get("unverified"))
				self.assertEqual(check["items"][0]["key"], "guest_ledger_clearing")
				self.assertAlmostEqual(check["items"][0]["variance"], var_amt, places=precision)
				self.assertEqual(check["summary"]["status"], "Variance")
				self.assertFalse(check["summary"]["in_balance"])
				blockers = self.night_audit.open_blockers([check])
				self.assertEqual([b["key"] for b in blockers], ["gl_control_tieout"])
				self.assertNotRegex(check["title"], FORBIDDEN_VENDOR_RE)
				self.assertNotRegex(check["action"], FORBIDDEN_VENDOR_RE)

	def test_check_gl_control_tieout_unverified_and_bridge_error_fail_closed(self):
		# 1. Explicit Unverified payload with empty controls[] must still block!
		unverified_payload = {
			"ok": False,
			"status": "Unverified",
			"in_balance": False,
			"error_code": "HSP-TIEOUT-UNVERIFIED",
			"reason": "Required control accounts are not configured on the company.",
			"property": "USD Hotel",
			"company": "Demo Hotels LLC",
			"currency": "USD",
			"precision": 2,
			"tolerance": 0.01,
			"business_date": "2026-11-24",
			"total_variance": 0.0,
			"controls": [],
		}
		self.tieout_handler = lambda _form: unverified_payload
		check = self.night_audit.check_gl_control_tieout("USD Hotel", "2026-11-24")
		self.assertEqual(check["severity"], "block")
		self.assertTrue(check.get("unverified"))
		self.assertEqual(check["count"], 0)
		self.assertEqual([b["key"] for b in self.night_audit.open_blockers([check])], ["gl_control_tieout"])
		self.assertEqual(check["summary"]["status"], "Unverified")

		# 2. Bridge raises PermissionError (e.g. Front Desk caller) -> fail closed & clean message_log
		self.frappe.local.message_log = ["keep-me"]

		def raise_perm(_form):
			self.frappe.local.message_log.append("[HSP-TIEOUT-PERM-001]: denied")
			raise self.frappe.PermissionError("[HSP-TIEOUT-PERM-001]: denied")

		self.tieout_handler = raise_perm
		perm_check = self.night_audit.check_gl_control_tieout("USD Hotel", "2026-11-24")
		self.assertEqual(perm_check["severity"], "block")
		self.assertTrue(perm_check.get("unverified"))
		self.assertEqual([b["key"] for b in self.night_audit.open_blockers([perm_check])], ["gl_control_tieout"])
		self.assertEqual(self.frappe.local.message_log, ["keep-me"])
		self.assertNotRegex(perm_check["action"], FORBIDDEN_VENDOR_RE)

	def test_eod_acknowledge_refuses_unverified_tieout_and_short_variance_note(self):
		self.set_override_policy(1)
		# Unverified tie-out must never be overridden in eod_acknowledge
		unverified_check = {
			"key": "gl_control_tieout",
			"title": "Accounting GL control tie-out",
			"severity": "block",
			"count": 0,
			"items": [],
			"unverified": True,
			"action": "Configure control accounts.",
		}
		with patch.object(self.night_audit, "run_checks", return_value=[unverified_check]):
			with self.assertRaisesRegex(ValidationError, "unverified"):
				self.night_audit.eod_acknowledge(
					"USD Hotel",
					"gl_control_tieout",
					"Attempting to override unverified control tie-out",
				)

		# Variance tie-out requires explanatory note of at least 10 chars
		variance_check = {
			"key": "gl_control_tieout",
			"title": "Accounting GL control tie-out",
			"severity": "block",
			"count": 1,
			"items": [{"key": "guest_ledger_clearing", "variance": 0.05}],
			"unverified": False,
			"action": "Reconcile variance.",
		}
		with patch.object(self.night_audit, "run_checks", return_value=[variance_check]):
			with self.assertRaisesRegex(ValidationError, "10"):
				self.night_audit.eod_acknowledge("USD Hotel", "gl_control_tieout", "too short")

	def test_build_pack_persists_gl_control_tieout_and_strip_figures_redacts_for_front_desk(self):
		kwd_payload = _sample_tieout(
			property_name="KWD Hotel",
			currency="KWD",
			precision=3,
			tolerance=0.001,
			clearing_variance=0.002,
		)
		self.tieout_handler = lambda _form: kwd_payload
		gl_check = self.night_audit.check_gl_control_tieout("KWD Hotel", "2026-11-24")

		with (
			patch.object(self.night_audit, "_property_currency_meta", return_value=("KWD", "KD ")),
			patch.object(self.night_audit, "_in_house", return_value=[]),
			patch.object(self.night_audit, "_movement", return_value=[]),
			patch.object(self.night_audit, "check_high_balance", return_value={"key": "high_balance", "count": 0}),
			patch.object(self.night_audit, "check_credit_limit", return_value={"key": "credit_limit", "count": 0}),
			patch.object(self.night_audit, "check_overdue_ar", return_value={"key": "overdue_ar", "count": 0}),
			patch.object(self.night_audit, "check_pos_room_postings", return_value={"key": "pos_room_postings", "count": 0}),
			patch.object(self.night_audit, "check_rate_variance", return_value={"key": "rate_variance", "count": 0}),
			patch.object(self.night_audit, "_cashier_sessions", return_value=[]),
			patch.object(self.night_audit, "journal_by_cashier", return_value={"lines": [], "subtotals": []}),
			patch.object(self.night_audit, "_deposit_ledger", return_value={"items": [], "total": 0}),
			patch.object(self.night_audit, "_cancellations", return_value=[]),
			patch.object(self.night_audit, "no_show_credits", return_value=[]),
			patch.object(self.ledger, "journal_by_transaction_code", return_value={"rows": []}),
			patch.object(self.ledger, "ledger_balances", return_value={"ledgers": []}),
			patch.object(self.ledger, "city_ledger_aging", return_value={"accounts": []}),
		):
			pack = self.night_audit.build_pack(
				"KWD Hotel",
				"2026-11-24",
				checks=[gl_check],
				acknowledgements=[],
				summary={"notes": "Night close"},
				no_shows=[],
				held_late=[],
			)

		self.assertIn("gl_control_tieout", pack)
		self.assertEqual(pack["gl_control_tieout"]["currency"], "KWD")
		self.assertEqual(pack["gl_control_tieout"]["precision"], 3)
		self.assertEqual(len(pack["gl_control_tieout"]["controls"]), 5)

		# Finance / Night Auditor (sees_figures=True): keeps full tie-out section
		self.can_see_figures = True
		finance_pack = self.night_audit._strip_figures(copy.deepcopy(pack))
		self.assertIn("gl_control_tieout", finance_pack)
		self.assertNotIn("figures_hidden", finance_pack)

		# Front Desk (sees_figures=False): strips pack["gl_control_tieout"] AND redacts precheck figures!
		self.can_see_figures = False
		desk_pack = self.night_audit._strip_figures(copy.deepcopy(pack))
		self.assertTrue(desk_pack["figures_hidden"])
		self.assertNotIn("gl_control_tieout", desk_pack)
		precheck_gl = next(c for c in desk_pack["prechecks"] if c["key"] == "gl_control_tieout")
		self.assertIsNone(precheck_gl.get("summary"))
		for item in precheck_gl.get("items") or []:
			for money_key in ("subledger_balance", "gl_balance", "ple_balance", "variance"):
				self.assertNotIn(money_key, item)
