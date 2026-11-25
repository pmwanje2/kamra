"""R9 — billing segments, historical cutoffs, receipt of legacy PINs, and translations."""
import csv
import inspect
import json
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.app import make_form_dict
from frappe.utils.logger import sanitized_dict
from werkzeug.test import Client
from werkzeug.wrappers import Request, Response

from kamra import api, authz, business_date, folio, housekeeping, pricing
from kamra.tests.audit_fixtures import (
	AuditTestCase, PROP_A, TODAY, d, guest_folio, make_property, make_reservation, make_room, make_room_type,
)
from kamra.tests.test_reservation_discounts import set_setting


class TestR9Packages(AuditTestCase):
	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.api", "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY, gst_mode="Fixed", rates_include_tax=0)
		set_setting(PROP_A, "rate_override_limit_pct", 25)
		a = make_room_type(PROP_A, "R9A", base_price=10000, tax_percent=5)
		self.b = make_room_type(PROP_A, "R9B", base_price=15000, tax_percent=18)
		self.target = make_room(PROP_A, self.b, "201")
		self.stay = make_reservation(PROP_A, a, TODAY, d(2), status="Checked In",
		                             room=make_room(PROP_A, a, "101"), adults=1, auto_price=1)

	def move_with_package(self, end, *, price=40000, nights=2):
		frappe.get_doc({
			"doctype": "Holiday Package", "property": PROP_A, "package_name": "R9 holiday",
			"room_type": self.b, "start_date": TODAY, "end_date": end,
			"package_nights": nights, "price_2_pax": price, "extra_night_price": 25000,
			"elements": [{"element_name": "Taxi", "element_type": "Transport", "transaction_code": "3200",
			              "charge_type": "Taxi", "allowance_basis": "Per Room Night", "unit_allowance": 500,
			              "posting_rhythm": "Every Night", "included_in_rate": 0}],
		}).insert(ignore_permissions=True)
		business_date.advance_business_date(PROP_A, TODAY)
		preview = api.room_move_preview(self.stay.name, self.target, upgrade_type="Paid")
		saved = api.move_reservation(self.stay.name, self.target, upgrade_type="Paid", confirm_price=1)
		self.stay.reload()
		return preview["new_amount"], saved["amount_after_tax"]

	def test_package_expired_before_destination_segment(self):
		prices = self.move_with_package(TODAY)
		for day in (TODAY, d(1)):
			folio.post_room_night(self.stay, day)
		bill = frappe.get_doc("Folio", guest_folio(self.stay.name))
		self.assertEqual((*prices, bill.grand_total), (28200, 28200, 28200))
		self.assertEqual([(r.charge_type, r.total) for r in bill.charges], [("Room", 10500), ("Room", 17700)])

	def test_elapsed_room_does_not_consume_first_package_night(self):
		prices = self.move_with_package(d(1), price=20000, nights=1)
		self.assertEqual(prices, (34100, 34100))
		self.assertEqual(folio.locked_night(self.stay, d(1)).rate, 20000)

	def test_locked_package_rejects_stay_wide_discount_before_writing(self):
		self.assertEqual(self.move_with_package(d(1), price=30000), (28200, 28200))
		terms = dict(discount_type="Percent", discount_value=10, reason_code="RATE-PROMO")
		for func, kwargs in ((api.discount_preview, {}), (api.set_reservation_discount, {"confirm_price": 1})):
			with self.subTest(method=func.__name__), self.assertRaisesRegex(frappe.ValidationError, "holiday package"):
				func(self.stay.name, **terms, **kwargs)
		self.assertEqual(self.stay.reload().amount_after_tax, 28200)
		self.assertFalse(self.stay.discount_value)


class TestR9History(AuditTestCase):
	def history(self, start, days, boundaries):
		stay = frappe._dict(name="R9 history", check_in_date=start, room_type="C", room_move_rate_cutoff=boundaries[-1][1],
		                    night_rates=[frappe._dict(stay_date=day, rate=100, gst_rate=5) for day in days])
		versions = [frappe._dict(creation=created, data=json.dumps({"changed": [
			["room_move_rate_cutoff", old, new], ["room_type", source, target]]}))
		            for old, new, created, source, target in boundaries]
		with patch.object(frappe, "get_all", return_value=versions):
			return pricing.nightly_billing_room_types(stay)

	def test_dotted_effective_date_beats_version_creation(self):
		for date in ("25.11.2026", "2026.11.25"):
			with self.subTest(date=date):
				types = self.history("2026-11-23", ["2026-11-24", "2026-11-25"], [
					(None, date, "2026-11-26", "A", "B"), (date, "2026-11-28", "2026-11-28", "B", "C")])
				self.assertEqual(types, {"2026-11-24": "A", "2026-11-25": "B"})

	def test_adjacent_cutoff_resolves_ambiguous_day_and_month(self):
		types = self.history("2026-05-01", ["2026-05-05", "2026-05-06"], [
			(None, "05-06-2026", "2026-05-07", "A", "B"),
			("2026-05-06", "2026-06-28", "2026-06-29", "B", "C")])
		self.assertEqual(types, {"2026-05-05": "A", "2026-05-06": "B"})

	def test_intermediate_cutoff_bounds_an_earlier_ambiguous_one(self):
		types = self.history("2026-05-01", ["2026-05-05", "2026-05-06", "2026-05-25"], [
			(None, "05-06-2026", "2026-05-07", "A", "B"),
			("05-06-2026", "2026-05-25", "2026-05-26", "B", "C"),
			("2026-05-25", "2026-06-28", "2026-06-29", "C", "D")])
		self.assertEqual(types, {"2026-05-05": "A", "2026-05-06": "B", "2026-05-25": "C"})

	def test_ambiguous_history_refuses_to_reprice_uncertain_night(self):
		with self.assertRaisesRegex(frappe.ValidationError, "(?i)ambiguous"):
			self.history("2026-05-01", ["2026-05-20"], [
				(None, "05-06-2026", "2026-05-07", "A", "B"),
				("05-06-2026", "2026-06-28", "2026-06-29", "B", "C")])


class TestR9Compatibility(AuditTestCase):
	def test_legacy_post_reaches_verifier_only_through_redacted_form(self):
		from importlib.util import find_spec

		def verify_boundary(request):
			with patch.object(frappe.local, "request", request, create=True), patch.object(frappe.local, "form_dict", frappe._dict()):
				make_form_dict(request)
				safe_metadata = sanitized_dict(frappe.local.form_dict)
				authz.prepare_pin_request()
				method, staged = frappe.local.kamra_pin_arguments
				self.assertEqual(method, "kamra.api.move_reservation")
				self.assertNotIn("806142", repr(safe_metadata))
				self.assertNotIn("806142", repr(staged))
				self.assertEqual(staged["pin_secret"].reveal(), expected)
				return Response(status=204)

		from frappe import app
		probe = Request.application(verify_boundary)
		with patch.object(app, "application", probe):
			if find_spec("kamra.wsgi"):
				from kamra.wsgi import application
			else:
				application = probe
			client = Client(application)
			path = "/api/method/kamra.api.move_reservation"
			for fmt in ("json", "data"):
				for canonical in (False, True):
					with self.subTest(fmt=fmt, canonical=canonical):
						body = {"reservation": "R9", "supervisor_pin": "806142"}
						expected = "842501" if canonical else "806142"
						if canonical:
							body["pin_secret"] = expected
						self.assertEqual(client.post(path, **{fmt: body}).status_code, 204)

	def test_assignment_annotations_cover_both_wire_arguments(self):
		params = inspect.signature(housekeeping.auto_assign_housekeeping_tasks).parameters
		for name in ("attendants", "max_credits_per_attendant"):
			self.assertNotEqual(params[name].annotation, inspect.Parameter.empty)

	def test_bundle_translations(self):
		root = Path(frappe.get_app_path("kamra")).parent
		i18n = root / "frontend/src/i18n"
		with (i18n / "catalog.csv").open(encoding="utf-8-sig", newline="") as f:
			catalog = {r["key"]: r for r in csv.DictReader(f)}
		for lang, column in (("ar", "arabic"), ("es", "spanish"), ("fr", "french")):
			bundle = json.loads((i18n / "locales" / (lang + ".json")).read_text())
			with (root / "kamra/translations" / (lang + ".csv")).open(newline="") as f:
				backend = {r[0]: r[1] for r in csv.reader(f)}
			with self.subTest(lang=lang, catalog="frontend"):
				self.assertEqual([k for k in FRONTEND if not catalog[k][column] or bundle.get(k) != catalog[k][column]], [])
			with self.subTest(lang=lang, catalog="backend"):
				self.assertEqual([k for k in BACKEND if not backend.get(k)], [])


FRONTEND = (
	'All Rooms',
	'Attendants',
	'Auto-Assign Queue',
	'Auto-assign tasks',
	'Booked room type: {type}',
	'Both reservations keep their booked rates.',
	'Complete the fee waiver details.',
	'Credit limit per attendant',
	'DND',
	'Deep clean credits',
	'Departure credits',
	'Discrepancies ({count})',
	'Early Check-In Fee',
	'Early Check-In Fee Policy',
	'Early Check-In Fee Value',
	'Early Check-In Window Start',
	'Early check-in fee: {amount}',
	'Front desk: {status}',
	'Housekeeping section',
	'Housekeeping views',
	'Not Required',
	'Preview assignments',
	'Reported occupancy',
	'Require Complimentary Upgrade Approval',
	'Require Early Check-In Waiver Approval',
	'Resolution note',
	'Resolve discrepancy',
	'Room type to charge: {type}',
	'Select status',
	'Skip (occupancy)',
	'Sleep (occupancy)',
	'Stayover credits',
	'Swap with occupied room',
	'Swap with room {room}',
	'Turndown',
	'Turndown credits',
	'Waive early check-in fee',
	'Waiver reason',
	'{used} / {limit} credits',
)


BACKEND = (
	'A paid room move needs a server-priced reservation.',
	'A room swap keeps both booked rates.',
	'Assignment Credits',
	'Assignment Credits Recorded',
	'Both rooms must be marked Clean, Inspected or Ready.',
	'Choose Vacant or Occupied.',
	'Choose a vacant room marked Clean, Inspected or Ready.',
	'Choose a valid turndown status.',
	'Choose a valid upgrade type.',
	'Choose attendants with access to this property.',
	'Choose two checked-in rooms of the same type at this property.',
	'Deep Clean Credits',
	'Departure Credits',
	'Early Check-In Fee Policy',
	'Early Check-In Fee Value',
	'Early Check-In Waiver Reason',
	'Early Check-In Window Start',
	'Enter a discrepancy resolution note.',
	'Enter a positive task credit limit.',
	'Enter a reason for the complimentary upgrade.',
	'Housekeeping Occupancy',
	'Housekeeping Occupancy Reported At',
	'Housekeeping Section',
	'Housekeeping discrepancy resolved for room {0}',
	'Nightly Room Types to Charge',
	'Original Room Type',
	'Require Complimentary Upgrade Approval',
	'Require Early Check-In Waiver Approval',
	'Resolved Front Desk Occupancy',
	'Room Move Rate Cutoff',
	'Room Type to Charge',
	'Room not found.',
	'Stayover Credits',
	'The destination room is outside this property.',
	'The destination room type has no rate in this contract.',
	'The early check-in percentage must be between 0 and 100.',
	'The early check-in window must start before standard check-in.',
	'The task reservation must belong to this property.',
	'The task room must belong to this property.',
	'This room has no open discrepancy.',
	'Turndown Business Date',
	'Turndown Credits',
	'Turndown Requested',
	'Turndown Status',
	'Turndown is unavailable for rooms out of order or out of service.',
	'Turndown is unavailable for this room on the current business date.',
	'Turndown requires a checked-in stay.',
	'Upgrade Approved By',
	'Upgrade Reason',
	'Upgrade Type',
	"Use Move Room to change a checked-in reservation's room.",
	'Use guided check-in when an early check-in fee applies.',
)
