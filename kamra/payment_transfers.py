"""K2 receipt transfers, folio windows overview, window payee updates, and K3 Credit & Rebill.

Kept in its own module so PR 4 stays strictly disjoint from kamra/api.py and
kamra/folio.py while aligning 1:1 with bridge@30 KMR-PAY-001 / KMR-CN-001 /
KMR-LC-001 fences.
"""

from __future__ import annotations

import hashlib
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import cint, flt

from kamra.authz import (
	assert_property_access,
	assert_record_access,
	require_cashier_pin,
	require_roles,
)

_FLOAT_DUST = Decimal("1e-9")


def _parse_transfer_amount(amount, places: int, label: str = "Transfer") -> Decimal:
	"""Parse a positive amount quantized strictly to the currency's ISO 4217 minor unit."""
	unit = Decimal(1).scaleb(-places)
	try:
		value = Decimal(str(amount))
	except (InvalidOperation, TypeError, ValueError):
		value = None
	if value is None or not value.is_finite() or value <= 0:
		frappe.throw(_("{0} amount must be positive.").format(_(label)))
	quantized = value.quantize(unit, rounding=ROUND_HALF_UP)
	if quantized <= 0 or abs(value - quantized) >= _FLOAT_DUST:
		if places == 0:
			frappe.throw(_("Enter a positive whole-unit amount."))
		frappe.throw(_("Enter a positive amount with at most {0} decimal places.").format(places))
	return quantized


def _has_submitted_sales_invoice(folio_name: str) -> bool:
	if not frappe.db.exists("DocType", "Sales Invoice"):
		return False
	meta = frappe.get_meta("Sales Invoice")
	if not meta.has_field("kamra_ref"):
		return False
	return bool(frappe.db.exists("Sales Invoice", {"kamra_ref": folio_name, "docstatus": 1}))


def _assert_open_uninvoiced(folio_doc) -> None:
	if (
		folio_doc.status != "Open"
		or folio_doc.get("invoice_number")
		or folio_doc.get("sales_invoice")
		or _has_submitted_sales_invoice(folio_doc.name)
	):
		frappe.throw(_("KMR-CO-001: Both folios must be open and uninvoiced to transfer a receipt."))


def _remaining_transferable(src_doc, orig, places: int) -> Decimal:
	"""Compute how much of `orig` on `src_doc` is still available to transfer."""
	unit = Decimal(1).scaleb(-places)
	orig_amt = Decimal(str(orig.amount or 0))
	orig_pe = orig.get("payment_entry") or None
	key_prefix = f"PT:{orig.name}:"
	deductions = Decimal(0)
	for p in src_doc.get("payments") or []:
		if p.name == orig.name:
			continue
		amt = Decimal(str(p.amount or 0))
		if p.get("refund_of") == orig.name and amt < 0:
			deductions += amt
			continue
		if orig_pe and p.get("reversal_of") == orig_pe and amt < 0:
			deductions += amt
			continue
		if p.get("transferred_to") and amt < 0:
			tgk = p.get("transfer_group_key") or ""
			if (orig_pe and p.get("payment_entry") == orig_pe) or tgk.startswith(key_prefix):
				deductions += amt
	return max((orig_amt + deductions).quantize(unit, rounding=ROUND_HALF_UP), Decimal(0))


def _record_transfer_ledger(src_doc, dst_doc, orig, src_row, dst_row, amt: Decimal,
                            reason: str, business_date: str) -> None:
	"""Move the receipt balance cleanly between source and target folio ledgers."""
	from kamra.ledger import _advance_held, write_ledger_entry

	deposit_leg = Decimal(0)
	if (orig.payment_kind or "Payment") == "Advance":
		held = Decimal(str(_advance_held(src_doc.name, orig.name, property=src_doc.property) or 0))
		if held > 0:
			deposit_leg = min(held, amt)
	guest_leg = amt - deposit_leg

	legs = []
	if deposit_leg > 0:
		legs.append(("Deposit", float(deposit_leg)))
	if guest_leg > 0:
		legs.append(("Guest", float(guest_leg)))

	code = src_row.get("transaction_code") or orig.get("transaction_code")
	for ledger_name, leg_amt in legs:
		write_ledger_entry(
			property=src_doc.property,
			folio=src_doc.name,
			ledger=ledger_name,
			debit=leg_amt,
			credit=0,
			description=f"Transfer to {dst_doc.name}: {reason}"[:140],
			transaction_code=code,
			source_doctype="Folio Payment",
			source_name=src_row.name,
			business_date=business_date,
		)
		write_ledger_entry(
			property=dst_doc.property,
			folio=dst_doc.name,
			ledger=ledger_name,
			debit=0,
			credit=leg_amt,
			description=f"Transfer from {src_doc.name}: {reason}"[:140],
			transaction_code=code,
			source_doctype="Folio Payment",
			source_name=dst_row.name,
			business_date=business_date,
		)


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance")
def transfer_folio_payment(
	from_folio: str,
	payment_row: str,
	to_folio: str,
	reason: str,
	amount: float | None = None,
	pin: str | None = None,
	idempotency_key: str | None = None,
) -> dict:
	"""Transfer all or part of an open receipt between two open, uninvoiced folios
	of the same stay or group (OPERA Transfer Deposit / K2).

	Follows bridge@30 KMR-PAY-001 save ordering strictly: source compensating
	negative row (transferred_to) is saved BEFORE target positive row
	(transferred_from), preserving payment_entry, mode, and currency.
	"""
	from kamra.folio import settlement_balance
	assert_record_access("Folio", from_folio)
	assert_record_access("Folio", to_folio)

	if not from_folio or not to_folio:
		frappe.throw(_("Choose both source and destination folios."))
	if from_folio == to_folio:
		frappe.throw(_("Choose a different destination folio."))

	reason_text = (reason or "").strip()
	if not reason_text:
		frappe.throw(_("A reason is required for a receipt transfer."))
	if len(reason_text) > 300:
		frappe.throw(_("Transfer reason must be at most 300 characters."))

	src_prop = frappe.db.get_value("Folio", from_folio, "property")
	dst_prop = frappe.db.get_value("Folio", to_folio, "property")
	if not src_prop or not dst_prop:
		frappe.throw(_("Folio not found."))
	assert_property_access(src_prop)
	assert_property_access(dst_prop)
	if src_prop != dst_prop:
		frappe.throw(_("KMR-CO-001: Receipts can only transfer between folios at the same property."))

	require_cashier_pin(src_prop, pin)

	key = (idempotency_key or "").strip() or None
	if key and len(key) > 140:
		frappe.throw(_("Idempotency key is too long."))

	from kamra.api import _lock_folio_scope, _with_snapshot_retry

	def _transfer_once():
		# Deterministic sorted lock [min, max] prevents AB/BA deadlocks.
		_lock_folio_scope(folios=sorted({from_folio, to_folio}))

		src = frappe.get_doc("Folio", from_folio)
		dst = frappe.get_doc("Folio", to_folio)
		assert_property_access(src.property)
		assert_property_access(dst.property)
		if src.property != dst.property:
			frappe.throw(_("KMR-CO-001: Receipts can only transfer between folios at the same property."))

		from kamra.business_date import get_business_date
		from kamra.erp_bridge import is_bridge_active
		from kamra.folio import _recalculate, _same_stay_or_group
		from kamra.ledger import _currency_places, home_currency
		from kamra.localization import money_text

		places = _currency_places(src.property)
		home_ccy = home_currency(src.property)

		# Idempotent replay check before running balance checks
		if key:
			existing_src = next(
				(
					p for p in (src.payments or [])
					if p.get("idempotency_key") == key and p.get("transferred_to")
				),
				None,
			)
			if existing_src:
				if existing_src.transferred_to != dst.name:
					frappe.throw(_("This retry key already belongs to a different transfer."))
				if amount not in (None, ""):
					req_amt = _parse_transfer_amount(amount, places)
					if abs(Decimal(str(existing_src.amount or 0))) != req_amt:
						frappe.throw(_("This retry key already belongs to a different transfer."))
				tgk = existing_src.get("transfer_group_key")
				existing_dst = next(
					(
						p for p in (dst.payments or [])
						if p.get("transferred_from") == src.name and p.get("transfer_group_key") == tgk
					),
					None,
				)
				return {
					"ok": True,
					"idempotent_replay": 1,
					"from_folio": src.name,
					"to_folio": dst.name,
					"payment_row": payment_row,
					"source_row": existing_src.name,
					"target_row": existing_dst.name if existing_dst else None,
					"amount": abs(float(existing_src.amount or 0)),
					"transfer_group_key": tgk,
					"from_balance": settlement_balance(src),
					"to_balance": settlement_balance(dst),
				}

		_assert_open_uninvoiced(src)
		_assert_open_uninvoiced(dst)

		if not _same_stay_or_group(src, dst):
			frappe.throw(_("Receipts can only transfer between folios of the same stay or group."))

		if (src.get("customer") or dst.get("customer")) and src.get("customer") != dst.get("customer"):
			frappe.throw(_("KMR-PAY-001: Receipts can only transfer between folios with the same payer."))

		orig = next((p for p in (src.payments or []) if p.name == payment_row), None)
		if not orig or Decimal(str(orig.amount or 0)) <= 0:
			frappe.throw(_("That payment is not on the source folio."))
		if (orig.payment_kind or "Payment") not in ("Payment", "Advance"):
			frappe.throw(_("Only Payment and Advance receipts can be transferred between windows."))
		if orig.get("reversal_of") or orig.get("refund_of"):
			frappe.throw(_("Refunds and reversals cannot be transferred."))
		if orig.get("transferred_from") or orig.get("transferred_to"):
			frappe.throw(_("A transferred receipt cannot be transferred a second time."))
		if flt(orig.get("fx_amount")) != 0 or (orig.get("currency") and orig.get("currency") != home_ccy):
			frappe.throw(_("Foreign currency receipts cannot be transferred between windows."))

		pe_field = frappe.get_meta("Folio Payment").has_field("payment_entry")
		pe_name = orig.get("payment_entry") if pe_field else None
		if is_bridge_active() and pe_field and not pe_name:
			frappe.throw(_("KMR-PAY-001: Only receipts backed by a submitted payment entry can be transferred."))

		remaining = _remaining_transferable(src, orig, places)
		if remaining <= 0:
			frappe.throw(_("This receipt has no remaining balance to transfer."))

		if amount in (None, ""):
			amt_dec = remaining
		else:
			amt_dec = _parse_transfer_amount(amount, places)
		if amt_dec > remaining:
			frappe.throw(_("Only {0} of this receipt is left to transfer.").format(
				money_text(src.property, float(remaining))
			))

		amt = float(amt_dec)
		bd = get_business_date(src.property)
		ccy = orig.get("currency") or home_ccy
		payment_kind = orig.payment_kind or "Payment"
		suffix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] if key else frappe.generate_hash(length=12)
		group_key = f"PT:{orig.name}:{suffix}"[:140]

		# Step A (bridge@30 mandatory sequence): save source negative row FIRST.
		src_label = f"W{dst.window_no}" if cint(dst.get("window_no")) else dst.name
		dst_label = f"W{src.window_no}" if cint(src.get("window_no")) else src.name
		src_row_payload = {
			"posting_date": bd,
			"payment_kind": payment_kind,
			"mode": orig.mode,
			"amount": -amt,
			"currency": ccy,
			"fx_amount": 0,
			"exchange_rate": 1,
			"reference": f"Transfer to {src_label}: {reason_text[:80]}"[:140],
			"transaction_code": orig.get("transaction_code"),
			"gl_code": orig.get("gl_code"),
			"transferred_to": dst.name,
			"transfer_group_key": group_key,
			"idempotency_key": key,
		}
		if pe_name:
			src_row_payload["payment_entry"] = pe_name

		src.append("payments", src_row_payload)
		_recalculate(src, payments_only=True)
		src.save(ignore_permissions=True)
		src_row = src.payments[-1]

		# Step B: save target positive row SECOND.
		dst_row_payload = {
			"posting_date": bd,
			"payment_kind": payment_kind,
			"mode": orig.mode,
			"amount": amt,
			"currency": ccy,
			"fx_amount": 0,
			"exchange_rate": 1,
			"reference": f"From {dst_label}: {reason_text[:80]}"[:140],
			"transaction_code": orig.get("transaction_code"),
			"gl_code": orig.get("gl_code"),
			"transferred_from": src.name,
			"transfer_group_key": group_key,
		}
		if pe_name:
			dst_row_payload["payment_entry"] = pe_name

		dst.append("payments", dst_row_payload)
		_recalculate(dst, payments_only=True)
		dst.save(ignore_permissions=True)
		dst_row = dst.payments[-1]

		_record_transfer_ledger(src, dst, orig, src_row, dst_row, amt_dec, reason_text, bd)

		from kamra.savings import log_action
		log_action(
			"payment_transfer",
			"Folio",
			dst.name,
			src.property,
			rationale=f"Transferred {money_text(src.property, amt)} from {src.name} to {dst.name}: {reason_text[:200]}",
		)

		return {
			"ok": True,
			"from_folio": src.name,
			"to_folio": dst.name,
			"payment_row": orig.name,
			"source_row": src_row.name,
			"target_row": dst_row.name,
			"amount": amt,
			"transfer_group_key": group_key,
			"from_balance": settlement_balance(src),
			"to_balance": settlement_balance(dst),
		}

	return _with_snapshot_retry(_transfer_once)


@frappe.whitelist()
@require_roles("Front Desk", "Finance", "Night Auditor")
def folio_windows_overview(folio: str) -> dict:
	"""Return sorted W1..W8 tabs with payee, totals, invoice status, and routing counts."""
	assert_record_access("Folio", folio)
	current = frappe.db.get_value(
		"Folio",
		folio,
		["name", "property", "reservation", "group_booking", "folio_type", "window_no"],
		as_dict=True,
	)
	if not current:
		frappe.throw(_("Folio {0} not found.").format(folio))
	assert_property_access(current.property)

	from kamra.folio import credit_note_positions, window_cap

	cap = min(window_cap(current.property), 8)
	has_si_col = frappe.get_meta("Folio").has_field("sales_invoice")
	fields = [
		"name", "folio_type", "status", "window_no", "payee_type",
		"payee_company", "payee_agent", "bill_to_name", "payment_method",
		"guest_name", "invoice_number", "charges_total", "grand_total",
		"payments_total", "balance", "reservation", "group_booking", "creation",
	]
	if has_si_col:
		fields.append("sales_invoice")

	stay_company = None
	group_id = current.group_booking
	rows = []
	if current.reservation:
		rows = frappe.get_all(
			"Folio",
			filters={"reservation": current.reservation, "property": current.property},
			fields=fields,
			order_by="window_no asc, creation asc",
		)
		res_info = frappe.db.get_value(
			"Reservation", current.reservation, ["company", "group_booking"], as_dict=True
		)
		if res_info:
			stay_company = res_info.company
			group_id = res_info.group_booking or group_id
		if group_id:
			seen = {r.name for r in rows}
			for m in frappe.get_all(
				"Folio",
				filters={"group_booking": group_id, "folio_type": "Group", "property": current.property},
				fields=fields,
				order_by="window_no asc, creation asc",
			):
				if m.name not in seen:
					rows.append(m)
	elif group_id:
		rows = frappe.get_all(
			"Folio",
			filters={"group_booking": group_id, "folio_type": "Group", "property": current.property},
			fields=fields,
			order_by="window_no asc, creation asc",
		)
	else:
		row = frappe.db.get_value("Folio", folio, fields, as_dict=True)
		rows = [row] if row else []

	group_company = group_id and frappe.db.get_value("Group Booking", group_id, "company")
	company_ids = list({
		c for r in rows
		for c in (r.get("payee_company"), stay_company if r.folio_type == "Company" else None,
		          group_company if r.folio_type == "Group" else None)
		if c
	})
	company_names = dict(frappe.get_all(
		"Corporate Account",
		filters={"name": ("in", company_ids)},
		fields=["name", "company_name"],
		as_list=True,
	)) if company_ids else {}

	agent_ids = list({r.payee_agent for r in rows if r.get("payee_agent")})
	agent_names = dict(frappe.get_all(
		"Travel Agent",
		filters={"name": ("in", agent_ids)},
		fields=["name", "agent_name"],
		as_list=True,
	)) if agent_ids else {}

	# Count active routing rules targeting each stay window or group master window
	window_rule_counts: dict[int, int] = {}
	group_rule_counts: dict[int, int] = {}
	if current.reservation:
		for ri in frappe.get_all(
			"Routing Instruction",
			filters={"reservation": current.reservation, "disabled": 0},
			fields=["target_type", "target_window"],
		):
			wn = cint(ri.target_window) or 1
			if ri.target_type == "Window":
				window_rule_counts[wn] = window_rule_counts.get(wn, 0) + 1
			elif ri.target_type == "Group Master":
				group_rule_counts[wn] = group_rule_counts.get(wn, 0) + 1
	elif group_id:
		for ri in frappe.get_all(
			"Routing Instruction",
			filters={"group_booking": group_id, "reservation": ("is", "not set"), "disabled": 0},
			fields=["target_type", "target_window"],
		):
			wn = cint(ri.target_window) or 1
			if ri.target_type == "Group Master":
				group_rule_counts[wn] = group_rule_counts.get(wn, 0) + 1

	# Optional fiscalization status when Sales Invoice exists
	efris_map: dict[str, bool] = {}
	positions = credit_note_positions(rows, current.property)
	si_names = [p["sales_invoice"] for p in positions if p["sales_invoice"]]
	if si_names and frappe.db.exists("DocType", "Sales Invoice"):
		si_meta = frappe.get_meta("Sales Invoice")
		check_fields = [f for f in ("efris_einvoice_status", "efris_irn") if si_meta.has_field(f)]
		if check_fields:
			for si in frappe.get_all(
				"Sales Invoice",
				filters={"name": ("in", si_names)},
				fields=["name", *check_fields],
			):
				efris_map[si.name] = bool(
					(si.get("efris_einvoice_status") == "EFRIS Generated")
					or si.get("efris_irn")
				)

	windows = []
	for r, position in zip(rows, positions, strict=True):
		wn = cint(r.get("window_no"))
		is_group = r.folio_type == "Group"
		payee_type = r.get("payee_type") or (
			"Company" if r.folio_type in ("Company", "Group") and (r.get("payee_company") or stay_company or group_company)
			else "Guest"
		)
		eff_company = (
			r.get("payee_company")
			or (group_company if is_group else None)
			or (stay_company if r.folio_type == "Company" else None)
		)
		if r.get("bill_to_name"):
			payee_name = r.bill_to_name
		elif payee_type == "Company" and eff_company:
			payee_name = company_names.get(eff_company) or eff_company
		elif payee_type == "Travel Agent" and r.get("payee_agent"):
			payee_name = agent_names.get(r.payee_agent) or r.payee_agent
		else:
			payee_name = r.get("guest_name") or r.name

		si_name = position["sales_invoice"]
		routed_count = (group_rule_counts if is_group else window_rule_counts).get(wn, 0)
		windows.append({
			"window_no": wn,
			"folio": r.name,
			"folio_type": r.folio_type,
			"payee_type": payee_type,
			"payee_name": payee_name,
			"payee_company": r.get("payee_company"),
			"company": eff_company if payee_type == "Company" or is_group else None,
			"payee_agent": r.get("payee_agent"),
			"agent_name": agent_names.get(r.payee_agent) if r.get("payee_agent") else None,
			"bill_to_name": r.get("bill_to_name"),
			"payment_method": r.get("payment_method"),
			"status": r.status,
			"invoice_number": r.get("invoice_number"),
			"charges_total": float(r.get("charges_total") or 0),
			"grand_total": float(r.get("grand_total") or 0),
			"payments_total": float(r.get("payments_total") or 0),
			"balance": float(r.get("balance") or 0),
			**position,
			"efris_fiscalized": bool(efris_map.get(si_name, False)) if si_name else False,
			"routed_rule_count": routed_count,
		})

	windows.sort(key=lambda w: (1 if w["folio_type"] == "Group" else 0, w["window_no"] or 99, 0 if w["status"] == "Open" else 1, w["folio"]))

	# Match folio.next_window: taken window numbers on the current stay (or group)
	if current.reservation:
		taken = {w["window_no"] for w in windows if w["folio_type"] != "Group" and 1 <= w["window_no"] <= cap}
	elif group_id:
		taken = {w["window_no"] for w in windows if w["folio_type"] == "Group" and 1 <= w["window_no"] <= cap}
	else:
		taken = set(range(1, cap + 1))

	return {
		"folio": folio,
		"property": current.property,
		"reservation": current.reservation,
		"group_booking": group_id,
		"window_cap": cap,
		"can_add_window": len(taken) < cap and bool(current.reservation or group_id),
		"windows": windows,
	}


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Finance", "Hotel Admin")
def update_folio_window(
	folio: str,
	payee_type: str | None = None,
	payee_company: str | None = None,
	payee_agent: str | None = None,
	payment_method: str | None = None,
	bill_to_name: str | None = None,
) -> dict:
	"""Update an open, uninvoiced billing window's payee or settlement method
	through a governed endpoint (respects KMR-LC-001 and KMR-CN-001)."""
	assert_record_access("Folio", folio)
	frappe.has_permission("Folio", "write", throw=True)

	from kamra.api import _lock_folio_scope
	_lock_folio_scope(folios=[folio])
	doc = frappe.get_doc("Folio", folio)
	assert_property_access(doc.property)

	if (
		doc.status != "Open"
		or doc.get("invoice_number")
		or doc.get("sales_invoice")
		or _has_submitted_sales_invoice(doc.name)
	):
		frappe.throw(_("KMR-CN-001: Issued or closed windows cannot change payee or payment method."))

	if payee_type is not None:
		clean_type = (payee_type or "").strip() or "Guest"
		if clean_type not in ("Guest", "Company", "Travel Agent"):
			frappe.throw(_("Choose Guest, Company, or Travel Agent as the window payee."))
		doc.payee_type = clean_type
		if clean_type == "Company":
			doc.payee_company = (payee_company or "").strip() or None
			doc.payee_agent = None
		elif clean_type == "Travel Agent":
			doc.payee_agent = (payee_agent or "").strip() or None
			doc.payee_company = None
		else:
			doc.payee_company = None
			doc.payee_agent = None
	else:
		if payee_company is not None and doc.payee_type == "Company":
			doc.payee_company = (payee_company or "").strip() or None
		if payee_agent is not None and doc.payee_type == "Travel Agent":
			doc.payee_agent = (payee_agent or "").strip() or None

	if payment_method is not None:
		doc.payment_method = (payment_method or "").strip() or None
	if bill_to_name is not None:
		doc.bill_to_name = (bill_to_name or "").strip()[:140] or None

	doc.save(ignore_permissions=True)

	from kamra.savings import log_action
	log_action(
		"update_folio_window",
		"Folio",
		doc.name,
		doc.property,
		rationale=f"Updated W{doc.window_no or 1} payee={doc.payee_type}, method={doc.payment_method or '-'}",
	)
	return {
		"ok": True,
		"folio": doc.name,
		"window_no": cint(doc.window_no),
		"payee_type": doc.payee_type,
		"payee_company": doc.payee_company,
		"payee_agent": doc.payee_agent,
		"payment_method": doc.payment_method,
		"bill_to_name": doc.bill_to_name,
	}


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Hotel Admin")
def credit_and_rebill(
	folio: str,
	reason: str,
	mode: str = "full",
	amount: float | None = None,
	pin: str | None = None,
) -> dict:
	"""Unified Credit & Rebill entry point for closed/invoiced folios (K3).

	Compatibility wrapper for unbridged callers. Bridge HTTP clients must
	call kamra.api.cancel_invoice for full corrections or
	kamra.ledger.issue_credit_note for partial credits directly.
	"""
	assert_record_access("Folio", folio)
	prop = frappe.db.get_value("Folio", folio, "property")
	if not prop:
		frappe.throw(_("Folio {0} not found.").format(folio))
	assert_property_access(prop)

	mode_clean = (mode or "full").strip().lower()
	if mode_clean not in ("full", "partial"):
		frappe.throw(_("Mode must be full or partial."))

	reason_text = (reason or "").strip()
	if not reason_text:
		frappe.throw(_("A reason is required."))
	if len(reason_text) > 500:
		frappe.throw(_("Reason must be at most 500 characters."))

	if mode_clean == "full":
		from kamra.api import cancel_invoice
		out = cancel_invoice(folio, reason_text, pin=pin)
		return {**(out if isinstance(out, dict) else {"result": out}), "mode": "full"}

	from kamra.ledger import _currency_places, issue_credit_note

	places = _currency_places(prop)
	amt_dec = _parse_transfer_amount(amount, places, label="Credit")
	out = issue_credit_note(folio, float(amt_dec), reason_text, pin=pin)
	return {**(out if isinstance(out, dict) else {"result": out}), "mode": "partial"}
