// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import App from "../src/App"

// A folio charge line's actions (B-3): Correct… and Move sit on one line, and the
// inline correction form has the cell to itself while it is open.
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

const charge = { name: "CH-1", posting_date: "2026-11-22", charge_type: "Room", description: "Deluxe King",
  amount: 474500, gst_rate: 0, gst_amount: 0, total: 474500 }
const invoice = {
  folio: { name: "FOLIO-B3", property: "Test Hotel", status: "Open", folio_type: "Guest", guest_name: "Folio Guest",
    charges: [charge], payments: [], charges_total: 474500, tax_total: 0, grand_total: 474500, payments_total: 0,
    balance: 474500 },
  property: { currency: "UGX", name: "Test Hotel", legal_name: null, logo_url: null, address: "", state: null,
    gstin: null, phone: null, email: null, sac: null, place_of_supply: null },
  guest: { name: "Folio Guest" },
  stay: { reservation: "RES-B3", check_in: "2026-11-22", check_out: "2026-11-23", arrival: null, departure: null,
    nights: 1, adults: 2, children: 0, pax: 2, meal_plan: null, room_type: "Test Hotel-DLX", room: null, company: null,
    tariff: null, agreed_rate: 474500, reference_currency: null },
  lines: [], summary_by_head: [], tax_summary: [], gst_summary: [], bill_to: null,
}
const options = {
  actions: [{ id: "void_charge", label: "Void", requires: [], reason_codes: [
    { name: "ERR", description: "Posting error", requires_supervisor: 0 }] }],
  blocked: [], supervisors: [], refund_cap: 0,
}

describe("Folio charge row actions", () => {
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

  async function render() {
    rpc.mockReset().mockImplementation(async (method: string) => {
      if (method === "kamra.api.folio_invoice") return invoice
      if (method === "kamra.api.property_locale") return { locale: "en", currency: "UGX", payment_modes: ["Cash"] }
      if (method === "kamra.ledger.list_exchange_rates") return { rates: [], business_date: "2026-11-22" }
      if (method === "kamra.cashier.my_till") return { open: true, property: "Test Hotel", terminal_links: [] }
      if (method === "kamra.api.linked_records") return { folios: [], reservations: [] }
      if (method === "kamra.api.correct_charged_nights") return { nights: [] }
      if (method === "kamra.api.payment_correction_options") return options
      return []
    })
    await act(async () => root.render(<MemoryRouter initialEntries={["/billing/FOLIO-B3"]}><App /></MemoryRouter>))
    await vi.waitFor(async () => {
      await act(() => new Promise((resolve) => setTimeout(resolve, 10)))
      expect(host.querySelector("button[aria-label='Move or split this charge']")).not.toBeNull()
    }, { interval: 5, timeout: 5000 })
  }
  const move = () => host.querySelector<HTMLButtonElement>("button[aria-label='Move or split this charge']")
  const correct = () => Array.from(host.querySelectorAll("button")).find((node) => node.textContent?.trim() === "Correct…")

  it("puts Correct… and Move on one compact line", async () => {
    await render()
    const cluster = move()!.closest<HTMLElement>("[data-row-actions]")
    expect(cluster, "the charge's actions share one cluster").not.toBeNull()
    expect(cluster!.contains(correct()!)).toBe(true)
    expect(cluster!.className).toContain("inline-flex")
    expect(cluster!.className).toContain("items-center")
    // both actions read as the same size of control
    expect(correct()!.className).toContain("rounded-md")
    expect(correct()!.className).toContain("px-2")
    expect(move()!.className).toContain("rounded-md")
    expect(move()!.className).toContain("px-2")
  })

  it("gives the inline correction form the cell while it is open, then brings Move back", async () => {
    await render()
    await act(async () => correct()!.click())
    await vi.waitFor(() => expect(host.querySelector("select[aria-label='Correction action']")).not.toBeNull())
    expect(move(), "Move waits while the correction form is open").toBeNull()
    expect(rpc).toHaveBeenCalledWith("kamra.api.payment_correction_options", { folio: "FOLIO-B3", payment_row: "CH-1" })
    const form = host.querySelector("select[aria-label='Correction action']")!.parentElement!
    const close = Array.from(form.querySelectorAll("button")).find((node) => node.textContent?.trim() === "Close")!
    await act(async () => close.click())
    expect(host.querySelector("select[aria-label='Correction action']")).toBeNull()
    expect(move()).not.toBeNull()
    expect(correct()).toBeDefined()
  })
})
