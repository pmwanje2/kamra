// Contract tariffs as pricing schedules: one row per room type with its
// occupancy tiers (1-5 adults; legacy single / double items read as 1 and 2
// adults) and its extra adult, child and bed prices. Also the rounding and
// "create with reference" arithmetic the mass actions share. Pure: no
// storage or locale state, so the screen and its tests share one copy.

/** Rows in one bulk change or decision: hospitality-tariffs@11 refuses more (kamra.rates.TARIFF_BATCH_LIMIT). */
export const TARIFF_BATCH_LIMIT = 200
/** The largest tariff spreadsheet an import sends (kamra.rates.TARIFF_IMPORT_BYTES, 1 MiB). */
export const TARIFF_IMPORT_BYTES = 1_048_576

export type RoundingMode = "Nearest" | "Up" | "Down"
export type Adjustment = { kind: "percent" | "amount" | "exact"; value: number }
export type ExtraKind = "adult" | "child" | "bed"
export type FxLockPolicy = "Fixed at Reservation" | "Fixed at Check In" | "Current Date"

export interface TariffVersion {
  name: string
  rate: number
  local_equiv?: number | null
  valid_from: string
  valid_upto?: string | null
  workflow_state: string
  /** who proposed it and when (the Item Price's owner and creation) */
  owner?: string | null
  creation?: string | null
  /** the release or rejection: who decided and when; modified_by stands in for an older integration */
  approver?: string | null
  modified_by?: string | null
  modified?: string | null
  rejection_reason?: string | null
  can_approve?: boolean
}

export interface PriceList {
  name: string
  currency: string
  fx_rate?: number | null
  workflow_state?: string | null
  fx_status?: string | null
  stale_days?: number | null
  fx_date?: string | null
  rate_plans?: string[]
}

/** What a bulk approve or discard answered: hospitality-tariffs@11 counts and per-draft results, or the
 *  single-endpoint fallback's list of decided drafts. */
export interface DecisionResult {
  approved?: number | unknown[]
  discarded?: number | unknown[]
  skipped?: number
  results?: { name: string; status: string; reason?: string }[]
}

/** How many drafts a bulk decision decided and skipped, and why the skipped ones were. */
export function decisionOutcome(result: DecisionResult, kind: "approve" | "discard", sent: number) {
  const value = kind === "approve" ? result.approved : result.discarded
  const skippedRows = (result.results ?? []).filter((row) => row.status === "skipped")
  const skipped = typeof result.skipped === "number" ? result.skipped : skippedRows.length
  const done = typeof value === "number" ? value : Array.isArray(value) ? value.length : sent - skipped
  const reasons = [...new Set(skippedRows.map((row) => row.reason?.trim()).filter((reason): reason is string => !!reason))]
  return { done, skipped, reasons }
}

export interface ContractTariffItem {
  item_code: string
  item_name: string
  category: "Room" | "Supplement" | "Venue" | "Other"
  room_type?: string | null
  room_type_id?: string | null
  room_type_code?: string | null
  occupancy?: string | null
  price_list: string
  customer?: string | null
  currency?: string
  fx_rate?: number | null
  fx_date?: string | null
  active?: TariffVersion | null
  pending: TariffVersion[]
  history: TariffVersion[]
}

export interface RatePlanPolicy {
  label: string
  code?: string | null
  currency?: string | null
  fx_lock_policy: FxLockPolicy
  base_rate_plan?: string | null
  adjust_extra_person: boolean
  rounding_step: number
  rounding_mode: RoundingMode
}

interface TariffRow {
  item_code: string
  item_name: string
  room_type?: string | null
  occupancy?: string | null
  price_list: string
  customer?: string | null
}

const NAMED_TIERS = new Map([["single", 1], ["double", 2], ["twin", 2], ["triple", 3], ["quad", 4], ["quadruple", 4]])

/** The adults an occupancy tier prices, or null when it is not an adult tier. */
export function adultsTier(occupancy?: string | null, itemCode?: string): number | null {
  const text = (occupancy ?? "").trim().toLowerCase()
  const named = NAMED_TIERS.get(text)
  if (named) return named
  const counted = /^(\d+)\s*adults?$/.exec(text)
  if (counted) return Number(counted[1]) || null
  const coded = !text && itemCode ? /-(\d+)A$/i.exec(itemCode) : null
  return coded ? Number(coded[1]) || null : null
}

/** Which extra-person price a supplement is, from its occupancy or its legacy item code. */
export function extraKind(item: Pick<TariffRow, "item_code" | "item_name" | "occupancy">): ExtraKind | null {
  const words = `${item.occupancy ?? ""} ${item.item_code} ${item.item_name}`.toLowerCase().replace(/[-_]+/g, " ")
  if (/\bextra adult\b/.test(words)) return "adult"
  if (/\bchild\b/.test(words)) return "child"
  if (/\bextra bed\b/.test(words)) return "bed"
  return null
}

/** ISO 4217 decimals of a currency (0 for UGX or JPY, 3 for KWD); 2 when unknown. */
export function currencyDigits(currency?: string): number {
  try {
    if (currency) return new Intl.NumberFormat("en", { style: "currency", currency }).resolvedOptions().maximumFractionDigits ?? 2
  } catch {
    /* unknown currency code */
  }
  return 2
}

/** A price rounded to a multiple of `step` (0: the currency's minor unit), half up by default. */
export function roundRate(amount: number, step: number, mode: RoundingMode = "Nearest", currency?: string): number {
  const unit = step > 0 ? step : 10 ** -currencyDigits(currency)
  const units = Number((amount / unit).toPrecision(12))
  const whole = mode === "Up" ? Math.ceil(units) : mode === "Down" ? Math.floor(units) : Math.round(units)
  return Number((whole * unit).toPrecision(12))
}

/** A new rate from a reference rate: plus or minus a percent or an amount, or an exact figure. */
export function adjustRate(rate: number, { kind, value }: Adjustment): number {
  const next = kind === "percent" ? rate * (1 + value / 100) : kind === "amount" ? rate + value : value
  return Number(next.toPrecision(12))
}

export interface MatrixRow<T> {
  key: string
  priceList: string
  roomType: string
  customer: string | null
  /** adult tiers in order (1, 2, 3...) */
  tiers: Map<number, T>
  /** room items with an occupancy that is not an adult count */
  others: T[]
  extras: Partial<Record<ExtraKind, T>>
}

/** Room tariffs grouped by price list, room type and customer, with each
 *  row's extra-person supplements: the room type's own first, then the
 *  price list's general ones, the customer's before the public price. */
export function roomMatrix<T extends TariffRow>(rooms: T[], supplements: T[]): MatrixRow<T>[] {
  const rows = new Map<string, MatrixRow<T>>()
  for (const item of rooms) {
    const roomType = item.room_type || item.item_name
    const customer = item.customer || null
    const key = [item.price_list, roomType, customer ?? ""].join("|")
    let row = rows.get(key)
    if (!row) rows.set(key, row = { key, priceList: item.price_list, roomType, customer, tiers: new Map(), others: [], extras: {} })
    const tier = adultsTier(item.occupancy, item.item_code)
    if (tier != null && !row.tiers.has(tier)) row.tiers.set(tier, item)
    else row.others.push(item)
  }
  const sorted = [...rows.values()].sort((a, b) => a.priceList.localeCompare(b.priceList)
    || a.roomType.localeCompare(b.roomType) || (a.customer ?? "").localeCompare(b.customer ?? ""))
  for (const row of sorted) {
    row.tiers = new Map([...row.tiers].sort(([a], [b]) => a - b))
    const customers = row.customer ? [row.customer, null] : [null]
    for (const kind of ["adult", "child", "bed"] as const) {
      const candidates = supplements.filter((item) => item.price_list === row.priceList && extraKind(item) === kind)
      for (const customer of customers) {
        const own = candidates.filter((item) => (item.customer || null) === customer)
        const found = own.find((item) => item.room_type === row.roomType) ?? own.find((item) => !item.room_type)
        if (found) {
          row.extras[kind] = found
          break
        }
      }
    }
  }
  return sorted
}

/** The one rounding rule the plans behind a price list share, or null when they differ or none is known. */
export function priceListRounding(list: PriceList | undefined, policies: Record<string, RatePlanPolicy>) {
  const rules = (list?.rate_plans ?? []).map((name) => policies[name]).filter(Boolean)
    .map((plan) => ({ step: plan.rounding_step || 0, mode: plan.rounding_mode || "Nearest" }))
  if (!rules.length || rules.some((rule) => rule.step !== rules[0].step || rule.mode !== rules[0].mode)) return null
  return rules[0]
}
