"""The fiscal folio: under the ERPNext accounting bridge the guest's tax
invoice is the ERPNext Sales Invoice, never this app's own folio number.

OPERA Cloud prints a fiscal folio with the statutory identifiers the fiscal
service returned, and Folio History reprints that folio. Uganda's URA EFRIS
makes the Fiscal Document Number (FDN), the verification (anti-fake) code and
the QR code part of the tax invoice. The bridge reads them from the folio's
Sales Invoice (`hospitality_kamra.fiscal_document`); here the reader is a
double behind kamra.erp_bridge, the same seam every bridge reader uses.
"""

from contextlib import contextmanager
from unittest.mock import patch

import frappe

from kamra import api, erp_bridge
from kamra.folio import close_folio
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	ensure_user,
	guest_folio,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

FISCAL = "hospitality_kamra.fiscal_document"
INVOICE = "ACC-SINV-2031-00007"
FISCALISED = {
	"state": "fiscalised", "sales_invoice": INVOICE, "fdn": "322000123456",
	"verification_code": "80123456789012345678", "qr": "data:image/png;base64,iVBORw0KGgo=",
	"issued": "2031-03-12 10:04:00", "sandbox": 0, "efris_status": "Success",
	"tax_template": "VAT 18% Inclusive - EA", "charges_tax": 1, "efris_enrolled": 1,
	"outstanding_amount": 0.0, "credit_notes": [], "replaced_by": None,
}


@contextmanager
def fiscal_reader(result):
	"""Install the bridge's fiscal-document reader and answer with `result` (or raise if an exception)."""
	real = erp_bridge.get_bridge_script
	reader = object()
	calls = []

	def get(method):
		return reader if method == FISCAL else real(method)

	def run(script, form):
		assert script is reader
		calls.append(dict(form))
		if isinstance(result, BaseException):
			raise result
		return result

	with patch.object(erp_bridge, "get_bridge_script", side_effect=get), \
			patch.object(erp_bridge, "run_bridge_script", side_effect=run):
		yield calls


class FiscalFolioTests(AuditTestCase):
	def setUp(self):
		super().setUp()
		make_property(PROP_A, business_date=TODAY)
		rt = make_room_type(PROP_A)
		self.res = make_reservation(PROP_A, rt, TODAY, d(1), status="Checked In",
		                            room=make_room(PROP_A, rt, "101"))
		self.folio = guest_folio(self.res.name)

	def settle(self):
		api._post_folio_charge(self.folio, "Misc", "Airport transfer", 1000)
		owed = frappe.db.get_value("Folio", self.folio, "balance")
		api.add_folio_payment(self.folio, "Cash", owed)
		return close_folio(self.folio)

	# --- the closed bill shows the statutory document ---------------------------------------------------

	def test_a_fiscalised_folio_prints_the_sales_invoice_and_its_efris_identifiers(self):
		internal = self.settle()
		with fiscal_reader(FISCALISED) as calls:
			data = api.folio_invoice(self.folio)
		self.assertEqual(calls, [{"folio": self.folio}])
		document = data["document"]
		self.assertEqual(document["number"], INVOICE)
		self.assertEqual(document["internal_number"], internal)
		self.assertTrue(document["is_final"])
		fiscal = data["fiscal"]
		self.assertEqual((fiscal["state"], fiscal["sales_invoice"], fiscal["fdn"], fiscal["verification_code"]),
		                 ("fiscalised", INVOICE, "322000123456", "80123456789012345678"))
		self.assertEqual(fiscal["qr"], FISCALISED["qr"])
		self.assertEqual(fiscal["issued"], "2031-03-12 10:04:00")

	def test_efris_pending_is_never_presented_as_a_final_invoice(self):
		internal = self.settle()
		pending = {"state": "pending", "sales_invoice": INVOICE, "efris_status": "Failed"}
		with fiscal_reader(pending):
			data = api.folio_invoice(self.folio)
		document = data["document"]
		self.assertFalse(document["is_final"])
		self.assertEqual(document["title"], "Fiscal document pending")
		self.assertEqual(document["number"], INVOICE)
		self.assertEqual(document["internal_number"], internal)
		self.assertEqual((data["fiscal"]["state"], data["fiscal"]["fdn"], data["fiscal"]["efris_status"]),
		                 ("pending", None, "Failed"))

	def test_a_closed_folio_whose_invoice_is_not_issued_yet_shows_pending_not_the_internal_number(self):
		internal = self.settle()
		with fiscal_reader({"state": "pending", "sales_invoice": None}):
			document = api.folio_invoice(self.folio)["document"]
		self.assertFalse(document["is_final"])
		self.assertNotEqual(document["number"], internal)
		self.assertEqual(document["number"], self.folio)
		self.assertEqual(document["internal_number"], internal)

	def test_an_efris_sandbox_number_is_not_a_valid_tax_invoice(self):
		self.settle()
		with fiscal_reader({**FISCALISED, "sandbox": 1}):
			data = api.folio_invoice(self.folio)
		self.assertFalse(data["document"]["is_final"])
		self.assertTrue(data["fiscal"]["sandbox"])

	def test_a_company_not_on_efris_prints_its_erp_invoice_number_as_final(self):
		internal = self.settle()
		for charges_tax, want_title in ((1, "Tax Invoice"), (0, "Invoice")):
			with self.subTest(charges_tax=charges_tax):
				with fiscal_reader({
					"state": "not_efris", "sales_invoice": INVOICE,
					"charges_tax": charges_tax, "efris_enrolled": 0,
				}):
					document = api.folio_invoice(self.folio)["document"]
				self.assertTrue(document["is_final"])
				self.assertEqual(document["title"], want_title)
				self.assertEqual((document["number"], document["internal_number"]), (INVOICE, internal))

	def test_not_efris_for_an_efris_enrolled_company_is_not_final(self):
		"""Tax plan PR-9: not_efris on an EFRIS-enrolled company stays pending, never final."""
		self.settle()
		answer = {
			"state": "not_efris", "sales_invoice": INVOICE,
			"charges_tax": 1, "efris_enrolled": 1,
		}
		with fiscal_reader(answer):
			data = api.folio_invoice(self.folio)
			self.assertFalse(data["document"]["is_final"])
			self.assertEqual(data["document"]["title"], "Fiscal document pending")
			self.assertEqual(data["fiscal"]["state"], "pending")
			with self.assertRaisesRegex(frappe.ValidationError, "pending"):
				api.reprint_fiscal_folio(self.folio)

	def test_a_reader_without_tax_and_enrollment_fields_fails_closed(self):
		"""Tax plan PR-9: a reader omitting charges_tax or efris_enrolled is never treated as final."""
		self.settle()
		legacy_fiscalised = {k: v for k, v in FISCALISED.items() if k not in ("charges_tax", "efris_enrolled")}
		for answer in (
			{"state": "not_efris", "sales_invoice": INVOICE},
			{"state": "not_efris", "sales_invoice": INVOICE, "efris_enrolled": 0},
			legacy_fiscalised,
		):
			with self.subTest(answer=answer), fiscal_reader(answer):
				data = api.folio_invoice(self.folio)
				self.assertFalse(data["document"]["is_final"])
				self.assertEqual(data["fiscal"]["state"], "unverified")
				self.assertEqual(data["document"]["title"], "Fiscal status unavailable")
				with self.assertRaisesRegex(frappe.ValidationError, "unavailable"):
					api.reprint_fiscal_folio(self.folio)

	def test_uganda_has_no_static_invoice_title_and_standalone_title_follows_configured_tax(self):
		"""Tax plan PR-9: no static Uganda INVOICE_TITLE; standalone title follows configured tax."""
		from kamra.localization import uganda

		self.assertIsNone(getattr(uganda, "INVOICE_TITLE", None))
		frappe.db.set_value("Property", PROP_A, {"country": "Uganda", "currency": "UGX",
		                                         "gst_rate_low": 0, "gst_rate_high": 0})
		frappe.clear_document_cache("Property", PROP_A)
		self.settle()
		self.assertEqual(api.folio_invoice(self.folio)["document"]["title"], "Invoice")
		frappe.db.set_value("Property", PROP_A, {"gst_rate_low": 18})
		frappe.clear_document_cache("Property", PROP_A)
		self.assertEqual(api.folio_invoice(self.folio)["document"]["title"], "Tax Invoice")

	def test_a_reader_error_falls_back_to_fiscal_status_unavailable_instead_of_500(self):
		"""Hydrogen P1-1: bridge_call error must never crash folio_invoice with HTTP 500."""
		internal = self.settle()
		err = AttributeError("module has no attribute 'parse_json'")
		with fiscal_reader(err):
			data = api.folio_invoice(self.folio)
			self.assertFalse(data["document"]["is_final"])
			self.assertEqual(data["document"]["title"], "Fiscal status unavailable")
			self.assertEqual(data["document"]["number"], self.folio)
			self.assertEqual(data["document"]["internal_number"], internal)
			self.assertEqual(data["fiscal"]["state"], "unverified")
			with self.assertRaisesRegex(frappe.ValidationError, "unavailable"):
				api.reprint_fiscal_folio(self.folio)

	def test_add_folio_payment_refuses_a_closed_folio(self):
		"""Hydrogen P1-2 (probe2b): OPERA refuses payments on a closed folio."""
		self.settle()
		before = len(frappe.get_doc("Folio", self.folio).payments)
		with self.assertRaisesRegex(frappe.ValidationError, "closed"):
			api.add_folio_payment(self.folio, "Cash", 100)
		self.assertEqual(len(frappe.get_doc("Folio", self.folio).payments), before)

	def test_an_unrecognised_reader_answer_fails_closed(self):
		for answer in ({"state": "issued", "sales_invoice": INVOICE},
		               {"state": "fiscalised", "sales_invoice": INVOICE, "fdn": None,
		                "charges_tax": 1, "efris_enrolled": 1},
		               {"state": "fiscalised", "sales_invoice": INVOICE, "fdn": 7,
		                "verification_code": "x", "charges_tax": 1, "efris_enrolled": 1}):
			with self.subTest(answer=answer):
				if not frappe.db.get_value("Folio", self.folio, "invoice_number"):
					self.settle()
				with fiscal_reader(answer):
					data = api.folio_invoice(self.folio)
				self.assertFalse(data["document"]["is_final"])
				self.assertEqual(data["fiscal"]["state"], "unverified")
				self.assertEqual(data["document"]["title"], "Fiscal status unavailable")

	# --- what does not change ---------------------------------------------------------------------------

	def test_without_the_bridge_the_folio_keeps_its_own_invoice(self):
		internal = self.settle()
		data = api.folio_invoice(self.folio)
		self.assertIsNone(data.get("fiscal"))
		self.assertEqual(data["document"]["number"], internal)
		self.assertTrue(data["document"]["is_final"])

	def test_an_open_folio_is_a_provisional_bill_and_never_asks_the_bridge(self):
		api._post_folio_charge(self.folio, "Misc", "Airport transfer", 1000)
		with fiscal_reader(FISCALISED) as calls:
			data = api.folio_invoice(self.folio)
		self.assertEqual(calls, [])
		self.assertIsNone(data.get("fiscal"))
		self.assertFalse(data["document"]["is_final"])

	# --- reprint the fiscal document (OPERA Folio History, Reprint) -------------------------------------

	def test_reprint_records_the_sales_invoice_and_counts_revisions(self):
		self.settle()
		with fiscal_reader(FISCALISED):
			first = api.reprint_fiscal_folio(self.folio, reason="Guest copy")
			second = api.reprint_fiscal_folio(self.folio)
		self.assertEqual((first["revision"], second["revision"]), (1, 2))
		self.assertEqual(first["document"]["number"], INVOICE)
		self.assertEqual(first["document"]["reprint"], 1)
		self.assertEqual(first["fiscal"]["fdn"], "322000123456")
		rows = frappe.get_all("Folio Reprint", filters={"folio": self.folio},
		                      fields=["invoice_number", "reprint_of", "reason", "revision"],
		                      order_by="revision asc")
		self.assertEqual([(r.invoice_number, r.reprint_of, r.revision) for r in rows],
		                 [(INVOICE, "Guest Folio", 1), (INVOICE, "Guest Folio", 2)])
		self.assertEqual(rows[0].reason, "Guest copy")

	def test_a_pending_fiscal_document_cannot_be_reprinted(self):
		self.settle()
		for answer in ({"state": "pending", "sales_invoice": INVOICE}, {**FISCALISED, "sandbox": 1},
		               {"state": "issued", "sales_invoice": INVOICE}):
			with self.subTest(answer=answer), fiscal_reader(answer):
				with self.assertRaisesRegex(frappe.ValidationError, "pending|not a valid|unavailable"):
					api.reprint_fiscal_folio(self.folio)
		self.assertFalse(frappe.db.exists("Folio Reprint", {"folio": self.folio}))

	def test_an_open_folio_has_nothing_to_reprint(self):
		with fiscal_reader(FISCALISED), self.assertRaises(frappe.ValidationError):
			api.reprint_fiscal_folio(self.folio)

	def test_without_the_bridge_reprint_keeps_the_folio_invoice_number(self):
		internal = self.settle()
		out = api.reprint_fiscal_folio(self.folio)
		self.assertEqual(out["document"]["number"], internal)
		self.assertEqual(frappe.db.get_value("Folio Reprint", {"folio": self.folio}, "invoice_number"), internal)

	def test_only_billing_roles_reprint(self):
		self.settle()
		user = ensure_user("fiscal-hk@example.test", ["Housekeeping"])
		frappe.set_user(user)  # nosemgrep: frappe-setuser -- test user with a single role
		try:
			with fiscal_reader(FISCALISED), self.assertRaises(frappe.PermissionError):
				api.reprint_fiscal_folio(self.folio)
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- restore the test user
