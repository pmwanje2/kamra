"""Booking and group lifecycles across a property's switch to contract tariffs."""
import frappe
from frappe.utils import add_days, nowdate

from kamra import api, folio, pricing, public_api, rates
from kamra.tests.audit_fixtures import AuditTestCase, make_property, make_room, make_room_type
from kamra.tests.test_release2_composed import _ensure_custom_field


class GovernedBookingLifecycleTest(AuditTestCase):
	properties = ("TEST Governed Lifecycle",)

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.get_meta("Rate Plan").has_field("custom_tariff_price_list"):
			_ensure_custom_field("Rate Plan", "custom_tariff_price_list", "Data")

	def setUp(self):
		super().setUp()
		self.prop = make_property(self.properties[0], booking_engine_enabled=1)
		self.rt = make_room_type(self.prop)
		self.room = make_room(self.prop, self.rt, "201")
		self.today = nowdate()
		self.end = str(add_days(self.today, 1))

	def plan(self, code, tariff=True):
		rp = frappe.get_doc({"doctype": "Rate Plan", "property": self.prop, "code": code,
		                     "rate_plan_name": code, "modifier_type": "Percent", "modifier_value": 0}).insert()
		if tariff:
			frappe.db.set_value("Rate Plan", rp.name, "custom_tariff_price_list", "Approved contract")
		return rp.name

	def test_sole_active_plan_is_implicit_default_but_two_are_ambiguous(self):
		only = self.plan("ONLY")
		args = self.prop, self.rt, self.today, self.end
		self.assertTrue(rates.effective_rate_plan_required(self.prop))
		self.assertEqual(pricing.resolve_booking_rate_plan(*args, check_availability=False), only)
		self.assertEqual(pricing.resolve_group_rate_plan(self.prop), only)
		self.assertEqual(public_api._public_default_rate_plan(self.prop)["name"], only)
		self.assertEqual(api.booking_options(self.prop)["default_plan"], only)
		self.plan("SECOND")
		with self.assertRaisesRegex(frappe.ValidationError, "default rate plan"):
			pricing.resolve_booking_rate_plan(*args, check_availability=False)
		self.assertIsNone(public_api._public_default_rate_plan(self.prop))

	def test_explicit_and_company_plans_precede_travel_agent(self):
		plan = self.plan("AGENT", tariff=False)
		other = self.plan("CORP", tariff=False)
		self.assertTrue(frappe.get_meta("Travel Agent").has_field("negotiated_rate_plan"))
		agent = frappe.get_doc({"doctype": "Travel Agent", "agent_name": "Lifecycle partner",
		                        "negotiated_rate_plan": plan}).insert().name
		company = frappe.get_doc({"doctype": "Corporate Account", "company_name": "Lifecycle company",
		                          "negotiated_rate_plan": other}).insert().name
		args = self.prop, self.rt, self.today, self.end
		self.assertEqual(api.get_quote(*args, travel_agent=agent)["rate_plan"], plan)
		self.assertEqual(api.get_quote(*args, company=company, travel_agent=agent)["rate_plan"], other)
		self.assertEqual(api.get_quote(*args, rate_plan=plan, company=company, travel_agent=agent)["rate_plan"], plan)
		booked = api.create_booking(*args, "Agent guest", travel_agent=agent)
		self.assertEqual(frappe.db.get_value("Reservation", booked["reservation"], "rate_plan"), plan)

	def test_old_block_still_opens_when_current_tariff_cannot_be_quoted(self):
		blocks = [{"room_type": self.rt, "rooms_blocked": 1}]
		group = frappe.get_doc({"doctype": "Group Booking", "property": self.prop, "group_name": "Legacy",
		                        "check_in_date": self.today, "check_out_date": self.end,
		                        "status": "Confirmed", "blocks": blocks}).insert()
		booked = api.create_booking(self.prop, self.rt, self.today, self.end, "Group guest", group_booking=group.name)
		self.plan("FIRST")
		self.plan("SECOND")
		with self.assertRaises(frappe.ValidationError):
			api.group_price_preview(self.prop, self.today, self.end, blocks)
		detail = api.group_detail(group.name)
		self.assertFalse(detail["quote_available"])
		self.assertTrue(detail["quote_refusal"])
		self.assertIsNone(detail["money"]["block_value"])
		self.assertEqual(detail["rooming_list"][0]["name"], booked["reservation"])

	def test_amend_and_check_out_a_pre_governance_stay(self):
		booked = api.create_booking(self.prop, self.rt, self.today, self.end, "Legacy guest")
		stay = frappe.get_doc("Reservation", booked["reservation"])
		original = float(stay.night_rates[0].rate)
		self.plan("FIRST")
		self.plan("SECOND")
		self.assertTrue(rates.effective_rate_plan_required(self.prop))
		end = str(add_days(self.end, 1))
		api.amend_stay(stay.name, self.today, end, confirm_price=1)
		stay.reload()
		self.assertFalse(stay.rate_plan)
		self.assertEqual([float(n.rate) for n in stay.night_rates], [original, original])
		api.check_in(stay.name, room=self.room)
		folio.post_remaining_nights(stay.reload())
		bill = frappe.get_doc("Folio", {"reservation": stay.name, "folio_type": "Guest"})
		api.add_folio_payment(bill.name, "Cash", bill.grand_total)
		api.check_out(stay.name)
		stay.reload()
		self.assertEqual(stay.status, "Checked Out")
		self.assertFalse(stay.rate_plan)
		self.assertEqual(float(bill.reload().balance), 0)
