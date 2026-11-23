// @vitest-environment jsdom
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

const { rpc, download } = vi.hoisted(() => ({ rpc: vi.fn(), download: vi.fn() }))
vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => method === "kamra.api.property_locale"
    ? Promise.resolve({ locale: "en", currency: "UGX" }) : rpc(method, params),
  getCurrentProperty: () => "Test Hotel",
  frappeFetch: async (path: string) => ({ data: path.includes("?") ? [] : {} }),
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
  can_propose: true, can_approve: true, price_lists: [usd, local], rate_plan_policies: policies,
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
    const result = Array.from(scope.querySelectorAll("button")).find((node) => node.textContent?.trim() === label)
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
    expect(labels()).toEqual(expect.arrayContaining(["Room matrix", "Supplements", "Venues", "Other", "Pending approval (1)"]))
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

  it("honors both property and row authorization without hiding the draft", async () => {
    rpc.mockResolvedValueOnce({ ...data, can_propose: false, can_approve: false,
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
    ["ar", "التعرفة التعاقدية", "نسخة جديدة"],
    ["de", "Vertragstarife", "Neue Version"],
    ["es", "Tarifas contractuales", "Nueva versión"],
    ["fil", "Mga kontratang taripa", "Bagong bersyon"],
    ["id", "Tarif kontrak", "Versi baru"],
    ["pt", "Tarifas contratuais", "Nova versão"],
    ["zh-CN", "合同价格", "新建版本"],
  ])("translates tariff-specific controls in %s", async (language, heading, action) => {
    await ensureDictionary(language)
    setLang(language)
    await render()
    expect(host.querySelector("h1")?.textContent).toBe(heading)
    expect(text()).toContain(action)
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

  it("edits a rate plan's currency, exchange rate lock, base plan and rounding", async () => {
    await render("/rate-plans")
    const headers = Array.from(host.querySelectorAll("th"), (node) => node.textContent?.trim())
    expect(headers).toEqual(expect.arrayContaining(["Currency", "Exchange rate lock", "Derived from"]))
    await click("New")
    for (const label of ["Currency (blank = property currency)", "Exchange rate lock", "Base rate plan (derived rate)",
      "Adjust extra person charges", "Rounding step", "Rounding mode"]) expect(text()).toContain(label)
    const options = Array.from(host.querySelectorAll("select option"), (node) => node.textContent)
    expect(options).toEqual(expect.arrayContaining(["Fixed at Reservation", "Fixed at Check In", "Current Date",
      "Fixed at Block", "Nearest", "Up", "Down"]))
  })
})
