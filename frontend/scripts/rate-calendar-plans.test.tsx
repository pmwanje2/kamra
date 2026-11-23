// @vitest-environment jsdom
// The Rate Calendar as a rate query (OPERA Cloud Rate Query, SAP pricing analysis):
// by rate plan and customer, each cell the server's quote. With no plan it stays
// the card (BAR) view it was.
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import App from "../src/App"
import { setLang } from "../src/lib/dir"
import { ensureDictionary } from "../src/lib/locales"
import ar from "../src/i18n/locales/ar.json"
import de from "../src/i18n/locales/de.json"
import es from "../src/i18n/locales/es.json"
import fil from "../src/i18n/locales/fil.json"
import id from "../src/i18n/locales/id.json"
import pt from "../src/i18n/locales/pt.json"
import zh from "../src/i18n/locales/zh-CN.json"

const { rpc, list } = vi.hoisted(() => ({ rpc: vi.fn(), list: vi.fn() }))
vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => method === "kamra.api.property_locale"
    ? Promise.resolve({ locale: "en", currency: "UGX", currency_symbol: "UGX " }) : rpc(method, params),
  getCurrentProperty: () => "Test Hotel",
  frappeFetch: async () => ({ data: [] }),
  serverError: (error: Error) => error.message,
}))
vi.mock("../src/lib/resource", async (original) => ({
  ...await original<typeof import("../src/lib/resource")>(),
  listResource: list,
}))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: ["Hotel Admin"] }) }))
vi.mock("../src/AppShell", () => ({ default: () => <Outlet /> }))

const REFUSAL = "HSP-TARIFF-004: No approved tariff for this date."
const contract = { currency: "USD", nightly: 130, fx_rate: 3648.2, fx_date: "2026-11-20", property_currency: "UGX" }
const policy = (name: string, label: string, extra: object = {}) => ({ name, label, code: name, currency: null,
  fx_lock_policy: "Fixed at Reservation", rounding_step: 0, rounding_mode: "Nearest", base_rate_plan: null, ...extra })
const plans = [
  policy("CONTRACT", "Corporate tariff", { currency: "USD", fx_lock_policy: "Fixed at Check In", rounding_step: 1000 }),
  policy("CORP", "Corporate card"),
]
const night = (date: string, rate: number, extra: object = {}) => ({ date, bar: 100000, after_season: rate, rate,
  season: null, demand: null, package: null, occupancy: 40, ...extra })
const base = { from: "2026-12-01", days: 2, tax_inclusive: false, adults: 2, rate_plans: plans,
  default_plan: "CONTRACT", property_currency: "UGX", rate_plan: null, company: null }
const bar = { ...base, room_types: [
  { room_type: "STD", code: "STD", name: "Standard", base_price: 100000,
    nights: [night("2026-12-01", 110000, { season: { label: "Festive", change: "+10%" } }), night("2026-12-02", 90000)] },
] }
const priced = { ...base, rate_plan: plans[0], room_types: [
  { room_type: "STD", code: "STD", name: "Standard", base_price: 100000, nights: [
    { date: "2026-12-01", available: true, rate: 474000, bar: 474000, after_season: 474000, season: null, demand: null,
      package: null, occupancy: 40, contract, source: "contract" },
    { date: "2026-12-02", available: false, refusal: REFUSAL, rate: null, contract: null, occupancy: 10 },
  ] },
  { room_type: "DLX", code: "DLX", name: "Deluxe", base_price: 150000, nights: [
    { ...night("2026-12-01", 135000), bar: 150000, card_rate: 150000, contract: null, source: "card", available: true },
    { ...night("2026-12-02", 135000), bar: 150000, card_rate: 150000, contract: null, source: "card", available: true },
  ] },
] }

describe("Rate Calendar by rate plan and customer", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    rpc.mockReset().mockImplementation((method: string, params: { rate_plan?: string; company?: string }) => {
      if (method === "kamra.rates.rate_calendar") {
        if (!params.rate_plan && !params.company) return Promise.resolve(bar)
        const plan = plans.find((p) => p.name === (params.rate_plan ?? "CONTRACT"))
        return Promise.resolve({ ...priced, rate_plan: plan, company: params.company ?? null })
      }
      return Promise.reject(new Error(`Unexpected method: ${method}`))
    })
    list.mockReset().mockImplementation(async (doctype: string) => {
      if (doctype === "Corporate Account") return [{ name: "ACME", company_name: "Acme Ltd" }]
      return []
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
  const calendarCalls = () => rpc.mock.calls.filter(([method]) => method === "kamra.rates.rate_calendar")
    .map(([, params]) => params as Record<string, unknown>)

  async function render(path = "/rate-calendar") {
    await act(async () => root.render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>))
    await vi.waitFor(async () => {
      await settle()
      expect(host.querySelector("table")).not.toBeNull()
    }, { interval: 1, timeout: 3000 })
    await settle()
  }
  const select = (label: string) => host.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`)
  async function choose(node: HTMLSelectElement, value: string) {
    await act(async () => {
      node.value = value
      node.dispatchEvent(new Event("change", { bubbles: true }))
    })
    await settle()
  }
  const row = (name: string) => Array.from(host.querySelectorAll("tbody tr")).find((tr) =>
    tr.querySelector("td")?.textContent?.trim() === name)!

  it("opens on the card view: no rate plan sent, the BAR column and legend as before", async () => {
    await render()
    const [first] = calendarCalls()
    expect(first).toMatchObject({ property: "Test Hotel", days: 14 })
    expect(first).not.toHaveProperty("rate_plan")
    expect(first).not.toHaveProperty("company")
    const headers = Array.from(host.querySelectorAll("thead th"), (node) => node.textContent?.trim())
    expect(headers).toContain("BAR")
    expect(text()).toContain("Above BAR")
    expect(text()).toContain("Below BAR")
    expect(text(row("Standard"))).toContain("110,000")
  })

  it("lists the card rate and every active rate plan, and queries the chosen plan", async () => {
    await render()
    const plan = select("Rate plan")
    expect(plan, "Missing rate plan selector").not.toBeNull()
    expect(Array.from(plan!.options, (option) => [option.value, option.textContent])).toEqual([
      ["", "Card rate (no rate plan)"], ["CONTRACT", "Corporate tariff"], ["CORP", "Corporate card"]])
    await choose(plan!, "CONTRACT")
    await vi.waitFor(() => expect(calendarCalls().at(-1)).toMatchObject({ rate_plan: "CONTRACT" }))
  })

  it("preselects a deep-linked rate plan and customer and sends both", async () => {
    await render("/rate-calendar?rate_plan=CORP&company=ACME")
    expect(calendarCalls()[0]).toMatchObject({ rate_plan: "CORP", company: "ACME" })
    expect(select("Rate plan")?.value).toBe("CORP")
    expect(select("Customer")?.value).toBe("ACME")
    expect(list).toHaveBeenCalledWith("Corporate Account", expect.objectContaining({
      fields: ["name", "company_name"], filters: [["disabled", "=", 0]] }))
  })

  it("shows a contract cell in its currency over the server's local rate, with no client conversion", async () => {
    await render("/rate-calendar?rate_plan=CONTRACT")
    const cell = row("Standard").querySelectorAll("td")[1]
    expect(text(cell)).toContain("USD 130.00")
    expect(text(cell)).toContain("474,000")
    // 130 x 3,648.2 = 474,266: the calendar never multiplies
    expect(text()).not.toContain("474,266")
    const headers = Array.from(host.querySelectorAll("thead th"), (node) => node.textContent?.trim())
    expect(headers).not.toContain("BAR")
  })

  it("marks a room type priced from the card rate", async () => {
    await render("/rate-calendar?rate_plan=CONTRACT")
    const cell = row("Deluxe").querySelectorAll("td")[1]
    expect(text(cell)).toContain("135,000")
    expect(text(cell)).toContain("Card rate")
    expect(text(row("Standard"))).not.toContain("Card rate")
  })

  it("shows a refused night as a dash named by the refusal", async () => {
    await render("/rate-calendar?rate_plan=CONTRACT")
    const refused = row("Standard").querySelector<HTMLElement>(`[aria-label="Not available: ${REFUSAL}"]`)
    expect(refused, "Missing refused cell").not.toBeNull()
    expect(refused!.textContent?.trim()).toBe("—")
  })

  it("explains the plan's exchange rate lock and rounding in the header", async () => {
    await render("/rate-calendar?rate_plan=CONTRACT")
    expect(text()).toContain("Exchange rate lock: Fixed at Check In")
    expect(text()).toContain("Fixed at Check In: converted again at the check-in date's rate.")
    expect(text()).toContain("Rounded to 1,000, Nearest")
    expect(text()).toContain(
      "USD contract tariff · local price at the published rate, rounded as the rate plan rounds")
  })

  it("offers customers only to a user who may read Corporate Accounts", async () => {
    await render()
    expect(select("Customer"), "Missing customer selector").not.toBeNull()
    expect(Array.from(select("Customer")!.options, (option) => [option.value, option.textContent])).toEqual([
      ["", "Public rate (no customer)"], ["ACME", "Acme Ltd"]])
    await act(async () => root.unmount())
    root = createRoot(host)
    list.mockImplementation(async (doctype: string) => {
      if (doctype === "Corporate Account") throw Object.assign(new Error("Not permitted"), { status: 403 })
      return []
    })
    await render()
    expect(select("Rate plan")).not.toBeNull()
    expect(select("Customer")).toBeNull()
  })

  it("points to Contract Tariffs among the places to change a rate", async () => {
    await render()
    const link = host.querySelector("a[href='/tariffs']")
    expect(link?.textContent ?? "").toContain("Contract Tariffs")
    expect(link?.textContent ?? "").toContain("Price each rate plan per room type and occupancy, in the contract currency.")
  })

  const fill = (template: string, vars: Record<string, string>) =>
    template.replace(/\{(\w+)\}/g, (match, key) => vars[key] ?? match)

  it.each([
    ["ar", ar], ["de", de], ["es", es], ["fil", fil], ["id", id], ["pt", pt], ["zh-CN", zh],
  ] as [string, Record<string, string>][])("translates the rate query in %s", async (language, dict) => {
    await ensureDictionary(language)
    setLang(language)
    await render("/rate-calendar?rate_plan=CONTRACT")
    const translated = (key: string, vars: Record<string, string> = {}) => {
      expect(dict[key], `${language}: missing translation for ${JSON.stringify(key)}`).toBeTruthy()
      expect(dict[key], `${language}: ${JSON.stringify(key)} is not translated`).not.toBe(key)
      return fill(dict[key], vars)
    }
    const options = Array.from(host.querySelectorAll("select option"), (option) => option.textContent)
    expect(options).toEqual(expect.arrayContaining([translated("Card rate (no rate plan)"),
      translated("Public rate (no customer)")]))
    const body = text()
    for (const expected of [
      translated("Card rate"),
      translated("Exchange rate lock: {policy}", { policy: dict["Fixed at Check In"] }),
      translated("Fixed at Check In: converted again at the check-in date's rate."),
      translated("Rounded to {step}, {mode}", { step: "1,000", mode: dict["Nearest"] }),
      translated("{currency} contract tariff · local price at the published rate, rounded as the rate plan rounds",
        { currency: "USD" }),
      translated("Price each rate plan per room type and occupancy, in the contract currency."),
      translated("Rate calendar"),
    ]) expect(body).toContain(expected)
    expect(host.querySelector(`[aria-label="${fill(translated("Not available: {reason}"), { reason: REFUSAL })}"]`))
      .not.toBeNull()
    expect(host.querySelector(`select[aria-label="${translated("Rate plan")}"]`)).not.toBeNull()
  })
})
