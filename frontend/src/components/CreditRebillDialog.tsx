import { useState } from "react"
import { call, type InvoiceCorrectionResult } from "../lib/api"
import { useCashierAuth } from "../lib/cashierAuth"
import { useT } from "../lib/i18n"
import { fmtMoney, moneyDigits, moneyStep, roundMoney, type MoneyContext } from "../lib/money"
import { serverError } from "../lib/resource"
import { Badge } from "./ui/badge"
import { Button } from "./ui/button"

type CreditRebillResult = InvoiceCorrectionResult & { mode: "full" | "partial" }

const inputCls =
  "min-h-11 w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-focus"

export default function CreditRebillDialog({
  folio,
  currency,
  money,
  fiscalScheme,
  allowFull = true,
  allowPartial = true,
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
  money?: MoneyContext
  fiscalScheme?: string | null
  allowFull?: boolean
  allowPartial?: boolean
  credited?: number
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useT()
  const moneyContext = money ?? { currency }
  const { ensureUnlocked, status: pinStatus } = useCashierAuth()
  const digits = moneyDigits(currency, money?.decimals ?? undefined)
  const step = moneyStep(digits)
  const hasTaxReturn = fiscalScheme === "efris"
  const left = Math.max(0, roundMoney(folio.grand_total - credited, digits))

  const [mode, setMode] = useState<"full" | "partial">(allowFull ? "full" : "partial")
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
      ? roundMoney((val * folio.tax_total) / folio.grand_total, digits)
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
      const args = {
        folio: folio.name,
        reason: reason.trim(),
        ...(pin.trim() ? { pin: pin.trim() } : {}),
      }
      const res = mode === "full"
        ? await call<InvoiceCorrectionResult>("kamra.api.cancel_invoice", args)
        : await call<InvoiceCorrectionResult>("kamra.ledger.issue_credit_note", { ...args, amount: val })
      setResult({
        ...res,
        mode,
        credit_note: res?.credit_note ?? (mode === "partial" ? res?.sales_invoice : undefined),
        rebill: res?.rebill ?? (mode === "full" ? res?.sales_invoice : undefined),
        status: res?.status ?? (res?.draft ? "Draft" : undefined),
      })
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
      <div className="relative max-h-dvh w-full max-w-lg overflow-y-auto rounded-t-2xl bg-surface shadow-2xl sm:rounded-2xl">
        <header className="border-b border-line px-5 py-4">
          <h2 className="text-lg font-semibold text-ink">{t("Credit & Rebill")}</h2>
          <p className="text-xs text-muted-ink">
            {invoiceLabel} · <span dir="ltr" className="inline-block tabular-nums">{fmtMoney(folio.grand_total, moneyContext)}</span>
          </p>
        </header>

        {result ? (
          <div className="space-y-3 px-5 py-4 text-sm">
            <div className="rounded-xl border border-success-line bg-success-subtle/70 p-4 space-y-2">
              <div className="flex items-center justify-between gap-2">
                <span className="font-semibold text-success-ink">
                  {result.mode === "partial"
                    ? t("Partial credit note prepared")
                    : t("Credit & Rebill prepared")}
                </span>
                {result.status && <Badge tone="amber">{t(result.status)}</Badge>}
              </div>
              {result.credit_note && (
                <p className="font-mono text-xs text-ink">
                  {t("Credit note")}: {result.credit_note}
                </p>
              )}
              {result.number && result.number !== result.credit_note && (
                <p className="font-mono text-xs text-ink">
                  {t("Credit note number")}: {result.number}
                </p>
              )}
              {result.mode === "full" && result.rebill && (
                <p className="font-mono text-xs text-ink">
                  {t("Replacement invoice")}: {result.rebill}
                </p>
              )}
              {result.cancelled && (
                <p className="font-mono text-xs text-ink">
                  {t("Cancelled invoice")}: {result.cancelled}
                </p>
              )}
              {result.message && <p className="text-xs text-secondary-ink">{result.message}</p>}
            </div>
            <p className="rounded-lg border border-warning-line bg-warning-subtle px-3 py-2 text-xs text-warning-ink">
              {result.mode === "partial"
                ? t("Second manager/Finance approval required to finalize the partial credit note.")
                : folio.sales_invoice ? t("Second manager/Finance approval required to finalize credit note & release replacement invoice.") : null}
            </p>
            <div className="flex justify-end pt-1">
              <Button className="min-h-11" type="button" onClick={onDone}>
                {t("Done")}
              </Button>
            </div>
          </div>
        ) : (
          <>
            <div className="space-y-4 px-5 py-4 text-sm">
              {folio.sales_invoice && (
                <p className="text-secondary-ink">
                  {folio.invoice_number
                    ? t("Invoice {invoice} was issued by the accounting system as {document}.", {
                        invoice: folio.invoice_number, document: folio.sales_invoice,
                      })
                    : t("The accounting system issued invoice {document} for this bill.", { document: folio.sales_invoice })}
                </p>
              )}
              <fieldset className="space-y-2">
                <legend className="sr-only">{t("Correction mode")}</legend>
                {allowFull && <label className="flex cursor-pointer items-start gap-2.5 rounded-xl border border-line p-3 hover:border-accent-control/40">
                  <input
                    type="radio"
                    name="credit-rebill-mode"
                    value="full"
                    className="mt-0.5 accent-focus"
                    checked={mode === "full"}
                    onChange={() => setMode("full")}
                  />
                  <span className="space-y-0.5">
                    <span className="block font-medium text-ink">{t("Full Credit & Rebill")}</span>
                    {mode === "full" && <span className="block text-xs text-muted-ink">
                      {folio.sales_invoice
                        ? t("Credits the full invoice and drafts a replacement invoice for correction.")
                        : t("Cancel {invoice}? The number goes on the cancelled register and the folio reopens for correction.", { invoice: invoiceLabel })}
                    </span>}
                    {hasTaxReturn && (
                      <span className="block font-mono text-[11px] text-accent-ink">
                        {t("Tax return reason code: 102 (Cancellation of goods/services)")}
                      </span>
                    )}
                  </span>
                </label>}

                {allowPartial && <label className="flex cursor-pointer items-start gap-2.5 rounded-xl border border-line p-3 hover:border-accent-control/40">
                  <input
                    type="radio"
                    name="credit-rebill-mode"
                    value="partial"
                    className="mt-0.5 accent-focus"
                    checked={mode === "partial"}
                    onChange={() => setMode("partial")}
                  />
                  <span className="space-y-0.5">
                    <span className="block font-medium text-ink">{t("Partial Credit Note")}</span>
                    <span className="block text-xs text-muted-ink">
                      {t("Credits a specific amount (up to {left}) without reissuing the full invoice.", {
                        left: fmtMoney(left, moneyContext),
                      })}
                    </span>
                    {hasTaxReturn && (
                      <span className="block font-mono text-[11px] text-accent-ink">
                        {t("Tax return reason code: 104 (Partial waive off / adjustment)")}
                      </span>
                    )}
                  </span>
                </label>}
              </fieldset>

              {mode === "partial" && (
                <div className="space-y-1.5">
                  <label className="block space-y-1 text-xs text-secondary-ink">
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
                    <p className="text-xs text-warning-ink">
                      {t("At most {amount} can still be credited on this invoice.", {
                        amount: fmtMoney(left, moneyContext),
                      })}
                    </p>
                  )}
                  {propTax > 0 && val <= left && (
                    <p className="text-xs text-muted-ink">
                      {t("Tax in this credit note: {amount}", {
                        amount: fmtMoney(propTax, moneyContext),
                      })}
                    </p>
                  )}
                </div>
              )}

              <label className="block space-y-1 text-xs text-secondary-ink">
                <span>{t("Reason")}</span>
                <input
                  data-testid="credit-rebill-reason"
                  aria-label={t("Credit & Rebill reason")}
                  className={inputCls}
                  maxLength={500}
                  placeholder={t("Reason (required - goes on the record)")}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </label>

              {needsPinInput && (
                <label className="block space-y-1 text-xs text-secondary-ink">
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

              <p className="rounded-lg border border-warning-line bg-warning-subtle px-3 py-2 text-xs text-warning-ink">
                {mode === "partial"
                ? t("Second manager/Finance approval required to finalize the partial credit note.")
                : folio.sales_invoice ? t("Second manager/Finance approval required to finalize credit note & release replacement invoice.") : null}
              </p>

              {error && (
                <p role="alert" className="rounded-lg bg-danger-subtle px-3 py-2 text-xs text-danger-ink">
                  {error}
                </p>
              )}
            </div>

            <footer className="flex justify-end gap-2 border-t border-line-subtle px-5 py-3">
              <Button className="min-h-11" type="button" variant="outline" disabled={busy} onClick={onClose}>
                {t("Close")}
              </Button>
              <Button
                type="button"
                className="min-h-11"
                data-testid="submit-credit-rebill"
                disabled={busy || !ready}
                onClick={() => void submit()}
              >
                {busy
                  ? t("Submitting…")
                  : mode === "full"
                    ? (folio.sales_invoice ? t("Draft Credit & Rebill") : t("Confirm cancel"))
                    : t("Draft Partial Credit Note")}
              </Button>
            </footer>
          </>
        )}
      </div>
    </div>
  )
}
