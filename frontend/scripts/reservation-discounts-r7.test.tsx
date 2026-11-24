// @vitest-environment jsdom
/** Round 7 UI checks (Hydrogen review P2-1, P2-2, P2-3 + BookingDialog full discount reason):
 * - P2-3: AgreedRatePanel preview Total row totals Base / Discount / Net columns,
 *   with change vs current booking shown on its own labelled line.
 * - P2-2: ReservationDetail "Confirmed rate / night" uses the same entry basis
 *   as "Base rate / night" on tax-inclusive unbridged stays.
 * - P2-1: 3-decimal currencies (KWD) preserve 3 decimal places in entryBase and
 *   DiscountPanel Amount inputs (step="0.001").
 * - Walk-in & New booking quote rail shows the full discount reason without
 *   truncating ("Discount (Promotional rate change)"), with tooltip + accessible text. */
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
    getBookingOptions: () => call("kamra.api.booking_options", { property: "Tank Hill Lodge" }),
    getQuote: (params: Record<string, unknown>) => call("kamra.api.get_quote", { property: "Tank Hill Lodge", ...params }),
    createBooking: (params: Record<string, unknown>) =>
      call("kamra.api.create_booking", { property: "Tank Hill Lodge", ...params }),
    guestAlert: async () => ({ guest: null, blacklisted: 0, blacklist_reason: null }),
    guestSearch: async () => [],
    reservationDetail: (reservation: string) => call("kamra.api.reservation_detail", { reservation }),
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
vi.mock("../src/components/ChangesLog", () => ({ default: () => null }))

import AgreedRatePanel from "../src/components/AgreedRatePanel"
import { BookingDialog } from "../src/components/BookingDialog"
import DiscountPanel from "../src/components/DiscountPanel"
import ReservationDetail from "../src/screens/ReservationDetail"
import { emptyDiscount, entryBase } from "../src/lib/reservationDiscount"
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

async function choose(select: HTMLSelectElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!
  await act(async () => {
    setter.call(select, value)
    select.dispatchEvent(new Event("change", { bubbles: true }))
  })
}

describe("P2-3: AgreedRatePanel preview Total row", () => {
  it("totals Base, Discount and Net columns and labels the change from current booking separately", async () => {
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.ledger.list_reason_codes") return Promise.resolve(REASONS)
      if (method === "kamra.api.discount_preview") {
        return Promise.resolve({
          eligible: true,
          base_total: 300000,
          discount_total: 75000,
          net_total: 225000,
          effective_pct: 25,
          max_pct: 25,
          limit_pct: 15,
          needs_supervisor: true,
          can_set_rate: false,
          agent: false,
          comp: false,
          floor_breach: [],
          reason_requires_supervisor: false,
          supervisors: [{ user: "gm@test.local", full_name: "David GM" }],
          current_amount: 270000,
          new_amount: 225000,
          difference: -45000,
          needs_confirmation: true,
          nights: NIGHTS.map((date) => ({
            date,
            base_rate: 150000,
            entry_base_rate: 150000,
            discount_type: "Percent",
            discount_value: 25,
            discount_amount: 37500,
            rate: 112500,
            entry_rate: 112500,
            own: false,
            charged: false,
          })),
        })
      }
      return Promise.resolve({})
    })

    await act(async () => root.render(
      <AgreedRatePanel
        reservation="RES-41"
        nightRates={NIGHTS.map((date) => ({
          date, rate: 135000, base_rate: 150000, entry_base_rate: 150000,
          entry_rate: 135000, discount_amount: 15000, charged: false,
        }))}
        current={{ discount_type: "Percent", discount_value: 10, discount_reason: "RATE-PROMO", fixed_rate: 0 }}
        reasonCategories={{ discount: ["Rate Change"], agreed_rate: ["Rate Change"] }}
        onSaved={() => undefined}
      />,
    ))

    const openBtn = Array.from(host.querySelectorAll("button")).find(
      (b) => b.textContent?.includes("Stay rate & daily details"),
    )!
    await act(async () => openBtn.click())
    const valInput = host.querySelector<HTMLInputElement>('input[aria-label="Discount value"]')!
    await typeInto(valInput, "25")

    await until(() => {
      const totalRow = Array.from(host.querySelectorAll("tr")).find(
        (tr) => tr.firstElementChild?.textContent?.trim() === "Total",
      )
      expect(totalRow).toBeDefined()
    })

    const totalRow = Array.from(host.querySelectorAll("tr")).find(
      (tr) => tr.firstElementChild?.textContent?.trim() === "Total",
    )!
    const cells = Array.from(totalRow.querySelectorAll("td")).map((td) => td.textContent?.trim())
    expect(cells[1]).toContain("300,000")
    expect(cells[2]).toContain("75,000")
    expect(cells[3]).toContain("225,000")

    // Change versus the current booking (270,000 -> 225,000, -45,000) is its own labelled line
    expect(host.textContent).toContain("Change from current booking")
    expect(host.textContent).toContain("270,000")
    expect(host.textContent).toContain("45,000")
  })

  it.each([
    ["ar", ar],
    ["de", de],
    ["es", es],
    ["fil", fil],
    ["id", id],
    ["pt", pt],
    ["zh-CN", zhCN],
  ])("translates 'Change from current booking' in %s", (_lang, dict: Record<string, string>) => {
    expect(dict["Change from current booking"]).toBeTruthy()
  })
})

describe("P2-2: ReservationDetail Confirmed rate / night basis", () => {
  it("shows Confirmed rate / night on the same entry basis as Base rate / night", async () => {
    localeState.currency = "INR"
    localeState.currency_symbol = "₹"
    localeState.locale = "en-IN"
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.reservation_detail") {
        return Promise.resolve({
          name: "RES-99", status: "Confirmed", booking_type: "Individual", source: "Direct", channel: null,
          property: "Unbridged Inclusive Hotel", room: "RM-101", room_number: "101",
          room_type: "DLX", room_type_name: "Deluxe",
          check_in_date: NIGHTS[0], check_out_date: "2026-11-26", nights: 2, adults: 2, children: 0,
          meal_plan: null, meal_plan_name: null, rate_plan: null, special_requests: null,
          eta: null, late_arrival: 0, occupants: [], booked_check_out_date: null, early_departure_reason: null,
          guest: { name: "G-1", full_name: "Priya Guest", phone: "+919876543210", email: "p@example.com",
            vip: 0, blacklisted: 0, stays: 1, last_stay: null },
          booker: null, cancellation: null,
          night_rates: NIGHTS.map((date) => ({
            date,
            base_rate: 8474.58,
            rate: 7627.12,
            discount_amount: 1000,
            discount_type: "Percent",
            discount_value: 10,
            entry_base_rate: 10000,
            entry_rate: 9000,
            demand_premium_pct: 0,
            charged: false,
          })),
          discount: { discount_type: "Percent", discount_value: 10, discount_reason: "RATE-PROMO", fixed_rate: 0 },
          precheckin_status: "Not Started", precheckin_token: null, id_document: null, id_document_source: null,
          id_document_on: null, id_document_discarded: 0, precheckin_verified_by: null, precheckin_verified_on: null,
          amount_after_tax: 18000, advance_paid: 0, company: null, travel_agent: null, folio_name: "FOLIO-99",
          money: { total: 18000, paid: 0, due: 18000, source: "folio", has_folio: true },
          deposit: null, security_deposit: null,
          actions: { can_check_in: true, can_check_out: false, can_cancel: true, can_no_show: false,
            can_amend: true, can_set_rate: false, can_reinstate: false, can_reverse_check_in: false },
          warnings: { id_document_missing: false, id_unverified: false, channel_review_needed: false },
          checkin_backfill: [],
        })
      }
      return Promise.resolve([])
    })

    await act(async () => root.render(
      <MemoryRouter>
        <ReservationDetail row={{ name: "RES-99" }} reload={() => undefined} onClose={() => undefined} />
      </MemoryRouter>,
    ))
    await until(() => expect(host.textContent).toContain("Confirmed rate / night"))

    const fieldValue = (label: string) => {
      const dt = Array.from(host.querySelectorAll("dt")).find((el) => el.textContent?.trim() === label)
      return dt?.parentElement?.querySelector("dd")?.textContent?.trim() ?? ""
    }
    expect(fieldValue("Base rate / night")).toContain("10,000")
    expect(fieldValue("Confirmed rate / night")).toContain("9,000")
    expect(fieldValue("Confirmed rate / night")).not.toContain("7,627")
  })
})

describe("P2-1: 3-decimal currency (KWD) precision in entryBase and DiscountPanel", () => {
  it("preserves 3 decimals in entryBase when places=3", () => {
    expect(entryBase({ rate: 11.111, gst_rate: 0, discount_amount: 1.234 }, false, 3)).toBe(12.345)
    expect(entryBase({ rate: 8.182, gst_rate: 10, discount_amount: 1.0 }, true, 3)).toBe(10)
  })

  it("uses step 0.001 for Amount inputs on a 3-decimal currency (KWD)", async () => {
    localeState.currency = "KWD"
    localeState.currency_symbol = "KWD "
    localeState.locale = "en-KW"

    await act(async () => root.render(
      <DiscountPanel
        value={{ ...emptyDiscount(), type: "Amount", value: "1.234", reason: "RATE-PROMO" }}
        onChange={() => undefined}
        reasons={REASONS}
        eligible={true}
        governance={null}
        nights={[{ date: NIGHTS[0], base_rate: 12.345 }]}
        supervisor=""
        pin=""
        onSupervisor={() => undefined}
        onPin={() => undefined}
        money={(n) => `KWD ${n.toFixed(3)}`}
      />,
    ))

    const stayInput = host.querySelector<HTMLInputElement>('input[aria-label="Discount value"]')!
    expect(stayInput.getAttribute("step")).toBe("0.001")

    const dailyToggle = Array.from(host.querySelectorAll("button")).find(
      (b) => b.textContent?.includes("Per-night daily details"),
    )!
    await act(async () => dailyToggle.click())
    const rowInput = host.querySelector<HTMLInputElement>('tr[data-night="2026-11-24"] input[type="number"]')!
    expect(rowInput.getAttribute("step")).toBe("0.001")
  })
})

describe("BookingDialog & Walk-in quote rail discount reason", () => {
  it.each([
    ["New booking", false],
    ["Walk-in", true],
  ])("shows the full discount reason without truncation in %s", async (_label, walkIn) => {
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.booking_options") {
        return Promise.resolve({
          property: { name: "Tank Hill Lodge", pets_allowed: 0, rates_include_tax: 1, checkin_time: "14:00", checkout_time: "11:00" },
          room_types: [{ name: "DLX", room_type_name: "Deluxe Room", base_price: 150000, adults_capacity: 2, base_occupancy: 2, children_capacity: 1 }],
          rate_plans: [{ name: "STD", rate_plan_name: "Standard Best Available", discount_eligible: 1 }],
          meal_plans: [],
          companies: [],
          travel_agents: [],
          rooms: [{ name: "RM-103", room_number: "103", room_type: "DLX", housekeeping_status: "Inspected" }],
          rate_plan_required: false,
          can_discount: true,
          rate_override_limit_pct: 15,
          discount_reasons: REASONS,
          can_set_rate: false,
          available_rooms: [{ name: "RM-103", room_number: "103", room_type: "DLX", housekeeping_status: "Inspected" }],
        })
      }
      if (method === "kamra.rates.rate_options") {
        return Promise.resolve({
          nights: [],
          plans: [],
          voucher: null,
          plain: { room_total: 150000, amount_after_tax: 150000, discount: 0 },
          tax_inclusive: true,
        })
      }
      if (method === "kamra.api.get_quote") {
        return Promise.resolve({
          nights: 1,
          room_total: 135000,
          room_base_total: 150000,
          reservation_discount_total: 15000,
          meal_total: 0,
          discount: 15000,
          voucher_discount: 0,
          amount_before_tax: 135000,
          tax_percent: 0,
          tax_amount: 0,
          amount_after_tax: 135000,
          advance: { percent: 0, season: null, amount: 0 },
          tax_basis: null,
          cancellation: { free_now: true, free_until: null, fee_basis: "None", fee_label: "", non_refundable_season: null },
          nightly: [{ date: NIGHTS[0], rate: 135000, base_rate: 150000, discount_amount: 15000, gst_rate: 0 }],
          discount_governance: {
            eligible: true, base_total: 150000, discount_total: 15000, net_total: 135000,
            effective_pct: 10, max_pct: 10, limit_pct: 15, needs_supervisor: false,
            can_set_rate: false, agent: false, comp: false, floor_breach: [],
            reason_requires_supervisor: false, supervisors: [],
          },
        })
      }
      if (method === "kamra.api.available_rooms") {
        return Promise.resolve([{ name: "RM-103", room_number: "103", room_type: "DLX", housekeeping_status: "Inspected" }])
      }
      if (method === "kamra.cashier.my_till") return Promise.resolve({ open: true })
      if (method === "kamra.ledger.list_exchange_rates") return Promise.resolve({})
      return Promise.resolve(null)
    })

    await act(async () => root.render(
      <MemoryRouter>
        <BookingDialog initial={{ room_type: "DLX", walkIn }} onClose={() => undefined} onBooked={() => undefined} />
      </MemoryRouter>,
    ))

    await until(() => expect(host.querySelector('input[aria-label="Discount value"]')).not.toBeNull())
    await typeInto(host.querySelector<HTMLInputElement>('input[aria-label="Discount value"]')!, "10")
    await choose(host.querySelector<HTMLSelectElement>('select[aria-label="Discount reason"]')!, "RATE-PROMO")

    await until(() => {
      const rail = host.querySelector("aside")!
      const span = Array.from(rail.querySelectorAll("span")).find(
        (s) => s.textContent?.trim() === "Discount (Promotional rate change)",
      )
      expect(span, "full discount reason span").toBeDefined()
    })

    const rail = host.querySelector("aside")!
    const span = Array.from(rail.querySelectorAll("span")).find(
      (s) => s.textContent?.trim() === "Discount (Promotional rate change)",
    )!
    expect(span.className).not.toContain("truncate")
    expect(span.className).toContain("break-words")
    expect(span.getAttribute("title")).toBe("Discount (Promotional rate change)")
  })
})
