"""Approval form text is UTF-8 regardless of the client's multipart encoder."""
from unittest import TestCase

from kamra.tests import test_r11_approval_fields as fields


class TestApprovalCharset(TestCase):
	setUp = fields.TestApprovalFields.setUp
	submit = fields.TestApprovalFields.submit

	def test_requires_one_well_formed_utf8_charset_when_declared(self):
		for params in ("charset=iso-8859-1", "charset=latin-1", "charset=utf-16",
		               "charset=utf-16le", "charset=utf-32", "charset", "charset=",
		               'charset=""', 'charset="utf-8', "charset=utf-8; Charset=utf-8",
		               "charset=latin-1; charset=utf-8", "charset*=utf-8''utf-8"):
			with self.subTest(parameters=params):
				self.calls.clear()
				reply = self.submit("text/plain; " + params)
				self.assertEqual((reply.status_code, reply.data, self.calls), (400, b"", []))

	def test_accepts_utf8_and_default(self):
		for media in ("text/plain", "text/plain;charset=utf-8", 'text/plain; CHARSET="UTF-8"',
		              'text/plain; note="desk; request"; charset=Utf-8'):
			with self.subTest(content_type=media):
				self.calls.clear()
				reply = self.submit(media)
				self.assertEqual((reply.status_code, self.calls), (204, [True]))
