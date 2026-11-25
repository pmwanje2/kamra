/*  Give a discount on a folio: a percent, or an amount for the stay or per
    room night, or a promo code. It comes off the room nights already on the
    bill and posts as an allowance (kamra.api.post_discount), so the
    property's allowance approval and voucher serials apply. */

import { useEffect, useState } from "react"
import { call } from "../lib/api"
import { cur, fmtAmount, moneyLocale, moneyStep } from "../lib/money"
import { serverError } from "../lib/resource"
import { isNoRoomNights } from "../lib/discountHint"
import { useT } from "../lib/i18n"
import { Button } from "./ui/button"
import { currencyText, formatFxRate } from "./ContractTariff"

const inputCls =
  "rounded-lg border border-control bg-surface px-3 py-1.5 text-sm " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-focus"

interface Preview {
  total: number
  label: string
  lines: { nights: number; base: number }[]
  conversion?: { home_amount: number; home_currency: string; fx_rate: number }
}

export default function FolioDiscount({ folio, adjustCodes: given, act, onPosted, onAdvanceBill, refreshKey, currency, currencySymbol, numberLocale }: {
  folio: string
  /** The Adjust reason codes; fetched here when the caller has none. */
  adjustCodes?: { name: string; description: string }[]
  /** FolioView's act(): unlocks the cashier PIN, shows errors, reloads. */
  act: (fn: () => Promise<unknown>) => Promise<void>
  onPosted: (pending: boolean) => void
  /** The folio's Advance bill action, offered when no night is posted yet. */
  onAdvanceBill?: () => void
  /** Changes when the folio reloads (after Advance bill posts nights), so the preview re-runs. */
  refreshKey?: unknown
  currency?: string | null
  currencySymbol?: string | null
  numberLocale?: string | null
}) {
  const { t } = useT()
  const sym = currencySymbol ?? cur()
  const fmt = (n: unknown) => fmtAmount(n, currency || undefined, numberLocale || undefined)
  const blank = { kind: "Percent", value: "", basis: "Stay", code: "", reason: "", reason_code: "" }
  const [d, setD] = useState(blank)
  const [stayCurrency, setStayCurrency] = useState<{ folio: string; currency: string } | null>(null)
  const [foreignSelection, setForeignSelection] = useState<{ folio: string; currency: string } | null>(null)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [hint, setHint] = useState<string | null>(null)
  const [noNights, setNoNights] = useState(false)
  const [pin, setPin] = useState<string | null>(null)
  const [fetched, setFetched] = useState<{ name: string; description: string }[]>([])
  const adjustCodes = given ?? fetched
  useEffect(() => {
    if (given) return
    call<{ name: string; description: string }[]>("kamra.ledger.list_reason_codes", { category: "Adjust" })
      .then(setFetched).catch(() => setFetched([]))
  }, [given])

  useEffect(() => {
    let live = true
    call<{ contract_currency?: string }>("kamra.api.folio_discount_currency", { folio })
      .then((reference) => {
        if (live) setStayCurrency(reference?.contract_currency ? { folio, currency: reference.contract_currency } : null)
      })
      .catch(() => { if (live) setStayCurrency(null) })
    return () => { live = false }
  }, [folio, refreshKey])

  const promo = d.code.trim()
  const contractCurrency = stayCurrency?.folio === folio ? stayCurrency.currency : null
  const foreignEntry = !promo && d.kind === "Amount" && foreignSelection?.folio === folio
  const discountCurrency = foreignEntry && foreignSelection.currency === contractCurrency ? contractCurrency : null
  const entryCurrency = (foreignEntry ? foreignSelection.currency : currency) || undefined
  const entryLabel = (foreignEntry ? foreignSelection.currency : sym.trim())
  const params = promo
    ? { folio, voucher_code: promo }
    : { folio, kind: d.kind, value: Number(d.value), basis: d.kind === "Amount" ? d.basis : "Stay",
        discount_currency: discountCurrency || undefined }

  // what it takes off, before anyone presses Give
  useEffect(() => {
    setPreview(null)
    setHint(null)
    setNoNights(false)
    if ((!promo && !(Number(d.value) > 0)) || (foreignEntry && !discountCurrency)) return
    let live = true
    const id = setTimeout(() => {
      call<Preview>("kamra.api.post_discount", { ...params, preview: 1 })
        .then((result) => { if (live) setPreview(result) })
        .catch((e) => {
          if (!live) return
          setHint(serverError(e))
          setNoNights(isNoRoomNights(e))
        })
    }, 350)
    return () => { live = false; clearTimeout(id) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [folio, promo, d.kind, d.value, d.basis, foreignEntry, discountCurrency, refreshKey])

  const nights = preview?.lines.reduce((s, l) => s + l.nights, 0) ?? 0
  const canGive = !!preview && (promo || d.reason.trim() || d.reason_code) && pin !== "" && (!foreignEntry || !!discountCurrency)

  return (
    <div className="mt-1 w-full border-t border-line-subtle pt-3">
      <div className="mb-2 text-xs font-medium uppercase tracking-wider text-muted-ink">
        {t("Give a discount")}
        <span className="ms-1.5 normal-case tracking-normal">
          {t("- off the room nights on the bill, or with a promo code")}
        </span>
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <select className={inputCls} aria-label={t("Discount type")} value={d.kind} disabled={!!promo}
          onChange={(e) => {
            if (foreignSelection?.folio === folio) {
              setD({ ...d, kind: e.target.value, value: "" })
              setForeignSelection(null)
            } else {
              setD({ ...d, kind: e.target.value })
            }
          }}>
          <option value="Percent">{t("Percent (%)")}</option>
          <option value="Amount">{t("Amount ({cur})", { cur: entryLabel })}</option>
        </select>
        <input className={`${inputCls} w-24`} type="number" min="0" step={d.kind === "Amount" ? moneyStep(entryCurrency) : "0.01"} inputMode="decimal" disabled={!!promo}
          placeholder={d.kind === "Percent" ? "%" : entryLabel} aria-label={t("Discount")}
          value={d.value} onChange={(e) => setD({ ...d, value: e.target.value })} />
        {d.kind === "Amount" && !promo && (
          <select className={inputCls} aria-label={t("Applies to")} value={d.basis}
            onChange={(e) => setD({ ...d, basis: e.target.value })}>
            <option value="Stay">{t("For the whole stay")}</option>
            <option value="Night">{t("Per night")}</option>
          </select>
        )}
        <span className="self-center text-xs text-muted-ink">{t("or")}</span>
        <input className={`${inputCls} w-32 uppercase`} placeholder={t("Promo code")}
          aria-label={t("Promo code")} value={d.code}
          onChange={(e) => setD({ ...d, code: e.target.value })} />
      </div>
      {d.kind === "Amount" && !promo && (contractCurrency || foreignEntry) && (
        <label className="mt-2 flex min-h-8 items-center gap-2 text-sm text-secondary-ink">
          <input type="checkbox" checked={foreignEntry} onChange={(e) => {
            setForeignSelection(e.target.checked && contractCurrency ? { folio, currency: contractCurrency } : null)
            setD({ ...d, value: "" })
          }} />
          {contractCurrency && (!foreignEntry || discountCurrency)
            ? t("Enter in stay currency ({contractCurrency})", { contractCurrency })
            : t("The stay currency exchange rate is unavailable.")}
        </label>
      )}
      {preview?.conversion && (
        <p className="mt-2 text-xs text-secondary-ink">
          {t("≈ {amount} @ {rate}", {
            amount: currencyText(preview.conversion.home_currency, preview.conversion.home_amount, numberLocale || moneyLocale()),
            rate: formatFxRate(preview.conversion.fx_rate, numberLocale || moneyLocale()),
          })}
        </p>
      )}
      <div className="mt-2 flex flex-wrap items-end gap-2">
        {adjustCodes.length > 0 && (
          <select className={inputCls} aria-label={t("Discount reason")} value={d.reason_code}
            onChange={(e) => setD({ ...d, reason_code: e.target.value })}>
            <option value="">{t("Reason…")}</option>
            {adjustCodes.map((c) => <option key={c.name} value={c.name}>{t(c.description)}</option>)}
          </select>
        )}
        <input className={`${inputCls} min-w-40 flex-1`}
          placeholder={d.reason_code || promo ? t("Details (optional)") : t("Reason (required)")}
          value={d.reason} onChange={(e) => setD({ ...d, reason: e.target.value })} />
        {pin !== null && (
          <input className={`${inputCls} w-24`} type="password" inputMode="numeric" autoComplete="off" autoFocus
            value={pin} onChange={(e) => setPin(e.target.value.replace(/\D/g, "").slice(0, 8))}
            placeholder={t("Your PIN")} aria-label={t("Your PIN")} />
        )}
        <Button variant="outline" disabled={!canGive}
          onClick={() => act(async () => {
            const r = await call<{ pending?: boolean }>("kamra.api.post_discount", {
              ...params, reason: d.reason.trim(), reason_code: d.reason_code || undefined,
              pin: pin || undefined,
            }).catch((e) => {
              // a manager gives it: their own PIN signs it, like an allowance
              if (serverError(e).includes("SUPERVISOR_PIN_REQUIRED")) {
                setPin("")
                return null
              }
              throw e
            })
            if (!r) return
            setPin(null)
            setD(blank)
            setForeignSelection(null)
            onPosted(!!r.pending)
          })}>
          {t("Give discount")}
        </Button>
      </div>
      {preview && (
        <p className="mt-2 text-xs text-secondary-ink">
          {t("{label}: {amount} off {n} room nights ({base}), before tax", {
            label: preview.label,
            amount: `${sym}${fmt(preview.total)}`,
            n: nights,
            base: `${sym}${fmt(preview.lines.reduce((s, l) => s + l.base, 0))}`,
          })}
        </p>
      )}
      {hint && <p className="mt-2 text-xs text-warning-ink">{hint}</p>}
      {noNights && onAdvanceBill && (
        <Button variant="outline" className="mt-2" onClick={onAdvanceBill}>
          {t("Advance bill")}
        </Button>
      )}
    </div>
  )
}
