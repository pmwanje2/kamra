/*  Correct an issued invoice with a credit note. A hotel that invoices here
    issues the credit note itself, with its share of the tax. Where the
    accounting system issued the invoice (the folio carries its
    sales_invoice), the correction goes to that system's credit note and
    re-bill (issue_credit_note routes it): this screen never reopens or
    credits that bill on its own. */

import { useState } from "react"
import { call } from "../lib/api"
import { useCashierAuth } from "../lib/cashierAuth"
import { fmtMoney } from "../lib/money"
import { serverError } from "../lib/resource"
import { useT } from "../lib/i18n"
import { Button } from "./ui/button"

export interface InvoicedFolio {
  name: string
  invoice_number: string | null
  /** the accounting system's invoice for this folio, where it issued one */
  sales_invoice?: string | null
  grand_total: number
  tax_total: number
}

const inputCls =
  "w-full rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm focus:outline-2 focus:outline-offset-1 focus:outline-brand-600"

export default function InvoiceCorrectionDialog(props: {
  folio: InvoicedFolio
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useT()
  const { ensureUnlocked, status } = useCashierAuth()
  const { folio } = props
  const document = folio.sales_invoice || null
  const invoice = folio.invoice_number ?? ""
  const [amount, setAmount] = useState("")
  const [reason, setReason] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const value = Number(amount)
  const ready = value > 0 && !!reason.trim()
  // the tax inside the credit, in the invoice's own proportion (as the server)
  const tax = value > 0 && folio.grand_total
    ? Math.round((value * folio.tax_total * 100) / folio.grand_total) / 100
    : 0

  async function issue() {
    setBusy(true)
    setError(null)
    try {
      if (status?.required) await ensureUnlocked()
      await call("kamra.ledger.issue_credit_note", {
        folio: folio.name, amount: value, reason: reason.trim(),
      })
      props.onDone()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[60] flex items-end justify-center sm:items-center print:hidden"
      role="dialog" aria-modal="true" aria-label={t("Credit note")}>
      <div className="absolute inset-0 bg-black/40" onClick={props.onClose} aria-hidden />
      <div className="relative w-full max-w-md rounded-t-2xl bg-white shadow-2xl sm:rounded-2xl">
        <header className="border-b border-zinc-200 px-5 py-4">
          <h2 className="text-lg font-semibold text-zinc-900">{t("Credit note")}</h2>
          <p className="text-sm text-zinc-500">{invoice}</p>
        </header>
        <div className="space-y-3 px-5 py-4 text-sm">
          <p className="text-zinc-700">
            {document
              ? t("Invoice {invoice} was issued by the accounting system as {document}. It is corrected there with a credit note and a new invoice; this bill is not reopened or credited here.", { invoice, document })
              : t("A credit note is issued here against invoice {invoice}, with its share of the tax.", { invoice })}
          </p>
          <input className={inputCls} type="number" min="0" step="any" inputMode="decimal"
            aria-label={t("Amount to credit, tax included")}
            placeholder={t("Amount to credit, tax included")}
            value={amount} onChange={(e) => setAmount(e.target.value)} />
          <input className={inputCls}
            aria-label={t("Reason (required - goes on the record)")}
            placeholder={t("Reason (required - goes on the record)")}
            value={reason} onChange={(e) => setReason(e.target.value)} />
          {!document && tax > 0 && (
            <p className="text-zinc-500">{t("Tax in this credit note: {amount}", { amount: fmtMoney(tax) })}</p>
          )}
          {error && <p className="rounded-lg bg-rose-50 px-3 py-2 text-rose-700">{error}</p>}
        </div>
        <footer className="flex justify-end gap-2 border-t border-zinc-100 px-5 py-3">
          <Button variant="outline" disabled={busy} onClick={props.onClose}>{t("Close")}</Button>
          <Button disabled={busy || !ready} onClick={() => void issue()}>
            {document ? t("Send for credit note and re-bill") : t("Issue credit note")}
          </Button>
        </footer>
      </div>
    </div>
  )
}
