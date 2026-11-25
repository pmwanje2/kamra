import { useState } from "react"
import { Lock, Plus, Star, Users } from "lucide-react"
import { Badge } from "./ui/badge"
import { Button } from "./ui/button"
import { useAuth } from "../lib/auth"
import { cn } from "../lib/utils"
import { hkTone, isAssignableHkStatus, visibleHkToggleOptions } from "../lib/hkStatus"
import { useT, t as translate } from "../lib/i18n"

export interface MobileTapeBooking {
  name: string
  room: string
  guest_name: string
  status: "Confirmed" | "Checked In"
  check_in_date: string
  check_out_date: string
  is_day_use: 0 | 1
  vip?: 0 | 1
  source?: string
  booking_type?: string
  company?: string | null
  travel_agent?: string | null
  group_booking?: string | null
  planned_check_in_time?: string | null
  planned_check_out_time?: string | null
}

export interface MobileTapeRoom {
  name: string
  room_number: string
  room_type: string
  room_type_name?: string
  floor?: string | null
  housekeeping_status: string
  bookings: MobileTapeBooking[]
  blocks?: { name: string; reason: string; from_date: string; to_date: string; note?: string | null }[]
}

export interface RoomAttributeMeta {
  floor?: string
  wing?: string
  room_view?: string
  bed_type?: string
  smoking_allowed?: number
  air_conditioning?: string
  kitchen?: number
  amenities?: string[]
}

function bookingOnDate(room: MobileTapeRoom, date: string): MobileTapeBooking | null {
  for (const b of room.bookings) {
    if (b.is_day_use === 1 && b.check_in_date === date) return b
    if (b.check_in_date <= date && b.check_out_date > date) return b
  }
  return null
}

function blockOnDate(room: MobileTapeRoom, date: string) {
  return (room.blocks ?? []).find((k) => k.from_date <= date && k.to_date > date) ?? null
}

export function TapeMobileCards({
  groups,
  selectedDate,
  today,
  canBook,
  unassignedCount,
  roomMeta,
  onOpenQueue,
  onOpenBooking,
  onNewBooking,
  onChangeHk,
}: {
  groups: { key: string; label: string; rooms: MobileTapeRoom[] }[]
  selectedDate: string
  today: string
  canBook: boolean
  unassignedCount: number
  roomMeta: Record<string, RoomAttributeMeta>
  onOpenQueue: () => void
  onOpenBooking: (b: MobileTapeBooking) => void
  onNewBooking: (room: MobileTapeRoom, date: string) => void
  onChangeHk: (room: MobileTapeRoom, nextStatus: string, reason?: string) => Promise<void> | void
}) {
  const { t } = useT()
  const { roles } = useAuth()
  const hkOptions = visibleHkToggleOptions(roles)
  const hkOooOption = hkOptions.find((opt) => opt.needsReason)
  const [hkMenuRoom, setHkMenuRoom] = useState<string | null>(null)
  const [oooReason, setOooReason] = useState("")

  return (
    <div data-testid="tape-mobile-cards" className="space-y-3 overflow-x-hidden md:hidden">
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-line bg-canvas px-3 py-2">
        <span className="text-xs font-medium text-secondary-ink tabular-nums">
          {selectedDate}
        </span>
        <Button
          type="button"
          variant="outline"
          onClick={onOpenQueue}
          className="min-h-11 px-3 text-xs"
        >
          <Users className="size-4 text-accent-text" aria-hidden />
          {t("Queue ({n})", { n: unassignedCount })}
        </Button>
      </div>

      {groups.length === 0 ? (
        <p className="rounded-xl border border-line bg-surface px-4 py-8 text-center text-sm text-muted-ink">
          {t("No rooms match the selected filters.")}
        </p>
      ) : (
        groups.map((g) => (
          <section key={g.key} className="space-y-2">
            <div className="flex items-center justify-between px-1 text-xs font-semibold uppercase tracking-wide text-muted-ink">
              <span>{translate(g.label)}</span>
              <span className="font-normal normal-case text-muted-ink">{g.rooms.length}</span>
            </div>
            {g.rooms.map((room) => {
              const stay = bookingOnDate(room, selectedDate)
              const blk = blockOnDate(room, selectedDate)
              const meta = roomMeta[room.name]
              const tone = hkTone(room.housekeeping_status)
              const tags = [meta?.floor || room.floor, meta?.room_view, meta?.bed_type]
                .filter(Boolean)
                .slice(0, 3)
              const menuOpen = hkMenuRoom === room.name

              return (
                <div
                  key={room.name}
                  data-mobile-room={room.name}
                  className="rounded-xl border border-line bg-surface p-3 shadow-sm"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="text-base font-semibold text-ink">
                          {room.room_number}
                        </span>
                        <span className="truncate text-xs text-muted-ink">
                          {room.room_type_name || room.room_type}
                        </span>
                      </div>
                      {tags.length > 0 && (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {tags.map((tag) => (
                            <span
                              key={String(tag)}
                              className="rounded bg-subtle px-1.5 py-0.5 text-[11px] text-secondary-ink"
                            >
                              {String(tag)}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>

                    <button
                      type="button"
                      data-testid={`hk-toggle-mobile-${room.name}`}
                      aria-expanded={menuOpen}
                      aria-label={t("Change housekeeping status for room {room}", { room: room.room_number })}
                      onClick={() => {
                        setHkMenuRoom(menuOpen ? null : room.name)
                        setOooReason("")
                      }}
                      className={cn(
                        "inline-flex min-h-11 min-w-11 shrink-0 items-center justify-center gap-1.5 rounded-lg border px-2.5 text-xs font-medium",
                        tone.badge,
                      )}
                    >
                      <span className={cn("size-2 rounded-full", tone.dot)} aria-hidden />
                      <span>{t(room.housekeeping_status)}</span>
                    </button>
                  </div>

                  {menuOpen && (
                    <div className="mt-2 space-y-2 rounded-lg border border-line bg-canvas p-2">
                      <div className="grid grid-cols-2 gap-1.5">
                        {hkOptions.filter((opt) => !opt.needsReason).map((opt) => (
                          <button
                            key={opt.status}
                            type="button"
                            data-hk-option={opt.status}
                            onClick={async () => {
                              setHkMenuRoom(null)
                              await onChangeHk(room, opt.status)
                            }}
                            className="min-h-11 rounded-md border border-line bg-surface px-2.5 text-xs font-medium text-ink hover:bg-subtle"
                          >
                            {t(opt.status)}
                          </button>
                        ))}
                      </div>
                      {hkOooOption && (
                        <div className="flex items-center gap-1.5">
                          <input
                            value={oooReason}
                            onChange={(e) => setOooReason(e.target.value)}
                            placeholder={t("Out of Order reason")}
                            className="min-h-11 min-w-0 flex-1 rounded-md border border-control bg-surface px-2 text-xs"
                          />
                          <Button
                            type="button"
                            variant="outline"
                            data-hk-option={hkOooOption.status}
                            disabled={!oooReason.trim()}
                            onClick={async () => {
                              const reason = oooReason.trim()
                              setHkMenuRoom(null)
                              setOooReason("")
                              await onChangeHk(room, hkOooOption.status, reason)
                            }}
                            className="min-h-11 shrink-0 px-2.5 text-xs text-danger-ink"
                          >
                            {t(hkOooOption.status)}
                          </Button>
                        </div>
                      )}
                    </div>
                  )}

                  <div className="mt-2.5">
                    {stay ? (
                      <button
                        type="button"
                        onClick={() => onOpenBooking(stay)}
                        className={cn(
                          "flex min-h-11 w-full items-center justify-between gap-2 rounded-lg px-3 py-2 text-start text-sm font-medium text-on-solid",
                          stay.status === "Checked In"
                            ? "bg-accent-fill hover:bg-accent-fill-hover"
                            : "bg-info-fill hover:bg-info-fill",
                        )}
                      >
                        <span className="flex min-w-0 items-center gap-1.5 truncate">
                          {stay.vip === 1 && (
                            <Star className="size-3.5 shrink-0 fill-amber-300 text-amber-300" aria-hidden />
                          )}
                          <span className="truncate">{stay.guest_name}</span>
                        </span>
                        <Badge tone={stay.status === "Checked In" ? "brand" : "sky"} className="shrink-0 bg-surface/90">
                          {t(stay.status)}
                        </Badge>
                      </button>
                    ) : blk ? (
                      <div className="flex min-h-11 items-center gap-2 rounded-lg border border-control bg-subtle px-3 text-xs font-medium text-secondary-ink">
                        <Lock className="size-3.5 shrink-0" aria-hidden />
                        <span className="truncate">{t(blk.reason)}</span>
                      </div>
                    ) : canBook && selectedDate >= today && isAssignableHkStatus(room.housekeeping_status) ? (
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() => onNewBooking(room, selectedDate)}
                        className="min-h-11 w-full justify-center text-xs"
                      >
                        <Plus className="size-4" aria-hidden />
                        {t("Available — Book")}
                      </Button>
                    ) : !isAssignableHkStatus(room.housekeeping_status) ? (
                      <p className={cn("py-2 text-xs font-medium", tone.text)}>{t(room.housekeeping_status)}</p>
                    ) : (
                      <p className="py-2 text-xs text-muted-ink">{t("Vacant")}</p>
                    )}
                  </div>
                </div>
              )
            })}
          </section>
        ))
      )}
    </div>
  )
}
