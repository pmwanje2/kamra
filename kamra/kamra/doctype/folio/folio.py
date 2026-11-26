# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


def charges_frozen(folio):
	return folio and (folio.status == "Closed" or folio.invoice_number or folio.get("sales_invoice"))


class Folio(Document):
	def load_from_db(self):
		# Frappe also requests a locked document for its before-save snapshot.
		if not self.flags.for_update or frappe.db.db_type != "mariadb":
			return super().load_from_db()
		from kamra.folio import advance_share_targets, lock_folio_parent
		# Resolve filters the same way as Document.load_from_db, then lock just that parent.
		if not isinstance(self.name, (str, int)):
			name = frappe.db.get_value(self.doctype, self.name, "name")
			if name is None:
				frappe.throw(
					_("Folio {0} not found.").format(self.name),
					frappe.DoesNotExistError(doctype=self.doctype),
				)
			self.name = name
		try:
			lock_folio_parent(self.name)
		except frappe.TimestampMismatchError:
			if self.name not in (advance_share_targets.get() or ()):
				raise
			return self._load_committed_folio()
		self.flags.for_update = False
		try:
			return super().load_from_db()
		finally:
			self.flags.for_update = True

	def _load_committed_folio(self):
		"""Read the newly committed document while the writer holds its primary-key lock."""
		from frappe.database import get_db

		writer = frappe.local.db
		reader = get_db(socket=writer.socket, host=writer.host, port=writer.port, user=writer.user,
		                password=writer.password, cur_db_name=writer.cur_db_name)
		try:
			# A second read view sees the concurrent join, including its child rows.
			# Native folio saves serialize on the parent held by the original transaction.
			reader.sql("START TRANSACTION READ ONLY")
			frappe.local.db = reader
			self.flags.for_update = False
			return super().load_from_db()
		finally:
			self.flags.for_update = True
			frappe.local.db = writer
			reader.close()

	def update_child_table(self, fieldname, df=None):
		# Frappe's delete-by-parent also gap-locks an empty child list. When
		# every previous row is retained, only the normal upserts are needed.
		previous = self.get_doc_before_save()
		if (previous and df and not df.is_virtual and fieldname in ("charges", "payments")
		    and frappe.db.db_type == "mariadb"):
			rows = self.get(fieldname)
			retained = {row.name for row in rows if not row.is_new()}
			if all(row.name in retained for row in previous.get(fieldname)):
				ignored = self.flags.ignore_children_type
				self.flags.ignore_children_type = (*(ignored or ()), df.options)
				try:
					return super().update_child_table(fieldname, df)
				finally:
					self.flags.ignore_children_type = ignored
		return super().update_child_table(fieldname, df)

	def save(self, *args, **kwargs):
		# Native cancellations append their refund before its ledger reversal.
		# Document.save locks this folio during its timestamp check.
		if self.reservation and self.folio_type == "Guest" and any(
			p.payment_kind == "Refund" and p.is_new() for p in self.payments or ()
		):
			from kamra.folio import lock_guest_deposit_windows
			lock_guest_deposit_windows(self.reservation)
		return super().save(*args, **kwargs)

	def precision(self, fieldname, parentfield=None):
		if self.property and (
			fieldname in {"grand_total", "charges_total", "tax_total", "payments_total", "balance"}
			or (parentfield == "payments" and fieldname == "amount")
		):
			from kamra.ledger import _currency_places

			return _currency_places(self.property)
		return super().precision(fieldname, parentfield)

	def validate(self):
		self.guard_closed()
		self.validate_window()
		self.validate_payment_modes()
		self.stamp_payment_currency()
		from kamra.ledger import stamp_gl_codes
		stamp_gl_codes(self)
		from kamra.room_statistics import stamp_room_rate_category
		stamp_room_rate_category(self)

	def validate_payment_modes(self):
		from kamra.masters import assert_known_payment_method
		for p in self.payments or ():
			if p.mode:
				assert_known_payment_method(self.property, p.mode)

	def stamp_payment_currency(self):
		"""Every payment row carries the property's currency unless a writer
		set one (FX rows do). One guard here covers all writers (#341)."""
		missing = [p for p in self.payments if not p.currency]
		if missing:
			from kamra.localization import default_currency, pack_for
			cur = (frappe.get_cached_value("Property", self.property, "currency")
			       or default_currency(pack_for(self.property)))
			for p in missing:
				p.currency = cur

	def guard_closed(self):
		"""Both locally issued and bridge-issued invoices freeze their charges:
		no reopening, no renumbering, no touching charge lines — only
		settling payments may still be recorded. Adjustments belong on a
		new folio (credit note territory), not inside an issued invoice.

		The one sanctioned way through: kamra.folio.cancel_invoice, which
		first books the number into the Cancelled Invoice register (the
		sequence never loses a bill) and sets this flag for its own save."""
		if getattr(frappe.flags, "kamra_invoice_cancel", False):
			return
		if self.is_new():
			return
		old = self.get_doc_before_save()
		if not charges_frozen(old):
			return
		if old.status == "Closed" and (self.status != "Closed" or self.invoice_number != old.invoice_number):
			frappe.throw(_("A closed folio cannot be reopened or renumbered."))

		# Include row identity, order and all persisted fields, including bridge
		# custom fields. Frappe refreshes the modification stamps on every save.
		def snapshot(row):
			values = row if isinstance(row, dict) else row.as_dict(convert_dates_to_str=True)
			return {key: round(value, 9) if isinstance(value, float) else value
			        for key, value in values.items() if key not in ("modified", "modified_by")}

		if [snapshot(row) for row in self.charges] != [snapshot(row) for row in old.charges]:
			if not old.invoice_number:
				frappe.throw(_("This folio has been invoiced. Post charges and adjustments on a new billing window."))
			frappe.throw(_(
				"Invoice {0} is issued — its charges are frozen. Post "
				"adjustments on a new folio.").format(old.invoice_number))
		if old.status == "Closed" and len(self.payments) < len(old.payments):
			frappe.throw(_("Payments cannot be removed from a closed folio."))

	def validate_window(self):
		"""A stay's or a posting master's folio is one billing window (OPERA
		1-8): numbered within the property's folio_windows, one open folio
		per window, and a payee that matches its type. A posting master
		(Group) belongs to a group at the same property. An Account invoice
		has no window."""
		if self.folio_type == "Account" or not (self.reservation or self.group_booking):
			return
		from kamra.folio import _window_key, next_window, window_cap
		if self.folio_type == "Group":
			if not self.group_booking:
				frappe.throw(_("A group's master bill needs its Group Booking."))
			if frappe.db.get_value("Group Booking", self.group_booking, "property") != self.property:
				frappe.throw(_("A group's master bill must be at the group's property."))
			key = _window_key(group_booking=self.group_booking)
		else:
			key = _window_key(reservation=self.reservation)
		if self.is_new():
			if not self.window_no:
				self.window_no = 1 if self.folio_type == "Guest" else next_window(
					key, self.property, start=1 if self.folio_type == "Group" else 2)
			if not self.payee_type:
				self.set_default_payee()
		before = None if self.is_new() else self.get_doc_before_save()
		if self.is_new() or (before and before.window_no != self.window_no):
			cap = window_cap(self.property)
			if not 1 <= int(self.window_no or 0) <= cap:
				frappe.throw(_("Window must be from 1 to {0}.").format(cap))
		if self.window_no and self.status == "Open" and frappe.db.exists("Folio", {
				**key, "window_no": self.window_no, "status": "Open",
				"name": ("!=", self.name or "")}):
			frappe.throw(_("Window {0} already has an open bill.").format(self.window_no))
		self.validate_payee()
		if self.payment_method and (self.is_new() or self.has_value_changed("payment_method")):
			from kamra.masters import assert_folio_payment_method
			assert_folio_payment_method(self.property, self.payment_method)

	def set_default_payee(self):
		"""A folio a legacy writer opened: its payee from its kind."""
		company = None
		if self.folio_type == "Company" and self.reservation:
			company = frappe.db.get_value("Reservation", self.reservation, "company")
		elif self.folio_type == "Group":
			company = frappe.db.get_value("Group Booking", self.group_booking, "company")
		self.payee_type = "Company" if company else "Guest"
		self.payee_company = company
		if company and not self.payment_method and frappe.db.get_value(
				"Corporate Account", company, "credit_allowed"):
			from kamra.folio import DIRECT_BILL
			self.payment_method = DIRECT_BILL

	def validate_payee(self):
		if not self.payee_type:
			return  # a folio from before windows, until the backfill names its payee
		if self.payee_type == "Company":
			if not self.payee_company or self.payee_agent:
				frappe.throw(_("A company window names the paying company, and only that."))
		elif self.payee_type == "Travel Agent":
			if not self.payee_agent or self.payee_company:
				frappe.throw(_("A travel agent window names the paying agent, and only that."))
		elif self.payee_company or self.payee_agent:
			frappe.throw(_("A guest window names no company or agent."))
