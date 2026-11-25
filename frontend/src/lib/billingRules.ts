/** A company billing rule as the check-in and check-out screens list it
 * (OPERA profile routing): a charge-type rule by its charge type, a rule
 * that names one transaction code by that code and its description. */
export interface BillingRuleRow {
  charge_type: string | null
  transaction_code?: string | null
  code_label?: string | null
  pay_by: string
}

export function ruleLabel(rule: BillingRuleRow, translate: (text: string) => string): string {
  if (rule.transaction_code) return rule.code_label || rule.transaction_code
  return translate(rule.charge_type ?? "")
}

export function ruleKey(rule: BillingRuleRow): string {
  return rule.transaction_code ? `#${rule.transaction_code}` : rule.charge_type ?? ""
}
