"""Idempotent backfill for P4 dated traces on existing Stay Instruction rows."""

import frappe


def execute():
	if not frappe.db.table_exists("Stay Instruction"):
		return
	frappe.db.sql(
		"""
		UPDATE `tabStay Instruction`
		SET
			trace_date = COALESCE(trace_date, DATE(creation), CURDATE()),
			trace_status = CASE
				WHEN COALESCE(status, 'Open') = 'Done' THEN 'Done'
				ELSE COALESCE(NULLIF(trace_status, ''), 'Open')
			END,
			resolved_by = CASE
				WHEN COALESCE(status, 'Open') = 'Done' THEN COALESCE(resolved_by, done_by)
				ELSE resolved_by
			END,
			resolved_at = CASE
				WHEN COALESCE(status, 'Open') = 'Done' THEN COALESCE(resolved_at, done_on)
				ELSE resolved_at
			END
		WHERE trace_date IS NULL
		   OR trace_status IS NULL
		   OR trace_status = ''
		   OR (COALESCE(status, 'Open') = 'Done' AND resolved_by IS NULL AND done_by IS NOT NULL)
		"""
	)
