"""Exercise the real health endpoints and property guard without a bench.

Only Frappe storage, the scheduler, and the existing audit/channel readers
are doubled; no test connects to a site or an external service.
"""

import importlib.util
import json
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from kamra import __version__


class Row(dict):
	__getattr__ = dict.__getitem__


def module(name, **attrs):
	result = ModuleType(name)
	vars(result).update(attrs)
	return result


def load(name):
	path = Path(__file__).resolve().parents[1] / f"{name}.py"
	spec = importlib.util.spec_from_file_location(f"kamra.{name}", path)
	result = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(result)
	return result


class TestHealthWhiteLabel(unittest.TestCase):
	def setUp(self):
		self.roles = ["Hotel Admin"]
		self.permissions = [{"doc": "North"}]
		self.properties = [
			{"name": "North", "property_name": "North Inn", "timezone": "Africa/Kampala",
			 "business_date": date(2026, 11, 21), "disabled": 0},
			{"name": "South", "property_name": "South Inn", "timezone": "UTC",
			 "business_date": date(2026, 11, 18), "disabled": 0},
		]
		self.runs = [{"property": "North", "business_date": date(2026, 11, 20),
		              "status": "Closed", "finished_at": datetime(2026, 11, 21, 1, 5),
		              "message": "Private failure detail from frappe at https://private.test"}]
		self.connections = [{"name": "Private channel at South", "property": "South", "active": 1}]
		self.frappe = module(
			"frappe", __version__="16.123.45", conf={"kamra_white_label": 1,
			"kamra_product_name": "Maximus Hotel", "kamra_strict_property_scope": 1},
			local=SimpleNamespace(site="private-host.test"), session=SimpleNamespace(user="tester"),
			cache=Mock(), defaults=Mock(), db=Mock(), get_site_path=lambda: "/",
			get_roles=lambda *args: self.roles, whitelist=lambda **kwargs: lambda fn: fn,
			get_installed_apps=Mock(return_value=["kamra", "frappe", "erpnext", "hrms", "payments"]),
			get_all=self.read, get_list=self.read,
		)
		self.frappe.cache.get_value.return_value = {"ok": True, "tag": "v99.1.2"}
		self.frappe.defaults.get_user_default.return_value = None
		self.frappe.db.get_default.return_value = None
		self.frappe.db.get_single_value.side_effect = lambda _, key: {"time_zone": "UTC", "enable_scheduler": 1}[key]
		self.frappe.db.get_value.side_effect = self.get_value
		self.scheduler_inactive = Mock(return_value=False)
		self.next_due = Mock(return_value="2026-11-22 02:00:00")
		self.outbox = Mock(return_value={"outbox": True, "pending": 0, "failed": 2, "paused": {},
		                                "last_push_status": "ERPNext https://private.test"})
		utils = module(
			"frappe.utils", cint=lambda val: int(val or 0), get_datetime=datetime.fromisoformat,
			add_to_date=Mock(), now_datetime=lambda: datetime(2026, 11, 21, 22),
			get_system_timezone=lambda: "UTC",
		)
		self.frappe.utils = utils
		modules = {
			"frappe": self.frappe,
			"frappe.utils": utils,
			"frappe.utils.scheduler": module("frappe.utils.scheduler", is_scheduler_inactive=self.scheduler_inactive),
			"frappe.core.doctype.user_permission.user_permission": module(
				"frappe.core.doctype.user_permission.user_permission",
				get_user_permissions=lambda _: {"Property": self.permissions},
			),
			"kamra.night_audit": module("kamra.night_audit", next_auto_run=self.next_due),
			"kamra.channel_ari": module("kamra.channel_ari", outbox_status=self.outbox),
		}
		self.enterContext(patch.dict(sys.modules, modules))
		self.enterContext(patch.dict(sys.modules, {"kamra.authz": load("authz"), "kamra.branding": load("branding")}))
		self.health = load("health")
		self.urlopen = self.enterContext(patch.object(self.health, "urlopen", side_effect=AssertionError("outbound HTTP")))
		self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("outbound socket")))

	def read(self, doctype, filters=None, fields=None, pluck=None, **kwargs):
		rows = {"Property": self.properties, "Night Audit Log": self.runs,
		        "Channel Manager Connection": self.connections}[doctype]
		for key, wanted in (filters or {}).items():
			if isinstance(wanted, (list, tuple)):
				op, values = wanted
				self.assertIn(op, ("in", "!="))
				rows = [r for r in rows if (r[key] in values if op == "in" else r[key] != values)]
			else:
				rows = [r for r in rows if r[key] == wanted]
		limit = kwargs.get("limit", kwargs.get("limit_page_length"))
		return [r[pluck] if pluck else Row({field: r[field] for field in fields or ["name"]}) for r in rows[:limit]]

	def get_value(self, doctype, filters, fields, **kwargs):
		if isinstance(filters, str):
			filters = {"name": filters}
		if isinstance(fields, str):
			rows = self.read(doctype, filters, pluck=fields, limit=1)
		else:
			rows = self.read(doctype, filters, fields=fields, limit=1)
		return rows[0] if rows else None

	def checks(self, payload):
		return {row["id"]: row for row in payload["checks"]}

	def check(self, payload, name):
		checks = self.checks(payload)
		self.assertIn(name, checks)
		return checks[name]

	def test_health_has_only_the_operational_contract(self):
		# A warm upstream cache must never be read or disclosed either.
		payload = self.health.system_health()
		self.assertEqual(set(payload), {"product", "overall", "checked_at", "properties", "checks"})
		self.assertEqual(payload["product"], "Maximus Hotel")
		self.assertEqual(payload["overall"], "passed")
		self.assertEqual(payload["checked_at"], "2026-11-21T22:00:00+00:00")
		self.assertEqual(payload["properties"], [{
			"property": "North Inn", "status": "passed", "business_date": "2026-11-21",
			"calendar_date": "2026-11-22", "night_audit": {
				"last_business_date": "2026-11-20", "status": "Completed",
				"completed_at": "2026-11-21T04:05:00+03:00", "next_due": "2026-11-22T02:00:00+03:00",
			},
		}])
		self.assertEqual(set(self.checks(payload)), {"service", "background", "night_audit", "property_clock"})
		for row in payload["checks"]:
			self.assertEqual(set(row), {"id", "title", "status", "detail"})
		body = json.dumps(payload).lower()
		for token in (__version__, self.frappe.__version__, self.frappe.local.site, "github", "kamra",
		              "frappe", "erpnext", "hrms", "payments", " gb", "/app", "disk", "python",
		              "operating system", "https://", "south"):
			self.assertNotIn(token, body)
		self.frappe.cache.get_value.assert_not_called()
		self.frappe.get_installed_apps.assert_not_called()

	def test_health_uses_shared_branding_settings(self):
		self.frappe.conf.update(kamra_white_label=" TRUE ", kamra_product_name="  Independent Hotel  ")
		payload = self.health.system_health()
		self.assertIn("product", payload)
		self.assertEqual(payload["product"], "Independent Hotel")
		self.frappe.conf["kamra_product_name"] = " "
		self.assertEqual(self.health.system_health()["product"], "Hotel PMS")
		self.assertEqual(self.health.update_nag(), {"available": False})
		self.urlopen.assert_not_called()

	def test_health_refresh_never_fetches_github_or_measures_disk(self):
		self.frappe.cache.get_value.return_value = None
		with patch.object(self.health.shutil, "disk_usage", side_effect=AssertionError("disk probe")):
			self.health.system_health(refresh=1)
		self.urlopen.assert_not_called()
		self.frappe.cache.delete_value.assert_not_called()

	def test_update_nag_is_disabled_without_fetch(self):
		self.frappe.cache.get_value.return_value = None
		self.assertEqual(self.health.update_nag(), {"available": False})
		self.urlopen.assert_not_called()
		self.frappe.cache.get_value.assert_not_called()

	def test_poll_skips_github_without_touching_release_cache(self):
		self.frappe.cache.get_value.return_value = None
		self.health.poll_github_releases()
		self.urlopen.assert_not_called()
		self.frappe.cache.delete_value.assert_not_called()

	def test_dismiss_does_not_write_a_user_default(self):
		self.health.dismiss_update_nag(tag="v99.1.2")
		self.frappe.defaults.set_user_default.assert_not_called()

	def test_stuck_dates_and_failed_runs_need_attention(self):
		for business_date, last_status in [(date(2026, 11, 20), "Closed"), (date(2026, 11, 21), "Failed")]:
			with self.subTest(business_date=business_date, last_status=last_status):
				self.properties[0]["business_date"] = business_date
				self.runs[0]["status"] = last_status
				payload = self.health.system_health()
				self.assertEqual(self.check(payload, "night_audit")["status"], "attention")
				self.assertEqual(payload["properties"][0]["status"], "attention")
				self.assertEqual(payload["overall"], "attention")

	def test_property_and_channels_follow_scope_for_both_roles(self):
		for role in ("Hotel Admin", "System Manager"):
			self.roles = [role]
			payload = self.health.system_health()
			self.assertIn("properties", payload)
			self.assertEqual([p["property"] for p in payload["properties"]], ["North Inn"])
			self.assertNotIn("channels", self.checks(payload))
		self.outbox.assert_not_called()
		self.roles = ["Hotel Admin"]
		self.permissions = []  # strict scope: no explicit grant means no property rows
		self.assertEqual(self.health.system_health()["properties"], [])

	def test_channel_errors_are_summarized_without_raw_details(self):
		self.connections[0]["property"] = "North"
		payload = self.health.system_health()
		self.assertEqual(self.check(payload, "channels")["status"], "attention")
		self.assertNotRegex(json.dumps(payload), r"Private channel|ERPNext|https?://")
		self.outbox.assert_called_once_with(self.connections[0]["name"])

	def test_paused_jobs_show_support_message_with_no_desk_link(self):
		self.scheduler_inactive.return_value = True
		payload = self.health.system_health()
		self.assertEqual(self.check(payload, "background"), {
			"id": "background", "title": "Background jobs", "status": "attention",
			"detail": "Background jobs are paused. Contact Maximus support.",
		})
		self.assertIsNone(payload["properties"][0]["night_audit"]["next_due"])

	def test_service_failure_does_not_echo_backend_errors(self):
		self.frappe.db.sql.side_effect = RuntimeError("private-host.test Frappe traceback")
		payload = self.health.system_health()
		self.assertEqual(self.check(payload, "service")["status"], "failed")
		self.assertEqual(payload["overall"], "failed")
		self.assertNotRegex(json.dumps(payload), r"private-host|Frappe|traceback")

	def test_legacy_health_contract_is_retained_when_flag_is_unset(self):
		self.frappe.conf.pop("kamra_white_label")
		self.frappe.cache.get_value.return_value = {"ok": True, "tag": "v99.1.2"}
		payload = self.health.system_health()
		self.assertEqual(set(payload), {"overall", "summary", "installed", "latest", "upgrade", "checks"})
		self.assertEqual(payload["installed"]["site"], self.frappe.local.site)
		self.assertEqual(set(self.checks(payload)), {"version", "frappe", "apps", "database", "scheduler", "disk", "timezone"})
