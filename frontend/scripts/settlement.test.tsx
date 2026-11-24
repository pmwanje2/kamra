import { describe, it, expect, vi } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"
import { formatFxRate } from "../src/components/ContractTariff"
import { SettlementPreview } from "../src/components/SettlementPreview"
import { tenderAmount, tenderModes } from "../src/components/TenderPicker"
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
  it("gates FX currencies by tender_type instead of method-name literals when standalone", () => {
    const out = tenderModes({
      payment_modes: ["Cash", "USD Wire", "USD Box", "Front Desk Till", "Airtel Money"],
      payment_tender_types: {
        Cash: "Cash",
        "Foreign Cash": "Foreign Cash",
        "USD Wire": "Bank Transfer",
        "USD Box": "Foreign Cash",
        "Front Desk Till": "Cash",
        "Airtel Money": "Instant",
      },
      tender_modes: null,
      fx_currencies: ["USD", "EUR"],
    }, "UGX")
    const byMode = Object.fromEntries(out.map((m) => [m.mode, m.currencies]))
    expect(byMode["USD Wire"]).toEqual(["UGX", "USD", "EUR"])
    expect(byMode["USD Box"]).toEqual(["USD", "EUR"])
    expect(byMode["Front Desk Till"]).toEqual(["UGX", "USD", "EUR"])
    expect(byMode["Airtel Money"]).toEqual(["UGX"])
  })
  it("gates FX currencies by tender_type instead of method-name literals when bridged", () => {
    const out = tenderModes({
      payment_modes: ["Cash", "USD Wire", "USD Box", "Airtel Money"],
      payment_tender_types: {
        Cash: "Cash",
        "Foreign Cash": "Foreign Cash",
        "USD Wire": "Bank Transfer",
        "USD Box": "Foreign Cash",
        "Airtel Money": "Instant",
      },
      tender_modes: [
        { mode: "Cash", currencies: ["UGX", "USD"] },
        { mode: "USD Wire", currencies: ["UGX", "USD"] },
        { mode: "USD Box", currencies: ["UGX", "USD"] },
        { mode: "Airtel Money", currencies: ["UGX", "USD"] },
      ],
      fx_currencies: ["USD"],
    }, "UGX")
    const byMode = Object.fromEntries(out.map((m) => [m.mode, m.currencies]))
    expect(byMode["USD Wire"]).toEqual(["UGX", "USD"])
    expect(byMode["USD Box"]).toEqual(["USD"])
    expect(byMode["Airtel Money"]).toEqual(["UGX"])
  })
})
