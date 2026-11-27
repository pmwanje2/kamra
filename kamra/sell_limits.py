"""Nightly inventory arithmetic. A sell limit adjusts a room count."""
from decimal import Decimal, InvalidOperation


def effective_capacities(rooms, adjustments, house_limit, protected, defaults=None):
	capacities = {rt: max(0, count + adjustments.get(rt, 0)) for rt, count in rooms.items()}
	if protected and sum(capacities.values()) > sum(rooms.values()) + house_limit:
		raise ValueError("The room-type sell limits exceed the house overbooking limit.")
	# Dated limits take priority over legacy percentage allowances. Allocate
	# remaining house headroom deterministically when protection is enabled.
	spare = sum(rooms.values()) + house_limit - sum(capacities.values())
	for rt in sorted(capacities.keys() - adjustments.keys()):
		extra = (defaults or {}).get(rt, 0)
		if protected:
			extra = min(extra, spare)
		capacities[rt] += extra
		spare -= extra
	return capacities


def relocation_minor_units(amount, digits):
	try:
		units = Decimal(str(amount)).scaleb(digits)
		if units.is_finite() and 0 <= units <= 2147483647 and units == units.to_integral_value():
			return int(units)
	except InvalidOperation:
		pass
	raise ValueError("Enter a nonnegative relocation cost in the property's currency precision.")
