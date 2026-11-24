// @vitest-environment jsdom
/* Reservation discounts, round 3 (Gap Record #8555, round-2 adversarial
 * review): CI runs these tests (P1-A); the Stay rate panel asks for its
 * reason codes once and lists each once (P1-B), seeds a manager's fixed-rate
 * override from the stay (P2-E) and asks for another manager's PIN when an
 * agreed rate stacked on a discount needs one (P0-B); the booking dialog
 * opens with the nights it was given, a walk-in starts today and the Total
 * names the quote's own dates (P1-C); the quote rail's discount rows keep
 * label and amount apart (P2-A); a long guest email stays in its column on
 * the reservation screen (P2-B). */
import fs from "node:fs"
import path from "node:path"
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const { rpc, auth } = vi.hoisted(() => {
  // Node's own localStorage global hides jsdom's; lib/api reads it on import
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return { rpc: vi.fn(), auth: { roles: ["Front Desk"] as string[] } }
})
vi.mock("../src/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api")>()
  const call = (method: string, params?: Record<string, unknown>) => rpc(method, params ?? {})
  return {
    ...actual,
    call,
    getCurrentProperty: () => "Test Hotel",
    getBookingOptions: () => call("kamra.api.booking_options", { property: "Test Hotel" }),
    getQuote: (params: Record<string, unknown>) => call("kamra.api.get_quote", { property: "Test Hotel", ...params }),
    createBooking: (params: Record<string, unknown>) =>
      call("kamra.api.create_booking", { property: "Test Hotel", ...params }),
    guestAlert: async () => ({ guest: null, blacklisted: 0, blacklist_reason: null }),
    guestSearch: async () => [],
    reservationDetail: (reservation: string) => call("kamra.api.reservation_detail", { reservation }),
  }
})
vi.mock("../src/lib/auth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/lib/auth")>()),
  useAuth: () => ({ status: "authed", user: "desk@test", roles: auth.roles,
    refresh: async () => {}, signOut: async () => {} }),
}))
vi.mock("../src/lib/realtime", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/lib/realtime")>()),
  useRealtime: () => {},
}))

import AgentCards from "../src/components/AgentCards"
import AgreedRatePanel from "../src/components/AgreedRatePanel"
import { BookingDialog } from "../src/components/BookingDialog"
import ReservationDetail from "../src/screens/ReservationDetail"
import { shiftDate } from "../src/lib/stay"
import { formatDate } from "../src/lib/utils"

const NIGHTS = ["2031-03-17", "2031-03-18"]
const REASONS = [
  { name: "RATE-PROMO", code: "RATE-PROMO", description: "Promotional rate change", category: "Rate Change", requires_supervisor: 0 },
  { name: "RATE-MGR", code: "RATE-MGR", description: "Manager rate override", category: "Rate Change", requires_supervisor: 1 },
  { name: "ADJ-RATE", code: "ADJ-RATE", description: "Rate correction", category: "Adjust", requires_supervisor: 0 },
  { name: "VOID-ERR", code: "VOID-ERR", description: "Keyed in error", category: "Void", requires_supervisor: 0 },
]
const governance = (over: Record<string, unknown> = {}) => ({
  base_total: 2000, discount_total: 200, net_total: 1800, effective_pct: 10, max_pct: 10,
  limit_pct: 10, needs_supervisor: false, can_set_rate: false, agent: false, eligible: true,
  comp: false, floor_breach: [], reason_requires_supervisor: false, supervisors: [], ...over,
})

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  host = document.createElement("div")
  document.body.append(host)
  root = createRoot(host)
  auth.roles = ["Front Desk"]
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const text = () => host.textContent ?? ""
const byLabel = <T extends Element>(label: string, scope: ParentNode = host) =>
  scope.querySelector<T>(`[aria-label="${label}"]`)
async function typeInto(node: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
  await act(async () => {
    setter.call(node, value)
    node.dispatchEvent(new Event("input", { bubbles: true }))
  })
}
async function choose(node: HTMLSelectElement, value: string) {
  await act(async () => {
    node.value = value
    node.dispatchEvent(new Event("change", { bubbles: true }))
  })
}
const button = (label: string) =>
  Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.trim() === label)
async function press(label: string) {
  expect(button(label), `Missing button: ${label}`).toBeDefined()
  await act(async () => button(label)!.click())
}
const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 0)))
const until = (check: () => void | Promise<void>) =>
  vi.waitFor(async () => { await settle(); await check() }, { timeout: 3000, interval: 20 })
const calls = (method: string) => rpc.mock.calls.filter(([m]) => m === method).map(([, p]) => p as Record<string, unknown>)
const optionValues = (select: HTMLSelectElement | null) =>
  Array.from(select?.querySelectorAll("option") ?? []).map((o) => o.value)
const localToday = () => {
  const d = new Date()
  return `${d.getFullYear()}-${`${d.getMonth() + 1}`.padStart(2, "0")}-${`${d.getDate()}`.padStart(2, "0")}`
}

describe("CI (P1-A)", () => {
  it("runs the vitest suite in the Frontend typecheck & build job", () => {
    const ci = fs.readFileSync(path.resolve(process.cwd(), "../.github/workflows/ci.yml"), "utf8")
    const start = ci.indexOf("\n  frontend:")
    const job = ci.slice(start, ci.indexOf("\n  contract:", start))
    expect(job).toContain("name: Frontend typecheck & build")
    expect(job).toMatch(/run: npm test\s*\n\s+working-directory: frontend/)
  })
})

describe("Stay rate & daily details panel", () => {
  const discountPlan = {
    ...governance(), current_amount: 2000, new_amount: 1800, difference: -200, needs_confirmation: true,
    nights: NIGHTS.map((date) => ({ date, base_rate: 1000, discount_amount: 100, rate: 900, charged: false })),
  }
  let planOver: Record<string, unknown> = {}
  let ratePreview: Record<string, unknown> = {}
  beforeEach(() => {
    planOver = {}
    ratePreview = {}
    rpc.mockReset().mockImplementation((method: string) => {
      // a server that answers every category with every code: the panel
      // must still list each code once, and only the ones it can use
      if (method === "kamra.ledger.list_reason_codes") return Promise.resolve(REASONS)
      if (method === "kamra.api.discount_preview") return Promise.resolve({ ...discountPlan, ...planOver })
      if (method === "kamra.api.rate_override_preview") return Promise.resolve(ratePreview)
      if (method === "kamra.api.set_agreed_rate") return Promise.resolve({ ok: true })
      return Promise.resolve(null)
    })
  })
  const nightRates = NIGHTS.map((date) => ({ date, rate: 900, base_rate: 1000 }))

  it("asks for its reason codes once and lists each code once (P1-B)", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {})
    // the categories are the server's (reservation_detail.reason_categories, R6)
    await act(async () => root.render(<AgreedRatePanel reservation="RES-9" onSaved={() => {}} nightRates={nightRates}
      reasonCategories={{ discount: ["Rate Change", "Adjust"], agreed_rate: ["Rate Change"] }} />))
    await press("Stay rate & daily details")
    await until(() => expect(optionValues(byLabel("Discount reason")).length).toBeGreaterThan(1))
    for (let i = 0; i < 5; i++) await settle()
    expect(calls("kamra.ledger.list_reason_codes")).toHaveLength(1)
    const discountCodes = optionValues(byLabel("Discount reason"))
    expect(discountCodes.sort()).toEqual(["", "ADJ-RATE", "RATE-MGR", "RATE-PROMO"])
    await press("Fixed rate")
    const rateCodes = optionValues(byLabel("Reason…"))
    expect(rateCodes.sort()).toEqual(["", "RATE-MGR", "RATE-PROMO"])
    expect(calls("kamra.ledger.list_reason_codes")).toHaveLength(1)
    expect(errors.mock.calls.some((args) => String(args[0]).includes("same key"))).toBe(false)
  })

  it("reopens a manager's fixed-rate stay with the override on (P2-E)", async () => {
    planOver = { eligible: false, can_set_rate: true }
    const current = { discount_type: "Percent" as const, discount_value: 10, discount_reason: "RATE-PROMO", fixed_rate: 1 }
    await act(async () => root.render(<AgreedRatePanel reservation="RES-9" onSaved={() => {}} canOverrideFixed
      nightRates={nightRates} current={current} />))
    await press("Stay rate & daily details")
    await until(() => expect(calls("kamra.api.discount_preview").length).toBeGreaterThan(0))
    expect(calls("kamra.api.discount_preview")[0]).toMatchObject({ discount_type: "Percent", discount_value: 10, fixed_rate: 1 })
    const override = Array.from(host.querySelectorAll("label")).find((l) => l.textContent?.includes("Fixed rate override"))
    expect(override?.querySelector<HTMLInputElement>("input[type=checkbox]")?.checked).toBe(true)
    expect(byLabel<HTMLInputElement>("Discount value")!.disabled).toBe(false)
    expect(byLabel<HTMLInputElement>("Discount value")!.value).toBe("10")
    // the front desk cannot override: it never sends one
    await press("Cancel")
    rpc.mockClear()
    planOver = { eligible: false, can_set_rate: false }
    await act(async () => root.render(<AgreedRatePanel reservation="RES-9" onSaved={() => {}}
      nightRates={nightRates} current={current} />))
    await press("Stay rate & daily details")
    await until(() => expect(calls("kamra.api.discount_preview").length).toBeGreaterThan(0))
    expect(calls("kamra.api.discount_preview")[0]).toMatchObject({ fixed_rate: 0 })
  })

  it("asks a manager for another manager's PIN when an agreed rate makes a comp (P0-B)", async () => {
    ratePreview = {
      current_amount: 1000, new_amount: 0, difference: -1000, current_nightly: 500, agreed_rate: 500,
      reduction_pct: 0, limit_pct: 15, can_set_rate: true, raises: false, needs_supervisor: true,
      needs_confirmation: true, nights: NIGHTS.map((date) => ({ date, rate: 0 })),
      supervisors: [{ user: "finance@test", full_name: "Finance Lead" }],
      discount_governance: governance({ comp: true, needs_supervisor: true, can_set_rate: true,
        supervisors: [{ user: "finance@test", full_name: "Finance Lead" }] }),
    }
    await act(async () => root.render(<AgreedRatePanel reservation="RES-9" onSaved={() => {}} nightRates={nightRates}
      current={{ discount_type: "Amount", discount_value: 500, discount_reason: "RATE-PROMO" }} />))
    await press("Stay rate & daily details")
    await press("Fixed rate")
    await typeInto(host.querySelector<HTMLInputElement>("input[type=number]")!, "500")
    await until(() => expect(optionValues(byLabel("Reason…")).length).toBeGreaterThan(1))
    await choose(byLabel<HTMLSelectElement>("Reason…")!, "RATE-PROMO")
    await until(() => expect(calls("kamra.api.rate_override_preview").length).toBeGreaterThan(0))
    await until(() => expect(byLabel("Manager PIN")).not.toBeNull())
    expect(text()).toContain("A 100% discount (comp) needs a manager's sign-off.")
    expect(button("Save agreed rate")!.disabled).toBe(true)
    await choose(byLabel<HTMLSelectElement>("Manager")!, "finance@test")
    await typeInto(byLabel<HTMLInputElement>("Manager PIN")!, "5678")
    expect(button("Save agreed rate")!.disabled).toBe(false)
    await press("Save agreed rate")
    await until(() => expect(calls("kamra.api.set_agreed_rate")).toHaveLength(1))
    expect(calls("kamra.api.set_agreed_rate")[0]).toMatchObject({
      reservation: "RES-9", agreed_rate: 500, reason_code: "RATE-PROMO", supervisor: "finance@test", pin: "5678" })
  })
})

const OPTIONS = {
  room_types: [{ name: "STD", room_type_name: "Standard", base_price: 1000, adults_capacity: 2, children_capacity: 1 }],
  meal_plans: [], companies: [], travel_agents: [], experiences: [], ota_channels: [], ota_commission: {},
  rate_plans: [{ name: "TARIFF", rate_plan_name: "Corporate tariff", code: "CORP", discount_eligible: 1 }],
  default_plan: "TARIFF", rate_plan_required: true, can_set_rate: false,
  can_discount: true, rate_override_limit_pct: 10, discount_reasons: REASONS.slice(0, 3),
  property: { sell_message: "", free_cancel_days: 0, cancellation_fee: "None", no_show_charge: "None",
    deposit_pct: 0, country: "Kenya", pets_allowed: 0, pet_charge: 0, pet_charge_basis: "Per stay",
    checkin_time: "14:00", checkout_time: "11:00" },
}

/** A quote for exactly the dates asked (or the nights quoteOver says). */
function quoteFor(params: Record<string, unknown>, nights?: number) {
  const ci = String(params.check_in_date)
  const n = nights ?? Math.max(1, Math.round(
    (new Date(String(params.check_out_date)).getTime() - new Date(ci).getTime()) / 86_400_000))
  const value = Number(params.discount_value ?? 0)
  const off = params.discount_type === "Percent" ? 1000 * value / 100 : params.discount_type === "Amount" ? value : 0
  const net = 1000 - off
  return {
    nights: n, nightly: Array.from({ length: n }, (_, i) => ({ date: shiftDate(ci, i), rate: net, base_rate: 1000,
      discount_amount: off, discount_type: params.discount_type ?? null, discount_value: value, fixed_rate: 0 })),
    room_total: n * net, room_base_total: n * 1000, reservation_discount_total: n * off,
    meal_total: 0, discount: n * off, voucher_discount: 0, rate_plan: params.rate_plan,
    amount_before_tax: n * net, tax_percent: 0, tax_amount: 0, amount_after_tax: n * net,
    advance: { percent: 0, season: null, amount: 0 }, tax_basis: null,
    cancellation: { free_now: true, free_until: null, fee_basis: "None", fee_label: "", non_refundable_season: null },
    ...(params.discount_type ? { discount_governance: governance({ effective_pct: value, max_pct: value }) } : {}),
  }
}

describe("Booking dialog", () => {
  let quoteOver: (params: Record<string, unknown>) => Record<string, unknown> = () => ({})
  beforeEach(() => {
    quoteOver = () => ({})
    rpc.mockReset().mockImplementation((method: string, params: Record<string, unknown>) => {
      switch (method) {
        case "kamra.api.booking_options": return Promise.resolve(OPTIONS)
        case "kamra.api.get_quote": return Promise.resolve({ ...quoteFor(params), ...quoteOver(params) })
        case "kamra.api.available_rooms": return Promise.resolve([{ name: "STD-101", room_number: "101", housekeeping_status: "Clean" }])
        case "kamra.api.property_locale": return Promise.resolve({ currency: "KES", currency_symbol: "KES ", locale: "en",
          tax_label: "VAT", tax_rates: [0], payment_modes: ["Cash"] })
        case "kamra.rates.rate_options": return Promise.resolve({ nights: [], plans: [], voucher: null,
          plain: { room_total: 2000, amount_after_tax: 2000, discount: 0 }, tax_inclusive: false })
        case "kamra.cashier.my_till": return Promise.resolve({ open: true })
        case "kamra.ledger.list_exchange_rates": return Promise.resolve({})
        default: return Promise.resolve(null)
      }
    })
  })
  const lastQuote = () => calls("kamra.api.get_quote").at(-1)!

  async function open(initial: Record<string, unknown>) {
    await act(async () => root.render(<MemoryRouter><BookingDialog initial={{ room_type: "STD", ...initial }}
      onClose={() => {}} onBooked={() => {}} /></MemoryRouter>))
    await until(() => {
      expect(calls("kamra.api.get_quote").length).toBeGreaterThan(0)
      expect(text()).toContain("Total")
    })
  }

  it("opens with the nights it was given (P1-C)", async () => {
    await open({ nights: 3 })
    const q = lastQuote()
    expect(q.check_out_date).toBe(shiftDate(String(q.check_in_date), 3))
  })

  it("never reads a guest's past stays as the nights of this one (P1-C)", async () => {
    await open({ guest: "G-1", guest_name: "Ada Guest", stays: 7 })
    const q = lastQuote()
    expect(q.check_out_date).toBe(shiftDate(String(q.check_in_date), 1))
  })

  it("starts a walk-in today, whatever date it was opened with (P1-C)", async () => {
    await open({ walkIn: true, date: "2031-03-17" })
    expect(lastQuote().check_in_date).toBe(localToday())
  })

  it("names the quote's own dates under the Total (P1-C)", async () => {
    quoteOver = (params) => quoteFor(params, 3)
    await open({})
    const ci = String(lastQuote().check_in_date)
    await until(() => expect(text()).toContain(`${formatDate(ci)} → ${formatDate(shiftDate(ci, 3))}`))
  })

  it("shows a day-use quote's slot under the Total (P1-C)", async () => {
    quoteOver = (params) => ({ ...quoteFor(params, 1), day_use: true })
    await open({})
    const ci = String(lastQuote().check_in_date)
    await until(() => expect(text()).toContain(`${formatDate(ci)} · 10:00-18:00`))
  })

  it("keeps the discount rows' labels and amounts apart, with no empty reason (P2-A)", async () => {
    await open({})
    await typeInto(byLabel<HTMLInputElement>("Discount value")!, "10")
    await until(() => expect(text()).toContain("Net room rate"))
    expect(text()).not.toContain("Discount ()")
    const rail = host.querySelector("aside")!
    const label = (name: string) => Array.from(rail.querySelectorAll("span")).find((s) => s.textContent?.trim() === name)
    for (const name of ["Base room rate", "Discount", "Net room rate"]) {
      const span = label(name)
      expect(span, name).toBeDefined()
      const row = span!.parentElement!
      expect(row.className, name).toContain("gap-3")
      const amount = row.lastElementChild!
      expect(amount).not.toBe(span)
      for (const cls of ["shrink-0", "whitespace-nowrap", "tabular-nums"]) expect(amount.className, `${name}: ${cls}`).toContain(cls)
    }
    await choose(byLabel<HTMLSelectElement>("Discount reason")!, "RATE-PROMO")
    await until(() => expect(label("Discount (Promotional rate change)")).toBeDefined())
  })
})

describe("Agent quote card (P1-C)", () => {
  it("books the quoted nights, not a count of stays", async () => {
    const openBooking = vi.fn()
    const card = { type: "quote" as const, room_type: "STD", check_in_date: "2031-03-17",
      check_out_date: "2031-03-20", nights: 3, adults: 2, children: 0, total: 3000 }
    await act(async () => root.render(<MemoryRouter><Routes>
      <Route element={<Outlet context={{ refreshKey: 0, canBook: true, openBooking }} />}>
        <Route index element={<AgentCards cards={[card]} />} />
      </Route>
    </Routes></MemoryRouter>))
    await press("Book")
    expect(openBooking).toHaveBeenCalledTimes(1)
    expect(openBooking.mock.calls[0][0]).toMatchObject({ room_type: "STD", date: "2031-03-17", nights: 3 })
    expect(openBooking.mock.calls[0][0]).not.toHaveProperty("stays")
  })
})

describe("Reservation screen", () => {
  const EMAIL = "a.very.long.guest.address.for.overflow.checks@example-travel-agency.example"
  const detail = (over: Record<string, unknown> = {}) => ({
    name: "RES-9", status: "Confirmed", booking_type: "Individual", source: "Direct", channel: null,
    property: "Test Hotel", room: "STD-101", room_number: "101", room_type: "STD", room_type_name: "Standard",
    check_in_date: NIGHTS[0], check_out_date: "2031-03-19", nights: 2, adults: 2, children: 0,
    guest: { name: "G-1", full_name: "Ada Guest", phone: "+254700000000", email: EMAIL, vip: 0,
      blacklisted: 0, stays: 1, last_stay: null },
    night_rates: NIGHTS.map((date) => ({ date, rate: 900, base_rate: 1000, discount_amount: 100,
      discount_type: null, discount_value: 0, entry_base_rate: 1000, entry_rate: 900, charged: false })),
    discount: { discount_type: "Percent", discount_value: 10, discount_reason: "RATE-PROMO", fixed_rate: 1 },
    // the server says who may fix a rate (R6): a Revenue Manager may
    actions: { can_amend: true, can_set_rate: true }, warnings: {}, occupants: [], checkin_backfill: [],
    amount_after_tax: 1800, advance_paid: 0,
    money: { total: 1800, paid: 0, due: 1800, has_folio: false, source: "booking" }, ...over,
  })
  beforeEach(() => {
    rpc.mockReset().mockImplementation((method: string) => {
      if (method === "kamra.api.reservation_detail") return Promise.resolve(detail())
      if (method === "kamra.ledger.list_reason_codes") return Promise.resolve(REASONS)
      if (method === "kamra.api.discount_preview") return Promise.resolve({
        ...governance({ eligible: false, can_set_rate: true }), current_amount: 1800, new_amount: 1800,
        difference: 0, needs_confirmation: false,
        nights: NIGHTS.map((date) => ({ date, base_rate: 1000, discount_amount: 100, rate: 900, charged: false })) })
      return Promise.resolve(null)
    })
  })
  async function show() {
    await act(async () => root.render(<MemoryRouter><ReservationDetail row={{ name: "RES-9" }}
      reload={() => {}} onClose={() => {}} /></MemoryRouter>))
    await until(() => expect(text()).toContain(EMAIL))
  }

  it("keeps a long guest email inside its own column (P2-B)", async () => {
    await show()
    const value = Array.from(host.querySelectorAll("dd")).find((dd) => dd.textContent === EMAIL)!
    expect(value.className).toContain("break-words")
    expect(value.className).toContain("[overflow-wrap:anywhere]")
    expect(value.parentElement!.className).toContain("min-w-0")
  })

  it("hands a manager's fixed-rate right to the Stay rate panel (P2-E)", async () => {
    auth.roles = ["Revenue Manager"]
    await show()
    await press("Stay rate & daily details")
    await until(() => expect(calls("kamra.api.discount_preview").length).toBeGreaterThan(0))
    expect(calls("kamra.api.discount_preview")[0]).toMatchObject({ reservation: "RES-9", fixed_rate: 1 })
  })
})
