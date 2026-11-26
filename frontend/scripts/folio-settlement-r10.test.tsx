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
    ...money.locale(), integrations: { ...money.locale().integrations, payment_gateways: ["Demo"] }, currency: hotel.currency, payment_modes: ["Cash"], fx_currencies: ["EUR"],
    tender_modes: [{ mode: "Cash", currencies: [hotel.currency, "EUR"] }],
  }) }
})

afterEach(() => vi.unstubAllGlobals())

it.each(["UGX", "USD", "KWD"])("uses signed %s balances for cashier actions", async currency => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  hotel.currency = currency
  for (const [posted, signed] of [[10, -15], [0, 30], [-5, 25], [-30, 0]]) {
    const bill = { name: "R10", property: "Hotel", status: "Open", folio_type: "Guest",
      balance: posted, window_no: 1, charges: [{ name: "L1", amount: 80 }], payments: [],
      charges_total: 80, grand_total: 80, payments_total: 80 - posted, tax_total: 0 }
    rpc.mockImplementation(async (method: string) => {
      if (method.endsWith("folio_invoice")) return {
        folio: bill, settlement_balance: signed, net_collectible_balance: Math.max(0, signed),
        property: { name: "Hotel", currency }, guest: { name: "Guest" },
        stay: { reservation: "Stay", nights: 1 }, lines: [], summary_by_head: [], tax_summary: [], gst_summary: [],
      }
      if (method.endsWith("linked_records")) return { folios: [], reservations: [] }
      if (method.endsWith("folio_windows_overview")) return { windows: [bill] }
      return []
    })
    const host = document.createElement("div")
    const root = createRoot(host)
    try {
      await act(async () => root.render(<MemoryRouter initialEntries={["/billing/R10"]}><Routes>
        <Route path="/billing/:name" element={<FolioView />} />
      </Routes></MemoryRouter>))
      const buttons = [...host.querySelectorAll("button")].map(button => button.textContent)
      expect.soft(buttons.includes("Payment link")).toBe(signed > 0)
      expect.soft(buttons.includes("Settle & continue stay")).toBe(signed === 0)
      expect.soft(host.querySelector<HTMLInputElement>('input[aria-label="Amount collected"]')?.value ?? "").toBe(
        signed > 0 ? String(signed) : "",
      )
    } finally {
      await act(async () => root.unmount())
    }
  }
})
