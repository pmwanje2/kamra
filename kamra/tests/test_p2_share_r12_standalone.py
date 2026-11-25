"""Exercise the native unshared posting interfaces used by the rate-matrix harness."""
import unittest


class TestUnsharedPostingCompatibility(unittest.TestCase):
	def test_standard_bill_with_default_idempotency_lookup(self):
		from kamra.tests.test_rate_matrix_fx_remediation import CurrentDatePostingTest
		case = CurrentDatePostingTest("test_the_charge_records_the_rate_it_posted_at")
		case.debug()

	def test_standard_bill_with_no_routing_instructions(self):
		from kamra.tests.test_rate_matrix_parity_r12 import ZeroTaxChargesTotalAndBridgeAgreementTest
		case = ZeroTaxChargesTotalAndBridgeAgreementTest("test_zero_tax_minor_unit_line_rounding_matrix_and_folio_currency_options")
		case.debug()
