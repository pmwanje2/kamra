"""Feasibility of room-type promises that compete for connecting-unit members."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta
from itertools import combinations
from math import comb

import frappe
from frappe.utils import getdate

from kamra.reservation_state import LIVE_STATUSES


def _span(stay):
	start = getdate(stay["check_in_date"])
	return start, max(start + timedelta(days=1), getdate(stay["check_out_date"]))


def _disjoint(choices):
	return sum(map(len, choices)) == len(frozenset().union(*choices))


def _selections(choices, count):
	"""Discard colliding branches as soon as one unit is placed."""
	if count == 1:
		yield from choices
	elif _disjoint(choices):
		for selection in combinations(choices, count):
			yield frozenset().union(*selection)
	else:
		for i in range(len(choices) - count + 1):
			first = choices[i]
			rest = [other for other in choices[i + 1:] if first.isdisjoint(other)]
			if len(rest) >= count - 1:
				for others in _selections(rest, count - 1):
					yield first | others


def _can_fit(groups, occupied):
	"""Each deferred stay needs one whole option, disjoint from all the others."""
	remaining = []
	min_rooms = 0
	all_rooms = set()
	for count, choices in groups:
		free = [rooms for rooms in choices if rooms.isdisjoint(occupied)]
		if len(free) < count:
			return False
		domain = frozenset().union(*free)
		min_rooms += sum(sorted(map(len, free))[:count])
		all_rooms.update(domain)
		remaining.append((count, free, domain))
	if min_rooms > len(all_rooms):
		return False

	# Independent types with disjoint options need only a count. For shared
	# types, enumerate combinations of units, never permutations of guests.
	competing = [(count, free) for i, (count, free, domain) in enumerate(remaining)
	             if not _disjoint(free) or any(not domain.isdisjoint(other)
	                                           for j, (_, _, other) in enumerate(remaining) if i != j)]
	if not competing:
		return True
	competing.sort(key=lambda group: comb(len(group[1]), group[0]))
	count, free = competing[0]
	for selected in _selections(free, count):
		if _can_fit(competing[1:], occupied | selected):
			return True
	return False


@dataclass
class CapacityWindow:
	"""Demand is constant until the next arrival, departure or block boundary."""
	options: dict
	occupied: set
	deferred: Counter
	incoming: dict

	def fits(self, assigned):
		occupied = self.occupied.copy()
		demand = self.deferred.copy()
		for index, room_type in self.incoming.items():
			rooms = assigned.get(index)
			if rooms:
				if not occupied.isdisjoint(rooms):
					return False
				occupied.update(rooms)
			else:
				demand[room_type] += 1
		return _can_fit([(n, self.options[rt]) for rt, n in demand.items()], occupied)


def shared_capacity_windows(property, lines, *, exclude_reservation=None):
	"""Read under the property inventory lock. Keep unrelated room types independent."""
	if not frappe.db.exists("Sellable Unit", {"property": property, "unit_kind": "composite"}):
		return []
	rooms = frappe.get_all("Room", {"property": property}, ["name", "room_type", "housekeeping_status"])
	room_types = {r.name: r.room_type for r in rooms}
	unserviceable = {r.name for r in rooms if r.housekeeping_status in ("Out of Order", "Out of Service")}
	options = defaultdict(set)
	for room in rooms:
		options[room.room_type].add(frozenset([room.name]))
	members = defaultdict(set)
	units = frappe.get_all("Sellable Unit", {"property": property, "unit_kind": ("in", ("individual", "composite"))},
	                       ["name", "room_type", "is_active", "unit_kind"])
	for row in frappe.get_all("Sellable Unit Room", {"parent": ("in", [u.name for u in units])}, ["parent", "room"]):
		members[row.parent].add(row.room)
	neighbors = defaultdict(set)
	composite_types = set()
	for unit in units:
		footprint = frozenset(members[unit.name])
		if unit.unit_kind == "composite":
			composite_types.add(unit.room_type)
		if unit.is_active and footprint and footprint.issubset(room_types):
			options[unit.room_type].add(footprint)
		for room in footprint:
			if room in room_types:
				neighbors[unit.room_type].add(room_types[room])
				neighbors[room_types[room]].add(unit.room_type)

	# A flexible room-type promise never needs to consume a larger unit
	# if a member alone satisfies the very same promise. Explicitly booked
	# units retain their full footprint in both the cart and stored holds.
	for rt, choices in options.items():
		singles = {room for choice in choices if len(choice) == 1 for room in choice}
		options[rt] = {choice for choice in choices if len(choice) == 1 or choice.isdisjoint(singles)}

	# Find connected products via their physical room types. An inactive
	# unit can still have a reservation holding its members.
	components = []
	seen = set()
	for line in lines:
		rt = line["room_type"]
		if rt in seen:
			continue
		component, pending = {rt}, [rt]
		while pending:
			linked = neighbors[pending.pop()] - component
			pending.extend(linked)
			component.update(linked)
		seen.update(component)
		if component & composite_types:
			components.append(component)
	if not components:
		return []

	spans = [_span(line) for line in lines]
	start = min(s for s, _ in spans)
	end = max(e for _, e in spans)
	bookings = frappe.db.sql(
		"""SELECT name, room_type, room, sellable_unit, check_in_date, check_out_date
		   FROM `tabReservation` WHERE property = %(property)s AND status IN %(live)s
		   AND name != %(exclude)s AND check_in_date < %(end)s AND check_out_date >= %(start)s
		   FOR UPDATE""", {"property": property, "live": LIVE_STATUSES, "exclude": exclude_reservation or "",
		                   "start": start, "end": end}, as_dict=True)
	blocks = frappe.get_all("Room Block", {"property": property, "block_status": "Active",
	                                      "from_date": ("<", end), "to_date": (">", start)},
	                        ["room", "from_date", "to_date"])
	windows = []
	for component in components:
		requests = [(i, line["room_type"], *spans[i]) for i, line in enumerate(lines) if line["room_type"] in component]
		holds = []
		for stay in bookings:
			held = members.get(stay.sellable_unit, set())
			if stay.room:
				held = held | {stay.room}
			if stay.room_type in component or any(room_types.get(r) in component for r in held):
				holds.append((stay.room_type, held, *_span(stay)))
		outages = [(b.room, getdate(b.from_date), getdate(b.to_date)) for b in blocks if room_types.get(b.room) in component]
		boundaries = {day for interval in (*requests, *holds, *outages) for day in interval[-2:]}
		for day in sorted(boundaries):
			incoming = {i: rt for i, rt, a, b in requests if a <= day < b}
			if not incoming:
				continue
			occupied = unserviceable | {r for r, a, b in outages if a <= day < b}
			deferred = Counter()
			for rt, held, a, b in holds:
				if a <= day < b:
					if held:
						occupied.update(held)
					else:
						deferred[rt] += 1
			windows.append(CapacityWindow(options, occupied, deferred, incoming))
	return windows
