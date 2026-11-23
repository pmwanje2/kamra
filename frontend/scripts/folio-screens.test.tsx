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
vi.mock("../src/lib/auth", () => ({
  useAuth: () => ({ roles: ["Front Desk"] }),
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
})
