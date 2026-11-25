// @vitest-environment jsdom
// P12: Editable BAR + OPERA Restrictions Matrix on RateCalendar.tsx
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
      return Promise.resolve({ locale: "en-US", currency: "KWD", currency_symbol: "KWD " })
    }
    return rpc(method, params)
  },
  getCurrentProperty: () => "Grand Kuwait",
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

function makeCalendar(currency: string, basePrice: number, rates: number[]) {
  return {
    from: "2026-12-01",
    days: 3,
    tax_inclusive: false,
    adults: 2,
    can_edit: authState.roles.some((r) => ["Revenue Manager", "Hotel Admin"].includes(r)),
    rate_plans: [],
    default_plan: null,
    property_currency: currency,
    rate_plan: null,
    company: null,
    room_types: [
      {
        room_type: "RT-STD",
        code: "STD",
        name: "Standard",
        base_price: basePrice,
        room_count: 5,
        sellable: true,
        nights: [
          {
            date: "2026-12-01",
            bar: basePrice,
            after_season: rates[0],
            rate: rates[0],
            season: null,
            demand: null,
            package: null,
            occupancy: 45,
            is_restricted: true,
            stop_sell: true,
            min_los: 2,
            max_los: 7,
            cta: true,
            ctd: false,
            restriction_scope: "room_type",
            restriction_reason: "Peak event",
          },
          {
            date: "2026-12-02",
            bar: basePrice,
            after_season: rates[1],
            rate: rates[1],
            season: null,
            demand: null,
            package: null,
            occupancy: 30,
            is_restricted: true,
            stop_sell: false,
            min_los: 0,
            max_los: 0,
            cta: false,
            ctd: true,
            restriction_scope: "room_type",
          },
          {
            date: "2026-12-03",
            bar: basePrice,
            after_season: rates[2],
            rate: rates[2],
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

describe("P12 Rate Calendar editable BAR & OPERA restrictions grid", () => {
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

  async function renderCalendar() {
    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={["/rate-calendar"]}>
          <App />
        </MemoryRouter>,
      ),
    )
    await vi.waitFor(async () => {
      await settle()
      expect(host.querySelector("table")).not.toBeNull()
    }, { interval: 1, timeout: 3000 })
    await settle()
  }

  it.each([
    ["KWD", 40.0, [45.125, 42.5, 40.0], "45.125"],
    ["UGX", 300000, [350000, 320000, 300000], "350,000"],
    ["USD", 100.0, [129.5, 110.0, 100.0], "129.50"],
  ] as const)(
    "renders restriction micro-badges (SS, CTA, CTD, MinLOS, MaxLOS) and %s precision rates",
    async (currency, base, vals, expectedText) => {
      rpc.mockImplementation((method: string) => {
        if (method === "kamra.rates.rate_calendar") return Promise.resolve(makeCalendar(currency, base, [...vals]))
        return Promise.reject(new Error(`Unexpected: ${method}`))
      })
      await renderCalendar()
      const cell1 = host.querySelector<HTMLElement>('[data-cell-rt="RT-STD"][data-cell-date="2026-12-01"]')
      expect(cell1, `Missing cell1 for ${currency}`).not.toBeNull()
      expect(cell1!.textContent).toContain(expectedText)
      expect(cell1!.querySelector('[data-badge="stop_sell"]')?.textContent).toContain("SS")
      expect(cell1!.querySelector('[data-badge="cta"]')?.textContent).toContain("CTA")
      expect(cell1!.querySelector('[data-badge="min_los"]')?.textContent).toContain("2+")
      expect(cell1!.querySelector('[data-badge="max_los"]')?.textContent).toContain("≤7")

      const cell2 = host.querySelector<HTMLElement>('[data-cell-rt="RT-STD"][data-cell-date="2026-12-02"]')
      expect(cell2!.querySelector('[data-badge="ctd"]')?.textContent).toContain("CTD")
      expect(FORBIDDEN.test(host.textContent ?? "")).toBe(false)
    },
  )

  it("gates edit mode to Revenue Manager / Hotel Admin and hides it for Front Desk", async () => {
    authState.roles = ["Front Desk"]
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.rates.rate_calendar") return Promise.resolve(makeCalendar("USD", 100, [120, 110, 100]))
      return Promise.reject(new Error(`Unexpected: ${method}`))
    })
    await renderCalendar()
    const editBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Edit rates & restrictions"),
    )
    expect(editBtn).toBeUndefined()
  })

  it("supports drag-select range editing of KWD 3dp BAR and restrictions via bulk_grid_update", async () => {
    rpc.mockImplementation((method: string, params: Record<string, unknown>) => {
      if (method === "kamra.rates.rate_calendar") return Promise.resolve(makeCalendar("KWD", 40, [45.125, 42.5, 40.0]))
      if (method === "kamra.restrictions.bulk_grid_update") {
        return Promise.resolve({
          ok: true,
          property: "Grand Kuwait",
          rates_updated: 2,
          restrictions_updated: 2,
          restrictions_deleted: 0,
          ari_queued: ["OUT-1"],
          echo: params,
        })
      }
      return Promise.reject(new Error(`Unexpected: ${method}`))
    })
    await renderCalendar()

    const toggle = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Edit rates & restrictions"),
    )!
    expect(toggle).toBeDefined()
    await act(async () => toggle.click())
    await settle()

    // Drag from 2026-12-01 to 2026-12-02
    const c1 = host.querySelector<HTMLElement>('[data-cell-rt="RT-STD"][data-cell-date="2026-12-01"]')!
    const c2 = host.querySelector<HTMLElement>('[data-cell-rt="RT-STD"][data-cell-date="2026-12-02"]')!
    await act(async () => {
      c1.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true }))
      c2.dispatchEvent(new MouseEvent("pointerover", { bubbles: true }))
      c2.dispatchEvent(new MouseEvent("pointerenter", { bubbles: true }))
      window.dispatchEvent(new MouseEvent("pointerup", { bubbles: true }))
    })
    await settle()

    // Drawer opens (or click "Edit selected")
    const editSelected = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Edit selected"),
    )
    if (editSelected) {
      await act(async () => editSelected.click())
      await settle()
    }

    const barInput = host.querySelector<HTMLInputElement>('input[aria-label="BAR rate"]')!
    expect(barInput, "BAR rate input missing").not.toBeNull()
    expect(barInput.step).toBe("0.001")

    const minInput = host.querySelector<HTMLInputElement>('input[aria-label="Min LOS"]')!
    const maxInput = host.querySelector<HTMLInputElement>('input[aria-label="Max LOS"]')!
    const ctaCheck = host.querySelector<HTMLInputElement>('input[aria-label="Closed to arrival"]')!
    const stopCheck = host.querySelector<HTMLInputElement>('input[aria-label="Stop-sell"]')!
    const reasonInput = host.querySelector<HTMLInputElement>('input[aria-label="Reason"]')!

    const setInput = (el: HTMLInputElement, value: string) => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
      setter.call(el, value)
      el.dispatchEvent(new Event("input", { bubbles: true }))
      el.dispatchEvent(new Event("change", { bubbles: true }))
    }

    await act(async () => {
      setInput(barInput, "48.375")
      setInput(minInput, "3")
      setInput(maxInput, "7")
      if (!ctaCheck.checked) ctaCheck.click()
      if (!stopCheck.checked) stopCheck.click()
      setInput(reasonInput, "Holiday peak")
    })
    await settle()

    const saveBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Save & sync OTAs"),
    )!
    expect(saveBtn).toBeDefined()
    await act(async () => saveBtn.click())
    await settle()

    const saveCall = rpc.mock.calls.find(([m]) => m === "kamra.restrictions.bulk_grid_update")
    expect(saveCall, "bulk_grid_update was not called").toBeDefined()
    const payload = saveCall![1] as Record<string, unknown>
    expect(payload.property).toBe("Grand Kuwait")
    expect(payload.reason).toBe("Holiday peak")
    expect(payload.rate_updates).toEqual([
      expect.objectContaining({
        room_type: "RT-STD",
        start_date: "2026-12-01",
        end_date: "2026-12-02",
        rate: 48.375,
      }),
    ])
    expect(payload.restriction_updates).toEqual([
      expect.objectContaining({
        room_type: "RT-STD",
        start_date: "2026-12-01",
        end_date: "2026-12-02",
        min_los: 3,
        max_los: 7,
        cta: 1,
        stop_sell: 1,
      }),
    ])
  })
})
