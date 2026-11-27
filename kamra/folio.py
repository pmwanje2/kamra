"""Folio lifecycle: open at check-in, post nightly charges, settle, close.

All amounts flow from the deterministic pricing engine. GST is carried
per line so a folio can mix rates (room 5/18%, F&B 5%) and still produce
a correct multi-rate invoice - the PRD's FR-60.
"""

import json
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe.utils import add_days, cint, getdate, now_datetime, nowdate

from kamra.labels import room_type_label
from kamra.pricing import billing_room_type

# SQL on `tabFolio Charge` fc: a line that is neither voided nor a void's
# reversal. Both stay on the folio (void_charge) and net to zero, so sums
# need nothing; counts of rooms sold and "already posted" checks do.
NOT_VOIDED = """(IFNULL(fc.reversal_of, '') = '' AND NOT EXISTS (
	SELECT 1 FROM `tabFolio Charge` v
	WHERE v.parent = fc.parent AND v.reversal_of = fc.name))"""

# SQL on `tabFolio Charge` fc: a line that stands on its own posting date,
# for the day's statistics and audit checks. A line voided the same day
# drops out with its reversal; one voided on a later business date still
# stands on its own date, and its reversal is an adjustment on the void
# date (OPERA dates the adjustment of a past revenue date on the current
# business date: the closed day is not restated). A reversal line is never
# a room sold. Sums need nothing: each day's lines add up on their own.
STANDS_ON_ITS_DATE = """(IFNULL(fc.reversal_of, '') = '' AND NOT EXISTS (
	SELECT 1 FROM `tabFolio Charge` v
	WHERE v.parent = fc.parent AND v.reversal_of = fc.name
	  AND v.posting_date = fc.posting_date))"""


def _fnb_gst(property):
	from kamra.localization import pack_for
	return pack_for(property).fnb_tax_rate(property)


def _payment_entry_is_security_deposit(key: str | None) -> bool:
	if not key or not frappe.db.exists("DocType", "Payment Entry"):
		return False
	return frappe.db.get_value("Payment Entry", key, "kamra_payment_kind") == "Security Deposit"


def _deposit_read_for_update(folio):
	"""Use current reads for ledger inputs when Frappe loaded the folio for update."""
	return bool((getattr(folio, "flags", None) or {}).get("for_update"))


def _security_deposit_row_ids(folio) -> set[int]:
	"""Indices of payments and refunds with recorded security-deposit provenance."""
	from kamra.ledger import SECURITY_DEPOSIT_REFUND_SOURCE

	payments = folio.get("payments") or []
	sec_idx = {
		idx for idx, p in enumerate(payments)
		if p.get("payment_kind") == "Security Deposit"
		or _payment_entry_is_security_deposit(p.get("payment_entry") or p.get("reversal_of"))
	}
	sec_names = {payments[idx].name for idx in sec_idx if payments[idx].get("name")}
	refunds = [(idx, p) for idx, p in enumerate(payments) if p.get("payment_kind") == "Refund"]
	if len(sec_idx) == len(payments):
		return sec_idx
	pending = (getattr(folio, "flags", None) or {}).get("kamra_pending_security_refund")
	has_pe = refunds and frappe.db.exists("DocType", "Payment Entry")
	for idx, p in refunds:
		if p is pending or (p.get("refund_of") in sec_names and not (
			has_pe and (p.get("payment_entry") or p.get("reversal_of"))
		)):
			sec_idx.add(idx)
	if not folio.get("name"):
		return sec_idx

	sec_names.update(payments[i].name for i in sec_idx if payments[i].get("name"))
	filters = {"folio": folio.name, "ledger": "Deposit"}
	fields = ["name", "source_doctype", "source_name", "reversal_of"]
	if _deposit_read_for_update(folio):
		entries = frappe.db.get_values("Folio Ledger Entry", filters, fields, as_dict=True, for_update=True)
	else:
		entries = frappe.get_all("Folio Ledger Entry", filters=filters, fields=fields)

	# A reversal retains its original liability, irrespective of its sign or depth.
	children = defaultdict(list)
	roots = []
	for entry in entries:
		if entry.reversal_of:
			children[entry.reversal_of].append(entry)
		if entry.source_doctype in ("Security Deposit", SECURITY_DEPOSIT_REFUND_SOURCE) or (
			entry.source_doctype == "Folio Payment" and entry.source_name in sec_names
		):
			roots.append(entry)
	seen = set()
	while roots:
		entry = roots.pop()
		if entry.name in seen:
			continue
		seen.add(entry.name)
		if entry.source_doctype in ("Folio Payment", SECURITY_DEPOSIT_REFUND_SOURCE):
			sec_names.add(entry.source_name)
		roots.extend(children.get(entry.name, ()))
	sec_idx.update(i for i, p in enumerate(payments) if p.get("name") and p.name in sec_names)
	return sec_idx


def reconcile_reservation_advance(reservation: str, property: str, *, changed_folio=None, returned=0) -> None:
	"""Reconcile collected funds and their linked advance refunds without replaying a delta."""
	from kamra.ledger import _currency_places, _money, _round_currency

	paid = _money(frappe.db.get_value("Reservation", reservation, "advance_paid", for_update=True))
	names = set(frappe.get_all("Folio", filters={"reservation": reservation, "folio_type": ["!=", "Group"]}, pluck="name"))
	if changed_folio is not None and changed_folio.get("folio_type") != "Group":
		names.add(changed_folio.name)
	payments = []
	for name in sorted(names):
		doc = changed_folio if changed_folio is not None and name == changed_folio.name else (
			frappe.get_doc("Folio", name, for_update=True)
		)
		security_rows = _security_deposit_row_ids(doc)
		payments.extend(
			p for i, p in enumerate(doc.payments)
			if i not in security_rows and p.get("mode") != "Company Credit"
		)

	advances = [p for p in payments if p.get("payment_kind") == "Advance" and _money(p.amount) > 0]
	advance_rows = {p.name for p in advances if p.get("name")}
	advance_entries = {p.get("payment_entry") for p in advances if p.get("payment_entry")}
	linked_refunds = sum(
		(_money(p.amount) for p in payments if _money(p.amount) < 0 and (
			p.get("refund_of") in advance_rows or (
				not p.get("refund_of") and p.get("reversal_of") in advance_entries
			)
		)),
		Decimal(0),
	)
	collected = sum((_money(p.amount) for p in payments), Decimal(0))
	# Transfers have two Advance rows, one on each window. Refunds either
	# identify the advance or reduce the pool of collected funds.
	advance_funds = sum((_money(p.amount) for p in payments if p.get("payment_kind") == "Advance"
	                     and (_money(p.amount) > 0 or p.get("transferred_to"))), Decimal(0))
	if advances:
		# A refund without a receipt link reduces the advance only when it
		# released Deposit liability. Guest-ledger refunds may be settlements.
		unlinked = [p.name for p in payments if p.get("name") and p.get("payment_kind") == "Refund"
		            and not (p.get("refund_of") or p.get("reversal_of")) and _money(p.amount) < 0]
		deposit_returns = Decimal(0)
		if unlinked:
			debits = frappe.get_all("Folio Ledger Entry", filters={
				"folio": ["in", sorted(names)], "ledger": "Deposit",
				"source_doctype": "Folio Payment", "source_name": ["in", unlinked],
			}, pluck="debit")
			deposit_returns = sum((_money(debit) for debit in debits), Decimal(0))
		remaining = min(collected, advance_funds + linked_refunds - deposit_returns)
	else:
		# Legacy stays can predate typed Advance rows.
		remaining = min(paid - _money(returned), collected)
	# Preserve fractional receipts already accepted by the advance desk.
	places = _currency_places(property)
	net = _round_currency(max(Decimal(0), remaining), max(places, 2) if advances else places)
	if _money(net) != paid:
		frappe.db.set_value("Reservation", reservation, "advance_paid", net)


def _refund_amounts(folio, sec_idx: set[int]) -> tuple[Decimal, Decimal, Decimal]:
	"""Return identified refunds and the mandatory and ambiguous parts of the unlinked cap."""
	from kamra.ledger import _money

	if not sec_idx:
		return Decimal(0), Decimal(0), Decimal(0)
	pending = (getattr(folio, "flags", None) or {}).get("kamra_pending_security_refund")
	identified = Decimal(0)
	candidates = {}
	for idx, payment in enumerate(folio.get("payments") or []):
		if payment is pending or not payment.get("name") or payment.get("payment_kind") != "Refund":
			continue
		amount = max(-_money(payment.get("amount")), Decimal(0))
		if idx in sec_idx:
			identified += amount
		elif not (payment.get("refund_of") or payment.get("payment_entry") or payment.get("reversal_of")):
			candidates[payment.name] = amount
	if not candidates:
		return identified, Decimal(0), Decimal(0)

	# All Deposit entries back the window's security. Only unlinked payment
	# debits are candidates for explaining its historical refunds.
	entries = frappe.db.sql(
		"""SELECT source_doctype, source_name, CONCAT(SUM(COALESCE(credit, 0) - COALESCE(debit, 0)), '')
		   FROM `tabFolio Ledger Entry`
		   WHERE folio = %s AND ledger = 'Deposit'
		   GROUP BY source_doctype, source_name""" + (" FOR UPDATE" if _deposit_read_for_update(folio) else ""),
		(folio.name,),
	)
	balance = unlinked = Decimal(0)
	for kind, name, net in entries:
		amount = _money(net)
		balance += amount
		if kind == "Folio Payment" and name in candidates and amount < 0:
			unlinked += min(candidates[name], -amount)

	security = max(Decimal(0), _security_before_legacy(folio, sec_idx))
	cap = min(unlinked, security)
	# During folio.save(), the pending payout already reduces the document's
	# security but its Deposit debit has yet to post. Compare posted amounts.
	posted_security = security
	if pending is not None:
		posted_security -= _money(pending.get("amount"))
	shortfall = max(Decimal(0), posted_security - max(Decimal(0), balance))
	mandatory = min(cap, shortfall)
	return identified, mandatory, cap - mandatory


@contextmanager
def payment_savepoint():
	"""Keep a refused posting atomic even if a caller handles its exception."""
	db = frappe.db
	if not hasattr(db, "transaction_writes"):  # site-free document adapters have no transaction
		yield
		return
	point = "folio_payment_" + frappe.generate_hash(length=8)
	writes = db.transaction_writes
	queues = [(queue._functions, list(queue._functions)) for queue in (
		db.before_commit, db.after_commit, db.before_rollback, db.after_rollback,
	)]
	queued = getattr(frappe.local, "_realtime_log", None)
	realtime = list(queued) if queued is not None else None
	db.savepoint(point)
	try:
		yield
	except Exception as exc:
		try:
			db.rollback(save_point=point)
		except Exception as rollback_error:
			# MariaDB removes every savepoint when it aborts the transaction.
			# Keep the original conflict visible to the transaction owner.
			if rollback_error.args and rollback_error.args[0] == 1305:
				raise exc from None
			raise
		finally:
			for queue, original in queues:
				queue.clear()
				queue.extend(original)
			if realtime is not None:
				frappe.local._realtime_log = realtime
			elif hasattr(frappe.local, "_realtime_log"):
				del frappe.local._realtime_log
		db.transaction_writes = writes
		db.release_savepoint(point)
		raise
	else:
		db.release_savepoint(point)


def lock_guest_deposit_windows(reservation):
	"""Serialize stay postings before any writer locks its individual folio."""
	if reservation:
		frappe.db.get_value("Reservation", reservation, "name", for_update=True)
		frappe.db.sql(
			"""SELECT name FROM `tabFolio` WHERE reservation = %s AND folio_type = 'Guest'
			   ORDER BY name FOR UPDATE""", reservation,
		)


@contextmanager
def current_locking_read():
	"""Read locked current versions without MariaDB aborting a stale read view."""
	if not _snapshot_isolation_enabled():
		yield
		return
	previous = frappe.db.sql("SELECT @@SESSION.innodb_snapshot_isolation")[0][0]
	try:
		frappe.db.sql("SET SESSION innodb_snapshot_isolation=0")
		yield
	finally:
		frappe.db.sql("SET SESSION innodb_snapshot_isolation=%s", (previous,))


def _check_receipt_snapshot(query, args):
	"""Lock current versions without allowing MariaDB 1020 to abort the caller.

	Only the comparison read uses current versions. A stale snapshot is
	refused before any posting, with the caller's transaction still intact.
	"""
	snapshot = frappe.db.sql(query, args)
	with current_locking_read():
		current = frappe.db.sql(query + " FOR UPDATE", args)
	if snapshot != current:
		raise frappe.TimestampMismatchError(frappe._(
			"The folio changed in another transaction. Retry the payment in a new transaction."))


def _snapshot_isolation_enabled():
	if getattr(frappe.db, "db_type", None) != "mariadb":
		return False
	mode = frappe.db.sql("SHOW SESSION VARIABLES LIKE 'innodb_snapshot_isolation'")
	return bool(mode and mode[0][1] == "ON")


def lock_receipt_till(session):
	"""The session is the serialization row for every append to its journal."""
	_check_receipt_snapshot("SELECT name, modified FROM `tabCashier Session` WHERE name=%s", session)


def lock_folio_parent(name):
	_check_receipt_snapshot(
		"SELECT name, modified, payments_total, balance FROM `tabFolio` WHERE name=%s", name)


def payment_folio_for_update(name, *, lock_standalone=False):
	"""Lock the stay first. Standalone keyed receipts still serialize on their own folio."""
	from kamra.api import _with_snapshot_retry

	def load_locked():
		reservation = frappe.db.get_value("Folio", name, "reservation")
		if not reservation and (not lock_standalone or not hasattr(frappe.db, "transaction_writes")):
			return frappe.get_doc("Folio", name)
		if _snapshot_isolation_enabled():
			if reservation:
				_check_receipt_snapshot("SELECT name, modified FROM `tabReservation` WHERE name=%s", reservation)
				_check_receipt_snapshot(
					"""SELECT name, modified, payments_total, balance FROM `tabFolio`
					   WHERE reservation=%s AND (folio_type='Guest' OR name=%s) ORDER BY name""",
					(reservation, name),
				)
				_check_receipt_snapshot(
					"""SELECT name, modified, refunded_amount, withheld_amount FROM `tabSecurity Deposit`
					   WHERE reservation=%s ORDER BY name""", reservation)
			else:
				lock_folio_parent(name)
		else:
			lock_guest_deposit_windows(reservation)
			lock_folio_parent(name)
		return frappe.get_doc("Folio", name)

	return _with_snapshot_retry(load_locked, receipt_lock=True)


def guest_security_windows(reservation, property_name, *, for_update=False):
	return frappe.db.get_values(
		"Folio", {"reservation": reservation, "property": property_name, "folio_type": "Guest"},
		"name", pluck=True, for_update=for_update, order_by="window_no asc, creation asc, name asc",
	)


def allocate_historical_security_refunds(windows, recorded: Decimal) -> dict[str, Decimal]:
	"""Cover ledger shortfalls first, then allocate the remainder in Guest window order."""
	if recorded <= 0:
		return {}
	remaining = recorded
	allocations = {}
	reserved = Decimal(0)
	mandatory = []
	ambiguous = []
	for window, sec_idx in windows:
		identified, required, optional = _refund_amounts(window, sec_idx)
		remaining -= identified
		if sec_idx and _is_closed_or_invoiced(window):
			# The issued bill records the historical share in its saved paid total.
			# Reserve that share before allocating refunds to still-open windows.
			fixed = Decimal(str(window.payments_total or 0)) - _payments_before_historical_refunds(window, sec_idx)
			if not required <= fixed <= required + optional:
				frappe.throw(frappe._("This posting would change the historical refund on a settled invoice."))
			allocations[window.name] = fixed
			reserved += fixed
			continue
		if required:
			mandatory.append((window.name, required))
		if optional:
			ambiguous.append((window.name, optional))

	remaining -= reserved
	if reserved and remaining < sum(cap for _, cap in mandatory):
		frappe.throw(frappe._("This posting would use funds reserved for a settled invoice."))
	for name, cap in mandatory + ambiguous:
		if remaining <= 0:
			break
		take = min(remaining, cap)
		allocations[name] = allocations.get(name, Decimal(0)) + take
		remaining -= take
	return allocations


def _legacy_security_deposit_refunded(folio, sec_idx: set[int]) -> Decimal:
	"""This Guest window's share of the stay's historical refund total."""
	if not sec_idx or not folio.get("reservation") or (folio.get("folio_type") or "Guest") != "Guest":
		return Decimal(0)
	if not frappe.db.exists("DocType", "Security Deposit"):
		return Decimal(0)
	from kamra.ledger import _money
	for_update = _deposit_read_for_update(folio)
	recorded = _money(frappe.db.get_value(
		"Security Deposit", {"reservation": folio.reservation}, "refunded_amount", for_update=for_update))
	if recorded <= 0:
		return Decimal(0)

	windows = []
	for name in guest_security_windows(folio.reservation, folio.property, for_update=for_update):
		if name == folio.name and not _is_closed_or_invoiced(folio):
			# Include pending payment rows when recalculating the current document.
			windows.append((folio, sec_idx))
		else:
			other = frappe.get_doc("Folio", name, for_update=for_update)
			windows.append((other, _security_deposit_row_ids(other)))
	return allocate_historical_security_refunds(windows, recorded).get(folio.name, Decimal(0))


def _stay_withheld_deposit(folio, has_sec_dep: bool) -> Decimal:
	"""Explicitly withheld stay Security Deposit applied against its posted damage charge."""
	if not has_sec_dep or not folio.get("reservation"):
		return Decimal(0)
	if (folio.get("folio_type") or "Guest") != "Guest":
		return Decimal(0)
	if not frappe.db.exists("DocType", "Security Deposit"):
		return Decimal(0)
	for_update = _deposit_read_for_update(folio)
	dep = frappe.db.get_value(
		"Security Deposit", {"reservation": folio.reservation}, ["name", "withheld_amount"], as_dict=True, for_update=for_update)
	if not dep or not dep.withheld_amount:
		return Decimal(0)
	# A recorded release on another window paid that window's bill. Older
	# aggregate-only releases still reserve the unexplained amount here.
	other = frappe.db.sql(
		"""SELECT COALESCE(SUM(e.debit - e.credit), 0)
		   FROM `tabFolio` f JOIN `tabFolio Ledger Entry` e ON e.folio = f.name
		   WHERE f.reservation = %s AND f.name != %s AND e.ledger = 'Deposit'
		     AND e.source_doctype = 'Security Deposit' AND e.source_name = %s""" + (" FOR UPDATE" if for_update else ""),
		(folio.reservation, folio.name, dep.name),
	)[0][0]
	return max(Decimal(0), Decimal(str(dep.withheld_amount)) - Decimal(str(other)))


def _security_before_legacy(folio, sec_idx: set[int]) -> Decimal:
	raw = sum((Decimal(str(folio.payments[i].get("amount") or 0)) for i in sec_idx), Decimal(0))
	return raw - _stay_withheld_deposit(folio, bool(sec_idx))


def security_deposit_held_on_folio(folio) -> float:
	"""Net held Security Deposit carried on this folio's Deposit ledger (excluded from folio.balance)."""
	sec_idx = _security_deposit_row_ids(folio)
	return float(_security_before_legacy(folio, sec_idx) - _legacy_security_deposit_refunded(folio, sec_idx))


def refundable_security_on_folio(folio, sec_idx: set[int], historical_refund: Decimal) -> Decimal:
	"""Cash that this locked window can return, backed by its Deposit ledger."""
	from kamra.ledger import SECURITY_DEPOSIT_REFUND_SOURCE, _money

	if not sec_idx:
		return Decimal(0)
	rows = {folio.payments[i].name for i in sec_idx}
	total = security = Decimal(0)
	for kind, name, net in frappe.db.sql(
		"""SELECT source_doctype, source_name, CONCAT(COALESCE(credit, 0) - COALESCE(debit, 0), '')
		   FROM `tabFolio Ledger Entry` WHERE folio = %s AND ledger = 'Deposit' FOR UPDATE""",
		(folio.name,),
	):
		total += _money(net)
		if kind in ("Security Deposit", SECURITY_DEPOSIT_REFUND_SOURCE) or (kind == "Folio Payment" and name in rows):
			security += _money(net)
	held = min(security, _security_before_legacy(folio, sec_idx)) - historical_refund
	return max(Decimal(0), min(total, held))


def _is_closed_or_invoiced(folio) -> bool:
	"""True once a folio is closed or linked to an issued invoice."""
	return bool(
		folio.get("status") == "Closed"
		or folio.get("invoice_number")
		or folio.get("sales_invoice")
	)


def _payments_before_historical_refunds(folio, sec_idx) -> Decimal:
	paid = sum(
		(Decimal(str(p.amount or 0)) for idx, p in enumerate(folio.payments) if idx not in sec_idx),
		Decimal(0),
	)
	return paid + _stay_withheld_deposit(folio, bool(sec_idx))


def _payments_total(folio) -> Decimal:
	sec_idx = _security_deposit_row_ids(folio)
	return _payments_before_historical_refunds(folio, sec_idx) + _legacy_security_deposit_refunded(folio, sec_idx)


def sync_deposit_payment_totals(*folios):
	"""Publish the final ledger allocation to saved Guest windows and the caller's documents."""
	from kamra.ledger import _money

	if not frappe.db.exists("DocType", "Security Deposit"):
		return
	active = {f.name: f for f in folios}
	stays = {(f.reservation, f.property) for f in folios
	         if f.get("reservation") and f.get("folio_type") == "Guest"}
	for reservation, prop in sorted(stays):
		recorded = _money(frappe.db.get_value("Security Deposit", {"reservation": reservation}, "refunded_amount", for_update=True))
		if not recorded:
			continue
		windows = []
		for name in guest_security_windows(reservation, prop, for_update=True):
			# The controller locks only the parent. Keep the flag for current ledger inputs.
			window = frappe.get_doc("Folio", name, for_update=True)
			windows.append((window, _security_deposit_row_ids(window)))
		historical = allocate_historical_security_refunds(windows, recorded)
		for window, sec_idx in windows:
			if not sec_idx:
				continue
			paid = _payments_before_historical_refunds(window, sec_idx) + historical.get(window.name, 0)
			balance = _money(window.grand_total) - paid
			if (paid, balance) != (_money(window.payments_total), _money(window.balance)):
				frappe.db.set_value("Folio", window.name, {"payments_total": paid, "balance": balance})
				if window.name in active:
					active[window.name].reload()


def _recalculate(folio, *, payments_only: bool = False):
	paid = _payments_total(folio)
	if _is_closed_or_invoiced(folio) and folio.get("grand_total") is not None:
		# Closed / invoiced folios have frozen charges, tax and grand_total;
		# adding a payment or correction must never re-round them.
		grand = Decimal(str(folio.grand_total or 0))
		folio.payments_total = float(paid)
		folio.balance = float(grand - paid)
		return
	charges = tax = Decimal(0)
	for c in folio.charges:
		amount = Decimal(str(c.amount or 0))
		line_tax = _line_tax(c, amount)
		c.gst_amount = float(line_tax)
		c.total = float(amount + line_tax)
		charges += amount
		tax += line_tax
	raw_grand = charges + tax
	prop = folio.get("property")
	rounded_grand = raw_grand
	if prop and folio.charges and tax and not _is_closed_or_invoiced(folio):
		# Document-level currency minor-unit rounding (SAP TCURX style) applies
		# only to open, uninvoiced folios with calculated tax. With tax == 0
		# (including ERPNext-bridged folios where KMR-TAX-001 keeps lines untaxed
		# and native Sales Invoice owns tax/rounding), charges_total and
		# grand_total stay equal to sum(c.amount) so KAM-SI-TOTAL-MISMATCH holds.
		from kamra.pricing import property_currency, round_rate_amount
		currency = property_currency(prop)
		basis = raw_grand
		if any(c.get("share_snapshot") for c in folio.charges):
			from kamra.shares import independent_rounding_adjustment
			basis += independent_rounding_adjustment(prop, folio.charges)
		rounded_grand = round_rate_amount(basis, 0, "Nearest", currency)
	if payments_only and folio.get("grand_total") is not None:
		# Settlement writers (payment, refund, city ledger, re-close after
		# cancel_invoice) must not re-round an already-computed bill whose
		# charge lines did not change — e.g. a legacy pre-R8 fractional invoice
		# reopened by cancel_invoice and then paid (SAP VF11/VF01 & OPERA
		# Void/Reissue preserve billed totals until a charge actually changes).
		stored_charges = Decimal(str(folio.charges_total or 0))
		stored_grand = Decimal(str(folio.grand_total or 0))
		if abs(stored_charges - charges) < Decimal("0.000001") and (
			abs(stored_grand - raw_grand) < Decimal("0.000001")
			or abs(stored_grand - rounded_grand) < Decimal("0.000001")
		):
			folio.payments_total = float(paid)
			folio.balance = float(stored_grand - paid)
			return
	grand = rounded_grand
	if prop and folio.charges and tax and not _is_closed_or_invoiced(folio):
		tax = grand - charges
	folio.charges_total = float(charges)
	folio.tax_total = float(tax)
	folio.grand_total = float(grand)
	folio.payments_total = float(paid)
	folio.balance = float(grand - paid)


def _line_tax(c, amount: Decimal) -> Decimal:
	"""A line's tax: worked out from its rate, or, for a line split to the
	currency's minor unit (tax_as_posted), the tax it was posted with."""
	if c.get("tax_as_posted"):
		return Decimal(str(c.gst_amount or 0))
	return amount * Decimal(str(c.gst_rate or 0)) / 100


def open_folio(reservation, *, prearrival: bool = False) -> str:
	"""Open (or return) a guest folio. Prearrival shares defer arrival charges."""
	in_arrival_fc = reservation.name in (getattr(frappe.flags, "_kamra_arrival_fc_res", None) or ())
	existing = frappe.db.get_value(
		"Folio", {"reservation": reservation.name, "folio_type": "Guest"},
		["name", "share_prearrival"], as_dict=True,
		for_update=prearrival or bool(reservation.get("share_group_id")),
		order_by="window_no asc, status desc, creation desc",
	)
	if existing and (in_arrival_fc or prearrival):
		return existing.name
	if existing:
		if existing.share_prearrival and reservation.status == "Confirmed":
			return existing.name
		name = existing.name
		folio = None
		if existing.share_prearrival:
			name = open_line_target(reservation, "Room", name)
			folio = _posting_folio(reservation, name)
	else:
		folio = frappe.get_doc({
			"doctype": "Folio", "property": reservation.property, "reservation": reservation.name,
			"guest": reservation.guest, "status": "Open", "opened_on": now_datetime(),
			"share_prearrival": int(prearrival),
		})
	if folio:
		from kamra.ledger import save_folio
		if not prearrival:
			folio.share_prearrival = 0
			_add_opening_voucher(reservation, folio)
		_recalculate(folio)
		save_folio(folio)
	if prearrival or (not existing and reservation.flags.get("carried_forward")):
		return folio.name
	name = folio.name if folio else name
	_post_addons(reservation, name)
	if folio:
		_post_cleaning_fee(reservation, name)
	_post_pet_charge(reservation, name)
	if not in_arrival_fc and not reservation.flags.get("carried_forward"):
		from kamra.fixed_charges import post_arrival_fixed_charges
		post_arrival_fixed_charges(reservation, primary_folio=name)
	return name


def _add_opening_voucher(reservation, folio):
	voucher = voucher_discount(reservation)
	if not voucher:
		return
	voucher_gst = _room_gst(reservation)
	if not float(voucher_gst or 0):
		from kamra.pricing import property_currency, round_rate_amount
		voucher = float(round_rate_amount(
			voucher, 0, "Nearest", property_currency(reservation.property)))
	folio.append("charges", {
		"posting_date": nowdate(), "charge_type": "Discount", "description": "Booking discount (voucher)",
		"qty": 1, "rate": -voucher, "amount": -voucher, "gst_rate": voucher_gst, "auto_posted": 1,
	})


def voucher_discount(reservation) -> float:
	"""The part of the booking's discount the folio posts as its own line:
	the voucher. The desk's reservation discount is already inside each
	locked night's rate (Room Rate is the base less the discount), so
	posting it again would discount the stay twice.

	Read as the voucher the booking was priced with (its quote snapshot's
	voucher_discount), never as what is left of discount_amount after the
	nights: a billed night that kept a discount the stay later dropped
	would hide a real voucher, and one that never got it would invent one.
	A booking priced before the snapshot kept it falls back to that sum."""
	from kamra.ledger import _currency_places, _money, _round_currency
	places = max(_currency_places(reservation.get("property")), 2)
	snap = reservation.get("quote_snapshot")
	if snap:
		try:
			data = json.loads(snap) if isinstance(snap, str) else snap
			if isinstance(data, dict) and "voucher_discount" in data:
				return _round_currency(max(_money(data.get("voucher_discount")), Decimal(0)), places)
		except (TypeError, ValueError):
			pass  # unreadable snapshot: the stored discount less the nights'
	total = _money(reservation.get("discount_amount"))
	in_nights = sum((_money(r.get("discount_amount"))
	                 for r in reservation.get("night_rates") or []), Decimal(0))
	return _round_currency(max(total - in_nights, Decimal(0)), places)


def _post_cleaning_fee(reservation, folio_name: str):
	"""Post one-time cleaning fee from quote snapshot / property (ADR-008)."""
	fallback = float(
		frappe.db.get_value("Property", reservation.property, "cleaning_fee") or 0
	)
	_post_stay_fee(reservation, folio_name, "cleaning_fee", "Cleaning Fee",
	               "Cleaning fee", fallback)


def _post_pet_charge(reservation, folio_name: str):
	"""Post the pet charge once per stay, at what the guest was quoted."""
	pets = int(reservation.get("pets") or 0)
	if pets <= 0:
		return
	from frappe.utils import date_diff

	from kamra.pricing import pet_charge_amount
	prop = frappe.get_cached_doc("Property", reservation.property)
	# a property that has since stopped taking pets must not block check-in;
	# the quoted line in the snapshot still posts
	fallback = float(pet_charge_amount(
		prop, pets,
		date_diff(reservation.check_out_date, reservation.check_in_date),
	)) if prop.get("pets_allowed") else 0.0
	_post_stay_fee(reservation, folio_name, "pet_charge", "Pet Charge",
	               f"Pets × {pets}", fallback)


def _post_stay_fee(reservation, folio_name: str, component: str,
                   charge_type: str, description: str, fallback: float):
	"""Post a one-time stay fee at the amount and tax in the booking's quote
	snapshot, so a later price change never re-bills the guest. Without a
	snapshot line it posts `fallback` untaxed. Idempotent per folio."""
	amount = 0.0
	tax_rate = 0.0
	snap = getattr(reservation, "quote_snapshot", None)
	if snap:
		try:
			data = json.loads(snap) if isinstance(snap, str) else snap
			for li in data.get("line_items") or []:
				if li.get("component") == component:
					amount = float(li.get("amount") or 0)
					tax = float(li.get("tax_amount") or 0)
					tax_rate = (tax / amount * 100) if amount else 0
					break
		except Exception:
			pass  # snapshot unreadable; fall back to the property's current fee
	if amount <= 0:
		amount = fallback
	if amount > 0 and not float(tax_rate or 0):
		from kamra.pricing import property_currency, round_rate_amount
		amount = float(round_rate_amount(amount, 0, "Nearest", property_currency(reservation.property)))
	if amount <= 0:
		return
	if frappe.db.exists(
		"Folio Charge",
		{"parent": folio_name, "charge_type": charge_type, "auto_posted": 1},
	):
		return
	doc = frappe.get_doc("Folio", folio_name)
	if doc.status == "Closed":
		return
	doc.append(
		"charges",
		{
			"posting_date": nowdate(),
			"charge_type": charge_type,
			"description": description,
			"qty": 1,
			"rate": amount,
			"amount": amount,
			"gst_rate": tax_rate,
			"auto_posted": 1,
		},
	)
	_recalculate(doc)
	from kamra.ledger import save_folio
	save_folio(doc)


def _post_addons(reservation, folio_name: str):
	"""Post booking-time add-ons (experiences) that haven't hit the folio
	yet. Idempotent via the row's posted flag."""
	pending = [a for a in reservation.get("addons") or [] if not a.posted]
	if not pending:
		return
	doc = frappe.get_doc("Folio", folio_name)
	if doc.status == "Closed":
		return
	for a in pending:
		doc.append("charges", {
			"posting_date": nowdate(),
			"charge_type": "Misc",
			"reservation": reservation.name,
			"description": a.description or a.experience,
			"qty": a.qty or 1,
			"rate": a.rate,
			"amount": a.amount,
			"gst_rate": a.gst_rate,
			"auto_posted": 1,
		})
		frappe.db.set_value("Stay Addon", a.name, "posted", 1,
		                    update_modified=False)
		a.posted = 1
	_recalculate(doc)
	from kamra.ledger import save_folio
	save_folio(doc)


# A billing window's payee (OPERA: each window has its own payee profile and
# payment method; SAP: one billing document per payer).
WINDOW_FIELDS = ("payee_type", "payee_company", "payee_agent", "payment_method")
DIRECT_BILL = "Company Credit"  # the Folio Payment mode of a direct-bill window


def window_cap(property: str | None) -> int:
	"""How many windows a stay or a posting master may have: the property's
	`folio_windows` setting (OPERA allows up to 8)."""
	from kamra.settings import get_setting
	return max(int(get_setting(property, "folio_windows") or 1), 1)


def _window_key(reservation: str | None = None, group_booking: str | None = None) -> dict:
	"""The folios that share one set of window numbers: a stay's own, or a
	group's posting master."""
	if group_booking and not reservation:
		return {"group_booking": group_booking, "folio_type": "Group"}
	return {"reservation": reservation, "folio_type": ("!=", "Group")}


def next_window(key: dict, property: str, start: int = 1, *, for_update=False) -> int:
	"""The lowest window number no folio of this stay or group holds yet. A
	window whose folio closed keeps its number: its next posting reopens it."""
	cap = window_cap(property)
	taken = {int(w) for w in frappe.db.get_values(
		"Folio", filters={**key, "window_no": (">", 0)}, fieldname="window_no", pluck=True, for_update=for_update)}
	for n in range(start, cap + 1):
		if n not in taken:
			return n
	frappe.throw(frappe._("A bill can have at most {0} windows. Close a window or move its "
	                      "charges to another one.").format(cap))


def _company_credit(company: str | None) -> bool:
	return bool(company) and bool(frappe.db.get_value("Corporate Account", company,
	                                                  "credit_allowed"))


def open_window(reservation: str | None = None, group_booking: str | None = None,
                window_no: int | None = None, payee_type: str | None = None,
                payee_company: str | None = None, payee_agent: str | None = None,
                payment_method: str | None = None, bill_to_name: str | None = None,
                folio_type: str | None = None, *, prearrival: bool = False, for_update=False) -> str:
	"""Open a billing window on a stay or on a group's posting master and
	return its folio. Without `window_no` it takes the next free number (2
	on, for a stay: window 1 is the guest's). The payee defaults to the
	guest; a posting master's to the group's company. A credit company's
	window is direct bill (Company Credit) unless a method is given.
	`folio_type` stays the account kind (Guest, Extra, Company, Group)."""
	if bool(reservation) == bool(group_booking):
		frappe.throw(frappe._("Open a window on a stay or on a group's master bill."))
	if reservation:
		res = frappe.db.get_value("Reservation", reservation,
		                          ["name", "property", "guest"], as_dict=True, for_update=for_update)
		if not res:
			frappe.throw(frappe._("Reservation {0} not found.").format(reservation))
		prop, guest = res.property, res.guest
	else:
		gb = frappe.db.get_value("Group Booking", group_booking,
		                         ["name", "property", "company", "master_reservation", "status"],
		                         as_dict=True, for_update=for_update)
		if not gb:
			frappe.throw(frappe._("Group {0} not found.").format(group_booking))
		if gb.status == "Cancelled":
			frappe.throw(frappe._("Group {0} is cancelled, so it has no master bill.").format(
				group_booking))
		prop = gb.property
		guest = gb.master_reservation and frappe.db.get_value(
			"Reservation", gb.master_reservation, "guest", for_update=for_update)
		if not payee_type:
			payee_type = "Company" if gb.company else "Guest"
			payee_company = payee_company or gb.company
	payee_type = payee_type or "Guest"
	key = _window_key(reservation, group_booking)
	n = int(window_no or 0) or next_window(key, prop, start=1 if group_booking else 2, for_update=for_update)
	if not payment_method and payee_type == "Company" and _company_credit(payee_company):
		payment_method = DIRECT_BILL
	if not folio_type:
		folio_type = "Group" if group_booking else "Guest" if n == 1 \
			else "Company" if payee_type == "Company" else "Extra"
	folio = frappe.get_doc({
		"doctype": "Folio",
		"property": prop,
		"reservation": reservation,
		"guest": guest,
		"folio_type": folio_type,
		"group_booking": group_booking,
		"window_no": n,
		"payee_type": payee_type,
		"payee_company": payee_company,
		"payee_agent": payee_agent,
		"payment_method": payment_method,
		"bill_to_name": (bill_to_name or "")[:140] or None,
		"status": "Open",
		"opened_on": now_datetime(),
		"share_prearrival": int(prearrival),
	})
	if reservation and n > 1 and payee_type == "Guest" and folio.meta.has_field("customer"):
		folio.customer = frappe.db.get_value(
			"Folio", {**key, "window_no": 1}, "customer", order_by="creation desc",
		) or frappe.get_cached_doc("Property", prop).get("walk_in_customer")
	_recalculate(folio)
	folio.insert(ignore_permissions=True)
	return folio.name


def window_folio(reservation: str | None = None, group_booking: str | None = None,
                 window_no: int = 1, *, for_update=False) -> str:
	"""The open folio of one window. When the window's folio was closed
	(invoiced), a new one opens for it with the same payee; a window never
	used opens now (a stay's window 1 through open_folio, with its one-time
	fees)."""
	key = _window_key(reservation, group_booking)
	n = int(window_no or 1)
	open_filters = {**key, "window_no": n, "status": "Open", "invoice_number": ("is", "not set")}
	if frappe.get_meta("Folio").has_field("sales_invoice"):
		open_filters["sales_invoice"] = ("is", "not set")
	current = frappe.db.get_value("Folio", open_filters, "name", for_update=for_update, order_by="creation desc")
	if current:
		return current
	last = frappe.db.get_value("Folio", {**key, "window_no": n},
	                           ["folio_type", "bill_to_name", "share_prearrival", *WINDOW_FIELDS],
	                           as_dict=True, for_update=for_update, order_by="creation desc")
	if last:
		return open_window(reservation, group_booking, window_no=n, for_update=for_update,
		                   folio_type=last.folio_type, bill_to_name=last.bill_to_name,
		                   prearrival=bool(last.share_prearrival), **{f: last.get(f) for f in WINDOW_FIELDS})
	if reservation and n == 1:
		legacy_fields = ["status", "invoice_number", "bill_to_name", "share_prearrival", *WINDOW_FIELDS]
		if "sales_invoice" in open_filters:
			legacy_fields.append("sales_invoice")
		legacy = frappe.db.get_value("Folio", {
			"reservation": reservation, "folio_type": "Guest", "window_no": ("is", "not set"),
		}, legacy_fields, as_dict=True, for_update=for_update)
		if legacy and _is_closed_or_invoiced(legacy):
			return open_window(reservation, window_no=n, for_update=for_update, folio_type="Guest",
			                   bill_to_name=legacy.bill_to_name,
			                   prearrival=bool(legacy.share_prearrival), **{f: legacy.get(f) for f in WINDOW_FIELDS})
		return open_folio(frappe.get_doc("Reservation", reservation, for_update=for_update))
	return open_window(reservation, group_booking, window_no=n, for_update=for_update)


def open_group_folio(group_booking: str, *, for_update=False) -> str:
	"""Open (or return) the group's posting master (OPERA PM): the group's
	own bill, window 1, with no stay behind it. It opens with the Group
	Booking, before any room is picked up, and takes deposits, banquet
	postings and what is routed to the group. A member's check-out never
	settles it; the group does (settle_group_bill)."""
	return window_folio(group_booking=group_booking, window_no=1, for_update=for_update)


def open_company_folio(reservation) -> str:
	"""Open (or return) the window of a corporate stay that bills the stay's
	company."""
	lock = bool(reservation.get("share_group_id"))
	company = reservation.get("company")
	if company:
		key = _window_key(reservation.name)
		existing = frappe.db.get_value(
			"Folio", {**key, "payee_type": "Company", "payee_company": company,
			          "window_no": (">", 0)},
			["name", "window_no", "status"], as_dict=True, for_update=lock, order_by="window_no asc")
		if existing:
			return existing.name if existing.status == "Open" else \
				window_folio(reservation=reservation.name, window_no=existing.window_no, for_update=lock)
	existing = frappe.db.get_value(
		"Folio",
		{"reservation": reservation.name, "folio_type": "Company",
		 "status": "Open", "payee_type": ("is", "not set")}, for_update=lock,
	)
	if existing:
		return existing
	return split_folio(reservation.name, "Company", for_update=lock)


# What a credit company pays when it has NO billing rules of its own - the
# property's `default_company_billing` setting. "Room" means the stay as sold:
# the room night, its meal plan, the levies on it, early/late fees, the
# cleaning fee, the extra beds agreed on it and any discount against it.
# Liquor is Food & Beverage like the food: a preset has no liquor rule (OPERA
# routes only what is included). A company that does not pay for liquor names
# the codes it does pay in its own billing rules.
_ROOM_CHARGES = frozenset({
	"Room", "Meal Plan", "Room Levy", "Early Check-in", "Late Checkout", "Early Departure",
	"Cleaning Fee", "Extra Guests", "Discount", "Rate Adjustment",
})
COMPANY_BILLING_DEFAULTS = {
	"Room and F&B": _ROOM_CHARGES | {"Food & Beverage"},
	"Room only": _ROOM_CHARGES,
	"Everything": None,  # every charge type
	"Nothing (guest pays unless rules say so)": frozenset(),
}
DEFAULT_COMPANY_BILLING = "Room and F&B"


def default_company_billing(property: str | None) -> str:
	from kamra.settings import get_setting
	value = get_setting(property, "default_company_billing")
	return value if value in COMPANY_BILLING_DEFAULTS else DEFAULT_COMPANY_BILLING


def _company_pays(company: str, charge_type: str,
                  property: str | None = None, code: str | None = None, *, for_update=False) -> bool:
	"""Does the company pay this charge? Its own billing rules decide when it
	has any (OPERA profile routing): a rule names a charge type, every code
	of it, or one transaction code; the company pays what a rule includes
	and nothing else. A credit company with none falls back to the
	property's default; a company without credit pays nothing by default."""
	rules = frappe.db.get_values(
		"Company Billing Rule",
		filters={"parent": company},
		fieldname=["charge_type", "transaction_code", "pay_by"], as_dict=True, for_update=for_update)
	if rules:
		# a rule that names a transaction code covers that code alone
		return any(r.pay_by == "Company" and (
			r.transaction_code == code if r.transaction_code
			else r.charge_type == charge_type) for r in rules)
	if not frappe.db.get_value("Corporate Account", company, "credit_allowed", for_update=for_update):
		return False
	covered = COMPANY_BILLING_DEFAULTS[default_company_billing(property)]
	return covered is None or charge_type in covered


def billing_rule_rows(company: str) -> list[dict]:
	"""A company's billing rules as the check-in and check-out screens show
	them: a charge-type rule by its type, a transaction-code rule by its code
	and the code's description (code_label)."""
	rows = frappe.get_all("Company Billing Rule", filters={"parent": company},
	                      fields=["charge_type", "transaction_code", "pay_by"],
	                      order_by="idx asc")
	out = []
	for r in rows:
		code = r.transaction_code or None
		label = None
		if code:
			label = " ".join(filter(None, (code, frappe.db.get_value(
				"Transaction Code", code, "description"))))
		out.append({"charge_type": None if code else r.charge_type,
		            "transaction_code": code, "code_label": label, "pay_by": r.pay_by})
	return out


def target_folio(reservation, charge_type: str, *, amount=None, posting_date=None,
                 transaction_code: str | None = None) -> str:
	"""Route a whole charge to the right folio of the stay: the stay's
	Routing Instruction for it first (kamra.routing), else the legacy
	presets (_legacy_target). A line routed whole is never split; with
	`amount` None an instruction with a limit is skipped."""
	from kamra.routing import route
	parts = route(reservation, charge_type, amount, posting_date,
	              transaction_code=transaction_code, whole=True)
	if parts:
		return parts[0]["folio"]
	return _legacy_target(reservation, charge_type, transaction_code)


def routed_target(reservation, charge_type: str, *, amount=None, posting_date=None,
                  transaction_code: str | None = None, pending=()) -> tuple[str, str | None]:
	"""(open folio, routing instruction) for a charge that moves whole, such
	as an outlet check or a bill moved to another room."""
	from kamra.routing import route
	parts = route(reservation, charge_type, amount, posting_date,
	              transaction_code=transaction_code, whole=True, pending=pending)
	if parts:
		return parts[0]["folio"], parts[0]["routing_instruction"]
	return open_line_target(reservation, charge_type, name=_legacy_target(
		reservation, charge_type, transaction_code)), None


def line_parts(reservation, charge_type: str, amount, posting_date=None, *,
               transaction_code: str | None = None, gst_rate=0, tax=None,
               pending=()) -> list[dict]:
	"""Where a hotel line goes: [{folio, amount, routing_instruction}], never
	an invoiced folio. A routing limit splits it into the routed part and
	the remainder on window 1, each with its tax to the currency's minor unit
	(gst_amount, tax_as_posted); `pending` are routed lines not saved yet."""
	from kamra.routing import route
	parts = route(reservation, charge_type, amount, posting_date,
	              transaction_code=transaction_code, gst_rate=gst_rate, tax=tax, pending=pending)
	if parts:
		return parts
	# no instruction covers it: the legacy presets, on an open folio
	name = open_line_target(reservation, charge_type, name=_legacy_target(
		reservation, charge_type, transaction_code))
	return [{"folio": name, "amount": amount, "routing_instruction": None}]


def _legacy_target(reservation, charge_type: str, transaction_code: str | None = None) -> str:
	"""The presets, used only when no Routing Instruction covers a charge.

	Group stays route company-payable charges to the group's posting master
	(company pays the stay for every room, each guest pays their own
	extras). Individual corporate stays route to the stay's company window.
	The company's billing rules pick the charge types and transaction codes;
	a credit company with no rules uses the property's default
	(`default_company_billing`). Nothing else decides: no hidden default
	keeps a charge off a company that includes its code.

	A group with a main room (`master_reservation`) and no paying company
	routes what `master_pays` covers to the same posting master. A room that
	opted out (`pays_own_charges`) keeps its own charges, on a company group
	too: the group's company does not pick them up through the stay either.
	A cancelled group has no posting master.
	"""
	from kamra.routing import line_code
	code = line_code(charge_type, transaction_code)
	lock = bool(reservation.get("share_group_id"))
	group = reservation.get("group_booking")
	g = frappe.db.get_value(
		"Group Booking", group,
		["company", "master_reservation", "master_pays", "status"], as_dict=True, for_update=lock) if group else None
	if g and g.status == "Cancelled":
		g = None
	opted_out = bool(g) and bool(reservation.get("pays_own_charges"))
	if g and not opted_out:
		if g.company and _company_pays(
				g.company, charge_type, reservation.get("property"), code, for_update=lock):
			return open_group_folio(group, for_update=lock)
		if g.master_reservation:
			covered = COMPANY_BILLING_DEFAULTS.get(g.master_pays, frozenset())
			if covered is None or charge_type in covered:
				return open_group_folio(group, for_update=lock)
	company = reservation.get("company")
	if company and not (opted_out and company == g.company) and _company_pays(
			company, charge_type, reservation.get("property"), code, for_update=lock):
		return open_company_folio(reservation)
	return open_folio(reservation)


def _posted_charge_folio(reservation_name: str, charge_type: str, date: str, *, lock: bool = False) -> str | None:
	"""Find the folio holding the first automated posting of this type for the night.
	Routing can land a member's room night on the GROUP
	master (anchored to the lead reservation), so we match on the line's
	own provenance first and fall back to the folio's reservation for
	lines predating the provenance field. Only automated lines count: a
	charge the desk posted by hand (an extra bed asked for at check-in) is
	not the night's own posting and must not suppress it.

	"Posted" is the standing rule the statistics use (STANDS_ON_ITS_DATE),
	not NOT_VOIDED: a night voided on a later business date still stands on
	its closed date, so check-out and the audit never post a second line onto
	it (OPERA posts room and tax only on the open business date and corrects
	a past one by an adjustment on the current date). A night voided on its
	own, still-open date has dropped out, so it can be posted again, at the
	rate code in effect when it is."""
	rows = frappe.db.sql(
		"""
		SELECT fc.parent FROM `tabFolio Charge` fc
		JOIN `tabFolio` f ON fc.parent = f.name
		WHERE fc.charge_type = %(ct)s AND fc.posting_date = %(date)s
		  AND fc.auto_posted = 1 AND """ + STANDS_ON_ITS_DATE + """
		  AND (fc.reservation = %(res)s
		       OR (IFNULL(fc.reservation, '') = '' AND f.reservation = %(res)s))
		ORDER BY fc.creation, fc.name LIMIT 1
		""" + (" FOR UPDATE" if lock else ""),
		{"res": reservation_name, "ct": charge_type, "date": str(date)},
	)
	return rows[0][0] if rows else None


def _charge_posted(reservation_name: str, charge_type: str, date: str, *, lock: bool = False) -> bool:
	return bool(_posted_charge_folio(reservation_name, charge_type, date, lock=lock))


def _posted_room_gst(reservation_name: str, date) -> float | None:
	"""The GST rate the night's own room line posted at (a Current Date
	night's own slab), voided lines and their reversals left out; None
	when no automated room line of the stay holds the night."""
	rows = frappe.get_all("Folio Charge", filters={
		"parenttype": "Folio", "reservation": reservation_name, "charge_type": "Room", "auto_posted": 1},
		fields=["name", "posting_date", "gst_rate", "reversal_of"])
	voided = {row.reversal_of for row in rows if row.get("reversal_of")}
	key = str(getdate(date))
	return next((float(row.gst_rate or 0) for row in rows if not row.get("reversal_of") and row.name not in voided
	             and row.get("posting_date") and str(getdate(row.posting_date)) == key), None)


def _room_gst(reservation) -> float:
	"""Effective GST for this reservation's first night (used for the
	discount line). Slab-aware."""
	from kamra.pricing import room_gst_rate

	locked = locked_night(reservation, reservation.check_in_date)
	if locked is not None:
		return float(locked.gst_rate or 0)

	rt = frappe.get_doc("Room Type", billing_room_type(reservation, reservation.check_in_date))
	rate = Decimal(str(_nightly_room_rate(reservation, reservation.check_in_date)))
	return float(room_gst_rate(reservation.property, rt, rate))


def locked_night(reservation, date):
	"""The rate confirmed for this night at booking (Reservation Night
	Rate), or None for a stay booked before rates were locked."""
	key = str(getdate(date))
	for row in reservation.get("night_rates") or []:
		if str(row.stay_date) == key:
			return row
	return None


def _nightly_room_rate(reservation, date) -> float:
	"""Taxable nightly rate as the guest was quoted it.

	A booking carries its confirmed nightly rates (`night_rates`, locked
	from the quote that priced it - demand premium included), and the folio
	bills exactly those: a later season, rate change or a drop in occupancy
	never re-prices a confirmed stay. Stays booked before rates were locked
	fall back to pricing the night now: seasons applied, tax backed out when
	the property configures tax-inclusive pricing, and the booking's rate
	plan applied the way pricing.quote applies it."""
	locked = locked_night(reservation, date)
	if locked is not None:
		return float(locked.rate or 0)
	plan = reservation.get("rate_plan")
	if plan:
		planned = _planned_nights(reservation, plan).get(str(getdate(date)))
		if planned is not None:
			return float(planned)
	return float(_plan_free_night_rate(reservation, date))


def _day_use_room_charge(reservation, date) -> tuple[float, float, str] | None:
	"""Taxable amount, GST rate and label for an hourly/block day-use stay.

	Uses elapsed hours from planned check-in to now (checkout), rounded by
	the property's day-use overstay rule. Returns None for overnight stays
	and for Nightly day-use plans (those keep the locked one-night rate)."""
	from kamra.day_use import (
		billable_units,
		overstay_rule,
		plan_billing,
		reservation_slot_hours,
	)
	from kamra.pricing import rates_include_tax, room_gst_rate

	if not getattr(reservation, "is_day_use", 0):
		return None
	plan = plan_billing(reservation.get("rate_plan"))
	if not plan:
		return None
	elapsed, planned = reservation_slot_hours(reservation)
	units = billable_units(
		elapsed, plan,
		overstay=overstay_rule(reservation.property),
		planned_hours=planned,
	)
	rt = frappe.get_doc("Room Type", billing_room_type(reservation, date))
	inclusive = rates_include_tax(reservation.property)
	if units["full_day"]:
		# Overstay past planned ETD under "Full day": one card night.
		amount, gst = _plan_free_night(reservation, date)
		return _day_use_discount(reservation, date, rt, inclusive, float(amount),
		                         float(gst), "day use · full day")
	gross = Decimal(str(units["amount"]))
	gst = float(room_gst_rate(reservation.property, rt,
	                          gross + _over_cap_night(reservation)))
	if inclusive:
		amount = float(gross / (Decimal(1) + Decimal(str(gst)) / Decimal(100)))
	else:
		amount = float(gross)
	if plan["billing_basis"] == "Hourly":
		label = f"day use · {units['hours']:g} h"
	else:
		label = (f"day use · {units['units']:g} × "
		         f"{plan['block_hours']:g}h")
	return _day_use_discount(reservation, date, rt, inclusive, amount, gst, label)


def _day_use_discount(reservation, date, rt, inclusive: bool, amount: float, gst: float,
                      label: str) -> tuple[float, float, str]:
	"""The desk's discount on an hourly / block day-use charge: the hours are
	only known at check-out, so the discount the booking locked (its night
	row's own, else the stay's) comes off the booked base charge - up to the
	hours actually used; hours past the booking bill at the plan's rate.
	Without a discount the charge is as worked out."""
	from kamra.pricing import (
		_dec,
		apply_currency_discount,
		contract_night_tariff,
		entry_rate,
		night_rate_places,
		property_currency,
		room_gst_rate,
		saved_contract_tariff,
	)
	locked = locked_night(reservation, date)
	if locked is None:
		return amount, gst, label
	home = property_currency(reservation.property)
	kind = locked.get("discount_type")
	if kind:
		value = float(locked.get("discount_value") or 0)
		disc_cur = (locked.get("discount_currency") or "").strip().upper() or home
	else:
		kind = reservation.get("discount_type")
		value = float(reservation.get("discount_value") or 0)
		disc_cur = (reservation.get("discount_currency") or "").strip().upper() or home
	if not kind or value <= 0:
		return amount, gst, label
	places = night_rate_places(reservation.property)
	unit = Decimal(1).scaleb(-places)
	used = entry_rate(amount, gst, inclusive, places)
	booked = entry_rate(locked.rate, locked.gst_rate, inclusive, places) + _dec(locked.get("discount_amount"))
	base = min(used, booked)
	tariff = saved_contract_tariff(reservation).get("tariff") or None
	tariff_night = contract_night_tariff(tariff, str(date), allow_stepped=True)
	if kind == "Amount" and disc_cur != home and tariff_night and tariff_night[0] == disc_cur:
		calc = apply_currency_discount(
			gross_home=base,
			kind=kind,
			value=_dec(value),
			home_currency=home,
			discount_currency=disc_cur,
			tariff_night=tariff_night,
			date=str(date),
			property=reservation.property,
		)
		off = min(calc["discount_home"], base).quantize(unit, rounding=ROUND_HALF_UP)
	else:
		off = (base * _dec(value) / Decimal(100) if kind == "Percent" else min(_dec(value), base)).quantize(
			unit, rounding=ROUND_HALF_UP)
	if off <= 0:
		return amount, gst, label
	net = used - off
	new_gst = room_gst_rate(reservation.property, rt, net + _over_cap_night(reservation))
	charge = net / (Decimal(1) + new_gst / Decimal(100)) if inclusive else net
	from kamra.localization import amount_text
	return (float(charge.quantize(unit, rounding=ROUND_HALF_UP)), float(new_gst),
	        f"{label} · {amount_text(reservation.property, used)} less discount {amount_text(reservation.property, off)}")


def _planned_nights(reservation, plan) -> dict[str, Decimal]:
	"""The stay's nights under its rate plan, priced the way pricing.quote
	prices them (pricing.plan_stay_nights): the plan on the whole stay, a
	derived plan leaving extra guests as its base priced them, then each
	night rounded to the plan's step. Worked out once per loaded booking."""
	from kamra.pricing import (
		extra_person_charge,
		nightly_billing_room_types,
		plan_stay_nights,
		rates_include_tax,
		small_group_allows_stay,
	)

	room_types = nightly_billing_room_types(reservation)
	default_type = billing_room_type(reservation)
	key = (plan, str(reservation.check_in_date), str(reservation.check_out_date),
	       reservation.adults, reservation.children, default_type, room_types,
	       reservation.get("over_capacity_charge"))
	flags = getattr(reservation, "flags", None)
	cache = flags.get("_kamra_planned_nights") if flags is not None else None
	if cache and cache[0] == key:
		return cache[1]
	start = getdate(reservation.check_in_date)
	count = max((getdate(reservation.check_out_date) - start).days, 1)
	inclusive = rates_include_tax(reservation.property)
	dates, nights, extras = [], [], Decimal(0)
	for i in range(count):
		day = add_days(start, i)
		rt = frappe.get_cached_doc("Room Type", room_types.get(str(day), default_type))
		small_ok = small_group_allows_stay(rt, reservation.check_in_date, reservation.check_out_date)
		rate, gst = _plan_free_night(reservation, day)
		extra = extra_person_charge(rt, reservation.adults, reservation.children, day, allow_small_group=small_ok)
		if inclusive:
			extra = extra / (Decimal(1) + gst / Decimal(100))
		extras += min(extra, rate)
		dates.append(str(getdate(day)))
		nights.append((rate, gst))
	from kamra.pricing import property_currency
	rates, _rounded = plan_stay_nights(plan, nights, extras, inclusive,
	                                   currency=property_currency(reservation.property))
	planned = dict(zip(dates, rates, strict=True))
	if flags is not None:
		flags["_kamra_planned_nights"] = (key, planned)
	return planned


def _plan_free_night_rate(reservation, date) -> Decimal:
	"""One night before the rate plan - the per-night step of pricing.quote."""
	return _plan_free_night(reservation, date)[0]


def _plan_free_night(reservation, date) -> tuple[Decimal, Decimal]:
	"""One night before the rate plan, taxable, and its GST %."""
	from kamra.pricing import (
		occupancy_rate,
		rates_include_tax,
		room_gst_rate,
		season_adjust,
		small_group_allows_stay,
	)

	rt = frappe.get_doc("Room Type", billing_room_type(reservation, date))
	base = occupancy_rate(
		rt, reservation.adults, reservation.children, date,
		allow_small_group=small_group_allows_stay(
			rt, reservation.check_in_date, reservation.check_out_date))
	rate = season_adjust(reservation.property, date, base,
	                     rt.name)
	gst = Decimal(str(room_gst_rate(reservation.property, rt,
	                                rate + _over_cap_night(reservation))))
	if rates_include_tax(reservation.property):
		rate = rate / (Decimal(1) + gst / Decimal(100))
	return rate, gst


def _over_cap_night(reservation) -> Decimal:
	"""The night's agreed extra-bed amount. It bills as its own line, but the
	slab reads the whole night's tariff, so it has to be in hand wherever the
	room's tax rate is worked out - matching pricing.quote."""
	return Decimal(str(reservation.get("over_capacity_charge") or 0))


def _nightly_gst(reservation, date) -> float:
	locked = locked_night(reservation, date)
	if locked is not None:
		return float(locked.gst_rate or 0)
	from kamra.pricing import (
		occupancy_rate,
		room_gst_rate,
		season_adjust,
		small_group_allows_stay,
	)

	rt = frappe.get_doc("Room Type", billing_room_type(reservation, date))
	base = occupancy_rate(
		rt, reservation.adults, reservation.children, date,
		allow_small_group=small_group_allows_stay(
			rt, reservation.check_in_date, reservation.check_out_date))
	gross = season_adjust(reservation.property, date, base,
	                      rt.name)
	return float(room_gst_rate(reservation.property, rt,
	                           gross + _over_cap_night(reservation)))


def open_line_target(reservation, charge_type: str, name: str | None = None) -> str:
	"""target_folio (or `name`), never an invoiced folio: when the folio the
	line would go to is closed, it goes on the open folio of the same window,
	and one opens when there is none, with the same payee (the part_settle
	shape, without repeating the one-time fees the first folio carried)."""
	name = name or target_folio(reservation, charge_type)
	fields = ["status", "folio_type", "group_booking", "window_no", "invoice_number"]
	if frappe.get_meta("Folio").has_field("sales_invoice"):
		fields.append("sales_invoice")
	lock = bool(reservation.get("share_group_id"))
	row = frappe.db.get_value("Folio", name, fields, as_dict=True, for_update=lock)
	if not row or not _is_closed_or_invoiced(row):
		return name
	if row.folio_type == "Group" and row.group_booking:
		return window_folio(group_booking=row.group_booking, window_no=row.window_no or 1, for_update=lock)
	return window_folio(reservation=reservation.name, window_no=row.window_no or 1, for_update=lock)


def routed_figure(property: str, charge_type: str, amount, tax) -> Decimal:
	"""What a routing limit counts of a line: its price, tax included, for a
	line priced with tax (OPERA: an inclusive code routes its value with its
	taxes), else its base (an exclusive code's taxes are not routed)."""
	amount = Decimal(str(amount or 0))
	return amount + Decimal(str(tax or 0)) if priced_with_tax(property, charge_type) else amount


def _pending_routed(folios) -> list[tuple]:
	"""Routed lines appended to the loaded folios and not saved yet, so a
	multi-night posting run spends a routing limit only once:
	[(instruction, posting_date, figure)]."""
	return [(c.routing_instruction, c.posting_date,
	         routed_figure(doc.property, c.charge_type, c.amount,
	                       _line_tax(c, Decimal(str(c.amount or 0)))))
	        for doc in folios.values() for c in doc.charges
	        if c.get("routing_instruction") and not c.get("creation")]


# Only target folios selected by the locked shared advance bill may be read
# beyond the caller's snapshot, including Frappe's second load before save.
advance_share_targets = ContextVar("advance_share_targets", default=None)


def _posting_folio(reservation, name):
	if not reservation.get("share_group_id"):
		return frappe.get_doc("Folio", name)
	targets = advance_share_targets.get()
	if targets is not None:
		targets.add(name)
	bill = frappe.get_doc("Folio", name, for_update=True)
	# Frappe validates links with cached, non-locking reads during save. A
	# just-committed peer (or their guest profile) can be newer than that
	# snapshot. Populate the exact validation lookups from current rows.
	links = {("Reservation", reservation.name, ("name",)), ("Folio", name, ("name",)),
	         ("Reservation", bill.reservation, ("name",)), ("Guest", bill.guest, ("name", "full_name"))}
	for doctype, target, fields in sorted(link for link in links if link[1]):
		frappe.db.value_cache[doctype][target].pop(fields, None)
		frappe.db.get_value(doctype, target, fields, as_dict=True, cache=True, for_update=True)
	return bill


def _append_charge(folios, reservation, charge_type, line):
	"""Append a line to the folio(s) routing sends it to, reusing loaded docs
	so a multi-night posting run saves each folio once. A routing limit
	splits it: the routed part and the remainder are each a line, with their
	tax to the minor unit."""
	parts = line_parts(reservation, charge_type, line["amount"], line["posting_date"],
	                   transaction_code=line.get("transaction_code"),
	                   gst_rate=line.get("gst_rate"),
	                   tax=line.get("gst_amount") if line.get("tax_as_posted") else None,
	                   pending=_pending_routed(folios))
	for part in parts:
		name = part["folio"]
		if name not in folios:
			folios[name] = _posting_folio(reservation, name)
		row = {**line, "routing_instruction": part["routing_instruction"]}
		if len(parts) > 1:
			row.update(qty=1, rate=part["amount"], amount=part["amount"],
			           gst_amount=part["gst_amount"], tax_as_posted=1)
		folios[name].append("charges", row)


STALE_FX_MARK = "[stale booking FX]"


def _negotiated_share(reservation, snapshot: dict, locked, night: dict | None = None) -> Decimal:
	"""What the stay's night is of its booked contract price - the night's
	own (`night`, a stay over a tariff change), else the stay's: below 1
	for a reservation discount, anything else for a manager's override. 1
	when the night is the contract as booked."""
	from kamra.pricing import rates_include_tax

	booked = Decimal(str((night or snapshot).get("agreed_rate") or 0))
	if booked <= 0:
		return Decimal(1)
	if locked is not None:
		shown = Decimal(str(locked.rate or 0))
		if rates_include_tax(reservation.property):
			shown *= Decimal(1) + Decimal(str(locked.gst_rate or 0)) / Decimal(100)
	else:
		stay = Decimal(str(snapshot.get("agreed_rate") or 0)) or booked
		shown = booked * (Decimal(str(reservation.get("agreed_rate") or 0)) or stay) / stay
	# the locked nights carry cents and the last night's rounding: that is not a discount
	tolerance = max(Decimal("0.05"), booked * Decimal("0.00001"))
	return Decimal(1) if abs(shown - booked) <= tolerance else shown / booked


def current_date_contract_night(reservation, date) -> dict | None:
	"""OPERA Exchange Type "Current Date": a foreign contract night is
	charged at the published rate of the date it is posted for (a catch-up
	or back-dated night at its own date, never later than the business
	date), not the booking's locked rate. A stay over a tariff change
	converts the night's own contract amount.

	Only the rate is looked up (pricing.posting_fx_rate, the governed
	Currency Exchange): the contract amount stays as booked, the rate
	plan's rounding applies, a reservation discount or override keeps its
	share of the night, and a tax-inclusive house backs the tax out as the
	quote does. With no published rate the locked night (the booking's
	rate) is billed, marked STALE_FX_MARK on the charge and logged for
	Night Audit and Finance. {rate, gst_rate, label, currency, fx_rate,
	fx_date, stale}, or None - bill the locked night - for other policies,
	a home-currency contract or a fixed rate (the manager's price, never
	re-converted, as pricing.relock_contract_fx_at_check_in keeps it). An
	OTA booking in the contract currency posts the OTA's own amount for that
	night (its snapshot's `nights`); the policy is the stay's
	(pricing.reservation_fx_policy)."""
	from kamra.pricing import (
		apply_currency_discount,
		contract_local_amount,
		posting_fx_rate,
		rates_include_tax,
		contract_fx_context,
		room_gst_rate,
		round_rate_amount,
	)

	plan = reservation.get("rate_plan")
	context = contract_fx_context(reservation, "Current Date", date)
	if context is None:
		return None
	snapshot, night, terms, currency, home, nightly = context
	contract = round_rate_amount(nightly, 0, currency=currency)
	posting = posting_fx_rate(reservation.property, currency, date)
	if not posting:
		fx, fx_date = Decimal(str(terms.get("fx_rate") or 0)), terms.get("fx_date")
		frappe.log_error(
			title=f"Current Date FX missing: {reservation.name}",
			message=(f"No single published {currency} rate for {date}: room night of {reservation.name} "
			         f"posted at the booking rate {fx} ({fx_date}). Finance publishes the rate; Night Audit "
			         "reviews the night."))
		return {"rate": _nightly_room_rate(reservation, date), "gst_rate": _nightly_gst(reservation, date),
		        "label": f"{currency} {contract:,} at {fx:,} ({fx_date}) {STALE_FX_MARK}",
		        "currency": currency, "fx_rate": float(fx), "fx_date": fx_date, "stale": True}
	fx, fx_date = posting
	rp = frappe.get_doc("Rate Plan", plan)
	base_gross = contract_local_amount(rp, nightly, fx, home)
	locked = locked_night(reservation, date)
	if locked is not None and locked.get("discount_type"):
		disc_kind = locked.get("discount_type")
		disc_val = Decimal(str(locked.get("discount_value") or 0))
		disc_cur = (locked.get("discount_currency") or "").strip().upper() or home
	else:
		disc_kind = reservation.get("discount_type")
		disc_val = Decimal(str(reservation.get("discount_value") or 0))
		disc_cur = (reservation.get("discount_currency") or "").strip().upper() or home
	discount_off = Decimal(0)
	contract_net = contract
	if disc_kind and disc_val > 0:
		calc = apply_currency_discount(
			gross_home=base_gross,
			kind=disc_kind,
			value=disc_val,
			home_currency=home,
			discount_currency=disc_cur,
			tariff_night=(currency, nightly, fx),
			rounding_step=Decimal(str(rp.get("rounding_step") or 0)),
			date=str(date),
			property=reservation.property,
		)
		gross = calc["net_home"]
		discount_off = calc["discount_home"]
		if calc["net_tx"] is not None:
			contract_net = calc["net_tx"]
	else:
		gross = base_gross
		share = _negotiated_share(reservation, snapshot, locked, night)
		if share != 1:
			gross = round_rate_amount(gross * share, 0, currency=home)
			contract_net = round_rate_amount(nightly * share, 0, currency=currency)
	rt = frappe.get_doc("Room Type", terms.get("room_type") or billing_room_type(reservation, date))
	gst = room_gst_rate(reservation.property, rt, gross + _over_cap_night(reservation))
	rate = gross / (Decimal(1) + Decimal(str(gst)) / Decimal(100)) if rates_include_tax(reservation.property) else gross
	shown_contract = contract_net if (disc_kind and disc_val > 0) else contract
	return {"rate": float(rate), "gst_rate": float(gst), "label": f"{currency} {shown_contract:,} at {fx:,} ({fx_date})",
	        "currency": currency, "fx_rate": float(fx), "fx_date": fx_date, "stale": False,
	        "base_entry": float(base_gross), "discount_amount": float(discount_off),
	        "contract_net_rate": float(contract_net)}


def _posted_contract_fx(reservation, currency: str, projected=()) -> tuple[Decimal, str, int] | None:
	"""The rates a Current Date stay's room nights actually posted at, from
	the charges themselves (voided nights and their reversals left out):
	their average - each night is the same contract amount - the latest
	rate date, and how many nights. A void_charge reversal carries no
	contract currency, so every Room line of the stay is read to find
	them. A night split over two folios is one night. A pro forma's
	`projected` room nights count at the rate they would post at."""
	rows = frappe.get_all("Folio Charge", filters={
		"parenttype": "Folio", "reservation": reservation.name, "charge_type": "Room"},
		fields=["name", "posting_date", "contract_currency", "contract_fx_rate", "contract_fx_date", "reversal_of"])
	rows += [frappe._dict(row) for row in projected if row.get("charge_type") == "Room"]
	reversed_rows = {row.reversal_of for row in rows if row.get("reversal_of")}
	posted = [row for row in rows if not row.get("reversal_of") and row.name not in reversed_rows
	          and row.get("contract_currency") == currency and Decimal(str(row.contract_fx_rate or 0)) > 0]
	if not posted:
		return None
	nights: dict[str, list[Decimal]] = {}
	for row in posted:
		night = str(getdate(row.posting_date)) if row.get("posting_date") else row.name
		nights.setdefault(night, []).append(Decimal(str(row.contract_fx_rate)))
	average = sum((sum(rates, Decimal(0)) / len(rates) for rates in nights.values()), Decimal(0)) / len(nights)
	latest = max(str(getdate(row.contract_fx_date)) for row in posted if row.contract_fx_date) \
		if any(row.contract_fx_date for row in posted) else None
	return average.quantize(Decimal("0.000000001")), latest, len(nights)


def _actual_reference_amounts(property: str, currency: str, booked_fx: Decimal, charges, payments) \
		-> tuple[Decimal, Decimal]:
	"""OPERA REFERENCE_CURRENCY_CALCULATION ACTUAL on a Current Date stay:
	each transaction in the reference currency at the rate of its own
	posting date - a room night at the rate it posted at (contract_fx_rate),
	any other line or payment at the governed rate of its posting date
	(pricing.posting_fx_rate), else the booked rate - and a payment
	tendered in that currency at what was tendered. A voided line and its
	reversal net to nothing and are left out. (charges, payments)."""
	from kamra.pricing import posting_fx_rate

	rates: dict = {}

	def rate_on(day) -> Decimal:
		key = str(getdate(day)) if day else None
		if key not in rates:
			posting = posting_fx_rate(property, currency, key) if key else None
			rates[key] = posting[0] if posting else booked_fx
		return rates[key]

	voided = {row.get("reversal_of") for row in charges if row.get("reversal_of")}
	total = Decimal(0)
	for row in charges:
		if row.get("reversal_of") or row.get("name") in voided:
			continue
		amount = Decimal(str(row.get("amount") or 0))
		gross = amount + amount * Decimal(str(row.get("gst_rate") or 0)) / Decimal(100)
		posted = Decimal(str(row.get("contract_fx_rate") or 0)) if row.get("contract_currency") == currency else 0
		total += gross / (posted if posted > 0 else rate_on(row.get("posting_date")))
	paid = Decimal(0)
	for row in payments:
		amount = Decimal(str(row.get("amount") or 0))
		tendered = abs(Decimal(str(row.get("fx_amount") or 0)))
		if row.get("currency") == currency and tendered:
			paid += tendered if amount >= 0 else -tendered
		else:
			paid += amount / rate_on(row.get("posting_date"))
	return total, paid


def active_billing_contract(reservation):
	"""Project the rate being billed without changing the historical booking snapshot."""
	from kamra.pricing import agreed_rate_differs, property_currency, saved_contract_tariff

	snapshot = saved_contract_tariff(reservation)
	tariff = snapshot.get("tariff") or {}
	override = snapshot.get("agreed_override") or {}
	manual = not cint(reservation.get("auto_price")) and cint(reservation.get("fixed_rate"))
	if not (override or manual):
		return snapshot
	home = property_currency(reservation.property)
	currency = override.get("currency")
	if currency and currency != home:
		# The override is the nightly price for the stay; old tier/night prices
		# belong to the published version. Retain only its source metadata.
		booked = tariff if currency == tariff.get("currency") else {}
		terms = {**tariff, "currency": currency, "property_currency": home, "nightly": override["amount"],
		         "fx_rate": override.get("fx_rate") or booked.get("fx_rate"),
		         "fx_date": override.get("fx_date") or booked.get("fx_date"), "nights": None}
		amount = override.get("home_amount", reservation.get("agreed_rate"))
		return {**snapshot, "pricing_basis": "fixed_foreign", "tariff": terms, "agreed_rate": amount}
	fixed_home = currency == home or (manual and not tariff)
	if manual and not currency and tariff and agreed_rate_differs(
		reservation.get("agreed_rate"), snapshot.get("agreed_rate"), home
	):
		foreign = tariff.get("currency")
		fixed_home = not foreign or foreign == home or not frappe.db.sql(
			"""SELECT 1 FROM `tabFolio Charge` fc JOIN `tabFolio` f ON f.name = fc.parent
			   WHERE (fc.reservation = %s OR (IFNULL(fc.reservation, '') = '' AND f.folio_type = 'Guest'
			          AND f.reservation = %s)) AND fc.contract_currency = %s
			   AND """ + NOT_VOIDED + " LIMIT 1", (reservation.name, reservation.name, foreign))
	if fixed_home:
		amount = float(reservation.get("agreed_rate") or 0)
		return {**snapshot, "pricing_basis": "fixed_home", "tariff": None, "agreed_rate": amount, "fixed_rate_amount": amount}
	return snapshot


def reference_currency(reservation, grand_total=None, balance=None, *, charges=None, payments=None, adjustment=0, billing_contract=None) -> dict | None:
	"""The stay's contract currency beside the bill's home amounts, as the
	Property's reference_currency_calculation says (OPERA Cloud): ACTUAL is
	the rate the stay was priced or posted at - its locked contract rate,
	or for a Current Date stay the average of the rates its nights posted
	at - and CURRENT is today's published rate (pricing.posting_fx_rate).
	With the bill's grand_total and balance, their equivalents in the
	contract currency at that rate; for ACTUAL on a Current Date stay with
	the bill's `charges` and `payments`, the sum of each transaction at its
	own posting rate instead. A fixed rate (OPERA Fixed Rate) is billed at
	its locked rate on any plan, so ACTUAL converts it at that rate. None
	for a home-currency stay, or when CURRENT has no published rate today."""
	from kamra.business_date import get_business_date
	from kamra.pricing import posting_fx_rate, reservation_fx_policy, round_rate_amount

	contract = active_billing_contract(reservation) if billing_contract is None else billing_contract
	tariff = contract.get("tariff") or {}
	currency, home = tariff.get("currency"), tariff.get("property_currency")
	if not currency or currency == home or not tariff.get("fx_rate"):
		return None
	calculation = frappe.db.get_value(
		"Property", reservation.property, "reference_currency_calculation") or "ACTUAL"
	fx, fx_date, nights = Decimal(str(tariff["fx_rate"])), tariff.get("fx_date"), None
	booked_fx = fx
	per_transaction = False
	if calculation == "CURRENT":
		posting = posting_fx_rate(reservation.property, currency, get_business_date(reservation.property))
		if not posting:
			return None
		fx, fx_date = posting
	elif reservation_fx_policy(reservation) == "Current Date" and not cint(reservation.get("fixed_rate")):
		# a fixed rate is never re-converted (current_date_contract_night): it stays at its locked rate
		per_transaction = charges is not None
		posted = _posted_contract_fx(reservation, currency, [row for row in charges or [] if row.get("projected")])
		if posted:
			fx, fx_date, nights = posted[0], posted[1] or fx_date, posted[2]
	out = {"currency": currency, "home_currency": home, "calculation": calculation,
	       "fx_rate": float(fx), "fx_date": fx_date}
	if nights:
		out["posted_nights"] = nights
	if per_transaction:
		total, paid = _actual_reference_amounts(reservation.property, currency, booked_fx, charges, payments or [])
		out["grand_total_reference"] = float(round_rate_amount(total, 0, currency=currency))
		out["balance_reference"] = float(round_rate_amount(total - paid + adjustment / fx, 0, currency=currency))
		return out
	if balance is not None:
		balance = Decimal(str(balance)) + adjustment
	for key, amount in (("grand_total_reference", grand_total), ("balance_reference", balance)):
		if amount is not None:
			out[key] = float(round_rate_amount(Decimal(str(amount or 0)) / fx, 0, currency=currency))
	return out


def room_assignment_start(reservation, room):
	"""Latest dated assignment of the physical room, for the next move's audit."""
	events = frappe.get_all("Agent Action Log", filters={
		"action_type": ("in", ["check_in", "room_move"]), "property": reservation.property,
		"reference_doctype": "Reservation", "reference_name": reservation.name,
	}, fields=["action_type", "after_snapshot"], order_by="creation desc, name desc")
	for event in events:
		after = frappe.parse_json(event.after_snapshot or "{}") or {}
		if event.action_type == "check_in" and not after:
			continue  # the guided endpoint also logs a descriptive check-in note
		return after.get("effective_date") if after.get("room") == room else None
	return None


def _legacy_source_assignment(reservation, source, target, effective, date):
	"""Corroborate an older move's source interval from adjacent physical-room changes."""
	from kamra.pricing import _version_move_dates

	first, end = getdate(reservation.check_in_date), getdate(effective)
	previous_room = previous_start = None
	for version in frappe.get_all("Version", filters={
		"ref_doctype": "Reservation", "docname": reservation.name,
	}, pluck="data", order_by="creation asc, name asc"):
		changes = {key: (old, new) for key, old, new in frappe.parse_json(version).get("changed", [])}
		if "room" not in changes:
			continue
		old, new = changes["room"]
		boundaries = _version_move_dates(changes.get("room_move_rate_cutoff", (None, None))[1], first, end)
		start = boundaries.pop() if len(boundaries) == 1 else None
		if (old, new, start) == (source, target, end) and previous_room == source and previous_start:
			return previous_start <= getdate(date) < end
		previous_room, previous_start = new, start
	return False


def _night_room_description(reservation, date):
	"""Replay physical assignments backwards. Rates can belong to a different type."""
	room, room_type = reservation.room, reservation.room_type
	changes = frappe.get_all("Agent Action Log", filters={"action_type": "room_move",
		"property": reservation.property, "reference_doctype": "Reservation", "reference_name": reservation.name},
		fields=["before_snapshot", "after_snapshot"], order_by="creation desc, name desc")
	cutoff = reservation.get("room_move_rate_cutoff")
	unknown = not changes and cutoff and date < str(cutoff)
	for change in changes:
		after = frappe.parse_json(change.after_snapshot or "{}") or {}
		before = frappe.parse_json(change.before_snapshot or "{}") or {}
		effective = after.get("effective_date")
		if not effective or after.get("room") != room:
			unknown = True
			break
		if date >= effective:
			break
		room = before.get("room")
		room_type = before.get("room_type") or room_type
		if before.get("assignment_since") and before["assignment_since"] <= date:
			break
	else:
		# Reaching the start of a partial log does not establish an arrival room.
		if changes:
			unknown = ("assignment_since" in before or not _legacy_source_assignment(
				reservation, room, after.get("room"), effective, date))
	if unknown:
		room = None
		# A billing type can differ from the physical assignment after a comp.
		room_type = None
	number = frappe.db.get_value("Room", {"name": room, "property": reservation.property}, "room_number") if room else None
	return (f"Room {number}" if number else "Room") + (f" · {room_type_label(room_type, reservation.property)}" if room_type else "")


def post_room_night(reservation, date, folios=None, *, room_posted: list | None = None,
                    package_stats: dict | None = None, advance_bill: bool = False) -> bool:
	"""Post one night's room (and meal plan / package elements) charge, routed by the
	company's billing rules. Skips dates already posted - safe to call
	from both audit and checkout. Pass `folios` (a dict) to batch saves
	across nights; when omitted the touched folios save immediately.
	`room_posted` (a list) takes the amount of the room line this call
	posted - a Current Date night at its posting rate - for Night Audit's
	total."""
	date = str(date)
	shared = bool(reservation.get("share_group_id"))
	if shared:
		from kamra.kamra.doctype.reservation.reservation import lock_property_for_inventory
		lock_property_for_inventory(reservation.property)
		reservation = frappe.get_doc("Reservation", reservation.name, for_update=True)
		shared = bool(reservation.get("share_group_id"))
	own_batch = folios is None
	if own_batch:
		folios = {}
	posted = False
	# the GST rate this call's room line posts at: its levy and extra guests follow it
	room_gst = None

	from kamra.packages import active_package_for_reservation, post_package_night_split
	pkg_doc = None if shared else active_package_for_reservation(reservation, date)

	lock_options = {"lock": True} if shared else {}
	if pkg_doc is not None or not _charge_posted(reservation.name, "Room", date, **lock_options):
		room_label = _night_room_description(reservation, date)
		day_use = _day_use_room_charge(reservation, date)
		current = None
		share = None
		if shared:
			from kamra.shares import nightly_share
			share = nightly_share(reservation, date, lock=True, advance_bill=advance_bill, folios=folios)
			if share is None:
				return False
		if share:
			night_rate, gst_rate = float(share["rate"]), share["gst"]
			desc = room_label
		elif day_use:
			night_rate, gst_rate, du_label = day_use
			desc = f"{room_label} · {du_label}"
		else:
			current = current_date_contract_night(reservation, date)
			if current:
				night_rate, gst_rate = current["rate"], current["gst_rate"]
			else:
				night_rate = _nightly_room_rate(reservation, date)
				gst_rate = _nightly_gst(reservation, date)
			desc = (room_label
			        + (" · day use" if getattr(reservation, "is_day_use", 0) else "")
			        + (f" · {current['label']}" if current else ""))
			locked = locked_night(reservation, date)
			if current and not current.get("stale") and float(current.get("discount_amount") or 0) > 0:
				from kamra.localization import amount_text
				desc += (f" · {amount_text(reservation.property, current['base_entry'])} "
				         f"less discount {amount_text(reservation.property, current['discount_amount'])}")
			elif locked is not None and float(locked.get("discount_amount") or 0) > 0:
				# the net rate is what bills; the line says what it came from,
				# base and discount on the basis the discount was given (gross
				# where rates include tax)
				from kamra.localization import amount_text
				from kamra.pricing import entry_rate, night_rate_places, rates_include_tax
				places = night_rate_places(reservation.property)
				off = float(locked.discount_amount)
				base = float(entry_rate(locked.rate, locked.gst_rate,
				                        rates_include_tax(reservation.property), places)) + off
				desc += (f" · {amount_text(reservation.property, base)} "
				         f"less discount {amount_text(reservation.property, off)}")
		if not float(gst_rate or 0):
			# SAP TCURX / OPERA: 0%-tax nightly room charges stay at the
			# currency's ISO 4217 minor unit at the line so charges_total ==
			# sum(lines) with payable currency units.
			from kamra.pricing import property_currency, round_rate_amount
			night_rate = float(round_rate_amount(
				night_rate, 0, "Nearest", property_currency(reservation.property)))
		fx_posted = {}
		if current:
			# the rate this night posted at: ACTUAL reference currency and Night Audit read it
			fx_posted = {"contract_currency": current["currency"], "contract_fx_rate": current["fx_rate"],
			             "contract_fx_date": current["fx_date"], "contract_fx_stale": int(current["stale"])}
		from kamra.room_statistics import category_rules
		rate_cat = category_rules(reservation.property).of(
			share["category"] if share else reservation.get("rate_category"))
		night_quote = {} if share else _quoted_room_night(reservation, date)
		sold_gross = night_quote.get("sold_room_gross") if not current and not day_use else None
		if pkg_doc is not None:
			pkg_room_gst = night_quote.get("room_gst_rate", gst_rate)
			pkg_res = post_package_night_split(
				reservation,
				pkg_doc,
				night_rate,
				pkg_room_gst,
				date,
				folios=folios,
				room_desc=desc,
				sold_gross=sold_gross,
				fx_posted=fx_posted,
				rate_category=rate_cat,
			)
			if package_stats is not None:
				package_stats["elements_posted"] = (
					int(package_stats.get("elements_posted") or 0) + pkg_res["elements_posted"]
				)
				package_stats["elements_amount"] = float(
					Decimal(str(package_stats.get("elements_amount") or 0))
					+ Decimal(str(pkg_res["elements_amount"]))
				)
				package_stats["addon_amount"] = float(
					Decimal(str(package_stats.get("addon_amount") or 0))
					+ Decimal(str(pkg_res["addon_amount"]))
				)
			if pkg_res["posted"]:
				posted = True
				room_gst = gst_rate
				if room_posted is not None:
					room_posted.append(pkg_res["night_total"])
				_post_room_levy(folios, reservation, date, night_rate, gst_rate)
		else:
			line = {
				**fx_posted,
				**({"tax_as_posted": 1, "gst_amount": float(Decimal(str(sold_gross)) - Decimal(str(night_rate)))}
				   if sold_gross is not None else {}),
				"posting_date": date,
				"charge_type": "Room",
				"reservation": reservation.name,
				"rate_category": rate_cat,
				"description": desc,
				"qty": 1,
				"rate": night_rate,
				"amount": night_rate,
				"gst_rate": gst_rate,
				"auto_posted": 1,
			}
			if share:
				line.update(share["fx"], share_room=share["room"], share_group=reservation.share_group_id,
				            share_snapshot=json.dumps({k: v for k, v in share.items() if k not in ("rate", "tax")}))
				if share["tax"] is not None:
					line.update(gst_amount=float(share["tax"]), tax_as_posted=1)
			_append_charge(folios, reservation, "Room", line)
			posted = True
			room_gst = gst_rate
			if room_posted is not None:
				room_posted.append(night_rate)
			_post_room_levy(folios, reservation, date, night_rate, gst_rate, **({"share": share} if share else {}))

	# Its own charge type, not the shared Misc bucket: dedupe is by type and
	# date, so a sundry the desk had already typed as Misc that day would
	# otherwise make the night's extra-bed charge look posted and skip it.
	over_cap = float(reservation.get("over_capacity_charge") or 0)
	if over_cap > 0 and not _charge_posted(
			reservation.name, "Extra Guests", date, **lock_options):
		guests = int(reservation.get("over_capacity_guests") or 0)
		if room_gst is None:
			# the room line posted in an earlier call: its rate, else the locked night's
			room_gst = _posted_room_gst(reservation.name, date)
		_append_charge(folios, reservation, "Extra Guests", {
			"posting_date": date,
			"charge_type": "Extra Guests",
			"reservation": reservation.name,
			"description": (f"{guests} extra guests by arrangement"
			                if guests else "Extra guests by arrangement"),
			"qty": 1,
			"rate": over_cap,
			"amount": over_cap,
			"gst_rate": _nightly_gst(reservation, date) if room_gst is None else room_gst,
			"auto_posted": 1,
		})
		posted = True

	if reservation.meal_plan and not _charge_posted(
			reservation.name, "Meal Plan", date, **lock_options):
		mp = frappe.get_doc("Meal Plan", reservation.meal_plan)
		locked = locked_night(reservation, date)
		if locked is not None:
			meal_amount = float(locked.meal_amount or 0)
		else:
			meal_amount = (
				(reservation.adults or 1) * float(mp.price_per_adult or 0)
				+ (reservation.children or 0) * float(mp.price_per_child or 0)
			)
		meal_gst = _fnb_gst(reservation.property)
		if meal_amount and not float(meal_gst or 0):
			from kamra.pricing import property_currency, round_rate_amount
			meal_amount = float(round_rate_amount(
				meal_amount, 0, "Nearest", property_currency(reservation.property)))
		if meal_amount:
			_append_charge(folios, reservation, "Meal Plan", {
				"posting_date": date,
				"charge_type": "Meal Plan",
				"reservation": reservation.name,
				"description": f"{mp.label or mp.code} × {reservation.adults} adult(s)",
				"qty": 1,
				"rate": meal_amount,
				"amount": meal_amount,
				"gst_rate": meal_gst,
				"auto_posted": 1,
			})

	if own_batch:
		save_folios(folios)
	return posted


def room_levy_components(base, pct, fixed, unit):
	"""Percentage and fixed portions at the precision used by a single room's levy."""
	precision = min(Decimal("0.01"), unit)
	percent_amount = Decimal(str(base)) * Decimal(str(pct)) / 100
	return (percent_amount.quantize(precision, rounding=ROUND_HALF_UP),
	        Decimal(str(fixed)).quantize(precision, rounding=ROUND_HALF_UP))


def _post_room_levy(folios, reservation, date, night_rate, gst_rate=None, *, share=None):
	"""The property's per-night levy on the room rate (municipality fee,
	city / tourism tax): a % of the night's rate and/or a fixed amount per
	occupied room-night (room_levy_per_night) - its own line so the invoice
	shows it and the ledger books it apart from room revenue. A taxable
	levy is taxed at `gst_rate`, the rate the night's room line posted at
	(a Current Date night's own), else the night's locked rate."""
	prop = frappe.get_cached_doc("Property", reservation.property)
	frozen = share.get("levy") if share else None
	posted_tax = {}
	if frozen is not None:
		pct = float(frozen["percent"])
		fixed = float(frozen["fixed"]) if share["lead"] == reservation.name else 0
		levy = float(frozen["amounts"].get(reservation.name, 0))
		levy_gst = frozen["gst"]
		posted_tax = {"gst_amount": float(frozen["taxes"].get(reservation.name, 0)), "tax_as_posted": 1}
	else:
		pct = float(prop.get("room_levy_percent") or 0)
		fixed = float(prop.get("room_levy_per_night") or 0) if not share or share["lead"] == reservation.name else 0
		levy_gst = (
			(_nightly_gst(reservation, date) if gst_rate is None else gst_rate)
			if int(prop.get("room_levy_taxable") if prop.get("room_levy_taxable") is not None else 1)
			else 0
		)
		from kamra.ledger import minor_unit
		unit = minor_unit(reservation.property)
		levy = sum(room_levy_components(night_rate, pct, fixed, unit))
		if not levy_gst:
			levy = levy.quantize(unit, rounding=ROUND_HALF_UP)
		levy = float(levy)
	if levy <= 0 or _charge_posted(
			reservation.name, "Room Levy", date, **({"lock": True} if share else {})):
		return
	from kamra.localization import pack_for
	label = prop.get("room_levy_label") or getattr(pack_for(prop.name), "ROOM_LEVY_LABEL", None) or "Room levy"
	parts = ([f"{pct:g}%"] if pct and night_rate else []) + (
		[f"{fixed:,g} per night"] if fixed else [])
	_append_charge(folios, reservation, "Room Levy", {
		"posting_date": date,
		"charge_type": "Room Levy",
		**posted_tax,
		"reservation": reservation.name,
		"description": f"{label} {' + '.join(parts)}",
		"qty": 1,
		"rate": levy,
		"amount": levy,
		"gst_rate": levy_gst,
		"auto_posted": 1,
	})


# A posted night is never re-priced in place: a later rate change reaches it
# only through these lines (kamra.settings "charged_night_rate_change"),
# dated the business date, with the night they belong to in stay_date.
RATE_ADJUSTMENT = "Rate Adjustment"


def _quoted_room_night(reservation, date):
	try:
		snap = frappe.parse_json(reservation.get("quote_snapshot") or "{}")
	except (TypeError, ValueError):
		return {}
	if not isinstance(snap, dict):
		return {}
	nights = snap.get("nightly") or []
	night = next((n for n in nights if n.get("date") == date), {})
	if night and "sold_room_gross" not in night and (snap.get("package") or any(n.get("package_id") for n in nights)):
		# Older quote snapshots already hold the pre-rounding net and component tax.
		from kamra.ledger import minor_unit
		from kamra.pricing import rates_include_tax, sold_night_gross
		if rates_include_tax(reservation.property):
			net = night.get("rate") if night.get("discount_amount") else night.get("quoted_base_rate")
			if net is not None:
				night["sold_room_gross"] = sold_night_gross(net, night.get("gst_rate"), minor_unit(reservation.property),
				                                         None if "planned_rate" in night else night.get("package_tax"))
	return night


def charged_nights(reservation_name: str) -> dict:
	"""What the folios hold for each night of the stay already charged: the
	room line(s) posted for the night (plus included package-element splits
	belonging to that night) plus every rate adjustment made to it since,
	and the night's levy. {night: {base, tax, gst_rate, levy_base, levy_tax,
	folio}} - folio is where the night's room line went.

	A night counts as charged under STANDS_ON_ITS_DATE, the same standing
	rule _charge_posted uses: a same-date void drops out, while a closed
	night voided on a later business date stays in the map (it will never
	be posted again) at the net amount the folio still holds for it."""
	rows = frappe.db.sql(
		"""
		SELECT IF(fc.charge_type = %(adj)s, fc.stay_date, fc.posting_date) AS night,
		       fc.charge_type, fc.amount, fc.gst_rate, fc.gst_amount, fc.tax_as_posted,
		       fc.idempotency_key, fc.parent, fc.share_room,
		       CASE WHEN """ + NOT_VOIDED + """ THEN 1 ELSE 0 END AS live
		FROM `tabFolio Charge` fc
		JOIN `tabFolio` f ON fc.parent = f.name
		WHERE (fc.reservation = %(res)s
		       OR (IFNULL(fc.reservation, '') = '' AND f.reservation = %(res)s))
		  AND ((fc.charge_type = 'Room' AND """ + STANDS_ON_ITS_DATE + """)
		       OR (fc.charge_type = 'Room Levy' AND """ + NOT_VOIDED + """)
		       OR (fc.charge_type = %(adj)s AND fc.stay_date IS NOT NULL
		           AND """ + NOT_VOIDED + """))
		ORDER BY fc.charge_type = 'Room' DESC, live DESC, fc.amount DESC
		""", {"res": reservation_name, "adj": RATE_ADJUSTMENT}, as_dict=True)
	out: dict = {}
	pkg_nights: set[str] = set()
	for r in rows:
		key = str(r.night)
		amount = float(r.amount or 0) if r.live else 0.0
		tax = (
			float(r.gst_amount or 0) if (r.live and r.get("tax_as_posted"))
			else amount * float(r.gst_rate or 0) / 100
		)
		if r.charge_type == "Room Levy":
			if key in out:
				out[key]["levy_base"] += amount
				out[key]["levy_tax"] += tax
			continue  # a levy with no room line is not a charged night
		if r.charge_type == "Room" and (r.get("idempotency_key") or "").startswith("pkg:"):
			pkg_nights.add(key)
		n = out.setdefault(key, {"base": 0.0, "tax": 0.0, "levy_base": 0.0,
		                         "levy_tax": 0.0, "folio": r.parent})
		n["base"] += amount
		n["tax"] += tax
		if r.share_room:
			n["shared"] = True
	if pkg_nights:
		pkg_rows = frappe.db.sql(
			"""
			SELECT fc.posting_date AS night, fc.amount, fc.gst_rate, fc.gst_amount, fc.tax_as_posted,
			       CASE WHEN """ + NOT_VOIDED + """ THEN 1 ELSE 0 END AS live
			FROM `tabFolio Charge` fc
			JOIN `tabFolio` f ON fc.parent = f.name
			LEFT JOIN `tabReservation Package Allowance` rpa
			  ON rpa.idempotency_key = fc.idempotency_key
			LEFT JOIN `tabPackage Element` pe
			  ON pe.name = COALESCE(
			      NULLIF(rpa.package_element, ''),
			      SUBSTRING_INDEX(SUBSTRING_INDEX(fc.idempotency_key, ':', 3), ':', -1)
			  )
			WHERE (fc.reservation = %(res)s
			       OR (IFNULL(fc.reservation, '') = '' AND f.reservation = %(res)s))
			  AND fc.charge_type != 'Room'
			  AND fc.idempotency_key LIKE 'pkg:%%'
			  AND fc.idempotency_key NOT LIKE 'pkg:%%:room:%%'
			  AND COALESCE(rpa.included_in_rate, pe.included_in_rate, 0) = 1
			  AND """ + STANDS_ON_ITS_DATE,
			{"res": reservation_name},
			as_dict=True,
		)
		for pr in pkg_rows:
			key = str(pr.night)
			if key not in pkg_nights or key not in out:
				continue
			amount = float(pr.amount or 0) if pr.live else 0.0
			tax = (
				float(pr.gst_amount or 0) if (pr.live and pr.get("tax_as_posted"))
				else amount * float(pr.gst_rate or 0) / 100
			)
			out[key]["base"] += amount
			out[key]["tax"] += tax
	from kamra.pricing import night_rate_places
	prop = frappe.db.get_value("Reservation", reservation_name, "property")
	places = night_rate_places(prop)
	for n in out.values():
		n["base"] = round(n["base"], places)
		n["gst_rate"] = round(n["tax"] / n["base"] * 100, 2) if n["base"] else 0.0
	return out


def post_rate_lines(reservation, lines: list) -> None:
	"""Post rate-change / rate-correction lines (from
	Reservation.hold_charged_nights) on the folio each night went to, dated
	the business date. Posted lines are never edited or deleted; a night
	whose folio has since been invoiced takes its line on the open folio."""
	from kamra.business_date import get_business_date
	from kamra.room_statistics import posted_night_category
	day = get_business_date(reservation.property)
	folios: dict = {}
	for line in lines:
		name = line["folio"]
		if frappe.db.get_value("Folio", name, "status") == "Closed":
			name = open_line_target(reservation, "Room")
		if name not in folios:
			folios[name] = frappe.get_doc("Folio", name)
		folios[name].append("charges", {
			"posting_date": day,
			"stay_date": line["night"],
			"charge_type": RATE_ADJUSTMENT,
			"reservation": reservation.name,
			"rate_category": posted_night_category(
				reservation.property, reservation.name, line["night"]),
			"description": line["description"],
			"qty": 1,
			"rate": line["amount"],
			"amount": line["amount"],
			"gst_rate": line["gst_rate"],
			"auto_posted": 1,
		})
	save_folios(folios)


def save_folios(folios: dict):
	"""Save each touched folio and post its new lines to the ledger."""
	from kamra.ledger import save_folio
	for doc in folios.values():
		_recalculate(doc)
		save_folio(doc)


def carried_forward_until(reservation_name: str):
	"""For a stay that was in house at go-live: the business date its
	opening balance was posted. Nights before it were billed by the old
	system and are inside that balance. None for every other stay."""
	cutoff = frappe.db.sql(
		"""
		SELECT MAX(fc.posting_date) FROM `tabFolio Charge` fc
		JOIN `tabFolio` f ON fc.parent = f.name
		WHERE fc.charge_type = 'Opening Balance' AND f.folio_type = 'Guest'
		  AND (fc.reservation = %(res)s
		       OR (IFNULL(fc.reservation, '') = '' AND f.reservation = %(res)s))
		""", {"res": reservation_name})[0][0]
	return getdate(cutoff) if cutoff else None


def post_remaining_nights(reservation) -> int:
	"""At checkout: make sure every night of the stay is on its folio(s) -
	from go-live on, for a stay carried forward from the old system."""
	open_folio(reservation)  # guest folio always exists after checkout
	folios: dict = {}
	posted = 0
	date = getdate(reservation.check_in_date)
	cutoff = carried_forward_until(reservation.name)
	if cutoff and cutoff > date:
		date = cutoff
	end = getdate(reservation.check_out_date)
	if getattr(reservation, "is_day_use", 0) and end == date:
		end = getdate(add_days(date, 1))  # day-use bills its one date
	while date < end:
		if post_room_night(reservation, date, folios):
			posted += 1
		date = getdate(add_days(date, 1))
	from kamra.fixed_charges import post_departure_fixed_charges
	post_departure_fixed_charges(reservation, folios=folios)
	save_folios(folios)
	if reservation.get("status") == "Checked Out":
		from kamra.packages import settle_package_breakage
		settle_package_breakage(reservation.name)
	return posted


def split_folio(reservation: str, folio_type: str = "Extra", *, for_update=False) -> str:
	"""Open an additional folio for a stay (Extra or Company) so charges
	can be routed/split - e.g. 70/30 corporate vs personal.

	If an empty folio of this type already exists, reuse it rather than
	piling up blank duplicates - you only ever need one unused split open."""
	res = frappe.get_doc("Reservation", reservation, for_update=for_update)
	for f in frappe.db.get_values(
		"Folio",
		filters={"reservation": reservation, "folio_type": folio_type,
		         "status": "Open"},
		fieldname=["name", "grand_total", "payments_total"], as_dict=True, for_update=for_update,
	):
		if not f.grand_total and not f.payments_total:
			return f.name
	# a company folio bills the stay's company as its window's payee
	company = res.get("company") if folio_type == "Company" else None
	return open_window(reservation=res.name, folio_type=folio_type,
	                   payee_type="Company" if company else "Guest",
	                   payee_company=company, for_update=for_update)


def transfer_charge(from_folio: str, charge_row: str, to_folio: str):
	"""Move one charge line between two open folios of the same stay."""
	transfer_charges(from_folio, [charge_row], to_folio)


def _folio_group(folio) -> str | None:
	if folio.get("group_booking"):
		return folio.group_booking
	return folio.get("reservation") and frappe.db.get_value(
		"Reservation", folio.reservation, "group_booking")


def _same_stay_or_group(src, dst) -> bool:
	# two folios with no stay (posting masters, account invoices) are not
	# one stay: only their group can make them one bill
	if src.reservation and src.reservation == dst.reservation:
		return True
	sg, dg = _folio_group(src), _folio_group(dst)
	return bool(sg and sg == dg)


def _assert_same_stay(src, dst):
	"""Charges may move within a stay, or within a group - company pays
	some rooms' charges on the master, guests settle the rest."""
	if not _same_stay_or_group(src, dst):
		frappe.throw("Folios belong to different stays.")


def transfer_charges(from_folio: str, charge_rows: list, to_folio: str):
	"""Move several charge lines at once - one save per folio, so a
	corporate re-bill of a whole stay is a single operation."""
	from kamra.api import _lock_folio_scope
	_lock_folio_scope(folios=[from_folio, to_folio])
	src = frappe.get_doc("Folio", from_folio)
	dst = frappe.get_doc("Folio", to_folio)
	_assert_same_stay(src, dst)
	move_charges(src, dst, charge_rows)


SHARE_CHARGE_FIELDS = ("share_room", "share_group", "share_snapshot")
CONTRACT_FX_FIELDS = ("contract_currency", "contract_fx_rate", "contract_fx_date", "contract_fx_stale")


def _contract_fx_trail(row) -> dict:
	"""The rate a Current Date room night posted at, for a line that carries
	it to another folio (ACTUAL reference currency reads it there)."""
	return {field: row.get(field) for field in CONTRACT_FX_FIELDS}


def move_charges(src, dst, charge_rows: list, reservation: str | None = None,
                 key: str | None = None, routing_instruction: str | None = None):
	"""Take charge lines off one open folio and put them on another, ledger
	and all. Callers decide whose bill a line may move to: transfer_charges
	keeps it within a stay or group, pos.move_room_bill to the right room.
	`reservation` re-points the lines at another stay."""
	if "Closed" in (src.status, dst.status):
		frappe.throw("Both folios must be open to transfer charges.")
	from kamra.ledger import ledger_charges, reverse_ledger_for_charge
	moved = []
	# a voided line and its reversal (a split's too) net to nothing and stay
	# on the folio they were posted to, as the bridge's void fences need: a
	# move of the whole bill leaves them, and moves what stands in their place
	pairs = void_pairs(src)
	if charge_rows and all(r in pairs for r in charge_rows):
		frappe.throw("A voided line and its reversal stay on the folio they were posted to.")
	for charge_row in charge_rows:
		if charge_row in pairs:
			continue
		row = next((c for c in src.charges if c.name == charge_row), None)
		if not row:
			frappe.throw(f"Charge {charge_row} not found on {src.name}.")
		# an outlet charge (the bridge's source_ref) was fiscalised at the
		# outlet: it moves whole and only within its stay or group, so the
		# outlet's own record still names the right bill
		if row.get("source_ref") and not _same_stay_or_group(src, dst):
			frappe.throw(frappe._("An outlet charge moves only between bills of its own "
			                      "stay or group. Correct the room at the outlet."))
		dst.append("charges", {
			"posting_date": row.posting_date,
			"stay_date": row.get("stay_date"),
			"charge_type": row.charge_type,
			"reservation": reservation or row.get("reservation"),
			"rate_category": row.get("rate_category"),
			"description": row.description,
			"qty": row.qty,
			"rate": row.rate,
			"amount": row.amount,
			"gst_rate": row.gst_rate,
			"auto_posted": row.auto_posted,
			"is_alcohol": row.get("is_alcohol"),
			# the line keeps the codes, and a Current Date night the rate, it was posted with
			"transaction_code": row.get("transaction_code"),
			"gl_code": row.get("gl_code"),
			**_contract_fx_trail(row),
			**{field: row.get(field) for field in SHARE_CHARGE_FIELDS},
			# and its outlet reference, so the hotel invoice never bills an
			# outlet charge again (the source row is saved away first)
			"source_ref": row.get("source_ref"),
			# routed there now (pos.move_room_bill); a hand move is no routing
			"routing_instruction": routing_instruction,
			# a split part keeps its lineage and the tax it was split with
			"split_of": row.get("split_of"),
			"tax_as_posted": row.get("tax_as_posted"),
			"gst_amount": row.gst_amount,
			"idempotency_key": key,
		})
		moved.append(dst.charges[-1])
		src.charges.remove(row)
		# the line leaves this folio's ledger and joins the target's
		reverse_ledger_for_charge(src.property, src.name, row.name,
		                          f"to {dst.name}", prefix="TRANSFER")
	_recalculate(src)
	_recalculate(dst)
	src.save(ignore_permissions=True)
	dst.save(ignore_permissions=True)
	ledger_charges(dst, moved)
	import kamra.ledger as _ledger_mod
	if hasattr(_ledger_mod, "sync_folio_rounding_ledger"):
		_ledger_mod.sync_folio_rounding_ledger(src)
	return moved


def split_charge(from_folio: str, charge_row: str, to_folio: str,
                 percent: float | None = None,
                 amount: float | None = None, credit: bool = False,
                 qty: float | None = None) -> dict:
	"""Split one charge line in two - the 70/30 corporate deal, the shared
	room, the disputed minibar - as OPERA does: by a percent of the line, an
	amount (before tax) or a number of its units. Both parts may stay on
	this folio, or one may go to another open folio of the same stay or
	group.

	The posted line is never rewritten. A reversal voids it (reversal_of,
	the void pair the ERPNext bridge's KMR-VOID-001 fences admit) and the two
	parts are posted new as ordinary lines, each naming the line it was split
	from (split_of), so either part can be corrected, voided, moved or split
	again. All three keep the line's description, so a banquet (EVT-) or
	outlet (ORD-) line is still recognised by the reports.

	The part split off is worked out to the currency's minor unit, and so is
	its tax; the line keeps the rest of both, so gross and tax add up exactly
	across the bills (tax_as_posted). A room night priced with tax included
	is split on its price, the figure the guest was quoted.

	Refused, as OPERA refuses them: a payment, a package line (the meal plan
	posted with the room) and an outlet charge (the bridge's source_ref),
	which moves only whole (move_charges); and an amount or percent that is
	not above zero. credit=True splits an allowance line instead (both parts
	stay negative); only the server does that, following the charges it
	wrote down."""
	from kamra.api import _lock_folio_scope
	_lock_folio_scope(folios=[from_folio, to_folio])
	src = frappe.get_doc("Folio", from_folio)
	dst = _split_target(src, to_folio)
	row, sign, line = _split_line(src, charge_row, credit)
	units = Decimal(str(row.qty or 1))
	part_units = Decimal(1)
	whole = _split_whole(line)
	if qty:
		part_units = Decimal(str(qty))
		if units <= 1:
			frappe.throw(frappe._("This line is one unit: split it by amount or percent."))
		if part_units != part_units.to_integral_value() or not 1 <= part_units < units:
			frappe.throw(frappe._("Split off between 1 and {0} of its {1} units.").format(
				int(units) - 1, f"{units.normalize():f}"))
		part, kept = split_figure(line, line["figure"] * part_units / units)
	elif percent is not None:
		part, kept = split_figure(line, line["figure"] * Decimal(str(percent)) / 100)
	elif amount is not None:
		if Decimal(str(amount)) <= 0:
			frappe.throw(frappe._("Give an amount above zero to split off."))
		if line["priced_with_tax"]:
			base = Decimal(str(amount)).quantize(line["unit"], rounding=ROUND_HALF_UP)
			part = (base, (base * line["rate"]).quantize(line["unit"], rounding=ROUND_HALF_UP))
			kept = (whole[0] - part[0], whole[1] - part[1])
		else:
			part, kept = split_figure(line, amount)
	else:
		frappe.throw("Give a percent, an amount or a quantity to split off.")
	if whole != cut_figure(line, line["figure"]):
		kept = (whole[0] - part[0], whole[1] - part[1])
	if not 0 < _figure(line, part) < line["figure"]:
		from kamra.localization import money_text
		frappe.throw(f"Split must be between 0 and {money_text(src.property, line['figure'])} (exclusive).")
	return _post_split(src, row, sign, line, [(dst, part, part_units)],
	                   units - part_units if qty else Decimal(1), kept=kept)


def _split_target(src, to_folio: str):
	"""The folio a part goes to: this one, or an open one of its stay or group."""
	dst = src if to_folio == src.name else frappe.get_doc("Folio", to_folio)
	if dst is not src:
		_assert_same_stay(src, dst)
	if "Closed" in (src.status, dst.status):
		frappe.throw("Both folios must be open to split charges.")
	return dst


def _split_line(src, charge_row: str, credit: bool):
	"""The line to split, refused where OPERA refuses a split, and its
	figures (magnitudes; `sign` puts an allowance's back): the base and tax
	as they stand, the rate, the currency's minor unit and the figure a split
	cuts - the price for a night priced with tax included, else the base."""
	row = next((c for c in src.charges if c.name == charge_row), None)
	if not row:
		if any(p.name == charge_row for p in src.payments):
			frappe.throw(frappe._("A payment can't be split. Refund it or move the "
			                      "charges instead."))
		frappe.throw(f"Charge {charge_row} not found on {src.name}.")
	if row.name in void_pairs(src):
		frappe.throw("A voided line and its reversal can't be split.")
	if row.get("source_ref"):
		frappe.throw(frappe._("An outlet charge can't be split. Move it whole to the "
		                      "other bill of the stay or group."))
	if row.charge_type == "Meal Plan":
		frappe.throw(frappe._("A package line (the meal plan posted with the room) "
		                      "can't be split."))
	sign = -1 if credit else 1
	base = Decimal(str(row.amount or 0)) * sign
	if base <= 0 or (credit and row.charge_type != "Allowance"):
		frappe.throw("Only positive charge lines can be split.")
	tax = _line_tax(row, Decimal(str(row.amount or 0))) * sign
	return row, sign, line_figures(src.property, row.charge_type, base, tax,
	                               row.gst_rate)


def priced_with_tax(property: str, charge_type: str) -> bool:
	"""A room night at a property whose rates include tax is priced with its
	tax (OPERA: an inclusive transaction code): it is split, and a routing
	limit caps it, on its price. Every other line on its base."""
	from kamra.pricing import rates_include_tax
	return charge_type == "Room" and bool(rates_include_tax(property))


def line_figures(property: str, charge_type: str, base, tax=None, gst_rate=0) -> dict:
	"""The figures one splitter cuts a line by - split_charge, share_charges
	and routing limits alike: its base and tax as they stand (tax worked out
	from the rate when not given), the rate, the currency's minor unit and the
	figure that is cut - the price for a line priced with tax, else the base."""
	from kamra.ledger import minor_unit
	base = Decimal(str(base or 0))
	rate = Decimal(str(gst_rate or 0)) / 100
	tax = base * rate if tax is None else Decimal(str(tax))
	unit = minor_unit(property)
	with_tax = priced_with_tax(property, charge_type)
	return {"base": base, "tax": tax, "rate": rate, "unit": unit,
	        "priced_with_tax": with_tax,
	        "figure": ((base + tax) if with_tax else base).quantize(unit, rounding=ROUND_HALF_UP)}


def _cut(line: dict, share: Decimal) -> tuple[Decimal, Decimal]:
	"""`share` of the line, to the minor unit, with its own tax to the minor
	unit: (base, tax). A line priced with tax included is cut on its price."""
	return cut_figure(line, line["figure"] * share)


def cut_figure(line: dict, figure) -> tuple[Decimal, Decimal]:
	"""The part of the line whose figure (its price, or its base) is
	`figure`, to the minor unit, with its own tax to the minor unit:
	(base, tax). The line keeps the rest of both."""
	unit, rate = line["unit"], line["rate"]
	part = Decimal(str(figure)).quantize(unit, rounding=ROUND_HALF_UP)
	if line["priced_with_tax"]:
		tax = (part * rate / (1 + rate)).quantize(unit, rounding=ROUND_HALF_UP)
		return part - tax, tax
	return part, (part * rate).quantize(unit, rounding=ROUND_HALF_UP)


def _figure(line: dict, part: tuple[Decimal, Decimal]) -> Decimal:
	return part[0] + part[1] if line["priced_with_tax"] else part[0]


def split_figure(line: dict, part_figure) -> tuple[tuple[Decimal, Decimal], tuple[Decimal, Decimal]]:
	"""Split `line` into (part, remainder), both quantized to the currency's
	minor unit so gross, tax and net of the two parts sum exactly to the
	quantized whole line (no sub-shilling / sub-minor-unit remainders)."""
	whole = (line["base"], line["tax"]) if line.get("tax_as_posted") else cut_figure(line, line["figure"])
	part = cut_figure(line, part_figure)
	if line.get("tax_as_posted") and line["priced_with_tax"]:
		gross = part[0] + part[1]
		tax = max(gross - whole[0], min(part[1], whole[1]))
		part = (gross - tax, tax)
	if _figure(line, part) >= line["figure"]:
		return whole, (Decimal(0), Decimal(0))
	if _figure(line, part) <= 0:
		return (Decimal(0), Decimal(0)), whole
	return part, (whole[0] - part[0], whole[1] - part[1])


def _split_whole(line: dict) -> tuple[Decimal, Decimal]:
	"""Whole (base, tax) to conserve across split parts: for a tax-inclusive
	room night, conserve its exact posted (base, tax) so unrounded net or tax
	(e.g. 5000 / 1.12 = 4464.29 + 535.7148) is never dropped; otherwise
	quantize to the currency's minor unit via cut_figure."""
	if line["priced_with_tax"]:
		return line["base"], line["tax"]
	return cut_figure(line, line["figure"])


def _post_split(src, row, sign: int, line: dict, cuts: list, kept_units: Decimal,
                kept: tuple[Decimal, Decimal] | None = None) -> dict:
	"""Void the line and post its parts: each cut (folio, (base, tax), units)
	on its folio, and the rest of the line's base and tax on this one."""
	same_line = {
		"posting_date": row.posting_date,
		"stay_date": row.get("stay_date"),
		"charge_type": row.charge_type,
		"reservation": row.get("reservation"),
		"rate_category": row.get("rate_category"),
		"description": row.description,
		"allowance_serial": row.get("allowance_serial"),
		"gst_rate": row.gst_rate,
		"auto_posted": row.auto_posted,
		"is_alcohol": row.get("is_alcohol"),
		"transaction_code": row.get("transaction_code"),
		"gl_code": row.get("gl_code"),
		"split_of": row.name,
		**{field: row.get(field) for field in SHARE_CHARGE_FIELDS},
	}
	names = ", ".join(dict.fromkeys(f.name for f, _p, _u in cuts if f is not src))
	# the posted line stays; its reversal voids it on its own date, tax and all
	src.append("charges", {
		**same_line,
		"qty": row.qty,
		"rate": -float(row.rate or 0),
		"amount": -float(row.amount or 0),
		"tax_as_posted": row.get("tax_as_posted"),
		"gst_amount": -float(row.gst_amount or 0),
		"reversal_of": row.name,
		"void_reason": f"Split to {names}" if names else "Split",
		"voided_by": frappe.session.user,
	})
	reversal = src.charges[-1]
	if kept is not None:
		kept_base, kept_tax = kept
	else:
		whole = _split_whole(line)
		kept_base = whole[0] - sum(p[0] for _f, p, _u in cuts)
		kept_tax = whole[1] - sum(p[1] for _f, p, _u in cuts)

	def part_line(base, tax, units, folio):
		# a part left on the folio a routing instruction put the line on is
		# still routed by it, so its usage follows; a part moved off is not
		routed = row.get("routing_instruction") if folio is src else None
		return {**same_line, **_contract_fx_trail(row), "qty": float(units),
		        "rate": float(base * sign / units),
		        "amount": float(base * sign), "gst_amount": float(tax * sign),
		        "tax_as_posted": 1, "routing_instruction": routed}

	src.append("charges", part_line(kept_base, kept_tax, kept_units, src))
	kept = src.charges[-1]
	folios, moved = {src.name: src}, []
	for f, (base, tax), units in cuts:
		f = folios.setdefault(f.name, f)
		f.append("charges", part_line(base, tax, units, f))
		moved.append((f, f.charges[-1]))
	for f in folios.values():
		_recalculate(f)
		f.save(ignore_permissions=True)
	# the reversal takes the posted line off the ledger; each part is posted
	# on the ledger of the folio it is on
	from kamra.ledger import _original_charge_entry, ledger_charges, record_charge_ledger
	first = _original_charge_entry(src.name, row.name)
	if first:
		record_charge_ledger(src, reversal.as_dict(), reversal_of=first.name)
	ledger_charges(src, [kept])
	for f, part in moved:
		ledger_charges(f, [part])
	return {"kept": float(kept_base * sign),
	        "moved": float(sum(p[0] for _f, p, _u in cuts) * sign),
	        "reversal": reversal.name, "kept_row": kept.name,
	        "moved_row": moved[0][1].name, "moved_rows": [p.name for _f, p in moved]}


def open_guest_bills(reservation: str, names: list | None = None) -> list[dict]:
	"""A separate bill for each guest sharing the room. The main guest keeps
	the stay's Guest folio; everyone else gets an Extra folio made out to
	them (bill_to_name), so each bill is invoiced in that guest's name.
	`names` defaults to the stay's other occupants. Asking twice for the
	same name returns the bill that is already open."""
	res = frappe.get_doc("Reservation", reservation)
	if res.status != "Checked In":
		frappe.throw(frappe._("Separate bills open once the guests are checked in."))
	main = (res.guest_name or "").strip().lower()
	if names is None:
		names = [o.full_name for o in res.get("occupants") or []]
	wanted = []
	for n in names:
		n = (n or "").strip()
		if n and n.lower() != main and n.lower() not in {w.lower() for w in wanted}:
			wanted.append(n)
	if not wanted:
		frappe.throw(frappe._("Add the other guests' names to the stay first."))
	out = []
	for n in wanted:
		existing = frappe.db.get_value(
			"Folio", {"reservation": res.name, "folio_type": "Extra",
			          "status": "Open", "bill_to_name": n})
		if not existing:
			existing = open_window(reservation=res.name, folio_type="Extra",
			                       payee_type="Guest", bill_to_name=n)
		out.append({"folio": existing, "name": n})
	return out


def share_charges(from_folio: str, charge_rows: list, to_folios: list,
                  shares: list | None = None) -> dict:
	"""Share charge lines (the room nights, usually) between the guests'
	bills: each line is cut into one part per target bill, evenly by
	default or by `shares` (a percent per target), and the source bill
	keeps the rest. Each line is split once (split_charge's trail: one
	reversal, the parts and the rest), every part a share of the line's
	figure - its price, for a night priced with tax included - to the
	currency's minor unit with its tax, so the bills add up to the line
	exactly. A part that rounds to nothing, or would take all that is left
	(0.02 three ways), is skipped."""
	to_folios = [f for f in to_folios if f and f != from_folio]
	if not to_folios or len(set(to_folios)) != len(to_folios):
		frappe.throw(frappe._("Pick the other guests' bills to share with."))
	# a line listed twice would be cut twice: more than its share leaves
	charge_rows = list(dict.fromkeys(charge_rows or []))
	if not charge_rows:
		frappe.throw(frappe._("Pick the charges to share."))
	if shares:
		shares = [Decimal(str(x)) for x in shares]
		if len(shares) != len(to_folios) or any(x <= 0 for x in shares) \
				or sum(shares) >= 100:
			frappe.throw(frappe._("Give each other bill a share above 0%; "
			                      "together under 100%."))
		shares = [x / 100 for x in shares]
	else:
		shares = [Decimal(1) / (len(to_folios) + 1)] * len(to_folios)
	from kamra.api import _lock_folio_scope
	_lock_folio_scope(folios=[from_folio, *to_folios])
	src = frappe.get_doc("Folio", from_folio)
	present = {c.name: c for c in src.charges}
	for row in charge_rows:
		if row not in present:
			frappe.throw(frappe._("Charge {0} is not on {1}.").format(row, from_folio))
		if Decimal(str(present[row].amount or 0)) <= 0:
			frappe.throw(frappe._("Only charges above zero can be shared."))
	moved = Decimal(0)
	for row in charge_rows:
		src = frappe.get_doc("Folio", from_folio)
		targets = [_split_target(src, f) for f in to_folios]
		line_row, sign, line = _split_line(src, row, False)
		whole = _split_whole(line)
		rem_base, rem_tax = whole
		cuts, left = [], line["figure"]
		for dst, share in zip(targets, shares):
			part = cut_figure(line, line["figure"] * share)
			if 0 < _figure(line, part) < left:
				if not line["priced_with_tax"] and part[1] > rem_tax:
					part = (part[0], rem_tax)
				cuts.append((dst, part, Decimal(1)))
				left -= _figure(line, part)
				rem_base -= part[0]
				rem_tax -= part[1]
		if cuts:
			moved += Decimal(str(_post_split(
				src, line_row, sign, line, cuts, Decimal(1),
				kept=(rem_base, rem_tax))["moved"]))
	return {"lines": len(charge_rows), "moved": float(moved)}


def percent_fee(reservation, pct: float) -> float:
	"""₹ for a cancellation band expressed as a percentage of the stay.

	A tiered policy says "half back between three and seven days", which is
	neither one night nor the whole stay, so it cannot be said with a fee
	basis. Pre-tax; GST rides on the folio line.
	"""
	from kamra.ledger import _currency_places, _round_currency
	places = max(_currency_places(reservation.property), 2)
	return _round_currency(
		Decimal(str(reservation.amount_before_tax or 0)) * Decimal(str(pct)) / Decimal(100),
		places,
	)


def policy_fee(reservation, basis: str, pct: float | None = None) -> float:
	"""₹ for a cancellation/no-show fee basis: 'First Night' or
	'Full Stay' (pre-tax; GST rides on the folio line).

	`pct` defers to percent_fee(), so a caller holding a tiered band can use
	the one entry point the posting path already calls.
	"""
	if pct is not None:
		return percent_fee(reservation, pct)
	if basis == "First Night":
		return float(_nightly_room_rate(reservation,
		                                reservation.check_in_date))
	if basis == "Full Stay":
		return float(reservation.amount_before_tax or 0)
	return 0.0


def post_policy_fee(reservation, basis: str, label: str,
                    pct: float | None = None,
                    posting_date: str | None = None) -> float:
	"""Open the guest folio and post a policy fee (no-show / late
	cancellation). Returns the pre-tax amount posted.

	`pct` posts that percentage of the stay, for a tiered policy.
	`posting_date` is the business day the fee belongs to: the night audit
	passes the date it is closing; left out (a cancellation at the desk) it
	is the property's current business date - never the calendar, which
	runs a day ahead between midnight and the audit."""
	amount = policy_fee(reservation, basis, pct=pct)
	gst = _room_gst(reservation)
	if amount > 0 and not float(gst or 0):
		from kamra.pricing import property_currency, round_rate_amount
		amount = float(round_rate_amount(amount, 0, "Nearest", property_currency(reservation.property)))
	if amount <= 0:
		return 0.0
	folio = frappe.get_doc("Folio", open_folio(reservation))
	if folio.status == "Closed":
		return 0.0
	already = any(c.description == label for c in folio.charges)
	if already:
		return 0.0
	if not posting_date:
		from kamra.business_date import get_business_date
		posting_date = get_business_date(reservation.property)
	folio.append("charges", {
		"posting_date": str(posting_date),
		"charge_type": "Misc",
		"reservation": reservation.name,
		"description": label,
		"qty": 1,
		"rate": amount,
		"amount": amount,
		"gst_rate": gst,
		"auto_posted": 1,
	})
	_recalculate(folio)
	from kamra.ledger import save_folio, transfer_advances_to_guest
	save_folio(folio)
	# the advance now pays the fee: it leaves the Deposit ledger (money held
	# for an arrival) for the Guest ledger, as at check-in. Left behind, a
	# no-show's advance showed as still held while its folio read settled.
	transfer_advances_to_guest(reservation.name, business_date=str(posting_date))
	return amount


def close_folio(folio_name: str) -> str:
	"""Close the folio and assign the GST invoice number.

	A folio closes only when it is settled: what the guest owes is paid, what
	the company owes has moved to the city ledger (transfer_to_city_ledger
	zeroes it with a Company Credit payment), and nothing is owed back.
	Closing with money on it was how a guest left owing."""
	folio = payment_folio_for_update(folio_name)
	if folio.status == "Closed":
		return folio.invoice_number
	prev_grand = folio.get("grand_total")
	_recalculate(folio, payments_only=True)
	if prev_grand is not None and abs(float(folio.grand_total or 0) - float(prev_grand)) > 1e-9:
		from kamra import ledger as _ledger_mod
		if hasattr(_ledger_mod, "sync_folio_rounding_ledger"):
			_ledger_mod.sync_folio_rounding_ledger(folio)
	from kamra.ledger import _format_currency_amount
	balance = settlement_balance(folio)
	if balance:
		if balance > 0:
			frappe.throw(frappe._(
				"Cannot close the folio: {0} is still owed. Collect "
				"payment, or move the company's share to the city ledger, "
				"first.").format(_format_currency_amount(folio.property, balance)))
		frappe.throw(frappe._(
			"Cannot close the folio: {0} is owed back to the guest. "
			"Issue the refund first.").format(_format_currency_amount(folio.property, -balance)))
	from frappe.model.naming import make_autoname

	folio.status = "Closed"
	folio.closed_on = now_datetime()
	# one series per property: each GSTIN must have its own unique
	# invoice series, and GST caps invoice numbers at 16 chars - a short
	# initials code keeps INV-KDP-26-00001 inside the limit
	code = "".join(w[0] for w in folio.property.split())[:3].upper()
	folio.invoice_number = make_autoname(f"INV-{code}-.YY.-.#####")
	_recalculate(folio, payments_only=True)
	folio.save(ignore_permissions=True)
	# a company bill moved to the city ledger at checkout, before it had a
	# number: the account's bill takes the invoice number now
	frappe.db.set_value("City Ledger Entry", {
		"folio": folio.name, "kind": "Invoice", "invoice_number": ("is", "not set")},
		"invoice_number", folio.invoice_number)
	# statutory e-invoicing (ZATCA in Saudi Arabia) - the pack decides
	from kamra import localization as loc
	loc.on_invoice_issued(loc.pack_for(folio.property), folio.name)
	return folio.invoice_number


def _business_date(property: str) -> str:
	from kamra.business_date import get_business_date
	return str(get_business_date(property))


def post_allowance(folio_name: str, amount: float, reason: str,
                   gst_rate: float = 0, approver: str | None = None,
                   requested_by: str | None = None,
                   serial: str | None = None, *,
                   contract_currency: str | None = None, contract_fx_rate: float | None = None,
                   contract_fx_date: str | None = None) -> str:
	"""A negative adjustment against a specific folio - the hotel writes off
	part of a charge (service recovery, dispute) WITHOUT touching the original
	line, so the trail stays honest. GST reverses at the same rate as the
	charge being adjusted."""
	folio = frappe.get_doc("Folio", folio_name)
	if folio.status == "Closed":
		frappe.throw("This folio is settled - pass the allowance on the "
		             "guest's open folio, or cancel the invoice first.")
	amount = abs(float(amount))
	if amount and not float(gst_rate or 0):
		from kamra.pricing import property_currency, round_rate_amount
		amount = float(round_rate_amount(amount, 0, "Nearest", property_currency(folio.property)))
	if not amount:
		frappe.throw("Allowance amount is required.")
	if not (reason or "").strip():
		frappe.throw("An allowance needs a reason - it goes on the record.")
	folio.append("charges", {
		# the business date, like every other folio line: the calendar runs a
		# day ahead between midnight and the audit
		"posting_date": _business_date(folio.property),
		"charge_type": "Allowance",
		"description": reason.strip(),
		"allowance_serial": serial,
		**({"contract_currency": contract_currency, "contract_fx_rate": contract_fx_rate,
		    "contract_fx_date": contract_fx_date} if contract_currency else {}),
		"qty": 1,
		"rate": -amount,
		"amount": -amount,
		"gst_rate": float(gst_rate or 0),
	})
	_recalculate(folio)
	from kamra.ledger import save_folio
	save_folio(folio)
	from kamra.localization import money_text
	from kamra.savings import log_action
	log_action("post_allowance", "Folio", folio.name, folio.property,
	           rationale=f"Allowance {money_text(folio.property, amount)} - {reason.strip()}"
	                     + (f" (asked by {requested_by})"
	                        if requested_by and requested_by != frappe.session.user
	                        else ""),
	           approver=approver,
	           after_snapshot={"serial": serial} if serial else None)
	return folio.name


def void_charge(folio_name: str, charge_row: str, reason: str = "", *, correction: dict | None = None) -> dict:
	"""Void a wrong charge line on an OPEN folio - the correction path for
	a mis-posted bill (wrong amount, duplicate, wrong guest). The line stays
	on the folio and a line of the opposite sign follows it (same charge
	type and transaction code, the reason and who voided it), posted to the
	ledger like any other line, so the pair nets to zero and the bill keeps
	its trail (OPERA's adjustment). Once the folio is invoiced, use an
	allowance instead. Every void is logged with who and why."""
	folio = frappe.get_doc("Folio", folio_name)
	if folio.status == "Closed":
		frappe.throw("This folio is settled - post an allowance to reverse a "
		             "charge, or cancel the invoice first.")
	if not (reason or "").strip():
		frappe.throw("A void needs a reason - it goes on the record.")
	row = next((c for c in folio.charges if c.name == charge_row), None)
	if not row:
		frappe.throw("That charge line is not on this folio.")
	if row.get("reversal_of"):
		frappe.throw("This line reverses a void and can't be voided. Post the "
		             "charge again if it was right.")
	if voided_rows(folio).get(row.name):
		frappe.throw("This line is already voided.")
	removed = {"charge_type": row.charge_type, "description": row.description,
	           "amount": row.amount}
	line = {**removed, "row": row.name, "posting_date": row.posting_date,
	        "gst_rate": row.gst_rate, "reservation": row.get("reservation")}
	from kamra.business_date import get_business_date
	from kamra.ledger import _original_charge_entry, code_for_charge_type, record_charge_ledger
	folio.append("charges", {
		"posting_date": get_business_date(folio.property),
		"stay_date": row.get("stay_date"),
		"charge_type": row.charge_type,
		"transaction_code": row.get("transaction_code")
		or code_for_charge_type(row.charge_type),
		"reservation": row.get("reservation"),
		"rate_category": row.get("rate_category"),
		"description": f"VOID: {row.description or row.charge_type} - {reason.strip()}"[:140],
		"allowance_serial": row.get("allowance_serial"),
		"qty": row.qty,
		"rate": -float(row.rate or 0),
		"amount": -float(row.amount or 0),
		"gst_rate": row.gst_rate,
		# a line split to the minor unit is voided with the tax it was posted with
		"tax_as_posted": row.get("tax_as_posted"),
		"gst_amount": -float(row.gst_amount or 0),
		"auto_posted": row.auto_posted,
		"is_alcohol": row.get("is_alcohol"),
		"reversal_of": row.name,
		"void_reason": reason.strip(),
		"voided_by": frappe.session.user,
		**{field: row.get(field) for field in SHARE_CHARGE_FIELDS},
		**(correction or {}),
	})
	_recalculate(folio)
	folio.save(ignore_permissions=True)
	# the reversal line's own ledger entry, linked to the original's; a line
	# that never reached the ledger (posted before it existed) takes none,
	# so the folio and its ledger stay equal
	first = _original_charge_entry(folio.name, row.name)
	if first:
		record_charge_ledger(folio, folio.charges[-1].as_dict(), reversal_of=first.name)
		import kamra.ledger as _ledger_mod
		if hasattr(_ledger_mod, "sync_folio_rounding_ledger"):
			_ledger_mod.sync_folio_rounding_ledger(folio)
	from kamra.webhooks import emit_charge_voided
	emit_charge_voided(folio, line, reason or "")
	from kamra.localization import money_text
	from kamra.savings import log_action
	log_action("void_charge", "Folio", folio.name, folio.property,
	           rationale=f"Voided {removed['charge_type']} "
	                     f"{removed['description']} "
	                     f"{money_text(folio.property, removed['amount'])}"
	                     + (f" - {reason}" if reason else ""))
	return {"folio": folio.name, "removed": removed, "reversal": folio.charges[-1].name,
	        "balance": settlement_balance(folio)}


def voided_rows(folio) -> dict:
	"""{voided line: its reversal line} on a loaded folio."""
	return {c.reversal_of: c.name for c in folio.charges if c.get("reversal_of")}


def void_pairs(folio) -> set:
	"""Every line of a void pair on a loaded folio: each voided (or split)
	line and its reversal. Together they net to nothing."""
	return {n for c in folio.charges if c.get("reversal_of") for n in (c.name, c.reversal_of)}


def part_settle(folio_name: str) -> dict:
	"""Interim invoice mid-stay: freeze the fully-paid folio with a real
	invoice number and open a fresh one so the stay keeps running - the
	long-stay pattern (guests settling every N days without checking out).
	The room stays occupied; only the bill closes."""
	folio = frappe.get_doc("Folio", folio_name, for_update=True)
	if folio.status == "Closed":
		frappe.throw("This folio is already settled.")
	_recalculate(folio, payments_only=True)
	if settlement_balance(folio):
		frappe.throw("Settle the balance first - an interim invoice needs "
		             "the folio fully paid.")
	if not folio.charges:
		frappe.throw("Nothing to invoice on this folio.")
	invoice = close_folio(folio.name)
	fresh = frappe.get_doc({
		"doctype": "Folio",
		"property": folio.property,
		"reservation": folio.reservation,
		"guest": folio.guest,
		"folio_type": folio.folio_type,
		"status": "Open",
		"opened_on": now_datetime(),
		"share_prearrival": folio.get("share_prearrival"),
		"group_booking": folio.get("group_booking"),
		# the same window and payee go on billing
		"window_no": folio.get("window_no"),
		"bill_to_name": folio.get("bill_to_name"),
		**{f: folio.get(f) for f in WINDOW_FIELDS},
	})
	_recalculate(fresh)
	fresh.insert(ignore_permissions=True)
	return {"invoice_number": invoice, "closed_folio": folio.name,
	        "new_folio": fresh.name}


CREDIT_AND_REBILL = "hospitality_kamra.credit_and_rebill"


def bridge_invoice(folio) -> str | None:
	"""The Sales Invoice the ERPNext bridge issued for this folio (the
	bridge's sales_invoice field), or None. That invoice is corrected only
	by the bridge's credit and re-bill - OPERA's Credit Bill, SAP's VF11
	cancellation, a URA EFRIS credit note - never reopened or credited here
	on its own."""
	return folio.get("sales_invoice") or None


def sales_invoice_field() -> list[str]:
	"""The bridge owns the optional Folio.sales_invoice custom field."""
	get_meta = getattr(frappe, "get_meta", None)
	return ["sales_invoice"] if get_meta and get_meta("Folio").has_field("sales_invoice") else []


_MAX_REBILL_HOPS = 16


def _verified_invoice_anchors(links, fetch):
	"""Walk stored pointers backwards in batches. Only the root's backlink proves ownership."""
	pending = dict(links)
	trails = {folio: [] for folio in links}
	stored = {}
	verified_paths = {}
	for _depth in range(_MAX_REBILL_HOPS + 1):
		if not pending:
			break
		rows = fetch("name", pending.values())
		by_name = {row.name: row for row in rows}
		parents = {}
		for folio, name in pending.items():
			row = by_name.get(name)
			trail = trails[folio]
			if not row or row.kamra_ref not in (None, "", folio) or name in trail:
				continue
			if not trail:
				stored[folio] = row
			trail.append(name)
			if row.get("kamra_rebill_of"):
				parents[folio] = row.kamra_rebill_of
			elif row.kamra_ref == folio:
				verified_paths[folio] = list(reversed(trail))
		pending = parents
	return {folio: stored[folio] for folio in verified_paths}, verified_paths


def _settlement_invoice_rows(*, filters, fields, order_by, for_update):
	if for_update:
		return frappe.db.get_values("Sales Invoice", filters=filters, fieldname=fields, order_by=order_by,
		                            as_dict=True, for_update=True)
	return frappe.get_all("Sales Invoice", filters=filters, fields=fields, order_by=order_by)


def _active_folio_invoices(links, scope, fields, *, fetch=None, for_update=False):
	"""Choose submitted descendants only after verifying every edge from the owned root."""
	try:
		has_rebill = frappe.db.has_column("Sales Invoice", "kamra_rebill_of")
	except (frappe.DoesNotExistError, frappe.db.TableMissingError):
		has_rebill = False
	if fetch is None:
		columns = [*fields, "docstatus", "kamra_ref"]
		if has_rebill:
			columns.append("kamra_rebill_of")

		def fetch(field, names):
			return _settlement_invoice_rows(
				filters={**scope, "is_return": 0, "docstatus": ("in", [1, 2]),
				         field: ("in", list(set(names)))},
				fields=columns, order_by="docstatus asc, creation desc, name asc", for_update=for_update,
			)

	anchors, paths = _verified_invoice_anchors(links, fetch)
	active = {folio: si for folio, si in anchors.items() if si.docstatus == 1}
	frontier = {si.name: (folio, paths[folio]) for folio, si in anchors.items()} if has_rebill else {}
	invalid = set()
	while frontier:
		children = fetch("kamra_rebill_of", frontier)
		next_level = {}
		for child in children:
			if child.kamra_rebill_of not in frontier:
				continue
			folio, trail = frontier[child.kamra_rebill_of]
			if folio in invalid or child.kamra_ref not in (None, "", folio):
				continue
			if child.name in trail or len(trail) > _MAX_REBILL_HOPS:
				invalid.add(folio)
				continue
			path = [*trail, child.name]
			next_level[child.name] = (folio, path)
			if child.docstatus == 1 and (folio not in active or len(path) > len(paths[folio])):
				active[folio] = child
				paths[folio] = path
		frontier = next_level
	for folio in invalid:
		active.pop(folio, None)
		paths.pop(folio, None)
	return active, paths


def _readable_invoice_names(names, *, filter_list=True):
	"""Apply list restrictions and native document-read hooks before exposing a reference."""
	try:
		if filter_list and names:
			names = frappe.get_list("Sales Invoice", filters={"name": ("in", list(names))},
			                        pluck="name", limit=0)
		return {name for name in names if frappe.has_permission("Sales Invoice", ptype="read", doc=name)}
	except (frappe.PermissionError, frappe.DoesNotExistError, frappe.db.TableMissingError):
		return set()


def _linked_sales_credit_notes(folios, property: str, currency: str, *, read_note_numbers: bool, for_update=False):
	"""Read returns against verified links and check native access before displaying identifiers."""
	links = {row.get("name") or row.get("folio"): bridge_invoice(row) for row in folios if bridge_invoice(row)}
	if not links or not frappe.db.exists("DocType", "Sales Invoice"):
		return {}, {}, set(), {}

	company = frappe.get_cached_doc("Property", property).get("company")
	try:
		has_backlink = frappe.db.has_column("Sales Invoice", "kamra_ref")
		has_company = frappe.db.has_column("Sales Invoice", "company")
	except (frappe.db.TableMissingError, frappe.DoesNotExistError):
		has_backlink = has_company = False
	if (has_company or has_backlink) and not (company and has_backlink):
		return {}, {}, set(), {}

	scope = {"currency": currency, "company": company or ["is", "not set"]}
	amount_fields = ["name", "grand_total", "rounded_total"]
	originals, paths = {}, {}
	try:
		if has_backlink:
			originals, paths = _active_folio_invoices(links, scope, amount_fields, for_update=for_update)
			links = {folio: si.name for folio, si in originals.items()}
		if not links:
			return {}, {}, set(), {}
		returns = _settlement_invoice_rows(
			filters={**scope, "docstatus": 1, "is_return": 1,
			         "return_against": ("in", list(links.values()))},
			fields=[*amount_fields, "return_against", "update_outstanding_for_self",
			        *(["kamra_ref"] if has_backlink else [])],
			order_by="name asc", for_update=for_update,
		)
	except (frappe.db.TableMissingError, frappe.DoesNotExistError):
		return {}, {}, set(), {}

	notes_by_invoice = {}
	for note in returns:
		notes_by_invoice.setdefault(note.return_against, []).append(note)
	notes = {
		folio: [note for note in notes_by_invoice.get(invoice, [])
		        if not has_backlink or note.get("kamra_ref") in (None, "", folio)]
		for folio, invoice in links.items()
	}
	visible = set()
	if read_note_numbers and frappe.has_permission("Sales Invoice", ptype="read"):
		names = set(links.values()) | {note.name for linked_notes in notes.values() for note in linked_notes}
		for path in paths.values():
			names.update(path)
		readable = _readable_invoice_names(names, filter_list=has_backlink)
		for folio, invoice in links.items():
			if invoice not in readable or (has_backlink and not readable.issuperset(paths[folio])):
				continue
			if has_backlink:
				visible.add(invoice)
			visible.update(note.name for note in notes[folio] if note.name in readable)
	return originals, notes, visible, paths


def _credit_projections(folios, property, *, read_note_numbers=True, for_update=False):
	"""Project collectible balances at the property currency's precision."""
	if not folios:
		return []
	from kamra.ledger import _money
	from kamra.pricing import _minor_unit, property_currency

	currency = property_currency(property)
	unit = Decimal(1).scaleb(-_minor_unit(currency))
	originals, notes, visible, paths = _linked_sales_credit_notes(
		folios, property, currency, read_note_numbers=read_note_numbers, for_update=for_update,
	)

	def rounded(value):
		return _money(value).quantize(unit, rounding=ROUND_HALF_UP)

	positions = []
	for row in folios:
		name = row.get("name") or row.get("folio")
		invoice = originals.get(name)
		returns = notes.get(name, [])
		# ERP posts these self-updating returns to their own receivable.
		# Issuing one does not apply it to the original invoice.
		credited = rounded(sum(
			(max(Decimal(0), -_money(n.rounded_total or n.grand_total)) for n in returns
			 if not cint(n.get("update_outstanding_for_self"))),
			Decimal(0),
		))
		posted = rounded(row.get("balance")) if invoice else _money(row.get("balance"))
		if invoice and len(paths[name]) > 1:
			# The original reversal retires the old bill. Carry its net collections
			# onto the replacement once; only credits on the active bill reduce it.
			collected = rounded(row.get("grand_total")) - posted
			posted = rounded(invoice.rounded_total or invoice.grand_total) - collected
		balance = posted - credited
		status = None
		if credited:
			invoice_total = invoice.rounded_total or invoice.grand_total if invoice else row.get("grand_total")
			status = "Credited" if credited >= rounded(invoice_total) else "Partially credited"
		positions.append(({
			"sales_invoice": invoice.name if invoice and invoice.name in visible else None,
			"credited_amount": float(credited),
			"credit_notes": [n.name for n in returns if n.name in visible],
			"settlement_balance": float(balance),
			"net_collectible_balance": float(max(Decimal(0), balance)),
			"credit_status": status,
		}, invoice.name if invoice else None))
	return positions


def credit_note_positions(folios, property: str, *, read_note_numbers: bool = True) -> list[dict]:
	return [position for position, _ in _credit_projections(folios, property, read_note_numbers=read_note_numbers)]


def settlement_balance(doc, *, for_update=False):
	"""Amount to collect (positive) or refund (negative) after applied credits and rebills."""
	if not bridge_invoice(doc):
		return doc.balance or 0
	# A locked folio needs current reads of its invoice chain and returns as well.
	position, _ = _credit_projections([doc], doc.property, read_note_numbers=False, for_update=for_update)[0]
	return position["settlement_balance"]


def project_settlement_balances(rows, property):
	"""Replace posted balances on transient report rows with their signed settlement amounts."""
	if not rows or not sales_invoice_field():
		return
	linked = frappe.get_all(
		"Folio", filters={"property": property, "name": ("in", [row.name for row in rows]),
		                  "sales_invoice": ("is", "set")},
		fields=["name", "balance", "grand_total", "sales_invoice"], limit=0,
	)
	positions = credit_note_positions(linked, property, read_note_numbers=False)
	balances = {row.name: position["settlement_balance"] for row, position in zip(linked, positions, strict=True)}
	for row in rows:
		if row.name in balances:
			row.balance = balances[row.name]


def reconcile_credit_note_status(folio) -> dict:
	"""Derive current credit status without changing the posted folio.

	The bridge can call this after submit or cancel in the same transaction.
	Reads derive the same position, so they also cover older bridged folios.
	"""
	if isinstance(folio, str):
		folio = frappe.get_doc("Folio", folio)
	return credit_note_positions([folio], folio.get("property"))[0]


def invoiced(folio) -> bool:
	"""The bill was invoiced: here (invoice_number) or by the bridge, whose
	check-out close sets only its Sales Invoice (sales_invoice)."""
	return bool(folio.get("invoice_number") or bridge_invoice(folio))


def visible_payment_rows(payments):
	"""Copy folio payment rows for display, respecting native voucher read permissions."""
	links = ("payment_entry", "reversal_of")
	names = {p.get(field) for p in payments for field in links if p.get(field)}
	if not names:
		return payments
	visible = set()
	if frappe.db.exists("DocType", "Payment Entry"):
		existing = frappe.get_all("Payment Entry", filters={"name": ("in", list(names))}, pluck="name", limit=0)
		visible = {name for name in existing if frappe.has_permission("Payment Entry", ptype="read", doc=name)}
	result = []
	for payment in payments:
		row = frappe._dict(payment)
		for field in links:
			name = row.get(field)
			if name and name not in visible:
				row[field] = None
				if row.get("reference") == name:
					row["reference"] = None
		result.append(row)
	return result


def refuse_bridge_correction(folio):
	invoice = reconcile_credit_note_status(folio)["sales_invoice"]
	if not invoice:
		frappe.throw(frappe._(
			"Correct this bill in the accounting system with a credit note and re-bill."
		))
	frappe.throw(frappe._(
		"Invoice {0} was issued by the accounting system as {1}. Correct it there "
		"with a credit note and re-bill; this bill is not reopened or credited here."
	).format(folio.invoice_number or folio.name, invoice))


def route_to_credit_and_rebill(folio, reason: str, amount: float | None = None):
	"""Hand a correction of a bridge-issued invoice to the bridge's credit
	and re-bill (`amount` None: the whole invoice). Refused when the bridge
	has no such flow: nothing is corrected here instead."""
	from kamra.erp_bridge import get_bridge_script, run_bridge_script
	script = get_bridge_script(CREDIT_AND_REBILL)
	if not script:
		refuse_bridge_correction(folio)
	return run_bridge_script(script, {"folio": folio.name, "reason": reason,
	                                  "amount": amount})


def cancel_invoice(folio_name: str, reason: str) -> dict:
	"""Void a generated invoice the auditable way: the number goes to the
	Cancelled Invoice register (finance never loses a bill in the sequence,
	and the old bill stays printable), then the folio reopens so it can be
	corrected and re-closed under a fresh number. An invoice the ERPNext
	bridge issued is never reopened here (bridge_invoice)."""
	folio = frappe.get_doc("Folio", folio_name)
	if bridge_invoice(folio):
		refuse_bridge_correction(folio)
	if folio.status != "Closed" or not folio.invoice_number:
		frappe.throw("Only a settled folio with an invoice can be cancelled.")
	if folio.folio_type == "Account":
		# reopened, an account invoice has no stay to correct it from and
		# nothing would reissue it: the AR way out is a credit note
		frappe.throw("An account invoice can't be cancelled - issue a credit "
		             "note against it instead.")
	if not (reason or "").strip():
		frappe.throw("A cancellation needs a reason - it goes on the record.")
	frappe.get_doc({
		"doctype": "Cancelled Invoice",
		"property": folio.property,
		"folio": folio.name,
		"invoice_number": folio.invoice_number,
		"grand_total": folio.grand_total,
		"cancelled_on": now_datetime(),
		"reason": reason.strip(),
	}).insert(ignore_permissions=True)
	old = folio.invoice_number
	# the company's account stops quoting the cancelled number; the bill
	# takes its new one when the folio closes again
	frappe.db.set_value("City Ledger Entry", {
		"folio": folio.name, "kind": "Invoice", "invoice_number": old},
		"invoice_number", None)
	folio.invoice_number = None
	folio.status = "Open"
	folio.closed_on = None
	frappe.flags.kamra_invoice_cancel = True
	try:
		folio.save(ignore_permissions=True)
	finally:
		frappe.flags.kamra_invoice_cancel = False
	# where the authority forbids cancelling (ZATCA), this issues the
	# credit note that reverses the invoice
	from kamra import localization as loc
	loc.on_invoice_cancelled(loc.pack_for(folio.property), folio.name, old,
	                         reason.strip())
	return {"cancelled": old, "folio": folio.name}


def apply_no_show(res, pol, decision, business_date) -> dict:
	"""Mark one stay No Show and apply the property's no-show fee policy
	(`decision` from kamra.no_show.audit_outcome): the fee is posted on
	the business date being closed, or waived for an OTA booking. Used by
	the night audit and by the End of Day "Mark no-show" action, so both
	charge the same way. Returns the audit pack's no-show row."""
	before = res.status
	frappe.db.set_value("Reservation", res.name, "status", "No Show")
	# a direct status write skips the Reservation doc events, so tell the
	# webhook subscribers here
	from kamra.webhooks import emit, reservation_payload
	emit("reservation.modified", res.property,
	     {**reservation_payload(res), "status": "No Show",
	      "changes": {"status": [before, "No Show"]}})
	# and the bell, the live desk screens and the channel push, for the same
	# reason: the room is free again, so OTAs should see it now, not at the
	# hourly push_all_ari
	from kamra.notifications import no_show
	no_show(res)
	from kamra.channel_manager import enqueue_property_push
	from kamra.realtime import notify
	notify(res, "on_update")
	enqueue_property_push(res.property)
	ns = {"reservation": res.name, "guest_name": res.guest_name,
	      "room_type": room_type_label(res.room_type, res.property),
	      "check_in_date": str(res.check_in_date),
	      "source": res.get("channel") or res.get("source"),
	      "fee": decision.fee, "fee_amount": 0.0,
	      "outcome": "No show - no fee"}
	if decision.fee == "waived":
		ns["outcome"] = f"No show - fee waived ({decision.reason})"
	elif decision.fee == "charge":
		fee = post_policy_fee(res, pol.charge,
		                      f"No-show charge ({pol.charge})",
		                      posting_date=business_date)
		ns["fee_amount"] = float(fee or 0)
		ns["outcome"] = f"No show - {pol.charge.lower()} fee posted"
	ns["credit"] = no_show_credit(res.name)
	return ns


NO_SHOW_FORFEIT = "Forfeited advance (no-show)"
CREDIT_ACTIONS = ("Refund", "Keep as forfeited deposit")


def no_show_credit(reservation: str) -> float:
	"""What a No Show's open guest folio holds for the guest after the fee:
	the advance paid beyond it. 0 when the folio is settled, owes, or does
	not exist."""
	row = frappe.db.get_value(
		"Folio", {"reservation": reservation, "folio_type": "Guest",
		          "status": ("!=", "Closed")}, ["name", "balance", "property"], as_dict=True)
	if not row:
		return 0.0
	from kamra.ledger import _currency_places, _round_currency
	places = max(_currency_places(row.property), 2)
	project_settlement_balances([row], row.property)
	return _round_currency(max(0.0, -float(row.balance or 0)), places)


def settle_no_show_credit(res, action: str, reason: str, mode: str | None = None,
                          pin: str | None = None) -> dict:
	"""Refund a no-show's credit to the guest, or keep it as a forfeited
	deposit (a charge at the room's tax rate whose total is the credit, so
	the folio settles at zero). Either way the advance first leaves the
	Deposit ledger for the Guest ledger, as post_policy_fee does, so the
	refund or the forfeit charge clears the Guest ledger and nothing is left
	shown as held."""
	if res.status != "No Show":
		frappe.throw(frappe._("Only a No Show's credit is settled here (this one is {0}).")
		             .format(res.status))
	if action not in CREDIT_ACTIONS:
		frappe.throw(frappe._("Choose Refund or Keep as forfeited deposit."))
	if not (reason or "").strip():
		frappe.throw(frappe._("A reason is required."))
	credit = no_show_credit(res.name)
	if credit <= 0:
		frappe.throw(frappe._("This no-show has no credit on its folio."))
	folio = frappe.db.get_value(
		"Folio", {"reservation": res.name, "folio_type": "Guest",
		          "status": ("!=", "Closed")}, "name")
	from kamra.business_date import get_business_date
	from kamra.ledger import transfer_advances_to_guest
	bd = str(get_business_date(res.property))
	transfer_advances_to_guest(res.name, business_date=bd)
	if action == "Refund":
		from kamra.api import _refund_folio_payment
		_refund_folio_payment(folio, credit, mode or "Cash",
		                      f"No-show credit refund: {reason.strip()}", pin=pin)
	else:
		from kamra.api import _pin_guard, _post_folio_charge
		_pin_guard(folio, pin)
		from kamra.ledger import _currency_places
		places = max(_currency_places(res.property), 2)
		gst = Decimal(str(_room_gst(res)))
		# pre-tax so that amount + tax is the credit; what rounding leaves
		# (under a minor unit) is inside close_folio's tolerance
		pre = (Decimal(str(credit)) * 100 / (100 + gst)).quantize(Decimal(1).scaleb(-places))
		# the reason stays out of the description: words in it must not
		# re-tax the line (_resolve_charge_gst reads the description)
		_post_folio_charge(folio, "Misc", NO_SHOW_FORFEIT, float(pre),
		                   gst_rate=float(gst), posting_date=bd,
		                   reservation=res.name)
	from kamra.localization import money_text
	from kamra.savings import log_action
	log_action("no_show_credit", "Reservation", res.name, res.property,
	           rationale=f"{res.name} no-show credit {money_text(res.property, credit)}: "
	                     f"{action.lower()} - {reason.strip()[:100]}")
	return {"reservation": res.name, "action": action, "amount": credit,
	        "credit": no_show_credit(res.name)}


def audit_clock(property: str):
	"""When the audit is running, on the property's clock - what a late
	arrival's ETA is compared with. Its own function so tests can pin it."""
	from kamra.no_show import property_now
	return property_now(property)


def run_night_audit(property: str, business_date: str | None = None,
                    acknowledge=None, note: str | None = None,
                    mode: str | None = None) -> dict:
	"""Automated end-of-day: open missing folios, post the night's room
	charges for every in-house guest, flag no-shows. Closes only the
	property's current business date; a retry for a date that already
	closed returns ``already_ran`` instead of posting twice.

	The night audit checklist (kamra.night_audit) runs first:

	mode "manual" (Billing, REST, MCP): refused while a blocking check has
	  items, unless every one is listed in ``acknowledge`` with a ``note``
	  (the reason) - who acknowledged it and when is stored on the run.
	mode "scheduled" (the 15 minute tick): follows the property's
	  na_auto_audit_with_blockers - "Run and report exceptions" closes the
	  day and records the blockers as exceptions; "Hold and alert" leaves
	  the business date and alerts Hotel Admin and Finance.
	mode None (an internal call): ordinary blockers are recorded as exceptions.
	GL control tie-out blockers always hold a scheduled run and stop an
	internal run. A manual run requires an authorized variance override.

	Every run saves its audit pack (night_audit.build_pack) on the record."""
	# In non-request contexts (the 3 AM scheduler, bench console) frappe.local.lang
	# is unset, which makes Frappe's number/currency formatting raise
	# UnboundLocalError. Default it so the audit runs everywhere.
	if not getattr(frappe.local, "lang", None):
		frappe.local.lang = "en"
	from kamra.business_date import ensure_business_date
	current = frappe.db.get_value("Property", property, "business_date", for_update=True)
	if not current:
		current = ensure_business_date(property)
	business_date = str(getdate(business_date or current))
	# look the run up per property AND date - a global per-date lookup would
	# make the second property's audit silently no-op every night. Checked
	# before the date guards so a retry after success answers already_ran.
	# an End of Day draft (status Draft) is the wizard's progress for the
	# date, not a closed day
	done = frappe.db.get_value(
		"Night Audit Run",
		{"property": property, "business_date": business_date,
		 "status": ("!=", "Draft")}, "name", for_update=True)
	if done:
		return {"already_ran": True, "audit": done}
	draft_name = frappe.db.get_value(
		"Night Audit Run",
		{"property": property, "business_date": business_date,
		 "status": "Draft"}, "name", for_update=True)
	# The audit only ever closes the property's current business date. A
	# different date would post into a closed day or skip days, and then
	# set the business date to that date + 1.
	if getdate(business_date) != getdate(current):
		frappe.throw(frappe._("Night audit can only close the current business date "
		               "({0}).").format(current))
	# A day can't be closed before it has happened. Without this, repeated
	# clicks rolled the business date into the future and charged in-house
	# guests a room night (and no-shows a fee) per click.
	if getdate(business_date) > getdate(nowdate()):
		frappe.throw(frappe._("Business date {0} is already ahead of today ({1}). "
		               "Night audit for it can run once that day has "
		               "started.").format(business_date, nowdate()))

	# the checklist, as it stands before anything is posted
	from kamra import night_audit as na
	run_at = audit_clock(property)
	# guests still in past their departure, with na_auto_extend on, stay one
	# more night before the checklist and the posting run (so it is posted)
	extended = na.auto_extend_departures(property, business_date)
	checks = na.run_checks(property, business_date, run_at)
	blockers = na.open_blockers(checks)
	gl_blocker = next((c for c in blockers if c["key"] == "gl_control_tieout"), None)
	acks: list[dict] = []
	if mode == "manual":
		# acknowledged in the End of Day wizard (each with its own reason)
		raw_acks = (frappe.db.get_value("Night Audit Run", draft_name, "acknowledgements")
		            if draft_name else None)
		drafted = {a["key"]: a for a in na.draft_acknowledgements(property, checks, raw_acks)}
		keys = na.parse_keys(acknowledge)
		if gl_blocker and gl_blocker["key"] in keys and gl_blocker["key"] not in drafted:
			na.validate_gl_variance_override(property, gl_blocker, (note or "").strip())
		missing = [c for c in blockers
		           if c["key"] not in keys and c["key"] not in drafted]
		if missing:
			frappe.throw(
				frappe._("Night audit is blocked: {0}. Resolve them, or acknowledge "
				         "each with a reason to run anyway.").format(
					"; ".join(f"{c['title']} ({c['count']})" for c in missing)),
				title=frappe._("Night audit pre-checks"))
		fresh = [c for c in blockers if c["key"] not in drafted]
		if fresh and len((note or "").strip()) < 3:
			frappe.throw(frappe._("Give a reason for acknowledging the blocking "
			                      "checks."))
		acks = [drafted[c["key"]] for c in blockers if c["key"] in drafted] + \
			na.acknowledgement_rows(fresh, keys, (note or "").strip())
	elif mode == "scheduled" and blockers:
		from kamra.settings import get_setting
		if gl_blocker or get_setting(property, "na_auto_audit_with_blockers") == na.HOLD_AND_ALERT:
			out = na.hold_and_alert(property, business_date, blockers)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- scheduler job persists the hold alert per property; reviewed as intentional
			return out
		acks = na.acknowledgement_rows(
			blockers, [c["key"] for c in blockers],
			"Scheduled audit: run and reported as exceptions", by="Administrator",
			auto=True)
	elif gl_blocker:
		frappe.throw(frappe._("Accounting GL control tie-out must be cleared before closing."))

	log_lines = []
	folios_opened = charges_posted = no_shows = 0
	fixed_charges_posted = package_elements_posted = package_breakage_count = 0
	amount_posted = Decimal(0)
	fixed_charges_amount = Decimal(0)
	package_elements_amount = Decimal(0)
	package_breakage_total = Decimal(0)
	no_show_rows: list[dict] = []

	from kamra.fixed_charges import post_fixed_charges_for_date
	from kamra.packages import expire_daily_allowances

	in_house = frappe.qb.get_query(
		"Reservation",
		filters={
			"property": property,
			"status": "Checked In",
			"check_in_date": ("<=", business_date),
			"check_out_date": (">", business_date),
		},
		fields=["name"], order_by="name asc", for_update=True, ignore_permissions=True,
	).run(as_dict=True)
	for row in in_house:
		res = frappe.get_doc("Reservation", row.name)
		if not frappe.db.get_value(
				"Folio", {"reservation": res.name, "folio_type": "Guest"}):
			folio_name = open_folio(res)
			folios_opened += 1
			log_lines.append(f"opened folio {folio_name} for {res.name}")
		room_posted: list = []
		pkg_stats: dict = {}
		if post_room_night(res, business_date, room_posted=room_posted, package_stats=pkg_stats):
			charges_posted += 1
			# what the room line posted at (a Current Date night at today's rate), not the booked night
			amount_posted += sum((Decimal(str(amount)) for amount in room_posted), Decimal(0))
			log_lines.append(f"posted room night {business_date} for {res.name}")
		if pkg_stats.get("elements_posted"):
			package_elements_posted += int(pkg_stats["elements_posted"])
			package_elements_amount += Decimal(str(pkg_stats.get("elements_amount") or 0))
			addon_amt = Decimal(str(pkg_stats.get("addon_amount") or 0))
			if addon_amt:
				amount_posted += addon_amt
			log_lines.append(
				f"posted {pkg_stats['elements_posted']} package element(s) for {res.name}"
			)
		fc_res = post_fixed_charges_for_date(res, business_date, trigger="night_audit")
		if fc_res["count"]:
			fixed_charges_posted += int(fc_res["count"])
			fc_amt = Decimal(str(fc_res["amount"]))
			fixed_charges_amount += fc_amt
			amount_posted += fc_amt
			log_lines.append(f"posted {fc_res['count']} fixed charge(s) for {res.name}")
		brk_res = expire_daily_allowances(res, business_date)
		if brk_res["count"]:
			package_breakage_count += int(brk_res["count"])
			package_breakage_total += Decimal(str(brk_res["breakage_amount"]))

	# no-shows: closing day D ends D's arrivals too (Opera end of day), so
	# a confirmed stay due on or before the closed date that never checked
	# in is flagged AND charged per the property's policy - on the closed
	# date, like the room nights of the same run. The property's no-show
	# policy (kamra.no_show) can push that a day later, hold late arrivals
	# due on the closed date until the next audit, and waive the fee on OTA
	# bookings (the OTA collects under its own policy).
	from kamra import no_show
	pol = no_show.policy(property)
	cutoff = business_date if pol.timing != no_show.ONE_DAY_LATER \
		else str(add_days(business_date, -1))
	held_late: list[dict] = []
	fees_waived = 0
	stale = frappe.get_all(
		"Reservation",
		filters={
			"property": property,
			"status": "Confirmed",
			"check_in_date": ("<=", cutoff),
		},
		fields=["name"],
		order_by="check_in_date asc, name asc",
	)
	for row in stale:
		res = frappe.get_doc("Reservation", row.name)
		decision = no_show.audit_outcome(res, pol, business_date, run_at)
		if decision.outcome == "defer":
			continue
		if decision.outcome == "hold":
			held_late.append({"reservation": res.name,
			                  "guest_name": res.guest_name,
			                  "eta": res.get("eta") or None,
			                  "reason": decision.reason})
			log_lines.append(f"held late arrival: {res.name} ({decision.reason})")
			continue
		ns = apply_no_show(res, pol, decision, business_date)
		no_shows += 1
		no_show_rows.append(ns)
		log_lines.append(f"flagged no-show: {row.name}")
		if decision.fee == "waived":
			fees_waived += 1
			log_lines.append(
				f"no-show fee waived for {row.name}: {decision.reason}")
		if ns["fee_amount"]:
			from kamra.ledger import _currency_places
			fee_places = max(_currency_places(property), 2)
			amount_posted += Decimal(str(ns["fee_amount"]))
			log_lines.append(
				f"posted no-show charge {ns['fee_amount']:,.{fee_places}f} for {row.name}")

	# purge stale waitlist entries - two days after their requested departure
	from frappe.utils import add_days as _add_days
	purged = 0
	for wl in frappe.get_all("Reservation", filters={
			"property": property, "status": "Waitlist",
			"check_out_date": ("<=", _add_days(business_date, -2))}, pluck="name"):
		frappe.delete_doc("Reservation", wl, ignore_permissions=True, force=True)
		purged += 1
	if purged:
		log_lines.append(f"purged {purged} stale waitlist entries")

	for a in acks:
		log_lines.append(f"acknowledged {a['key']} ({a['count']}) by {a['by']}: "
		                 f"{a['note']}")
	for x in extended:
		log_lines.append(f"extended {x['reservation']} one night" if x["extended"]
		                 else f"could not extend {x['reservation']}: {x.get('error')}")
	unacked = [c for c in blockers if c["key"] not in {a["key"] for a in acks}]
	for c in unacked:
		log_lines.append(f"exception: {c['title']} ({c['count']})")
	trigger = {"manual": "Manual", "scheduled": "Scheduled"}.get(mode or "", "System")
	summary = {
		"room_charges_posted": charges_posted,
		"fixed_charges_posted": fixed_charges_posted,
		"fixed_charges_amount": float(fixed_charges_amount),
		"package_elements_posted": package_elements_posted,
		"package_elements_amount": float(package_elements_amount),
		"package_breakage_count": package_breakage_count,
		"package_breakage_total": float(package_breakage_total),
		"amount_posted": float(amount_posted),
		"no_shows_flagged": no_shows,
		"late_arrivals_held": len(held_late),
		"no_show_fees_waived": fees_waived,
		"folios_opened": folios_opened,
		"stays_extended": sum(1 for x in extended if x["extended"]),
		"trigger": trigger,
		"run_by": frappe.session.user,
		"exceptions": len(blockers),
		"notes": (frappe.db.get_value("Night Audit Run", draft_name,
		                              "auditor_notes") if draft_name else None),
	}
	report_pack = None
	for _ in range(2):  # one retry
		try:
			pack = na.build_pack(property, business_date, checks=checks,
			                     acknowledgements=acks, summary=summary,
			                     no_shows=no_show_rows, held_late=held_late)
			report_pack = json.dumps(pack, default=str)
			break
		except Exception:
			frappe.log_error(title=f"Night audit pack failed: {property}")
			report_pack = None

	values = {
		"status": "Completed with Warnings" if blockers else "Completed",
		"room_charges_posted": charges_posted,
		"amount_posted": float(amount_posted),
		"no_shows_flagged": no_shows,
		"late_arrivals_held": len(held_late),
		"no_show_fees_waived": fees_waived,
		"folios_opened": folios_opened,
		"run_trigger": trigger,
		"run_by": frappe.session.user,
		"exceptions": len(blockers),
		"acknowledgements": json.dumps(acks) if acks else None,
		"report_pack": report_pack,
		"log": "\n".join(log_lines) or "Nothing to do.",
		"eod_step": 11,
	}
	if draft_name:
		# the wizard's draft becomes the run: its notes and progress stay
		audit = frappe.get_doc("Night Audit Run", draft_name)
		audit.update(values)
		audit.save(ignore_permissions=True)
	else:
		audit = frappe.get_doc({"doctype": "Night Audit Run",
		                        "property": property,
		                        "business_date": business_date, **values})
		audit.insert(ignore_permissions=True)

	# Advance the front-office business date (Opera End of Day roll).
	from kamra.business_date import advance_business_date, ensure_business_date
	ensure_business_date(property)
	new_bd = advance_business_date(property, business_date)
	# reports scheduled "After night audit" go out once this commits
	frappe.enqueue("kamra.custom_reports.send_after_night_audit",
	               property=property, business_date=business_date,
	               enqueue_after_commit=True)

	from kamra.savings import log_action
	log_action(
		action_type="night_audit",
		reference_doctype="Night Audit Run",
		reference_name=audit.name,
		property=property,
		minutes_saved=90,
		rationale=f"Posted {charges_posted} room nights, flagged {no_shows} "
		          f"no-shows, held {len(held_late)} late arrivals for "
		          f"{business_date}; business date → {new_bd}",
		agent_name="Night Audit",
		autonomy="Full",
		channel="API",
	)
	if acks and mode == "manual":
		log_action(
			action_type="night_audit_acknowledged",
			reference_doctype="Night Audit Run",
			reference_name=audit.name,
			property=property,
			rationale="Blocking checks acknowledged: "
			          + "; ".join(f"{a['title']} ({a['count']})" for a in acks)
			          + f" - {acks[0]['note']}",
			channel="MCP" if frappe.flags.get("kamra_agent_call") else "Desk",
			after_snapshot=acks,
		)
	from kamra.webhooks import emit_night_audit_closed
	emit_night_audit_closed(property, {
		"audit": audit.name, "status": audit.status,
		"business_date": business_date, "next_business_date": new_bd,
		"room_charges_posted": charges_posted,
		"amount_posted": float(amount_posted), "no_shows_flagged": no_shows,
		"exceptions": len(blockers)})
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persists the completed operation before returning to an external/public caller; reviewed as intentional
	return {
		"audit": audit.name,
		"status": audit.status,
		"exceptions": len(blockers),
		"acknowledged": [a["key"] for a in acks],
		"has_pack": bool(report_pack),
		"room_charges_posted": charges_posted,
		"fixed_charges_posted": fixed_charges_posted,
		"fixed_charges_amount": float(fixed_charges_amount),
		"package_elements_posted": package_elements_posted,
		"package_elements_amount": float(package_elements_amount),
		"package_breakage_count": package_breakage_count,
		"package_breakage_total": float(package_breakage_total),
		"amount_posted": float(amount_posted),
		"no_shows_flagged": no_shows,
		"late_arrivals_held": len(held_late),
		"held_late_arrivals": held_late,
		"no_show_fees_waived": fees_waived,
		"folios_opened": folios_opened,
		"business_date": business_date,
		"next_business_date": new_bd,
	}


# The scheduler closes at most this many overdue days on its own (the
# property's night_audit_catch_up_days, else this). A property further
# behind gets an alert instead: weeks of unattended room nights and no-show
# fees need a person to check the stays first.
NIGHTLY_CATCH_UP_DAYS = 3


def _days(n: int) -> str:
	return f"{n} day" if n == 1 else f"{n} days"


def _catch_up_limit(property: str) -> int | None:
	"""The property's stored night_audit_catch_up_days, NIGHTLY_CATCH_UP_DAYS
	when unset or empty, and None when the stored value is not a whole
	number from 1 to 7 (a row written around save_settings): the caller
	then stops instead of guessing a limit."""
	from kamra.settings import SETTINGS, read_kv
	raw = read_kv(property, "night_audit_catch_up_days")
	if raw is None or raw == "":
		return NIGHTLY_CATCH_UP_DAYS
	spec = SETTINGS["night_audit_catch_up_days"][1]
	if isinstance(raw, bool) or not isinstance(raw, int) or not spec.min <= raw <= spec.max:
		return None
	return raw


def audit_due_at(property: str, day):
	"""When the automatic audit is due on `day`, on the property's clock:
	night_audit_time plus the grace minutes."""
	from datetime import datetime, timedelta

	from kamra.settings import get_setting
	at = datetime.strptime(get_setting(property, "night_audit_time"), "%H:%M").time()
	grace = int(get_setting(property, "night_audit_grace_minutes") or 0)
	return datetime.combine(getdate(day), at) + timedelta(minutes=grace)


def scheduled_attempt_made(property: str, business_date, local_now, due) -> bool:
	"""The scheduler already tried this business date since today's due
	time and did not close it (held, skipped or failed). It then waits for
	tomorrow or for staff, instead of retrying and alerting every tick."""
	since = now_datetime() - (local_now - due)
	return bool(frappe.db.exists("Night Audit Log", {
		"property": property, "run_trigger": "Scheduled",
		"business_date": business_date, "status": ("!=", "Closed"),
		"started_at": (">=", since)}))


def nightly_audit_all_properties():
	"""Scheduler entry point, every 15 minutes. For each active property
	with night_audit_auto on, once its local clock is past night_audit_time
	(+ grace) and its business date is before the local calendar date,
	closes ONE business date. A property several days behind catches up
	one day per tick. Each property runs on its own; one failing never
	stops the rest."""
	from kamra.night_audit import record_run
	from kamra.settings import get_setting
	for p in frappe.get_all("Property", filters={"disabled": 0}, pluck="name"):
		try:
			if not get_setting(p, "night_audit_auto"):
				continue
			local_now = audit_clock(p)
			today = local_now.date()
			due = audit_due_at(p, today)
			if local_now < due:
				continue
			bd = frappe.db.get_value("Property", p, "business_date")
			if bd and getdate(bd) >= today:
				continue
			if bd and scheduled_attempt_made(p, bd, local_now, due):
				continue
			# a fresh snapshot, then a row lock on the property: a second
			# worker waits here and then sees the first one's date roll
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- releases the read snapshot before the per-property lock
			frappe.db.sql("SELECT name FROM `tabProperty` WHERE name = %s FOR UPDATE", p)
			record_run(p, "Scheduled", lambda p=p, today=today: _nightly_audit_property(p, today))
		except Exception:
			frappe.log_error(title=f"Night audit failed: {p}")


def _nightly_audit_property(property: str, today) -> dict | None:
	"""One scheduled attempt: None when there is nothing to do, else what
	happened (a run_night_audit result, or a skip with its reason)."""
	from frappe.utils import date_diff

	from kamra import night_audit as na
	from kamra.business_date import advance_business_date, ensure_business_date

	log = frappe.logger("kamra")
	bd = getdate(ensure_business_date(property))
	if bd >= today:
		log.info(f"Night audit skipped for {property}: business date {bd} "
		         f"is not before today ({today})")
		return None
	# A completed run for the open date whose roll did not stick. Without
	# this every night answered already_ran and the date never moved again.
	# Advance it like recover_stuck_business_date does; nothing is re-posted.
	limit = _catch_up_limit(property)
	if limit is None:
		name = frappe.db.get_value("Property", property, "property_name") or property
		msg = (f"Catch up at most (night_audit_catch_up_days) is not a whole number "
		       f"from 1 to 7 in Property Setting {property}::night_audit_catch_up_days. "
		       f"The scheduled night audit is stopped until it is fixed under Settings "
		       f"→ Front Desk → Automatic night audit.")
		log.warning(f"Night audit not run for {property}: {msg}")
		na.alert_staff(property, ("Hotel Admin",),
		               f"Night audit stopped - {name}", f"{name}: {msg}")
		return {"skipped": True, "business_date": str(bd), "message": msg}
	stuck = na.stuck_business_date(property)
	if stuck:
		bd = getdate(advance_business_date(property, str(bd)))
		msg = (f"Business date was stuck on {stuck['business_date']} "
		       f"(audit {stuck['audit']}), moved to {bd}")
		log.warning(f"Night audit for {property}: {msg}")
		if bd >= today:
			return {"skipped": True, "business_date": stuck["business_date"],
			        "message": msg}
	behind = date_diff(today, bd)
	if behind > limit:
		name = frappe.db.get_value("Property", property, "property_name") or property
		msg = (f"The business date is {bd}, {_days(behind)} behind today. The "
		       f"scheduled night audit only catches up {_days(limit)} on its own. "
		       f"Check in-house stays and arrivals, then close the open days from "
		       f"End of Day.")
		log.warning(f"Night audit not run for {property}: {msg}")
		na.alert_staff(property, ("Hotel Admin", "Finance"),
		               f"Night audit behind - {name}", f"{name}: {msg}")
		return {"skipped": True, "business_date": str(bd), "message": msg}
	return run_night_audit(property, mode="scheduled")
