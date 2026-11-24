// @vitest-environment jsdom
/** Round 8 UI checks (Hydrogen R7 review):
 * - P1 #1: Applied OTA modify banner separates reason codes from #46's unapplied
 *   banner, states what was removed & the new total, and ships all 8 languages.
 * - P2 #5: AgreedRatePanel and ReservationDetail format amounts with the currency's
 *   decimals (KWD 3, USD 2, UGX 0) instead of rounding to integers.
 * - P2 #7: Mobile (390px) Billing card stacks cleanly on narrow viewports with
 *   min-w-0 cells instead of a fixed 3-column grid that overflows/overlaps. */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const { rpc, localeState } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return {
    rpc: vi.fn(),
    localeState: { currency: "UGX", currency_symbol: "UGX ", locale: "en-UG" },
  }
})

vi.mock("../src/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api")>()
  const call = (method: string, params?: Record<string, unknown>) => rpc(method, params ?? {})
  return {
    ...actual,
    call,
    getCurrentProperty: () => "Tank Hill Lodge",
    reservationDetail: (reservation: string) => call("kamra.api.reservation_detail", { reservation }),
    clearChannelReview: (reservation: string) =>
      call("kamra.channel_manager.clear_channel_review", { reservation }),
    idDocumentImage: vi.fn().mockResolvedValue({ data: "" }),
  }
})
vi.mock("../src/lib/auth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/lib/auth")>()),
  useAuth: () => ({
    status: "authed", user: "desk@test", roles: ["Front Desk", "Revenue Manager"],
    refresh: async () => {}, signOut: async () => {},
  }),
}))
vi.mock("../src/lib/realtime", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/lib/realtime")>()),
  useRealtime: () => {},
}))
vi.mock("../src/lib/money", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/money")>()
  const digits = (c = localeState.currency) =>
    new Intl.NumberFormat("en", { style: "currency", currency: c }).resolvedOptions().maximumFractionDigits ?? 2
  const cached: Record<string, ReturnType<typeof makeLoc>> = {}
  const makeLoc = (currency: string, currency_symbol: string, locale: string) => ({
    currency,
    currency_symbol,
    locale,
    tax_label: "VAT",
    tax_id_label: "TIN",
    tax_rates: [0],
    id_types: ["Passport"],
    id_type_hints: {},
    payment_modes: ["Cash"],
    instant_payment_modes: [],
    outlet_payment_modes: ["Cash"],
    default_nationality: "Ugandan",
    adult_age: 18,
    minor_ids: "foreign" as const,
    integrations: { payment_gateways: [], tax_return: false, verify_tax_id: false, verify_guest_id: false },
  })
  const locObj = () => {
    const key = `${localeState.currency}|${localeState.currency_symbol}|${localeState.locale}`
    return (cached[key] ??= makeLoc(localeState.currency, localeState.currency_symbol, localeState.locale))
  }
  return {
    ...actual,
    cur: () => localeState.currency_symbol,
    moneyLocale: () => localeState.locale,
    moneyDigits: digits,
    fmtAmount: (n: unknown, c = localeState.currency) =>
      Number(n ?? 0).toLocaleString(localeState.locale, {
        minimumFractionDigits: digits(c), maximumFractionDigits: digits(c),
      }),
    fmtMoney: (n: unknown) =>
      localeState.currency_symbol + Number(n ?? 0).toLocaleString(localeState.locale, {
        minimumFractionDigits: digits(), maximumFractionDigits: digits(),
      }),
    locale: locObj,
    useLocale: locObj,
    loadLocale: async () => locObj(),
  }
})
vi.mock("../src/components/LinkedRecords", () => ({ default: () => null }))
vi.mock("../src/components/StayInstructions", () => ({ default: () => null }))
vi.mock("../src/components/CancelPanel", () => ({ default: () => null }))

import AgreedRatePanel from "../src/components/AgreedRatePanel"
import ReservationDetail from "../src/screens/ReservationDetail"
import ar from "../src/i18n/locales/ar.json"
import de from "../src/i18n/locales/de.json"
import es from "../src/i18n/locales/es.json"
import fil from "../src/i18n/locales/fil.json"
import id from "../src/i18n/locales/id.json"
import pt from "../src/i18n/locales/pt.json"
import zhCN from "../src/i18n/locales/zh-CN.json"

const NIGHTS = ["2026-11-24", "2026-11-25"]

const REASONS = [
  { name: "RATE-PROMO", code: "RATE-PROMO", description: "Promotional rate change", category: "Rate Change", requires_supervisor: 0 },
]

function baseDetail(overrides: Record<string, unknown> = {}) {
  return {
    name: "RES-2026-00011",
    status: "Confirmed",
    booking_type: "Individual",
    source: "OTA",
    channel: "Booking.com",
    property: "Tank Hill Lodge",
    room: "RM-101",
    room_number: "101",
    room_type: "DLX",
    room_type_name: "Deluxe",
    check_in_date: NIGHTS[0],
    check_out_date: "2026-11-26",
    nights: 2,
    adults: 2,
    children: 0,
    meal_plan: null,
    meal_plan_name: null,
    rate_plan: null,
    special_requests: null,
    eta: null,
    late_arrival: 0,
    occupants: [],
    booked_check_out_date: null,
    early_departure_reason: null,
    guest: {
      name: "G-1", full_name: "Kato Mukasa", phone: "+256700000001", email: "kato@example.com",
      vip: 0, blacklisted: 0, stays: 1, last_stay: null,
    },
    booker: null,
    cancellation: null,
    night_rates: NIGHTS.map((date) => ({
      date, base_rate: 150000, rate: 150000, discount_amount: 0,
      discount_type: null, discount_value: 0, entry_base_rate: 150000, entry_rate: 150000,
      demand_premium_pct: 0, charged: false,
    })),
    discount: { discount_type: null, discount_value: 0, discount_reason: null, fixed_rate: 0 },
    precheckin_status: "Not Started",
    precheckin_token: null,
    id_document: null,
    id_document_source: null,
    id_document_on: null,
    id_document_discarded: 0,
    precheckin_verified_by: null,
    precheckin_verified_on: null,
    amount_after_tax: 300000,
    advance_paid: 0,
    company: null,
    travel_agent: null,
    folio_name: null,
    money: { total: 300000, paid: 0, due: 300000, source: "booking", has_folio: false },
    deposit: null,
    security_deposit: null,
    channel_review_needed: 0,
    channel_review_reason: null,
    channel_reviews: [],
    actions: {
      can_check_in: true, can_check_out: false, can_cancel: true, can_no_show: false,
      can_amend: true, can_set_rate: false, can_reinstate: false, can_reverse_check_in: false,
    },
    warnings: { id_document_missing: false, id_unverified: false, channel_review_needed: false },
    checkin_backfill: [],
    ...overrides,
  }
}

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  localeState.currency = "UGX"
  localeState.currency_symbol = "UGX "
  localeState.locale = "en-UG"
  rpc.mockReset()
  host = document.createElement("div")
  document.body.append(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
})

const until = (fn: () => void) => vi.waitFor(fn, { interval: 5, timeout: 2500 })

async function typeInto(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
  await act(async () => {
    setter.call(input, value)
    input.dispatchEvent(new Event("input", { bubbles: true }))
  })
}

describe("R8 P1 #1: Applied OTA modify banner vs #46 unapplied banner", () => {
  it("shows applied discount-removed banner with new total and removed details, never 'not applied / left unchanged'", async () => {
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.reservation_detail") {
        return Promise.resolve(baseDetail({
          channel_review_needed: 1,
          channel_review_reason: "discount_removed",
          warnings: { id_document_missing: false, id_unverified: false, channel_review_needed: true },
          channel_reviews: [{
            name: "AAL-2026-000011",
            action_type: "channel.modify_discount_removed",
            reason: "discount_removed",
            rationale: "Channel modify (Booking.com BK-THL-R8-1) applied — removed desk discount: 10% off (RATE-PROMO); new total UGX 300,000.",
            executed_at: "2026-11-24 10:00:00",
          }],
        }))
      }
      return Promise.resolve([])
    })

    await act(async () => root.render(
      <MemoryRouter>
        <ReservationDetail row={{ name: "RES-2026-00011" }} reload={() => undefined} onClose={() => undefined} />
      </MemoryRouter>,
    ))
    await until(() => expect(host.textContent).toContain("Kato Mukasa"))

    expect(host.textContent).toContain("Channel update applied — desk discount removed")
    expect(host.textContent).toContain("UGX 300,000")
    expect(host.textContent).toContain("10% off (RATE-PROMO)")
    expect(host.textContent).not.toContain("Channel update not applied — review required")
    expect(host.textContent).not.toContain("left unchanged")
    expect(host.textContent).not.toMatch(/\b(?:Kamra|ERPNext|Frappe|URY)\b/i)
  })

  it("shows applied discount-review banner when added nights were priced without unapproved discount", async () => {
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.reservation_detail") {
        return Promise.resolve(baseDetail({
          nights: 3,
          amount_after_tax: 375000,
          money: { total: 375000, paid: 0, due: 375000, source: "booking", has_folio: false },
          channel_review_needed: 1,
          channel_review_reason: "discount_review",
          warnings: { id_document_missing: false, id_unverified: false, channel_review_needed: true },
          channel_reviews: [{
            name: "AAL-2026-000012",
            action_type: "channel.modify_discount_review",
            reason: "discount_review",
            rationale: "Channel modify (Booking.com BK-THL-R8-2) applied — kept approved desk discount on 2026-11-24, 2026-11-25; added night(s) 2026-11-26 priced without unapproved discount; new total UGX 375,000.",
            executed_at: "2026-11-24 10:05:00",
          }],
        }))
      }
      return Promise.resolve([])
    })

    await act(async () => root.render(
      <MemoryRouter>
        <ReservationDetail row={{ name: "RES-2026-00011" }} reload={() => undefined} onClose={() => undefined} />
      </MemoryRouter>,
    ))
    await until(() => expect(host.textContent).toContain("Kato Mukasa"))

    expect(host.textContent).toContain("Channel update applied — discount review required")
    expect(host.textContent).toContain("UGX 375,000")
    expect(host.textContent).toContain("2026-11-26")
    expect(host.textContent).not.toContain("Channel update not applied — review required")
    expect(host.textContent).not.toContain("left unchanged")
  })

  it("keeps #46 unapplied banner when reason is update_not_applied", async () => {
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.reservation_detail") {
        return Promise.resolve(baseDetail({
          status: "Checked In",
          channel_review_needed: 1,
          channel_review_reason: "update_not_applied",
          warnings: { id_document_missing: false, id_unverified: false, channel_review_needed: true },
          channel_reviews: [{
            name: "AAL-2026-000013",
            action_type: "channel.modify_needs_review",
            reason: "update_not_applied",
            rationale: "Booking.com modify not applied: stay is Checked In",
            executed_at: "2026-11-24 10:10:00",
          }],
        }))
      }
      return Promise.resolve([])
    })

    await act(async () => root.render(
      <MemoryRouter>
        <ReservationDetail row={{ name: "RES-2026-00011" }} reload={() => undefined} onClose={() => undefined} />
      </MemoryRouter>,
    ))
    await until(() => expect(host.textContent).toContain("Kato Mukasa"))

    expect(host.textContent).toContain("Channel update not applied — review required")
    expect(host.textContent).toContain("left unchanged")
    expect(host.textContent).not.toContain("Channel update applied — desk discount removed")
  })

  it.each([
    ["ar", ar],
    ["de", de],
    ["es", es],
    ["fil", fil],
    ["id", id],
    ["pt", pt],
    ["zh-CN", zhCN],
  ])("translates both applied and unapplied channel review banner strings in %s", (_lang, dict: Record<string, string>) => {
    const keys = [
      "Channel update not applied — review required",
      "The stay's dates, room, and charges were left unchanged. Review what the channel sent below and update the stay manually if needed.",
      "Channel update applied — desk discount removed",
      "The channel update was applied and removed the desk discount (new total: {total}). Review what was removed below and re-apply a discount if needed.",
      "Channel update applied — discount review required",
      "The channel update was applied (new total: {total}). Approved discounted nights were kept; added nights were priced without the unapproved discount. Review below and adjust if needed.",
    ]
    for (const key of keys) {
      expect(dict[key], key).toBeTruthy()
      if (key.includes("{total}")) {
        expect(dict[key], `${key} placeholder`).toContain("{total}")
      }
    }
  })
})

describe("R8 P2 #5: Currency decimals in AgreedRatePanel & ReservationDetail (KWD 3, USD 2, UGX 0)", () => {
  it("formats KWD with 3 decimals in AgreedRatePanel preview and ReservationDetail cards & ChangesLog", async () => {
    localeState.currency = "KWD"
    localeState.currency_symbol = "KWD "
    localeState.locale = "en-KW"

    rpc.mockImplementation((method: string) => {
      if (method === "kamra.ledger.list_reason_codes") return Promise.resolve(REASONS)
      if (method === "kamra.api.discount_preview") {
        return Promise.resolve({
          eligible: true, base_total: 24.69, discount_total: 2.468, net_total: 22.222,
          effective_pct: 10, max_pct: 10, limit_pct: 20, needs_supervisor: false,
          can_set_rate: false, agent: false, comp: false, floor_breach: [],
          reason_requires_supervisor: false, supervisors: [],
          current_amount: 24.69, new_amount: 22.222, difference: -2.468, needs_confirmation: true,
          nights: NIGHTS.map((date) => ({
            date, base_rate: 12.345, entry_base_rate: 12.345,
            discount_type: "Percent", discount_value: 10, discount_amount: 1.234,
            rate: 11.111, entry_rate: 11.111, charged: false,
          })),
        })
      }
      if (method === "kamra.api.reservation_detail") {
        return Promise.resolve(baseDetail({
          amount_after_tax: 22.222,
          money: { total: 22.222, paid: 10.125, due: 12.097, source: "booking", has_folio: false },
          night_rates: NIGHTS.map((date) => ({
            date, base_rate: 12.345, rate: 11.111, discount_amount: 1.234,
            discount_type: "Amount", discount_value: 1.234, discount_reason: "RATE-PROMO",
            entry_base_rate: 12.345, entry_rate: 11.111, demand_premium_pct: 0, charged: false,
          })),
          discount: { discount_type: "Amount", discount_value: 1.234, discount_reason: "RATE-PROMO", fixed_rate: 0 },
        }))
      }
      if (method === "kamra.api.reservation_changes") {
        return Promise.resolve({
          entries: [{
            kind: "action", at: "2026-11-24 10:00:00", by: "desk@test", by_name: "Desk",
            approver: null, approver_name: null, action_type: "reservation_discount",
            title: "Discount", rationale: "1.234 off",
            fields: [], nights: [{ date: NIGHTS[0], from: 12.345, to: 11.111 }],
          }],
        })
      }
      return Promise.resolve([])
    })

    // 1) AgreedRatePanel preview table preserves 3 decimals for KWD
    await act(async () => root.render(
      <AgreedRatePanel
        reservation="RES-KWD"
        nightRates={NIGHTS.map((date) => ({
          date, rate: 12.345, base_rate: 12.345, entry_base_rate: 12.345, charged: false,
        }))}
        reasonCategories={{ discount: ["Rate Change"], agreed_rate: ["Rate Change"] }}
        onSaved={() => undefined}
      />,
    ))
    const openBtn = Array.from(host.querySelectorAll("button")).find(
      (b) => b.textContent?.includes("Stay rate & daily details"),
    )!
    await act(async () => openBtn.click())
    await typeInto(host.querySelector<HTMLInputElement>('input[aria-label="Discount value"]')!, "10")
    await until(() => expect(host.textContent).toContain("KWD 12.345"))
    expect(host.textContent).toContain("KWD 1.234")
    expect(host.textContent).toContain("KWD 11.111")
    expect(host.textContent).toContain("KWD 24.690")
    expect(host.textContent).toContain("KWD 2.468")
    expect(host.textContent).toContain("KWD 22.222")

    // 2) ReservationDetail cards & ChangesLog preserve 3 decimals for KWD
    await act(async () => root.render(
      <MemoryRouter>
        <ReservationDetail row={{ name: "RES-KWD" }} reload={() => undefined} onClose={() => undefined} />
      </MemoryRouter>,
    ))
    await until(() => expect(host.textContent).toContain("Confirmed rate / night"))
    expect(host.textContent).toContain("KWD 22.222")
    expect(host.textContent).toContain("KWD 10.125")
    expect(host.textContent).toContain("KWD 12.097")
    expect(host.textContent).toContain("KWD 12.345")
    expect(host.textContent).toContain("KWD 11.111")
    expect(host.textContent).toContain("KWD 1.234")

    const logBtn = Array.from(host.querySelectorAll("button")).find(
      (b) => b.textContent?.trim() === "Changes log",
    )!
    await act(async () => logBtn.click())
    await until(() => expect(host.textContent).toContain("KWD 12.345 → KWD 11.111"))
  })

  it("formats USD with 2 decimals and UGX with 0 decimals in ReservationDetail", async () => {
    localeState.currency = "USD"
    localeState.currency_symbol = "$"
    localeState.locale = "en-US"
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.reservation_detail") {
        return Promise.resolve(baseDetail({
          amount_after_tax: 270.9,
          money: { total: 270.9, paid: 100.25, due: 170.65, source: "booking", has_folio: false },
          night_rates: NIGHTS.map((date) => ({
            date, base_rate: 150.5, rate: 135.45, discount_amount: 15.05,
            discount_type: "Percent", discount_value: 10,
            entry_base_rate: 150.5, entry_rate: 135.45, demand_premium_pct: 0, charged: false,
          })),
          discount: { discount_type: "Percent", discount_value: 10, discount_reason: "RATE-PROMO", fixed_rate: 0 },
        }))
      }
      return Promise.resolve([])
    })

    await act(async () => root.render(
      <MemoryRouter>
        <ReservationDetail row={{ name: "RES-USD" }} reload={() => undefined} onClose={() => undefined} />
      </MemoryRouter>,
    ))
    await until(() => expect(host.textContent).toContain("Confirmed rate / night"))
    expect(host.textContent).toContain("$270.90")
    expect(host.textContent).toContain("$100.25")
    expect(host.textContent).toContain("$170.65")
    expect(host.textContent).toContain("$150.50")
    expect(host.textContent).toContain("$135.45")

    // Switch to UGX: 0 decimals (no ".00")
    localeState.currency = "UGX"
    localeState.currency_symbol = "UGX "
    localeState.locale = "en-UG"
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.reservation_detail") {
        return Promise.resolve(baseDetail({
          amount_after_tax: 300000,
          money: { total: 300000, paid: 0, due: 300000, source: "booking", has_folio: false },
        }))
      }
      return Promise.resolve([])
    })
    await act(async () => root.render(
      <MemoryRouter>
        <ReservationDetail row={{ name: "RES-UGX" }} reload={() => undefined} onClose={() => undefined} />
      </MemoryRouter>,
    ))
    await until(() => expect(host.textContent).toContain("UGX 300,000"))
    expect(host.textContent).not.toContain("300,000.00")
  })
})

describe("R8 P2 #7: Mobile (390px) Billing card responsive layout", () => {
  it("uses a responsive single-column mobile grid with min-w-0 cells instead of cramped 3-col layout", async () => {
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.reservation_detail") {
        return Promise.resolve(baseDetail({
          amount_after_tax: 1350000,
          money: { total: 1350000, paid: 500000, due: 850000, source: "booking", has_folio: false },
        }))
      }
      return Promise.resolve([])
    })

    await act(async () => root.render(
      <MemoryRouter>
        <ReservationDetail row={{ name: "RES-MOB" }} reload={() => undefined} onClose={() => undefined} />
      </MemoryRouter>,
    ))
    await until(() => expect(host.textContent).toContain("Balance due"))

    const balanceLabel = Array.from(host.querySelectorAll("div")).find(
      (el) => el.textContent?.trim() === "Balance due",
    )
    const summary = balanceLabel?.parentElement?.parentElement as HTMLElement | null
    expect(summary, "Billing summary grid container").not.toBeNull()
    expect(summary!.className).toContain("grid-cols-1")
    expect(summary!.className).toContain("sm:grid-cols-3")
    expect(summary!.className).not.toMatch(/(?:^|\s)grid-cols-3(?:\s|$)/)
    const cells = Array.from(summary!.children) as HTMLElement[]
    expect(cells.length).toBe(3)
    for (const cell of cells) {
      expect(cell.className).toContain("min-w-0")
    }
  })
})
