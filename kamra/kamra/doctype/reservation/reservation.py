# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

import frappe

from kamra.branding import product_name
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, cint, date_diff, flt, formatdate, getdate, now_datetime, today


class InventoryRefused(frappe.ValidationError):
	"""No room left to sell: category sold out, room or villa already taken."""


# The SQL test for "a room type that is not a whole villa" (rt = Room Type).
# One definition shared by the villa <-> room lockout and the member-room
# fallback, so the two can never drift apart.
NON_VILLA_SQL = "(rt.room_category != 'Villa' OR rt.room_category IS NULL)"


# A locked night's price: what the folio bills for it, the price the desk
# typed for it (hand_price, which the next quote prices the night at) and its
# rate-code amount (what an agreed rate's or a discount's cut is measured
# from). Written only by the booking's own pricing (Reservation.validate),
# the channel manager and create_booking's typed nights.
LOCKED_NIGHT_COLUMNS = ("rate", "base_rate", "discount_amount", "gst_rate", "meal_amount",
                        "hand_price", "rate_code_amount")
# what a night is sold at: a change in any of them re-prices every night
# still to come, and a discounted stay takes the discount rules again
SOLD_LINKS = ("rate_plan", "room_type")
SOLD_FIGURES = ("agreed_rate",)
# the party: it re-prices the room too, unless the stay holds a fixed rate
# (OPERA keeps a Fixed Rate when the party changes)
PARTY_FIGURES = ("adults", "children", "extra_beds")
# the quote inputs a re-quote may change while the booked room nights stay as
# they are (Reservation.pinned_room_nights): a meal plan always, the party on
# a fixed-rate stay
ROOM_NEUTRAL_INPUTS = ("meal_plan",)
PARTY_INPUTS = ("adults", "children")
# the columns that say a locked night's price moved
NIGHT_PRICE_COLUMNS = ("rate", "base_rate", "discount_amount")
# what a night on the folio keeps from when it was charged, besides the rate
# and tax rate the folio holds (Reservation.hold_charged_nights)
CHARGED_NIGHT_HISTORY = ("base_rate", "discount_amount", "rate_code_amount")
# the quote inputs that are the desk's discount, not what is being sold
DISCOUNT_INPUT_KEYS = ("discount_type", "discount_value", "night_discounts")
# every column a locked night row carries
NIGHT_ROW_FIELDS = ("stay_date", "rate", "gst_rate", "meal_amount", "demand_premium_pct",
                    "hand_price", "rate_code_amount", "base_rate", "discount_type",
                    "discount_value", "discount_amount", "discount_reason", "fixed_rate")


def discount_inputs(doc) -> tuple[tuple, set]:
	"""A booking's discount as the desk set it: the stay's (type, value,
	reason, fixed rate) and each night's own Daily Details entry."""
	stay = (doc.get("discount_type") or None, flt(doc.get("discount_value")),
	        doc.get("discount_reason") or None, cint(doc.get("fixed_rate")))
	nights = {(str(getdate(r.stay_date)), r.get("discount_type") or None,
	           flt(r.get("discount_value")), r.get("discount_reason") or None,
	           cint(r.get("fixed_rate")))
	          for r in doc.get("night_rates") or []
	          if r.get("discount_type") or cint(r.get("fixed_rate"))}
	return stay, nights


def night_entry_base(row, inclusive: bool, places: int = 2) -> float:
	"""A locked night's base before the desk's discount, on the basis the
	desk enters a discount (gross where the property's rates include tax):
	the night's net plus its discount."""
	from kamra.pricing import entry_rate
	return float(entry_rate(row.get("rate"), row.get("gst_rate"), inclusive, places)) \
		+ flt(row.get("discount_amount"))


def lock_property_for_inventory(property: str) -> None:
	"""One inventory decision at a time for this property.

	Category capacity and the villa/room lockout both re-read after this
	returns. A plain SELECT would keep the snapshot from before the wait
	and a second booking would sell a room the first one just took.
	"""
	if property:
		frappe.db.get_value("Property", property, "name", for_update=True)


def ota_live_key(property: str, ota_ref: str) -> str:
	"""Unique key for one live, non-group stay with this OTA reference.

	Group rooming lists share a reference on purpose, so they do not use
	this key. Longer than the field allows, the key is a hash of the same
	pair so the unique index still rejects a duplicate delivery.
	"""
	raw = f"{property}::{(ota_ref or '').strip()}"
	if len(raw) <= 140:
		return raw
	import hashlib
	return hashlib.sha256(raw.encode()).hexdigest()


def is_entire_place(room_type: str | None) -> bool:
	"""An entire-place (whole villa) stay: its room type is Villa-category,
	sold as one unit - the same test validate_villa_lockout uses."""
	return bool(room_type) and frappe.get_cached_value(
		"Room Type", room_type, "room_category") == "Villa"


def uses_member_rooms(property: str, room_type: str | None) -> bool:
	"""True when a whole-villa stay has no Room of its own to assign and
	instead takes the property's member rooms (the hybrid estate). A villa
	type with its own Room records checks in to one of them like any room."""
	return is_entire_place(room_type) and not frappe.db.exists(
		"Room", {"property": property, "room_type": room_type})


def entire_place_rooms(property: str, room_type: str,
                       include_out_of_order: bool = False) -> list[dict]:
	"""The member rooms (name, room_number) a whole-villa stay occupies when
	its villa type has no rooms of its own: every non-villa room on the
	property, mirroring the villa <-> room lockout. Out of Order rooms are
	left out - they are not sold and must not be touched (check-out asks
	for them only to free one that went out of order mid-stay).

	Only safe when this is the property's ONLY Villa type: with two villa
	listings there is no telling which rooms belong to which, so it refuses
	and asks for the rooms to be set up under the villa type."""
	other_villas = frappe.db.count("Room Type", {
		"property": property, "room_category": "Villa",
		"name": ("!=", room_type)})
	if other_villas:
		frappe.throw(
			_("{0} has no rooms of its own and the property has other villa "
			  "types, so {product} can't tell which rooms it takes. Add Room "
			  "records under {0}, or assign a room before check-in.").format(
				room_type, product=product_name()),
			title=_("Villa has no rooms"))
	ooo = "" if include_out_of_order else \
		"AND COALESCE(r.housekeeping_status, '') != 'Out of Order'"
	return frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolated text is the module constant NON_VILLA_SQL and a fixed clause; the property is bound
		f"""
		SELECT r.name, r.room_number FROM `tabRoom` r
		LEFT JOIN `tabRoom Type` rt ON rt.name = r.room_type
		WHERE r.property = %s
		  AND {NON_VILLA_SQL}
		  {ooo}
		ORDER BY r.room_number
		""", property, as_dict=True)


# Statuses that hold a room (kamra.reservation_state.LIVE_STATUSES), as SQL.
_LIVE_SQL = "('Confirmed', 'Checked In', 'Held', 'Pending Payment')"


def villa_lockout_bookings(property: str, room_type: str | None, check_in,
                           check_out, exclude: str | None = None,
                           limit: int | None = None,
                           for_update: bool = False) -> list[dict]:
	"""Live reservations on the OTHER side of the villa <-> room lockout that
	overlap [check_in, check_out): for a whole-villa type, any booked
	non-villa (member) room; for any other type, a booked whole villa.

	The one query behind both the write-time guard (validate_villa_lockout)
	and every availability read (desk, booking engine, ARI push), so what
	Kamra refuses to book and what it offers for sale can never disagree.
	A property with no villa type never matches (the join finds nothing)."""
	if not room_type:
		return []
	side = NON_VILLA_SQL if is_entire_place(room_type) \
		else "rt.room_category = 'Villa'"
	return frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolated text is module constants (_LIVE_SQL, NON_VILLA_SQL) or a fixed clause; every value is bound
		f"""
		SELECT r.name, r.check_in_date, r.check_out_date FROM `tabReservation` r
		LEFT JOIN `tabRoom Type` rt ON r.room_type = rt.name
		WHERE r.property = %(property)s
		  AND r.name != %(name)s
		  AND r.status IN {_LIVE_SQL}
		  AND {side}
		  AND r.check_in_date < GREATEST(%(check_out)s, DATE_ADD(%(check_in)s, INTERVAL 1 DAY))
		  AND GREATEST(r.check_out_date, DATE_ADD(r.check_in_date, INTERVAL 1 DAY)) > %(check_in)s
		ORDER BY r.check_in_date
		{"LIMIT %d" % int(limit) if limit else ""}
		{" FOR UPDATE" if for_update else ""}
		""",
		{
			"property": property,
			"name": exclude or "new",
			"check_in": check_in,
			"check_out": check_out,
		},
		as_dict=True,
	)


def villa_lockout_conflict(property: str, room_type: str | None, check_in,
                           check_out, exclude: str | None = None,
                           for_update: bool = False) -> str | None:
	"""The reservation that stops `room_type` selling for the stay under the
	villa <-> room lockout, or None when it is free to sell."""
	rows = villa_lockout_bookings(property, room_type, check_in, check_out,
	                              exclude=exclude, limit=1, for_update=for_update)
	return rows[0].name if rows else None


def villa_locked_nights(property: str, room_type: str | None, start,
                        end) -> set[str]:
	"""The nights in [start, end) on which `room_type` cannot sell because
	the other side of the villa <-> room lockout is booked."""
	from frappe.utils import add_days, getdate
	locked: set[str] = set()
	lo, hi = getdate(start), getdate(end)
	for b in villa_lockout_bookings(property, room_type, start, end):
		ci, co = getdate(b.check_in_date), getdate(b.check_out_date)
		co = max(co, getdate(add_days(ci, 1)))  # day-use holds its date
		d = max(ci, lo)
		while d < co and d < hi:
			locked.add(str(d))
			d = getdate(add_days(d, 1))
	return locked


class Reservation(Document):
	def autoname(self):
		# RES-<year>-00001, the year being the property's financial year, so
		# the count restarts in April in India and in January elsewhere
		from frappe.model.naming import make_autoname

		from kamra.localization import financial_year, pack_for
		prefix = f"RES-{financial_year(pack_for(self.property), today())}-"
		# a year's counter is seeded once, after the highest number that year
		# already has. Plain reads: an INSERT ... SELECT would hold shared
		# locks on Reservation rows and deadlock two bookings saved at once.
		if not frappe.db.sql("SELECT 1 FROM `tabSeries` WHERE name = %s", prefix):
			top = frappe.db.sql(
				"""SELECT MAX(CAST(SUBSTRING(name, %s) AS UNSIGNED))
				   FROM `tabReservation` WHERE name LIKE %s""",
				(len(prefix) + 1, f"{prefix}%"))[0][0]
			frappe.db.sql("INSERT IGNORE INTO `tabSeries` (name, current) VALUES (%s, %s)",
			              (prefix, int(top or 0)))
		self.name = make_autoname(f"{prefix}.#####")

	def insert(self, *args, **kwargs):
		"""Insert, and report a taken OTA reference as DuplicateEntryError.

		The unique index on ota_live_key is what stops two deliveries of one
		OTA booking. Frappe turns that collision into UniqueValidationError
		inside db_insert, before the caller sees it. create_booking and the
		channel manager catch DuplicateEntryError and hand back the stay that
		won, so translate just this key."""
		try:
			return super().insert(*args, **kwargs)
		except frappe.UniqueValidationError as exc:
			if self.ota_live_key and "ota_live_key" in str(exc):
				raise frappe.DuplicateEntryError(self.ota_live_key) from exc
			raise

	def _sync_ota_live_key(self):
		"""Set the unique live-stay key, or clear it when this stay no
		longer claims the reference. An older duplicate left in place by
		the patch keeps a blank key so editing it is still possible."""
		ref = (self.ota_ref or "").strip()
		desired = None
		if ref and (self.status or "") != "Cancelled" and not self.group_booking:
			desired = ota_live_key(self.property, ref)
		if desired and self.name and not self.is_new():
			other = frappe.db.get_value("Reservation", {"ota_live_key": desired}, "name")
			if other and other != self.name:
				self.ota_live_key = None
				return
		self.ota_live_key = desired

	def after_insert(self):
		# every booking gets a pre-arrival check-in link
		self.db_set(
			"precheckin_token", frappe.generate_hash(length=24),
			update_modified=False,
		)
		if self.voucher:
			from kamra.pricing import consume_voucher_use
			consume_voucher_use(self.voucher)
		# WhatsApp booking confirmation - enqueued so a booking never
		# waits on Meta; skipped entirely unless the property connected
		# its own number (Channel Provider Connection, Meta Business)
		if self.status == "Confirmed":
			from kamra import whatsapp
			if whatsapp.has_connection(self.property):
				frappe.enqueue(
					"kamra.whatsapp.notify_booking_confirmed",
					reservation=self.name, queue="short",
					enqueue_after_commit=True)
		# ADR-009: open deposit liability when property policy requires one.
		try:
			from kamra.deposit import ensure_deposit_for_reservation
			ensure_deposit_for_reservation(self)
		except Exception:
			frappe.log_error(title="Security deposit ensure failed")

	def validate_type_capacity(self):
		"""Room-type capacity with a controlled overbooking allowance.

		The room-level overlap guard below stops physical double-booking;
		this stops UNASSIGNED bookings quietly over-selling a category. The
		allowance (room type override, else property-wide, default 0%) is a
		revenue-management decision made in Settings - never implicit.
		"""
		# waitlisted / inquiry stays hold no inventory
		from kamra.reservation_state import holds_inventory
		if not holds_inventory(self.status):
			return
		# also skip classic non-inventory statuses not in LIVE
		if self.status in ("Cancelled", "No Show", "Checked Out", "Waitlist"):
			return
		# Serialize bookings of a category, even before a room is assigned.
		# The property lock in validate() is already held; this lock orders
		# category checks. The count below is a locking read so it sees a
		# booking that committed while this one waited.
		frappe.db.get_value("Room Type", self.room_type, "name", for_update=True)
		total = frappe.db.count("Room", {"room_type": self.room_type})
		if not total:
			return  # setup-time bookings before rooms exist
		pct = float(frappe.db.get_value(
			"Room Type", self.room_type, "overbooking_pct") or 0)
		if not pct:
			# db read, not the document cache - enforcement must see the
			# committed value, not a stale cached doc
			pct = float(frappe.db.get_value(
				"Property", self.property, "overbooking_pct") or 0)
		from frappe.utils import add_days, date_diff
		nights = max(1, date_diff(self.check_out_date, self.check_in_date))
		dates = [str(add_days(self.check_in_date, i))
		         for i in range(min(int(nights), 366))]
		# rooms a confirmed group block holds back (until its cutoff) are
		# not for general sale; a pickup books against its own block. Only
		# a stay taking NEW inventory is held to this - a block made after
		# a booking never stops that booking being edited.
		# Out of order and blocked rooms are not for sale either, on the
		# nights this stay newly takes; a night it already held is not
		# re-checked, so shortening or extending a stay still works
		held, out = {}, {}
		if self._takes_new_inventory():
			from kamra.group_blocks import block_hold_by_night
			held = block_hold_by_night(self.property, self.room_type, dates,
			                           for_group=self.group_booking, for_update=True)
			before = self._nights_held_before()
			new_nights = [d for d in dates if d not in before]
			if new_nights:
				out = self._rooms_out_of_sale(new_nights)
		for d in dates:
			cnt = len(frappe.db.sql(
				"""SELECT name FROM `tabReservation`
				   WHERE room_type = %(rt)s AND name != %(name)s
				     AND status IN ('Confirmed', 'Checked In', 'Held', 'Pending Payment')
				     AND check_in_date <= %(d)s
				     AND GREATEST(check_out_date,
				                  DATE_ADD(check_in_date, INTERVAL 1 DAY)) > %(d)s
				   FOR UPDATE""",
				{"rt": self.room_type, "name": self.name or "new", "d": d},
			))
			hold = held.get(d, 0)
			rooms = total - out.get(d, 0)
			limit = int(rooms * (1 + pct / 100))
			if cnt + hold + 1 > limit:
				if hold:
					frappe.throw(
						_("{0} is sold out for {1}: {2} of {3} rooms sold and "
						  "{4} held for group blocks until their cutoff.").format(
							self.room_type, d, cnt, rooms, hold),
						title=_("Held for a group"), exc=InventoryRefused)
				if out.get(d):
					frappe.throw(
						_("{0} is sold out for {1}: {2} of {3} sellable rooms sold "
						  "({4} out of order or blocked).").format(
							self.room_type, d, cnt, rooms, out[d]),
						title=_("Overbooking limit"), exc=InventoryRefused)
				frappe.throw(
					_("{0} is sold out for {1}: {2} of {3} rooms sold and "
					  "the overbooking allowance ({4}%) is used up.").format(
						self.room_type, d, cnt, rooms, pct),
					title=_("Overbooking limit"), exc=InventoryRefused)

	def _nights_held_before(self) -> set[str]:
		"""Nights this stay already held in this room type before this save.
		A date change re-checks every night; these are not re-checked
		against the capacity that out of order or blocked rooms reduce."""
		from frappe.utils import add_days

		from kamra.reservation_state import holds_inventory
		old = None if self.is_new() else self.get_doc_before_save()
		if not old or old.room_type != self.room_type or not holds_inventory(old.status):
			return set()
		n = max(1, date_diff(old.check_out_date, old.check_in_date))
		return {str(add_days(old.check_in_date, i)) for i in range(min(int(n), 366))}

	def _rooms_out_of_sale(self, dates: list[str]) -> dict[str, int]:
		"""Rooms of this type that can't be sold on each night: Out of Order,
		or held by an active Room Block. A stay already sitting in one of
		those rooms is still counted as sold, so this can over-count by one
		for a guest in house in a room marked Out of Order mid-stay; that
		errs on refusing a booking, never on overbooking."""
		rooms = frappe.get_all("Room", filters={"room_type": self.room_type},
		                       fields=["name", "housekeeping_status"])
		ooo = {r.name for r in rooms if r.housekeeping_status == "Out of Order"}
		blocks = frappe.get_all("Room Block", filters={
			"room": ("in", [r.name for r in rooms]),
			"block_status": "Active",
			"from_date": ("<=", dates[-1]), "to_date": (">", dates[0])},
			fields=["room", "from_date", "to_date"])
		# a block holds its nights [from_date, to_date)
		return {d: len(ooo | {b.room for b in blocks
		                      if str(b.from_date) <= d < str(b.to_date)})
		        for d in dates}

	def _takes_new_inventory(self) -> bool:
		"""True when this save starts holding rooms it did not hold before:
		a new live booking, one entering a live status, or a change of
		dates / room type."""
		from kamra.reservation_state import holds_inventory
		if self.is_new():
			return True
		old = self.get_doc_before_save()
		if not old or not holds_inventory(old.status):
			return True
		return (str(old.check_in_date), str(old.check_out_date), old.room_type) \
			!= (str(self.check_in_date), str(self.check_out_date), self.room_type)

	def validate(self):
		# A ledger path that saves the booking sets flags.kamra_ledger_advance;
		# Frappe never fills doc.flags from a client payload.
		if self.is_new():
			moved = flt(self.advance_paid) != 0
		else:
			before = self.get_doc_before_save()
			moved = bool(before) and flt(before.advance_paid) != flt(self.advance_paid)
		if moved and not self.flags.get("kamra_ledger_advance"):
			from kamra.api import AdvancePaidIsLedgerOwned
			frappe.throw(_("Advance paid follows the payments on the folio; take a payment or refund there."),
			             AdvancePaidIsLedgerOwned)
		self.guard_discount_inputs()
		self.guard_locked_nights()
		# Channel review flag and reason are owned by channel_manager (db_set)
		# and cleared only through clear_channel_review (role check + audit log).
		# Any insert with them set or save that changes them is refused.
		prev_doc = None if self.is_new() else self.get_doc_before_save()
		prev_review = 0 if not prev_doc else cint(prev_doc.get("channel_review_needed"))
		prev_reason = "" if not prev_doc else (prev_doc.get("channel_review_reason") or "")
		if cint(self.get("channel_review_needed")) != prev_review or (self.get("channel_review_reason") or "") != prev_reason:
			frappe.throw(
				_("Channel review status can only be cleared via the review action."),
				frappe.PermissionError,
			)
		# Before any other inventory lock, so villa vs room and category
		# capacity take this row in the same order as booking and accept.
		if self.property:
			lock_property_for_inventory(self.property)
		self._sync_ota_live_key()
		if self.get("booked_check_out_date") and getdate(self.check_out_date) \
				>= getdate(self.booked_check_out_date):
			# extended back to (or past) the booked date: no longer leaving early
			self.booked_check_out_date = None
			self.early_departure_reason = None
		self.validate_dates()
		self._check_out_only_through_check_out()
		self.validate_past_check_in()
		self.nights = date_diff(self.check_out_date, self.check_in_date)
		self.validate_minimum_nights()
		self.validate_status_transition()
		self.validate_blacklist()
		self.validate_occupancy()
		self.default_occupant_nationality()
		self.validate_room_belongs_to_type()
		self.validate_no_overlap()
		self.validate_itinerary_leg()
		self.validate_room_vacant()
		self.validate_villa_lockout()
		self.validate_type_capacity()
		self.validate_cancellation_path()
		self.apply_negotiated_rate_plan()
		self.capture_contract_tariff()
		self.guard_agreed_rate()
		self.keep_hand_set_nights()
		self.apply_pricing()
		self.govern_discount()
		self.stamp_room_assignment()

	def default_occupant_nationality(self):
		"""An occupant added without a nationality takes the property
		country's own (the pack's DEFAULT_NATIONALITY), never a fixed one.
		Rows already on file, and a nationality the desk typed, are kept."""
		new_rows = [o for o in self.get("occupants") or []
		            if o.is_new() and not (o.nationality or "").strip()]
		if not new_rows or not self.property:
			return
		from kamra.localization import default_nationality, pack_for
		nationality = default_nationality(pack_for(self.property))
		for o in new_rows:
			o.nationality = nationality or None

	def stamp_room_assignment(self):
		"""Who put the guest in this room, and when. Every writer that
		changes the room (booking, allocator, check-in, room move, upgrade)
		goes through this save, so the record follows the room: a later move
		is stamped with whoever made it."""
		old = None if self.is_new() else self.get_doc_before_save()
		if self.room and self.room != (old.room if old else None):
			self.room_assigned_by = frappe.session.user
			self.room_assigned_at = now_datetime()

	def apply_negotiated_rate_plan(self):
		"""A new booking for a company with no rate plan of its own takes
		the plan the company negotiated. Stored on the booking, so the quote,
		the locked night rates and every folio night agree on it. Only new
		bookings: a confirmed stay is never re-priced by a contract change."""
		if not self.is_new() or self.rate_plan or not self.get("company"):
			return
		from kamra.pricing import negotiated_rate_plan
		plan = negotiated_rate_plan(self.property, self.company)
		if plan:
			self.rate_plan = plan

	def capture_contract_tariff(self):
		"""Freeze the read resolver's reference at booking, not on later reads.
		The one exception is a contract locked at check-in, whose exchange
		rate check-in has just converted again (pricing.relock_contract_fx_at_check_in;
		kamra_fx_checkin_relock is the tariff resolver's flag for a stay it priced).
		An OTA booking in its rate plan's currency brings its own snapshot, the
		OTA's price converted at booking (kamra_channel_tariff, channel_manager).
		New dates price the nights the contract never priced (reprice_contract_nights)."""
		if self.flags.get("kamra_channel_tariff"):
			return
		if not self.is_new():
			if self.flags.get("kamra_contract_relocked") or self.flags.get("kamra_fx_checkin_relock"):
				return
			previous = self.get_doc_before_save()
			self.contract_tariff = previous.get("contract_tariff") if previous else None
			if previous:
				self.reprice_contract_nights(previous.get("check_in_date"), previous.get("check_out_date"),
					booked_agreed_rate=previous.get("agreed_rate"))
			return
		self.contract_tariff = None
		if not self.auto_price or (self.group_booking and flt(self.agreed_rate) > 0):
			return  # external or block totals already set the room price
		from kamra.pricing import billable_children, contract_rate
		resolved = contract_rate(self.property, self.room_type, self.rate_plan, self.check_in_date,
			self.check_out_date, self.adults, billable_children(self), self.get("extra_beds"), company=self.get("company"))
		if not resolved:
			return
		agreed_rate, tariff = resolved
		# A site's Before Validate writer may already have set the rate. Refuse
		# a disagreement rather than store a reference to a different price.
		if flt(self.agreed_rate) and flt(self.agreed_rate) != agreed_rate:
			frappe.throw(_("The contract rate changed. Refresh the quote before booking."))
		self.agreed_rate = agreed_rate
		# Freeze the booked pricing company at the top level of contract_tariff
		# (keeping contract_tariff["tariff"] unchanged) so change_stay_company
		# keeps the booked rate across later date extensions (OPERA Linked Profiles).
		self.contract_tariff = {
			"tariff": tariff,
			"agreed_rate": agreed_rate,
			"company": self.get("company") or None,
		}

	def reprice_contract_nights(self, booked_check_in, booked_check_out, booked_agreed_rate=None):
		"""The contract for the stay's new dates, from the ones it was priced
		for (pricing.contract_tariff_for_new_dates): an added night is
		resolved at its own tariff, or the new dates are refused; the save and
		the amendment previews both run it, so they agree."""
		from kamra.pricing import contract_tariff_for_new_dates
		snapshot = contract_tariff_for_new_dates(self, booked_check_in, booked_check_out,
			booked_agreed_rate=booked_agreed_rate)
		if snapshot is not None:
			self.contract_tariff = snapshot
			if snapshot.get("agreed_rate") is not None:
				self.agreed_rate = snapshot["agreed_rate"]
				self.flags.kamra_contract_repriced = True

	def validate_minimum_nights(self):
		"""Property.minimum_nights is a hard floor for overnight stays."""
		if self.status in ("Cancelled", "No Show", "Checked Out", "Inquiry", "Quoted"):
			return
		# day-use is nights == 0; min nights applies to overnight only
		nights = date_diff(self.check_out_date, self.check_in_date)
		if nights <= 0:
			return
		min_n = cint(frappe.db.get_value(
			"Property", self.property, "minimum_nights") or 1)
		if min_n > 1 and nights < min_n:
			frappe.throw(
				_("This property requires a stay of at least {0} nights.").format(
					min_n),
				title=_("Minimum stay"),
			)

		# Peak dates can ask for longer than the property's own floor - a
		# two-night minimum over New Year, say - and the rule lives on the
		# Season that already prices those nights.
		from kamra.pricing import stay_season_rules

		rules = stay_season_rules(self.property, self.check_in_date,
		                          self.check_out_date, self.room_type)
		if rules["minimum_nights"] > nights:
			frappe.throw(
				_("{0} needs a stay of at least {1} nights.").format(
					rules["season"] or _("These dates"),
					rules["minimum_nights"]),
				title=_("Minimum stay"),
			)

	def validate_status_transition(self):
		if self.is_new():
			return
		old = self.get_doc_before_save()
		if not old:
			return
		from kamra.reservation_state import assert_transition
		assert_transition(old.status, self.status)
	def validate_cancellation_path(self):
		"""Cancellations must go through cancel_reservation so the
		property's cancellation policy is applied (or knowingly waived) —
		flipping the status field would silently skip the fee."""
		if self.is_new() or frappe.flags.kamra_cancelling:
			return
		old = self.get_doc_before_save()
		if old and old.status != "Cancelled" and self.status == "Cancelled":
			frappe.throw(_(
				"Use the Cancel action (or the cancel_reservation API) so "
				"the cancellation policy is applied."))

	def validate_occupancy(self):
		"""A room only sleeps so many. Checked when the party or room type
		changes — legacy over-capacity stays can still check out."""
		if not self.room_type:
			return
		old = None if self.is_new() else self.get_doc_before_save()
		if old and (
			cint(old.adults), cint(old.children), cint(old.get("over_capacity_guests")),
			cint(old.get("infants")), old.room_type,
		) == (cint(self.adults), cint(self.children),
		      cint(self.get("over_capacity_guests")), cint(self.get("infants")),
		      self.room_type):
			return
		adults = cint(self.adults)
		# infants stay free and sleep with their parents - they take no bed
		children = max(0, cint(self.children) - cint(self.get("infants")))
		extra = max(0, cint(self.get("over_capacity_guests")))
		if adults < 1:
			frappe.throw(_("A stay needs at least one adult."),
				title=_("Room capacity"))
		cap = frappe.db.get_value(
			"Room Type", self.room_type,
			["adults_capacity", "children_capacity", "room_type_name",
			 "max_extra_beds", "max_total_occupants"],
			as_dict=True,
		) or frappe._dict()
		cap_a, cap_c = cint(cap.adults_capacity), cint(cap.children_capacity)

		# A room can take a few more than it sleeps comfortably, by
		# arrangement - a family with one more child, a group happy to share.
		# The desk books them as extra guests on top of a full room; older
		# bookings and API callers may still count them inside adults or
		# children (with over_capacity_guests then naming those same people,
		# so the two are never added). Either way it is a decision someone
		# signs their name to, so it needs a reason and stops at the extra
		# beds the room type allows.
		from kamra.pricing import capacity_overflow
		above, beds = capacity_overflow(cap, adults, children, extra)
		stated = (self.get("over_capacity_reason") or "").strip()
		allowance = beds if stated else 0
		if beds and above and not stated:
			frappe.throw(_(
				"{0} sleeps {1}. Up to {2} more can be booked by arrangement "
				"- give a reason on the booking to record who agreed it."
			).format(cap.room_type_name or self.room_type, cap_a, beds),
				title=_("Over room capacity"))

		over = []
		if above > allowance:
			if extra:
				over.append(_("{0} extra guests (max {1})").format(above, allowance))
			if cap_a and adults > cap_a + allowance:
				over.append(_("{0} adults (max {1})").format(
					adults, cap_a + allowance))
			if cap_c and children > cap_c + allowance:
				over.append(_("{0} children (max {1})").format(
					children, cap_c + allowance))
			if not over:
				over.append(_("{0} guests above what it sleeps (max {1})").format(
					above, allowance))
		if over:
			frappe.throw(_(
				"{0} can't sleep {1}. Reduce the party, pick a bigger room "
				"type, or book additional rooms — use a Group Booking for "
				"large parties."
			).format(cap.room_type_name or self.room_type, " {} ".format(_("and")).join(over)),
				title=_("Over room capacity"))

	def validate_dates(self):
		diff = date_diff(self.check_out_date, self.check_in_date)
		if getattr(self, "is_day_use", 0):
			if diff != 0:
				frappe.throw(_("Day-use stays check out the same day."))
		elif diff < 1:
			frappe.throw(_("Check-out must be after check-in."))

	def validate_past_check_in(self):
		"""Check-in cannot be before calendar today for sellable stays.

		- New bookings with a past check-in are rejected.
		- Edits that *change* check-in to a past date are rejected (so desk
		  cannot quietly backdate Confirmed / Held / Checked In stays).
		- Saving an in-house stay without changing dates stays allowed —
		  arrival already happened, the nights are historical.
		- Cancelled / No Show are exempt (not inventoriable).
		- Walk-in catch-up, seeding, and imports set flags.allow_past_check_in
		  (or ignore_validate on history import).
		"""
		if self.flags.get("allow_past_check_in"):
			return
		if self.status in ("Cancelled", "No Show"):
			return
		if date_diff(today(), self.check_in_date) <= 0:
			return
		if not self.is_new():
			old = self.get_doc_before_save()
			if old and date_diff(old.check_in_date, self.check_in_date) == 0:
				return
		frappe.throw(
			_("Check-in {0} has already passed. Pick today or a later date.")
			.format(formatdate(self.check_in_date)),
			title=_("Check-in date is in the past"),
		)

	def validate_blacklist(self):
		if not self.guest or not self.is_new():
			return
		# a Hotel Admin confirmed this one guest, with a reason (api._blacklist_gate)
		if frappe.flags.get("kamra_blacklist_override") == self.guest:
			return
		flagged, reason = frappe.db.get_value(
			"Guest", self.guest, ["blacklisted", "blacklist_reason"]
		) or (0, None)
		if flagged:
			frappe.throw(
				_("Guest {0} is blacklisted{1}. Remove the flag on the guest "
				  "profile to book.").format(
					self.guest_name or self.guest,
					f" — {reason}" if reason else "",
				),
				title=_("Blacklisted guest"),
			)

	def quote_inputs(self) -> dict:
		"""What the pricing engine needs to price this stay as it stands."""
		voucher_code = None
		if self.voucher:
			voucher_code = frappe.db.get_value(
				"Discount Voucher", self.voucher, "voucher_code"
			)
		# Deduct free children under free_child_age from billing
		from kamra.pricing import billable_children
		charged_children = billable_children(self)
		inputs = {
			"property": self.property,
			"room_type": self.room_type,
			"check_in_date": str(self.check_in_date),
			"check_out_date": str(self.check_out_date),
			"adults": int(self.adults or 0),
			"children": charged_children,
			"meal_plan": self.meal_plan or None,
			"rate_plan": self.rate_plan or None,
			"voucher_code": voucher_code,
			"over_capacity_charge": float(self.get("over_capacity_charge") or 0),
		}
		# only when there are pets, so the rate-lock key of every booking
		# made before pets existed stays the same and nothing re-prices
		if cint(self.get("pets")) > 0:
			inputs["pets"] = cint(self.get("pets"))
		# same rule: only when set, so existing rate-lock keys stay put
		if flt(self.get("agreed_rate")) > 0:
			inputs["agreed_rate"] = flt(self.get("agreed_rate"))
			# a contract over a tariff change: each night's own rate, only
			# then - a one-rate contract keeps its rate-lock key
			from kamra.pricing import contract_night_rates
			nights = contract_night_rates(self)
			if nights:
				inputs["agreed_nights"] = nights
		# nights the desk priced by hand: only when there are some, so every
		# other booking's rate-lock key stays the same
		hand = self.hand_set_prices()
		if hand:
			inputs["night_prices"] = hand
		# the desk's discount, the same way: only when there is one
		if self.get("discount_type") and flt(self.get("discount_value")) > 0:
			inputs["discount_type"] = self.discount_type
			inputs["discount_value"] = flt(self.discount_value)
		nights = self.night_discount_inputs()
		if nights:
			inputs["night_discounts"] = nights
		# Day-use times only enter the lock key when the stay is same-day:
		# overnight ETA/ETD is display-only and must not re-price.
		if cint(self.get("is_day_use")) or (
				self.check_in_date and self.check_out_date
				and getdate(self.check_in_date) == getdate(self.check_out_date)):
			from kamra.day_use import format_hhmm
			if self.get("planned_check_in_time"):
				inputs["check_in_time"] = format_hhmm(self.planned_check_in_time)
			if self.get("planned_check_out_time"):
				inputs["check_out_time"] = format_hhmm(self.planned_check_out_time)
		return inputs

	def hand_set_prices(self) -> list[dict]:
		"""The nights the desk priced by hand that are still inside the stay,
		as the {date, price} list the quote takes (price as the desk typed it)."""
		if not self.check_in_date or not self.check_out_date:
			return []
		first, last = getdate(self.check_in_date), getdate(self.check_out_date)
		rows = [{"date": str(getdate(r.stay_date)), "price": flt(r.hand_price)}
		        for r in self.get("night_rates") or []
		        if flt(r.get("hand_price")) > 0
		        and first <= getdate(r.stay_date) < last]
		return sorted(rows, key=lambda r: r["date"])

	def night_discount_inputs(self) -> list[dict]:
		"""The nights with a discount of their own (Daily Details) still inside
		the stay, as the quote takes them. A night row with no discount type
		follows the stay's discount; its base / discount / rate columns are
		what the quote worked out, never an input."""
		if not self.check_in_date or not self.check_out_date:
			return []
		first = getdate(self.check_in_date)
		last = max(getdate(self.check_out_date), getdate(add_days(first, 1)))
		initial = self.flags.get("kamra_initial_night_discounts")
		if self.is_new() and initial and not self.get("night_rates"):
			# Daily Details given at booking (create_booking, walk_in) ride on
			# flags, so the insert carries no night rows until this save locks
			# them: a Before Validate writer (hospitality-tariffs' tariff
			# resolver, HSP-TARIFF-007) sees a booking that is not priced yet
			return sorted((dict(n) for n in initial if first <= getdate(n["date"]) < last),
			              key=lambda n: n["date"])
		rows = [{"date": str(getdate(r.stay_date)), "discount_type": r.discount_type,
		         "discount_value": flt(r.get("discount_value")),
		         "discount_reason": r.get("discount_reason") or None,
		         "fixed_rate": cint(r.get("fixed_rate"))}
		        for r in self.get("night_rates") or []
		        if (r.get("discount_type") or cint(r.get("fixed_rate")))
		        and first <= getdate(r.stay_date) < last]
		return sorted(rows, key=lambda r: r["date"])

	def guard_discount_inputs(self):
		"""A discount is granted only through the governed actions
		(create_booking, walk_in, set_reservation_discount), which check the
		reason, the desk's limit and any supervisor sign-off and then set
		flags.kamra_reservation_discount. Frappe never fills doc.flags from a
		client payload, so a discount written straight onto the booking - its
		own fields or a night row's - is refused. set_agreed_rate may mark the
		stay a fixed rate. A night that drops out of the stay takes its
		discount with it."""
		if self.flags.get("kamra_reservation_discount"):
			return
		old = None if self.is_new() else self.get_doc_before_save()
		(stay_was, nights_were) = discount_inputs(old) if old else ((None, 0.0, None, 0), set())
		stay, nights = discount_inputs(self)
		if self.flags.get("kamra_agreed_rate_override"):
			stay, stay_was = stay[:3], stay_was[:3]
		# a Daily Details night leaves the booking only with its date (a
		# shorter stay); clearing or changing one inside the stay is a new
		# discount like any other
		first, last = self.stay_window()
		dropped = [n for n in nights_were - nights
		           if first and first <= getdate(n[0]) < last]
		if stay != stay_was or not nights <= nights_were or dropped:
			frappe.throw(_("A discount is given on the booking screen, with a reason, and "
			               "signed off by a supervisor when it needs one."),
			             frappe.PermissionError, title=_("Discount"))

	def guard_locked_nights(self):
		"""A locked night's price (rate, base, discount, tax rate, meals and
		the price the desk typed) is what the folio bills, and only the
		booking's own pricing writes it: this save's apply_pricing (after this
		check), or the channel manager (flags.kamra_night_rates_writer). The
		fields are read-only on the form, but read_only is not enforced on a
		REST PUT, so a night row that arrives changed, added inside the stay
		or taken out of it is refused against what was saved.

		A new booking carries night rows, or is priced by hand (auto_price
		off), only from a writer that checked it: create_booking's typed
		nights (after _check_night_prices: flags.kamra_night_prices_writer),
		an OTA total, the channel manager or an import (night_rates_writer).
		Any other insert (REST POST) is refused: its nights are priced by this
		save, never by the caller. An existing stay's pricing is turned off
		(auto_price 1 to 0) only by those same writers."""
		if self.is_new():
			if self.night_rates_writer() or self.flags.get("kamra_night_prices_writer"):
				return
			days = sorted({str(getdate(r.stay_date)) for r in self.get("night_rates") or []
			               if r.get("stay_date")})
			if self.get("night_rates"):
				self.refuse_locked_nights(days or [_("a night")])
			if not cint(self.get("auto_price")):
				self.refuse_hand_price()
			return
		if self.night_rates_writer():
			return
		old = self.get_doc_before_save()
		if not old:
			return
		if cint(old.get("auto_price")) and not cint(self.get("auto_price")):
			self.refuse_hand_price()

		def by_date(doc):
			out: dict = {}
			for r in doc.get("night_rates") or []:
				out.setdefault(str(getdate(r.stay_date)), []).append(r)
			return out
		was, now = by_date(old), by_date(self)
		first, last = self.stay_window()

		def inside(day):
			return bool(first) and first <= getdate(day) < last

		tampered = []
		for day, rows in now.items():
			before = was.get(day) or []
			if len(rows) > max(len(before), 1):
				tampered.append(day)  # a second row for a night
				continue
			if not before:
				# a governed discount may add an unpriced row the save prices
				if not (self.flags.get("kamra_reservation_discount")
				        and not any(flt(rows[0].get(c)) for c in LOCKED_NIGHT_COLUMNS)):
					tampered.append(day)
				continue
			if any(abs(flt(rows[0].get(c)) - flt(before[0].get(c))) >= 0.005
			       for c in LOCKED_NIGHT_COLUMNS):
				tampered.append(day)
		tampered += [day for day in was if day not in now and inside(day)]
		if tampered:
			self.refuse_locked_nights(tampered)

	def night_rates_writer(self) -> bool:
		"""This save writes the stay's locked nights besides its own pricing:
		the channel manager or one of Kamra's imports
		(flags.kamra_night_rates_writer), or Frappe's own Data Import, which
		the server runs with frappe.flags.in_import set (Document.insert then
		skips the DocType's defaults, auto_price among them). Frappe resets
		frappe.flags for every request and fills neither from a payload."""
		return bool(self.flags.get("kamra_night_rates_writer") or frappe.flags.get("in_import"))

	@staticmethod
	def refuse_hand_price():
		frappe.throw(_("A booking is priced by hand only from the booking screen (nightly "
		               "prices or an OTA total), the channel manager or an import."),
		             frappe.PermissionError, title=_("Locked night rate"))

	@staticmethod
	def refuse_locked_nights(days):
		frappe.throw(_("The locked rate of {0} is changed only by the booking's own pricing "
		               "(amend the stay, move the room, an agreed rate or a discount).").format(
			", ".join(sorted(set(days)))),
			frappe.PermissionError, title=_("Locked night rate"))

	def guard_agreed_rate(self):
		"""An agreed rate (OPERA's rate override, a Fixed Rate) is set,
		changed or cleared only by its writers: set_agreed_rate and a room
		move (flags.kamra_agreed_rate_override, after the desk's limit and any
		supervisor's sign-off), a group pickup (create_booking, the same flag),
		the channel manager's foreign-contract stamp/clear
		(flags.kamra_channel_tariff) and a contract tariff
		(capture_contract_tariff, check-in FX relock or multi-rate contract
		reprice, just before this). The field is read-only on the form, but
		read_only is not enforced on a REST PUT or POST, so any other change is
		refused."""
		if (self.flags.get("kamra_agreed_rate_override")
				or self.flags.get("kamra_contract_relocked")
				or self.flags.get("kamra_fx_checkin_relock")
				or self.flags.get("kamra_contract_repriced")
				or self.flags.get("kamra_channel_tariff")):
			return
		if self.is_new():
			changed = flt(self.get("agreed_rate")) > 0 and not self.get("contract_tariff")
		else:
			old = self.get_doc_before_save()
			changed = bool(old) and abs(flt(old.get("agreed_rate")) - flt(self.get("agreed_rate"))) >= 0.005
		if changed:
			frappe.throw(_("An agreed rate is set with Agreed rate on the reservation: a Rate Change "
			               "reason, the desk's limit and a supervisor's sign-off when it needs one."),
			             frappe.PermissionError, title=_("Agreed rate"))

	def agreed_rate_overrides(self) -> bool:
		"""The stay's agreed rate overrides its rate code (set_agreed_rate, a
		custom-rate room move). A contract tariff's or a group block's rate is
		the stay's rate code itself, so it is not an override."""
		rate = flt(self.get("agreed_rate"))
		if rate <= 0:
			return False
		tariff = self.get("contract_tariff")
		if isinstance(tariff, str):
			try:
				tariff = frappe.parse_json(tariff)
			except Exception:
				tariff = None
		if isinstance(tariff, dict) and abs(flt(tariff.get("agreed_rate")) - rate) < 0.005:
			return False
		if self.get("group_booking"):
			from kamra.group_blocks import group_rate
			if abs(flt(group_rate(self.group_booking, self.room_type)) - rate) < 0.005:
				return False
		return True

	def holds_manager_fixed_rate(self) -> bool:
		"""A manager's fixed-rate override of the discount (a plan that takes
		none), which a re-price of the stay keeps. A fixed rate that is only an
		agreed rate set by hand never waives a plan's Discount eligible flag."""
		return bool(cint(self.get("fixed_rate"))) and not self.agreed_rate_overrides()

	def stored(self):
		"""The booking as it is saved: the document before this save, or for a
		preview (nothing saved yet) the stored record. None for a new one."""
		getter = getattr(self, "get_doc_before_save", None)
		old = getter() if callable(getter) else None
		if old is not None or self.is_new() or not self.get("name"):
			return old
		if self.flags.get("kamra_stored") is None:
			self.flags.kamra_stored = frappe.get_doc("Reservation", self.name)
		return self.flags.kamra_stored

	def rate_code_amounts(self) -> dict[str, float]:
		"""Each night's rate-code amount while an agreed rate overrides the
		rate code ({date: amount}, on the desk's basis); empty when the stay
		sells at its rate code, whose amount is the night's own base. A night
		keeps the amount it was booked with while its room type and rate plan
		stay; a night new to the stay, or one moved to another room type or
		plan, takes its rate-code amount now (card_entry_bases)."""
		if not self.agreed_rate_overrides():
			return {}
		from kamra.pricing import night_rate_places, rates_include_tax
		places = night_rate_places(self.property)
		out: dict[str, float] = {}
		old = self.stored()
		if old and old.room_type == self.room_type and \
				(old.get("rate_plan") or None) == (self.get("rate_plan") or None):
			inclusive = rates_include_tax(self.property)
			overrode = old.agreed_rate_overrides() if callable(getattr(old, "agreed_rate_overrides", None)) else False
			for r in old.get("night_rates") or []:
				amount = flt(r.get("rate_code_amount"))
				if not amount and not overrode:
					# a night locked before rate-code amounts were kept: its
					# base was the rate code's
					amount = night_entry_base(r, inclusive, places)
				if amount > 0:
					out[str(getdate(r.stay_date))] = round(amount, places)
		missing = [day for day in self.stay_nights() if day not in out]
		if missing:
			card = self.card_entry_bases()
			out.update({day: card[day] for day in missing if day in card})
		return out

	def card_entry_bases(self) -> dict[str, float]:
		"""Each night's rate-code amount now: the stay priced without its
		agreed rate, typed nights or the desk's discount (the contract tariff
		when the stay carries one, else the rate card, season, demand and rate
		plan), on the desk's basis."""
		from kamra.pricing import entry_rate, night_rate_places, quote, rates_include_tax, saved_contract_tariff
		inputs = {k: v for k, v in self.quote_inputs().items()
		          if k not in ("agreed_rate", "agreed_nights", "night_prices", *DISCOUNT_INPUT_KEYS)}
		snapshot = saved_contract_tariff(self)
		if snapshot and flt(snapshot.get("agreed_rate")) > 0:
			tariff = snapshot.get("tariff") or {}
			own = {str(n["date"]): flt(n["agreed_rate"]) for n in tariff.get("nights") or [] if n.get("date")}
			places = night_rate_places(self.property)
			return {day: round(own.get(day, flt(snapshot["agreed_rate"])), places)
			        for day in self.stay_nights()}
		q = quote(**inputs, check_voucher_limits=False,
		          exclude_reservation=None if self.is_new() else self.name)
		nightly = q.get("nightly") or []
		plan_free = sum(float(n["rate"]) for n in nightly)
		factor = float(q.get("room_total") or 0) / plan_free if plan_free else 1.0
		inclusive = rates_include_tax(self.property)
		places = night_rate_places(self.property)
		return {str(n["date"]): round(float(entry_rate(
			float(n["planned_rate"]) if "planned_rate" in n else float(n["rate"]) * factor,
			n.get("gst_rate"), inclusive, places)), places)
		        for n in nightly}

	def pinned_room_nights(self, inputs: dict) -> dict | None:
		"""The booked room nights a re-quote keeps as they are ({date: row}),
		or None when the room is priced again. OPERA keeps a booked room rate
		when a meal plan is added or changed, and keeps a Fixed Rate when the
		party changes: only the meals, the voucher, the fees, the levy and the
		tax around the room are priced again (pricing.quote locked_nights).
		Only for an auto-priced stay whose every night is locked under the
		inputs it holds, with the same dates, when nothing else that is sold
		changed (room type, rate plan, agreed rate, the discount, a voucher,
		extra guests, pets) and the caller did not ask for a re-quote by
		clearing or replacing the lock (correct_charged_nights)."""
		if self.is_new() or not cint(self.get("auto_price")) or self.flags.get("correct_charged_nights"):
			return None
		old = self.stored()
		if (not old or not cint(old.get("auto_price")) or not old.get("night_rates")
				or not callable(getattr(old, "lock_is_current", None))
				or not old.lock_is_current()
				or (self.get("rate_lock_key") or None) != (old.get("rate_lock_key") or None)):
			return None
		if cint(self.get("is_day_use")) or self.stay_window() != old.stay_window():
			return None
		was = old.quote_inputs()
		changed = {k for k in set(inputs) | set(was) if inputs.get(k) != was.get(k)}
		allowed = set(ROOM_NEUTRAL_INPUTS) | (set(PARTY_INPUTS) if cint(self.get("fixed_rate")) else set())
		if not changed or not changed <= allowed:
			return None
		rows = {str(getdate(r.stay_date)): {f: r.get(f) for f in NIGHT_ROW_FIELDS}
		        for r in old.night_rates}
		if not set(self.stay_nights()) <= set(rows):
			return None
		return rows

	def stay_window(self) -> tuple:
		"""(first night, the day after the last night) - a day-use stay
		bills one night, its check-in date. (None, None) without dates."""
		if not self.check_in_date or not self.check_out_date:
			return None, None
		first = getdate(self.check_in_date)
		return first, max(getdate(self.check_out_date), getdate(add_days(first, 1)))

	def stay_nights(self) -> list[str]:
		first, last = self.stay_window()
		if not first:
			return []
		return [str(add_days(first, i)) for i in range(date_diff(last, first))]

	def nights_cover_stay(self) -> bool:
		"""Every night of the stay has its locked row."""
		dates = {str(getdate(r.stay_date)) for r in self.get("night_rates") or []}
		nights = self.stay_nights()
		return bool(nights) and set(nights) <= dates

	def govern_discount(self):
		"""The discount rules (kamra.api.discount_governance) on the nights
		this save just locked, when a governed action put a discount on the
		booking - so the reason, the desk's limit and any sign-off are
		checked against exactly what the folio will bill."""
		if self.flags.get("kamra_discount_context"):
			from kamra.api import govern_reservation_discount
			govern_reservation_discount(self)
			return
		self.regovern_repriced_discount()

	def has_discount(self) -> bool:
		if self.get("discount_type") and flt(self.get("discount_value")) > 0:
			return True
		return any(r.get("discount_type") and flt(r.get("discount_value")) > 0
		           for r in self.get("night_rates") or [])

	def regovern_repriced_discount(self):
		"""A discounted stay this save re-priced (amend_stay, move_reservation
		or a direct edit) takes the discount rules again on the nights
		judged_nights names. A plan that takes no discount, a cut over the
		desk's limit or under the rate floor, or a night at no charge is
		refused unless a supervisor signs it off (flags.kamra_discount_supervisor);
		a shorter stay adds nothing.

		An agreed rate never stacks on a discount: set_agreed_rate and a
		custom-rate room move replace it (OPERA's Fixed Rate), and their own
		cut is measured from each night's rate-code amount, as every discount
		is (kamra.api.discount_governance). So neither a chain of agreed rates
		nor an agreed rate and a discount together step around the desk's
		limit or the full-comp sign-off."""
		if (self.is_new() or not self.flags.get("kamra_repriced") or not self.has_discount()
				or self.flags.get("kamra_contract_relocked")
				or self.flags.get("kamra_fx_checkin_relock")):
			return
		old = self.get_doc_before_save()
		if not old:
			return
		judged = self.judged_nights(old, self.night_rates)
		if not judged:
			return
		from kamra.api import govern_repriced_discount
		govern_repriced_discount(self, judged)

	def judged_nights(self, old, rows) -> set[str]:
		"""The nights a re-price of this stay (from `old`, the stay as saved,
		to `rows`, the nights it now locks) takes the discount rules on: every
		night when what is sold moved (rate plan, room type, agreed rate, or
		the party on a stay without a fixed rate), else each night added or
		whose locked price changed. The save and the previews use it alike."""
		was = {str(getdate(r.get("stay_date"))): r for r in old.get("night_rates") or []}
		now = {str(getdate(r.get("stay_date"))): r for r in rows or []}
		party = () if cint(self.get("fixed_rate")) else PARTY_FIGURES
		moved = any(str(old.get(f) or "") != str(self.get(f) or "") for f in SOLD_LINKS) or any(
			abs(flt(old.get(f)) - flt(self.get(f))) >= 0.005 for f in (*SOLD_FIGURES, *party))
		if moved:
			return set(now)
		return {day for day, r in now.items()
		        if day not in was or any(abs(flt(r.get(c)) - flt(was[day].get(c))) >= 0.005
		                                 for c in NIGHT_PRICE_COLUMNS)}

	def keep_hand_set_nights(self):
		"""Prices the nights a desk typed (night_rates rows carrying a
		hand_price, auto_price off): on the first save, quoted with those
		nights; after a date, room-type or party change, the typed nights
		still in the stay are kept and Kamra prices the others. With none
		left the booking goes back to auto pricing."""
		if self.get("auto_price") or not self.room_type:
			return
		if not any(flt(r.get("hand_price")) > 0 for r in self.get("night_rates") or []):
			return  # priced by hand as a whole (OTA total, desk amount)
		inputs = self.quote_inputs()
		key = self.rate_lock_key_for(inputs)
		if self.get("rate_lock_key") == key:
			return
		if not inputs.get("night_prices"):
			if self.is_new():
				frappe.throw(_("None of the nights with a typed price is in this stay."))
			self.auto_price = 1  # apply_pricing re-quotes and re-locks it
			return
		self.apply_quote(self.requote(), key)

	@staticmethod
	def rate_lock_key_for(inputs: dict) -> str:
		import hashlib
		import json
		raw = json.dumps(inputs, sort_keys=True, default=str)
		return hashlib.sha1(raw.encode()).hexdigest()[:20]  # nosec B324 -- change detector, not security

	def requote(self, check_voucher_limits: bool = False) -> dict:
		"""Price the stay as it stands now, without counting this booking
		in the house's occupancy. Used by the save path and the amendment
		previews, so a preview and the save that follows agree."""
		from kamra.pricing import quote, saved_contract_tariff
		inputs = self.quote_inputs()
		pinned = self.pinned_room_nights(inputs)
		# a re-quote that keeps the booked room nights re-prices no night
		self.flags.kamra_room_pinned = bool(pinned)
		tariff = None
		if flt(self.get("agreed_rate")) > 0 and not self.agreed_rate_overrides():
			tariff = saved_contract_tariff(self).get("tariff") or None
		return quote(**inputs,
		             check_voucher_limits=check_voucher_limits,
		             exclude_reservation=None if self.is_new() else self.name,
		             **({"locked_nights": pinned} if pinned else {}),
		             **({"contract_tariff": tariff} if tariff else {}))

	def apply_pricing(self):
		"""Price the booking from the engine while auto_price is on, and lock
		the confirmed nightly rates (`night_rates`).

		Once the stay has its rates, a save that does not change what is
		being sold (dates, room type, party, meal / rate plan, voucher)
		keeps them: later rate, season or demand changes never re-price a
		confirmed booking. Changing what is sold re-quotes and re-locks;
		the desk endpoints (amend_stay, move_reservation) show that price
		and ask for confirmation first. Turn auto_price off to hold manually
		negotiated amounts."""
		if not getattr(self, "auto_price", 0) or not self.room_type:
			return
		inputs = self.quote_inputs()
		key = self.rate_lock_key_for(inputs)
		if (not self.is_new() and self.get("night_rates")
				and self.get("rate_lock_key") == key
				and not self.has_value_changed("auto_price")):
			self._apply_commission()
			return
		if self.discount_changes_only(inputs):
			# OPERA keeps a booked rate: a discount given, changed or taken
			# off comes off each locked night's booked base, never today's card
			self.apply_rediscount(self.rediscount_locked_nights(), key)
			return

		q = self.requote(
			# the voucher's dates and use limit apply when it is first
			# attached; a saved booking keeps its discount on later edits
			check_voucher_limits=bool(
				self.is_new() or self.has_value_changed("voucher")),
		)
		self.apply_quote(q, key)
		self.flags.kamra_repriced = not self.flags.get("kamra_room_pinned")

	@classmethod
	def sold_key(cls, inputs: dict) -> str:
		"""The lock key of what is being sold, the desk's discount left out."""
		return cls.rate_lock_key_for({k: v for k, v in inputs.items()
		                              if k not in DISCOUNT_INPUT_KEYS})

	def lock_is_current(self) -> bool:
		"""The locked nights were priced from exactly what the booking holds
		now (its rate-lock key matches its quote inputs)."""
		return bool(self.get("rate_lock_key")) and \
			self.get("rate_lock_key") == self.rate_lock_key_for(self.quote_inputs())

	def discount_changes_only(self, inputs: dict) -> bool:
		"""This save changes the desk's discount and nothing else that is
		sold (dates, room type, party, meal / rate plan, voucher, agreed
		rate), on a stay whose every night is locked for what it held."""
		if (self.is_new() or self.has_value_changed("auto_price")
				or self.flags.get("correct_charged_nights") or not self.nights_cover_stay()):
			return False
		old = self.get_doc_before_save()
		# a caller that cleared or replaced the lock asks for a re-quote
		if (not old or not old.lock_is_current()
				or self.get("rate_lock_key") != old.get("rate_lock_key")):
			return False
		return self.sold_key(inputs) == self.sold_key(old.quote_inputs())

	def rediscount_locked_nights(self) -> dict:
		"""The stay's discount as it now stands, on the nights already locked:
		each night still to come keeps its booked base (and its meals) and
		takes the discount off it (pricing.discount_night, the same as at
		booking); a night on the folio keeps what it holds. Meals, cleaning,
		pets and the voucher stay at their booked price; the room's tax, the
		extra guests' tax and the levy follow the new room rate. Returns the
		rows, the totals and the quote snapshot the save writes, so a preview
		(kamra.api.discount_preview) is exactly what the save persists.
		Nothing is written."""
		import json
		from decimal import Decimal

		from kamra.folio import charged_nights
		from kamra.pricing import (
			_dec,
			contract_night_tariff,
			discount_night,
			entry_rate,
			night_rate_places,
			normalize_discounts,
			property_currency,
			rates_include_tax,
			room_gst_rate,
			round_rate_amount,
			saved_contract_tariff,
		)
		disc = normalize_discounts(self.get("discount_type"), self.get("discount_value"),
		                           self.night_discount_inputs(),
		                           self.check_in_date, self.check_out_date,
		                           property=self.property)
		stay = (disc or {}).get("stay")
		own = (disc or {}).get("nights") or {}
		inclusive = rates_include_tax(self.property)
		places = night_rate_places(self.property)
		tariff = (saved_contract_tariff(self).get("tariff")
		          if flt(self.get("agreed_rate")) > 0 and not self.agreed_rate_overrides() else None)
		rt = frappe.get_doc("Room Type", self.room_type)
		over_cap = _dec(self.get("over_capacity_charge"))
		charged = charged_nights(self.name)
		prop = frappe.get_cached_doc("Property", self.property)
		levy_pct = _dec(prop.get("room_levy_percent")) / Decimal(100)
		levy_taxed = cint(prop.get("room_levy_taxable")
		                  if prop.get("room_levy_taxable") is not None else 1)

		def oc(rate):
			return over_cap / (Decimal(1) + rate / Decimal(100)) if inclusive else over_cap

		rows, effective = [], {}
		d_room = d_tax = d_oc = d_oc_tax = d_levy = d_levy_tax = Decimal(0)
		for r in sorted(self.get("night_rates") or [], key=lambda r: getdate(r.stay_date)):
			row = {f: r.get(f) for f in NIGHT_ROW_FIELDS}
			day = str(getdate(r.stay_date))
			row["stay_date"] = day
			rows.append(row)
			mine = own.get(day)
			kind, value = (mine["type"], mine["value"]) if mine else (stay or (None, Decimal(0)))
			effective[day] = (kind, float(value), "night" if mine else ("stay" if kind else None))
			if day in charged:
				continue  # on the folio: it keeps what it holds
			rate, gst, off = _dec(r.rate), _dec(r.gst_rate), _dec(r.discount_amount)
			base = _dec(r.base_rate) if flt(r.get("base_rate")) else rate
			# the night's booked base on the desk's basis, and its tax rate
			entry = entry_rate(rate, gst, inclusive, places) + off
			base_gst = room_gst_rate(self.property, rt, entry + over_cap) if off else gst
			net, new_gst, new_off = discount_night(
				self.property, rt, base, base_gst, entry,
				kind, value, inclusive, over_cap, day,
				tariff_night=contract_night_tariff(tariff, day, rate_plan=self.rate_plan),
			)
			row.update(rate=float(net), gst_rate=float(new_gst), base_rate=float(base),
			           discount_amount=float(new_off))
			d_room += net - rate
			d_tax += (net * new_gst - rate * gst) / Decimal(100)
			if over_cap and new_gst != gst:
				d_oc += oc(new_gst) - oc(gst)
				d_oc_tax += (oc(new_gst) * new_gst - oc(gst) * gst) / Decimal(100)
			if levy_pct:
				d_levy += (net - rate) * levy_pct
				if levy_taxed:
					d_levy_tax += (net * new_gst - rate * gst) * levy_pct / Decimal(100)

		snap = {}
		try:
			snap = json.loads(self.quote_snapshot) if self.get("quote_snapshot") else {}
		except (TypeError, ValueError):
			snap = {}
		voucher = self.booked_voucher_discount(snap)
		currency = property_currency(self.property)
		raw_before = _dec(self.amount_before_tax) + d_room + d_oc + d_levy
		snap_items = snap.get("line_items") if isinstance(snap.get("line_items"), list) else None
		if snap_items and not voucher:
			base_tax = sum((_dec(li.get("tax_amount")) for li in snap_items), Decimal(0))
		else:
			base_tax = _dec(self.tax_amount)
		raw_tax = base_tax + d_tax + d_oc_tax + d_levy_tax
		if raw_tax:
			after_dec = round_rate_amount(raw_before + raw_tax, 0, "Nearest", currency)
			tax_dec = after_dec - raw_before
			before_dec = raw_before
		else:
			before_dec = raw_before
			after_dec = raw_before
			tax_dec = Decimal(0)
		before, tax, after = float(before_dec), float(tax_dec), float(after_dec)
		base_total = sum(float(entry_rate(r["rate"], r["gst_rate"], inclusive, places))
		                 + flt(r["discount_amount"]) for r in rows)
		off_total = round(sum(flt(r["discount_amount"]) for r in rows), places)
		snap.update(self._rediscounted_snapshot(
			snap, rows, effective, voucher, off_total, round(base_total, places),
			places=places,
			room=float(d_room), room_tax=float(d_tax), over_cap=float(d_oc),
			over_cap_tax=float(d_oc_tax), levy=float(d_levy), levy_tax=float(d_levy_tax),
			before=before, tax=tax, after=after))
		return {"rows": rows, "amount_before_tax": before, "tax_amount": tax,
		        "amount_after_tax": after, "voucher_discount": voucher,
		        "discount_total": off_total, "snapshot": snap}

	def booked_voucher_discount(self, snap: dict | None = None) -> float:
		"""The voucher part of the booking's discount, as it was priced."""
		from kamra.folio import voucher_discount
		from kamra.pricing import night_rate_places
		if snap and "voucher_discount" in snap:
			return round(max(flt(snap.get("voucher_discount")), 0), night_rate_places(self.property))
		return voucher_discount(self)

	@staticmethod
	def _rediscounted_snapshot(snap: dict, rows: list, effective: dict, voucher: float,
	                           off_total: float, base_total: float, places: int = 2, **d) -> dict:
		"""The booking's quote snapshot after an in-place discount change: the
		nights, the room line and the totals follow it; every other line
		(meals, cleaning, pets) is the booked one."""
		nightly = {str(n.get("date")): dict(n) for n in snap.get("nightly") or []}
		out_nights = []
		for r in rows:
			kind, value, source = effective[r["stay_date"]]
			n = nightly.get(r["stay_date"], {"date": r["stay_date"]})
			n.update(rate=flt(r["rate"]), gst_rate=flt(r["gst_rate"]), base_rate=flt(r["base_rate"]),
			         discount_type=kind, discount_value=value, discount_source=source,
			         discount_amount=flt(r["discount_amount"]))
			out_nights.append(n)
		old_off = flt(snap.get("reservation_discount_total"))
		lines = [dict(li) for li in snap.get("line_items") or []]
		for li in lines:
			if li.get("component") == "accommodation":
				li["amount"] = round(flt(li.get("amount")) + d["room"], places)
				li["tax_amount"] = round(flt(li.get("tax_amount")) + d["room_tax"], places)
				li["base_amount"], li["reservation_discount"] = base_total, off_total
			elif li.get("component") == "over_capacity":
				li["amount"] = round(flt(li.get("amount")) + d["over_cap"], places)
				li["tax_amount"] = round(flt(li.get("tax_amount")) + d["over_cap_tax"], places)
			elif li.get("component") == "room_levy":
				li["amount"] = round(flt(li.get("amount")) + d["levy"], places)
				li["tax_amount"] = round(flt(li.get("tax_amount")) + d["levy_tax"], places)
		totals = dict(snap.get("totals") or {})
		totals.update(
			subtotal=round(flt(totals.get("subtotal")) + d["room"] + d["over_cap"] + d["levy"]
			               + off_total - old_off, places),
			discount=round(voucher + off_total, places), tax=d["tax"], total=d["after"])
		return {
			"nightly": out_nights, "line_items": lines, "totals": totals,
			"room_total": round(flt(snap.get("room_total")) + d["room"], places),
			"over_capacity_total": round(flt(snap.get("over_capacity_total")) + d["over_cap"], places),
			"room_levy": round(flt(snap.get("room_levy")) + d["levy"], places),
			"room_base_total": base_total, "reservation_discount_total": off_total,
			"voucher_discount": voucher, "discount": round(voucher + off_total, places),
			"amount_before_tax": d["before"], "tax_amount": d["tax"], "amount_after_tax": d["after"],
			# when the discount was last changed on the booked nights
			"rediscounted_at": str(now_datetime()),
		}

	def apply_rediscount(self, plan: dict, key: str | None):
		"""Put an in-place discount change on the booking: its nights, its
		totals and its snapshot (rediscount_locked_nights)."""
		import json
		from kamra.pricing import night_rate_places
		places = night_rate_places(self.property)
		self.set("night_rates", plan["rows"])
		self.amount_before_tax = plan["amount_before_tax"]
		self.tax_amount = plan["tax_amount"]
		self.amount_after_tax = plan["amount_after_tax"]
		self.discount_amount = round(plan["voucher_discount"] + plan["discount_total"], places)
		if hasattr(self, "quote_snapshot"):
			snap = dict(plan["snapshot"])
			snap["idempotency_key"] = getattr(self, "idempotency_key", None)
			self.quote_snapshot = json.dumps(snap, default=str)
		self.flags.rate_change_lines = []
		self.rate_lock_key = key
		self._apply_commission()

	def apply_quote(self, q: dict, key: str | None):
		"""Put a quote on the booking: its amounts, the immutable snapshot and
		the locked nightly rates, under the lock key of the inputs it priced."""
		from kamra.pricing import night_rate_places
		places = night_rate_places(self.property)
		self.amount_before_tax = q["amount_before_tax"]
		self.tax_amount = q["tax_amount"]
		self.amount_after_tax = q["amount_after_tax"]
		# Persist immutable quote snapshot (ADR-008).
		if hasattr(self, "quote_snapshot"):
			import json
			snap = dict(q)
			snap["idempotency_key"] = getattr(self, "idempotency_key", None)
			self.quote_snapshot = json.dumps(snap, default=str)
		# nights already charged keep what the folio holds for them (or
		# get their adjustment lines): the totals follow the same nights
		d_base, d_tax = self.set_night_rates(q)
		if d_base or d_tax:
			from decimal import Decimal

			from kamra.pricing import property_currency, round_rate_amount
			base_dec = Decimal(str(self.amount_before_tax or 0)) + Decimal(str(d_base))
			tax_dec = Decimal(str(self.tax_amount or 0)) + Decimal(str(d_tax))
			currency = property_currency(self.property)
			if tax_dec:
				total_dec = round_rate_amount(base_dec + tax_dec, 0, "Nearest", currency)
				tax_dec = total_dec - base_dec
			else:
				total_dec = base_dec
			self.amount_before_tax = float(base_dec)
			self.tax_amount = float(tax_dec)
			self.amount_after_tax = float(total_dec)
		# the booking's discount is its nights' (charged ones as the folio
		# holds them) plus the voucher - never the quote's, which may count a
		# discount a billed night never got
		voucher = q.get("voucher_discount")
		if voucher is None:
			voucher = flt(q.get("discount")) - flt(q.get("reservation_discount_total"))
		self.discount_amount = round(flt(voucher) + sum(
			flt(r.get("discount_amount")) for r in self.get("night_rates") or []), places)
		self.rate_lock_key = key
		self._apply_commission()

	def set_night_rates(self, q: dict) -> tuple[float, float]:
		"""Lock the quote's nights on the booking - the rates the folio bills.

		Nights already charged to the folio are held by the property's
		rate-change policy (hold_charged_nights); returns what that changes
		in the quote's totals (before tax, tax). Lines it decides to post
		wait in flags until on_update."""
		rows = self.quote_night_rows(q)
		hold = self.hold_charged_nights(
			rows, correct=bool(self.flags.get("correct_charged_nights")))
		self.flags.rate_change_lines = hold["lines"]
		self.set("night_rates", rows)
		return hold["base"], hold["tax"]

	def quote_night_rows(self, q: dict) -> list[dict]:
		"""The quote's nights as night-rate rows.

		A rate plan works on the stay total, so each night carries its share
		of the planned total (same spread as pricing.plan_stay_nights) - or,
		when the plan rounds, the night's own rounded price (planned_rate) -
		and the last night absorbs rounding so the nights add up to the
		quote's room total exactly - to the currency's decimals, never fewer
		than two (pricing.night_rate_places)."""
		from kamra.pricing import night_rate_places
		places = night_rate_places(self.property)
		nightly = q.get("nightly") or []
		plan_free = sum(float(n["rate"]) for n in nightly)
		room_total = float(q.get("room_total") or 0)
		factor = room_total / plan_free if plan_free else 1.0
		meal = 0.0
		if self.meal_plan:
			mp = frappe.db.get_value(
				"Meal Plan", self.meal_plan,
				["price_per_adult", "price_per_child"], as_dict=True) or {}
			meal = ((self.adults or 1) * float(mp.get("price_per_adult") or 0)
			        + (self.children or 0) * float(mp.get("price_per_child") or 0))
		from kamra.pricing import entry_rate, rates_include_tax
		inclusive = rates_include_tax(self.property)
		# what each night's cut is measured from: under an agreed rate that
		# overrides the rate code, the rate code's amount; else the night's
		# own base before the desk's discount
		codes = self.rate_code_amounts() if flt(self.get("agreed_rate")) > 0 else {}
		rows, running = [], 0.0
		for i, n in enumerate(nightly):
			rate = round(float(n["planned_rate"]) if "planned_rate" in n else float(n["rate"]) * factor, places)
			# a zero-priced room's spread nights carry planned_rate: they absorb too
			if i == len(nightly) - 1 and (plan_free or "planned_rate" in n):
				rate = round(room_total - running, places)
			running += rate
			# gross, discount and net side by side (the discounted quote's
			# nights are already at night_rate_places, so the discount is exact)
			off = round(float(n.get("discount_amount") or 0), places)
			own = n.get("night_discount") or {}
			code = codes.get(str(n["date"])) or flt(n.get("rate_code_amount")) or (
				float(entry_rate(rate, n.get("gst_rate"), inclusive, places)) + off)
			rows.append({
				"stay_date": n["date"],
				"rate": rate,
				"gst_rate": float(n.get("gst_rate") or 0),
				"meal_amount": meal,
				"demand_premium_pct": float(n.get("demand_premium_pct") or 0),
				# what the desk typed for the night (0: Kamra priced it)
				"hand_price": float(n.get("typed_price") or 0),
				"rate_code_amount": round(float(code), places),
				# the night's base before tax (on a tax-inclusive slab the
				# discount is on the gross, so the base is the quote's own)
				"base_rate": round(float(n.get("base_rate") or rate), places) if off else rate,
				"discount_amount": off,
				# the night's own Daily Details discount; blank follows the stay
				"discount_type": own.get("discount_type"),
				"discount_value": float(own.get("discount_value") or 0),
				"discount_reason": own.get("discount_reason"),
				"fixed_rate": cint(own.get("fixed_rate")),
			})
		return rows

	def hold_charged_nights(self, rows: list[dict], correct: bool = False) -> dict:
		"""Apply the property's rate-change policy to the nights already
		charged (kamra.settings charged_night_rate_change). Nobody changes
		the rate of a night on the folio in place:

		- Keep charged nights (default): the night keeps what the folio
		  holds; only nights still to come take the new rate.
		- Credit the difference: a lower new rate is credited per night
		  (one Rate Adjustment line, taxed like the night); a higher one is
		  kept, as with Keep.
		- Correct back-dated rates: as Keep, except inside the dedicated
		  correction (correct=True), which reverses each changed night and
		  posts it again at the new rate.

		Mutates `rows` so each charged night carries what the folio will
		hold for it; a night it keeps is history (OPERA's posted Daily
		Details): it keeps the base, discount and rate-code amount it was
		charged with. Returns {base, tax} - the change against the quote's
		totals - plus `lines` to post and `nights` for a preview."""
		out = {"base": 0.0, "tax": 0.0, "lines": [], "nights": [],
		       "policy": None}
		if self.is_new() or not rows:
			return out
		from kamra.folio import charged_nights
		charged = charged_nights(self.name)
		if not charged:
			return out
		# the nights as they are locked now, before this quote replaces them
		held = {str(getdate(r.stay_date)): r for r in self.get("night_rates") or []
		        if r.get("stay_date")}
		from kamra.settings import get_setting
		policy = get_setting(self.property, "charged_night_rate_change")
		out["policy"] = policy
		from kamra.pricing import entry_rate, night_rate_places, rates_include_tax
		inclusive = rates_include_tax(self.property)
		places = night_rate_places(self.property)
		tol = 10 ** -places
		prop = frappe.get_cached_doc("Property", self.property)
		levy_pct = flt(prop.get("room_levy_percent")) / 100
		levy_fixed = flt(prop.get("room_levy_per_night"))
		levy_taxed = cint(prop.get("room_levy_taxable")
		                  if prop.get("room_levy_taxable") is not None else 1)
		for row in rows:
			c = charged.get(str(row["stay_date"]))
			if not c:
				continue
			night = str(row["stay_date"])
			new, new_gst = flt(row["rate"]), flt(row["gst_rate"])
			base, gst = c["base"], c["gst_rate"]
			changed = abs(new - base) >= tol or (correct and abs(new_gst - gst) >= 0.01)
			action = "kept" if changed else "same"
			if changed and correct:
				out["lines"] += [
					{"night": night, "folio": c["folio"], "amount": -base,
					 "gst_rate": gst,
					 "description": f"Rate correction: night of {night} reversed at {base:,.2f}"},
					{"night": night, "folio": c["folio"], "amount": round(new, places),
					 "gst_rate": new_gst,
					 "description": f"Rate correction: night of {night} at {new:,.2f}"},
				]
				base, gst, action = new, new_gst, "corrected"
			elif changed and policy == "Credit the difference" and new < base:
				out["lines"].append(
					{"night": night, "folio": c["folio"],
					 "amount": round(new - base, places), "gst_rate": gst,
					 "description": f"Rate change: night of {night} {base:,.2f} to {new:,.2f}"})
				action = "credited"
				base = new
			# the folio's levy for the night stays as posted
			# ponytail: the quote taxes its levy at the stay's average rate;
			# a per-night levy rate here can differ by a rounding paisa
			levy_new = new * levy_pct + levy_fixed
			out["base"] += (base - new) + (c["levy_base"] - levy_new)
			out["tax"] += (base * gst - new * new_gst) / 100 + c["levy_tax"] - (
				levy_new * new_gst / 100 if levy_taxed else 0)
			if action != "same":
				out["nights"].append({"date": night, "charged": c["base"],
				                      "new": round(new, places), "action": action})
			# the night's base on the desk's basis, before the folio's figure
			# replaces the quote's
			entry = flt(entry_rate(new, new_gst, inclusive, places)) + flt(row.get("discount_amount"))
			row["rate"], row["gst_rate"] = round(base, places), gst
			was = held.get(night)
			if action in ("kept", "same") and was is not None and flt(was.get("base_rate")):
				# kept as charged: its base, discount and rate-code amount as
				# billed (a row locked before base_rate existed is worked out below)
				for f in CHARGED_NIGHT_HISTORY:
					if f in row:
						row[f] = flt(was.get(f))
			elif "base_rate" in row:
				# what the folio holds for the night, against the night's base
				off = round(max(entry - flt(entry_rate(row["rate"], gst, inclusive, places)), 0), places)
				row["discount_amount"] = off
				if not inclusive or not off:
					row["base_rate"] = round(row["rate"] + (off if not inclusive else 0), places)
		out["base"], out["tax"] = round(out["base"], places), round(out["tax"], places)
		return out

	def _apply_commission(self):
		if getattr(self, "travel_agent", None):
			pct = frappe.db.get_value(
				"Travel Agent", self.travel_agent, "commission_pct") or 0
			self.commission_amount = float(self.amount_before_tax or 0) * float(pct) / 100
		# ponytail: an OTA booking's commission is set once at booking
		# (channel_manager.set_ota_commission) and kept; a re-price does not
		# rescale it. Store the % on the stay if that ever matters.
		elif not self.get("channel"):
			self.commission_amount = 0

	def validate_room_belongs_to_type(self):
		if not self.room:
			return
		room_type = frappe.db.get_value("Room", self.room, "room_type")
		if room_type != self.room_type:
			# help the user by naming the rooms they CAN move to: rooms of the
			# reservation's own type that are free for these dates
			from kamra.api import _available_rooms_raw
			free = [
				r.room_number for r in _available_rooms_raw(
					self.property, self.room_type,
					self.check_in_date, self.check_out_date)
				if r.name != self.room
			]
			if free:
				hint = " " + _("Free {0} rooms for these dates: {1}.").format(
					self.room_type, ", ".join(free))
			else:
				hint = " " + _("No {0} rooms are free for these dates.").format(
					self.room_type)
			frappe.throw(
				_("Room {0} belongs to {1}, not {2}.").format(
					self.room, room_type, self.room_type
				) + hint
			)

	def validate_no_overlap(self):
		"""A physical room can hold only one live reservation per night.

		This check is the seed of Kamra's no-overbooking guarantee: it runs
		on every insert/update, regardless of whether a human or an AI agent
		created the booking.
		"""
		if not self.room or self.status in (
			"Cancelled", "No Show", "Checked Out", "Waitlist",
			"Inquiry", "Quoted", "Requested",
		):
			return
		# serialize concurrent bookings for the same room: the row lock
		# makes the second transaction wait, and the locking read below
		# then sees the first one's committed reservation — check-then-
		# insert alone would let two simultaneous requests both pass
		frappe.db.get_value("Room", self.room, "name", for_update=True)
		# a day-use stay occupies [check_in, check_in + 1 day) — GREATEST
		# normalises both sides of the comparison
		conflict = frappe.db.sql(
			"""
			SELECT name FROM `tabReservation`
			WHERE room = %(room)s
			  AND name != %(name)s
			  AND status IN ('Confirmed', 'Checked In', 'Held', 'Pending Payment')
			  AND check_in_date < GREATEST(%(check_out)s,
			                               DATE_ADD(%(check_in)s, INTERVAL 1 DAY))
			  AND GREATEST(check_out_date,
			               DATE_ADD(check_in_date, INTERVAL 1 DAY)) > %(check_in)s
			LIMIT 1
			FOR UPDATE
			""",
			{
				"room": self.room,
				"name": self.name or "new",
				"check_in": self.check_in_date,
				"check_out": self.check_out_date,
			},
		)
		if conflict:
			frappe.throw(
				_("Room {0} is already booked ({1}) for these dates.").format(
					self.room, conflict[0][0]
				),
				title=_("Double booking blocked"),
				exc=InventoryRefused,
			)

	def validate_itinerary_leg(self):
		"""A leg of an itinerary can't overlap another live leg of the same
		trip - the guest can't sleep in two hotels on one night. Runs on
		every save, so amend_stay on one leg is held to it too.

		The link itself is kamra.crs's alone (it sets it with db.set_value
		when it books a leg): a save that adds, moves or drops it, from
		/api/resource say, is refused."""
		previous = None if self.is_new() else self.get_doc_before_save()
		if (self.get("itinerary") or None) != ((previous and previous.get("itinerary")) or None):
			frappe.throw(_("A stay joins or leaves a trip only through the "
			               "trip itself."), frappe.PermissionError)
		from kamra.reservation_state import holds_inventory
		if self.get("itinerary") and holds_inventory(self.status):
			from kamra.crs import assert_leg_fits
			assert_leg_fits(self.itinerary, self.check_in_date,
			                self.check_out_date, self.name, self.property)

	def validate_room_vacant(self):
		"""One guest in a room at a time. The night-based overlap guard lets a
		new stay start on another stay's departure date - right for bookings,
		since the departing guest will be gone by then - but a guest still
		Checked In has not gone. Checking in (or moving in-house) to a room
		whose occupant has not checked out yet is refused until they do."""
		if self.status != "Checked In" or not self.room:
			return
		before = self.get_doc_before_save()
		if before and before.status == "Checked In" and before.room == self.room:
			return  # already in this room; an unrelated edit
		# the occupant holds the room until they check out: their stay runs
		# to their departure date or, while they are still in, to tomorrow
		occupant = frappe.db.sql(
			"""
			SELECT name, guest_name FROM `tabReservation`
			WHERE room = %(room)s AND name != %(name)s
			  AND status = 'Checked In'
			  AND check_in_date < GREATEST(%(check_out)s,
			                               DATE_ADD(%(check_in)s, INTERVAL 1 DAY))
			  AND GREATEST(check_out_date,
			               DATE_ADD(check_in_date, INTERVAL 1 DAY),
			               DATE_ADD(%(today)s, INTERVAL 1 DAY)) > %(check_in)s
			LIMIT 1
			FOR UPDATE
			""",
			{"room": self.room, "name": self.name or "new",
			 "check_in": self.check_in_date, "check_out": self.check_out_date,
			 "today": today()},
			as_dict=True,
		)
		if occupant:
			room_no = frappe.db.get_value("Room", self.room, "room_number") \
				or self.room
			frappe.throw(
				_("Room {0} is still occupied by {1} ({2}). Check them out "
				  "first, or choose another room.").format(
					room_no, occupant[0].guest_name or occupant[0].name,
					occupant[0].name),
				title=_("Room still occupied"),
				exc=InventoryRefused,
			)

	def validate_villa_lockout(self):
		if self.status in (
			"Cancelled", "No Show", "Checked Out", "Waitlist",
			"Inquiry", "Quoted", "Requested",
		) or not self.room_type:
			return

		conflict = villa_lockout_conflict(
			self.property, self.room_type, self.check_in_date,
			self.check_out_date, exclude=self.name, for_update=True)
		if not conflict:
			return
		if is_entire_place(self.room_type):
			frappe.throw(
				_("This property cannot be booked as an Entire Villa for these dates because an individual room is already booked at {0} under Reservation {1}.").format(
					self.property, conflict
				),
				title=_("Double Booking Blocked"),
				exc=InventoryRefused,
			)
		frappe.throw(
			_("This room cannot be booked for these dates because the Entire Villa is already booked at {0} under Reservation {1}.").format(
				self.property, conflict
			),
			title=_("Double Booking Blocked"),
			exc=InventoryRefused,
		)

	def _check_out_only_through_check_out(self):
		"""A stay becomes Checked Out only through kamra.api.check_out, which
		refuses while the bill is unpaid or laundry / a restaurant bill is
		still open. A status edit (Desk form, REST) would skip all of it."""
		previous = self.get_doc_before_save()
		if (previous and previous.status != "Checked Out"
				and self.status == "Checked Out" and not self.flags.kamra_check_out):
			frappe.throw(_("Use Check out to check a guest out: it makes sure every "
			               "bill is cleared first."), title=_("Check out"))

	def on_update(self):
		lines = self.flags.pop("rate_change_lines", None)
		if lines:
			from kamra.folio import post_rate_lines
			post_rate_lines(self, lines)
		previous = self.get_doc_before_save()
		old_status = previous.status if previous else None
		if self.status == old_status:
			return
		if self.status == "Checked In" and old_status == "Checked Out":
			self.handle_reinstate()
		elif self.status == "Checked In":
			self.handle_check_in()
		elif self.status == "Checked Out":
			self.handle_check_out()
		elif self.status == "Confirmed" and old_status in (
			"Held", "Pending Payment", "Requested", "Waitlist",
		):
			self._notify_confirmed()

	def _notify_confirmed(self):
		from kamra import whatsapp
		if whatsapp.has_connection(self.property):
			frappe.enqueue(
				"kamra.whatsapp.notify_booking_confirmed",
				reservation=self.name, queue="short",
				enqueue_after_commit=True,
			)
	def _occupied_rooms(self, checking_in=True) -> list[str]:
		"""The rooms this stay holds: its assigned room, or for a whole-villa
		booking whose villa type has no rooms of its own, the member rooms
		(Out of Order ones excluded)."""
		if self.room:
			return [self.room]
		if uses_member_rooms(self.property, self.room_type):
			return [r.name for r in entire_place_rooms(
				self.property, self.room_type,
				include_out_of_order=not checking_in)]
		if checking_in:
			frappe.throw(_("Assign a room before check-in."))
		return []

	def handle_check_in(self):
		rooms = self._occupied_rooms()
		at = now_datetime()
		self.db_set({"actual_check_in": at, "checked_in_at": at,
		             "checked_in_by": frappe.session.user},
		            update_modified=False)
		# everyone on the register so far arrived with the guest; someone
		# added later gets their own time (api.update_occupants)
		for occ in self.get("occupants") or []:
			if not occ.get("arrived_at"):
				occ.db_set("arrived_at", at, update_modified=False)
		for room in rooms:
			frappe.db.set_value("Room", room, "occupancy_status", "Occupied")
		from kamra.folio import open_folio

		open_folio(self)
		# the advance stops being a deposit the moment the guest is in house:
		# it pays the stay, so it moves to the Guest ledger
		from kamra.ledger import transfer_advances_to_guest
		transfer_advances_to_guest(self.name)
		self.log_action("check_in", minutes_saved=10)

	def handle_reinstate(self):
		"""Back in house after a checkout undone the same day: the rooms are
		occupied again and the checkout clean is dropped. The folios stay as they are (kamra.api.reinstate_checkout
		reopened them)."""
		self.db_set({"actual_check_out": None, "check_out_business_date": None},
		            update_modified=False)
		# occupied again; it stays Dirty (the guest used it) for the next service
		for room in self._occupied_rooms(checking_in=False):
			frappe.db.set_value("Room", room, "occupancy_status", "Occupied")
		for task in frappe.get_all("Housekeeping Task", pluck="name", filters={
				"reservation": self.name, "task_type": "Checkout Clean",
				"status": ("in", ["Pending", "In Progress"])}):
			frappe.delete_doc("Housekeeping Task", task, ignore_permissions=True)

	def handle_check_out(self):
		from kamra.business_date import get_business_date
		self.db_set({"actual_check_out": now_datetime(),
		             "check_out_business_date": get_business_date(self.property)},
		            update_modified=False)
		from kamra.folio import post_remaining_nights
		from kamra.ledger import transfer_advances_to_guest

		post_remaining_nights(self)
		# a no-op unless an advance reached the stay some other way
		transfer_advances_to_guest(self.name)
		for room in self._occupied_rooms(checking_in=False):
			hk, occ = frappe.db.get_value(
				"Room", room, ["housekeeping_status", "occupancy_status"]) or (None, None)
			if hk == "Out of Order":
				# out of service: never flipped to Dirty, no checkout clean.
				# Only one that went out of order mid-stay is freed.
				if occ == "Occupied":
					frappe.db.set_value("Room", room, "occupancy_status", "Vacant")
				continue
			frappe.db.set_value(
				"Room",
				room,
				{"occupancy_status": "Vacant", "housekeeping_status": "Dirty"},
			)
			task = frappe.get_doc(
				{
					"doctype": "Housekeeping Task",
					"property": self.property,
					"room": room,
					"task_type": "Checkout Clean",
					"priority": "High",
					"status": "Pending",
					"reservation": self.name,
				}
			)
			task.insert(ignore_permissions=True)
		self.log_action("check_out", minutes_saved=12)

	def log_action(self, action_type, minutes_saved=0):
		from kamra.savings import log_action

		log_action(
			action_type=action_type,
			reference_doctype="Reservation",
			reference_name=self.name,
			property=self.property,
			minutes_saved=minutes_saved if self.source == "AI Agent" else 0,
			rationale=f"Reservation {self.name} moved to {self.status}",
		)
