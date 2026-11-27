"""Guest privacy by design (India DPDP Act 2023 and Rules 2025; GDPR-friendly).

The hotel is the Data Fiduciary for its guests. Kamra's job is to make the
right thing the default and the lawful thing one click:

  erase_guest        right to erasure (s.12): every identifying trace goes,
                     including photos, signatures, message logs and change
                     history; bills and stays stay for the tax books.
  export_guest_data  right to access (s.11): a summary of what is held.
  apply_retention    storage limitation (s.8(7)): guests with no stay for
                     Property.guest_retention_months are erased by the daily
                     scheduler. Guests on the blacklist are kept (security is
                     the stated purpose) until                      the flag is lifted.
  apply_message_retention
                     storage limitation for conversations: WhatsApp message
                     text and AI assistant chats older than
                     Property.message_retention_days are cleared nightly;
                     who, when and delivery status stay for audit.

What is deliberately kept after erasure: amounts, dates, rooms and invoice
numbers (GST law needs them), an ID masked to its last 4 digits in the stay
register, and one audit line saying the erasure happened (without the name).
"""


import re

import frappe
from frappe.utils import add_days, add_months, cint, getdate, now_datetime, nowdate

from kamra.authz import require_roles, restricted_properties


def _mask(value: str | None) -> str | None:
	if not value or len(value) <= 4 or value.startswith("•"):
		return value
	return "•" * (len(value) - 4) + value[-4:]


def _delete_files(doctype: str, name: str, field: str | None = None, url: str | None = None) -> int:
	filters = {"attached_to_doctype": doctype, "attached_to_name": name}
	if field:
		filters["attached_to_field"] = field
	if url:
		filters["file_url"] = url
	gone = 0
	for f in frappe.get_all("File", filters=filters, pluck="name"):
		# delete_doc handles shared content hashes; delete_permanently leaves
		# no Deleted Document row holding the file name
		frappe.delete_doc("File", f, force=True, ignore_permissions=True, delete_permanently=True)
		gone += 1
	return gone


def _scrub_text(doctype: str, fields: tuple, secrets: list[str], alias: str, where: str = "", values=()):
	"""Replace each identifying string with the alias in free-text columns."""
	for field in fields:
		for s in secrets:
			frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- doctype/field are constants from this module; values parameterized
				f"UPDATE `tab{doctype}` SET `{field}` = REPLACE(`{field}`, %s, %s) "
				f"WHERE `{field}` LIKE %s {where}",
				(s, alias, f"%{s}%", *values))


# Primary guest's stay-specific submission. Access tokens and companions'
# records are not part of this export. Values are the post-erasure defaults.
_STAY_REGISTRATION = {
	"booking_contact_email": None, "booking_marketing_opt_in": 0,
	"precheckin_registration": None, "eta": None, "special_requests": None,
	"precheckin_billing_address": None, "precheckin_signature": None,
	"precheckin_status": "Not Started", "precheckin_consent": 0, "precheckin_on": None,
	"precheckin_consent_ip": None, "precheckin_terms_version": None,
	"precheckin_address_file": None, "id_document": None,
	"id_document_source": None, "id_document_on": None,
}


def _erase_group_contacts(stays, profile_values, guest, alias):
	"""Legacy carts copied a guest name into the title. Desk groups can share
	the same guest links, so keep organization titles and unrelated contacts."""
	groups = {stay.group_booking for stay in stays if stay.group_booking}
	if not groups:
		return
	from kamra.public_api import precheckin_registration

	values = {v.strip() for v in profile_values if v and v.strip()}
	names = {s.guest_name.strip() for s in stays if s.guest_name}
	values.update(names)
	phones = {guest.phone.strip()} if guest.phone else set()
	emails = {guest.email.strip().casefold()} if guest.email else set()
	ids = {guest.id_number.strip()} if guest.id_number else set()
	# Titles historically contained booker contact details. Additional personal
	# fields belong to the notes matcher only.
	exact = {v.casefold() for v in values}
	names.update(v.strip() for v in (guest.full_name, guest.first_name, guest.last_name) if v)
	values.update(names)
	for field in ("address_line", "city", "nationality", "date_of_birth",
	              "gender", "id_place_of_issue", "id_issue_date", "id_expiry_date", "guest_notes", "blacklist_reason",
	              "id_file", "address_proof_file", "photo"):
		if value := guest.get(field):
			values.add(str(value).strip())
	for stay in stays:
		registration = precheckin_registration(stay)
		exact.update(v.strip().casefold() for v in (registration.get("address_line"), stay.precheckin_billing_address)
		             if v and v.strip())
		if email := registration.get("email"):
			emails.add(email.strip().casefold())
		if number := registration.get("id_number"):
			ids.add(number.strip())
		values.update(v.strip() for v in (registration.get("address_line"), registration.get("city"),
		                                  registration.get("nationality"), stay.precheckin_billing_address,
		                                  stay.special_requests, stay.precheckin_consent_ip,
		                                  stay.precheckin_signature, stay.precheckin_address_file,
		                                  stay.id_document) if v and v.strip())
		# A third-party booker can have their own contact on the reservation.
		phone = (stay.booked_by_phone or "").strip()
		if phone and (not stay.booked_by_name or stay.booked_by_name == stay.guest_name):
			phones.add(phone)
	if not any((values, phones, emails, ids)):
		return
	exact.update(emails)
	exact.update(p.casefold() for p in phones)
	# Consume a whole email before looking for names or identifiers inside it.
	# A domain label after a dot is part of the address; a sentence-ending dot is prose.
	email_token = r"(?P<email>[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)*)"
	patterns = [email_token]
	guarded = {v.casefold() for v in names | ids}
	for value in sorted((values | ids) - phones - {""}, key=len, reverse=True):
		if value.casefold() in emails:
			continue
		pattern = re.escape(value)
		if value.casefold() in guarded:
			# The surrounding word boundaries already cover underscores.
			pattern = r"(?<!\w[./-])" + pattern + r"(?![./-]\w)"
		patterns.append(pattern)
	for phone in phones:
		digits = re.sub(r"\D", "", phone)
		if 7 <= len(digits) <= 15 and re.fullmatch(r"[+0-9().\s-]+", phone):
			# Match the complete number across display separators, never a piece
			# of a longer number or a hyphenated reference such as INV-123…-09.
			number = r"[().\s-]*".join(digits)
			patterns.append(r"(?<!\w[.-])(?<!\d[\s()])[+(]?" + number + r"(?![.-]\w|[\s()]\d)")
		else:
			patterns.append(re.escape(phone))
	contacts = re.compile(r"(?<!\w)(?:" + "|".join(patterns) + r")(?!\w)", re.IGNORECASE)

	def replacement(match):
		email = match["email"]
		if not email or email.casefold() in emails:
			return alias
		# Apostrophes and backticks can be mailbox characters or prose delimiters.
		# Remove only a known mailbox between paired delimiters, retaining the quotes.
		quote = email[0]
		if quote in ("'", "`") and email[1:].casefold() in emails:
			if re.search(re.escape(quote) + r"(?!\w)", match.string[match.end() :]):
				return quote + alias
		return email

	for name in sorted(groups):
		group = frappe.db.get_value("Group Booking", name, ["group_name", "notes", "company", "group_type"],
		                            as_dict=True, for_update=True)
		if not group:
			continue

		def scrub(field, value):
			if (field == "group_name" and not group.company and group.group_type not in ("Corporate", "Tour")
			        and value and value.strip().casefold() in exact):
				return name
			if field == "notes" and value:
				return contacts.sub(replacement, value)
			return value

		changes = {f: clean for f, v in group.items() if (clean := scrub(f, v)) != v}
		if changes:
			frappe.db.set_value("Group Booking", name, changes, update_modified=False)
		_scrub_history("Group Booking", name, scrub)


def _scrub_history(doctype, name, scrub):
	"""Redact the same fields in change history, preserving unrelated edits."""
	for version in frappe.get_all("Version", filters={"ref_doctype": doctype, "docname": name},
	                              fields=["name", "data"]):
		data = frappe.parse_json(version.data) or {}
		changed = data.get("changed", [])
		clean = [[field, scrub(field, old), scrub(field, new)] for field, old, new in changed]
		if clean != changed:
			data["changed"] = clean
			frappe.db.set_value("Version", version.name, "data", frappe.as_json(data), update_modified=False)


def erase_guest(guest: str, reason: str = "request") -> dict:
	"""Irreversibly remove what identifies a guest. Financial records remain."""
	g = frappe.get_doc("Guest", guest)
	alias = f"Guest {frappe.generate_hash(length=6).upper()}"
	profile_values = {g.full_name, g.phone, g.email, g.id_number, f"{g.first_name or ''} {g.last_name or ''}".strip()}
	secrets = [s for s in profile_values if s and len(s) >= 4]
	reservations = frappe.get_all("Reservation", filters={"guest": guest}, fields=[
		"name", "guest_name", "group_booking", "precheckin_registration", "precheckin_billing_address",
		"booked_by_name", "booked_by_phone", "special_requests", "precheckin_consent_ip",
		"precheckin_signature", "precheckin_address_file", "id_document",
	])
	_erase_group_contacts(reservations, profile_values, g, alias)
	counts = {"files": 0, "messages": 0, "occupants": 0}

	# 1. the profile itself, photos included
	for field in ("id_file", "address_proof_file", "photo"):
		counts["files"] += _delete_files("Guest", guest, field)
	counts["files"] += _delete_files("Guest", guest)
	blank = {"first_name": alias, "last_name": "", "full_name": alias, "phone": "", "email": "",
	         "id_type": "", "id_number": "", "id_place_of_issue": "", "id_issue_date": None,
	         "id_expiry_date": None, "nationality": "", "address_line": "", "city": "",
	         "id_file": None, "address_proof_file": None, "photo": None, "date_of_birth": None, "gender": "",
	         "guest_notes": "Profile erased.", "vip": 0, "marketing_consent": 0}
	frappe.db.set_value("Guest", guest, {k: v for k, v in blank.items() if g.meta.has_field(k)},
	                    update_modified=False)

	# 2. stays: names on the books become the alias; signatures, booker
	#    contact, occupant photos and phones go; IDs are masked
	for doctype in ("Reservation", "Folio"):
		frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- doctype is a constant from this loop
			f"UPDATE `tab{doctype}` SET guest_name = %s WHERE guest = %s", (alias, guest))
	for stay in reservations:
		r = stay.name
		res = frappe.get_doc("Reservation", r)
		changes = {
			**_STAY_REGISTRATION, "booked_by_phone": "", "id_document_discarded": 1,
			"precheckin_verified_on": None, "precheckin_verified_by": None, "precheckin_link_sent": None,
			"precheckin_token": None, "precheckin_token_expires_at": None,
		}
		counts["files"] += _delete_files("Reservation", r, "id_document")
		counts["files"] += _delete_files("Reservation", r, "precheckin_address_file")
		if res.get("precheckin_address_file"):
			counts["files"] += _delete_files("Reservation", r, url=res.precheckin_address_file)
		if res.get("booked_by_name") and res.booked_by_name in secrets:
			changes["booked_by_name"] = alias
		frappe.db.set_value("Reservation", r, {k: v for k, v in changes.items() if res.meta.has_field(k)},
		                    update_modified=False)
		for o in res.get("occupants") or []:
			oc = {"id_number": _mask(o.id_number), "phone": ""}
			if o.get("id_file"):
				counts["files"] += _delete_files("Reservation", r, url=o.id_file)
				oc["id_file"] = None
			if o.get("photo"):
				counts["files"] += _delete_files("Reservation", r, url=o.photo)
				oc["photo"] = None
			if o.full_name in secrets:
				oc["full_name"] = alias
			frappe.db.set_value("Stay Occupant", o.name, oc, update_modified=False)
			counts["occupants"] += 1
		_scrub_history("Reservation", r, lambda field, value: None if field in _STAY_REGISTRATION else value)
		for dt in ("Laundry Order", "Service Ticket"):
			frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- dt is a constant from this loop
				f"UPDATE `tab{dt}` SET guest_name = %s WHERE reservation = %s", (alias, r))

	# 3. conversations about the guest
	for m in frappe.get_all("WhatsApp Message", filters={"guest": guest}, pluck="name"):
		frappe.db.set_value("WhatsApp Message", m, {"content": "[erased]", "to_number": "",
		                                            "from_number": "", "guest": None},
		                    update_modified=False)
		counts["messages"] += 1
	_scrub_text("Copilot Conversation", ("messages", "title"), secrets, alias)
	_scrub_text("Communication", ("content", "subject", "recipients", "sender", "phone_no"), secrets, alias)

	# 4. audit and change history keep what happened, not who it was
	_scrub_text("Agent Action Log", ("rationale", "before_snapshot", "after_snapshot"), secrets, alias)
	frappe.db.delete("Version", {"ref_doctype": "Guest", "docname": guest})
	_scrub_text("Version", ("data",), secrets, alias,
	            "AND ref_doctype IN ('Reservation', 'Folio')", ())

	from kamra.savings import log_action
	log_action("erase_guest", "Guest", guest,
	           rationale=f"Personal data erased ({reason}); financial records kept for the books")
	return {"guest": guest, "alias": alias, "reservations": len(reservations), **counts}


@frappe.whitelist(methods=["POST"])
@require_roles(scope={"guest": "Guest"})
def export_guest_data(guest: str) -> dict:
	"""Right to access: everything Kamra holds about one guest, readable."""
	g = frappe.get_doc("Guest", guest)
	profile = {f.fieldname: g.get(f.fieldname) for f in g.meta.fields
	           if f.fieldtype not in ("Section Break", "Column Break", "Tab Break") and g.get(f.fieldname)}
	from kamra.public_api import precheckin_registration

	filters = {"guest": guest}
	properties = restricted_properties()
	if properties is not None:
		filters["property"] = ("in", sorted(properties))
	stays = frappe.get_all("Reservation", filters=filters,
	                       fields=["name", "property", "check_in_date", "check_out_date", "status",
	                               "adults", "children", "source", *_STAY_REGISTRATION], order_by="check_in_date desc")
	for stay in stays:
		stay.precheckin_registration = precheckin_registration(stay)
	folios = frappe.get_all("Folio", filters=filters,
	                        fields=["name", "reservation", "status", "grand_total", "balance"])
	messages = frappe.get_all("WhatsApp Message", filters=filters,
	                          fields=["creation", "direction", "status", "template_name"],
	                          order_by="creation desc", limit=200)
	from kamra.savings import log_action
	log_action("export_guest_data", "Guest", guest, rationale="Data access request answered")
	return {"generated_on": str(frappe.utils.now_datetime()), "guest": guest, "profile": profile,
	        "stays": stays, "bills": folios, "messages": messages,
	        "note": "Bills and stay dates are kept as tax records even after erasure."}


def _last_activity(guest: str):
	row = frappe.db.sql("""select max(greatest(coalesce(check_out_date, '1970-01-01'),
		date(modified))) from `tabReservation` where guest=%s""", guest)
	last = row[0][0] if row and row[0][0] else None
	return getdate(last) if last else getdate(frappe.db.get_value("Guest", guest, "modified"))


def apply_retention(limit: int = 500) -> dict:
	"""Daily: erase guests idle past their property's retention period.
	A guest's period is the longest among the properties they stayed at;
	any property with retention off (0) keeps them."""
	months = {p.name: cint(p.guest_retention_months)
	          for p in frappe.get_all("Property", fields=["name", "guest_retention_months"])}
	if not any(months.values()):
		return {"erased": 0}
	erased = 0
	candidates = frappe.db.sql("""select g.name from `tabGuest` g
		where coalesce(g.blacklisted, 0) = 0 and g.full_name not like 'Guest %%'
		and not exists (select 1 from `tabReservation` r where r.guest = g.name
		                and r.status in ('Tentative', 'Pending Payment', 'Held', 'Confirmed', 'Checked In'))
		order by g.modified limit %s""", limit, pluck=True)
	for guest in candidates:
		props = set(frappe.get_all("Reservation", filters={"guest": guest}, pluck="property"))
		keep = [months.get(p, 0) for p in props] or [0]
		if 0 in keep:
			continue
		if _last_activity(guest) < getdate(add_months(nowdate(), -max(keep))):
			erase_guest(guest, reason="retention period ended")
			erased += 1
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- scheduler: one guest's erasure must not roll back another's
	return {"erased": erased}


CLEARED_CHAT_TITLE = "Chat cleared (retention period ended)"


def apply_message_retention() -> dict:
	"""Daily: clear WhatsApp message text and AI assistant chats older than
	their property's message_retention_days. Rows stay, so delivery status,
	direction, template and timestamps remain for audit. A chat with no
	property follows the shortest period set on any property."""
	days = {p.name: cint(p.message_retention_days)
	        for p in frappe.get_all("Property", fields=["name", "message_retention_days"])}
	set_days = [d for d in days.values() if d > 0]
	if not set_days:
		return {"messages": 0, "chats": 0}
	now = now_datetime()
	messages = chats = 0
	for prop, d in days.items():
		if d <= 0:
			continue
		cutoff = add_days(now, -d)
		names = frappe.get_all("WhatsApp Message", pluck="name", filters={
			"property": prop, "creation": ("<", cutoff), "content": ("not in", ("", "[erased]"))})
		for name in names:
			frappe.db.set_value("WhatsApp Message", name, "content", "", update_modified=False)
		messages += len(names)
		chats += _clear_chats({"property": prop, "modified": ("<", cutoff)})
	chats += _clear_chats({"property": ("is", "not set"),
	                       "modified": ("<", add_days(now, -min(set_days)))})
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- scheduler: persist the nightly clean-up
	return {"messages": messages, "chats": chats}


def _clear_chats(filters: dict) -> int:
	names = frappe.get_all("Copilot Conversation", pluck="name",
	                       filters={**filters, "messages": ("!=", "[]")})
	for name in names:
		# modified is the last activity; keep it so the chat is not
		# picked up again as fresh
		frappe.db.set_value("Copilot Conversation", name,
		                    {"messages": "[]", "title": CLEARED_CHAT_TITLE}, update_modified=False)
	return len(names)


# Marketing consent (DPDP s.6): promotional WhatsApp goes only to guests who
# opted in, and withdrawing is as easy as giving it.
CONSENT_CHANNEL = {"Booking Page": "API", "Desk": "Desk", "WhatsApp Keyword": "WhatsApp"}


def set_marketing_consent(guest: str, consent, source: str, property: str | None = None) -> bool:
	"""The only writer of Guest.marketing_consent. Records when and how, and
	logs the change. Returns False when nothing changed."""
	if source not in CONSENT_CHANNEL:
		frappe.throw(f"Unknown consent source {source}.")
	consent = 1 if cint(consent) else 0
	if cint(frappe.db.get_value("Guest", guest, "marketing_consent")) == consent:
		return False
	frappe.db.set_value("Guest", guest, {"marketing_consent": consent,
	                                     "marketing_consent_date": now_datetime(),
	                                     "marketing_consent_source": source})
	from kamra.savings import log_action
	verb = "granted" if consent else "withdrawn"
	log_action(f"marketing_consent_{verb}", "Guest", guest, property=property,
	           channel=CONSENT_CHANNEL[source],
	           rationale=f"Marketing consent {verb} ({source})")
	return True


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent", scope={"guest": "Guest"})
def update_marketing_consent(guest: str, consent) -> dict:
	"""Desk toggle in the guest profile: record or withdraw consent."""
	set_marketing_consent(guest, consent, "Desk")
	return frappe.db.get_value("Guest", guest, ["marketing_consent", "marketing_consent_date",
	                                            "marketing_consent_source"], as_dict=True)
