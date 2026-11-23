// @vitest-environment jsdom
/* Complimentary and House Use rate plans (OPERA Cloud rate code
 * Complimentary / House Use flags; the parity check's item 11b on #32): the
 * Rate Plans form sets a plan's category from the property's own list
 * (Settings, seeded with OPERA's), and a booking on a plan whose category
 * needs a sign-off asks for a manager's PIN in the dialog (OPERA's comp
 * authorizer, through the sign-off the discounts use) before it is sent. */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const { rpc } = vi.hoisted(() => {
  // Node's own localStorage global hides jsdom's; lib/api reads it on import
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return { rpc: vi.fn() }
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
  }
})

import { BookingDialog } from "../src/components/BookingDialog"
import { ratePlansConfig } from "../src/screens/configs"

const NIGHTS = ["2031-03-17", "2031-03-18"]
const GM = { user: "gm@test", full_name: "General Manager" }

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  host = document.createElement("div")
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
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
async function press(label: string, scope: ParentNode = host) {
  const button = Array.from(scope.querySelectorAll("button")).find((b) => b.textContent?.trim() === label)
  expect(button, `Missing button: ${label}`).toBeDefined()
  await act(async () => button!.click())
}
const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 0)))
const until = (check: () => void | Promise<void>) =>
  vi.waitFor(async () => { await settle(); await check() }, { timeout: 3000, interval: 20 })

const OPTIONS = {
  room_types: [{ name: "STD", room_type_name: "Standard", base_price: 1000, adults_capacity: 2, children_capacity: 1 }],
  meal_plans: [], companies: [], travel_agents: [], experiences: [], ota_channels: [], ota_commission: {},
  rate_plans: [
    { name: "BAR", rate_plan_name: "Best available", code: "BAR", discount_eligible: 1 },
    { name: "COMP", rate_plan_name: "Complimentary", code: "COMP", discount_eligible: 0 },
    { name: "STAFF", rate_plan_name: "Staff rate", code: "STAFF", discount_eligible: 0 },
  ],
  default_plan: "BAR", rate_plan_required: true, can_set_rate: false,
  can_discount: true, rate_override_limit_pct: 10, discount_reasons: [],
  property: { sell_message: "", free_cancel_days: 0, cancellation_fee: "None", no_show_charge: "None",
    deposit_pct: 0, country: "Kenya", pets_allowed: 0, pet_charge: 0, pet_charge_basis: "Per stay",
    checkin_time: "14:00", checkout_time: "11:00" },
}

// each plan's category, as the property configured them
const CATEGORY: Record<string, string> = { BAR: "Standard", COMP: "Complimentary", STAFF: "Staff" }

function quoteFor(params: Record<string, unknown>) {
  const category = CATEGORY[String(params.rate_plan)] ?? "Standard"
  const comp = category !== "Standard"
  const rate = comp ? 0 : 1000
  return {
    nights: 2, nightly: NIGHTS.map((date) => ({ date, rate, base_rate: rate, discount_amount: 0,
      discount_type: null, discount_value: 0, fixed_rate: 0 })),
    room_total: 2 * rate, room_base_total: 2 * rate, reservation_discount_total: 0,
    meal_total: 0, discount: 0, voucher_discount: 0, rate_plan: params.rate_plan,
    amount_before_tax: 2 * rate, tax_percent: 0, tax_amount: 0, amount_after_tax: 2 * rate,
    advance: { percent: 0, season: null, amount: 0 }, tax_basis: null,
    cancellation: { free_now: true, free_until: null, fee_basis: "None", fee_label: "", non_refundable_season: null },
    // kamra.api.comp_rate_governance: who signs off a comp / house-use plan
    comp_governance: { rate_category: category, needs_supervisor: comp,
      agent: false, supervisors: comp ? [GM] : [] },
  }
}

function serve(method: string, params: Record<string, unknown>) {
  switch (method) {
    case "kamra.api.booking_options": return Promise.resolve(OPTIONS)
    case "kamra.api.get_quote": return Promise.resolve(quoteFor(params))
    case "kamra.api.available_rooms": return Promise.resolve([{ name: "STD-101", room_number: "101", housekeeping_status: "Clean" }])
    case "kamra.api.property_locale": return Promise.resolve({ currency: "KES", currency_symbol: "KES ", locale: "en", tax_label: "VAT", tax_rates: [0], payment_modes: ["Cash"] })
    case "kamra.rates.rate_options": return Promise.resolve({ nights: [], plans: [], voucher: null,
      plain: { room_total: 2000, amount_after_tax: 2000, discount: 0 }, tax_inclusive: false })
    case "kamra.cashier.my_till": return Promise.resolve({ open: true })
    case "kamra.ledger.list_exchange_rates": return Promise.resolve({})
    case "kamra.api.create_booking": return Promise.resolve({ reservation: "RES-1", room: "STD-101", room_number: "101" })
    case "kamra.api.reservation_detail": return Promise.resolve({ deposit: null })
    default: return Promise.resolve(null)
  }
}

describe("Rate Plans: the plan's category", () => {
  it("offers the property's categories (kamra.settings.menu_masters), OPERA's until they load", () => {
    const field = ratePlansConfig.form.find((f) => f.field === "rate_category")
    expect(field).toBeDefined()
    expect(field).toMatchObject({ type: "select", optionsFrom: "rate_categories",
      options: ["Standard", "Complimentary", "House Use"], default: "Standard" })
    expect(ratePlansConfig.columns.some((c) => c.field === "rate_category")).toBe(true)
  })
})

describe("BookingDialog: a Complimentary or House Use plan is signed off", () => {
  beforeEach(() => { rpc.mockReset().mockImplementation(serve) })
  const quoteCalls = () => rpc.mock.calls.filter(([m]) => m === "kamra.api.get_quote").map(([, p]) => p)
  const booked = () => rpc.mock.calls.filter(([m]) => m === "kamra.api.create_booking").map(([, p]) => p)

  async function openOn(plan: string) {
    await act(async () => root.render(<MemoryRouter><BookingDialog initial={{ room_type: "STD" }}
      onClose={() => {}} onBooked={() => {}} /></MemoryRouter>))
    await until(() => expect(quoteCalls().length).toBeGreaterThan(0))
    await typeInto(host.querySelector<HTMLInputElement>("#booking-guest")!, "Ada Guest")
    await choose(host.querySelector<HTMLSelectElement>("#booking-rate-plan")!, plan)
    await until(() => expect(quoteCalls().at(-1)).toMatchObject({ rate_plan: plan }))
  }
  const openOnComp = () => openOn("COMP")

  it("asks for a manager's PIN, names why, and sends it with the booking", async () => {
    await openOnComp()
    await until(() => expect(byLabel("Manager PIN")).not.toBeNull())
    expect(text()).toContain("A Complimentary rate plan needs a manager's sign-off.")
    await choose(byLabel<HTMLSelectElement>("Manager")!, GM.user)
    await typeInto(byLabel<HTMLInputElement>("Manager PIN")!, "4321")
    await press("Confirm booking")
    await until(() => expect(booked().length).toBe(1))
    expect(booked()[0]).toMatchObject({ rate_plan: "COMP", supervisor_user: GM.user, supervisor_pin: "4321" })
  })

  it("does not send the booking until the manager has signed it off", async () => {
    await openOnComp()
    await until(() => expect(byLabel("Manager PIN")).not.toBeNull())
    await press("Confirm booking")
    await settle()
    expect(booked()).toEqual([])
    expect(text()).toContain("A manager signs this off with their PIN.")
  })

  it("names a category the property configured", async () => {
    await openOn("STAFF")
    await until(() => expect(byLabel("Manager PIN")).not.toBeNull())
    expect(text()).toContain("A Staff rate plan needs a manager's sign-off.")
  })

  it("asks nothing on a standard plan", async () => {
    await act(async () => root.render(<MemoryRouter><BookingDialog initial={{ room_type: "STD" }}
      onClose={() => {}} onBooked={() => {}} /></MemoryRouter>))
    await until(() => expect(quoteCalls().length).toBeGreaterThan(0))
    await settle()
    expect(byLabel("Manager PIN")).toBeNull()
    expect(text()).not.toContain("rate plan needs a manager's sign-off")
  })
})
