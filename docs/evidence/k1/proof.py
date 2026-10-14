import json
import frappe
from kamra.tests.test_channel_manager_availability import ChannelManagerAvailabilityTestCase
from kamra.channel_manager import ari_snapshot
from kamra.channels.aiosell import build_push_bodies

frappe.local.lang = "en"
case = ChannelManagerAvailabilityTestCase()
case.setUp()
stay = case.reservation()
snapshot = ari_snapshot(case.property, case.conn.name, days=1)
inventory, rates = build_push_bodies("demo", snapshot)
assert snapshot[0]["days"][0]["available"] == 0
proof = {"fix": "K1", "property": case.property, "room_type": case.room_type,
         "physical_rooms": frappe.db.count("Room", {"room_type": case.room_type}),
         "reservation": stay.name, "room_assigned": bool(stay.room), "status": stay.status,
         "function": "kamra.channels.aiosell.build_push_bodies",
         "push_preview": {"hotelCode": "demo", "updates": inventory},
         "outbound_network_calls": 0}
file = frappe.get_doc({"doctype": "File", "file_name": "kamra-k1-preview.json",
                       "is_private": 0, "content": json.dumps(proof, indent=2)}).insert(ignore_permissions=True)
frappe.db.commit()
print(file.file_url)
