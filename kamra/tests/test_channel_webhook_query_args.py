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

	def test_json_post_with_no_connection_is_a_validation_error(self):
		"""Neither the URL nor the body names a connection: a clean refusal,
		not a TypeError out of Frappe's dispatcher."""
		with self.assertRaises(frappe.ValidationError):
			post({}, channex_booking(f"CX-NONE-{self.tag}", self.inc.room, "150000",
			                         INCLUSIVE_TAXES), secret=self.inc.secret)
