"""RED->GREEN tests for P18 / K2 Receipt Transfers, Folio Windows Overview, and K3 Credit & Rebill.

Runs both siteless schema/API-surface contracts (host Python) and full live
Frappe/MariaDB integration tests (bench run-tests) across UGX (0dp), USD (2dp),
and KWD (3dp).
"""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

KAMRA_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile(
	r"(?<![a-z0-9])(?:Kamra|Frappe|ERPNext|URY|HUF|Tridz|BuildSuite)(?![a-z0-9])|kamrapms\.com|HeyKoala",
	re.I,
)


class TestP18K2SchemaAndSurface(unittest.TestCase):
	"""Siteless contract tests verifying K2 schema fields and payment_transfers API surface."""

	def test_folio_payment_json_has_k2_transfer_fields(self):
		fp_path = KAMRA_ROOT / "kamra" / "doctype" / "folio_payment" / "folio_payment.json"
		doc = json.loads(fp_path.read_text(encoding="utf-8"))
		field_order = doc.get("field_order") or []
		fields = {f["fieldname"]: f for f in doc.get("fields") or []}

		for name in ("transferred_from", "transferred_to", "transfer_group_key"):
			self.assertIn(name, field_order, f"Folio Payment.field_order missing {name}")
			self.assertIn(name, fields, f"Folio Payment.fields missing {name}")
			self.assertEqual(fields[name].get("read_only"), 1, f"{name} must be read_only=1")
			self.assertEqual(fields[name].get("no_copy"), 1, f"{name} must be no_copy=1")

		self.assertEqual(fields["transferred_from"].get("fieldtype"), "Link")
		self.assertEqual(fields["transferred_from"].get("options"), "Folio")
		self.assertEqual(fields["transferred_to"].get("fieldtype"), "Link")
		self.assertEqual(fields["transferred_to"].get("options"), "Folio")
		self.assertEqual(fields["transfer_group_key"].get("fieldtype"), "Data")

	def test_payment_transfers_module_exports_whitelisted_apis_and_is_white_label(self):
		mod_path = KAMRA_ROOT / "payment_transfers.py"
		self.assertTrue(mod_path.exists(), "kamra/payment_transfers.py must exist")
		source = mod_path.read_text(encoding="utf-8")
		tree = ast.parse(source)

		funcs = {
			node.name: node
			for node in tree.body
			if isinstance(node, ast.FunctionDef)
		}
		for fn_name, required_params in (
			("transfer_folio_payment", {"from_folio", "payment_row", "to_folio", "reason", "amount", "pin", "idempotency_key"}),
			("folio_windows_overview", {"folio"}),
			("credit_and_rebill", {"folio", "reason", "mode", "amount", "pin"}),
			("update_folio_window", {"folio", "payee_type", "payee_company", "payee_agent", "payment_method"}),
		):
			self.assertIn(fn_name, funcs, f"kamra.payment_transfers.{fn_name} missing")
			node = funcs[fn_name]
			decos = [ast.unparse(d) for d in node.decorator_list]
			self.assertTrue(
				any("whitelist" in d for d in decos),
				f"{fn_name} must be decorated with @frappe.whitelist",
			)
			params = {a.arg for a in node.args.args}
			self.assertTrue(
				required_params.issubset(params),
				f"{fn_name} missing params {required_params - params}",
			)

		# User-facing messages must be 100% white-label
		leaks = []
		for node in ast.walk(tree):
			if isinstance(node, ast.Call):
				fn = ast.unparse(node.func)
				if fn in {"_", "frappe._", "frappe.throw", "frappe.msgprint"}:
					for arg in node.args[:1]:
						for sub in ast.walk(arg):
							if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and FORBIDDEN.search(sub.value):
								leaks.append(f"L{sub.lineno}: {sub.value}")
		self.assertEqual(leaks, [], f"Vendor leaks in kamra/payment_transfers.py: {leaks}")


try:
	import frappe
	from frappe.utils import add_days, getdate
	from kamra import api, ledger, settings
	from kamra import folio as folio_mod
	from kamra.tests.audit_fixtures import (
		TODAY,
		AuditTestCase,
		ensure_user,
		make_property,
		make_reservation,
		make_room,
		make_room_type,
	)
except ImportError:  # siteless host unittest run
	frappe = None


if frappe is not None:
	PROP_KWD = "UT P18 KWD"
	PROP_UGX = "UT P18 UGX"
	PROP_USD = "UT P18 USD"
	PROP_OTHER = "UT P18 Other"

	def _ensure_currency(code: str, symbol: str, fraction_units: int):
		if not frappe.db.exists("Currency", code):
			frappe.get_doc({
				"doctype": "Currency",
				"currency_name": code,
				"symbol": symbol,
				"fraction": "Unit",
				"fraction_units": fraction_units,
				"enabled": 1,
			}).insert(ignore_permissions=True)
		else:
			frappe.db.set_value("Currency", code, {
				"enabled": 1,
				"fraction_units": fraction_units,
			}, update_modified=False)

	def _grant_property(user: str, prop: str):
		for existing in frappe.get_all("User Permission", filters={"user": user, "allow": "Property"}, pluck="name"):
			frappe.delete_doc("User Permission", existing, ignore_permissions=True)
		frappe.get_doc({
			"doctype": "User Permission",
			"user": user,
			"allow": "Property",
			"for_value": prop,
		}).insert(ignore_permissions=True)
		frappe.clear_cache(user=user)

	def _set_cashier_pin(user: str, pin_value: str):
		if frappe.db.exists("Cashier PIN", user):
			doc = frappe.get_doc("Cashier PIN", user)
			doc.pin = pin_value
			doc.must_reset = 0
			doc.pin_attempts = 0
			doc.locked_until = None
			doc.save(ignore_permissions=True)
		else:
			frappe.get_doc({
				"doctype": "Cashier PIN",
				"user": user,
				"pin": pin_value,
				"must_reset": 0,
			}).insert(ignore_permissions=True)
		frappe.cache.delete_value(f"kamra_cashier_unlock:{user}")

	class TestP18K2ReceiptTransfer(AuditTestCase):
		properties = (PROP_KWD, PROP_UGX, PROP_USD, PROP_OTHER)

		def setUp(self):
			super().setUp()
			self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
			_ensure_currency("KWD", "KD", 1000)
			_ensure_currency("UGX", "UGX", 1)
			_ensure_currency("USD", "$", 100)
			ledger.ensure_default_transaction_codes()
			from kamra.masters import ensure_masters
			ensure_masters()
			self.day0 = str(getdate(TODAY))
			self.day1 = str(add_days(self.day0, 1))
			self.day2 = str(add_days(self.day0, 2))

		def _setup_stay(self, prop_name: str, currency: str, country: str, room_no: str = "101"):
			make_property(
				prop_name,
				country=country,
				currency=currency,
				business_date=self.day0,
				gst_mode="Fixed",
				gst_rate_low=0,
				gst_rate_high=0,
				rates_include_tax=0,
				require_cashier_pin=0,
			)
			settings.clear_cache(prop_name)
			rt = make_room_type(prop_name, "DLX", base_price=200, adults_capacity=4)
			room = make_room(prop_name, rt, room_no)
			res = make_reservation(prop_name, rt, self.day0, self.day2, status="Checked In", room=room)
			w1 = folio_mod.open_folio(res)
			return rt, room, res, w1

		def test_k2_receipt_transfer_roundtrip_kwd_3dp_and_source_before_target_save_order(self):
			from kamra import payment_transfers
			from kamra.kamra.doctype.folio.folio import Folio as FolioDoc

			_, _, res, w1 = self._setup_stay(PROP_KWD, "KWD", "Kuwait")
			w2 = folio_mod.open_window(reservation=res.name, payee_type="Guest", bill_to_name="KWD Window 2")

			# Post 200.000 KWD receipt on W1
			api.add_folio_payment(w1, "Cash", 200.000, reference="RCPT-KWD-01", kind="Payment")
			src_before = frappe.get_doc("Folio", w1)
			pay_row = src_before.payments[-1].name

			save_order = []
			orig_save = FolioDoc.save

			def spy_save(doc_self, *args, **kwargs):
				last_p = doc_self.payments[-1] if doc_self.payments else None
				save_order.append((
					doc_self.name,
					float(last_p.amount) if last_p else None,
					last_p.get("transferred_to") if last_p else None,
					last_p.get("transferred_from") if last_p else None,
				))
				return orig_save(doc_self, *args, **kwargs)

			with patch.object(FolioDoc, "save", spy_save):
				out = payment_transfers.transfer_folio_payment(
					from_folio=w1,
					payment_row=pay_row,
					to_folio=w2,
					reason="Split KWD receipt to second window",
					amount=75.125,
					idempotency_key="k2-kwd-001",
				)

			self.assertTrue(out.get("ok"))
			self.assertAlmostEqual(out["amount"], 75.125, places=3)
			# Strict bridge@30 save sequence: source negative row BEFORE target positive row
			self.assertGreaterEqual(len(save_order), 2)
			self.assertEqual(save_order[0], (w1, -75.125, w2, None))
			self.assertEqual(save_order[1], (w2, 75.125, None, w1))

			src_after = frappe.get_doc("Folio", w1)
			dst_after = frappe.get_doc("Folio", w2)
			self.assertAlmostEqual(float(src_after.payments_total), 124.875, places=3)
			self.assertAlmostEqual(float(dst_after.payments_total), 75.125, places=3)

			src_neg = next(p for p in src_after.payments if p.name == out["source_row"])
			dst_pos = next(p for p in dst_after.payments if p.name == out["target_row"])
			self.assertEqual(src_neg.transferred_to, w2)
			self.assertFalse(src_neg.transferred_from)
			self.assertEqual(dst_pos.transferred_from, w1)
			self.assertFalse(dst_pos.transferred_to)
			self.assertEqual(src_neg.transfer_group_key, dst_pos.transfer_group_key)
			self.assertTrue(src_neg.transfer_group_key)
			self.assertEqual(src_neg.mode, "Cash")
			self.assertEqual(dst_pos.mode, "Cash")
			self.assertEqual(src_neg.currency, "KWD")
			self.assertEqual(dst_pos.currency, "KWD")

			# Four-ledger reconciliation must remain balanced
			recon = ledger.folio_reconciliation(PROP_KWD)
			self.assertTrue(recon["in_balance"], f"Ledger out of balance after K2 transfer: {recon}")

			# Agent Action Log entry written
			self.assertTrue(frappe.db.exists("Agent Action Log", {
				"action": "payment_transfer",
				"target_doctype": "Folio",
				"target_name": w2,
			}))

			# Idempotent replay returns same result without adding rows
			count_src = len(src_after.payments)
			count_dst = len(dst_after.payments)
			replay = payment_transfers.transfer_folio_payment(
				from_folio=w1,
				payment_row=pay_row,
				to_folio=w2,
				reason="Split KWD receipt to second window",
				amount=75.125,
				idempotency_key="k2-kwd-001",
			)
			self.assertEqual(replay.get("idempotent_replay"), 1)
			self.assertEqual(len(frappe.get_doc("Folio", w1).payments), count_src)
			self.assertEqual(len(frappe.get_doc("Folio", w2).payments), count_dst)

			# Over-transfer beyond remaining 124.875 fails
			with self.assertRaises(frappe.ValidationError):
				payment_transfers.transfer_folio_payment(
					from_folio=w1, payment_row=pay_row, to_folio=w2,
					reason="Too much", amount=125.000,
				)

			# Sub-minor fraction 0.0004 KWD fails
			with self.assertRaises(frappe.ValidationError):
				payment_transfers.transfer_folio_payment(
					from_folio=w1, payment_row=pay_row, to_folio=w2,
					reason="Sub-fils", amount=0.0004,
				)

			# Second hop from transferred_from row is refused (bridge@30 one-hop rule)
			with self.assertRaises(frappe.ValidationError):
				payment_transfers.transfer_folio_payment(
					from_folio=w2, payment_row=dst_pos.name, to_folio=w1,
					reason="Second hop attempt", amount=10.000,
				)

		def test_k2_receipt_transfer_ugx_0dp_usd_2dp_guards_and_cross_property_403(self):
			from kamra import payment_transfers

			# UGX 0dp precision
			_, _, res_ug, w1_ug = self._setup_stay(PROP_UGX, "UGX", "Uganda", room_no="201")
			w2_ug = folio_mod.open_window(reservation=res_ug.name, payee_type="Guest", bill_to_name="UGX W2")
			api.add_folio_payment(w1_ug, "Cash", 100000, reference="UG-1", kind="Payment")
			pay_ug = frappe.get_doc("Folio", w1_ug).payments[-1].name

			with self.assertRaises(frappe.ValidationError):
				payment_transfers.transfer_folio_payment(
					from_folio=w1_ug, payment_row=pay_ug, to_folio=w2_ug,
					reason="Fractional shilling", amount=10.5,
				)

			ug_out = payment_transfers.transfer_folio_payment(
				from_folio=w1_ug, payment_row=pay_ug, to_folio=w2_ug,
				reason="Move whole UGX", amount=40000,
			)
			self.assertEqual(ug_out["amount"], 40000.0)
			self.assertTrue(ledger.folio_reconciliation(PROP_UGX)["in_balance"])

			# Reason required and max 300 chars
			with self.assertRaises(frappe.ValidationError):
				payment_transfers.transfer_folio_payment(
					from_folio=w1_ug, payment_row=pay_ug, to_folio=w2_ug,
					reason="   ", amount=1000,
				)
			with self.assertRaises(frappe.ValidationError):
				payment_transfers.transfer_folio_payment(
					from_folio=w1_ug, payment_row=pay_ug, to_folio=w2_ug,
					reason="x" * 301, amount=1000,
				)

			# USD 2dp + closed/invoiced folio refusal
			_, _, res_us, w1_us = self._setup_stay(PROP_USD, "USD", "United States", room_no="301")
			w2_us = folio_mod.open_window(reservation=res_us.name, payee_type="Guest", bill_to_name="USD W2")
			api.add_folio_payment(w1_us, "Cash", 100.00, reference="US-1", kind="Payment")
			pay_us = frappe.get_doc("Folio", w1_us).payments[-1].name

			with self.assertRaises(frappe.ValidationError):
				payment_transfers.transfer_folio_payment(
					from_folio=w1_us, payment_row=pay_us, to_folio=w2_us,
					reason="Sub-cent USD", amount=10.005,
				)

			us_out = payment_transfers.transfer_folio_payment(
				from_folio=w1_us, payment_row=pay_us, to_folio=w2_us,
				reason="Partial USD transfer", amount=33.33,
			)
			self.assertAlmostEqual(us_out["amount"], 33.33, places=2)

			# Close W2 -> further transfer into closed W2 fails with KMR-CO-001
			frappe.db.set_value("Folio", w2_us, {"status": "Closed", "invoice_number": "INV-USD-CLOSE"})
			with self.assertRaisesRegex(frappe.ValidationError, "KMR-CO-001"):
				payment_transfers.transfer_folio_payment(
					from_folio=w1_us, payment_row=pay_us, to_folio=w2_us,
					reason="Into closed window", amount=10.00,
				)

			# Cross-property isolation: clerk limited to PROP_OTHER gets PermissionError on PROP_UGX
			_, _, _, _ = self._setup_stay(PROP_OTHER, "USD", "United States", room_no="401")
			clerk_other = ensure_user("p18_other_clerk@example.test", ["Front Desk"])
			_grant_property(clerk_other, PROP_OTHER)
			prev_user = frappe.session.user
			try:
				frappe.set_user(clerk_other)
				with self.assertRaises(frappe.PermissionError):
					payment_transfers.transfer_folio_payment(
						from_folio=w1_ug, payment_row=pay_ug, to_folio=w2_ug,
						reason="Cross property attempt", amount=5000,
					)
			finally:
				frappe.set_user(prev_user)

			# Cashier PIN enforcement when property requires PIN
			clerk_ug = ensure_user("p18_ug_clerk@example.test", ["Front Desk"])
			_grant_property(clerk_ug, PROP_UGX)
			_set_cashier_pin(clerk_ug, "2468")
			frappe.db.set_value("Property", PROP_UGX, "require_cashier_pin", 1)
			frappe.clear_document_cache("Property", PROP_UGX)
			try:
				frappe.set_user(clerk_ug)
				with self.assertRaises(frappe.ValidationError):
					payment_transfers.transfer_folio_payment(
						from_folio=w1_ug, payment_row=pay_ug, to_folio=w2_ug,
						reason="Missing PIN", amount=5000,
					)
				pin_ok = payment_transfers.transfer_folio_payment(
					from_folio=w1_ug, payment_row=pay_ug, to_folio=w2_ug,
					reason="With valid PIN", amount=5000, pin="2468",
				)
				self.assertTrue(pin_ok.get("ok"))
			finally:
				frappe.set_user(prev_user)

		def test_folio_windows_overview_and_update_window_metadata(self):
			from kamra import payment_transfers

			_, _, res, w1 = self._setup_stay(PROP_USD, "USD", "United States", room_no="302")
			corp = frappe.get_doc({
				"doctype": "Corporate Account",
				"company_name": "UT P18 Window Corp",
				"credit_allowed": 1,
			}).insert(ignore_permissions=True).name
			w2 = folio_mod.open_window(
				reservation=res.name, payee_type="Company", payee_company=corp, payment_method="Company Credit",
			)
			w3 = folio_mod.open_window(reservation=res.name, payee_type="Guest", bill_to_name="Guest Extra")

			# Add routing rule targeting W2
			frappe.get_doc({
				"doctype": "Routing Instruction",
				"property": PROP_USD,
				"reservation": res.name,
				"target_type": "Window",
				"target_window": 2,
				"codes": [{"charge_type": "Laundry"}],
			}).insert(ignore_permissions=True)

			overview = payment_transfers.folio_windows_overview(w1)
			self.assertEqual([w["window_no"] for w in overview["windows"]], [1, 2, 3])
			by_no = {w["window_no"]: w for w in overview["windows"]}
			self.assertEqual(by_no[2]["payee_type"], "Company")
			self.assertEqual(by_no[2]["payee_company"], corp)
			self.assertEqual(by_no[2]["routed_rule_count"], 1)
			self.assertEqual(by_no[3]["payee_name"], "Guest Extra")
			self.assertTrue(overview["can_add_window"])

			# Edit payee/method on open uninvoiced window W3
			upd = payment_transfers.update_folio_window(
				folio=w3,
				payee_type="Company",
				payee_company=corp,
				payment_method="Company Credit",
			)
			self.assertEqual(upd["payee_type"], "Company")
			self.assertEqual(upd["payee_company"], corp)

			# Editing a closed/invoiced window fails with KMR-CN-001
			frappe.db.set_value("Folio", w3, {"status": "Closed", "invoice_number": "INV-W3-LOCK"})
			with self.assertRaisesRegex(frappe.ValidationError, "KMR-CN-001"):
				payment_transfers.update_folio_window(folio=w3, payee_type="Guest")

		def test_credit_and_rebill_delegates_full_and_partial_with_role_guard(self):
			from kamra import payment_transfers

			_, _, res, w1 = self._setup_stay(PROP_USD, "USD", "United States", room_no="303")
			api.add_folio_charge(w1, "Laundry", "Suit press", 100.00, gst_rate=0)
			api.add_folio_payment(w1, "Cash", 100.00, reference="SETTLE-1", kind="Payment")
			api.close_folio(w1)

			# Front Desk user cannot run credit_and_rebill
			fd = ensure_user("p18_fd_nocredit@example.test", ["Front Desk"])
			_grant_property(fd, PROP_USD)
			prev_user = frappe.session.user
			try:
				frappe.set_user(fd)
				with self.assertRaises(frappe.PermissionError):
					payment_transfers.credit_and_rebill(folio=w1, reason="Not allowed", mode="partial", amount=20.00)
			finally:
				frappe.set_user(prev_user)

			# Finance user executes partial credit note
			fin = ensure_user("p18_fin_credit@example.test", ["Finance"])
			_grant_property(fin, PROP_USD)
			try:
				frappe.set_user(fin)
				partial = payment_transfers.credit_and_rebill(
					folio=w1, reason="Partial service adjustment", mode="partial", amount=25.50,
				)
				self.assertEqual(partial["mode"], "partial")
				self.assertTrue(partial.get("credit_note"))

				# Full credit & rebill on standalone closed folio delegates to cancel_invoice
				full = payment_transfers.credit_and_rebill(
					folio=w1, reason="Full invoice cancellation and rebill", mode="full",
				)
				self.assertEqual(full["mode"], "full")
				self.assertTrue(full.get("cancelled"))
			finally:
				frappe.set_user(prev_user)
