# Copyright (c) 2026, Kamra and contributors
# For license information, please see license.txt

from __future__ import annotations

import hashlib

import frappe
from frappe import _

from kamra.masters import MasterCode


def payment_method_name(code: str, property: str | None = None) -> str:
	"""Primary key for Payment Method: shared rows keep `code`; property rows
	use `<property>::<code>` (bounded to Frappe's 140-char name column)."""
	code = (code or "").strip()
	prop = (property or "").strip()
	if not prop:
		return code
	candidate = f"{prop}::{code}"
	if len(candidate) <= 140:
		return candidate
	digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:12]
	prefix_len = max(1, 140 - len(code) - 16)
	return f"{prop[:prefix_len]}::{code}::{digest}"[:140]


def _is_property_scoped_user() -> bool:
	user = getattr(frappe.session, "user", None)
	if not user or user in ("Administrator", "Guest"):
		return False
	return bool(frappe.db.exists("User Permission", {"user": user, "allow": "Property"}))


class PaymentMethod(MasterCode):
	def autoname(self):
		self.code = (self.code or "").strip()
		if not self.code:
			frappe.throw(_("Give the code."))
		if self.is_new() and not self.flags.get("kamra_seeding"):
			self.system = 0
		if self.system:
			self.property = None
		self.property = (self.property or "").strip() or None
		self.name = payment_method_name(self.code, self.property)

	def _guard_shared_mutation(self):
		if self.flags.get("kamra_seeding"):
			return
		prop = (self.property or "").strip() or None
		before = None if self.is_new() else self.get_doc_before_save()
		before_prop = ((before.property or "").strip() or None) if before else prop
		if (not prop or (before is not None and not before_prop)) and _is_property_scoped_user():
			frappe.throw(
				_("Shared payment methods cannot be changed by a property-scoped user. "
				  "Create a property override instead."),
				frappe.PermissionError,
			)

	def validate(self):
		self._guard_shared_mutation()
		super().validate()
		if self.system:
			self.property = None
		prop = (self.property or "").strip() or None
		self.property = prop
		existing = frappe.db.sql(
			"""SELECT name FROM `tabPayment Method`
			   WHERE code = %s AND COALESCE(property, '') = %s AND name != %s
			   LIMIT 1""",
			(self.code, prop or "", self.name or ""),
		)
		if existing:
			scope = prop or _("shared")
			frappe.throw(
				_("Payment Method {0} already exists for {1}.").format(self.code, scope),
				frappe.DuplicateEntryError,
			)

	def on_trash(self):
		if not (self.property or "").strip() and _is_property_scoped_user():
			frappe.throw(
				_("Shared payment methods cannot be deleted by a property-scoped user."),
				frappe.PermissionError,
			)
		super().on_trash()
