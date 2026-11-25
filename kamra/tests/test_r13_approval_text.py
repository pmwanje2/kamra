"""Multipart approval fields must decode losslessly before dispatch."""
import json
from io import BytesIO
from unittest import TestCase

from werkzeug.datastructures import FileStorage, MultiDict
from werkzeug.test import Client, encode_multipart
from werkzeug.wrappers import Request, Response

from kamra.wsgi import approval_middleware


class TestApprovalText(TestCase):
	def setUp(self):
		self.bodies = []

		def downstream(environ, start_response):
			self.bodies.append(Request(environ).get_data())
			return Response(status=204)(environ, start_response)

		self.client = Client(approval_middleware(downstream))

	def submit(self, fields, path="/api/method/kamra.api.move_room"):
		boundary, body = encode_multipart(MultiDict([
			("reason", FileStorage(BytesIO(data), content_type=media)) for data, media in fields
		]))
		return self.client.post(path, data=body, content_type=f"multipart/form-data; boundary={boundary}")

	def test_rejects_invalid_field_bytes_before_dispatch(self):
		for media in ("text/plain", "text/plain; charset=utf-8"):
			with self.subTest(media=media):
				self.bodies.clear()
				response = self.submit([(b"ordinary note", media), (bytes([255]), media)])
				self.assertEqual((response.status_code, response.data, self.bodies), (400, b"", []))

	def test_accepts_valid_multilingual_text_across_read_boundaries(self):
		note = "a" * 65400 + " à bientôt · mañana · مساء الخير · �" * 12
		for media in ("text/plain", 'text/plain; charset="UTF-8"'):
			with self.subTest(media=media):
				response = self.submit([(note.encode(), media)])
				self.assertEqual(response.status_code, 204)
				self.assertEqual(json.loads(self.bodies[-1]), {"reason": note})

	def test_leaves_unrelated_route_body_intact(self):
		response = self.submit([(bytes([255]), "text/plain")], "/another-route")
		self.assertEqual(response.status_code, 204)
		self.assertIn(bytes([255]), self.bodies[0])
