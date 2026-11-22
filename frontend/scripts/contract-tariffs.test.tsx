// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet } from "react-router-dom"
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest"
import App from "../src/App"
import RoomTypeExtras from "../src/components/RoomTypeTariffs"
import { appForPath } from "../src/lib/apps"
import { FEATURES } from "../src/lib/searchIndex"

const { rpc } = vi.hoisted(() => ({ rpc: vi.fn() }))
vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => method === "kamra.api.property_locale"
    ? Promise.resolve({ locale: "en", currency: "UGX" }) : rpc(method, params),
  getCurrentProperty: () => "Test Hotel",
  frappeFetch: async (path: string) => ({ data: path.includes("?") ? [] : {} }),
  serverError: (error: Error) => error.message,
}))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: ["Hotel Admin"] }) }))
vi.mock("../src/AppShell", () => ({ default: () => <Outlet /> }))

const listMethod = "kamra.rates.list_contract_tariffs"
const active = { name: "CURRENT", rate: 130, local_equiv: 526268, valid_from: "2026-11-22", workflow_state: "Approved" }
const draft = { name: "DRAFT-USD", rate: 140, valid_from: "2026-12-01", workflow_state: "Draft", can_approve: true }
const usd = { name: "Corporate USD", currency: "USD", fx_rate: 4048.221567891, fx_date: "2026-11-22", rate_plans: ["CORP"] }
const local = { name: "Local rates", currency: "UGX", rate_plans: [] }
const item = { item_code: "DLX", item_name: "Deluxe King", category: "Room", room_type: "Deluxe",
  occupancy: "Double", price_list: usd.name, currency: usd.currency, active, pending: [draft],
  history: [{ name: "OLD", rate: 115, valid_from: "2026-10-01", valid_upto: "2026-11-21", workflow_state: "Approved" }] }
const data = { supported: true, property: "Test Hotel", property_currency: "UGX", business_date: "2026-11-22",
  can_propose: true, can_approve: true, price_lists: [usd, local], items: [
    item,
    { ...item, item_code: "BED", item_name: "Extra bed", category: "Supplement", room_type: null, active: null, pending: [], history: [] },
    { ...item, item_code: "HALL", item_name: "Garden Hall", category: "Venue", price_list: local.name, currency: "UGX",
      active: { ...active, rate: 850000, local_equiv: null }, pending: [], history: [] },
  ] }

describe("Contract Tariffs", () => {
  let host: HTMLDivElement
  let root: Root

  beforeAll(() => {
    // jsdom has the dialog element but not its native top-layer methods.
    Object.defineProperties(HTMLDialogElement.prototype, {
      showModal: { configurable: true, value(this: HTMLDialogElement) { this.open = true } },
      close: { configurable: true, value(this: HTMLDialogElement) { this.open = false } },
    })
  })

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    rpc.mockReset().mockImplementation((method: string) => {
      if (method === listMethod) return Promise.resolve(data)
      if (method === "kamra.rates.room_type_tariffs") return Promise.resolve(null)
      throw new Error(`Unexpected method: ${method}`)
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

  async function render(path = "/tariffs") {
    await act(async () => root.render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>))
    // Wait for the real lazy route (or the app's normal not-found fallback).
    await vi.waitFor(async () => {
      await act(() => new Promise((resolve) => setTimeout(resolve, 10)))
      expect(host.textContent).not.toMatch(/^Loading…$/)
    }, { interval: 1 })
  }
  const text = () => host.textContent ?? ""
  function button(label: string, scope: ParentNode = host) {
    const result = Array.from(scope.querySelectorAll("button")).find((node) => node.textContent?.trim() === label)
    expect(result, `Missing button: ${label}`).toBeDefined()
    return result!
  }
  async function click(label: string, scope?: ParentNode) {
    await act(async () => button(label, scope).click())
  }
  async function input(node: HTMLInputElement, value: string) {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
    await act(async () => {
      setter.call(node, value)
      node.dispatchEvent(new Event("input", { bubbles: true }))
    })
  }

  it("groups tariffs, formats the book rate and local equivalent, and filters multiple lists", async () => {
    await render()
    expect(rpc).toHaveBeenCalledWith(listMethod, { property: "Test Hotel" })
    expect(text()).toContain("Test Hotel")
    expect(text()).toContain("Deluxe King")
    expect(text()).toContain("Extra bed")
    expect(text()).toContain("1 USD = 4,048.22 UGX")
    expect(text()).toContain("USD 130.00")
    expect(text()).toContain("UGX 526,268")
    expect(text()).toContain("Pending: USD 140.00")
    expect(text()).not.toMatch(/4048\.221567891|ERPNext|Frappe|Kamra/i)
    expect(host.querySelectorAll("tbody th[scope=rowgroup]")).toHaveLength(2)
    await click("History", host.querySelector("tr[data-item='DLX']") ?? host)
    expect(text()).toContain("USD 115.00")
    await click("Supplements")
    expect(text()).not.toContain("Deluxe King")
    const select = host.querySelector("select")!
    await act(async () => {
      select.value = local.name
      select.dispatchEvent(new Event("change", { bubbles: true }))
    })
    await click("Venues / Conference")
    expect(text()).toContain("Garden Hall")
    expect(text()).toContain("UGX 850,000")
    expect(text()).not.toContain("4,048.22")
  })

  it("opens, previews and submits a new version with the selected item and currency", async () => {
    rpc.mockImplementationOnce(async () => data)
      .mockImplementationOnce(async () => ({ item_price: "NEW" }))
      .mockImplementationOnce(async () => data)
    await render("/tariffs?q=Deluxe")
    expect(text()).not.toContain("Extra bed")
    await click("New version")
    const dialog = host.querySelector("dialog[open]")!
    expect(dialog.textContent).toContain("Corporate USD")
    expect(dialog.textContent).toContain("USD 130.00")
    expect(dialog.querySelector<HTMLInputElement>("input[name=valid_from]")?.value).toBe("2026-11-23")
    await input(dialog.querySelector("input[type=number]")!, "140")
    expect(dialog.textContent).toContain("USD 140.00 ≈ UGX 566,751")
    expect(dialog.textContent).not.toContain("4048.221567891")
    await input(dialog.querySelector("input[name=valid_upto]")!, "2026-12-31")
    await click("Submit for approval", dialog)
    expect(rpc.mock.calls).toEqual([
      [listMethod, { property: "Test Hotel" }],
      ["kamra.rates.propose_contract_tariff", { property: "Test Hotel", price_list: usd.name,
        item_code: "DLX", price_list_rate: 140, valid_from: "2026-11-23", valid_upto: "2026-12-31" }],
      [listMethod, { property: "Test Hotel" }],
    ])
    expect(text()).toContain("Tariff version submitted for approval")
    expect(host.querySelector("dialog[open]")).toBeNull()
  })

  it.each(["Approve", "Discard"])("confirms %s before mutating and refreshes the pending queue", async (action) => {
    rpc.mockImplementationOnce(async () => data)
      .mockImplementationOnce(async () => ({ item_price: draft.name }))
      .mockImplementationOnce(async () => ({ ...data, items: [{ ...item, pending: [] }] }))
    await render()
    await click("1 pending approval")
    expect(text()).not.toContain("Extra bed")
    await click(action)
    expect(rpc).toHaveBeenCalledTimes(1)
    const dialog = host.querySelector("dialog[open]")!
    expect(dialog.textContent).toContain("USD 140.00")
    await click(action, dialog)
    expect(rpc.mock.calls.slice(1)).toEqual([
      [`kamra.rates.${action.toLowerCase()}_contract_tariff`, { item_price: draft.name }],
      [listMethod, { property: "Test Hotel" }],
    ])
    expect(text()).toContain("No tariff versions are pending approval.")
  })

  it("honors both property and row authorization without hiding the draft", async () => {
    rpc.mockResolvedValueOnce({ ...data, can_propose: false,
      items: [{ ...item, pending: [{ ...draft, can_approve: false }] }] })
    await render()
    expect(text()).toContain("Pending: USD 140.00")
    expect(text()).not.toMatch(/New version|Discard/)
    expect(Array.from(host.querySelectorAll("button"), (node) => node.textContent?.trim())).not.toContain("Approve")
  })

  it("keeps the proposal open and does not report success when the write bridge is absent", async () => {
    rpc.mockResolvedValueOnce(data).mockResolvedValueOnce(null)
    await render("/tariffs?q=Deluxe")
    await click("New version")
    const dialog = host.querySelector("dialog[open]")!
    await input(dialog.querySelector("input[type=number]")!, "140")
    await click("Submit for approval", dialog)
    expect(dialog.querySelector("[role=alert]")?.textContent).toContain("Tariff editing is not available")
    expect(text()).not.toContain("Tariff version submitted for approval")
    expect(rpc).toHaveBeenCalledTimes(2)
  })

  it("shows a neutral unavailable state when the property has no integration", async () => {
    rpc.mockResolvedValueOnce({ supported: false, property: "Test Hotel", price_lists: [], items: [],
      can_approve: false, can_propose: false })
    await render()
    expect(text()).toContain("Contract tariffs are not available for this property.")
    expect(text()).not.toMatch(/New version|Approve|Discard|ERPNext|Frappe|Kamra/)
  })

  it("is discoverable in Revenue and through staff search", () => {
    expect(appForPath("/tariffs").id).toBe("revenue")
    const search = FEATURES.find((entry) => entry.to === "/tariffs")
    expect(search?.keywords).toEqual(expect.arrayContaining(["Change USD room tariff", "Edit USD rates"]))
  })

  it("offers a filtered editor link from room type details even without published tariffs", async () => {
    await act(async () => root.render(<MemoryRouter>
      <RoomTypeExtras row={{ name: "DLX Test", room_type_name: "Deluxe King" }} reload={() => {}} />
    </MemoryRouter>))
    const link = host.querySelector("a")
    expect(link?.textContent ?? "").toContain("Manage tariffs")
    expect(link?.getAttribute("href")).toBe("/tariffs?q=DLX+Test")
  })

  it("offers the editor link from Rate Plans", async () => {
    await render("/rate-plans")
    const link = host.querySelector("a[href='/tariffs']")
    expect(link?.textContent ?? "").toContain("Manage tariffs")
  })
})
