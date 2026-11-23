// @vitest-environment jsdom
/* Reservation and walk-in discounts (OPERA Cloud Look to Book / Walk-In /
 * Stay Details / Daily Details; SAP gross vs manual discount): a % OR a flat
 * amount per night with a mandatory reason, shown inline on the booking
 * dialog for any discountable rate plan (tariff plans and walk-ins included),
 * a supervisor PIN when the cut is over the desk's limit, and the stay's own
 * Stay Rate & Daily Details editor. */
import fs from "node:fs"
import path from "node:path"
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

import AgreedRatePanel from "../src/components/AgreedRatePanel"
import { BookingDialog } from "../src/components/BookingDialog"
import { ratePlansConfig } from "../src/screens/configs"

const NIGHTS = ["2031-03-17", "2031-03-18"]
const REASONS = [
  { name: "RATE-PROMO", code: "RATE-PROMO", description: "Promotional rate change", category: "Rate Change", requires_supervisor: 0 },
  { name: "RATE-MGR", code: "RATE-MGR", description: "Manager rate override", category: "Rate Change", requires_supervisor: 1 },
  { name: "ADJ-RATE", code: "ADJ-RATE", description: "Rate correction", category: "Adjust", requires_supervisor: 0 },
]
const governance = (over: Record<string, unknown> = {}) => ({
  base_total: 2000, discount_total: 200, net_total: 1800, effective_pct: 10, max_pct: 10,
  limit_pct: 10, needs_supervisor: false, can_set_rate: false, agent: false, eligible: true,
  comp: false, floor_breach: [], reason_requires_supervisor: false,
  supervisors: [{ user: "gm@test", full_name: "General Manager" }], ...over,
})

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

const OPTIONS = {
  room_types: [{ name: "STD", room_type_name: "Standard", base_price: 1000, adults_capacity: 2, children_capacity: 1 }],
  meal_plans: [], companies: [], travel_agents: [], experiences: [], ota_channels: [], ota_commission: {},
  rate_plans: [
    { name: "TARIFF", rate_plan_name: "Corporate tariff", code: "CORP", discount_eligible: 1 },
    { name: "NRF", rate_plan_name: "Non-refundable", code: "NRF", discount_eligible: 0 },
  ],
  default_plan: "TARIFF", rate_plan_required: true, can_set_rate: false,
  can_discount: true, rate_override_limit_pct: 10, discount_reasons: REASONS,
  property: { sell_message: "", free_cancel_days: 0, cancellation_fee: "None", no_show_charge: "None",
    deposit_pct: 0, country: "Uganda", pets_allowed: 0, pet_charge: 0, pet_charge_basis: "Per stay",
    checkin_time: "14:00", checkout_time: "11:00" },
}

function quoteFor(params: Record<string, unknown>) {
  const value = Number(params.discount_value ?? 0)
  const off = params.discount_type === "Percent" ? 1000 * value / 100 : params.discount_type === "Amount" ? value : 0
  const net = 1000 - off
  return {
    nights: 2, nightly: NIGHTS.map((date) => ({ date, rate: net, base_rate: 1000, discount_amount: off,
      discount_type: params.discount_type ?? null, discount_value: value, fixed_rate: 0 })),
    room_total: 2 * net, room_base_total: 2000, reservation_discount_total: 2 * off,
    meal_total: 0, discount: 2 * off, voucher_discount: 0, rate_plan: params.rate_plan,
    amount_before_tax: 2 * net, tax_percent: 0, tax_amount: 0, amount_after_tax: 2 * net,
    advance: { percent: 0, season: null, amount: 0 }, tax_basis: null,
    cancellation: { free_now: true, free_until: null, fee_basis: "None", fee_label: "", non_refundable_season: null },
    ...(params.discount_type ? { discount_governance: governance({
      effective_pct: value, max_pct: value, needs_supervisor: value > 10,
      discount_total: 2 * off, net_total: 2 * net,
    }) } : {}),
  }
}

// per-test changes to what the server answers (reset before each test)
let optionsOver: Record<string, unknown> = {}
let quoteOver: (params: Record<string, unknown>) => Record<string, unknown> = () => ({})

function serve(method: string, params: Record<string, unknown>) {
  switch (method) {
    case "kamra.api.booking_options": return Promise.resolve({ ...OPTIONS, ...optionsOver })
    case "kamra.api.get_quote": return Promise.resolve({ ...quoteFor(params), ...quoteOver(params) })
    case "kamra.api.available_rooms": return Promise.resolve([{ name: "STD-101", room_number: "101", housekeeping_status: "Clean" }])
    case "kamra.api.property_locale": return Promise.resolve({ currency: "UGX", currency_symbol: "UGX ", locale: "en", tax_label: "VAT", tax_rates: [0], payment_modes: ["Cash"] })
    case "kamra.rates.rate_options": return Promise.resolve({ nights: [], plans: [], voucher: null,
      plain: { room_total: 2000, amount_after_tax: 2000, discount: 0 }, tax_inclusive: false })
    case "kamra.cashier.my_till": return Promise.resolve({ open: true })
    case "kamra.ledger.list_exchange_rates": return Promise.resolve({})
    case "kamra.api.create_booking": return Promise.resolve({ reservation: "RES-1", room: "STD-101", room_number: "101" })
    case "kamra.api.walk_in": return Promise.resolve({ reservation: "RES-2", room: "STD-101", room_number: "101", folio: null, checked_in: 0 })
    case "kamra.api.reservation_detail": return Promise.resolve({ deposit: null })
    default: return Promise.resolve(null)
  }
}

describe("BookingDialog: inline discount for new bookings and walk-ins", () => {
  beforeEach(() => {
    optionsOver = {}
    quoteOver = () => ({})
    rpc.mockReset().mockImplementation(serve)
  })
  const quoteCalls = () => rpc.mock.calls.filter(([m]) => m === "kamra.api.get_quote").map(([, p]) => p)

  async function open(walkIn: boolean) {
    await act(async () => root.render(<MemoryRouter><BookingDialog initial={{ walkIn, room_type: "STD" }}
      onClose={() => {}} onBooked={() => {}} /></MemoryRouter>))
    await vi.waitFor(async () => {
      await settle()
      expect(quoteCalls().length).toBeGreaterThan(0)
      expect(byLabel("Discount value")).not.toBeNull()
    }, { timeout: 3000, interval: 20 })
  }

  it("works on a tariff rate plan at the walk-in counter and re-prices the amount collected", async () => {
    await open(true)
    // a rate plan is selected and required: the discount is still offered
    expect(byLabel<HTMLInputElement>("Discount value")!.disabled).toBe(false)
    await typeInto(byLabel<HTMLInputElement>("Discount value")!, "10")
    await choose(byLabel<HTMLSelectElement>("Discount reason")!, "RATE-PROMO")
    await vi.waitFor(async () => {
      await settle()
      expect(quoteCalls().at(-1)).toMatchObject({ rate_plan: "TARIFF", discount_type: "Percent",
        discount_value: 10, discount_reason: "RATE-PROMO" })
      expect(text()).toContain("Net room rate")
    }, { timeout: 3000, interval: 20 })
    expect(text()).toContain("Base room rate")
    expect(text()).toContain("Discount (Promotional rate change)")
    expect(byLabel<HTMLInputElement>("Amount collected")!.value).toBe("1800")
  })

  it("clears and disables the discount on a non-discountable rate plan", async () => {
    await open(false)
    await typeInto(byLabel<HTMLInputElement>("Discount value")!, "10")
    const plan = host.querySelector<HTMLSelectElement>("#booking-rate-plan")!
    await choose(plan, "NRF")
    await vi.waitFor(async () => {
      await settle()
      expect(byLabel<HTMLInputElement>("Discount value")!.disabled).toBe(true)
      expect(byLabel<HTMLInputElement>("Discount value")!.value).toBe("")
      expect(quoteCalls().at(-1)).toMatchObject({ rate_plan: "NRF" })
      expect(quoteCalls().at(-1)).not.toHaveProperty("discount_type")
    }, { timeout: 3000, interval: 20 })
    expect(text()).toContain("This rate plan does not allow discounts.")
  })

  it("asks for the supervisor PIN in the dialog and sends it with the booking", async () => {
    await open(false)
    await typeInto(host.querySelector<HTMLInputElement>("#booking-guest")!, "Ada Guest")
    await typeInto(byLabel<HTMLInputElement>("Discount value")!, "25")
    await choose(byLabel<HTMLSelectElement>("Discount reason")!, "RATE-PROMO")
    await vi.waitFor(async () => {
      await settle()
      expect(byLabel("Manager PIN")).not.toBeNull()
    }, { timeout: 3000, interval: 20 })
    await choose(byLabel<HTMLSelectElement>("Manager")!, "gm@test")
    await typeInto(byLabel<HTMLInputElement>("Manager PIN")!, "4321")
    await press("Confirm booking")
    await vi.waitFor(() => expect(rpc.mock.calls.some(([m]) => m === "kamra.api.create_booking")).toBe(true),
      { timeout: 3000, interval: 20 })
    const [, booked] = rpc.mock.calls.find(([m]) => m === "kamra.api.create_booking")!
    expect(booked).toMatchObject({ rate_plan: "TARIFF", discount_type: "Percent", discount_value: 25,
      discount_reason: "RATE-PROMO", supervisor_user: "gm@test", supervisor_pin: "4321" })
  })

  // --- round 2 (Gap Record #8555 review) ---------------------------------
  const walkInButton = () => Array.from(host.querySelectorAll("button")).find((b) =>
    /^(Collect .* and continue to check-in|Continue to check-in)$/.test(b.textContent?.trim() ?? ""))!
  const lastQuote = () => quoteCalls().at(-1) as Record<string, unknown>
  async function discountOf(value: string, reason = "RATE-PROMO") {
    await typeInto(byLabel<HTMLInputElement>("Discount value")!, value)
    if (reason) await choose(byLabel<HTMLSelectElement>("Discount reason")!, reason)
    await vi.waitFor(async () => {
      await settle()
      expect(lastQuote()).toMatchObject({ discount_value: Number(value) })
    }, { timeout: 3000, interval: 20 })
  }

  it("blocks the walk-in until the discount has its reason and the manager's PIN (P1-8)", async () => {
    await open(true)
    await typeInto(host.querySelector<HTMLInputElement>("#booking-guest")!, "Walk Guest")
    await discountOf("5")
    await vi.waitFor(() => expect(walkInButton().disabled).toBe(false), { timeout: 3000, interval: 20 })
    // over the desk's limit: a manager signs it off before the money is taken
    await discountOf("25")
    await vi.waitFor(() => expect(byLabel("Manager PIN")).not.toBeNull(), { timeout: 3000, interval: 20 })
    expect(walkInButton().disabled).toBe(true)
    await choose(byLabel<HTMLSelectElement>("Manager")!, "gm@test")
    await typeInto(byLabel<HTMLInputElement>("Manager PIN")!, "4321")
    await vi.waitFor(() => expect(walkInButton().disabled).toBe(false), { timeout: 3000, interval: 20 })
    // a discount with no reason is never sent
    await choose(byLabel<HTMLSelectElement>("Discount reason")!, "")
    await vi.waitFor(() => expect(walkInButton().disabled).toBe(true), { timeout: 3000, interval: 20 })
    await act(async () => walkInButton().click())
    expect(rpc.mock.calls.some(([m]) => m === "kamra.api.walk_in")).toBe(false)
  })

  it("holds an amount or a Daily Details night to the night's own rate (P1-8)", async () => {
    await open(true)
    await typeInto(host.querySelector<HTMLInputElement>("#booking-guest")!, "Walk Guest")
    await press("Amount / night")
    await discountOf("1500")
    await choose(byLabel<HTMLSelectElement>("Manager")!, "gm@test")
    await typeInto(byLabel<HTMLInputElement>("Manager PIN")!, "4321")
    await settle()
    // 1,500 off a 1,000 night: refused here, not left to the server
    expect(walkInButton().disabled).toBe(true)
    await typeInto(byLabel<HTMLInputElement>("Discount value")!, "")
    // Daily Details list the quote's nights: wait for the re-quote
    await vi.waitFor(async () => {
      await settle()
      expect(Array.from(host.querySelectorAll("button")).some((b) =>
        b.textContent?.trim() === "Per-night daily details")).toBe(true)
    }, { timeout: 3000, interval: 20 })
    await press("Per-night daily details")
    const night = host.querySelector<HTMLInputElement>(`tr[data-night="${NIGHTS[0]}"] input[type=number]`)!
    await press("% off")
    await typeInto(night, "150")
    await settle()
    expect(text()).toContain("A percentage is between 0 and 100.")
    expect(walkInButton().disabled).toBe(true)
  })

  it("stops a new booking whose Daily Details night is out of bounds (P1-8)", async () => {
    await open(false)
    await typeInto(host.querySelector<HTMLInputElement>("#booking-guest")!, "Ada Guest")
    await choose(byLabel<HTMLSelectElement>("Discount reason")!, "RATE-PROMO")
    await press("Per-night daily details")
    await typeInto(host.querySelector<HTMLInputElement>(`tr[data-night="${NIGHTS[1]}"] input[type=number]`)!, "150")
    await settle()
    await press("Confirm booking")
    // let a booking the dialog wrongly sent finish before this test ends
    for (let i = 0; i < 20; i++) await settle()
    expect(rpc.mock.calls.some(([m]) => m === "kamra.api.create_booking")).toBe(false)
  })

  it("lets a revenue manager fix the rate on a plan that takes no discount (P1-9)", async () => {
    optionsOver = { can_set_rate: true }
    await open(false)
    await typeInto(host.querySelector<HTMLInputElement>("#booking-guest")!, "Ada Guest")
    await choose(host.querySelector<HTMLSelectElement>("#booking-rate-plan")!, "NRF")
    await vi.waitFor(async () => {
      await settle()
      expect(byLabel("Fixed rate override")).not.toBeNull()
    }, { timeout: 3000, interval: 20 })
    expect(byLabel<HTMLInputElement>("Discount value")!.disabled).toBe(true)
    await act(async () => byLabel<HTMLInputElement>("Fixed rate override")!.click())
    expect(byLabel<HTMLInputElement>("Discount value")!.disabled).toBe(false)
    await discountOf("10")
    expect(lastQuote()).toMatchObject({ rate_plan: "NRF", discount_type: "Percent", fixed_rate: 1 })
    await press("Confirm booking")
    await vi.waitFor(() => expect(rpc.mock.calls.some(([m]) => m === "kamra.api.create_booking")).toBe(true),
      { timeout: 3000, interval: 20 })
    const [, booked] = rpc.mock.calls.find(([m]) => m === "kamra.api.create_booking")!
    expect(booked).toMatchObject({ rate_plan: "NRF", discount_type: "Percent", discount_value: 10, fixed_rate: 1 })
  })

  it("offers no fixed-rate override to the front desk (P1-9)", async () => {
    await open(false)
    await choose(host.querySelector<HTMLSelectElement>("#booking-rate-plan")!, "NRF")
    await settle()
    expect(byLabel("Fixed rate override")).toBeNull()
  })

  it("shows a discounted contract tariff as base, discount and net only (P1-10)", async () => {
    quoteOver = () => ({ agreed_rate: 1000, tariff: { currency: "USD", nightly: 100, property_currency: "UGX",
      fx_rate: 10, fx_date: "2031-03-01" } })
    await open(false)
    await vi.waitFor(() => expect(text()).toContain("Room tariff subtotal"), { timeout: 3000, interval: 20 })
    await discountOf("10")
    await vi.waitFor(() => expect(text()).toContain("Net room rate"), { timeout: 3000, interval: 20 })
    expect(text()).toContain("Base room rate")
    expect(text()).not.toContain("Room tariff subtotal")
  })
})

describe("Stay Rate & Daily Details editor (reservation screen)", () => {
  const preview = {
    base_total: 2000, discount_total: 200, net_total: 1800, effective_pct: 10, max_pct: 10, limit_pct: 10,
    needs_supervisor: false, can_set_rate: false, agent: false, eligible: true, comp: false, floor_breach: [],
    reason_requires_supervisor: false, supervisors: [], current_amount: 2000, new_amount: 1800,
    difference: -200, needs_confirmation: true,
    nights: NIGHTS.map((date) => ({ date, base_rate: 1000, discount_amount: 100, rate: 900, charged: false })),
  }
  let planOver: Record<string, unknown> = {}
  beforeEach(() => rpc.mockReset().mockImplementation((method: string, params: Record<string, unknown>) => {
    if (method === "kamra.ledger.list_reason_codes")
      return Promise.resolve(REASONS.filter((r) => !params.category || r.category === params.category))
    if (method === "kamra.api.discount_preview") return Promise.resolve({ ...preview, ...planOver })
    if (method === "kamra.api.set_reservation_discount") return Promise.resolve({ ok: true })
    return Promise.resolve(null)
  }))

  it("previews base, discount and net per night and saves through set_reservation_discount", async () => {
    const saved = vi.fn()
    await act(async () => root.render(<AgreedRatePanel reservation="RES-9" onSaved={saved}
      nightRates={NIGHTS.map((date) => ({ date, rate: 1000 }))} />))
    await press("Stay rate & daily details")
    expect(Array.from(host.querySelectorAll("[role=tab]")).map((n) => n.textContent?.trim()))
      .toEqual(["Discount", "Fixed rate"])
    await typeInto(byLabel<HTMLInputElement>("Discount value")!, "10")
    await choose(byLabel<HTMLSelectElement>("Discount reason")!, "ADJ-RATE")
    await vi.waitFor(async () => {
      await settle()
      expect(rpc.mock.calls.some(([m]) => m === "kamra.api.discount_preview")).toBe(true)
      expect(text()).toContain("Net")
    }, { timeout: 3000, interval: 20 })
    const [, sent] = rpc.mock.calls.filter(([m]) => m === "kamra.api.discount_preview").at(-1)!
    expect(sent).toMatchObject({ reservation: "RES-9", discount_type: "Percent", discount_value: 10,
      reason_code: "ADJ-RATE" })
    await press("Save discount")
    await vi.waitFor(() => expect(saved).toHaveBeenCalled(), { timeout: 3000, interval: 20 })
    const [, applied] = rpc.mock.calls.find(([m]) => m === "kamra.api.set_reservation_discount")!
    expect(applied).toMatchObject({ reservation: "RES-9", discount_type: "Percent", discount_value: 10,
      reason_code: "ADJ-RATE", confirm_price: 1 })
  })

  // --- round 2 (Gap Record #8555 review) ---------------------------------
  beforeEach(() => { planOver = {} })
  const previews = () => rpc.mock.calls.filter(([m]) => m === "kamra.api.discount_preview").map(([, p]) => p)
  const saveButton = () => Array.from(host.querySelectorAll("button"))
    .find((b) => b.textContent?.trim() === "Save discount")!

  it("leaves a night already on the folio out of what it sends (P1-7)", async () => {
    const saved = vi.fn()
    await act(async () => root.render(<AgreedRatePanel reservation="RES-9" onSaved={saved}
      current={{ discount_type: null, discount_value: 0, discount_reason: null }}
      nightRates={[
        { date: NIGHTS[0], rate: 900, base_rate: 1000, discount_type: "Percent", discount_value: 10, charged: true },
        { date: NIGHTS[1], rate: 1000 },
      ]} />))
    await press("Stay rate & daily details")
    await typeInto(byLabel<HTMLInputElement>("Discount value")!, "5")
    await choose(byLabel<HTMLSelectElement>("Discount reason")!, "RATE-PROMO")
    await vi.waitFor(async () => {
      await settle()
      expect(previews().at(-1)).toMatchObject({ discount_type: "Percent", discount_value: 5 })
    }, { timeout: 3000, interval: 20 })
    expect(previews().at(-1)!.night_discounts).toBeUndefined()
    await press("Save discount")
    await vi.waitFor(() => expect(saved).toHaveBeenCalled(), { timeout: 3000, interval: 20 })
    const [, applied] = rpc.mock.calls.find(([m]) => m === "kamra.api.set_reservation_discount")!
    expect(applied.night_discounts).toBeUndefined()
  })

  it("starts from the stay's discount every time it opens (P1-7)", async () => {
    const nightRates = NIGHTS.map((date) => ({ date, rate: 1000 }))
    const panel = (pct: number) => <AgreedRatePanel reservation="RES-9" onSaved={() => {}} nightRates={nightRates}
      current={{ discount_type: "Percent", discount_value: pct, discount_reason: "RATE-PROMO" }} />
    await act(async () => root.render(panel(10)))
    await press("Stay rate & daily details")
    expect(byLabel<HTMLInputElement>("Discount value")!.value).toBe("10")
    await press("Cancel")
    // the stay was saved elsewhere at 20%: the drawer re-renders with it
    await act(async () => root.render(panel(20)))
    await press("Stay rate & daily details")
    expect(byLabel<HTMLInputElement>("Discount value")!.value).toBe("20")
  })

  it("keeps Save discount off on a plan that takes no discount (P1-11)", async () => {
    planOver = { eligible: false, can_set_rate: false }
    await act(async () => root.render(<AgreedRatePanel reservation="RES-9" onSaved={() => {}}
      nightRates={NIGHTS.map((date) => ({ date, rate: 1000 }))} />))
    await press("Stay rate & daily details")
    await typeInto(byLabel<HTMLInputElement>("Discount value")!, "10")
    await choose(byLabel<HTMLSelectElement>("Discount reason")!, "RATE-PROMO")
    await vi.waitFor(async () => {
      await settle()
      expect(previews().length).toBeGreaterThan(0)
      expect(text()).toContain("This rate plan does not allow discounts.")
    }, { timeout: 3000, interval: 20 })
    expect(saveButton().disabled).toBe(true)
  })

  it("keeps the fixed-rate override on the existing agreed-rate flow", async () => {
    await act(async () => root.render(<AgreedRatePanel reservation="RES-9" onSaved={() => {}}
      nightRates={NIGHTS.map((date) => ({ date, rate: 1000 }))} />))
    await press("Stay rate & daily details")
    await press("Fixed rate")
    expect(text()).toContain("New rate / night")
    expect(text()).toContain("Save agreed rate")
  })
})

describe("Rate Plans and translations", () => {
  it("exposes the Discount eligible flag on the rate plan form", () => {
    const field = ratePlansConfig.form.find((f) => f.field === "discount_eligible")
    expect(field).toMatchObject({ label: "Discount eligible", type: "check" })
  })

  const KEYS = [
    "Discount", "% off", "Amount / night", "Discount value", "Discount reason",
    "This rate plan does not allow discounts.",
    "Comes off the room rate only - meals, extras and tax keep their own price.",
    "Per-night daily details", "Apply to future nights", "Base", "Net",
    "A 100% discount (comp) needs a manager's sign-off.", "Below the rate floor on {dates}.",
    "This reason needs a manager's sign-off.", "A percentage is between 0 and 100.",
    "The discount is more than the night's rate.", "Enter a discount of zero or more.",
    "Base room rate", "Discount ({reason})", "Net room rate", "Stay rate & daily details",
    "Fixed rate", "Save discount", "Fixed rate override", "Discount eligible",
    "Untick for rates that never take a desk discount (non-refundable, packages).",
  ]
  it.each(["ar", "de", "es", "fil", "id", "pt", "zh-CN"])("translates the discount controls in %s", (language) => {
    const dictionary = JSON.parse(fs.readFileSync(path.resolve(process.cwd(), `src/i18n/locales/${language}.json`), "utf8"))
    for (const key of KEYS) expect(dictionary[key]?.trim(), `${language}: ${key}`).toBeTruthy()
  })
})
