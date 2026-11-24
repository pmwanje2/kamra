import frappe
from frappe import _
from frappe.model.document import Document


class ChannelManagerConnection(Document):
	def validate(self):
		self.seed_ota_tax_rules()
		self.validate_ota_taxes()
		self.validate_inhouse_policy()
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

	def validate_inhouse_policy(self):
		"""OPERA Cloud 26.2 OXI UPDATE INHOUSE RES (default N): N rejects OTA
		changes to Checked In stays and flags them for front-desk review. Y is
		refused until arrival-date, room-type, billed-night, and assigned-room
		safety bounds are implemented."""
		value = (self.get("update_inhouse_res") or "N").strip()
		self.update_inhouse_res = value
		if value != "N":
			frappe.throw(
				_("Update In-House Reservations = Y is not yet enabled: OXI Y requires arrival-date, room-type, billed-night, and assigned-room safety bounds. Keep N so OTA changes to checked-in stays are rejected and flagged for review."),
				title=_("Update In-House Reservations"),
			)

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
		CJK rules need at least two characters so single characters like "税"
		cannot swallow unrelated taxes, and each OTA has one tax basis (a
		second row for it was never read)."""
		from kamra.channel_manager import ota_key
		from kamra.channels.channex import is_cjk_word, tax_words

		for row in self.get("ota_tax_rules") or []:
			words = tax_words(row.match_text)
			if not words:
				frappe.throw(_("OTA Tax Rules row {0}: the text needs a letter or a digit.").format(row.idx),
				             title=_("OTA Tax Rules"))
			if any(is_cjk_word(w) and len(w) < 2 for w in words):
				frappe.throw(
					_("OTA Tax Rules row {0}: a CJK rule needs at least two characters so it does not match unrelated taxes.").format(row.idx),
					title=_("OTA Tax Rules"),
				)
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
