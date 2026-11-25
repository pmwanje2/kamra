/*  The reservation's Changes Log (OPERA Actions > Changes Log; SAP's
    pricing change documents): every save Frappe recorded on the stay - the
    fields that moved and each night's rate - and every governed action on
    it - who, why, each night before and after, and who signed it off -
    newest first, from kamra.api.reservation_changes. Read only; loaded when
    the desk opens it. */

import { useState } from "react"
import { call } from "../lib/api"
import { serverError } from "../lib/resource"
import { useT } from "../lib/i18n"
import { Button } from "./ui/button"

export interface ChangeEntry {
  kind: "change" | "action"
  at: string
  by: string | null
  by_name: string | null
  approver: string | null
  approver_name: string | null
  action_type: string | null
  /** an action's name (Discount, Agreed rate, Room move, ...) */
  title: string | null
  rationale: string | null
  fields: { field: string; label: string; from: string; to: string }[]
  nights: { date: string; from: number | null; to: number | null }[]
}

export default function ChangesLog({ reservation, money }: {
  reservation: string
  money: (n: number) => string
}) {
  const { t } = useT()
  const [open, setOpen] = useState(false)
  const [entries, setEntries] = useState<ChangeEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  function toggle() {
    if (open) {
      setOpen(false)
      return
    }
    setOpen(true)
    setEntries(null)
    setError(null)
    call<{ entries: ChangeEntry[] }>("kamra.api.reservation_changes", { reservation })
      .then((r) => setEntries(r.entries ?? []))
      .catch((e) => setError(serverError(e)))
  }

  const amount = (n: number | null) => (n === null || n === undefined ? "-" : money(n))

  return (
    <div className="mt-3">
      <Button variant="outline" aria-expanded={open} onClick={toggle}>{t("Changes log")}</Button>
      {open && (
        <div className="mt-2 rounded-lg border border-line p-3 text-sm">
          {error && <p className="text-xs text-danger-text">{t(error)}</p>}
          {!entries && !error && <p className="text-xs text-muted-ink">{t("Loading…")}</p>}
          {entries?.length === 0 && <p className="text-xs text-muted-ink">{t("No changes recorded yet.")}</p>}
          <ol className="space-y-2">
            {(entries ?? []).map((e, i) => (
              <li key={`${e.at}-${i}`} className="min-w-0 border-b border-line-subtle pb-2 last:border-0 last:pb-0">
                <div className="flex flex-wrap justify-between gap-x-2 text-xs text-muted-ink">
                  <span className="font-medium text-secondary-ink">{e.title ? t(e.title) : t("Saved")}</span>
                  <span className="tabular-nums">{e.at.slice(0, 16)} · {e.by_name ?? e.by ?? "-"}</span>
                </div>
                {e.rationale && (
                  <p className="mt-0.5 break-words text-secondary-ink [overflow-wrap:anywhere]">{e.rationale}</p>
                )}
                {e.fields.map((f) => (
                  <p key={f.field} className="break-words text-xs text-secondary-ink [overflow-wrap:anywhere]">
                    {`${t(f.label)}: ${f.from || "-"} → ${f.to || "-"}`}
                  </p>
                ))}
                {e.nights.map((n) => (
                  <p key={n.date} className="text-xs tabular-nums text-secondary-ink">
                    {`${n.date}: ${amount(n.from)} → ${amount(n.to)}`}
                  </p>
                ))}
                {e.approver && (
                  <p className="text-xs text-success-ink">
                    {t("Signed off by {name}", { name: e.approver_name ?? e.approver })}
                  </p>
                )}
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  )
}
