"""Pinned HTTPS transport for property encoder bridges."""
import ipaddress
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from urllib.parse import urlsplit

from urllib3 import HTTPSConnectionPool, Timeout

_resolvers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bridge-dns")
_slots = BoundedSemaphore(4)
_private_networks = tuple(map(ipaddress.ip_network, (
	"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
)))


_platform_networks = tuple(map(ipaddress.ip_network, (
	"168.63.129.16/32", "fd00:ec2::/64", "fd20:ce::/64",
)))


def allowed_address(address, allow_private=False):
	ip = ipaddress.ip_address(address.split("%", 1)[0])
	ip = getattr(ip, "ipv4_mapped", None) or ip
	if ip.is_link_local or ip.is_unspecified or ip.is_multicast or any(ip in net for net in _platform_networks):
		return False
	return ip.is_global or (allow_private and (ip.is_loopback or any(ip in net for net in _private_networks)))


def resolve_address(host, port, *, allow_private, timeout=2):
	if not _slots.acquire(blocking=False):
		raise TimeoutError("DNS resolver busy")
	try:
		job = _resolvers.submit(socket.getaddrinfo, host, port, proto=socket.IPPROTO_TCP)
	except BaseException:
		_slots.release()
		raise
	job.add_done_callback(lambda _: _slots.release())
	addresses = [row[4][0] for row in job.result(timeout=timeout)]
	if not addresses or not all(allowed_address(ip, allow_private) for ip in addresses):
		raise ValueError("Address outside permitted networks")
	return addresses[0]


def bridge_endpoint(url):
	try:
		target = urlsplit(url)
		valid = (target.scheme == "https" and target.hostname and target.username is None
		         and target.password is None and not any(c in url for c in ("?", "#", "\\"))
		         and not any(ord(c) <= 32 for c in url) and target.port != 0)
	except (ValueError, TypeError):
		valid = False
	if not valid:
		raise ValueError("Enter an HTTPS endpoint without credentials, query or fragment.")
	return target


def post_json(url, payload, secret, *, allow_private):
	target = bridge_endpoint(url)
	port = target.port or 443
	ip = resolve_address(target.hostname, port, allow_private=allow_private)
	pool = HTTPSConnectionPool(ip, port=port, server_hostname=target.hostname,
	                           assert_hostname=target.hostname, cert_reqs="CERT_REQUIRED",
	                           timeout=Timeout(connect=2, read=3))
	try:
		response = pool.urlopen("POST", target.path or "/", body=json.dumps(payload).encode(),
		                        headers={"Host": target.netloc, "Content-Type": "application/json",
		                                 "Authorization": "Bearer " + secret},
		                        retries=False, redirect=False, preload_content=False)
		try:
			return response.status
		finally:
			response.close()
	finally:
		pool.close()
