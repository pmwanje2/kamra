// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { BookingDialog } from "../src/components/BookingDialog"
import ContractTariffs from "../src/screens/ContractTariffs"
import SettingsHub from "../src/screens/settings/SettingsHub"

const { rpc } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return { rpc: vi.fn() }
})
vi.mock("../src/lib/api", async (original) => ({
  ...await original<typeof import("../src/lib/api")>(),
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => "Test Hotel",
  frappeFetch: async () => ({ data: [] }),
}))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: ["Revenue Manager"] }) }))

const villa = { name: "Test Hotel-VILLA", room_type_name: "Garden Villa", room_type_code: "VILLA",
  base_occupancy: 6, adults_capacity: 7, room_count: 1, price_lists: ["Contract USD"] }
const standard = { name: "Test Hotel-STD", room_type_name: "Standard", room_type_code: "STD",
  room_count: 2, price_lists: ["Contract USD"] }
const baseItem = { price_list: "Contract USD", active: { name: "IP", rate: 100, valid_from: "2026-11-25",
  workflow_state: "Approved" }, pending: [], history: [] }
const directory = { supported: true, can_propose: true, property: "Test Hotel", property_currency: "UGX",
  business_date: "2026-11-25", price_lists: [{ name: "Contract USD", currency: "USD", fx_rate: 3650 }],
  items: [
    { ...baseItem, category: "Room", item_code: "DLX", room_type: "Deluxe", occupancy: "Double",
      item_name: "Deluxe package with Conference Room credit", room_type_id: "Test Hotel-DLX", room_type_code: "DLX" },
    { ...baseItem, category: "Venue", item_code: "COURTYARD", item_name: "Conference Room Hire - Courtyard",
      room_type: "Conference Room", room_type_id: "Test Hotel-CONFERENCE", room_type_code: "CONFERENCE" },
  ],
  room_types: [standard, villa], unpriced_room_types: [standard, villa],
}
const ratePlan = { name: "PROMO", rate_plan_name: "Promotion 20% off", label: "Promotion 20% off", code: "PROMO" }
const optionalPlans = {
  room_types: [{ ...standard, base_price: 410619, adults_capacity: 2, children_capacity: 0 }],
  rate_plans: [ratePlan], default_rate_plan: null, default_plan: null, rate_plan_required: false,
  all_active_plans_tariff: false, meal_plans: [], companies: [], travel_agents: [], experiences: [],
  property: { country: "Uganda", checkin_time: "14:00", checkout_time: "11:00" }, ota_channels: [],
}
const revenue = { id: "revenue", title: "Revenue", description: "", group: "modules", toggle: true,
  enabled: true, can_edit: true, editors: ["Revenue Manager"], related: [], sections: [], values: {} }
const frontDesk = { ...revenue, id: "front-desk", title: "Front Desk", editors: ["Hotel Admin"],
  sections: [{ id: "pricing", title: "Pricing", description: "", settings: [
    { key: "rate_plan_required", label: "Require a rate plan on every booking", type: "check", default: 0 },
  ] }], values: { rate_plan_required: 0 } }

let host: HTMLDivElement
let root: Root
let settingsModules: object[]

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: { configurable: true, value(this: HTMLDialogElement) { this.open = true } },
    close: { configurable: true, value(this: HTMLDialogElement) { this.open = false } },
  })
  settingsModules = [revenue]
  rpc.mockReset().mockImplementation(async (method: string) => {
    if (method === "kamra.api.booking_options") return optionalPlans
    if (method === "kamra.rates.list_contract_tariffs") return directory
    if (method === "kamra.settings.settings_schema") return {
      modules: settingsModules, is_admin: settingsModules.includes(frontDesk), enabled_modules: ["revenue"],
    }
    return []
  })
  host = document.createElement("div")
  document.body.append(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
})

const button = (text: string, scope: ParentNode = host) =>
  [...scope.querySelectorAll("button")].find((node) => node.textContent?.trim() === text)

async function tariffs(path: string) {
  await act(async () => root.render(<MemoryRouter initialEntries={[path]}><ContractTariffs /></MemoryRouter>))
}

it.each([false, true])("starts on Standard rate for walk-in=%s when optional plans have no designated default", async (walkIn) => {
  await act(async () => root.render(<MemoryRouter><BookingDialog
    initial={{ room_type: standard.name, check_in_date: "2026-11-25", nights: 1, walkIn }}
    onClose={() => {}} onBooked={() => {}} />
  </MemoryRouter>))
  const picker = [...host.querySelectorAll("select")].find((node) => node.textContent?.includes("Promotion 20% off"))
  expect(picker?.selectedOptions[0]?.textContent).toContain("Standard rate (no plan)")
  expect(picker?.value).toBe("")
})

it("gives the linked venue identity priority over an unrelated room-item description", async () => {
  await tariffs("/tariffs?q=Conference+Room&room_type=Test+Hotel-CONFERENCE&room_type_code=CONFERENCE")
  expect(button("Venues (1)")?.getAttribute("aria-pressed")).toBe("true")
  expect(host.querySelector("[data-item='COURTYARD']")).not.toBeNull()
})

it("counts a matching unpriced room setup row in the Room matrix tab", async () => {
  await tariffs("/tariffs?q=Standard")
  expect(button("Set up tariff")).toBeDefined()
  expect(button("Room matrix (1)")?.getAttribute("aria-pressed")).toBe("true")
})

it("selects the villa base occupancy and offers tiers through its full capacity", async () => {
  await tariffs("/tariffs?q=Garden+Villa")
  await act(async () => button("Set up tariff")!.click())
  const picker = host.querySelector<HTMLSelectElement>("dialog select[name=occupancy_tier]")
  expect(picker?.value).toBe("6")
  expect([...picker!.options].map((option) => option.value)).toEqual(["1", "2", "3", "4", "5", "6", "7", "bed"])
})

it.each([false, true])("renders the pricing shortcut only with access to that setting (access=%s)", async (hasAccess) => {
  if (hasAccess) settingsModules.push(frontDesk)
  await act(async () => root.render(<MemoryRouter initialEntries={["/settings/revenue"]}><Routes>
    <Route path="/settings/:section" element={<SettingsHub />} />
  </Routes></MemoryRouter>))
  expect(host.querySelector("h2")?.textContent).toContain("Revenue")
  const link = host.querySelector('a[href="/settings/front-desk?focus=rate_plan_required"]')
  expect(Boolean(link)).toBe(hasAccess)
})
