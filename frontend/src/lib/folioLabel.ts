export interface SiblingFolio {
  name: string
  folio_type: string
  status: string
  invoice_number?: string | null
  balance: number
  grand_total: number
  payments_total: number
  /** a guest's own bill in a shared room, made out to them */
  bill_to_name?: string | null
  /** the company a Company or Group bill goes to */
  company?: string | null
  window_no?: number | null
  payee_type?: string | null
  payee_company?: string | null
  payee_agent?: string | null
  agent_name?: string | null
  payment_method?: string | null
}

/** A distinct label per folio window. Travel Agent windows name their agent
 *  rather than falling back to "Extra". Plain Extras get numbered when
 *  there's more than one. Any other type that repeats (Settle & continue stay
 *  closes the Guest folio and opens a fresh one) names the settled one by its
 *  invoice number, and numbers open ones that still share a type. */
export function folioLabel(
  siblings: SiblingFolio[],
  s: SiblingFolio,
  tr: (s: string, v?: Record<string, string | number>) => string,
) {
  if (s.bill_to_name) return s.bill_to_name
  if (s.payee_type === "Travel Agent") {
    const agent = s.agent_name || s.payee_agent
    const base = agent ? `${tr("Travel Agent")} · ${agent}` : tr("Travel Agent")
    const same = siblings.filter(
      (x) => x.payee_type === "Travel Agent" && (x.payee_agent || "") === (s.payee_agent || ""),
    )
    if (same.length <= 1) return base
    if (s.status === "Closed")
      return s.invoice_number ? `${base} · ${s.invoice_number}` : tr("{type} · settled", { type: base })
    const open = same.filter((x) => x.status !== "Closed")
    if (open.length <= 1) return base
    return `${base} ${open.findIndex((x) => x.name === s.name) + 1}`
  }
  const same = siblings.filter(
    (x) =>
      x.folio_type === s.folio_type &&
      x.payee_type !== "Travel Agent" &&
      !x.bill_to_name,
  )
  if (s.folio_type === "Extra") {
    if (same.length <= 1) return tr("Extra")
    return tr("Extra {n}", { n: same.findIndex((x) => x.name === s.name) + 1 })
  }
  // a company bill names its company: a stay can carry a second sponsor's
  // company window, or a group member can carry its own company's bill next
  // to the group's
  const company = s.payee_company || s.company
  const type = company ? `${tr(s.folio_type)} · ${company}` : tr(s.folio_type)
  const sameNamed = company
    ? same.filter((x) => (x.payee_company || x.company) === company)
    : same
  if (sameNamed.length <= 1) return type
  if (s.status === "Closed")
    return s.invoice_number ? `${type} · ${s.invoice_number}` : tr("{type} · settled", { type })
  const open = sameNamed.filter((x) => x.status !== "Closed")
  if (open.length <= 1) return type
  return `${type} ${open.findIndex((x) => x.name === s.name) + 1}`
}
