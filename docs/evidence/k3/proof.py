"""Live K3 proof on the disposable bench: real worker, real commit/rollback."""

import json

import frappe

from kamra.channel_manager import _sellable, process_webhook_events
from kamra.tests.test_channel_manager_bookings import ChannelManagerBookingTestCase

FIELDS = ("name", "ota_ref", "source", "status", "special_requests",
          "cancellation_reason", "cancellation_note", "amount_after_tax")

frappe.local.lang = "en"
case = ChannelManagerBookingTestCase()
case.setUp()
case.sold_out()
frappe.db.commit()
nights = (case.payload()["checkin"], case.payload()["checkout"])

process_webhook_events(case.conn.name, case.payload())
booked = case.received()
after_book = {f: booked.get(f) for f in FIELDS}
after_book["sellable_tonight"] = _sellable(case.property, case.room_type, *nights)
assert booked.status == "Requested"

process_webhook_events(case.conn.name, {"action": "cancel", "hotelCode": "demo",
                                        "channel": "Booking.com", "bookingId": case.ref})
cancelled = case.received()
after_cancel = {f: cancelled.get(f) for f in FIELDS}
assert cancelled.status == "Cancelled"

proof = {"fix": "K3", "worker": "process_webhook_events", "real_commit_and_rollback": True,
         "1_sold_out_book_event": after_book, "2_ota_cancel_event": after_cancel}
file = frappe.get_doc({"doctype": "File", "file_name": "kamra-k3-requested-cancel.json",
                       "is_private": 0, "content": json.dumps(proof, indent=2, default=str)})
file.insert(ignore_permissions=True)
frappe.db.commit()
print(file.file_url)
print(json.dumps(proof, indent=2, default=str))
