import { useMemo, useState } from "react"
import { GripVertical, Sparkles, Star } from "lucide-react"
import { Button } from "./ui/button"
import { Badge } from "./ui/badge"
import { cn } from "../lib/utils"
import { hkTone, hkWeight, isAssignableHkStatus } from "../lib/hkStatus"
import { useT } from "../lib/i18n"

export interface UnassignedBooking {
  name: string
  guest?: string | null
  guest_name: string
  vip?: 0 | 1
  room_type: string
  room_type_name?: string
  check_in_date: string
  check_out_date: string
  nights?: number
  is_day_use?: 0 | 1
  adults?: number
  children?: number
  booking_type?: string
  source?: string
  company?: string | null
  travel_agent?: string | null
  group_booking?: string | null
  special_requests?: string | null
  planned_check_in_time?: string | null
  planned_check_out_time?: string | null
}

export interface DockRoomOption {
  name: string
  room_number: string
  room_type: string
  housekeeping_status: string
  bookings: { check_in_date: string; check_out_date: string; is_day_use?: 0 | 1 }[]
  blocks?: { from_date: string; to_date: string }[]
}

function segmentDot(item: UnassignedBooking): string {
  if (item.vip) return "bg-amber-400"
  if (item.booking_type === "Group" || item.group_booking) return "bg-sky-400"
  if (item.booking_type === "Corporate" || item.company) return "bg-violet-400"
  if (item.source === "OTA") return "bg-orange-400"
  return "bg-zinc-400"
}

function overlaps(aStart: string, aEnd: string, bStart: string, bEnd: string): boolean {
  const effAEnd = aEnd > aStart ? aEnd : `${aStart}T24`
  const effBEnd = bEnd > bStart ? bEnd : `${bStart}T24`
  return aStart < effBEnd && effAEnd > bStart
}

function freeCandidatesFor(item: UnassignedBooking, rooms: DockRoomOption[]): DockRoomOption[] {
  return rooms
    .filter((room) => {
      if (room.room_type !== item.room_type) return false
      if (!isAssignableHkStatus(room.housekeeping_status)) return false
      const hasBooking = room.bookings.some((b) =>
        overlaps(item.check_in_date, item.check_out_date, b.check_in_date, b.check_out_date),
      )
      if (hasBooking) return false
      const hasBlock = (room.blocks ?? []).some((blk) =>
        overlaps(item.check_in_date, item.check_out_date, blk.from_date, blk.to_date),
      )
      return !hasBlock
    })
    .sort((a, b) => {
      const diff = hkWeight(b.housekeeping_status) - hkWeight(a.housekeeping_status)
      if (diff !== 0) return diff
      return a.room_number.localeCompare(b.room_number, undefined, { numeric: true })
    })
}

export function TapeUnassignedDock({
  items,
  rooms,
  selectedDate,
  busy,
  onAutoAssign,
  onQuickAssign,
  onStartDrag,
  className,
}: {
  items: UnassignedBooking[]
  rooms: DockRoomOption[]
  selectedDate: string
  busy?: boolean
  onAutoAssign: () => void
  onQuickAssign: (reservation: string, room: string) => Promise<void> | void
  onStartDrag?: (e: React.PointerEvent, item: UnassignedBooking) => void
  className?: string
}) {
  const { t, tn } = useT()
  const [scope, setScope] = useState<"today" | "all">("all")
  const [picked, setPicked] = useState<Record<string, string>>({})
  const [assigning, setAssigning] = useState<string | null>(null)

  const shown = useMemo(() => {
    if (scope === "today") {
      return items.filter((item) => item.check_in_date === selectedDate)
    }
    return items
  }, [items, scope, selectedDate])

  async function assign(item: UnassignedBooking, candidates: DockRoomOption[]) {
    const targetRoom = picked[item.name] || candidates[0]?.name
    if (!targetRoom) return
    setAssigning(item.name)
    try {
      await onQuickAssign(item.name, targetRoom)
    } finally {
      setAssigning(null)
    }
  }

  return (
    <aside
      data-testid="tape-unassigned-dock"
      aria-label={t("Unassigned queue")}
      className={cn(
        "flex flex-col rounded-xl border border-zinc-200 bg-white shadow-sm",
        className,
      )}
    >
      <div className="space-y-2 border-b border-zinc-200 p-3">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-semibold text-zinc-900">
              {t("Unassigned ({n})", { n: shown.length })}
            </h2>
          </div>
          <div className="inline-flex rounded-lg border border-zinc-200 p-0.5 text-xs">
            <button
              type="button"
              onClick={() => setScope("today")}
              className={cn(
                "min-h-9 rounded-md px-2 py-1 font-medium transition md:min-h-7",
                scope === "today" ? "bg-brand-50 text-brand-700" : "text-zinc-500 hover:text-zinc-700",
              )}
            >
              {t("Today's arrivals")}
            </button>
            <button
              type="button"
              onClick={() => setScope("all")}
              className={cn(
                "min-h-9 rounded-md px-2 py-1 font-medium transition md:min-h-7",
                scope === "all" ? "bg-brand-50 text-brand-700" : "text-zinc-500 hover:text-zinc-700",
              )}
            >
              {t("All window")}
            </button>
          </div>
        </div>
        <Button
          type="button"
          variant="outline"
          data-testid="dock-auto-assign"
          disabled={busy || items.length === 0}
          onClick={onAutoAssign}
          className="min-h-11 w-full justify-center md:min-h-9"
        >
          <Sparkles className="size-4 text-brand-600" aria-hidden />
          {t("Auto-Assign (Inspected/Ready)")}
        </Button>
      </div>

      <div className="flex-1 space-y-2 overflow-y-auto p-3">
        {shown.length === 0 ? (
          <p className="py-6 text-center text-xs text-zinc-400">
            {t("No unassigned bookings in this view.")}
          </p>
        ) : (
          shown.map((item) => {
            const candidates = freeCandidatesFor(item, rooms)
            const selectedRoom = picked[item.name] || candidates[0]?.name || ""
            const nights = item.nights ?? 1
            return (
              <div
                key={item.name}
                data-unassigned-reservation={item.name}
                onPointerDown={(e) => {
                  const target = e.target as HTMLElement | null
                  if (target?.closest("select, button, input, a")) return
                  onStartDrag?.(e, item)
                }}
                className="cursor-grab select-none rounded-lg border border-zinc-200 bg-zinc-50/60 p-2.5 transition hover:border-brand-300 hover:bg-white active:cursor-grabbing"
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5 text-sm font-semibold text-zinc-900">
                      {item.vip === 1 ? (
                        <Star className="size-3.5 shrink-0 fill-amber-400 text-amber-400" aria-hidden />
                      ) : (
                        <span className={cn("size-2 shrink-0 rounded-full", segmentDot(item))} aria-hidden />
                      )}
                      <span className="truncate">{item.guest_name}</span>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-zinc-500">
                      <Badge tone="sky">{item.room_type_name || item.room_type}</Badge>
                      <span className="tabular-nums">
                        {item.check_in_date} → {item.check_out_date}
                      </span>
                      <span>({tn(nights, "{n} night", "{n} nights")})</span>
                    </div>
                  </div>
                  <GripVertical className="mt-0.5 size-4 shrink-0 text-zinc-300" aria-hidden />
                </div>

                {item.special_requests ? (
                  <p className="mt-1.5 rounded-md bg-amber-50 px-2 py-1 text-xs text-amber-800">
                    {item.special_requests}
                  </p>
                ) : null}

                <div className="mt-2 flex items-center gap-1.5">
                  <select
                    aria-label={t("Assign room for {guest}", { guest: item.guest_name })}
                    value={selectedRoom}
                    onChange={(e) =>
                      setPicked((prev) => ({ ...prev, [item.name]: e.target.value }))
                    }
                    className="min-h-11 min-w-0 flex-1 rounded-lg border border-zinc-300 bg-white px-2 py-1 text-xs md:min-h-8"
                  >
                    {candidates.length === 0 ? (
                      <option value="">{t("No free room")}</option>
                    ) : (
                      candidates.map((cand) => (
                        <option key={cand.name} value={cand.name}>
                          {`${cand.room_number} · ${t(cand.housekeeping_status)}`}
                        </option>
                      ))
                    )}
                  </select>
                  <Button
                    type="button"
                    variant="outline"
                    data-testid={`quick-assign-${item.name}`}
                    disabled={busy || assigning === item.name || !selectedRoom}
                    onClick={() => assign(item, candidates)}
                    className="min-h-11 shrink-0 px-2.5 text-xs md:min-h-8"
                  >
                    {t("Assign")}
                  </Button>
                </div>

                {selectedRoom ? (
                  <div className="mt-1 flex items-center gap-1 text-[10px] text-zinc-400">
                    {(() => {
                      const chosen = candidates.find((c) => c.name === selectedRoom)
                      if (!chosen) return null
                      const tone = hkTone(chosen.housekeeping_status)
                      return (
                        <>
                          <span className={cn("size-1.5 rounded-full", tone.dot)} aria-hidden />
                          <span>{t(chosen.housekeeping_status)}</span>
                        </>
                      )
                    })()}
                  </div>
                ) : null}
              </div>
            )
          })
        )}
      </div>
    </aside>
  )
}
