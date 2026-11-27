// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import App from "../src/App"

// Property.reference_currency_calculation on the bill (Gap Record #8556): the contract
// currency, the calculation (Actual or Current), its rate and date, and the bill in it.
const { rpc } = vi.hoisted(() => {
  // Node's own localStorage stub hides jsdom's; the real api module reads it on import.
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return { rpc: vi.fn() }
})
vi.mock("../src/lib/api", async (original) => ({
  ...await original<typeof import("../src/lib/api")>(),
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => "Test Hotel",
  frappeFetch: async (path: string) => ({ data: path.includes("?") ? [] : {} }),
  serverError: (error: Error) => error.message,
}))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: ["Hotel Admin"] }) }))
vi.mock("../src/AppShell", () => ({ default: () => <Outlet /> }))

const tariff = { currency: "USD", nightly: 130, fx_rate: 3650, fx_date: "2026-11-20", property_currency: "UGX" }
const invoice = (reference: Record<string, unknown> | null) => ({
  folio: { name: "FOLIO-K3", property: "Test Hotel", status: "Open", folio_type: "Guest", guest_name: "Contract Guest",
    charges: [], payments: [], charges_total: 949000, tax_total: 0, grand_total: 949000, payments_total: 474500,
    balance: 474500 },
  property: { currency: "UGX", name: "Test Hotel", legal_name: null, logo_url: null, address: "", state: null,
    gstin: null, phone: null, email: null, sac: null, place_of_supply: null },
  guest: { name: "Contract Guest" },
  stay: { reservation: "RES-K3", check_in: "2026-11-21", check_out: "2026-11-23", arrival: null, departure: null,
    nights: 2, adults: 2, children: 0, pax: 2, meal_plan: null, room_type: "Test Hotel-DLX", room: null, company: null,
    tariff, agreed_rate: 474500, reference_currency: reference },
  lines: [], summary_by_head: [], tax_summary: [], gst_summary: [], bill_to: null,
})

describe("Folio reference currency", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    host = document.createElement("div")
    document.body.append(host)
    root = createRoot(host)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    host.remove()
    localStorage.clear()
    vi.unstubAllGlobals()
  })

  async function render(reference: Record<string, unknown> | null, changes: Record<string, unknown> = {}) {
    rpc.mockReset().mockImplementation(async (method: string) => {
      if (method === "kamra.api.folio_invoice") return { ...invoice(reference), ...changes }
      if (method === "kamra.api.property_locale") return { locale: "en", currency: "UGX", payment_modes: ["Cash"] }
      if (method === "kamra.ledger.list_exchange_rates") return { rates: [{ currency: "EUR", rate_date: "2026-11-22", buy_rate: 4000 }], business_date: "2026-11-22" }
      if (method === "kamra.ledger.folio_in_currency") return { currency: "EUR", rate: 4000, rate_date: "2026-11-22", home_currency: "UGX", grand_total: 215.16, balance: -1936.41 }
      if (method === "kamra.cashier.my_till") return { open: true, property: "Test Hotel", terminal_links: [] }
      if (method === "kamra.api.linked_records") return { folios: [], reservations: [] }
      if (method === "kamra.api.correct_charged_nights") return { nights: [] }
      return []
    })
    await act(async () => root.render(<MemoryRouter initialEntries={["/billing/FOLIO-K3"]}><App /></MemoryRouter>))
    await vi.waitFor(async () => {
      await act(() => new Promise((resolve) => setTimeout(resolve, 10)))
      expect(host.textContent).toContain("Contract Guest")
    }, { interval: 5, timeout: 5000 })
  }
  const reference = () => host.querySelector<HTMLElement>("[data-reference-currency]")

  it("R3 prints the fixed home rate, full room and meal names, and deferred tax note", async () => {
    const note = "Final VAT breakdown is issued on the tax invoice at settlement."
    await render(null, { tax_source: "accounting_deferred", document: { accounting_tax_note: note },
      stay: { ...invoice(null).stay, tariff: null, pricing_basis: "fixed_home", fixed_rate_amount: 290333.33,
        room_type: "Test Hotel-JUNIOR-SUITE", room_type_name: "Junior Suite",
        meal_plan: "Test Hotel-FULL-BOARD", meal_plan_name: "Full Board" } })
    expect.soft(host.textContent).toContain("Fixed agreed rate: UGX 290,333 / night")
    expect.soft(host.textContent).toContain("Junior Suite")
    expect.soft(host.textContent).toContain("Full Board")
    expect.soft(host.textContent).toContain(note)
  })

  it("R3 displays a credit as an advance and prints matching contract FX only once", async () => {
    await render({ currency: "USD", home_currency: "UGX", calculation: "ACTUAL", fx_rate: 3650, fx_date: "2026-11-20",
      grand_total_reference: 260, balance_reference: -1936.41 })
    expect.soft(reference()?.textContent).toContain("Advance USD 1,936.41")
    expect.soft(host.textContent?.match(/3,650/g)).toHaveLength(1)
  })

  it("R3 shows the converted credit as an advance when a third currency is selected", async () => {
    await render({ currency: "USD", home_currency: "UGX", calculation: "ACTUAL", fx_rate: 3650, fx_date: "2026-11-20" })
    const select = [...host.querySelectorAll("select")].find((node) => node.closest("label")?.textContent?.includes("Show in"))!
    await act(async () => {
      select.value = "EUR"
      select.dispatchEvent(new Event("change", { bubbles: true }))
    })
    expect(host.textContent).toMatch(/advance EUR 1,?936\.41/i)
    expect(host.textContent).not.toMatch(/balance EUR -/)
  })

  it("shows an Actual reference currency with the bill and balance in it", async () => {
    await render({ currency: "USD", home_currency: "UGX", calculation: "ACTUAL", fx_rate: 3650, fx_date: "2026-11-20",
      grand_total_reference: 260, balance_reference: 130 })
    const note = reference()
    expect(note, "the bill must show its reference currency").not.toBeNull()
    expect(note!.textContent).toContain("Reference currency USD · Actual: 1 USD = 3,650 UGX (Nov 20, 2026)")
    expect(note!.textContent).toContain("Total USD 260.00 · Balance USD 130.00")
  })

  it("names a Current calculation and the nights an Actual rate was posted at", async () => {
    await render({ currency: "USD", home_currency: "UGX", calculation: "CURRENT", fx_rate: 3800, fx_date: "2026-11-22",
      grand_total_reference: 249.74, balance_reference: 124.87 })
    expect(reference()!.textContent).toContain("Reference currency USD · Current: 1 USD = 3,800 UGX (Nov 22, 2026)")
    await act(async () => root.unmount())
    root = createRoot(host)
    await render({ currency: "USD", home_currency: "UGX", calculation: "ACTUAL", fx_rate: 3750, fx_date: "2026-11-22",
      posted_nights: 2, grand_total_reference: 253.07, balance_reference: 126.53 })
    expect(reference()!.textContent).toContain("Average of the rates 2 nights posted at")
  })

  it("shows nothing for a stay billed in the hotel currency", async () => {
    await render(null)
    expect(reference()).toBeNull()
  })
})
