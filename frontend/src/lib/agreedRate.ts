/** Rules for the Agreed rate action on a booking (kamra.api.set_agreed_rate):
 * an agreed nightly rate for one stay, in place (OPERA rate override). */

import type { DiscountGovernance, DiscountReason } from "./reservationDiscount"

/** The reason-code categories a discount and an agreed rate take: the
 * property's settings, sent by the server (reservation_detail.reason_categories). */
export interface ReasonCategories {
  discount?: string[]
  agreed_rate?: string[]
}

/** The codes of the given categories from one list_reason_codes answer,
 * each once by name, in the server's order (every category when none are
 * given). */
function codesOf(all: DiscountReason[] | null | undefined, categories?: readonly string[] | null) {
  const seen = new Set<string>()
  const out: DiscountReason[] = []
  for (const r of all ?? []) {
    if ((categories && !categories.includes(r.category)) || seen.has(r.name)) continue
    seen.add(r.name)
    out.push(r)
  }
  return out
}

/** The reason codes a discount can carry: its categories' codes. */
export function discountReasonCodes(all: DiscountReason[] | null | undefined,
  categories?: readonly string[] | null): DiscountReason[] {
  return codesOf(all, categories)
}

/** The reason codes an agreed rate can carry: its categories' codes. */
export function agreedRateReasonCodes(all: DiscountReason[] | null | undefined,
  categories?: readonly string[] | null): DiscountReason[] {
  return codesOf(all, categories)
}

export interface AgreedRatePreview {
  current_amount: number
  new_amount: number
  difference: number
  current_nightly: number
  agreed_rate: number
  reduction_pct: number
  limit_pct: number
  /** a Revenue Manager / Hotel Admin sets any positive rate */
  can_set_rate: boolean
  raises: boolean
  /** a desk cut over the property's limit: a supervisor signs it off */
  needs_supervisor: boolean
  needs_confirmation: boolean
  nights: { date: string; rate: number }[]
  /** who can sign it off here (Hotel Admin / Finance with a cashier PIN) */
  supervisors: { user: string; full_name: string }[]
  /** nights the new rate takes under the rate guardrail floor */
  floor_breach?: string[]
  /** the stay had a desk discount: the agreed rate replaces it */
  replaces_discount?: boolean
  /** a stay's discount judged with the rate (older servers; an agreed rate
   * now replaces the discount, so the server no longer sends one) */
  discount_governance?: DiscountGovernance
}

export interface AgreedRateRow {
  date: string
  was: number
  now: number
  diff: number
}

/** Each night: the rate the folio bills now, the agreed one, the difference. */
export function agreedRateRows(
  current: { date: string; rate: number }[],
  preview: Pick<AgreedRatePreview, "nights">,
): AgreedRateRow[] {
  const was = new Map(current.map((n) => [n.date, n.rate]))
  return preview.nights.map((n) => {
    const before = was.get(n.date) ?? 0
    return { date: n.date, was: before, now: n.rate, diff: Math.round((n.rate - before) * 100) / 100 }
  })
}

/** Whether Save can be pressed: a Rate Change reason code, and a supervisor
 * with their PIN when the desk's cut is over the limit or the reason code
 * needs one, or when the stay's discount on the new rate needs one (a full
 * comp, a manager's included). The desk never raises a rate. The server
 * checks all of it again. */
export function agreedRateReady(s: {
  preview: AgreedRatePreview | null
  reasonCode: string
  codeNeedsSupervisor: boolean
  supervisor: string
  pin: string
}): boolean {
  const p = s.preview
  if (!p || !s.reasonCode) return false
  if (!p.can_set_rate && p.raises) return false
  return agreedRateNeedsSupervisor(p, s.codeNeedsSupervisor) ? !!s.supervisor && !!s.pin.trim() : true
}

/** A supervisor signs this agreed rate off: the desk's cut is over the
 * limit or its reason code needs one, or the stay's discount on the new rate
 * needs one (a full comp, a manager's included). */
export function agreedRateNeedsSupervisor(p: AgreedRatePreview, codeNeedsSupervisor: boolean): boolean {
  return !!p.discount_governance?.needs_supervisor
    || (!p.can_set_rate && (p.needs_supervisor || codeNeedsSupervisor))
}
