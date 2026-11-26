import { activeMoneyLocale, fmtAmount } from "./money"

export function formatMoveDelta(amount: number, currency: string, places: number): string {
  const locale = activeMoneyLocale()
  const symbol = new Intl.NumberFormat(locale, {
    style: "currency",
    currency,
    currencyDisplay: "narrowSymbol",
  }).formatToParts(0).find((part) => part.type === "currency")?.value ?? currency
  const figures = fmtAmount(amount, currency, places, locale)
  return symbol === currency ? `${currency} ${figures}` : `${symbol}${figures}`
}

export type HousekeepingWork = "all" | "discrepancies" | "turndown"

export function housekeepingWorkRows<T extends {
  discrepancy?: string | null
  turndown_status?: string | null
}>(rows: T[], view: HousekeepingWork): T[] {
  if (view === "discrepancies") return rows.filter((row) => row.discrepancy)
  if (view === "turndown") {
    return rows.filter((row) => row.turndown_status && row.turndown_status !== "Not Required")
  }
  return rows
}
