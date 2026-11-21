import { useEffect, useState } from "react"
import { call } from "../lib/api"
import { serverError } from "../lib/resource"
import { formatDate } from "../lib/utils"
import { t } from "../lib/i18n"

type Quote = {
  rate: number
  rate_date: string
  currency: string
  home_currency: string
  home_amount: number
  quote_id: string
}

export function useSettlementQuote(
  property: string,
  currency: string,
  amount: number,
) {
  const key = JSON.stringify([property, currency, amount])
  const [result, setResult] = useState<{
    key: string
    quote?: Quote
    error?: string
  }>()
  useEffect(() => {
    if (!currency || !property || !(amount > 0)) return
    let active = true
    call<Quote>("kamra.ledger.payment_rate", { property, currency, amount })
      .then((quote) => {
        if (active) setResult({ key, quote })
      })
      .catch((e) => {
        if (active) setResult({ key, error: serverError(e) })
      })
    return () => {
      active = false
    }
  }, [property, currency, amount, key])
  return result?.key === key ? result : undefined
}

export function CurrencyInput({
  home,
  value,
  onChange,
}: {
  home: string
  value: string
  onChange: (v: string) => void
}) {
  return (
    <label className="text-xs">
      {t("Tender currency")}
      <input
        className="ml-1 w-16 rounded border p-1 uppercase"
        aria-label={t("Tender currency")}
        maxLength={3}
        placeholder={home}
        value={value}
        onChange={(e) => onChange(e.target.value.toUpperCase().trim())}
      />
    </label>
  )
}

export function SettlementPreview({
  result,
  amount,
  balance,
}: {
  result: ReturnType<typeof useSettlementQuote>
  amount: number
  balance: number
}) {
  if (result?.error)
    return (
      <p role="alert" className="text-rose-700">
        {result.error}
      </p>
    )
  const q = result?.quote
  if (!q)
    return (
      <p role="status">
        {t("Enter the currency and amount to preview the published rate.")}
      </p>
    )
  const rest = Math.round((balance - q.home_amount) * 100) / 100
  const facts = [
    [t("Amount tendered"), `${q.currency} ${amount}`],
    [t("Exchange rate"), q.rate],
    [t("Effective rate date"), formatDate(q.rate_date)],
    [t("Company-currency equivalent"), `${q.home_currency} ${q.home_amount}`],
    [
      rest < 0 ? t("Change due") : t("Remaining balance"),
      `${q.home_currency} ${Math.abs(rest)}`,
    ],
  ]
  return (
    <dl
      className="flex w-full flex-wrap gap-3 rounded border p-2 text-xs"
      aria-live="polite"
    >
      {facts.map(([k, v]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd className="font-semibold">{v}</dd>
        </div>
      ))}
    </dl>
  )
}
