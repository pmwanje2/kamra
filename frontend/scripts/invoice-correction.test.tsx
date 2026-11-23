// @vitest-environment jsdom
/* Correcting an issued invoice from the folio screen (group billing P0).
   A hotel that invoices here gets a credit note dialog that shows the tax
   it credits. Where the accounting system issued the invoice, the dialog
   says so and sends the correction to its credit note and re-bill; this
   screen never reopens or credits that bill on its own. No browser prompt. */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import FolioView from "../src/screens/FolioView"
import { setLang } from "../src/lib/dir"
import { ensureDictionary } from "../src/lib/locales"
import { fmtMoney } from "../src/lib/money"

const { rpc } = vi.hoisted(() => ({ rpc: vi.fn() }))
vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => "Test Hotel",
  newIdempotencyKey: () => "key",
}))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: ["Finance"] }) }))

const DOCUMENT = "ACC-SINV-2026-00001"

function invoice(folio: Record<string, unknown> = {}) {
  return {
    folio: {
      name: "FOLIO-1", property: "Test Hotel", status: "Closed", folio_type: "Extra",
      invoice_number: "INV-0001", guest_name: "Ada Guest",
      charges: [{ name: "c1", posting_date: "2026-11-22", charge_type: "Misc", description: "Hall",
        amount: 1000, gst_rate: 18, gst_amount: 180, total: 1180 }],
      payments: [], charges_total: 1000, tax_total: 180, grand_total: 1180,
      payments_total: 1180, balance: 0, ...folio,
    },
    property: { currency: "UGX", name: "Test Hotel", legal_name: null, logo_url: null, address: "",
      state: null, gstin: null, phone: null, email: null, sac: null, place_of_supply: null },
    stay: { reservation: null, check_in: "", check_out: "", arrival: null, departure: null, nights: 0,
      adults: 0, children: 0, pax: 0, meal_plan: null, room_type: null, room: null, company: null,
      group_booking: null, booked_by_name: null, booked_by_phone: null, contact_preference: null },
    gst_summary: [], bill_to: null, statutory: null,
    document: { title: "Tax invoice", is_final: true, number: "INV-0001", date: "2026-11-22",
      amount_in_words: "", footer: null, service_code_label: "SAC" },
    lines: [], guest: { name: "Ada Guest", phone: null, email: null, nationality: null, address: "", id_type: null },
    summary_by_head: [], tax_summary: [],
  }
}

describe("Invoice corrections on the folio screen", () => {
  let host: HTMLDivElement
  let root: Root
  let data: ReturnType<typeof invoice>

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    data = invoice()
    rpc.mockReset().mockImplementation((method: string) => {
      if (method === "kamra.api.folio_invoice") return Promise.resolve(data)
      if (method === "kamra.ledger.list_reason_codes") return Promise.resolve([])
      if (method === "kamra.api.property_locale") return Promise.resolve({ currency: "UGX" })
      return Promise.resolve({})
    })
    vi.spyOn(window, "prompt").mockReturnValue(null)
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

  async function render() {
    await act(async () => root.render(
      <MemoryRouter initialEntries={["/billing/FOLIO-1"]}>
        <Routes><Route path="/billing/:name" element={<FolioView />} /></Routes>
      </MemoryRouter>))
    await vi.waitFor(() => expect(host.textContent).toContain("INV-0001"), { interval: 1 })
  }
  const text = () => host.textContent ?? ""
  const button = (label: string) => {
    const found = [...host.querySelectorAll("button")].find((b) => b.textContent?.trim() === label)
    if (!found) throw new Error(`No button ${label}`)
    return found
  }
  const press = (label: string) => act(async () => button(label).click())
  const dialog = () => host.querySelector<HTMLElement>('[role="dialog"]')
  async function type(label: string, value: string) {
    const field = dialog()?.querySelector<HTMLInputElement | HTMLTextAreaElement>(`[aria-label="${label}"]`)
    if (!field) throw new Error(`No field ${label}`)
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(field), "value")!.set!
    await act(async () => {
      setter.call(field, value)
      field.dispatchEvent(new Event("input", { bubbles: true }))
    })
  }

  it("issues a credit note here with the tax it credits, in a dialog", async () => {
    await render()
    await press("Credit note")
    expect(window.prompt).not.toHaveBeenCalled()
    expect(dialog()?.getAttribute("aria-label")).toBe("Credit note")
    expect(dialog()?.textContent).toContain("A credit note is issued here against invoice INV-0001")
    await type("Amount to credit, tax included", "590")
    await type("Reason (required - goes on the record)", "Hall used half a day")
    expect(dialog()?.textContent).toContain(`Tax in this credit note: ${fmtMoney(90)}`)
    await press("Issue credit note")
    expect(rpc).toHaveBeenCalledWith("kamra.ledger.issue_credit_note",
      { folio: "FOLIO-1", amount: 590, reason: "Hall used half a day" })
  })

  it("sends an invoice the accounting system issued to its credit note and re-bill", async () => {
    data = invoice({ sales_invoice: DOCUMENT })
    await render()
    await press("Credit note")
    expect(window.prompt).not.toHaveBeenCalled()
    const said = dialog()?.textContent ?? ""
    expect(said).toContain(`Invoice INV-0001 was issued by the accounting system as ${DOCUMENT}.`)
    expect(said).not.toContain("issued here")
    expect(said).not.toMatch(/ERPNext|Frappe|Kamra/)
    await type("Amount to credit, tax included", "100")
    await type("Reason (required - goes on the record)", "Short stay")
    await press("Send for credit note and re-bill")
    expect(rpc).toHaveBeenCalledWith("kamra.ledger.issue_credit_note",
      { folio: "FOLIO-1", amount: 100, reason: "Short stay" })
  })

  it("never offers to reopen an invoice the accounting system issued", async () => {
    data = invoice({ sales_invoice: DOCUMENT })
    await render()
    await press("Cancel invoice")
    expect(text()).toContain("Cancel INV-0001? The accounting system credits it and issues a new invoice; this bill is not reopened here.")
    expect(text()).not.toContain("the folio reopens for correction")
  })

  it.each([
    ["ar", "إشعار دائن", "أصدر النظام المحاسبي الفاتورة INV-0001"],
    ["de", "Gutschrift", "Die Rechnung INV-0001 wurde von der Buchhaltung"],
    ["es", "Nota de crédito", "El sistema contable emitió la factura INV-0001"],
    ["fil", "Credit note", "Ang invoice INV-0001 ay inilabas ng sistema ng accounting"],
    ["id", "Credit note", "Invoice INV-0001 diterbitkan oleh sistem akuntansi"],
    ["pt", "Nota de crédito", "A fatura INV-0001 foi emitida pelo sistema contábil"],
    ["zh-CN", "贷项通知单", "发票 INV-0001 由会计系统以"],
  ])("explains the correction path in %s", async (language, heading, path) => {
    await ensureDictionary(language)
    setLang(language)
    data = invoice({ sales_invoice: DOCUMENT })
    await render()
    await press(heading)
    expect(dialog()?.getAttribute("aria-label")).toBe(heading)
    expect(dialog()?.textContent).toContain(path)
  })
})
