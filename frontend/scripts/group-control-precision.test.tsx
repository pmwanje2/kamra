// @vitest-environment jsdom
/**
 * KWD 3dp currency precision regression tests for GroupControl & FolioView:
 * - GroupControl formats KWD amounts to 3 decimal places (never truncating via maximumFractionDigits: 0)
 * - Sub-0.5 KWD balances (e.g. 0.250 KWD) remain visible and trigger "settle" / "Collect & check out"
 * - Settle group bill pre-fills exact 3dp string ("125.750", never Math.round -> "126")
 * - Monetary number inputs use dynamic moneyStep() ("0.001" for KWD) or step="any" (never default step=1 or hardcoded step="0.01")
 */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const { rpc } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: jsdom.window.localStorage,
  })
  return { rpc: vi.fn() }
})

vi.mock("../src/lib/api", async (original) => ({
  ...(await original<typeof import("../src/lib/api")>()),
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => "Kuwait Grand Hotel",
  frappeFetch: async (path: string) => ({ data: path.includes("?") ? [] : {} }),
  serverError: (error: Error) => error.message,
}))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: ["Hotel Admin"] }) }))
vi.mock("../src/AppShell", () => ({ default: () => <Outlet /> }))

import App from "../src/App"
import GroupControl from "../src/components/GroupControl"
import LinkedRecords from "../src/components/LinkedRecords"

const KWD_LOCALE = {
  currency: "KWD",
  currency_symbol: "KD ",
  locale: "en-US",
  tax_label: "VAT",
  tax_rates: [0],
  payment_modes: ["Cash", "Card"],
}

function makeGroupDetail(overrides: Record<string, unknown> = {}) {
  return {
    business_date: "2031-03-14",
    group: {
      name: "GRP-KWD-1",
      group_name: "Kuwait Summit",
      company: null,
      status: "Confirmed",
      check_in_date: "2031-03-12",
      check_out_date: "2031-03-14",
      cutoff_date: null,
      notes: null,
      group_type: "Corporate",
      master_reservation: "RES-KWD-1",
      master_pays: "Room only",
      in_house: 0,
      rooms_total: 1,
      attrition_pct: 0,
      deposit_due: 0,
    },
    pickup: [],
    money: {
      block_value: 125.75,
      booked: 125.75,
      deposits: 0,
      paid: 0,
      deposit_due: 0,
      due: 125.75,
    },
    attrition: {
      blocked_room_nights: 0,
      picked_room_nights: 0,
      pickup_pct: 100,
      allowed_pct: 0,
      shortfall_room_nights: 0,
      fee_estimate: 0,
      days_to_cutoff: 0,
      warn: false,
    },
    room_types: [{ name: "KWD-DLX", room_type_name: "Deluxe" }],
    can_set_rate: true,
    rooming_list: [
      {
        name: "RES-KWD-1",
        guest_name: "Nadia Al-Sabah",
        phone: null,
        room_type: "KWD-DLX",
        room_type_name: "Deluxe",
        room: "KWD-401",
        room_number: "401",
        status: "Checked Out",
        check_in_date: "2031-03-12",
        check_out_date: "2031-03-14",
        pays_own_charges: 0,
        is_master: true,
        own_balance: 0.25,
        amount: 125.75,
      },
    ],
    event: null,
    master_folio: "FOL-KWD-GRP",
    master_folio_status: "Open",
    master_folio_balance: 125.75,
    ...overrides,
  }
}

describe("KWD 3dp precision in GroupControl and FolioView", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    localStorage.clear()
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

  it("displays KWD 3dp amounts, treats 0.250 KWD as unpaid, pre-fills 125.750, and uses step='0.001'", async () => {
    rpc.mockReset().mockImplementation(async (method: string) => {
      if (method === "kamra.api.property_locale") return KWD_LOCALE
      if (method === "kamra.api.group_detail") return makeGroupDetail()
      if (method === "kamra.api.linked_records") return { folios: [], reservations: [] }
      return null
    })

    await act(async () =>
      root.render(
        <MemoryRouter>
          <GroupControl row={{ name: "GRP-KWD-1" }} reload={() => {}} />
        </MemoryRouter>,
      ),
    )

    await vi.waitFor(() => {
      expect(host.textContent).toContain("Kuwait Summit")
      expect(host.textContent).toContain("Settle group bill")
    })

    // 1. 3dp KWD balance must render with 3 decimal places, not rounded to 126
    expect(host.textContent).toContain("KD 125.750")

    // 2. Guest own_balance of 0.250 KWD (below legacy 0.5 threshold) must show KD 0.250, not "Nothing"
    expect(host.textContent).toContain("KD 0.250")

    // 3. Clicking Settle group bill pre-fills exact "125.750" (not "126") with dynamic KWD step="0.001"
    const settleBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Settle group bill"),
    )
    expect(settleBtn).toBeDefined()
    await act(async () => settleBtn!.click())

    const settleInput = host.querySelector<HTMLInputElement>('input[type="number"][value="125.750"]')
    expect(settleInput).not.toBeNull()
    expect(settleInput?.getAttribute("step")).toBe("0.001")
  })

  it("keeps group stage in 'settle' when master_folio_balance is 0.250 KWD", async () => {
    rpc.mockReset().mockImplementation(async (method: string) => {
      if (method === "kamra.api.property_locale") return KWD_LOCALE
      if (method === "kamra.api.group_detail") {
        return makeGroupDetail({
          money: { block_value: 0.25, booked: 0.25, deposits: 0, paid: 0, deposit_due: 0, due: 0.25 },
          master_folio_balance: 0.25,
        })
      }
      if (method === "kamra.api.linked_records") return { folios: [], reservations: [] }
      return null
    })

    await act(async () =>
      root.render(
        <MemoryRouter>
          <GroupControl row={{ name: "GRP-KWD-1" }} reload={() => {}} />
        </MemoryRouter>,
      ),
    )

    await vi.waitFor(() => expect(host.textContent).toContain("Kuwait Summit"))
    expect(host.textContent).toContain("KD 0.250")
    expect(host.textContent).not.toContain("All done")
    expect(host.textContent).toContain("Settle group bill")
  })

  it("uses step='any' on FolioView charge and allowance number inputs for 3dp KWD", async () => {
    const kwdInvoice = {
      folio: {
        name: "FOL-KWD-1", property: "Kuwait Grand Hotel", status: "Open", folio_type: "Guest",
        guest_name: "Nadia Al-Sabah", charges: [], payments: [],
        charges_total: 0, tax_total: 0, grand_total: 0, payments_total: 0, balance: 0,
      },
      property: {
        currency: "KWD", name: "Kuwait Grand Hotel", legal_name: null, logo_url: null,
        address: "", state: null, gstin: null, phone: null, email: null, sac: null, place_of_supply: null,
      },
      guest: { name: "Nadia Al-Sabah" },
      stay: {
        reservation: "RES-KWD-1", check_in: "2031-03-12", check_out: "2031-03-14",
        arrival: null, departure: null, nights: 2, adults: 1, children: 0, pax: 1,
        meal_plan: null, room_type: "KWD-DLX", room: null, company: null, tariff: null,
        agreed_rate: 50.125, reference_currency: null,
      },
      lines: [], summary_by_head: [], tax_summary: [], gst_summary: [], bill_to: null,
    }
    rpc.mockReset().mockImplementation(async (method: string) => {
      if (method === "kamra.api.folio_invoice") return kwdInvoice
      if (method === "kamra.api.property_locale") return KWD_LOCALE
      if (method === "kamra.ledger.list_exchange_rates") return { rates: [], business_date: "2031-03-12" }
      if (method === "kamra.cashier.my_till") return { open: true, property: "Kuwait Grand Hotel", terminal_links: [] }
      if (method === "kamra.api.linked_records") return { folios: [], reservations: [] }
      if (method === "kamra.api.correct_charged_nights") return { nights: [] }
      if (method === "kamra.api.payment_correction_options") return { actions: [], blocked: [], supervisors: [], refund_cap: 0 }
      return []
    })

    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={["/billing/FOL-KWD-1"]}>
          <App />
        </MemoryRouter>,
      ),
    )

    await vi.waitFor(() => {
      expect(host.querySelectorAll('input[type="number"]').length).toBeGreaterThanOrEqual(2)
    })

    const numInputs = Array.from(host.querySelectorAll<HTMLInputElement>('input[type="number"]'))
    for (const input of numInputs) {
      if (input.getAttribute("aria-label") === "Amount collected") {
        expect(input.getAttribute("step")).toBe("0.001")
      } else if (input.getAttribute("aria-label") === "Discount") {
        // FolioDiscount defaults to Percent mode (0.01%) and switches to moneyStep() ("0.001" for KWD) in Amount mode
        expect(input.getAttribute("step")).toBe("0.01")
      } else {
        expect(input.getAttribute("step")).toBe("any")
      }
    }
  })

  it("formats LinkedRecords folio balance chips to 3dp KWD (never truncating 0.255 to 0 or 125.750 to 126)", async () => {
    rpc.mockReset().mockImplementation(async (method: string) => {
      if (method === "kamra.api.property_locale") return KWD_LOCALE
      if (method === "kamra.api.linked_records") {
        return {
          guest: "G-1",
          guest_name: "Nadia Al-Sabah",
          reservations: [],
          folios: [
            { name: "FOL-1", folio_type: "Guest", status: "Open", balance: 0.255, invoice_number: null },
            { name: "FOL-2", folio_type: "Company", status: "Open", balance: 125.75, invoice_number: null },
          ],
          company: "Al-Sabah Energy Holdings",
          group_booking: null,
          group_name: null,
          event: null,
          event_type: null,
        }
      }
      return null
    })
    localStorage.setItem("kamra_locale", JSON.stringify(KWD_LOCALE))

    await act(async () =>
      root.render(
        <MemoryRouter>
          <LinkedRecords doctype="Reservation" name="RES-KWD-1" />
        </MemoryRouter>,
      ),
    )

    await vi.waitFor(() => {
      expect(host.textContent).toContain("Nadia Al-Sabah")
    })
    expect(host.textContent).toContain("KD 0.255")
    expect(host.textContent).toContain("KD 125.750")
    expect(host.textContent).not.toContain("KD 126")
  })

  it("renders FolioView payment row as KWD 50.125 and fills 100.125 on Full amount click", async () => {
    const kwdInvoiceWithPayment = {
      folio: {
        name: "FOL-KWD-PAY", property: "Kuwait Grand Hotel", status: "Open", folio_type: "Guest",
        guest_name: "Nadia Al-Sabah",
        charges: [
          {
            name: "FC-1", posting_date: "2031-03-12", charge_type: "Room",
            description: "Deluxe Night", qty: 1, rate: 150.25, amount: 150.25,
            gst_rate: 0, gst_amount: 0, total: 150.25,
          },
        ],
        payments: [
          {
            name: "FP-1", payment_date: "2031-03-12", mode: "Cash",
            payment_kind: "Payment", amount: 50.125, reference: "REF-1",
          },
        ],
        charges_total: 150.25, tax_total: 0, grand_total: 150.25,
        payments_total: 50.125, balance: 100.125,
      },
      property: {
        currency: "KWD", name: "Kuwait Grand Hotel", legal_name: null, logo_url: null,
        address: "", state: null, gstin: null, phone: null, email: null, sac: null, place_of_supply: null,
      },
      guest: { name: "Nadia Al-Sabah" },
      stay: {
        reservation: "RES-KWD-1", check_in: "2031-03-12", check_out: "2031-03-14",
        arrival: null, departure: null, nights: 2, adults: 1, children: 0, pax: 1,
        meal_plan: null, room_type: "KWD-DLX", room: null, company: null, tariff: null,
        agreed_rate: 150.25, reference_currency: null,
      },
      lines: [], summary_by_head: [], tax_summary: [], gst_summary: [], bill_to: null,
    }
    rpc.mockReset().mockImplementation(async (method: string) => {
      if (method === "kamra.api.folio_invoice") return kwdInvoiceWithPayment
      if (method === "kamra.api.property_locale") return KWD_LOCALE
      if (method === "kamra.ledger.list_exchange_rates") return { rates: [], business_date: "2031-03-12" }
      if (method === "kamra.cashier.my_till") return { open: true, property: "Kuwait Grand Hotel", terminal_links: [] }
      if (method === "kamra.api.linked_records") return { folios: [], reservations: [] }
      if (method === "kamra.api.correct_charged_nights") return { nights: [] }
      if (method === "kamra.api.payment_correction_options") return { actions: [], blocked: [], supervisors: [], refund_cap: 0 }
      return []
    })

    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={["/billing/FOL-KWD-PAY"]}>
          <App />
        </MemoryRouter>,
      ),
    )

    await vi.waitFor(() => {
      expect(host.textContent).toContain("KWD 50.125")
    })
    expect(host.textContent).not.toContain("KWD 50.13")

    const fullBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.trim() === "Full amount",
    )
    expect(fullBtn).toBeDefined()
    await act(async () => fullBtn!.click())

    const amtInput = host.querySelector<HTMLInputElement>('input[aria-label="Amount collected"]')
    expect(amtInput).not.toBeNull()
    expect(amtInput?.value).toBe("100.125")
    expect(amtInput?.value).not.toBe("100.13")
    expect(amtInput?.getAttribute("step")).toBe("0.001")
  })
})
