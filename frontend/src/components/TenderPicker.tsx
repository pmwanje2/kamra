import { useEffect, useState } from "react"
import { call } from "../lib/api"
import { useT } from "../lib/i18n"
import type { TenderMode } from "../lib/money"

type Vocabulary = { payment_modes: string[]; tender_modes?: TenderMode[] | null; fx_currencies?: string[] }
type Rate = { currency: string; rate_date: string; buy_rate: number }
type Rates = { business_date: string; rates: Rate[]; tender_rates?: Rate[] }
type Choice = { mode: string; currency: string }
type BuyRates = Readonly<Record<string, number>>

export function useTenderRates(property: string, enabled = true) {
  const [state, setState] = useState<{ property: string; data: Rates }>()
  useEffect(() => {
    if (!property || !enabled) return
    let active = true
    call<Rates>("kamra.ledger.list_exchange_rates", { property })
      .then((data) => { if (active) setState({ property, data }) })
      .catch(() => { if (active) setState(undefined) })
    return () => { active = false }
  }, [property, enabled])
  const data = state?.property === property ? state.data : undefined
  const today: BuyRates = Object.fromEntries(
    (data?.tender_rates ?? data?.rates ?? [])
      .filter((r) => r.rate_date === data?.business_date && Number(r.buy_rate) > 0)
      .map((r) => [r.currency, Number(r.buy_rate)]),
  )
  return { today, rates: data?.rates ?? [] }
}

export function tenderModes(loc: Vocabulary, home: string, paymentLink = false): TenderMode[] {
  if (!home) return []
  const foreign = [...new Set((loc.fx_currencies ?? [])
    .map((c) => c.trim().toUpperCase()).filter((c) => /^[A-Z]{3}$/.test(c) && c !== home))]
  const codes = [home, ...foreign]
  const mapped = loc.tender_modes ?? [
    ...loc.payment_modes.map((mode) => ({ mode, currencies: ["Cash", "Bank Transfer"].includes(mode) ? codes : [home] })),
    ...(paymentLink ? [{ mode: "Payment Link", currencies: [home] }] : []),
  ]
  const gotForeignCash = mapped.some(({ mode }) => mode === "Foreign Cash")
  const cashCodes = mapped.filter(({ mode }) => mode === (gotForeignCash ? "Foreign Cash" : "Cash"))
    .flatMap(({ currencies }) => currencies.filter((c) => c !== home))
  const byMode = new Map<string, string[]>()
  for (const { mode, currencies } of mapped) {
    const allowed = mode === "Cash"
      ? [...currencies.filter((c) => c === home), ...cashCodes]
      : currencies.filter((c) => mode === "Foreign Cash" ? c !== home : mode === "Bank Transfer" || c === home)
    byMode.set(mode, [...(byMode.get(mode) ?? []), ...allowed])
  }
  if (!gotForeignCash && cashCodes.length) byMode.set("Foreign Cash", cashCodes)
  return [...byMode].map(([mode, allowed]) => ({ mode, currencies: codes.filter((c) => allowed.includes(c)) }))
    .filter(({ currencies }) => currencies.length > 0)
}

export function resolveTender(modes: TenderMode[], home: string, requested: Choice): Choice {
  const item = modes.find(({ mode }) => mode === requested.mode) ??
    modes.find(({ currencies }) => currencies.includes(home)) ?? modes[0]
  if (!item) return { mode: "", currency: "" }
  const currency = requested.currency
    ? item.mode === requested.mode && item.currencies.includes(requested.currency) ? requested.currency : ""
    : item.currencies.includes(home) ? home : ""
  return { mode: item.mode, currency }
}

export function tenderReady({ mode, currency }: Choice, home: string, rates: BuyRates) {
  return !!(mode && currency && (currency === home || rates[currency] > 0))
}

export function tenderAmount(balance: number, currency: string, home: string, rates: BuyRates) {
  if (!(balance > 0) || !currency) return ""
  if (currency === home) return String(Math.round(balance * 100) / 100)
  const rate = rates[currency]
  if (!(rate > 0)) return ""
  const cents = (balance / rate) * 100
  return String(Math.ceil(cents - Number.EPSILON * cents) / 100)
}

export default function TenderPicker({
  home, chosen, modes, rates = {}, onChange, fixedCurrency,
}: {
  home: string
  chosen: Choice
  modes: TenderMode[]
  rates?: BuyRates
  onChange: (value: Choice) => void
  fixedCurrency?: string
}) {
  const { t } = useT()
  const active = modes.find(({ mode }) => mode === chosen.mode)
  const codes = fixedCurrency ? [fixedCurrency] : active?.currencies ?? []
  const homeOnly = !fixedCurrency && codes.length === 1 && codes[0] === home
  const foreignCurrencies = [...new Set(modes.flatMap(({ currencies }) => currencies.filter((c) => c !== home)))]
  const displayed = homeOnly ? [home, ...foreignCurrencies] : codes
  const pill = (selected: boolean) => "rounded-lg border px-3 py-1.5 text-sm font-medium disabled:opacity-50 " +
    (selected ? "border-brand-600 bg-brand-50 text-brand-800" : "border-zinc-200 bg-white text-zinc-700")

  return (
    <div className="w-full min-w-0 space-y-2">
      <div role="group" aria-label={t("Payment mode")} className="flex flex-wrap gap-2">
        {modes.map(({ mode, currencies }) => (
          <button key={mode} type="button" aria-pressed={chosen.mode === mode} className={pill(chosen.mode === mode)}
            onClick={() => {
              if (chosen.mode === mode) return
              const currency = fixedCurrency || (currencies.includes(home) ? home : "")
              onChange({ mode, currency })
            }}>
            {t(mode)}
          </button>
        ))}
      </div>
      {modes.length === 0 && <p className="text-xs">{t("No payment methods are available.")}</p>}
      {displayed.length > 0 && (
        <div role="group" aria-label={t("Tender currency")} className="space-y-1">
          <p className="text-xs">{t("Tender currency")}</p>
          <div className="flex flex-wrap gap-2">
            {displayed.map((code) => {
              const wrongMode = homeOnly && code !== home
              const noRate = !fixedCurrency && code !== home && !(rates[code] > 0)
              const reason = wrongMode ? t("{mode} accepts {currency} only.", { mode: t(chosen.mode), currency: home })
                : noRate ? t("No published rate for {currency} on the business date.", { currency: code }) : ""
              return (
                <button key={code} type="button" aria-pressed={code === chosen.currency}
                  title={reason || undefined} disabled={!!reason || !!fixedCurrency}
                  className={pill(code === chosen.currency)}
                  onClick={() => {
                    if (code !== chosen.currency)
                      onChange({ currency: code, mode: chosen.mode === "Cash" && code !== home ? "Foreign Cash" : chosen.mode })
                  }}>
                  {code}
                </button>
              )
            })}
          </div>
          {homeOnly && foreignCurrencies.length > 0 && <p className="text-xs">
            {t("{mode} accepts {currency} only.", { mode: t(chosen.mode), currency: home })}
          </p>}
          {!homeOnly && !fixedCurrency && codes.filter((code) => code !== home && !(rates[code] > 0)).map((code) =>
            <p key={code} className="text-xs">{t("No published rate for {currency} on the business date.", { currency: code })}</p>
          )}
        </div>
      )}
    </div>
  )
}
