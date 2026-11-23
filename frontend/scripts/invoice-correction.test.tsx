// @vitest-environment jsdom
/* Correcting an issued invoice from the folio screen (group billing P0).
   A hotel that invoices here gets a credit note dialog that shows the tax
   it credits. Where the accounting system issued the invoice, the dialog
   says so and sends the correction to its credit note and re-bill; this
   screen never reopens or credits that bill on its own, even when it closed
   the bill at check-out with no hotel invoice number. No browser prompt.
   Only the roles that may credit a bill are offered it, a credit never goes
   past what is left on the invoice, and a split shows as a split. */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import FolioView from "../src/screens/FolioView"
import { setLang } from "../src/lib/dir"
import { ensureDictionary } from "../src/lib/locales"
import { fmtMoney, moneyDigits } from "../src/lib/money"

const { rpc } = vi.hoisted(() => ({ rpc: vi.fn() }))
vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => "Test Hotel",
  newIdempotencyKey: () => "key",
}))
const auth = vi.hoisted(() => ({ roles: ["Finance"] }))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: auth.roles }) }))

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
    auth.roles = ["Finance"]
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

  async function render(shown = "INV-0001") {
    await act(async () => root.render(
      <MemoryRouter initialEntries={["/billing/FOLIO-1"]}>
        <Routes><Route path="/billing/:name" element={<FolioView />} /></Routes>
      </MemoryRouter>))
    await vi.waitFor(() => expect(host.textContent).toContain(shown), { interval: 1 })
  }
  const buttons = () => [...host.querySelectorAll("button")].map((b) => b.textContent?.trim())
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

  it("corrects a bill the accounting system closed at check-out with no hotel number", async () => {
    data = invoice({ invoice_number: null, sales_invoice: DOCUMENT })
    data.document = { ...data.document, number: null as unknown as string }
    await render("Ada Guest")
    await press("Credit note")
    const said = dialog()?.textContent ?? ""
    expect(said).toContain(`The accounting system issued invoice ${DOCUMENT} for this bill.`)
    expect(said).not.toContain("issued here")
    await press("Close")
    await press("Cancel invoice")
    expect(text()).toContain(`Cancel ${DOCUMENT}? The accounting system credits it and issues a new invoice; this bill is not reopened here.`)
  })

  it("offers to credit a bill only to the roles that may", async () => {
    auth.roles = ["Front Desk"]
    await render()
    expect(buttons()).not.toContain("Credit note")
    // a bill invoiced here is still cancelled and reopened at the desk
    expect(buttons()).toContain("Cancel invoice")
    await act(async () => root.unmount())
    root = createRoot(host)
    data = invoice({ sales_invoice: DOCUMENT })
    await render()
    expect(buttons()).not.toContain("Credit note")
    expect(buttons()).not.toContain("Cancel invoice")
  })

  it("never credits more than is left on the invoice", async () => {
    data = { ...invoice(), credited: 1000 }
    await render()
    await press("Credit note")
    await type("Reason (required - goes on the record)", "Hall unused")
    await type("Amount to credit, tax included", "500")
    expect(dialog()?.textContent).toContain(`At most ${fmtMoney(180)} can still be credited on this invoice.`)
    expect(button("Issue credit note").disabled).toBe(true)
    await type("Amount to credit, tax included", "180")
    expect(dialog()?.textContent).not.toContain("can still be credited")
    expect(button("Issue credit note").disabled).toBe(false)
  })

  it("shows the tax the server books, in the currency's minor unit", async () => {
    await render()
    await vi.waitFor(() => expect(moneyDigits()).toBe(0), { interval: 1 })
    await press("Credit note")
    // 95.0228 x 180 / 1180 = 14.495: a shilling bill books 14, not 14.50
    await type("Amount to credit, tax included", "95.0228")
    expect(dialog()?.textContent).toContain(`Tax in this credit note: ${fmtMoney(14)}`)
  })

  it("names a split for what it is and leaves it off the printed bill", async () => {
    const line = { posting_date: "2026-11-22", charge_type: "Laundry", gst_rate: 0, gst_amount: 0 }
    data = invoice({ charges: [
      { ...line, name: "c1", description: "Bag A", amount: 1000, total: 1000 },
      { ...line, name: "c2", description: "Bag A", amount: -1000, total: -1000,
        reversal_of: "c1", split_of: "c1", void_reason: "Split to FOLIO-2" },
      { ...line, name: "c3", description: "Bag A", amount: 700, total: 700, split_of: "c1" },
      { ...line, name: "v1", description: "Bag B", amount: 50, total: 50 },
      { ...line, name: "v2", description: "VOID: Bag B - wrong", amount: -50, total: -50,
        reversal_of: "v1", void_reason: "wrong" },
    ] })
    await render()
    const rows = [...host.querySelectorAll("tr")].filter((r) => r.textContent?.includes("Bag"))
    expect(rows).toHaveLength(5)
    const badge = (r: Element) => [...r.querySelectorAll("span")].map((s) => s.textContent)
    expect(badge(rows[0])).toContain("Split")
    expect(badge(rows[1])).toContain("Split")
    expect(badge(rows[0])).not.toContain("Voided")
    expect(badge(rows[2])).not.toContain("Split")
    expect(badge(rows[3])).toContain("Voided")
    expect(badge(rows[4])).toContain("Voided")
    expect(rows.map((r) => r.className.includes("print:hidden"))).toEqual([true, true, false, false, false])
  })

  it("moves an outlet or package line whole without offering a split input", async () => {
    data = invoice({
      status: "Open",
      invoice_number: null,
      charges: [
        { name: "c_out", posting_date: "2026-11-22", charge_type: "Food & Beverage",
          description: "Restaurant Order", source_ref: "POS-ORD-8801",
          amount: 1000, gst_rate: 18, gst_amount: 180, total: 1180 },
        { name: "c_pkg", posting_date: "2026-11-22", charge_type: "Meal Plan",
          description: "Breakfast Plan",
          amount: 500, gst_rate: 18, gst_amount: 90, total: 590 },
        { name: "c_room", posting_date: "2026-11-22", charge_type: "Room",
          description: "Room 101",
          amount: 2000, gst_rate: 18, gst_amount: 360, total: 2360 },
      ],
      charges_total: 3500, tax_total: 630, grand_total: 4130, payments_total: 0, balance: 4130,
    })
    data.stay = { ...data.stay, reservation: "RES-1" }
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.folio_invoice") return Promise.resolve(data)
      if (method === "kamra.api.reservation_folios") {
        return Promise.resolve([
          { name: "FOLIO-1", folio_type: "Guest", status: "Open", balance: 4130 },
          { name: "FOLIO-2", folio_type: "Extra", status: "Open", balance: 0 },
        ])
      }
      if (method === "kamra.api.correct_charged_nights") return Promise.resolve({ nights: [] })
      if (method === "kamra.api.linked_records") return Promise.resolve(null)
      if (method === "kamra.api.allowance_requests") return Promise.resolve({ requests: [], can_decide: false })
      if (method === "kamra.ledger.list_reason_codes") return Promise.resolve([])
      if (method === "kamra.api.property_locale") return Promise.resolve({ currency: "UGX" })
      return Promise.resolve({})
    })
    await render("Restaurant Order")

    const rowCheckbox = (label: string) => {
      const row = [...host.querySelectorAll("tr")].find((r) => r.textContent?.includes(label))
      const box = row?.querySelector<HTMLInputElement>('input[type="checkbox"]')
      if (!box) throw new Error(`No checkbox for ${label}`)
      return box
    }
    const toggle = (label: string) => act(async () => rowCheckbox(label).click())
    const splitInput = () => host.querySelector<HTMLInputElement>('input[placeholder="30% or 1500"]')

    // Single outlet line: no split input or "move only" prompt, explains whole move
    await toggle("Restaurant Order")
    expect(splitInput()).toBeNull()
    expect(text()).not.toContain("move only")
    expect(text()).toContain("Moves whole (cannot be split)")
    await press("Clear")

    // Single package (Meal Plan) line: same whole-move behaviour
    await toggle("Breakfast Plan")
    expect(splitInput()).toBeNull()
    expect(text()).not.toContain("move only")
    expect(text()).toContain("Moves whole (cannot be split)")
    await press("Clear")

    // Ordinary room line offers the split input; switching to an outlet line
    // without clearing still sends a whole transfer, never a split call
    await toggle("Room 101")
    const input = splitInput()
    expect(input).not.toBeNull()
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
    await act(async () => {
      setter.call(input!, "30%")
      input!.dispatchEvent(new Event("input", { bubbles: true }))
    })
    await toggle("Room 101")
    await toggle("Restaurant Order")
    expect(splitInput()).toBeNull()
    const targetBtn = [...host.querySelectorAll("button")].find(
      (b) => b.textContent?.includes("Extra") && b.textContent?.includes("owes"),
    )
    if (!targetBtn) throw new Error("No Extra target button")
    await act(async () => targetBtn.click())
    expect(rpc).toHaveBeenCalledWith("kamra.api.transfer_folio_charges", {
      from_folio: "FOLIO-1",
      charge_rows: ["c_out"],
      to_folio: "FOLIO-2",
    })
    expect(rpc).not.toHaveBeenCalledWith("kamra.api.split_folio_charge", expect.anything())
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
