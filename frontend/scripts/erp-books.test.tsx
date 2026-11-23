// @vitest-environment jsdom
// Screens whose books the site's ERP keeps: hidden from navigation and,
// opened by URL, pointed at the ERP screen. The server refuses their
// endpoints (kamra.erp_books); window.kamra_erp_screens is what it boots.
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import App from "../src/App"
import CheckOutDialog from "../src/components/CheckOutDialog"
import { APPS, visibleApps } from "../src/lib/apps"
import { keptInErp } from "../src/lib/erpBooks"
import Ledgers from "../src/screens/Ledgers"
import Owner from "../src/screens/Owner"
import Reports from "../src/screens/Reports"

const { rpc } = vi.hoisted(() => ({ rpc: vi.fn() }))
vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => method === "kamra.api.property_locale"
    ? Promise.resolve({ locale: "en", currency: "UGX" }) : rpc(method, params),
  getCurrentProperty: () => "Test Hotel",
  myProperties: () => Promise.resolve([]),
  setCurrentProperty: () => undefined,
  newIdempotencyKey: () => "idem-1",
  frappeFetch: async (path: string) => ({ data: path.includes("?") ? [] : {} }),
  serverError: (error: Error) => error.message,
}))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: ["Finance", "Front Desk", "Night Auditor"] }) }))
vi.mock("../src/lib/cashierAuth", () => ({
  useCashierAuth: () => ({ ensureUnlocked: async () => undefined, withPin: async (_p: string, fn: () => Promise<unknown>) => fn() }),
}))
vi.mock("../src/lib/realtime", () => ({ useRealtime: () => undefined }))
vi.mock("../src/AppShell", () => ({ default: () => <Outlet /> }))

const PAYABLES = { label: "Purchase Invoice", url: "/app/purchase-invoice" }
const PNL = { label: "Profit and Loss Statement", url: "/app/query-report/Profit%20and%20Loss%20Statement" }
const RECEIVABLES = { label: "Accounts Receivable", url: "/app/query-report/Accounts%20Receivable" }
const EXPORT = { label: "Sales Register", url: "/app/query-report/Sales%20Register" }

type Screens = Record<string, { label: string; url: string }>
const boot = (screens?: Screens, posUrl?: string | null) => {
  if (screens) window.kamra_erp_screens = screens
  else delete window.kamra_erp_screens
  if (posUrl !== undefined) (window as Window & { kamra_erp_pos_url?: string | null }).kamra_erp_pos_url = posUrl
  else delete (window as Window & { kamra_erp_pos_url?: string | null }).kamra_erp_pos_url
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

  it("deduplicates CheckOutDialog blocker banners and uses outlet wording for external checks", async () => {
    const blockerMsg = "1 bill(s) for this stay are still open at the outlet: Main Dining ACC-PSINV-1 (Open). Settle them at the outlet before check-out."
    rpc.mockImplementation(async (method: string) => {
      if (method === "kamra.api.checkout_context") {
        return {
          reservation: {
            name: "RES-1", status: "Checked In", guest: "G-1", guest_name: "Priya Sharma",
            company: null, company_name: null, group_booking: null,
            room: "Test Hotel-101", room_number: "101", room_type_name: "DLX",
            check_in_date: "2026-11-23", check_out_date: "2026-11-24",
          },
          currency_symbol: "₹", payment_modes: ["Cash"], pin_required: false,
          till_open: true, till: { open: true, required: false, can_open: false, who_can_open: [] },
          folios: [], groups: {},
          totals: { charges: 0, paid: 0, balance: 0, owed: 0, refund_due: 0, to_city_ledger: 0 },
          billing: { company: null, company_name: null, rules: [] },
          city_ledger: [], credit: null, deposit: null, collect: [], refunds: [],
          pending_bills: [{
            kind: "outlet", name: "ACC-PSINV-1", label: "Main Dining", status: "Open", amount: 450,
            complimentary: false, can_act: true, can_post_to_room: false, can_mark_ready: false,
            billable: true, pay_modes: [], act_roles: [],
            hint: "Still open at the outlet. Settle it there before check-out: charge it to the room, take payment or void it.",
          }],
          late_checkout: null, early_departure: null, companions: [],
          blockers: [{ code: "pending_bills", step: "review", message: blockerMsg }],
          notices: [], settled: true,
          actions: { can_checkout: false, can_withhold_deposit: false, can_check_out_role: true },
          sop: { steps: [], next_tip: blockerMsg },
        }
      }
      return null
    })
    await act(async () => {
      root.render(
        <MemoryRouter>
          <CheckOutDialog reservation="RES-1" onDone={() => undefined} onClose={() => undefined} />
        </MemoryRouter>,
      )
    })
    await vi.waitFor(() => {
      expect(host.textContent).toContain("Main Dining")
    })
    // Review step: blocker text must appear ONCE (not duplicated in both amber tip and red alert).
    expect(host.textContent?.split(blockerMsg).length).toBe(2)

    // Jump to Check out (confirm) step via step header button.
    const confirmTab = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.includes("Check out"))
    expect(confirmTab).toBeTruthy()
    await act(async () => { confirmTab!.click() })
    expect(host.textContent?.split(blockerMsg).length).toBe(2)
    expect(host.textContent).not.toContain("Go back to the Bill step and post them to the room")
    expect(host.textContent).toContain("Settle them at the outlet")
  })

  it("points EndOfDayWizard Open POS and Receivables links at site-local external routes when bridged", async () => {
    boot({ receivables: RECEIVABLES }, "/pos")
    rpc.mockImplementation(async (method: string) => {
      if (method === "kamra.night_audit.eod_session") {
        return {
          property: "Test Hotel", property_name: "Test Hotel", business_date: "2026-11-23",
          server_date: "2026-11-24", server_time: "02:30", property_now: "02:30",
          require_cashier_pin: 0, blind_count: 0, high_balance_threshold: 0,
          auto_audit_with_blockers: "warn", reports: [], audit: "NA-1", ran: false, status: "Draft",
          current_step: 0, notes: "", acknowledgements: [], sessions: [],
          steps: [{ index: 0, key: "review", title: "Review", checks: ["open_pos_bills", "credit_limit"], status: "blocked", count: 2, blocking: ["open_pos_bills"] }],
          checks: [
            {
              key: "open_pos_bills", title: "Open restaurant bills", severity: "block", count: 1,
              action: "Settle each bill at its outlet.", resolve: "/pos", amount: 450,
              items: [{ order: "ACC-PSINV-1", outlet: "Main Dining", table_no: "IRD-101", room: "101", state: "open", amount: 450 }],
            },
            {
              key: "credit_limit", title: "Companies over their credit limit", severity: "warn", count: 1,
              action: "Ask the company for a payment.", resolve: "/receivables",
              items: [{ company: "Acme Corp", credit_limit: 1000, ar_balance: 1200, in_house: 0, exposure: 1200, over_by: 200 }],
            },
          ],
        }
      }
      return null
    })
    await act(async () => {
      root.render(
        <MemoryRouter basename="/kamra" initialEntries={["/kamra/end-of-day/Test%20Hotel"]}>
          <App />
        </MemoryRouter>,
      )
    })
    await vi.waitFor(() => {
      expect(host.textContent).toContain("Open restaurant bills")
    })
    const hrefs = Array.from(host.querySelectorAll("a")).map((a) => ({
      text: a.textContent?.trim() ?? "",
      href: a.getAttribute("href"),
    }))
    const openPos = hrefs.find((l) => l.text.includes("Open POS"))
    expect(openPos?.href).toBe("/pos")
    expect(hrefs.some((l) => l.href === "/kamra/pos")).toBe(false)
    expect(hrefs.some((l) => l.href === "/kamra/receivables")).toBe(false)
    expect(hrefs.some((l) => l.href === RECEIVABLES.url)).toBe(true)
  })

  it("replaces residual /receivables and /accounting-export links in Ledgers, Reports and Owner when bridged", async () => {
    boot({ receivables: RECEIVABLES, accounting_export: EXPORT })
    rpc.mockImplementation(async (method: string) => {
      if (method === "kamra.ledger.ledger_balances") {
        return {
          business_date: "2026-11-23", in_balance: true, reconciliation: null,
          ledgers: [{ ledger: "AR", debit: 400, credit: 0, balance: 400 }],
        }
      }
      if (method === "kamra.ledger.journal_by_transaction_code") return { rows: [] }
      if (method === "kamra.custom_reports.saved_reports") return { reports: [] }
      if (method === "kamra.custom_reports.report_datasets") return { datasets: [] }
      if (method === "kamra.dashboards.owner_snapshot") {
        return {
          property_name: "Test Hotel", date: "2026-11-23", total_rooms: 10,
          today: { rooms_sold: 5, occupancy_pct: 50, adr: 100, revpar: 50, room_revenue: 500, fnb_revenue: 0, other_revenue: 0, allowances: 0, total_revenue: 500 },
          movement: { arrivals: 1, departures: 1, in_house: 5, no_shows: 0 },
          collections: { grand_total: 500, modes: [] },
          outlook: [], guest_balances: 0, open_folios: 0,
          city_ledger: 400, city_ledger_overdue: 100, audits: null,
        }
      }
      return null
    })

    // Ledgers screen: bottom link and AR help link point at ERP Accounts Receivable, never /kamra/receivables.
    await act(async () => {
      root.render(<MemoryRouter basename="/kamra" initialEntries={["/kamra/ledgers"]}><Ledgers /></MemoryRouter>)
    })
    await vi.waitFor(() => { expect(host.textContent).toContain("Books check") })
    const ledgersHrefs = Array.from(host.querySelectorAll("a")).map((a) => a.getAttribute("href"))
    expect(ledgersHrefs).not.toContain("/kamra/receivables")
    expect(ledgersHrefs).toContain(RECEIVABLES.url)

    // Reports screen: hides /accounting-export link when kept in ERP.
    await act(async () => {
      root.render(<MemoryRouter basename="/kamra" initialEntries={["/kamra/reports/custom"]}><Reports /></MemoryRouter>)
    })
    await vi.waitFor(() => { expect(host.textContent).toContain("Reports") })
    const reportsHrefs = Array.from(host.querySelectorAll("a")).map((a) => a.getAttribute("href"))
    expect(reportsHrefs).not.toContain("/kamra/accounting-export")

    // Owner view: Company accounts row links to ERP Accounts Receivable.
    await act(async () => {
      root.render(<MemoryRouter basename="/kamra" initialEntries={["/kamra/owner"]}><Owner /></MemoryRouter>)
    })
    await vi.waitFor(() => { expect(host.textContent).toContain("Company accounts (city ledger)") })
    const ownerHrefs = Array.from(host.querySelectorAll("a")).map((a) => a.getAttribute("href"))
    expect(ownerHrefs).toContain(RECEIVABLES.url)
  })
})
