// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { BookingDialog } from "../src/components/BookingDialog"
import { RateOptions } from "../src/components/RateOptions"

// Round 6 (OPERA Cloud / SAP S/4HANA parity): the booking dialog's Rate Options show the company's own
// tariff, as the quote below them does (OPERA Rate Query with a Company, SAP PR02 A305), and say how
// each plan fixes its exchange rate and what it derives from.
const { rpc } = vi.hoisted(() => {
  // Node's own localStorage stub hides jsdom's; the real api module reads it on import.
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return { rpc: vi.fn() }
})
vi.mock("../src/lib/api", async (original) => ({
  ...await original<typeof import("../src/lib/api")>(),
  call: (method: string, params: unknown) => rpc(method, params),
  getBookingOptions: () => rpc("kamra.api.booking_options", {}),
  getQuote: (params: unknown) => rpc("kamra.api.get_quote", params),
  getCurrentProperty: () => "Test Hotel",
  frappeFetch: async (path: string) => ({ data: path.includes("?") ? [] : {} }),
  serverError: (error: Error) => error.message,
}))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: ["Front Desk"] }) }))

const nights = [
  { date: "2026-12-30", nightly: 100, agreed_rate: 365000, item_price: "IP-2026" },
  { date: "2026-12-31", nightly: 100, agreed_rate: 365000, item_price: "IP-2026" },
  { date: "2027-01-01", nightly: 200, agreed_rate: 730000, item_price: "IP-2027" },
]
const tariff = { currency: "USD", nightly: 100, property_currency: "UGX", fx_rate: 3650, fx_date: "2026-12-20", nights }
const night = (date: string, rate: number) =>
  ({ date, bar: rate, after_season: rate, rate, season: null, demand: null, occupancy: 0 })
const options = (companyPlan: string | null) => ({
  nights: companyPlan
    ? nights.map((n) => night(n.date, n.agreed_rate))
    : nights.map((n) => night(n.date, 100000)),
  plain: { room_total: 300000, amount_after_tax: 300000 },
  plans: [
    { name: "BAR", label: "Best available", code: "BAR", room_total: 300000, vs_plain: 0, available: true, tariff: null,
      fx_lock_policy: "Fixed at Reservation", derived_from: null },
    { name: "CORP", label: "Corporate", code: "CORP", room_total: 1460000, vs_plain: 1160000, available: true, tariff,
      agreed_rate: 365000, fx_lock_policy: "Fixed at Check In", derived_from: "Best available" },
  ],
  voucher: null, company_plan: companyPlan, default_plan: null, tax_inclusive: false,
})

describe("Rate Options", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    rpc.mockReset().mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === "kamra.rates.rate_options") return options(params.company ? "CORP" : null)
      if (method === "kamra.api.property_locale") return { locale: "en", currency: "UGX", payment_modes: ["Cash"] }
      if (method === "kamra.api.booking_options") return {
        room_types: [{ name: "Test Hotel-DLX", room_type_name: "Deluxe", base_price: 100000, adults_capacity: 2,
          children_capacity: 0 }],
        meal_plans: [], rate_plans: [{ name: "BAR", rate_plan_name: "Best available", code: "BAR" },
          { name: "CORP", rate_plan_name: "Corporate", code: "CORP" }],
        companies: [{ name: "Client Co", company_name: "Client Co", negotiated_rate_plan: "CORP" }],
        travel_agents: [], experiences: [], property: { country: "Uganda" }, ota_channels: [], can_set_rate: false,
      }
      if (method === "kamra.api.get_quote") return { nights: 3, nightly: [], room_total: 300000, meal_total: 0,
        amount_before_tax: 300000, tax_amount: 0, amount_after_tax: 300000, discount: 0, currency: "UGX" }
      return []
    })
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

  const optionCalls = () => rpc.mock.calls.filter(([method]) => method === "kamra.rates.rate_options")
    .map(([, params]) => params as Record<string, unknown>)
  const button = (label: string) => Array.from(host.querySelectorAll("button"))
    .find((node) => node.textContent?.includes(label))

  async function render(company?: string) {
    const props = { roomType: "Test Hotel-DLX", checkIn: "2026-12-30", checkOut: "2027-01-02", adults: 2, children: 0,
      ratePlan: "", onPickPlan: () => {}, nightPrices: {}, onNightPrices: () => {}, canSetPrice: false }
    await act(async () => root.render(<RateOptions {...props} {...(company ? { company } : {})} />))
    await vi.waitFor(async () => {
      await act(() => new Promise((resolve) => setTimeout(resolve, 20)))
      expect(host.textContent).toContain("Rate plans")
    }, { interval: 5, timeout: 5000 })
  }

  it("asks for the company's own rates and shows its plan as the one pricing the stay", async () => {
    await render("Client Co")
    expect(optionCalls().at(-1)).toMatchObject({ property: "Test Hotel", room_type: "Test Hotel-DLX", company: "Client Co" })
    // with no plan picked the company's negotiated plan prices the stay, as the quote below does
    expect(button("Corporate")?.getAttribute("aria-pressed")).toBe("true")
    expect(button("Standard rate")).toBeUndefined()
  })

  it("names each plan's exchange rate lock and the plan it derives from", async () => {
    await render()
    expect(optionCalls().at(-1)).not.toHaveProperty("company")
    expect(button("Standard rate")).toBeDefined()
    const corporate = button("Corporate")!.textContent ?? ""
    expect(corporate).toContain("Fixed at Check In")
    expect(corporate).toContain("Derived from Best available")
    // a home-currency plan has nothing to convert, so its lock is not shown
    expect(button("Best available")!.textContent).not.toContain("Fixed at Reservation")
  })

  it("passes the booking's company from the booking dialog", async () => {
    await act(async () => root.render(<MemoryRouter><BookingDialog initial={{ room_type: "Test Hotel-DLX",
      date: "2026-12-30", stays: 3 }} onClose={() => {}} onBooked={() => {}} /></MemoryRouter>))
    await vi.waitFor(async () => {
      await act(() => new Promise((resolve) => setTimeout(resolve, 20)))
      expect(optionCalls().length).toBeGreaterThan(0)
    }, { interval: 20, timeout: 15000 })
    await act(async () => button("Company, add-ons & arrival details")!.click())
    const company = Array.from(host.querySelectorAll("select"))
      .find((node) => Array.from(node.options).some((option) => option.value === "Client Co"))!
    await act(async () => {
      company.value = "Client Co"
      company.dispatchEvent(new Event("change", { bubbles: true }))
    })
    await vi.waitFor(async () => {
      await act(() => new Promise((resolve) => setTimeout(resolve, 20)))
      expect(optionCalls().at(-1)).toMatchObject({ company: "Client Co" })
    }, { interval: 20, timeout: 15000 })
  }, 40000)
})
