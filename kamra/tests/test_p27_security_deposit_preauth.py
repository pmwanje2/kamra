# Copyright (c) 2026, Maximus Hospitality and contributors
# For license information, please see license.txt

"""P27 Card Pre-Authorization Consumer tests for kamra/deposit.py & Security Deposit schema.

Siteless: loads the real kamra/deposit.py with lightweight Frappe stubs so it runs
under standard ``python3 -m unittest`` as well as inside a Frappe bench.
"""

from __future__ import annotations

import ast
import datetime
import importlib.util
import json
import re
import sys
import unittest
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

KAMRA = Path(__file__).resolve().parents[1]
REPO_ROOT = KAMRA.parent
SECURITY_DEPOSIT_JSON = KAMRA / "kamra" / "doctype" / "security_deposit" / "security_deposit.json"

ISO_PRECISION = {
	"UGX": 0,
	"USD": 2,
	"KWD": 3,
}

FORBIDDEN_BRAND = re.compile(
	r"(?<![a-z0-9])(?:Kamra|Frappe|ERPNext|URY|Tridz|BuildSuite)(?![a-z0-9])"
	r"|kamrapms\.com|github\.com/Kamra|HeyKoala|IndianRupee|BadgeIndianRupee|CurrencyRupee|\u20b9",
	re.I,
)


def _mod(name: str, **attrs) -> ModuleType:
	m = ModuleType(name)
	vars(m).update(attrs)
	return m


class _ValidationError(Exception):
	pass


class _PermissionError(Exception):
	pass


class _Doc(dict):
	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError as exc:
			raise AttributeError(key) from exc

	def __setattr__(self, key, value):
		self[key] = value

	def get(self, key, default=None):
		return super().get(key, default)

	def insert(self, ignore_permissions=False):
		if self._store is not None:
			self._store._insert_doc(self)
		return self

	def save(self, ignore_permissions=False):
		if self._store is not None:
			self._store._save_doc(self)
		return self

	def append(self, fieldname, row):
		self.setdefault(fieldname, []).append(_Doc(row, _store=None))

	def reload(self):
		return self

	def as_dict(self):
		return {k: v for k, v in self.items() if k != "_store"}


class _FakeSite:
	"""Minimal in-memory Frappe/ERP store exercising real kamra/deposit.py."""

	def __init__(self, *, preauth_installed: bool = True, allowed_properties: set[str] | None = None):
		self.preauth_installed = preauth_installed
		self.allowed_properties = allowed_properties
		self.permissions: dict[tuple[str, str], bool] = {}
		self.customers: dict[str, dict] = {}
		self.properties: dict[str, dict] = {}
		self.reservations: dict[str, dict] = {}
		self.folios: dict[str, dict] = {}
		self.deposits: dict[str, dict] = {}
		self.preauths: dict[str, dict] = {}
		self.preauth_events: dict[str, list[dict]] = {}
		self.folio_charges: list[dict] = []
		self.folio_payments: list[dict] = []
		self.ledger_entries: list[dict] = []
		self.payment_entries: list[dict] = []
		self.gl_entries: list[dict] = []
		self.actions: list[dict] = []
		self.pin_checks: list[tuple[str, str | None]] = []
		self.bridge_calls: list[tuple[str, dict]] = []
		self._seq = 0
		self.now = datetime.datetime(2026, 11, 25, 10, 0, 0)

	def _next(self, prefix: str) -> str:
		self._seq += 1
		return f"{prefix}-{self._seq:05d}"

	def add_property(
		self,
		name: str,
		*,
		currency: str,
		company: str,
		walk_in_customer: str,
		security_deposit_amount: float = 0,
		cost_center: str | None = None,
		branch: str | None = None,
	):
		self.properties[name] = {
			"name": name,
			"currency": currency,
			"company": company,
			"walk_in_customer": walk_in_customer,
			"security_deposit_amount": security_deposit_amount,
			"cost_center": cost_center,
			"branch": branch,
			"business_date": "2026-11-25",
		}

	def add_reservation(
		self,
		name: str,
		*,
		property: str,
		guest: str = "GUEST-1",
		guest_name: str = "Amina Guest",
		status: str = "Checked In",
		folio_name: str | None = None,
		folio_status: str = "Open",
		folio_balance: float = 0.0,
		folio_customer: str | None = None,
	):
		self.reservations[name] = {
			"doctype": "Reservation",
			"name": name,
			"property": property,
			"guest": guest,
			"guest_name": guest_name,
			"status": status,
		}
		if folio_name:
			self.folios[folio_name] = {
				"doctype": "Folio",
				"name": folio_name,
				"property": property,
				"reservation": name,
				"folio_type": "Guest",
				"status": folio_status,
				"balance": folio_balance,
				"charges_total": 0.0,
				"tax_total": 0.0,
				"grand_total": 0.0,
				"payments_total": 0.0,
				"customer": folio_customer,
				"charges": [],
				"payments": [],
			}

	def _insert_doc(self, doc: _Doc):
		dt = doc.get("doctype")
		if dt == "Security Deposit":
			if not doc.get("name"):
				doc["name"] = self._next("DEP")
			self._recalc_deposit(doc)
			self.deposits[doc["name"]] = doc.as_dict()
		elif dt == "Folio":
			if not doc.get("name"):
				doc["name"] = self._next("FOLIO")
			self.folios[doc["name"]] = doc.as_dict()

	def _save_doc(self, doc: _Doc):
		dt = doc.get("doctype")
		if dt == "Security Deposit":
			self._recalc_deposit(doc)
			self.deposits[doc["name"]] = doc.as_dict()
		elif dt == "Folio":
			self.folios[doc["name"]] = doc.as_dict()
		elif dt == "Reservation":
			self.reservations[doc["name"]] = doc.as_dict()

	def _recalc_deposit(self, doc: _Doc):
		col = float(doc.get("collected_amount") or 0)
		withheld = float(doc.get("withheld_amount") or 0)
		refunded = float(doc.get("refunded_amount") or 0)
		doc["balance"] = max(0.0, col - withheld - refunded)

	def _table(self, dt: str) -> dict[str, dict]:
		return {
			"Property": self.properties,
			"Reservation": self.reservations,
			"Folio": self.folios,
			"Security Deposit": self.deposits,
			"Credit Preauthorization": self.preauths,
			"Customer": self.customers,
			"Payment Entry": {row["name"]: row for row in self.payment_entries},
		}.get(dt, {})

	def _match(self, row: dict, filters) -> bool:
		if isinstance(filters, str):
			return row.get("name") == filters
		if isinstance(filters, dict):
			for k, v in filters.items():
				if isinstance(v, tuple) and v[0] == "like":
					prefix = v[1].rstrip("%")
					if not str(row.get(k) or "").startswith(prefix):
						return False
				elif row.get(k) != v:
					return False
			return True
		return False

	def exists(self, dt: str, name=None):
		if dt == "DocType":
			if name == "Security Deposit":
				return True
			if name in ("Credit Preauthorization", "Credit Preauthorization Event"):
				return self.preauth_installed
			if name in ("Company", "Customer", "Payment Entry"):
				return self.preauth_installed
			return False
		table = self._table(dt)
		if isinstance(name, str):
			return name in table
		return any(self._match(r, name) for r in table.values())

	def get_value(self, dt: str, filters, fieldname=None, as_dict=False, order_by=None, **kwargs):
		rows = [r for r in self._table(dt).values() if self._match(r, filters)]
		if not rows:
			return None
		row = rows[0]
		if isinstance(fieldname, list):
			data = {f: row.get(f) for f in fieldname}
			return _Doc(data, _store=None) if as_dict else tuple(data[f] for f in fieldname)
		if as_dict and isinstance(fieldname, str):
			return _Doc({fieldname: row.get(fieldname)}, _store=None)
		return row.get(fieldname)

	def get_doc(self, arg1, arg2=None, **kwargs):
		if isinstance(arg1, dict):
			return _Doc(dict(arg1), _store=self)
		table = self._table(arg1)
		if arg2 not in table:
			raise KeyError(f"{arg1} {arg2} not found")
		data = dict(table[arg2])
		data.setdefault("doctype", arg1)
		return _Doc(data, _store=self)

	def get_all(self, dt: str, filters=None, fields=None, order_by=None, limit_page_length=None, **kwargs):
		if dt == "Folio Payment":
			parent = (filters or {}).get("parent")
			rows = [r for r in self.folio_payments if r.get("parent") == parent]
		elif dt == "Folio Charge":
			parent = (filters or {}).get("parent")
			rows = [r for r in self.folio_charges if r.get("parent") == parent and self._match(r, filters)]
		elif dt == "Credit Preauthorization Event":
			parent = (filters or {}).get("parent")
			rows = list(self.preauth_events.get(parent, []))
		else:
			rows = [r for r in self._table(dt).values() if self._match(r, filters or {})]
		return [_Doc(dict(r), _store=None) for r in rows]

	def bridge_call(self, api_method: str, *, response_types=None, **form):
		if not self.preauth_installed:
			return None
		self.bridge_calls.append((api_method, dict(form)))
		if api_method == "preauth_control.authorize":
			prec = ISO_PRECISION.get(form.get("currency", "USD"), 2)
			amt = round(float(form.get("authorized_amount") or form.get("amount") or 0), prec)
			name = self._next("AUTH-2026")
			rec = {
				"name": name,
				"company": form["company"],
				"branch": form.get("branch"),
				"cost_center": form.get("cost_center"),
				"customer": form["customer"],
				"currency": form["currency"],
				"precision": prec,
				"status": "Authorized",
				"gateway_auth_ref": form["gateway_auth_ref"],
				"card_brand": form.get("card_brand"),
				"card_last4": form.get("card_last4"),
				"authorized_amount": amt,
				"captured_amount": 0.0,
				"released_amount": 0.0,
				"remaining_hold_amount": amt,
				"authorized_at": str(self.now),
				"expires_on": str(form["expires_on"]),
				"payment_entry": None,
			}
			self.preauths[name] = rec
			self.preauth_events[name] = [
				{
					"event_type": "Authorize",
					"delta_amount": amt,
					"resulting_authorized": amt,
					"resulting_captured": 0.0,
					"gateway_tx_ref": form["gateway_auth_ref"],
					"event_at": str(self.now),
					"note": form.get("note") or "Pre-authorization hold created",
				}
			]
			return {"ok": True, **rec}

		if api_method == "preauth_control.increment":
			name = form["name"]
			rec = self.preauths[name]
			prec = rec["precision"]
			delta = round(float(form["additional_amount"]), prec)
			new_auth = round(rec["authorized_amount"] + delta, prec)
			rec["authorized_amount"] = new_auth
			rec["remaining_hold_amount"] = new_auth
			rec["status"] = "Incremented"
			if form.get("expires_on"):
				rec["expires_on"] = str(form["expires_on"])
			self.preauth_events[name].append(
				{
					"event_type": "Increment",
					"delta_amount": delta,
					"resulting_authorized": new_auth,
					"resulting_captured": 0.0,
					"gateway_tx_ref": form.get("gateway_tx_ref") or rec["gateway_auth_ref"],
					"event_at": str(self.now),
					"note": form.get("note") or "Pre-authorization hold incremented",
				}
			)
			return {"ok": True, **rec}

		if api_method == "preauth_control.capture":
			name = form["name"]
			rec = self.preauths[name]
			prec = rec["precision"]
			raw_cap = float(form.get("capture_amount") or form.get("amount") or 0)
			cap_amt = round(raw_cap, prec)
			if raw_cap > rec["remaining_hold_amount"] or cap_amt > rec["remaining_hold_amount"]:
				raise _ValidationError("[PREAUTH-OVER-CAPTURE]: Capture amount cannot exceed authorized hold.")
			pe_name = form.get("existing_payment_entry")
			if not pe_name:
				pe_name = self._next("ACC-PAY-2026")
				can_submit = self.permissions.get(("Payment Entry", "submit"), True)
				self.payment_entries.append({
					"name": pe_name,
					"company": rec["company"],
					"party": rec["customer"],
					"party_type": "Customer",
					"payment_type": "Receive",
					"currency": rec["currency"],
					"paid_amount": cap_amt,
					"reference_no": form.get("gateway_capture_ref") or rec["gateway_auth_ref"],
					"docstatus": int(can_submit),
				})
				if not can_submit:
					return {**rec, "ok": True, "capture_mode": "draft_pending_submit", "payment_entry": pe_name}
				self.gl_entries.append({"voucher_no": pe_name, "debit": cap_amt, "credit": 0.0})
				self.gl_entries.append({"voucher_no": pe_name, "debit": 0.0, "credit": cap_amt})
			rec["status"] = "Captured"
			rec["captured_amount"] = cap_amt
			rec["released_amount"] = round(rec["authorized_amount"] - cap_amt, prec)
			rec["remaining_hold_amount"] = 0.0
			rec["payment_entry"] = pe_name
			self.preauth_events[name].append(
				{
					"event_type": "Capture",
					"delta_amount": cap_amt,
					"resulting_authorized": rec["authorized_amount"],
					"resulting_captured": cap_amt,
					"gateway_tx_ref": form.get("gateway_capture_ref") or rec["gateway_auth_ref"],
					"event_at": str(self.now),
					"note": form.get("note") or f"Captured via {pe_name}",
				}
			)
			return {"ok": True, **rec, "capture_mode": "posted", "payment_entry_docstatus": 1}

		if api_method == "preauth_control.release":
			name = form["name"]
			rec = self.preauths[name]
			prec = rec["precision"]
			rel = rec["remaining_hold_amount"]
			rec["status"] = "Released"
			rec["released_amount"] = rel
			rec["remaining_hold_amount"] = 0.0
			rec["release_reason"] = form.get("release_reason") or "Released without capture"
			self.preauth_events[name].append(
				{
					"event_type": "Release",
					"delta_amount": rel,
					"resulting_authorized": rec["authorized_amount"],
					"resulting_captured": 0.0,
					"gateway_tx_ref": rec["gateway_auth_ref"],
					"event_at": str(self.now),
					"note": form.get("note") or rec["release_reason"],
				}
			)
			return {"ok": True, **rec}

		return None


def _load_deposit_module(site: _FakeSite):
	def _has_permission(
		doctype=None, ptype="read", doc=None, user=None, throw=False, *,
		parent_doctype=None, debug=False, ignore_share_permissions=False,
	):
		return site.permissions.get((doctype, ptype), True)

	def _throw(msg, exc=None, title=None):
		raise (exc or _ValidationError)(msg)

	def _get_list(dt, **kwargs):
		if dt == "Customer":
			raise _PermissionError("Insufficient Permission for Customer")
		return site.get_all(dt, **kwargs)

	def _get_datetime(val):
		if isinstance(val, datetime.datetime):
			return val
		text = str(val).strip().replace("T", " ")
		for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
			try:
				return datetime.datetime.strptime(text, fmt)
			except ValueError:
				continue
		raise ValueError(f"Invalid datetime: {val}")

	def _assert_property_access(prop):
		if site.allowed_properties is not None and prop not in site.allowed_properties:
			raise _PermissionError(f"Not permitted for property {prop}")

	def _post_folio_charge(folio, charge_type, description, amount, gst_rate=0, **_kw):
		f = site.folios[folio]
		row = {
			"parent": folio,
			"parenttype": "Folio",
			"posting_date": "2026-11-25",
			"charge_type": charge_type,
			"description": description,
			"amount": float(amount),
		}
		site.folio_charges.append(row)
		f["charges"].append(row)
		f["charges_total"] = float(f["charges_total"]) + float(amount)
		f["grand_total"] = float(f["grand_total"]) + float(amount)
		f["balance"] = float(f["grand_total"]) - float(f["payments_total"])
		return f

	def _recalculate(doc, payments_only=False):
		p_tot = sum(float(p.get("amount") or 0) for p in doc.get("payments") or [])
		doc["payments_total"] = p_tot
		doc["balance"] = float(doc.get("grand_total") or 0) - p_tot
		for p in doc.get("payments") or []:
			row = dict(p)
			row["parent"] = doc["name"]
			if row not in site.folio_payments:
				site.folio_payments.append(row)

	def _open_folio(res):
		fname = site._next("FOLIO")
		site.folios[fname] = {
			"doctype": "Folio",
			"name": fname,
			"property": res.property,
			"reservation": res.name,
			"folio_type": "Guest",
			"status": "Open",
			"balance": 0.0,
			"charges_total": 0.0,
			"tax_total": 0.0,
			"grand_total": 0.0,
			"payments_total": 0.0,
			"customer": site.properties[res.property].get("walk_in_customer"),
			"charges": [],
			"payments": [],
		}
		return fname

	def _require_roles(*roles, **kw):
		def deco(fn):
			fn._kamra_roles = set(roles)
			return fn
		return deco

	def _whitelist(*args, **kwargs):
		if args and callable(args[0]):
			fn = args[0]
			fn._whitelisted = True
			fn._whitelist_methods = None
			return fn

		def deco(fn):
			fn._whitelisted = True
			fn._whitelist_methods = kwargs.get("methods")
			return fn
		return deco

	fake_frappe = _mod(
		"frappe",
		_=lambda s: s,
		whitelist=_whitelist,
		ValidationError=_ValidationError,
		PermissionError=_PermissionError,
		DoesNotExistError=KeyError,
		throw=_throw,
		session=SimpleNamespace(user="desk@example.test"),
		flags=SimpleNamespace(),
		db=SimpleNamespace(
			exists=site.exists,
			get_value=site.get_value,
		),
		defaults=SimpleNamespace(get_global_default=lambda k: None),
		get_doc=site.get_doc,
		get_cached_doc=site.get_doc,
		get_cached_value=site.get_value,
		get_all=site.get_all,
		get_list=_get_list,
		has_permission=_has_permission,
		get_meta=lambda dt: SimpleNamespace(
			has_field=lambda f: (dt == "Folio" and f == "customer")
			or (dt == "Folio Payment" and f == "payment_entry")
			or (dt == "Property" and f in ("company", "cost_center", "branch", "walk_in_customer"))
		),
	)

	def _flt(v=0, precision=None):
		val = float(v or 0)
		return round(val, int(precision)) if precision is not None else val

	fake_utils = _mod(
		"frappe.utils",
		flt=_flt,
		now_datetime=lambda: site.now,
		nowdate=lambda: "2026-11-25",
		add_days=lambda date, days: date + datetime.timedelta(days=days),
		getdate=datetime.date.fromisoformat,
		get_datetime=_get_datetime,
	)

	from decimal import Decimal, ROUND_HALF_UP

	def _currency_places(prop):
		curr = site.properties.get(prop, {}).get("currency", "USD") if prop else "USD"
		return ISO_PRECISION.get(curr, 2)

	def _money(v):
		return Decimal(str(v or 0))

	def _round_currency(v, places):
		places = max(int(places), 0)
		out = float(_money(v).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))
		return 0.0 if out == 0 else out

	def _fmt_currency(v, places):
		places = max(int(places), 0)
		return f"{_round_currency(v, places):,.{places}f}"

	def _write_ledger_entry(**kw):
		site.ledger_entries.append(dict(kw))

	stubs = {
		"frappe": fake_frappe,
		"frappe.utils": fake_utils,
		"kamra.crs": _mod("kamra.crs", assert_property_access=_assert_property_access),
		"kamra.authz": _mod(
			"kamra.authz",
			require_roles=_require_roles,
			require_cashier_pin=lambda prop, pin=None: site.pin_checks.append((prop, pin)),
		),
		"kamra.api": _mod(
			"kamra.api",
			_pin_guard=lambda folio, pin=None: site.pin_checks.append((site.folios[folio]["property"], pin)),
			_post_folio_charge=_post_folio_charge,
		),
		"kamra.folio": _mod("kamra.folio", open_folio=_open_folio, _recalculate=_recalculate),
		"kamra.ledger": _mod(
			"kamra.ledger",
			_currency_places=_currency_places,
			_money=_money,
			_round_currency=_round_currency,
			_fmt_currency=_fmt_currency,
			home_currency=lambda prop: site.properties[prop]["currency"],
			PAYMENT_CODES={"Security Deposit": ("9020",)},
			write_ledger_entry=_write_ledger_entry,
		),
		"kamra.pricing": _mod(
			"kamra.pricing",
			property_currency=lambda prop: site.properties[prop]["currency"],
			_minor_unit=lambda code: ISO_PRECISION.get((code or "").upper(), 2),
		),
		"kamra.localization": _mod(
			"kamra.localization",
			pack_for=lambda prop: SimpleNamespace(),
			payment_modes=lambda pack, prop: ["Cash", "Card", "Bank Transfer"],
		),
		"kamra.masters": _mod(
			"kamra.masters",
			payment_tender_type=lambda mode, prop=None: "Card" if mode == "Card" else "Cash",
		),
		"kamra.business_date": _mod(
			"kamra.business_date",
			get_business_date=lambda prop: site.properties[prop].get("business_date", "2026-11-25"),
		),
		"kamra.savings": _mod(
			"kamra.savings",
			log_action=lambda *a, **kw: site.actions.append({"args": a, "kw": kw}),
		),
		"kamra.erp_bridge": _mod(
			"kamra.erp_bridge",
			get_bridge_script=lambda method: (
				SimpleNamespace(api_method=method, execute_method=lambda: None)
				if site.preauth_installed
				else None
			),
			bridge_call=site.bridge_call,
		),
	}

	patcher = patch.dict(sys.modules, stubs)
	patcher.start()
	try:
		spec = importlib.util.spec_from_file_location("deposit_p27_under_test", KAMRA / "deposit.py")
		mod = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(mod)
		return mod, patcher
	except Exception:
		patcher.stop()
		raise


class P27SecurityDepositPreauthTest(unittest.TestCase):
	def setUp(self):
		self.site = _FakeSite(preauth_installed=True)
		self.site.add_property(
			"Kampala Grand",
			currency="UGX",
			company="Kampala Grand Ltd",
			walk_in_customer="Walk-In UGX",
			security_deposit_amount=500000,
			cost_center="Main - KGL",
		)
		self.site.add_property(
			"Harbor Plaza",
			currency="USD",
			company="Harbor Plaza Inc",
			walk_in_customer="Walk-In USD",
			security_deposit_amount=250.00,
			cost_center="Main - HPI",
		)
		self.site.add_property(
			"Kuwait Bay",
			currency="KWD",
			company="Kuwait Bay Co",
			walk_in_customer="Walk-In KWD",
			security_deposit_amount=50.125,
			cost_center="Main - KBC",
		)
		self.site.add_reservation(
			"RES-UGX-1",
			property="Kampala Grand",
			status="Checked In",
			folio_name="FOL-UGX-1",
			folio_customer="Walk-In UGX",
		)
		self.site.add_reservation(
			"RES-USD-1",
			property="Harbor Plaza",
			status="Checked In",
			folio_name="FOL-USD-1",
			folio_customer="Walk-In USD",
		)
		self.site.add_reservation(
			"RES-KWD-1",
			property="Kuwait Bay",
			status="Checked In",
			folio_name="FOL-KWD-1",
			folio_customer="Walk-In KWD",
		)
		self.deposit, self._patcher = _load_deposit_module(self.site)

	def tearDown(self):
		self._patcher.stop()

	def _authorize_usd(self):
		return self.deposit.authorize_deposit(
			"RES-USD-1", 250, gateway_auth_ref="GW-USD-27", card_last4="4242",
			expires_on="2026-12-05 12:00:00",
		)

	def _capture_usd(self, amount=50, **kwargs):
		return self.deposit.capture_deposit_auth("RES-USD-1", amount, "Damage", **kwargs)

	def test_capture_draft_is_reported_after_refresh_without_posting(self):
		self._authorize_usd()
		self.site.permissions["Payment Entry", "submit"] = False
		result = self._capture_usd(gateway_capture_ref="CUSTOM-CAPTURE-REF")
		self.assertEqual(result["capture_mode"], "draft_pending_submit")
		entry = result["pending_payment_entry"]
		self.assertEqual(self.site.payment_entries[0]["docstatus"], 0)
		self.assertFalse(self.site.folio_charges)
		self.assertFalse(self.site.gl_entries)
		for state in (result, self.deposit.deposit_state("RES-USD-1")):
			with self.subTest(state=state):
				self.assertEqual(state.get("pending_capture_payment_entry"), entry)
				self.assertEqual(state["status"], "Authorized")
				self.assertFalse(state["can_capture"])

	def test_duplicate_capture_is_refused_with_draft_name(self):
		self._authorize_usd()
		self.site.permissions["Payment Entry", "submit"] = False
		entry = self._capture_usd(gateway_capture_ref="CUSTOM-CAPTURE-REF")["pending_payment_entry"]
		with self.assertRaisesRegex(_ValidationError, entry):
			self._capture_usd(gateway_capture_ref="DIFFERENT-CAPTURE-REF")
		self.assertEqual(len(self.site.payment_entries), 1)
		self.assertFalse(self.site.folio_charges)

	def _make_capture_entry(self, docstatus):
		self._authorize_usd()
		self.site.permissions["Payment Entry", "submit"] = False
		entry = self._capture_usd()["pending_payment_entry"]
		if docstatus == 1:
			self.site.payment_entries[0]["docstatus"] = 1
			self.site.gl_entries.extend([
				{"voucher_no": entry, "debit": 50.0, "credit": 0.0},
				{"voucher_no": entry, "debit": 0.0, "credit": 50.0},
			])
		return entry

	def test_submitted_capture_requires_read_and_submit_permissions(self):
		self._make_capture_entry(1)
		for can_read, can_create, can_submit in (
			(True, True, False), (False, True, True), (True, False, True),
		):
			with self.subTest(read=can_read, create=can_create, submit=can_submit):
				self.site.permissions["Payment Entry", "read"] = can_read
				self.site.permissions["Payment Entry", "create"] = can_create
				self.site.permissions["Payment Entry", "submit"] = can_submit
				state = self.deposit.deposit_state("RES-USD-1")
				self.assertIsNone(state["pending_capture_payment_entry"])
				self.assertEqual(state["can_capture"], can_read and can_submit)

	def test_capture_entry_disables_release_and_increment(self):
		self._make_capture_entry(0)
		for docstatus in (0, 1):
			with self.subTest(docstatus=docstatus):
				self.site.payment_entries[0]["docstatus"] = docstatus
				state = self.deposit.deposit_state("RES-USD-1")
				self.assertEqual((state["can_release"], state["can_increment"]), (False, False))

	def _assert_capture_entry_blocks(self, docstatus, action, **kwargs):
		entry = self._make_capture_entry(docstatus)
		before = deepcopy(vars(self.site))
		with self.assertRaisesRegex(_ValidationError, re.escape(entry)):
			action("RES-USD-1", **kwargs)
		self.assertEqual(vars(self.site), before)

	def test_draft_capture_blocks_release(self):
		self._assert_capture_entry_blocks(0, self.deposit.release_deposit_auth)

	def test_draft_capture_blocks_increment(self):
		self._assert_capture_entry_blocks(0, self.deposit.increment_deposit_auth, additional_amount=25)

	def test_submitted_capture_blocks_release(self):
		self._assert_capture_entry_blocks(1, self.deposit.release_deposit_auth)

	def test_submitted_capture_blocks_increment(self):
		self._assert_capture_entry_blocks(1, self.deposit.increment_deposit_auth, additional_amount=25)

	def test_detects_preexisting_draft_by_bridge_reference(self):
		hold = self._authorize_usd()
		self.site.payment_entries.append({
			"name": "PE-LEGACY-DRAFT", "docstatus": 0, "payment_type": "Receive",
			"company": "Harbor Plaza Inc", "party_type": "Customer", "party": "Walk-In USD",
			"reference_no": hold["gateway_auth_ref"],
		})
		state = self.deposit.deposit_state("RES-USD-1")
		self.assertEqual(state.get("pending_capture_payment_entry"), "PE-LEGACY-DRAFT")
		with self.assertRaisesRegex(_ValidationError, "PE-LEGACY-DRAFT"):
			self._capture_usd()
		self.assertEqual(len(self.site.payment_entries), 1)

	def test_capture_reuses_the_tracked_entry_after_finance_submits(self):
		self._authorize_usd()
		self.site.permissions["Payment Entry", "submit"] = False
		entry = self._capture_usd()["pending_payment_entry"]
		self.site.permissions["Payment Entry", "submit"] = True
		self.site.payment_entries[0]["docstatus"] = 1
		with self.assertRaisesRegex(_ValidationError, entry):
			self._capture_usd(amount=51)
		result = self._capture_usd()
		self.assertEqual(result["payment_entry"], entry)
		self.assertIsNone(result.get("pending_capture_payment_entry"))
		self.assertEqual(len(self.site.payment_entries), 1)
		self.assertEqual(len(self.site.folio_charges), 1)

	def test_desk_can_resolve_customer_on_a_permitted_property(self):
		self.site.allowed_properties = {"Harbor Plaza"}
		self.site.properties["Harbor Plaza"]["walk_in_customer"] = ""
		self.site.folios["FOL-USD-1"]["customer"] = None
		self.site.customers["CUS-AMINA"] = {"name": "CUS-AMINA", "customer_name": "Amina Guest"}
		state = self._authorize_usd()
		self.assertEqual(self.site.preauths[state["preauth_hold"]]["customer"], "CUS-AMINA")

	def test_frappe_permission_calls_are_not_in_boolean_expressions(self):
		tree = ast.parse((KAMRA / "deposit.py").read_text(encoding="utf-8"))
		unchecked_lines = set()
		for expr in ast.walk(tree):
			if not isinstance(expr, ast.BoolOp):
				continue
			for node in ast.walk(expr):
				if not isinstance(node, ast.Call):
					continue
				func = node.func
				if (
					isinstance(func, ast.Attribute)
					and func.attr == "has_permission"
					and isinstance(func.value, ast.Name)
					and func.value.id == "frappe"
					and not any(
						kw.arg == "throw" and isinstance(kw.value, ast.Constant) and kw.value.value is True
						for kw in node.keywords
					)
				):
					unchecked_lines.add(node.lineno)

		self.assertEqual(
			sorted(unchecked_lines), [],
			"Use an explicit guard for frappe.has_permission calls in deposit.py",
		)

	def test_has_permission_checks_frappe_with_optional_doc(self):
		has_permission = getattr(self.deposit, "_has_permission", None)
		self.assertTrue(callable(has_permission), "Expected a deposit._has_permission helper")
		for doctype, ptype, kwargs in (
			("Credit Preauthorization", "create", {}),
			("Credit Preauthorization", "write", {"doc": "PREAUTH-1"}),
			("Payment Entry", "create", {}),
			("Payment Entry", "submit", {}),
		):
			for allowed in (True, False):
				with self.subTest(doctype=doctype, ptype=ptype, allowed=allowed):
					with patch.object(self.deposit.frappe, "has_permission", return_value=allowed) as check:
						self.assertIs(has_permission(doctype, ptype, **kwargs), allowed)
						check.assert_called_once_with(doctype, ptype, **kwargs)

	def test_card_actions_follow_document_permissions(self):
		self.site.permissions["Credit Preauthorization", "create"] = False
		self.assertFalse(self.deposit.deposit_state("RES-USD-1")["can_authorize"])
		self.site.permissions["Credit Preauthorization", "create"] = True
		self.assertTrue(self.deposit.deposit_state("RES-USD-1")["can_authorize"])
		self._authorize_usd()
		for can_write, can_create, can_submit in (
			(False, True, True), (True, False, False), (True, True, False), (True, False, True),
		):
			with self.subTest(write=can_write, create=can_create, submit=can_submit):
				self.site.permissions["Credit Preauthorization", "write"] = can_write
				self.site.permissions["Payment Entry", "create"] = can_create
				self.site.permissions["Payment Entry", "submit"] = can_submit
				state = self.deposit.deposit_state("RES-USD-1")
				self.assertEqual(state["can_increment"], can_write)
				self.assertEqual(state["can_release"], can_write)
				self.assertEqual(state["can_capture"], can_write and can_create)

	def test_all_deposit_currency_fields_use_property_currency(self):
		fields = json.loads(SECURITY_DEPOSIT_JSON.read_text())["fields"]
		for field in fields:
			if field["fieldtype"] == "Currency":
				with self.subTest(field=field["fieldname"]):
					self.assertEqual(field.get("options"), "Property:property:currency")

	def test_preauth_event_notes_translate_before_formatting(self):
		translations = {
			"Security deposit pre-authorization for {0}": "Préautorisation de caution pour {0}",
			"Incremental pre-authorization for {0}": "Augmentation de la préautorisation pour {0}",
		}
		translate = lambda text: translations.get(text, text)
		with patch.object(self.deposit, "_", translate), patch.object(self.deposit.frappe, "_", translate):
			state = self._authorize_usd()
			updated = self.deposit.increment_deposit_auth("RES-USD-1", 25)
		self.assertEqual(state["history"][-1]["note"], "Préautorisation de caution pour RES-USD-1")
		self.assertEqual(updated["history"][-1]["note"], "Augmentation de la préautorisation pour RES-USD-1")

	def test_checkout_context_blocks_active_holds_and_clears_on_terminal_state(self):
		hold = self._authorize_usd()
		name = hold["preauth_hold"]
		checkout, modules = self._load_checkout()
		with patch.dict(sys.modules, modules):
			for status in ("Authorized", "Incremented", "Released", "Captured", "Expired"):
				with self.subTest(status=status):
					self.site.preauths[name]["status"] = status
					context = checkout._checkout_context("RES-USD-1")
					active = status in ("Authorized", "Incremented")
					self.assertEqual(context["actions"]["can_checkout"], not active)
					self.assertEqual(
						any("release the card hold" in b["message"] for b in context["blockers"]),
						active,
					)

	def test_checkout_endpoint_refuses_active_holds_and_allows_terminal_states(self):
		hold = self._authorize_usd()
		name = hold["preauth_hold"]
		checkout, modules = self._load_checkout()
		tree = ast.parse((KAMRA / "api.py").read_text())
		endpoint = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "check_out")
		endpoint.decorator_list = []
		namespace = {
			"frappe": self.deposit.frappe, "_": self.deposit._,
			"_assert_settled": lambda *_: [], "_scrub_stay_ids": lambda *_: None,
		}
		exec(compile(ast.Module(body=[endpoint], type_ignores=[]), "api.py", "exec"), namespace)
		with patch.dict(sys.modules, {**modules, "kamra.checkout": checkout}):
			for status in ("Authorized", "Incremented", "Released", "Captured", "Expired"):
				with self.subTest(status=status):
					res = self.site.reservations["RES-USD-1"]
					res["status"] = "Checked In"
					self.site.preauths[name]["status"] = status
					if status in ("Authorized", "Incremented"):
						with self.assertRaisesRegex(_ValidationError, "release the card hold"):
							namespace["check_out"]("RES-USD-1")
						self.assertEqual(res["status"], "Checked In")
					else:
						self.assertTrue(namespace["check_out"]("RES-USD-1")["ok"])
						self.assertEqual(self.site.reservations["RES-USD-1"]["status"], "Checked Out")

	def _load_checkout(self):
		"""Run checkout orchestration with a settled stay and unrelated services stubbed."""
		res = self.site.reservations["RES-USD-1"]
		res.update(
			room=None, company=None, room_type="Standard", check_in_date=self.site.now.date(),
			check_out_date=self.site.now.date(), adults=1, children=0, flags=SimpleNamespace(),
		)

		modules = {
			"kamra.deposit": self.deposit,
			"kamra.sop": _mod("kamra.sop", group_for_charge=lambda *_: None),
			"kamra.traces": _mod(
				"kamra.traces", assert_no_blocking_alerts=lambda *a, **kw: None,
				check_point_alerts=lambda *a, **kw: [], can_override_blocking_alert=lambda: False,
			),
		}
		for mod_name, attrs in {
			"kamra.authz": {"assert_property_access": lambda prop: None},
			"kamra.localization": {"pack_for": lambda prop: SimpleNamespace(locale=lambda doc: {})},
			"kamra.folio": {
				"post_remaining_nights": lambda res: None, "_security_deposit_row_ids": lambda doc: set(),
				"billing_rule_rows": lambda company: [],
			},
		}.items():
			modules[mod_name] = _mod(mod_name, **{**vars(sys.modules[mod_name]), **attrs})
		self.deposit.frappe.db.savepoint = lambda name: None
		spec = importlib.util.spec_from_file_location("checkout_p27_under_test", KAMRA / "checkout.py")
		checkout = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(checkout)
		checkout._NoTrace = lambda *args: nullcontext()
		checkout._modes = lambda prop: ["Card"]
		checkout._late_checkout = lambda *args: None
		checkout._pending_bills = lambda *args: []
		checkout._till = lambda prop: {"open": True}
		checkout._can_check_out = lambda: True
		checkout.early_departure_plan = lambda res: None
		checkout.settlement_position = lambda res: {
			"folios": [], "owed": 0, "refund_due": 0, "to_city_ledger": [],
		}
		return checkout, modules

	def test_security_deposit_schema_declares_preauth_fields_and_statuses(self):
		raw = SECURITY_DEPOSIT_JSON.read_text(encoding="utf-8")
		doc = json.loads(raw)
		for field in doc["fields"]:
			for key in ("label", "description", "options"):
				val = field.get(key)
				if isinstance(val, str):
					self.assertIsNone(
						FORBIDDEN_BRAND.search(val),
						f"security_deposit.json field {field.get('fieldname')}.{key} leaked vendor wording: {val!r}",
					)
		fields_by_name = {f["fieldname"]: f for f in doc["fields"]}

		for fname, ftype in (
			("preauth_hold", "Data"),
			("gateway_auth_ref", "Data"),
			("card_brand", "Data"),
			("card_last4", "Data"),
			("expires_on", "Datetime"),
			("authorized_amount", "Currency"),
		):
			self.assertIn(fname, fields_by_name, f"Missing field {fname} in security_deposit.json")
			self.assertEqual(fields_by_name[fname]["fieldtype"], ftype)
			self.assertIn(fname, doc["field_order"], f"Field {fname} missing from field_order")

		self.assertEqual(fields_by_name["card_last4"].get("length"), 4)
		statuses = fields_by_name["status"]["options"].splitlines()
		for required_status in (
			"Required",
			"Waived",
			"Authorized",
			"Collected",
			"Held",
			"Withheld",
			"Refunded",
			"Released",
			"Expired",
		):
			self.assertIn(required_status, statuses)

	def test_fail_closed_when_preauth_package_not_installed(self):
		self.site.preauth_installed = False
		with self.assertRaises(_ValidationError) as cm_auth:
			self.deposit.authorize_deposit(
				"RES-USD-1",
				250.00,
				gateway_auth_ref="AUTH-REF-1",
				card_last4="4242",
				card_brand="Visa",
				expires_on="2026-12-01 12:00:00",
			)
		self.assertIn("[HSP-DEP-PREAUTH-UNAVAILABLE]", str(cm_auth.exception))

		for fn, kwargs in (
			(self.deposit.increment_deposit_auth, {"reservation": "RES-USD-1", "additional_amount": 50.0}),
			(self.deposit.capture_deposit_auth, {"reservation": "RES-USD-1", "amount": 50.0, "reason": "Damage"}),
			(self.deposit.release_deposit_auth, {"reservation": "RES-USD-1", "reason": "Checkout"}),
		):
			with self.assertRaises(_ValidationError) as cm:
				fn(**kwargs)
			self.assertIn("[HSP-DEP-PREAUTH-UNAVAILABLE]", str(cm.exception))

		st = self.deposit.deposit_state("RES-USD-1")
		self.assertFalse(st["can_authorize"])
		self.assertFalse(st["can_increment"])
		self.assertFalse(st["can_capture"])
		self.assertFalse(st["can_release"])

	def test_authorize_increment_release_zero_gl_across_0_2_3dp(self):
		cases = [
			("RES-UGX-1", 500000, 100000, 600000, 499999, "UGX", "UGX-REF-1"),
			("RES-USD-1", 250.00, 50.00, 300.00, 249.99, "USD", "USD-REF-1"),
			("RES-KWD-1", 50.125, 10.375, 60.500, 50.124, "KWD", "KWD-REF-1"),
		]
		for res_name, initial_amt, inc_amt, total_amt, short_amt, curr, ref in cases:
			with self.subTest(currency=curr):
				# Sub-policy partial hold does not satisfy policy at exact 0/2/3dp precision
				st_short = self.deposit.authorize_deposit(
					res_name,
					short_amt,
					gateway_auth_ref=f"{ref}-SHORT",
					card_last4="4242",
					card_brand="Visa",
					expires_on="2026-12-05 12:00:00",
					pin="1234",
				)
				self.assertEqual(st_short["status"], "Authorized")
				self.assertAlmostEqual(st_short["authorized_amount"], short_amt, places=3)
				self.assertEqual(st_short["collected"], 0.0)
				self.assertEqual(st_short["held"], 0.0)
				self.assertEqual(st_short["card_brand"], "Visa")
				self.assertEqual(st_short["card_last4"], "4242")
				self.assertTrue(st_short["preauth_hold"].startswith("AUTH-2026-"))
				self.assertTrue(st_short["can_increment"])
				self.assertTrue(st_short["can_capture"])
				self.assertTrue(st_short["can_release"])
				self.assertFalse(st_short["can_authorize"])
				self.assertTrue(st_short["blocks_checkout"])
				self.assertFalse(
					self.deposit.deposit_satisfied(res_name),
					f"{curr} hold {short_amt} must NOT satisfy required {initial_amt}",
				)

				# Increment brings hold above policy -> now satisfied, still zero GL / PE / folio payment
				delta_to_total = round(total_amt - short_amt, ISO_PRECISION[curr])
				st_inc = self.deposit.increment_deposit_auth(
					res_name,
					delta_to_total,
					gateway_auth_ref=f"{ref}-INC",
					pin="1234",
				)
				self.assertEqual(st_inc["status"], "Authorized")
				self.assertAlmostEqual(st_inc["authorized_amount"], total_amt, places=3)
				self.assertTrue(self.deposit.deposit_satisfied(res_name))
				self.assertEqual(self.site.payment_entries, [])
				self.assertEqual(self.site.gl_entries, [])
				self.assertEqual(self.site.folio_payments, [])

				# Release clears hold, keeps 0 GL, clears blocks_checkout
				st_rel = self.deposit.release_deposit_auth(
					res_name,
					reason="Clean checkout",
					pin="1234",
				)
				self.assertEqual(st_rel["status"], "Released")
				self.assertFalse(st_rel["blocks_checkout"])
				self.assertFalse(st_rel["can_increment"])
				self.assertFalse(st_rel["can_capture"])
				self.assertFalse(st_rel["can_release"])
				self.assertEqual(self.site.payment_entries, [])
				self.assertEqual(self.site.gl_entries, [])

	def test_capture_posts_damage_charge_and_gl_only_on_capture_0_2_3dp(self):
		cases = [
			("RES-UGX-1", 500000, 500001, 150000, "UGX", "CAP-UGX-1"),
			("RES-USD-1", 250.00, 250.01, 120.55, "USD", "CAP-USD-1"),
			("RES-KWD-1", 50.125, 50.126, 25.125, "KWD", "CAP-KWD-1"),
		]
		for res_name, auth_amt, over_amt, cap_amt, curr, ref in cases:
			with self.subTest(currency=curr):
				before_charges = len(self.site.folio_charges)
				before_pe = len(self.site.payment_entries)
				before_gl = len(self.site.gl_entries)

				self.deposit.authorize_deposit(
					res_name,
					auth_amt,
					gateway_auth_ref=ref,
					card_last4="8811",
					card_brand="Mastercard",
					expires_on="2026-12-05 12:00:00",
					pin="1234",
				)
				self.assertEqual(len(self.site.payment_entries), before_pe)
				self.assertEqual(len(self.site.gl_entries), before_gl)

				# Over-capture rejected fail-closed with zero damage charge posted
				with self.assertRaises(_ValidationError) as cm_over:
					self.deposit.capture_deposit_auth(
						res_name,
						over_amt,
						reason="Excess damage attempt",
						pin="1234",
					)
				self.assertIn("[PREAUTH-OVER-CAPTURE]", str(cm_over.exception))
				self.assertEqual(len(self.site.folio_charges), before_charges)
				self.assertEqual(len(self.site.payment_entries), before_pe)

				# Exact capture posts damage Folio Charge + single submitted Payment Entry + GL
				st_cap = self.deposit.capture_deposit_auth(
					res_name,
					cap_amt,
					reason="Broken minibar glass",
					evidence="https://files.example.test/damage-1.jpg",
					pin="1234",
				)
				self.assertEqual(st_cap["status"], "Withheld")
				self.assertAlmostEqual(st_cap["withheld"], cap_amt, places=3)
				self.assertEqual(st_cap["held"], 0.0)
				self.assertFalse(st_cap["blocks_checkout"])
				self.assertEqual(len(self.site.folio_charges), before_charges + 1)
				self.assertIn("Broken minibar glass", self.site.folio_charges[-1]["description"])
				self.assertAlmostEqual(self.site.folio_charges[-1]["amount"], cap_amt, places=3)
				self.assertEqual(len(self.site.payment_entries), before_pe + 1)
				self.assertAlmostEqual(self.site.payment_entries[-1]["paid_amount"], cap_amt, places=3)
				self.assertEqual(len(self.site.gl_entries), before_gl + 2)
				self.assertTrue(st_cap.get("payment_entry", "").startswith("ACC-PAY-2026-"))

	def test_pci_guard_blocks_raw_pan_cvv_track_and_bad_last4(self):
		bad_cases = [
			{"gateway_auth_ref": "4111222233334444", "card_last4": "4242"},
			{"gateway_auth_ref": "AUTH-OK-1", "card_last4": "4111222233334444"},
			{"gateway_auth_ref": "AUTH-OK-2", "card_last4": "12345"},
			{"gateway_auth_ref": "AUTH CVV 123", "card_last4": "4242"},
			{"gateway_auth_ref": "%B4111222233334444^GUEST", "card_last4": "4242"},
		]
		for bad in bad_cases:
			with self.subTest(bad=bad):
				with self.assertRaises(_ValidationError) as cm:
					self.deposit.authorize_deposit(
						"RES-USD-1",
						250.00,
						gateway_auth_ref=bad["gateway_auth_ref"],
						card_last4=bad["card_last4"],
						card_brand="Visa",
						expires_on="2026-12-05 12:00:00",
					)
				self.assertIn("[PREAUTH-PCI-001]", str(cm.exception))
		self.assertEqual(self.site.bridge_calls, [], "PCI violations must fail before outbound bridge calls")

	def test_expired_hold_unsatisfies_deposit_and_clears_checkout_block(self):
		self.deposit.authorize_deposit(
			"RES-USD-1",
			250.00,
			gateway_auth_ref="AUTH-EXP-1",
			card_last4="4242",
			card_brand="Visa",
			expires_on="2026-11-26 12:00:00",
		)
		self.assertTrue(self.deposit.deposit_satisfied("RES-USD-1"))
		# Simulate scheduler marking the horizontal Credit Preauthorization Expired
		hold_name = next(iter(self.site.preauths))
		self.site.preauths[hold_name]["status"] = "Expired"
		self.site.preauths[hold_name]["remaining_hold_amount"] = 0.0

		self.assertFalse(self.deposit.deposit_satisfied("RES-USD-1"))
		st = self.deposit.deposit_state("RES-USD-1")
		self.assertEqual(st["status"], "Expired")
		self.assertFalse(st["blocks_checkout"])
		self.assertFalse(st["can_capture"])
		self.assertFalse(st["can_increment"])

	def test_cross_property_access_denied_and_pin_checked(self):
		self.site.allowed_properties = {"Kampala Grand"}
		for fn, kwargs in (
			(
				self.deposit.authorize_deposit,
				{
					"reservation": "RES-USD-1",
					"amount": 250.00,
					"gateway_auth_ref": "AUTH-XPROP",
					"card_last4": "4242",
					"card_brand": "Visa",
					"expires_on": "2026-12-05 12:00:00",
				},
			),
			(self.deposit.increment_deposit_auth, {"reservation": "RES-USD-1", "additional_amount": 25.00}),
			(self.deposit.capture_deposit_auth, {"reservation": "RES-USD-1", "amount": 50.00, "reason": "Damage"}),
			(self.deposit.release_deposit_auth, {"reservation": "RES-USD-1", "reason": "Checkout"}),
		):
			with self.assertRaises(_PermissionError):
				fn(**kwargs)

		# Allowed property enforces cashier PIN
		self.site.pin_checks.clear()
		self.deposit.authorize_deposit(
			"RES-UGX-1",
			500000,
			gateway_auth_ref="AUTH-PIN-1",
			card_last4="4242",
			card_brand="Visa",
			expires_on="2026-12-05 12:00:00",
			pin="9999",
		)
		self.assertIn(("Kampala Grand", "9999"), self.site.pin_checks)

	def test_endpoints_whitelisted_and_deposit_strings_white_label_clean(self):
		for fn_name in (
			"authorize_deposit",
			"increment_deposit_auth",
			"capture_deposit_auth",
			"release_deposit_auth",
		):
			fn = getattr(self.deposit, fn_name)
			self.assertTrue(getattr(fn, "_whitelisted", False), f"{fn_name} must be @frappe.whitelist")
			self.assertEqual(getattr(fn, "_whitelist_methods", None), ["POST"], f"{fn_name} must require POST")
			self.assertTrue(getattr(fn, "_kamra_roles", None), f"{fn_name} must enforce @require_roles")

		tree = ast.parse((KAMRA / "deposit.py").read_text(encoding="utf-8"))
		for node in ast.walk(tree):
			if isinstance(node, ast.Call):
				func = node.func
				is_msg = (
					(isinstance(func, ast.Name) and func.id == "_")
					or (isinstance(func, ast.Attribute) and func.attr in ("_", "throw", "msgprint"))
				)
				if is_msg:
					for sub in ast.walk(node):
						if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
							self.assertIsNone(
								FORBIDDEN_BRAND.search(sub.value),
								f"Forbidden vendor wording in kamra/deposit.py message: {sub.value!r}",
							)


if __name__ == "__main__":
	unittest.main()
