// @vitest-environment jsdom
/** R2 precision regression suite for PR #56 (Hydrogen R1 blockers + P2 follow-ups):
 *  - DiscountPanel: Amount inputs use moneyStep() ("1" UGX 0dp, "0.01" USD 2dp, "0.001" KWD 3dp)
 *  - GroupControl: openSettle uses roundMoney() (preserves KWD 3dp), money inputs carry moneyStep(),
 *    and balance checks use moneyEps() (no hardcoded 0.5)
 *  - agreedRate: agreedRateRows preserves KWD 3dp night diffs (no * 100 / 100)
 *  - splitPayment & CheckOutDialog: dynamic roundMoney() / moneyEps() / moneyStep() on 0/2/3dp
 *  - ContractTariff: honors activeMoneyLocale() for non-English UI languages
 *  - Billing: zero-balance cells format via fmtMoney(0) (matching currency precision) */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const { rpc, frappeFetchMock } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return { rpc: vi.fn(), frappeFetchMock: vi.fn() }
})

vi.mock("../src/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api")>()
  return {
    ...actual,
    call: (method: string, params?: Record<string, unknown>) => rpc(method, params ?? {}),
    frappeFetch: (path: string, init?: RequestInit) => frappeFetchMock(path, init),
    getCurrentProperty: () => "Test Hotel",
  }
})
vi.mock("../src/lib/realtime", () => ({
  useRealtime: () => {},
}))
vi.mock("../src/lib/cashierAuth", () => ({
  useCashierAuth: () => ({ ensureUnlocked: vi.fn(), status: { required: false } }),
}))

import DiscountPanel from "../src/components/DiscountPanel"
import GroupControl from "../src/components/GroupControl"
import CheckOutDialog, { type CheckoutContext } from "../src/components/CheckOutDialog"
import { ReferenceCurrencyNote, TariffNight } from "../src/components/ContractTariff"
import Billing from "../src/screens/Billing"
import { agreedRateRows } from "../src/lib/agreedRate"
import { allocate, splitProblem, splitTotals, type PayLine } from "../src/lib/splitPayment"
import { emptyDiscount } from "../src/lib/reservationDiscount"
import { adoptUiLocale, fmtMoney } from "../src/lib/money"
import { setLang } from "../src/lib/dir"

const CURRENCY_DECIMALS = { UGX: 0, USD: 2, EUR: 2, KWD: 3, BHD: 3 }

function setCurrency(currency: "UGX" | "USD" | "KWD") {
  const specs = {
    UGX: { currency: "UGX", currency_symbol: "UGX ", locale: "en-UG", decimals: 0 },
    USD: { currency: "USD", currency_symbol: "$", locale: "en-GB", decimals: 2 },
    KWD: { currency: "KWD", currency_symbol: "KWD ", locale: "en-GB", decimals: 3 },
  }[currency]
  adoptUiLocale({ ...specs, currency_decimals: CURRENCY_DECIMALS })
  return specs
}

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  localStorage.clear()
  setLang("en")
  rpc.mockReset()
  frappeFetchMock.mockReset()
  host = document.createElement("div")
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  setLang("en")
  localStorage.clear()
  vi.unstubAllGlobals()
})

const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 0)))
const until = (check: () => void | Promise<void>) =>
  vi.waitFor(async () => { await settle(); await check() }, { timeout: 3000, interval: 20 })

async function clickButton(label: string) {
  const btn = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.trim() === label)
  expect(btn, `Expected button "${label}"`).toBeDefined()
  await act(async () => btn!.click())
}

describe("Blocker 1: DiscountPanel dynamic currency step across 0/2/3dp", () => {
  const reasons = [{ name: "RATE-PROMO", code: "RATE-PROMO", description: "Promo", category: "Rate Change", requires_supervisor: 0 }]

  it.each([
    ["UGX", "1"],
    ["USD", "0.01"],
    ["KWD", "0.001"],
  ] as const)("uses %s (step=%s) for Amount discounts (stay-wide and per-night)", async (currency, expectedStep) => {
    setCurrency(currency)
    await act(async () => root.render(
      <DiscountPanel
        value={{ ...emptyDiscount(), type: "Amount", value: "10", reason: "RATE-PROMO" }}
        onChange={() => {}}
        reasons={reasons}
        eligible={true}
        governance={null}
        nights={[{ date: "2026-12-01", base_rate: 100 }]}
        supervisor=""
        pin=""
        onSupervisor={() => {}}
        onPin={() => {}}
        money={(n) => fmtMoney(n)}
      />,
    ))

    const stayInput = host.querySelector<HTMLInputElement>('input[aria-label="Discount value"]')!
    expect(stayInput.getAttribute("step")).toBe(expectedStep)

    await clickButton("Per-night daily details")
    const nightInput = host.querySelector<HTMLInputElement>('tr[data-night="2026-12-01"] input[type="number"]')!
    expect(nightInput.getAttribute("step")).toBe(expectedStep)
  })

  it("keeps step=0.01 for Percent discounts even on UGX (0dp)", async () => {
    setCurrency("UGX")
    await act(async () => root.render(
      <DiscountPanel
        value={{ ...emptyDiscount(), type: "Percent", value: "10", reason: "RATE-PROMO" }}
        onChange={() => {}}
        reasons={reasons}
        eligible={true}
        governance={null}
        nights={[{ date: "2026-12-01", base_rate: 150000 }]}
        supervisor=""
        pin=""
        onSupervisor={() => {}}
        onPin={() => {}}
        money={(n) => fmtMoney(n)}
      />,
    ))

    const stayInput = host.querySelector<HTMLInputElement>('input[aria-label="Discount value"]')!
    expect(stayInput.getAttribute("step")).toBe("0.01")
  })
})

describe("Blocker 2: GroupControl precision pre-fill, money steps, and moneyEps thresholds", () => {
  function mockGroup(currency: "UGX" | "KWD", opts: {
    masterBalance: number
    guestBalance?: number
    guestStatus?: string
    company?: string | null
  }) {
    const spec = setCurrency(currency)
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.property_locale") {
        return Promise.resolve({
          ...spec,
          currency_decimals: CURRENCY_DECIMALS,
          tax_label: "VAT",
          tax_rates: [0],
          payment_modes: ["Cash", "Card"],
        })
      }
      if (method === "kamra.api.group_detail") {
        return Promise.resolve({
          business_date: "2026-12-05",
          group: {
            name: "GRP-105", group_name: "Kuwait Royal Delegation", company: opts.company ?? null,
            status: "Confirmed", check_in_date: "2026-12-01", check_out_date: "2026-12-03",
            cutoff_date: null, notes: null, group_type: "Corporate",
            master_reservation: "RES-1", master_pays: "Room & Tax", in_house: opts.guestStatus === "Checked In" ? 1 : 0,
            rooms_total: 1, attrition_pct: 0, deposit_due: 50.125,
          },
          pickup: [{
            room_type: "DLX", room_type_name: "Deluxe", rooms_blocked: 1, block_rate: 47.673,
            nightly_counts: {}, picked_up: 1, remaining: 0,
          }],
          money: { block_value: opts.masterBalance, booked: opts.masterBalance, deposits: 10.005, paid: 10.005, deposit_due: 50.125, due: opts.masterBalance },
          attrition: {
            blocked_room_nights: 2, picked_room_nights: 2, pickup_pct: 100, allowed_pct: 0,
            shortfall_room_nights: 0, fee_estimate: 0, days_to_cutoff: 0, warn: false,
          },
          room_types: [{ name: "DLX", room_type_name: "Deluxe" }],
          can_set_rate: true,
          rooming_list: [{
            name: "RES-1", guest_name: "Nadia Al-Sabah", phone: null, room_type: "DLX",
            room_type_name: "Deluxe", room: "RM-101", room_number: "101",
            status: opts.guestStatus ?? "Checked Out",
            check_in_date: "2026-12-01", check_out_date: "2026-12-03", pays_own_charges: 0,
            is_master: true, own_balance: opts.guestBalance ?? 0, amount: opts.masterBalance,
          }],
          event: null,
          master_folio: "FOLIO-109",
          master_folio_status: "Open",
          master_folio_balance: opts.masterBalance,
          comp_governance: null,
        })
      }
      return Promise.resolve(null)
    })
  }

  it("preserves KWD 3dp (95.345) on Settle group bill and sets step=0.001 on all money inputs", async () => {
    mockGroup("KWD", { masterBalance: 95.345 })
    await act(async () => root.render(
      <MemoryRouter><GroupControl row={{ name: "GRP-105" }} reload={() => {}} /></MemoryRouter>,
    ))
    await until(() => expect(host.textContent).toContain("95.345"))

    // Deposit due input on Bill tab carries step="0.001"
    const billInputsBefore = Array.from(host.querySelectorAll<HTMLInputElement>('input[type="number"]'))
    expect(billInputsBefore.length).toBeGreaterThanOrEqual(1)
    expect(billInputsBefore[0].getAttribute("step")).toBe("0.001")

    // Take deposit opens deposit input with step="0.001" and 3dp rounded remaining deposit (40.12)
    await clickButton("Take deposit")
    const depositInput = host.querySelector<HTMLInputElement>('input[aria-label="Deposit amount"]')!
    expect(depositInput.getAttribute("step")).toBe("0.001")
    expect(depositInput.value).toBe("40.12")

    // Settle group bill pre-fills exact 95.345 (not 95!) with step="0.001"
    await clickButton("Settle group bill")
    const settleInput = Array.from(host.querySelectorAll<HTMLInputElement>('input[type="number"]'))
      .find((el) => el.value === "95.345" || el.value === "95")!
    expect(settleInput).toBeDefined()
    expect(settleInput.value).toBe("95.345")
    expect(settleInput.getAttribute("step")).toBe("0.001")

    // Rooms tab Group rate / night input carries step="0.001"
    const roomsTab = Array.from(host.querySelectorAll<HTMLButtonElement>('button[role="tab"]'))
      .find((b) => b.textContent?.trim() === "Rooms")!
    await act(async () => roomsTab.click())
    const rateInput = host.querySelector<HTMLInputElement>('input[placeholder="Normal price"]')!
    expect(rateInput.getAttribute("step")).toBe("0.001")
  })

  it("treats sub-0.5 KWD balances (0.345 group due, 0.250 guest balance) as unpaid using moneyEps()", async () => {
    mockGroup("KWD", { masterBalance: 0.345, guestBalance: 0.250, guestStatus: "Checked In" })
    await act(async () => root.render(
      <MemoryRouter><GroupControl row={{ name: "GRP-105" }} reload={() => {}} /></MemoryRouter>,
    ))
    await until(() => expect(host.textContent).toContain("Room 101"))

    // Guests tab shows guest owes 0.250 and offers "Collect & check out"
    expect(host.textContent).toContain("0.250")
    expect(host.textContent).toContain("Collect & check out")

    // Bill tab still offers "Settle group bill" for 0.345 KWD
    const billTab = Array.from(host.querySelectorAll<HTMLButtonElement>('button[role="tab"]'))
      .find((b) => b.textContent?.trim() === "Bill")!
    await act(async () => billTab.click())
    expect(host.textContent).toContain("Settle group bill")
  })

  it("uses step=1 and whole-unit pre-fill on UGX (0dp)", async () => {
    mockGroup("UGX", { masterBalance: 50000 })
    await act(async () => root.render(
      <MemoryRouter><GroupControl row={{ name: "GRP-105" }} reload={() => {}} /></MemoryRouter>,
    ))
    await until(() => expect(host.textContent).toContain("50,000"))

    await clickButton("Settle group bill")
    const settleInput = Array.from(host.querySelectorAll<HTMLInputElement>('input[type="number"]'))
      .find((el) => el.value === "50000")!
    expect(settleInput).toBeDefined()
    expect(settleInput.getAttribute("step")).toBe("1")
  })
})

describe("Blocker 3: agreedRateRows preserves dynamic currency precision", () => {
  it("preserves KWD 3dp night diffs (45.750 -> 45.754 yields +0.004, not 0)", () => {
    setCurrency("KWD")
    expect(
      agreedRateRows(
        [{ date: "2026-12-01", rate: 45.750 }],
        { nights: [{ date: "2026-12-01", rate: 45.754 }] },
      ),
    ).toEqual([{ date: "2026-12-01", was: 45.75, now: 45.754, diff: 0.004 }])

    expect(
      agreedRateRows(
        [{ date: "2026-12-01", rate: 45.750 }],
        { nights: [{ date: "2026-12-01", rate: 45.746 }] },
      ),
    ).toEqual([{ date: "2026-12-01", was: 45.75, now: 45.746, diff: -0.004 }])
  })

  it("rounds UGX 0dp diffs to whole units", () => {
    setCurrency("UGX")
    expect(
      agreedRateRows(
        [{ date: "2026-12-01", rate: 150000 }],
        { nights: [{ date: "2026-12-01", rate: 149500 }] },
      ),
    ).toEqual([{ date: "2026-12-01", was: 150000, now: 149500, diff: -500 }])
  })
})

describe("Blocker 4: splitPayment & CheckOutDialog dynamic 0/2/3dp precision", () => {
  const line = (amount: string, mode = "Cash", reference = "", key = "k1"): PayLine => ({
    id: key, key, mode, amount, reference,
  })

  it("preserves KWD 3dp in splitTotals, splitProblem, and allocate without overpaying 5 fils", () => {
    setCurrency("KWD")
    expect(splitTotals([line("10.005")], 10.005)).toEqual({ entered: 10.005, left: 0 })
    expect(splitProblem([line("10.005")], 10.005, (s) => s)).toBeNull()
    expect(allocate([line("10.005")], [{ folio: "F-1", amount: 10.005 }])).toEqual([
      { folio: "F-1", mode: "Cash", amount: 10.005, kind: "Payment", reference: undefined, idempotency_key: "k1:F-1" },
    ])

    // 5 fils and 4 fils shortfalls are both detected accurately (not masked by 0.009/cents)
    expect(splitTotals([line("45.750")], 45.755)).toEqual({ entered: 45.75, left: 0.005 })
    expect(splitProblem([line("45.750")], 45.754, (s) => s)).toBe("The lines do not cover the balance yet.")

    // Multi-folio KWD 3dp split allocation
    const split = [line("45.753", "Cash", "", "k1"), line("49.592", "Card", "AUTH-9", "k2")]
    expect(splitTotals(split, 95.345)).toEqual({ entered: 95.345, left: 0 })
    expect(splitProblem(split, 95.345, (s) => s)).toBeNull()
    expect(allocate(split, [
      { folio: "F-1", amount: 50.123 },
      { folio: "F-2", amount: 45.222 },
    ])).toEqual([
      { folio: "F-1", mode: "Cash", amount: 45.753, kind: "Payment", reference: undefined, idempotency_key: "k1:F-1" },
      { folio: "F-1", mode: "Card", amount: 4.37, kind: "Payment", reference: "AUTH-9", idempotency_key: "k2:F-1" },
      { folio: "F-2", mode: "Card", amount: 45.222, kind: "Payment", reference: "AUTH-9", idempotency_key: "k2:F-2" },
    ])
  })

  it("handles UGX (0dp) and USD (2dp) splits cleanly", () => {
    setCurrency("UGX")
    const ugx = [line("50000", "Cash", "", "u1"), line("25000", "Card", "REF1", "u2")]
    expect(splitTotals(ugx, 75000)).toEqual({ entered: 75000, left: 0 })
    expect(splitProblem(ugx, 75000, (s) => s)).toBeNull()

    setCurrency("USD")
    const usd = [line("10.05", "Cash", "", "d1"), line("20.10", "Card", "REF2", "d2")]
    expect(splitTotals(usd, 30.15)).toEqual({ entered: 30.15, left: 0 })
    expect(splitProblem(usd, 30.15, (s) => s)).toBeNull()
  })

  function checkoutCtx(currency: "UGX" | "KWD", overrides: Partial<CheckoutContext> = {}): CheckoutContext {
    const sym = currency === "KWD" ? "KWD " : "UGX "
    const amt = currency === "KWD" ? 10.005 : 50000
    return {
      reservation: {
        name: "RES-CO", status: "Checked In", guest: "G-1", guest_name: "Tariq Guest",
        company: null, company_name: null, group_booking: null,
        room: "RM-101", room_number: "101", room_type_name: "Deluxe",
        check_in_date: "2026-12-01", check_out_date: "2026-12-03",
      },
      currency_symbol: sym,
      payment_modes: ["Cash", "Card"],
      pin_required: false,
      till_open: true,
      till: { open: true, can_open: true, who_can_open: [], terminal: "Front Desk", session: "CS-1", opened_at: null, opening_float: 0 },
      folios: [{
        folio: "F-CO", name: "F-CO", folio_type: "Guest", status: "Open", invoice_number: null,
        balance: amt, grand_total: amt, paid_total: 0, refundable: 0,
        city_ledger_company: null, side: "guest", has_charges: true, charges: [],
      }],
      groups: {},
      totals: { charges: amt, paid: 0, balance: amt, owed: amt, refund_due: 0, to_city_ledger: 0 },
      billing: { company: null, company_name: null, rules: [] },
      city_ledger: [],
      credit: null,
      deposit: { name: "DEP-1", status: "Held", held: currency === "KWD" ? 5.125 : 20000, folio: "F-CO" },
      collect: [{ folio: "F-CO", folio_type: "Guest", amount: amt, invoiced: false }],
      refunds: [],
      pending_bills: [],
      late_checkout: {
        checkout_time: "11:00", now: "13:30", late: true, requested: false, already_charged: false,
        folio: "F-CO", tax_rate: 0, fee_mode: "Fixed", fee_value: currency === "KWD" ? 2.505 : 10000,
      },
      early_departure: null,
      companions: [],
      blockers: [],
      notices: [],
      settled: false,
      actions: { can_checkout: true, can_withhold_deposit: true, can_check_out_role: true },
      sop: { steps: [], next_tip: null },
      ...overrides,
    }
  }

  it("renders dynamic step=0.001 on CheckOutDialog inputs (KWD) and rejects 0.005 KWD folio from readyToInvoice", async () => {
    const spec = setCurrency("KWD")
    const ctx = checkoutCtx("KWD")
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.property_locale") return Promise.resolve({ ...spec, currency_decimals: CURRENCY_DECIMALS, tax_label: "VAT", tax_rates: [0], payment_modes: ["Cash", "Card"] })
      if (method === "kamra.api.checkout_context") return Promise.resolve(ctx)
      return Promise.resolve(null)
    })

    await act(async () => root.render(
      <CheckOutDialog reservation="RES-CO" onClose={() => {}} onDone={() => {}} />,
    ))
    await until(() => expect(host.textContent).toContain("Tariq Guest"))

    // Navigate to Late check-out step and verify step="0.001"
    const lateTab = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.includes("Late check-out"))!
    await act(async () => lateTab.click())
    const lateInput = host.querySelector<HTMLInputElement>('input[type="number"]')!
    expect(lateInput.getAttribute("step")).toBe("0.001")

    // Navigate to Payment step and verify split payment & withhold inputs have step="0.001" and exact 10.005 total
    const payTab = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.includes("Payment"))!
    await act(async () => payTab.click())
    const numInputs = Array.from(host.querySelectorAll<HTMLInputElement>('input[type="number"]'))
    expect(numInputs.length).toBeGreaterThanOrEqual(2)
    expect(numInputs[0].value).toBe("10.005")
    expect(numInputs[0].getAttribute("step")).toBe("0.001")
    expect(numInputs[1].getAttribute("step")).toBe("0.001")
    expect(host.textContent).toContain("Entered KWD 10.005 · Left KWD 0.000")

    // Unmount and render checked-out KWD stay with 0.005 balance: must NOT be readyToInvoice (0.005 > 0.0005)
    await act(async () => root.unmount())
    root = createRoot(host)
    const unpaidCtx = checkoutCtx("KWD", {
      reservation: { ...ctx.reservation, status: "Checked Out" },
      folios: [{ ...ctx.folios[0], balance: 0.005 }],
      collect: [],
      settled: false,
    })
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.property_locale") return Promise.resolve({ ...spec, currency_decimals: CURRENCY_DECIMALS, tax_label: "VAT", tax_rates: [0], payment_modes: ["Cash", "Card"] })
      if (method === "kamra.api.checkout_context") return Promise.resolve(unpaidCtx)
      return Promise.resolve(null)
    })
    await act(async () => root.render(
      <CheckOutDialog reservation="RES-CO" onClose={() => {}} onDone={() => {}} />,
    ))
    await until(() => expect(host.textContent).toContain("Tariq Guest"))
    expect(host.textContent).not.toContain("Generate invoices & finish")
  })

  it("renders dynamic step=1 on CheckOutDialog payment inputs for UGX (0dp)", async () => {
    const spec = setCurrency("UGX")
    const ctx = checkoutCtx("UGX")
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.property_locale") return Promise.resolve({ ...spec, currency_decimals: CURRENCY_DECIMALS, tax_label: "VAT", tax_rates: [0], payment_modes: ["Cash", "Card"] })
      if (method === "kamra.api.checkout_context") return Promise.resolve(ctx)
      return Promise.resolve(null)
    })

    await act(async () => root.render(
      <CheckOutDialog reservation="RES-CO" onClose={() => {}} onDone={() => {}} />,
    ))
    await until(() => expect(host.textContent).toContain("Tariq Guest"))

    const payTab = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.includes("Payment"))!
    await act(async () => payTab.click())
    const payInput = host.querySelector<HTMLInputElement>('input[type="number"]')!
    expect(payInput.getAttribute("step")).toBe("1")
  })
})

describe("Quick P2 fixes: ContractTariff activeMoneyLocale & Billing zero-balance precision", () => {
  it("formats ContractTariff amounts using activeMoneyLocale() when UI language is German (de)", async () => {
    setCurrency("USD")
    setLang("de")
    await act(async () => root.render(
      <div>
        <TariffNight
          tariff={{ currency: "USD", nightly: 1234.5, fx_rate: 3650, fx_date: "2026-12-20", property_currency: "UGX" }}
          agreed_rate={4505925}
        />
        <ReferenceCurrencyNote
          reference={{
            currency: "USD", home_currency: "UGX", calculation: "ACTUAL",
            fx_rate: 3650, fx_date: "2026-12-20", grand_total_reference: 1234.5, balance_reference: 260.25,
          }}
        />
      </div>,
    ))
    expect(host.textContent).toContain("USD 1.234,50")
    expect(host.textContent).toContain("USD 260,25")
  })

  it("formats zero-balance folios on Billing screen with full currency precision (KWD 0.000)", async () => {
    const spec = setCurrency("KWD")
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.property_locale") return Promise.resolve({ ...spec, currency_decimals: CURRENCY_DECIMALS, tax_label: "VAT", tax_rates: [0], payment_modes: ["Cash"] })
      if (method === "kamra.api.accounting_bridge_status") return Promise.resolve({ erp_kept_books: false })
      if (method === "kamra.api.billing_folios") return Promise.resolve([{
        name: "FOLIO-ZERO", guest_name: "Settled Guest", status: "Closed", invoice_number: "INV-1",
        grand_total: 48.095, payments_total: 48.095, balance: 0, net_collectible_balance: 0,
      }])
      return Promise.resolve([])
    })
    await act(async () => root.render(
      <MemoryRouter><Billing /></MemoryRouter>,
    ))
    await until(() => expect(host.textContent).toContain("FOLIO-ZERO"))
    expect(host.textContent).toContain("KWD 0.000")
  })
})
