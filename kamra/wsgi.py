"""Mask deprecated approval fields before session initialization (remove in 2.10)."""
import io
import json

from werkzeug.exceptions import BadRequest, RequestEntityTooLarge
from werkzeug.formparser import MultiPartParser
from werkzeug.http import parse_options_header
from werkzeug.wrappers import Request, Response

_APPROVAL_METHODS = {"kamra.api.check_in", "kamra.api.move_reservation", "kamra.api.move_room"}


class _ApprovalMultipartParser(MultiPartParser):
	"""Inspect each part while Werkzeug still has its original headers."""

	def get_part_charset(self, headers):
		media = headers.getlist("Content-Type")
		disposition = headers.getlist("Content-Disposition")
		if len(disposition) != 1 or parse_options_header(disposition[0])[0].lower() != "form-data":
			raise BadRequest
		if media and (len(media) != 1 or parse_options_header(media[0])[0].lower() != "text/plain"):
			raise BadRequest
		return super().get_part_charset(headers)

	def start_file_streaming(self, event, total_content_length):
		raise BadRequest


def _stage_body(environ, method):
	from kamra.authz import _discard_request_query, _protect_pin, _request_has_pin_query

	request = Request(environ, populate_request=False)
	if _request_has_pin_query(request):
		_discard_request_query(request)
		raise BadRequest
	if request.method != "POST":
		if request.content_length or request.is_json:
			raise BadRequest
		return
	# These three RPCs take scalar arguments and never upload files. Bound
	# parsing before a site has been opened, as Frappe does for form data.
	request.max_content_length = 25 * 1024 * 1024
	if request.is_json:
		body = request.get_json()
	elif request.mimetype == "multipart/form-data":
		boundary = request.mimetype_params.get("boundary")
		if not boundary:
			raise BadRequest
		parser = _ApprovalMultipartParser(max_form_memory_size=request.max_form_memory_size,
		                                  max_form_parts=request.max_form_parts)
		fields, _ = parser.parse(request.stream, boundary.encode("ascii"), request.content_length)
		body = {**request.args.to_dict(), **fields.to_dict()}
	elif request.mimetype == "application/x-www-form-urlencoded":
		body = {**request.args.to_dict(), **request.form.to_dict()}
	else:
		if request.content_length or environ.get("wsgi.input_terminated"):
			raise BadRequest
		return
	if not isinstance(body, dict) or any(
			not key.isidentifier() or isinstance(value, (dict, list)) for key, value in body.items()):
		raise BadRequest
	legacy = body.pop("supervisor_pin", None)
	value = body.pop("pin_secret", legacy)
	if value is not None:
		environ["kamra.approval_credential"] = (method, _protect_pin(value))
	# Rebuild even a canonical request: denied sessions, error snapshots,
	# tracebacks and downstream request-body readers only see public fields.
	payload = json.dumps(body, ensure_ascii=False).encode()
	environ.update({"wsgi.input": io.BytesIO(payload), "CONTENT_LENGTH": str(len(payload)),
	                "CONTENT_TYPE": "application/json"})


def approval_middleware(app):
	def wrapped(environ, start_response):
		path = environ.get("PATH_INFO", "").rstrip("/")
		prefix, _, method = path.rpartition("/method/")
		if prefix in ("/api", "/api/v1", "/api/v2") and method in _APPROVAL_METHODS:
			try:
				_stage_body(environ, method)
			except RequestEntityTooLarge:
				return Response(status=413)(environ, start_response)
			except (BadRequest, UnicodeError, ValueError, RecursionError):
				return Response(status=400)(environ, start_response)
		return app(environ, start_response)
	return wrapped


def application(environ, start_response):
	"""Standalone WSGI entry point for a self-managed Bench."""
	from frappe.app import application as frappe_application

	return approval_middleware(frappe_application)(environ, start_response)


def post_worker_init(worker):
	"""Gunicorn's public worker hook preserves the image's normal server settings."""
	worker.wsgi = approval_middleware(worker.wsgi)
