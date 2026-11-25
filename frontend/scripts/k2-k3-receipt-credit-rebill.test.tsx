// @vitest-environment jsdom
/* RED->GREEN tests for K2 ReceiptTransferDialog & K3 CreditRebillDialog UX:
   - 0/2/3dp precision steps (UGX/USD/KWD)
   - Country-neutrality: EFRIS reason codes 102/104 strictly gated behind locale().integrations.tax_return
   - Four-eyes reminder & immediate Draft result display (credit_note + rebill)
   - Zero vendor leaks */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import FolioView from "../src/screens/FolioView"
import { setLang } from "../src/lib/dir"

const { rpc } = vi.hoisted(() => ({ rpc: vi.fn() }))
vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => "Test Hotel",
  newIdempotencyKey: () => "k2-k3-idem",
}))
const auth = vi.hoisted(() => ({ roles: ["Front Desk", "Finance", "Hotel Admin"] }))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: auth.roles }) }))

function makeLocale(currency: "KWD" | "USD" | "UGX", taxReturn: boolean) {
  return {
    currency,
    currency_symbol: currency === "USD" ? "$" : `${currency} `,
    locale: "en",
    tax_label: "VAT",
    tax_id_label: "Tax ID",
    tax_rates: [0, 18],
    payment_modes: ["Cash", "Card"],
    instant_payment_modes: [],
    outlet_payment_modes: ["Cash"],
    id_types: ["Passport"],
    id_type_hints: {},
    default_nationality: "",
    adult_age: 18,
    minor_ids: "foreign",
    integrations: {
      payment_gateways: [],
      tax_return: taxReturn,
      verify_tax_id: false,
      verify_guest_id: false,
    },
  }
}

function makeFolioData(opts: {
  currency: "KWD" | "USD" | "UGX"
  status: "Open" | "Closed"
  invoice_number?: string | null
  sales_invoice?: string | null
  payments?: Record<string, unknown>[]
  grand_total?: number
  tax_total?: number
  balance?: number
}) {
  return {
    folio: {
      name: "FOLIO-1",
      property: "Test Hotel",
      status: opts.status,
      folio_type: "Guest",
      window_no: 1,
      payee_type: "Guest",
      invoice_number: opts.invoice_number ?? null,
      sales_invoice: opts.sales_invoice ?? null,
      guest_name: "Tariq Mansoor",
      charges: [
        {
          name: "ch-1",
          posting_date: "2026-11-24",
          charge_type: "Room",
          description: "Suite Night",
          qty: 1,
          rate: opts.grand_total ?? 250,
          amount: opts.grand_total ?? 250,
          gst_rate: 0,
          gst_amount: opts.tax_total ?? 0,
          total: opts.grand_total ?? 250,
        },
      ],
      payments: opts.payments ?? [],
      charges_total: opts.grand_total ?? 250,
      tax_total: opts.tax_total ?? 0,
      grand_total: opts.grand_total ?? 250,
      payments_total: 0,
      balance: opts.balance ?? (opts.grand_total ?? 250),
    },
    property: {
      currency: opts.currency,
      name: "Test Hotel",
      legal_name: null,
      logo_url: null,
      address: "",
      state: null,
      gstin: null,
      phone: null,
      email: null,
      sac: null,
      place_of_supply: null,
      tax_label: "VAT",
    },
    stay: {
      reservation: "RES-1",
      check_in: "2026-11-24",
      check_out: "2026-11-26",
      arrival: null,
      departure: null,
      nights: 2,
      adults: 2,
      children: 0,
      pax: 2,
      meal_plan: null,
      room_type: null,
      room: null,
      company: null,
      group_booking: null,
      booked_by_name: null,
      booked_by_phone: null,
      contact_preference: null,
    },
    gst_summary: [],
    bill_to: null,
    statutory: null,
    document: {
      title: opts.status === "Closed" ? "Tax Invoice" : "Provisional Bill",
      is_final: opts.status === "Closed",
      number: opts.invoice_number ?? "FOLIO-1",
      date: "2026-11-24",
      amount_in_words: "",
      footer: null,
      service_code_label: "SAC",
    },
    lines: [],
    guest: {
      name: "Tariq Mansoor",
      phone: null,
      email: null,
      nationality: null,
      address: "",
      id_type: null,
    },
    summary_by_head: [],
    tax_summary: [],
  }
}

describe("K2 ReceiptTransferDialog & K3 CreditRebillDialog", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    auth.roles = ["Front Desk", "Finance", "Hotel Admin"]
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

  async function renderView() {
    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={["/billing/FOLIO-1"]}>
          <Routes>
            <Route path="/billing/:name" element={<FolioView />} />
          </Routes>
        </MemoryRouter>,
      ),
    )
    await vi.waitFor(() => expect(host.textContent).toContain("Tariq Mansoor"), { interval: 5, timeout: 5000 })
  }

  function setInput(el: HTMLInputElement | HTMLSelectElement, val: string) {
    const proto = Object.getPrototypeOf(el)
    const setter = Object.getOwnPropertyDescriptor(proto, "value")!.set!
    setter.call(el, val)
    el.dispatchEvent(new Event("input", { bubbles: true }))
    el.dispatchEvent(new Event("change", { bubbles: true }))
  }

  it("opens ReceiptTransferDialog from an open payment row with 3dp KWD step=0.001 and calls transfer_folio_payment", async () => {
    const data = makeFolioData({
      currency: "KWD",
      status: "Open",
      grand_total: 200,
      balance: 0,
      payments: [
        {
          name: "pay-1",
          posting_date: "2026-11-24",
          payment_kind: "Payment",
          mode: "Cash",
          amount: 200,
          reference: "CASH-01",
          currency: "KWD",
        },
      ],
    })
    const overview = {
      folio: "FOLIO-1",
      property: "Test Hotel",
      reservation: "RES-1",
      group_booking: null,
      window_cap: 8,
      can_add_window: true,
      windows: [
        { window_no: 1, folio: "FOLIO-1", folio_type: "Guest", payee_type: "Guest", payee_name: "Tariq Mansoor", status: "Open", charges_total: 200, grand_total: 200, payments_total: 200, balance: 0, routed_rule_count: 0 },
        { window_no: 2, folio: "FOLIO-2", folio_type: "Extra", payee_type: "Guest", payee_name: "Window 2", status: "Open", charges_total: 100, grand_total: 100, payments_total: 0, balance: 100, routed_rule_count: 0 },
      ],
    }

    rpc.mockReset().mockImplementation((method: string) => {
      if (method === "kamra.api.folio_invoice") return Promise.resolve(data)
      if (method === "kamra.payment_transfers.folio_windows_overview") return Promise.resolve(overview)
      if (method === "kamra.api.reservation_folios") return Promise.resolve([])
      if (method === "kamra.api.property_locale") return Promise.resolve(makeLocale("KWD", false))
      if (method === "kamra.payment_transfers.transfer_folio_payment") return Promise.resolve({ ok: true, amount: 75.125 })
      if (method === "kamra.ledger.list_reason_codes") return Promise.resolve([])
      if (method === "kamra.api.correct_charged_nights") return Promise.resolve({ nights: [] })
      if (method === "kamra.api.linked_records") return Promise.resolve(null)
      if (method === "kamra.api.allowance_requests") return Promise.resolve({ requests: [], can_decide: false })
      return Promise.resolve({})
    })

    await renderView()
    await vi.waitFor(() => expect(host.querySelector('[data-testid="transfer-payment-pay-1"]')).not.toBeNull())

    const trigger = host.querySelector<HTMLButtonElement>('[data-testid="transfer-payment-pay-1"]')!
    await act(async () => trigger.click())

    const dialog = host.querySelector<HTMLElement>('[role="dialog"][aria-label="Transfer receipt"]')!
    expect(dialog).not.toBeNull()

    const amountInput = dialog.querySelector<HTMLInputElement>('input[aria-label="Transfer amount"]')!
    expect(amountInput.step).toBe("0.001")

    const destSelect = dialog.querySelector<HTMLSelectElement>('select[aria-label="Destination window"]')!
    const reasonInput = dialog.querySelector<HTMLInputElement>('input[aria-label="Transfer reason"]')!

    await act(async () => {
      setInput(destSelect, "FOLIO-2")
      setInput(amountInput, "75.125")
      setInput(reasonInput, "Split deposit to W2")
    })

    const submit = dialog.querySelector<HTMLButtonElement>('[data-testid="confirm-receipt-transfer"]')!
    await act(async () => submit.click())

    expect(rpc).toHaveBeenCalledWith(
      "kamra.payment_transfers.transfer_folio_payment",
      expect.objectContaining({
        from_folio: "FOLIO-1",
        payment_row: "pay-1",
        to_folio: "FOLIO-2",
        amount: 75.125,
        reason: "Split deposit to W2",
      }),
    )
    expect(dialog.textContent).not.toMatch(/ERPNext|Frappe|Kamra|URY/i)
  })

  it("hides EFRIS reason codes in CreditRebillDialog when tax_return is false and shows Draft credit_note + rebill", async () => {
    const data = makeFolioData({
      currency: "USD",
      status: "Closed",
      invoice_number: "INV-USD-1",
      sales_invoice: "ACC-SINV-2026-00099",
      grand_total: 250,
      tax_total: 30,
      balance: 0,
    })

    rpc.mockReset().mockImplementation((method: string) => {
      if (method === "kamra.api.folio_invoice") return Promise.resolve(data)
      if (method === "kamra.payment_transfers.folio_windows_overview") return Promise.resolve({ folio: "FOLIO-1", window_cap: 8, can_add_window: false, windows: [] })
      if (method === "kamra.api.reservation_folios") return Promise.resolve([])
      if (method === "kamra.api.property_locale") return Promise.resolve(makeLocale("USD", false))
      if (method === "kamra.payment_transfers.credit_and_rebill") {
        return Promise.resolve({
          ok: 1,
          mode: "full",
          credit_note: "ACC-CN-2026-0001",
          rebill: "ACC-SINV-2026-00100",
          status: "Draft",
          message: "Credit note drafted for second approval.",
        })
      }
      if (method === "kamra.ledger.list_reason_codes") return Promise.resolve([])
      if (method === "kamra.api.correct_charged_nights") return Promise.resolve({ nights: [] })
      if (method === "kamra.api.linked_records") return Promise.resolve(null)
      return Promise.resolve({})
    })

    await renderView()
    const openBtn = host.querySelector<HTMLButtonElement>('[data-testid="open-credit-rebill-btn"]')!
    await act(async () => openBtn.click())

    const dialog = host.querySelector<HTMLElement>('[role="dialog"][aria-label="Credit & Rebill"]')!
    expect(dialog).not.toBeNull()
    // Country-neutral: tax_return=false -> ZERO EFRIS copy or 102/104 codes
    expect(dialog.textContent).not.toMatch(/EFRIS|\b102\b|\b104\b/i)
    // Four-eyes reminder shown
    expect(dialog.textContent).toMatch(/Second manager|Finance approval/i)
    // Zero vendor leaks
    expect(dialog.textContent).not.toMatch(/ERPNext|Frappe|Kamra|URY/i)

    const reasonInput = dialog.querySelector<HTMLInputElement>('input[aria-label="Credit & Rebill reason"]')!
    await act(async () => setInput(reasonInput, "Wrong corporate billing profile"))

    const submit = dialog.querySelector<HTMLButtonElement>('[data-testid="submit-credit-rebill"]')!
    await act(async () => submit.click())

    expect(rpc).toHaveBeenCalledWith(
      "kamra.payment_transfers.credit_and_rebill",
      expect.objectContaining({
        folio: "FOLIO-1",
        mode: "full",
        reason: "Wrong corporate billing profile",
      }),
    )
    await vi.waitFor(() => expect(dialog.textContent).toContain("ACC-CN-2026-0001"))
    expect(dialog.textContent).toContain("ACC-SINV-2026-00100")
  })

  it("shows EFRIS reason codes (102/104) ONLY when integrations.tax_return is true and uses step=1 for UGX 0dp partial credit", async () => {
    const data = makeFolioData({
      currency: "UGX",
      status: "Closed",
      invoice_number: "INV-UG-1",
      sales_invoice: "ACC-SINV-UG-1",
      grand_total: 400000,
      tax_total: 61017,
      balance: 0,
    })

    rpc.mockReset().mockImplementation((method: string) => {
      if (method === "kamra.api.folio_invoice") return Promise.resolve(data)
      if (method === "kamra.payment_transfers.folio_windows_overview") return Promise.resolve({ folio: "FOLIO-1", window_cap: 8, can_add_window: false, windows: [] })
      if (method === "kamra.api.reservation_folios") return Promise.resolve([])
      if (method === "kamra.api.property_locale") return Promise.resolve(makeLocale("UGX", true))
      if (method === "kamra.payment_transfers.credit_and_rebill") {
        return Promise.resolve({
          ok: 1,
          mode: "partial",
          credit_note: "ACC-CN-UG-0001",
          status: "Draft",
          message: "Partial credit note drafted.",
        })
      }
      if (method === "kamra.ledger.list_reason_codes") return Promise.resolve([])
      if (method === "kamra.api.correct_charged_nights") return Promise.resolve({ nights: [] })
      if (method === "kamra.api.linked_records") return Promise.resolve(null)
      return Promise.resolve({})
    })

    await renderView()
    const openBtn = host.querySelector<HTMLButtonElement>('[data-testid="open-credit-rebill-btn"]')!
    await act(async () => openBtn.click())

    const dialog = host.querySelector<HTMLElement>('[role="dialog"][aria-label="Credit & Rebill"]')!
    await vi.waitFor(() => expect(dialog.textContent).toContain("102"))

    const partialRadio = dialog.querySelector<HTMLInputElement>('input[type="radio"][value="partial"]')!
    await act(async () => partialRadio.click())
    expect(dialog.textContent).toContain("104")

    const amountInput = dialog.querySelector<HTMLInputElement>('input[aria-label="Partial credit amount"]')!
    expect(amountInput.step).toBe("1")
    const reasonInput = dialog.querySelector<HTMLInputElement>('input[aria-label="Credit & Rebill reason"]')!
    await act(async () => {
      setInput(amountInput, "50000")
      setInput(reasonInput, "Partial late checkout waiver")
    })

    const submit = dialog.querySelector<HTMLButtonElement>('[data-testid="submit-credit-rebill"]')!
    await act(async () => submit.click())

    expect(rpc).toHaveBeenCalledWith(
      "kamra.payment_transfers.credit_and_rebill",
      expect.objectContaining({
        folio: "FOLIO-1",
        mode: "partial",
        amount: 50000,
        reason: "Partial late checkout waiver",
      }),
    )
  })
})
