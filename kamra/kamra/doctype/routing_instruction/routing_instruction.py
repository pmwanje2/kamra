# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class RoutingInstruction(Document):
	"""OPERA reservation routing: see kamra.routing. Created, read and
	changed through native document permissions; posting never saves it."""

	def __setup__(self):
		# keep an unset include_alcohol unset until before_insert picks the
		# default (Frappe would fill a Check with 0 first)
		self.dont_update_if_missing = ["include_alcohol"]

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

	def before_insert(self):
		# liquor is off by default; a group's own instruction takes the
		# group's Route Alcohol setting unless it was set on the instruction
		if self.include_alcohol is None:
			route = 0
			if self.group_booking and not self.reservation:
				import frappe
				route = frappe.db.get_value("Group Booking", self.group_booking, "route_alcohol")
			self.include_alcohol = 1 if route else 0

	def validate(self):
		from kamra.routing import validate_instruction
		validate_instruction(self)
