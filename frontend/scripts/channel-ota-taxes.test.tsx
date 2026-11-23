// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import App from "../src/App"
import ar from "../src/i18n/locales/ar.json"
import de from "../src/i18n/locales/de.json"
import es from "../src/i18n/locales/es.json"
import fil from "../src/i18n/locales/fil.json"
import id from "../src/i18n/locales/id.json"
import pt from "../src/i18n/locales/pt.json"
import zhCN from "../src/i18n/locales/zh-CN.json"

/** The Channel Manager screen shows and edits a connection's OTA Tax Rules
 *  (which of the taxes an OTA lists is the room tax, tried top to bottom)
 *  and OTA Tax Basis (whether each OTA's amounts include tax), and its one
 *  Save sends both back with the rest of the connection: the client sets
 *  them here, as OPERA lets a property set its channel mappings, never in
 *  the back office. */

const { fetches } = vi.hoisted(() => ({ fetches: vi.fn() }))
vi.mock("../src/lib/api", () => ({
  call: async (method: string) => method === "kamra.api.property_locale"
    ? { locale: "en", currency: "UGX" }
    : method === "kamra.channel_ari.outbox_status" ? { outbox: false } : null,
  getCurrentProperty: () => "Test Hotel",
  frappeFetch: (path: string, init?: RequestInit) => fetches(path, init),
  serverError: (error: Error) => error.message,
}))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: ["Hotel Admin"] }) }))
vi.mock("../src/AppShell", () => ({ default: () => <Outlet /> }))

const rule = (name: string, idx: number, match_on: string, match_type: string, match_text: string, counts_as: string) => ({
  name, idx, doctype: "Channel OTA Tax Rule", parent: "CMC-00001", parentfield: "ota_tax_rules",
  parenttype: "Channel Manager Connection", match_on, match_type, match_text, counts_as,
})
const connection = {
  name: "CMC-00001", doctype: "Channel Manager Connection", property: "Test Hotel", provider: "Channex",
  external_property_id: "cx-1", active: 1, sync_days: 365,
  ota_tax_rules: [
    rule("r1", 1, "Type or Name", "Contains", "fee", "Ignored"),
    rule("r2", 2, "Type or Name", "Contains", "vat", "Room tax"),
    rule("r3", 3, "Type", "Is", "tax", "Room tax"),
  ],
  ota_tax_basis: [{ name: "b1", idx: 1, doctype: "Channel OTA Tax Basis", parent: "CMC-00001",
    parentfield: "ota_tax_basis", parenttype: "Channel Manager Connection",
    channel: "Booking.com", amounts: "Including tax" }],
}

describe("Channel Manager: OTA taxes", { timeout: 20000 }, () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    fetches.mockReset().mockImplementation(async (path: string, init?: RequestInit) => {
      if (init?.method === "PUT") return { data: { ...connection, ...JSON.parse(String(init.body)) } }
      if (path.startsWith("/api/resource/Channel%20Manager%20Connection/CMC-00001")) return { data: connection }
      if (path.startsWith("/api/resource/Channel%20Manager%20Connection?")) return { data: [
        { name: "CMC-00001", provider: "Channex", external_property_id: "cx-1", active: 1 }] }
      return { data: path.includes("?") ? [] : {} }
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

  const text = () => host.textContent ?? ""
  const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 10)))

  async function openConnection() {
    await act(async () => root.render(<MemoryRouter initialEntries={["/channel-manager"]}><App /></MemoryRouter>))
    await vi.waitFor(async () => {
      await settle()
      expect(host.querySelector("tbody tr")?.textContent).toContain("cx-1")
    }, { interval: 1, timeout: 5000 })
    await act(async () => (host.querySelector("tbody tr") as HTMLElement).click())
    await vi.waitFor(async () => {
      await settle()
      expect(table("OTA Tax Rules").querySelectorAll("tbody tr")).toHaveLength(3)
    }, { interval: 1, timeout: 5000 })
  }
  function table(label: string): HTMLFieldSetElement {
    const found = Array.from(document.querySelectorAll("fieldset"))
      .find((node) => node.querySelector("legend")?.textContent?.trim() === label)
    expect(found, `Missing table: ${label}`).toBeDefined()
    return found!
  }
  const rows = (label: string) => Array.from(table(label).querySelectorAll("tbody tr"))
  const cells = (row: Element) => Array.from(row.querySelectorAll("select, input"))
    .map((node) => (node as HTMLInputElement | HTMLSelectElement).value)
  function control(scope: ParentNode, name: string): HTMLButtonElement {
    const found = Array.from(scope.querySelectorAll("button"))
      .find((node) => node.getAttribute("aria-label") === name || node.textContent?.trim() === name)
    expect(found, `Missing button: ${name}`).toBeDefined()
    return found!
  }
  async function type(node: HTMLInputElement, value: string) {
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

  it("shows the connection's rules in order and each OTA's basis", async () => {
    await openConnection()
    expect(rows("OTA Tax Rules").map(cells)).toEqual([
      ["Type or Name", "Contains", "fee", "Ignored"],
      ["Type or Name", "Contains", "vat", "Room tax"],
      ["Type", "Is", "tax", "Room tax"],
    ])
    expect(rows("OTA Tax Basis").map(cells)).toEqual([["Booking.com", "Including tax"]])
    expect(document.body.textContent).not.toMatch(/Kamra/i)
  })

  it("saves both tables with the connection: edited, reordered, added and removed rows", async () => {
    await openConnection()
    await type(rows("OTA Tax Rules")[1].querySelector("input")!, "value added tax")
    await act(async () => control(rows("OTA Tax Rules")[1], "Move up").click())
    await act(async () => control(rows("OTA Tax Rules")[2], "Remove").click())
    await act(async () => control(table("OTA Tax Rules"), "Add row").click())
    const added = rows("OTA Tax Rules")[2]
    await choose(added.querySelectorAll("select")[0] as HTMLSelectElement, "Name")
    await choose(added.querySelectorAll("select")[1] as HTMLSelectElement, "Contains")
    await type(added.querySelector("input")!, "lodging tax")
    await choose(added.querySelectorAll("select")[2] as HTMLSelectElement, "Ignored")
    await act(async () => control(table("OTA Tax Basis"), "Add row").click())
    const basis = rows("OTA Tax Basis")[1]
    await type(basis.querySelector("input")!, "Expedia")
    await choose(basis.querySelector("select")!, "Excluding tax")

    await act(async () => control(document.body, "Save").click())
    await settle()
    const put = fetches.mock.calls.find(([, init]) => init?.method === "PUT")
    expect(put?.[0]).toBe("/api/resource/Channel%20Manager%20Connection/CMC-00001")
    const body = JSON.parse(String(put?.[1]?.body))
    expect(body.provider).toBe("Channex")
    expect(body.ota_tax_rules).toEqual([
      { name: "r2", idx: 1, match_on: "Type or Name", match_type: "Contains", match_text: "value added tax", counts_as: "Room tax" },
      { name: "r1", idx: 2, match_on: "Type or Name", match_type: "Contains", match_text: "fee", counts_as: "Ignored" },
      { idx: 3, match_on: "Name", match_type: "Contains", match_text: "lodging tax", counts_as: "Ignored" },
    ])
    expect(body.ota_tax_basis).toEqual([
      { name: "b1", idx: 1, channel: "Booking.com", amounts: "Including tax" },
      { idx: 2, channel: "Expedia", amounts: "Excluding tax" },
    ])
  })

  it("an untouched connection saves its tables as they were", async () => {
    await openConnection()
    await act(async () => control(document.body, "Save").click())
    await settle()
    const body = JSON.parse(String(fetches.mock.calls.find(([, init]) => init?.method === "PUT")?.[1]?.body))
    expect(body.ota_tax_rules).toEqual(connection.ota_tax_rules.map(({ name, idx, match_on, match_type, match_text, counts_as }) =>
      ({ name, idx, match_on, match_type, match_text, counts_as })))
    expect(body.ota_tax_basis).toEqual([{ name: "b1", idx: 1, channel: "Booking.com", amounts: "Including tax" }])
  })

  it.each([
    ["ar", ar], ["de", de], ["es", es], ["fil", fil], ["id", id], ["pt", pt], ["zh-CN", zhCN],
  ])("translates the OTA tax tables in %s", (_language, dictionary: Record<string, string>) => {
    for (const key of ["OTA Tax Rules", "OTA Tax Basis", "Match on", "Match", "Text", "Counts as", "OTA",
      "Booking amounts", "Type or Name", "Contains", "Is", "Room tax", "Ignored", "Including tax",
      "Excluding tax", "Add row", "Move up", "Move down", "Remove"])
      expect(dictionary[key], key).toBeTruthy()
  })
})
