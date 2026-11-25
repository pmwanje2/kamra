import { t } from "../lib/i18n"

/** The fiscal document behind a settled folio under the ERP accounting
 *  bridge (kamra.fiscal.fiscal_status): the ERP Sales Invoice and, where a
 *  fiscal service issues them, its fiscal document number, verification
 *  code and QR. Anything but a fiscalised (or non-fiscal-service) invoice is
 *  said plainly not to be a tax invoice. */
export type Fiscal = {
  scheme?: string | null
  state: "fiscalised" | "pending" | "not_efris" | "unverified"
  sales_invoice: string | null
  fdn: string | null
  verification_code: string | null
  qr: string | null
  issued: string | null
  sandbox: boolean
  efris_status: string | null
  tax_template?: string | null
  charges_tax?: 0 | 1 | boolean
  efris_enrolled?: 0 | 1 | boolean
  outstanding_amount?: number
  credit_notes?: { name: string; grand_total?: number; efris_irn?: string | null }[]
  replaced_by?: string | null
}

export function FiscalDocument({ fiscal, internalNumber }: { fiscal: Fiscal; internalNumber?: string | null }) {
  const effectiveState =
    fiscal.state === "not_efris" && fiscal.efris_enrolled ? "pending" : fiscal.state
  const warning =
    effectiveState === "pending"
      ? t("Fiscal document pending")
      : effectiveState === "unverified"
        ? t("Fiscal status unavailable")
        : fiscal.sandbox
          ? t("Test fiscal number - not a valid tax invoice")
          : null
  return (
    <div className="mt-2 space-y-1 text-end text-xs text-muted-ink">
      {warning && (
        <div className="rounded border border-warning-line-strong bg-warning-subtle px-2 py-1 text-warning-ink">
          <p className="font-semibold">{warning}</p>
          {effectiveState !== "fiscalised" && (
            <p>{t("This is not a tax invoice yet. It becomes one when the fiscal number is issued.")}</p>
          )}
          {effectiveState === "pending" && fiscal.efris_status && (
            <p>{t("Status: {status}", { status: fiscal.efris_status })}</p>
          )}
        </div>
      )}
      {effectiveState === "fiscalised" && (
        <div className="flex items-start justify-end gap-3">
          <div className="space-y-0.5">
            <p>
              {t("Fiscal Document Number (FDN)")}: <span className="font-mono font-semibold text-ink">{fiscal.fdn}</span>
            </p>
            <p>
              {t("Verification code")}: <span className="font-mono text-ink">{fiscal.verification_code}</span>
            </p>
            {fiscal.issued && <p>{t("Issued {when}", { when: fiscal.issued })}</p>}
          </div>
          {fiscal.qr && <img src={fiscal.qr} alt={t("Fiscal QR code")} className="size-24 shrink-0" />}
        </div>
      )}
      {internalNumber && <p>{t("Internal folio no. {number}", { number: internalNumber })}</p>}
    </div>
  )
}
