// @vitest-environment jsdom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { renderToStaticMarkup } from "react-dom/server"
import { MemoryRouter } from "react-router-dom"
import { expect, it, vi } from "vitest"
import { FiscalDocument } from "../src/components/FiscalDocument"
import { StayBilling } from "../src/components/RoomStayPanels"
import { call } from "../src/lib/api"
import type { RoomDetail } from "../src/lib/rooms"

vi.mock("../src/lib/api", async (original) => ({
  ...await original<typeof import("../src/lib/api")>(), call: vi.fn(),
}))

it.each([
  ["denied", null, false], ["mismatched", "SI-OTHER", false], ["authorized", "SI-OWNED", true],
] as const)("renders statutory identifiers only for the matching printable invoice (%s)", (_, verifiedInvoice, show) => {
  const fiscal = {
    state: "fiscalised" as const, sales_invoice: "SI-OWNED", fdn: "FDN-R6", verification_code: "CODE-R6",
    qr: "data:image/png;base64,cjY=", issued: "2031-03-12 10:04:00", sandbox: false, efris_status: "Success",
  }
  const props = { fiscal, verifiedInvoice, internalNumber: "LOCAL-R6" }
  const html = renderToStaticMarkup(<FiscalDocument {...props} />)
  for (const value of [fiscal.fdn, fiscal.verification_code, fiscal.qr, fiscal.issued]) {
    expect(html.includes(value)).toBe(show)
  }
  expect(html).toContain("LOCAL-R6")
})

it.each([
  ["UGX", 0, 75], ["USD", 2, 74.88], ["KWD", 3, 74.877],
] as const)("refreshes the selected paid group window using its own %s balance", async (currency, decimals, guestOwes) => {
  let groupOwes = 20
  const base = { status: "Open", grand_total: 100, payments_total: 0, balance: 100 }
  const rpc = vi.mocked(call)
  rpc.mockImplementation(async (method, args) => {
    if (method === "kamra.api.reservation_folios") return [
      { ...base, name: "GUEST", folio_type: "Guest", net_collectible_balance: guestOwes },
      { ...base, name: "GROUP", folio_type: "Group", net_collectible_balance: groupOwes },
    ]
    if (method === "kamra.api.property_locale") return { currency, decimals, currency_symbol: currency + " " }
    if (method === "kamra.api.add_folio_payment") {
      expect(args).toMatchObject({ folio: "GROUP", amount: 20 })
      groupOwes = 0
    }
    return null
  })
  const host = document.createElement("div")
  const root = createRoot(host)
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  try {
    await act(async () => root.render(<MemoryRouter>
      <StayBilling d={{ stay: { name: "STAY" } } as RoomDetail} onChanged={() => {}} />
    </MemoryRouter>))
    const select = host.querySelector<HTMLOptionElement>('option[value="GROUP"]')!.parentElement as HTMLSelectElement
    const amount = [...host.querySelectorAll("label")].find(l => l.textContent.trim() === "Amount")!.querySelector("input")!
    const pay = [...host.querySelectorAll("button")].find(b => b.textContent === "Record payment")!
    await act(async () => { select.value = "GROUP"; select.dispatchEvent(new Event("change", { bubbles: true })) })
    expect(amount.value).toBe("20")
    await act(async () => pay.click())
    expect(groupOwes).toBe(0)
    expect(select.value).toBe("GROUP")
    expect(amount.value).toBe("")
    expect(pay.disabled).toBe(true)
  } finally {
    await act(async () => root.unmount())
    vi.unstubAllGlobals()
  }
})
