// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import App from "../src/App"
import { BookingDialog } from "../src/components/BookingDialog"
import { TIMEZONES } from "../src/screens/settings/cards"
import ar from "../src/i18n/locales/ar.json"
import de from "../src/i18n/locales/de.json"
import es from "../src/i18n/locales/es.json"
import fil from "../src/i18n/locales/fil.json"
import id from "../src/i18n/locales/id.json"
import pt from "../src/i18n/locales/pt.json"
import zhCN from "../src/i18n/locales/zh-CN.json"

/**
 * PR #44 R2 frontend contracts:
 * - All 9 configurable masters have SPA Settings screens under Configuration
 *   (list, add, edit, disable; system codes cannot be deleted or disabled).
 * - Payment Methods screen is scoped to the current property + shared system rows.
 * - Uganda settings: Africa/Kampala in TIMEZONES, UGX currency unit on money keys
 *   (room_levy_per_night, na_high_balance_threshold), Cashier limits card on Finance.
 * - Tax plan PR-11: tax_basis.determined === 0 renders the non-blocking notice on
 *   BookingDialog and Settings › Finance; absent determined or determined === 1 hides it.
 * - All new strings translated across all 7 locales; zero "Kamra" leaks.
 */

const TAX_NOTICE = "Sales tax isn't set up for this hotel's company. Finance sets it in Accounting."

const MASTER_PAGES = [
  { id: "market-codes", title: "Market Codes", doctype: "Market Code" },
  { id: "origin-codes", title: "Origin Codes", doctype: "Origin Code" },
  { id: "booking-types", title: "Booking Types", doctype: "Booking Type" },
  { id: "booker-relations", title: "Booker Relations", doctype: "Booker Relation" },
  { id: "cancellation-reasons", title: "Cancellation Reasons", doctype: "Cancellation Reason" },
  { id: "payment-methods", title: "Payment Methods", doctype: "Payment Method" },
  { id: "meal-plan-codes", title: "Meal Plan Codes", doctype: "Meal Plan Code" },
  { id: "payer-types", title: "Payer Types", doctype: "Payer Type" },
  { id: "check-in-requests", title: "Check-in Requests", doctype: "Check-in Request" },
] as const

const { calls, fetches, state } = vi.hoisted(() => ({
  calls: vi.fn(),
  fetches: vi.fn(),
  state: {
    taxBasis: null as { label: string; rate: number; inclusive: number; determined?: number } | null,
    quoteTaxBasis: undefined as { label: string; rate: number; inclusive: number; determined?: number } | null | undefined,
  },
}))

vi.mock("../src/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api")>()
  return {
    ...actual,
    call: (method: string, params?: Record<string, unknown>) => calls(method, params),
    getCurrentProperty: () => "Kampala Hotel",
    frappeFetch: (path: string, init?: RequestInit) => fetches(path, init),
  }
})
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: ["Hotel Admin", "Finance"] }) }))
vi.mock("../src/AppShell", () => ({ default: () => <Outlet /> }))

const SETTINGS_SCHEMA = {
  is_admin: true,
  is_it_admin: true,
  enabled_modules: ["front-desk", "finance"],
  modules: [
    {
      id: "general",
      title: "Property",
      description: "Identity and locale.",
      group: "organisation",
      toggle: false,
      enabled: true,
      editors: ["Hotel Admin"],
      can_edit: true,
      related: [],
      sections: [
        {
          id: "location",
          title: "Location & locale",
          description: "",
          settings: [
            { key: "country", label: "Country", type: "select", default: null, hint: "", store: "property", options_source: "countries" },
            { key: "timezone", label: "Time zone", type: "select", default: null, hint: "", store: "property", options_source: "timezones" },
            { key: "locale", label: "Number format (locale)", type: "select", default: "", hint: "", store: "property", options_source: "locales" },
            { key: "date_format", label: "Date format", type: "select", default: "", hint: "", store: "kv", options: ["dd-mm-yyyy", "yyyy-mm-dd"] },
          ],
        },
      ],
      values: { country: "Uganda", timezone: "Africa/Kampala", locale: "en-UG", date_format: "dd-mm-yyyy" },
    },
    {
      id: "front-desk",
      title: "Front Desk",
      description: "Night audit.",
      group: "modules",
      toggle: false,
      enabled: true,
      editors: ["Hotel Admin"],
      can_edit: true,
      related: [],
      sections: [
        {
          id: "auditchecks",
          title: "Night audit checklist",
          description: "",
          settings: [
            { key: "na_high_balance_threshold", label: "Flag in-house folios owing more than", type: "number", default: null, hint: "", store: "kv" },
          ],
        },
      ],
      values: { na_high_balance_threshold: null },
    },
    {
      id: "finance",
      title: "Finance",
      description: "Room tax, fees and cashier controls.",
      group: "modules",
      toggle: true,
      enabled: true,
      editors: ["Hotel Admin", "Finance"],
      can_edit: true,
      related: [],
      sections: [
        {
          id: "tax",
          title: "Room tax",
          description: "",
          settings: [
            { key: "gst_rate_low", label: "Room tax rate", type: "number", default: 18, hint: "", store: "property", unit: "%" },
            { key: "rates_include_tax", label: "Displayed rates include tax", type: "check", default: 0, hint: "", store: "property" },
          ],
        },
        {
          id: "fees",
          title: "Fees & levies",
          description: "",
          settings: [
            { key: "room_levy_per_night", label: "Room levy per night", type: "number", default: 0, hint: "", store: "property" },
          ],
        },
      ],
      values: { gst_rate_low: 0, rates_include_tax: 1, room_levy_per_night: 2000 },
    },
  ],
}

const MARKET_ROWS = [
  { name: "Corporate", code: "Corporate", description: "Corporate", sort_order: 10, system: 1, disabled: 0 },
  { name: "Safari Operator", code: "Safari Operator", description: "Safari Tour Operator", sort_order: 0, system: 0, disabled: 0 },
]

const CASHIER_ROWS = [
  { name: "CSH-1", cashier_id: "CSH-1", user: "frontdesk@example.test", property: "Kampala Hotel", max_cash_refund: 0, allowed_modes: "", disabled: 0 },
]

describe("PR-44 R2 masters UI, Uganda defaults, cashier limits and PR-11 tax notice", { timeout: 25000 }, () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    localStorage.clear()
    state.taxBasis = null
    state.quoteTaxBasis = undefined

    calls.mockReset().mockImplementation(async (method: string) => {
      if (method === "kamra.api.property_locale") {
        return {
          currency: "UGX",
          currency_symbol: "UGX ",
          locale: "en-UG",
          tax_label: "VAT",
          tax_id_label: "TIN",
          tax_rates: [0, 18],
          id_types: ["National ID", "Passport"],
          id_type_hints: {},
          payment_modes: ["Cash", "Card", "Bank Transfer", "Mobile Money"],
          instant_payment_modes: ["Mobile Money"],
          outlet_payment_modes: ["Cash", "Card", "Mobile Money"],
          default_nationality: "Ugandan",
          adult_age: 18,
          minor_ids: "foreign",
          integrations: { payment_gateways: [], tax_return: false, verify_tax_id: false, verify_guest_id: false },
          market_codes: [{ value: "Corporate", label: "Corporate" }],
          booker_relations: [{ value: "Assistant", label: "Assistant" }],
          cancellation_reasons: [{ value: "Other", label: "Other" }],
          tax_basis: state.taxBasis,
        }
      }
      if (method === "kamra.settings.settings_schema") return SETTINGS_SCHEMA
      if (method === "kamra.api.localization_countries") return [{ country: "Uganda" }, { country: "India" }]
      if (method === "kamra.api.booking_options") {
        return {
          room_types: [{ name: "STD", room_type_name: "Standard", base_price: 250000, adults_capacity: 2, children_capacity: 1 }],
          meal_plans: [],
          companies: [],
          rate_plans: [],
          travel_agents: [],
          experiences: [],
          property: { country: "Uganda", currency: "UGX" },
        }
      }
      if (method === "kamra.api.available_rooms") return [{ name: "RM-101", room_number: "101", housekeeping_status: "Clean" }]
      if (method === "kamra.api.get_quote") {
        return {
          nights: 1,
          nightly: [{ date: "2026-11-25", rate: 250000 }],
          room_total: 250000,
          meal_total: 0,
          discount: 0,
          amount_before_tax: 250000,
          tax_percent: 0,
          tax_amount: 0,
          amount_after_tax: 250000,
          tax_basis: state.quoteTaxBasis,
        }
      }
      if (method === "kamra.api.rate_options") return { nights: [], plans: [], plain: null, voucher: null }
      if (method === "kamra.api.guest_alert") return {}
      if (method === "kamra.cashier.my_till") return { required: false, open: true }
      if (method === "kamra.ledger.list_exchange_rates") return { business_date: "2026-11-24", rates: [] }
      return {}
    })

    fetches.mockReset().mockImplementation(async (path: string, init?: RequestInit) => {
      if (init?.method === "POST") return { data: { name: "Created", ...JSON.parse(String(init.body)) } }
      if (init?.method === "PUT") return { data: { ...JSON.parse(String(init.body)) } }
      if (path.startsWith("/api/resource/Market%20Code/Corporate")) return { data: MARKET_ROWS[0] }
      if (path.startsWith("/api/resource/Market%20Code/Safari%20Operator")) return { data: MARKET_ROWS[1] }
      if (path.startsWith("/api/resource/Market%20Code?")) return { data: MARKET_ROWS }
      if (path.startsWith("/api/resource/Payment%20Method?")) {
        return {
          data: [
            { name: "Cash", code: "Cash", description: "Cash", tender_type: "Cash", mode_of_payment: "", property: "", system: 1, disabled: 0 },
            { name: "Airtel Money", code: "Airtel Money", description: "Airtel Money", tender_type: "Instant", mode_of_payment: "Airtel Money", property: "Kampala Hotel", system: 0, disabled: 0 },
          ],
        }
      }
      if (path.startsWith("/api/resource/Cashier?")) return { data: CASHIER_ROWS }
      return { data: path.includes("?") ? [] : {} }
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

  const settle = () => act(() => new Promise((r) => setTimeout(r, 15)))

  it("lists all nine masters under Configuration in Settings and finds them in search", async () => {
    await act(async () => root.render(<MemoryRouter initialEntries={["/settings/general"]}><App /></MemoryRouter>))
    await vi.waitFor(async () => {
      await settle()
      expect(host.textContent).toContain("Configuration")
    }, { interval: 5, timeout: 5000 })

    for (const page of MASTER_PAGES) {
      expect(host.textContent, `Missing Configuration nav item: ${page.title}`).toContain(page.title)
    }
    expect(host.textContent).not.toMatch(/Kamra/i)

    const search = host.querySelector('input[aria-label="Search settings"]') as HTMLInputElement
    expect(search).not.toBeNull()
    for (const q of ["Market Code", "Cancellation Reason", "Payment Method", "Origin Code"]) {
      await act(async () => {
        search.focus()
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
        setter.call(search, q)
        search.dispatchEvent(new Event("input", { bubbles: true }))
        search.dispatchEvent(new Event("change", { bubbles: true }))
      })
      await settle()
      expect(host.textContent).not.toContain("No setting matches.")
    }
  })

  it("manages Market Codes in SPA Settings and blocks deleting/disabling system codes", async () => {
    await act(async () => root.render(<MemoryRouter initialEntries={["/settings/market-codes"]}><App /></MemoryRouter>))
    await vi.waitFor(async () => {
      await settle()
      expect(host.textContent).toContain("Safari Tour Operator")
    }, { interval: 5, timeout: 5000 })

    // Open system row ("Corporate"): Delete must NOT be offered
    const corpRow = Array.from(host.querySelectorAll("tbody tr")).find((tr) => tr.textContent?.includes("Corporate")) as HTMLElement
    expect(corpRow).toBeDefined()
    await act(async () => corpRow.click())
    await vi.waitFor(async () => {
      await settle()
      expect(document.body.textContent).toContain("Corporate")
    }, { interval: 5, timeout: 5000 })
    const deleteOnSystem = Array.from(document.querySelectorAll("button")).find((b) => b.getAttribute("aria-label") === "Delete")
    expect(deleteOnSystem).toBeUndefined()

    // Close sheet and open custom row ("Safari Operator"): Delete IS offered
    const cancelBtn = Array.from(document.querySelectorAll("button")).find((b) => b.textContent?.trim() === "Cancel") as HTMLElement
    await act(async () => cancelBtn.click())
    await settle()

    const customRow = Array.from(host.querySelectorAll("tbody tr")).find((tr) => tr.textContent?.includes("Safari Tour Operator")) as HTMLElement
    await act(async () => customRow.click())
    await vi.waitFor(async () => {
      await settle()
      expect(document.querySelector('button[aria-label="Delete"]')).not.toBeNull()
    }, { interval: 5, timeout: 5000 })
    expect(document.body.textContent).not.toMatch(/Kamra/i)
  })

  it("scopes Payment Methods creation and listing to the active property", async () => {
    await act(async () => root.render(<MemoryRouter initialEntries={["/settings/payment-methods"]}><App /></MemoryRouter>))
    await vi.waitFor(async () => {
      await settle()
      expect(host.textContent).toContain("Airtel Money")
    }, { interval: 5, timeout: 5000 })

    const listCall = fetches.mock.calls.find(([p]) => String(p).startsWith("/api/resource/Payment%20Method?"))
    expect(listCall).toBeDefined()
    expect(decodeURIComponent(String(listCall![0]))).toContain("Kampala Hotel")

    const newBtn = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.includes("New")) as HTMLElement
    await act(async () => newBtn.click())
    await settle()
    const inputs = Array.from(document.querySelectorAll("input"))
    const codeInput = inputs.find((i) => i.closest("label")?.textContent?.includes("Code")) as HTMLInputElement
    expect(codeInput).toBeDefined()
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
      setter.call(codeInput, "MTN MoMo")
      codeInput.dispatchEvent(new Event("input", { bubbles: true }))
      codeInput.dispatchEvent(new Event("change", { bubbles: true }))
    })
    const saveBtn = Array.from(document.querySelectorAll("button")).find((b) => b.textContent?.trim() === "Save") as HTMLElement
    await act(async () => saveBtn.click())
    await settle()
    const post = fetches.mock.calls.find(([, init]) => init?.method === "POST")
    expect(post).toBeDefined()
    expect(JSON.parse(String(post![1]?.body)).property).toBe("Kampala Hotel")
  })

  it("includes Africa/Kampala in TIMEZONES and shows UGX on money keys", async () => {
    expect(TIMEZONES).toContain("Africa/Kampala")
    await act(async () => root.render(<MemoryRouter key="fin-money" initialEntries={["/settings/finance"]}><App /></MemoryRouter>))
    await vi.waitFor(async () => {
      await settle()
      expect(host.querySelector("#row-room_levy_per_night")?.textContent).toContain("UGX")
    }, { interval: 5, timeout: 5000 })

    await act(async () => root.render(<MemoryRouter key="fd-money" initialEntries={["/settings/front-desk"]}><App /></MemoryRouter>))
    await vi.waitFor(async () => {
      await settle()
      expect(host.querySelector("#row-na_high_balance_threshold")?.textContent).toContain("UGX")
    }, { interval: 5, timeout: 5000 })
  })

  it("renders Cashier limits card on Settings > Finance and saves a cashier's refund limit", async () => {
    await act(async () => root.render(<MemoryRouter initialEntries={["/settings/finance"]}><App /></MemoryRouter>))
    await vi.waitFor(async () => {
      await settle()
      expect(host.textContent).toContain("Cashier limits")
      expect(host.textContent).toContain("CSH-1")
    }, { interval: 5, timeout: 5000 })

    const limitInput = host.querySelector('input[aria-label*="CSH-1"]') as HTMLInputElement
    expect(limitInput).not.toBeNull()
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
      setter.call(limitInput, "250000")
      limitInput.dispatchEvent(new Event("input", { bubbles: true }))
      limitInput.dispatchEvent(new Event("change", { bubbles: true }))
    })
    const saveBtn = Array.from(host.querySelectorAll("button")).find(
      (b) => b.closest("section, div")?.textContent?.includes("Cashier limits") && b.textContent?.trim() === "Save",
    ) as HTMLElement
    expect(saveBtn).toBeDefined()
    await act(async () => saveBtn.click())
    await settle()
    const put = fetches.mock.calls.find(([p, init]) => String(p).includes("/api/resource/Cashier/CSH-1") && init?.method === "PUT")
    expect(put).toBeDefined()
    expect(JSON.parse(String(put![1]?.body)).max_cash_refund).toBe(250000)
  })

  it("PR-11: shows tax-not-set-up notice on Settings > Finance and BookingDialog only when determined is 0", async () => {
    // 1. determined: 0 -> notice shown on Settings > Finance
    state.taxBasis = { label: "Sales tax not set up", rate: 0, inclusive: 0, determined: 0 }
    await act(async () => root.render(<MemoryRouter key="pr11-fin-0" initialEntries={["/settings/finance"]}><App /></MemoryRouter>))
    await vi.waitFor(async () => {
      await settle()
      expect(host.textContent).toContain(TAX_NOTICE)
    }, { interval: 5, timeout: 5000 })

    // 2. determined: 0 -> notice shown on BookingDialog quote panel, non-blocking
    state.quoteTaxBasis = { label: "Sales tax not set up", rate: 0, inclusive: 0, determined: 0 }
    await act(async () => root.render(
      <MemoryRouter key="pr11-dlg-0">
        <BookingDialog initial={{ room_type: "STD", guest_name: "Grace Nambi", phone: "+256772111222" }} onClose={() => {}} onBooked={() => {}} />
      </MemoryRouter>,
    ))
    await vi.waitFor(async () => {
      await settle()
      expect(host.textContent).toContain(TAX_NOTICE)
    }, { interval: 5, timeout: 5000 })
    const confirmBtn = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.includes("Confirm booking")) as HTMLButtonElement
    expect(confirmBtn).toBeDefined()
    expect(confirmBtn.disabled).toBe(false)

    // 3. older bridge (determined absent) -> notice NOT shown on either screen
    state.taxBasis = { label: "VAT 18%", rate: 18, inclusive: 1 }
    state.quoteTaxBasis = { label: "VAT 18%", rate: 18, inclusive: 1 }
    await act(async () => root.render(<MemoryRouter key="pr11-fin-old" initialEntries={["/settings/finance"]}><App /></MemoryRouter>))
    await vi.waitFor(async () => {
      await settle()
      expect(host.textContent).toContain("Room tax")
    }, { interval: 5, timeout: 5000 })
    expect(host.textContent).not.toContain(TAX_NOTICE)

    await act(async () => root.render(
      <MemoryRouter key="pr11-dlg-old">
        <BookingDialog initial={{ room_type: "STD", guest_name: "Grace Nambi", phone: "+256772111222" }} onClose={() => {}} onBooked={() => {}} />
      </MemoryRouter>,
    ))
    await vi.waitFor(async () => {
      await settle()
      expect(host.textContent).toContain("Includes VAT 18%")
    }, { interval: 5, timeout: 5000 })
    expect(host.textContent).not.toContain(TAX_NOTICE)
  })

  it.each([
    ["ar", ar], ["de", de], ["es", es], ["fil", fil], ["id", id], ["pt", pt], ["zh-CN", zhCN],
  ])("translates Configuration, all 9 masters, Cashier limits and PR-11 notice in %s", (_lang, dict: Record<string, string>) => {
    const required = [
      "Configuration",
      "Cashier limits",
      TAX_NOTICE,
      ...MASTER_PAGES.map((p) => p.title),
    ]
    for (const key of required) {
      expect(dict[key], key).toBeTruthy()
    }
  })
})
