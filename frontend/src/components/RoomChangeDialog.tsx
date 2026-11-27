/*  Change a booking's room from wherever the desk is - the reservation
    drawer or the GRC - not only the tape chart (#113). Same writer and the
    same availability rules as the tape chart (movable_rooms / move_reservation);
    the price effect is previewed before anything changes, and a discounted
    stay's discount rules with it: a manager signs the move off here when
    they need one. */

import { useEffect, useMemo, useState } from "react"
import { ArrowRight, BedDouble, Loader2, Search, X } from "lucide-react"
import { call } from "../lib/api"
import { serverError } from "../lib/resource"
import { fmtMoney, moneyStep } from "../lib/money"
import { formatMoveDelta } from "../lib/pmsOperations"
import { useT, t as translate } from "../lib/i18n"
import { cn } from "../lib/utils"
import { supervisorArgs, type DiscountGovernance } from "../lib/reservationDiscount"
import SupervisorSignOff, { SignOffReasons } from "./SupervisorSignOff"
import { Button } from "./ui/button"


import RevocationWarnings, { outstandingWarnings, type RevocationWarning } from "./RevocationWarnings"

type MoveResult = { room: string; room_type: string; pending_revocations?: RevocationWarning[] }

interface MovableRoom {
  name: string
  room_number: string
  room_type: string
  room_type_name: string
  floor: string | null
  housekeeping_status: string | null
  free: boolean
  same_type: boolean
  current: boolean
  swap_reservation?: string | null
}

interface Preview {
  same_type: boolean
  approval_required?: boolean
  original_room_type_name?: string
  currency?: string
  places?: number
  new_room_type_name: string | null
  current_amount: number
  new_amount: number
  difference: number
  auto_price: boolean
  in_house: boolean
  can_set_rate: boolean
  /** a discounted stay: the discount rules the move runs (kamra.api._repriced_governance) */
  discount_governance?: DiscountGovernance | null
  needs_supervisor?: boolean
  supervisors?: { user: string; full_name: string }[]
}

/** What the guest pays after the move: the new room's rate, what they pay
 *  now, or a nightly rate typed in (Revenue Manager / admins). */
type RateMode = "destination" | "source" | "custom"

const REASONS = ["Guest request", "Upgrade", "Maintenance", "Noise / complaint", "Other"]

const hkTone: Record<string, string> = {
  Clean: "bg-success-subtle text-success-ink dark:bg-emerald-950/40 dark:text-emerald-300",
  Pickup: "bg-indigo-subtle text-indigo-ink dark:bg-indigo-950/40 dark:text-indigo-300",
  Inspected: "bg-info-subtle text-info-ink dark:bg-sky-950/40 dark:text-sky-300",
  Ready: "bg-teal-subtle text-teal-ink dark:bg-teal-950/40 dark:text-teal-300",
  Dirty: "bg-warning-subtle text-warning-ink dark:bg-amber-950/40 dark:text-amber-300",
  "Out of Service": "bg-notice-subtle text-notice-ink dark:bg-orange-950/40 dark:text-orange-300",
  "Out of Order": "bg-danger-subtle text-danger-ink dark:bg-rose-950/40 dark:text-rose-300",
}

export default function RoomChangeDialog(props: {
  reservation: string
  currentRoomNumber?: string | null
  /** Pre-pick this room (a bar dropped on its row on the tape chart). The
   *  price preview and the reason still come before the move. */
  initialRoom?: string | null
  onClose: () => void
  onMoved: (r: { room: string; room_type: string }) => void
}) {
  const { t } = useT()
  const [rooms, setRooms] = useState<MovableRoom[] | null>(null)
  const [scope, setScope] = useState<"same" | "all">("same")
  const [q, setQ] = useState("")
  const [pick, setPick] = useState<MovableRoom | null>(null)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [rateMode, setRateMode] = useState<RateMode>("destination")
  const [agreedRate, setAgreedRate] = useState("")
  const [canSetRate, setCanSetRate] = useState(false)
  const upgradeType = rateMode === "source" ? "Complimentary" : "Paid"
  const [reason, setReason] = useState(REASONS[0])
  const [note, setNote] = useState("")
  const [supervisor, setSupervisor] = useState("")
  const [pin, setPin] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [moved, setMoved] = useState<MoveResult | null>(null)
  const swapReservation = pick?.swap_reservation

  useEffect(() => {
    call<MovableRoom[]>("kamra.api.movable_rooms", { reservation: props.reservation })
      .then((list) => {
        setRooms(list)
        const want = props.initialRoom && list.find((r) => r.name === props.initialRoom)
        if (!want || want.current) return
        if (!want.same_type) setScope("all")
        if (want.free || want.swap_reservation) setPick(want)
        else setError(t("Room {room} is booked for these dates.", { room: want.room_number }))
      })
      .catch((e) => setError(serverError(e)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.reservation, props.initialRoom])

  useEffect(() => {
    setPreview(null)
    if (!pick || pick.swap_reservation || (rateMode === "custom" && !(Number(agreedRate) > 0))) return
    let live = true
    call<Preview>("kamra.api.room_move_preview", {
      reservation: props.reservation,
      new_room: pick.name,
      rate_mode: rateMode,
      upgrade_type: rateMode === "custom" ? undefined : pick.same_type ? "None" : upgradeType,
      agreed_rate: rateMode === "custom" ? Number(agreedRate) : undefined,
    })
      .then((p) => {
        if (!live) return
        setPreview(p)
        setCanSetRate(p.can_set_rate)
        if (p.supervisors?.length === 1) setSupervisor(p.supervisors[0].user)
        setError(null)
      })
      .catch((e) => live && setError(serverError(e)))
    return () => {
      live = false
    }
  }, [pick, props.reservation, rateMode, agreedRate, upgradeType])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return
      if (moved) props.onMoved(moved)
      props.onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [props, moved])

  const shown = useMemo(() => {
    const list = (rooms ?? []).filter(
      (r) =>
        !r.current &&
        (scope === "all" || r.same_type) &&
        (!q.trim() || r.room_number.toLowerCase().includes(q.trim().toLowerCase())),
    )
    // free and clean first - the room the desk most likely wants
    const rank = (r: MovableRoom) =>
      (r.free ? 0 : 2) + (["Clean", "Inspected", "Ready"].includes(r.housekeeping_status ?? "") ? 0 : 1)
    return [...list].sort((a, b) => rank(a) - rank(b))
  }, [rooms, scope, q])

  const groups = useMemo(() => {
    const m = new Map<string, MovableRoom[]>()
    for (const r of shown) {
      const k = r.room_type_name
      m.set(k, [...(m.get(k) ?? []), r])
    }
    return [...m.entries()]
  }, [shown])

  const gov = preview?.discount_governance ?? null
  const approvalRequired = !!preview?.approval_required
  const signOff = approvalRequired
    ? { supervisor_user: supervisor, pin_secret: pin }
    : supervisorArgs(gov, supervisor, pin)
  const signed = approvalRequired ? !!(supervisor && pin) : !gov?.needs_supervisor || !!signOff.supervisor_user
  const money = (n: number) => preview?.currency && preview.places !== undefined
    ? formatMoveDelta(Math.abs(n), preview.currency, preview.places)
    : fmtMoney(Math.abs(n))

  async function confirm() {
    if (!pick || (!swapReservation && (!preview || !signed))) return
    setBusy(true)
    setError(null)
    const target = {
      reservation: props.reservation,
      new_room: pick.name,
      reason: note.trim() ? `${reason}: ${note.trim()}` : reason,
    }
    try {
      const r = swapReservation
        ? await call<MoveResult>("kamra.api.move_room", {
            ...target, swap_reservation: swapReservation,
          })
        : await call<MoveResult>("kamra.api.move_reservation", {
            ...target,
            // the dialog shows the re-priced total before this button; pressing
            // it is the confirmation the server asks for
            confirm_price: 1,
            rate_mode: rateMode,
            upgrade_type: rateMode === "custom" ? undefined : pick.same_type ? "None" : upgradeType,
            agreed_rate: rateMode === "custom" ? Number(agreedRate) : undefined,
            ...signOff,
          })
      const warnings = await outstandingWarnings(r.pending_revocations)
      if (warnings.length) setMoved({ ...r, pending_revocations: warnings })
      else {
        props.onMoved(r)
        props.onClose()
      }
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  const freeCount = (rooms ?? []).filter((r) => r.free && !r.current).length
  const currentType = (rooms ?? []).find((r) => r.current)?.room_type_name

  if (moved) return (
    <div role="dialog" aria-modal="true" aria-label={t("Change room")} className="fixed inset-0 z-[60] grid place-items-center bg-black/40 p-4">
      <div className="w-full max-w-md space-y-3 rounded-lg bg-surface p-4">
        <RevocationWarnings warnings={moved.pending_revocations ?? []} />
        <Button onClick={() => { props.onMoved(moved); props.onClose() }}>{t("Done")}</Button>
      </div>
    </div>
  )
  return (
    <div className="fixed inset-0 z-[60] flex items-end justify-center sm:items-center" role="dialog" aria-modal="true" aria-label={t("Change room")}>
      <div className="absolute inset-0 bg-black/40" onClick={props.onClose} aria-hidden />
      <div className="relative flex max-h-[90dvh] w-full max-w-2xl flex-col overflow-hidden rounded-t-2xl bg-surface shadow-2xl sm:rounded-2xl [&_button]:min-h-11 [&_input]:min-h-11 [&_select]:min-h-11">
        <header className="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
          <div>
            <h2 className="text-lg font-semibold text-ink">{t("Change room")}</h2>
            <p className="text-sm text-muted-ink">
              {props.currentRoomNumber
                ? t("Now in {room} · {n} rooms free for these dates", {
                    room: currentType ? `${props.currentRoomNumber} (${currentType})` : props.currentRoomNumber,
                    n: freeCount,
                  })
                : t("{n} rooms free for these dates", { n: freeCount })}
            </p>
          </div>
          <Button variant="ghost" onClick={props.onClose} aria-label={t("Close")}>
            <X className="size-5" />
          </Button>
        </header>

        <div className="flex flex-wrap items-center gap-2 border-b border-line-subtle px-5 py-3">
          <div role="radiogroup" aria-label={t("Room types")} className="inline-flex rounded-lg bg-subtle p-0.5 text-sm">
            {(
              [
                ["same", t("Same type")],
                ["all", t("All types (upgrade)")],
              ] as const
            ).map(([k, label]) => (
              <button
                key={k}
                type="button"
                role="radio"
                aria-checked={scope === k}
                onClick={() => setScope(k)}
                className={cn(
                  "rounded-md px-3 py-1.5 font-medium",
                  scope === k ? "bg-surface text-ink shadow-sm" : "text-muted-ink",
                )}
              >
                {translate(label)}
              </button>
            ))}
          </div>
          <label className="relative ms-auto">
            <Search className="pointer-events-none absolute start-2.5 top-2.5 size-4 text-muted-ink" aria-hidden />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t("Room no.")}
              aria-label={t("Search room number")}
              className="w-32 rounded-lg border border-control py-2 ps-8 pe-2 text-sm focus:outline-2 focus:outline-focus"
            />
          </label>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {!rooms && !error && (
            <p className="flex items-center gap-2 text-sm text-muted-ink">
              <Loader2 className="size-4 animate-spin" aria-hidden /> {t("Loading…")}
            </p>
          )}
          {rooms && groups.length === 0 && (
            <p className="py-6 text-center text-sm text-muted-ink">
              {scope === "same"
                ? t("No other room of this type - try All types.")
                : t("No rooms match.")}
            </p>
          )}
          <div className="space-y-4">
            {groups.map(([type, list]) => (
              <div key={type}>
                <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-ink">{type}</p>
                <div className="grid grid-cols-3 gap-2 sm:grid-cols-5">
                  {list.map((r) => (
                    <button
                      key={r.name}
                      type="button"
                      disabled={!r.free && !r.swap_reservation}
                      aria-pressed={pick?.name === r.name}
                      onClick={() => { setPick(r); setError(null) }}
                      title={r.free || r.swap_reservation ? undefined : t("Booked for these dates")}
                      className={cn(
                        "min-h-11 rounded-lg border px-2.5 py-2 text-start transition-colors",
                        !r.free && !r.swap_reservation && "cursor-not-allowed border-dashed border-line bg-canvas opacity-50",
                        (r.free || r.swap_reservation) && pick?.name !== r.name && "border-line hover:border-control-strong",
                        pick?.name === r.name && "border-accent-control bg-accent-subtle ring-2 ring-accent-control/30",
                      )}
                    >
                      <div className="flex items-center justify-between">
                        <span className="font-semibold text-ink">{r.room_number}</span>
                        {!r.free && <BedDouble className="size-3.5 text-muted-ink" aria-hidden />}
                      </div>
                      <span
                        className={cn(
                          "mt-1 inline-block rounded px-1.5 py-0.5 text-[10px] font-medium",
                          r.free ? hkTone[r.housekeeping_status ?? ""] ?? "bg-subtle text-secondary-ink" : "bg-subtle text-muted-ink",
                        )}
                      >
                        {r.swap_reservation ? t("Swap with occupied room") : r.free ? t(r.housekeeping_status || "-") : t("Booked")}
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>

        <footer className="max-h-[49dvh] shrink-0 space-y-3 overflow-y-auto border-t border-line bg-canvas px-5 py-4">
          {pick && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
              <span className="font-medium text-ink">
                {props.currentRoomNumber ?? "-"}{currentType ? ` (${currentType})` : ""} <ArrowRight className="inline size-3.5 rtl:rotate-180" aria-hidden /> {pick.room_number} ({pick.room_type_name})
              </span>
              {preview?.original_room_type_name && (
                <p className="basis-full text-secondary-ink">
                  {t("Booked room type: {type}", { type: preview.original_room_type_name })}
                </p>
              )}
              {swapReservation ? (
                <p className="basis-full text-secondary-ink">
                  {t("Both reservations keep their booked rates.")}
                </p>
              ) : preview ? (
                preview.difference === 0 ? (
                  <span className="text-muted-ink">{t("No change to the price.")}</span>
                ) : (
                  <span className={preview.difference > 0 ? "text-warning-ink" : "text-success-ink"}>
                    {preview.difference > 0
                      ? t("{type}: stay total +{amt} (now {total})", {
                          type: preview.new_room_type_name ?? "",
                          amt: money(preview.difference),
                          total: money(preview.new_amount),
                        })
                      : t("{type}: stay total -{amt} (now {total})", {
                          type: preview.new_room_type_name ?? "",
                          amt: money(preview.difference),
                          total: money(preview.new_amount),
                        })}
                  </span>
                )
              ) : (
                <Loader2 className="size-4 animate-spin text-muted-ink" aria-label={t("Checking price")} />
              )}
              {!pick.same_type && (
                <div role="radiogroup" aria-label={t("Rate")} className="flex basis-full flex-wrap items-center gap-1.5">
                  {(
                    [
                      ["destination", t("Paid")],
                      ["source", t("Complimentary")],
                      ...(canSetRate ? [["custom", t("Enter a rate")] as const] : []),
                    ] as const
                  ).map(([k, label]) => (
                    <button
                      key={k}
                      type="button"
                      role="radio"
                      aria-checked={rateMode === k}
                      onClick={() => setRateMode(k)}
                      className={cn(
                        "min-h-11 rounded-md border px-2.5 py-1 text-xs font-medium",
                        rateMode === k ? "border-accent-control bg-accent-subtle text-accent-ink" : "border-line bg-surface text-secondary-ink",
                      )}
                    >
                      {label}
                    </button>
                  ))}
                  {rateMode === "custom" && (
                    <input
                      type="number"
                      min={1}
                      step={moneyStep()}
                      inputMode="decimal"
                      value={agreedRate}
                      onChange={(e) => setAgreedRate(e.target.value)}
                      placeholder={t("Rate per night")}
                      aria-label={t("Rate per night")}
                      className="w-32 rounded-md border border-control bg-surface px-2 py-1 text-xs"
                    />
                  )}
                </div>
              )}
              {preview?.in_house && (
                <span className="basis-full text-xs text-muted-ink">
                  {t("The guest is in-house: the old room is marked Dirty for housekeeping.")}
                </span>
              )}
              {pick.housekeeping_status && !["Clean", "Inspected", "Ready"].includes(pick.housekeeping_status) && (
                <span className="basis-full text-xs text-warning-ink">
                  {t("{room} hasn't been cleaned yet.", { room: pick.room_number })}
                </span>
              )}
            </div>
          )}
          {pick && (approvalRequired || gov?.needs_supervisor) && (
            <SupervisorSignOff reason={gov && <SignOffReasons gov={gov} />}
              supervisors={preview?.supervisors ?? gov?.supervisors ?? []}
              supervisor={supervisor} pin={pin} onSupervisor={setSupervisor} onPin={setPin} />
          )}
          <div className="flex flex-wrap items-center gap-2">
            <select
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              aria-label={t("Reason")}
              className="min-h-11 rounded-lg border border-control bg-surface px-2.5 py-2 text-sm"
            >
              {REASONS.map((r) => (
                <option key={r} value={r}>
                  {t(r)}
                </option>
              ))}
            </select>
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder={t("Note (optional)")}
              className="min-w-0 flex-1 min-h-11 rounded-lg border border-control bg-surface px-2.5 py-2 text-sm"
            />
            <Button className="min-h-11" disabled={!pick || busy || (!swapReservation && (!preview || !signed))} onClick={confirm}>
              {busy ? t("Moving…") : pick ? (swapReservation
                ? t("Swap with room {room}", { room: pick.room_number })
                : t("Move to {room}", { room: pick.room_number })) : t("Pick a room")}
            </Button>
          </div>
          {error && (
            <div className="rounded-lg border border-danger-line bg-danger-subtle px-3 py-2 text-sm text-danger-ink">{translate(error)}</div>
          )}
        </footer>
      </div>
    </div>
  )
}
