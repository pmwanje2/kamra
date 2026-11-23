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

Rates go out with the currency's ISO 4217 decimals: none for UGX, three
for KWD, two for USD.

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
	          price=150000, tax=18, provider: str = "Channex", plans=()) -> frappe._dict:
		tag = frappe.generate_hash(length=6).upper()
		prop = frappe.get_doc({"doctype": "Property", "property_name": "TEST BASIS " + tag,
			"country": country, "currency": currency, "rates_include_tax": inclusive,
			"gst_rate_low": tax, "gst_rate_high": tax}).insert(ignore_permissions=True).name
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
				"rate_plan": plan}).insert(ignore_permissions=True)
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

	def charged(self, h, pushed) -> Decimal:
		"""What the guest pays the OTA for a night offered at `pushed`, its
		extranet's tax setting matching the property: the price itself
		where it includes tax, the price plus the room tax where not."""
		price = Decimal(str(pushed))
		return price if h.inclusive else price * (1 + h.tax / 100)

	# -- the desk and the inbound booking ------------------------------------

	def guest(self) -> str:
		return frappe.get_doc({"doctype": "Guest", "first_name": "Basis",
			"last_name": frappe.generate_hash(length=6)}).insert(ignore_permissions=True).name

	def desk_booking(self, h, n: int = NIGHT, rate_plan=None):
		return frappe.get_doc({"doctype": "Reservation", "property": h.property,
			"room_type": h.room_type, "guest": self.guest(), "status": "Confirmed",
			"check_in_date": day(n), "check_out_date": day(n + 1), "adults": 2,
			"rate_plan": rate_plan}).insert(ignore_permissions=True)

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

	def channex_booking(self, h, charged, n: int = NIGHT):
		ref = "BDC-" + frappe.generate_hash(length=8)
		self.deliver(h, {"event": "booking", "payload": {
			"status": "new", "ota_reservation_code": ref, "ota_name": "Booking.com",
			"arrival_date": day(n), "departure_date": day(n + 1), "currency": h.currency,
			"amount": str(charged), "customer": {"name": "Ota", "surname": "Guest"},
			"rooms": [{"room_type_id": "rt-std", "checkin_date": day(n), "checkout_date": day(n + 1),
			           "occupancy": {"adults": 2, "children": 0},
			           "days": {day(n): str(charged)}}]}})
		return self.received(h, ref)

	def aiosell_booking(self, h, pushed, charged, n: int = NIGHT):
		ref = "AIO-" + frappe.generate_hash(length=8)
		self.deliver(h, {"action": "book", "hotelCode": "demo", "channel": "Booking.com",
			"bookingId": ref, "checkin": day(n), "checkout": day(n + 1),
			"guest": {"firstName": "Ota", "lastName": "Guest"},
			"amount": {"amountAfterTax": float(charged), "currency": h.currency},
			"rooms": [{"roomCode": "rt-std", "occupancy": {"adults": 2, "children": 0},
			           "prices": [{"date": day(n), "sellRate": float(pushed)}]}]})
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
		h = self.hotel(inclusive=1)
		pushed = self.channex_push(h)
		desk = self.desk_booking(h)
		ota = self.channex_booking(h, self.charged(h, pushed))
		self.assertBillsLikeTheDesk(ota, desk)

	def test_round_trip_on_a_tax_exclusive_property_bills_like_the_desk(self):
		h = self.hotel(inclusive=0)
		pushed = self.channex_push(h)
		desk = self.desk_booking(h)
		ota = self.channex_booking(h, self.charged(h, pushed))
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
