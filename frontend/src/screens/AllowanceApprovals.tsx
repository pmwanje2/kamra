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
  Pending:
    "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-800/60 dark:bg-amber-950/50 dark:text-amber-200",
  Approved:
    "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-800/60 dark:bg-emerald-950/50 dark:text-emerald-200",
  Rejected:
    "border-rose-200 bg-rose-50 text-rose-800 dark:border-rose-800/60 dark:bg-rose-950/50 dark:text-rose-200",
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
          <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-zinc-500 dark:text-zinc-400">
            {t("Billing")}
          </p>
          <h1 className="text-2xl font-semibold text-zinc-900 dark:text-zinc-100">
            {t("Allowances to approve")}
          </h1>
          <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
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

      <div className="flex items-start gap-2.5 rounded-xl border border-brand-200/80 border-s-4 border-s-brand-600 bg-brand-50/60 p-3.5 text-sm text-zinc-700 dark:border-brand-800/60 dark:border-s-brand-400 dark:bg-brand-950/30 dark:text-zinc-200">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-brand-700 dark:text-brand-300" aria-hidden="true" />
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
                ? "border-brand-600 bg-brand-50 text-brand-800 dark:border-brand-400 dark:bg-brand-950/50 dark:text-brand-200"
                : "border-zinc-200 bg-white text-zinc-600 hover:bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300 dark:hover:bg-zinc-800"
            }`}
          >
            {label[k]}
          </button>
        ))}
      </div>

      {error && (
        <p className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:border-rose-800/60 dark:bg-rose-950/40 dark:text-rose-300">
          {translate(error)}
        </p>
      )}

      {!rows && !error && (
        <p className="py-6 text-center text-sm text-zinc-500 dark:text-zinc-400">{t("Loading…")}</p>
      )}

      {rows && rows.length === 0 && (
        <p className="rounded-xl border border-dashed border-zinc-200 bg-zinc-50/50 py-8 text-center text-sm text-zinc-500 dark:border-zinc-800 dark:bg-zinc-900/40 dark:text-zinc-400">
          {t("Nothing here.")}
        </p>
      )}

      {rows && rows.length > 0 && (
        <div className="space-y-3">
          {rows.map((r) => (
            <article
              key={r.name}
              className="rounded-xl border border-zinc-200 bg-white p-4 shadow-xs dark:border-zinc-800 dark:bg-zinc-900"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex min-w-0 flex-wrap items-baseline gap-x-2.5 gap-y-1">
                  <span className="text-base font-semibold tabular-nums text-zinc-900 dark:text-zinc-100">
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
                  className="max-w-full truncate text-xs font-medium text-brand-700 hover:underline dark:text-brand-300"
                >
                  {[r.room, r.bill_to_name || r.guest_name, r.folio].filter(Boolean).join(" · ")}
                </Link>
              </div>

              <p className="mt-1.5 break-words text-sm text-zinc-800 dark:text-zinc-200">{r.reason}</p>

              <p className="mt-1.5 text-xs text-zinc-500 dark:text-zinc-400">
                {t("Asked by {who} on {when}", {
                  who: r.requested_by,
                  when: String(r.requested_on || "").slice(0, 16),
                })}
                {r.channel && r.channel !== "Desk" ? ` · ${r.channel}` : ""}
              </p>

              {r.status !== "Pending" && (
                <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 border-t border-zinc-100 pt-2 text-xs text-zinc-600 dark:border-zinc-800 dark:text-zinc-300">
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
                      <span className="font-mono text-zinc-700 dark:text-zinc-200">{r.serial}</span>
                      <span aria-hidden="true">·</span>
                      <Link
                        to={`/allowance-voucher/${encodeURIComponent(r.serial)}`}
                        className="font-medium text-brand-700 hover:underline dark:text-brand-300"
                      >
                        {t("Print voucher")}
                      </Link>
                    </>
                  )}
                </div>
              )}

              {r.status === "Pending" && (
                <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-zinc-100 pt-2.5 dark:border-zinc-800">
                  <span className="text-xs text-zinc-500 dark:text-zinc-400">
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
