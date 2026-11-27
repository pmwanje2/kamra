# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class Room(Document):
	def validate(self):
		# Room.floor stays the floor's name; make sure the Floor master has it
		from kamra.floors import ensure_floor
		self.floor = (self.floor or "").strip() or None
		if self.floor and (self.is_new() or self.has_value_changed("floor")):
			ensure_floor(self.property, self.floor)

		previous = self.get_doc_before_save()
		if self.is_new() or previous and previous.property != self.property:
			from kamra.arrival_rooms import backfill_property
			properties = {self.property}
			if previous:
				properties.add(previous.property)
			for prop in sorted(properties):
				backfill_property(prop, villas_only=True)
			if previous and frappe.db.sql("""
				SELECT 1 FROM `tabReservation Occupied Room` m
				JOIN `tabReservation` r ON r.name=m.parent
				WHERE m.room=%s AND r.status='Checked In' LIMIT 1 FOR UPDATE""", self.name):
				frappe.throw(frappe._("Room unavailable"))
		for field, default in {
			"hk_occupancy": None, "hk_reported_at": None, "hk_resolved_occupancy": None,
			"turndown_requested": 0, "turndown_status": "Not Required",
			"turndown_business_date": None,
		}.items():
			self.set(field, previous.get(field) if previous else default)

	def after_insert(self):
		# Keep Sellable Units in sync with physical inventory (ADR-001).
		try:
			from kamra.siu.units import ensure_individual_siu
			ensure_individual_siu(self.name)
		except Exception:
			frappe.log_error(title="Sellable Unit sync on Room insert")

	def on_update(self):
		from kamra.housekeeping import UNSERVICEABLE_ROOMS, retire_turndown_tasks
		if self.has_value_changed("housekeeping_status") and self.housekeeping_status in UNSERVICEABLE_ROOMS:
			retire_turndown_tasks(self.property, room=self.name)
		# Room.room_type drives the individual Sellable Unit's type (#548).
		if not self.has_value_changed("room_type"):
			return
		from kamra.siu.units import sync_individual_siu_room_type
		sync_individual_siu_room_type(self.name)
