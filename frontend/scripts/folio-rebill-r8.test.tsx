// @vitest-environment jsdom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, expect, it, vi } from "vitest"
import FolioView from "../src/screens/FolioView"

const { rpc, hotel } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return { rpc: vi.fn(), hotel: { currency: "USD" } }
})
vi.mock("../src/lib/api", async (original) => ({
  ...await original<typeof import("../src/lib/api")>(), call: rpc, getCurrentProperty: () => "Hotel",
}))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ roles: ["Finance"] }) }))
vi.mock("../src/lib/money", async (original) => {
  const money = await original<typeof import("../src/lib/money")>()
  return { ...money, useLocale: () => ({
    ...money.locale(), currency: hotel.currency, payment_modes: ["Cash"], fx_currencies: ["EUR"],
    tender_modes: [{ mode: "Cash", currencies: [hotel.currency, "EUR"] }],
  }) }
})

afterEach(() => vi.unstubAllGlobals())

it.each(["UGX", "USD", "KWD"])("uses collectible debt for the %s window, cash, full tender and FX preview", async (currency) => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  hotel.currency = currency
  const window = { name: "R8", status: "Open", folio_type: "Guest", balance: 80, net_collectible_balance: 55,
    grand_total: 80, payments_total: 0, window_no: 1 }
  rpc.mockImplementation(async (method: string, args: Record<string, unknown>) => {
    if (method === "kamra.api.folio_invoice") return {
      folio: { ...window, property: "Hotel", charges: [], payments: [], charges_total: 80, tax_total: 0 },
      net_collectible_balance: 55, credited_amount: 25, credit_status: "Partially credited",
      property: { name: "Hotel", currency }, guest: { name: "Guest" },
      stay: { reservation: "Reservation", nights: 1 }, lines: [], summary_by_head: [], tax_summary: [], gst_summary: [],
    }
    if (method.endsWith("linked_records")) return { folios: [], reservations: [] }
    if (method.endsWith("folio_windows_overview")) return { windows: [window] }
    if (method.endsWith("list_exchange_rates")) return { business_date: "2026-11-25",
      rates: [{ currency: "EUR", rate_date: "2026-11-25", settlement_rate: 2 }] }
    if (method.endsWith("payment_rate")) return { currency: "EUR", home_currency: currency,
      rate_date: "2026-11-25", rate: 2, home_amount: 2 * Number(args.amount), quote_id: "quote" }
    return []
  })
  const host = document.createElement("div")
  const root = createRoot(host)
  try {
    await act(async () => root.render(<MemoryRouter initialEntries={["/billing/R8"]}><Routes>
      <Route path="/billing/:name" element={<FolioView />} />
    </Routes></MemoryRouter>))
    const input = host.querySelector<HTMLInputElement>('input[aria-label="Amount collected"]')!
    expect.soft(host.querySelector('[role="tab"]')?.textContent).toMatch(/55/)
    expect.soft(input?.value).toBe("55")
    const button = (text: string) => [...host.querySelectorAll("button")].find((b) => b.textContent === text)!
    await act(async () => button("Full amount").click())
    expect.soft(input.value).toBe("55")
    await act(async () => button("EUR").click())
    expect.soft(input.value).toBe("27.5")
    expect(rpc).toHaveBeenCalledWith("kamra.ledger.payment_rate", expect.objectContaining({ amount: 27.5 }))
    expect(host.textContent).toContain("Settled in full")
  } finally {
    await act(async () => root.unmount())
  }
})
