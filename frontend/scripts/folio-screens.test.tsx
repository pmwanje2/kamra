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
  moneyLocale: () => mockLoc.locale,
  moneyDigits: (c?: string) => digitsFor(c ?? mockLoc.currency),
  fmtAmount: (n: unknown, c?: string) => {
    const d = digitsFor(c ?? mockLoc.currency)
    return Number(n ?? 0).toLocaleString(mockLoc.locale, { minimumFractionDigits: d, maximumFractionDigits: d })
  },
  fmtMoney: (n: unknown) => {
    const d = digitsFor(mockLoc.currency)
    return mockLoc.currency_symbol + Number(n ?? 0).toLocaleString(mockLoc.locale, { minimumFractionDigits: d, maximumFractionDigits: d })
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
  return {
    t,
    useT: () => ({ lang: "en", t, tn: (_n: number, one: string) => one }),
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

  it("keeps 'Reprint fiscal invoice' for fiscalised invoices and shows Credit note / Cancel invoice when credit_and_rebill is installed", async () => {
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
    expect(labels).toContain("Credit note")
    expect(labels).toContain("Cancel invoice")
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
