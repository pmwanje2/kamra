// @vitest-environment jsdom
/** R2 RED->GREEN regression test for Hydrogen R1 P1 finding on pmwanje2/kamra#59:
 *  BanquetMonth, CashierBoard, and CashierSessions must subscribe to useLocale()
 *  so cold reloads (screen data resolving before property_locale) and property
 *  switches immediately re-render with the resolved property locale/currency/digits
 *  instead of staying stuck on neutral fallback ("en", "", 2dp).
 */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import BanquetMonth from "../src/screens/BanquetMonth"
import CashierBoard from "../src/screens/CashierBoard"
import CashierSessions from "../src/screens/CashierSessions"
import { loadLocale } from "../src/lib/money"
import { setLang } from "../src/lib/dir"

const { rpc, monthAvailability } = vi.hoisted(() => ({
  rpc: vi.fn(),
  monthAvailability: vi.fn(),
}))

vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => "Hotel Horizon Paris",
  banquet: {
    monthAvailability: (month: string) => monthAvailability(month),
  },
}))

vi.mock("../src/lib/resource", () => ({
  serverError: (e: unknown) => (e instanceof Error ? e.message : String(e)),
}))

vi.mock("../src/lib/auth", () => ({
  useAuth: () => ({ status: "authed", roles: ["Front Desk", "Finance", "Hotel Admin"] }),
}))

vi.mock("../src/lib/cashierAuth", () => ({
  useCashierAuth: () => ({ withPin: async (fn: () => Promise<unknown>) => fn() }),
}))

function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

const FR_UGX_LOCALE = {
  currency: "UGX",
  currency_symbol: "Sh",
  locale: "fr-FR",
  tax_label: "VAT",
  tax_id_label: "TIN",
  tax_rates: [0, 18],
  payment_modes: ["Cash", "Card"],
  instant_payment_modes: [],
  outlet_payment_modes: ["Cash"],
  id_types: ["Passport"],
  id_type_hints: {},
  default_nationality: "",
  adult_age: 18,
  minor_ids: "foreign" as const,
  integrations: {
    payment_gateways: [],
    tax_return: false,
    verify_tax_id: false,
    verify_guest_id: false,
  },
}

const normSpaces = (s: string) => s.replace(/[\u00a0\u202f]/g, " ")

describe("R2 P1: BanquetMonth, CashierBoard, and CashierSessions subscribe to useLocale()", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(async () => {
    localStorage.clear()
    setLang("fr")
    rpc.mockReset()
    monthAvailability.mockReset()
    // Reset money.ts module cache to neutral cold-load defaults ("en", "", 2dp)
    rpc.mockResolvedValueOnce({
      currency: "",
      currency_symbol: "",
      locale: "en",
      tax_label: "Tax",
      tax_id_label: "Tax ID",
      tax_rates: [],
      payment_modes: [],
      instant_payment_modes: [],
      outlet_payment_modes: [],
      id_types: [],
      id_type_hints: {},
      default_nationality: "",
      adult_age: 18,
      minor_ids: "foreign",
      integrations: {
        payment_gateways: [],
        tax_return: false,
        verify_tax_id: false,
        verify_guest_id: false,
      },
    })
    await loadLocale("Hotel Horizon Paris", true)
    localStorage.clear()
    rpc.mockReset()
    host = document.createElement("div")
    document.body.appendChild(host)
    root = createRoot(host)
  })

  afterEach(() => {
    act(() => root.unmount())
    host.remove()
  })

  it("BanquetMonth re-renders hall prices in UGX 0dp fr-FR when property_locale resolves after screen data", async () => {
    const loc = deferred<typeof FR_UGX_LOCALE>()
    monthAvailability.mockResolvedValue({
      month: "2026-11",
      dates: ["2026-11-24"],
      rows: [
        {
          venue: "HALL-1",
          venue_name: "Grand Salon",
          capacity: 320,
          base_price: 850000,
          session: "Morning",
          by_date: { "2026-11-24": [] },
        },
        {
          venue: "HALL-1",
          venue_name: "Grand Salon",
          capacity: 320,
          base_price: 850000,
          session: "Afternoon",
          by_date: { "2026-11-24": [] },
        },
        {
          venue: "HALL-1",
          venue_name: "Grand Salon",
          capacity: 320,
          base_price: 850000,
          session: "Evening",
          by_date: { "2026-11-24": [] },
        },
      ],
    })
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.property_locale") return loc.promise
      return Promise.resolve({})
    })

    await act(async () => {
      root.render(
        <MemoryRouter>
          <BanquetMonth />
        </MemoryRouter>,
      )
      await Promise.resolve()
    })

    // Screen data has arrived while locale is still neutral fallback ("850,000.00")
    expect(host.textContent).toContain("Grand Salon")

    // Now AppShell / loadLocale resolves to Hotel Horizon Paris (UGX 0dp, fr-FR, "Sh")
    await act(async () => {
      const p = loadLocale("Hotel Horizon Paris", true)
      loc.resolve(FR_UGX_LOCALE)
      await p
    })

    const text = normSpaces(host.textContent ?? "")
    expect(text).toContain("Sh850 000")
    expect(text).not.toContain("850,000.00")
  })

  it("CashierBoard re-renders cash and FX drawer totals in fr-FR UGX/KWD/USD when property_locale resolves after data", async () => {
    const loc = deferred<typeof FR_UGX_LOCALE>()
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.property_locale") return loc.promise
      if (method === "kamra.cashier.tills_board") {
        return Promise.resolve({
          business_date: "2026-11-25",
          tills: [
            {
              name: "TILL-1",
              terminal_name: "Reception 1",
              kind: "Front Desk",
              outlet: null,
              bill_printer: null,
              is_default: true,
              sessions: [
                {
                  name: "SESS-1",
                  user: "cashier@hotel.test",
                  opened_at: "2026-11-25 08:00:00",
                  expected_cash: 250000,
                  foreign_drawers: [
                    { currency: "KWD", expected_fx: 12.5, counted_fx: null, variance_fx: null, home_equivalent: 150000 },
                    { currency: "USD", expected_fx: 45.5, counted_fx: null, variance_fx: null, home_equivalent: 166000 },
                  ],
                },
              ],
            },
          ],
          pending_transfers: [],
          variance_review: [],
        })
      }
      return Promise.resolve([])
    })

    await act(async () => {
      root.render(
        <MemoryRouter>
          <CashierBoard />
        </MemoryRouter>,
      )
      await Promise.resolve()
    })

    expect(host.textContent).toContain("Reception 1")

    await act(async () => {
      const p = loadLocale("Hotel Horizon Paris", true)
      loc.resolve(FR_UGX_LOCALE)
      await p
    })

    const text = normSpaces(host.textContent ?? "")
    expect(text).toContain("250 000")
    expect(text).toContain("12,500")
    expect(text).toContain("45,50")
    expect(text).not.toContain("250,000.00")
  })

  it("CashierSessions re-renders session cash and FX columns in fr-FR UGX/KWD when property_locale resolves after data", async () => {
    const loc = deferred<typeof FR_UGX_LOCALE>()
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.property_locale") return loc.promise
      if (method === "kamra.cashier.list_sessions") {
        return Promise.resolve({
          business_date: "2026-11-25",
          sessions: [
            {
              name: "SESS-1",
              user: "cashier@hotel.test",
              business_date: "2026-11-25",
              status: "Closed",
              opening_float: 50000,
              system_cash: 200000,
              expected_cash: 250000,
              counted_cash: 249500,
              variance: -500,
              foreign_drawers: [
                { currency: "KWD", expected_fx: 12.5, counted_fx: 12.35, variance_fx: -0.15 },
              ],
              opened_at: "2026-11-25 08:00:00",
              closed_at: "2026-11-25 18:00:00",
            },
          ],
        })
      }
      return Promise.resolve({})
    })

    await act(async () => {
      root.render(
        <MemoryRouter>
          <CashierSessions />
        </MemoryRouter>,
      )
      await Promise.resolve()
    })

    expect(host.textContent).toContain("cashier@hotel.test")

    await act(async () => {
      const p = loadLocale("Hotel Horizon Paris", true)
      loc.resolve(FR_UGX_LOCALE)
      await p
    })

    const text = normSpaces(host.textContent ?? "")
    expect(text).toContain("250 000")
    expect(text).toContain("12,500")
    expect(text).not.toContain("250,000.00")
  })
})
