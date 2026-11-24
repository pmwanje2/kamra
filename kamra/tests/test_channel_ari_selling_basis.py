"""Channel ARI on the property's selling basis, at the currency's minor unit.

What a channel is offered for a night must be what the desk sells it for:
gross (the room tax put back on) where the property's rates include tax
(Property.rates_include_tax), net otherwise - the basis the OTA extranet's
own tax setting must match. A booking that comes back at that price then
bills the nights a desk booking would, so the round trip is pinned end to
end: push -> the OTA charges the guest under a matching extranet setting
(the pushed price where it includes tax, the pushed price plus the room
tax where it does not) -> the booking webhook -> the locked night rates
equal a desk booking's of the same stay.

How a channel reports what the guest paid is its own setting, not the
property's: Channex lists a room's taxes in rooms[].taxes[] and marks each
is_inclusive - true when the day prices already hold it, false when it is
added on top (Expedia "Total Amount Excluding Tax", Agoda "Exclusive";
docs.channex.io, bookings collection). Both are pinned on both bases. Only
the room tax (VAT/GST) is what the desk charges; an OTA's fees and city
taxes are not.

Rates go out with the currency's ISO 4217 decimals: none for UGX, three
for KWD, two for USD. A locked night rate keeps the currency's decimals and
never fewer than two: the net share of a tax-inclusive UGX price is not a
whole shilling.

A property's own selling basis (whether its rates include tax, its
currency, country and room tax rates) moves what every channel is offered,
so an edit to it queues the change at once.

No HTTP: FakeChannex stands in for channex._send. Dates are relative to
today."""

import json
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from kamra import channel_ari
from kamra import channel_manager as cm
from kamra.channels import aiosell, channex
from kamra.tests.test_channex_outbox import FakeChannex, day

NIGHT = 3  # the stay priced, pushed and booked: today + 3 .. today + 4


class _SellingBasisFixture:
	"""One property per test: a room type of 3 rooms at `price` (2 adults),
	the room tax at `tax` %, one connection and one mapping of the room's
	base price (plus one per rate plan given)."""

	def setUp(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		frappe.local.lang = "en"

	def hotel(self, inclusive: int, currency: str = "UGX", country: str = "Uganda",
	          price=150000, tax=18, provider: str = "Channex", plans=(), slabs=None) -> frappe._dict:
		"""`slabs` (threshold, low %, high %): India's GST slab instead of
		one rate."""
		tag = frappe.generate_hash(length=6).upper()
		rates = ({"gst_mode": "Slab", "gst_slab_threshold": slabs[0], "gst_rate_low": slabs[1],
		          "gst_rate_high": slabs[2]} if slabs else {"gst_rate_low": tax, "gst_rate_high": tax})
		prop = frappe.get_doc({"doctype": "Property", "property_name": "TEST BASIS " + tag,
			"country": country, "currency": currency, "rates_include_tax": inclusive,
			**rates}).insert(ignore_permissions=True).name
		rt = frappe.get_doc({"doctype": "Room Type", "property": prop, "room_type_code": "STD" + tag,
			"room_type_name": "Standard", "base_price": price,
			"adults_capacity": 2}).insert(ignore_permissions=True).name
		for i in range(3):
			frappe.get_doc({"doctype": "Room", "property": prop, "room_type": rt,
				"room_number": f"{tag}-{i + 1}"}).insert(ignore_permissions=True)
		conn = frappe.get_doc({"doctype": "Channel Manager Connection", "property": prop,
			"provider": provider, "active": 1, "external_property_id": "cx-basis",
			"webhook_secret": "basis-hook", "ari_batch_seconds": 0}).insert(ignore_permissions=True)
		mappings = {"rp-std": None}
		for code, (kind, value) in dict(plans).items():
			mappings[f"rp-std-{code}"] = frappe.get_doc({"doctype": "Rate Plan", "property": prop,
				"rate_plan_name": f"Plan {code}", "code": f"{code}{tag}", "modifier_type": kind,
				"modifier_value": value}).insert(ignore_permissions=True).name
		for ext_rate, plan in mappings.items():
			frappe.get_doc({"doctype": "Channel Room Mapping", "connection": conn.name,
				"room_type": rt, "external_room_id": "rt-std", "external_rate_id": ext_rate,
				"external_currency": currency, "rate_plan": plan}).insert(ignore_permissions=True)
		frappe.db.delete("Channel ARI Outbox", {"connection": conn.name})
		for kind in channel_ari.KINDS:
			frappe.cache.delete_value(f"channex_ari_pause::{conn.name}::{kind}")
			frappe.cache.delete_value(f"channex_ari_calls::{conn.name}::{kind}")
		return frappe._dict(property=prop, room_type=rt, conn=conn, currency=currency,
		                    tax=Decimal(str(tax)), inclusive=inclusive, plans=mappings)

	# -- outbound -------------------------------------------------------

	def flush(self, conn, fake: FakeChannex) -> dict:
		"""One flush_outbox pass as the worker runs it; its commits are
		savepoints, a bare rollback goes back to the last one."""
		state = {"sp": "basis_" + frappe.generate_hash(length=8)}
		frappe.db.savepoint(state["sp"])
		rollback = frappe.db.rollback

		def worker_commit(*args, **kwargs):
			state["sp"] = "basis_" + frappe.generate_hash(length=8)
			frappe.db.savepoint(state["sp"])

		def worker_rollback(*args, **kwargs):
			if args or kwargs:
				return rollback(*args, **kwargs)
			return rollback(save_point=state["sp"])

		with patch.object(frappe.db, "commit", side_effect=worker_commit), \
		     patch.object(frappe.db, "rollback", side_effect=worker_rollback), \
		     patch.object(channex, "_send", side_effect=fake):
			return channel_ari.flush_outbox(conn.name)

	def channex_push(self, h, n: int = NIGHT, ext_rate: str = "rp-std", fake=None) -> str:
		"""The rate string the outbox posts to Channex for day n on
		`ext_rate`, from a change of that one day sent whole."""
		fake = fake or FakeChannex()
		channel_ari.enqueue_ari_change(h.property, day(n), day(n), kinds="restrictions",
		                               reason="selling basis test", full=True)
		self.flush(h.conn, fake)
		sent = [v for call in fake.posts("restrictions") for v in call
		        if v["rate_plan_id"] == ext_rate and v.get("date") == day(n)]
		self.assertEqual(len(sent), 1, fake.posts("restrictions"))
		self.assertNotIn("stop_sell", [k for k, v in sent[0].items() if v is True])
		return sent[0]["rate"]

	def aiosell_push(self, h, n: int = NIGHT):
		"""The rate the legacy whole-snapshot push puts in AioSell's
		/update-rates body for day n."""
		snapshot = cm.ari_snapshot(h.property, h.conn.name, days=1, start=day(n))
		_inv, rates = aiosell.build_push_bodies("demo", snapshot)
		self.assertEqual(len(rates), 1, rates)
		return rates[0]["rates"][0]["rate"]

	def charged(self, h, pushed, tax=None) -> Decimal:
		"""What the guest pays the OTA for a night offered at `pushed`, its
		extranet's tax setting matching the property: the price itself
		where it includes tax, the price plus the room tax (`tax` %, default
		the property's) where not. That is the money; how the channel then
		reports it is channex_booking's `report`."""
		price = Decimal(str(pushed))
		tax = h.tax if tax is None else Decimal(str(tax))
		return price if h.inclusive else price * (1 + tax / 100)

	# -- the desk and the inbound booking ------------------------------------

	def guest(self) -> str:
		return frappe.get_doc({"doctype": "Guest", "first_name": "Basis",
			"last_name": frappe.generate_hash(length=6)}).insert(ignore_permissions=True).name

	def desk_booking(self, h, n: int = NIGHT, rate_plan=None, nights: int = 1):
		return frappe.get_doc({"doctype": "Reservation", "property": h.property,
			"room_type": h.room_type, "guest": self.guest(), "status": "Confirmed",
			"check_in_date": day(n), "check_out_date": day(n + nights), "adults": 2,
			"rate_plan": rate_plan}).insert(ignore_permissions=True)

	def season(self, h, n: int, price) -> None:
		"""Night n sells for `price` (an Absolute season on the room type)."""
		frappe.get_doc({"doctype": "Season", "property": h.property, "season_name": f"Night {n}",
			"room_type": h.room_type, "start_date": day(n), "end_date": day(n),
			"adjustment_type": "Absolute", "adjustment_value": price,
			"priority": 10}).insert(ignore_permissions=True)

	def deliver(self, h, payload):
		sp = "basis_hook_" + frappe.generate_hash(length=8)
		frappe.db.savepoint(sp)
		rollback = frappe.db.rollback

		def worker_rollback(*args, **kwargs):
			return rollback(*args, **kwargs) if args or kwargs else rollback(save_point=sp)

		with patch.object(frappe.db, "commit", return_value=None), \
		     patch.object(frappe.db, "rollback", side_effect=worker_rollback):
			cm.process_webhook_events(h.conn.name, payload)

	def received(self, h, ref):
		name = frappe.db.get_value("Reservation", {"property": h.property, "ota_ref": ref})
		self.assertTrue(name, f"the OTA booking {ref} did not land")
		return frappe.get_doc("Reservation", name)

	def channex_payload(self, h, charged, n: int = NIGHT, report: str = "inclusive",
	                    taxes=None, tax_type: str = "tax", extra_taxes=()) -> dict:
		"""The booking webhook Channex sends for a stay from day n the guest
		paid `charged` for (one night's gross price, or a list of them, one a
		night), the room tax at `taxes` % a night (default the property's).

		`report` is how the channel reports that room tax
		(docs.channex.io, bookings collection, rooms[].taxes[].is_inclusive):
		- "inclusive": the day prices hold the tax; it is listed with
		  is_inclusive true (Booking.com's VAT, Expedia "Total Amount", Agoda
		  "Inclusive");
		- "exclusive": the day prices are before tax and the tax is listed on
		  top, is_inclusive false (Expedia "Total Amount Excluding Tax", Agoda
		  "Exclusive"). The booking amount is then before tax too.
		`tax_type` is the room tax's type as the channel names it; extra_taxes
		go into taxes[] as given (an OTA's fees and city taxes)."""
		gross = [Decimal(str(c)) for c in (charged if isinstance(charged, list | tuple) else [charged])]
		pcts = [Decimal(str(t)) for t in (taxes or [h.tax] * len(gross))]
		quantum = min(Decimal("0.01"), channel_ari.rate_quantum(h.property))
		money = lambda v: str(Decimal(v).quantize(quantum))  # noqa: E731
		inclusive = report == "inclusive"
		days = {day(n + i): money(g if inclusive else g / (1 + t / 100))
		        for i, (g, t) in enumerate(zip(gross, pcts, strict=True))}
		room_amount = sum(Decimal(v) for v in days.values())
		tax = sum(gross) - (room_amount if not inclusive else sum(
			g / (1 + t / 100) for g, t in zip(gross, pcts, strict=True)))
		room_taxes = [{"is_inclusive": inclusive, "name": "VAT", "total_price": money(tax),
		               "type": tax_type}] if tax else []
		ref = "BDC-" + frappe.generate_hash(length=8)
		return {"event": "booking", "payload": {
			"status": "new", "ota_reservation_code": ref, "ota_name": "Booking.com",
			"arrival_date": day(n), "departure_date": day(n + len(gross)), "currency": h.currency,
			"amount": money(room_amount), "customer": {"name": "Ota", "surname": "Guest"},
			"rooms": [{"room_type_id": "rt-std", "checkin_date": day(n),
			           "checkout_date": day(n + len(gross)), "occupancy": {"adults": 2, "children": 0},
			           "amount": money(room_amount), "days": days,
			           "taxes": room_taxes + list(extra_taxes)}]}}

	def channex_booking(self, h, charged, n: int = NIGHT, **report):
		payload = self.channex_payload(h, charged, n, **report)
		self.deliver(h, payload)
		return self.received(h, payload["payload"]["ota_reservation_code"])

	def aiosell_booking(self, h, pushed, charged, n: int = NIGHT, before_tax=None):
		"""`pushed` and `charged`: one night's price, or a list, one a night.
		before_tax: the booking's amountBeforeTax, as AioSell's own sample
		booking carries it (kamra/tests/test_aiosell.py), when given."""
		as_list = lambda v: list(v) if isinstance(v, list | tuple) else [v]  # noqa: E731
		pushed, charged = as_list(pushed), as_list(charged)
		amount = {"amountAfterTax": float(sum(Decimal(str(c)) for c in charged)), "currency": h.currency}
		if before_tax is not None:
			amount.update(amountBeforeTax=float(before_tax),
			              tax=amount["amountAfterTax"] - float(before_tax))
		ref = "AIO-" + frappe.generate_hash(length=8)
		self.deliver(h, {"action": "book", "hotelCode": "demo", "channel": "Booking.com",
			"bookingId": ref, "checkin": day(n), "checkout": day(n + len(pushed)),
			"guest": {"firstName": "Ota", "lastName": "Guest"}, "amount": amount,
			"rooms": [{"roomCode": "rt-std", "occupancy": {"adults": 2, "children": 0},
			           "prices": [{"date": day(n + i), "sellRate": float(p)}
			                      for i, p in enumerate(pushed)]}]})
		return self.received(h, ref)

	def assertBillsLikeTheDesk(self, ota, desk):
		"""The folio bills an OTA booking's night exactly as the desk's: the
		same locked rate before tax, at the same tax rate, the same total."""
		nights = lambda doc: [(str(r.stay_date), flt(r.rate, 2), flt(r.gst_rate, 2))  # noqa: E731
		                      for r in sorted(doc.night_rates, key=lambda r: str(r.stay_date))]
		self.assertEqual(nights(ota), nights(desk))
		self.assertAlmostEqual(flt(ota.amount_after_tax), flt(desk.amount_after_tax), places=2)


class ChannexSellingBasisTestCase(_SellingBasisFixture, IntegrationTestCase):
	"""The ARI outbox (Channex): what is offered, and the round trip."""

	def test_a_tax_inclusive_property_offers_the_gross_price(self):
		"""Rates include 18% tax and the room sells for 150,000 at the desk:
		the channel is offered 150,000, not the 127,118.64 left once the tax
		is backed out."""
		h = self.hotel(inclusive=1)
		self.assertEqual(Decimal(self.channex_push(h)), Decimal("150000"))

	def test_a_tax_exclusive_property_offers_the_net_price(self):
		"""Rates exclude tax: the channel is offered the price before tax
		(150,000), never with the tax already on it (177,000) - the OTA adds
		it, as its extranet is set to."""
		h = self.hotel(inclusive=0)
		self.assertEqual(Decimal(self.channex_push(h)), Decimal("150000"))

	def test_a_rate_plan_price_is_offered_gross_too(self):
		"""A +10% plan on a tax-inclusive property: the desk sells it at
		165,000 gross, and that is what the channel is offered."""
		h = self.hotel(inclusive=1, plans={"bb": ("Percent", 10)})
		self.assertEqual(Decimal(self.channex_push(h, ext_rate="rp-std-bb")), Decimal("165000"))

	def test_round_trip_on_a_tax_inclusive_property_bills_like_the_desk(self):
		"""The guest pays 150,000; Channex reports the 22,881.36 of VAT
		inside the day price (is_inclusive: true)."""
		h = self.hotel(inclusive=1)
		pushed = self.channex_push(h)
		desk = self.desk_booking(h)
		ota = self.channex_booking(h, self.charged(h, pushed), report="inclusive")
		self.assertBillsLikeTheDesk(ota, desk)

	def test_round_trip_on_a_tax_inclusive_property_reported_before_tax(self):
		"""The guest pays 150,000, but the channel records bookings before
		tax (Expedia "Total Amount Excluding Tax"): the day price is
		127,118.64 and the 22,881.36 of VAT is listed on top
		(is_inclusive: false). The folio still bills 150,000."""
		h = self.hotel(inclusive=1)
		pushed = self.channex_push(h)
		desk = self.desk_booking(h)
		ota = self.channex_booking(h, self.charged(h, pushed), report="exclusive")
		self.assertBillsLikeTheDesk(ota, desk)

	def test_round_trip_on_a_tax_exclusive_property_bills_like_the_desk(self):
		"""Rates exclude 18% VAT: 150,000 is offered, the guest pays
		177,000, and Channex reports the day at 150,000 with 27,000 of VAT
		on top (is_inclusive: false). The folio bills 150,000 + 18%, not
		127,118.64 + 18% (the VAT dropped, then backed out of the net)."""
		h = self.hotel(inclusive=0)
		pushed = self.channex_push(h)
		desk = self.desk_booking(h)
		ota = self.channex_booking(h, self.charged(h, pushed), report="exclusive")
		self.assertBillsLikeTheDesk(ota, desk)

	def test_round_trip_on_a_tax_exclusive_property_reported_with_tax(self):
		"""The same stay, the channel reporting the 177,000 the guest paid
		in the day price and the VAT as inside it (is_inclusive: true)."""
		h = self.hotel(inclusive=0)
		pushed = self.channex_push(h)
		desk = self.desk_booking(h)
		ota = self.channex_booking(h, self.charged(h, pushed), report="inclusive")
		self.assertBillsLikeTheDesk(ota, desk)

	def test_round_trip_reads_an_ota_named_vat_as_the_room_tax(self):
		"""Channex relays an OTA's own tax names (Booking.com: "Value Added
		Tax (VAT)") as the type: that is the room tax too."""
		h = self.hotel(inclusive=0)
		pushed = self.channex_push(h)
		desk = self.desk_booking(h)
		ota = self.channex_booking(h, self.charged(h, pushed), report="exclusive",
		                           tax_type="Value Added Tax (VAT)")
		self.assertBillsLikeTheDesk(ota, desk)

	def test_round_trip_leaves_out_the_otas_fees_and_city_taxes(self):
		"""Fees and city taxes the OTA lists on top are not the room tax the
		desk charges: only the VAT is added. A withheld tax (collected by the
		OTA, collected_taxes) is not added either."""
		h = self.hotel(inclusive=0)
		pushed = self.channex_push(h)
		desk = self.desk_booking(h)
		ota = self.channex_booking(h, self.charged(h, pushed), report="exclusive", extra_taxes=[
			{"is_inclusive": False, "name": "Cleaning fee", "total_price": "20000.00", "type": "fee"},
			{"is_inclusive": False, "name": "City tax", "total_price": "5000.00", "type": "city_tax"},
			{"is_inclusive": False, "name": "CITY_TAX (7.20%)", "total_price": "10800.00",
			 "type": "City Tax"},
			{"is_inclusive": False, "is_withheld": True, "name": "VAT (Withheld Tax)",
			 "total_price": "27000.00", "type": "tax"}])
		self.assertBillsLikeTheDesk(ota, desk)

	def test_round_trip_of_a_rate_plan_on_a_tax_inclusive_property(self):
		h = self.hotel(inclusive=1, plans={"bb": ("Percent", 10)})
		pushed = self.channex_push(h, ext_rate="rp-std-bb")
		desk = self.desk_booking(h, rate_plan=h.plans["rp-std-bb"])
		ota = self.channex_booking(h, self.charged(h, pushed))
		self.assertBillsLikeTheDesk(ota, desk)


class LegacySnapshotSellingBasisTestCase(_SellingBasisFixture, IntegrationTestCase):
	"""The hourly whole-snapshot push (AioSell, STAAH): the same basis."""

	def test_a_tax_inclusive_property_offers_the_gross_price(self):
		h = self.hotel(inclusive=1, provider="AioSell")
		self.assertEqual(Decimal(str(self.aiosell_push(h))), Decimal("150000"))

	def test_a_tax_exclusive_property_offers_the_net_price(self):
		h = self.hotel(inclusive=0, provider="AioSell")
		self.assertEqual(Decimal(str(self.aiosell_push(h))), Decimal("150000"))

	def test_round_trip_on_a_tax_inclusive_property_bills_like_the_desk(self):
		h = self.hotel(inclusive=1, provider="AioSell")
		pushed = self.aiosell_push(h)
		desk = self.desk_booking(h)
		ota = self.aiosell_booking(h, pushed, self.charged(h, pushed))
		self.assertBillsLikeTheDesk(ota, desk)

	def test_round_trip_on_a_tax_exclusive_property_bills_like_the_desk(self):
		h = self.hotel(inclusive=0, provider="AioSell")
		pushed = self.aiosell_push(h)
		desk = self.desk_booking(h)
		ota = self.aiosell_booking(h, pushed, self.charged(h, pushed))
		self.assertBillsLikeTheDesk(ota, desk)


class ChannexMinorUnitTestCase(_SellingBasisFixture, IntegrationTestCase):
	"""Rates as decimal strings with the currency's ISO 4217 decimals."""

	def conn_of(self, currency: str, country: str):
		prop = frappe.get_doc({"doctype": "Property", "property_name": "TEST MINOR "
			+ frappe.generate_hash(length=6), "country": country,
			"currency": currency}).insert(ignore_permissions=True).name
		conn = frappe._dict(name="CMC-MINOR", provider="Channex", property=prop,
		                    endpoint="http://channex.test/api/v1", external_property_id="prop")
		conn.get_password = lambda *a, **k: "key-1"
		return conn

	def adapter_rate(self, conn, rate: Decimal) -> str:
		fake = FakeChannex()
		with patch.object(channex, "_send", side_effect=fake):
			channex.push_ari_changes(conn, "restrictions", [
				{"external_id": "rp1", "date": "2030-01-01", "values": {"rate": rate}}])
		return fake.posts("restrictions")[0][0]["rate"]

	def test_the_adapter_sends_ugx_without_decimals(self):
		conn = self.conn_of("UGX", "Uganda")
		self.assertEqual(self.adapter_rate(conn, Decimal("150000")), "150000")
		self.assertEqual(self.adapter_rate(conn, Decimal("149999.5")), "150000")

	def test_the_adapter_sends_kwd_with_three_decimals(self):
		conn = self.conn_of("KWD", "Kuwait")
		self.assertEqual(self.adapter_rate(conn, Decimal("12.345")), "12.345")
		self.assertEqual(self.adapter_rate(conn, Decimal("12")), "12.000")

	def test_the_adapter_keeps_two_decimals_for_usd(self):
		conn = self.conn_of("USD", "Ecuador")
		self.assertEqual(self.adapter_rate(conn, Decimal("241")), "241.00")
		self.assertEqual(self.adapter_rate(conn, Decimal("312.666")), "312.67")

	def test_the_outbox_posts_a_ugx_price_without_decimals(self):
		h = self.hotel(inclusive=0)
		self.assertEqual(self.channex_push(h), "150000")

	def test_the_outbox_posts_a_kwd_price_with_three_decimals(self):
		h = self.hotel(inclusive=0, currency="KWD", country="Kuwait", price=12.345, tax=0)
		self.assertEqual(self.channex_push(h), "12.345")

	def test_a_change_in_the_third_kwd_decimal_goes_out(self):
		"""12.345 went out and was accepted; the price moves to 12.349. The
		change is sent (not lost to a 2-decimal comparison) and remembered
		as sent."""
		h = self.hotel(inclusive=0, currency="KWD", country="Kuwait", price=12.345, tax=0)
		self.assertEqual(self.channex_push(h), "12.345")
		frappe.get_doc({"doctype": "Season", "property": h.property, "season_name": "Third decimal",
			"room_type": h.room_type, "start_date": day(NIGHT), "end_date": day(NIGHT),
			"adjustment_type": "Absolute", "adjustment_value": 12.349,
			"priority": 10}).insert(ignore_permissions=True)
		fake = FakeChannex()
		self.flush(h.conn, fake)
		self.assertEqual([(v["rate_plan_id"], v.get("date"), v["rate"])
		                  for call in fake.posts("restrictions") for v in call],
		                 [("rp-std", day(NIGHT), "12.349")])
		state = json.loads(frappe.db.get_value("Channel ARI State",
			f"{h.conn.name}|restrictions|rp-std", "sent_values") or "{}")
		self.assertEqual(Decimal(state[day(NIGHT)]["rate"]), Decimal("12.349"))


class MultiNightSellingBasisTestCase(_SellingBasisFixture, IntegrationTestCase):
	"""Two nights at two prices: each night bills what the desk locks for
	it, the rounding left on the last night as the desk leaves it, and each
	night's room tax at its own slab."""

	def test_two_nights_across_a_season_on_channex(self):
		"""18% inclusive, 150,000 then 180,000: the desk locks 127,118.64
		and 152,542.38 (the last night takes the rounding so the nights add
		up to the stay); the OTA booking locks the same, not 152,542.37."""
		h = self.hotel(inclusive=1)
		pushed = [self.channex_push(h)]
		self.season(h, NIGHT + 1, 180000)
		pushed.append(self.channex_push(h, NIGHT + 1))
		desk = self.desk_booking(h, nights=2)
		ota = self.channex_booking(h, [self.charged(h, p) for p in pushed])
		self.assertBillsLikeTheDesk(ota, desk)

	def test_two_nights_across_a_season_on_aiosell(self):
		h = self.hotel(inclusive=1, provider="AioSell")
		pushed = [self.aiosell_push(h)]
		self.season(h, NIGHT + 1, 180000)
		pushed.append(self.aiosell_push(h, NIGHT + 1))
		desk = self.desk_booking(h, nights=2)
		ota = self.aiosell_booking(h, pushed, [self.charged(h, p) for p in pushed])
		self.assertBillsLikeTheDesk(ota, desk)

	def india(self, provider: str) -> frappe._dict:
		"""Rates exclude GST, slab 5% up to 7,500 and 18% above: 7,000 the
		first night, 10,000 the second."""
		h = self.hotel(inclusive=0, currency="INR", country="India", price=7000, provider=provider,
		               slabs=(7500, 5, 18))
		self.season(h, NIGHT + 1, 10000)
		return h

	def test_two_nights_in_two_gst_slabs_on_aiosell(self):
		"""AioSell's prices are before tax (they add up to amountBeforeTax,
		17,000); the guest pays 7,350 + 11,800 = 19,150. Each night keeps its
		own price and slab, not one blended 19,150 / 17,000 mark-up."""
		h = self.india("AioSell")
		pushed = [self.aiosell_push(h), self.aiosell_push(h, NIGHT + 1)]
		self.assertEqual([Decimal(str(p)) for p in pushed], [Decimal("7000"), Decimal("10000")])
		desk = self.desk_booking(h, nights=2)
		ota = self.aiosell_booking(h, pushed, [self.charged(h, 7000, 5), self.charged(h, 10000, 18)],
		                           before_tax=17000)
		self.assertBillsLikeTheDesk(ota, desk)

	def test_two_nights_in_two_gst_slabs_on_channex(self):
		"""Channex reports the day prices before tax and 2,150 of GST on top."""
		h = self.india("Channex")
		pushed = [self.channex_push(h), self.channex_push(h, NIGHT + 1)]
		self.assertEqual([Decimal(p) for p in pushed], [Decimal("7000"), Decimal("10000")])
		desk = self.desk_booking(h, nights=2)
		ota = self.channex_booking(h, [self.charged(h, 7000, 5), self.charged(h, 10000, 18)],
		                           report="exclusive", taxes=[5, 18])
		self.assertBillsLikeTheDesk(ota, desk)


class NightRateMinorUnitTestCase(_SellingBasisFixture, IntegrationTestCase):
	"""A locked night rate keeps the currency's ISO 4217 decimals (three for
	KWD) and never fewer than two."""

	def kwd(self, provider: str = "Channex") -> frappe._dict:
		return self.hotel(inclusive=0, currency="KWD", country="Kuwait", price=12.345, tax=0,
		                  provider=provider)

	def locked(self, doc) -> list:
		return [(str(r.stay_date), Decimal(str(r.rate))) for r in doc.night_rates]

	def test_a_kwd_desk_booking_locks_the_third_decimal(self):
		h = self.kwd()
		self.assertEqual(self.locked(self.desk_booking(h)), [(day(NIGHT), Decimal("12.345"))])

	def test_a_kwd_channex_booking_locks_the_third_decimal(self):
		h = self.kwd()
		pushed = self.channex_push(h)
		self.assertEqual(pushed, "12.345")
		ota = self.channex_booking(h, self.charged(h, pushed))
		self.assertEqual(self.locked(ota), [(day(NIGHT), Decimal("12.345"))])
		self.assertEqual(Decimal(str(ota.amount_after_tax)), Decimal("12.345"))

	def test_aiosell_is_offered_a_kwd_price_with_three_decimals(self):
		h = self.kwd(provider="AioSell")
		self.assertEqual(Decimal(str(self.aiosell_push(h))), Decimal("12.345"))

	def test_a_kwd_aiosell_booking_locks_the_third_decimal(self):
		h = self.kwd(provider="AioSell")
		pushed = self.aiosell_push(h)
		ota = self.aiosell_booking(h, pushed, self.charged(h, pushed))
		self.assertEqual(self.locked(ota), [(day(NIGHT), Decimal("12.345"))])

	def test_ugx_night_rates_keep_two_decimals_on_a_tax_inclusive_property(self):
		"""UGX has no decimals, but the net share of a tax-inclusive 150,000
		is 127,118.64: rounding it to a whole 127,119 would bill
		150,000.42 with the 18% on top, not the 150,000 the guest was
		quoted."""
		h = self.hotel(inclusive=1)
		pushed = self.channex_push(h)
		desk = self.desk_booking(h)
		ota = self.channex_booking(h, self.charged(h, pushed))
		for doc in (desk, ota):
			self.assertEqual(self.locked(doc), [(day(NIGHT), Decimal("127118.64"))])


class PropertyBasisChangeTestCase(_SellingBasisFixture, IntegrationTestCase):
	"""Editing what a property's prices are offered on - whether its rates
	include tax, its currency, its country (the tax pack) and its room tax
	rates - queues the channels' rates at once, not at the nightly sweep."""

	def queued(self, h) -> list:
		return frappe.get_all("Channel ARI Outbox", filters={"connection": h.conn.name, "status": "Pending"},
		                      fields=["kinds", "date_from", "date_to"])

	def edit(self, h, **values) -> None:
		prop = frappe.get_doc("Property", h.property)
		prop.update(values)
		prop.save(ignore_permissions=True)

	def test_each_selling_basis_field_queues_the_rates(self):
		edits = {"rates_include_tax": 1, "currency": "USD", "country": "Kenya", "gst_mode": "Fixed",
		         "gst_slab_threshold": 9000, "gst_rate_low": 12, "gst_rate_high": 12}
		for field, value in edits.items():
			with self.subTest(field=field):
				h = self.hotel(inclusive=0)
				self.edit(h, **{field: value})
				self.assertEqual([r.kinds for r in self.queued(h)], ["restrictions"], field)

	def test_an_edit_that_moves_no_price_queues_nothing(self):
		h = self.hotel(inclusive=0)
		self.edit(h, house_rules="No smoking", website="https://basis.example")
		self.assertEqual(self.queued(h), [])

	def test_the_new_basis_goes_out_without_waiting_for_the_sweep(self):
		"""A plan 10,000 over the base price: offered at 160,000 while rates
		exclude tax; once they include it, 161,800 (127,118.64 + 10,000 with
		the 18% put back), sent by the edit's own change."""
		h = self.hotel(inclusive=0, plans={"amt": ("Amount", 10000)})
		self.assertEqual(Decimal(self.channex_push(h, ext_rate="rp-std-amt")), Decimal("160000"))
		with patch.object(channel_ari, "ARI_HORIZON_DAYS", NIGHT + 2):
			self.edit(h, rates_include_tax=1)
			fake = FakeChannex()
			self.flush(h.conn, fake)
		offered = {v.get("date") or (v["date_from"], v["date_to"]): v["rate"]
		           for call in fake.posts("restrictions") for v in call if v["rate_plan_id"] == "rp-std-amt"}
		self.assertTrue(any(Decimal(rate) == Decimal("161800") and (
			key == day(NIGHT) or (isinstance(key, tuple) and key[0] <= day(NIGHT) <= key[1]))
			for key, rate in offered.items()), offered)


def _tax(name, total, type=None, is_inclusive=False, **extra) -> dict:
	"""One rooms[].taxes[] entry as Channex relays it (BookingRoomTax: type
	is a string or null, name a string)."""
	return dict({"is_inclusive": is_inclusive, "name": name, "total_price": str(total),
	             "type": type}, **extra)


VAT_ON_TOP = _tax("VAT", "27000.00", type="tax")


class ChannexTaxRuleTestCase(_SellingBasisFixture, IntegrationTestCase):
	"""Which of a booking room's taxes[] is the room tax the folio bills is
	the connection's configuration (Channel Manager Connection > OTA tax
	rules): rules on the tax's type and name, tried in order, the first that
	matches deciding. A new Channex connection starts from rules that count
	VAT, GST, sales, occupancy and room taxes and leave out fees, city taxes
	and a tax the OTA withheld; a property can change them.

	Each booking is one night sold at 150,000 before tax: 177,000 billed when
	the 27,000 listed on top is the room tax, 150,000 when it is not."""

	def payload(self, h, *taxes, ota_name: str = "Expedia") -> dict:
		"""One night at 150,000 before tax whose room lists `taxes`."""
		return {"event": "booking", "payload": {
			"status": "new", "ota_reservation_code": "TAX-" + frappe.generate_hash(length=6),
			"ota_name": ota_name, "arrival_date": day(NIGHT), "departure_date": day(NIGHT + 1),
			"currency": h.currency, "customer": {"name": "Ota", "surname": "Guest"},
			"rooms": [{"room_type_id": "rt-std", "checkin_date": day(NIGHT),
			           "checkout_date": day(NIGHT + 1), "occupancy": {"adults": 2, "children": 0},
			           "days": {day(NIGHT): "150000.00"}, "taxes": list(taxes)}]}}

	def booked(self, h, *taxes, ota_name: str = "Expedia") -> tuple[float, bool]:
		"""(total, nightly_net) of the one room Channex's webhook parses
		into, read with the connection's configuration as saved."""
		conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
		event = channex.parse_webhook(conn, self.payload(h, *taxes, ota_name=ota_name))[0]
		return flt(event["total"], 2), event["nightly_net"]

	def landed(self, h, *taxes, ota_name: str = "Expedia"):
		"""The reservation that booking lands as, delivered by the webhook."""
		payload = self.payload(h, *taxes, ota_name=ota_name)
		self.deliver(h, payload)
		return self.received(h, payload["payload"]["ota_reservation_code"])

	def assertHeld(self, doc, *parts):
		"""Held for staff: Requested, nothing billed, no night locked, the
		reason (with each of `parts`) in the note."""
		self.assertEqual(doc.status, "Requested")
		self.assertEqual(flt(doc.amount_after_tax), 0)
		self.assertEqual([r.stay_date for r in doc.night_rates], [])
		for part in ("resolve", *parts):
			self.assertIn(part, doc.special_requests or "")

	def rules(self, h, rows) -> None:
		"""Replace the connection's OTA tax rules with `rows`
		(match_on, match_type, match_text, counts_as), in that order."""
		conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
		conn.set("ota_tax_rules", [{"match_on": on, "match_type": how, "match_text": text, "counts_as": counts}
		                           for on, how, text, counts in rows])
		conn.save(ignore_permissions=True)

	def test_a_vat_without_a_type_is_read_by_its_name(self):
		"""BookingRoomTax.type may be null: the name still says VAT."""
		h = self.hotel(inclusive=0)
		self.assertEqual(self.booked(h, _tax("VAT (18%)", "27000.00", type=None)), (177000.0, True))

	def test_us_sales_occupancy_and_room_taxes_are_the_room_tax(self):
		"""Expedia "Total Amount Excluding Taxes" and Agoda "Exclusive" are
		the US model (docs.channex.io), where the room tax is a sales,
		occupancy or room tax, not a VAT."""
		h = self.hotel(inclusive=0)
		for label in ("Sales Tax", "Occupancy Tax", "Room Tax"):
			with self.subTest(label=label):
				self.assertEqual(self.booked(h, _tax(label, "27000.00", type=label)), (177000.0, True))

	def test_a_gst_is_the_room_tax(self):
		h = self.hotel(inclusive=0)
		for name, type_ in (("GST (18%)", None), ("Goods and Services Tax", "Goods and Services Tax")):
			with self.subTest(name=name):
				self.assertEqual(self.booked(h, _tax(name, "27000.00", type=type_)), (177000.0, True))

	def test_a_city_tax_of_type_tax_is_left_out(self):
		"""Channex's generic type "tax" on a city tax: the name says what it is."""
		h = self.hotel(inclusive=0)
		self.assertEqual(self.booked(h, VAT_ON_TOP, _tax("City Tax", "5000.00", type="tax")),
		                 (177000.0, True))

	def test_a_fee_whose_label_mentions_vat_is_left_out(self):
		h = self.hotel(inclusive=0)
		self.assertEqual(self.booked(h, VAT_ON_TOP, _tax("Cleaning Fee", "20000.00",
		                                                 type="Cleaning Fee (incl. VAT)")),
		                 (177000.0, True))

	def test_fees_city_taxes_and_withheld_taxes_are_left_out(self):
		h = self.hotel(inclusive=0)
		for tax in (_tax("Cleaning fee", "20000.00", type="fee"),
		            _tax("City tax", "5000.00", type="city_tax"),
		            _tax("Tourism levy", "3000.00", type="tax"),
		            _tax("Service charge", "15000.00", type=None),
		            _tax("VAT (Withheld Tax)", "27000.00", type="tax", is_withheld=True)):
			with self.subTest(tax=tax["name"]):
				self.assertEqual(self.booked(h, tax), (150000.0, False))

	def test_the_standard_rules_are_the_connections_own_rows(self):
		"""The defaults are configuration on the connection, not code: both
		kinds of row are there to read and change."""
		h = self.hotel(inclusive=0)
		conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
		self.assertEqual({r.counts_as for r in conn.ota_tax_rules}, {"Room tax", "Ignored"})

	def test_a_property_that_configured_its_own_rules(self):
		"""A property's own rules replace the standard ones: with only a
		lodging tax counted, a lodging tax on top is billed and a VAT is not."""
		h = self.hotel(inclusive=0)
		self.assertEqual(self.booked(h, _tax("Lodging Tax", "27000.00", type=None)), (150000.0, False))
		self.rules(h, [("Type or Name", "Contains", "lodging tax", "Room tax")])
		self.assertEqual(self.booked(h, _tax("Lodging Tax", "27000.00", type=None)), (177000.0, True))
		self.assertEqual(self.booked(h, VAT_ON_TOP), (150000.0, False))

	def test_the_first_matching_rule_decides(self):
		"""A property that bills the city tax on its own folio puts a rule
		for it above the one that leaves city taxes out."""
		h = self.hotel(inclusive=0)
		conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
		standard = [(r.match_on, r.match_type, r.match_text, r.counts_as) for r in conn.ota_tax_rules]
		self.rules(h, [("Name", "Is", "City Tax", "Room tax"), *standard])
		self.assertEqual(self.booked(h, VAT_ON_TOP, _tax("City Tax", "5000.00", type="tax")),
		                 (182000.0, True))

	def test_matching_is_on_whole_words(self):
		""""vat" is a word of "VAT (18%)", not of "Private transfer"; an
		"Is" rule needs the whole label."""
		h = self.hotel(inclusive=0)
		self.rules(h, [("Type or Name", "Contains", "vat", "Room tax"), ("Type", "Is", "tax", "Room tax")])
		self.assertEqual(self.booked(h, _tax("VAT (18%)", "27000.00", type=None)), (177000.0, True))
		self.assertEqual(self.booked(h, _tax("Private transfer", "27000.00", type=None)), (150000.0, False))
		self.assertEqual(self.booked(h, _tax("Sales", "27000.00", type="Sales Tax")), (150000.0, False))
		self.assertEqual(self.booked(h, _tax("Sales", "27000.00", type="TAX")), (177000.0, True))

	def test_the_round_trip_bills_a_vat_read_by_its_name_like_the_desk(self):
		h = self.hotel(inclusive=0)
		pushed = self.channex_push(h)
		desk = self.desk_booking(h)
		payload = self.channex_payload(h, self.charged(h, pushed), report="exclusive")
		for tax in payload["payload"]["rooms"][0]["taxes"]:
			tax.update(type=None, name="VAT (18%)")
		self.deliver(h, payload)
		self.assertBillsLikeTheDesk(self.received(h, payload["payload"]["ota_reservation_code"]), desk)

	def test_migrate_gives_existing_channex_connections_the_standard_rules(self):
		"""A connection made before the rules existed has none: the patch
		gives it the standard ones and leaves a property's own alone."""
		from kamra.patches.v48.seed_channel_ota_tax_rules import execute

		fresh = self.hotel(inclusive=0)
		standard = [(r.match_on, r.match_type, r.match_text, r.counts_as) for r in
		            frappe.get_doc("Channel Manager Connection", fresh.conn.name).ota_tax_rules]
		old, own = self.hotel(inclusive=0), self.hotel(inclusive=0)
		frappe.db.delete("Channel OTA Tax Rule", {"parenttype": "Channel Manager Connection",
		                                          "parent": old.conn.name})
		self.rules(own, [("Type or Name", "Contains", "lodging tax", "Room tax")])
		self.assertEqual(self.booked(old, VAT_ON_TOP), (150000.0, False))
		execute()
		rows = lambda h: [(r.match_on, r.match_type, r.match_text, r.counts_as) for r in  # noqa: E731
		                  frappe.get_doc("Channel Manager Connection", h.conn.name).ota_tax_rules]
		self.assertTrue(standard)
		self.assertEqual(rows(old), standard)
		self.assertEqual(rows(own), [("Type or Name", "Contains", "lodging tax", "Room tax")])
		self.assertEqual(self.booked(old, VAT_ON_TOP), (177000.0, True))


	# -- R2: rules in any language, a connection that becomes Channex, rules that cannot match

	def test_rules_read_words_in_any_script(self):
		"""A rule is the property's own words, in whatever script its OTAs
		use (Neon R1 P1-2: the Arabic and Chinese rules saved, then never
		matched): letters and digits of any script, case folded."""
		h = self.hotel(inclusive=0)
		self.rules(h, [("Type or Name", "Contains", "ضريبة القيمة المضافة", "Room tax"),
		               ("Type or Name", "Contains", "增值税", "Room tax"),
		               ("Type or Name", "Contains", "MwSt", "Room tax"),
		               ("Type or Name", "Contains", "taxe de séjour", "Ignored")])
		for label in ("ضريبة القيمة المضافة (15%)", "增值税 (6%)", "MWST 19%"):
			with self.subTest(label=label):
				self.assertEqual(self.booked(h, _tax(label, "27000.00")), (177000.0, True))
		doc = self.landed(h, _tax("增值税", "27000.00"), _tax("Taxe de Séjour", "5000.00"))
		self.assertEqual((doc.status, flt(doc.amount_after_tax)), ("Confirmed", 177000.0))

	def test_a_connection_switched_to_channex_starts_from_the_standard_rules(self):
		"""Made for another provider and switched to Channex, a connection
		has no rules of its own yet (Neon R1 P1-1: 0 rules, the VAT on top
		dropped, 150,000 confirmed instead of 177,000): it gets the standard
		ones, as a new Channex connection does."""
		from kamra.kamra.doctype.channel_ota_tax_rule.channel_ota_tax_rule import STANDARD_RULES

		h = self.hotel(inclusive=0, provider="Custom")
		conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
		self.assertEqual(list(conn.ota_tax_rules), [])
		conn.provider = "Channex"
		conn.save(ignore_permissions=True)
		rows = [(r.match_on, r.match_type, r.match_text, r.counts_as) for r in
		        frappe.get_doc("Channel Manager Connection", h.conn.name).ota_tax_rules]
		self.assertEqual(rows, list(STANDARD_RULES))
		desk = self.desk_booking(h)
		pushed = self.channex_push(h)
		payload = self.channex_payload(h, self.charged(h, pushed), report="exclusive")
		self.deliver(h, payload)
		ota = self.received(h, payload["payload"]["ota_reservation_code"])
		self.assertEqual(ota.status, "Confirmed")
		self.assertBillsLikeTheDesk(ota, desk)

	def test_a_rule_with_no_word_is_refused(self):
		"""A rule of punctuation alone ("---", "%") can never match a tax:
		it is refused when saved, not kept as a rule that does nothing."""
		h = self.hotel(inclusive=0)
		for text in ("---", "%"):
			with self.subTest(text=text), self.assertRaises(frappe.ValidationError):
				self.rules(h, [("Type or Name", "Contains", text, "Room tax")])


	def test_a_tax_on_top_no_rule_reads_is_held_for_staff(self):
		"""A Lodging Tax, India's CGST and IGST, Germany's MwSt and France's
		TVA listed on top of the price (is_inclusive false) that no rule
		reads were dropped, and the booking confirmed at 150,000 (Neon R1
		P1-2). The guest paid that tax on top: the booking is held for staff
		to add a rule, whatever the OTA's tax basis, the tax named in the
		note."""
		h = self.hotel(inclusive=0)
		for name, type_, amount in (("Lodging Tax", "lodging_tax", "27000.00"),
		                            ("CGST (9%)", None, "13500.00"), ("IGST (18%)", None, "27000.00"),
		                            ("MwSt (19%)", None, "28500.00"), ("TVA (10%)", None, "15000.00")):
			with self.subTest(name=name):
				self.assertHeld(self.landed(h, _tax(name, amount, type=type_)), name)

	def test_a_room_tax_beside_a_tax_no_rule_reads_is_held(self):
		"""One tax read (a Sales Tax) does not cover another on top that is
		not (a County Lodging Tax)."""
		h = self.hotel(inclusive=0)
		self.assertHeld(self.landed(h, _tax("Sales Tax", "27000.00", type="Sales Tax"),
		                            _tax("County Lodging Tax", "12000.00")), "County Lodging Tax")

	def test_a_tax_a_rule_ignores_is_not_held(self):
		"""An Ignored rule is the property's answer for that tax (pin: R1
		already confirmed it)."""
		h = self.hotel(inclusive=0)
		conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
		standard = [(r.match_on, r.match_type, r.match_text, r.counts_as) for r in conn.ota_tax_rules]
		self.rules(h, [("Name", "Contains", "lodging tax", "Ignored"), *standard])
		doc = self.landed(h, VAT_ON_TOP, _tax("County Lodging Tax", "12000.00"))
		self.assertEqual((doc.status, flt(doc.amount_after_tax)), ("Confirmed", 177000.0))

	def test_a_tax_inside_the_price_or_withheld_by_the_ota_is_not_held(self):
		"""A tax no rule reads that is inside the day price (is_inclusive
		true) is already in what the folio bills; one the OTA withheld
		(is_withheld: the tax the OTA collected and remits itself,
		docs.channex.io) is not the property's to bill. Neither holds the
		booking (pin: R1 already confirmed both)."""
		h = self.hotel(inclusive=0)
		for tax in (_tax("MwSt (19%)", "23949.58", is_inclusive=True),
		            _tax("Lodging Tax", "27000.00", is_withheld=True)):
			with self.subTest(tax=tax["name"]):
				doc = self.landed(h, tax)
				self.assertEqual((doc.status, flt(doc.amount_after_tax)), ("Confirmed", 150000.0))

	def test_a_channex_connection_with_no_rules_holds_every_booking(self):
		"""With no rules nothing on a booking can be read: not a VAT on top,
		nor whether a booking listing no tax has it in the price (Neon R1
		P1-1). Held for staff, never confirmed."""
		h = self.hotel(inclusive=0)
		frappe.db.delete("Channel OTA Tax Rule", {"parenttype": "Channel Manager Connection",
		                                          "parent": h.conn.name})
		for taxes in ((VAT_ON_TOP,), ()):
			with self.subTest(taxes=len(taxes)):
				self.assertHeld(self.landed(h, *taxes), "OTA tax rules")

	def test_tax_words_splits_letter_digit_and_script_boundaries(self):
		"""Adjacent letters and digits ("VAT18%", "增值税6%") and Latin/CJK
		boundaries ("Hotel增值税") split into distinct words so rules do not
		miss joined rate percentages."""
		self.assertEqual(channex.tax_words("VAT18%"), ["vat", "18"])
		self.assertEqual(channex.tax_words("18%VAT"), ["18", "vat"])
		self.assertEqual(channex.tax_words("GST5"), ["gst", "5"])
		self.assertEqual(channex.tax_words("增值税6%"), ["增值税", "6"])
		self.assertEqual(channex.tax_words("Hotel增值税"), ["hotel", "增值税"])
		self.assertEqual(channex.tax_words("VAT (18%)"), ["vat", "18"])

	def test_letter_digit_joined_and_cjk_compound_taxes_match_and_still_fail_closed(self):
		"""Joined percentages ("VAT18%", "增值税6%") and unspaced CJK phrases
		("酒店增值税" containing "增值税") match their rules; unmatched CJK and
		Latin substring near-misses still fail closed."""
		h = self.hotel(inclusive=0)
		self.rules(h, [
			("Type or Name", "Contains", "vat", "Room tax"),
			("Type or Name", "Contains", "gst", "Room tax"),
			("Type or Name", "Contains", "增值税", "Room tax"),
			("Type or Name", "Contains", "宿泊税", "Room tax"),
			("Type or Name", "Contains", "부가가치세", "Room tax"),
		])
		for label in ("VAT18%", "GST5%", "增值税6%", "酒店增值税", "酒店增值税6%", "東京都宿泊税", "호텔부가가치세10%"):
			with self.subTest(label=label):
				self.assertEqual(self.booked(h, _tax(label, "27000.00")), (177000.0, True))
		# Fail closed: unmatched CJK exclusive tax and Latin partial-word "Private"
		self.assertHeld(self.landed(h, _tax("酒店城市税", "5000.00")), "酒店城市税")
		self.assertHeld(self.landed(h, _tax("Private transfer", "27000.00")), "Private transfer")
		# "Is" stays exact even for CJK
		self.rules(h, [("Name", "Is", "增值税", "Room tax")])
		exact = self.landed(h, _tax("增值税", "27000.00"))
		self.assertEqual((exact.status, flt(exact.amount_after_tax)), ("Confirmed", 177000.0))
		self.assertHeld(self.landed(h, _tax("酒店增值税", "27000.00")), "酒店增值税")

	def test_single_character_cjk_tax_rule_is_refused_on_save(self):
		"""A single-character CJK rule phrase (e.g. "税") matches unrelated
		city/tourism/exempt taxes: refuse it on save, while allowing 2+ char
		CJK rules."""
		h = self.hotel(inclusive=0)
		for match_type, bad in (
			("Contains", "税"),
			("Contains", "料"),
			("Contains", "세"),
			("Contains", "税%"),
			("Contains", "Hotel 税 6%"),
			("Is", "税"),
		):
			with self.subTest(match_type=match_type, text=bad), self.assertRaises(frappe.ValidationError):
				self.rules(h, [("Type or Name", match_type, bad, "Room tax")])
		# 2+ char CJK phrases (and mixed-script phrases with 2+ char CJK) save cleanly
		self.rules(h, [
			("Type or Name", "Contains", "城市税", "Ignored"),
			("Type or Name", "Contains", "增值税", "Room tax"),
			("Type or Name", "Contains", "宿泊税", "Room tax"),
			("Type or Name", "Contains", "Hotel 增值税 6%", "Room tax"),
			("Name", "Is", "增值税", "Room tax"),
		])

	def test_mixed_script_middle_and_boundary_cjk_tokens_match_and_fail_closed(self):
		"""Mixed-script Contains rules match CJK substrings at leading, middle,
		and trailing positions ("Hotel 增值税 6%" matches "Hotel 酒店增值税 6%",
		"Hotel 增值税" matches "Hotel 酒店增值税", "增值税 6%" matches "增值税率 6%"),
		while non-CJK tokens stay whole-word and unmatched taxes fail closed."""
		h = self.hotel(inclusive=0)
		self.rules(h, [("Type or Name", "Contains", "Hotel 增值税 6%", "Room tax")])
		for label in ("Hotel 增值税 6%", "Hotel 酒店增值税 6%", "Hotel 增值税率 6%"):
			with self.subTest(rule="middle", label=label):
				self.assertEqual(self.booked(h, _tax(label, "27000.00")), (177000.0, True))
		for bad in ("Hotel 酒店增值税 60%", "Hotels 酒店增值税 6%", "Hotel 酒店城市税 6%"):
			with self.subTest(rule="middle-miss", label=bad):
				self.assertHeld(self.landed(h, _tax(bad, "27000.00")), bad)

		self.rules(h, [("Type or Name", "Contains", "Hotel 增值税", "Room tax")])
		self.assertEqual(self.booked(h, _tax("Hotel 酒店增值税", "27000.00")), (177000.0, True))
		self.assertHeld(self.landed(h, _tax("Hotels 酒店增值税", "27000.00")), "Hotels 酒店增值税")

		self.rules(h, [("Type or Name", "Contains", "增值税 6%", "Room tax")])
		self.assertEqual(self.booked(h, _tax("增值税率 6%", "27000.00")), (177000.0, True))
		self.assertHeld(self.landed(h, _tax("增值税率 60%", "27000.00")), "增值税率 60%")

	def test_ignored_cjk_rule_above_broader_room_tax_rule_wins_by_precedence(self):
		"""First matching rule wins: STANDARD_RULES keeps every Ignored rule
		above every Room tax rule, and placing an Ignored CJK rule above a
		broader Room tax rule prevents misclassifying city/tourism tax."""
		from kamra.kamra.doctype.channel_ota_tax_rule.channel_ota_tax_rule import STANDARD_RULES

		seen_room_tax = False
		for _on, _how, _text, counts_as in STANDARD_RULES:
			if counts_as == "Room tax":
				seen_room_tax = True
			else:
				self.assertFalse(seen_room_tax, "STANDARD_RULES must place all Ignored rules above Room tax rules")

		h = self.hotel(inclusive=0)
		self.rules(h, [
			("Type or Name", "Contains", "城市税", "Ignored"),
			("Type or Name", "Contains", "增值税", "Room tax"),
			("Type", "Is", "tax", "Room tax"),
		])
		# City tax with type="tax" is matched by the earlier Ignored rule, not Type Is tax
		doc = self.landed(h, VAT_ON_TOP, _tax("酒店城市税", "5000.00", type="tax"))
		self.assertEqual((doc.status, flt(doc.amount_after_tax)), ("Confirmed", 177000.0))


class ChannexTaxBasisTestCase(_SellingBasisFixture, IntegrationTestCase):
	"""Whether an OTA's amounts include tax is that OTA's setting on the
	channel manager (Expedia booking_amount_settings, Agoda
	booking_tax_settings), and a Channex booking does not carry it: a
	booking that lists no taxes reads the same either way. The connection
	records it per OTA (Channel Manager Connection > OTA tax basis). A
	booking from an OTA set to report amounts before tax that lists no tax
	is held as Requested for staff, its amount in the note, never billed as
	if tax were in it. An OTA not on the list keeps today's reading."""

	def before_tax(self, h, channel: str = "Expedia") -> None:
		conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
		conn.append("ota_tax_basis", {"channel": channel, "amounts": "Excluding tax"})
		conn.save(ignore_permissions=True)

	def booking(self, h, ota_name: str = "Expedia", listed: bool = False, status: str = "new",
	            ref: str | None = None, taxes=()) -> dict:
		"""One night sold at 150,000 before tax, the guest paying it plus
		the property's room tax, reported before tax; `taxes` are what the
		room lists (default: none), or the room tax on top when `listed`."""
		payload = self.channex_payload(h, self.charged(h, Decimal("150000")), report="exclusive")
		body = payload["payload"]
		body.update(ota_name=ota_name, status=status)
		if ref:
			body["ota_reservation_code"] = ref
		if not listed:
			body["rooms"][0]["taxes"] = list(taxes)
		return payload

	def land(self, h, payload):
		self.deliver(h, payload)
		return self.received(h, payload["payload"]["ota_reservation_code"])

	def sellable(self, h) -> int:
		return cm._sellable(h.property, h.room_type, day(NIGHT), day(NIGHT + 1))

	def test_a_before_tax_ota_listing_no_tax_is_held_for_staff(self):
		h = self.hotel(inclusive=0)
		self.before_tax(h)
		doc = self.land(h, self.booking(h))
		self.assertEqual(doc.status, "Requested")
		for part in ("Expedia", "150000", "UGX", "resolve"):
			self.assertIn(part, doc.special_requests or "")
		self.assertEqual(flt(doc.amount_after_tax), 0)
		self.assertEqual(self.sellable(h), 3)

	def test_a_before_tax_ota_listing_only_a_city_tax_is_held_for_staff(self):
		"""A city tax is not the room tax: the room tax is still missing."""
		h = self.hotel(inclusive=0)
		self.before_tax(h)
		doc = self.land(h, self.booking(h, taxes=[_tax("City Tax", "5000.00", type="city_tax")]))
		self.assertEqual(doc.status, "Requested")
		self.assertEqual(flt(doc.amount_after_tax), 0)

	def test_a_modify_listing_no_tax_is_held_for_staff(self):
		h = self.hotel(inclusive=0)
		self.before_tax(h)
		first = self.booking(h, listed=True)
		self.assertEqual(self.land(h, first).status, "Confirmed")
		ref = first["payload"]["ota_reservation_code"]
		doc = self.land(h, self.booking(h, status="modified", ref=ref))
		self.assertEqual(doc.status, "Requested")
		self.assertIn("Expedia", doc.special_requests or "")
		self.assertEqual(flt(doc.amount_after_tax), 0)

	def test_a_before_tax_ota_that_lists_its_tax_bills_like_the_desk(self):
		h = self.hotel(inclusive=0)
		self.before_tax(h)
		desk = self.desk_booking(h)
		doc = self.land(h, self.booking(h, listed=True))
		self.assertEqual(doc.status, "Confirmed")
		self.assertBillsLikeTheDesk(doc, desk)

	def test_another_ota_on_the_same_connection_keeps_todays_reading(self):
		h = self.hotel(inclusive=0)
		self.before_tax(h, "Expedia")
		doc = self.land(h, self.booking(h, ota_name="Booking.com"))
		self.assertEqual(doc.status, "Confirmed")
		self.assertEqual(flt(doc.amount_after_tax), 150000)

	def test_a_connection_with_no_tax_basis_keeps_todays_reading(self):
		h = self.hotel(inclusive=0)
		doc = self.land(h, self.booking(h))
		self.assertEqual(doc.status, "Confirmed")
		self.assertEqual(flt(doc.amount_after_tax), 150000)

	def test_a_property_without_room_tax_is_not_held(self):
		"""Nothing is missing when the room carries no tax: before tax and
		after are the same amount."""
		h = self.hotel(inclusive=0, tax=0)
		self.before_tax(h)
		doc = self.land(h, self.booking(h))
		self.assertEqual(doc.status, "Confirmed")
		self.assertEqual(flt(doc.amount_after_tax), 150000)

	# -- R2: the modify path, a tax that is not there, one OTA listed twice

	def test_a_held_modify_keeps_no_locked_night(self):
		"""Held at 0, the stay kept the night locked at 150,000 from the
		booking before it (Neon R1 P1-3, c1)."""
		h = self.hotel(inclusive=0)
		self.before_tax(h)
		first = self.booking(h, listed=True)
		self.assertTrue(self.land(h, first).night_rates)
		doc = self.land(h, self.booking(h, status="modified", ref=first["payload"]["ota_reservation_code"]))
		self.assertEqual(doc.status, "Requested")
		self.assertEqual(flt(doc.amount_after_tax), 0)
		self.assertEqual([r.stay_date for r in doc.night_rates], [])

	def test_a_held_modify_leaves_an_in_house_stay_as_it_is(self):
		"""The guest is in house: a modify whose tax cannot be read never
		turns the stay back into a request (Neon R1 P1-3, c2). It stays
		Checked In with its nights and amount, flagged for staff on the stay
		and in the action log."""
		h = self.hotel(inclusive=0)
		self.before_tax(h)
		first = self.booking(h, listed=True)
		booked = self.land(h, first)
		frappe.db.set_value("Reservation", booked.name, "status", "Checked In")
		nights = lambda doc: [(str(r.stay_date), flt(r.rate, 2), flt(r.gst_rate, 2))  # noqa: E731
		                      for r in doc.night_rates]
		doc = self.land(h, self.booking(h, status="modified", ref=first["payload"]["ota_reservation_code"]))
		self.assertEqual(doc.status, "Checked In")
		self.assertEqual(flt(doc.amount_after_tax), flt(booked.amount_after_tax))
		self.assertEqual(nights(doc), nights(booked))
		for part in ("resolve", "Expedia"):
			self.assertIn(part, doc.special_requests or "")
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": doc.name,
			"action_type": "channel.modify_needs_review"}))

	def test_a_held_modify_leaves_a_departed_stay_as_it_is(self):
		"""Nor a stay the guest has left (Neon R1 P1-3: never demote Checked
		In / Checked Out): it stays Checked Out, its nights and amount as
		billed, flagged for staff."""
		h = self.hotel(inclusive=0)
		self.before_tax(h)
		first = self.booking(h, listed=True)
		booked = self.land(h, first)
		frappe.db.set_value("Reservation", booked.name, "status", "Checked Out")
		nights = lambda doc: [(str(r.stay_date), flt(r.rate, 2), flt(r.gst_rate, 2))  # noqa: E731
		                      for r in doc.night_rates]
		doc = self.land(h, self.booking(h, status="modified", ref=first["payload"]["ota_reservation_code"]))
		self.assertEqual(doc.status, "Checked Out")
		self.assertEqual(flt(doc.amount_after_tax), flt(booked.amount_after_tax))
		self.assertEqual(nights(doc), nights(booked))
		self.assertIn("resolve", doc.special_requests or "")

	def test_a_later_modify_listing_the_tax_releases_the_hold(self):
		"""Held for a missing tax, then modified with it: confirmed, priced
		like the desk, and holding its room again (Neon R1 P1-3, c3)."""
		h = self.hotel(inclusive=0)
		self.before_tax(h)
		first = self.booking(h)
		self.assertEqual(self.land(h, first).status, "Requested")
		desk = self.desk_booking(h)
		doc = self.land(h, self.booking(h, listed=True, status="modified",
		                                ref=first["payload"]["ota_reservation_code"]))
		self.assertEqual(doc.status, "Confirmed")
		self.assertBillsLikeTheDesk(doc, desk)
		self.assertNotIn("resolve", doc.special_requests or "")
		self.assertEqual(self.sellable(h), 1)

	def test_a_room_tax_of_nothing_or_withheld_is_not_listed(self):
		"""A VAT of 0.00, or one the OTA withheld (is_withheld: Channex marks
		the taxes an OTA collected and remits itself, docs.channex.io
		changelog), is not the room tax on top of a before-tax price: still
		held (Neon R1 P1-3, c4)."""
		h = self.hotel(inclusive=0)
		self.before_tax(h)
		for tax in (_tax("VAT", "0.00", type="tax"),
		            _tax("VAT", "27000.00", type="tax", is_withheld=True)):
			with self.subTest(tax=tax):
				doc = self.land(h, self.booking(h, taxes=[tax]))
				self.assertEqual(doc.status, "Requested")
				self.assertEqual(flt(doc.amount_after_tax), 0)

	def test_an_ota_is_read_by_its_name_whatever_the_case(self):
		"""The basis row "hostelworld" is the OTA a booking names
		"Hostelworld"."""
		h = self.hotel(inclusive=0)
		self.before_tax(h, "hostelworld")
		self.assertEqual(self.land(h, self.booking(h, ota_name="Hostelworld")).status, "Requested")

	def test_an_ota_listed_twice_is_refused(self):
		"""Two rows for one OTA: the first won silently, the second ignored
		(Neon R1 P2). Refused when saved."""
		h = self.hotel(inclusive=0)
		for a, b in (("Booking.com", "BookingCom"), ("Expedia", "expedia  "), ("Hostelworld", "hostelworld")):
			with self.subTest(a=a, b=b), self.assertRaises(frappe.ValidationError):
				conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
				conn.set("ota_tax_basis", [{"channel": a, "amounts": "Including tax"},
				                           {"channel": b, "amounts": "Excluding tax"}])
				conn.save(ignore_permissions=True)

	def test_an_ota_basis_row_with_no_word_is_refused(self):
		"""A punctuation-only OTA name ("---", "...", "___") has an empty
		ota_key and can never match any booking: refused when saved, matching
		the rule-phrase check."""
		h = self.hotel(inclusive=0)
		for bad in ("---", "...", "!!!@@@", "___", "   "):
			with self.subTest(channel=bad), self.assertRaises(frappe.ValidationError):
				conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
				conn.set("ota_tax_basis", [{"channel": bad, "amounts": "Excluding tax"}])
				conn.save(ignore_permissions=True)


class ChannelModifySafetyTestCase(_SellingBasisFixture, IntegrationTestCase):
	"""OPERA Cloud OXI safety for in-house and closed stays:
	- UPDATE INHOUSE RES = N: an OTA modify for a Checked In stay never alters
	  financials, room, dates, or status; it flags the stay and logs
	  channel.modify_needs_review for front-desk review.
	- Closed stays (Checked Out, No Show, Cancelled) are immutable to OTA
	  modifies and record the unapplied update for review.
	- Inbound OTA cancellations never cancel Checked In, Checked Out, or
	  No Show stays; they leave the stay untouched and record it for review,
	  while duplicate cancels on already-Cancelled stays remain idempotent."""

	def add_deluxe_room_type(self, h) -> str:
		tag = frappe.generate_hash(length=5).upper()
		dlx = frappe.get_doc({
			"doctype": "Room Type", "property": h.property,
			"room_type_code": "DLX" + tag, "room_type_name": "Deluxe",
			"base_price": 250000, "adults_capacity": 2,
		}).insert(ignore_permissions=True).name
		for i in range(2):
			frappe.get_doc({
				"doctype": "Room", "property": h.property, "room_type": dlx,
				"room_number": f"D{tag}-{i + 1}",
			}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Channel Room Mapping", "connection": h.conn.name,
			"room_type": dlx, "external_room_id": "rt-dlx", "external_rate_id": "rp-dlx",
			"external_currency": h.currency,
		}).insert(ignore_permissions=True)
		return dlx

	def book_today(self, h, ota_name: str = "Booking.com", notes: str = ""):
		payload = self.channex_payload(h, Decimal("177000"), n=0, report="exclusive")
		payload["payload"]["ota_name"] = ota_name
		payload["payload"]["notes"] = notes
		self.deliver(h, payload)
		ref = payload["payload"]["ota_reservation_code"]
		return self.received(h, ref), ref

	def modify_event(self, h, ref: str, *, ota_name: str = "Booking.com",
	                 ext_room: str = "rt-dlx", nights: int = 2,
	                 gross: list[Decimal] | None = None, notes: str = "Changed via OTA") -> dict:
		gross = gross or [Decimal("295000")] * nights
		payload = self.channex_payload(h, gross, n=0, report="exclusive")
		body = payload["payload"]
		body.update(status="modified", ota_reservation_code=ref, ota_name=ota_name, notes=notes)
		body["rooms"][0]["room_type_id"] = ext_room
		body["rooms"][0]["occupancy"] = {"adults": 1, "children": 1}
		conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
		return channex.parse_webhook(conn, payload)[0]

	def cancel_event(self, h, ref: str, ota_name: str = "Booking.com") -> dict:
		payload = {
			"event": "booking",
			"payload": {
				"status": "cancelled",
				"ota_reservation_code": ref,
				"ota_name": ota_name,
				"rooms": [{"room_type_id": "rt-std", "days": {}}],
			},
		}
		conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
		return channex.parse_webhook(conn, payload)[0]

	def check_in_real(self, h, doc):
		from kamra import api, folio as folio_mod

		room = frappe.db.get_value("Room", {"property": h.property, "room_type": h.room_type}, "name")
		api.check_in(doc.name, room=room)
		doc.reload()
		folio_mod.post_remaining_nights(doc)
		folio_name = frappe.db.get_value("Folio", {"reservation": doc.name, "folio_type": "Guest"}, "name")
		self.assertTrue(folio_name, "Expected open guest folio after check-in")
		return frappe.get_doc("Reservation", doc.name), frappe.get_doc("Folio", folio_name)

	def check_out_real(self, h, doc):
		from kamra import api, folio as folio_mod

		doc, folio = self.check_in_real(h, doc)
		api.add_folio_payment(folio.name, "Cash", flt(folio.grand_total))
		api.check_out(doc.name)
		folio_mod.close_folio(folio.name)
		return frappe.get_doc("Reservation", doc.name), frappe.get_doc("Folio", folio.name)

	def night_snapshot(self, doc):
		return [(str(r.stay_date), flt(r.rate, 2), flt(r.gst_rate, 2)) for r in doc.night_rates]

	def folio_snapshot(self, folio_name: str):
		f = frappe.get_doc("Folio", folio_name)
		return (f.status, flt(f.grand_total, 2), flt(f.balance, 2),
		        [(c.description, flt(c.amount, 2), flt(c.gst_amount, 2)) for c in f.charges])

	def test_readable_modify_leaves_checked_in_stay_and_folio_unchanged_and_flags_review(self):
		from kamra import api

		h = self.hotel(inclusive=0)
		self.add_deluxe_room_type(h)
		booked, ref = self.book_today(h, notes="Early check-in")
		doc, folio = self.check_in_real(h, booked)
		before_nights = self.night_snapshot(doc)
		before_folio = self.folio_snapshot(folio.name)
		before_state = (doc.status, doc.room_type, doc.room, str(doc.check_in_date),
		                str(doc.check_out_date), doc.adults, doc.children,
		                flt(doc.amount_after_tax, 2), flt(doc.amount_before_tax, 2),
		                flt(doc.tax_amount, 2), doc.auto_price)

		conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
		# First: same room type with changed dates + price (silently rewrote Checked In on base)
		out = cm._apply_event(conn, self.modify_event(h, ref, ext_room="rt-std"))
		self.assertEqual(out["result"], "held_for_review")
		self.assertEqual(out["reservation"], doc.name)
		# Second: different room type change must also hold cleanly without mutating or throwing
		out_rt = cm._apply_event(conn, self.modify_event(h, ref, ext_room="rt-dlx"))
		self.assertEqual(out_rt["result"], "held_for_review")

		doc.reload()
		after_state = (doc.status, doc.room_type, doc.room, str(doc.check_in_date),
		               str(doc.check_out_date), doc.adults, doc.children,
		               flt(doc.amount_after_tax, 2), flt(doc.amount_before_tax, 2),
		               flt(doc.tax_amount, 2), doc.auto_price)
		self.assertEqual(after_state, before_state)
		self.assertEqual(self.night_snapshot(doc), before_nights)
		self.assertEqual(self.folio_snapshot(folio.name), before_folio)
		self.assertEqual(doc.channel_review_needed, 1)
		self.assertIn("Early check-in", doc.special_requests or "")
		self.assertIn("not applied: the stay is Checked In", doc.special_requests or "")
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": doc.name,
			"action_type": "channel.modify_needs_review"}))

		detail = api.reservation_detail(doc.name)
		self.assertEqual(detail["channel_review_needed"], 1)
		self.assertTrue(detail["warnings"]["channel_review_needed"])
		self.assertTrue(detail["channel_reviews"])
		self.assertEqual(detail["channel_reviews"][0]["action_type"], "channel.modify_needs_review")

		cleared = cm.clear_channel_review(doc.name)
		self.assertTrue(cleared["ok"])
		doc.reload()
		self.assertEqual(doc.channel_review_needed, 0)
		self.assertEqual(self.night_snapshot(doc), before_nights)
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": doc.name,
			"action_type": "channel.review_cleared"}))

	def test_readable_modify_leaves_checked_out_stay_unchanged_and_flags_review(self):
		h = self.hotel(inclusive=0)
		self.add_deluxe_room_type(h)
		booked, ref = self.book_today(h, notes="VIP pillow")
		doc, folio = self.check_out_real(h, booked)
		before_nights = self.night_snapshot(doc)
		before_folio = self.folio_snapshot(folio.name)
		before_state = (doc.status, doc.room_type, doc.room, str(doc.check_in_date),
		                str(doc.check_out_date), flt(doc.amount_after_tax, 2))

		conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
		# Same room type with changed dates + price (silently rewrote Checked Out on base)
		out = cm._apply_event(conn, self.modify_event(h, ref, ext_room="rt-std"))
		self.assertEqual(out["result"], "held_for_review")
		# Different room type change must also hold cleanly without mutating or throwing
		out_rt = cm._apply_event(conn, self.modify_event(h, ref, ext_room="rt-dlx"))
		self.assertEqual(out_rt["result"], "held_for_review")

		doc.reload()
		self.assertEqual((doc.status, doc.room_type, doc.room, str(doc.check_in_date),
		                  str(doc.check_out_date), flt(doc.amount_after_tax, 2)), before_state)
		self.assertEqual(self.night_snapshot(doc), before_nights)
		self.assertEqual(self.folio_snapshot(folio.name), before_folio)
		self.assertEqual(doc.channel_review_needed, 1)
		self.assertIn("VIP pillow", doc.special_requests or "")
		self.assertIn("not applied: the stay is Checked Out", doc.special_requests or "")
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": doc.name,
			"action_type": "channel.modify_needs_review"}))

	def test_readable_modify_leaves_no_show_and_cancelled_stays_unchanged_and_flags_review(self):
		for status in ("No Show", "Cancelled"):
			with self.subTest(status=status):
				h = self.hotel(inclusive=0)
				self.add_deluxe_room_type(h)
				doc, ref = self.book_today(h)
				frappe.db.set_value("Reservation", doc.name, "status", status)
				doc.reload()
				before_nights = self.night_snapshot(doc)
				before_state = (doc.status, doc.room_type, str(doc.check_in_date),
				                str(doc.check_out_date), flt(doc.amount_after_tax, 2))

				conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
				out = cm._apply_event(conn, self.modify_event(h, ref))
				self.assertEqual(out["result"], "held_for_review")

				doc.reload()
				self.assertEqual((doc.status, doc.room_type, str(doc.check_in_date),
				                  str(doc.check_out_date), flt(doc.amount_after_tax, 2)), before_state)
				self.assertEqual(self.night_snapshot(doc), before_nights)
				self.assertEqual(doc.channel_review_needed, 1)
				self.assertIn(f"not applied: the stay is {status}", doc.special_requests or "")
				self.assertTrue(frappe.db.exists("Agent Action Log", {
					"reference_doctype": "Reservation", "reference_name": doc.name,
					"action_type": "channel.modify_needs_review"}))

	def test_modify_with_unmapped_room_on_checked_in_stay_still_flags_review(self):
		h = self.hotel(inclusive=0)
		booked, ref = self.book_today(h)
		doc, _folio = self.check_in_real(h, booked)
		conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
		out = cm._apply_event(conn, self.modify_event(h, ref, ext_room="rt-unmapped"))
		self.assertEqual(out["result"], "held_for_review")
		doc.reload()
		self.assertEqual(doc.status, "Checked In")
		self.assertEqual(doc.channel_review_needed, 1)
		self.assertIn("rt-unmapped", doc.special_requests or "")
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": doc.name,
			"action_type": "channel.modify_needs_review"}))

	def test_cancel_leaves_checked_in_checked_out_and_no_show_stays_unchanged_and_flags_review(self):
		for mode in ("Checked In", "Checked Out", "No Show"):
			with self.subTest(status=mode):
				h = self.hotel(inclusive=0)
				booked, ref = self.book_today(h)
				folio = None
				if mode == "Checked In":
					doc, folio = self.check_in_real(h, booked)
				elif mode == "Checked Out":
					doc, folio = self.check_out_real(h, booked)
				else:
					frappe.db.set_value("Reservation", booked.name, "status", "No Show")
					doc = frappe.get_doc("Reservation", booked.name)
				before_nights = self.night_snapshot(doc)
				before_folio = self.folio_snapshot(folio.name) if folio else None

				conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
				out = cm._apply_event(conn, self.cancel_event(h, ref))
				self.assertEqual(out["result"], "held_for_review")
				self.assertEqual(out.get("reservations"), [])
				self.assertEqual(out.get("credit_notes"), [])

				doc.reload()
				self.assertEqual(doc.status, mode)
				self.assertEqual(self.night_snapshot(doc), before_nights)
				if folio:
					self.assertEqual(self.folio_snapshot(folio.name), before_folio)
				self.assertEqual(doc.channel_review_needed, 1)
				self.assertIn(f"not applied: the stay is {mode}", doc.special_requests or "")
				self.assertTrue(frappe.db.exists("Agent Action Log", {
					"reference_doctype": "Reservation", "reference_name": doc.name,
					"action_type": "channel.cancel_needs_review"}))

	def test_cancel_on_already_cancelled_stay_is_idempotent_without_review_flag(self):
		h = self.hotel(inclusive=0)
		doc, ref = self.book_today(h)
		conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
		first = cm._apply_event(conn, self.cancel_event(h, ref))
		self.assertEqual(first["result"], "cancelled")
		second = cm._apply_event(conn, self.cancel_event(h, ref))
		self.assertEqual(second["result"], "already_cancelled")
		doc.reload()
		self.assertEqual(doc.status, "Cancelled")
		self.assertEqual(doc.channel_review_needed, 0)
		self.assertFalse(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": doc.name,
			"action_type": "channel.cancel_needs_review"}))

	def test_clear_channel_review_enforces_roles(self):
		h = self.hotel(inclusive=0)
		booked, ref = self.book_today(h)
		doc, _folio = self.check_in_real(h, booked)
		conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
		cm._apply_event(conn, self.modify_event(h, ref))

		hk_email = f"hk-{frappe.generate_hash(length=6)}@example.test"
		frappe.get_doc({
			"doctype": "User", "email": hk_email, "first_name": "HK",
			"enabled": 1, "roles": [{"role": "Housekeeping"}],
		}).insert(ignore_permissions=True)
		fd_email = f"fd-{frappe.generate_hash(length=6)}@example.test"
		frappe.get_doc({
			"doctype": "User", "email": fd_email, "first_name": "FD",
			"enabled": 1, "roles": [{"role": "Front Desk"}],
		}).insert(ignore_permissions=True)
		try:
			frappe.set_user(hk_email)  # nosemgrep: frappe-setuser -- test RBAC check
			with self.assertRaises(frappe.PermissionError):
				cm.clear_channel_review(doc.name)
			frappe.set_user(fd_email)  # nosemgrep: frappe-setuser -- test RBAC check
			self.assertTrue(cm.clear_channel_review(doc.name)["ok"])
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- restore test user
		self.assertEqual(frappe.db.get_value("Reservation", doc.name, "channel_review_needed"), 0)

	def test_channel_review_needed_cannot_be_forged_or_cleared_outside_review_action(self):
		"""Reservation.channel_review_needed is server-owned: REST PUT
		(v1.update_doc), frappe.client.set_value, doc.save(), and client insert
		cannot clear or forge it for any role (including Front Desk, Hotel Admin,
		and Kamra Agent), even when sending forged flags. Only clear_channel_review
		clears it (and enforces roles + channel.review_cleared audit log), while
		unrelated edits to the flagged stay still succeed."""
		from frappe import client
		from frappe.api import v1

		h = self.hotel(inclusive=0)
		booked, ref = self.book_today(h)
		doc, _folio = self.check_in_real(h, booked)
		conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
		cm._apply_event(conn, self.modify_event(h, ref))
		doc.reload()
		self.assertEqual(doc.channel_review_needed, 1)

		users = {}
		for role in ("Front Desk", "Hotel Admin", "Kamra Agent"):
			email = f"{role.lower().replace(' ', '-')}-{frappe.generate_hash(length=6)}@example.test"
			frappe.get_doc({
				"doctype": "User", "email": email, "first_name": role,
				"enabled": 1, "roles": [{"role": role}],
			}).insert(ignore_permissions=True)
			users[role] = email

		try:
			# Kamra Agent is excluded from clear_channel_review
			frappe.set_user(users["Kamra Agent"])  # nosemgrep: frappe-setuser -- test RBAC check
			with self.assertRaises(frappe.PermissionError):
				cm.clear_channel_review(doc.name)

			# None of Front Desk, Hotel Admin, or Kamra Agent can clear channel_review_needed directly
			for role, email in users.items():
				with self.subTest(role=role, attack="direct_clear"):
					frappe.set_user(email)  # nosemgrep: frappe-setuser -- test RBAC check
					with self.assertRaises(frappe.PermissionError):
						client.set_value("Reservation", doc.name, "channel_review_needed", 0)
					with self.assertRaises(frappe.PermissionError):
						client.set_value("Reservation", doc.name, {
							"channel_review_needed": 0,
							"flags": {"kamra_channel_review": 1},
						})
					frappe.local.form_dict = frappe._dict({
						"data": json.dumps({"channel_review_needed": 0, "flags": {"kamra_channel_review": 1}})
					})
					with self.assertRaises(frappe.PermissionError):
						v1.update_doc("Reservation", doc.name)
					target = frappe.get_doc("Reservation", doc.name)
					target.channel_review_needed = 0
					with self.assertRaises(frappe.PermissionError):
						target.save()

			# Still flagged and zero forged review_cleared logs
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- inspect DB
			self.assertEqual(frappe.db.get_value("Reservation", doc.name, "channel_review_needed"), 1)
			self.assertFalse(frappe.db.exists("Agent Action Log", {
				"reference_doctype": "Reservation", "reference_name": doc.name,
				"action_type": "channel.review_cleared"}))

			# Unrelated edits to a flagged stay still succeed and keep the flag intact
			frappe.set_user(users["Front Desk"])  # nosemgrep: frappe-setuser -- test unrelated edit
			updated_note = (doc.special_requests or "") + "\nGuest contacted front desk"
			client.set_value("Reservation", doc.name, "special_requests", updated_note)
			self.assertEqual(frappe.db.get_value("Reservation", doc.name, "channel_review_needed"), 1)

			# Authorized clear_channel_review succeeds and logs channel.review_cleared
			self.assertTrue(cm.clear_channel_review(doc.name)["ok"])
			self.assertEqual(frappe.db.get_value("Reservation", doc.name, "channel_review_needed"), 0)
			self.assertTrue(frappe.db.exists("Agent Action Log", {
				"reference_doctype": "Reservation", "reference_name": doc.name,
				"action_type": "channel.review_cleared"}))

			# Forging 0 -> 1 on an existing stay or inserting with channel_review_needed=1 is refused
			for role in ("Front Desk", "Kamra Agent"):
				with self.subTest(role=role, attack="forge_set"):
					frappe.set_user(users[role])  # nosemgrep: frappe-setuser -- test RBAC check
					with self.assertRaises(frappe.PermissionError):
						client.set_value("Reservation", doc.name, "channel_review_needed", 1)
					frappe.local.form_dict = frappe._dict({"data": json.dumps({"channel_review_needed": 1})})
					with self.assertRaises(frappe.PermissionError):
						v1.update_doc("Reservation", doc.name)

			frappe.set_user(users["Front Desk"])  # nosemgrep: frappe-setuser -- test insert forge
			with self.assertRaises(frappe.PermissionError):
				client.insert(json.dumps({
					"doctype": "Reservation", "property": h.property, "room_type": h.room_type,
					"guest": self.guest(), "status": "Confirmed",
					"check_in_date": day(5), "check_out_date": day(6), "adults": 2,
					"channel_review_needed": 1,
				}))
		finally:
			frappe.local.form_dict = frappe._dict()
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- restore test user

	def test_update_inhouse_res_defaults_to_n_and_refuses_y_until_bounds_exist(self):
		"""OPERA Cloud 26.2 OXI UPDATE INHOUSE RES is configurable per Channel
		Manager Connection, defaults to N, and refuses Y on save with a clear
		message citing arrival-date, room-type, billed-night, and assigned-room
		bounds until OXI Y is implemented."""
		meta = frappe.get_meta("Channel Manager Connection")
		field = meta.get_field("update_inhouse_res")
		self.assertIsNotNone(field, "Channel Manager Connection must define update_inhouse_res")
		self.assertEqual(field.fieldtype, "Select")
		self.assertEqual(field.default, "N")
		self.assertEqual([o.strip() for o in (field.options or "").splitlines() if o.strip()], ["N", "Y"])

		h = self.hotel(inclusive=0)
		conn = frappe.get_doc("Channel Manager Connection", h.conn.name)
		self.assertEqual(conn.update_inhouse_res, "N")

		conn.update_inhouse_res = "N"
		conn.save(ignore_permissions=True)

		conn.update_inhouse_res = "Y"
		with self.assertRaises(frappe.ValidationError) as ctx:
			conn.save(ignore_permissions=True)
		msg = str(ctx.exception).lower()
		for term in ("arrival", "room", "billed"):
			self.assertIn(term, msg)

	def _cleanup_committed_property(self, prop: str, conn_name: str, guest_names: list[str],
	                                error_cutoff: str) -> None:
		frappe.db.rollback()
		folios = frappe.get_all("Folio", filters={"property": prop}, pluck="name")
		for f in folios:
			frappe.db.delete("Folio Charge", {"parent": f})
			frappe.db.delete("Folio Payment", {"parent": f})
		res_names = frappe.get_all("Reservation", filters={"property": prop}, pluck="name")
		for r in res_names:
			frappe.db.delete("Reservation Night Rate", {"parent": r})
		frappe.db.delete("Channel ARI Outbox", {"connection": conn_name})
		frappe.db.delete("Channel Room Mapping", {"connection": conn_name})
		frappe.db.delete("Channel OTA Tax Rule", {"parent": conn_name})
		frappe.db.delete("Channel OTA Tax Basis", {"parent": conn_name})
		for dt in frappe.get_all("DocType", filters={"module": "Kamra", "istable": 0}, pluck="name"):
			if dt != "Property" and frappe.get_meta(dt).has_field("property"):
				frappe.db.delete(dt, {"property": prop})
		for g in guest_names:
			frappe.db.delete("Guest", {"name": g})
		frappe.db.delete("Property", {"name": prop})
		frappe.db.delete("Error Log", {"creation": (">=", error_cutoff), "method": ("like", "OTA webhook%")})
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- clean up committed concurrency test fixture

	def _check_in_in_other_connection(self, site: str, sites_path: str, res_name: str, room: str) -> None:
		import threading

		errors: list[BaseException] = []

		def worker():
			frappe.init(site, sites_path=sites_path)
			frappe.connect()
			try:
				frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test concurrency worker
				from kamra import api, folio as folio_mod

				api.check_in(res_name, room=room)
				doc = frappe.get_doc("Reservation", res_name)
				folio_mod.post_remaining_nights(doc)
				frappe.db.commit()  # nosemgrep: frappe-manual-commit -- commit check-in on second DB connection
			except BaseException as exc:
				frappe.db.rollback()
				errors.append(exc)
			finally:
				frappe.destroy()

		t = threading.Thread(target=worker)
		t.start()
		t.join(timeout=30)
		self.assertFalse(t.is_alive(), "Concurrent check-in thread timed out")
		if errors:
			raise errors[0]

	def test_concurrent_check_in_and_ota_modify_under_repeatable_read_1020_retries_and_flags_review(self):
		"""Real two-connection MariaDB 11.8 REPEATABLE-READ snapshot race:
		Connection A opens its read snapshot before Connection B commits
		check_in. Both process_webhook_events and synchronous webhook hit
		MariaDB 1020 on FOR UPDATE, roll back to a fresh snapshot, see
		Checked In, leave the stay/folio untouched, and flag review instead
		of silently dropping or returning 500."""
		iso = frappe.db.sql("SELECT @@transaction_isolation, @@innodb_snapshot_isolation")[0]
		self.assertEqual(iso, ("REPEATABLE-READ", 1))

		for mode in ("worker", "sync_webhook"):
			with self.subTest(mode=mode):
				cutoff = frappe.utils.now()
				h = self.hotel(inclusive=0)
				booked, ref = self.book_today(h, notes="Before race")
				room = frappe.db.get_value("Room", {"property": h.property, "room_type": h.room_type}, "name")
				guests = [booked.guest]
				frappe.db.commit()  # nosemgrep: frappe-manual-commit -- publish fixture to second DB connection
				self.addCleanup(self._cleanup_committed_property, h.property, h.conn.name, guests, cutoff)

				# Open Connection A's REPEATABLE-READ snapshot BEFORE check-in commits
				frappe.db.sql("SELECT name FROM `tabChannel Manager Connection` WHERE name = %s", (h.conn.name,))
				self.assertEqual(frappe.db.get_value("Reservation", booked.name, "status"), "Confirmed")

				# Connection B checks the guest in and commits
				self._check_in_in_other_connection(frappe.local.site, frappe.local.sites_path, booked.name, room)

				# Connection A still holds the stale pre-check-in snapshot
				self.assertEqual(frappe.db.get_value("Reservation", booked.name, "status"), "Confirmed")

				mod_payload = self.channex_payload(h, [Decimal("295000"), Decimal("295000")], n=0, report="exclusive")
				mod_payload["payload"].update(
					status="modified", ota_reservation_code=ref, ota_name="Booking.com", notes="Race modify"
				)
				if mode == "worker":
					cm.process_webhook_events(h.conn.name, mod_payload)
				else:
					out = cm.webhook(connection=h.conn.name, secret="basis-hook", **mod_payload)
					self.assertTrue(out["ok"])
					self.assertEqual(out["results"][0]["result"], "held_for_review")
					frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist sync webhook result in concurrency test

				doc = frappe.get_doc("Reservation", booked.name)
				self.assertEqual(doc.status, "Checked In")
				self.assertEqual(doc.room, room)
				self.assertEqual(flt(doc.amount_after_tax, 2), 177000.0)
				self.assertEqual(doc.channel_review_needed, 1)
				self.assertIn("not applied: the stay is Checked In", doc.special_requests or "")
				self.assertTrue(frappe.db.exists("Agent Action Log", {
					"reference_doctype": "Reservation", "reference_name": doc.name,
					"action_type": "channel.modify_needs_review"}))
				failed_logs = frappe.db.count("Error Log", {
					"creation": (">=", cutoff),
					"method": ("like", f"OTA webhook apply failed: {h.conn.name}%"),
				})
				self.assertEqual(failed_logs, 0)

	def test_concurrent_check_in_and_ota_cancel_under_repeatable_read_1020_retries_and_flags_review(self):
		"""Same real two-connection MariaDB 11.8 REPEATABLE-READ 1020 race for
		an inbound OTA cancel arriving as check_in commits: rolls back, retries
		on a fresh snapshot, leaves stay Checked In, and flags review."""
		iso = frappe.db.sql("SELECT @@transaction_isolation, @@innodb_snapshot_isolation")[0]
		self.assertEqual(iso, ("REPEATABLE-READ", 1))

		cutoff = frappe.utils.now()
		h = self.hotel(inclusive=0)
		booked, ref = self.book_today(h, notes="Before cancel race")
		room = frappe.db.get_value("Room", {"property": h.property, "room_type": h.room_type}, "name")
		guests = [booked.guest]
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- publish fixture to second DB connection
		self.addCleanup(self._cleanup_committed_property, h.property, h.conn.name, guests, cutoff)

		frappe.db.sql("SELECT name FROM `tabChannel Manager Connection` WHERE name = %s", (h.conn.name,))
		self.assertEqual(frappe.db.get_value("Reservation", booked.name, "status"), "Confirmed")

		self._check_in_in_other_connection(frappe.local.site, frappe.local.sites_path, booked.name, room)
		self.assertEqual(frappe.db.get_value("Reservation", booked.name, "status"), "Confirmed")

		cancel_payload = {
			"event": "booking",
			"payload": {
				"status": "cancelled",
				"ota_reservation_code": ref,
				"ota_name": "Booking.com",
				"rooms": [{"room_type_id": "rt-std", "days": {}}],
			},
		}
		cm.process_webhook_events(h.conn.name, cancel_payload)

		doc = frappe.get_doc("Reservation", booked.name)
		self.assertEqual(doc.status, "Checked In")
		self.assertEqual(doc.room, room)
		self.assertEqual(doc.channel_review_needed, 1)
		self.assertIn("not applied: the stay is Checked In", doc.special_requests or "")
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"reference_doctype": "Reservation", "reference_name": doc.name,
			"action_type": "channel.cancel_needs_review"}))
		failed_logs = frappe.db.count("Error Log", {
			"creation": (">=", cutoff),
			"method": ("like", f"OTA webhook apply failed: {h.conn.name}%"),
		})
		self.assertEqual(failed_logs, 0)

	def test_exhausted_snapshot_conflicts_requeue_webhook_instead_of_dropping(self):
		"""If snapshot conflicts persist past the inline retry cap,
		process_webhook_events re-enqueues itself rather than silently
		dropping the OTA event."""
		h = self.hotel(inclusive=0)
		payload = self.channex_payload(h, Decimal("177000"), n=0, report="exclusive")
		with patch.object(cm, "_apply_event", side_effect=frappe.QueryDeadlockError("1020")), \
		     patch.object(frappe.db, "rollback", return_value=None), \
		     patch.object(frappe, "enqueue") as enqueue_mock:
			cm.process_webhook_events(h.conn.name, payload)
		self.assertTrue(enqueue_mock.called, "Expected webhook event to be re-queued on retry exhaustion")
		args, kwargs = enqueue_mock.call_args
		self.assertEqual(args[0], "kamra.channel_manager.process_webhook_events")
		self.assertEqual(kwargs.get("connection"), h.conn.name)
		self.assertEqual(kwargs.get("payload"), payload)
