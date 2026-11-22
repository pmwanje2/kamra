"""Mock both API bases so RED measures the actual UGX inversion error."""

import io
import json
import unittest
from unittest.mock import Mock, patch

import frappe

from kamra import hooks, ledger


AS_OF = "Sat, 21 Nov 2026 00:00:00 +0000"
USD_RATES = {"USD": 1, "UGX": 4048.218994, "EUR": 0.864129, "GBP": 0.761245}


def response(base, rates):
	body = json.dumps({"result": "success", "base_code": base, "rates": rates, "time_last_update_utc": AS_OF})
	reply = Mock()
	stream = io.BytesIO(body.encode())
	reply.raw.read.side_effect = lambda size, **kwargs: stream.read(size)
	return reply


def open_feed(url, **kwargs):
	if url.endswith("/USD"):
		return response("USD", USD_RATES)
	return response("UGX", {"UGX": 1, "USD": 0.000248, "EUR": 0.000213, "GBP": 0.000188})


def refuse(message, *args, **kwargs):
	raise frappe.ValidationError(message)


class FXPrecisionTest(unittest.TestCase):
	def test_ugx_cross_rates_use_one_full_precision_usd_snapshot(self):
		with patch("requests.get", side_effect=open_feed) as get:
			mids, as_of = ledger.market_rates("UGX")
		self.assertEqual(mids["USD"], 4048.218994)
		self.assertEqual(mids["EUR"], 4048.218994 / 0.864129)
		self.assertEqual(mids["GBP"], 4048.218994 / 0.761245)
		self.assertEqual(mids["UGX"], 1)
		self.assertEqual(as_of, AS_OF)
		get.assert_called_once_with("https://open.er-api.com/v6/latest/USD",
		                            timeout=5, stream=True, allow_redirects=False)

	def test_invalid_home_rate_refuses_the_snapshot(self):
		for bad in (None, 0, -1, float("inf"), float("nan")):
			with self.subTest(home_rate=bad), patch("frappe.throw", side_effect=refuse):
				with patch("requests.get", side_effect=lambda url, **kw: response(
						url.rsplit("/", 1)[-1], {**USD_RATES, "UGX": bad})):
					with self.assertRaisesRegex(frappe.ValidationError, "UGX.*rate"):
						ledger.market_rates("UGX")

	def test_uganda_defaults_use_644_source_resolution_without_an_official_feed(self):
		from kamra.settings import fx_currencies_default

		with patch("frappe.get_hooks", return_value=hooks.kamra_localization), \
				patch("frappe.get_cached_value", side_effect=lambda dt, name, field: {"country": "Uganda", "currency": "UGX"}[field]), \
				patch("requests.get", side_effect=open_feed) as get:
			self.assertEqual(fx_currencies_default("P"), "USD, EUR, GBP, KES")
			source = ledger._rates_source("P")
			self.assertIs(source, ledger.market_rates)
			self.assertEqual(source("UGX")[0]["USD"], 4048.218994)
		get.assert_called_once()
