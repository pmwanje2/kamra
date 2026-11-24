"""Round 8 of the rate matrix: the Gemini 4 Neon re-review (R7) of kamra#33.

P1-R7-1 a fixed rate on a Current Date plan is fixed on the bill too: its
ACTUAL reference currency converts at the booking's locked rate (OPERA Fixed
Rate), on the folio's bill and on a pro forma, never at the posting date's.
P1-R7-2 an added night the rate code now prices in another currency than the
booking's contract refuses the new dates: a rate code's currency change never
re-denominates an existing booking (OPERA), the contract is re-priced first.
P2 the resolver's scalar figures follow its first night's own extra-person
charge when the scalar one is left out.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from kamra.tests import test_rate_matrix_parity_r7 as r7
from kamra.tests.rate_matrix_harness import Doc, RateMatrixHarness
from kamra.tests.test_rate_matrix_parity_r6 import NIGHTS, STAY, stub_module


def invoice_localization():
	"""kamra.localization as folio_invoice reads it: one VAT, no statutory block."""
	pack = SimpleNamespace(calculate_room_tax=lambda *args: 0, invoice_context=lambda prop: {
		"sac": None, "place_of_supply": None, "tax_label": "VAT", "tax_id_label": "TIN"})
	return stub_module("kamra.localization", pack_for=lambda property: pack, default_currency=lambda pack: "UGX",
	                   tax_split=lambda pack, prop, tax_id: [("vat", 1)], service_code_for=lambda *args: None,
	                   invoice_print_block=lambda *args: None, invoice_title=lambda pack: "Tax Invoice",
	                   amount_in_words=lambda *args: "", invoice_tax_note=lambda pack: "")


class FixedRateReferenceCurrencyTest(RateMatrixHarness):
	"""P1-R7-1: a fixed-rate Current Date stay's bill and pro forma convert at the booking's locked rate."""

	modules = ("kamra.pricing", "kamra.folio", "kamra.authz", "kamra.api", "kamra.ledger")

	def extra_modules(self):
		return {"kamra.localization": invoice_localization(),
		        "kamra.checkout": stub_module("kamra.checkout", _NoTrace=self.no_trace)}

	@staticmethod
	@contextmanager
	def no_trace(name):
		yield

	def setUp(self):
		super().setUp()
		self.roles = ["Finance"]
		self.authz.restricted_properties = lambda user=None: None
		self.docs["Property", "Test Hotel"].update(business_date="2026-12-30", property_name="Test Hotel")
		self.plan("CORP", fx_lock_policy="Current Date")
		# USD 100 at 3,650 = 365,000: the manager's fixed rate (Track A), billed as booked
		self.res = self.add("Reservation", "RES-1", property="Test Hotel", rate_plan="CORP", status="Checked In",
		                    guest_name="Fixed Guest", check_in_date="2026-12-30", check_out_date="2027-01-01",
		                    agreed_rate=365000, fixed_rate=1, contract_tariff=json.dumps({
		                        "agreed_rate": 365000, "tariff": {
		                            "currency": "USD", "nightly": 100, "fx_rate": 3650, "fx_date": "2026-12-20",
		                            "property_currency": "UGX"}}))
		# the posting date's published rate the fixed rate never floats to
		self.rate("USD", 4000, "2026-12-30")
		self.rate("USD", 4000, "2026-12-31")
		line = dict(parenttype="Folio", parent="F-1", reservation="RES-1", qty=1, gst_rate=0, gst_amount=0,
		            posting_date="2026-12-30", auto_posted=1)
		# the night as post_room_night bills a fixed rate: the locked night, no contract FX
		self.charges = [Doc(line, name="FC-1", charge_type="Room", description="Room 101", rate=365000,
		                    amount=365000, total=365000),
		                Doc(line, name="FC-2", charge_type="F&B", description="Dinner", rate=36500, amount=36500,
		                    total=36500, auto_posted=0)]
		self.payments = [Doc(name="FP-1", posting_date="2026-12-30", payment_kind="Payment", mode="Cash",
		                     amount=182500, currency="UGX")]
		self.records["Folio Charge"] = self.charges
		folio = self.add("Folio", "F-1", property="Test Hotel", reservation="RES-1", folio_type="Guest",
		                 status="Open", guest_name="Fixed Guest", grand_total=401500, balance=219000,
		                 charges=self.charges, payments=self.payments)
		folio.as_dict = lambda: {key: value for key, value in folio.items() if not callable(value)}
		self.inserted = []
		harness_get_doc = self.frappe.get_doc

		def get_doc(doctype, name=None):
			if isinstance(doctype, dict):
				doc = Doc(doctype)
				doc.insert = lambda **kwargs: self.inserted.append(doc)
				doc.name = "PF-1"
				return doc
			return harness_get_doc(doctype, name)

		patcher = patch.object(self.frappe, "get_doc", get_doc)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_the_bill_converts_a_fixed_rate_stay_at_its_locked_rate(self):
		reference = self.api.folio_invoice("F-1")["stay"]["reference_currency"]
		self.assertEqual((reference["calculation"], reference["fx_rate"], reference["fx_date"]),
		                 ("ACTUAL", 3650.0, "2026-12-20"))
		self.assertNotIn("posted_nights", reference)
		# 401,500 and 219,000 at the locked 3,650: not the night at 4,000 (USD 91.25) the header contradicts
		self.assertEqual((reference["grand_total_reference"], reference["balance_reference"]), (110.0, 60.0))

	def test_the_pro_forma_converts_a_fixed_rate_stay_s_estimate_at_its_locked_rate(self):
		# the night check-out would post: a fixed rate posts the locked night with no contract FX
		projected = [{"posting_date": "2026-12-31", "charge_type": "Room", "description": "Room 101", "qty": 1,
		              "rate": 365000, "amount": 365000, "gst_rate": 0, "gst_amount": 0, "total": 365000,
		              "projected": True}]
		with patch.object(self.ledger, "_projected_lines", lambda folio: projected):
			invoice = self.ledger.generate_proforma("F-1")["invoice"]
		self.assertEqual((invoice["estimate"]["grand_total"], invoice["estimate"]["balance"]), (766500, 584000))
		reference = invoice["stay"]["reference_currency"]
		self.assertEqual((reference["fx_rate"], reference["fx_date"]), (3650.0, "2026-12-20"))
		self.assertNotIn("posted_nights", reference)
		self.assertEqual((reference["grand_total_reference"], reference["balance_reference"]), (210.0, 160.0))


class AddedNightCurrencyTest(r7.ContractStayAmendmentTest):
	"""P1-R7-2: an added night priced in another currency than the booking's contract refuses the new dates."""

	def test_an_added_night_in_another_currency_refuses_the_new_dates(self):
		resolver = self.resolver

		def now_in_euros(form):
			# the rate plan and its price list moved from USD to EUR after the booking
			return {**resolver(form), "currency": "EUR", "nightly": 150, "agreed_rate": 600000,
			        "price_list": "Contract EUR"}

		self.tariffs["CORP"] = now_in_euros
		for case in ("save", "preview"):
			with self.subTest(case):
				self.res = self.booked()
				with self.assertRaisesRegex(self.pricing.ContractRateRefused, "EUR") as refused:
					if case == "save":
						self.amend(STAY[2], "2027-01-03")
					else:
						self.api.amend_stay_preview("RES-1", STAY[2], "2027-01-03")
				self.assertIn("USD", str(refused.exception))
				# nothing was written under the booking's USD: its contract is as booked
				self.assertNotIn("2027-01-02", [night["date"] for night in self.snapshot()["tariff"]["nights"]])


# the amendment fixture only: its own tests run in test_rate_matrix_parity_r7
for _name in [name for name in vars(r7.ContractStayAmendmentTest) if name.startswith("test_")]:
	setattr(AddedNightCurrencyTest, _name, None)
del _name


class ScalarExtraPersonTest(RateMatrixHarness):
	"""P2-R7-1: with no scalar extra-person charge, the scalar figures take the first night's own."""

	def test_the_scalar_figures_follow_the_first_night_s_own_extra_person_charge(self):
		self.plan("CON")
		self.plan("CON-NR", base_rate_plan="CON", modifier_type="Percent", modifier_value=-10, rounding_step=1000)
		nights = [dict(night, extra_person=20) for night in NIGHTS]
		self.tariffs["CON"] = self.tariff(nightly=100, agreed_rate=365000, nights=nights)  # no scalar extra_person
		agreed, tariff = self.pricing.contract_rate(*STAY[:2], "CON-NR", *STAY[2:])
		first = tariff["nights"][0]
		# USD 100 with USD 20 of extra persons: 80 less 10% = 72, plus 20 = 92 (not 90 off no extra persons)
		self.assertEqual((first["nightly"], first["agreed_rate"]), (92.0, 336000.0))
		self.assertEqual((tariff["nightly"], agreed), (first["nightly"], first["agreed_rate"]))
