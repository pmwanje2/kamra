# Scope gate from delegation-of-authority.v7.json (SiteOps 3ecc141833df802fab69d434da652ee3681a66fe).
# Inject the native script's database, identity and role helpers via the test runner.
def authorize_reset(frappe, eligible_user, user_roles, active_role, reserved_role_name, security_role):
    def permission_scope(account, scope):
        global_scope = []
        user_scope = []
        for perm in frappe.get_all(
            "User Permission", {"user": account, "allow": scope},
            fields=["for_value", "applicable_for", "apply_to_all_doctypes"], limit_page_length=0,
        ):
            value = perm.get("for_value")
            applies_to = perm.get("applicable_for")
            if not value:
                frappe.throw("[SITEOPS-DOA-ROLE] A scoped User Permission has no value.", frappe.PermissionError)
            if frappe.utils.cint(perm.get("apply_to_all_doctypes")) == 1 or not applies_to:
                global_scope.append(value)
            elif applies_to == "User":
                user_scope.append(value)
            else:
                frappe.throw(
                    "[SITEOPS-DOA-ROLE] A document-specific User Permission cannot authorize a Floor PIN reset.",
                    frappe.PermissionError,
                )
        return user_scope or global_scope

    req = frappe.form_dict or {}
    caller_row = eligible_user(str(frappe.session.user or "").strip(), allow_administrator=True)
    caller = caller_row.get("name")
    roles = user_roles(caller_row)
    target = eligible_user(str(req.get("user") or "").strip().lower(), lock=True).get("name")
    if caller.lower() == target.lower():
        frappe.throw("[SITEOPS-DOA-SELF] A different user must reset this Floor PIN.", frappe.PermissionError)
    company = str(req.get("company") or "").strip()
    property_name = str(req.get("property") or "").strip()
    branch_name = str(req.get("branch") or "").strip()
    caller_companies = permission_scope(caller, "Company")
    target_companies = permission_scope(target, "Company")
    scope_error = "[SITEOPS-DOA-ROLE] Reset requires an active authorized role and matching company, property and branch scope."
    for allowed in (caller_companies, target_companies):
        if allowed and company not in allowed:
            frappe.throw(scope_error, frappe.PermissionError)
    for account in (caller, target):
        for scope in ("Property", "Branch"):
            allowed = permission_scope(account, scope)
            requested = property_name if scope == "Property" else branch_name
            if allowed and requested not in allowed:
                frappe.throw(scope_error, frappe.PermissionError)
    if company and property_name and frappe.db.exists("DocType", "Property"):
        meta = frappe.get_meta("Property")
        if meta and meta.has_field("company"):
            property_company = frappe.db.get_value("Property", property_name, "company")
            if property_company and property_company != company:
                frappe.throw(scope_error, frappe.PermissionError)

    if caller != "Administrator" and not ("system manager" in roles and active_role("System Manager")):
        if company not in caller_companies or company not in target_companies:
            frappe.throw(scope_error, frappe.PermissionError)
        governed_roles = ""
        if frappe.db.exists("Approval Limit", company):
            governed_roles = frappe.db.get_value("Approval Limit", company, "custom_floor_override_approver_roles")
        authorized = False
        for piece in str(governed_roles or "").replace("\n", ",").split(","):
            role = piece.strip()
            if role.lower() in roles and not reserved_role_name(role) and active_role(role) and not security_role(role):
                authorized = True
                break
        if not authorized:
            frappe.throw(scope_error, frappe.PermissionError)
