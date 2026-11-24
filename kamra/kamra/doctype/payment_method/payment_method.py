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

	def validate(self):
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
