// @vitest-environment jsdom
/* Reservation discounts, round 4 (Gap Record #8555; R3 code review and the
 * SAP S/4HANA / Oracle OPERA Cloud parity check): a date amendment and a
 * room move ask for the discount's sign-off in place (R4-7); the Fixed rate
 * tab says it replaces the discount and names a rate-floor breach (R4-3,
 * R4-8); the reason list keeps one row per code (R4-12); the agent guide
 * books the nights it asked for (R4-14); the reservation screen shows the
 * discount, its base and the fixed rate without opening the editor, Daily
 * Details nights take a reason of their own and a discount takes an
 * optional note (R4-15). */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
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
    amendStayPreview: (reservation: string, check_in_date: string, check_out_date: string) =>
      call("kamra.api.amend_stay_preview", { reservation, check_in_date, check_out_date }),
    amendStay: (reservation: string, check_in_date: string, check_out_date: string, confirm_price = 0,
      signOff: Record<string, unknown> = {}) =>
      call("kamra.api.amend_stay", { reservation, check_in_date, check_out_date, confirm_price, ...signOff }),
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

import AgreedRatePanel from "../src/components/AgreedRatePanel"
import { flows, type Answers } from "../src/components/AgentGuide"
import { BookingDialog } from "../src/components/BookingDialog"
import RoomChangeDialog from "../src/components/RoomChangeDialog"
import ReservationDetail from "../src/screens/ReservationDetail"

const NIGHTS = ["2031-03-17", "2031-03-18"]
const REASONS = [
  { name: "RATE-PROMO", code: "RATE-PROMO", description: "Promotional rate change", category: "Rate Change", requires_supervisor: 0 },
  { name: "RATE-MGR", code: "RATE-MGR", description: "Manager rate override", category: "Rate Change", requires_supervisor: 1 },
  { name: "ADJ-RATE", code: "ADJ-RATE", description: "Rate correction", category: "Adjust", requires_supervisor: 0 },
  { name: "VOID-ERR", code: "VOID-ERR", description: "Keyed in error", category: "Void", requires_supervisor: 0 },
]
const GM = { user: "gm@test", full_name: "General Manager" }
const governance = (over: Record<string, unknown> = {}) => ({
  base_total: 2000, discount_total: 200, net_total: 1800, effective_pct: 10, max_pct: 10,
  limit_pct: 10, needs_supervisor: false, can_set_rate: false, agent: false, eligible: true,
  comp: false, floor_breach: [], reason_requires_supervisor: false, supervisors: [], ...over,
})
const overLimit = () => governance({ max_pct: 30, needs_supervisor: true, supervisors: [GM] })

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
const labelled = (name: string) =>
  Array.from(host.querySelectorAll("label")).find((l) => l.querySelector("span")?.textContent?.trim() === name)
    ?.querySelector<HTMLInputElement>("input") ?? null

async function signOff() {
  await until(() => expect(byLabel("Manager PIN")).not.toBeNull())
  await choose(byLabel<HTMLSelectElement>("Manager")!, GM.user)
  await typeInto(byLabel<HTMLInputElement>("Manager PIN")!, "4321")
}

const detail = (over: Record<string, unknown> = {}) => ({
  name: "RES-9", status: "Confirmed", booking_type: "Individual", source: "Direct", channel: null,
  property: "Test Hotel", room: "STD-101", room_number: "101", room_type: "STD", room_type_name: "Standard",
  check_in_date: NIGHTS[0], check_out_date: "2031-03-19", nights: 2, adults: 2, children: 0,
  guest: { name: "G-1", full_name: "Ada Guest", phone: "+254700000000", email: "ada@example.com", vip: 0,
    blacklisted: 0, stays: 1, last_stay: null },
  night_rates: NIGHTS.map((date) => ({ date, rate: 700, base_rate: 1000, discount_amount: 300,
    discount_type: null, discount_value: 0, entry_base_rate: 1000, entry_rate: 700, charged: false })),
  discount: { discount_type: "Percent", discount_value: 30, discount_reason: "RATE-PROMO", fixed_rate: 0 },
  actions: { can_amend: true }, warnings: {}, occupants: [], checkin_backfill: [],
  amount_after_tax: 1400, advance_paid: 0,
  money: { total: 1400, paid: 0, due: 1400, has_folio: false, source: "booking" }, ...over,
})

describe("Reservation screen", () => {
  let shown = detail()
  beforeEach(() => {
    shown = detail()
    rpc.mockReset().mockImplementation((method: string) => {
      if (method === "kamra.api.reservation_detail") return Promise.resolve(shown)
      if (method === "kamra.api.amend_stay_preview") return Promise.resolve({
        current_amount: 1400, new_amount: 2100, difference: 700, needs_confirmation: true,
        nights: [...NIGHTS, "2031-03-19"].map((date) => ({ date, rate: 700, demand_premium_pct: 0 })),
        discount_governance: overLimit(), needs_supervisor: true, supervisors: [GM] })
      if (method === "kamra.api.amend_stay") return Promise.resolve({ ok: true, nights: 3, amount_after_tax: 2100 })
      return Promise.resolve(null)
    })
  })
  async function show() {
    await act(async () => root.render(<MemoryRouter><ReservationDetail row={{ name: "RES-9" }}
      reload={() => {}} onClose={() => {}} /></MemoryRouter>))
    await until(() => expect(text()).toContain("Ada Guest"))
  }

  it("asks for the discount's sign-off before new dates are confirmed (R4-7)", async () => {
    await show()
    await typeInto(labelled("Check-out")!, "2031-03-20")
    await until(() => expect(calls("kamra.api.amend_stay_preview").length).toBeGreaterThan(0))
    await until(() => expect(text()).toContain("A cut of 30% is over the front desk's limit of 10%."))
    expect(button("Confirm new dates and price")!.disabled).toBe(true)
    await signOff()
    expect(button("Confirm new dates and price")!.disabled).toBe(false)
    await press("Confirm new dates and price")
    await until(() => expect(calls("kamra.api.amend_stay")).toHaveLength(1))
    expect(calls("kamra.api.amend_stay")[0]).toMatchObject({ reservation: "RES-9", check_in_date: NIGHTS[0],
      check_out_date: "2031-03-20", confirm_price: 1, supervisor_user: GM.user, supervisor_pin: "4321" })
  })

  it("shows the discount, its base and the fixed rate without opening the editor (R4-15)", async () => {
    shown = detail({ discount: { discount_type: "Percent", discount_value: 30, discount_reason: "RATE-PROMO", fixed_rate: 1 } })
    await show()
    expect(text()).toContain("30% off · RATE-PROMO")
    expect(text()).toContain("Base rate / night")
    const badge = Array.from(host.querySelectorAll("span")).find((s) => s.textContent?.trim() === "Fixed rate")
    expect(badge, "Fixed rate badge").toBeDefined()
  })
})

describe("Room move dialog (R4-7)", () => {
  beforeEach(() => {
    rpc.mockReset().mockImplementation((method: string) => {
      if (method === "kamra.api.movable_rooms") return Promise.resolve([
        { name: "STD-101", room_number: "101", room_type: "STD", room_type_name: "Standard", floor: "1",
          housekeeping_status: "Clean", free: true, same_type: true, current: true },
        { name: "DLX-201", room_number: "201", room_type: "DLX", room_type_name: "Deluxe", floor: "2",
          housekeeping_status: "Clean", free: true, same_type: false, current: false },
      ])
      if (method === "kamra.api.room_move_preview") return Promise.resolve({
        same_type: false, new_room_type: "DLX", new_room_type_name: "Deluxe", current_amount: 1400,
        new_amount: 1680, difference: 280, needs_confirmation: true, nights: [], auto_price: true,
        in_house: false, can_set_rate: false, discount_governance: overLimit(), needs_supervisor: true,
        supervisors: [GM] })
      if (method === "kamra.api.move_reservation") return Promise.resolve({ ok: true, room: "DLX-201", room_type: "DLX" })
      return Promise.resolve(null)
    })
  })

  it("asks for the discount's sign-off before the move", async () => {
    const moved = vi.fn()
    await act(async () => root.render(<RoomChangeDialog reservation="RES-9" currentRoomNumber="101"
      onClose={() => {}} onMoved={moved} />))
    await until(() => expect(button("All types (upgrade)")).toBeDefined())
    await press("All types (upgrade)")
    await until(() => expect(Array.from(host.querySelectorAll("button")).some((b) => b.textContent?.includes("201"))).toBe(true))
    await act(async () => Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.startsWith("201"))!.click())
    await until(() => expect(text()).toContain("A cut of 30% is over the front desk's limit of 10%."))
    expect(button("Move to 201")!.disabled).toBe(true)
    await signOff()
    expect(button("Move to 201")!.disabled).toBe(false)
    await press("Move to 201")
    await until(() => expect(calls("kamra.api.move_reservation")).toHaveLength(1))
    expect(calls("kamra.api.move_reservation")[0]).toMatchObject({ reservation: "RES-9", new_room: "DLX-201",
      confirm_price: 1, supervisor_user: GM.user, supervisor_pin: "4321" })
    expect(moved).toHaveBeenCalled()
  })
})

describe("Stay rate & daily details panel", () => {
  const nightRates = NIGHTS.map((date) => ({ date, rate: 900, base_rate: 1000 }))
  let reasons: typeof REASONS = REASONS
  let ratePreview: Record<string, unknown> = {}
  beforeEach(() => {
    reasons = REASONS
    ratePreview = {}
    rpc.mockReset().mockImplementation((method: string, params: Record<string, unknown>) => {
      if (method === "kamra.ledger.list_reason_codes") return Promise.resolve(reasons)
      if (method === "kamra.api.discount_preview") return Promise.resolve({
        ...governance(), current_amount: 2000, new_amount: 1800, difference: -200, needs_confirmation: true,
        nights: NIGHTS.map((date) => ({ date, base_rate: 1000, discount_amount: 100, rate: 900, charged: false,
          own: (params.night_discounts as { date: string }[] | undefined)?.some((n) => n.date === date) ?? false })) })
      if (method === "kamra.api.set_reservation_discount") return Promise.resolve({ ok: true })
      if (method === "kamra.api.rate_override_preview") return Promise.resolve(ratePreview)
      return Promise.resolve(null)
    })
  })
  async function openPanel(props: Partial<Parameters<typeof AgreedRatePanel>[0]> = {}) {
    await act(async () => root.render(<AgreedRatePanel reservation="RES-9" onSaved={() => {}}
      nightRates={nightRates} {...props} />))
    await press("Stay rate & daily details")
    await until(() => expect(optionValues(byLabel("Discount reason")).length).toBeGreaterThan(1))
  }

  it("lists a reason code the server sends twice only once (R4-12)", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {})
    reasons = [...REASONS, { ...REASONS[0] }, { ...REASONS[2] }]
    await openPanel()
    expect(optionValues(byLabel("Discount reason")).sort()).toEqual(["", "ADJ-RATE", "RATE-MGR", "RATE-PROMO"])
    await press("Fixed rate")
    expect(optionValues(byLabel("Reason…")).sort()).toEqual(["", "RATE-MGR", "RATE-PROMO"])
    expect(errors.mock.calls.some((args) => String(args[0]).includes("same key"))).toBe(false)
  })

  it("says the agreed rate replaces the discount and names a rate-floor breach (R4-3, R4-8)", async () => {
    ratePreview = {
      current_amount: 1800, new_amount: 1760, difference: -40, current_nightly: 900, agreed_rate: 880,
      reduction_pct: 12, limit_pct: 15, can_set_rate: false, raises: false, needs_supervisor: true,
      needs_confirmation: true, nights: NIGHTS.map((date) => ({ date, rate: 880 })), supervisors: [GM],
      floor_breach: NIGHTS,
    }
    await openPanel({ current: { discount_type: "Percent", discount_value: 10, discount_reason: "RATE-PROMO" } })
    await press("Fixed rate")
    expect(text()).toContain("The agreed rate replaces this stay's discount.")
    await typeInto(host.querySelector<HTMLInputElement>("input[type=number]")!, "880")
    await choose(byLabel<HTMLSelectElement>("Reason…")!, "RATE-PROMO")
    await until(() => expect(byLabel("Manager PIN")).not.toBeNull())
    expect(text()).toContain(`Below the rate floor on ${NIGHTS.join(", ")}.`)
    expect(text()).not.toContain("A cut of 12% is over the front desk's limit of 15%.")
  })

  it("gives a Daily Details night a reason of its own (R4-15)", async () => {
    await openPanel()
    await typeInto(byLabel<HTMLInputElement>("Discount value")!, "10")
    await choose(byLabel<HTMLSelectElement>("Discount reason")!, "RATE-PROMO")
    await press("Per-night daily details")
    await typeInto(host.querySelector<HTMLInputElement>(`tr[data-night="${NIGHTS[1]}"] input[type=number]`)!, "5")
    const own = byLabel<HTMLSelectElement>(`Discount reason ${NIGHTS[1]}`)
    expect(own, "the night's own reason").not.toBeNull()
    expect(optionValues(own).sort()).toEqual(["", "ADJ-RATE", "RATE-MGR", "RATE-PROMO"])
    await choose(own!, "ADJ-RATE")
    await until(() => expect(calls("kamra.api.discount_preview").at(-1)?.night_discounts).toEqual([
      { date: NIGHTS[1], discount_type: "Percent", discount_value: 5, discount_reason: "ADJ-RATE" }]))
  })

  it("saves an optional note with the discount, never with the preview (R4-15)", async () => {
    await openPanel()
    await typeInto(byLabel<HTMLInputElement>("Discount value")!, "10")
    await choose(byLabel<HTMLSelectElement>("Discount reason")!, "RATE-PROMO")
    expect(byLabel("Discount note"), "the note input").not.toBeNull()
    await typeInto(byLabel<HTMLInputElement>("Discount note")!, "Guest waited 40 minutes")
    await until(() => expect(button("Save discount")!.disabled).toBe(false))
    await press("Save discount")
    await until(() => expect(calls("kamra.api.set_reservation_discount")).toHaveLength(1))
    expect(calls("kamra.api.set_reservation_discount")[0]).toMatchObject({
      discount_type: "Percent", discount_value: 10, reason_code: "RATE-PROMO",
      discount_note: "Guest waited 40 minutes" })
    expect(calls("kamra.api.discount_preview").every((p) => !("discount_note" in p))).toBe(true)
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

describe("Booking dialog (R4-15)", () => {
  beforeEach(() => {
    rpc.mockReset().mockImplementation((method: string, params: Record<string, unknown>) => {
      const value = Number(params?.discount_value ?? 0)
      const off = params?.discount_type === "Percent" ? 1000 * value / 100 : 0
      switch (method) {
        case "kamra.api.booking_options": return Promise.resolve(OPTIONS)
        case "kamra.api.get_quote": return Promise.resolve({
          nights: 1, nightly: [{ date: String(params.check_in_date), rate: 1000 - off, base_rate: 1000,
            discount_amount: off, discount_type: params.discount_type ?? null, discount_value: value, fixed_rate: 0 }],
          room_total: 1000 - off, room_base_total: 1000, reservation_discount_total: off, meal_total: 0,
          discount: off, voucher_discount: 0, rate_plan: params.rate_plan, amount_before_tax: 1000 - off,
          tax_percent: 0, tax_amount: 0, amount_after_tax: 1000 - off,
          advance: { percent: 0, season: null, amount: 0 }, tax_basis: null,
          cancellation: { free_now: true, free_until: null, fee_basis: "None", fee_label: "", non_refundable_season: null },
          ...(params.discount_type ? { discount_governance: governance({ effective_pct: value, max_pct: value }) } : {}) })
        case "kamra.api.available_rooms": return Promise.resolve([{ name: "STD-101", room_number: "101", housekeeping_status: "Clean" }])
        case "kamra.api.property_locale": return Promise.resolve({ currency: "KES", currency_symbol: "KES ", locale: "en",
          tax_label: "VAT", tax_rates: [0], payment_modes: ["Cash"] })
        case "kamra.rates.rate_options": return Promise.resolve({ nights: [], plans: [], voucher: null,
          plain: { room_total: 1000, amount_after_tax: 1000, discount: 0 }, tax_inclusive: false })
        case "kamra.cashier.my_till": return Promise.resolve({ open: true })
        case "kamra.ledger.list_exchange_rates": return Promise.resolve({})
        case "kamra.api.create_booking": return Promise.resolve({ reservation: "RES-1", room: "STD-101", room_number: "101" })
        case "kamra.api.reservation_detail": return Promise.resolve({ deposit: null })
        default: return Promise.resolve(null)
      }
    })
  })

  it("books the discount with its optional note", async () => {
    await act(async () => root.render(<MemoryRouter><BookingDialog initial={{ room_type: "STD" }}
      onClose={() => {}} onBooked={() => {}} /></MemoryRouter>))
    await until(() => expect(text()).toContain("Total"))
    await typeInto(host.querySelector<HTMLInputElement>("#booking-guest")!, "Ada Guest")
    await typeInto(byLabel<HTMLInputElement>("Discount value")!, "5")
    await choose(byLabel<HTMLSelectElement>("Discount reason")!, "RATE-PROMO")
    expect(byLabel("Discount note"), "the note input").not.toBeNull()
    await typeInto(byLabel<HTMLInputElement>("Discount note")!, "Returning corporate guest")
    await until(() => expect(calls("kamra.api.get_quote").at(-1)).toMatchObject({ discount_value: 5 }))
    await press("Confirm booking")
    await until(() => expect(calls("kamra.api.create_booking")).toHaveLength(1))
    expect(calls("kamra.api.create_booking")[0]).toMatchObject({ discount_type: "Percent", discount_value: 5,
      discount_reason: "RATE-PROMO", discount_note: "Returning corporate guest" })
    expect(calls("kamra.api.get_quote").every((p) => !("discount_note" in p))).toBe(true)
  })
})

describe("Agent guide (R4-14)", () => {
  const answers = (): Answers => ({
    date: { label: "Tomorrow", value: "2031-03-17" },
    adults: { label: "3", value: "3" },
    nights: { label: "4", value: "4" },
    room_type: { label: "Standard", value: "STD" },
    guest: { label: "Ada Guest", value: "Ada Guest" },
    phone: { label: "Skip", value: "" },
  })

  it("opens the booking and walk-in forms with the nights and adults it asked for", () => {
    for (const key of ["booking", "walkin"] as const) {
      const out = flows()[key].finish(answers(), false)
      const form = "choices" in out ? out.choices?.find((c) => c.book) : undefined
      expect(form?.book, key).toMatchObject({ room_type: "STD", guest_name: "Ada Guest", nights: 4, adults: 3 })
    }
  })
})
