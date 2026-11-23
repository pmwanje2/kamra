// @vitest-environment jsdom
/* Complimentary and House Use rate plans, round 4 (Hydrogen's R3 review &
 * live check of #38):
 * - ReservationDetail Stay card (OPERA Stay Details) lets Front Desk change a
 *   reservation's rate plan through Amend stay, previews the re-priced total
 *   and charged-nights notice, requires a manager's comp sign-off (and handles
 *   dual discount + comp sign-off), and sends rate_plan + comp_supervisor_*
 *   to kamra.api.amend_stay. */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const { rpc } = vi.hoisted(() => {
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
    reservationDetail: (name: string) => call("kamra.api.reservation_detail", { reservation: name }),
    amendStayPreview: (
      reservation: string,
      check_in_date: string,
      check_out_date: string,
      opts?: { rate_plan?: string },
    ) =>
      call("kamra.api.amend_stay_preview", {
        reservation,
        check_in_date,
        check_out_date,
        ...(opts?.rate_plan ? { rate_plan: opts.rate_plan } : {}),
      }),
    amendStay: (
      reservation: string,
      check_in_date: string,
      check_out_date: string,
      confirm_price = 0,
      signOff: Record<string, unknown> = {},
    ) =>
      call("kamra.api.amend_stay", {
        reservation,
        check_in_date,
        check_out_date,
        confirm_price,
        ...signOff,
      }),
  }
})
vi.mock("../src/lib/auth", () => ({
  useAuth: () => ({ roles: ["Front Desk"], user: "desk@test", full_name: "Front Desk" }),
}))
vi.mock("../src/lib/realtime", () => ({
  useRealtime: () => {},
}))

import ReservationDetail from "../src/screens/ReservationDetail"

const GM = { user: "gm@test", full_name: "General Manager" }
const FIN = { user: "fin@test", full_name: "Finance Manager" }
const BOTH = { user: "both@test", full_name: "Dual Approver" }

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  localStorage.clear()
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
const allByLabel = <T extends Element>(label: string, scope: ParentNode = host) =>
  Array.from(scope.querySelectorAll<T>(`[aria-label="${label}"]`))
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
const buttonByText = (label: string, scope: ParentNode = host) =>
  Array.from(scope.querySelectorAll("button")).find((b) => b.textContent?.trim() === label)
async function press(label: string, scope: ParentNode = host) {
  const button = buttonByText(label, scope)
  expect(button, `Missing button: ${label}`).toBeDefined()
  await act(async () => button!.click())
}
const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 0)))
const until = (check: () => void | Promise<void>) =>
  vi.waitFor(async () => { await settle(); await check() }, { timeout: 3000, interval: 20 })

const RATE_PLANS = [
  { name: "Test Hotel-BAR", rate_plan_name: "Best Available", code: "BAR", rate_category: "Standard" },
  { name: "Test Hotel-CORP", rate_plan_name: "Corporate Flex", code: "CORP", rate_category: "Standard" },
  { name: "Test Hotel-COMP", rate_plan_name: "Complimentary", code: "COMP", rate_category: "Complimentary" },
]

function makeDetail(over: Record<string, unknown> = {}) {
  return {
    name: "RES-9",
    status: "Checked In",
    booking_type: "Individual",
    source: "Manual",
    channel: null,
    property: "Test Hotel",
    room: "STD-101",
    room_number: "101",
    room_type: "STD",
    room_type_name: "Standard",
    rate_plan: "Test Hotel-BAR",
    rate_plan_name: "Best Available",
    rate_category: "Standard",
    rate_plans: RATE_PLANS,
    check_in_date: "2031-03-11",
    check_out_date: "2031-03-14",
    nights: 3,
    adults: 2,
    children: 0,
    guest: {
      name: "G-1", full_name: "Ada Guest", phone: "+254700000000", email: "ada@example.com",
      vip: 0, blacklisted: 0, stays: 1, last_stay: null,
    },
    night_rates: [
      { date: "2031-03-11", rate: 1000, base_rate: 1000, discount_amount: 0, charged: true },
      { date: "2031-03-12", rate: 1000, base_rate: 1000, discount_amount: 0, charged: false },
      { date: "2031-03-13", rate: 1000, base_rate: 1000, discount_amount: 0, charged: false },
    ],
    discount: { discount_type: null, discount_value: 0, discount_reason: null, fixed_rate: 0 },
    actions: { can_amend: true, can_set_rate: false, can_check_in: false, can_check_out: true, can_cancel: false, can_reinstate: false },
    warnings: { id_document_missing: false, id_unverified: false },
    occupants: [],
    checkin_backfill: [],
    amount_after_tax: 3000,
    advance_paid: 0,
    company: null,
    travel_agent: null,
    folio_name: "FOLIO-1",
    precheckin_status: null,
    precheckin_token: null,
    id_document: null,
    id_document_source: null,
    id_document_on: null,
    id_document_discarded: 0,
    precheckin_verified_by: null,
    precheckin_verified_on: null,
    deposit: null,
    security_deposit: null,
    booker: null,
    cancellation: null,
    money: { total: 3000, paid: 0, due: 3000, has_folio: true, source: "folio" },
    ...over,
  }
}

describe("ReservationDetail: Amend stay rate-plan change with comp sign-off (R4 UX #5)", () => {
  beforeEach(() => rpc.mockReset())

  async function renderDetail() {
    await act(async () =>
      root.render(<MemoryRouter><ReservationDetail row={{ name: "RES-9" }} reload={() => {}} onClose={() => {}} /></MemoryRouter>),
    )
    await until(() => expect(text()).toContain("Ada Guest"))
  }

  it("changes an in-house stay to a Comp plan for remaining nights with a manager's sign-off", async () => {
    rpc.mockImplementation((method: string, params: Record<string, unknown>) => {
      if (method === "kamra.api.reservation_detail") return Promise.resolve(makeDetail())
      if (method === "kamra.api.amend_stay_preview") {
        return Promise.resolve({
          current_amount: 3000,
          new_amount: 1000,
          difference: -2000,
          needs_confirmation: true,
          nights: [
            { date: "2031-03-11", rate: 1000, demand_premium_pct: 0 },
            { date: "2031-03-12", rate: 0, demand_premium_pct: 0 },
            { date: "2031-03-13", rate: 0, demand_premium_pct: 0 },
          ],
          charged_nights: {
            policy: "Keep charged nights",
            nights: [{ date: "2031-03-11", charged: 1000, new: 0, action: "kept" }],
            kept_difference: 1000,
            credit_total: 0,
          },
          discount_governance: null,
          needs_supervisor: false,
          supervisors: [],
          comp_governance: params.rate_plan === "Test Hotel-COMP"
            ? { rate_category: "Complimentary", needs_supervisor: true, agent: false, supervisors: [GM, FIN] }
            : { rate_category: "Standard", needs_supervisor: false, agent: false, supervisors: [] },
        })
      }
      if (method === "kamra.api.amend_stay") return Promise.resolve({ nights: 3, amount_after_tax: 1000 })
      return Promise.resolve(null)
    })

    await renderDetail()
    const select = byLabel<HTMLSelectElement>("Rate plan")
    expect(select).not.toBeNull()
    expect(select!.value).toBe("Test Hotel-BAR")

    await choose(select!, "Test Hotel-COMP")
    await until(() =>
      expect(rpc.mock.calls.some(([m]) => m === "kamra.api.amend_stay_preview")).toBe(true),
    )
    const previewCall = rpc.mock.calls.find(([m]) => m === "kamra.api.amend_stay_preview")![1]
    expect(previewCall).toMatchObject({
      reservation: "RES-9",
      check_in_date: "2031-03-11",
      check_out_date: "2031-03-14",
      rate_plan: "Test Hotel-COMP",
    })

    await until(() => expect(text()).toContain("A Complimentary rate plan needs a manager's sign-off."))
    expect(text()).toContain("Applies from the current business date for remaining nights; posted nights keep their rate.")
    const confirmBtn = buttonByText("Confirm rate plan and price")
    expect(confirmBtn).toBeDefined()
    expect(confirmBtn!.disabled).toBe(true)

    await choose(byLabel<HTMLSelectElement>("Manager")!, FIN.user)
    await typeInto(byLabel<HTMLInputElement>("Manager PIN")!, "5678")
    expect(buttonByText("Confirm rate plan and price")!.disabled).toBe(false)

    await press("Confirm rate plan and price")
    await until(() => expect(rpc.mock.calls.some(([m]) => m === "kamra.api.amend_stay")).toBe(true))
    const amendCall = rpc.mock.calls.find(([m]) => m === "kamra.api.amend_stay")![1]
    expect(amendCall).toMatchObject({
      reservation: "RES-9",
      check_in_date: "2031-03-11",
      check_out_date: "2031-03-14",
      confirm_price: 1,
      rate_plan: "Test Hotel-COMP",
      comp_supervisor_user: FIN.user,
      comp_supervisor_pin: "5678",
    })
  })

  it("changes to a Standard plan without prompting for comp sign-off", async () => {
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.reservation_detail") return Promise.resolve(makeDetail())
      if (method === "kamra.api.amend_stay_preview") {
        return Promise.resolve({
          current_amount: 3000,
          new_amount: 2600,
          difference: -400,
          needs_confirmation: true,
          nights: [],
          discount_governance: null,
          needs_supervisor: false,
          supervisors: [],
          comp_governance: { rate_category: "Standard", needs_supervisor: false, agent: false, supervisors: [] },
        })
      }
      if (method === "kamra.api.amend_stay") return Promise.resolve({ nights: 3, amount_after_tax: 2600 })
      return Promise.resolve(null)
    })

    await renderDetail()
    await choose(byLabel<HTMLSelectElement>("Rate plan")!, "Test Hotel-CORP")
    await until(() => expect(buttonByText("Confirm rate plan and price")).toBeDefined())
    expect(byLabel("Manager PIN")).toBeNull()
    expect(buttonByText("Confirm rate plan and price")!.disabled).toBe(false)

    await press("Confirm rate plan and price")
    await until(() => expect(rpc.mock.calls.some(([m]) => m === "kamra.api.amend_stay")).toBe(true))
    const amendCall = rpc.mock.calls.find(([m]) => m === "kamra.api.amend_stay")![1]
    expect(amendCall).toMatchObject({
      reservation: "RES-9",
      confirm_price: 1,
      rate_plan: "Test Hotel-CORP",
    })
    expect(amendCall).not.toHaveProperty("comp_supervisor_user")
  })

  it("handles dual discount and comp sign-off (two managers, or one covering both)", async () => {
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.reservation_detail") return Promise.resolve(makeDetail())
      if (method === "kamra.api.amend_stay_preview") {
        return Promise.resolve({
          current_amount: 3000,
          new_amount: 1000,
          difference: -2000,
          needs_confirmation: true,
          nights: [],
          discount_governance: {
            base_total: 2000, discount_total: 1000, net_total: 1000,
            effective_pct: 50, max_pct: 50, limit_pct: 10,
            needs_supervisor: true, can_set_rate: false, agent: false,
            eligible: true, comp: false, floor_breach: [], reason_requires_supervisor: false,
            supervisors: [GM, BOTH],
          },
          needs_supervisor: true,
          supervisors: [GM, BOTH],
          comp_governance: {
            rate_category: "Complimentary", needs_supervisor: true, agent: false, supervisors: [FIN, BOTH],
          },
        })
      }
      if (method === "kamra.api.amend_stay") return Promise.resolve({ nights: 3, amount_after_tax: 1000 })
      return Promise.resolve(null)
    })

    await renderDetail()
    await choose(byLabel<HTMLSelectElement>("Rate plan")!, "Test Hotel-COMP")
    await until(() => expect(allByLabel<HTMLSelectElement>("Manager").length).toBe(2))

    // Picking a manager who covers both hides the second sign-off
    await choose(allByLabel<HTMLSelectElement>("Manager")[0], BOTH.user)
    await until(() => expect(allByLabel<HTMLSelectElement>("Manager").length).toBe(1))
    await typeInto(allByLabel<HTMLInputElement>("Manager PIN")[0], "1357")
    expect(buttonByText("Confirm rate plan and price")!.disabled).toBe(false)

    // Switching back to GM (discount-only) re-opens the second prompt for Finance
    await choose(allByLabel<HTMLSelectElement>("Manager")[0], GM.user)
    await until(() => expect(allByLabel<HTMLSelectElement>("Manager").length).toBe(2))
    expect(buttonByText("Confirm rate plan and price")!.disabled).toBe(true)

    await choose(allByLabel<HTMLSelectElement>("Manager")[1], FIN.user)
    await typeInto(allByLabel<HTMLInputElement>("Manager PIN")[1], "5678")
    expect(buttonByText("Confirm rate plan and price")!.disabled).toBe(false)

    await press("Confirm rate plan and price")
    await until(() => expect(rpc.mock.calls.some(([m]) => m === "kamra.api.amend_stay")).toBe(true))
    expect(rpc.mock.calls.find(([m]) => m === "kamra.api.amend_stay")![1]).toMatchObject({
      rate_plan: "Test Hotel-COMP",
      supervisor_user: GM.user,
      supervisor_pin: "1357",
      comp_supervisor_user: FIN.user,
      comp_supervisor_pin: "5678",
    })
  })
})
