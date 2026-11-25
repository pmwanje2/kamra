import { useState } from "react"
import { call } from "../lib/api"
import { useCashierAuth } from "../lib/cashierAuth"
import { useT } from "../lib/i18n"
import { cur, fmtAmount, moneyDigits, moneyStep, roundMoney, useLocale } from "../lib/money"
import { serverError } from "../lib/resource"
import { Badge } from "./ui/badge"
import { Button } from "./ui/button"

interface CreditRebillResult {
  mode?: "full" | "partial"
  credit_note?: string | null
  number?: string | null
  rebill?: string | null
  cancelled?: string | null
  status?: string | null
  message?: string | null
}

const inputCls =
  "w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-brand-600"

export default function CreditRebillDialog({
  folio,
  currency,
  credited = 0,
  onClose,
  onDone,
}: {
  folio: {
    name: string
    invoice_number: string | null
    sales_invoice?: string | null
    grand_total: number
    tax_total: number
  }
  currency: string
  credited?: number
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useT()
  const loc = useLocale()
  const { ensureUnlocked, status: pinStatus } = useCashierAuth()
  const digits = moneyDigits(currency)
  const step = moneyStep(currency)
  const hasTaxReturn = Boolean(loc.integrations?.tax_return)
  const left = Math.max(0, roundMoney(folio.grand_total - credited, currency))

  const [mode, setMode] = useState<"full" | "partial">("full")
  const [amount, setAmount] = useState("")
  const [reason, setReason] = useState("")
  const [pin, setPin] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<CreditRebillResult | null>(null)

  const val = Number(amount)
  const validDecimals =
    digits === 0
      ? /^\d+$/.test(amount.trim())
      : new RegExp(`^\\d+(?:\\.\\d{1,${digits}})?$`).test(amount.trim())
  const partialValid = mode === "full" || (Number.isFinite(val) && val > 0 && val <= left && validDecimals)
  const propTax =
    mode === "partial" && val > 0 && folio.grand_total > 0
      ? roundMoney((val * folio.tax_total) / folio.grand_total, currency)
      : 0
  const needsPinInput = Boolean(pinStatus?.required && !pinStatus?.unlocked)
  const ready = partialValid && Boolean(reason.trim()) && (!needsPinInput || Boolean(pin.trim()))

  const invoiceLabel = folio.invoice_number || folio.sales_invoice || folio.name

  async function submit() {
    if (!ready || busy) return
    setBusy(true)
    setError(null)
    try {
      if (pinStatus?.required && !pinStatus?.unlocked && !pin.trim()) {
        await ensureUnlocked()
      }
      const res = await call<CreditRebillResult>("kamra.payment_transfers.credit_and_rebill", {
        folio: folio.name,
        reason: reason.trim(),
        mode,
        ...(mode === "partial" ? { amount: val } : {}),
        ...(pin.trim() ? { pin: pin.trim() } : {}),
      })
      setResult(res ?? { mode })
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-[60] flex items-end justify-center sm:items-center print:hidden"
      role="dialog"
      aria-modal="true"
      aria-label={t("Credit & Rebill")}
    >
      <div className="absolute inset-0 bg-black/40" onClick={result ? onDone : onClose} aria-hidden />
      <div className="relative w-full max-w-lg rounded-t-2xl bg-white shadow-2xl sm:rounded-2xl">
        <header className="border-b border-zinc-200 px-5 py-4">
          <h2 className="text-lg font-semibold text-zinc-900">{t("Credit & Rebill")}</h2>
          <p className="text-xs text-zinc-500">
            {invoiceLabel} · {cur()}
            {fmtAmount(folio.grand_total, currency)}
          </p>
        </header>

        {result ? (
          <div className="space-y-3 px-5 py-4 text-sm">
            <div className="rounded-xl border border-emerald-200 bg-emerald-50/70 p-4 space-y-2">
              <div className="flex items-center justify-between gap-2">
                <span className="font-semibold text-emerald-900">
                  {result.mode === "partial"
                    ? t("Partial credit note prepared")
                    : t("Credit & Rebill prepared")}
                </span>
                {result.status && <Badge tone="amber">{t(result.status)}</Badge>}
              </div>
              {result.credit_note && (
                <p className="font-mono text-xs text-zinc-800">
                  {t("Credit note")}: {result.credit_note}
                </p>
              )}
              {result.number && result.number !== result.credit_note && (
                <p className="font-mono text-xs text-zinc-800">
                  {t("Credit note number")}: {result.number}
                </p>
              )}
              {result.rebill && (
                <p className="font-mono text-xs text-zinc-800">
                  {t("Replacement invoice")}: {result.rebill}
                </p>
              )}
              {result.cancelled && (
                <p className="font-mono text-xs text-zinc-800">
                  {t("Cancelled invoice")}: {result.cancelled}
                </p>
              )}
              {result.message && <p className="text-xs text-zinc-600">{result.message}</p>}
            </div>
            <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              {t("Second manager/Finance approval required to finalize credit note & release replacement invoice.")}
            </p>
            <div className="flex justify-end pt-1">
              <Button type="button" onClick={onDone}>
                {t("Done")}
              </Button>
            </div>
          </div>
        ) : (
          <>
            <div className="space-y-4 px-5 py-4 text-sm">
              <fieldset className="space-y-2">
                <legend className="sr-only">{t("Correction mode")}</legend>
                <label className="flex cursor-pointer items-start gap-2.5 rounded-xl border border-zinc-200 p-3 hover:border-brand-600/40">
                  <input
                    type="radio"
                    name="credit-rebill-mode"
                    value="full"
                    className="mt-0.5 accent-brand-600"
                    checked={mode === "full"}
                    onChange={() => setMode("full")}
                  />
                  <span className="space-y-0.5">
                    <span className="block font-medium text-zinc-900">{t("Full Credit & Rebill")}</span>
                    <span className="block text-xs text-zinc-500">
                      {t("Credits the full invoice and drafts a replacement invoice for correction.")}
                    </span>
                    {hasTaxReturn && (
                      <span className="block font-mono text-[11px] text-brand-700">
                        {t("Tax return reason code: 102 (Cancellation of goods/services)")}
                      </span>
                    )}
                  </span>
                </label>

                <label className="flex cursor-pointer items-start gap-2.5 rounded-xl border border-zinc-200 p-3 hover:border-brand-600/40">
                  <input
                    type="radio"
                    name="credit-rebill-mode"
                    value="partial"
                    className="mt-0.5 accent-brand-600"
                    checked={mode === "partial"}
                    onChange={() => setMode("partial")}
                  />
                  <span className="space-y-0.5">
                    <span className="block font-medium text-zinc-900">{t("Partial Credit Note")}</span>
                    <span className="block text-xs text-zinc-500">
                      {t("Credits a specific amount (up to {left}) without reissuing the full invoice.", {
                        left: `${cur()}${fmtAmount(left, currency)}`,
                      })}
                    </span>
                    {hasTaxReturn && (
                      <span className="block font-mono text-[11px] text-brand-700">
                        {t("Tax return reason code: 104 (Partial waive off / adjustment)")}
                      </span>
                    )}
                  </span>
                </label>
              </fieldset>

              {mode === "partial" && (
                <div className="space-y-1.5">
                  <label className="block space-y-1 text-xs text-zinc-600">
                    <span>{t("Partial credit amount")}</span>
                    <input
                      aria-label={t("Partial credit amount")}
                      className={inputCls}
                      type="number"
                      min="0"
                      max={left}
                      step={step}
                      inputMode={digits === 0 ? "numeric" : "decimal"}
                      placeholder={t("Amount to credit, tax included")}
                      value={amount}
                      onChange={(e) => setAmount(e.target.value)}
                    />
                  </label>
                  {val > left && (
                    <p className="text-xs text-amber-700">
                      {t("At most {amount} can still be credited on this invoice.", {
                        amount: `${cur()}${fmtAmount(left, currency)}`,
                      })}
                    </p>
                  )}
                  {propTax > 0 && val <= left && (
                    <p className="text-xs text-zinc-500">
                      {t("Tax in this credit note: {amount}", {
                        amount: `${cur()}${fmtAmount(propTax, currency)}`,
                      })}
                    </p>
                  )}
                </div>
              )}

              <label className="block space-y-1 text-xs text-zinc-600">
                <span>{t("Reason")}</span>
                <input
                  aria-label={t("Credit & Rebill reason")}
                  className={inputCls}
                  maxLength={500}
                  placeholder={t("Reason (required - goes on the record)")}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </label>

              {needsPinInput && (
                <label className="block space-y-1 text-xs text-zinc-600">
                  <span>{t("Cashier PIN")}</span>
                  <input
                    aria-label={t("Cashier PIN")}
                    className={inputCls}
                    type="password"
                    inputMode="numeric"
                    autoComplete="off"
                    placeholder={t("Your PIN")}
                    value={pin}
                    onChange={(e) => setPin(e.target.value.replace(/\D/g, "").slice(0, 8))}
                  />
                </label>
              )}

              <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                {t("Second manager/Finance approval required to finalize credit note & release replacement invoice.")}
              </p>

              {error && (
                <p role="alert" className="rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-700">
                  {error}
                </p>
              )}
            </div>

            <footer className="flex justify-end gap-2 border-t border-zinc-100 px-5 py-3">
              <Button type="button" variant="outline" disabled={busy} onClick={onClose}>
                {t("Close")}
              </Button>
              <Button
                type="button"
                data-testid="submit-credit-rebill"
                disabled={busy || !ready}
                onClick={() => void submit()}
              >
                {busy
                  ? t("Submitting…")
                  : mode === "full"
                    ? t("Draft Credit & Rebill")
                    : t("Draft Partial Credit Note")}
              </Button>
            </footer>
          </>
        )}
      </div>
    </div>
  )
}
