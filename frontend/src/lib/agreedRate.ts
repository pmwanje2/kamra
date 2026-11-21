/** Rules for the Agreed rate action on a booking (kamra.api.set_agreed_rate):
 * an agreed nightly rate for one stay, in place (OPERA rate override). */

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
 * needs one. The desk never raises a rate. The server checks all of it again. */
export function agreedRateReady(s: {
  preview: AgreedRatePreview | null
  reasonCode: string
  codeNeedsSupervisor: boolean
  supervisor: string
  pin: string
}): boolean {
  const p = s.preview
  if (!p || !s.reasonCode) return false
  if (p.can_set_rate) return true
  if (p.raises) return false
  if (p.needs_supervisor || s.codeNeedsSupervisor) return !!s.supervisor && !!s.pin.trim()
  return true
}
