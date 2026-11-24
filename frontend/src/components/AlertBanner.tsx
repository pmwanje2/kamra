import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { AlertTriangle, CheckCircle2, Info, Plus, ShieldAlert } from "lucide-react"
import { call, getCurrentProperty } from "../lib/api"
import { serverError } from "../lib/resource"
import { useT } from "../lib/i18n"
import { cn } from "../lib/utils"
import { Button } from "./ui/button"

export type AlertTrigger =
  | "Reservation"
  | "Check-In"
  | "Check-Out"
  | "In-House"
  | "Cashier"
  | "Night Audit"

export type AlertSeverity = "Info" | "Warning" | "Blocking"
export type AlertDisplayMode = "Every Access" | "Once Per Business Day" | "Until Resolved"

export interface GuestAlertItem {
  name: string
  property: string
  scope?: "Reservation" | "Guest"
  reservation?: string | null
  guest?: string | null
  trigger_area: AlertTrigger
  severity: AlertSeverity
  alert_code?: string | null
  message: string
  display_mode?: AlertDisplayMode
  status: "Active" | "Acknowledged" | "Resolved"
  acknowledged_by?: string | null
  acknowledged_on?: string | null
  acknowledged_reason?: string | null
  resolved_by?: string | null
  resolved_at?: string | null
  resolution_note?: string | null
  synthetic?: boolean
  can_override?: number
}

const TRIGGERS: AlertTrigger[] = [
  "Reservation",
  "Check-In",
  "Check-Out",
  "In-House",
  "Cashier",
  "Night Audit",
]
const SEVERITIES: AlertSeverity[] = ["Info", "Warning", "Blocking"]
const DISPLAY_MODES: AlertDisplayMode[] = [
  "Every Access",
  "Once Per Business Day",
  "Until Resolved",
]

const SEVERITY_ORDER: Record<AlertSeverity, number> = {
  Blocking: 0,
  Warning: 1,
  Info: 2,
}

const SURFACE: Record<AlertSeverity, string> = {
  Blocking:
    "border-rose-300 bg-rose-50 text-rose-950 dark:border-rose-500/40 dark:bg-rose-950/40 dark:text-rose-100",
  Warning:
    "border-amber-300 bg-amber-50 text-amber-950 dark:border-amber-500/40 dark:bg-amber-950/30 dark:text-amber-100",
  Info:
    "border-sky-300 bg-sky-50 text-sky-950 dark:border-sky-500/40 dark:bg-sky-950/30 dark:text-sky-100",
}

const BADGE_TONE: Record<AlertSeverity, string> = {
  Blocking: "bg-rose-600 text-white",
  Warning: "bg-amber-600 text-white",
  Info: "bg-sky-600 text-white",
}

export default function AlertBanner({
  trigger,
  property,
  reservation,
  guest,
  initialAlerts,
  canCreate = false,
  onAlertsChanged,
  onBlockingChange,
}: {
  trigger: AlertTrigger
  property?: string
  reservation?: string
  guest?: string
  initialAlerts?: GuestAlertItem[]
  canCreate?: boolean
  onAlertsChanged?: (alerts: GuestAlertItem[]) => void
  onBlockingChange?: (hasBlocking: boolean) => void
}) {
  const { t } = useT()
  const [alerts, setAlerts] = useState<GuestAlertItem[]>(initialAlerts ?? [])
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [resolvingId, setResolvingId] = useState<string | null>(null)
  const [resolveNote, setResolveNote] = useState("")
  const [creating, setCreating] = useState(false)
  const [draft, setDraft] = useState<{
    trigger_area: AlertTrigger
    severity: AlertSeverity
    display_mode: AlertDisplayMode
    alert_code: string
    message: string
  }>({
    trigger_area: trigger,
    severity: "Warning",
    display_mode: "Every Access",
    alert_code: "",
    message: "",
  })

  const prop = property || getCurrentProperty()
  const onAlertsChangedRef = useRef(onAlertsChanged)
  const onBlockingChangeRef = useRef(onBlockingChange)
  onAlertsChangedRef.current = onAlertsChanged
  onBlockingChangeRef.current = onBlockingChange

  const load = useCallback(async (notify = false) => {
    if (!prop || (!reservation && !guest)) return
    try {
      const rows = await call<GuestAlertItem[]>("kamra.traces.list_alerts", {
        property: prop,
        reservation: reservation || undefined,
        guest: guest || undefined,
        trigger_area: trigger,
      })
      const list = Array.isArray(rows) ? rows : []
      setAlerts(list)
      setError(null)
      if (notify) onAlertsChangedRef.current?.(list)
    } catch (e) {
      setError(serverError(e))
    }
  }, [prop, reservation, guest, trigger])

  useEffect(() => {
    if (initialAlerts !== undefined) {
      setAlerts(initialAlerts)
      return
    }
    void load(false)
  }, [initialAlerts, load])

  const visible = useMemo(
    () =>
      alerts
        .filter((a) => a.status !== "Resolved")
        .sort(
          (a, b) =>
            (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9),
        ),
    [alerts],
  )

  const hasBlocking = useMemo(
    () => visible.some((a) => a.severity === "Blocking" && a.status === "Active" && !a.synthetic),
    [visible],
  )

  useEffect(() => {
    onBlockingChangeRef.current?.(hasBlocking)
  }, [hasBlocking])

  async function handleAcknowledge(item: GuestAlertItem) {
    setBusyId(item.name)
    setError(null)
    try {
      await call("kamra.traces.acknowledge_alert", { alert: item.name })
      if (initialAlerts !== undefined) {
        const next = alerts.map((a) =>
          a.name === item.name ? { ...a, status: "Acknowledged" as const } : a,
        )
        setAlerts(next)
        onAlertsChangedRef.current?.(next)
      } else {
        await load(true)
      }
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusyId(null)
    }
  }

  async function handleResolve(item: GuestAlertItem) {
    const note = resolveNote.trim()
    if (item.severity === "Blocking" && !note) {
      setError(t("A resolution reason is required to clear a blocking alert."))
      return
    }
    setBusyId(item.name)
    setError(null)
    try {
      await call("kamra.traces.resolve_alert", {
        alert: item.name,
        resolution_note: note || undefined,
      })
      setResolvingId(null)
      setResolveNote("")
      if (initialAlerts !== undefined) {
        const next = alerts.filter((a) => a.name !== item.name)
        setAlerts(next)
        onAlertsChangedRef.current?.(next)
      } else {
        await load(true)
      }
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusyId(null)
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    if (!draft.message.trim()) return
    setBusyId("__create__")
    setError(null)
    try {
      await call("kamra.traces.create_alert", {
        property: prop,
        reservation: reservation || undefined,
        guest: guest || undefined,
        trigger_area: draft.trigger_area,
        severity: draft.severity,
        display_mode: draft.display_mode,
        alert_code: draft.alert_code.trim() || undefined,
        message: draft.message.trim(),
      })
      setDraft({
        trigger_area: trigger,
        severity: "Warning",
        display_mode: "Every Access",
        alert_code: "",
        message: "",
      })
      setCreating(false)
      await load(true)
    } catch (err) {
      setError(serverError(err))
    } finally {
      setBusyId(null)
    }
  }

  if (visible.length === 0 && !canCreate) return null

  return (
    <div className="space-y-2.5" data-alert-trigger={trigger}>
      {visible.map((a) => {
        const Icon =
          a.severity === "Blocking"
            ? ShieldAlert
            : a.severity === "Warning"
              ? AlertTriangle
              : Info
        const isResolving = resolvingId === a.name
        return (
          <div
            key={a.name}
            role={a.severity === "Blocking" ? "alert" : "status"}
            data-severity={a.severity}
            className={cn(
              "rounded-xl border px-4 py-3 text-start text-sm",
              SURFACE[a.severity],
            )}
          >
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="flex min-w-0 flex-1 items-start gap-2.5">
                <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span
                      className={cn(
                        "rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide",
                        BADGE_TONE[a.severity],
                      )}
                    >
                      {t(a.severity)}
                    </span>
                    <span className="rounded bg-black/5 px-1.5 py-0.5 text-[11px] font-medium dark:bg-white/10">
                      {t(a.trigger_area)}
                    </span>
                    {a.alert_code && (
                      <span className="font-mono text-[11px] opacity-75">
                        {a.alert_code}
                      </span>
                    )}
                    {a.status === "Acknowledged" && (
                      <span className="text-[11px] font-medium opacity-75">
                        · {t("Acknowledged")}
                      </span>
                    )}
                  </div>
                  <p className="whitespace-pre-wrap break-words text-sm font-medium">
                    {a.message}
                  </p>
                </div>
              </div>

              {!a.synthetic && (
                <div className="flex shrink-0 flex-wrap items-center gap-1.5">
                  {a.severity !== "Blocking" && a.status !== "Acknowledged" && (
                    <Button
                      type="button"
                      variant="outline"
                      disabled={busyId === a.name}
                      onClick={() => void handleAcknowledge(a)}
                      className="min-h-11 sm:min-h-8"
                    >
                      {t("Acknowledge")}
                    </Button>
                  )}
                  <Button
                    type="button"
                    variant={a.severity === "Blocking" ? "primary" : "outline"}
                    disabled={busyId === a.name}
                    onClick={() => {
                      if (a.severity === "Blocking") {
                        setResolvingId(isResolving ? null : a.name)
                        setResolveNote("")
                      } else {
                        void handleResolve(a)
                      }
                    }}
                    className="min-h-11 sm:min-h-8"
                  >
                    <CheckCircle2 className="size-3.5" aria-hidden />
                    {a.severity === "Blocking" ? t("Clear / Resolve") : t("Resolve")}
                  </Button>
                </div>
              )}
            </div>

            {isResolving && (
              <div className="mt-3 flex flex-col gap-2 border-t border-rose-200/70 pt-3 sm:flex-row sm:items-center dark:border-rose-500/30">
                <input
                  value={resolveNote}
                  onChange={(e) => setResolveNote(e.target.value)}
                  placeholder={t("Supervisor reason for clearing this blocking alert")}
                  aria-label={t("Resolution reason")}
                  className="min-h-11 min-w-0 flex-1 rounded-lg border border-rose-300 bg-white px-3 py-1.5 text-sm text-zinc-900 focus:outline-2 focus:outline-offset-1 focus:outline-rose-600 sm:min-h-9 dark:border-rose-500/50 dark:bg-zinc-900 dark:text-zinc-100"
                />
                <div className="flex items-center gap-2">
                  <Button
                    type="button"
                    disabled={busyId === a.name || !resolveNote.trim()}
                    onClick={() => void handleResolve(a)}
                    className="min-h-11 sm:min-h-9"
                  >
                    {t("Confirm resolve")}
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={() => {
                      setResolvingId(null)
                      setResolveNote("")
                    }}
                    className="min-h-11 sm:min-h-9"
                  >
                    {t("Cancel")}
                  </Button>
                </div>
              </div>
            )}
          </div>
        )
      })}

      {canCreate && (
        <div className="rounded-xl border border-zinc-200 bg-zinc-50/70 px-3.5 py-2.5 dark:border-zinc-700 dark:bg-zinc-900/50">
          {!creating ? (
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-xs font-medium text-zinc-600 dark:text-zinc-300">
                {visible.length === 0
                  ? t("No active alerts on this stay.")
                  : t("{n} active alert(s)", { n: visible.length })}
              </span>
              <Button
                type="button"
                variant="outline"
                onClick={() => setCreating(true)}
                className="min-h-11 sm:min-h-8"
              >
                <Plus className="size-3.5" aria-hidden />
                {t("Add alert")}
              </Button>
            </div>
          ) : (
            <form onSubmit={(e) => void handleCreate(e)} className="space-y-2.5 text-start">
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-zinc-500">
                    {t("Trigger")}
                  </span>
                  <select
                    aria-label={t("Trigger")}
                    value={draft.trigger_area}
                    onChange={(e) =>
                      setDraft((d) => ({
                        ...d,
                        trigger_area: e.target.value as AlertTrigger,
                      }))
                    }
                    className="min-h-11 w-full rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm sm:min-h-9 dark:border-zinc-700 dark:bg-zinc-900"
                  >
                    {TRIGGERS.map((tr) => (
                      <option key={tr} value={tr}>
                        {t(tr)}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-zinc-500">
                    {t("Severity")}
                  </span>
                  <select
                    aria-label={t("Severity")}
                    value={draft.severity}
                    onChange={(e) =>
                      setDraft((d) => ({
                        ...d,
                        severity: e.target.value as AlertSeverity,
                      }))
                    }
                    className="min-h-11 w-full rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm sm:min-h-9 dark:border-zinc-700 dark:bg-zinc-900"
                  >
                    {SEVERITIES.map((s) => (
                      <option key={s} value={s}>
                        {t(s)}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-zinc-500">
                    {t("Display")}
                  </span>
                  <select
                    aria-label={t("Display")}
                    value={draft.display_mode}
                    onChange={(e) =>
                      setDraft((d) => ({
                        ...d,
                        display_mode: e.target.value as AlertDisplayMode,
                      }))
                    }
                    className="min-h-11 w-full rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm sm:min-h-9 dark:border-zinc-700 dark:bg-zinc-900"
                  >
                    {DISPLAY_MODES.map((m) => (
                      <option key={m} value={m}>
                        {t(m)}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <textarea
                rows={2}
                value={draft.message}
                onChange={(e) => setDraft((d) => ({ ...d, message: e.target.value }))}
                placeholder={t("Alert message for staff (e.g. Passport check required)")}
                aria-label={t("Alert message")}
                className="w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm focus:outline-2 focus:outline-offset-1 focus:outline-brand-600 dark:border-zinc-700 dark:bg-zinc-900"
              />
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  type="submit"
                  disabled={busyId === "__create__" || !draft.message.trim()}
                  className="min-h-11 sm:min-h-9"
                >
                  {t("Save alert")}
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => setCreating(false)}
                  className="min-h-11 sm:min-h-9"
                >
                  {t("Cancel")}
                </Button>
              </div>
            </form>
          )}
        </div>
      )}

      {error && (
        <p role="alert" className="text-sm text-rose-700 dark:text-rose-300">
          {error}
        </p>
      )}
    </div>
  )
}
