// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import FixedChargesPanel from "../src/components/FixedChargesPanel"
import AuditSummaryCard from "../src/components/AuditSummaryCard"
import EndOfDayWizard from "../src/screens/EndOfDayWizard"
import { adoptUiLocale } from "../src/lib/money"

const { rpc } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return { rpc: vi.fn() }
})

vi.mock("../src/lib/api", async (original) => ({
  ...await original<typeof import("../src/lib/api")>(),
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => "Kampala Grand",
}))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: ["Hotel Admin", "Front Desk"] }) }))
vi.mock("../src/lib/cashierAuth", () => ({ useCashierAuth: () => ({ ensure: async () => true }) }))
vi.mock("../src/screens/AuditPack", async (original) => ({
  ...await original<typeof import("../src/screens/AuditPack")>(),
  AuditPackView: () => <div data-testid="audit-pack-stub" />,
}))

describe("FixedChargesPanel & EndOfDayWizard Step 9 Summary (P29 + P41)", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    host = document.createElement("div")
    document.body.appendChild(host)
    root = createRoot(host)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    host.remove()
    localStorage.clear()
    vi.unstubAllGlobals()
  })

  it("lists fixed charges with 0dp UGX formatting and creates a new Daily fixed charge", async () => {
    adoptUiLocale({ currency: "UGX", currency_symbol: "UGX ", locale: "en-US" })
    const savedRows: Record<string, unknown>[] = [
      {
        name: "FC-1",
        transaction_code: "3300",
        charge_type: "Misc",
        description: "Valet Parking",
        qty: 1,
        unit_price: 25000,
        frequency: "Daily",
        start_date: "2026-11-24",
        end_date: "2026-11-27",
        window_no: 2,
        active: 1,
        post_count: 1,
      },
    ]
    rpc.mockReset().mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === "kamra.fixed_charges.list_fixed_charges") {
        return {
          reservation: "RES-2026-00001",
          property: "Kampala Grand",
          currency: "UGX",
          check_in_date: "2026-11-24",
          check_out_date: "2026-11-27",
          rows: savedRows,
          transaction_codes: [
            { code: "3300", description: "Parking", charge_type: "Misc" },
            { code: "3400", description: "Resort Fee", charge_type: "Misc" },
          ],
        }
      }
      if (method === "kamra.fixed_charges.save_fixed_charge") {
        const data = params.row_data as Record<string, unknown>
        const row = { name: "FC-2", active: 1, post_count: 0, ...data }
        savedRows.push(row)
        return { row, rows: savedRows }
      }
      return {}
    })

    await act(async () => {
      root.render(<FixedChargesPanel reservation="RES-2026-00001" status="Checked In" />)
    })

    await vi.waitFor(() => {
      expect(host.textContent).toContain("Valet Parking")
      expect(host.textContent).toContain("25,000")
      expect(host.textContent).not.toContain("25,000.00")
      expect(host.textContent).toContain("W2")
    })

    const addBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Add fixed charge"),
    )
    expect(addBtn).toBeDefined()
    await act(async () => addBtn!.click())

    const descInput = host.querySelector<HTMLInputElement>("input[name='fc_description']")
    const priceInput = host.querySelector<HTMLInputElement>("input[name='fc_unit_price']")
    expect(descInput).not.toBeNull()
    expect(priceInput).not.toBeNull()

    await act(async () => {
      const setNative = (el: HTMLInputElement, val: string) => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, val)
        el.dispatchEvent(new Event("input", { bubbles: true }))
        el.dispatchEvent(new Event("change", { bubbles: true }))
      }
      setNative(descInput!, "Resort Fee")
      setNative(priceInput!, "15000")
    })

    const submitBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Save charge"),
    )
    expect(submitBtn).toBeDefined()
    await act(async () => submitBtn!.click())

    expect(rpc).toHaveBeenCalledWith(
      "kamra.fixed_charges.save_fixed_charge",
      expect.objectContaining({
        reservation: "RES-2026-00001",
      }),
    )
  })

  it("renders AuditSummaryCard and EndOfDayWizard with exact 0dp/3dp precision and Step 9 itemised counts", async () => {
    adoptUiLocale({ currency: "KWD", currency_symbol: "KD ", locale: "en-US" })
    await act(async () => {
      root.render(
        <AuditSummaryCard
          currency="KWD"
          summary={{
            room_charges_posted: 12,
            package_elements_posted: 8,
            fixed_charges_posted: 5,
            fixed_charges_amount: 125.5,
            package_breakage_total: 40,
            amount_posted: 2450.25,
          }}
        />,
      )
    })
    expect(host.textContent).toContain("12")
    expect(host.textContent).toContain("8")
    expect(host.textContent).toContain("5")
    expect(host.textContent).toContain("125.500")
    expect(host.textContent).toContain("40.000")
    expect(host.textContent).toContain("2,450.250")

    // Verify EndOfDayWizard formats amounts at property currency digits (0dp for UGX, not hardcoded 2dp)
    adoptUiLocale({ currency: "UGX", currency_symbol: "UGX ", locale: "en-US" })
    rpc.mockReset().mockImplementation(async (method: string) => {
      if (method === "kamra.night_audit.eod_session") {
        return {
          property: "Kampala Grand",
          property_name: "Kampala Grand",
          currency: "UGX",
          business_date: "2026-11-24",
          server_date: "2026-11-24",
          server_time: "23:00:00",
          property_now: "2026-11-24 23:00:00",
          require_cashier_pin: 0,
          blind_count: 0,
          high_balance_threshold: 500000,
          auto_audit_with_blockers: "Hold and alert",
          reports: [],
          audit: "AUDIT-2026-11-24-Kampala Grand",
          ran: false,
          status: "Draft",
          current_step: 9,
          acknowledgements: [],
          checks: [
            {
              key: "high_balance",
              title: "High balances",
              severity: "warn",
              count: 1,
              items: [{ reservation: "RES-1", guest_name: "Guest A", room: "101", balance: 450000 }],
              action: "Review high balances",
            },
          ],
          steps: [],
          sessions: [],
        }
      }
      return {}
    })

    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/end-of-day/Kampala%20Grand"]}>
          <Routes>
            <Route path="/end-of-day/:property" element={<EndOfDayWizard />} />
          </Routes>
        </MemoryRouter>,
      )
    })

    await vi.waitFor(() => {
      expect(host.textContent).toContain("450,000")
      expect(host.textContent).not.toContain("450,000.00")
    })
  })
})
