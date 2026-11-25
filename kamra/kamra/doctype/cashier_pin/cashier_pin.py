# Copyright (c) 2026, Kamra and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class CashierPIN(Document):
	def validate(self):
		# SiteOps v7 writes the shared record through the document API.
		# Convert a newly supplied PIN before Frappe's Password field writer
		# can persist it. A loaded Password field contains only a mask.
		if not self.pin or set(self.pin) == {"*"}:
			return
		from frappe.utils.password import remove_encrypted_password

		from kamra.authz import (
			PIN_LOCK_MINUTES,
			PIN_MAX_ATTEMPTS,
			_record_pin_attempt,
			_revoke_horizontal_floor_pin,
			_scrub_pin_from_form_dict,
			hash_cashier_pin,
			normalize_approval_pin,
			pin_password_locked_until,
			require_pin_identity,
			verify_pin_account_password,
		)

		pin = self.pin
		request = getattr(frappe.local, "request", None)
		if request is not None:
			form = getattr(frappe.local, "form_dict", None) or {}
			password = form.get("current_password") if "current_password" in form else form.get("password")
			_scrub_pin_from_form_dict()
			actor = require_pin_identity(for_update=True)
			if actor != self.user:
				frappe.throw(frappe._("This account cannot manage approval PINs."), frappe.PermissionError)
			# The verified SiteOps setter scrubs the password itself and
			# locks this User before invoking the document writer.
			siteops_setter = "siteops_doa.set_my_floor_pin"
			if form.get("cmd") == siteops_setter and request.path in (
				f"/api/method/{siteops_setter}", f"/api/v1/method/{siteops_setter}",
			):
				if pin_password_locked_until(actor, fresh=True):
					frappe.throw(frappe._("Too many incorrect passwords. Try again later."))
			else:
				verify_pin_account_password(actor, password)
			pin = normalize_approval_pin(pin)
			_revoke_horizontal_floor_pin(actor)
			_record_pin_attempt(actor, "pin", matched=True, limit=PIN_MAX_ATTEMPTS, minutes=PIN_LOCK_MINUTES)
			frappe.cache.delete_value(f"kamra_cashier_unlock:{actor}")
		self.pin_hash = hash_cashier_pin(pin)
		self.pin = None
		if not self.is_new():
			remove_encrypted_password("Cashier PIN", self.name, "pin")

	def get_password(self, fieldname="password", raise_exception=True):
		if fieldname != "pin":
			return super().get_password(fieldname, raise_exception=raise_exception)

		from kamra.authz import DOA_FLOOR_VERIFY_METHOD, _check_user_pin, _protect_pin, _scrub_pin_from_form_dict

		# SiteOps v6/v7 asks the document for its PIN only after validating
		# company, approver eligibility, separation of duties and nonce.
		# Verify the supplied candidate and return that same candidate for
		# its legacy comparison. A stored hash cannot be reversed or exposed.
		form = getattr(frappe.local, "form_dict", None) or {}
		if (form.get("cmd") == DOA_FLOOR_VERIFY_METHOD
		    and str(form.get("approver") or "").strip().casefold() == self.name.casefold()):
			candidate = _protect_pin(form.get("pin") or form.get("approver_pin"))
			_scrub_pin_from_form_dict()
			_check_user_pin(self.name, candidate, form.get("property"))
			return candidate.reveal()

		if self.pin_hash:
			return None
		return super().get_password(fieldname, raise_exception=raise_exception)
