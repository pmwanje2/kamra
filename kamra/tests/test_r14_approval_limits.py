"""Field byte and part-count limits at the in-process approval middleware boundary."""
from unittest import TestCase
from unittest.mock import patch

from werkzeug.wrappers import Request

from kamra.tests import test_r13_approval_text as fields


class TestApprovalLimits(TestCase):
	setUp = fields.TestApprovalText.setUp
	submit = fields.TestApprovalText.submit

	def check(self, sizes, status):
		self.bodies.clear()
		response = self.submit([(b"a" * size, "text/plain") for size in sizes])
		self.assertEqual((response.status_code, response.data, len(self.bodies)),
		                 (status, b"", int(status == 204)))

	def test_default_boundary(self):
		self.check([500_000], 204)
		self.check([500_001], 413)

	def test_configured_limit_counts_each_fields_bytes(self):
		with patch.object(Request, "max_form_memory_size", 150_000):
			self.check([150_000, 150_000], 204)
			self.check([1, 150_001], 413)

	def test_unset_limit_uses_werkzeug_default(self):
		with patch.object(Request, "max_form_memory_size", None):
			self.check([500_000], 204)
			self.check([500_001], 413)

	def test_configured_part_limit(self):
		with patch.object(Request, "max_form_parts", 2):
			self.check([1, 1], 204)
			self.check([1, 1, 1], 413)
