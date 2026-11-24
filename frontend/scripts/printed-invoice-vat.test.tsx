// @vitest-environment jsdom
/** Bridged closed Tax Invoice VAT rendering in FolioView:
 * - Renders the ERP accounting invoice's own tax rate, taxable amount and tax
 *   in lines and summary, formatted in whole shillings for UGX (0 decimals).
 * - When the accounting invoice cannot be read, shows "Tax as per the
 *   accounting invoice" instead of 0% / VAT @ 0%.
 * - Translates new strings across all 7 shipped locales and contains no
 *   user-visible vendor names. */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import FolioView from "../src/screens/FolioView"
import { setLang } from "../src/lib/dir"
import ar from "../src/i18n/locales/ar.json"
import de from "../src/i18n/locales/de.json"
import es from "../src/i18n/locales/es.json"
import fil from "../src/i18n/locales/fil.json"
import id from "../src/i18n/locales/id.json"
import pt from "../src/i18n/locales/pt.json"
import zhCN from "../src/i18n/locales/zh-CN.json"

const { rpc } = vi.hoisted(() => ({ rpc: vi.fn() }))
vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => "Tank Hill Lodge",
  newIdempotencyKey: () => "key-vat",
}))
vi.mock("../src/lib/auth", () => ({
  useAuth: () => ({ status: "authed", roles: ["Front Desk"] }),
}))

function bridgedPayload(over: Record<string, unknown> = {}) {
  return {
    folio: {
      name: "FOLIO-2026-001",
      property: "Tank Hill Lodge",
      status: "Closed",
      folio_type: "Guest",
      invoice_number: "INV-THL-26-00003",
      sales_invoice: "ACC-SINV-2026-00001",
      guest_name: "John Ssemwogerere",
      // Raw folio charge still carries 0% on a bridged site; enriched `lines` carries ERP tax
      charges: [{
        name: "ch-1",
        posting_date: "2026-11-24",
        charge_type: "Room",
        description: "Room 102 · DLX",
        amount: 499500,
        gst_rate: 0,
        gst_amount: 0,
        total: 499500,
      }],
      payments: [{
        name: "pay-1",
        posting_date: "2026-11-24",
        payment_kind: "Advance",
        mode: "Cash",
        amount: 499500,
        reference: "ACC-PAY-2026-00008",
      }],
      charges_total: 423305.08,
      tax_total: 76194.92,
      grand_total: 499500,
      payments_total: 499500,
      balance: 0,
    },
    property: {
      currency: "UGX",
      name: "Tank Hill Lodge",
      legal_name: null,
      logo_url: null,
      address: "Kampala",
      state: null,
      gstin: null,
      phone: null,
      email: null,
      sac: null,
      place_of_supply: "Kampala",
      tax_label: "VAT",
      tax_id_label: "TIN",
    },
    stay: {
      reservation: "RES-2026-00042",
      check_in: "2026-11-24",
      check_out: "2026-11-25",
      arrival: "2026-11-24 08:15:00",
      departure: null,
      nights: 1,
      adults: 2,
      children: 0,
      pax: 2,
      meal_plan: null,
      room_type: "Tank Hill Lodge-DLX",
      room: "RM-102",
      room_number: "102",
      company: null,
      group_booking: null,
      booked_by_name: null,
      booked_by_phone: null,
      contact_preference: null,
    },
    bill_to: null,
    statutory: null,
    document: {
      title: "Invoice",
      is_final: true,
      number: "INV-THL-26-00003",
      date: "2026-11-24",
      amount_in_words: "UGX Four Hundred Ninety Nine Thousand Five Hundred Only",
      footer: "This is a computer-generated invoice.",
      service_code_label: "SAC",
    },
    lines: [{
      row: "ch-1",
      posting_date: "2026-11-24",
      charge_type: "Room",
      description: "Room 102 · DLX",
      qty: 1,
      rate: 423305.08,
      amount: 423305.08,
      gst_rate: 18,
      gst_amount: 76194.92,
      total: 499500,
      service_code: null,
    }],
    guest: { name: "John Ssemwogerere", phone: null, email: null, nationality: "Ugandan", address: "", id_type: null },
    summary_by_head: [{ head: "Room", amount: 423305.08, tax: 76194.92, total: 499500, lines: 1, service_code: null }],
    tax_source: "accounting",
    tax_summary: [{
      rate: 18,
      taxable: 423305.08,
      total_tax: 76194.92,
      parts: [{ label: "VAT", rate: 18, amount: 76194.92 }],
    }],
    gst_summary: [{
      rate: 18,
      taxable: 423305.08,
      cgst: 76194.92,
      sgst: 76194.92,
      total_tax: 76194.92,
    }],
    ...over,
  }
}

describe("Bridged Tax Invoice VAT in FolioView", () => {
  let host: HTMLDivElement
  let root: Root
  let data: ReturnType<typeof bridgedPayload>

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    data = bridgedPayload()
    rpc.mockReset().mockImplementation((method: string) => {
      if (method === "kamra.api.folio_invoice") return Promise.resolve(data)
      if (method === "kamra.api.reservation_folios") return Promise.resolve([])
      if (method === "kamra.api.linked_records") return Promise.resolve(null)
      if (method === "kamra.api.allowance_requests") return Promise.resolve({ requests: [], can_decide: false })
      if (method === "kamra.api.correct_charged_nights") return Promise.resolve({ nights: [] })
      if (method === "kamra.ledger.list_reason_codes") return Promise.resolve([])
      if (method === "kamra.api.property_locale") {
        return Promise.resolve({
          currency: "UGX", currency_symbol: "UGX ", locale: "en-UG",
          tax_label: "VAT", tax_id_label: "TIN", tax_rates: [0],
          payment_modes: ["Cash"], instant_payment_modes: [], outlet_payment_modes: ["Cash"],
          id_types: ["Passport"], id_type_hints: {}, default_nationality: "Ugandan",
          adult_age: 18, minor_ids: "foreign",
          integrations: { payment_gateways: [], tax_return: false, verify_tax_id: false, verify_guest_id: false },
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

  async function renderInvoice() {
    await act(async () => root.render(
      <MemoryRouter initialEntries={["/billing/FOLIO-2026-001"]}>
        <Routes><Route path="/billing/:name" element={<FolioView />} /></Routes>
      </MemoryRouter>,
    ))
    await vi.waitFor(() => expect(host.textContent).toContain("INV-THL-26-00003"), { interval: 5 })
  }

  it("shows the ERP invoice line rate/tax and VAT summary in whole shillings", async () => {
    await renderInvoice()
    const roomRow = [...host.querySelectorAll("tr")].find((tr) => tr.textContent?.includes("Room 102"))
    expect(roomRow).toBeDefined()
    const rowText = roomRow!.textContent ?? ""
    expect(rowText).toContain("423,305")
    expect(rowText).toContain("18%")
    expect(rowText).toContain("76,195")
    expect(rowText).toContain("499,500")
    expect(rowText).not.toContain("0%")
    expect(rowText).not.toContain(".08")
    expect(rowText).not.toContain(".92")

    const page = host.textContent ?? ""
    expect(page).toContain("VAT @ 18%")
    expect(page).toContain("taxable UGX 423,305")
    expect(page).toContain("UGX 76,195")
    expect(page).not.toContain("VAT @ 0%")
    expect(page).not.toMatch(/Kamra|ERPNext|Frappe|URY/i)
  })

  it("says 'Tax as per the accounting invoice' instead of 0% when the accounting invoice cannot be read", async () => {
    data = bridgedPayload({
      tax_source: "accounting_unavailable",
      tax_summary: [],
      gst_summary: [],
      document: {
        title: "Invoice",
        is_final: true,
        number: "INV-THL-26-00003",
        date: "2026-11-24",
        amount_in_words: "UGX Four Hundred Ninety Nine Thousand Five Hundred Only",
        footer: "This is a computer-generated invoice.",
        service_code_label: "SAC",
        accounting_tax_note: "Tax as per the accounting invoice",
      },
      lines: [{
        row: "ch-1", posting_date: "2026-11-24", charge_type: "Room",
        description: "Room 102 · DLX", qty: 1, rate: 499500, amount: 499500,
        gst_rate: 0, gst_amount: 0, total: 499500, service_code: null,
      }],
    })
    await renderInvoice()
    const page = host.textContent ?? ""
    expect(page).toContain("Tax as per the accounting invoice")
    expect(page).not.toContain("0%")
    expect(page).not.toContain("VAT @ 0%")
    expect(page).not.toMatch(/Kamra|ERPNext|Frappe|URY/i)
  })

  it("renders multi-row ERP taxes and fixed-amount taxes cleanly without duplicate rates", async () => {
    data = bridgedPayload({
      tax_summary: [
        { rate: 9, taxable: 100000, total_tax: 9000, parts: [{ label: "CGST", rate: 9, amount: 9000 }] },
        { rate: 9, taxable: 100000, total_tax: 9000, parts: [{ label: "SGST", rate: 9, amount: 9000 }] },
        { rate: 0, taxable: 100000, total_tax: 5000, parts: [{ label: "Local Hotel Tax", rate: 0, amount: 5000 }] },
      ],
    })
    await renderInvoice()
    const page = host.textContent ?? ""
    expect(page).toContain("CGST @ 9%")
    expect(page).toContain("SGST @ 9%")
    expect(page).toContain("Local Hotel Tax")
    expect(page).not.toContain("Local Hotel Tax @ 0%")
  })

  it.each([
    ["ar", ar],
    ["de", de],
    ["es", es],
    ["fil", fil],
    ["id", id],
    ["pt", pt],
    ["zh-CN", zhCN],
  ])("translates 'Tax as per the accounting invoice' in %s", (_lang, dict: Record<string, string>) => {
    expect(dict["Tax as per the accounting invoice"]).toBeTruthy()
  })
})
