"""Door lock interface - the contract a lock vendor connector implements.

Properties select a connector in Settings -> Front Desk -> Door locks.
External apps can register additional adapters.

    class AcmeAdapter(DoorLockAdapter):
        key = "acme"
        label = "Acme Locks (cloud)"

        def issue_key(self, request: KeyRequest) -> KeyResult: ...
        def revoke_keys(self, property, reservation, room) -> KeyResult: ...

    # in your app's hooks.py
    kamra_door_lock_adapters = {"acme": "acme_kamra.locks.AcmeAdapter"}

Rules for a connector:
    - it is called after Kamra has decided (check-in done, room assigned);
      it never decides who gets a key, and it never changes the booking;
    - it gets the room, the stay window and a key count - no guest contact
      details or ID documents;
    - credentials live in the connector's own encrypted settings, never in
      the payload or the logs;
    - a failure returns KeyResult(ok=False, message=...) for the desk to
      show; it must not raise into check-in.

A connector can also listen to the guest.checked_in / guest.checked_out
outbound webhooks instead of running in-process (middleware on the lock
vendor's side) - see docs-site/integrations.md.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace

import frappe

DEFAULT_ADAPTER = "manual"


@dataclass(frozen=True)
class KeyRequest:
	property: str
	reservation: str
	room: str
	room_number: str
	valid_from: str  # ISO datetime, property local time
	valid_to: str
	key_count: int = 1
	guest_name: str | None = None  # for the card label only
	options: dict = field(default_factory=dict)  # vendor-specific (encoder id, ...)


@dataclass
class KeyResult:
	ok: bool
	adapter: str
	message: str = ""
	key_ids: list = field(default_factory=list)  # vendor card / credential ids
	manual: bool = False  # staff must finish it at the vendor's encoder
	uncertain: bool = False  # request may have reached the physical encoder


class DoorLockAdapter(ABC):
	"""Base class every lock connector implements."""

	key: str = ""
	label: str = ""
	# what the connector can do, so the desk only offers those buttons
	supports_mobile_keys: bool = False
	supports_revoke: bool = True

	@abstractmethod
	def issue_key(self, request: KeyRequest) -> KeyResult:
		"""Make `request.key_count` keys for the room, valid for the stay."""

	@abstractmethod
	def revoke_keys(self, property: str, reservation: str, room: str) -> KeyResult:
		"""Cancel every key issued for this stay and room (check-out, room
		move, lost card)."""

	def bind_target(self, request: KeyRequest) -> tuple[KeyRequest, str | None]:
		"""Snapshot the dispatch destination and its non-secret journal identity."""
		return request, None

	def revoke_request(self, request: KeyRequest) -> KeyResult:
		"""Adapters with an idempotent transport can use the journal request ID."""
		return self.revoke_keys(request.property, request.reservation, request.room)

	def health(self, property: str) -> dict:
		"""Optional: is the vendor reachable / configured for this property?"""
		return {"ok": True}


class ManualAdapter(DoorLockAdapter):
	"""No connected lock system: staff cut cards at the vendor's encoder.
	Returns an instruction so the desk can prompt them; does nothing else."""

	key = "manual"
	label = "Manual (encode cards at the lock's encoder)"
	supports_revoke = False

	def issue_key(self, request: KeyRequest) -> KeyResult:
		return KeyResult(
			ok=True, adapter=self.key, manual=True,
			message=f"Encode {request.key_count} key card(s) for room "
			        f"{request.room_number}, valid until {request.valid_to}.")

	def revoke_keys(self, property: str, reservation: str, room: str) -> KeyResult:
		return KeyResult(ok=True, adapter=self.key, manual=True,
		                 message="Collect the key cards; they expire at check-out time.")


def bridge_payload(request: KeyRequest) -> dict:
	"""Allowlisted stay and encoder fields for the HTTPS middleware."""
	return {
		"operation": request.options.get("operation", "issue"),
		"request_id": request.options.get("request_id"),
		"property": request.property, "reservation": request.reservation,
		"room": request.room, "room_number": request.room_number,
		"valid_from": request.valid_from, "valid_to": request.valid_to,
		"key_count": request.key_count, "encoder_id": request.options.get("encoder_id"),
	}


class SimulatorAdapter(DoorLockAdapter):
	key = "simulator"
	label = "Simulator"
	supports_mobile_keys = True

	def issue_key(self, request):
		import hashlib
		import json
		digest = hashlib.sha256(json.dumps(bridge_payload(request), sort_keys=True).encode()).hexdigest()
		return KeyResult(True, self.key, frappe._("Simulated key operation completed."),
		                 key_ids=[f"sim-{digest[:16]}-{i + 1}" for i in range(request.key_count)])

	def revoke_keys(self, property, reservation, room):
		return KeyResult(True, self.key, frappe._("Simulated revocation completed."))


class EdgeAdapter(DoorLockAdapter):
	key = "fias_edge"
	label = "HTTPS encoder bridge"
	supports_mobile_keys = True

	def bind_target(self, request):
		import hashlib
		from kamra.bridge_transport import bridge_endpoint
		from kamra.settings import get_setting
		url = get_setting(request.property, "door_lock_endpoint")
		try:
			target = bridge_endpoint(url)
			identity = f"{target.hostname}|{target.port or 443}|{target.path or '/'}"
		except ValueError:
			url, identity = "", "unconfigured"
		fingerprint = hashlib.sha256(identity.encode()).hexdigest()
		return replace(request, options={**request.options, "bridge_url": url}), fingerprint

	def _send(self, property, payload, *, endpoint=None):
		from kamra.bridge_transport import post_json
		from kamra.settings import get_setting
		from kamra.webhooks import _allow_private
		message = frappe._("Encoder bridge unavailable. Use the manual encoder.")
		try:
			url = get_setting(property, "door_lock_endpoint") if endpoint is None else endpoint
			secret = frappe.get_doc("Property", property).get_password("door_lock_secret", raise_exception=False)
			allow_private = _allow_private()
		except Exception:
			return KeyResult(False, self.key, message)
		if not url or not secret:
			return KeyResult(False, self.key, message)
		try:
			code = post_json(url, payload, secret, allow_private=allow_private)
		except Exception:
			# A transport failure can follow a successful physical write.
			return KeyResult(False, self.key, message, uncertain=True)
		if 200 <= code < 300:
			return KeyResult(True, self.key, frappe._("Encoder bridge accepted the request."))
		return KeyResult(False, self.key, message, uncertain=code >= 500 or code == 408)

	def issue_key(self, request):
		return self._send(request.property, bridge_payload(request), endpoint=request.options.get("bridge_url"))

	def revoke_request(self, request):
		payload = bridge_payload(request)
		payload["operation"] = "revoke"
		if request.options["operation"] == "rekey":
			# The bridge deduplicates revocation and subsequent issuance separately.
			payload["request_id"] += "-revoke"
		return self._send(request.property, payload, endpoint=request.options.get("bridge_url"))

	def revoke_keys(self, property, reservation, room):
		return self._send(property, {"operation": "revoke", "property": property,
		                             "reservation": reservation, "room": room})


_BUILTIN = {cls.key: cls for cls in (ManualAdapter, SimulatorAdapter, EdgeAdapter)}
_BUILTIN["webhook_bridge"] = EdgeAdapter


def registered_adapters() -> dict[str, type[DoorLockAdapter]]:
	"""Built-in adapters plus every app's `kamra_door_lock_adapters` hook."""
	out = dict(_BUILTIN)
	hooks = frappe.get_hooks("kamra_door_lock_adapters") or {}
	# dict hooks merge across apps as {key: [path, ...]}; the last app wins
	for key, paths in (hooks.items() if isinstance(hooks, dict) else []):
		path = paths[-1] if isinstance(paths, (list, tuple)) else paths
		try:
			cls = frappe.get_attr(path)
		except Exception:
			frappe.log_error(title=f"Door lock adapter {key} failed to load")
			continue
		if isinstance(cls, type) and issubclass(cls, DoorLockAdapter):
			out[key] = cls
	return out


def get_adapter(property: str | None) -> DoorLockAdapter:
	"""The adapter the property chose; the manual one when unset or unknown."""
	from kamra.settings import get_setting
	key = get_setting(property, "door_lock_adapter", DEFAULT_ADAPTER) or DEFAULT_ADAPTER
	cls = registered_adapters().get(key) or ManualAdapter
	return cls()
