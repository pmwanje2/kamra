export interface SiblingFolio {
  name: string
  folio_type: string
  status: string
  invoice_number?: string | null
  balance: number
  net_collectible_balance?: number
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
  sales_invoice?: string | null
  charges_total?: number
  payee_name?: string | null
  efris_fiscalized?: boolean
  routed_rule_count?: number
}

/** Compact billing window badge (W1..W8 or PM for an unnumbered group master). */
export function windowBadge(s: Pick<SiblingFolio, "window_no" | "folio_type">): string {
  if (s.window_no && s.window_no > 0) return `W${s.window_no}`
  return s.folio_type === "Group" ? "PM" : "W1"
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

/** Build RPC arguments for `kamra.api.share_folio_charges`. When all target
 *  bills stay at the displayed even split (e.g. "33.33" for a 3-way split),
 *  omit `shares` so the server cuts exact 1/(N+1) parts in whole minor units
 *  with the rounding remainder on the source window (OPERA). Explicit custom
 *  percentages are forwarded as entered. */
export function shareFolioArgs(
  fromFolio: string,
  chargeRows: string[],
  toFolios: string[],
  shares: Record<string, string>,
): { from_folio: string; charge_rows: string[]; to_folios: string[]; shares?: number[] } {
  const even = (100 / (toFolios.length + 1)).toFixed(2)
  const isEven = toFolios.every((f) => {
    const raw = shares[f]
    return raw === undefined || raw === "" || raw === even
  })
  const payload: {
    from_folio: string
    charge_rows: string[]
    to_folios: string[]
    shares?: number[]
  } = {
    from_folio: fromFolio,
    charge_rows: chargeRows,
    to_folios: toFolios,
  }
  if (!isEven) {
    payload.shares = toFolios.map((f) => Number(shares[f] ?? even))
  }
  return payload
}


/** Retain signed advances while presenting the collectible amount on debit windows. */
export function windowBalance(w: Pick<SiblingFolio, "balance" | "net_collectible_balance">) {
  return w.balance < 0 ? w.balance : w.net_collectible_balance ?? w.balance
}
