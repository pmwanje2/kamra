"""Money at the POS contract boundary, in the currency of the amount."""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from babel.numbers import get_currency_precision


def minor_amount(value, currency):
	unit = Decimal(1).scaleb(-get_currency_precision(currency))
	return Decimal(str(value or 0)).quantize(unit, rounding=ROUND_HALF_UP)


def rounded_tip(value, currency):
	try:
		raw = Decimal(str(0 if value is None else value))
		if not raw.is_finite() or raw < 0:
			raise ValueError
		rounded = minor_amount(raw, currency)
		if rounded > 999_999_999_999:
			raise ValueError
		return rounded
	except InvalidOperation as exc:
		raise ValueError from exc


def available_credit(held, allowance, balance, remaining, currency):
	return (max(minor_amount(held, currency), minor_amount(allowance, currency))
	        - minor_amount(balance, currency) - minor_amount(remaining, currency))
