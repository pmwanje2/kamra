# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

"""Security deposit liability and card pre-authorization lifecycle (ADR-009, P27).

A refundable damage deposit (villas, long stays) is a liability the hotel
holds for the guest - never accommodation revenue. Every unit of cash/bank
money moves through the folio writers that already exist; card pre-authorization
holds delegate to the horizontal credit pre-authorization control package so
zero GL or payment entries are posted until an explicit capture occurs:

collect   add_folio_payment(kind="Security Deposit")
          folio: payment row          ledger: Deposit credit (liability up)
waive     no money; a reason on the record
withhold  _post_folio_charge (a damage line, revenue)
          ledger: Guest debit (the charge) + transfer Deposit debit /
          Guest credit (the held money pays the charge)
refund    _refund_folio_payment (the refund writer: till, folio, ledger)
          ledger: Deposit debit (liability down) - not the Guest ledger
authorize preauth_control.authorize (zero GL, zero payment entry)
increment preauth_control.increment (zero GL, zero payment entry)
capture   preauth_control.capture (submitted receipt + GL) + damage charge
release   preauth_control.release (zero GL, clears checkout block)
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP

import frappe
from frappe import _
from frappe.utils import flt, get_datetime, now_datetime, nowdate

from kamra.authz import require_roles

# a deposit can be taken on a booking that is still coming, or in-house
COLLECT_STATUSES = ("Confirmed", "Pending Payment", "Held", "Checked In")
REFUND_REASON = "Security deposit refund"
WITHHOLD_PREFIX = "Damage / security deposit withheld"
PREAUTH_CAPTURE_PREFIX = "Pre-authorization captured"
PREAUTH_API_METHODS = (
	"preauth_control.authorize",
	"preauth_control.increment",
	"preauth_control.capture",
	"preauth_control.release",
)
_FLOAT_DUST = Decimal("1e-9")


def _has_doctype() -> bool:
	return bool(frappe.db.exists("DocType", "Security Deposit"))


def _preauth_available() -> bool:
	if not frappe.db.exists("DocType", "Credit Preauthorization"):
		return False
	from kamra.erp_bridge import get_bridge_script
	return all(get_bridge_script(method) is not None for method in PREAUTH_API_METHODS)


def _require_preauth_available() -> None:
	if not _preauth_available():
		frappe.throw(
			_("[HSP-DEP-PREAUTH-UNAVAILABLE]: Card pre-authorization control is not installed on this site.")
		)


def _policy_amount(property: str) -> float:
	return flt(frappe.get_cached_value("Property", property,
	                                   "security_deposit_amount") or 0)


def ensure_deposit_for_reservation(reservation, amount: float = 0) -> str | None:
	"""The stay's Security Deposit row, created on first need. The amount
	comes from the property's policy; with no policy, a desk-agreed amount
	(the first collection) opens it - villas often set it per booking."""
	if isinstance(reservation, str):
		reservation = frappe.get_doc("Reservation", reservation)
	if not _has_doctype():
		return None
	existing = frappe.db.get_value(
		"Security Deposit", {"reservation": reservation.name}, "name")
	if existing:
		return existing
	required = _policy_amount(reservation.property) or flt(amount)
	if required <= 0:
		return None
	doc = frappe.get_doc({
		"doctype": "Security Deposit",
		"reservation": reservation.name,
		"property": reservation.property,
		"guest": reservation.guest,
		"status": "Required",
		"method": "payment",
		"required_amount": required,
	})
	doc.insert(ignore_permissions=True)
	return doc.name


def _get(reservation: str, *, for_update: bool = False):
	name = frappe.db.get_value(
		"Security Deposit", {"reservation": reservation}, "name", for_update=for_update) \
		if _has_doctype() else None
	if not name:
		frappe.throw(_("No security deposit has been taken on this stay."))
	return frappe.get_doc("Security Deposit", name, for_update=for_update)


def _reservation(reservation: str, *, for_update: bool = False):
	res = frappe.get_doc("Reservation", reservation, for_update=for_update)
	from kamra.crs import assert_property_access
	assert_property_access(res.property)
	return res


def _guest_folio(res, create: bool = False) -> str | None:
	name = frappe.db.get_value(
		"Folio", {"reservation": res.name, "folio_type": "Guest"}, "name")
	if name or not create:
		return name
	from kamra.folio import open_folio
	return open_folio(res)


def _held_status(res) -> str:
	return "Held" if res.status == "Checked In" else "Collected"


def _deposit_precision(property: str | None) -> tuple[int, int, float]:
	"""Return (round_places, display_places, eps) for a property's currency."""
	from kamra.ledger import _currency_places
	display = _currency_places(property)
	rplaces = max(display, 2)
	return rplaces, display, 0.5 * (10 ** -rplaces)


def _preauth_precision(property: str | None) -> tuple[int, float]:
	"""Return exact ISO minor-unit (places, eps) for 0/2/3dp pre-authorization math."""
	from kamra.ledger import _currency_places
	places = max(int(_currency_places(property)), 0)
	return places, 0.5 * (10 ** -places)


def _parse_positive_amount(raw, places: int, empty_msg: str) -> float:
	if raw is None or raw == "" or isinstance(raw, bool):
		frappe.throw(empty_msg)
	try:
		value = Decimal(str(raw))
	except (InvalidOperation, TypeError, ValueError):
		frappe.throw(empty_msg)
	if not value.is_finite() or value <= 0 or abs(value) > Decimal("1e15"):
		frappe.throw(empty_msg)
	unit = Decimal(1).scaleb(-places)
	quantized = value.quantize(unit, rounding=ROUND_HALF_UP)
	if quantized <= 0 or abs(value - quantized) >= _FLOAT_DUST:
		if places == 0:
			frappe.throw(_("Enter a positive whole-unit amount."))
		frappe.throw(_("Enter a positive amount with at most {0} decimal places.").format(places))
	return float(quantized)


def _assert_no_pci(label: str, raw_value: str | None) -> None:
	text = str(raw_value or "").strip()
	if not text:
		return
	digits_run = 0
	for ch in text:
		if "0" <= ch <= "9":
			digits_run += 1
		elif ch in (" ", "-") and digits_run > 0:
			continue
		else:
			if 13 <= digits_run <= 19:
				frappe.throw(_("[PREAUTH-PCI-001]: Card PAN data is prohibited in {0}.").format(label))
			digits_run = 0
	if 13 <= digits_run <= 19:
		frappe.throw(_("[PREAUTH-PCI-001]: Card PAN data is prohibited in {0}.").format(label))
	lower = text.lower()
	if "%b" in lower:
		frappe.throw(_("[PREAUTH-PCI-001]: Card magnetic track data is prohibited in {0}.").format(label))
	pos = 0
	while True:
		semi = text.find(";", pos)
		if semi == -1:
			break
		eq = text.find("=", semi + 1)
		if eq != -1:
			between = text[semi + 1 : eq]
			dcount = 0
			all_track = True
			for ch in between:
				if "0" <= ch <= "9":
					dcount += 1
				elif ch not in (" ", "-"):
					all_track = False
					break
			if all_track and dcount >= 4:
				frappe.throw(_("[PREAUTH-PCI-001]: Card magnetic track data is prohibited in {0}.").format(label))
		pos = semi + 1
	for marker in ("cvv2", "cvc2", "cvv", "cvc", "cid", "csc"):
		start = 0
		mlen = len(marker)
		while True:
			idx = lower.find(marker, start)
			if idx == -1:
				break
			prev_ok = idx == 0 or not lower[idx - 1].isalnum()
			if prev_ok:
				cursor = idx + mlen
				while cursor < len(lower) and lower[cursor] in (" ", ":", "=", "-", "#", "/", "."):
					cursor += 1
				dcount = 0
				while cursor < len(lower) and "0" <= lower[cursor] <= "9":
					dcount += 1
					cursor += 1
				if dcount in (3, 4) and (cursor == len(lower) or not ("0" <= lower[cursor] <= "9")):
					frappe.throw(_("[PREAUTH-PCI-001]: Card security code is prohibited in {0}.").format(label))
			start = idx + mlen


def _validate_card_last4(raw: str | None, *, required: bool = False) -> str:
	text = str(raw or "").strip()
	if not text:
		if required:
			frappe.throw(_("[PREAUTH-PCI-001]: Card Last 4 must contain exactly 4 ASCII digits."))
		return ""
	_assert_no_pci("card_last4", text)
	if len(text) != 4 or any(not ("0" <= ch <= "9") for ch in text):
		frappe.throw(_("[PREAUTH-PCI-001]: Card Last 4 must contain exactly 4 ASCII digits."))
	return text


def _normalize_expires_on(raw: str | None) -> str:
	text = str(raw or "").strip()
	if not text:
		frappe.throw(_("Enter an authorization expiry date and time."))
	text = text.replace("T", " ")
	if len(text) == 10:
		text = f"{text} 23:59:59"
	elif len(text) == 16:
		text = f"{text}:00"
	try:
		parsed = get_datetime(text)
	except Exception:
		frappe.throw(_("Enter a valid authorization expiry date and time."))
	return str(parsed)


def _call_preauth(action: str, payload: dict) -> dict:
	from kamra.erp_bridge import bridge_call
	result = bridge_call(f"preauth_control.{action}", **payload)
	if result is None:
		_require_preauth_available()
	if not isinstance(result, dict) or not result.get("ok"):
		frappe.throw(_("[HSP-DEP-PREAUTH-UNAVAILABLE]: Unexpected pre-authorization control response."))
	return result


def _resolve_preauth_context(res, folio_name: str | None = None) -> dict:
	prop = frappe.get_cached_doc("Property", res.property)
	currency = (prop.get("currency") or "").strip()
	if not currency:
		from kamra.pricing import property_currency
		currency = (property_currency(res.property) or "").strip()

	company = (prop.get("company") or "").strip()
	if not company and frappe.db.exists("DocType", "Company"):
		default_co = (frappe.defaults.get_global_default("company") or "").strip()
		if currency:
			by_curr = frappe.get_list(
				"Company",
				filters={"default_currency": currency},
				fields=["name"],
				limit_page_length=20,
			)
			names = [r.get("name") for r in by_curr if r.get("name")]
			if default_co and default_co in names:
				company = default_co
			elif names:
				company = names[0]
		if not company and default_co:
			rows = frappe.get_list(
				"Company",
				filters={"name": default_co},
				fields=["name"],
				limit_page_length=1,
			)
			if rows:
				company = rows[0].get("name") or ""
	if not company:
		frappe.throw(_("[HSP-DEP-PREAUTH-CONFIG]: No accounting company is linked to this property."))

	customer = ""
	if folio_name and frappe.get_meta("Folio").has_field("customer"):
		customer = (frappe.db.get_value("Folio", folio_name, "customer") or "").strip()
	if not customer:
		customer = (prop.get("walk_in_customer") or "").strip()
	if not customer and res.get("guest_name") and frappe.db.exists("DocType", "Customer"):
		# The reservation already passed the property-access check.
		customer = frappe.db.get_value("Customer", {"customer_name": res.guest_name}, "name") or ""
	if not customer:
		frappe.throw(_("[HSP-DEP-PREAUTH-CONFIG]: No billing customer is configured for this folio or property."))

	return {
		"company": company,
		"branch": (prop.get("branch") or "").strip() or None,
		"cost_center": (prop.get("cost_center") or "").strip() or None,
		"customer": customer,
		"currency": currency,
	}


def _live_preauth_snapshot(preauth_hold: str | None, *, for_update: bool = False) -> dict | None:
	if not preauth_hold or not frappe.db.exists("DocType", "Credit Preauthorization"):
		return None
	return frappe.db.get_value(
		"Credit Preauthorization",
		preauth_hold,
		[
			"status",
			"authorized_amount",
			"captured_amount",
			"released_amount",
			"remaining_hold_amount",
			"expires_on",
			"payment_entry",
			"card_brand",
			"card_last4",
			"gateway_auth_ref",
		],
		as_dict=True, for_update=for_update,
	)


def _is_expired_ts(expires_on) -> bool:
	if not expires_on:
		return False
	try:
		return get_datetime(expires_on) <= now_datetime()
	except Exception:
		return False


def _effective_preauth(row, places: int, eps: float, *, for_update: bool = False) -> dict:
	"""Reconcile stored Security Deposit row with live Credit Preauthorization state."""
	if not row:
		return {
			"status": None,
			"authorized_amount": 0.0,
			"remaining_hold_amount": 0.0,
			"preauth_hold": None,
			"gateway_auth_ref": None,
			"card_brand": None,
			"card_last4": None,
			"expires_on": None,
			"payment_entry": None,
			"active": False,
		}
	status = row.get("status")
	preauth_hold = row.get("preauth_hold")
	live = _live_preauth_snapshot(preauth_hold, for_update=for_update)
	if live:
		live_status = live.get("status")
		auth_amt = flt(live.get("authorized_amount"), places)
		rem_amt = flt(live.get("remaining_hold_amount"), places)
		expires_on = live.get("expires_on") or row.get("expires_on")
		payment_entry = live.get("payment_entry") or None
		card_brand = live.get("card_brand") or row.get("card_brand")
		card_last4 = live.get("card_last4") or row.get("card_last4")
		gateway_auth_ref = live.get("gateway_auth_ref") or row.get("gateway_auth_ref")
		if live_status == "Expired":
			status = "Expired"
			rem_amt = 0.0
		elif live_status == "Released":
			status = "Released"
			rem_amt = 0.0
		elif live_status == "Captured":
			if status == "Authorized":
				status = "Withheld"
			rem_amt = 0.0
		elif live_status in ("Authorized", "Incremented"):
			if _is_expired_ts(expires_on):
				status = "Expired"
				rem_amt = 0.0
			else:
				status = "Authorized"
	else:
		auth_amt = flt(row.get("authorized_amount"), places)
		expires_on = row.get("expires_on")
		payment_entry = None
		card_brand = row.get("card_brand")
		card_last4 = row.get("card_last4")
		gateway_auth_ref = row.get("gateway_auth_ref")
		rem_amt = 0.0
		if status in ("Authorized", "Incremented"):
			if _is_expired_ts(expires_on):
				status = "Expired"
			else:
				status = "Authorized"
				rem_amt = auth_amt

	active = bool(preauth_hold and status == "Authorized" and rem_amt > eps)
	return {
		"status": status,
		"authorized_amount": auth_amt,
		"remaining_hold_amount": rem_amt if active else 0.0,
		"preauth_hold": preauth_hold,
		"gateway_auth_ref": gateway_auth_ref,
		"card_brand": card_brand,
		"card_last4": card_last4,
		"expires_on": str(expires_on) if expires_on else None,
		"payment_entry": payment_entry,
		"active": active,
	}


def _card_hold_blocks_checkout(res, effective: dict, eps: float) -> bool:
	return (
		res.status == "Checked In" and bool(effective["preauth_hold"])
		and effective["status"] == "Authorized" and effective["authorized_amount"] > eps
	)


def has_checkout_card_hold(res) -> bool:
	"""Read the current hold while check-out owns the reservation lock."""
	if res.status != "Checked In" or not _has_doctype():
		return False
	row = frappe.db.get_value(
		"Security Deposit", {"reservation": res.name},
		["status", "preauth_hold", "expires_on", "authorized_amount"], as_dict=True, for_update=True,
	)
	places, eps = _preauth_precision(res.property)
	return _card_hold_blocks_checkout(res, _effective_preauth(row, places, eps, for_update=True), eps)


def _capture_payment_entry(row, effective: dict, *, for_update: bool = False) -> dict | None:
	"""Find an outstanding capture receipt for this hold, including older drafts."""
	hold = effective["preauth_hold"]
	if not effective["active"] or not frappe.db.exists("DocType", "Payment Entry"):
		return None

	fields = ["name", "docstatus", "paid_amount"]
	tracked = row.get("pending_capture_payment_entry")
	if tracked:
		entry = frappe.db.get_value("Payment Entry", tracked, fields, as_dict=True, for_update=for_update)
		if entry and entry.docstatus in (0, 1):
			return entry

	# Before Kamra recorded the link, the bridge used the hold ID in remarks
	# and defaulted the transaction reference to the gateway authorization.
	# ERPNext can replace the supplied remarks, so check both identifiers.
	owner = frappe.db.get_value(
		"Credit Preauthorization", hold, ["company", "customer"], as_dict=True,
	)
	if not owner or not owner.company or not owner.customer:
		return None
	filters = {
		"docstatus": 0, "payment_type": "Receive", "party_type": "Customer",
		"company": owner.company, "party": owner.customer,
	}
	entry = frappe.db.get_value(
		"Payment Entry", {**filters, "remarks": f"Pre-authorization capture for {hold}"},
		fields, as_dict=True,
	)
	if not entry and effective["gateway_auth_ref"]:
		entry = frappe.db.get_value(
			"Payment Entry", {**filters, "reference_no": effective["gateway_auth_ref"]},
			fields, as_dict=True,
		)
	return entry


def _require_no_capture_payment_entry(dep, effective: dict) -> None:
	entry = _capture_payment_entry(dep, effective, for_update=True)
	if entry:
		frappe.throw(
			_("The card hold cannot be released or increased while capture Payment Entry {0} exists.").format(entry.name)
		)


def _select_card_mode(property: str) -> str:
	from kamra.localization import pack_for, payment_modes
	from kamra.masters import payment_tender_type
	modes = payment_modes(pack_for(property), property) or ["Card"]
	for m in modes:
		if payment_tender_type(m, property) == "Card":
			return m
	return "Card" if "Card" in modes else modes[0]


def _record_captured_folio_payment(
	res,
	folio_name: str,
	dep,
	amt: float,
	payment_entry: str | None,
	reason: str,
) -> None:
	"""Mirror an already-submitted pre-authorization capture onto the guest folio
	and Guest ledger without creating a second ERP payment or double GL."""
	from kamra.business_date import get_business_date
	from kamra.folio import _recalculate
	from kamra.ledger import home_currency, write_ledger_entry

	folio_doc = frappe.get_doc("Folio", folio_name)
	bd = get_business_date(res.property) or nowdate()
	mode = _select_card_mode(res.property)
	ref = f"{PREAUTH_CAPTURE_PREFIX}: {dep.preauth_hold}"[:140]
	row_data = {
		"posting_date": bd,
		"payment_kind": "Payment",
		"mode": mode,
		"amount": float(amt),
		"reference": ref,
		"currency": home_currency(res.property),
	}
	if payment_entry and frappe.get_meta("Folio Payment").has_field("payment_entry"):
		row_data["payment_entry"] = payment_entry
	folio_doc.append("payments", row_data)
	_recalculate(folio_doc, payments_only=True)
	folio_doc.save(ignore_permissions=True)
	write_ledger_entry(
		property=res.property,
		folio=folio_name,
		ledger="Guest",
		debit=0,
		credit=amt,
		description=f"{PREAUTH_CAPTURE_PREFIX}: {reason}"[:140],
		source_doctype="Security Deposit",
		source_name=dep.name,
	)


def _settle_status(dep, res) -> None:
	"""Status after a withhold or refund: open while money is still held."""
	_rplaces, _display, eps = _deposit_precision(res.property)
	if flt(dep.balance) > eps:
		dep.status = "Withheld" if flt(dep.withheld_amount) > 0 \
			else _held_status(res)
	elif flt(dep.refunded_amount) > 0:
		dep.status = "Refunded"
	else:
		dep.status = "Withheld"


def record_collected(reservation: str, amount: float, mode: str) -> None:
	"""Called by add_folio_payment after a Security Deposit payment lands on
	a guest folio - the only place collected_amount moves up."""
	if not _has_doctype():
		return
	res = frappe.get_doc("Reservation", reservation)
	name = ensure_deposit_for_reservation(res, amount)
	if not name:
		return
	from kamra.masters import payment_tender_type
	dep = frappe.get_doc("Security Deposit", name)
	dep.collected_amount = flt(dep.collected_amount) + flt(amount)
	dep.method = "cash" if payment_tender_type(mode, res.property) == "Cash" else "payment"
	dep.status = _held_status(res)
	dep.save(ignore_permissions=True)


def collect_deposit(reservation: str, amount: float, *, mode: str = "Cash",
                    reference: str | None = None,
                    pin: str | None = None,
                    idempotency_key: str | None = None) -> dict:
	"""Take the security deposit: a Security Deposit payment on the guest
	folio (opened early if the guest hasn't arrived), held as a liability.

	Pass idempotency_key (one per Collect press): a retry with the same key
	returns the first result instead of posting again."""
	key = (idempotency_key or "").strip() or None
	if key:
		from kamra.api import _paid_with_key
		folio = _paid_with_key(key, reservation=reservation)
		if folio:
			res = _reservation(reservation)
			state = deposit_state(res)
			state["idempotent_replay"] = 1
			return state
	res = _reservation(reservation)
	if res.status not in COLLECT_STATUSES:
		frappe.throw(_("A security deposit can't be taken on a {0} booking.")
		             .format(res.status))
	amt = flt(amount)
	if amt <= 0:
		frappe.throw(_("Enter the deposit amount."))
	if not _has_doctype():
		frappe.throw(_("Security deposits are not set up on this site."))
	existing = frappe.db.get_value(
		"Security Deposit", {"reservation": res.name}, ["status"], as_dict=True)
	if existing and existing.status == "Waived":
		frappe.throw(_("This deposit was waived."))
	if existing and existing.status == "Refunded":
		frappe.throw(_("This deposit has already been refunded."))
	from kamra.localization import pack_for, payment_modes
	modes = payment_modes(pack_for(res.property), res.property)
	if mode not in modes:
		frappe.throw(_("Pick a payment mode: {0}.").format(", ".join(modes)))

	folio = _guest_folio(res, create=True)
	from kamra.api import add_folio_payment
	# the one payment writer: cashier PIN, open till, ledger (Deposit) and
	# record_collected all happen in there
	add_folio_payment(folio, mode, amt,
	                  reference=reference or f"security-deposit:{res.name}",
	                  pin=pin, kind="Security Deposit", idempotency_key=key)
	from kamra.ledger import _fmt_currency
	from kamra.savings import log_action
	_rplaces, display, _eps = _deposit_precision(res.property)
	log_action("collect_security_deposit", "Reservation", res.name,
	           res.property,
	           rationale=f"{mode} security deposit {_fmt_currency(amt, display)} held for "
	                     f"{res.guest_name}")
	res.reload()
	return deposit_state(res)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Hotel Admin", "Kamra Agent")
def authorize_deposit(
	reservation: str,
	amount: float,
	gateway_auth_ref: str,
	card_last4: str,
	card_brand: str | None = None,
	expires_on: str | None = None,
	pin: str | None = None,
) -> dict:
	"""Create a non-GL card pre-authorization hold for a stay security deposit."""
	_require_preauth_available()
	res = _reservation(reservation, for_update=True)
	if res.status not in COLLECT_STATUSES:
		frappe.throw(_("A security deposit can't be taken on a {0} booking.").format(res.status))
	if not _has_doctype():
		frappe.throw(_("Security deposits are not set up on this site."))

	from kamra.authz import require_cashier_pin
	require_cashier_pin(res.property, pin)

	places, eps = _preauth_precision(res.property)
	amt = _parse_positive_amount(amount, places, _("Enter the deposit amount."))
	auth_ref = str(gateway_auth_ref or "").strip()
	if not auth_ref:
		frappe.throw(_("Enter the gateway authorization reference."))
	last4 = _validate_card_last4(card_last4, required=True)
	brand = str(card_brand or "").strip()
	if brand and len(brand) > 32:
		frappe.throw(_("[PREAUTH-PCI-001]: Card Brand exceeds 32 characters."))
	_assert_no_pci("gateway_auth_ref", auth_ref)
	_assert_no_pci("card_brand", brand)
	exp_str = _normalize_expires_on(expires_on)

	existing = frappe.db.get_value(
		"Security Deposit",
		{"reservation": res.name},
		["name", "status", "collected_amount", "authorized_amount", "preauth_hold", "expires_on"],
		as_dict=True, for_update=True,
	)
	if existing:
		eff = _effective_preauth(existing, places, eps, for_update=True)
		if eff["status"] == "Waived":
			frappe.throw(_("This deposit was waived."))
		if eff["status"] == "Refunded":
			frappe.throw(_("This deposit has already been refunded."))
		if eff["active"]:
			frappe.throw(_("A card pre-authorization hold is already active on this stay."))
		if flt(existing.get("collected_amount"), places) > eps:
			frappe.throw(_("A collected deposit is already held on this stay."))

	folio_name = _guest_folio(res, create=False)
	ctx = _resolve_preauth_context(res, folio_name=folio_name)
	payload = {
		"company": ctx["company"],
		"customer": ctx["customer"],
		"currency": ctx["currency"],
		"reference_doctype": "Reservation",
		"reference_name": res.name,
		"gateway_auth_ref": auth_ref,
		"card_brand": brand or None,
		"card_last4": last4,
		"authorized_amount": amt,
		"expires_on": exp_str,
		"note": _("Security deposit pre-authorization for {0}").format(res.name),
	}
	if ctx.get("branch"):
		payload["branch"] = ctx["branch"]
	if ctx.get("cost_center"):
		payload["cost_center"] = ctx["cost_center"]

	result = _call_preauth("authorize", payload)

	dep_name = ensure_deposit_for_reservation(res, amt)
	if not dep_name:
		frappe.throw(_("Security deposits are not set up on this site."))
	dep = frappe.get_doc("Security Deposit", dep_name)
	dep.status = "Authorized"
	dep.method = "authorization"
	dep.authorized_amount = flt(result.get("authorized_amount", amt), places)
	dep.collected_amount = 0.0
	dep.withheld_amount = 0.0
	dep.refunded_amount = 0.0
	dep.balance = 0.0
	dep.preauth_hold = result.get("name")
	dep.pending_capture_payment_entry = None
	dep.gateway_auth_ref = result.get("gateway_auth_ref") or auth_ref
	dep.card_brand = result.get("card_brand") or brand or None
	dep.card_last4 = result.get("card_last4") or last4
	dep.expires_on = result.get("expires_on") or exp_str
	dep.save(ignore_permissions=True)

	from kamra.ledger import _fmt_currency
	from kamra.savings import log_action
	card_label = f"{dep.card_brand} •••• {dep.card_last4}" if dep.card_brand else f"•••• {dep.card_last4}"
	log_action(
		"authorize_security_deposit",
		"Reservation",
		res.name,
		res.property,
		rationale=(
			f"Card pre-authorization {_fmt_currency(dep.authorized_amount, places)} "
			f"({card_label}, {dep.preauth_hold}) held for {res.guest_name}"
		),
	)
	return deposit_state(res)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Hotel Admin", "Kamra Agent")
def increment_deposit_auth(
	reservation: str,
	additional_amount: float,
	gateway_auth_ref: str | None = None,
	expires_on: str | None = None,
	pin: str | None = None,
) -> dict:
	"""Increase an active card pre-authorization hold (zero GL)."""
	_require_preauth_available()
	res = _reservation(reservation, for_update=True)
	if res.status not in COLLECT_STATUSES:
		frappe.throw(_("A security deposit can't be taken on a {0} booking.").format(res.status))
	dep = _get(res.name, for_update=True)

	places, eps = _preauth_precision(res.property)
	eff = _effective_preauth(dep, places, eps, for_update=True)
	if not eff["active"] or not dep.get("preauth_hold"):
		frappe.throw(_("No active card pre-authorization hold on this stay."))
	_require_no_capture_payment_entry(dep, eff)

	from kamra.authz import require_cashier_pin
	require_cashier_pin(res.property, pin)

	delta = _parse_positive_amount(
		additional_amount, places, _("Enter the additional amount to authorize.")
	)
	tx_ref = str(gateway_auth_ref or "").strip()
	_assert_no_pci("gateway_auth_ref", tx_ref)

	payload = {
		"name": dep.preauth_hold,
		"additional_amount": delta,
		"note": _("Incremental pre-authorization for {0}").format(res.name),
	}
	if tx_ref:
		payload["gateway_tx_ref"] = tx_ref
	if expires_on:
		payload["expires_on"] = _normalize_expires_on(expires_on)

	result = _call_preauth("increment", payload)
	dep.status = "Authorized"
	dep.authorized_amount = flt(result.get("authorized_amount"), places)
	if result.get("expires_on"):
		dep.expires_on = result.get("expires_on")
	dep.collected_amount = 0.0
	dep.balance = 0.0
	dep.save(ignore_permissions=True)

	from kamra.ledger import _fmt_currency
	from kamra.savings import log_action
	log_action(
		"increment_security_deposit_auth",
		"Reservation",
		res.name,
		res.property,
		rationale=(
			f"Incremented card pre-authorization {dep.preauth_hold} by "
			f"{_fmt_currency(delta, places)} to {_fmt_currency(dep.authorized_amount, places)}"
		),
	)
	return deposit_state(res)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Hotel Admin")
def capture_deposit_auth(
	reservation: str,
	amount: float,
	reason: str,
	evidence: str | None = None,
	pin: str | None = None,
	gateway_capture_ref: str | None = None,
	mode_of_payment: str | None = None,
	existing_payment_entry: str | None = None,
) -> dict:
	"""Capture all or part of an active card hold against damage (GL posts only here)."""
	_require_preauth_available()
	res = _reservation(reservation, for_update=True)
	dep = _get(res.name, for_update=True)
	places, eps = _preauth_precision(res.property)
	eff = _effective_preauth(dep, places, eps, for_update=True)
	if not eff["active"] or not dep.get("preauth_hold"):
		frappe.throw(_("No active card pre-authorization hold on this stay."))

	# The reservation lock serializes retries with the first draft creation.
	entry = _capture_payment_entry(dep, eff, for_update=True)
	if entry and entry.docstatus == 0:
		frappe.throw(_("Capture draft {0} is awaiting Finance submission.").format(entry.name))

	amt = _parse_positive_amount(amount, places, _("Enter the amount to capture."))
	reason_clean = str(reason or "").strip()
	if not reason_clean:
		frappe.throw(_("Capturing a pre-authorization needs a reason."))
	evidence_clean = str(evidence or "").strip()
	cap_ref = str(gateway_capture_ref or "").strip()
	_assert_no_pci("reason", reason_clean)
	_assert_no_pci("evidence", evidence_clean)
	_assert_no_pci("gateway_capture_ref", cap_ref)

	if entry:
		# Finance submitted the draft. Finalize against that receipt instead
		# of taking a second payment or posting a different amount.
		if (
			(existing_payment_entry and existing_payment_entry.strip() != entry.name)
			or flt(entry.paid_amount, places) != amt
		):
			frappe.throw(_("Use submitted capture Payment Entry {0} and its original amount.").format(entry.name))
		existing_payment_entry = entry.name

	folio = _guest_folio(res, create=True)
	if not folio or frappe.db.get_value("Folio", folio, "status") == "Closed":
		frappe.throw(_("The guest folio is closed - capture before closing it."))

	from kamra.authz import require_cashier_pin
	require_cashier_pin(res.property, pin)

	from kamra.business_date import get_business_date
	posting_date = get_business_date(res.property) or nowdate()
	payload = {
		"name": dep.preauth_hold,
		"capture_amount": amt,
		"posting_date": str(posting_date),
		"note": reason_clean[:240],
	}
	if cap_ref:
		payload["gateway_capture_ref"] = cap_ref
	if mode_of_payment:
		payload["mode_of_payment"] = str(mode_of_payment).strip()
	if existing_payment_entry:
		payload["existing_payment_entry"] = str(existing_payment_entry).strip()

	result = _call_preauth("capture", payload)

	if result.get("capture_mode") == "draft_pending_submit":
		pending = result.get("payment_entry")
		if not pending:
			frappe.throw(_("[HSP-DEP-PREAUTH-UNAVAILABLE]: Unexpected pre-authorization control response."))
		dep.pending_capture_payment_entry = pending
		dep.save(ignore_permissions=True)
		state = deposit_state(res)
		state["capture_mode"] = "draft_pending_submit"
		state["pending_payment_entry"] = pending
		state["payment_entry_docstatus"] = 0
		return state

	from kamra.api import _post_folio_charge
	from kamra.ledger import _fmt_currency, _money, _round_currency
	_post_folio_charge(folio, "Misc", f"{WITHHOLD_PREFIX}: {reason_clean}"[:140], amt, 0)
	_record_captured_folio_payment(res, folio, dep, amt, result.get("payment_entry"), reason_clean)

	dep.collected_amount = _round_currency(_money(dep.collected_amount) + _money(amt), places)
	dep.withheld_amount = _round_currency(_money(dep.withheld_amount) + _money(amt), places)
	dep.balance = max(
		0.0,
		_round_currency(
			_money(dep.collected_amount) - _money(dep.withheld_amount) - _money(dep.refunded_amount),
			places,
		),
	)
	dep.pending_capture_payment_entry = None
	dep.status = "Withheld"
	dep.reason = reason_clean
	if evidence_clean:
		dep.evidence = evidence_clean
	dep.save(ignore_permissions=True)

	from kamra.savings import log_action
	log_action(
		"capture_security_deposit_auth",
		"Reservation",
		res.name,
		res.property,
		rationale=(
			f"Captured {_fmt_currency(amt, places)} from {dep.preauth_hold}: {reason_clean}"
		),
	)
	state = deposit_state(res)
	state["payment_entry"] = result.get("payment_entry") or state.get("payment_entry")
	state["capture_mode"] = result.get("capture_mode") or "posted"
	return state


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Hotel Admin", "Kamra Agent")
def release_deposit_auth(
	reservation: str,
	reason: str | None = None,
	pin: str | None = None,
) -> dict:
	"""Release an uncaptured card pre-authorization hold (zero GL, clears checkout block)."""
	_require_preauth_available()
	res = _reservation(reservation, for_update=True)
	dep = _get(res.name, for_update=True)
	places, eps = _preauth_precision(res.property)
	eff = _effective_preauth(dep, places, eps, for_update=True)
	if not eff["active"] or not dep.get("preauth_hold"):
		frappe.throw(_("No active card pre-authorization hold on this stay."))
	_require_no_capture_payment_entry(dep, eff)

	from kamra.authz import require_cashier_pin
	require_cashier_pin(res.property, pin)

	reason_clean = str(reason or "").strip() or _("Released without capture")
	_assert_no_pci("reason", reason_clean)

	_call_preauth(
		"release",
		{
			"name": dep.preauth_hold,
			"release_reason": reason_clean,
			"note": reason_clean[:240],
		},
	)

	dep.status = "Released"
	dep.reason = reason_clean
	dep.collected_amount = 0.0
	dep.balance = 0.0
	dep.save(ignore_permissions=True)

	from kamra.savings import log_action
	log_action(
		"release_security_deposit_auth",
		"Reservation",
		res.name,
		res.property,
		rationale=f"Released pre-authorization {dep.preauth_hold}: {reason_clean}",
	)
	return deposit_state(res)


def waive_deposit(reservation: str, reason: str) -> dict:
	res = _reservation(reservation)
	if not (reason or "").strip():
		frappe.throw(_("Waiving a deposit needs a reason."))
	name = ensure_deposit_for_reservation(res)
	if not name:
		frappe.throw(_("No security deposit is required for this stay."))
	dep = frappe.get_doc("Security Deposit", name)
	places, eps = _preauth_precision(res.property)
	if flt(dep.collected_amount) > 0:
		frappe.throw(_("Money has already been taken - refund the deposit "
		               "instead of waiving it."))
	if _effective_preauth(dep, places, eps)["active"]:
		frappe.throw(_("A card pre-authorization hold is active - release it before waiving."))
	dep.status = "Waived"
	dep.method = "waiver"
	dep.reason = reason.strip()
	dep.save(ignore_permissions=True)
	from kamra.savings import log_action
	log_action("waive_security_deposit", "Reservation", res.name,
	           res.property, rationale=f"Deposit waived: {reason.strip()}")
	return deposit_state(res)


def withhold_deposit(reservation: str, amount: float, reason: str,
                     evidence: str | None = None,
                     pin: str | None = None) -> dict:
	"""Keep all or part of the held deposit against damage. The withheld
	part becomes a charge on the folio (revenue, Guest ledger) and the same
	amount moves out of the Deposit ledger to pay it."""
	res = _reservation(reservation, for_update=True)
	dep = _get(res.name, for_update=True)
	amt = flt(amount)
	if amt <= 0:
		frappe.throw(_("Enter the amount to withhold."))
	if not (reason or "").strip():
		frappe.throw(_("Withholding a deposit needs a reason."))
	from kamra.ledger import _fmt_currency
	_rplaces, display, eps = _deposit_precision(res.property)
	if amt > flt(dep.balance) + eps:
		frappe.throw(_("Only {0} of the deposit is still held.")
		             .format(_fmt_currency(dep.balance, display)))
	folio = _guest_folio(res)
	if not folio or frappe.db.get_value("Folio", folio, "status") == "Closed":
		frappe.throw(_("The guest folio is closed - withhold before closing it."))
	from kamra.api import _pin_guard, _post_folio_charge
	if dep.refunded_amount:
		from kamra.folio import lock_guest_deposit_windows
		lock_guest_deposit_windows(res.name)
	_pin_guard(folio, pin)

	reason = reason.strip()
	_post_folio_charge(folio, "Misc", f"{WITHHOLD_PREFIX}: {reason}"[:140],
	                   amt, 0)
	# the held money pays that charge: liability released, guest credited
	_transfer_to_guest(res, folio, dep, amt,
	                   f"Deposit applied to damage: {reason}")

	dep.withheld_amount = flt(dep.withheld_amount) + amt
	dep.reason = reason
	if evidence:
		dep.evidence = evidence
	dep.status = "Withheld"
	dep.save(ignore_permissions=True)
	from kamra.folio import _recalculate
	fdoc = frappe.get_doc("Folio", folio)
	_recalculate(fdoc)
	fdoc.save(ignore_permissions=True)
	if dep.refunded_amount:
		from kamra.folio import sync_deposit_payment_totals
		sync_deposit_payment_totals(fdoc)
	from kamra.savings import log_action
	log_action("withhold_security_deposit", "Reservation", res.name,
	           res.property, rationale=f"Withheld {_fmt_currency(amt, display)}: {reason}")
	return deposit_state(res)


def _transfer_to_guest(res, folio: str, dep, amt: float, why: str) -> None:
	"""Held money leaves the Deposit ledger (liability) and credits the Guest
	ledger - it is paying something on the bill."""
	from kamra.ledger import PAYMENT_CODES, write_ledger_entry
	code = PAYMENT_CODES["Security Deposit"][0]
	if not frappe.db.exists("Transaction Code", code):
		code = None
	for ledger, debit, credit in (("Deposit", amt, 0), ("Guest", 0, amt)):
		write_ledger_entry(
			property=res.property, folio=folio, ledger=ledger,
			debit=debit, credit=credit, description=why[:140],
			transaction_code=code,
			source_doctype="Security Deposit", source_name=dep.name)


def apply_held_to_bill(res) -> float:
	"""At check-out, once the folio is settled: whatever deposit is still
	held was used to pay the bill (the desk let it cover the balance rather
	than refunding it and taking a payment). Record that, so the Deposit
	ledger does not carry a liability for a guest who has left."""
	if not _has_doctype():
		return 0.0
	name = frappe.db.get_value(
		"Security Deposit", {"reservation": res.name}, "name")
	if not name:
		return 0.0
	from kamra.ledger import _fmt_currency
	_rplaces, display, eps = _deposit_precision(res.property)
	frappe.db.get_value("Reservation", res.name, "name", for_update=True)
	dep = frappe.get_doc("Security Deposit", name, for_update=True)
	amt = flt(dep.balance)
	if amt <= eps:
		return 0.0
	folio = _guest_folio(res)
	if not folio:
		return 0.0
	if dep.refunded_amount:
		from kamra.folio import lock_guest_deposit_windows
		lock_guest_deposit_windows(res.name)
	_transfer_to_guest(res, folio, dep, amt, "Deposit applied to the bill")
	dep.withheld_amount = flt(dep.withheld_amount) + amt
	dep.reason = "\n".join(filter(None, [  # nosemgrep: frappe-no-functional-code -- drops an empty previous reason
		dep.reason, _("Applied to the bill at check-out")]))
	dep.status = "Withheld"
	dep.save(ignore_permissions=True)
	from kamra.folio import _recalculate
	fdoc = frappe.get_doc("Folio", folio)
	_recalculate(fdoc)
	fdoc.save(ignore_permissions=True)
	if dep.refunded_amount:
		from kamra.folio import sync_deposit_payment_totals
		sync_deposit_payment_totals(fdoc)
	from kamra.savings import log_action
	log_action("apply_security_deposit", "Reservation", res.name,
	           res.property,
	           rationale=f"Held deposit {_fmt_currency(amt, display)} applied to the bill at check-out")
	return amt


def refund_deposit(reservation: str, amount: float | None = None,
                   mode: str | None = None, note: str | None = None,
                   pin: str | None = None, *, folio: str | None = None) -> dict:
	"""Return held funds in window order, or solely from the named Guest folio."""
	from kamra.api import _refund_folio_payment
	from kamra.folio import (
		_security_deposit_row_ids, allocate_historical_security_refunds,
		guest_security_windows, lock_guest_deposit_windows, refundable_security_on_folio,
	)
	from kamra.ledger import _fmt_currency, _money, _parse_currency_amount, _round_currency
	from kamra.localization import pack_for, payment_modes
	from kamra.savings import log_action

	res = _reservation(reservation, for_update=True)
	lock_guest_deposit_windows(res.name)
	dep = _get(res.name, for_update=True)
	rplaces, display, _eps = _deposit_precision(res.property)
	unit = Decimal(1).scaleb(-display)
	balance = _money(dep.balance)
	amt = (balance.quantize(unit, rounding=ROUND_DOWN) if amount in (None, "", 0, "0")
	       else _parse_currency_amount(amount, display))
	if amt <= 0:
		frappe.throw(_("Nothing is held to refund."))
	if amt > balance:
		frappe.throw(_("Only {0} of the deposit is still held.").format(_fmt_currency(balance, display)))

	# Frappe filters extra RPC arguments before the reservation-only public
	# wrapper runs. Honor an explicit window on that request as a restriction.
	form = frappe.local.form_dict or {}
	rpc = "kamra.api.refund_security_deposit"
	request = getattr(frappe.local, "request", None)
	if folio is None and (form.get("cmd") == rpc or (request and request.path.endswith("/method/" + rpc))):
		folio = form.get("folio")
	names = guest_security_windows(res.name, res.property, for_update=True)
	if not names or (folio is not None and folio not in names):
		frappe.throw(_("No guest folio for this stay."))
	windows = []
	for name in names:
		window = frappe.get_doc("Folio", name, for_update=True)
		windows.append((window, _security_deposit_row_ids(window)))
	historical = allocate_historical_security_refunds(windows, _money(dep.refunded_amount))
	remaining = amt
	allocations = []
	for window, sec_idx in windows:
		if folio is not None and window.name != folio:
			continue
		available = refundable_security_on_folio(
			window, sec_idx, historical.get(window.name, Decimal(0)),
		).quantize(unit, rounding=ROUND_DOWN)
		part = min(available, remaining)
		if part > 0:
			allocations.append((window.name, part))
			remaining -= part
		if not remaining:
			break
	if remaining:
		frappe.throw(_("Only {0} of the deposit is still held.").format(_fmt_currency(amt - remaining, display)))

	modes = payment_modes(pack_for(res.property), res.property)
	text = f"{REFUND_REASON} {dep.name}"
	if (note or "").strip():
		text += f": {note.strip()}"
	savepoint = "deposit_refund_" + frappe.generate_hash(length=8)
	frappe.db.savepoint(savepoint)
	details = []
	try:
		for name, part in allocations:
			tender = mode or _last_mode(name) or "Cash"
			if tender not in modes:
				frappe.throw(_("Pick a payment mode: {0}.").format(", ".join(modes)))
			# The post-ledger allocation must see this leg in the stay total.
			# The savepoint rolls both writes back if cash posting fails.
			dep.refunded_amount = _round_currency(_money(dep.refunded_amount) + part, rplaces)
			dep.refund_reference = text[:140]
			dep.balance = _round_currency(
				_money(dep.collected_amount) - _money(dep.withheld_amount) - _money(dep.refunded_amount), rplaces)
			_settle_status(dep, res)
			dep.save(ignore_permissions=True)
			_refund_folio_payment(name, float(part), tender, text[:140], pin=pin,
			                      ledger="Deposit", allow_closed=True, security_deposit_refund=True)
			details.append(f"{tender} deposit refund {_fmt_currency(part, display)}")
		log_action("refund_security_deposit", "Reservation", res.name, res.property,
		           rationale=", ".join(details))
		state = deposit_state(res)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	frappe.db.release_savepoint(savepoint)
	return state


def _last_mode(folio: str) -> str | None:
	return frappe.db.get_value(
		"Folio Payment",
		{"parent": folio, "payment_kind": "Security Deposit"},
		"mode", order_by="idx desc")


_PREAUTH_EVENT_LABELS = {
	"Authorize": "Authorized",
	"Increment": "Incremented",
	"Capture": "Captured",
	"Release": "Released",
	"Expire": "Expired",
}


def _has_permission(doctype: str, ptype: str, doc=None) -> bool:
	if doc is not None:
		if frappe.has_permission(doctype, ptype, doc=doc):
			return True
		return False
	if frappe.has_permission(doctype, ptype):
		return True
	return False


def deposit_state(res) -> dict:
	"""What the deposit panel shows: the policy, what is held or authorized,
	what came out, and which actions are open right now."""
	if isinstance(res, str):
		res = frappe.get_doc("Reservation", res)
	policy = _policy_amount(res.property)
	row = frappe.db.get_value(
		"Security Deposit", {"reservation": res.name},
		["name", "status", "method", "required_amount", "authorized_amount",
		 "collected_amount", "withheld_amount", "refunded_amount", "balance",
		 "reason", "refund_reference", "preauth_hold", "gateway_auth_ref",
		 "card_brand", "card_last4", "expires_on", "pending_capture_payment_entry"], as_dict=True) if _has_doctype() else None
	folio_fields = ["name", "status", "balance"]
	if frappe.get_meta("Folio").has_field("sales_invoice"):
		folio_fields.extend(["property", "grand_total", "sales_invoice"])
	folio = frappe.db.get_value(
		"Folio", {"reservation": res.name, "folio_type": "Guest"}, folio_fields, as_dict=True)
	if folio and folio.get("sales_invoice"):
		from kamra.folio import settlement_balance
		folio.balance = settlement_balance(folio)

	from kamra.ledger import _money, _round_currency
	rplaces, _display, cash_eps = _deposit_precision(res.property)
	places, eps = _preauth_precision(res.property)

	eff = _effective_preauth(row, places, eps)
	status = eff["status"] if row else ("Required" if policy > 0 else "None")
	held = flt(row.balance) if row else 0.0
	collected = flt(row.collected_amount) if row else 0.0
	required = flt(row.required_amount) if row else policy
	authorized = eff["authorized_amount"]
	remaining_hold = eff["remaining_hold_amount"]
	active_preauth = eff["active"]
	preauth_available = _preauth_available()
	entry = _capture_payment_entry(row, eff)
	pending = entry.name if entry and entry.docstatus == 0 else None
	can_create_hold = preauth_available and not active_preauth and _has_permission("Credit Preauthorization", "create")
	can_write_hold = (
		preauth_available and active_preauth
		and frappe.db.exists("Credit Preauthorization", eff["preauth_hold"])
		and _has_permission("Credit Preauthorization", "write", doc=eff["preauth_hold"])
	)
	can_receive_payment = False
	if can_write_hold and frappe.db.exists("DocType", "Payment Entry"):
		if not entry:
			can_receive_payment = _has_permission("Payment Entry", "create")
		elif entry.docstatus == 1:
			can_receive_payment = (
				_has_permission("Payment Entry", "read")
				and _has_permission("Payment Entry", "submit")
			)

	folio_open = bool(folio and folio.status != "Closed")
	folio_can_capture = (not folio) or folio_open
	in_play = res.status in COLLECT_STATUSES
	history = []
	from kamra.folio import _security_deposit_row_ids
	identified_refunds = _money(0)
	for window in frappe.get_all("Folio", filters={"reservation": res.name},
	                             fields=["name"], order_by="creation asc"):
		folio_doc = frappe.get_doc("Folio", window.name)
		for idx in sorted(_security_deposit_row_ids(folio_doc)):
			p = folio_doc.payments[idx]
			if p.payment_kind == "Security Deposit":
				history.append({"kind": "Collected", "date": str(p.posting_date),
				                "mode": p.mode, "amount": flt(p.amount)})
			elif p.payment_kind == "Refund":
				identified_refunds -= _money(p.amount)
				history.append({"kind": "Refunded", "date": str(p.posting_date),
				                "mode": p.mode, "amount": -flt(p.amount)})
		for c in frappe.get_all(
				"Folio Charge",
				filters={"parent": window.name, "parenttype": "Folio",
				         "description": ("like", f"{WITHHOLD_PREFIX}%")},
				fields=["posting_date", "description", "amount"],
				order_by="idx asc"):
			history.append({"kind": "Withheld", "date": str(c.posting_date),
			                "amount": flt(c.amount),
			                "note": c.description.split(":", 1)[-1].strip()})
	# Old stays recorded only a cumulative refund. Show the unexplained
	# portion once without inventing a payment, date or tender.
	historical = _money(row.refunded_amount if row else 0) - identified_refunds
	if historical > 0:
		history.append({"kind": "Refunded", "date": "", "amount": float(historical),
		                "note": _("Recorded historical refund")})

	if eff["preauth_hold"] and frappe.db.exists("DocType", "Credit Preauthorization Event"):
		masked = (
			f"{eff['card_brand']} •••• {eff['card_last4']}"
			if eff["card_brand"] and eff["card_last4"]
			else (f"•••• {eff['card_last4']}" if eff["card_last4"] else None)
		)
		for ev in frappe.get_all(
			"Credit Preauthorization Event",
			filters={"parent": eff["preauth_hold"], "parenttype": "Credit Preauthorization"},
			fields=["event_type", "delta_amount", "event_at", "note"],
			order_by="idx asc",
		):
			ev_kind = _PREAUTH_EVENT_LABELS.get(ev.get("event_type"), ev.get("event_type") or "Authorized")
			ev_date = str(ev.get("event_at") or "")[:10]
			ev_amt = flt(ev.get("delta_amount"), places)
			history.append({
				"kind": ev_kind,
				"date": ev_date,
				"mode": masked,
				"amount": -ev_amt if ev_kind in ("Released", "Expired") else ev_amt,
				"note": ev.get("note") or None,
			})

	if status in ("Waived", "Refunded"):
		due = 0.0
	elif active_preauth:
		due = max(0.0, _round_currency(_money(required) - _money(authorized), places))
	else:
		due = max(0.0, _round_currency(_money(required) - _money(collected), rplaces))

	cash_blocks_checkout = res.status == "Checked In" and held > cash_eps
	preauth_blocks_checkout = _card_hold_blocks_checkout(res, eff, eps)

	return {
		"name": row.name if row else None,
		"status": status,
		"method": row.get("method") if row else "payment",
		"policy_amount": policy,
		"required": required,
		"authorized_amount": authorized,
		"remaining_hold_amount": remaining_hold,
		"collected": collected,
		"withheld": flt(row.withheld_amount) if row else 0.0,
		"refunded": flt(row.refunded_amount) if row else 0.0,
		"held": held,
		"due": due,
		"reason": row.reason if row else None,
		"folio": folio.name if folio else None,
		"folio_credit": max(0.0, -flt(folio.balance)) if folio else 0.0,
		"preauth_hold": eff["preauth_hold"],
		"gateway_auth_ref": eff["gateway_auth_ref"],
		"card_brand": eff["card_brand"],
		"card_last4": eff["card_last4"],
		"expires_on": eff["expires_on"],
		"payment_entry": eff["payment_entry"],
		"pending_capture_payment_entry": pending,
		"preauth_available": preauth_available,
		"history": history,
		"can_collect": in_play and status not in ("Waived", "Refunded") and not active_preauth,
		"can_waive": in_play and collected <= eps and not active_preauth and status != "Waived"
			and required > eps,
		"can_withhold": held > cash_eps and folio_open,
		"can_refund": held > cash_eps and bool(folio),
		"can_authorize": (
			can_create_hold and in_play
			and collected <= eps
			and status not in ("Waived", "Refunded", "Withheld", "Collected", "Held")
		),
		"can_increment": bool(can_write_hold and not entry and in_play),
		"can_capture": bool(can_receive_payment and folio_can_capture),
		"can_release": bool(can_write_hold and not entry),
		"blocks_checkout_card_hold": preauth_blocks_checkout,
		# check-out refuses while the guest is owed held cash or has an active
		# uncaptured card pre-authorization hold
		"blocks_checkout": cash_blocks_checkout or preauth_blocks_checkout,
	}


def deposit_satisfied(reservation: str) -> bool:
	"""True when deposit policy is met (none required, waived, collected, or active authorized hold)."""
	prop = frappe.db.get_value("Reservation", reservation, "property")
	required = _policy_amount(prop)
	if required <= 0:
		return True
	if not _has_doctype():
		return True
	row = frappe.db.get_value(
		"Security Deposit",
		{"reservation": reservation},
		["status", "collected_amount", "authorized_amount", "required_amount", "preauth_hold", "expires_on"],
		as_dict=True,
	)
	if not row:
		return False
	if row.status == "Waived":
		return True
	places, eps = _preauth_precision(prop)
	eff = _effective_preauth(row, places, eps)
	if eff["active"]:
		return flt(eff["authorized_amount"], places) + eps >= flt(row.required_amount, places)
	return flt(row.collected_amount, places) + eps >= flt(row.required_amount, places)
