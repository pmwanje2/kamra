"""Keep pre-move discounts stable when a destination tariff extends a stay."""

from unittest.mock import patch

from kamra import api, business_date, folio, pricing
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)
from kamra.tests.test_reservation_discounts import set_setting


class TestPmsP1R4Regressions(AuditTestCase):
	properties = (PROP_A,)

	def test_extension_discounts_unposted_pre_move_night_once(self):
		self.at(TODAY, "kamra.api", "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY, currency="INR", gst_rate=0)
		set_setting(PROP_A, "rate_override_limit_pct", 20)
		standard = make_room_type(PROP_A, "STD", base_price=10000)
		suite = make_room_type(PROP_A, "STE", base_price=15000)
		source = make_room(PROP_A, standard, "101")
		destination = make_room(PROP_A, suite, "201")

		def resolve(_property, room_type, _rate_plan, start, *_args, **_kwargs):
			if room_type == suite:
				return 15000, {
					"currency": "INR", "property_currency": "INR", "nightly": 15000,
					"fx_rate": 1, "fx_date": str(start),
				}
			return None

		with patch.object(pricing, "contract_rate", side_effect=resolve):
			stay = make_reservation(
				PROP_A, standard, TODAY, d(2), status="Checked In", room=source, auto_price=1,
			)
			api.set_reservation_discount(
				stay.name, discount_type="Percent", discount_value=10, reason_code="RATE-PROMO",
				confirm_price=1,
			)
			self.assertEqual(folio.locked_night(stay.reload(), TODAY).rate, 9000)
			self.assertFalse(folio.charged_nights(stay.name))
			business_date.advance_business_date(PROP_A, TODAY)
			api.move_reservation(stay.name, destination, upgrade_type="Paid", confirm_price=1)
			self.assertEqual(folio.locked_night(stay.reload(), TODAY).rate, 9000)
			self.assertFalse(folio.charged_nights(stay.name))
			api.amend_stay(stay.name, TODAY, d(3), confirm_price=1)

		stay.reload()
		self.assertEqual(folio.locked_night(stay, TODAY).rate, 9000)
		self.assertEqual(folio.locked_night(stay, TODAY).discount_amount, 1000)
		self.assertEqual(folio.locked_night(stay, d(2)).rate, 13500)
		self.assertEqual(pricing.contract_night_rates(stay)[TODAY], 10000)
