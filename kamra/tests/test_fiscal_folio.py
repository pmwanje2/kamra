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
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from kamra.tests.bridge_fields import ensure_bridge_field
		ensure_bridge_field("Folio", "sales_invoice")
		ensure_bridge_field("Property", "company")

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
		"""Tax plan PR-9 / #48: no static Uganda INVOICE_TITLE; standalone title follows VAT determination."""
		from kamra.localization import uganda

		self.assertIsNone(getattr(uganda, "INVOICE_TITLE", None))
		self.assertEqual(getattr(uganda, "VAT_INVOICE_TITLE", None), "Tax Invoice")
		frappe.db.set_value("Property", PROP_A, {"country": "Uganda", "currency": "UGX",
		                                         "gst_rate_low": 0, "gst_rate_high": 0})
		frappe.clear_document_cache("Property", PROP_A)
		self.settle()
		self.assertEqual(api.folio_invoice(self.folio)["document"]["title"], "Invoice")
		frappe.db.set_value("Property", PROP_A, {"gst_rate_low": 18})
		frappe.clear_document_cache("Property", PROP_A)
		# Already-settled 0%-VAT invoice stays "Invoice"; a settled VAT-charged invoice prints "Tax Invoice".
		self.assertEqual(api.folio_invoice(self.folio)["document"]["title"], "Invoice")
		taxed_folio = api.split_folio(self.res.name, "Extra")["folio"]
		api._post_folio_charge(taxed_folio, "Misc", "Taxed transfer", 1000, gst_rate=18)
		owed = frappe.db.get_value("Folio", taxed_folio, "balance")
		api.add_folio_payment(taxed_folio, "Cash", owed)
		close_folio(taxed_folio)
		self.assertEqual(api.folio_invoice(taxed_folio)["document"]["title"], "Tax Invoice")

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

	def test_folio_invoice_reports_bridge_credit_and_rebill_availability(self):
		"""Hydrogen P2: UI hides bridge Credit note / Cancel invoice when credit_and_rebill script is absent."""
		self.settle()
		with fiscal_reader(FISCALISED):
			without = api.folio_invoice(self.folio)
		self.assertIs(without.get("bridge_credit_and_rebill"), False)

		real = erp_bridge.get_bridge_script
		sentinel = object()
		with fiscal_reader(FISCALISED), patch.object(
			erp_bridge, "get_bridge_script",
			side_effect=lambda m: sentinel if m == "hospitality_kamra.credit_and_rebill" else real(m),
		):
			with_script = api.folio_invoice(self.folio)
		self.assertIs(with_script.get("bridge_credit_and_rebill"), True)

	def test_stay_security_deposit_full_chains_never_reduce_balance_or_auto_apply(self):
		"""OPERA/SAP on stay (non-event) Guest folios: collect->close while held->refund later; collect->refund->close; explicit withhold."""
		from kamra import ledger

		# Chain 1: collect -> post charges -> pay bill -> close while deposit still held -> refund later (partial + full)
		from kamra.folio import post_remaining_nights
		post_remaining_nights(self.res)
		api.collect_security_deposit(self.res.name, 100000, "Cash")
		room_amt = float(frappe.db.get_value("Folio", self.folio, "grand_total") or 0)
		api._post_folio_charge(self.folio, "Misc", "Room package", 500000 - room_amt)
		fd = frappe.get_doc("Folio", self.folio)
		self.assertAlmostEqual(float(fd.payments_total), 0.0, 2)
		self.assertAlmostEqual(float(fd.balance), 500000.0, 2)
		self.assertTrue(ledger.folio_reconciliation(PROP_A)["in_balance"])
		# check_out refuses while bill is unpaid AND while deposit is held.
		with self.assertRaises(frappe.ValidationError):
			api.check_out(self.res.name)
		api.add_folio_payment(self.folio, "Cash", 500000)
		fd = frappe.get_doc("Folio", self.folio)
		self.assertAlmostEqual(float(fd.payments_total), 500000.0, 2)
		self.assertAlmostEqual(float(fd.balance), 0.0, 2)
		# Folio can close while Security Deposit is still held as a separate liability.
		close_folio(self.folio)
		self.assertEqual(frappe.db.get_value("Folio", self.folio, "status"), "Closed")
		# check_out still refuses while held deposit is unrefunded (never silently applied!).
		with self.assertRaises(frappe.ValidationError):
			api.check_out(self.res.name)
		# Refund partial (40k) then remainder (60k) AFTER folio close.
		st1 = api.refund_security_deposit(self.res.name, 40000, "Cash", "Partial return")
		self.assertEqual((st1["held"], st1["refunded"]), (60000.0, 40000.0))
		fd = frappe.get_doc("Folio", self.folio)
		self.assertAlmostEqual(float(fd.payments_total), 500000.0, 2)
		self.assertAlmostEqual(float(fd.balance), 0.0, 2)
		st2 = api.refund_security_deposit(self.res.name, 60000, "Cash", "Final return")
		self.assertEqual((st2["held"], st2["refunded"], st2["status"]), (0.0, 100000.0, "Refunded"))
		self.assertTrue(ledger.folio_reconciliation(PROP_A)["in_balance"])
		api.check_out(self.res.name)
		self.assertEqual(frappe.db.get_value("Reservation", self.res.name, "status"), "Checked Out")

		# Chain 2: collect -> refund -> post charges -> pay -> close
		rt = frappe.db.get_value("Reservation", self.res.name, "room_type")
		res2 = make_reservation(PROP_A, rt, TODAY, d(1), status="Checked In", room=make_room(PROP_A, rt, "102"))
		f2 = guest_folio(res2.name)
		api.collect_security_deposit(res2.name, 50000, "Cash")
		api.refund_security_deposit(res2.name, 50000, "Cash", "Returned early")
		api._post_folio_charge(f2, "Misc", "Spa", 200000)
		fd2 = frappe.get_doc("Folio", f2)
		self.assertAlmostEqual(float(fd2.payments_total), 0.0, 2)
		self.assertAlmostEqual(float(fd2.balance), 200000.0, 2)
		api.add_folio_payment(f2, "Cash", 200000)
		close_folio(f2)
		self.assertTrue(ledger.folio_reconciliation(PROP_A)["in_balance"])

		# Chain 3: explicit signed-off withhold_security_deposit applies ONLY withheld_amount to cover damage charge
		res3 = make_reservation(PROP_A, rt, TODAY, d(1), status="Checked In", room=make_room(PROP_A, rt, "103"))
		f3 = guest_folio(res3.name)
		post_remaining_nights(res3)
		api.collect_security_deposit(res3.name, 100000, "Cash")
		room_amt3 = float(frappe.db.get_value("Folio", f3, "grand_total") or 0)
		api._post_folio_charge(f3, "Misc", "Room package", 500000 - room_amt3)
		api.add_folio_payment(f3, "Cash", 500000)
		api.withhold_security_deposit(res3.name, 30000, "Broken lamp")
		fd3 = frappe.get_doc("Folio", f3)
		self.assertAlmostEqual(float(fd3.charges_total), 530000.0, 2)
		self.assertAlmostEqual(float(fd3.payments_total), 530000.0, 2)
		self.assertAlmostEqual(float(fd3.balance), 0.0, 2)
		self.assertTrue(ledger.folio_reconciliation(PROP_A)["in_balance"])
		api.refund_security_deposit(res3.name, 70000, "Cash", "Balance returned")
		fd3 = frappe.get_doc("Folio", f3)
		self.assertAlmostEqual(float(fd3.payments_total), 530000.0, 2)
		self.assertAlmostEqual(float(fd3.balance), 0.0, 2)
		self.assertTrue(ledger.folio_reconciliation(PROP_A)["in_balance"])
		api.check_out(res3.name)
		self.assertEqual(frappe.db.get_value("Reservation", res3.name, "status"), "Checked Out")

	# --- Hydrogen R1 merge-gate regression tests (Findings 2, 3, 5, 6) ---------------------------------

	def test_non_efris_tourism_levy_only_renders_neutral_invoice_not_tax_invoice(self):
		"""Hydrogen R1 #2: Uganda Non-EFRIS invoice charging 2% Tourism Levy only (0% VAT) stays 'Invoice'."""
		import json as _json

		frappe.db.set_value("Property", PROP_A, {
			"country": "Uganda", "currency": "UGX",
			"gst_rate_low": 0, "gst_rate_high": 0, "company": None,
		})
		frappe.clear_document_cache("Property", PROP_A)
		self.settle()
		fdoc = frappe.get_doc("Folio", self.folio)
		charge_row = fdoc.charges[0]
		frappe.db.set_value("Folio", self.folio, {
			"sales_invoice": INVOICE, "invoice_number": None,
		}, update_modified=False)

		def _run(taxes, net, tax_tot, grand, charges_tax, template):
			fake_si = frappe._dict(
				doctype="Sales Invoice", name=INVOICE, kamra_ref=self.folio,
				docstatus=1, company=None, currency="UGX", is_return=0,
				return_against=None, kamra_rebill_of=None,
				net_total=net, total_taxes_and_charges=tax_tot, grand_total=grand,
				items=[frappe._dict(
					name="sii-1", item_code="ITEM-MISC", source_ref=charge_row.name,
					net_amount=net, amount=grand, kamra_allowance_refs="",
				)],
				taxes=[frappe._dict(tx) for tx in taxes],
				item_wise_tax_details=[],
			)
			real_get_doc = frappe.get_doc
			real_exists = frappe.db.exists
			reader_ans = {
				"state": "not_efris", "sales_invoice": INVOICE,
				"charges_tax": charges_tax, "efris_enrolled": 0,
				"tax_template": template,
			}
			with fiscal_reader(reader_ans), \
					patch("frappe.get_doc", side_effect=lambda dt, nm=None, *a, **k:
					      fake_si if dt == "Sales Invoice" and nm == INVOICE else real_get_doc(dt, nm, *a, **k)), \
					patch("frappe.db.exists", side_effect=lambda dt, nm=None, *a, **k:
					      True if dt == "DocType" and nm == "Sales Invoice" else real_exists(dt, nm, *a, **k)):
				return api.folio_invoice(self.folio)

		# 2% Tourism Levy only (0% VAT): bridge reports charges_tax=1, yet title MUST stay neutral "Invoice".
		levy_inv = _run(
			[{
				"name": "sit-1", "charge_type": "On Net Total", "account_head": "Tourism Levy - UT",
				"description": "Tourism Levy 2%", "rate": 2.0,
				"tax_amount": 20.0, "tax_amount_after_discount_amount": 20.0,
				"included_in_print_rate": 0,
				"item_wise_tax_detail": _json.dumps({"ITEM-MISC": [2.0, 20.0]}),
			}],
			1000.0, 20.0, 1020.0, charges_tax=1, template="Tourism Levy 2% - UT",
		)
		self.assertEqual(levy_inv["tax_source"], "accounting")
		self.assertTrue(levy_inv["document"]["is_final"])
		self.assertEqual(levy_inv["document"]["title"], "Invoice")

		# 18% VAT on Non-EFRIS Uganda -> "Tax Invoice".
		vat_inv = _run(
			[{
				"name": "sit-2", "charge_type": "On Net Total", "account_head": "VAT - UT",
				"description": "VAT 18%", "rate": 18.0,
				"tax_amount": 180.0, "tax_amount_after_discount_amount": 180.0,
				"included_in_print_rate": 0,
				"item_wise_tax_detail": _json.dumps({"ITEM-MISC": [18.0, 180.0]}),
			}],
			1000.0, 180.0, 1180.0, charges_tax=1, template="Hotel VAT 18% - UT",
		)
		self.assertEqual(vat_inv["tax_source"], "accounting")
		self.assertTrue(vat_inv["document"]["is_final"])
		self.assertEqual(vat_inv["document"]["title"], "Tax Invoice")

	def test_restricted_viewer_without_sales_invoice_read_gets_neutral_title_and_no_fiscal_tax_leak(self):
		"""Hydrogen R1 #2: restricted viewer (tax_source='accounting_unavailable') never sees charges_tax/tax_template or 'Tax Invoice'."""
		frappe.db.set_value("Property", PROP_A, {
			"country": "Uganda", "currency": "UGX", "gst_rate_low": 0, "gst_rate_high": 0,
		})
		frappe.clear_document_cache("Property", PROP_A)
		self.settle()
		frappe.db.set_value("Folio", self.folio, {
			"sales_invoice": INVOICE, "invoice_number": None,
		}, update_modified=False)

		real_exists = frappe.db.exists
		real_has_perm = frappe.has_permission

		def deny_si(doctype, ptype="read", doc=None, *a, **k):
			if doctype == "Sales Invoice":
				return False
			return real_has_perm(doctype, ptype=ptype, doc=doc, *a, **k)

		with fiscal_reader(FISCALISED), \
				patch("frappe.db.exists", side_effect=lambda dt, nm=None, *a, **k:
				      True if dt == "DocType" and nm == "Sales Invoice" else real_exists(dt, nm, *a, **k)), \
				patch("frappe.has_permission", side_effect=deny_si):
			data = api.folio_invoice(self.folio)

		self.assertEqual(data["tax_source"], "accounting_unavailable")
		self.assertEqual(data["tax_summary"], [])
		self.assertTrue(data["document"]["is_final"])
		self.assertEqual(data["document"]["title"], "Invoice")
		self.assertIsNone(data["fiscal"]["charges_tax"])
		self.assertIsNone(data["fiscal"]["tax_template"])

		# Pending EFRIS under restricted viewer stays "Fiscal document pending" and still redacts tax metadata.
		pending = {
			"state": "pending", "sales_invoice": INVOICE, "efris_status": "Queued",
			"charges_tax": 1, "efris_enrolled": 1, "tax_template": "VAT 18% - UT",
		}
		with fiscal_reader(pending), \
				patch("frappe.db.exists", side_effect=lambda dt, nm=None, *a, **k:
				      True if dt == "DocType" and nm == "Sales Invoice" else real_exists(dt, nm, *a, **k)), \
				patch("frappe.has_permission", side_effect=deny_si):
			p_data = api.folio_invoice(self.folio)

		self.assertFalse(p_data["document"]["is_final"])
		self.assertEqual(p_data["document"]["title"], "Fiscal document pending")
		self.assertIsNone(p_data["fiscal"]["charges_tax"])
		self.assertIsNone(p_data["fiscal"]["tax_template"])

	def test_closing_unbridged_folio_preserves_ordinary_refund_alongside_security_deposit(self):
		"""Hydrogen R1 #3: closing an unbridged folio must NOT sweep ordinary Refund rows into Security Deposits."""
		from kamra.folio import security_deposit_held_on_folio

		frappe.db.set_value("Property", PROP_A, {"gst_rate_low": 0, "gst_rate_high": 0})
		frappe.clear_document_cache("Property", PROP_A)
		api._post_folio_charge(self.folio, "Misc", "Parking fee", 105.0, gst_rate=0)
		api.collect_security_deposit(self.res.name, 50.0, "Cash")
		api.add_folio_payment(self.folio, "Cash", 150.0)
		api.refund_folio_payment(self.folio, 45.0, "Cash", "Overpayment returned")

		open_doc = frappe.get_doc("Folio", self.folio)
		self.assertEqual(open_doc.status, "Open")
		self.assertAlmostEqual(float(open_doc.grand_total), 105.0, 2)
		self.assertAlmostEqual(float(open_doc.payments_total), 105.0, 2)
		self.assertAlmostEqual(float(open_doc.balance), 0.0, 2)
		self.assertAlmostEqual(security_deposit_held_on_folio(open_doc), 50.0, 2)

		close_folio(self.folio)
		closed_doc = frappe.get_doc("Folio", self.folio)
		self.assertEqual(closed_doc.status, "Closed")
		self.assertAlmostEqual(float(closed_doc.grand_total), 105.0, 2)
		self.assertAlmostEqual(float(closed_doc.payments_total), 105.0, 2)
		self.assertAlmostEqual(float(closed_doc.balance), 0.0, 2)
		self.assertAlmostEqual(security_deposit_held_on_folio(closed_doc), 50.0, 2)

	def test_batch_print_folios_enforces_fiscal_finality_and_records_sales_invoice(self):
		"""Hydrogen R1 #5: batch_print_folios refuses pending/sandbox/unverified EFRIS folios and logs sales_invoice."""
		from kamra import ledger

		self.settle()
		for answer in (
			{"state": "pending", "sales_invoice": INVOICE},
			{**FISCALISED, "sandbox": 1},
			{"state": "issued", "sales_invoice": INVOICE},
			{"state": "not_efris", "sales_invoice": INVOICE, "charges_tax": 1, "efris_enrolled": 1},
		):
			with self.subTest(answer=answer), fiscal_reader(answer):
				with self.assertRaisesRegex(frappe.ValidationError, "pending|not a valid|unavailable"):
					ledger.batch_print_folios([self.folio])
		self.assertFalse(frappe.db.exists("Folio Reprint", {"folio": self.folio}))

		# When final (even if internal invoice_number is None on a bridged close), batch_print logs sales_invoice.
		frappe.db.set_value("Folio", self.folio, "invoice_number", None, update_modified=False)
		with fiscal_reader(FISCALISED):
			res = ledger.batch_print_folios([self.folio])
		self.assertEqual(len(res["reprints"]), 1)
		logged_inv = frappe.db.get_value("Folio Reprint", res["reprints"][0]["reprint"], "invoice_number")
		self.assertEqual(logged_inv, INVOICE)

	def test_amount_in_words_and_stay_money_respect_currency_precision(self):
		"""Hydrogen R1 #6: KWD 3dp precision preserved in amount_in_words and _stay_money."""
		from kamra.localization.words import amount_in_words

		self.assertEqual(amount_in_words(12.375, "KWD", indian=False), "KWD Twelve point 375 Only")
		self.assertEqual(amount_in_words(12.05, "KWD", indian=False), "KWD Twelve point 050 Only")
		self.assertEqual(amount_in_words(115.375, "KWD", indian=False), "KWD One Hundred Fifteen point 375 Only")
		self.assertEqual(amount_in_words(330400, "UGX", indian=False), "UGX Three Hundred Thirty Thousand Four Hundred Only")
		self.assertEqual(
			amount_in_words(193.33, "USD", indian=False),
			"Dollars One Hundred Ninety Three and Thirty Three Cents Only",
		)

		if not frappe.db.exists("Currency", "KWD"):
			frappe.get_doc({
				"doctype": "Currency", "currency_name": "KWD", "symbol": "د.ك",
				"fraction": "Fils", "fraction_units": 1000,
				"smallest_currency_fraction_value": 0.001,
				"number_format": "#,###.###", "enabled": 1,
			}).insert(ignore_permissions=True)
		else:
			frappe.db.set_value("Currency", "KWD", "enabled", 1)
		frappe.db.set_value("Property", PROP_A, {
			"country": "Kuwait", "currency": "KWD", "gst_rate_low": 0, "gst_rate_high": 0,
		})
		frappe.clear_document_cache("Property", PROP_A)
		api._post_folio_charge(self.folio, "Food & Beverage", "Mezzeh 3dp", 12.375, gst_rate=0)
		api.collect_security_deposit(self.res.name, 25.375, "Cash")
		inv = api.folio_invoice(self.folio)
		self.assertEqual(inv["document"]["amount_in_words"], "KWD Twelve point 375 Only")
		self.assertEqual(api._stay_money(self.res)["deposit_held"], 25.375)
