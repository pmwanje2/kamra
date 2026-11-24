/*  Stay rate & daily details for one stay (OPERA Stay Details / Daily
    Details), two ways:

    Discount - a % OR an amount off each night still to come, or nights of
    their own with "Apply to future nights", with a Rate Change or Adjust
    reason. kamra.api.discount_preview shows each night's base, discount and
    net and the new total; kamra.api.set_reservation_discount saves it. The
    base rate, an agreed rate and a contract's snapshot stay as booked.

    Fixed rate - agree a different nightly rate (OPERA rate override /
    negotiated rate): kamra.api.rate_override_preview shows each night's old
    rate, new rate and difference plus the new total; kamra.api.set_agreed_rate
    saves it with a Rate Change reason code.

    An agreed rate is OPERA's Fixed Rate: it replaces the stay's discount,
    so the rate entered is what the guest pays, and its cut is measured from
    each night's rate-code amount. Either way the front desk works within
    the property's limit and above the rate floor; past them, or for a
    reason code that needs a supervisor, a Hotel Admin or Finance user signs
    it off with their PIN. The rate card is never touched. */

import { useEffect, useState } from "react"
import { call } from "../lib/api"
import {
  agreedRateNeedsSupervisor,
  agreedRateReady,
  agreedRateReasonCodes,
  agreedRateRows,
  discountReasonCodes,
  type ReasonCategories,
  type AgreedRatePreview,
} from "../lib/agreedRate"
import { cur, fmtMoney, moneyStep } from "../lib/money"
import {
  discountArgs,
  discountNote,
  discountReady,
  emptyDiscount,
  supervisorArgs,
  type DiscountGovernance,
  type DiscountReason,
  type DiscountState,
  type DiscountType,
} from "../lib/reservationDiscount"
import { serverError } from "../lib/resource"
import { useT } from "../lib/i18n"
import DiscountPanel from "./DiscountPanel"
import SupervisorSignOff from "./SupervisorSignOff"
import { Button } from "./ui/button"

const inputCls =
  "rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-brand-600"

const money = (n: number) => fmtMoney(n || 0)
const signed = (n: number) => `${n > 0 ? "+" : n < 0 ? "−" : ""}${money(Math.abs(n))}`

/** kamra.api.discount_preview: the rules plus what saving would do. */
interface DiscountPlan extends DiscountGovernance {
  current_amount: number
  new_amount: number
  difference: number
  needs_confirmation: boolean
  nights: {
    date: string; base_rate: number; discount_amount: number; rate: number; charged: boolean
    /** base and net on the basis the desk enters a discount (gross where rates include tax) */
    entry_base_rate?: number; entry_rate?: number
  }[]
}

export default function AgreedRatePanel({ reservation, nightRates, current, canOverrideFixed = false,
  reasonCategories, onSaved }: {
  reservation: string
  /** the nightly rates the folio bills now (reservation_detail.night_rates) */
  nightRates: {
    date: string; rate: number; base_rate?: number; discount_type?: DiscountType | null; discount_value?: number
    /** the night's own reason (blank: the stay's) */
    discount_reason?: string | null
    /** the night's base on the basis the desk enters a discount */
    entry_base_rate?: number
    /** already on the folio: its discount is a folio allowance, never re-sent */
    charged?: boolean
  }[]
  /** the stay's discount now (reservation_detail.discount); fixed_rate: it
   * holds a manager's fixed-rate override */
  current?: {
    discount_type: DiscountType | null; discount_value: number; discount_reason: string | null; fixed_rate?: number
  } | null
  /** a Revenue Manager / Hotel Admin: may override a plan that takes no
   * discount with a fixed rate (kamra.api._can_set_rate) */
  canOverrideFixed?: boolean
  /** the reason-code categories the discount and the agreed rate take (the
   * property's settings, reservation_detail.reason_categories) */
  reasonCategories?: ReasonCategories | null
  onSaved: () => void
}) {
  const { t } = useT()
  const rateStep = moneyStep()
  const [open, setOpen] = useState(false)
  const [mode, setMode] = useState<"discount" | "fixed">("discount")
  const [rate, setRate] = useState("")
  const [preview, setPreview] = useState<AgreedRatePreview | null>(null)
  const [reasonCode, setReasonCode] = useState("")
  const [reason, setReason] = useState("")
  const [supervisor, setSupervisor] = useState("")
  const [pin, setPin] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setPreview(null)
    if (!open || mode !== "fixed" || !(Number(rate) > 0)) return
    let live = true
    const id = setTimeout(() => {
      call<AgreedRatePreview>("kamra.api.rate_override_preview", { reservation, agreed_rate: Number(rate) })
        .then((p) => {
          if (!live) return
          setPreview(p)
          setError(null)
          if (p.supervisors.length === 1) setSupervisor(p.supervisors[0].user)
        })
        .catch((e) => live && setError(serverError(e)))
    }, 350)
    return () => {
      live = false
      clearTimeout(id)
    }
  }, [open, mode, rate, reservation])

  // --- Discount: the stay's, or Daily Details nights of their own. A night
  // already on the folio keeps its discount there and is never sent again.
  const openNights = nightRates.filter((n) => !n.charged)
  const initial = (): DiscountState => ({
    ...emptyDiscount(),
    type: current?.discount_type ?? "Percent",
    value: current?.discount_type ? String(current.discount_value) : "",
    reason: current?.discount_reason ?? "",
    nights: Object.fromEntries(openNights.filter((n) => n.discount_type)
      .map((n) => [n.date, { type: n.discount_type as DiscountType, value: String(n.discount_value ?? 0),
        ...(n.discount_reason ? { reason: n.discount_reason } : {}) }])),
  })
  const [discount, setDiscount] = useState<DiscountState>(initial)
  // a stay that holds a manager's fixed-rate override reopens with it on
  const seededOverride = () => Boolean(current?.fixed_rate && canOverrideFixed)
  const [fixedOverride, setFixedOverride] = useState(seededOverride)
  const [allCodes, setAllCodes] = useState<DiscountReason[]>([])
  // each one's reasons: the codes of the categories the property set
  const discountCodes = discountReasonCodes(allCodes, reasonCategories?.discount)
  const codes = agreedRateReasonCodes(allCodes, reasonCategories?.agreed_rate)
  const [plan, setPlan] = useState<DiscountPlan | null>(null)
  const stayNights = openNights.map((n) => n.date)
  const args = discountArgs(discount, stayNights)
  const hadDiscount = !!current?.discount_type || openNights.some((n) => n.discount_type)
  // clearing a discount the stay has is a change too
  const asks = !!(args.discount_type || args.night_discounts?.length) || hadDiscount
  const argsKey = JSON.stringify([args, fixedOverride])

  // one request for every reason the panel offers, each code listed once
  useEffect(() => {
    if (!open) return
    call<DiscountReason[]>("kamra.ledger.list_reason_codes", {})
      .then((all) => setAllCodes(all ?? []))
      .catch((e) => setError(serverError(e)))
  }, [open])

  useEffect(() => {
    setPlan(null)
    if (!open || mode !== "discount" || !asks) return
    let live = true
    const id = setTimeout(() => {
      call<DiscountPlan>("kamra.api.discount_preview", {
        reservation, discount_type: args.discount_type, discount_value: args.discount_value,
        reason_code: args.discount_reason, night_discounts: args.night_discounts,
        fixed_rate: fixedOverride ? 1 : 0,
      })
        .then((p) => {
          if (!live) return
          setPlan(p)
          setError(null)
          if (p.supervisors.length === 1) setSupervisor(p.supervisors[0].user)
        })
        .catch((e) => live && setError(serverError(e)))
    }, 350)
    return () => {
      live = false
      clearTimeout(id)
    }
    // argsKey stands for args and the override
  }, [open, mode, argsKey, reservation])

  // a cleared discount is ready too (it puts the stay back on its rate); a
  // plan that takes no discount needs a manager's fixed-rate override
  const discountOk = !!plan && discountReady({ args, governance: plan, supervisor, pin, fixedOverride })

  async function saveDiscount() {
    if (!plan || !discountOk) return
    setBusy(true)
    setError(null)
    try {
      await call("kamra.api.set_reservation_discount", {
        reservation,
        discount_type: args.discount_type,
        discount_value: args.discount_value,
        reason_code: args.discount_reason,
        night_discounts: args.night_discounts,
        fixed_rate: fixedOverride ? 1 : 0,
        ...supervisorArgs(plan, supervisor, pin),
        ...discountNote(discount, args),
        // the desk has seen the new price in the preview
        confirm_price: 1,
      })
      close()
      onSaved()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  function close() {
    setOpen(false)
    setMode("discount")
    setDiscount(initial())
    setFixedOverride(false)
    setPlan(null)
    setRate("")
    setPreview(null)
    setReasonCode("")
    setReason("")
    setSupervisor("")
    setPin("")
    setError(null)
  }

  const codeNeedsSupervisor = !!codes.find((c) => c.name === reasonCode)?.requires_supervisor
  const askSupervisor = !!preview && agreedRateNeedsSupervisor(preview, codeNeedsSupervisor)
  const ready = agreedRateReady({ preview, reasonCode, codeNeedsSupervisor, supervisor, pin })
  const rows = preview ? agreedRateRows(nightRates, preview) : []

  async function save() {
    if (!preview || !ready) return
    setBusy(true)
    setError(null)
    try {
      await call("kamra.api.set_agreed_rate", {
        reservation,
        agreed_rate: Number(rate),
        reason_code: reasonCode,
        reason: reason.trim(),
        confirm_price: preview.needs_confirmation ? 1 : 0,
        supervisor: askSupervisor ? supervisor : undefined,
        pin: askSupervisor ? pin.trim() : undefined,
      })
      close()
      onSaved()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  // every time it opens, it starts from the stay as it is now (a save
  // elsewhere, or this panel's own, re-renders it with fresh props)
  function openPanel() {
    setDiscount(initial())
    setFixedOverride(seededOverride())
    setPlan(null)
    setOpen(true)
  }

  if (!open)
    return (
      <Button variant="outline" className="mt-3" onClick={openPanel}>
        {t("Stay rate & daily details")}
      </Button>
    )

  const tab = (key: "discount" | "fixed", label: string) => (
    <button type="button" role="tab" aria-selected={mode === key} onClick={() => { setMode(key); setError(null) }}
      className={`px-3 py-1 text-sm font-medium ${mode === key ? "rounded-md bg-white text-zinc-900 shadow-sm" : "text-zinc-500"}`}>
      {label}
    </button>
  )
  const planNights = new Map((plan?.nights ?? []).map((n) => [n.date, n]))

  return (
    <div className="mt-3 space-y-2 rounded-lg border border-brand-600/30 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs font-medium uppercase tracking-wider text-zinc-400">
          {t("Stay rate & daily details")}
          <span className="ms-1.5 normal-case tracking-normal">{t("- this stay only; the rate card stays as it is")}</span>
        </div>
        <div role="tablist" className="inline-flex rounded-lg bg-zinc-100 p-0.5">
          {tab("discount", t("Discount"))}
          {tab("fixed", t("Fixed rate"))}
        </div>
      </div>

      {mode === "discount" && (<>
        <DiscountPanel value={discount} onChange={setDiscount} reasons={discountCodes}
          eligible={!plan || plan.eligible || fixedOverride} governance={plan}
          nights={nightRates.map((n) => {
            const p = planNights.get(n.date)
            return {
              date: n.date,
              base_rate: p?.entry_base_rate ?? p?.base_rate ?? n.entry_base_rate ?? n.base_rate ?? n.rate,
              charged: !!(n.charged || p?.charged),
            }
          })}
          supervisor={supervisor} pin={pin} onSupervisor={setSupervisor} onPin={setPin} money={money}>
          {plan && !plan.eligible && plan.can_set_rate && (
            <label className="flex items-center gap-2 text-xs text-zinc-600">
              <input type="checkbox" checked={fixedOverride} onChange={(e) => setFixedOverride(e.target.checked)} />
              {t("Fixed rate override")}
            </label>
          )}
        </DiscountPanel>
        {plan && (() => {
          const totalBase = plan.nights.reduce((s, n) => s + (n.entry_base_rate ?? n.base_rate), 0)
          const totalDiscount = plan.nights.reduce((s, n) => s + (n.discount_amount || 0), 0)
          const totalNet = plan.nights.reduce((s, n) => s + (n.entry_rate ?? n.rate), 0)
          return (
            <div className="space-y-1.5">
              <div className="w-0 min-w-full overflow-x-auto">
                <table className="w-full whitespace-nowrap text-xs tabular-nums">
                  <thead>
                    <tr className="text-zinc-400">
                      <th className="pe-2 text-start font-normal">{t("Night")}</th>
                      <th className="px-1.5 text-end font-normal">{t("Base")}</th>
                      <th className="px-1.5 text-end font-normal">{t("Discount")}</th>
                      <th className="ps-1.5 text-end font-normal">{t("Net")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {plan.nights.map((n) => (
                      <tr key={n.date} className={n.charged ? "text-zinc-400" : ""}>
                        <td className="pe-2">{n.date}</td>
                        <td className="px-1.5 text-end">{money(n.entry_base_rate ?? n.base_rate)}</td>
                        <td className="px-1.5 text-end text-emerald-700">{n.discount_amount ? signed(-n.discount_amount) : "-"}</td>
                        <td className="ps-1.5 text-end">{money(n.entry_rate ?? n.rate)}</td>
                      </tr>
                    ))}
                    <tr className="border-t border-zinc-200 font-medium">
                      <td className="pe-2">{t("Total")}</td>
                      <td className="px-1.5 text-end">{money(totalBase)}</td>
                      <td className="px-1.5 text-end text-emerald-700">{totalDiscount ? signed(-totalDiscount) : "-"}</td>
                      <td className="ps-1.5 text-end">{money(totalNet)}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              {Math.abs(plan.difference) >= 0.005 && (
                <p className="flex flex-wrap items-baseline justify-between gap-2 text-xs tabular-nums text-zinc-600">
                  <span>{t("Change from current booking")}</span>
                  <span className="font-medium text-zinc-800">
                    {money(plan.current_amount)} → {money(plan.new_amount)} ({signed(plan.difference)})
                  </span>
                </p>
              )}
            </div>
          )
        })()}
        {error && <p className="text-xs text-rose-600">{t(error)}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={close}>{t("Cancel")}</Button>
          <Button disabled={busy || !discountOk} onClick={() => void saveDiscount()}>
            {busy ? t("Saving…") : t("Save discount")}
          </Button>
        </div>
      </>)}

      {mode === "fixed" && (<>
      <div className="text-xs font-medium uppercase tracking-wider text-zinc-400">
        {t("Agreed rate")}
      </div>
      {hadDiscount && (
        <p className="text-xs text-zinc-500">{t("The agreed rate replaces this stay's discount.")}</p>
      )}
      <label className="block">
        <span className="text-xs text-zinc-400">{t("New rate / night")}</span>
        <input className={`${inputCls} mt-0.5 w-full`} type="number" min="0" step={rateStep} inputMode="decimal"
          placeholder={cur()} value={rate} onChange={(e) => setRate(e.target.value)} />
      </label>

      {preview && (
        <>
          <div className="w-0 min-w-full overflow-x-auto">
          <table className="w-full whitespace-nowrap text-sm tabular-nums">
            <thead>
              <tr className="text-xs text-zinc-400">
                <th className="pe-2 text-start font-normal">{t("Night")}</th>
                <th className="px-2 text-end font-normal">{t("Was")}</th>
                <th className="px-2 text-end font-normal">{t("Now")}</th>
                <th className="ps-2 text-end font-normal">{t("Difference")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.date}>
                  <td className="pe-2">{r.date}</td>
                  <td className="px-2 text-end">{money(r.was)}</td>
                  <td className="px-2 text-end">{money(r.now)}</td>
                  <td className={`ps-2 text-end ${r.diff < 0 ? "text-emerald-700" : r.diff > 0 ? "text-amber-700" : ""}`}>
                    {signed(r.diff)}
                  </td>
                </tr>
              ))}
              <tr className="border-t border-zinc-200 font-medium">
                <td className="pe-2">{t("Total")}</td>
                <td className="px-2 text-end">{money(preview.current_amount)}</td>
                <td className="px-2 text-end">{money(preview.new_amount)}</td>
                <td className="ps-2 text-end">{signed(preview.difference)}</td>
              </tr>
            </tbody>
          </table>
          </div>
          {!preview.can_set_rate && preview.raises && (
            <p className="text-xs text-rose-600">
              {t("The front desk can lower a stay's rate, not raise it. Ask a Revenue Manager.")}
            </p>
          )}
        </>
      )}

      <div className="flex flex-wrap items-end gap-2">
        <select className={inputCls} aria-label={t("Reason…")} value={reasonCode}
          onChange={(e) => setReasonCode(e.target.value)}>
          <option value="">{t("Reason…")}</option>
          {codes.map((c) => <option key={c.name} value={c.name}>{t(c.description)}</option>)}
        </select>
        <input className={`${inputCls} min-w-40 flex-1`} placeholder={t("Details (optional)")}
          value={reason} onChange={(e) => setReason(e.target.value)} />
      </div>

      {askSupervisor && preview && (
        <SupervisorSignOff supervisors={preview.supervisors} supervisor={supervisor} pin={pin}
          onSupervisor={setSupervisor} onPin={setPin}
          reason={preview.discount_governance?.comp
            ? t("A 100% discount (comp) needs a manager's sign-off.")
            : <>
                {preview.needs_supervisor && preview.reduction_pct > preview.limit_pct
                  ? t("A cut of {pct}% is over the front desk's limit of {limit}%.", {
                      pct: preview.reduction_pct, limit: preview.limit_pct,
                    }) + " " : ""}
                {preview.floor_breach?.length
                  ? t("Below the rate floor on {dates}.", { dates: preview.floor_breach.join(", ") }) + " " : ""}
                {codeNeedsSupervisor ? t("This reason needs a manager's sign-off.") : ""}
              </>} />
      )}

      {error && <p className="text-xs text-rose-600">{t(error)}</p>}

      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={close}>{t("Cancel")}</Button>
        <Button disabled={busy || !ready} onClick={() => void save()}>
          {busy ? t("Saving…") : t("Save agreed rate")}
        </Button>
      </div>
      </>)}
    </div>
  )
}
