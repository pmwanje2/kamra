// @vitest-environment jsdom
// The Rate Calendar as a rate query (OPERA Cloud Rate Query, SAP pricing analysis):
// by rate plan and customer, each cell the server's quote. With no plan it stays
// the card (BAR) view it was.
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet, useLocation } from "react-router-dom"
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
  policy("CONTRACT", "Corporate tariff", { currency: "USD", is_default: true, rate_category: "House Use", fx_lock_policy: "Fixed at Check In", rounding_step: 1000 }),
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

type CalendarParams = { rate_plan?: string; company?: string }
// kamra.rates.rate_calendar: a customer with no rate plan resolves to the
// customer's negotiated plan (ACME's is CONTRACT); nothing at all is the card.
function calendarServer(method: string, params: CalendarParams) {
  if (method === "kamra.rates.rate_calendar") {
    if (!params.rate_plan && !params.company) return Promise.resolve(bar)
    const plan = plans.find((p) => p.name === (params.rate_plan || "CONTRACT"))
    return Promise.resolve({ ...priced, rate_plan: plan, company: params.company ?? null, company_plan: params.company ? "CONTRACT" : null })
  }
  return Promise.reject(new Error(`Unexpected method: ${method}`))
}
const refuse = (message: string, status = 417) => Promise.reject(Object.assign(new Error(message), { status }))
// the screen's query string, as the router holds it
let search = ""
function Url() {
  search = useLocation().search
  return null
}
const query = () => new URLSearchParams(search)
const languages = [
  ["ar", ar], ["de", de], ["es", es], ["fil", fil], ["id", id], ["pt", pt], ["zh-CN", zh],
] as [string, Record<string, string>][]

describe("Rate Calendar by rate plan and customer", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    rpc.mockReset().mockImplementation(calendarServer)
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
    await act(async () => root.render(<MemoryRouter initialEntries={[path]}><App /><Url /></MemoryRouter>))
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
  const policyHeader = () => host.querySelector("[data-rate-plan-policy]")
  const button = (label: string) => Array.from(host.querySelectorAll("button"))
    .find((node) => node.textContent?.trim() === label) ?? null
  /** Render a link the server refuses: no grid ever arrives, so wait for the error. */
  async function renderRefused(path: string, message: string) {
    await act(async () => root.render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>))
    await vi.waitFor(async () => {
      await settle()
      expect(text()).toContain(message)
    }, { interval: 1, timeout: 3000 })
    await settle()
  }

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
      ["", "Card rate (no rate plan)"], ["CONTRACT", "Corporate tariff · Default · House Use · USD"], ["CORP", "Corporate card"]])
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

  // OPERA Rate Query: the screen names the rate code it prices, and the user's own choice wins.
  it("names the customer's negotiated rate plan it prices, in the selector and the header", async () => {
    await render("/rate-calendar?company=ACME")
    expect(calendarCalls()[0]).toMatchObject({ company: "ACME" })
    expect(calendarCalls()[0]).not.toHaveProperty("rate_plan")
    const plan = select("Rate plan")!
    expect(plan.value, "The selector claims a plan the grid is not priced at").toBe("CONTRACT")
    for (const label of ["Corporate tariff", "Negotiated", "Default", "House Use", "USD"]) expect(plan.selectedOptions[0]?.textContent).toContain(label)
    expect(policyHeader(), "Missing rate plan header").not.toBeNull()
    expect(text(policyHeader()!)).toContain("Rate plan: Corporate tariff")
    expect(text(policyHeader()!)).toContain("CONTRACT")
    expect(text(policyHeader()!)).toContain("Negotiated rate plan for Acme Ltd")
  })

  it("names an explicitly chosen rate plan without calling it the customer's negotiated one", async () => {
    await render("/rate-calendar?rate_plan=CORP&company=ACME")
    expect(text(policyHeader()!)).toContain("Rate plan: Corporate card")
    expect(text(policyHeader()!)).not.toContain("Negotiated rate plan")
  })

  it("prices the card rate when the user picks it, not the customer's negotiated plan", async () => {
    await render("/rate-calendar?rate_plan=CORP&company=ACME")
    await choose(select("Rate plan")!, "")
    await vi.waitFor(() => expect(Array.from(host.querySelectorAll("thead th"),
      (node) => node.textContent?.trim())).toContain("BAR"))
    expect(select("Rate plan")!.value).toBe("")
    expect(select("Customer")!.value, "The user's customer choice is kept").toBe("ACME")
    expect(policyHeader()).toBeNull()
    expect(text(row("Standard"))).toContain("110,000")
    expect(text()).not.toContain("474,000")
  })

  // SAP Fiori smart filter bar: Customer is the controlling field and Rate plan depends on it (IN/OUT
  // parameter). A Rate plan choice holds only for the Customer it was made with; a new Customer fills it again.
  describe("Customer controls Rate plan", () => {
    const customersOf = (...rows: [string, string][]) => list.mockImplementation(async (doctype: string) =>
      doctype === "Corporate Account" ? rows.map(([name, company_name]) => ({ name, company_name })) : [])
    // BETA negotiated CORP; ACME (as everywhere here) CONTRACT
    const withBeta = () => {
      customersOf(["ACME", "Acme Ltd"], ["BETA", "Beta plc"])
      rpc.mockImplementation((method: string, params: CalendarParams) =>
        method === "kamra.rates.rate_calendar" && params.company === "BETA" && !params.rate_plan
          ? Promise.resolve({ ...priced, rate_plan: plans[1], company: "BETA" }) : calendarServer(method, params))
    }
    const headers = () => Array.from(host.querySelectorAll("thead th"), (node) => node.textContent?.trim())

    it.each([
      ["the card rate", "/rate-calendar?company=ACME", ""],
      ["a named rate plan", "/rate-calendar?company=ACME", "CORP"],
    ])("clearing the customer drops %s chosen for it: the card rate, with no customer", async (_, path, chosen) => {
      await render(path)
      await choose(select("Rate plan")!, chosen)
      expect(query().get("rate_plan")).toBe(chosen)
      await choose(select("Customer")!, "")
      await vi.waitFor(() => expect(headers()).toContain("BAR"))
      expect(query().has("rate_plan"), "?rate_plan= is left stranded").toBe(false)
      expect(query().has("company")).toBe(false)
      expect(calendarCalls().at(-1)).not.toHaveProperty("rate_plan")
      expect(calendarCalls().at(-1)).not.toHaveProperty("company")
      expect(select("Rate plan")!.value).toBe("")
      expect(text(row("Standard"))).toContain("110,000")
    })

    it.each([
      ["the card rate", ""],
      ["a named rate plan", "CONTRACT"],
    ])("switching customer drops %s chosen for the last one: the new customer's negotiated plan", async (_, chosen) => {
      withBeta()
      await render("/rate-calendar?company=ACME")
      await choose(select("Rate plan")!, chosen)
      await choose(select("Customer")!, "BETA")
      await vi.waitFor(() => expect(policyHeader()).not.toBeNull())
      expect(calendarCalls().at(-1), "The new customer is not asked for").toMatchObject({ company: "BETA" })
      expect(calendarCalls().at(-1)).not.toHaveProperty("rate_plan")
      expect(query().get("company")).toBe("BETA")
      expect(query().has("rate_plan")).toBe(false)
      expect(select("Rate plan")!.value).toBe("CORP")
      expect(text(policyHeader()!)).toContain("Rate plan: Corporate card")
      expect(text(policyHeader()!)).toContain("Negotiated rate plan for Beta plc")
    })

    it("re-selecting the customer after clearing it prices its negotiated plan again", async () => {
      await render("/rate-calendar?company=ACME")
      await choose(select("Rate plan")!, "")
      await choose(select("Customer")!, "")
      await choose(select("Customer")!, "ACME")
      await vi.waitFor(() => expect(policyHeader()).not.toBeNull())
      expect(calendarCalls().at(-1), "The customer is dropped from the query").toMatchObject({ company: "ACME" })
      expect(calendarCalls().at(-1)).not.toHaveProperty("rate_plan")
      expect(Object.fromEntries(query())).toEqual({ company: "ACME" })
      expect(select("Rate plan")!.value).toBe("CONTRACT")
      expect(text(policyHeader()!)).toContain("Negotiated rate plan for Acme Ltd")
      expect(text(row("Standard"))).toContain("474,000")
    })

    // as a linked rate plan that is not active: named as asked, never shown as the public rate
    it("names a linked customer that is not an active Corporate Account, flagged as not available", async () => {
      await render("/rate-calendar?company=GONE")
      expect(calendarCalls()[0], "The linked customer is still the one priced").toMatchObject({ company: "GONE" })
      const customer = select("Customer")!
      expect(customer.value, "The selector shows the public rate for a customer's prices").toBe("GONE")
      expect(customer.selectedOptions[0]?.textContent).toBe("Not available: GONE")
      expect(Array.from(customer.options, (option) => [option.value, option.textContent])).toEqual([
        ["", "Public rate (no customer)"], ["ACME", "Acme Ltd"], ["GONE", "Not available: GONE"]])
    })
  })

  it("never lets a slower, superseded query overwrite the grid", async () => {
    await render()
    let release: (value: unknown) => void = () => undefined
    rpc.mockImplementation((method: string, params: CalendarParams) => {
      if (method === "kamra.rates.rate_calendar" && params.rate_plan === "CONTRACT")
        return new Promise((resolve) => { release = resolve })
      return calendarServer(method, params)
    })
    await choose(select("Rate plan")!, "CONTRACT")
    await choose(select("Rate plan")!, "")
    await vi.waitFor(() => expect(text(row("Standard"))).toContain("110,000"))
    await act(async () => release({ ...priced, rate_plan: plans[0] }))
    await settle()
    expect(select("Rate plan")!.value).toBe("")
    expect(text(row("Standard"))).toContain("110,000")
    expect(text()).not.toContain("474,000")
    expect(policyHeader()).toBeNull()
  })

  it("shows every amount in the property currency's minor unit, and a home-currency contract once", async () => {
    const usd = { ...base, property_currency: "USD", rate_plans: plans, rate_plan: null }
    const fx = { fx_date: "2026-11-20", property_currency: "USD" }
    rpc.mockImplementation((method: string, params: CalendarParams) => {
      if (method !== "kamra.rates.rate_calendar") return calendarServer(method, params)
      if (!params.rate_plan) return Promise.resolve({ ...usd, room_types: [{ room_type: "STD", code: "STD",
        name: "Standard", base_price: 89.99,
        nights: [night("2026-12-01", 89.99, { bar: 89.99 }), night("2026-12-02", 99.5, { bar: 89.99 })] }] })
      return Promise.resolve({ ...usd, rate_plan: { ...plans[0], currency: null }, room_types: [
        { room_type: "STD", code: "STD", name: "Standard", base_price: 89.99, nights: [
          { date: "2026-12-01", available: true, rate: 130.5, bar: 130.5, after_season: 130.5, season: null,
            demand: null, package: null, occupancy: 40, source: "contract",
            contract: { currency: "USD", nightly: 130.5, fx_rate: 1, ...fx } },
          { date: "2026-12-02", available: true, rate: 108.45, bar: 108.45, after_season: 108.45, season: null,
            demand: null, package: null, occupancy: 40, source: "contract",
            contract: { currency: "EUR", nightly: 100, fx_rate: 1.0845, ...fx } },
        ] },
      ] })
    })
    await render()
    const card = row("Standard").querySelectorAll("td")
    expect(text(card[1]), "BAR column").toContain("89.99")
    expect(text(card[2])).toContain("89.99")
    expect(text(card[3])).toContain("99.50")
    await choose(select("Rate plan")!, "CONTRACT")
    await vi.waitFor(() => expect(text(row("Standard"))).toContain("EUR 100.00"))
    const [, home, foreign] = row("Standard").querySelectorAll("td")
    expect(text(home).match(/130\.50/g), "A home-currency contract shows its figure once").toHaveLength(1)
    expect(text(home)).not.toContain("131")
    expect(text(foreign)).toContain("EUR 100.00")
    expect(text(foreign)).toContain("108.45")
  })

  it("clears the grid when a query fails, keeps the rate plans to recover with, and resets", async () => {
    await render()
    rpc.mockImplementation((method: string, params: CalendarParams) =>
      method === "kamra.rates.rate_calendar" && params.company ? refuse("Not permitted", 403) : calendarServer(method, params))
    await choose(select("Customer")!, "ACME")
    await vi.waitFor(() => expect(text()).toContain("Not permitted"))
    expect(host.querySelector("table"), "Stale prices stay under the error").toBeNull()
    expect(Array.from(select("Rate plan")!.options, (option) => option.value)).toEqual(["", "CONTRACT", "CORP"])
    const reset = button("Clear rate plan and customer")
    expect(reset, "Missing reset").not.toBeNull()
    await act(async () => reset!.click())
    await vi.waitFor(() => expect(host.querySelector("table")).not.toBeNull())
    expect(calendarCalls().at(-1)).not.toHaveProperty("company")
    expect(calendarCalls().at(-1)).not.toHaveProperty("rate_plan")
    expect(select("Customer")!.value).toBe("")
    expect(text()).not.toContain("Not permitted")
  })

  it("recovers from a deep link to a plan and customer the server refuses", async () => {
    const REFUSED = "OLD is not an active rate plan of this property."
    list.mockImplementation(async (doctype: string) => {
      if (doctype === "Corporate Account") throw Object.assign(new Error("Not permitted"), { status: 403 })
      return []
    })
    rpc.mockImplementation((method: string, params: CalendarParams) =>
      method === "kamra.rates.rate_calendar" && (params.rate_plan === "OLD" || params.company)
        ? refuse(REFUSED) : calendarServer(method, params))
    await renderRefused("/rate-calendar?rate_plan=OLD&company=ACME", REFUSED)
    expect(host.querySelector("table")).toBeNull()
    expect(select("Rate plan")!.value, "The selector names the plan asked for, not the card rate").toBe("OLD")
    expect(select("Customer")).toBeNull()
    const reset = button("Clear rate plan and customer")
    expect(reset, "No way to clear ?company= without the customer list").not.toBeNull()
    await act(async () => reset!.click())
    await vi.waitFor(() => expect(host.querySelector("table")).not.toBeNull())
    expect(calendarCalls().at(-1)).not.toHaveProperty("company")
    expect(calendarCalls().at(-1)).not.toHaveProperty("rate_plan")
    expect(Array.from(select("Rate plan")!.options, (option) => option.value)).toEqual(["", "CONTRACT", "CORP"])
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

  const translator = (language: string, dict: Record<string, string>) =>
    (key: string, vars: Record<string, string> = {}) => {
      expect(dict[key], `${language}: missing translation for ${JSON.stringify(key)}`).toBeTruthy()
      expect(dict[key], `${language}: ${JSON.stringify(key)} is not translated`).not.toBe(key)
      return fill(dict[key], vars)
    }

  it.each(languages)("translates the rate query in %s", async (language, dict) => {
    await ensureDictionary(language)
    setLang(language)
    await render("/rate-calendar?rate_plan=CONTRACT")
    const translated = translator(language, dict)
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
      translated("Rate plan: {plan}", { plan: "Corporate tariff" }),
    ]) expect(body).toContain(expected)
    expect(host.querySelector(`[aria-label="${fill(translated("Not available: {reason}"), { reason: REFUSAL })}"]`))
      .not.toBeNull()
    expect(host.querySelector(`select[aria-label="${translated("Rate plan")}"]`)).not.toBeNull()
  })

  it.each(languages)("translates the negotiated rate plan and the reset in %s", async (language, dict) => {
    await ensureDictionary(language)
    setLang(language)
    await render("/rate-calendar?company=ACME")
    const translated = translator(language, dict)
    expect(text(policyHeader()!)).toContain(translated("Negotiated rate plan for {customer}", { customer: "Acme Ltd" }))
    rpc.mockImplementation((method: string, params: CalendarParams) =>
      method === "kamra.rates.rate_calendar" && params.rate_plan === "CORP" ? refuse("Refused") : calendarServer(method, params))
    await choose(select(translated("Rate plan"))!, "CORP")
    await vi.waitFor(() => expect(button(translated("Clear rate plan and customer"))).not.toBeNull())
  })

  // ── Round 7 (Tank Hill live check fixes) ───────────────────────────────────
  it("opens on the server's default tariff rate plan and allows switching explicitly to Card rate", async () => {
    const tariffPlans = [
      policy("CONTRACT", "Corporate tariff", { currency: "USD", fx_lock_policy: "Fixed at Check In",
        rounding_step: 1000, is_tariff: true }),
      policy("CORP", "Corporate card", { is_tariff: false }),
    ]
    rpc.mockImplementation((method: string, params: CalendarParams) => {
      if (method !== "kamra.rates.rate_calendar") return Promise.reject(new Error(`Unexpected: ${method}`))
      // Server opens on CONTRACT when rate_plan is omitted; explicit rate_plan="" returns Card rate
      if (params.rate_plan === "") return Promise.resolve({ ...bar, rate_plans: tariffPlans, rate_plan: null })
      const plan = tariffPlans.find((p) => p.name === (params.rate_plan || "CONTRACT"))!
      return Promise.resolve({ ...priced, rate_plans: tariffPlans, rate_plan: plan, company: params.company ?? null, company_plan: params.company ? "CONTRACT" : null })
    })
    await render("/rate-calendar")
    expect(calendarCalls()[0]).not.toHaveProperty("rate_plan")
    expect(select("Rate plan")!.value).toBe("CONTRACT")
    expect(policyHeader()).not.toBeNull()
    expect(text(policyHeader()!)).toContain("Rate plan: Corporate tariff")
    expect(text(policyHeader()!)).not.toContain("Negotiated rate plan")
    const headers = Array.from(host.querySelectorAll("thead th"), (node) => node.textContent?.trim())
    expect(headers).not.toContain("BAR")
    expect(text(row("Standard"))).toContain("USD 130.00")

    // User explicitly chooses Card rate -> sends rate_plan: "" and renders BAR view
    await choose(select("Rate plan")!, "")
    await vi.waitFor(() => expect(Array.from(host.querySelectorAll("thead th"),
      (node) => node.textContent?.trim())).toContain("BAR"))
    expect(calendarCalls().at(-1)).toMatchObject({ rate_plan: "" })
    expect(select("Rate plan")!.value).toBe("")
    expect(policyHeader()).toBeNull()
  })

  it("shows room types with zero rooms as dashes with a No rooms badge, never as sellable rates", async () => {
    const withEmpty = {
      ...priced,
      room_types: [
        { ...priced.room_types[0], room_count: 5, sellable: true },
        { room_type: "PRS", code: "PRS", name: "Presidential Suite", base_price: 900000, room_count: 0, sellable: false,
          nights: [
            { date: "2026-12-01", available: true, rate: 1021000, bar: 1021000, after_season: 1021000, season: null,
              demand: null, package: null, occupancy: 0, contract: { ...contract, nightly: 280 }, source: "contract" },
            { date: "2026-12-02", available: true, rate: 1021000, bar: 1021000, after_season: 1021000, season: null,
              demand: null, package: null, occupancy: 0, contract: { ...contract, nightly: 280 }, source: "contract" },
          ] },
      ],
    }
    rpc.mockImplementation((method: string) =>
      method === "kamra.rates.rate_calendar" ? Promise.resolve(withEmpty) : Promise.reject(new Error(method)))
    await render("/rate-calendar?rate_plan=CONTRACT")
    const prs = Array.from(host.querySelectorAll("tbody tr")).find((tr) =>
      tr.textContent?.includes("Presidential Suite"))!
    expect(text(prs)).toContain("No rooms")
    expect(text(prs)).not.toContain("USD 280.00")
    expect(text(prs)).not.toContain("1,021,000")
    const noRoomBtns = prs.querySelectorAll("button[aria-label='No rooms']")
    expect(noRoomBtns).toHaveLength(2)
    expect(noRoomBtns[0].textContent?.trim()).toBe("—")
    await act(async () => (noRoomBtns[0] as HTMLButtonElement).click())
    await settle()
    expect(text()).toContain("No rooms configured for this room type.")
  })

  it.each(languages)("translates No rooms on the rate calendar in %s", async (language, dict) => {
    await ensureDictionary(language)
    setLang(language)
    const withEmpty = {
      ...bar,
      room_types: [
        { ...bar.room_types[0], room_count: 0, sellable: false },
      ],
    }
    rpc.mockImplementation((method: string) =>
      method === "kamra.rates.rate_calendar" ? Promise.resolve(withEmpty) : Promise.reject(new Error(method)))
    await render("/rate-calendar")
    const translated = translator(language, dict)
    expect(text()).toContain(translated("No rooms"))
    const btn = host.querySelector<HTMLButtonElement>(`button[aria-label="${translated("No rooms")}"]`)
    expect(btn).not.toBeNull()
    await act(async () => btn!.click())
    await settle()
    expect(text()).toContain(translated("No rooms configured for this room type."))
  })
})
