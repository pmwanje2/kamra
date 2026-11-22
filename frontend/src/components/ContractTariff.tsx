import type { ContractTariff, TariffReference } from "../lib/api"
import { useT } from "../lib/i18n"
import { moneyDigits, moneyLocale } from "../lib/money"

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

export function TariffNight({ tariff, agreed_rate }: TariffReference) {
  const { t } = useT()
  const locale = moneyLocale()
  if (!tariff || agreed_rate == null) return null
  return <span className="tabular-nums">{t("{contract} / night · {home} at {rate} ({date})", {
    contract: currencyText(tariff.currency, tariff.nightly, locale),
    home: currencyText(tariff.property_currency, agreed_rate, locale),
    rate: formatFxRate(tariff.fx_rate, locale),
    date: tariffDate(tariff.fx_date, locale),
  })}</span>
}

export function TariffRoomTotal({ tariff, nights, agreedRate, locale }: {
  tariff: ContractTariff; nights: number; agreedRate: number; locale: string
}) {
  const { t } = useT()
  return <span className="tabular-nums">{t("Room tariff subtotal: {contract} · {home}", {
    contract: currencyText(tariff.currency, tariff.nightly * Math.max(1, nights), locale),
    home: currencyText(tariff.property_currency, agreedRate * Math.max(1, nights), locale),
  })}</span>
}
