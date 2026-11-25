/*  Books screens the site's ERP keeps (kamra.erp_books on the server).
    The shell boot sets window.kamra_erp_screens to {screen: {label, url}};
    a kept screen is left out of the navigation and, opened by URL, points
    at the ERP screen instead. Its endpoints refuse on the server anyway.
    It also sets window.kamra_erp_pos_url to the site's own point of sale
    when another one runs the restaurants (null otherwise). */

export interface KeptScreen {
  label: string
  url: string
}

declare global {
  interface Window {
    kamra_erp_screens?: Record<string, KeptScreen>
    kamra_erp_pos_url?: string | null
  }
}

const ROUTE_SCREEN: Record<string, string> = {
  "/payables": "payables",
  "/cash-forecast": "cash_forecast",
  "/profit-and-loss": "profit_and_loss",
  "/balance-sheet": "balance_sheet",
  "/accounting-export": "accounting_export",
  "/receivables": "receivables",
}

/** The ERP screen that keeps this route's books, or undefined. */
export function keptInErp(path: string): KeptScreen | undefined {
  const screens = typeof window === "undefined" ? undefined : window.kamra_erp_screens
  if (!screens) return undefined
  const clean = path.replace(/\/+$/, "").toLowerCase()
  const route = Object.keys(ROUTE_SCREEN).find((r) => clean === r || clean.startsWith(r + "/"))
  return route ? screens[ROUTE_SCREEN[route]] : undefined
}

/** The site-local route of the point of sale that runs the restaurants, when it is not this app's own. */
export function erpPosUrl(): string | undefined {
  return (typeof window === "undefined" ? undefined : window.kamra_erp_pos_url) || undefined
}

/** Where an in-app route lives when this app no longer does the job: the ERP
 *  screen that keeps a books route, or the outlet point of sale for /pos. */
export function externalRoute(path: string): string | undefined {
  const kept = keptInErp(path)
  if (kept) return kept.url
  const clean = path.replace(/\/+$/, "").toLowerCase()
  return clean === "/pos" ? erpPosUrl() : undefined
}
