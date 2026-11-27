import json
from decimal import Decimal
from pathlib import Path
from unittest import TestCase


class OutletTip(TestCase):
	def test_tip_schema_supports_non_revenue(self):
		root = Path(__file__).resolve().parents[1] / "kamra" / "doctype"
		for doc, field, value in (("folio_charge", "charge_type", "Outlet Tip"),
		                          ("transaction_code", "txn_type", "Non-Revenue")):
			schema = json.loads((root / doc / (doc + ".json")).read_text())
			options = next(f["options"] for f in schema["fields"] if f["fieldname"] == field)
			self.assertIn(value, options.splitlines())

	def test_tip_rounds_half_up_and_rejects_negative_before_rounding(self):
		from kamra.room_charge_money import rounded_tip
		for currency, raw, expected in (("UGX", "1.5", "2"), ("USD", ".015", ".02"), ("KWD", ".0015", ".002")):
			self.assertEqual(rounded_tip(raw, currency), Decimal(expected))
			with self.assertRaises(ValueError):
				rounded_tip("-.00001", currency)

	def test_rounded_source_tip_fits_storage(self):
		from kamra.room_charge_money import rounded_tip

		for currency in ("UGX", "USD", "KWD"):
			self.assertEqual(rounded_tip(999_999_999_999, currency), 999_999_999_999)
			with self.assertRaises(ValueError):
				rounded_tip(10**12, currency)
