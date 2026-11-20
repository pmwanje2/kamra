"""A quotation for one booking, printable and emailed to the booker (M17).

OPERA: a confirmation / proposal letter is generated from the reservation
and emailed to the address on the booker's profile. Here the quotation is
the booking's own price (its locked nightly rates - what the folio bills)
with the cancellation terms get_quote promises; "Email to guest" sends it
through Frappe's native email (a Communication on the Reservation, the PDF
attached), only ever to the address on record, and refuses with a stable
id when the site cannot send mail."""

from __future__ import annotations

from unittest.mock import patch

import frappe

from kamra import api
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	ensure_user,
	make_guest,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

DESK = "ut.quote.desk@test.local"
NIGHT = "ut.quote.night@test.local"
GUEST_EMAIL = "booker@guest.test"
ACCOUNT = "UT Quote Outgoing"


def _blank_pdf() -> bytes:
	"""A real one-page PDF: Frappe's File checks what it stores."""
	from io import BytesIO

	from pypdf import PdfWriter
	out = BytesIO()
	w = PdfWriter()
	w.add_blank_page(width=72, height=72)
	w.write(out)
	return out.getvalue()


class BookingQuotationTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY)
		make_property(PROP_A, business_date=TODAY)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.room = make_room(PROP_A, self.rt, "101")
		ensure_user(DESK, ["Front Desk"])
		ensure_user(NIGHT, ["Night Auditor"])
		guest = make_guest("UT Quote", email=GUEST_EMAIL)
		self.res = make_reservation(PROP_A, self.rt, d(5), d(7), room=self.room, guest=guest)
		# no outgoing account unless a test adds one
		frappe.db.delete("Email Account")
		self._forget_accounts()

	def tearDown(self):
		super().tearDown()
		self._forget_accounts()

	def _forget_accounts(self):
		for cache in ("outgoing_email_account", "incoming_email_account"):
			if hasattr(frappe.local, cache):
				delattr(frappe.local, cache)

	def outgoing_account(self):
		frappe.get_doc({
			"doctype": "Email Account", "email_account_name": ACCOUNT,
			"email_id": "frontdesk@hotel.test", "service": "",
			"smtp_server": "localhost", "smtp_port": 25,
			"enable_outgoing": 1, "default_outgoing": 1,
			"no_smtp_authentication": 1, "enable_incoming": 0,
		}).insert(ignore_permissions=True)
		self._forget_accounts()

	def as_user(self, user, fn, *args, **kwargs):
		frappe.set_user(user)  # nosemgrep: frappe-setuser -- acting as the persona
		try:
			return fn(*args, **kwargs)
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- restore

	def send(self, user=DESK, **extra):
		"""The HTTP path: frappe.call drops arguments the method does not take."""
		with patch("frappe.utils.pdf.get_pdf", return_value=_blank_pdf()) as pdf:
			out = self.as_user(user, frappe.call, "kamra.api.email_booking_quotation",
			                   reservation=self.res.name, **extra)
		return out, pdf

	def comms(self):
		return frappe.get_all(
			"Communication",
			filters={"reference_doctype": "Reservation", "reference_name": self.res.name},
			fields=["name", "recipients", "cc", "bcc", "sent_or_received",
			        "communication_medium", "has_attachment", "subject"])

	# --- the print view -------------------------------------------------

	def test_the_quotation_is_the_booked_price(self):
		q = self.as_user(DESK, api.booking_quotation, self.res.name)
		self.assertEqual(q["booking"]["name"], self.res.name)
		self.assertEqual(q["booking"]["check_in_date"], d(5))
		self.assertEqual(q["booking"]["check_out_date"], d(7))
		self.assertEqual([n["rate"] for n in q["nights"]], [1000, 1000])
		self.assertAlmostEqual(q["totals"]["total"], float(self.res.amount_after_tax), 2)
		self.assertIn("free_now", q["cancellation"])
		self.assertEqual(q["property"]["property_name"], PROP_A)
		self.assertEqual(q["email_to"], GUEST_EMAIL)

	# --- email ----------------------------------------------------------

	def test_email_goes_only_to_the_bookers_address(self):
		self.outgoing_account()
		out, _pdf = self.send(recipients="someone@else.test", cc="cc@else.test",
		                      bcc="bcc@else.test")
		self.assertEqual(out["sent_to"], GUEST_EMAIL)
		[comm] = self.comms()
		self.assertEqual(comm.recipients, GUEST_EMAIL)
		self.assertFalse(comm.cc)
		self.assertFalse(comm.bcc)
		queued = frappe.get_all("Email Queue Recipient",
		                        filters={"parent": ("in", frappe.get_all(
		                            "Email Queue", filters={"communication": comm.name},
		                            pluck="name"))},
		                        pluck="recipient")
		self.assertEqual(queued, [GUEST_EMAIL])

	def test_a_communication_is_logged_on_the_reservation_with_the_pdf(self):
		self.outgoing_account()
		_out, pdf = self.send()
		[comm] = self.comms()
		self.assertEqual(comm.sent_or_received, "Sent")
		self.assertEqual(comm.communication_medium, "Email")
		self.assertTrue(comm.has_attachment)
		self.assertIn(self.res.name, comm.subject)
		files = frappe.get_all("File", filters={"attached_to_doctype": "Communication",
		                                        "attached_to_name": comm.name},
		                       fields=["file_name", "is_private"])
		self.assertEqual(len(files), 1)
		self.assertTrue(files[0].file_name.endswith(".pdf"))
		self.assertTrue(files[0].is_private)
		# the PDF is the quotation itself
		html = pdf.call_args[0][0]
		self.assertIn(self.res.name, html)
		self.assertIn("2,000", html)

	def test_refused_without_an_outgoing_email_account(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			self.send()
		self.assertTrue(str(raised.exception).startswith("QUOTE_NO_EMAIL_ACCOUNT:"))
		self.assertFalse(self.comms())
		self.assertFalse(frappe.get_all("Email Queue", filters={
			"reference_doctype": "Reservation", "reference_name": self.res.name}))

	def test_refused_when_no_address_is_on_record(self):
		self.outgoing_account()
		frappe.db.set_value("Guest", self.res.guest, "email", None)
		with self.assertRaises(frappe.ValidationError) as raised:
			self.send()
		self.assertTrue(str(raised.exception).startswith("QUOTE_NO_RECIPIENT_EMAIL:"))
		self.assertFalse(self.comms())

	def test_a_separate_booker_without_an_email_is_not_sent_to_the_guest(self):
		self.outgoing_account()
		frappe.db.set_value("Reservation", self.res.name, {
			"booked_by_name": "UT Assistant", "contact_preference": "Booker"})
		with self.assertRaises(frappe.ValidationError) as raised:
			self.send()
		self.assertTrue(str(raised.exception).startswith("QUOTE_NO_RECIPIENT_EMAIL:"))
		self.assertFalse(self.comms())

	def test_a_closed_booking_gets_no_quotation(self):
		self.outgoing_account()
		frappe.db.set_value("Reservation", self.res.name, "status", "Cancelled")
		with self.assertRaises(frappe.ValidationError) as raised:
			self.send()
		self.assertTrue(str(raised.exception).startswith("QUOTE_BOOKING_CLOSED:"))

	def test_roles_below_the_front_desk_are_denied_not_refused(self):
		"""An authorization denial (PermissionError), kept apart from the
		validation refusals above."""
		self.outgoing_account()
		with self.assertRaises(frappe.PermissionError):
			self.send(user=NIGHT)
		self.assertFalse(self.comms())
