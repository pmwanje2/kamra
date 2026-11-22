import { describe, it, expect, vi } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"
import { formatFxRate } from "../src/components/ContractTariff"
import { SettlementPreview } from "../src/components/SettlementPreview"
import { tenderAmount } from "../src/components/TenderPicker"
vi.mock("../src/lib/api", () => ({}))
vi.mock("../src/lib/resource", () => ({}))
vi.mock("../src/lib/utils", () => ({formatDate:(s:string)=>s}))
vi.mock("../src/lib/i18n", () => ({ t: (s: string) => s }))
describe("settlement", () => {
  it("formats published rates without losing small rates", () => {
    expect([4032.258064516, 1.123456, 0.001234567].map(r => formatFxRate(r, "en"))).toEqual(["4,032.26", "1.1235", "0.001235"])
  })
  it("accepts the exact foreign cents when they cover the rounded local balance", () => {
    expect(tenderAmount(524194, "USD", "UGX", {USD:4032.258064516})).toBe("130")
    expect(tenderAmount(524195, "USD", "UGX", {USD:4032.258064516})).toBe("130.01")
  })
  it.each([[524194, "Settled in full"], [524195, "Remaining balance"], [524193, "Change due"]] as const)("compares posted home units with %s", (balance, label) => {
    const quote = {currency:"USD", home_currency:"UGX", rate:4032.258064516, rate_date:"2026-11-22", quote_id:"q", home_amount:524194}
    const html = renderToStaticMarkup(<SettlementPreview result={{key:"q", quote}} amount={130} balance={balance} />)
    for (const text of [label, "USD 130.00", "1 USD = 4,032.26 UGX", "UGX 524,194"]) expect(html).toContain(text)
  })
})
