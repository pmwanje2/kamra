import json
import frappe
from kamra.tests.test_channel_manager_bookings import ChannelManagerBookingTestCase
from kamra.channel_manager import process_webhook_events

frappe.local.lang = "en"
case = ChannelManagerBookingTestCase()
case.setUp()
case.sold_out()
payload = case.payload()
frappe.db.commit()
process_webhook_events(case.conn.name, payload)
doc = case.received()
assert doc.status == "Requested"
assert "OTA overbooking — resolve" in doc.special_requests
proof = {"fix": "K3", "worker": "process_webhook_events", "real_commit_and_rollback": True,
         "reservation": {field: doc.get(field) for field in (
             "name", "property", "room_type", "ota_ref", "source", "status", "special_requests",
             "check_in_date", "check_out_date", "amount_after_tax")}}
file = frappe.get_doc({"doctype": "File", "file_name": "kamra-k3-requested.json", "is_private": 0,
                       "content": json.dumps(proof, indent=2, default=str)}).insert(ignore_permissions=True)
frappe.db.commit()
print(file.file_url)
print("Requested reservation:", doc.name)
