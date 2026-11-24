// @vitest-environment jsdom
/** 3-decimal ISO 4217 (KWD) precision on FolioView:
 * - Payments list renders exact 3-dp amounts (11.944 and -5.944, never 11.94 / -5.94).
 * - Amount-in-words displays 3-dp minor units ("KWD Twenty Three point 888 Only", never "point 89").
 * - No user-visible vendor/OSS brand wording. */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import FolioView from "../src/screens/FolioView"
import { setLang } from "../src/lib/dir"

const { rpc } = vi.hoisted(() => ({ rpc: vi.fn() }))
vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => "Kuwait Bay Hotel",
  newIdempotencyKey: () => "key-kwd",
}))
vi.mock("../src/lib/auth", () => ({
  useAuth: () => ({ status: "authed", roles: ["Front Desk", "Finance"] }),
}))

function kwdPayload() {
  return {
    folio: {
      name: "FOLIO-2026-00334",
      property: "Kuwait Bay Hotel",
      status: "Open",
      folio_type: "Guest",
      invoice_number: null,
      sales_invoice: null,
      guest_name: "Nasser Al-Sabah",
      charges: [
        {
          name: "ch-1",
          posting_date: "2026-11-24",
          charge_type: "Room",
          description: "Night 1",
          amount: 11.375,
          gst_rate: 5,
          gst_amount: 0.56875,
          total: 11.94375,
        },
        {
          name: "ch-2",
          posting_date: "2026-11-24",
          charge_type: "Room",
          description: "Night 2",
          amount: 11.375,
          gst_rate: 5,
          gst_amount: 0.56875,
          total: 11.94375,
        },
      ],
      payments: [
        {
          name: "pay-1",
          posting_date: "2026-11-24",
          payment_kind: "Payment",
          mode: "Cash",
          amount: 11.944,
          reference: "CASH-KWD-1",
          currency: "KWD",
        },
        {
          name: "pay-2",
          posting_date: "2026-11-24",
          payment_kind: "Refund",
          mode: "Cash",
          amount: -5.944,
          reference: "REF-KWD-1",
          currency: "KWD",
        },
      ],
      charges_total: 22.75,
      tax_total: 1.138,
      grand_total: 23.888,
      payments_total: 6.0,
      balance: 17.888,
    },
    property: {
      currency: "KWD",
      name: "Kuwait Bay Hotel",
      legal_name: null,
      logo_url: null,
      address: "Kuwait City",
      state: null,
      gstin: null,
      phone: null,
      email: null,
      sac: null,
      place_of_supply: "Kuwait City",
      tax_label: "VAT",
      tax_id_label: "Tax ID",
    },
    stay: {
      reservation: "RES-2026-00047",
      check_in: "2026-11-24",
      check_out: "2026-11-26",
      arrival: "2026-11-24 14:00:00",
      departure: null,
      nights: 2,
      adults: 2,
      children: 0,
      pax: 2,
      meal_plan: null,
      room_type: "Kuwait Bay Hotel-DLX",
      room: "RM-201",
      room_number: "201",
      company: null,
      group_booking: null,
      booked_by_name: null,
      booked_by_phone: null,
      contact_preference: null,
    },
    bill_to: null,
    statutory: null,
    document: {
      title: "Provisional Bill",
      is_final: false,
      number: "FOLIO-2026-00334",
      date: "2026-11-24",
      amount_in_words: "KWD Twenty Three point 888 Only",
      footer: "This is a computer-generated invoice.",
      service_code_label: "SAC",
    },
    lines: [
      {
        row: "ch-1",
        posting_date: "2026-11-24",
        charge_type: "Room",
        description: "Night 1",
        qty: 1,
        rate: 11.375,
        amount: 11.375,
        gst_rate: 5,
        gst_amount: 0.56875,
        total: 11.94375,
        service_code: null,
      },
      {
        row: "ch-2",
        posting_date: "2026-11-24",
        charge_type: "Room",
        description: "Night 2",
        qty: 1,
        rate: 11.375,
        amount: 11.375,
        gst_rate: 5,
        gst_amount: 0.56875,
        total: 11.94375,
        service_code: null,
      },
    ],
    guest: {
      name: "Nasser Al-Sabah",
      phone: null,
      email: null,
      nationality: "Kuwaiti",
      address: "",
      id_type: null,
    },
    summary_by_head: [
      { head: "Room", amount: 22.75, tax: 1.138, total: 23.888, lines: 2, service_code: null },
    ],
    tax_source: "folio",
    tax_summary: [
      {
        rate: 5,
        taxable: 22.75,
        total_tax: 1.138,
        parts: [{ label: "VAT", rate: 5, amount: 1.138 }],
      },
    ],
    gst_summary: [
      { rate: 5, taxable: 22.75, cgst: 1.138, sgst: 1.138, total_tax: 1.138 },
    ],
  }
}

describe("3-decimal KWD rendering in FolioView", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    rpc.mockReset().mockImplementation((method: string) => {
      if (method === "kamra.api.folio_invoice") return Promise.resolve(kwdPayload())
      if (method === "kamra.api.reservation_folios") return Promise.resolve([])
      if (method === "kamra.api.linked_records") return Promise.resolve(null)
      if (method === "kamra.api.allowance_requests") return Promise.resolve({ requests: [], can_decide: false })
      if (method === "kamra.api.correct_charged_nights") return Promise.resolve({ nights: [] })
      if (method === "kamra.ledger.list_reason_codes") return Promise.resolve([])
      if (method === "kamra.ledger.fx_rates") return Promise.resolve([])
      if (method === "kamra.api.property_locale") {
        return Promise.resolve({
          currency: "KWD",
          currency_symbol: "KWD ",
          locale: "en-US",
          tax_label: "VAT",
          tax_id_label: "Tax ID",
          tax_rates: [5],
          payment_modes: ["Cash"],
          instant_payment_modes: [],
          outlet_payment_modes: ["Cash"],
          id_types: ["Passport"],
          id_type_hints: {},
          default_nationality: "Kuwaiti",
          adult_age: 18,
          minor_ids: "foreign",
          integrations: {
            payment_gateways: [],
            tax_return: false,
            verify_tax_id: false,
            verify_guest_id: false,
          },
        })
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

  it("formats KWD payment rows to 3 decimals and renders 3-dp amount in words without brand leaks", async () => {
    await act(async () => root.render(
      <MemoryRouter initialEntries={["/billing/FOLIO-2026-00334"]}>
        <Routes><Route path="/billing/:name" element={<FolioView />} /></Routes>
      </MemoryRouter>,
    ))
    await vi.waitFor(() => expect(host.textContent).toContain("FOLIO-2026-00334"), { interval: 5 })
    await vi.waitFor(() => expect(host.textContent).toContain("23.888"), { interval: 5 })

    const page = host.textContent ?? ""
    // Payment row must render 11.944 and -5.944, never truncated 11.94 or -5.94
    expect(page).toContain("KWD 11.944")
    expect(page).toContain("KWD -5.944")
    expect(page).not.toMatch(/KWD 11\.94(?!4)/)
    expect(page).not.toMatch(/KWD -5\.94(?!4)/)

    // Amount in words shows exact 3-dp minor units
    expect(page).toContain("KWD Twenty Three point 888 Only")
    expect(page).not.toContain("point 89")

    // Zero user-visible OSS/vendor wording
    expect(page).not.toMatch(/ERPNext|Frappe|URY|Kamra|HUF|HeyKoala|Tridz|BuildSuite/i)
  })
})
