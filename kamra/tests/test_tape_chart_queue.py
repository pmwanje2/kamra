"""Tests for kamra.tape_queue (P22 Tape Chart unassigned queue, room metadata,
HK-prioritized allocation, quick-assign, and strict multi-property isolation).

Runs both standalone (`python3 -m unittest kamra/tests/test_tape_chart_queue.py`)
and inside a live Frappe v16 bench (`bench run-tests --module kamra.tests.test_tape_chart_queue`).
"""

from __future__ import annotations

import ast
import datetime
import importlib
import re
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

KAMRA_ROOT = Path(__file__).resolve().parents[1]

try:
	import frappe
	from kamra.tests.audit_fixtures import (
		PROP_A,
		PROP_B,
		TODAY,
		AuditTestCase,
		d,
		ensure_user,
		make_property,
		make_room,
		make_room_type,
	)

	_HAS_FRAPPE = True
except ModuleNotFoundError:
	_HAS_FRAPPE = False


class _AttrDict(dict):
	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError as exc:
			raise AttributeError(key) from exc

	def __setattr__(self, key, value):
		self[key] = value


class _PermissionError(Exception):
	pass


class _ValidationError(Exception):
	pass


def _add_days(date_str: str, days: int) -> str:
	base = datetime.date.fromisoformat(str(date_str)[:10])
	return (base + datetime.timedelta(days=int(days))).isoformat()


def _date_diff(a: str, b: str) -> int:
	return (datetime.date.fromisoformat(str(a)[:10]) - datetime.date.fromisoformat(str(b)[:10])).days


def _getdate(val) -> datetime.date:
	if isinstance(val, datetime.date):
		return val
	return datetime.date.fromisoformat(str(val)[:10])


def _build_offline_stubs(allowed_properties=None, db_state=None):
	"""Lightweight Frappe + kamra stubs used ONLY when running outside bench."""
	state = db_state or {}
	frappe_mod = ModuleType("frappe")
	frappe_mod._ = lambda s: s
	frappe_mod._dict = _AttrDict
	frappe_mod.PermissionError = _PermissionError
	frappe_mod.ValidationError = _ValidationError
	frappe_mod.whitelist = lambda **kw: (lambda fn: fn)

	def _throw(msg, exc=None):
		raise (exc or _ValidationError)(msg)

	frappe_mod.throw = _throw

	perm_calls = []

	def _has_permission(doctype, ptype="read", doc=None, throw=False):
		perm_calls.append((doctype, ptype))
		if state.get("deny_permission") == (doctype, ptype):
			if throw:
				raise _PermissionError(f"No {ptype} permission for {doctype}")
			return False
		return True

	frappe_mod.has_permission = _has_permission
	frappe_mod._perm_calls = perm_calls

	sql_calls = []

	def _sql(query, params=None, as_dict=False):
		sql_calls.append((query, params))
		handler = state.get("sql")
		if handler:
			return handler(query, params or {}, as_dict=as_dict)
		return []

	def _get_all(doctype, filters=None, fields=None, order_by=None, pluck=None):
		handler = state.get("get_all")
		if handler:
			return handler(doctype, filters=filters, fields=fields, order_by=order_by, pluck=pluck)
		return []

	def _get_value(doctype, name, fieldname=None, as_dict=False):
		handler = state.get("get_value")
		if handler:
			return handler(doctype, name, fieldname=fieldname, as_dict=as_dict)
		return None

	frappe_mod.db = SimpleNamespace(sql=_sql, get_value=_get_value)
	frappe_mod.get_all = _get_all
	frappe_mod._sql_calls = sql_calls

	utils_mod = ModuleType("frappe.utils")
	utils_mod.add_days = _add_days
	utils_mod.date_diff = _date_diff
	utils_mod.getdate = _getdate
	utils_mod.nowdate = lambda: state.get("today", "2026-11-24")
	frappe_mod.utils = utils_mod

	authz_mod = ModuleType("kamra.authz")
	access_calls = []

	def _assert_property_access(prop):
		access_calls.append(prop)
		if allowed_properties is not None and prop not in allowed_properties:
			raise _PermissionError(f"You don't have access to {prop}.")

	authz_mod.assert_property_access = _assert_property_access
	authz_mod.require_roles = lambda *roles, **kw: (lambda fn: fn)
	authz_mod._access_calls = access_calls

	floors_mod = ModuleType("kamra.floors")
	floors_mod.floors_for = lambda prop: state.get("floors", [{"name": "1 - North Wing", "sort_order": 10}])

	alloc_mod = ModuleType("kamra.allocation")
	alloc_mod._as_int = lambda v: int(str(v).strip()) if str(v or "").strip().lstrip("-").isdigit() else None
	alloc_mod._room_pool = lambda prop: (
		state.get("room_pool", []),
		state.get("max_floor"),
		state.get("min_floor"),
	)

	def _score(prefs, vip, room, max_floor, min_floor):
		p = (prefs or "").lower()
		sc = 0.0
		why = []
		view = (room.get("room_view") or "").lower()
		if "sea" in p and "sea" in view:
			sc += 3.0
			why.append(f"{room['room_view']} view")
		if vip:
			sc += (alloc_mod._as_int(room.get("room_number")) or 0) / 100000.0
			why.append("VIP - best available")
		return sc, why

	alloc_mod._score = _score
	alloc_mod.apply_allocation = lambda prop, assignments: state["apply_allocation"](prop, assignments)

	api_mod = ModuleType("kamra.api")
	api_mod.available_rooms = lambda prop, rt, ci, co, group_booking=None, ready_now=0: state["available_rooms"](
		prop, rt, ci, co, group_booking=group_booking, ready_now=ready_now
	)

	return {
		"frappe": frappe_mod,
		"frappe.utils": utils_mod,
		"kamra.authz": authz_mod,
		"kamra.floors": floors_mod,
		"kamra.allocation": alloc_mod,
		"kamra.api": api_mod,
	}


class TestTapeQueueUnit(unittest.TestCase):
	"""Pure logic, static SQL hygiene, and offline contract tests."""

	def _load_module(self, allowed_properties=None, db_state=None):
		if _HAS_FRAPPE:
			import kamra.tape_queue as tq

			return tq, None
		stubs = _build_offline_stubs(allowed_properties=allowed_properties, db_state=db_state)
		sys.modules.pop("kamra.tape_queue", None)
		patcher = patch.dict(sys.modules, stubs)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.addCleanup(lambda: sys.modules.pop("kamra.tape_queue", None))
		tq = importlib.import_module("kamra.tape_queue")
		return tq, stubs

	def test_hk_readiness_weight_order_and_exclusions(self):
		tq, _ = self._load_module()
		self.assertGreater(tq.hk_readiness_weight("Inspected"), tq.hk_readiness_weight("Ready"))
		self.assertGreater(tq.hk_readiness_weight("Ready"), tq.hk_readiness_weight("Clean"))
		self.assertGreater(tq.hk_readiness_weight("Clean"), tq.hk_readiness_weight("Pickup"))
		self.assertGreater(tq.hk_readiness_weight("Pickup"), tq.hk_readiness_weight("Dirty"))
		self.assertEqual(tq.hk_readiness_weight("Inspected"), 100)
		self.assertEqual(tq.hk_readiness_weight("Ready"), 90)
		self.assertEqual(tq.hk_readiness_weight("Clean"), 50)
		self.assertEqual(tq.hk_readiness_weight("Pickup"), 25)
		self.assertEqual(tq.hk_readiness_weight("Dirty"), 0)

		for blocked in ("Out of Order", "Out of Service", "OO", "OS"):
			self.assertFalse(tq.is_assignable_hk_status(blocked), blocked)
		for allowed in ("Inspected", "Ready", "Clean", "Pickup", "Dirty"):
			self.assertTrue(tq.is_assignable_hk_status(allowed), allowed)

	def test_parse_wing_from_floor_or_room_number(self):
		tq, _ = self._load_module()
		self.assertEqual(tq._parse_wing("2 - North Wing", "201"), "North Wing")
		self.assertEqual(tq._parse_wing("East / Floor 3", "301"), "East")
		self.assertEqual(tq._parse_wing("1", "N-101"), "N")
		self.assertEqual(tq._parse_wing("2", "204"), "")

	def test_tape_queue_sql_is_semgrep_clean_and_white_label(self):
		path = KAMRA_ROOT / "tape_queue.py"
		self.assertTrue(path.is_file(), "kamra/tape_queue.py must exist")
		source = path.read_text(encoding="utf-8")
		tree = ast.parse(source)
		forbidden_vendor = re.compile(
			r"(?<![a-z0-9])(?:Kamra|Frappe|ERPNext|URY|HeyKoala|Tridz|BuildSuite)(?![a-z0-9])|₹|\u20b9",
			re.I,
		)
		for node in ast.walk(tree):
			if isinstance(node, ast.Call):
				fn_name = ast.unparse(node.func)
				if fn_name == "frappe.db.sql" and node.args:
					self.assertIsInstance(
						node.args[0],
						ast.Constant,
						f"Dynamic SQL forbidden in frappe.db.sql at line {node.lineno}",
					)
					self.assertIsInstance(node.args[0].value, str)
				if fn_name in {"_", "frappe._", "frappe.throw", "frappe.msgprint"} and node.args:
					for sub in ast.walk(node.args[0]):
						if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
							self.assertNotRegex(sub.value, forbidden_vendor)


if not _HAS_FRAPPE:

	class TestTapeQueueContractOffline(unittest.TestCase):
		"""Offline endpoint behavior tests when running outside bench."""

		def _load(self, allowed_properties=None, db_state=None):
			stubs = _build_offline_stubs(allowed_properties=allowed_properties, db_state=db_state)
			sys.modules.pop("kamra.tape_queue", None)
			patcher = patch.dict(sys.modules, stubs)
			patcher.start()
			self.addCleanup(patcher.stop)
			self.addCleanup(lambda: sys.modules.pop("kamra.tape_queue", None))
			return importlib.import_module("kamra.tape_queue"), stubs

		def test_unassigned_queue_filters_sorts_and_enforces_property_scope(self):
			rows = [
				_AttrDict(
					name="RES-2", guest="G-2", guest_name="Standard Guest", vip=0,
					room_type="DLX", room_type_name="Deluxe",
					check_in_date="2026-11-24", check_out_date="2026-11-26",
					is_day_use=0, adults=2, children=0, booking_type="Individual",
					source="Direct", company=None, travel_agent=None, group_booking=None,
					special_requests="", planned_check_in_time=None, planned_check_out_time=None,
					status="Confirmed", creation="2026-11-20 09:00:00",
				),
				_AttrDict(
					name="RES-1", guest="G-1", guest_name="VIP Guest", vip=1,
					room_type="DLX", room_type_name="Deluxe",
					check_in_date="2026-11-24", check_out_date="2026-11-27",
					is_day_use=0, adults=2, children=0, booking_type="Individual",
					source="Direct", company=None, travel_agent=None, group_booking=None,
					special_requests="High floor", planned_check_in_time="14:00:00",
					planned_check_out_time="11:00:00", status="Confirmed",
					creation="2026-11-20 10:00:00",
				),
			]
			tq, stubs = self._load(
				allowed_properties={"PROP-A"},
				db_state={"sql": lambda q, p, as_dict=False: list(rows)},
			)
			out = tq.unassigned_queue("PROP-A", "2026-11-24", 14)
			self.assertEqual(stubs["kamra.authz"]._access_calls, ["PROP-A"])
			self.assertIn(("Reservation", "read"), stubs["frappe"]._perm_calls)
			self.assertEqual(out["property"], "PROP-A")
			self.assertEqual([item["name"] for item in out["items"]], ["RES-1", "RES-2"])
			self.assertEqual(out["items"][0]["nights"], 3)

			with self.assertRaises(_PermissionError):
				tq.unassigned_queue("PROP-B", "2026-11-24", 14)

		def test_room_metadata_returns_floors_and_room_attributes(self):
			meta_rows = [
				_AttrDict(
					name="PROP-A-201", room_number="201", floor="2 - North Wing",
					room_type="DLX", room_type_name="Deluxe",
					housekeeping_status="Inspected", occupancy_status="Vacant",
					room_view="Sea", bed_type="King", smoking_allowed=0,
					air_conditioning="AC", kitchen=1, amenities="WiFi, Balcony, Safe",
				),
			]
			tq, stubs = self._load(
				allowed_properties={"PROP-A"},
				db_state={
					"floors": [{"name": "2 - North Wing", "sort_order": 20}],
					"sql": lambda q, p, as_dict=False: meta_rows,
				},
			)
			out = tq.room_metadata("PROP-A")
			self.assertEqual(out["floors"], [{"name": "2 - North Wing", "sort_order": 20}])
			r = out["rooms"]["PROP-A-201"]
			self.assertEqual(r["floor"], "2 - North Wing")
			self.assertEqual(r["wing"], "North Wing")
			self.assertEqual(r["room_view"], "Sea")
			self.assertEqual(r["bed_type"], "King")
			self.assertEqual(r["smoking_allowed"], 0)
			self.assertEqual(r["air_conditioning"], "AC")
			self.assertEqual(r["kitchen"], 1)
			self.assertEqual(r["amenities"], ["WiFi", "Balcony", "Safe"])

			with self.assertRaises(_PermissionError):
				tq.room_metadata("PROP-B")

		def test_suggest_queue_allocation_prefers_hk_readiness_and_excludes_oo_os(self):
			arrivals = [
				_AttrDict(
					name="RES-A1", guest="G-1", guest_name="Guest One", room_type="DLX",
					check_in_date="2026-11-24", check_out_date="2026-11-26",
					special_requests="", group_booking=None, is_day_use=0,
				),
				_AttrDict(
					name="RES-A2", guest="G-2", guest_name="Guest Two", room_type="DLX",
					check_in_date="2026-11-24", check_out_date="2026-11-26",
					special_requests="", group_booking=None, is_day_use=0,
				),
				_AttrDict(
					name="RES-A3", guest="G-3", guest_name="Guest Three", room_type="DLX",
					check_in_date="2026-11-24", check_out_date="2026-11-26",
					special_requests="", group_booking=None, is_day_use=0,
				),
				_AttrDict(
					name="RES-A4", guest="G-4", guest_name="Guest Four", room_type="DLX",
					check_in_date="2026-11-24", check_out_date="2026-11-26",
					special_requests="", group_booking=None, is_day_use=0,
				),
				_AttrDict(
					name="RES-A5", guest="G-5", guest_name="Guest Five", room_type="DLX",
					check_in_date="2026-11-24", check_out_date="2026-11-26",
					special_requests="", group_booking=None, is_day_use=0,
				),
				_AttrDict(
					name="RES-A6", guest="G-6", guest_name="Guest Six", room_type="DLX",
					check_in_date="2026-11-24", check_out_date="2026-11-26",
					special_requests="", group_booking=None, is_day_use=0,
				),
			]
			pool = [
				_AttrDict(name="P-101", room_number="101", floor="1", room_type="DLX", room_type_name="Deluxe", room_view="", bed_type="King"),
				_AttrDict(name="P-102", room_number="102", floor="1", room_type="DLX", room_type_name="Deluxe", room_view="", bed_type="King"),
				_AttrDict(name="P-103", room_number="103", floor="1", room_type="DLX", room_type_name="Deluxe", room_view="", bed_type="King"),
				_AttrDict(name="P-104", room_number="104", floor="1", room_type="DLX", room_type_name="Deluxe", room_view="", bed_type="King"),
				_AttrDict(name="P-105", room_number="105", floor="1", room_type="DLX", room_type_name="Deluxe", room_view="", bed_type="King"),
				_AttrDict(name="P-106", room_number="106", floor="1", room_type="DLX", room_type_name="Deluxe", room_view="", bed_type="King"),
				_AttrDict(name="P-107", room_number="107", floor="1", room_type="DLX", room_type_name="Deluxe", room_view="", bed_type="King"),
			]
			free = [
				{"name": "P-101", "room_number": "101", "housekeeping_status": "Dirty"},
				{"name": "P-102", "room_number": "102", "housekeeping_status": "Pickup"},
				{"name": "P-103", "room_number": "103", "housekeeping_status": "Clean"},
				{"name": "P-104", "room_number": "104", "housekeeping_status": "Ready"},
				{"name": "P-105", "room_number": "105", "housekeeping_status": "Inspected"},
				{"name": "P-106", "room_number": "106", "housekeeping_status": "Out of Service"},
				{"name": "P-107", "room_number": "107", "housekeeping_status": "Out of Order"},
			]

			def _get_all(doctype, filters=None, fields=None, order_by=None, pluck=None):
				if doctype == "Reservation":
					return arrivals
				if doctype == "Guest":
					return []
				if doctype == "Room":
					return [_AttrDict(r) for r in free]
				return []

			tq, _ = self._load(
				allowed_properties={"PROP-A"},
				db_state={
					"get_all": _get_all,
					"room_pool": pool,
					"available_rooms": lambda *a, **kw: free,
				},
			)
			out = tq.suggest_queue_allocation("PROP-A", "2026-11-24")
			picked = [(p["suggested_room"], p["housekeeping_status"]) for p in out["proposals"]]
			self.assertEqual(
				picked,
				[
					("P-105", "Inspected"),
					("P-104", "Ready"),
					("P-103", "Clean"),
					("P-102", "Pickup"),
					("P-101", "Dirty"),
				],
			)
			self.assertEqual([u["reservation"] for u in out["unfittable"]], ["RES-A6"])

		def test_quick_assign_room_refuses_oo_os_and_apply_skips(self):
			rooms = {
				"P-101": _AttrDict(property="PROP-A", room_type="DLX", housekeeping_status="Inspected"),
				"P-106": _AttrDict(property="PROP-A", room_type="DLX", housekeeping_status="Out of Service"),
				"P-107": _AttrDict(property="PROP-A", room_type="DLX", housekeeping_status="Out of Order"),
				"P-B01": _AttrDict(property="PROP-B", room_type="DLX", housekeeping_status="Inspected"),
			}
			res_docs = {
				"RES-1": _AttrDict(property="PROP-A", room_type="DLX", status="Confirmed", room=""),
			}
			apply_result = {"executed": [{"reservation": "RES-1", "room": "P-101"}], "skipped": []}

			def _get_value(doctype, name, fieldname=None, as_dict=False):
				if doctype == "Room":
					return rooms.get(name)
				if doctype == "Reservation":
					return res_docs.get(name)
				return None

			tq, _ = self._load(
				allowed_properties={"PROP-A"},
				db_state={
					"get_value": _get_value,
					"apply_allocation": lambda prop, assignments: apply_result,
				},
			)
			ok = tq.quick_assign_room("PROP-A", "RES-1", "P-101", "Desk drag")
			self.assertTrue(ok["ok"])
			self.assertEqual(ok["room"], "P-101")

			with self.assertRaises(_ValidationError):
				tq.quick_assign_room("PROP-A", "RES-1", "P-106")
			with self.assertRaises(_ValidationError):
				tq.quick_assign_room("PROP-A", "RES-1", "P-107")
			with self.assertRaises(_ValidationError):
				tq.quick_assign_room("PROP-A", "RES-1", "P-B01")

			apply_result = {
				"executed": [],
				"skipped": [{"reservation": "RES-1", "room": "P-101", "reason": "That room is no longer free or ready for these nights"}],
			}
			with self.assertRaisesRegex(_ValidationError, "no longer free"):
				tq.quick_assign_room("PROP-A", "RES-1", "P-101")


if _HAS_FRAPPE:

	class TestTapeQueueLiveIntegration(AuditTestCase):
		"""Zero-mock live MariaDB/Frappe integration tests for kamra.tape_queue."""

		properties = (PROP_A, PROP_B)

		def setUp(self):
			super().setUp()
			self.at(TODAY, "frappe.utils", "kamra.folio", "kamra.business_date", "kamra.api", "kamra.tape_queue")
			make_property(PROP_A, business_date=TODAY)
			make_property(PROP_B, business_date=TODAY)

			from kamra.settings import clear_cache, write_kv

			write_kv(PROP_A, "assign_room_on_booking", 0)
			write_kv(PROP_B, "assign_room_on_booking", 0)
			clear_cache(PROP_A)
			clear_cache(PROP_B)

			self.rt_dlx = make_room_type(
				PROP_A,
				"DLX",
				base_price=250,
				room_view="Sea",
				bed_type="King",
				smoking_allowed=0,
				air_conditioning="AC",
				kitchen=0,
				amenities="WiFi, Balcony",
			)
			self.rt_std = make_room_type(
				PROP_A,
				"STD",
				base_price=150,
				room_view="Garden",
				bed_type="Twin",
				smoking_allowed=1,
				air_conditioning="AC",
				kitchen=1,
				amenities="WiFi",
			)
			self.rt_b = make_room_type(PROP_B, "DLX", base_price=200)

			self.r_dirty = make_room(PROP_A, self.rt_dlx, "101")
			self.r_clean = make_room(PROP_A, self.rt_dlx, "102")
			self.r_ready = make_room(PROP_A, self.rt_dlx, "103")
			self.r_inspected = make_room(PROP_A, self.rt_dlx, "104")
			self.r_oo = make_room(PROP_A, self.rt_dlx, "105")
			self.r_os = make_room(PROP_A, self.rt_dlx, "106")
			self.r_std = make_room(PROP_A, self.rt_std, "201")
			self.r_b1 = make_room(PROP_B, self.rt_b, "301")

			frappe.db.set_value("Room", self.r_dirty, {"housekeeping_status": "Dirty", "floor": "1 - North Wing"})
			frappe.db.set_value("Room", self.r_clean, {"housekeeping_status": "Clean", "floor": "1 - North Wing"})
			frappe.db.set_value("Room", self.r_ready, {"housekeeping_status": "Ready", "floor": "1 - North Wing"})
			frappe.db.set_value("Room", self.r_inspected, {"housekeeping_status": "Inspected", "floor": "2 - South Wing"})
			frappe.db.set_value("Room", self.r_oo, {"housekeeping_status": "Out of Order", "floor": "2 - South Wing"})
			frappe.db.set_value("Room", self.r_os, {"housekeeping_status": "Out of Service", "floor": "2 - South Wing"})
			frappe.db.set_value("Room", self.r_std, {"housekeeping_status": "Inspected", "floor": "2 - South Wing"})
			frappe.db.set_value("Room", self.r_b1, {"housekeeping_status": "Inspected", "floor": "3"})

		def _book_unassigned(self, prop: str, rt: str, guest_name: str, phone: str, ci: str, co: str, vip: int = 0):
			from kamra import api

			out = api.create_booking(
				property=prop,
				room_type=rt,
				check_in_date=ci,
				check_out_date=co,
				guest_name=guest_name,
				phone=phone,
			)
			res_name = out["reservation"]
			if vip:
				guest = frappe.db.get_value("Reservation", res_name, "guest")
				frappe.db.set_value("Guest", guest, "vip", 1)
			return res_name

		def test_live_unassigned_queue_metadata_suggest_assign_and_isolation(self):
			from kamra import tape_queue

			res_std = self._book_unassigned(PROP_A, self.rt_dlx, "Regular Arrival", "+256700100001", TODAY, d(2), vip=0)
			res_vip = self._book_unassigned(PROP_A, self.rt_dlx, "VIP Arrival", "+256700100002", TODAY, d(2), vip=1)
			self._book_unassigned(PROP_B, self.rt_b, "Prop B Arrival", "+256700100003", TODAY, d(2), vip=1)

			queue = tape_queue.unassigned_queue(PROP_A, TODAY, 14)
			ids = [item["name"] for item in queue["items"]]
			self.assertEqual(ids, [res_vip, res_std])

			meta = tape_queue.room_metadata(PROP_A)
			self.assertIn(self.r_inspected, meta["rooms"])
			self.assertNotIn(self.r_b1, meta["rooms"])
			self.assertEqual(meta["rooms"][self.r_inspected]["wing"], "South Wing")
			self.assertEqual(meta["rooms"][self.r_inspected]["room_view"], "Sea")
			self.assertEqual(meta["rooms"][self.r_inspected]["bed_type"], "King")
			self.assertEqual(meta["rooms"][self.r_inspected]["amenities"], ["WiFi", "Balcony"])

			suggested = tape_queue.suggest_queue_allocation(PROP_A, TODAY)
			by_res = {p["reservation"]: p for p in suggested["proposals"]}
			self.assertEqual(by_res[res_vip]["suggested_room"], self.r_inspected)
			self.assertEqual(by_res[res_vip]["housekeeping_status"], "Inspected")
			self.assertEqual(by_res[res_std]["suggested_room"], self.r_ready)
			self.assertEqual(by_res[res_std]["housekeeping_status"], "Ready")
			for p in suggested["proposals"]:
				self.assertNotIn(p["suggested_room"], (self.r_oo, self.r_os))

			# Quick assign works for valid Inspected room and rejects OO/OS/wrong-type/double-booking
			assigned = tape_queue.quick_assign_room(PROP_A, res_vip, self.r_inspected, "VIP auto-assign")
			self.assertTrue(assigned["ok"])
			self.assertEqual(frappe.db.get_value("Reservation", res_vip, "room"), self.r_inspected)

			with self.assertRaises(frappe.ValidationError):
				tape_queue.quick_assign_room(PROP_A, res_std, self.r_inspected)
			with self.assertRaises(frappe.ValidationError):
				tape_queue.quick_assign_room(PROP_A, res_std, self.r_std)
			with self.assertRaises(frappe.ValidationError):
				tape_queue.quick_assign_room(PROP_A, res_std, self.r_oo)
			with self.assertRaises(frappe.ValidationError):
				tape_queue.quick_assign_room(PROP_A, res_std, self.r_os)

			# Multi-property isolation: user restricted to PROP_A cannot read/mutate PROP_B
			user = ensure_user("ut_tape_prop_a@example.test", ["Front Desk"])
			if not frappe.db.exists("User Permission", {"user": user, "allow": "Property", "for_value": PROP_A}):
				frappe.get_doc({
					"doctype": "User Permission",
					"user": user,
					"allow": "Property",
					"for_value": PROP_A,
				}).insert(ignore_permissions=True)
			frappe.clear_cache(user=user)
			prev_user = frappe.session.user
			try:
				frappe.set_user(user)  # nosemgrep: frappe-setuser -- test multi-property isolation for restricted Front Desk user
				self.assertEqual(tape_queue.unassigned_queue(PROP_A, TODAY, 14)["property"], PROP_A)
				with self.assertRaises(frappe.PermissionError):
					tape_queue.unassigned_queue(PROP_B, TODAY, 14)
				with self.assertRaises(frappe.PermissionError):
					tape_queue.room_metadata(PROP_B)
				with self.assertRaises(frappe.PermissionError):
					tape_queue.suggest_queue_allocation(PROP_B, TODAY)
			finally:
				frappe.set_user(prev_user)  # nosemgrep: frappe-setuser -- restore prior session user after isolation test


if __name__ == "__main__":
	unittest.main()
