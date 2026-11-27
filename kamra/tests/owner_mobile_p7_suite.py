"""Exercise the owner dashboard and subsequent booking tests in the same process."""
from unittest import TestSuite


def load_tests(loader, standard_tests, pattern):
	modules = ("test_owner_mobile", "test_p7_direct_booking_r10", "test_p7_r2_review")
	return TestSuite(loader.loadTestsFromName(f"kamra.tests.{module}") for module in modules)
