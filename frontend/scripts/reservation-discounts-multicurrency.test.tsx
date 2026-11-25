// @vitest-environment jsdom
/* PR 1 (KAMRA-MC-DISCOUNT-CORE) Frontend RED -> GREEN tests:
 * SAP S/4HANA (WAERK -> HWAER) + Oracle OPERA Cloud multi-currency discounts:
 * - DiscountPanel currency badge/toggle, dynamic 0/2/3dp moneyStep, FX equiv & Daily Details
 * - BookingDialog Tankhill Walk-In reproduction: USD 150 - USD 50 = USD 100 & Collect USD 100.00 CTA
 * - AgreedRatePanel currency-aware discount + Fixed rate USD override
 * - ReservationDetail dual-currency discount ("USD 50.00 off a night", never "UGX 50") */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const { rpc, auth } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return { rpc: vi.fn(), auth: { roles: ["Front Desk", "Revenue Manager"] as string[] } }
})

vi.mock("../src/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api")>()
  const call = (method: string, params?: Record<string, unknown>) => rpc(method, params ?? {})
  return {
    ...actual,
    call,
    getCurrentProperty: () => "Tankhill",
    getBookingOptions: () => call("kamra.api.booking_options", { property: "Tankhill" }),
    getQuote: (params: Record<string, unknown>) => call("kamra.api.get_quote", { property: "Tankhill", ...params }),
    createBooking: (params: Record<string, unknown>) =>
      call("kamra.api.create_booking", { property: "Tankhill", ...params }),
    guestAlert: async () => ({ guest: null, blacklisted: 0, blacklist_reason: null }),
    guestSearch: async () => [],
    reservationDetail: (reservation: string) => call("kamra.api.reservation_detail", { reservation }),
  }
})

vi.mock("../src/lib/auth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/lib/auth")>()),
  useAuth: () => ({
    status: "authed", user: "desk@test", roles: auth.roles,
    refresh: async () => {}, signOut: async () => {},
  }),
}))

vi.mock("../src/lib/realtime", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/lib/realtime")>()),
  useRealtime: () => {},
}))

import AgreedRatePanel from "../src/components/AgreedRatePanel"
import { BookingDialog } from "../src/components/BookingDialog"
import DiscountPanel from "../src/components/DiscountPanel"
import { adoptUiLocale, fmtMoney } from "../src/lib/money"
import { emptyDiscount, type DiscountState } from "../src/lib/reservationDiscount"
import ReservationDetail from "../src/screens/ReservationDetail"

const FX_RATE = 4085.833333
const GROSS_UGX = 612875
const DISC_UGX = 204292
const NET_UGX = 408583

const REASONS = [
  { name: "RATE-PROMO", code: "RATE-PROMO", description: "Promotional rate change", category: "Rate Change", requires_supervisor: 0 },
  { name: "RATE-MGR", code: "RATE-MGR", description: "Manager rate override", category: "Rate Change", requires_supervisor: 1 },
  { name: "ADJ-RATE", code: "ADJ-RATE", description: "Rate correction", category: "Adjust", requires_supervisor: 0 },
]

const TANKHILL_OPTIONS = {
  room_types: [{ name: "STD", room_type_name: "Standard Double", base_price: 300000, adults_capacity: 2, children_capacity: 1 }],
  meal_plans: [], companies: [], travel_agents: [], experiences: [], ota_channels: [], ota_commission: {},
  rate_plans: [
    { name: "BB-USD", rate_plan_name: "Bed & Breakfast (CP)", code: "CP", discount_eligible: 1 },
  ],
  default_plan: "BB-USD", rate_plan_required: true, can_set_rate: true,
  can_discount: true, rate_override_limit_pct: 40, discount_reasons: REASONS,
  property: {
    sell_message: "", free_cancel_days: 0, cancellation_fee: "None", no_show_charge: "None",
    deposit_pct: 0, country: "Uganda", pets_allowed: 0, pet_charge: 0, pet_charge_basis: "Per stay",
    checkin_time: "14:00", checkout_time: "11:00",
  },
}

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  host = document.createElement("div")
  document.body.append(host)
  root = createRoot(host)
  auth.roles = ["Front Desk", "Revenue Manager"]
  adoptUiLocale({
    currency: "UGX",
    currency_symbol: "UGX ",
    locale: "en-UG",
    decimals: 0,
    currency_decimals: { UGX: 0, USD: 2, KWD: 3, EUR: 2 },
  })
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
const button = (label: string, scope: ParentNode = host) =>
  Array.from(scope.querySelectorAll("button")).find((b) => b.textContent?.trim() === label)

async function press(label: string, scope: ParentNode = host) {
  const b = button(label, scope)
  expect(b, `Missing button: ${label}`).toBeDefined()
  await act(async () => b!.click())
}

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

const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 0)))
const until = (check: () => void | Promise<void>) =>
  vi.waitFor(async () => { await settle(); await check() }, { timeout: 4000, interval: 20 })
const calls = (method: string) =>
  rpc.mock.calls.filter(([m]) => m === method).map(([, p]) => p as Record<string, unknown>)

describe("DiscountPanel: multi-currency badge, toggle, precision step & Daily Details", () => {
  it("defaults Amount / night to USD (step 0.01) on a USD rate plan at a UGX 0dp property, switches cleanly to UGX (step 1), and enforces USD ceiling", async () => {
    let state: DiscountState = { ...emptyDiscount(), type: "Amount", value: "50", reason: "RATE-PROMO" }
    const render = async () => {
      await act(async () => {
        root.render(
          <DiscountPanel
            value={state}
            onChange={(next) => { state = next; void render() }}
            reasons={REASONS}
            eligible={true}
            governance={null}
            rateCurrency="USD"
            homeCurrency="UGX"
            fxRate={FX_RATE}
            nights={[{ date: "2026-12-01", base_rate: GROSS_UGX, contract_base_rate: 150, contract_currency: "USD" }]}
            supervisor=""
            pin=""
            onSupervisor={() => {}}
            onPin={() => {}}
            money={(n, c) => fmtMoney(n, c)}
          />,
        )
      })
    }
    await render()

    // Active currency defaults to USD with step="0.01" (not UGX step="1")
    const valInput = byLabel<HTMLInputElement>("Discount value")!
    expect(valInput.step).toBe("0.01")
    expect(valInput.max).toBe("150")
    // Currency toggle group present with USD active and UGX switchable
    const curGroup = byLabel("Discount currency")
    expect(curGroup).not.toBeNull()
    expect(button("USD", curGroup!)?.getAttribute("aria-pressed")).toBe("true")
    expect(button("UGX", curGroup!)?.getAttribute("aria-pressed")).toBe("false")
    // Shows live UGX equivalent preview for USD 50
    expect(text()).toContain("204,292")

    // Entering 151 USD exceeds the USD 150 ceiling
    await typeInto(valInput, "151")
    expect(host.querySelector('[role="alert"]')?.textContent ?? "").toContain("more than the night's rate")

    // Switch currency pill to UGX -> step becomes "1", ceiling becomes 612875, 50000 is valid
    await press("UGX", curGroup!)
    expect(state.currency).toBe("UGX")
    expect(byLabel<HTMLInputElement>("Discount value")!.step).toBe("1")
    await typeInto(byLabel<HTMLInputElement>("Discount value")!, "50000")
    expect(host.querySelector('[role="alert"]')).toBeNull()

    // Daily Details shows USD 150.00 primary + UGX 612,875 secondary + per-night currency control
    await press("Per-night daily details")
    expect(text()).toContain("USD 150.00")
    expect(text()).toContain("UGX 612,875")
  })

  it("uses step 0.001 for KWD (3dp) rate plans and accepts 12.125 KWD", async () => {
    let state: DiscountState = { ...emptyDiscount(), type: "Amount", value: "12.125", currency: "KWD", reason: "RATE-PROMO" }
    const render = async () => {
      await act(async () => {
        root.render(
          <DiscountPanel
            value={state}
            onChange={(next) => { state = next; void render() }}
            reasons={REASONS}
            eligible={true}
            governance={null}
            rateCurrency="KWD"
            homeCurrency="UGX"
            fxRate={12500}
            nights={[{ date: "2026-12-01", base_rate: 1256250, contract_base_rate: 100.5, contract_currency: "KWD" }]}
            supervisor=""
            pin=""
            onSupervisor={() => {}}
            onPin={() => {}}
            money={(n, c) => fmtMoney(n, c)}
          />,
        )
      })
    }
    await render()
    const valInput = byLabel<HTMLInputElement>("Discount value")!
    expect(valInput.step).toBe("0.001")
    expect(host.querySelector('[role="alert"]')).toBeNull()
  })
})

describe("BookingDialog: Tankhill Walk-In USD 150 - USD 50 = USD 100 & Collect USD 100.00 CTA", () => {
  beforeEach(() => {
    rpc.mockReset().mockImplementation((method: string, params: Record<string, unknown>) => {
      if (method === "kamra.api.booking_options") return Promise.resolve(TANKHILL_OPTIONS)
      if (method === "kamra.api.property_locale") {
        return Promise.resolve({
          currency: "UGX", currency_symbol: "UGX ", locale: "en-UG", decimals: 0,
          currency_decimals: { UGX: 0, USD: 2, KWD: 3 },
          tax_label: "VAT", tax_rates: [0],
          payment_modes: ["Cash", "Card"],
          tender_modes: [
            { mode: "Cash", currencies: ["UGX", "USD"] },
            { mode: "Card", currencies: ["UGX", "USD"] },
          ],
          fx_currencies: ["USD"],
        })
      }
      if (method === "kamra.api.available_rooms") {
        return Promise.resolve([{ name: "STD-101", room_number: "101", housekeeping_status: "Clean" }])
      }
      if (method === "kamra.cashier.my_till") return Promise.resolve({ open: true })
      if (method === "kamra.ledger.list_exchange_rates") {
        return Promise.resolve({
          business_date: "2026-12-01",
          rates: [{ currency: "USD", rate_date: "2026-12-01", buy_rate: FX_RATE, settlement_rate: FX_RATE }],
        })
      }
      if (method === "kamra.ledger.quote_fx") {
        const amt = Number(params.foreign_amount ?? 0)
        return Promise.resolve({
          quote_id: "FXQ-1", foreign_amount: amt, currency: "USD",
          rate: FX_RATE, home_amount: Math.round(amt * FX_RATE),
        })
      }
      if (method === "kamra.rates.rate_options") {
        return Promise.resolve({ nights: [], plans: [], voucher: null, plain: { room_total: GROSS_UGX, amount_after_tax: GROSS_UGX, discount: 0 }, tax_inclusive: false })
      }
      if (method === "kamra.api.get_quote") {
        const discType = params.discount_type as string | undefined
        const discVal = Number(params.discount_value ?? 0)
        const discCur = (params.discount_currency as string | undefined) ?? "USD"
        const isUsdAmt = discType === "Amount" && discVal > 0 && discCur === "USD"
        const roomDiscUsd = isUsdAmt ? discVal : 0
        const roomNetUsd = 150 - roomDiscUsd
        const roomNetUgx = isUsdAmt ? NET_UGX : GROSS_UGX
        const roomDiscUgx = isUsdAmt ? DISC_UGX : 0
        return Promise.resolve({
          nights: 1,
          property_currency: "UGX",
          rate_currency: "USD",
          discount_currency: isUsdAmt ? "USD" : null,
          agreed_rate: GROSS_UGX,
          tariff: {
            currency: "USD", nightly: 150, property_currency: "UGX",
            fx_rate: FX_RATE, fx_date: "2026-11-24",
          },
          contract_summary: {
            currency: "USD", fx_rate: FX_RATE, fx_date: "2026-11-24",
            fx_lock_policy: "Fixed at Reservation",
            room_base_total: 150, room_discount_total: roomDiscUsd,
            room_net_total: roomNetUsd, room_tax_included: false,
            room_tax_amount: 0, room_after_tax_total: roomNetUsd,
            has_home_currency_extras: false, home_extras_total: 0,
          },
          nightly: [{
            date: "2026-12-01", rate: roomNetUgx, base_rate: GROSS_UGX,
            discount_amount: roomDiscUgx, discount_type: discType ?? null,
            discount_value: discVal, discount_currency: isUsdAmt ? "USD" : null,
            contract_currency: "USD", contract_base_rate: 150,
            contract_discount_amount: roomDiscUsd, contract_rate: roomNetUsd, fixed_rate: 0,
          }],
          room_total: roomNetUgx, room_base_total: GROSS_UGX, reservation_discount_total: roomDiscUgx,
          meal_total: 0, discount: roomDiscUgx, voucher_discount: 0, rate_plan: "BB-USD",
          amount_before_tax: roomNetUgx, tax_percent: 0, tax_amount: 0, amount_after_tax: roomNetUgx,
          advance: { percent: 0, season: null, amount: 0 }, tax_basis: null,
          cancellation: { free_now: true, free_until: null, fee_basis: "None", fee_label: "", non_refundable_season: null },
          ...(isUsdAmt ? {
            discount_governance: {
              base_total: GROSS_UGX, discount_total: DISC_UGX, net_total: NET_UGX,
              effective_pct: 33.33, max_pct: 33.33, limit_pct: 40,
              needs_supervisor: false, can_set_rate: true, agent: false, eligible: true,
              comp: false, floor_breach: [], reason_requires_supervisor: false, supervisors: [],
            },
          } : {}),
        })
      }
      if (method === "kamra.api.walk_in") {
        return Promise.resolve({ reservation: "RES-WI-1", room: "STD-101", room_number: "101", folio: "FOL-1", checked_in: 1 })
      }
      return Promise.resolve(null)
    })
  })

  it("shows USD 150.00 - USD 50.00 = USD 100.00 (+ UGX equiv) in Quote rail and Collect USD 100.00 CTA", async () => {
    await act(async () => {
      root.render(
        <MemoryRouter>
          <BookingDialog
            initial={{ walkIn: true, room_type: "STD", room: "STD-101", room_number: "101", guest_name: "Tankhill Guest", nights: 1 }}
            onClose={() => {}}
            onBooked={() => {}}
          />
        </MemoryRouter>,
      )
    })
    await until(() => expect(byLabel("Discount value")).not.toBeNull())

    // Pick Amount / night, enter 50, choose reason
    await press("Amount / night")
    await typeInto(byLabel<HTMLInputElement>("Discount value")!, "50")
    await choose(byLabel<HTMLSelectElement>("Discount reason")!, "RATE-PROMO")

    await until(() => {
      const qCalls = calls("kamra.api.get_quote")
      const latest = qCalls.at(-1)
      expect(latest?.discount_type).toBe("Amount")
      expect(latest?.discount_value).toBe(50)
      expect(latest?.discount_currency).toBe("USD")
    })

    // Quote rail shows primary USD lines AND UGX equivalents
    await until(() => {
      const body = text()
      expect(body).toContain("USD 150.00")
      expect(body).toContain("USD 50.00")
      expect(body).toContain("USD 100.00")
      expect(body).toContain("UGX 612,875")
      expect(body).toContain("UGX 204,292")
      expect(body).toContain("UGX 408,583")
    })

    // Switch tender currency to USD and verify exact Collect USD 100.00 CTA (NEVER USD 149.99!)
    const usdPayBtn = Array.from(host.querySelectorAll("button")).find(
      (b) => b.textContent?.trim() === "USD" && !b.closest('[aria-label="Discount currency"]'),
    )
    expect(usdPayBtn, "Missing USD tender button").toBeDefined()
    await act(async () => usdPayBtn!.click())

    await until(() => {
      const body = text()
      expect(body).toContain("Collect USD 100.00 and continue to check-in")
      expect(body).not.toContain("USD 149.99")
    })
  })
})

describe("AgreedRatePanel & ReservationDetail: multi-currency discount and USD fixed override", () => {
  it("AgreedRatePanel sends discount_currency USD on Discount tab and override_currency USD on Fixed rate tab", async () => {
    rpc.mockReset().mockImplementation((method: string) => {
      if (method === "kamra.ledger.list_reason_codes") return Promise.resolve(REASONS)
      if (method === "kamra.api.discount_preview") {
        return Promise.resolve({
          base_total: GROSS_UGX, discount_total: DISC_UGX, net_total: NET_UGX,
          effective_pct: 33.33, max_pct: 33.33, limit_pct: 40,
          needs_supervisor: false, can_set_rate: true, agent: false, eligible: true,
          comp: false, floor_breach: [], reason_requires_supervisor: false, supervisors: [],
          current_amount: GROSS_UGX, new_amount: NET_UGX, difference: -DISC_UGX, needs_confirmation: true,
          nights: [{
            date: "2026-12-01", base_rate: GROSS_UGX, discount_amount: DISC_UGX, rate: NET_UGX,
            entry_base_rate: GROSS_UGX, entry_rate: NET_UGX,
            contract_currency: "USD", contract_base_rate: 150, contract_discount_amount: 50, contract_rate: 100,
            charged: false,
          }],
        })
      }
      if (method === "kamra.api.rate_override_preview") {
        return Promise.resolve({
          current_amount: GROSS_UGX, new_amount: 449442, difference: -163433,
          current_nightly: GROSS_UGX, agreed_rate: 449442,
          override_currency: "USD", override_amount: 110, fx_rate: FX_RATE,
          reduction_pct: 26.67, limit_pct: 40, can_set_rate: true, raises: false,
          needs_supervisor: false, needs_confirmation: true, supervisors: [],
          nights: [{ date: "2026-12-01", rate: 449442 }],
        })
      }
      if (method === "kamra.api.set_agreed_rate") return Promise.resolve({ ok: true })
      return Promise.resolve(null)
    })

    await act(async () => {
      root.render(
        <AgreedRatePanel
          reservation="RES-MC-1"
          rateCurrency="USD"
          homeCurrency="UGX"
          fxRate={FX_RATE}
          nightRates={[{
            date: "2026-12-01", rate: GROSS_UGX, base_rate: GROSS_UGX,
            entry_base_rate: GROSS_UGX, contract_currency: "USD", contract_base_rate: 150, contract_rate: 150,
            charged: false,
          }]}
          current={{ discount_type: null, discount_value: 0, discount_currency: null, discount_reason: null, fixed_rate: 0 }}
          onSaved={() => {}}
        />,
      )
    })

    await press("Stay rate & daily details")
    await press("Amount / night")
    await typeInto(byLabel<HTMLInputElement>("Discount value")!, "50")
    await until(() => {
      const prev = calls("kamra.api.discount_preview").at(-1)
      expect(prev?.discount_currency).toBe("USD")
    })

    // Switch to Fixed rate tab -> defaults to USD step="0.01", sends override_currency="USD"
    await press("Fixed rate")
    const rateInput = host.querySelector<HTMLInputElement>('input[type="number"]')!
    expect(rateInput.step).toBe("0.01")
    await typeInto(rateInput, "110")
    await until(() => {
      const ov = calls("kamra.api.rate_override_preview").at(-1)
      expect(ov?.agreed_rate).toBe(110)
      expect(ov?.override_currency).toBe("USD")
    })
  })

  it("ReservationDetail displays USD 50.00 off a night instead of UGX 50 off a night", async () => {
    rpc.mockReset().mockImplementation((method: string) => {
      if (method === "kamra.api.reservation_detail") {
        return Promise.resolve({
          name: "RES-MC-DETAIL", status: "Confirmed", booking_type: "Individual", source: "Walk-in", channel: null,
          property: "Tankhill", property_currency: "UGX", rate_currency: "USD",
          room: "STD-101", room_number: "101", room_type: "STD", room_type_name: "Standard Double",
          rate_plan: "BB-USD", rate_plan_name: "Bed & Breakfast (CP)",
          check_in_date: "2026-12-01", check_out_date: "2026-12-02", nights: 1, adults: 2, children: 0,
          tariff: { currency: "USD", nightly: 150, property_currency: "UGX", fx_rate: FX_RATE, fx_date: "2026-11-24" },
          agreed_rate: GROSS_UGX,
          guest: { name: "G-1", full_name: "Tankhill Guest", phone: "+256700000000", email: null, vip: 0, blacklisted: 0, stays: 1, last_stay: null },
          night_rates: [{
            date: "2026-12-01", rate: NET_UGX, base_rate: GROSS_UGX, discount_amount: DISC_UGX,
            entry_base_rate: GROSS_UGX, entry_rate: NET_UGX,
            discount_type: null, discount_value: 0, discount_currency: null,
            contract_currency: "USD", contract_base_rate: 150, contract_discount_amount: 50, contract_rate: 100,
            charged: false,
          }],
          discount: {
            discount_type: "Amount", discount_value: 50, discount_currency: "USD",
            discount_reason: "RATE-PROMO", fixed_rate: 0,
            rate_currency: "USD", property_currency: "UGX", fx_rate: FX_RATE,
            contract_base_nightly: 150, contract_discount_amount: 50, contract_net_nightly: 100,
          },
          reason_categories: { discount: ["Rate Change", "Adjust"], agreed_rate: ["Rate Change"] },
          actions: { can_amend: true, can_set_rate: true }, warnings: {}, occupants: [], checkin_backfill: [],
          amount_after_tax: NET_UGX, advance_paid: 0,
          money: { total: NET_UGX, paid: 0, due: NET_UGX, has_folio: false, source: "booking" },
        })
      }
      return Promise.resolve(null)
    })

    await act(async () => {
      root.render(
        <MemoryRouter>
          <ReservationDetail row={{ name: "RES-MC-DETAIL" }} reload={() => {}} onClose={() => {}} />
        </MemoryRouter>,
      )
    })

    await until(() => {
      const body = text()
      expect(body).toContain("Tankhill Guest")
      expect(body).toContain("USD 50.00 off a night")
      expect(body).not.toContain("UGX 50 off a night")
      expect(body).toContain("USD 150.00")
      expect(body).toContain("USD 100.00")
    })
  })
})
