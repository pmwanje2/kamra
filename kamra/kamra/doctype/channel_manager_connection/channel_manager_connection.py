import frappe
from frappe.model.document import Document


class ChannelManagerConnection(Document):
	def before_insert(self):
		# a Channex booking lists its taxes; which is the room tax is this
		# connection's rules, starting from the standard ones
		if self.provider == "Channex" and not self.get("ota_tax_rules"):
			from kamra.kamra.doctype.channel_ota_tax_rule.channel_ota_tax_rule import standard_rules
			self.set("ota_tax_rules", standard_rules())

	def validate(self):
		# Inbound webhooks are refused until the connection can authenticate
		# them - say so up front instead of letting bookings silently bounce.
		if not self.active:
			return
		if self.provider == "AioSell":
			missing = not (self.api_username and self.api_key)
			what = "API username and API key"
		else:
			missing = not self.webhook_secret
			what = "webhook secret"
		if missing:
			frappe.msgprint(
				f"Set the {what}: inbound bookings from {self.provider} are "
				"rejected until it is configured.",
				indicator="orange", alert=True)
