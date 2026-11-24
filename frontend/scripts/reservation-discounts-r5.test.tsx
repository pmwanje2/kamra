// @vitest-environment jsdom
/* Reservation discounts, round 5 (Gap Record #8555; Gemini 4 Neon's R4
 * re-review, P2-3): the reservation screen shows a discount given only in
 * Daily Details (OPERA's per-night discount) - what it takes off, its
 * reason and the night's base - as it shows the stay's own. */
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
    guestAlert: async () => ({ guest: null, blacklisted: 0, blacklist_reason: null }),
    reservationDetail: (reservation: string) => call("kamra.api.reservation_detail", { reservation }),
  }
})
vi.mock("../src/lib/auth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/lib/auth")>()),
  useAuth: () => ({ status: "authed", user: "desk@test", roles: ["Front Desk"],
    refresh: async () => {}, signOut: async () => {} }),
}))
vi.mock("../src/lib/realtime", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/lib/realtime")>()),
  useRealtime: () => {},
}))

import ReservationDetail from "../src/screens/ReservationDetail"

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
  vi.restoreAllMocks()
})

const text = () => host.textContent ?? ""
const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 0)))
const until = (check: () => void | Promise<void>) =>
  vi.waitFor(async () => { await settle(); await check() }, { timeout: 3000, interval: 20 })
/** The value a Field shows under its label (null: no such field). */
const field = (label: string) =>
  Array.from(host.querySelectorAll("dt")).find((dt) => dt.textContent?.trim() === label)
    ?.nextElementSibling?.textContent ?? null

const detail = {
  name: "RES-9", status: "Confirmed", booking_type: "Individual", source: "Direct", channel: null,
  property: "Test Hotel", room: "STD-101", room_number: "101", room_type: "STD", room_type_name: "Standard",
  check_in_date: "2031-03-17", check_out_date: "2031-03-19", nights: 2, adults: 2, children: 0,
  guest: { name: "G-1", full_name: "Ada Guest", phone: "+254700000000", email: "ada@example.com", vip: 0,
    blacklisted: 0, stays: 1, last_stay: null },
  // only the second night has a discount, of its own (Daily Details)
  night_rates: [
    { date: "2031-03-17", rate: 1000, base_rate: 1000, discount_amount: 0, discount_type: null,
      discount_value: 0, discount_reason: null, entry_base_rate: 1000, entry_rate: 1000, charged: false },
    { date: "2031-03-18", rate: 900, base_rate: 1000, discount_amount: 100, discount_type: "Amount",
      discount_value: 100, discount_reason: "ADJ-RATE", entry_base_rate: 1000, entry_rate: 900, charged: false },
  ],
  discount: { discount_type: null, discount_value: 0, discount_reason: null, fixed_rate: 0 },
  actions: { can_amend: true }, warnings: {}, occupants: [], checkin_backfill: [],
  amount_after_tax: 1900, advance_paid: 0,
  money: { total: 1900, paid: 0, due: 1900, has_folio: false, source: "booking" },
}

describe("Reservation screen (R5, P2-3)", () => {
  beforeEach(() => {
    rpc.mockReset().mockImplementation((method: string) =>
      Promise.resolve(method === "kamra.api.reservation_detail" ? detail : null))
  })

  it("shows a Daily Details discount, its reason and the night's base", async () => {
    await act(async () => root.render(<MemoryRouter><ReservationDetail row={{ name: "RES-9" }}
      reload={() => {}} onClose={() => {}} /></MemoryRouter>))
    await until(() => expect(text()).toContain("Ada Guest"))
    expect(field("Discount"), "Discount field").not.toBeNull()
    expect(field("Discount")).toContain("ADJ-RATE")
    expect(field("Discount")).toContain("off a night")
    expect(field("Base rate / night"), "Base rate field").not.toBeNull()
    expect(field("Base rate / night")).toContain("1,000")
  })
})
