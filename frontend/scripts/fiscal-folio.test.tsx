import { describe, it, expect, vi } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"
import { FiscalDocument } from "../src/components/FiscalDocument"
import { eventBilling } from "../src/lib/eventBilling"
vi.mock("../src/lib/i18n", () => ({
  t: (s: string, vars?: Record<string, unknown>) =>
    s.replace(/\{(\w+)\}/g, (_m, k: string) => String(vars?.[k] ?? `{${k}}`)),
}))

const fiscalised = {
  state: "fiscalised" as const, sales_invoice: "ACC-SINV-2026-00007", fdn: "322000123456",
  verification_code: "80123456789012345678", qr: "data:image/png;base64,iVBORw0KGgo=",
  issued: "2026-11-23 10:04:00", sandbox: false, efris_status: "Success",
}

describe("fiscal folio", () => {
  it("prints the fiscal number, verification code and QR of a fiscalised invoice", () => {
    const html = renderToStaticMarkup(<FiscalDocument fiscal={fiscalised} internalNumber="INV-UTA-26-00001" />)
    for (const text of ["322000123456", "80123456789012345678", 'src="data:image/png;base64,iVBORw0KGgo="',
                        "2026-11-23 10:04:00", "Internal folio no. INV-UTA-26-00001"]) expect(html).toContain(text)
    expect(html).not.toContain("not a tax invoice")
  })
  it.each([
    [{ ...fiscalised, state: "pending" as const, fdn: null, verification_code: null, qr: null, efris_status: "Failed" },
     ["Fiscal document pending", "not a tax invoice", "Status: Failed"]],
    [{ ...fiscalised, state: "unverified" as const, fdn: null, verification_code: null, qr: null },
     ["Fiscal status unavailable", "not a tax invoice"]],
    [{ ...fiscalised, sandbox: true }, ["Test fiscal number - not a valid tax invoice"]],
  ])("never presents %s as a final tax invoice", (fiscal, texts) => {
    const html = renderToStaticMarkup(<FiscalDocument fiscal={fiscal} internalNumber="INV-UTA-26-00001" />)
    for (const text of texts) expect(html).toContain(text)
    if (fiscal.state !== "fiscalised") expect(html).not.toContain("<img")
  })
  it("adds nothing for an invoice outside the fiscal service", () => {
    const html = renderToStaticMarkup(
      <FiscalDocument fiscal={{ ...fiscalised, state: "not_efris", fdn: null, verification_code: null, qr: null }}
                      internalNumber="INV-UTA-26-00001" />)
    expect(html).toContain("Internal folio no. INV-UTA-26-00001")
    expect(html).not.toContain("FDN")
    expect(html).not.toContain("not a tax invoice")
  })
})

describe("event billing", () => {
  it("bills a standalone function on its event folio under the bridge", () => {
    expect(eventBilling({ fiscal_bridge: 1, group_booking: null, status: "Confirmed", event_folio: null }))
      .toEqual({ canPost: true, invoiceOnFolio: true, folio: null })
    expect(eventBilling({ fiscal_bridge: 1, group_booking: null, status: "Confirmed", event_folio: "FOLIO-1" }).folio)
      .toBe("FOLIO-1")
  })
  it("keeps the group master folio and the function invoice without the bridge", () => {
    expect(eventBilling({ fiscal_bridge: 0, group_booking: null, status: "Confirmed", event_folio: null }))
      .toEqual({ canPost: false, invoiceOnFolio: false, folio: null })
    expect(eventBilling({ fiscal_bridge: 0, group_booking: "GRP-1", status: "Confirmed", event_folio: null }).canPost)
      .toBe(true)
  })
  it("never bills an enquiry", () => {
    expect(eventBilling({ fiscal_bridge: 1, group_booking: "GRP-1", status: "Enquiry", event_folio: null }).canPost)
      .toBe(false)
  })
})
