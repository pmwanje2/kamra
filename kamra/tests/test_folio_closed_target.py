"""New room-night lines never land on an invoiced folio.

A guest folio closed before every night was posted (an empty Sh0 invoice)
is frozen. The nights still to post open, or use, an open Guest folio, the
rule post_rate_lines already applies to rate lines. The board's check-out
projection, polled every 30 seconds, must not write an Error Log on every
poll, and it says when it could not project a stay.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from kamra import api
from kamra import folio as folio_mod
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	guest_folio,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)


class FolioClosedTargetTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger",
		        "kamra.api", "kamra.checkout")
		make_property(PROP_A, business_date=TODAY)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.room = make_room(PROP_A, self.rt, "101")

	def closed_empty_stay(self):
		"""Two nights in house, due out today, the guest folio closed as an
		empty Sh0 invoice before either night was posted."""
		res = make_reservation(PROP_A, self.rt, d(-2), TODAY,
		                       status="Checked In", room=self.room)
		# Error Log is MyISAM: rows outlive the test rollback while the
		# naming series rolls back, so an earlier run left logs under this name
		frappe.db.delete("Error Log", {"reference_doctype": "Reservation",
		                               "reference_name": res.name})
		closed = guest_folio(res.name)
		self.assertFalse(frappe.get_doc("Folio", closed).charges)
		folio_mod.close_folio(closed)
		self.assertEqual(frappe.db.get_value("Folio", closed, "status"), "Closed")
		return res, closed

	def error_logs(self) -> int:
		return frappe.db.count("Error Log")

	def test_checkout_context_has_no_nights_not_posted(self):
		res, _closed = self.closed_empty_stay()
		ctx = api.checkout_context(res.name)
		codes = [b["code"] for b in ctx["blockers"]]
		self.assertNotIn("nights_not_posted", codes)

	def test_nights_land_on_an_open_guest_folio(self):
		res, closed = self.closed_empty_stay()
		self.assertEqual(folio_mod.post_remaining_nights(res), 2)
		self.assertFalse(frappe.get_doc("Folio", closed).charges,
		                 "the invoiced folio must stay frozen")
		rooms = frappe.get_all(
			"Folio Charge", filters={"reservation": res.name, "charge_type": "Room"},
			fields=["parent", "posting_date"])
		self.assertEqual(sorted(str(r.posting_date) for r in rooms), [d(-2), d(-1)])
		for r in rooms:
			self.assertNotEqual(r.parent, closed)
			self.assertEqual(
				frappe.db.get_value("Folio", r.parent, ["status", "folio_type"]),
				("Open", "Guest"))
		# posting again finds the nights already posted
		self.assertEqual(folio_mod.post_remaining_nights(res), 0)

	def test_rate_lines_on_an_invoiced_night_go_to_the_open_folio(self):
		res, closed = self.closed_empty_stay()
		folio_mod.post_rate_lines(res, [{
			"folio": closed, "night": d(-2), "description": "UT rate change",
			"amount": 50, "gst_rate": 0}])
		parents = frappe.get_all(
			"Folio Charge", filters={"reservation": res.name,
			                         "charge_type": folio_mod.RATE_ADJUSTMENT},
			pluck="parent")
		self.assertEqual(len(parents), 1)
		self.assertNotEqual(parents[0], closed)
		self.assertEqual(frappe.db.get_value("Folio", parents[0], "status"), "Open")

	def test_board_projection_counts_the_nights_without_logging(self):
		res, _closed = self.closed_empty_stay()
		before = self.error_logs()
		for _ in range(3):
			money = api._stay_balances([res.name])[res.name]
		self.assertLessEqual(self.error_logs() - before, 1)
		self.assertFalse(money.get("projection_unavailable"))
		self.assertGreater(money["due"], 0, "the two unposted nights are owed")
		# a read: nothing stayed on any folio
		self.assertFalse(frappe.get_all(
			"Folio Charge", filters={"reservation": res.name, "charge_type": "Room"}))

	def test_failed_projection_logs_once_per_stay_and_business_date(self):
		res, _closed = self.closed_empty_stay()
		before = self.error_logs()
		with patch("kamra.folio.post_remaining_nights",
		           side_effect=frappe.ValidationError("UT projection failure")):
			for _ in range(3):
				money = api._stay_balances([res.name])[res.name]
		self.assertEqual(self.error_logs() - before, 1)
		self.assertTrue(money.get("projection_unavailable"))
		# the next business date may log again
		frappe.db.set_value("Property", PROP_A, "business_date", d(1))
		frappe.clear_document_cache("Property", PROP_A)
		with patch("kamra.folio.post_remaining_nights",
		           side_effect=frappe.ValidationError("UT projection failure")):
			api._stay_balances([res.name])
		self.assertEqual(self.error_logs() - before, 2)

	def test_one_failed_stay_does_not_hide_the_others(self):
		res, _closed = self.closed_empty_stay()
		room2 = make_room(PROP_A, self.rt, "102")
		other = make_reservation(PROP_A, self.rt, d(-1), TODAY,
		                         status="Checked In", room=room2)
		real = folio_mod.post_remaining_nights

		def flaky(reservation):
			if reservation.name == res.name:
				raise frappe.ValidationError("UT projection failure")
			return real(reservation)

		with patch("kamra.folio.post_remaining_nights", side_effect=flaky):
			money = api._stay_balances([res.name, other.name])
		self.assertTrue(money[res.name].get("projection_unavailable"))
		self.assertFalse(money[other.name].get("projection_unavailable"))
		self.assertGreater(money[other.name]["due"], 0)
