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

/**
 * R8 Rate Plans UI contracts (P1-1 & P1-2):
 * - Currency link picker fetches enabled currencies only with limit >= 500 (never 100 stopping at NZD).
 * - Other link pickers on Rate Plans (Base rate plan) also load without the 100-row cap.
 * - Property currency (UGX) and the plan's linked tariff currency (USD) are always selectable even if
 *   omitted from the enabled Currency query.
 * - Derived rate plans show inherited currency read-only (disabled) as OPERA Cloud does, and omit
 *   currency on create so the server assigns the base plan's currency instead of Frappe's global default.
 * - Zero "Kamra" brand leaks; new hint string translated in all 7 locales.
 */

const INHERITED_HINT = "Inherited from the base rate plan."

const { calls, fetches, state } = vi.hoisted(() => ({
  calls: vi.fn(),
  fetches: vi.fn(),
  state: {
    currencies: ["EUR", "GBP", "KES", "UGX", "USD"] as string[],
  },
}))

vi.mock("../src/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api")>()
  return {
    ...actual,
    call: (method: string, params?: Record<string, unknown>) => calls(method, params),
    getCurrentProperty: () => "Tank Hill Hotel",
    frappeFetch: (path: string, init?: RequestInit) => fetches(path, init),
  }
})
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: ["Hotel Admin", "Revenue Manager"] }) }))
vi.mock("../src/AppShell", () => ({ default: () => <Outlet /> }))

const RATE_PLANS = [
  {
    name: "Tank Hill Hotel-CPUSD",
    rate_plan_name: "Tank Hill CP",
    code: "CPUSD",
    modifier_type: "Percent",
    modifier_value: 0,
    currency: "",
    fx_lock_policy: "Fixed at Reservation",
    base_rate_plan: "",
    custom_tariff_price_list: "Tank Hill CP USD",
    is_default: 0,
    disabled: 0,
  },
  {
    name: "Tank Hill Hotel-STD",
    rate_plan_name: "Tank Hill Standard",
    code: "STD",
    modifier_type: "Percent",
    modifier_value: 0,
    currency: "UGX",
    fx_lock_policy: "Fixed at Reservation",
    base_rate_plan: "",
    is_default: 1,
    disabled: 0,
  },
  {
    name: "Tank Hill Hotel-CPHALF",
    rate_plan_name: "Tank Hill CP Half",
    code: "CPHALF",
    modifier_type: "Percent",
    modifier_value: -50,
    currency: "USD",
    fx_lock_policy: "Fixed at Check In",
    base_rate_plan: "Tank Hill Hotel-CPUSD",
    is_default: 0,
    disabled: 0,
  },
]

describe("R8 Rate Plans screen currency picker and derived-plan inheritance", { timeout: 25000 }, () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    localStorage.clear()
    state.currencies = ["EUR", "GBP", "KES", "UGX", "USD"]

    calls.mockReset().mockImplementation(async (method: string) => {
      if (method === "kamra.api.property_locale") {
        return {
          currency: "UGX",
          currency_symbol: "UGX ",
          locale: "en-UG",
          tax_label: "VAT",
          tax_id_label: "TIN",
          tax_rates: [0, 18],
          id_types: ["National ID", "Passport"],
          id_type_hints: {},
          payment_modes: ["Cash", "Card", "Bank Transfer", "Mobile Money"],
          instant_payment_modes: ["Mobile Money"],
          outlet_payment_modes: ["Cash", "Card", "Mobile Money"],
          default_nationality: "Ugandan",
          adult_age: 18,
          minor_ids: "foreign",
          integrations: { payment_gateways: [], tax_return: false, verify_tax_id: false, verify_guest_id: false },
          market_codes: [],
          booker_relations: [],
          cancellation_reasons: [],
        }
      }
      if (method === "kamra.rates.list_contract_tariffs") {
        return {
          supported: true,
          property: "Tank Hill Hotel",
          property_currency: "UGX",
          price_lists: [
            { name: "Tank Hill CP USD", currency: "USD", rate_plans: ["Tank Hill Hotel-CPUSD"] },
          ],
          rate_plan_policies: {
            "Tank Hill Hotel-CPUSD": {
              label: "Tank Hill CP",
              code: "CPUSD",
              currency: "USD",
              fx_lock_policy: "Fixed at Reservation",
              base_rate_plan: null,
            },
          },
          items: [],
          can_propose: true,
          can_approve: true,
          can_discard: true,
        }
      }
      return {}
    })

    fetches.mockReset().mockImplementation(async (path: string, init?: RequestInit) => {
      if (init?.method === "POST") return { data: { name: "Tank Hill Hotel-NEW", ...JSON.parse(String(init.body)) } }
      if (init?.method === "PUT") return { data: { ...JSON.parse(String(init.body)) } }
      if (path.startsWith("/api/resource/Currency?")) {
        return { data: state.currencies.map((name) => ({ name })) }
      }
      if (path.startsWith("/api/resource/Rate%20Plan/")) {
        const name = decodeURIComponent(path.slice("/api/resource/Rate%20Plan/".length))
        return { data: RATE_PLANS.find((rp) => rp.name === name) ?? {} }
      }
      if (path.startsWith("/api/resource/Rate%20Plan?")) {
        return { data: RATE_PLANS }
      }
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

  const settle = () => act(() => new Promise((r) => setTimeout(r, 20)))

  const setSelect = async (sel: HTMLSelectElement, value: string) => {
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!
      setter.call(sel, value)
      sel.dispatchEvent(new Event("change", { bubbles: true }))
    })
  }

  const setInput = async (input: HTMLInputElement, value: string) => {
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
      setter.call(input, value)
      input.dispatchEvent(new Event("input", { bubbles: true }))
      input.dispatchEvent(new Event("change", { bubbles: true }))
    })
  }

  const fieldSelect = (labelRe: RegExp) => {
    const lbl = Array.from(document.querySelectorAll("label")).find((l) => labelRe.test(l.textContent || ""))
    return lbl?.querySelector("select") as HTMLSelectElement | null
  }

  const fieldInput = (labelRe: RegExp) => {
    const lbl = Array.from(document.querySelectorAll("label")).find((l) => labelRe.test(l.textContent || ""))
    return lbl?.querySelector("input") as HTMLInputElement | null
  }

  it("fetches enabled currencies only with limit >= 500, removes 100 cap on other link fields, and ensures property + tariff currencies", async () => {
    // Even if enabled Currency query omits UGX and USD, property currency (UGX) and plan tariff currency (USD) must be present
    state.currencies = ["EUR", "GBP"]

    await act(async () => root.render(<MemoryRouter initialEntries={["/rate-plans"]}><App /></MemoryRouter>))
    await vi.waitFor(async () => {
      await settle()
      expect(host.textContent).toContain("Tank Hill CP")
    }, { interval: 10, timeout: 5000 })

    // Verify Currency fetch uses enabled=1 filter and limit >= 500
    const currencyUrl = fetches.mock.calls.map(([p]) => String(p)).find((p) => p.startsWith("/api/resource/Currency?"))
    expect(currencyUrl).toBeDefined()
    const currencyParams = new URL(currencyUrl!, "http://localhost").searchParams
    expect(JSON.parse(currencyParams.get("filters") || "[]")).toContainEqual(["enabled", "=", 1])
    expect(Number(currencyParams.get("limit_page_length"))).toBeGreaterThanOrEqual(500)

    // Verify Base rate plan link fetch also has limit >= 500 (not capped at 100)
    const linkRatePlanUrls = fetches.mock.calls
      .map(([p]) => String(p))
      .filter((p) => p.startsWith("/api/resource/Rate%20Plan?") && new URL(p, "http://localhost").searchParams.get("fields") === '["name"]')
    expect(linkRatePlanUrls.length).toBeGreaterThan(0)
    for (const u of linkRatePlanUrls) {
      expect(Number(new URL(u, "http://localhost").searchParams.get("limit_page_length"))).toBeGreaterThanOrEqual(500)
    }

    // Open Tank Hill CP (unbackfilled blank currency, linked to Tank Hill CP USD tariff price list)
    const cpRow = Array.from(host.querySelectorAll("tbody tr")).find((tr) => tr.textContent?.includes("CPUSD")) as HTMLElement
    expect(cpRow).toBeDefined()
    await act(async () => cpRow.click())
    await vi.waitFor(async () => {
      await settle()
      expect(fieldSelect(/Currency/i)).not.toBeNull()
    }, { interval: 10, timeout: 5000 })

    const curSel = fieldSelect(/Currency/i)!
    const opts = Array.from(curSel.options).map((o) => o.value).filter(Boolean)
    expect(opts).toContain("EUR")
    expect(opts).toContain("GBP")
    expect(opts).toContain("UGX")
    expect(opts).toContain("USD")
    expect(curSel.disabled).toBe(false)
    expect(document.body.textContent).not.toMatch(/Kamra/i)
  })

  it("shows inherited currency read-only on derived plans and omits currency on create", async () => {
    await act(async () => root.render(<MemoryRouter initialEntries={["/rate-plans"]}><App /></MemoryRouter>))
    await vi.waitFor(async () => {
      await settle()
      expect(host.textContent).toContain("Tank Hill CP")
    }, { interval: 10, timeout: 5000 })

    const newBtn = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.trim() === "New") as HTMLElement
    expect(newBtn).toBeDefined()
    await act(async () => newBtn.click())
    await vi.waitFor(async () => {
      await settle()
      expect(fieldSelect(/Base rate plan/i)).not.toBeNull()
    }, { interval: 10, timeout: 5000 })

    const curSel = fieldSelect(/Currency/i)!
    const baseSel = fieldSelect(/Base rate plan/i)!
    expect(curSel.disabled).toBe(false)

    // Pick Tank Hill Hotel-CPUSD (whose tariff currency is USD): Currency becomes read-only & shows USD
    await setSelect(baseSel, "Tank Hill Hotel-CPUSD")
    await settle()
    expect(curSel.disabled).toBe(true)
    expect(curSel.value).toBe("USD")
    expect(document.body.textContent).toContain(INHERITED_HINT)

    // Fill required fields and Save: POST payload must include base_rate_plan and omit currency
    await setInput(fieldInput(/^Name/i)!, "Tank Hill CP Promo")
    await setInput(fieldInput(/^Code/i)!, "CPPROMO")
    const saveBtn = Array.from(document.querySelectorAll("button")).find((b) => b.textContent?.trim() === "Save") as HTMLElement
    await act(async () => saveBtn.click())
    await settle()

    const post = fetches.mock.calls.find(([p, init]) => String(p) === "/api/resource/Rate%20Plan" && init?.method === "POST")
    expect(post).toBeDefined()
    const payload = JSON.parse(String(post![1]?.body))
    expect(payload.base_rate_plan).toBe("Tank Hill Hotel-CPUSD")
    expect(payload).not.toHaveProperty("currency")

    // Re-open New, pick base then clear base: Currency re-enables
    await act(async () => newBtn.click())
    await settle()
    const curSel2 = fieldSelect(/Currency/i)!
    const baseSel2 = fieldSelect(/Base rate plan/i)!
    await setSelect(baseSel2, "Tank Hill Hotel-CPUSD")
    await settle()
    expect(curSel2.disabled).toBe(true)
    await setSelect(baseSel2, "")
    await settle()
    expect(curSel2.disabled).toBe(false)
  })

  it.each([
    ["ar", ar], ["de", de], ["es", es], ["fil", fil], ["id", id], ["pt", pt], ["zh-CN", zhCN],
  ])("translates inherited currency hint in %s", (_lang, dict: Record<string, string>) => {
    expect(dict[INHERITED_HINT], INHERITED_HINT).toBeTruthy()
  })
})
