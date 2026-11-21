/*  Agree a different nightly rate for this stay only (OPERA rate override /
    negotiated rate): kamra.api.rate_override_preview shows each night's old
    rate, new rate and difference plus the new total; kamra.api.set_agreed_rate
    saves it with a Rate Change reason code. The front desk may only lower a
    rate, by up to the property's limit; a bigger cut, or a reason code that
    needs a supervisor, is signed off by a Hotel Admin or Finance user with
    their PIN. The rate card is never touched. */

import { useEffect, useState } from "react"
import { call } from "../lib/api"
import { agreedRateReady, agreedRateRows, type AgreedRatePreview } from "../lib/agreedRate"
import { cur, moneyLocale } from "../lib/money"
import { serverError } from "../lib/resource"
import { useT } from "../lib/i18n"
import { Button } from "./ui/button"

const inputCls =
  "rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-brand-600"

interface ReasonCode { name: string; description: string; requires_supervisor: number }

// whole currency units, like the rest of the reservation drawer
const money = (n: number) =>
  `${cur()}${Number(n || 0).toLocaleString(moneyLocale(), { maximumFractionDigits: 0 })}`
const signed = (n: number) => `${n > 0 ? "+" : n < 0 ? "−" : ""}${money(Math.abs(n))}`

export default function AgreedRatePanel({ reservation, nightRates, onSaved }: {
  reservation: string
  /** the nightly rates the folio bills now (reservation_detail.night_rates) */
  nightRates: { date: string; rate: number }[]
  onSaved: () => void
}) {
  const { t } = useT()
  const [open, setOpen] = useState(false)
  const [rate, setRate] = useState("")
  const [preview, setPreview] = useState<AgreedRatePreview | null>(null)
  const [codes, setCodes] = useState<ReasonCode[]>([])
  const [reasonCode, setReasonCode] = useState("")
  const [reason, setReason] = useState("")
  const [supervisor, setSupervisor] = useState("")
  const [pin, setPin] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    call<ReasonCode[]>("kamra.ledger.list_reason_codes", { category: "Rate Change" })
      .then(setCodes)
      .catch((e) => setError(serverError(e)))
  }, [open])

  useEffect(() => {
    setPreview(null)
    if (!open || !(Number(rate) > 0)) return
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
  }, [open, rate, reservation])

  function close() {
    setOpen(false)
    setRate("")
    setPreview(null)
    setReasonCode("")
    setReason("")
    setSupervisor("")
    setPin("")
    setError(null)
  }

  const codeNeedsSupervisor = !!codes.find((c) => c.name === reasonCode)?.requires_supervisor
  const askSupervisor = !!preview && !preview.can_set_rate && (preview.needs_supervisor || codeNeedsSupervisor)
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

  if (!open)
    return (
      <Button variant="outline" className="mt-3" onClick={() => setOpen(true)}>
        {t("Agreed rate")}
      </Button>
    )

  return (
    <div className="mt-3 space-y-2 rounded-lg border border-brand-600/30 p-3">
      <div className="text-xs font-medium uppercase tracking-wider text-zinc-400">
        {t("Agreed rate")}
        <span className="ms-1.5 normal-case tracking-normal">{t("- this stay only; the rate card stays as it is")}</span>
      </div>
      <label className="block">
        <span className="text-xs text-zinc-400">{t("New rate / night")}</span>
        <input className={`${inputCls} mt-0.5 w-full`} type="number" min="0" inputMode="decimal"
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
        <div className="space-y-2 rounded-lg bg-amber-50 p-2">
          <p className="text-xs text-amber-800">
            {preview.needs_supervisor
              ? t("A cut of {pct}% is over the front desk's limit of {limit}%.", {
                  pct: preview.reduction_pct, limit: preview.limit_pct,
                })
              : null}{" "}
            {t("A manager signs this off with their PIN.")}
          </p>
          {preview.supervisors.length === 0 ? (
            <p className="text-xs text-rose-700">{t("No manager with a cashier PIN can approve here yet.")}</p>
          ) : (
            <div className="flex flex-wrap gap-2">
              <select className={inputCls} aria-label={t("Manager")} value={supervisor}
                onChange={(e) => setSupervisor(e.target.value)}>
                <option value="">{t("Manager…")}</option>
                {preview.supervisors.map((s) => <option key={s.user} value={s.user}>{s.full_name}</option>)}
              </select>
              <input className={`${inputCls} w-24`} type="password" inputMode="numeric" autoComplete="off"
                value={pin} onChange={(e) => setPin(e.target.value.replace(/\D/g, "").slice(0, 8))}
                placeholder={t("Their PIN")} aria-label={t("Manager PIN")} />
            </div>
          )}
        </div>
      )}

      {error && <p className="text-xs text-rose-600">{t(error)}</p>}

      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={close}>{t("Cancel")}</Button>
        <Button disabled={busy || !ready} onClick={() => void save()}>
          {busy ? t("Saving…") : t("Save agreed rate")}
        </Button>
      </div>
    </div>
  )
}
