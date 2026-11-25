// @vitest-environment jsdom
// R2 RED tests for PR #57:
// - P1 #1: rate_plan_required=1 default BAR plan enables #bar-rate-input (UGX/USD/KWD)
//          while keeping derived plans, contract tariffs, non-default plans, and customer views read-only for BAR
// - P2-B: Desktop Shift+Click multi-date range selection in RateCalendar.tsx
// - P2-C: Mobile (< md) 14-day parity toggle & 1/day full-push confirmation guard in ChannelHealth.tsx
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import App from "../src/App"

const FORBIDDEN = /(?<![a-z0-9])(?:Kamra|Frappe|ERPNext|URY)(?![a-z0-9])|kamra-mark|kamrapms\.com|github\.com\/Kamra|HeyKoala/i

const { rpc, list, authState } = vi.hoisted(() => ({
  rpc: vi.fn(),
  list: vi.fn(),
  authState: { roles: ["Revenue Manager"] as string[] },
}))

vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => {
    if (method === "kamra.api.property_locale") {
      return Promise.resolve({ locale: "en-US", currency: "UGX", currency_symbol: "UGX " })
    }
    return rpc(method, params)
  },
  getCurrentProperty: () => "Kampala Grand",
  frappeFetch: async () => ({ data: [] }),
  serverError: (error: Error) => error.message,
}))

vi.mock("../src/lib/resource", async (original) => ({
  ...await original<typeof import("../src/lib/resource")>(),
  listResource: list,
}))

vi.mock("../src/lib/auth", () => ({
  useAuth: () => ({ status: "authed", roles: authState.roles }),
}))

vi.mock("../src/AppShell", () => ({ default: () => <Outlet /> }))

const setInput = (el: HTMLInputElement, value: string) => {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
  setter.call(el, value)
  el.dispatchEvent(new Event("input", { bubbles: true }))
  el.dispatchEvent(new Event("change", { bubbles: true }))
}

function makeRequiredPlanCalendar(opts: {
  currency: string
  rates: number[]
  selectedPlan?: {
    name: string
    label: string
    code: string | null
    currency: string | null
    fx_lock_policy: string
    rounding_step: number
    rounding_mode: string
    base_rate_plan: string | null
    is_tariff?: boolean
  } | null
  defaultPlan?: string | null
  company?: string | null
  contractNight?: boolean
}) {
  const barPlan = {
    name: "PROP-BAR",
    label: "Best Available Rate",
    code: "BAR",
    currency: null,
    fx_lock_policy: "Fixed at Reservation",
    rounding_step: 0,
    rounding_mode: "Nearest",
    base_rate_plan: null,
    is_tariff: false,
  }
  const activePlan = opts.selectedPlan === undefined ? barPlan : opts.selectedPlan
  const defPlan = opts.defaultPlan === undefined ? "PROP-BAR" : opts.defaultPlan
  return {
    from: "2026-12-01",
    days: 3,
    tax_inclusive: false,
    adults: 2,
    can_edit: true,
    rate_plan_required: true,
    default_plan: defPlan,
    rate_plans: activePlan && activePlan.name !== barPlan.name ? [barPlan, activePlan] : [barPlan],
    property_currency: opts.currency,
    rate_plan: activePlan,
    company: opts.company ?? null,
    room_types: [
      {
        room_type: "RT-STD",
        code: "STD",
        name: "Standard",
        base_price: 0,
        room_count: 5,
        sellable: true,
        nights: [
          {
            date: "2026-12-01",
            bar: opts.rates[0],
            card_rate: opts.rates[0],
            after_season: opts.rates[0],
            rate: opts.rates[0],
            source: opts.contractNight ? "contract" : "card",
            contract: opts.contractNight ? { currency: "USD", nightly: 120, fx_rate: 3700 } : null,
            available: true,
            season: null,
            demand: null,
            package: null,
            occupancy: 40,
          },
          {
            date: "2026-12-02",
            bar: opts.rates[1],
            card_rate: opts.rates[1],
            after_season: opts.rates[1],
            rate: opts.rates[1],
            source: opts.contractNight ? "contract" : "card",
            contract: opts.contractNight ? { currency: "USD", nightly: 120, fx_rate: 3700 } : null,
            available: true,
            season: null,
            demand: null,
            package: null,
            occupancy: 35,
          },
          {
            date: "2026-12-03",
            bar: opts.rates[2],
            card_rate: opts.rates[2],
            after_season: opts.rates[2],
            rate: opts.rates[2],
            source: opts.contractNight ? "contract" : "card",
            contract: opts.contractNight ? { currency: "USD", nightly: 120, fx_rate: 3700 } : null,
            available: true,
            season: null,
            demand: null,
            package: null,
            occupancy: 20,
          },
        ],
      },
    ],
  }
}

function makeHealthSnapshot() {
  return {
    property: "Kampala Grand",
    property_currency: "KWD",
    checked_at: "2026-12-01 09:00:00",
    can_manage: true,
    connections: [
      {
        name: "CMC-00001",
        provider: "Channex",
        active: 1,
        external_property_id: "cx-kampala-01",
        uses_outbox: true,
        sync_days: 90,
        ari_calls_per_minute: 10,
        ari_batch_seconds: 30,
        last_push: "2026-12-01 08:55:00",
        last_push_status: "OK",
        last_full_sync: "2026-12-01 06:00:00",
        health_status: "Online",
        paused: {},
        outbox_counts: { pending: 0, sending: 0, sent: 14, failed: 0 },
        mappings_count: 1,
      },
    ],
    outbox: [],
    parity: [
      {
        mapping_name: "CRM-00001",
        connection: "CMC-00001",
        provider: "Channex",
        room_type: "RT-STD",
        room_type_name: "Standard Suite",
        rate_plan: "PROP-BAR",
        rate_plan_label: "Best Available Rate",
        external_room_id: "rt-std",
        external_rate_id: "rp-std-kwd",
        external_currency: "KWD",
        currency: "KWD",
        direct_rate: 45.125,
        ota_rate: 40.0,
        parity_delta: -5.125,
        parity_status: "Undercut",
        last_synced_at: "2026-12-01 08:55:00",
        last_error: "",
        restrictions_synced: {
          stop_sell: true,
          min_stay_arrival: 2,
          max_stay: 7,
          closed_to_arrival: true,
          closed_to_departure: false,
          active_days: 2,
        },
        days: [
          {
            date: "2026-12-01",
            direct_rate: 45.125,
            ota_rate: 40.0,
            delta: -5.125,
            status: "Undercut",
            stop_sell: true,
            min_los: 2,
            max_los: 7,
            cta: true,
            ctd: false,
          },
          {
            date: "2026-12-02",
            direct_rate: 46.25,
            ota_rate: 46.25,
            delta: 0,
            status: "OK",
            stop_sell: false,
            min_los: 0,
            max_los: 0,
            cta: false,
            ctd: false,
          },
        ],
      },
    ],
    summary: {
      connections_total: 1,
      connections_active: 1,
      outbox_pending: 0,
      outbox_failed: 0,
      mappings_total: 1,
      parity_ok: 0,
      parity_issues: 1,
      unmapped_room_types: [],
    },
  }
}

describe("PR #57 R2: RateCalendar BAR under rate_plan_required, Shift+Click, and ChannelHealth mobile/confirm guard", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    authState.roles = ["Revenue Manager"]
    list.mockReset().mockResolvedValue([])
    rpc.mockReset()
    host = document.createElement("div")
    document.body.append(host)
    root = createRoot(host)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    host.remove()
    vi.unstubAllGlobals()
  })

  const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 10)))

  async function renderRoute(path: string, waitForSelector: string) {
    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={[path]}>
          <App />
        </MemoryRouter>,
      ),
    )
    await vi.waitFor(async () => {
      await settle()
      expect(host.querySelector(waitForSelector)).not.toBeNull()
    }, { interval: 1, timeout: 3000 })
    await settle()
  }

  async function openDrawerByDrag() {
    const toggle = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Edit rates & restrictions"),
    )!
    expect(toggle).toBeDefined()
    await act(async () => toggle.click())
    await settle()

    const c1 = host.querySelector<HTMLElement>('[data-cell-rt="RT-STD"][data-cell-date="2026-12-01"]')!
    const c2 = host.querySelector<HTMLElement>('[data-cell-rt="RT-STD"][data-cell-date="2026-12-02"]')!
    await act(async () => {
      c1.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true }))
      c2.dispatchEvent(new MouseEvent("pointerover", { bubbles: true }))
      c2.dispatchEvent(new MouseEvent("pointerenter", { bubbles: true }))
      window.dispatchEvent(new MouseEvent("pointerup", { bubbles: true }))
    })
    await settle()
  }

  it.each([
    ["UGX", [250000, 260000, 270000], "1", "350000", 350000],
    ["USD", [120.0, 125.0, 130.0], "0.01", "149.50", 149.5],
    ["KWD", [40.0, 42.5, 45.0], "0.001", "48.375", 48.375],
  ] as const)(
    "[P1 #1] enables #bar-rate-input and saves %s BAR when rate_plan_required=1 on default BAR plan",
    async (currency, rates, expectedStep, typedRate, expectedRate) => {
      rpc.mockImplementation((method: string) => {
        if (method === "kamra.rates.rate_calendar") {
          return Promise.resolve(makeRequiredPlanCalendar({ currency, rates: [...rates] }))
        }
        if (method === "kamra.restrictions.bulk_grid_update") {
          return Promise.resolve({
            ok: true,
            property: "Kampala Grand",
            rates_updated: 2,
            restrictions_updated: 0,
            restrictions_deleted: 0,
            ari_queued: ["OUT-R2-1"],
          })
        }
        return Promise.reject(new Error(`Unexpected: ${method}`))
      })

      await renderRoute("/rate-calendar", "table")
      await openDrawerByDrag()

      const barInput = host.querySelector<HTMLInputElement>("#bar-rate-input")
      expect(barInput, `#bar-rate-input missing for ${currency} with rate_plan_required=1`).not.toBeNull()
      expect(barInput!.step).toBe(expectedStep)

      await act(async () => {
        setInput(barInput!, typedRate)
      })
      await settle()

      const saveBtn = Array.from(host.querySelectorAll("button")).find((b) =>
        b.textContent?.includes("Save & sync OTAs"),
      )!
      await act(async () => saveBtn.click())
      await settle()

      const saveCall = rpc.mock.calls.find(([m]) => m === "kamra.restrictions.bulk_grid_update")
      expect(saveCall, "bulk_grid_update was not called").toBeDefined()
      const payload = saveCall![1] as Record<string, unknown>
      expect(payload.rate_updates).toEqual([
        expect.objectContaining({
          room_type: "RT-STD",
          start_date: "2026-12-01",
          end_date: "2026-12-02",
          rate: expectedRate,
        }),
      ])
      expect(FORBIDDEN.test(host.textContent ?? "")).toBe(false)
    },
  )

  it("[P1 #1] keeps #bar-rate-input hidden for derived, contract-tariff, non-default, and customer-filtered plans", async () => {
    const cases: { label: string; route: string; calendar: ReturnType<typeof makeRequiredPlanCalendar> }[] = [
      {
        label: "derived plan",
        route: "/rate-calendar?rate_plan=PROP-DERIVED",
        calendar: makeRequiredPlanCalendar({
          currency: "USD",
          rates: [100, 100, 100],
          defaultPlan: "PROP-DERIVED",
          selectedPlan: {
            name: "PROP-DERIVED",
            label: "Derived BAR",
            code: "DBAR",
            currency: null,
            fx_lock_policy: "Fixed at Reservation",
            rounding_step: 0,
            rounding_mode: "Nearest",
            base_rate_plan: "PROP-BAR",
            is_tariff: false,
          },
        }),
      },
      {
        label: "contract tariff plan",
        route: "/rate-calendar?rate_plan=PROP-TARIFF",
        calendar: makeRequiredPlanCalendar({
          currency: "UGX",
          rates: [444000, 444000, 444000],
          defaultPlan: "PROP-TARIFF",
          contractNight: true,
          selectedPlan: {
            name: "PROP-TARIFF",
            label: "Contract USD",
            code: "CUSD",
            currency: "USD",
            fx_lock_policy: "Fixed at Check In",
            rounding_step: 1000,
            rounding_mode: "Nearest",
            base_rate_plan: null,
            is_tariff: true,
          },
        }),
      },
      {
        label: "non-default plan",
        route: "/rate-calendar?rate_plan=PROP-CORP",
        calendar: makeRequiredPlanCalendar({
          currency: "USD",
          rates: [90, 90, 90],
          defaultPlan: "PROP-BAR",
          selectedPlan: {
            name: "PROP-CORP",
            label: "Corporate -10%",
            code: "CORP",
            currency: null,
            fx_lock_policy: "Fixed at Reservation",
            rounding_step: 0,
            rounding_mode: "Nearest",
            base_rate_plan: null,
            is_tariff: false,
          },
        }),
      },
      {
        label: "customer filtered",
        route: "/rate-calendar?company=ACME",
        calendar: makeRequiredPlanCalendar({
          currency: "USD",
          rates: [120, 120, 120],
          company: "ACME",
        }),
      },
    ]

    for (const c of cases) {
      rpc.mockReset().mockImplementation((method: string) => {
        if (method === "kamra.rates.rate_calendar") return Promise.resolve(c.calendar)
        return Promise.reject(new Error(`Unexpected: ${method}`))
      })
      await act(async () => root.unmount())
      root = createRoot(host)
      await renderRoute(c.route, "table")
      await openDrawerByDrag()

      expect(
        host.querySelector("#bar-rate-input"),
        `#bar-rate-input must stay hidden for ${c.label}`,
      ).toBeNull()
    }
  })

  it("[P2-B] preserves desktop Shift+Click range selection without prematurely resetting or opening drawer", async () => {
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.rates.rate_calendar") {
        return Promise.resolve(makeRequiredPlanCalendar({ currency: "KWD", rates: [40, 42.5, 45] }))
      }
      return Promise.reject(new Error(`Unexpected: ${method}`))
    })
    await renderRoute("/rate-calendar", "table")

    const toggle = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Edit rates & restrictions"),
    )!
    await act(async () => toggle.click())
    await settle()

    const c1 = host.querySelector<HTMLElement>('[data-cell-rt="RT-STD"][data-cell-date="2026-12-01"]')!
    const c3 = host.querySelector<HTMLElement>('[data-cell-rt="RT-STD"][data-cell-date="2026-12-03"]')!

    // Single click on cell 1 (realistic pointerdown -> pointerup -> click)
    await act(async () => {
      c1.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, shiftKey: false }))
      window.dispatchEvent(new MouseEvent("pointerup", { bubbles: true, shiftKey: false }))
      c1.dispatchEvent(new MouseEvent("click", { bubbles: true, shiftKey: false }))
    })
    await settle()

    // Drawer must NOT have prematurely opened before Shift+Click range extension
    expect(host.querySelector("#bar-rate-input")).toBeNull()
    expect(host.textContent).toContain("2026-12-01 to 2026-12-01 (1 night(s) selected)")

    // Shift+Click on cell 3 (realistic pointerdown(shift) -> pointerup(shift) -> click(shift))
    await act(async () => {
      c3.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, shiftKey: true }))
      window.dispatchEvent(new MouseEvent("pointerup", { bubbles: true, shiftKey: true }))
      c3.dispatchEvent(new MouseEvent("click", { bubbles: true, shiftKey: true }))
    })
    await settle()

    expect(host.textContent).toContain("2026-12-01 to 2026-12-03 (3 night(s) selected)")

    // Clicking "Edit selected" opens the drawer for the full 3-night range
    const editSelected = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Edit selected"),
    )!
    expect(editSelected).toBeDefined()
    await act(async () => editSelected.click())
    await settle()
    expect(host.querySelector("#bar-rate-input")).not.toBeNull()
    expect(host.textContent).toContain("2026-12-01 → 2026-12-03 (3)")
  })

  it("[P2-C] exposes mobile 14-day parity toggle and respects 1/day full-push confirmation guard on ChannelHealth", async () => {
    rpc.mockImplementation((method: string, params: Record<string, unknown>) => {
      if (method === "kamra.restrictions.channel_health_snapshot") {
        return Promise.resolve(makeHealthSnapshot())
      }
      if (method === "kamra.channel_ari.request_full_sync") {
        if (params.confirm === 0) {
          return Promise.resolve({
            queued: false,
            needs_confirm: true,
            last_full_sync: "2026-12-01 06:00:00",
          })
        }
        return Promise.resolve({ queued: true })
      }
      return Promise.reject(new Error(`Unexpected: ${method}`))
    })

    await renderRoute("/channel-health", "h1")
    await vi.waitFor(async () => {
      await settle()
      expect(host.textContent).toContain("Standard Suite")
    }, { interval: 1, timeout: 3000 })

    // 1. Mobile (< md) parity card exposes an expandable 14-day breakdown toggle
    const mobileList = host.querySelector<HTMLElement>(".md\\:hidden")
    expect(mobileList, "Mobile parity card list missing").not.toBeNull()
    const mobileToggle = mobileList!.querySelector<HTMLButtonElement>("button[aria-expanded]")
    expect(mobileToggle, "Mobile 14-day parity toggle button missing").not.toBeNull()
    expect(mobileToggle!.getAttribute("aria-expanded")).toBe("false")

    await act(async () => mobileToggle!.click())
    await settle()
    expect(mobileToggle!.getAttribute("aria-expanded")).toBe("true")
    expect(mobileList!.textContent).toContain("12-01")
    expect(mobileList!.textContent).toContain("12-02")
    expect(mobileList!.textContent).toContain("46.250")

    // 2. Push full ARI respects needs_confirm=true instead of blindly sending confirm: 1
    const syncBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Push full ARI"),
    )!
    expect(syncBtn).toBeDefined()
    await act(async () => syncBtn.click())
    await settle()

    const fullSyncCallsBeforeConfirm = rpc.mock.calls.filter(([m]) => m === "kamra.channel_ari.request_full_sync")
    expect(fullSyncCallsBeforeConfirm).toHaveLength(1)
    expect(fullSyncCallsBeforeConfirm[0][1]).toEqual({ connection: "CMC-00001", confirm: 0 })

    const confirmDialog = host.querySelector('[role="alertdialog"]')
    expect(confirmDialog, "Expected 1/day full-sync confirmation alertdialog").not.toBeNull()

    const sendItBtn = Array.from(confirmDialog!.querySelectorAll("button")).find((b) =>
      b.textContent?.trim() === "Send it",
    )!
    expect(sendItBtn, "Missing 'Send it' confirmation button").toBeDefined()
    await act(async () => sendItBtn.click())
    await settle()

    const fullSyncCallsAfterConfirm = rpc.mock.calls.filter(([m]) => m === "kamra.channel_ari.request_full_sync")
    expect(fullSyncCallsAfterConfirm).toHaveLength(2)
    expect(fullSyncCallsAfterConfirm[1][1]).toEqual({ connection: "CMC-00001", confirm: 1 })
    expect(FORBIDDEN.test(host.textContent ?? "")).toBe(false)
  })
})
