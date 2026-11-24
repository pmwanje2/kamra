"""Backfill currency and fx_amount on Open cashier sessions' transactions only.

Closed sessions are historical financial records and MUST stay untouched so
closed drawer counts and variances are never rewritten retroactively.
Idempotent: running twice is a no-op once every open-session transaction carries
a currency.
"""

from __future__ import annotations

import re

import frappe

_FX_NOTE_RE = re.compile(r"\b([A-Z]{3})\s+([0-9][0-9,]*(?:\.[0-9]+)?)\s*@")


def _parse_fx_note(notes: str | None, amount: float) -> tuple[str | None, float]:
	if not notes:
		return None, 0.0
	m = _FX_NOTE_RE.search(notes)
	if not m:
		return None, 0.0
	ccy = m.group(1).upper()
	raw = float(m.group(2).replace(",", ""))
	signed = -raw if float(amount or 0) < 0 else raw
	return ccy, round(signed, 2)


def _infer_currency_and_fx(row, home: str) -> tuple[str, float]:
	amt = float(row.amount or 0)
	if row.folio and row.kind in ("Payment", "Refund"):
		matches = frappe.db.sql(
			"""SELECT currency, fx_amount
			   FROM `tabFolio Payment`
			   WHERE parent = %s AND mode = %s AND ABS(amount - %s) < 0.01
			   ORDER BY (CASE WHEN COALESCE(fx_amount, 0) != 0 THEN 0 ELSE 1 END), creation DESC
			   LIMIT 1""",
			(row.folio, row.mode, amt),
			as_dict=True,
		)
		if matches:
			fp = matches[0]
			ccy = (fp.currency or "").strip().upper() or home
			fx_amt = float(fp.fx_amount or 0)
			if ccy != home and fx_amt:
				return ccy, round(fx_amt, 2)
			if ccy == home:
				return home, 0.0
	note_ccy, note_fx = _parse_fx_note(row.notes, amt)
	if note_ccy and note_ccy != home and note_fx:
		return note_ccy, note_fx
	return home, 0.0


def execute():
	if not frappe.db.table_exists("Cashier Transaction") or not frappe.db.table_exists("Cashier Session"):
		return
	if not frappe.db.has_column("Cashier Transaction", "currency") or not frappe.db.has_column(
		"Cashier Transaction", "fx_amount"
	):
		return

	from kamra.cashier import _recompute_session_totals
	from kamra.ledger import home_currency

	rows = frappe.db.sql(
		"""SELECT t.name, t.session, t.property, t.kind, t.mode, t.amount, t.folio, t.notes
		   FROM `tabCashier Transaction` t
		   JOIN `tabCashier Session` s ON s.name = t.session
		   WHERE s.status = 'Open'
		     AND COALESCE(t.currency, '') = ''
		   ORDER BY t.creation ASC""",
		as_dict=True,
	)
	if not rows:
		return

	home_by_prop: dict[str, str] = {}
	touched_sessions: set[str] = set()
	for r in rows:
		if r.property not in home_by_prop:
			home_by_prop[r.property] = home_currency(r.property)
		ccy, fx_amt = _infer_currency_and_fx(r, home_by_prop[r.property])
		frappe.db.sql(
			"""UPDATE `tabCashier Transaction`
			   SET currency = %s, fx_amount = %s
			   WHERE name = %s""",
			(ccy, fx_amt, r.name),
		)
		touched_sessions.add(r.session)

	for sess in sorted(touched_sessions):
		_recompute_session_totals(sess, persist=True)
