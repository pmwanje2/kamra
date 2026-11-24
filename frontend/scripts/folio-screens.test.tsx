// @vitest-environment jsdom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, describe, expect, it, vi } from "vitest"
import FolioHistory from "../src/screens/FolioHistory"
import FolioView from "../src/screens/FolioView"

const callMock = vi.fn()

vi.mock("../src/lib/api", () => ({
  call: (...args: unknown[]) => callMock(...args),
  getCurrentProperty: () => "PROP-1",
  newIdempotencyKey: () => "idem-1",
}))
vi.mock("../src/lib/resource", () => ({
  serverError: (e: unknown) => (e instanceof Error ? e.message : String(e)),
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
vi.mock("../src/lib/money", () => {
  const loc = {
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
  }
  return {
    cur: () => loc.currency_symbol,
    locale: () => loc,
    useLocale: () => loc,
    loadLocale: () => Promise.resolve(loc),
    moneyLocale: () => loc.locale,
    moneyDigits: () => 0,
    fmtAmount: (n: unknown) => String(n ?? 0),
    sellsAlcohol: () => true,
    taxRates: () => loc.tax_rates,
    taxLabel: () => loc.tax_label,
  }
})
vi.mock("../src/lib/i18n", () => {
  const t = (s: unknown, vars?: Record<string, unknown>) =>
    typeof s === "string"
      ? s.replace(/\{(\w+)\}/g, (_m, k: string) => String(vars?.[k] ?? `{${k}}`))
      : s
  return {
    t,
    useT: () => ({ lang: "en", t, tn: (_n: number, one: string) => one }),
    useUILanguage: () => "en",
  }
})

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

afterEach(() => {
  authState.roles = ["Front Desk"]
  callMock.mockReset()
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
