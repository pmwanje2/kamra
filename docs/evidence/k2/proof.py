import json
from unittest.mock import patch
import frappe
from kamra.tests.test_reservation_capacity import ReservationCapacityTestCase

frappe.local.lang = "en"
case = ReservationCapacityTestCase()
case.setUp()
doc = case.reservation()
with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as queries:
    doc.validate_type_capacity()
sql = [str(call.args[0]) for call in queries.call_args_list]
assert "FOR UPDATE" in sql[0].upper() and "tabRoom Type" in sql[0]
proof = {"fix": "K2", "property": case.property, "room_type": case.room_type,
         "function": "Reservation.validate_type_capacity", "real_database_calls": True,
         "query_order": [{"index": i, "sql": q} for i, q in enumerate(sql)
                         if "FOR UPDATE" in q.upper() or "COUNT(" in q.upper()],
         "result": "Room Type row locked before physical and reservation counts"}
file = frappe.get_doc({"doctype": "File", "file_name": "kamra-k2-lock.json", "is_private": 0,
                       "content": json.dumps(proof, indent=2)}).insert(ignore_permissions=True)
frappe.db.commit()
print(file.file_url)
