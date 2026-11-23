// @vitest-environment jsdom
// Room Types the OPERA Cloud / S/4HANA way: the room type is the inventory, the
// rate plan's contract tariff is the price (shown first, at the server's rounded
// local amount), and the card rate (BAR) is a labelled, collapsed section.
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import App from "../src/App"
import RoomTypeTariffs from "../src/components/RoomTypeTariffs"
import { setLang } from "../src/lib/dir"
import { ensureDictionary } from "../src/lib/locales"
import ar from "../src/i18n/locales/ar.json"
import de from "../src/i18n/locales/de.json"
import es from "../src/i18n/locales/es.json"
import fil from "../src/i18n/locales/fil.json"
import id from "../src/i18n/locales/id.json"
import pt from "../src/i18n/locales/pt.json"
import zh from "../src/i18n/locales/zh-CN.json"

const { rpc, update, create } = vi.hoisted(() => ({ rpc: vi.fn(), update: vi.fn(), create: vi.fn() }))
const roomType = { name: "STANDARD", room_type_name: "Standard Room", room_type_code: "STD", base_price: 410619,
  base_occupancy: 2, single_occupancy_price: 380000, extra_adult_price: 50000, child_price: 20000,
  adults_capacity: 2, children_capacity: 0 }
vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => method === "kamra.api.property_locale"
    ? Promise.resolve({ locale: "en", currency: "UGX" }) : rpc(method, params),
  getCurrentProperty: () => "Test Hotel",
  frappeFetch: async (path: string) => ({ data: path.includes("?") ? [roomType] : roomType }),
  serverError: (error: Error) => error.message,
}))
vi.mock("../src/lib/resource", async (original) => ({
  ...await original<typeof import("../src/lib/resource")>(),
  listResource: async (doctype: string) => doctype === "Room Type" ? [roomType] : [],
  updateResource: update,
  createResource: create,
}))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: ["Hotel Admin"] }) }))
vi.mock("../src/AppShell", () => ({ default: () => <Outlet /> }))

// hospitality_tariffs.room_type_tariffs, enriched by kamra.rates.room_type_tariffs
const row = { rate_plan: "CONTRACT", label: "Corporate tariff", currency: "USD", room: 130, single: 100, extra: 20,
  approval_state: "Approved", valid_from: "2026-11-01", fx_rate: 3648.2, fx_date: "2026-11-21",
  local_room: 474000, local_single: 365000, fx_lock_policy: "Fixed at Check In", rounding_step: 1000,
  rounding_mode: "Nearest" }
const panel = { tariffs: [row], all_active_plans_tariff: false, property_currency: "UGX",
  primary_rate_plan: "CONTRACT",
  card_rate_plans: [{ name: "BAR", label: "Best available" }, { name: "WEEKEND", label: "Weekend" }] }
const SECTION = "Card rate (BAR) and extra-person prices"
const CARD_FIELDS = ["Card rate / night (UGX)", "Single occupancy price", "Extra adult / night", "Extra child / night"]

describe("Room Types pricing", () => {
  let host: HTMLDivElement
  let root: Root
  let tariffs: typeof panel

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    tariffs = structuredClone(panel)
    update.mockReset().mockResolvedValue({ data: roomType })
    create.mockReset().mockResolvedValue({ data: roomType })
    rpc.mockReset().mockImplementation((method: string) => {
      if (method === "kamra.rates.room_type_tariffs") return Promise.resolve(tariffs)
      return Promise.reject(new Error(`Unexpected method: ${method}`))
    })
    host = document.createElement("div")
    document.body.append(host)
    root = createRoot(host)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    host.remove()
    setLang("en")
    localStorage.clear()
    vi.unstubAllGlobals()
  })

  const text = (scope: ParentNode = host) => scope.textContent ?? ""
  const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 10)))

  async function render(path = "/room-types?open=STANDARD") {
    await act(async () => root.render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>))
    await vi.waitFor(async () => {
      await settle()
      expect(host.textContent).not.toMatch(/^Loading…$/)
      if (path.includes("open=")) expect(host.querySelector("[role=dialog] table")).not.toBeNull()
    }, { interval: 1, timeout: 3000 })
    await settle()
  }
  const dialog = () => host.querySelector<HTMLElement>("[role=dialog]")!
  const region = () => {
    const found = dialog().querySelector<HTMLElement>("section[aria-label='Contract tariffs']")
    expect(found, "Missing Contract tariffs region").not.toBeNull()
    return found!
  }
  function inputFor(label: string, scope: ParentNode = dialog()) {
    const owner = Array.from(scope.querySelectorAll("label")).find((node) =>
      node.querySelector("span")?.textContent?.replace(/\s*\*$/, "").trim() === label)
    return owner?.querySelector<HTMLInputElement>("input") ?? null
  }
  function cardSection() {
    const section = Array.from(dialog().querySelectorAll("details")).find((node) =>
      node.querySelector("summary")?.textContent?.trim() === SECTION)
    expect(section, `Missing collapsed section: ${SECTION}`).toBeDefined()
    return section!
  }
  async function type(node: HTMLInputElement, value: string) {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
    await act(async () => {
      setter.call(node, value)
      node.dispatchEvent(new Event("input", { bubbles: true }))
    })
  }
  async function click(label: string, scope: ParentNode = host) {
    const node = Array.from(scope.querySelectorAll("button")).find((button) => button.textContent?.trim() === label)
    expect(node, `Missing button: ${label}`).toBeDefined()
    await act(async () => node!.click())
  }

  it("shows the server's rounded local amount, never a client-side multiply", async () => {
    await render()
    expect(text(region())).toContain("UGX 474,000")
    expect(text(dialog())).not.toContain("474,266")
  })

  it("summarises the primary tariff with its plan, exchange rate lock and rounding", async () => {
    await render()
    const primary = region().querySelector<HTMLElement>("[data-primary-tariff]")
    expect(primary, "Missing primary tariff summary").not.toBeNull()
    expect(text(primary!)).toContain("Corporate tariff")
    expect(text(primary!)).toContain("USD 130.00")
    expect(text(primary!)).toContain("UGX 474,000")
    expect(text(primary!)).toContain("Exchange rate lock: Fixed at Check In")
    expect(text(primary!)).toContain("Rounded to 1,000, Nearest")
    const headers = Array.from(region().querySelectorAll("thead th"), (node) => node.textContent?.trim())
    expect(headers).toContain("Exchange rate lock")
  })

  it("shows no local equivalent when the server gives none, even with an exchange rate", async () => {
    tariffs.tariffs[0] = { ...row, local_room: null as unknown as number }
    tariffs.primary_rate_plan = null as unknown as string
    await render()
    const headers = Array.from(region().querySelectorAll("thead th"), (node) => node.textContent?.trim())
    const cells = region().querySelector("tbody tr")!.children
    expect(cells[headers.indexOf("Local equiv.")].textContent?.trim()).toBe("—")
    expect(text(dialog())).not.toContain("474,266")
  })

  it("links each tariff to its rate plan's rate calendar after Manage tariffs", async () => {
    await render()
    const links = Array.from(region().querySelectorAll("a"))
    expect(links[0]?.textContent ?? "").toContain("Manage tariffs")
    expect(links[0]?.getAttribute("href")).toBe("/tariffs?q=STANDARD")
    expect(links.map((link) => link.getAttribute("href"))).toContain("/rate-calendar?rate_plan=CONTRACT")
  })

  it("names the active rate plans priced from the card rate", async () => {
    await render()
    expect(text(region())).toContain("Priced from the card rate: Best available, Weekend")
    expect(text(region())).toContain("Stays booked without a rate plan still use the card rate.")
  })

  it("puts the contract tariff before the room type's fields", async () => {
    await render()
    const firstInput = dialog().querySelector("input, select, textarea")
    expect(firstInput).not.toBeNull()
    expect(region().compareDocumentPosition(firstInput!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it("collapses the card rate and extra-person prices into a labelled section", async () => {
    await render()
    const section = cardSection()
    expect(section.open).toBe(false)
    for (const label of CARD_FIELDS) {
      const input = inputFor(label)
      expect(input, `Missing field: ${label}`).not.toBeNull()
      expect(input!.closest("details")).toBe(section)
    }
    expect(text(section)).toContain(
      "Prices stays booked without a rate plan, and rate plans with no contract tariff for this room type.")
    expect(text(dialog())).not.toContain("Used only by rate plans without a contract tariff")
    expect(text(dialog())).toContain(
      "What each unit is and how many it sleeps. Rate plans and contract tariffs set the selling price.")
  })

  it("opens the card rate section for a new room type, whose card rate is required", async () => {
    await render("/room-types")
    await click("New")
    const section = cardSection()
    expect(section.open).toBe(true)
    expect(inputFor("Card rate / night (UGX)", section)).not.toBeNull()
  })

  it("still saves a card rate edited inside the collapsed section", async () => {
    await render()
    const input = inputFor("Card rate / night (UGX)", cardSection())
    expect(input).not.toBeNull()
    await type(input!, "420000")
    await click("Save", dialog())
    await vi.waitFor(() => expect(update).toHaveBeenCalled())
    const [doctype, name, payload] = update.mock.calls[0]
    expect([doctype, name]).toEqual(["Room Type", "STANDARD"])
    expect(payload).toMatchObject({ base_price: 420000, single_occupancy_price: 380000, extra_adult_price: 50000,
      child_price: 20000 })
  })

  it("renders the panel on its own for a room type row", async () => {
    await act(async () => root.render(<MemoryRouter>
      <RoomTypeTariffs row={{ name: "STANDARD", room_type_name: "Standard Room" }} reload={() => {}} />
    </MemoryRouter>))
    await vi.waitFor(async () => {
      await settle()
      expect(host.querySelector("[data-primary-tariff]")).not.toBeNull()
    }, { interval: 1, timeout: 3000 })
    expect(host.querySelector("a")?.getAttribute("href")).toBe("/tariffs?q=STANDARD")
    expect(text()).toContain("UGX 474,000")
  })

  const fill = (template: string, vars: Record<string, string>) =>
    template.replace(/\{(\w+)\}/g, (match, key) => vars[key] ?? match)

  it.each([
    ["ar", ar], ["de", de], ["es", es], ["fil", fil], ["id", id], ["pt", pt], ["zh-CN", zh],
  ] as [string, Record<string, string>][])("translates the pricing section and panel in %s", async (language, dict) => {
    await ensureDictionary(language)
    setLang(language)
    await render()
    const translated = (key: string, vars: Record<string, string> = {}) => {
      expect(dict[key], `${language}: missing translation for ${JSON.stringify(key)}`).toBeTruthy()
      expect(dict[key], `${language}: ${JSON.stringify(key)} is not translated`).not.toBe(key)
      return fill(dict[key], vars)
    }
    const summaries = Array.from(dialog().querySelectorAll("summary"), (node) => node.textContent?.trim())
    expect(summaries).toContain(translated(SECTION))
    const body = text(dialog())
    for (const expected of [
      translated("Prices stays booked without a rate plan, and rate plans with no contract tariff for this room type."),
      translated("What each unit is and how many it sleeps. Rate plans and contract tariffs set the selling price."),
      translated("Card rate / night ({currency})", { currency: "UGX" }),
      translated("Exchange rate lock: {policy}", { policy: dict["Fixed at Check In"] }),
      translated("Rounded to {step}, {mode}", { step: "1,000", mode: dict["Nearest"] }),
      translated("Priced from the card rate: {plans}", { plans: "Best available, Weekend" }),
      translated("Stays booked without a rate plan still use the card rate."),
      translated("Rate calendar"),
      translated("Rate plan"),
    ]) expect(body).toContain(expected)
  })
})
