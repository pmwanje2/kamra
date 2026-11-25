import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { describe, it, expect, vi } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"
import { FiscalDocument, type Fiscal } from "../src/components/FiscalDocument"
import { eventBilling } from "../src/lib/eventBilling"
import { ITEM_TYPES, inr, payModes } from "../src/screens/banquet/shared"

const mockLocale: {
  currency: string
  currency_symbol: string
  locale: string
  payment_modes: string[]
  instant_payment_modes: string[]
  tender_modes: { mode: string; currencies: string[] }[] | null
} = {
  currency: "UGX",
  currency_symbol: "Sh",
  locale: "en-UG",
  payment_modes: ["Cash", "Card", "Bank Transfer", "Mobile Money"],
  instant_payment_modes: ["Mobile Money"],
  tender_modes: null,
}

function mockDigitsFor(currency = mockLocale.currency) {
  try {
    if (currency)
      return new Intl.NumberFormat("en", { style: "currency", currency }).resolvedOptions()
        .maximumFractionDigits ?? 2
  } catch {
    /* ignore */
  }
  return 2
}

vi.mock("../src/lib/money", () => ({
  cur: () => mockLocale.currency_symbol,
  locale: () => mockLocale,
  activeMoneyLocale: () => mockLocale.locale,
  moneyLocale: () => mockLocale.locale,
  moneyStep: (c?: string | number | null) => {
    const d = typeof c === "number" ? c : mockDigitsFor(c || mockLocale.currency)
    return d <= 0 ? "1" : `0.${"0".repeat(d - 1)}1`
  },
  fmtAmount: (n: unknown, c?: string, l?: string) => {
    const d = mockDigitsFor(c ?? mockLocale.currency)
    return Number(n ?? 0).toLocaleString(l || mockLocale.locale, { minimumFractionDigits: d, maximumFractionDigits: d })
  },
  fmtMoney: (n: unknown, ctx?: { currency?: string | null; currency_symbol?: string | null; locale?: string | null } | null) => {
    const currency = ctx?.currency || mockLocale.currency
    const symbol =
      ctx && ctx.currency_symbol != null
        ? ctx.currency_symbol
        : ctx?.currency && ctx.currency !== mockLocale.currency
          ? `${ctx.currency} `
          : mockLocale.currency_symbol
    const numLocale = ctx?.locale || (ctx?.currency && ctx.currency !== mockLocale.currency ? "en-US" : mockLocale.locale)
    const d = mockDigitsFor(currency)
    return symbol + Number(n ?? 0).toLocaleString(numLocale, { minimumFractionDigits: d, maximumFractionDigits: d })
  },
}))
vi.mock("../src/lib/i18n", () => ({
  t: (s: string, vars?: Record<string, unknown>) =>
    s.replace(/\{(\w+)\}/g, (_m, k: string) => String(vars?.[k] ?? `{${k}}`)),
  useUILanguage: () => "en",
  uiDateLocale: () => "en-US",
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
  it("treats not_efris as pending when the company is enrolled in EFRIS (PR-9)", () => {
    const enrolled = {
      ...fiscalised,
      state: "not_efris" as const,
      fdn: null,
      verification_code: null,
      qr: null,
      efris_enrolled: 1 as const,
    } as Fiscal
    const html = renderToStaticMarkup(<FiscalDocument fiscal={enrolled} internalNumber="INV-UTA-26-00001" />)
    expect(html).toContain("Fiscal document pending")
    expect(html).toContain("not a tax invoice")
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

describe("banquet tender modes and item types", () => {
  it("includes Service Charge in banquet ITEM_TYPES", () => {
    expect(ITEM_TYPES).toContain("Service Charge")
  })

  it("derives banquet payModes from configured home-currency tender_modes without Foreign Cash or hardcoded Cheque/Other", () => {
    mockLocale.currency = "UGX"
    mockLocale.tender_modes = [
      { mode: "Cash", currencies: ["UGX", "USD"] },
      { mode: "Mobile Money", currencies: ["UGX"] },
      { mode: "Foreign Cash", currencies: ["UGX", "USD"] },
    ]
    expect(payModes()).toEqual(["Cash", "Mobile Money"])
  })

  it("falls back to locale payment_modes without Foreign Cash or appending Cheque/Other when tender_modes is unset", () => {
    mockLocale.currency = "UGX"
    mockLocale.tender_modes = null
    mockLocale.payment_modes = ["Cash", "Card", "Bank Transfer", "Mobile Money", "Foreign Cash"]
    expect(payModes()).toEqual(["Cash", "Card", "Bank Transfer", "Mobile Money"])
  })
})

describe("PR #45 i18n catalog and 7-locale coverage", () => {
  const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
  const requiredKeys = [
    "Fiscal document pending",
    "Fiscal status unavailable",
    "Test fiscal number - not a valid tax invoice",
    "This is not a tax invoice yet. It becomes one when the fiscal number is issued.",
    "Status: {status}",
    "Fiscal Document Number (FDN)",
    "Verification code",
    "Issued {when}",
    "Fiscal QR code",
    "Internal folio no. {number}",
    "Reprint fiscal invoice",
    "Reprint invoice",
    "Copy - reprint {n}",
    "Posts to this function's own folio. Its deposits are taken there, and its tax invoice is issued when that folio is settled.",
    "Open the function's folio",
    "The tax invoice is issued from the function's folio.",
    "Service Charge",
    "Invoice",
    "Tax Invoice",
  ]
  const placeholders = (s: string) => [...new Set([...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]))].sort()

  it("ships every fiscal/event UI string in catalog.csv and all 7 locale dictionaries", () => {
    const csv = fs.readFileSync(path.join(frontendRoot, "src/i18n/catalog.csv"), "utf8")
    const missingCatalog = requiredKeys.filter((key) => !csv.includes(key))
    expect(missingCatalog, "missing from catalog.csv").toEqual([])
    const localesDir = path.join(frontendRoot, "src/i18n/locales")
    const langs = ["ar", "de", "es", "fil", "fr", "id", "pt", "zh-CN"]
    for (const lang of langs) {
      const dict = JSON.parse(fs.readFileSync(path.join(localesDir, `${lang}.json`), "utf8")) as Record<string, string>
      for (const key of requiredKeys) {
        expect(typeof dict[key] === "string" && dict[key].trim().length > 0, `${lang}.json missing ${key}`).toBe(true)
        expect(placeholders(dict[key]), `${lang}.json placeholder mismatch for ${key}`).toEqual(placeholders(key))
      }
    }
  })
})

describe("Hydrogen R2 #4 — document-scoped currency formatting and country-neutral banquet icon", () => {
  it("formats banquet inr() with document currency/symbol/decimals instead of global UGX cache", () => {
    mockLocale.currency = "UGX"
    mockLocale.currency_symbol = "UGX "
    mockLocale.locale = "en-UG"
    const kwdCtx = { currency: "KWD", currency_symbol: "KD", locale: "en-US" }
    expect((inr as (n: unknown, ctx?: unknown) => string)(37.475, kwdCtx)).toBe("KD37.475")
    expect((inr as (n: unknown, ctx?: unknown) => string)(10.125, kwdCtx)).toBe("KD10.125")
    expect((inr as (n: unknown, ctx?: unknown) => string)(27.35, kwdCtx)).toBe("KD27.350")
  })

  it("supports explicit currency, symbol, and number locale in real lib/money fmtMoney and fmtAmount", async () => {
    const realMoney = await vi.importActual<typeof import("../src/lib/money")>("../src/lib/money")
    realMoney.adoptUiLocale({ currency: "UGX", currency_symbol: "UGX ", locale: "en-UG" })
    expect(
      (realMoney.fmtMoney as (n: unknown, ctx?: unknown) => string)(37.475, {
        currency: "KWD",
        currency_symbol: "KD",
        locale: "en-US",
      }),
    ).toBe("KD37.475")
    expect(
      (realMoney.fmtAmount as (n: unknown, c?: string, l?: string) => string)(123456.375, "KWD", "en-US"),
    ).toBe("123,456.375")
  })

  it("removes hardcoded BadgeIndianRupee from BanquetFunction.tsx", () => {
    const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
    const src = fs.readFileSync(path.join(frontendRoot, "src/screens/BanquetFunction.tsx"), "utf8")
    expect(src).not.toContain("BadgeIndianRupee")
  })
})
