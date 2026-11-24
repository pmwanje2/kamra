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
	properties = (PROP_A, "UT Quote UGX", "UT Quote KWD", "UT Quote USD")

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

	# --- the price, line by line (review on #16) -------------------------

	def priced_booking(self):
		"""Two nights at 1,000 for two adults, breakfast at 200 a head a
		night, a 10% voucher and a 500 cleaning fee: room 2,000, meal plan
		800, discount -280, cleaning 500 before tax."""
		make_property(PROP_A, business_date=TODAY, cleaning_fee=500,
		              cleaning_fee_taxable=1)
		meal = frappe.get_doc({"doctype": "Meal Plan", "property": PROP_A, "code": "CP",
		                       "label": "Breakfast", "price_per_adult": 200}
		                      ).insert(ignore_permissions=True).name
		voucher = frappe.get_doc({"doctype": "Discount Voucher", "property": PROP_A,
		                          "voucher_code": "TENOFF", "discount_type": "Percent",
		                          "value": 10}).insert(ignore_permissions=True).name
		# a second room, so the priced stay is not refused as sold out
		self.res = make_reservation(PROP_A, self.rt, d(5), d(7),
		                            room=make_room(PROP_A, self.rt, "102"),
		                            guest=make_guest("UT Quote", email=GUEST_EMAIL),
		                            meal_plan=meal, voucher=voucher)
		return self.as_user(DESK, api.booking_quotation, self.res.name)

	def test_the_lines_add_up_to_the_total(self):
		"""OPERA proforma: room nights, the meal plan, the discount and the fee
		each on their own line, and the lines plus tax are the total."""
		q = self.priced_booking()
		amounts = {line["component"]: line["amount"] for line in q["lines"]}
		self.assertEqual(amounts, {"accommodation": 2000, "meal_plan": 800,
		                           "promotion": -280, "cleaning_fee": 500})
		self.assertEqual(sum(n["rate"] for n in q["nights"]), amounts["accommodation"])
		total = float(self.res.amount_after_tax)
		self.assertGreater(q["totals"]["tax"], 0)
		self.assertAlmostEqual(sum(amounts.values()) + q["totals"]["tax"], total, places=2)
		self.assertAlmostEqual(q["totals"]["total"], total, places=2)
		discount = next(x for x in q["lines"] if x["component"] == "promotion")
		self.assertIn("TENOFF", discount["description"])
		meal = next(x for x in q["lines"] if x["component"] == "meal_plan")
		self.assertIn("Breakfast", meal["description"])

	def test_the_terms_match_what_is_shown(self):
		q = self.priced_booking()
		terms = " ".join(q["terms"])
		self.assertNotIn("exclude meals", terms)
		self.assertIn("Breakfast", terms)
		html = api._booking_quotation_html(q)
		for line in q["lines"]:
			self.assertIn(frappe.utils.escape_html(line["description"]), html)
		self.assertIn("-280.00", html)
		self.assertIn("500.00", html)
		for term in q["terms"]:
			self.assertIn(frappe.utils.escape_html(term), html)
		self.assertNotIn("exclude meals", html)

	def test_a_room_only_booking_says_meals_are_not_included(self):
		q = self.as_user(DESK, api.booking_quotation, self.res.name)
		self.assertEqual([x["component"] for x in q["lines"]], ["accommodation"])
		self.assertAlmostEqual(q["lines"][0]["amount"] + q["totals"]["tax"],
		                       q["totals"]["total"], places=2)
		self.assertTrue(any("not included" in t for t in q["terms"]))

	def test_the_currency_symbol_is_escaped_in_the_pdf(self):
		currency = frappe.db.get_value("Property", PROP_A, "currency")
		frappe.db.set_value("Currency", currency, "symbol", "<i>R")
		q = self.as_user(DESK, api.booking_quotation, self.res.name)
		html = api._booking_quotation_html(q)
		self.assertNotIn("<i>R", html)
		self.assertIn("&lt;i&gt;R", html)

	# --- one send per version a minute ----------------------------------

	def test_the_same_quotation_is_not_resent_within_a_minute(self):
		self.outgoing_account()
		out, _pdf = self.send()
		self.assertEqual(out["status"], "Queued")
		with self.assertRaises(frappe.ValidationError) as raised:
			self.send()
		self.assertTrue(str(raised.exception).startswith("QUOTE_RECENTLY_SENT:"))
		self.assertEqual(len(self.comms()), 1)
		# a minute later it goes again
		log = frappe.get_all("Agent Action Log", filters={
			"action_type": "quotation_emailed", "reference_name": self.res.name},
			pluck="name")
		self.assertEqual(len(log), 1)
		frappe.db.set_value("Agent Action Log", log[0], "executed_at",
		                    frappe.utils.add_to_date(frappe.utils.now_datetime(),
		                                             seconds=-61))
		self.send()
		self.assertEqual(len(self.comms()), 2)

	def test_a_changed_quotation_can_be_sent_at_once(self):
		self.outgoing_account()
		self.send()
		res = frappe.get_doc("Reservation", self.res.name)
		res.check_out_date = d(8)
		res.save(ignore_permissions=True)
		self.send()
		self.assertEqual(len(self.comms()), 2)

	def test_roles_below_the_front_desk_are_denied_not_refused(self):
		"""An authorization denial (PermissionError), kept apart from the
		validation refusals above."""
		self.outgoing_account()
		with self.assertRaises(frappe.PermissionError):
			self.send(user=NIGHT)
		self.assertFalse(self.comms())

	def test_quotation_html_uses_property_locale_symbol_and_currency_decimals(self):
		"""R8 P2 #6: _booking_quotation_html uses the property's locale symbol and
		currency minor-unit decimals (UGX whole shillings replacing 'Sh', KWD 3 dec, USD 2 dec)."""
		def _ensure_currency(name, sym, frac_units, fmt):
			if not frappe.db.exists("Currency", name):
				frappe.get_doc({
					"doctype": "Currency", "currency_name": name, "enabled": 1,
					"symbol": sym, "fraction": "Sub", "fraction_units": frac_units,
					"number_format": fmt,
				}).insert(ignore_permissions=True)
			else:
				frappe.db.set_value("Currency", name, {
					"enabled": 1, "symbol": sym, "fraction_units": frac_units, "number_format": fmt,
				})

		_ensure_currency("UGX", "Sh", 1, "#,###")
		_ensure_currency("KWD", "KWD ", 1000, "#,###.###")
		_ensure_currency("USD", "$", 100, "#,###.##")

		# Uganda property with Currency.UGX.symbol == "Sh": must render "UGX 270,000" (0 decimals, no "Sh")
		ug_prop = "UT Quote UGX"
		make_property(ug_prop, business_date=TODAY, country="Uganda", currency="UGX",
		              gst_mode="Fixed", gst_rate_low=0, gst_rate_high=0, rates_include_tax=1)
		ug_rt = make_room_type(ug_prop, "USTD", base_price=135000, tax_percent=0)
		frappe.db.set_value("Room Type", ug_rt, {"base_price": 135000, "tax_percent": 0})
		ug_room = make_room(ug_prop, ug_rt, "U101")
		ug_res = make_reservation(ug_prop, ug_rt, d(5), d(7), room=ug_room,
		                          guest=make_guest("UG Guest", email="ug@guest.test"))
		ug_q = self.as_user(DESK, api.booking_quotation, ug_res.name)
		ug_html = api._booking_quotation_html(ug_q)
		self.assertIn("UGX 135,000", ug_html)
		self.assertIn("UGX 270,000", ug_html)
		self.assertNotIn("Sh135,000", ug_html)
		self.assertNotIn("Sh270,000", ug_html)
		self.assertNotIn("270,000.00", ug_html)

		# Kuwait property (KWD): 3 decimals preserved
		kw_prop = "UT Quote KWD"
		make_property(kw_prop, business_date=TODAY, country="Kuwait", currency="KWD",
		              gst_mode="Fixed", gst_rate_low=0, gst_rate_high=0, rates_include_tax=0)
		kw_rt = make_room_type(kw_prop, "KSTD", base_price=12.345, tax_percent=0)
		frappe.db.set_value("Room Type", kw_rt, {"base_price": 12.345, "tax_percent": 0})
		kw_room = make_room(kw_prop, kw_rt, "K101")
		kw_res = make_reservation(kw_prop, kw_rt, d(5), d(7), room=kw_room,
		                          guest=make_guest("KW Guest", email="kw@guest.test"))
		kw_q = self.as_user(DESK, api.booking_quotation, kw_res.name)
		kw_html = api._booking_quotation_html(kw_q)
		self.assertIn("KWD 12.345", kw_html)
		self.assertIn("KWD 24.690", kw_html)

		# US property (USD): 2 decimals preserved
		us_prop = "UT Quote USD"
		make_property(us_prop, business_date=TODAY, country="United States", currency="USD",
		              gst_mode="Fixed", gst_rate_low=0, gst_rate_high=0, rates_include_tax=0)
		us_rt = make_room_type(us_prop, "USSTD", base_price=150.5, tax_percent=0)
		frappe.db.set_value("Room Type", us_rt, {"base_price": 150.5, "tax_percent": 0})
		us_room = make_room(us_prop, us_rt, "S101")
		us_res = make_reservation(us_prop, us_rt, d(5), d(7), room=us_room,
		                          guest=make_guest("US Guest", email="us@guest.test"))
		us_q = self.as_user(DESK, api.booking_quotation, us_res.name)
		us_html = api._booking_quotation_html(us_q)
		self.assertIn("$150.50", us_html)
		self.assertIn("$301.00", us_html)
