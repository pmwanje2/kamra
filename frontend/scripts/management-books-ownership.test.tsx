// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import ManagementBooks from "../src/screens/ManagementBooks"
import CashForecast from "../src/screens/CashForecast"
import { setLang } from "../src/lib/dir"
import { ensureDictionary } from "../src/lib/locales"
import languages from "../src/i18n/languages.json"

const rpc = vi.hoisted(() => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return vi.fn()
})
vi.mock("../src/lib/api", async (original) => ({
  ...await original<typeof import("../src/lib/api")>(),
  call: (method: string, args: unknown) => method === "kamra.api.property_locale" ? Promise.resolve({}) : rpc(method, args),
  getCurrentProperty: () => "Test property",
}))

const balance = {
  assets: [{ key: "Visible account", amount: 70 }], assets_total: 70,
  liabilities: [], liabilities_total: 0, net: 70, tax_collected_month: 0, tax_on_bills_month: 0,
}
const pnl = {
  erp_owned: ["pos"], revenue: [{ key: "Visible account", amount: null }], revenue_total: null,
  supplier_bills: [], petty_cash: [], paid_outs: { amount: 0, count: 0 },
  expense_total: 0, net: null, tax_collected: 0, tax_on_bills: 0,
}
let host: HTMLDivElement
let root: Root
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  rpc.mockReset()
  host = document.createElement("div")
  document.body.appendChild(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  setLang("en")
  vi.unstubAllGlobals()
})
async function render(view: "balance" | "pnl") {
  await act(async () => root.render(<MemoryRouter><ManagementBooks view={view} /></MemoryRouter>))
}
async function changeDate(value: string) {
  const field = host.querySelector<HTMLInputElement>("input[type=date]")!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, value)
    field.dispatchEvent(new Event("change", { bubbles: true }))
  })
}
function refused(message: string) {
  return { network: true, message }
}

for (const [view, report] of [["balance", balance], ["pnl", pnl]] as const) {
  it(`${view} clears the displayed report while the new date loads`, async () => {
    rpc.mockResolvedValueOnce(report).mockReturnValueOnce(new Promise(() => {}))
    await render(view)
    expect(host.textContent).toContain("Visible account")
    await changeDate("2020-01-01")
    expect(rpc).toHaveBeenCalledTimes(2)
    expect(host.textContent).not.toContain("Visible account")
  })

  it(`${view} keeps the newest refusal when an older success arrives`, async () => {
    const pending = Promise.withResolvers<unknown>()
    rpc.mockReturnValueOnce(pending.promise).mockRejectedValueOnce(refused("Historical balance unavailable"))
    await render(view)
    await changeDate("2020-01-01")
    expect(host.textContent).toContain("Historical balance unavailable")
    await act(async () => pending.resolve(report))
    expect(host.textContent).not.toContain("Visible account")
    expect(host.textContent).toContain("Historical balance unavailable")
  })

  it(`${view} keeps the newest report when an older refusal arrives`, async () => {
    const pending = Promise.withResolvers<unknown>()
    rpc.mockReturnValueOnce(pending.promise).mockResolvedValueOnce(report)
    await render(view)
    await changeDate("2020-01-01")
    expect(host.textContent).toContain("Visible account")
    await act(async () => pending.reject(refused("Superseded refusal")))
    expect(host.textContent).toContain("Visible account")
    expect(host.textContent).not.toContain("Superseded refusal")
  })
}

const posNote = "Manage this in the point of sale."
it.each(languages.filter(({ code }) => code !== "en"))("renders the delegated report in $code", async ({ code }) => {
  setLang(code)
  const dictionary = await ensureDictionary(code)
  rpc.mockResolvedValueOnce(pnl)
  await render("pnl")
  expect(host.textContent).toContain("Visible account")
  for (const english of [posNote, "Unavailable"]) {
    expect.soft(host.textContent).not.toContain(english)
    expect.soft(dictionary[english]).toBeTruthy()
    if (dictionary[english]) expect(host.textContent).toContain(dictionary[english])
  }
})

const forecast = {
  opening: 123, opening_source: "entered", money_in: 20, money_out: 10, closing: 133,
  days: [], weeks: [], overdue_in: 0, overdue_out: 0, draft_out: 0,
}
const delegatedForecast = { erp_owned: ["receivables"], closing: null }
const ownershipNotice = "This is kept in the accounting system."

async function forecastAction(label: string) {
  const button = Array.from(host.querySelectorAll("button")).find((element) => element.textContent === label)
  expect(button).toBeDefined()
  await act(async () => button!.click())
}

it("keeps the 15-day refusal when a held 5-day forecast arrives", async () => {
  const earlier = Promise.withResolvers<unknown>()
  rpc.mockReturnValueOnce(earlier.promise).mockResolvedValueOnce(delegatedForecast)
  await act(async () => root.render(<CashForecast />))
  await forecastAction("15 days")
  expect(rpc.mock.calls.map(([, args]) => args.days)).toEqual([5, 15])
  expect(host.textContent).toContain(ownershipNotice)
  await act(async () => earlier.resolve(forecast))
  expect(host.textContent).toContain(ownershipNotice)
  expect(host.textContent).not.toContain("Starting position")
})

it.each(["15 days", "Update"])("clears the prior forecast while %s loads", async (trigger) => {
  const pending = Promise.withResolvers<unknown>()
  rpc.mockResolvedValueOnce(forecast).mockReturnValueOnce(pending.promise)
  await act(async () => root.render(<CashForecast />))
  expect(host.textContent).toContain("Starting position")
  await forecastAction(trigger)
  expect(host.textContent).not.toContain("Starting position")
  expect(host.textContent).toContain("Loading…")
  await act(async () => pending.resolve(delegatedForecast))
  expect(host.textContent).toContain(ownershipNotice)
})

it("keeps the newest manual refresh when an older one fails", async () => {
  const earlier = Promise.withResolvers<unknown>()
  rpc.mockResolvedValueOnce(delegatedForecast).mockReturnValueOnce(earlier.promise).mockResolvedValueOnce(forecast)
  await act(async () => root.render(<CashForecast />))
  await forecastAction("Update")
  expect.soft(host.textContent).not.toContain(ownershipNotice)
  await forecastAction("Update")
  expect(rpc.mock.calls.map(([, args]) => args.days)).toEqual([5, 5, 5])
  expect(host.textContent).toContain("Starting position")
  await act(async () => earlier.reject(refused("Old refresh failed")))
  expect(host.textContent).toContain("Starting position")
  expect(host.textContent).not.toContain("Old refresh failed")
})

it("applies the draft opening amount only on a span change or Update", async () => {
  rpc.mockResolvedValue(forecast)
  await act(async () => root.render(<CashForecast />))
  const input = host.querySelector<HTMLInputElement>("input")!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "320")
    input.dispatchEvent(new Event("input", { bubbles: true }))
  })
  expect(rpc).toHaveBeenCalledTimes(1)
  await forecastAction("15 days")
  await forecastAction("Update")
  expect(rpc.mock.calls.map(([, args]) => [args.days, args.opening])).toEqual([[5, undefined], [15, 320], [15, 320]])
})

it("discards a pending forecast refusal after unmount", async () => {
  const pending = Promise.withResolvers<unknown>()
  const readMessage = vi.fn(() => "Obsolete error")
  rpc.mockReturnValueOnce(pending.promise)
  await act(async () => root.render(<CashForecast />))
  await act(async () => root.render(null))
  await act(async () => pending.reject({ network: true, get message() { return readMessage() } }))
  expect(readMessage).not.toHaveBeenCalled()
})
