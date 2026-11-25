"""Mask deprecated approval fields before session initialization (remove in 2.10)."""
import io
import json

from werkzeug.exceptions import BadRequest, RequestEntityTooLarge
from werkzeug.wrappers import Request, Response

_APPROVAL_METHODS = {"kamra.api.check_in", "kamra.api.move_reservation", "kamra.api.move_room"}


def _stage_body(environ, method):
	from kamra.authz import _discard_request_query, _protect_pin, _request_has_pin_query

	request = Request(environ, populate_request=False)
	if _request_has_pin_query(request):
		_discard_request_query(request)
		raise BadRequest
	if request.method != "POST":
		return
	# These three RPCs take scalar arguments and never upload files. Bound
	# parsing before a site has been opened, as Frappe does for form data.
	request.max_content_length = 25 * 1024 * 1024
	if request.is_json:
		body = request.get_json()
	elif request.mimetype in ("application/x-www-form-urlencoded", "multipart/form-data"):
		body = {**request.args.to_dict(), **request.form.to_dict()}
		if request.files:
			raise BadRequest
	else:
		return
	if isinstance(body, dict):
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
			except (BadRequest, UnicodeError, ValueError):
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
