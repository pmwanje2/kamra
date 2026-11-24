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


def _original_entry(doc, row):
	return frappe.db.get_value(
		"Folio Ledger Entry",
		{"folio": doc.name, "source_doctype": "Folio Payment", "source_name": row.name, "credit": [">", 0]},
		["name", "session"],
		as_dict=True,
		order_by="creation asc",
	)


def _block(doc, row, charge, entry):
	if (
		doc.status != "Open"
		or doc.invoice_number
		or doc.get("sales_invoice")
		or (
			doc.reservation and frappe.db.get_value("Reservation", doc.reservation, "status") == "Checked Out"
		)
	):
		return FISCAL
	if str(row.posting_date) > str(get_business_date(doc.property)):
		return "Ask Finance to review the future posting date."
	if charge:
		if row.get("reversal_of") or any(c.get("reversal_of") == row.name for c in doc.charges):
			return "This charge is a reversal or has already been voided."
	elif row.get("payment_entry") or row.get("reversal_of"):
		return "ERP-booked receipt. Ask Finance to review the Payment Entry and its allocations. " + JOURNAL
	elif row.payment_kind == "Security Deposit":
		return "Return a security deposit through the stay's deposit record."
	elif row.payment_kind == "Refund" or row.amount <= 0:
		return "A reversal cannot itself be reversed here."
	elif not entry:
		return "Ask Finance to reconcile this payment with its missing ledger entry."
	return ""


def options(doc, name):
	row, charge = _row(doc, name)
	entry = None if charge else _original_entry(doc, row)
	out = _options(doc, row, charge, entry, _open_original_session(doc, entry))
	from kamra.authz import supervisors_for

	out["supervisors"] = (
		supervisors_for(doc.property, exclude=frappe.session.user)
		if any(a["requires"] or any(c.requires_supervisor for c in a["reason_codes"]) for a in out["actions"])
		else []
	)
	return out


def _open_original_session(doc, entry, *, lock=False):
	if (
		entry
		and entry.session
		and frappe.db.get_value(
			"Cashier Session", {"name": entry.session, "property": doc.property}, "status", for_update=lock
		)
		== "Open"
	):
		return entry.session


def _options(doc, row, charge, entry, session):
	name = row.name
	blocked = _block(doc, row, charge, entry)
	prior = str(row.posting_date) < str(get_business_date(doc.property)) or frappe.db.exists(
		"Night Audit Run",
		{
			"property": doc.property,
			"business_date": row.posting_date,
			"status": ["in", ["Completed", "Completed with Warnings"]],
		},
	)
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
		if (
			not why
			and action in ("reverse_payment", "reclassify_tender")
			and (prior or (entry.session and not session))
		):
			why = JOURNAL
		if why:
			denied.append(dict(id=action, reason=why))
		else:
			category = "Void" if charge else "Refund" if action == "partial_refund" else "Payment Reversal"
			codes = list_reason_codes(category)
			actions.append(
				dict(
					id=action,
					label=LABELS[action],
					requires=[] if charge and not prior else ["supervisor_user", "supervisor_pin"],
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
		requires_supervisor=any(a["requires"] for a in actions),
		blocked_reason=blocked or (denied[0]["reason"] if denied and not actions else ""),
		residual=residual,
		refund_cap=min(residual, max(0, -doc.balance)),
	)


def correct(folio, name, action, code, note, key, pin, supervisor_user, supervisor_pin, amount, destination):
	from kamra.api import _refund_folio_payment
	from kamra.authz import require_cashier_pin, verify_supervisor
	from kamra.cashier import record_cashier_txn, require_open_session
	from kamra.folio import _recalculate, void_charge
	from kamra.ledger import home_currency, record_payment_ledger
	from kamra.localization import assert_payment_mode

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
		result = dict(ok=True, idempotent_replay=1, balance=doc.balance)
		result["refund" if action == "partial_refund" else "reversal"] = prior.name
		if action == "reclassify_tender":
			result["repost"] = next(
				p.name for p in doc.payments if p.repost_of == name and p.idempotency_key == key + ":repost"
			)
		return result
	entry = None if charge else _original_entry(doc, row)
	session = _open_original_session(doc, entry, lock=True)
	opts = _options(doc, row, charge, entry, session)
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
	from kamra.masters import default_mode_for_tender, payment_tender_type
	dest_tender = payment_tender_type(destination, doc.property) if repost else None
	row_tender = payment_tender_type(row.mode, doc.property)
	if repost:
		if not destination or destination == row.mode or (not row.fx_amount and dest_tender == "Foreign Cash"):
			frappe.throw("Choose a different valid destination tender.")
		# Only Foreign Cash / Bank Transfer retain the original FX snapshot when reposted.
		currency = (row.currency if row.fx_amount and dest_tender in ("Foreign Cash", "Bank Transfer")
		            else home_currency(doc.property))
		assert_payment_mode(doc.property, destination,
		                    currency, pack_only=True)
		if any(p.get("idempotency_key") == key + ":repost" for p in doc.payments):
			frappe.throw("Repost retry key already used.")
	# Partial refunds pay out home money. Bookkeeping reversals negate the original tender.
	is_refund = action == "partial_refund"
	mode = default_mode_for_tender(doc.property, "Cash") if is_refund and row_tender == "Foreign Cash" else row.mode
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
	# Drawerless bank entries stay drawerless. A cash payout or cash repost
	# needs a live drawer even when the original shift has closed.
	cash_tenders = ("Cash", "Foreign Cash")
	refund_session = session or (require_open_session(doc.property) if payment_tender_type(mode, doc.property) in cash_tenders else None)
	repost_session = session or (require_open_session(doc.property) if dest_tender in cash_tenders else None)
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
		session=refund_session,
	)
	if repost:
		doc.reload()
		if dest_tender not in ("Foreign Cash", "Bank Transfer"):
			fx = {"currency": home_currency(doc.property)}
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
		pay = doc.payments[-1]
		record_payment_ledger(
			doc, pay.as_dict(), session=repost_session, ledger="Deposit" if advance else "Guest"
		)
		if repost_session:
			fx_note = (
				f"{pay.currency} {float(pay.fx_amount):,.2f} @ {pay.exchange_rate}"
				if float(pay.get("fx_amount") or 0) else None
			)
			record_cashier_txn(
				doc.property,
				"Payment",
				destination,
				amount,
				folio=folio,
				reference=why,
				notes=fx_note,
				session=repost_session,
				tender_validated=True,
				currency=pay.get("currency"),
				fx_amount=float(pay.get("fx_amount") or 0),
			)
		result.update(repost=pay.name, balance=doc.balance)
	elif advance:
		paid = frappe.db.get_value("Reservation", doc.reservation, "advance_paid", for_update=True) or 0
		frappe.db.set_value(
			"Reservation", doc.reservation, "advance_paid", max(0, round_cents(paid - amount))
		)
	return result
