// @vitest-environment jsdom
/* Reservation discounts, round 6 (Gap Record #8555; Paul's standing
 * direction: nothing a client configures in SAP or OPERA is hard-coded).
 * The reservation screen holds no role or reason-category list: the server
 * says whether this person may fix a rate (actions.can_set_rate) and which
 * reason-code categories a discount and an agreed rate take
 * (reason_categories, from the property's settings). */
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
    guestAlert: async () => ({ guest: null, blacklisted: 0, blacklist_reason: null }),
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

import { agreedRateReasonCodes, discountReasonCodes } from "../src/lib/agreedRate"
import ReservationDetail from "../src/screens/ReservationDetail"

const NIGHTS = ["2031-03-17", "2031-03-18"]
const REASONS = [
  { name: "RATE-PROMO", code: "RATE-PROMO", description: "Promotional rate change", category: "Rate Change", requires_supervisor: 0 },
  { name: "RATE-MGR", code: "RATE-MGR", description: "Manager rate override", category: "Rate Change", requires_supervisor: 1 },
  { name: "ADJ-RATE", code: "ADJ-RATE", description: "Rate correction", category: "Adjust", requires_supervisor: 0 },
  { name: "NC-MGMT", code: "NC-MGMT", description: "Management NC", category: "NC", requires_supervisor: 1 },
  { name: "VOID-ERR", code: "VOID-ERR", description: "Keyed in error", category: "Void", requires_supervisor: 0 },
]

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
const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 0)))
const until = (check: () => void | Promise<void>) =>
  vi.waitFor(async () => { await settle(); await check() }, { timeout: 3000, interval: 20 })
const calls = (method: string) => rpc.mock.calls.filter(([m]) => m === method).map(([, p]) => p as Record<string, unknown>)
const byLabel = <T extends Element>(label: string) => host.querySelector<T>(`[aria-label="${label}"]`)
const optionValues = (select: HTMLSelectElement | null) =>
  Array.from(select?.querySelectorAll("option") ?? []).map((o) => o.value)
const button = (label: string) =>
  Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.trim() === label)
async function press(label: string) {
  expect(button(label), `Missing button: ${label}`).toBeDefined()
  await act(async () => button(label)!.click())
}

const detail = (over: Record<string, unknown> = {}) => ({
  name: "RES-9", status: "Confirmed", booking_type: "Individual", source: "Direct", channel: null,
  property: "Test Hotel", room: "STD-101", room_number: "101", room_type: "STD", room_type_name: "Standard",
  check_in_date: NIGHTS[0], check_out_date: "2031-03-19", nights: 2, adults: 2, children: 0,
  guest: { name: "G-1", full_name: "Ada Guest", phone: "+254700000000", email: "ada@example.com", vip: 0,
    blacklisted: 0, stays: 1, last_stay: null },
  night_rates: NIGHTS.map((date) => ({ date, rate: 900, base_rate: 1000, discount_amount: 100,
    discount_type: null, discount_value: 0, entry_base_rate: 1000, entry_rate: 900, charged: false })),
  discount: { discount_type: "Percent", discount_value: 10, discount_reason: "ADJ-RATE", fixed_rate: 1 },
  reason_categories: { discount: ["Adjust", "NC"], agreed_rate: ["Rate Change"] },
  actions: { can_amend: true, can_set_rate: false }, warnings: {}, occupants: [], checkin_backfill: [],
  amount_after_tax: 1800, advance_paid: 0,
  money: { total: 1800, paid: 0, due: 1800, has_folio: false, source: "booking" }, ...over,
})

describe("Reservation screen (R6)", () => {
  let shown = detail()
  beforeEach(() => {
    shown = detail()
    rpc.mockReset().mockImplementation((method: string) => {
      if (method === "kamra.api.reservation_detail") return Promise.resolve(shown)
      if (method === "kamra.ledger.list_reason_codes") return Promise.resolve(REASONS)
      if (method === "kamra.api.discount_preview") return Promise.resolve({
        base_total: 2000, discount_total: 200, net_total: 1800, effective_pct: 10, max_pct: 10, limit_pct: 10,
        needs_supervisor: false, can_set_rate: false, agent: false, eligible: false, comp: false, floor_breach: [],
        reason_requires_supervisor: false, supervisors: [], current_amount: 1800, new_amount: 1800,
        difference: 0, needs_confirmation: false,
        nights: NIGHTS.map((date) => ({ date, base_rate: 1000, discount_amount: 100, rate: 900, charged: false })) })
      return Promise.resolve(null)
    })
  })
  async function openPanel() {
    await act(async () => root.render(<MemoryRouter><ReservationDetail row={{ name: "RES-9" }}
      reload={() => {}} onClose={() => {}} /></MemoryRouter>))
    await until(() => expect(text()).toContain("Ada Guest"))
    await press("Stay rate & daily details")
    await until(() => expect(calls("kamra.api.discount_preview").length).toBeGreaterThan(0))
  }

  it("takes who may fix a rate from the server, not from a role list", async () => {
    shown = detail({ actions: { can_amend: true, can_set_rate: true } })
    await openPanel()
    expect(calls("kamra.api.discount_preview")[0]).toMatchObject({ reservation: "RES-9", fixed_rate: 1 })
    await act(async () => root.unmount())
    root = createRoot(host)
    rpc.mockClear()
    // a role the old list named, which the server says may not fix a rate
    auth.roles = ["Revenue Manager"]
    shown = detail({ actions: { can_amend: true, can_set_rate: false } })
    await openPanel()
    expect(calls("kamra.api.discount_preview")[0]).toMatchObject({ fixed_rate: 0 })
  })

  it("offers the reason codes of the categories the property set", async () => {
    await openPanel()
    await until(() => expect(optionValues(byLabel("Discount reason")).length).toBeGreaterThan(1))
    expect(optionValues(byLabel("Discount reason")).sort()).toEqual(["", "ADJ-RATE", "NC-MGMT"])
    await press("Fixed rate")
    expect(optionValues(byLabel("Reason…")).sort()).toEqual(["", "RATE-MGR", "RATE-PROMO"])
    expect(calls("kamra.ledger.list_reason_codes")).toHaveLength(1)
  })
})

describe("Reason code rules (R6)", () => {
  it("keep each code once, from the categories they are given", () => {
    const served = [...REASONS, { ...REASONS[2] }]
    expect(discountReasonCodes(served, ["Adjust", "NC"]).map((r) => r.name)).toEqual(["ADJ-RATE", "NC-MGMT"])
    expect(agreedRateReasonCodes(served, ["Rate Change"]).map((r) => r.name)).toEqual(["RATE-PROMO", "RATE-MGR"])
    expect(agreedRateReasonCodes(served, ["Adjust"]).map((r) => r.name)).toEqual(["ADJ-RATE"])
  })
})
