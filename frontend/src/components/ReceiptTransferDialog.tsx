import { useState } from "react"
import { call, newIdempotencyKey } from "../lib/api"
import { useCashierAuth } from "../lib/cashierAuth"
import { folioLabel, windowBadge, type SiblingFolio } from "../lib/folioLabel"
import { useT } from "../lib/i18n"
import { fmtMoney, type MoneyContext, moneyDigits, moneyStep, roundMoney } from "../lib/money"
import { serverError } from "../lib/resource"
import { Button } from "./ui/button"

const inputCls =
  "min-h-11 w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-focus"

export default function ReceiptTransferDialog({
  folio,
  currency,
  money,
  payment,
  maxAmount,
  siblings,
  targets,
  onClose,
  onDone,
}: {
  folio: string
  currency: string
  money?: MoneyContext
  payment: {
    name: string
    mode: string
    amount: number
    payment_kind?: string | null
    reference?: string | null
  }
  maxAmount: number
  siblings: SiblingFolio[]
  targets: SiblingFolio[]
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useT()
  const moneyContext = money ?? { currency }
  const { ensureUnlocked, status: pinStatus } = useCashierAuth()
  const digits = moneyDigits(currency, money?.decimals ?? undefined)
  const step = moneyStep(currency, money?.decimals ?? undefined)
  const cap = roundMoney(maxAmount, digits)

  const [target, setTarget] = useState(targets[0]?.name ?? "")
  const [amount, setAmount] = useState(cap.toFixed(digits))
  const [reason, setReason] = useState("")
  const [pin, setPin] = useState("")
  const [idemKey] = useState(newIdempotencyKey)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const val = Number(amount)
  const validDecimals =
    digits === 0
      ? /^\d+$/.test(amount.trim())
      : new RegExp(`^\\d+(?:\\.\\d{1,${digits}})?$`).test(amount.trim())
  const amountValid = Number.isFinite(val) && val > 0 && val <= cap && validDecimals
  const needsPinInput = Boolean(pinStatus?.required && !pinStatus?.unlocked)
  const ready = Boolean(target) && amountValid && Boolean(reason.trim()) && (!needsPinInput || Boolean(pin.trim()))

  async function submit() {
    if (!ready || busy) return
    setBusy(true)
    setError(null)
    try {
      if (pinStatus?.required && !pinStatus?.unlocked && !pin.trim()) {
        await ensureUnlocked()
      }
      await call("kamra.payment_transfers.transfer_folio_payment", {
        from_folio: folio,
        payment_row: payment.name,
        to_folio: target,
        amount: val,
        reason: reason.trim(),
        ...(pin.trim() ? { pin: pin.trim() } : {}),
        idempotency_key: idemKey,
      })
      onDone()
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
      aria-label={t("Transfer receipt")}
    >
      <div className="absolute inset-0 bg-black/40" onClick={onClose} aria-hidden />
      <div className="relative max-h-dvh w-full max-w-md overflow-y-auto rounded-t-2xl bg-surface shadow-2xl sm:rounded-2xl">
        <header className="border-b border-line px-5 py-4">
          <h2 className="text-lg font-semibold text-ink">{t("Transfer receipt")}</h2>
          <p className="text-xs text-muted-ink">
            {t(payment.mode)} · <span dir="ltr" className="inline-block tabular-nums">{fmtMoney(cap, moneyContext)}</span> {t("available to transfer")}
          </p>
        </header>

        <div className="space-y-3 px-5 py-4 text-sm">
          <label className="block space-y-1 text-xs text-secondary-ink">
            <span>{t("Destination window")}</span>
            <select
              aria-label={t("Destination window")}
              className={inputCls}
              value={target}
              onChange={(e) => setTarget(e.target.value)}
            >
              {targets.map((s) => (
                <option key={s.name} value={s.name}>
                  {windowBadge(s)} · {folioLabel(siblings, s, t)} ({fmtMoney(s.balance, moneyContext)})
                </option>
              ))}
            </select>
          </label>

          <label className="block space-y-1 text-xs text-secondary-ink">
            <span>{t("Transfer amount")}</span>
            <input
              aria-label={t("Transfer amount")}
              className={inputCls}
              type="number"
              min="0"
              max={cap}
              step={step}
              inputMode={digits === 0 ? "numeric" : "decimal"}
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
            />
          </label>

          {!amountValid && amount.trim() !== "" && (
            <p className="text-xs text-warning-ink">
              {digits === 0
                ? t("Enter a whole-unit amount up to {max}.", { max: fmtMoney(cap, moneyContext) })
                : t("Enter an amount up to {max} with at most {decimals} decimals.", {
                    max: fmtMoney(cap, moneyContext),
                    decimals: digits,
                  })}
            </p>
          )}

          <label className="block space-y-1 text-xs text-secondary-ink">
            <span>{t("Reason")}</span>
            <input
              aria-label={t("Transfer reason")}
              className={inputCls}
              maxLength={300}
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
          <Button className="min-h-11 sm:min-h-0"
            type="button"
            data-testid="confirm-receipt-transfer"
            disabled={busy || !ready}
            onClick={() => void submit()}
          >
            {busy ? t("Transferring…") : t("Transfer receipt")}
          </Button>
        </footer>
      </div>
    </div>
  )
}
