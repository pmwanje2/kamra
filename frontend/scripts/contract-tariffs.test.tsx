// @vitest-environment jsdom
import { execFileSync } from "node:child_process"
import { resolve } from "node:path"
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet } from "react-router-dom"
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest"
import App from "../src/App"
import RoomTypeExtras from "../src/components/RoomTypeTariffs"
import { appForPath } from "../src/lib/apps"
import { setLang } from "../src/lib/dir"
import { ensureDictionary } from "../src/lib/locales"
import { FEATURES } from "../src/lib/searchIndex"

const { rpc, download, resource } = vi.hoisted(() => ({ rpc: vi.fn(), download: vi.fn(), resource: vi.fn() }))
vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => method === "kamra.api.property_locale"
    ? Promise.resolve({ locale: "en", currency: "UGX" }) : rpc(method, params),
  getCurrentProperty: () => "Test Hotel",
  frappeFetch: (path: string) => resource(path),
  serverError: (error: Error) => error.message,
}))
vi.mock("../src/lib/csv", async (original) => ({ ...await original<typeof import("../src/lib/csv")>(), downloadCsv: download }))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: ["Hotel Admin"] }) }))
vi.mock("../src/AppShell", () => ({ default: () => <Outlet /> }))

const listMethod = "kamra.rates.list_contract_tariffs"
const active = { name: "CURRENT", rate: 130, local_equiv: 526268, valid_from: "2026-11-22", workflow_state: "Approved" }
const draft = { name: "DRAFT-USD", rate: 140, valid_from: "2026-12-01", workflow_state: "Draft", can_approve: true }
const usd = { name: "Corporate USD", currency: "USD", fx_rate: 4048.221567891, fx_date: "2026-11-22", rate_plans: ["Test Hotel-CORP"] }
const local = { name: "Local rates", currency: "UGX", rate_plans: [] }
const deluxe = { category: "Room", room_type: "Deluxe", price_list: usd.name, currency: usd.currency, customer: null }
const item = { ...deluxe, item_code: "DLX", item_name: "Deluxe King", occupancy: "Double", active, pending: [draft],
  history: [{ name: "OLD", rate: 115, valid_from: "2026-10-01", valid_upto: "2026-11-21", workflow_state: "Approved" }] }
const single = { ...deluxe, item_code: "DLX-1A", item_name: "Deluxe King single", occupancy: "Single",
  active: { name: "SGL", rate: 110, local_equiv: 445304, valid_from: "2026-11-22", workflow_state: "Approved" },
  pending: [], history: [] }
const triple = { ...deluxe, item_code: "DLX-3A", item_name: "Deluxe King triple", occupancy: "3 Adults", active: null,
  pending: [], history: [{ name: "NEXT", rate: 150, valid_from: "2027-01-01", workflow_state: "Approved" }] }
const corporate = { ...item, customer: "Client Co", active: { ...active, name: "CORP-DLX", rate: 120, local_equiv: 485787 },
  pending: [], history: [] }
const extraAdult = { item_code: "TAR-EXTRA-ADULT", item_name: "Extra adult", category: "Supplement", room_type: null,
  occupancy: null, price_list: usd.name, currency: "USD", customer: null,
  active: { name: "EA", rate: 20, local_equiv: 80964, valid_from: "2026-11-01", workflow_state: "Approved" },
  pending: [], history: [] }
const bed = { ...extraAdult, item_code: "BED", item_name: "Extra bed", active: null }
const hall = { item_code: "HALL", item_name: "Garden Hall", category: "Venue", room_type: null, occupancy: null,
  price_list: local.name, currency: "UGX", customer: null,
  active: { ...active, name: "HALL-1", rate: 850000, local_equiv: null }, pending: [], history: [] }
const policies = {
  "Test Hotel-CORP": { label: "Corporate", code: "CORP", currency: "USD", fx_lock_policy: "Fixed at Check In",
    base_rate_plan: "Test Hotel-BAR", adjust_extra_person: false, rounding_step: 1000, rounding_mode: "Nearest" },
  "Test Hotel-BAR": { label: "Best available", code: "BAR", currency: null, fx_lock_policy: "Fixed at Reservation",
    base_rate_plan: null, adjust_extra_person: false, rounding_step: 0, rounding_mode: "Nearest" },
}
const data = { supported: true, property: "Test Hotel", property_currency: "UGX", business_date: "2026-11-22",
  can_propose: true, can_approve: true, can_discard: true, price_lists: [usd, local], rate_plan_policies: policies,
  items: [item, single, triple, corporate, extraAdult, bed, hall] }

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
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    download.mockReset()
    resource.mockReset().mockImplementation(async (path: string) => ({ data: path.includes("?") ? [] : {} }))
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
    localStorage.clear()
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
  const text = (scope: ParentNode = host) => scope.textContent ?? ""
  function button(label: string, scope: ParentNode = host) {
    const result = Array.from(scope.querySelectorAll("button")).find((node) => node.textContent?.trim() === label ||
      (node.hasAttribute("aria-pressed") && node.textContent?.trim().replace(/ \(\d+\)$/, "") === label))
    expect(result, `Missing button: ${label}`).toBeDefined()
    return result!
  }
  const labels = (scope: ParentNode = host) => Array.from(scope.querySelectorAll("button"), (node) => node.textContent?.trim())
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
  async function choose(node: HTMLSelectElement, value: string) {
    await act(async () => {
      node.value = value
      node.dispatchEvent(new Event("change", { bubbles: true }))
    })
  }
  async function check(label: string) {
    const box = host.querySelector<HTMLInputElement>(`input[type=checkbox][aria-label="${label}"]`)
    expect(box, `Missing checkbox: ${label}`).not.toBeNull()
    await act(async () => box!.click())
  }
  const row = (key: string) => host.querySelector<HTMLElement>(`tr[data-room="${key}"]`)!
  const field = <T extends Element>(scope: ParentNode, name: string) => scope.querySelector<T>(`[name=${name}]`)!
  async function chooseFile(dialog: ParentNode, csv: string) {
    const file = field<HTMLInputElement>(dialog, "tariff_file")
    await act(async () => {
      Object.defineProperty(file, "files", { configurable: true, value: [new File([csv], "tariffs.csv", { type: "text/csv" })] })
      file.dispatchEvent(new Event("change", { bubbles: true }))
    })
  }
  async function openBulk() {
    await check("Select Deluxe · Corporate USD")
    await click("Create new version in bulk")
    return host.querySelector("dialog[open]")!
  }

  it("R4 preserves an unpriced item's room floor from the listing endpoint to the drawer", async () => {
    const room = "Hotel-JUNIOR-SUITE"
    const bridge = { ...data, items: [], available_items: [
      { item_code: "DLX", item_name: "Deluxe King", category: "Room", room_type_id: room },
    ], guardrails: { "": Array(7).fill([50, 1000000]), [room]: Array(7).fill([600000, 1000000]) } }
    // Exercise the endpoint's serialization as well as the drawer's item selection.
    const endpoint = [
      "import json, sys",
      "from kamra.tests.test_contract_tariffs_api import ContractTariffHarness",
      "site = ContractTariffHarness()",
      "site.setUp()",
      "site.script = json.load(sys.stdin)",
      "print(json.dumps(site.endpoint('list_contract_tariffs')('Test Hotel')))",
    ].join("\n")
    const payload = execFileSync("python3", ["-c", endpoint], { cwd: resolve(".."), input: JSON.stringify(bridge), encoding: "utf8" })
    rpc.mockResolvedValue(JSON.parse(payload))
    await render()
    await click("Add tariff")
    const dialog = host.querySelector("dialog[open]")!
    await choose(field(dialog, "item_code"), "DLX")
    await input(field(dialog, "price_list_rate"), "100")
    expect(dialog.querySelector("p.text-warning-ink")?.textContent).toContain("600,000")
  })

  it("R3 applies the selected existing item's room-specific Saturday floor", async () => {
    rpc.mockResolvedValue({ ...data, items: [{ ...item, room_type_id: "Hotel-JUNIOR-SUITE" }],
      available_items: [{ item_code: "DLX", item_name: "Deluxe King", category: "Room" }],
      guardrails: { "": Array(7).fill([50, 1000000]), "Hotel-JUNIOR-SUITE": Array.from({length: 7}, (_, d) => [d === 6 ? 600000 : 50, 1000000]) } })
    await render()
    await click("Add tariff")
    const dialog = host.querySelector("dialog[open]")!
    await choose(field(dialog, "item_code"), "DLX")
    await input(field(dialog, "price_list_rate"), "100")
    expect(dialog.querySelector("p.text-warning-ink")?.textContent).toContain("600,000")
  })

  it("R2 opens all pending drafts directly from a dashboard link", async () => {
    await render("/tariffs?tab=pending")
    expect(button("Pending approval (1)").getAttribute("aria-pressed")).toBe("true")
    expect(host.querySelector("tr[data-item='DLX']")).toBeTruthy()
  })

  it("R2 retains the corporate customer when proposing a public supplement override", async () => {
    rpc.mockImplementationOnce(async () => data).mockImplementationOnce(async () => ({ item_price: "NEW" }))
      .mockImplementationOnce(async () => data)
    await render()
    await click("Extra adult USD 20.00", row("Corporate USD|Deluxe|Client Co"))
    const dialog = host.querySelector("dialog[open]")!
    await input(dialog.querySelector("input[type=number]")!, "22")
    await input(field(dialog, "valid_from"), "2026-11-23")
    await click("Submit for approval", dialog)
    expect(rpc).toHaveBeenCalledWith("kamra.rates.propose_contract_tariff", expect.objectContaining({
      customer: "Client Co", item_code: "TAR-EXTRA-ADULT", price_list_rate: 22,
    }))
  })

  it.each(["2026-11-29", ""])("R2 checks Saturday's floor for proposals ending %s", async (until) => {
    rpc.mockResolvedValue({ ...data, guardrails: { "": Array.from({ length: 7 }, (_, day) =>
      day === 6 ? [600000, 800000] : [100000, 900000]) } })
    await render()
    await click("New version", row("Corporate USD|Deluxe|").querySelector("[data-item='DLX']")!)
    const dialog = host.querySelector("dialog[open]")!
    await input(dialog.querySelector("input[type=number]")!, "130")
    await input(field(dialog, "valid_from"), "2026-11-23")
    await input(field(dialog, "valid_upto"), until)
    expect(dialog.querySelector("[aria-live].text-warning-ink")?.textContent).toContain("600,000")
  })

  it("links the filtered corporate row to finance and revenue records", async () => {
    await render("/tariffs?price_list=Corporate%20USD&customer=Client%20Co")
    expect(host.querySelectorAll("tr[data-room]")).toHaveLength(1)
    for (const path of ["/cashier/fx", "/rate-plans?open=", "/companies?q=", "/room-types?open=", "/rate-calendar?rate_plan="]) {
      expect(host.querySelector(`a[href^="${path}"]`), path).toBeTruthy()
    }
    const corporateRow = row("Corporate USD|Deluxe|Client Co")
    expect(text(corporateRow.lastElementChild!)).toContain("History")
    expect(text(corporateRow.lastElementChild!)).toContain("Corporate")
    expect(Array.from(corporateRow.querySelectorAll("button")).some(b => b.textContent?.includes("Extra adult"))).toBe(true)
  })

  it("shows one pricing row per room type with occupancy columns, extras and rounded local amounts", async () => {
    await render()
    expect(rpc).toHaveBeenCalledWith(listMethod, { property: "Test Hotel" })
    const headers = Array.from(host.querySelectorAll("thead th"), (node) => node.textContent?.trim())
    expect(headers).toEqual(expect.arrayContaining(["Room type", "1 Adult", "2 Adults", "3 Adults", "Extras"]))
    const publicRow = row("Corporate USD|Deluxe|")
    expect(publicRow).not.toBeNull()
    expect(host.querySelectorAll("tr[data-room]")).toHaveLength(2)
    expect(text(publicRow)).toContain("USD 110.00")
    expect(text(publicRow)).toContain("≈ UGX 445,000")
    expect(text(publicRow)).toContain("USD 130.00")
    expect(text(publicRow)).toContain("≈ UGX 526,000")
    expect(text(publicRow)).toContain("Scheduled: USD 150.00 from Jan 1, 2027")
    expect(text(publicRow)).toContain("In review: USD 140.00 from Dec 1, 2026")
    expect(text(publicRow)).toContain("Extra adult USD 20.00")
    expect(text(publicRow)).toContain("Approved")
    expect(text(row("Corporate USD|Deluxe|Client Co"))).toContain("USD 120.00")
    expect(text()).toContain("Governed posting FX: 1 USD = 4,048.22 UGX · Nov 22, 2026 · rounded to nearest 1,000")
    expect(text()).toContain("Corporate · USD · Fixed at Check In · Derived from Best available")
    expect(text()).not.toMatch(/4048\.221567891|526,268|ERPNext|Frappe|Kamra/i)
    await click("History", publicRow)
    expect(text()).toContain("USD 115.00")
  })

  it("filters by category tab, price list, customer and search", async () => {
    await render()
    expect(labels()).toEqual(expect.arrayContaining(["Room matrix (2)", "Supplements (2)", "Venues (0)", "Other (0)", "Pending approval (1)"]))
    await click("Supplements")
    expect(text()).toContain("Extra adult")
    expect(text()).toContain("Extra bed")
    expect(text()).not.toContain("Deluxe King")
    await click("Room matrix")
    const customer = field<HTMLSelectElement>(host, "customer")
    expect(Array.from(customer.options, (option) => option.textContent)).toEqual(["All customers", "Public (standard)", "Client Co"])
    await choose(customer, "__public__")
    expect(row("Corporate USD|Deluxe|Client Co")).toBeNull()
    expect(row("Corporate USD|Deluxe|")).not.toBeNull()
    await choose(customer, "Client Co")
    expect(row("Corporate USD|Deluxe|")).toBeNull()
    expect(row("Corporate USD|Deluxe|Client Co")).not.toBeNull()
    await choose(customer, "")
    await choose(field<HTMLSelectElement>(host, "price_list"), local.name)
    await click("Venues")
    expect(text()).toContain("Garden Hall")
    expect(text()).toContain("UGX 850,000")
    expect(text()).not.toContain("4,048.22")
    await click("Room matrix")
    expect(text()).toContain("No tariffs match these filters.")
  })

  it("reads the directory again for another effective date", async () => {
    await render()
    await input(field<HTMLInputElement>(host, "as_of_date"), "2026-12-15")
    await vi.waitFor(() => expect(rpc).toHaveBeenLastCalledWith(listMethod, { property: "Test Hotel", as_of_date: "2026-12-15" }))
  })

  it("opens, previews and submits a single new version from its matrix cell", async () => {
    rpc.mockImplementationOnce(async () => data)
      .mockImplementationOnce(async () => ({ item_price: "NEW" }))
      .mockImplementationOnce(async () => data)
    await render("/tariffs?q=Deluxe")
    await click("New version", row("Corporate USD|Deluxe|").querySelector("[data-item='DLX']")!)
    const dialog = host.querySelector("dialog[open]")!
    expect(dialog.textContent).toContain("Corporate USD")
    expect(dialog.textContent).toContain("USD 130.00")
    expect(dialog.querySelector<HTMLInputElement>("input[name=valid_from]")?.value).toBe("2026-11-23")
    await input(dialog.querySelector("input[type=number]")!, "140")
    expect(dialog.textContent).toContain("USD 140.00 ≈ UGX 567,000")
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
    await click("Pending approval (1)")
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

  it("creates new versions in bulk with reference to the current rates", async () => {
    rpc.mockImplementationOnce(async () => data)
      .mockImplementationOnce(async () => ({ proposed: ["N1", "N2"] }))
      .mockImplementationOnce(async () => data)
    await render()
    await check("Select Deluxe · Corporate USD")
    expect(text()).toContain("3 tariffs selected")
    await click("Create new version in bulk")
    const dialog = host.querySelector("dialog[open]")!
    expect(field<HTMLSelectElement>(dialog, "adjustment").value).toBe("percent")
    await input(field(dialog, "adjustment_value"), "5")
    await input(field(dialog, "valid_upto"), "2026-12-31")
    expect(text(dialog)).toContain("Deluxe King single: USD 110.00 → USD 115.50")
    expect(text(dialog)).toContain("Deluxe King: USD 130.00 → USD 136.50")
    expect(text(dialog)).toContain("1 tariff has no current rate and is left out.")
    await click("Submit 2 for approval", dialog)
    expect(rpc.mock.calls.slice(1)).toEqual([
      ["kamra.rates.bulk_propose_contract_tariffs", { property: "Test Hotel", entries: [
        { price_list: usd.name, item_code: "DLX-1A", price_list_rate: 115.5, valid_from: "2026-11-23", valid_upto: "2026-12-31" },
        { price_list: usd.name, item_code: "DLX", price_list_rate: 136.5, valid_from: "2026-11-23", valid_upto: "2026-12-31" },
      ] }],
      [listMethod, { property: "Test Hotel" }],
    ])
    expect(text()).toContain("2 tariff versions submitted for approval")
  })

  it("shifts the reference dates and rounds a fixed adjustment for every selected row", async () => {
    rpc.mockImplementationOnce(async () => data)
      .mockImplementationOnce(async () => ({ proposed: ["N1", "N2", "N3"] }))
      .mockImplementationOnce(async () => data)
    await render()
    await check("Select all")
    await click("Create new version in bulk")
    const dialog = host.querySelector("dialog[open]")!
    await choose(field(dialog, "adjustment"), "amount")
    await input(field(dialog, "adjustment_value"), "7.25")
    await input(field(dialog, "rounding_step"), "5")
    await choose(field(dialog, "rounding_mode"), "Up")
    await choose(field(dialog, "date_mode"), "shift")
    await input(field(dialog, "shift_days"), "30")
    expect(text(dialog)).toContain("Deluxe King: USD 130.00 → USD 140.00")
    await click("Submit 3 for approval", dialog)
    expect(rpc.mock.calls[1]).toEqual(["kamra.rates.bulk_propose_contract_tariffs", { property: "Test Hotel", entries: [
      { price_list: usd.name, item_code: "DLX-1A", price_list_rate: 120, valid_from: "2026-12-22", valid_upto: null },
      { price_list: usd.name, item_code: "DLX", price_list_rate: 140, valid_from: "2026-12-22", valid_upto: null },
      { price_list: usd.name, item_code: "DLX", price_list_rate: 130, valid_from: "2026-12-22", valid_upto: null, customer: "Client Co" },
    ] }])
  })

  it("keeps bulk versions that would start before their current version from being submitted", async () => {
    await render()
    const dialog = await openBulk()
    await input(field(dialog, "adjustment_value"), "5")
    await input(field(dialog, "valid_from"), "2026-11-22")
    expect(text(dialog)).toContain("2 versions would start before their current versions or end before they start. Change the dates.")
    expect(button("Submit 2 for approval", dialog).disabled).toBe(true)
  })

  it("sets an exact rate in bulk, including a tariff with no current rate", async () => {
    rpc.mockImplementationOnce(async () => data)
      .mockImplementationOnce(async () => ({ proposed: ["N1", "N2", "N3"] }))
      .mockImplementationOnce(async () => data)
    await render()
    const dialog = await openBulk()
    await choose(field(dialog, "adjustment"), "exact")
    await input(field(dialog, "adjustment_value"), "160")
    expect(text(dialog)).toContain("Deluxe King triple: — → USD 160.00")
    expect(text(dialog)).not.toContain("left out")
    await click("Submit 3 for approval", dialog)
    const { entries } = rpc.mock.calls[1][1] as { entries: { item_code: string; price_list_rate: number }[] }
    expect(entries.map((entry) => [entry.item_code, entry.price_list_rate])).toEqual([["DLX-1A", 160], ["DLX", 160], ["DLX-3A", 160]])
  })

  it.each([
    [1000, "Up", "rounded up to 1,000"],
    [500, "Down", "rounded down to 500"],
    [0, "Nearest", "standard currency rounding"],
  ])("labels a %s %s rounding rule and shows a derived plan's inherited currency", async (step, mode, label) => {
    rpc.mockResolvedValueOnce({ ...data, rate_plan_policies: {
      "Test Hotel-CORP": { ...policies["Test Hotel-CORP"], currency: null, rounding_step: step, rounding_mode: mode },
      "Test Hotel-BAR": { ...policies["Test Hotel-BAR"], currency: "USD" },
    } })
    await render()
    expect(text()).toContain(`Governed posting FX: 1 USD = 4,048.22 UGX · Nov 22, 2026 · ${label}`)
    expect(text()).toContain("Corporate · USD · Fixed at Check In · Derived from Best available")
  })

  it("leaves local amounts unrounded when the price list's rate plans round differently", async () => {
    rpc.mockResolvedValueOnce({ ...data, price_lists: [{ ...usd, rate_plans: ["Test Hotel-CORP", "Test Hotel-BAR"] }, local] })
    await render()
    expect(text()).toContain("Governed posting FX: 1 USD = 4,048.22 UGX · Nov 22, 2026 · rounding set per rate plan")
    expect(text(row("Corporate USD|Deluxe|"))).toContain("≈ UGX 526,268")
  })

  it("shows an occupancy that is not an adult count in its own column", async () => {
    const family = { ...deluxe, item_code: "DLX-FAM", item_name: "Deluxe King family", occupancy: "Family",
      active: { ...active, name: "FAM", rate: 180, local_equiv: 728680 }, pending: [], history: [] }
    rpc.mockResolvedValueOnce({ ...data, items: [...data.items, family] })
    await render()
    expect(Array.from(host.querySelectorAll("thead th"), (node) => node.textContent?.trim())).toContain("Other occupancy")
    expect(text(row("Corporate USD|Deluxe|").querySelector("[data-item='DLX-FAM']")!)).toContain("USD 180.00")
    expect(text(row("Corporate USD|Deluxe|"))).toContain("Family")
  })

  it("approves the selected drafts but leaves out the ones the approver proposed", async () => {
    const own = { ...draft, name: "OWN-DRAFT", rate: 112, owner: "me", can_approve: false }
    const queue = { ...data, items: [item, { ...single, pending: [own] }, triple, corporate, extraAdult, bed, hall] }
    rpc.mockImplementationOnce(async () => queue)
      .mockImplementationOnce(async () => ({ approved: [draft.name] }))
      .mockImplementationOnce(async () => queue)
    await render()
    await click("Pending approval (2)")
    await check("Select all")
    await click("Approve selected")
    const dialog = host.querySelector("dialog[open]")!
    expect(text(dialog)).toContain("Approve 1 tariff version?")
    expect(text(dialog)).toContain("1 draft is left out: a different approver must release a draft its maker proposed.")
    await click("Approve", dialog)
    expect(rpc.mock.calls[1]).toEqual(["kamra.rates.bulk_approve_contract_tariffs", { item_prices: [draft.name] }])
    expect(text()).toContain("1 tariff version approved")
  })

  it("discards the selected drafts after confirmation", async () => {
    rpc.mockImplementationOnce(async () => data)
      .mockImplementationOnce(async () => ({ discarded: [draft.name] }))
      .mockImplementationOnce(async () => data)
    await render()
    await click("Pending approval (1)")
    await check("Select all")
    await click("Discard selected")
    const dialog = host.querySelector("dialog[open]")!
    expect(text(dialog)).toContain("Discard 1 tariff draft?")
    await click("Discard", dialog)
    expect(rpc.mock.calls[1]).toEqual(["kamra.rates.bulk_discard_contract_tariffs", { item_prices: [draft.name] }])
  })

  it("exports the selected price list to CSV for offline editing", async () => {
    const columns = ["Price List", "Currency", "Customer", "Item Code", "Room Type", "Occupancy Tier", "Rate",
      "Valid From", "Valid Upto", "Status"]
    const rows = [["Corporate USD", "USD", "", "DLX", "Deluxe", "Double", 130, "2026-11-22", "", "Approved"]]
    rpc.mockImplementationOnce(async () => data)
      .mockImplementationOnce(async () => ({ supported: true, business_date: "2026-11-22", columns, rows }))
    await render()
    await click("Export CSV")
    expect(rpc.mock.calls[1]).toEqual(["kamra.rates.export_contract_tariffs_csv", { property: "Test Hotel", price_list: usd.name }])
    expect(download).toHaveBeenCalledWith("contract-tariffs-2026-11-22", columns, rows)
  })

  it("imports a CSV as drafts and lists the rows it refused", async () => {
    const csv = "Price List,Currency,Item Code,Rate,Valid From\r\nCorporate USD,USD,DLX,145,2026-12-15"
    rpc.mockImplementationOnce(async () => data)
      .mockImplementationOnce(async () => ({ created: 0, unchanged: 0, skipped: 0,
        errors: [{ row: 2, message: "UGX does not match the Corporate USD price list currency (USD)." }] }))
      .mockImplementationOnce(async () => ({ created: 3, unchanged: 1, skipped: 0, errors: [] }))
      .mockImplementationOnce(async () => data)
    await render()
    await click("Import CSV")
    const dialog = host.querySelector("dialog[open]")!
    const file = field<HTMLInputElement>(dialog, "tariff_file")
    await act(async () => {
      Object.defineProperty(file, "files", { configurable: true, value: [new File([csv], "tariffs.csv", { type: "text/csv" })] })
      file.dispatchEvent(new Event("change", { bubbles: true }))
    })
    await click("Import as drafts", dialog)
    await vi.waitFor(() => expect(text(dialog)).toContain("Row 2: UGX does not match the Corporate USD price list currency (USD)."))
    expect(rpc.mock.calls[1]).toEqual(["kamra.rates.import_contract_tariffs_rows", { property: "Test Hotel", rows: csv }])
    expect(host.querySelector("dialog[open]")).not.toBeNull()
    await click("Import as drafts", dialog)
    await vi.waitFor(() => expect(text()).toContain("3 draft versions created for approval · 1 unchanged"))
    expect(rpc.mock.calls[3]).toEqual([listMethod, { property: "Test Hotel" }])
    expect(rpc.mock.calls.flat()).not.toContain("kamra.rates.bulk_approve_contract_tariffs")
  })

  it("reports an export the server could not produce", async () => {
    rpc.mockImplementationOnce(async () => data).mockImplementationOnce(async () => { throw new Error("Export failed") })
    await render()
    await click("Export CSV")
    await vi.waitFor(() => expect(text()).toContain("The contract tariffs could not be exported."))
    expect(download).not.toHaveBeenCalled()
  })

  it("says when an import has nothing new to create", async () => {
    rpc.mockImplementationOnce(async () => data)
      .mockImplementationOnce(async () => ({ created: 0, unchanged: 2, skipped: 0, errors: [] }))
      .mockImplementationOnce(async () => data)
    await render()
    await click("Import CSV")
    const dialog = host.querySelector("dialog[open]")!
    await chooseFile(dialog, "Price List,Currency,Item Code,Rate,Valid From\r\nCorporate USD,USD,DLX,130,2026-11-22")
    await click("Import as drafts", dialog)
    await vi.waitFor(() => expect(text()).toContain("No new versions to create · 2 unchanged"))
  })

  it("honors both property and row authorization without hiding the draft", async () => {
    rpc.mockResolvedValueOnce({ ...data, can_propose: false, can_approve: false, can_discard: false,
      items: [{ ...item, pending: [{ ...draft, can_approve: false }] }] })
    await render()
    expect(text()).toContain("In review: USD 140.00")
    expect(labels()).toContain("Export CSV")
    expect(text()).not.toMatch(/New version|Discard|Import CSV|Create new version in bulk/)
    expect(labels()).not.toContain("Approve")
    expect(labels()).not.toContain("Approve selected")
  })

  it("keeps the proposal open and does not report success when the write bridge is absent", async () => {
    rpc.mockResolvedValueOnce(data).mockResolvedValueOnce(null)
    await render("/tariffs?q=Deluxe")
    await click("New version", row("Corporate USD|Deluxe|").querySelector("[data-item='DLX']")!)
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

  it.each([
    ["ar", "التعرفة التعاقدية", "نسخة جديدة", "مصفوفة الغرف", "تصدير CSV"],
    ["de", "Vertragstarife", "Neue Version", "Zimmermatrix", "CSV exportieren"],
    ["es", "Tarifas contractuales", "Nueva versión", "Matriz de habitaciones", "Exportar CSV"],
    ["fil", "Mga kontratang taripa", "Bagong bersyon", "Matrix ng kuwarto", "I-export ang CSV"],
    ["id", "Tarif kontrak", "Versi baru", "Matriks kamar", "Ekspor CSV"],
    ["pt", "Tarifas contratuais", "Nova versão", "Matriz de quartos", "Exportar CSV"],
    ["zh-CN", "合同价格", "新建版本", "房型价格矩阵", "导出 CSV"],
  ])("translates tariff-specific controls in %s", async (language, heading, action, matrix, exportLabel) => {
    await ensureDictionary(language)
    setLang(language)
    await render()
    expect(host.querySelector("h1")?.textContent).toBe(heading)
    expect(text()).toContain(action)
    expect(labels()).toEqual(expect.arrayContaining([`${matrix} (2)`, exportLabel]))
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
    expect(link?.getAttribute("href")).toBe("/tariffs?q=Deluxe+King&room_type=DLX+Test")
  })

  it("offers the editor link from Rate Plans", async () => {
    await render("/rate-plans")
    const link = host.querySelector("a[href='/tariffs']")
    expect(link?.textContent ?? "").toContain("Manage tariffs")
  })

  it("edits a rate plan's currency, exchange rate lock, base plan and rounding", async () => {
    await render("/rate-plans")
    const headers = Array.from(host.querySelectorAll("th"), (node) => node.textContent?.trim())
    expect(headers).toEqual(expect.arrayContaining(["Currency", "Exchange rate lock", "Derived from"]))
    await click("New")
    for (const label of ["Currency (blank = property currency)", "Exchange rate lock", "Base rate plan (derived rate)",
      "Adjust extra person charges", "Rounding step", "Rounding mode"]) expect(text()).toContain(label)
    const options = Array.from(host.querySelectorAll("select option"), (node) => node.textContent)
    expect(options).toEqual(expect.arrayContaining(["Fixed at Reservation", "Fixed at Check In", "Current Date",
      "Nearest", "Up", "Down"]))
    // no group block locks a rate (P1-5): the policy is not offered
    expect(options).not.toContain("Fixed at Block")
  })

  // ── Round 2 (Gap Record #8556) ─────────────────────────────────────────────
  it("proposes a customer's own version from its row and names the customer", async () => {
    rpc.mockImplementationOnce(async () => data)
      .mockImplementationOnce(async () => ({ name: "NEW", customer: "Client Co" }))
      .mockImplementationOnce(async () => data)
    await render("/tariffs?q=Deluxe")
    await click("New version", row("Corporate USD|Deluxe|Client Co").querySelector("[data-item='DLX']")!)
    const dialog = host.querySelector("dialog[open]")!
    expect(text(dialog.querySelector("header")!)).toContain("Client Co")
    await input(dialog.querySelector("input[type=number]")!, "125")
    await click("Submit for approval", dialog)
    expect(rpc.mock.calls[1]).toEqual(["kamra.rates.propose_contract_tariff", { property: "Test Hotel",
      price_list: usd.name, item_code: "DLX", price_list_rate: 125, valid_from: "2026-11-23", valid_upto: null,
      customer: "Client Co" }])
  })

  it("reports the drafts a bulk approval skipped and why", async () => {
    const second = { ...draft, name: "DRAFT-2", rate: 150, valid_from: "2026-12-15" }
    const queue = { ...data, items: [{ ...item, pending: [draft, second] }, single, triple, corporate, extraAdult, bed, hall] }
    rpc.mockImplementationOnce(async () => queue)
      .mockImplementationOnce(async () => ({ ok: true, approved: 1, skipped: 1, results: [
        { name: draft.name, status: "approved" },
        { name: second.name, status: "skipped", reason: "HSP-TARIFF-005: Only a saved Draft can be approved." }] }))
      .mockImplementationOnce(async () => queue)
    await render()
    await click("Pending approval (2)")
    await check("Select all")
    await click("Approve selected")
    await click("Approve", host.querySelector("dialog[open]")!)
    await vi.waitFor(() => expect(text()).toContain(
      "1 tariff version approved · 1 skipped: HSP-TARIFF-005: Only a saved Draft can be approved."))
  })

  it("keeps a bulk decision open when every draft was skipped", async () => {
    rpc.mockImplementationOnce(async () => data)
      .mockImplementationOnce(async () => ({ ok: true, discarded: 0, skipped: 1, results: [
        { name: draft.name, status: "skipped", reason: "HSP-TARIFF-005: You may not delete this draft tariff." }] }))
    await render()
    await click("Pending approval (1)")
    await check("Select all")
    await click("Discard selected")
    const dialog = host.querySelector("dialog[open]")!
    await click("Discard", dialog)
    await vi.waitFor(() => expect(dialog.querySelector("[role=alert]")?.textContent).toContain(
      "No tariff drafts were discarded · 1 skipped: HSP-TARIFF-005: You may not delete this draft tariff."))
    expect(host.querySelector("dialog[open]")).not.toBeNull()
    expect(text()).not.toContain("tariff draft discarded")
  })

  it("lets a Hotel Admin discard, but not approve, a draft they proposed", async () => {
    const own = { ...draft, owner: "me", can_approve: false }
    const mine = { ...data, can_approve: false, can_discard: true, items: [{ ...item, pending: [own] }, single, hall] }
    rpc.mockImplementationOnce(async () => mine)
      .mockImplementationOnce(async () => ({ ok: true, discarded: 1, skipped: 0, results: [] }))
      .mockImplementationOnce(async () => mine)
    await render()
    await click("Pending approval (1)")
    expect(labels()).toContain("Discard")
    expect(labels()).not.toContain("Approve")
    await check("Select all")
    expect(labels()).toContain("Discard selected")
    expect(labels()).not.toContain("Approve selected")
    await click("Discard selected")
    const dialog = host.querySelector("dialog[open]")!
    expect(text(dialog)).toContain("Discard 1 tariff draft?")
    await click("Discard", dialog)
    expect(rpc.mock.calls[1]).toEqual(["kamra.rates.bulk_discard_contract_tariffs", { item_prices: [own.name] }])
    await vi.waitFor(() => expect(text()).toContain("1 tariff draft discarded"))
  })

  it("holds a bulk change to the integration's 200-row limit", async () => {
    const halls = Array.from({ length: 201 }, (_, n) => ({ ...hall, item_code: `HALL-${n}`, item_name: `Hall ${n}`,
      pending: [{ ...draft, name: `HALL-DRAFT-${n}` }] }))
    rpc.mockImplementationOnce(async () => ({ ...data, items: halls }))
    await render()
    await choose(field<HTMLSelectElement>(host, "price_list"), local.name)
    await click("Venues")
    await check("Select all")
    await click("Create new version in bulk")
    const versions = host.querySelector("dialog[open]")!
    expect(text(versions)).toContain("Choose at most 200 tariffs at a time.")
    await input(field<HTMLInputElement>(versions, "adjustment_value"), "5")
    expect(button("Submit 201 for approval", versions).disabled).toBe(true)
    await click("Cancel", versions)
    await click("Approve selected")
    const decision = host.querySelector("dialog[open]")!
    expect(text(decision)).toContain("Choose at most 200 tariffs at a time.")
    expect(button("Approve", decision).disabled).toBe(true)
    expect(rpc).toHaveBeenCalledTimes(1)
  })

  it("refuses an import file over 1 MB before reading it", async () => {
    rpc.mockImplementationOnce(async () => data)
    await render()
    await click("Import CSV")
    const dialog = host.querySelector("dialog[open]")!
    await chooseFile(dialog, "Price List,Currency,Item Code,Rate,Valid From\r\n" + "x".repeat(1_048_576))
    await click("Import as drafts", dialog)
    expect(dialog.querySelector("[role=alert]")?.textContent).toContain("The file is larger than 1 MB. Split it and import each part.")
    expect(rpc).toHaveBeenCalledTimes(1)
  })

  it("exports only the customer and effective date on screen", async () => {
    const columns = ["Price List"]
    rpc.mockImplementation(async (method: string) => method === listMethod ? data
      : { supported: true, business_date: "2026-11-22", columns, rows: [] })
    await render()
    await choose(field<HTMLSelectElement>(host, "customer"), "Client Co")
    await input(field<HTMLInputElement>(host, "as_of_date"), "2026-12-15")
    await vi.waitFor(() => expect(rpc).toHaveBeenLastCalledWith(listMethod, { property: "Test Hotel", as_of_date: "2026-12-15" }))
    await click("Export CSV")
    expect(rpc).toHaveBeenLastCalledWith("kamra.rates.export_contract_tariffs_csv", { property: "Test Hotel",
      price_list: usd.name, customer: "Client Co", as_of_date: "2026-12-15" })
    await choose(field<HTMLSelectElement>(host, "customer"), "__public__")
    await click("Export CSV")
    expect(rpc).toHaveBeenLastCalledWith("kamra.rates.export_contract_tariffs_csv", { property: "Test Hotel",
      price_list: usd.name, customer: "__public__", as_of_date: "2026-12-15" })
  })

  // ── Round 3: matrix and bulk-version UX (B-1, B-2) ─────────────────────────
  it("tells a customer's negotiated row apart from the public row at a glance", async () => {
    await render()
    const publicRow = row("Corporate USD|Deluxe|")
    const corporateRow = row("Corporate USD|Deluxe|Client Co")
    expect(publicRow.dataset.scope).toBe("public")
    expect(corporateRow.dataset.scope).toBe("corporate")
    const badge = (scope: HTMLElement) => scope.querySelector<HTMLElement>("th[role=rowheader] [data-scope-badge]")
    expect(text(badge(publicRow)!)).toBe("Public")
    expect(badge(publicRow)!.className).toContain("bg-subtle")
    expect(text(badge(corporateRow)!)).toContain("Corporate")
    expect(text(badge(corporateRow)!)).toContain("Client Co")
    expect(badge(corporateRow)!.className).toContain("bg-indigo-subtle")
    // the negotiated row carries an accent edge and tint; the public row stays plain
    expect(corporateRow.className).toContain("border-s-indigo-500")
    expect(corporateRow.classList).toContain("bg-indigo-subtle/25")
    expect(publicRow.classList).toContain("bg-surface")
    expect(publicRow.className).not.toContain("indigo")
    // the row's controls are unchanged
    expect(host.querySelector("input[aria-label='Select Deluxe · Corporate USD · Client Co']")).not.toBeNull()
    expect(text(corporateRow)).toContain("USD 120.00")
  })

  it("names the tariff currency in a bulk edit and keeps a posting-currency step out of a foreign tariff", async () => {
    await render()
    const dialog = await openBulk()
    // the plan rounds the UGX posting to 1,000; a USD tariff must not round to 1,000 dollars
    expect(field<HTMLInputElement>(dialog, "rounding_step").value).toBe("")
    expect(field<HTMLSelectElement>(dialog, "rounding_mode").value).toBe("Nearest")
    expect(text(dialog)).not.toContain("Prefilled from the rate plan rounding rule")
    expect(text(dialog)).toContain("Round to (USD — tariff currency; 0 = the currency's smallest unit)")
    expect(text(dialog)).toContain("Tariffs are edited in the price list's contract currency; the UGX posting converts "
      + "at booking or check-in under the rate plan's exchange rate lock.")
    await choose(field(dialog, "adjustment"), "amount")
    expect(text(dialog)).toContain("Amount change (USD)")
    await choose(field(dialog, "adjustment"), "exact")
    expect(text(dialog)).toContain("New rate (USD)")
    await input(field(dialog, "adjustment_value"), "125.5")
    expect(text(dialog)).toContain("Deluxe King: USD 130.00 → USD 125.50")
  })

  it("prefills the bulk rounding from the plans' shared rule on tariffs priced in the posting currency", async () => {
    rpc.mockImplementationOnce(async () => ({ ...data,
      price_lists: [usd, { ...local, rate_plans: ["Test Hotel-LOC", "Test Hotel-LOC2"] }],
      rate_plan_policies: { ...policies,
        "Test Hotel-LOC": { label: "Local", code: "LOC", currency: null, fx_lock_policy: "Fixed at Reservation",
          base_rate_plan: null, adjust_extra_person: false, rounding_step: 500, rounding_mode: "Up" },
        "Test Hotel-LOC2": { label: "Local weekend", code: "LOC2", currency: null, fx_lock_policy: "Fixed at Reservation",
          base_rate_plan: null, adjust_extra_person: false, rounding_step: 500, rounding_mode: "Up" } } }))
      .mockImplementationOnce(async () => ({ proposed: ["N1"] }))
      .mockImplementationOnce(async () => data)
    await render()
    await choose(field<HTMLSelectElement>(host, "price_list"), local.name)
    await click("Venues")
    await check("Select all")
    await click("Create new version in bulk")
    const dialog = host.querySelector("dialog[open]")!
    expect(field<HTMLInputElement>(dialog, "rounding_step").value).toBe("500")
    expect(field<HTMLSelectElement>(dialog, "rounding_mode").value).toBe("Up")
    expect(text(dialog)).toContain("Prefilled from the rate plan rounding rule")
    expect(text(dialog)).toContain("Round to (UGX — tariff currency; 0 = the currency's smallest unit)")
    // a tariff in the posting currency needs no conversion note
    expect(text(dialog)).not.toContain("Tariffs are edited in the price list's contract currency")
    await choose(field(dialog, "adjustment"), "amount")
    expect(text(dialog)).toContain("Amount change (UGX)")
    await input(field(dialog, "adjustment_value"), "1234")
    expect(text(dialog)).toContain("Garden Hall: UGX 850,000 → UGX 851,500")
    // the prefill stays editable
    await input(field(dialog, "rounding_step"), "0")
    expect(text(dialog)).toContain("Garden Hall: UGX 850,000 → UGX 851,234")
    await click("Submit 1 for approval", dialog)
    expect(rpc.mock.calls[1]).toEqual(["kamra.rates.bulk_propose_contract_tariffs", { property: "Test Hotel", entries: [
      { price_list: local.name, item_code: "HALL", price_list_rate: 851234, valid_from: "2026-11-23", valid_upto: null },
    ] }])
  })

  it("says each tariff keeps its own currency when a bulk edit spans currencies", async () => {
    const terrace = { ...hall, item_code: "TERRACE", item_name: "Sky Terrace", price_list: usd.name, currency: "USD",
      active: { ...active, name: "TER-1", rate: 400, local_equiv: null } }
    rpc.mockImplementationOnce(async () => ({ ...data, price_lists: [usd, { ...local, rate_plans: ["Test Hotel-CORP"] }],
      items: [...data.items, terrace] }))
    await render()
    await choose(field<HTMLSelectElement>(host, "price_list"), "")
    await click("Venues")
    await check("Select all")
    await click("Create new version in bulk")
    const dialog = host.querySelector("dialog[open]")!
    expect(field<HTMLInputElement>(dialog, "rounding_step").value).toBe("")
    expect(text(dialog)).toContain("Round to (in each tariff's currency; 0 = the currency's smallest unit)")
    expect(text(dialog)).toContain("Tariffs are edited in the price list's contract currency")
    await choose(field(dialog, "adjustment"), "amount")
    expect(text(dialog)).toContain("Amount change (in each tariff's currency)")
    await choose(field(dialog, "adjustment"), "exact")
    expect(text(dialog)).toContain("New rate (in each tariff's currency)")
  })

  // ── Round 6 (OPERA Cloud / SAP S/4HANA parity) ─────────────────────────────
  it("creates a new customer's first own rate from a public row's single version dialog", async () => {
    resource.mockImplementation(async (path: string) => path.startsWith("/api/resource/Customer")
      ? { data: [{ name: "New Client", customer_name: "New Client Ltd" }, { name: "Client Co", customer_name: "Client Co" }] }
      : { data: path.includes("?") ? [] : {} })
    rpc.mockImplementationOnce(async () => data)
      .mockImplementationOnce(async () => ({ name: "NEW", customer: "New Client" }))
      .mockImplementationOnce(async () => data)
    await render("/tariffs?q=Deluxe")
    await click("New version", row("Corporate USD|Deluxe|").querySelector("[data-item='DLX']")!)
    const dialog = host.querySelector("dialog[open]")!
    const customer = field<HTMLInputElement>(dialog, "customer")
    expect(text(dialog)).toContain("Customer (optional)")
    expect(text(dialog)).toContain("Leave empty for the public rate.")
    const suggestions = Array.from(dialog.querySelectorAll(`datalist[id="${customer.getAttribute("list")}"] option`),
      (option) => option.getAttribute("value"))
    expect(suggestions).toEqual(["Client Co", "New Client"])
    await input(customer, "New Client")
    expect(text(dialog.querySelector("header")!)).toContain("Customer rate: New Client")
    await input(dialog.querySelector("input[type=number]")!, "118")
    await click("Submit for approval", dialog)
    expect(rpc.mock.calls[1]).toEqual(["kamra.rates.propose_contract_tariff", { property: "Test Hotel",
      price_list: usd.name, item_code: "DLX", price_list_rate: 118, valid_from: "2026-11-23", valid_upto: null,
      customer: "New Client" }])
  })

  it("keeps a customer row's own customer in its single version dialog", async () => {
    await render("/tariffs?q=Deluxe")
    await click("New version", row("Corporate USD|Deluxe|Client Co").querySelector("[data-item='DLX']")!)
    expect(host.querySelector("dialog[open] [name=customer]")).toBeNull()
  })

  it("creates a customer's own rates in bulk from the selected public tariffs", async () => {
    rpc.mockImplementationOnce(async () => data)
      .mockImplementationOnce(async () => ({ proposed: ["N1", "N2"] }))
      .mockImplementationOnce(async () => data)
    await render()
    const dialog = await openBulk()
    expect(text(dialog)).toContain("Leave empty to keep each tariff's own customer.")
    await input(field(dialog, "customer"), "New Client")
    await input(field(dialog, "adjustment_value"), "-10")
    expect(text(dialog)).toContain("Deluxe King · New Client: USD 130.00 → USD 117.00")
    await click("Submit 2 for approval", dialog)
    expect(rpc.mock.calls[1]).toEqual(["kamra.rates.bulk_propose_contract_tariffs", { property: "Test Hotel", entries: [
      { price_list: usd.name, item_code: "DLX-1A", price_list_rate: 99, valid_from: "2026-11-23", valid_upto: null,
        customer: "New Client" },
      { price_list: usd.name, item_code: "DLX", price_list_rate: 117, valid_from: "2026-11-23", valid_upto: null,
        customer: "New Client" },
    ] }])
  })

  it("prices a customer's bulk version from that customer's own row when both rows are selected", async () => {
    rpc.mockImplementationOnce(async () => data)
      .mockImplementationOnce(async () => ({ proposed: ["N1", "N2"] }))
      .mockImplementationOnce(async () => data)
    await render()
    await check("Select all")
    await click("Create new version in bulk")
    const dialog = host.querySelector("dialog[open]")!
    expect(field(dialog, "customer"), "the bulk dialog's customer picker").not.toBeNull()
    await input(field(dialog, "customer"), "Client Co")
    await choose(field(dialog, "adjustment"), "amount")
    await input(field(dialog, "adjustment_value"), "5")
    await click("Submit 2 for approval", dialog)
    expect(rpc.mock.calls[1]).toEqual(["kamra.rates.bulk_propose_contract_tariffs", { property: "Test Hotel", entries: [
      { price_list: usd.name, item_code: "DLX-1A", price_list_rate: 115, valid_from: "2026-11-23", valid_upto: null,
        customer: "Client Co" },
      { price_list: usd.name, item_code: "DLX", price_list_rate: 125, valid_from: "2026-11-23", valid_upto: null,
        customer: "Client Co" },
    ] }])
  })

  it("proposes a complimentary (zero) supplement from the single version dialog", async () => {
    rpc.mockImplementationOnce(async () => data)
      .mockImplementationOnce(async () => ({ item_price: "NEW" }))
      .mockImplementationOnce(async () => data)
    await render()
    await click("Supplements")
    await click("New version", host.querySelector("tr[data-item='TAR-EXTRA-ADULT']")!)
    const dialog = host.querySelector("dialog[open]")!
    const amount = dialog.querySelector<HTMLInputElement>("input[type=number]")!
    expect(amount.min).toBe("0")
    await input(amount, "0")
    // the default effective date follows the clock for a version that did not start on the business date
    await input(field(dialog, "valid_from"), "2026-11-23")
    await click("Submit for approval", dialog)
    expect(rpc.mock.calls[1]).toEqual(["kamra.rates.propose_contract_tariff", { property: "Test Hotel",
      price_list: usd.name, item_code: "TAR-EXTRA-ADULT", price_list_rate: 0, valid_from: "2026-11-23", valid_upto: null }])
  })

  it("still needs a room rate above zero", async () => {
    await render("/tariffs?q=Deluxe")
    await click("New version", row("Corporate USD|Deluxe|").querySelector("[data-item='DLX']")!)
    const dialog = host.querySelector("dialog[open]")!
    expect(dialog.querySelector<HTMLInputElement>("input[type=number]")!.min).toBe("0.01")
    await input(dialog.querySelector("input[type=number]")!, "0")
    await click("Submit for approval", dialog)
    expect(rpc).toHaveBeenCalledTimes(1)
  })

  it("shows who proposed, approved or rejected each version and when", async () => {
    const audited = { ...item,
      pending: [{ ...draft, owner: "maker@example.test", creation: "2026-11-21 10:05:00" }],
      history: [
        { name: "OLD", rate: 115, valid_from: "2026-10-01", valid_upto: "2026-11-21", workflow_state: "Approved",
          owner: "maker@example.test", creation: "2026-09-28 09:15:00", approver: "checker@example.test",
          modified_by: "checker@example.test", modified: "2026-09-29 16:40:00" },
        { name: "REJ", rate: 125, valid_from: "2026-11-01", workflow_state: "Rejected", owner: "maker@example.test",
          creation: "2026-10-20 08:00:00", modified_by: "checker@example.test", modified: "2026-10-21 12:30:00",
          rejection_reason: "Above the corporate ceiling" },
        // an older integration answers no audit fields: the row still reads cleanly
        { name: "BARE", rate: 105, valid_from: "2026-08-01", workflow_state: "Approved" },
      ] }
    rpc.mockImplementation(async (method: string) => method === listMethod
      ? { ...data, items: [audited, single, triple, corporate, extraAdult, bed, hall] } : null)
    await render()
    const publicRow = row("Corporate USD|Deluxe|")
    expect(text(publicRow)).toContain("Proposed by maker@example.test")
    expect(text(publicRow)).toContain("Nov 21, 2026")
    await click("History", publicRow)
    const history = host.querySelector<HTMLElement>("tr[id] ul")!
    const [approved, rejected, bare] = Array.from(history.querySelectorAll("li"), (node) => node.textContent ?? "")
    expect(approved).toContain("Proposed by maker@example.test")
    expect(approved).toContain("Sep 28, 2026")
    expect(approved).toContain("Approved by checker@example.test")
    expect(approved).toContain("Sep 29, 2026")
    expect(rejected).toContain("Rejected by checker@example.test")
    expect(rejected).toContain("Oct 21, 2026")
    expect(rejected).toContain("Reason: Above the corporate ceiling")
    expect(bare).not.toMatch(/by|Reason|undefined|null|Invalid/)
  })

  it("schedules only the versions that start after the effective date being viewed", async () => {
    await render()
    expect(text(row("Corporate USD|Deluxe|"))).toContain("Scheduled: USD 150.00 from Jan 1, 2027")
    await input(field<HTMLInputElement>(host, "as_of_date"), "2027-01-15")
    await vi.waitFor(() => expect(rpc).toHaveBeenLastCalledWith(listMethod, { property: "Test Hotel", as_of_date: "2027-01-15" }))
    await vi.waitFor(() => expect(host.textContent).not.toContain("Refreshing contract tariffs…"))
    expect(text(row("Corporate USD|Deluxe|"))).not.toContain("Scheduled: USD 150.00")
  })

  // ── Round 7 (Gemini 4 Neon re-review R6) ───────────────────────────────────
  const fxLockHint = "When a foreign-currency contract converts to the property's currency: at booking, at check-in, "
    + "or on each night's posting date."
  const groupBlockHint = "When a foreign-currency contract converts to the property's currency: at booking, at "
    + "check-in, on each night's posting date, or when the group block is created."

  it("explains the exchange rate lock without the group block lock it no longer offers", async () => {
    await render("/rate-plans")
    await click("New")
    expect(text()).toContain(fxLockHint)
    expect(text()).not.toMatch(/group block/i)
  })

  it.each(["ar", "de", "es", "fil", "id", "pt", "zh-CN"])("translates the exchange rate lock hint in %s", async (language) => {
    const dictionary = await ensureDictionary(language)
    expect(dictionary[fxLockHint]?.trim(), `${language}: ${fxLockHint}`).toBeTruthy()
    expect(dictionary).not.toHaveProperty([groupBlockHint])
  })

  it("lets a new customer's first own rate start on the business date when the public version did", async () => {
    rpc.mockImplementationOnce(async () => data)
      .mockImplementationOnce(async () => ({ name: "NEW", customer: "New Client" }))
      .mockImplementationOnce(async () => data)
    await render("/tariffs?q=Deluxe")
    await click("New version", row("Corporate USD|Deluxe|").querySelector("[data-item='DLX']")!)
    const dialog = host.querySelector("dialog[open]")!
    const from = field<HTMLInputElement>(dialog, "valid_from")
    // the public row's own version started on the business date: its next public version starts tomorrow
    expect(from.min).toBe("2026-11-23")
    // a customer's first price follows no version of its own (as the bulk dialog has it)
    await input(field<HTMLInputElement>(dialog, "customer"), "New Client")
    expect(from.min).toBe("2026-11-22")
    await input(from, "2026-11-22")
    await input(dialog.querySelector("input[type=number]")!, "118")
    await click("Submit for approval", dialog)
    expect(rpc.mock.calls[1]).toEqual(["kamra.rates.propose_contract_tariff", { property: "Test Hotel",
      price_list: usd.name, item_code: "DLX", price_list_rate: 118, valid_from: "2026-11-22", valid_upto: null,
      customer: "New Client" }])
  })

  // ── Round 7 (Tank Hill: list room types without a tariff) ──────────────────
  it("lists room types without a tariff on the Room matrix tab so none are missed", async () => {
    const withUnpriced = {
      ...data,
      unpriced_room_types: [
        { name: "Test Hotel-EXE", room_type_name: "Executive Suite", room_type_code: "EXE",
          room_count: 2, price_lists: ["Corporate USD", "Local rates"] },
        { name: "Test Hotel-STD", room_type_name: "Standard", room_type_code: "STD",
          room_count: 0, price_lists: ["Corporate USD"] },
      ],
    }
    rpc.mockImplementation(async (method: string) => method === listMethod ? withUnpriced : null)
    await render()
    const section = host.querySelector<HTMLElement>("section[aria-label='Room types without a tariff']")
    expect(section, "Missing Room types without a tariff section").not.toBeNull()
    expect(text(section!)).toContain("Room types without a tariff")
    expect(text(section!)).toContain("Executive Suite")
    expect(text(section!)).toContain("2 rooms")
    expect(text(section!)).toContain("Standard")
    expect(text(section!)).toContain("No rooms")
    const link = section!.querySelector<HTMLAnchorElement>("a[href='/room-types?open=Test%20Hotel-EXE']")
    expect(link, "Missing Room Types link for Executive Suite").not.toBeNull()
    // Switching to Local rates hides Standard (only missing on Corporate USD) and keeps Executive Suite
    await choose(field<HTMLSelectElement>(host, "price_list"), "Local rates")
    const updated = host.querySelector<HTMLElement>("section[aria-label='Room types without a tariff']")!
    expect(text(updated)).toContain("Executive Suite")
    expect(text(updated)).not.toContain("Standard")
  })

  it.each(["ar", "de", "es", "fil", "id", "pt", "zh-CN"])("translates unpriced room types labels in %s", async (language) => {
    const dictionary = await ensureDictionary(language)
    for (const key of [
      "Room types without a tariff",
      "These room types have no contract tariff on this price list yet.",
      "No rooms",
      "{n} room",
      "{n} rooms",
      "Open room type",
    ]) {
      expect(dictionary[key]?.trim(), `${language}: missing ${key}`).toBeTruthy()
      expect(dictionary[key], `${language}: untranslated ${key}`).not.toBe(key)
    }
  })
})
