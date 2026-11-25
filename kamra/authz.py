"""Endpoint authorization - Frappe checks doctype permissions on ORM
paths, but raw-SQL reads and db.set_value writes sail past them. Every
whitelisted Kamra endpoint therefore declares who may call it - and,
for staff restricted to some properties, which properties it may touch."""

import hashlib
import hmac
import inspect
import json
import math
import secrets
from contextlib import contextmanager
from copy import copy
from functools import wraps
from urllib.parse import parse_qsl, unquote_plus

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from kamra.branding import role_label

ADMIN = ("System Manager", "Administrator", "Hotel Admin")

# IT / site admins only - deliberately EXCLUDES the Hotel Admin business role.
# For user management, developer settings and API keys.
IT_ADMIN = ("System Manager", "Administrator")

# who may correct an issued invoice - a credit note here, or the accounting
# system's credit and re-bill (OPERA's Credit Bill permission). The folio
# screen offers both corrections to these roles only.
INVOICE_CORRECTION_ROLES = ("Finance", "Hotel Admin", "Kamra Agent")


def require_invoice_correction_role() -> None:
	if not (set(INVOICE_CORRECTION_ROLES) | set(ADMIN)) & set(frappe.get_roles()):
		frappe.throw(
			f"Not permitted - needs one of: "
			f"{', '.join(role_label(r) for r in sorted(INVOICE_CORRECTION_ROLES))}.",
			frappe.PermissionError)

# defaults - each property can change them in Settings -> Finance
PIN_MAX_ATTEMPTS = 5
PIN_LOCK_MINUTES = 15
PIN_HASH_ROUNDS = 600_000
_PIN_FIELDS = frozenset({
	"pin", "current_pin", "new_pin", "confirm_pin", "supervisor_pin", "comp_supervisor_pin",
	"override_pin", "approver_pin", "manager_pin", "pin_secret", "confirm_pin_secret", "password", "current_password",
})
# Frappe sanitizes these names in Error Log metadata before before_request runs.
_PIN_HTTP_FIELDS = {
	"kamra.api.set_cashier_pin": {"pin_secret": "pin", "current_password": "current_password"},
	"kamra.api.verify_cashier_pin": {"pin_secret": "pin"},
}
_PIN_ATTEMPT_DOCTYPE = "Cashier PIN Attempt"
_FLOOR_PIN_PARENT = "SiteOps Floor PIN"
_FLOOR_PIN_HASH = "siteops_floor_pin_hash"
_FLOOR_PIN_LOCK = "siteops_floor_pin_locked_until"
_FLOOR_PIN_ATTEMPTS = "siteops_floor_pin_attempts"
_FLOOR_PIN_RESET = "siteops_floor_pin_must_reset"
_FLOOR_PIN_ROUNDS = 200_000
_FLOOR_PIN_METHODS = {
	"status": ("siteops_doa.my_floor_pin_status", "siteops_doa.floor_pin_status"),
	"set": ("siteops_doa.set_my_floor_pin", "siteops_doa.set_floor_pin"),
	"clear": ("siteops_doa.reset_user_floor_pin", "siteops_doa.reset_floor_pin"),
}


def _pin_policy(property: str | None) -> tuple[int, int]:
	"""(max wrong attempts, lock / unlock-window minutes) for a property."""
	from kamra.settings import get_setting
	return (min(PIN_MAX_ATTEMPTS, int(get_setting(property, "pin_max_attempts", PIN_MAX_ATTEMPTS))),
	        int(get_setting(property, "pin_lock_minutes", PIN_LOCK_MINUTES)))


@contextmanager
def act_as(user: str):
	"""Run a block as another user (e.g. the governed agent@kamra.local) and
	hand the caller's own session back untouched afterwards.

	Switching user rewrites the live session in place - sid becomes the
	user name and session.data (csrf_token, expiry, ...) is wiped. A plain
	set_user(me) afterwards keeps the user but not the rest, and Frappe's
	after-request Session.update() then caches that wiped record under the
	real sid: the staff member's next call resolves to "User None" and the
	SPA drops to the login screen. Snapshot and restore the session instead."""
	session = frappe.local.session
	saved = dict(session)
	form_dict = frappe.local.form_dict
	frappe.set_user(user)  # nosemgrep: frappe-setuser -- callers authorize first and scope what runs as this user
	try:
		yield
	finally:
		frappe.set_user(saved.get("user"))  # nosemgrep: frappe-setuser -- restoring the caller's own user
		session.clear()
		session.update(saved)
		frappe.local.form_dict = form_dict


def require_it_admin(fn):
	"""Stricter than require_roles: System / site administrators only, not the
	Hotel Admin (GM) business role. Place below @frappe.whitelist()."""

	@wraps(fn)
	def guarded(*args, **kwargs):
		if not set(IT_ADMIN) & set(frappe.get_roles()):
			frappe.throw(
				"Needs a System / site administrator (IT).", frappe.PermissionError)
		return fn(*args, **kwargs)

	return guarded


# Endpoint argument name -> the doctype(s) it names. Each carries a
# `property` link, so the guard can tell which hotel a call reaches into.
# Several candidates are tried in order (their naming series don't collide).
SCOPED_ARGS = {
	"reservation": ("Reservation", "POS Table Reservation"),
	"folio": ("Folio",), "from_folio": ("Folio",), "to_folio": ("Folio",),
	"folios": ("Folio",), "allowance_request": ("Allowance Request",),
	"group_booking": ("Group Booking",), "group": ("Group Booking",),
	"order": ("POS Order", "Laundry Order"),
	"outlet": ("POS Outlet",),
	"room": ("Room",), "new_room": ("Room",),
	"task": ("Housekeeping Task", "Banquet Function Task"),
	"function": ("Venue Booking",),
	"venue": ("Venue",),
	"menu": ("Banquet Menu",),
	"menu_item": ("Menu Item",),
	"service_item": ("Banquet Service Item",),
	"ingredient": ("Ingredient",),
	"session": ("Cashier Session",),
	"cashier_terminal": ("Cashier Terminal",), "to_terminal": ("Cashier Terminal",),
	"cash_office": ("Cashier Terminal",), "transfer": ("Cash Transfer",),
	"ticket": ("Service Ticket",),
	"account": ("City Ledger Account",),
	"connection": ("Channel Manager Connection", "Channel Provider Connection"),
	# chain-wide: checked by stay history (assert_guest_access), not a link
	"guest": ("Guest",),
}


def restricted_properties(user: str | None = None) -> set[str] | None:
	"""The properties the user (default: current) is limited to by Frappe
	User Permissions, or None when they aren't restricted (they see them all).
	Opt-in strict scope requires everyone but Administrator / System Manager
	to have explicit Property permissions."""
	from frappe.core.doctype.user_permission.user_permission import get_user_permissions
	user = user or frappe.session.user
	perms = get_user_permissions(user).get("Property")
	if perms:
		return {p.get("doc") for p in perms}
	if _strict_scope() and not (user == "Administrator" or "System Manager" in frappe.get_roles(user)):
		return set()
	return None


def _strict_scope() -> bool:
	return bool(cint(frappe.conf.get("kamra_strict_property_scope")))


def property_query_conditions(user: str | None = None, doctype: str | None = None) -> str:
	"""permission_query_conditions hook for property-linked doctypes. Only
	under strict scope: a user without Property permissions then lists none,
	not every hotel. Otherwise Frappe's own User Permissions apply as before."""
	if not _strict_scope() or not doctype:
		return ""
	allowed = restricted_properties(user)
	if allowed is None:
		return ""
	if not allowed:
		return "1=0"
	props = ", ".join(frappe.db.escape(p) for p in sorted(allowed))
	return f"`tab{doctype}`.`property` IN ({props})"


def property_has_permission(doc, ptype: str | None = None, user: str | None = None,
                            debug: bool = False) -> bool:
	"""has_permission hook matching property_query_conditions for single
	records. Frappe treats a falsy return as a deny."""
	if not _strict_scope():
		return True
	allowed = restricted_properties(user)
	return allowed is None or doc.get("property") in allowed


# A connection pushes and pulls one property's channel data; these doctypes
# hang from it and carry no property of their own.
_CONNECTION_DOCTYPES = ("Channel Manager Connection", "Channel Provider Connection")


def _connection_link(doctype: str) -> tuple[str, str] | None:
	"""(fieldname, connection doctype) of the doctype's link to a channel
	connection."""
	for df in frappe.get_meta(doctype).get("fields", {"fieldtype": "Link"}):
		if df.options in _CONNECTION_DOCTYPES:
			return df.fieldname, df.options
	return None


def connection_query_conditions(user: str | None = None, doctype: str | None = None) -> str:
	"""permission_query_conditions hook for doctypes that belong to a property
	through their channel connection (Channel ARI State, Channel Room
	Mapping...). Property User Permissions only bite on a Property link, so
	this applies the user's properties - restricted or strict scope - to the
	connection's: the rows of another hotel's connection are not listed."""
	allowed = restricted_properties(user)
	if allowed is None or not doctype:
		return ""
	link = _connection_link(doctype)
	if not allowed or not link:
		return "1=0"
	field, target = link
	props = ", ".join(frappe.db.escape(p) for p in sorted(allowed))
	return (f"`tab{doctype}`.`{field}` IN (SELECT `name` FROM `tab{target}`"
	        f" WHERE `property` IN ({props}))")


def connection_has_permission(doc, ptype: str | None = None, user: str | None = None,
                              debug: bool = False) -> bool:
	"""has_permission hook matching connection_query_conditions: read, write
	or create a row only if its connection is at a property the user may
	access (the connection it points to now, also on a save that moves it)."""
	allowed = restricted_properties(user)
	if allowed is None:
		return True
	link = _connection_link(doc.doctype)
	if not link:
		return False
	field, target = link
	connection = doc.get(field)
	if not connection:
		return True  # nothing to reach into yet; the link is mandatory on save
	return frappe.db.get_value(target, connection, "property") in allowed


def assert_property_access(property: str | None):
	"""Refuse a call aimed at a property the user isn't permitted for."""
	allowed = restricted_properties()
	if property and allowed is not None and property not in allowed:
		frappe.throw(f"You don't have access to {property}.",
		             frappe.PermissionError)


def assert_record_access(doctype: str, name: str | None):
	"""Same guard for a record: refuse it when it belongs to a property
	the user can't access."""
	if not name or restricted_properties() is None:
		return
	if frappe.get_meta(doctype).has_field("property"):
		assert_property_access(
			frappe.db.get_value(doctype, name, "property"))


def assert_guest_access(guest: str | None):
	"""Guest profiles are shared across the chain, so a guest has no single
	property. A restricted user may open one only if the guest has stayed
	(or is booked) at one of their properties, or has no stays yet."""
	allowed = restricted_properties()
	if not guest or allowed is None:
		return
	props = set(frappe.get_all("Reservation", filters={"guest": guest},
	                           pluck="property", distinct=True))
	if props and not props & allowed:
		frappe.throw("You don't have access to this guest.",
		             frappe.PermissionError)


def guest_scope_sql(alias: str = "g") -> tuple[str, dict]:
	"""SQL condition + params limiting a Guest query to the guests a
	restricted user may see (see assert_guest_access); ("1=1", {}) when
	the user isn't restricted."""
	allowed = restricted_properties()
	if allowed is None:
		return "1=1", {}
	return (
		f"(EXISTS (SELECT 1 FROM `tabReservation` gs WHERE gs.guest = {alias}.name"
		f" AND gs.property IN %(scope_properties)s)"
		f" OR NOT EXISTS (SELECT 1 FROM `tabReservation` gn"
		f" WHERE gn.guest = {alias}.name))",
		{"scope_properties": tuple(sorted(allowed)) or ("",)},
	)


def guest_query_conditions(user: str | None = None, doctype: str | None = None) -> str:
	"""permission_query_conditions hook for Guest. Guest has no property
	link, so User Permissions alone don't limit it: without this, Frappe's
	own REST (/api/resource/Guest, frappe.client.get_list) and Desk list
	every guest in the chain to a property-restricted user. Same rule as
	guest_scope_sql."""
	allowed = restricted_properties(user)
	if allowed is None:
		return ""
	props = ", ".join(frappe.db.escape(p) for p in sorted(allowed)) or "''"
	return (
		f"(EXISTS (SELECT 1 FROM `tabReservation` gs WHERE gs.guest = `tabGuest`.name"
		f" AND gs.property IN ({props}))"
		" OR NOT EXISTS (SELECT 1 FROM `tabReservation` gn"
		" WHERE gn.guest = `tabGuest`.name))"
	)


def guest_has_permission(doc, ptype: str | None = None, user: str | None = None,
                         debug: bool = False) -> bool:
	"""has_permission hook for Guest (single-document reads, writes and
	private ID-file downloads). Frappe treats any falsy return as a deny,
	so this must return True explicitly to allow."""
	allowed = restricted_properties(user)
	if allowed is None or not doc.name or doc.is_new():
		return True
	props = set(frappe.get_all("Reservation", filters={"guest": doc.name},
	                           pluck="property", distinct=True))
	return not props or bool(props & allowed)


def itinerary_query_conditions(user: str | None = None, doctype: str | None = None) -> str:
	"""permission_query_conditions hook for Itinerary. A trip has no property
	of its own; a restricted user sees it only when every leg is at a
	property they may access (kamra.crs._itinerary, same rule), and never
	while it has no leg at all (nothing says which hotel it belongs to)."""
	allowed = restricted_properties(user)
	if allowed is None:
		return ""
	props = ", ".join(frappe.db.escape(p) for p in sorted(allowed)) or "''"
	return (
		"EXISTS (SELECT 1 FROM `tabReservation` il0"
		" WHERE il0.itinerary = `tabItinerary`.name)"
		" AND NOT EXISTS (SELECT 1 FROM `tabReservation` il"
		" WHERE il.itinerary = `tabItinerary`.name"
		f" AND il.property NOT IN ({props}))"
	)


def itinerary_has_permission(doc, ptype: str | None = None, user: str | None = None,
                             debug: bool = False) -> bool:
	"""has_permission hook for Itinerary (Desk, /api/resource): same rule
	as itinerary_query_conditions. Must return True explicitly to allow."""
	allowed = restricted_properties(user)
	if allowed is None or not doc.name or doc.is_new():
		return True
	props = set(frappe.get_all("Reservation", filters={"itinerary": doc.name},
	                           pluck="property", distinct=True))
	return bool(props) and props <= allowed


def _values(value) -> list[str]:
	if isinstance(value, str) and value.startswith("["):
		try:
			value = json.loads(value)
		except ValueError:
			return [value]
	if isinstance(value, (list, tuple)):
		return [v for v in value if isinstance(v, str)]
	return [value] if isinstance(value, str) else []


def _enforce_property_scope(fn, args, kwargs, scope: dict):
	"""Check every argument that names a property-scoped record. A no-op
	for unrestricted users; unknown names are left for the endpoint's own
	not-found handling."""
	allowed = restricted_properties()
	if allowed is None:
		return
	try:
		bound = inspect.signature(fn).bind_partial(*args, **kwargs).arguments
	except TypeError:
		bound = kwargs
	for arg, value in bound.items():
		if arg == "property":
			assert_property_access(value if isinstance(value, str) else None)
			continue
		doctypes = scope.get(arg) or SCOPED_ARGS.get(arg)
		if not doctypes:
			continue
		if isinstance(doctypes, str):
			doctypes = (doctypes,)
		if doctypes == ("Guest",):
			for name in _values(value):
				assert_guest_access(name)
			continue
		for name in _values(value):
			for dt in doctypes:
				prop = frappe.db.get_value(dt, name, "property")
				if prop:
					assert_property_access(prop)
					break


def require_roles(*roles, scope: dict | None = None):
	"""Allow the listed roles (plus admins). Usage - below the
	whitelist decorator so the registered function is the guarded one:

	    @frappe.whitelist()
	    @require_roles("Front Desk", "Kamra Agent")
	    def check_in(...): ...

	It also enforces property scope: a user restricted to some properties
	can't pass a `property`, or a record id (see SCOPED_ARGS), that belongs
	to another one. `scope` adds endpoint-specific arguments, e.g.
	scope={"name": "Hurdle Rate"}. A "Guest" scope checks guest visibility
	(assert_guest_access) instead, since guests belong to the whole chain.
	"""
	allowed = set(roles) | set(ADMIN)
	scope = scope or {}
	# a housekeeping supervisor can do everything an attendant can
	if "Housekeeping" in allowed:
		allowed.add("Housekeeping Supervisor")

	def deco(fn):
		@wraps(fn)
		def guarded(*args, **kwargs):
			if not allowed & set(frappe.get_roles()):
				frappe.throw(
					f"Not permitted - needs one of: {', '.join(role_label(role) for role in sorted(roles))}."
					if roles else "Not permitted - needs Hotel Admin.",
					frappe.PermissionError)
			_enforce_property_scope(fn, args, kwargs, scope)
			return fn(*args, **kwargs)
		# introspectable RBAC: Kamra Agent filters its tool list by this
		guarded._kamra_roles = allowed
		return scrub_pin_arguments(guarded) if _PIN_FIELDS & inspect.signature(fn).parameters.keys() else guarded
	return deco


class _PinSecret:
	"""Opaque credential. Contextual traceback formatters inspect string subclasses."""

	__slots__ = ("_value",)

	def __init__(self, value: str | None):
		self._value = value

	def __repr__(self):
		return "<redacted>"

	__str__ = __repr__

	def __bool__(self):
		return bool(self._value)

	def __len__(self):
		return len(self._value) if self._value is not None else 0

	def __eq__(self, other):
		if isinstance(other, _PinSecret):
			return self._value == other._value
		return isinstance(other, str) and self._value == other

	def strip(self, chars=None):
		return _PinSecret(self._value.strip(chars)) if self._value is not None else self

	def isascii(self):
		return self._value is not None and self._value.isascii()

	def isdigit(self):
		return self._value is not None and self._value.isdigit()

	def encode(self):
		return self._value.encode()

	def reveal(self):
		return self._value


def _protect_pin(value):
	if value is None or isinstance(value, _PinSecret):
		return value
	return _PinSecret(value if isinstance(value, str) else None)


class _FloorPinForm(frappe._dict):
	"""Allow native scripts to consume credentials without storing raw form values."""

	_kamra_pin_form = True

	def get(self, key, default=None):
		value = super().get(key, default)
		return value.reveal() if isinstance(value, _PinSecret) else value

	def pop(self, key, default=None):
		value = super().pop(key, default)
		return value.reveal() if isinstance(value, _PinSecret) else value

def _scrub_pin_from_form_dict(*extra_fields: str) -> None:
	form = getattr(frappe.local, "form_dict", None)
	if isinstance(form, dict):
		fields = _PIN_FIELDS | set(extra_fields)
		for key in tuple(form):
			if isinstance(key, str) and key.casefold() in fields:
				form.pop(key, None)


def _request_has_pin_query(request) -> bool:
	args = getattr(request, "args", None)
	if args and any(unquote_plus(str(key)).casefold() in _PIN_FIELDS for key in args):
		return True
	query = getattr(request, "query_string", "")
	if isinstance(query, bytes):
		query = query.decode("utf-8", errors="replace")
	url_queries = [str(getattr(request, field, "") or "").partition("?")[2] for field in ("full_path", "path")]
	return any(
		key.casefold() in _PIN_FIELDS
		for source in (query, *url_queries) if isinstance(source, str)
		for key, _ in parse_qsl(source.replace(";", "&"), keep_blank_values=True)
	)


def _discard_request_query(request) -> None:
	"""Clear the raw WSGI URL and Werkzeug's cached representations before raising."""
	request.query_string = b""
	for key in ("args", "values"):
		vars(request).pop(key, None)
	for key in ("path", "full_path", "url", "base_url", "root_url", "url_root", "referrer"):
		url = vars(request).get(key)
		if isinstance(url, str):
			setattr(request, key, url.partition("?")[0])

	environ = getattr(request, "environ", None)
	if isinstance(environ, dict):
		environ["QUERY_STRING"] = ""
		for key in ("RAW_URI", "REQUEST_URI", "PATH_INFO", "HTTP_REFERER",
		            "HTTP_X_ORIGINAL_URI", "HTTP_X_FORWARDED_URI"):
			if isinstance(environ.get(key), str):
				environ[key] = environ[key].partition("?")[0]


def _reject_pin_query(request) -> None:
	if request is not None and _request_has_pin_query(request):
		_discard_request_query(request)
		_scrub_pin_from_form_dict()
		frappe.throw(frappe._("Send PINs and passwords in the request body."), frappe.ValidationError)


def prepare_pin_request() -> None:
	"""Stage credentials before Frappe copies RPC kwargs or logs a denied request."""
	form = getattr(frappe.local, "form_dict", None)
	request = getattr(frappe.local, "request", None) or getattr(frappe, "request", None) or None
	if not isinstance(form, dict) or request is None:
		return
	path = request.path.partition("?")[0].rstrip("/")
	method = path.rsplit("/method/", 1)[-1] if path.startswith("/api/") and "/method/" in path else form.get("cmd")
	if not isinstance(method, str) or not method.startswith("kamra."):
		return
	_reject_pin_query(request)
	frappe.local.kamra_pin_arguments = (
		method, {key: _protect_pin(form.pop(key)) for key in _PIN_FIELDS if key in form},
	)


def scrub_pin_arguments(fn):
	"""Keep raw credentials out of role, scope, type-check, and handler frames."""
	if getattr(fn, "_kamra_pin_scrubbed", False):
		return fn
	parameters = inspect.signature(fn).parameters
	positional = tuple(name for name, param in parameters.items()
	                   if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD))
	method = f"{fn.__module__}.{fn.__name__}"

	@wraps(fn)
	def scrubbed(*args, **kwargs):
		args = tuple(_protect_pin(value) if i < len(positional) and positional[i] in _PIN_FIELDS else value
		             for i, value in enumerate(args))
		kwargs = {key: _protect_pin(value) if key in _PIN_FIELDS else value for key, value in kwargs.items()}
		_scrub_pin_from_form_dict()
		request = getattr(frappe.local, "request", None)
		wire_fields = _PIN_HTTP_FIELDS.get(method) if request is not None else None
		if wire_fields is not None:
			# Requests may supply only the sanitizer-compatible credential names.
			kwargs = {wire_fields.get(key, key): value for key, value in kwargs.items()
			          if key in wire_fields or key not in _PIN_FIELDS}
		staged = getattr(frappe.local, "kamra_pin_arguments", None)
		if staged and staged[0] == method:
			frappe.local.kamra_pin_arguments = None
			for key, value in staged[1].items():
				parameter = wire_fields.get(key) if wire_fields is not None else key
				if parameter in parameters and parameter not in positional[:len(args)]:
					kwargs.setdefault(parameter, value)
		_reject_pin_query(request or getattr(frappe, "request", None) or None)
		try:
			if request is not None:
				from frappe.utils.typing_validations import transform_parameter_types

				args, kwargs = transform_parameter_types(fn, args, kwargs)
			return fn(*args, **kwargs)
		except Exception as error:
			if (type(error).__module__ == "frappe.exceptions"
			    or isinstance(error, (frappe.ValidationError, frappe.PermissionError))):
				raise
		# Raise outside the provider's exception context so Frappe cannot log its frames.
		frappe.clear_messages()
		frappe.throw(frappe._("The PIN operation could not be completed. Try again."), frappe.ValidationError)

	# Frappe's whitelist wrapper validates before entering this decorator.
	# Move non-secret type validation behind the credential boundary.
	fn.__annotations__ = {key: value for key, value in fn.__annotations__.items() if key not in _PIN_FIELDS}
	scrubbed.__annotations__ = {}
	scrubbed._kamra_pin_scrubbed = True
	return scrubbed


def _pin_locked(doc) -> bool:
	if not doc.get("locked_until"):
		return False
	return now_datetime() < doc.locked_until


def require_cashier_pin(property: str, pin=None):
	"""The walk-up-to-an-unlocked-terminal guard: money actions re-confirm
	WHO is acting with a personal PIN, even inside a valid session.

	Skipped for agents (Kamra Agent role, the copilot's in-process tool calls,
	and gated replays) - their identity and accountability come from the
	autonomy gate + action log, not a keypad. Off unless the property enables
	require_cashier_pin.

	Supports a short unlock window via frappe.cache (set by verify_cashier_pin).
	"""
	if not property or not frappe.db.get_value(
			"Property", property, "require_cashier_pin"):
		return
	if getattr(frappe.flags, "kamra_agent_call", False) or \
	   getattr(frappe.flags, "kamra_gate_bypass", False):
		return
	if "Kamra Agent" in frappe.get_roles():
		return
	user = frappe.session.user
	if user == "Administrator":
		return
	_, lock_minutes = _pin_policy(property)

	# Sliding unlock window (set after a successful PinPad entry)
	cache_key = f"kamra_cashier_unlock:{user}"
	if pin in (None, "") and frappe.cache.get_value(cache_key):
		state = approval_pin_state(user)
		if state["has_pin"] and not state["locked"]:
			frappe.cache.set_value(cache_key, 1, expires_in_sec=lock_minutes * 60)
			return
	_check_user_pin(user, pin, property)
	frappe.cache.set_value(cache_key, 1, expires_in_sec=lock_minutes * 60)


# Who may approve what a captain cannot do alone (voiding food the kitchen
# has already started, say). The same roles the reason-code supervisor
# rule accepts (kamra.ledger.require_reason).
SUPERVISOR_ROLES = ("Hotel Admin", "Finance", "System Manager", "Administrator")
# At the POS only (void approvals): the F&B Manager runs the floor too, but
# never approves folio corrections or sees full guest phones.
POS_SUPERVISOR_ROLES = SUPERVISOR_ROLES + ("F&B Manager",)


def is_supervisor(user: str | None = None, roles=SUPERVISOR_ROLES) -> bool:
	user = user or frappe.session.user
	return user == "Administrator" or bool(
		set(roles) & set(frappe.get_roles(user)))


def _people(who) -> set[str]:
	"""One user or several, case-folded."""
	return {str(u).casefold() for u in ([who] if isinstance(who, str) else who or ()) if u}


def approver_roles(roles) -> str:
	"""The roles that approve, as a refusal names them: "Hotel Admin or
	Finance" (the site's own System Manager and Administrator left out)."""
	named = [r for r in roles if r not in ("System Manager", "Administrator")] or list(roles)
	return " or ".join([", ".join(named[:-1]), named[-1]] if len(named) > 2 else named)


# Horizontal DoA Floor Authority API Server Script (delegation-of-authority@6)
DOA_FLOOR_VERIFY_METHOD = "siteops_doa.verify_floor_authority"

_PLATFORM_ROLES_LOWER = frozenset({
	"administrator",
	"system manager",
	"script manager",
	"all",
	"guest",
	"desk user",
	"read-agent",
	"maximus tenant admin",
	"employee",
	"employee self service",
	"website manager",
})

_DOA_PERMISSION_TAGS = (
	"[SITEOPS-DOA-SELF]",
	"[SITEOPS-DOA-ELIGIBLE]",
	"[SITEOPS-DOA-ROLE]",
	"[SITEOPS-DOA-UNCONFIGURED]",
)


def _doa_floor_script():
	from kamra import erp_bridge
	return (erp_bridge.get_bridge_script(DOA_FLOOR_VERIFY_METHOD)
	        or erp_bridge.get_bridge_script("siteops_doa.verify_floor_pin"))


def doa_floor_bridge_active() -> bool:
	"""True when the horizontal DoA floor authority script is installed and enabled."""
	return _doa_floor_script() is not None


def _floor_pin_script(action: str):
	from kamra import erp_bridge

	for method in _FLOOR_PIN_METHODS[action]:
		script = erp_bridge.get_bridge_script(method)
		if script is not None:
			return script
	return None


def _horizontal_floor_status_script():
	script = _floor_pin_script("status")
	# Early releases shared the same RPC names but preferred the Cashier PIN
	# record. The horizontal-only package reads its DefaultValue records.
	return script if script is not None and "Cashier PIN" not in (getattr(script, "script", "") or "") else None


def _call_floor_pin_script(action: str, form: dict):
	from kamra import erp_bridge

	script = _floor_pin_script(action)
	if script is None:
		frappe.throw(frappe._("This account cannot manage approval PINs."), frappe.PermissionError)
	if action == "clear":
		# This v7 script passes filters positionally alongside fields=.
		# Frappe v16 binds that position to fields. Keep the installed
		# script and its authorization unchanged and adapt the call in memory.
		source = getattr(script, "script", "") or ""
		positional = '"User Permission", {"user": account, "allow": scope},'
		if positional in source:
			script = copy(script)
			script.script = source.replace(positional, positional.replace(", {", ", filters={", 1))
	result = erp_bridge.call_bridge_script(script, _FloorPinForm(form))
	if not isinstance(result, dict) or not cint(result.get("ok")):
		frappe.throw(frappe._("This account cannot manage approval PINs."), frappe.PermissionError)
	return result


def _doctype_has_field(doctype: str, fieldname: str) -> bool:
	has_col = getattr(frappe.db, "has_column", None)
	if callable(has_col):
		try:
			if has_col(doctype, fieldname):
				return True
		except Exception:
			pass
	get_meta = getattr(frappe, "get_meta", None)
	if callable(get_meta):
		try:
			meta = get_meta(doctype)
			if meta and meta.has_field(fieldname):
				return True
		except Exception:
			pass
	return False


def _property_company(property: str | None) -> str:
	"""Resolve the ERP Company linked to `property` without guessing across companies."""
	if property:
		if _doctype_has_field("Property", "company"):
			val = frappe.db.get_value("Property", property, "company")
			return str(val or "").strip()
		if _doctype_has_field("Property", "custom_company"):
			val = frappe.db.get_value("Property", property, "custom_company")
			if val:
				return str(val).strip()
	get_default = getattr(frappe.db, "get_default", None)
	if callable(get_default):
		val = get_default("company")
		if val:
			return str(val).strip()
	return ""


def _platform_roles_denylist() -> set[str]:
	denied = set(_PLATFORM_ROLES_LOWER)
	denied.add("kamra agent")
	get_hooks = getattr(frappe, "get_hooks", None)
	if callable(get_hooks):
		for role in (get_hooks("pos_bypass_roles") or ()):
			if isinstance(role, str) and role.strip():
				denied.add(role.strip().casefold())
	return denied


def _approval_limit_floor_roles(company: str) -> list[str] | None:
	"""Return active non-platform roles from Approval Limit.custom_floor_override_approver_roles,
	or None when the field/record is not configured for `company`."""
	if not company:
		return None
	if not frappe.db.exists("DocType", "Approval Limit") or not frappe.db.exists("Approval Limit", company):
		return None
	if not _doctype_has_field("Approval Limit", "custom_floor_override_approver_roles"):
		return None
	raw = frappe.db.get_value("Approval Limit", company, "custom_floor_override_approver_roles")
	if raw is None or not str(raw).strip():
		return None
	denied = _platform_roles_denylist()
	roles: list[str] = []
	for piece in str(raw).replace("\n", ",").split(","):
		rname = piece.strip()
		if not rname or rname.casefold() in denied:
			continue
		role_row = frappe.db.get_value("Role", rname, ["name", "disabled"], as_dict=True)
		if not role_row or cint(role_row.get("disabled")) == 1:
			continue
		canon = str(role_row.get("name") or rname).strip()
		if canon.casefold() in denied:
			continue
		if canon not in roles:
			roles.append(canon)
	return roles


def floor_override_max_amount(property: str | None, company: str | None = None) -> float | None:
	"""Return Approval Limit.custom_floor_override_max_amount for the property's company,
	rounded to currency precision, or None if not configured."""
	comp = (company or _property_company(property) or "").strip()
	if not comp:
		return None
	if not frappe.db.exists("DocType", "Approval Limit") or not frappe.db.exists("Approval Limit", comp):
		return None
	if not _doctype_has_field("Approval Limit", "custom_floor_override_max_amount"):
		return None
	raw = frappe.db.get_value("Approval Limit", comp, "custom_floor_override_max_amount")
	if raw is None or str(raw).strip() == "":
		return None
	try:
		parsed = float(raw)
	except (TypeError, ValueError):
		return None
	if not math.isfinite(parsed) or parsed < 0:
		return None
	places = 2
	if property:
		try:
			from kamra.ledger import _currency_places
			places = _currency_places(property)
		except Exception:
			places = 2
	return round(parsed, places)


def _company_scoped_role_holders(company: str, roles: list[str]) -> set[str]:
	"""Active System Users holding any role in `roles` (direct Has Role, primary
	Role Profile, or User Role Profile) and permitted for `company`."""
	if not company or not roles:
		return set()
	role_set = set(roles)
	enabled_users = {}
	for u in frappe.get_all(
		"User",
		filters={"enabled": 1, "user_type": "System User"},
		fields=["name", "role_profile_name"],
		limit_page_length=0,
	):
		uname = str(u.get("name") or "").strip()
		if not uname or uname in ("Administrator", "Guest"):
			continue
		enabled_users[uname] = str(u.get("role_profile_name") or "").strip()
	if not enabled_users:
		return set()

	scoped_companies: dict[str, set[str]] = {}
	for cp in frappe.get_all(
		"User Permission",
		filters={"allow": "Company"},
		fields=["user", "for_value", "applicable_for", "apply_to_all_doctypes"],
		limit_page_length=0,
	):
		uname = str(cp.get("user") or "").strip()
		val = str(cp.get("for_value") or "").strip()
		if not uname or not val:
			continue
		app_for = str(cp.get("applicable_for") or "").strip()
		if cint(cp.get("apply_to_all_doctypes")) == 1 or not app_for:
			scoped_companies.setdefault(uname, set()).add(val)

	company_users = {
		uname: rp
		for uname, rp in enabled_users.items()
		if uname not in scoped_companies or company in scoped_companies[uname]
	}
	if not company_users:
		return set()

	holders: set[str] = set()
	for hr in frappe.get_all(
		"Has Role",
		filters={"parenttype": "User", "role": ["in", sorted(role_set)]},
		fields=["parent"],
		limit_page_length=0,
	):
		uname = str(hr.get("parent") or "").strip()
		if uname in company_users:
			holders.add(uname)

	matching_profiles = {
		str(pg.get("parent") or "").strip()
		for pg in frappe.get_all(
			"Has Role",
			filters={"parenttype": "Role Profile", "role": ["in", sorted(role_set)]},
			fields=["parent"],
			limit_page_length=0,
		)
		if pg.get("parent")
	}
	if matching_profiles:
		for uname, rp in company_users.items():
			if rp and rp in matching_profiles:
				holders.add(uname)
		for urp in frappe.get_all(
			"User Role Profile",
			filters={"parenttype": "User", "parentfield": "role_profiles"},
			fields=["parent", "role_profile"],
			limit_page_length=0,
		):
			uname = str(urp.get("parent") or "").strip()
			rp = str(urp.get("role_profile") or "").strip()
			if uname in company_users and rp in matching_profiles:
				holders.add(uname)

	return holders


def supervisors_for(property: str, roles=SUPERVISOR_ROLES,
                    exclude=None, *, include_pin_status=False) -> list[dict]:
	"""Who can approve on a till at this property: enabled users holding
	one of `roles`, with access to the property and a cashier PIN set by default.
	`exclude` (the person asking, or several people) is left out: nobody
	approves their own request. Names only."""
	users = set(frappe.get_all(
		"Has Role", filters={"role": ["in", [r for r in roles if r != "Administrator"]],
		                     "parenttype": "User"},
		pluck="parent", distinct=True))
	bridge_active = doa_floor_bridge_active()
	if bridge_active:
		company = _property_company(property)
		floor_roles = _approval_limit_floor_roles(company)
		if floor_roles is not None:
			users = _company_scoped_role_holders(company, floor_roles)
	elif not include_pin_status:
		users &= set(frappe.get_all("Cashier PIN", pluck="name"))
	excluded = _people(exclude) | {"administrator", "guest"}
	users = {u for u in users if u.casefold() not in excluded}
	out = []
	for u in frappe.get_all("User", filters={"name": ["in", list(users) or [""]],
	                                         "enabled": 1, "user_type": "System User"},
	                        fields=["name", "full_name"], order_by="full_name"):
		allowed = restricted_properties(u.name)
		if allowed is None or property in allowed:
			row = {"user": u.name, "full_name": u.full_name or u.name}
			if include_pin_status or bridge_active:
				status = approval_pin_state(u.name, bridge_active=bridge_active)
				if include_pin_status:
					row.update(has_pin=status["has_pin"], must_reset=status["must_reset"], locked=status["locked"])
				elif not status["has_pin"]:
					continue
			out.append(row)
	return out


def verify_supervisor(property: str, supervisor: str | None, pin,
                      roles=SUPERVISOR_ROLES, requester=None, *,
                      company: str | None = None,
                      document_owner: str | None = None,
                      amount: float | None = 0.0,
                      currency: str | None = None,
                      action: str | None = None,
                      role_field: str = "custom_floor_override_approver_roles",
                      max_amount_field: str | None = "custom_floor_override_max_amount",
                      reference_doctype: str = "",
                      reference_name: str | None = "",
                      reason: str = "",
                      nonce: str | None = None) -> str:
	"""A supervisor approves at someone else's till with their user id and
	their own cashier PIN. The PIN is always required here, whatever the
	property's PIN setting, because it is the only proof the supervisor was
	there. Wrong PINs count against the supervisor's lock like any other
	entry. When `siteops_doa.verify_floor_authority` is installed, delegates
	floor PIN DoA + SoD verification with a unique single-use nonce, refusing
	Administrator free-pass and self-approval. Always scrubs PIN fields from
	`form_dict`. Returns the approving user."""
	pin = _protect_pin(pin)
	_scrub_pin_from_form_dict()
	try:
		doa_script = _doa_floor_script()
		supervisor_in = (supervisor or "").strip()
		if doa_script is not None:
			if not supervisor_in:
				frappe.throw("SUPERVISOR_REQUIRED: pick the supervisor who approves this.",
				             frappe.PermissionError)
			if supervisor_in.casefold() in ("administrator", "guest"):
				frappe.throw(
					"[SITEOPS-DOA-ELIGIBLE] Floor approver must be a named active System User, "
					"never Administrator or Guest.",
					frappe.PermissionError,
				)
			canonical = frappe.db.get_value("User", supervisor_in, "name", for_update=True)
			if not canonical:
				frappe.throw("SUPERVISOR_REQUIRED: pick the supervisor who approves this.",
				             frappe.PermissionError)
			supervisor_user = str(canonical)
			if supervisor_user in ("Administrator", "Guest"):
				frappe.throw(
					"[SITEOPS-DOA-ELIGIBLE] Floor approver must be a named active System User, "
					"never Administrator or Guest.",
					frappe.PermissionError,
				)
			session_user = str(getattr(getattr(frappe, "session", None), "user", "") or "").strip()
			forbidden = _people(requester) | _people(document_owner) | _people(session_user)
			if supervisor_user.casefold() in forbidden:
				frappe.throw(
					"[SITEOPS-DOA-SELF] SUPERVISOR_SELF_APPROVAL: Maker and floor approver "
					"must be two different people.",
					frappe.PermissionError,
				)
			supplied_pin = pin if isinstance(pin, _PinSecret) else ""
			if not supplied_pin:
				frappe.throw("SUPERVISOR_PIN_REQUIRED: the supervisor enters their PIN.")

			if not supplied_pin.isascii() or not supplied_pin.isdigit():
				frappe.throw(frappe._("Wrong cashier PIN."))
			comp = (company or _property_company(property) or "").strip()
			req_people = [
				str(u).strip()
				for u in ([requester] if isinstance(requester, str) else (requester or ()))
				if u and str(u).strip()
			]
			if session_user and session_user != "Administrator":
				doa_requester = session_user
			else:
				doa_requester = req_people[0] if req_people else session_user
			doa_doc_owner = (document_owner or "").strip()
			if not doa_doc_owner:
				for person in req_people:
					if person.casefold() != doa_requester.casefold():
						doa_doc_owner = person
						break

			req_nonce = (nonce or "").strip() or f"kmr-{secrets.token_hex(16)}"
			payload = {
				"cmd": DOA_FLOOR_VERIFY_METHOD,
				"company": comp,
				"property": property or "",
				"requester": doa_requester,
				"approver": supervisor_user,
				"document_owner": doa_doc_owner,
				"pin": supplied_pin,
				"amount": 0 if amount is None else amount,
				"role_field": role_field or "custom_floor_override_approver_roles",
				"reference_doctype": reference_doctype or "",
				"reference_name": "" if reference_name is None else reference_name,
				"reason": reason or "",
				"nonce": req_nonce,
			}
			if currency is not None:
				payload["currency"] = currency
			if action is not None:
				payload["action"] = action
			if max_amount_field is not None:
				payload["max_amount_field"] = max_amount_field

			from kamra import erp_bridge
			refusal = None
			try:
				result = erp_bridge.call_bridge_script(doa_script, _FloorPinForm(payload))
			except (frappe.ValidationError, frappe.PermissionError) as exc:
				refusal = (type(exc), str(exc))
			if refusal:
				# A legacy SiteOps script may retain the PIN in its own
				# stack. Re-raise the refusal outside those traceback frames.
				error_type, message = refusal
				if any(tag in message for tag in _DOA_PERMISSION_TAGS):
					error_type = frappe.PermissionError
				if "[SITEOPS-DOA-PIN] Invalid approver PIN." in message:
					message = frappe._("Wrong cashier PIN.")
				frappe.clear_messages()
				frappe.throw(message, error_type)

			if not isinstance(result, dict) or not cint(result.get("ok")):
				frappe.throw(
					"[SITEOPS-DOA-UNCONFIGURED] Floor authority verification did not return an approval.",
					frappe.PermissionError,
				)
			verified_user = str(result.get("approver") or supervisor_user).strip()
			if verified_user.casefold() != supervisor_user.casefold():
				frappe.throw(
					"[SITEOPS-DOA-ELIGIBLE] Floor authority approver mismatch.",
					frappe.PermissionError,
				)
			if _horizontal_floor_status_script() is None:
				_upgrade_horizontal_floor_pin(supervisor_user, supplied_pin)
			return verified_user

		supervisor = supervisor_in
		canonical = frappe.db.exists("User", supervisor) if supervisor else None
		if not canonical:
			frappe.throw("SUPERVISOR_REQUIRED: pick the supervisor who approves this.",
			             frappe.PermissionError)
		supervisor = str(canonical)
		if supervisor.casefold() in (_people(requester) | _people(document_owner)):
			frappe.throw("SUPERVISOR_SELF_APPROVAL: you cannot approve your own "
			             "request. Another supervisor signs it off.", frappe.PermissionError)
		if not frappe.db.get_value("User", supervisor, "enabled") or \
				not is_supervisor(supervisor, roles):
			frappe.throw(f"{supervisor} cannot approve this. It needs a {approver_roles(roles)} "
			             "user.", frappe.PermissionError)
		if (property or company) and not _pin_scope_allows(supervisor, property, company or _property_company(property)):
			frappe.throw(f"{supervisor} has no access to {property}.",
			             frappe.PermissionError)
		if not frappe.db.exists("Cashier PIN", supervisor):
			frappe.throw(f"PIN_NOT_SET: {supervisor} has no cashier PIN yet. They "
			             "set one the first time a money action asks for it.")
		if not pin:
			frappe.throw("SUPERVISOR_PIN_REQUIRED: the supervisor enters their PIN.")
		_check_user_pin(supervisor, pin, property)
		return supervisor
	finally:
		_scrub_pin_from_form_dict()


def _require_approval_property(property: str) -> str:
	# The general role guard allows absent property filters on list endpoints.
	canonical = frappe.db.exists("Property", property) if isinstance(property, str) and property else None
	if not canonical:
		frappe.throw("An accessible property is required for supervisor approval.", frappe.PermissionError)
	return str(canonical)


@frappe.whitelist()
@require_roles("Front Desk", "Finance")
def list_supervisors(property: str) -> list[dict]:
	"""List eligible independent approvers for this property's floor overrides.
	When Approval Limit.custom_floor_override_approver_roles is configured for
	Property.company, intersects candidates with active company-scoped holders
	of those roles and excludes the authenticated requester."""
	property = _require_approval_property(property)
	company = _property_company(property)
	floor_roles = _approval_limit_floor_roles(company)
	if floor_roles is not None and not doa_floor_bridge_active():
		holders = _company_scoped_role_holders(company, floor_roles)
		holders &= set(frappe.get_all("Cashier PIN", pluck="name"))
		excluded = _people(frappe.session.user) | {"administrator", "guest"}
		holders = {u for u in holders if u.casefold() not in excluded}
		out = []
		for u in frappe.get_all(
			"User",
			filters={"name": ["in", sorted(holders) or [""]], "enabled": 1, "user_type": "System User"},
			fields=["name", "full_name"],
			order_by="full_name",
		):
			allowed = restricted_properties(u.name)
			if allowed is None or property in allowed:
				out.append({"user": u.name, "full_name": u.full_name or u.name})
		return out
	return supervisors_for(property, exclude=frappe.session.user)


@frappe.whitelist(methods=["POST"])
@scrub_pin_arguments
@require_roles("Front Desk", "Finance")
def verify_supervisor_approval(property: str, supervisor: str | None = None, pin: str | None = None) -> dict:
	"""Verify a named supervisor for the authenticated requester's correction."""
	property = _require_approval_property(property)
	approver = verify_supervisor(property, supervisor, pin, roles=SUPERVISOR_ROLES,
	                             requester=frappe.session.user)
	return {"supervisor": approver}


def resolve_property_for_pos_profile(pos_profile: str | None, user: str | None = None, *, strict: bool = False) -> str | None:
	"""Resolve the Property bound to a POS Profile deterministically.

	Resolution order (never guesses across multiple ambiguous properties):
	1. `POS Profile.custom_property` (when field exists and is populated)
	2. `Branch.custom_property` via `POS Profile.branch` (when field exists and is populated)
	3. `Cost Center.custom_property` via `POS Profile.cost_center`, or unique enabled
	   `Property` matching `Property.custom_cost_center`
	4. Unique enabled `Property` whose `name` or `property_name` equals `POS Profile.branch`
	5. Unique enabled `Property` linked to `POS Profile.company` (`company`, `custom_company`,
	   `legal_name`, or `property_name`)
	6. Caller's (`user` or `frappe.session.user`) `Property` User Permissions intersected with
	   enabled properties, when that leaves exactly one permitted enabled Property
	7. Single enabled `Property` on the site (when only one exists)

	Strict PIN operations require an enabled profile with an explicit property link
	or mapped cost center. Name matching and user/site defaults remain for older POS callers.
	"""

	def linked_property(value):
		if not strict or frappe.db.exists("Property", {"name": value, "disabled": 0}):
			return str(value)
		return None
	if pos_profile and frappe.db.table_exists("POS Profile") and frappe.db.exists("POS Profile", pos_profile):
		meta = frappe.get_meta("POS Profile")
		if strict and meta.has_field("disabled") and cint(frappe.db.get_value("POS Profile", pos_profile, "disabled")):
			return None
		for field in ("custom_property", "property"):
			if meta.has_field(field):
				prop = frappe.db.get_value("POS Profile", pos_profile, field)
				if prop:
					return linked_property(prop)
		branch = frappe.db.get_value("POS Profile", pos_profile, "branch") if meta.has_field("branch") else None
		if branch:
			if frappe.db.table_exists("Branch") and frappe.get_meta("Branch").has_field("custom_property"):
				prop = frappe.db.get_value("Branch", branch, "custom_property")
				if prop:
					return linked_property(prop)
		cost_center = frappe.db.get_value("POS Profile", pos_profile, "cost_center") if meta.has_field("cost_center") else None
		if cost_center:
			if frappe.db.table_exists("Cost Center") and frappe.get_meta("Cost Center").has_field("custom_property"):
				prop = frappe.db.get_value("Cost Center", cost_center, "custom_property")
				if prop:
					return linked_property(prop)
			prop_meta = frappe.get_meta("Property")
			if prop_meta.has_field("custom_cost_center"):
				props = frappe.get_all(
					"Property", filters={"custom_cost_center": cost_center, "disabled": 0}, pluck="name", limit=2
				)
				if len(props) == 1:
					return str(props[0])
		if strict:
			return None
		if branch:
			props = frappe.get_all(
				"Property",
				filters={"disabled": 0},
				or_filters={"name": branch, "property_name": branch},
				pluck="name",
				limit=2,
			)
			if len(props) == 1:
				return str(props[0])
		company = frappe.db.get_value("POS Profile", pos_profile, "company") if meta.has_field("company") else None
		if company:
			prop_meta = frappe.get_meta("Property")
			for field in ("company", "custom_company", "legal_name", "property_name"):
				if prop_meta.has_field(field):
					props = frappe.get_all(
						"Property", filters={field: company, "disabled": 0}, pluck="name", limit=2
					)
					if len(props) == 1:
						return str(props[0])

	if strict:
		return None
	effective_user = user or getattr(getattr(frappe, "session", None), "user", None)
	if effective_user and effective_user not in ("Administrator", "Guest"):
		allowed = restricted_properties(effective_user)
		if allowed:
			permitted_enabled = frappe.get_all(
				"Property",
				filters={"name": ["in", sorted(allowed)], "disabled": 0},
				pluck="name",
				limit=2,
			)
			if len(permitted_enabled) == 1:
				return str(permitted_enabled[0])

	props = frappe.get_all("Property", filters={"disabled": 0}, pluck="name", limit=2)
	return str(props[0]) if len(props) == 1 else None


def require_pin_identity(user: str | None = None, *, for_update: bool = False) -> str:
	"""Return an active human account. Lock its User row before PIN writes."""
	user = user if user is not None else frappe.session.user
	if (
		not isinstance(user, str)
		or not user.strip()
		or user.strip().casefold() in {"administrator", "guest"}
		or getattr(frappe.flags, "kamra_agent_call", False)
		or getattr(frappe.flags, "kamra_gate_bypass", False)
		or "Kamra Agent" in frappe.get_roles()
	):
		frappe.throw(frappe._("This account cannot manage approval PINs."), frappe.PermissionError)
	account = frappe.db.get_value("User", user.strip(), ["name", "enabled"],
	                              as_dict=True, for_update=for_update)
	if not account or not cint(account.enabled) or "Kamra Agent" in frappe.get_roles(account.name):
		frappe.throw(frappe._("This account cannot manage approval PINs."), frappe.PermissionError)
	return account.name


def _active_lock(until):
	if isinstance(until, str):
		from frappe.utils import get_datetime
		until = get_datetime(until)
	return until if until and until > now_datetime() else None


def _pin_attempt_name(user: str, kind: str) -> str:
	return f"{kind}:{hashlib.sha256(user.casefold().encode()).hexdigest()}"


def _pin_attempt_lock(user: str, kind: str, *, fresh: bool = False):
	name = _pin_attempt_name(user, kind)
	if fresh:
		# A previous read in the business transaction can predate the User lock.
		with _pin_attempt_connection() as db:
			return _active_lock(db.get_value(_PIN_ATTEMPT_DOCTYPE, name, "locked_until"))
	return _active_lock(frappe.db.get_value(_PIN_ATTEMPT_DOCTYPE, name, "locked_until"))


@contextmanager
def _pin_attempt_connection():
	"""Commit credential counters independently of the caller's business transaction."""
	from frappe.database import get_db

	conf = frappe.conf
	db = get_db(socket=conf.get("db_socket"), host=conf.get("db_host"), port=conf.get("db_port"),
	            user=conf.get("db_user") or conf.get("db_name"), password=conf.get("db_password"),
	            cur_db_name=conf.get("db_name"))
	previous = frappe.local.db
	try:
		# Frappe's ORM and query builder use the active context connection.
		frappe.local.db = db
		yield db
		db.commit()  # nosemgrep: frappe-manual-commit -- only the credential attempt transaction
	finally:
		frappe.local.db = previous
		db.close()


def _record_pin_attempt(user: str, kind: str, *, matched: bool, limit: int, minutes: int, previous: int = 0):
	"""Called with the account's User row locked, including for first enrollment."""
	name = _pin_attempt_name(user, kind)
	with _pin_attempt_connection() as db:
		row = db.get_value(_PIN_ATTEMPT_DOCTYPE, name, ["attempts", "locked_until"], as_dict=True)
		now = now_datetime()
		if matched:
			if row and (row.attempts or row.locked_until):
				db.set_value(_PIN_ATTEMPT_DOCTYPE, name, {"attempts": 0, "locked_until": None})
			return None

		attempts = (cint(row.attempts) if not row.locked_until else 0) if row else previous
		attempts += 1
		until = add_to_date(now, minutes=minutes) if attempts >= limit else None
		if not row:
			db.bulk_insert(_PIN_ATTEMPT_DOCTYPE, ["name", "creation", "modified"], [(name, now, now)])
		db.set_value(_PIN_ATTEMPT_DOCTYPE, name, {"attempts": attempts, "locked_until": until})
		return until


def cashier_pin_state(user: str) -> dict:
	"""Read only non-secret fields. A reset stub is not an enrolled PIN."""
	row = frappe.db.get_value("Cashier PIN", user, ["must_reset", "locked_until"], as_dict=True)
	must_reset = bool(row and cint(row.must_reset))
	locks = [_pin_attempt_lock(user, "pin"), _active_lock(row.locked_until) if row else None]
	locked_until = max((lock for lock in locks if lock), default=None)
	return {"has_pin": bool(row and not must_reset), "must_reset": must_reset,
	        "locked": bool(locked_until), "locked_until": locked_until}


def _floor_pin_state_value(user: str, key: str):
	return frappe.db.get_value(
		"DefaultValue", {"parent": user, "parenttype": _FLOOR_PIN_PARENT, "defkey": key}, "defvalue",
	)


def _horizontal_pin_state(user: str, status_script) -> dict:
	locked_until = _active_lock(_floor_pin_state_value(user, _FLOOR_PIN_LOCK))
	if user == frappe.session.user:
		from kamra import erp_bridge

		status = erp_bridge.call_bridge_script(status_script, {})
		if not isinstance(status, dict) or status.get("store") != "siteops_floor_pin":
			frappe.throw(frappe._("This account cannot manage approval PINs."), frappe.PermissionError)
		return {"has_pin": bool(status.get("has_pin")), "must_reset": bool(status.get("must_reset")),
		        "locked": bool(status.get("locked")) or bool(locked_until), "locked_until": locked_until}
	# The SiteOps status RPC is self-only. Staff lists have already checked
	# target scope and read the same status fields without changing session.
	must_reset = bool(cint(_floor_pin_state_value(user, _FLOOR_PIN_RESET)))
	return {"has_pin": bool(_floor_pin_state_value(user, _FLOOR_PIN_HASH)) and not must_reset,
	        "must_reset": must_reset, "locked": bool(locked_until), "locked_until": locked_until}


def approval_pin_state(user: str, *, bridge_active: bool | None = None) -> dict:
	if (status_script := _horizontal_floor_status_script()) is not None:
		return _horizontal_pin_state(user, status_script)
	state = cashier_pin_state(user)
	if bridge_active is None:
		bridge_active = doa_floor_bridge_active()
	if bridge_active and not (state["has_pin"] or state["must_reset"]):
		state["has_pin"] = bool(_floor_pin_state_value(user, _FLOOR_PIN_HASH))
		floor_lock = _active_lock(_floor_pin_state_value(user, _FLOOR_PIN_LOCK))
		if floor_lock and (not state["locked_until"] or floor_lock > state["locked_until"]):
			state.update(locked=True, locked_until=floor_lock)
	return state


def normalize_approval_pin(pin: str | _PinSecret | None) -> _PinSecret:
	pin = _protect_pin(pin)
	if not (pin and pin.isascii() and pin.isdigit() and 4 <= len(pin) <= 8):
		frappe.throw(frappe._("The PIN must be 4 to 8 digits."))
	steps = {(int(right) - int(left)) % 10 for left, right in zip(pin.reveal(), pin.reveal()[1:])}
	if steps in ({0}, {1}, {9}):
		frappe.throw(frappe._("Choose a PIN without repeated or consecutive digits."))
	return pin


def hash_cashier_pin(pin: str) -> str:
	salt = secrets.token_bytes(16)
	digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PIN_HASH_ROUNDS)
	return f"pbkdf2_sha256${PIN_HASH_ROUNDS}${salt.hex()}${digest.hex()}"


def _matches_cashier_hash(pin: str, encoded_hash: str) -> bool:
	try:
		algorithm, rounds, salt, digest = encoded_hash.split("$")
		if algorithm != "pbkdf2_sha256" or int(rounds) != PIN_HASH_ROUNDS:
			return False
		expected = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(rounds))
		return hmac.compare_digest(expected, bytes.fromhex(digest))
	except (ValueError, UnicodeError):
		return False


def _save_cashier_pin_hash(user: str, encoded_hash: str | None) -> None:
	from frappe.utils.password import remove_encrypted_password

	exists = frappe.db.get_value("Cashier PIN", user, "name", for_update=True)
	doc = frappe.get_doc("Cashier PIN", user, for_update=True) if exists else frappe.get_doc({
		"doctype": "Cashier PIN", "user": user,
	})
	doc.pin = None
	doc.pin_hash = encoded_hash
	doc.must_reset = int(encoded_hash is None)
	doc.pin_attempts = 0
	doc.locked_until = None
	if exists:
		doc.save(ignore_permissions=True)
		remove_encrypted_password("Cashier PIN", user, "pin")
	else:
		doc.insert(ignore_permissions=True)


def _revoke_horizontal_floor_pin(user: str) -> None:
	frappe.db.delete("DefaultValue", {"parent": user, "parenttype": _FLOOR_PIN_PARENT,
	                                "defkey": ["in", [_FLOOR_PIN_HASH, _FLOOR_PIN_LOCK, "siteops_floor_pin_attempts"]]})


def write_cashier_pin(user: str, pin: str | None) -> None:
	"""Update the shared PIN record after locking its User row."""
	encoded_hash = hash_cashier_pin(normalize_approval_pin(pin)) if pin is not None else None
	_save_cashier_pin_hash(user, encoded_hash)
	if doa_floor_bridge_active():
		_revoke_horizontal_floor_pin(user)
	_record_pin_attempt(user, "pin", matched=True, limit=PIN_MAX_ATTEMPTS, minutes=PIN_LOCK_MINUTES)
	frappe.cache.delete_value(f"kamra_cashier_unlock:{user}")


def _write_floor_pin_state(user: str, values: dict) -> None:
	for key, value in values.items():
		filters = {"parent": user, "parenttype": _FLOOR_PIN_PARENT, "defkey": key}
		if frappe.db.get_value("DefaultValue", filters, "name", for_update=True):
			frappe.db.set_value("DefaultValue", filters, "defvalue", str(value))
		else:
			frappe.get_doc({"doctype": "DefaultValue", **filters,
			                "parentfield": "siteops_floor_pin", "defvalue": str(value)}).insert(ignore_permissions=True)


def _floor_pin_key(pin: str) -> bytes:
	return hashlib.sha256(b"siteops_floor_pin_v2|" + pin.encode()).digest()


def _encode_floor_pin(pin: str) -> str:
	salt = secrets.token_bytes(16)
	digest = hashlib.pbkdf2_hmac("sha256", _floor_pin_key(pin), salt, _FLOOR_PIN_ROUNDS)
	return f"pbkdf2_sha256${_FLOOR_PIN_ROUNDS}${salt.hex()}${digest.hex()}"


def _is_weak_floor_pin(pin: str) -> bool:
	"""Match SiteOps delegation-of-authority@7 on the preverified enrollment path."""
	reverse = pin[::-1]
	size = len(pin)
	if pin == reverse or len(set(pin)) <= 2:
		return True
	isolated = sum(
		(index == 0 or digit != pin[index - 1]) and (index == size - 1 or digit != pin[index + 1])
		for index, digit in enumerate(pin)
	)
	if isolated <= 1 or any(pin == (pin[:width] * size)[:size] for width in range(2, size // 2 + 1)):
		return True
	if size == 4 and 1990 <= int(pin) <= 2030:
		return True
	sequence = "01234567890123456789"
	if pin in sequence or reverse in sequence:
		return True

	# The bottom row of the keypad contains only zero, in the middle column.
	positions = ["123456789 0".index(digit) for digit in pin]
	if all(left != right and abs(left // 3 - right // 3) <= 1 and abs(left % 3 - right % 3) <= 1
	       for left, right in zip(positions, positions[1:])):
		return True
	for line in ("123", "456", "789", "147", "258", "369", "159", "357"):
		for walk in (line * 4, (line + "0") * 3, line + "0258258", line + "0852852"):
			if pin in walk or reverse in walk:
				return True
	return pin in ("0001", "1000", "1004", "1122", "2000", "5683", "112233")


def enroll_approval_pin(user: str, pin, current_password: str | None, *, preverified: bool = False) -> None:
	pin, current_password = _protect_pin(pin), _protect_pin(current_password)
	if not preverified or current_password is not None:
		verify_pin_account_password(user, current_password)
	pin = normalize_approval_pin(pin)
	if _horizontal_floor_status_script() is None:
		write_cashier_pin(user, pin)
		return
	if current_password is not None:
		_call_floor_pin_script("set", {"pin": pin, "password": current_password})
	else:
		# The native setter requires a password. The trusted POS hook already
		# proved it. Use the exact same store and format under the User lock.
		if _is_weak_floor_pin(pin.reveal()):
			frappe.throw(frappe._("Choose four to eight digits and avoid common PIN patterns."))
		_write_floor_pin_state(user, {
			_FLOOR_PIN_HASH: _encode_floor_pin(pin),
			_FLOOR_PIN_ATTEMPTS: 0, _FLOOR_PIN_LOCK: "", _FLOOR_PIN_RESET: 0,
		})
	frappe.cache.delete_value(f"kamra_cashier_unlock:{user}")


def _matches_floor_pin(user: str, pin, stored: str) -> bool:
	if not isinstance(pin, _PinSecret) or not pin.isascii() or not pin.isdigit() or not 4 <= len(pin) <= 8:
		return False
	parts = stored.split("$")
	try:
		if len(parts) == 1:
			digest = hashlib.sha256(b"siteops_floor_pin_v1|" + user.lower().encode() + b"|" + pin.encode())
			return hmac.compare_digest(digest.hexdigest(), stored)
		if len(parts) == 4 and parts[:2] == ["pbkdf2_sha256", str(_FLOOR_PIN_ROUNDS)]:
			expected = hashlib.pbkdf2_hmac("sha256", _floor_pin_key(pin), bytes.fromhex(parts[2]), _FLOOR_PIN_ROUNDS)
			return hmac.compare_digest(expected, bytes.fromhex(parts[3]))
	except ValueError:
		return False
	return False


def _check_horizontal_till_pin(user: str, pin, limit: int, minutes: int) -> None:
	# The primary transaction holds the User lock. Update the shared
	# SiteOps counter in its own transaction so a refusal cannot roll it back.
	with _pin_attempt_connection():
		if cint(_floor_pin_state_value(user, _FLOOR_PIN_RESET)):
			frappe.throw("PIN_MUST_RESET: your PIN was reset by an admin - enroll a new PIN first.")
		stored = _floor_pin_state_value(user, _FLOOR_PIN_HASH)
		if not stored:
			frappe.throw("PIN_NOT_SET: set your cashier PIN first (ask for it on this screen), then retry.")
		expiry = _floor_pin_state_value(user, _FLOOR_PIN_LOCK)
		if _active_lock(expiry):
			frappe.throw("PIN_LOCKED: too many wrong attempts - try again later.")
		if pin is None or pin == "":
			frappe.throw("PIN_REQUIRED: this action needs your cashier PIN.")
		matched = _matches_floor_pin(user, pin, str(stored).strip().lower())
		attempts = 0 if matched else (0 if expiry else cint(_floor_pin_state_value(user, _FLOOR_PIN_ATTEMPTS))) + 1
		until = add_to_date(now_datetime(), minutes=minutes) if attempts >= limit else ""
		_write_floor_pin_state(user, {_FLOOR_PIN_ATTEMPTS: 0 if until else attempts, _FLOOR_PIN_LOCK: until})
	if until:
		frappe.cache.delete_value(f"kamra_cashier_unlock:{user}")
		frappe.throw("PIN_LOCKED: too many wrong attempts - try again later.")
	if not matched:
		frappe.throw(frappe._("Wrong cashier PIN."))


def _upgrade_horizontal_floor_pin(user: str, pin: str) -> None:
	"""Adopt an older Floor PIN only after SiteOps authorizes the approval."""
	if frappe.db.exists("Cashier PIN", user):
		return
	# Serialize with enrollment and recheck after locking.
	frappe.db.get_value("User", user, "name", for_update=True)
	if frappe.db.exists("Cashier PIN", user):
		return
	stored = _floor_pin_state_value(user, _FLOOR_PIN_HASH)
	digest = hashlib.sha256(b"siteops_floor_pin_v1|" + user.lower().encode() + b"|" + pin.encode()).hexdigest()
	if stored and hmac.compare_digest(digest.encode(), str(stored).strip().lower().encode()):
		_save_cashier_pin_hash(user, hash_cashier_pin(pin))
		_revoke_horizontal_floor_pin(user)


def pin_password_locked_until(user: str, *, fresh: bool = False):
	until = _pin_attempt_lock(user, "password", fresh=fresh)
	if _horizontal_floor_status_script() is None and not doa_floor_bridge_active():
		return until
	floor_until = _active_lock(_floor_pin_state_value(user, "siteops_floor_pin_pw_locked_until"))
	return max((lock for lock in (until, floor_until) if lock), default=None)


def verify_pin_account_password(user: str, password: str | None) -> None:
	"""Verify without logging the user in again or resetting the login tracker."""
	from frappe.utils.password import check_password

	password = _protect_pin(password)
	if pin_password_locked_until(user, fresh=True):
		frappe.throw(frappe._("Too many incorrect passwords. Try again later."))
	verified = False
	if password:
		try:
			check_password(user, password.reveal(), delete_tracker_cache=False)
			verified = True
		except frappe.AuthenticationError:
			pass
	if not verified:
		until = _record_pin_attempt(user, "password", matched=False, limit=PIN_MAX_ATTEMPTS, minutes=PIN_LOCK_MINUTES)
		if until:
			frappe.throw(frappe._("Too many incorrect passwords. Try again later."))
		frappe.throw(frappe._("The account password is incorrect."))
	_record_pin_attempt(user, "password", matched=True, limit=PIN_MAX_ATTEMPTS, minutes=PIN_LOCK_MINUTES)


def _pin_user_scope(user: str, doctype: str) -> set[str] | None:
	from frappe.core.doctype.user_permission.user_permission import get_user_permissions

	permissions = get_user_permissions(user).get(doctype)
	if not permissions:
		return None
	user_scope = {entry["doc"] for entry in permissions if entry.get("doc") and entry.get("applicable_for") == "User"}
	return user_scope or {entry["doc"] for entry in permissions if entry.get("doc") and not entry.get("applicable_for")}


def _pin_company_scope(user: str) -> set[str] | None:
	return _pin_user_scope(user, "Company")


def _pin_property_scope(user: str) -> set[str] | None:
	allowed = restricted_properties(user)
	return {prop for prop in allowed if prop} if allowed is not None else None


def _pin_scope_allows(user: str, property: str | None, company: str | None) -> bool:
	properties = _pin_property_scope(user)
	companies = _pin_company_scope(user)
	return (properties is None or property in properties) and (companies is None or company in companies)


def _pos_pin_company(pos_profile: str | None, property: str | None) -> str:
	if pos_profile and _doctype_has_field("POS Profile", "company"):
		company = frappe.db.get_value("POS Profile", pos_profile, "company")
		if company:
			return company
	return _property_company(property)


def _pin_scope_covers(actor: str, target: str) -> bool:
	for scope in (_pin_property_scope, _pin_company_scope):
		mine, theirs = scope(actor), scope(target)
		if mine == set() or theirs == set() or (mine is not None and (theirs is None or not theirs <= mine)):
			return False
	return True


def _pin_scopes_overlap(actor: str, target: str) -> bool:
	for scope in (_pin_property_scope, _pin_company_scope):
		mine, theirs = scope(actor), scope(target)
		if mine == set() or theirs == set() or (mine is not None and theirs is not None and not mine & theirs):
			return False
	return True


def _unique_reset_scope(scopes) -> str | None:
	assigned = [scope for scope in scopes if scope is not None]
	shared = set.intersection(*assigned) if assigned else set()
	return next(iter(shared)) if len(shared) == 1 else None


def _linked_pin_scope(doctype: str, name: str | None, field: str) -> str:
	if name:
		for link in (field, f"custom_{field}"):
			if _doctype_has_field(doctype, link):
				value = frappe.db.get_value(doctype, name, link)
				if value:
					return str(value)
	return ""


def _floor_pin_reset_scope(actor, target, property, pos_profile):
	accounts = (actor, target)
	prop = property or _unique_reset_scope(_pin_property_scope(user) for user in accounts)
	company = _linked_pin_scope("POS Profile", pos_profile, "company") or _linked_pin_scope("Property", prop, "company")
	if not company:
		company = _unique_reset_scope(_pin_company_scope(user) for user in accounts) or ""
	if not company and not set(frappe.get_roles(actor)).intersection(IT_ADMIN):
		frappe.throw(frappe._("This account cannot manage approval PINs."), frappe.PermissionError)

	branch = _linked_pin_scope("POS Profile", pos_profile, "branch") or _linked_pin_scope("Property", prop, "branch")
	if not branch and prop and _doctype_has_field("Branch", "custom_property"):
		branches = frappe.get_all("Branch", filters={"custom_property": prop}, pluck="name", limit=2)
		branch = branches[0] if len(branches) == 1 else ""
	if not branch and not property and pos_profile is None:
		branch = _unique_reset_scope(_pin_user_scope(user, "Branch") for user in accounts) or ""
	return prop, company, branch


def manage_pos_override_pin(
	action: str,
	*,
	user: str | None = None,
	pin: str | None = None,
	current_password: str | None = None,
	password_verified: bool | None = None,
	caller: str | None = None,
	pos_profile: str | None = None,
	property: str | None = None,
) -> dict:
	"""Internal POS hook. A missing password uses the caller's server-side reauthentication."""
	from kamra.savings import log_action

	pin, current_password = _protect_pin(pin), _protect_pin(current_password)
	_scrub_pin_from_form_dict()
	_reject_pin_query(getattr(frappe.local, "request", None))
	actor = require_pin_identity()
	if caller is not None and (not isinstance(caller, str) or caller.strip().casefold() != actor.casefold()):
		frappe.throw(frappe._("This account cannot manage approval PINs."), frappe.PermissionError)
	if action not in ("status", "set", "clear"):
		frappe.throw(frappe._("Unknown approval PIN action."))
	target = require_pin_identity(user if user is not None else actor, for_update=action != "status")
	if target != actor:
		roles = set(frappe.get_roles(actor))
		if (
			action == "set"
			or not roles.intersection((*ADMIN, "URY Admin", "URY Manager", "Accounts Manager"))
			or ("System Manager" in frappe.get_roles(target) and not roles.intersection(IT_ADMIN))
		):
			frappe.throw(frappe._("This account cannot manage approval PINs."), frappe.PermissionError)
		if action == "clear" and not _pin_scope_covers(actor, target):
			frappe.throw(frappe._("This account cannot manage approval PINs."), frappe.PermissionError)
		if action == "status" and pos_profile is None and not _pin_scopes_overlap(actor, target):
			return {"has_pin": False, "must_reset": False, "locked": False}

	prop = property
	if pos_profile is not None:
		prop = resolve_property_for_pos_profile(pos_profile, user=actor, strict=True)
		if not prop:
			frappe.throw(frappe._("This account cannot manage approval PINs."), frappe.PermissionError)
	horizontal_reset = action == "clear" and _horizontal_floor_status_script() is not None
	branch = ""
	if horizontal_reset:
		prop, company, branch = _floor_pin_reset_scope(actor, target, prop, pos_profile)
	else:
		company = _pos_pin_company(pos_profile, prop) if prop else ""
	if pos_profile is not None and company:
		property_company = _linked_pin_scope("Property", prop, "company")
		if property_company and property_company != company:
			frappe.throw(frappe._("This account cannot manage approval PINs."), frappe.PermissionError)
	if prop and any(not _pin_scope_allows(account, prop, company) for account in {actor, target}):
		frappe.throw(frappe._("This account cannot manage approval PINs."), frappe.PermissionError)

	if action == "status":
		status = approval_pin_state(target)
		status.pop("locked_until")
		status["locked"] |= bool(pin_password_locked_until(target))
		return status
	if action == "set":
		enroll_approval_pin(target, pin, current_password, preverified=password_verified is not False)
		log_action("set_cashier_pin", "Cashier PIN", target, None)
		return {"ok": True, "has_pin": True}
	if horizontal_reset:
		_call_floor_pin_script("clear", {"user": target, "property": prop or "", "company": company, "branch": branch})
		frappe.cache.delete_value(f"kamra_cashier_unlock:{target}")
	else:
		write_cashier_pin(target, None)
	log_action("reset_cashier_pin", "Cashier PIN", target, None, rationale=f"PIN reset by {actor}")
	return {"ok": True, "has_pin": False, "must_reset": True}


def verify_pos_override_pin(
	approver: str | None,
	pin: str | None,
	pos_profile: str | None = None,
	*,
	property: str | None = None,
	requester: str | None = None,
	privilege: str | None = None,
	amount=None,
	currency=None,
	action=None,
	reference_doctype=None,
	reference_name=None,
) -> dict:
	"""Frappe hook (`pos_override_pin_verifier`) called by POS override verification.

	Reuses Kamra's existing Cashier PIN store through `verify_supervisor`:
	same 5/15 lockout, self-approval refused, property scope enforced, AI agents
	and Administrator/Guest refused, and `form_dict` PIN scrubbed.
	Returns only yes/no + approver display name on success (and a neutral refusal
	code on failure). Never introduces a second PIN store.
	"""
	pin = _protect_pin(pin)
	_scrub_pin_from_form_dict()
	_reject_pin_query(getattr(frappe.local, "request", None))
	try:
		if (
			getattr(frappe.flags, "kamra_agent_call", False)
			or getattr(frappe.flags, "kamra_gate_bypass", False)
			or "Kamra Agent" in frappe.get_roles()
		):
			return {"ok": False, "name": None, "code": "POS-AUTH-NOT-ELIGIBLE"}

		req = (requester if requester is not None else getattr(frappe.session, "user", None)) or ""
		if not req or req == "Guest":
			return {"ok": False, "name": None, "code": "POS-AUTH-NOT-ELIGIBLE"}

		approver_in = (approver or "").strip()
		if not approver_in or approver_in.casefold() in ("administrator", "guest"):
			return {"ok": False, "name": None, "code": "POS-AUTH-NOT-ELIGIBLE"}

		canonical = frappe.db.exists("User", approver_in)
		if not canonical:
			return {"ok": False, "name": None, "code": "POS-AUTH-NOT-ELIGIBLE"}
		approver_user = str(canonical)

		if approver_user.casefold() == str(req).casefold():
			return {"ok": False, "name": None, "code": "POS-AUTH-SELF"}

		prop = property
		if pos_profile is not None:
			prop = resolve_property_for_pos_profile(pos_profile, user=req, strict=True)
			if property is not None and prop != property:
				return {"ok": False, "name": None, "code": "POS-AUTH-NOT-ELIGIBLE"}
		if not prop:
			return {"ok": False, "name": None, "code": "POS-AUTH-NOT-ELIGIBLE"}
		company = _pos_pin_company(pos_profile, prop)
		property_company = _linked_pin_scope("Property", prop, "company")
		if pos_profile is not None and property_company and company != property_company:
			return {"ok": False, "name": None, "code": "POS-AUTH-NOT-ELIGIBLE"}
		if any(not _pin_scope_allows(account, prop, company) for account in (req, approver_user)):
			return {"ok": False, "name": None, "code": "POS-AUTH-NOT-ELIGIBLE"}

		try:
			verified = verify_supervisor(
				prop or "",
				approver_user,
				pin,
				roles=POS_SUPERVISOR_ROLES,
				requester=req,
				company=company,
				amount=amount,
				currency=currency,
				action=action,
				reference_doctype="POS Invoice" if reference_doctype is None else reference_doctype,
				reference_name=reference_name,
				reason=action or privilege or "pos_override",
			)
		except frappe.PermissionError as exc:
			frappe.clear_messages()
			msg = str(exc)
			code = (
				"POS-AUTH-SELF"
				if ("SUPERVISOR_SELF_APPROVAL" in msg or "SITEOPS-DOA-SELF" in msg)
				else "POS-AUTH-NOT-ELIGIBLE"
			)
			return {"ok": False, "name": None, "code": code}
		except frappe.ValidationError as exc:
			frappe.clear_messages()
			msg = str(exc)
			if "PIN_LOCKED" in msg or "SITEOPS-DOA-LOCKED" in msg:
				code = "POS-AUTH-LOCKED"
			elif any(tag in msg for tag in ("PIN_NOT_SET", "PIN_MUST_RESET", "no Floor PIN enrolled", "must be reset")):
				code = "POS-AUTH-NO-PIN"
			elif "SITEOPS-DOA-SELF" in msg:
				code = "POS-AUTH-SELF"
			elif any(tag in msg for tag in ("SITEOPS-DOA-ELIGIBLE", "SITEOPS-DOA-ROLE", "SITEOPS-DOA-UNCONFIGURED", "SITEOPS-DOA-LIMIT")):
				code = "POS-AUTH-NOT-ELIGIBLE"
			else:
				code = "POS-AUTH-PIN"
			return {"ok": False, "name": None, "code": code}

		full_name = frappe.db.get_value("User", verified, "full_name") or verified
		return {"ok": True, "name": str(full_name)}
	finally:
		_scrub_pin_from_form_dict()


def _check_user_pin(user: str, pin, property: str | None):
	"""Check the shared cashier PIN and serialize guesses across till and approval."""
	pin = _protect_pin(pin)
	max_attempts, lock_minutes = _pin_policy(property)
	frappe.db.get_value("User", user, "name", for_update=True)
	if _horizontal_floor_status_script() is not None:
		_check_horizontal_till_pin(user, pin, max_attempts, lock_minutes)
		return
	if not frappe.db.get_value("Cashier PIN", user, "name", for_update=True):
		frappe.throw("PIN_NOT_SET: set your cashier PIN first (ask for it on "
		             "this screen), then retry.")
	doc = frappe.get_doc("Cashier PIN", user, for_update=True)
	if doc.get("must_reset"):
		frappe.throw("PIN_MUST_RESET: your PIN was reset by an admin - "
		             "enroll a new PIN first.")
	if _pin_locked(doc) or _pin_attempt_lock(user, "pin", fresh=True):
		frappe.throw("PIN_LOCKED: too many wrong attempts - try again later.")
	if pin is None or pin == "":
		frappe.throw("PIN_REQUIRED: this action needs your cashier PIN.")
	encoded_hash = doc.get("pin_hash")
	matched = False
	if isinstance(pin, _PinSecret) and pin.isascii() and pin.isdigit() and len(pin) <= 8:
		if encoded_hash:
			matched = _matches_cashier_hash(pin, encoded_hash)
		else:
			from frappe.utils.password import get_decrypted_password
			stored = get_decrypted_password("Cashier PIN", user, "pin", raise_exception=False)
			matched = bool(stored and hmac.compare_digest(pin.encode(), str(stored).encode()))

	until = _record_pin_attempt(user, "pin", matched=matched, limit=max_attempts, minutes=lock_minutes,
	                            previous=cint(doc.pin_attempts) if not doc.locked_until else 0)
	if not matched:
		if until:
			frappe.cache.delete_value(f"kamra_cashier_unlock:{user}")
			frappe.throw("PIN_LOCKED: too many wrong attempts - locked for "
			             f"{lock_minutes} minutes.")
		frappe.throw(frappe._("Wrong cashier PIN."))
	if not encoded_hash:
		# Existing tenants keep their current PIN until they choose to rotate it.
		_save_cashier_pin_hash(user, hash_cashier_pin(pin))
	elif doc.pin_attempts or doc.locked_until:
		frappe.db.set_value("Cashier PIN", user, {"pin_attempts": 0, "locked_until": None})


def unlock_cashier_session(property: str, pin: str) -> dict:
	"""Explicit PinPad verify: validates PIN and opens the unlock window."""
	user = require_pin_identity()
	cache_key = f"kamra_cashier_unlock:{user}"
	frappe.cache.delete_value(cache_key)
	_check_user_pin(user, pin, property)
	minutes = _pin_policy(property)[1]
	frappe.cache.set_value(cache_key, 1, expires_in_sec=minutes * 60)
	return {"ok": True, "unlocked_minutes": minutes}


def cashier_unlock_status(property: str) -> dict:
	required = bool(property and frappe.db.get_value(
		"Property", property, "require_cashier_pin"))
	user = frappe.session.user
	state = approval_pin_state(user)
	locked = state["locked"] or (not state["has_pin"] and bool(pin_password_locked_until(user)))
	unlocked = bool(frappe.cache.get_value(f"kamra_cashier_unlock:{user}"))
	return {
		"required": required,
		"has_pin": state["has_pin"],
		"must_reset": state["must_reset"],
		"locked": locked,
		"unlocked": (unlocked and state["has_pin"] and not locked) or not required or user == "Administrator",
		# how long an unlock lasts here (Settings -> Finance, pin_lock_minutes)
		"unlock_minutes": _pin_policy(property)[1] if property else PIN_LOCK_MINUTES,
	}
