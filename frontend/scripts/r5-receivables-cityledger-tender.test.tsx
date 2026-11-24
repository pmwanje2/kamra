// @vitest-environment jsdom
/** R5 RED->GREEN tests for Hydrogen R4 blocking findings #2 and #3 (P2):
 * 2. Receivables AccountList cold-load / property-switch locale race:
 *    - AccountList subscribes to useLocale() and AppShell refreshKey so cold
 *      reloads (ar_accounts resolving before property_locale) and property
 *      switches immediately render 3dp KWD with د.ك (and recompute overdue/
 *      queue thresholds with moneyEps(currency)).
 * 3. CityLedgerPayment.tsx & TenderPicker.tsx dynamic currency decimals:
 *    - CityLedgerPayment replaces hardcoded r2 / +0.005 / step="0.01" with
 *      currency-aware roundMoney / moneyEps / moneyStep / useLocale() across
 *      0dp (UGX), 2dp (USD), and 3dp (KWD), handling 0.004 KWD open bills and
 *      11.944 KWD allocations without truncating to 11.940 or 0.000.
 *    - TenderPicker tenderAmount & TenderInput use dynamic currency minor-unit
 *      digits across home & FX currencies (step="0.001" for KWD, "1" for UGX,
 *      "0.01" for USD; "Full amount" fills "11.944" on 11.944 KWD).
 */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import Receivables from "../src/screens/Receivables"
import CityLedgerPayment from "../src/screens/CityLedgerPayment"
import { TenderInput, tenderAmount } from "../src/components/TenderPicker"
import { loadLocale } from "../src/lib/money"
import { setLang } from "../src/lib/dir"

const { rpc, listRes, state } = vi.hoisted(() => ({
  rpc: vi.fn(),
  listRes: vi.fn(),
  state: { property: "Kuwait Bay Hotel" },
}))

vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => state.property,
  newIdempotencyKey: () => "key-r5-kwd",
}))

vi.mock("../src/lib/resource", () => ({
  listResource: (doctype: string, opts: unknown) => listRes(doctype, opts),
  serverError: (e: unknown) => (e instanceof Error ? e.message : String(e)),
}))

vi.mock("../src/lib/auth", () => ({
  useAuth: () => ({ status: "authed", roles: ["Front Desk", "Finance", "Hotel Admin"] }),
}))

function setValue(el: HTMLInputElement | HTMLSelectElement, value: string) {
  const proto =
    el instanceof HTMLSelectElement
      ? HTMLSelectElement.prototype
      : HTMLInputElement.prototype
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set
  setter?.call(el, value)
  el.dispatchEvent(new Event("input", { bubbles: true }))
  el.dispatchEvent(new Event("change", { bubbles: true }))
}

function makeLocale(currency: string, currency_symbol: string, tax_rates: number[]) {
  return {
    currency,
    currency_symbol,
    locale: "en-US",
    tax_label: "VAT",
    tax_id_label: "Tax ID",
    tax_rates,
    payment_modes: ["Cash", "Card", "Bank Transfer"],
    payment_tender_types: { Cash: "Cash", Card: "Card", "Bank Transfer": "Bank Transfer" },
    instant_payment_modes: [],
    outlet_payment_modes: ["Cash"],
    id_types: ["Passport"],
    id_type_hints: {},
    default_nationality: "",
    adult_age: 18,
    minor_ids: "foreign",
    integrations: {
      payment_gateways: [],
      tax_return: false,
      verify_tax_id: false,
      verify_guest_id: false,
    },
  }
}

describe("R5 P2: Receivables AccountList locale race, CityLedgerPayment, and TenderPicker precision", () => {
  let host: HTMLDivElement
  let root: Root
  let activeLocale = makeLocale("KWD", "د.ك", [0, 5])

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    localStorage.clear()
    state.property = "Kuwait Bay Hotel"
    activeLocale = makeLocale("KWD", "د.ك", [0, 5])
    listRes.mockReset().mockResolvedValue([])
    rpc.mockReset()
    host = document.createElement("div")
    document.body.append(host)
    root = createRoot(host)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    host.remove()
    setLang("en")
    localStorage.clear()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it("AccountList re-renders immediately on cold load and property switch when KWD locale resolves after ar_accounts", async () => {
    let resolveLocale!: (val: ReturnType<typeof makeLocale>) => void
    const pendingLocale = new Promise<ReturnType<typeof makeLocale>>((res) => {
      resolveLocale = res
    })
    let arCalls = 0

    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.property_locale") return pendingLocale
      if (method === "kamra.receivables.ar_accounts") {
        arCalls += 1
        return Promise.resolve({
          accounts: [
            {
              name: "CLA-KWD-1",
              company: "Al-Sayer Travel KWT",
              company_name: "Al-Sayer Travel KWT",
              account_number: "AR-KWT-001",
              balance: 11.948,
              credit_limit: 500,
              credit_days: 30,
              overdue: 0.004,
              unapplied_credit: 0,
              restricted: 0,
              not_due: 11.944,
              d1_30: 0.004,
              d31_60: 0,
              d61_90: 0,
              d90p: 0,
            },
          ],
          summary: {
            to_bill: 0,
            billed_pending: 11.948,
            overdue: 0.004,
            received_this_month: 5.944,
            deposits_held: 11.944,
            guests_owing: [],
          },
        })
      }
      return Promise.resolve({})
    })

    // Cold load: ar_accounts resolves BEFORE property_locale finishes (cache is still empty/default)
    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={["/receivables"]}>
          <Routes>
            <Route element={<Outlet context={{ refreshKey: 0 }} />}>
              <Route path="/receivables" element={<Receivables />} />
            </Route>
          </Routes>
        </MemoryRouter>,
      ),
    )
    await vi.waitFor(() => expect(host.textContent).toContain("Al-Sayer Travel KWT"))

    // Trigger locale load (as AppShell does on cold start) and resolve KWD after ar_accounts already painted
    const locPromise = loadLocale()
    await act(async () => {
      resolveLocale(makeLocale("KWD", "د.ك", [0, 5]))
      await locPromise
    })

    // AccountList MUST immediately display 3dp KWD with د.ك (never stuck on 5.94 / 11.94 / 11.95)
    await vi.waitFor(() => {
      expect(host.textContent).toContain("د.ك5.944 received this month")
    })
    const text = host.textContent ?? ""
    expect(text).toContain("د.ك11.944 advances held")
    expect(text).toContain("د.ك11.948")
    expect(text).toContain("Overdue · 1")
    expect(text).toContain("د.ك0.004")
    expect(text).not.toContain("5.94 received this month")
    expect(text).not.toContain("11.94 advances held")
    expect(text).not.toContain("11.95")

    // Respect AppShell refreshKey updates (e.g. realtime or property/locale refresh)
    const callsBeforeRefresh = arCalls
    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={["/receivables"]}>
          <Routes>
            <Route element={<Outlet context={{ refreshKey: 1 }} />}>
              <Route path="/receivables" element={<Receivables />} />
            </Route>
          </Routes>
        </MemoryRouter>,
      ),
    )
    await vi.waitFor(() => expect(arCalls).toBeGreaterThan(callsBeforeRefresh))

    // Property switch: switch from KWD -> USD where ar_accounts answers before USD property_locale
    let resolveUsd!: (val: ReturnType<typeof makeLocale>) => void
    const pendingUsd = new Promise<ReturnType<typeof makeLocale>>((res) => {
      resolveUsd = res
    })
    state.property = "Manhattan USD Hotel"
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.property_locale") return pendingUsd
      if (method === "kamra.receivables.ar_accounts") {
        return Promise.resolve({
          accounts: [
            {
              name: "CLA-USD-1",
              company: "Acme Corp USD",
              company_name: "Acme Corp USD",
              account_number: "AR-USD-001",
              balance: 25.5,
              credit_limit: 500,
              credit_days: 30,
              overdue: 0,
              unapplied_credit: 0,
              restricted: 0,
              not_due: 25.5,
            },
          ],
          summary: {
            to_bill: 0,
            billed_pending: 25.5,
            overdue: 0,
            received_this_month: 10.25,
            deposits_held: 0,
            guests_owing: [],
          },
        })
      }
      return Promise.resolve({})
    })

    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={["/receivables"]}>
          <Routes>
            <Route element={<Outlet context={{ refreshKey: 2 }} />}>
              <Route path="/receivables" element={<Receivables key="usd-prop" />} />
            </Route>
          </Routes>
        </MemoryRouter>,
      ),
    )
    await vi.waitFor(() => expect(host.textContent).toContain("Acme Corp USD"))

    const usdLocPromise = loadLocale()
    await act(async () => {
      resolveUsd(makeLocale("USD", "$", [0, 8]))
      await usdLocPromise
    })
    await vi.waitFor(() => {
      expect(host.textContent).toContain("$10.25 received this month")
    })
    expect(host.textContent).toContain("$25.50")
    expect(host.textContent).not.toContain("د.ك")
  })

  it("CityLedgerPayment uses dynamic currency decimals for KWD 0.004 & 11.944, UGX 0dp, and USD 2dp", async () => {
    const accountData = {
      account: "CLA-KWD-1",
      company: "Al-Sayer Travel KWT",
      account_number: "AR-KWT-001",
      balance: 11.948,
      open_balance: 11.948,
      unapplied_credit: 0,
      credit_limit: 500,
      credit_days: 30,
      not_due: 11.948,
      d1_30: 0,
      d31_60: 0,
      d61_90: 0,
      d90p: 0,
      overdue: 0,
      open_items: [
        {
          folio: "FOL-5",
          invoice_number: "INV-KBH-26-00005",
          invoice_date: "2026-11-20",
          due_date: "2026-12-20",
          days_overdue: 0,
          billed: 11.944,
          paid: 5.944,
          outstanding: 6.0,
        },
        {
          folio: "FOL-6",
          invoice_number: "INV-KBH-26-00006",
          invoice_date: "2026-11-21",
          due_date: "2026-12-21",
          days_overdue: 0,
          billed: 0.004,
          paid: 0,
          outstanding: 0.004,
        },
        {
          folio: "FOL-7",
          invoice_number: "INV-KBH-26-00007",
          invoice_date: "2026-11-22",
          due_date: "2026-12-22",
          days_overdue: 0,
          billed: 5.944,
          paid: 0,
          outstanding: 5.944,
        },
      ],
      payments: [],
      payment_pin_required: false,
    }

    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.property_locale") return Promise.resolve(activeLocale)
      if (method === "kamra.ledger.city_ledger_account") return Promise.resolve(accountData)
      return Promise.resolve({})
    })

    // Prime KWD locale
    activeLocale = makeLocale("KWD", "د.ك", [0, 5])
    await act(async () => {
      await loadLocale()
      root.render(
        <CityLedgerPayment account="CLA-KWD-1" onClose={() => undefined} onPosted={() => undefined} />,
      )
    })
    await vi.waitFor(() => expect(host.textContent).toContain("INV-KBH-26-00006"))

    const amountInput = host.querySelector('input[inputmode="decimal"]') as HTMLInputElement
    expect(amountInput.step).toBe("0.001")

    // 1. Entering 0.004 KWD applies د.ك0.004 (never د.ك0.000)
    await act(async () => setValue(amountInput, "0.004"))
    expect(host.textContent).toContain("Applies د.ك0.004")
    expect(host.textContent).not.toContain("Applies د.ك0.000")

    // 2. Entering 11.944 KWD allocates 6.000 + 0.004 + 5.940 = د.ك11.944 (never 11.940 or skipping 0.004)
    await act(async () => setValue(amountInput, "11.944"))
    expect(host.textContent).toContain("Applies د.ك11.944")
    expect(host.textContent).not.toContain("Applies د.ك11.940")
    const rows = Array.from(host.querySelectorAll("tbody tr"))
    const inv6Row = rows.find((r) => r.textContent?.includes("INV-KBH-26-00006"))
    const inv7Row = rows.find((r) => r.textContent?.includes("INV-KBH-26-00007"))
    expect(inv6Row?.querySelectorAll("td")[5]?.textContent?.trim()).toBe("د.ك0.004")
    expect(inv7Row?.querySelectorAll("td")[5]?.textContent?.trim()).toBe("د.ك5.940")

    // 3. Review payment confirmation preserves exact 3dp Balance after: د.ك0.004 (never د.ك0.000)
    const reviewBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Review payment"),
    ) as HTMLButtonElement
    await act(async () => reviewBtn.click())
    expect(host.textContent).toContain("Balance after: د.ك0.004")
    const backBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.trim() === "Back",
    ) as HTMLButtonElement
    await act(async () => backBtn.click())

    // 4. Manual pick mode: picking 0.004 KWD bill uses step=0.001, prefills 0.004, and rejects 0.005 over-allocation
    const pickRadio = Array.from(host.querySelectorAll('input[type="radio"]'))[1] as HTMLInputElement
    await act(async () => pickRadio.click())
    const inv6Checkbox = host.querySelector('input[aria-label="Pay INV-KBH-26-00006"]') as HTMLInputElement
    await act(async () => inv6Checkbox.click())
    const manualInput = host.querySelector('tbody input[type="number"]') as HTMLInputElement
    expect(manualInput.step).toBe("0.001")
    expect(Number(manualInput.value)).toBeCloseTo(0.004, 6)
    await act(async () => setValue(manualInput, "0.005"))
    expect(host.textContent).toContain("INV-KBH-26-00006 owes د.ك0.004.")

    // 5. UGX (0dp) uses step="1" and USD (2dp) uses step="0.01"
    activeLocale = makeLocale("UGX", "UGX ", [0, 18])
    await act(async () => {
      await loadLocale()
      root.render(
        <CityLedgerPayment key="ugx" account="CLA-KWD-1" onClose={() => undefined} onPosted={() => undefined} />,
      )
    })
    await vi.waitFor(() => {
      const inp = host.querySelector('input[inputmode="decimal"]') as HTMLInputElement | null
      expect(inp?.step).toBe("1")
    })

    activeLocale = makeLocale("USD", "$", [0, 5])
    await act(async () => {
      await loadLocale()
      root.render(
        <CityLedgerPayment key="usd" account="CLA-KWD-1" onClose={() => undefined} onPosted={() => undefined} />,
      )
    })
    await vi.waitFor(() => {
      const inp = host.querySelector('input[inputmode="decimal"]') as HTMLInputElement | null
      expect(inp?.step).toBe("0.01")
    })
  })

  it("TenderPicker tenderAmount and TenderInput use exact currency decimals for KWD 3dp, UGX 0dp, USD 2dp, and FX", async () => {
    // 1. Home currencies: KWD 3dp, USD 2dp, UGX 0dp
    expect(tenderAmount(11.944, "KWD", "KWD", {})).toBe("11.944")
    expect(tenderAmount(0.004, "KWD", "KWD", {})).toBe("0.004")
    expect(tenderAmount(11.944, "USD", "USD", {})).toBe("11.94")
    expect(tenderAmount(524194.4, "UGX", "UGX", {})).toBe("524194")

    // 2. Cross-currency FX: foreign KWD (3dp) on USD hotel, foreign UGX (0dp) on KWD hotel, USD on UGX regression
    expect(tenderAmount(38.82, "KWD", "USD", { KWD: 3.25 })).toBe("11.944")
    expect(tenderAmount(11.944, "UGX", "KWD", { UGX: 0.000083 })).toBe("143903")
    expect(tenderAmount(524194, "USD", "UGX", { USD: 4032.258064516 })).toBe("130")
    expect(tenderAmount(524195, "USD", "UGX", { USD: 4032.258064516 })).toBe("130.01")

    // 3. TenderInput renders step="0.001" for KWD and fills "11.944" on "Full amount"
    const onKwdChange = vi.fn()
    await act(async () =>
      root.render(
        <TenderInput
          currency="KWD"
          value=""
          full={tenderAmount(11.944, "KWD", "KWD", {})}
          onChange={onKwdChange}
        />,
      ),
    )
    const kwdInput = host.querySelector('input[aria-label="Amount collected"]') as HTMLInputElement
    expect(kwdInput.step).toBe("0.001")
    const kwdFullBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Full amount"),
    ) as HTMLButtonElement
    await act(async () => kwdFullBtn.click())
    expect(onKwdChange).toHaveBeenCalledWith("11.944")

    // 4. TenderInput renders step="1" for UGX and step="0.01" for USD
    const onUgxChange = vi.fn()
    await act(async () =>
      root.render(
        <TenderInput
          currency="UGX"
          value=""
          full={tenderAmount(524194, "UGX", "UGX", {})}
          onChange={onUgxChange}
        />,
      ),
    )
    const ugxInput = host.querySelector('input[aria-label="Amount collected"]') as HTMLInputElement
    expect(ugxInput.step).toBe("1")
    const ugxFullBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Full amount"),
    ) as HTMLButtonElement
    await act(async () => ugxFullBtn.click())
    expect(onUgxChange).toHaveBeenCalledWith("524194")

    const onUsdChange = vi.fn()
    await act(async () =>
      root.render(
        <TenderInput
          currency="USD"
          value=""
          full={tenderAmount(10.25, "USD", "USD", {})}
          onChange={onUsdChange}
        />,
      ),
    )
    const usdInput = host.querySelector('input[aria-label="Amount collected"]') as HTMLInputElement
    expect(usdInput.step).toBe("0.01")
  })
})
