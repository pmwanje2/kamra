import frappe
from unittest.mock import patch

from kamra import api, cashier
from kamra.tests.audit_fixtures import PROP_A, TODAY, AuditTestCase, d, make_property, make_room_type, make_room, make_reservation, guest_folio


class CorrectionTests(AuditTestCase):
	def setUp(self):
		super().setUp()
		make_property(PROP_A, business_date=TODAY, require_cashier_pin=0)
		rt = make_room_type(PROP_A)
		self.res = make_reservation(PROP_A, rt, TODAY, d(2), status='Checked In', room=make_room(PROP_A, rt, '101'))
		self.folio = guest_folio(self.res.name)
		self.session = cashier.open_session(PROP_A, 0)['name']
		self.row = api.add_folio_payment(self.folio, 'Cash', 100)['payments'][-1]['name']
		self.assertTrue(callable(getattr(api, 'correct_folio_payment', None)), 'Missing governed correction action')

	def options(self, row=None):
		return api.payment_correction_options(self.folio, row or self.row)

	def submit(self, action, code, key, **kw):
		return api.correct_folio_payment(self.folio, self.row, action_type=action, reason_code=code,
		                                 reason_note='Correction at counter', idempotency_key=key, **kw)

	def test_reclassify_replays_once_and_keeps_drawer_and_balance(self):
		self.assertTrue(self.options()['can_reclassify'])
		before = frappe.db.get_value('Folio', self.folio, 'balance')
		for _ in range(2):
			self.submit('reclassify_tender', 'REV-ACCOUNT', 'one', destination_mode='Card')
		doc = frappe.get_doc('Folio', self.folio)
		self.assertEqual(doc.balance, before)
		self.assertEqual([(r.mode, r.amount) for r in doc.payments], [('Cash', 100), ('Cash', -100), ('Card', 100)])
		self.assertEqual(doc.payments[1].refund_of, self.row)
		self.assertEqual(doc.payments[2].repost_of, self.row)
		self.assertNotEqual(doc.payments[1].idempotency_key, doc.payments[2].idempotency_key)
		self.assertFalse(self.options()['can_reverse'])
		self.assertFalse(self.options(doc.payments[1].name)['can_reclassify'])
		self.assertEqual(sum(frappe.get_all('Cashier Transaction', {'session': self.session, 'mode':'Cash'}, pluck='amount')), 0)

	def test_refund_cap_and_named_approval(self):
		args = dict(refund_amount=25, supervisor_user='Administrator', supervisor_pin='1234')
		with self.assertRaisesRegex(frappe.PermissionError, 'SUPERVISOR_SELF_APPROVAL'):
			self.submit('partial_refund', 'REF-OVER', 'two', **args)
		user = 'k2-supervisor@example.test'
		frappe.get_doc(dict(doctype='User', email=user, first_name='Test', send_welcome_email=0, roles=[dict(role='Finance')])).insert()
		frappe.get_doc(dict(doctype='Cashier PIN', user=user, pin='8394')).insert()
		args.update(supervisor_user=user)
		with self.assertRaises(frappe.ValidationError):
			self.submit('partial_refund', 'REF-OVER', 'two', **args)
		args.update(supervisor_pin='8394')
		self.submit('partial_refund', 'REF-OVER', 'two', **args)
		args.update(refund_amount=76)
		with self.assertRaises(frappe.ValidationError):
			self.submit('partial_refund', 'REF-OVER', 'three', **args)
		self.assertEqual(self.options()['residual'], 75)

	def test_closed_audited_and_role_denial(self):
		with patch('frappe.get_roles', return_value=['Housekeeping']):
			with self.assertRaises(frappe.PermissionError):
				self.options()
			with self.assertRaises(frappe.PermissionError):
				self.submit('reverse_payment','REV-ERR','no')
		for dt, name, field, value in [('Property',PROP_A,'business_date',d(1)), ('Folio',self.folio,'status','Closed')]:
			frappe.db.set_value(dt,name,field,value)
			self.assertIn('Credit Note', self.options()['blocked_reason'])
			with self.assertRaises(frappe.ValidationError):
				self.submit('reverse_payment','REV-ERR','no')
