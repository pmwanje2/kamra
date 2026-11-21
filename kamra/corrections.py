"""OPERA folio corrections. Fiscal documents stay with the accounting bridge."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

import frappe

from kamra.business_date import get_business_date
from kamra.ledger import list_reason_codes, round_cents

LABELS = {
	"void_charge": "Void charge",
	"reverse_payment": "Reverse payment",
	"reclassify_tender": "Correct payment mode",
	"partial_refund": "Refund part of payment",
}
FISCAL = (
	"KMR-CN-001: Ask Finance for a Credit Note against the original invoice and a linked refund or rebill."
)
JOURNAL = "Ask Finance to reclassify the tender by Journal Entry; the original receipt stays on file."


def _row(doc, name):
	for field in ("charges", "payments"):
		for row in doc.get(field):
			if row.name == name:
				return row, field == "charges"
	frappe.throw("Select a line on this folio.")


def _block(doc, row, charge):
	day = str(row.posting_date)
	if (
		doc.status != "Open"
		or doc.invoice_number
		or doc.get("sales_invoice")
		or (
			doc.reservation and frappe.db.get_value("Reservation", doc.reservation, "status") == "Checked Out"
		)
		or day != str(get_business_date(doc.property))
		or frappe.db.exists(
			"Night Audit Run",
			{
				"property": doc.property,
				"business_date": day,
				"status": ["in", ["Completed", "Completed with Warnings"]],
			},
		)
	):
		return FISCAL
	if charge:
		if row.get("reversal_of") or any(c.get("reversal_of") == row.name for c in doc.charges):
			return "This charge is a reversal or has already been voided."
	elif row.get("payment_entry") or row.get("reversal_of"):
		return "ERP-booked receipt. Ask Finance to review the Payment Entry and its allocations. " + JOURNAL
	elif row.payment_kind == "Security Deposit":
		return "Return a security deposit through the stay's deposit record."
	elif row.payment_kind == "Refund" or row.amount <= 0:
		return "A reversal cannot itself be reversed here."
	else:
		entry = frappe.db.get_value(
			"Folio Ledger Entry",
			{
				"folio": doc.name,
				"source_doctype": "Folio Payment",
				"source_name": row.name,
				"credit": [">", 0],
			},
			["name", "session"],
			as_dict=True,
			order_by="creation asc",
		)
		if not entry:
			return "Ask Finance to reconcile this payment with its missing ledger entry."
		if entry.session and frappe.db.get_value("Cashier Session", entry.session, "status") != "Open":
			return JOURNAL
	return ""


def options(doc, name):
	row, charge = _row(doc, name)
	blocked = _block(doc, row, charge)
	residual = (
		0
		if charge
		else round_cents(row.amount + sum(p.amount for p in doc.payments if p.get("refund_of") == name))
	)
	if not charge and not blocked and residual <= 0:
		blocked = "This payment is already reversed in full."
	actions, denied = [], []
	for action in ["void_charge"] if charge else ["reverse_payment", "reclassify_tender", "partial_refund"]:
		why = blocked or (
			"There is no guest credit to return." if action == "partial_refund" and doc.balance >= 0 else ""
		)
		if why:
			denied.append(dict(id=action, reason=why))
		else:
			category = "Void" if charge else "Refund" if action == "partial_refund" else "Payment Reversal"
			codes = list_reason_codes(category)
			actions.append(
				dict(
					id=action,
					label=LABELS[action],
					requires=[] if charge else ["supervisor_user", "supervisor_pin"],
					reason_codes=codes,
				)
			)
	ids = {a["id"] for a in actions}
	return dict(
		actions=actions,
		blocked=denied,
		can_void="void_charge" in ids,
		can_reverse="reverse_payment" in ids,
		can_reclassify="reclassify_tender" in ids,
		requires_supervisor=bool(actions and not charge),
		blocked_reason=blocked,
		residual=residual,
		refund_cap=min(residual, max(0, -doc.balance)),
	)


def correct(folio, name, action, code, note, key, pin, supervisor_user, supervisor_pin, amount, destination):
	from kamra.api import _refund_folio_payment
	from kamra.authz import require_cashier_pin, verify_supervisor
	from kamra.cashier import record_cashier_txn, require_open_session
	from kamra.folio import _recalculate, void_charge
	from kamra.ledger import record_payment_ledger
	from kamra.localization import pack_for, payment_modes

	if action not in LABELS or not key or len(key) > 100 or not code or not (note or "").strip():
		frappe.throw("Choose a correction and reason, enter a note, and provide a retry key.")
	if action == "partial_refund":
		try:
			value = Decimal(str(amount))
			if not value.is_finite() or value <= 0 or value != value.quantize(Decimal(".01")):
				raise InvalidOperation
		except InvalidOperation:
			frappe.throw("Enter a positive refund amount to the cent.")
		amount = float(value)
	elif amount is not None:
		frappe.throw("Only a partial refund takes an amount.")
	if destination and action != "reclassify_tender":
		frappe.throw("Only a tender correction takes a destination mode.")
	doc = frappe.get_doc("Folio", folio, for_update=True)
	require_cashier_pin(doc.property, pin)
	row, charge = _row(doc, name)
	signature = hashlib.sha256(
		json.dumps([name, action, code, note, amount, destination, frappe.session.user]).encode()
	).hexdigest()
	prior = next((p for p in [*doc.payments, *doc.charges] if p.get("idempotency_key") == key), None)
	if prior:
		if prior.get("correction_signature") != signature:
			frappe.throw("This retry key belongs to another request.")
		return dict(ok=True, idempotent_replay=1, reversal=prior.name, balance=doc.balance)
	opts = options(doc, name)
	allowed = next((a for a in opts["actions"] if a["id"] == action), None)
	if not allowed:
		frappe.throw(
			next(
				(b["reason"] for b in opts["blocked"] if b["id"] == action),
				"Correction unavailable on this row.",
			)
		)
	reason = next((c for c in allowed["reason_codes"] if c.name == code), None)
	if not reason:
		frappe.throw("Choose an enabled reason for this action.")
	approved = None
	if allowed["requires"] or reason.requires_supervisor:
		approved = verify_supervisor(
			doc.property, supervisor_user, supervisor_pin, requester=frappe.session.user
		)
	metadata = dict(idempotency_key=key, correction_signature=signature, correction_approved_by=approved)
	why = f"{code}: {note.strip()}"
	if charge:
		return void_charge(folio, name, why, correction=metadata)
	if action == "partial_refund" and amount > opts["refund_cap"]:
		frappe.throw("Amount exceeds the remaining refundable balance.")
	amount = amount or opts["residual"]
	repost = action == "reclassify_tender"
	if repost:
		modes = payment_modes(pack_for(doc.property))
		if (
			destination == row.mode
			or destination not in modes
			or (destination == "Foreign Cash" and not row.fx_amount)
		):
			frappe.throw("Choose a different valid destination tender.")
		if any(p.get("idempotency_key") == key + ":repost" for p in doc.payments):
			frappe.throw("Repost retry key already used.")
	# Partial refunds pay out home money. Bookkeeping reversals negate the original tender.
	is_refund = action == "partial_refund"
	mode = "Cash" if is_refund and row.mode == "Foreign Cash" else row.mode
	fx = {}
	if row.fx_amount and not is_refund:
		fx = dict(
			currency=row.currency,
			exchange_rate=row.exchange_rate,
			exchange_rate_date=row.exchange_rate_date,
			fx_amount=-round_cents(row.fx_amount * amount / row.amount),
		)
	# A pre-arrival advance is still a liability; an in-house advance has moved to Guest.
	advance = (
		row.payment_kind == "Advance"
		and doc.reservation
		and frappe.db.get_value("Reservation", doc.reservation, "status")
		in ("Held", "Pending Payment", "Confirmed")
	)
	result = _refund_folio_payment(
		folio,
		amount,
		mode,
		why,
		pin=pin,
		refund_of=name,
		ledger="Deposit" if advance else "Guest",
		correction={**metadata, **fx},
		bookkeeping_action=None if is_refund else action,
	)
	if repost:
		doc.reload()
		if destination not in ("Foreign Cash", "Bank Transfer"):
			fx = {}
		elif fx:
			fx["fx_amount"] *= -1
		doc.append(
			"payments",
			dict(
				payment_kind=row.payment_kind,
				posting_date=get_business_date(doc.property),
				mode=destination,
				amount=amount,
				reference=why[:140],
				repost_of=name,
				idempotency_key=key + ":repost",
				correction_approved_by=approved,
				**fx,
			),
		)
		_recalculate(doc)
		doc.save()
		sess = require_open_session(doc.property)
		pay = doc.payments[-1]
		record_payment_ledger(doc, pay.as_dict(), session=sess, ledger="Deposit" if advance else "Guest")
		record_cashier_txn(
			doc.property, "Payment", destination, amount, folio=folio, reference=why, session=sess
		)
		result.update(repost=pay.name, balance=doc.balance)
	elif advance:
		paid = frappe.db.get_value("Reservation", doc.reservation, "advance_paid", for_update=True) or 0
		frappe.db.set_value(
			"Reservation", doc.reservation, "advance_paid", max(0, round_cents(paid - amount))
		)
	return result
