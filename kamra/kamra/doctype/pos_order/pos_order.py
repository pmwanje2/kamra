# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from kamra.localization import money_text


def _folio_charge_description(outlet_label: str, detail: str, order_name: str,
                              limit: int = 140) -> str:
	"""Format a POS room-charge description within Folio Charge's 140-char limit
	while always keeping the trailing ` (ORD-...)` marker so moves and audit
	reports recognize the order."""
	suffix = f" ({order_name})"
	prefix = f"{outlet_label}: " if outlet_label else ""
	full = f"{prefix}{detail}{suffix}"
	if len(full) <= limit:
		return full
	room = limit - len(prefix) - len(suffix)
	if room <= 1:
		return (f"{prefix}{detail}"[:max(0, limit - len(suffix))] + suffix)[:limit]
	return f"{prefix}{detail[:room - 1].rstrip(' ,')}…{suffix}"


def _code_gst_rate(property: str, code: str | None, *, is_alcohol: bool,
                   outlet_gst: float) -> float:
	"""OPERA Cloud taxes by transaction code (Generates):
	https://docs.oracle.com/en/industries/hospitality/opera-cloud/25.5/ocsuh/t_admin_financial_transaction_management_configuring_generates.htm
	1. Explicit Transaction Code.default_tax_rate wins when configured.
	2. Otherwise an alcohol code uses the country pack's alcohol/services rate
	   when the pack distinguishes it from food (e.g. India 18% vs 5% F&B).
	3. Otherwise fall back to the outlet's configured gst_rate."""
	from kamra.ledger import code_default_tax_rate
	tc_rate = code_default_tax_rate(code)
	if tc_rate is not None:
		return tc_rate
	if is_alcohol:
		from kamra.localization import pack_for, services_tax_rate
		pack = pack_for(property)
		alc_fn = getattr(pack, "alcohol_tax_rate", None)
		if alc_fn is not None:
			return float(alc_fn(property))
		if hasattr(pack, "services_tax_rate"):
			return float(services_tax_rate(pack, property))
	return float(outlet_gst)


class POSOrder(Document):
	def validate(self):
		subtotal = 0.0
		for it in self.items:
			it.amount = 0.0 if it.voided else float(it.qty or 1) * float(it.rate or 0)
			subtotal += it.amount
		self.subtotal = subtotal
		discount = min(float(self.discount_amount or 0), subtotal)
		self.discount_amount = discount
		# NC (no charge): the items stand for the kitchen and the audit,
		# but the bill is zero
		self.order_total = 0.0 if self.nc else subtotal - discount
		self.guard_room_posting()
		self.guard_bill_close()
		self.stamp_cashier()

	def stamp_cashier(self):
		"""The cashier on the bill: whoever closes it (pay_order, a room
		posting or an NC close all land here). Set once, on the close, and
		only here: a value sent by a client is put back. A user with no till
		role (Guest, Administrator, an integration account) leaves it blank."""
		previous = self.get_doc_before_save()
		self.settled_by = previous.settled_by if previous else None
		if self.status != "Delivered" or self.settled_by or \
				(previous and previous.status == "Delivered"):
			return
		from kamra.authz import ADMIN
		from kamra.pos import POS_ROLES
		user = frappe.session.user
		if user not in ("Guest", "Administrator") and \
				set(POS_ROLES + ADMIN) & set(frappe.get_roles(user)):
			self.settled_by = user

	def charges_to_room(self) -> bool:
		"""The bill is meant for a guest's room: room service, or any order
		(a dine-in one too) tied to a room or an in-house stay."""
		return bool(self.room or self.reservation
		            or self.order_type == "Room Service")

	def guard_room_posting(self):
		"""A room bill closes (Delivered) only two ways: it posts to a
		checked-in stay's folio, or it was paid at the outlet. Anything else
		was food served with nothing billed and nothing paid."""
		if self.status != "Delivered" or self.paid or self.nc \
		   or self.posted_to_folio or not self.charges_to_room():
			return
		previous = self.get_doc_before_save()
		if previous and previous.status == "Delivered":
			return
		if self.room and not self.reservation:
			# the guest may have checked in since the order was taken
			self.reservation = frappe.db.get_value(
				"Reservation", {"room": self.room, "status": "Checked In",
				                "property": self.property}, "name")
		status = self.reservation and frappe.db.get_value(
			"Reservation", self.reservation, "status")
		if status != "Checked In":
			where = (_("Room {0}").format(frappe.db.get_value(
				"Room", self.room, "room_number") or self.room)
				if self.room else _("this bill"))
			frappe.throw(_(
				"{0} has no checked-in guest to post to. Take payment at the "
				"outlet to close it.").format(where))

	def guard_bill_close(self):
		"""Delivered means the BILL is closed, not that the food left the
		pass (the kitchen tracks that per line: kot_status Prepared). A bill
		closes only when it is settled one of three ways:

		- paid at the outlet (pay_order: Cash / Card / the country's instant modes),
		- signed to a checked-in guest's room (guard_room_posting checks the
		  stay; on_update posts it to the folio),
		- complimentary (NC) authorized through mark_nc.

		A bill with nothing left to charge (every line voided) also closes.
		Anything else was food served with nothing billed and nothing paid."""
		if self.status != "Delivered":
			return
		previous = self.get_doc_before_save()
		if previous and previous.status == "Delivered":
			return
		if self.paid:
			if not self.payment_mode:
				frappe.throw(_("A paid bill needs its payment mode."))
			return
		if self.nc:
			if not (self.nc_authorized_by or "").strip():
				frappe.throw(_("An NC (complimentary) bill needs who "
				              "authorized it before it can close."))
			return
		if self.posted_to_folio or self.charges_to_room():
			# room bills: guard_room_posting has already insisted on a
			# checked-in stay; on_update posts the bill to its folio
			return
		if not any(not it.voided for it in self.items):
			return
		frappe.throw(_(
			"This bill is not settled. Take payment at the outlet, "
			"charge it to an in-house guest's room, or mark it NC with "
			"an authorization before closing it."))

	def on_update(self):
		previous = self.get_doc_before_save()
		old_status = previous.status if previous else None
		if self.status != "Delivered" or old_status == "Delivered":
			return
		from kamra.business_date import get_business_date
		self.db_set("business_date", get_business_date(self.property), update_modified=False)
		if self.posted_to_folio or not self.reservation:
			return
		# settled at the outlet (cash, card...) - never also post to the room
		if self.paid:
			return
		# complimentary - there is nothing to charge anywhere
		if self.nc:
			return
		# room-service charge routed whole per transaction code by the stay's
		# routing instructions (with its amount, so a limit applies) or its
		# billing rules. Grouping by transaction code follows Simphony-OPERA:
		# https://docs.oracle.com/en/industries/hospitality/simphony/19.7/simcg/c_opera_connection.htm
		from decimal import ROUND_HALF_UP, Decimal

		from frappe.utils import cint

		from kamra.folio import _recalculate, routed_figure, routed_target
		from kamra.ledger import (
			beverage_transaction_code,
			code_for_charge_type,
			minor_unit,
			save_folio,
		)

		res = frappe.get_doc("Reservation", self.reservation)
		bd = get_business_date(self.property)
		# the outlet's own rate, 0 included (the field defaults new outlets to 5;
		# `or 5` here charged tax at a 0% outlet)
		outlet_doc = frappe.db.get_value(
			"POS Outlet", self.outlet, ["gst_rate", "transaction_code"], as_dict=True
		) or frappe._dict()
		gst = 5 if outlet_doc.gst_rate is None else outlet_doc.gst_rate
		outlet_code = (outlet_doc.transaction_code or "").strip() or None
		outlet_code_alc = bool(
			outlet_code and cint(frappe.db.get_value("Transaction Code", outlet_code, "is_alcohol"))
		)

		active_items = [it for it in self.items if not it.voided]
		mi_ids = list({it.menu_item for it in active_items if it.menu_item})
		mi_map = {
			m.name: m
			for m in frappe.get_all(
				"Menu Item",
				filters={"name": ["in", mi_ids]},
				fields=["name", "is_alcohol", "transaction_code"],
			)
		} if mi_ids else {}

		default_fnb = code_for_charge_type("Food & Beverage")
		default_bev = beverage_transaction_code(self.property)
		buckets: dict[str, list] = {}
		for it in active_items:
			mi = mi_map.get(it.menu_item) if it.menu_item else None
			item_code = ((mi.transaction_code or "").strip() if mi else "") or None
			item_alc = bool(cint(mi.is_alcohol)) if mi else False
			if item_code:
				code = item_code
			elif item_alc:
				code = outlet_code if (outlet_code and outlet_code_alc) else default_bev
			elif outlet_code:
				code = outlet_code
			else:
				code = default_fnb
			buckets.setdefault(code, []).append((it, item_alc))

		if not buckets:
			buckets[outlet_code or default_fnb] = []

		bucket_rows: list[tuple[str, list, float]] = []
		if len(buckets) == 1:
			code, entries = next(iter(buckets.items()))
			bucket_rows.append((code, entries, float(self.order_total or 0)))
		else:
			total_dec = Decimal(str(self.order_total or 0))
			gross_by_code = [
				(code, entries, sum((Decimal(str(it.amount or 0)) for it, _ in entries), Decimal(0)))
				for code, entries in buckets.items()
			]
			subtotal_dec = sum((g for _, _, g in gross_by_code), Decimal(0))
			if not float(self.discount_amount or 0) or subtotal_dec <= 0:
				bucket_rows = [(code, entries, float(g)) for code, entries, g in gross_by_code]
			else:
				unit = minor_unit(self.property)
				allocated = Decimal(0)
				for idx, (code, entries, g) in enumerate(gross_by_code):
					if idx == len(gross_by_code) - 1:
						share = total_dec - allocated
					else:
						share = (total_dec * g / subtotal_dec).quantize(unit, rounding=ROUND_HALF_UP)
						allocated += share
					bucket_rows.append((code, entries, float(share)))

		folios: dict[str, Document] = {}
		pending: list[tuple] = []
		appended: list[tuple[str, int]] = []
		for code, entries, amt in bucket_rows:
			has_alcohol = 1 if (
				any(alc for _, alc in entries)
				or cint(frappe.db.get_value("Transaction Code", code, "is_alcohol"))
			) else 0
			bucket_gst = _code_gst_rate(
				self.property, code, is_alcohol=bool(has_alcohol), outlet_gst=float(gst))
			target, instruction = routed_target(
				res, "Food & Beverage", amount=amt,
				posting_date=bd, transaction_code=code, pending=pending)
			if target not in folios:
				fdoc = frappe.get_doc("Folio", target)
				if fdoc.status == "Closed":
					frappe.throw("Folio is closed — settle the order directly.")
				folios[target] = fdoc
			fdoc = folios[target]
			b_detail = ", ".join(f"{it.item_name} ×{int(it.qty)}" for it, _ in entries)
			fdoc.append("charges", {
				"posting_date": bd,
				"charge_type": "Food & Beverage",
				"transaction_code": code,
				"reservation": self.reservation,
				"description": _folio_charge_description(
					self.outlet.split("-")[-1], b_detail, self.name),
				"qty": 1,
				"rate": amt,
				"amount": amt,
				"gst_rate": bucket_gst,
				"auto_posted": 1,
				"is_alcohol": has_alcohol,
				"routing_instruction": instruction,
			})
			appended.append((target, len(fdoc.charges) - 1))
			if instruction:
				pending.append((instruction, bd, routed_figure(
					self.property, "Food & Beverage", amt, amt * bucket_gst / 100)))

		for fdoc in folios.values():
			_recalculate(fdoc)
			save_folio(fdoc)
		self.db_set("posted_to_folio", 1, update_modified=False)
		from kamra.webhooks import emit_folio_posted
		from kamra.whatsapp import queue_room_charge_alert
		for target, idx in appended:
			fdoc = folios[target]
			row = fdoc.charges[idx]
			emit_folio_posted(fdoc, row)
			queue_room_charge_alert(fdoc, row)

		detail = ", ".join(
			f"{it.item_name} ×{int(it.qty)}" for it in active_items)
		targets_str = ", ".join(folios.keys())
		from kamra.savings import log_action
		log_action("post_pos_order", "POS Order", self.name, self.property,
		           minutes_saved=5 if self.source in ("AI Agent", "QR") else 0,
		           rationale=f"{money_text(self.property, round(self.order_total or 0))} {detail} → {targets_str}",
		           channel="API")
