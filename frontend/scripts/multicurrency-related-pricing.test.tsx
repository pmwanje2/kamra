import { readFileSync } from "node:fs"
import { describe, expect, it } from "vitest"

const source = (path: string) => readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8")

describe("related pricing currency displays", () => {
  it("uses the transaction currency of an amount voucher", () => {
    const options = source("components/RateOptions.tsx")
    expect(options).toContain("voucher_currency")
    expect(options).toContain("voucher_tx_amount")
    expect(options).toContain("Voucher {code} takes {txAmount} ({homeAmount}) off.")
  })

  it("formats fixed charge rows and inputs in their selected currency", () => {
    const fixed = source("components/FixedChargesPanel.tsx")
    expect(fixed).toMatch(/fmtMoney\([^\n]+row\.currency\s*\|\|\s*homeCurrency/)
    expect(fixed).toContain("moneyStep(selectedCurrency)")
    expect(fixed).not.toMatch(/\bcur\(/)
  })

  it("shows the rate currency beside the property accounting currency", () => {
    const quotation = source("screens/BookingQuotation.tsx")
    expect(quotation).toContain("currency_reference")
    expect(quotation).toContain("contract_rate")
    expect(quotation).toContain("Rate currency")
    expect(quotation).toContain("Property accounting currency")
  })

  it("offers stay currency entry and displays the server conversion", () => {
    const discount = source("components/FolioDiscount.tsx")
    expect(discount).toContain("Enter in stay currency ({contractCurrency})")
    expect(discount).toContain("discount_currency")
    expect(discount).toContain("fx_rate")
    expect(discount).toContain("moneyStep(entryCurrency)")
  })
})
