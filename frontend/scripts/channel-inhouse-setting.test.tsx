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

/** Channel Manager screen: OPERA Cloud OXI UPDATE INHOUSE RES setting
 *  (defaults to N, sends selected value on Save, surfaces server refusal for Y)
 *  and OTA Tax Rules precedence guidance (specific/Ignored rules above broader
 *  Room tax rules). */

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

const connection = {
  name: "CMC-00001", doctype: "Channel Manager Connection", property: "Test Hotel", provider: "Channex",
  external_property_id: "cx-1", active: 1, sync_days: 365, update_inhouse_res: "N",
  ota_tax_rules: [],
  ota_tax_basis: [],
}

describe("Channel Manager: update_inhouse_res & tax precedence", { timeout: 20000 }, () => {
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
      expect(document.body.textContent).toContain("Update in-house reservations")
    }, { interval: 1, timeout: 5000 })
  }

  function fieldSelect(labelText: string): HTMLSelectElement {
    const label = Array.from(document.querySelectorAll("label"))
      .find((node) => node.textContent?.includes(labelText))
    expect(label, `Missing field label: ${labelText}`).toBeDefined()
    const select = label!.querySelector("select")
    expect(select, `Missing select for: ${labelText}`).not.toBeNull()
    return select!
  }

  function button(name: string): HTMLButtonElement {
    const found = Array.from(document.querySelectorAll("button"))
      .find((node) => node.textContent?.trim() === name)
    expect(found, `Missing button: ${name}`).toBeDefined()
    return found!
  }

  it("shows Update in-house reservations defaulted to N, sends it on Save, and shows precedence hint", async () => {
    await openConnection()
    const sel = fieldSelect("Update in-house reservations")
    expect(sel.value).toBe("N")
    expect(Array.from(sel.options).map((o) => o.value)).toEqual(["", "N", "Y"])
    expect(document.body.textContent).toContain("Put specific or Ignored rules")
    expect(document.body.textContent).not.toMatch(/Kamra/i)

    await act(async () => {
      sel.value = "Y"
      sel.dispatchEvent(new Event("change", { bubbles: true }))
    })
    await act(async () => button("Save").click())
    await settle()
    const put = fetches.mock.calls.find(([, init]) => init?.method === "PUT")
    expect(put?.[0]).toBe("/api/resource/Channel%20Manager%20Connection/CMC-00001")
    expect(JSON.parse(String(put?.[1]?.body)).update_inhouse_res).toBe("Y")
  })

  it.each([
    ["ar", ar], ["de", de], ["es", es], ["fil", fil], ["id", id], ["pt", pt], ["zh-CN", zhCN],
  ])("translates the in-house update setting and precedence hint in %s", (_language, dictionary: Record<string, string>) => {
    for (const key of [
      "Update in-house reservations",
      "OXI UPDATE INHOUSE RES: N rejects OTA changes to checked-in stays and flags them for review. Y requires arrival-date, room-type, billed-night, and assigned-room bounds and is not yet enabled.",
      "Which of the taxes an OTA lists on a booking is the room tax, tried top to bottom: the first rule that matches decides. Put specific or Ignored rules (such as city or tourism taxes) above broader Room tax rules. A tax on top of the price that no rule reads holds the booking for review. A new Channex connection, or one switched to Channex, starts with the standard rules.",
      "N",
      "Y",
    ]) {
      expect(dictionary[key], key).toBeTruthy()
    }
  })
})
