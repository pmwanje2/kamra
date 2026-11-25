import { useState } from "react"
import { call, getCurrentProperty } from "../lib/api"
import { useT } from "../lib/i18n"
import { serverError } from "../lib/resource"
import { Button } from "./ui/button"

export interface HousekeepingWorkRoom {
  name: string
  room_number: string
  hk_section?: string
  hk_occupancy?: "Vacant" | "Occupied" | null
  fd_occupancy?: "Vacant" | "Occupied"
  discrepancy?: "Skip" | "Sleep" | null
  turndown_status?: string
}

const control = "min-h-11 rounded-lg border border-zinc-300 bg-white px-3 text-sm text-zinc-900 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100"


const roomActions = {
  report: "kamra.housekeeping.report_hk_occupancy",
  turndown: "kamra.housekeeping.set_turndown_status",
  resolve: "kamra.housekeeping.resolve_hk_discrepancy",
} as const

export function HousekeepingRoomWork({ room, canResolve, refresh }: {
  room: HousekeepingWorkRoom
  canResolve: boolean
  refresh: () => void
}) {
  const { t } = useT()
  const [note, setNote] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const occupancy = room.fd_occupancy ?? "Vacant"
  const statuses = {
    "Not Required": t("Not Required"), Pending: t("Pending"),
    Completed: t("Completed"), DND: t("DND"),
  }

  async function update(action: keyof typeof roomActions, args: Record<string, string>) {
    setError("")
    setBusy(true)
    try {
      await call(roomActions[action], { room: room.name, ...args })
      setNote("")
      refresh()
    } catch (cause) {
      setError(serverError(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid min-w-0 gap-2 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <p className="font-medium">{t("Front desk: {status}", { status: t(occupancy) })}</p>
        {room.discrepancy && <strong className="rounded-full bg-amber-100 px-2 py-1 text-amber-900 dark:bg-amber-950 dark:text-amber-200">
          {room.discrepancy === "Skip" ? t("Skip (occupancy)") : t("Sleep (occupancy)")}
        </strong>}
      </div>
      <label className="grid gap-1">
        {t("Reported occupancy")}
        <select className={control} disabled={busy} value={room.hk_occupancy ?? ""}
          onChange={(event) => void update("report", { hk_occupancy: event.target.value })}>
          <option value="" disabled>{t("Select status")}</option>
          <option value="Vacant">{t("Vacant")}</option>
          <option value="Occupied">{t("Occupied")}</option>
        </select>
      </label>
      {occupancy === "Occupied" && (
        <label className="grid gap-1">
          {t("Turndown")}
          <select className={control} disabled={busy} value={room.turndown_status ?? "Not Required"}
            onChange={(event) => void update("turndown", { status: event.target.value })}>
            {Object.entries(statuses).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
      )}
      {canResolve && room.discrepancy && (
        <form className="grid gap-2" onSubmit={(event) => {
          event.preventDefault()
          if (note.trim()) void update("resolve", { resolution_note: note.trim() })
        }}>
          <label className="grid gap-1">
            {t("Resolution note")}
            <input className={control} value={note} onChange={(event) => setNote(event.target.value)} required />
          </label>
          <Button className="min-h-11" disabled={busy || !note.trim()} type="submit">{t("Resolve discrepancy")}</Button>
        </form>
      )}
      {error && <p role="alert" className="text-rose-700 dark:text-rose-300">{t(error)}</p>}
    </div>
  )
}

interface Assignment {
  task: string
  room_number: string
  section: string
  task_type: string
  credits: number
  user?: string
}

interface AssignmentResult {
  loads: { user: string; full_name: string; credits: number; max_credits: number }[]
  assigned: Assignment[]
  unassigned: Assignment[]
}

export function HousekeepingAssignment({ businessDate, staff, refresh }: {
  businessDate: string
  staff: { user: string; full_name: string }[]
  refresh: () => void
}) {
  const { t, lang } = useT()
  const [cap, setCap] = useState("16")
  const [excluded, setExcluded] = useState<string[]>([])
  const [result, setResult] = useState<AssignmentResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const attendants = staff.filter((row) => !excluded.includes(row.user)).map((row) => row.user)
  const credit = new Intl.NumberFormat(lang, { minimumFractionDigits: 1, maximumFractionDigits: 3 })

  async function assign(preview: boolean) {
    setBusy(true)
    setError("")
    try {
      setResult(await call<AssignmentResult>("kamra.housekeeping.auto_assign_housekeeping_tasks", {
        property: getCurrentProperty(), business_date: businessDate, attendants,
        max_credits_per_attendant: cap, preview: Number(preview),
      }))
      if (!preview) refresh()
    } catch (cause) {
      setResult(null)
      setError(serverError(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4 rounded-lg border bg-white p-4 dark:border-zinc-700 dark:bg-zinc-900">
      <label className="grid gap-1 text-sm sm:max-w-xs">
        {t("Credit limit per attendant")}
        <input type="number" inputMode="decimal" min="0.001" step="0.001" className={control} value={cap}
          onChange={(event) => { setCap(event.target.value); setResult(null) }} />
      </label>
      <fieldset className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        <legend className="text-sm font-medium">{t("Attendants")}</legend>
        {staff.map((row) => (
          <label key={row.user} className="flex min-h-11 items-center gap-2 text-sm">
            <input type="checkbox" checked={!excluded.includes(row.user)} onChange={(event) => {
              setExcluded(event.target.checked ? excluded.filter((user) => user !== row.user) : [...excluded, row.user])
              setResult(null)
            }} />
            {row.full_name}
          </label>
        ))}
      </fieldset>
      <div className="flex flex-wrap gap-2">
        <Button className="min-h-11" variant="outline" disabled={busy || !attendants.length || !(Number(cap) > 0)}
          onClick={() => void assign(true)}>{t("Preview assignments")}</Button>
        <Button className="min-h-11" disabled={busy || !attendants.length || !(Number(cap) > 0)}
          onClick={() => void assign(false)}>{t("Auto-assign tasks")}</Button>
      </div>
      {error && <p role="alert" className="text-rose-700 dark:text-rose-300">{t(error)}</p>}
      {result && (
        <div aria-live="polite" className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {result.loads.map((load) => {
              const text = t("{used} / {limit} credits", {
                used: credit.format(load.credits), limit: credit.format(load.max_credits),
              })
              return (
                <div key={load.user} className="rounded-lg border p-3 text-sm dark:border-zinc-700">
                  <p className="font-medium">{load.full_name}</p>
                  <p>{text}</p>
                  <progress className="mt-2 w-full accent-brand-700" aria-label={`${load.full_name} · ${text}`}
                    value={Math.min(load.credits, load.max_credits)} max={load.max_credits} />
                </div>
              )
            })}
          </div>
          {([...result.assigned, ...result.unassigned]).map((row) => (
            <div key={row.task} className="flex flex-wrap gap-x-3 gap-y-1 border-t py-2 text-sm dark:border-zinc-700">
              <strong>{row.room_number}</strong>
              <span>{row.task_type === "Turndown" ? t("Turndown") : t(row.task_type)}</span>
              {row.section && <span>{row.section}</span>}
              <span>{staff.find((member) => member.user === row.user)?.full_name ?? t("Unassigned")}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
