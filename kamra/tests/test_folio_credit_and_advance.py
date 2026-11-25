"""Regression coverage for the desk's collectible balance and advance."""
from unittest.mock import patch

import frappe

from kamra import api, folio, receivables, settings
from kamra.tests.audit_fixtures import (
	PROP_A, TODAY, AuditTestCase, d, make_property,
	make_reservation, make_room, make_room_type,
)
from kamra.tests.bridge_fields import ensure_bridge_field


class FolioCreditAndAdvanceTest(AuditTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_bridge_field("Folio", "sales_invoice")

	def setUp(self):
		super().setUp()
		make_property(PROP_A, business_date=TODAY)
		self.room_type = make_room_type(PROP_A)

	def test_bridged_invoice_in_reservation_windows(self):
		res = make_reservation(PROP_A, self.room_type, TODAY, d(2))
		name = folio.open_folio(res)
		frappe.db.set_value("Folio", name, {"sales_invoice": "SI-DESK", "invoice_number": None})
		self.assertEqual(api.reservation_folios(res.name)[0].get("sales_invoice"), "SI-DESK")

	def test_credit_status_uses_submitted_returns_and_clears_when_cancelled(self):
		source = frappe._dict(name="FO-TEST", property=PROP_A, sales_invoice="SI-DESK",
		                     grand_total=100, balance=100)
		for code, balance, amount, remaining in (
			("UGX", 446, "445.51", 0),
			("USD", 4.46, "4.455", 0),
			("KWD", 4.46, "4.455", 0.005),
		):
			with self.subTest(currency=code):
				make_property(PROP_A, currency=code)
				source.update(balance=balance, grand_total=balance)
				with self._sales_returns([frappe._dict(
					name="CN-DESK", return_against="SI-DESK", currency=code,
					grand_total="-" + amount, rounded_total=0,
				)]) as query:
					got = folio.reconcile_credit_note_status(source)
					self.assertEqual(folio.reconcile_credit_note_status(source), got)
					self.assertEqual(got["net_collectible_balance"], remaining)
					self.assertEqual(got["credit_notes"], ["CN-DESK"])
					self.assertEqual(got["credited_amount"], balance - remaining if code != "KWD" else 4.455)
					self.assertEqual(got["credit_status"], "Partially credited" if remaining else "Credited")
					self.assertEqual(source.balance, balance)
					self.assertEqual(query[-1]["docstatus"], 1)
					self.assertEqual(query[-1]["is_return"], 1)
				with self._sales_returns([]):
					self.assertEqual(folio.reconcile_credit_note_status(source)["net_collectible_balance"], balance)

	def test_fully_credited_departed_stay_is_not_owed(self):
		res = make_reservation(PROP_A, self.room_type, d(-2), d(-1), status="Checked Out")
		name = folio.open_folio(res)
		frappe.db.set_value("Folio", name, {"sales_invoice": "SI-OWING", "grand_total": 100, "balance": 100})
		with self._sales_returns([frappe._dict(name="CN-OWING", return_against="SI-OWING",
		                                     currency="INR", grand_total=-100, rounded_total=0)]):
			self.assertNotIn(name, [r["folio"] for r in receivables.guests_owing(PROP_A)])
		with self._sales_returns([frappe._dict(name="CN-PART", return_against="SI-OWING",
		                                     currency="INR", grand_total=-75, rounded_total=0)]):
			owing = {r["folio"]: r["balance"] for r in receivables.guests_owing(PROP_A)}
			self.assertEqual(owing[name], 25)
		self.assertEqual(frappe.db.get_value("Folio", name, "balance"), 100)

	def _sales_returns(self, returns):
		from contextlib import contextmanager

		@contextmanager
		def mocked():
			exists = frappe.db.exists
			get_all = frappe.get_all
			queries = []

			def query(doctype, *args, **kwargs):
				if doctype == "Sales Invoice":
					queries.append(kwargs["filters"])
					return returns
				return get_all(doctype, *args, **kwargs)

			def doc_exists(doctype, name=None, *args, **kwargs):
				if (doctype, name) == ("DocType", "Sales Invoice"):
					return True
				return exists(doctype, name, *args, **kwargs)

			with patch.object(frappe, "get_all", side_effect=query), patch.object(frappe.db, "exists", side_effect=doc_exists):
				yield queries

		return mocked()

	def test_post_check_in_refund_clears_advance_at_property_precision(self):
		room = make_room(PROP_A, self.room_type, "111")
		settings.write_kv(PROP_A, "use_cashier_tills", 0)
		settings.clear_cache(PROP_A)
		for i, (code, paid) in enumerate((("UGX", 11), ("USD", 11.12), ("KWD", 11.123))):
			with self.subTest(currency=code):
				make_property(PROP_A, currency=code)
				res = make_reservation(PROP_A, self.room_type, d(3 + i * 3), d(5 + i * 3), room=room)
				name = api.record_advance(res.name, paid)["folio"]
				frappe.db.set_value("Reservation", res.name, "status", "Checked In")
				from kamra.ledger import transfer_advances_to_guest
				transfer_advances_to_guest(res.name)
				api.refund_folio_payment(name, paid, "Cash", "Guest requested advance back")
				self.assertEqual(frappe.db.get_value("Folio", name, "payments_total"), 0)
				self.assertEqual(frappe.db.get_value("Reservation", res.name, "advance_paid"), 0)

	def test_reservation_search_exposes_room_number_and_keeps_identity(self):
		room = make_room(PROP_A, self.room_type, "111")
		res = make_reservation(PROP_A, self.room_type, TODAY, d(2), room=room)
		row = api.find_reservations(PROP_A, query=res.name)[0]
		self.assertEqual(row["room"], room)
		self.assertEqual(row.get("room_number"), "111")

	def test_finance_schema_uses_a_customer_facing_role_hint(self):
		module = next(m for m in settings.settings_schema(PROP_A)["modules"] if m["id"] == "finance")
		spec = next(s for sec in module["sections"] for s in sec["settings"] if s["key"] == "rate_override_limits_by_role")
		self.assertNotIn("Kamra Agent", spec["hint"])

	def test_folio_desk_has_no_label_for_column_break(self):
		import json
		from pathlib import Path
		path = Path(folio.__file__).parent / "kamra" / "doctype" / "folio" / "folio.json"
		fields = json.loads(path.read_text())["fields"]
		self.assertFalse(any(f["fieldtype"] == "Column Break" and f.get("label") == "Column Break B" for f in fields))
