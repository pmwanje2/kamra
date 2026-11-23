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
} from "../lib/reservationDiscount"
import { useT } from "../lib/i18n"

const inputCls =
  "rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm disabled:bg-zinc-100 disabled:text-zinc-400 " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-brand-600"

export interface DiscountNight {
  date: string
  /** the night's room rate before the discount, on the basis the desk sees */
  base_rate: number
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
  /** the stay's nights, for Daily Details; omitted: stay-wide only */
  nights?: DiscountNight[]
  supervisor: string
  pin: string
  onSupervisor: (user: string) => void
  onPin: (pin: string) => void
  money: (n: number) => string
  /** shown under the controls, e.g. the manager's fixed-rate override */
  children?: React.ReactNode
}) {
  const { t } = useT()
  const { value: d, onChange, eligible, governance: gov } = props
  const [daily, setDaily] = useState(false)
  const [future, setFuture] = useState(false)
  const off = !eligible
  // a night already on the folio keeps its discount there (a folio
  // allowance): the bounds and "Apply to future nights" are the others'
  const open = (props.nights ?? []).filter((n) => !n.charged)
  const lowest = open.length ? Math.min(...open.map((n) => n.base_rate)) : undefined
  const error = discountError(d, open)
  const stayNights = open.map((n) => n.date)

  function setType(type: DiscountType) {
    onChange({ ...d, type })
  }
  function setNight(date: string, entry: { type: DiscountType; value: string }) {
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
        <div className="inline-flex rounded-lg bg-zinc-100 p-0.5">
          {segment("Percent", t("% off"))}
          {segment("Amount", t("Amount / night"))}
        </div>
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <input className={`${inputCls} w-28`} type="number" min="0" step="0.01" inputMode="decimal"
          aria-label={t("Discount value")} disabled={off} value={off ? "" : d.value}
          max={d.type === "Percent" ? 100 : lowest}
          onChange={(e) => onChange({ ...d, value: e.target.value })} />
        <select className={`${inputCls} min-w-44 flex-1`} aria-label={t("Discount reason")} required
          disabled={off} value={off ? "" : d.reason} onChange={(e) => onChange({ ...d, reason: e.target.value })}>
          <option value="">{t("Reason…")}</option>
          {props.reasons.map((r) => <option key={r.name} value={r.name}>{t(r.description || r.name)}</option>)}
        </select>
      </div>
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
              <div className="w-0 min-w-full overflow-x-auto">
                <table className="w-full whitespace-nowrap text-sm tabular-nums">
                  <thead>
                    <tr className="text-xs text-zinc-400">
                      <th className="pe-2 text-start font-normal">{t("Night")}</th>
                      <th className="px-2 text-end font-normal">{t("Base")}</th>
                      <th className="ps-2 text-start font-normal">{t("Discount")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {props.nights!.map((n) => {
                      const own = d.nights[n.date] ?? { type: d.type, value: "" }
                      return (
                        <tr key={n.date} data-night={n.date}>
                          <td className="pe-2">{n.date}</td>
                          <td className="px-2 text-end">{props.money(n.base_rate)}</td>
                          <td className="flex gap-1 ps-2">
                            <select className={inputCls} aria-label={`${t("Discount")} ${n.date}`} disabled={n.charged}
                              value={own.type} onChange={(e) => setNight(n.date, { ...own, type: e.target.value as DiscountType })}>
                              <option value="Percent">{t("% off")}</option>
                              <option value="Amount">{t("Amount / night")}</option>
                            </select>
                            <input className={`${inputCls} w-24`} type="number" min="0" step="0.01" inputMode="decimal"
                              disabled={n.charged} value={own.value} placeholder={d.value || "0"}
                              onChange={(e) => setNight(n.date, { ...own, value: e.target.value })} />
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {props.children}

      {!off && gov?.needs_supervisor && (
        <div className="space-y-2 rounded-lg bg-amber-50 p-2">
          <p className="text-xs text-amber-800">
            {!gov.can_set_rate && gov.max_pct > gov.limit_pct
              ? t("A cut of {pct}% is over the front desk's limit of {limit}%.", {
                  pct: gov.max_pct, limit: gov.limit_pct,
                }) + " " : ""}
            {gov.comp ? t("A 100% discount (comp) needs a manager's sign-off.") + " " : ""}
            {gov.floor_breach.length
              ? t("Below the rate floor on {dates}.", { dates: gov.floor_breach.join(", ") }) + " " : ""}
            {gov.reason_requires_supervisor ? t("This reason needs a manager's sign-off.") + " " : ""}
            {t("A manager signs this off with their PIN.")}
          </p>
          {gov.supervisors.length === 0 ? (
            <p className="text-xs text-rose-700">{t("No manager with a cashier PIN can approve here yet.")}</p>
          ) : (
            <div className="flex flex-wrap gap-2">
              <select className={inputCls} aria-label={t("Manager")} value={props.supervisor}
                onChange={(e) => props.onSupervisor(e.target.value)}>
                <option value="">{t("Manager…")}</option>
                {gov.supervisors.map((s) => <option key={s.user} value={s.user}>{s.full_name}</option>)}
              </select>
              <input className={`${inputCls} w-24`} type="password" inputMode="numeric" autoComplete="off"
                value={props.pin} onChange={(e) => props.onPin(e.target.value.replace(/\D/g, "").slice(0, 8))}
                placeholder={t("Their PIN")} aria-label={t("Manager PIN")} />
            </div>
          )}
        </div>
      )}
    </div>
  )
}
