// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import {
  activeMoneyLocale,
  adoptUiLocale,
  fmtAmount,
  fmtMoney,
  moneyDigits,
  moneyEps,
  moneyStep,
  roundMoney,
} from "./money"
import {
  accountingImportLabel,
  assistant,
  deskLabel,
  logo,
  product,
  providerLabel,
  roleLabel,
  SERVICE_ROLE,
  voicePartner,
} from "./brand"
import { setLang } from "./dir"
import { tenderAmount } from "../components/TenderPicker"

vi.mock("./api", () => ({
  call: vi.fn(),
  getCurrentProperty: () => "Test Property",
}))

describe("lib/money canonical precision & locale", () => {
  beforeEach(() => {
    localStorage.clear()
    adoptUiLocale({
      currency: "USD",
      currency_symbol: "$",
      locale: "en-GB",
      decimals: 2,
      currency_decimals: { UGX: 0, USD: 2, EUR: 2, KWD: 3, BHD: 3, XTS: 0 },
    })
  })

  afterEach(() => {
    localStorage.clear()
    delete window.kamra_brand
  })

  it("resolves minor-unit digits with explicit -> currency_decimals -> Intl -> 2 fallback priority", () => {
    expect(moneyDigits("USD", 3)).toBe(3)
    expect(moneyDigits("UGX", 2)).toBe(2)
    expect(moneyDigits("KWD", 0)).toBe(0)
    expect(moneyDigits("UGX")).toBe(0)
    expect(moneyDigits("USD")).toBe(2)
    expect(moneyDigits("KWD")).toBe(3)
    expect(moneyDigits("BHD")).toBe(3)
    expect(moneyDigits("XTS")).toBe(0)
    expect(moneyDigits("JPY")).toBe(0)
    expect(moneyDigits("OMR")).toBe(3)
    expect(moneyDigits("UNKNOWN_CODE")).toBe(2)
  })

  it("computes moneyStep and moneyEps for both numeric decimals and currency codes", () => {
    expect(moneyStep(0)).toBe("1")
    expect(moneyStep(2)).toBe("0.01")
    expect(moneyStep(3)).toBe("0.001")
    expect(moneyStep("UGX")).toBe("1")
    expect(moneyStep("USD")).toBe("0.01")
    expect(moneyStep("KWD")).toBe("0.001")
    expect(moneyEps(0)).toBe(0.5)
    expect(moneyEps(2)).toBe(0.005)
    expect(moneyEps(3)).toBe(0.0005)
    expect(moneyEps("KWD")).toBe(0.0005)
  })

  it("rounds half-away-from-zero to currency minor units without -0 or float artifacts", () => {
    expect(roundMoney(12.3456, 3)).toBe(12.346)
    expect(roundMoney(12.3455, 3)).toBe(12.346)
    expect(roundMoney(-12.3455, 3)).toBe(-12.346)
    expect(roundMoney(1.005, 2)).toBe(1.01)
    expect(roundMoney(-1.005, 2)).toBe(-1.01)
    expect(roundMoney(50000.5, "UGX")).toBe(50001)
    expect(roundMoney(12.3456, "KWD")).toBe(12.346)
    expect(Object.is(roundMoney(-0.0001, 2), 0)).toBe(true)
    expect(roundMoney(1e-7, 2)).toBe(0)
  })

  it("formats UGX (0dp), USD (2dp), and KWD (3dp) dynamically", () => {
    expect(fmtMoney(12.5, "USD")).toBe("$12.50")
    expect(fmtMoney(50000, "UGX")).toBe("UGX 50,000")
    expect(fmtMoney(12.5, "KWD")).toBe("KWD 12.500")

    adoptUiLocale({ currency: "UGX", currency_symbol: "UGX ", locale: "en-UG", decimals: 0 })
    expect(fmtMoney(50000)).toBe("UGX 50,000")
    expect(moneyStep()).toBe("1")

    adoptUiLocale({ currency: "KWD", currency_symbol: "KWD ", locale: "en-GB", decimals: 3 })
    expect(fmtMoney(12.345)).toBe("KWD 12.345")
    expect(moneyStep()).toBe("0.001")
  })

  it("uses active UI language locale and never hardcodes en-US", () => {
    expect(activeMoneyLocale()).toBe("en-GB")
    setLang("es")
    expect(activeMoneyLocale()).toBe("es-u-nu-latn")
    expect(fmtAmount(1234.5, "USD")).toContain(",")

    setLang("de")
    expect(activeMoneyLocale()).toBe("de-u-nu-latn")
    expect(fmtAmount(1234.5, "USD")).toBe("1.234,50")

    setLang("ar")
    expect(activeMoneyLocale()).toBe("ar-u-nu-latn")
    // Western ASCII ledger digits preserved in Arabic
    expect(fmtAmount(1234.5, "USD")).toMatch(/1.?234\.50|1.?234,50/)
  })

  it("preserves KWD 3dp and UGX 0dp in TenderPicker.tenderAmount", () => {
    expect(tenderAmount(12.345, "KWD", "KWD", {})).toBe("12.345")
    expect(tenderAmount(50000, "UGX", "UGX", {})).toBe("50000")
    expect(tenderAmount(123450, "KWD", "UGX", { KWD: 10000 })).toBe("12.345")
    expect(tenderAmount(524194, "USD", "UGX", { USD: 4032.258064516 })).toBe("130")
    expect(tenderAmount(524195, "USD", "UGX", { USD: 4032.258064516 })).toBe("130.01")
  })
})

describe("lib/brand neutral white-label fallbacks", () => {
  afterEach(() => {
    delete window.kamra_brand
  })

  it("uses neutral fallbacks when siteBrand is absent or white-labelled", () => {
    delete window.kamra_brand
    expect(product()).toBe("Hotel PMS")
    expect(assistant()).toBe("Concierge Assistant")
    expect(roleLabel(SERVICE_ROLE)).toBe("{assistant} (service)")

    window.kamra_brand = { product: "", assistant: "", white_label: true }
    expect(product()).toBe("Hotel PMS")
    expect(assistant()).toBe("Concierge Assistant")
    expect(accountingImportLabel()).toBe("Accounting system import")
    expect(providerLabel(voicePartner)).toBe("Voice partner")
    expect(deskLabel()).toBeUndefined()
    expect(logo()).toBeNull()
  })
})
