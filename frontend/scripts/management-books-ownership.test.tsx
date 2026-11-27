// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import ManagementBooks from "../src/screens/ManagementBooks"
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
