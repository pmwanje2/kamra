"""The channel booking webhook takes its connection from the query string.

Channex is registered at
/api/method/kamra.channel_manager.webhook?connection=<name> and posts its
booking as Content-Type: application/json. Frappe builds a JSON request's
arguments from the body alone (frappe.app.make_form_dict ignores
request.args when request.is_json), so the documented ?connection= never
reached webhook() and every Channex booking died with HTTP 500 TypeError.

These tests drive Frappe's own request plumbing rather than calling
webhook() with keyword arguments: a Werkzeug request, the real
frappe.app.make_form_dict, then frappe.handler.execute_cmd, which is the
path /api/method/<cmd> takes once init_request has run.
"""

import json
from contextlib import contextmanager
from unittest.mock import patch

import frappe
import frappe.app
import frappe.handler
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

CMD = "kamra.channel_manager.webhook"


def channex_booking(ref: str, room: str, amount: str, taxes: list[dict]) -> dict:
	"""A Channex booking webhook body, as Neon posted it live on PR #35."""
	arrival, departure = str(add_days(nowdate(), 3)), str(add_days(nowdate(), 4))
	return {"event": "booking", "payload": {
		"status": "new", "ota_reservation_code": ref, "ota_name": "Booking.com",
		"arrival_date": arrival, "departure_date": departure, "currency": "UGX",
		"amount": amount, "customer": {"name": "Amina", "surname": "Webhook"},
		"rooms": [{"room_type_id": room, "checkin_date": arrival,
		           "checkout_date": departure, "occupancy": {"adults": 2, "children": 0},
		           "amount": amount, "days": {arrival: amount}, "taxes": taxes}]}}


INCLUSIVE_TAXES = [{"is_inclusive": True, "name": "VAT", "total_price": "22881.36",
                    "type": "tax"}]
EXCLUSIVE_TAXES = [{"is_inclusive": False, "name": "VAT", "total_price": "27000.00",
                    "type": "tax"}]
MIXED_TAXES = [
	{"is_inclusive": False, "name": "Value Added Tax (VAT)", "total_price": "27000.00",
	 "type": "Value Added Tax (VAT)"},
	{"is_inclusive": False, "name": "City Tax", "total_price": "5000.00", "type": "city_tax"},
	{"is_inclusive": False, "name": "Cleaning fee", "total_price": "20000.00", "type": "fee"},
	{"is_inclusive": False, "is_withheld": True, "name": "VAT (Withheld Tax)",
	 "total_price": "27000.00", "type": "tax"},
]


@contextmanager
def guest_request(query: dict, body: dict, *, secret: str | None = None,
                  content_type: str = "application/json"):
	"""One inbound POST as Frappe sees it: the request object is bound,
	form_dict is built by Frappe itself, and the caller is Guest."""
	headers = {"X-Webhook-Secret": secret} if secret is not None else {}
	environ = EnvironBuilder(method="POST", path=f"/api/method/{CMD}",
	                         query_string=query, data=json.dumps(body),
	                         content_type=content_type, headers=headers).get_environ()
	request = Request(environ)
	form_dict = frappe.local.form_dict
	frappe.local.request = request
	try:
		# set_user resets form_dict, so the session comes first - as in a
		# real request, where it exists before the arguments are built
		frappe.set_user("Guest")  # nosemgrep: frappe-setuser -- test fixture: the webhook is an allow_guest endpoint
		frappe.app.make_form_dict(request)
		with patch.object(frappe.db, "commit", return_value=None):
			yield request
	finally:
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		frappe.local.form_dict = form_dict
		del frappe.local.request


def post(query: dict, body: dict, **kwargs):
	with guest_request(query, body, **kwargs):
		return frappe.handler.execute_cmd(CMD)


def respond(query: dict, body: dict, **kwargs) -> tuple[int, dict]:
	"""What the caller receives: the HTTP status and JSON body built by Frappe's
	own exception handler (frappe.app.handle_exception, which also turns a
	Guest's DoesNotExistError into a doctype-access 403). Only the per-request
	error id is dropped. Response state starts empty, as frappe.init leaves it
	for a fresh request."""
	saved = (frappe.local.response, frappe.local.message_log,
	         getattr(frappe.local, "debug_log", []), frappe.flags.error_message)
	frappe.local.response = frappe._dict({"docs": []})
	frappe.local.message_log, frappe.local.debug_log = [], []
	frappe.flags.error_message = None
	try:
		with guest_request(query, body, **kwargs):
			try:
				return 200, {"message": frappe.handler.execute_cmd(CMD)}
			except Exception as exc:
				response = frappe.app.handle_exception(exc)
				return response.status_code, without_error_ids(json.loads(response.get_data()))
	finally:
		(frappe.local.response, frappe.local.message_log, frappe.local.debug_log,
		 frappe.flags.error_message) = saved


def without_error_ids(body: dict) -> dict:
	messages = [json.loads(m) for m in json.loads(body.get("_server_messages") or "[]")]
	for m in messages:
		m.pop("__frappe_exc_id", None)
	return {**body, "_server_messages": messages}


@contextmanager
def connection_lookups():
	"""Every query the call runs against the connections or the stored secrets:
	the work whose shape and count could tell one refusal from another."""
	seen = []
	real = frappe.db.sql

	def spy(query, *args, **kwargs):
		text = str(query)
		if "tabChannel Manager Connection" in text or "__Auth" in text:
			seen.append(text)
		return real(query, *args, **kwargs)

	with patch.object(frappe.db, "sql", side_effect=spy):
		yield seen


class ChannelWebhookQueryArgsTestCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		frappe.local.lang = "en"
		self.tag = frappe.generate_hash(length=6).upper()
		self.inc = self._property("Inclusive", rates_include_tax=1, secret="hook-inc")
		self.exc = self._property("Exclusive", rates_include_tax=0, secret="hook-exc")

	def _property(self, label: str, *, rates_include_tax: int, secret: str | None):
		prop = frappe.get_doc({"doctype": "Property",
			"property_name": f"TEST HOOK {label} {self.tag}", "country": "Uganda",
			"currency": "UGX", "rates_include_tax": rates_include_tax, "gst_mode": "Fixed",
			"gst_rate_low": 18, "gst_rate_high": 18}).insert(ignore_permissions=True).name
		room_type = frappe.get_doc({"doctype": "Room Type", "property": prop,
			"room_type_code": f"STD{label[:3].upper()}{self.tag}",
			"room_type_name": "Standard Room", "base_price": 150000,
			"adults_capacity": 2}).insert(ignore_permissions=True).name
		for i in range(2):
			frappe.get_doc({"doctype": "Room", "property": prop, "room_type": room_type,
				"room_number": f"{label[:3].upper()}{self.tag}-{i + 1}"}
			).insert(ignore_permissions=True)
		conn = frappe.get_doc({"doctype": "Channel Manager Connection", "property": prop,
			"provider": "Channex", "active": 1, "endpoint": "http://channex.test/api/v1",
			"external_property_id": f"cx-{label.lower()}-{self.tag}",
			"webhook_secret": secret, "ari_batch_seconds": 0}).insert(ignore_permissions=True)
		room = f"cx-rt-{label.lower()}-{self.tag}"
		frappe.get_doc({"doctype": "Channel Room Mapping", "connection": conn.name,
			"room_type": room_type, "external_room_id": room}).insert(ignore_permissions=True)
		return frappe._dict(property=prop, room_type=room_type, connection=conn.name,
		                    room=room, secret=secret)

	def booked(self, prop, ref: str):
		name = frappe.db.get_value("Reservation", {"property": prop.property, "ota_ref": ref})
		self.assertTrue(name, f"booking {ref} did not land on {prop.property}")
		return frappe.get_doc("Reservation", name)

	def test_documented_json_post_books_each_tax_case(self):
		"""Inclusive, exclusive, and the mixed VAT/city_tax/fee/withheld case,
		each posted exactly as Channex does: JSON body, ?connection= in the URL."""
		cases = (
			("inclusive", self.inc, "150000", INCLUSIVE_TAXES),
			("exclusive", self.exc, "150000", EXCLUSIVE_TAXES),
			("mixed", self.exc, "150000", MIXED_TAXES),
		)
		for label, prop, amount, taxes in cases:
			with self.subTest(label):
				ref = f"CX-{label.upper()}-{self.tag}"
				out = post({"connection": prop.connection},
				           channex_booking(ref, prop.room, amount, taxes), secret=prop.secret)
				self.assertTrue(out["ok"])
				self.assertEqual(out["results"][0]["result"], "booked")
				doc = self.booked(prop, ref)
				self.assertEqual(out["results"][0]["reservation"], doc.name)
				self.assertEqual(doc.room_type, prop.room_type)
				self.assertEqual(doc.source, "OTA")

	def test_text_plain_post_reaches_the_webhook(self):
		"""Control: when Frappe does merge request.args (a non-JSON body), the
		same harness books. A failure above is the JSON path, not the harness."""
		ref = f"CX-PLAIN-{self.tag}"
		out = post({"connection": self.inc.connection},
		           channex_booking(ref, self.inc.room, "150000", INCLUSIVE_TAXES),
		           secret=self.inc.secret, content_type="text/plain")
		self.assertEqual(out["results"][0]["result"], "booked")
		self.booked(self.inc, ref)

	def test_secret_in_the_json_body_still_authenticates(self):
		"""The documented second channel for the secret: `secret` in the payload."""
		ref = f"CX-BODYSECRET-{self.tag}"
		body = channex_booking(ref, self.inc.room, "150000", INCLUSIVE_TAXES)
		body["secret"] = self.inc.secret
		out = post({"connection": self.inc.connection}, body)
		self.assertEqual(out["results"][0]["result"], "booked")
		self.booked(self.inc, ref)

	def test_the_url_connection_wins_over_a_body_key(self):
		"""One URL per connection: a `connection` key inside a provider's JSON
		body never redirects the booking to another connection."""
		ref = f"CX-URLWINS-{self.tag}"
		body = channex_booking(ref, self.inc.room, "150000", INCLUSIVE_TAXES)
		body["connection"] = self.exc.connection
		out = post({"connection": self.inc.connection}, body, secret=self.inc.secret)
		self.assertEqual(out["results"][0]["result"], "booked")
		self.booked(self.inc, ref)
		self.assertFalse(frappe.db.exists("Reservation",
		                                  {"property": self.exc.property, "ota_ref": ref}))

	def test_json_post_with_a_wrong_secret_is_refused(self):
		ref = f"CX-BADSECRET-{self.tag}"
		with self.assertRaises(frappe.PermissionError):
			post({"connection": self.inc.connection},
			     channex_booking(ref, self.inc.room, "150000", INCLUSIVE_TAXES),
			     secret="not-the-secret")
		self.assertFalse(frappe.db.exists("Reservation", {"ota_ref": ref}))

	def test_json_post_without_a_secret_header_is_refused(self):
		ref = f"CX-NOSECRET-{self.tag}"
		with self.assertRaises(frappe.PermissionError):
			post({"connection": self.inc.connection},
			     channex_booking(ref, self.inc.room, "150000", INCLUSIVE_TAXES))
		self.assertFalse(frappe.db.exists("Reservation", {"ota_ref": ref}))

	def test_json_post_to_a_connection_with_no_secret_is_refused(self):
		"""No secret configured means nothing to authenticate against."""
		bare = self._property("Bare", rates_include_tax=1, secret=None)
		ref = f"CX-UNSET-{self.tag}"
		for sent in ("", "guess"):
			with self.subTest(sent=sent), self.assertRaises(frappe.PermissionError):
				post({"connection": bare.connection},
				     channex_booking(ref, bare.room, "150000", INCLUSIVE_TAXES), secret=sent)
		self.assertFalse(frappe.db.exists("Reservation", {"ota_ref": ref}))

	def test_json_post_with_no_connection_is_refused(self):
		"""Neither the URL nor the body names a connection: the webhook's own
		refusal, not a TypeError out of Frappe's dispatcher."""
		with self.assertRaisesRegex(frappe.PermissionError, "Webhook secret mismatch"):
			post({}, channex_booking(f"CX-NONE-{self.tag}", self.inc.room, "150000",
			                         INCLUSIVE_TAXES), secret=self.inc.secret)

	# --- an unauthenticated caller learns nothing about the connections -------
	# Neon, PR #40 R1: a JSON object as `connection` reached frappe.get_doc as an
	# ORM filter (the module's `from __future__ import annotations` makes Frappe
	# skip the type check), and unknown / active / inactive connections answered
	# differently - a blind oracle over every non-password column.

	def inactive(self):
		dead = self._property("Dead", rates_include_tax=1, secret="hook-dead")
		frappe.db.set_value("Channel Manager Connection", dead.connection, "active", 0)
		return dead

	def the_refusal(self):
		"""The reference answer: an active connection and a wrong secret."""
		ref = f"CX-REFUSED-{self.tag}"
		with connection_lookups() as lookups:
			status, body = respond({"connection": self.inc.connection},
			                       channex_booking(ref, self.inc.room, "150000", INCLUSIVE_TAXES),
			                       secret="not-the-secret")
		self.assertEqual(status, 403)
		self.assertEqual(body["exc_type"], "PermissionError")
		self.assertEqual([m["message"] for m in body["_server_messages"]],
		                 ["Webhook secret mismatch."])
		return (status, body), lookups

	def test_every_refusal_looks_the_same_whatever_the_connection_state(self):
		"""Unknown, inactive, no secret configured, wrong or missing secret: one
		status, one body, and the same lookups in the same order."""
		refusal, lookups = self.the_refusal()
		bare = self._property("Bare", rates_include_tax=1, secret=None)
		dead = self.inactive()
		cases = (
			("unknown connection", f"CMC-NOPE-{self.tag}", self.inc.room, "guess"),
			("active, no secret sent", self.inc.connection, self.inc.room, None),
			("no secret configured", bare.connection, bare.room, "guess"),
			("inactive, wrong secret", dead.connection, dead.room, "guess"),
			("inactive, right secret", dead.connection, dead.room, dead.secret),
		)
		for label, connection, room, secret in cases:
			with self.subTest(label):
				ref = f"CX-{label.upper().replace(' ', '').replace(',', '')}-{self.tag}"
				with connection_lookups() as seen:
					got = respond({"connection": connection},
					              channex_booking(ref, room, "150000", INCLUSIVE_TAXES),
					              secret=secret)
				self.assertEqual(got, refusal)
				self.assertEqual(seen, lookups, "a refusal ran different lookups")
				self.assertFalse(frappe.db.exists("Reservation", {"ota_ref": ref}))
		with self.subTest("no connection at all"):
			self.assertEqual(respond({}, channex_booking(f"CX-NOCONN-{self.tag}", self.inc.room,
			                                             "150000", INCLUSIVE_TAXES),
			                         secret=self.inc.secret), refusal)

	def test_a_filter_object_as_connection_never_reaches_the_orm(self):
		"""Neon's probes, posted as JSON with no ?connection= and no secret: each
		must get the plain refusal before anything is looked up."""
		refusal, _ = self.the_refusal()
		self.inactive()
		probes = (
			("like, matching", {"external_property_id": ["like", f"cx-inclusive-{self.tag}%"]}),
			("like, missing", {"external_property_id": ["like", "cx-nowhere-%"]}),
			("property like", {"property": ["like", "TEST HOOK Inclusive%"]}),
			("provider, matching", {"provider": "Channex"}),
			("provider, missing", {"provider": "STAAH"}),
			("inactive", {"active": 0}),
			("list", [self.inc.connection]),
			("number", 1),
		)
		for label, probe in probes:
			with self.subTest(label):
				body = channex_booking(f"CX-PROBE-{self.tag}", self.inc.room, "150000",
				                       INCLUSIVE_TAXES)
				body["connection"] = probe
				with connection_lookups() as seen:
					got = respond({}, body)
				self.assertEqual(got, refusal)
				self.assertEqual(seen, [], "a filter object reached the connection table")

	def test_a_non_ascii_secret_is_refused_not_a_server_error(self):
		"""hmac.compare_digest refuses non-ASCII str with a TypeError, and only
		when the connection has a secret: a 500 there is one more oracle."""
		refusal, _ = self.the_refusal()
		ref = f"CX-NONASCII-{self.tag}"
		with self.subTest("header"):
			self.assertEqual(respond({"connection": self.inc.connection},
			                         channex_booking(ref, self.inc.room, "150000", INCLUSIVE_TAXES),
			                         secret="h\xe9"), refusal)
		with self.subTest("body"):
			body = channex_booking(ref, self.inc.room, "150000", INCLUSIVE_TAXES)
			body["secret"] = "hé-ā"
			self.assertEqual(respond({"connection": self.inc.connection}, body), refusal)
		self.assertFalse(frappe.db.exists("Reservation", {"ota_ref": ref}))
