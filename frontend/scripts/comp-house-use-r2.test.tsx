// @vitest-environment jsdom
/* Complimentary and House Use rate plans, round 2 (Neon's R1 review of
 * #38). A comp plan that also takes a discount over the desk's limit needs
 * both sign-offs: OPERA's one authorizer whose privileges cover both
 * prompts signs both, otherwise each is signed off by its own approver, and
 * the dialog shows every sign-off still needed. Several rooms on a comp plan
 * carry the sign-off to the group booking. A category setting the user may
 * not change is shown read-only. */
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
import { ModulePage } from "../src/screens/settings/ModulePage"
import type { SettingsModule } from "../src/screens/settings/api"

const NIGHTS = ["2031-03-17", "2031-03-18"]
// a discount approver (Hotel Admin), the comp approver this property
// configured (Revenue Manager) and a manager who is both
const HA = { user: "gm@test", full_name: "General Manager" }
const RM = { user: "rm@test", full_name: "Revenue Manager" }
const BOTH = { user: "both@test", full_name: "Duty Manager" }
const REASONS = [{ name: "RATE-PROMO", code: "RATE-PROMO", description: "Promotional rate change",
  category: "Rate Change", requires_supervisor: 0 }]

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
const byLabel = <T extends Element>(label: string, scope: ParentNode | null = host) =>
  scope?.querySelector<T>(`[aria-label="${label}"]`) ?? null
const discountBox = () => host.querySelector("#booking-discount")
const compBox = () => host.querySelector("#booking-comp")
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
    // a Complimentary plan at half the rate that still takes a desk discount
    { name: "HALF", rate_plan_name: "Half comp", code: "HALF", discount_eligible: 1 },
  ],
  default_plan: "BAR", rate_plan_required: true, can_set_rate: false,
  can_discount: true, rate_override_limit_pct: 10, discount_reasons: REASONS,
  property: { sell_message: "", free_cancel_days: 0, cancellation_fee: "None", no_show_charge: "None",
    deposit_pct: 0, country: "Kenya", pets_allowed: 0, pet_charge: 0, pet_charge_basis: "Per stay",
    checkin_time: "14:00", checkout_time: "11:00" },
}

function quoteFor(params: Record<string, unknown>) {
  const comp = params.rate_plan === "HALF"
  const card = comp ? 500 : 1000
  const value = Number(params.discount_value ?? 0)
  const off = params.discount_type === "Percent" ? card * value / 100 : 0
  const net = card - off
  return {
    nights: 2, nightly: NIGHTS.map((date) => ({ date, rate: net, base_rate: card, discount_amount: off,
      discount_type: params.discount_type ?? null, discount_value: value, fixed_rate: 0 })),
    room_total: 2 * net, room_base_total: 2 * card, reservation_discount_total: 2 * off,
    meal_total: 0, discount: 2 * off, voucher_discount: 0, rate_plan: params.rate_plan,
    amount_before_tax: 2 * net, tax_percent: 0, tax_amount: 0, amount_after_tax: 2 * net,
    advance: { percent: 0, season: null, amount: 0 }, tax_basis: null,
    cancellation: { free_now: true, free_until: null, fee_basis: "None", fee_label: "", non_refundable_season: null },
    // kamra.api.discount_governance: Hotel Admin / Finance sign a cut over 10%
    ...(params.discount_type ? { discount_governance: {
      base_total: 2 * card, discount_total: 2 * off, net_total: 2 * net, effective_pct: value,
      max_pct: value, limit_pct: 10, needs_supervisor: value > 10, can_set_rate: false, agent: false,
      eligible: true, comp: false, floor_breach: [], reason_requires_supervisor: false,
      supervisors: [HA, BOTH] } } : {}),
    // kamra.api.comp_rate_governance: this property's comp approvers
    comp_governance: { rate_category: comp ? "Complimentary" : "Standard", needs_supervisor: comp,
      agent: false, supervisors: comp ? [RM, BOTH] : [] },
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
    case "kamra.api.create_group_booking": return Promise.resolve({ group_booking: "GRP-1", created: ["RES-1", "RES-2"], skipped: [] })
    case "kamra.api.reservation_detail": return Promise.resolve({ deposit: null })
    default: return Promise.resolve(null)
  }
}

describe("BookingDialog: a discounted comp takes every sign-off it needs", () => {
  beforeEach(() => { rpc.mockReset().mockImplementation(serve) })
  const quoteCalls = () => rpc.mock.calls.filter(([m]) => m === "kamra.api.get_quote").map(([, p]) => p)
  const booked = () => rpc.mock.calls.filter(([m]) => m === "kamra.api.create_booking").map(([, p]) => p)

  async function openDiscountedComp() {
    await act(async () => root.render(<MemoryRouter><BookingDialog initial={{ room_type: "STD" }}
      onClose={() => {}} onBooked={() => {}} /></MemoryRouter>))
    await until(() => expect(byLabel("Discount value")).not.toBeNull())
    await typeInto(host.querySelector<HTMLInputElement>("#booking-guest")!, "Ada Guest")
    await choose(host.querySelector<HTMLSelectElement>("#booking-rate-plan")!, "HALF")
    await until(() => expect(quoteCalls().at(-1)).toMatchObject({ rate_plan: "HALF" }))
    await typeInto(byLabel<HTMLInputElement>("Discount value")!, "25")
    await choose(byLabel<HTMLSelectElement>("Discount reason")!, "RATE-PROMO")
    await until(() => expect(byLabel("Manager PIN", discountBox())).not.toBeNull())
  }

  it("shows the discount's and the comp's sign-off and sends each", async () => {
    await openDiscountedComp()
    // the comp's sign-off is not hidden behind the discount's
    await until(() => expect(byLabel("Manager PIN", compBox())).not.toBeNull())
    expect(text()).toContain("A Complimentary rate plan needs a manager's sign-off.")
    await choose(byLabel<HTMLSelectElement>("Manager", discountBox())!, HA.user)
    await typeInto(byLabel<HTMLInputElement>("Manager PIN", discountBox())!, "4321")
    await choose(byLabel<HTMLSelectElement>("Manager", compBox())!, RM.user)
    await typeInto(byLabel<HTMLInputElement>("Manager PIN", compBox())!, "2468")
    await press("Confirm booking")
    await until(() => expect(booked().length).toBe(1))
    expect(booked()[0]).toMatchObject({ rate_plan: "HALF", discount_type: "Percent", discount_value: 25,
      supervisor_user: HA.user, supervisor_pin: "4321",
      comp_supervisor_user: RM.user, comp_supervisor_pin: "2468" })
  })

  it("lets one manager who can sign both off do it once", async () => {
    await openDiscountedComp()
    await choose(byLabel<HTMLSelectElement>("Manager", discountBox())!, BOTH.user)
    await typeInto(byLabel<HTMLInputElement>("Manager PIN", discountBox())!, "1357")
    // their sign-off covers the comp too: no second one is asked for
    await until(() => expect(compBox()).toBeNull())
    await press("Confirm booking")
    await until(() => expect(booked().length).toBe(1))
    expect(booked()[0]).toMatchObject({ supervisor_user: BOTH.user, supervisor_pin: "1357" })
    expect(booked()[0]).not.toHaveProperty("comp_supervisor_user")
  })

  it("does not send the booking until the comp is signed off too", async () => {
    await openDiscountedComp()
    await choose(byLabel<HTMLSelectElement>("Manager", discountBox())!, HA.user)
    await typeInto(byLabel<HTMLInputElement>("Manager PIN", discountBox())!, "4321")
    await until(() => expect(byLabel("Manager PIN", compBox())).not.toBeNull())
    await press("Confirm booking")
    await settle()
    expect(booked()).toEqual([])
  })
})

describe("BookingDialog: several rooms on a comp plan", () => {
  beforeEach(() => { rpc.mockReset().mockImplementation(serve) })
  const quoteCalls = () => rpc.mock.calls.filter(([m]) => m === "kamra.api.get_quote").map(([, p]) => p)
  const grouped = () => rpc.mock.calls.filter(([m]) => m === "kamra.api.create_group_booking").map(([, p]) => p)

  it("sends the comp's sign-off with the group booking", async () => {
    await act(async () => root.render(<MemoryRouter><BookingDialog initial={{ room_type: "STD" }}
      onClose={() => {}} onBooked={() => {}} /></MemoryRouter>))
    await until(() => expect(quoteCalls().length).toBeGreaterThan(0))
    await typeInto(host.querySelector<HTMLInputElement>("#booking-guest")!, "Ada Guest")
    await choose(host.querySelector<HTMLSelectElement>("#booking-rate-plan")!, "HALF")
    await until(() => expect(byLabel("Manager PIN", compBox())).not.toBeNull())
    await press("Add another room")
    await choose(byLabel<HTMLSelectElement>("Manager", compBox())!, RM.user)
    await typeInto(byLabel<HTMLInputElement>("Manager PIN", compBox())!, "2468")
    await press("Confirm booking")
    await until(() => expect(grouped().length).toBe(1))
    expect(grouped()[0]).toMatchObject({ rate_plan: "HALF", comp_supervisor_user: RM.user,
      comp_supervisor_pin: "2468" })
  })
})

describe("Settings: a setting the user's role can't change", () => {
  it("is shown read-only on a page they can edit", async () => {
    const page: SettingsModule = {
      id: "revenue", title: "Revenue", description: "", group: "modules", toggle: true, enabled: true,
      editors: ["Hotel Admin", "Revenue Manager"], can_edit: true, related: [],
      sections: [{ id: "s", title: "Complimentary and house use", description: "", settings: [
        { key: "overbooking_pct", label: "Overbooking allowance", type: "number", default: 0, hint: "",
          store: "property" },
        // kamra.settings.settings_schema: the configuration privilege's only
        { key: "rate_categories", label: "Rate categories", type: "textarea", default: "", hint: "",
          store: "kv", can_edit: false },
      ] }],
      values: { overbooking_pct: 0, rate_categories: "Standard" },
    }
    await act(async () => root.render(<MemoryRouter><ModulePage module={page}
      sources={{ countries: [], timezones: [] }} onSaved={() => {}} onDirtyChange={() => {}} />
    </MemoryRouter>))
    expect(host.querySelector<HTMLTextAreaElement>("#setting-rate_categories")!.disabled).toBe(true)
    expect(host.querySelector<HTMLInputElement>("#setting-overbooking_pct")!.disabled).toBe(false)
  })
})
