/*  Currency and tax vocabulary, from the property's localization pack. Loaded
    once per session; neutral defaults (no symbol, plain "Tax") until it resolves.
    The last resolved locale is kept in localStorage so screens render with
    the right symbol immediately on reload, before the network answers. */

import { useEffect, useState } from "react"
import { call, getCurrentProperty, type TaxBasis } from "./api"
import { adoptGuestLanguage, applyEnabledLanguages } from "./dir"

export interface TenderMode {
  mode: string
  currencies: string[]
}

/** One code of a hotel master: the stored value and what the desk shows. */
export interface Choice {
  value: string
  label: string
}

interface Locale {
  currency_symbol: string
  locale: string
  currency: string
  date_format?: string | null
  tax_label: string
  tax_basis?: TaxBasis | null
  erp_tax_managed?: boolean
  tax_id_label: string
  tax_rates: number[]
  /** ID documents this country's desk records (pack ID_TYPES). */
  id_types: string[]
  /** A help line per ID type, e.g. which copy of a national ID is fine. */
  id_type_hints: Record<string, string>
  /** Ways to pay the desk offers - always canonical Folio Payment modes. */
  payment_modes: string[]
  tender_modes?: TenderMode[] | null
  fx_currencies?: string[]
  /** The pack's own instant / wallet modes; the till keeps
   *  them in their own bucket (system_upi). Empty where there are none. */
  instant_payment_modes: string[]
  /** What an outlet bill settles with: Cash, Card and the instant modes. */
  outlet_payment_modes: string[]
  default_nationality: string
  /** Under this age an occupant is a minor: name and age only. */
  adult_age: number
  /** "foreign": a minor's ID is kept only for foreign nationals;
   *  "all": the country's register covers every guest. */
  minor_ids: "foreign" | "all"
  /** Country integrations this property has - India: Razorpay/Cashfree,
   *  GSTR-1, Kamra Verify. Off until the pack resolves. */
  integrations: Integrations
  /** The rate the server puts on any food & beverage charge, whatever the
   *  caller picked, and the words that make a charge F&B. */
  fnb_tax_rate?: number
  fnb_charge_words?: string[]
  /** The property's rate on a guest service (laundry, spa, misc): where
   *  the folio's Post a charge box starts. */
  services_tax_rate?: number
  /** Off for a dry property: alcohol fields and labels are hidden. */
  sells_alcohol?: boolean
  /** The hotel's own Market codes, booker relations and Cancellation
   *  Reasons (Settings data, OPERA codes) - never a list typed in a screen. */
  market_codes?: Choice[]
  booker_relations?: Choice[]
  cancellation_reasons?: Choice[]
}

export interface Integrations {
  payment_gateways: string[]
  tax_return: boolean
  verify_tax_id: boolean
  verify_guest_id: boolean
}

let cache: Locale = {
  currency_symbol: "",
  locale: "en",
  currency: "",
  tax_label: "Tax",
  tax_id_label: "Tax ID",
  tax_rates: [],
  id_types: [],
  payment_modes: [],
  instant_payment_modes: [],
  outlet_payment_modes: [],
  id_type_hints: {},
  default_nationality: "",
  adult_age: 18,
  minor_ids: "foreign",
  integrations: {
    payment_gateways: [],
    tax_return: false,
    verify_tax_id: false,
    verify_guest_id: false,
  },
}

const listeners = new Set<() => void>()

function syncDateFormat(fmt?: string | null) {
  if (fmt && typeof window !== "undefined") {
    ;(window as Window & { kamra_date_format?: string }).kamra_date_format = fmt
  }
}

try {
  const saved = JSON.parse(localStorage.getItem("kamra_locale") || "")
  if (saved && saved.currency) {
    cache = { ...cache, ...saved }
    syncDateFormat(cache.date_format)
  }
} catch {
  /* first run - neutral defaults until the pack resolves */
}

function remember() {
  listeners.forEach((fn) => fn())
  try {
    localStorage.setItem("kamra_locale", JSON.stringify(cache))
  } catch {
    /* private mode */
  }
}

export function loadLocale(): Promise<Locale> {
  const property = getCurrentProperty()
  return call<Locale>("kamra.api.property_locale", {
    property,
  })
    .then((l) => {
      if (property !== getCurrentProperty()) return cache
      cache = { ...cache, tender_modes: null, fx_currencies: [], tax_basis: null, erp_tax_managed: false, ...l }
      syncDateFormat(cache.date_format)
      remember()
      return cache
    })
    .catch(() => cache)
}

/** Public pages carry `ui_locale` in their payload - adopt it directly. */
export function adoptUiLocale(ui?: {
  currency_symbol?: string
  locale?: string
  currency?: string
  default_language?: string | null
  enabled_languages?: string[] | null
} | null) {
  if (!ui) return
  if (Array.isArray(ui.enabled_languages)) applyEnabledLanguages(ui.enabled_languages)
  // guest pages only: the hotel's booking engine language, unless the guest picked one
  adoptGuestLanguage(ui.default_language)
  cache = {
    ...cache,
    // "" is a valid symbol (generic pack shows bare numbers)
    currency_symbol: ui.currency_symbol ?? cache.currency_symbol,
    locale: ui.locale || cache.locale,
    currency: ui.currency || cache.currency,
  }
  remember()
}

export const locale = () => cache
/** False on a dry property (Settings → Property): hide every alcohol field. */
export const sellsAlcohol = () => cache.sells_alcohol !== false
/** The property's currency symbol, e.g. "₹" or "Rp". */
export const cur = () => cache.currency_symbol
/** The number-formatting locale, e.g. "en-IN" (lakhs) or "id-ID". */
export const moneyLocale = () => cache.locale
/** The currency's minor-unit digits (2 for INR/USD, 0 for JPY), 2 until
 *  the pack resolves or for a code Intl does not know. */
export function moneyDigits(currency = cache.currency) {
  try {
    if (currency)
      return new Intl.NumberFormat("en", { style: "currency", currency }).resolvedOptions()
        .maximumFractionDigits ?? 2
  } catch {
    /* unknown currency code */
  }
  return 2
}
/** An amount with the currency's normal decimals, always the same count
 *  (2,27,255.40, never 2,27,255.4), so a column of figures lines up. */
export const fmtAmount = (n: unknown) => {
  const d = moneyDigits()
  return Number(n ?? 0).toLocaleString(cache.locale, { minimumFractionDigits: d, maximumFractionDigits: d })
}
/** fmtAmount with the currency symbol in front. */
export const fmtMoney = (n: unknown) => cache.currency_symbol + fmtAmount(n)
export const taxRates = () => cache.tax_rates
export const taxLabel = () => cache.tax_label
/** False where the hotel charges no tax: its only rate is 0% (a country
 *  without hotel tax). Forms then hide the tax picker; documents hide tax
 *  whenever none of their lines is taxed. */
export const chargesTax = () =>
  !(cache.tax_rates.length === 1 && Number(cache.tax_rates[0]) === 0)
/** Heading for the till's instant-mode bucket, "" where the country has none. */
export const instantLabel = () => (cache.instant_payment_modes ?? []).join(" / ")

/** The property's locale, re-rendering when the country pack resolves. */
export function useLocale(): Locale {
  const [l, setL] = useState(cache)
  useEffect(() => {
    const fn = () => setL(cache)
    listeners.add(fn)
    loadLocale()
    return () => {
      listeners.delete(fn)
    }
  }, [])
  return l
}
