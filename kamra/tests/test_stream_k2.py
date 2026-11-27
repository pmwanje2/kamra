import unittest
from unittest.mock import patch
import importlib.util
import sys
from pathlib import Path


class InventoryPolicy(unittest.TestCase):
    def test_protected_sell_limits(self):
        from kamra.sell_limits import effective_capacities
        rooms = {"double": 8, "suite": 2}
        self.assertEqual(effective_capacities(rooms, {"double": 2, "suite": -1}, 1, True),
                         {"double": 10, "suite": 1})
        with self.assertRaises(ValueError):
            effective_capacities(rooms, {"double": 1}, 0, True)
        self.assertEqual(effective_capacities(rooms, {"suite": -9}, 0, True)["suite"], 0)

    def test_walk_cost_currency_precision(self):
        from kamra.sell_limits import relocation_minor_units
        for amount, digits, expected in [("357", 0, 357), ("14.95", 2, 1495), ("2.005", 3, 2005)]:
            self.assertEqual(relocation_minor_units(amount, digits), expected)
        with self.assertRaises(ValueError):
            relocation_minor_units("1.005", 2)


class EncoderContract(unittest.TestCase):
    def test_bridge_payload_and_simulator(self):
        spec = importlib.util.spec_from_file_location("isolated_lock", Path(__file__).parents[1] / "door_lock.py")
        locks = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"isolated_lock": locks, "frappe": unittest.mock.Mock()}):
            spec.loader.exec_module(locks)
            req = locks.KeyRequest("P", "R", "101", "101", "2026-01-01", "2026-01-02",
                                   guest_name="Private", options={"encoder_id": "DESK", "private": "hidden"})
            payload = locks.bridge_payload(req)
            self.assertEqual(payload["encoder_id"], "DESK")
            self.assertNotIn("Private", str(payload))
            self.assertNotIn("hidden", str(payload))
            adapter = locks.registered_adapters()["simulator"]()
            self.assertTrue(adapter.issue_key(req).ok)
            self.assertEqual(adapter.issue_key(req), adapter.issue_key(req))
