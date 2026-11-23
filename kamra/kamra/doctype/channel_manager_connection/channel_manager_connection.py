import frappe
from frappe import _
from frappe.model.document import Document


class ChannelManagerConnection(Document):
	def validate(self):
		self.seed_ota_tax_rules()
		self.validate_ota_taxes()
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

	def seed_ota_tax_rules(self):
		"""A Channex booking lists its taxes; which is the room tax is this
		connection's rules. A connection that becomes Channex - made as one,
		or switched to it from another provider - with no rules of its own
		starts from the standard ones."""
		if self.provider != "Channex" or self.get("ota_tax_rules"):
			return
		if self.is_new() or self.has_value_changed("provider"):
			from kamra.kamra.doctype.channel_ota_tax_rule.channel_ota_tax_rule import standard_rules
			self.set("ota_tax_rules", standard_rules())

	def validate_ota_taxes(self):
		"""A rule needs a word to match (punctuation alone matches no tax),
		and each OTA has one tax basis (a second row for it was never read)."""
		from kamra.channel_manager import ota_key
		from kamra.channels.channex import tax_words

		for row in self.get("ota_tax_rules") or []:
			if not tax_words(row.match_text):
				frappe.throw(_("OTA Tax Rules row {0}: the text needs a letter or a digit.").format(row.idx),
				             title=_("OTA Tax Rules"))
		seen = {}
		for row in self.get("ota_tax_basis") or []:
			key = ota_key(row.channel)
			if not key:
				frappe.throw(_("OTA Tax Basis row {0}: the OTA name needs a letter or a digit.").format(row.idx),
				             title=_("OTA Tax Basis"))
			if key in seen:
				frappe.throw(_("OTA Tax Basis rows {0} and {1} are the same OTA ({2}): keep one.").format(
					seen[key], row.idx, row.channel.strip()), title=_("OTA Tax Basis"))
			seen[key] = row.idx
