// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { MemoryRouter } from "react-router-dom"
import { BookingDialog } from "../src/components/BookingDialog"
import { ResourceScreen } from "../src/components/ResourceScreen"
import RowCorrection from "../src/components/RowCorrection"
import { tenderModes } from "../src/components/TenderPicker"
import CashierBoard from "../src/screens/CashierBoard"
import CashierShiftReport from "../src/screens/CashierShiftReport"
import CashierTill from "../src/screens/CashierTill"
import { paymentMethodsConfig } from "../src/screens/configs"
import ar from "../src/i18n/locales/ar.json"
import de from "../src/i18n/locales/de.json"
import es from "../src/i18n/locales/es.json"
import fil from "../src/i18n/locales/fil.json"
import id from "../src/i18n/locales/id.json"
import pt from "../src/i18n/locales/pt.json"
import zhCN from "../src/i18n/locales/zh-CN.json"

const { calls, fetches, state } = vi.hoisted(() => ({
  calls: vi.fn(),
  fetches: vi.fn(),
  state: {
    pmRows: [] as Record<string, unknown>[],
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
vi.mock("../src/lib/cashierAuth", () => ({
  useCashierAuth: () => ({
    ensureUnlocked: async () => {},
    withPin: async (fn: (pin: string) => Promise<void>) => fn("1234"),
  }),
}))

const BASE_LOCALE = {
  currency: "UGX",
  currency_symbol: "UGX ",
  locale: "en-UG",
  tax_label: "VAT",
  tax_id_label: "TIN",
  tax_rates: [0, 18],
  id_types: ["National ID", "Passport"],
  id_type_hints: {},
  payment_modes: ["Cash", "Card", "Bank Transfer", "Mobile Money", "USD Box", "USD Wire", "Front Desk Till"],
  payment_tender_types: {
    Cash: "Cash",
    "Foreign Cash": "Foreign Cash",
    Card: "Card",
    "Bank Transfer": "Bank Transfer",
    "Mobile Money": "Instant",
    "USD Box": "Foreign Cash",
    "USD Wire": "Bank Transfer",
    "Front Desk Till": "Cash",
  },
  instant_payment_modes: ["Mobile Money"],
  outlet_payment_modes: ["Cash", "Card", "Mobile Money"],
  fx_currencies: ["USD", "EUR"],
  default_nationality: "Ugandan",
  adult_age: 18,
  minor_ids: "foreign",
  integrations: { payment_gateways: [], tax_return: false, verify_tax_id: false, verify_guest_id: false },
  market_codes: [{ value: "Corporate", label: "Corporate" }],
  booking_types: [
    { value: "Individual", label: "Individual" },
    { value: "Group", label: "Group" },
    { value: "Corporate", label: "Corporate" },
    { value: "Staycation", label: "Staycation" },
  ],
  booker_relations: [{ value: "Assistant", label: "Assistant" }],
  cancellation_reasons: [{ value: "Other", label: "Other" }],
}

const FOREIGN_DRAWERS = [
  {
    currency: "USD",
    receipts_fx: 15,
    refunds_fx: 0,
    expected_fx: 15,
    home_equivalent: 55500,
    counted_fx: 14,
    variance_fx: -1,
    txns: 2,
  },
  {
    currency: "EUR",
    receipts_fx: 20,
    refunds_fx: 0,
    expected_fx: 20,
    home_equivalent: 80000,
    counted_fx: 20,
    variance_fx: 0,
    txns: 1,
  },
]

describe("PR #47 R2 OPERA/SAP cashier, FX reclassification, permissions and master UX", { timeout: 25000 }, () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    localStorage.clear()
    state.pmRows = [
      { name: "Cash", code: "Cash", description: "Cash", tender_type: "Cash", mode_of_payment: "", property: "", system: 1, disabled: 0 },
      { name: "Mobile Money", code: "Mobile Money", description: "Mobile Money", tender_type: "Instant", mode_of_payment: "", property: "", system: 1, disabled: 0 },
      { name: "Shared Voucher", code: "Shared Voucher", description: "Shared Voucher", tender_type: "Other", mode_of_payment: "", property: "", system: 0, disabled: 0 },
      { name: "Kampala Hotel::Airtel Money", code: "Airtel Money", description: "Airtel Money", tender_type: "Instant", mode_of_payment: "Airtel KLA", property: "Kampala Hotel", system: 0, disabled: 0 },
    ]

    calls.mockReset().mockImplementation(async (method: string) => {
      if (method === "frappe.client.has_permission") return { has_permission: true }
      if (method === "kamra.api.property_locale") return BASE_LOCALE
      if (method === "kamra.api.payment_correction_options") {
        return {
          actions: [{
            id: "reclassify_tender",
            label: "Correct payment mode",
            requires: [],
            reason_codes: [{ name: "REV-ACCOUNT", description: "Wrong tender", requires_supervisor: 0 }],
          }],
          blocked: [],
          supervisors: [],
          refund_cap: 0,
        }
      }
      if (method === "kamra.api.booking_options") {
        return {
          room_types: [
            { name: "STD", room_type_name: "Standard", base_price: 250000, adults_capacity: 2, children_capacity: 1 },
            { name: "DLX", room_type_name: "Deluxe", base_price: 350000, adults_capacity: 2, children_capacity: 1 },
          ],
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
          nights: 1, nightly: [{ date: "2026-11-25", rate: 250000 }],
          room_total: 250000, meal_total: 0, discount: 0,
          amount_before_tax: 250000, tax_percent: 0, tax_amount: 0, amount_after_tax: 250000,
        }
      }
      if (method === "kamra.api.rate_options") return { nights: [], plans: [], plain: null, voucher: null }
      if (method === "kamra.api.guest_alert") return {}
      if (method === "kamra.cashier.my_till") return { required: false, open: true }
      if (method === "kamra.ledger.list_exchange_rates") return { business_date: "2026-11-24", rates: [] }
      if (method === "kamra.api.create_group_booking") {
        return { group_booking: "GRP-2026-0001", created: ["RES-1", "RES-2"], skipped: [] }
      }
      if (method === "kamra.cashier.current_session") {
        return {
          open: true,
          name: "CS-1",
          cashier_id: "CSH-1",
          status: "Open",
          business_date: "2026-11-24",
          opening_float: 100000,
          system_cash: 50000,
          system_card: 0,
          system_upi: 0,
          paid_outs: 0,
          drops: 0,
          petty_cash: 0,
          expected_cash: 150000,
          totals: { cash: 50000, card: 0, upi: 0, other: 0, foreign_cash: 135500, expected_cash: 150000 },
          foreign_drawers: FOREIGN_DRAWERS.map((d) => ({ ...d, counted_fx: null, variance_fx: null })),
          transactions: [
            { name: "TX-1", kind: "Payment", mode: "Front Desk Till", amount: 37000, currency: "USD", fx_amount: 10, reference: "F-1", posted_at: "2026-11-24 10:15:00" },
          ],
        }
      }
      if (method === "kamra.cashier.close_session") return { ok: true }
      if (method === "kamra.cashier.terminals") return []
      if (method === "kamra.printing.printers") return []
      if (method === "kamra.pos.outlets") return []
      if (method === "kamra.cashier.shift_report") {
        return {
          business_date: "2026-11-24",
          sessions: [{
            name: "CS-1", cashier_id: "CSH-1", user: "fd@example.test", status: "Closed",
            system_cash: 50000, system_card: 0, system_upi: 0, variance: 0,
            foreign_drawers: FOREIGN_DRAWERS,
          }],
          journal: [],
        }
      }
      if (method === "kamra.cashier.session_report") {
        return {
          name: "CS-1",
          cashier_id: "CSH-1",
          status: "Closed",
          expected_cash: 150000,
          counted_cash: 150000,
          variance: 0,
          totals: {
            opening_float: 100000, cash: 50000, card: 0, upi: 0, other: 0,
            foreign_cash: 135500, paid_outs: 0, drops: 0, petty_cash: 0, expected_cash: 150000,
          },
          foreign_drawers: FOREIGN_DRAWERS,
          by_method: [
            { mode: "Cash", receipts: 50000, refunds: 0, net: 50000, system: 150000, counted: 150000, variance: 0 },
            { mode: "USD Box", receipts: 18500, refunds: 0, net: 18500, system: 18500, counted: null, variance: null },
          ],
        }
      }
      if (method === "kamra.cashier.tills_board") {
        return {
          business_date: "2026-11-24",
          tills: [{
            name: null, terminal_name: "Default", kind: "Front Desk", outlet: null, bill_printer: null, is_default: true,
            sessions: [{
              name: "CS-OPEN", user: "fd@example.test", cashier_id: "CSH-1", opened_at: "2026-11-24 08:00:00",
              expected_cash: 150000,
              foreign_drawers: FOREIGN_DRAWERS.map((d) => ({ ...d, counted_fx: null, variance_fx: null })),
            }],
          }],
          pending_transfers: [],
          variance_review: [{
            name: "CS-1", user: "fd@example.test", variance: 0, business_date: "2026-11-24",
            foreign_drawers: FOREIGN_DRAWERS,
          }],
        }
      }
      return {}
    })

    fetches.mockReset().mockImplementation(async (path: string, init?: RequestInit) => {
      if (init?.method === "POST") return { data: { name: "Created", ...JSON.parse(String(init.body)) } }
      if (init?.method === "PUT") return { data: { ...JSON.parse(String(init.body)) } }
      if (path.startsWith("/api/resource/Payment%20Method?")) return { data: state.pmRows }
      if (path.startsWith("/api/resource/Payment%20Method/")) {
        const docName = decodeURIComponent(path.slice("/api/resource/Payment%20Method/".length))
        const hit = state.pmRows.find((r) => r.name === docName) ?? {}
        return { data: hit }
      }
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
  const tick = () => new Promise((r) => setTimeout(r, 15))

  it("P1 #1: RowCorrection gates FX destinations by tender_type so custom USD Box and USD Wire appear", async () => {
    const modes = tenderModes(BASE_LOCALE, "UGX", false)
    await act(async () => root.render(
      <RowCorrection
        folio="FOLIO-1"
        row="PAY-1"
        home="UGX"
        currency="USD"
        modes={modes}
        act={async (fn) => { await fn() }}
      />,
    ))
    await settle()

    const openBtn = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.includes("Correct")) as HTMLButtonElement
    await act(async () => openBtn.click())
    await vi.waitFor(async () => {
      await tick()
      expect(host.textContent).toContain("Destination tender")
    }, { interval: 25, timeout: 5000 })

    const modeButtons = Array.from(host.querySelectorAll('[role="group"][aria-label="Payment mode"] button')).map((b) => b.textContent?.trim())
    expect(modeButtons).toContain("USD Box")
    expect(modeButtons).toContain("USD Wire")

    // Selecting USD Box keeps currency fixed to USD (never UGX)
    const usdBoxBtn = Array.from(host.querySelectorAll('[role="group"][aria-label="Payment mode"] button'))
      .find((b) => b.textContent?.trim() === "USD Box") as HTMLButtonElement
    await act(async () => usdBoxBtn.click())
    await settle()
    const currencyButtons = Array.from(host.querySelectorAll('[role="group"][aria-label="Tender currency"] button')).map((b) => b.textContent?.trim())
    expect(currencyButtons).toEqual(["USD"])

    // Domestic payment excludes Foreign Cash tenders like USD Box
    await act(async () => root.render(
      <RowCorrection
        key="domestic"
        folio="FOLIO-1"
        row="PAY-2"
        home="UGX"
        currency="UGX"
        modes={modes}
        act={async (fn) => { await fn() }}
      />,
    ))
    await settle()
    const openBtn2 = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.includes("Correct")) as HTMLButtonElement
    await act(async () => openBtn2.click())
    await vi.waitFor(async () => {
      await tick()
      expect(host.textContent).toContain("Destination tender")
    }, { interval: 25, timeout: 5000 })
    const domButtons = Array.from(host.querySelectorAll('[role="group"][aria-label="Payment mode"] button')).map((b) => b.textContent?.trim())
    expect(domButtons).not.toContain("USD Box")
  })

  it("P2 #5: tenderModes reflects Cashier.allowed_modes in standalone and bridged configurations", () => {
    const standalone = tenderModes({
      ...BASE_LOCALE,
      tender_modes: null,
      cashier_allowed_modes: ["Card", "USD Wire"],
    }, "UGX", true)
    expect(standalone.map((m) => m.mode)).toEqual(["Card", "USD Wire"])

    const bridged = tenderModes({
      ...BASE_LOCALE,
      tender_modes: [
        { mode: "Cash", currencies: ["UGX", "USD"] },
        { mode: "Card", currencies: ["UGX"] },
        { mode: "USD Wire", currencies: ["UGX", "USD"] },
      ],
      cashier_allowed_modes: ["USD Wire"],
    }, "UGX", true)
    expect(bridged.map((m) => m.mode)).toEqual(["USD Wire"])
    expect(bridged[0].currencies).toEqual(["UGX", "USD"])
  })

  it("P2 #6: BookingDialog passes selected booking_type to create_group_booking", async () => {
    await act(async () => root.render(
      <MemoryRouter>
        <BookingDialog initial={{ room_type: "STD", guest_name: "Group Lead", phone: "+256772111222" }} onClose={() => {}} onBooked={() => {}} />
      </MemoryRouter>,
    ))
    await vi.waitFor(async () => {
      await tick()
      expect(host.textContent).toContain("Add another room")
    }, { interval: 25, timeout: 5000 })

    // Add a second room so BookingDialog submits via create_group_booking
    const addRoomBtn = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.includes("Add another room")) as HTMLButtonElement
    await act(async () => addRoomBtn.click())
    await settle()

    const moreBtn = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.includes("Company, add-ons & arrival details")) as HTMLButtonElement
    await act(async () => moreBtn.click())
    await settle()

    const btSelect = Array.from(host.querySelectorAll("label"))
      .find((l) => l.textContent?.includes("Booking type"))?.querySelector("select") as HTMLSelectElement
    expect(btSelect).toBeTruthy()
    await act(async () => {
      btSelect.value = "Staycation"
      btSelect.dispatchEvent(new Event("change", { bubbles: true }))
    })
    await settle()

    const confirmBtn = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.includes("Confirm")) as HTMLButtonElement
    await vi.waitFor(async () => {
      await tick()
      expect(confirmBtn.disabled).toBe(false)
    }, { interval: 25, timeout: 5000 })
    await act(async () => confirmBtn.click())
    await vi.waitFor(async () => {
      await tick()
      expect(calls).toHaveBeenCalledWith(
        "kamra.api.create_group_booking",
        expect.objectContaining({ booking_type: "Staycation" }),
      )
    }, { interval: 25, timeout: 5000 })
  })

  it("P2 #7: Payment Methods searches across all rows (>50) and edits Shared rows via property override (never silent global PUT)", async () => {
    const many: Record<string, unknown>[] = [...state.pmRows]
    for (let i = 1; i <= 55; i++) {
      many.push({
        name: `Kampala Hotel::PM-${String(i).padStart(2, "0")}`,
        code: `PM-${String(i).padStart(2, "0")}`,
        description: i === 55 ? "Zebra Pay Method" : `Method ${i}`,
        tender_type: "Instant",
        mode_of_payment: "",
        property: "Kampala Hotel",
        system: 0,
        disabled: 0,
      })
    }
    // Also include a Shared row that ALREADY has a Kampala Hotel override
    many.push(
      { name: "Shared Dup", code: "Shared Dup", description: "Shared Dup", tender_type: "Other", mode_of_payment: "", property: "", system: 0, disabled: 0 },
      { name: "Kampala Hotel::Shared Dup", code: "Shared Dup", description: "Kampala Dup", tender_type: "Other", mode_of_payment: "", property: "Kampala Hotel", system: 0, disabled: 0 },
    )
    state.pmRows = many

    await act(async () => root.render(
      <MemoryRouter initialEntries={["/settings/payment-methods"]}>
        <ResourceScreen config={paymentMethodsConfig} />
      </MemoryRouter>,
    ))
    await vi.waitFor(async () => {
      await tick()
      expect(host.textContent).toContain("PM-01")
    }, { interval: 25, timeout: 5000 })

    // Fetch used limit > 50 so search spans all rows
    const listUrl = String(fetches.mock.calls.find(([p]) => String(p).startsWith("/api/resource/Payment%20Method?"))?.[0] ?? "")
    expect(listUrl).toContain("limit_page_length=2000")

    // Search for row #55 beyond the first 50 rows
    const searchInput = host.querySelector('input[placeholder*="Search"]') as HTMLInputElement
    expect(searchInput).not.toBeNull()
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
      setter.call(searchInput, "Zebra")
      searchInput.dispatchEvent(new Event("input", { bubbles: true }))
      searchInput.dispatchEvent(new Event("change", { bubbles: true }))
    })
    await vi.waitFor(async () => {
      await tick()
      expect(host.textContent).toContain("Zebra Pay Method")
    }, { interval: 40, timeout: 5000 })

    // Clear search, click Shared Voucher (no override yet) -> creates property override via POST, never PUT
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
      setter.call(searchInput, "")
      searchInput.dispatchEvent(new Event("input", { bubbles: true }))
      searchInput.dispatchEvent(new Event("change", { bubbles: true }))
    })
    await vi.waitFor(async () => {
      await tick()
      expect(host.textContent).toContain("Shared Voucher")
    }, { interval: 40, timeout: 5000 })

    const sharedRow = Array.from(host.querySelectorAll("tbody tr")).find((tr) => tr.textContent?.includes("Shared Voucher")) as HTMLElement
    await act(async () => sharedRow.click())
    await vi.waitFor(async () => {
      await tick()
      expect(document.body.textContent).toContain("property override")
    }, { interval: 25, timeout: 5000 })
    // Delete button must not be shown on a Shared row
    expect(document.querySelector('button[aria-label="Delete"]')).toBeNull()

    const saveBtn = Array.from(document.querySelectorAll("button")).find((b) => b.textContent?.trim() === "Save") as HTMLButtonElement
    await act(async () => saveBtn.click())
    await settle()
    const postCall = fetches.mock.calls.find(([p, init]) => String(p) === "/api/resource/Payment%20Method" && init?.method === "POST")
    expect(postCall).toBeDefined()
    expect(JSON.parse(String(postCall![1]?.body))).toMatchObject({ code: "Shared Voucher", property: "Kampala Hotel" })
    const badPut = fetches.mock.calls.find(([p, init]) => String(p).includes("Shared%20Voucher") && init?.method === "PUT")
    expect(badPut).toBeUndefined()

    // Search for "Shared Dup" and open the Shared row (where Kampala Hotel override already exists) -> refuses global PUT
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
      setter.call(searchInput, "Shared Dup")
      searchInput.dispatchEvent(new Event("input", { bubbles: true }))
      searchInput.dispatchEvent(new Event("change", { bubbles: true }))
    })
    await vi.waitFor(async () => {
      await tick()
      expect(host.textContent).toContain("Shared Dup")
    }, { interval: 40, timeout: 5000 })

    const sharedDupRow = Array.from(host.querySelectorAll("tbody tr")).find(
      (tr) => tr.textContent?.includes("Shared Dup") && tr.textContent?.includes("Shared") && !tr.textContent?.includes("Kampala Hotel"),
    ) as HTMLElement
    expect(sharedDupRow).toBeDefined()
    await act(async () => sharedDupRow.click())
    await vi.waitFor(async () => {
      await tick()
      expect(document.body.textContent).toContain("already has its own override")
    }, { interval: 25, timeout: 5000 })
    const putDup = fetches.mock.calls.find(([p, init]) => String(p).includes("Shared%20Dup") && init?.method === "PUT")
    expect(putDup).toBeUndefined()
  })

  it("OPERA Cashier Closure: CashierTill, CashierShiftReport and CashierBoard show foreign drawers by currency", async () => {
    // 1. CashierTill shows foreign drawers per currency and submits foreign_counts on close
    await act(async () => root.render(<MemoryRouter><CashierTill /></MemoryRouter>))
    await vi.waitFor(async () => {
      await tick()
      expect(host.textContent).toContain("My Till")
    }, { interval: 25, timeout: 5000 })
    expect(host.textContent).toContain("Foreign currency drawers")
    expect(host.textContent).toContain("USD")
    expect(host.textContent).toContain("15.00")
    expect(host.textContent).toContain("EUR")
    expect(host.textContent).toContain("20.00")

    // Close till with home counted cash and USD foreign count
    const countedInput = Array.from(host.querySelectorAll("label"))
      .find((l) => l.textContent?.includes("Counted cash"))?.querySelector("input") as HTMLInputElement
    const usdCountInput = host.querySelector('input[aria-label*="USD"]') as HTMLInputElement
    expect(usdCountInput).not.toBeNull()
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
      setter.call(countedInput, "150000")
      countedInput.dispatchEvent(new Event("input", { bubbles: true }))
      countedInput.dispatchEvent(new Event("change", { bubbles: true }))
      setter.call(usdCountInput, "14")
      usdCountInput.dispatchEvent(new Event("input", { bubbles: true }))
      usdCountInput.dispatchEvent(new Event("change", { bubbles: true }))
    })
    await settle()
    const closeBtn = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.trim() === "Close session") as HTMLButtonElement
    await act(async () => closeBtn.click())
    await vi.waitFor(async () => {
      await tick()
      expect(calls).toHaveBeenCalledWith(
        "kamra.cashier.close_session",
        expect.objectContaining({
          session: "CS-1",
          counted_cash: 150000,
          foreign_counts: expect.objectContaining({ USD: 14 }),
        }),
      )
    }, { interval: 25, timeout: 5000 })

    // 2. CashierShiftReport (closing / Z report) renders foreign drawers table by currency
    await act(async () => root.render(
      <MemoryRouter initialEntries={["/cashier/shift-report?session=CS-1"]}><CashierShiftReport /></MemoryRouter>,
    ))
    await vi.waitFor(async () => {
      await tick()
      expect(host.textContent).toContain("Till detail")
    }, { interval: 25, timeout: 5000 })
    expect(host.textContent).toContain("Foreign currency drawers")
    expect(host.textContent).toContain("USD")
    expect(host.textContent).toContain("14.00")
    expect(host.textContent).toContain("-1.00")
    expect(host.textContent).toContain("EUR")
    expect(host.textContent).toContain("20.00")

    // 3. CashierBoard (shift variance review screen) renders foreign drawer variances
    await act(async () => root.render(<MemoryRouter><CashierBoard /></MemoryRouter>))
    await vi.waitFor(async () => {
      await tick()
      expect(host.textContent).toContain("Tills to review")
    }, { interval: 25, timeout: 5000 })
    expect(host.textContent).toContain("USD")
    expect(host.textContent).toContain("-1.00")
    expect(host.textContent).not.toMatch(/Kamra/i)
  })

  it.each([
    ["ar", ar], ["de", de], ["es", es], ["fil", fil], ["id", id], ["pt", pt], ["zh-CN", zhCN],
  ])("translates new R2 cashier foreign-drawer and property-override strings in %s", (_lang, dict: Record<string, string>) => {
    for (const key of [
      "Foreign currency drawers",
      "Foreign drawers",
      "Expected (FX)",
      "Counted (FX)",
      "Home equivalent",
      "Edit property override",
    ]) {
      expect(dict[key], key).toBeTruthy()
    }
  })
})
