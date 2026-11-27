import frappe
from frappe.model.document import Document

from kamra.kamra.doctype.reservation.reservation import lock_property_for_inventory
from kamra.rooms import nightly_sell_adjustments, property_inventory
from kamra.sell_limits import effective_capacities


def whole_adjustment(value):
	try:
		return int(str(value))
	except ValueError:
		frappe.throw(frappe._("Enter a whole number of rooms."))


def validate_capacities(rooms, adjustments, house, protected):
	try:
		effective_capacities(rooms, adjustments, house or 0, protected)
	except ValueError as exc:
		frappe.throw(frappe._(str(exc)))


def _validate_day(prop, day, exclude, edit=None):
	rooms = property_inventory(prop)
	adjustments = nightly_sell_adjustments(prop, day, day, exclude=exclude)[str(day)]
	if edit:
		rooms.setdefault(edit.room_type, 0)
		adjustments[edit.room_type] = edit.adjustment
	house, protected = frappe.db.get_value("Property", prop,
		["house_overbooking_limit", "overbooking_protection"], for_update=True)
	validate_capacities(rooms, adjustments, house, protected)


class RoomSellLimit(Document):
	def load_from_db(self):
		if self.flags.for_update:
			lock_property_for_inventory(frappe.db.get_value(self.doctype, self.name, "property"))
		return super().load_from_db()

	def load_doc_before_save(self, *, raise_exception=False):
		if not self.is_new():
			previous = frappe.db.get_value(self.doctype, self.name, "property")
			for prop in sorted({p for p in (self.property, previous) if p}):
				lock_property_for_inventory(prop)
		return super().load_doc_before_save(raise_exception=raise_exception)

	def validate(self):
		self.adjustment = whole_adjustment(0 if self.adjustment in (None, "") else self.adjustment)
		before = self.get_doc_before_save()
		lock_property_for_inventory(self.property)
		if frappe.db.get_value("Room Type", self.room_type, "property", for_update=True) != self.property:
			frappe.throw(frappe._("Choose a room type at this property."))
		if frappe.db.get_value("Room Sell Limit", {"property": self.property, "room_type": self.room_type,
				"stay_date": self.stay_date, "name": ("!=", self.name or "")}, "name", for_update=True):
			raise frappe.DuplicateEntryError(self.doctype, self.room_type, self.stay_date)
		if before and (before.property, str(before.stay_date)) != (self.property, str(self.stay_date)):
			_validate_day(before.property, before.stay_date, self.name)
		_validate_day(self.property, self.stay_date, self.name, self)

	def on_trash(self):
		lock_property_for_inventory(self.property)
		_validate_day(self.property, self.stay_date, self.name)
