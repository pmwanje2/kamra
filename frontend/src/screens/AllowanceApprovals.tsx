/*  Allowances to approve: lightweight read and audit log of bill allowance
    requests (waiting, approved, and rejected). Back-office decisions run in
    the central Approvals Inbox under four-eyes workflow governance; floor
    overrides within the company floor cap require a second manager's PIN. */

import { useCallback, useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { ExternalLink, ShieldCheck } from "lucide-react"
import { call, getCurrentProperty } from "../lib/api"
import { serverError } from "../lib/resource"
import { fmtMoney, useLocale } from "../lib/money"
import { useT, t as translate } from "../lib/i18n"
import { Button } from "../components/ui/button"

interface AllowanceRow {
  name: string
  serial: string | null
  folio: string
  amount: number
  reason: string
  status: "Pending" | "Approved" | "Rejected"
  requested_by: string
  requested_on: string
  channel: string | null
  decided_by: string | null
  decided_by_name?: string | null
  decided_by_title?: string | null
  decided_on: string | null
  decision_note: string | null
  guest_name: string | null
  room: string | null
  folio_status: string | null
  bill_to_name: string | null
}

const TABS = ["Pending", "Approved", "Rejected"] as const

const STATUS_BADGE: Record<(typeof TABS)[number], string> = {
  Pending: "border-warning-line bg-warning-subtle text-warning-ink",
  Approved: "border-success-line bg-success-subtle text-success-ink",
  Rejected: "border-danger-line bg-danger-subtle text-danger-ink",
}

function approvalsInboxUrl(): string {
  const custom =
    typeof window !== "undefined"
      ? (window as Window & { kamra_approvals_inbox_url?: string }).kamra_approvals_inbox_url
      : undefined
  return custom && custom.startsWith("/") ? custom : "/app/workflow-action"
}

function openApprovalsInbox() {
  if (typeof window !== "undefined") {
    window.location.assign(approvalsInboxUrl())
  }
}

export default function AllowanceApprovals() {
  const { t } = useT()
  useLocale()
  const property = getCurrentProperty()
  const [tab, setTab] = useState<(typeof TABS)[number]>("Pending")
  const [rows, setRows] = useState<AllowanceRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const label: Record<(typeof TABS)[number], string> = {
    Pending: t("Waiting"),
    Approved: t("Approved"),
    Rejected: t("Rejected"),
  }

  const load = useCallback(() => {
    let active = true
    setRows(null)
    setError(null)
    call<{ requests: AllowanceRow[]; can_decide: boolean }>("kamra.api.allowance_requests", {
      property,
      status: tab,
    })
      .then((r) => {
        if (active) setRows(r.requests ?? [])
      })
      .catch((e) => {
        if (active) setError(serverError(e))
      })
    return () => {
      active = false
    }
  }, [property, tab])

  useEffect(load, [load])

  return (
    <div className="w-full max-w-3xl space-y-4 text-start">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-ink">
            {t("Billing")}
          </p>
          <h1 className="text-2xl font-semibold text-ink">
            {t("Allowances to approve")}
          </h1>
          <p className="mt-1 text-sm text-secondary-ink">
            {t("Nothing is taken off a bill until a manager approves it. A rejection is kept with who and why.")}
          </p>
        </div>
        <Button
          type="button"
          onClick={openApprovalsInbox}
          className="min-h-[44px] w-full shrink-0 justify-center gap-2 sm:w-auto"
        >
          <ExternalLink className="h-4 w-4 shrink-0" aria-hidden="true" />
          <span>{t("Open in Approvals Inbox")}</span>
        </Button>
      </div>

      <div className="flex items-start gap-2.5 rounded-xl border border-accent-line border-s-4 border-s-accent-control bg-accent-subtle p-3.5 text-sm text-secondary-ink">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-accent-ink" aria-hidden="true" />
        <p className="min-w-0 leading-relaxed">
          {t("Back-office allowance decisions are handled in Approvals Inbox under four-eyes approval controls. Floor overrides within the property limit require a second authorized manager's PIN.")}
        </p>
      </div>

      <div className="flex flex-wrap gap-2" role="tablist" aria-label={t("Allowances to approve")}>
        {TABS.map((k) => (
          <button
            key={k}
            type="button"
            role="tab"
            aria-selected={tab === k}
            onClick={() => setTab(k)}
            className={`min-h-[44px] rounded-full border px-4 py-1.5 text-xs font-medium transition-colors sm:min-h-[36px] ${
              tab === k
                ? "border-accent-control bg-accent-subtle text-accent-ink"
                : "border-line bg-surface text-secondary-ink hover:bg-subtle"
            }`}
          >
            {label[k]}
          </button>
        ))}
      </div>

      {error && (
        <p className="rounded-lg border border-danger-line bg-danger-subtle px-3 py-2 text-sm text-danger-ink">
          {translate(error)}
        </p>
      )}

      {!rows && !error && (
        <p className="py-6 text-center text-sm text-muted-ink">{t("Loading…")}</p>
      )}

      {rows && rows.length === 0 && (
        <p className="rounded-xl border border-dashed border-line bg-surface py-8 text-center text-sm text-muted-ink">
          {t("Nothing here.")}
        </p>
      )}

      {rows && rows.length > 0 && (
        <div className="space-y-3">
          {rows.map((r) => (
            <article
              key={r.name}
              className="rounded-xl border border-line bg-surface p-4 shadow-xs"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex min-w-0 flex-wrap items-baseline gap-x-2.5 gap-y-1">
                  <span className="text-base font-semibold tabular-nums text-ink">
                    {fmtMoney(r.amount)}
                  </span>
                  <span
                    className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium ${STATUS_BADGE[r.status]}`}
                  >
                    {label[r.status]}
                  </span>
                </div>
                <Link
                  to={`/billing/${encodeURIComponent(r.folio)}`}
                  className="max-w-full truncate text-xs font-medium text-accent-ink hover:underline"
                >
                  {[r.room, r.bill_to_name || r.guest_name, r.folio].filter(Boolean).join(" · ")}
                </Link>
              </div>

              <p className="mt-1.5 break-words text-sm text-ink">{r.reason}</p>

              <p className="mt-1.5 text-xs text-muted-ink">
                {t("Asked by {who} on {when}", {
                  who: r.requested_by,
                  when: String(r.requested_on || "").slice(0, 16),
                })}
                {r.channel && r.channel !== "Desk" ? ` · ${r.channel}` : ""}
              </p>

              {r.status !== "Pending" && (
                <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 border-t border-line-subtle pt-2 text-xs text-secondary-ink">
                  <span>
                    {r.status === "Approved"
                      ? r.decided_by
                        ? t("Approved by {who}", {
                            who: [
                              r.decided_by_name || r.decided_by,
                              r.decided_by_title && translate(r.decided_by_title),
                            ]
                              .filter(Boolean)
                              .join(", "),
                          })
                        : t("Posted without approval")
                      : t("Rejected by {who}: {why}", {
                          who: r.decided_by_name || r.decided_by || "",
                          why: r.decision_note || "",
                        })}
                  </span>
                  {r.serial && (
                    <>
                      <span aria-hidden="true">·</span>
                      <span className="font-mono text-secondary-ink">{r.serial}</span>
                      <span aria-hidden="true">·</span>
                      <Link
                        to={`/allowance-voucher/${encodeURIComponent(r.serial)}`}
                        className="font-medium text-accent-ink hover:underline"
                      >
                        {t("Print voucher")}
                      </Link>
                    </>
                  )}
                </div>
              )}

              {r.status === "Pending" && (
                <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-line-subtle pt-2.5">
                  <span className="text-xs text-muted-ink">
                    {r.folio_status === "Closed"
                      ? t("This bill is settled now. Reject it and pass the allowance on an open bill.")
                      : t("Pending workflow approval")}
                  </span>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={openApprovalsInbox}
                    className="min-h-[44px] w-full justify-center gap-1.5 sm:min-h-[34px] sm:w-auto"
                  >
                    <ExternalLink className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                    <span>{t("Open in Approvals Inbox")}</span>
                  </Button>
                </div>
              )}
            </article>
          ))}
        </div>
      )}
    </div>
  )
}
