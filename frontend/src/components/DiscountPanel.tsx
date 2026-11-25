/*  The desk's discount on a stay (OPERA Cloud Reservation Discounts): a
    percentage OR an amount off each night's room rate, a mandatory reason,
    and Daily Details nights of their own with "Apply to future nights". A
    rate plan that is not discountable disables it with the reason why. When
    the server says the cut needs a supervisor (over the desk's limit, a
    comp, under the rate floor, a reason that needs one), a manager signs it
    off with their PIN right here. Used by the booking dialog (new booking
    and walk-in) and the reservation's Stay rate & daily details editor. */

import { useState } from "react"
import {
  applyToFutureNights,
  discountError,
  type DiscountGovernance,
  type DiscountReason,
  type DiscountState,
  type DiscountType,
  type NightEntry,
} from "../lib/reservationDiscount"
import { useT } from "../lib/i18n"
import { moneyDigits, moneyStep, roundMoney } from "../lib/money"
import SupervisorSignOff, { SignOffReasons } from "./SupervisorSignOff"

const inputCls =
  "rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm disabled:bg-zinc-100 disabled:text-zinc-400 " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-brand-600"

export interface DiscountNight {
  date: string
  /** the night's room rate before the discount, on the basis the desk sees */
  base_rate: number
  /** the night's contract-currency room rate before the discount, when foreign */
  contract_base_rate?: number
  /** the contract currency code (e.g. USD, KWD) */
  contract_currency?: string
  /** already on the folio: a discount there is a folio allowance */
  charged?: boolean
}

export default function DiscountPanel(props: {
  value: DiscountState
  onChange: (next: DiscountState) => void
  reasons: DiscountReason[]
  /** the rate plan allows a desk discount (OPERA Rate Code "Discount") */
  eligible: boolean
  governance: DiscountGovernance | null
  /** rate plan / contract currency (e.g. USD, KWD) when selling foreign tariffs */
  rateCurrency?: string | null
  /** property / company currency (e.g. UGX) */
  homeCurrency?: string
  /** stay exchange rate (1 rateCurrency = fxRate homeCurrency) */
  fxRate?: number | null
  /** the stay's nights, for Daily Details; omitted: stay-wide only */
  nights?: DiscountNight[]
  supervisor: string
  pin: string
  onSupervisor: (user: string) => void
  onPin: (pin: string) => void
  money: (n: number, currency?: string) => string
  /** shown under the controls, e.g. the manager's fixed-rate override */
  children?: React.ReactNode
}) {
  const { t } = useT()
  const { value: d, onChange, eligible, governance: gov } = props
  const [daily, setDaily] = useState(false)
  const [future, setFuture] = useState(false)
  const off = !eligible
  const homeCur = (props.homeCurrency || "").trim().toUpperCase()
  const rateCur = (props.rateCurrency || "").trim().toUpperCase()
  const isForeign = Boolean(rateCur && homeCur && rateCur !== homeCur)
  const activeCurrency = isForeign ? (d.currency || rateCur) : homeCur
  // a night already on the folio keeps its discount there (a folio
  // allowance): the bounds and "Apply to future nights" are the others'
  const open = (props.nights ?? []).filter((n) => !n.charged)
  const baseFor = (n: DiscountNight, cur?: string) =>
    cur && n.contract_currency && cur.toUpperCase() === n.contract_currency.toUpperCase() && n.contract_base_rate !== undefined
      ? n.contract_base_rate
      : n.base_rate
  const lowest = open.length
    ? Math.min(...open.map((n) => baseFor(n, d.type === "Amount" ? activeCurrency : undefined)))
    : undefined
  const error = discountError(d, open, {
    rateCurrency: isForeign ? rateCur : undefined,
    homeCurrency: homeCur || undefined,
  })
  const stayNights = open.map((n) => n.date)
  const amountStep = moneyStep(moneyDigits(activeCurrency || undefined))
  const typedNum = d.value.trim() === "" ? null : Number(d.value)
  const fxEquiv = (() => {
    if (!isForeign || d.type !== "Amount" || typedNum === null || !Number.isFinite(typedNum) || typedNum <= 0) return null
    const rate = Number(props.fxRate || 0)
    if (!Number.isFinite(rate) || rate <= 0) return null
    const foreignDiscount = activeCurrency === rateCur
    const currency = foreignDiscount ? homeCur : rateCur
    const equivalents = open.map((night) => {
      if (night.contract_currency?.toUpperCase() !== rateCur || night.contract_base_rate === undefined) return null
      const base = night.contract_base_rate
      if (foreignDiscount) {
        const net = roundMoney(base - roundMoney(typedNum, rateCur), rateCur)
        if (net < 0) return null
        return roundMoney(night.base_rate - Math.min(night.base_rate, roundMoney(net * rate, homeCur)), homeCur)
      }
      const localNet = roundMoney(night.base_rate - typedNum, homeCur)
      if (localNet < 0) return null
      return roundMoney(base - Math.min(base, roundMoney(localNet / rate, rateCur)), rateCur)
    })
    const amount = equivalents[0]
    return amount != null && equivalents.every((equivalent) => equivalent === amount)
      ? { amount, currency }
      : null
  })()

  function setType(type: DiscountType) {
    onChange({
      ...d,
      type,
      currency: type === "Amount" && isForeign ? (d.currency || rateCur) : d.currency,
    })
  }
  function setNight(date: string, entry: NightEntry) {
    const nights = future
      ? applyToFutureNights(d.nights, stayNights, date, entry)
      : { ...d.nights, [date]: entry }
    onChange({ ...d, nights })
  }

  const segment = (type: DiscountType, label: string) => (
    <button type="button" aria-pressed={d.type === type} disabled={off} onClick={() => setType(type)}
      className={`px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${d.type === type
        ? "rounded-md bg-white text-zinc-900 shadow-sm" : "text-zinc-500"}`}>
      {label}
    </button>
  )

  return (
    <div role="group" aria-label={t("Discount")} className="space-y-2.5 rounded-xl border border-zinc-200 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-zinc-800">{t("Discount")}</h3>
        <div className="flex flex-wrap items-center gap-2">
          {isForeign && d.type === "Amount" && (
            <div role="group" aria-label={t("Discount currency")} className="inline-flex rounded-lg bg-zinc-100 p-0.5">
              {[rateCur, homeCur].map((code) => (
                <button key={code} type="button" aria-pressed={activeCurrency === code} disabled={off}
                  onClick={() => onChange({ ...d, currency: code })}
                  className={`px-2.5 py-1 text-xs font-medium disabled:opacity-50 ${activeCurrency === code
                    ? "rounded-md bg-white text-zinc-900 shadow-sm" : "text-zinc-500"}`}>
                  {code}
                </button>
              ))}
            </div>
          )}
          <div className="inline-flex rounded-lg bg-zinc-100 p-0.5">
            {segment("Percent", t("% off"))}
            {segment("Amount", t("Amount / night"))}
          </div>
        </div>
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex items-center gap-1.5">
          {d.type === "Amount" && activeCurrency && (
            <span className="inline-flex items-center rounded-md border border-zinc-200 bg-zinc-100 px-2 py-1.5 text-xs font-semibold tabular-nums text-zinc-700">
              {activeCurrency}
            </span>
          )}
          <input className={`${inputCls} w-28`} type="number" min="0" step={d.type === "Amount" ? amountStep : "0.01"} inputMode="decimal"
            aria-label={t("Discount value")} disabled={off} value={off ? "" : d.value}
            max={d.type === "Percent" ? 100 : lowest}
            onChange={(e) => onChange({ ...d, value: e.target.value })} />
        </div>
        <select className={`${inputCls} min-w-44 flex-1`} aria-label={t("Discount reason")} required
          disabled={off} value={off ? "" : d.reason} onChange={(e) => onChange({ ...d, reason: e.target.value })}>
          <option value="">{t("Reason…")}</option>
          {props.reasons.map((r) => <option key={r.name} value={r.name}>{t(r.description || r.name)}</option>)}
        </select>
        <input className={`${inputCls} min-w-40 flex-1`} aria-label={t("Discount note")} disabled={off}
          placeholder={t("Details (optional)")} maxLength={140} value={off ? "" : d.note ?? ""}
          onChange={(e) => onChange({ ...d, note: e.target.value })} />
      </div>
      {!off && fxEquiv && (
        <p className="text-xs tabular-nums text-zinc-500">
          {t("≈ {amt} off / night", { amt: props.money(fxEquiv.amount, fxEquiv.currency) })}
        </p>
      )}
      <p className="text-xs text-zinc-500">
        {off ? t("This rate plan does not allow discounts.")
          : t("Comes off the room rate only - meals, extras and tax keep their own price.")}
      </p>
      {!off && error && <p role="alert" className="text-xs text-rose-600">{t(error)}</p>}

      {!off && (props.nights?.length ?? 0) > 0 && (
        <div>
          <button type="button" className="text-xs font-medium text-brand-700 underline-offset-2 hover:underline"
            aria-expanded={daily} onClick={() => setDaily((v) => !v)}>
            {t("Per-night daily details")}
          </button>
          {daily && (
            <div className="mt-2 space-y-2">
              <label className="flex items-center gap-2 text-xs text-zinc-600">
                <input type="checkbox" aria-label={t("Apply to future nights")} checked={future}
                  onChange={(e) => setFuture(e.target.checked)} />
                {t("Apply to future nights")}
              </label>
              <table className="w-full table-fixed text-sm">
                <tbody>
                  {props.nights!.map((n) => {
                    const own = d.nights[n.date] ?? { type: d.type, value: "", currency: activeCurrency || undefined }
                    const nightCur = isForeign ? (own.currency || activeCurrency) : homeCur
                    const nightStep = moneyStep(moneyDigits(nightCur || undefined))
                    const showForeignBase = isForeign && n.contract_currency && n.contract_base_rate !== undefined
                    return (
                      <tr key={n.date} data-night={n.date} className="border-t border-zinc-200">
                        <td className="min-w-0 space-y-2 py-2">
                          <div className="flex flex-wrap justify-between gap-x-2 tabular-nums">
                            <span>{n.date}</span>
                            <span className="text-end">
                              {showForeignBase ? (
                                <>
                                  <span className="block">{props.money(n.contract_base_rate!, n.contract_currency)}</span>
                                  <span className="block text-xs text-zinc-400">{props.money(n.base_rate, homeCur)}</span>
                                </>
                              ) : props.money(n.base_rate, homeCur || undefined)}
                            </span>
                          </div>
                          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                            <select className={`${inputCls} min-w-0 max-w-full`} aria-label={`${t("Discount")} ${n.date}`} disabled={n.charged}
                              value={own.type} onChange={(e) => setNight(n.date, { ...own, type: e.target.value as DiscountType })}>
                              <option value="Percent">{t("% off")}</option>
                              <option value="Amount">{t("Amount / night")}</option>
                            </select>
                            {isForeign && own.type === "Amount" && (
                              <select className={`${inputCls} min-w-0 max-w-full`} aria-label={`${t("Discount currency")} ${n.date}`} disabled={n.charged}
                                value={nightCur} onChange={(e) => setNight(n.date, { ...own, currency: e.target.value })}>
                                <option value={rateCur}>{rateCur}</option>
                                <option value={homeCur}>{homeCur}</option>
                              </select>
                            )}
                            <input className={`${inputCls} w-24 min-w-0 max-w-full`} type="number" min="0" step={own.type === "Amount" ? nightStep : "0.01"} inputMode="decimal"
                              disabled={n.charged} value={own.value} placeholder={d.value || "0"}
                              onChange={(e) => setNight(n.date, { ...own, value: e.target.value })} />
                          </div>
                          <select className={`${inputCls} w-full min-w-0`} aria-label={`${t("Discount reason")} ${n.date}`}
                            disabled={n.charged} value={own.reason ?? ""}
                            onChange={(e) => setNight(n.date, { ...own, reason: e.target.value })}>
                            <option value="">{t("Same reason as the stay")}</option>
                            {props.reasons.map((r) => <option key={r.name} value={r.name}>{t(r.description || r.name)}</option>)}
                          </select>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {props.children}

      {!off && gov?.needs_supervisor && (
        <SupervisorSignOff reason={<SignOffReasons gov={gov} />} supervisors={gov.supervisors}
          supervisor={props.supervisor} pin={props.pin} onSupervisor={props.onSupervisor} onPin={props.onPin} />
      )}
    </div>
  )
}
