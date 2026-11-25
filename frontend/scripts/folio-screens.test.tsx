// @vitest-environment jsdom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, describe, expect, it, vi } from "vitest"
import BanquetFunction from "../src/screens/BanquetFunction"
import FolioHistory from "../src/screens/FolioHistory"
import FolioView from "../src/screens/FolioView"

const callMock = vi.fn()
const banquetSheetMock = vi.fn()
const banquetCatalogueMock = vi.fn()

vi.mock("../src/lib/api", () => ({
  call: (...args: unknown[]) => callMock(...args),
  getCurrentProperty: () => "PROP-1",
  newIdempotencyKey: () => "idem-1",
  banquet: {
    sheet: (...args: unknown[]) => banquetSheetMock(...args),
    catalogue: (...args: unknown[]) => banquetCatalogueMock(...args),
    linkableGroups: () => Promise.resolve([]),
    functionTasks: () => Promise.resolve({ tasks: [], total: 0, done: 0, by_department: {} }),
  },
}))
vi.mock("../src/lib/resource", () => ({
  serverError: (e: unknown) => (e instanceof Error ? e.message : String(e)),
  listResource: () => Promise.resolve([]),
}))
const authState = { roles: ["Front Desk"] }
vi.mock("../src/lib/auth", () => ({
  useAuth: () => ({ roles: authState.roles }),
}))
vi.mock("../src/lib/cashierAuth", () => ({
  useCashierAuth: () => ({
    status: { required: false, unlocked: true },
    ensureUnlocked: vi.fn(),
    dialog: null,
  }),
}))

function defaultMockLocale() {
  return {
    currency: "UGX",
    currency_symbol: "Sh",
    locale: "en-UG",
    tax_label: "VAT",
    tax_rates: [0],
    payment_modes: ["Cash", "Mobile Money"],
    instant_payment_modes: ["Mobile Money"],
    tender_modes: [{ mode: "Cash", currencies: ["UGX"] }],
    fx_currencies: [],
    sells_alcohol: true,
    fnb_charge_words: [],
    integrations: {
      payment_gateways: [],
      tax_return: false,
      verify_tax_id: false,
      verify_guest_id: false,
    },
  }
}

let mockLoc = defaultMockLocale()

function digitsFor(currency = mockLoc.currency) {
  try {
    if (currency)
      return new Intl.NumberFormat("en", { style: "currency", currency }).resolvedOptions()
        .maximumFractionDigits ?? 2
  } catch {
    /* ignore */
  }
  return 2
}

vi.mock("../src/lib/money", () => ({
  cur: () => mockLoc.currency_symbol,
  locale: () => mockLoc,
  useLocale: () => mockLoc,
  loadLocale: () => Promise.resolve(mockLoc),
  activeMoneyLocale: () => mockLoc.locale,
  moneyLocale: () => mockLoc.locale,
  moneyDigits: (c?: string | null) => digitsFor(c || mockLoc.currency),
  moneyStep: (c?: string | number | null) => {
    const d = typeof c === "number" ? c : digitsFor(c || mockLoc.currency)
    return d <= 0 ? "1" : `0.${"0".repeat(d - 1)}1`
  },
  moneyEps: (c?: string) => 0.5 * 10 ** -digitsFor(c ?? mockLoc.currency),
  roundMoney: (n: number, c?: string) => {
    if (!Number.isFinite(n) || n === 0) return 0
    const factor = 10 ** digitsFor(c ?? mockLoc.currency)
    const sign = n < 0 ? -1 : 1
    return (sign * Math.round((Math.abs(n) + Number.EPSILON * Math.max(1, Math.abs(n))) * factor)) / factor
  },
  fmtAmount: (n: unknown, c?: string, l?: string) => {
    const d = digitsFor(c ?? mockLoc.currency)
    return Number(n ?? 0).toLocaleString(l || mockLoc.locale, { minimumFractionDigits: d, maximumFractionDigits: d })
  },
  fmtMoney: (n: unknown, ctx?: { currency?: string | null; currency_symbol?: string | null; locale?: string | null } | null) => {
    const currency = ctx?.currency || mockLoc.currency
    const symbol =
      ctx && ctx.currency_symbol != null
        ? ctx.currency_symbol
        : ctx?.currency && ctx.currency !== mockLoc.currency
          ? `${ctx.currency} `
          : mockLoc.currency_symbol
    const numLocale = ctx?.locale || (ctx?.currency && ctx.currency !== mockLoc.currency ? "en-US" : mockLoc.locale)
    const d = digitsFor(currency)
    return symbol + Number(n ?? 0).toLocaleString(numLocale, { minimumFractionDigits: d, maximumFractionDigits: d })
  },
  chargesTax: () => !(mockLoc.tax_rates.length === 1 && Number(mockLoc.tax_rates[0]) === 0),
  sellsAlcohol: () => true,
  taxRates: () => mockLoc.tax_rates,
  taxLabel: () => mockLoc.tax_label,
}))
vi.mock("../src/lib/i18n", () => {
  const t = (s: unknown, vars?: Record<string, unknown>) =>
    typeof s === "string"
      ? s.replace(/\{(\w+)\}/g, (_m, k: string) => String(vars?.[k] ?? `{${k}}`))
      : s
  const tn = (n: number, one: string, many?: string, vars?: Record<string, unknown>) =>
    t(n === 1 ? one : (many ?? one), { n, ...vars })
  return {
    t,
    tn,
    useT: () => ({ lang: "en", t, tn }),
    useUILanguage: () => "en",
    uiDateLocale: () => "en-US",
  }
})

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

afterEach(() => {
  authState.roles = ["Front Desk"]
  mockLoc = defaultMockLocale()
  callMock.mockReset()
  banquetSheetMock.mockReset()
  banquetCatalogueMock.mockReset()
  document.body.innerHTML = ""
})

async function flush() {
  for (let i = 0; i < 4; i++) {
    await act(async () => {
      await Promise.resolve()
    })
  }
}

describe("FolioView error state", () => {
  it("shows the load error instead of hanging on Loading… when folio_invoice rejects", async () => {
    callMock.mockImplementation((method: string) => {
      if (method === "kamra.api.folio_invoice") return Promise.reject(new Error("Fiscal bridge failed"))
      return Promise.resolve([])
    })
    const host = document.createElement("div")
    document.body.appendChild(host)
    const root = createRoot(host)
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/billing/FOLIO-ERR"]}>
          <Routes>
            <Route path="/billing/:name" element={<FolioView />} />
          </Routes>
        </MemoryRouter>,
      )
    })
    await flush()
    expect(host.textContent).toContain("Fiscal bridge failed")
    expect(host.textContent).not.toContain("Loading…")
    await act(async () => root.unmount())
  })
})

describe("FolioHistory event folio reference", () => {
  it("renders venue_booking when an event Account folio has no reservation", async () => {
    callMock.mockImplementation((method: string) => {
      if (method === "kamra.ledger.folio_history") {
        return Promise.resolve({
          folios: [
            {
              name: "FOLIO-EVT-1",
              guest_name: "Acme Gala",
              reservation: "",
              venue_booking: "EVT-2026-0042",
              status: "Closed",
              invoice_number: "ACC-SINV-2026-0001",
              grand_total: 500000,
              balance: 0,
              reprints: 0,
            },
          ],
        })
      }
      return Promise.resolve([])
    })
    const host = document.createElement("div")
    document.body.appendChild(host)
    const root = createRoot(host)
    await act(async () => {
      root.render(
        <MemoryRouter>
          <FolioHistory />
        </MemoryRouter>,
      )
    })
    await flush()
    expect(host.textContent).toContain("Acme Gala")
    expect(host.textContent).toContain("EVT-2026-0042")
    await act(async () => root.unmount())
  })

  it("prefers sales_invoice over internal invoice_number on bridge sites", async () => {
    callMock.mockImplementation((method: string) => {
      if (method === "kamra.ledger.folio_history") {
        return Promise.resolve({
          folios: [
            {
              name: "FOLIO-EVT-2",
              guest_name: "Summit Corp",
              reservation: "",
              venue_booking: "EVT-2026-0099",
              status: "Closed",
              invoice_number: "INV-UTA-26-0099",
              sales_invoice: "ACC-SINV-2026-0099",
              grand_total: 900000,
              balance: 0,
              reprints: 0,
            },
          ],
        })
      }
      return Promise.resolve([])
    })
    const host = document.createElement("div")
    document.body.appendChild(host)
    const root = createRoot(host)
    await act(async () => {
      root.render(
        <MemoryRouter>
          <FolioHistory />
        </MemoryRouter>,
      )
    })
    await flush()
    expect(host.textContent).toContain("ACC-SINV-2026-0099")
    expect(host.textContent).not.toContain("INV-UTA-26-0099")
    await act(async () => root.unmount())
  })
})

describe("FolioView bridge reprint & correction labels", () => {
  function bridgePayload(overrides: Record<string, unknown> = {}) {
    return {
      folio: {
        name: "FOLIO-BR-1", property: "PROP-1", status: "Closed", folio_type: "Guest",
        invoice_number: "INV-UTA-26-0001", sales_invoice: "ACC-SINV-2026-0001",
        guest_name: "Ada Guest",
        charges: [{ name: "c1", posting_date: "2026-11-23", charge_type: "Room", description: "Night 1",
          amount: 500000, gst_rate: 0, gst_amount: 0, total: 500000 }],
        payments: [], charges_total: 500000, tax_total: 0, grand_total: 500000,
        payments_total: 500000, balance: 0,
      },
      property: { currency: "UGX", name: "Test Hotel", legal_name: null, logo_url: null, address: "",
        state: null, gstin: null, phone: null, email: null, sac: null, place_of_supply: null },
      stay: { reservation: "RES-1", check_in: "2026-11-22", check_out: "2026-11-23", arrival: null,
        departure: null, nights: 1, adults: 1, children: 0, pax: 1, meal_plan: null, room_type: "Std",
        room: "101", company: null, group_booking: null, booked_by_name: null, booked_by_phone: null,
        contact_preference: null },
      gst_summary: [], bill_to: null, statutory: null,
      fiscal: {
        state: "not_efris", sales_invoice: "ACC-SINV-2026-0001",
        fdn: null, verification_code: null, qr: null, issued: null, sandbox: false,
        efris_status: null, charges_tax: 0, efris_enrolled: 0,
      },
      bridge_credit_and_rebill: false,
      document: { title: "Invoice", is_final: true, number: "ACC-SINV-2026-0001",
        internal_number: "INV-UTA-26-0001", date: "2026-11-23", amount_in_words: "", footer: null,
        service_code_label: "SAC" },
      lines: [], guest: { name: "Ada Guest", phone: null, email: null, nationality: null, address: "", id_type: null },
      summary_by_head: [], tax_summary: [],
      ...overrides,
    }
  }

  it("labels non-EFRIS final bridge invoice 'Reprint invoice' and hides Credit note / Cancel invoice when credit_and_rebill is absent", async () => {
    authState.roles = ["Finance", "Hotel Admin"]
    const payload = bridgePayload()
    callMock.mockImplementation((method: string) => {
      if (method === "kamra.api.folio_invoice") return Promise.resolve(payload)
      if (method === "kamra.api.linked_records") return Promise.resolve({ folios: [], stays: [], guests: [], rooms: [] })
      return Promise.resolve([])
    })
    const host = document.createElement("div")
    document.body.appendChild(host)
    const root = createRoot(host)
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/billing/FOLIO-BR-1"]}>
          <Routes><Route path="/billing/:name" element={<FolioView />} /></Routes>
        </MemoryRouter>,
      )
    })
    await flush()
    const labels = [...host.querySelectorAll("button")].map((b) => b.textContent?.trim())
    expect(labels).toContain("Reprint invoice")
    expect(labels).not.toContain("Reprint fiscal invoice")
    expect(labels).not.toContain("Credit note")
    expect(labels).not.toContain("Cancel invoice")
    await act(async () => root.unmount())
  })

  it("keeps 'Reprint fiscal invoice' for fiscalised invoices and shows Credit & Rebill when credit_and_rebill is installed", async () => {
    authState.roles = ["Finance"]
    const payload = bridgePayload({
      bridge_credit_and_rebill: true,
      fiscal: {
        state: "fiscalised", sales_invoice: "ACC-SINV-2026-0001",
        fdn: "322000123456", verification_code: "80123456789012345678",
        qr: "data:image/png;base64,iVBORw0KGgo=", issued: "2026-11-23 10:04:00",
        sandbox: false, efris_status: "Success", charges_tax: 1, efris_enrolled: 1,
      },
    })
    callMock.mockImplementation((method: string) => {
      if (method === "kamra.api.folio_invoice") return Promise.resolve(payload)
      if (method === "kamra.api.linked_records") return Promise.resolve({ folios: [], stays: [], guests: [], rooms: [] })
      return Promise.resolve([])
    })
    const host = document.createElement("div")
    document.body.appendChild(host)
    const root = createRoot(host)
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/billing/FOLIO-BR-1"]}>
          <Routes><Route path="/billing/:name" element={<FolioView />} /></Routes>
        </MemoryRouter>,
      )
    })
    await flush()
    const labels = [...host.querySelectorAll("button")].map((b) => b.textContent?.trim())
    expect(labels).toContain("Reprint fiscal invoice")
    expect(labels.filter((label) => label === "Credit & Rebill")).toHaveLength(1)
    expect(labels).not.toContain("Credit note")
    expect(labels).not.toContain("Cancel invoice")
    await act(async () => root.unmount())
  })
})

describe("Hydrogen R1 #6 & #7 — KWD 3dp currency precision and Banquet mobile receipt wrapping", () => {
  it("renders KWD 3dp precision in FolioView header chip (LinkedRecords) and payment list", async () => {
    mockLoc = {
      ...defaultMockLocale(),
      currency: "KWD",
      currency_symbol: "د.ك",
      locale: "en-US",
      tender_modes: [{ mode: "Cash", currencies: ["KWD"] }],
    }
    const payload = {
      folio: {
        name: "FOLIO-KW-1", property: "PROP-KW", status: "Open", folio_type: "Guest",
        invoice_number: null, sales_invoice: null, guest_name: "Tariq Al-Sabah",
        charges: [{
          name: "c1", posting_date: "2026-11-24", charge_type: "Food & Beverage",
          description: "Evening Mezzeh 3dp", amount: 12.375, gst_rate: 0, gst_amount: 0, total: 12.375,
        }],
        payments: [{
          name: "p1", posting_date: "2026-11-24", payment_kind: "Security Deposit",
          mode: "Cash", amount: 25.375, reference: "ACC-PAY-KW-1", currency: "KWD",
        }],
        charges_total: 12.375, tax_total: 0, grand_total: 12.375, payments_total: 0, balance: 12.375,
      },
      property: {
        currency: "KWD", name: "Kuwait Suites", legal_name: null, logo_url: null, address: "Kuwait City",
        state: null, gstin: null, phone: null, email: null, sac: null, place_of_supply: null,
      },
      stay: {
        reservation: "RES-KW-1", check_in: "2026-11-24", check_out: "2026-11-24", arrival: null,
        departure: null, nights: 0, adults: 1, children: 0, pax: 1, meal_plan: null, room_type: "STE",
        room: "402", company: null, group_booking: null, booked_by_name: null, booked_by_phone: null,
        contact_preference: null,
      },
      gst_summary: [], bill_to: null, statutory: null, fiscal: null,
      document: {
        title: "Provisional Bill", is_final: false, number: "FOLIO-KW-1",
        date: "2026-11-24", amount_in_words: "KWD Twelve point 375 Only", footer: null,
        service_code_label: "SAC",
      },
      lines: [{
        row: "c1", posting_date: "2026-11-24", charge_type: "Food & Beverage",
        description: "Evening Mezzeh 3dp", qty: 1, rate: 12.375, amount: 12.375,
        gst_rate: 0, gst_amount: 0, total: 12.375, service_code: null,
      }],
      guest: { name: "Tariq Al-Sabah", phone: null, email: null, nationality: null, address: "", id_type: null },
      summary_by_head: [], tax_summary: [],
    }
    callMock.mockImplementation((method: string) => {
      if (method === "kamra.api.folio_invoice") return Promise.resolve(payload)
      if (method === "kamra.api.linked_records") {
        return Promise.resolve({
          guest: "G-KW-1",
          guest_name: "Tariq Al-Sabah",
          reservations: [],
          folios: [{ name: "FOLIO-KW-1", folio_type: "Guest", status: "Open", balance: 12.375, invoice_number: null }],
          company: null, group_booking: null, group_name: null, event: null, event_type: null,
        })
      }
      return Promise.resolve([])
    })
    const host = document.createElement("div")
    document.body.appendChild(host)
    const root = createRoot(host)
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/billing/FOLIO-KW-1"]}>
          <Routes><Route path="/billing/:name" element={<FolioView />} /></Routes>
        </MemoryRouter>,
      )
    })
    await flush()
    // Header LinkedRecords chip must show 3 decimals (د.ك12.375), not truncated 0dp (د.ك12).
    const chip = [...host.querySelectorAll("button")].find((b) => b.textContent?.includes("Guest folio"))
    expect(chip?.textContent).toContain("12.375")
    // Payment list must show 3 decimals (KWD 25.375), not 2dp-rounded KWD 25.38.
    expect(host.textContent).toContain("KWD 25.375")
    expect(host.textContent).not.toContain("KWD 25.38")
    await act(async () => root.unmount())
  })

  it("formats FolioHistory totals and balances with property ISO 4217 precision (3dp KWD, 2dp USD, 0dp UGX)", async () => {
    for (const [currency, symbol, total, bal, wantTotal, wantBal] of [
      ["KWD", "د.ك", 12.375, 5.125, "12.375", "5.125"],
      ["USD", "$", 193.33, 17.58, "193.33", "17.58"],
      ["UGX", "Sh", 50400, 9000, "50,400", "9,000"],
    ] as const) {
      mockLoc = {
        ...defaultMockLocale(),
        currency,
        currency_symbol: symbol,
        locale: "en-US",
      }
      callMock.mockImplementation((method: string) => {
        if (method === "kamra.ledger.folio_history") {
          return Promise.resolve({
            folios: [{
              name: `FOLIO-${currency}`, guest_name: "Precision Guest",
              reservation: "RES-1", status: "Closed", sales_invoice: `SI-${currency}`,
              grand_total: total, balance: bal, reprints: 0,
            }],
          })
        }
        return Promise.resolve([])
      })
      const host = document.createElement("div")
      document.body.appendChild(host)
      const root = createRoot(host)
      await act(async () => {
        root.render(
          <MemoryRouter>
            <FolioHistory />
          </MemoryRouter>,
        )
      })
      await flush()
      const cells = [...host.querySelectorAll("tbody td")].map((td) => td.textContent?.trim())
      expect(cells, `${currency} grand_total`).toContain(wantTotal)
      expect(cells, `${currency} balance`).toContain(wantBal)
      await act(async () => root.unmount())
      host.remove()
    }
  })

  it("wraps long Banquet receipt references (city-ledger:...) cleanly without an overflowing table", async () => {
    banquetCatalogueMock.mockResolvedValue({ menus: [], services: [] })
    banquetSheetMock.mockResolvedValue({
      name: "EVT-2026-0626",
      property: "PROP-1",
      status: "Completed",
      customer_name: "Kampala Conference Corp",
      event_name: "FinTech Summit",
      event_type: "Conference",
      venue: "PROP-1-Grand Ballroom",
      event_date: "2026-12-09",
      start_time: "09:00",
      end_time: "17:00",
      session: "Custom Hours",
      attendees: 150,
      pax_guaranteed: 150,
      pax_actual: 150,
      billable_pax: 150,
      grand_total: 2400000,
      subtotal: 2400000,
      taxable_amount: 2400000,
      tax_amount: 0,
      discount_amount: 0,
      service_charge_percent: 0,
      service_charge: 0,
      advance_received: 500000,
      balance_due: 1900000,
      deposit_held: 200000,
      deposit_refunded: 0,
      damage_amount: 0,
      quote_version: 0,
      posted_to_folio: 1,
      folio: "FOLIO-EVT-626",
      fiscal_bridge: 1,
      event_folio: "FOLIO-EVT-626",
      items: [],
      payment_terms: [],
      receipts: [
        {
          name: "r1", receipt_date: "2026-11-24", kind: "Payment", mode: "Company Credit",
          amount: 1900000, reference: "city-ledger:gotj6mvqps", native: 1,
        },
      ],
      revisions: [],
      selections: [],
      venue_detail: {},
      group_detail: null,
      scheduled_total: 0,
      open_item_impact: 0,
      next_actions: [],
      checklist: [],
      open_items: [],
    })
    const host = document.createElement("div")
    document.body.appendChild(host)
    const root = createRoot(host)
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/banquet/EVT-2026-0626"]}>
          <Routes><Route path="/banquet/:name" element={<BanquetFunction />} /></Routes>
        </MemoryRouter>,
      )
    })
    await flush()
    const moneyBtn = [...host.querySelectorAll("button")].find((b) => b.textContent?.includes("Money"))
    expect(moneyBtn).toBeTruthy()
    await act(async () => {
      moneyBtn!.click()
    })
    await flush()
    const refEl = [...host.querySelectorAll("*")].find(
      (el) => el.childElementCount === 0 && el.textContent?.trim() === "city-ledger:gotj6mvqps",
    )
    expect(refEl).toBeTruthy()
    expect(refEl?.closest("table")).toBeNull()
    expect(refEl?.className).toContain("break-all")
    await act(async () => root.unmount())
  })
})

describe("Hydrogen R2 #4 — cross-property multi-currency display and zero-decimal isolation", () => {
  it("renders KWD (3dp, KD) on FolioView when global switcher is UGX (0dp) without leaking UGX", async () => {
    mockLoc = {
      ...defaultMockLocale(),
      currency: "UGX",
      currency_symbol: "UGX ",
      locale: "en-UG",
      tender_modes: [{ mode: "Cash", currencies: ["UGX"] }],
    }
    const payload = {
      folio: {
        name: "FOLIO-KW-CROSS", property: "Sunset Kuwait", status: "Open", folio_type: "Guest",
        invoice_number: null, sales_invoice: null, guest_name: "Tariq Al-Sabah",
        charges: [
          {
            name: "c1", posting_date: "2026-11-24", charge_type: "Room",
            description: "Suite Night", amount: 10.0, gst_rate: 0, gst_amount: 0, total: 10.0,
          },
          {
            name: "c2", posting_date: "2026-11-24", charge_type: "Food & Beverage",
            description: "Evening Mezzeh", amount: 2.375, gst_rate: 0, gst_amount: 0, total: 2.375,
          },
        ],
        payments: [{
          name: "p1", posting_date: "2026-11-24", payment_kind: "Payment",
          mode: "Cash", amount: 12.36, reference: "ACC-PAY-KW-2", currency: "KWD",
        }],
        charges_total: 12.375, tax_total: 0, grand_total: 12.375, payments_total: 12.36, balance: 0.015,
      },
      property: {
        currency: "KWD", currency_symbol: "KD", locale: "en-US",
        name: "Sunset Kuwait", legal_name: null, logo_url: null, address: "Kuwait City",
        state: null, gstin: null, phone: null, email: null, sac: null, place_of_supply: null,
        tax_label: "Tax",
      },
      stay: {
        reservation: "RES-KW-2", check_in: "2026-11-23", check_out: "2026-11-24", arrival: null,
        departure: null, nights: 1, adults: 1, children: 0, pax: 1, meal_plan: null, room_type: "STE",
        room: "402", company: null, group_booking: null, booked_by_name: null, booked_by_phone: null,
        contact_preference: null,
      },
      gst_summary: [], bill_to: null, statutory: null, fiscal: null,
      document: {
        title: "Provisional Bill", is_final: false, number: "FOLIO-KW-CROSS",
        date: "2026-11-24", amount_in_words: "KWD Twelve point 375 Only", footer: null,
        service_code_label: "SAC",
      },
      lines: [
        {
          row: "c1", posting_date: "2026-11-24", charge_type: "Room",
          description: "Suite Night", qty: 1, rate: 10.0, amount: 10.0,
          gst_rate: 0, gst_amount: 0, total: 10.0, service_code: null,
        },
        {
          row: "c2", posting_date: "2026-11-24", charge_type: "Food & Beverage",
          description: "Evening Mezzeh", qty: 1, rate: 2.375, amount: 2.375,
          gst_rate: 0, gst_amount: 0, total: 2.375, service_code: null,
        },
      ],
      guest: { name: "Tariq Al-Sabah", phone: null, email: null, nationality: null, address: "", id_type: null },
      summary_by_head: [
        { head: "Room", amount: 10.0, tax: 0, total: 10.0, lines: 1, service_code: null },
        { head: "Food & Beverage", amount: 2.375, tax: 0, total: 2.375, lines: 1, service_code: null },
      ],
      tax_summary: [],
    }
    callMock.mockImplementation((method: string) => {
      if (method === "kamra.api.folio_invoice") return Promise.resolve(payload)
      if (method === "kamra.api.reservation_folios") {
        return Promise.resolve([
          { name: "FOLIO-KW-CROSS", folio_type: "Guest", status: "Open", balance: 0.015, grand_total: 12.375, payments_total: 12.36 },
          { name: "FOLIO-KW-EXT", folio_type: "Extra", status: "Open", balance: 0, grand_total: 0, payments_total: 0 },
        ])
      }
      if (method === "kamra.api.linked_records") {
        return Promise.resolve({
          guest: "G-KW-2", guest_name: "Tariq Al-Sabah", reservations: [],
          folios: [{ name: "FOLIO-KW-CROSS", folio_type: "Guest", status: "Open", balance: 0.015, invoice_number: null }],
          company: null, group_booking: null, group_name: null, event: null, event_type: null,
        })
      }
      return Promise.resolve([])
    })
    const host = document.createElement("div")
    document.body.appendChild(host)
    const root = createRoot(host)
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/billing/FOLIO-KW-CROSS"]}>
          <Routes><Route path="/billing/:name" element={<FolioView />} /></Routes>
        </MemoryRouter>,
      )
    })
    await flush()
    const text = host.textContent || ""
    expect(text).toContain("Amount KD")
    expect(text).toContain("Total KD")
    expect(text).toContain("Grand total: KD12.375")
    expect(text).toContain("Paid: KD12.360")
    expect(text).toContain("Balance: KD0.015")
    expect(text).toContain("KD10.000")
    expect(text).toContain("KD2.375")
    const linkedChip = [...host.querySelectorAll("button")].find((b) => b.textContent?.includes("Guest folio"))
    expect(linkedChip?.textContent).toContain("KD0.015")
    expect(text).not.toContain("UGX")
    await act(async () => root.unmount())
  })

  it("renders USD (2dp, $) on FolioView when global switcher is UGX (0dp) without leaking UGX", async () => {
    mockLoc = {
      ...defaultMockLocale(),
      currency: "UGX",
      currency_symbol: "UGX ",
      locale: "en-UG",
      tender_modes: [{ mode: "Cash", currencies: ["UGX"] }],
    }
    const payload = {
      folio: {
        name: "FOLIO-USD-CROSS", property: "Sunset Resort", status: "Closed", folio_type: "Guest",
        invoice_number: "INV-SR-26-0001", sales_invoice: "ACC-SINV-USD-1", guest_name: "Jane USD",
        charges: [{
          name: "c1", posting_date: "2026-11-24", charge_type: "Room",
          description: "Ocean View", amount: 200.0, gst_rate: 12.375, gst_amount: 24.75, total: 224.75,
        }],
        payments: [{
          name: "p1", posting_date: "2026-11-24", payment_kind: "Payment",
          mode: "Cash", amount: 224.75, reference: "ACC-PAY-USD-1", currency: "USD",
        }],
        charges_total: 200.0, tax_total: 24.75, grand_total: 224.75, payments_total: 224.75, balance: 0,
      },
      property: {
        currency: "USD", currency_symbol: "$", locale: "en-US",
        name: "Sunset Resort", legal_name: null, logo_url: null, address: "Miami",
        state: null, gstin: null, phone: null, email: null, sac: null, place_of_supply: null,
        tax_label: "VAT",
      },
      stay: {
        reservation: "RES-USD-1", check_in: "2026-11-23", check_out: "2026-11-24", arrival: null,
        departure: null, nights: 1, adults: 2, children: 0, pax: 2, meal_plan: null, room_type: "DLX",
        room: "201", company: null, group_booking: null, booked_by_name: null, booked_by_phone: null,
        contact_preference: null,
      },
      gst_summary: [], bill_to: null, statutory: null, fiscal: null,
      document: {
        title: "Invoice", is_final: true, number: "ACC-SINV-USD-1",
        date: "2026-11-24", amount_in_words: "Dollars Two Hundred Twenty Four and Seventy Five Cents Only",
        footer: null, service_code_label: "SAC",
      },
      lines: [{
        row: "c1", posting_date: "2026-11-24", charge_type: "Room",
        description: "Ocean View", qty: 1, rate: 200.0, amount: 200.0,
        gst_rate: 12.375, gst_amount: 24.75, total: 224.75, service_code: null,
      }],
      guest: { name: "Jane USD", phone: null, email: null, nationality: null, address: "", id_type: null },
      summary_by_head: [],
      tax_summary: [{
        rate: 12.375, taxable: 200.0, total_tax: 24.75,
        parts: [{ label: "VAT", rate: 12.375, amount: 24.75 }],
      }],
    }
    callMock.mockImplementation((method: string) => {
      if (method === "kamra.api.folio_invoice") return Promise.resolve(payload)
      if (method === "kamra.api.linked_records") {
        return Promise.resolve({
          guest: "G-USD-1", guest_name: "Jane USD", reservations: [],
          folios: [{ name: "FOLIO-USD-CROSS", folio_type: "Guest", status: "Closed", balance: 0, invoice_number: "INV-SR-26-0001" }],
          company: null, group_booking: null, group_name: null, event: null, event_type: null,
        })
      }
      return Promise.resolve([])
    })
    const host = document.createElement("div")
    document.body.appendChild(host)
    const root = createRoot(host)
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/billing/FOLIO-USD-CROSS"]}>
          <Routes><Route path="/billing/:name" element={<FolioView />} /></Routes>
        </MemoryRouter>,
      )
    })
    await flush()
    const text = host.textContent || ""
    expect(text).toContain("Amount $")
    expect(text).toContain("VAT $")
    expect(text).toContain("Total $")
    expect(text).toContain("Grand total: $224.75")
    expect(text).toContain("Paid: $224.75")
    expect(text).not.toContain("UGX")
    await act(async () => root.unmount())
  })

  it("renders KWD (3dp, KD) on BanquetFunction when global switcher is UGX (0dp) without truncating to integers", async () => {
    mockLoc = {
      ...defaultMockLocale(),
      currency: "UGX",
      currency_symbol: "UGX ",
      locale: "en-UG",
      tender_modes: [{ mode: "Cash", currencies: ["UGX"] }],
    }
    banquetCatalogueMock.mockResolvedValue({ menus: [], services: [] })
    banquetSheetMock.mockResolvedValue({
      name: "EVT-2026-0609",
      property: "Sunset Kuwait",
      currency: "KWD",
      currency_symbol: "KD",
      locale: "en-US",
      tax_label: "Tax",
      status: "Confirmed",
      customer_name: "Kuwait Gulf Summit",
      event_name: "Gulf Board Dinner",
      event_type: "Conference",
      venue: "Sunset Kuwait-Pearl Hall",
      event_date: "2026-12-10",
      start_time: "18:00",
      end_time: "22:00",
      session: "Evening",
      attendees: 10,
      pax_guaranteed: 10,
      pax_actual: 0,
      billable_pax: 10,
      grand_total: 37.475,
      subtotal: 37.475,
      taxable_amount: 37.475,
      tax_amount: 0,
      discount_amount: 0,
      service_charge_percent: 0,
      service_charge: 0,
      advance_received: 10.125,
      balance_due: 27.35,
      deposit_held: 0,
      deposit_refunded: 0,
      damage_amount: 0,
      quote_version: 1,
      posted_to_folio: 0,
      folio: "FOLIO-KW-EVT",
      fiscal_bridge: 1,
      event_folio: "FOLIO-KW-EVT",
      items: [{
        name: "i1", item_type: "Menu", item_name: "Mezzeh Platter", qty: 10, uom: "Pax",
        rate: 3.7475, list_rate: 3.7475, amount: 37.475, net_amount: 37.475,
        chargeable: 1, gst_rate: 0, gst_amount: 0, total: 37.475,
      }],
      payment_terms: [],
      receipts: [{
        name: "r1", receipt_date: "2026-11-24", kind: "Advance", mode: "Cash",
        amount: 10.125, reference: "ACC-PAY-2026-00081",
      }],
      revisions: [],
      selections: [],
      venue_detail: {},
      group_detail: null,
      scheduled_total: 0,
      open_item_impact: 0,
      next_actions: [],
      checklist: [],
      open_items: [],
    })
    const host = document.createElement("div")
    document.body.appendChild(host)
    const root = createRoot(host)
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/banquet/EVT-2026-0609"]}>
          <Routes><Route path="/banquet/:name" element={<BanquetFunction />} /></Routes>
        </MemoryRouter>,
      )
    })
    await flush()
    const initialText = host.textContent || ""
    expect(initialText).toContain("KD37.475")
    expect(initialText).toContain("KD10.125")
    expect(initialText).toContain("KD27.350")
    expect(initialText).not.toContain("UGX")

    const moneyBtn = [...host.querySelectorAll("button")].find((b) => b.textContent?.includes("Money"))
    expect(moneyBtn).toBeTruthy()
    await act(async () => {
      moneyBtn!.click()
    })
    await flush()
    const moneyText = host.textContent || ""
    expect(moneyText).toContain("KD10.125")
    expect(moneyText).toContain("KD27.350")
    expect(moneyText).not.toContain("UGX")
    await act(async () => root.unmount())
  })
})
