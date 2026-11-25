// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { AuditPackView } from "../src/screens/AuditPack"
import EndOfDayWizard from "../src/screens/EndOfDayWizard"
import { adoptUiLocale } from "../src/lib/money"
import { setLang } from "../src/lib/dir"
import { ensureDictionary } from "../src/lib/locales"

const { rpc, auth } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return { rpc: vi.fn(), auth: { status: "authed", roles: ["Finance", "Hotel Admin"] } }
})

vi.mock("../src/lib/api", async (original) => ({
  ...await original<typeof import("../src/lib/api")>(),
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => "Grand Plaza",
}))
vi.mock("../src/lib/auth", () => ({ useAuth: () => auth }))
vi.mock("../src/lib/cashierAuth", () => ({ useCashierAuth: () => ({ ensure: async () => true, withPin: async (fn: (p: string) => unknown) => fn("1234") }) }))

const FORBIDDEN_VENDOR_RE = /(?<![a-z0-9])(?:Kamra|Frappe|ERPNext|URY|Tridz|BuildSuite)(?![a-z0-9])/i

function makeControls(overrides: Partial<Record<string, { subledger: number; gl: number; ple?: number; variance: number; status: string }>> = {}) {
  const base = [
    { key: "guest_ledger", label: "Guest Ledger", account: "Debtors - GP", subledger: 125.5, gl: 125.5, ple: 125.5, variance: 0, status: "Balanced" },
    { key: "guest_advance_received", label: "Guest Advance Received", account: "Guest Advance Received - GP", subledger: 40.125, gl: 40.125, ple: 40.125, variance: 0, status: "Balanced" },
    { key: "guest_ledger_clearing", label: "Guest Ledger Clearing", account: "Guest Ledger Clearing - GP", subledger: 12.375, gl: 12.375, ple: 0, variance: 0, status: "Balanced" },
    { key: "room_tax_revenue", label: "Room & Tax Revenue", account: "", subledger: 85.0, gl: 85.0, ple: 0, variance: 0, status: "Balanced" },
    { key: "city_ledger_ar", label: "City Ledger AR", account: "Debtors - GP", subledger: 210.25, gl: 210.25, ple: 210.25, variance: 0, status: "Balanced" },
  ]
  return base.map((row) => {
    const ov = overrides[row.key]
    return {
      key: row.key,
      label: row.label,
      account: row.account,
      subledger_balance: ov?.subledger ?? row.subledger,
      gl_balance: ov?.gl ?? row.gl,
      ple_balance: ov?.ple ?? row.ple,
      variance: ov?.variance ?? row.variance,
      status: ov?.status ?? row.status,
    }
  })
}

describe("P40 Subledger-to-GL Control Tie-Out UI (AuditPack & EndOfDayWizard)", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    auth.roles = ["Finance", "Hotel Admin"]
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    setLang("en")
    host = document.createElement("div")
    document.body.appendChild(host)
    root = createRoot(host)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    host.remove()
    localStorage.clear()
    setLang("en")
    document.documentElement.dir = "ltr"
    vi.unstubAllGlobals()
  })

  it("AuditPackView (TrialBalance) renders Control Tie-Out: Balanced with 5 rows formatted at 3dp KWD", async () => {
    adoptUiLocale({ currency: "KWD", currency_symbol: "KD ", locale: "en-GB", decimals: 3 })
    rpc.mockReset().mockImplementation(async (method: string) => {
      if (method === "kamra.night_audit.night_audit_pack") {
        return {
          audit: "AUDIT-2026-11-24-KWD",
          run: { name: "AUDIT-2026-11-24-KWD", status: "Completed", business_date: "2026-11-24" },
          has_pack: true,
          can_rebuild: true,
          pack: {
            property_name: "Kuwait Grand",
            currency: "KWD",
            currency_symbol: "KD ",
            business_date: "2026-11-24",
            generated_at: "2026-11-25 03:00",
            reports: [{ key: "trial_balance", title: "Trial balance" }],
            ledger_balances: { ledgers: [], total_debit: 0, total_credit: 0, in_balance: true },
            revenue_by_transaction_code: { rows: [] },
            gl_control_tieout: {
              ok: true,
              status: "Balanced",
              in_balance: true,
              property: "Kuwait Grand",
              company: "Kuwait Hospitality Co",
              currency: "KWD",
              precision: 3,
              tolerance: 0.001,
              business_date: "2026-11-24",
              total_variance: 0,
              controls: makeControls(),
            },
          },
        }
      }
      return {}
    })

    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/audit-pack/AUDIT-2026-11-24-KWD"]}>
          <AuditPackView audit="AUDIT-2026-11-24-KWD" />
        </MemoryRouter>,
      )
    })

    await vi.waitFor(() => {
      expect(host.querySelector('[data-testid="gl-control-tieout-card"]')).not.toBeNull()
    })

    const card = host.querySelector('[data-testid="gl-control-tieout-card"]')!
    expect(card.textContent).toContain("Accounting GL Control Tie-Out")
    expect(card.textContent).toContain("Control Tie-Out: Balanced")
    expect(card.textContent).toContain("Guest Ledger")
    expect(card.textContent).toContain("Guest Advance Received")
    expect(card.textContent).toContain("Guest Ledger Clearing")
    expect(card.textContent).toContain("Room & Tax Revenue")
    expect(card.textContent).toContain("City Ledger AR")
    // 3dp KWD formatting
    expect(card.textContent).toContain("125.500")
    expect(card.textContent).toContain("40.125")
    expect(card.textContent).toContain("12.375")
    expect(card.textContent).toContain("0.000")
    expect(card.textContent).toContain("KD 0.001")
    // 100% Maximus white-label
    expect(card.textContent).not.toMatch(FORBIDDEN_VENDOR_RE)
  })

  it("AuditPackView (TrialBalance) renders 0dp UGX Variance, Unverified state, and omits card when redacted", async () => {
    adoptUiLocale({ currency: "UGX", currency_symbol: "UGX ", locale: "en-GB", decimals: 0 })
    const makePackResponse = (tieout: Record<string, unknown> | null | undefined) => ({
      audit: "AUDIT-2026-11-24-UGX",
      run: { name: "AUDIT-2026-11-24-UGX", status: "Completed with Warnings", business_date: "2026-11-24" },
      has_pack: true,
      can_rebuild: true,
      pack: {
        property_name: "Kampala Grand",
        currency: "UGX",
        currency_symbol: "UGX ",
        business_date: "2026-11-24",
        generated_at: "2026-11-25 03:00",
        reports: [{ key: "trial_balance", title: "Trial balance" }],
        ledger_balances: { ledgers: [], total_debit: 0, total_credit: 0, in_balance: true },
        revenue_by_transaction_code: { rows: [] },
        ...(tieout !== undefined ? { gl_control_tieout: tieout } : {}),
      },
    })

    // 1. UGX Variance (0dp: UGX 1, never 1.00)
    rpc.mockReset().mockResolvedValue(
      makePackResponse({
        ok: true,
        status: "Variance",
        in_balance: false,
        currency: "UGX",
        precision: 0,
        tolerance: 0,
        business_date: "2026-11-24",
        total_variance: 1,
        controls: makeControls({
          guest_ledger: { subledger: 250000, gl: 250000, ple: 250000, variance: 0, status: "Balanced" },
          guest_advance_received: { subledger: 100000, gl: 100000, ple: 100000, variance: 0, status: "Balanced" },
          guest_ledger_clearing: { subledger: 50000, gl: 50001, ple: 0, variance: 1, status: "Variance" },
          room_tax_revenue: { subledger: 200000, gl: 200000, ple: 0, variance: 0, status: "Balanced" },
          city_ledger_ar: { subledger: 300000, gl: 300000, ple: 300000, variance: 0, status: "Balanced" },
        }),
      }),
    )

    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/audit-pack/UGX-VAR"]}>
          <AuditPackView key="ugx-var" audit="UGX-VAR" />
        </MemoryRouter>,
      )
    })
    await vi.waitFor(() => {
      expect(host.querySelector('[data-testid="gl-control-tieout-card"]')).not.toBeNull()
    })
    const varCard = host.querySelector('[data-testid="gl-control-tieout-card"]')!
    expect(varCard.textContent).toContain("Control Tie-Out: Variance (UGX 1)")
    expect(varCard.textContent).toContain("50,001")
    expect(varCard.textContent).not.toContain("50,001.00")
    expect(varCard.textContent).not.toMatch(FORBIDDEN_VENDOR_RE)

    // 2. Unverified state (fail-closed amber card, never Balanced)
    rpc.mockReset().mockResolvedValue(
      makePackResponse({
        ok: false,
        status: "Unverified",
        in_balance: false,
        currency: "UGX",
        precision: 0,
        tolerance: 0,
        business_date: "2026-11-24",
        total_variance: 0,
        reason: "Required control accounts are not configured on the company.",
        controls: [],
      }),
    )
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/audit-pack/UGX-UNV"]}>
          <AuditPackView key="ugx-unv" audit="UGX-UNV" />
        </MemoryRouter>,
      )
    })
    await vi.waitFor(() => {
      expect(host.textContent).toContain("Control Tie-Out: Not verified")
    })
    expect(host.textContent).toContain("Required control accounts are not configured on the company.")
    expect(host.textContent).not.toContain("Control Tie-Out: Balanced")

    // 3. Redacted / non-ERP pack (no gl_control_tieout section) -> card omitted
    rpc.mockReset().mockResolvedValue(makePackResponse(undefined))
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/audit-pack/UGX-NONE"]}>
          <AuditPackView key="ugx-none" audit="UGX-NONE" />
        </MemoryRouter>,
      )
    })
    await vi.waitFor(() => {
      expect(host.textContent).toContain("Trial balance")
    })
    expect(host.querySelector('[data-testid="gl-control-tieout-card"]')).toBeNull()
  })

  it("EndOfDayWizard renders USD variance blocker card, disables Close, and enforces >=10 char override note", async () => {
    adoptUiLocale({ currency: "USD", currency_symbol: "$", locale: "en-GB", decimals: 2 })
    const varianceControls = makeControls({
      guest_ledger: { subledger: 400, gl: 400, ple: 400, variance: 0, status: "Balanced" },
      guest_advance_received: { subledger: 150, gl: 150, ple: 150, variance: 0, status: "Balanced" },
      guest_ledger_clearing: { subledger: 80, gl: 80.05, ple: 0, variance: 0.05, status: "Variance" },
      room_tax_revenue: { subledger: 320, gl: 320, ple: 0, variance: 0, status: "Balanced" },
      city_ledger_ar: { subledger: 500, gl: 500, ple: 500, variance: 0, status: "Balanced" },
    })

    rpc.mockReset().mockImplementation(async (method: string) => {
      if (method === "kamra.night_audit.eod_session") {
        return {
          property: "Grand Plaza",
          property_name: "Grand Plaza",
          currency: "USD",
          currency_symbol: "$",
          business_date: "2026-11-24",
          server_date: "2026-11-24",
          server_time: "23:15",
          property_now: "2026-11-24 23:15",
          require_cashier_pin: 0,
          blind_count: 0,
          high_balance_threshold: 0,
          auto_audit_with_blockers: "Hold and alert",
          reports: [],
          audit: "AUDIT-2026-11-24-Grand Plaza",
          ran: false,
          status: "Draft",
          current_step: 4,
          acknowledgements: [],
          steps: [],
          sessions: [],
          checks: [
            {
              key: "gl_control_tieout",
              title: "Accounting GL control tie-out",
              severity: "block",
              count: 1,
              unverified: false,
              can_override_gl_variance: true,
              items: [varianceControls[2]],
              action: "Reconcile the control account variance in Accounting before closing the business date.",
              resolve: "/end-of-day",
              summary: {
                ok: true,
                status: "Variance",
                in_balance: false,
                property: "Grand Plaza",
                company: "Grand Plaza LLC",
                currency: "USD",
                precision: 2,
                tolerance: 0.01,
                business_date: "2026-11-24",
                total_variance: 0.05,
                controls: varianceControls,
              },
            },
          ],
        }
      }
      return {}
    })

    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/end-of-day/Grand%20Plaza"]}>
          <Routes>
            <Route path="/end-of-day/:property" element={<EndOfDayWizard />} />
          </Routes>
        </MemoryRouter>,
      )
    })

    await vi.waitFor(() => {
      expect(host.querySelector('[data-testid="gl-control-tieout-card"]')).not.toBeNull()
    })
    expect(host.textContent).toContain("Control Tie-Out: Variance ($0.05)")
    expect(host.textContent).toContain("Guest Ledger Clearing")
    expect(host.textContent).toContain("80.05")
    expect(host.textContent).not.toMatch(FORBIDDEN_VENDOR_RE)

    // Close button MUST be disabled while gl_control_tieout blocks
    const closeBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Close 24 Nov 2026"),
    )
    expect(closeBtn).toBeDefined()
    expect(closeBtn!.disabled).toBe(true)

    // Override button stays disabled for short reason (< 10 chars) and enables at >= 10 chars
    const reasonInput = host.querySelector<HTMLInputElement>('input[placeholder="Reason"]')
    const overrideBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Override with reason"),
    )
    expect(reasonInput).not.toBeNull()
    expect(overrideBtn).toBeDefined()
    expect(overrideBtn!.disabled).toBe(true)

    const setNative = (el: HTMLInputElement, val: string) => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, val)
      el.dispatchEvent(new Event("input", { bubbles: true }))
      el.dispatchEvent(new Event("change", { bubbles: true }))
    }
    await act(async () => setNative(reasonInput!, "short"))
    expect(overrideBtn!.disabled).toBe(true)

    await act(async () => setNative(reasonInput!, "Approved by Finance controller pending JV"))
    expect(overrideBtn!.disabled).toBe(false)
  })

  async function renderVarianceCheck(overrides: Record<string, unknown> = {}) {
    adoptUiLocale({ currency: "USD", currency_symbol: "$", locale: "en-GB", decimals: 2 })
    const controls = makeControls({
      guest_ledger_clearing: { subledger: 80, gl: 80.05, variance: 0.05, status: "Variance" },
    })
    rpc.mockReset().mockResolvedValue({
      property: "Grand Plaza",
      property_name: "Grand Plaza",
      currency: "USD",
      currency_symbol: "$",
      business_date: "2026-11-24",
      server_date: "2026-11-24",
      property_now: "2026-11-24 23:15",
      auto_audit_with_blockers: "Hold and alert",
      ran: false,
      acknowledgements: [],
      sessions: [],
      checks: [{
        key: "gl_control_tieout",
        title: "Accounting GL control tie-out",
        severity: "block",
        count: 1,
        unverified: false,
        can_override_gl_variance: false,
        items: [controls[2]],
        action: "Reconcile the control account variance in Accounting before closing the business date.",
        summary: {
          ok: true,
          status: "Variance",
          in_balance: false,
          currency: "USD",
          precision: 2,
          tolerance: 0.01,
          total_variance: 0.05,
          controls,
        },
        ...overrides,
      }],
    })
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/end-of-day/Grand%20Plaza"]}>
          <Routes>
            <Route path="/end-of-day/:property" element={<EndOfDayWizard />} />
          </Routes>
        </MemoryRouter>,
      )
    })
    await vi.waitFor(() => expect(host.textContent).toContain("Accounting GL control tie-out"))
  }

  it.each([
    ["Finance", "property policy disables overrides"],
    ["Night Auditor", "role is ineligible"],
  ])("EndOfDayWizard hides the override controls for %s when %s", async (role) => {
    auth.roles = [role]
    await renderVarianceCheck()
    expect(host.querySelector('input[placeholder="Reason"]')).toBeNull()
    expect(host.textContent).not.toContain("Override with reason")
    expect(host.textContent).toContain("Finance or Hotel Admin can override this variance only when property policy allows it.")
    expect(host.textContent).not.toMatch(FORBIDDEN_VENDOR_RE)
  })

  it("EndOfDayWizard shows restricted figures for a redacted verified tie-out", async () => {
    auth.roles = ["Front Desk"]
    await renderVarianceCheck({
      summary: null,
      items: [{ key: "guest_ledger_clearing", label: "Guest Ledger Clearing", status: "Variance" }],
    })
    expect(host.textContent).not.toContain("0.00")
    expect(host.textContent).not.toContain("Control Tie-Out: Variance (")
    expect(host.textContent).toContain("Accounting GL control tie-out figures are restricted.")
  })

  it("EndOfDayWizard blocks Close and hides Override when gl_control_tieout is Unverified (even count=0)", async () => {
    adoptUiLocale({ currency: "USD", currency_symbol: "$", locale: "en-GB", decimals: 2 })
    rpc.mockReset().mockImplementation(async (method: string) => {
      if (method === "kamra.night_audit.eod_session") {
        return {
          property: "Grand Plaza",
          property_name: "Grand Plaza",
          currency: "USD",
          currency_symbol: "$",
          business_date: "2026-11-24",
          server_date: "2026-11-24",
          server_time: "23:15",
          property_now: "2026-11-24 23:15",
          require_cashier_pin: 0,
          blind_count: 0,
          high_balance_threshold: 0,
          auto_audit_with_blockers: "Hold and alert",
          reports: [],
          audit: "AUDIT-2026-11-24-Grand Plaza",
          ran: false,
          status: "Draft",
          current_step: 4,
          acknowledgements: [],
          steps: [],
          sessions: [],
          checks: [
            {
              key: "gl_control_tieout",
              title: "Accounting GL control tie-out",
              severity: "block",
              count: 0,
              unverified: true,
              items: [],
              action: "Accounting GL control tie-out could not be verified. Configure control accounts and verify balances before closing.",
              resolve: "/end-of-day",
              summary: {
                ok: false,
                status: "Unverified",
                in_balance: false,
                property: "Grand Plaza",
                currency: "USD",
                precision: 2,
                tolerance: 0.01,
                business_date: "2026-11-24",
                total_variance: 0,
                reason: "Required control accounts are not configured on the company.",
                controls: [],
              },
            },
          ],
        }
      }
      return {}
    })

    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/end-of-day/Grand%20Plaza"]}>
          <Routes>
            <Route path="/end-of-day/:property" element={<EndOfDayWizard />} />
          </Routes>
        </MemoryRouter>,
      )
    })

    await vi.waitFor(() => {
      expect(host.textContent).toContain("Control Tie-Out: Not verified")
    })
    const closeBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Close 24 Nov 2026"),
    )
    expect(closeBtn).toBeDefined()
    expect(closeBtn!.disabled).toBe(true)
    const overrideBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Override with reason"),
    )
    expect(overrideBtn).toBeUndefined()
    expect(host.textContent).not.toContain("All clear")
  })

  it("EndOfDayWizard shows Balanced tie-out card translated in French (fr) and Arabic RTL (ar) with logical CSS", async () => {
    adoptUiLocale({ currency: "KWD", currency_symbol: "KD ", locale: "en-GB", decimals: 3 })
    const balancedControls = makeControls()
    rpc.mockReset().mockImplementation(async (method: string) => {
      if (method === "kamra.night_audit.eod_session") {
        return {
          property: "Grand Plaza",
          property_name: "Grand Plaza",
          currency: "KWD",
          currency_symbol: "KD ",
          business_date: "2026-11-24",
          server_date: "2026-11-24",
          server_time: "23:15",
          property_now: "2026-11-24 23:15",
          require_cashier_pin: 0,
          blind_count: 0,
          high_balance_threshold: 0,
          auto_audit_with_blockers: "Hold and alert",
          reports: [],
          audit: "AUDIT-2026-11-24-Grand Plaza",
          ran: false,
          status: "Draft",
          current_step: 4,
          acknowledgements: [],
          steps: [],
          sessions: [],
          checks: [
            {
              key: "gl_control_tieout",
              title: "Accounting GL control tie-out",
              severity: "block",
              count: 0,
              unverified: false,
              items: [],
              action: "All five accounting GL control buckets agree within currency tolerance.",
              resolve: "/end-of-day",
              summary: {
                ok: true,
                status: "Balanced",
                in_balance: true,
                property: "Grand Plaza",
                company: "Grand Plaza KWD",
                currency: "KWD",
                precision: 3,
                tolerance: 0.001,
                business_date: "2026-11-24",
                total_variance: 0,
                controls: balancedControls,
              },
            },
          ],
        }
      }
      return {}
    })

    // French (fr)
    await ensureDictionary("fr")
    setLang("fr")
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/end-of-day/Grand%20Plaza"]}>
          <Routes>
            <Route path="/end-of-day/:property" element={<EndOfDayWizard key="fr-balanced" />} />
          </Routes>
        </MemoryRouter>,
      )
    })
    await vi.waitFor(() => {
      expect(host.querySelector('[data-testid="gl-control-tieout-card"]')).not.toBeNull()
    })
    const frCard = host.querySelector('[data-testid="gl-control-tieout-card"]')!
    expect(frCard.textContent).toContain("Rapprochement de contrôle : Équilibré")
    expect(frCard.textContent).not.toMatch(FORBIDDEN_VENDOR_RE)

    // Arabic RTL (ar)
    await ensureDictionary("ar")
    setLang("ar")
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/end-of-day/Grand%20Plaza"]}>
          <Routes>
            <Route path="/end-of-day/:property" element={<EndOfDayWizard key="ar-balanced" />} />
          </Routes>
        </MemoryRouter>,
      )
    })
    await vi.waitFor(() => {
      expect(host.querySelector('[data-testid="gl-control-tieout-card"]')?.textContent).toContain("مطابقة حسابات المراقبة: متوازن")
    })
    const arCard = host.querySelector('[data-testid="gl-control-tieout-card"]')!
    expect(document.documentElement.dir).toBe("rtl")
    expect(arCard.innerHTML).not.toMatch(/\b(?:text-left|text-right|ml-|mr-|pl-|pr-)\b/)
    expect(arCard.textContent).not.toMatch(FORBIDDEN_VENDOR_RE)
  })
})
