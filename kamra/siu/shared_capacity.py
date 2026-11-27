"""Feasibility of room-type promises that compete for connecting-unit members."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta

import frappe
from frappe.utils import getdate
from networkx import Graph, max_weight_matching
from networkx.utils import UnionFind

from kamra.reservation_state import LIVE_STATUSES


def _span(stay):
	start = getdate(stay["check_in_date"])
	return start, max(start + timedelta(days=1), getdate(stay["check_out_date"]))


class SearchLimit(Exception):
	"""The request has used its inventory search allowance."""


@dataclass
class SearchBudget:
	remaining: int = 50_000

	def spend(self, steps=1):
		if steps > self.remaining:
			self.remaining = 0
			raise SearchLimit()
		self.remaining -= steps


def _free(rooms, mask, occupied):
	return all(not (occupied.get(room, 0) & mask) for room in rooms)


def _occupy(rooms, mask, occupied):
	for room in rooms:
		occupied[room] = occupied.get(room, 0) | mask


def _physical_components(groups, occupied, masks, budget):
	"""Partition options by shared physical rooms, even within one room type."""
	connected = UnionFind()
	offers = []
	for group, (count, choices) in enumerate(groups):
		free = set()
		for rooms in choices:
			budget.spend(1 + len(rooms))
			if rooms and _free(rooms, masks[group], occupied):
				free.add(tuple(sorted(rooms)))
		if len(free) < count:
			return None
		for rooms in sorted(free):
			connected.union(*rooms)
			offers.append((group, rooms))

	components = defaultdict(lambda: defaultdict(list))
	for group, rooms in offers:
		components[connected[rooms[0]]][group].append(frozenset(rooms))
	return components.values()


def _profiles(component, limits, masks, budget):
	"""Yield attainable group counts in ONE component, always using whole footprints."""
	offers = [(group, rooms) for group, options in component.items() for rooms in options]
	zero = (0,) * len(limits)
	yield zero
	# Each stack frame extends a selection using only later offers. This
	# enumerates combinations, never permutations, without Python recursion.
	stack = [(iter(range(len(offers))), {}, zero)]
	while stack:
		indices, used, counts = stack[-1]
		index = next(indices, None)
		if index is None:
			stack.pop()
			continue
		group, rooms = offers[index]
		budget.spend(1 + len(rooms))
		if counts[group] >= limits[group] or not _free(rooms, masks[group], used):
			continue
		budget.spend(len(counts) + len(used))
		picked = list(counts)
		picked[group] += 1
		profile = tuple(picked)
		yield profile
		next_used = used.copy()
		_occupy(rooms, masks[group], next_used)
		stack.append((iter(range(index + 1, len(offers))), next_used, profile))


def _single_capacity(options, limit, budget):
	"""Maximum disjoint units in this component, capped at the outstanding demand."""
	if limit <= 1:
		return limit
	domain = frozenset().union(*options)
	upper = min(limit, len(domain) // min(len(option) for option in options))
	used = set()
	count = 0
	for rooms in options:
		if rooms.isdisjoint(used):
			used.update(rooms)
			count += 1
			if count == upper:
				return count

	if all(len(rooms) <= 2 for rooms in options):
		# Taking a singleton can always replace a pair using that room.
		# On the remaining graph Edmonds' blossom matching also handles odd cycles.
		singles = {next(iter(rooms)) for rooms in options if len(rooms) == 1}
		graph = Graph()
		graph.add_edges_from(rooms for rooms in options if len(rooms) == 2 and rooms.isdisjoint(singles))
		budget.spend(len(graph) * graph.number_of_edges())
		return min(limit, len(singles) + len(max_weight_matching(graph, maxcardinality=True)))

	for (attainable,) in _profiles({0: options}, (limit,), (1,), budget):
		count = max(count, attainable)
		if count == upper:
			break
	return count


def _fills_component(component, demand, masks, budget):
	"""Search only placements that can still meet all of the specified counts."""
	def placements(options, needs, group):
		for i, rooms in enumerate(options[group]):
			if len(options[group]) - i < needs[group]:
				break
			rest = {}
			remaining = needs.copy()
			remaining[group] -= 1
			for g, choices in options.items():
				if remaining[g]:
					budget.spend(len(choices))
					candidates = choices[i + 1:] if g == group else choices
					rest[g] = ([choice for choice in candidates if rooms.isdisjoint(choice)]
					           if masks[g] & masks[group] else candidates)
			yield rest, remaining

	stack = [iter([(component, list(demand))])]
	while stack:
		state = next(stack[-1], None)
		if state is None:
			stack.pop()
			continue
		options, needs = state
		active = [g for g, need in enumerate(needs) if need]
		if any(len(options.get(g, ())) < needs[g] for g in active):
			continue
		group = min(active, key=lambda g: len(options[g]) - needs[g])
		if len(active) == 1:
			if _single_capacity(options[group], needs[group], budget) == needs[group]:
				return True
			continue
		stack.append(placements(options, needs, group))
	return False


def _mixed_fits(components, demand, masks, budget):
	"""Combine feasible component allocations, retaining the outstanding demand by type."""
	if len(demand) == 1:
		needed = demand[0]
		for component in components:
			needed -= _single_capacity(component[0], needed, budget)
			if needed == 0:
				return True
		return False

	# An explicit witness settles common mixed carts without enumerating
	# alternatives. Failure here says nothing; the exact search follows.
	free = defaultdict(list)
	for component in components:
		for group, options in component.items():
			free[group].extend(options)
	used = {}
	for group in sorted(range(len(demand)), key=lambda g: (len(free[g]) - demand[g], -masks[g].bit_count())):
		needed = demand[group]
		for rooms in free[group]:
			budget.spend(len(rooms))
			if _free(rooms, masks[group], used):
				_occupy(rooms, masks[group], used)
				needed -= 1
				if not needed:
					break
		if needed:
			break
	else:
		return True

	# For different spans a room can satisfy several non-overlapping stays.
	# Branch on the most constrained stay before enumerating component counts.
	if len(set(masks)) > 1:
		return _fills_component(free, demand, masks, budget)

	target = tuple(demand)
	remaining = {target}
	for i, component in enumerate(components):
		if i == len(components) - 1:
			return any(_fills_component(component, needs, masks, budget) for needs in sorted(remaining))
		next_remaining = set()
		seen = set()
		for profile in _profiles(component, target, masks, budget):
			if profile in seen:
				continue
			seen.add(profile)
			for needs in remaining:
				budget.spend(len(needs))
				residual = tuple(max(0, need - n) for need, n in zip(needs, profile, strict=True))
				if not any(residual):
					return True
				next_remaining.add(residual)
		remaining = next_remaining
	return False


def _can_fit(groups, occupied, *, budget=None, masks=None):
	"""One-component capacities add; only competing products need a joint search."""
	budget = budget if budget is not None else SearchBudget()
	try:
		if masks is None:
			masks = [1] * len(groups)
			occupied = dict.fromkeys(occupied, 1)
		components = _physical_components(groups, occupied, masks, budget)
		if components is None:
			return False
		demand = [count for count, _ in groups]
		mixed = []
		for component in components:
			if len(component) == 1:
				group, options = next(iter(component.items()))
				demand[group] -= _single_capacity(options, demand[group], budget)
			else:
				mixed.append(component)
		if not any(demand):
			return True
		# Do not form a Cartesian product for unrelated products. A demand
		# spanning two physical components couples only the groups it shares.
		products = UnionFind(i for i, n in enumerate(demand) if n)
		regions = defaultdict(list)
		for component in mixed:
			active = [g for g in component if demand[g]]
			if active:
				products.union(*active)
		for component in mixed:
			active = {g: opts for g, opts in component.items() if demand[g]}
			if active:
				regions[products[next(iter(active))]].append(active)
		for region in products.to_sets():
			group_ids = sorted(region)
			index = {group: i for i, group in enumerate(group_ids)}
			local = [{index[g]: opts for g, opts in c.items()} for c in regions[products[group_ids[0]]]]
			if not _mixed_fits(local, [demand[g] for g in group_ids], [masks[g] for g in group_ids], budget):
				return False
		return True
	except SearchLimit:
		return False


# A room can be occupied and its sell allowance spent independently. Stored
# bookings and scalar holds have already been deducted from the sell limits.
# Bundle holds and new offers still need both resources on the same rooms.
_PHYSICAL = 1
_ALLOWANCE = 2
_BOTH = _PHYSICAL | _ALLOWANCE


def _limit_groups(member_types, occupied, remaining, house_free):
	"""Represent a limit of K among N free rooms by withholding N-K allowance slots."""
	by_type = defaultdict(set)
	for room, rt in member_types.items():
		if room not in occupied:
			by_type[rt].add(frozenset([room]))
	groups = []
	total = 0
	for rt, singles in sorted(by_type.items()):
		allowed = min(len(singles), max(0, remaining[rt]))
		if allowed < len(singles):
			groups.append((len(singles) - allowed, singles, _ALLOWANCE))
		total += allowed
	# Category holdbacks already count towards the house holdback. Only the
	# additional slots must be withheld, anywhere in the remaining house.
	if total > max(0, house_free):
		groups.append((total - max(0, house_free), set().union(*by_type.values()), _ALLOWANCE))
	return groups


@dataclass
class NightlyOffers:
	"""One physical placement test for published counts and a desk assignment."""
	targets: set
	groups: list
	masks: list
	occupied: dict

	def _greedy(self, upper, occupied):
		"""Deterministic, witnessed lower bound for budget-limited searches."""
		used = occupied.copy()

		def take(choices, mask, count):
			taken = 0
			for rooms in sorted(choices, key=lambda r: (len(r), sorted(r))):
				if taken == count:
					break
				if _free(rooms, mask, used):
					_occupy(rooms, mask, used)
					taken += 1
			return taken

		for (need, choices), mask in zip(self.groups, self.masks, strict=True):
			if take(choices, mask, need) < need:
				return -1
		return take(self.targets, _BOTH, upper)

	def fits(self, count, assigned=(), *, budget=None):
		if any(room in self.occupied for room in assigned):
			return False
		occupied = self.occupied | dict.fromkeys(assigned, _BOTH)
		budget = budget or SearchBudget()
		groups, masks = self.groups, self.masks
		if count:
			groups = [*groups, (count, self.targets)]
			masks = [*masks, _BOTH]
		if _can_fit(groups, occupied, masks=masks, budget=budget):
			return True
		return budget.remaining == 0 and self._greedy(count, occupied) >= count

	def maximum(self, upper):
		if not upper or not self.targets:
			return 0
		upper = min(upper, len(self.targets))
		budget = SearchBudget()
		if not self.groups:
			# Independent components add. Pairs use maximum matching, while
			# larger footprints use the bounded exact component search.
			try:
				components = _physical_components([(1, self.targets)], self.occupied, [_BOTH], budget)
				total = 0
				for component in components or ():
					total += _single_capacity(component[0], upper - total, budget)
					if total == upper:
						break
				return total
			except SearchLimit:
				return self._greedy(upper, self.occupied)
		low, high = max(0, self._greedy(upper, self.occupied)), upper
		while low < high and budget.remaining:
			count = (low + high + 1) // 2
			if self.fits(count, budget=budget):
				low = count
			else:
				high = count - 1
		return low


def nightly_offers(options, targets, holds, bundle_types, deferred, occupied, member_types,
                   remaining, house_free, *, bundle):
	"""Collect promises connected to the target by a room, category limit or house limit."""
	pending = [(count, options[rt], _BOTH if rt in bundle_types else _PHYSICAL)
	           for rt, count in sorted(holds.items())]
	pending.extend((count, options[rt], _PHYSICAL) for rt, count in sorted(deferred.items()))
	pending.extend(_limit_groups(member_types, occupied, remaining, house_free))
	physical = set().union(*targets)
	allowance = physical.copy()
	groups, masks = [], []
	shared_hold = False
	while pending:
		unlinked = []
		for count, choices, mask in pending:
			domain = set().union(*choices)
			if not ((mask & _PHYSICAL and not physical.isdisjoint(domain)) or
			        (mask & _ALLOWANCE and not allowance.isdisjoint(domain))):
				unlinked.append((count, choices, mask))
				continue
			groups.append((count, choices))
			masks.append(mask)
			if mask & _PHYSICAL:
				physical.update(domain)
			if mask & _ALLOWANCE:
				allowance.update(domain)
			shared_hold |= mask == _BOTH
		if len(unlinked) == len(pending):
			break
		pending = unlinked
	if bundle or shared_hold:
		return NightlyOffers(targets, groups, masks, dict.fromkeys(occupied, _PHYSICAL))
	return None


@dataclass
class CapacityWindow:
	"""Physical footprints are shared across stays; each bit represents one interval."""
	options: dict
	occupied: dict
	demand: Counter
	incoming: dict
	budget: SearchBudget

	def fits(self, assigned, *, budget=None):
		budget = budget or self.budget
		occupied = self.occupied.copy()
		demand = self.demand.copy()
		for index, key in self.incoming.items():
			rooms = assigned.get(index)
			if rooms:
				if not _free(rooms, key[1], occupied):
					return False
				_occupy(rooms, key[1], occupied)
			else:
				demand[key] += 1
		groups = [(count, self.options[rt]) for (rt, _), count in demand.items()]
		return _can_fit(groups, occupied, masks=[mask for _, mask in demand], budget=budget)


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


def _window(options, requests, deferred, fixed, unserviceable, quotas, start, end, *, budget=None):
	budget = budget or SearchBudget()
	budget.spend(1 + len(requests) + len(deferred) + len(fixed) + len(quotas) + len(unserviceable))
	bounds = {start, end}
	for intervals in (requests, deferred, fixed):
		for *_, a, b in intervals:
			bounds.update((max(a, start), min(b, end)))
	for a, b, _, _ in quotas:
		bounds.update((max(a, start), min(b, end)))
	index = {day: i for i, day in enumerate(sorted(bounds))}

	def mask(a, b):
		last = index[min(b, end)]
		budget.spend(1 + last // 30)  # Account for the words in the interval bitmask.
		return (1 << last) - (1 << index[max(a, start)])

	incoming = {i: (rt, mask(a, b)) for i, rt, a, b in requests}
	demand = Counter((rt, mask(a, b)) for rt, a, b in deferred)
	for a, b, rt, count in quotas:
		# Unpicked holds can use different rooms on different nights.
		first, last = index[max(a, start)], index[min(b, end)]
		budget.spend(last - first)
		for i in range(first, last):
			demand[rt, 1 << i] += count
	occupied = dict.fromkeys(unserviceable, mask(start, end))
	for rooms, a, b in fixed:
		budget.spend(len(rooms))
		_occupy(rooms, mask(a, b), occupied)
	return CapacityWindow(options, occupied, demand, incoming, budget)


def shared_capacity_windows(property, lines, *, exclude_reservation=None, budget=None, pending_swap=None):
	"""Read under the property inventory lock. Keep unrelated room types independent."""
	budget = budget or SearchBudget()
	if not frappe.db.exists("Sellable Unit", {"property": property, "unit_kind": "composite"}):
		return []
	rooms = frappe.get_all("Room", {"property": property}, ["name", "room_type", "housekeeping_status"])
	budget.spend(len(rooms))
	room_types = {r.name: r.room_type for r in rooms}
	unserviceable = {r.name for r in rooms if r.housekeeping_status in ("Out of Order", "Out of Service")}
	options = defaultdict(set)
	for room in rooms:
		options[room.room_type].add(frozenset([room.name]))
	members = defaultdict(set)
	units = frappe.get_all("Sellable Unit", {"property": property, "unit_kind": ("in", ("individual", "composite"))},
	                       ["name", "room_type", "is_active", "unit_kind"])
	budget.spend(len(units))
	for row in frappe.get_all("Sellable Unit Room", {"parent": ("in", [u.name for u in units])}, ["parent", "room"]):
		budget.spend()
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
		budget.spend(len(promises) + len(requests))
		deferred = [(s.room_type, *_span(s)) for s in promises]
		for start, end in _connected_spans(requests, deferred):
			budget.spend(len(requests) + len(deferred))
			arrivals = [r for r in requests if r[2] < end and r[3] > start]
			stays = [r for r in deferred if r[1] < end and r[2] > start]
			bookings = frappe.db.sql(
				"""SELECT name, room, sellable_unit, share_group_id, check_in_date, check_out_date FROM `tabReservation`
				   WHERE property = %(property)s AND status IN %(live)s AND name != %(exclude)s
				   AND check_in_date < %(end)s AND check_out_date >= %(start)s
				   AND (COALESCE(room, '') != '' OR COALESCE(sellable_unit, '') != '' OR EXISTS (
				       SELECT 1 FROM `tabReservation Occupied Room` p WHERE p.parent=`tabReservation`.name)) FOR UPDATE""",
				{**params, "start": start, "end": end}, as_dict=True)
			pinned = defaultdict(set)
			if bookings:
				for stay, room in frappe.db.get_values("Reservation Occupied Room",
				        {"parent": ("in", [b.name for b in bookings])}, ["parent", "room"], for_update=True):
					pinned[stay].add(room)
			fixed = []
			# Only Desk submits a share identity. The incoming reservation supplies
			# the shared footprint on its own dates; retain the peer on either side.
			peers = {(lines[i]["share_group_id"], lines[i]["room"], lines[i].get("sellable_unit") or None): (a, b)
			         for i, _, a, b in arrivals if lines[i].get("share_group_id") and lines[i].get("room")}
			for booking in bookings:
				if pending_swap and booking.name == pending_swap[0]:
					booking.room, booking.sellable_unit = pending_swap[1:]
				budget.spend(1 + len(members.get(booking.sellable_unit, ())))
				held = pinned.get(booking.name) or members.get(booking.sellable_unit, set())
				if booking.room:
					held = held | {booking.room}
				a, b = _span(booking)
				if b > start and any(room_types.get(r) in component for r in held):
					peer = peers.get((booking.share_group_id, booking.room, booking.sellable_unit or None))
					if peer and a < peer[1] and b > peer[0]:
						if a < peer[0]:
							fixed.append((held, a, peer[0]))
						if b > peer[1]:
							fixed.append((held, peer[1], b))
					else:
						fixed.append((held, a, b))
			blocks = frappe.get_all("Room Block", {"property": property, "block_status": "Active",
			                                      "from_date": ("<", end), "to_date": (">", start)},
			                        ["room", "from_date", "to_date"])
			fixed.extend(({row.room}, getdate(row.from_date), getdate(row.to_date)) for row in blocks
			             if room_types.get(row.room) in component)
			arrivals = [r for r in arrivals if not lines[r[0]].get("holds_only")]
			pickups = [(lines[i].get("group_booking"), rt, a, b,
			            ("room", lines[i]["room"]) if lines[i].get("share_group_id") else ("incoming", i))
			           for i, rt, a, b in arrivals]
			quotas = shared_block_holds(property, component, start, end, pickups, exclude_reservation)
			windows.append(_window(options, arrivals, stays, fixed, unserviceable, quotas, start, end, budget=budget))
	return windows
