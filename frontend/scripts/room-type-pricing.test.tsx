// @vitest-environment jsdom
// Room Types the OPERA Cloud / S/4HANA way: the room type is the inventory, the
// rate plan's contract tariff is the price (shown first, at the server's rounded
// local amount), and the card rate (BAR) is a labelled, collapsed section.
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import App from "../src/App"
import { ResourceScreen, type ScreenConfig } from "../src/components/ResourceScreen"
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

const { rpc, update, create, localeState, listState } = vi.hoisted(() => ({
  rpc: vi.fn(),
  update: vi.fn(),
  create: vi.fn(),
  localeState: { value: { locale: "en", currency: "UGX" } as Record<string, unknown> },
  listState: { value: null as Record<string, unknown>[] | null },
}))
const roomType = { name: "STANDARD", room_type_name: "Standard Room", room_type_code: "STD", base_price: 410619,
  base_occupancy: 2, single_occupancy_price: 380000, extra_adult_price: 50000, child_price: 20000,
  adults_capacity: 2, children_capacity: 0 }
vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => method === "kamra.api.property_locale"
    ? Promise.resolve(localeState.value) : rpc(method, params),
  getCurrentProperty: () => "Test Hotel",
  frappeFetch: async (path: string) => ({ data: path.includes("?") ? (listState.value ?? [roomType]) : roomType }),
  serverError: (error: Error) => error.message,
}))
vi.mock("../src/lib/resource", async (original) => ({
  ...await original<typeof import("../src/lib/resource")>(),
  listResource: async (doctype: string) => doctype === "Room Type" ? (listState.value ?? [roomType]) : [],
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
    localeState.value = { locale: "en", currency: "UGX" }
    listState.value = null
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
    expect(host.querySelector("a")?.getAttribute("href")).toBe("/tariffs?q=Standard+Room&room_type=STANDARD")
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

  it("composes #34 Room Types tariff lead and collapsed card rate with #44 Uganda tax_basis", async () => {
    localeState.value = {
      locale: "en-UG",
      currency: "UGX",
      tax_label: "VAT",
      tax_basis: { label: "VAT", rate: 18, inclusive: 1, determined: 1 },
    }
    await render()
    expect(text(dialog())).toContain("Tax is applied by the invoice (VAT included).")
    expect(inputFor("Tax %")).toBeNull()
    expect(text(region())).toContain("UGX 474,000")
    expect(cardSection().open).toBe(false)
  })

  it("composes #34 collapsible sections with #44 readOnlyOnEdit/disabledWhen and #39 TableField", async () => {
    const composed: ScreenConfig = {
      doctype: "Room Type",
      title: "Composed Master",
      columns: [{ field: "room_type_code", label: "Code" }],
      sections: [{ key: "card", label: "Protected pricing rules" }],
      form: [
        { field: "room_type_code", label: "Code", type: "data", readOnlyOnEdit: true },
        { field: "base_price", label: "Locked card rate", type: "currency", section: "card",
          disabledWhen: (d) => String(d.room_type_code) === "STD" },
        { field: "rules", label: "Tax Rules", type: "table", section: "card",
          columns: [{ field: "match_text", label: "Text", type: "data" }] },
      ],
    }
    await act(async () => root.render(<MemoryRouter initialEntries={["/composed?open=STANDARD"]}>
      <ResourceScreen config={composed} />
    </MemoryRouter>))
    await vi.waitFor(async () => {
      await settle()
      expect(host.querySelector("[role=dialog]")).not.toBeNull()
    }, { interval: 1, timeout: 3000 })
    const codeInput = inputFor("Code")
    expect(codeInput?.disabled).toBe(true)
    const section = Array.from(dialog().querySelectorAll("details")).find((n) =>
      n.querySelector("summary")?.textContent?.trim() === "Protected pricing rules")
    expect(section).toBeDefined()
    expect(section!.open).toBe(false)
    expect(inputFor("Locked card rate", section!)?.disabled).toBe(true)
    expect(text(section!)).toContain("Tax Rules")
    expect(Array.from(section!.querySelectorAll("button")).some((b) => b.textContent?.includes("Add row"))).toBe(true)
  })

  // ── Round 7 (Tank Hill live check fixes) ───────────────────────────────────
  const tankHillTypes = [
    { name: "JNR", room_type_name: "Junior Suite", room_type_code: "JNR", room_category: "Private",
      base_price: 410619, base_occupancy: 2, extra_adult_price: 50000, tax_percent: 0 },
    { name: "EXE", room_type_name: "Executive Suite", room_type_code: "EXE", room_category: "Private",
      base_price: 550000, base_occupancy: 2, extra_adult_price: 75000, tax_percent: 0 },
    { name: "STD", room_type_name: "Standard", room_type_code: "STD", room_category: "Private",
      base_price: 350000, base_occupancy: 2, extra_adult_price: 40000, tax_percent: 0 },
  ]
  const tankHillSummary = {
    property_currency: "UGX",
    has_card_rate_plans: false,
    default_plan: { name: "STD-USD", label: "Standard USD", code: "STD-USD", currency: "USD", is_tariff: true },
    room_types: [
      { room_type: "JNR", room_count: 5, priced: true, source: "contract", currency: "USD", amount: 150,
        local_amount: 612743, refusal: null },
      { room_type: "EXE", room_count: 2, priced: false, source: null, currency: null, amount: null,
        local_amount: null, refusal: "HSP-TARIFF-001: Set Room Type.custom_tariff_item." },
      { room_type: "STD", room_count: 0, priced: false, source: null, currency: null, amount: null,
        local_amount: null, refusal: "HSP-TARIFF-001: Set Room Type.custom_tariff_item." },
    ],
  }

  it("shows default-plan selling rates, room counts, badges, and hides card columns when no active plan uses card rate", async () => {
    listState.value = tankHillTypes
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.rates.room_types_summary") return Promise.resolve(tankHillSummary)
      if (method === "kamra.rates.room_type_tariffs") return Promise.resolve(tariffs)
      return Promise.reject(new Error(`Unexpected method: ${method}`))
    })
    await render("/room-types")
    await vi.waitFor(() => expect(text(host)).toContain("USD 150.00"))
    const headers = Array.from(host.querySelectorAll("thead th"), (th) => th.textContent?.trim())
    expect(headers).toContain("Selling rate / night")
    expect(headers).toContain("Rooms")
    expect(headers.some((h) => h?.includes("Card rate"))).toBe(false)
    expect(headers.some((h) => h?.includes("Extra adult"))).toBe(false)
    const rows = Array.from(host.querySelectorAll("tbody tr"))
    const jnr = rows.find((tr) => tr.textContent?.includes("Junior Suite"))!
    expect(text(jnr)).toContain("USD 150.00 · UGX 612,743")
    expect(text(jnr)).toContain("Standard USD · 2 adults")
    expect(text(jnr)).toContain("5")
    expect(text(jnr)).not.toContain("410,619")
    const exe = rows.find((tr) => tr.textContent?.includes("Executive Suite"))!
    expect(text(exe)).toContain("No rate set")
    expect(text(exe)).toContain("2")
    const std = rows.find((tr) => tr.querySelector("td")?.textContent?.trim() === "Standard")!
    expect(text(std)).toContain("No rate set")
    expect(text(std)).toContain("No rooms")
  })

  it("shows card-rate columns when at least one active plan really sells from the card rate", async () => {
    listState.value = tankHillTypes
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.rates.room_types_summary")
        return Promise.resolve({ ...tankHillSummary, has_card_rate_plans: true })
      return Promise.resolve(tariffs)
    })
    await render("/room-types")
    await vi.waitFor(() => expect(text(host)).toContain("USD 150.00"))
    const headers = Array.from(host.querySelectorAll("thead th"), (th) => th.textContent?.trim())
    expect(headers).toContain("Card rate / night (UGX)")
    expect(headers.some((h) => h?.includes("Extra adult"))).toBe(true)
    expect(headers).toContain("Selling rate / night")
    expect(headers).toContain("Rooms")
  })

  it("renders no stray 0 when all_active_plans_tariff is numeric 0 and explains untariffed rooms truthfully", async () => {
    // 1. Numeric 0 must never leak as a literal "0" text node
    tariffs = { ...structuredClone(panel), all_active_plans_tariff: 0 as unknown as boolean }
    await render()
    const directTexts = Array.from(region().childNodes)
      .filter((node) => node.nodeType === Node.TEXT_NODE)
      .map((node) => node.textContent?.trim())
      .filter(Boolean)
    expect(directTexts, "Stray 0 text node leaked into Contract tariffs drawer").not.toContain("0")

    // 2. Untariffed room type on an all-tariff property: never claims card rate, names unpriced plans, says what to do
    await act(async () => root.unmount())
    root = createRoot(host)
    tariffs = {
      tariffs: [],
      all_active_plans_tariff: 1 as unknown as boolean,
      property_currency: "UGX",
      primary_rate_plan: null,
      card_rate_plans: [],
      unpriced_plans: [
        { name: "STD-USD", label: "Standard USD" },
        { name: "HALF-USD", label: "Half Price USD (-50%)" },
      ],
    } as unknown as typeof panel
    await act(async () => root.render(<MemoryRouter initialEntries={["/room-types?open=STANDARD"]}><App /></MemoryRouter>))
    await vi.waitFor(async () => {
      await settle()
      expect(host.querySelector("[role=dialog] section[aria-label='Contract tariffs']")).not.toBeNull()
      expect(text(region())).toContain("No approved contract tariffs.")
    }, { interval: 1, timeout: 3000 })
    expect(text(region())).toContain("The card rate is not used by any active rate plan.")
    expect(text(region())).not.toContain("Priced from the card rate")
    expect(text(region())).toContain("No tariff set for: Standard USD, Half Price USD (-50%)")
    const cta = Array.from(region().querySelectorAll("a")).find((a) =>
      a.textContent?.includes("Set its tariff in Contract Tariffs"))
    expect(cta, "Missing actionable Contract Tariffs link").toBeDefined()
    expect(cta!.getAttribute("href")).toBe("/tariffs")
  })

  it.each([
    ["ar", ar], ["de", de], ["es", es], ["fil", fil], ["id", id], ["pt", pt], ["zh-CN", zh],
  ] as [string, Record<string, string>][])("translates the R7 Room Types list and untariffed drawer strings in %s", async (language, dict) => {
    await ensureDictionary(language)
    setLang(language)
    listState.value = tankHillTypes
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.rates.room_types_summary") return Promise.resolve(tankHillSummary)
      if (method === "kamra.rates.room_type_tariffs") return Promise.resolve({
        tariffs: [], all_active_plans_tariff: true, property_currency: "UGX", primary_rate_plan: null,
        card_rate_plans: [], unpriced_plans: [{ name: "STD-USD", label: "Standard USD" }],
      })
      return Promise.reject(new Error(`Unexpected method: ${method}`))
    })
    await render("/room-types")
    await vi.waitFor(() => expect(text(host)).toContain("USD 150.00"))
    const translated = (key: string, vars: Record<string, string> = {}) => {
      expect(dict[key], `${language}: missing translation for ${JSON.stringify(key)}`).toBeTruthy()
      expect(dict[key], `${language}: ${JSON.stringify(key)} is not translated`).not.toBe(key)
      return fill(dict[key], vars)
    }
    const pageBody = text(host)
    for (const expected of [
      translated("Selling rate / night"),
      translated("{plan} · 2 adults", { plan: "Standard USD" }),
      translated("No rate set"),
      translated("No rooms"),
      translated("Rooms"),
    ]) expect(pageBody).toContain(expected)
    // Open drawer for untariffed room
    const exeRow = Array.from(host.querySelectorAll("tbody tr")).find((tr) => tr.textContent?.includes("Executive Suite"))!
    await act(async () => (exeRow as HTMLElement).click())
    await vi.waitFor(async () => {
      await settle()
      expect(dialog()).not.toBeNull()
      expect(text(dialog())).toContain(translated("Set its tariff in Contract Tariffs"))
    }, { interval: 1, timeout: 3000 })
    expect(text(dialog())).toContain(translated("No tariff set for: {plans}", { plans: "Standard USD" }))
  })

  // ── Round 8 (Hydrogen R8 P1 mobile table min-width + derived drawer rows) ──
  it("gives the Room Types table a minimum width inside overflow-x-auto so 390px phones scroll instead of clipping", async () => {
    listState.value = tankHillTypes
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.rates.room_types_summary") return Promise.resolve(tankHillSummary)
      return Promise.resolve(tariffs)
    })
    await render("/room-types")
    await vi.waitFor(() => expect(text(host)).toContain("USD 150.00"))
    const table = host.querySelector<HTMLTableElement>(".overflow-x-auto table")
    expect(table, "Missing table inside overflow-x-auto").not.toBeNull()
    expect(table!.className).toMatch(/\bmin-w-\[\d+px\]/)
  })

  it("renders calculated derived rate plan rows with base attribution in the Room Type drawer", async () => {
    const derivedRow = {
      rate_plan: "HALF-USD",
      label: "Half Price USD (-50%)",
      currency: "USD",
      room: 75,
      single: 60,
      extra: 30,
      approval_state: "Approved",
      valid_from: "2026-11-01",
      fx_rate: 4084.953333,
      fx_date: "2026-11-24",
      local_room: 306371,
      local_single: 245097,
      fx_lock_policy: "Fixed at Check In",
      rounding_step: 0,
      rounding_mode: "Nearest",
      derived_from: "CONTRACT",
      derived_from_label: "Corporate tariff",
    }
    tariffs = { ...structuredClone(panel), tariffs: [row, derivedRow] } as typeof panel
    await render()
    const body = text(region())
    expect(body).toContain("Half Price USD (-50%)")
    expect(body).toContain("Derived from Corporate tariff")
    expect(body).toContain("USD 75.00")
    expect(body).toContain("UGX 306,371")
    const links = Array.from(region().querySelectorAll("a"), (a) => a.getAttribute("href"))
    expect(links).toContain("/rate-calendar?rate_plan=HALF-USD")
  })
})
