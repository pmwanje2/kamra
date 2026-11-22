import { useEffect, useState } from "react"
import { call } from "../lib/api"
import { serverError } from "../lib/resource"
import { currencyText, formatFxRate, tariffDate } from "./ContractTariff"
import { t } from "../lib/i18n"
import { moneyDigits, moneyLocale } from "../lib/money"

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
  const locale = moneyLocale()
  const scale = 10 ** moneyDigits(q.home_currency)
  const rest = (Math.round(balance * scale) - Math.round(q.home_amount * scale)) / scale
  const facts = [
    [t("Amount tendered"), currencyText(q.currency, amount, locale)],
    [t("Exchange rate"), `1 ${q.currency} = ${formatFxRate(q.rate, locale)} ${q.home_currency}`],
    [t("Effective rate date"), tariffDate(q.rate_date, locale)],
    [t("Local equivalent"), currencyText(q.home_currency, q.home_amount, locale)],
  ]
  const tone = rest === 0 ? "bg-emerald-50 text-emerald-700 border-emerald-200"
    : rest > 0 ? "bg-amber-50 text-amber-800 border-amber-200" : "bg-sky-50 text-sky-800 border-sky-200"
  return <dl className="w-full rounded-xl border border-zinc-200 bg-zinc-50/80 p-3.5 text-xs" aria-live="polite">
    {facts.map(([label, value]) => <div key={label} className="flex items-center justify-between gap-3 py-1">
      <dt>{label}</dt><dd className="text-right font-semibold tabular-nums">{value}</dd>
    </div>)}
    <div className="mt-2 flex items-center justify-between gap-3 border-t border-zinc-200 pt-2.5">
      <dt className={`rounded-full border px-2 py-1 font-medium ${tone}`}>
        {rest === 0 ? t("Settled in full") : rest > 0 ? t("Remaining balance") : t("Change due")}
      </dt>
      <dd className="font-bold tabular-nums">{currencyText(q.home_currency, Math.abs(rest), locale)}</dd>
    </div>
  </dl>
}
