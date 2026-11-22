"""Property-scoped planned departures and completed checkout movements."""


def departure_state(row, business_date):
	day = str(business_date)
	scheduled = str(row.get("check_out_date") or "")
	if row.get("status") == "Checked Out":
		return "checked_out" if str(row.get("check_out_business_date") or scheduled) == day else "future"
	if row.get("status") == "Checked In" and scheduled:
		if scheduled < day:
			return "overdue"
		if scheduled == day:
			return "due_today"
	return "future"


def completed_on(stay, start, end):
	"""Filter completed movements by posting date, with the legacy schedule fallback."""
	from frappe.query_builder.functions import Coalesce

	return (stay.status == "Checked Out") & Coalesce(
		stay.check_out_business_date, stay.check_out_date
	).between(start, end)


def departure_counts(property, start, end):
	import frappe
	from frappe.query_builder import Case
	from frappe.query_builder.functions import Count

	stay = frappe.qb.DocType("Reservation")
	in_house = stay.status == "Checked In"

	def count(condition):
		return Count(Case().when(condition, 1))

	return (
		frappe.qb.from_(stay)
		.select(
			count(in_house & stay.check_out_date.between(start, end)).as_("departures"),
			count(in_house & (stay.check_out_date < start)).as_("departures_overdue"),
			count(completed_on(stay, start, end)).as_("checked_out"),
		)
		.where((stay.property == property) & stay.status.isin(["Checked In", "Checked Out"]))
		.run(as_dict=True)[0]
	)


def report_dates(property, requested=None):
	from kamra.business_date import get_business_date

	business = get_business_date(property)
	day = str(requested or business)
	return {
		"date": day,
		"report_date": day,
		"business_date": business,
		"basis": "actual" if day < business else "projected",
	}
