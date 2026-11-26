"""Posted Deposit balances, final invoices and concurrent Guest receipts."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier, BrokenBarrierError
from unittest.mock import patch

import frappe
from frappe.model.base_document import BaseDocument
from frappe.tests import IntegrationTestCase

from kamra import api, folio, ledger, payments
from kamra.tests import test_advance_3dp_precision_r9 as r9
from kamra.tests.audit_fixtures import TODAY, AuditTestCase, make_property


class TestAdvancePrecisionR10(AuditTestCase):
	properties = r9.TestAdvancePrecisionR9.properties
	fixture = r9.TestAdvancePrecisionR9.fixture
	synchronize_fixture = r9.TestAdvancePrecisionR9.synchronize_fixture
	saved = r9.TestAdvancePrecisionR9.saved

	def test_native_reversal_reconciles_siblings(self):
		for case in r9.TestAdvancePrecisionR9.cases:
			currency, _, _, unit = case
			with self.subTest(currency=currency):
				_, windows, _ = self.fixture(*case)
				doc = frappe.get_doc("Folio", windows[0])
				receipt = next(p for p in doc.payments if p.payment_kind == "Security Deposit")
				entry = frappe.db.get_value("Folio Ledger Entry", {
					"folio": doc.name, "ledger": "Deposit", "source_name": receipt.name})
				doc.append("payments", {
					"posting_date": TODAY, "payment_kind": "Refund", "mode": "Cash",
					"amount": -receipt.amount, "currency": currency,
				})
				folio._recalculate(doc)
				doc.save()
				ledger.write_ledger_entry(
					property=doc.property, folio=doc.name, ledger="Deposit", transaction_code="9004",
					debit=receipt.amount, reversal_of=entry, source_doctype="Folio Payment",
					source_name=doc.payments[-1].name,
				)
				self.assertEqual(self.saved(windows)[2], (-unit, unit))
				for name, saved in zip(windows, self.saved(windows)):
					window = frappe.get_doc("Folio", name)
					folio._recalculate(window)
					self.assertEqual(saved, (window.payments_total, window.balance))

	def test_closed_invoice_keeps_its_historical_refund(self):
		for case in r9.TestAdvancePrecisionR9.cases:
			currency, _, returned, unit = case
			with self.subTest(currency=currency):
				_, windows, _ = self.fixture(*case)
				first, _, third = windows
				charge = api.add_folio_charge(third, "Misc", "Room service", unit)
				if charge["balance"]:
					api.add_folio_payment(third, "Cash", charge["balance"])
				invoice = folio.close_folio(third)
				closed_paid, balance = self.saved([third])[0]
				self.assertEqual(balance, 0)
				api.refund_folio_payment(first, returned, "Cash", reason="Advance adjustment")
				expected = Decimal(10 * unit) - Decimal(str(returned))
				self.assertEqual(self.saved(windows), [(expected, -expected), (0, 0), (closed_paid, 0)])
				doc = frappe.get_doc("Folio", third)
				folio._recalculate(doc)
				self.assertEqual((doc.invoice_number, doc.status, doc.payments_total, doc.balance),
				                 (invoice, "Closed", float(closed_paid), 0))

	def test_payment_link_failure_can_be_retried_once(self):
		for case in r9.TestAdvancePrecisionR9.cases:
			currency, _, returned, unit = case
			with self.subTest(currency=currency):
				res, windows, _ = self.fixture(*case)
				first, _, third = windows
				api.refund_folio_payment(first, returned, "Cash", reason="Advance adjustment")
				before = self.saved(windows)
				advance = frappe.db.get_value("Reservation", res.name, "advance_paid")
				name = "test-link-" + currency
				rows = frappe.db.count("Folio Ledger Entry", {"folio": third})
				with patch("kamra.folio.sync_deposit_payment_totals", side_effect=RuntimeError("sync failed")):
					with self.assertRaisesRegex(RuntimeError, "sync failed"):
						payments.settle_payment_link(third, name, unit)
				self.assertEqual(self.saved(windows), before)
				self.assertEqual(frappe.db.get_value("Reservation", res.name, "advance_paid"), advance)
				self.assertEqual(frappe.db.count("Folio Payment", {"parent": third, "reference": name}), 0)
				self.assertEqual(frappe.db.count("Folio Ledger Entry", {"folio": third}), rows)
				self.assertTrue(payments.settle_payment_link(third, name, unit))
				self.assertFalse(payments.settle_payment_link(third, name, unit))
				paid = before[0][0] + unit
				self.assertEqual(self.saved(windows), [(paid, -paid), (0, 0), (0, 0)])
				self.assertEqual(frappe.db.get_value("Reservation", res.name, "advance_paid"), advance + unit)


class TestKeyedSiblingReceipts(IntegrationTestCase):
	def test_concurrent_receipts_both_commit(self):
		site = frappe.local.site
		fixture = r9.TestAdvancePrecisionR9()
		for case in fixture.cases:
			with self.subTest(currency=case[0]):
				# Workers require committed fixtures. Track only rows inserted by this
				# case so teardown also removes its committed ledger and child rows.
				inserted = []
				insert = BaseDocument.db_insert

				def tracking_insert(doc, *args, **kwargs):
					result = insert(doc, *args, **kwargs)
					inserted.append((doc.doctype, doc.name))
					return result

				prop = "UT R10 " + frappe.generate_hash(length=10)
				try:
					with patch.object(BaseDocument, "db_insert", tracking_insert), patch.object(
						r9, "make_property", side_effect=lambda name, **kw: make_property(prop, **kw),
					):
						_, windows, _ = fixture.fixture(*case)
						frappe.db.commit()  # nosemgrep: frappe-manual-commit -- publish the worker fixture
						start, at_recalc = Barrier(2), Barrier(2)
						recalculate = folio._recalculate

						def overlap(doc, **kwargs):
							# Where writers serialize before recalculation the second
							# cannot arrive until the first commits.
							try:
								at_recalc.wait(timeout=2)
							except BrokenBarrierError:
								pass
							return recalculate(doc, **kwargs)

						def receive(name):
							frappe.init(site=site)
							try:
								frappe.connect()
								frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test worker
								start.wait(timeout=15)
								api.add_folio_payment(name, "Cash", case[3], kind="Payment", idempotency_key=name)
								frappe.db.commit()  # nosemgrep: frappe-manual-commit -- independent receipt
							finally:
								frappe.destroy()

						with patch.object(folio, "_recalculate", overlap), ThreadPoolExecutor(2) as workers:
							list(workers.map(receive, (windows[0], windows[2])))
						for name in (windows[0], windows[2]):
							self.assertEqual(frappe.db.count("Folio Payment", {
								"parent": name, "idempotency_key": name}), 1)
						unit = case[3]
						self.assertEqual(fixture.saved(windows), [(11 * unit, -11 * unit), (0, 0), (2 * unit, -2 * unit)])
				finally:
					for doctype, name in reversed(inserted):
						frappe.db.delete(doctype, {"name": name})
					frappe.db.commit()  # nosemgrep: frappe-manual-commit -- remove this case's committed fixtures
					frappe.clear_document_cache("Property", prop)
