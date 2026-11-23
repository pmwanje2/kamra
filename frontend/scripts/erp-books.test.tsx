// @vitest-environment jsdom
// Screens whose books the site's ERP keeps: hidden from navigation and,
// opened by URL, pointed at the ERP screen. The server refuses their
// endpoints (kamra.erp_books); window.kamra_erp_screens is what it boots.
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import App from "../src/App"
import { APPS, visibleApps } from "../src/lib/apps"
import { keptInErp } from "../src/lib/erpBooks"

const { rpc } = vi.hoisted(() => ({ rpc: vi.fn() }))
vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => method === "kamra.api.property_locale"
    ? Promise.resolve({ locale: "en", currency: "UGX" }) : rpc(method, params),
  getCurrentProperty: () => "Test Hotel",
  frappeFetch: async (path: string) => ({ data: path.includes("?") ? [] : {} }),
  serverError: (error: Error) => error.message,
}))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: ["Finance"] }) }))
vi.mock("../src/AppShell", () => ({ default: () => <Outlet /> }))

const PAYABLES = { label: "Purchase Invoice", url: "/app/purchase-invoice" }
const PNL = { label: "Profit and Loss Statement", url: "/app/query-report/Profit%20and%20Loss%20Statement" }
const RECEIVABLES = { label: "Accounts Receivable", url: "/app/query-report/Accounts%20Receivable" }

type Screens = Record<string, { label: string; url: string }>
const boot = (screens?: Screens) => {
  if (screens) window.kamra_erp_screens = screens
  else delete window.kamra_erp_screens
}
const finance = () => visibleApps(["Finance"]).find((a) => a.id === "finance")!
const routes = () => finance().items.map((i) => i.to)

describe("ERP-kept books", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    rpc.mockReset().mockImplementation(async (method: string) => {
      if (method === "kamra.payables.vendor_bills") return { bills: [] }
      if (method === "kamra.payables.vendors") return { vendors: [], categories: [] }
      if (method === "kamra.payables.payables_aging")
        return { totals: { not_due: 0, "0_30": 0, "31_60": 0, "61_90": 0, "90_plus": 0, total: 0 } }
      return null
    })
    host = document.createElement("div")
    document.body.append(host)
    root = createRoot(host)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    host.remove()
    boot()
    vi.unstubAllGlobals()
  })

  async function render(path: string) {
    await act(async () => root.render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>))
    await vi.waitFor(async () => {
      await act(() => new Promise((resolve) => setTimeout(resolve, 10)))
      expect(host.textContent).not.toMatch(/^Loading…$/)
    }, { interval: 1 })
  }

  it("keeps every books screen in the navigation when the site declares none", () => {
    boot()
    expect(routes()).toEqual(expect.arrayContaining(
      ["/receivables", "/payables", "/cash-forecast", "/profit-and-loss", "/accounting-export"]))
    expect(keptInErp("/payables")).toBeUndefined()
  })

  it("hides only the screens the ERP keeps", () => {
    boot({ payables: PAYABLES, profit_and_loss: PNL, receivables: RECEIVABLES })
    expect(routes()).not.toContain("/payables")
    expect(routes()).not.toContain("/profit-and-loss")
    expect(routes()).not.toContain("/receivables")
    expect(routes()).toEqual(expect.arrayContaining(["/cash-forecast", "/accounting-export", "/folio-history"]))
    // the static registry is untouched: another site's boot shows them again
    expect(APPS.find((a) => a.id === "finance")!.items.map((i) => i.to)).toContain("/payables")
  })

  it("maps each route and its detail pages to the screen key", () => {
    boot({ receivables: RECEIVABLES, balance_sheet: PNL, accounting_export: PAYABLES, cash_forecast: PNL })
    expect(keptInErp("/receivables/CLA-0001")).toEqual(RECEIVABLES)
    expect(keptInErp("/Receivables/")).toEqual(RECEIVABLES)
    expect(keptInErp("/balance-sheet")).toEqual(PNL)
    expect(keptInErp("/accounting-export")).toEqual(PAYABLES)
    expect(keptInErp("/cash-forecast")).toEqual(PNL)
    expect(keptInErp("/receivables-report")).toBeUndefined()
    expect(keptInErp("/folio-history")).toBeUndefined()
  })

  it("points a kept screen opened by URL to the ERP and never loads its books", async () => {
    boot({ payables: PAYABLES })
    await render("/payables")
    expect(host.textContent).toContain("This is kept in the accounting system.")
    const link = Array.from(host.querySelectorAll("a")).find((a) => a.textContent === "Open Purchase Invoice")
    expect(link?.getAttribute("href")).toBe("/app/purchase-invoice")
    expect(rpc.mock.calls.filter(([method]) => String(method).startsWith("kamra.payables."))).toEqual([])
    expect(host.textContent).not.toMatch(/ERPNext|Frappe|Kamra/i)
  })

  it("opens the screen as before on a site that keeps its own books", async () => {
    boot()
    await render("/payables")
    expect(host.textContent).not.toContain("This is kept in the accounting system.")
    expect(rpc).toHaveBeenCalledWith("kamra.payables.vendor_bills", expect.objectContaining({ property: "Test Hotel" }))
  })
})
