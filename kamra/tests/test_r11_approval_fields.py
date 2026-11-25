"""Approval RPCs accept plain scalar form fields before session initialization."""
from io import BytesIO
from unittest import TestCase

from werkzeug.datastructures import FileStorage, MultiDict
from werkzeug.test import Client, encode_multipart
from werkzeug.wrappers import Response

from kamra.wsgi import approval_middleware


class TestApprovalFields(TestCase):
	def setUp(self):
		self.calls = []
		def downstream(environ, start_response):
			self.calls.append(True)
			return Response(status=204)(environ, start_response)
		self.client = Client(approval_middleware(downstream))

	def submit(self, media_type, filename=None):
		field = FileStorage(BytesIO(b"ordinary text"), filename=filename, content_type=media_type)
		boundary, data = encode_multipart(MultiDict([("reason", field)]))
		return self.client.post("/api/method/kamra.api.move_room", data=data,
		                        content_type=f"multipart/form-data; boundary={boundary}")

	def test_rejects_non_plain_fields_and_attachments(self):
		for media, filename in (("application/json", None), ("application/xml", None), ("text/plain", "note.txt")):
			with self.subTest(media_type=media, filename=filename):
				self.calls.clear()
				response = self.submit(media, filename)
				self.assertEqual((response.status_code, response.data, self.calls), (400, b"", []))

	def test_accepts_plain_fields(self):
		self.assertEqual(self.submit("text/plain; charset=utf-8").status_code, 204)
		response = self.client.post("/api/method/kamra.api.move_room", data={"reason": "ordinary text"},
		                            content_type="multipart/form-data")
		self.assertEqual((response.status_code, self.calls), (204, [True, True]))
