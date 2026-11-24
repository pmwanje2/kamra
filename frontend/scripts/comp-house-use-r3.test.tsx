// @vitest-environment jsdom
/* Complimentary and House Use rate plans, round 3 (Hydrogen's R2 review &
 * live check of #38):
 * - Today reads server occupancy_now and forecast_summary directly (no client
 *   occupancy maths);
 * - AuditPack labels gross room revenue "Rooms", not "Rooms (net of discounts)";
 * - GroupControl pickup and GroupBookingDialog rooming list prompt for a
 *   manager's comp sign-off and forward comp_supervisor_user/pin. */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom"
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
    getSnapshot: () => call("kamra.api.front_desk_snapshot", { property: "Test Hotel" }),
    getBookingOptions: () => call("kamra.api.booking_options", { property: "Test Hotel" }),
    setHousekeepingStatus: vi.fn(),
  }
})
vi.mock("../src/lib/auth", () => ({
  useAuth: () => ({ roles: ["Front Desk"], user: "desk@test", full_name: "Front Desk" }),
}))
vi.mock("../src/lib/realtime", () => ({
  useRealtime: () => {},
}))
vi.mock("../src/lib/cashierAuth", () => ({
  useCashierAuth: () => ({ ensureUnlocked: vi.fn(), status: { required: false } }),
}))

import Today from "../src/screens/Today"
import { AuditPackView } from "../src/screens/AuditPack"
import GroupControl from "../src/components/GroupControl"
import GroupBookingDialog from "../src/components/GroupBookingDialog"

const RM = { user: "rm@test", full_name: "Revenue Manager" }

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
const byLabel = <T extends Element>(label: string, scope: ParentNode | null = host) =>
  scope?.querySelector<T>(`[aria-label="${label}"]`) ?? null

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
const buttonByText = (label: string, scope: ParentNode = host) =>
  Array.from(scope.querySelectorAll("button")).find((b) => b.textContent?.trim() === label)
const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 0)))
const until = (check: () => void | Promise<void>) =>
  vi.waitFor(async () => { await settle(); await check() }, { timeout: 3000, interval: 20 })

function buildForecast() {
  const days = []
  for (let i = 0; i < 90; i++) {
    const dt = new Date("2031-03-12T00:00:00Z")
    dt.setUTCDate(dt.getUTCDate() + i)
    const date = dt.toISOString().slice(0, 10)
    const busy = i < 3
    days.push({
      date,
      booked: busy ? 4 : 0,
      rooms_occupied: busy ? 3 : 0,
      held: 0,
      available: busy ? 0 : 4,
      occupancy_pct: busy ? 75 : 0,
      occupancy_with_holds_pct: busy ? 75 : 0,
    })
  }
  return days
}

function renderToday() {
  return act(async () => {
    root.render(
      <MemoryRouter>
        <Routes>
          <Route element={<Outlet context={{ refreshKey: 0 }} />}>
            <Route index element={<Today />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )
  })
}

describe("Today: server occupancy_now and forecast_summary are the single source", () => {
  beforeEach(() => rpc.mockReset())

  function mockTodayRpc(occupancyPct: number) {
    const rooms = ["101", "102", "103", "104"].map((n) => ({
      name: `STD-${n}`, room_number: n, room_type: "STD", room_type_name: "Standard",
      floor: "1", housekeeping_status: "Dirty", occupancy_status: "Occupied",
    }))
    rpc.mockImplementation((method: string) => {
      switch (method) {
        case "kamra.api.front_desk_snapshot":
          return Promise.resolve({
            date: "2031-03-12", business_date: "2031-03-12",
            rooms, arrivals: [], departures_due: [], departures_overdue: [], checked_out: [], in_house: [],
          })
        case "kamra.dashboards.property_dashboard":
          return Promise.resolve({
            figures_hidden: true,
            occupancy_now: { occupied: 3, sellable: 4, occupancy_pct: occupancyPct },
            housekeeping: {
              room_status: { Clean: 0, Dirty: 4, Inspected: 0, "Out of Order": 0 },
              occupied: 4, vacant: 0, open_tasks: 0, overdue_tasks: 0,
            },
            forecast: buildForecast(),
            forecast_summary: {
              spans: [
                { days: 30, occupancy_pct: 7.5 },
                { days: 60, occupancy_pct: 3.8 },
                { days: 90, occupancy_pct: 2.5 },
              ],
              weeks: [
                { week_of: "2031-03-12", booked: 12, rooms_occupied: 9, held: 0, occupancy_pct: 32.1 },
              ],
            },
          })
        case "kamra.booking_requests.booking_requests":
          return Promise.resolve({ holds_inventory: false, count: 0, requests: [] })
        case "kamra.today_layout.get_today_layout":
          return Promise.resolve(null)
        default:
          return Promise.resolve(null)
      }
    })
  }

  it("renders server occupancy_now (3 of 4, 75%) and forecast_summary (7.5%, 32.1%) under House Use: no occupancy", async () => {
    mockTodayRpc(75)
    await renderToday()
    await until(() => expect(text()).toContain("3 of 4 rooms"))
    expect(text()).toContain("75%")
    expect(text()).toContain("75% occupied · 3 of 4")
    expect(text()).not.toContain("100%")
    expect(text()).not.toContain("4 of 4 rooms")
    // Forecast summary spans & weekly table use server figures, never client booked / rooms (10% / 43%)
    expect(text()).toContain("7.5%")
    expect(text()).toContain("3.8%")
    expect(text()).toContain("2.5%")
    expect(text()).toContain("32.1%")
    expect(text()).not.toContain("10%")
    expect(text()).not.toContain("43%")
  })

  it("renders server occupancy_pct verbatim without recomputing occupied / sellable", async () => {
    mockTodayRpc(75.5)
    await renderToday()
    await until(() => expect(text()).toContain("75.5%"))
    expect(text()).toContain("75.5% occupied · 3 of 4")
  })
})

describe("AuditPack: gross room revenue column label", () => {
  beforeEach(() => rpc.mockReset())

  it("labels room_revenue 'Rooms' rather than 'Rooms (net of discounts)'", async () => {
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.night_audit.night_audit_pack") {
        return Promise.resolve({
          audit: "EOD-1", has_pack: true, run: { status: "Completed" },
          pack: {
            property_name: "Test Hotel", business_date: "2031-03-11", generated_at: "2031-03-12 00:05",
            audit_report: {
              periods: [{
                label: "Day", from_date: "2031-03-11", rooms_sold: 4, occupancy_pct: 100,
                room_revenue: 2600, adr: 1300, revpar: 650, food: 0, beverage: 0,
                banquets: 0, meal_plan_minibar: 0, other: 0, allowances: -200,
                total_revenue: 2400, outlets: [],
                banquet: { functions: 0, fnb: 0, hall: 0, other: 0, total: 0 },
              }],
              balances: [],
            },
          },
        })
      }
      return Promise.resolve(null)
    })
    await act(async () => root.render(<MemoryRouter><AuditPackView audit="EOD-1" /></MemoryRouter>))
    await until(() => expect(host.querySelectorAll("th").length).toBeGreaterThan(0))
    const headers = Array.from(host.querySelectorAll("th")).map((th) => th.textContent?.trim())
    expect(headers).toContain("Rooms")
    expect(headers).toContain("Discounts & allowances")
    expect(headers).not.toContain("Rooms (net of discounts)")
  })
})

describe("GroupControl: comp pickup requires and sends supervisor sign-off", () => {
  beforeEach(() => rpc.mockReset())

  it("shows SupervisorSignOff on pickup and sends comp_supervisor_user and pin", async () => {
    rpc.mockImplementation((method: string) => {
      switch (method) {
        case "kamra.api.group_detail":
          return Promise.resolve({
            business_date: "2031-03-12",
            group: {
              name: "GRP-1", group_name: "Comp Corp Retreat", company: "CO-COMP",
              status: "Confirmed", check_in_date: "2031-03-17", check_out_date: "2031-03-19",
              cutoff_date: "2031-03-15", notes: null, group_type: "Corporate",
              master_reservation: null, master_pays: null, in_house: 0, rooms_total: 3,
              attrition_pct: 0, deposit_due: 0,
            },
            pickup: [{
              room_type: "STD", room_type_name: "Standard", rooms_blocked: 3, block_rate: 0,
              nightly_counts: {}, picked_up: 1, remaining: 2,
            }],
            money: { block_value: 0, booked: 0, deposits: 0, paid: 0, deposit_due: 0, due: 0 },
            attrition: {
              blocked_room_nights: 6, picked_room_nights: 2, pickup_pct: 33, allowed_pct: 0,
              shortfall_room_nights: 4, fee_estimate: 0, days_to_cutoff: 3, warn: false,
            },
            room_types: [{ name: "STD", room_type_name: "Standard" }],
            can_set_rate: false,
            rooming_list: [{
              name: "RES-1", guest_name: "First Guest", phone: null, room_type: "STD",
              room_type_name: "Standard", room: null, room_number: null, status: "Confirmed",
              check_in_date: "2031-03-17", check_out_date: "2031-03-19", pays_own_charges: 0,
              is_master: false, own_balance: 0, amount: 0,
            }],
            event: null, master_folio: null, master_folio_status: null, master_folio_balance: 0,
            comp_governance: {
              rate_category: "Complimentary", needs_supervisor: true, agent: false, supervisors: [RM],
            },
          })
        case "kamra.api.pickup_group_room":
          return Promise.resolve({ reservation: "RES-2" })
        case "kamra.api.property_locale":
          return Promise.resolve({ currency: "KES", currency_symbol: "KES ", locale: "en", tax_label: "VAT", tax_rates: [0], payment_modes: ["Cash"] })
        default:
          return Promise.resolve(null)
      }
    })

    await act(async () => root.render(
      <MemoryRouter><GroupControl row={{ name: "GRP-1" }} reload={() => {}} /></MemoryRouter>,
    ))
    await until(() => expect(host.querySelector('input[placeholder="Guest name"]')).not.toBeNull())
    expect(text()).toContain("A Complimentary rate plan needs a manager's sign-off.")

    const guestInput = host.querySelector<HTMLInputElement>('input[placeholder="Guest name"]')!
    await typeInto(guestInput, "Amina Guest")
    expect(buttonByText("Add guest")!.disabled).toBe(true)

    await choose(byLabel<HTMLSelectElement>("Manager")!, RM.user)
    await typeInto(byLabel<HTMLInputElement>("Manager PIN")!, "2468")
    expect(buttonByText("Add guest")!.disabled).toBe(false)

    await press("Add guest")
    const calls = rpc.mock.calls.filter(([m]) => m === "kamra.api.pickup_group_room").map(([, p]) => p)
    await until(() => expect(calls.length).toBe(1))
    expect(calls[0]).toMatchObject({
      group_booking: "GRP-1",
      room_type: "STD",
      guest_name: "Amina Guest",
      comp_supervisor_user: RM.user,
      comp_supervisor_pin: "2468",
    })
  })
})

describe("GroupBookingDialog: corporate comp block prompts once for supervisor sign-off", () => {
  beforeEach(() => rpc.mockReset())

  it("passes company to group_price_preview, requires sign-off to book, and sends comp_supervisor_user/pin", async () => {
    rpc.mockImplementation((method: string, params: Record<string, unknown>) => {
      switch (method) {
        case "kamra.api.booking_options":
          return Promise.resolve({
            room_types: [{ name: "STD", room_type_name: "Standard", base_price: 1000, adults_capacity: 2, children_capacity: 1 }],
            meal_plans: [],
            companies: [{ name: "CO-COMP", company_name: "Comp Partner Ltd" }],
            travel_agents: [], experiences: [], ota_channels: [], ota_commission: {},
            rate_plans: [], default_plan: null, rate_plan_required: false,
            can_set_rate: false, can_discount: false, rate_override_limit_pct: 0, discount_reasons: [],
            property: { sell_message: "", free_cancel_days: 0, cancellation_fee: "None", no_show_charge: "None",
              deposit_pct: 0, country: "Kenya", pets_allowed: 0, pet_charge: 0, pet_charge_basis: "Per stay",
              checkin_time: "14:00", checkout_time: "11:00" },
          })
        case "kamra.api.available_rooms":
          return Promise.resolve([
            { name: "STD-101", room_number: "101" },
            { name: "STD-102", room_number: "102" },
          ])
        case "kamra.api.group_price_preview":
          return Promise.resolve({
            rows: [{ room_type: "STD", room_type_name: "Standard", rooms: 1, room_nights: 1,
              group_rate: false, rate: 0, before_tax: 0, tax: 0, levy: 0, total: 0 }],
            nights: 1, before_tax: 0, tax: 0, levy: 0, total: 0, can_set_rate: false,
            comp_governance: params.company === "CO-COMP"
              ? { rate_category: "Complimentary", needs_supervisor: true, agent: false, supervisors: [RM] }
              : { rate_category: "Standard", needs_supervisor: false, agent: false, supervisors: [] },
          })
        case "kamra.api.create_group_with_rooming_list":
          return Promise.resolve({
            group_booking: "GRP-2", status: "Confirmed", created: ["RES-1"], skipped: [], warnings: [],
            blocked: 1, held: 0, cutoff_date: "2031-03-12",
          })
        default:
          return Promise.resolve(null)
      }
    })

    await act(async () => root.render(
      <MemoryRouter><GroupBookingDialog onClose={() => {}} onDone={() => {}} /></MemoryRouter>,
    ))
    await until(() => expect(host.querySelector('input[placeholder="e.g. Sharma Wedding"]')).not.toBeNull())

    // Step 1: pick company payer with negotiated comp plan
    await typeInto(host.querySelector<HTMLInputElement>('input[placeholder="e.g. Sharma Wedding"]')!, "Comp Offsite")
    const radios = host.querySelectorAll<HTMLInputElement>('input[type="radio"]')
    await act(async () => radios[1].click())
    const selects = host.querySelectorAll<HTMLSelectElement>("select")
    const companySelect = Array.from(selects).find((s) =>
      Array.from(s.options).some((o) => o.value === "CO-COMP"),
    )!
    await choose(companySelect, "CO-COMP")
    const contactLabel = Array.from(host.querySelectorAll("label")).find((l) =>
      l.textContent?.includes("Contact name"),
    )!
    await typeInto(contactLabel.querySelector("input")!, "Lina Host")
    await press("Next")

    // Step 2: wait for group_price_preview with company: "CO-COMP"
    await until(() => {
      const calls = rpc.mock.calls.filter(([m]) => m === "kamra.api.group_price_preview").map(([, p]) => p)
      expect(calls.some((p) => p.company === "CO-COMP")).toBe(true)
    })
    await press("Next")

    // Step 3: first room is pre-filled with contact name "Lina Host"
    await until(() => expect(byLabel<HTMLInputElement>("Guest for room 1")).not.toBeNull())
    await press("Next")

    // Step 4: sign-off required to book, quotation remains enabled
    await until(() => expect(byLabel("Manager PIN")).not.toBeNull())
    expect(text()).toContain("A Complimentary rate plan needs a manager's sign-off.")
    expect(buttonByText("Book group")!.disabled).toBe(true)
    expect(buttonByText("Save as quotation")!.disabled).toBe(false)

    await choose(byLabel<HTMLSelectElement>("Manager")!, RM.user)
    await typeInto(byLabel<HTMLInputElement>("Manager PIN")!, "2468")
    expect(buttonByText("Book group")!.disabled).toBe(false)

    await press("Book group")
    const created = rpc.mock.calls
      .filter(([m]) => m === "kamra.api.create_group_with_rooming_list")
      .map(([, p]) => p)
    await until(() => expect(created.length).toBe(1))
    expect(created[0]).toMatchObject({
      company: "CO-COMP",
      comp_supervisor_user: RM.user,
      comp_supervisor_pin: "2468",
    })
  })
})
