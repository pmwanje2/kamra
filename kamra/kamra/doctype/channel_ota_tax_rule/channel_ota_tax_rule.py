from frappe.model.document import Document

# The rules a new Channex connection starts from (match_on, match_type,
# match_text, counts_as), tried top to bottom, the first match deciding: an
# OTA's own charges - fees, city and tourism taxes, levies, service charges -
# first, so a "Cleaning Fee (incl. VAT)" or a city tax of type "tax" is not
# read as the room tax; then the room tax under the names OTAs give it -
# VAT, GST, the US model's sales, occupancy and room taxes, and Channex's
# own type "tax". A property edits them on its connection.
STANDARD_RULES = (
	("Type or Name", "Contains", "fee", "Ignored"),
	("Type or Name", "Contains", "fees", "Ignored"),
	("Type or Name", "Contains", "city", "Ignored"),
	("Type or Name", "Contains", "municipal", "Ignored"),
	("Type or Name", "Contains", "tourism", "Ignored"),
	("Type or Name", "Contains", "resort", "Ignored"),
	("Type or Name", "Contains", "cleaning", "Ignored"),
	("Type or Name", "Contains", "levy", "Ignored"),
	("Type or Name", "Contains", "service charge", "Ignored"),
	("Type or Name", "Contains", "vat", "Room tax"),
	("Type or Name", "Contains", "value added tax", "Room tax"),
	("Type or Name", "Contains", "gst", "Room tax"),
	("Type or Name", "Contains", "goods and services tax", "Room tax"),
	("Type or Name", "Contains", "sales tax", "Room tax"),
	("Type or Name", "Contains", "occupancy tax", "Room tax"),
	("Type or Name", "Contains", "room tax", "Room tax"),
	("Type", "Is", "tax", "Room tax"),
)


def standard_rules() -> list[dict]:
	return [{"match_on": on, "match_type": how, "match_text": text, "counts_as": counts}
	        for on, how, text, counts in STANDARD_RULES]


class ChannelOTATaxRule(Document):
	pass
