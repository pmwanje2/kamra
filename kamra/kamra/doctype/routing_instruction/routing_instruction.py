# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class RoutingInstruction(Document):
	"""OPERA reservation routing: see kamra.routing. Created, read and
	changed through native document permissions; posting never saves it."""

	@property
	def used_amount(self):
		"""What the posted lines this instruction routed add up to - the
		business day's when the limit is per day. Never stored, so it cannot
		drift from the folios."""
		if self.is_new():
			return 0.0
		from kamra.routing import used
		day = None
		if self.per_day:
			from kamra.business_date import get_business_date
			day = get_business_date(self.property)
		return float(used(self.name, day))

	def validate(self):
		from kamra.routing import validate_instruction
		validate_instruction(self)
