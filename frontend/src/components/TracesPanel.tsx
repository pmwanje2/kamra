import { useCallback, useEffect, useMemo, useState } from "react"
import { CalendarClock, Check, Clock, ListChecks, RotateCcw } from "lucide-react"
import { call } from "../lib/api"
import { serverError } from "../lib/resource"
import { Button } from "./ui/button"
import { cn } from "../lib/utils"
import { useT } from "../lib/i18n"

export const TRACE_DEPARTMENTS = [
  "Housekeeping",
  "Front Desk",
  "F&B",
  "Concierge",
  "Transport",
  "Maintenance",
  "Security",
] as const

export interface StayTraceItem {
  name: string
  reservation?: string
  department: string
  instruction: string
  status: "Open" | "Done"
  trace_date?: string | null
  trace_time?: string | null
  trace_status?: "Scheduled" | "Open" | "Overdue" | "Done"
  activated_on?: string | null
  done_by?: string | null
  done_on?: string | null
  resolved_by?: string | null
  resolved_at?: string | null
  todo_ref?: string | null
  is_overdue?: number
}

type TraceTab = "due" | "scheduled" | "done" | "all"

function todayIso(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`
}

function effectiveStatus(row: StayTraceItem, bd: string): "Scheduled" | "Open" | "Overdue" | "Done" {
  if (row.status === "Done" || row.trace_status === "Done") return "Done"
  if (row.trace_status === "Overdue" || row.trace_status === "Scheduled") return row.trace_status
  if (row.trace_date && bd) {
    if (row.trace_date < bd) return "Overdue"
    if (row.trace_date > bd) return "Scheduled"
  }
  return "Open"
}

function formatTraceTime(raw: string): string {
  const parts = String(raw).trim().split(":")
  if (parts.length >= 2) {
    return `${parts[0].padStart(2, "0")}:${parts[1].padStart(2, "0")}`
  }
  return String(raw).slice(0, 5)
}

const STATUS_BADGE: Record<"Scheduled" | "Open" | "Overdue" | "Done", string> = {
  Overdue:
    "border-danger-line-strong bg-danger-subtle text-danger-ink dark:border-rose-500/40 dark:bg-rose-950/40",
  Open:
    "border-warning-line-strong bg-warning-subtle text-warning-ink dark:border-amber-500/40 dark:bg-amber-950/30",
  Scheduled:
    "border-info-line-strong bg-info-subtle text-info-ink dark:border-sky-500/40 dark:bg-sky-950/30",
  Done:
    "border-success-line-strong bg-success-subtle text-success-ink dark:border-emerald-500/40 dark:bg-emerald-950/30",
}

export default function TracesPanel({
  reservation,
  property,
  businessDate,
  checkInDate,
  canAdd = true,
  initialTraces,
}: {
  reservation?: string
  property?: string
  businessDate?: string
  checkInDate?: string
  canAdd?: boolean
  initialTraces?: StayTraceItem[]
}) {
  const { t } = useT()
  const bd = businessDate || todayIso()
  const [rows, setRows] = useState<StayTraceItem[] | null>(initialTraces ?? null)
  const [tab, setTab] = useState<TraceTab>("due")
  const [deptFilter, setDeptFilter] = useState("All")
  const [dept, setDept] = useState<string>(TRACE_DEPARTMENTS[0])
  const [traceDate, setTraceDate] = useState<string>(businessDate || checkInDate || todayIso())
  const [traceTime, setTraceTime] = useState<string>("")
  const [text, setText] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const data = await call<StayTraceItem[]>("kamra.traces.list_traces", {
        reservation: reservation || undefined,
        property: property || undefined,
        business_date: businessDate || undefined,
      })
      setRows(Array.isArray(data) ? data : [])
      setError(null)
    } catch (e) {
      setError(serverError(e))
    }
  }, [reservation, property, businessDate])

  useEffect(() => {
    if (initialTraces !== undefined) {
      setRows(initialTraces)
      return
    }
    void load()
  }, [initialTraces, load])

  const counts = useMemo(() => {
    const list = rows ?? []
    let due = 0
    let overdue = 0
    let scheduled = 0
    let done = 0
    for (const r of list) {
      const s = effectiveStatus(r, bd)
      if (s === "Overdue") {
        due += 1
        overdue += 1
      } else if (s === "Open") {
        due += 1
      } else if (s === "Scheduled") {
        scheduled += 1
      } else {
        done += 1
      }
    }
    return { due, overdue, scheduled, done, all: list.length }
  }, [rows, bd])

  const shown = useMemo(() => {
    return (rows ?? []).filter((r) => {
      if (deptFilter !== "All" && r.department !== deptFilter) return false
      const s = effectiveStatus(r, bd)
      if (tab === "due") return s === "Open" || s === "Overdue"
      if (tab === "scheduled") return s === "Scheduled"
      if (tab === "done") return s === "Done"
      return true
    })
  }, [rows, deptFilter, tab, bd])

  async function addNewTrace() {
    if (!reservation || !text.trim()) return
    setBusy(true)
    setError(null)
    try {
      const res = await call<{ traces?: StayTraceItem[] }>("kamra.traces.add_trace", {
        reservation,
        department: dept,
        instruction: text.trim(),
        trace_date: traceDate || bd,
        trace_time: traceTime || undefined,
      })
      if (Array.isArray(res?.traces)) setRows(res.traces)
      else await load()
      setText("")
      setTraceTime("")
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  async function toggleStatus(row: StayTraceItem) {
    const targetRes = row.reservation || reservation
    if (!targetRes) return
    setBusy(true)
    setError(null)
    try {
      const next = row.status === "Done" ? "Open" : "Done"
      const res = await call<{ traces?: StayTraceItem[] }>("kamra.traces.resolve_trace", {
        reservation: targetRes,
        row: row.name,
        status: next,
      })
      if (Array.isArray(res?.traces)) setRows(res.traces)
      else await load()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  const tabs: { key: TraceTab; label: string; count: number }[] = [
    { key: "due", label: t("Due Today & Overdue"), count: counts.due },
    { key: "scheduled", label: t("Scheduled"), count: counts.scheduled },
    { key: "done", label: t("Done"), count: counts.done },
  ]

  return (
    <div className="rounded-xl border border-line bg-surface p-4 text-start">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-semibold text-secondary-ink">
          <ListChecks className="size-4 text-muted-ink" aria-hidden />
          {t("Traces & Instructions")}
          {counts.overdue > 0 && (
            <span className="rounded-full bg-danger-subtle px-2 py-0.5 text-xs font-semibold text-danger-ink">
              {counts.overdue} {t("Overdue")}
            </span>
          )}
        </div>
        <select
          aria-label={t("Filter by department")}
          value={deptFilter}
          onChange={(e) => setDeptFilter(e.target.value)}
          className="min-h-10 rounded-lg border border-control bg-surface px-2 py-1 text-xs text-ink sm:min-h-8"
        >
          <option value="All">{t("All departments")}</option>
          {TRACE_DEPARTMENTS.map((d) => (
            <option key={d} value={d}>
              {t(d)}
            </option>
          ))}
        </select>
      </div>

      <div className="mb-3 flex flex-wrap gap-1.5" role="tablist" aria-label={t("Trace status filter")}>
        {tabs.map((tb) => (
          <button
            key={tb.key}
            type="button"
            role="tab"
            aria-selected={tab === tb.key}
            onClick={() => setTab(tb.key)}
            className={cn(
              "inline-flex min-h-11 items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium sm:min-h-8",
              tab === tb.key
                ? "border-accent-control bg-accent-subtle text-accent-ink"
                : "border-line bg-surface text-secondary-ink hover:border-control",
            )}
          >
            <span>{tb.label}</span>
            <span className="tabular-nums opacity-75">{tb.count}</span>
          </button>
        ))}
      </div>

      {rows && shown.length === 0 && (
        <p className="py-2 text-sm text-muted-ink">{t("No traces in this view.")}</p>
      )}
      {rows && shown.length > 0 && (
        <ul className="divide-y divide-line-subtle">
          {shown.map((r) => {
            const st = effectiveStatus(r, bd)
            const who = r.resolved_by || r.done_by
            const when = r.resolved_at || r.done_on
            return (
              <li key={r.name} className="flex flex-wrap items-start gap-2.5 py-2.5">
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="rounded bg-subtle px-1.5 py-0.5 text-[11px] font-medium text-secondary-ink">
                      {t(r.department)}
                    </span>
                    <span
                      className={cn(
                        "rounded border px-1.5 py-0.5 text-[11px] font-semibold",
                        STATUS_BADGE[st],
                      )}
                    >
                      {t(st)}
                    </span>
                    {r.trace_date && (
                      <span className="inline-flex items-center gap-1 text-xs tabular-nums text-muted-ink">
                        <CalendarClock className="size-3" aria-hidden />
                        {r.trace_date}
                        {r.trace_time ? (
                          <span className="inline-flex items-center gap-0.5">
                            · <Clock className="size-3" aria-hidden />
                            {formatTraceTime(r.trace_time)}
                          </span>
                        ) : null}
                      </span>
                    )}
                  </div>
                  <p
                    className={cn(
                      "whitespace-pre-wrap break-words text-sm",
                      st === "Done"
                        ? "text-muted-ink line-through"
                        : "text-ink",
                    )}
                  >
                    {r.instruction}
                  </p>
                  {st === "Done" && who && (
                    <p className="text-xs text-muted-ink">
                      {t("Resolved by {who}", { who })}
                      {when ? ` · ${String(when).slice(0, 16)}` : ""}
                    </p>
                  )}
                </div>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void toggleStatus(r)}
                  className="inline-flex min-h-11 shrink-0 items-center gap-1 rounded-md border border-line px-3 py-1.5 text-xs font-medium text-secondary-ink hover:bg-canvas sm:min-h-8 sm:px-2.5"
                >
                  {r.status === "Done" ? (
                    <>
                      <RotateCcw className="size-3.5" aria-hidden />
                      {t("Reopen")}
                    </>
                  ) : (
                    <>
                      <Check className="size-3.5" aria-hidden />
                      {t("Done")}
                    </>
                  )}
                </button>
              </li>
            )
          })}
        </ul>
      )}

      {canAdd && reservation && (
        <div className="mt-3 space-y-2 border-t border-line-subtle pt-3">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <select
              aria-label={t("Department")}
              value={dept}
              onChange={(e) => setDept(e.target.value)}
              className="min-h-11 rounded-lg border border-control bg-surface px-2.5 py-1.5 text-sm text-ink sm:min-h-9"
            >
              {TRACE_DEPARTMENTS.map((d) => (
                <option key={d} value={d}>
                  {t(d)}
                </option>
              ))}
            </select>
            <input
              type="date"
              aria-label={t("Trace date")}
              value={traceDate}
              onChange={(e) => setTraceDate(e.target.value)}
              className="min-h-11 rounded-lg border border-control bg-surface px-2.5 py-1.5 text-sm text-ink sm:min-h-9"
            />
            <input
              type="time"
              aria-label={t("Trace time")}
              value={traceTime}
              onChange={(e) => setTraceTime(e.target.value)}
              className="min-h-11 rounded-lg border border-control bg-surface px-2.5 py-1.5 text-sm text-ink sm:min-h-9"
            />
          </div>
          <div className="flex flex-col gap-2 sm:flex-row">
            <input
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={t("e.g. Fruit basket before arrival")}
              aria-label={t("Trace instruction")}
              className="min-h-11 min-w-0 flex-1 rounded-lg border border-control bg-surface px-3 py-1.5 text-sm text-ink focus:outline-2 focus:outline-offset-1 focus:outline-focus sm:min-h-9"
            />
            <Button
              type="button"
              variant="outline"
              disabled={busy || !text.trim()}
              onClick={() => void addNewTrace()}
              className="min-h-11 sm:min-h-9"
            >
              {t("Add trace")}
            </Button>
          </div>
        </div>
      )}
      {error && (
        <p role="alert" className="mt-2 text-sm text-danger-ink dark:text-rose-300">
          {error}
        </p>
      )}
    </div>
  )
}
