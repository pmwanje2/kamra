/** Rules for a reservation or walk-in discount (OPERA Cloud Reservation
 * Discounts: a percentage OR an amount off each night's room rate, with a
 * mandatory reason; Daily Details nights of their own). The server prices it
 * and enforces every rule again (kamra.api.discount_governance); these only
 * decide what the screen sends and when Save can be pressed. */

export type DiscountType = "Percent" | "Amount"

/** One night's own discount as typed (Daily Details). */
export interface NightEntry {
  type: DiscountType
  value: string
  /** the night's own Reason Code ("" or missing: the stay's reason) */
  reason?: string
}

export interface DiscountState {
  type: DiscountType
  /** as typed; "" = no discount for the stay */
  value: string
  /** Reason Code name; required whenever a discount is given */
  reason: string
  /** date -> the night's own discount ("" value: the night follows the stay) */
  nights: Record<string, NightEntry>
  /** the desk's optional free text beside the reason; it goes on the audit
   * row with the save, never with a preview (discountNote) */
  note?: string
}

export interface DiscountReason {
  name: string
  code?: string
  description: string
  category: string
  requires_supervisor: number
}

/** kamra.api.discount_governance: what the discount takes to book. */
export interface DiscountGovernance {
  base_total: number
  discount_total: number
  net_total: number
  effective_pct: number
  max_pct: number
  limit_pct: number
  needs_supervisor: boolean
  can_set_rate: boolean
  agent: boolean
  eligible: boolean
  comp: boolean
  floor_breach: string[]
  reason_requires_supervisor: boolean
  supervisors: { user: string; full_name: string }[]
  fixed_rate?: number
}

export interface DiscountArgs {
  discount_type?: DiscountType
  discount_value?: number
  discount_reason?: string
  night_discounts?: { date: string; discount_type: DiscountType; discount_value: number; discount_reason?: string }[]
  /** a manager's fixed-rate override of a plan that takes no discount */
  fixed_rate?: 0 | 1
}

/** A night's room rate before the discount on the basis the desk enters one:
 * the net rate after the discount (gross where the property's rates include
 * tax) plus the discount, which the server reports on that same basis. */
export function entryBase(
  n: { rate: number; gst_rate?: number; base_rate?: number; discount_amount?: number },
  inclusive: boolean,
): number {
  const net = inclusive ? Math.round(n.rate * (1 + (n.gst_rate ?? 0) / 100) * 100) / 100 : n.rate
  return Math.round((net + (n.discount_amount ?? 0)) * 100) / 100
}

export const emptyDiscount = (): DiscountState => ({ type: "Percent", value: "", reason: "", nights: {}, note: "" })

/** The note a save sends (discount_note), only beside a discount. */
export function discountNote(state: Pick<DiscountState, "note">, args: DiscountArgs): { discount_note?: string } {
  const note = state.note?.trim()
  return note && (args.discount_type || args.night_discounts?.length) ? { discount_note: note } : {}
}

const num = (s: string) => (s.trim() === "" ? null : Number(s))

/** What get_quote / create_booking / walk_in take for the discount entered:
 * nothing at all when none is (existing callers are unchanged). Only nights
 * of this stay are sent. */
export function discountArgs(state: DiscountState, stayNights: string[]): DiscountArgs {
  const value = num(state.value)
  const stay = value !== null && Number.isFinite(value) && value > 0
  const nights = stayNights
    .filter((date) => state.nights[date] && num(state.nights[date].value) !== null)
    .map((date) => ({
      date,
      discount_type: state.nights[date].type,
      discount_value: Number(state.nights[date].value),
      ...(state.nights[date].reason ? { discount_reason: state.nights[date].reason } : {}),
    }))
  if (!stay && !nights.length) return {}
  return {
    ...(stay ? { discount_type: state.type, discount_value: value } : {}),
    discount_reason: state.reason || undefined,
    ...(nights.length ? { night_discounts: nights } : {}),
  }
}

/** Apply to future nights (OPERA Daily Details): the night and every later
 * night of the stay take the same discount. */
export function applyToFutureNights(
  nights: Record<string, NightEntry>,
  stayNights: string[],
  from: string,
  entry: NightEntry,
): Record<string, NightEntry> {
  const out = { ...nights }
  for (const date of stayNights) if (date >= from) out[date] = { ...entry }
  return out
}

/** A night as the discount bounds read it: its room rate before the
 * discount, on the basis the desk enters one (gross where rates include tax). */
export interface NightBase {
  date: string
  base_rate: number
}

function entryError(type: DiscountType, raw: string, nightRate?: number): string | null {
  const value = num(raw)
  if (value === null) return null
  if (!Number.isFinite(value) || value < 0) return "Enter a discount of zero or more."
  if (type === "Percent" && value > 100) return "A percentage is between 0 and 100."
  if (type === "Amount" && nightRate !== undefined && value > nightRate + 0.005)
    return "The discount is more than the night's rate."
  return null
}

/** The bounds the server holds too (SAP OVB2): 0 to 100%, never below zero,
 * and an amount never more than the night's rate - the stay's discount
 * against its lowest night, each Daily Details night against its own.
 * `nights` is the stay's nights (or just the lowest rate). null when fine. */
export function discountError(
  state: Pick<DiscountState, "type" | "value"> & { nights?: DiscountState["nights"] },
  nights?: number | NightBase[],
): string | null {
  const list = Array.isArray(nights) ? nights : []
  const lowest = typeof nights === "number" ? nights
    : list.length ? Math.min(...list.map((n) => n.base_rate)) : undefined
  const stay = entryError(state.type, state.value, lowest)
  if (stay) return stay
  for (const night of list) {
    const own = state.nights?.[night.date]
    const bad = own && entryError(own.type, own.value, night.base_rate)
    if (bad) return bad
  }
  return null
}

/** Whether the booking (or Save) can go ahead: a discount needs a reason,
 * a rate plan that takes one (or a manager's fixed-rate override), and a
 * supervisor with their PIN when the server says it is over the desk's
 * limit (or a comp, under the floor, or a reason that needs one). */
export function discountReady(s: {
  args: DiscountArgs
  governance: DiscountGovernance | null | undefined
  supervisor: string
  pin: string
  /** a Revenue Manager / Hotel Admin fixed the rate of a plan that takes none */
  fixedOverride?: boolean
}): boolean {
  if (!s.args.discount_type && !s.args.night_discounts?.length) return true
  if (!s.args.discount_reason) return false
  if (s.governance && s.governance.eligible === false && !s.fixedOverride) return false
  if (s.governance?.needs_supervisor) return !!s.supervisor && !!s.pin.trim()
  return true
}

/** The supervisor's sign-off, sent only when the discount needs one. */
export function supervisorArgs(
  governance: DiscountGovernance | null | undefined,
  supervisor: string,
  pin: string,
): { supervisor_user?: string; supervisor_pin?: string } {
  return governance?.needs_supervisor && supervisor && pin.trim()
    ? { supervisor_user: supervisor, supervisor_pin: pin.trim() }
    : {}
}

/** OPERA's Rate Code "Discount" flag: no plan, or a plan without the flag
 * (older data), may be discounted; a plan marked 0 may not. */
export function discountEligible(
  plans: { name: string; discount_eligible?: number | boolean | null }[] | undefined,
  plan: string | null | undefined,
): boolean {
  if (!plan) return true
  const found = plans?.find((p) => p.name === plan)
  if (!found || found.discount_eligible === undefined || found.discount_eligible === null) return true
  return !!Number(found.discount_eligible)
}
