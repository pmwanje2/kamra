"""Transport contracts, using synthetic resolver and HTTPS pool."""
import unittest
from unittest.mock import patch


class BridgeTransport(unittest.TestCase):
	def test_metadata_addresses_always_excluded(self):
		from kamra.bridge_transport import allowed_address
		for ip in ("169.254.169.254", "169.254.1.2", "fe80::1", "fd00:ec2::254", "::ffff:169.254.1.1"):
			self.assertFalse(allowed_address(ip, allow_private=True))
		self.assertTrue(allowed_address("10.4.1.9", allow_private=True))

	def test_https_connects_to_validated_address_and_checks_hostname(self):
		from kamra import bridge_transport
		with patch.object(bridge_transport, "resolve_address", return_value="203.0.113.5"), patch.object(bridge_transport, "HTTPSConnectionPool") as pool:
			pool.return_value.urlopen.return_value.status = 204
			bridge_transport.post_json("https://encoder.example/key", {}, "test", allow_private=False)
			self.assertEqual(pool.call_args.args[0], "203.0.113.5")
			self.assertEqual(pool.call_args.kwargs["server_hostname"], "encoder.example")
			self.assertEqual(pool.call_args.kwargs["assert_hostname"], "encoder.example")

	def test_resolver_wait_is_bounded(self):
		from threading import Event
		from kamra import bridge_transport
		gate = Event()
		try:
			with patch("socket.getaddrinfo", side_effect=lambda *a, **k: gate.wait(2)):
				with self.assertRaises(TimeoutError):
					bridge_transport.resolve_address("encoder.example", 443, allow_private=False, timeout=0.01)
		finally:
			gate.set()
