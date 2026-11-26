// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { BookingDialog } from "../src/components/BookingDialog"
import GroupBookingDialog from "../src/components/GroupBookingDialog"
import { RateOptions } from "../src/components/RateOptions"
import ContractTariffs from "../src/screens/ContractTariffs"

const { rpc } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return { rpc: vi.fn() }
})
vi.mock("../src/lib/api", async (original) => ({
  ...await original<typeof import("../src/lib/api")>(),
  call: (method: string, params: unknown) => rpc(method, params),
  getQuote: (params: unknown) => rpc("kamra.api.get_quote", params),
  getCurrentProperty: () => "Tankhill Inn By SEB",
  frappeFetch: async () => ({ data: [] }),
}))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: ["Hotel Admin"] }) }))

const room = { name: "STD", room_type_name: "Standard", base_price: 410619, adults_capacity: 2, children_capacity: 0 }
const plans = [{ name: "CORP", label: "Corporate", rate_plan_name: "Corporate", code: "CORP",
  is_negotiated: true, is_default: true, rate_category: "House Use", room_total: 365000, vs_plain: 0, available: true }]
const version = { name: "IP-1", rate: 100, valid_from: "2026-11-25", workflow_state: "Approved" }
const data = { supported: true, can_propose: true, property: "Tankhill Inn By SEB", property_currency: "UGX",
  business_date: "2026-11-25", price_lists: [{ name: "Contract USD", currency: "USD", fx_rate: 3650 }],
  items: [{
    category: "Venue", item_code: "COURTYARD", item_name: "Conference Room Hire - Courtyard", room_type: "Conference Room",
    room_type_id: "Tankhill Inn By SEB-CONFERENCE", room_type_code: "CONFERENCE", price_list: "Contract USD",
    active: version, pending: [], history: [],
  }, {
    category: "Room", item_code: "DLX", item_name: "Deluxe", room_type: "Deluxe", room_type_id: "DLX-ROOM",
    occupancy: "Double", price_list: "Contract USD", active: version, pending: [], history: [],
  }],
  unpriced_room_types: [{ name: "STD", room_type_name: "Standard", room_type_code: "STD", room_count: 2,
    price_lists: ["Contract USD"] }],
}

let host: HTMLDivElement
let root: Root
let required: boolean
let governed: boolean

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: { configurable: true, value(this: HTMLDialogElement) { this.open = true } },
    close: { configurable: true, value(this: HTMLDialogElement) { this.open = false } },
  })
  required = false
  governed = true
  rpc.mockReset().mockImplementation(async (method: string) => {
    if (method === "kamra.rates.list_contract_tariffs") return data
    if (method === "kamra.api.property_locale") return { locale: "en", currency: "UGX" }
    if (method === "kamra.api.booking_options") return {
      default_plan: "CORP", rate_plans: plans, room_types: [room], meal_plans: [], companies: [], travel_agents: [], experiences: [],
      property: { country: "Uganda", checkin_time: "14:00", checkout_time: "11:00" }, ota_channels: [],
      rate_plan_required: required, all_active_plans_tariff: governed,
    }
    if (method === "kamra.rates.rate_options") return {
      plans, nights: [], voucher: null, company_plan: null, default_rate_plan: null,
      plain: { room_total: 410619, amount_after_tax: 410619, discount: 0 },
      rate_plan_required: required, all_active_plans_tariff: governed,
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

const button = (label: string) => [...host.querySelectorAll("button")].find((node) => node.textContent?.trim() === label)

it("opens the venue tab from a room type ID and reports all live match counts", async () => {
  await act(async () => root.render(<MemoryRouter initialEntries={["/tariffs?q=Tankhill%20Inn%20By%20SEB-CONFERENCE"]}>
    <ContractTariffs />
  </MemoryRouter>))
  expect(button("Venues (1)")?.getAttribute("aria-pressed")).toBe("true")
  for (const label of ["Room matrix (0)", "Supplements (0)", "Other (0)", "Pending approval (0)", "New version"]) {
    expect(button(label)).toBeDefined()
  }
  expect(host.textContent).toContain("Conference Room Hire - Courtyard")
  expect(host.textContent).toContain("USD 100.00")
})

it.each(["Add tariff", "Set rate", "Set up tariff"])("opens the proposal from %s", async (label) => {
  await act(async () => root.render(<MemoryRouter><ContractTariffs /></MemoryRouter>))
  expect(button(label)).toBeDefined()
  await act(async () => button(label)!.click())
  const dialog = host.querySelector("dialog[open]")
  expect(dialog?.textContent).toContain("Submit for approval")
  expect(dialog?.querySelector<HTMLInputElement>('[name="valid_from"]')?.value).toBe("2026-11-25")
  if (label === "Set up tariff") expect(dialog?.querySelector<HTMLSelectElement>('[name="room_type"]')?.value).toBe("STD")
})

it.each([false, true])("hides the plain rate in rate query with required=%s", async (policy) => {
  required = policy
  governed = !policy
  await act(async () => {
    root.render(<RateOptions roomType="STD" checkIn="2026-11-25" checkOut="2026-11-26" adults={2} children={0}
      ratePlan="" onPickPlan={() => {}} nightPrices={{}} onNightPrices={() => {}} canSetPrice={false} />)
  })
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 350)) })
  for (const label of ["Corporate", "Negotiated", "Default", "House Use"]) expect(host.textContent).toContain(label)
  expect(host.textContent).not.toContain("Standard rate")
})

it.each([[false, false], [true, false], [false, true], [true, true]])(
  "selects the available plan for walk-in=%s and required=%s", async (walkIn, policy) => {
    required = policy
    governed = !policy
    await act(async () => root.render(<MemoryRouter>
      <BookingDialog initial={{ room_type: "STD", check_in_date: "2026-11-25", nights: 1, walkIn }}
        onClose={() => {}} onBooked={() => {}} />
    </MemoryRouter>))
    const picker = [...host.querySelectorAll("select")].find((node) => node.textContent?.includes("Corporate (CORP)"))
    expect(picker?.value).toBe("CORP")
    expect(picker?.textContent).not.toContain("Standard rate")
  },
)

it("quotes the governed default selected in the group wizard", async () => {
  await act(async () => root.render(<MemoryRouter><GroupBookingDialog onClose={() => {}} onDone={() => {}} /></MemoryRouter>))
  const picker = [...host.querySelectorAll("select")].find(node => node.value === "CORP")
  expect(picker).toBeDefined()
  expect(picker?.textContent).not.toContain("Standard rate")
  await vi.waitFor(() => expect(rpc).toHaveBeenCalledWith("kamra.api.group_price_preview", expect.objectContaining({ rate_plan: "CORP" })))
})
