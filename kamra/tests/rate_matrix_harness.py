"""A site-free Frappe stand-in for the rate plan matrix tests.

The real Kamra modules (pricing, folio, api, the Rate Plan and Reservation
controllers) run against in-memory documents and a recording tariff bridge,
so the tests exercise production code without a bench."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from datetime import date, datetime, timedelta
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

TODAY = date(2026, 11, 23)
PRECISION = {"UGX": 0, "JPY": 0, "KWD": 3}


class ValidationError(Exception):
	pass


class PermissionDenied(ValidationError):
	pass


class Doc(dict):
	"""A document: fields read as attributes (None when unset) or with get()."""

	def __getattr__(self, key):
		if key.startswith("__"):
			raise AttributeError(key)
		return self.get(key)

	def __setattr__(self, key, value):
		self[key] = value

	def set(self, key, value):
		self[key] = value

	def is_new(self):
		fn = self.get("is_new")
		return fn() if callable(fn) else (bool(self.get("__islocal")) or not self.get("name"))

	def get_doc_before_save(self):
		fn = self.get("get_doc_before_save")
		return fn() if callable(fn) else self.get("_doc_before_save")


def getdate(value=None):
	if value in (None, ""):
		return TODAY
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	return date.fromisoformat(str(value)[:10])


def cint(value):
	try:
		return int(float(value or 0))
	except (TypeError, ValueError):
		return 0


def flt(value, precision=None):
	try:
		number = float(value or 0)
	except (TypeError, ValueError):
		number = 0.0
	return round(number, precision) if precision is not None else number


def utils_module():
	utils = ModuleType("frappe.utils")
	utils.getdate = getdate
	utils.add_days = lambda value, days: getdate(value) + timedelta(days=int(days))
	utils.date_diff = lambda a, b: (getdate(a) - getdate(b)).days
	utils.cint = cint
	utils.flt = flt
	utils.cstr = lambda value: "" if value is None else str(value)
	utils.nowdate = lambda: str(TODAY)
	utils.today = utils.nowdate
	utils.now_datetime = lambda: datetime(2026, 11, 23, 12, 0)
	utils.get_datetime = lambda value=None: datetime.fromisoformat(str(value)) if value else utils.now_datetime()
	utils.add_to_date = lambda value, **kwargs: value
	utils.formatdate = lambda value, *args, **kwargs: str(value)
	utils.__getattr__ = lambda name: MagicMock(name=f"frappe.utils.{name}")
	return utils


class RateMatrixHarness(unittest.TestCase):
	"""In-memory documents plus a recording `hospitality_tariffs` bridge."""

	modules: tuple[str, ...] = ("kamra.pricing",)

	def setUp(self):
		self.docs: dict[tuple[str, str], Doc] = {}
		self.tariffs: dict[str, dict] = {}
		self.bridge_calls: list[tuple[str, dict]] = []
		# ERPNext Currency Exchange rows (selling), other records for get_all, and what was asked of them
		self.exchange_rates: list[Doc] = []
		self.records: dict[str, list[Doc]] = {}
		self.fx_queries: list[dict] = []
		self.stale_days = 1
		self.logged: list[tuple[tuple, dict]] = []
		self.roles = ["Hotel Admin"]
		self.frappe = self.frappe_module()
		self.utils = utils_module()
		document = ModuleType("frappe.model.document")
		document.Document = Doc
		model = ModuleType("frappe.model")
		model.document = document
		bridge = ModuleType("kamra.erp_bridge")
		bridge.bridge_call = self.bridge_call
		bridge.get_bridge_script = lambda method: None
		localization = ModuleType("kamra.localization")
		localization.default_currency = lambda pack: "USD"
		localization.pack_for = lambda property: SimpleNamespace(
			calculate_room_tax=lambda *args: self.room_tax)
		localization.setting_default = lambda property, key: None
		numbers = ModuleType("babel.numbers")
		numbers.get_currency_precision = lambda currency: PRECISION.get(currency, 2)
		babel = ModuleType("babel")
		babel.numbers = numbers
		self.room_tax = 0
		stubs = {
			"frappe": self.frappe, "frappe.utils": self.utils, "frappe.model": model,
			"frappe.model.document": document, "kamra.erp_bridge": bridge,
			"kamra.localization": localization, "babel": babel, "babel.numbers": numbers,
		}
		stubs.update(self.extra_modules())
		if "kamra.localization" in stubs and not hasattr(stubs["kamra.localization"], "setting_default"):
			stubs["kamra.localization"].setting_default = lambda property, key: None
		modules = patch.dict(sys.modules, stubs)
		modules.start()
		self.addCleanup(modules.stop)
		self.load("kamra.branding")
		self.load("kamra.business_date")
		for name in self.modules:
			setattr(self, name.rsplit(".", 1)[-1], self.load(name))
		self.add("Property", "Test Hotel", currency="UGX", business_date="2026-12-01", rates_include_tax=0)

	def extra_modules(self) -> dict:
		return {}

	def load(self, name):
		spec = importlib.util.find_spec(name)
		module = importlib.util.module_from_spec(spec)
		sys.modules[name] = module
		spec.loader.exec_module(module)
		return module

	# -- documents ---------------------------------------------------------
	def add(self, doctype, name, **fields):
		doc = Doc(doctype=doctype, name=name, flags=Doc(), **fields)
		self.docs[(doctype, name)] = doc
		return doc

	def plan(self, name, **fields):
		defaults = {"property": "Test Hotel", "rate_plan_name": name, "code": name,
		            "modifier_type": "Percent", "modifier_value": 0,
		            "fx_lock_policy": "Fixed at Reservation", "rounding_step": 0,
		            "rounding_mode": "Nearest", "adjust_extra_person": 0}
		return self.add("Rate Plan", name, **{**defaults, **fields})

	def get_doc(self, doctype, name=None):
		if isinstance(doctype, dict):
			doc = Doc(doctype)
			doc["insert"] = lambda **kwargs: doc
			return doc
		try:
			return self.docs[(doctype, name)]
		except KeyError:
			raise ValidationError(f"{doctype} {name} not found") from None

	def get_value(self, doctype, name, fieldname="name", as_dict=False, **kwargs):
		doc = self.docs.get((doctype, name)) if isinstance(name, str) else None
		if doc is None:
			return None
		if isinstance(fieldname, (list, tuple)):
			values = Doc({field: doc.get(field) for field in fieldname})
			return values if as_dict else tuple(values.values())
		return doc.get(fieldname)

	def rate(self, currency, exchange_rate, on, to="UGX"):
		"""Publish a selling Currency Exchange row (1 `currency` = `exchange_rate` `to`) dated `on`."""
		row = Doc(from_currency=currency, to_currency=to, exchange_rate=exchange_rate, date=on, for_selling=1)
		self.exchange_rates.append(row)
		return row

	@staticmethod
	def matches(row, filters) -> bool:
		for key, wanted in (filters or {}).items():
			value = row.get(key)
			if isinstance(wanted, (list, tuple)) and wanted and wanted[0] == "between":
				low, high = wanted[1]
				if value is None or not str(low) <= str(value) <= str(high):
					return False
			elif isinstance(wanted, (list, tuple)) and wanted and wanted[0] == "in":
				if value not in wanted[1]:
					return False
			elif value != wanted:
				return False
		return True

	def get_all(self, doctype, filters=None, fields=None, pluck=None, **kwargs):
		if doctype == "Currency Exchange":
			self.fx_queries.append(dict(filters or {}))
		rows = self.exchange_rates if doctype == "Currency Exchange" else self.records.get(doctype, [])
		found = [Doc(row) for row in rows if self.matches(row, filters)]
		return [row.get(pluck) for row in found] if pluck else found

	def exists(self, doctype, name=None):
		if doctype == "DocType":
			return name in ("Currency Exchange", "Accounts Settings") or name in self.records
		if isinstance(name, dict):
			return any(self.matches(row, name) for row in self.records.get(doctype, []))
		return (doctype, name) in self.docs

	def get_single_value(self, doctype, field):
		return self.stale_days if (doctype, field) == ("Accounts Settings", "stale_days") else None

	def throw(self, message, exc=None, title=None):
		raise (exc or ValidationError)(message)

	def frappe_module(self):
		frappe = ModuleType("frappe")
		frappe.ValidationError = ValidationError
		frappe.PermissionError = PermissionDenied
		frappe.DoesNotExistError = ValidationError
		frappe._ = lambda text, *args: text
		frappe._dict = Doc
		frappe.throw = self.throw
		frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
		frappe.get_doc = self.get_doc
		frappe.get_cached_doc = self.get_doc
		frappe.db = SimpleNamespace(get_value=self.get_value, exists=self.exists,
		                            get_single_value=self.get_single_value,
		                            sql=lambda *args, **kwargs: [(0,)])
		frappe.get_all = self.get_all
		frappe.get_meta = lambda doctype: SimpleNamespace(has_field=lambda field: True)
		frappe.parse_json = lambda value: json.loads(value) if isinstance(value, str) else value
		frappe.as_json = json.dumps
		frappe.get_roles = lambda user=None: self.roles
		frappe.session = SimpleNamespace(user="desk@example.test")
		frappe.local = SimpleNamespace(form_dict={}, lang="en")
		frappe.response = {}
		frappe.flags = Doc()
		frappe.log_error = lambda *args, **kwargs: self.logged.append((args, kwargs))
		frappe.__getattr__ = lambda name: MagicMock(name=f"frappe.{name}")
		return frappe

	# -- the tariff bridge -------------------------------------------------
	def tariff(self, **fields):
		"""A resolved foreign contract night, as `hospitality_tariffs.tariff_rate` returns it."""
		values = {"is_tariff": 1, "price_list": "Contract USD", "currency": "USD", "nightly": 130,
		          "property_currency": "UGX", "fx_rate": 3650, "fx_date": "2026-11-20",
		          "agreed_rate": 474500}
		return {**values, **fields}

	def bridge_call(self, api_method, *, response_types=None, **form):
		self.bridge_calls.append((api_method, dict(form)))
		if api_method != "hospitality_tariffs.tariff_rate":
			raise AssertionError(f"Unexpected bridge call: {api_method}")
		result = self.tariffs.get(form.get("rate_plan"), {"is_tariff": 0})
		if callable(result):
			result = result(form)
		return {"tariff": dict(result)}

	def tariff_forms(self):
		return [form for method, form in self.bridge_calls if method == "hospitality_tariffs.tariff_rate"]
