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
	"""One connected set of stays. A resource is a physical room during one interval."""
	options: dict
	occupied: set
	demand: Counter
	incoming: dict

	def fits(self, assigned):
		occupied = self.occupied.copy()
		demand = self.demand.copy()
		for index, key in self.incoming.items():
			rooms = assigned.get(index)
			if rooms:
				cells = {(night, room) for night in key[1] for room in rooms}
				if not occupied.isdisjoint(cells):
					return False
				occupied.update(cells)
			else:
				demand[key] += 1
		return _can_fit([(count, self.options[key]) for key, count in demand.items()], occupied)


def _connected_spans(requests, deferred):
	"""Extend through overlapping deferred stays, including transitive overlaps."""
	intervals = [(a, b, True) for _, _, a, b in requests]
	intervals.extend((a, b, False) for _, a, b in deferred)
	start = end = None
	requested = False
	for a, b, incoming in sorted(intervals):
		if end is None or a >= end:
			if requested:
				yield start, end
			start, end, requested = a, b, incoming
		else:
			end = max(end, b)
			requested |= incoming
	if requested:
		yield start, end


def _window(options, requests, deferred, fixed, unserviceable, quotas, start, end):
	# Only split when inventory changes. A physical option covers ALL cells
	# of a reservation, so a guest cannot switch rooms at an interval boundary.
	bounds = {start, end}
	for intervals in (requests, deferred, fixed):
		for *_, a, b in intervals:
			bounds.update((max(a, start), min(b, end)))
	for a, b, _, _ in quotas:
		bounds.update((a, b))
	points = sorted(bounds)[:-1]
	def stay_key(rt, a, b):
		return rt, tuple(i for i, day in enumerate(points) if a <= day < b)
	incoming = {i: stay_key(rt, a, b) for i, rt, a, b in requests}
	demand = Counter(stay_key(rt, a, b) for rt, a, b in deferred)
	for a, b, rt, count in quotas:
		# Allotments are nightly promises. A later pickup becomes a stay.
		for night in stay_key(rt, a, b)[1]:
			demand[rt, (night,)] += count
	occupied = {(i, room) for rooms, a, b in fixed for i in stay_key(None, a, b)[1] for room in rooms}
	choices = {}
	for key in set(demand) | set(incoming.values()):
		rt, nights = key
		choices[key] = [frozenset((n, r) for n in nights for r in footprint)
		                for footprint in options[rt] if footprint.isdisjoint(unserviceable)]
	occupied.update((i, r) for i in range(len(points)) for r in unserviceable)
	return CapacityWindow(choices, occupied, demand, incoming)


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

	from kamra.group_blocks import shared_block_holds
	windows = []
	for component in components:
		requests = [(i, line["room_type"], *_span(line)) for i, line in enumerate(lines)
		            if line["room_type"] in component]
		params = {"property": property, "live": LIVE_STATUSES, "exclude": exclude_reservation or "",
		          "types": sorted(component)}
		promises = frappe.db.sql(
			"""SELECT room_type, check_in_date, check_out_date FROM `tabReservation`
			   WHERE property = %(property)s AND status IN %(live)s AND name != %(exclude)s
			   AND room_type IN %(types)s AND COALESCE(room, '') = '' AND COALESCE(sellable_unit, '') = ''
			   ORDER BY check_in_date FOR UPDATE""", params, as_dict=True)
		deferred = [(s.room_type, *_span(s)) for s in promises]
		for start, end in _connected_spans(requests, deferred):
			arrivals = [r for r in requests if r[2] < end and r[3] > start]
			stays = [r for r in deferred if r[1] < end and r[2] > start]
			bookings = frappe.db.sql(
				"""SELECT room, sellable_unit, check_in_date, check_out_date FROM `tabReservation`
				   WHERE property = %(property)s AND status IN %(live)s AND name != %(exclude)s
				   AND check_in_date < %(end)s AND check_out_date >= %(start)s
				   AND (COALESCE(room, '') != '' OR COALESCE(sellable_unit, '') != '') FOR UPDATE""",
				{**params, "start": start, "end": end}, as_dict=True)
			fixed = []
			for booking in bookings:
				held = members.get(booking.sellable_unit, set())
				if booking.room:
					held = held | {booking.room}
				a, b = _span(booking)
				if b > start and any(room_types.get(r) in component for r in held):
					fixed.append((held, a, b))
			blocks = frappe.get_all("Room Block", {"property": property, "block_status": "Active",
			                                      "from_date": ("<", end), "to_date": (">", start)},
			                        ["room", "from_date", "to_date"])
			fixed.extend(({row.room}, getdate(row.from_date), getdate(row.to_date)) for row in blocks
			             if room_types.get(row.room) in component)
			pickups = [(lines[i].get("group_booking"), rt, a, b) for i, rt, a, b in arrivals]
			quotas = shared_block_holds(property, component, start, end, pickups, exclude_reservation)
			windows.append(_window(options, arrivals, stays, fixed, unserviceable, quotas, start, end))
	return windows
