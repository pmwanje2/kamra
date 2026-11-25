/*  Housekeeping Board for the housekeeping supervisor (#99): every room at a
    glance - status, who is in it, who is on it - inside the Housekeeping
    module, not just on the front desk's Today. Status changes go through
    set_housekeeping_status, which checks role, property and DocPerm. */

import { useCallback, useEffect, useState } from "react"
import { BedDouble, Lock, LogOut, RefreshCw, User, UserPlus, Wrench } from "lucide-react"
import { call, getCurrentProperty, setHousekeepingStatus } from "../lib/api"
import { serverError } from "../lib/resource"
import { Button } from "../components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card"
import { cn } from "../lib/utils"
import { sortFloors, type FloorOrder } from "../lib/floors"
import { housekeepingWorkRows, type HousekeepingWork } from "../lib/pmsOperations"
import { HousekeepingAssignment, HousekeepingRoomWork, type HousekeepingWorkRoom } from "../components/HousekeepingWork"
import { useT, t as translate } from "../lib/i18n"


// Ready: passed its inspection when the turnover profile requires one
type HkStatus =
  | "Clean"
  | "Dirty"
  | "Pickup"
  | "Inspected"
  | "Ready"
  | "Out of Service"
  | "Out of Order"

interface BoardRoom extends HousekeepingWorkRoom {
  name: string
  room_number: string
  room_type: string
  floor: string | null
  housekeeping_status: HkStatus
  occupancy_status: "Vacant" | "Occupied"
  os_oo_reason?: string | null
  os_oo_until?: string | null
  os_oo_set_by?: string | null
  guest_name: string | null
  due_out: boolean
  /** a same-day (hourly) stay - the room turns today, at etd */
  day_use?: boolean
  etd?: string | null
  clean_tasks: {name: string; type: string}[]
  task: {
    name: string
    type: string
    status: string
    priority: string
    assignee: string | null
  } | null
}

const STATUSES: HkStatus[] = [
  "Dirty",
  "Pickup",
  "Clean",
  "Inspected",
  "Ready",
  "Out of Service",
  "Out of Order",
]
// an attendant marks rooms Clean / Pickup / Dirty; passing or blocking a room is the
// supervisor's call - the server enforces the same rule
const ATTENDANT: HkStatus[] = ["Dirty", "Pickup", "Clean"]

const tone: Record<HkStatus, string> = {
  Clean: "border-success-line-strong bg-success-subtle text-success-ink",
  Pickup: "border-indigo-line-strong bg-indigo-subtle text-indigo-ink",
  Inspected: "border-info-line-strong bg-info-subtle text-info-ink",
  Ready: "border-teal-line-strong bg-teal-subtle text-teal-ink",
  Dirty: "border-warning-line-strong bg-warning-subtle text-warning-ink",
  "Out of Service": "border-notice-line-strong bg-notice-subtle text-notice-ink",
  "Out of Order": "border-danger-line-strong bg-danger-subtle text-danger-ink",
}
const dot: Record<HkStatus, string> = {
  Clean: "bg-emerald-500",
  Pickup: "bg-indigo-500",
  Inspected: "bg-sky-500",
  Ready: "bg-teal-500",
  Dirty: "bg-amber-500",
  "Out of Service": "bg-orange-500",
  "Out of Order": "bg-rose-500",
}

type Filter = "All" | HkStatus | "Due out" | "Has task"

export default function HousekeepingBoard() {
  const { t } = useT()
  const [work, setWork] = useState<HousekeepingWork | "assignment">("all")
  const [businessDate, setBusinessDate] = useState("")
  const [rooms, setRooms] = useState<BoardRoom[] | null>(null)
  const [canSupervise, setCanSupervise] = useState(false)
  const [canManageWork, setCanManageWork] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [floor, setFloor] = useState("All")
  const [filter, setFilter] = useState<Filter>("All")
  const [floorOrder, setFloorOrder] = useState<FloorOrder[]>([])
  const [roomTypes, setRoomTypes] = useState<{ name: string; room_type_name: string }[]>([])
  const [type, setType] = useState("All")
  // housekeepers a supervisor can hand a task to (hk_assign_task)
  const [staff, setStaff] = useState<{ user: string; full_name: string }[]>([])

  const load = useCallback(() => {
    call<{
      rooms: BoardRoom[]
      business_date: string
      can_supervise: boolean
      can_manage_work: boolean
      floors?: FloorOrder[]
      room_types?: { name: string; room_type_name: string }[]
    }>("kamra.api.room_board", {
      property: getCurrentProperty(),
    })
      .then((d) => {
        setRooms(d.rooms)
        setBusinessDate(d.business_date)
        setCanSupervise(d.can_supervise)
        setCanManageWork(d.can_manage_work)
        setFloorOrder(d.floors ?? [])
        setRoomTypes(d.room_types ?? [])
        setError(null)
      })
      .catch((e) => setError(serverError(e)))
  }, [])

  useEffect(() => {
    load()
    // Match Rooms/Today: 60s poll, skip while the tab is hidden.
    const id = setInterval(() => {
      if (document.visibilityState === "hidden") return
      load()
    }, 60_000)
    return () => clearInterval(id)
  }, [load])

  useEffect(() => {
    if (!canSupervise) return
    call<{ user: string; full_name: string }[]>("kamra.api.hk_staff", {
      property: getCurrentProperty(),
    })
      .then(setStaff)
      .catch(() => setStaff([]))
  }, [canSupervise])

  async function assign(room: string, task: string, user: string) {
    setBusy(room)
    setOpen(null)
    try {
      await call("kamra.api.hk_assign_task", { task, user })
      load()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(null)
    }
  }

  async function complete(room: string, task: string) {
    setBusy(room)
    try {
      await call("kamra.api.hk_update_task", {task, status: "Done"})
      setOpen(null); load()
    } catch (e) { setError(serverError(e)) } finally { setBusy(null) }
  }

  async function setStatus(room: string, status: HkStatus) {
    const inspection = status === "Inspected" || status === "Ready"
    const reason = inspection ? undefined : window.prompt(t("Reason for overriding room status"))?.trim()
    if (!inspection && !reason) return
    const needsUntil = status === "Out of Service" || status === "Out of Order"
    const untilDate = needsUntil
      ? window.prompt(t("Return date YYYY-MM-DD (optional)"))?.trim() || undefined
      : undefined
    setBusy(room)
    setOpen(null)
    try {
      await setHousekeepingStatus(room, status, reason, untilDate)
      load()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(null)
    }
  }

  if (error && !rooms)
    return (
      <div className="rounded-lg border border-danger-line bg-danger-subtle px-4 py-3 text-sm text-danger-ink">
        {translate(error)}
      </div>
    )
  if (!rooms) return <p className="py-10 text-center text-muted-ink">{t("Loading…")}</p>

  const floors = sortFloors(
    Array.from(new Set(rooms.map((r) => r.floor).filter(Boolean))) as string[],
    floorOrder,
  )
  const counts = Object.fromEntries(
    STATUSES.map((s) => [s, rooms.filter((r) => r.housekeeping_status === s).length]),
  ) as Record<HkStatus, number>
  const dueOut = rooms.filter((r) => r.due_out).length
  const withTask = rooms.filter((r) => r.task).length
  const shown = rooms.filter(
    (r) =>
      (floor === "All" || r.floor === floor) &&
      (type === "All" || r.room_type === type) &&
      (filter === "All" ||
        (filter === "Due out" ? r.due_out : filter === "Has task" ? !!r.task : r.housekeeping_status === filter)),
  )
  const groups = floor === "All" && floors.length ? floors : [floor === "All" ? "" : floor]
  const choices = canSupervise ? STATUSES : ATTENDANT
  const discrepancies = housekeepingWorkRows(rooms, "discrepancies")
  const tabs = [
    { id: "all", label: t("All Rooms") },
    { id: "discrepancies", label: t("Discrepancies ({count})", { count: discrepancies.length }) },
    { id: "turndown", label: t("Turndown") },
    ...(canManageWork ? [{ id: "assignment", label: t("Auto-Assign Queue") }] : []),
  ] as { id: HousekeepingWork | "assignment"; label: string }[]

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink">{t("Housekeeping Board")}</h1>
          <p className="text-sm text-muted-ink">
            {canSupervise
              ? t("Every room at a glance. Tap a room to set its status.")
              : t("Every room at a glance. You can mark rooms Clean, Pickup, or Dirty.")}
          </p>
        </div>
        <Button className="min-h-11" variant="outline" onClick={load}>
          <RefreshCw className="size-4" aria-hidden />
          {t("Refresh")}
        </Button>
      </div>

      {error && (
        <div className="rounded-lg border border-danger-line bg-danger-subtle px-4 py-2 text-sm text-danger-ink">
          {translate(error)}
        </div>
      )}

      <nav className="flex flex-wrap gap-2" aria-label={t("Housekeeping views")}>
        {tabs.map((tab) => (
          <Button className="min-h-11" key={tab.id} variant={work === tab.id ? "primary" : "outline"}
            aria-pressed={work === tab.id} onClick={() => setWork(tab.id)}>{tab.label}</Button>
        ))}
      </nav>

      {work === "assignment" && canManageWork && (
        <HousekeepingAssignment businessDate={businessDate} staff={staff} refresh={load} />
      )}
      {(work === "discrepancies" || work === "turndown") && (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {housekeepingWorkRows(rooms, work).length === 0 && <p>{t("No rooms match.")}</p>}
          {housekeepingWorkRows(rooms, work).map((room) => (
            <Card key={room.name}>
              <CardHeader>
                <CardTitle>{room.room_number}</CardTitle>
                {room.hk_section && <p className="text-sm">{room.hk_section}</p>}
              </CardHeader>
              <CardContent><HousekeepingRoomWork room={room} canResolve={canManageWork} refresh={load} /></CardContent>
            </Card>
          ))}
        </div>
      )}

      {work === "all" && <>
      <div className="flex flex-wrap gap-2" role="toolbar" aria-label={t("Filter rooms")}>
        {(
          [
            ["All", rooms.length, "bg-fill-prominent"],
            // Ready only exists where inspections are on - no empty chip otherwise
            ...STATUSES.filter((s) => s !== "Ready" || counts.Ready > 0 || filter === "Ready").map(
              (s) => [s, counts[s], dot[s]],
            ),
            ["Due out", dueOut, "bg-violet-500"],
            ["Has task", withTask, "bg-inverse-muted"],
          ] as [Filter, number, string][]
        ).map(([f, n, d]) => (
          <button
            key={f}
            type="button"
            aria-pressed={filter === f}
            onClick={() => setFilter(f)}
            className={cn(
              "inline-flex min-h-11 items-center gap-2 rounded-lg border px-3 py-1.5 text-sm",
              filter === f
                ? "border-accent-control bg-accent-subtle font-medium text-accent-ink"
                : "border-line bg-surface text-secondary-ink hover:border-control",
            )}
          >
            <span className={cn("size-2 rounded-full", d)} aria-hidden />
            {t(f)}
            <span className="tabular-nums text-muted-ink">{n}</span>
          </button>
        ))}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{t("Rooms")}</CardTitle>
          <div className="flex flex-wrap items-center gap-2">
          {roomTypes.length > 1 && (
            <select
              value={type}
              onChange={(e) => setType(e.target.value)}
              aria-label={t("Room type")}
              className="rounded-lg border border-control bg-surface px-2 py-1 text-xs"
            >
              <option value="All">{t("All room types")}</option>
              {roomTypes.map((rt) => (
                <option key={rt.name} value={rt.name}>
                  {rt.room_type_name}
                </option>
              ))}
            </select>
          )}
          {floors.length > 1 && (
            <div className="flex flex-wrap gap-1">
              {["All", ...floors].map((f) => (
                <button
                  key={f}
                  type="button"
                  onClick={() => setFloor(f)}
                  className={cn(
                    "rounded-md px-2.5 py-1 text-xs font-medium",
                    floor === f ? "bg-inverse text-on-inverse" : "text-muted-ink hover:bg-subtle",
                  )}
                >
                  {f === "All" ? t("All Floors") : t("Floor {f}", { f })}
                </button>
              ))}
            </div>
          )}
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {shown.length === 0 && (
            <p className="py-6 text-center text-sm text-muted-ink">{t("No rooms match.")}</p>
          )}
          {groups.map((g) => {
            const list = shown.filter((r) => !g || r.floor === g)
            if (!list.length) return null
            return (
              <div key={g || "all"}>
                {g && (
                  <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-ink">
                    {t("Floor {f}", { f: g })}
                  </p>
                )}
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-6">
                  {list.map((r) => (
                    <div key={r.name} className="relative min-w-0">
                      <button
                        type="button"
                        disabled={busy === r.name}
                        aria-expanded={open === r.name}
                        onClick={() => setOpen(open === r.name ? null : r.name)}
                        className={cn(
                          "min-h-11 w-full rounded-lg border px-3 py-2.5 text-start transition-colors",
                          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
                          tone[r.housekeeping_status],
                          busy === r.name && "opacity-60",
                        )}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="min-w-0 break-words text-base font-semibold">{r.room_number}</span>
                          <span className="flex items-center gap-1">
                            {r.housekeeping_status === "Out of Service" && (
                              <Wrench className="size-3.5 text-notice-text" aria-label={t("Out of Service")} />
                            )}
                            {r.housekeeping_status === "Out of Order" && (
                              <Lock className="size-3.5 text-danger-text" aria-label={t("Out of Order")} />
                            )}
                            {r.due_out && (
                              <LogOut className="size-3.5 text-violet-text" aria-label={t("Due out")} />
                            )}
                            {r.occupancy_status === "Occupied" && (
                              <BedDouble className="size-3.5" aria-label={t("Occupied")} />
                            )}
                          </span>
                        </div>
                        <div className="mt-0.5 text-[11px] font-medium uppercase tracking-wide opacity-80">
                          {t(r.housekeeping_status)}
                        </div>
                        {(r.housekeeping_status === "Out of Service" || r.housekeeping_status === "Out of Order") &&
                          (r.os_oo_reason || r.os_oo_until) && (
                            <div className="mt-1 truncate text-xs opacity-90">
                              {r.os_oo_reason}
                              {r.os_oo_until ? ` · ${r.os_oo_until}` : ""}
                            </div>
                          )}
                        {r.guest_name && (
                          <div className="mt-1 truncate text-xs opacity-80">{r.guest_name}</div>
                        )}
                        {r.day_use && (
                          <div className="mt-1 inline-flex items-center gap-1 rounded bg-violet-muted/80 px-1.5 py-0.5 text-[11px] font-medium text-violet-ink">
                            {r.etd ? t("Day use · out {time}", { time: r.etd }) : t("Day use")}
                          </div>
                        )}
                        {r.task && (
                          <div className="mt-1.5 flex items-center gap-1 truncate rounded bg-surface/70 px-1.5 py-0.5 text-[11px] text-secondary-ink">
                            <User className="size-3 shrink-0" aria-hidden />
                            <span className="truncate">
                              {t(r.task.type)} · {r.task.assignee ?? t("Unassigned")}
                            </span>
                          </div>
                        )}
                      </button>
                      {open === r.name && (
                        <div
                          aria-label={t("Room actions")} role="group"
                          className="relative z-10 mt-1 min-w-0 rounded-lg border border-line bg-surface shadow-lg"
                        >
                          <div className="col-span-full border-b p-2 border-line">
                            <HousekeepingRoomWork room={r} canResolve={canManageWork} refresh={load} />
                          </div>
                          {r.clean_tasks.length > 1 && <p className="p-2 text-xs">{t("Choose a cleaning task")}</p>}
                          {r.clean_tasks.map(task => <Button key={task.name} className="w-full whitespace-normal" disabled={busy === r.name} onClick={() => complete(r.name, task.name)}>
                            {task.type === "Checkout Clean" ? t("Complete Checkout Clean") : t("Complete Stayover Clean")}{r.clean_tasks.length > 1 && ` · ${task.name}`}</Button>)}
                          <details><summary className="cursor-pointer p-2 text-sm">{t("Override room status")}</summary>
                          {choices
                            .filter((s) => s !== r.housekeeping_status && s !== "Inspected" && s !== "Ready")
                            .map((s) => (
                              <button
                                key={s}
                                type="button"
                                onClick={() => setStatus(r.name, s)}
                                className="flex min-h-11 w-full items-center gap-2 px-3 py-2 text-start text-sm hover:bg-canvas sm:min-h-9"
                              >
                                <span className={cn("size-2 rounded-full", dot[s])} aria-hidden />
                                {t("Mark {status}", { status: t(s) })}
                              </button>
                            ))}
                          </details>
                          {canSupervise && choices.filter(s => (s === "Inspected" || s === "Ready") && s !== r.housekeeping_status).map(s => <Button key={s} variant="outline" onClick={() => setStatus(r.name, s)}>{t("Mark {status}", {status: t(s)})}</Button>)}
                          {canSupervise && r.task && (
                            <div className="border-t border-line-subtle px-3 py-2">
                              <label className="flex items-center gap-1.5 text-xs font-medium text-muted-ink">
                                <UserPlus className="size-3.5" aria-hidden />
                                {t("Assign {task} to", { task: t(r.task.type) })}
                              </label>
                              <select
                                className="mt-1 w-full rounded-md border border-control bg-surface px-2 py-1.5 text-sm"
                                value=""
                                onChange={(e) => e.target.value && assign(r.name, r.task!.name, e.target.value)}
                              >
                                <option value="">
                                  {staff.length ? t("Choose a housekeeper…") : t("No housekeeping staff")}
                                </option>
                                {staff.map((u) => (
                                  <option key={u.user} value={u.user}>
                                    {u.full_name}
                                  </option>
                                ))}
                              </select>
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )
          })}
        </CardContent>
      </Card>
      </>}
    </div>
  )
}
