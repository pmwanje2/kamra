import frappe
from frappe.model.document import Document


class DoorKeyAudit(Document):
	def validate(self):
		if not self.is_new():
			frappe.throw(frappe._("Key audit records are immutable."))

	def on_trash(self):
		frappe.throw(frappe._("Key audit records are immutable."))
