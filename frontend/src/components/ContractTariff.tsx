import type { ContractTariff, ReferenceCurrency, TariffReference } from "../lib/api"
import { t, useT } from "../lib/i18n"
import { activeMoneyLocale, moneyDigits } from "../lib/money"

export function currencyText(currency: string, amount: number, locale: string) {
  const digits = moneyDigits(currency)
  return `${currency} ${amount.toLocaleString(locale, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`
}

export function formatFxRate(rate: number, locale: string) {
  return rate.toLocaleString(locale, { useGrouping: true, maximumFractionDigits: rate >= 100 ? 2 : rate >= 1 ? 4 : 6 })
}

export function tariffDate(value: string, locale: string) {
  const date = new Date(`${value}T00:00:00Z`)
  if (!Number.isFinite(date.getTime())) return value
  return new Intl.DateTimeFormat(locale, { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" })
    .format(date)
}

/** A rate plan's rounding rule (pricing.round_rate_amount): its step, else the currency's smallest unit. */
export function roundingRule(step: number | null | undefined, mode: string | null | undefined, locale: string) {
  const how = t(mode || "Nearest")
  return step ? t("Rounded to {step}, {mode}", { step: step.toLocaleString(locale), mode: how })
    : t("Rounded to the currency's smallest unit, {mode}", { mode: how })
}

/** What a rate plan's exchange rate lock (OPERA Exchange Type) means for the local price. */
export function fxLockNote(policy: string | null | undefined) {
  switch (policy) {
    case "Fixed at Check In": return t("Fixed at Check In: converted again at the check-in date's rate.")
    case "Current Date": return t("Current Date: each night converts at its posting date's rate.")
    default: return t("Fixed at Reservation: the local price is fixed when the stay is booked.")
  }
}

/** A server timestamp ("YYYY-MM-DD HH:MM:SS", the site's own clock) as the site shows it. */
export function tariffDateTime(value: string, locale: string) {
  const date = new Date(`${value.slice(0, 19).replace(" ", "T")}Z`)
  if (!Number.isFinite(date.getTime())) return value
  return new Intl.DateTimeFormat(locale, {
    day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC",
  }).format(date)
}

export function TariffNight({ tariff, agreed_rate }: TariffReference) {
  const { t } = useT()
  const locale = activeMoneyLocale()
  if (!tariff || agreed_rate == null) return null
  // the stay's own first night: a stay shortened from the front or moved keeps its check-in night's scalars
  const first = tariff.nights?.[0] ?? { nightly: tariff.nightly, agreed_rate }
  // a stay over a tariff change: the nights priced apart from the first night
  const changes = (tariff.nights ?? []).filter((night) => night.nightly !== first.nightly || night.agreed_rate !== first.agreed_rate)
  return <span className="tabular-nums">{t("{contract} / night · {home} at {rate} ({date})", {
    contract: currencyText(tariff.currency, first.nightly, locale),
    home: currencyText(tariff.property_currency, first.agreed_rate, locale),
    rate: formatFxRate(tariff.fx_rate, locale),
    date: tariffDate(tariff.fx_date, locale),
  })}
    {changes.map((night) => <span key={night.date} className="block text-xs text-secondary-ink">
      {t("{date}: {contract} · {home}", {
        date: tariffDate(night.date, locale),
        contract: currencyText(tariff.currency, night.nightly, locale),
        home: currencyText(tariff.property_currency, night.agreed_rate, locale),
      })}
    </span>)}
  </span>
}

/** The bill in its contract currency: which rate (Actual or Current), its date, and the total and balance at it. */
export function ReferenceCurrencyNote({ reference }: { reference?: ReferenceCurrency | null }) {
  const { t, tn } = useT()
  const locale = activeMoneyLocale()
  if (!reference?.currency || !Number.isFinite(reference.fx_rate) || reference.fx_rate <= 0) return null
  const current = reference.calculation === "CURRENT"
  const nights = reference.posted_nights ?? 0
  const hasTotals = reference.grand_total_reference != null && reference.balance_reference != null
  return <p data-reference-currency className="mb-3 text-sm text-secondary-ink tabular-nums"
    title={current ? t("Converted at today's published rate.") : t("Converted at the rate the stay was priced or posted at.")}>
    {t(current ? "Reference currency {currency} · Current: 1 {currency} = {rate} {home} ({date})"
      : "Reference currency {currency} · Actual: 1 {currency} = {rate} {home} ({date})", {
      currency: reference.currency, rate: formatFxRate(reference.fx_rate, locale), home: reference.home_currency,
      date: reference.fx_date ? tariffDate(reference.fx_date, locale) : "—",
    })}
    {nights > 0 && <> · {tn(nights, "Rate {n} night posted at", "Average of the rates {n} nights posted at")}</>}
    {hasTotals && <span className="block font-medium text-ink">{t("Total {total} · Balance {balance}", {
      total: currencyText(reference.currency, reference.grand_total_reference!, locale),
      balance: currencyText(reference.currency, reference.balance_reference!, locale),
    })}</span>}
  </p>
}

export function TariffRoomTotal({ tariff, nights, agreedRate, locale }: {
  tariff: ContractTariff; nights: number; agreedRate: number; locale: string
}) {
  const { t } = useT()
  // a stay over a tariff change adds up each night's own tariff
  const own = tariff.nights?.length ? tariff.nights : null
  return <span className="tabular-nums">{t("Room tariff subtotal: {contract} · {home}", {
    contract: currencyText(tariff.currency, own ? own.reduce((sum, night) => sum + night.nightly, 0)
      : tariff.nightly * Math.max(1, nights), locale),
    home: currencyText(tariff.property_currency, own ? own.reduce((sum, night) => sum + night.agreed_rate, 0)
      : agreedRate * Math.max(1, nights), locale),
  })}</span>
}
