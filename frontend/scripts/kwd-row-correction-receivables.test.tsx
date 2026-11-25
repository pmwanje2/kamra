// @vitest-environment jsdom
/** R4 RED->GREEN tests for Hydrogen R3 finding #3 (P2):
 * - RowCorrection.tsx: currency-aware step ("0.001" KWD, "0.01" USD, "1" UGX) and
 *   decimal validation so 0.001 KWD partial refunds submit cleanly while sub-minor
 *   inputs (0.0004 KWD, 0.5 UGX, 0.001 USD) are rejected cleanly.
 * - Receivables.tsx: currency-aware step and preview rounding in NewInvoice (and
 *   AccountSetup) so 11.375 KWD renders 11.375 (NEVER 11.380), 5% VAT renders
 *   0.569 / 11.944, and 0.004 KWD open balances are never mislabeled Paid.
 */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import RowCorrection from "../src/components/RowCorrection"
import Receivables from "../src/screens/Receivables"
import { setLang } from "../src/lib/dir"

const { rpc, listRes } = vi.hoisted(() => ({ rpc: vi.fn(), listRes: vi.fn() }))

vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => "Kuwait Bay Hotel",
  newIdempotencyKey: () => "key-r4-kwd",
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

async function enterSupervisorPin(host: HTMLElement, pin: string) {
  const pad = host.querySelector("dialog[open]")!
  const buttons = Array.from(pad.querySelectorAll("button"))
  await act(async () => {
    for (const digit of pin) buttons.find((button) => button.textContent === digit)!.click()
  })
  const approve = buttons.find((button) => button.textContent === "Approve correction")!
  await act(async () => approve.click())
}

function makeLocale(currency: string, currency_symbol: string, tax_rates: number[]) {
  return {
    currency,
    currency_symbol,
    locale: "en-US",
    tax_label: "VAT",
    tax_id_label: "Tax ID",
    tax_rates,
    payment_modes: ["Cash", "Card"],
    payment_tender_types: { Cash: "Cash", Card: "Card" },
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

describe("R4 P2: RowCorrection and Receivables currency-aware minor-unit precision", () => {
  let host: HTMLDivElement
  let root: Root
  let activeLocale = makeLocale("KWD", "د.ك", [0, 5])

  beforeEach(() => {
    const dialog = HTMLDialogElement.prototype
    // jsdom implements the dialog element without its top-layer methods.
    if (!dialog.showModal) dialog.showModal = function () { this.open = true }
    if (!dialog.close) dialog.close = function () { this.open = false }
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    localStorage.clear()
    activeLocale = makeLocale("KWD", "د.ك", [0, 5])
    listRes.mockReset().mockResolvedValue([])
    rpc.mockReset().mockImplementation((method: string) => {
      if (method === "kamra.api.property_locale") return Promise.resolve(activeLocale)
      if (method === "kamra.api.payment_correction_options") {
        return Promise.resolve({
          actions: [
            {
              id: "partial_refund",
              label: "Refund part of payment",
              requires: ["supervisor_user", "supervisor_pin"],
              reason_codes: [
                { name: "REF-OVER", description: "Overpayment refund", requires_supervisor: 0 },
              ],
            },
          ],
          blocked: [],
          supervisors: [{ user: "sup@example.test", full_name: "Shift Supervisor" }],
          refund_cap: activeLocale.currency === "UGX" ? 50000 : activeLocale.currency === "USD" ? 10 : 11.943,
        })
      }
      if (method === "kamra.api.correct_folio_payment") {
        return Promise.resolve({ ok: true, refund: "FP-REF-1", balance: 0 })
      }
      if (method === "kamra.ledger.city_ledger_account") {
        return Promise.resolve({
          account: "CLA-KWD-1",
          company: "Kuwait Corp",
          account_number: "AR-KWD-01",
          credit_limit: 100,
          credit_days: 30,
          use_default_credit_days: true,
          account_credit_days: 30,
          billing_address: "Kuwait City",
          restricted: 0,
          restricted_reason: null,
          balance: 17.944,
          open_balance: 17.944,
          in_house: 0,
          available_credit: 82.056,
          unapplied_credit: 0,
          overdue: 0,
          not_due: 17.944,
          d1_30: 0,
          d31_60: 0,
          d61_90: 0,
          d90p: 0,
          open_items: [
            {
              folio: "FOL-KWD-SMALL",
              invoice_number: "INV-KWD-00002",
              folio_type: "Account",
              invoice_date: "2026-11-24",
              due_date: "2026-12-24",
              days_overdue: 0,
              billed: 0.004,
              paid: 0,
              outstanding: 0.004,
            },
          ],
          bills: [],
          payments: [],
          pending_payments: [],
          cashier_pin_required: false,
        })
      }
      if (method === "kamra.ledger.city_ledger_history") {
        return Promise.resolve({ entries: [] })
      }
      return Promise.resolve({})
    })

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

  it("RowCorrection uses step=0.001 for KWD, submits 0.001 partial refund cleanly, and rejects 0.0004", async () => {
    await act(async () =>
      root.render(
        <RowCorrection
          folio="FOLIO-2026-00041"
          row="PAY-KWD-1"
          home="KWD"
          currency="KWD"
          modes={[{ mode: "Cash", currencies: ["KWD"] }]}
          act={async (fn) => {
            await fn()
          }}
        />,
      ),
    )

    const openBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Correct"),
    ) as HTMLButtonElement
    await act(async () => openBtn.click())
    await vi.waitFor(() => expect(host.querySelector('input[aria-label="Refund amount"]')).toBeTruthy())

    const amtInput = host.querySelector('input[aria-label="Refund amount"]') as HTMLInputElement
    expect(amtInput.step).toBe("0.001")

    const noteInput = host.querySelector('input[aria-label="Reason note"]') as HTMLInputElement
    const supSelect = host.querySelector('select[aria-label="Supervisor user"]') as HTMLSelectElement
    const confirmBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Confirm correction"),
    ) as HTMLButtonElement

    // 1. Sub-fil 0.0004 MUST be rejected client-side without calling correct_folio_payment
    await act(async () => {
      setValue(noteInput, "Sub-fil check")
      setValue(supSelect, "sup@example.test")
      setValue(amtInput, "0.0004")
    })
    await act(async () => confirmBtn.click())
    const badAlert = host.querySelector('[role="alert"]')?.textContent ?? ""
    expect(badAlert).toContain("3")
    expect(rpc).not.toHaveBeenCalledWith("kamra.api.correct_folio_payment", expect.anything())

    // 2. Valid 0.001 KWD partial refund submits cleanly with zero validation error
    await act(async () => {
      setValue(amtInput, "0.001")
    })
    await act(async () => confirmBtn.click())
    await enterSupervisorPin(host, "8394")
    await vi.waitFor(() =>
      expect(rpc).toHaveBeenCalledWith(
        "kamra.api.correct_folio_payment",
        expect.objectContaining({
          folio: "FOLIO-2026-00041",
          payment_row: "PAY-KWD-1",
          action_type: "partial_refund",
          refund_amount: 0.001,
        }),
      ),
    )
    expect(host.querySelector('[role="alert"]')).toBeNull()
  })

  it("RowCorrection uses step=1 for 0dp UGX (rejects 0.5, accepts 1) and step=0.01 for 2dp USD (rejects 0.001, accepts 0.01)", async () => {
    // UGX 0dp
    activeLocale = makeLocale("UGX", "UGX ", [0, 18])
    await act(async () =>
      root.render(
        <RowCorrection
          key="ugx"
          folio="FOLIO-UGX"
          row="PAY-UGX-1"
          home="UGX"
          currency="UGX"
          modes={[{ mode: "Cash", currencies: ["UGX"] }]}
          act={async (fn) => {
            await fn()
          }}
        />,
      ),
    )
    const openUgx = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Correct"),
    ) as HTMLButtonElement
    await act(async () => openUgx.click())
    await vi.waitFor(() => expect(host.querySelector('input[aria-label="Refund amount"]')).toBeTruthy())

    const ugxAmt = host.querySelector('input[aria-label="Refund amount"]') as HTMLInputElement
    expect(ugxAmt.step).toBe("1")
    await act(async () => {
      setValue(host.querySelector('input[aria-label="Reason note"]') as HTMLInputElement, "UGX check")
      setValue(host.querySelector('select[aria-label="Supervisor user"]') as HTMLSelectElement, "sup@example.test")
      setValue(ugxAmt, "0.5")
    })
    const confirmUgx = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Confirm correction"),
    ) as HTMLButtonElement
    await act(async () => confirmUgx.click())
    expect(host.querySelector('[role="alert"]')).toBeTruthy()
    expect(rpc).not.toHaveBeenCalledWith("kamra.api.correct_folio_payment", expect.anything())

    await act(async () => setValue(ugxAmt, "1"))
    await act(async () => confirmUgx.click())
    await enterSupervisorPin(host, "8394")
    await vi.waitFor(() =>
      expect(rpc).toHaveBeenCalledWith(
        "kamra.api.correct_folio_payment",
        expect.objectContaining({ refund_amount: 1 }),
      ),
    )

    // USD 2dp
    rpc.mockClear()
    activeLocale = makeLocale("USD", "$", [0, 5])
    await act(async () =>
      root.render(
        <RowCorrection
          key="usd"
          folio="FOLIO-USD"
          row="PAY-USD-1"
          home="USD"
          currency="USD"
          modes={[{ mode: "Cash", currencies: ["USD"] }]}
          act={async (fn) => {
            await fn()
          }}
        />,
      ),
    )
    const openUsd = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Correct"),
    ) as HTMLButtonElement
    await act(async () => openUsd.click())
    await vi.waitFor(() => expect(host.querySelector('input[aria-label="Refund amount"]')).toBeTruthy())

    const usdAmt = host.querySelector('input[aria-label="Refund amount"]') as HTMLInputElement
    expect(usdAmt.step).toBe("0.01")
    await act(async () => {
      setValue(host.querySelector('input[aria-label="Reason note"]') as HTMLInputElement, "USD check")
      setValue(host.querySelector('select[aria-label="Supervisor user"]') as HTMLSelectElement, "sup@example.test")
      setValue(usdAmt, "0.001")
    })
    const confirmUsd = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Confirm correction"),
    ) as HTMLButtonElement
    await act(async () => confirmUsd.click())
    expect(host.querySelector('[role="alert"]')).toBeTruthy()
    expect(rpc).not.toHaveBeenCalledWith("kamra.api.correct_folio_payment", expect.anything())

    await act(async () => setValue(usdAmt, "0.01"))
    await act(async () => confirmUsd.click())
    await enterSupervisorPin(host, "8394")
    await vi.waitFor(() =>
      expect(rpc).toHaveBeenCalledWith(
        "kamra.api.correct_folio_payment",
        expect.objectContaining({ refund_amount: 0.01 }),
      ),
    )
  })

  it("Receivables NewInvoice & AccountSetup use step=0.001 for KWD and render 11.375 (never 11.380) and 5% VAT 11.944", async () => {
    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={["/receivables/CLA-KWD-1"]}>
          <Routes>
            <Route path="/receivables/:account" element={<Receivables />} />
          </Routes>
        </MemoryRouter>,
      ),
    )
    await vi.waitFor(() => expect(host.textContent).toContain("Kuwait Corp"))

    // Open New invoice modal
    const newInvBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("New invoice"),
    ) as HTMLButtonElement
    await act(async () => newInvBtn.click())
    await vi.waitFor(() => expect(host.querySelector('input[inputmode="decimal"]')).toBeTruthy())

    const priceInput = host.querySelector('input[inputmode="decimal"]') as HTMLInputElement
    expect(priceInput.step).toBe("0.001")

    // 11.375 KWD at 0% VAT MUST display د.ك11.375 everywhere (never 11.380)
    await act(async () => setValue(priceInput, "11.375"))
    await vi.waitFor(() => expect(host.textContent).toContain("Raise invoice for د.ك11.375"))
    const page0 = host.textContent ?? ""
    expect(page0).not.toContain("11.380")
    expect(page0).not.toMatch(/ERPNext|Frappe|URY|Kamra|HUF|HeyKoala|Tridz|BuildSuite/i)

    // 11.375 KWD at 5% VAT MUST display tax د.ك0.569 and total د.ك11.944
    const taxSelect = Array.from(host.querySelectorAll("select")).find((s) =>
      Array.from(s.options).some((o) => o.textContent === "5%"),
    ) as HTMLSelectElement
    await act(async () => setValue(taxSelect, "5"))
    await vi.waitFor(() => expect(host.textContent).toContain("Raise invoice for د.ك11.944"))
    expect(host.textContent).toContain("د.ك0.569")

    // Close New invoice and open Account setup: credit limit input uses step=0.001
    const cancelBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.trim() === "Cancel",
    ) as HTMLButtonElement
    await act(async () => cancelBtn.click())
    const setupBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.trim() === "Account setup",
    ) as HTMLButtonElement
    await act(async () => setupBtn.click())
    await vi.waitFor(() => expect(host.querySelector('input[placeholder="0 = no limit"]')).toBeTruthy())
    const limitInput = host.querySelector('input[placeholder="0 = no limit"]') as HTMLInputElement
    expect(limitInput.step).toBe("0.001")

    // Open invoice with 0.004 KWD balance stays visible as د.ك0.004 (not mislabeled Paid)
    expect(host.textContent).toContain("د.ك0.004")
  })
})
