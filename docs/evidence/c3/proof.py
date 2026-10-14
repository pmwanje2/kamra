"""Read live supervisor decisions on the disposable bench; no production wiring."""

import json

import frappe

from kamra.tests.test_supervisor_reason import SupervisorReasonTestCase


frappe.local.lang = "en"
case = SupervisorReasonTestCase()
case.setUp()
proof = {
    "fix": "C3",
    "source_sha": "65bfc3832db242d38826923c5e2e61770850e994",
    "property": case.property,
    "require_cashier_pin": frappe.db.get_value(
        "Property", case.property, "require_cashier_pin"
    ),
    "reason_code": case.reason,
    "requires_supervisor": frappe.db.get_value(
        "Reason Code", case.reason, "requires_supervisor"
    ),
    "decisions": [],
}
for user, role in (
    (case.finance, "Finance"),
    (case.hotel_admin, "Hotel Admin"),
    (case.frontdesk, "Front Desk"),
):
    decision = {"user": user, "role": role, "pin_supplied": False}
    try:
        decision["result"] = case.reason_for(user)
        decision["allowed"] = True
    except frappe.PermissionError as error:
        decision["allowed"] = False
        decision["exception"] = type(error).__name__
        decision["message"] = str(error)
    decision["effective_roles"] = frappe.get_roles()
    proof["decisions"].append(decision)
assert proof["require_cashier_pin"] == 0
assert proof["requires_supervisor"] == 1
assert [row["allowed"] for row in proof["decisions"]] == [True, True, False]
case.tearDown()
file = frappe.get_doc({
    "doctype": "File",
    "file_name": "kamra-c3-supervisor-decisions.json",
    "is_private": 0,
    "content": json.dumps(proof, indent=2, default=str),
}).insert(ignore_permissions=True)
frappe.db.commit()
print(file.file_url)
print(json.dumps(proof, indent=2, default=str))
