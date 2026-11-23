/*  Books screens the site's ERP keeps (kamra.erp_books on the server).
    The shell boot sets window.kamra_erp_screens to {screen: {label, url}};
    a kept screen is left out of the navigation and, opened by URL, points
    at the ERP screen instead. Its endpoints refuse on the server anyway. */

export interface KeptScreen {
  label: string
  url: string
}

declare global {
  interface Window {
    kamra_erp_screens?: Record<string, KeptScreen>
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
