"""Kamra's pricing engine.

Deterministic by design: agents and humans both get quotes from this module;
no LLM ever computes money. Resolution order per night:
  season-adjusted occupancy rate -> long-stay discount (guardrail floor)
  -> rate-plan modifier (or an agreed / contract rate) -> the desk's
  reservation discount -> meal plan -> voucher discount -> tax.

A rate plan may derive from a base plan (OPERA Cloud Base Rates / Derived
Rates): the base's price first, then the derived plan's own modifier, with
the plan's rounding step (SAP rounding DIFF) on the price the guest sees.
"""

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import add_days, cint, date_diff, flt, getdate, nowdate

from kamra.branding import product_name
from kamra.localization import default_currency, pack_for


def _dec(v) -> Decimal:
	return Decimal(str(v or 0))


# OPERA Cloud Rate Code "Exchange Type": when a foreign contract's rate is fixed.
DEFAULT_FX_LOCK = "Fixed at Reservation"
ROUNDING_MODES = {"Nearest": ROUND_HALF_UP, "Up": ROUND_CEILING, "Down": ROUND_FLOOR}
# The most rate plans one derivation chain may hold, its root base included.
RATE_PLAN_CHAIN_LIMIT = 5

# Authoritative Reservation price-determination inputs (SAP condition-record key model).
# Kept in 1:1 parity with hospitality-tariffs@11 `Kamra Tariff Resolve` and kamra#32's
# reservation-discount inputs so Kamra's pricing engine and tariff guards cannot drift apart.
PRICE_DETERMINATION_GUARDED_FIELDS: tuple[tuple[str, str], ...] = (
	("property", "str"),
	("room_type", "str"),
	("rate_plan", "str"),
	("company", "str"),
	("group_booking", "str"),
	("meal_plan", "str"),
	("voucher", "str"),
	("adults", "int"),
	("children", "int"),
	("infants", "int"),
	("extra_beds", "int"),
	("pets", "int"),
	("is_day_use", "int"),
	("fixed_rate", "int"),
	("auto_price", "int"),
	("agreed_rate", "flt"),
	("over_capacity_charge", "flt"),
	("discount_type", "str"),
	("discount_value", "flt"),
	("planned_check_in_time", "day_use_time"),
	("planned_check_out_time", "day_use_time"),
	("occupants", "occupant_ages"),
	("night_rates", "night_rates"),
)
PRICE_DETERMINATION_DATE_FIELDS: tuple[str, ...] = ("check_in_date", "check_out_date")
PRICE_DETERMINATION_SNAPSHOT_FIELDS: tuple[str, ...] = ("contract_tariff",)
PRICE_DETERMINATION_FIELDS: frozenset[str] = frozenset(
	[name for name, _kind in PRICE_DETERMINATION_GUARDED_FIELDS]
	+ list(PRICE_DETERMINATION_DATE_FIELDS)
	+ list(PRICE_DETERMINATION_SNAPSHOT_FIELDS)
)


class RatePlanChainError(frappe.ValidationError):
	"""A rate plan's base plans loop back to it or run too deep."""


def _minor_unit(currency: str | None) -> int:
	"""ISO 4217 decimals of a currency (CLDR data, as payment rounding uses)."""
	if not currency:
		return 2
	from babel.numbers import get_currency_precision
	return get_currency_precision(currency)


def round_rate_amount(amount, step, mode: str = "Nearest", currency: str | None = None) -> Decimal:
	"""A price rounded the way its rate plan says.

	`step` is the multiple to round to, for example 1,000 or 5,000 in a
	currency with no cents. A step of 0 rounds to the currency's ISO 4217
	minor unit: whole units for UGX or JPY, cents for USD, two decimals
	without a currency. Nearest rounds half up; Up and Down go to the
	higher or lower multiple."""
	rounding = ROUNDING_MODES.get(mode or "Nearest")
	if rounding is None:
		frappe.throw(_("Rounding mode must be Nearest, Up or Down."))
	step = _dec(step)
	if not step.is_finite() or step < 0:
		frappe.throw(_("The rounding step cannot be negative."))
	amount = _dec(amount)
	if step:
		return (amount / step).quantize(Decimal(1), rounding=rounding) * step
	return amount.quantize(Decimal(1).scaleb(-_minor_unit(currency)), rounding=rounding)


def resolve_rate_plan_chain(rate_plan) -> list:
	"""A rate plan and the base plans it derives from, root first - the
	order their modifiers apply. Takes a plan name or a Rate Plan document
	(an unsaved one too). A plan whose bases loop back to it is refused, and
	so is a chain of more than RATE_PLAN_CHAIN_LIMIT plans."""
	plan = frappe.get_doc("Rate Plan", rate_plan) if isinstance(rate_plan, str) else rate_plan
	chain = [plan]
	seen = {plan.name} if plan.name else set()
	while chain[0].get("base_rate_plan"):
		base = chain[0].base_rate_plan
		if base in seen:
			frappe.throw(_("The base rate plans of {0} loop back to it. Choose a base plan that does not "
			               "derive from it.").format(plan.get("rate_plan_name") or plan.name), RatePlanChainError)
		if len(chain) >= RATE_PLAN_CHAIN_LIMIT:
			frappe.throw(_("A rate plan can derive through at most {0} plans.").format(RATE_PLAN_CHAIN_LIMIT),
			             RatePlanChainError)
		seen.add(base)
		chain.insert(0, frappe.get_doc("Rate Plan", base))
	return chain


def property_currency(property: str) -> str:
	return frappe.get_cached_doc("Property", property).currency or default_currency(pack_for(property))


def _tariff_price_list_currency(plan) -> str | None:
	"""The currency of a rate plan's linked tariff Price List, if any."""
	price_list = plan.get("custom_tariff_price_list")
	if not price_list or not frappe.db.exists("DocType", "Price List"):
		return None
	return frappe.db.get_value("Price List", price_list, "currency") or None


def rate_plan_currency(chain: list) -> str:
	"""A plan's currency: its own, else its linked tariff price list's,
	else its nearest base plan's, else the property's."""
	for plan in reversed(chain):
		if plan.get("currency"):
			return plan.currency
		list_currency = _tariff_price_list_currency(plan)
		if list_currency:
			return list_currency
	return property_currency(chain[-1].property)


def _fx_stale_window() -> int:
	"""Days a published rate stays valid: ERPNext Accounts Settings.stale_days, at least 1."""
	if not frappe.db.exists("DocType", "Accounts Settings") or not frappe.get_meta(
			"Accounts Settings").has_field("stale_days"):
		return 1
	return max(1, cint(frappe.db.get_single_value("Accounts Settings", "stale_days")))


def posting_fx_rate(property: str, currency: str, on_date=None) -> tuple[Decimal, str] | None:
	"""The governed selling rate for one foreign unit in the property's
	currency on a date (never later than the business date), read the way
	the tariff resolver reads it (hospitality_tariffs.tariff_rate, SAP OB08
	valid-from): the latest ERPNext Currency Exchange row on or before that
	date and within Accounts Settings.stale_days. Only the rate - a contract
	is never re-priced here. (rate, its date), or None without exactly one
	positive row on that latest date."""
	from kamra.business_date import get_business_date

	home = property_currency(property)
	if not currency or currency == home:
		return Decimal(1), str(getdate(on_date or get_business_date(property)))
	if not frappe.db.exists("DocType", "Currency Exchange"):
		return None
	business = getdate(get_business_date(property))
	day = min(getdate(on_date), business) if on_date else business
	rows = frappe.get_all("Currency Exchange", fields=["exchange_rate", "date"], filters={
		"from_currency": currency, "to_currency": home, "for_selling": 1,
		"date": ["between", [str(add_days(day, 1 - _fx_stale_window())), str(day)]]})
	latest = max((str(getdate(row.date)) for row in rows), default=None)
	rows = [row for row in rows if latest and str(getdate(row.date)) == latest]
	if len(rows) != 1 or _dec(rows[0].exchange_rate) <= 0:
		return None
	return _dec(rows[0].exchange_rate), latest


def rate_plan_fx_policy(rate_plan: str | None) -> str:
	"""The plan's exchange-rate lock; Fixed at Reservation when unset."""
	if not rate_plan:
		return DEFAULT_FX_LOCK
	return frappe.db.get_value("Rate Plan", rate_plan, "fx_lock_policy") or DEFAULT_FX_LOCK


def contract_local_amount(rate_plan, nightly, fx_rate, home_currency) -> Decimal:
	"""A contract amount in the posting currency: converted at `fx_rate` and rounded as the plan rounds."""
	return round_rate_amount(_dec(nightly) * _dec(fx_rate), rate_plan.get("rounding_step"),
	                         rate_plan.get("rounding_mode") or "Nearest", home_currency)


def pet_charge_amount(prop, pets: int, nights: int) -> Decimal:
	"""What the property charges for this many pets over the stay, before
	tax. Per stay by default - most homestays charge once per pet, not per
	night - with Per night for the ones that price it like a room."""
	pets = max(0, cint(pets))
	if not pets:
		return Decimal(0)
	if not cint(prop.get("pets_allowed")):
		frappe.throw("This property does not accept pets.")
	amount = _dec(prop.get("pet_charge")) * pets
	if (prop.get("pet_charge_basis") or "Per stay") == "Per night":
		amount *= max(cint(nights), 1)
	return amount


def small_group_allows_stay(rt, check_in, check_out) -> bool:
	"""Does the small-group rate survive this particular stay?

	Properties that price small parties per head usually mean it midweek
	only, and treat a stay that touches a weekend as a weekend stay
	throughout - you cannot have two nights of a villa at the per-head rate
	because the first one happened to be a Thursday. `small_group_whole_stay`
	says so: every night has to fall on a small-group day, or the rate is off
	for the whole booking.

	Off (the default), each night is judged on its own, which is what a
	property wanting a simple midweek discount expects.
	"""
	if not rt.get("small_group_whole_stay"):
		return True
	days = rt.get("small_group_days")
	if not days:
		return True
	nights = max(1, int(date_diff(check_out, check_in) or 0))
	return all(
		days_cover(days, getdate(add_days(check_in, i)).strftime("%a").lower())
		for i in range(nights)
	)


def _small_group_rate(rt, adults: int, date=None) -> Decimal | None:
	"""Per-person rate for a party smaller than the room type's small-group
	threshold, or None when the rule does not apply.

	Whole-place lets (villas, homestays) often price a small midweek party
	per head rather than charging the full house rate - "under 5 guests, 4,000
	each". Kamra's standard model only ever ADDS for guests beyond base
	occupancy, so without this a party of three pays the eight-person rate.

	`small_group_days` scopes the rule to the days it applies on (blank = every
	day), which is what keeps a midweek-only rule from undercutting the weekend
	house rate. With no date in hand the rule is treated as day-agnostic.
	"""
	threshold = int(rt.get("small_group_max_adults") or 0)
	per_adult = _dec(rt.get("small_group_price_per_adult"))
	if not threshold or not per_adult or adults >= threshold:
		return None
	if date is not None and not days_cover(
			rt.get("small_group_days"), getdate(date).strftime("%a").lower()):
		return None
	return adults * per_adult


def _extra_adult_charge(rt, adults: int) -> Decimal:
	"""What guests beyond the base occupancy add, up to the room's capacity.

	The rate card only covers the room as sold - base occupancy plus the
	extra adults it can take. Anyone beyond the stated capacity is there by
	arrangement, on a mattress on the floor, and what they cost is agreed
	with the guest rather than read off a rate card. That amount lives on the
	booking (`over_capacity_charge`), so the engine stops at capacity and does
	not invent a price for people it was never asked about.
	"""
	base = int(rt.base_occupancy or 2)
	cap = int(rt.get("adults_capacity") or 0)
	billable = min(adults, cap) if cap else adults
	extra = max(0, billable - base)
	return extra * _dec(rt.extra_adult_price)


def occupancy_rate(rt, adults: int, children: int, date=None,
                   allow_small_group: bool = True) -> Decimal:
	"""Nightly room rate for a party, from Room Type occupancy pricing.

	`date` is optional and only used by the small-group rule, so every existing
	caller keeps its behaviour; pass the night being priced to honour
	`small_group_days`. `allow_small_group=False` switches that rate off for
	the whole stay - see small_group_allows_stay().
	"""
	room, extra = _occupancy_parts(rt, adults, children, date, allow_small_group)
	return room + extra


def extra_person_charge(rt, adults: int, children: int, date=None,
                        allow_small_group: bool = True) -> Decimal:
	"""The part of occupancy_rate() that extra adults and children add
	(OPERA Extra Adult / Extra Child). A per-head small-group price has none."""
	return _occupancy_parts(rt, adults, children, date, allow_small_group)[1]


def _occupancy_parts(rt, adults: int, children: int, date=None,
                     allow_small_group: bool = True) -> tuple[Decimal, Decimal]:
	"""occupancy_rate() as (the room as sold, what extra guests add)."""
	adults = max(1, int(adults or 1))
	children = int(children or 0)

	# Where children fill the base occupancy alongside adults, they also
	# decide whether the party is a small group: four adults and two children
	# are six guests, not a party of four with two extras.
	counts_children = bool(rt.get("children_within_base_occupancy"))
	guests = adults + children if counts_children else adults
	small = _small_group_rate(rt, guests, date) if allow_small_group else None
	if small is not None and counts_children:
		return small, Decimal(0)  # every guest, child or adult, is in the per-head price
	if small is not None:
		room, extra = small, Decimal(0)
	elif adults == 1 and rt.single_occupancy_price:
		room, extra = _dec(rt.single_occupancy_price), Decimal(0)
	else:
		room, extra = _dec(rt.base_price), _extra_adult_charge(rt, adults)

	# children between free_child_age and child_age_limit pay child_price;
	# the API treats older children as adults upstream.
	return room, extra + _child_charge(rt, adults, children, priced_per_head=small is not None)


def _child_charge(rt, adults: int, children: int,
                  priced_per_head: bool = False) -> Decimal:
	"""What the children in a party add to the nightly rate.

	By default every child is charged, which suits a room sold per person.
	A whole-place let is different: the rate buys the house up to its base
	occupancy, so a family of six adults and two children in a house that
	sleeps eight has already paid for all eight - charging the children again
	bills the same beds twice. `children_within_base_occupancy` switches to
	that reading, where children fill the base occupancy alongside adults and
	only the guests beyond it are charged.

	A small-group rate is already per head, so every child is charged there
	whichever way the flag is set.
	"""
	per_child = _dec(rt.child_price)
	if not children or not per_child:
		return Decimal(0)
	if priced_per_head or not rt.get("children_within_base_occupancy"):
		return children * per_child
	base = int(rt.base_occupancy or 2)
	# adults take the base occupancy first; children fill what is left
	free_seats = max(0, base - adults)
	return max(0, children - free_seats) * per_child


def party_by_age(rt, adults: int, child_ages) -> tuple[int, int, int]:
	"""Split a party's children by the room type's age bands.

	Returns (adults, children, infants). A child at or under `free_child_age`
	is an infant: free, and not counted toward the price tier or the room's
	capacity. Above `child_age_limit` a child is charged as an adult.
	"""
	free_age = rt.get("free_child_age")
	free_age = 5 if free_age is None else int(free_age)
	limit = rt.get("child_age_limit")
	limit = 11 if limit is None else int(limit)
	ages = [int(a) for a in (child_ages or []) if str(a).strip() != ""]
	infants = sum(1 for a in ages if a <= free_age)
	older = sum(1 for a in ages if a > limit)
	return (int(adults or 0) + older, len(ages) - infants - older, infants)


def capacity_overflow(cap, adults: int, children: int,
                      extra_guests: int = 0) -> tuple[int, int]:
	"""(guests above what the room sleeps, extra guests it may take).

	Guests above capacity arrive either as extra guests on top of a full room
	or, from older bookings and API callers, counted inside adults or
	children - never both, so the two are not added.

	`max_total_occupants` (when set) caps adults + children together, so a
	room that sleeps 6 adults or 6 children still refuses 6 + 1 (#342).
	"""
	cap_a = int(cap.get("adults_capacity") or 0)
	cap_c = int(cap.get("children_capacity") or 0)
	overflow = ((max(0, int(adults or 0) - cap_a) if cap_a else 0)
	            + (max(0, int(children or 0) - cap_c) if cap_c else 0))
	max_total = int(cap.get("max_total_occupants") or 0)
	if max_total:
		total = int(adults or 0) + int(children or 0)
		overflow = max(overflow, max(0, total - max_total))
	return max(int(extra_guests or 0), overflow), int(cap.get("max_extra_beds") or 0)


def forecast_occupancy(property: str, date,
                       exclude: str | None = None) -> float:
	"""Property occupancy for a future date: live reservations covering that
	night over sellable rooms. This is the demand signal hurdle tiers key on.

	`exclude` leaves one reservation out - re-quoting a saved booking must
	not count the booking itself, or an amendment would price against a
	house the original booking never saw."""
	total = frappe.db.count("Room", {"property": property})
	if not total:
		return 0.0
	sold = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabReservation`
		   WHERE property = %(p)s AND status IN ('Confirmed', 'Checked In')
		     AND name != %(x)s
		     AND check_in_date <= %(d)s
		     AND GREATEST(check_out_date,
		                  DATE_ADD(check_in_date, INTERVAL 1 DAY)) > %(d)s""",
		{"p": property, "d": str(date), "x": exclude or ""})[0][0]
	return round(sold / total * 100, 1)


def demand_tier(property: str, room_type: str, date,
                occupancy: float | None = None) -> dict | None:
	"""The active hurdle tier for a date: the highest occupancy threshold the
	forecast has crossed (room-type-specific rows beat property-wide ones).
	Returns {occupancy, occupancy_from, premium_pct, min_rate} or None."""
	occ = forecast_occupancy(property, date) if occupancy is None else occupancy
	rows = frappe.get_all(
		"Hurdle Rate",
		filters={"property": property, "disabled": 0,
		         "occupancy_from": ("<=", occ),
		         "room_type": ("in", [room_type, "", None])},
		fields=["room_type", "occupancy_from", "premium_pct", "min_rate"],
		order_by="occupancy_from desc")
	if not rows:
		return None
	specific = [r for r in rows if r.room_type == room_type]
	t = specific[0] if specific else rows[0]
	return {"occupancy": occ, "occupancy_from": float(t.occupancy_from),
	        "premium_pct": float(t.premium_pct or 0),
	        "min_rate": float(t.min_rate or 0)}


def demand_adjust(property: str, room_type: str, date, rate: Decimal,
                  occupancy: float | None = None) -> tuple[Decimal, dict | None]:
	"""Apply the active demand tier: premium % on top of the season rate,
	then the hurdle floor - the rate never sells below the tier's minimum."""
	tier = demand_tier(property, room_type, date, occupancy)
	if not tier:
		return rate, None
	rate = rate * (Decimal(1) + _dec(tier["premium_pct"]) / Decimal(100))
	if tier["min_rate"] and rate < _dec(tier["min_rate"]):
		rate = _dec(tier["min_rate"])
	return rate, tier


def days_cover(days_of_week: str | None, day_abbr: str) -> bool:
	"""Does a comma-separated day list cover this weekday?

	A blank list runs every day; otherwise the weekday has to be listed, with
	the weekend/weekday shorthands expanded. Shared by seasons, rate
	guardrails and small-group pricing so all three read a day list the same
	way - note that "weekend" means Sat+Sun, so a property whose weekend is
	Fri+Sat spells the days out.
	"""
	if not days_of_week:
		return True
	normalized_days = set()
	for d in (x.strip().lower() for x in str(days_of_week).split(",")):
		if d in ("weekend", "weekends"):
			normalized_days.update(["sat", "sun"])
		elif d in ("weekday", "weekdays"):
			normalized_days.update(["mon", "tue", "wed", "thu", "fri"])
		else:
			normalized_days.add(d[:3])
	return day_abbr in normalized_days


def rail_for_day(guardrails, room_type: str | None, day_abbr: str):
	"""The rate guardrail that governs this weekday, or None. The most
	specific rail wins: room-type + days, then room-type, then
	property-wide + days, then property-wide."""
	ranked = [((1 if g.room_type == room_type else 0,
	            1 if g.days_of_week else 0), g)
	          for g in guardrails if days_cover(g.days_of_week, day_abbr)]
	return max(ranked, key=lambda x: x[0])[1] if ranked else None


def long_stay_rule(property: str, room_type: str, nights: int):
	"""The Long Stay Discount a stay of `nights` earns, or None.

	The highest tier the stay reaches wins (7+ nights 10% and 28+ nights
	25%: a 30-night stay gets 25%); at the same tier a room-type rule beats
	a house-wide one."""
	if nights < 1:
		return None
	rules = frappe.get_all(
		"Long Stay Discount",
		filters={"property": property, "disabled": 0,
		         "min_nights": ("<=", nights),
		         "room_type": ("in", [room_type, "", None])},
		fields=["name", "min_nights", "discount_percent", "room_type"])
	if not rules:
		return None
	return max(rules, key=lambda r: (int(r.min_nights or 0),
	                                 1 if r.room_type else 0))


def _season_covers_day(season, day_abbr: str) -> bool:
	return days_cover(season.days_of_week, day_abbr)


def active_season(property: str, date, room_type: str | None = None):
	"""The season that prices `date` for this room type, or None.

	A season that names a room type moves only that room type's rate; one
	with the field left blank is house-wide - "blank = all", as the Seasons
	form says. The room-type season wins over a house-wide one and priority
	breaks the tie inside each group, the same most-specific-wins rule the
	rate guardrails use. Called without a room_type only house-wide seasons
	apply, so one room type's rate can never be charged for another.
	"""
	seasons = frappe.get_all(
		"Season",
		filters={
			"property": property,
			"disabled": 0,
			"start_date": ("<=", date),
			"end_date": (">=", date),
			"room_type": ("in", [room_type, "", None]),
		},
		fields=["name", "season_name", "adjustment_type", "adjustment_value",
		        "days_of_week", "room_type", "priority"],
		order_by="priority desc",
	)
	if not seasons:
		return None

	from frappe.utils import getdate
	day_abbr = getdate(date).strftime("%a").lower()
	todays = [s for s in seasons if _season_covers_day(s, day_abbr)]
	if not todays:
		return None

	# most specific wins, then priority - `todays` is already priority desc,
	# so max() keeps the highest-priority season within each group
	return max(todays, key=lambda x: (1 if x.room_type else 0,
	                                  float(x.priority or 0)))


def season_adjust(property: str, date, base: Decimal,
                  room_type: str | None = None) -> Decimal:
	"""Apply the active season covering `date` for this room type."""
	s = active_season(property, date, room_type)
	if not s:
		return base
	v = _dec(s.adjustment_value)
	if s.adjustment_type == "Percent":
		return base * (Decimal(1) + v / Decimal(100))
	if s.adjustment_type == "Amount":
		return base + v
	return v  # Absolute


def active_holiday_package(property: str, check_in, check_out,
                           room_type: str | None = None):
	"""The active Holiday Package that prices this stay, or None.

	A stay qualifies for a holiday package when:
	1. Stay dates fall within package bounds (start_date <= check_in, last_night <= end_date).
	2. Stay meets or exceeds package minimum_nights.
	3. Room-type specific package beats house-wide, priority breaks ties.
	"""
	from frappe.utils import add_days, date_diff, getdate

	nights = max(1, int(date_diff(check_out, check_in) or 0))
	last_night = add_days(check_in, nights - 1)

	packages = frappe.get_all(
		"Holiday Package",
		filters={
			"property": property,
			"disabled": 0,
			"start_date": ("<=", last_night),
			"end_date": (">=", check_in),
			"room_type": ("in", [room_type, "", None]),
		},
		fields=["name", "package_name", "room_type", "start_date", "end_date",
		        "package_nights", "minimum_nights", "extra_night_percent", "extra_night_price",
		        "price_1_pax", "price_2_pax", "price_3_pax",
		        "price_4_pax", "price_5_pax", "extra_adult_price", "child_price",
		        "flyer_image", "ota_strategy", "ota_daily_rate", "advance_percent",
		        "non_refundable", "inclusions", "priority"],
		order_by="priority desc",
	)
	if not packages:
		return None

	valid = []
	for p in packages:
		# If any night of the stay falls within the package window [start_date, end_date], the package applies
		if getdate(check_in) <= getdate(p.end_date) and getdate(last_night) >= getdate(p.start_date):
			valid.append(p)

	if not valid:
		return None

	return max(valid, key=lambda x: (1 if x.room_type else 0, float(x.priority or 0)))


def package_covering_date(property: str, date, room_type: str | None = None):
	"""The Holiday Package covering this single date, or None."""
	packages = frappe.get_all(
		"Holiday Package",
		filters={
			"property": property,
			"disabled": 0,
			"start_date": ("<=", date),
			"end_date": (">=", date),
			"room_type": ("in", [room_type, "", None]),
		},
		fields=["name", "package_name", "room_type", "start_date", "end_date",
		        "package_nights", "minimum_nights", "extra_night_percent", "extra_night_price",
		        "price_1_pax", "price_2_pax", "price_3_pax",
		        "price_4_pax", "price_5_pax", "extra_adult_price", "child_price",
		        "flyer_image", "ota_strategy", "ota_daily_rate", "priority"],
		order_by="priority desc",
	)
	if not packages:
		return None
	return max(packages, key=lambda x: (1 if x.room_type else 0, float(x.priority or 0)))


def room_gst_rate(property: str, room_type_doc, nightly_rate: Decimal) -> Decimal:
	"""Room-night tax rate, resolved by the property's localization pack
	(India = GST slab/fixed; other countries = their own). Kept named
	room_gst_rate for its many callers; the logic lives in the pack."""
	from kamra.localization import pack_for
	return pack_for(property).calculate_room_tax(
		property, room_type_doc, nightly_rate)


def rates_include_tax(property: str | None) -> bool:
	if not property:
		return False
	return bool(frappe.get_cached_doc("Property", property).get("rates_include_tax"))


def selling_currency(property: str | None) -> str | None:
	"""The currency a property prices in: its own, else its country pack's."""
	if not property:
		return None
	from kamra.localization import default_currency, pack_for
	return frappe.get_cached_doc("Property", property).currency or default_currency(pack_for(property))


def night_rate_places(property: str | None) -> int:
	"""The decimals a booking's locked night rate keeps: the ISO 4217 minor
	unit of the property's currency (three for KWD), never fewer than two.
	The net share of a tax-inclusive price is not a whole UGX: 127,118.64 of
	a 150,000 night at 18%, where 127,119 would bill 150,000.42."""
	return max(2, _minor_unit(selling_currency(property)))


def stay_season_rules(property: str, check_in, check_out,
                      room_type: str | None = None) -> dict:
	"""The strictest season rules a stay runs into.

	Peak dates carry more than a price: New Year wants a two-night minimum,
	December wants no refunds. Those live on the Season that already prices
	the date, so one record covers the whole rule rather than a separate
	calendar nobody remembers to update.

	Returns the longest minimum stay any covered night asks for, whether any
	of them is non-refundable or wants full payment on booking, whether one
	switches the small-group rate off, and the holiday the stay touches.
	"""
	nights = max(1, int(date_diff(check_out, check_in) or 0))
	seasons = frappe.get_all(
		"Season",
		filters={"property": property, "disabled": 0,
		         "start_date": ("<=", add_days(check_in, nights - 1)),
		         "end_date": (">=", check_in),
		         "room_type": ("in", [room_type, "", None])},
		fields=["season_name", "start_date", "end_date", "days_of_week",
		        "minimum_nights", "non_refundable", "full_payment",
		        "no_small_group", "is_holiday", "advance_percent"])
	min_nights, non_refundable, named = 0, False, None
	advance_pct, advance_season = 0.0, None
	full_payment = no_small_group = False
	holiday = None
	for i in range(nights):
		date = add_days(check_in, i)
		day_abbr = getdate(date).strftime("%a").lower()
		for s in seasons:
			if not (getdate(s.start_date) <= getdate(date) <= getdate(s.end_date)):
				continue
			if not days_cover(s.days_of_week, day_abbr):
				continue
			if int(s.minimum_nights or 0) > min_nights:
				min_nights, named = int(s.minimum_nights), s.season_name
			if s.non_refundable:
				non_refundable = True
			full_payment = full_payment or bool(s.full_payment)
			if float(s.advance_percent or 0) > advance_pct:
				advance_pct = float(s.advance_percent)
				advance_season = s.season_name
			no_small_group = no_small_group or bool(s.no_small_group)
			if s.is_holiday and not holiday:
				holiday = s.season_name

	pkg = active_holiday_package(property, check_in, check_out, room_type)
	if pkg:
		pkg_nights = int(getattr(pkg, "package_nights", None) or getattr(pkg, "minimum_nights", None) or 1)
		if pkg_nights > min_nights:
			min_nights, named = pkg_nights, pkg.package_name
		if pkg.non_refundable:
			non_refundable = True
		if float(pkg.advance_percent or 0) > advance_pct:
			advance_pct = float(pkg.advance_percent)
			advance_season = pkg.package_name
		holiday = pkg.package_name

	return {"minimum_nights": min_nights, "non_refundable": non_refundable,
	        "season": named, "nights": nights, "full_payment": full_payment,
	        "no_small_group": no_small_group, "is_holiday": bool(holiday),
	        "holiday": holiday, "advance_percent": advance_pct,
	        "advance_season": advance_season,
	        "package": pkg.package_name if pkg else None}


def advance_percent(property: str, stay_rules: dict) -> tuple[float, str | None]:
	"""The advance % a booking owes and the season that set it.

	A season on the stay's dates with an advance % sets it (the highest
	one when the stay crosses seasons, as the other season rules take the
	strictest); otherwise the property's usual "Deposit expected at
	booking". (pct, season name or None)."""
	season = float(stay_rules.get("advance_percent") or 0)
	if season > 0:
		return season, stay_rules.get("advance_season")
	return float(frappe.get_cached_value("Property", property,
	                                     "deposit_pct") or 0), None


def validate_voucher(property: str, voucher_code: str, nights: int,
                     check_limits: bool = True):
	"""Return the voucher doc if valid, else throw with a guest-readable reason.

	check_limits=False skips the active flag, validity dates and use limit.
	Re-pricing a booking that already holds the voucher passes it: the
	booking was accepted when it was made, and its own use would otherwise
	count against the limit (or the voucher would have expired since)."""
	name = frappe.db.get_value(
		"Discount Voucher",
		{"property": property, "voucher_code": voucher_code.strip().upper()},
	)
	if not name:
		frappe.throw(f"Voucher '{voucher_code}' does not exist.")
	v = frappe.get_doc("Discount Voucher", name)
	if check_limits:
		today = getdate(nowdate())
		if v.disabled:
			frappe.throw(f"Voucher {v.voucher_code} is no longer active.")
		if v.valid_from and getdate(v.valid_from) > today:
			frappe.throw(f"Voucher {v.voucher_code} starts on {v.valid_from}.")
		if v.valid_to and getdate(v.valid_to) < today:
			frappe.throw(f"Voucher {v.voucher_code} expired on {v.valid_to}.")
		if v.max_uses and (v.times_used or 0) >= v.max_uses:
			frappe.throw(f"Voucher {v.voucher_code} has been fully redeemed.")
	if nights < (v.min_nights or 1):
		frappe.throw(
			f"Voucher {v.voucher_code} needs a stay of at least "
			f"{v.min_nights} nights."
		)
	return v


def consume_voucher_use(voucher: str) -> None:
	"""Count one redemption, under a lock, and refuse a code that is used up.

	The check in validate_voucher is only the friendly early answer. Two
	bookings can both pass it. This update runs in the same transaction as
	the booking and only increments while the code is still under its cap
	(max_uses 0 means unlimited).
	"""
	row = frappe.db.sql(
		"""SELECT voucher_code, IFNULL(max_uses, 0), IFNULL(times_used, 0)
		   FROM `tabDiscount Voucher` WHERE name = %s FOR UPDATE""",
		voucher)
	if not row:
		frappe.throw(f"Voucher '{voucher}' does not exist.")
	code, max_uses, times_used = row[0][0], int(row[0][1] or 0), int(row[0][2] or 0)
	if max_uses and times_used >= max_uses:
		frappe.throw(f"Voucher {code} has been fully redeemed.")
	frappe.db.sql(
		"""UPDATE `tabDiscount Voucher`
		   SET times_used = COALESCE(times_used, 0) + 1
		   WHERE name = %s
		     AND (IFNULL(max_uses, 0) = 0 OR COALESCE(times_used, 0) < max_uses)""",
		voucher)


def apply_rate_plan(rate_plan: str | None, room_total: Decimal,
                    extra_person_total: Decimal = Decimal(0), nights: int = 1) -> Decimal:
	"""The stay's room total after its rate plan's modifier.

	The plan works on the whole stay's (season- and occupancy-priced) room
	total: Percent scales it, Amount adds to it, Absolute replaces it. The
	quote calls this, and so does the folio when it posts a night, so a
	booking and its room nights can never disagree about the plan.

	A derived plan first takes its base plan's price (the base's own base
	first), then applies its own modifier. `extra_person_total` is the part
	of room_total extra adults and children add: a derived plan leaves that
	part as its base priced it unless the plan sets adjust_extra_person
	(OPERA Base Rate Extra Person Calculation). A derived plan's Amount or
	Absolute is per night (`nights` of them), as on a contract night; a
	root plan's stays per stay.
	"""
	room_total = _dec(room_total)
	if not rate_plan:
		return room_total
	return _price_through(resolve_rate_plan_chain(rate_plan), room_total, _dec(extra_person_total), root=True,
	                      nights=max(cint(nights), 1))


def _apply_modifier(rp, amount: Decimal, nights: int = 1) -> Decimal:
	v = _dec(rp.modifier_value)
	if rp.modifier_type == "Percent":
		return amount * (Decimal(1) + v / Decimal(100))
	if rp.modifier_type == "Amount":
		return amount + v * nights
	if rp.modifier_type == "Absolute":
		return v * nights
	return amount


def _price_through(plans: list, total: Decimal, extras: Decimal, root: bool, nights: int = 1) -> Decimal:
	"""Apply each plan's modifier in turn. A root plan (no base) works on the
	whole price, its amount once; a derived plan leaves the extra-person
	part as its base left it unless the plan's adjust_extra_person is set,
	and adds or fixes its amount for each of the `nights`."""
	extras = min(max(extras, Decimal(0)), max(total, Decimal(0)))
	room = total - extras
	for depth, rp in enumerate(plans):
		derived = bool(depth or not root)
		count = nights if derived else 1
		if derived and not cint(rp.get("adjust_extra_person")):
			room = _apply_modifier(rp, room, count)
			continue
		whole = room + extras
		priced = _apply_modifier(rp, whole, count)
		extras = extras * priced / whole if whole else Decimal(0)
		room = priced - extras
	return room + extras


def plan_stay_nights(rate_plan: str, nights: list[tuple[Decimal, Decimal]], extra_person_total: Decimal,
                     inclusive: bool, floor: Decimal = Decimal(0), *,
                     currency: str | None = None) -> tuple[list[Decimal], bool]:
	"""Each night's taxable room rate under the plan, for (taxable rate, GST
	%) nights before it. The plan's modifier works on the stay (a derived
	Amount or Absolute once per night), its result spread over the nights
	in proportion and held at the long-stay floor; the plan's rounding step
	then rounds each night's price as the guest sees it (OPERA per-night
	commercial rounding) - gross in a tax-inclusive house, net otherwise -
	unless that would take the stay below its floor. For 0%-tax nights when
	step is 0, amounts round to the currency's ISO 4217 minor unit so zero-
	tax / bridged stays never carry sub-unit fractions. Nights with no price
	of their own (an Absolute or Amount plan on a zero-priced room) share
	the plan's price evenly, the last night taking the remainder. The
	quote and the folio's unlocked nights both price through here. (rates,
	rounded): rounded when each night carries its own price to lock."""
	total = sum((_dec(rate) for rate, _gst in nights), Decimal(0))
	planned = max(apply_rate_plan(rate_plan, total, extra_person_total, nights=len(nights)), _dec(floor))
	rp = frappe.get_doc("Rate Plan", rate_plan)
	cur = currency or rp.get("currency")
	spread = bool(nights) and not total and planned > 0
	if spread:
		share = round_rate_amount(planned / len(nights), 0, currency=cur)
		rates = [share] * (len(nights) - 1) + [planned - share * (len(nights) - 1)]
	else:
		factor = planned / total if total else Decimal(1)
		rates = [_dec(rate) * factor for rate, _gst in nights]
	step = _dec(rp.get("rounding_step"))
	mode = rp.get("rounding_mode") or "Nearest"
	if step <= 0:
		rounded = [
			round_rate_amount(rate, 0, mode, cur) if (rate > 0 and not _dec(gst)) else rate
			for rate, (_rate, gst) in zip(rates, nights, strict=True)
		]
		if rounded != rates and sum(rounded, Decimal(0)) >= _dec(floor):
			return rounded, True
		return rates, spread
	rounded = []
	for rate, (_rate, gst) in zip(rates, nights, strict=True):
		tax = Decimal(1) + _dec(gst) / Decimal(100) if inclusive else Decimal(1)
		rounded.append(round_rate_amount(rate * tax, step, mode) / tax if rate > 0 else rate)
	if sum(rounded, Decimal(0)) < _dec(floor):
		return rates, spread
	return rounded, True


def rate_plan_factor(rate_plan: str | None, room_total: Decimal) -> Decimal:
	"""What each night of a stay is multiplied by under its rate plan.

	The quote applies the plan to the stay total and scales the room tax by
	the same ratio, which spreads the plan over the nights in proportion to
	their rates. A night's share of the planned total is therefore its rate
	times this factor - the folio uses it so each posted room night is
	exactly the night the guest was quoted."""
	room_total = _dec(room_total)
	if not rate_plan or not room_total:
		return Decimal(1)
	return apply_rate_plan(rate_plan, room_total) / room_total


def negotiated_rate_plan(property: str, company: str | None) -> str | None:
	"""The rate plan a company negotiated, as it applies at *property*.

	Corporate Accounts are chain-wide but rate plans belong to a property:
	the company's plan applies when it is this property's (and enabled);
	at a sister property the plan with the same code stands in for it.
	None when the company has no plan, or none fits here."""
	if not company:
		return None
	plan = frappe.db.get_value("Corporate Account", company,
	                           "negotiated_rate_plan")
	if not plan:
		return None
	rp = frappe.db.get_value("Rate Plan", plan,
	                         ["name", "property", "code", "disabled"],
	                         as_dict=True)
	if not rp:
		return None
	if rp.property == property:
		return None if rp.disabled else rp.name
	if rp.code:
		return frappe.db.get_value(
			"Rate Plan", {"property": property, "code": rp.code, "disabled": 0},
			"name")
	return None


def billable_children(reservation) -> int:
	"""Chargeable children for a stay: total children less free children
	(occupants at or under the room type's free_child_age, or recorded
	infants, whichever is higher). Shared by quote_inputs,
	capture_contract_tariff and contract_tariff_for_new_dates so quotes,
	bookings, amendments and the tariff resolver agree."""
	free_children = 0
	free_child_age = frappe.db.get_value("Room Type", reservation.room_type, "free_child_age")
	free_child_age = 6 if free_child_age is None else int(free_child_age)
	for occ in reservation.get("occupants") or []:
		age = occ.get("age") if isinstance(occ, dict) else getattr(occ, "age", None)
		if age and int(age) <= free_child_age:
			free_children += 1
	infants = cint(reservation.get("infants"))
	if infants > free_children:
		free_children = infants
	return max(0, cint(reservation.get("children")) - free_children)


class ContractRateRefused(frappe.ValidationError):
	"""The resolver did not authorize a price for this option."""


def contract_rate(property, room_type, rate_plan, check_in, check_out, adults, children, extra_beds=0, *,
                  company=None, pricing_event="reservation", fx_date=None):
	"""Resolve an optional contract without reading the provider's records.

	The resolver is told the plan's exchange-rate lock and rounding, the
	pricing event ("reservation", "check_in" or "posting") with the FX
	date it prices at, and the company for a customer price. An older
	resolver ignores what it does not know. A derived plan with no
	contract of its own takes its base plan's contract and applies its own
	modifier per night, in the contract currency, and its local rate is
	held to the property's Rate Guardrails as the resolver holds a base
	plan's. The local rate is rounded to the plan's rounding step; a
	resolver's figure is otherwise kept.

	A stay over a season or tariff change (OPERA Rate Changes During Stay,
	SAP condition validity) comes back with `nights`: one {date, nightly,
	agreed_rate, item_price} per stay night in date order (with its own
	extra_person when that renews too), the scalar figures being the
	check-in night's. Each night is derived, rounded and checked on its own
	and kept as tariff["nights"]; a list that does not cover the stay night
	by night, or whose first night is not the scalar figures, is refused.
	"nights": None is a one-rate answer."""
	if not rate_plan:
		return None
	from kamra.erp_bridge import bridge_call
	chain = resolve_rate_plan_chain(rate_plan)
	plan = chain[-1]
	form = {"property": property, "room_type": room_type, "check_in": str(check_in), "check_out": str(check_out),
	        "adults": cint(adults), "children": cint(children), "extra_beds": cint(extra_beds),
	        "fx_lock_policy": plan.get("fx_lock_policy") or DEFAULT_FX_LOCK,
	        "rounding_step": float(_dec(plan.get("rounding_step"))),
	        "rounding_mode": plan.get("rounding_mode") or "Nearest", "pricing_event": pricing_event}
	if company:
		form["company"] = company
	if fx_date:
		form["fx_date"] = str(getdate(fx_date))
	# the plan's own contract, else the nearest base plan's
	for depth in range(len(chain) - 1, -1, -1):
		response = bridge_call("hospitality_tariffs.tariff_rate", **form, rate_plan=chain[depth].name) or {}
		result = response.get("tariff", response)
		if result and result.get("refusal"):
			raise ContractRateRefused(result["refusal"])
		if result and cint(result.get("is_tariff")):
			break
	else:
		return None
	try:
		agreed, nightly, fx = (_dec(result.get(k)) for k in ("agreed_rate", "nightly", "fx_rate"))
		if not all(value.is_finite() and value > 0 for value in (agreed, nightly, fx)):
			raise ValueError
		price_date = str(getdate(result["fx_date"])) if result.get("fx_date") else None
		currency = result["currency"]
		if not currency or not price_date:
			raise ValueError
		nights = (_resolver_nights(result, check_in, check_out) if result.get("nights") is not None
		          else None)
	except (KeyError, TypeError, ValueError, InvalidOperation):
		raise ContractRateRefused(_("The contract rate is incomplete. Ask a manager to check the tariff."))
	prop_currency = property_currency(property)
	step, mode = plan.get("rounding_step"), plan.get("rounding_mode") or "Nearest"
	derived = chain[depth + 1:]

	def price_night(nightly, agreed, extra_person=None):
		if derived:
			extras = _dec(result.get("extra_person") if extra_person is None else extra_person)
			nightly = _price_through(derived, nightly, extras, root=False)
			if nightly <= 0:
				raise ContractRateRefused(_("The derived rate plan prices this stay at zero or less. "
				                            "Ask a manager to check the rate plan."))
			nightly = round_rate_amount(nightly, 0, currency=currency)
			agreed = contract_local_amount(plan, nightly, fx, prop_currency)
		elif _dec(step) > 0:
			agreed = round_rate_amount(agreed, step, mode, prop_currency)
		return nightly, agreed

	# the scalars are the first night's: its own extra-person charge when the resolver gave one
	nightly, agreed = price_night(nightly, agreed, nights[0]["extra_person"] if nights else None)
	tariff = {"currency": currency, "nightly": float(nightly), "fx_rate": float(fx),
	          "fx_date": price_date, "property_currency": prop_currency}
	if nights is not None:
		tariff["nights"] = []
		for night in nights:
			night_nightly, night_agreed = price_night(night["nightly"], night["agreed_rate"], night["extra_person"])
			tariff["nights"].append({"date": night["date"], "nightly": float(night_nightly),
			                         "agreed_rate": float(night_agreed), "item_price": night["item_price"]})
	if derived:
		tariff["derived_from"] = chain[depth].name
		priced = ([(night["date"], _dec(night["agreed_rate"])) for night in tariff["nights"]] if nights is not None
		          else [(day, agreed) for day in _stay_nights(check_in, check_out)])
		_hold_to_guardrails(property, room_type, priced)
	return float(agreed), tariff


def _resolver_nights(result: dict, check_in, check_out) -> list[dict]:
	"""The resolver's per-night tariff (result["nights"]), checked: exactly
	one {date, nightly, agreed_rate, item_price} for each night of the stay
	(its check-in date for a day-use stay), in date order, each amount above
	zero, an optional extra_person at zero or more; the first night is the
	scalar nightly, agreed_rate and extra_person. Anything else raises
	ValueError. A night's extra_person is None when it has none."""
	raw = result["nights"]
	if not isinstance(raw, list):
		raise ValueError
	dates = _stay_nights(check_in, check_out)
	if len(raw) != len(dates):
		raise ValueError
	nights = []
	for day, night in zip(dates, raw, strict=True):
		if not isinstance(night, dict) or str(night.get("date") or "")[:10] != day:
			raise ValueError
		nightly, agreed = _dec(night["nightly"]), _dec(night["agreed_rate"])
		if not all(value.is_finite() and value > 0 for value in (nightly, agreed)):
			raise ValueError
		extra = None if night.get("extra_person") is None else _dec(night["extra_person"])
		if extra is not None and not (extra.is_finite() and extra >= 0):
			raise ValueError
		nights.append({"date": day, "nightly": nightly, "agreed_rate": agreed, "extra_person": extra,
		               "item_price": night.get("item_price")})
	first = nights[0]
	if (first["nightly"], first["agreed_rate"]) != (_dec(result.get("nightly")), _dec(result.get("agreed_rate"))):
		raise ValueError
	if first["extra_person"] is not None and result.get("extra_person") is not None \
			and first["extra_person"] != _dec(result["extra_person"]):
		raise ValueError
	return nights


def _hold_to_guardrails(property: str, room_type: str, nights: list) -> None:
	"""A derived contract's local rate, (date, rate) a night, within the
	property's Rate Guardrails: the most specific rail for each night's
	weekday (rail_for_day), as hospitality_tariffs.tariff_rate holds a
	base plan's tariff (HSP-TARIFF-004). Refused below its floor or above
	its ceiling."""
	rails = [rail for rail in frappe.get_all(
		"Rate Guardrail", filters={"property": property, "disabled": 0},
		fields=["room_type", "floor_price", "ceiling_price", "days_of_week"])
		if not rail.room_type or rail.room_type == room_type]
	if not rails:
		return
	for day, rate in nights:
		rail = rail_for_day(rails, room_type, getdate(day).strftime("%a").lower())
		if rail and rate < _dec(rail.floor_price):
			raise ContractRateRefused(_(
				"The derived rate plan prices the night of {0} below the Rate Guardrail floor. Revenue "
				"Management reviews the rate plan or the guardrail.").format(day))
		if rail and rate > _dec(rail.ceiling_price):
			raise ContractRateRefused(_(
				"The derived rate plan prices the night of {0} above the Rate Guardrail ceiling. Revenue "
				"Management reviews the rate plan or the guardrail.").format(day))


def contract_night_rates(reservation) -> dict[str, float]:
	"""Each night's own contract rate, {date: agreed_rate}, from the
	booking's frozen contract: a stay over a tariff change (OPERA Rate
	Changes During Stay), while its agreed rate is still the contract as
	booked. A manager's agreed rate (set_agreed_rate) or a fixed rate is one
	price for every night (OPERA Fixed Rate), so it is empty then - and for
	a contract with one rate for the stay."""
	if cint(reservation.get("fixed_rate")):
		return {}
	snapshot = saved_contract_tariff(reservation)
	nights = (snapshot.get("tariff") or {}).get("nights") or []
	booked = snapshot.get("agreed_rate")
	if not nights or booked is None or abs(_dec(reservation.get("agreed_rate")) - _dec(booked)) > Decimal("0.005"):
		return {}
	return {str(night["date"]): float(night["agreed_rate"]) for night in nights}


def contract_tariff_for_new_dates(reservation, booked_check_in, booked_check_out,
                                  *, booked_agreed_rate=None) -> dict | None:
	"""The booking's frozen contract for its new dates (amend_stay, an early
	check-out, any save that moves them), or None when it does not change.

	The nights the contract already priced keep their locked rates. A night
	it never priced is resolved now through the tariff resolver (OPERA
	re-prices added nights from the rate code), at the booking's exchange
	rate date, and a night with no valid price refuses the new dates rather
	than bill another night's tariff, as does a night priced in another
	currency than the contract's (the rate plan's currency changed since
	booking: the contract is re-priced first, never re-denominated). A night that left the stay leaves the
	contract. When the contract's nights change, the stay's headline rate
	(contract_tariff["agreed_rate"] and tariff["nightly"]) follows the new
	first night (OPERA Daily Details / refresh-all-nights). A one-rate
	contract stays one rate while its rate still prices every night. A
	manager's agreed rate, a fixed rate (OPERA Fixed Rate), a stay priced by
	hand or by its group block keeps its price: None."""
	if not cint(reservation.get("auto_price")) or reservation.get("group_booking") \
			or cint(reservation.get("fixed_rate")):
		return None
	snapshot = saved_contract_tariff(reservation)
	tariff = snapshot.get("tariff") or {}
	booked = snapshot.get("agreed_rate")
	previous_agreed = reservation.get("agreed_rate") if booked_agreed_rate is None else booked_agreed_rate
	if not tariff.get("currency") or booked is None \
			or abs(_dec(previous_agreed) - _dec(booked)) > Decimal("0.005"):
		return None
	stay = _stay_nights(reservation.check_in_date, reservation.check_out_date)
	own = tariff.get("nights")
	if own:
		held = {str(night["date"]): night for night in own}
	elif booked_check_in and booked_check_out:
		held = {day: {"date": day, "nightly": tariff["nightly"], "agreed_rate": booked, "item_price": None}
		        for day in _stay_nights(booked_check_in, booked_check_out)}
	else:
		return None
	added = [day for day in stay if day not in held]
	if not added and (not own or [str(night["date"]) for night in own] == stay):
		return None
	runs: list[list[str]] = []
	for day in added:
		if runs and str(getdate(add_days(runs[-1][-1], 1))) == day:
			runs[-1].append(day)
		else:
			runs.append([day])
	# OPERA Cloud Managing Reservation Linked Profiles: changing the billing company
	# via change_stay_company must not re-price extensions; use the pricing company
	# frozen at booking when present (falling back to reservation.company for legacy stays).
	pricing_company = snapshot["company"] if "company" in snapshot else reservation.get("company")
	for run in runs:
		start, end = run[0], str(getdate(add_days(run[-1], 1)))
		resolved = contract_rate(reservation.property, reservation.room_type, reservation.rate_plan, start, end,
			reservation.adults, billable_children(reservation), reservation.get("extra_beds"),
			company=pricing_company, fx_date=tariff.get("fx_date"))
		if not resolved:
			frappe.throw(_("The contract has no valid rate for the added nights {0} to {1}. Ask a manager to check "
			               "the tariff, or keep the stay's dates.").format(start, end), ContractRateRefused)
		agreed, priced = resolved
		if priced.get("currency") != tariff["currency"]:
			# a rate code's currency change never re-denominates a booking: OPERA refreshes it first
			frappe.throw(_("The rate plan now prices the added nights {0} to {1} in {2}, but this stay's contract "
			               "is in {3}. Ask a manager to re-price the stay's contract, or keep the stay's dates.")
			             .format(start, end, priced.get("currency"), tariff["currency"]), ContractRateRefused)
		by_date = {night["date"]: night for night in priced.get("nights") or []}
		for day in run:
			night = by_date.get(day) or {"nightly": priced["nightly"], "agreed_rate": agreed}
			held[day] = {"date": day, "nightly": night["nightly"], "agreed_rate": night["agreed_rate"],
			             "item_price": night.get("item_price")}
	nights = [held[day] for day in stay]
	if not own and all(_dec(night["nightly"]) == _dec(tariff["nightly"])
	                   and abs(_dec(night["agreed_rate"]) - _dec(booked)) <= Decimal("0.005") for night in nights):
		return None
	first = nights[0]
	if booked_agreed_rate is not None \
			and abs(_dec(reservation.get("agreed_rate")) - _dec(booked)) > Decimal("0.005") \
			and abs(_dec(reservation.get("agreed_rate")) - _dec(first["agreed_rate"])) > Decimal("0.005"):
		return None
	return {**snapshot, "agreed_rate": float(first["agreed_rate"]),
	        "tariff": {**tariff, "nightly": float(first["nightly"]), "nights": nights}}


def _stay_nights(check_in, check_out) -> list[str]:
	"""Each night of a stay by date (its check-in date for a day-use stay)."""
	return [str(getdate(add_days(check_in, i))) for i in range(max(date_diff(check_out, check_in), 1))]


def relock_contract_fx_at_check_in(reservation) -> bool:
	"""OPERA Exchange Type "Fixed at Check In": a foreign contract converts
	at the posting rate of the check-in business date, not the booking's.

	Only the exchange rate moves: the booked contract amount (tariff
	nightly, a derived plan's modifier already in it) is converted again at
	the governed Currency Exchange rate for the business date and rounded
	as the plan rounds - the Item Price is never re-read, so a tariff
	revised after booking never re-prices the stay. The new agreed rate and
	snapshot go on the (unsaved) booking; its save re-locks the nights not
	yet charged. A booking whose agreed rate no longer matches the contract
	(a manager's override or a reservation discount) keeps it. The tariff
	resolver's own re-lock (kamra_fx_checkin_relock) is asked for on stays
	it priced (custom_tariff_currency) and on derived foreign-currency
	contract stays whose currency resolves through their base plan chain;
	OTA channel stays re-lock under kamra_contract_relocked only. No
	published rate refuses the check-in.
	A contract over a tariff change converts each booked night's own
	amount. A fixed rate is the manager's price and is never relocked.
	True when the booking was relocked."""
	if rate_plan_fx_policy(reservation.get("rate_plan")) != "Fixed at Check In":
		return False
	if cint(reservation.get("fixed_rate")):
		return False
	snapshot = saved_contract_tariff(reservation)
	tariff = snapshot.get("tariff") or {}
	home = tariff.get("property_currency") or property_currency(reservation.property)
	currency = tariff.get("currency")
	nightly = _dec(tariff.get("nightly"))
	if not currency or currency == home or nightly <= 0:
		return False
	booked = snapshot.get("agreed_rate")
	if booked is not None and abs(_dec(reservation.get("agreed_rate")) - _dec(booked)) > Decimal("0.005"):
		return False  # an override or a discount is the manager's price now
	from kamra.business_date import get_business_date
	business = get_business_date(reservation.property)
	posting = posting_fx_rate(reservation.property, currency, business)
	if not posting:
		raise ContractRateRefused(_(
			"No single published {0} exchange rate for {1}. Finance enters the current rate before this "
			"contract stay checks in.").format(currency, business))
	fx, fx_date = posting
	rp = frappe.get_doc("Rate Plan", reservation.rate_plan)
	agreed = float(contract_local_amount(rp, nightly, fx, home))
	relocked = {**tariff, "nightly": float(nightly), "fx_rate": float(fx), "fx_date": fx_date}
	if tariff.get("nights"):
		relocked["nights"] = [{**night, "agreed_rate": float(contract_local_amount(rp, night["nightly"], fx, home))}
		                      for night in tariff["nights"]]
	reservation.agreed_rate = agreed
	reservation.contract_tariff = {**snapshot, "tariff": relocked, "agreed_rate": agreed}
	reservation.flags.kamra_contract_relocked = True
	if reservation.get("custom_tariff_currency") or (
		tariff.get("source") != "channel"
		and not rp.get("custom_tariff_price_list")
		and rp.get("base_rate_plan")
		and rate_plan_currency(resolve_rate_plan_chain(rp)) != home
	):
		reservation.flags.kamra_fx_checkin_relock = True
	return True


def tax_basis(property):
	"""The invoice's tax vocabulary. This does not change posted line tax."""
	from kamra.erp_bridge import bridge_call
	return bridge_call("hospitality_kamra.tax_basis", property=property)


def saved_contract_tariff(reservation):
	"""Return the original contract, never re-resolve it at today's rate."""
	snapshot = reservation.get("contract_tariff")
	return frappe.parse_json(snapshot) if snapshot else {}


def quote_with_contract(property, room_type, check_in_date, check_out_date, adults=2, children=0,
	                    meal_plan=None, rate_plan=None, voucher_code=None, *, extra_beds=0,
	                    discount_type=None, discount_value=0, night_discounts=None, **kwargs):
	"""Use the same quote math as a booking with an agreed room rate. A desk
	discount comes off the contract's agreed rate, which stays the gross."""
	if date_diff(check_out_date, check_in_date) < 0:
		frappe.throw(_("Check-out cannot be before check-in."))
	resolved = contract_rate(property, room_type,
		rate_plan or negotiated_rate_plan(property, kwargs.get("company")),
		check_in_date, check_out_date, adults, children, extra_beds, company=kwargs.get("company"))
	nights = {night["date"]: night["agreed_rate"] for night in (resolved[1].get("nights") or [])} if resolved else {}
	q = quote(property, room_type, check_in_date, check_out_date, adults, children,
	          meal_plan, rate_plan, voucher_code, agreed_rate=resolved[0] if resolved else 0,
	          agreed_nights=nights or None,
	          contract_tariff=resolved[1] if resolved else None,
	          discount_type=discount_type, discount_value=discount_value,
	          night_discounts=night_discounts, **kwargs)
	if resolved:
		q.update(agreed_rate=resolved[0], tariff=resolved[1])
	return q


# A typed nightly price is a sanity-checked figure, not a free-for-all: the
# bound is far above any real room rate in any currency but stops a stray
# digit string (or an overflow probe) from reaching the ledger.
MAX_NIGHT_PRICE = Decimal("100000000")


def refuse_with_night_prices(what: str):
	"""Nights priced by hand and `what` both set the room price: say so."""
	frappe.throw(
		_("Nightly prices typed by hand cannot be combined with {0}. "
		  "Clear the typed prices, or remove {0}.").format(what),
		title=_("Prices typed by hand"))


def normalize_night_prices(night_prices, check_in_date, check_out_date) -> dict:
	"""Validate the nightly prices a desk typed: [{date, price}, ...] (a JSON
	string works too). Returns {"YYYY-MM-DD": Decimal} - empty when none were
	typed. Nothing is coerced or dropped: a malformed payload, a date outside
	the stay, a repeated date, or a price that is not a finite number above
	zero (with at most 2 decimals) is refused with the reason."""
	if night_prices in (None, "", [], {}):
		return {}
	if isinstance(night_prices, str):
		try:
			night_prices = frappe.parse_json(night_prices)
		except Exception:
			frappe.throw(_("night_prices must be a list of {date, price}."))
	if not isinstance(night_prices, (list, tuple)):
		frappe.throw(_("night_prices must be a list of {date, price}."))
	if date_diff(check_out_date, check_in_date) < 1:
		frappe.throw(_("Nightly prices need an overnight stay; a day-use stay is priced by its time slot."))
	first, last = getdate(check_in_date), getdate(check_out_date)
	out: dict = {}
	for row in night_prices:
		if not isinstance(row, dict) or set(row) != {"date", "price"}:
			frappe.throw(_("Each night price must be exactly {date, price}."))
		try:
			day = getdate(row["date"])
		except Exception:
			day = None
		if not day or not isinstance(row["date"], str):
			frappe.throw(_("{0} is not a valid night.").format(row["date"]))
		if not first <= day < last:
			frappe.throw(_("{0} is not a night of this stay ({1} to {2}).").format(
				day, first, last))
		if str(day) in out:
			frappe.throw(_("The night {0} is priced twice.").format(day))
		raw = row["price"]
		if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
			frappe.throw(_("The price for {0} must be a number.").format(day))
		try:
			price = Decimal(str(raw).strip())
		except Exception:
			frappe.throw(_("The price for {0} must be a number.").format(day))
		if not price.is_finite() or price <= 0:
			frappe.throw(_("The price for {0} must be above zero.").format(day))
		if price > MAX_NIGHT_PRICE:
			frappe.throw(_("The price for {0} is above the highest price {product} accepts ({1}).").format(
				day, f"{MAX_NIGHT_PRICE:,.0f}", product=product_name()))
		if price.as_tuple().exponent < -2:
			frappe.throw(_("The price for {0} can have at most 2 decimals.").format(day))
		out[str(day)] = price
	return out


DISCOUNT_TYPES = ("Percent", "Amount")
# what one night of Daily Details may carry; `rate` is the client's echo of
# the night it was shown - the server prices the night, a client never does
NIGHT_DISCOUNT_KEYS = frozenset(
	{"date", "discount_type", "discount_value", "discount_reason", "fixed_rate", "rate"})
CENT = Decimal("0.01")


def _discount_entry(discount_type, discount_value, where: str, places: int = 2) -> tuple[str | None, Decimal]:
	"""(type, value) of one discount as the desk entered it: Percent (0 to
	100, at most 2 decimals) or Amount (zero or more, at most `places`
	decimals: 2 by default, 3 for KWD via night_rate_places). A value without
	a type, a type that is neither, or a figure that is not a number is
	refused - never coerced."""
	raw = discount_value
	kind = discount_type.strip() if isinstance(discount_type, str) else discount_type
	max_dec = places if kind == "Amount" else 2
	if raw in (None, ""):
		value = Decimal(0)
	else:
		if isinstance(raw, bool) or not isinstance(raw, (int, float, str, Decimal)):
			frappe.throw(_("The discount {0} must be a number.").format(where))
		try:
			value = Decimal(str(raw).strip())
		except (InvalidOperation, ValueError):
			frappe.throw(_("The discount {0} must be a number.").format(where))
		if not value.is_finite():
			frappe.throw(_("The discount {0} must be a number.").format(where))
		if value < 0:
			frappe.throw(_("The discount {0} can't be below zero.").format(where))
		if value.as_tuple().exponent < -max_dec:
			if max_dec == 2:
				frappe.throw(_("The discount {0} can have at most 2 decimals.").format(where))
			frappe.throw(_("The discount {0} can have at most {1} decimals.").format(where, max_dec))
	if kind in (None, ""):
		if value:
			frappe.throw(_("Pick Percent or Amount for the discount {0}.").format(where))
		return None, Decimal(0)
	if kind not in DISCOUNT_TYPES:
		frappe.throw(_("A discount is Percent or Amount, not {0}.").format(kind))
	if kind == "Percent" and value > 100:
		frappe.throw(_("A percentage discount {0} is between 0 and 100.").format(where))
	return kind, value


def normalize_discounts(discount_type, discount_value, night_discounts,
                        check_in_date, check_out_date, property: str | None = None) -> dict:
	"""Validate the desk's reservation discount: one for the stay and,
	optionally, Daily Details nights of their own ([{date, discount_type,
	discount_value, discount_reason, fixed_rate}], a JSON string works too).

	Returns {"stay": (type, value) or None, "nights": {date: {type, value,
	reason, fixed_rate}}}, or {} when there is no discount. A night listed
	with no value is that night taken out of the stay's discount. Dates
	outside the stay, a night listed twice and unknown keys are refused."""
	places = night_rate_places(property)
	kind, value = _discount_entry(discount_type, discount_value, _("for the stay"), places=places)
	stay = (kind, value) if kind and value > 0 else None
	nights: dict = {}
	if night_discounts not in (None, "", [], {}):
		if isinstance(night_discounts, str):
			try:
				night_discounts = frappe.parse_json(night_discounts)
			except Exception:
				night_discounts = None
		if not isinstance(night_discounts, (list, tuple)):
			frappe.throw(_("night_discounts must be a list of {date, discount_type, discount_value}."))
		first = getdate(check_in_date)
		# a day-use stay bills one date: its check-in
		last = max(getdate(check_out_date), getdate(add_days(first, 1)))
		for row in night_discounts:
			if not isinstance(row, dict) or "date" not in row or not set(row) <= NIGHT_DISCOUNT_KEYS:
				frappe.throw(_("Each night discount is {date, discount_type, discount_value, "
				               "discount_reason, fixed_rate}."))
			try:
				day = getdate(row["date"]) if isinstance(row["date"], str) else None
			except Exception:
				day = None
			if not day:
				frappe.throw(_("{0} is not a valid night.").format(row["date"]))
			if not first <= day < last:
				frappe.throw(_("{0} is not a night of this stay ({1} to {2}).").format(
					day, first, last))
			if str(day) in nights:
				frappe.throw(_("The night {0} has two discounts.").format(day))
			fixed = row.get("fixed_rate")
			if fixed not in (None, "", 0, 1, True, False, "0", "1"):
				frappe.throw(_("fixed_rate is 0 or 1."))
			k, v = _discount_entry(row.get("discount_type"), row.get("discount_value"),
			                       _("for {0}").format(day), places=places)
			reason = row.get("discount_reason")
			nights[str(day)] = {"type": k, "value": v,
			                    "reason": reason if isinstance(reason, str) and reason else None,
			                    "fixed_rate": cint(fixed)}
	if not stay and not nights:
		return {}
	return {"stay": stay, "nights": nights}


def entry_rate(rate, gst_rate, inclusive: bool, places: int = 2) -> Decimal:
	"""A night's room rate on the basis the desk sees and enters it: gross
	(to `places` decimals) where the property's rates include tax, else the
	net rate."""
	unit = Decimal(1).scaleb(-places)
	rate = _dec(rate)
	if inclusive:
		return (rate * (Decimal(1) + _dec(gst_rate) / Decimal(100))).quantize(
			unit, rounding=ROUND_HALF_UP)
	return rate.quantize(unit, rounding=ROUND_HALF_UP)


def contract_night_tariff(
	contract_tariff: dict | None,
	date: str | None = None,
	rate_plan: str | None = None,
) -> tuple[str, Decimal, Decimal] | None:
	"""(currency, nightly, fx_rate) for one night of a foreign-currency contract
	tariff, or None when the stay is not priced in a foreign tariff currency or
	when the rate plan applies a commercial `rounding_step` (in which case SAP
	pricing-procedure / OPERA Rate Code semantics take percentage discounts off
	the rounded gross night rate rather than the unrounded foreign figure).
	Accepts either the inner `tariff` dict or the full `contract_tariff` snapshot."""
	if not isinstance(contract_tariff, dict):
		return None
	if rate_plan and _dec(frappe.db.get_value("Rate Plan", rate_plan, "rounding_step")) > 0:
		return None
	tariff = contract_tariff.get("tariff") if isinstance(contract_tariff.get("tariff"), dict) else contract_tariff
	currency = tariff.get("currency")
	home = tariff.get("property_currency")
	fx = _dec(tariff.get("fx_rate"))
	if not currency or (home and currency == home) or fx <= 0:
		return None
	nightly = _dec(tariff.get("nightly"))
	if date and tariff.get("nights"):
		key = str(getdate(date))
		own = next((n for n in tariff["nights"] if str(n.get("date")) == key), None)
		if own is not None:
			nightly = _dec(own.get("nightly"))
	if nightly <= 0:
		return None
	return currency, nightly, fx


def discount_night(property, rt, base, base_gst, entry, kind, value, inclusive: bool,
                   night_over_cap=Decimal(0), date=None, *,
                   tariff_night: tuple[str, Decimal, Decimal] | None = None) -> tuple[Decimal, Decimal, Decimal]:
	"""One night's desk discount, from the night's own base: `base` (before
	tax) at `base_gst`, which the desk sees as `entry` (gross where rates
	include tax). A percentage or an amount comes off `entry`; on a foreign-
	currency contract tariff with no commercial rounding step (`tariff_night`
	whose minor-unit FX conversion equals `entry`), a percentage discount
	applies in the tariff's own currency first, then converts at the contract
	FX rate and rounds to the property currency's ISO minor unit (whole
	shillings for UGX). When a plan rounding step already rounded `entry`
	(SAP DIFF / OPERA rounding rule), the percentage comes off the rounded
	gross `entry` so the guest receives the exact percentage requested and
	percent agrees with an equivalent flat amount. The tax rate is read again
	from the discounted price (a slab can drop) and the tax backed out of it.
	Returns (net rate before tax, its tax rate, the discount on the desk's
	basis). The discount is what the guest is given: never negative, even when
	a lower slab leaves more of a cheaper gross before tax.

	Used when a booking is priced and when a booked stay's discount changes,
	so a night is discounted the same way both times."""
	if not (kind and value):
		return _dec(base), _dec(base_gst), Decimal(0)
	currency = selling_currency(property)
	entry_places = _minor_unit(currency)
	entry_unit = Decimal(1).scaleb(-entry_places)
	places = night_rate_places(property)
	unit = Decimal(1).scaleb(-places)
	entry = _dec(entry)
	use_foreign_pct = False
	if kind == "Percent" and tariff_night is not None:
		tariff_currency, tariff_nightly, fx_rate = tariff_night
		raw_local = round_rate_amount(tariff_nightly * fx_rate, 0, "Nearest", currency)
		use_foreign_pct = round_rate_amount(entry, 0, "Nearest", currency) == raw_local
	if use_foreign_pct:
		tariff_currency, tariff_nightly, fx_rate = tariff_night
		net_foreign = round_rate_amount(
			tariff_nightly * (Decimal(1) - _dec(value) / Decimal(100)),
			0, "Nearest", tariff_currency,
		)
		net_entry = max(round_rate_amount(net_foreign * fx_rate, 0, "Nearest", currency), Decimal(0))
	else:
		if kind == "Percent":
			off = entry * _dec(value) / Decimal(100)
		else:
			if _dec(value) > entry:
				frappe.throw(_("A discount of {0} is more than the room rate on {1} ({2}).").format(
					f"{_dec(value):,.2f}", date, f"{entry:,.2f}"))
			off = _dec(value)
		net_entry = max(entry - off, Decimal(0)).quantize(entry_unit, rounding=ROUND_HALF_UP)
	gst = room_gst_rate(property, rt, net_entry + _dec(night_over_cap))
	net = (net_entry / (Decimal(1) + gst / Decimal(100))).quantize(
		unit, rounding=ROUND_HALF_UP) if inclusive else net_entry
	return net, gst, max(entry - entry_rate(net, gst, inclusive, entry_places), Decimal(0))


def _discount_nights(property, rt, nightly: list, room_total: Decimal, disc: dict,
                     inclusive: bool, night_over_cap: Decimal, agreed_rate,
                     contract_tariff: dict | None = None,
                     rate_plan: str | None = None) -> tuple:
	"""Take the reservation discount off each night's accommodation.

	Each night's base is its share of the planned room total (season,
	demand, rate plan or agreed / contract rate already applied). The
	discount comes off it on the basis the desk sees it - gross where the
	property's rates include tax (discount_night). Nothing else on the stay
	moves. The nights are left at the property's night-rate decimals
	(night_rate_places), so the locked rows add up to the quote. Returns
	(room_total, room_tax, extra-guest net change, extra-guest tax change,
	base total and discount total on the desk's basis)."""
	places = night_rate_places(property)
	unit = Decimal(1).scaleb(-places)
	plan_free = sum((_dec(n["rate"]) for n in nightly), Decimal(0))
	factor = room_total / plan_free if plan_free else Decimal(1)
	new_total = new_tax = base_total = off_total = oc_net = oc_tax = Decimal(0)
	for n in nightly:
		gst = _dec(n["gst_rate"])
		base = (
			_dec(n["planned_rate"]).quantize(unit, rounding=ROUND_HALF_UP)
			if "planned_rate" in n
			else (_dec(n["rate"]) * factor).quantize(unit, rounding=ROUND_HALF_UP)
		)
		own = disc["nights"].get(n["date"])
		kind, value = (own["type"], own["value"]) if own else (disc["stay"] or (None, Decimal(0)))
		entry = entry_rate(base, gst, inclusive, places)
		net, new_gst, off = discount_night(
			property, rt, base, gst, entry, kind, value,
			inclusive, night_over_cap, n["date"],
			tariff_night=contract_night_tariff(contract_tariff, n["date"], rate_plan=rate_plan),
		)
		if night_over_cap and new_gst != gst:
			# the extra guests ride the night's tax rate
			def _oc(rate):
				return night_over_cap / (Decimal(1) + rate / Decimal(100)) if inclusive else night_over_cap
			oc_net += _oc(new_gst) - _oc(gst)
			oc_tax += _oc(new_gst) * new_gst / Decimal(100) - _oc(gst) * gst / Decimal(100)
		n.update({
			"base_rate": float(base), "rate": float(net), "gst_rate": float(new_gst),
			"discount_type": kind, "discount_value": float(value),
			"discount_amount": float(off),
			"discount_source": "night" if own else ("stay" if kind else None),
			"fixed_rate": 1 if (own and own["fixed_rate"]) or agreed_rate else 0,
		})
		if own:
			# the night's own Daily Details entry, kept on its locked row
			n["night_discount"] = {"discount_type": own["type"],
			                       "discount_value": float(own["value"]),
			                       "discount_reason": own["reason"],
			                       "fixed_rate": own["fixed_rate"]}
			if own["reason"]:
				n["discount_reason"] = own["reason"]
		base_total += entry
		off_total += off
		new_total += net
		new_tax += net * new_gst / Decimal(100)
	return new_total, new_tax, oc_net, oc_tax, base_total, off_total


def stay_room_tax_rate(room_tax: Decimal, room_total: Decimal, nightly: list) -> Decimal:
	"""The room tax rate of a stay, for the lines taxed like the room
	(cleaning, pets, a taxable levy): the rate the room nights carry on
	average. A stay whose room is at no charge (a full comp) still carries
	its nights' own tax rate - the rate the folio posts the levy at
	(folio._nightly_gst reads the locked night) - so a fee is never untaxed
	because the room was given away."""
	if room_total:
		return room_tax / room_total * Decimal(100)
	rates = [_dec(n.get("gst_rate")) for n in nightly or []]
	return sum(rates, Decimal(0)) / len(rates) if rates else Decimal(0)


def _pin_nights(nightly: list, locked: dict, inclusive: bool, night_over_cap: Decimal,
                places: int = 2) -> tuple:
	"""Put each booked night's locked room price back on the quote's night
	(quote locked_nights) and total the room from them: (room total, room
	tax, base and discount on the desk's basis, extra guests and their tax,
	each at the night's own tax rate)."""
	room_total = room_tax = base_total = off_total = oc = oc_tax = Decimal(0)
	for n in nightly:
		row = locked.get(str(n["date"]))
		if row:
			n.update(rate=flt(row.get("rate")), gst_rate=flt(row.get("gst_rate")),
			         base_rate=flt(row.get("base_rate")) or flt(row.get("rate")),
			         discount_amount=flt(row.get("discount_amount")),
			         demand_premium_pct=flt(row.get("demand_premium_pct")),
			         rate_code_amount=flt(row.get("rate_code_amount")))
		rate, gst, off = _dec(n["rate"]), _dec(n["gst_rate"]), _dec(n.get("discount_amount"))
		room_total += rate
		room_tax += rate * gst / Decimal(100)
		base_total += entry_rate(rate, gst, inclusive, places) + off
		off_total += off
		if night_over_cap:
			net = night_over_cap / (Decimal(1) + gst / Decimal(100)) if inclusive else night_over_cap
			oc += net
			oc_tax += net * gst / Decimal(100)
	return room_total, room_tax, base_total, off_total, oc, oc_tax


def quote(
	property: str,
	room_type: str,
	check_in_date: str,
	check_out_date: str,
	adults: int = 2,
	children: int = 0,
	meal_plan: str | None = None,
	rate_plan: str | None = None,
	voucher_code: str | None = None,
	check_voucher_limits: bool = True,
	over_capacity_charge: float = 0,
	exclude_reservation: str | None = None,
	pets: int = 0,
	company: str | None = None,
	agreed_rate: float = 0,
	check_in_time: str | None = None,
	check_out_time: str | None = None,
	night_prices=None,
	discount_type: str | None = None,
	discount_value: float = 0,
	night_discounts=None,
	locked_nights: dict | None = None,
	agreed_nights: dict | None = None,
	contract_tariff: dict | None = None,
) -> dict:
	"""locked_nights ({date: a locked night row}): a booked stay's room
	nights that keep their price (Reservation.pinned_room_nights); the
	meals, the voucher, the fees, the levy and the tax are priced around
	them as usual."""
	# a stay for a company is priced on the plan the company negotiated,
	# unless the caller named a plan of its own
	company_plan = False
	if not rate_plan and company:
		rate_plan = negotiated_rate_plan(property, company)
		company_plan = bool(rate_plan)
	nights = date_diff(check_out_date, check_in_date)
	day_use = nights == 0
	if nights < 0:
		frappe.throw("Check-out cannot be before check-in.")

	rt = frappe.get_doc("Room Type", room_type)
	inclusive = rates_include_tax(property)
	small_ok = small_group_allows_stay(rt, check_in_date, check_out_date)
	# holiday dates can charge a small party the house rate, as a weekend does
	stay_rules = stay_season_rules(property, check_in_date, check_out_date,
	                               room_type)
	if stay_rules["no_small_group"]:
		small_ok = False

	nightly = []
	room_total = Decimal(0)
	room_tax = Decimal(0)
	# what extra adults and children add to the card nights (for a derived plan)
	extra_person_total = Decimal(0)
	# Guests above the room's capacity, at the price agreed on this booking:
	# gathered per night so each night is taxed at its own rate.
	night_over_cap = _dec(over_capacity_charge)
	over_cap = Decimal(0)
	over_cap_tax = Decimal(0)
	occ_cache: dict = {}
	# long-stay discount: off each night's rate before tax, so the nights
	# locked on the booking (and billed by the folio) already carry it. A
	# desk-agreed rate is a price a person set and is left alone.
	los = None if agreed_rate else long_stay_rule(property, room_type, nights)
	los_pct = _dec(los.discount_percent) if los else Decimal(0)
	rails = frappe.get_all(
		"Rate Guardrail",
		filters={"property": property, "disabled": 0,
		         "room_type": ("in", [room_type, "", None])},
		fields=["name", "room_type", "floor_price", "days_of_week"]) if los else []
	los_saved = Decimal(0)
	# the nights' floors (net of tax) on a long-stay discounted stay: the
	# rate plan and a voucher stack on top and must not undercut them either
	los_floor = Decimal(0)
	day_use_meta = None

	pkg = None if agreed_rate else active_holiday_package(property, check_in_date, check_out_date, room_type)

	# Nights the desk priced by hand. Anything that would also move the room
	# price is refused rather than quietly dropped or stacked on the typed
	# figure: the guest was told that exact price.
	typed = normalize_night_prices(night_prices, check_in_date, check_out_date)
	# The desk's reservation discount, validated before anything is priced
	# and taken off the room nights once the rate plan has priced them.
	disc = normalize_discounts(discount_type, discount_value, night_discounts,
	                           check_in_date, check_out_date, property=property)
	if typed:
		refused = (
			(voucher_code, _("a voucher")),
			(rate_plan and not company_plan, _("a rate plan")),
			(company_plan, _("the company's negotiated rate plan")),
			(agreed_rate, _("an agreed rate")),
			(los, _("the long-stay discount")),
			(pkg, _("a holiday package")),
			(disc, _("a discount")),
		)
		for hit, what in refused:
			if hit:
				refuse_with_night_prices(what)
	if disc and pkg:
		frappe.throw(_("A holiday package is sold at its own price and can't take a desk discount."))
	if pkg:
		pkg_doc = frappe.get_doc("Holiday Package", pkg.name)
		pkg_nights = int(getattr(pkg, "package_nights", None) or getattr(pkg, "minimum_nights", None) or 1)
		billable_nights = max(nights, 1)

		# The package base covers up to pkg_nights
		pkg_base_rate = pkg_doc.package_price_for(adults, children, nights=pkg_nights)
		stay_dates = [add_days(check_in_date, i) for i in range(billable_nights)]

		if billable_nights <= pkg_nights:
			base_per_night = (pkg_base_rate / Decimal(billable_nights)).quantize(Decimal("0.01"))
			accum = Decimal(0)
			for i, d in enumerate(stay_dates):
				rate = (pkg_base_rate - accum) if i == billable_nights - 1 else base_per_night
				accum += rate

				gst = room_gst_rate(property, rt, rate + night_over_cap)
				if inclusive:
					rate = rate / (Decimal(1) + gst / Decimal(100))
				if night_over_cap:
					net = (night_over_cap / (Decimal(1) + gst / Decimal(100))
					       if inclusive else night_over_cap)
					over_cap += net
					over_cap_tax += net * gst / Decimal(100)

				nightly.append({
					"date": str(d),
					"rate": float(rate),
					"gst_rate": float(gst),
					"demand_premium_pct": 0,
					"package": pkg.package_name,
					"occupancy": 0
				})
				room_total += rate
				room_tax += rate * gst / Decimal(100)
		else:
			# Identify which dates are package dates (prefer inside [start_date, end_date])
			inside_pkg = [d for d in stay_dates if getdate(pkg.start_date) <= getdate(d) <= getdate(pkg.end_date)]
			outside_pkg = [d for d in stay_dates if not (getdate(pkg.start_date) <= getdate(d) <= getdate(pkg.end_date))]

			# Sort outside_pkg by proximity to package window
			def _dist(d):
				dt = getdate(d)
				if dt < getdate(pkg.start_date):
					return (getdate(pkg.start_date) - dt).days
				return (dt - getdate(pkg.end_date)).days
			outside_pkg.sort(key=_dist)

			if len(inside_pkg) >= pkg_nights:
				pkg_dates_set = set(inside_pkg[:pkg_nights])
			else:
				needed = pkg_nights - len(inside_pkg)
				pkg_dates_set = set(inside_pkg + outside_pkg[:needed])

			package_daily = (pkg_base_rate / Decimal(pkg_nights)).quantize(Decimal("0.01"))
			fixed_extra = _dec(getattr(pkg, "extra_night_price", 0))
			pct_raw = getattr(pkg, "extra_night_percent", None)
			pct_extra = float(pct_raw) if (pct_raw is not None and pct_raw != "") else 100.0

			if fixed_extra > 0:
				extra_night_rate = fixed_extra
			else:
				extra_night_rate = (package_daily * Decimal(str(pct_extra)) / Decimal(100)).quantize(Decimal("0.01"))

			base_per_night = package_daily
			accum = Decimal(0)
			pkg_count = len(pkg_dates_set)
			pkg_i = 0

			for d in stay_dates:
				if d in pkg_dates_set:
					pkg_i += 1
					rate = (pkg_base_rate - accum) if pkg_i == pkg_count else base_per_night
					accum += rate
					label = pkg.package_name
				else:
					rate = extra_night_rate
					label = f"{pkg.package_name} (Diária Adicional)"

				gst = room_gst_rate(property, rt, rate + night_over_cap)
				if inclusive:
					rate = rate / (Decimal(1) + gst / Decimal(100))
				if night_over_cap:
					net = (night_over_cap / (Decimal(1) + gst / Decimal(100))
					       if inclusive else night_over_cap)
					over_cap += net
					over_cap_tax += net * gst / Decimal(100)

				nightly.append({
					"date": str(d),
					"rate": float(rate),
					"gst_rate": float(gst),
					"demand_premium_pct": 0,
					"package": label,
					"occupancy": 0
				})
				room_total += rate
				room_tax += rate * gst / Decimal(100)
	else:
		# a day-use stay bills one "night" on the check-in date
		for i in range(max(nights, 1)):
			date = add_days(check_in_date, i)
			rate = season_adjust(
				property, date,
				occupancy_rate(rt, adults, children, date,
				               allow_small_group=small_ok),
				room_type
			)
			# demand pricing: when the house fills past a hurdle tier, the
			# premium applies and the tier's floor holds - decided here, in
			# code, never by the caller
			if str(date) not in occ_cache:
				occ_cache[str(date)] = forecast_occupancy(
					property, date, exclude=exclude_reservation)
			rate, tier = demand_adjust(property, room_type, date, rate,
			                           occupancy=occ_cache[str(date)])
			if agreed_rate:
				# a rate the desk agreed replaces the card and the premium; a
				# contract over a tariff change agrees each night's own rate
				rate, tier = _dec((agreed_nights or {}).get(str(date), agreed_rate)), None
			hand = typed.get(str(date))
			if hand is not None:
				# a night the desk priced: replaces the card, the season and
				# any demand premium. In a tax-inclusive property the typed
				# figure is gross, and the slab / back-out below read it the
				# way they read the card.
				rate, tier = hand, None
			night_extra = (Decimal(0) if agreed_rate or hand is not None else
			               extra_person_charge(rt, adults, children, date, allow_small_group=small_ok))
			if los_pct:
				off = rate * (Decimal(1) - los_pct / Decimal(100))
				# never below the owner's floor or the hurdle tier's minimum for
				# this night (a rate already under them is not raised by them)
				rail = rail_for_day(rails, room_type,
				                    getdate(date).strftime("%a").lower())
				floor = max(_dec(rail.floor_price) if rail else Decimal(0),
				            _dec(tier["min_rate"]) if tier else Decimal(0))
				floor = min(rate, floor)
				off = max(off, floor)
				gst_off = room_gst_rate(property, rt, off + night_over_cap)
				saved = rate - off
				los_saved += (saved / (Decimal(1) + gst_off / Decimal(100))
				              if inclusive else saved)
				los_floor += (floor / (Decimal(1) + gst_off / Decimal(100))
				              if inclusive else floor)
				rate = off
			# The agreed amount for extra beds counts toward the slab even though
			# it bills as its own line: GST reads what the unit fetches for the
			# day, not the occupancy its rate card started from. A room at 7,000
			# with 2,000 of extra beds is a 9,000 night, so 18%, not 5%.
			gst = room_gst_rate(property, rt, rate + night_over_cap)
			if inclusive:
				# configured price is gross — back out the taxable value
				rate = rate / (Decimal(1) + gst / Decimal(100))
				night_extra = night_extra / (Decimal(1) + gst / Decimal(100))
			extra_person_total += min(night_extra, rate)
			if night_over_cap:
				net = (night_over_cap / (Decimal(1) + gst / Decimal(100))
				       if inclusive else night_over_cap)
				over_cap += net
				over_cap_tax += net * gst / Decimal(100)
			nightly.append({"date": str(date), "rate": float(rate),
			                "gst_rate": float(gst),
			                "demand_premium_pct": tier["premium_pct"] if tier else 0,
			                "occupancy": occ_cache[str(date)],
			                **({"hand_set": True, "typed_price": float(hand)}
			                   if hand is not None else {})})
			room_total += rate
			room_tax += rate * gst / Decimal(100)

	# Hourly / block day-use: replace the one nightly rate with hours/blocks
	# from the planned slot. Overnight stays and Nightly day-use plans are
	# untouched. An agreed desk rate still wins.
	if day_use and not agreed_rate:
		from kamra.day_use import quote_day_use_amount
		# Gross nightly (before tax back-out) as the Full-day overstay stand-in.
		nightly_gross = room_total * (Decimal(1) + (
			_dec(nightly[0]["gst_rate"]) / Decimal(100) if nightly else Decimal(0)
		)) if inclusive else room_total
		day_use_meta = quote_day_use_amount(
			property, rate_plan, check_in_time, check_out_time, nightly_gross)
		if day_use_meta:
			gross = _dec(day_use_meta["amount"])
			gst = room_gst_rate(property, rt, gross + night_over_cap)
			rate = (gross / (Decimal(1) + gst / Decimal(100))
			        if inclusive else gross)
			nightly[0] = {
				"date": str(check_in_date), "rate": float(rate),
				"gst_rate": float(gst),
				"demand_premium_pct": 0,
				"occupancy": occ_cache.get(str(check_in_date), 0),
				"billing_basis": day_use_meta["billing_basis"],
				"units": day_use_meta["units"],
				"unit_rate": float(
					rate / _dec(day_use_meta["units"])
					if day_use_meta["units"] else rate),
				"hours": day_use_meta["hours"],
			}
			room_total = rate
			room_tax = rate * gst / Decimal(100)
			# Hourly/block plans own the room total; nightly modifiers do not
			# stack on top (Absolute would wipe the hour math).
			rate_plan_applied = True
		else:
			rate_plan_applied = False
	else:
		rate_plan_applied = False

	prop_currency = property_currency(property)
	planned_by_plan = False
	if rate_plan and not agreed_rate and not rate_plan_applied:
		planned_by_plan = True
		old_room_total = room_total
		planned, rounded = plan_stay_nights(rate_plan, [(_dec(n["rate"]), _dec(n["gst_rate"])) for n in nightly],
		                                    extra_person_total, inclusive, los_floor,
		                                    currency=prop_currency)
		room_total = sum(planned, Decimal(0))
		room_tax = sum((rate * _dec(n["gst_rate"]) / Decimal(100) for rate, n in zip(planned, nightly, strict=True)),
		               Decimal(0))
		if old_room_total:
			los_saved *= room_total / old_room_total
		if rounded:
			# each night's own rounded price: the booking locks exactly these
			for night, rate in zip(nightly, planned, strict=True):
				night["planned_rate"] = float(rate)

	# SAP TCURX / OPERA: 0%-tax stays (including bridged properties where the
	# native invoice owns tax) must not carry fractions of the currency's
	# smallest unit. Round each 0%-tax night at the line so charges_total
	# stays equal to the sum of lines while every line is payable.
	if planned_by_plan:
		adjusted = [
			round_rate_amount(rate, 0, "Nearest", prop_currency) if not _dec(n["gst_rate"]) else rate
			for rate, n in zip(planned, nightly, strict=True)
		]
		if adjusted != planned:
			for night, rate in zip(nightly, adjusted, strict=True):
				night["planned_rate"] = float(rate)
			room_total = sum(adjusted, Decimal(0))
			room_tax = sum((rate * _dec(n["gst_rate"]) / Decimal(100) for rate, n in zip(adjusted, nightly, strict=True)),
			               Decimal(0))
	else:
		zero_tax_adjusted = False
		for n in nightly:
			if _dec(n["gst_rate"]):
				continue
			raw = _dec(n["rate"])
			rnd = round_rate_amount(raw, 0, "Nearest", prop_currency)
			if rnd != raw:
				n["rate"] = float(rnd)
				zero_tax_adjusted = True
		if zero_tax_adjusted:
			room_total = sum((_dec(n["rate"]) for n in nightly), Decimal(0))

	# Gross, discount and net stay apart (the base rate is never overwritten):
	# the discount comes off the accommodation only, after every rule that
	# sets the room's price, so meals, extras and levies keep their own. The
	# base and the discount are on the basis the desk entered the discount
	# (gross where rates include tax), so a cut that drops a night into a
	# lower tax slab still reads as the cut it is.
	room_base_total = room_total + (room_tax if inclusive else Decimal(0))
	reservation_discount = Decimal(0)
	if disc:
		room_total, room_tax, oc_net, oc_tax, room_base_total, reservation_discount = _discount_nights(
			property, rt, nightly, room_total, disc, inclusive, night_over_cap, agreed_rate,
			contract_tariff=contract_tariff, rate_plan=rate_plan)
		over_cap += oc_net
		over_cap_tax += oc_tax
	for n in nightly:
		n.setdefault("base_rate", n["rate"])
		n.setdefault("discount_type", None)
		n.setdefault("discount_value", 0)
		n.setdefault("discount_amount", 0)
		n.setdefault("discount_source", None)
		n.setdefault("fixed_rate", 1 if agreed_rate else 0)
	if locked_nights and not pkg and not day_use:
		room_total, room_tax, room_base_total, reservation_discount, over_cap, over_cap_tax = \
			_pin_nights(nightly, locked_nights, inclusive, night_over_cap,
			            places=night_rate_places(property))

	from kamra.folio import _fnb_gst
	fnb_rate = _fnb_gst(property)  # the country pack's F&B rate

	billable_nights = max(nights, 1)
	meal_total = Decimal(0)
	if meal_plan:
		mp = frappe.get_doc("Meal Plan", meal_plan)
		meal_total = billable_nights * (
			adults * _dec(mp.price_per_adult)
			+ children * _dec(mp.price_per_child)
		)
		if inclusive:
			meal_total /= Decimal(1) + _dec(fnb_rate) / Decimal(100)
	meal_tax = meal_total * _dec(fnb_rate) / Decimal(100)

	subtotal = room_total + meal_total

	discount = Decimal(0)
	voucher_name = None
	if voucher_code:
		v = validate_voucher(property, voucher_code, max(nights, 1),
		                     check_limits=check_voucher_limits)
		voucher_name = v.name
		if v.discount_type == "Percent":
			discount = subtotal * _dec(v.value) / Decimal(100)
		else:
			discount = min(_dec(v.value), subtotal)
		# ponytail: the floor guard covers long-stay stays only; a voucher
		# or rate plan alone keeps its old behaviour
		discount = min(discount, max(Decimal(0), subtotal - los_floor))

	taxable = subtotal - discount
	# discount reduces tax proportionally across the blended rate
	gross_tax = room_tax + meal_tax
	tax = gross_tax * (taxable / subtotal) if subtotal else Decimal(0)

	# Cleaning fee (ADR-008): separate line; taxable at room GST when enabled.
	prop = frappe.get_cached_doc("Property", property)
	cleaning = _dec(prop.get("cleaning_fee") or 0)
	cleaning_tax = Decimal(0)
	# the lines taxed like the room (cleaning, pets, a taxable levy) take the
	# room's own tax rate, a full comp included
	avg_gst = stay_room_tax_rate(room_tax, room_total, nightly)
	if cleaning > 0:
		if cint(prop.get("cleaning_fee_taxable") if prop.get("cleaning_fee_taxable") is not None else 1):
			if inclusive:
				cleaning = cleaning / (Decimal(1) + avg_gst / Decimal(100))
			cleaning_tax = cleaning * avg_gst / Decimal(100)
		tax += cleaning_tax
		total_base = taxable + cleaning
	else:
		total_base = taxable

	# The extra beds bill as their own line rather than buried in the room
	# rate: the guest is told what the house costs and what the extra beds
	# cost, and the desk agreed the second number separately. A rate plan or
	# a discount does not touch it - it is a figure a person agreed.
	if over_cap > 0:
		tax += over_cap_tax
		total_base += over_cap

	# Pets: their own line, taxed at the stay's room GST like the cleaning
	# fee. A voucher discounts the stay, not the pet charge.
	pets = max(0, cint(pets))
	pet_fee = pet_charge_amount(prop, pets, billable_nights)
	pet_tax = Decimal(0)
	if pet_fee > 0:
		if inclusive:
			pet_fee = pet_fee / (Decimal(1) + avg_gst / Decimal(100))
		pet_tax = pet_fee * avg_gst / Decimal(100)
		tax += pet_tax
		total_base += pet_fee

	# Room levy (municipality fee, city / tourism tax): a % of the room rate
	# net of discount and/or a fixed amount per occupied room-night (a local
	# hotel tax), posted per night as its own folio line
	levy_pct = _dec(prop.get("room_levy_percent") or 0)
	levy_fixed = _dec(prop.get("room_levy_per_night") or 0)
	levy = levy_tax = Decimal(0)
	if levy_pct > 0 and room_total:
		room_net = room_total * (taxable / subtotal) if subtotal else room_total
		levy = room_net * levy_pct / Decimal(100)
	if levy_fixed > 0:
		levy += levy_fixed * billable_nights
	if levy:
		if cint(prop.get("room_levy_taxable")
		        if prop.get("room_levy_taxable") is not None else 1):
			levy_tax = levy * avg_gst / Decimal(100)
		tax += levy_tax
		total_base += levy

	effective_pct = float(tax / total_base * 100) if total_base else 0
	# Document-level currency minor-unit rounding (SAP TCURX style: e.g. whole
	# UGX on tax-exclusive stays, never per-line rounding that loses a shilling).
	currency = prop.get("currency") or property_currency(property)
	if tax:
		total = round_rate_amount(total_base + tax, 0, "Nearest", currency)
		tax = total - total_base
	else:
		total = total_base

	deposit_required = float(prop.get("security_deposit_amount") or 0)

	room_label = "Room nights"
	if day_use_meta:
		room_label = day_use_meta.get("description") or "Day use"
	line_items = [
		{
			"component": "accommodation",
			"description": room_label,
			"amount": float(room_total),
			"tax_amount": float(room_tax * (taxable / subtotal) if subtotal else 0),
			"tax_rate": float(room_tax / room_total * 100) if room_total else 0,
			"is_tax_inclusive": bool(inclusive),
			"nights_applied": billable_nights,
		},
	]
	if los_saved > 0:
		# already inside the room nights above: shown, not subtracted again
		line_items[0]["description"] = (
			f"Room nights, long stay {float(los_pct):g}% off")
		line_items[0]["long_stay_discount"] = round(float(los_saved), 2)
	if reservation_discount > 0:
		# the desk's discount is inside the room nights too: gross and
		# discount are shown beside the net, never subtracted again
		line_items[0]["base_amount"] = float(room_base_total)
		line_items[0]["reservation_discount"] = float(reservation_discount)
	if meal_total:
		line_items.append({
			"component": "meal_plan",
			"description": "Meal plan",
			"amount": float(meal_total),
			"tax_amount": float(meal_tax * (taxable / subtotal) if subtotal else 0),
			"nights_applied": billable_nights,
		})
	if over_cap > 0:
		line_items.append({
			"component": "over_capacity",
			"description": "Extra guests by arrangement",
			"amount": float(over_cap),
			"tax_amount": float(over_cap_tax),
			"tax_rate": float(over_cap_tax / over_cap * 100) if over_cap else 0,
			"is_tax_inclusive": bool(inclusive),
			"nights_applied": billable_nights,
		})
	if cleaning > 0:
		line_items.append({
			"component": "cleaning_fee",
			"description": "Cleaning fee",
			"amount": float(cleaning),
			"tax_amount": float(cleaning_tax),
			"nights_applied": 0,
		})
	if pet_fee > 0:
		line_items.append({
			"component": "pet_charge",
			"description": f"Pets × {pets}",
			"amount": float(pet_fee),
			"tax_amount": float(pet_tax),
			"nights_applied": billable_nights
				if prop.get("pet_charge_basis") == "Per night" else 0,
		})
	if levy:
		levy_label = prop.get("room_levy_label") or getattr(pack_for(property), "ROOM_LEVY_LABEL", None) or "Room levy"
		line_items.append({
			"component": "room_levy",
			"description": levy_label,
			"amount": float(levy),
			"tax_amount": float(levy_tax),
			"nights_applied": billable_nights,
		})
	if discount:
		line_items.append({
			"component": "promotion",
			"description": "Discount",
			"amount": -float(discount),
			"tax_amount": 0.0,
			"nights_applied": billable_nights,
		})

	adv_pct, adv_season = advance_percent(property, stay_rules)

	from frappe.utils import now_datetime
	out = {
		"nights": nights,
		"day_use": day_use,
		"nightly": nightly,
		"room_total": float(room_total),
		"meal_total": float(meal_total),
		"cleaning_fee": float(cleaning),
		"over_capacity_total": float(over_cap),
		"pets": pets,
		"pet_charge": float(pet_fee),
		"room_levy": float(levy),
		# every discount on the stay: the voucher plus the desk's discount
		# (which is already inside room_total, the net accommodation)
		"discount": float(discount + reservation_discount),
		# the voucher's part on its own: what the folio posts as its line
		"voucher_discount": float(discount),
		# the accommodation before the desk's discount and the discount
		# itself, both on the basis the desk entered it (gross where rates
		# include tax): base less discount is what the guest pays for the room
		"room_base_total": float(room_base_total),
		"reservation_discount_total": float(reservation_discount),
		"voucher": voucher_name,
		# the long-stay discount already taken off the room nights
		"long_stay": {"rule": los.name, "min_nights": int(los.min_nights),
		              "percent": float(los_pct),
		              "amount": round(float(los_saved), 2)}
		if los_saved > 0 else None,
		# what the booking should secure in advance: the % its dates ask
		# for (a season's advance %, else the property's usual deposit %)
		"advance": {"percent": adv_pct, "season": adv_season,
		            "amount": round(float(total) * adv_pct / 100, 2)},
		"package": {
			"name": pkg.name,
			"package_name": pkg.package_name,
			"package_nights": pkg_nights,
			"extra_night_percent": pct_extra if "pct_extra" in locals() else float(getattr(pkg, "extra_night_percent", 0) or 100),
			"extra_night_price": float(extra_night_rate if "extra_night_rate" in locals() else (getattr(pkg, "extra_night_price", 0) or 0)),
			"inclusions": pkg.inclusions or "",
			"flyer_image": pkg.flyer_image or None,
			"ota_strategy": pkg.ota_strategy,
			"ota_daily_rate": float(pkg.ota_daily_rate or 0)
		} if pkg else None,
		"rate_plan": rate_plan or None,
		"amount_before_tax": float(total_base),
		"tax_percent": round(effective_pct, 2),
		"tax_amount": float(tax),
		"amount_after_tax": float(total),
		"line_items": line_items,
		"totals": {
			# before every discount, so subtotal - discount + tax is the total
			"subtotal": float(subtotal + reservation_discount + cleaning + pet_fee + levy),
			"discount": float(discount + reservation_discount),
			"tax": float(tax),
			"total": float(total),
			"deposit_required": deposit_required,
		},
		"currency": prop.get("currency") or default_currency(pack_for(property)),
		"quote_version": 1,
		# the date rules this stay runs into, so a booking page or an agent
		# can say why a stay needs two nights or full payment
		"stay_rules": {k: stay_rules[k] for k in (
			"minimum_nights", "season", "non_refundable", "full_payment",
			"is_holiday", "holiday", "advance_percent", "advance_season")},
		"is_holiday": stay_rules["is_holiday"],
		"quoted_at": str(now_datetime()),
	}
	if day_use_meta:
		out["day_use_billing"] = {
			"billing_basis": day_use_meta["billing_basis"],
			"units": day_use_meta["units"],
			"hours": day_use_meta["hours"],
			"unit_rate": day_use_meta["unit_rate"],
			"full_day": bool(day_use_meta.get("full_day")),
			"description": day_use_meta.get("description"),
		}
	return out
