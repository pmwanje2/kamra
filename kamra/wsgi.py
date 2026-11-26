"""Mask deprecated approval fields before session initialization (remove in 2.10)."""
import io
import json
import re

from werkzeug.exceptions import BadRequest, RequestEntityTooLarge
from werkzeug.http import parse_options_header
from werkzeug.sansio.multipart import Data, Epilogue, Field, File, MultipartDecoder, NeedData
from werkzeug.wrappers import Request, Response

_APPROVAL_METHODS = {"kamra.api.check_in", "kamra.api.move_reservation", "kamra.api.move_room"}

# Preserve duplicate and malformed parameters that parse_options_header discards.
_TOKEN = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_PARAMETER = re.compile(rf';[ \t]*({_TOKEN})[ \t]*=[ \t]*({_TOKEN}|"(?:[^"\\\r\n]|\\[^\r\n])*")[ \t]*')


def _validate_text_part(content_type):
	media = content_type.partition(";")[0]
	if media.strip().lower() != "text/plain":
		raise BadRequest
	pos = len(media)
	seen_charset = False
	while pos < len(content_type):
		match = _PARAMETER.match(content_type, pos)
		if not match:
			raise BadRequest
		name, value = match.groups()
		name = name.lower()
		if name == "charset":
			if seen_charset or value.lower() not in ("utf-8", '"utf-8"'):
				raise BadRequest
			seen_charset = True
		elif name.startswith("charset*"):
			raise BadRequest
		pos = match.end()


def _validate_field_headers(headers):
	media = headers.getlist("Content-Type")
	disposition = headers.getlist("Content-Disposition")
	if len(disposition) != 1 or parse_options_header(disposition[0])[0].lower() != "form-data":
		raise BadRequest
	if len(media) > 1:
		raise BadRequest
	if media:
		_validate_text_part(media[0])


def _approval_form(request, boundary):
	"""Decode field bytes strictly, including duplicate fields, before dispatch."""
	field_limit = request.max_form_memory_size
	if field_limit is None:
		field_limit = 500_000  # Werkzeug's default maximum bytes per form field.
	decoder = MultipartDecoder(boundary, max_form_memory_size=field_limit,
	                           max_parts=request.max_form_parts)
	# Leave space for framing bytes retained between reads at smaller limits.
	read_size = min(64 * 1024, max(1, field_limit // 2))
	stream = request.stream
	fields = {}
	value = bytearray()
	while True:
		event = decoder.next_event()
		if isinstance(event, NeedData):
			decoder.receive_data(stream.read(read_size) or None)
		elif isinstance(event, Field):
			_validate_field_headers(event.headers)
			name = event.name
			value.clear()
		elif isinstance(event, File):
			raise BadRequest
		elif isinstance(event, Data):
			# The decoder bounds its own buffer. One field can span many buffers.
			if len(value) + len(event.data) > field_limit:
				raise RequestEntityTooLarge
			value.extend(event.data)
			if not event.more_data:
				fields.setdefault(name, value.decode("utf-8", errors="strict"))
		elif isinstance(event, Epilogue):
			return fields


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
		body = {**request.args.to_dict(), **_approval_form(request, boundary.encode("ascii"))}
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
