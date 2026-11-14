"""TKH-08: the folio's Post a charge box starts on the property's own rate.

property_locale says which rate the server puts on a guest service (the
one it applies when a charge comes with no rate), so the screen can start
there instead of a hardcoded 5%."""

from __future__ import annotations

from kamra import api
from kamra.tests.audit_fixtures import PROP_A, AuditTestCase, make_property


class ChargeTaxDefaultTestCase(AuditTestCase):
	properties = (PROP_A,)

	def test_a_property_with_no_tax_reports_a_zero_services_rate(self):
		# Tankhill's shape: a country with no pack of its own (generic), rates 0%
		make_property(PROP_A, country="Uganda", gst_rate_low=0, gst_rate_high=0)
		loc = api.property_locale(PROP_A)
		self.assertEqual(loc["tax_rates"], [0])
		self.assertEqual(loc["services_tax_rate"], 0)

	def test_the_services_rate_follows_the_property(self):
		make_property(PROP_A, country="Uganda", gst_rate_low=12, gst_rate_high=18)
		loc = api.property_locale(PROP_A)
		self.assertEqual(loc["services_tax_rate"], 12)
		self.assertIn(12, loc["tax_rates"])

	def test_india_services_rate_is_the_pack_rate(self):
		make_property(PROP_A, country="India")
		loc = api.property_locale(PROP_A)
		self.assertEqual(loc["services_tax_rate"], 18)
