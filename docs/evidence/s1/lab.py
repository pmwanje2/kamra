"""Disposable demo data for the S1 screenshot: two hotels, a stay in each,
and two staff users with no Property User Permission."""

import frappe
from frappe.utils import add_days, nowdate
from frappe.utils.password import update_password

frappe.set_user("Administrator")
for name in ("P1", "P2"):
    if not frappe.db.exists("Property", name):
        frappe.get_doc({"doctype": "Property", "property_name": name,
                        "country": "Uganda", "currency": "UGX"}).insert(ignore_permissions=True)
    if not frappe.db.exists("Reservation", {"property": name, "ota_ref": "LAB-" + name}):
        guest = frappe.get_doc({"doctype": "Guest", "first_name": "Lab " + name}).insert(
            ignore_permissions=True).name
        stay = frappe.get_doc({"doctype": "Reservation", "property": name, "guest": guest,
                               "room_type": "Lab", "status": "Confirmed", "ota_ref": "LAB-" + name,
                               "check_in_date": nowdate(), "check_out_date": add_days(nowdate(), 1)})
        stay.set_new_name()
        stay.db_insert()

for email, role in (("unscoped@example.test", "Front Desk"), ("agent-only@example.test", "Kamra Agent")):
    if not frappe.db.exists("User", email):
        frappe.get_doc({"doctype": "User", "email": email, "first_name": role,
                        "user_type": "System User", "send_welcome_email": 0,
                        "roles": [{"role": role}]}).insert(ignore_permissions=True)
    update_password(email, "KamraDemo1!")
    frappe.clear_cache(user=email)
frappe.db.commit()
print(frappe.get_all("Reservation", filters={"ota_ref": ["like", "LAB-%"]}, fields=["name", "property"]))
