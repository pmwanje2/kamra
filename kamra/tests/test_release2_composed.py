"""Release 2 composed integration tests: #32 (discounts) x #33 (multicurrency rate matrix)
x #34 (pricing SSOT), plus hospitality-tariffs@11 guards.

Covers all 11 composed Release 2 scenarios end-to-end on a real Frappe + ERPNext site:
 1. Discount on a USD tariff stay sold in UGX (discount in USD -> FX -> whole UGX).
 2. Discount on a half-price derived plan (base_rate_plan with -50% modifier).
 3. FX relock at check-in (Fixed at Check In) with % and flat Amount discounts + Current Date posting.
 4. contract_tariff / HSP-TARIFF-007 / HSP-TARIFF-008 guard vs governed discount vs hand-typed rate.
 5. Rate change (set_agreed_rate), extend, shorten (amend_stay), split (split_charge); charged nights kept.
 6. proforma == folio == invoice == ledger (through public posting, payment and close APIs).
 7. (Printed tax invoice: see #48, not part of this branch.)
 8. OTA modify alert (channel_review_needed, reservation_discount, channel.modify_needs_review) on tariff stay.
 9. Room Types / Rate Calendar selling rate unaffected by a stay's discount.
10. Reservation currency options (Property:property:currency) with reference currency fields & zero INR leak.
11. Migration order & idempotency of all v48 patches on a populated site.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.utils import flt

from kamra import api, channel_manager, ledger, rates
from kamra import folio as folio_mod
from kamra.patches.v48 import (
	backfill_rate_plan_tariff_currency,
	configurable_masters,
	replace_untouched_india_defaults,
	seed_channel_ota_tax_rules,
)
from kamra.settings import clear_cache, write_kv
from kamra.tests.audit_fixtures import (
	TODAY,
	AuditTestCase,
	d,
	ensure_user,
	guest_folio,
	make_property,
	make_room,
	make_room_type,
)


def set_setting(prop: str, key: str, value) -> None:
	write_kv(prop, key, value)
	clear_cache(prop)
from kamra.tests.bridge_fields import ensure_bridge_field

PROP_R2 = "UT Rel2 UGX"
PROP_R2_VAT = "UT Rel2 UGX VAT"
DESK_USER = "rel2-desk@example.com"
RM_USER = "rel2-rm@example.com"
GM_USER = "rel2-gm@example.com"
GM_PIN = "4829"


# Faithful subset of hospitality-tariffs@11 `Kamra Tariff Resolve` (56199e91: HSP-TARIFF-006/007/008)
# covering direct, derived-plan (`base_rate_plan` chain), and repointed (`contract_tariff` snapshot)
# tariff stays so CI exercises the exact contract without depending on an external repo path.
TARIFF_GUARD_SCRIPT = """
utils = frappe.utils
user = frappe.session.user
held = [] if user == "Administrator" else frappe.get_all(
    "Has Role", filters={"parent": user, "parenttype": "User"}, pluck="role")
revenue = user == "Administrator" or bool([r for r in held if r in ("System Manager", "Hotel Admin", "Revenue Manager")])
old = None if doc.is_new() else doc.get_doc_before_save()
discount = doc.flags.get("kamra_reservation_discount")
relock = doc.flags.get("kamra_fx_checkin_relock")
contract_relock = doc.flags.get("kamra_contract_relocked")

def reasoned(d):
    cstr = frappe.utils.cstr
    flt = frappe.utils.flt
    if cstr(d.get("discount_reason")).strip():
        return True
    if d.get("discount_type") and flt(d.get("discount_value")) > 0:
        return False
    nights = list(d.get("night_rates") or []) + list(d.flags.get("kamra_initial_night_discounts") or [])
    disc = [n for n in nights if n.get("discount_type") and flt(n.get("discount_value")) > 0]
    return bool(disc) and not [n for n in disc if not cstr(n.get("discount_reason")).strip()]

def tariff_source(plan_name):
    if not plan_name:
        return None
    meta = frappe.get_meta("Rate Plan")
    has_base = meta.has_field("base_rate_plan")
    fields = ["name", "custom_tariff_price_list"]
    if has_base:
        fields.append("base_rate_plan")
    current = plan_name
    seen = []
    while current:
        if current in seen:
            frappe.throw("HSP-TARIFF-007: Rate Plan " + frappe.utils.cstr(plan_name)
                         + " has a cyclic base_rate_plan chain.")
        if len(seen) >= 5:
            frappe.throw("HSP-TARIFF-007: Rate Plan " + frappe.utils.cstr(plan_name)
                         + " derives through more than 5 plans.")
        seen.append(current)
        row = frappe.db.get_value("Rate Plan", current, fields, as_dict=True)
        if not row:
            return None
        if row.get("custom_tariff_price_list"):
            return row
        current = frappe.utils.cstr(row.get("base_rate_plan")).strip() if has_base else ""
    return None

def snapshot_contract(record):
    if not record:
        return None
    raw = record.get("contract_tariff")
    if isinstance(raw, str):
        if not raw.strip():
            return None
        try:
            raw = json.loads(raw)
        except Exception:
            frappe.throw("HSP-TARIFF-007: The stay's contract_tariff snapshot cannot be read.")
    if not isinstance(raw, dict):
        return None
    terms = raw.get("tariff")
    if not isinstance(terms, dict) or terms.get("source") == "channel":
        return None
    if terms.get("derived_from") or (terms.get("currency") and frappe.utils.flt(terms.get("nightly")) > 0):
        return raw
    return None

redirect = "HSP-TARIFF-008: A confirmed tariff stay is re-priced only by Move Room with a custom rate."
if old and old.get("custom_tariff_currency"):
    before = utils.flt(old.get("agreed_rate"))
    after = utils.flt(doc.agreed_rate)
    if utils.cint(doc.auto_price) != utils.cint(old.get("auto_price")):
        frappe.throw(redirect)
    if relock:
        contract = doc.get("contract_tariff")
        if isinstance(contract, str) and contract.strip():
            contract = json.loads(contract)
        tariff = (contract or {}).get("tariff") or {}
        if tariff.get("fx_rate"):
            doc.custom_fx_rate = utils.flt(tariff.get("fx_rate"), 9)
            doc.custom_fx_date = tariff.get("fx_date")
    elif after != before and doc.flags.get("kamra_agreed_rate_override"):
        desk = "Front Desk" in held
        if after <= 0 or not (revenue or (desk and after <= before)):
            frappe.throw(redirect)
    elif discount and after == before:
        pass
    elif after != before:
        moved = utils.cstr(doc.get("room")) != utils.cstr(old.get("room"))
        if not moved or after <= 0 or not revenue:
            frappe.throw(redirect)
elif relock and old:
    # Derived-plan check-in FX relock (v11 lines 303-323 / 587-596)
    tariff_source(old.get("rate_plan"))
    tariff_source(doc.get("rate_plan"))
    booked_plan = frappe.db.get_value(
        "Rate Plan", old.get("rate_plan"), ["custom_tariff_price_list", "fx_lock_policy"], as_dict=True
    ) or {}
    if (booked_plan.get("custom_tariff_price_list")
            or utils.cstr(booked_plan.get("fx_lock_policy")) != "Fixed at Check In"
            or utils.cint(old.get("fixed_rate")) or utils.cint(doc.get("fixed_rate"))
            or utils.cint(doc.auto_price) != utils.cint(old.get("auto_price"))
            or utils.flt(doc.agreed_rate) <= 0):
        frappe.throw(redirect)
else:
    plan = doc.rate_plan
    plan_tariff = tariff_source(plan) if plan else None
    old_tariff = tariff_source(old.get("rate_plan")) if (old and old.get("rate_plan")) else None
    old_contract = snapshot_contract(old) if old else None
    old_guarded = bool(old_tariff or old_contract)
    repointed_stay = bool(
        not plan_tariff and old_contract and old
        and utils.cstr(old.get("rate_plan")) == utils.cstr(doc.get("rate_plan"))
        and (utils.flt(old.get("agreed_rate")) > 0 or utils.flt(old_contract.get("agreed_rate")) > 0)
    )
    if (plan_tariff and doc.room_type) or repointed_stay:
        is_derived = (not plan_tariff) or (plan_tariff.get("name") != plan)
        plan_moved = not old or utils.cstr(old.get("rate_plan")) != utils.cstr(doc.rate_plan)
        manual = utils.flt(doc.agreed_rate) > 0 or not utils.cint(doc.auto_price)
        if is_derived and old and old_guarded and (
                utils.flt(old.get("agreed_rate")) > 0
                or (not plan_moved and old_contract and utils.flt(old_contract.get("agreed_rate")) > 0)):
            # Mirrors hospitality-tariffs@11 lines 629-726: existing derived/repointed tariff stay
            before = utils.flt(old.get("agreed_rate"))
            after = utils.flt(doc.agreed_rate)
            channel = bool(doc.flags.get("kamra_channel_tariff"))
            if not channel and utils.cint(doc.auto_price) != utils.cint(old.get("auto_price")):
                frappe.throw(redirect)
            if channel or contract_relock:
                pass
            elif after != before and doc.flags.get("kamra_agreed_rate_override"):
                desk = "Front Desk" in held
                if after <= 0 or not (revenue or (desk and after <= before)):
                    frappe.throw(redirect)
            elif discount and after == before:
                pass
            elif after != before:
                moved = utils.cstr(doc.get("room")) != utils.cstr(old.get("room"))
                if not moved or after <= 0 or not revenue:
                    frappe.throw(redirect)
        elif discount and doc.is_new():
            if not reasoned(doc):
                frappe.throw("HSP-TARIFF-007: A reservation discount needs a discount_reason.")
            if [n for n in (doc.get("night_rates") or []) if utils.flt(n.get("rate")) > 0 or utils.flt(n.get("hand_price")) > 0]:
                frappe.throw("HSP-TARIFF-007: Locked nights cannot take a tariff rate_plan.")
            if not is_derived:
                res = frappe.call("hospitality_tariffs.tariff_rate", property=doc.property, room_type=doc.room_type,
                                  rate_plan=plan, check_in_date=utils.cstr(doc.check_in_date),
                                  check_out_date=utils.cstr(doc.check_out_date), adults=utils.cint(doc.adults),
                                  children=utils.cint(doc.children), extra_beds=utils.cint(doc.get("extra_beds")))
                tariff = (res or {}).get("tariff") or {}
                if tariff.get("refusal"):
                    frappe.throw(tariff["refusal"])
                doc.agreed_rate = tariff.get("agreed_rate")
                doc.custom_tariff_currency = tariff.get("currency")
                doc.custom_tariff_nightly = tariff.get("nightly")
                doc.custom_fx_rate = tariff.get("fx_rate")
                doc.custom_fx_date = tariff.get("fx_date")
        elif manual and not revenue and not doc.flags.get("kamra_channel_tariff"):
            frappe.throw("HSP-TARIFF-007: Changing the price on a tariff-plan booking needs Revenue Manager or Hotel Admin.")
        elif not manual and doc.is_new() and not is_derived:
            res = frappe.call("hospitality_tariffs.tariff_rate", property=doc.property, room_type=doc.room_type,
                              rate_plan=plan, check_in_date=utils.cstr(doc.check_in_date),
                              check_out_date=utils.cstr(doc.check_out_date), adults=utils.cint(doc.adults),
                              children=utils.cint(doc.children), extra_beds=utils.cint(doc.get("extra_beds")))
            tariff = (res or {}).get("tariff") or {}
            if tariff.get("refusal"):
                frappe.throw(tariff["refusal"])
            doc.agreed_rate = tariff.get("agreed_rate")
            doc.custom_tariff_currency = tariff.get("currency")
            doc.custom_tariff_nightly = tariff.get("nightly")
            doc.custom_fx_rate = tariff.get("fx_rate")
            doc.custom_fx_date = tariff.get("fx_date")
"""


def _ensure_currency(code: str, symbol: str, fraction_units: int, number_format: str = "#,###.##"):
	if not frappe.db.exists("Currency", code):
		frappe.get_doc({
			"doctype": "Currency",
			"currency_name": code,
			"symbol": symbol,
			"fraction": "" if fraction_units == 1 else "Cent",
			"fraction_units": fraction_units,
			"number_format": number_format,
			"enabled": 1,
		}).insert(ignore_permissions=True)
	else:
		frappe.db.set_value("Currency", code, "enabled", 1)


def _ensure_custom_field(dt: str, fieldname: str, fieldtype: str, options: str = "", label: str = ""):
	if frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname}):
		return
	frappe.get_doc({
		"doctype": "Custom Field",
		"dt": dt,
		"fieldname": fieldname,
		"label": label or fieldname.replace("_", " ").title(),
		"fieldtype": fieldtype,
		"options": options,
	}).insert(ignore_permissions=True)
	frappe.clear_cache(doctype=dt)


def _set_fx(from_cur: str, to_cur: str, rate: float, on_date: str):
	frappe.db.delete("Currency Exchange", {
		"from_currency": from_cur, "to_currency": to_cur, "date": on_date, "for_selling": 1,
	})
	frappe.get_doc({
		"doctype": "Currency Exchange",
		"date": on_date,
		"from_currency": from_cur,
		"to_currency": to_cur,
		"exchange_rate": rate,
		"for_selling": 1,
		"for_buying": 0,
	}).insert(ignore_permissions=True)


class Release2ComposedTests(AuditTestCase):
	"""End-to-end composed verification of #32 x #33 x #34."""

	properties = (PROP_R2, PROP_R2_VAT)

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_bridge_field("Folio", "sales_invoice")
		_ensure_currency("UGX", "UGX ", 1, "#,###")
		_ensure_currency("USD", "$", 100, "#,###.##")
		_ensure_custom_field("Rate Plan", "custom_tariff_price_list", "Link", "Price List")
		_ensure_custom_field("Reservation", "custom_tariff_currency", "Link", "Currency")
		_ensure_custom_field("Reservation", "custom_tariff_nightly", "Currency", "custom_tariff_currency")
		_ensure_custom_field("Reservation", "custom_fx_rate", "Float")
		_ensure_custom_field("Reservation", "custom_fx_date", "Date")

		ensure_user(DESK_USER, ["Front Desk"])
		ensure_user(RM_USER, ["Revenue Manager", "Front Desk"])
		ensure_user(GM_USER, ["Hotel Admin", "Front Desk", "Finance"])
		frappe.db.delete("Cashier PIN", {"name": GM_USER})
		frappe.get_doc({"doctype": "Cashier PIN", "user": GM_USER, "pin": GM_PIN}).insert(
			ignore_permissions=True,
		)
		frappe.db.set_single_value("Accounts Settings", "stale_days", 7)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger", "kamra.pricing")
		frappe.db.delete("Server Script", {"name": ("like", "UT Rel2%")})
		frappe.db.delete("Server Script", {"api_method": "hospitality_tariffs.tariff_rate"})
		frappe.db.delete("Currency Exchange", {"from_currency": "USD", "to_currency": "UGX"})
		frappe.client_cache.delete_value("server_script_map")

		# Property 1: UGX bridged style (0% Kamra GST, rates_include_tax=1)
		make_property(
			PROP_R2, business_date=TODAY, country="Uganda", currency="UGX",
			gst_mode="Fixed", gst_rate_low=0, gst_rate_high=0, rates_include_tax=1,
		)
		# Property 2: UGX tax-exclusive 18% VAT (document-level rounding test)
		make_property(
			PROP_R2_VAT, business_date=TODAY, country="Uganda", currency="UGX",
			gst_mode="Fixed", gst_rate_low=18, gst_rate_high=18, rates_include_tax=0,
		)
		for prop in (PROP_R2, PROP_R2_VAT):
			frappe.db.delete("Property Setting", {"property": prop})
			clear_cache(prop)
			set_setting(prop, "rate_override_limit_pct", 10)
			write_kv(prop, "charged_night_rate_change", "Keep charged nights")
			clear_cache(prop)

		self.rt_bridged = make_room_type(PROP_R2, "DLX", base_price=0, tax_percent=0)
		frappe.db.set_value("Room Type", self.rt_bridged, {"base_price": 0, "tax_percent": 0})
		self.rooms_bridged = [make_room(PROP_R2, self.rt_bridged, f"R10{i}") for i in range(1, 6)]

		self.rt_vat = make_room_type(PROP_R2_VAT, "VDLX", base_price=0, tax_percent=18)
		frappe.db.set_value("Room Type", self.rt_vat, {"base_price": 0, "tax_percent": 18})
		self.rooms_vat = [make_room(PROP_R2_VAT, self.rt_vat, f"V10{i}") for i in range(1, 5)]

		if not frappe.db.exists("Price List", "UT Rel2 USD PL"):
			frappe.get_doc({
				"doctype": "Price List",
				"price_list_name": "UT Rel2 USD PL",
				"currency": "USD",
				"selling": 1,
				"enabled": 1,
			}).insert(ignore_permissions=True)

		ledger.ensure_default_reason_codes()
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture

	def tearDown(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		frappe.db.delete("Server Script", {"name": ("like", "UT Rel2%")})
		frappe.db.delete("Server Script", {"api_method": "hospitality_tariffs.tariff_rate"})
		frappe.client_cache.delete_value("server_script_map")
		super().tearDown()

	def _make_plan(self, prop: str, code: str, *, currency: str | None = "USD",
	               fx_lock_policy: str = "Fixed at Reservation", base_rate_plan: str | None = None,
	               modifier_type: str = "Percent", modifier_value: float = 0,
	               discount_eligible: int = 1, is_default: int = 0,
	               rounding_step: float = 0, rounding_mode: str = "Nearest",
	               custom_tariff_price_list: str | None = None) -> str:
		frappe.db.delete("Rate Plan", {"property": prop, "code": code})
		doc = frappe.get_doc({
			"doctype": "Rate Plan",
			"property": prop,
			"rate_plan_name": f"{code} Plan",
			"code": code,
			"currency": currency,
			"fx_lock_policy": fx_lock_policy,
			"base_rate_plan": base_rate_plan,
			"modifier_type": modifier_type,
			"modifier_value": modifier_value,
			"discount_eligible": discount_eligible,
			"is_default": is_default,
			"rounding_step": rounding_step,
			"rounding_mode": rounding_mode,
			"custom_tariff_price_list": custom_tariff_price_list,
		}).insert(ignore_permissions=True)
		return doc.name

	def _as(self, user: str, fn, *args, **kwargs):
		prev = frappe.session.user
		frappe.set_user(user)  # nosemgrep: frappe-setuser -- test fixture
		try:
			return fn(*args, **kwargs)
		finally:
			frappe.set_user(prev)  # nosemgrep: frappe-setuser -- test fixture

	def _install_tariff_api(self, plan_tariffs: dict[str, dict]):
		"""Install a Server Script for hospitality_tariffs.tariff_rate mapping rate_plan -> tariff dict."""
		frappe.db.delete("Server Script", {"api_method": "hospitality_tariffs.tariff_rate"})
		payload = json.dumps(plan_tariffs)
		script = (
			f"mapping = json.loads({payload!r})\n"
			"rp = frappe.form_dict.get('rate_plan')\n"
			"entry = mapping.get(rp)\n"
			"if not entry:\n"
			"    t = {'is_tariff': 0}\n"
			"else:\n"
			"    t = dict(entry)\n"
			"    t.setdefault('is_tariff', 1)\n"
			"frappe.flags.tariff = t\n"
			"frappe.response['message'] = {'tariff': t}\n"
		)
		frappe.get_doc({
			"doctype": "Server Script",
			"name": "UT Rel2 Tariff Rate",
			"script_type": "API",
			"api_method": "hospitality_tariffs.tariff_rate",
			"script": script,
		}).insert(ignore_permissions=True)
		frappe.client_cache.delete_value("server_script_map")

	def _install_tariff_guard(self):
		frappe.db.delete("Server Script", {"name": "UT Rel2 Tariff Resolve"})
		frappe.get_doc({
			"doctype": "Server Script",
			"name": "UT Rel2 Tariff Resolve",
			"script_type": "DocType Event",
			"reference_doctype": "Reservation",
			"doctype_event": "Before Validate",
			"script": TARIFF_GUARD_SCRIPT,
		}).insert(ignore_permissions=True)
		frappe.client_cache.delete_value("server_script_map")

	# ── Case 1: USD tariff stay sold in UGX + discount order & rounding ──────

	def test_01_usd_tariff_discount_in_usd_then_fx_then_whole_ugx_and_rediscount_parity(self):
		"""Discount comes off the tariff's USD currency first, then FX, then whole-shilling
		rounding — and create_booking with discount agrees shilling-for-shilling with
		set_reservation_discount after booking."""
		plan_vat = self._make_plan(PROP_R2_VAT, "CPUSD", is_default=1)
		plan_br = self._make_plan(PROP_R2, "CP75", is_default=1)
		self._install_tariff_api({
			# $150 USD @ 3,655 = 548,250 UGX. 15% off USD = $127.50 -> * 3,655 = 466,012.50 -> 466,013 UGX
			plan_vat: {
				"currency": "USD", "nightly": 150.0, "fx_rate": 3655.0,
				"fx_date": TODAY, "agreed_rate": 548250.0, "property_currency": "UGX",
			},
			# $75 USD @ 3,712.5 = 278,438 UGX. 10% off USD = $67.50 -> * 3,712.5 = 250,593.75 -> 250,594 UGX
			# (Discounting already-converted 278,438 by 10% would yield 250,594.20!)
			plan_br: {
				"currency": "USD", "nightly": 75.0, "fx_rate": 3712.5,
				"fx_date": TODAY, "agreed_rate": 278438.0, "property_currency": "UGX",
			},
		})

		# A) Bridged 0%-tax UGX: $75 @ 3,712.5 less 10% = $67.50 * 3,712.5 -> 250,594 whole UGX
		q_br = self._as(
			DESK_USER, api.get_quote, PROP_R2, self.rt_bridged, d(5), d(7),
			rate_plan=plan_br, discount_type="Percent", discount_value=10,
			discount_reason="RATE-PROMO",
		)
		self.assertEqual(q_br["nightly"][0]["rate"], 250594.0)
		self.assertEqual(q_br["nightly"][0]["discount_amount"], 27844.0)
		self.assertEqual(q_br["room_total"], 501188.0)
		self.assertEqual(q_br["amount_after_tax"], 501188.0)

		# B) Tax-exclusive 18% VAT UGX: $150 @ 3,655 less 15% = $127.50 * 3,655 -> 466,013/night
		# 2 nights: 932,026 net; 18% VAT = 167,764.68; document total = 1,099,791 (tax = 167,765)
		q_vat = self._as(
			RM_USER, api.get_quote, PROP_R2_VAT, self.rt_vat, d(5), d(7),
			rate_plan=plan_vat, discount_type="Percent", discount_value=15,
			discount_reason="RATE-PROMO",
		)
		self.assertEqual(q_vat["nightly"][0]["rate"], 466013.0)
		self.assertEqual(q_vat["nightly"][0]["discount_amount"], 82237.0)
		self.assertEqual(q_vat["room_total"], 932026.0)
		self.assertEqual(q_vat["amount_before_tax"], 932026.0)
		self.assertEqual(q_vat["tax_amount"], 167765.0)
		self.assertEqual(q_vat["amount_after_tax"], 1099791.0)

		# Book directly WITH 15% discount
		b1 = self._as(
			RM_USER, api.create_booking, PROP_R2_VAT, self.rt_vat, d(5), d(7), "Direct Disc Guest",
			room=self.rooms_vat[0], rate_plan=plan_vat,
			discount_type="Percent", discount_value=15, discount_reason="RATE-PROMO",
		)
		res1 = frappe.get_doc("Reservation", b1["reservation"])
		self.assertEqual(flt(res1.agreed_rate), 548250.0)
		self.assertEqual([flt(r.rate) for r in res1.night_rates], [466013.0, 466013.0])
		self.assertEqual([flt(r.discount_amount) for r in res1.night_rates], [82237.0, 82237.0])
		self.assertEqual(flt(res1.amount_before_tax), 932026.0)
		self.assertEqual(flt(res1.tax_amount), 167765.0)
		self.assertEqual(flt(res1.amount_after_tax), 1099791.0)

		# Book WITHOUT discount, then apply 15% via set_reservation_discount (rediscount_locked_nights)
		b2 = self._as(
			RM_USER, api.create_booking, PROP_R2_VAT, self.rt_vat, d(5), d(7), "Post Disc Guest",
			room=self.rooms_vat[1], rate_plan=plan_vat,
		)
		self._as(
			RM_USER, api.set_reservation_discount, b2["reservation"],
			discount_type="Percent", discount_value=15, reason_code="RATE-PROMO",
		)
		res2 = frappe.get_doc("Reservation", b2["reservation"])
		self.assertEqual([flt(r.rate) for r in res2.night_rates], [466013.0, 466013.0])
		self.assertEqual([flt(r.discount_amount) for r in res2.night_rates], [82237.0, 82237.0])
		self.assertEqual(flt(res2.amount_before_tax), 932026.0)
		self.assertEqual(flt(res2.tax_amount), 167765.0)
		self.assertEqual(flt(res2.amount_after_tax), 1099791.0)

	# ── Case 2: Discount on a half-price derived plan ────────────────────────

	def test_02_discount_on_half_price_derived_usd_plan(self):
		"""A derived -50% plan over a USD base tariff discounts its derived USD nightly
		($150 -> $75 -> less 10% = $67.50) before FX and whole-shilling rounding."""
		base = self._make_plan(PROP_R2, "CPBASE", currency="USD", is_default=1)
		half = self._make_plan(
			PROP_R2, "CPHALF", currency="USD", base_rate_plan=base,
			modifier_type="Percent", modifier_value=-50, discount_eligible=1,
		)
		self._install_tariff_api({
			base: {
				"currency": "USD", "nightly": 150.0, "fx_rate": 3712.5,
				"fx_date": TODAY, "agreed_rate": 556875.0, "property_currency": "UGX",
			},
		})
		q = self._as(
			DESK_USER, api.get_quote, PROP_R2, self.rt_bridged, d(5), d(7),
			rate_plan=half, discount_type="Percent", discount_value=10,
			discount_reason="RATE-PROMO",
		)
		self.assertEqual(q["tariff"]["nightly"], 75.0)
		self.assertEqual(q["tariff"]["derived_from"], base)
		self.assertEqual(q["agreed_rate"], 278438.0)
		self.assertEqual(q["nightly"][0]["rate"], 250594.0)
		self.assertEqual(q["nightly"][0]["discount_amount"], 27844.0)
		self.assertEqual(q["amount_after_tax"], 501188.0)

		booked = self._as(
			DESK_USER, api.create_booking, PROP_R2, self.rt_bridged, d(5), d(7), "Half Plan Guest",
			room=self.rooms_bridged[0], rate_plan=half,
			discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO",
		)
		res = frappe.get_doc("Reservation", booked["reservation"])
		self.assertEqual(flt(res.agreed_rate), 278438.0)
		self.assertEqual([flt(r.rate) for r in res.night_rates], [250594.0, 250594.0])
		self.assertEqual(flt(res.amount_after_tax), 501188.0)

	# ── Case 3a: FX relock at check-in (Fixed at Check In) with a discount ──

	def test_03a_fx_relock_at_check_in_with_percent_flat_and_derived_discounts(self):
		"""Fixed at Check In relocks at check-in business-date FX without hitting
		guard_agreed_rate or re-demanding supervisor PIN: % discount re-applies on
		new FX, flat Amount keeps its UGX currency meaning."""
		plan_chk = self._make_plan(
			PROP_R2, "CPCHK", currency="USD", fx_lock_policy="Fixed at Check In",
			custom_tariff_price_list="UT Rel2 USD PL",
		)
		plan_half_chk = self._make_plan(
			PROP_R2, "CPHCHK", currency="USD", fx_lock_policy="Fixed at Check In",
			base_rate_plan=plan_chk, modifier_type="Percent", modifier_value=-50,
		)
		self._install_tariff_api({
			plan_chk: {
				"currency": "USD", "nightly": 150.0, "fx_rate": 3655.0,
				"fx_date": TODAY, "agreed_rate": 548250.0, "property_currency": "UGX",
			},
		})
		self._install_tariff_guard()
		_set_fx("USD", "UGX", 3655.0, TODAY)

		# 1. Percent 20% (> 10% desk limit) approved by supervisor PIN at booking
		b_pct = self._as(
			DESK_USER, api.create_booking, PROP_R2, self.rt_bridged, d(0), d(2), "Relock Pct Guest",
			room=self.rooms_bridged[0], rate_plan=plan_chk,
			discount_type="Percent", discount_value=20, discount_reason="RATE-PROMO",
			supervisor_user=GM_USER, supervisor_pin=GM_PIN,
		)
		# 2. Flat UGX 50,000 off (within 10% desk limit: 50,000 / 548,250 = 9.12%)
		b_amt = self._as(
			DESK_USER, api.create_booking, PROP_R2, self.rt_bridged, d(0), d(2), "Relock Flat Guest",
			room=self.rooms_bridged[1], rate_plan=plan_chk,
			discount_type="Amount", discount_value=50000, discount_reason="RATE-PROMO",
		)
		# 3. Derived -50% plan with 10% discount
		b_drv = self._as(
			DESK_USER, api.create_booking, PROP_R2, self.rt_bridged, d(0), d(2), "Relock Derived Guest",
			room=self.rooms_bridged[2], rate_plan=plan_half_chk,
			discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO",
		)

		# New FX published at check-in: 3,700 UGX/USD ($150 -> 555,000; $75 -> 277,500)
		_set_fx("USD", "UGX", 3700.0, TODAY)

		# Front Desk checks all 3 in with NO supervisor PIN needed
		self._as(DESK_USER, api.check_in, b_pct["reservation"])
		self._as(DESK_USER, api.check_in, b_amt["reservation"])
		self._as(DESK_USER, api.check_in, b_drv["reservation"])

		r_pct = frappe.get_doc("Reservation", b_pct["reservation"])
		self.assertEqual(r_pct.status, "Checked In")
		self.assertEqual(flt(r_pct.agreed_rate), 555000.0)
		# $150 * 0.80 = $120 * 3,700 = 444,000; discount = 111,000
		self.assertEqual([flt(r.rate) for r in r_pct.night_rates], [444000.0, 444000.0])
		self.assertEqual([flt(r.discount_amount) for r in r_pct.night_rates], [111000.0, 111000.0])
		self.assertEqual(flt(r_pct.amount_after_tax), 888000.0)

		r_amt = frappe.get_doc("Reservation", b_amt["reservation"])
		self.assertEqual(flt(r_amt.agreed_rate), 555000.0)
		# Flat UGX 50,000 keeps its UGX meaning: 555,000 - 50,000 = 505,000
		self.assertEqual([flt(r.rate) for r in r_amt.night_rates], [505000.0, 505000.0])
		self.assertEqual([flt(r.discount_amount) for r in r_amt.night_rates], [50000.0, 50000.0])
		self.assertEqual(flt(r_amt.amount_after_tax), 1010000.0)

		r_drv = frappe.get_doc("Reservation", b_drv["reservation"])
		self.assertEqual(flt(r_drv.agreed_rate), 277500.0)
		# $75 * 0.90 = $67.50 * 3,700 = 249,750; discount = 27,750
		self.assertEqual([flt(r.rate) for r in r_drv.night_rates], [249750.0, 249750.0])
		self.assertEqual([flt(r.discount_amount) for r in r_drv.night_rates], [27750.0, 27750.0])
		self.assertEqual(flt(r_drv.amount_after_tax), 499500.0)

	# ── Case 3b: Current Date posting with a discount ────────────────────────

	def test_03b_current_date_posting_with_percent_and_flat_amount_discounts(self):
		"""Current Date posting re-applies % discount in USD before posting FX and keeps
		flat UGX Amount fixed in UGX; folio line description matches the posted FX amounts."""
		plan_cur = self._make_plan(PROP_R2, "CPCUR", currency="USD", fx_lock_policy="Current Date")
		self._install_tariff_api({
			plan_cur: {
				"currency": "USD", "nightly": 150.0, "fx_rate": 3655.0,
				"fx_date": TODAY, "agreed_rate": 548250.0, "property_currency": "UGX",
			},
		})
		_set_fx("USD", "UGX", 3655.0, TODAY)

		b_pct = self._as(
			DESK_USER, api.create_booking, PROP_R2, self.rt_bridged, d(0), d(2), "Cur Pct Guest",
			room=self.rooms_bridged[0], rate_plan=plan_cur,
			discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO",
		)
		b_amt = self._as(
			DESK_USER, api.create_booking, PROP_R2, self.rt_bridged, d(0), d(2), "Cur Amt Guest",
			room=self.rooms_bridged[1], rate_plan=plan_cur,
			discount_type="Amount", discount_value=50000, discount_reason="RATE-PROMO",
		)
		self._as(DESK_USER, api.check_in, b_pct["reservation"])
		self._as(DESK_USER, api.check_in, b_amt["reservation"])

		# Posting date FX moves to 3,700 UGX/USD
		_set_fx("USD", "UGX", 3700.0, TODAY)
		folio_mod.post_room_night(frappe.get_doc("Reservation", b_pct["reservation"]), d(0))
		folio_mod.post_room_night(frappe.get_doc("Reservation", b_amt["reservation"]), d(0))

		line_pct = next(
			c for c in frappe.get_doc("Folio", guest_folio(b_pct["reservation"])).charges
			if c.charge_type == "Room"
		)
		self.assertEqual(flt(line_pct.amount), 499500.0)
		self.assertEqual(flt(line_pct.contract_fx_rate), 3700.0)
		self.assertIn("555,000 less discount 55,500", line_pct.description)
		self.assertNotIn("548,250", line_pct.description)

		line_amt = next(
			c for c in frappe.get_doc("Folio", guest_folio(b_amt["reservation"])).charges
			if c.charge_type == "Room"
		)
		# Flat UGX 50,000 off 555,000 = 505,000 (NOT scaled by FX to 504,384)
		self.assertEqual(flt(line_amt.amount), 505000.0)
		self.assertEqual(flt(line_amt.contract_fx_rate), 3700.0)
		self.assertIn("555,000 less discount 50,000", line_amt.description)

	# ── Case 4: HSP-TARIFF-007 / 008 vs governed discount vs hand-typed rate ─

	def test_04_tariff_guard_allows_governed_discount_and_refuses_hand_typed_rate(self):
		"""With hospitality-tariffs@11 guard active: governed booking & post-booking discounts
		pass cleanly; hand-typed night_prices and direct agreed_rate/auto_price/discount writes fail."""
		plan = self._make_plan(
			PROP_R2, "CPGRD", currency="USD", custom_tariff_price_list="UT Rel2 USD PL",
		)
		self._install_tariff_api({
			plan: {
				"currency": "USD", "nightly": 150.0, "fx_rate": 3655.0,
				"fx_date": TODAY, "agreed_rate": 548250.0, "property_currency": "UGX",
			},
		})
		self._install_tariff_guard()

		# Governed booking with discount -> allowed
		b = self._as(
			DESK_USER, api.create_booking, PROP_R2, self.rt_bridged, d(5), d(7), "Guard OK Guest",
			room=self.rooms_bridged[0], rate_plan=plan,
			discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO",
		)
		res = frappe.get_doc("Reservation", b["reservation"])
		self.assertEqual(res.custom_tariff_currency, "USD")
		self.assertEqual(flt(res.custom_tariff_nightly), 150.0)
		self.assertEqual(flt(res.agreed_rate), 548250.0)
		self.assertEqual([flt(r.rate) for r in res.night_rates], [493425.0, 493425.0])

		# Governed post-booking discount change -> allowed
		self._as(
			DESK_USER, api.set_reservation_discount, res.name,
			discount_type="Percent", discount_value=5, reason_code="RATE-PROMO",
		)
		res.reload()
		self.assertEqual(flt(res.agreed_rate), 548250.0)
		self.assertEqual([flt(r.rate) for r in res.night_rates], [520838.0, 520838.0])

		# Hand-typed night_prices on a tariff plan -> refused
		with self.assertRaises(frappe.ValidationError):
			self._as(
				RM_USER, api.create_booking, PROP_R2, self.rt_bridged, d(8), d(9), "Hand Typed",
				room=self.rooms_bridged[1], rate_plan=plan,
				night_prices=[{"date": d(8), "price": 400000}],
			)

		# Direct agreed_rate edit -> refused by HSP-TARIFF-008
		with self.assertRaisesRegex(frappe.ValidationError, "HSP-TARIFF-008"):
			tampered = frappe.get_doc("Reservation", res.name)
			tampered.agreed_rate = 400000
			self._as(DESK_USER, tampered.save, ignore_permissions=True)

		# Direct auto_price flip -> refused by HSP-TARIFF-008
		with self.assertRaisesRegex(frappe.ValidationError, "HSP-TARIFF-008"):
			tampered2 = frappe.get_doc("Reservation", res.name)
			tampered2.auto_price = 0
			self._as(DESK_USER, tampered2.save, ignore_permissions=True)

		# Direct discount write bypassing governed action -> refused by guard_discount_inputs
		with self.assertRaises(frappe.PermissionError):
			tampered3 = frappe.get_doc("Reservation", res.name)
			tampered3.discount_value = 25
			self._as(DESK_USER, tampered3.save, ignore_permissions=True)

	# ── Case 5: set_agreed_rate, extend, shorten, split; charged nights kept ─

	def test_05_rate_change_extend_shorten_split_keep_charged_nights(self):
		"""Extend, shorten (including over a multi-rate tariff change), set_agreed_rate and
		split_charge all preserve charged nights and ledger balance on a discounted USD tariff stay."""
		plan = self._make_plan(PROP_R2, "CPOPS", currency="USD")
		# Dynamic tariff: d(0), d(1) are $150 (548,250); d(2) is $180 (657,900)
		frappe.db.delete("Server Script", {"api_method": "hospitality_tariffs.tariff_rate"})
		script = f"""
rp = frappe.form_dict.get('rate_plan')
if rp != {plan!r}:
    frappe.response['message'] = {{'tariff': {{'is_tariff': 0}}}}
else:
    ci = frappe.utils.getdate(frappe.form_dict.get('check_in'))
    co = frappe.utils.getdate(frappe.form_dict.get('check_out'))
    n = max(frappe.utils.date_diff(co, ci), 1)
    nights = []
    for i in range(n):
        day = str(frappe.utils.add_days(ci, i))
        usd = 180.0 if day >= {d(2)!r} else 150.0
        ugx = 657900.0 if day >= {d(2)!r} else 548250.0
        nights.append({{'date': day, 'nightly': usd, 'agreed_rate': ugx, 'item_price': 'IP-' + day}})
    frappe.response['message'] = {{'tariff': {{
        'is_tariff': 1, 'currency': 'USD', 'nightly': nights[0]['nightly'],
        'agreed_rate': nights[0]['agreed_rate'], 'fx_rate': 3655.0,
        'fx_date': {TODAY!r}, 'property_currency': 'UGX', 'nights': nights,
    }}}}
"""
		frappe.get_doc({
			"doctype": "Server Script",
			"name": "UT Rel2 Multi Tariff",
			"script_type": "API",
			"api_method": "hospitality_tariffs.tariff_rate",
			"script": script,
		}).insert(ignore_permissions=True)
		frappe.client_cache.delete_value("server_script_map")

		b = self._as(
			DESK_USER, api.create_booking, PROP_R2, self.rt_bridged, d(0), d(2), "Ops Guest",
			room=self.rooms_bridged[0], rate_plan=plan,
			discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO",
		)
		res_name = b["reservation"]
		self._as(DESK_USER, api.check_in, res_name)
		folio_mod.post_room_night(frappe.get_doc("Reservation", res_name), d(0))

		# 1. Extend to d(3) (adds $180 night -> less 10% = $162 * 3,655 = 592,110)
		pv_ext = self._as(DESK_USER, api.amend_stay_preview, res_name, d(0), d(3))
		self.assertEqual(pv_ext["new_amount"], 493425.0 + 493425.0 + 592110.0)
		self._as(DESK_USER, api.amend_stay, res_name, d(0), d(3), confirm_price=1)
		res = frappe.get_doc("Reservation", res_name)
		self.assertEqual([flt(r.rate) for r in res.night_rates], [493425.0, 493425.0, 592110.0])

		# 2. Shorten back to d(2)
		self._as(DESK_USER, api.amend_stay, res_name, d(0), d(2), confirm_price=1)
		res.reload()
		self.assertEqual([flt(r.rate) for r in res.night_rates], [493425.0, 493425.0])

		# 3. Shorten a pre-arrival multi-rate booking by moving check-in to the $180 night
		#    (triggers reprice_contract_nights changing agreed_rate -> must not trip guard_agreed_rate)
		b_shift = self._as(
			DESK_USER, api.create_booking, PROP_R2, self.rt_bridged, d(1), d(3), "Shift Guest",
			room=self.rooms_bridged[1], rate_plan=plan,
			discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO",
		)
		self._as(DESK_USER, api.amend_stay, b_shift["reservation"], d(2), d(3), confirm_price=1)
		shifted = frappe.get_doc("Reservation", b_shift["reservation"])
		self.assertEqual(flt(shifted.agreed_rate), 657900.0)
		self.assertEqual([flt(r.rate) for r in shifted.night_rates], [592110.0])

		# 4. set_agreed_rate on the in-house stay: replaces discount on uncharged Night 2, keeps Night 1
		self._as(
			RM_USER, api.set_agreed_rate, res_name, 470000.0, "RATE-PROMO",
			reason="Corporate match", confirm_price=1,
		)
		res.reload()
		self.assertEqual(int(res.fixed_rate), 1)
		self.assertEqual([flt(r.rate) for r in res.night_rates], [493425.0, 470000.0])
		self.assertEqual(flt(res.night_rates[0].discount_amount), 54825.0)

		# 5. split_charge on charged Night 1 across Guest + Extra folio -> exact sum & ledger balanced
		g_folio = guest_folio(res_name)
		e_folio = folio_mod.split_folio(res_name, "Extra")
		room_row = next(c for c in frappe.get_doc("Folio", g_folio).charges if c.charge_type == "Room")
		folio_mod.split_charge(g_folio, room_row.name, e_folio, percent=50)
		fg = frappe.get_doc("Folio", g_folio)
		fe = frappe.get_doc("Folio", e_folio)
		self.assertEqual(flt(fg.grand_total) + flt(fe.grand_total), 493425.0)
		self.assertIn(d(0), folio_mod.charged_nights(res_name))
		self.assertEqual(folio_mod.charged_nights(res_name)[d(0)]["base"], 493425.0)
		recon = ledger.folio_reconciliation(PROP_R2)
		self.assertTrue(recon["in_balance"], recon)

	# ── Case 6: proforma == folio == invoice == ledger ───────────────────────

	def test_06_proforma_equals_folio_equals_invoice_equals_ledger_with_discount(self):
		"""Quote, reservation, mid-stay proforma, checkout folio, invoice and Guest ledger
		all agree to the whole shilling on a discounted derived USD stay with 18% VAT."""
		base = self._make_plan(PROP_R2_VAT, "VBASE", currency="USD")
		half = self._make_plan(
			PROP_R2_VAT, "VHALF", currency="USD", base_rate_plan=base,
			modifier_type="Percent", modifier_value=-50,
		)
		self._install_tariff_api({
			base: {
				"currency": "USD", "nightly": 150.0, "fx_rate": 3712.5,
				"fx_date": TODAY, "agreed_rate": 556875.0, "property_currency": "UGX",
			},
		})
		# $75 less 10% = $67.50 * 3,712.5 = 250,593.75 -> 250,594 UGX/night
		# 2 nights = 501,188 net; 18% VAT = 90,213.84 -> total = 591,402 (tax = 90,214)
		q = self._as(
			DESK_USER, api.get_quote, PROP_R2_VAT, self.rt_vat, d(0), d(2),
			rate_plan=half, discount_type="Percent", discount_value=10,
			discount_reason="RATE-PROMO",
		)
		self.assertEqual(q["amount_after_tax"], 591402.0)

		b = self._as(
			DESK_USER, api.create_booking, PROP_R2_VAT, self.rt_vat, d(0), d(2), "Reconcile Guest",
			room=self.rooms_vat[0], rate_plan=half,
			discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO",
		)
		res_name = b["reservation"]
		res = frappe.get_doc("Reservation", res_name)
		self.assertEqual(flt(res.amount_after_tax), 591402.0)

		self._as(DESK_USER, api.check_in, res_name)
		folio_mod.post_room_night(frappe.get_doc("Reservation", res_name), d(0))
		fname = guest_folio(res_name)

		# Mid-stay proforma == quote == booked total
		pf = self._as(DESK_USER, ledger.generate_proforma, fname)["invoice"]
		self.assertEqual(flt(pf["estimate"]["grand_total"]), 591402.0)
		self.assertEqual(flt(pf["estimate"]["balance"]), 591402.0)

		# Post remaining night -> folio == quote == proforma
		folio_mod.post_remaining_nights(frappe.get_doc("Reservation", res_name))
		fdoc = frappe.get_doc("Folio", fname)
		self.assertEqual(flt(fdoc.charges_total), 501188.0)
		self.assertEqual(flt(fdoc.tax_total), 90214.0)
		self.assertEqual(flt(fdoc.grand_total), 591402.0)

		# Ledger reconciliation in balance via normal posting path (no internal rounding calls)
		recon = ledger.folio_reconciliation(PROP_R2_VAT)
		self.assertTrue(recon["in_balance"], recon)

		# Pay exact whole shillings through public API & close -> invoice agrees
		set_setting(PROP_R2_VAT, "use_cashier_tills", 0)
		self._as(GM_USER, api.add_folio_payment, fname, "Cash", 591402.0, pin=GM_PIN)
		folio_mod.close_folio(fname)

		inv = self._as(GM_USER, api.folio_invoice, fname)
		self.assertEqual(flt(inv["folio"]["grand_total"]), 591402.0)
		self.assertEqual(flt(inv["folio"]["tax_total"]), 90214.0)
		self.assertEqual(flt(inv["folio"]["balance"]), 0.0)
		self.assertEqual(flt(inv["tax_summary"][0]["total_tax"]), 90214.0)
		self.assertTrue(ledger.folio_reconciliation(PROP_R2_VAT)["in_balance"])

	def test_06b_closed_folio_totals_frozen_and_zero_tax_bridged_stay_skips_document_rounding(self):
		"""#33 R9 composed with #32: (a) when tax == 0 (ERPNext-bridged UGX stay where the
		accounting invoice owns tax and rounding), quote, booking, rediscount_locked_nights,
		proforma and open folio skip document rounding so charges_total == grand_total == sum(lines);
		(b) once a folio is closed/invoiced, its charges_total, tax_total and grand_total stay frozen."""
		plan = self._make_plan(PROP_R2, "CPBR0", currency="USD")
		self._install_tariff_api({
			plan: {
				"currency": "USD", "nightly": 150.0, "fx_rate": 3700.0,
				"fx_date": TODAY, "agreed_rate": 555000.0, "property_currency": "UGX",
			},
		})
		# 0%-tax bridged UGX stay with a fractional extra-guest charge (12,345.50/night)
		b = self._as(
			DESK_USER, api.create_booking, PROP_R2, self.rt_bridged, d(0), d(1), "ZeroTax Bridge Guest",
			room=self.rooms_bridged[0], rate_plan=plan,
			over_capacity_charge=12345.50,
		)
		res_name = b["reservation"]
		res = frappe.get_doc("Reservation", res_name)
		self.assertEqual(flt(res.tax_amount), 0.0)
		self.assertEqual(flt(res.amount_before_tax), 567345.5)
		self.assertEqual(flt(res.amount_after_tax), 567345.5)

		# Apply 10% discount via rediscount_locked_nights: $150 less 10% = 499,500 + 12,345.50 = 511,845.50
		# Must NOT document-round to 511,846 when tax == 0!
		self._as(
			DESK_USER, api.set_reservation_discount, res_name,
			discount_type="Percent", discount_value=10, reason_code="RATE-PROMO",
		)
		res.reload()
		self.assertEqual(flt(res.tax_amount), 0.0)
		self.assertEqual(flt(res.amount_before_tax), 511845.5)
		self.assertEqual(flt(res.amount_after_tax), 511845.5)

		# Check in & post room + extra-guest charge on 0%-tax folio: exact sum preserved (no doc rounding)
		self._as(DESK_USER, api.check_in, res_name)
		folio_mod.post_room_night(frappe.get_doc("Reservation", res_name), d(0))
		fname = guest_folio(res_name)
		fdoc = frappe.get_doc("Folio", fname)
		self.assertEqual(flt(fdoc.tax_total), 0.0)
		self.assertEqual(flt(fdoc.charges_total), 511845.5)
		self.assertEqual(flt(fdoc.grand_total), 511845.5)

		# Settle & close -> closed/invoiced folio totals remain frozen even on later recalculation
		set_setting(PROP_R2, "use_cashier_tills", 0)
		self._as(GM_USER, api.add_folio_payment, fname, "Cash", 511845.5, pin=GM_PIN)
		folio_mod.close_folio(fname)
		closed = frappe.get_doc("Folio", fname)
		self.assertEqual(closed.status, "Closed")
		self.assertEqual(flt(closed.charges_total), 511845.5)
		self.assertEqual(flt(closed.grand_total), 511845.5)
		self.assertEqual(flt(closed.balance), 0.0)
		# Even if a charge row carries non-zero historical tax metadata or a later recalc runs,
		# the closed folio's charges_total / tax_total / grand_total never shift.
		closed.charges[0].gst_rate = 18.0
		folio_mod._recalculate(closed)
		self.assertEqual(flt(closed.charges_total), 511845.5)
		self.assertEqual(flt(closed.tax_total), 0.0)
		self.assertEqual(flt(closed.grand_total), 511845.5)
		self.assertEqual(flt(closed.balance), 0.0)

	# ── Case 8: OTA modify alert on a discounted tariff stay ─────────────────

	def test_08_ota_modify_alert_on_discounted_tariff_stay(self):
		"""An OTA modify with a total on any tariff-owned stay (direct, derived-plan,
		or repointed contract_tariff snapshot) is held for desk review under
		hospitality-tariffs@11 instead of throwing HSP-TARIFF-008, with an accurate
		hold reason and the desk discount preserved; a non-tariff discounted stay
		still applies cleanly and flags discount_removed."""
		plan = self._make_plan(
			PROP_R2, "CPOTA", currency="USD", custom_tariff_price_list="UT Rel2 USD PL",
		)
		derived = self._make_plan(
			PROP_R2, "CPOTAH", currency="USD", base_rate_plan=plan,
			modifier_type="Percent", modifier_value=-50,
		)
		repointed = self._make_plan(
			PROP_R2, "CPOTAR", currency="USD", base_rate_plan=plan,
			modifier_type="Percent", modifier_value=-50,
		)
		local_plan = self._make_plan(
			PROP_R2, "LOCOTA", currency="UGX",
			modifier_type="Percent", modifier_value=0,
		)
		rt_local = make_room_type(PROP_R2, "LOC", base_price=300000, tax_percent=0)
		frappe.db.set_value("Room Type", rt_local, {"base_price": 300000, "tax_percent": 0})
		room_local = make_room(PROP_R2, rt_local, "L101")
		self._install_tariff_api({
			plan: {
				"currency": "USD", "nightly": 150.0, "fx_rate": 3655.0,
				"fx_date": TODAY, "agreed_rate": 548250.0, "property_currency": "UGX",
			},
		})
		self._install_tariff_guard()

		frappe.db.delete("Channel Room Mapping", {"external_room_id": ("in", ["EXT-DLX", "EXT-LOC"])})
		frappe.db.delete("Channel Manager Connection", {"property": PROP_R2})
		conn = frappe.get_doc({
			"doctype": "Channel Manager Connection",
			"property": PROP_R2,
			"provider": "Channex",
			"enabled": 1,
		}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Channel Room Mapping",
			"connection": conn.name,
			"room_type": self.rt_bridged,
			"external_room_id": "EXT-DLX",
		}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Channel Room Mapping",
			"connection": conn.name,
			"room_type": rt_local,
			"external_room_id": "EXT-LOC",
		}).insert(ignore_permissions=True)

		# Subcase A: derived tariff stay (no custom_tariff_currency) with OTA ref + 10% desk discount
		b_drv = self._as(
			DESK_USER, api.create_booking, PROP_R2, self.rt_bridged, d(5), d(7), "OTA Derived Guest",
			room=self.rooms_bridged[0], rate_plan=derived,
			booked_via="Booking.com", ota_ref="OTA-R2-DRV-1",
			discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO",
		)
		out_drv = channel_manager._apply_event(conn, {
			"event": "modify", "ota_ref": "OTA-R2-DRV-1", "channel": "Booking.com",
			"room_type_external_id": "EXT-DLX", "check_in": d(5), "check_out": d(7),
			"adults": 2, "children": 0, "currency": "UGX", "total": 520000,
		})
		self.assertEqual(out_drv["result"], "held_for_review")
		res_drv = frappe.get_doc("Reservation", b_drv["reservation"])
		self.assertEqual(int(res_drv.channel_review_needed), 1)
		self.assertEqual(res_drv.channel_review_reason, "update_not_applied")
		self.assertEqual(res_drv.discount_type, "Percent")
		self.assertEqual(flt(res_drv.discount_value), 10.0)
		self.assertIn("contract-tariff", res_drv.special_requests or "")
		self.assertNotIn("the stay is Confirmed", res_drv.special_requests or "")
		actions_drv = set(frappe.get_all(
			"Agent Action Log",
			filters={"reference_doctype": "Reservation", "reference_name": res_drv.name},
			pluck="action_type",
		))
		self.assertIn("reservation_discount", actions_drv)
		self.assertIn("channel.modify_needs_review", actions_drv)

		# Subcase B: direct tariff stay (custom_tariff_currency='USD') + 10% desk discount
		b_grd = self._as(
			DESK_USER, api.create_booking, PROP_R2, self.rt_bridged, d(8), d(10), "OTA Guarded Guest",
			room=self.rooms_bridged[1], rate_plan=plan,
			booked_via="Booking.com", ota_ref="OTA-R2-GRD-1",
			discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO",
		)
		out_grd = channel_manager._apply_event(conn, {
			"event": "modify", "ota_ref": "OTA-R2-GRD-1", "channel": "Booking.com",
			"room_type_external_id": "EXT-DLX", "check_in": d(8), "check_out": d(10),
			"adults": 2, "children": 0, "currency": "UGX", "total": 900000,
		})
		self.assertEqual(out_grd["result"], "held_for_review")
		res_grd = frappe.get_doc("Reservation", b_grd["reservation"])
		self.assertEqual(int(res_grd.channel_review_needed), 1)
		self.assertEqual(res_grd.channel_review_reason, "update_not_applied")
		self.assertIn("contract-tariff", res_grd.special_requests or "")
		self.assertNotIn("the stay is Confirmed", res_grd.special_requests or "")
		actions_grd = set(frappe.get_all(
			"Agent Action Log",
			filters={"reference_doctype": "Reservation", "reference_name": res_grd.name},
			pluck="action_type",
		))
		self.assertIn("channel.modify_needs_review", actions_grd)

		# Subcase C: repointed derived tariff stay (base_rate_plan cleared after booking;
		# only saved contract_tariff snapshot marks it tariff-owned, mirroring v11 repointed_stay)
		b_rep = self._as(
			DESK_USER, api.create_booking, PROP_R2, self.rt_bridged, d(11), d(13), "OTA Repointed Guest",
			room=self.rooms_bridged[2], rate_plan=repointed,
			booked_via="Booking.com", ota_ref="OTA-R2-REP-1",
			discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO",
		)
		frappe.db.set_value("Rate Plan", repointed, "base_rate_plan", None, update_modified=False)
		out_rep = channel_manager._apply_event(conn, {
			"event": "modify", "ota_ref": "OTA-R2-REP-1", "channel": "Booking.com",
			"room_type_external_id": "EXT-DLX", "check_in": d(11), "check_out": d(13),
			"adults": 2, "children": 0, "currency": "UGX", "total": 510000,
		})
		self.assertEqual(out_rep["result"], "held_for_review")
		res_rep = frappe.get_doc("Reservation", b_rep["reservation"])
		self.assertEqual(int(res_rep.channel_review_needed), 1)
		self.assertEqual(res_rep.channel_review_reason, "update_not_applied")
		self.assertEqual(res_rep.discount_type, "Percent")

		# Subcase D: non-tariff discounted stay still applies OTA modify and flags discount_removed
		b_loc = self._as(
			DESK_USER, api.create_booking, PROP_R2, rt_local, d(14), d(16), "OTA Local Guest",
			room=room_local, rate_plan=local_plan,
			booked_via="Booking.com", ota_ref="OTA-R2-LOC-1",
			discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO",
		)
		out_loc = channel_manager._apply_event(conn, {
			"event": "modify", "ota_ref": "OTA-R2-LOC-1", "channel": "Booking.com",
			"room_type_external_id": "EXT-LOC", "check_in": d(14), "check_out": d(16),
			"adults": 2, "children": 0, "currency": "UGX", "total": 580000,
		})
		self.assertEqual(out_loc["result"], "modified")
		res_loc = frappe.get_doc("Reservation", b_loc["reservation"])
		self.assertEqual(int(res_loc.channel_review_needed), 1)
		self.assertEqual(res_loc.channel_review_reason, "discount_removed")
		self.assertIsNone(res_loc.discount_type)
		actions_loc = set(frappe.get_all(
			"Agent Action Log",
			filters={"reference_doctype": "Reservation", "reference_name": res_loc.name},
			pluck="action_type",
		))
		self.assertIn("channel.modify_discount_removed", actions_loc)

	# ── Case 9: Room Types & Rate Calendar selling rate unaffected ───────────

	def test_09_room_types_and_rate_calendar_selling_rate_unaffected_by_stay_discount(self):
		"""Active discounted stays never change Room Types summary or Rate Calendar selling rates."""
		plan = self._make_plan(
			PROP_R2, "CPSELL", currency="USD", is_default=1,
			custom_tariff_price_list="UT Rel2 USD PL",
		)
		self._install_tariff_api({
			plan: {
				"currency": "USD", "nightly": 150.0, "fx_rate": 3655.0,
				"fx_date": TODAY, "agreed_rate": 548250.0, "property_currency": "UGX",
			},
		})
		summary_before = self._as(DESK_USER, rates.room_types_summary, PROP_R2)
		cal_before = self._as(DESK_USER, rates.rate_calendar, PROP_R2, from_date=d(5), days=3, rate_plan=plan)

		# Create a heavily discounted stay (25% off) on the same room type and dates
		self._as(
			RM_USER, api.create_booking, PROP_R2, self.rt_bridged, d(5), d(8), "Discounted Guest",
			room=self.rooms_bridged[0], rate_plan=plan,
			discount_type="Percent", discount_value=25, discount_reason="RATE-PROMO",
		)

		summary_after = self._as(DESK_USER, rates.room_types_summary, PROP_R2)
		cal_after = self._as(DESK_USER, rates.rate_calendar, PROP_R2, from_date=d(5), days=3, rate_plan=plan)

		row_before = next(r for r in summary_before["room_types"] if r["room_type"] == self.rt_bridged)
		row_after = next(r for r in summary_after["room_types"] if r["room_type"] == self.rt_bridged)
		self.assertEqual(row_before["amount"], 150.0)
		self.assertEqual(row_before["local_amount"], 548250.0)
		self.assertEqual(row_after, row_before)

		nights_before = [n["rate"] for n in cal_before["room_types"][0]["nights"]]
		nights_after = [n["rate"] for n in cal_after["room_types"][0]["nights"]]
		self.assertEqual(nights_before, [548250.0, 548250.0, 548250.0])
		self.assertEqual(nights_after, nights_before)

	# ── Case 10: Reservation currency options + reference currency fields ────

	def test_10_reservation_currency_options_with_reference_currency_and_no_inr_leak(self):
		"""Property:property:currency on Reservation works cleanly alongside custom_tariff_*
		and reference_currency: zero INR / ₹ leak in Version / Changes Log even when
		Global Defaults default_currency is INR."""
		plan = self._make_plan(
			PROP_R2, "CPCUR10", currency="USD", custom_tariff_price_list="UT Rel2 USD PL",
		)
		self._install_tariff_api({
			plan: {
				"currency": "USD", "nightly": 150.0, "fx_rate": 3700.0,
				"fx_date": TODAY, "agreed_rate": 555000.0, "property_currency": "UGX",
			},
		})
		self._install_tariff_guard()

		meta = frappe.get_meta("Reservation")
		for fieldname in ("amount_before_tax", "tax_amount", "amount_after_tax",
		                  "discount_amount", "agreed_rate"):
			self.assertEqual(meta.get_field(fieldname).options, "Property:property:currency")
		self.assertEqual(meta.get_field("custom_tariff_nightly").options, "custom_tariff_currency")

		prev_default = frappe.db.get_single_value("Global Defaults", "default_currency")
		try:
			frappe.db.set_single_value("Global Defaults", "default_currency", "INR")
			b = self._as(
				RM_USER, api.create_booking, PROP_R2, self.rt_bridged, d(5), d(6), "Currency Check Guest",
				room=self.rooms_bridged[0], rate_plan=plan,
				discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO",
			)
			res_name = b["reservation"]
			with patch.object(frappe, "in_test", False):
				self._as(
					RM_USER, api.set_reservation_discount, res_name,
					discount_type="Percent", discount_value=20, reason_code="RATE-PROMO",
				)

			entries = self._as(DESK_USER, api.reservation_changes, res_name)["entries"]
			amt_field = next(
				f for e in entries if e["kind"] == "change"
				for f in e["fields"] if f["field"] == "amount_after_tax"
			)
			for side in (amt_field["from"], amt_field["to"]):
				self.assertIn("UGX", side)
				self.assertNotIn("₹", side)
				self.assertNotIn(".00", side)
			self.assertIn("499,500", amt_field["from"])
			self.assertIn("444,000", amt_field["to"])

			detail = self._as(DESK_USER, api.reservation_detail, res_name)
			self.assertEqual(detail["tariff"]["currency"], "USD")
			self.assertEqual(detail["tariff"]["nightly"], 150.0)
			self.assertEqual(detail["tariff"]["property_currency"], "UGX")
		finally:
			if prev_default:
				frappe.db.set_single_value("Global Defaults", "default_currency", prev_default)

	# ── Case 11: v48 patch migration order on a populated site ───────────────

	def test_11_v48_patch_migration_order_on_populated_site(self):
		"""All four v48 post_model_sync patches execute cleanly in patches.txt order on a
		populated UGX site and are idempotent on a second run."""
		# Seed pre-v48 populated state
		frappe.db.set_value("Property", PROP_R2, {
			"gst_mode": "Slab", "gst_slab_threshold": 7500, "gst_rate_low": 5,
			"gst_rate_high": 18, "security_deposit_amount": 5000, "locale": "en-IN",
		}, update_modified=False)
		frappe.db.delete("Version", {"ref_doctype": "Property", "docname": PROP_R2})
		frappe.db.set_value("Currency", "UGX", "number_format", "#,###.##", update_modified=False)
		frappe.db.delete("Version", {"ref_doctype": "Currency", "docname": "UGX"})

		# Tariff rate plan with blank currency linked to USD Price List
		blank_plan = self._make_plan(
			PROP_R2, "V48BLK", currency=None, custom_tariff_price_list="UT Rel2 USD PL",
		)
		frappe.db.set_value("Rate Plan", blank_plan, "currency", "", update_modified=False)

		# Channex connection with empty ota_tax_rules
		frappe.db.delete("Channel Manager Connection", {"property": PROP_R2})
		conn = frappe.get_doc({
			"doctype": "Channel Manager Connection",
			"property": PROP_R2, "provider": "Channex", "enabled": 1,
		}).insert(ignore_permissions=True)
		frappe.db.delete("Channel OTA Tax Rule", {"parent": conn.name})

		# Run all four v48 patches in exact patches.txt order
		seed_channel_ota_tax_rules.execute()
		configurable_masters.execute()
		replace_untouched_india_defaults.execute()
		backfilled = backfill_rate_plan_tariff_currency.execute()

		self.assertGreaterEqual(backfilled, 1)
		self.assertEqual(frappe.db.get_value("Rate Plan", blank_plan, "currency"), "USD")
		self.assertEqual(frappe.db.get_value("Currency", "UGX", "number_format"), "#,###")
		prop_after = frappe.db.get_value(
			"Property", PROP_R2,
			["gst_slab_threshold", "security_deposit_amount", "locale"], as_dict=True,
		)
		self.assertEqual(flt(prop_after.gst_slab_threshold), 0.0)
		self.assertEqual(flt(prop_after.security_deposit_amount), 0.0)
		self.assertEqual(prop_after.locale or "", "")
		self.assertGreater(
			frappe.db.count("Channel OTA Tax Rule", {"parent": conn.name}), 0,
		)

		# Second run is completely idempotent
		seed_channel_ota_tax_rules.execute()
		configurable_masters.execute()
		replace_untouched_india_defaults.execute()
		self.assertEqual(backfill_rate_plan_tariff_currency.execute(), 0)

	# ── Case 12: Percent discount on foreign tariff with plan rounding_step ──

	def test_12_percent_discount_on_foreign_tariff_with_rounding_step_and_desk_cap(self):
		"""F1 (SAP/OPERA parity): when a foreign-currency rate plan has a commercial
		rounding_step (e.g. USD 150 @ 3,655.5 = 548,325 -> Up 1,000 = 549,000), a 10%
		Percent discount takes 10.00% off the rounded gross (494,100; cut 54,900),
		agrees with Flat 54,900, admits a Front Desk user at their 10% limit without
		spurious supervisor PIN refusal, and agrees across quote, booking, post-booking
		rediscount, check-in FX relock, and Current Date folio posting."""
		plan_chk = self._make_plan(
			PROP_R2, "CPRND", currency="USD", fx_lock_policy="Fixed at Check In",
			rounding_step=1000, rounding_mode="Up",
			custom_tariff_price_list="UT Rel2 USD PL",
		)
		plan_cur = self._make_plan(
			PROP_R2, "CPRNDCUR", currency="USD", fx_lock_policy="Current Date",
			rounding_step=1000, rounding_mode="Up",
			custom_tariff_price_list="UT Rel2 USD PL",
		)
		plan_down = self._make_plan(
			PROP_R2, "CPRNDDN", currency="USD", fx_lock_policy="Fixed at Reservation",
			rounding_step=1000, rounding_mode="Down",
		)
		self._install_tariff_api({
			# USD 150 * 3,655.5 = 548,325 -> Up 1,000 = 549,000; Down 1,000 = 548,000
			plan_chk: {
				"currency": "USD", "nightly": 150.0, "fx_rate": 3655.5,
				"fx_date": TODAY, "agreed_rate": 549000.0, "property_currency": "UGX",
			},
			plan_cur: {
				"currency": "USD", "nightly": 150.0, "fx_rate": 3655.5,
				"fx_date": TODAY, "agreed_rate": 549000.0, "property_currency": "UGX",
			},
			plan_down: {
				"currency": "USD", "nightly": 150.0, "fx_rate": 3655.5,
				"fx_date": TODAY, "agreed_rate": 548000.0, "property_currency": "UGX",
			},
		})
		self._install_tariff_guard()
		_set_fx("USD", "UGX", 3655.5, TODAY)

		# 1. Plain quote = 549,000; Flat 54,900 = 494,100 (10.00%); Percent 10% = 494,100 (10.00%)
		q_plain = self._as(
			DESK_USER, api.get_quote, PROP_R2, self.rt_bridged, d(0), d(2), rate_plan=plan_chk,
		)
		self.assertEqual(q_plain["agreed_rate"], 549000.0)
		self.assertEqual(q_plain["nightly"][0]["rate"], 549000.0)

		q_flat = self._as(
			DESK_USER, api.get_quote, PROP_R2, self.rt_bridged, d(0), d(2),
			rate_plan=plan_chk, discount_type="Amount", discount_value=54900,
			discount_reason="RATE-PROMO",
		)
		q_pct = self._as(
			DESK_USER, api.get_quote, PROP_R2, self.rt_bridged, d(0), d(2),
			rate_plan=plan_chk, discount_type="Percent", discount_value=10,
			discount_reason="RATE-PROMO",
		)
		self.assertEqual(q_flat["nightly"][0]["rate"], 494100.0)
		self.assertEqual(q_pct["nightly"][0]["rate"], 494100.0)
		self.assertEqual(q_pct["nightly"][0]["discount_amount"], 54900.0)
		self.assertEqual(q_pct["amount_after_tax"], q_flat["amount_after_tax"])
		self.assertEqual(q_pct["discount_governance"]["effective_pct"], 10.0)
		self.assertFalse(q_pct["discount_governance"]["needs_supervisor"])

		# 2. Front Desk (10% limit) books 10% Percent with NO supervisor PIN -> admitted
		b_pct = self._as(
			DESK_USER, api.create_booking, PROP_R2, self.rt_bridged, d(0), d(2), "Step Up Pct Guest",
			room=self.rooms_bridged[0], rate_plan=plan_chk,
			discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO",
		)
		res_pct = frappe.get_doc("Reservation", b_pct["reservation"])
		self.assertEqual(flt(res_pct.agreed_rate), 549000.0)
		self.assertEqual([flt(r.rate) for r in res_pct.night_rates], [494100.0, 494100.0])
		self.assertEqual([flt(r.discount_amount) for r in res_pct.night_rates], [54900.0, 54900.0])
		self.assertEqual(flt(res_pct.amount_after_tax), 988200.0)

		# Above-cap discount (10.5%) is still refused without supervisor PIN
		with self.assertRaises(frappe.PermissionError):
			self._as(
				DESK_USER, api.create_booking, PROP_R2, self.rt_bridged, d(5), d(6), "Over Cap Guest",
				room=self.rooms_bridged[1], rate_plan=plan_chk,
				discount_type="Percent", discount_value=10.5, discount_reason="RATE-PROMO",
			)

		# 3. Post-booking set_reservation_discount agrees shilling-for-shilling (even if plan step changes later)
		b_post = self._as(
			DESK_USER, api.create_booking, PROP_R2, self.rt_bridged, d(5), d(7), "Step Up Post Guest",
			room=self.rooms_bridged[1], rate_plan=plan_chk,
		)
		self._as(
			DESK_USER, api.set_reservation_discount, b_post["reservation"],
			discount_type="Percent", discount_value=10, reason_code="RATE-PROMO",
		)
		res_post = frappe.get_doc("Reservation", b_post["reservation"])
		self.assertEqual([flt(r.rate) for r in res_post.night_rates], [494100.0, 494100.0])
		self.assertEqual([flt(r.discount_amount) for r in res_post.night_rates], [54900.0, 54900.0])
		# Even if the rate plan's rounding_step is edited to 0 after booking, rediscounting
		# keeps the booked rounded gross basis (494,100, exact 10.00%, no drift to 493,493)
		frappe.db.set_value("Rate Plan", plan_chk, "rounding_step", 0, update_modified=False)
		try:
			self._as(
				DESK_USER, api.set_reservation_discount, b_post["reservation"],
				discount_type="Percent", discount_value=5, reason_code="RATE-PROMO",
			)
			self._as(
				DESK_USER, api.set_reservation_discount, b_post["reservation"],
				discount_type="Percent", discount_value=10, reason_code="RATE-PROMO",
			)
			res_post.reload()
			self.assertEqual([flt(r.rate) for r in res_post.night_rates], [494100.0, 494100.0])
			self.assertEqual([flt(r.discount_amount) for r in res_post.night_rates], [54900.0, 54900.0])
		finally:
			frappe.db.set_value("Rate Plan", plan_chk, "rounding_step", 1000, update_modified=False)

		# 4. Book Current Date stay at FX 3,655.5 before FX moves, then check in & post at new FX 3,705.5
		b_cur = self._as(
			DESK_USER, api.create_booking, PROP_R2, self.rt_bridged, d(0), d(2), "Step Up Cur Guest",
			room=self.rooms_bridged[2], rate_plan=plan_cur,
			discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO",
		)
		self._as(DESK_USER, api.check_in, b_cur["reservation"])

		# FX moves to 3,705.5: USD 150 * 3,705.5 = 555,825 -> Up 1,000 = 556,000; 10% off = 500,400 (cut 55,600)
		_set_fx("USD", "UGX", 3705.5, TODAY)

		# 4a. Check-in FX relock on Fixed at Check In stay
		self._as(DESK_USER, api.check_in, b_pct["reservation"])
		res_pct.reload()
		self.assertEqual(res_pct.status, "Checked In")
		self.assertEqual(flt(res_pct.agreed_rate), 556000.0)
		self.assertEqual([flt(r.rate) for r in res_pct.night_rates], [500400.0, 500400.0])
		self.assertEqual([flt(r.discount_amount) for r in res_pct.night_rates], [55600.0, 55600.0])
		self.assertEqual(flt(res_pct.amount_after_tax), 1000800.0)

		# 4b. Current Date folio posting agrees identically (500,400 = 556,000 less discount 55,600)
		folio_mod.post_room_night(frappe.get_doc("Reservation", b_cur["reservation"]), d(0))
		line_cur = next(
			c for c in frappe.get_doc("Folio", guest_folio(b_cur["reservation"])).charges
			if c.charge_type == "Room"
		)
		self.assertEqual(flt(line_cur.amount), 500400.0)
		self.assertEqual(flt(line_cur.contract_fx_rate), 3705.5)
		self.assertIn("556,000 less discount 55,600", line_cur.description)

		# 5. Down rounding_mode (548,000 plain -> 10% = 493,200, cut 54,800) also passes 10% desk cap
		b_dn = self._as(
			DESK_USER, api.create_booking, PROP_R2, self.rt_bridged, d(5), d(6), "Step Down Pct Guest",
			room=self.rooms_bridged[3], rate_plan=plan_down,
			discount_type="Percent", discount_value=10, discount_reason="RATE-PROMO",
		)
		res_dn = frappe.get_doc("Reservation", b_dn["reservation"])
		self.assertEqual(flt(res_dn.agreed_rate), 548000.0)
		self.assertEqual([flt(r.rate) for r in res_dn.night_rates], [493200.0])
		self.assertEqual([flt(r.discount_amount) for r in res_dn.night_rates], [54800.0])
